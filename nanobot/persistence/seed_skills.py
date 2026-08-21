"""Seed image-generation skills into the data dir when they are absent.

These three skills were built interactively by the agent and lost with the
container that held them, because at the time nothing outside that container's
disk knew they existed. Shipping them in the image makes the loss unrepeatable:
even with an empty durable mirror, a fresh container starts with them present.

Rules:
  * Never overwrite. A skill directory already on disk — restored from the
    mirror or edited by the agent — is newer than the image copy and wins.
  * Never write secrets. Keys come from the environment at run time.
  * Report every decision, and fail loudly on an unreadable source tree, so a
    silent no-op cannot masquerade as a successful seed.

Run as a pre-start step (see entrypoint.sh), after the durable-state restore.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

DEFAULT_SOURCE = Path("/app/seed-skills")


def resolve_data_dir() -> Path:
    explicit = os.environ.get("NANOBOT_DATA_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("HOME", "/home/nanobot")) / ".nanobot"


def seed(source: Path, target_root: Path) -> tuple[list[str], list[str]]:
    """Copy each missing skill directory. Returns (seeded, skipped)."""
    if not source.is_dir():
        raise FileNotFoundError(f"seed source {source} is not a directory")

    candidates = sorted(p for p in source.iterdir() if p.is_dir())
    if not candidates:
        raise FileNotFoundError(f"seed source {source} contains no skill directories")

    target_root.mkdir(parents=True, exist_ok=True)
    seeded: list[str] = []
    skipped: list[str] = []
    for skill in candidates:
        destination = target_root / skill.name
        if destination.exists():
            skipped.append(skill.name)
            continue
        shutil.copytree(skill, destination)
        if not (destination / "SKILL.md").is_file():
            raise RuntimeError(f"seeded {skill.name} but SKILL.md is missing at {destination}")
        seeded.append(skill.name)
    return seeded, skipped


def main() -> int:
    source = Path(os.environ.get("NANOBOT_SEED_SKILLS_DIR", DEFAULT_SOURCE))
    if os.environ.get("NANOBOT_SEED_SKILLS", "1").lower() in ("0", "false", "no"):
        print("[seed-skills] disabled by NANOBOT_SEED_SKILLS")
        return 0

    target_root = resolve_data_dir() / "workspace" / "skills"
    try:
        seeded, skipped = seed(source, target_root)
    except Exception as exc:  # noqa: BLE001 - the reason must reach the deploy log
        print(f"[seed-skills] error: {exc}", file=sys.stderr)
        return 1

    if seeded:
        print(f"[seed-skills] installed {len(seeded)} skill(s): {', '.join(seeded)}")
    if skipped:
        print(f"[seed-skills] already present, left untouched: {', '.join(skipped)}")
    if not seeded and not skipped:
        print("[seed-skills] nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
