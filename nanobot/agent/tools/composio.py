"""Composio integration tools.

Gives the agent dynamic access to the whole Composio catalogue (~1200+ tools
across hundreds of third-party apps) without hard-coding a single one:

``composio_search_tools``  discover tools by intent or by app
``composio_tool_schema``   read a tool's input schema before calling it
``composio_execute``       run any Composio tool
``composio_connect``       produce an OAuth link the owner can click (Telegram)
``composio_connections``   inspect which apps are connected and healthy

Design notes (verified against the live v3 API):

* Composio-managed OAuth connections MUST be created through
  ``POST /connected_accounts/link``. The legacy ``POST /connected_accounts``
  path answers HTTP 400 for managed auth configs.
* Generated links are short-lived (~15 minutes), so they are never cached;
  every request mints a fresh one.
* An app with no auth config yet is provisioned on demand with Composio-managed
  auth, so the owner can connect a brand-new app without leaving the chat.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, cast

import httpx
from loguru import logger
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config_base import Base

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext

_DEFAULT_BASE_URL = "https://backend.composio.dev/api/v3"
_API_KEY_ENV = "COMPOSIO_API_KEY"
_MAX_RESULT_CHARS = 6000
_FACEBOOK_GRAPH_BASE = "https://graph.facebook.com/v20.0"
_FACEBOOK_CREATE_POST = "FACEBOOK_CREATE_POST"


class ComposioToolsConfig(Base):
    """Composio bridge configuration."""

    enabled: bool = True
    api_key: str = ""  # falls back to the COMPOSIO_API_KEY environment variable
    base_url: str = _DEFAULT_BASE_URL
    user_id: str = "default"  # Composio end-user identity owning the connections
    timeout: float = Field(default=60.0, gt=0)
    default_limit: int = Field(default=10, ge=1, le=50)
    auto_create_auth_config: bool = True
    proxy: str = ""


class ComposioError(RuntimeError):
    """Raised when the Composio API answers with an error payload."""


def _resolve_api_key(config: ComposioToolsConfig) -> str:
    """Config value wins; the environment is the deployment-friendly fallback."""
    key = (config.api_key or "").strip()
    if key:
        return key
    return (os.environ.get(_API_KEY_ENV) or "").strip()


def _truncate(payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + f"\n... [truncated, {len(text)} chars total]"


class _ComposioClient:
    """Thin async wrapper over the Composio v3 REST API."""

    def __init__(self, config: ComposioToolsConfig) -> None:
        self._config = config
        self._api_key = _resolve_api_key(config)

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = (self._config.base_url or _DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}/{path.lstrip('/')}"
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        client_kwargs: dict[str, Any] = {"timeout": self._config.timeout}
        if self._config.proxy:
            client_kwargs["proxy"] = self._config.proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                params={k: v for k, v in (params or {}).items() if v not in (None, "")},
                json=json_body,
            )

        try:
            payload = cast(dict[str, Any], response.json())
        except ValueError as exc:  # non-JSON body: surface status and raw text
            raise ComposioError(
                f"HTTP {response.status_code} from Composio ({path}): {response.text[:300]}"
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error)
                fix = error.get("suggested_fix")
                if fix:
                    message = f"{message} (suggested fix: {fix})"
            else:
                message = str(error or payload)[:400]
            raise ComposioError(f"HTTP {response.status_code} from Composio ({path}): {message}")

        return payload

    async def post_url(self, url: str, json_body: dict[str, Any]) -> dict[str, Any]:
        """POST to an arbitrary URL (outside this client's base) and parse JSON.

        Used by the Facebook page-token path: the Graph API is reached directly
        with a page access token, bypassing the connection's user-level token.
        """
        client_kwargs: dict[str, Any] = {"timeout": self._config.timeout}
        if self._config.proxy:
            client_kwargs["proxy"] = self._config.proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, json=json_body)

        try:
            payload = cast(dict[str, Any], response.json())
        except ValueError as exc:  # non-JSON body: surface status and raw text
            raise ComposioError(f"HTTP {response.status_code} from Facebook ({url}): {response.text[:300]}") from exc
        if response.status_code >= 400:
            message = str(payload.get("error") or payload)[:400]
            raise ComposioError(f"HTTP {response.status_code} from Facebook ({url}): {message}")
        return payload

    @staticmethod
    def items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("items")
        if raw is None:
            raw = payload.get("data")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    async def find_auth_config(self, toolkit: str) -> dict[str, Any] | None:
        """Return an existing usable auth config for ``toolkit``, if any."""
        payload = await self.request("GET", "/auth_configs", params={"toolkit_slug": toolkit, "limit": 20})
        candidates = self.items(payload)
        enabled = [c for c in candidates if str(c.get("status", "")).upper() == "ENABLED"]
        chosen = enabled or candidates
        return chosen[0] if chosen else None

    async def create_managed_auth_config(self, toolkit: str) -> dict[str, Any]:
        """Provision a Composio-managed auth config for a not-yet-configured app."""
        payload = await self.request(
            "POST",
            "/auth_configs",
            json_body={"toolkit": {"slug": toolkit}, "auth_config": {"type": "use_composio_managed_auth"}},
        )
        created = payload.get("auth_config")
        if isinstance(created, dict) and created.get("id"):
            return created
        if payload.get("id"):
            return payload
        raise ComposioError(f"Composio did not return an auth config id for '{toolkit}': {payload}")


class _ComposioTool(Tool):
    """Shared plumbing for the Composio tool family.

    Stays abstract (``name``/``description`` unset) so the package scanner does
    not register it as a callable tool.
    """

    config_key = "composio"
    _scopes = {"core", "subagent"}

    @classmethod
    def config_cls(cls) -> type[ComposioToolsConfig]:
        return ComposioToolsConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        config = getattr(ctx.config, "composio", None)
        if config is None or not config.enabled:
            return False
        # Without a key every call would fail; hide the tools instead.
        return bool(_resolve_api_key(config))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(config=getattr(ctx.config, "composio", None))

    def __init__(self, config: ComposioToolsConfig | None = None) -> None:
        self.config = config if config is not None else ComposioToolsConfig()
        self.client = _ComposioClient(self.config)

    def _user_id(self, user_id: str | None = None) -> str:
        return (user_id or "").strip() or self.config.user_id or "default"

    async def _guarded(self, coro: Any) -> Any:
        """Run an API coroutine, converting failures into tool-level errors."""
        if not self.client.has_key:
            return ToolResult.error(
                "No Composio API key configured. Set tools.composio.apiKey or the "
                f"{_API_KEY_ENV} environment variable."
            )
        try:
            return await coro
        except ComposioError as exc:
            logger.warning("Composio API error: {}", exc)
            return ToolResult.error(str(exc))
        except httpx.HTTPError as exc:
            logger.warning("Composio transport error: {}", exc)
            return ToolResult.error(f"Could not reach Composio: {exc}")


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Intent to search for, e.g. 'send an email', 'create a calendar event'."),
        toolkit=StringSchema("Optional app slug to restrict the search, e.g. gmail, slack, notion, github."),
        limit=IntegerSchema(description="Maximum number of tools to return (1-50, default 10).", minimum=1, maximum=50),
    )
)
class ComposioSearchToolsTool(_ComposioTool):
    """Discover Composio tools dynamically."""

    name = "composio_search_tools"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Search the Composio catalogue of third-party app tools (Gmail, Slack, Notion, GitHub, "
        "Sheets, and hundreds more). Provide query and/or toolkit. Returns tool slugs to use with "
        "composio_tool_schema and composio_execute. Prefer toolkit when the app is already known."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query") or "").strip()
        toolkit = str(kwargs.get("toolkit") or "").strip().lower()
        limit = int(kwargs.get("limit") or self.config.default_limit)
        if not query and not toolkit:
            return ToolResult.error("Provide at least one of 'query' or 'toolkit'.")

        async def _run() -> Any:
            payload = await self.client.request(
                "GET",
                "/tools",
                params={"search": query, "toolkit_slug": toolkit, "limit": limit},
            )
            items = self.client.items(payload)
            if not items:
                hint = f" for toolkit '{toolkit}'" if toolkit else ""
                return f"No Composio tools matched{hint}. Try a different query or app slug."
            lines = [f"{payload.get('total_items', len(items))} match(es); showing {len(items)}:"]
            for item in items:
                tk = item.get("toolkit")
                tk_slug = tk.get("slug") if isinstance(tk, dict) else tk
                desc = str(item.get("description") or item.get("human_description") or "").strip()
                if len(desc) > 160:
                    desc = desc[:160] + "..."
                auth = "no auth required" if item.get("no_auth") else "needs a connected account"
                lines.append(f"- {item.get('slug')} [{tk_slug}] ({auth}): {desc}")
            return "\n".join(lines)

        return await self._guarded(_run())


@tool_parameters(
    tool_parameters_schema(
        tool_slug=StringSchema("Exact Composio tool slug, e.g. GMAIL_SEND_EMAIL.", min_length=1),
        required=["tool_slug"],
    )
)
class ComposioToolSchemaTool(_ComposioTool):
    """Read a Composio tool's input schema."""

    name = "composio_tool_schema"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Get the input parameter schema of one Composio tool before executing it. "
        "Call this whenever the required arguments of a tool are not already known."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        tool_slug = str(kwargs.get("tool_slug") or "").strip()
        if not tool_slug:
            return ToolResult.error("'tool_slug' is required.")

        async def _run() -> Any:
            try:
                payload = await self.client.request("GET", f"/tools/{tool_slug}")
            except ComposioError:
                # Older/alternate routing: fall back to a filtered listing.
                listing = await self.client.request("GET", "/tools", params={"tool_slugs": tool_slug, "limit": 1})
                found = self.client.items(listing)
                if not found:
                    return ToolResult.error(
                        f"Unknown Composio tool '{tool_slug}'. Use composio_search_tools to find a valid slug."
                    )
                payload = found[0]

            tk = payload.get("toolkit")
            summary = {
                "slug": payload.get("slug", tool_slug),
                "name": payload.get("name"),
                "toolkit": tk.get("slug") if isinstance(tk, dict) else tk,
                "no_auth": payload.get("no_auth"),
                "description": payload.get("description") or payload.get("human_description"),
                "input_parameters": payload.get("input_parameters"),
            }
            return _truncate(summary)

        return await self._guarded(_run())


@tool_parameters(
    tool_parameters_schema(
        tool_slug=StringSchema("Exact Composio tool slug to run, e.g. GMAIL_SEND_EMAIL.", min_length=1),
        arguments=ObjectSchema(
            description="Arguments object matching the tool's input schema (see composio_tool_schema).",
            additional_properties=True,
        ),
        user_id=StringSchema("Optional Composio end-user id owning the connection. Defaults to the configured owner."),
        required=["tool_slug"],
    )
)
class ComposioExecuteTool(_ComposioTool):
    """Execute any Composio tool."""

    name = "composio_execute"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Execute a Composio tool by slug with an arguments object. Verify the argument names with "
        "composio_tool_schema first. If the app is not connected yet, use composio_connect to get "
        "an authorization link for the owner."
    )

    async def execute(self, **kwargs: Any) -> Any:
        tool_slug = str(kwargs.get("tool_slug") or "").strip()
        if not tool_slug:
            return ToolResult.error("'tool_slug' is required.")
        raw_args = kwargs.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except ValueError:
                return ToolResult.error("'arguments' must be a JSON object.")
        if not isinstance(raw_args, dict):
            return ToolResult.error("'arguments' must be a JSON object.")
        user_id = self._user_id(kwargs.get("user_id"))

        # FACEBOOK_CREATE_POST is special-cased: Composio authenticates it with
        # the connection's user-level token, which the modern Facebook Pages API
        # rejects (#200) when posting to a managed Page. Routing it instead
        # through the Page's own access token (fetched via GET_USER_PAGES, which
        # returns per-Page admin tokens) makes publishing work for the owner.
        if tool_slug == _FACEBOOK_CREATE_POST:
            return await self._guarded(self._facebook_create_post(user_id, raw_args))

        async def _run() -> Any:
            payload = await self.client.request(
                "POST",
                f"/tools/execute/{tool_slug}",
                json_body={"user_id": user_id, "arguments": raw_args},
            )
            if payload.get("successful"):
                return _truncate(payload.get("data", payload))
            error = str(payload.get("error") or "unknown error")
            hint = ""
            lowered = error.lower()
            if "connect" in lowered or "account" in lowered or "auth" in lowered:
                hint = " Use composio_connect to send the owner an authorization link for this app."
            return ToolResult.error(f"{tool_slug} failed: {error}.{hint}")

        return await self._guarded(_run())

    async def _facebook_create_post(self, user_id: str, args: dict[str, Any]) -> Any:
        """Publish to a Facebook Page using the Page's own access token."""
        page_id = str(args.get("page_id") or "").strip()
        if not page_id:
            return ToolResult.error("'page_id' is required to publish on Facebook.")

        try:
            pages_payload = await self.client.request(
                "POST",
                "/tools/execute/FACEBOOK_GET_USER_PAGES",
                json_body={"user_id": user_id, "arguments": {}},
            )
        except ComposioError as exc:
            return ToolResult.error(f"Could not list Facebook pages to publish: {exc}")

        page_token = None
        root = pages_payload.get("data")
        if isinstance(root, dict) and isinstance(root.get("response_data"), dict):
            root = root.get("response_data", {})
        pages = root.get("data", []) if isinstance(root, dict) else []
        for page in pages if isinstance(pages, list) else []:
            if str(page.get("id")) == page_id:
                page_token = page.get("access_token")
                break
        if not page_token:
            return ToolResult.error(
                f"No Page access token found for Facebook page '{page_id}' for user '{user_id}'. "
                "Make sure the connected account manages this page, then retry."
            )

        feed_body: dict[str, Any] = {"access_token": page_token}
        if args.get("message"):
            feed_body["message"] = str(args["message"]).strip()
        if args.get("link"):
            feed_body["link"] = str(args["link"]).strip()
        try:
            result = await self.client.post_url(
                f"{_FACEBOOK_GRAPH_BASE}/{page_id}/feed",
                feed_body,
            )
        except ComposioError as exc:
            return ToolResult.error(f"{_FACEBOOK_CREATE_POST} failed: {exc}")
        return _truncate(result)


@tool_parameters(
    tool_parameters_schema(
        toolkit=StringSchema("App slug to connect, e.g. gmail, slack, notion, github.", min_length=1),
        user_id=StringSchema("Optional Composio end-user id to connect. Defaults to the configured owner."),
        required=["toolkit"],
    )
)
class ComposioConnectTool(_ComposioTool):
    """Mint a fresh OAuth authorization link for an app."""

    name = "composio_connect"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Get a one-click authorization link so the owner can connect a third-party app to Composio. "
        "Send the returned link to the owner in chat. Links expire in about 15 minutes, so always "
        "generate a new one instead of reusing an old link."
    )

    async def execute(self, **kwargs: Any) -> Any:
        toolkit = str(kwargs.get("toolkit") or "").strip().lower()
        if not toolkit:
            return ToolResult.error("'toolkit' is required, e.g. gmail.")
        user_id = self._user_id(kwargs.get("user_id"))

        async def _run() -> Any:
            auth_config = await self.client.find_auth_config(toolkit)
            provisioned = False
            if auth_config is None:
                if not self.config.auto_create_auth_config:
                    return ToolResult.error(
                        f"No auth config exists for '{toolkit}' and auto-creation is disabled."
                    )
                auth_config = await self.client.create_managed_auth_config(toolkit)
                provisioned = True

            auth_config_id = auth_config.get("id")
            if not auth_config_id:
                return ToolResult.error(f"Could not resolve an auth config id for '{toolkit}'.")

            link = await self.client.request(
                "POST",
                "/connected_accounts/link",
                json_body={"auth_config_id": auth_config_id, "user_id": user_id},
            )
            redirect_url = link.get("redirect_url")
            if not redirect_url:
                return ToolResult.error(f"Composio returned no authorization link for '{toolkit}': {link}")

            lines = [
                f"Authorization link for {toolkit} (user '{user_id}'):",
                str(redirect_url),
                "Send this link to the owner; it expires in about 15 minutes.",
            ]
            if link.get("expires_at"):
                lines.append(f"Expires at: {link['expires_at']}")
            if provisioned:
                lines.append(f"(A Composio-managed auth config was created for {toolkit}.)")
            return "\n".join(lines)

        return await self._guarded(_run())


@tool_parameters(
    tool_parameters_schema(
        toolkit=StringSchema("Optional app slug to filter by, e.g. gmail."),
        user_id=StringSchema("Optional Composio end-user id. Defaults to the configured owner."),
        limit=IntegerSchema(
            description="Maximum number of connections to list (1-50, default 10).", minimum=1, maximum=50
        ),
    )
)
class ComposioConnectionsTool(_ComposioTool):
    """Inspect connected accounts and their health."""

    name = "composio_connections"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "List the third-party accounts connected through Composio and their status "
        "(ACTIVE, EXPIRED, ...). Use it to check whether an app is usable before executing its tools, "
        "or to spot a connection that needs re-authorization via composio_connect."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        toolkit = str(kwargs.get("toolkit") or "").strip().lower()
        limit = int(kwargs.get("limit") or self.config.default_limit)
        user_id = self._user_id(kwargs.get("user_id"))

        async def _run() -> Any:
            payload = await self.client.request(
                "GET",
                "/connected_accounts",
                params={"toolkit_slugs": toolkit, "user_ids": user_id, "limit": limit},
            )
            items = self.client.items(payload)
            if not items:
                scope = f" for '{toolkit}'" if toolkit else ""
                return (
                    f"No Composio connections found{scope} for user '{user_id}'. "
                    "Use composio_connect to authorize an app."
                )
            lines = [f"{len(items)} connection(s) for user '{user_id}':"]
            for item in items:
                tk = item.get("toolkit")
                tk_slug = tk.get("slug") if isinstance(tk, dict) else tk
                status = str(item.get("status") or "UNKNOWN")
                note = " -> needs re-authorization" if status.upper() != "ACTIVE" else ""
                lines.append(f"- {tk_slug}: {status}{note} (id {item.get('id')})")
            return "\n".join(lines)

        return await self._guarded(_run())
