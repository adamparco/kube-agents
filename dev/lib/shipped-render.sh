#!/usr/bin/env bash
# shipped-render.sh — render an install-path manifest through THE SHIPPED RENDERER, and refuse to
# hand back a tier egress policy whose rule 9 names an address no packet on this cluster carries.
#
# WHY THIS EXISTS
#   Three L2 suites now need the per-tier egress allowlist that `provision_13_apply_network_policies.sh`
#   applies, and until this file there were three ways of getting it. `tenant-isolation-l2.sh` has a
#   `render()` wrapper of its own, `startup-ordering-l2.sh` grew a second one on 2026-08-01, and
#   `broker-driver.sh` sidesteps the whole question with a `hostAliases` entry and declares the
#   non-claim in its header. Three call sites, no shared helper — which is [[LSN-068]]'s first
#   proposed mechanization, and this is it.
#
#   The reason a fixture needs the policy at all is worth restating, because the failure has no
#   error message. The operator's `<agent>-to-broker` policy (`pair_netpol.go`) is Egress-ONLY and
#   selects the reader pod, and in Kubernetes ANY egress policy makes the selected pod default-deny
#   for every OTHER egress, DNS included. The operator is right to render only the hop it owns; rule
#   1 of the TIER allowlist (`kube-system:53`) owns the rest, and in a namespace where `provision_13`
#   has never run, nothing owns it. The agent's init container then cannot resolve the broker's name,
#   `wait-for-broker` burns its full 120s, and the suite reads a healthy fleet as "the broker is not
#   ready" ([[LSN-068]]).
#
# THE RENDER IS THE SHIPPED ONE, NEVER A COPY ([[LSN-024]])
#   `common.sh:render_egress_policy` is the same function `provision_13` calls, over the same
#   `netpol-agent-egress.yaml.template`. A hand-written stand-in is a fixture that can pass while
#   the shipped policy is broken, which is scenery.
#
# WHY THE SOURCE IS IN A SUBSHELL WITH THE TRAP CLEARED, AND WHY THAT IS NOT STYLE
#   `common.sh` is a provisioning helper, not a library. At load it installs its own
#   `trap cleanup EXIT`, and that cleanup writes `tput cnorm` to STDOUT — the same stream the
#   manifest is captured from. Sourced into a suite it also replaces that suite's namespace teardown,
#   so the fixture leaks on every run. The subshell contains the trap; `trap - EXIT` immediately
#   after the source removes it before the subshell can ever fire it. Note while reading this that
#   `tenant-isolation-l2.sh`'s own `render()` does NOT clear the trap — latent today, one blank
#   `tput` sequence away from a manifest the API server rejects with a message about YAML.
#   `common.sh` also parses `$@` for `--dry-run`/`-y` at load, so the arguments are passed as
#   environment and `set --` empties the list before the parse sees it.
#
# WHY THE CONTEXT IS A REQUIRED ARGUMENT AND NEVER DEFAULTED
#   `common.sh` calls a BARE `kubectl`, which answers for the AMBIENT context. During the unit that
#   found this the ambient context on the developer's machine was a THIRD cluster
#   (`k8s-lookout-test`), and `resolve_apiserver_cidrs` cheerfully returned that cluster's
#   control-plane address for a policy about to be applied somewhere else. A shell function shadows
#   the binary for the whole subshell, so the shipped resolver stays the single definition site AND
#   asks the cluster under test. An empty context is REFUSED, not defaulted: a policy rendered for
#   the wrong cluster applies cleanly and denies everything ([[LSN-018]]).
#
# THE RULE-9 ASSERTION, AND WHY IT IS NOT "EVERY ADDRESS MUST MATCH" ([[LSN-069]])
#   Seeding the shipped allowlist took the broker DOWN instead of fixing it, with no logs and both
#   probes reporting `connection refused` — `startSources()` reads the brake before the listener
#   opens, so a broker that cannot reach the API server never binds :8443 at all. Rule 9 WAS
#   rendered. It named two addresses no packet carries:
#
#     | source                                     | scratch        | live           |
#     | ------------------------------------------ | -------------- | -------------- |
#     | `svc/kubernetes` ClusterIP (what pods dial) | 34.118.224.1   | 34.118.224.1   |
#     | kubeconfig `server:` (public endpoint)      | 35.221.35.254  | 34.145.154.119 |
#     | `endpoints/kubernetes` (what the PACKET has)| 10.150.0.9     | 10.150.0.2     |
#
#   GKE Dataplane V2 DNATs the ClusterIP in eBPF BEFORE egress policy is scored, so the address the
#   policy sees is the control plane's node-network address — a third form, on neither list.
#   `resolve_apiserver_cidrs` now emits all three, endpoint-first, and KEEPS all three deliberately:
#   the script cannot know where a given dataplane scores egress, and three /32s on 443 is a narrow
#   price for not needing to be right about it.
#
#   THAT IS EXACTLY WHY THE PREDICATE IS AN INTERSECTION AND NOT A SUBSET. "Every rendered address
#   appears in `endpoints/kubernetes`" is the predicate that suggests itself, and it is WRONG: it
#   fails on a CORRECT render, because the ClusterIP and the kubeconfig host are supposed to be
#   there and are supposed not to be endpoint addresses. The property that actually failed on
#   2026-08-01 is that NONE of the rendered addresses was an endpoint address. So the assertion is
#   sized to that and no tighter: AT LEAST ONE rendered address must be an endpoint address. Every
#   rendered address still gets its own verdict line, because a single aggregate `OK` is not
#   evidence ([[LSN-035]]/[[LSN-038]]) — and zero rendered addresses FAILS rather than passing
#   vacuously.
#
#   Every caller gets this for free. `shipped_render_tier_egress` runs it before it will emit the
#   manifest, so a fixture cannot forget to ask; the alternative — one arm in one suite — is the
#   shape [[LSN-068]] is about.
#
# WHAT THIS FILE TOUCHES (blast radius, credentials)
#   IT APPLIES NOTHING AND WRITES NOTHING. Every cluster call here is a `get`: the `kubernetes`
#   Service's endpoints in `default`, through `resolve_apiserver_cidrs` and through the assertion.
#   It mints no credential, reads no Secret and creates no object; the `kubectl apply` stays at the
#   call site, where the caller's own guard can see it. Callers are guarded to `gke-scratch-*` and
#   this file does not relax that — `shipped_render_require_scratch_ctx` is provided so a caller
#   whose next line is an apply has one definition site for the refusal, and it EXITS rather than
#   returning, because a guard a caller can skip by not checking `$?` is a comment.
#
#   The read half is deliberately NOT guarded to scratch. `endpoints/kubernetes` on the live install
#   is precisely where [[LSN-069]] says this arm "would have gone red on both clusters", and a guard
#   here would forbid the one verification the lesson asks for.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/shipped-render.sh"
#   shipped_render_tier_egress "$CTX" platform "$NS" | $K apply -f -   # manifest on stdout
#   shipped_render_require_scratch_ctx "$CTX"                          # before your own apply
#   printf '%s\n' "$manifest" | shipped_render_rule9_cidrs             # offline, no cluster
#   printf '%s\n' "$manifest" | shipped_render_score_manifest 10.150.0.9   # offline predicate
#
# Offline self-test:
#   bash dev/lib/shipped-render.sh --negative-control
#
# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060 — the control synthesises manifests, so these
# statements never run in it and its green says nothing about them)
#   - the `. ./common.sh` source, the `trap - EXIT` that follows it, and the `set --` before it
#   - `resolve_apiserver_cidrs` and all three of its cluster reads, including the endpoint-first
#     ordering that is the whole subject of LSN-069
#   - the `kubectl()` shadow that carries the caller's context into the shipped resolver
#   - `envsubst`, the template on disk, and `render_egress_policy`'s optional-block composition
#   - `shipped_render_apiserver_endpoint_addresses`: the live endpointslices read AND its
#     `endpoints` fallback, which is the arm that only runs on a cluster old enough to need it
#   - the apply, which is not in this file at all
#   - `shipped_render_require_scratch_ctx`'s `exit 2` arm, which would end the control's own shell

# The repository root, so the shipped scripts directory can be found. Two steps rather than one
# substitution: under a caller running `set -euo pipefail` a bare failing `X=$(...)` kills the
# sourcing script before it can say why (the reason substrate-capacity.sh writes every probe this
# way). `: "${X:=...}"` is a simple command whose status is `:`, so a failed `cd` here degrades to
# an empty root and the named refusal below, not to a dead caller.
: "${SHIPPED_RENDER_REPO_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}"

# provision_13's settings for a Workload-Identity GKE cluster, which is what every target in
# `binding.md` §Targets is. `auto` emits BOTH dataplane metadata pairings, exactly as the install
# path does. Narrowing either here would make the fixture's policy something no install produces,
# which is the failure mode this whole file exists to remove.
: "${SHIPPED_RENDER_WORKLOAD_IDENTITY:=true}"
: "${SHIPPED_RENDER_GKE_DATAPLANE:=auto}"

# -------------------------------------------------------------------------------------------------
# The guard. Anchored, never a substring ([[LSN-005]]).
# -------------------------------------------------------------------------------------------------

# shipped_render_require_scratch_ctx <context>
#   Refuses anything that is not an anchored `gke-scratch-*`. `*scratch*)` accepts
#   `my-gke-scratch-of-prod`, and the live install `platform-agent-host` is one `*` away.
#
#   IT EXITS, IT DOES NOT RETURN, and it ends the SOURCING shell when it does. That is deliberate:
#   the caller's next line is a write to a cluster, and a guard whose refusal can be ignored by
#   omitting `|| exit` is prose. Exit 2 is the corpus-wide "refused target" code, so a suite's EXIT
#   trap still runs its teardown.
#
#   NOTHING IN THIS FILE CALLS IT, because nothing in this file writes to a cluster. If a function
#   is ever added here that applies, deletes or patches anything, it calls this FIRST or the file is
#   wrong.
shipped_render_require_scratch_ctx() {
  local CTX="${1:-}"
  case "$CTX" in
    gke-scratch-*) : ;;
    *)
      echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
      echo "  The caller of shipped_render_require_scratch_ctx is about to WRITE to this cluster." >&2
      echo "  The live install 'platform-agent-host' is verification-only (binding.md §Targets)." >&2
      echo "  Name the dev cluster explicitly: gke-scratch-kube-agents-dev" >&2
      exit 2
      ;;
  esac
}

# -------------------------------------------------------------------------------------------------
# The pure half — offline, no cluster, manifest on stdin
# -------------------------------------------------------------------------------------------------

# shipped_render_unsubstituted_tokens
#   Manifest on stdin. Writes every offending line to stdout; rc 1 if there were any, 0 if clean.
#
#   provision_13 makes this refusal for a reason worth copying: an unsubstituted token buried in a
#   `cidr:` field is rejected by the API server with a message about the FIELD, not about the render
#   (V-CMP-003) — and a half-rendered policy that IS accepted applies cleanly and denies everything.
#
#   The shapes are the ones the templates actually carry. `netpol-agent-egress.yaml.template` is
#   `envsubst`-shaped throughout (`${NETPOL_NAME}`, `${AGENT_NAMESPACE}`, `${AGENT_TIER}`,
#   `${CONTROL_NAMESPACE}`, `${EGRESS_OPTIONAL_BLOCKS}`) and a correct render contains no `$` at
#   all; `__UPPER__`, `REPLACE_WITH_` and `PLACEHOLDER` are the three other spellings in use in this
#   tree's manifest corpus and cost nothing to keep.
shipped_render_unsubstituted_tokens() {
  local hits
  hits="$(grep -n -E '\$\{[A-Za-z_][A-Za-z0-9_]*\}|__[A-Z0-9_]+__|REPLACE_WITH_|PLACEHOLDER' || true)"
  [ -n "$hits" ] || return 0
  printf '%s\n' "$hits"
  return 1
}

# shipped_render_rule9_cidrs
#   Manifest on stdin, one rule-9 CIDR per line on stdout. Nothing else, ever — the caller pipes it.
#
#   READ OUT OF THE ARTIFACT, NOT RE-DERIVED. Calling `resolve_apiserver_cidrs` a second time would
#   score the resolver against itself and would have been green all through 2026-08-01. The whole
#   point is to check the bytes that are about to be applied.
#
#   Scoped to rule 9 and not to the document. Rules 3 and 4 are full of `cidr:` lines (the Google
#   restricted VIP, GitHub's four published blocks); a whole-file scan that happened to find an
#   endpoint address in one of those would accept exactly the render LSN-069 was about. The block
#   starts at `render_apiserver_block`'s `# 9)` marker and ends at its `ports:` line, which is the
#   end of that rule's `to:` list wherever the optional blocks put it. Comment lines inside the
#   block are skipped so a future comment mentioning `cidr:` cannot become an address.
shipped_render_rule9_cidrs() {
  awk '
    /^[[:space:]]*#[[:space:]]*9\)/ { in9 = 1; next }
    in9 && /^[[:space:]]*ports:/   { in9 = 0; next }
    in9 && /^[[:space:]]*#/        { next }
    in9 && /cidr:[[:space:]]*[^[:space:]]/ {
      v = $0
      sub(/^.*cidr:[[:space:]]*/, "", v)
      sub(/[[:space:]].*$/, "", v)
      if (v != "") print v
    }
  '
}

# shipped_render_score_manifest <endpoint-address>...
#   THE PREDICATE, and it is pure: manifest on stdin, the live endpoint addresses as arguments,
#   verdicts on stdout, no cluster call anywhere. That split is what lets the negative control
#   exercise the parse and the predicate offline while the cluster half stays one function away.
#
#   rc 0 = at least one rendered rule-9 address is an endpoint address
#   rc 1 = FAILED — no rule 9, no addresses in it, an unsubstituted token, an empty manifest, or
#          the LSN-069 defect itself: addresses were rendered and not one of them is an endpoint
#   rc 3 = COULD NOT BE SCORED, with the blocker named. Never rc 0: "the instrument did not run"
#          and "the property holds" must not look alike ([[LSN-038]]).
#
#   Verdict tokens, one per line, so a caller or a grep can name WHICH rule fired rather than
#   reading a single aggregate OK ([[LSN-035]]):
#     R9-ADDR-MATCH / R9-ADDR-NO-MATCH / R9-ADDR-UNSCORED   per rendered address
#     R9-MATCH · R9-NO-ENDPOINT-MATCH · R9-ABSENT · TOKEN-UNSUBSTITUTED · MANIFEST-EMPTY ·
#     R9-UNSCORED-RANGE · R9-UNSCOREABLE-NO-ENDPOINTS       the aggregate
shipped_render_score_manifest() {
  local manifest haystack=" $* " tokens cidrs c bare prefix
  local n_match=0 n_nomatch=0 n_unscored=0 n_total=0

  manifest="$(cat)"

  if [ -z "$(printf '%s' "$manifest" | tr -d '[:space:]')" ]; then
    echo "  shipped-render: MANIFEST-EMPTY — nothing was rendered. An empty manifest applies"
    echo "    cleanly, changes nothing, and reads exactly like a policy that is in place."
    return 1
  fi

  if ! tokens="$(printf '%s\n' "$manifest" | shipped_render_unsubstituted_tokens)"; then
    echo "  shipped-render: TOKEN-UNSUBSTITUTED — the render left a template token behind:"
    printf '    %s\n' "$tokens"
    echo "    A half-rendered policy applies cleanly and denies everything, and the API server's"
    echo "    complaint (when it complains at all) is about the field, not about the render."
    return 1
  fi

  # Checked before the addresses are scored, because with no endpoint list every address scores
  # NO-MATCH and the run would report the LSN-069 defect when what actually happened is that the
  # cluster read came back empty. Two different findings; only one of them is about the manifest.
  if [ "$#" -eq 0 ]; then
    echo "  shipped-render: R9-UNSCOREABLE-NO-ENDPOINTS — no endpoint addresses were supplied, so"
    echo "    'rule 9 names an address the packet carries' could not be evaluated at all. Blocker:"
    echo "    the 'kubernetes' Service's endpoints in namespace 'default' could not be read."
    return 3
  fi

  cidrs="$(printf '%s\n' "$manifest" | shipped_render_rule9_cidrs)"
  if [ -z "$(printf '%s' "$cidrs" | tr -d '[:space:]')" ]; then
    echo "  shipped-render: R9-ABSENT — the manifest carries no kube-apiserver rule, or carries one"
    echo "    with no addresses in it. The tier selector is 'kube-agents/tier', which the BROKER pod"
    echo "    carries too, so this policy closes TokenReview (step 1), the FleetFreeze read (step 5)"
    echo "    and the ActionRecord write (step 11). Zero addresses is a FAILURE, not a vacuous pass."
    return 1
  fi

  for c in $cidrs; do
    n_total=$((n_total + 1))
    case "$c" in
      */*)
        bare="${c%/*}"
        prefix="${c##*/}"
        ;;
      *)
        bare="$c"
        prefix=32
        ;;
    esac

    if [ "$prefix" != "32" ]; then
      # Honest about what this cannot do: deciding whether a /28 contains an endpoint address is
      # containment arithmetic, and guessing it in shell in the permissive direction is how a
      # policy gets "verified" against a range that does not cover the control plane. Reported,
      # counted separately, and never silently scored either way.
      n_unscored=$((n_unscored + 1))
      echo "  rule 9 address $c — R9-ADDR-UNSCORED: a prefix shorter than /32. This predicate"
      echo "    compares addresses, not ranges; check by hand that it covers an endpoint address."
      continue
    fi

    case "$haystack" in
      *" $bare "*)
        n_match=$((n_match + 1))
        echo "  rule 9 address $c — R9-ADDR-MATCH: named by endpoints/kubernetes, so this is the"
        echo "    address the packet actually carries past a Dataplane-V2 DNAT."
        ;;
      *)
        n_nomatch=$((n_nomatch + 1))
        echo "  rule 9 address $c — R9-ADDR-NO-MATCH: not an endpoint address. Expected for the two"
        echo "    the resolver keeps on purpose (the Service ClusterIP and the kubeconfig host);"
        echo "    a failure only if NO rendered address matches."
        ;;
    esac
  done

  if [ "$n_match" -gt 0 ]; then
    echo "  shipped-render: R9-MATCH — $n_match of $n_total rendered rule-9 address(es) appear in"
    echo "    endpoints/kubernetes ($n_nomatch do not, $n_unscored unscored, and that is correct:"
    echo "    resolve_apiserver_cidrs emits all three forms deliberately)."
    return 0
  fi

  if [ "$n_unscored" -gt 0 ]; then
    echo "  shipped-render: R9-UNSCORED-RANGE — $n_unscored of $n_total rule-9 entries are ranges"
    echo "    rather than /32s and none of the /32s matched, so the property could not be decided."
    echo "    Blocker: address-vs-range containment is not evaluated here."
    return 3
  fi

  echo "  shipped-render: R9-NO-ENDPOINT-MATCH — all $n_total rendered rule-9 addresses are absent"
  echo "    from endpoints/kubernetes. This is [[LSN-069]] exactly: the policy pins addresses no"
  echo "    packet carries, the broker blocks in startSources() before it binds :8443, 'kubectl"
  echo "    logs' is EMPTY and both probes report 'connection refused'. Nothing says 'network'."
  return 1
}

# -------------------------------------------------------------------------------------------------
# The cluster half — read-only
# -------------------------------------------------------------------------------------------------

# shipped_render_apiserver_endpoint_addresses <context>
#   The live endpoint addresses of the `kubernetes` Service, one per line on stdout. rc 1 if the
#   context is missing or nothing could be read.
#
#   EndpointSlice first, `endpoints` as the fallback, and stderr goes nowhere on both: v1 Endpoints
#   is deprecated from 1.33 and prints a warning on EVERY read, which is noise in a suite log and —
#   on a cluster that has dropped the compatibility shim — no answer at all. The same ordering
#   `resolve_apiserver_cidrs` uses, for the same reason.
shipped_render_apiserver_endpoint_addresses() {
  local ctx="${1:-}" addrs

  if [ -z "$ctx" ]; then
    echo "  shipped-render: REFUSED — an explicit kubectl context is required as \$1." >&2
    return 1
  fi

  addrs="$(kubectl --context "$ctx" get endpointslices -n default \
    -l kubernetes.io/service-name=kubernetes \
    -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null || true)"
  if [ -z "$(printf '%s' "$addrs" | tr -d '[:space:]')" ]; then
    addrs="$(kubectl --context "$ctx" get endpoints kubernetes -n default \
      -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  fi

  [ -n "$(printf '%s' "$addrs" | tr -d '[:space:]')" ] || return 1
  # jsonpath returns a space-separated list; the split IS the reformat to one address per line.
  # shellcheck disable=SC2086
  printf '%s\n' $addrs
}

# shipped_render_assert_rule9_matches_endpoints <context>
#   Manifest on stdin. The cluster wrapper around the predicate: read the endpoints, hand them to
#   `shipped_render_score_manifest`, propagate its rc. Verdict lines on stdout.
#   rc 0 = at least one rendered address is an endpoint address · rc 1 = failed · rc 3 = unscoreable
shipped_render_assert_rule9_matches_endpoints() {
  local ctx="${1:-}" manifest eps

  manifest="$(cat)"

  if [ -z "$ctx" ]; then
    echo "  shipped-render: REFUSED — shipped_render_assert_rule9_matches_endpoints needs an"
    echo "    explicit kubectl context as \$1. The ambient context on a developer machine belongs"
    echo "    to a third cluster, which is how a rule 9 gets scored against the wrong control plane."
    return 3
  fi

  # Deliberately not `|| return`: an empty list is passed through to the predicate, which reports
  # R9-UNSCOREABLE-NO-ENDPOINTS with the blocker named rather than letting an unreadable cluster
  # present as the LSN-069 defect.
  eps="$(shipped_render_apiserver_endpoint_addresses "$ctx" 2>/dev/null || true)"

  # Word splitting is the argument list here — every element is an IPv4 literal the API server
  # produced, and quoting it would hand the predicate one argument containing spaces.
  # shellcheck disable=SC2086
  printf '%s\n' "$manifest" | shipped_render_score_manifest $eps
}

# -------------------------------------------------------------------------------------------------
# The one call every fixture should be making
# -------------------------------------------------------------------------------------------------

# shipped_render_tier_egress <context> <tier> <namespace> [netpol-name]
#   The rendered per-tier egress NetworkPolicy on stdout AND NOTHING ELSE — every diagnostic,
#   including the per-address rule-9 verdicts, goes to stderr, because the caller pipes stdout
#   straight into `kubectl apply -f -`.
#
#   rc 0 = a manifest was written to stdout and its rule 9 names an address this cluster's packets
#          carry
#   rc 1 = FAILED. Nothing on stdout: the resolver refused, the render left a token behind, or rule
#          9 does not match. A caller must not apply a policy this refused — that is provision_13's
#          own refusal, made here for the same reason.
#   rc 2 = bad arguments (no context, tier or namespace)
#   rc 3 = the rule-9 property could not be scored, blocker on stderr. An L2 suite should report
#          DEFERRED, never pass ([[LSN-038]]).
#
#   THE PLURAL/SINGULAR TWO-STEP IS `provision_13`'s, VERBATIM IN EFFECT. `resolve_apiserver_cidrs`
#   writes the list; `render_apiserver_block` reads it out of the PLURAL name `KUBE_APISERVER_CIDRS`
#   and returns silently when it is empty. Assigning without exporting, or exporting the singular
#   `KUBE_APISERVER_CIDR` override name by mistake, renders a policy with no rule 9 and no complaint
#   — which is what a scratch namespace got all morning on 2026-08-01.
shipped_render_tier_egress() {
  local ctx="${1:-}" tier="${2:-}" ns="${3:-}" name="${4:-}"
  local scripts rendered rc

  if [ -z "$ctx" ]; then
    echo "  shipped-render: REFUSED — shipped_render_tier_egress needs an explicit kubectl context" >&2
    echo "    as \$1. common.sh calls a BARE kubectl, so an unset context resolves the API-server" >&2
    echo "    address of whatever cluster this machine happens to be pointed at and renders it into" >&2
    echo "    a policy for a different one ([[LSN-018]])." >&2
    return 2
  fi
  if [ -z "$tier" ] || [ -z "$ns" ]; then
    echo "  shipped-render: REFUSED — usage: shipped_render_tier_egress <context> <tier> <namespace> [netpol-name]" >&2
    return 2
  fi
  : "${name:=${tier}-egress}"

  scripts="${SHIPPED_RENDER_REPO_ROOT}/k8s-operator/scripts"
  if [ ! -f "$scripts/common.sh" ]; then
    echo "  shipped-render: no common.sh at $scripts — set SHIPPED_RENDER_REPO_ROOT." >&2
    return 1
  fi

  # The arguments travel as environment, not as positionals: `common.sh` parses "$@" for
  # `--dry-run`/`--no-confirm`/`-y` at load, and `set --` empties the list before that parse can
  # read a tier name as a flag.
  rendered="$(
    cd "$scripts" 2>/dev/null &&
      SCRIPT_DIR="$scripts" \
        WORKLOAD_IDENTITY_ENABLED="$SHIPPED_RENDER_WORKLOAD_IDENTITY" \
        GKE_DATAPLANE="$SHIPPED_RENDER_GKE_DATAPLANE" \
        CONTROL_NAMESPACE="${CONTROL_NAMESPACE:-kubeagents-system}" \
        SR_CTX="$ctx" SR_NAME="$name" SR_NS="$ns" SR_TIER="$tier" \
        bash -c '
          set --
          # shellcheck disable=SC1091
          . ./common.sh >/dev/null 2>&1 || exit 1
          # common.sh installs `trap cleanup EXIT` at load and its cleanup writes `tput cnorm` to
          # STDOUT — the same stream the manifest is captured on. Cleared before anything can fire it.
          trap - EXIT
          # The shipped resolver calls a BARE kubectl. A shell function shadows the binary for this
          # subshell only, so resolve_apiserver_cidrs stays the single definition site AND asks the
          # cluster under test instead of the ambient one.
          kubectl() { command kubectl --context "$SR_CTX" "$@"; }
          KUBE_APISERVER_CIDRS="$(resolve_apiserver_cidrs)" || exit 1
          export KUBE_APISERVER_CIDRS
          printf "  shipped-render: rule 9 resolved to %s\n" "$KUBE_APISERVER_CIDRS" >&2
          render_egress_policy "$SR_NAME" "$SR_NS" "$SR_TIER"
        '
  )" || {
    echo "  shipped-render: could not resolve a kube-apiserver address for context '$ctx', so the" >&2
    echo "    policy would carry no rule 9. Applying it would close TokenReview, the FleetFreeze" >&2
    echo "    read and the ActionRecord write for every tier-labelled pod in the namespace — the" >&2
    echo "    broker included. This is provision_13's own refusal (V-CMP-003)." >&2
    return 1
  }

  # Both halves of the artifact check, on the bytes about to be applied. Verdicts to stderr so
  # stdout stays a manifest.
  printf '%s\n' "$rendered" | shipped_render_assert_rule9_matches_endpoints "$ctx" >&2
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  shipped-render: REFUSING to hand back this manifest for $ns (tier $tier, context $ctx)." >&2
    return "$rc"
  fi

  printf '%s\n' "$rendered"
  echo "  shipped-render: NetworkPolicy/$name for $ns rendered by common.sh:render_egress_policy —" >&2
  echo "    the same function provision_13 calls, never a copy ([[LSN-024]])." >&2
  return 0
}

# -------------------------------------------------------------------------------------------------
# `--negative-control` — the mandatory offline ¬ arm (V-MET-014)
#
# Every row NAMES THE RULE IT EXERCISES, in the row name and in the asserted signal, because a
# control that only proves the code went red proves almost nothing ([[LSN-035]]; the convention is
# `dev/tests/negative-controls-name-their-rule.py`'s and `dev/lib/l2-lock.sh`'s). Nothing here
# touches a cluster: the manifests are synthetic and the endpoint list is the literal 10.150.0.9
# measured on the scratch cluster on 2026-08-01.
#
# The prefix on every helper below is not decoration. `l2-lock.sh` already defines `nc_ok`/`nc_bad`
# at file scope, and a suite that sources both libraries would otherwise get whichever was sourced
# last.
# -------------------------------------------------------------------------------------------------

_SR_NC_TOTAL=0
_SR_NC_PASS=0
_SR_NC_FAIL=0

_sr_nc_ok() {
  _SR_NC_TOTAL=$((_SR_NC_TOTAL + 1))
  _SR_NC_PASS=$((_SR_NC_PASS + 1))
  printf 'PASS: %-62s %s\n' "$1" "$2"
}

_sr_nc_bad() {
  _SR_NC_TOTAL=$((_SR_NC_TOTAL + 1))
  _SR_NC_FAIL=$((_SR_NC_FAIL + 1))
  printf 'FAIL: %-62s %s\n' "$1" "$2"
}

# _sr_nc_manifest <case>
#   The synthetic policies. Shaped like the real render — rules 1 and 4 present, rule 9 introduced
#   by `render_apiserver_block`'s `# 9)` marker and closed by its `ports:` line — because a fixture
#   the extractor cannot fail on is not a fixture.
_sr_nc_manifest() {
  case "$1" in
    no-rule-9) _sr_nc_head "" ;;
    endpoint-in-rule-4) _sr_nc_head "10.150.0.9/32" ;;
    lsn069-bug)
      _sr_nc_head ""
      _sr_nc_rule9 "34.118.224.1/32" "35.221.35.254/32"
      ;;
    correct-render)
      _sr_nc_head ""
      _sr_nc_rule9 "10.150.0.9/32" "34.118.224.1/32" "35.221.35.254/32"
      ;;
    rule-9-with-no-addresses)
      _sr_nc_head ""
      _sr_nc_rule9
      ;;
    range-only)
      _sr_nc_head ""
      _sr_nc_rule9 "10.150.0.0/28"
      ;;
    unsubstituted)
      # A GOOD rule 9 alongside the token, so the row proves the token check fires FIRST rather
      # than proving that a manifest with two defects fails for one of them.
      # The single quotes are load-bearing: the token must survive INTO the fixture unexpanded,
      # which is the defect the row is about.
      # shellcheck disable=SC2016
      _sr_nc_head "" | sed 's/namespace: kubeagents-system/namespace: ${AGENT_NAMESPACE}/'
      _sr_nc_rule9 "10.150.0.9/32"
      ;;
    rule-4-endpoint-only)
      _sr_nc_head "10.150.0.9/32"
      _sr_nc_rule9 "34.118.224.1/32" "35.221.35.254/32"
      ;;
    empty) : ;;
  esac
}

# _sr_nc_head [extra-rule-4-cidr]
_sr_nc_head() {
  cat <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: platform-egress
  namespace: kubeagents-system
spec:
  podSelector:
    matchLabels:
      kube-agents/tier: platform
  policyTypes:
    - Egress
  egress:
    # 1) DNS — cluster resolution. Without this, every name-based egress fails before it starts.
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
    # 4) GitHub — the four published, stable IPv4 blocks.
    - to:
        - ipBlock:
            cidr: 192.30.252.0/22
YAML
  if [ -n "${1:-}" ]; then
    printf '        - ipBlock:\n            cidr: %s\n' "$1"
  fi
  cat <<'YAML'
      ports:
        - protocol: TCP
          port: 443
YAML
}

# _sr_nc_rule9 <cidr>...
_sr_nc_rule9() {
  local c
  cat <<'YAML'
    # 9) The kube-apiserver. The BROKER cannot work without it — TokenReview (pipeline step 1),
    #    the FleetFreeze read (step 5) and the ActionRecord write (step 11) all go here.
    - to:
YAML
  for c in "$@"; do
    printf '        - ipBlock:\n            cidr: %s\n' "$c"
  done
  cat <<'YAML'
      ports:
        - protocol: TCP
          port: 443
YAML
}

# _sr_nc_row <row-name> <fixture> <want-rc> <want-signal> [must-not-contain]
_sr_nc_row() {
  local row="$1" fixture="$2" want_rc="$3" signal="$4" absent="${5:-}"
  local out rc

  out="$(_sr_nc_manifest "$fixture" | shipped_render_score_manifest 10.150.0.9 2>&1)" && rc=0 || rc=$?

  if [ "$rc" -ne "$want_rc" ]; then
    _sr_nc_bad "$row" "wanted rc $want_rc, got rc $rc: $(printf '%s' "$out" | tr '\n' ' ')"
    return
  fi
  if ! printf '%s' "$out" | grep -q -- "$signal"; then
    _sr_nc_bad "$row" "rc $rc was right and the reason was not: no '$signal' in the verdict"
    return
  fi
  if [ -n "$absent" ] && printf '%s' "$out" | grep -q -- "$absent"; then
    _sr_nc_bad "$row" "the verdict also carried '$absent', so it did not stop where the rule says"
    return
  fi
  _sr_nc_ok "$row" "rc $rc, and the verdict names $signal"
}

shipped_render_negative_control() {
  local out rc

  echo
  echo "-- THE RULE-9-PRESENT RULE: a tier policy with no apiserver rule closes the broker's writes --"
  _sr_nc_row "a-manifest-with-no-rule-9-is-rejected" no-rule-9 1 "R9-ABSENT"
  _sr_nc_row "a-rule-9-marker-with-no-addresses-in-it-is-rejected" rule-9-with-no-addresses 1 "R9-ABSENT"

  echo
  echo "-- THE ENDPOINT-MATCH RULE: the one LSN-069 is about --"
  _sr_nc_row "rule-9-naming-only-non-endpoint-addresses-is-rejected" lsn069-bug 1 "R9-NO-ENDPOINT-MATCH"
  _sr_nc_row "rule-9-carrying-the-endpoint-address-is-accepted" correct-render 0 "R9-MATCH"

  # THE ROW THAT PINS THE PREDICATE. The accepted render above contains TWO addresses that are not
  # endpoint addresses, and it must be accepted anyway: resolve_apiserver_cidrs emits the ClusterIP
  # and the kubeconfig host deliberately. A subset predicate ("every rendered address is an endpoint
  # address") passes every other row in this table and fails only this one, which is why it is here.
  out="$(_sr_nc_manifest correct-render | shipped_render_score_manifest 10.150.0.9 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "R9-ADDR-NO-MATCH"; then
    _sr_nc_ok "the-predicate-is-at-least-one-not-every-address" \
      "accepted a render carrying non-endpoint addresses, and said so per address"
  else
    _sr_nc_bad "the-predicate-is-at-least-one-not-every-address" \
      "wanted rc 0 WITH per-address R9-ADDR-NO-MATCH lines; got rc $rc"
  fi

  echo
  echo "-- THE RULE-9-SCOPE RULE: rules 3 and 4 are full of CIDRs and none of them is the apiserver --"
  _sr_nc_row "an-endpoint-address-sitting-in-rule-4-does-not-satisfy-rule-9" \
    rule-4-endpoint-only 1 "R9-NO-ENDPOINT-MATCH" "R9-ADDR-MATCH"

  echo
  echo "-- THE SUBSTITUTION RULE: a half-rendered policy applies cleanly and denies everything --"
  _sr_nc_row "an-unsubstituted-token-is-rejected-before-rule-9-is-scored" \
    unsubstituted 1 "TOKEN-UNSUBSTITUTED" "R9-MATCH"

  echo
  echo "-- THE NON-VACUITY RULE: 'could not be scored' must never read as 'the property holds' --"
  _sr_nc_row "an-empty-manifest-is-rejected-not-passed" empty 1 "MANIFEST-EMPTY"
  _sr_nc_row "a-rule-9-naming-a-range-is-reported-unscored-not-silently-accepted" \
    range-only 3 "R9-UNSCORED-RANGE"

  out="$(_sr_nc_manifest correct-render | shipped_render_score_manifest 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 3 ] && printf '%s' "$out" | grep -q "R9-UNSCOREABLE-NO-ENDPOINTS"; then
    _sr_nc_ok "an-unreadable-endpoint-list-is-deferred-not-reported-as-the-LSN-069-defect" \
      "rc 3, and the blocker is named rather than blamed on the manifest"
  else
    _sr_nc_bad "an-unreadable-endpoint-list-is-deferred-not-reported-as-the-LSN-069-defect" \
      "wanted rc 3 naming R9-UNSCOREABLE-NO-ENDPOINTS; got rc $rc"
  fi

  echo
  echo "-- THE EXPLICIT-CONTEXT RULE: a render aimed at the ambient context answers for a third cluster --"
  out="$(shipped_render_tier_egress "" platform some-ns 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -q "explicit kubectl context"; then
    _sr_nc_ok "a-render-with-no-context-is-refused" "rc 2, and the refusal names the missing context"
  else
    _sr_nc_bad "a-render-with-no-context-is-refused" "wanted rc 2 naming the context; got rc $rc: $out"
  fi

  out="$(shipped_render_apiserver_endpoint_addresses 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "explicit kubectl context"; then
    _sr_nc_ok "an-endpoint-read-with-no-context-is-refused" "rc $rc, and no cluster was addressed"
  else
    _sr_nc_bad "an-endpoint-read-with-no-context-is-refused" "wanted a non-zero rc naming the context; got rc $rc: $out"
  fi

  echo
  echo "negative control: $_SR_NC_PASS/$_SR_NC_TOTAL rows scored as expected, $_SR_NC_FAIL wrong"
  [ "$_SR_NC_FAIL" -eq 0 ] || return 1
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -uo pipefail
  case "${1:-}" in
    --negative-control)
      echo "shipped-render.sh --negative-control — the offline ¬ for the LSN-069 rule-9 predicate"
      shipped_render_negative_control
      exit $?
      ;;
    *)
      echo "shipped-render.sh is a library. Source it, or run:" >&2
      echo "  bash dev/lib/shipped-render.sh --negative-control" >&2
      exit 2
      ;;
  esac
fi
