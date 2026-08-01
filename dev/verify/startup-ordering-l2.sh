#!/usr/bin/env bash
# V-RUN-005 at L2 — "Startup ordering is safe both directions; broker-first and agent-first both
# converge" (09 §6.8, the row reads verbatim:
#
#     | V-RUN-005 | Startup ordering is safe both directions; broker-first and agent-first both
#       converge | 08 §2.4 | L2 | 9 |
#
# THE LEVEL IS L2 AND ONLY L2, AND THAT IS THE FIRST THING TO SAY ABOUT THIS FILE.
#   Every other startup-ordering claim in the corpus is answered by a lint or by a render golden —
#   `agent_manifests_test.go` asserts `wait-for-broker` is prepended, `broker_manifests_test.go`
#   asserts its flags, and `waitforbroker_test.go`'s TestRunWaitForBrokerTimesOutIntoObserveAndReport
#   asserts the timeout branch writes `unavailable`. Those are three good results and none of them is
#   this row. 09 §6.8 grades V-RUN-005 at L2 because the property is a claim about what a real
#   kubelet, a real init container and a real controller do to each other IN TIME, and the one thing
#   a process-local test cannot manufacture is an ordering. A hermetic arm here would be a false
#   green: it would re-assert the shape and record the ordering as proven.
#
# WHAT 08 §2.4 SAYS, verbatim (08 §2.4:171-176):
#
#     "Startup ordering is safe in both directions. The agent pod runs a `wait-for-broker` init
#     container that polls the broker's `/healthz` with a bounded timeout. On success the agent
#     starts with its write tool surface registered. On timeout it starts anyway, in
#     observe-and-report mode — a broker outage must not blind the fleet… A broker that starts
#     before its agent simply has no caller; it never initiates work."
#
# WHAT 08 §7 ASKS FOR, verbatim (08 §7:661-664) — three arms, and this suite runs all three:
#
#     "(a) Create an `Agent` and delay the broker's image pull: the agent pod starts in
#     observe-and-report mode after the init-container timeout and does not crash-loop. (b) Start
#     the broker first with no agent: it serves `/healthz`, initiates nothing, and logs no
#     envelopes. (c) `Ready` on the CR requires both `AgentReady` and `BrokerReady`."
#
# THE DIGEST CONSTRAINT, verbatim (docs/build/phase-9.md:7622-7624), which is why this file reads
# imageIDs and refuses to proceed without them:
#
#     "V-RUN-001/002/004/009 are pure *shape* claims and inherit `multi-agent-namespace-l2.sh`'s
#     licence to use *a* pullable image, but V-RUN-005 is a claim about the agent binary's
#     `wait-for-broker` behaviour and must pin the digest it actually ran."
#
#   So a tag is not enough anywhere in this file. Both halves of the pin are asserted, because the
#   binary whose behaviour this row is about is NOT the agent image: `buildWaitForBrokerContainer`
#   (broker_manifests.go) runs `brokerImage()`, so `wait-for-broker` is the BROKER binary executing
#   inside the AGENT pod. The init container's resolved `imageID` is therefore the digest under
#   test, it is compared against the digest the controller was told to render, and the agent
#   container's own digest is pinned against Artifact Registry beside it. An unresolvable digest on
#   either is a FAILURE, not a note: every sentence below would otherwise be about unknown code.
#
# WHAT IS ASSERTED, in order:
#   L2-0  P1 on the controller — it chooses the broker image, so a stale controller hands out a
#         stale `wait-for-broker`. Plus the image indirection itself: the ref the controller was
#         told to render.
#   L2-1  ARM (b), BROKER-FIRST WITH NO AGENT AT ALL. The CR is created with
#         `spec.deployment.scaleToZero: true`, which zeroes the AGENT Deployment and leaves the
#         broker at one replica (`brokerReplicas` is a constant in broker_manifests.go; only
#         `resolveDeploymentReplicasAndStrategy` reads scaleToZero). So the broker converges to
#         Available with no caller in existence — not a caller that is idle, a caller that has never
#         been scheduled. It is then observed idle for a bounded window and must not restart, must
#         create no ActionRecord, and must log no envelope line. Its own digest is pinned here.
#   L2-2  ARM (b) CONVERGING, AND ARM (c)'s POSITIVE HALF. The CR is re-applied without
#         scaleToZero. The agent Deployment scales to one, and the new pod's `wait-for-broker`
#         meets a broker that was already serving: it must terminate 0 having logged "broker is
#         ready" against the RENDERED endpoint's /healthz — which is this suite's evidence that the
#         broker serves that route, produced by the shipped binary over real mTLS rather than by a
#         probe pod this suite would have had to invent. The CR must then report AgentReady ∧
#         BrokerReady ∧ Ready, and the broker pod must be the SAME pod, unrestarted.
#   L2-3  ARM (a), AGENT-FIRST WITH NO BROKER, AND ARM (c)'s NEGATIVE HALF. The actor
#         ServiceAccount this suite created is deleted and the broker Deployment is force-recreated,
#         so the ServiceAccount admission plugin refuses its pod and the broker Service has no
#         endpoints. The agent Deployment is then force-recreated: its `wait-for-broker` polls for
#         the full `waitForBrokerTimeoutSeconds`, times out, and must exit 0 having logged
#         "starting in observe-and-report mode"; the agent container must then be Running with zero
#         restarts. The CR must report AgentReady=True, BrokerReady=False and therefore Ready=False
#         — the conjunction actually biting, which is the only form in which arm (c) is evidence.
#   L2-4  THE ¬ (V-MET-014). The verdict predicate both live directions feed is re-run against nine
#         doctored transcripts, seven of which it must reject and two of which it must accept.
#
# WHY THERE IS NO CONTROL-PLANE MUTATION HERE, and why the obvious lever is the WRONG experiment
#   08 §7(a) says "delay the broker's image pull", and the only lever for that is
#   `KUBEAGENTS_BROKER_IMAGE` on the CONTROLLER's Deployment — `brokerImage()` reads it there and
#   there is deliberately no `spec.deployment.brokerImage` on the CR (broker_manifests.go says so in
#   its own comment). That lever is not merely risky, it measures the wrong thing: the init
#   container runs the broker image too, so an unpullable broker image leaves the AGENT pod in
#   `Init:ImagePullBackOff`. The init container never runs, never times out, and observe-and-report
#   is never reached — the arm would fail to exercise the branch it exists for. 08 §7(a) predates
#   the decision that `wait-for-broker` runs the broker binary.
#
#   The absent-actor fixture below reaches the same state through the CR's own namespace: the broker
#   pod is refused at admission, so the endpoint is dark while the init container's image is already
#   on the node. Nothing outside this suite's namespace is written, so the restore path is the
#   namespace delete the EXIT trap already owns.
#
# NON-CLAIMS
#   - Not a claim that the agent's write tool surface is registered on the success path, or absent
#     on the timeout path. That is what the surface itself does with `KUBEAGENTS_BROKER_STATUS_FILE`
#     and it belongs to a different row; this suite asserts the init container's verdict and the
#     CR's conditions, which are the two artifacts 08 §2.4 names.
#   - Not a claim about the shipped agent image's CONTENTS. The digest is pinned so the run says
#     which build it measured; P1's freshness half is asserted against the controller only.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates and deletes a namespace,
# an Agent CR, a ServiceAccount and cluster-scoped RBAC, and it deliberately deletes an actor
# ServiceAccount to darken a broker. On the live install that is a test breaking the fleet's own
# write path.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED (P1/P10/substrate unverifiable).
# Usage: dev/verify/startup-ordering-l2.sh [kube-context]
#        dev/verify/startup-ordering-l2.sh --negative-control
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — asserted with
#      p1_assert_build_under_test, because the controller is what CHOOSES the broker image that
#      `wait-for-broker` executes. Beyond it, and stronger than P1's selector form, this suite pins
#      the resolved imageID DIGEST of three running artifacts by hand: the broker pod, the
#      `wait-for-broker` init container inside the agent pod, and the agent container. Any one of
#      them reporting a tag rather than a digest fails the run (phase-9.md:7622-7624).
#   P3 admission-recreate: the namespace and the Agent CR are created fresh on every run and deleted
#      by the EXIT trap, so both Deployments are rendered by the controller now running. Both halves
#      of the pair are then put through `p3_force_recreate` at the point their admission is the
#      subject — the broker in L2-3, so its refusal is a fresh admission decision and not an
#      inherited one, and the gateway in L2-3, so the pod that meets the dark broker went through
#      admission after it went dark. Every pod this suite reads is resolved with `p3_pod_of_deploy`,
#      by ownership, so the generation P3 has just replaced can never be read as the current one.
#   P6 runtime-authoritative: every claim is read off LIVE objects — the controller Deployment's own
#      env var, the rendered broker and gateway Deployments, the rendered broker endpoint read back
#      out of the agent container's environment, the pods' kubelet-written imageIDs, the init
#      container's own log, the Agent CR's `.status.conditions`, and the ActionRecords the API
#      server holds. Never a render golden, never `agent_manifests.go`: the goldens for this row are
#      already green at L1 and they are the reason the row is graded L2.
set -uo pipefail

MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

# A per-run namespace, deleted by the EXIT trap. `kubeagents-system` would work for the pair itself,
# but L2-3 has to DELETE an actor ServiceAccount, and in the shared namespace that object is one the
# other broker suites create and deliberately leave behind for each other. Deleting it there would
# silently change what the next suite in the chain is running against; here the only thing that
# holds it is this run.
NS=startup-ordering-l2
AGENT=ordering-agent
TIER=platform
# The scope leaf, and it is suite-private on purpose: `actor_service_account_name` derives the actor
# SA from tier+leaf, so a unique project id gives this run a uniquely-named actor SA and a uniquely
# named cluster-scoped grant. Both are removed by the trap. It also keeps the (tier, scope)
# cardinality key clear of any platform agent another suite left on the cluster.
PROJECT_SCOPE=startup-ordering-l2-project
GATEWAY="$AGENT-gateway"
BROKER="$AGENT-broker"
# Both container names are literals in the operator: `platform-agent` is the agent container's name
# for EVERY tier (agent_manifests.go), and `wait-for-broker` is buildWaitForBrokerContainer's.
AGENT_CONTAINER=platform-agent
INIT_CONTAINER=wait-for-broker
# waitForBrokerTimeoutSeconds in broker_manifests.go. Not a duplicate definition site — the value is
# only used to size this suite's own waits, and the assertion is on the VERDICT the init container
# reaches, never on how long it took to reach it.
WAIT_FOR_BROKER_S=120
# How long the broker is watched doing nothing in L2-1. Long enough that a broker which crash-loops
# for want of a caller would have restarted at least once (its livenessProbe period is 20s).
IDLE_OBSERVATION_S=60

fail=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END. Borrowed from broker-auth-l2.sh, which
# earned it: a suite whose arms were moved into a function and whose call site was not added back
# printed green PASS lines and a PROVEN verdict having asserted nothing at all. `fail` stays 0 when
# no assertion runs, so the number of arms has to be an assertion too.
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}
# 2 (L2-0) + 4 (L2-1) + 5 (L2-2) + 3 (L2-3) + 1 (L2-4). Change this deliberately, in the same commit
# as the arm you added.
EXPECTED_ASSERTIONS=15

# ------------------------------------------------------------------------------------------------
# THE VERDICT PREDICATE — one function, both directions, and the ¬ re-runs it
# ------------------------------------------------------------------------------------------------
#
# It takes a flattened transcript of one agent pod and one CR, and answers with a count of problems
# on stdout and prose on stderr. Both live arms feed it and L2-4 feeds it doctored rows, which is
# what makes L2-4 a control over THIS check rather than over a paraphrase of it.
#
#   mode      `ready`   the broker was serving when the init container polled
#             `observe` the broker was dark and the init container had to time out
#   init_exit the init container's terminated exitCode. 08 §2.4 requires SUCCESS EITHER WAY: a
#             non-zero exit here restarts the pod, which is the crash-loop arm (a) forbids.
#   init_log  `ready` | `observe` | `none` — which of runWaitForBroker's two terminal lines the
#             container actually logged. `none` is the vacuity guard: an init container that
#             terminated 0 having logged neither is one that did not run the branch under test.
#   restarts  the agent container's restartCount.
#   phase     the pod's `.status.phase`.
#   ar/br/rd  the CR's AgentReady / BrokerReady / Ready condition statuses.
ordering_problems() {
  local mode="$1" init_exit="$2" init_log="$3" restarts="$4" phase="$5"
  local cond_agent="$6" cond_broker="$7" cond_ready="$8"
  local problems=0

  if [ "$init_exit" != "0" ]; then
    echo "  the $INIT_CONTAINER init container exited ${init_exit:-<never terminated>}, not 0 — a" >&2
    echo "    non-zero init exit restarts the pod, so the agent never starts at all (08 §2.4)" >&2
    problems=$((problems + 1))
  fi
  if [ "$phase" != "Running" ]; then
    echo "  the agent pod is in phase '${phase:-<unreadable>}', not Running" >&2
    problems=$((problems + 1))
  fi
  case "$restarts" in
    0) : ;;
    "")
      echo "  the agent container reports no restartCount; 'does not crash-loop' is unjudgeable" >&2
      problems=$((problems + 1))
      ;;
    *)
      echo "  the agent container has restarted $restarts time(s) — that is the crash-loop 08 §2.4" >&2
      echo "    forbids in both directions" >&2
      problems=$((problems + 1))
      ;;
  esac

  case "$mode" in
    ready)
      if [ "$init_log" != "ready" ]; then
        echo "  broker-first: the init container logged '${init_log}', not the 'broker is ready'" >&2
        echo "    line runWaitForBroker emits on a 200 from /healthz" >&2
        problems=$((problems + 1))
      fi
      if [ "$cond_agent" != "True" ] || [ "$cond_broker" != "True" ] || [ "$cond_ready" != "True" ]; then
        echo "  broker-first did not converge: AgentReady=${cond_agent:-<unset>}" >&2
        echo "    BrokerReady=${cond_broker:-<unset>} Ready=${cond_ready:-<unset>}" >&2
        problems=$((problems + 1))
      fi
      ;;
    observe)
      if [ "$init_log" != "observe" ]; then
        echo "  agent-first: the init container logged '${init_log}', not the observe-and-report" >&2
        echo "    line runWaitForBroker emits when the bounded timeout expires" >&2
        problems=$((problems + 1))
      fi
      if [ "$cond_agent" != "True" ]; then
        echo "  agent-first: AgentReady=${cond_agent:-<unset>}. 08 §2.4 says the agent 'starts" >&2
        echo "    anyway' when the broker is dark; a fleet blinded by a broker outage is the" >&2
        echo "    failure this branch exists to prevent" >&2
        problems=$((problems + 1))
      fi
      if [ "$cond_broker" = "True" ]; then
        echo "  agent-first: BrokerReady=True while the broker was deliberately dark — the fixture" >&2
        echo "    did not produce the ordering, so nothing below it is evidence" >&2
        problems=$((problems + 1))
      fi
      if [ "$cond_ready" != "False" ]; then
        echo "  Ready=${cond_ready:-<unset>} with BrokerReady=${cond_broker:-<unset>}. 08 §7(c):" >&2
        echo "    Ready is the CONJUNCTION, so a half-up pair may not report Ready" >&2
        problems=$((problems + 1))
      fi
      ;;
    *)
      echo "  unknown ordering mode '$mode'" >&2
      problems=$((problems + 1))
      ;;
  esac

  echo "$problems"
}

# ------------------------------------------------------------------------------------------------
# L2-4 — the ¬ (V-MET-014)
# ------------------------------------------------------------------------------------------------
#
# 09 §6 marks V-MET-014 mandatory for every check: a suite that cannot fail is not evidence. There
# is no way to inject a broken ordering into a live cluster — an init container that exits non-zero
# would mean shipping a `kage-broker` build that must never exist, which is the same argument
# broker-auth-l2.sh's transcript control makes — so the injection point moves out to the transcript.
# The predicate is the whole verdict for both live directions, so doctoring its inputs is doctoring
# the check and not a paraphrase of it.
#
# TWO ROWS MUST PASS, and that is the control on the control: a predicate hard-wired to reject
# everything would satisfy seven rejections and tell nobody anything.
#
# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060 — a ¬ form that synthesises its input measures
# nothing about how that input is obtained, and the statements it bypasses are exactly where an L2
# suite fails: the API call, the parse, the lookup. For this suite the bypassed set is unusually
# large, because the whole property is an ORDERING and an ordering is precisely the thing a table
# of rows cannot have.)
#   - THE ORDERING ITSELF, which is the row. `scaleToZero: true` producing a running broker with no
#     agent pod, the re-apply scaling the agent half up without disturbing the broker, and the
#     deleted actor ServiceAccount darkening the broker for a pod admitted afterwards — the control
#     is handed a `mode` argument that asserts an ordering happened. If every fixture silently
#     produced the same ordering, or none, the control would still be 9/9.
#   - the init container running at all: the pull of the broker image into the agent pod, the mTLS
#     handshake, the poll of /healthz, and the bounded timeout actually expiring. `init_log` is a
#     word in a table here; live it is a grep over a log that a real binary either wrote or did not.
#   - every read that produces the transcript — `wait_init_terminated`, `init_log_verdict`,
#     `pod_transcript`, `cr_conditions`, `broker_ready_now`. A jsonpath that selects nothing returns
#     the empty string, and empty is a value the predicate is fed by hand in exactly one row; the
#     control cannot tell a genuinely-absent condition from a misspelled selector.
#   - the digest pins, all three. They are asserted outside the predicate, so the ¬ says nothing
#     about whether the run measured the build it claims — and phase-9.md:7622-7624 makes that the
#     load-bearing half of this row.
#   - `p3_force_recreate` and `p3_pod_of_deploy`: whether the pod read is the NEW generation. A
#     stale pod produces a perfectly well-formed transcript of the wrong experiment.
#   - the two vacuity guards (`still_dark`, the gateway-scaled-to-0 check) that decide whether each
#     arm set its ordering up at all. Those are the assertions that stop this suite passing on a
#     fixture that never happened, and the control does not touch them.
# What it does prove, and all it proves: the verdict function is not always-green — it rejects a
# non-zero init exit, a crash-looping agent, a pod stuck in Init, a terminated-but-silent init
# container, an agent that never became ready, `Ready=True` over `BrokerReady=False`, and a
# broker-first run whose init container never saw a 200 — while still accepting the two transcripts
# 08 §2.4 describes.
run_negative_control() {
  local nc_fail=0 label want fields n out

  echo "===================================================================="
  echo " ¬ V-RUN-005 — the ordering predicate, against transcripts it must reject"
  echo "===================================================================="

  while IFS='|' read -r label want fields; do
    [ -n "$label" ] || continue
    # shellcheck disable=SC2086
    out="$(ordering_problems $fields 2>&1 >/dev/null)"
    # shellcheck disable=SC2086
    n="$(ordering_problems $fields 2>/dev/null)"
    if [ "$want" = FAIL ] && [ "${n:-0}" -gt 0 ]; then
      echo "PASS: ¬ rejected — $label"
    elif [ "$want" = PASS ] && [ "${n:-0}" -eq 0 ]; then
      echo "PASS: ¬ accepted — $label"
    else
      nc_fail=1
      echo "FAIL: ¬ $label — wanted $want, the predicate reported ${n:-<no answer>} problem(s): $out"
    fi
  done <<'DECL'
the init container exited non-zero instead of succeeding either way|FAIL|observe 1 observe 0 Running True False False
the agent container crash-loops behind a dark broker|FAIL|observe 0 observe 4 Running True False False
the agent pod never left Init|FAIL|observe 0 observe 0 Pending True False False
the init container terminated 0 having logged neither terminal line|FAIL|observe 0 none 0 Running True False False
observe-and-report, but the agent itself never became ready|FAIL|observe 0 observe 0 Running False False False
Ready is True while BrokerReady is False — the conjunction does not bite|FAIL|observe 0 observe 0 Running True False True
broker-first, but the init container never saw a 200 on /healthz|FAIL|ready 0 none 0 Running True True True
agent-first into observe-and-report, exactly as 08 §2.4 describes it|PASS|observe 0 observe 0 Running True False False
broker-first, both halves up, Ready is the conjunction of two Trues|PASS|ready 0 ready 0 Running True True True
DECL

  echo "===================================================================="
  if [ "$nc_fail" -eq 0 ]; then
    echo " ¬ SATISFIED: the verdict both live directions feed rejects every broken ordering above,"
    echo "   and still accepts the two that are correct."
    echo "===================================================================="
    return 0
  fi
  echo " ¬ FAILED — this suite would have passed an ordering it must refuse."
  echo "===================================================================="
  return 1
}

if [ "$MODE" = "negative-control" ]; then
  K="/bin/false"
  cd "$REPO_ROOT" || exit 1
  run_negative_control
  exit $?
fi

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero; that is the
# half that makes the rest of it a guard.
#
# It sits BELOW the `--negative-control` dispatch and above everything that addresses a cluster, in
# the exact shape invariants-gate.py's LSN-005 check parses: an unadorned switch on $CTX alone.
# Folding the mode into the switch subject would be equally correct and unreadable to the gate.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    echo "  This creates and DELETES namespace $NS, an Agent CR, cluster-scoped RBAC, and it" >&2
    echo "  deliberately deletes an actor ServiceAccount to darken a broker. Name the dev cluster" >&2
    echo "  explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

cd "$REPO_ROOT" || exit 1

# The agent image these CRs run, resolved exactly the way multi-agent-namespace-l2.sh resolves it
# and for the same reason: the shipped `ghcr.io/gke-labs/kube-agents/*-agent:v0.1.0` is unpublished,
# so a CR pinned to it produces an ImagePullBackOff that this suite would read as "the agent does
# not start when the broker is absent" — a false failure with exactly the right shape to be
# believed. The dirty-tree tag is DISCOVERED in the registry the kubelet pulls from rather than
# guessed, because a check whose one precondition is false inside every build session is a check
# that can only run outside one.
#
# Below the guard on purpose: it is the first thing in this file that reaches off the machine, and a
# refused target should cost nothing.
PROJECT_ID="${PROJECT_ID:-$(gcloud config get core/project 2>/dev/null)}"
REGION="${REGION:-us-east4}"
AR_REPO="${AR_REPO:-kube-agents}"
AGENT_IMAGE_REPO="${AGENT_IMAGE_REPO:-$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO}"
if [ -z "${AGENT_IMAGE_TAG:-}" ]; then
  _sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  AGENT_IMAGE_TAG="dev-$_sha"
  if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
    _dirty="$(gcloud artifacts docker tags list "$AGENT_IMAGE_REPO/$TIER-agent" \
      --project "$PROJECT_ID" --format='value(tag)' 2>/dev/null |
      grep "^dev-$_sha-dirty-[0-9]\{1,\}$" | sort -t- -k4,4n | tail -1)"
    [ -n "$_dirty" ] && AGENT_IMAGE_TAG="$_dirty"
  fi
fi

echo "===================================================================="
echo " V-RUN-005 at L2 — startup ordering is safe in both directions"
echo " ctx: $CTX · ns: $NS · agent: $AGENT"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "DEFERRED: context '$CTX' is not reachable."; exit 3; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# The suite-private cluster-scoped objects `render_actor_grant` emits are named for the actor SA,
# which is named for this run's unique scope leaf, so the trap can remove them by name. The two
# SHARED-name objects are deliberately left: `ClusterRole/kubeagents-broker-operations` is identical
# on every install, and `ClusterRoleBinding/<reader-ksa>-broker-operations` is re-applied by every
# `seed_agent_identity` caller. Its subject will name a namespace that no longer exists, which
# grants nothing to nobody; deleting it instead would leave the same hole with an extra step.
ACTOR_KSA=""
cleanup() {
  if [ -n "$ACTOR_KSA" ]; then
    $K delete clusterrolebinding "$ACTOR_KSA" --ignore-not-found --wait=false >/dev/null 2>&1
    $K delete clusterrole "$ACTOR_KSA" --ignore-not-found --wait=false >/dev/null 2>&1
  fi
  $K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1
  echo
  echo "CLEANED UP: namespace $NS is deleted — the Agent CR, both Deployments, both pods, the mesh"
  echo "  Certificates, the reader and actor ServiceAccounts and the namespaced grant go with it by"
  echo "  ownership. This run's cluster-scoped grant '${ACTOR_KSA:-<never published>}' is deleted by"
  echo "  name. Nothing outside this namespace was modified at any point: L2-3 darkens the broker by"
  echo "  removing an identity THIS RUN created, never by editing the controller."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# Polling helpers. Every `.status` read in this file lives inside one of these loops (P9): the
# controller and the kubelet write those subtrees after admission, and an unsynchronised read
# returns whatever has landed so far — an empty one being indistinguishable from the property
# genuinely being absent.
# ------------------------------------------------------------------------------------------------

# wait_broker_replicas <want> <timeout> — rc 0 once the broker Deployment reports >= <want> ready.
wait_broker_replicas() {
  local want="$1" timeout="$2" got="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    got="$($K -n "$NS" get "deploy/$BROKER" -o jsonpath="{.status.readyReplicas}" 2>/dev/null)"
    case "${got:-0}" in
      '' | *[!0-9]*) : ;;
      *) [ "${got:-0}" -ge "$want" ] && return 0 ;;
    esac
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# broker_ready_now — the current readyReplicas, or 0. Polled once around, because the answer is only
# used as a witness that the broker STAYED dark and a transient empty read would flatter it.
broker_ready_now() {
  local got="" i=0
  while [ "$i" -lt 2 ]; do
    got="$($K -n "$NS" get "deploy/$BROKER" -o jsonpath="{.status.readyReplicas}" 2>/dev/null)"
    # The subject is $got, NOT ${got:-0}. With the default applied first, an empty read arrives at
    # the case as the string `0`, which matches neither arm, so `got` stays EMPTY and the `-gt`
    # below dies with `integer expression expected` — the sanitiser sanitised a copy.
    case "$got" in
      '' | *[!0-9]*) got=0 ;;
    esac
    [ "$got" -gt 0 ] && break
    i=$((i + 1))
    sleep 2
  done
  printf '%s' "${got:-0}"
}

# wait_init_terminated <pod> <timeout> — prints the init container's exitCode once it has one.
wait_init_terminated() {
  local pod="$1" timeout="$2" code="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    code="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.initContainerStatuses[?(@.name==\"$INIT_CONTAINER\")].state.terminated.exitCode}" 2>/dev/null)"
    if [ -n "$code" ]; then
      printf '%s' "$code"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# wait_init_image_id <pod> <timeout> — the digest the kubelet resolved for `wait-for-broker`.
# This is the binary V-RUN-005 is about; an empty answer is a failure, never a note.
wait_init_image_id() {
  local pod="$1" timeout="$2" iid="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    iid="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.initContainerStatuses[?(@.name==\"$INIT_CONTAINER\")].imageID}" 2>/dev/null)"
    if [ -n "$iid" ]; then
      printf '%s' "$iid"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# wait_agent_image_id <pod> <timeout> — the same, for the agent container.
wait_agent_image_id() {
  local pod="$1" timeout="$2" iid="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    iid="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.containerStatuses[?(@.name==\"$AGENT_CONTAINER\")].imageID}" 2>/dev/null)"
    if [ -n "$iid" ]; then
      printf '%s' "$iid"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# wait_agent_running <pod> <timeout> — rc 0 once the agent container is Running. rc 1 on timeout,
# with the container's own waiting reason on stdout so the caller can tell an image problem (which
# is a deferral) from a container that will not start (which is the property failing).
wait_agent_running() {
  local pod="$1" timeout="$2" started="" reason="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    started="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.containerStatuses[?(@.name==\"$AGENT_CONTAINER\")].state.running.startedAt}" 2>/dev/null)"
    [ -n "$started" ] && return 0
    reason="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.containerStatuses[?(@.name==\"$AGENT_CONTAINER\")].state.waiting.reason}" 2>/dev/null)"
    sleep 3
    waited=$((waited + 3))
  done
  printf '%s' "${reason:-none reported}"
  return 1
}

# pod_transcript <pod> — the three remaining pod fields the predicate wants, tab-separated:
# phase, the agent container's restartCount, and the pod's readiness. One function so the picture is
# taken at one moment rather than assembled from four independently-timed reads.
pod_transcript() {
  local pod="$1" phase="" restarts="" waited=0
  while [ "$waited" -lt 60 ]; do
    phase="$($K -n "$NS" get "pod/$pod" -o jsonpath="{.status.phase}" 2>/dev/null)"
    restarts="$($K -n "$NS" get "pod/$pod" \
      -o jsonpath="{.status.containerStatuses[?(@.name==\"$AGENT_CONTAINER\")].restartCount}" 2>/dev/null)"
    if [ -n "$phase" ] && [ -n "$restarts" ]; then
      break
    fi
    sleep 3
    waited=$((waited + 3))
  done
  printf '%s\t%s' "${phase:-}" "${restarts:-}"
}

# broker_image_id <pod> — the digest the kubelet resolved for the broker container.
broker_image_id() {
  local pod="$1" iid="" waited=0
  while [ "$waited" -lt 120 ]; do
    iid="$($K -n "$NS" get "pod/$pod" -o jsonpath='{.status.containerStatuses[*].imageID}' 2>/dev/null)"
    if [ -n "$iid" ]; then
      printf '%s' "$iid"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# broker_restarts <pod> — the broker container's restartCount, polled until the runtime publishes it.
broker_restarts() {
  local pod="$1" n="" waited=0
  while [ "$waited" -lt 60 ]; do
    n="$($K -n "$NS" get "pod/$pod" -o jsonpath="{.status.containerStatuses[*].restartCount}" 2>/dev/null)"
    [ -n "$n" ] && break
    sleep 3
    waited=$((waited + 3))
  done
  printf '%s' "${n:-}"
}

# cr_conditions <want-broker-ready> <timeout> — the three 08 §7(c) conditions, tab-separated.
# Polls until BrokerReady reaches the state the arm is waiting for, then takes all three at once, so
# the conjunction is read from one settled object rather than from three moments.
cr_conditions() {
  local want="$1" timeout="$2" a="" b="" r="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    a="$($K -n "$NS" get "agent/$AGENT" -o jsonpath="{.status.conditions[?(@.type=='AgentReady')].status}" 2>/dev/null)"
    b="$($K -n "$NS" get "agent/$AGENT" -o jsonpath="{.status.conditions[?(@.type=='BrokerReady')].status}" 2>/dev/null)"
    r="$($K -n "$NS" get "agent/$AGENT" -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}" 2>/dev/null)"
    if [ "$a" = "True" ] && [ "$b" = "$want" ]; then
      break
    fi
    sleep 3
    waited=$((waited + 3))
  done
  printf '%s\t%s\t%s' "${a:-}" "${b:-}" "${r:-}"
}

# wait_actor_ksa <timeout> — the actor ServiceAccount NAME the controller published. Read rather
# than derived: `scope.Of(agent).Leaf()` already has two implementations and a third here would be a
# third thing to keep in step (P6).
wait_actor_ksa() {
  local timeout="$1" v="" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    v="$($K -n "$NS" get "agent/$AGENT" -o jsonpath="{.status.broker.actorServiceAccount}" 2>/dev/null)"
    if [ -n "$v" ]; then
      printf '%s' "$v"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

# init_log_verdict <pod> — which of runWaitForBroker's two terminal lines the container logged.
# `none` if neither, which the predicate treats as a vacuity failure rather than as a near-miss.
init_log_verdict() {
  local pod="$1" log
  log="$($K -n "$NS" logs "pod/$pod" -c "$INIT_CONTAINER" 2>/dev/null)"
  if printf '%s\n' "$log" | grep -q 'broker did not become ready within the timeout'; then
    printf 'observe'
  elif printf '%s\n' "$log" | grep -q 'broker is ready'; then
    printf 'ready'
  else
    printf 'none'
  fi
}

# digest_of <imageID> — the `sha256:...` half, empty if the reference names no digest.
digest_of() {
  case "$1" in
    *@sha256:*) printf '%s' "${1##*@}" ;;
    *) printf '' ;;
  esac
}

# create_the_agent <scale-to-zero: true|false>
#   The CR, applied. Applied and never delete-then-applied on the second call: L2-2's whole point is
#   that the BROKER keeps running while the agent half arrives, and a delete would take it with it
#   by ownerReference.
#
#   `serviceAccountName` is the TIER's reader SA, not a name of this suite's choosing: 06 §1.2 V-10
#   refuses an arbitrary one, because that would be an authority the CR author picked rather than
#   one the tier template grants.
create_the_agent() {
  local stz="$1"
  $K apply -f - >/dev/null <<YAML || return 1
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $AGENT
  namespace: $NS
spec:
  tier: $TIER
  scope:
    projectId: $PROJECT_SCOPE
  harness:
    clusterName: cluster-a
    location: us-central1
    hermes:
      agentHome: /opt/data
      apiServerSecretRef:
        name: ${AGENT}-secrets
        key: API_SERVER_KEY
  deployment:
    image: ${AGENT_IMAGE_REPO}/${TIER}-agent
    tag: ${AGENT_IMAGE_TAG}
    imagePullPolicy: IfNotPresent
    scaleToZero: $stz
  security:
    serviceAccountName: ${TIER}-agent
YAML
}

# seed_tier_egress_policy — the per-tier default-deny egress allowlist provision_13 applies.
#
# WITHOUT THIS THE SUITE MEASURES A NAMESPACE NO INSTALL PATH PRODUCES, and it measured one for two
# runs. Arm (a) went red with the init container logging `context deadline exceeded` on its FIRST
# probe while deploy/$BROKER read 1/1/1, which is a shape worth naming: the broker was up, the
# caller could not say its name. The cause is the UNION the operator's own pair_netpol.go header
# states. `<agent>-to-broker` is Egress-ONLY and selects the reader pod, and in Kubernetes ANY
# egress policy makes the selected pod default-deny for every OTHER egress — DNS included. The
# operator is right to render only the hop it owns; rule 1 of the TIER allowlist (kube-system:53)
# owns the rest, and in a namespace where provision_13 has never run, nothing owns it at all. The
# broker's name does not resolve, wait-for-broker exhausts its 120s, the pair converges to
# observe-and-report, and the whole thing presents as "the broker is not ready".
#
# THE ABSENCE ALSO MADE ARM (a) VACUOUS, which is why this is seeded rather than worked around: arm
# (a) EXPECTS the timeout, so on a namespace with no DNS it passes for the wrong reason and would go
# on passing if wait-for-broker were deleted outright ([[LSN-035]]).
#
# RENDERED BY THE SHIPPED RENDERER, never a second copy ([[LSN-024]]). common.sh:render_egress_policy
# is the same function provision_13 calls, sourced in a SUBSHELL for a reason that is not style:
# common.sh installs its own `trap cleanup EXIT` at load, and sourcing it here would replace this
# suite's namespace teardown with a `tput cnorm` and leak the fixture on every run.
#
# RULE 9 IS NOT OPTIONAL HERE and it is resolved by the SHIPPED resolver, not by a local copy. The
# tier selector is `kube-agents/tier`, which the BROKER pod carries too, so applying this allowlist
# without the API-server rule takes TokenReview, the FleetFreeze read and the ActionRecord write away
# from the broker. That failure is silent past the point of being subtle: `startSources()` reads the
# brake BEFORE the listener opens, so the pod never binds :8443, `kubectl logs` is EMPTY, and both
# probes report `connection refused` — measured here on 2026-08-01, and it is what found the resolver
# bug the same day ([[LSN-069]]).
#
# THE `kubectl` SHIM IS THE WHOLE REASON THIS IS NOT A SECOND COPY OF THE RESOLUTION. common.sh calls
# a BARE `kubectl`, which answers for the AMBIENT context — during this unit that was
# `k8s-lookout-test`, and the resolver returned that cluster's control-plane address for a policy
# about to be applied to $CTX. A shell function shadows the binary for the whole subshell, so
# resolve_apiserver_cidrs stays the single definition site AND asks the cluster under test.
seed_tier_egress_policy() {
  local rendered

  # WORKLOAD_IDENTITY_ENABLED=true and GKE_DATAPLANE=auto are provision_13's settings for a WI GKE
  # cluster, which is what $CTX is. `auto` emits BOTH dataplane pairings, exactly as the install
  # path does — narrowing it here would make the fixture's policy something no install produces.
  rendered="$(
    cd "$REPO_ROOT/k8s-operator/scripts" 2>/dev/null &&
      WORKLOAD_IDENTITY_ENABLED=true \
        GKE_DATAPLANE=auto \
        KAGE_CTX="$CTX" \
        CONTROL_NAMESPACE="${CONTROL_NAMESPACE:-kubeagents-system}" \
        bash -c '
          . ./common.sh >/dev/null 2>&1
          # common.sh installs `trap cleanup EXIT` at load and its cleanup writes `tput cnorm` to
          # STDOUT, which is the same stream the manifest is being captured on.
          trap - EXIT
          kubectl() { command kubectl --context "$KAGE_CTX" "$@"; }
          # provision_13 lines 81-88, verbatim in effect: resolve_apiserver_cidrs writes the list and
          # render_apiserver_block reads it out of the PLURAL name. Exporting neither renders a
          # policy with no rule 9 and no complaint, which is what a scratch namespace got all morning.
          KUBE_APISERVER_CIDRS="$(resolve_apiserver_cidrs)" || exit 1
          export KUBE_APISERVER_CIDRS
          printf "  apiserver rule 9: %s\n" "$KUBE_APISERVER_CIDRS" >&2
          render_egress_policy "$1" "$2" "$3"
        ' _ "${TIER}-egress" "$NS" "$TIER"
  )" || {
    echo "  could not resolve a kube-apiserver address for $CTX; refusing to apply an egress policy" >&2
    echo "  that would close the broker's write path (the same refusal provision_13 makes)." >&2
    return 1
  }

  # provision_13's own pre-apply refusal, for the same reason it makes it: an unsubstituted token
  # buried in a `cidr:` field is rejected by the API server with a message about the field, not
  # about the render (V-CMP-003).
  if printf '%s' "$rendered" | grep -q 'REPLACE_WITH_\|PLACEHOLDER\|\${'; then
    echo "  the rendered tier egress policy still carries an unsubstituted token" >&2
    return 1
  fi
  printf '%s\n' "$rendered" | $K apply -f - >/dev/null || return 1
  echo "  tier egress policy: NetworkPolicy/${TIER}-egress applied in $NS, rendered by"
  echo "    common.sh:render_egress_policy — the same function provision_13 calls"
}

# ------------------------------------------------------------------------------------------------
# L2-0: the build under test, and the image indirection
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-0: the controller is the build under test, and it names a broker image =="

if ! $K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the Agent CRD is not installed on '$CTX' — nothing would reconcile this CR."
  echo "  Stand it up: dev/cluster/up.sh"
  exit 3
fi
if ! $K get crd actionrecords.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the ActionRecord CRD is absent, so 'the idle broker logs no envelopes' would be"
  echo "  asserted against a journal that cannot exist. Zero records is not evidence when zero is"
  echo "  the only possible answer (V-MET-014)."
  exit 3
fi

p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the controller (see above). The controller chooses the"
    echo "  broker image that wait-for-broker executes, so nothing below would be evidence about"
    echo "  this commit."
    exit 3
    ;;
  *)
    bad "P1: the controller is not running the build under test"
    exit 1
    ;;
esac

# The env var, read off the live Deployment (P6). Empty means every broker on this cluster is
# running broker_manifests.go's defaultBrokerImage — an unpublished GHCR tag whose pod cannot pull,
# and whose init container therefore never runs.
WANT_BROKER_IMAGE="$($K -n kubeagents-system get deploy kubeagents-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].env[?(@.name=="KUBEAGENTS_BROKER_IMAGE")].value}' 2>/dev/null)"
if [ -z "$WANT_BROKER_IMAGE" ]; then
  echo "DEFERRED: the controller carries no KUBEAGENTS_BROKER_IMAGE, so it is handing out"
  echo "  broker_manifests.go's defaultBrokerImage — the tag V-CMP-002 measured as unpullable."
  echo "  The init container would sit in Init:ImagePullBackOff and no ordering would be exercised."
  echo "  Unblock: dev/cluster/reload-images.sh broker $CTX"
  exit 3
fi
WANT_BROKER_DIGEST="$(digest_of "$WANT_BROKER_IMAGE")"
pass "the controller was told to render brokers at ${WANT_BROKER_IMAGE##*/}"

# The agent image must resolve in the registry the kubelet pulls from, and it must resolve to a
# DIGEST — a tag present somewhere says nothing about which build it is (LSN-001).
WANT_AGENT_DIGEST="$(gcloud artifacts docker images describe \
  "$AGENT_IMAGE_REPO/$TIER-agent:$AGENT_IMAGE_TAG" --project "$PROJECT_ID" \
  --format='value(image_summary.digest)' 2>/dev/null)"
if [ -z "$WANT_AGENT_DIGEST" ]; then
  echo "DEFERRED: $AGENT_IMAGE_REPO/$TIER-agent:$AGENT_IMAGE_TAG does not resolve in Artifact"
  echo "  Registry, so the agent pod would fail to pull and this suite would read that as 'the"
  echo "  agent does not start when the broker is absent' — a false failure shaped exactly like"
  echo "  arm (a) succeeding at nothing."
  echo "  Unblock: dev/cluster/reload-images.sh agents $CTX"
  exit 3
fi
echo "  agent image under test: $TIER-agent:$AGENT_IMAGE_TAG -> ${WANT_AGENT_DIGEST:0:19}..."

# ------------------------------------------------------------------------------------------------
# L2-1: ARM (b) — the broker starts first, and there is no agent at all
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-1: arm (b) — a broker with no caller in existence =="

$K create namespace "$NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true
# Before the CR, not after: the policy has to be in place before either pod dials anything, and a
# policy that lands mid-run restricts a broker whose connections are already open, which measures
# neither the with-policy nor the without-policy namespace.
seed_tier_egress_policy || {
  echo "FAIL: could not seed the per-tier egress policy; the namespace would not model an installed" >&2
  echo "  agent and arm (a) would pass for the wrong reason." >&2
  exit 1
}
create_the_agent true || {
  echo "FAIL: the API server refused the Agent CR; there is no fixture to measure." >&2
  exit 1
}
seed_agent_fixtures "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed the reader ServiceAccount and API-key Secret for $AGENT" >&2
  exit 1
}
ACTOR_KSA="$(wait_actor_ksa 90)" || {
  echo "DEFERRED: the controller never published status.broker.actorServiceAccount for $NS/$AGENT,"
  echo "  so the broker's identity cannot be created and no ordering can be set up."
  exit 3
}
seed_agent_identity "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed the actor identity for $AGENT" >&2
  exit 1
}

if ! wait_broker_replicas 1 300; then
  echo "DEFERRED: deploy/$BROKER never reported a ready replica. There is no broker to start first."
  $K -n "$NS" describe "deploy/$BROKER" 2>&1 | tail -25
  exit 3
fi

# The agent half must be absent, and absent by SPEC and not merely slow. `scaleToZero` sets
# replicas=0 on the gateway only; brokerReplicas is a constant.
gw_replicas="$($K -n "$NS" get "deploy/$GATEWAY" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
gw_pods="$($K -n "$NS" get pods -l "app=$GATEWAY" -o name 2>/dev/null | grep -c .)"
if [ "${gw_replicas:-x}" = "0" ] && [ "${gw_pods:-1}" -eq 0 ]; then
  pass "arm (b): deploy/$BROKER is serving while deploy/$GATEWAY is scaled to 0 and no agent pod exists — the broker started first, with no caller"
else
  bad "arm (b) was not set up: deploy/$GATEWAY has replicas=${gw_replicas:-<unset>} and $gw_pods pod(s). A broker whose agent is running is not a broker that started first, and everything below would be about a different ordering"
fi

broker_pod="$(p3_pod_of_deploy "$K" "$NS" "$BROKER" 180)"
if [ -z "$broker_pod" ]; then
  echo "DEFERRED: no pod is owned by deploy/$BROKER after 180s; there is nothing to observe."
  exit 3
fi
echo "  broker pod (by ownership, P3): $broker_pod"

broker_iid="$(broker_image_id "$broker_pod")" || broker_iid=""
broker_digest="$(digest_of "$broker_iid")"
if [ -z "$broker_digest" ]; then
  bad "the broker container reports imageID '${broker_iid:-none}', which names no digest. Every sentence in this suite is about what a particular binary does with an ordering (LSN-001: a tag is not a build)"
elif [ -n "$WANT_BROKER_DIGEST" ] && [ "$broker_digest" != "$WANT_BROKER_DIGEST" ]; then
  bad "the broker pod resolved to $broker_digest but the controller was told to render $WANT_BROKER_DIGEST. Either the pod predates the current KUBEAGENTS_BROKER_IMAGE, or this runtime reports a config digest rather than the manifest digest — both make the pin meaningless, and the first is LSN-001"
else
  pass "the broker pod runs the digest the controller pinned (${broker_digest:0:19}...)"
fi

# Idle observation. A broker that needs a caller to stay alive would restart inside this window
# (its livenessProbe fires every 20s), and one that initiates work would have journalled it.
echo "  observing the idle broker for ${IDLE_OBSERVATION_S}s..."
r0="$(broker_restarts "$broker_pod")"
sleep "$IDLE_OBSERVATION_S"
r1="$(broker_restarts "$broker_pod")"
if [ -n "$r0" ] && [ "$r0" = "$r1" ] && [ "$r1" = "0" ]; then
  pass "arm (b): the broker survived ${IDLE_OBSERVATION_S}s with no caller and zero restarts — it never initiates work, it waits"
else
  bad "arm (b): the broker's restartCount went '${r0:-unreadable}' -> '${r1:-unreadable}' over ${IDLE_OBSERVATION_S}s idle. A broker that cannot idle without a caller is not one an agent can start after"
fi

n_records="$($K -n "$NS" get actionrecords -o name 2>/dev/null | grep -c .)"
envelope_lines="$($K -n "$NS" logs "pod/$broker_pod" 2>/dev/null |
  grep -c -e 'action pipeline' -e 'broker security refusal')"
if [ "${n_records:-1}" -eq 0 ] && [ "${envelope_lines:-1}" -eq 0 ]; then
  pass "arm (b): the idle broker journalled 0 ActionRecords and logged 0 envelope lines — nothing was initiated from its end"
else
  bad "arm (b): the idle broker produced ${n_records:-?} ActionRecord(s) and ${envelope_lines:-?} envelope log line(s) with no caller in existence. 08 §2.4: 'it never initiates work'"
fi

# ------------------------------------------------------------------------------------------------
# L2-2: ARM (b) CONVERGING, and ARM (c)'s positive half
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-2: the agent arrives after the broker, and the pair converges =="

create_the_agent false || {
  echo "FAIL: could not scale the agent half up; the CR apply was refused." >&2
  exit 1
}

agent_pod="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY" 240)"
if [ -z "$agent_pod" ]; then
  echo "DEFERRED: no pod is owned by deploy/$GATEWAY after 240s, so no init container ever polled."
  $K -n "$NS" describe "deploy/$GATEWAY" 2>&1 | tail -25
  exit 3
fi
echo "  agent pod (by ownership, P3): $agent_pod"

init_exit="$(wait_init_terminated "$agent_pod" $((WAIT_FOR_BROKER_S + 180)))" || {
  echo "DEFERRED: the $INIT_CONTAINER init container in $agent_pod never terminated. It is still"
  echo "  pulling or still polling, and a verdict it has not reached is not one to record."
  $K -n "$NS" describe "pod/$agent_pod" 2>&1 | tail -25
  exit 3
}
init_log="$(init_log_verdict "$agent_pod")"

# The endpoint the init container actually polled, read out of its own log and checked against the
# endpoint the controller rendered into the agent container's environment (P6). This is the suite's
# evidence for "it serves /healthz": the shipped binary got a 200 there over real mTLS, across the
# real Service and the real pair NetworkPolicy.
rendered_endpoint="$($K -n "$NS" get "deploy/$GATEWAY" \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="KUBEAGENTS_BROKER_ENDPOINT")].value}' 2>/dev/null)"
polled_url="$($K -n "$NS" logs "pod/$agent_pod" -c "$INIT_CONTAINER" 2>/dev/null |
  grep -o 'https://[^" ]*/healthz' | head -1)"
if [ -n "$rendered_endpoint" ] && [ "$polled_url" = "$rendered_endpoint/healthz" ]; then
  pass "the broker served /healthz at the rendered endpoint $polled_url — probed by the shipped wait-for-broker binary, over mTLS, from inside the pair"
else
  bad "the init container polled '${polled_url:-nothing this suite could read}' while the controller rendered '${rendered_endpoint:-<unset>}'. Either the route under test was not the one exercised, or the two definition sites have moved apart"
fi

init_iid="$(wait_init_image_id "$agent_pod" 120)" || init_iid=""
init_digest="$(digest_of "$init_iid")"
if [ -z "$init_digest" ]; then
  bad "the $INIT_CONTAINER init container reports imageID '${init_iid:-none}', which names no digest. This is the binary V-RUN-005 is a claim about, and phase-9.md requires this suite to pin the digest it actually ran"
elif [ -n "$WANT_BROKER_DIGEST" ] && [ "$init_digest" != "$WANT_BROKER_DIGEST" ]; then
  bad "the $INIT_CONTAINER init container ran $init_digest, not the $WANT_BROKER_DIGEST the controller was told to render. The ordering below was exercised by a binary nobody chose"
else
  pass "the $INIT_CONTAINER init container ran the pinned broker digest (${init_digest:0:19}...)"
fi

agent_iid="$(wait_agent_image_id "$agent_pod" 240)" || agent_iid=""
agent_digest="$(digest_of "$agent_iid")"
if [ -z "$agent_digest" ]; then
  bad "the $AGENT_CONTAINER container reports imageID '${agent_iid:-none}', which names no digest — the node that ran it cannot be shown to hold the image under test"
elif [ "$agent_digest" != "$WANT_AGENT_DIGEST" ]; then
  bad "the agent container resolved to $agent_digest, not the $WANT_AGENT_DIGEST Artifact Registry holds for $TIER-agent:$AGENT_IMAGE_TAG. The pod is running some other build"
else
  pass "the agent container resolved to the pinned agent digest (${agent_digest:0:19}...)"
fi

if ! waiting_reason="$(wait_agent_running "$agent_pod" 300)"; then
  case "$waiting_reason" in
    *ImagePull* | *ErrImage* | *CreateContainerConfigError*)
      echo "DEFERRED: the agent container never started, and its waiting reason is"
      echo "  '$waiting_reason' — an image or config problem, not an ordering one."
      exit 3
      ;;
    *)
      bad "the agent container never started after the broker was already serving (waiting: ${waiting_reason:-none reported})"
      ;;
  esac
fi

# Substitute FIRST, into a plain variable, and only then read. Never
# `IFS=$'\t' read ... <<<"$(fn)"`: a variable assignment written as a command prefix is already in
# effect while the here-string operand is expanded, so the function runs with IFS=<tab> — and every
# helper in this file invokes the cluster as unquoted `$K`, which then stops splitting on spaces and
# becomes one command named `kubectl --context gke-scratch-kube-agents-dev`. There is no such
# command, the `2>/dev/null` on each read eats the error, and every field comes back empty. It does
# not look like a broken instrument; it looks like a pod with no phase and a CR with no conditions,
# which is exactly what this suite is built to report ([[LSN-065]]).
transcript="$(pod_transcript "$agent_pod")"
conditions="$(cr_conditions True 300)"
IFS=$'\t' read -r phase restarts <<<"$transcript"
IFS=$'\t' read -r cond_a cond_b cond_r <<<"$conditions"
echo "  transcript: init_exit=$init_exit init_log=$init_log phase=$phase restarts=$restarts AgentReady=$cond_a BrokerReady=$cond_b Ready=$cond_r"
n="$(ordering_problems ready "$init_exit" "$init_log" "$restarts" "$phase" "$cond_a" "$cond_b" "$cond_r")"
if [ "${n:-1}" -eq 0 ]; then
  pass "V-RUN-005 broker-first: the agent met a broker that was already serving, started with its write surface registered, and the CR converged to Ready (08 §7(c): AgentReady ∧ BrokerReady)"
else
  bad "V-RUN-005 broker-first did not converge — $n problem(s) reported above"
fi

broker_pod_after="$(p3_pod_of_deploy "$K" "$NS" "$BROKER" 60)"
r2="$(broker_restarts "$broker_pod")"
if [ "$broker_pod_after" = "$broker_pod" ] && [ "$r2" = "0" ]; then
  pass "the broker that the agent found is the same pod that was already running, unrestarted — the agent joined it, it did not wait for the agent"
else
  bad "the broker pod is now '${broker_pod_after:-none}' with restartCount '${r2:-unreadable}' (was '$broker_pod' at 0). The broker did not survive the agent's arrival, so 'broker-first' was not what converged"
fi

# ------------------------------------------------------------------------------------------------
# L2-3: ARM (a) — the agent starts first, into a dark broker; and ARM (c)'s negative half
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-3: arm (a) — the agent starts with no broker, and does not go blind =="

# Darken the broker by removing the identity its pod runs as. Nothing outside this namespace is
# touched: `$ACTOR_KSA` was created by this run's `seed_agent_identity` call, and the ServiceAccount
# admission plugin refuses to create a pod naming a ServiceAccount that does not exist — so the
# broker Deployment reports zero pods for a reason the API server records on the ReplicaSet.
$K -n "$NS" delete serviceaccount "$ACTOR_KSA" --ignore-not-found >/dev/null 2>&1
p3_force_recreate "$K" "$NS" "deploy/$BROKER" 120 || {
  echo "DEFERRED: deploy/$BROKER could not be force-recreated, so its pod was never re-admitted"
  echo "  under the rules that now apply and the broker may still be serving."
  exit 3
}
if wait_broker_replicas 1 45; then
  echo "DEFERRED: deploy/$BROKER still reports a ready replica after its actor ServiceAccount was"
  echo "  deleted. The fixture did not produce the agent-first ordering, and an arm that measures"
  echo "  a broker that is up is not arm (a) (V-MET-014)."
  exit 3
fi

# Now a NEW agent pod, admitted after the broker went dark.
p3_force_recreate "$K" "$NS" "deploy/$GATEWAY" 120 || {
  echo "DEFERRED: deploy/$GATEWAY could not be force-recreated; the pod below would be the one that"
  echo "  already met a healthy broker."
  exit 3
}
dark_pod="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY" 240)"
if [ -z "$dark_pod" ] || [ "$dark_pod" = "$agent_pod" ]; then
  echo "DEFERRED: no NEW pod is owned by deploy/$GATEWAY after 240s (got '${dark_pod:-none}')."
  exit 3
fi
echo "  agent pod facing a dark broker (by ownership, P3): $dark_pod"

# The bounded timeout has to actually elapse, so the budget is the timeout plus room to pull and
# schedule. A verdict read early is a verdict that has not happened.
dark_exit="$(wait_init_terminated "$dark_pod" $((WAIT_FOR_BROKER_S + 240)))" || {
  echo "DEFERRED: the $INIT_CONTAINER init container in $dark_pod never terminated within"
  echo "  ${WAIT_FOR_BROKER_S}s + 240s. Whether it would have timed out cleanly is unmeasured."
  $K -n "$NS" describe "pod/$dark_pod" 2>&1 | tail -25
  exit 3
}
dark_log="$(init_log_verdict "$dark_pod")"

if ! waiting_reason="$(wait_agent_running "$dark_pod" 300)"; then
  case "$waiting_reason" in
    *ImagePull* | *ErrImage* | *CreateContainerConfigError*)
      echo "DEFERRED: the agent container never started, and its waiting reason is"
      echo "  '$waiting_reason' — an image or config problem, not an ordering one."
      exit 3
      ;;
    *)
      bad "arm (a): the agent container never started behind a dark broker (waiting: ${waiting_reason:-none reported}). 08 §2.4: 'it starts anyway, in observe-and-report mode — a broker outage must not blind the fleet'"
      ;;
  esac
fi

still_dark="$(broker_ready_now)"
if [ "${still_dark:-1}" -eq 0 ]; then
  pass "arm (a): deploy/$BROKER had zero ready replicas for the whole of this pod's init window — the ordering under test was genuinely agent-first"
else
  bad "arm (a): deploy/$BROKER reported $still_dark ready replica(s) during the window, so the init container may have succeeded rather than timed out. The arm did not exercise the branch it claims"
fi

# Substituted first, for the reason spelled out at the broker-first pair above ([[LSN-065]]).
dark_transcript="$(pod_transcript "$dark_pod")"
dark_conditions="$(cr_conditions False 300)"
IFS=$'\t' read -r dark_phase dark_restarts <<<"$dark_transcript"
IFS=$'\t' read -r dcond_a dcond_b dcond_r <<<"$dark_conditions"
echo "  transcript: init_exit=$dark_exit init_log=$dark_log phase=$dark_phase restarts=$dark_restarts AgentReady=$dcond_a BrokerReady=$dcond_b Ready=$dcond_r"
n="$(ordering_problems observe "$dark_exit" "$dark_log" "$dark_restarts" "$dark_phase" "$dcond_a" "$dcond_b" "$dcond_r")"
if [ "${n:-1}" -eq 0 ]; then
  pass "V-RUN-005 agent-first: the init container timed out into observe-and-report, exited 0, and the agent started anyway without crash-looping"
else
  bad "V-RUN-005 agent-first did not converge — $n problem(s) reported above"
fi

if [ "$dcond_a" = "True" ] && [ "$dcond_b" = "False" ] && [ "$dcond_r" = "False" ]; then
  pass "arm (c): with AgentReady=True and BrokerReady=False the CR reports Ready=False — the conjunction is biting on a half-up pair, not merely agreeing with a tidy one"
else
  bad "arm (c): AgentReady=$dcond_a BrokerReady=$dcond_b gave Ready=$dcond_r. 08 §7(c) requires Ready to be the conjunction, and this run is the only one in which it can be observed failing"
fi

# ------------------------------------------------------------------------------------------------
# L2-4: the ¬
# ------------------------------------------------------------------------------------------------
echo
if run_negative_control; then
  pass "¬ V-RUN-005: the verdict predicate rejects all seven broken orderings and accepts the two correct ones"
else
  bad "¬ V-RUN-005: the verdict predicate would have passed an ordering it must refuse, so every PASS above is unattributable"
fi

# ------------------------------------------------------------------------------------------------
echo
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo "V-RUN-005 at L2: FAILED — $assertions arms ran, $EXPECTED_ASSERTIONS were expected. A suite"
  echo "  that reports a verdict it did not compute is worse than a suite that fails."
  exit 1
fi
if [ "$fail" -eq 0 ]; then
  echo "V-RUN-005 at L2: PROVEN — startup ordering is safe in both directions. Broker-first"
  echo "  converged against broker digest ${WANT_BROKER_DIGEST:-${broker_digest:-unknown}} and agent"
  echo "  digest ${WANT_AGENT_DIGEST:-unknown}; agent-first timed out into observe-and-report"
  echo "  without crash-looping; Ready stayed False while BrokerReady was False."
  exit 0
fi
echo "V-RUN-005 at L2: FAILED — see the FAIL lines above."
exit 1
