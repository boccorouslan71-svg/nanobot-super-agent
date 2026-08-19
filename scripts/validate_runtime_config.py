"""Validate the committed runtime config: placeholder expansion, provider
fallback chain, Telegram owner allowlist, and Composio wiring."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGNES_API_KEY", "sk-agnes-test")
os.environ.setdefault("GEMINI_API_KEY", "gemini-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "anthropic-test")
os.environ.setdefault("COMPOSIO_API_KEY", "ak_test")
os.environ.setdefault("COMPOSIO_USER_ID", "nanobot-owner")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "111:AAA-token")
os.environ.setdefault("TELEGRAM_OWNER_ID", "8888207809")
os.environ.setdefault("NANOBOT_WEB_TOKEN", "web-secret")
os.environ.setdefault("SUPABASE_URL", "https://validate.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "sb_secret_validate")

from nanobot.config.loader import load_config, resolve_config_env_vars  # noqa: E402
from nanobot.providers.factory import _resolve_fallback_presets  # noqa: E402

repo = Path(__file__).resolve().parents[1]
template = repo / "render-config.json"

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "config.json"
    path.write_text(template.read_text())
    # load_config parses; the gateway resolves ${VAR} refs as a separate step,
    # so the template must survive both stages exactly as production runs them.
    config = resolve_config_env_vars(load_config(path), config_path=path)

print("--- placeholder expansion ---")
check("providers.agnes.api_key", config.providers.agnes.api_key, "sk-agnes-test")
check("providers.agnes.api_base", config.providers.agnes.api_base, "https://apihub.agnes-ai.com/v1")
check("providers.gemini.api_key", config.providers.gemini.api_key, "gemini-test")
check("tools.composio.api_key", config.tools.composio.api_key, "ak_test")
check("tools.composio.user_id", config.tools.composio.user_id, "nanobot-owner")

print("\n--- telegram owner restriction (expansion inside a list) ---")
telegram = config.channels.model_extra.get("telegram") if config.channels.model_extra else None
telegram = telegram if telegram is not None else getattr(config.channels, "telegram", None)
tg = telegram if isinstance(telegram, dict) else (telegram.model_dump() if telegram else {})
check("telegram.enabled", tg.get("enabled"), True)
check("telegram.token", tg.get("token"), "111:AAA-token")
check("telegram.allow_from", tg.get("allow_from", tg.get("allowFrom")), ["8888207809"])
check("telegram.mode", tg.get("mode"), "polling")

print("\n--- primary model + native fallback chain ---")
defaults = config.agents.defaults
check("primary model", defaults.model, "agnes-2.5-flash")
check("primary provider", defaults.provider, "agnes")
primary_preset = type(config).__module__ and None
from nanobot.config.schema import ModelPresetConfig  # noqa: E402

primary = ModelPresetConfig(
    model=defaults.model, provider=defaults.provider, max_tokens=defaults.max_tokens
)
chain = _resolve_fallback_presets(config, primary)
resolved = [f"{p.provider}/{p.model}" for p in chain]
check("fallback chain", resolved, ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"])

print("\n--- telegram allowlist semantics (owner-only) ---")
# Exercised through the shared channel authorization path (BaseChannel), so the
# check does not require the optional python-telegram-bot dependency.
from nanobot.channels.base import BaseChannel  # noqa: E402


class _AuthProbe(BaseChannel):
    name = "telegram"

    def __init__(self, config: dict[str, object]) -> None:
        self._probe_config = config

    @property
    def config(self) -> dict[str, object]:  # type: ignore[override]
        return self._probe_config

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: object) -> None: ...


probe = _AuthProbe(tg)
check("owner is allowed", probe.is_allowed("8888207809"), True)
check("stranger is denied", probe.is_allowed("123456789"), False)

print("\n--- cron declarations (version-controlled bootstrap) ---")
from nanobot.cron.bootstrap import bootstrap_declared_cron_jobs  # noqa: E402
from nanobot.cron.service import CronService  # noqa: E402
from nanobot.cron.session_turns import is_bound_cron_job  # noqa: E402

check("cron bootstrap enabled", config.cron.enabled, True)
declared = {d.id: d for d in config.cron.declarations}
check("declaration ids", sorted(declared), ["composio-connection-health", "daily-brief"])
check("daily-brief schedule", declared["daily-brief"].describe_schedule(), "cron 0 8 * * *")
check("daily-brief timezone", declared["daily-brief"].build_schedule().tz, "Africa/Porto-Novo")
check("daily-brief target expanded", declared["daily-brief"].to, "8888207809")
check("daily-brief session key", declared["daily-brief"].session_key, "telegram:8888207809")

with tempfile.TemporaryDirectory() as tmp:
    store_path = Path(tmp) / "cron" / "jobs.json"
    cron_service = CronService(store_path)
    bootstrap = bootstrap_declared_cron_jobs(cron_service, config)
    jobs = cron_service.list_jobs(include_disabled=True)
    check("bootstrap failures", bootstrap.failed, {})
    check(
        "declared jobs registered",
        sorted(job.id for job in jobs),
        ["declared:composio-connection-health", "declared:daily-brief"],
    )
    check("declared jobs enabled", all(job.enabled for job in jobs), True)
    check("declared jobs session-bound", all(is_bound_cron_job(job) for job in jobs), True)
    check("declared jobs scheduled", all(job.state.next_run_at_ms for job in jobs), True)

    # Simulate the ephemeral host: the whole cron store disappears on redeploy.
    store_path.unlink()
    rebuilt = CronService(store_path)
    bootstrap_declared_cron_jobs(rebuilt, config)
    check("declarations survive store loss", len(rebuilt.list_jobs()), 2)

    # Re-applying the same config must not duplicate or disable anything.
    again = bootstrap_declared_cron_jobs(rebuilt, config)
    check("re-apply is idempotent", len(rebuilt.list_jobs()), 2)
    check("re-apply prunes nothing", again.pruned, [])

print("\n--- supabase state mirror ---")
supabase = config.persistence.supabase
check("mirror enabled", supabase.enabled, True)
check("mirror url expanded", supabase.url, "https://validate.supabase.co")
check("mirror key expanded", supabase.service_key, "sb_secret_validate")
check("mirror table", supabase.table, "nanobot_state_blobs")
check("mirror paths", supabase.paths, ["cron/jobs.json"])
check("mirror restores on start", supabase.restore_on_start, True)

print("\n--- free-tier keepalive (anti-sleep self ping) ---")
from nanobot.persistence import build_keepalive  # noqa: E402

keepalive_cfg = config.persistence.keepalive
check("keepalive enabled", keepalive_cfg.enabled, True)
check("keepalive interval", keepalive_cfg.interval_s, 300)
check("keepalive path", keepalive_cfg.path, "/")
# baseUrl is intentionally unset in the template: Render injects the real public
# URL at runtime, so the ping follows the service instead of a hardcoded host.
check("keepalive base url unset in template", keepalive_cfg.base_url, None)

built = build_keepalive(
    config, environ={"RENDER_EXTERNAL_URL": "https://nanobot-abee.onrender.com"}
)
check("keepalive built from platform url", built is not None, True)
if built is not None:
    check("keepalive ping url", built.url, "https://nanobot-abee.onrender.com/")
    check("keepalive interval under render's 15min idle window", built.interval_s < 900, True)
# A host that injects nothing must degrade to "no keepalive", never crash boot.
check("keepalive degrades without a public url", build_keepalive(config, environ={}), None)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
