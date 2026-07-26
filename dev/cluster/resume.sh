#!/usr/bin/env bash
# resume.sh — bring the paused inner-loop cluster's nodes back.
#
# Restores every node pool to NUM_NODES (2 by default) and then ASSERTS that at least two nodes are
# actually Ready before reporting success. Two is not a preference: RWO volumes exclude per node, so
# V-CMP-004's CLAIM 2 needs a second one (LSN-015), and a cluster that comes back with one turns
# that claim into a deferral rather than a failure — indistinguishable, in the ledger, from work
# nobody has done.
#
# See scale.sh for what a pause did and did not preserve, and why the guard is on the cluster NAME.
# Exit codes and env vars are scale.sh's.  Usage: dev/cluster/resume.sh
#   NUM_NODES=2
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scale.sh" "${NUM_NODES:-2}"
