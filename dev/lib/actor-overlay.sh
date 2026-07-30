#!/usr/bin/env bash
# actor-overlay.sh — grant the deployed actor identity a READ-ONLY tenant authority, and take it
# back afterwards.
#
# WHY THIS EXISTS
#   P9-T8b-4b-ii-1 put a real caller at a real broker's door and every envelope came back the same
#   way: 403 target-forbidden, raised at step 3, because the phase-9 actor holds the 06 §2.2.1
#   broker-operations grant and no authority over a tenant workload whatsoever. That is the
#   correct shipped posture and it is also a ceiling on what any L2 suite can observe — steps 4
#   through 9 of the pipeline have never executed against a live API server, so V-REV-001's
#   "shadow mode never mutates" has L1 evidence and nothing at L2. Proving a system does not
#   mutate requires first letting it get far enough to try.
#
#   This library lifts that ceiling by exactly one notch: `get`/`list`/`watch` on six workload
#   kinds in one namespace that exists only for the test. The grant itself is
#   `dev/verify/fixtures/actor-tenant-grant.yaml`, whose header carries the reasoning for its
#   labels; read that before changing anything here.
#
# WHAT IT DELIBERATELY DOES NOT DO
#   No write verb, at any level, to anything. The write half of the overlay belongs to T9b, which
#   also has to rule on a design question this fixture surfaced and cannot answer: a write grant
#   wearing `kube-agents/tier` is denied by `vap-agent-readonly` validation 1, and one without it
#   is governed by no admission rule at all until `vap-agent-scope` lands in P10-T1.
#
#   No namespace deletion. `actor_overlay_revoke` removes the Role and the RoleBinding — the
#   authority, which is the point — and leaves the namespace and everything the run wrote into it
#   standing ([[LSN-045]]).
#
#   No actor ServiceAccount of its own. The name is read from the CR's
#   `status.broker.actorServiceAccount`, which 06 §5.1 publishes precisely so that nothing outside
#   the controller has to recompute `<tier>-<scope-leaf>-actor`. A suite that improvises the name
#   still passes on a cluster whose controller resolved a different one.
#
# CALLERS ARE GUARDED TO gke-scratch-*
#   This applies RBAC to a live cluster. Every caller must carry the anchored `case "$CTX" in
#   gke-scratch-*)` guard whose `*)` arm exits non-zero; this file does not relax that and must
#   never be sourced by anything that does.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/actor-overlay.sh"
#   sa="$(actor_overlay_actor_sa "$K" <agent-ns> <agent>)"
#   actor_overlay_apply  "$K" <agent-ns> <agent> <tenant-ns>
#   actor_overlay_revoke "$K" <tenant-ns>

# The rendered fixture. Resolved from this file's own location so a caller in dev/verify/ and a
# caller in dev/tests/ find the same one.
ACTOR_OVERLAY_FIXTURE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../verify/fixtures" && pwd)/actor-tenant-grant.yaml"
# The WRITE half (P9-T9b-5a). Layers on top of the read fixture above; read that file's header for
# the ruling on why it wears neither `kube-agents/tier` nor `kube-agents/role`.
ACTOR_OVERLAY_WRITE_FIXTURE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../verify/fixtures" && pwd)/actor-tenant-write-grant.yaml"
# How long RBAC may take to become visible to the authorizer after the RoleBinding is accepted.
# The API server's RBAC cache is informer-driven, so this is normally sub-second; the wait exists
# because "normally" is not a property, and a suite that races the cache fails as a 403 that looks
# exactly like the 403 it is trying to eliminate.
ACTOR_OVERLAY_PROPAGATION_TIMEOUT="${ACTOR_OVERLAY_PROPAGATION_TIMEOUT:-60}"

# actor_overlay_actor_sa <kubectl-cmd> <agent-namespace> <agent>
#   The actor identity the controller RESOLVED, printed on stdout. Empty output + rc 1 when the CR
#   has not published one, which is a real answer: it means the broker is not reconciled and any
#   grant written now would bind a subject that does not exist yet.
actor_overlay_actor_sa() {
  local K="$1" ns="$2" agent="$3" sa
  sa="$($K -n "$ns" get agent "$agent" -o jsonpath='{.status.broker.actorServiceAccount}' 2>/dev/null)"
  if [ -z "$sa" ]; then
    echo "  overlay: Agent $ns/$agent publishes no .status.broker.actorServiceAccount" >&2
    return 1
  fi
  printf '%s\n' "$sa"
}

# actor_overlay_apply <kubectl-cmd> <agent-namespace> <agent> <tenant-namespace>
#   Render the fixture, apply it, and do not return until the API server's own authorizer agrees
#   the actor can read in the tenant namespace.
#   rc 0 = the authority is live · rc 1 = could not resolve the actor · rc 2 = applied but never
#   became effective within the timeout.
#
#   The readiness question is asked with `auth can-i`, of the authorizer, rather than inferred from
#   `kubectl apply` returning 0. Those are different claims: apply proves the object was accepted,
#   can-i proves the decision changed. No positional word here contains a `/` ([[LSN-044]]).
actor_overlay_apply() {
  local K="$1" ns="$2" agent="$3" tenant_ns="$4"
  local sa subject waited

  sa="$(actor_overlay_actor_sa "$K" "$ns" "$agent")" || return 1
  subject="system:serviceaccount:${ns}:${sa}"

  KAGE_TENANT_NS="$tenant_ns" KAGE_ACTOR_NS="$ns" KAGE_ACTOR_SA="$sa" \
    envsubst '${KAGE_TENANT_NS} ${KAGE_ACTOR_NS} ${KAGE_ACTOR_SA}' <"$ACTOR_OVERLAY_FIXTURE" |
    $K apply -f - >/dev/null || return 1
  echo "  overlay: granted $subject read-only on namespace $tenant_ns"

  waited=0
  while [ "$waited" -lt "$ACTOR_OVERLAY_PROPAGATION_TIMEOUT" ]; do
    if [ "$($K auth can-i list configmaps -n "$tenant_ns" --as="$subject" 2>/dev/null)" = "yes" ]; then
      # The negative half, asserted here rather than left to the suite: the overlay must not have
      # granted a write, and it must not have leaked outside its namespace. Both are cheap, both
      # are properties of THIS function's output, and a fixture that quietly widened would
      # otherwise be discovered as a surprising pass three steps downstream.
      if [ "$($K auth can-i update configmaps -n "$tenant_ns" --as="$subject" 2>/dev/null)" != "no" ]; then
        echo "  overlay: FAILED — $subject can write in $tenant_ns; the overlay is not read-only" >&2
        return 2
      fi
      if [ "$($K auth can-i list configmaps -n kube-system --as="$subject" 2>/dev/null)" != "no" ]; then
        echo "  overlay: FAILED — $subject can read kube-system; the overlay is not namespaced" >&2
        return 2
      fi
      echo "  overlay: authorizer agrees after ${waited}s (read yes · write no · cross-namespace no)"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done

  echo "  overlay: FAILED — $subject still cannot list configmaps in $tenant_ns after ${waited}s" >&2
  return 2
}

# actor_overlay_revoke <kubectl-cmd> <tenant-namespace>
#   Take the authority back. Role and RoleBinding only — never the namespace, and never anything
#   the run wrote into it ([[LSN-045]]). Idempotent; a second call on an already-revoked namespace
#   is rc 0.
actor_overlay_revoke() {
  local K="$1" tenant_ns="$2"
  $K -n "$tenant_ns" delete rolebinding kubeagents-actor-tenant-readonly \
    --ignore-not-found >/dev/null 2>&1
  $K -n "$tenant_ns" delete role kubeagents-actor-tenant-readonly \
    --ignore-not-found >/dev/null 2>&1
  echo "  overlay: revoked the tenant grant in $tenant_ns (namespace left standing)"
  return 0
}

# --- the WRITE half (P9-T9b-5a) -------------------------------------------------------------------
#
# Everything above grants reads and is bounded by the cluster's own admission policy. Everything
# below grants writes and is not — `dev/verify/fixtures/actor-tenant-write-grant.yaml`'s header
# carries the ruling and names the three things that bound it instead. Two of those three are the
# functions here, which is why they assert considerably more than the read half does.

# actor_overlay_can <kubectl-cmd> <subject> <verb> <resource> <want yes|no> <where...>
#   One authorizer question, with the answer required up front. `<where...>` is passed through to
#   kubectl verbatim (`-n <ns>`, or `--subresource=scale`, or both). Prints nothing on the expected
#   answer; prints what it asked and what it got on the unexpected one. rc 0 = as expected.
#
#   The `*/*)` arm is [[LSN-044]] property 1b, and it is load-bearing rather than decorative here:
#   this helper takes its resource as a variable, which is precisely the refactor that makes the
#   static half of that rule (no `/` in an `auth can-i` positional) unenforceable. Half of the
#   questions below are negative, and `auth can-i update deployments/scale` asks whether the subject
#   may update a Deployment NAMED `scale` — a question nobody was ever granted, which answers `no`
#   for a reason that has nothing to do with the policy under test.
actor_overlay_can() {
  local K="$1" subject="$2" verb="$3" resource="$4" want="$5"
  shift 5
  local got
  case "$resource" in
    */*)
      # The message deliberately does not SPELL the bad form. `cluster-check-hygiene.py` scans shell
      # source for a slashed word in a positional slot and cannot tell an invocation from a sentence
      # describing one, so a guard that quoted the shape it rejects would trip the lint it enforces.
      echo "  overlay: REFUSING — '$resource' contains a slash. Positionally, kubectl reads that as a" >&2
      echo "           resource type and an OBJECT NAME, so the question asked is not the one meant." >&2
      echo "           Pass the subresource as --subresource=<name> instead ([[LSN-044]])." >&2
      return 2
      ;;
  esac
  got="$($K auth can-i "$verb" "$resource" --as="$subject" "$@" 2>/dev/null)"
  if [ "$got" != "$want" ]; then
    echo "  overlay: FAILED — can-i $verb $resource $* --as=$subject => '${got:-<no answer>}', want '$want'" >&2
    return 1
  fi
  return 0
}

# actor_overlay_apply_write <kubectl-cmd> <agent-namespace> <agent> <tenant-namespace>
#   Grant the actor the write authority the executor's dry-run apply needs, and do not return until
#   the authorizer agrees the grant is EXACTLY what the fixture says.
#   rc 0 = the authority is live and correctly bounded · rc 1 = could not resolve the actor, or the
#   read half failed · rc 2 = applied but never became effective, or became effective and is WIDER
#   than the fixture claims.
#
#   The read half is applied first and its failure is fatal. That is not convenience: the write
#   fixture deliberately grants no read verb, so an actor holding only the write Role can `patch` a
#   Deployment it cannot `get` — and the executor's step-3 pre-state read would fail in a way that
#   looks exactly like the 403 this whole overlay exists to eliminate.
actor_overlay_apply_write() {
  local K="$1" ns="$2" agent="$3" tenant_ns="$4"
  local sa subject waited rc=0

  actor_overlay_apply "$K" "$ns" "$agent" "$tenant_ns" || return $?

  sa="$(actor_overlay_actor_sa "$K" "$ns" "$agent")" || return 1
  subject="system:serviceaccount:${ns}:${sa}"

  KAGE_TENANT_NS="$tenant_ns" KAGE_ACTOR_NS="$ns" KAGE_ACTOR_SA="$sa" \
    envsubst '${KAGE_TENANT_NS} ${KAGE_ACTOR_NS} ${KAGE_ACTOR_SA}' <"$ACTOR_OVERLAY_WRITE_FIXTURE" |
    $K apply -f - >/dev/null || return 1
  echo "  overlay: granted $subject WRITE on namespace $tenant_ns (configmaps · deployments · scale)"

  waited=0
  while [ "$waited" -lt "$ACTOR_OVERLAY_PROPAGATION_TIMEOUT" ]; do
    if [ "$($K auth can-i patch configmaps -n "$tenant_ns" --as="$subject" 2>/dev/null)" = "yes" ]; then
      break
    fi
    sleep 2
    waited=$((waited + 2))
  done
  if [ "$waited" -ge "$ACTOR_OVERLAY_PROPAGATION_TIMEOUT" ]; then
    echo "  overlay: FAILED — $subject still cannot patch configmaps in $tenant_ns after ${waited}s" >&2
    return 2
  fi

  # THE POSITIVE HALF — every verb a caller is entitled to rely on. A grant that applied cleanly and
  # conferred three of its four verbs would otherwise be discovered as a 403 inside the broker,
  # attributed to the broker.
  actor_overlay_can "$K" "$subject" create configmaps  yes -n "$tenant_ns" || rc=2
  actor_overlay_can "$K" "$subject" delete configmaps  yes -n "$tenant_ns" || rc=2
  actor_overlay_can "$K" "$subject" patch  deployments yes -n "$tenant_ns" || rc=2
  actor_overlay_can "$K" "$subject" update deployments yes -n "$tenant_ns" --subresource=scale || rc=2

  # THE NEGATIVE HALF — the four ways this fixture could widen without anyone editing its rules
  # block, each asked of the authorizer rather than inferred from the YAML.
  #   secrets:          the kind deliberately left out (see the fixture's header)
  #   kube-system:      namespaced containment, the property the Role kind is supposed to give
  #   roles:            V-CTN-037 P5 as a runtime fact — authority over RBAC is authority to widen
  #   nodes:            cluster scope, which no Role can confer and a stray ClusterRoleBinding can
  actor_overlay_can "$K" "$subject" patch  secrets     no -n "$tenant_ns" || rc=2
  actor_overlay_can "$K" "$subject" patch  configmaps  no -n kube-system  || rc=2
  actor_overlay_can "$K" "$subject" create roles       no -n "$tenant_ns" || rc=2
  actor_overlay_can "$K" "$subject" patch  nodes       no                 || rc=2

  if [ "$rc" -ne 0 ]; then
    echo "  overlay: the write grant is not what actor-tenant-write-grant.yaml says it is." >&2
    echo "           Revoking it rather than handing a wider authority to the suite." >&2
    actor_overlay_revoke_write "$K" "$tenant_ns" >/dev/null 2>&1
    return 2
  fi

  echo "  overlay: authorizer agrees after ${waited}s (4 granted verbs yes · secrets, kube-system, RBAC, cluster-scope no)"
  return 0
}

# actor_overlay_revoke_write <kubectl-cmd> <tenant-namespace>
#   Take the write authority back, and PROVE it is gone. Role and RoleBinding only — never the
#   namespace, and never anything the run wrote into it ([[LSN-045]]).
#
#   The read half's revoke asserts nothing and is right not to: a read grant that outlived its suite
#   on a scratch cluster is untidy. A WRITE grant that outlived its suite is a real over-grant
#   sitting on a cluster with a filename that says it is only for tests — the exact outcome
#   V-CTN-037 exists to prevent, one layer down from the file system where V-CTN-037 can see. So
#   this one asks the authorizer whether the deletion took effect, and returns non-zero if it did
#   not. Idempotent: a second call on an already-revoked namespace is rc 0, because `no` is `no`.
#   rc 0 = the authority is gone · rc 2 = it is still there after the timeout.
actor_overlay_revoke_write() {
  local K="$1" tenant_ns="$2" waited=0
  $K -n "$tenant_ns" delete rolebinding kubeagents-actor-tenant-write \
    --ignore-not-found >/dev/null 2>&1
  $K -n "$tenant_ns" delete role kubeagents-actor-tenant-write \
    --ignore-not-found >/dev/null 2>&1

  # The subject is re-derived from the binding we just deleted, so it cannot be asked for directly.
  # It is instead asked of every subject the write Role could have bound: `auth can-i --as` needs a
  # name, so the caller's actor is recovered from the READ binding, which revoke leaves for its own
  # call. When the read half is already gone there is nothing left to prove and rc 0 is honest.
  local subject
  subject="$($K -n "$tenant_ns" get rolebinding kubeagents-actor-tenant-readonly \
    -o jsonpath='{range .subjects[0]}system:serviceaccount:{.namespace}:{.name}{end}' 2>/dev/null)"
  if [ -z "$subject" ]; then
    echo "  overlay: revoked the WRITE grant in $tenant_ns (no read binding left to name a subject against)"
    return 0
  fi

  while [ "$waited" -lt "$ACTOR_OVERLAY_PROPAGATION_TIMEOUT" ]; do
    if [ "$($K auth can-i patch configmaps -n "$tenant_ns" --as="$subject" 2>/dev/null)" = "no" ]; then
      echo "  overlay: revoked the WRITE grant in $tenant_ns — authorizer says no after ${waited}s (namespace left standing)"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done

  echo "  overlay: FAILED — $subject can STILL patch configmaps in $tenant_ns ${waited}s after revoke." >&2
  echo "           A write authority that outlived its suite is on this cluster right now. Look for" >&2
  echo "           a second RoleBinding naming that subject: kubectl -n $tenant_ns get rolebindings" >&2
  return 2
}
