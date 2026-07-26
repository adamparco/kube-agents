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
# "The build under test" is TWO claims, and P1 makes both: the cluster runs the image this host
# built (digest), and that image was built from the source as it stands now (freshness). The first
# without the second passed green over a six-hour-stale operator on 2026-07-25.
#
# Usage:  . "$(dirname "$0")/../lib/preconditions.sh"
#         p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager

# --- P1, the freshness half ------------------------------------------------------------------------
# THE HOLE THIS CLOSES, found 2026-07-25 by running P1 for the first time on kind-kube-agents-dev.
# The digest comparison below answers "is the cluster running MY LOCAL IMAGE of this tag" — and it
# answered yes about an image built six hours and twenty-five minutes BEFORE the last commit that
# touched the operator source. That is not a corner case: "ran the gate, forgot to rebuild" is
# LSN-001's actual recurrence mode, and the check written to mechanize LSN-001 could not fail in it.
# V-MET-014, on the very check that exists to stop the thing it could not see.
#
# Freshness is decided against the newest of (a) the commit time of the last commit touching the
# image's build inputs and (b) the mtime of any DIRTY file among them. Commit time rather than mtime
# for tracked files, because a fresh clone or a branch switch rewrites every mtime and would report
# a perfectly current image as stale — a check that cries wolf is the other way to make one
# decorative. Dirty files have no commit time, so mtime is the only answer for them, and it is the
# right one: an uncommitted edit to the controller is exactly the code a local L2 run is about.

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

# _p1_newest_source_epoch <path> -> epoch on stdout; empty if git cannot answer.
_p1_newest_source_epoch() {
  local path="$1" newest t f
  newest="$(git log -1 --format=%ct -- "$path" 2>/dev/null)"
  [ -n "$newest" ] || return 1
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    t="$(_p1_mtime "$f")"
    [ -n "$t" ] && [ "$t" -gt "$newest" ] && newest="$t"
  done <<EOF
$(git status --porcelain --untracked-files=all -- "$path" 2>/dev/null | cut -c4-)
EOF
  echo "$newest"
}

# _p1_assert_image_is_current <image> -> 0 fresh, 1 stale, 3 undecidable (all print their reason).
_p1_assert_image_is_current() {
  local image="$1" inputs created img_epoch src_epoch
  if ! inputs="$(_p1_build_inputs "$image")"; then
    echo "  P1 freshness UNVERIFIED: no build-input mapping for '$image'. The digest matched, so the"
    echo "    cluster runs your local copy — but nothing here can tell whether that copy predates the"
    echo "    source. Add the image to _p1_build_inputs in $(basename "${BASH_SOURCE[0]}")."
    return 3
  fi
  created="$(docker image inspect "$image" --format '{{.Created}}' 2>/dev/null)"
  img_epoch="$(python3 -c 'import sys,re,datetime
s=re.sub(r"\.\d+","",sys.argv[1])
print(int(datetime.datetime.fromisoformat(s).timestamp()))' "$created" 2>/dev/null)"
  src_epoch="$(_p1_newest_source_epoch "$inputs")"
  if [ -z "$img_epoch" ] || [ -z "$src_epoch" ]; then
    echo "  P1 freshness UNVERIFIED: could not read the image's creation time or git's newest change"
    echo "    to $inputs (image='$created' src='$src_epoch'). Not a pass."
    return 3
  fi
  if [ "$img_epoch" -ge "$src_epoch" ]; then
    return 0
  fi
  echo "P1 FAILED: the image is the one the cluster runs, and it is OLDER THAN THE SOURCE."
  echo "    image built:  $(date -r "$img_epoch" 2>/dev/null || echo "$img_epoch")  ($image)"
  echo "    $inputs last changed: $(date -r "$src_epoch" 2>/dev/null || echo "$src_epoch")"
  echo "  The digest matches because you are running your own build; it is a build of code that is"
  echo "  no longer in the tree. This is LSN-001 in its actual recurrence mode — the gate was run,"
  echo "  the rebuild was not. Fix, do not skip:"
  echo "      dev/cluster/reload-images.sh operator <kube-context>"
  return 1
}

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
  local running spec_image local_id pod

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
    # The digest half is satisfied. It is only half: see _p1_assert_image_is_current above.
    _p1_assert_image_is_current "$spec_image"
    local fresh=$?
    [ "$fresh" -eq 1 ] && return 1
    echo "P1 ok: running ${running_digest:0:12} == local build of $spec_image$(
      [ "$fresh" -eq 0 ] && echo ", built from the current source"
    )"
    [ "$fresh" -eq 3 ] && return 3
    return 0
  fi

  # The image exists locally and differs. Before calling it a mismatch, check the manifest digests --
  # a pulled image legitimately reports a different digest kind, and reporting that as "stale bits"
  # would be a false accusation that trains everyone to ignore this check.
  local repo_digests
  repo_digests="$(docker image inspect "$spec_image" --format '{{join .RepoDigests " "}}' 2>/dev/null)"
  case " $repo_digests " in
    *"@sha256:$running_digest"*)
      _p1_assert_image_is_current "$spec_image"
      local fresh=$?
      [ "$fresh" -eq 1 ] && return 1
      echo "P1 ok: running ${running_digest:0:12} matches a RepoDigest of $spec_image"
      [ "$fresh" -eq 3 ] && return 3
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
