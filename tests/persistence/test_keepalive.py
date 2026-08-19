"""Tests for the free-tier self-ping keepalive."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from nanobot.persistence import (
    KeepaliveError,
    SelfPingKeepalive,
    build_keepalive,
    resolve_public_base_url,
)

_PUBLIC = "https://nanobot-abee.onrender.com"


def _keepalive(handler, **kwargs) -> SelfPingKeepalive:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("base_url", _PUBLIC)
    return SelfPingKeepalive(client=client, **kwargs)


class TestUrlResolution:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (_PUBLIC, _PUBLIC),
            (f"{_PUBLIC}/", _PUBLIC),
            ("nanobot-abee.onrender.com", _PUBLIC),  # bare host → https
            ("http://example.com/base/", "http://example.com/base"),
        ],
    )
    def test_normalises_public_urls(self, raw: str, expected: str) -> None:
        assert resolve_public_base_url(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ("", "requires a public base url"),
            (None, "requires a public base url"),
            ("ftp://example.com", "must be http"),
            # The whole point of the keepalive is platform-visible traffic: a
            # request that never leaves the container cannot produce any.
            ("http://127.0.0.1:8765", "not publicly routable"),
            ("http://localhost:8765", "not publicly routable"),
            ("http://10.0.0.5", "not publicly routable"),
            ("http://[::1]:8765", "not publicly routable"),
        ],
    )
    def test_rejects_urls_that_cannot_keep_a_host_awake(
        self, raw: str | None, match: str
    ) -> None:
        with pytest.raises(KeepaliveError, match=match):
            resolve_public_base_url(raw)

    def test_rejects_non_positive_interval_and_timeout(self) -> None:
        with pytest.raises(KeepaliveError, match="interval must be positive"):
            SelfPingKeepalive(base_url=_PUBLIC, interval_s=0)
        with pytest.raises(KeepaliveError, match="timeout must be positive"):
            SelfPingKeepalive(base_url=_PUBLIC, timeout_s=0)

    def test_builds_the_ping_url_from_base_and_path(self) -> None:
        assert SelfPingKeepalive(base_url=_PUBLIC).url == f"{_PUBLIC}/"
        assert SelfPingKeepalive(base_url=_PUBLIC, path="health").url == f"{_PUBLIC}/health"
        assert SelfPingKeepalive(base_url=f"{_PUBLIC}/", path="/health").url == (
            f"{_PUBLIC}/health"
        )


class TestPing:
    @pytest.mark.asyncio
    async def test_ping_requests_the_public_url(self) -> None:
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ok")

        keepalive = _keepalive(_handler)
        assert await keepalive.ping() == 200
        assert str(seen[0].url) == f"{_PUBLIC}/"
        assert seen[0].headers["user-agent"] == "nanobot-keepalive/1.0"
        assert keepalive.success_count == 1
        assert keepalive.failure_count == 0

    @pytest.mark.asyncio
    async def test_any_status_counts_as_traffic(self) -> None:
        # The platform router wakes the instance before the app answers, so a
        # 404/500 still resets the idle timer — it must not be a failure.
        keepalive = _keepalive(lambda request: httpx.Response(404))
        assert await keepalive.ping() == 404
        assert keepalive.success_count == 1
        assert keepalive.failure_count == 0
        assert keepalive.last_status == 404

    @pytest.mark.asyncio
    async def test_transport_failure_is_counted_not_raised(self) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure", request=request)

        keepalive = _keepalive(_boom)
        assert await keepalive.ping() is None
        assert keepalive.failure_count == 1

    @pytest.mark.asyncio
    async def test_dead_event_loop_error_is_contained(self) -> None:
        def _dead(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("Event loop is closed")

        keepalive = _keepalive(_dead)
        # Same hazard that crashed the gateway through the Supabase mirror: a
        # RuntimeError from a stale pool must never escape a background task.
        assert await keepalive.ping() is None
        assert keepalive.failure_count == 1

    def test_owned_client_is_rebuilt_for_each_event_loop(self) -> None:
        keepalive = SelfPingKeepalive(base_url=_PUBLIC)

        async def _capture() -> int:
            return id(await keepalive._get_client())  # noqa: SLF001 - loop binding is the contract

        first = asyncio.run(_capture())
        second = asyncio.run(_capture())
        assert first != second


class TestRunForever:
    @pytest.mark.asyncio
    async def test_pings_repeatedly_and_survives_failures(self) -> None:
        calls = 0

        def _flaky(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise httpx.ConnectError("transient", request=request)
            return httpx.Response(200)

        keepalive = _keepalive(_flaky, interval_s=0.01)
        task = asyncio.create_task(keepalive.run_forever())
        while calls < 4:
            await asyncio.sleep(0.01)
        assert not task.done(), "a failed ping must not stop the keepalive"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert keepalive.failure_count == 1
        assert keepalive.success_count >= 3

    @pytest.mark.asyncio
    async def test_unexpected_error_does_not_end_the_loop(self) -> None:
        cycles = 0

        async def _boom() -> None:
            nonlocal cycles
            cycles += 1
            raise ValueError("unexpected")

        keepalive = SelfPingKeepalive(base_url=_PUBLIC, interval_s=0.01)
        keepalive.ping = _boom  # type: ignore[method-assign]

        task = asyncio.create_task(keepalive.run_forever())
        while cycles < 3:
            await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class _Cfg:
    """Minimal stand-in for the nested persistence.keepalive config."""

    class _Keepalive:
        def __init__(self, **kw: object) -> None:
            self.enabled = kw.get("enabled", True)
            self.base_url = kw.get("base_url")
            self.path = kw.get("path", "/")
            self.interval_s = kw.get("interval_s", 300)
            self.timeout_s = kw.get("timeout_s", 30.0)

    class _Persistence:
        def __init__(self, keepalive: object) -> None:
            self.keepalive = keepalive

    def __init__(self, **kw: object) -> None:
        self.persistence = _Cfg._Persistence(_Cfg._Keepalive(**kw))


class TestBuildFromConfig:
    def test_disabled_config_builds_nothing(self) -> None:
        assert build_keepalive(_Cfg(enabled=False), environ={}) is None

    def test_falls_back_to_render_external_url(self) -> None:
        keepalive = build_keepalive(
            _Cfg(), environ={"RENDER_EXTERNAL_URL": _PUBLIC}
        )
        assert keepalive is not None
        assert keepalive.url == f"{_PUBLIC}/"
        assert keepalive.interval_s == 300

    def test_explicit_base_url_wins_over_environment(self) -> None:
        keepalive = build_keepalive(
            _Cfg(base_url="https://custom.example.com"),
            environ={"RENDER_EXTERNAL_URL": _PUBLIC},
        )
        assert keepalive is not None
        assert keepalive.url == "https://custom.example.com/"

    def test_unresolved_placeholder_falls_back_to_environment(self) -> None:
        keepalive = build_keepalive(
            _Cfg(base_url="${RENDER_EXTERNAL_URL}"),
            environ={"RENDER_EXTERNAL_URL": _PUBLIC},
        )
        assert keepalive is not None
        assert keepalive.url == f"{_PUBLIC}/"

    def test_missing_url_degrades_to_no_keepalive(self) -> None:
        # A local run has no public URL; that is not an error.
        assert build_keepalive(_Cfg(), environ={}) is None

    def test_unroutable_url_degrades_instead_of_crashing_startup(self) -> None:
        assert build_keepalive(
            _Cfg(), environ={"RENDER_EXTERNAL_URL": "http://127.0.0.1:8765"}
        ) is None
