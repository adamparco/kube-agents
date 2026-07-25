#!/usr/bin/env bash
# preconditions.sh — the executable form of binding.md §Preconditions P1 and P3.
#
# WHY THIS FILE EXISTS. P1 and P3 were prose in binding.md, and prose is what LSN-001 and LSN-002
# have each already survived: LSN-001 recurred THREE times (Phase 3, Phase 6, the first live
# install), LSN-002 once, and both were recorded closed against a precondition ID that nothing
# executes. The scripts that "honour P1" honoured it by printing a reminder:
#
#     echo "  NOTE (P1): rebuild + kind load + rollout restart before trusting a green run here."
#
# A note is addressed to whoever already knows. The failure mode of both lessons is a run that looks
# green, so the only useful form is one that turns the run red.
#
# BOTH FUNCTIONS RETURN THREE STATES, not two. Digest verification is genuinely impossible in some
# environments (an image pulled from a registry this host has never seen), and a check that cannot
# distinguish "verified equal" from "could not look" is how P1 became decorative in the first place.
# Callers MUST map the third state to their DEFERRED exit, never to a pass.
#
#   0 = verified: the running artifact IS the build under test
#   1 = verified DIFFERENT: it is not. This is the finding; it is never a skip.
#   3 = could not verify. Defer, and say what was missing.
#
# Usage:  . "$(dirname "$0")/lib/preconditions.sh"
#         p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager

# --- P1 ------------------------------------------------------------------------------------------
# p1_assert_build_under_test <kubectl-cmd> <namespace> <label-selector> [container-index]
#
# THE IDENTITY THIS RELIES ON, stated because it is the part that could silently stop being true:
# for an image side-loaded with `kind load docker-image`, the pod's
# `status.containerStatuses[].imageID` digest equals `docker image inspect --format '{{.Id}}'` of the
# local image -- containerd records the image CONFIG digest, which is what docker calls .Id. Verified
# on kind-kube-agents-dev: running ee4699b1... == local ee4699b1... For an image PULLED from a
# registry, imageID is instead `repo@sha256:<manifest digest>`, which .Id does not match; that case
# falls back to RepoDigests, and to state 3 if the host has never seen the image at all.
p1_assert_build_under_test() {
  local K="$1" ns="$2" sel="$3" idx="${4:-0}"
  local running spec_image local_id

  running="$($K -n "$ns" get pods -l "$sel" \
    -o jsonpath="{.items[0].status.containerStatuses[$idx].imageID}" 2>/dev/null)"
  spec_image="$($K -n "$ns" get pods -l "$sel" \
    -o jsonpath="{.items[0].spec.containers[$idx].image}" 2>/dev/null)"

  if [ -z "$running" ]; then
    echo "P1 UNVERIFIABLE: no running pod matched -l $sel in $ns, or it reports no imageID."
    echo "  Not a pass. A digest that cannot be read is not a digest that matches."
    return 3
  fi
  if [ -z "$spec_image" ]; then
    echo "P1 UNVERIFIABLE: pod matched but its container[$idx] declares no image."
    return 3
  fi

  local running_digest="${running##*@}"
  running_digest="${running_digest#sha256:}"

  if ! command -v docker >/dev/null 2>&1; then
    echo "P1 UNVERIFIABLE: no docker on this host, so the build under test has no local identity"
    echo "  to compare against. running=${running_digest:0:12} image=$spec_image"
    return 3
  fi

  local_id="$(docker image inspect "$spec_image" --format '{{.Id}}' 2>/dev/null)"
  local_id="${local_id#sha256:}"

  if [ -z "$local_id" ]; then
    echo "P1 UNVERIFIABLE: '$spec_image' is not present in this host's docker images, so there is"
    echo "  no local build to compare the running one to. This is the honest answer for an image"
    echo "  pulled from a registry; it is NOT evidence that the cluster runs the current code."
    echo "  running=${running_digest:0:12}"
    return 3
  fi

  if [ "$running_digest" = "$local_id" ]; then
    echo "P1 ok: running ${running_digest:0:12} == local build of $spec_image"
    return 0
  fi

  # The image exists locally and differs. Before calling it a mismatch, check the manifest digests --
  # a pulled image legitimately reports a different digest kind, and reporting that as "stale bits"
  # would be a false accusation that trains everyone to ignore this check.
  local repo_digests
  repo_digests="$(docker image inspect "$spec_image" --format '{{join .RepoDigests " "}}' 2>/dev/null)"
  case " $repo_digests " in
    *"@sha256:$running_digest"*)
      echo "P1 ok: running ${running_digest:0:12} matches a RepoDigest of $spec_image"
      return 0
      ;;
  esac

  echo "P1 FAILED: the cluster is NOT running the build under test."
  echo "    running:     ${running_digest:0:12}  (from $running)"
  echo "    local build: ${local_id:0:12}  ($spec_image)"
  echo "  Every result below this line describes different code. This is LSN-001, which has already"
  echo "  recurred three times. Fix, do not skip:"
  echo "      make -C k8s-operator docker-build && kind load docker-image $spec_image --name <cluster>"
  echo "      kubectl -n $ns rollout restart deploy && kubectl -n $ns rollout status deploy"
  return 1
}

# --- P3 ------------------------------------------------------------------------------------------
# p3_force_recreate <kubectl-cmd> <namespace> <resource> [timeout-seconds]
#
# Admission policies evaluate ADMISSION. An object that already exists was admitted under whatever
# rules were in force when it was created, and it keeps running when the rules change -- so reading
# a running object tells you about the past. LSN-002: `kubeagents-system` was labelled Pod Security
# `enforce: restricted`, the bundled pods had no securityContext, and everything stayed Ready. The
# gap appeared only when a clean cluster refused to schedule them.
#
# This deletes and waits for the object to be gone, so the caller's next apply goes through
# admission for real. It deliberately does NOT recreate: the caller owns what the replacement looks
# like, and a helper that guesses would hide the shape being tested.
p3_force_recreate() {
  local K="$1" ns="$2" res="$3" timeout="${4:-60}"
  if ! $K -n "$ns" get "$res" >/dev/null 2>&1; then
    echo "P3 ok: $res does not exist in $ns; the next apply is a genuine admission."
    return 0
  fi
  $K -n "$ns" delete "$res" --wait=true --timeout="${timeout}s" >/dev/null 2>&1
  if $K -n "$ns" get "$res" >/dev/null 2>&1; then
    echo "P3 FAILED: $res still exists in $ns after a $timeout s delete."
    echo "  Asserting an admission property against it would be testing the past (LSN-002)."
    return 1
  fi
  echo "P3 ok: $res deleted; the next apply passes through admission under the current rules."
  return 0
}
