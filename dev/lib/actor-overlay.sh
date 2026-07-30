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
