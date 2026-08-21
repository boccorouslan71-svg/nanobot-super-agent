"""Scheduled jobs must survive a container restart — file *and* schedule.

Two separate failures hide behind "are scheduled tasks saved?":

  1. the store file is lost with the container (covered by the durable mirror,
     and asserted end-to-end here through a real archive round trip), and
  2. the file survives but the schedule is silently recomputed past the runs
     that fell due while the process was down — the daily brief jumps to
     tomorrow, and a one-off reminder gets ``next_run_at_ms = None`` and never
     fires while still looking scheduled in the job list.

The second one leaves no trace at all, which is exactly why it needs tests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nanobot.cron.service import _MISSED_RUN_GRACE_MS, CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.persistence.tree_mirror import TreeArchiveMirror
from tests.persistence.test_tree_mirror import FakeStore

_HOUR_MS = 60 * 60 * 1000
_OWNER = {"session_key": "telegram:8888207809", "origin_channel": "telegram", "origin_chat_id": "8888207809"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _agent_job(
    job_id: str,
    schedule: CronSchedule,
    *,
    due_at: int | None,
    enabled: bool = True,
) -> CronJob:
    job = CronJob(
        id=job_id,
        name=job_id,
        enabled=enabled,
        schedule=schedule,
        payload=CronPayload(kind="agent_turn", message=f"run {job_id}", **_OWNER),
    )
    job.state.next_run_at_ms = due_at
    return job


def _write_store(path: Path, *jobs: CronJob) -> CronService:
    from nanobot.cron.types import CronStore

    service = CronService(path)
    service._store = CronStore(version=1, jobs=list(jobs))
    service._running = True
    service._save_store()
    service._running = False
    service._store = None
    return CronService(path)


async def _start_collecting(service: CronService) -> list[str]:
    """Start the service and return the ids it decided to execute."""
    executed: list[str] = []

    async def on_job(job: CronJob) -> None:
        executed.append(job.id)

    service.on_job = on_job
    await service.start()
    # start() only arms the timer; run the due pass deterministically.
    await service._on_timer()
    service.stop()
    return executed


class TestMissedRunsAreNotSilentlyDropped:
    @pytest.mark.asyncio
    async def test_recurring_run_missed_while_down_is_executed_once(self, tmp_path: Path) -> None:
        due = _now_ms() - 2 * _HOUR_MS
        service = _write_store(
            tmp_path / "cron" / "jobs.json",
            _agent_job("daily-brief", CronSchedule(kind="cron", expr="0 8 * * *"), due_at=due),
        )

        executed = await _start_collecting(service)

        assert executed == ["daily-brief"], "a brief that fell due during the outage must still run"
        job = service.get_job("daily-brief")
        assert job is not None
        assert job.state.last_status == "ok"
        assert job.state.next_run_at_ms is not None
        assert job.state.next_run_at_ms > _now_ms(), "and then move on to its next occurrence"

    @pytest.mark.asyncio
    async def test_one_off_reminder_missed_while_down_still_fires(self, tmp_path: Path) -> None:
        due = _now_ms() - _HOUR_MS
        service = _write_store(
            tmp_path / "cron" / "jobs.json",
            _agent_job("reminder", CronSchedule(kind="at", at_ms=due), due_at=due),
        )

        executed = await _start_collecting(service)

        assert executed == ["reminder"]
        job = service.get_job("reminder")
        assert job is not None
        assert job.state.last_status == "ok"
        assert job.enabled is False, "a one-shot is spent after running"
        assert job.state.next_run_at_ms is None

    @pytest.mark.asyncio
    async def test_a_run_missed_by_days_is_recorded_not_replayed(self, tmp_path: Path) -> None:
        due = _now_ms() - _MISSED_RUN_GRACE_MS - 3 * _HOUR_MS
        service = _write_store(
            tmp_path / "cron" / "jobs.json",
            _agent_job("stale-brief", CronSchedule(kind="cron", expr="0 8 * * *"), due_at=due),
            _agent_job("stale-reminder", CronSchedule(kind="at", at_ms=due), due_at=due),
        )

        executed = await _start_collecting(service)

        assert executed == [], "a run this late is noise, not news"
        brief = service.get_job("stale-brief")
        reminder = service.get_job("stale-reminder")
        assert brief is not None and reminder is not None
        for job in (brief, reminder):
            assert job.state.last_status == "skipped"
            assert "missed scheduled run" in (job.state.last_error or "")
            assert [r.status for r in job.state.run_history] == ["skipped"]
        assert brief.state.next_run_at_ms is not None
        assert brief.state.next_run_at_ms > _now_ms(), "the recurring job keeps running tomorrow"
        assert reminder.enabled is False, "the expired one-shot is closed out, not left dangling"

    @pytest.mark.asyncio
    async def test_internal_heartbeats_are_not_replayed(self, tmp_path: Path) -> None:
        due = _now_ms() - 2 * _HOUR_MS
        heartbeat = CronJob(
            id="heartbeat",
            name="heartbeat",
            enabled=True,
            schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
            payload=CronPayload(kind="system_event", message=""),
        )
        heartbeat.state.next_run_at_ms = due
        service = _write_store(tmp_path / "cron" / "jobs.json", heartbeat)

        executed = await _start_collecting(service)

        assert executed == [], "replaying a missed heartbeat does no work the next tick won't do"
        job = service.get_job("heartbeat")
        assert job is not None and job.state.next_run_at_ms is not None
        assert job.state.next_run_at_ms > _now_ms()

    @pytest.mark.asyncio
    async def test_a_future_run_is_left_alone(self, tmp_path: Path) -> None:
        service = _write_store(
            tmp_path / "cron" / "jobs.json",
            _agent_job(
                "later",
                CronSchedule(kind="cron", expr="0 8 * * *"),
                due_at=_now_ms() + 6 * _HOUR_MS,
            ),
        )

        executed = await _start_collecting(service)

        assert executed == []
        job = service.get_job("later")
        assert job is not None and job.state.last_status is None


class TestDeclaredJobsKeepTheirHistory:
    """A declared job's definition comes from config; its history comes from disk."""

    def _declared(self, *, expr: str = "0 8 * * *") -> CronJob:
        return CronJob(
            id="declared:daily-brief",
            name="daily-brief",
            enabled=True,
            schedule=CronSchedule(kind="cron", expr=expr),
            payload=CronPayload(kind="agent_turn", message="brief", **_OWNER),
        )

    @pytest.mark.asyncio
    async def test_reregistering_preserves_run_history_and_pending_run(
        self, tmp_path: Path
    ) -> None:
        stored = self._declared()
        due = _now_ms() - _HOUR_MS
        stored.state.next_run_at_ms = due
        stored.state.last_run_at_ms = due - 24 * _HOUR_MS
        stored.state.last_status = "ok"
        stored.created_at_ms = 1_700_000_000_000
        service = _write_store(tmp_path / "cron" / "jobs.json", stored)

        service._running = True
        service.register_system_job(self._declared())

        job = service.get_job("declared:daily-brief")
        assert job is not None
        assert job.state.next_run_at_ms == due, "an overdue run must survive re-registration"
        assert job.state.last_run_at_ms == due - 24 * _HOUR_MS
        assert job.state.last_status == "ok"
        assert job.created_at_ms == 1_700_000_000_000, "the job was not created just now"

    @pytest.mark.asyncio
    async def test_a_changed_schedule_reschedules_from_now(self, tmp_path: Path) -> None:
        stored = self._declared()
        stored.state.next_run_at_ms = _now_ms() - _HOUR_MS
        service = _write_store(tmp_path / "cron" / "jobs.json", stored)

        service._running = True
        service.register_system_job(self._declared(expr="0 20 * * *"))

        job = service.get_job("declared:daily-brief")
        assert job is not None and job.state.next_run_at_ms is not None
        assert job.state.next_run_at_ms > _now_ms(), "the old schedule's pending run is meaningless"

    @pytest.mark.asyncio
    async def test_declared_brief_missed_during_a_restart_still_runs(self, tmp_path: Path) -> None:
        """Bootstrap then start — the real startup order for a declared job."""
        stored = self._declared()
        stored.state.next_run_at_ms = _now_ms() - 2 * _HOUR_MS
        service = _write_store(tmp_path / "cron" / "jobs.json", stored)

        service._running = True
        service.register_system_job(self._declared())  # config bootstrap
        service._running = False

        executed = await _start_collecting(service)  # then the service starts

        assert executed == ["declared:daily-brief"]

    @pytest.mark.asyncio
    async def test_agent_created_job_survives_a_full_archive_round_trip(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end shape of a restart: mirror the tree, lose the disk, restore."""
        live = tmp_path / "live"
        store_path = live / "workspace" / "cron" / "jobs.json"
        due = _now_ms() + 6 * _HOUR_MS
        _write_store(
            store_path,
            _agent_job("ad-hoc-42", CronSchedule(kind="cron", expr="30 19 * * 5"), due_at=due),
        )

        store = FakeStore()
        await TreeArchiveMirror(store=store, root=live).snapshot(force=True)

        recycled = tmp_path / "recycled"  # a brand-new container: empty disk
        restored = await TreeArchiveMirror(store=store, root=recycled).restore()
        assert "workspace/cron/jobs.json" in restored

        restored_path = recycled / "workspace" / "cron" / "jobs.json"
        assert restored_path.is_file(), "the schedule file must come back with the container"
        stored = json.loads(restored_path.read_text())
        assert [j["id"] for j in stored["jobs"]] == ["ad-hoc-42"]

        service = CronService(restored_path)
        await service.start()
        service.stop()
        job = service.get_job("ad-hoc-42")
        assert job is not None
        assert job.payload.message == "run ad-hoc-42"
        assert job.schedule.expr == "30 19 * * 5", "with its schedule, not a default"
        assert job.enabled is True
