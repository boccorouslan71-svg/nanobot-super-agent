"""Tests for the degraded-start gate shared by the Supabase mirrors.

The gate is what makes a failed pre-start restore survivable: one marker file
freezes every snapshot push until a background restorer has pulled the full
mirror. These tests pin the marker lifecycle and each mirror's obedience to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanobot.persistence import restore_gate
from nanobot.persistence.supabase_store import WorkspaceStateMirror


def test_marker_lifecycle_roundtrip(tmp_path: Path) -> None:
    assert restore_gate.is_held(tmp_path) is False

    restore_gate.place(tmp_path, "test reason")
    assert restore_gate.is_held(tmp_path) is True
    assert "test reason" in (tmp_path / ".restore-pending").read_text(encoding="utf-8")

    restore_gate.clear(tmp_path)
    assert restore_gate.is_held(tmp_path) is False


def test_clear_is_a_noop_without_a_marker(tmp_path: Path) -> None:
    restore_gate.clear(tmp_path)  # must not raise
    assert restore_gate.is_held(tmp_path) is False


def test_default_data_dir_prefers_explicit_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "explicit"))
    assert restore_gate.default_data_dir() == tmp_path / "explicit"

    monkeypatch.delenv("NANOBOT_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert restore_gate.default_data_dir() == tmp_path / "home" / ".nanobot"


class _FakeStore:
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


@pytest.mark.asyncio
async def test_workspace_mirror_snapshot_is_frozen_by_the_marker(
    tmp_path: Path,
) -> None:
    """The per-file JSON mirror obeys the same gate as the tree mirror."""
    workspace = tmp_path / "workspace"
    (workspace / "cron").mkdir(parents=True)
    (workspace / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")

    store = _FakeStore()
    mirror = WorkspaceStateMirror(
        store=store,
        workspace_path=workspace,
        paths=["cron/jobs.json"],
        data_path=tmp_path / "data",
    )

    # No marker: normal behaviour, a changed file is pushed.
    assert await mirror.snapshot() == ["cron/jobs.json"]
    assert store.pushes == 1

    # Marker placed (degraded start): every push is skipped.
    restore_gate.place(tmp_path / "data", "test: degraded start")
    (workspace / "cron" / "jobs.json").write_text('{"jobs": [1]}', encoding="utf-8")
    assert await mirror.snapshot() == []
    assert store.pushes == 1  # unchanged — nothing reached Supabase

    # Marker cleared: the cadence resumes on the next cycle.
    restore_gate.clear(tmp_path / "data")
    assert await mirror.snapshot() == ["cron/jobs.json"]
    assert store.pushes == 2


@pytest.mark.asyncio
async def test_workspace_mirror_falls_back_to_the_default_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without data_path configured, the gate still resolves the real data dir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "default-data"

    monkeypatch.setenv("NANOBOT_DATA_DIR", str(data_dir))
    restore_gate.place(data_dir, "test: degraded start")

    mirror = WorkspaceStateMirror(
        store=_FakeStore(),
        workspace_path=workspace,
        paths=["anything.json"],
    )
    assert await mirror.snapshot() == []
