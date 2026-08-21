#!/bin/sh
dir="$HOME/.nanobot"

# Render deploy path (see render.yaml + render-config.json). Gated on Render's
# automatic RENDER=true env var so local Docker/podman usage is unaffected.
# Initializes the on-disk config from the committed template (wiring secrets via
# ${VAR} env vars, keeping runtime data on the persistent disk) and appends the
# --config flag. Logs each decision so a failed start is diagnosable in Render's
# logs. Privilege dropping is handled below, for every root start (not just here).
if [ "$RENDER" = "true" ]; then
    echo "[entrypoint] Render deploy — starting as $(id)"
    mkdir -p "$dir" || echo "[entrypoint] warning: mkdir $dir failed"
    config="$dir/config.json"
    # Restore durable state BEFORE the config template is written and before
    # nanobot reads anything from disk. This host has no persistent disk, so the
    # whole data dir (config.json with its provider keys, self-installed skills,
    # workspace files, session history, MCP tokens) arrives from the Supabase
    # mirror. Ordering is load-bearing twice over: a restore after the template
    # copy would find config.json present and keep the pristine template
    # forever, and a restore after startup could not affect the config already
    # loaded in memory.
    # Fail closed on error: booting with an empty tree would let the next
    # snapshot overwrite a healthy mirror with nothing.
    if ! python -m nanobot.persistence.bootstrap; then
        echo "[entrypoint] error: durable state restore failed — refusing to start" >&2
        exit 1
    fi
    # Reinstall any missing image-generation skill from the image. Runs after the
    # restore so a mirrored (possibly agent-edited) copy always wins, and never
    # overwrites what is already on disk. A failure here is not fatal: the agent
    # is still usable without these skills.
    python -m nanobot.persistence.seed_skills || \
        echo "[entrypoint] warning: skill seeding failed — continuing without it"
    # Initialize config only when it does not already exist, so WebUI/provider
    # settings edited at runtime survive restarts — they come back through the
    # restore above, which is what makes this branch a genuine first-boot path.
    if [ ! -f "$config" ]; then
        echo "[entrypoint] initializing $config from render-config.json"
        cp /app/render-config.json "$config" || echo "[entrypoint] warning: cp config failed"
    else
        echo "[entrypoint] existing $config found — leaving it in place"
    fi
    # The restored config wins by design, which strands deployment-owned fixes in
    # the template: the mirror cadence was tightened here and the live instance
    # kept the old numbers. Re-apply just those platform keys (never credentials,
    # never user-edited sections) so infrastructure changes actually ship. The
    # merged config is validated before it replaces anything, so a failure here
    # leaves the working config untouched and is not fatal.
    python -m nanobot.persistence.reconcile_platform_config || \
        echo "[entrypoint] warning: platform config reconcile failed — continuing with the stored config"
    set -- "$@" --config "$config"
fi

# Drop privileges whenever the container starts as root. Render mounts the
# persistent disk root-owned, and a plain `docker run` also defaults to root now,
# so this covers both. Chown the data dir so the non-root user can write it, then
# re-exec as nanobot. Fail closed: if the privilege drop cannot be performed,
# exit rather than run the agent as root.
if [ "$(id -u)" = "0" ]; then
    chown -R nanobot:nanobot "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"
    if setpriv --reuid=nanobot --regid=nanobot --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to nanobot via setpriv"
        exec setpriv --reuid=nanobot --regid=nanobot --init-groups nanobot "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

# Already non-root: make sure the data dir is writable before starting.
if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.nanobot
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi

exec nanobot "$@"
