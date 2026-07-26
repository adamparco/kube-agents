#!/usr/bin/env bash
# agent-fixtures.sh — seed the two cluster objects an agent pod needs to START but which
# NOTHING in dev creates.
#
# WHY THIS EXISTS
#   An Agent CR names two objects it does not own:
#     spec.harness.hermes.apiServerSecretRef  -> a Secret holding the gateway's own API key
#     spec.security.serviceAccountName        -> a ServiceAccount
#   The operator creates neither (there is no ServiceAccount constructor in
#   k8s-operator/internal/controller — checked, not assumed). On a real install both arrive from
#   the GitOps tree: examples/gitops-repo/.../50-developer-team-identity.yaml ships the SA, and
#   provision_07_gcp_k8s_secrets.sh writes the Secret. dev applies NEITHER of those paths,
#   so on a scratch cluster the objects simply never exist.
#
#   The failure that follows is misleading in a specific way. A missing SA blocks pod CREATION
#   (the ServiceAccount admission plugin rejects it), so the Deployment reports no pods at all;
#   a missing Secret lets the pod be created and then wedges it in CreateContainerConfigError.
#   Neither says "a fixture is absent" — they read as "the agent is broken", which is the same
#   shape as LSN-021: the environment quietly fails to provide something and the SYMPTOM lands on
#   whatever ran next. This file makes providing it a callable step instead of a thing you were
#   supposed to remember.
#
# WHAT IT DELIBERATELY DOES NOT DO
#   The API key it mints is `openssl rand`, generated locally, never echoed, and only ever written
#   to a scratch cluster. It is NOT a credential for anything — the gateway checks it against itself.
#   No model-provider key, Slack token or GCP credential is created here, and none should be: those
#   live only in the gitignored vars.sh and the real platform-agent-secrets (phase-8-live-checklist).
#   Callers are guarded to gke-scratch-* contexts; this file does not relax that.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/agent-fixtures.sh"
#   seed_agent_fixtures "$K" <namespace> <agent-name>

# seed_agent_fixtures <kubectl-cmd> <namespace> <agent-name>
#   Reads the object NAMES off the CR itself rather than reconstructing them, so a CR that names its
#   Secret something unexpected still gets the right fixture and this never drifts from the schema
#   (V-MET-013 — one definition site, and it is the CR).
#   rc 0 = the fixtures are present (created or already there) · rc 1 = could not read the CR.
seed_agent_fixtures() {
  local K="$1" ns="$2" name="$3"
  local sec_name sec_key sa_name

  if ! $K -n "$ns" get agent "$name" >/dev/null 2>&1; then
    echo "  fixtures: no Agent '$name' in namespace '$ns' — nothing to seed" >&2
    return 1
  fi

  sec_name="$($K -n "$ns" get agent "$name" -o jsonpath='{.spec.harness.hermes.apiServerSecretRef.name}' 2>/dev/null)"
  sec_key="$($K -n "$ns" get agent "$name" -o jsonpath='{.spec.harness.hermes.apiServerSecretRef.key}' 2>/dev/null)"
  sa_name="$($K -n "$ns" get agent "$name" -o jsonpath='{.spec.security.serviceAccountName}' 2>/dev/null)"
  : "${sec_key:=API_SERVER_KEY}"

  if [ -n "$sa_name" ]; then
    if $K -n "$ns" get serviceaccount "$sa_name" >/dev/null 2>&1; then
      echo "  fixtures: ServiceAccount $ns/$sa_name already present"
    else
      $K -n "$ns" create serviceaccount "$sa_name" >/dev/null 2>&1 &&
        echo "  fixtures: created ServiceAccount $ns/$sa_name" ||
        echo "  fixtures: WARNING could not create ServiceAccount $ns/$sa_name" >&2
    fi
  fi

  if [ -n "$sec_name" ]; then
    if $K -n "$ns" get secret "$sec_name" -o jsonpath="{.data.$sec_key}" 2>/dev/null | grep -q .; then
      echo "  fixtures: Secret $ns/$sec_name already carries $sec_key"
    else
      # `create --dry-run | apply` rather than plain create, so an existing Secret that is missing
      # only this key is completed instead of erroring out as AlreadyExists.
      local val
      val="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      $K -n "$ns" create secret generic "$sec_name" \
        --from-literal="$sec_key=$val" --dry-run=client -o yaml 2>/dev/null |
        $K apply -f - >/dev/null 2>&1 &&
        echo "  fixtures: created Secret $ns/$sec_name with a locally generated $sec_key" ||
        echo "  fixtures: WARNING could not create Secret $ns/$sec_name" >&2
      unset val
    fi
  fi

  return 0
}
