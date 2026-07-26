#!/usr/bin/env bash
# seed-agent-fixtures.sh — give every Agent CR on a Kind cluster the Secret and ServiceAccount it
# references but does not own, so its pods can actually start.
#
# WHY THIS EXISTS
#   `up.sh` creates a cluster and `make deploy` installs the operator, but nothing in local-dev ever
#   applies the GitOps identity manifests or runs provision_07 — so the two objects every Agent CR
#   names (see lib/agent-fixtures.sh) are simply absent on Kind. The agent Deployments then sit in
#   CreateContainerConfigError forever, and the message names a missing Secret rather than a missing
#   INSTALL STEP, which is why this went unnoticed until a gate needed a running agent pod.
#
#   This is the step that was missing. It is idempotent, so it is safe to re-run after applying CRs.
#
# WHAT IT MINTS
#   A locally generated random API key per agent, written only into this cluster. No model-provider
#   key, Slack token or cloud credential is created or read here — those belong to the live install
#   path only (docs/build/phase-8-live-checklist.md).
#
# DESTRUCTIVE-TEST GUARD: Kind / scratch-GKE contexts only, anchored. This writes Secrets and
# ServiceAccounts, so the guard is load-bearing — it must never run against the live cluster.
# Exit: 0 = fixtures present · 2 = refused target · 3 = DEFERRED (unreachable / no CRD).
# Usage: local-dev/kind/seed-agent-fixtures.sh [kube-context]
set -uo pipefail

CTX="${1:-kind-kube-agents-dev}"
K="kubectl --context $CTX"

case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2
    exit 2
    ;;
esac

. "$(dirname "$0")/lib/agent-fixtures.sh"

if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable."
  exit 3
fi
if ! $K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the Agent CRD is not installed on '$CTX' — there are no Agent CRs to seed."
  echo "  Stand the stack up first: local-dev/kind/up.sh"
  exit 3
fi

echo "== seeding agent fixtures on $CTX =="
n=0
while read -r ns name; do
  [ -n "${name:-}" ] || continue
  n=$((n + 1))
  echo "- $ns/$name"
  seed_agent_fixtures "$K" "$ns" "$name"
done <<EOF
$($K get agents.kubeagents.x-k8s.io -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' 2>/dev/null)
EOF

echo
if [ "$n" -eq 0 ]; then
  # VACUOUS, not success (V-MET-014). "Seeded 0 agents" and "every agent is seeded" print the same
  # green tick if you let them, and the first one means the caller pointed this at the wrong cluster.
  echo "VACUOUS: no Agent CRs exist on '$CTX', so nothing was seeded. This is NOT a success —"
  echo "  apply the CRs first, then re-run, or you will hit the missing-fixture wall anyway."
  exit 3
fi
echo "OK: $n Agent CR(s) have their Secret and ServiceAccount on '$CTX'."
echo "   Pods already wedged on the absent fixture recover on the kubelet's next retry; to hurry one:"
echo "     $K -n <namespace> rollout restart deploy/<agent-name>-gateway"
