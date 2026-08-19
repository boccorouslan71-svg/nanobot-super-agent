"""MCP tool retention, bounded recovery, and session keepalive.

Regression cover for the production symptom: a Make MCP session dies, the
lazy reconnect fails, and the dynamically registered tools vanish from the
registry until the process restarts (the web card still shows the server as
configured, so the failure is invisible).

The contract asserted here:

1. A failed reconnect leaves the existing tool registrations untouched.
2. A dead server is retried in the background with bounded backoff, and a
   successful retry swaps the new tool set in.
3. Recovery gives up after a bounded number of attempts without ever
   unregistering tools.
4. An idle session is pinged, and a failed ping triggers recovery instead of
   waiting for the user's next tool call to discover the dead session.

These tests drive ``MCPProvider`` with a stubbed ``connect_mcp_servers``, so
they are deterministic and touch no network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.agent.tools import mcp as mcp_module
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig

_SERVER = "repro"
_TOOL = "mcp_repro_greet"


class _FakeConnection:
    """Stand-in for an MCP connection handle."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeTool(Tool):
    """Minimal registry-visible tool using the mcp_<server>_ name convention."""

    def __init__(self, name: str = _TOOL) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake mcp tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _FakeWrapper(mcp_module._MCPWrapperBase, _FakeTool):
    """Fake tool that also carries a session, like the real MCP wrappers."""

    def __init__(self, session: Any, name: str = _TOOL) -> None:
        _FakeTool.__init__(self, name)
        self._set_mcp_connection(session, _SERVER)


class _FailingSession:
    def __init__(self) -> None:
        self.pings = 0

    async def send_ping(self) -> None:
        self.pings += 1
        raise RuntimeError("session terminated")


def _server_config() -> MCPServerConfig:
    return MCPServerConfig(
        type="streamableHttp",
        url="https://mcp.example.com/mcp",
        enabled_tools=["*"],
    )


def _make_provider(tool: Tool) -> tuple[mcp_module.MCPProvider, ToolRegistry, _FakeConnection]:
    registry = ToolRegistry()
    registry.register(tool)
    provider = mcp_module.MCPProvider({_SERVER: _server_config()}, registry)
    connection = _FakeConnection()
    provider._connections[_SERVER] = connection
    provider._set_runtime_status({_SERVER}, "connected")
    return provider, registry, connection


def _freeze_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push the retry delay out of the way so recovery cannot interfere."""
    monkeypatch.setattr(mcp_module, "_MCP_RECOVERY_MIN_DELAY_S", 600.0)
    monkeypatch.setattr(mcp_module, "_MCP_RECOVERY_MAX_DELAY_S", 600.0)


def _fast_recovery(monkeypatch: pytest.MonkeyPatch, *, attempts: int = 3) -> None:
    monkeypatch.setattr(mcp_module, "_MCP_RECOVERY_MIN_DELAY_S", 0.01)
    monkeypatch.setattr(mcp_module, "_MCP_RECOVERY_MAX_DELAY_S", 0.02)
    monkeypatch.setattr(mcp_module, "_MCP_RECOVERY_MAX_ATTEMPTS", attempts)


def _no_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_module, "_MCP_PING_INTERVAL_S", 0.0)


async def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_failed_reconnect_keeps_tools_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core regression: a failed reconnect must not strip the tools."""
    _freeze_recovery(monkeypatch)
    _no_keepalive(monkeypatch)
    tool = _FakeTool()
    provider, registry, connection = _make_provider(tool)

    attempts = 0

    async def failing_connect(servers: Any, registry_arg: ToolRegistry, **kwargs: Any) -> dict:
        nonlocal attempts
        attempts += 1
        # A real failure registers nothing and returns no handle.
        return {}

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", failing_connect)

    refreshed = await provider._refresh_terminated_server(_SERVER, _TOOL, tool)

    assert refreshed is None
    assert attempts == 1
    # The whole point: the tool object is still callable by the agent.
    assert registry.get(_TOOL) is tool
    assert _TOOL in registry.tool_names
    assert provider.runtime_status()[_SERVER] == "failed"
    # Dead handle dropped, and a bounded retry armed.
    assert _SERVER not in provider.connected_server_names
    assert connection.closed is True  # dead handle released, tools untouched
    assert _SERVER in provider._recovery_tasks

    await provider.aclose()


async def test_successful_reconnect_swaps_in_new_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proven replacement session replaces the stale registrations."""
    _freeze_recovery(monkeypatch)
    _no_keepalive(monkeypatch)
    stale = _FakeTool()
    provider, registry, connection = _make_provider(stale)
    # A tool the new session no longer exposes; it must not survive the swap.
    registry.register(_FakeTool("mcp_repro_retired"))

    fresh = _FakeTool()
    new_connection = _FakeConnection()

    async def succeeding_connect(
        servers: Any, registry_arg: ToolRegistry, **kwargs: Any
    ) -> dict:
        registry_arg.register(fresh)
        return {_SERVER: new_connection}

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", succeeding_connect)

    refreshed = await provider._refresh_terminated_server(_SERVER, _TOOL, stale)

    assert refreshed is fresh
    assert registry.get(_TOOL) is fresh
    assert "mcp_repro_retired" not in registry.tool_names
    assert provider.connected_server_names == {_SERVER}
    assert provider.runtime_status()[_SERVER] == "connected"
    assert connection.closed is True  # stale session closed only after success

    await provider.aclose()


async def test_bounded_recovery_readopts_tools_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background recovery retries with backoff and adopts the new tool set."""
    _fast_recovery(monkeypatch, attempts=5)
    _no_keepalive(monkeypatch)
    tool = _FakeTool()
    provider, registry, _ = _make_provider(tool)
    provider._connections.pop(_SERVER)

    fresh = _FakeTool()
    calls = 0

    async def flaky_connect(servers: Any, registry_arg: ToolRegistry, **kwargs: Any) -> dict:
        nonlocal calls
        calls += 1
        if calls < 3:
            return {}
        registry_arg.register(fresh)
        return {_SERVER: _FakeConnection()}

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", flaky_connect)

    provider._schedule_recovery(_SERVER)
    task = provider._recovery_tasks[_SERVER]
    await asyncio.wait_for(task, timeout=10)

    assert calls == 3
    assert registry.get(_TOOL) is fresh
    assert provider.connected_server_names == {_SERVER}
    assert provider.runtime_status()[_SERVER] == "connected"

    await provider.aclose()


async def test_recovery_is_bounded_and_never_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanently dead server stops retrying but keeps its tools."""
    _fast_recovery(monkeypatch, attempts=2)
    _no_keepalive(monkeypatch)
    tool = _FakeTool()
    provider, registry, _ = _make_provider(tool)
    provider._connections.pop(_SERVER)

    calls = 0

    async def always_failing_connect(
        servers: Any, registry_arg: ToolRegistry, **kwargs: Any
    ) -> dict:
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", always_failing_connect)

    await provider._recover_server(_SERVER)

    assert calls == 2  # bounded by _MCP_RECOVERY_MAX_ATTEMPTS
    assert registry.get(_TOOL) is tool
    assert provider.runtime_status()[_SERVER] == "failed"

    await provider.aclose()


async def test_recovery_survives_raising_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that raises must not kill the retry loop."""
    _fast_recovery(monkeypatch, attempts=2)
    _no_keepalive(monkeypatch)
    tool = _FakeTool()
    provider, registry, _ = _make_provider(tool)
    provider._connections.pop(_SERVER)

    calls = 0

    async def raising_connect(servers: Any, registry_arg: ToolRegistry, **kwargs: Any) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("oauth token revoked")

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", raising_connect)

    await provider._recover_server(_SERVER)

    assert calls == 2
    assert registry.get(_TOOL) is tool

    await provider.aclose()


async def test_keepalive_ping_failure_triggers_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead idle session is detected by the ping, not by the next tool call."""
    _freeze_recovery(monkeypatch)
    monkeypatch.setattr(mcp_module, "_MCP_PING_INTERVAL_S", 0.01)
    monkeypatch.setattr(mcp_module, "_MCP_PING_TIMEOUT_S", 1.0)

    session = _FailingSession()
    tool = _FakeWrapper(session)
    provider, registry, connection = _make_provider(tool)

    async def unused_connect(servers: Any, registry_arg: ToolRegistry, **kwargs: Any) -> dict:
        return {}

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", unused_connect)

    provider._start_keepalive(_SERVER)

    assert await _wait_until(lambda: _SERVER not in provider.connected_server_names)
    assert session.pings >= 1
    assert connection.closed is True  # dead handle released
    assert registry.get(_TOOL) is tool  # tools retained
    assert provider.runtime_status()[_SERVER] == "failed"
    assert await _wait_until(lambda: _SERVER in provider._recovery_tasks)

    await provider.aclose()


async def test_keepalive_pings_healthy_session_without_disruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy session keeps getting pinged and stays connected."""
    _freeze_recovery(monkeypatch)
    monkeypatch.setattr(mcp_module, "_MCP_PING_INTERVAL_S", 0.01)
    monkeypatch.setattr(mcp_module, "_MCP_PING_TIMEOUT_S", 1.0)

    pings = 0

    class _HealthySession:
        async def send_ping(self) -> None:
            nonlocal pings
            pings += 1

    tool = _FakeWrapper(_HealthySession())
    provider, registry, _ = _make_provider(tool)

    provider._start_keepalive(_SERVER)

    assert await _wait_until(lambda: pings >= 2)
    assert provider.connected_server_names == {_SERVER}
    assert registry.get(_TOOL) is tool
    assert _SERVER not in provider._recovery_tasks

    await provider.aclose()


async def test_aclose_cancels_background_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not leave keepalive or recovery tasks running."""
    _freeze_recovery(monkeypatch)
    monkeypatch.setattr(mcp_module, "_MCP_PING_INTERVAL_S", 30.0)

    tool = _FakeTool()
    provider, _, _ = _make_provider(tool)
    provider._start_keepalive(_SERVER)
    provider._schedule_recovery(_SERVER)
    keepalive = provider._keepalive_tasks[_SERVER]
    recovery = provider._recovery_tasks[_SERVER]

    await provider.aclose()

    assert keepalive.done()
    assert recovery.done()
    assert provider._keepalive_tasks == {}
    assert provider._recovery_tasks == {}
