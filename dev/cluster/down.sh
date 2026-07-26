#!/usr/bin/env bash
# down.sh — delete the remote inner-loop cluster and its kube context.
#
# THIS IS PROBABLY NOT WHAT YOU WANT between campaigns. `pause.sh` resizes the node pools to zero,
# which drops the bill to the control-plane fee alone and comes back in about two minutes;
# delete-and-recreate costs 5-8 minutes plus a full cert-manager + operator + agent-image install.
# Deleting is for "this cluster is the wrong shape" — most often a dataplane that P4 does not
# recognise, which GKE cannot fix in place.
#
# WHY THIS REFUSES ANY NAME BUT ITS OWN. `gcloud container clusters delete` is the most destructive
# command in this repo, and `platform-agent-host` is a live install with real Slack and Gemini
# wiring, in the same project, one variable away. Every other script here guards on the CONTEXT;
# this one cannot, because it addresses the cluster by name through the GCP API and never uses a
# context at all. So the name itself is the guard, compared with `=` and not a glob (LSN-005).
#
# Exit: 0 = deleted (or already absent) · 2 = refused · 3 = tool missing.
# Usage: dev/cluster/down.sh
#   CLUSTER=kube-agents-dev  PROJECT_ID=<gcloud default>  ZONE=us-east4-a
set -euo pipefail

CLUSTER="${CLUSTER:-kube-agents-dev}"
PROJECT_ID="${PROJECT_ID:-}"
# Not `${PROJECT_ID:-$(gcloud ...)}`: under `set -e` a failing substitution in an assignment
# aborts here, with gcloud's exit code and no message, instead of reaching the check below.
[ -n "$PROJECT_ID" ] || PROJECT_ID="$(gcloud config get core/project 2>/dev/null)" || PROJECT_ID=""
ZONE="${ZONE:-us-east4-a}"
CTX="gke-scratch-$CLUSTER"

command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud is not installed." >&2; exit 3; }
[ -n "$PROJECT_ID" ] || { echo "ERROR: no GCP project set." >&2; exit 3; }

if [ "$CLUSTER" != "kube-agents-dev" ]; then
  echo "REFUSING: this script deletes only 'kube-agents-dev', and CLUSTER='$CLUSTER'." >&2
  echo "  Deleting any other cluster is a deliberate act, so do it deliberately, by hand, with the" >&2
  echo "  name in front of you. It should not be one environment variable away in a dev-loop script." >&2
  exit 2
fi

if ! gcloud container clusters describe "$CLUSTER" \
       --zone "$ZONE" --project "$PROJECT_ID" --format='value(name)' >/dev/null 2>&1; then
  echo "'$CLUSTER' does not exist in $ZONE ($PROJECT_ID) — nothing to delete."
else
  echo "== deleting '$CLUSTER' from $ZONE ($PROJECT_ID) — 3-5 minutes =="
  gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --project "$PROJECT_ID" --quiet
fi

# The context outlives the cluster otherwise, and a stale `gke-scratch-*` entry is worse than no
# entry: it satisfies every anchored guard in dev/ and then fails at the API call, so a suite run
# reports connection errors instead of "there is no cluster".
if kubectl config delete-context "$CTX" >/dev/null 2>&1; then
  echo "   removed kube context '$CTX'"
else
  echo "   kube context '$CTX' was not present"
fi

echo "Bring it back:  dev/cluster/up.sh"
