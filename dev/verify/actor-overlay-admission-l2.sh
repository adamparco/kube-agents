#!/usr/bin/env bash
# actor-overlay-admission-l2.sh — the P9-T9b-5a ruling, executed against a real API server.
#
# WHAT THIS IS FOR
#   `dev/verify/fixtures/actor-tenant-grant.yaml` ends its header by handing T9b a question:
#
#     "a WRITE tenant overlay cannot wear `kube-agents/tier` (validation 1 denies it) and, without
#      it, is governed by no admission rule at all — `vap-agent-scope` does not exist until P10-T1,
#      and `vap-agent-readonly`'s matchConstraints cover roles and clusterroles but not
#      rolebindings. T9b has to rule on that."
#
#   Every clause of that paragraph is a PREDICTION about what an API server would do, made by
#   reading a CEL expression in a file. `actor-tenant-write-grant.yaml`'s header is the ruling; this
#   script is the part that makes it a fact. It submits all three label variants of the same rule
#   set to the deployed policy and records what came back.
#
#   That matters in two directions. Forwards: a ruling nobody executed is a ruling that was right on
#   the day it was written. Backwards: if a future edit to `vap-agent-readonly` makes the shipped
#   fixture DENIED — widening the match condition, adding a namespaced-Role validation, changing the
#   discriminator — this suite goes red at the edit, instead of surfacing three suites downstream as
#   a 403 inside the broker that reads like a broker bug.
#
# WHAT IS ASSERTED, in order:
#   L2-0  P2 — THE POLICY IS LIVE, established by experiment and not by `kubectl get`. Every arm
#         below that expects an ADMIT is indistinguishable from the same arm run against a cluster
#         with no policy installed, a binding that has not activated, or a binding whose
#         validationActions omit Deny. This is LSN-006 aimed at admission instead of the dataplane,
#         and it is the reason binding.md's P2 finally has a function. The probe is the WRONG-SCOPE
#         validation (a `developer-team` ClusterRole), deliberately a different validation from
#         either of the two arms below, so that establishing liveness does not pre-establish an arm.
#   L2-1  P6 — THE DEPLOYED POLICY IS THE TREE'S. Same count of validations, and every `message:`
#         string in the tree present in the deployed object. Messages rather than a digest because
#         the API server defaults and reorders fields it owns; messages rather than a count alone
#         because a count is satisfied by a rewritten validation. A ruling about "the shipped
#         policy" measured against a four-day-old generation of it is a ruling about nothing.
#   L2-2  VALIDATION 1 DENIES A TIER-LABELLED WRITE ROLE. The exact rules of the shipped write
#         fixture, wearing `kube-agents/tier: platform`. Denied, and the denial attributable — the
#         message must be validation 1's, not merely non-empty. A Role rejected for the wrong reason
#         is a Role that will be admitted when that reason goes away.
#   L2-3  VALIDATION 3 DENIES AN ACTOR-LABELLED TENANT ROLE. The same rules wearing
#         `kube-agents/role: actor`, denied by the compiled 06 §2.2.1 allow-list. This is the arm
#         that says the second escape route is closed: an author who read validation 1's message and
#         reached for the other label does not get in either.
#   L2-4  THE SHIPPED FIXTURE, WEARING NEITHER, IS ADMITTED. The ruling's core claim and its cost.
#         Reported as a PASS because it is what the ruling says will happen, immediately followed by
#         the three things that bound the fixture in admission's place. A green here with no such
#         note would read as "admission approves of this", which is the opposite of true.
#   L2-5  THE BINDING IS OUTSIDE matchConstraints, and that is not a second hole. A RoleBinding
#         wearing every label in the vocabulary is admitted, because the policy governs `roles` and
#         `clusterroles`. Recorded because a reader auditing this file should not have to
#         rediscover it, and because a binding confers exactly the authority of the Role it names —
#         so a policy that bounds every Role bounds every binding transitively.
#   L2-6  THE LIBRARY ROUND TRIP against a real actor identity: `actor_overlay_apply_write` grants
#         and self-verifies, then `actor_overlay_revoke_write` takes it back and PROVES the
#         authorizer agrees. Two of the three compensating controls named in L2-4 are those
#         functions; shipping them unexercised would make the ruling's mitigation the one part of
#         it nobody ran.
#
# WHAT THIS DOES NOT CLAIM
#   A check ID. There is no `V-*` row for "the ruling this phase made is true", and inventing one
#   would put a line in `verification/results.csv` for a property nothing in 09 §6 states. Same
#   shape as `verify-phase9.sh`, and for the same reason. What this suite protects is a decision.
#
#   That the broker can now execute. It cannot yet — nothing here submits an envelope. This grants
#   the authority that `broker-execute-l2.sh` (P9-T9b-5b) needs in order to reach step 8 at all, and
#   proves the grant is exactly what it says. Accept (a) is that suite's.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This applies real RBAC granting real
# write verbs to a real ServiceAccount on the target, and seeds/deletes Agent fixtures. On the live
# install that is an over-grant against the fleet's own actor identity. L2-2 through L2-5 are
# `--dry-run=server` and persist nothing; L2-6 is a real apply with an EXIT-trap revoke.
# Exit: 0 = the ruling holds · 1 = it does not · 2 = refused target · 3 = DEFERRED (P2/P10).
# Usage: dev/verify/actor-overlay-admission-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — no claim here is about a first-party binary. L2-0 through L2-5 are
#      about the API server's admission chain and a YAML file in this tree, neither of which is an
#      image we build. L2-6 reads ONE thing from the deployed controller, the actor ServiceAccount
#      NAME in `status.broker.actorServiceAccount`, and a stale controller does not make that name
#      wrong: it is by construction the name the broker pod on THIS cluster will look up, which is
#      the property P6 asks for and P1 cannot add to. Pinning a digest this suite never inspects
#      would read as coverage while providing none. Named expiry: when `broker-execute-l2.sh` reuses
#      this overlay to drive a real pipeline, that suite needs P1 in full, because the thing under
#      test there is a binary.
#   P3 admission-recreate: none of L2-0..L2-5 — every object they judge is constructed inside this
#      run and submitted with `--dry-run=server`, so nothing is read that predates the policy under
#      test; "grandfathered" is unrepresentable for an object that never existed. L2-6 DOES recreate:
#      the Agent CR it needs is deleted with `--wait=true` and re-applied before anything reads its
#      status, so the actor identity the grant binds to is one the controller running NOW resolved
#      through the admission chain running NOW — not one left behind by a prior generation of
#      either. The Role and RoleBinding are created and destroyed inside the run.
#   P6 runtime-authoritative: the policy under test is read from the CLUSTER
#      (`kubectl get validatingadmissionpolicy kube-agents-agent-readonly`), never from
#      `examples/gitops-repo/policy/vap-agent-readonly.yaml`; the file is used only as the expected
#      value that L2-1 compares the deployed object against, and a mismatch defers rather than
#      quietly testing the file against itself. The verdicts in L2-2..L2-5 are the API server's own
#      rejection messages. The actor ServiceAccount in L2-6 is read from
#      `status.broker.actorServiceAccount` and never recomputed from a naming function.
#   P2 policy-live: `p2_assert_policy_live`, asserted below before any admit-shaped verdict. This is
#      that precondition's first executable use in the repository; see its header for why the
#      admit direction is the one that needs it.
#   P10 control-plane-healthy: asserted below, before any verdict. L2-6 creates namespaces and
#      RoleBindings and waits on controller-written `.status`; a control plane that is not
#      converging turns every one of those into a red that describes the cluster (LSN-026).
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

K="kubectl --context $CTX"

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "  NOTE: $1"; }
cd "$REPO_ROOT" || { echo "REFUSING: cannot cd to $REPO_ROOT" >&2; exit 2; }

POLICY=kube-agents-agent-readonly
VAP_FILE=examples/gitops-repo/policy/vap-agent-readonly.yaml
NS=kubeagents-system
AGENT=platform-agent
AGENT_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml
# One namespace, reused across runs and never deleted: the read overlay creates it and it holds
# whatever a later suite journals into it ([[LSN-045]]).
TENANT_NS=kubeagents-overlay-ruling

WORK="$(mktemp -d)"
cleanup() {
  # The write grant first and unconditionally — a leaked write authority is the one outcome this
  # whole ruling is about. Then the read half. The namespace stays.
  . "$REPO_ROOT/dev/lib/actor-overlay.sh" 2>/dev/null || true
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1 || true
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "===================================================================="
echo " P9-T9b-5a — the test-only write overlay, ruled on against a real"
echo " admission chain (no check ID; this protects a decision)"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

. "$REPO_ROOT/dev/lib/preconditions.sh"
. "$REPO_ROOT/dev/lib/actor-overlay.sh"
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# --- L2-0 · P2 — the policy is live -----------------------------------------------------------
#
# The probe is validation 2's subject, not validation 1's or 3's. Using the tier-labelled write Role
# here would make L2-2 a restatement of the precondition rather than an independent arm.
echo
echo "== L2-0. P2 — the policy is LIVE, proved by making it reject something =="
cat >"$WORK/p2-probe.yaml" <<'YAML'
# A wrong-scope ClusterRole: the namespace tier may not hold one (validation 2). Adversarial input,
# never applied for real — `--dry-run=server` runs the full admission chain and writes nothing.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubeagents-p2-liveness-probe
  labels:
    kube-agents/tier: developer-team
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get"]
YAML
p2_assert_policy_live "$K" "$POLICY" "$WORK/p2-probe.yaml" || exit 3
pass "L2-0 the admission chain is enforcing $POLICY — every ADMIT below is a measurement"

# --- L2-1 · P6 — the deployed policy is the tree's ---------------------------------------------
echo
echo "== L2-1. P6 — the DEPLOYED policy is this tree's generation of it =="
# Every validation the tree declares, by its message. `message:` is one line per validation in this
# file and each is a distinct sentence, so the set of them is a fingerprint of what the policy
# actually says — and unlike a digest it survives the API server defaulting fields it owns.
grep -o '^ *message: ".*"$' "$VAP_FILE" | sed 's/^ *message: "//; s/"$//' >"$WORK/tree-messages.txt"
tree_n="$(wc -l <"$WORK/tree-messages.txt" | tr -d ' ')"
deployed_msgs="$($K get validatingadmissionpolicy "$POLICY" \
  -o jsonpath='{range .spec.validations[*]}{.message}{"\n"}{end}' 2>/dev/null)"
deployed_n="$(printf '%s' "$deployed_msgs" | grep -c . | tr -d ' ')"

if [ "$tree_n" -eq 0 ]; then
  bad "L2-1 read ZERO validations out of $VAP_FILE — this script's parse is broken, not the cluster"
elif [ "$tree_n" -ne "$deployed_n" ]; then
  bad "L2-1 the deployed $POLICY has $deployed_n validations; $VAP_FILE declares $tree_n."
  bad "  The cluster is running a different generation of the policy this ruling is about."
  bad "  Fix the environment, not this check: kubectl --context $CTX apply -f $VAP_FILE"
else
  missing=0
  while IFS= read -r msg; do
    [ -n "$msg" ] || continue
    case "$deployed_msgs" in
      *"$msg"*) ;;
      *) bad "L2-1 the deployed policy is missing a validation the tree declares: ${msg:0:80}…"
         missing=1 ;;
    esac
  done <"$WORK/tree-messages.txt"
  [ "$missing" -eq 0 ] && pass "L2-1 the deployed $POLICY carries all $tree_n of the tree's validations, verbatim"
fi

# --- L2-2 .. L2-5 · the three label variants and the binding ------------------------------------
#
# submit <file> <admit|deny> <label-for-the-report> [message-substring-the-denial-must-carry]
#   One `--dry-run=server` apply, with the expected outcome stated up front. The denial's TEXT is
#   checked, not merely its non-zero exit: an object rejected by a quota, a webhook, a missing
#   namespace or a schema error also exits non-zero, and reading that as "the policy denied it"
#   would keep passing on the day the policy stopped selecting the object.
submit() {
  local file="$1" want="$2" label="$3" needle="${4:-}" out rc
  out="$($K apply --dry-run=server -f "$file" 2>&1)"; rc=$?
  case "$want" in
    deny)
      if [ "$rc" -eq 0 ]; then
        bad "$label was ADMITTED; the policy must reject it"
        return 1
      fi
      if [ -n "$needle" ] && ! printf '%s' "$out" | grep -qF -- "$needle"; then
        bad "$label was rejected, but not by the validation this arm is about."
        bad "  wanted the message to carry: $needle"
        bad "  got: $out"
        return 1
      fi
      pass "$label — denied, and the denial names the validation this arm is about"
      ;;
    admit)
      if [ "$rc" -ne 0 ]; then
        bad "$label was REJECTED; the ruling says nothing selects it. Answer: $out"
        return 1
      fi
      pass "$label — admitted, as the ruling says (see the NOTEs below for what bounds it instead)"
      ;;
  esac
  return 0
}

# THE THREE VARIANTS ARE DERIVED FROM THE SHIPPED FIXTURE, NOT RE-TYPED.
#
#   The ruling's claim is "these rules are denied when they wear a tier label, denied when they wear
#   an actor label, and admitted when they wear neither" — one rule set, three label sets. Typing
#   the rules out here would make all three arms true of a rule set that nobody grants, and the day
#   the fixture grew a fourth resource the suite would keep passing about the old three. So the Role
#   document is rendered out of `actor-tenant-write-grant.yaml` exactly as `actor_overlay_apply_write`
#   renders it, and each variant is that document with one label line inserted and the name changed.
#
#   Renamed because a `--dry-run=server` apply over a name that already exists is an UPDATE, not a
#   CREATE — and a previous crashed run can leave `kubeagents-actor-tenant-write` behind in the
#   tenant namespace. Same rules, same admission decision, different object.
if [ ! -f "$ACTOR_OVERLAY_WRITE_FIXTURE" ]; then
  bad "L2-2..L2-4 no write fixture at $ACTOR_OVERLAY_WRITE_FIXTURE — the ruling has no subject"
else
  # A server dry-run of a namespaced Role still needs its namespace to exist. Applied rather than
  # created, because the read overlay applies this same namespace and `create` first leaves it
  # without the last-applied annotation that apply then warns about.
  printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$TENANT_NS" |
    $K apply -f - >/dev/null 2>&1
  KAGE_TENANT_NS="$TENANT_NS" KAGE_ACTOR_NS="$NS" KAGE_ACTOR_SA="placeholder-actor" \
    envsubst '${KAGE_TENANT_NS} ${KAGE_ACTOR_NS} ${KAGE_ACTOR_SA}' \
    <"$ACTOR_OVERLAY_WRITE_FIXTURE" >"$WORK/shipped.yaml"
  # Everything up to the SECOND `---`: the header, the separator that opens the file, and the Role.
  # The RoleBinding is L2-5's subject and names a ServiceAccount that does not exist in these arms.
  awk '/^---$/{n++} n<2' "$WORK/shipped.yaml" >"$WORK/shipped-role.yaml"

  # variant <file> <name> <one label line, already indented>
  #   The rendered Role with its metadata.name replaced and one label spliced in immediately after
  #   the `labels:` key. Nothing else about the document changes.
  variant() {
    sed -e "s|^  name: kubeagents-actor-tenant-write$|  name: $2|" \
        -e "s|^  labels:$|  labels:\\
$3|" "$WORK/shipped-role.yaml" >"$1"
  }

  if ! grep -q '^kind: Role$' "$WORK/shipped-role.yaml"; then
    bad "L2-2..L2-4 could not split the Role out of the rendered fixture — the file's shape changed"
  else
    echo
    echo "== L2-2. validation 1 — a tier-labelled WRITE Role is denied =="
    variant "$WORK/tier.yaml" kubeagents-ruling-tier '    kube-agents/tier: platform'
    grep -q 'kube-agents/tier: platform' "$WORK/tier.yaml" ||
      bad "L2-2 the tier label never landed in the variant — this arm would pass vacuously"
    submit "$WORK/tier.yaml" deny "L2-2 the shipped write rules wearing kube-agents/tier" \
      "agent RBAC may grant only read verbs"

    echo
    echo "== L2-3. validation 3 — an actor-labelled TENANT Role is denied =="
    variant "$WORK/actor.yaml" kubeagents-ruling-actor '    kube-agents/role: actor'
    grep -q 'kube-agents/role: actor' "$WORK/actor.yaml" ||
      bad "L2-3 the actor label never landed in the variant — this arm would pass vacuously"
    submit "$WORK/actor.yaml" deny "L2-3 the shipped write rules wearing kube-agents/role: actor" \
      "actor RBAC may grant only the 06 §2.2.1 broker-operations rule set"

    echo
    echo "== L2-4. the shipped fixture, wearing neither, is ADMITTED — the ruling and its cost =="
    submit "$WORK/shipped-role.yaml" admit "L2-4 the shipped write fixture (no tier, no role label)"
    note "admission does NOT bound this object. Three things do, and all three are live:"
    note "  V-CTN-037 (L0, BLOCKING-ALWAYS) — namespaced only, no escalate/bind/impersonate, no *,"
    note "    no RBAC API group, marker confined to dev/, unreferenced from outside dev/"
    note "  actor_overlay_apply_write — asks the authorizer whether the grant is what the file says"
    note "  actor_overlay_revoke_write — asks the authorizer whether the grant is GONE"
    note "P10-T1's vap-agent-scope is what closes this: once an actor's tenant template is a compiled"
    note "  allow-list, this fixture can wear kube-agents/role: actor and be bounded by the cluster."
  fi
fi

echo
echo "== L2-5. the binding is outside matchConstraints, and why that is not a second hole =="
cat >"$WORK/binding.yaml" <<YAML
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubeagents-ruling-binding
  namespace: $TENANT_NS
  labels:
    kube-agents/tier: platform
    kube-agents/role: actor
subjects:
  - kind: ServiceAccount
    name: placeholder-actor
    namespace: $NS
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: kubeagents-actor-tenant-readonly
YAML
submit "$WORK/binding.yaml" admit "L2-5 a RoleBinding wearing BOTH agent labels"
note "matchConstraints cover roles and clusterroles. A binding confers exactly the authority of the"
note "  Role it names, so bounding every Role bounds every binding — this is transitive, not a gap."

# --- L2-6 · the library round trip ---------------------------------------------------------------
echo
echo "== L2-6. apply_write grants exactly what it claims, and revoke_write proves it is gone =="
if [ ! -f "$AGENT_MANIFEST" ]; then
  bad "L2-6 no agent manifest at $AGENT_MANIFEST"
else
  # P3: delete before apply, so the identity is one the controller running NOW resolved.
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=true >/dev/null 2>&1
  if ! $K -n "$NS" apply -f "$AGENT_MANIFEST" >/dev/null 2>&1; then
    bad "L2-6 could not apply $AGENT_MANIFEST"
  elif ! seed_agent_fixtures "$K" "$NS" "$AGENT"; then
    bad "L2-6 could not seed the agent's Secret/ServiceAccount"
  elif ! seed_agent_identity "$K" "$NS" "$AGENT"; then
    bad "L2-6 the controller never published status.broker.actorServiceAccount — no actor to grant to"
  else
    rc=0
    actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || rc=$?
    if [ "$rc" -ne 0 ]; then
      bad "L2-6 actor_overlay_apply_write returned $rc — the grant is absent, or wider than the fixture says"
    else
      pass "L2-6a the write overlay conferred exactly its four verbs, in one namespace, over no RBAC"
      rc=0
      actor_overlay_revoke_write "$K" "$TENANT_NS" || rc=$?
      if [ "$rc" -ne 0 ]; then
        bad "L2-6b revoke_write returned $rc — a write authority is STILL on $CTX right now"
      else
        pass "L2-6b the authorizer agrees the write authority is gone (namespace left standing)"
      fi
    fi
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "P9-T9b-5a ruling: HOLDS on $CTX"
else
  echo "P9-T9b-5a ruling: FAILED on $CTX — the write overlay's premises are not what the tree says"
fi
exit "$fail"
