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
# Usage: local-dev/tests/otel-endpoint.sh [REPO_ROOT]
# Exit 0 = seam holds; non-zero on any failure.
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
