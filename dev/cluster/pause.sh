#!/usr/bin/env bash
# pause.sh — stop paying for the inner-loop cluster's nodes without losing it.
#
# The everyday between-campaigns action. Resizes every node pool to zero, which leaves the GKE
# control-plane fee as the whole bill and keeps every API object exactly as you left it, because
# etcd lives in the control plane. `resume.sh` brings the nodes back in about two minutes.
#
# `down.sh` is the other thing, and it is rarely what you want: it deletes the cluster, which costs
# 5-8 minutes plus a full cert-manager + operator + agent-image install to undo.
#
# See scale.sh for what survives, what does not, and why the guard is on the cluster NAME.
# Exit codes and env vars are scale.sh's.  Usage: dev/cluster/pause.sh
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scale.sh" 0
