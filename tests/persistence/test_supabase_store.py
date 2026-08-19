"""Tests for the Supabase state mirror used on disk-less hosts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from nanobot.persistence import (
    SupabasePersistenceError,
    SupabaseStateStore,
    WorkspaceStateMirror,
)

_URL = "https://project.supabase.co"
_KEY = "sb_secret_test_key"
_TABLE = "nanobot_state_blobs"


class _Recorder:
    """Collects requests and replays scripted responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(201, json=[])


def _store(handler: _Recorder) -> SupabaseStateStore:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SupabaseStateStore(url=_URL, service_key=_KEY, table=_TABLE, client=client)


class TestStoreConstruction:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"url": "", "service_key": _KEY}, "project url"),
            ({"url": _URL, "service_key": "  "}, "service key"),
            ({"url": _URL, "service_key": _KEY, "table": ""}, "table name"),
        ],
    )
    def test_rejects_incomplete_configuration(self, kwargs: dict, expected: str) -> None:
        with pytest.raises(SupabasePersistenceError, match=expected):
            SupabaseStateStore(**kwargs)

    def test_normalises_endpoint(self) -> None:
        store = SupabaseStateStore(url=f"{_URL}/", service_key=_KEY, table=_TABLE)
        assert store.endpoint == f"{_URL}/rest/v1/{_TABLE}"


class TestStoreRowAccess:
    @pytest.mark.asyncio
    async def test_pull_returns_row_and_sends_service_credentials(self) -> None:
        handler = _Recorder(
            [httpx.Response(200, json=[{"key": "k", "content": {"a": 1}, "content_hash": "h"}])]
        )
        store = _store(handler)

        row = await store.pull("workspace/cron/jobs.json")

        assert row is not None and row["content"] == {"a": 1}
        request = handler.requests[0]
        assert request.method == "GET"
        assert request.url.params["key"] == "eq.workspace/cron/jobs.json"
        assert request.headers["apikey"] == _KEY
        assert request.headers["authorization"] == f"Bearer {_KEY}"

    @pytest.mark.asyncio
    async def test_pull_returns_none_when_absent(self) -> None:
        store = _store(_Recorder([httpx.Response(200, json=[])]))
        assert await store.pull("missing") is None

    @pytest.mark.asyncio
    async def test_pull_rejects_unexpected_payload_shape(self) -> None:
        store = _store(_Recorder([httpx.Response(200, json={"unexpected": True})]))
        with pytest.raises(SupabasePersistenceError, match="unexpected payload shape"):
            await store.pull("k")

    @pytest.mark.asyncio
    async def test_pull_rejects_row_without_content_column(self) -> None:
        store = _store(_Recorder([httpx.Response(200, json=[{"key": "k"}])]))
        with pytest.raises(SupabasePersistenceError, match="missing the 'content' column"):
            await store.pull("k")

    @pytest.mark.asyncio
    async def test_push_upserts_with_content_hash(self) -> None:
        handler = _Recorder([httpx.Response(201, json=[])])
        store = _store(handler)

        digest = await store.push("workspace/cron/jobs.json", {"jobs": [1, 2]})

        request = handler.requests[0]
        body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.params["on_conflict"] == "key"
        assert "merge-duplicates" in request.headers["prefer"]
        assert body["key"] == "workspace/cron/jobs.json"
        assert body["content"] == {"jobs": [1, 2]}
        assert body["content_hash"] == digest and len(digest) == 64

    @pytest.mark.asyncio
    async def test_push_hash_is_key_order_independent(self) -> None:
        store = _store(_Recorder([httpx.Response(201), httpx.Response(201)]))
        first = await store.push("k", {"a": 1, "b": 2})
        second = await store.push("k", {"b": 2, "a": 1})
        assert first == second

    @pytest.mark.asyncio
    async def test_delete_targets_the_row(self) -> None:
        handler = _Recorder([httpx.Response(204)])
        await _store(handler).delete("workspace/cron/jobs.json")
        assert handler.requests[0].method == "DELETE"
        assert handler.requests[0].url.params["key"] == "eq.workspace/cron/jobs.json"

    @pytest.mark.asyncio
    async def test_http_error_is_raised_with_status(self) -> None:
        store = _store(_Recorder([httpx.Response(401, text="Invalid API key")]))
        with pytest.raises(SupabasePersistenceError, match="HTTP 401"):
            await store.pull("k")

    @pytest.mark.asyncio
    async def test_transport_failure_is_raised(self) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
        store = SupabaseStateStore(url=_URL, service_key=_KEY, client=client)
        with pytest.raises(SupabasePersistenceError, match="failed"):
            await store.pull("k")


class TestWorkspaceMirror:
    def _mirror(
        self, tmp_path: Path, handler: _Recorder, paths: list[str] | None = None
    ) -> WorkspaceStateMirror:
        return WorkspaceStateMirror(
            store=_store(handler),
            workspace_path=tmp_path,
            paths=paths or ["cron/jobs.json"],
        )

    @pytest.mark.asyncio
    async def test_restore_writes_missing_file_from_remote(self, tmp_path: Path) -> None:
        remote = {"jobs": [{"id": "declared:daily-brief"}]}
        handler = _Recorder(
            [httpx.Response(200, json=[{"key": "workspace/cron/jobs.json", "content": remote, "content_hash": "h"}])]
        )

        restored = await self._mirror(tmp_path, handler).restore()

        assert restored == ["cron/jobs.json"]
        written = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
        assert written == remote

    @pytest.mark.asyncio
    async def test_restore_preserves_existing_local_state(self, tmp_path: Path) -> None:
        local = tmp_path / "cron" / "jobs.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"jobs": ["local"]}), encoding="utf-8")
        handler = _Recorder()

        restored = await self._mirror(tmp_path, handler).restore()

        assert restored == []
        assert handler.requests == [], "local state is authoritative; no remote read needed"
        assert json.loads(local.read_text(encoding="utf-8")) == {"jobs": ["local"]}

    @pytest.mark.asyncio
    async def test_restore_treats_empty_file_as_missing(self, tmp_path: Path) -> None:
        local = tmp_path / "cron" / "jobs.json"
        local.parent.mkdir(parents=True)
        local.write_text("", encoding="utf-8")
        handler = _Recorder(
            [httpx.Response(200, json=[{"key": "workspace/cron/jobs.json", "content": {"jobs": []}, "content_hash": "h"}])]
        )

        assert await self._mirror(tmp_path, handler).restore() == ["cron/jobs.json"]

    @pytest.mark.asyncio
    async def test_restore_is_a_no_op_without_remote_state(self, tmp_path: Path) -> None:
        handler = _Recorder([httpx.Response(200, json=[])])
        assert await self._mirror(tmp_path, handler).restore() == []
        assert not (tmp_path / "cron" / "jobs.json").exists()

    @pytest.mark.asyncio
    async def test_snapshot_pushes_local_state_once_per_change(self, tmp_path: Path) -> None:
        local = tmp_path / "cron" / "jobs.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"jobs": ["a"]}), encoding="utf-8")
        handler = _Recorder([httpx.Response(201), httpx.Response(201)])
        mirror = self._mirror(tmp_path, handler)

        assert await mirror.snapshot() == ["cron/jobs.json"]
        assert await mirror.snapshot() == [], "unchanged content must not be re-pushed"

        local.write_text(json.dumps({"jobs": ["a", "b"]}), encoding="utf-8")
        assert await mirror.snapshot() == ["cron/jobs.json"]
        assert len(handler.requests) == 2

    @pytest.mark.asyncio
    async def test_snapshot_force_repushes(self, tmp_path: Path) -> None:
        local = tmp_path / "cron" / "jobs.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        handler = _Recorder([httpx.Response(201), httpx.Response(201)])
        mirror = self._mirror(tmp_path, handler)

        await mirror.snapshot()
        assert await mirror.snapshot(force=True) == ["cron/jobs.json"]

    @pytest.mark.asyncio
    async def test_snapshot_skips_absent_files(self, tmp_path: Path) -> None:
        handler = _Recorder()
        assert await self._mirror(tmp_path, handler).snapshot() == []
        assert handler.requests == []

    @pytest.mark.asyncio
    async def test_snapshot_fails_loudly_on_corrupt_json(self, tmp_path: Path) -> None:
        local = tmp_path / "cron" / "jobs.json"
        local.parent.mkdir(parents=True)
        local.write_text("{not json", encoding="utf-8")

        with pytest.raises(SupabasePersistenceError, match="not valid JSON"):
            await self._mirror(tmp_path, _Recorder()).snapshot()

    @pytest.mark.asyncio
    async def test_round_trip_survives_workspace_loss(self, tmp_path: Path) -> None:
        """Snapshot from one workspace, restore into a fresh one."""
        source = tmp_path / "source"
        (source / "cron").mkdir(parents=True)
        state = {"jobs": [{"id": "declared:daily-brief", "enabled": True}]}
        (source / "cron" / "jobs.json").write_text(json.dumps(state), encoding="utf-8")

        captured: dict[str, object] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                body = json.loads(request.content)
                captured[body["key"]] = body["content"]
                return httpx.Response(201)
            key = request.url.params["key"].removeprefix("eq.")
            if key in captured:
                return httpx.Response(200, json=[{"key": key, "content": captured[key], "content_hash": "h"}])
            return httpx.Response(200, json=[])

        recorder = _Recorder()
        recorder.__call__ = _handler  # type: ignore[method-assign]

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        store = SupabaseStateStore(url=_URL, service_key=_KEY, client=client)

        outbound = WorkspaceStateMirror(store=store, workspace_path=source, paths=["cron/jobs.json"])
        assert await outbound.snapshot() == ["cron/jobs.json"]

        fresh = tmp_path / "fresh"
        inbound = WorkspaceStateMirror(store=store, workspace_path=fresh, paths=["cron/jobs.json"])
        assert await inbound.restore() == ["cron/jobs.json"]
        assert json.loads((fresh / "cron" / "jobs.json").read_text(encoding="utf-8")) == state

    @pytest.mark.asyncio
    async def test_run_forever_rejects_non_positive_interval(self, tmp_path: Path) -> None:
        with pytest.raises(SupabasePersistenceError, match="positive"):
            await self._mirror(tmp_path, _Recorder()).run_forever(0)

    def test_paths_are_normalised(self, tmp_path: Path) -> None:
        mirror = self._mirror(tmp_path, _Recorder(), paths=["/cron/jobs.json", "  ", "sessions.json"])
        assert mirror.paths == ["cron/jobs.json", "sessions.json"]


class TestEventLoopResilience:
    """Regression cover for the crash that killed the deployed gateway.

    The gateway restores mirrored state with ``asyncio.run(...)`` before its own
    loop exists, then snapshots from the gateway loop. A client pooled on the
    first (now closed) loop raised ``RuntimeError: Event loop is closed`` out of
    httpcore — not an ``httpx.HTTPError`` — which escaped the snapshot task and
    stopped the agent, its Telegram channel, and its cron jobs every cycle.
    """

    def test_owned_client_is_rebuilt_for_each_event_loop(self, tmp_path: Path) -> None:
        state = {"jobs": [{"id": "declared:daily-brief"}]}
        target = tmp_path / "cron" / "jobs.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(state), encoding="utf-8")

        # Hold real references, never id() values: a discarded client can be
        # collected and the next allocation can land on the same address, which
        # made an id()-based assertion flaky under a full-suite run.
        seen: list[httpx.AsyncClient] = []

        store = SupabaseStateStore(url=_URL, service_key=_KEY, table=_TABLE)

        async def _capture_client() -> httpx.AsyncClient:
            return await store._get_client()  # noqa: SLF001 - loop binding is the contract

        # Two separate asyncio.run calls == two distinct, sequentially closed loops.
        seen.append(asyncio.run(_capture_client()))
        seen.append(asyncio.run(_capture_client()))

        assert seen[0] is not seen[1], "a client pooled on a closed loop must not be reused"

    @pytest.mark.asyncio
    async def test_runtime_error_from_dead_pool_is_retried_then_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _dead_loop(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise RuntimeError("Event loop is closed")

        real_client = httpx.AsyncClient

        def _mocked_client(**kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(_dead_loop), **kwargs)  # type: ignore[arg-type]

        # The rebuilt client must stay offline too, so the assertion sees the
        # wrapped RuntimeError rather than a DNS error from a real connection.
        monkeypatch.setattr(httpx, "AsyncClient", _mocked_client)

        store = SupabaseStateStore(url=_URL, service_key=_KEY, table=_TABLE)
        store._client = _mocked_client()  # noqa: SLF001
        store._owns_client = True  # noqa: SLF001
        store._client_loop = asyncio.get_running_loop()  # noqa: SLF001

        # Never a bare RuntimeError escaping to the caller's task group.
        with pytest.raises(SupabasePersistenceError, match="Event loop is closed"):
            await store.push("cron/jobs.json", {"jobs": []})
        assert len(calls) == 2, "an owned client gets exactly one retry on a fresh pool"

    @pytest.mark.asyncio
    async def test_snapshot_loop_survives_unexpected_errors(self, tmp_path: Path) -> None:
        mirror = WorkspaceStateMirror(
            store=_store(_Recorder()),
            workspace_path=tmp_path,
            paths=["cron/jobs.json"],
        )
        cycles = 0

        async def _boom() -> list[str]:
            nonlocal cycles
            cycles += 1
            raise RuntimeError("Event loop is closed")

        mirror.snapshot = _boom  # type: ignore[method-assign]

        task = asyncio.create_task(mirror.run_forever(0.01))
        while cycles < 3:
            await asyncio.sleep(0.01)
        assert not task.done(), "the mirror must keep cycling instead of taking the process down"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
