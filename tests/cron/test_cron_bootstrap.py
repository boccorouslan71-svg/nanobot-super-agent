"""Tests for version-controlled cron declaration bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.config.schema import Config, CronDeclarationConfig
from nanobot.cron.bootstrap import bootstrap_declared_cron_jobs
from nanobot.cron.service import CronService
from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule

_OWNER = {"channel": "telegram", "to": "8888207809"}


def _config(*declarations: dict, enabled: bool = True, prune: bool = True) -> Config:
    return Config(
        cron={
            "enabled": enabled,
            "pruneRemoved": prune,
            "declarations": list(declarations),
        }
    )


def _declaration(job_id: str = "daily-brief", **overrides: object) -> dict:
    payload: dict = {
        "id": job_id,
        "message": "Résume mes emails du jour",
        "cron": "0 8 * * *",
        **_OWNER,
    }
    payload.update(overrides)
    return payload


def _service(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron" / "jobs.json")


class TestDeclarationSchema:
    def test_derives_stable_job_id(self) -> None:
        declaration = CronDeclarationConfig(**_declaration())
        assert declaration.job_id == "declared:daily-brief"
        assert CronDeclarationConfig.is_declared_job_id(declaration.job_id)
        assert not CronDeclarationConfig.is_declared_job_id("dream")

    def test_builds_cron_schedule_with_timezone(self) -> None:
        declaration = CronDeclarationConfig(**_declaration())
        schedule = declaration.build_schedule("Africa/Porto-Novo")
        assert (schedule.kind, schedule.expr, schedule.tz) == (
            "cron",
            "0 8 * * *",
            "Africa/Porto-Novo",
        )
        assert declaration.describe_schedule() == "cron 0 8 * * *"

    def test_builds_interval_schedule_in_milliseconds(self) -> None:
        declaration = CronDeclarationConfig(
            **_declaration(cron=None, every_minutes=30)
        )
        schedule = declaration.build_schedule("Africa/Porto-Novo")
        assert (schedule.kind, schedule.every_ms) == ("every", 1_800_000)
        assert declaration.describe_schedule() == "every 30m"

    def test_declaration_timezone_overrides_agent_default(self) -> None:
        declaration = CronDeclarationConfig(**_declaration(timezone="Europe/Paris"))
        assert declaration.build_schedule("Africa/Porto-Novo").tz == "Europe/Paris"

    def test_session_key_is_channel_scoped(self) -> None:
        declaration = CronDeclarationConfig(**_declaration())
        assert declaration.session_key == "telegram:8888207809"

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"cron": None}, "exactly one"),
            ({"every_minutes": 5}, "exactly one"),
            ({"message": "   "}, "non-empty 'message'"),
            ({"channel": None}, "'channel' and 'to'"),
            ({"to": None}, "'channel' and 'to'"),
            ({"id": "Bad_ID"}, "String should match pattern"),
            ({"every_minutes": 0, "cron": None}, "greater than or equal to 1"),
        ],
    )
    def test_rejects_unusable_declarations(
        self, overrides: dict, expected: str
    ) -> None:
        with pytest.raises(ValidationError, match=expected):
            CronDeclarationConfig(**_declaration(**overrides))

    def test_disabled_declaration_may_omit_delivery_target(self) -> None:
        declaration = CronDeclarationConfig(
            id="paused", enabled=False, every_minutes=15
        )
        assert declaration.enabled is False

    def test_rejects_duplicate_declaration_ids(self) -> None:
        with pytest.raises(ValidationError, match="duplicate cron declaration id"):
            _config(_declaration("same"), _declaration("same", cron="0 9 * * *"))

    def test_accepts_camel_case_keys(self) -> None:
        config = _config(_declaration(cron=None, everyMinutes=45))
        assert config.cron.declarations[0].every_minutes == 45


class TestBootstrap:
    def test_registers_declared_jobs_as_bound_agent_turns(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        result = bootstrap_declared_cron_jobs(cron, _config(_declaration()))

        assert result.registered == ["daily-brief"]
        assert result.failed == {}
        job = cron.list_jobs(include_disabled=True)[0]
        assert job.id == "declared:daily-brief"
        assert job.enabled is True
        assert is_bound_cron_job(job), "declared jobs must be session-bound to survive"
        assert job.payload.session_key == "telegram:8888207809"
        assert job.payload.origin_channel == "telegram"
        assert job.payload.origin_chat_id == "8888207809"
        assert job.payload.message == "Résume mes emails du jour"
        assert job.state.next_run_at_ms is not None
        assert job.state.last_error is None

    def test_uses_declaration_name_when_provided(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        bootstrap_declared_cron_jobs(cron, _config(_declaration(name="Brief matinal")))
        assert cron.list_jobs(include_disabled=True)[0].name == "Brief matinal"

    def test_is_idempotent_across_restarts(self, tmp_path: Path) -> None:
        config = _config(_declaration())
        first = _service(tmp_path)
        bootstrap_declared_cron_jobs(first, config)

        # A fresh service instance reads the persisted store, like a restart.
        second = _service(tmp_path)
        result = bootstrap_declared_cron_jobs(second, config)

        jobs = second.list_jobs(include_disabled=True)
        assert [job.id for job in jobs] == ["declared:daily-brief"]
        assert result.registered == ["daily-brief"]
        assert all(job.enabled and is_bound_cron_job(job) for job in jobs)

    def test_restores_declarations_after_total_store_loss(self, tmp_path: Path) -> None:
        """The ephemeral-host case: the store file is gone after a redeploy."""
        config = _config(_declaration())
        bootstrap_declared_cron_jobs(_service(tmp_path), config)
        store_path = tmp_path / "cron" / "jobs.json"
        store_path.unlink()

        rebuilt = _service(tmp_path)
        bootstrap_declared_cron_jobs(rebuilt, config)

        assert [job.id for job in rebuilt.list_jobs()] == ["declared:daily-brief"]
        assert store_path.exists()

    def test_prunes_declarations_removed_from_config(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        bootstrap_declared_cron_jobs(
            cron, _config(_declaration("keep"), _declaration("drop", cron="0 9 * * *"))
        )

        result = bootstrap_declared_cron_jobs(cron, _config(_declaration("keep")))

        assert result.pruned == ["declared:drop"]
        assert [job.id for job in cron.list_jobs(include_disabled=True)] == [
            "declared:keep"
        ]

    def test_prunes_disabled_declarations(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        bootstrap_declared_cron_jobs(cron, _config(_declaration("brief")))

        result = bootstrap_declared_cron_jobs(
            cron,
            _config(
                {
                    "id": "brief",
                    "enabled": False,
                    "cron": "0 8 * * *",
                    "message": "Résume mes emails du jour",
                    **_OWNER,
                }
            ),
        )

        assert result.registered == []
        assert result.pruned == ["declared:brief"]
        assert cron.list_jobs(include_disabled=True) == []

    def test_prune_can_be_disabled(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        bootstrap_declared_cron_jobs(cron, _config(_declaration("keep"), _declaration("drop", cron="0 9 * * *")))

        result = bootstrap_declared_cron_jobs(
            cron, _config(_declaration("keep"), prune=False)
        )

        assert result.pruned == []
        assert {job.id for job in cron.list_jobs(include_disabled=True)} == {
            "declared:keep",
            "declared:drop",
        }

    def test_never_prunes_foreign_or_system_jobs(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        cron.register_system_job(
            CronJob(
                id="dream",
                name="dream",
                schedule=CronSchedule(kind="every", every_ms=7_200_000),
                payload=CronPayload(kind="system_event"),
            )
        )

        bootstrap_declared_cron_jobs(cron, _config(_declaration()))

        assert {job.id for job in cron.list_jobs(include_disabled=True)} == {
            "dream",
            "declared:daily-brief",
        }

    def test_disabled_bootstrap_leaves_store_untouched(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        result = bootstrap_declared_cron_jobs(
            cron, _config(_declaration(), enabled=False)
        )

        assert result.enabled is False
        assert result.describe() == "disabled"
        assert cron.list_jobs(include_disabled=True) == []

    def test_no_declarations_is_a_no_op(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        result = bootstrap_declared_cron_jobs(cron, _config())

        assert (result.registered, result.pruned, result.failed) == ([], [], {})
        assert cron.list_jobs(include_disabled=True) == []

    def test_one_failing_declaration_does_not_block_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cron = _service(tmp_path)
        original = cron.register_system_job

        def _explode_on_first(job: CronJob) -> CronJob:
            if job.id == "declared:broken":
                raise RuntimeError("bad schedule")
            return original(job)

        monkeypatch.setattr(cron, "register_system_job", _explode_on_first)

        result = bootstrap_declared_cron_jobs(
            cron, _config(_declaration("broken"), _declaration("healthy", cron="0 9 * * *"))
        )

        assert result.registered == ["healthy"]
        assert "broken" in result.failed
        assert "bad schedule" in result.failed["broken"]
        assert "1 declared" in result.describe() and "1 failed" in result.describe()

    def test_describe_summarises_counts(self, tmp_path: Path) -> None:
        cron = _service(tmp_path)
        bootstrap_declared_cron_jobs(cron, _config(_declaration("a"), _declaration("b", cron="0 9 * * *")))
        result = bootstrap_declared_cron_jobs(cron, _config(_declaration("a")))
        assert result.describe() == "1 declared, 1 pruned"

    def test_registers_without_prune_on_a_minimal_scheduler(self) -> None:
        """A scheduler that only registers jobs must still work (prune is optional)."""

        class _RegisterOnlyScheduler:
            def __init__(self) -> None:
                self.jobs: list[CronJob] = []

            def register_system_job(self, job: CronJob) -> CronJob:
                self.jobs.append(job)
                return job

        scheduler = _RegisterOnlyScheduler()
        result = bootstrap_declared_cron_jobs(scheduler, _config(_declaration()))  # type: ignore[arg-type]

        assert result.registered == ["daily-brief"]
        assert result.pruned == []
        assert [job.id for job in scheduler.jobs] == ["declared:daily-brief"]
