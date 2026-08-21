"""Whole-tree Supabase mirror for hosts with no persistent disk.

:mod:`nanobot.persistence.supabase_store` mirrors a hand-listed set of JSON
files. That is enough for ``cron/jobs.json`` and the MCP token store, but it
cannot carry the state a user actually accumulates at runtime:

* ``config.json`` — provider API keys and channel settings edited in the WebUI
* ``workspace/skills/**`` — skills the agent installs for itself
* ``workspace/**`` — everything the agent writes while working
* ``sessions/**``, ``webui/**`` — conversation history
* ``mcp/**``, ``auth/**`` — MCP server state and OAuth credentials

None of that is a fixed list of JSON documents, so this module mirrors the
whole data directory as one deterministic ``tar.gz`` blob:

* :class:`TreeArchiveMirror` — archive/restore a directory tree through
  :class:`~nanobot.persistence.supabase_store.SupabaseStateStore`.

Rules that make the mirror safe to run unattended:

* **A restore never clobbers local work.** A member is written only when the
  local file is missing or empty, so a container that has already done work
  keeps it and only the gaps are filled.
* **A snapshot never destroys remote state.** Pushing an empty tree over a
  populated mirror is refused loudly, because that is the shape of every
  catastrophic "the fresh container overwrote my backup" bug.
* **Extraction is hostile-input safe.** Absolute paths, ``..`` traversal,
  symlinks, and device entries are rejected rather than trusted.
* **Failures are loud**, per the module contract of the JSON mirror.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import gzip
import hashlib
import io
import os
import tarfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanobot.persistence.supabase_store import (
    SupabasePersistenceError,
    SupabaseStateStore,
)

__all__ = [
    "DEFAULT_TREE_EXCLUDES",
    "TreeArchiveMirror",
    "TreeArchivePayload",
]

_ARCHIVE_FORMAT = "tar.gz+base64"

# Excluded by default: regenerated caches, logs (write-heavy, never restored),
# and dependency trees that would dwarf the payload. Everything else is user
# state and is mirrored, including binaries such as channel auth databases.
DEFAULT_TREE_EXCLUDES: tuple[str, ...] = (
    "logs",
    # Channel attachment cache. Excluded because one large inbound video would
    # push the archive over its size limit, and a mirror that fails wholesale
    # protects nothing — while the media itself still lives in the chat platform
    # it came from. Everything that is only in this container is mirrored.
    "media",
    "__pycache__",
    "node_modules",
    ".venv",
    ".cache",
    # Rendered video. The photo-zoom-video skill writes clips into the workspace,
    # and a handful of them outweighs every text file the mirror exists to
    # protect — a 40 MB archive limit reached is a mirror that fails wholesale,
    # taking the config and cron state down with it. A clip is reproducible from
    # its source photo and one command, so it is regenerated, never archived.
    "**/*.mp4",
    "**/*.mov",
    "**/*.mkv",
    "**/*.webm",
    "**/*.m4v",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.sock",
    "**/*.lock",
    "**/*.tmp",
    "**/.DS_Store",
)


class TreeArchivePayload(BaseModel):
    """Parsed representation of a stored tree row.

    Parsed at the boundary on purpose: a row written by an older build (or by
    hand) must fail here with a clear message instead of turning into a
    half-extracted tree.
    """

    model_config = ConfigDict(extra="ignore")

    format: str
    data: str
    file_count: int = Field(default=0, ge=0)
    byte_size: int = Field(default=0, ge=0)
    created_at: str | None = None

    def decode(self) -> bytes:
        """Return the raw ``tar.gz`` bytes carried by this payload."""
        if self.format != _ARCHIVE_FORMAT:
            raise SupabasePersistenceError(
                f"mirrored tree has unsupported format {self.format!r}; expected {_ARCHIVE_FORMAT!r}"
            )
        try:
            return base64.b64decode(self.data.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise SupabasePersistenceError(f"mirrored tree is not valid base64: {exc}") from exc


def _is_excluded(rel_path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Return whether ``rel_path`` (posix, relative to the tree root) is excluded.

    A pattern without a separator or wildcard matches any path *segment* — so
    ``logs`` covers both ``logs/app.log`` and ``workspace/logs/app.log``.
    Anything else is matched against the full relative path with
    :mod:`fnmatch`, whose ``*`` crosses separators, making ``logs/**`` and
    ``**/*.pyc`` behave as expected.
    """
    segments = rel_path.split("/")
    for pattern in patterns:
        if not pattern:
            continue
        if "/" not in pattern and "*" not in pattern and "?" not in pattern:
            if pattern in segments:
                return True
            continue
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


class TreeArchiveMirror:
    """Mirror a whole directory tree to one Supabase row."""

    def __init__(
        self,
        *,
        store: SupabaseStateStore,
        root: Path,
        key: str = "state/data-tree",
        excludes: tuple[str, ...] | list[str] | None = None,
        max_bytes: int = 25_000_000,
    ) -> None:
        if max_bytes <= 0:
            raise SupabasePersistenceError("tree mirror max_bytes must be positive")
        self._store = store
        self._root = Path(root)
        self._key = key.strip("/") or "state/data-tree"
        self._excludes = tuple(excludes if excludes is not None else DEFAULT_TREE_EXCLUDES)
        self._max_bytes = max_bytes
        self._hash: str | None = None
        self._signature: str | None = None

    @property
    def key(self) -> str:
        """Return the Supabase row key this mirror owns."""
        return self._key

    @property
    def root(self) -> Path:
        """Return the mirrored tree root."""
        return self._root

    # ---------------------------------------------------------------- archive

    def iter_files(self) -> list[Path]:
        """Return the mirrored regular files, sorted for a deterministic archive."""
        if not self._root.exists():
            return []
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            current = Path(dirpath)
            rel_dir = current.relative_to(self._root).as_posix()
            # Prune excluded directories in place so os.walk never descends.
            kept: list[str] = []
            for name in dirnames:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if _is_excluded(rel, self._excludes):
                    continue
                kept.append(name)
            dirnames[:] = sorted(kept)
            for name in sorted(filenames):
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if _is_excluded(rel, self._excludes):
                    continue
                path = current / name
                # Symlinks are not mirrored: their target may sit outside the
                # tree, and restoring one would be a path-traversal write.
                if path.is_symlink() or not path.is_file():
                    continue
                found.append(path)
        return sorted(found, key=lambda p: p.relative_to(self._root).as_posix())

    def build_archive(self) -> tuple[bytes, int]:
        """Return ``(tar.gz bytes, file count)`` for the current tree.

        The archive is byte-stable for identical content: member order is
        sorted, ownership and timestamps are zeroed, and gzip is told not to
        stamp the current time. Without that, every cycle would look changed
        and re-upload the whole tree.
        """
        files = self.iter_files()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in files:
                rel = path.relative_to(self._root).as_posix()
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise SupabasePersistenceError(
                        f"cannot mirror '{rel}': {exc}"
                    ) from exc
                info = tarfile.TarInfo(name=rel)
                info.size = len(data)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                # Keep the executable bit; drop everything else to a sane mode.
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
        archive = gzip.compress(raw.getvalue(), compresslevel=6, mtime=0)
        if len(archive) > self._max_bytes:
            biggest = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:5]
            listing = ", ".join(
                f"{p.relative_to(self._root).as_posix()} ({p.stat().st_size} bytes)"
                for p in biggest
            )
            raise SupabasePersistenceError(
                f"tree archive is {len(archive)} bytes, over the {self._max_bytes} byte limit; "
                f"largest files: {listing}"
            )
        return archive, len(files)

    # --------------------------------------------------------------- transfer

    def tree_signature(self) -> str:
        """Return a cheap fingerprint of the tree: path, size and mtime only.

        This exists so a short snapshot cadence stays affordable. Building the
        archive means reading and gzipping every mirrored file; at a 15s cadence
        that is 240 full reads an hour, almost all of them producing a payload
        identical to the last one. Stat-ing the same files costs no reads, so an
        idle cycle can be dismissed for the price of a directory walk.

        Deliberately *not* a content hash: it must be strictly cheaper than the
        work it guards. mtime is nanosecond-resolution, so a rewritten file
        changes the signature even when its size is identical; and the signature
        only ever suppresses work when nothing looks touched — the archive digest
        remains the authority on whether a push is actually needed.
        """
        parts: list[str] = []
        for path in self.iter_files():
            rel = path.relative_to(self._root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                # A file that vanished mid-walk is a change by definition; let
                # the archive build be the one to deal with it.
                return ""
            parts.append(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}")
        if not parts:
            return ""
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    async def snapshot(self, *, force: bool = False) -> int:
        """Push the tree when it changed; return the number of files pushed."""
        signature = self.tree_signature()
        # An empty signature means either an empty tree or a file that moved
        # under us. Both must fall through: the empty-tree case has its own
        # guard below, and a moving tree needs a real archive pass.
        if not force and signature and signature == self._signature:
            return 0
        archive, count = self.build_archive()
        if count == 0:
            # A fresh container that failed to restore must not erase the
            # mirror: refuse, loudly, and let the next cycle try again.
            remote = await self._read_remote()
            if remote is not None and remote.file_count > 0:
                raise SupabasePersistenceError(
                    f"refusing to overwrite {remote.file_count} mirrored files with an empty tree "
                    f"({self._root})"
                )
            return 0
        digest = hashlib.sha256(archive).hexdigest()
        if not force and self._hash == digest:
            # Touched but not changed (a rewrite with identical content). The
            # remote is already current, so record the new signature and skip
            # the rebuild next cycle too.
            self._signature = signature
            return 0
        payload = {
            "format": _ARCHIVE_FORMAT,
            "data": base64.b64encode(archive).decode("ascii"),
            "file_count": count,
            "byte_size": len(archive),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._store.push(self._key, payload)
        self._hash = digest
        # Only after a successful push: if the push raises, the next cycle must
        # rebuild and retry rather than believe the tree is already mirrored.
        self._signature = signature
        logger.info(
            "Supabase tree mirror: snapshotted {} files ({} bytes) from {}",
            count,
            len(archive),
            self._root,
        )
        return count

    async def _read_remote(self) -> TreeArchivePayload | None:
        row = await self._store.pull(self._key)
        if row is None:
            return None
        content = row.get("content")
        if not isinstance(content, dict):
            raise SupabasePersistenceError(
                f"mirrored tree '{self._key}' has a non-object content column"
            )
        try:
            return TreeArchivePayload.model_validate(content)
        except ValidationError as exc:
            raise SupabasePersistenceError(
                f"mirrored tree '{self._key}' is malformed: {exc}"
            ) from exc

    def _safe_target(self, member_name: str) -> Path | None:
        """Resolve an archive member to a path inside the tree, or None.

        Absolute names are rejected rather than re-rooted: a member named
        ``/etc/passwd`` is not something this mirror ever writes, so it means the
        row came from somewhere else and none of it should be trusted.
        """
        name = member_name.replace("\\", "/")
        if name.startswith("/") or ":" in name.split("/")[0]:
            return None
        if not name or name.startswith("../") or "/../" in name or name.endswith("/.."):
            return None
        target = (self._root / name).resolve()
        root = self._root.resolve()
        if target == root or root not in target.parents:
            return None
        return target

    async def restore(self) -> list[str]:
        """Extract mirrored files that are missing or empty locally.

        Returns the restored relative paths. Existing non-empty files always
        win: local state is authoritative once a container has written to it.
        """
        remote = await self._read_remote()
        if remote is None:
            logger.info("Supabase tree mirror: no remote tree for '{}'", self._key)
            return []
        archive = remote.decode()
        restored: list[str] = []
        skipped_unsafe: list[str] = []
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile():
                    skipped_unsafe.append(member.name)
                    continue
                target = self._safe_target(member.name)
                if target is None:
                    skipped_unsafe.append(member.name)
                    continue
                if target.exists() and target.stat().st_size > 0:
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:  # pragma: no cover - defensive
                    skipped_unsafe.append(member.name)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extracted.read())
                with suppress(OSError):
                    target.chmod(member.mode or 0o644)
                restored.append(member.name)
        if skipped_unsafe:
            logger.warning(
                "Supabase tree mirror: skipped {} unsafe archive member(s): {}",
                len(skipped_unsafe),
                ", ".join(skipped_unsafe[:5]),
            )
        if restored:
            logger.info(
                "Supabase tree mirror: restored {} file(s) into {}", len(restored), self._root
            )
        else:
            logger.info("Supabase tree mirror: local tree already complete ({})", self._root)
        return restored

    async def run_forever(self, interval_s: float) -> None:
        """Snapshot on a fixed cadence until cancelled.

        A failing cycle is logged and retried on the next tick, and the final
        snapshot runs on cancellation so a graceful shutdown does not lose the
        last few minutes of work. Losing the mirror must never take the agent
        process down.
        """
        if interval_s <= 0:
            raise SupabasePersistenceError("snapshot interval must be positive")
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.snapshot()
            except asyncio.CancelledError:
                try:
                    await self.snapshot()
                except Exception as exc:  # pragma: no cover - shutdown path
                    logger.error("Supabase tree mirror: final snapshot failed: {}", exc)
                raise
            except SupabasePersistenceError as exc:
                logger.error("Supabase tree mirror: snapshot cycle failed: {}", exc)
            except Exception as exc:
                logger.error("Supabase tree mirror: unexpected snapshot error: {}", exc)

