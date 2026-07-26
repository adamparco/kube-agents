#!/usr/bin/env bash
# Tenant isolation at L2 — V-CMP-001 / V-ISO (Phase 8, P8-T3).
#
# Two tenant-isolation manifests have existed in examples/gitops-repo/ since Phase 3 — a
# ResourceQuota and a namespace default-deny NetworkPolicy — and NO INSTALL PATH APPLIED EITHER.
# P8-T3 renders both from templates and wires them into provisioning. This script proves the two
# controls actually bind, on a real dataplane, against a live API server.
#
# WHAT EACH HALF PROVES, AND WHY IT NEEDS A CLUSTER:
#
#   QUOTA — admission-time, no CNI needed. The claim is not "the object exists" but "a pod that does
#   not fit is refused". That is an API-server behaviour; the only way to observe it is to submit a
#   pod and be told no. Section 2 submits four: one with no requests at all (must be refused — this
#   is the coupling provision_12 orders itself around), one shaped like the agent's rendered spec
#   (must be admitted), one exceeding the ceiling (must be refused), and it checks the quota's own
#   accounting moves.
#
#   NETWORK — enforcement is a property of the CNI (LSN-006). kindnet accepts a NetworkPolicy and
#   enforces nothing, so on kindnet this half is DEFERRED, never passed (binding.md P4). Section 4
#   baselines every probe with no policy in force, then applies the allowlist, then the floor — in
#   the order the install path applies them — and re-probes at each stage. A deny that was never
#   first shown to be an allow is not evidence.
#
# THE SEAM THIS SCRIPT DOES NOT CROSS. It renders the manifests through the same common.sh helpers
# the install path calls, so the bytes under test are the bytes that ship. It does not execute
# provision_12/13 themselves — those need cloud state this cluster does not have. That the drivers
# actually invoke those steps is a separate, cheaper claim, proven at L0 by
# dev/tests/install-path-wired.py. Neither check is sufficient alone and both are required.
#
# DESTRUCTIVE-TEST GUARD: Kind / scratch-GKE contexts only, anchored. This one creates namespaces,
# pods and policies, so the guard is load-bearing rather than a formality.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED.
# Usage: dev/verify/tenant-isolation-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each
# recurred against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  none — the ResourceQuota and default-deny under test are rendered by this script from
#      `common.sh`, and enforced by the API server and Calico. No operator code path is involved.
#   P3 admission-recreate: every probe pod and every fixture pod. Section 2 submits pods AFTER the quota is in force, which
#      is the only way to observe a quota refusal; a pod admitted before it would keep running.
#   P6 runtime-authoritative: the rendered manifests from `common.sh` and the live objects read back from the API server.
set -uo pipefail

CTX="${1:-kind-kube-agents-dev}"
K="kubectl --context $CTX"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="$REPO/k8s-operator/scripts"

# Two scratch namespaces, not one. The quota forces every pod in its namespace to declare
# requests+limits, which would otherwise silently constrain the network fixtures and make a
# scheduling failure look like a policy deny.
QNS="tenant-iso-quota-l2"
NNS="tenant-iso-net-l2"
FOREIGN_NS="tenant-iso-foreign-l2"
CTRL_NS="kubeagents-system"
TIER="developer-team"
SERVER_IMG="python:3.12-alpine"
CLIENT_IMG="curlimages/curl:8.10.1"

case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() {
  echo "FAIL: $1"
  fail=1
}
note() { echo "  ...: $1"; }

render() { # render <helper> <args...>
  (
    cd "$SCRIPTS" || exit 1
    SCRIPT_DIR="$SCRIPTS"
    # shellcheck disable=SC1091
    source ./common.sh --dry-run >/dev/null 2>&1
    "$@"
  )
}

cleanup() {
  for ns in "$QNS" "$NNS" "$FOREIGN_NS"; do
    $K delete namespace "$ns" --ignore-not-found=true --wait=false >/dev/null 2>&1
  done
}
trap cleanup EXIT

# --- 0) preconditions ------------------------------------------------------------------------------
echo "== 0) preconditions =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable."
  exit 3
fi

ENFORCING="unknown"
if $K -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  ENFORCING="calico"
elif $K -n kube-system get daemonset anetd >/dev/null 2>&1 || $K -n kube-system get daemonset cilium >/dev/null 2>&1; then
  ENFORCING="dataplane-v2"
elif $K -n kube-system get daemonset kindnet >/dev/null 2>&1; then
  ENFORCING="kindnet"
fi
note "dataplane: $ENFORCING"
pass "cluster reachable"

for ns in "$QNS" "$NNS" "$FOREIGN_NS"; do
  $K create namespace "$ns" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1
done

# --- 1) the quota renders and applies ---------------------------------------------------------------
echo
echo "== 1) the rendered ResourceQuota applies =="
QUOTA="$(render render_tenant_quota "$QNS")"
if [ -z "$QUOTA" ]; then
  bad "render_tenant_quota produced nothing"
  exit 1
fi
if printf '%s' "$QUOTA" | grep -q 'REPLACE_WITH_\|PLACEHOLDER\|\${'; then
  bad "the rendered quota still contains an unsubstituted token"
fi
if printf '%s\n' "$QUOTA" | $K apply -f - >/dev/null 2>&1; then
  pass "ResourceQuota applied in $QNS"
else
  bad "the rendered ResourceQuota does not apply"
  exit 1
fi

# `.status.hard` and not `.spec.hard`, because P6 wants the artifact the runtime acknowledged
# rather than the one we asked for -- but status is written by the quota controller, so it is
# EMPTY for a while after the apply and this read had no wait at all. On 2026-07-25 that raced:
# the same quota that section 2 proves binds (a 200-CPU pod refused, `used.requests.cpu=500m`
# accounted) was reported here as capping NOTHING on all five axes. Measured on the Calico
# cluster, status took 21s and five polls to appear; on the faster dev cluster the unwaited read
# won often enough to look green. A check that reports "caps nothing" about a working quota is
# worse than one that is simply slow -- and it fails in the safe direction only by luck, since
# the identical empty read is what a genuinely empty quota returns.
HARD=""
for _i in $(seq 1 60); do
  HARD="$($K -n "$QNS" get resourcequota "${QNS}-quota" -o jsonpath='{.status.hard}' 2>/dev/null)"
  [ -n "$HARD" ] && break
  sleep 1
done
if [ -z "$HARD" ]; then
  bad "the quota controller never populated .status.hard in 60s — the quota object exists but"
  bad "  nothing has acknowledged it, so it caps nothing yet. Not a slow read; an unenforced quota."
fi
note "hard: $HARD"
for k in '"requests.cpu"' '"requests.memory"' '"limits.cpu"' '"limits.memory"' '"pods"'; do
  if printf '%s' "$HARD" | grep -q "$k"; then
    pass "quota bounds $k"
  else
    bad "quota does not bound $k — it caps nothing on that axis"
  fi
done

# --- 2) the quota actually refuses pods -------------------------------------------------------------
# The object existing is not the control. These four probes are the control.
echo
echo "== 2) admission: what the quota admits and refuses =="

mkpod() { # mkpod <name> <resources-json>; echoes kubectl's error on rejection
  $K -n "$QNS" run "$1" --image="$SERVER_IMG" --restart=Never \
    --overrides="{\"spec\":{\"containers\":[{\"name\":\"c\",\"image\":\"$SERVER_IMG\",\"command\":[\"sleep\",\"3600\"],\"resources\":$2}]}}" \
    --dry-run=server -o name 2>&1
}

NO_RES='{}'
FITS='{"requests":{"cpu":"500m","memory":"2Gi"},"limits":{"cpu":"2","memory":"4Gi"}}'
TOO_BIG='{"requests":{"cpu":"200","memory":"2Gi"},"limits":{"cpu":"400","memory":"4Gi"}}'

out="$(mkpod q-no-resources "$NO_RES")"
if printf '%s' "$out" | grep -qi 'must specify\|failed quota\|exceeded quota'; then
  pass "a pod declaring NO requests/limits is REFUSED — this is the coupling provision_12 relies on"
  note "  $(printf '%s' "$out" | head -1 | cut -c1-140)"
else
  bad "a pod with no requests/limits was ADMITTED. The quota is not forcing declarations, so"
  bad "  applying it before the agent pod proves nothing. Got: $(printf '%s' "$out" | head -1)"
fi

out="$(mkpod q-agent-shaped "$FITS")"
if printf '%s' "$out" | grep -q '^pod/'; then
  pass "a pod shaped like the agent's rendered spec (500m/2Gi req, 2/4Gi lim) is ADMITTED"
else
  bad "the agent-shaped pod was REFUSED by the tenant quota. provision_12 would fail at install."
  bad "  $(printf '%s' "$out" | head -2)"
fi

out="$(mkpod q-too-big "$TOO_BIG")"
if printf '%s' "$out" | grep -qi 'exceeded quota'; then
  pass "a pod requesting 200 CPU is REFUSED — the ceiling binds"
else
  bad "a pod far over the quota ceiling was ADMITTED — the quota is not a bound. Got: $(printf '%s' "$out" | head -1)"
fi

# Accounting: a real admitted pod must move `used`, or the quota is decorative.
$K -n "$QNS" run q-real --image="$SERVER_IMG" --restart=Never \
  --overrides="{\"spec\":{\"containers\":[{\"name\":\"c\",\"image\":\"$SERVER_IMG\",\"command\":[\"sleep\",\"3600\"],\"resources\":$FITS}]}}" \
  >/dev/null 2>&1
# Poll rather than `sleep 3`. `used` is quota-controller output like `hard` above, and the same
# 21 s that field took on this cluster would have blown straight through a three-second sleep. It
# has been passing on luck: a fixed sleep encodes a guess about a controller's latency, and the
# guess is re-made every time the cluster is slower than the day the number was chosen.
USED_CPU=""
for _i in $(seq 1 60); do
  USED_CPU="$($K -n "$QNS" get resourcequota "${QNS}-quota" -o jsonpath='{.status.used.requests\.cpu}' 2>/dev/null)"
  [ "$USED_CPU" = "500m" ] && break
  sleep 1
done
if [ "$USED_CPU" = "500m" ]; then
  pass "quota accounting tracks the admitted pod (used.requests.cpu=$USED_CPU)"
else
  bad "quota accounting did not move after admitting a 500m pod (used.requests.cpu='$USED_CPU')"
fi

# --- 3) network fixtures ----------------------------------------------------------------------------
if [ "$ENFORCING" != "calico" ] && [ "$ENFORCING" != "dataplane-v2" ]; then
  echo
  echo "DEFERRED (network half): dataplane is '$ENFORCING', which ACCEPTS NetworkPolicy and ENFORCES"
  echo "  NOTHING. Any deny observed here would be a false green. Re-run on Calico:"
  echo "    dev/cluster/up.sh && $0 kind-kube-agents-dev"
  echo
  if [ "$fail" -ne 0 ]; then
    echo "V-CMP-001 (L2, quota half): FAILURES ABOVE"
    exit 1
  fi
  echo "V-CMP-001 (L2): quota half PROVEN; network half DEFERRED on a named blocker (non-enforcing CNI)."
  exit 3
fi

echo
echo "== 3) network fixtures =="
$K -n "$NNS" run svc-intra --image="$SERVER_IMG" --restart=Never \
  --command -- python3 -m http.server 80 >/dev/null 2>&1
$K -n "$NNS" run tier-client --image="$CLIENT_IMG" --restart=Never \
  --labels="kube-agents/tier=$TIER" --command -- sleep 3600 >/dev/null 2>&1
# An ordinary tenant workload, NOT tier-labelled. The allowlist does not select it; the floor does.
# This pod is what distinguishes "the agent is contained" from "the namespace is contained".
$K -n "$NNS" run plain-client --image="$CLIENT_IMG" --restart=Never \
  --command -- sleep 3600 >/dev/null 2>&1
$K -n "$FOREIGN_NS" run foreign-client --image="$CLIENT_IMG" --restart=Never \
  --command -- sleep 3600 >/dev/null 2>&1

for spec in "$NNS/svc-intra" "$NNS/tier-client" "$NNS/plain-client" "$FOREIGN_NS/foreign-client"; do
  if ! $K -n "${spec%%/*}" wait --for=condition=Ready "pod/${spec##*/}" --timeout=180s >/dev/null 2>&1; then
    bad "fixture ${spec} never became Ready — cannot prove enforcement"
    exit 1
  fi
done

IP_INTRA="$($K -n "$NNS" get pod svc-intra -o jsonpath='{.status.podIP}')"
if [ -z "$IP_INTRA" ]; then
  bad "could not resolve the fixture pod IP — setup error, not a policy result"
  exit 1
fi
note "intra-namespace server: $IP_INTRA:80"

http() { # http <ns> <pod> <ip> <port>; 0 = reachable
  $K -n "$1" exec "$2" -- curl -s -o /dev/null --max-time 5 "http://$3:$4/" >/dev/null 2>&1
}
dns() { # dns <ns> <pod>; 0 = the pod can reach kube-dns
  # A DNS lookup, not an HTTP request to the API server. The first version of this probe curled
  # http://kubernetes.default.svc — which serves 443, never 80 — so it failed in the baseline and
  # the suite (correctly) SKIPPED the two assertions that depend on it rather than passing them.
  # Those two are the ones proving NetworkPolicy's union is additive, i.e. that the floor does not
  # revoke what the allowlist granted, which is the entire justification for the apply order.
  # `timeout` bounds a blocked lookup to ~5s instead of busybox's default retry chain.
  $K -n "$1" exec "$2" -- timeout 8 nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1
}

# --- 4) baseline: no policy in force ----------------------------------------------------------------
echo
echo "== 4) baseline (no policy) — every probe must succeed =="
BASE_DNS=1
for t in "$NNS tier-client intra tier-client->intra:80" "$NNS plain-client intra plain-client->intra:80" \
  "$FOREIGN_NS foreign-client intra foreign->intra:80"; do
  set -- $t
  if http "$1" "$2" "$IP_INTRA" 80; then
    pass "baseline: $4 reachable"
  else
    bad "baseline: $4 UNREACHABLE with no policy — setup error, so no later deny is evidence"
    exit 1
  fi
done
if dns "$NNS" tier-client; then
  pass "baseline: tier-client resolves + reaches the API server via DNS"
else
  BASE_DNS=0
  note "baseline: DNS/API unreachable even with no policy — the 'allowlist preserves DNS' probe"
  note "          will be SKIPPED, not counted as a pass (09 §6)."
fi

# --- 5) apply the allowlist (install order: allowlist first) ----------------------------------------
echo
echo "== 5) the per-tier egress allowlist, applied as provision_13 applies it =="
ALLOW="$(CONTROL_NAMESPACE="$CTRL_NS" render render_egress_policy "developer-team-egress" "$NNS" "$TIER")"
printf '%s\n' "$ALLOW" | $K apply -f - >/dev/null 2>&1 || {
  bad "the rendered allowlist does not apply"
  exit 1
}
sleep 5

if http "$NNS" tier-client "$IP_INTRA" 80; then
  bad "tier-client still reaches an intra-namespace pod on :80. The allowlist admits only the control"
  bad "  namespace, private-Google and GitHub — an arbitrary in-namespace pod is not on it."
else
  pass "allowlist: tier-client -> intra-namespace :80 is DENIED (not an allowlisted destination)"
fi
if http "$NNS" plain-client "$IP_INTRA" 80; then
  pass "allowlist: plain-client -> intra :80 still ALLOWED (the allowlist selects only tier pods)"
else
  bad "allowlist: plain-client lost egress, but no policy selects it. The podSelector is wrong."
fi
if [ "$BASE_DNS" -eq 1 ]; then
  if dns "$NNS" tier-client; then
    pass "allowlist: tier-client keeps DNS + API reachability (rule 1 and rule 2 hold)"
  else
    bad "allowlist: tier-client lost DNS. The agent cannot resolve anything; rule 1 is broken."
  fi
fi

# --- 6) apply the floor (install order: floor second) -----------------------------------------------
echo
echo "== 6) the tenant default-deny floor, applied second =="
FLOOR="$(render render_tenant_default_deny "$NNS")"
printf '%s\n' "$FLOOR" | $K apply -f - >/dev/null 2>&1 || {
  bad "the rendered default-deny floor does not apply"
  exit 1
}
sleep 5

if http "$NNS" plain-client "$IP_INTRA" 80; then
  bad "floor: an ordinary tenant workload STILL has egress. The empty podSelector is not selecting"
  bad "  every pod, so the namespace is not default-deny and only the agent was ever contained."
else
  pass "floor: plain-client -> intra :80 is now DENIED (the floor covers non-agent workloads)"
fi
if http "$FOREIGN_NS" foreign-client "$IP_INTRA" 80; then
  bad "floor: a pod in another namespace still reaches into $NNS. Ingress is not denied — the agent"
  bad "  pod is addressable from outside, which 03 §11 says it must never be."
else
  pass "floor: cross-namespace ingress into $NNS is DENIED"
fi
if [ "$BASE_DNS" -eq 1 ]; then
  if dns "$NNS" tier-client; then
    pass "floor+allowlist: tier-client STILL has DNS — the union is additive, the floor did not"
    pass "  revoke what the allowlist granted (this is why apply order is safe)"
  else
    bad "floor+allowlist: the floor revoked the allowlist's DNS grant. NetworkPolicy is supposed to"
    bad "  be additive; if this fails the whole allowlist-then-floor ordering argument is wrong."
  fi
fi

# --- 7) negative control ----------------------------------------------------------------------------
# Everything above is a deny. If this cluster drops that traffic for some reason unrelated to the
# policies, every deny is a false green. Prove the probe still reports reachability when it exists.
echo
echo "== 7) negative control =="
$K -n "$NNS" delete networkpolicy default-deny-all developer-team-egress >/dev/null 2>&1
sleep 5
if http "$NNS" plain-client "$IP_INTRA" 80; then
  pass "control: with the policies removed the same probe succeeds — the denies above were the"
  pass "  policies, not a broken fixture"
else
  bad "control: the probe FAILS even with no policy in force. Every deny above is unexplained and"
  bad "  none of them is evidence."
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "V-CMP-001 (L2): FAILURES ABOVE"
  exit 1
fi
echo "V-CMP-001 (L2): PROVEN on $ENFORCING — the tenant quota refuses undeclared and oversized pods"
echo "  and admits the agent's shape; the default-deny floor contains non-agent workloads and blocks"
echo "  cross-namespace ingress; the allowlist survives the floor; the control fires."
exit 0
