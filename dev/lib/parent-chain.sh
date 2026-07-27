#!/usr/bin/env bash
# parent-chain.sh — establish the parent Agent a non-platform CR names, as SETUP.
#
# WHY THIS EXISTS
#   06 §1.2 V-6 rejects a child whose `spec.parentRef` names an Agent that does not exist: the
#   authority ceiling is then UNVERIFIABLE, and "could not verify" is a rejection, never a pass
#   (the same rule preconditions P1/P4/P10 encode on the verification side). P8-T9 made that rule
#   real in the webhook, and four L2 suites went red at once — not because the property under test
#   broke, but because every one of them applied a cluster-admin or developer-team CR onto a cluster
#   where the tier above it had never been created. verify-phase2 lost V-K9 and both halves of V-K1,
#   verify-phase3 lost P3-K1/K2/K4, and the failure text they printed was about a NotFound parent
#   while the check name still said "cardinality". That is a check reporting on something other than
#   what it claims, which is worse than a red.
#
#   The fix belongs HERE and not copy-pasted into each caller for the reason V-MET-013 exists: this
#   is the third idiom (after P1 and P3) that every L2 suite needs and that each one was about to
#   grow its own slightly-different copy of.
#
# WHY SEEDING IS NOT LAUNDERING
#   The parent is setup in exactly the sense that `kubectl create namespace` is setup: without it the
#   subject cannot be submitted at all, so its absence fails the check for a reason the check is not
#   about. Three properties keep it honest:
#     1. The parent is read from a SHIPPED manifest, never hand-written here. If the tree's parent
#        stops being admissible, seeding fails loudly and the caller says so.
#     2. It is deleted and re-applied every time (P3): a leftover from an earlier run may predate the
#        rules under test.
#     3. It is never a subject. No caller may assert a property OF the seeded parent — the point of
#        seeding is that the child becomes judgeable, not that the parent passed something.
#
# WHY scaleToZero
#   The shipped manifests pin `ghcr.io/gke-labs/kube-agents/*-agent:v0.1.0`, which is unpublished and
#   answers an anonymous pull with 403. Seeded as-is, every run leaves an ImagePullBackOff pod for the
#   next suite to read as scenery — the exact residue class LSN-026 was written for. `scaleToZero` is
#   INJECTED at apply time rather than edited into the tree, because the tree is an artifact under
#   test elsewhere (dev/verify/gitops-tree-applies-l2.sh) and must stay shipped-as-is.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/parent-chain.sh"
#   if ref="$(seed_parent_agent "$K" examples/gitops-repo/fleet/platform-agent.yaml)"; then
#     seeded+=("$ref"); pass "seeded parent $ref"
#   else
#     bad "could not seed parent: $ref"
#   fi
#   ...
#   unseed_parent_agents "$K" "${seeded[@]:-}"   # from the caller's EXIT trap

# seed_parent_agent <kubectl-cmd> <manifest-path>
#   Applies one Agent manifest with `spec.deployment.scaleToZero: true` injected, after deleting any
#   object of the same name left by an earlier run.
#   rc 0: prints "<namespace>/<name>" — pass it to unseed_parent_agents.
#   rc 1: prints the reason (kubectl's error, or what could not be parsed out of the file).
seed_parent_agent() {
  local kube="$1" f="$2" name ns out
  [ -f "$f" ] || { echo "no such manifest: $f"; return 1; }
  # Read the identity off the manifest instead of taking it as an argument: a caller that names the
  # object separately is a second definition site that drifts the first time the tree is renamed.
  name="$(sed -n 's|^  name: *||p' "$f" | head -1 | tr -d '"'"'"' \r')"
  ns="$(sed -n 's|^  namespace: *||p' "$f" | head -1 | tr -d '"'"'"' \r')"
  [ -n "$name" ] && [ -n "$ns" ] || { echo "could not read metadata.name/.namespace from $f"; return 1; }
  # An Agent with no `  deployment:` block would be seeded WITHOUT scaleToZero and leave a wedged pod
  # behind. Refuse rather than silently regress into the residue this function exists to avoid.
  grep -q '^  deployment:$' "$f" || { echo "$f has no 'spec.deployment' block to inject scaleToZero into"; return 1; }
  # P3: never reuse a leftover — it may predate the rules under test.
  $kube delete agent "$name" -n "$ns" --ignore-not-found >/dev/null 2>&1
  if out="$(awk '{print} /^  deployment:$/{print "    scaleToZero: true"}' "$f" | $kube apply -f - 2>&1)"; then
    echo "$ns/$name"
    return 0
  fi
  echo "$out"
  return 1
}

# unseed_parent_agents <kubectl-cmd> [<namespace>/<name> ...]
#   Removes what seed_parent_agent created. Safe to call with no refs, and safe to call twice.
#   `--wait=false` because the caller is usually an EXIT trap and the Deployment/pod children are
#   ownerReferenced to the CR, so GC completes without anyone watching.
unseed_parent_agents() {
  local kube="$1" ref
  shift
  for ref in "$@"; do
    [ -n "$ref" ] || continue
    $kube delete agent "${ref#*/}" -n "${ref%%/*}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done
}
