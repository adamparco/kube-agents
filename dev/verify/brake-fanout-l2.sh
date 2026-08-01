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
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates namespaces, Agent CRs,
# ActionRecords and RBAC, it re-seeds the fleet's parent chain, and it PAUSES an agent — which on
# the live cluster would be an outage caused by a test. The guard is the most load-bearing one in
# this directory.
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
#   P3 admission-recreate: every SUBJECT is created fresh on every run — the Agent CRs are deleted
#      and re-applied, the broker RBAC is re-applied, the parent chain is re-seeded
#      (`seed_parent_agent` deletes before it applies), and each run mints its own ActionRecord IDs
#      so no record is ever reused. Nothing asserted below was admitted under an earlier generation
#      of the webhook or the VAP, which matters more than usual here: L2-5 is an assertion ABOUT
#      admission, and a grandfathered object would be evidence about rules that are no longer there.
#      Events are matched on `involvedObject.uid`, not name, so a page from a previous run against a
#      same-named Agent cannot be read as this run's — that direction is a FALSE GREEN on L2-3.
#
#      WHAT IS *NOT* DELETED, AND WHY THAT IS THE JOURNAL WORKING. The two namespaces are reused,
#      not recreated, because a namespace holding an ActionRecord CANNOT BE DELETED — and must not
#      be. `kube-agents-journal-retention` denies DELETE to every principal but the retention path,
#      and denies it even to them until `status.exported.confirmed` is true; the namespace
#      controller is not on that list, so `kubectl delete ns` strands the namespace in Terminating
#      permanently. On a cluster with no audit sink nothing is ever confirmed, which the operator
#      says out loud ("the record will be retained indefinitely because the export is the durable
#      record", journal_reconciler.go), so these records are undeletable by design. The suite
#      therefore leaves them, names them uniquely per run so they can never be mistaken for this
#      run's, and reports what it left at the end rather than pretending the cluster is clean. It
#      does NOT fabricate an export confirmation to get its namespace back: writing
#      `exported.confirmed` for an export that never happened is forging the audit trail 05 §1.2
#      makes the durable record, and no test's tidiness is worth teaching that idiom to the repo.
#   P6 runtime-authoritative: the live `spec.operations.paused` and `pauseReason` on the Agent
#      object, the live `status.escalation` subtree on the ActionRecord, and the live
#      ValidatingAdmissionPolicy the API server compiled — never config/policy/*.yaml, which is the
#      input to the install and not the artifact deciding these writes.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

SYS=kubeagents-system
# `-braked` rather than the bare suite name: the original `brake-fanout-l2` namespace is wedged in
# Terminating on the dev cluster forever, because the first version of this script called
# `delete ns` on a namespace holding ActionRecords before anyone had worked out that the journal
# forbids exactly that. It is not recoverable without forging an export. Reusing these two from
# here on means it cannot happen again.
NS=brake-fanout-l2-braked
# WHY THE QUIET AGENT NEEDS A NAMESPACE OF ITS OWN. This suite needs two developer-team Agents that
# differ only in whether their record owes a fan-out. It cannot have them in one namespace: 06 §1.2
# V-5 makes (tier, scope) unique across the fleet, and a developer-team agent's scope is
# (projectId, clusterName, namespace) — so two of them in `$NS` are the SAME scope identity and the
# webhook rejects the second. The placement clause then forces the rest: `metadata.namespace` must
# equal `spec.scope.namespace`, so a distinct scope is a distinct namespace, not just a distinct
# name. Both records still live in `$NS` under one broker identity; only `agentRef` crosses.
NS_QUIET=brake-fanout-l2-quiet
AGENT=braked-agent
QUIET_AGENT=unbraked-agent
# The scope both fixtures sit under. Not arbitrary: 06 §1.2 V-6 requires the child's scope to be
# within its parent's, and the parent here is the SHIPPED cluster-admin manifest seeded below, whose
# scope is exactly this project and cluster. Inventing a project id makes the fixture unadmittable.
PROJECT_ID=your-gcp-project-id
CLUSTER_NAME=cluster-a
PARENT_AGENT=cluster-admin-cluster-a
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

# apply_fixture <what> <<stdin: manifest>  -- applies and, on refusal, prints why.
# The API server's own message is the diagnosis; without it a failed fixture costs an entire L2 run
# (and a cluster round trip) just to learn which validation rejected it.
apply_fixture() { # <what>
  local what="$1" out
  if out="$($K apply -f - 2>&1)"; then
    return 0
  fi
  bad "could not create $what: $out"
  return 1
}
cd "$REPO_ROOT" || exit 1

echo "===================================================================="
echo " V-REV-006 at L2 — the brake fans out for real — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/parent-chain.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

seeded=()
cleanup() {
  # The Agents and the seeded parents go; the namespaces and their ActionRecords stay, for the
  # reason in the P3 note above. Saying so is part of the cleanup: whoever finds these later should
  # not have to work out whether a run died halfway.
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  $K -n "$NS_QUIET" delete agent "$QUIET_AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  unseed_parent_agents "$K" "${seeded[@]:-}"
  echo
  echo "LEFT BEHIND (by design): ActionRecords ${REC_QUIET_NONE:-<none created>} ${REC_QUIET_IDLE:-} ${REC_LOUD:-} in $NS."
  echo "  The journal is append-only and this cluster has no audit sink, so nothing is permitted to"
  echo "  delete them (kube-agents-journal-retention). They are inert; each run mints new IDs."
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT

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

# A SUBRESOURCE IS NOT A PATH SEGMENT HERE. `kubectl auth can-i <verb> <type>/<thing>` parses the
# slash as TYPE/**NAME**, not TYPE/SUBRESOURCE — so `can-i patch actionrecords/status` asks "may I
# patch the ActionRecord *named* status", which is a different question with a different answer.
# Both directions of the mistake are wrong and one of them is silent: a `want_yes` on the positional
# form reads as a missing verb against a grant that actually holds it (this check's first red, and
# the RBAC was fine), while a `want_no` on it is VACUOUSLY GREEN — it passes because no object of
# that name exists, not because the authority is absent, and it would keep passing after someone
# widened the role. Subresources go through `--subresource=`. The guard below makes the wrong form
# impossible to write rather than merely discouraged, because the failure mode is a check that
# reports on something other than what it claims.
can() { # <verb> <resource> [extra args...] -> echoes yes|no
  case "$2" in
    */*)
      bad "check bug: can() was passed '$2'. kubectl parses TYPE/NAME, not TYPE/SUBRESOURCE — pass --subresource= instead"
      echo "malformed"
      return
      ;;
  esac
  $K auth can-i "$1" "$2" --as="$BRAKE_SA" "${@:3}" 2>/dev/null
}

# subj renders what was actually asked, so the message names the subresource even though the
# argument does not: "patch actionrecords.../status", not a bare "patch actionrecords...".
subj() { # <resource> [extra...] -> resource[/subresource]
  local res="$1" a
  shift
  for a in "$@"; do
    case "$a" in --subresource=*) res="$res/${a#--subresource=}" ;; esac
  done
  printf '%s' "$res"
}

want_yes() { # <verb> <resource> <why> [extra...]
  local got what; got="$(can "$1" "$2" "${@:4}")"; what="$(subj "$2" "${@:4}")"
  if [ "$got" = "yes" ]; then
    pass "C-BR may $1 $what — $3"
  else
    bad "C-BR may NOT $1 $what (can-i said '${got:-<empty>}'). $3. brake_role.yaml is hand-written, so no \`make manifests\` run can notice a missing verb"
  fi
}

want_no() { # <verb> <resource> <why> [extra...]
  local got what; got="$(can "$1" "$2" "${@:4}")"; what="$(subj "$2" "${@:4}")"
  if [ "$got" = "no" ]; then
    pass "C-BR may not $1 $what — $3"
  else
    bad "C-BR CAN $1 $what (can-i said '${got:-<empty>}'). $3"
  fi
}

want_yes patch actionrecords.kubeagents.x-k8s.io "the fulfilment receipt is the whole seam" --subresource=status
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
# Reused if present, created if not. Then every SUBJECT inside them is removed, so that what this
# run asserts about was admitted by the rules in force right now (P3).
for n in "$NS" "$NS_QUIET"; do
  if ! $K get ns "$n" >/dev/null 2>&1; then
    if ! out="$($K create ns "$n" 2>&1)"; then
      bad "could not create namespace $n: $out"
      exit 1
    fi
  fi
done
$K -n "$NS"       delete agent "$AGENT"       --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1
$K -n "$NS_QUIET" delete agent "$QUIET_AGENT" --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1

# The tier above, as SETUP and never as a subject (see dev/lib/parent-chain.sh). 06 §1.2 V-6 refuses
# a child whose `parentRef` names an Agent that does not exist, because the authority ceiling is
# then unverifiable — so without this the two fixtures below are unadmittable and the suite fails
# for a reason it is not about. Seeded from the shipped manifests so the chain cannot drift from
# what the repo actually ships.
for pf in examples/gitops-repo/fleet/platform-agent.yaml \
          examples/gitops-repo/clusters/cluster-a/agents/agent.yaml; do
  if ref="$(seed_parent_agent "$K" "$pf")"; then
    seeded+=("$ref")
  else
    bad "could not seed the parent chain from $pf: $ref"
    exit 1
  fi
done
pass "parent chain seeded: ${seeded[*]}"

agent_yaml() { # <name> <namespace>
  cat <<YAML
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $1
  namespace: $2
spec:
  tier: developer-team
  # Required for every non-platform tier (06 §1.2 V-3), and the scope below must sit within this
  # parent's (V-6) — which is why the project and cluster are the shipped manifest's, not this
  # suite's. metadata.namespace equals spec.scope.namespace because the placement clause makes a
  # developer-team agent live in the namespace it is scoped to. (No backticks in this heredoc: it
  # is unquoted so the shell would run them.)
  parentRef:
    name: $PARENT_AGENT
  scope:
    projectId: $PROJECT_ID
    clusterName: $CLUSTER_NAME
    namespace: $2
  harness:
    clusterName: $CLUSTER_NAME
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

agent_yaml "$AGENT"       "$NS"       | apply_fixture "Agent $NS/$AGENT" || exit 1
agent_yaml "$QUIET_AGENT" "$NS_QUIET" | apply_fixture "Agent $NS_QUIET/$QUIET_AGENT" || exit 1
# Captured now and used in every Event field-selector below. A name is not an identity across runs
# in a namespace that outlives them: an `AgentEscalated` Event left by a previous run against a
# previous `braked-agent` would satisfy L2-3 without C-BR doing anything at all.
AGENT_UID="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
QUIET_UID="$($K -n "$NS_QUIET" get agent "$QUIET_AGENT" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
[ -n "$AGENT_UID" ] && [ -n "$QUIET_UID" ] \
  || { bad "could not read the fixtures' UIDs; Event assertions would fall back to matching by name"; exit 1; }
pass "two Agent CRs admitted: $NS/$AGENT and $NS_QUIET/$QUIET_AGENT"

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

# ------------------------------------------------------------------------------------------------
# What a record has to be before the API server will keep it
# ------------------------------------------------------------------------------------------------
# The ActionRecord CRD is heavily validated (06 §4.3) and none of it is negotiable from here: the
# actionId must be a real Crockford ULID, the idempotency key a real SHA-256, the chain id present,
# and both retention clocks set with `undoWindowExpiresAt <= expiresAt`. Building these properly
# rather than with plausible-looking strings is the point — a fixture the schema would reject is a
# fixture the broker could never have written, and this suite's whole claim is about what happens to
# records the system really produces.

# The `ar-<lowercase actionId>` naming convention, in one place. Derived rather than passed so the
# name and the actionId cannot drift into naming different actions.
rec_name() { printf 'ar-%s' "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"; }

# A real digest, because the schema demands ^sha256:[0-9a-f]{64}$ and a made-up 64 characters would
# be a lie the pattern happens not to catch. Both tool names, because this runs on a developer's
# macOS laptop and on a Linux runner.
sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | cut -d' ' -f1
  else
    printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1
  fi
}

# Retention clocks, computed rather than hardcoded. A pinned future date rots into the past and then
# the RETENTION controller deletes these records mid-run, which would read as C-BR having done
# something to them -- a negative control failing for the one reason it must never fail for.
rfc3339_in() { # <hours>
  if date -u -d "+$1 hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null; then :
  else date -u -v"+$1"H +%Y-%m-%dT%H:%M:%SZ; fi
}

EXPIRES_AT="$(rfc3339_in 720)"
UNDO_EXPIRES_AT="$(rfc3339_in 24)"
[ -n "$EXPIRES_AT" ] && [ -n "$UNDO_EXPIRES_AT" ] \
  || { bad "could not compute retention timestamps (neither GNU nor BSD date worked)"; exit 1; }

# MINTED PER RUN, not constants. The records of previous runs cannot be deleted (see the P3 note),
# so a fixed id would collide with a leftover -- and `ActionRecord.spec` is immutable, so the apply
# would be REFUSED rather than refreshed and the suite would assert against a record admitted under
# whatever rules were in force weeks ago. The epoch makes each run's records traceable to when they
# ran; 26 digits is a valid actionId, since the schema's alphabet (Crockford base32, no I/L/O/U)
# contains all ten.
RUN_EPOCH="$(date -u +%s)"
mk_action_id() { printf '%026d' "$RUN_EPOCH$1"; }
REC_QUIET_NONE_ID="$(mk_action_id 1)"
REC_QUIET_IDLE_ID="$(mk_action_id 2)"
REC_LOUD_ID="$(mk_action_id 3)"
CHAIN_ID="$(mk_action_id 9)"

REC_QUIET_NONE="$(rec_name "$REC_QUIET_NONE_ID")"
REC_QUIET_IDLE="$(rec_name "$REC_QUIET_IDLE_ID")"
REC_LOUD="$(rec_name "$REC_LOUD_ID")"

# The record lives in `$NS` whatever its agent's namespace is: 06 §4.3 binds the right to write
# `status.escalation` to the record's own `spec.actorServiceAccount` in the record's own namespace,
# so keeping all four records under the one broker identity created above is what keeps that write
# the real one. `agentRef` is the thing that crosses — `BrakeReconciler.resolveAgent` looks the
# Agent up by `ref.Namespace`/`ref.Name`, so the quiet agent being elsewhere is exactly as visible
# to C-BR as the loud one.
record_yaml() { # <action-id> <agent> <agent-namespace>
  cat <<YAML
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionRecord
metadata:
  name: $(rec_name "$1")
  namespace: $NS
spec:
  actionId: "$1"
  agentRef: { name: $2, namespace: $3 }
  agentIdentity: developer-team/$PROJECT_ID/$CLUSTER_NAME/$3
  actorServiceAccount: $BROKER_SA
  requester: { kind: agent, id: $2 }
  attributionUnverified: false
  trigger:
    source: watch
    ref: deployment/api-gateway
    detail: "brake-fanout-l2"
    chainId: "$CHAIN_ID"
  intent: "scale api-gateway to 3 replicas"
  idempotencyKey: "sha256:$(sha256_hex "$1")"
  dryRun: false
  classification:
    class: elevated
    blastRadius: { objects: 1, fractionOfScope: "0.02", cap: 25 }
    undoable: true
  targets:
    - { group: apps, version: v1, kind: Deployment, namespace: $3, name: api-gateway }
  retention:
    class: elevated
    ttl: 720h
    expiresAt: "$EXPIRES_AT"
    undoWindow: 24h
    undoWindowExpiresAt: "$UNDO_EXPIRES_AT"
YAML
}

# ------------------------------------------------------------------------------------------------
# L2-4 (created first, asserted last): the two records that owe nothing
# ------------------------------------------------------------------------------------------------
# Created BEFORE the loud one on purpose. See the header: the barrier for a negative control is the
# controller demonstrably having processed something enqueued LATER, not a sleep.
echo; echo "== ¬ fixtures: two records that owe no fan-out, enqueued before the loud one =="

record_yaml "$REC_QUIET_NONE_ID" "$QUIET_AGENT" "$NS_QUIET" \
  | apply_fixture "$REC_QUIET_NONE (no escalation at all)" || exit 1
record_yaml "$REC_QUIET_IDLE_ID" "$QUIET_AGENT" "$NS_QUIET" \
  | apply_fixture "$REC_QUIET_IDLE (escalation requesting neither effect)" || exit 1

# An escalation that asks for NEITHER effect. `fanoutPending` must reject it on the flags alone —
# this is the shape that distinguishes "C-BR reads the request" from "C-BR reacts to the field
# existing", and the second one pauses an agent on any record that mentions an escalation.
$K -n "$NS" patch actionrecord "$REC_QUIET_IDLE" $AS_BROKER --subresource=status --type=merge \
  -p '{"status":{"escalation":{"pageRequested":false,"pauseRequested":false,"reason":"recorded, but nothing was asked for","requestedAt":"2026-07-28T00:00:00Z"}}}' >/dev/null 2>&1 \
  || { bad "the broker could not write the ¬ escalation — check the VAP's isOwningBroker row"; exit 1; }
pass "two silent records in place (no escalation / escalation requesting neither effect)"

# ------------------------------------------------------------------------------------------------
# L2-3: the positive
# ------------------------------------------------------------------------------------------------
echo; echo "== L2-3: a recorded rung-5 escalation fans out into a real pause and a real page =="

record_yaml "$REC_LOUD_ID" "$AGENT" "$NS" \
  | apply_fixture "$REC_LOUD (the rung-5 escalation)" || exit 1

paused_before="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.spec.operations.paused}' 2>/dev/null)"
if [ "$paused_before" = "true" ]; then
  bad "$AGENT is already paused before the escalation was written — the positive below would be vacuous"
  exit 1
fi
pass "$AGENT starts unpaused"

$K -n "$NS" patch actionrecord "$REC_LOUD" $AS_BROKER --subresource=status --type=merge \
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
  receipt="$($K -n "$NS" get actionrecord "$REC_LOUD" \
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
  ev="$($K -n "$NS" get events --field-selector "reason=AgentEscalated,involvedObject.uid=$AGENT_UID" -o jsonpath='{.items[*].message}' 2>/dev/null)"
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
  denied_write "the request half of status.escalation" "$REC_QUIET_NONE" \
    '{"status":{"escalation":{"pageRequested":true,"pauseRequested":true,"reason":"self-authored"}}}' \
    "'A failed rollback pages AND auto-pauses' would be self-attested by the party with the motive (vap-agent-scope-journal, validation 5)"

  denied_write "status.phase" "$REC_LOUD" \
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
  for rec in "$REC_QUIET_NONE" "$REC_QUIET_IDLE"; do
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

  qp="$($K -n "$NS_QUIET" get agent "$QUIET_AGENT" -o jsonpath='{.spec.operations.paused}' 2>/dev/null)"
  if [ "$qp" != "true" ]; then
    pass "$QUIET_AGENT is still unpaused"
  else
    bad "$QUIET_AGENT was paused by a record that asked for nothing"
  fi

  qev="$($K -n "$NS_QUIET" get events --field-selector "reason=AgentEscalated,involvedObject.uid=$QUIET_UID" -o jsonpath='{.items[*].message}' 2>/dev/null)"
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
