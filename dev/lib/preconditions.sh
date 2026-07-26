#!/usr/bin/env bash
# preconditions.sh — the executable form of binding.md §Preconditions P1, P3, P4 and P10.
#
# WHY THIS FILE EXISTS. P1 and P3 were prose in binding.md, and prose is what LSN-001 and LSN-002
# have each already survived: LSN-001 recurred THREE times (Phase 3, Phase 6, the first live
# install), LSN-002 once, and both were recorded closed against a precondition ID that nothing
# executes. The scripts that "honour P1" honoured it by printing a reminder:
#
#     echo "  NOTE (P1): rebuild + push + rollout restart before trusting a green run here."
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
# "The build under test" is TWO claims, and P1 makes both: the cluster runs the image this host
# built (digest), and that image was built from the source as it stands now (freshness). The first
# without the second passed green over a six-hour-stale operator on 2026-07-25.
#
# Usage:  . "$(dirname "$0")/../lib/preconditions.sh"
#         p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager

# --- P1, and why the registry is now the only witness ----------------------------------------------
#
# THE HOLE THIS CLOSES, found 2026-07-25 by running P1 for the first time. The digest half answers
# "is the cluster running the image I have" — and it answered yes about an image built six hours and
# twenty-five minutes BEFORE the last commit that touched the operator source. That is not a corner
# case: "ran the gate, forgot to rebuild" is LSN-001's actual recurrence mode, and the check written
# to mechanize LSN-001 could not fail in it. V-MET-014, on the check that exists to stop the thing it
# could not see. So P1 makes TWO claims, and the second one is the one that keeps needing defending.
#
# WHAT CHANGED WHEN THE INNER LOOP LEFT THIS HOST. Both halves used to be answered by `docker image
# inspect` — the config digest for identity, `.Created` for freshness. That worked for exactly one
# reason: `kind load docker-image` side-loads the host's image into the node's containerd, so the
# host's copy and the cluster's copy were the same object by construction. On a remote cluster there
# is no local copy and there cannot be one: the nodes are amd64, this host is arm64, and Cloud Build
# builds on neither. `docker image inspect` would return nothing every time and P1 would answer 3
# forever — the decorative state this file was written to escape, arrived at by a different road.
#
# Both halves therefore move to Artifact Registry, which is the one party that the builder wrote to
# AND the kubelet pulled from:
#
#   identity  — the digest the pod reports is looked up in the registry, by digest.
#   freshness — the TAGS that digest carries are read back, and one of them must name the commit
#               this tree is on. `reload-images.sh` derives the tag from `git rev-parse --short
#               HEAD`, so the tag is not a label someone typed; it is the build's own statement of
#               where it came from, recorded by the builder and read from a third party.
#
# That is more than the old check could see, not less. `.Created` versus the newest source commit
# was an inference about provenance drawn from two clocks; a commit sha is the provenance. The clock
# survives in exactly one place — an UNCOMMITTED edit has no commit to name, so the build tag
# carries `-dirty-<epoch>` and that epoch is compared against the mtime of the dirty files. The
# fresh-clone / branch-switch special case that the mtime arithmetic needed is gone with it.

# Build inputs per image, keyed by the repository's last path segment. Deliberately small and
# deliberately NOT defaulted to the repo root: a docs commit would then mark every image stale, and
# a check that fires on unrelated changes gets ignored on the day it is right. An image with no
# mapping returns 3 (could not verify) and says so, so a new one announces itself instead of
# silently losing the freshness half.
_p1_build_inputs() {
  # Strip the digest, then the tag — and the tag only from the LAST path segment, because a
  # registry host may legitimately carry a port (localhost:5000/kube-agents/...).
  local last="${1%%@*}"
  last="${last##*/}"
  case "${last%%:*}" in
    k8s-operator | kage-router) echo "k8s-operator" ;; # docker build context is k8s-operator/
    *) return 1 ;;
  esac
}

_p1_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# _p1_newest_dirty_epoch <path> -> newest mtime among DIRTY files under <path>; EMPTY if none.
#
# Replaces `_p1_newest_source_epoch`, which took the newest of (last commit touching <path>, mtime of
# anything dirty under it) so that an image's `.Created` could be compared against it. The commit
# half is now answered by the tag — `dev-<HEAD-sha>` states the commit outright, which no comparison
# of two clocks can improve on — so what is left is the part a commit cannot speak for. Empty is a
# meaningful return here and not a failure: it means every build input is committed.
_p1_newest_dirty_epoch() {
  local path="$1" newest="" t f
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    t="$(_p1_mtime "$f")"
    [ -n "$t" ] || continue
    if [ -z "$newest" ] || [ "$t" -gt "$newest" ]; then newest="$t"; fi
  done <<EOF
$(git status --porcelain --untracked-files=all -- "$path" 2>/dev/null | cut -c4-)
EOF
  echo "$newest"
}

# _p1_repo_path <image-reference> -> the registry path with any digest and any tag removed.
# The tag comes off the LAST segment only, because a registry host may legitimately carry a port.
_p1_repo_path() {
  local ref="${1%%@*}"
  case "${ref##*/}" in
    *:*) ref="${ref%:*}" ;;
  esac
  echo "$ref"
}

# _p1_registry_tags <repo-path> <sha256:digest> -> every tag that digest carries, space separated.
#
# The filter key is `version`, not `digest`. `--filter=digest=...` matches no field on this resource:
# gcloud prints "the following filter keys were not present in any resource" to STDERR and exits 0
# with empty stdout, which is byte-for-byte what "that digest is not in this registry" looks like.
# `describe` is not an option either — it returns digest, registry, repository and an SLSA level,
# and no tags at all. The project and location come from the URI, not from the active gcloud config.
_p1_registry_tags() {
  gcloud artifacts docker images list "$1" --include-tags \
    --filter="version=$2" --format='value(tags)' 2>/dev/null |
    tr ',\n' '  ' | tr -s ' ' | sed 's/^ *//; s/ *$//'
}

# Abbreviated shas compared as prefixes, in both directions. `git rev-parse --short` picks a length
# that grows with the object count, so the tag written at build time and the sha computed now can
# legitimately differ in length while naming the same commit — and a length-sensitive comparison
# would report that as "the cluster runs a different commit", which is a false accusation of exactly
# the kind that teaches people to skip this check.
_p1_sha_eq() {
  [ -n "$1" ] && [ -n "$2" ] || return 1
  case "$1" in "$2"*) return 0 ;; esac
  case "$2" in "$1"*) return 0 ;; esac
  return 1
}

# _p1_assert_tag_is_current <image> <tags> -> 0 fresh, 1 stale, 3 undecidable (all print a reason).
_p1_assert_tag_is_current() {
  local image="$1" tags="$2"
  local inputs head_sha t rest sha epoch cand="" cand_sha="" cand_epoch="" dirty

  if ! inputs="$(_p1_build_inputs "$image")"; then
    echo "  P1 freshness UNVERIFIED: no build-input mapping for '$image'. The digest resolved, so the"
    echo "    cluster runs an image from your registry — but nothing here can tell whether that image"
    echo "    predates the source. Add it to _p1_build_inputs in $(basename "${BASH_SOURCE[0]}")."
    return 3
  fi
  head_sha="$(git rev-parse --short HEAD 2>/dev/null)"
  if [ -z "$head_sha" ]; then
    echo "  P1 freshness UNVERIFIED: git cannot name HEAD from $PWD, so there is no commit to compare"
    echo "    the deployed tag against. Not a pass."
    return 3
  fi

  # Pick the commit-carrying tag, preferring one that names HEAD. A digest usually carries several
  # tags -- `buildcache` from the cache write, `dev-<sha>` from reload-images.sh, `src-<sha>` from
  # `make cloud-build-push` -- and only some of them say anything about provenance.
  for t in $tags; do
    case "$t" in
      dev-* | src-*) : ;;
      *) continue ;;
    esac
    rest="${t#*-}"
    sha="${rest%%-*}"
    case "$rest" in *-dirty-*) epoch="${rest##*-dirty-}" ;; *) epoch="" ;; esac
    if [ -z "$cand" ] || _p1_sha_eq "$sha" "$head_sha"; then
      cand="$t" cand_sha="$sha" cand_epoch="$epoch"
    fi
    _p1_sha_eq "$sha" "$head_sha" && break
  done

  if [ -z "$cand" ]; then
    echo "  P1 freshness UNVERIFIED: the running digest carries no commit-carrying tag (has: ${tags:-none})."
    echo "    Only dev-<sha> and src-<sha> record which commit a build came from, so provenance cannot"
    echo "    be read off this image. Not a pass — rebuild through dev/cluster/reload-images.sh."
    return 3
  fi
  if ! _p1_sha_eq "$cand_sha" "$head_sha"; then
    echo "P1 FAILED: the cluster runs a build of a DIFFERENT COMMIT."
    echo "    deployed:  $cand  (commit $cand_sha)"
    echo "    this tree: $head_sha"
    echo "  The digest matches an image in your registry; it is an image of code that is no longer"
    echo "  what you are testing. This is LSN-001 in its actual recurrence mode — the gate was run,"
    echo "  the rebuild was not. Fix, do not skip:"
    echo "      dev/cluster/reload-images.sh operator <kube-context>"
    return 1
  fi

  # The commit matches. What a commit cannot account for is an uncommitted edit to the build inputs,
  # so the dirty half is judged against the epoch the builder stamped into the tag.
  dirty="$(_p1_newest_dirty_epoch "$inputs")"
  if [ -z "$dirty" ]; then
    return 0
  fi
  if [ -z "$cand_epoch" ]; then
    echo "P1 FAILED: the deployed image is a clean build of $cand_sha, and $inputs has UNCOMMITTED"
    echo "  changes that are not in it (newest edit $(date -r "$dirty" 2>/dev/null || echo "$dirty"))."
    echo "  The commit is right and the code under test is not. Rebuild:"
    echo "      dev/cluster/reload-images.sh operator <kube-context>"
    return 1
  fi
  if [ "$cand_epoch" -ge "$dirty" ]; then
    return 0
  fi
  echo "P1 FAILED: the deployed image was built from a dirty tree BEFORE the newest edit under $inputs."
  echo "    image built: $(date -r "$cand_epoch" 2>/dev/null || echo "$cand_epoch")  ($cand)"
  echo "    last edited: $(date -r "$dirty" 2>/dev/null || echo "$dirty")"
  echo "  Rebuild, do not skip:"
  echo "      dev/cluster/reload-images.sh operator <kube-context>"
  return 1
}

# --- P1 ------------------------------------------------------------------------------------------
# p1_assert_build_under_test <kubectl-cmd> <namespace> <label-selector> [container-index]
#
# THE IDENTITY THIS RELIES ON, stated because it is the part that could silently stop being true:
# for an image PULLED from a registry, containerd records `status.containerStatuses[].imageID` as
# `<repo>@sha256:<manifest digest>` -- the same digest Artifact Registry indexes the image under, so
# the pod's own report and the registry's record are directly comparable with no third party in
# between. (The old side-loaded identity, imageID == `docker image inspect --format '{{.Id}}'`, held
# only for `kind load` and is gone with it.) A container that reports no manifest digest falls back
# to the digest PINNED IN THE POD SPEC, and to state 3 if there is no digest anywhere.
p1_assert_build_under_test() {
  local K="$1" ns="$2" sel="$3" idx="${4:-0}"
  local running spec_image pod

  # The NEWEST Running pod, not `.items[0]`. Measured 2026-07-25: immediately after a `rollout
  # status` reported success, items[0] was the previous revision's pod, still Terminating and still
  # listed — so P1 read the image of the build that was being replaced and called the restore a
  # failure. It fails safe in that direction and unsafe in the other one (a lingering pod that
  # happens to be current), and either way an arbitrary pick is not evidence about a named artifact.
  pod="$($K -n "$ns" get pods -l "$sel" --field-selector=status.phase=Running \
    --sort-by=.status.startTime -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)"
  if [ -z "$pod" ]; then
    echo "P1 UNVERIFIABLE: no RUNNING pod matched -l $sel in $ns."
    echo "  Not a pass. A digest that cannot be read is not a digest that matches."
    return 3
  fi

  running="$($K -n "$ns" get pod "$pod" \
    -o jsonpath="{.status.containerStatuses[$idx].imageID}" 2>/dev/null)"
  spec_image="$($K -n "$ns" get pod "$pod" \
    -o jsonpath="{.spec.containers[$idx].image}" 2>/dev/null)"

  if [ -z "$running" ]; then
    echo "P1 UNVERIFIABLE: no running pod matched -l $sel in $ns, or it reports no imageID."
    echo "  Not a pass. A digest that cannot be read is not a digest that matches."
    return 3
  fi
  if [ -z "$spec_image" ]; then
    echo "P1 UNVERIFIABLE: pod matched but its container[$idx] declares no image."
    return 3
  fi

  # The digest, and where it is allowed to come from. The container's own report is authoritative and
  # is used whenever it carries one. The fallback to the SPEC's digest is not a softening: a digest
  # reference is unresolvable to anything else, so a kubelet given one either pulled that exact
  # manifest or failed to start the container -- and this code only runs for a pod that is Running.
  # That is the whole argument for deploying by digest rather than by tag, stated as code.
  local digest="" digest_src="the running container"
  case "$running" in *@sha256:*) digest="sha256:${running##*@sha256:}" ;; esac
  if [ -z "$digest" ]; then
    case "$spec_image" in
      *@sha256:*)
        digest="sha256:${spec_image##*@sha256:}"
        digest_src="the pod spec's digest pin"
        ;;
    esac
  fi
  if [ -z "$digest" ]; then
    echo "P1 UNVERIFIABLE: neither the running container nor the pod spec names a manifest digest"
    echo "  (running='$running' spec='$spec_image'), so there is nothing to look up. Not a pass."
    echo "  Deploy by digest: dev/cluster/reload-images.sh operator <kube-context>"
    return 3
  fi

  local repo
  repo="$(_p1_repo_path "$spec_image")"
  case "$repo" in
    *.pkg.dev/*) : ;;
    *)
      # Not a finding about bits, a finding about provenance: nothing built from this tree is
      # published anywhere but Artifact Registry, so an image from elsewhere is the UPSTREAM build.
      # That is the plainest possible form of LSN-001 -- the suite would be measuring somebody
      # else's binary -- and it is a failure, not a skip.
      echo "P1 FAILED: the cluster is not running an image built from this tree."
      echo "    deployed: $spec_image"
      echo "  This tree publishes only to Artifact Registry (*.pkg.dev). Anything else is the"
      echo "  upstream published image, so every result below would describe code you did not build."
      echo "      dev/cluster/reload-images.sh operator <kube-context>"
      return 1
      ;;
  esac

  if ! command -v gcloud >/dev/null 2>&1; then
    echo "P1 UNVERIFIABLE: no gcloud on this host, so the registry cannot be asked what ${digest:7:12}"
    echo "  is. Not a pass. image=$spec_image"
    return 3
  fi

  local tags
  tags="$(_p1_registry_tags "$repo" "$digest")"
  if [ -z "$tags" ]; then
    # The digest came out of the cluster, so this is not "the image is missing" -- it is "the
    # registry the cluster pulled from will not confirm it to me". Credentials, network, or a
    # repository this account cannot read. All three are could-not-look, none is evidence.
    echo "P1 UNVERIFIABLE: $repo has no record of ${digest:0:19}..., so the registry cannot confirm"
    echo "  what the cluster is running. Check credentials and the repository path. Not a pass."
    return 3
  fi

  # The identity half is satisfied: the running manifest is one the registry holds under this repo.
  # It is only half -- see _p1_assert_tag_is_current above.
  _p1_assert_tag_is_current "$spec_image" "$tags"
  local fresh=$?
  [ "$fresh" -eq 1 ] && return 1
  echo "P1 ok: $repo @ ${digest:7:12} (per $digest_src), tagged '$tags'$(
    [ "$fresh" -eq 0 ] && echo " — built from the current source"
  )"
  [ "$fresh" -eq 3 ] && return 3
  return 0
}

# --- P4 ------------------------------------------------------------------------------------------
# p4_assert_enforcing_dataplane <kubectl-cmd>
#
# Sets $P4_DATAPLANE to the dataplane's name. rc 0 = it is known to ENFORCE NetworkPolicy · rc 3 =
# it is not known to, so any network claim here is deferred. Never rc 1: a cluster whose CNI ignores
# NetworkPolicy has not failed a security property, it cannot host the experiment (the P10 rule,
# applied one layer down), and "egress default-deny does not hold" is a sentence someone acts on.
#
# AN ALLOW-LIST OF KNOWN-ENFORCING DATAPLANES, not a deny-list of known-broken ones. LSN-006: kindnet
# ACCEPTS a NetworkPolicy object, stores it, returns 201, and enforces nothing — so every green from
# a network check on kindnet was a statement about the API server's willingness to persist YAML. The
# deny-list shape (`if kindnet then defer`) gets that case right and gets the NEXT unrecognised
# dataplane wrong in the direction that produces a false green, which is the only direction that
# matters. Anything not named here is deferred, including things that would in fact have enforced;
# a spurious deferral costs a line in the ledger, a spurious pass costs the credibility of the suite.
#
# This lived in tenant-isolation-l2.sh, where it was the only correct copy, while egress-enforcement
# (both the L2 script and the L1 test) hard-required `ds/calico-node` and would have deferred on a
# GKE Dataplane V2 cluster that enforces perfectly. One definition site, per V-MET-013.

# The function's second return value. A shell function has one exit code and P4 spends it on the
# 0/3 verdict, so the NAME of the dataplane travels in a global. tenant-isolation-l2.sh prints it
# and needs it separately from the rc; folding it into the exit code would mean numbering CNIs.
# shellcheck disable=SC2034  # assigned here, read by callers of the library.
P4_DATAPLANE=""

p4_assert_enforcing_dataplane() {
  local K="$1" probe seen
  # GKE's Dataplane V2 ships Cilium under the name `anetd`; upstream Cilium keeps its own. Both are
  # eBPF NetworkPolicy enforcement, and the DaemonSet name is the only thing that differs.
  for probe in calico-node:calico anetd:dataplane-v2 cilium:cilium; do
    if $K -n kube-system get daemonset "${probe%%:*}" >/dev/null 2>&1; then
      P4_DATAPLANE="${probe##*:}"
      return 0
    fi
  done
  seen="$($K -n kube-system get ds -o jsonpath='{range .items[*]}{.metadata.name} {end}' 2>/dev/null)"
  # shellcheck disable=SC2034  # see the declaration above: read by callers, not by this library.
  P4_DATAPLANE="unknown"
  echo "DEFERRED (P4): no dataplane that is KNOWN to enforce NetworkPolicy."
  echo "  looked for: calico-node · anetd (GKE Dataplane V2) · cilium"
  echo "  kube-system has: ${seen:-<nothing readable>}"
  echo "  A NetworkPolicy applies cleanly on a CNI that ignores it entirely (LSN-006), so a pass here"
  echo "  would be evidence about the API server and not about the network. Bring up a cluster with"
  echo "  an enforcing dataplane: dev/cluster/up.sh"
  return 3
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
# This deletes and waits until the object the caller is about to inspect is a DIFFERENT object, so
# the next read is about a fresh admission. It deliberately does not recreate anything itself: the
# caller owns what the replacement looks like, and a helper that guesses would hide the shape under
# test.
#
# WHY THE TEST IS "NEW UID" AND NOT "GONE", measured on kind-kube-agents-dev 2026-07-25. The first
# version asserted the resource had disappeared. For `deploy/developer-team-team-x-gateway` that is
# never true for longer than a few milliseconds: the Agent CR still exists, so the controller
# reconciles the Deployment straight back. The helper spent its full 90-second budget watching the
# name it had just deleted keep existing, then reported a P3 FAILURE about a recreate that had in
# fact happened perfectly — the very next assertion in verify-phase3.sh read the freshly rendered
# pod and passed. A controller-owned object is exactly the kind this precondition is for, so "gone"
# was the wrong question. What LSN-002 actually requires is that the object being judged went
# through admission under the rules in force NOW, and a changed metadata.uid is precisely that,
# whether the replacement came from the caller's next apply or from the controller's reconcile.
p3_force_recreate() {
  local K="$1" ns="$2" res="$3" timeout="${4:-60}"
  local old_uid new_uid i
  old_uid="$($K -n "$ns" get "$res" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
  if [ -z "$old_uid" ]; then
    echo "P3 ok: $res does not exist in $ns; the next apply is a genuine admission."
    return 0
  fi

  # --wait=false, then poll for the identity change ourselves. `--wait=true` blocks on the NAME
  # disappearing, which a controller-owned resource may never do, and burning the timeout before
  # asking the real question is how the first version got the wrong answer.
  $K -n "$ns" delete "$res" --wait=false >/dev/null 2>&1
  for i in $(seq 1 "$timeout"); do
    new_uid="$($K -n "$ns" get "$res" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
    if [ -z "$new_uid" ]; then
      echo "P3 ok: $res deleted after ${i}s; the next apply passes through admission under the"
      echo "  current rules."
      return 0
    fi
    if [ "$new_uid" != "$old_uid" ]; then
      echo "P3 ok: $res was replaced after ${i}s (uid ${old_uid:0:8} -> ${new_uid:0:8}); its owner"
      echo "  recreated it, so what follows is a fresh admission and not the object that was there."
      return 0
    fi
    sleep 1
  done

  echo "P3 FAILED: $res in $ns is still the SAME object after ${timeout}s (uid ${old_uid:0:8})."
  echo "  The delete did not take effect, so asserting an admission property against it would be"
  echo "  testing the rules in force when it was created (LSN-002)."
  return 1
}

# -----------------------------------------------------------------------------------------------
# P3, second half — the pod the CURRENT Deployment owns, by name
# -----------------------------------------------------------------------------------------------
#
# `p3_force_recreate` returns the instant the DEPLOYMENT has a new uid. The old Deployment's pods are
# garbage-collected after that, asynchronously, so at the moment it returns the pre-recreate pod is
# still listed and does not yet carry a deletionTimestamp. A caller that then polls for "a pod
# matching the selector" is handed the OLD one — precisely the object P3 exists to keep it away from
# — and reads its spec until GC removes it out from under the sequence.
#
# Measured on 2026-07-25 in verify-phase3.sh. One run pinned the pod its PREVIOUS run had created,
# read `.spec.serviceAccountName` successfully, and got empty strings for the image and the tier
# label one kubectl call later. Two runs in three failed, always with EMPTY reads and never with
# wrong values (LSN-024's signature). No amount of extra waiting fixes it: the thing being waited for
# was the wrong object, and a deletionTimestamp filter cannot see a pod that is merely about to be
# deleted. verify-phase2.sh carried the identical block and was passing on GC timing alone.
#
# So resolve by OWNERSHIP, not by time or by label: Deployment uid -> ReplicaSets whose ownerReference
# is that uid -> a Pod whose ownerReference is one of those ReplicaSets. No clock, no assumption about
# GC latency, no selector that both generations answer to. Emits one pod NAME on stdout — keep every
# other message off stdout — so the caller's assertions read a single pinned object instead of
# re-listing a moving set once per field.
p3_pod_of_deploy() {
  local K="$1" ns="$2" deploy="$3" timeout="${4:-120}"
  local duid rsuids name i
  for i in $(seq 1 "$timeout"); do
    duid="$($K -n "$ns" get "deploy/$deploy" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
    if [ -n "$duid" ]; then
      rsuids="$($K -n "$ns" get rs \
        -o go-template='{{range .items}}{{$u := .metadata.uid}}{{range .metadata.ownerReferences}}{{$u}} {{.uid}}{{"\n"}}{{end}}{{end}}' \
        2>/dev/null | awk -v d="$duid" '$2 == d {print $1}')"
      if [ -n "$rsuids" ]; then
        name="$($K -n "$ns" get pods \
          -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{$n := .metadata.name}}{{range .metadata.ownerReferences}}{{$n}} {{.uid}}{{"\n"}}{{end}}{{end}}{{end}}' \
          2>/dev/null | awk -v r="$(echo "$rsuids" | tr '\n' ' ')" \
            'BEGIN { n = split(r, a, " "); for (k = 1; k <= n; k++) if (a[k] != "") s[a[k]] = 1 }
             ($2 in s) { print $1; exit }')"
        if [ -n "$name" ]; then
          echo "$name"
          return 0
        fi
      fi
    fi
    sleep 1
  done
  return 1
}

# --- P10 -------------------------------------------------------------------------------------------
#
# The cluster can still DO the things an L2 claim needs done, before any L2 claim is believed.
#
# Written on 2026-07-25, after verify-phase8.sh's first end-to-end run reported that tenant isolation
# did not hold, that the egress default-deny did not hold, and that chaos C2 failed to replace a
# deleted pod. All three were false. `kube-scheduler` and `kube-controller-manager` on the egress Kind
# were both in CrashLoopBackOff — 41 and 37 restarts against a 9h-old cluster — losing their leader
# leases because API-server calls were timing out at 5s under host memory pressure (the Docker VM has
# ~1.9GiB total and was carrying two Kind control planes). With no scheduler, fixture pods stay
# Pending forever and every enforcement claim downstream of them reports the property ABSENT. With no
# controller-manager, new namespaces never get a `default` ServiceAccount and `kubectl run` fails
# with a Forbidden that a suppressed exit status swallows.
#
# That is the LSN-024 shape aimed at infrastructure rather than at timing: the check reported a
# security property missing when the property was fine and the cluster could not run the experiment.
# It is the worst possible direction for this failure to point, because "tenant isolation does not
# hold" is exactly the sentence someone acts on. A suite that cannot tell a dead scheduler from a
# broken NetworkPolicy is not measuring the NetworkPolicy.
#
# Probes the CAPABILITY, not a proxy for it. Reading `.status.phase` of the static pods would have
# caught this particular outage, but it answers a question about pods when the question is whether
# the control plane still converges. Creating a namespace and waiting for the ServiceAccount its
# controller must write is the same claim stated as an experiment, and it costs about a second.
#
# rc 0 healthy · rc 2 could-not-run (never rc 1: an unhealthy cluster is not a failed property, and
# a caller that maps this to FAIL reintroduces the exact confusion it exists to remove).
p10_assert_control_plane_healthy() {
  local K="$1" label="${2:-cluster}" ns="" i sa=""
  # 0. Memoize per target, for the life of the process tree.
  #
  # The L2 scripts invoke each other: verify-phase7 reaches phases 2-6, and phase 5 reaches 2-4 and
  # chaos-suite, all as CHILD PROCESSES. Unmemoized, one chain would create and delete a probe
  # namespace a dozen times to re-answer a question whose answer cannot have changed — and the
  # per-probe cost is not the real objection. A check that is expensive to call gets called
  # defensively, in one place, at the top, which is precisely the "declared once by a caller on
  # everyone else's behalf" shape that L2_SCOPE_FLOOR exists to reject.
  #
  # Memoizing a POSITIVE only. A failure is never cached: the caller is expected to exit on it, and
  # if some future caller chooses to retry instead, it must get a live answer. Exported so children
  # inherit it; keyed by the kubectl invocation, so a script that probes two clusters (verify-phase8
  # probes both) still asks about each one separately.
  #
  # A caller that PIPES this function runs it in a subshell, where the export lands on a copy and is
  # lost — such a caller simply re-probes, which is a second of wasted time and never a wrong answer.
  # Left as-is deliberately: the alternative is a temp file, i.e. real shared state and a cleanup
  # obligation, bought to save a second on a path no L2 script currently takes.
  local _key
  _key="P10_OK_$(printf '%s' "$K" | tr -c 'A-Za-z0-9' '_')"
  if [ "${!_key:-}" = "1" ]; then
    echo "P10 ok: $label — asserted healthy earlier in this run (memoized)"
    return 0
  fi
  # 1. The API server answers at all, within a bound. `kubectl version` alone is served from cache in
  #    some clients, so ask for something the server must actually look up.
  if ! $K get --raw='/readyz' >/dev/null 2>&1; then
    echo "P10-UNHEALTHY: $label — the API server did not answer /readyz. Nothing below this line" >&2
    echo "  could be measured, and an L2 verdict taken now would describe the cluster, not the code." >&2
    return 2
  fi
  # 2. kube-controller-manager converges. The `default` ServiceAccount in a fresh namespace is written
  #    by its ServiceAccount controller and by nothing else, so its appearance is proof of liveness
  #    rather than evidence about it. Cleaned up regardless of outcome.
  #
  # The NAME comes from the server, via generateName, rather than from anything assembled here.
  # Step 2 deletes with --wait=false, so any client-side scheme has to be unique per CALL or a
  # second probe collides with its own still-Terminating namespace and reports "could not create a
  # probe namespace" — an infrastructure complaint standing exactly where the real verdict belongs,
  # and reached precisely on RETRY after a failure, which is the one path memoization leaves live.
  # `$$` alone collides immediately; `$$` plus a shell counter looks right and still collides,
  # because a caller that pipes this function's output (`| tee run.log`, `| head`) runs it in a
  # SUBSHELL, where the increment happens to a copy and is discarded. Rather than keep guessing at
  # client-side uniqueness, ask the API server — which is authoritative, cannot collide, and makes
  # the create itself a second piece of evidence that the server accepts writes.
  ns="$(printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  generateName: p10-health-\n' \
    | $K create -f - -o jsonpath='{.metadata.name}' 2>/dev/null)"
  if [ -z "$ns" ]; then
    echo "P10-UNHEALTHY: $label — could not create a probe namespace, so the API server is not" >&2
    echo "  accepting writes. Every fixture an L2 claim needs would fail the same way." >&2
    return 2
  fi
  for i in $(seq 1 30); do
    sa="$($K -n "$ns" get sa default -o name 2>/dev/null)"
    [ -n "$sa" ] && break
    sleep 1
  done
  $K delete namespace "$ns" --wait=false >/dev/null 2>&1
  if [ -z "$sa" ]; then
    echo "P10-UNHEALTHY: $label — a fresh namespace got no 'default' ServiceAccount within 30s, so" >&2
    echo "  kube-controller-manager is not converging. Pods will fail to create with a Forbidden and" >&2
    echo "  every enforcement claim downstream would report its property ABSENT for the wrong reason." >&2
    return 2
  fi
  # 3. kube-scheduler is up. Self-hosted (Kind, kubeadm) exposes it as a static pod; a managed control
  #    plane (GKE) does not, and absence there is the provider hiding it, not a fault. Say which case
  #    this was — an unobservable component reported as healthy is a green with nothing behind it.
  local sched
  sched="$($K -n kube-system get pods -l component=kube-scheduler \
    -o jsonpath='{range .items[*]}{.metadata.name}={range @.status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' 2>/dev/null)"
  local sched_note=""
  if [ -z "$sched" ]; then
    # Fall through rather than return: step 4 reads `tier=control-plane` pods, which a managed
    # provider also hides, so the loop is a no-op here — but if some provider DOES expose them with
    # a restart history, an early return would have thrown that evidence away to save nothing.
    sched_note=" (managed/unobservable scheduler; not asserted)"
  elif ! printf '%s' "$sched" | grep -q '=True'; then
    echo "P10-UNHEALTHY: $label — kube-scheduler is not Ready ($sched). Fixture pods will sit Pending" >&2
    echo "  until the timeout and every claim that needs a running pod would report ABSENT." >&2
    return 2
  fi
  # 4. And it has been up for a while, not merely up at this instant.
  #
  # Steps 1-3 all passed against the very cluster whose kube-scheduler had restarted 41 times in 9
  # hours, because a CrashLoopBackOff is a cycle: probe it during an up-swing and every liveness
  # question answers yes. An L2 suite runs for half an hour. "Healthy now" is not the property it
  # needs — "healthy for the next thirty minutes" is, and a recent restart is the available evidence
  # against it. Without this the precondition would have certified the cluster that produced three
  # false security failures, which is a check reporting green about the thing it was written for.
  local finished when now flap=""
  now="$(date +%s)"
  while read -r when; do
    [ -n "$when" ] || continue
    # GNU date first, BSD second — this runs on whatever host the developer has. BOTH need to be told
    # the input is UTC: the API server emits RFC3339 with a trailing `Z`, and BSD `date -j -f` parses
    # the fields as LOCAL time and ignores the Z outright. The first version of this omitted `-u` and
    # every delta came out negative on a UTC-4 host — and since every negative is less than the
    # window, it flagged as "just restarted" every restart that had ever happened. A check that
    # always fires is no more evidence than one that never does (V-MET-014); it just fails safe
    # enough to look deliberate.
    finished="$(date -u -d "$when" +%s 2>/dev/null || date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$when" +%s 2>/dev/null)"
    [ -n "$finished" ] || continue
    if [ $((now - finished)) -lt 0 ]; then
      # Future-dated: the parse is wrong, or the cluster's clock is. Either way this cannot be
      # reasoned about, and guessing "recent" would resurrect the bug above.
      echo "P10: $label — cannot compare restart time '$when' (parsed in the future); treating as" >&2
      echo "  unknown rather than recent. Fix the parse before trusting a green from this probe." >&2
      continue
    fi
    if [ $((now - finished)) -lt "${P10_FLAP_WINDOW:-900}" ]; then
      flap="$flap $((now - finished))s-ago"
    fi
  done <<EOT
$($K -n kube-system get pods -l tier=control-plane \
  -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.lastState.terminated.finishedAt}{"\n"}{end}{end}' 2>/dev/null)
EOT
  if [ -n "$flap" ]; then
    echo "P10-UNHEALTHY: $label — a control-plane component restarted in the last" >&2
    echo "  ${P10_FLAP_WINDOW:-900}s (${flap# }). It answers probes now, but a CrashLoopBackOff answers" >&2
    echo "  probes during its up-swing too, and an L2 suite runs for half an hour. Anything it" >&2
    echo "  reported would be a claim about scheduler uptime wearing the costume of a security result." >&2
    return 2
  fi
  echo "P10 ok: $label — API server ready, controller-manager converging, scheduler Ready, no recent restarts${sched_note}"
  export "$_key=1"
  return 0
}
