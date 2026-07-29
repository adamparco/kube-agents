#!/usr/bin/env bash
# V-REV-006 at L2 — "a failed rollback pages AND auto-pauses the agent ¬" (09 §6.3, 04 §5.1).
#
# The L1 half of this check is already green and is a real result: `escalate_envtest_test.go` proves
# the broker RECORDS the escalation against a real CRD schema, and `brake_controller_test.go` proves
# `BrakeReconciler` FANS IT OUT into `spec.operations.paused`, a page, and a receipt. Between them
# they cover the logic completely. What neither can reach is the sentence this script exists for:
#
#     the escalation is fanned out by a PROCESS, running an IMAGE, under a SERVICE ACCOUNT, whose
#     writes are decided by the ADMISSION CHAIN and the RBAC that are actually installed.
#
# Every one of those five words is a place the L1 story can be complete and the shipped system still
# do nothing at all, and P9-T7c-3c-ii-b-2-b is the unit that created all five:
#
#   * The PROCESS. Until this unit, `BrakeReconciler` was wired into no manager — `2-a` says so in
#     its own doc comment. A reconciler nothing constructs passes every unit test it has.
#   * The IMAGE. C-BR is the same binary as the operator with `--controllers=brake`, so the parsing
#     of that flag decides whether the controller is registered at all, in a code path no envtest
#     runs (envtest builds the manager itself and never goes through `cmd/`).
#   * The SERVICE ACCOUNT. `vap-agent-scope-journal` allows the fulfilment write to the literal
#     string `system:serviceaccount:kubeagents-system:kubeagents-brake-controller`. At L1 that
#     string is supplied by the test as a fake username, so the test and the policy agree with each
#     other by construction. Here the name is whatever kustomize actually minted, and one character
#     of drift turns the whole ladder into a silent no-op — the record keeps saying "owed", forever,
#     and nothing in the L1 suite can tell.
#   * The ADMISSION CHAIN. The VAP is the real one, compiled by the real API server, with C-BR's
#     write racing the same object's other writers.
#   * The RBAC. `brake_role.yaml` is hand-written rather than marker-generated (its own header says
#     why), which means no `make manifests` run can catch a verb it forgot. A missing verb here is
#     a `Forbidden` at 3am and a green build.
#
# WHAT IS ASSERTED, in order:
#   L2-1  C-BR runs, under its OWN ServiceAccount, on the same image digest as the operator, and
#         that ServiceAccount is NOT the operator's. (05 §1.5, 06 §4.3.)
#   L2-2  08 §2.7 against the LIVE RBAC, not the file: C-BR can patch `actionrecords/status` and
#         `agents`, and cannot read a Secret, cannot delete an ActionRecord, cannot touch Pods or
#         Deployments. `kubectl auth can-i` asks the API server's authorizer, which is the thing
#         that will actually decide it.
#   L2-3  THE POSITIVE. A real ActionRecord carrying a real rung-5 escalation, written by the
#         principal 06 §4.3 says may write it, fans out into `spec.operations.paused: true` with the
#         escalation's `pauseReason` on a real Agent, an `AgentEscalated` Event, and a fulfilment
#         receipt on the record — all of it through admission.
#   L2-4  THE `¬` (09 §6 line 271, mandatory for this row). Two records that owe nothing must be
#         left completely alone: one with no escalation at all, one whose escalation requests
#         neither effect. A brake that fires on its own resync stops the fleet by itself.
#   L2-5  The 2-a VAP rows, decided by the real API server: C-BR may not write the REQUEST half of
#         `status.escalation`, and may not edit `status.phase`. At L1 those two rows were proven
#         against a policy the test loaded; here they are proven against the policy installed.
#
# HOW L2-4 IS SYNCHRONISED, since this is the part a negative control usually gets wrong. Asserting
# that something did NOT happen needs a barrier, and `sleep 30` is a guess about controller latency
# (precondition P9). So the two silent records are created BEFORE the loud one, and the barrier is
# the loud one's own fan-out: once C-BR has demonstrably processed a record enqueued AFTER these
# two, "these two are untouched" is a statement about a controller that has run, not one that might
# still be starting. The negatives are then re-read once more after that point.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates a namespace, Agent CRs,
# ActionRecords and RBAC, and it PAUSES an agent — which on the live cluster would be an outage
# caused by a test. The guard is the most load-bearing one in this directory.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED (P1/P10 unverifiable).
# Usage: dev/verify/brake-fanout-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=brake-controller — asserted with
#      p1_assert_build_under_test against C-BR's OWN pod, not the operator's. This is the whole
#      point of the run: every claim below is about a controller that did not exist in any image
#      before this unit, so a stale digest here does not weaken the evidence, it inverts it. The
#      operator pod is checked too (L2-1), because the two Deployments share one image and a reload
#      that repointed only one of them is exactly the trap `reload-images.sh` was extended to close.
#   P3 admission-recreate: the Agent CRs, the ActionRecords and the broker RBAC — every one is
#      created fresh in a per-run namespace that this script deletes on entry via `delete ns`, so
#      nothing here was admitted under an earlier generation of the webhook or the VAP. That
#      matters more than usual: L2-5 is an assertion ABOUT admission, and a grandfathered object
#      would be evidence about the rules in force the day it was created.
#   P6 runtime-authoritative: the live `spec.operations.paused` and `pauseReason` on the Agent
#      object, the live `status.escalation` subtree on the ActionRecord, and the live
#      ValidatingAdmissionPolicy the API server compiled — never config/policy/*.yaml, which is the
#      input to the install and not the artifact deciding these writes.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

SYS=kubeagents-system
NS=brake-fanout-l2
AGENT=braked-agent
QUIET_AGENT=unbraked-agent
BROKER_SA=developer-team-brake-fanout-l2-actor
BRAKE_SA="system:serviceaccount:$SYS:kubeagents-brake-controller"
REASON='rollback failed: replay refused, cluster state is not what the record describes'

case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (destructive-test guard)." >&2
    echo "  This script PAUSES an agent. Name the dev cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT" || exit 1

echo "===================================================================="
echo " V-REV-006 at L2 — the brake fans out for real — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

cleanup() {
  $K delete ns "$NS" --wait=false --ignore-not-found >/dev/null 2>&1
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# L2-1: C-BR is running, on the build under test, under its own identity
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-1: C-BR runs as its own principal on the build under test =="

if ! $K -n "$SYS" get deploy kubeagents-brake-controller >/dev/null 2>&1; then
  echo "DEFERRED: Deployment kubeagents-brake-controller does not exist in $SYS."
  echo "  This cluster was installed before C-BR had one. Re-apply the install and reload:"
  echo "    make -C k8s-operator deploy IMG=<digest> KUBE_CONTEXT=$CTX"
  echo "    dev/cluster/reload-images.sh operator $CTX"
  exit 3
fi

p1_assert_build_under_test "$K" "$SYS" control-plane=brake-controller
case "$?" in
  0) pass "P1: the running C-BR is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable for C-BR (see above). Every claim below is about what that"
     echo "  process does, so none of them would be evidence about this commit."
     exit 3 ;;
  *) bad "P1: C-BR is not running the build under test"; exit 1 ;;
esac

# The operator too. Not redundant: both Deployments carry `manager` from ONE image, and the failure
# `reload-images.sh` was extended to prevent is a reload that repointed the controller-manager and
# left C-BR on the previous build. Asserting only the pod under test cannot see a skew, and a skew
# is a cluster where L2-3 below is being decided by two different commits.
p1_assert_build_under_test "$K" "$SYS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the operator is the same build under test — no skew across the shared image" ;;
  3) echo "DEFERRED: P1 unverifiable for the operator (see above)."; exit 3 ;;
  *) bad "P1: operator/C-BR image skew — the two halves of this seam are different builds"; exit 1 ;;
esac

brake_sa="$($K -n "$SYS" get deploy kubeagents-brake-controller -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)"
op_sa="$($K -n "$SYS" get deploy kubeagents-controller-manager -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)"
if [ "$brake_sa" = "kubeagents-brake-controller" ]; then
  pass "C-BR runs as ServiceAccount '$brake_sa'"
else
  bad "C-BR runs as ServiceAccount '${brake_sa:-<none>}', not 'kubeagents-brake-controller' — the VAP allow-list names a principal that never appears, so every fulfilment write will be denied"
fi
if [ -n "$op_sa" ] && [ "$brake_sa" != "$op_sa" ]; then
  pass "C-BR's identity is not the operator's ('$op_sa') — the exporter cannot write status.escalation"
else
  bad "C-BR shares the operator's ServiceAccount ('${op_sa:-<none>}'). The journal exporter's write is what unlocks deletion of the record, so one identity holding both could write the receipt for an escalation and then destroy the evidence of it (06 §4.3)"
fi

# The selector is what makes the process a brake rather than a second operator. A Deployment that
# rolled with the default `--controllers` would run three reconcilers under C-BR's minimal grant and
# fan out nothing, while every assertion above still passed.
args="$($K -n "$SYS" get deploy kubeagents-brake-controller -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].args}' 2>/dev/null)"
case "$args" in
  *--controllers=brake*) pass "C-BR is launched with --controllers=brake" ;;
  *) bad "C-BR's args do not contain --controllers=brake (got: ${args:-<none>})" ;;
esac

# ------------------------------------------------------------------------------------------------
# L2-2: 08 §2.7 asked of the live authorizer
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-2: C-BR's live grant is the narrow one (08 §2.7) =="

can() { # <verb> <resource> [extra args...] -> echoes yes|no
  $K auth can-i "$1" "$2" --as="$BRAKE_SA" "${@:3}" 2>/dev/null
}

want_yes() { # <verb> <resource> <why> [extra...]
  local got; got="$(can "$1" "$2" "${@:4}")"
  if [ "$got" = "yes" ]; then
    pass "C-BR may $1 $2 — $3"
  else
    bad "C-BR may NOT $1 $2 (can-i said '${got:-<empty>}'). $3. brake_role.yaml is hand-written, so no \`make manifests\` run can notice a missing verb"
  fi
}

want_no() { # <verb> <resource> <why> [extra...]
  local got; got="$(can "$1" "$2" "${@:4}")"
  if [ "$got" = "no" ]; then
    pass "C-BR may not $1 $2 — $3"
  else
    bad "C-BR CAN $1 $2 (can-i said '${got:-<empty>}'). $3"
  fi
}

want_yes patch actionrecords.kubeagents.x-k8s.io/status "the fulfilment receipt is the whole seam"
want_yes get   actionrecords.kubeagents.x-k8s.io        "it reads the escalation it fans out"
want_yes patch agents.kubeagents.x-k8s.io               "the pause is a merge patch on spec.operations"
want_yes create events                                  "the page is emitted as an Event" --namespace "$NS"

# 08 §2.7's normative list, asked one resource at a time. `secrets` first because it is the one the
# spec singles out: the controller must hold NO read verb on Secrets, and a ClusterRoleBinding
# conferring cluster-wide write on any workload or Secret is a conformance FAILURE, not a smell.
want_no get    secrets     "08 §2.7 — no read verb on Secrets, at all" --namespace "$NS"
want_no list   secrets     "a list is a read" --namespace "$NS"
want_no create pods        "08 §2.7 — no cluster-wide write on workloads" --namespace "$NS"
want_no delete pods        "08 §2.7 — no cluster-wide write on workloads" --namespace "$NS"
want_no create deployments.apps "08 §2.7 — no cluster-wide write on workloads" --namespace "$NS"
want_no create configmaps  "08 §2.7 — no cluster-wide write on ConfigMaps" --namespace "$NS"
want_no create networkpolicies.networking.k8s.io "08 §2.7 — no cluster-wide write on NetworkPolicies" --namespace "$NS"
# Deleting the record is the retention controller's verb, and the record is the evidence of the
# escalation C-BR just carried out. The one principal that must never hold it is this one.
want_no delete actionrecords.kubeagents.x-k8s.io "the escalation's own audit trail is not C-BR's to destroy" --namespace "$NS"
want_no delete agents.kubeagents.x-k8s.io "the brake stops an agent, it does not remove one" --namespace "$NS"
want_no create agents.kubeagents.x-k8s.io "the brake stops an agent, it does not mint one" --namespace "$NS"
# The negative control for this whole section: `can-i` must be capable of saying yes to something,
# or every `want_no` above is passing because impersonation is silently broken.
if [ "$(can get actionrecords.kubeagents.x-k8s.io --namespace "$NS")" = "yes" ]; then
  pass "impersonation of $BRAKE_SA is live — the denials above are decisions, not failures to ask"
else
  bad "impersonating $BRAKE_SA answers 'no' even to a verb C-BR provably holds; every denial above is vacuous"
fi

# ------------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------------
echo; echo "== fixtures: a namespace, two Agents, a broker identity =="

# P3: a fresh namespace per run, deleted first. Every object below is therefore admitted by the
# webhook and the VAPs currently installed — which is the subject of L2-5, so a grandfathered
# fixture would make that section evidence about a policy that is no longer there.
$K delete ns "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
$K create ns "$NS" >/dev/null 2>&1 || { bad "could not create namespace $NS"; exit 1; }

agent_yaml() { # <name>
  cat <<YAML
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $1
  namespace: $NS
spec:
  tier: developer-team
  scope:
    projectId: brake-fanout-l2-project
    clusterName: cluster-a
    namespace: $NS
  harness:
    clusterName: cluster-a
    location: us-central1
    hermes:
      agentHome: /opt/data
      apiServerSecretRef:
        name: $1-secrets
        key: API_SERVER_KEY
  deployment:
    image: ghcr.io/gke-labs/kube-agents/developer-team-agent
    tag: v0.1.0
    # This fixture is a CR whose spec field gets patched, not a workload. Without scaleToZero its
    # Deployment sits in ImagePullBackOff for the whole run and the next suite reads it as scenery
    # (LSN-026) — and a pod would prove nothing here anyway: 08 §2.4 and 06 §4.4 both say a paused
    # agent keeps running, so "the pod stopped" is not this check's postcondition and asserting it
    # would be asserting the scale-to-zero that V-RUN-012 exists to forbid.
    scaleToZero: true
  security:
    serviceAccountName: developer-team-agent
YAML
}

for a in "$AGENT" "$QUIET_AGENT"; do
  agent_yaml "$a" | $K apply -f - >/dev/null 2>&1 || { bad "could not create Agent $a"; exit 1; }
done
pass "two Agent CRs admitted into $NS"

# The broker identity. 06 §4.3 binds the right to write `status.escalation` to the record's own
# declared `spec.actorServiceAccount`, in the record's own namespace — so the escalation cannot be
# planted by this script's own (cluster-admin) credentials, and impersonating the broker is not a
# shortcut here, it is the only way to write the field the way the system writes it. The Role below
# is the namespace-scoped grant 06 §4.3 describes; nothing else in dev creates one.
$K -n "$NS" create serviceaccount "$BROKER_SA" >/dev/null 2>&1
$K apply -f - >/dev/null 2>&1 <<YAML || { bad "could not create the broker RBAC"; exit 1; }
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: brake-l2-broker, namespace: $NS }
rules:
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [create, get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords/status]
    verbs: [get, patch, update]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: brake-l2-broker, namespace: $NS }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: brake-l2-broker }
subjects:
  - kind: ServiceAccount
    name: $BROKER_SA
    namespace: $NS
YAML
pass "broker ServiceAccount $BROKER_SA created with the 06 §4.3 namespace-scoped grant"

AS_BROKER="--as=system:serviceaccount:$NS:$BROKER_SA"

record_yaml() { # <name> <action-id> <agent>
  cat <<YAML
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionRecord
metadata:
  name: $1
  namespace: $NS
spec:
  actionId: "$2"
  agentRef: { name: $3, namespace: $NS }
  agentIdentity: developer-team/brake-fanout-l2-project/cluster-a/$NS
  actorServiceAccount: $BROKER_SA
  requester: { kind: agent, id: $3 }
  attributionUnverified: false
  trigger: { source: watch, ref: deployment/api-gateway, detail: "brake-fanout-l2" }
  intent: "scale api-gateway to 3 replicas"
  idempotencyKey: "sha256:$1"
  dryRun: false
  classification:
    class: elevated
    blastRadius: { objects: 1, fractionOfScope: 0.02, cap: 25 }
    undoable: true
  targets:
    - { group: apps, version: v1, kind: Deployment, namespace: $NS, name: api-gateway }
  retention:
    class: elevated
    ttl: 720h
    undoWindow: 24h
YAML
}

# ------------------------------------------------------------------------------------------------
# L2-4 (created first, asserted last): the two records that owe nothing
# ------------------------------------------------------------------------------------------------
# Created BEFORE the loud one on purpose. See the header: the barrier for a negative control is the
# controller demonstrably having processed something enqueued LATER, not a sleep.
echo; echo "== ¬ fixtures: two records that owe no fan-out, enqueued before the loud one =="

record_yaml quiet-no-escalation 01J0000000000000000000QUI0 "$QUIET_AGENT" | $K apply -f - >/dev/null 2>&1 \
  || { bad "could not create quiet-no-escalation"; exit 1; }
record_yaml quiet-owes-nothing  01J0000000000000000000QUI1 "$QUIET_AGENT" | $K apply -f - >/dev/null 2>&1 \
  || { bad "could not create quiet-owes-nothing"; exit 1; }

# An escalation that asks for NEITHER effect. `fanoutPending` must reject it on the flags alone —
# this is the shape that distinguishes "C-BR reads the request" from "C-BR reacts to the field
# existing", and the second one pauses an agent on any record that mentions an escalation.
$K -n "$NS" patch actionrecord quiet-owes-nothing $AS_BROKER --subresource=status --type=merge \
  -p '{"status":{"escalation":{"pageRequested":false,"pauseRequested":false,"reason":"recorded, but nothing was asked for","requestedAt":"2026-07-28T00:00:00Z"}}}' >/dev/null 2>&1 \
  || { bad "the broker could not write the ¬ escalation — check the VAP's isOwningBroker row"; exit 1; }
pass "two silent records in place (no escalation / escalation requesting neither effect)"

# ------------------------------------------------------------------------------------------------
# L2-3: the positive
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-3: a recorded rung-5 escalation fans out into a real pause and a real page =="

record_yaml loud-escalation 01J0000000000000000000LOUD "$AGENT" | $K apply -f - >/dev/null 2>&1 \
  || { bad "could not create loud-escalation"; exit 1; }

paused_before="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.spec.operations.paused}' 2>/dev/null)"
if [ "$paused_before" = "true" ]; then
  bad "$AGENT is already paused before the escalation was written — the positive below would be vacuous"
  exit 1
fi
pass "$AGENT starts unpaused"

$K -n "$NS" patch actionrecord loud-escalation $AS_BROKER --subresource=status --type=merge \
  -p "{\"status\":{\"escalation\":{\"pageRequested\":true,\"pauseRequested\":true,\"reason\":\"$REASON\",\"requestedAt\":\"2026-07-28T00:00:00Z\"}}}" >/dev/null 2>&1 \
  || { bad "the broker could not record the escalation"; exit 1; }
pass "the broker recorded a rung-5 escalation requesting both effects"

# Polled, never slept on (precondition P9). The subtree is written by a controller after admission,
# so an unsynchronised read cannot tell "not yet" from "never".
#
# All three fulfilment fields are captured by the SAME read, not by three kubectl calls after the
# poll. C-BR writes them in one status patch, so reading them separately would let a re-reconcile
# land between the reads and produce a mixture of two receipts — and would report `pagedAt: empty`
# for a record that has one, which is the failure mode this whole script exists to distinguish from
# a page that genuinely never happened.
fanned=0
receipt=""
for _ in $(seq 1 60); do
  receipt="$($K -n "$NS" get actionrecord loud-escalation \
    -o jsonpath='{.status.escalation.pausedAt}|{.status.escalation.pagedAt}|{.status.escalation.failure}' 2>/dev/null)"
  if [ -n "${receipt%%|*}" ]; then fanned=1; break; fi
  sleep 2
done
rest="${receipt#*|}"
paged="${rest%%|*}"
failure="${rest#*|}"

if [ "$fanned" -eq 1 ]; then
  pass "C-BR wrote status.escalation.pausedAt — the fan-out ran, through the installed VAP"
else
  bad "status.escalation.pausedAt never appeared within 120s. Either C-BR is not registered in the process, or the installed VAP is denying its write. Check: $K -n $SYS logs deploy/kubeagents-brake-controller --tail=50"
fi

if [ -n "$paged" ]; then
  pass "C-BR wrote status.escalation.pagedAt in the same receipt"
else
  bad "pagedAt is empty. 04 §5.1 rung 5 is 'pages AND auto-pauses' — half of it is not it"
fi

if [ -z "$failure" ]; then
  pass "the fan-out recorded no failure"
else
  bad "C-BR recorded a fan-out failure: $failure"
fi

# The pause is the load-bearing half, and it lands on a DIFFERENT object through a DIFFERENT verb
# than the receipt above, so the receipt is not evidence for it.
paused_now="" ; reason_now=""
for _ in $(seq 1 30); do
  paused_now="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.spec.operations.paused}' 2>/dev/null)"
  [ "$paused_now" = "true" ] && break
  sleep 2
done
reason_now="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.spec.operations.pauseReason}' 2>/dev/null)"

if [ "$paused_now" = "true" ]; then
  pass "the live Agent carries spec.operations.paused=true — the agent is stopped"
else
  bad "the live Agent's spec.operations.paused is '${paused_now:-<unset>}'. The receipt says the fan-out ran; the agent is still acting"
fi
if [ "$reason_now" = "$REASON" ]; then
  pass "pauseReason carries the escalation's reason verbatim — a human running \`resume\` is told why"
else
  bad "pauseReason is '${reason_now:-<unset>}', not the escalation's reason"
fi

# The page. An Event is the page EMISSION, not its delivery — there is no outbound transport in the
# tree and the reconciler's own doc comment says so. What is asserted is that the emission is on the
# object an operator is already looking at, with the stable reason a check can grep for.
ev=""
for _ in $(seq 1 30); do
  ev="$($K -n "$NS" get events --field-selector "reason=AgentEscalated,involvedObject.name=$AGENT" -o jsonpath='{.items[*].message}' 2>/dev/null)"
  [ -n "$ev" ] && break
  sleep 2
done
if [ -n "$ev" ]; then
  pass "an AgentEscalated Event was emitted on $AGENT"
else
  bad "no AgentEscalated Event on $AGENT. C-BR's grant includes events:create, so a missing Event is a missing page, not a missing verb"
fi

# ------------------------------------------------------------------------------------------------
# L2-5: the 2-a VAP rows, decided by the installed policy
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-5: the installed VAP confines C-BR to the fulfilment half =="

if ! $K get validatingadmissionpolicy kube-agents-agent-scope-journal >/dev/null 2>&1; then
  bad "kube-agents-agent-scope-journal is not installed on this cluster — L2-3 above proves the fan-out happens and nothing is confining it"
else
  pass "kube-agents-agent-scope-journal is installed (P6: the compiled policy, not config/policy/*.yaml)"

  # denied_write <what> <record> <patch> <consequence>
  #
  # The denial REASON is asserted, not just the non-zero exit. A patch can fail for reasons that
  # have nothing to do with the policy — a typo'd field name, a missing record, impersonation not
  # permitted — and every one of them would read as "admission stopped it" to a bare rc check,
  # which is a negative control that passes when the thing it guards has been deleted.
  denied_write() {
    local what="$1" rec="$2" patch="$3" consequence="$4" out rc
    out="$($K -n "$NS" patch actionrecord "$rec" --as="$BRAKE_SA" --subresource=status \
      --type=merge -p "$patch" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      bad "C-BR wrote $what. $consequence"
      return
    fi
    case "$out" in
      *ValidatingAdmissionPolicy*|*admission*|*denied*)
        pass "C-BR is DENIED $what by live admission" ;;
      *)
        bad "the $what write failed, but not at admission — so this proves nothing about the policy: $out" ;;
    esac
  }

  # The REQUEST half is the attack the row exists to stop: the party that fans an escalation out
  # must not be the party that can declare one.
  denied_write "the request half of status.escalation" quiet-no-escalation \
    '{"status":{"escalation":{"pageRequested":true,"pauseRequested":true,"reason":"self-authored"}}}' \
    "'A failed rollback pages AND auto-pauses' would be self-attested by the party with the motive (vap-agent-scope-journal, validation 5)"

  denied_write "status.phase" loud-escalation \
    '{"status":{"phase":"Verified"}}' \
    "Its grant is the fulfilment half of status.escalation and nothing else"
fi

# ------------------------------------------------------------------------------------------------
# L2-4: assert the negatives, now that C-BR has demonstrably run
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-4 (¬): the records that owe nothing were left alone =="

if [ "$fanned" -eq 0 ]; then
  bad "the ¬ section cannot be evidence: C-BR never processed the loud record, so 'it did not touch these two' is indistinguishable from 'it is not running'"
else
  for rec in quiet-no-escalation quiet-owes-nothing; do
    touched=""
    for f in pagedAt pausedAt failure; do
      v="$($K -n "$NS" get actionrecord "$rec" -o jsonpath="{.status.escalation.$f}" 2>/dev/null)"
      [ -n "$v" ] && touched="$touched $f=$v"
    done
    if [ -z "$touched" ]; then
      pass "$rec has no fulfilment fields — C-BR left it alone"
    else
      bad "$rec was fanned out anyway ($touched). A brake that fires on a record owing nothing stops the fleet on its own resync"
    fi
  done

  qp="$($K -n "$NS" get agent "$QUIET_AGENT" -o jsonpath='{.spec.operations.paused}' 2>/dev/null)"
  if [ "$qp" != "true" ]; then
    pass "$QUIET_AGENT is still unpaused"
  else
    bad "$QUIET_AGENT was paused by a record that asked for nothing"
  fi

  qev="$($K -n "$NS" get events --field-selector "reason=AgentEscalated,involvedObject.name=$QUIET_AGENT" -o jsonpath='{.items[*].message}' 2>/dev/null)"
  if [ -z "$qev" ]; then
    pass "no AgentEscalated Event on $QUIET_AGENT — nothing paged a human about a non-incident"
  else
    bad "$QUIET_AGENT was paged about: $qev"
  fi
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " V-REV-006 at L2: ALL CHECKS PASSED"
else
  echo " V-REV-006 at L2: FAILURES ABOVE"
fi
echo "===================================================================="
exit "$fail"
