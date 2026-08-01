#!/usr/bin/env bash
# V-BRK-012 at L2 — "one broker per Agent CR; no fleet-wide writer exists anywhere in the DEPLOYED
# system ¬" (09 §6.2, 05 §7).
#
# The L0 half of this row is green and is a real result: `dev/tests/one-broker-per-agent.py` proves
# that every broker-derived name is a function of the CR, that nothing outside the definition site
# spells the `-broker` suffixes, that the rendered Deployment is single-replica with a two-label
# selector, and that the unpaired render is unrepresentable. Its own note ends by naming what it
# cannot reach: "the lint reads source, so it says nothing about a *deployed* fleet".
#
# This is that. And the gap between the two halves was not theoretical — until P9-T8b-4a the
# deployed fleet had NO brokers at all, on any cluster, and the L0 half was green the whole time:
#
#     `brokerImage()` reads KUBEAGENTS_BROKER_IMAGE off the CONTROLLER's Deployment and otherwise
#     falls back to `defaultBrokerImage` (broker_manifests.go). That variable was set nowhere in the
#     repository, and that fallback tag is one of the four the V-CMP-002 deferral measured as
#     unpullable. Every Agent CR rendered a broker whose pod could not pull.
#
#     The fallback is named here and never spelled: the tag has one definition site and it is that
#     constant. A copy in this header would be a second one, and `test_image_provenance` is right to
#     say so — it reads this file too.
#
# A source lint cannot see that, a render golden cannot see that, and the only thing that can is a
# cluster with two real Agent CRs on it.
#
# WHAT IS ASSERTED, in order:
#   L2-1  P1 twice over, on both ends of the indirection: the CONTROLLER (which chooses the broker
#         image) and every BROKER pod (which runs it) are the build under test, and the image the
#         controller was told to use is the image the rendered Deployments carry. A cluster where
#         those disagree is one whose brokers are a generation behind the variable, and every claim
#         below would be about the previous build.
#   L2-2  CARDINALITY AND OWNERSHIP. Each Agent CR has exactly one broker Deployment; that
#         Deployment's ownerReference is that CR; it is single-replica; and the number of broker
#         Deployments in the namespace equals the number of Agent CRs — no extra, no shared, no
#         orphan. Then one Running broker pod each, resolved by OWNERSHIP (P3), never by selector.
#   L2-3  THE SERVICE RESOLVES TO ITS OWN BROKER AND NOBODY ELSE'S. Each `<agent>-broker` Service
#         has exactly one endpoint address and it is that agent's own broker pod's IP.
#   L2-4  THE `¬` (09 §6 marks this row mandatory-negative-control), and it has two arms because
#         "no fleet-wide writer" can fail in two unrelated ways:
#         (a) IDENTITY. Each actor may create ActionRecords in its OWN namespace and may NOT create
#             them cluster-wide. A ClusterRoleBinding instead of a RoleBinding on the journal half
#             of the 06 §2.2.1 grant would make every agent's actor a writer of the whole fleet's
#             journal, and nothing about the pod topology would look different.
#         (b) TOPOLOGY, injected. A decoy Deployment carrying `kube-agents/role=actor` and owned by
#             no Agent CR is created, the L2-2 predicate is re-run and MUST reject it, and the decoy
#             is removed. Without that, "the counts matched" is a statement about a cluster that
#             happens to be tidy, not about a check that can tell.
#
# WHY THE FIXTURE IS THE TWO SHIPPED MANIFESTS, CO-LOCATED
#   `examples/gitops-repo/fleet/platform-agent.yaml` and
#   `examples/gitops-repo/clusters/cluster-a/agents/agent.yaml` both live in `kubeagents-system` —
#   a platform broker and a cluster-admin broker in one namespace, which is 08 §2.6's shape and the
#   only arrangement in which L2-3 can fail. If the Service selector were `role: actor` alone, each
#   Service would resolve to BOTH brokers and one agent's envelopes would round-robin into the
#   other's, which is a scope escape that looks like load balancing. A one-CR fixture cannot fail
#   that, which is LSN-015 stated as a fixture rather than as a note. They are seeded through
#   `seed_parent_agent`, so they are the shipped manifests and not this suite's paraphrase of them
#   (LSN-024) — and the cluster-admin CR needs the platform one anyway, as its parent (06 §1.2 V-6).
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates and deletes Agent CRs in
# `kubeagents-system`, applies RBAC, and creates a decoy Deployment. On the live install that is a
# test deleting the fleet's own agents.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED (P1/P10 unverifiable).
# Usage: dev/verify/broker-per-agent-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager AND every rendered
#      broker pod — asserted with p1_assert_build_under_test against both, because the broker's
#      image is chosen by one process and executed by another. The operator's own P1 says nothing
#      about the broker: a controller rebuilt an hour ago can still be handing out last week's
#      broker digest, and every claim here is about what the broker binary does.
#   P3 admission-recreate: both Agent CRs are deleted and re-applied on every run (`seed_parent_agent`
#      deletes before it applies, and the EXIT trap deletes after), so the broker Deployments under
#      test are rendered fresh by the controller now running — never inherited from an earlier
#      generation of the renderer or admitted under an earlier webhook. The decoy Deployment of
#      L2-4b is likewise created and deleted within the run. Broker pods are resolved through
#      `p3_pod_of_deploy`, by ownership, so a pod left over from the previous generation of the same
#      Deployment can never be read as this one's.
#   P6 runtime-authoritative: every claim is read off LIVE objects — the controller Deployment's own
#      env var, the rendered broker Deployments and their ownerReferences, the Endpoints the API
#      server computed from each Service's selector, and `kubectl auth can-i` answered by the
#      cluster's authorizer. Never config/manager/manager.yaml, never a render golden: this suite
#      exists precisely because the goldens were right and the cluster had no brokers.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
PLATFORM_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml
CLUSTER_ADMIN_MANIFEST=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
PLATFORM_AGENT=platform-agent
CLUSTER_ADMIN_AGENT=cluster-admin-cluster-a
# Two positionally-parallel indexed arrays below rather than one associative array: bash 3.2 is what
# /usr/bin/env bash resolves to on the macOS hosts this suite is driven from, and `declare -A` is a
# bash-4 syntax error there — not a runtime failure, a PARSE failure, so the script would die before
# the destructive-test guard ever ran.
AGENTS=("$PLATFORM_AGENT" "$CLUSTER_ADMIN_AGENT")
BROKER_PODS=()
DECOY=broker-per-agent-l2-decoy
# A namespace that certainly is not any agent's own, used only as the "somewhere else" of L2-4a.
# kube-system rather than a namespace this script creates: the question is whether the actor's
# journal grant is cluster-wide, and asking it about a namespace the suite made up would be asking
# about a namespace the RoleBinding could not have been written for either way.
ELSEWHERE=kube-system

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero; that is the
# half that makes the rest of it a guard.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (destructive-test guard)." >&2
    echo "  This DELETES Agent CRs named platform-agent and cluster-admin-cluster-a. Name the dev" >&2
    echo "  cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }

cd "$REPO_ROOT" || exit 1

echo "===================================================================="
echo " V-BRK-012 at L2 — one broker per Agent CR, deployed — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/parent-chain.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

seeded=()
cleanup() {
  $K -n "$NS" delete deploy "$DECOY" --ignore-not-found --wait=false >/dev/null 2>&1
  unseed_parent_agents "$K" "${seeded[@]:-}"
  echo
  echo "CLEANED UP: both Agent CRs and the decoy Deployment are deleted; their brokers go with them"
  echo "  by ownerReference. The actor ServiceAccounts and the broker-operations grant are LEFT — a"
  echo "  real install creates those once per namespace and outlives every CR in it, and deleting"
  echo "  them here would silently change what the NEXT suite in the chain is running against."
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures: the two shipped CRs, then the identity each one's broker runs as
# ------------------------------------------------------------------------------------------------
echo; echo "== fixtures: two co-located Agent CRs and their actor identities =="

for m in "$PLATFORM_MANIFEST" "$CLUSTER_ADMIN_MANIFEST"; do
  # Order matters and is not incidental: the cluster-admin CR's parentRef names the platform agent,
  # and 06 §1.2 V-6 rejects a child whose parent does not exist.
  if ref="$(seed_parent_agent "$K" "$m")"; then
    seeded+=("$ref")
    echo "  seeded $ref from $m"
  else
    echo "FAIL: could not seed $m: $ref" >&2
    exit 1
  fi
done

for a in "${AGENTS[@]}"; do
  seed_agent_fixtures "$K" "$NS" "$a" || { echo "FAIL: could not seed fixtures for $a" >&2; exit 1; }
  seed_agent_identity "$K" "$NS" "$a" || { echo "FAIL: could not seed the actor identity for $a" >&2; exit 1; }
done

# ------------------------------------------------------------------------------------------------
# L2-1: the build under test, on BOTH ends of the image indirection
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-1: the controller and its brokers are the build under test =="

p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable for the controller (see above). The controller is what CHOOSES"
     echo "  the broker image, so nothing below would be evidence about this commit."
     exit 3 ;;
  *) bad "P1: the controller is not running the build under test"; exit 1 ;;
esac

# The env var, read off the live Deployment. An empty value here is not a warning: it is the exact
# defect this unit fixed, and it means every broker below is running whatever `defaultBrokerImage`
# happens to be — which is an unpullable GHCR tag.
want_img="$($K -n "$NS" get deploy kubeagents-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].env[?(@.name=="KUBEAGENTS_BROKER_IMAGE")].value}' 2>/dev/null)"
if [ -z "$want_img" ]; then
  echo "DEFERRED: the controller carries no KUBEAGENTS_BROKER_IMAGE, so it is handing out"
  echo "  broker_manifests.go's defaultBrokerImage — the tag V-CMP-002 measured as unpullable."
  echo "  There is no broker to make a claim about. Deploy one:"
  echo "    dev/cluster/reload-images.sh broker $CTX"
  exit 3
fi
pass "the controller was told to render brokers at ${want_img##*/}"

# ------------------------------------------------------------------------------------------------
# L2-2: cardinality and ownership
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-2: one broker Deployment per Agent CR, owned by it =="

# THE PREDICATE IS A FUNCTION, because L2-4b re-runs it against an injected decoy. A copy-pasted
# second version there would be a negative control for a check that is not this one.
#
# It answers with a count of problems on stdout and prose on stderr, so the caller can use it both
# as an assertion and as an experiment.
broker_topology_problems() {
  local n_agents n_brokers problems=0 dname owner_kind owner_name reps
  n_agents="$($K -n "$NS" get agents -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c .)"
  n_brokers="$($K -n "$NS" get deploy -l kube-agents/role=actor -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c .)"
  if [ "$n_agents" != "$n_brokers" ]; then
    echo "  $n_brokers broker Deployment(s) for $n_agents Agent CR(s) in $NS" >&2
    problems=$((problems + 1))
  fi
  while read -r dname; do
    [ -n "$dname" ] || continue
    owner_kind="$($K -n "$NS" get "deploy/$dname" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)"
    owner_name="$($K -n "$NS" get "deploy/$dname" -o jsonpath='{.metadata.ownerReferences[0].name}' 2>/dev/null)"
    reps="$($K -n "$NS" get "deploy/$dname" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
    if [ "$owner_kind" != "Agent" ]; then
      echo "  broker Deployment $dname is owned by '${owner_kind:-nothing}/${owner_name:-}', not by an Agent CR" >&2
      problems=$((problems + 1))
    elif [ "$dname" != "$owner_name-broker" ]; then
      echo "  broker Deployment $dname is owned by Agent '$owner_name' — the name is not derived from its CR" >&2
      problems=$((problems + 1))
    fi
    if [ "$reps" != "1" ]; then
      echo "  broker Deployment $dname has replicas=${reps:-<unset>}; two brokers for one agent are two writers" >&2
      problems=$((problems + 1))
    fi
  done <<<"$($K -n "$NS" get deploy -l kube-agents/role=actor -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
  echo "$problems"
}

# The controller has to have rendered them first. Polled, not slept on (P9): the Deployments are
# controller-written and appear an unknown time after the CRs are accepted.
waited=0
while :; do
  n="$($K -n "$NS" get deploy -l kube-agents/role=actor -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c .)"
  [ "$n" -ge 2 ] && break
  if [ "$waited" -ge 120 ]; then
    bad "only $n broker Deployment(s) appeared in $NS after 120s; the controller renders one per Agent CR (pod_launcher.go BuildPair)"
    exit 1
  fi
  sleep 5
  waited=$((waited + 5))
done

problems="$(broker_topology_problems)"
if [ "$problems" = "0" ]; then
  pass "every broker Deployment in $NS is owned by exactly one Agent CR, named after it, single-replica"
else
  bad "$problems broker-topology problem(s) in $NS (listed above)"
fi

# Now the pods, one per broker, resolved by OWNERSHIP. A `-l kube-agents/role=actor` pod list would
# answer with whichever broker's pod sorted first and would happily hand back a pod from the
# previous generation of the same Deployment (LSN-024's signature; that is what p3_pod_of_deploy is
# for).
for a in "${AGENTS[@]}"; do
  d="$a-broker"
  if pod="$(p3_pod_of_deploy "$K" "$NS" "$d" 240)"; then
    BROKER_PODS+=("$pod")
    pass "broker Deployment $d owns pod $pod"
  else
    BROKER_PODS+=("")
    bad "broker Deployment $d never produced a pod. The actor ServiceAccount, the mesh certificate or the image is missing — read: kubectl --context $CTX -n $NS describe deploy/$d"
    continue
  fi

  # Running, and on the digest the controller was told to use. Polled (P9) — a pod that exists is
  # not a pod that pulled.
  waited=0
  while :; do
    phase="$($K -n "$NS" get "pod/$pod" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [ "$phase" = "Running" ] && break
    if [ "$waited" -ge 180 ]; then
      reason="$($K -n "$NS" get "pod/$pod" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null)"
      bad "broker pod $pod is '${phase:-<gone>}' after 180s (${reason:-no waiting reason}). A rendered broker that never runs is 09 §11.9 — built, never wired"
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done

  got_img="$($K -n "$NS" get "deploy/$d" -o jsonpath='{.spec.template.spec.containers[?(@.name=="broker")].image}' 2>/dev/null)"
  if [ "$got_img" = "$want_img" ]; then
    pass "$d carries the image the controller was told to use"
  else
    bad "$d carries '${got_img:-<none>}' but the controller's KUBEAGENTS_BROKER_IMAGE is '$want_img' — the rendered brokers are a generation behind the variable"
  fi

  # P1 against the BROKER pod, which is the whole reason `kage-broker` gained a build-input mapping
  # in this unit. Reached by the Deployment's own selector, which is `role=actor` AND `agent=<name>`
  # — the conjunction, so this cannot pick up the co-located broker.
  p1_assert_build_under_test "$K" "$NS" "kube-agents/role=actor,kube-agents/agent=$a"
  case "$?" in
    0) pass "P1: $a's broker pod is the build under test" ;;
    3) echo "DEFERRED: P1 unverifiable for $a's broker (see above)."; exit 3 ;;
    *) bad "P1: $a's broker is not running the build under test" ;;
  esac
done

# ------------------------------------------------------------------------------------------------
# L2-3: each Service resolves to its own broker and to nothing else
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-3: the broker Service selects one pod — its own =="

for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  svc="$a-broker"
  mine="${BROKER_PODS[$i]:-}"
  [ -n "$mine" ] || { bad "no broker pod pinned for $a; cannot judge $svc's endpoints"; continue; }
  want_ip="$($K -n "$NS" get "pod/$mine" -o jsonpath='{.status.podIP}' 2>/dev/null)"

  # Endpoints are computed by the API server from the LIVE selector, which is what makes this a
  # statement about the selector rather than about the manifest that declared it. Polled (P9):
  # endpoints are controller-written and lag readiness.
  waited=0 ips=""
  while :; do
    ips="$($K -n "$NS" get endpoints "$svc" -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{"\n"}{end}' 2>/dev/null | grep -c . )"
    [ "$ips" != "0" ] && break
    [ "$waited" -ge 120 ] && break
    sleep 5
    waited=$((waited + 5))
  done
  got="$($K -n "$NS" get endpoints "$svc" -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" "}{end}' 2>/dev/null | tr -s ' ')"
  got="${got% }"

  if [ -z "$want_ip" ]; then
    bad "$a's broker pod $mine has no podIP; cannot judge $svc"
  elif [ "$got" = "$want_ip" ]; then
    pass "$svc resolves to exactly one endpoint, and it is $a's own broker ($want_ip)"
  else
    bad "$svc resolves to '${got:-<none>}' but $a's own broker is $want_ip. With a platform and a cluster-admin broker co-located, a selector of 'role: actor' alone selects both and one agent's envelopes round-robin into the other's broker (08 §2.3, §2.6)"
  fi
done

# ------------------------------------------------------------------------------------------------
# L2-4a: the ¬, identity arm — an actor writes its own journal, never the fleet's
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-4a: no actor is a fleet-wide journal writer =="

for a in "${AGENTS[@]}"; do
  actor="$($K -n "$NS" get agent "$a" -o jsonpath='{.status.broker.actorServiceAccount}' 2>/dev/null)"
  if [ -z "$actor" ]; then
    bad "$a publishes no status.broker.actorServiceAccount; there is no principal to ask about"
    continue
  fi
  subj="system:serviceaccount:$NS:$actor"

  # THE POSITIVE FIRST, and it is not decoration. Every `want_no` below is satisfied by a principal
  # with no grant at all, so without this the whole arm passes green on a cluster where the actor
  # identity was never applied — which is the LSN-035 vacuity shape and, on this cluster, the
  # default state.
  if [ "$($K auth can-i create actionrecords.kubeagents.x-k8s.io -n "$NS" --as="$subj" 2>/dev/null)" = "yes" ]; then
    pass "$actor may append to the journal in its own namespace"
  else
    bad "$actor may NOT create ActionRecords in $NS. The 06 §2.2.1 grant is absent or misbound, so every negative below would pass for the wrong reason"
  fi

  if [ "$($K auth can-i create actionrecords.kubeagents.x-k8s.io --all-namespaces --as="$subj" 2>/dev/null)" = "no" ]; then
    pass "$actor may not create ActionRecords cluster-wide"
  else
    bad "$actor CAN create ActionRecords in every namespace. The journal half of the grant is a ClusterRoleBinding, which makes this one agent a writer of the whole fleet's journal — the fleet-wide writer V-BRK-012 forbids, wearing an RBAC object rather than a Deployment"
  fi

  if [ "$($K auth can-i create actionrecords.kubeagents.x-k8s.io -n "$ELSEWHERE" --as="$subj" 2>/dev/null)" = "no" ]; then
    pass "$actor may not append to another namespace's journal ($ELSEWHERE)"
  else
    bad "$actor CAN create ActionRecords in $ELSEWHERE, a namespace it has no agent in"
  fi
done

# ------------------------------------------------------------------------------------------------
# L2-4b: the ¬, topology arm — the injected decoy
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-4b: negative control — an unowned actor workload is caught =="

# A Deployment carrying the broker's own role label and owned by no Agent CR: the shape a
# fleet-wide broker would actually have if someone installed one. `replicas: 0` because nothing
# needs to run — the claim under test is about what the topology predicate SEES, and a decoy that
# schedules a pod would also have to be admissible, pullable and cleaned up, none of which is the
# property being controlled for. The image is never pulled.
if $K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $DECOY
  namespace: $NS
  labels:
    kube-agents/role: actor
spec:
  replicas: 0
  selector:
    matchLabels:
      app: $DECOY
  template:
    metadata:
      labels:
        app: $DECOY
    spec:
      containers:
        - name: decoy
          image: registry.k8s.io/pause:3.10
YAML
then
  caught="$(broker_topology_problems 2>/dev/null)"
  if [ "$caught" != "0" ]; then
    pass "the topology predicate rejects an unowned actor Deployment ($caught problem(s)) — L2-2's green is a measurement, not a tidy cluster"
  else
    bad "the topology predicate accepted a Deployment labelled kube-agents/role=actor owned by no Agent CR. L2-2 above cannot fail, so its pass is not evidence (V-MET-014)"
  fi
  $K -n "$NS" delete deploy "$DECOY" --ignore-not-found --wait=false >/dev/null 2>&1
else
  bad "could not create the decoy Deployment, so L2-2's non-vacuity is unmeasured. A negative control that did not run is not a negative control (LSN-048)"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-BRK-012 at L2: PROVEN — one broker per Agent CR, owned by it, on the build under test,"
  echo "  each Service resolving only to its own; no actor is a fleet-wide journal writer; and the"
  echo "  topology predicate was shown to reject an unowned actor workload."
  exit 0
fi
echo "V-BRK-012 at L2: FAILED — see the FAIL lines above."
exit 1
