"""Re-apply deployment-owned settings to a persisted config.json on every boot.

The durable mirror made ``config.json`` survive a container recycle, which is
what keeps WebUI-edited provider keys and channel settings alive. It also means
``entrypoint.sh`` stops copying the committed template: an existing config wins,
forever. That is right for anything a human tunes at runtime — and wrong for the
plumbing that keeps the deployment alive, because a fix committed to
``render-config.json`` then never reaches the running instance.

That trap is not hypothetical: the mirror cadence was tightened in the template
and the live service kept snapshotting on the old schedule, since its persisted
config had the previous numbers baked in.

So a narrow allowlist of *platform* keys is reconciled from the template into the
persisted config at every start. Deliberately narrow:

  * credentials are never touched — the template holds ``${VAR}`` placeholders,
    and overwriting a resolved secret with a placeholder would break the mirror,
  * user-owned sections (providers, channels, tools, agents, cron declarations,
    model presets) are never touched,
  * the merged document must parse *and* validate before it replaces anything,
    and the write is atomic, so a bad template cannot corrupt a working config,
  * an unchanged config is left byte-identical, so this step never churns the
    file mtime and never triggers a pointless mirror push.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE = Path("/app/render-config.json")

# Dotted paths, in template (camelCase) spelling. Everything here is
# infrastructure the deployment owns; nothing here is user-tunable.
PLATFORM_KEYS: tuple[str, ...] = (
    "persistence.supabase.snapshotIntervalS",
    "persistence.supabase.treeSnapshotIntervalS",
    "persistence.supabase.treeEnabled",
    "persistence.supabase.treeKey",
    "persistence.supabase.treeMaxBytes",
    "persistence.supabase.paths",
    "persistence.supabase.restoreOnStart",
    "persistence.keepalive.enabled",
    "persistence.keepalive.intervalS",
    "persistence.keepalive.path",
)

# Never copied, even if a key above would otherwise cover them.
CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "persistence.supabase.url",
        "persistence.supabase.serviceKey",
    }
)


def resolve_data_dir() -> Path:
    explicit = os.environ.get("NANOBOT_DATA_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("HOME", "/home/nanobot")) / ".nanobot"


def _get(document: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """Return ``(found, value)`` for a dotted path."""
    node: Any = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _set(document: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = document
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def plan_changes(
    live: dict[str, Any],
    template: dict[str, Any],
    keys: tuple[str, ...] = PLATFORM_KEYS,
) -> dict[str, tuple[Any, Any]]:
    """Return ``{dotted: (old, new)}`` for the platform keys that differ."""
    changes: dict[str, tuple[Any, Any]] = {}
    for dotted in keys:
        if dotted in CREDENTIAL_KEYS:
            continue
        present, wanted = _get(template, dotted)
        if not present:
            # The template no longer pins this setting; leave the live value be.
            continue
        _, current = _get(live, dotted)
        if current != wanted:
            changes[dotted] = (current, wanted)
    return changes


def _validates(document: dict[str, Any]) -> None:
    """Raise if the merged document is not a loadable config."""
    from nanobot.config.schema import Config

    Config(**document)


def reconcile(config_path: Path, template_path: Path) -> dict[str, tuple[Any, Any]]:
    """Apply platform keys from the template to ``config_path``.

    Returns the applied changes (empty when already current). The file is only
    rewritten when something actually changed, and only after the merged result
    validates.
    """
    if not config_path.is_file():
        # First boot: entrypoint.sh copies the template wholesale, so there is
        # nothing to reconcile.
        return {}
    if not template_path.is_file():
        raise FileNotFoundError(f"config template {template_path} is missing")

    live = json.loads(config_path.read_text())
    template = json.loads(template_path.read_text())
    if not isinstance(live, dict) or not isinstance(template, dict):
        raise ValueError("both config and template must be JSON objects")

    changes = plan_changes(live, template)
    if not changes:
        return {}

    merged = json.loads(json.dumps(live))  # deep copy; the original stays intact
    for dotted, (_, wanted) in changes.items():
        _set(merged, dotted, wanted)
    _validates(merged)

    tmp = config_path.with_suffix(config_path.suffix + ".reconcile-tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, config_path)
    return changes


def main() -> int:
    if os.environ.get("NANOBOT_RECONCILE_PLATFORM_CONFIG", "1").lower() in ("0", "false", "no"):
        print("[reconcile-config] disabled by NANOBOT_RECONCILE_PLATFORM_CONFIG")
        return 0

    template = Path(os.environ.get("NANOBOT_CONFIG_TEMPLATE", DEFAULT_TEMPLATE))
    config_path = resolve_data_dir() / "config.json"
    try:
        changes = reconcile(config_path, template)
    except Exception as exc:  # noqa: BLE001 - the reason must reach the deploy log
        print(f"[reconcile-config] error: {exc}", file=sys.stderr)
        return 1

    if not changes:
        print("[reconcile-config] platform settings already current")
        return 0
    for dotted, (old, new) in sorted(changes.items()):
        print(f"[reconcile-config] {dotted}: {old!r} -> {new!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
