"""Self-ping keepalive that stops free-tier hosts from sleeping.

Render's free web services suspend an instance after ~15 minutes without
*inbound* HTTP traffic. A suspended instance answers nothing: the Telegram
channel stops polling and every cron job silently misses its window until the
next request wakes it (cold start ~30-50s).

The container cannot vote itself awake from the inside — a loopback request to
127.0.0.1 never reaches the platform router. What does count is a request to the
service's own *public* URL, which leaves the instance, hits Render's edge and
comes back as ordinary inbound traffic. Render publishes that URL to the
container as ``RENDER_EXTERNAL_URL``, so the keepalive is zero-config there.

Design rules (same contract as the Supabase mirror in this package):

* The keepalive is a convenience, never a dependency: no failure here may reach
  the gateway's task group. Every cycle is logged and the loop keeps running.
* Unset/unresolved configuration degrades to "no keepalive" instead of raising,
  so a local run and a non-Render host stay unaffected.
* A loopback or private-host URL is refused loudly at construction: it would
  spin forever while the platform still counts the instance as idle.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from loguru import logger

__all__ = [
    "KeepaliveError",
    "SelfPingKeepalive",
    "resolve_public_base_url",
]

_LOOPBACK_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class KeepaliveError(RuntimeError):
    """Raised when the keepalive cannot be configured."""


def _is_non_public_host(host: str) -> bool:
    """Return True for hosts that can never produce platform-visible traffic."""
    candidate = host.strip().strip("[]").lower()
    if not candidate or candidate in _LOOPBACK_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False  # A DNS name; only the platform can resolve it.
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def resolve_public_base_url(raw: str | None) -> str:
    """Normalise a public base URL, or raise :class:`KeepaliveError`.

    Accepts a bare host (``nanobot-abee.onrender.com``) as well as a full URL,
    because platforms are inconsistent about which one they inject.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise KeepaliveError("keepalive requires a public base url")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"}:
        raise KeepaliveError(f"keepalive url must be http(s), got '{parts.scheme}'")
    if not parts.hostname:
        raise KeepaliveError(f"keepalive url has no host: '{raw}'")
    if _is_non_public_host(parts.hostname):
        raise KeepaliveError(
            f"keepalive url '{parts.hostname}' is not publicly routable; "
            "a loopback/private request does not count as inbound traffic"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class SelfPingKeepalive:
    """Periodically request the service's own public URL to defeat idle sleep."""

    def __init__(
        self,
        *,
        base_url: str,
        path: str = "/",
        interval_s: float = 300.0,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if interval_s <= 0:
            raise KeepaliveError("keepalive interval must be positive")
        if timeout_s <= 0:
            raise KeepaliveError("keepalive timeout must be positive")

        self._base_url = resolve_public_base_url(base_url)
        self._path = "/" + (path or "/").strip().lstrip("/")
        self._interval_s = float(interval_s)
        self._timeout_s = float(timeout_s)
        self._client = client
        self._owns_client = client is None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self.success_count = 0
        self.failure_count = 0
        self.last_status: int | None = None

    @property
    def url(self) -> str:
        """Return the exact URL each cycle requests."""
        return f"{self._base_url}{self._path}"

    @property
    def interval_s(self) -> float:
        return self._interval_s

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a client bound to the running loop, rebuilding across loops.

        Same hazard as the Supabase store: a pool created on a loop that later
        closes raises ``RuntimeError: Event loop is closed`` from inside
        httpcore, which is not an ``httpx.HTTPError``.
        """
        if self._owns_client:
            loop = asyncio.get_running_loop()
            if self._client is not None and self._client_loop is not loop:
                await self._reset_client()
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout_s,
                    follow_redirects=True,
                )
            self._client_loop = loop
        elif self._client is None:  # pragma: no cover - defensive
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_client = True
        return self._client

    async def _reset_client(self) -> None:
        if self._client is None or not self._owns_client:
            return
        stale, self._client, self._client_loop = self._client, None, None
        try:
            await stale.aclose()
        except Exception as exc:  # pragma: no cover - stale-loop teardown
            logger.debug("Keepalive: discarded unusable HTTP client ({})", exc)

    async def aclose(self) -> None:
        """Close the HTTP client when this keepalive owns it."""
        await self._reset_client()

    async def ping(self) -> int | None:
        """Request the public URL once; return the status code, or None on error.

        Any status counts as a wake-up: the platform router sees the request
        before the application decides what to answer, so even a 404 keeps the
        instance alive. Only an unreachable service is a real failure.
        """
        try:
            client = await self._get_client()
            response = await client.get(
                self.url,
                headers={"User-Agent": "nanobot-keepalive/1.0"},
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            self.failure_count += 1
            await self._reset_client()
            logger.warning("Keepalive: ping {} failed: {}", self.url, exc)
            return None

        self.success_count += 1
        self.last_status = response.status_code
        logger.debug("Keepalive: ping {} → HTTP {}", self.url, response.status_code)
        return response.status_code

    async def run_forever(self) -> None:
        """Ping on a fixed cadence until cancelled; never propagate a failure."""
        logger.info(
            "Keepalive: pinging {} every {:.0f}s to prevent idle sleep",
            self.url,
            self._interval_s,
        )
        while True:
            try:
                await asyncio.sleep(self._interval_s)
                await self.ping()
            except asyncio.CancelledError:
                await self._reset_client()
                raise
            except Exception as exc:
                # Last line of defence: keeping the instance awake must never be
                # able to take the agent, its channels or its crons down.
                self.failure_count += 1
                logger.error("Keepalive: unexpected error, continuing: {}", exc)


def build_keepalive(config: Any, *, environ: dict[str, str]) -> SelfPingKeepalive | None:
    """Build the keepalive from config + environment, or None when unavailable.

    ``base_url`` is optional in config precisely so the common case needs no
    configuration: Render injects ``RENDER_EXTERNAL_URL`` at runtime.
    """
    cfg = getattr(getattr(config, "persistence", None), "keepalive", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return None

    configured = (getattr(cfg, "base_url", None) or "").strip()
    if configured.startswith("${") or not configured:
        configured = ""
    base_url = configured or environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not base_url:
        logger.info(
            "Keepalive: enabled but no public url (set persistence.keepalive.baseUrl "
            "or RENDER_EXTERNAL_URL); running without it"
        )
        return None

    try:
        return SelfPingKeepalive(
            base_url=base_url,
            path=getattr(cfg, "path", "/"),
            interval_s=getattr(cfg, "interval_s", 300),
            timeout_s=getattr(cfg, "timeout_s", 30.0),
        )
    except KeepaliveError as exc:
        logger.warning("Keepalive: disabled ({})", exc)
        return None
