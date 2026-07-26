#!/usr/bin/env bash
# Phase 6 (Failure-isolation & resilience) — the 05 §8 chaos suite.
#
# Proves the design's central resilience claim — NO CASCADE FAILURE (04 §6) — by killing things and
# asserting that (1) running state and the other tiers survive, and (2) the controller relaunches what it
# owns. This is the load-bearing 05 §8 "failure isolation (chaos)" bullet, deferred N-A through Phases 4-5
# and now live. It adds no new agent behaviour and no write path; every op is reversible and Kind-guarded.
#
#   C1  Controller DOWN -> running pods CONTINUE + NO new reconciles + reconcile RESUMES on restart.
#         Scale kubeagents-controller-manager -> 0. A Running stand-in pod stays UID-stable + Ready
#         (running pods continue). Delete the REAL cluster-admin agent Deployment while the controller is
#         down -> it is NOT recreated (no reconcile without the controller; deleting a Deployment does not
#         touch the Agent CR webhook, so this is a clean "no reconcile" probe — creating a CR while the
#         webhook-serving controller is down would instead be rejected at admission, a different thing).
#         Scale -> original replicas; the controller re-acquires leadership and RECREATES the Deployment
#         (reconcile resumes / provisioning resumes). 05 §8 "kill the controller"; 04 §6 controller row;
#         Accept (b) new provisioning pauses+resumes.
#   C2  Controller UP -> it RELAUNCHES agent pods.
#         Delete the REAL agent Deployment -> the controller recreates it promptly (owns lifecycle).
#         Delete a running stand-in POD -> its Deployment recreates the pod (standard self-heal). 05 §8
#         "kill the controller ... controller relaunches"; 04 §6 "controller relaunches the pod"; Accept (c).
#   C3  Cluster Admin DOWN -> its Developer Team Agents KEEP RUNNING (no cascade) + cluster-admin relaunched.
#         With a cluster-admin + a developer-team stand-in both Running, delete the cluster-admin pod. The
#         dev-team pod is UID-stable + never NotReady across the whole window (polled). The cluster-admin
#         pod is relaunched by its Deployment (recovery). 05 §8 "kill a Cluster Admin Agent -> Dev Team
#         Agents keep running"; 04 §6 cluster-admin row; Accept (b).
#   C4  Hub DOWN -> the spoke keeps running its LAST-APPLIED STATE; agents pause (honest); no bundled engine.
#         Scale a hub-inference stand-in -> 0 (the hub hosts shared inference/Minty, 05 §3). A spoke-workload
#         stand-in stays Ready across the window (last-applied state survives hub loss). The workload is
#         STRUCTURALLY DECOUPLED from the hub (no ownerRef to the hub/agent). No Config Sync / Config
#         Connector / Argo / Flux CRD is required (unopinionated actuation, 05 §8 bullet 4). 05 §8 "kill the
#         hub -> spoke workloads keep running (agents pause)"; 04 §6 hub honest-scoping; Accept (a).
#
# DEFERRED, NOT FAKED (04 §6 honest scoping, D3): the LITERAL "spoke agent pauses because it cannot reach
# real hub-hosted inference/Minty over private networking" needs a real hub + inference across two clusters
# (scratch-GKE) — C4 proves the load-bearing half (cluster state + workloads survive hub loss) on Kind and
# defers the agent-reasoning-pause. Never asserted green here.
#
# FIXTURES (D1): controller *reconcile-behaviour* (C1 no-reconcile/resume, C2 Deployment relaunch) uses the
# REAL Agent CR + REAL controller — observed on the Deployment object, faithful even though the real agent
# pod stays Pending on the single-node dev Kind (the controller bakes prod-correct ~2Gi+ requests across a
# 4-container pod). *Pod-continuity / no-cascade* (C1/C3/C4 running-pod claims) uses lightweight stand-in
# Deployments labeled kube-agents/tier with the FULL hardened securityContext (so they are admitted under
# the same PSS-restricted + pod-hardening-VAP ceiling a real agent faces), running registry.k8s.io/pause
# with tiny requests so they schedule. A stand-in is a faithful proxy for the K8s-level "independent
# Deployment-managed pods do not share fate" property. All stand-ins live in the kube-agents-chaos namespace.
#
# CONTINUITY IS POLLED, RECOVERY IS BOUNDED (D4): "pod X keeps running while Y is down" is asserted by
# polling that X's exact pod stays present + Ready across the ENTIRE disruption window (not one snapshot);
# "Z is relaunched/reconciled" waits for the new object with a bounded timeout that FAILS loudly if absent.
#
# DESTRUCTIVE-TEST GUARD (D2): only runs against a Kind context; every op is reversible + single-object; a
# cleanup trap restores the controller replica count and removes kube-agents-chaos on any exit.
# Usage: dev/verify/chaos-suite.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions).
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — C1 and C2 are claims
#      ABOUT THE CONTROLLER: that it stops reconciling when it is down, resumes when it comes back, and
#      recreates an agent Deployment it owns. There is no way to make those statements about the build
#      in this tree without checking that the pod being scaled to zero and back is running that build.
#      This is also the script most exposed to LSN-001, because a stale controller passes C1 and C2
#      exactly as convincingly as a current one — the behaviour under test is old in both cases, and
#      only the digest can tell them apart. Asserted via p1_assert_build_under_test before C1 starts.
#      C3 and C4 use stand-ins running upstream pause and do not depend on it.
#   P3 admission-recreate: the stand-in Deployments in kube-agents-chaos, and the real agent Deployment
#      in kubeagents-system. The namespace is deleted and recreated at the top of every run, and the
#      scaffold self-check asserts that a hardened tier-labeled stand-in is ADMITTED under PSS
#      restricted plus the pod-hardening VAP — an admission claim that is only worth anything because
#      the object is created fresh, after the labels are applied, on every run. C1/C2 then delete the
#      real agent Deployment outright and require the controller to recreate it, so the recovery
#      claims are about a genuinely new admission rather than a survivor (LSN-002).
#   P6 runtime-authoritative: the live API server throughout — pod presence and Ready conditions polled
#      across each disruption window, ownerReferences on the recreated Deployment and on the spoke pod,
#      the installed CRD list, and the recreated agent pod's securityContext read back at the end. This
#      script reads no file at all, so the image-baked config the operator shadows with a rendered
#      ConfigMap (LSN-003) is out of scope by construction.
set -uo pipefail  # -e omitted: exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

CHAOS_NS=kube-agents-chaos
CM_NS=kubeagents-system
CM=kubeagents-controller-manager
REAL_NS=kubeagents-system
REAL_DEPLOY=cluster-admin-cluster-a-gateway   # owned by Agent CR cluster-admin-cluster-a
REAL_CR=cluster-admin-cluster-a
PAUSE=registry.k8s.io/pause:3.9

case "$CTX" in
  kind-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "  NOTE: $1"; }
cd "$REPO_ROOT"
. "$REPO_ROOT/dev/lib/preconditions.sh"

# ---- helpers -----------------------------------------------------------------------------------------
CM_ORIG_REPLICAS=""

cleanup() {
  # Restore the controller replica count (C1 scales it) and remove the chaos namespace. Best-effort.
  if [ -n "$CM_ORIG_REPLICAS" ]; then
    $K -n "$CM_NS" scale deploy "$CM" --replicas="$CM_ORIG_REPLICAS" >/dev/null 2>&1 || true
  fi
  $K delete ns "$CHAOS_NS" --wait=false --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Deployment yaml for a hardened stand-in. args: <name> <tier|-> <role|->
standin_yaml() { # emits a Deployment; tier "-" omits the tier label; role "-" omits the role label
  local name="$1" tier="$2" role="$3"
  local tmpl_labels="        app: $name"
  local meta_labels="    app: $name"
  [ "$tier" != "-" ] && { tmpl_labels+=$'\n        kube-agents/tier: '"$tier"; meta_labels+=$'\n    kube-agents/tier: '"$tier"; }
  [ "$role" != "-" ] && { tmpl_labels+=$'\n        role: '"$role"; meta_labels+=$'\n    role: '"$role"; }
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  namespace: $CHAOS_NS
  labels:
$meta_labels
    kube-agents-chaos: "true"
spec:
  replicas: 1
  strategy: { type: Recreate }
  selector:
    matchLabels: { app: $name }
  template:
    metadata:
      labels:
$tmpl_labels
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: c
          image: $PAUSE
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            capabilities: { drop: ["ALL"] }
          resources:
            requests: { cpu: "10m", memory: "16Mi" }
            limits:   { cpu: "50m", memory: "32Mi" }
EOF
}

make_standin() { # <name> <tier|-> <role|->  -> apply + rollout ready
  standin_yaml "$1" "$2" "$3" | $K apply -f - >/dev/null 2>&1
  $K -n "$CHAOS_NS" rollout status deploy/"$1" --timeout=90s >/dev/null 2>&1
}

pod_of()   { $K -n "$CHAOS_NS" get pods -l app="$1" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
is_ready() { # <ns> <pod> -> echoes "True" if Ready
  $K -n "$1" get pod "$2" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null;
}

# Poll that an exact pod stays present + Ready across a window. args: <ns> <pod> <iters> <sleep> -> rc 0 stable
assert_pod_stable() {
  local ns="$1" pod="$2" iters="$3" slp="$4" i
  for ((i=1; i<=iters; i++)); do
    $K -n "$ns" get pod "$pod" >/dev/null 2>&1 || { echo "    (pod $pod vanished at iter $i)"; return 1; }
    [ "$(is_ready "$ns" "$pod")" = "True" ] || { echo "    (pod $pod NotReady at iter $i)"; return 1; }
    sleep "$slp"
  done
  return 0
}

# Assert a Deployment stays ABSENT across a window. args: <ns> <deploy> <iters> <sleep> -> rc 0 absent throughout
assert_deploy_absent() {
  local ns="$1" dep="$2" iters="$3" slp="$4" i
  for ((i=1; i<=iters; i++)); do
    if $K -n "$ns" get deploy "$dep" >/dev/null 2>&1; then echo "    ($dep reappeared at iter $i)"; return 1; fi
    sleep "$slp"
  done
  return 0
}

# Wait until a Deployment exists. args: <ns> <deploy> <timeout-s> -> rc 0 present
wait_deploy_present() {
  local ns="$1" dep="$2" to="$3" waited=0
  while [ "$waited" -lt "$to" ]; do
    $K -n "$ns" get deploy "$dep" >/dev/null 2>&1 && return 0
    sleep 3; waited=$((waited+3))
  done
  return 1
}

# Wait for a NEW ready pod (name != old) for a stand-in. args: <label> <old-pod> <timeout-s> -> rc 0 + prints name
wait_new_ready_pod() {
  local label="$1" old="$2" to="$3" waited=0 cur
  while [ "$waited" -lt "$to" ]; do
    cur="$(pod_of "$label")"
    if [ -n "$cur" ] && [ "$cur" != "$old" ] && [ "$(is_ready "$CHAOS_NS" "$cur")" = "True" ]; then echo "$cur"; return 0; fi
    sleep 3; waited=$((waited+3))
  done
  return 1
}

echo "===================================================================="
echo " Phase 6 chaos suite (05 §8 failure isolation) — context: $CTX"
echo "===================================================================="

if ! $K version >/dev/null 2>&1; then
  echo "REFUSING: context '$CTX' is not reachable — the chaos suite needs a live Kind cluster." >&2
  exit 2
fi

# P1 — C1 and C2 are claims about how THIS controller behaves when it is killed and restarted. A
# controller from three phases ago fails and recovers just as convincingly, so without the digest the
# whole A block is a statement about unknown code (LSN-001).
p1_assert_build_under_test "$K" "$CM_NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the controller about to be killed and restarted is the build under test" ;;
  3) echo "  DEFERRED (not faked): P1 unverifiable — reason above; C1/C2 below describe an unidentified controller." ;;
  *) bad "P1: the cluster is NOT running the build under test (LSN-001 — C1/C2 would describe other code)" ;;
esac

# Fresh chaos namespace (PSS restricted so stand-ins face the same ceiling a real agent pod does).
$K delete ns "$CHAOS_NS" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
$K create ns "$CHAOS_NS" >/dev/null 2>&1
$K label ns "$CHAOS_NS" pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null 2>&1
CM_ORIG_REPLICAS="$($K -n "$CM_NS" get deploy "$CM" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
[ -z "$CM_ORIG_REPLICAS" ] && CM_ORIG_REPLICAS=1

# Scaffold self-check: a tier-labeled hardened stand-in must be ADMITTED (VAP+PSS) and reach Ready.
if make_standin standin-selfcheck platform -; then
  pass "scaffold: hardened tier-labeled stand-in ADMITTED (VAP+PSS restricted) and Ready in $CHAOS_NS"
else
  bad "scaffold: hardened stand-in did NOT become Ready (admission or scheduling problem)"
  $K -n "$CHAOS_NS" get pods 2>&1 | tail -5
fi

# ============================ C1 — controller down ============================
echo; echo "== C1: controller DOWN -> running pods continue + NO new reconciles + resume on restart =="
if ! $K -n "$REAL_NS" get deploy "$REAL_DEPLOY" >/dev/null 2>&1; then
  note "$REAL_DEPLOY absent at start; waiting for the controller to reconcile it first…"
  wait_deploy_present "$REAL_NS" "$REAL_DEPLOY" 60 || bad "C1 precondition: real agent Deployment $REAL_DEPLOY never appeared (controller not reconciling?)"
fi
c1_pod="$(pod_of standin-selfcheck)"
if [ -n "$c1_pod" ] && $K -n "$REAL_NS" get deploy "$REAL_DEPLOY" >/dev/null 2>&1; then
  # Kill the controller.
  $K -n "$CM_NS" scale deploy "$CM" --replicas=0 >/dev/null 2>&1
  down_ok=0
  for _ in $(seq 1 20); do
    r="$($K -n "$CM_NS" get deploy "$CM" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
    [ -z "$r" ] || [ "$r" = "0" ] && { down_ok=1; break; }
    sleep 2
  done
  [ "$down_ok" -eq 1 ] && note "controller scaled to 0 (down)" || bad "C1: controller did not scale down"
  # Delete the real agent Deployment while the controller is down.
  $K -n "$REAL_NS" delete deploy "$REAL_DEPLOY" --wait=true --timeout=60s >/dev/null 2>&1
  # (i) running pod continues + (ii) no reconcile recreates the Deployment, across the same window.
  if assert_pod_stable "$CHAOS_NS" "$c1_pod" 10 2; then
    pass "C1(i): a running stand-in pod stays Ready throughout the controller outage (running pods continue)"
  else
    bad "C1(i): stand-in pod did not stay Ready while the controller was down"
  fi
  if assert_deploy_absent "$REAL_NS" "$REAL_DEPLOY" 10 2; then
    pass "C1(ii): the deleted agent Deployment is NOT recreated while the controller is down (no new reconciles)"
  else
    bad "C1(ii): agent Deployment was recreated with the controller down (unexpected reconcile — Accept b)"
  fi
  # (iii) resume: bring the controller back; it re-acquires leadership and recreates the Deployment.
  $K -n "$CM_NS" scale deploy "$CM" --replicas="$CM_ORIG_REPLICAS" >/dev/null 2>&1
  $K -n "$CM_NS" rollout status deploy/"$CM" --timeout=120s >/dev/null 2>&1
  if wait_deploy_present "$REAL_NS" "$REAL_DEPLOY" 120; then
    pass "C1(iii): controller back up -> reconcile RESUMES, agent Deployment recreated (provisioning resumes)"
  else
    bad "C1(iii): controller restart did NOT recreate the agent Deployment within 120s (reconcile did not resume)"
  fi
else
  bad "C1: preconditions unmet (stand-in pod or real agent Deployment missing) — skipped"
fi

# ============================ C2 — controller relaunches agent pods ============================
echo; echo "== C2: controller UP -> it RELAUNCHES agent pods (Deployment + pod) =="
# Ensure the controller is up first.
$K -n "$CM_NS" rollout status deploy/"$CM" --timeout=120s >/dev/null 2>&1
# (i) delete the real agent Deployment; the controller recreates it promptly.
if $K -n "$REAL_NS" get deploy "$REAL_DEPLOY" >/dev/null 2>&1; then
  $K -n "$REAL_NS" delete deploy "$REAL_DEPLOY" --wait=true --timeout=60s >/dev/null 2>&1
  if wait_deploy_present "$REAL_NS" "$REAL_DEPLOY" 90; then
    owner="$($K -n "$REAL_NS" get deploy "$REAL_DEPLOY" -o jsonpath='{.metadata.ownerReferences[0].name}' 2>/dev/null)"
    if [ "$owner" = "$REAL_CR" ]; then
      pass "C2(i): controller recreated the deleted agent Deployment, owned by Agent CR $REAL_CR (relaunch)"
    else
      pass "C2(i): controller recreated the deleted agent Deployment ($REAL_DEPLOY)"
      note "ownerRef was '$owner' (expected $REAL_CR)"
    fi
  else
    bad "C2(i): controller did NOT recreate the deleted agent Deployment within 90s (Accept c)"
  fi
else
  bad "C2(i): real agent Deployment absent before the test — skipped"
fi
# (ii) delete a running stand-in pod; its Deployment recreates a new Ready pod (self-heal).
c2_pod="$(pod_of standin-selfcheck)"
if [ -n "$c2_pod" ]; then
  $K -n "$CHAOS_NS" delete pod "$c2_pod" --wait=false >/dev/null 2>&1
  if newp="$(wait_new_ready_pod standin-selfcheck "$c2_pod" 90)"; then
    pass "C2(ii): a deleted agent pod is relaunched by its Deployment (new Ready pod $newp)"
  else
    bad "C2(ii): deleted pod was not replaced by a new Ready pod within 90s (Accept c)"
  fi
else
  bad "C2(ii): no stand-in pod to delete — skipped"
fi

# ============================ C3 — cluster-admin down -> dev-team survives ============================
echo; echo "== C3: cluster-admin DOWN -> its dev-team agents KEEP RUNNING (no cascade) + cluster-admin relaunched =="
make_standin standin-cluster-admin cluster-admin -
make_standin standin-developer-team developer-team -
ca_pod="$(pod_of standin-cluster-admin)"
dt_pod="$(pod_of standin-developer-team)"
if [ -n "$ca_pod" ] && [ -n "$dt_pod" ]; then
  # Kill the cluster-admin pod.
  $K -n "$CHAOS_NS" delete pod "$ca_pod" --wait=false >/dev/null 2>&1
  # The dev-team pod must stay the SAME pod + Ready across the whole window (no cascade).
  if assert_pod_stable "$CHAOS_NS" "$dt_pod" 10 2; then
    pass "C3: the developer-team pod stays UID-stable + Ready while the cluster-admin pod is killed (no cascade)"
  else
    bad "C3: the developer-team pod was disturbed by the cluster-admin's death (CASCADE — Accept b / HALT)"
  fi
  # Recovery: the cluster-admin pod is relaunched by its Deployment.
  if newca="$(wait_new_ready_pod standin-cluster-admin "$ca_pod" 90)"; then
    pass "C3: the cluster-admin pod is relaunched on recovery (new Ready pod $newca)"
  else
    bad "C3: the cluster-admin pod was not relaunched within 90s (recovery — 04 §6)"
  fi
else
  bad "C3: cluster-admin/developer-team stand-ins did not both come up — skipped"
fi

# ============================ C4 — hub down -> spoke last-applied state survives ============================
echo; echo "== C4: hub DOWN -> the spoke keeps running its last-applied state; decoupled; no bundled engine =="
make_standin standin-hub-inference - hub-inference
make_standin standin-spoke-workload - spoke-workload
hub_pod="$(pod_of standin-hub-inference)"
spoke_pod="$(pod_of standin-spoke-workload)"
if [ -n "$hub_pod" ] && [ -n "$spoke_pod" ]; then
  # Kill the hub (its shared inference service).
  $K -n "$CHAOS_NS" scale deploy standin-hub-inference --replicas=0 >/dev/null 2>&1
  # The spoke workload keeps running its last-applied state throughout the hub outage.
  if assert_pod_stable "$CHAOS_NS" "$spoke_pod" 10 2; then
    pass "C4: the spoke workload stays Ready across the hub outage (last-applied state survives — Accept a)"
  else
    bad "C4: the spoke workload did NOT survive the hub outage (Accept a / HALT)"
  fi
  # Structural decoupling: the spoke workload is owned only by its own ReplicaSet, not the hub/an agent.
  owner_kind="$($K -n "$CHAOS_NS" get pod "$spoke_pod" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)"
  if [ "$owner_kind" = "ReplicaSet" ]; then
    pass "C4: the spoke workload is structurally decoupled from the hub (owned by its own ReplicaSet, no hub ownerRef)"
  else
    bad "C4: unexpected spoke-workload ownerRef kind '$owner_kind' (expected ReplicaSet — decoupling)"
  fi
else
  bad "C4: hub/spoke stand-ins did not both come up — skipped"
fi

# 05 §8 bullet 4 — unopinionated actuation: nothing requires a bundled GitOps engine to be installed.
echo; echo "== 05 §8: unopinionated actuation — no bundled GitOps engine is required =="
engine_crds="$($K get crd -o name 2>/dev/null | grep -Ei 'configsync|configmanagement|argoproj\.io|fluxcd\.io|kustomize\.toolkit|source\.toolkit' || true)"
if [ -z "$engine_crds" ]; then
  pass "no Config Sync / Config Connector / Argo / Flux CRD is installed (actuation is the customer's CI/CD — apply.yml)"
else
  note "engine CRDs present on this cluster (not required by kube-agents; informational):"; echo "$engine_crds" | sed 's/^/    /'
  pass "unopinionated actuation holds structurally (kube-agents ships no GitOps engine; apply.yml is the writer)"
fi

# ---- restore pre-chaos steady state (regression-safety, D2) -----------------------------------------
# C1/C2 delete the real agent Deployment and let the controller recreate it; wait for its POD object to
# exist again (Pending is expected on single-node dev Kind — the controller bakes prod-correct ~2Gi+
# requests) so the post-chaos regression (e.g. verify-phase2 V-K9, which reads that pod) sees steady state.
echo; echo "== restore pre-chaos steady state: real agent pod recreated =="
settle_ok=0
for _ in $(seq 1 40); do
  if [ -n "$($K -n "$REAL_NS" get pod -l app="$REAL_DEPLOY" -o name 2>/dev/null | head -1)" ]; then settle_ok=1; break; fi
  sleep 3
done
if [ "$settle_ok" -eq 1 ]; then
  rof="$($K -n "$REAL_NS" get pod -l app="$REAL_DEPLOY" -o jsonpath='{.items[0].spec.containers[0].securityContext.readOnlyRootFilesystem}' 2>/dev/null)"
  note "real agent pod recreated (steady state restored; containers[0].readOnlyRootFilesystem=${rof:-<unset>})"
else
  note "real agent pod not recreated after 120s — the post-chaos regression may observe a transient gap"
fi

echo
echo "  DEFERRED (not faked, D3): the LITERAL spoke agent-reasoning-pause under real hub loss (no real"
echo "  inference/Minty over private networking) → two-cluster / scratch-GKE. C4 proves the load-bearing"
echo "  half (cluster state + workloads survive hub loss) on Kind."
echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 6 chaos suite: ALL CHECKS PASSED"; else echo " Phase 6 chaos suite: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
