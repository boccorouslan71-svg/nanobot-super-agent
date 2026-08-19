"""Supabase-backed mirror for runtime state that would otherwise be lost.

Free-tier container hosts (Render workers, Fly machines, …) give no persistent
disk: the workspace is recreated on every deploy and after every sleep cycle.
Nanobot keeps live state on disk (``cron/jobs.json`` and friends), so a restart
silently drops scheduled jobs.

This module mirrors those JSON files into a Supabase table through PostgREST:

* :class:`SupabaseStateStore` — thin, typed row access (pull / push / delete).
* :class:`WorkspaceStateMirror` — maps workspace-relative JSON files to rows,
  restores them on boot and snapshots them while the process runs.

Design rules (see the repository's persistence notes):

* Only the service/secret key is accepted; the table is expected to have RLS
  enabled so the publishable key cannot read it.
* A restore never clobbers newer local state: it writes a file only when the
  local copy is missing or empty, which is exactly the fresh-container case.
* Failures are loud. Every method either succeeds or raises
  :class:`SupabasePersistenceError`; nothing is reported as "no data".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

__all__ = [
    "SupabasePersistenceError",
    "SupabaseStateStore",
    "WorkspaceStateMirror",
]

_JSON_CONTENT_TYPE = "application/json"


class SupabasePersistenceError(RuntimeError):
    """Raised when the Supabase state mirror cannot complete an operation."""


def _content_hash(payload: Any) -> str:
    """Return a stable hash of a JSON-serialisable payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SupabaseStateStore:
    """Row-level access to the Supabase state table over PostgREST."""

    def __init__(
        self,
        *,
        url: str,
        service_key: str,
        table: str = "nanobot_state_blobs",
        timeout_s: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url or not url.strip():
            raise SupabasePersistenceError("Supabase persistence requires a project url")
        if not service_key or not service_key.strip():
            raise SupabasePersistenceError("Supabase persistence requires a service key")
        if not table or not table.strip():
            raise SupabasePersistenceError("Supabase persistence requires a table name")

        self._url = url.strip().rstrip("/")
        self._service_key = service_key.strip()
        self._table = table.strip()
        self._timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    @property
    def endpoint(self) -> str:
        """Return the PostgREST endpoint for the configured table."""
        return f"{self._url}/rest/v1/{self._table}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": _JSON_CONTENT_TYPE,
            "Accept": _JSON_CONTENT_TYPE,
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """Return an owned client bound to the *running* event loop.

        The gateway restores mirrored state with ``asyncio.run(...)`` before its
        own loop exists, so a client created there (and the connections pooled
        inside it) outlives the loop it was built on. Reusing that pool from the
        gateway loop raises ``RuntimeError: Event loop is closed`` from deep
        inside httpcore — a non-``httpx`` error that used to escape the snapshot
        task and take the whole gateway down. Rebuilding whenever the loop
        changes keeps every request on a live pool.
        """
        if self._owns_client:
            loop = asyncio.get_running_loop()
            if self._client is not None and self._client_loop is not loop:
                await self._reset_client()
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self._timeout_s)
            self._client_loop = loop
        elif self._client is None:  # pragma: no cover - defensive
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_client = True
        return self._client

    async def _reset_client(self) -> None:
        """Discard an owned client so the next call rebuilds it on this loop."""
        if self._client is None or not self._owns_client:
            return
        stale = self._client
        self._client = None
        self._client_loop = None
        try:
            await stale.aclose()
        except Exception as exc:  # pragma: no cover - stale-loop teardown
            logger.debug("Supabase mirror: discarded unusable HTTP client ({})", exc)

    async def aclose(self) -> None:
        """Close the HTTP client when this store owns it."""
        if self._client is not None and self._owns_client:
            await self._reset_client()

    async def _request(self, method: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        # One retry, but only for a client this store owns: a transport error can
        # come from a pool that is no longer usable, and the retry runs against a
        # freshly built one. An injected client (tests) is never rebuilt.
        attempts = 2 if self._owns_client else 1
        for attempt in range(1, attempts + 1):
            client = await self._get_client()
            try:
                response = await client.request(
                    method,
                    self.endpoint,
                    headers=headers,
                    **kwargs,
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                # httpx.HTTPError: network / DNS / timeout.
                # RuntimeError: pooled connection bound to a dead event loop.
                await self._reset_client()
                if attempt < attempts:
                    logger.debug(
                        "Supabase {} {} failed ({}); retrying on a fresh client",
                        method,
                        self._table,
                        exc,
                    )
                    continue
                raise SupabasePersistenceError(
                    f"Supabase {method} {self._table} failed: {exc}"
                ) from exc

            if response.status_code >= 400:
                raise SupabasePersistenceError(
                    f"Supabase {method} {self._table} returned HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            return response

        raise SupabasePersistenceError(  # pragma: no cover - loop always returns/raises
            f"Supabase {method} {self._table} failed: no attempt completed"
        )

    async def pull(self, key: str) -> dict[str, Any] | None:
        """Return the stored payload for ``key``, or None when absent."""
        response = await self._request(
            "GET",
            params={"key": f"eq.{key}", "select": "key,content,content_hash", "limit": 1},
        )
        try:
            rows = response.json()
        except ValueError as exc:
            raise SupabasePersistenceError(
                f"Supabase returned non-JSON payload for key '{key}'"
            ) from exc
        if not isinstance(rows, list):
            raise SupabasePersistenceError(
                f"Supabase returned unexpected payload shape for key '{key}': {type(rows)!r}"
            )
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict) or "content" not in row:
            raise SupabasePersistenceError(
                f"Supabase row for key '{key}' is missing the 'content' column"
            )
        return row

    async def push(self, key: str, content: Any) -> str:
        """Upsert ``content`` under ``key`` and return the stored content hash."""
        digest = _content_hash(content)
        await self._request(
            "POST",
            params={"on_conflict": "key"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"key": key, "content": content, "content_hash": digest},
        )
        return digest

    async def delete(self, key: str) -> None:
        """Delete the row stored under ``key`` (no-op when absent)."""
        await self._request("DELETE", params={"key": f"eq.{key}"})


class WorkspaceStateMirror:
    """Mirror workspace JSON files to Supabase and restore them on a cold start."""

    def __init__(
        self,
        *,
        store: SupabaseStateStore,
        workspace_path: Path,
        paths: list[str],
        namespace: str = "workspace",
    ) -> None:
        self._store = store
        self._workspace_path = Path(workspace_path)
        self._paths = [p.strip().lstrip("/") for p in paths if p and p.strip()]
        self._namespace = namespace.strip("/") or "workspace"
        self._hashes: dict[str, str] = {}

    @property
    def paths(self) -> list[str]:
        """Return the mirrored workspace-relative paths."""
        return list(self._paths)

    def _row_key(self, rel_path: str) -> str:
        return f"{self._namespace}/{rel_path}"

    def _local_path(self, rel_path: str) -> Path:
        return self._workspace_path / rel_path

    async def restore(self) -> list[str]:
        """Restore mirrored files that are missing or empty locally.

        Returns the list of restored relative paths. Existing non-empty local
        files are always preserved — local state is authoritative once a
        container has written to it.
        """
        restored: list[str] = []
        for rel_path in self._paths:
            local = self._local_path(rel_path)
            if local.exists() and local.stat().st_size > 0:
                logger.debug("Supabase mirror: keeping local {}", rel_path)
                continue

            row = await self._store.pull(self._row_key(rel_path))
            if row is None:
                logger.debug("Supabase mirror: no remote state for {}", rel_path)
                continue

            content = row["content"]
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(
                json.dumps(content, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._hashes[rel_path] = _content_hash(content)
            restored.append(rel_path)
            logger.info("Supabase mirror: restored {} from Supabase", rel_path)
        return restored

    def _read_local_json(self, rel_path: str) -> Any | None:
        local = self._local_path(rel_path)
        if not local.exists() or local.stat().st_size == 0:
            return None
        raw = local.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SupabasePersistenceError(
                f"Cannot mirror '{rel_path}': file is not valid JSON ({exc})"
            ) from exc

    async def snapshot(self, *, force: bool = False) -> list[str]:
        """Push locally changed files to Supabase; return the pushed paths."""
        pushed: list[str] = []
        for rel_path in self._paths:
            content = self._read_local_json(rel_path)
            if content is None:
                continue
            digest = _content_hash(content)
            if not force and self._hashes.get(rel_path) == digest:
                continue
            await self._store.push(self._row_key(rel_path), content)
            self._hashes[rel_path] = digest
            pushed.append(rel_path)
            logger.info("Supabase mirror: snapshotted {}", rel_path)
        return pushed

    async def run_forever(self, interval_s: float) -> None:
        """Snapshot on a fixed cadence until cancelled.

        A failing cycle is logged and retried on the next tick: losing the
        mirror must never take the agent process down.
        """
        if interval_s <= 0:
            raise SupabasePersistenceError("snapshot interval must be positive")
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.snapshot()
            except asyncio.CancelledError:
                with_final_error: BaseException | None = None
                try:
                    await self.snapshot()
                except Exception as exc:  # pragma: no cover - shutdown path
                    with_final_error = exc
                if with_final_error is not None:  # pragma: no cover - shutdown path
                    logger.error("Supabase mirror: final snapshot failed: {}", with_final_error)
                raise
            except SupabasePersistenceError as exc:
                logger.error("Supabase mirror: snapshot cycle failed: {}", exc)
            except Exception as exc:
                # Last line of defence: the mirror is a convenience, so no
                # unexpected error from it may ever reach the gateway's task
                # group and stop the agent, its channels, or its cron jobs.
                logger.error("Supabase mirror: unexpected snapshot error: {}", exc)
