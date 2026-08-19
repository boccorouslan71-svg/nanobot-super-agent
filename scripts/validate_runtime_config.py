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

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
