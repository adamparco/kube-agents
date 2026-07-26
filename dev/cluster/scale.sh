#!/usr/bin/env bash
# scale.sh — resize every node pool of the inner-loop cluster to N nodes.
#
# The primitive behind `pause.sh` (N=0) and `resume.sh` (N=2). It exists as its own file rather than
# as two copies because the two directions differ only in a number, and the parts that are easy to
# get wrong — enumerating the pools instead of assuming one, refusing any cluster but the dev one,
# waiting for Ready rather than for the API call to return — would then have to be right twice.
#
# WHY RESIZE AND NOT DELETE. A paused cluster costs the GKE control-plane fee and nothing else; the
# nodes are the bill. Coming back is about two minutes, against 5-8 for a create plus a full
# cert-manager + operator + agent-image install. Between campaigns, pause. `down.sh` is for a
# cluster that is the wrong SHAPE, which is a different problem.
#
# WHAT SURVIVES A PAUSE: the API objects. etcd is part of the control plane, so CRDs, the operator
# Deployment (at its digest), the VAP, Agent CRs, namespaces and policies are all exactly as you
# left them. What does NOT survive: running pods, and anything a pod had in emptyDir or a local
# volume. Every pod is re-admitted from scratch on resume, which is closer to a P3 force-recreate
# than to a reboot — a green run after a resume is evidence about a FRESH admission.
#
# Exit: 0 = resized · 1 = usage · 2 = refused · 3 = tool missing · 4 = did not reach the target.
# Usage: dev/cluster/scale.sh <num-nodes-per-pool>
#   CLUSTER=kube-agents-dev  PROJECT_ID=<gcloud default>  ZONE=us-east4-a
set -euo pipefail

N="${1:-}"
CLUSTER="${CLUSTER:-kube-agents-dev}"
PROJECT_ID="${PROJECT_ID:-}"
# Not `${PROJECT_ID:-$(gcloud ...)}`: under `set -e` a failing substitution in an assignment
# aborts here, with gcloud's exit code and no message, instead of reaching the check below.
[ -n "$PROJECT_ID" ] || PROJECT_ID="$(gcloud config get core/project 2>/dev/null)" || PROJECT_ID=""
ZONE="${ZONE:-us-east4-a}"
CTX="gke-scratch-$CLUSTER"

case "$N" in
  ''|*[!0-9]*) echo "usage: $0 <num-nodes-per-pool>   (0 to pause, 2 to resume)" >&2; exit 1 ;;
esac

for tool in gcloud kubectl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed." >&2; exit 3; }
done
[ -n "$PROJECT_ID" ] || { echo "ERROR: no GCP project set." >&2; exit 3; }

# Same guard, same reasoning as down.sh: this addresses the cluster by NAME through the GCP API, so
# the context-prefix guard the rest of dev/ uses does not apply and cannot be borrowed. Scaling
# `platform-agent-host` to zero would take a live install down, which makes an anchored `=` on the
# one name this script is for the only honest form.
if [ "$CLUSTER" != "kube-agents-dev" ]; then
  echo "REFUSING: this script resizes only 'kube-agents-dev', and CLUSTER='$CLUSTER'." >&2
  echo "  Scaling any other cluster — including scaling a live one to zero — is a deliberate act." >&2
  exit 2
fi

if ! gcloud container clusters describe "$CLUSTER" \
       --zone "$ZONE" --project "$PROJECT_ID" --format='value(name)' >/dev/null 2>&1; then
  echo "ERROR: '$CLUSTER' does not exist in $ZONE ($PROJECT_ID). Create it: dev/cluster/up.sh" >&2
  exit 2
fi

# Enumerated, not assumed to be one. up.sh creates `default-pool`, and its footer documents adding a
# gVisor pool; a pause that silently left a second pool running would bill for it forever and a
# resume that silently left it at zero would strand every sandboxed workload with no node to land on.
pools="$(gcloud container node-pools list --cluster "$CLUSTER" \
  --zone "$ZONE" --project "$PROJECT_ID" --format='value(name)')"
[ -n "$pools" ] || { echo "ERROR: '$CLUSTER' reports no node pools." >&2; exit 4; }

for pool in $pools; do
  echo "== resizing pool '$pool' to $N =="
  gcloud container clusters resize "$CLUSTER" --node-pool "$pool" --num-nodes "$N" \
    --zone "$ZONE" --project "$PROJECT_ID" --quiet
done

# Asserted, not assumed, in BOTH directions. `gcloud ... resize` returning 0 means the operation was
# accepted, not that the nodes exist — and a resume that comes back with one node is the silent
# regression this whole file is careful about: V-CMP-004's CLAIM 2 needs a second node and turns
# into a deferral without one, which reads in the ledger exactly like work nobody has done.
if [ "$N" -eq 0 ]; then
  echo "== paused. Control plane still running; nodes: 0 =="
  echo "   API objects (CRDs, operator Deployment, VAP, Agent CRs) are untouched — etcd is part of"
  echo "   the control plane. Running pods are gone and will be re-admitted from scratch."
  echo "Resume:  dev/cluster/resume.sh"
  exit 0
fi

echo "== waiting for $N node(s) per pool to be Ready =="
K="kubectl --context $CTX"
$K wait --for=condition=Ready nodes --all --timeout=420s || true
ready="$($K get nodes --no-headers 2>/dev/null | grep -c ' Ready ' || true)"
if [ "${ready:-0}" -lt 2 ]; then
  echo "ERROR: only ${ready:-0} Ready node(s) after the resize; the L2 suite needs >= 2." >&2
  echo "  Nothing here is safe to run yet — CLAIM 2 would DEFER rather than fail, quietly." >&2
  echo "  Check the operation:  gcloud container operations list --project $PROJECT_ID --limit 5" >&2
  exit 4
fi
echo "   $ready Ready nodes on $CTX"
echo "Pick up code:  dev/cluster/reload-images.sh all $CTX"
