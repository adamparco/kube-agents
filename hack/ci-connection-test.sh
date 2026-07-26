#!/usr/bin/env bash
set -euo pipefail

TIMEOUT="30s"

# Name the cluster; do not read it off the ambient context (LSN-018). `kubectl config
# current-context` on a build host may be the live cluster `platform-agent-host`, which sits in the
# same project as the scratch cluster and one `kubectl config use-context` away. This test only
# reads, but a smoke test that cannot say WHICH cluster it smoke-tested is evidence about none of
# them. Same rule as `ctx-guard` in k8s-operator/Makefile: unset falls back to the ambient context
# only when that is an anchored `gke-scratch-*` (LSN-005 — anchored prefix, never `*scratch*`), and
# naming the context explicitly is always allowed.
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
explicit=yes
if [ -z "$KUBE_CONTEXT" ]; then
  explicit=no
  KUBE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
fi
if [ -z "$KUBE_CONTEXT" ]; then
  echo "REFUSING: no KUBE_CONTEXT= given and kubectl has no current-context." >&2
  exit 2
fi
if [ "$explicit" = no ]; then
  case "$KUBE_CONTEXT" in
    gke-scratch-*) : ;;
    *) echo "REFUSING: ambient context '$KUBE_CONTEXT' is not gke-scratch-*, and no" >&2
       echo "  KUBE_CONTEXT= was given. If you mean it, name it:" >&2
       echo "      KUBE_CONTEXT=$KUBE_CONTEXT $0" >&2
       exit 2 ;;
  esac
fi
K="kubectl --context $KUBE_CONTEXT"

echo "=== Verifying GKE Cluster Connectivity: $KUBE_CONTEXT (explicit=$explicit) ==="
$K cluster-info --request-timeout="${TIMEOUT}"

echo "=== Verifying Namespace Access ==="
$K get namespaces --request-timeout="${TIMEOUT}"

echo "=== Connectivity Smoke Test Passed ==="
