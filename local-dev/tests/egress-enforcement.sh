#!/usr/bin/env bash
# Phase-5 Accept (b) / E-A: EGRESS ENFORCEMENT proof (load-bearing).
#
# Proves that on a NetworkPolicy-ENFORCING CNI (Calico), a tier-shaped default-deny + pure-allowlist
# egress policy actually BLOCKS an off-allowlist destination while ALLOWING an on-allowlist one — the
# thing kindnet cannot do (verify-phase3 P3-K6 + the P5 shape checks are structural-only). It uses a
# representative in-cluster policy of the SAME shape as the production tier netpols (podSelector by tier
# label, policyTypes:[Egress], DNS + a single ipBlock allow) so it proves the ENFORCEMENT MECHANISM,
# not a specific production CIDR.
#
# Adversarially distinguishes a REAL deny from a setup/DNS error: it first proves BOTH destinations are
# reachable with no policy (baseline), so a later failure to reach the off-allowlist target can only be
# the policy — not broken networking.
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind or scratch-GKE context.
# Exit codes: 0 = enforcement PROVEN; 1 = enforcement FAILED (halt condition); 3 = DEFERRED (no
# NetworkPolicy-enforcing CNI reachable — not faked green; stand up local-dev/kind/kind-config.yaml).
# Usage: local-dev/tests/egress-enforcement.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions).
#   P1 image-under-test:  none — the two fixture servers are upstream nginx and the client is upstream
#      curl, chosen precisely so that nothing about kube-agents' own build can influence the result.
#      The subject of the claim is the CNI's NetworkPolicy implementation, and it is asserted through a
#      packet, not through a rendered manifest, so no first-party digest is on the path.
#   P3 admission-recreate: none, and the reason is a real distinction rather than a convenience.
#      NetworkPolicy is not an admission control: the CNI evaluates it per-connection for as long as
#      the policy exists, so a policy applied AFTER a pod is already running still governs that pod.
#      That is exactly why the baseline-then-apply-then-reprobe ordering here is valid, and it is the
#      same ordering that would be invalid for an admission claim (LSN-002). The namespace is deleted
#      and recreated each run anyway, so no fixture survives to be grandfathered.
#   P6 runtime-authoritative: the dataplane result — an HTTP probe from inside the client pod to two
#      live pod IPs — plus the baseline that proves both were reachable before the policy existed. That
#      baseline is what makes the artifact authoritative rather than merely live: without it, a DNS or
#      scheduling failure would present as a successful deny. P4 also applies and is enforced in code:
#      absent calico-node this exits 3 (DEFERRED), never 0.
set -uo pipefail # -e omitted deliberately: kubectl/exec exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
K="kubectl --context $CTX"
NS="egress-test"
TIER="egress-probe"
ALLOWED_IMG="nginx:1.27-alpine"
CLIENT_IMG="curlimages/curl:8.10.1"

# Anchored allow-list: kind-* and gke-scratch-* ONLY. Substring globs like *scratch* would let a prod
# context slip through — never do that.
case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=1; }
cleanup() { $K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- 0) require a NetworkPolicy-enforcing CNI; otherwise DEFER (do not fake) ---------------------------
echo "== 0) checking for a NetworkPolicy-enforcing CNI (Calico) =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable — no enforcing cluster to test against."
  echo "  Stand one up: local-dev/kind/up.sh"
  exit 3
fi
if ! $K -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  echo "DEFERRED: no calico-node found — kindnet does NOT enforce NetworkPolicy, so egress deny cannot be"
  echo "  proven here. This is deferred-not-faked (E-A). Use local-dev/kind/kind-config.yaml + install Calico."
  exit 3
fi
pass "Calico (NetworkPolicy-enforcing CNI) present"

# --- 1) fixtures: two server pods (allowed + denied targets) + a client pod --------------------------
echo "== 1) deploying fixtures in namespace $NS =="
$K create namespace "$NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true
$K -n "$NS" run allowed-server --image="$ALLOWED_IMG" --labels="role=allowed-server" \
  --port=80 >/dev/null 2>&1 || true
$K -n "$NS" run denied-server --image="$ALLOWED_IMG" --labels="role=denied-server" \
  --port=80 >/dev/null 2>&1 || true
$K -n "$NS" run client --image="$CLIENT_IMG" --labels="kube-agents/tier=$TIER" \
  --command -- sleep 3600 >/dev/null 2>&1 || true

for p in allowed-server denied-server client; do
  if ! $K -n "$NS" wait --for=condition=Ready "pod/$p" --timeout=120s >/dev/null 2>&1; then
    bad "fixture pod $p never became Ready (image pull / scheduling) — cannot prove enforcement"
    exit 1
  fi
done
ALLOWED_IP="$($K -n "$NS" get pod allowed-server -o jsonpath='{.status.podIP}')"
DENIED_IP="$($K -n "$NS" get pod denied-server -o jsonpath='{.status.podIP}')"
echo "  allowed-server=$ALLOWED_IP  denied-server=$DENIED_IP"
if [ -z "$ALLOWED_IP" ] || [ -z "$DENIED_IP" ] || [ "$ALLOWED_IP" = "$DENIED_IP" ]; then
  bad "could not resolve two distinct server pod IPs — setup error, not a policy result"; exit 1
fi

probe() { # probe <ip> -> 0 if HTTP reachable within 4s, non-zero otherwise
  $K -n "$NS" exec client -- curl -s -o /dev/null --max-time 4 "http://$1/" >/dev/null 2>&1
}

# --- 2) baseline (no policy): BOTH reachable -> proves networking works (real-vs-setup) ---------------
echo "== 2) baseline with NO egress policy (both must be reachable) =="
if probe "$ALLOWED_IP"; then pass "baseline: allowed-server reachable"; else
  bad "baseline: allowed-server UNREACHABLE with no policy — setup/networking error, not a real deny"; exit 1; fi
if probe "$DENIED_IP"; then pass "baseline: denied-server reachable"; else
  bad "baseline: denied-server UNREACHABLE with no policy — setup/networking error, not a real deny"; exit 1; fi

# --- 3) apply a tier-shaped default-deny + single-ipBlock allowlist -----------------------------------
echo "== 3) applying default-deny + pure-allowlist egress (allow ONLY $ALLOWED_IP/32 + DNS) =="
$K apply -f - >/dev/null 2>&1 <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: egress-probe-allowlist
  namespace: $NS
  labels:
    kube-agents/tier: $TIER
spec:
  podSelector:
    matchLabels:
      kube-agents/tier: $TIER
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
    - to:
        - ipBlock:
            cidr: $ALLOWED_IP/32
      ports:
        - { protocol: TCP, port: 80 }
EOF
sleep 5 # let Calico program the policy

# --- 4) the load-bearing assertions: on-allowlist ALLOWED, off-allowlist DENIED ----------------------
echo "== 4) enforcement assertions =="
if probe "$ALLOWED_IP"; then pass "on-allowlist destination ($ALLOWED_IP) is ALLOWED"; else
  bad "on-allowlist destination is BLOCKED — policy over-blocks (or Calico not ready)"; fi
if probe "$DENIED_IP"; then
  bad "off-allowlist destination ($DENIED_IP) is REACHABLE — egress is NOT enforced (HALT: Accept b fails)"; else
  pass "off-allowlist destination ($DENIED_IP) is DENIED (egress enforced)"; fi

echo
if [ "$fail" -eq 0 ]; then echo "EGRESS ENFORCEMENT: PROVEN (on-allowlist allowed, off-allowlist denied)"; else
  echo "EGRESS ENFORCEMENT: FAILURES ABOVE"; fi
exit "$fail"
