#!/usr/bin/env bash
# V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 · V-BRK-031 at L2 — what a DEPLOYED
# broker does with the credentials it is handed (09 §6.2, 03 §4.1, 05 §7).
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
#         This runs BEFORE every negative and its failure is fatal to the run. Eight "the broker
#         refused this" results prove nothing about a broker that refuses everything — including a
#         broker whose pod is wedged, whose Service resolves nowhere, or that cannot reach the API
#         server to answer a TokenReview. LSN-024 in one sentence, aimed at authentication.
#         This arm also carries V-BRK-031, because passing through it is what produces the
#         condition: the phase-9 actor holds the 06 §2.2.1 grant and no tenant authority, so its
#         step-3 pre-state read of the envelope's target is denied by the real API server, and what
#         the broker answers to that is the row. Injecting a Forbidden into a fake is L1's half; a
#         genuine denial of a genuine actor by a genuine authorizer is only observable here.
#   L2-1  V-BRK-009, the token layer: valid mTLS and no bearer token is 401 `token-required`; valid
#         mTLS and a garbage bearer token is 401 `token-invalid`.
#   L2-2  V-BRK-008 / V-BRK-017, the audience: the pod's own automounted
#         `kubernetes.io/serviceaccount` token — the SAME ServiceAccount the broker expects, and
#         genuinely signed — is refused 401, and the refusal is attributable to the AUDIENCE and not
#         merely to the token. Two layers refuse it and the outer one fires first, which is a real
#         divergence from V-BRK-017's 09 §6 wording; the assertion site below states the divergence,
#         the exact two-branch condition, and why it is narrower rather than weaker.
#         This is the check that a broker which stopped reading at `status.Authenticated` would fail
#         and nothing else here would catch: it would accept every ServiceAccount token in the
#         cluster and still pass L2-0, L2-1 and L2-3.
#   L2-3  V-BRK-007 / V-BRK-009, the transport layer, each arm carrying a VALID token so that a
#         refusal is attributable to the certificate and not to the bearer: no client certificate;
#         plaintext HTTP to :8443; and a syntactically perfect client certificate signed by a CA the
#         mesh never trusted. All three must fail BELOW the broker. For the two certificate arms
#         that means a transport error with no HTTP status ever received; a 401 there would mean the
#         listener completed a handshake it should have refused. The plaintext arm is the exception
#         and it is Go's, not ours: `net/http`'s TLS listener answers an unencrypted request with a
#         bare `400 Bad Request` of its own before any handler runs, so the arm asserts "no handler
#         answered" — an HTTP reply carrying no `reason` field, which every broker refusal has.
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

# MODES. `live` drives a real broker on a real cluster and is what every claim above is about.
# `--negative-control` is the mandatory `¬` arm for all five rows, and it deliberately touches
# NOTHING: it replays the assertion block against hand-written transcripts of a broker that
# MISBEHAVED, and requires every arm to go red. See run_negative_control below for why that is the
# only form the `¬` can take for a suite whose every real result is a refusal.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

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

fail=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END.
#
# This exists because of a defect this file had for exactly one commit: the eight credential arms
# were moved into `assert_credentials` and the call site was not added back. The suite ran, printed
# six green PASS lines, printed "PROVEN: V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017
# at L2", and exited 0 — having asserted none of them. Nothing could have caught it: `fail` stays 0
# when no assertion runs, and the `¬` mode calls `assert_credentials` directly, so it was green too.
# A suite that reports a verdict it did not compute is worse than a suite that fails.
#
# So the number of arms is itself an assertion. If it disagrees, the run is a FAILURE and not a
# smaller pass. Change EXPECTED_ASSERTIONS deliberately, in the same commit as the arm you added.
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}

# 2 x P1 + broker Available + L2-0 nonce + L2-0 envelope (V-BRK-031) + 8 credential arms + V-BRK-010b.
EXPECTED_ASSERTIONS=14

field() { # <scenario> <1=outcome 2=status 3=reason 4=detail 5=retryAfterSeconds>
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

# expect_not_served <scenario> <label>
#   The request never became a broker request. Either the connection failed outright, or something
#   answered that is demonstrably not the broker.
#
#   This exists for the plaintext arm, and the distinction it draws is the whole content of that
#   arm. Go's TLS listener does answer an unencrypted request on an HTTPS port — with `400 Bad
#   Request` and the bare sentence "Client sent an HTTP request to an HTTPS server", written by
#   `net/http` before any handler runs, and followed immediately by a reset. So "a status was
#   received" cannot be the failure condition here. What must be true is that the BROKER never saw
#   it: no `reason` field, because every refusal the broker itself writes carries one.
expect_not_served() {
  local s="$1" label="$2" outcome status reason detail
  if ! seen "$s"; then
    bad "$label — the probe never reported scenario '$s'"
    return
  fi
  outcome="$(field "$s" 1)"
  status="$(field "$s" 2)"
  reason="$(field "$s" 3)"
  detail="$(field "$s" 4)"
  if [ "$outcome" = "transport-error" ]; then
    pass "$label — the connection never formed: $detail"
    return
  fi
  if [ "$outcome" != "http" ]; then
    bad "$label — the probe could not run this scenario: $detail"
    return
  fi
  if [ -n "$reason" ]; then
    bad "$label — the BROKER answered (HTTP $status '$reason'). An unencrypted request reached a handler; the listener is not TLS-only."
    return
  fi
  case "$status" in
    200) bad "$label — HTTP 200 over plaintext. The envelope port is serving without TLS." ;;
    *) pass "$label — refused by the TLS listener before any handler: HTTP $status ${detail:-(no body)}" ;;
  esac
}

# assert_credentials — the eight negatives, read off $FLAT. A function because the mandatory `¬`
# arm below replays exactly these arms against a transcript nobody's broker produced.
assert_credentials() {
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

  # TWO LAYERS REFUSE THIS, AND THE OUTER ONE FIRES FIRST — which 09 §6's V-BRK-017 wording does not
  # anticipate. That row says the token is refused "— `TokenReview` says `authenticated: true` for
  # it", and against a real GKE API server it does not: `APITokenReviewer.Review` puts
  # `audiences: ["kubeagents-broker"]` IN the TokenReview request, so the API server intersects,
  # finds nothing, and returns `authenticated: false` with "token audiences [...] is invalid for the
  # target audiences [kubeagents-broker]". `Authenticate` therefore refuses at the
  # `status.Authenticated` step with `token-invalid` and never reaches its own `containsAudience`.
  #
  # The PROPERTY holds — a default-audience token is refused, which is what V-BRK-008 and V-BRK-017
  # are for. The mechanism the row names is unreachable at L2 by construction and is L1's to cover
  # (`auth_test.go` supplies a reviewer that returns `authenticated: true` with the wrong audiences,
  # which no real API server will do while the broker asks the question correctly). Recorded as a 09
  # §6 wording finding for `harness-improve`; NOT a weakening, because the assertion below is
  # strictly narrower than "401 something":
  #
  #   the refusal must be ATTRIBUTABLE TO THE AUDIENCE. Either the broker's own reason
  #   (`token-audience-invalid`), or `token-invalid` whose detail names the target audience. A
  #   `token-invalid` for a malformed token — which is what the `bad-token` scenario above produces —
  #   does not satisfy this, and that is the confusion the extra clause exists to prevent.
  da_status="$(field default-audience 2)"
  da_reason="$(field default-audience 3)"
  da_detail="$(field default-audience 4)"
  if [ "$(field default-audience 1)" != "http" ]; then
    bad "V-BRK-008/017: the default-audience token produced no HTTP answer: $da_detail"
  elif [ "$da_status" != "401" ]; then
    bad "V-BRK-008/017: the pod's automounted default-audience token was answered HTTP $da_status '$da_reason', not 401: $da_detail"
  elif [ "$da_reason" = "token-audience-invalid" ]; then
    pass "V-BRK-008/017: refused by the BROKER's own audience check — HTTP 401 $da_reason"
  elif [ "$da_reason" = "token-invalid" ] && printf '%s' "$da_detail" | grep -q "kubeagents-broker"; then
    pass "V-BRK-008/017: refused because of the AUDIENCE, by the API server ahead of the broker's own check — HTTP 401 $da_reason: $da_detail"
  else
    bad "V-BRK-008/017: refused HTTP 401 '$da_reason' but not demonstrably because of the audience: $da_detail"
  fi

  # ------------------------------------------------------------------------------------------------
  # L2-3: V-BRK-007 / V-BRK-009 — the transport layer, with a valid token in hand
  # ------------------------------------------------------------------------------------------------
  echo
  echo "== L2-3: V-BRK-007 — mTLS is required, and a valid token does not substitute for it =="
  expect_transport_error no-client-cert "V-BRK-007a: a client presenting no certificate"
  expect_not_served plaintext "V-BRK-007b: plaintext HTTP to the envelope port"
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
}

# assert_security_event <log-text> — V-BRK-010's second clause, over the broker log lines written
# during this run.
assert_security_event() {
  local new_log="$1" n
  if printf '%s\n' "$new_log" | grep -q "broker security refusal"; then
    n="$(printf '%s\n' "$new_log" | grep -c "broker security refusal")"
    pass "V-BRK-010b: the broker raised $n security event(s) during this run"
    printf '%s\n' "$new_log" | grep "broker security refusal" | tail -3 | sed 's/^/  broker| /'
  else
    bad "V-BRK-010b: the broker refused the foreign caller but wrote no 'broker security refusal' line. A refusal nobody can see is not a detection."
  fi
}

# assert_envelope_baseline — L2-0's second half, and V-BRK-031's L2 row.
#
# A function rather than an inline block for one reason: `--negative-control` replays it against
# transcripts of brokers that answered wrongly, which is the only way to show that its
# discrimination IS one, rather than a shape that happens to be green against today's broker.
assert_envelope_baseline() {
  local envelope_status envelope_reason envelope_retry auth_layer_refusal
  envelope_status="$(field envelope-accepted 2)"
  envelope_reason="$(field envelope-accepted 3)"
  envelope_retry="$(field envelope-accepted 5)"

  # WHY THIS DISCRIMINATES ON THE REASON AND NOT ON THE STATUS.
  #
  # It used to read `401 | 403) bad "refused at the AUTH layer"`, which was true when the only 403
  # the actions route could produce was `forbidden-caller`. V-BRK-031 added a second one, and the
  # two are opposites: `forbidden-caller` is the AUTHENTICATOR saying this caller is not this
  # broker's agent at all, and `target-forbidden` is the broker's own ACTOR identity hitting its
  # authority ceiling while reading a target it was correctly permitted to ask about. Two 403s about
  # two different subjects — there, the identity presenting the credential; here, the identity the
  # broker acts as.
  #
  # In shadow mode the second is the EXPECTED answer for every envelope, because the phase-9 actor
  # holds the 06 §2.2.1 broker-operations grant and no tenant authority whatsoever (see
  # `agent-identity.yaml.template`: binding a tenant credential now would hand the actor months of
  # authority ahead of the controls meant to bound it). So a status-only arm would have gone red on
  # a broker doing exactly the right thing. The change is a NARROWING — every reason that was a
  # failure before is still a failure, and one reason that could not previously occur is now named.
  auth_layer_refusal=0
  case "$envelope_reason" in
    forbidden-caller | peer-identity-mismatch | scope-spoofed | token-*) auth_layer_refusal=1 ;;
  esac

  case "$(field envelope-accepted 1)" in
    http)
      case "$envelope_status" in
        401 | 403)
          if [ "$auth_layer_refusal" = 1 ]; then
            bad "an envelope from the agent's own identity was refused at the AUTH layer (HTTP $envelope_status '$envelope_reason'). The nonce path and the actions path do not agree about who this caller is."
          elif [ "$envelope_status" = "403" ] && [ "$envelope_reason" = "target-forbidden" ]; then
            # V-BRK-031 at L2, and the only place in this suite it is observable: it takes a real
            # cluster to produce a real RBAC denial of a real actor by a real authorizer. The L1
            # table can inject an `apierrors.NewForbidden`; it cannot show that what the API server
            # actually returns to the shipped actor is classified as one.
            if [ "$envelope_retry" = "0" ]; then
              pass "V-BRK-031: the actor's authority ceiling is answered as a typed refusal — HTTP 403 target-forbidden, no Retry-After, not 500 internal-error. $(field envelope-accepted 4)"
            else
              bad "V-BRK-031: HTTP 403 target-forbidden carries retryAfterSeconds=$envelope_retry. An RBAC denial does not clear on its own, and a fleet told to wait and try again spends the rest of the phase retrying a permission boundary."
            fi
          else
            bad "the envelope was answered HTTP $envelope_status '$envelope_reason', which is neither an authentication refusal this suite recognises nor the actor's own ceiling: $(field envelope-accepted 4). A 4xx nobody has classified is a hole in this arm, not a pass."
          fi
          ;;
        400)
          # A 400 is not an auth answer, so it does not contradict any of the five rows — which is
          # precisely why it must be loud rather than tolerated. It means the envelope never reached
          # the pipeline, so "the door is open" was demonstrated for GET /nonce and assumed for
          # POST /actions.
          bad "the envelope was rejected as malformed (HTTP 400 '$envelope_reason'): $(field envelope-accepted 4). The probe's envelope does not match 06 §4.1's closed schema — fix the fixture, not the broker."
          ;;
        503)
          # `Authenticate` answers 503 — not 401 — when the TokenReview itself could not be
          # completed (auth.go's "a control plane outage should not become a debugging session about
          # credentials"). So a 503 there means authentication was never actually DECIDED on this
          # path, and the door was not shown to be open: the documented P9-T7d-4 egress hole, which
          # invalidates the baseline rather than merely annotating it.
          #
          # V-BRK-031 gave step 3 a 503 of its own — `snapshot-failed`, a live read that failed
          # transiently. That one is past authentication, so it does not invalidate the rows below;
          # it is still a real fault and still reported, one arm down.
          if [ "$envelope_reason" = "snapshot-failed" ]; then
            bad "the envelope reached step 3 and its live reads failed transiently (HTTP 503 snapshot-failed, retryAfterSeconds=$envelope_retry): $(field envelope-accepted 4). Authentication was decided, so the rows below stand — but the API server was not answering the actor, and this run's baseline is a broker talking to a sick cluster."
          else
            bad "the envelope POST was answered HTTP 503 '$envelope_reason': $(field envelope-accepted 4). The broker could not complete a TokenReview, so authentication on POST /actions was never decided — the negatives below would be measuring an unreachable API server, not a policy."
          fi
          ;;
        5??)
          # An unclassified error from INSIDE the actions handler: server.go's `refuse` falls back to
          # 500 `internal-error` for any error that is not a typed *Refusal.
          #
          # THIS USED TO PASS WITH A NOTE, and that was right at the time — every step-3 live read
          # returned a bare `fmt.Errorf`, so in shadow mode the arm fired on every run, and the
          # defect was known, filed, and owned by the unit that has now closed it. What reaches here
          # after V-BRK-031 is an error nobody has classified, in a handler where every known
          # outcome is typed. Tolerating it a second time would mean this arm can no longer tell
          # "the defect we are tracking" from "a new one".
          bad "the envelope POST died inside the actions handler: HTTP $envelope_status '$envelope_reason' $(field envelope-accepted 4). Every outcome of this route is supposed to be a typed *broker.Refusal — an untyped error is answered 500 internal-error AND journaled nowhere, so the envelope's disposition is recorded in no place at all."
          if [ -n "${broker_pod:-}" ]; then
            echo "  Broker log tail follows."
            $K -n "$NS" logs "pod/$broker_pod" --tail=40 2>/dev/null | sed 's/^/  broker| /'
          fi
          ;;
        *) pass "a shipped-builder envelope reaches the pipeline — HTTP $envelope_status $envelope_reason $(field envelope-accepted 4)" ;;
      esac
      ;;
    *) bad "the envelope POST did not complete: $(field envelope-accepted 4)" ;;
  esac
}

# run_negative_control — the mandatory `¬` for V-BRK-007/008/009/010/017/031.
#
# WHY IT LOOKS LIKE THIS. The usual `¬` shape is "inject the defect, watch the check catch it".
# Here the defect is *a broker that accepts a credential it must refuse*, and there is no way to
# build one: it would mean deploying a deliberately broken `kage-broker` image, which is a build of
# code that must never exist in this repository, on a cluster, listening. So the injection point
# moves one layer out — to the transcript. Every verdict in this suite is derived from the probe's
# JSON and nothing else, so a transcript of a broker that misbehaved exercises the entire assertion
# layer with total fidelity, and it needs no cluster, which is why this mode is an L0 line.
#
# WHAT IT DOES NOT COVER, stated plainly: it proves the assertions are live and correctly attached,
# not that the probe reports faithfully. The probe is covered by the live run's L2-0 — a probe that
# fabricated refusals would have to fabricate the two acceptances too.
#
# THREE TRANSCRIPTS, because there are three distinct ways this suite could be vacuous:
#   admitted     every negative answered 200. Catches an arm that was never wired up at all.
#   wrong-reason every negative refused, each with ANOTHER arm's reason. This is the sharp one: it
#                is what a suite asserting "not 200" would pass, and it is specifically the reading
#                of L2-2 that the two-branch audience condition exists to rule out — the
#                `default-audience` row here is a 401 `token-invalid` whose detail does NOT name the
#                audience, i.e. exactly what a malformed token produces.
#   truncated    the probe died after the first scenario, which is not hypothetical: it happened on
#                this suite's first run and four arms reported nothing. Silence must be red.
run_negative_control() {
  local nc_fail=0 name transcript out n_pass n_fail

  echo "===================================================================="
  echo " NEGATIVE CONTROL (¬) for V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 · V-BRK-031"
  echo " No cluster is addressed. Every credential arm must go RED on all three transcripts."
  echo "===================================================================="

  for name in admitted wrong-reason truncated; do
    case "$name" in
      admitted)
        transcript="$(printf '%s\n' \
          "no-token\thttp\t200\t\t" \
          "bad-token\thttp\t200\t\t" \
          "default-audience\thttp\t200\t\t" \
          "no-client-cert\thttp\t200\t\t" \
          "plaintext\thttp\t200\t\t" \
          "untrusted-client-cert\thttp\t200\t\t" \
          "peer-mismatch\thttp\t200\t\t" \
          "foreign-caller\thttp\t200\t\t")"
        ;;
      wrong-reason)
        transcript="$(printf '%s\n' \
          "no-token\thttp\t401\ttoken-invalid\tthe presented token is not authenticated" \
          "bad-token\thttp\t401\ttoken-required\tno Authorization header" \
          "default-audience\thttp\t401\ttoken-invalid\tthe presented token is not authenticated: invalid bearer token" \
          "no-client-cert\thttp\t401\ttoken-required\tno Authorization header" \
          "plaintext\thttp\t401\ttoken-required\tno Authorization header" \
          "untrusted-client-cert\thttp\t403\tpeer-identity-mismatch\tthe two layers must agree" \
          "peer-mismatch\thttp\t403\tforbidden-caller\tnot this broker reader" \
          "foreign-caller\thttp\t403\tpeer-identity-mismatch\tthe two layers must agree")"
        ;;
      truncated)
        transcript=""
        ;;
    esac

    out="$(FLAT="$transcript" assert_credentials 2>&1)"
    n_pass="$(printf '%s\n' "$out" | grep -c '^PASS:')"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$n_pass" -eq 0 ] && [ "$n_fail" -eq 8 ]; then
      echo "PASS: ¬ transcript '$name' — all 8 credential arms went red, none passed"
    else
      nc_fail=1
      echo "FAIL: ¬ transcript '$name' — expected 0 PASS / 8 FAIL, got $n_pass PASS / $n_fail FAIL."
      echo "  An arm that stays green on a misbehaving broker is not asserting anything. Output:"
      printf '%s\n' "$out" | sed 's/^/    /'
    fi
  done

  # V-BRK-031's own vacuity modes, replayed through assert_envelope_baseline. Four transcripts,
  # each a broker that answered the actor's authority ceiling wrongly in a different way, plus the
  # correct answer so the arm is not merely always-red. The four are exactly the failure modes the
  # 09 §6 row names, and the fourth is the one this arm was rewritten for: a 403 that IS an
  # authentication refusal must still be a failure, or the narrowing would have swallowed the case
  # the arm originally existed to catch.
  local ev_name ev_line ev_want
  while IFS='|' read -r ev_name ev_want ev_line; do
    [ -n "$ev_name" ] || continue
    out="$(FLAT="$(printf 'envelope-accepted\t%s' "$ev_line")" assert_envelope_baseline 2>&1)"
    if printf '%s\n' "$out" | grep -q "^$ev_want:"; then
      echo "PASS: ¬ V-BRK-031 — '$ev_name' is reported as a $ev_want"
    else
      nc_fail=1
      echo "FAIL: ¬ V-BRK-031 — '$ev_name' should have been a $ev_want. Got: $out"
    fi
  done <<'EV'
the ceiling answered as an unclassified 500|FAIL|http	500	internal-error	step 3: capturing pre-state for 1 targets	0
the ceiling answered as a retryable 503|FAIL|http	503	snapshot-failed	forbidden	30
a target-forbidden that tells the fleet to retry|FAIL|http	403	target-forbidden	the actor may not read this	60
a genuine auth refusal, still a failure|FAIL|http	403	forbidden-caller	not this broker's agent	0
the ceiling answered correctly|PASS|http	403	target-forbidden	configmaps "app-config" is forbidden	0
EV

  # V-BRK-010's second clause has its own vacuity mode: a log with no refusal line in it.
  out="$(assert_security_event "some unrelated broker chatter
and another line" 2>&1)"
  if printf '%s\n' "$out" | grep -q '^FAIL: V-BRK-010b'; then
    echo "PASS: ¬ V-BRK-010b — a broker log carrying no refusal line is reported as a failure"
  else
    nc_fail=1
    echo "FAIL: ¬ V-BRK-010b — a log with no 'broker security refusal' line did not go red: $out"
  fi

  # And the converse, so the clause is not merely always-red: a log that does carry one must pass.
  out="$(assert_security_event "2026-01-01T00:00:00Z	INFO	security	broker security refusal	{}" 2>&1)"
  if printf '%s\n' "$out" | grep -q '^PASS: V-BRK-010b'; then
    echo "PASS: ¬ V-BRK-010b — and a log that does carry one is reported as a pass (not always-red)"
  else
    nc_fail=1
    echo "FAIL: ¬ V-BRK-010b — a log carrying a refusal line did not pass: $out"
  fi

  echo "===================================================================="
  if [ "$nc_fail" -eq 0 ]; then
    echo " ¬ SATISFIED: every assertion in this suite fails on a broker that misbehaves."
    echo "===================================================================="
    return 0
  fi
  echo " ¬ FAILED — this suite would have passed a broker it must refuse."
  echo "===================================================================="
  return 1
}

if [ "$MODE" = "negative-control" ]; then
  K="/bin/false"
  cd "$REPO_ROOT" || exit 1
  run_negative_control
  exit $?
fi

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero; that is the
# half that makes the rest of it a guard.
#
# IT SITS BELOW THE `--negative-control` DISPATCH, AND THAT IS THE POINT. The guard is written in
# exactly the shape `invariants-gate.py`'s LSN-005 check can read: an unadorned switch on $CTX
# alone, whose arms it parses and anchors. An earlier draft of this file folded the mode into the
# switch subject, which is equally correct and which the gate cannot see, so it reported this
# script as unguarded. Ordering the modes instead of the patterns keeps one guard, one shape, and
# one mechanism that verifies it. Nothing between the top of the file and here addresses a cluster.
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
#
# THE DETAIL CAP IS LOAD-BEARING, NOT COSMETIC. L2-2 asserts on a SUBSTRING of the detail, so a cap
# short enough to cut that substring turns a correct broker into a FAIL. The first run of this
# suite did exactly that: the API server's wrong-audience message is ~248 characters and names the
# expected audience at the very END, so a 200-character cap removed the only evidence the assertion
# was looking for. Keep this comfortably above the longest detail the broker or the API server can
# produce; it is a readability guard, and readability must never truncate an assertion input.
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
        (r.get("detail") or "").replace("\t", " ")[:1000],
        str(r.get("retryAfterSeconds") or 0),
    ]))
')"

if [ -z "$FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output."
  exit 3
fi

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
  echo "HALTING before the negatives. Eight refusals from a broker that refuses everything are not"
  echo "evidence for any of these five rows. Diagnose this first: the usual causes are the broker"
  echo "being unable to reach the API server for the TokenReview (P9-T7d-4, the documented egress"
  echo "hole), a mesh Certificate not yet issued by cert-manager, or the reader ServiceAccount not"
  echo "matching the one the broker was told to expect."
  $K -n "$NS" logs "pod/$broker_pod" --tail=30 2>/dev/null | sed 's/^/  broker| /'
  exit 1
fi

assert_envelope_baseline

# L2-1 through L2-5a: the eight negatives, over the transcript the driver just produced. Defined
# as a function so `--negative-control` can replay the identical arms against a broker that
# misbehaved; this is its only call site in the live path.
assert_credentials

# L2-5b: V-BRK-010's second clause, over the broker's own log.
assert_security_event "$($K -n "$NS" logs "pod/$broker_pod" 2>/dev/null | tail -n "+$((log_baseline + 1))")"

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 · V-BRK-031 at L2"
  echo " A real reader identity reached a deployed broker; eight other credentials did not."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
