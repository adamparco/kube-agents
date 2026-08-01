#!/usr/bin/env bash
# tier-egress-render-l2.sh — the caller for two library predicates that had none.
#
# There is no `V-` row for this file, and that is stated first so nobody looks for one. 09 §6 grades
# rows; this suite closes two LESSONS whose mechanizations shipped as library functions and were
# then never invoked by anything. A mechanization with no caller is prose with a test attached
# ([[LSN-019]]) — `dev/lib/preconditions.sh:p11_assert_namespace_admits_dns` and
# `dev/lib/shipped-render.sh`'s rule-9 predicate were both in that state. This file is their caller.
# The nearest graded neighbours are V-CMP-003 (provision_13's own refusal, which
# `shipped_render_tier_egress` reproduces) and V-CTN-036; neither row's property is asserted here.
#
# ------------------------------------------------------------------------------------------------
# ARM 1 — [[LSN-068]] item 3: every Agent's namespace admits DNS to that Agent's tier
# ------------------------------------------------------------------------------------------------
# The operator's `<agent>-to-broker` policy (`buildAgentToBrokerPolicy`) is EGRESS-ONLY and renders
# exactly one rule: TCP 8443 to the actor pod. In Kubernetes a pod selected by ANY egress policy is
# default-deny for EVERY other egress, DNS included. The operator is right to render only the hop it
# owns — rule 1 of the per-tier allowlist is what carries DNS, and that allowlist is applied by
# `provision_13_apply_network_policies.sh`, which `dev/cluster/up.sh` does not run. So the moment the
# controller reconciles an Agent into a namespace no install path touched, the pair silently loses
# name resolution, and the transcript reads as a broker that never came up.
#
# LSN-068 proposed three mechanizations and called this one "the only one of the three that would
# catch a real install". The other two are already closed. This is the third.
#
# THE VERDICT IS DELIBERATELY NOT A FAILURE. `p11_assert_namespace_admits_dns` returns rc 2 —
# COULD-NOT-RUN — for a namespace that does not model an installed agent, never rc 1, because an
# unseeded namespace is not a failed security property. This suite reports rc 2 as DEFERRED. A caller
# that converted it into a red would re-create the exact confusion the lesson is about, and would
# have broken the precondition's contract ([[LSN-038]]).
#
# THE NAMESPACE SET IS DERIVED, NEVER LISTED ([[LSN-036]]). Two sources, unioned:
#   - every namespace holding an `agents.kubeagents.x-k8s.io` CR, which is LSN-068's own wording,
#     with the tier taken from `.spec.tier` and defaulted by the DEFAULT THE SERVED CRD DECLARES —
#     read off the cluster, not typed here. (`agentindex.EffectiveTier` maps the empty string to
#     `platform` in Go; the CRD carries `default: platform` for the same reason. Reading the served
#     schema means this file holds no second opinion about which it is.)
#   - every namespace holding a workload the operator stamped with `kube-agents/tier` — the label
#     `agentlabels.Tier` puts on both Deployments and both pod templates, and the same label the
#     per-tier egress policy selects on. A namespace whose CR was deleted while its pair is still
#     running is still a namespace where this property matters.
# The union, not either one: the CR list alone misses an orphaned pair, and the workload list alone
# misses an Agent the controller has not reconciled yet.
#
# ------------------------------------------------------------------------------------------------
# ARM 2 — [[LSN-069]]: rule 9 of the rendered tier policy names an address a packet actually carries
# ------------------------------------------------------------------------------------------------
# NetworkPolicy is L3/L4 and cannot name a Service, so "allow the kubernetes endpoint" is
# inexpressible: rule 9 has to be an ADDRESS, and which address is per-cluster and known only at
# install time. GKE Dataplane V2 DNATs the `kubernetes` ClusterIP in eBPF BEFORE egress policy is
# scored, so the address the policy sees is the one in `endpoints/kubernetes` — not the ClusterIP and
# not the kubeconfig host. A rule 9 naming only the other two applies cleanly, reads correctly, and
# closes TokenReview (broker step 1), the FleetFreeze read (step 5) and the ActionRecord write
# (step 11). The symptom is a broker that blocks in `startSources()` before it binds :8443: `kubectl
# logs` is EMPTY and both probes say "connection refused". Nothing anywhere says "network".
#
# LSN-069 asked for exactly this arm: "for each address in a rendered rule 9, assert it appears in
# `endpoints/kubernetes` — cheap, needs no traffic, and would have gone red on both clusters."
#
# THE PREDICATE IS "AT LEAST ONE", NOT "EVERY", AND MUST STAY THAT WAY. `resolve_apiserver_cidrs`
# emits all three address forms ON PURPOSE — the endpoint address, the Service ClusterIP and the
# kubeconfig host — because which one the dataplane sees depends on where it evaluates egress
# relative to DNAT, and a policy naming only one of them fails on the other dataplane as a
# connection timeout inside a client library. Two of the three are therefore EXPECTED not to appear
# in `endpoints/kubernetes`. Tightening this to "every" would make a correct render red.
#
# ZERO ENDPOINT ADDRESSES IS UNSCOREABLE, NOT A FAILURE. `shipped_render_score_manifest` checks the
# endpoint list before it scores any address and returns rc 3 with the blocker named, because with
# an empty list every address scores NO-MATCH and the run would report the LSN-069 defect when what
# actually happened is that the cluster read came back empty. This suite reports rc 3 as DEFERRED.
#
# THE RENDER IS THE SHIPPED ONE ([[LSN-024]]). `shipped_render_tier_egress` sources
# `k8s-operator/scripts/common.sh` and calls `render_egress_policy` — the same function provision_13
# calls, never a copy — with a `kubectl()` shadow that carries this suite's context into the shipped
# resolver. A local re-implementation would be a check of a policy no install produces.
#
# NOT COVERED BY ARM 2: THE POLICY THAT IS ACTUALLY INSTALLED. This arm's subject is the bytes the
# SHIPPED RENDERER PRODUCES TODAY — provision_13's own pre-apply refusal, made here. It is silent
# about whether the NetworkPolicy object standing in the namespace right now carries a rule 9 at
# all, and the two can differ by an install that predates the rule. Measured read-only on
# 2026-08-01 against the live install `gke_adamparco-kage_us-east4_platform-agent-host`: all three
# tier policies (`platform-egress`, `cluster-admin-egress`, `developer-team-egress`) carry exactly
# four egress rules — DNS, the control namespace on :80/:8080, the Google restricted VIP and
# GitHub's four blocks — and NO kube-apiserver rule whatsoever, while three tier-labelled gateways
# run under them. A fresh render against that same cluster resolves rule 9 to
# `10.150.0.2/32,34.118.224.1/32,34.145.154.119/32` and scores R9-MATCH. So arm 2 would have
# reported PASS there while the installed policy carried nothing. Closing that needs a second
# subject — the object, not the render — and `shipped_render_rule9_cidrs` cannot supply it: it scopes
# to the `# 9)` comment marker, which `kubectl get -o yaml` does not return. That arm is proposed,
# not shipped here, and this file's green banner says so out loud rather than only in this comment.
#
# THE TIER SET IS DERIVED FROM THE SERVED CRD's `tier` ENUM, unioned with the tiers actually seen in
# the roster. That keeps arm 2 non-vacuous on a cluster with no agents installed at all (which is
# the state of the scratch cluster most of the time) without ever listing three tier names in this
# file. The namespace handed to the renderer is a roster namespace for that tier when one exists and
# the control namespace otherwise; rule 9 does not depend on it, and this suite APPLIES NOTHING.
#
# ------------------------------------------------------------------------------------------------
# WHAT THIS SUITE WRITES
# ------------------------------------------------------------------------------------------------
# Both arms are READ-ONLY. Every cluster call this file makes is a `get`: no namespace is created,
# no policy is applied, no Agent is minted, and the manifest arm 2 renders is scored in a temp
# directory and never reaches a cluster. `render_egress_policy` writes to stdout; the `apply` that
# would follow it in provision_13 is not in this file.
#
# The one write on the live path is `p10_assert_control_plane_healthy`'s own probe namespace, which
# it creates and deletes to prove the API server still accepts writes. That is the precondition's
# write, on the destructive-test target, and it is why the guard below is not optional even though
# this suite's own arms would survive without one. A read-only suite that grows one `apply` is a
# suite whose guard was removed in a different commit from the one that made it necessary.
#
# ------------------------------------------------------------------------------------------------
# A KNOWN LIMITATION OF ARM 1's PREDICATE, FOUND BY THIS FILE'S CONTROL AND NOT FIXED HERE
# ------------------------------------------------------------------------------------------------
# `p11_assert_namespace_admits_dns` reads its policy table with `while IFS=<TAB> read -r name sel
# rules types`. TAB is an IFS *whitespace* character, so consecutive tabs collapse into one and the
# columns shift left whenever a field is empty. A policy with `podSelector: {}` renders an empty
# `sel` — `kubectl -n team-x get networkpolicies -o jsonpath=...` on the live install returns
# `default-deny-all\t\t\t["Ingress","Egress"]` — so the branch that says "an empty selector selects
# every pod in the namespace" is unreachable against real output, and a namespace whose DNS comes
# from a namespace-wide `allow-dns` policy is read as not admitting DNS.
#
# The consequence for this suite is a FALSE DEFERRAL, never a false pass: the row reports
# COULD-NOT-RUN for a namespace that actually resolves names. That is the safe direction, which is
# why this file ships rather than waiting. It is not repaired here because `dev/lib/` is a single
# definition site and a suite that quietly worked around its own precondition would leave the next
# caller to rediscover it ([[LSN-019]]). The control below therefore asserts what the predicate
# genuinely guarantees — exact tier matching, the `policyTypes` gate, the portless-rule case — and
# does not pin the misparse as if it were intended.
#
# ------------------------------------------------------------------------------------------------
# NON-VACUITY ([[LSN-035]])
# ------------------------------------------------------------------------------------------------
# A zero-row roster is NOT a pass. "Every Agent's namespace admits DNS" is vacuously true on a
# cluster with no Agents, and a green banner for that would be the most convincing kind of nothing.
# `score_roster` emits `VACUOUS-ROSTER` and a DEFERRAL, and a deferral is worth exit 3 — never 0.
# Both arms also assert that every derived row produced exactly one scored verdict, so a loop that
# quietly stops iterating is a red rather than a shorter green ([[LSN-063]]).
#
# ------------------------------------------------------------------------------------------------
# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) `--negative-control` touches no cluster. It
# drives `p11_assert_namespace_admits_dns` through a synthetic `kubectl`, drives
# `shipped_render_score_manifest` over synthetic manifests, and drives this file's own derivation and
# verdict mapping over synthetic input. What its green therefore says nothing about:
#   - THE COLLECTION STATEMENTS. Every `kubectl get` in the live path is bypassed, including the
#     `kube-agents/tier` selector that finds the workloads and the jsonpath that reads the CRD's
#     tier default and enum. A selector that matched nothing would produce an empty roster live and
#     a full one here, and only `score_roster` distinguishes them.
#   - THE SHIPPED RENDER. `shipped_render_tier_egress` is never called: not the `. ./common.sh`,
#     not the `set --` before it or the `trap - EXIT` after it, not the `kubectl()` shadow, not
#     `resolve_apiserver_cidrs`, not `envsubst` and not the template on disk. The control scores
#     manifests it wrote itself, so a render that stopped producing rule 9 for a live reason is
#     invisible to it.
#   - THE ENDPOINT READ. `shipped_render_apiserver_endpoint_addresses` is never called, so neither
#     the EndpointSlice read nor its v1-Endpoints fallback is exercised.
#   - THE DATAPLANE. That Dataplane V2 DNATs before scoring egress is the premise of arm 2 and is
#     asserted nowhere, at any level, by anything. It is why arm 2 is a render check and not a
#     traffic check; a traffic check is the honest instrument and it is not this file.
#   - THE LOCK. `l2_lock_guard` is live-path only. `bash dev/lib/l2-lock.sh --negative-control` is
#     its own ¬ arm.
#   - P10. The control plane health assertion runs only against a real control plane.
#
# ------------------------------------------------------------------------------------------------
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: none — no first-party image participates. Nothing this suite reads was
#      produced by an agent, a broker or the controller binary: the subjects are NetworkPolicy
#      objects the API server holds, the `kubernetes` Service's endpoints, the served CRD schema,
#      and a manifest rendered here and now by a SHELL function in the working tree. A stale image
#      cannot change any of those four answers, so there is no digest whose currency is load-bearing
#      and P1 would assert nothing it could later be wrong about.
#   P3 admission-recreate: none — nothing is created, so nothing can be grandfathered. Arm 1 reads
#      NetworkPolicy objects, which the dataplane re-evaluates continuously rather than at admission,
#      so "the policy in force now" is what a `get` returns now. Arm 2's subject is a manifest this
#      run rendered; it has never been through admission and is never applied.
#   P6 runtime-authoritative: asserted by construction, on both arms. Arm 1 reads the LIVE
#      NetworkPolicy objects, never `netpol-agent-egress.yaml.template` and never a golden. Arm 2
#      scores the render against `endpoints/kubernetes` READ FROM THE CLUSTER UNDER TEST, and the
#      render itself is produced by the shipped `render_egress_policy` with the cluster's own
#      `resolve_apiserver_cidrs` output substituted in — so the addresses under test are this
#      cluster's, obtained now, and not any file's idea of them.
#   P10 control-plane-healthy: asserted via p10_assert_control_plane_healthy before the first
#      question. A cluster that has stopped converging still answers a `get`, but it can answer with
#      a PARTIAL list, and a short roster is a suite reporting "every Agent namespace" about the
#      namespaces it happened to see.
#   P12 one-L2-suite-per-cluster: taken via l2_lock_guard before any cluster read. This suite
#      derives its subject set cluster-wide from `kube-agents/tier`, and every suite that seeds an
#      agent identity through the shipped renderer mints objects carrying that label — correctly.
#      Run concurrently with one of them, this suite measures the other suite's fixtures ([[LSN-066]]).
#
# Run:  bash dev/verify/tier-egress-render-l2.sh gke-scratch-kube-agents-dev
#       bash dev/verify/tier-egress-render-l2.sh --negative-control
#
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / precondition refused · 3 = DEFERRED

set -uo pipefail

MODE=live
if [ "${1:-}" = "--negative-control" ]; then MODE=negative-control; shift; fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

# The Agent CRD, spelled once. Fully qualified because `agents` is a plural several API groups could
# claim and the ambiguity resolves silently in the wrong direction.
AGENT_CRD=agents.kubeagents.x-k8s.io

# Where the tier policy is rendered for a tier no roster namespace claims. Honours the same override
# `shipped-render.sh` and `common.sh` honour, so the three cannot disagree about it.
CONTROL_NS="${CONTROL_NAMESPACE:-kubeagents-system}"

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring ([[LSN-005]]). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and
# the live install `gke_adamparco-kage_us-east4_platform-agent-host` is one `*` away. Placed above
# every network call — including the reachability probe — because a suite that dials a cluster
# before deciding whether it is allowed to has already touched it. It runs in `--negative-control`
# mode too, against the default context, so the guard cannot rot in a mode nobody points at a
# cluster.
#
# This suite only reads. The guard is here anyway: see "WHAT THIS SUITE WRITES" above.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    echo "  Name the dev cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    echo "  The live install is verification-only and is never a target of this chain, even for a" >&2
    echo "  suite that happens to be read-only today." >&2
    exit 2
    ;;
esac

WORK="$(mktemp -d "${TMPDIR:-/tmp}/tier-egress-render-l2.XXXXXX")"
FAILFILE="$WORK/failures"
DEFERFILE="$WORK/deferrals"
CNTFILE="$WORK/assertions"
: >"$FAILFILE"
: >"$DEFERFILE"
: >"$CNTFILE"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# Counters live in FILES, not in shell variables. `producer | consumer` runs the consumer in a
# subshell, so a `fail=1` set inside one is discarded when the pipeline ends: webhook-negatives-l2.sh
# ran 31 assertions, printed four `FAIL:` lines and exited 0 for exactly that reason ([[LSN-064]]).
pass() { echo "PASS: $1"; echo x >>"$CNTFILE"; }
bad()  { echo "FAIL: $1"; echo x >>"$CNTFILE"; echo x >>"$FAILFILE"; }
defer() { echo "DEFERRED: $1"; echo x >>"$CNTFILE"; echo x >>"$DEFERFILE"; }
note() { echo "  $1"; }
quote() { sed 's/^/      | /' "$1"; }
count_of() { if [ -s "$1" ]; then wc -l <"$1" | tr -d ' '; else echo 0; fi; }

TAB="$(printf '\t')"

# ------------------------------------------------------------------------------------------------
# THE JUDGEMENT. Four functions, pure, no cluster. Everything this file decides for itself is here,
# so `--negative-control` drives the same code the live run does rather than a model of it.
# ------------------------------------------------------------------------------------------------

# roster_rows <default-tier>
#   `namespace<TAB>tier` rows on stdin from any number of sources; the deduplicated union on stdout.
#   An empty tier becomes <default-tier>, which the caller reads off the SERVED CRD — this function
#   holds no opinion about what the default is. A row with no namespace is dropped: it is a jsonpath
#   that produced a trailing newline, not a namespace called "".
roster_rows() {
  awk -F'\t' -v deft="${1:-}" '
    { ns = $1; tier = $2 }
    ns == "" { next }
    tier == "" { tier = deft }
    !seen[ns "\x1f" tier]++ { print ns "\t" tier }
  '
}

# verdict_for_p11 <rc> — the whole of LSN-068's contract, in one place.
#   rc 0 = a policy admits DNS to this tier · rc 2 = COULD-NOT-RUN, which is a DEFERRAL and never a
#   red · anything else = the precondition itself misbehaved, which is a red because a predicate
#   returning an rc its own header does not document has not answered the question.
verdict_for_p11() {
  case "${1:-}" in
    0) echo "ARM1-PASS" ;;
    2) echo "ARM1-DEFERRED" ;;
    *) echo "ARM1-FAIL" ;;
  esac
}

# verdict_for_render <rc> — the whole of shipped_render_tier_egress's contract.
#   rc 0 = rule 9 names an address the packet carries · rc 3 = the property could not be scored
#   (no endpoints, or every entry a range), a DEFERRAL and never a red · rc 1 = the LSN-069 defect,
#   an absent rule 9, an empty manifest or a left-behind token · rc 2 = bad arguments, which is a
#   defect in THIS file and is a red rather than a skip, because a skipped arm and a passing arm
#   produce the same banner.
verdict_for_render() {
  case "${1:-}" in
    0) echo "ARM2-PASS" ;;
    3) echo "ARM2-DEFERRED" ;;
    *) echo "ARM2-FAIL" ;;
  esac
}

# overall_exit <n-failures> <n-deferrals> — a failure dominates a deferral; a deferral is never 0.
overall_exit() {
  if [ "${1:-0}" -gt 0 ]; then echo 1
  elif [ "${2:-0}" -gt 0 ]; then echo 3
  else echo 0
  fi
}

# score_roster <n-rows> — the non-vacuity guard, as a function so the control can drive it.
score_roster() {
  if [ "${1:-0}" -gt 0 ]; then
    pass "the Agent-namespace roster is non-empty: $1 (namespace, tier) pair(s) derived from the cluster"
    return 0
  fi
  defer "VACUOUS-ROSTER — no namespace on this cluster holds an Agent CR or a kube-agents/tier"
  note "workload, so 'every Agent's namespace admits DNS' is vacuously true and arm 1 asserted"
  note "nothing. A green here would be a pass produced by not asking ([[LSN-035]]). Seed an agent"
  note "(dev/verify/seed-agent-fixtures.sh) and re-run to score arm 1."
  return 3
}

echo "===================================================================="
echo " tier-egress-render-l2 — the caller for two library predicates"
echo "   arm 1  LSN-068 item 3 — every Agent namespace admits DNS to its tier"
echo "   arm 2  LSN-069        — rule 9 names an address a packet carries"
echo "   mode: $MODE — ctx: $CTX"
echo "===================================================================="

# ------------------------------------------------------------------------------------------------
# NEGATIVE CONTROL. No cluster, no lock, no render. Every row NAMES THE RULE IT EXERCISES, in the
# row name and in the asserted needle, because a control that only proves the suite went red proves
# almost nothing ([[LSN-035]]; scored by dev/tests/negative-controls-name-their-rule.py).
# ------------------------------------------------------------------------------------------------

# A `kubectl` that is a shell function, so `$K -n ns get networkpolicies -o jsonpath=...` inside
# p11_assert_namespace_admits_dns resolves to this and the precondition is exercised for real
# against a policy table this file wrote. `TE_FAKE_NP` is the precondition's own output contract:
# name, podSelector.matchLabels JSON, one bracketed group per egress rule, policyTypes.
TE_FAKE_NP=""
# shellcheck disable=SC2329  # invoked as `$K` from inside the precondition, which is the point
te_fake_kubectl() {
  printf '%s' "$TE_FAKE_NP"
}

# nc_row <label> <needle> <want-rc> <want-verdict> — run the last-declared probe and score it twice:
# the rc against the contract, and the OUTPUT against a needle that names the property. A row that
# asserted only "it went non-zero" would pass against a predicate that had stopped reading its input.
nc_score() { # nc_score <label> <got-rc> <want-rc> <got-verdict> <want-verdict> <needle> <outfile>
  local label="$1" got_rc="$2" want_rc="$3" got_v="$4" want_v="$5" needle="$6" out="$7"
  if [ "$got_rc" != "$want_rc" ]; then
    bad "$label — wanted rc $want_rc, got rc $got_rc"
    quote "$out"
    return 1
  fi
  if [ "$got_v" != "$want_v" ]; then
    bad "$label — rc $got_rc mapped to $got_v; the contract says $want_v"
    return 1
  fi
  if ! grep -qF -- "$needle" "$out"; then
    bad "$label — rc and verdict were right but '$needle' never appeared, so the row is asserting that something failed rather than that this property is what caught it"
    quote "$out"
    return 1
  fi
  pass "$label — rc $got_rc / $got_v / names '$needle'"
  return 0
}

# nc_untally <assertions-before> <deferrals-before> — restore the counters to a snapshot, so a row
# that drives a scoring function for its VERDICT does not also inflate the row count.
nc_untally() {
  # `head -n 0` is an error on BSD head and a no-op on GNU head, so zero is handled without it.
  _nc_truncate() {
    if [ "$1" -le 0 ]; then : >"$2"; return 0; fi
    head -n "$1" "$2" >"$WORK/untally" && mv "$WORK/untally" "$2"
  }
  _nc_truncate "$1" "$CNTFILE"
  _nc_truncate "$2" "$DEFERFILE"
}

run_negative_control() {
  local out="$WORK/nc.out" rc v got want

  # shellcheck disable=SC1091
  . "$REPO_ROOT/dev/lib/preconditions.sh"
  # shellcheck disable=SC1091
  . "$REPO_ROOT/dev/lib/shipped-render.sh"

  echo
  echo "== arm 1's predicate, driven through a synthetic kubectl (no cluster) =="

  # Every row is `<label>|<want-rc>|<want-verdict>|<needle>|<tier>`, with the policy table read from
  # the following line. Quoted delimiter: the heredoc is a constant, so nothing here is a computed
  # redirection operand ([[LSN-065]]).
  local label want_rc want_v needle tier table
  while IFS='|' read -r label want_rc want_v needle tier table; do
    [ -n "$label" ] || continue
    case "$label" in \#*) continue ;; esac
    TE_FAKE_NP="$(printf '%b' "$table")"
    p11_assert_namespace_admits_dns te_fake_kubectl nc-ns "$tier" >"$out" 2>&1
    rc=$?
    v="$(verdict_for_p11 "$rc")"
    nc_score "$label" "$rc" "$want_rc" "$v" "$want_v" "$needle" "$out"
  done <<'P11ROWS'
a-policy-selecting-the-tier-and-admitting-53-UDP-is-an-installed-namespace|0|ARM1-PASS|ADMITS DNS to tier=platform|platform|platform-egress\t{"kube-agents/tier":"platform"}\t[53/UDP,]\t["Egress"]\n
an-egress-rule-with-no-ports-admits-every-port-including-53|0|ARM1-PASS|ADMITS DNS to tier=platform|platform|allow-all-egress\t{"kube-agents/tier":"platform"}\t[]\t["Egress"]\n
the-tier-in-the-selector-is-matched-EXACTLY-so-a-longer-tier-name-is-not-a-hit|2|ARM1-DEFERRED|admits DNS but not to tier=platform|platform|platform-team-egress\t{"kube-agents/tier":"platform-team"}\t[53/UDP,]\t["Egress"]\n
a-policy-that-selects-the-tier-but-opens-no-port-53-is-COULD-NOT-RUN-not-a-red|2|ARM1-DEFERRED|selects tier=platform but admits no port 53|platform|to-broker\t{"kube-agents/tier":"platform"}\t[8443/TCP,]\t["Egress"]\n
a-DNS-rule-that-selects-a-DIFFERENT-tier-does-not-admit-DNS-for-this-one|2|ARM1-DEFERRED|admits DNS but not to tier=platform|platform|developer-team-egress\t{"kube-agents/tier":"developer-team"}\t[53/UDP,]\t["Egress"]\n
an-Ingress-only-policy-carrying-an-egress-block-admits-nothing-because-policyTypes-gates-it|2|ARM1-DEFERRED|selects tier=platform but admits no port 53|platform|ingress-only\t{"kube-agents/tier":"platform"}\t[53/UDP,]\t["Ingress"]\n
a-namespace-with-no-policies-at-all-is-COULD-NOT-RUN-with-the-count-named|2|ARM1-DEFERRED|0 NetworkPolicies|platform|
an-empty-tier-argument-is-refused-rather-than-answered-about-nobody|2|ARM1-DEFERRED|The TIER is required, not optional||platform-egress\t{"kube-agents/tier":"platform"}\t[53/UDP,]\t["Egress"]\n
P11ROWS

  echo
  echo "== arm 2's predicate, over synthetic manifests (no cluster, no render) =="

  # The three address forms `resolve_apiserver_cidrs` emits, and the endpoint list a cluster answers
  # with. The literals are measured values, named as such: 10.150.0.9 is the scratch cluster's
  # endpoint address and 34.118.224.1 its `kubernetes` ClusterIP, both read on 2026-08-01.
  local EPS="10.150.0.9"
  local M_MATCH M_NOMATCH M_ABSENT M_TOKEN
  M_MATCH="$(cat <<'YAML'
kind: NetworkPolicy
spec:
  egress:
    # 9) The kube-apiserver.
    - to:
        - ipBlock:
            cidr: 10.150.0.9/32
        - ipBlock:
            cidr: 34.118.224.1/32
      ports:
        - protocol: TCP
          port: 443
YAML
)"
  M_NOMATCH="${M_MATCH//10.150.0.9/35.221.35.254}"
  M_ABSENT="$(printf 'kind: NetworkPolicy\nspec:\n  egress:\n    # 1) DNS.\n    - to: []\n      ports:\n        - protocol: UDP\n          port: 53\n')"
  # A half-rendered policy: the `envsubst` never ran, so the resolver's value is still a token. The
  # heredoc is quoted, so the token survives to the predicate instead of being expanded here.
  M_TOKEN="$(cat <<'YAML'
kind: NetworkPolicy
spec:
  egress:
    # 9) The kube-apiserver.
    - to:
        - ipBlock:
            cidr: ${KUBE_APISERVER_CIDRS}
      ports:
        - protocol: TCP
          port: 443
YAML
)"

  printf '%s\n' "$M_MATCH" | shipped_render_score_manifest $EPS >"$out" 2>&1
  rc=$?
  nc_score "a-rule-9-matching-ONE-of-its-addresses-passes-the-predicate-is-at-least-one-not-every" \
    "$rc" 0 "$(verdict_for_render "$rc")" ARM2-PASS "R9-MATCH" "$out"

  printf '%s\n' "$M_MATCH" | shipped_render_score_manifest $EPS >"$out" 2>&1
  if grep -qF "34.118.224.1/32 — R9-ADDR-NO-MATCH" "$out"; then
    pass "the-ClusterIP-form-is-reported-NO-MATCH-per-address-and-is-still-not-a-failure — R9-ADDR-NO-MATCH beside R9-MATCH"
  else
    bad "the-ClusterIP-form-is-reported-NO-MATCH-per-address-and-is-still-not-a-failure — the per-address needle R9-ADDR-NO-MATCH is gone, so the aggregate verdict is no longer traceable to an address"
    quote "$out"
  fi

  printf '%s\n' "$M_NOMATCH" | shipped_render_score_manifest $EPS >"$out" 2>&1
  rc=$?
  nc_score "a-rule-9-naming-NO-endpoint-address-is-the-LSN-069-defect-and-is-a-red" \
    "$rc" 1 "$(verdict_for_render "$rc")" ARM2-FAIL "R9-NO-ENDPOINT-MATCH" "$out"

  printf '%s\n' "$M_ABSENT" | shipped_render_score_manifest $EPS >"$out" 2>&1
  rc=$?
  nc_score "a-manifest-with-no-rule-9-at-all-is-a-red-not-a-vacuous-pass" \
    "$rc" 1 "$(verdict_for_render "$rc")" ARM2-FAIL "R9-ABSENT" "$out"

  printf '%s\n' "$M_TOKEN" | shipped_render_score_manifest $EPS >"$out" 2>&1
  rc=$?
  nc_score "a-left-behind-template-token-is-caught-BEFORE-any-address-is-scored" \
    "$rc" 1 "$(verdict_for_render "$rc")" ARM2-FAIL "TOKEN-UNSUBSTITUTED" "$out"

  printf '%s\n' "$M_MATCH" | shipped_render_score_manifest >"$out" 2>&1
  rc=$?
  nc_score "zero-endpoint-addresses-is-UNSCOREABLE-and-must-not-present-as-the-LSN-069-defect" \
    "$rc" 3 "$(verdict_for_render "$rc")" ARM2-DEFERRED "R9-UNSCOREABLE-NO-ENDPOINTS" "$out"

  printf '' | shipped_render_score_manifest $EPS >"$out" 2>&1
  rc=$?
  nc_score "an-empty-manifest-is-a-red-because-it-applies-cleanly-and-changes-nothing" \
    "$rc" 1 "$(verdict_for_render "$rc")" ARM2-FAIL "MANIFEST-EMPTY" "$out"

  echo
  echo "== this file's own derivation and verdict mapping =="

  # The roster. Input is what the two live jsonpaths produce, including the shapes that only appear
  # in real output: a trailing blank line, a CR with an empty `.spec.tier`, and the same namespace
  # arriving from both sources.
  got="$(printf 'kubeagents-system\tplatform\nteam-x\tdeveloper-team\nkubeagents-system\t\nkubeagents-system\tplatform\n\t\n' | roster_rows platform | tr '\t' '=' | tr '\n' ' ')"
  want="kubeagents-system=platform team-x=developer-team "
  if [ "$got" = "$want" ]; then
    pass "the-roster-defaults-an-empty-spec-tier-dedupes-and-drops-a-blank-line — ROSTER-DERIVED: [$got]"
  else
    bad "the-roster-defaults-an-empty-spec-tier-dedupes-and-drops-a-blank-line — ROSTER-DERIVED wanted [$want], got [$got]"
  fi

  got="$(printf 'orphan-ns\tcluster-admin\n' | roster_rows platform | tr '\t' '=' | tr -d '\n')"
  if [ "$got" = "orphan-ns=cluster-admin" ]; then
    pass "the-roster-is-a-UNION-so-a-tier-stamped-workload-with-no-Agent-CR-is-still-in-scope — ROSTER-UNION: [$got]"
  else
    bad "the-roster-is-a-UNION-so-a-tier-stamped-workload-with-no-Agent-CR-is-still-in-scope — ROSTER-UNION wanted [orphan-ns=cluster-admin], got [$got]"
  fi

  got="$(printf 'late-ns\t\n' | roster_rows '' | tr '\t' '=' | tr -d '\n')"
  if [ "$got" = "late-ns=" ]; then
    pass "an-unreadable-CRD-default-leaves-the-tier-EMPTY-so-P11-refuses-rather-than-this-file-guessing — ROSTER-NO-DEFAULT: [$got]"
  else
    bad "an-unreadable-CRD-default-leaves-the-tier-EMPTY-so-P11-refuses-rather-than-this-file-guessing — ROSTER-NO-DEFAULT wanted [late-ns=], got [$got]"
  fi

  # The verdict mappings, both directions, including the rc nobody expects.
  while IFS='|' read -r label fn arg want; do
    [ -n "$label" ] || continue
    got="$("$fn" "$arg")"
    if [ "$got" = "$want" ]; then
      pass "$label — $fn($arg) = $want"
    else
      bad "$label — $fn($arg) returned $got; the contract says $want"
    fi
  done <<'MAPROWS'
p11-rc-0-is-the-only-PASS|verdict_for_p11|0|ARM1-PASS
p11-rc-2-is-COULD-NOT-RUN-and-maps-to-DEFERRED-never-to-FAIL|verdict_for_p11|2|ARM1-DEFERRED
p11-rc-1-is-undocumented-so-it-is-a-red-not-a-quiet-deferral|verdict_for_p11|1|ARM1-FAIL
render-rc-0-is-the-only-PASS|verdict_for_render|0|ARM2-PASS
render-rc-3-is-UNSCOREABLE-and-maps-to-DEFERRED-never-to-FAIL|verdict_for_render|3|ARM2-DEFERRED
render-rc-1-is-the-LSN-069-defect-and-maps-to-FAIL|verdict_for_render|1|ARM2-FAIL
render-rc-2-is-bad-arguments-in-THIS-file-and-is-a-red-not-a-skip|verdict_for_render|2|ARM2-FAIL
MAPROWS

  # The exit composition, which is the only thing the chain reads.
  while IFS='|' read -r label nf nd want; do
    [ -n "$label" ] || continue
    got="$(overall_exit "$nf" "$nd")"
    if [ "$got" = "$want" ]; then
      pass "$label — overall_exit($nf failures, $nd deferrals) = $want"
    else
      bad "$label — overall_exit($nf failures, $nd deferrals) returned $got; wanted $want"
    fi
  done <<'EXITROWS'
an-all-green-run-exits-0|0|0|0
a-deferral-with-no-failure-exits-3-and-never-0|0|1|3
a-failure-dominates-a-deferral|1|1|1
a-failure-alone-exits-1|2|0|1
EXITROWS

  # Non-vacuity, which is the row that keeps every row above from being decoration. `score_roster`
  # scores through pass()/defer(), so driving it here would tally rows this control did not assert;
  # the counters are snapshotted and restored around each call.
  local n0 d0
  n0="$(count_of "$CNTFILE")"; d0="$(count_of "$DEFERFILE")"
  score_roster 0 >"$out" 2>&1
  rc=$?
  nc_untally "$n0" "$d0"
  if [ "$rc" -eq 3 ] && grep -qF "VACUOUS-ROSTER" "$out" && ! grep -q '^PASS:' "$out"; then
    pass "a-zero-row-roster-is-a-DEFERRAL-and-never-a-PASS — VACUOUS-ROSTER, rc 3"
  else
    bad "a-zero-row-roster-is-a-DEFERRAL-and-never-a-PASS — score_roster 0 returned rc $rc and did not emit VACUOUS-ROSTER as a deferral. An empty subject set that scores green is the most convincing green in the repository"
    quote "$out"
  fi

  n0="$(count_of "$CNTFILE")"; d0="$(count_of "$DEFERFILE")"
  score_roster 4 >"$out" 2>&1
  rc=$?
  nc_untally "$n0" "$d0"
  if [ "$rc" -eq 0 ] && grep -qF "roster is non-empty" "$out"; then
    pass "a-populated-roster-is-a-PASS-so-the-non-vacuity-guard-is-two-sided — 4 pair(s), rc 0"
  else
    bad "a-populated-roster-is-a-PASS-so-the-non-vacuity-guard-is-two-sided — score_roster 4 returned rc $rc. A guard that only ever defers cannot tell an empty cluster from a full one"
    quote "$out"
  fi

  echo
  local got_n
  got_n="$(count_of "$CNTFILE")"
  EXPECTED_ASSERTIONS=31
  if [ "$got_n" -ne "$EXPECTED_ASSERTIONS" ]; then
    bad "the control scored $got_n row(s); $EXPECTED_ASSERTIONS were expected. A control that quietly stops running a mutation is the shape [[LSN-063]] is about, and it reports as a shorter green"
  fi

  echo "===================================================================="
  if [ "$(count_of "$FAILFILE")" -eq 0 ]; then
    echo "NEGATIVE CONTROL PASSED — $(count_of "$CNTFILE") row(s), each naming the rule it exercises"
    return 0
  fi
  echo "NEGATIVE CONTROL FAILED — $(count_of "$FAILFILE") of $(count_of "$CNTFILE") row(s)"
  return 1
}

if [ "$MODE" = negative-control ]; then
  run_negative_control
  exit $?
fi

# ------------------------------------------------------------------------------------------------
# LIVE RUN
# ------------------------------------------------------------------------------------------------
$K version >/dev/null 2>&1 || { echo "DEFERRED: context '$CTX' is not reachable."; exit 3; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/l2-lock.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/shipped-render.sh"

# P12, before the first read. Chains the cleanup trap installed above rather than replacing it.
l2_lock_guard "$CTX" "tier-egress-render-l2"

p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

if ! $K get crd "$AGENT_CRD" >/dev/null 2>&1; then
  echo "DEFERRED: $AGENT_CRD is not served on '$CTX'. There are no Agents to have namespaces, and"
  echo "  the tier enum arm 2 derives its subject set from lives in that CRD's schema. Both arms"
  echo "  would be measuring a cluster that has never had the product installed."
  exit 3
fi

# ------------------------------------------------------------------------------------------------
# THE DERIVATION. Read once, here, so both arms score the same cluster state.
# ------------------------------------------------------------------------------------------------
echo
echo "== deriving the subject sets from the cluster =="

# The CRD's own declared default for `.spec.tier`. Read rather than typed: `agentindex.EffectiveTier`
# and the CRD schema are two definition sites that already agree, and adding a third here is how
# they stop agreeing ([[LSN-036]]). If it cannot be read the tier stays empty and P11 refuses the
# row by name, which is a visible deferral rather than a guess.
TIER_DEFAULT="$($K get crd "$AGENT_CRD" \
  -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.tier.default}' 2>/dev/null)"
note "CRD-declared default for .spec.tier: '${TIER_DEFAULT:-<unreadable>}'"

$K get "$AGENT_CRD" -A \
  -o jsonpath="{range .items[*]}{.metadata.namespace}{\"$TAB\"}{.spec.tier}{\"\n\"}{end}" \
  >"$WORK/src.crs" 2>/dev/null || : >"$WORK/src.crs"
note "Agent CRs: $(grep -c . "$WORK/src.crs" || true) across the cluster"

# The label the operator actually stamps — `agentlabels.Tier`, on both Deployments and both pod
# templates, and the same label the per-tier egress policy selects on. Deployments AND pods, because
# a pair whose Deployment was removed while its pods drain is still a pair that needs DNS.
$K get deployments,pods -A -l kube-agents/tier \
  -o jsonpath="{range .items[*]}{.metadata.namespace}{\"$TAB\"}{.metadata.labels.kube-agents/tier}{\"\n\"}{end}" \
  >"$WORK/src.workloads" 2>/dev/null || : >"$WORK/src.workloads"
note "workloads carrying kube-agents/tier: $(grep -c . "$WORK/src.workloads" || true)"

cat "$WORK/src.crs" "$WORK/src.workloads" | roster_rows "$TIER_DEFAULT" >"$WORK/roster"
N_ROWS="$(count_of "$WORK/roster")"
while IFS="$TAB" read -r rns rtier; do
  note "roster: $rns — tier=${rtier:-<undetermined>}"
done <"$WORK/roster"

# Arm 2's tier set: the served CRD's enum, unioned with whatever the roster turned up. The enum keeps
# the arm non-vacuous on an empty cluster; the union keeps it honest if an Agent is ever stored with
# a tier the schema no longer serves.
{
  $K get crd "$AGENT_CRD" \
    -o jsonpath='{range .spec.versions[*]}{range .schema.openAPIV3Schema.properties.spec.properties.tier.enum[*]}{@}{"\n"}{end}{end}' 2>/dev/null
  cut -f2 "$WORK/roster"
} | grep . | sort -u >"$WORK/tiers"
N_TIERS="$(count_of "$WORK/tiers")"
note "tiers under test (served enum ∪ roster): $(tr '\n' ' ' <"$WORK/tiers")"

echo
echo "== arm 1 — LSN-068 item 3: every Agent namespace admits DNS to its tier =="
score_roster "$N_ROWS"

ARM1_SCORED=0
while IFS="$TAB" read -r ns tier; do
  [ -n "$ns" ] || continue
  ARM1_SCORED=$((ARM1_SCORED + 1))
  p11_assert_namespace_admits_dns "$K" "$ns" "$tier" >"$WORK/p11.out" 2>&1
  rc=$?
  case "$(verdict_for_p11 "$rc")" in
    ARM1-PASS)
      pass "$ns (tier=$tier) models an installed agent — a policy there admits DNS to its pods"
      quote "$WORK/p11.out"
      ;;
    ARM1-DEFERRED)
      defer "$ns (tier=${tier:-<undetermined>}) does not model an installed agent. The pair's"
      note "<agent>-to-broker policy is egress-only, so its reader is default-deny for DNS and the"
      note "run reads as a broker that never came up. This is COULD-NOT-RUN, not a security"
      note "failure: nothing here is misconfigured, the per-tier allowlist was never applied."
      quote "$WORK/p11.out"
      ;;
    *)
      bad "$ns (tier=$tier) — p11_assert_namespace_admits_dns returned rc $rc, which its own header does not document. The precondition did not answer the question"
      quote "$WORK/p11.out"
      ;;
  esac
done <"$WORK/roster"

if [ "$ARM1_SCORED" -ne "$N_ROWS" ]; then
  bad "arm 1 scored $ARM1_SCORED of $N_ROWS derived row(s). A loop that stops iterating reports as a shorter green ([[LSN-063]])"
fi

echo
echo "== arm 2 — LSN-069: rule 9 names an address a packet actually carries =="

EPS="$(shipped_render_apiserver_endpoint_addresses "$CTX" 2>/dev/null)"
if [ -n "$(printf '%s' "$EPS" | tr -d '[:space:]')" ]; then
  pass "endpoints/kubernetes in namespace default answers: $(printf '%s' "$EPS" | tr '\n' ' ')"
  note "This is the address a packet carries past a Dataplane-V2 DNAT, and therefore the address"
  note "rule 9 has to name. The Service ClusterIP and the kubeconfig host are the other two forms"
  note "resolve_apiserver_cidrs emits on purpose; neither is expected to appear here."
else
  defer "endpoints/kubernetes could not be read on '$CTX', so every rule-9 verdict below is"
  note "unscoreable rather than red. An empty endpoint list makes every rendered address score"
  note "NO-MATCH, which reads exactly like the LSN-069 defect and is a different finding."
fi

ARM2_SCORED=0
while IFS= read -r tier; do
  [ -n "$tier" ] || continue
  ARM2_SCORED=$((ARM2_SCORED + 1))

  # A roster namespace for this tier when one exists, the control namespace otherwise. Rule 9 does
  # not depend on the namespace; naming a real one keeps the rendered manifest something a human can
  # diff against what is actually installed. NOTHING IS APPLIED.
  rns="$(awk -F"$TAB" -v t="$tier" '$2 == t { print $1; exit }' "$WORK/roster")"
  : "${rns:=$CONTROL_NS}"

  shipped_render_tier_egress "$CTX" "$tier" "$rns" >"$WORK/manifest.out" 2>"$WORK/render.err"
  rc=$?
  case "$(verdict_for_render "$rc")" in
    ARM2-PASS)
      pass "tier=$tier (rendered for namespace $rns) — rule 9 names an endpoint address"
      grep -E 'R9-|rule 9 resolved to' "$WORK/render.err" | sed 's/^/      | /'
      ;;
    ARM2-DEFERRED)
      defer "tier=$tier (rendered for namespace $rns) — the rule-9 property could not be scored."
      quote "$WORK/render.err"
      ;;
    *)
      bad "tier=$tier (rendered for namespace $rns) — the shipped render's rule 9 does not name any address this cluster's packets carry (rc $rc). This closes TokenReview, the FleetFreeze read and the ActionRecord write for every kube-agents/tier pod in the namespace, and the symptom is an empty 'kubectl logs' and 'connection refused' ([[LSN-069]])"
      quote "$WORK/render.err"
      ;;
  esac
done <"$WORK/tiers"

if [ "$ARM2_SCORED" -ne "$N_TIERS" ]; then
  bad "arm 2 scored $ARM2_SCORED of $N_TIERS derived tier(s). A loop that stops iterating reports as a shorter green ([[LSN-063]])"
fi
if [ "$N_TIERS" -eq 0 ]; then
  bad "the tier set is empty, so arm 2 rendered nothing and asserted nothing. The served CRD's tier enum could not be read and the roster contributed no tier either ([[LSN-035]])"
fi

# Every derived subject produced exactly one verdict, plus the roster row and the endpoint row.
EXPECTED_ASSERTIONS=$((2 + N_ROWS + N_TIERS))
got="$(count_of "$CNTFILE")"
if [ "$got" -ne "$EXPECTED_ASSERTIONS" ]; then
  bad "$got verdict(s) were scored; $EXPECTED_ASSERTIONS were expected ($N_ROWS roster row(s) + $N_TIERS tier(s) + the roster and endpoint verdicts). An arm that stops rendering a verdict is an arm whose property nothing asserts, and the run still ends in a banner"
fi

echo
echo "===================================================================="
RC="$(overall_exit "$(count_of "$FAILFILE")" "$(count_of "$DEFERFILE")")"
case "$RC" in
  0)
    echo "tier-egress-render-l2 — PASSED. $(count_of "$CNTFILE") verdict(s):"
    echo "  arm 1 (LSN-068 item 3): $N_ROWS Agent namespace(s), every one admitting DNS to its tier."
    echo "  arm 2 (LSN-069): $N_TIERS tier(s) rendered through the shipped render_egress_policy, every"
    echo "    rule 9 naming an address endpoints/kubernetes confirms."
    echo "  NOT COVERED: that the dataplane DNATs before scoring egress. That is arm 2's premise and"
    echo "    only a traffic test can assert it; this file checks the render, not the packet."
    echo "  NOT COVERED: the NetworkPolicy objects actually installed. Arm 2 scores what the shipped"
    echo "    renderer produces today, not what is standing in the namespace — an install predating"
    echo "    rule 9 carries none of it and is green here. See the header; the live install is"
    echo "    exactly that case."
    ;;
  3)
    echo "tier-egress-render-l2 — DEFERRED. $(count_of "$DEFERFILE") of $(count_of "$CNTFILE")"
    echo "  verdict(s) could not be scored and none failed. A could-not-run is not a pass"
    echo "  ([[LSN-038]]); the blockers are named above."
    ;;
  *)
    echo "tier-egress-render-l2 — FAILED. $(count_of "$FAILFILE") of $(count_of "$CNTFILE") verdict(s)"
    echo "  ($(count_of "$DEFERFILE") additionally deferred)."
    ;;
esac
exit "$RC"
