"""Degraded-start gate shared by the Supabase mirrors.

``bootstrap.py`` used to fail closed when the pre-start restore could not
reach Supabase: the container exited, the platform restarted it, and the same
failing read ran again — a crash loop that took the whole agent offline for as
long as the database had a slow stretch. Booting anyway has its own hazard: a
fresh container carries only the template state, and its first snapshots would
overwrite a healthy mirror with near-nothing.

This gate resolves that tension with one marker file in the data directory:

* :func:`place` records "the local tree may be incomplete" after bootstrap has
  exhausted its retries;
* while the marker exists, both mirrors skip every snapshot push, so an
  incomplete tree can never reach Supabase;
* the gateway starts a background restorer (:meth:`TreeArchiveMirror.restore`
  and its per-file sibling) that retries on a fixed cadence; the first fully
  successful round clears the marker and both mirrors resume on their next
  cycle — no restart needed.

The marker is excluded from the mirrored tree so it never travels to another
container.
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "MARKER_FILENAME",
    "clear",
    "default_data_dir",
    "is_held",
    "marker_path",
    "place",
]

MARKER_FILENAME = ".restore-pending"


def default_data_dir() -> Path:
    """Return the instance data dir without importing the config loader.

    Mirrors :func:`nanobot.persistence.bootstrap.resolve_data_dir` on purpose:
    this module must stay importable from contexts where the config package is
    not loaded yet (or must not be).
    """
    explicit = os.getenv("NANOBOT_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.getenv("HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".nanobot"


def marker_path(data_dir: Path | None = None) -> Path:
    """Return the marker location inside ``data_dir`` (env-resolved by default)."""
    root = data_dir if data_dir is not None else default_data_dir()
    return root / MARKER_FILENAME


def place(data_dir: Path, reason: str) -> None:
    """Record that this container started without a verified restore.

    Raises if the marker cannot be written: an unwritable data directory means
    the agent cannot persist anything anyway, and silently skipping the gate
    would let snapshots overwrite a healthy mirror.
    """
    path = marker_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{datetime.now(timezone.utc).isoformat()} {reason}\n", encoding="utf-8"
    )


def is_held(data_dir: Path | None = None) -> bool:
    """Return True while snapshots must stay frozen for this container."""
    try:
        return marker_path(data_dir).exists()
    except OSError:  # pragma: no cover - defensive: stat on a weird path
        return False


def clear(data_dir: Path | None = None) -> None:
    """Remove the marker once a full restore has succeeded."""
    with suppress(OSError):
        marker_path(data_dir).unlink()
