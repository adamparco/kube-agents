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
#   seed_agent_identity "$K" <namespace> <agent-name>
#   run_tier_fixture_pod "$K" <namespace> <pod-name> <image> <tier>

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

# seed_agent_identity <kubectl-cmd> <namespace> <agent-name>
#
#   The BROKER's half of the same problem seed_agent_fixtures solves for the agent. A broker pod
#   runs as the actor ServiceAccount (`buildBrokerDeployment`), and the operator creates neither
#   that SA nor the 06 §2.2.1 grant it needs — 08 §4 forbids the controller from minting RBAC, so
#   on a real install both arrive from `provision_12_deploy_agent_tiers.sh` via
#   `apply_agent_identity`. dev applies no provisioning step, so on a scratch cluster the actor
#   simply does not exist and the ServiceAccount admission plugin refuses to create the broker pod
#   at all: the Deployment reports zero pods and nothing says why.
#
#   RENDERED BY THE SHIPPED RENDERER, NOT BY A COPY OF IT (LSN-024). This calls
#   `render_broker_operations_grant`, `render_actor_grant` and `render_agent_identity` out of
#   `k8s-operator/scripts/common.sh` — the same three functions the install path calls — so the
#   fixture carries the same `kube-agents/tier` + `kube-agents/role` labels the admission policies
#   select on, and the same bindings. A hand-written stand-in here (the shape
#   `brake-fanout-l2.sh` still carries) is a fixture that can pass while the shipped identity is
#   broken, which is scenery.
#
#   WHY THE RENDERERS RUN IN A SUBSHELL. `common.sh` is a provisioning helper, not a library: at
#   load time it installs its own `trap ... EXIT` and parses `$@` for `--dry-run`. Sourced into an
#   L2 suite it would silently replace that suite's cleanup trap with a `tput cnorm`, and read the
#   suite's kube-context argument as a flag. The subshell contains both, and `set --` empties the
#   argument list before the parse sees it.
#
#   WHY THE LEAF IS INVERTED FROM status, NOT RECOMPUTED. `render_agent_identity` wants the scope
#   leaf; `scope.Of(agent).Leaf()` is namespace-else-cluster-else-project and already has two
#   implementations (Go and bash) that V-CMP-007 holds together. A third one here would be a third
#   thing to keep in step, so the actor name is read from `status.broker.actorServiceAccount` —
#   which the controller publishes and which is by definition the name the broker pod will look up
#   (P6: the runtime-authoritative artifact, not a re-derivation of it) — and the leaf is recovered
#   from it. The round trip is then CHECKED: if `<tier>-<leaf>-actor` does not reproduce the
#   published name, the name came from the >253-character truncation arm and this function refuses
#   rather than creating a ServiceAccount nothing binds to.
#
#   rc 0 = the actor identity and the grant are applied · rc 1 = could not read the CR, the status
#   is not published yet, or the apply was refused (the reason is on stderr).
seed_agent_identity() {
  local K="$1" ns="$2" name="$3"
  local tier reader_ksa actor_ksa leaf rendered out
  local common="${AGENT_FIXTURES_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}/k8s-operator/scripts/common.sh"

  [ -f "$common" ] || { echo "  identity: no common.sh at $common" >&2; return 1; }

  tier="$($K -n "$ns" get agent "$name" -o jsonpath='{.spec.tier}' 2>/dev/null)"
  # An absent spec.tier IS platform (agentindex.EffectiveTier) — the same default patch_agent_crs
  # applies, and for the same reason: without it a platform agent written without the field gets an
  # identity rendered for the empty tier.
  : "${tier:=platform}"
  reader_ksa="$($K -n "$ns" get agent "$name" -o jsonpath='{.spec.security.serviceAccountName}' 2>/dev/null)"

  if [ -z "$reader_ksa" ]; then
    echo "  identity: $ns/$name names no spec.security.serviceAccountName" >&2
    return 1
  fi

  # POLLED, never slept on (precondition P9). `status.broker` is controller-written and appears some
  # unknown time after the CR is accepted; a fixed sleep here is a guess about reconcile latency
  # that re-fails on the first slow cluster, and reading it once returns empty on a fast one.
  local waited=0
  while [ -z "$actor_ksa" ]; do
    actor_ksa="$($K -n "$ns" get agent "$name" -o jsonpath='{.status.broker.actorServiceAccount}' 2>/dev/null)"
    [ -n "$actor_ksa" ] && break
    if [ "$waited" -ge "${AGENT_IDENTITY_STATUS_TIMEOUT:-60}" ]; then
      echo "  identity: $ns/$name never published status.broker.actorServiceAccount. The controller" >&2
      echo "    has not reconciled it, and that name is not something to guess at." >&2
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done

  leaf="${actor_ksa#"$tier"-}"
  leaf="${leaf%-actor}"
  if [ "$tier-$leaf-actor" != "$actor_ksa" ]; then
    echo "  identity: cannot recover the scope leaf from '$actor_ksa' (tier '$tier'). That name is" >&2
    echo "    the truncated/hashed form, so rendering from a leaf would create a DIFFERENT SA and" >&2
    echo "    the broker would still fail closed. Refusing." >&2
    return 1
  fi

  if ! rendered="$(
    set --
    cd "$(dirname "$common")" || exit 1
    # shellcheck disable=SC1090
    . "$common" >/dev/null 2>&1 || exit 1
    render_broker_operations_grant || exit 1
    echo '---'
    # The per-tier grant is not optional scenery for an L2 fixture: since P9-T9b-5b-0-ii-b the
    # journal Role — `create actionrecords`, the status update — is rendered HERE and nowhere else,
    # so a fixture that skips it seeds a broker that authenticates its caller and then fails at
    # step 11 with a 403 that looks like a broker bug.
    render_actor_grant "$tier" "$ns" "$leaf" || exit 1
    echo '---'
    render_agent_identity "$tier" "$ns" "$reader_ksa" "$leaf" || exit 1
  )"; then
    echo "  identity: the shipped renderers failed for tier '$tier' leaf '$leaf'" >&2
    return 1
  fi

  # Grant first, then the identity that binds to it — the ordering apply_agent_identity documents:
  # a RoleBinding whose roleRef names a Role that does not exist yet is ACCEPTED and grants nothing,
  # so the failure is a silent authorization denial in the broker rather than an apply error. One
  # `apply` of the concatenated stream preserves that order.
  if ! out="$(printf '%s\n' "$rendered" | $K apply -f - 2>&1)"; then
    echo "  identity: could not apply the actor identity in $ns: $out" >&2
    return 1
  fi
  echo "  fixtures: actor identity '$actor_ksa' + the 06 §2.2.1 grant and the $tier read profile applied in $ns"
  return 0
}

# run_tier_fixture_pod <kubectl-cmd> <namespace> <pod-name> <image> <tier>
#   Creates the long-lived client pod the NetworkPolicy suites probe from. It has to carry
#   `kube-agents/tier=<tier>` — that label is precisely what the per-tier egress policy selects on, so
#   a fixture without it proves nothing about the policy under test.
#
#   Which means the fixture is ALSO in scope for `kube-agents-agent-pod-hardening`, the VAP that Denies
#   any tier-labelled pod whose containers do not set `readOnlyRootFilesystem: true`. So the pod is
#   rendered hardened, the same way the operator renders a real agent container. This is not a
#   concession to the policy: a fixture standing in for an agent pod that could not itself be admitted
#   as an agent pod was never representative of one.
#
#   On a disposable per-campaign cluster this never came up — nothing had applied the hardening VAP
#   yet when the netpol suites ran. On one long-lived cluster the suites share admission state with
#   whatever ran before them, and `verify-phase7.sh` applies that VAP and leaves it applied.
#
#   The refusal is REPORTED, not swallowed. The call sites used to send `kubectl run` to /dev/null and
#   discover the outcome 180s later as "fixture never became Ready" — LSN-021's shape exactly, a
#   command that quietly did nothing and handed its symptom to the next step. An admission denial is
#   an answer; it belongs on stderr the moment it arrives.
#
#   rc 0 = the pod exists · rc 1 = the API server refused it (message on stderr).
run_tier_fixture_pod() {
  local K="$1" ns="$2" name="$3" img="$4" tier="$5" out

  # The suites' cleanup traps delete with --wait=false, so a fast re-run can still find the previous
  # pod Terminating. Settle that here rather than surfacing it as an AlreadyExists "refusal".
  $K -n "$ns" delete pod "$name" --ignore-not-found --timeout=60s >/dev/null 2>&1 || true

  # --override-type=strategic so the containers list merges on its `name` key: a plain JSON merge
  # patch replaces the whole list, which would drop the image and command kubectl just generated.
  if out="$($K -n "$ns" run "$name" --image="$img" --restart=Never \
    --labels="kube-agents/tier=$tier" \
    --override-type=strategic \
    --overrides="{\"spec\":{\"containers\":[{\"name\":\"$name\",\"securityContext\":{\"readOnlyRootFilesystem\":true}}]}}" \
    --command -- sleep 3600 2>&1)"; then
    return 0
  fi

  echo "  fixtures: the API server REFUSED tier fixture $ns/$name — it was never created:" >&2
  printf '    %s\n' "$out" >&2
  return 1
}
