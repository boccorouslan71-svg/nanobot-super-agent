"""Deployment-owned settings must reach an instance whose config already exists.

The durable mirror makes ``config.json`` survive a recycle, so ``entrypoint.sh``
stops copying the template. That is what protects WebUI-edited provider keys —
and what silently strands infrastructure fixes: the mirror cadence was changed in
the template while the live service kept snapshotting on the old schedule.

These tests pin the narrow reconcile that closes that gap, and the boundaries it
must not cross: no credentials, no user-owned sections, no write unless the
merged document validates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.persistence import reconcile_platform_config as rpc

_TEMPLATE = {
    "agents": {"defaults": {"model": "agnes-2.5-flash", "provider": "agnes"}},
    "providers": {"agnes": {"apiKey": "${AGNES_API_KEY}"}},
    "persistence": {
        "supabase": {
            "enabled": True,
            "url": "${SUPABASE_URL}",
            "serviceKey": "${SUPABASE_SERVICE_KEY}",
            "paths": ["cron/jobs.json", "data:auth/mcp.json"],
            "restoreOnStart": True,
            "snapshotIntervalS": 15,
            "treeEnabled": True,
            "treeKey": "state/data-tree",
            "treeSnapshotIntervalS": 15,
            "treeMaxBytes": 40000000,
        },
        "keepalive": {"enabled": True, "path": "/", "intervalS": 300},
    },
}

_LIVE = {
    "agents": {"defaults": {"model": "gemini-3.6-flash", "provider": "gemini"}},
    "providers": {"agnes": {"apiKey": "sk-live-secret"}, "gemini": {"apiKey": "live-gemini"}},
    "channels": {"telegram": {"enabled": True, "token": "111:live"}},
    "modelPresets": {"fast": {"model": "x", "provider": "agnes"}},
    "persistence": {
        "supabase": {
            "enabled": True,
            "url": "https://live.supabase.co",
            "serviceKey": "sb_secret_live",
            "paths": ["cron/jobs.json", "data:auth/mcp.json"],
            "restoreOnStart": True,
            "snapshotIntervalS": 120,
            "treeEnabled": True,
            "treeKey": "state/data-tree",
            "treeSnapshotIntervalS": 300,
            "treeMaxBytes": 40000000,
        },
        "keepalive": {"enabled": True, "path": "/", "intervalS": 300},
    },
}


def _write(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document, indent=2))
    return path


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _write(tmp_path / "config.json", _LIVE),
        _write(tmp_path / "render-config.json", _TEMPLATE),
    )


class TestPlatformSettingsShip:
    def test_cadence_reaches_an_existing_config(self, paths: tuple[Path, Path]) -> None:
        config, template = paths

        changes = rpc.reconcile(config, template)

        assert changes == {
            "persistence.supabase.snapshotIntervalS": (120, 15),
            "persistence.supabase.treeSnapshotIntervalS": (300, 15),
        }
        stored = json.loads(config.read_text())
        assert stored["persistence"]["supabase"]["snapshotIntervalS"] == 15
        assert stored["persistence"]["supabase"]["treeSnapshotIntervalS"] == 15

    def test_the_result_is_a_loadable_config(self, paths: tuple[Path, Path]) -> None:
        from nanobot.config.schema import Config

        config, template = paths
        rpc.reconcile(config, template)

        loaded = Config(**json.loads(config.read_text()))
        assert loaded.persistence.supabase.snapshot_interval_s == 15
        assert loaded.persistence.supabase.tree_snapshot_interval_s == 15

    def test_running_twice_changes_nothing_the_second_time(self, paths: tuple[Path, Path]) -> None:
        config, template = paths
        rpc.reconcile(config, template)
        after_first = config.read_bytes()
        mtime = config.stat().st_mtime_ns

        assert rpc.reconcile(config, template) == {}
        assert config.read_bytes() == after_first
        assert config.stat().st_mtime_ns == mtime, "an idle reconcile must not touch the file"


class TestUserStateIsNeverTouched:
    def test_credentials_survive(self, paths: tuple[Path, Path]) -> None:
        """The template holds ${VAR} placeholders; copying one over a live secret
        would silently disconnect the mirror it is meant to protect."""
        config, template = paths

        rpc.reconcile(config, template)

        supabase = json.loads(config.read_text())["persistence"]["supabase"]
        assert supabase["url"] == "https://live.supabase.co"
        assert supabase["serviceKey"] == "sb_secret_live"

    def test_user_owned_sections_survive(self, paths: tuple[Path, Path]) -> None:
        config, template = paths

        rpc.reconcile(config, template)

        stored = json.loads(config.read_text())
        assert stored["providers"]["agnes"]["apiKey"] == "sk-live-secret"
        assert stored["providers"]["gemini"]["apiKey"] == "live-gemini"
        assert stored["channels"]["telegram"]["token"] == "111:live"
        assert stored["modelPresets"] == {"fast": {"model": "x", "provider": "agnes"}}
        # A model switched at runtime is a user decision, not deployment plumbing.
        assert stored["agents"]["defaults"]["model"] == "gemini-3.6-flash"

    def test_credential_keys_are_excluded_even_if_listed(self, tmp_path: Path) -> None:
        config = _write(tmp_path / "config.json", _LIVE)
        template = _write(tmp_path / "render-config.json", _TEMPLATE)

        changes = rpc.plan_changes(
            json.loads(config.read_text()),
            json.loads(template.read_text()),
            keys=("persistence.supabase.url", "persistence.supabase.serviceKey"),
        )

        assert changes == {}


class TestItCannotBreakABootingInstance:
    def test_a_template_that_would_not_validate_is_refused(self, tmp_path: Path) -> None:
        config = _write(tmp_path / "config.json", _LIVE)
        broken = json.loads(json.dumps(_TEMPLATE))
        broken["persistence"]["supabase"]["treeSnapshotIntervalS"] = 3  # below the floor
        template = _write(tmp_path / "render-config.json", broken)
        before = config.read_bytes()

        with pytest.raises(Exception):
            rpc.reconcile(config, template)

        assert config.read_bytes() == before, "a bad template must not corrupt a working config"

    def test_first_boot_is_a_no_op(self, tmp_path: Path) -> None:
        """entrypoint.sh copies the template wholesale when no config exists."""
        template = _write(tmp_path / "render-config.json", _TEMPLATE)

        assert rpc.reconcile(tmp_path / "config.json", template) == {}

    def test_a_missing_template_is_reported(self, tmp_path: Path) -> None:
        config = _write(tmp_path / "config.json", _LIVE)

        with pytest.raises(FileNotFoundError):
            rpc.reconcile(config, tmp_path / "absent.json")

    def test_a_key_the_template_stopped_pinning_is_left_alone(self, tmp_path: Path) -> None:
        config = _write(tmp_path / "config.json", _LIVE)
        thin = {"persistence": {"supabase": {"snapshotIntervalS": 15}}}
        template = _write(tmp_path / "render-config.json", thin)

        changes = rpc.reconcile(config, template)

        assert list(changes) == ["persistence.supabase.snapshotIntervalS"]
        stored = json.loads(config.read_text())
        assert stored["persistence"]["supabase"]["treeSnapshotIntervalS"] == 300
        assert stored["persistence"]["supabase"]["treeKey"] == "state/data-tree"

    def test_no_temp_file_is_left_behind(self, paths: tuple[Path, Path]) -> None:
        config, template = paths

        rpc.reconcile(config, template)

        assert [p.name for p in config.parent.iterdir() if "tmp" in p.name] == []


class TestEntrypointWiring:
    def test_reconcile_runs_after_the_template_copy(self) -> None:
        """Order matters: reconciling before the copy would have nothing to fix."""
        entrypoint = Path(__file__).resolve().parents[2] / "entrypoint.sh"
        text = entrypoint.read_text()

        copy_at = text.find("cp /app/render-config.json")
        reconcile_at = text.find("nanobot.persistence.reconcile_platform_config")

        assert -1 < copy_at < reconcile_at
        assert "continuing with the stored config" in text, "a reconcile failure must not be fatal"
