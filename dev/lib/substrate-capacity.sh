#!/usr/bin/env bash
# substrate-capacity.sh — the single definition site for "can this substrate actually hold the
# inner-loop cluster". One function per substrate; each declares, in a machine-readable `@covers:`
# line, the cluster-creating command it is the preflight FOR.
#
# WHY THIS IS A LIBRARY AND NOT A COMMENT IN up.sh
#   Every check below was discovered the same way: a cluster refused to work, the error named
#   something else, and hours went into the wrong layer. Each fix was then written into whichever
#   script happened to be in hand, which is how the memory check briefly existed in two files with
#   two different floors. `invariants-gate.py check_cluster_creating_scripts_assert_capacity` fails
#   any script that creates a cluster without calling the preflight that covers it, so there is one
#   floor, one message, and one place to add the next resource when it bites (V-MET-013).
#
# WHY `@covers:` AND NOT A ROSTER IN THE GATE
#   The gate builds the command -> function map by PARSING this file. That is what makes the rule
#   survive a substrate change without an edit on both sides, and it has now been tested by one:
#   on 2026-07-26 the inner loop left Kind, `assert_host_capacity` and its `# @covers: kind create
#   cluster` line were deleted together, the map shrank to one entry, and the gate stayed green
#   against the substrate that replaced it without a line of the CHECK changing. A hardcoded table
#   in the gate would instead have gone quietly green about a pattern nothing uses any more — the
#   failure mode every VACUOUS arm in that file exists to prevent.
#
# THE POINT OF MEASURING A FEW THINGS AND NAMING THE REST
#   A preflight grown one incident at a time only ever measures the PREVIOUS incident. The memory
#   check was written after LSN-026; the very next new cluster failed on inotify while the memory
#   check printed 5758Mi of headroom — a green line that is not neutral, because it actively sends
#   you to look at size when size is fine. That is LSN-027, and it generalizes past the host that
#   taught it: a quota check that passes says nothing about IAM, and reads as "the project is fine".
#   Hence the closing note in each entry point: it says what it checked AND what it did not.
#
# EVERY PROBE READ IS `x="$(...)" || x=""`, AND THAT IS LOAD-BEARING
#   Each check below has a "could not read it — proceed, and here is what to read INSTEAD if the
#   create fails" branch, which is the whole LSN-027 idea: never let an unmeasurable resource send
#   you to the wrong layer. Under a caller running `set -euo pipefail` — which up.sh does, and
#   should — a bare `x="$(cmd | head -1)"` whose command fails does not reach that branch: the
#   assignment fails, `set -e` kills the CALLER, and the exit status is the tool's. Found on the
#   first real run, 2026-07-26: `gcloud compute regions describe` was called with a flag it does
#   not accept, and up.sh exited 2 having printed nothing at all after the APIs line. A preflight
#   that dies silently is strictly worse than no preflight, because the reader now has a script to
#   debug on top of whatever they came for. So every probe is written with the `|| x=""` escape,
#   which makes the assignment a compound command that `set -e` does not act on and routes the
#   failure into the branch written for it.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/substrate-capacity.sh"
#   assert_project_capacity         # prints its findings; exits 2 if the project cannot hold one

# --- knobs, remote substrate ---------------------------------------------------------------------
# Two lines rather than `${PROJECT_ID:-$(gcloud ...)}` for the reason in the header: the fallback
# only runs when PROJECT_ID is unset, which is exactly the case _check_project was written to
# refuse — and a bare substitution there kills the sourcing script before it can say so.
CAP_PROJECT_ID="${PROJECT_ID:-}"
[ -n "$CAP_PROJECT_ID" ] || CAP_PROJECT_ID="$(gcloud config get core/project 2>/dev/null)" || CAP_PROJECT_ID=""
CAP_REGION="${REGION:-us-east4}"
CAP_AR_REPO="${AR_REPO:-kube-agents}"
# 2 x e2-standard-4. Stated as vCPU rather than as a machine type because CPUS is the quota that is
# actually enforced, and the failure it produces is the one worth pre-empting.
CAP_NEED_VCPU="${CAP_NEED_VCPU:-8}"
CAP_REQUIRED_APIS="${CAP_REQUIRED_APIS:-container.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com compute.googleapis.com}"
ALLOW_TIGHT_QUOTA="${ALLOW_TIGHT_QUOTA:-0}"

# _check_project — the project IS the substrate now, and "not set" is its version of "no disk".
# Every gcloud call below would otherwise fail with a different error naming a different resource.
_check_project() {
  if [ -n "$CAP_PROJECT_ID" ]; then
    echo "   project: $CAP_PROJECT_ID · region: $CAP_REGION"
    return 0
  fi
  cat >&2 <<EOF

REFUSING: no GCP project is set, so nothing below can be measured.

  Set one:
      gcloud config set project adamparco-kage
  or pass it for this run:
      PROJECT_ID=adamparco-kage \$0
EOF
  exit 2
}

# _check_apis — a disabled service API is the remote analogue of a missing binary, and it presents
# the way the inotify ceiling did: not as itself. `gcloud container clusters create` against a
# project without container.googleapis.com spends its time and then reports an access error, which
# reads as an IAM problem and sends you to permissions, where everything is already correct.
_check_apis() {
  local enabled missing="" api
  enabled="$(gcloud services list --enabled --project "$CAP_PROJECT_ID" \
    --format='value(config.name)' 2>/dev/null)" || enabled=""
  if [ -z "$enabled" ]; then
    echo "   APIs: could not list enabled services — proceeding, but if cluster creation fails with"
    echo "   an access error, read 'gcloud services list --enabled' before reading IAM."
    return 0
  fi
  for api in $CAP_REQUIRED_APIS; do
    printf '%s\n' "$enabled" | grep -qxF "$api" || missing="$missing $api"
  done
  if [ -z "$missing" ]; then
    echo "   APIs: all required services enabled ($(printf '%s' "$CAP_REQUIRED_APIS" | wc -w | tr -d ' ') checked)"
    return 0
  fi
  cat >&2 <<EOF

REFUSING: required service API(s) not enabled on $CAP_PROJECT_ID:$missing

  Enable them:
      gcloud services enable$missing --project $CAP_PROJECT_ID
EOF
  exit 2
}

# _check_quota — the remote resource that actually runs out. Regional CPUS is consumed by every
# cluster in the region and this project already runs the live install, so the number that matters
# is headroom, not the limit. Over quota, `clusters create` fails PARTWAY: the control plane exists,
# the node pool does not, and a re-run then says "already exists" while up.sh's node-count assert
# reports 0 nodes — a shape that looks like a broken script rather than a full region.
_check_quota() {
  local line limit usage free
  # `--filter` is not a flag `compute regions describe` accepts (it is a LIST flag), so the metric
  # is selected here rather than server-side. That was the first-run defect: gcloud exited 2, the
  # command substitution took the caller down with it, and nothing was printed.
  line="$(gcloud compute regions describe "$CAP_REGION" --project "$CAP_PROJECT_ID" \
    --flatten='quotas[]' --format='value(quotas.metric,quotas.limit,quotas.usage)' 2>/dev/null |
    awk '$1 == "CPUS" {print $2, $3; exit}')" || line=""
  limit="$(printf '%s' "$line" | awk '{printf "%d", $1}')" || limit=0
  usage="$(printf '%s' "$line" | awk '{printf "%d", $2}')" || usage=0
  if [ -z "$line" ] || [ "${limit:-0}" -le 0 ]; then
    echo "   quota: could not read CPUS for $CAP_REGION — proceeding, but if the node pool never"
    echo "   materialises, read 'gcloud container operations list' rather than the cluster."
    return 0
  fi
  free=$((limit - usage))
  echo "   quota: CPUS in $CAP_REGION — ${usage} used of ${limit} · ${free} free (want >= ${CAP_NEED_VCPU})"
  [ "$free" -ge "$CAP_NEED_VCPU" ] && return 0
  [ "$ALLOW_TIGHT_QUOTA" = "1" ] && { echo "   ALLOW_TIGHT_QUOTA=1 — proceeding anyway." >&2; return 0; }
  cat >&2 <<EOF

REFUSING: ${free} free vCPU in $CAP_REGION, want >= ${CAP_NEED_VCPU} (2 x e2-standard-4).

  Free some, or shrink the ask:
      dev/cluster/pause.sh                 # resize an idle dev cluster's node pools to 0
      gcloud container clusters list --project $CAP_PROJECT_ID
  or request more:
      https://console.cloud.google.com/iam-admin/quotas?project=$CAP_PROJECT_ID
  or override deliberately:
      ALLOW_TIGHT_QUOTA=1 \$0

  Do NOT free capacity by touching platform-agent-host. It is the live install, and it is not a
  destructive-test target.
EOF
  exit 2
}

# _check_registry — the images under test are built remotely and deployed BY DIGEST, so a repository
# that cannot be read is not a late inconvenience: nothing the cluster runs is under test without
# it, and the gates would certify the published upstream binary instead (LSN-001, in its remote
# form). Asked here, where the answer is one API call, rather than at the rollout, where it costs a
# cluster bring-up to find out.
_check_registry() {
  if gcloud artifacts repositories describe "$CAP_AR_REPO" --location "$CAP_REGION" \
    --project "$CAP_PROJECT_ID" --format='value(name)' >/dev/null 2>&1; then
    echo "   registry: $CAP_REGION-docker.pkg.dev/$CAP_PROJECT_ID/$CAP_AR_REPO reachable"
    return 0
  fi
  cat >&2 <<EOF

REFUSING: Artifact Registry repository '$CAP_AR_REPO' is not readable in $CAP_REGION.

  Create it:
      gcloud artifacts repositories create $CAP_AR_REPO --repository-format=docker \\
        --location=$CAP_REGION --project=$CAP_PROJECT_ID
  or point at an existing one:
      AR_REPO=<name> REGION=<location> \$0
EOF
  exit 2
}

# @covers: gcloud container clusters create
assert_project_capacity() {
  echo "== project preflight =="
  _check_project
  _check_apis
  _check_quota
  _check_registry
  # Say what this is and is not evidence about. A row of green lines reads as "the project is
  # fine", and that reading is what cost an hour looking at memory while inotify was the limit
  # (LSN-027). The remote substrate has more ways to be not-fine than the host did, not fewer.
  echo "   NOT checked: IAM permissions, org policy, GKE version availability in the zone, quotas"
  echo "   other than CPUS (IN_USE_ADDRESSES, SSD_TOTAL_GB), Cloud Build concurrency. If creation"
  echo "   still fails, read the operation — the resource that ran out is named there:"
  echo "     gcloud container operations list --project $CAP_PROJECT_ID --limit 5"
}

# WHAT WAS HERE, and where the lesson went. `assert_host_capacity` measured two things about a
# laptop: Docker VM memory headroom (LSN-026) and `fs.inotify.max_user_instances` (LSN-027). Both
# were properties of a substrate that left the loop on 2026-07-26, and a preflight that measures a
# host nothing runs on is worse than no preflight — it prints green lines about a machine that is
# not the constraint. The GENERALIZATION is what survives, and it survives here rather than in a
# comment: one definition site, an `@covers:` annotation the gate parses, and a closing note in
# every entry point that says what was NOT measured. Both lessons stay closed against
# `check_cluster_creating_scripts_assert_capacity`, which now polices
# `gcloud container clusters create` for exactly the reason it used to police `kind create cluster`.
# The old bodies are in git history at dev/lib/substrate-capacity.sh, and there is nothing in them
# worth reviving unless a local substrate comes back.
