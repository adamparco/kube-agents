#!/usr/bin/env bash
# Phase-5 Accept (b) / E-A: EGRESS ENFORCEMENT proof (load-bearing).
#
# Proves that on a NetworkPolicy-ENFORCING dataplane, a tier-shaped default-deny + pure-allowlist
# egress policy actually BLOCKS an off-allowlist destination while ALLOWING an on-allowlist one — the
# thing a shape check cannot do (verify-phase3 P3-K6 + the P5 checks judge YAML only). It uses a
# representative in-cluster policy of the SAME shape as the production tier netpols (podSelector by tier
# label, policyTypes:[Egress], DNS + one in-cluster allow + one external ipBlock allow) so it proves the
# ENFORCEMENT MECHANISM, not a specific production CIDR. Why the allowlist carries both an endpoint
# selector and an ipBlock — and why substituting one for the other silently stops proving anything on
# Dataplane V2 — is at section 3.
#
# Adversarially distinguishes a REAL deny from a setup/DNS error: it first proves BOTH destinations are
# reachable with no policy (baseline), so a later failure to reach the off-allowlist target can only be
# the policy — not broken networking.
#
# DESTRUCTIVE-TEST GUARD: only runs against a scratch-GKE context.
# Exit codes: 0 = enforcement PROVEN; 1 = enforcement FAILED (halt condition); 3 = DEFERRED (no
# NetworkPolicy-enforcing dataplane reachable — not faked green; stand one up with dev/cluster/up.sh).
# Usage: dev/tests/egress-enforcement.sh [kube-context]
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
#      absent an enforcing dataplane this exits 3 (DEFERRED), never 0.
set -uo pipefail # -e omitted deliberately: kubectl/exec exit codes are inspected manually.

CTX="${1:-gke-scratch-kube-agents-dev}"
K="kubectl --context $CTX"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=dev/lib/preconditions.sh
. "$REPO/dev/lib/preconditions.sh"
NS="egress-test"
TIER="egress-probe"
ALLOWED_IMG="nginx:1.27-alpine"
CLIENT_IMG="curlimages/curl:8.10.1"
# Two well-known anycast resolvers, used only as TCP:443 endpoints outside the cluster. Nothing is
# sent to them and no name is resolved through them — they are the cheapest stable pair of external
# addresses that a pure-allowlist policy can put one of on the list and leave the other off.
EXT_ALLOWED="1.1.1.1"
EXT_DENIED="8.8.8.8"

# Anchored allow-list: gke-scratch-* ONLY. Substring globs like *scratch* would let a prod
# context slip through — never do that.
case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=1; }
cleanup() { $K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- 0) require a NetworkPolicy-enforcing CNI; otherwise DEFER (do not fake) ---------------------------
echo "== 0) checking for a NetworkPolicy-enforcing dataplane (P4) =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable — no enforcing cluster to test against."
  echo "  Stand one up: dev/cluster/up.sh"
  exit 3
fi
# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
# The baseline in section 2 catches most of this — it refuses to proceed unless both destinations
# are reachable with no policy — but it catches it as an "unreachable fixture", which reads as a
# setup problem in one namespace rather than as a control plane that is not converging at all.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2
# P4, from the library: an allow-list of dataplanes known to ENFORCE, not a hard requirement for one
# named product. This is deferred-not-faked (E-A) — exit 3, never a green.
p4_assert_enforcing_dataplane "$K" || exit 3
pass "NetworkPolicy-enforcing dataplane present ($P4_DATAPLANE)"

# --- 1) fixtures: two server pods (allowed + denied targets) + a client pod --------------------------
echo "== 1) deploying fixtures in namespace $NS =="
$K create namespace "$NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true
$K -n "$NS" run allowed-server --image="$ALLOWED_IMG" --labels="role=allowed-server" \
  --port=80 >/dev/null 2>&1 || true
$K -n "$NS" run denied-server --image="$ALLOWED_IMG" --labels="role=denied-server" \
  --port=80 >/dev/null 2>&1 || true
. "$(dirname "$0")/../lib/agent-fixtures.sh"
if ! run_tier_fixture_pod "$K" "$NS" client "$CLIENT_IMG" "$TIER"; then
  bad "the tier-labelled client fixture was refused at admission — cannot prove enforcement"
  exit 1
fi

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

probe() { # probe <url> -> 0 if reachable within 4s, non-zero otherwise
  $K -n "$NS" exec client -- curl -s -o /dev/null --max-time 4 "$1" >/dev/null 2>&1
}

# --- 2) baseline (no policy): everything reachable -> proves networking works (real-vs-setup) ---------
echo "== 2) baseline with NO egress policy (every destination must be reachable) =="
if probe "http://$ALLOWED_IP/"; then pass "baseline: allowed-server reachable"; else
  bad "baseline: allowed-server UNREACHABLE with no policy — setup/networking error, not a real deny"; exit 1; fi
if probe "http://$DENIED_IP/"; then pass "baseline: denied-server reachable"; else
  bad "baseline: denied-server UNREACHABLE with no policy — setup/networking error, not a real deny"; exit 1; fi

# The external pair is what makes the ipBlock arm below evidence rather than decoration: an ipBlock
# deny proves nothing on a cluster with no internet egress, where every external probe fails anyway.
# Both must be reachable first, or the whole arm is SKIPPED — never counted as a pass (09 §6).
EXT_BASELINE=0
if probe "https://$EXT_ALLOWED/" && probe "https://$EXT_DENIED/"; then
  EXT_BASELINE=1
  pass "baseline: both external hosts ($EXT_ALLOWED, $EXT_DENIED) reachable on :443"
else
  echo "  NOTE: no internet egress from this cluster — the ipBlock arm will be SKIPPED, not passed."
fi

# --- 3) apply a tier-shaped default-deny + pure allowlist ---------------------------------------------
# The allowlist has two kinds of rule because the production tier netpols have two kinds, and on a
# Cilium-family dataplane they are NOT interchangeable:
#
#   in-cluster destination  -> podSelector / namespaceSelector   (DNS, the control namespace)
#   out-of-cluster address  -> ipBlock                           (metadata server, hub CIDR)
#
# That split is load-bearing on Dataplane V2 and was learned the hard way. This policy used to allow
# its in-cluster destination with `ipBlock: <podIP>/32`, which Calico honours and DPv2 does not:
# Cilium resolves pod-to-pod traffic by ENDPOINT IDENTITY, and a CIDR selector does not name an
# identity, so the rule matched nothing and the on-allowlist probe was denied along with everything
# else. Measured on gke-scratch-kube-agents-dev 2026-07-26: same two pods, same policy otherwise —
# ipBlock <podIP>/32 => BLOCKED, podSelector => REACHABLE.
#
# The failure mode is the dangerous direction. An over-blocking allowlist fails SAFE, so the tempting
# read of that red line is "the fixture is wrong, relax the assertion" — and relaxing it would have
# left a check that only ever proves denial, on a suite whose entire point is that the allowlist
# admits what it lists. Both rule kinds are therefore exercised here, each against the destination
# class it actually governs.
echo "== 3) applying default-deny + pure-allowlist egress (in-cluster: allowed-server by podSelector;"
echo "      external: $EXT_ALLOWED/32 by ipBlock; plus DNS) =="
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
        - podSelector:
            matchLabels:
              role: allowed-server
      ports:
        - { protocol: TCP, port: 80 }
    - to:
        - ipBlock:
            cidr: $EXT_ALLOWED/32
      ports:
        - { protocol: TCP, port: 443 }
EOF
sleep 5 # let the dataplane program the policy

# --- 4) the load-bearing assertions: on-allowlist ALLOWED, off-allowlist DENIED ----------------------
echo "== 4) enforcement assertions =="
if probe "http://$ALLOWED_IP/"; then pass "on-allowlist in-cluster destination ($ALLOWED_IP) is ALLOWED"; else
  bad "on-allowlist in-cluster destination is BLOCKED — the allowlist over-blocks: it denies what it lists"; fi
if probe "http://$DENIED_IP/"; then
  bad "off-allowlist destination ($DENIED_IP) is REACHABLE — egress is NOT enforced (HALT: Accept b fails)"; else
  pass "off-allowlist in-cluster destination ($DENIED_IP) is DENIED (egress enforced)"; fi

if [ "$EXT_BASELINE" -eq 1 ]; then
  if probe "https://$EXT_ALLOWED/"; then pass "on-allowlist EXTERNAL destination ($EXT_ALLOWED:443) is ALLOWED by ipBlock"; else
    bad "on-allowlist EXTERNAL destination ($EXT_ALLOWED:443) is BLOCKED — the ipBlock allow does not admit what it names"; fi
  if probe "https://$EXT_DENIED/"; then
    bad "off-allowlist EXTERNAL destination ($EXT_DENIED:443) is REACHABLE — the allowlist does not bound outbound egress (HALT: Accept b fails)"; else
    pass "off-allowlist EXTERNAL destination ($EXT_DENIED:443) is DENIED"; fi
else
  echo "SKIP: the ipBlock arm — no internet egress at baseline, so neither result would be evidence."
fi

echo
if [ "$fail" -eq 0 ]; then echo "EGRESS ENFORCEMENT: PROVEN (on-allowlist allowed, off-allowlist denied)"; else
  echo "EGRESS ENFORCEMENT: FAILURES ABOVE"; fi
exit "$fail"
