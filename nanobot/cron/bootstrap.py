"""Re-declare cron jobs from version-controlled configuration at startup.

Nanobot's cron store lives on disk. On hosts without a persistent disk the
store is empty after every deploy, so any schedule the user cares about must be
re-created deterministically. Declarations in ``config.cron.declarations`` are
applied here: each one owns a stable job id, so applying them repeatedly is
idempotent, and a declaration removed from the config is pruned from the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from nanobot.config.schema import CronDeclarationConfig
from nanobot.cron.types import CronJob, CronPayload

if TYPE_CHECKING:
    from nanobot.config.schema import Config

__all__ = ["CronBootstrapResult", "bootstrap_declared_cron_jobs"]


class _CronServiceLike(Protocol):
    """The subset of :class:`~nanobot.cron.service.CronService` used here."""

    def register_system_job(self, job: CronJob) -> CronJob: ...

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]: ...

    def remove_job(self, job_id: str) -> str: ...


@dataclass
class CronBootstrapResult:
    """Outcome of a bootstrap pass, for startup output and tests."""

    enabled: bool = True
    registered: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        """Return a one-line human-readable summary."""
        if not self.enabled:
            return "disabled"
        parts = [f"{len(self.registered)} declared"]
        if self.pruned:
            parts.append(f"{len(self.pruned)} pruned")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


def bootstrap_declared_cron_jobs(
    cron: _CronServiceLike,
    config: "Config",
) -> CronBootstrapResult:
    """Apply the configured cron declarations to the cron store.

    Registration failures are collected per declaration instead of aborting the
    pass, so one malformed schedule cannot silently drop every other job.
    """
    bootstrap_cfg = config.cron
    if not bootstrap_cfg.enabled:
        logger.info("Cron bootstrap: disabled by configuration")
        return CronBootstrapResult(enabled=False)

    result = CronBootstrapResult()
    timezone = config.agents.defaults.timezone
    wanted_ids: set[str] = set()

    for declaration in bootstrap_cfg.declarations:
        wanted_ids.add(declaration.job_id)
        if not declaration.enabled:
            logger.info("Cron bootstrap: '{}' is disabled; leaving it unregistered", declaration.id)
            continue
        try:
            cron.register_system_job(
                CronJob(
                    id=declaration.job_id,
                    name=declaration.name or declaration.id,
                    enabled=True,
                    schedule=declaration.build_schedule(timezone),
                    payload=CronPayload(
                        kind="agent_turn",
                        message=declaration.message,
                        # Declared jobs are created session-bound on purpose: the
                        # cron service disables agent jobs that carry only the
                        # legacy channel/to delivery fields.
                        session_key=declaration.session_key,
                        origin_channel=declaration.channel,
                        origin_chat_id=declaration.to,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad declaration must not hide the rest
            result.failed[declaration.id] = str(exc)
            logger.error("Cron bootstrap: failed to register '{}': {}", declaration.id, exc)
            continue

        result.registered.append(declaration.id)
        logger.info(
            "Cron bootstrap: registered '{}' ({})",
            declaration.id,
            declaration.describe_schedule(),
        )

    if bootstrap_cfg.prune_removed:
        lister = getattr(cron, "list_jobs", None)
        remover = getattr(cron, "remove_job", None)
        if not callable(lister) or not callable(remover):
            # Embedders may pass a minimal scheduler that only registers jobs.
            # Say so instead of pretending the prune pass ran.
            logger.warning(
                "Cron bootstrap: scheduler cannot enumerate or remove jobs; prune skipped"
            )
            return result
        for job in lister(include_disabled=True):
            if not CronDeclarationConfig.is_declared_job_id(job.id):
                continue
            if job.id in wanted_ids and _is_enabled_declaration(bootstrap_cfg, job.id):
                continue
            outcome = remover(job.id)
            if outcome == "removed":
                result.pruned.append(job.id)
                logger.info("Cron bootstrap: pruned '{}' (no longer declared)", job.id)
            else:
                logger.warning("Cron bootstrap: could not prune '{}' ({})", job.id, outcome)

    return result


def _is_enabled_declaration(bootstrap_cfg: object, job_id: str) -> bool:
    """Return True when ``job_id`` maps to an enabled declaration."""
    declarations = getattr(bootstrap_cfg, "declarations", [])
    return any(d.job_id == job_id and d.enabled for d in declarations)
