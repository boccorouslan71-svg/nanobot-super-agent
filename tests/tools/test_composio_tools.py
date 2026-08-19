"""Tests for the Composio bridge tools.

Every test runs offline: the HTTP layer (``_ComposioClient.request``) is
replaced with a recorder so the tools' routing, formatting, and error handling
are asserted without touching the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.agent.tools.composio import (
    ComposioConnectionsTool,
    ComposioConnectTool,
    ComposioError,
    ComposioExecuteTool,
    ComposioSearchToolsTool,
    ComposioToolSchemaTool,
    ComposioToolsConfig,
    _resolve_api_key,
)
from nanobot.agent.tools.loader import ToolLoader
from nanobot.config.schema import Config, ToolsConfig

_COMPOSIO_TOOL_NAMES = {
    "composio_search_tools",
    "composio_tool_schema",
    "composio_execute",
    "composio_connect",
    "composio_connections",
}


class _FakeAPI:
    """Records requests and replays queued responses."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body})
        if not self._responses:
            raise AssertionError(f"unexpected extra request: {method} {path}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _tool(cls: type, responses: list[Any], **config_kwargs: Any) -> tuple[Any, _FakeAPI]:
    config = ComposioToolsConfig(api_key="test-key", user_id="owner", **config_kwargs)
    tool = cls(config=config)
    fake = _FakeAPI(responses)
    tool.client.request = fake  # type: ignore[method-assign]
    return tool, fake


# --- wiring -----------------------------------------------------------------


def test_composio_config_is_wired_into_tools_config() -> None:
    config = Config()

    assert isinstance(config.tools.composio, ComposioToolsConfig)
    assert config.tools.composio.base_url == "https://backend.composio.dev/api/v3"
    assert config.tools.composio.enabled is True


def test_composio_config_accepts_camel_case_overrides() -> None:
    config = Config.model_validate({
        "tools": {"composio": {"apiKey": "ak_x", "userId": "telegram-owner", "defaultLimit": 25}},
    })

    assert config.tools.composio.api_key == "ak_x"
    assert config.tools.composio.user_id == "telegram-owner"
    assert config.tools.composio.default_limit == 25


def test_native_tool_configs_are_untouched() -> None:
    """Adding the bridge must not disturb existing tool configuration."""
    tools = ToolsConfig()

    for field in ("web", "exec", "file", "cli_apps", "my", "image_generation"):
        assert hasattr(tools, field)


def test_all_five_composio_tools_are_discovered() -> None:
    discovered = {cls.__name__ for cls in ToolLoader().discover()}

    assert {
        "ComposioSearchToolsTool",
        "ComposioToolSchemaTool",
        "ComposioExecuteTool",
        "ComposioConnectTool",
        "ComposioConnectionsTool",
    } <= discovered
    # The shared abstract base must never be exposed as a callable tool.
    assert "_ComposioTool" not in discovered


def test_tool_names_and_schemas() -> None:
    configs = ComposioToolsConfig(api_key="k")
    tools = [
        ComposioSearchToolsTool(config=configs),
        ComposioToolSchemaTool(config=configs),
        ComposioExecuteTool(config=configs),
        ComposioConnectTool(config=configs),
        ComposioConnectionsTool(config=configs),
    ]

    assert {t.name for t in tools} == _COMPOSIO_TOOL_NAMES
    for t in tools:
        assert t.parameters["type"] == "object"
        assert t.description

    # Read-only vs side-effecting classification drives safe parallelism.
    assert ComposioSearchToolsTool(config=configs).read_only is True
    assert ComposioConnectionsTool(config=configs).read_only is True
    assert ComposioExecuteTool(config=configs).read_only is False
    assert ComposioConnectTool(config=configs).read_only is False


def test_api_key_falls_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSIO_API_KEY", "ak_from_env")

    assert _resolve_api_key(ComposioToolsConfig()) == "ak_from_env"
    # An explicit config value wins over the environment.
    assert _resolve_api_key(ComposioToolsConfig(api_key="ak_explicit")) == "ak_explicit"


def test_tools_hide_themselves_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)

    class _Ctx:
        config = ToolsConfig()

    assert ComposioSearchToolsTool.enabled(_Ctx()) is False  # type: ignore[arg-type]

    monkeypatch.setenv("COMPOSIO_API_KEY", "ak_present")
    assert ComposioSearchToolsTool.enabled(_Ctx()) is True  # type: ignore[arg-type]


def test_disabled_config_hides_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSIO_API_KEY", "ak_present")

    class _Ctx:
        config = ToolsConfig.model_validate({"composio": {"enabled": False}})

    assert ComposioSearchToolsTool.enabled(_Ctx()) is False  # type: ignore[arg-type]


# --- search -----------------------------------------------------------------


async def test_search_lists_slugs_with_toolkit_and_auth_hint() -> None:
    tool, fake = _tool(ComposioSearchToolsTool, [{
        "total_items": 2,
        "items": [
            {"slug": "GMAIL_SEND_EMAIL", "toolkit": {"slug": "gmail"}, "description": "Send an email", "no_auth": False},
            {"slug": "COMPOSIO_SEARCH_NEWS", "toolkit": {"slug": "composio_search"}, "description": "News", "no_auth": True},
        ],
    }])

    result = await tool.execute(query="send an email", limit=5)

    assert "GMAIL_SEND_EMAIL" in result
    assert "[gmail]" in result
    assert "needs a connected account" in result
    assert "no auth required" in result
    assert fake.calls[0]["params"]["search"] == "send an email"
    assert fake.calls[0]["params"]["limit"] == 5


async def test_search_requires_query_or_toolkit() -> None:
    tool, fake = _tool(ComposioSearchToolsTool, [])

    result = await tool.execute()

    assert result.is_error
    assert not fake.calls


async def test_search_reports_empty_catalogue_match() -> None:
    tool, _ = _tool(ComposioSearchToolsTool, [{"items": []}])

    result = await tool.execute(toolkit="nonexistent_app")

    assert "No Composio tools matched" in result
    assert not getattr(result, "is_error", False)


async def test_api_error_becomes_tool_error() -> None:
    tool, _ = _tool(ComposioSearchToolsTool, [ComposioError("HTTP 401 from Composio (/tools): invalid key")])

    result = await tool.execute(query="x")

    assert result.is_error
    assert "invalid key" in result


# --- schema -----------------------------------------------------------------


async def test_tool_schema_returns_input_parameters() -> None:
    tool, fake = _tool(ComposioToolSchemaTool, [{
        "slug": "GMAIL_SEND_EMAIL",
        "name": "Send Email",
        "toolkit": {"slug": "gmail"},
        "no_auth": False,
        "description": "Sends an email",
        "input_parameters": {"type": "object", "required": ["recipient_email"]},
    }])

    result = await tool.execute(tool_slug="GMAIL_SEND_EMAIL")

    assert "recipient_email" in result
    assert fake.calls[0]["path"] == "/tools/GMAIL_SEND_EMAIL"


async def test_tool_schema_falls_back_to_filtered_listing() -> None:
    tool, fake = _tool(ComposioToolSchemaTool, [
        ComposioError("HTTP 404 from Composio (/tools/X): not found"),
        {"items": [{"slug": "X", "toolkit": "custom", "input_parameters": {"type": "object"}}]},
    ])

    result = await tool.execute(tool_slug="X")

    assert not getattr(result, "is_error", False)
    assert len(fake.calls) == 2
    assert fake.calls[1]["params"]["tool_slugs"] == "X"


async def test_tool_schema_unknown_slug_is_actionable() -> None:
    tool, _ = _tool(ComposioToolSchemaTool, [
        ComposioError("HTTP 404"),
        {"items": []},
    ])

    result = await tool.execute(tool_slug="NOPE")

    assert result.is_error
    assert "composio_search_tools" in result


# --- execute ----------------------------------------------------------------


async def test_execute_sends_user_id_and_arguments() -> None:
    tool, fake = _tool(ComposioExecuteTool, [{"successful": True, "data": {"id": "msg-1"}}])

    result = await tool.execute(tool_slug="GMAIL_SEND_EMAIL", arguments={"recipient_email": "a@b.c"})

    assert "msg-1" in result
    call = fake.calls[0]
    assert call["path"] == "/tools/execute/GMAIL_SEND_EMAIL"
    assert call["json"] == {"user_id": "owner", "arguments": {"recipient_email": "a@b.c"}}


async def test_execute_accepts_json_encoded_arguments() -> None:
    tool, fake = _tool(ComposioExecuteTool, [{"successful": True, "data": "ok"}])

    result = await tool.execute(tool_slug="T", arguments='{"query": "x"}')

    assert not getattr(result, "is_error", False)
    assert fake.calls[0]["json"]["arguments"] == {"query": "x"}


async def test_execute_rejects_non_object_arguments() -> None:
    tool, fake = _tool(ComposioExecuteTool, [])

    result = await tool.execute(tool_slug="T", arguments="not json")

    assert result.is_error
    assert not fake.calls


async def test_execute_failure_points_to_the_connect_tool() -> None:
    tool, _ = _tool(ComposioExecuteTool, [
        {"successful": False, "error": "No connected account found for user ID owner for toolkit gmail"},
    ])

    result = await tool.execute(tool_slug="GMAIL_SEND_EMAIL", arguments={})

    assert result.is_error
    assert "composio_connect" in result


async def test_execute_honours_per_call_user_id() -> None:
    tool, fake = _tool(ComposioExecuteTool, [{"successful": True, "data": {}}])

    await tool.execute(tool_slug="T", arguments={}, user_id="someone-else")

    assert fake.calls[0]["json"]["user_id"] == "someone-else"


# --- connect ----------------------------------------------------------------


async def test_connect_reuses_an_enabled_auth_config() -> None:
    tool, fake = _tool(ComposioConnectTool, [
        {"items": [
            {"id": "ac_disabled", "status": "DISABLED"},
            {"id": "ac_live", "status": "ENABLED"},
        ]},
        {"redirect_url": "https://connect.composio.dev/link/lk_1", "expires_at": "2026-08-19T00:51:49.087Z"},
    ])

    result = await tool.execute(toolkit="gmail")

    assert "https://connect.composio.dev/link/lk_1" in result
    assert "expires" in result.lower()
    # Managed OAuth must use the /link endpoint, not the legacy create path.
    assert fake.calls[1]["path"] == "/connected_accounts/link"
    assert fake.calls[1]["json"] == {"auth_config_id": "ac_live", "user_id": "owner"}


async def test_connect_provisions_a_managed_auth_config_when_missing() -> None:
    tool, fake = _tool(ComposioConnectTool, [
        {"items": []},
        {"toolkit": {"slug": "linear"}, "auth_config": {"id": "ac_new", "is_composio_managed": True}},
        {"redirect_url": "https://connect.composio.dev/link/lk_2"},
    ])

    result = await tool.execute(toolkit="linear")

    assert "lk_2" in result
    assert "auth config was created" in result
    assert fake.calls[1]["path"] == "/auth_configs"
    assert fake.calls[1]["json"]["auth_config"] == {"type": "use_composio_managed_auth"}
    assert fake.calls[2]["json"]["auth_config_id"] == "ac_new"


async def test_connect_respects_auto_create_opt_out() -> None:
    tool, fake = _tool(ComposioConnectTool, [{"items": []}], auto_create_auth_config=False)

    result = await tool.execute(toolkit="linear")

    assert result.is_error
    assert len(fake.calls) == 1


async def test_connect_errors_when_no_link_is_returned() -> None:
    tool, _ = _tool(ComposioConnectTool, [
        {"items": [{"id": "ac_live", "status": "ENABLED"}]},
        {"link_token": "lk_x"},  # no redirect_url
    ])

    result = await tool.execute(toolkit="gmail")

    assert result.is_error


async def test_connect_requires_a_toolkit() -> None:
    tool, fake = _tool(ComposioConnectTool, [])

    result = await tool.execute()

    assert result.is_error
    assert not fake.calls


# --- connections ------------------------------------------------------------


async def test_connections_flags_unhealthy_accounts() -> None:
    tool, fake = _tool(ComposioConnectionsTool, [{"items": [
        {"id": "ca_1", "toolkit": {"slug": "gmail"}, "status": "ACTIVE"},
        {"id": "ca_2", "toolkit": {"slug": "github"}, "status": "EXPIRED"},
    ]}])

    result = await tool.execute()

    assert "gmail: ACTIVE" in result
    assert "github: EXPIRED -> needs re-authorization" in result
    assert fake.calls[0]["params"]["user_ids"] == "owner"


async def test_connections_empty_suggests_connecting() -> None:
    tool, _ = _tool(ComposioConnectionsTool, [{"items": []}])

    result = await tool.execute(toolkit="slack")

    assert "composio_connect" in result
