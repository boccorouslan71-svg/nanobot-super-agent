"""Tests for the whole-tree Supabase mirror.

The tree mirror is the last line of defence for a deployment with no persistent
disk: if it silently mirrors nothing, or overwrites a healthy backup with an
empty tree, the user loses every provider key, skill, and workspace file they
accumulated. These tests pin the behaviours that make it safe.
"""

from __future__ import annotations

import base64
import gzip
import io
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from nanobot.persistence.supabase_store import SupabasePersistenceError
from nanobot.persistence.tree_mirror import TreeArchiveMirror


class FakeStore:
    """In-memory stand-in for SupabaseStateStore."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.pushes = 0

    async def pull(self, key: str) -> dict[str, Any] | None:
        if key not in self.rows:
            return None
        return {"key": key, "content": self.rows[key]}

    async def push(self, key: str, content: Any) -> str:
        self.rows[key] = content
        self.pushes += 1
        return "digest"

    async def aclose(self) -> None:  # pragma: no cover - parity with the real store
        return None


def _tree(root: Path) -> Path:
    """Create a data-dir-shaped tree with the state a user actually accumulates."""
    (root / "workspace" / "skills" / "make").mkdir(parents=True)
    (root / "sessions").mkdir(parents=True)
    (root / "auth").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "workspace" / "__pycache__").mkdir(parents=True)

    (root / "config.json").write_text('{"providers": {"gemini": {"apiKey": "live-key"}}}')
    (root / "workspace" / "skills" / "make" / "SKILL.md").write_text("# Make skill")
    (root / "workspace" / "notes.md").write_text("agent work product")
    (root / "sessions" / "telegram.json").write_text('{"messages": []}')
    (root / "auth" / "mcp.json").write_text('{"make": {"token": "t"}}')
    (root / "logs" / "app.log").write_text("noise" * 1000)
    (root / "workspace" / "__pycache__" / "x.pyc").write_bytes(b"\x00cached")
    return root


def _mirror(root: Path, store: FakeStore, **kwargs: Any) -> TreeArchiveMirror:
    return TreeArchiveMirror(store=store, root=root, key="state/data-tree", **kwargs)


def test_archive_covers_user_state_and_skips_noise(tmp_path: Path) -> None:
    mirror = _mirror(_tree(tmp_path), FakeStore())
    names = {p.relative_to(tmp_path).as_posix() for p in mirror.iter_files()}

    assert "config.json" in names
    assert "workspace/skills/make/SKILL.md" in names
    assert "workspace/notes.md" in names
    assert "sessions/telegram.json" in names
    assert "auth/mcp.json" in names
    # Regenerated or write-heavy paths stay out of the payload.
    assert "logs/app.log" not in names
    assert "workspace/__pycache__/x.pyc" not in names


def test_archive_is_byte_stable_for_identical_content(tmp_path: Path) -> None:
    """Without this, every cycle looks changed and re-uploads the whole tree."""
    mirror = _mirror(_tree(tmp_path), FakeStore())
    first, count_a = mirror.build_archive()
    second, count_b = mirror.build_archive()

    assert first == second
    assert count_a == count_b > 0


def test_symlinks_are_not_mirrored(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "workspace" / "escape.md").symlink_to(tmp_path / "outside.md")
    mirror = _mirror(root, FakeStore())

    names = {p.relative_to(root).as_posix() for p in mirror.iter_files()}
    assert "workspace/escape.md" not in names


@pytest.mark.asyncio
async def test_snapshot_pushes_once_then_skips_unchanged(tmp_path: Path) -> None:
    store = FakeStore()
    mirror = _mirror(_tree(tmp_path), store)

    assert await mirror.snapshot() > 0
    assert store.pushes == 1
    assert await mirror.snapshot() == 0
    assert store.pushes == 1

    (tmp_path / "workspace" / "notes.md").write_text("new work")
    assert await mirror.snapshot() > 0
    assert store.pushes == 2


@pytest.mark.asyncio
async def test_snapshot_refuses_to_overwrite_remote_with_empty_tree(tmp_path: Path) -> None:
    """The catastrophic case: a fresh container erasing the only copy of the state."""
    store = FakeStore()
    populated = _mirror(_tree(tmp_path / "full"), store)
    await populated.snapshot()
    remote_before = dict(store.rows["state/data-tree"])

    empty_root = tmp_path / "fresh"
    empty_root.mkdir()
    with pytest.raises(SupabasePersistenceError, match="refusing to overwrite"):
        await _mirror(empty_root, store).snapshot()

    assert store.rows["state/data-tree"] == remote_before


@pytest.mark.asyncio
async def test_empty_tree_with_empty_remote_is_not_an_error(tmp_path: Path) -> None:
    store = FakeStore()
    assert await _mirror(tmp_path, store).snapshot() == 0
    assert store.pushes == 0


@pytest.mark.asyncio
async def test_restore_repopulates_a_fresh_container(tmp_path: Path) -> None:
    store = FakeStore()
    await _mirror(_tree(tmp_path / "old"), store).snapshot()

    fresh = tmp_path / "new"
    fresh.mkdir()
    restored = await _mirror(fresh, store).restore()

    assert "config.json" in restored
    assert (fresh / "config.json").read_text() == (
        '{"providers": {"gemini": {"apiKey": "live-key"}}}'
    )
    assert (fresh / "workspace" / "skills" / "make" / "SKILL.md").read_text() == "# Make skill"
    assert (fresh / "sessions" / "telegram.json").exists()
    assert (fresh / "auth" / "mcp.json").exists()


@pytest.mark.asyncio
async def test_restore_never_clobbers_existing_local_work(tmp_path: Path) -> None:
    store = FakeStore()
    await _mirror(_tree(tmp_path / "old"), store).snapshot()

    fresh = tmp_path / "new"
    (fresh / "workspace").mkdir(parents=True)
    (fresh / "workspace" / "notes.md").write_text("newer local work")
    (fresh / "config.json").write_text("")  # empty counts as missing

    restored = await _mirror(fresh, store).restore()

    assert (fresh / "workspace" / "notes.md").read_text() == "newer local work"
    assert "workspace/notes.md" not in restored
    assert "config.json" in restored
    assert "live-key" in (fresh / "config.json").read_text()


@pytest.mark.asyncio
async def test_restore_rejects_path_traversal_members(tmp_path: Path) -> None:
    """A malicious or corrupted row must not write outside the tree root."""
    store = FakeStore()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in ("../escaped.txt", "/etc/absolute.txt", "ok.txt"):
            data = b"payload"
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = gzip.compress(raw.getvalue(), mtime=0)
    store.rows["state/data-tree"] = {
        "format": "tar.gz+base64",
        "data": base64.b64encode(archive).decode("ascii"),
        "file_count": 3,
        "byte_size": len(archive),
    }

    root = tmp_path / "root"
    root.mkdir()
    restored = await _mirror(root, store).restore()

    assert restored == ["ok.txt"]
    assert not (tmp_path / "escaped.txt").exists()
    assert not Path("/etc/absolute.txt").exists()


@pytest.mark.asyncio
async def test_restore_reports_a_malformed_row_loudly(tmp_path: Path) -> None:
    store = FakeStore()
    store.rows["state/data-tree"] = {"format": "zip", "data": "nope"}

    with pytest.raises(SupabasePersistenceError, match="unsupported format"):
        await _mirror(tmp_path, store).restore()


@pytest.mark.asyncio
async def test_restore_without_remote_row_is_a_no_op(tmp_path: Path) -> None:
    assert await _mirror(tmp_path, FakeStore()).restore() == []


def test_oversized_tree_fails_loudly_naming_the_biggest_files(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    # Incompressible on purpose: the cap applies to the compressed archive.
    (root / "workspace" / "huge.bin").write_bytes(os.urandom(200_000))
    mirror = _mirror(root, FakeStore(), max_bytes=1_000)

    with pytest.raises(SupabasePersistenceError, match="huge.bin"):
        mirror.build_archive()


@pytest.mark.asyncio
async def test_executable_bit_survives_a_round_trip(tmp_path: Path) -> None:
    store = FakeStore()
    old = _tree(tmp_path / "old")
    script = old / "workspace" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    await _mirror(old, store).snapshot()

    fresh = tmp_path / "new"
    fresh.mkdir()
    await _mirror(fresh, store).restore()

    assert (fresh / "workspace" / "run.sh").stat().st_mode & 0o111


class TestRenderedVideoStaysOutOfTheArchive:
    """Generated clips must not be able to break the mirror they share a disk with.

    The photo-zoom-video skill writes mp4s into the workspace. They are large,
    they accumulate, and the archive fails *wholesale* past its size cap — so a
    batch of clips could take config.json and the cron store down with it. A clip
    is reproducible from its source photo and one command; durable state is not.
    """

    def test_clips_are_excluded_but_their_sources_are_kept(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        (root / "output").mkdir(parents=True)
        (root / "output" / "holiday_zoom.mp4").write_bytes(os.urandom(3_000_000))
        (root / "output" / "reel.mov").write_bytes(os.urandom(1_000_000))
        (root / "output" / "clip.webm").write_bytes(os.urandom(500_000))
        (root / "output" / "source.jpg").write_bytes(os.urandom(40_000))
        (root / "output" / "caption.md").write_text("the copy that goes with the clip")

        names = {p.relative_to(root).as_posix() for p in _mirror(root, FakeStore()).iter_files()}

        assert "output/holiday_zoom.mp4" not in names
        assert "output/reel.mov" not in names
        assert "output/clip.webm" not in names
        # The inputs and the text around them are exactly what must survive.
        assert "output/source.jpg" in names
        assert "output/caption.md" in names

    def test_a_batch_of_clips_cannot_blow_the_size_cap(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        (root / "output").mkdir(parents=True)
        for index in range(12):
            (root / "output" / f"clip{index:02d}.mp4").write_bytes(os.urandom(4_000_000))

        # 48 MB of clips on disk, and the archive still builds well under its cap.
        archive, count = _mirror(root, FakeStore(), max_bytes=1_000_000).build_archive()

        assert count > 0
        assert len(archive) < 1_000_000
