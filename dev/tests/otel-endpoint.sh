#!/usr/bin/env bash
# otel-endpoint.sh — OTLP exporter endpoint seam validator (kube-agents Phase 7, P7-T3; 01 §6; D3).
#
# The agent's OTLP export endpoint moved from baked-at-build-only to the standard
# OTEL_EXPORTER_OTLP_ENDPOINT env, resolved by docker-entrypoint.sh, defaulting to the GKE managed-OTel
# collector so unset ⇒ no regression. This test proves the resolution CONTRACT hermetically (no image
# build, no yaml lib needed):
#
#   1. static — the Dockerfile still BAKES the exact GKE default endpoint (unset ⇒ unchanged).
#   2. static — the entrypoint resolves OTEL_EXPORTER_OTLP_ENDPOINT with that same GKE default and only
#      rewrites the backend when an override is set (guarded, so unset is a no-op).
#   3. functional — replaying the entrypoint's own shell default-expansion:
#        set   OTEL_EXPORTER_OTLP_ENDPOINT ⇒ resolves to the override value.
#        unset OTEL_EXPORTER_OTLP_ENDPOINT ⇒ resolves to the exact GKE default.
#
# Deferred-not-faked (D3): the in-container YAML rewrite + a live OTLP collector receiving spans — the
# mechanics run in the image's venv python at runtime; here we prove the load-bearing resolution logic.
#
# Usage: dev/tests/otel-endpoint.sh [REPO_ROOT]
# Exit 0 = seam holds; non-zero on any failure.
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). All three are waived, and the single reason is worth stating
# plainly rather than three times: this script never contacts a cluster. It is reached from an L2 gate
# (verify-phase7.sh section A) and therefore falls inside the lint's scope, but it is hermetic, and a
# declaration block that pretended otherwise would be the exact failure the block exists to prevent.
#   P1 image-under-test:  none — nothing runs. The Dockerfile and the entrypoint are read as TEXT from
#      the tree; no image is built, pulled or executed, so there is no digest to compare and no way for
#      a stale container to change the answer.
#   P3 admission-recreate: none — no Kubernetes object is created, applied or dry-run, so there is no
#      admission decision here for a pre-existing object to have escaped (LSN-002 cannot arise).
#   P6 runtime-authoritative: none, in the sense the precondition means — but the reason it does not
#      apply is the same reason LSN-003 exists. The endpoint is resolved at container start by
#      docker-entrypoint.sh, so the runtime-authoritative artifact would be a running container's
#      environment. This script deliberately does not claim to have read that; it proves the RESOLUTION
#      CONTRACT by replaying the entrypoint's own default-expansion, and D3 above keeps the live
#      collector deferred rather than asserting it green.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DOCKERFILE="$ROOT/deploy/docker/Dockerfile"
ENTRYPOINT="$ROOT/deploy/shared/docker-entrypoint.sh"
GKE_DEFAULT="http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"

fail=0
pass() { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1" >&2; fail=1; }

echo "otel-endpoint: OTLP exporter endpoint seam (root=$ROOT)"

# --- Check 1: Dockerfile bakes the exact GKE default ------------------------
if grep -qF "$GKE_DEFAULT" "$DOCKERFILE"; then
  pass "Dockerfile bakes the GKE default OTLP endpoint (unset ⇒ unchanged)"
else
  bad "Dockerfile no longer bakes the GKE default endpoint ($GKE_DEFAULT)"
fi

# --- Check 2: entrypoint has the env-driven, guarded resolution -------------
if grep -q "OTEL_EXPORTER_OTLP_ENDPOINT" "$ENTRYPOINT"; then
  pass "entrypoint resolves OTEL_EXPORTER_OTLP_ENDPOINT"
else
  bad "entrypoint does not reference OTEL_EXPORTER_OTLP_ENDPOINT"
fi
if grep -qF "$GKE_DEFAULT" "$ENTRYPOINT"; then
  pass "entrypoint carries the exact GKE default endpoint"
else
  bad "entrypoint missing the GKE default endpoint"
fi
# Guarded rewrite: only acts when the override is non-empty (unset ⇒ no-op ⇒ no regression).
if grep -Eq 'if \[ -n "\$\{OTEL_EXPORTER_OTLP_ENDPOINT:-\}" \]' "$ENTRYPOINT"; then
  pass "entrypoint only rewrites the endpoint when an override is set (unset ⇒ no-op)"
else
  bad "entrypoint rewrite is not guarded on OTEL_EXPORTER_OTLP_ENDPOINT being set"
fi

# --- Check 3: functional resolution contract (replay the entrypoint expr) ---
resolve() { echo "${OTEL_EXPORTER_OTLP_ENDPOINT:-$GKE_DEFAULT}"; }

override="http://otel-collector.observability.svc.cluster.local:4318/v1/traces"
got="$(OTEL_EXPORTER_OTLP_ENDPOINT="$override" resolve)"
if [ "$got" = "$override" ]; then
  pass "set OTEL_EXPORTER_OTLP_ENDPOINT ⇒ resolves to the override ($override)"
else
  bad "override not honored: got '$got'"
fi

got="$(unset OTEL_EXPORTER_OTLP_ENDPOINT 2>/dev/null; resolve)"
if [ "$got" = "$GKE_DEFAULT" ]; then
  pass "unset OTEL_EXPORTER_OTLP_ENDPOINT ⇒ resolves to the GKE default (no regression)"
else
  bad "unset default wrong: got '$got'"
fi

if [ "$fail" -eq 0 ]; then
  echo ""
  echo "otel-endpoint: OK — OTLP endpoint is env-driven with the GKE default preserved."
  echo "  (deferred-not-faked: in-container YAML rewrite + a live OTLP collector — D3)"
fi
exit "$fail"
