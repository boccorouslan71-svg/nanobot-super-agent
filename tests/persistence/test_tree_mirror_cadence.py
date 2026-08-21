"""The cheap-change-detection path that makes a 15s mirror cadence affordable.

At a 15s cadence the mirror wakes 240 times an hour. Building the archive each
time means reading and gzipping every mirrored file, and on an idle tree every
one of those payloads is identical to the last. These tests pin the fast path
that skips the build, and — more importantly — pin the cases where it must NOT
skip, because a mirror that stops noticing changes is worse than a slow one.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from nanobot.persistence.supabase_store import SupabasePersistenceError
from nanobot.persistence.tree_mirror import TreeArchiveMirror
from tests.persistence.test_tree_mirror import FakeStore, _tree


class CountingMirror(TreeArchiveMirror):
    """Mirror that records how often the expensive archive build ran."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.builds = 0

    def build_archive(self) -> tuple[bytes, int]:
        self.builds += 1
        return super().build_archive()


def _mirror(root: Path, store: FakeStore, **kwargs: Any) -> CountingMirror:
    return CountingMirror(store=store, root=root, key="state/data-tree", **kwargs)


def _touch_later(path: Path, content: str) -> None:
    """Rewrite a file and make sure its mtime is observably newer."""
    path.write_text(content)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


class TestIdleCyclesAreCheap:
    @pytest.mark.asyncio
    async def test_an_unchanged_tree_is_not_rebuilt(self, tmp_path: Path) -> None:
        store = FakeStore()
        mirror = _mirror(_tree(tmp_path), store)

        assert await mirror.snapshot() > 0
        assert mirror.builds == 1
        assert store.pushes == 1

        for _ in range(20):  # five minutes of 15s cycles on an idle agent
            assert await mirror.snapshot() == 0

        assert mirror.builds == 1, "an idle cycle must not read or gzip the tree"
        assert store.pushes == 1, "and must not push"

    @pytest.mark.asyncio
    async def test_signature_is_cheaper_than_the_build_it_guards(self, tmp_path: Path) -> None:
        """The guard has to be strictly cheaper than the work, not just different."""
        root = tmp_path / "big"
        (root / "workspace").mkdir(parents=True)
        for i in range(200):
            (root / "workspace" / f"note{i}.md").write_bytes(os.urandom(20_000))
        mirror = _mirror(root, FakeStore())

        start = time.perf_counter()
        mirror.tree_signature()
        signature_s = time.perf_counter() - start

        start = time.perf_counter()
        mirror.build_archive()
        build_s = time.perf_counter() - start

        assert signature_s < build_s / 2, (
            f"signature {signature_s * 1000:.1f}ms vs build {build_s * 1000:.1f}ms"
        )

    @pytest.mark.asyncio
    async def test_force_still_rebuilds(self, tmp_path: Path) -> None:
        store = FakeStore()
        mirror = _mirror(_tree(tmp_path), store)
        await mirror.snapshot()

        await mirror.snapshot(force=True)

        assert mirror.builds == 2, "an explicit force must bypass the fast path"


class TestChangesAreNeverMissed:
    @pytest.mark.asyncio
    async def test_a_new_file_is_picked_up_on_the_next_cycle(self, tmp_path: Path) -> None:
        store = FakeStore()
        root = _tree(tmp_path)
        mirror = _mirror(root, store)
        await mirror.snapshot()
        assert await mirror.snapshot() == 0

        (root / "workspace" / "skills" / "new-skill.md").write_text("# built by the agent")

        assert await mirror.snapshot() > 0
        assert store.pushes == 2

    @pytest.mark.asyncio
    async def test_an_edit_of_identical_length_is_detected(self, tmp_path: Path) -> None:
        """Size alone would miss this; mtime is why the signature catches it."""
        store = FakeStore()
        root = _tree(tmp_path)
        target = root / "workspace" / "notes.md"
        mirror = _mirror(root, store)
        await mirror.snapshot()

        original = target.read_text()
        _touch_later(target, "X" * len(original))

        assert await mirror.snapshot() > 0
        assert store.pushes == 2

    @pytest.mark.asyncio
    async def test_a_deleted_file_is_mirrored_as_a_change(self, tmp_path: Path) -> None:
        store = FakeStore()
        root = _tree(tmp_path)
        mirror = _mirror(root, store)
        await mirror.snapshot()

        (root / "workspace" / "notes.md").unlink()

        assert await mirror.snapshot() > 0
        assert store.pushes == 2

    @pytest.mark.asyncio
    async def test_a_cron_job_written_between_cycles_is_mirrored(self, tmp_path: Path) -> None:
        """The case the cadence exists for: a schedule created seconds before a crash."""
        store = FakeStore()
        root = _tree(tmp_path)
        (root / "workspace" / "cron").mkdir(parents=True)
        jobs = root / "workspace" / "cron" / "jobs.json"
        jobs.write_text('{"version": 1, "jobs": []}')
        mirror = _mirror(root, store)
        await mirror.snapshot()

        _touch_later(jobs, '{"version": 1, "jobs": [{"id": "new-job"}]}')

        assert await mirror.snapshot() > 0
        restored = tmp_path / "fresh"
        restored.mkdir()
        await _mirror(restored, store).restore()
        assert "new-job" in (restored / "workspace" / "cron" / "jobs.json").read_text()


class TestTheFastPathCannotStrandTheMirror:
    @pytest.mark.asyncio
    async def test_a_failed_push_is_retried_on_the_next_cycle(self, tmp_path: Path) -> None:
        """The signature must only be trusted after the push actually landed."""

        class BrokenOnceStore(FakeStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = True

            async def push(self, key: str, content: Any) -> str:
                if self.fail_next:
                    self.fail_next = False
                    raise SupabasePersistenceError("upstream 503")
                return await super().push(key, content)

        store = BrokenOnceStore()
        mirror = _mirror(_tree(tmp_path), store)

        with pytest.raises(SupabasePersistenceError):
            await mirror.snapshot()
        assert store.pushes == 0

        assert await mirror.snapshot() > 0, "the retry must not be skipped as 'unchanged'"
        assert store.pushes == 1

    @pytest.mark.asyncio
    async def test_a_touched_but_unchanged_file_is_not_pushed_twice(self, tmp_path: Path) -> None:
        store = FakeStore()
        root = _tree(tmp_path)
        target = root / "workspace" / "notes.md"
        mirror = _mirror(root, store)
        await mirror.snapshot()

        _touch_later(target, target.read_text())  # rewritten, same bytes

        assert await mirror.snapshot() == 0, "the digest is still the authority on pushing"
        assert store.pushes == 1
        assert mirror.builds == 2
        # And the new signature is now remembered, so the next cycle is cheap again.
        assert await mirror.snapshot() == 0
        assert mirror.builds == 2

    @pytest.mark.asyncio
    async def test_an_empty_tree_still_refuses_to_erase_the_mirror(self, tmp_path: Path) -> None:
        """The empty-tree guard must not be short-circuited by the fast path."""
        store = FakeStore()
        await _mirror(_tree(tmp_path / "populated"), store).snapshot()

        empty = tmp_path / "empty"
        empty.mkdir()
        mirror = _mirror(empty, store)

        for _ in range(3):  # every cycle must keep refusing, not go quiet
            with pytest.raises(SupabasePersistenceError, match="refusing to overwrite"):
                await mirror.snapshot()
