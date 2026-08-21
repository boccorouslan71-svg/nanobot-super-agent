"""Validate the committed runtime config: placeholder expansion, provider
fallback chain, Telegram owner allowlist, and Composio wiring."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGNES_API_KEY", "sk-agnes-test")
os.environ.setdefault("GEMINI_API_KEY", "gemini-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "anthropic-test")
os.environ.setdefault("COMPOSIO_API_KEY", "ak_test")
os.environ.setdefault("COMPOSIO_USER_ID", "nanobot-owner")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "111:AAA-token")
os.environ.setdefault("TELEGRAM_OWNER_ID", "8888207809")
os.environ.setdefault("NANOBOT_WEB_TOKEN", "web-secret")
os.environ.setdefault("SUPABASE_URL", "https://validate.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "sb_secret_validate")

from nanobot.config.loader import load_config, resolve_config_env_vars  # noqa: E402
from nanobot.providers.factory import _resolve_fallback_presets  # noqa: E402

repo = Path(__file__).resolve().parents[1]
template = repo / "render-config.json"

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "config.json"
    path.write_text(template.read_text())
    # load_config parses; the gateway resolves ${VAR} refs as a separate step,
    # so the template must survive both stages exactly as production runs them.
    config = resolve_config_env_vars(load_config(path), config_path=path)

print("--- placeholder expansion ---")
check("providers.agnes.api_key", config.providers.agnes.api_key, "sk-agnes-test")
check("providers.agnes.api_base", config.providers.agnes.api_base, "https://apihub.agnes-ai.com/v1")
check("providers.gemini.api_key", config.providers.gemini.api_key, "gemini-test")
check("tools.composio.api_key", config.tools.composio.api_key, "ak_test")
check("tools.composio.user_id", config.tools.composio.user_id, "nanobot-owner")

print("\n--- telegram owner restriction (expansion inside a list) ---")
telegram = config.channels.model_extra.get("telegram") if config.channels.model_extra else None
telegram = telegram if telegram is not None else getattr(config.channels, "telegram", None)
tg = telegram if isinstance(telegram, dict) else (telegram.model_dump() if telegram else {})
check("telegram.enabled", tg.get("enabled"), True)
check("telegram.token", tg.get("token"), "111:AAA-token")
check("telegram.allow_from", tg.get("allow_from", tg.get("allowFrom")), ["8888207809"])
check("telegram.mode", tg.get("mode"), "polling")

print("\n--- primary model + native fallback chain ---")
defaults = config.agents.defaults
check("primary model", defaults.model, "agnes-2.5-flash")
check("primary provider", defaults.provider, "agnes")
primary_preset = type(config).__module__ and None
from nanobot.config.schema import ModelPresetConfig  # noqa: E402

primary = ModelPresetConfig(
    model=defaults.model, provider=defaults.provider, max_tokens=defaults.max_tokens
)
chain = _resolve_fallback_presets(config, primary)
resolved = [f"{p.provider}/{p.model}" for p in chain]
check("fallback chain", resolved, ["gemini/gemini-3.6-flash", "gemini/gemini-3.5-flash"])

print("\n--- telegram allowlist semantics (owner-only) ---")
# Exercised through the shared channel authorization path (BaseChannel), so the
# check does not require the optional python-telegram-bot dependency.
from nanobot.channels.base import BaseChannel  # noqa: E402


class _AuthProbe(BaseChannel):
    name = "telegram"

    def __init__(self, config: dict[str, object]) -> None:
        self._probe_config = config

    @property
    def config(self) -> dict[str, object]:  # type: ignore[override]
        return self._probe_config

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: object) -> None: ...


probe = _AuthProbe(tg)
check("owner is allowed", probe.is_allowed("8888207809"), True)
check("stranger is denied", probe.is_allowed("123456789"), False)

print("\n--- cron declarations (version-controlled bootstrap) ---")
from nanobot.cron.bootstrap import bootstrap_declared_cron_jobs  # noqa: E402
from nanobot.cron.service import CronService  # noqa: E402
from nanobot.cron.session_turns import is_bound_cron_job  # noqa: E402

check("cron bootstrap enabled", config.cron.enabled, True)
declared = {d.id: d for d in config.cron.declarations}
check("declaration ids", sorted(declared), ["composio-connection-health", "daily-brief"])
check("daily-brief schedule", declared["daily-brief"].describe_schedule(), "cron 0 8 * * *")
check("daily-brief timezone", declared["daily-brief"].build_schedule().tz, "Africa/Porto-Novo")
check("daily-brief target expanded", declared["daily-brief"].to, "8888207809")
check("daily-brief session key", declared["daily-brief"].session_key, "telegram:8888207809")

with tempfile.TemporaryDirectory() as tmp:
    store_path = Path(tmp) / "cron" / "jobs.json"
    cron_service = CronService(store_path)
    bootstrap = bootstrap_declared_cron_jobs(cron_service, config)
    jobs = cron_service.list_jobs(include_disabled=True)
    check("bootstrap failures", bootstrap.failed, {})
    check(
        "declared jobs registered",
        sorted(job.id for job in jobs),
        ["declared:composio-connection-health", "declared:daily-brief"],
    )
    check("declared jobs enabled", all(job.enabled for job in jobs), True)
    check("declared jobs session-bound", all(is_bound_cron_job(job) for job in jobs), True)
    check("declared jobs scheduled", all(job.state.next_run_at_ms for job in jobs), True)

    # Simulate the ephemeral host: the whole cron store disappears on redeploy.
    store_path.unlink()
    rebuilt = CronService(store_path)
    bootstrap_declared_cron_jobs(rebuilt, config)
    check("declarations survive store loss", len(rebuilt.list_jobs()), 2)

    # Re-applying the same config must not duplicate or disable anything.
    again = bootstrap_declared_cron_jobs(rebuilt, config)
    check("re-apply is idempotent", len(rebuilt.list_jobs()), 2)
    check("re-apply prunes nothing", again.pruned, [])

print("\n--- supabase state mirror ---")
supabase = config.persistence.supabase
check("mirror enabled", supabase.enabled, True)
check("mirror url expanded", supabase.url, "https://validate.supabase.co")
check("mirror key expanded", supabase.service_key, "sb_secret_validate")
check("mirror table", supabase.table, "nanobot_state_blobs")
check("mirror paths", supabase.paths, ["cron/jobs.json", "data:auth/mcp.json"])
check("mirror restores on start", supabase.restore_on_start, True)
check("json mirror cadence", supabase.snapshot_interval_s, 15)

print("\n--- free-tier keepalive (anti-sleep self ping) ---")
from nanobot.persistence import build_keepalive  # noqa: E402

keepalive_cfg = config.persistence.keepalive
check("keepalive enabled", keepalive_cfg.enabled, True)
check("keepalive interval", keepalive_cfg.interval_s, 300)
check("keepalive path", keepalive_cfg.path, "/")
# baseUrl is intentionally unset in the template: Render injects the real public
# URL at runtime, so the ping follows the service instead of a hardcoded host.
check("keepalive base url unset in template", keepalive_cfg.base_url, None)

built = build_keepalive(
    config, environ={"RENDER_EXTERNAL_URL": "https://nanobot-abee.onrender.com"}
)
check("keepalive built from platform url", built is not None, True)
if built is not None:
    check("keepalive ping url", built.url, "https://nanobot-abee.onrender.com/")
    check("keepalive interval under render's 15min idle window", built.interval_s < 900, True)
# A host that injects nothing must degrade to "no keepalive", never crash boot.
check("keepalive degrades without a public url", build_keepalive(config, environ={}), None)

print("\n--- whole-tree state mirror (config, skills, workspace, sessions) ---")
import asyncio  # noqa: E402

from nanobot.persistence import TreeArchiveMirror  # noqa: E402

tree_cfg = config.persistence.supabase
check("tree mirror enabled", tree_cfg.tree_enabled, True)
check("tree row key", tree_cfg.tree_key, "state/data-tree")
check("tree snapshot cadence", tree_cfg.tree_snapshot_interval_s, 15)
check("tree size limit", tree_cfg.tree_max_bytes, 40000000)
# Write-heavy or regenerable paths stay out; everything else is user state.
for skipped in ("logs", "media", "__pycache__", "node_modules"):
    check(f"tree excludes '{skipped}'", skipped in tree_cfg.tree_excludes, True)

# End-to-end round trip against an in-memory store: this is the behaviour the
# deployment depends on, so it is checked here rather than assumed from config.
class _MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def pull(self, key: str):
        return {"key": key, "content": self.rows[key]} if key in self.rows else None

    async def push(self, key: str, content: object) -> str:
        self.rows[key] = content
        return "digest"


async def _round_trip() -> tuple[int, dict[str, str], bool]:
    with tempfile.TemporaryDirectory() as old_dir, tempfile.TemporaryDirectory() as new_dir:
        old, new = Path(old_dir), Path(new_dir)
        (old / "workspace" / "skills" / "make").mkdir(parents=True)
        (old / "sessions").mkdir(parents=True)
        (old / "logs").mkdir(parents=True)
        (old / "config.json").write_text('{"providers": {"gemini": {"apiKey": "runtime-key"}}}')
        (old / "workspace" / "skills" / "make" / "SKILL.md").write_text("# Make")
        (old / "workspace" / "notes.md").write_text("agent work")
        (old / "sessions" / "telegram.json").write_text('{"messages": [1]}')
        (old / "logs" / "app.log").write_text("noise")

        store = _MemoryStore()
        pushed = await TreeArchiveMirror(
            store=store, root=old, key=tree_cfg.tree_key, excludes=tree_cfg.tree_excludes
        ).snapshot()
        restored = await TreeArchiveMirror(
            store=store, root=new, key=tree_cfg.tree_key, excludes=tree_cfg.tree_excludes
        ).restore()
        contents = {
            name: (new / name).read_text()
            for name in ("config.json", "workspace/notes.md")
        }
        return pushed, contents, "logs/app.log" in restored


pushed_count, restored_contents, logs_leaked = asyncio.run(_round_trip())
check("tree snapshot pushed the user's files", pushed_count, 4)
check(
    "provider keys survive a cold start",
    "runtime-key" in restored_contents["config.json"],
    True,
)
check("workspace work survives a cold start", restored_contents["workspace/notes.md"], "agent work")
check("excluded logs are not restored", logs_leaked, False)

# A 15s cadence is only safe because an unchanged tree costs a stat walk instead
# of a full archive build. Assert that here: if the fast path ever regresses, the
# deployment would quietly read and gzip the whole data dir 240 times an hour.
async def _idle_cycles_are_cheap() -> tuple[int, int, int]:
    with tempfile.TemporaryDirectory() as work_dir:
        root = Path(work_dir)
        (root / "workspace").mkdir(parents=True)
        (root / "config.json").write_text('{"providers": {}}')
        (root / "workspace" / "notes.md").write_text("state")

        builds = 0

        class _CountingMirror(TreeArchiveMirror):
            def build_archive(self):  # type: ignore[no-untyped-def]
                nonlocal builds
                builds += 1
                return super().build_archive()

        store = _MemoryStore()
        mirror = _CountingMirror(
            store=store, root=root, key=tree_cfg.tree_key, excludes=tree_cfg.tree_excludes
        )
        first = await mirror.snapshot()
        for _ in range(20):  # five minutes of 15s ticks with nothing happening
            await mirror.snapshot()
        idle_builds = builds
        (root / "workspace" / "new-skill.md").write_text("# created by the agent")
        after_change = await mirror.snapshot()
        return first, idle_builds, after_change


first_push, idle_builds, change_push = asyncio.run(_idle_cycles_are_cheap())
check("first cycle mirrors the tree", first_push > 0, True)
check("20 idle cycles rebuild the archive once", idle_builds, 1)
check("a new file is still mirrored on the next cycle", change_push > 0, True)

print("\n--- seeded image skills (unrepeatable-loss guard) ---")
from nanobot.persistence import seed_skills  # noqa: E402

shipped = repo / "seed-skills"
check("seed tree is committed", shipped.is_dir(), True)
names = sorted(p.name for p in shipped.iterdir() if p.is_dir()) if shipped.is_dir() else []
check(
    "skills shipped in the image",
    names,
    ["agnes-image", "cloudflare-ai-image", "hugging-face-image", "photo-zoom-video"],
)
for name in names:
    check(f"{name} has SKILL.md", (shipped / name / "SKILL.md").is_file(), True)
    scripts = sorted(p.name for p in (shipped / name / "scripts").glob("*.py"))
    check(f"{name} has a runnable script", bool(scripts), True)

with tempfile.TemporaryDirectory() as seed_dir:
    root = Path(seed_dir) / "workspace" / "skills"
    seeded, skipped = seed_skills.seed(shipped, root)
    check("a fresh container gets every skill", sorted(seeded), names)
    again = seed_skills.seed(shipped, root)
    check("a second boot overwrites nothing", again, ([], names))

# Keys must live in the environment, never in the shipped copy.
leaks = [
    f"{p.name}:{m}"
    for p in shipped.rglob("*")
    if p.is_file()
    for m in ("cfut_", "hf_PhG", "sk-Lme", "sk-or-v1-")
    if m in p.read_text(errors="replace")
]
check("no credentials committed in the seeded skills", leaks, [])

entrypoint_text = (repo / "entrypoint.sh").read_text()
seed_at = entrypoint_text.find("nanobot.persistence.seed_skills")
restore_at2 = entrypoint_text.find("nanobot.persistence.bootstrap")
check("skills are seeded after the durable restore", -1 < restore_at2 < seed_at, True)
check("seeding failure is non-fatal", "continuing without it" in entrypoint_text, True)

print("\n--- photo -> zoom video skill (ffmpeg dependency + mirror safety) ---")
dockerfile = (repo / "Dockerfile").read_text()
# The skill is dead weight without the binary, and the loader would hide it as
# unavailable — so the image must ship ffmpeg, not just the script.
check("image installs ffmpeg", "ffmpeg" in dockerfile, True)
apt_line = next((line for line in dockerfile.splitlines() if "apt-get install" in line), "")
check("ffmpeg is on the apt install line", "ffmpeg" in apt_line, True)

zoom_skill = shipped / "photo-zoom-video"
zoom_meta = json.loads(
    (zoom_skill / "SKILL.md").read_text().split("metadata:", 1)[1].strip().splitlines()[0]
)
check(
    "skill declares its binaries",
    zoom_meta["nanobot"]["requires"]["bins"],
    ["ffmpeg", "ffprobe"],
)
zoom_description = (
    (zoom_skill / "SKILL.md").read_text().split("description:", 1)[1].split("\n", 1)[0].lower()
)
check("skill triggers on a French zoom-video request", "vidéo zoom" in zoom_description, True)
check(
    "skill script is executable python",
    (zoom_skill / "scripts" / "zoom_video.py").is_file(),
    True,
)

# Rendered clips share the workspace with durable state, and the archive fails
# wholesale past its cap — so they must be excluded, while their sources are not.
from nanobot.persistence.tree_mirror import DEFAULT_TREE_EXCLUDES  # noqa: E402

for pattern in ("**/*.mp4", "**/*.mov", "**/*.webm"):
    check(f"mirror excludes {pattern}", pattern in DEFAULT_TREE_EXCLUDES, True)
check("mirror still archives photos", "**/*.jpg" not in DEFAULT_TREE_EXCLUDES, True)

with tempfile.TemporaryDirectory() as clip_dir:
    root = Path(clip_dir)
    (root / "output").mkdir(parents=True)
    (root / "config.json").write_text('{"providers": {}}')
    (root / "output" / "source.jpg").write_bytes(os.urandom(30_000))
    for index in range(8):
        (root / "output" / f"clip{index}.mp4").write_bytes(os.urandom(4_000_000))
    mirrored = {
        p.relative_to(root).as_posix()
        for p in TreeArchiveMirror(store=_MemoryStore(), root=root).iter_files()
    }
    check("32 MB of clips are not mirrored", [n for n in mirrored if n.endswith(".mp4")], [])
    check("the source photo is mirrored", "output/source.jpg" in mirrored, True)
    check("config.json is still mirrored", "config.json" in mirrored, True)
check("Dockerfile ships the seed tree", "COPY seed-skills/" in (repo / "Dockerfile").read_text(), True)

# Ordering in entrypoint.sh is load-bearing: a restore that runs after the
# config template is written can never bring back edited provider settings.
entrypoint = (repo / "entrypoint.sh").read_text()
restore_at = entrypoint.find("nanobot.persistence.bootstrap")
template_at = entrypoint.find("cp /app/render-config.json")
check("entrypoint runs the state restore", restore_at > -1, True)
check("state restore precedes the config template copy", -1 < restore_at < template_at, True)
check(
    "entrypoint fails closed when the restore fails",
    "refusing to start" in entrypoint,
    True,
)

print("\n--- platform config reconcile (template fixes reach a restored config) ---")
from nanobot.persistence import reconcile_platform_config as reconcile_mod  # noqa: E402

reconcile_at = entrypoint.find("nanobot.persistence.reconcile_platform_config")
copy_at = entrypoint.find("cp /app/render-config.json")
check("reconcile runs after the config template copy", -1 < copy_at < reconcile_at, True)
check("reconcile failure is non-fatal", "continuing with the stored config" in entrypoint, True)
check(
    "credentials are excluded from the reconcile",
    sorted(reconcile_mod.CREDENTIAL_KEYS),
    ["persistence.supabase.serviceKey", "persistence.supabase.url"],
)

# The exact situation that stranded the previous cadence change: a persisted
# config carrying the old numbers plus live secrets.
with tempfile.TemporaryDirectory() as tmp:
    live_path = Path(tmp) / "config.json"
    stored = json.loads(template.read_text())
    stored["persistence"]["supabase"]["snapshotIntervalS"] = 120
    stored["persistence"]["supabase"]["treeSnapshotIntervalS"] = 300
    stored["persistence"]["supabase"]["url"] = "https://live.supabase.co"
    stored["persistence"]["supabase"]["serviceKey"] = "sb_secret_live"
    live_path.write_text(json.dumps(stored, indent=2))

    applied = reconcile_mod.reconcile(live_path, template)
    merged = json.loads(live_path.read_text())
    check("cadence is corrected on a restored config", sorted(applied), [
        "persistence.supabase.snapshotIntervalS",
        "persistence.supabase.treeSnapshotIntervalS",
    ])
    check("json cadence after reconcile", merged["persistence"]["supabase"]["snapshotIntervalS"], 15)
    check("tree cadence after reconcile", merged["persistence"]["supabase"]["treeSnapshotIntervalS"], 15)
    check(
        "live supabase url preserved",
        merged["persistence"]["supabase"]["url"],
        "https://live.supabase.co",
    )
    check(
        "live service key preserved",
        merged["persistence"]["supabase"]["serviceKey"],
        "sb_secret_live",
    )
    check("second reconcile is a no-op", reconcile_mod.reconcile(live_path, template), {})

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
