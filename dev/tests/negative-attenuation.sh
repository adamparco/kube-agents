#!/usr/bin/env bash
# A3 / 03 §11 attenuation negative test (load-bearing).
# Applies the agent-read-only ValidatingAdmissionPolicy, then asserts:
#   - a Role granting an agent SA a write verb            -> DENIED
#   - a ClusterRole granting a privilege-escalation verb  -> DENIED
#   - a ClusterRole for the namespace tier (wrong-scope)  -> DENIED
#   - a read-only agent Role                              -> ADMITTED
# Adversarially distinguishes a real policy denial from a malformed-object error.
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind or scratch-GKE context.
# Usage: dev/tests/negative-attenuation.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). This script is the read-only ceiling's load-bearing
# negative, and four different gates delegate to it (verify-phase2 V-K2, verify-phase3 P3-K5,
# verify-phase4 and verify-phase5 regressions), so each of them can only waive P3 by pointing here.
#   P1 image-under-test:  none — no first-party image is on the path of any claim. The policy under
#      test is a ValidatingAdmissionPolicy applied from the working tree by this script, and the
#      component that enforces it is the API server's own CEL evaluator. Neither the operator nor an
#      agent container participates, so a stale image cannot make a denial look like an admission.
#   P3 admission-recreate: the four Roles/ClusterRoles below. Each is applied INSIDE the run and after
#      the VAP, so every verdict is rendered by the policy as it exists in this tree; each is deleted
#      again on the way out, including on the unexpected-admit path, so a leaked object from a previous
#      run cannot turn a later run's create into an update against stale bytes. This is what lets the
#      gates above declare their own P3 as "none — owned by negative-attenuation.sh" honestly.
#   P6 runtime-authoritative: the API server's admission response itself, not a rendered manifest — and
#      the check is adversarial about which one it got: a non-zero exit is only accepted as a denial
#      when the message matches the policy's own text, so a malformed object or an unrelated webhook
#      cannot be read as the attenuation working.
set -uo pipefail  # -e omitted deliberately: kubectl exit codes are inspected manually below.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VAP="$REPO_ROOT/examples/gitops-repo/policy/vap-agent-readonly.yaml"
K="kubectl --context $CTX"

# Anchored allow-list: kind-* (up.sh) and gke-scratch-* (create.sh rename) ONLY. Substring globs like
# *scratch* would let a prod context (e.g. gke_prod_..._kube-agents-dev-prod) slip through — never do that.
case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }

# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
# Every assertion here is "the API server REFUSED this" — and an API server that has stopped
# answering refuses everything, so a wedged cluster reads as a perfect read-only ceiling.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

echo "== applying ValidatingAdmissionPolicy =="
$K apply -f "$VAP" || { echo "could not apply VAP"; exit 1; }
$K create namespace team-x --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true
sleep 3  # allow the policy/binding to register

# Helpers read the manifest from stdin ONCE into a variable, then feed both apply and delete from it.
# (A heredoc is consumed by the first read; a second `kubectl -f -` would get empty stdin and silently
# no-op, leaking the admitted object — so we re-emit the captured manifest for cleanup.)
expect_deny() {
  local name="$1" needle="$2"; shift 2
  local manifest; manifest="$(cat)"
  local out rc; out="$(printf '%s' "$manifest" | $K apply -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    bad "$name was ADMITTED (expected denial)"
    printf '%s' "$manifest" | $K delete -f - >/dev/null 2>&1 || true
    return
  fi
  if echo "$out" | grep -qi "$needle"; then pass "$name denied by policy"; else
    bad "$name rejected but NOT by our policy (adversarial check): $out"; fi
}
expect_admit() {
  local name="$1"
  local manifest; manifest="$(cat)"
  local out rc; out="$(printf '%s' "$manifest" | $K apply -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    pass "$name admitted"
    printf '%s' "$manifest" | $K delete -f - >/dev/null 2>&1 || true
  else bad "$name was DENIED (expected admit): $out"; fi
}

echo "== 1) write-verb Role (expect deny) =="
expect_deny "write-verb Role" "read verbs" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: evil-writer, namespace: team-x, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "delete"]
EOF

echo "== 2) privilege-escalation ClusterRole (impersonate; expect deny) =="
expect_deny "impersonate ClusterRole" "read verbs" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: evil-impersonator, labels: { kube-agents/tier: platform } }
rules:
  - apiGroups: [""]
    resources: ["users", "groups", "serviceaccounts"]
    verbs: ["impersonate"]
EOF

echo "== 3) wrong-scope ClusterRole for namespace tier (expect deny) =="
expect_deny "wrong-scope ClusterRole" "wrong-scope" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: evil-scope, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
EOF

echo "== 4) read-only agent Role (expect admit) =="
expect_admit "read-only Role" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: good-reader, namespace: team-x, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
EOF

echo
if [ "$fail" -eq 0 ]; then echo "A3 attenuation: ALL CHECKS PASSED"; else echo "A3 attenuation: FAILURES ABOVE"; fi
exit "$fail"
