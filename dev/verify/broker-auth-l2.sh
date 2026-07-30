#!/usr/bin/env bash
# V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 at L2 — what a DEPLOYED broker does
# with the credentials it is handed (09 §6.2, 03 §4.1, 05 §7).
#
# All five rows are BLOCKING-ALWAYS, all five carry `¬`, and until this suite existed not one of
# them had a single row in `verification/results.csv`. They are acceptance bullet (c)'s entire L2
# half. The reason they went ungathered for a whole phase is worth stating, because it was not
# oversight: the broker demands mTLS with a certificate the `kubeagents-mesh-ca` issued, a projected
# ServiceAccount token whose audience is `kubeagents-broker`, and the SPIFFE URI in the first to
# name the ServiceAccount in the second — and nothing in `dev/` could assemble that. Every earlier
# broker suite asserted around the door. `broker-per-agent-l2.sh` says so itself: it does not prove
# the broker *serves*, "because no client here holds a certificate".
#
# `dev/lib/broker-driver.sh` holds one. This suite asks it ten questions and checks the answers.
#
# WHAT IS ASSERTED, in order:
#   L2-0  THE DOOR IS OPEN FIRST. A correctly-credentialled caller gets a nonce, and an envelope
#         built by the shipped `action_envelope.build_envelope` is not refused at the auth layer.
#         This runs BEFORE every negative and its failure is fatal to the run. Nine "the broker
#         refused this" results prove nothing about a broker that refuses everything — including a
#         broker whose pod is wedged, whose Service resolves nowhere, or that cannot reach the API
#         server to answer a TokenReview. LSN-024 in one sentence, aimed at authentication.
#   L2-1  V-BRK-009, the token layer: valid mTLS and no bearer token is 401 `token-required`; valid
#         mTLS and a garbage bearer token is 401 `token-invalid`.
#   L2-2  V-BRK-008 / V-BRK-017, the audience: the pod's own automounted
#         `kubernetes.io/serviceaccount` token — the SAME ServiceAccount the broker expects,
#         genuinely signed, and one for which `TokenReview` returns `authenticated: true` — is 401
#         `token-audience-invalid`. This is the check that a broker which stopped reading at
#         `status.Authenticated` would fail and nothing else here would catch: it would accept every
#         ServiceAccount token in the cluster and still pass L2-0, L2-1 and L2-3.
#   L2-3  V-BRK-007 / V-BRK-009, the transport layer, each arm carrying a VALID token so that a
#         refusal is attributable to the certificate and not to the bearer: no client certificate;
#         plaintext HTTP to :8443; and a syntactically perfect client certificate signed by a CA the
#         mesh never trusted. All three must fail as TRANSPORT, with no HTTP status ever received —
#         a 401 here would mean the listener completed a handshake it should have refused.
#   L2-4  THE TWO LAYERS MUST AGREE (V-BRK-009's conjunction, stated as its contrapositive): the
#         cluster-admin agent's mesh certificate presented with the PLATFORM agent's token — both
#         individually valid, both mesh-signed, both live — is 403 `peer-identity-mismatch`. Without
#         the binding this is an authorized write attributed to the wrong agent, and every other
#         check in this file passes.
#   L2-5  V-BRK-010, and it has two halves because the row has two clauses. A consistent FOREIGN
#         reader — certificate and token agreeing with each other and naming the cluster-admin agent
#         — is 403 `forbidden-caller`, AND the broker emits a security event for it, observed as a
#         `broker security refusal` line appearing in the broker pod's log DURING this run. A
#         refusal nobody can see is not a detection.
#
# WHAT THIS DOES NOT CLAIM
#   That the published agent IMAGE carries this transport code. The driver pod mounts
#   `agents/platform/scripts/` from the working tree onto a stock `python:3.12-slim`, because the
#   agent images in Artifact Registry are days stale and are the wrong artifact for the question
#   anyway — what is under test is the shipped source against the deployed broker. Image parity is
#   P1's job, on a row that is not one of these five.
#
#   That cluster DNS publishes the broker's name. The driver pod pins the SAN to the Service's
#   ClusterIP with `hostAliases`, because `<agent>-to-broker` grants a reader pod exactly one egress
#   hop and no DNS rule at all (see the driver's header). TLS still verifies that exact name against
#   the certificate, which is the property these rows are about. That the name resolves is
#   `broker-per-agent-l2.sh`'s L2-3, which reads the Endpoints the API server computed, and is green.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies Agent CRs
# named `platform-agent` and `cluster-admin-cluster-a` in `kubeagents-system`, applies RBAC, mints a
# short-lived token for another agent's ServiceAccount, and runs a pod carrying it. On the live
# install that is a test deleting the fleet's own agents and handing out a real credential.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED (P1/P10 unverifiable).
# Usage: dev/verify/broker-auth-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager AND the platform
#      agent's BROKER pod — both, and for the same reason `broker-per-agent-l2.sh` needs both: the
#      controller chooses the broker image and the broker executes it. Every claim in this file is a
#      claim about what the broker BINARY does with a credential, so a broker one generation behind
#      the tree would answer questions about the previous build's auth chain. Unverifiable → rc 3.
#   P3 admission-recreate: both Agent CRs are deleted and re-applied on every run (`seed_parent_agent`
#      deletes before it applies; the EXIT trap deletes after), so the broker Deployment, its mesh
#      Certificates and the pair NetworkPolicies under test are all rendered fresh by the controller
#      running now. The driver pod, its ConfigMap and the untrusted keypair are likewise created and
#      destroyed within the run. The broker pod is resolved through `p3_pod_of_deploy`, by ownership,
#      so a pod from the previous generation of the same Deployment can never be read as this one's.
#   P6 runtime-authoritative: the driver's entire environment — endpoint, SAN, agent identity, token
#      path, TLS directory — is read off the RENDERED agent Deployment, never recomputed from the
#      naming functions in `broker_manifests.go`. A driver that derived its own endpoint would agree
#      with itself on a cluster whose controller rendered a different one. The verdicts themselves
#      are the broker's own JSON replies and its own log lines; nothing here is read from a golden.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
PLATFORM_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml
CLUSTER_ADMIN_MANIFEST=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
# The agent under test, and the agent whose credentials must be refused. Both are the shipped
# manifests, co-located in one namespace (08 §2.6) — which is the only arrangement in which L2-4 and
# L2-5 can be asked at all, because both need a second mesh identity that the SAME CA signed. A
# fixture with one agent could only ever present an untrusted certificate, and "the CA works" is a
# different and much weaker claim than "a trusted certificate belonging to somebody else is refused".
AGENT=platform-agent
FOREIGN_AGENT=cluster-admin-cluster-a

DRIVER_POD=broker-auth-l2-driver
DRIVER_CM=broker-auth-l2-code
UNTRUSTED_SECRET=broker-auth-l2-untrusted

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero; that is the
# half that makes the rest of it a guard.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (destructive-test guard)." >&2
    echo "  This DELETES Agent CRs named $AGENT and $FOREIGN_AGENT, and mints a bearer token for" >&2
    echo "  another agent's ServiceAccount. Name the dev cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() {
  echo "FAIL: $1"
  fail=1
}

cd "$REPO_ROOT" || exit 1

echo "===================================================================="
echo " V-BRK-007/008/009/010/017 at L2 — a real caller at the broker's door"
echo " ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  exit 1
}

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/parent-chain.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/broker-driver.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

seeded=()
cleanup() {
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete configmap broker-auth-l2-probe-target --ignore-not-found --wait=false >/dev/null 2>&1
  unseed_parent_agents "$K" "${seeded[@]:-}"
  echo
  echo "CLEANED UP: the driver pod, its code ConfigMap, the throwaway keypair and both Agent CRs are"
  echo "  deleted. The actor ServiceAccounts and the broker-operations grant are LEFT — a real"
  echo "  install creates those once per namespace and outlives every CR in it. The foreign token"
  echo "  minted for L2-5 is not revocable and is not revoked; it expires in 15 minutes, grants"
  echo "  nothing this broker will honour, and exists only on a scratch cluster."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures: the two shipped CRs and the identities their brokers run as
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: two co-located Agent CRs and their actor identities =="

for m in "$PLATFORM_MANIFEST" "$CLUSTER_ADMIN_MANIFEST"; do
  # Order matters: the cluster-admin CR's parentRef names the platform agent, and 06 §1.2 V-6
  # rejects a child whose parent does not exist.
  if ref="$(seed_parent_agent "$K" "$m")"; then
    seeded+=("$ref")
    echo "  seeded $ref from $m"
  else
    echo "FAIL: could not seed $m: $ref" >&2
    exit 1
  fi
done

for a in "$AGENT" "$FOREIGN_AGENT"; do
  seed_agent_fixtures "$K" "$NS" "$a" || {
    echo "FAIL: could not seed fixtures for $a" >&2
    exit 1
  }
  seed_agent_identity "$K" "$NS" "$a" || {
    echo "FAIL: could not seed the actor identity for $a" >&2
    exit 1
  }
done

# ------------------------------------------------------------------------------------------------
# P1: the build under test, on both ends of the image indirection
# ------------------------------------------------------------------------------------------------
echo
echo "== P1: the controller and this agent's broker are the build under test =="

p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the controller (see above). The controller renders the"
    echo "  broker, its mesh Certificates and the pair NetworkPolicies; nothing below would be"
    echo "  evidence about this commit."
    exit 3
    ;;
  *)
    bad "P1: the controller is not running the build under test"
    exit 1
    ;;
esac

broker_deploy="${AGENT}-broker"
broker_pod="$(p3_pod_of_deploy "$K" "$NS" "$broker_deploy" 180)"
if [ -z "$broker_pod" ]; then
  echo "DEFERRED: no pod is owned by deploy/$broker_deploy after 180s. There is no broker to ask."
  $K -n "$NS" describe "deploy/$broker_deploy" 2>&1 | tail -20
  exit 3
fi
echo "  broker pod (by ownership, P3): $broker_pod"

p1_assert_build_under_test "$K" "$NS" "kube-agents/agent=$AGENT,kube-agents/role=actor"
case "$?" in
  0) pass "P1: the broker is running the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the broker. Every claim in this suite is a claim about what"
    echo "  the broker BINARY does with a credential; an unidentifiable binary makes all five rows"
    echo "  statements about an unknown build."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

# The broker must be SERVING before a refusal means anything. Polled, not slept on: `.status` here
# is controller-written and a fixed wait is how a slow image pull becomes "the broker refuses
# everything".
echo
echo "== waiting for the broker to become Available =="
avail=""
deadline=$((SECONDS + 240))
while [ "$SECONDS" -lt "$deadline" ]; do
  avail="$($K -n "$NS" get "deploy/$broker_deploy" \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)"
  [ "$avail" = "True" ] && break
  sleep 3
done
if [ "$avail" != "True" ]; then
  echo "DEFERRED: deploy/$broker_deploy never became Available (condition='${avail:-none}')."
  $K -n "$NS" describe "pod/$broker_pod" 2>&1 | tail -25
  exit 3
fi
pass "deploy/$broker_deploy is Available"

# Where the broker's log stands BEFORE the run. L2-5's second half must observe a security event
# this suite caused, and a broker pod that has served earlier suites already has refusals in its
# log. Counting first turns "there is a refusal in the log" into "this run produced one".
log_baseline="$($K -n "$NS" logs "pod/$broker_pod" 2>/dev/null | wc -l | tr -d ' ')"
[ -n "$log_baseline" ] || log_baseline=0
echo "  broker log baseline: $log_baseline lines"

# ------------------------------------------------------------------------------------------------
# Run the driver
# ------------------------------------------------------------------------------------------------
echo
echo "== driving the broker from inside the cluster =="

broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the untrusted client keypair" >&2
  exit 1
}

driver_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$FOREIGN_AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion, so no credential was ever presented."
  echo "  This is an inability to run the experiment, not a property that failed (P10's distinction)."
  exit 3
fi

echo "$driver_out" | sed 's/^/  | /'

# One pass over the probe's JSON, flattened to tab-separated fields the shell can assert on.
FLAT="$(printf '%s\n' "$driver_out" | python3 -c '
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    status = r.get("status")
    print("\t".join([
        r.get("scenario") or "",
        r.get("outcome") or "",
        "" if status is None else str(status),
        r.get("reason") or "",
        (r.get("detail") or "").replace("\t", " ")[:200],
    ]))
')"

if [ -z "$FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output."
  exit 3
fi

field() { # <scenario> <1=outcome 2=status 3=reason 4=detail>
  printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" -v i="$(($2 + 1))" '$1 == s { print $i; exit }'
}
seen() { printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" '$1 == s { found = 1 } END { exit !found }'; }

# expect_http <scenario> <status> <reason> <label>
#   An HTTP answer with exactly this status and reason. A `transport-error` here is a FAILURE and not
#   a near-miss: it would mean the connection died before the broker got to make the decision under
#   test, and the row would be recording the network's verdict as the broker's.
expect_http() {
  local s="$1" want_status="$2" want_reason="$3" label="$4" outcome status reason detail
  if ! seen "$s"; then
    bad "$label — the probe never reported scenario '$s'"
    return
  fi
  outcome="$(field "$s" 1)"
  status="$(field "$s" 2)"
  reason="$(field "$s" 3)"
  detail="$(field "$s" 4)"
  if [ "$outcome" != "http" ]; then
    bad "$label — expected an HTTP $want_status/$want_reason, got outcome '$outcome': $detail"
    return
  fi
  if [ "$status" != "$want_status" ] || [ "$reason" != "$want_reason" ]; then
    bad "$label — expected HTTP $want_status '$want_reason', got HTTP $status '$reason': $detail"
    return
  fi
  pass "$label — HTTP $status $reason"
}

# expect_transport_error <scenario> <label>
#   No HTTP status was ever received. This is the pass condition for the mTLS arms and it is
#   deliberately not "any failure": a 401 would mean the listener completed a handshake with a
#   client it must refuse, and the broker's `RequireAndVerifyClientCert` would be decorative.
expect_transport_error() {
  local s="$1" label="$2" outcome status detail
  if ! seen "$s"; then
    bad "$label — the probe never reported scenario '$s'"
    return
  fi
  outcome="$(field "$s" 1)"
  status="$(field "$s" 2)"
  detail="$(field "$s" 4)"
  case "$outcome" in
    transport-error) pass "$label — refused at the transport: $detail" ;;
    http) bad "$label — the connection SUCCEEDED and the broker answered HTTP $status. The listener completed a handshake it must refuse." ;;
    *) bad "$label — the probe could not run this scenario: $detail" ;;
  esac
}

# ------------------------------------------------------------------------------------------------
# L2-0: the door is open, first
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-0: a correctly-credentialled caller GETS IN (so the negatives are not vacuous) =="

if [ "$(field nonce-accepted 1)" = "http" ] && [ "$(field nonce-accepted 2)" = "200" ]; then
  pass "the agent's own mesh certificate + audience token is issued a nonce — $(field nonce-accepted 4)"
else
  bad "the agent's own credentials were NOT accepted: outcome='$(field nonce-accepted 1)' status='$(field nonce-accepted 2)' reason='$(field nonce-accepted 3)' — $(field nonce-accepted 4)"
  echo
  echo "HALTING before the negatives. Nine refusals from a broker that refuses everything are not"
  echo "evidence for any of these five rows. Diagnose this first: the usual causes are the broker"
  echo "being unable to reach the API server for the TokenReview (P9-T7d-4, the documented egress"
  echo "hole), a mesh Certificate not yet issued by cert-manager, or the reader ServiceAccount not"
  echo "matching the one the broker was told to expect."
  $K -n "$NS" logs "pod/$broker_pod" --tail=30 2>/dev/null | sed 's/^/  broker| /'
  exit 1
fi

envelope_status="$(field envelope-accepted 2)"
case "$(field envelope-accepted 1)" in
  http)
    case "$envelope_status" in
      401 | 403) bad "an envelope from the agent's own identity was refused at the AUTH layer (HTTP $envelope_status '$(field envelope-accepted 3)'). The nonce path and the actions path do not agree about who this caller is." ;;
      *) pass "a shipped-builder envelope reaches the pipeline — HTTP $envelope_status $(field envelope-accepted 3)" ;;
    esac
    ;;
  *) bad "the envelope POST did not complete: $(field envelope-accepted 4)" ;;
esac

# ------------------------------------------------------------------------------------------------
# L2-1: V-BRK-009 — the token layer, with the certificate layer already satisfied
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-1: V-BRK-009 — valid mTLS is not sufficient on its own =="
expect_http no-token 401 token-required "V-BRK-009a: mTLS with no bearer token"
expect_http bad-token 401 token-invalid "V-BRK-009b: mTLS with an unverifiable bearer token"

# ------------------------------------------------------------------------------------------------
# L2-2: V-BRK-008 / V-BRK-017 — the audience
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-2: V-BRK-008 / V-BRK-017 — a genuine, authenticated, WRONG-audience token =="
expect_http default-audience 401 token-audience-invalid \
  "V-BRK-008/017: the pod's automounted default-audience token (TokenReview says authenticated: true)"

# ------------------------------------------------------------------------------------------------
# L2-3: V-BRK-007 / V-BRK-009 — the transport layer, with a valid token in hand
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-3: V-BRK-007 — mTLS is required, and a valid token does not substitute for it =="
expect_transport_error no-client-cert "V-BRK-007a: a client presenting no certificate"
expect_transport_error plaintext "V-BRK-007b: plaintext HTTP to the envelope port"
expect_transport_error untrusted-client-cert "V-BRK-007c: a well-formed certificate from a CA the mesh never trusted"

# ------------------------------------------------------------------------------------------------
# L2-4: the two layers must agree
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-4: V-BRK-009 — a mesh-signed certificate and a valid token that name different workloads =="
expect_http peer-mismatch 403 peer-identity-mismatch \
  "V-BRK-009c: ${FOREIGN_AGENT}'s certificate carrying ${AGENT}'s token"

# ------------------------------------------------------------------------------------------------
# L2-5: V-BRK-010 — the foreign reader, refused AND recorded
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-5: V-BRK-010 — a consistent foreign reader is refused and raises a security event =="
expect_http foreign-caller 403 forbidden-caller \
  "V-BRK-010a: ${FOREIGN_AGENT}'s reader identity, certificate and token agreeing"

# The second clause. Only lines the broker wrote AFTER the baseline count, so an inherited refusal
# from an earlier suite cannot be read as this one's.
new_log="$($K -n "$NS" logs "pod/$broker_pod" 2>/dev/null | tail -n "+$((log_baseline + 1))")"
if printf '%s\n' "$new_log" | grep -q "broker security refusal"; then
  n="$(printf '%s\n' "$new_log" | grep -c "broker security refusal")"
  pass "V-BRK-010b: the broker raised $n security event(s) during this run"
  printf '%s\n' "$new_log" | grep "broker security refusal" | tail -3 | sed 's/^/  broker| /'
else
  bad "V-BRK-010b: the broker refused the foreign caller but wrote no 'broker security refusal' line. A refusal nobody can see is not a detection."
fi

# ------------------------------------------------------------------------------------------------
echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 at L2"
  echo " A real reader identity reached a deployed broker; nine other credentials did not."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
