"""Pre-start state restore, driven entirely by the environment.

The in-process mirror cannot restore ``config.json``: by the time the gateway
has a config object to read Supabase credentials from, the config file has
already been loaded, so a restored copy would only take effect one restart
later. Provider keys and channel settings edited in the WebUI therefore need a
restore that happens *before* anything reads the disk.

This module is that step. It takes credentials from the environment
(``SUPABASE_URL`` / ``SUPABASE_SERVICE_KEY``), pulls the mirrored tree, and
fills in every file the fresh container is missing — including the config file
— then exits. ``entrypoint.sh`` runs it before nanobot starts.

Exit codes matter here. When credentials are absent the step is a no-op and
exits 0 (local Docker usage must stay unaffected). When credentials are
present but every retry fails, it no longer refuses to start: a crash loop
kept the whole agent offline for as long as Supabase stayed slow. Instead it
places the restore-pending marker (see
:mod:`nanobot.persistence.restore_gate`) — which freezes every snapshot push
until a background restorer has pulled the full mirror — and exits 0. A
non-zero exit now only signals an unexpected internal error.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from nanobot.persistence.restore_gate import clear as clear_restore_marker
from nanobot.persistence.restore_gate import place as place_restore_marker
from nanobot.persistence.supabase_store import (
    SupabasePersistenceError,
    SupabaseStateStore,
)
from nanobot.persistence.tree_mirror import DEFAULT_TREE_EXCLUDES, TreeArchiveMirror

__all__ = ["main", "resolve_data_dir", "restore_tree"]

_PREFIX = "[state-bootstrap]"
# Enough attempts to ride out a transient database blip, short enough to stay
# well inside the platform's deploy health-check grace period. Anything longer
# is handled after boot by the gateway's background restorer, which keeps
# retrying without blocking startup.
_ATTEMPTS = 5
_BACKOFF_S = (5.0, 15.0, 30.0, 45.0)


def _log(message: str) -> None:
    """Print a prefixed line to stdout so it lands in the platform's logs."""
    print(f"{_PREFIX} {message}", flush=True)


def resolve_data_dir() -> Path:
    """Return the instance data directory without importing the config loader.

    Kept independent of :mod:`nanobot.config` on purpose: this runs before the
    config exists, and importing the loader would create it.
    """
    explicit = os.getenv("NANOBOT_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.getenv("HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".nanobot"


def _table() -> str:
    return os.getenv("NANOBOT_STATE_TABLE", "").strip() or "nanobot_state_blobs"


def _key() -> str:
    return os.getenv("NANOBOT_STATE_TREE_KEY", "").strip() or "state/data-tree"


def _max_bytes() -> int:
    raw = os.getenv("NANOBOT_STATE_TREE_MAX_BYTES", "").strip()
    if not raw:
        return 25_000_000
    try:
        value = int(raw)
    except ValueError as exc:
        raise SupabasePersistenceError(
            f"NANOBOT_STATE_TREE_MAX_BYTES is not an integer: {raw!r}"
        ) from exc
    if value <= 0:
        raise SupabasePersistenceError("NANOBOT_STATE_TREE_MAX_BYTES must be positive")
    return value


async def restore_tree(*, url: str, service_key: str, data_dir: Path) -> list[str]:
    """Restore the mirrored tree into ``data_dir`` and return restored paths."""
    store = SupabaseStateStore(
        url=url,
        service_key=service_key,
        table=_table(),
        timeout_s=float(os.getenv("NANOBOT_STATE_TIMEOUT_S", "").strip() or 30.0),
    )
    mirror = TreeArchiveMirror(
        store=store,
        root=data_dir,
        key=_key(),
        excludes=DEFAULT_TREE_EXCLUDES,
        max_bytes=_max_bytes(),
    )
    try:
        return await mirror.restore()
    finally:
        await store.aclose()


def main(argv: list[str] | None = None) -> int:
    """Restore mirrored state before startup. Returns a process exit code."""
    del argv  # no flags: every knob is an environment variable
    if os.getenv("NANOBOT_STATE_TREE", "").strip() in {"0", "false", "off"}:
        _log("tree restore disabled by NANOBOT_STATE_TREE — starting with local state only")
        return 0

    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not service_key or url.startswith("${") or service_key.startswith("${"):
        _log("Supabase credentials not set — starting with local state only")
        return 0

    data_dir = resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _log(f"restoring durable state into {data_dir} from {url}")

    last_error: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            restored = asyncio.run(
                restore_tree(url=url, service_key=service_key, data_dir=data_dir)
            )
        except Exception as exc:  # noqa: BLE001 - the exit code is the contract
            last_error = exc
            _log(f"attempt {attempt}/{_ATTEMPTS} failed: {exc}")
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_S[min(attempt - 1, len(_BACKOFF_S) - 1)])
            continue
        if restored:
            _log(f"restored {len(restored)} file(s)")
            for name in restored[:20]:
                _log(f"  + {name}")
            if len(restored) > 20:
                _log(f"  … and {len(restored) - 20} more")
        else:
            _log("nothing to restore (no mirrored tree yet, or local state already complete)")
        # A verified restore lifts any degraded start left over from a previous
        # boot: snapshots must never stay frozen behind a stale marker.
        clear_restore_marker(data_dir)
        return 0

    place_restore_marker(data_dir, f"restore failed after {_ATTEMPTS} attempts: {last_error}")
    _log(f"state restore failed after {_ATTEMPTS} attempts: {last_error}")
    _log("DEGRADED START: booting with unverified local state.")
    _log("Snapshots are frozen so the mirror cannot be overwritten, and a")
    _log("background restorer will retry until Supabase answers; snapshots")
    _log("resume automatically then. No restart is needed.")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
