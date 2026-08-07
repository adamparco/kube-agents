#!/bin/sh
set -e

export TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
export HERMES_HOME="$TARGET_DIR"
export INSTALL_DIR="/opt/hermes"

# Pre-export AGENT_BROWSER_EXECUTABLE_PATH before running stage2-hook.sh.
# Why: Upstream stage2-hook.sh scans for Playwright's Chromium binary and
# attempts to export it to s6-overlay by creating /run/s6/container_environment/.
# In unprivileged Kubernetes Pods (RunAsNonRoot: true), /run is read-only or
# root-owned, so stage2-hook.sh crashes on `mkdir -p /run/s6/` with Permission denied.
# By pre-exporting AGENT_BROWSER_EXECUTABLE_PATH here, stage2-hook.sh detects
# [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] is false and cleanly skips writing to /run/s6/.
if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/hermes/.playwright" ]; then
    export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/hermes/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
fi

# 1. Execute upstream container initialization natively (inherits 100% of upstream updates)
if [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    /opt/hermes/docker/stage2-hook.sh
fi

# Which container of the pod this is. The operator stamps PLATFORM_AGENT_ROLE
# `sidecar` on platform-agent-dashboard and leaves it unset on the agent itself,
# so an image running anywhere else — plain docker, the kustomize bases, a
# cluster profile — is the primary by default and behaves exactly as before.
#
# This gates only what genuinely admits one owner per pod: the session KV server
# (one process may hold :8699) and the OTel service-name stamp (the dashboard
# carries no OTEL_SERVICE_NAME, so its run of step 4 DELETED the name the agent
# had just written — the observed `resource_attributes: {}`). Shared-state
# writes are serialised by the lock below instead, not skipped, so a sidecar
# still leaves the volume usable on its own.
if [ "$PLATFORM_AGENT_ROLE" = "sidecar" ]; then
    IS_BOOTSTRAP_PRIMARY=0
else
    IS_BOOTSTRAP_PRIMARY=1
fi

# 1.5 Serialise everything below that writes to $TARGET_DIR.
#
# More than one container in this pod runs this entrypoint against the same
# ReadWriteOnce volume. The operator gives platform-agent-dashboard `args`
# ([hermes dashboard]) but no `command:`, so it inherits this ENTRYPOINT and
# performs the identical bootstrap, concurrently, on the identical paths.
#
# Observed damage, not theoretical: both containers abort step 2.5 with
# `shutil.Error` from the plugin copytree, each naming DIFFERENT files (one
# `hermes_otel/website/README.md`, the other `hermes_otel/docker-compose/...`)
# on the same boot — the signature of a write-write race rather than a
# permission problem. Two concurrent overlays of cron/jobs.json can likewise
# interleave a read-merge-write and lose one side's run history.
#
# Nothing downstream was corrupted only because the two partial copies happened
# to union to a complete tree. That is luck: --plugins is passed in step 2.5
# alone, which is gated on first scaffold, so a genuine gap would persist for
# the life of the PVC.
#
# A lock rather than "only the primary bootstraps", deliberately: each container
# still leaves the volume ready by itself, so there is no start-order dependency
# and no wait-for-peer timeout to tune. The loser of the race runs second and
# finds every step already idempotently satisfied. Steps that are singletons
# rather than shared-state writes (the session KV server, the OTel service-name
# stamp) are guarded by IS_BOOTSTRAP_PRIMARY instead — a lock cannot serialise a
# port bind held for the container's lifetime.
#
# Absent flock, or an unwritable volume, this degrades to today's behaviour
# rather than refusing to start.
BOOTSTRAP_LOCK="$TARGET_DIR/.bootstrap.lock"
BOOTSTRAP_LOCK_FD=""
if command -v flock >/dev/null 2>&1; then
    mkdir -p "$TARGET_DIR" 2>/dev/null || true
    # `touch`, not `: >>"$LOCK"`. A redirection that fails on a POSIX *special*
    # builtin — and `:` is one — exits a non-interactive shell outright, before
    # the `2>/dev/null` on the same line is even installed. Under dash (Debian's
    # /bin/sh) an unwritable $TARGET_DIR then killed the container at this line
    # instead of falling through to the unlocked path. Proving writability with
    # an external command first also makes the `exec` below safe, which would
    # abort the shell the same way.
    if touch "$BOOTSTRAP_LOCK" 2>/dev/null; then
        exec 9>>"$BOOTSTRAP_LOCK"
        BOOTSTRAP_LOCK_FD=9
        # Bounded: a peer wedged mid-bootstrap must not hold this container at
        # the starting line forever. Timing out and proceeding is the current
        # behaviour, so the worst case is no worse than before the lock.
        flock -w 300 9 || echo "WARN: timed out waiting for the $TARGET_DIR bootstrap lock; proceeding concurrently with the peer container" >&2
    fi
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 2a. Force-sync the image-managed default-profile files so they ALWAYS track the
# image, not the persistent PVC. The update-only copy above (cp -u) can skip them:
# anything that rewrites one at runtime bumps its mtime, so on the next image roll
# cp -u sees the PVC copy as "newer" and never overwrites it — leaving a stale
# persona live. These files are image-owned (not runtime state), so overwrite them
# unconditionally.
#
# config.yaml is NOT in this list, and is not in /opt/defaults at all: it is the one
# file here the agent itself writes to (`/sethome`'s home channel, the monitoring
# install id, saved slash-command preferences), so force-copying it discarded all of
# that on every start. Step 2d rebuilds it instead, from the image template, the
# operator's overlay and the runtime's own edits.
if [ -d "/opt/defaults" ]; then
    for f in SOUL.md AGENTS.md CAPABILITIES.md; do
        [ -f "/opt/defaults/$f" ] && cp -f "/opt/defaults/$f" "$TARGET_DIR/$f" 2>/dev/null || true
    done
fi

# 2b. Force-sync the shared scripts, for the reason step 2a gives for the default
# profile's files: they are image-owned, never runtime state, and `cp -ru` above can skip
# them. It skips whenever the destination looks newer, which covers both a rollback to an
# older image and any build that stamps deterministic file timestamps — in the second case
# a new script never lands at all. The runtime paths that scaffold a cluster profile run
# from here (cluster_agent_profile.py and what it imports), and a stale copy of those
# silently drops the overlay merge and the plugin links for every cluster onboarded after
# the pod started. Extra files already on the PVC are left alone.
#
# Reported, not swallowed, for the reason step 2.7 gives: a silent no-op here IS the bug
# this step exists to prevent, and it surfaces far away — as a cluster agent that quietly
# runs untuned, or without the plugin it was given.
if [ -d "/opt/defaults/scripts" ]; then
    mkdir -p "$TARGET_DIR/scripts"
    cp -rf /opt/defaults/scripts/. "$TARGET_DIR/scripts/" \
        || echo "WARN: could not refresh $TARGET_DIR/scripts from the image; runtime profile scaffolding may run stale code" >&2
fi

# Where the operator mounts its config ConfigMap (the operator's profileOverlayDir), and
# the two scripts that consume what it holds. Resolved once, here, because step 2d and
# step 2.7 both need them.
#
# Prefer the IMAGE copy of a script over the PVC copy. Step 2 syncs /opt/defaults with
# `cp -ru`, which skips a destination that looks newer — the same trap step 2a documents
# — so a PVC copy can outlive the image it came from. These scripts decide what every
# profile's config ends up containing, so they must track the image.
OVERLAY_DIR="/opt/agent-config"
OVERLAY_SCRIPT="/opt/defaults/scripts/profile_overlay.py"
[ -f "$OVERLAY_SCRIPT" ] || OVERLAY_SCRIPT="$TARGET_DIR/scripts/profile_overlay.py"
DEFAULT_CONFIG_SCRIPT="/opt/defaults/scripts/default_profile_config.py"
[ -f "$DEFAULT_CONFIG_SCRIPT" ] || DEFAULT_CONFIG_SCRIPT="$TARGET_DIR/scripts/default_profile_config.py"

# 2d. Rebuild the default profile's config.yaml from the image template, the operator's
# overlay, and the runtime's own edits.
#
# This is the default profile's equivalent of step 2.7, and it exists because neither of
# the two mechanisms it replaces worked:
#
#   * The operator subPath-mounted its rendering over $TARGET_DIR/config.yaml. A subPath
#     mount is a read-only mount POINT, so the agent could no longer save anything to its
#     own config: `/sethome` failed with EACCES (os.replace over a mount point gives
#     EBUSY, and the copyfile fallback then gives EACCES), the monitoring policy could
#     not persist `monitoring.install_id`, and every saved slash-command preference was
#     lost. The error the user saw had the path scrubbed out of it by the Slack egress
#     sanitiser, so it read "Permission denied: ''".
#   * Step 2a then force-copied the image's config over that same path anyway, so the
#     operator's rendering never reached the agent at all — 12 keys the CR asked for,
#     including platforms.slack.enabled and the model endpoint, were simply absent from
#     the live file.
#
# So the file is now an ordinary writable file on the PVC, and this step reconciles it
# once per start: image + operator overlay is the baseline, and the previous baseline
# recorded beside it is what lets the runtime's own edits be told apart and carried
# across. default_profile_config.py has the full rationale and the per-key rules.
#
# The image's copy lives at /opt/chat-template, NOT in /opt/defaults, precisely so that
# step 2's `cp -ru` cannot reach it. That copy is mtime-driven and races an image roll —
# and losing the live file to it, in either container, before this step reads it would
# discard exactly the runtime state this step exists to keep.
#
# PRIMARY ONLY, not merely serialised by the step-1.5 lock. The lock's own rule is that
# a step is lockable when each container can leave the volume ready by itself; this one
# fails that test because its INPUTS are per-container. platform-agent-dashboard does not
# mount platform-agent-config-vol at all (the operator's dashboardVolumeMounts), so
# $OVERLAY_DIR does not exist there and the sidecar would compute a baseline of pure
# image config and overwrite the primary's correct one. Same reasoning as step 4.
#
# UNCONDITIONAL, never mtime-gated: this is now the only path by which an image roll or a
# CR edit reaches the front door's config.
CHAT_TEMPLATE_CONFIG="/opt/chat-template/config.yaml"

# Fresh volume: lay the image's copy down before anything can read it. The rebuild below
# is the primary's alone, and the dashboard sidecar must not come up against a missing
# config, fall back to Hermes' built-in defaults, and save those over the top.
if [ ! -f "$TARGET_DIR/config.yaml" ] && [ -f "$CHAT_TEMPLATE_CONFIG" ]; then
    cp "$CHAT_TEMPLATE_CONFIG" "$TARGET_DIR/config.yaml" \
        || echo "WARN: could not seed $TARGET_DIR/config.yaml from $CHAT_TEMPLATE_CONFIG" >&2
fi

if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$DEFAULT_CONFIG_SCRIPT" ] && [ -f "$CHAT_TEMPLATE_CONFIG" ]; then
    # Reported, not swallowed. A silent no-op here reproduces the bug this step exists to
    # remove, and the symptom surfaces far away — as a front door talking to the wrong
    # model endpoint, or as a home channel that quietly forgets itself on every restart.
    "$INSTALL_DIR/.venv/bin/python3" "$DEFAULT_CONFIG_SCRIPT" \
        --config "$TARGET_DIR/config.yaml" \
        --image "$CHAT_TEMPLATE_CONFIG" \
        --overlay "$OVERLAY_DIR/profile-default.overlay.yaml" \
        || echo "WARN: could not rebuild $TARGET_DIR/config.yaml; the Chat Agent is running the previous file, so operator settings (model endpoint, platforms, approvals, toolsets) may not have applied" >&2
fi

# The image's own copy of the scaffolder, never the volume's. Step 2 seeds
# $TARGET_DIR/scripts with `cp -u`, which SKIPS any file the PVC holds a newer
# mtime for — the same trap step 2a exists to work around for config.yaml. This
# is the one script in the pod whose job is to make the volume track the image,
# so it is the one script that must not be read back off the volume: last
# release's scaffolder running this release's template is how a partial upgrade
# looks like a successful one. (Step 2b force-syncs the rest of the scripts for
# the same reason; this one cannot wait for that to have worked.)
SCAFFOLD="/opt/defaults/scripts/profile_scaffold.py"

# 2c. Merge the image's default-profile cron roster into the volume's.
#
# Step 2's `cp -ru` cannot do this and never could: the scheduler rewrites
# $TARGET_DIR/cron/jobs.json on every tick, so the volume's copy is always newer
# than the image's and the update-only copy always skips it. On a fresh PVC the
# roster lands with everything else and the gap is invisible; on every existing
# PVC a newly shipped job simply never arrives. That is how `profile-cron-tick`
# — the job that ticks every OTHER profile's cron store — would have shipped to
# nobody.
#
# Same merge, same reasons, as the platform profile's roster in step 2.6: the
# file is image-owned definitions and live scheduler state at once, so the image
# wins every key it ships and the volume keeps every key it does not (see
# merge_cron_store). `--home` overlays the default profile in place, because that
# profile IS $TARGET_DIR and has no entry under profiles/ to name.
#
# `--cron-jobs` names the one id this merge may force, and that restriction is
# load-bearing: two of the jobs in this roster DELETE THEMSELVES. Once the
# onboarding report is delivered, bootstrap_delivery.py calls remove_job on the
# scan/delivery pair, and an unfiltered merge would put both back on the next
# restart — polling once a minute forever to no-op on a marker. Add an id here
# only when the image genuinely owns that job's definition on a live volume.
if [ -f "/opt/defaults/cron/jobs.json" ] && [ -f "$SCAFFOLD" ]; then
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --home "$TARGET_DIR" \
        --template /opt/defaults \
        --items "cron" \
        --cron-jobs "profile-cron-tick" \
        >/dev/null || echo "WARN: default-profile cron merge failed; jobs added by this image will not run" >&2
fi

# 2.5 Scaffold the Platform Agent specialist profile (idempotent).
# The `default` profile is the front-door Chat Agent (synced above). Today's
# Platform Agent runs as a separate named `platform` profile so the Chat Agent
# can route to it. Its persona/config/skills are baked at /opt/platform-template;
# executable scripts stay in the shared $TARGET_DIR/scripts and are not overlaid.
#
# Gated on profile.yaml — written by `hermes profile create`, shipped by no template —
# rather than on the directory. A directory is not evidence of a scaffold: the kubelet
# creates a mounted volume's mount point before this script runs, so anything mounted
# under profiles/<name>/ brings the directory into being on the PVC first. Targeted
# plugins are mounted outside $HERMES_HOME for exactly that reason (step 2.65), and this
# gate is the belt to that pair of braces: on a PVC already carrying such a directory,
# the scaffold now still runs instead of being skipped forever.
PLATFORM_TEMPLATE="/opt/platform-template"
if [ -d "$PLATFORM_TEMPLATE" ] && [ ! -f "$TARGET_DIR/profiles/platform/profile.yaml" ] && [ -f "$SCAFFOLD" ]; then
    PLATFORM_DESC="Platform Agent: fleet-wide GKE architecture, cluster lifecycle/provisioning, multi-tenancy, and the GitOps write path (Pull Requests). Owns per-cluster agent lifecycle."
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --plugins /opt/defaults/plugins \
        --description "$PLATFORM_DESC" || echo "WARN: platform profile scaffold failed; continuing" >&2
fi
# Point the platform profile's home-relative `scripts/` at the shared scripts dir
# (executable scripts are shared across profiles, not copied per-profile). Self-heal
# on every start. Cluster agents use absolute /opt/data/scripts paths and need no link.
# Requires evidence that the directory is a profile at all — profile.yaml from `hermes
# profile create`, or a config.yaml from a profile built before that marker existed.
# Putting a symlink inside a bare mount point would leave content that the skeleton
# cleanup then refuses to remove, wedging the scaffold; gating on the marker ALONE would
# instead strip a legacy profile of its scripts link, which nothing else restores.
if { [ -f "$TARGET_DIR/profiles/platform/profile.yaml" ] || [ -f "$TARGET_DIR/profiles/platform/config.yaml" ]; } \
    && [ -d "$TARGET_DIR/scripts" ]; then
    ln -sfn "$TARGET_DIR/scripts" "$TARGET_DIR/profiles/platform/scripts" 2>/dev/null || true
fi

# 2.6 Force-sync the image-managed persona and config files of the specialist
# profiles so they ALWAYS track the image, not the persistent PVC — the same
# guarantee step 2a gives the default profile. The scaffold in 2.5 only runs when
# a profile is ABSENT, so without this an existing platform/cluster profile on
# the PVC keeps stale personas after an image roll.
#
# The platform profile also force-syncs config.yaml, the cluster profiles do NOT,
# and that asymmetry is deliberate:
#   - The platform config.yaml is entirely image-owned — built at image build
#     time by merging the shared defaults with the platform overlay. `hermes
#     profile create` emits no config.yaml, and nothing writes to
#     profiles/platform/config.yaml at runtime (step 3's otel injection targets
#     only the default profile; the platform template already enables
#     hermes_otel). Without syncing it, an image that changes the platform's
#     toolsets or plugins has no effect on any existing deployment.
#   - A cluster config.yaml is identity-stamped at scaffold time with that
#     cluster's `cluster_identity` block (project/cluster/location), so it is
#     runtime state. Overwriting it from the template would strip the record
#     cluster_agent_reconcile.py matches a profile to its cluster by, and the
#     reconciler would then scaffold a duplicate profile it can never prune.
#     (KUBECONFIG is not in this file — it is pinned in the profile's .env by
#     cluster_agent_profile.py:_pin_kubeconfig_env.)
#
# Profile identity is NOT at risk either way: `hermes profile create` records the
# name and description in profiles/<name>/profile.yaml, a separate file that no
# template ships, so it is never overwritten here. Per-profile runtime state
# (USER.md, memory/, sessions/) is likewise left untouched.
#
# The sync goes through profile_scaffold.py --items rather than a `cp -f` loop
# because the list is no longer files-only: cron/, skills/, and governance/ carry
# the machinery CAPABILITIES.md advertises. `[ -f ]` is false for a directory, so
# naming them in a shell loop would be a silent no-op — an upgraded install would
# take the new CAPABILITIES.md and none of what it describes. --items copies each
# entry with copytree(dirs_exist_ok=True), which handles both. The profile already
# exists here, so the scaffold's `hermes profile create` is a no-op and only the
# overlay runs.
#
# --plugins is passed here as well as in step 2.5, for the reason this whole step
# exists: 2.5 runs on first scaffold only, so a plugin the image adds or changes
# after the PVC was created would otherwise never arrive, and a plugin copy that
# failed part-way through would stay half-copied for the volume's lifetime. The
# copy adds and overwrites without pruning, so plugin-owned runtime state on the
# volume — hermes_otel's live.db and the rest — is not in the source tree and
# survives. Targeted plugin volumes are linked in afterwards by step 2.65.
#
# cron/jobs.json is the one entry that is merged rather than replaced, inside
# profile_scaffold.py. It is image-owned and runtime state in the same file: the
# schedules, prompts and `enabled` flags ship in the image, but the scheduler
# writes each job's run history back into it and the operator can add jobs of
# its own. Copying it wholesale erased both on every pod restart, losing the
# operator's jobs and re-firing one-shots (an erased `last_run_at` is an erased
# already-ran guard) while leaving recurring jobs to skip a late run instead of
# catching it up. The merge is per key — the image wins every key it ships, the
# volume keeps every key it does not — so flipping `enabled` to false in the
# image still disables a watchdog.
#
# Known limit: the overlay adds and overwrites, it never prunes. A skill or SOP
# dropped from the image stays on the PVC until an operator removes it by hand.
# That is the deliberate trade — this path must not start silently deleting from
# a user's volume — not an oversight.
# Gated on profile.yaml, not on the directory: a bare mount point is not a profile, and
# dressing one in a persona and a config makes it indistinguishable from a real profile at
# the next start — which is how a half-built profile used to become permanent.
if [ -f "$TARGET_DIR/profiles/platform/profile.yaml" ] && [ -d "$PLATFORM_TEMPLATE" ] && [ -f "$SCAFFOLD" ]; then
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --plugins /opt/defaults/plugins \
        --items "config.yaml SOUL.md AGENTS.md CAPABILITIES.md cron skills governance" \
        >/dev/null || echo "WARN: platform profile force-sync failed; continuing" >&2
fi
CLUSTER_TEMPLATE="/opt/cluster-template"
if [ -d "$CLUSTER_TEMPLATE" ]; then
    for d in "$TARGET_DIR"/profiles/cluster-*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
        for f in SOUL.md AGENTS.md CAPABILITIES.md; do
            [ -f "$CLUSTER_TEMPLATE/$f" ] && cp -f "$CLUSTER_TEMPLATE/$f" "$d/$f" 2>/dev/null || true
        done
        # Targeted self-heal: drop `memory.provider` from cluster configs already
        # on the PVC. The template no longer sets it (multiuser_memory scopes by
        # gateway user identity, which a dispatcher-spawned worker never has), but
        # cluster config.yaml is NOT force-synced above — it is identity-stamped
        # with `cluster_identity`, the record cluster_agent_reconcile.py reads to
        # match a profile to its cluster. (KUBECONFIG is pinned separately, in the
        # profile's .env by cluster_agent_profile.py:_pin_kubeconfig_env.) So
        # remove just this one key and leave everything else, rather than
        # overwriting the file.
        #
        # The rewrite goes through a temp file and os.replace: a torn write here
        # would drop `cluster_identity`, and reconcile then treats the profile as
        # unidentifiable — it scaffolds a duplicate AND stops pruning the orphan.
        # Errors are reported, not swallowed: a silent no-op is the exact failure
        # mode this whole change exists to fix.
        if [ -f "$d/config.yaml" ] && [ -w "$d/config.yaml" ]; then
            "$INSTALL_DIR/.venv/bin/python3" -c "import os, sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {}; m = c.get('memory'); sys.exit(0) if not isinstance(m, dict) or 'provider' not in m else None; m.pop('provider'); t = p.with_name(p.name + '.tmp'); t.write_text(yaml.safe_dump(c)); os.replace(t, p)" "$d/config.yaml" \
                || echo "WARN: failed to strip memory.provider from $d/config.yaml; this cluster agent keeps an inert provider" >&2
        fi
    done
fi

# 2.65 Link profile-targeted plugin image volumes into their profile homes.
#
# The operator mounts a plugin with spec.targetProfile at /opt/agent-plugins/<profile>/<plugin>,
# outside $HERMES_HOME, and this links it to profiles/<profile>/plugins/<plugin> where Hermes
# resolves a profile's plugins from. Mounting it there directly is what the kubelet cannot be
# allowed to do: it creates the mount point before this script runs, which brings
# profiles/<profile> into existence on the PVC ahead of the scaffold and permanently convinces
# every "is this profile built?" check that it is. The whole failure mode is written up in
# deploy/shared/profile_plugins.py.
#
# Runs after 2.5/2.6 so the profile home exists. Cluster profiles scaffolded later, at runtime,
# are linked by cluster_agent_profile.create_profile instead.
#
# Prefer the IMAGE copy of the script over the PVC copy, for the reason step 2.7 documents.
PLUGIN_LINK_SCRIPT="/opt/defaults/scripts/profile_plugins.py"
[ -f "$PLUGIN_LINK_SCRIPT" ] || PLUGIN_LINK_SCRIPT="$TARGET_DIR/scripts/profile_plugins.py"
if [ -f "$PLUGIN_LINK_SCRIPT" ]; then
    # --mount-root is deliberately not passed: the path is the script's own default, and
    # the operator's pluginProfileMountRoot is the other end of it. A third copy here
    # would be the one that silently keeps pointing at the old location.
    "$INSTALL_DIR/.venv/bin/python3" "$PLUGIN_LINK_SCRIPT" --hermes-home "$TARGET_DIR" \
        || echo "WARN: linking targeted plugin volumes failed; plugins targeting a named profile will not load" >&2
fi

# 2.7 Merge operator-rendered per-profile config overlays.
#
# An AgentPlugin with spec.targetProfile is linked into profiles/<name>/plugins/<plugin>,
# but a mounted plugin is inert until it is listed in that profile's plugins.enabled:
# Hermes only calls register(ctx) — and therefore ctx.register_skill() — for enabled
# plugins. The operator cannot write the profile's config.yaml directly (step 2.6
# force-syncs it from the image, and the operator has no copy of the image-built merge
# to reproduce), so it emits an overlay per profile and this step merges it in.
#
# ORDERING IS LOAD-BEARING: this must run AFTER step 2.6, or the force-sync overwrites
# the merge and every targeted plugin silently goes missing again.
#
# The merge itself lives in profile_overlay.py so it can be unit tested, and because it
# is more than a merge: it records what it applied so a withdrawn overlay can be undone.
# Cluster profiles are NOT force-synced (their config.yaml carries the cluster_identity
# stamp), so without that, removing tuning from the CR would leave every cluster agent
# running the old limits forever.
#
# Failures are reported, not swallowed: a silent no-op here reproduces exactly the bug
# this step exists to prevent, and the symptom surfaces far away — as "Unknown skill(s)"
# in a worker, or as an agent that improvises without the skill it was told to use.
#
# $OVERLAY_DIR and $OVERLAY_SCRIPT are resolved above step 2d, which needs them too.
#
# Gated on $OVERLAY_DIR existing, not merely on the script existing. Only the agent
# container mounts platform-agent-config-vol; in the dashboard sidecar the directory is
# absent, overlays_for() finds no files, and apply_overlay reads that as "the operator
# withdrew the overlay" and deletes every profile's last-applied record. A container that
# cannot see what the operator rendered must not get to decide what the operator said.
if [ -f "$OVERLAY_SCRIPT" ] && [ -d "$OVERLAY_DIR" ]; then
    # Every profile directory is reconciled — including ones with no overlay, so a
    # withdrawn overlay is undone rather than left applied. Which files apply to a given
    # profile is resolved by name inside the script (profile_overlay.overlays_for): a
    # `cluster-*` profile takes the cluster class overlay AND its own profile-<name> one,
    # if a plugin targets that specific cluster. Matching only the class overlay here is
    # what left such a plugin mounted but never enabled.
    for d in "$TARGET_DIR"/profiles/*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
        name=$(basename "$d")
        "$INSTALL_DIR/.venv/bin/python3" "$OVERLAY_SCRIPT" --profile-dir "$d" --overlay-dir "$OVERLAY_DIR" \
            || echo "WARN: overlay sync failed for profile '$name'; settings it carries will not apply" >&2
    done

    # Warn when an overlay names a profile that does not exist. The operator cannot
    # validate spec.targetProfile — profiles are scaffolded here at startup, not by the
    # operator — so this is the only place a typo becomes visible. A `cluster-*` name is
    # reported differently: those profiles appear when their cluster is onboarded, and
    # cluster_agent_profile.create_profile applies the overlay then, so a missing one is
    # ordinary rather than a mistake.
    for overlay in "$OVERLAY_DIR"/profile-*.overlay.yaml; do
        [ -f "$overlay" ] || continue
        base=$(basename "$overlay"); name=${base#profile-}; name=${name%.overlay.yaml}
        [ -d "$TARGET_DIR/profiles/$name" ] && continue
        case "$name" in
            # The default profile IS $TARGET_DIR and has no entry under profiles/, so the
            # directory test above can never find it. Step 2d applied its overlay in place.
            default)   continue ;;
            cluster-*) echo "NOTE: overlay $base names cluster profile '$name', which is not scaffolded yet; it applies when that cluster is onboarded" >&2 ;;
            *)         echo "WARN: overlay $base names profile '$name', which does not exist; plugins targeting it will not load" >&2 ;;
        esac
    done
fi

# Step 3 used to force `hermes_otel` into the default profile's plugins.enabled here. It
# is gone: both of step 2d's inputs already list it (agents/chat/config.yaml's
# plugins.enabled and the operator's defaultProfilePlugins), so it was a no-op write —
# and a non-atomic one, on a file step 2d has just staged-and-renamed into place.

# 4. Inject dynamic OpenTelemetry service name (if writable)
#
# The write is the primary's alone. This file is shared through the PVC but the
# value is per-container, and the sidecar's value is "none" — so letting the
# sidecar run this turns the agent's service.name into an empty
# resource_attributes map, which is what the deployed pod was observed doing.
# The compat symlink below is per-container ($HOME differs) and must still run
# in both.
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ]; then
        "$INSTALL_DIR/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true
    fi

    # hermes-otel resolves config below ~/.hermes even when HERMES_HOME points
    # elsewhere. Expose the generated config at both locations.
    OTEL_CONFIG="$TARGET_DIR/plugins/hermes_otel/config.yaml"
    OTEL_COMPAT_CONFIG="$HOME/.hermes/plugins/hermes_otel/config.yaml"
    mkdir -p "$(dirname "$OTEL_COMPAT_CONFIG")"
    if [ ! "$OTEL_CONFIG" -ef "$OTEL_COMPAT_CONFIG" ]; then
        ln -sf "$OTEL_CONFIG" "$OTEL_COMPAT_CONFIG"
    fi
fi

# Everything that writes shared volume state is done; release the bootstrap lock
# before starting anything long-lived, so a peer container is not blocked behind
# this one for the life of the pod. Closing the fd releases it too, but do both
# explicitly — the fd is inherited across the `exec` in step 6 otherwise.
if [ -n "$BOOTSTRAP_LOCK_FD" ]; then
    flock -u 9 2>/dev/null || true
    exec 9>&-
fi

# 5. Start background microservices (FastAPI proxy)
#
# Primary only: this binds a fixed port in the pod's shared network namespace,
# and the sidecar's copy lost the race with `[Errno 98] address already in use`
# every boot while both wrote the same log file, interleaved. The port is what
# both containers reach it on, so one server serves the pod.
mkdir -p "$TARGET_DIR/logs"
if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$TARGET_DIR/scripts/session_kv_server.py" ]; then
    echo "Starting Session KV server on port 8699..."
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 0.0.0.0 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi

# 5.5. The default kubectl context is NOT established here. `gcloud` in this
# container is the credential-proxy shim, so get-credentials would execute in
# the sidecar and write the sidecar's kubeconfig, not ours — and it is rejected
# outright, because the steps above run from a working directory outside
# CREDENTIAL_PROXY_WORKSPACE_ROOT (step 6 moves into it, but only for the agent
# process it execs). The sidecar bootstraps its own context from
# CREDENTIAL_PROXY_BOOTSTRAP_COMMAND (see buildCredentialProxyEnv in the
# operator), which runs inside the workspace root before the proxy serves any
# request. The event-watcher does not need a copy either: it reads
# /var/run/event-watcher/watcher.config and falls back to its in-cluster config
# when that file is absent, which it always is.

# 6. Execute primary process from inside the shared workspace.
#
# The image inherits WORKDIR /opt/hermes from the upstream base, and every
# credentialed CLI in this container (kubectl, gcloud, gh, git) is a PATH shim
# for credential_proxy_client.py. That client posts `"cwd": os.getcwd()` on
# every request unconditionally, and the proxy refuses any cwd outside
# CREDENTIAL_PROXY_WORKSPACE_ROOT — which the operator sets to this same
# $TARGET_DIR. Launched from /opt/hermes, therefore, a plain `kubectl version
# --client` fails with "working directory is outside the shared workspace"
# before it runs, purely because of where the process was started.
#
# The cwd is the only lever that reaches every caller. Hermes resolves the
# terminal and execute_code working directories from a ladder that ends at
# os.getcwd(), and for the `local` backend the CLI *overwrites* any configured
# terminal.cwd with os.getcwd() outright ("Local backend: always os.getcwd().
# Use `cd /dir && hermes` to control it."), so a config key cannot fix this —
# and kanban workers, which the dispatcher spawns as child processes, inherit
# whatever cwd the agent was started with.
#
# Guarded rather than unconditional: `set -e` would abort the container on a
# missing directory, and a shell whose cd fails silently continues in the old
# one. $TARGET_DIR is created in step 2 and written throughout, so the warning
# is a canary for a broken mount rather than an expected path.
if ! cd "$TARGET_DIR"; then
    echo "WARN: could not enter $TARGET_DIR; credentialed CLIs (kubectl/gcloud/gh/git) will be refused by the credential proxy as out-of-workspace" >&2
fi

exec "$@"
