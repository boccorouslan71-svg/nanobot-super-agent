"""Tests for the pre-start state bootstrap.

This step runs before nanobot loads its config, which is the only moment a
restored ``config.json`` can still take effect. Its exit code is a contract:
0 means "start", and a failed restore now degrades (marker + frozen mirrors)
instead of refusing to start — a crash loop kept the whole agent offline
whenever Supabase had a slow stretch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanobot.persistence import bootstrap


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "NANOBOT_STATE_TREE",
        "NANOBOT_STATE_TABLE",
        "NANOBOT_STATE_TREE_KEY",
        "NANOBOT_STATE_TREE_MAX_BYTES",
        "NANOBOT_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)


def _creds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_test")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "data"))


def test_no_credentials_is_a_no_op_that_allows_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _fail(**_kwargs: Any) -> Any:  # pragma: no cover - must not run
        nonlocal called
        called = True

    monkeypatch.setattr(bootstrap, "restore_tree", _fail)
    assert bootstrap.main() == 0
    assert called is False


def test_unresolved_placeholders_count_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A template shipped without env substitution must not look like credentials."""
    monkeypatch.setenv("SUPABASE_URL", "${SUPABASE_URL}")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "${SUPABASE_SERVICE_KEY}")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path))

    assert bootstrap.main() == 0


def test_explicit_opt_out_skips_the_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _creds(monkeypatch, tmp_path)
    monkeypatch.setenv("NANOBOT_STATE_TREE", "0")

    def _fail(**_kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("restore must not run when disabled")

    monkeypatch.setattr(bootstrap, "restore_tree", _fail)
    assert bootstrap.main() == 0


def test_successful_restore_allows_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _creds(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    async def _restore(*, url: str, service_key: str, data_dir: Path) -> list[str]:
        seen["url"] = url
        seen["data_dir"] = data_dir
        return ["config.json", "workspace/skills/make/SKILL.md"]

    monkeypatch.setattr(bootstrap, "restore_tree", _restore)

    assert bootstrap.main() == 0
    assert seen["url"] == "https://example.supabase.co"
    assert seen["data_dir"] == tmp_path / "data"
    out = capsys.readouterr().out
    assert "restored 2 file(s)" in out
    assert "config.json" in out


def test_empty_remote_still_allows_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """First ever boot: nothing mirrored yet is normal, not a failure."""
    _creds(monkeypatch, tmp_path)

    async def _restore(**_kwargs: Any) -> list[str]:
        return []

    monkeypatch.setattr(bootstrap, "restore_tree", _restore)

    assert bootstrap.main() == 0
    assert "nothing to restore" in capsys.readouterr().out


def test_restore_failure_degrades_instead_of_refusing_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exhausted retries must not crash-loop the container: degrade and start."""
    _creds(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    attempts = 0

    async def _restore(**_kwargs: Any) -> list[str]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("supabase unreachable")

    monkeypatch.setattr(bootstrap, "restore_tree", _restore)

    assert bootstrap.main() == 0
    assert attempts == bootstrap._ATTEMPTS
    out = capsys.readouterr().out
    assert "DEGRADED START" in out
    assert "refusing to start" not in out
    # The gate marker is the whole contract: mirrors read it to freeze pushes.
    assert (data_dir / ".restore-pending").exists()


def test_successful_restore_clears_a_stale_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A healthy boot must lift a degraded start left over by an earlier one."""
    _creds(monkeypatch, tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / ".restore-pending").write_text("stale", encoding="utf-8")

    async def _restore(**_kwargs: Any) -> list[str]:
        return ["config.json"]

    monkeypatch.setattr(bootstrap, "restore_tree", _restore)

    assert bootstrap.main() == 0
    assert "restored 1 file(s)" in capsys.readouterr().out
    assert not (data_dir / ".restore-pending").exists()


def test_transient_failure_recovers_within_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _creds(monkeypatch, tmp_path)
    attempts = 0

    async def _restore(**_kwargs: Any) -> list[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return ["config.json"]

    monkeypatch.setattr(bootstrap, "restore_tree", _restore)

    assert bootstrap.main() == 0
    assert attempts == 2


def test_data_dir_resolution_prefers_explicit_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "explicit"))
    assert bootstrap.resolve_data_dir() == tmp_path / "explicit"

    monkeypatch.delenv("NANOBOT_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert bootstrap.resolve_data_dir() == tmp_path / "home" / ".nanobot"
