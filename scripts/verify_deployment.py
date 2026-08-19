"""Live end-to-end verification of the deployed super-agent.

Complements the offline suites: ``pytest`` proves the code is correct and
``validate_runtime_config.py`` proves the committed config resolves, while this
script proves the *running deployment* actually works — every external
dependency answered, the durable state is real, and the anti-sleep keepalive is
doing its job.

Everything is read-only. Credentials come from the environment only (the same
variable names Render injects); nothing is written to the repo, and no secret is
ever printed.

Usage:  python scripts/verify_deployment.py [--service-url URL]
Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

import httpx

_DEFAULT_SERVICE_URL = "https://nanobot-abee.onrender.com"
_TIMEOUT = httpx.Timeout(60.0, connect=30.0)

_GREEN, _RED, _DIM, _RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Report:
    """Collects check outcomes so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        mark = f"{_GREEN}PASS{_RESET}" if ok else f"{_RED}FAIL{_RESET}"
        print(f"  {mark}  {label}" + (f" {_DIM}— {detail}{_RESET}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{label}: {detail or 'check returned false'}")
        return ok

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def guard(self, label: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        """Run a check, turning an unexpected exception into a FAIL, not a crash."""
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check
            return self.check(label, False, f"{type(exc).__name__}: {exc}")
        return self.check(label, ok, detail)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _require(report: Report, names: list[str]) -> bool:
    missing = [n for n in names if not _env(n)]
    if missing:
        report.check(f"credentials present ({', '.join(names)})", False, f"missing: {missing}")
        return False
    return True


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_service(report: Report, service_url: str) -> None:
    report.section("deployed service (public endpoint)")

    def _root() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{service_url}/")
        return response.status_code == 200, f"HTTP {response.status_code}"

    report.guard("service answers on its public url", _root)

    def _cold_start_budget() -> tuple[bool, str]:
        # A warm instance answers fast. A multi-second response means the
        # instance had gone to sleep and cold-started, i.e. the keepalive is
        # not holding it open.
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{service_url}/")
        elapsed = response.elapsed.total_seconds()
        return elapsed < 5.0, f"{elapsed:.2f}s (warm instance expected)"

    report.guard("instance is warm, not cold-starting", _cold_start_budget)


def check_telegram(report: Report) -> None:
    report.section("telegram channel")
    if not _require(report, ["TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"]):
        return
    token = _env("TELEGRAM_BOT_TOKEN")

    def _get_me() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(f"https://api.telegram.org/bot{token}/getMe")
        payload = response.json()
        bot = (payload.get("result") or {}).get("username")
        return bool(payload.get("ok")) and bool(bot), f"@{bot}"

    report.guard("bot token valid", _get_me)

    def _webhook_clear() -> tuple[bool, str]:
        # The channel runs in polling mode; a webhook would silently swallow
        # every update before the gateway could poll it.
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        url = (response.json().get("result") or {}).get("url") or ""
        return url == "", "no webhook set (polling mode)" if not url else f"webhook: {url}"

    report.guard("polling not shadowed by a webhook", _webhook_clear)


def check_supabase(report: Report) -> None:
    report.section("supabase durable state")
    if not _require(report, ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]):
        return
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_KEY")
    table = os.environ.get("SUPABASE_TABLE", "nanobot_state_blobs")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    state: dict[str, Any] = {}

    def _row_exists() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(
                f"{url}/rest/v1/{table}",
                headers=headers,
                params={"select": "key,content", "limit": "20"},
            )
        rows = response.json() if response.status_code < 400 else []
        for row in rows:
            state[row.get("key", "")] = row.get("content")
        keys = sorted(state)
        return any("cron/jobs.json" in k for k in keys), f"rows: {keys or 'none'}"

    report.guard("mirrored cron state present", _row_exists)

    def _jobs_mirrored() -> tuple[bool, str]:
        content = next(
            (v for k, v in state.items() if "cron/jobs.json" in k and isinstance(v, (dict, list))),
            None,
        )
        if content is None:
            return False, "no mirrored cron payload to inspect"
        blob = json.dumps(content)
        declared = [d for d in ("declared:daily-brief", "declared:composio-connection-health")
                    if d in blob]
        return len(declared) == 2, f"declared jobs mirrored: {declared}"

    report.guard("both declared cron jobs survive in durable state", _jobs_mirrored)

    def _rls_blocks_anon() -> tuple[bool, str]:
        # The mirror holds runtime state; an anonymous reader must get nothing.
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(f"{url}/rest/v1/{table}", params={"select": "key"})
        return response.status_code in (401, 403, 404), f"anon read → HTTP {response.status_code}"

    report.guard("table not readable without the service key", _rls_blocks_anon)


def check_composio(report: Report) -> None:
    report.section("composio tool catalogue")
    if not _require(report, ["COMPOSIO_API_KEY"]):
        return
    headers = {"x-api-key": _env("COMPOSIO_API_KEY")}

    def _toolkits() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(
                "https://backend.composio.dev/api/v3/toolkits",
                headers=headers,
                params={"limit": 1},
            )
        if response.status_code == 401:
            # Be explicit: an expired/rotated key looks identical to a broken
            # integration in the logs, and only one of them is fixable here.
            return False, "HTTP 401 — COMPOSIO_API_KEY rejected (rotate the key in Render)"
        return response.status_code == 200, f"HTTP {response.status_code}"

    report.guard("api key accepted by the catalogue", _toolkits)

    def _tools_searchable() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(
                "https://backend.composio.dev/api/v3/tools",
                headers=headers,
                params={"limit": 5},
            )
        items = (response.json() or {}).get("items") or []
        return response.status_code == 200 and bool(items), f"{len(items)} tool(s) returned"

    report.guard("tool search returns executable tools", _tools_searchable)

    def _connected_accounts() -> tuple[bool, str]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(
                "https://backend.composio.dev/api/v3/connected_accounts",
                headers=headers,
                params={"limit": 50},
            )
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        items = (response.json() or {}).get("items") or []
        # Zero connected accounts is a valid state (nothing linked yet); the
        # check is that the endpoint the bridge health job calls actually works.
        active = [i for i in items if str(i.get("status", "")).upper() == "ACTIVE"]
        return True, f"{len(items)} account(s), {len(active)} active"

    report.guard("connection-health endpoint reachable", _connected_accounts)


def check_providers(report: Report) -> None:
    report.section("model providers (primary + fallback chain)")

    def _agnes() -> tuple[bool, str]:
        key = _env("AGNES_API_KEY")
        if not key:
            return False, "AGNES_API_KEY missing"
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(
                "https://apihub.agnes-ai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "agnes-2.5-flash",
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    # Generous on purpose: this is a reasoning model, and a small
                    # budget is spent entirely on reasoning_content, leaving the
                    # visible answer empty for reasons that have nothing to do
                    # with provider health.
                    "max_tokens": 256,
                },
            )
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        choices = (response.json() or {}).get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        answer = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
        # Either channel proves the model generated tokens.
        return bool(answer or reasoning), f"answered {(answer or reasoning)[:24]!r}"

    report.guard("primary model responds (agnes-2.5-flash)", _agnes)

    # Must stay in sync with agents.defaults.fallbackModels in render-config.json.
    for model in ("gemini-3.6-flash", "gemini-3.5-flash"):
        def _gemini(model: str = model) -> tuple[bool, str]:
            key = _env("GEMINI_API_KEY")
            if not key:
                return False, "GEMINI_API_KEY missing"
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": "Reply with the single word: ok"}]}]},
                )
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            candidates = (response.json() or {}).get("candidates") or []
            return bool(candidates), f"{len(candidates)} candidate(s)"

        report.guard(f"fallback model responds ({model})", _gemini)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=os.environ.get("SERVICE_URL", _DEFAULT_SERVICE_URL))
    args = parser.parse_args()
    service_url = args.service_url.rstrip("/")

    print(f"Verifying deployment at {service_url}")
    report = Report()
    check_service(report, service_url)
    check_telegram(report)
    check_supabase(report)
    check_composio(report)
    check_providers(report)

    print()
    if report.failures:
        print(f"{_RED}{len(report.failures)} FAILURE(S){_RESET} ({report.passed} passed):")
        for failure in report.failures:
            print(" -", failure)
        return 1
    print(f"{_GREEN}ALL {report.passed} LIVE CHECKS PASSED{_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
