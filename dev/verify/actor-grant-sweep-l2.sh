#!/usr/bin/env bash
# V-BRK-013 at L2, and Accept (e) of Phase 9 — "no agent identity in the fleet holds a write verb,
# verified by a full two-sided `auth can-i` sweep" (07 §2 acceptance (e); 09 §6.14 V-BRK-013, levels
# L0 and L2, BLOCKING-ALWAYS).
#
# THE L0 HALF IS GREEN AND IS A REAL RESULT, AND IT CANNOT REACH THIS.
# `dev/tests/actor-grant-single-sourced.py` proves that the broker-operations grant has ONE
# definition site (06 §2.2.1), that all three VAP copies compile exactly it, that every RBAC object
# labelled `kube-agents/role: actor` stays inside its tier's ceiling, and that per tier the union of
# those objects equals the profile the install is supposed to have rendered. Seven properties, and
# every one of them is a statement about FILES.
#
# Three things follow that only a cluster can answer:
#
#   1. AN RBAC OBJECT THAT OMITS THE LABEL IS INVISIBLE TO THE L0 HALF. Its discovery key is
#      `kube-agents/role: actor` — it has to be, because that is also `vap-agent-readonly`'s own
#      selector (`variables.isActor`), and the check asserts the tree against the policy that
#      governs it. A Role granting `update actionrecords` to an actor ServiceAccount and carrying no
#      such label is outside the check's world, outside the VAP's actor arm, and fully effective:
#      RBAC is a union, so the extra rule silently grants. The authorizer is the only artifact that
#      sees it. This suite injects exactly that object as its negative control.
#   2. A RULE THAT WAS NEVER APPLIED LOOKS IDENTICAL TO ONE THAT WAS. The L0 half reads the tree;
#      the tree is not the cluster. `broker-per-agent-l2.sh`'s header records the version of this
#      that already happened — the L0 half of V-BRK-012 was green for weeks while the deployed fleet
#      had no brokers at all.
#   3. RBAC IS A UNION ACROSS EVERY BINDING, and no file contains the union. What an identity may do
#      is a function of every ClusterRoleBinding and RoleBinding naming it, including ones no
#      template owns — `BACKLOG.md` B-007 is a live example on this very cluster. Reading each
#      object and finding it correct says nothing about their sum, which is the only thing that
#      authorizes a request.
#
# TWO-SIDED, OR IT IS NOT A SWEEP. `verify-phase9.sh` section F says this in its own failure text:
# "every agent identity must answer no, and the broker's identity must answer yes, or a fleet whose
# RBAC failed to apply at all would pass the negative half perfectly". So every question comes in
# both directions out of one derivation:
#
#   * the POSITIVE half — 06 §2.2.1's grant and the READ half of each tier's 06 §2.2 template must
#     be held by the actor identity that is owed them. A `no` here is not a safe failure: 06 §2.2.1
#     says a tier that cannot read `fleetfreezes` fails closed permanently (06 §4.4), so the
#     missing-grant direction BRICKS a tier rather than protecting it.
#   * the NEGATIVE half — the WRITE half of the tier template, the append-only verbs on the record
#     itself, and the same verbs asked in a namespace the identity has no agent in. 07 §2's Phase 9
#     acceptance is that the whole safety machinery runs "with no write authority anywhere", and
#     this is where that stops being an intention and becomes a measurement.
#
# THE QUESTIONS ARE DERIVED, NOT LISTED. `phase-9.md` binds Accept (e) to V-BRK-013 and requires
# that "the sweep script asserts the exclusion set BY NAME rather than by 'these are the ones that
# were there when I wrote it'". `dev/verify/fixtures/actor_grant_expectations.py` computes the table
# by importing the L0 check's own parser of 06 — so a verb added to the spec is a verb this sweep
# asks about on its next run, and a table that derives nothing fails its own `--self-test`.
#
# WHAT IS ASSERTED, in order:
#   S-0  P1: the CONTROLLER is the build under test. It is what publishes
#        `status.broker.actorServiceAccount`, which is how the identities under test are named.
#   S-1  DISCOVERY, and its completeness. Identities are found by label across all namespaces, and
#        then cross-checked against what the live Agent CRs DECLARE — every actor SA a CR publishes
#        and every reader SA a CR references must appear in the discovered set. Without that arm,
#        an identity that lost its label is an identity the sweep silently does not sweep, which is
#        the same failure mode as (1) above wearing the sweep's own clothes.
#   S-2  THE SWEEP. Every derived question asked of the live authorizer with `kubectl auth can-i
#        --as=<subject>`, one row per question, into a transcript.
#   S-3  THE ASSERTION BLOCK, `actor_grant_sweep_assert.py` — seven arms A-1…A-7 over that
#        transcript: coverage, no unanswered questions, the positive half, the negative half,
#        append-only by name, the per-tier freeze read, and reader non-vacuity.
#   S-4  THE `¬` (09 §6 marks V-BRK-013 mandatory-negative-control), ON THE CLUSTER. An unlabelled
#        Role granting `update actionrecords` and `create deployments` is bound to a real actor
#        ServiceAccount; the affected questions are re-asked; the answers are spliced into the real
#        transcript and the assertion block MUST go red on A-4 and A-5 by name. Then the binding is
#        removed and the same questions must answer `no` again — both directions, because a control
#        that only proves the arm can fail leaves the cluster's final state unproven, and the PROVEN
#        verdict above is a statement about that state.
#
# WHAT THIS SUITE DOES NOT CLAIM.
#   * It does not ask about `resources: ["*"]` rows. `kubectl auth can-i <verb> '*'` is a question
#     about a wildcard REQUEST, which RBAC does not answer the way the rule means it. Those rows are
#     counted and PRINTED, never dropped in silence.
#   * It does not ask about resource types the API server does not serve. On a cluster without
#     Config Connector, `get containernodepools.container.cnrm.cloud.google.com` answers `no`
#     because the type is unknown, not because the grant is absent — and that `no` would satisfy a
#     negative row for a reason that has nothing to do with authority. Both directions are excluded
#     together, counted, and printed.
#   * It says nothing about what the agent PROCESS does with the authority it has. That is the
#     broker's pipeline, and it is `broker-execute-l2.sh` and `broker-gate-l2.sh`.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies three
# Agent CRs, re-renders their identities, and TEMPORARILY BINDS A WRITE GRANT to a real actor
# ServiceAccount as its negative control. On the live install that last step is a test handing an
# agent write authority.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target, or the cluster cannot run the experiment ·
#       3 = DEFERRED (P1 unverifiable).
# Usage: dev/verify/actor-grant-sweep-l2.sh [kube-context]
#        dev/verify/actor-grant-sweep-l2.sh --negative-control
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager, asserted with
#      p1_assert_build_under_test. The controller is what names the actor identities this suite
#      sweeps — `status.broker.actorServiceAccount` is its output — so a stale controller means the
#      sweep is about a previous generation's identities. No broker pod is involved: this suite
#      never speaks to a broker, it speaks to the API server's authorizer.
#   P3 admission-recreate: all three Agent CRs are deleted and re-applied on every run
#      (`seed_parent_agent` deletes before it applies; the EXIT trap deletes after), so the
#      identities under test are rendered by the controller running NOW. The decoy Role and its
#      RoleBinding are likewise created and destroyed inside the run.
#   P6 runtime-authoritative: every answer comes from `kubectl auth can-i`, which is the cluster's
#      own authorizer evaluating the union of every binding — never from reading an RBAC file, which
#      is what the L0 half already does and precisely what this level exists not to repeat. The
#      identities are read off live Agent CRs and live ServiceAccounts.
#   P9 status-polled: `status.broker.actorServiceAccount` is polled, never slept on, inside
#      `seed_agent_identity`.
#   P10 substrate: p10_assert_control_plane_healthy before anything is seeded.
set -uo pipefail

# MODES. `live` sweeps a real cluster and is what every claim above is about. `--negative-control`
# replays the assertion block against synthesised transcripts and requires each to go red by name.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
# A namespace no agent has a scope in, for the `elsewhere` rows. kube-system rather than one this
# suite creates, for `broker-per-agent-l2.sh`'s reason: the question is whether the grant is
# cluster-wide, and a namespace the RoleBinding could not have named either way does not ask it.
ELSEWHERE_NS="kube-system"

PLATFORM_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml
CLUSTER_ADMIN_MANIFEST=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
DEVELOPER_MANIFEST=examples/gitops-repo/clusters/cluster-a/namespaces/team-x/60-developer-team-agent.yaml

EXPECT=dev/verify/fixtures/actor_grant_expectations.py
ASSERT=dev/verify/fixtures/actor_grant_sweep_assert.py

DECOY_ROLE=actor-grant-sweep-l2-decoy
DECOY_BINDING=actor-grant-sweep-l2-decoy

# How many `auth can-i` calls are in flight at once. Each is one API round trip and the table is
# ~700 questions; serial is minutes of wall clock for no reason. Bounded rather than unbounded
# because the API server is shared with everything else running on this cluster.
PARALLEL="${SWEEP_PARALLEL:-10}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/actor-grant-sweep.XXXXXX")"
TRANSCRIPT="$WORK/transcript.tsv"
IDENTITIES="$WORK/identities.txt"

# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060.) `--negative-control` SYNTHESISES the transcript and
# hands it to the assertion block, so everything upstream of the block is unmeasured by it:
#   - the derivation itself. The control builds its baseline from the real question table, so a
#     table that derived the WRONG questions would produce a baseline the arms accept. That property
#     has its own control: `actor_grant_expectations.py --self-test`, a separate L0 chain line.
#   - the `kubectl auth can-i` invocation — the flag shapes, `--subresource`, `--as`, the namespace
#     choice. A malformed query answers nothing, and it is A-2's job to notice, but the control
#     never runs one. ONE PART OF IT IS EXERCISED: the last case fires `resource_word` directly, so
#     the `*/*)` refusal is known to trigger rather than merely known to be written. Everything else
#     about the invocation is still live-only.
#   - IDENTITY DISCOVERY (S-1) and its completeness cross-check against the Agent CRs. The control
#     is handed an identity list; it never derives one from a cluster.
#   - the served-resource filter, which decides which rows are asked at all.
#   - P1, the fixtures, and the seeding of the three tiers' identities.
# The on-cluster arm S-4 is what covers the first three of those, and it is in the LIVE mode only —
# a decoy grant is a statement about an authorizer, and there is no authorizer at L0.
# What the control proves, and all it proves: the seven arms are not always-green, and each of the
# ten defects below is caught by the arm that targets it, named in the output.

# Non-empty lines, on stdin or in a file, ALWAYS exiting 0. Not `grep -c .`: grep exits 1 on zero
# matches, so the idiomatic `$(grep -c . f || echo 0)` prints "0" from grep AND "0" from the
# fallback, and the two-line string it yields turns every downstream `[ "$n" -ge 6 ]` into
# "integer expression expected" — a count that is wrong in a way that reads as a broken script
# rather than as a wrong number. GNU/BSD alike ([[LSN-029]]).
count() { awk 'NF{n++} END{print n+0}' "$@"; }

fail=0
# EVERY ARM IS COUNTED AND THE COUNT IS ASSERTED AT THE END. `fail` stays 0 when no assertion runs,
# so a suite that skipped its whole body would print a PROVEN banner. Change EXPECTED_ASSERTIONS
# deliberately, in the same commit as the arm.
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}

# P1 + S-1 (2 arms: label discovery non-vacuity, CR-declared completeness) + A-1…A-7 (7) +
# S-4 (3: the control went red, it named the right arms, the revoke put it back) = 13.
EXPECTED_ASSERTIONS=13

cd "$REPO_ROOT" || exit 1

# ------------------------------------------------------------------------------------------------
# score_transcript <transcript> <identities> — the assertion block, one PASS/FAIL per arm.
#
# A wrapper rather than a copy: `--negative-control` calls this same function with synthesised
# transcripts, so the arms it exercises are the arms the live run uses. Every line the analyzer
# prints is folded into this suite's own counters, which is what makes the ¬ mode's arm count
# comparable to the live one's.
# ------------------------------------------------------------------------------------------------
score_transcript() {
  local transcript="$1" identities="$2" line rc
  local out
  out="$(python3 "$ASSERT" "$transcript" "$identities" 2>&1)"
  rc=$?
  while IFS= read -r line; do
    case "$line" in
      PASS:*) pass "${line#PASS: }" ;;
      FAIL:*) bad "${line#FAIL: }" ;;
      *) [ -n "$line" ] && echo "  $line" ;;
    esac
  done <<<"$out"
  return $rc
}

# ------------------------------------------------------------------------------------------------
# resource_word <resource> <group> — the word that goes in `auth can-i`'s positional slot, or the
# refusal `malformed`.
#
# THE RUNTIME HALF OF [[LSN-044]], and it is not decoration here. The word is COMPUTED from a table
# this script derives from 06 §2.2/§2.2.1, so no static scan can prove it never contains a slash —
# `dev/tests/cluster-check-hygiene.py` property 1b demands a `*/*)` arm for exactly that reason:
# 1a's ban on a literal slash is evaded by the ordinary refactor of hoisting the query into a
# helper, which is precisely the shape of `ask_one`. This suite is where the evasion would cost the
# most: 434 of its 647 questions are NEGATIVE, so a resource word nobody was ever granted answers a
# confident `no` and the withheld-verb arm goes green having asked about an object NAMED `status`.
# Refusing is the only safe answer. `malformed` is outside the analyzer's closed alphabet for the
# answer column, so a refused row cannot be scored as a refusal by the authorizer.
#
# A named function rather than an inline `case` so that `--negative-control` can fire it without a
# cluster — a guard nothing ever triggers is indistinguishable from a guard whose pattern is wrong.
# ------------------------------------------------------------------------------------------------
resource_word() {
  local res="$1"
  [ -n "$2" ] && res="$1.$2"
  case "$res" in
    */*)
      echo "  suite bug: the resource word '$res' contains a slash. kubectl parses a positional" >&2
      echo "  TYPE/NAME, not TYPE/SUBRESOURCE — the subresource belongs in the table's own column," >&2
      echo "  which ask_one passes as --subresource=. Refusing to ask (LSN-044)." >&2
      printf 'malformed'
      return
      ;;
  esac
  printf '%s' "$res"
}

# ================================================================================================
# NEGATIVE CONTROL — no cluster, no authorizer, ten synthesised defects
# ================================================================================================
if [ "$MODE" = negative-control ]; then
  echo "== actor-grant-sweep-l2.sh --negative-control: can the assertion block tell a correct sweep from a broken one? =="

  if ! python3 "$EXPECT" --table >"$WORK/table.tsv" 2>"$WORK/table.err"; then
    echo "FAIL: the question table would not derive: $(cat "$WORK/table.err")" >&2
    rm -rf "$WORK"
    exit 1
  fi

  # The baseline: the REAL derived table, given a plausible identity per (tier, role) and answered
  # exactly as 06 says it must be. Synthesised — see the block above for what that does not measure.
  python3 - "$WORK/table.tsv" "$WORK/baseline.tsv" "$IDENTITIES" <<'PY'
import sys
table, out, idents = sys.argv[1], sys.argv[2], sys.argv[3]
NS = {"developer-team": "team-x"}
rows, seen = [], []
for line in open(table):
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    tier, role, kind, group, resource, sub, verb, where, expected = line.split("\t")
    ns = NS.get(tier, "kubeagents-system")
    sa = f"{tier}-fixture-{role}"
    ident = f"{ns}/{sa}/{tier}/{role}"
    if ident not in seen:
        seen.append(ident)
    rows.append("\t".join((ns, sa, tier, role, kind, group, resource, sub, verb, where, expected, expected)))
open(out, "w").write("\n".join(rows) + "\n")
open(idents, "w").write("\n".join(seen) + "\n")
PY

  # Each case: a name, whether the block must stay green or go red, the needle a red must mention,
  # and a python mutation applied to a copy of the baseline. The needle is the arm's own vocabulary,
  # so a case that goes red for a DIFFERENT reason scores as a MISS rather than as a catch.
  total=0
  caught=0
  rc=0
  while IFS='|' read -r name expect needle mutation; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    cp "$WORK/baseline.tsv" "$WORK/case.tsv"
    if ! python3 - "$WORK/case.tsv" "$mutation" <<'PY'
import sys
path, mutation = sys.argv[1], sys.argv[2]
rows = [l.rstrip("\n").split("\t") for l in open(path) if l.strip()]
F = "ns sa tier role kind group resource subresource verb where expected answer".split()
I = {n: i for i, n in enumerate(F)}


def first(pred):
    for r in rows:
        if pred(r):
            return r
    raise SystemExit(f"the mutation '{mutation}' matched no row; it would measure nothing")


if mutation == "none":
    pass
elif mutation == "empty":
    rows = []
elif mutation == "truncate":
    rows = rows[:40]
elif mutation == "drop-identity":
    victim = rows[-1][I["sa"]]
    rows = [r for r in rows if r[I["sa"]] != victim]
elif mutation == "blank-answer":
    first(lambda r: r[I["kind"]] == "dark-write")[I["answer"]] = ""
elif mutation == "lose-journal-write":
    first(lambda r: r[I["kind"]] == "journal-write")[I["answer"]] = "no"
elif mutation == "lose-tier-read":
    first(lambda r: r[I["kind"]] == "tier-read")[I["answer"]] = "no"
elif mutation == "gain-dark-write":
    first(lambda r: r[I["kind"]] == "dark-write")[I["answer"]] = "yes"
elif mutation == "gain-elsewhere":
    first(lambda r: r[I["kind"]] == "elsewhere-write")[I["answer"]] = "yes"
elif mutation == "gain-append":
    first(lambda r: r[I["kind"]] == "append-only" and r[I["verb"]] == "update")[I["answer"]] = "yes"
elif mutation == "lose-freeze":
    for r in rows:
        if r[I["kind"]] == "freeze-read" and r[I["tier"]] == "developer-team":
            r[I["answer"]] = "no"
elif mutation == "mute-reader":
    victim = first(lambda r: r[I["role"]] == "reader")[I["sa"]]
    for r in rows:
        if r[I["sa"]] == victim and r[I["kind"]] == "reader-read":
            r[I["answer"]] = "no"
elif mutation == "everything-denied":
    for r in rows:
        r[I["answer"]] = "no"
elif mutation == "reader-writes":
    first(lambda r: r[I["kind"]] == "reader-no-write")[I["answer"]] = "yes"
elif mutation == "shift-fields":
    # Exactly what `IFS=$'\t' read` did to every row with an empty subresource on the first live
    # run: the empty column vanishes, the rest slide left, the row still has twelve columns.
    r = first(lambda r: r[I["subresource"]] == "")
    del r[I["subresource"]]
    r.append("")
else:
    raise SystemExit(f"unknown mutation '{mutation}'")
open(path, "w").write("".join("\t".join(r) + "\n" for r in rows))
PY
    then
      # A mutation that did not apply leaves the BASELINE in place, and a baseline is green — the
      # case would score as "the arm did not fire" when in truth the fault was never staged
      # ([[LSN-048]]: an unevaluated mutant read as a survivor).
      echo "  MISS $name — the mutation itself failed to apply, so nothing was measured (LSN-048)"
      rc=1
      continue
    fi
    out="$(python3 "$ASSERT" "$WORK/case.tsv" "$IDENTITIES" 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — a correct sweep passes, so the arms below are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT sweep was failed $n_fail time(s); every case below would be caught for the wrong reason"
        printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
        rc=1
      fi
    elif printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
      echo "  ok   $name — caught by the arm that targets it ('$needle')"
      caught=$((caught + 1))
    else
      echo "  MISS $name — went red $n_fail time(s) but no FAIL line mentions '$needle', so the property it targets is not what caught it"
      printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
      rc=1
    fi
  done <<'CASES'
baseline|green|-|none
empty-transcript|red|the sweep did not sweep|empty
truncated-transcript|red|below the floor|truncate
identity-never-asked|red|were asked nothing|drop-identity
question-errored|red|A-2 unanswered|blank-answer
journal-append-lost|red|A-3 positive half|lose-journal-write
tier-read-lost|red|A-3 positive half|lose-tier-read
dark-write-held|red|A-4 negative half|gain-dark-write
grant-is-cluster-wide|red|A-4 negative half|gain-elsewhere
record-is-mutable|red|A-5 append-only|gain-append
developer-team-cannot-read-freezes|red|A-6 freeze read|lose-freeze
reader-bound-to-nothing|red|A-7 reader non-vacuity|mute-reader
reader-holds-a-write|red|A-4 negative half|reader-writes
rbac-never-applied|red|A-3 positive half|everything-denied
field-shifted-parse|red|field-shifted parse|shift-fields
CASES

  # The sixteenth case does not mutate a transcript, because the defect it stages happens one layer
  # earlier — in the word this suite hands to `auth can-i`, before there is a transcript to mutate.
  # It is the only case that exercises a function the LIVE path calls, and it is here because a
  # `*/*)` arm that never fires reads exactly like one whose pattern is wrong.
  total=$((total + 1))
  if [ "$(resource_word "actionrecords/status" "kubeagents.x-k8s.io" 2>/dev/null)" = malformed ] &&
    [ "$(resource_word "actionrecords" "kubeagents.x-k8s.io" 2>/dev/null)" = "actionrecords.kubeagents.x-k8s.io" ]; then
    echo "  ok   slashed-resource-word — the guard refuses 'actionrecords/status' and still builds the ordinary word (LSN-044)"
    caught=$((caught + 1))
  else
    echo "  MISS slashed-resource-word — resource_word did not refuse a slashed word, or refused a good one;"
    echo "       the live sweep's 434 negative questions could be answered 'no' about an object NAMED 'status' (LSN-044)"
    rc=1
  fi

  echo
  rm -rf "$WORK"
  if [ "$rc" -eq 0 ]; then
    echo "NEGATIVE CONTROL: $caught/$total — every synthesised defect was caught by the arm that targets it."
    exit 0
  fi
  echo "NEGATIVE CONTROL: $caught/$total — see the MISS lines above."
  exit 1
fi

# ================================================================================================
# LIVE
# ================================================================================================
# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (destructive-test guard)." >&2
    echo "  This DELETES and re-applies three Agent CRs, and TEMPORARILY BINDS a write grant to a" >&2
    echo "  real actor ServiceAccount as its negative control. On the live install that is a test" >&2
    echo "  handing an agent the write authority Phase 9 exists to withhold. Name the dev cluster:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    rm -rf "$WORK"
    exit 2
    ;;
esac

echo "===================================================================="
echo " V-BRK-013 at L2 / Accept (e) — two-sided auth can-i sweep — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  rm -rf "$WORK"
  exit 1
}

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/parent-chain.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || { rm -rf "$WORK"; exit 2; }

seeded=()
DECOY_APPLIED=no
cleanup() {
  # The decoy FIRST and unconditionally. Everything else here is tidying; this one is repair. A run
  # killed between the bind and the revoke leaves a real actor ServiceAccount holding
  # `update actionrecords` and `create deployments` — the exact authority this phase exists to
  # withhold, left behind by the test that proves it is withheld.
  if [ "$DECOY_APPLIED" = yes ]; then
    $K -n "$NS" delete rolebinding "$DECOY_BINDING" --ignore-not-found >/dev/null 2>&1
    $K -n "$NS" delete role "$DECOY_ROLE" --ignore-not-found >/dev/null 2>&1
    echo "  the decoy write grant is revoked"
  fi
  unseed_parent_agents "$K" "${seeded[@]:-}"
  rm -rf "$WORK"
  echo
  echo "CLEANED UP: the decoy Role and RoleBinding are deleted and the three Agent CRs are gone."
  echo "  The actor and reader ServiceAccounts and the rendered grants are LEFT — a real install"
  echo "  creates those once per namespace and they outlive every CR in it; deleting them would"
  echo "  silently change what the next suite in the chain is running against."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# S-0: the build under test
# ------------------------------------------------------------------------------------------------
echo; echo "== S-0: the controller is the build under test =="
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable for the controller (see above). The controller publishes the"
     echo "  actor ServiceAccount names this suite sweeps, so nothing below would be evidence about"
     echo "  this commit."
     exit 3 ;;
  *) bad "P1: the controller is not running the build under test"; exit 1 ;;
esac

# ------------------------------------------------------------------------------------------------
# Fixtures: one Agent CR per tier, and the identity each one's pair runs as
# ------------------------------------------------------------------------------------------------
echo; echo "== fixtures: three shipped Agent CRs, one per tier, and their rendered identities =="

# Order matters: the cluster-admin CR's parentRef names the platform agent and the developer-team
# CR's names the cluster-admin one, and 06 §1.2 V-6 rejects a child whose parent does not exist.
# All three tiers, because V-BRK-013's `fleetfreezes` clause is explicitly "every tier, including
# developer-team" — a two-tier fixture cannot fail the clause the spec singles out.
for m in "$PLATFORM_MANIFEST" "$CLUSTER_ADMIN_MANIFEST" "$DEVELOPER_MANIFEST"; do
  if ref="$(seed_parent_agent "$K" "$m")"; then
    seeded+=("$ref")
    echo "  seeded $ref from $m"
  else
    echo "FAIL: could not seed $m: $ref" >&2
    exit 1
  fi
done

for ref in "${seeded[@]}"; do
  ns="${ref%%/*}"
  name="${ref#*/}"
  seed_agent_identity "$K" "$ns" "$name" || {
    echo "FAIL: could not seed the actor identity for $ref" >&2
    exit 1
  }
done

# AND THE READER'S READ GRANT, which `seed_agent_identity` does not render. `apply_agent_identity`
# creates both ServiceAccounts and binds the ACTOR to 06 §2.2.1's grant and the tier's read profile;
# the reader's own `<tier>-agent-explorer` is a separate object, shipped in the GitOps overlay and
# applied by provisioning step 12 rather than by the controller (08 §4: the controller never mints
# RBAC).
#
# SEEDED HERE RATHER THAN ASSUMED, and the first live run is why. Two of the three explorer grants
# were on this cluster — applied on 2026-07-26 from clusters/cluster-a — and the platform one was
# not, because it lives only under policy/rbac-overlay/ which that path does not apply. A-7 caught
# it: the platform reader was bound to nothing, so all ~60 of its `reader-no-write` rows had passed
# for free. That is the arm working, but it is a finding about one cluster's install history, and a
# sweep whose answer depends on which directory somebody applied in July is not measuring the tree.
# Applying the shipped overlay makes the run a statement about what the repository ships.
#
# ONLY THE `-explorer` DOCUMENTS. The same files carry the actor ServiceAccount with a literal
# `PROJECT_ID` in its name, substituted per project by CI/CD; applying the whole file would create a
# ServiceAccount named `platform-PROJECT_ID-actor` and leave it on the cluster. The explorer docs
# carry no placeholders.
for tier in platform cluster-admin developer-team; do
  overlay="examples/gitops-repo/policy/rbac-overlay/$tier.yaml"
  if [ ! -f "$overlay" ]; then
    echo "FAIL: the shipped read overlay $overlay is missing; the reader half cannot be seeded" >&2
    exit 1
  fi
  if awk -v RS='---\n' '/name: [a-z-]+-explorer/ {print "---"; print}' "$overlay" | $K apply -f - >/dev/null 2>&1; then
    echo "  fixtures: the $tier reader's '$tier-agent-explorer' read grant applied from $overlay"
  else
    echo "FAIL: could not apply the $tier reader's explorer grant from $overlay" >&2
    exit 1
  fi
done

# ------------------------------------------------------------------------------------------------
# S-1: discovery, and whether it found everything
# ------------------------------------------------------------------------------------------------
echo; echo "== S-1: every agent identity in the fleet, discovered by label and cross-checked =="

# By LABEL and across ALL namespaces, because "every agent identity in the fleet" is the claim. A
# list of names would sweep the fleet this suite seeded rather than the fleet that is there.
$K get sa -A -o json 2>/dev/null | python3 -c "$(
  cat <<'PY'
import json, sys
for i in json.load(sys.stdin)["items"]:
    m = i["metadata"]
    labels = m.get("labels") or {}
    role = labels.get("kube-agents/role")
    tier = labels.get("kube-agents/tier")
    if role in ("actor", "reader") and tier:
        print("%s/%s/%s/%s" % (m["namespace"], m["name"], tier, role))
PY
)" | sort -u >"$IDENTITIES"

n_ident="$(count "$IDENTITIES")"
n_tiers="$(cut -d/ -f3 <"$IDENTITIES" | sort -u | count)"
n_actors="$(grep -c "/actor$" "$IDENTITIES" || true)"
n_readers="$(grep -c "/reader$" "$IDENTITIES" || true)"
if [ "$n_ident" -ge 6 ] && [ "$n_tiers" -ge 3 ] && [ "$n_actors" -ge 3 ] && [ "$n_readers" -ge 3 ]; then
  pass "S-1a: $n_ident agent identities discovered by label across $n_tiers tiers ($n_actors actor, $n_readers reader)"
else
  bad "S-1a: only $n_ident labelled agent identities across $n_tiers tier(s) ($n_actors actor, $n_readers reader). Three tiers with a pair each is the fixture this suite just seeded, so a smaller number means discovery is not finding what is there — and every arm below sweeps whatever it found"
fi

# COMPLETENESS. The Agent CRs DECLARE their identities: `status.broker.actorServiceAccount` is the
# name the broker pod binds to, and `spec.security.serviceAccountName` is the reader the agent pod
# runs as. Both must be in the discovered set. An identity whose label was dropped is invisible to
# the sweep AND to `vap-agent-readonly`'s actor arm at the same time — the label is the discovery
# key for both — so a sweep that trusted its own label query would go quiet exactly when the object
# it should be shouting about lost the thing that makes it findable.
$K get agents -A -o json 2>/dev/null | python3 -c '
import json, sys
for i in json.load(sys.stdin)["items"]:
    ns = i["metadata"]["namespace"]
    actor = ((i.get("status") or {}).get("broker") or {}).get("actorServiceAccount")
    reader = ((i.get("spec") or {}).get("security") or {}).get("serviceAccountName")
    for sa in (actor, reader):
        if sa:
            print(f"{ns}/{sa}")
' | sort -u >"$WORK/declared.txt"

missing=""
while IFS= read -r decl; do
  [ -n "$decl" ] || continue
  if ! cut -d/ -f1,2 <"$IDENTITIES" | grep -qxF "$decl"; then
    missing="$missing $decl"
  fi
done <"$WORK/declared.txt"
n_declared="$(count "$WORK/declared.txt")"
if [ -z "$missing" ] && [ "$n_declared" -ge 6 ]; then
  pass "S-1b: all $n_declared identities the live Agent CRs declare are in the discovered set"
elif [ "$n_declared" -lt 6 ]; then
  bad "S-1b: the live Agent CRs declare only $n_declared identities, so the completeness cross-check has almost nothing to check against. The controller has not published status.broker.actorServiceAccount for the CRs this suite seeded"
else
  bad "S-1b: the Agent CRs declare identities the label sweep did not find:$missing. Those identities exist, are bound, and are being authorized — and they are invisible both to this sweep and to vap-agent-readonly's actor arm, which selects on the same label"
fi

# ------------------------------------------------------------------------------------------------
# S-2: the sweep
# ------------------------------------------------------------------------------------------------
echo; echo "== S-2: asking the live authorizer every question 06 derives =="

if ! python3 "$EXPECT" --table >"$WORK/table.tsv" 2>"$WORK/table.err"; then
  bad "S-2: the question table would not derive from 06: $(cat "$WORK/table.err")"
  echo "V-BRK-013 at L2: FAILED — there were no questions to ask." >&2
  exit 1
fi
skipped_wildcard="$(awk -F'\t' '$1=="#skipped"{print $2}' "$WORK/table.tsv")"

# The resource types the API server actually serves, as `resource[.group]` — the same key the table
# uses. A row naming a type this cluster does not have answers `no` for a reason that is not about
# authority, and that `no` would satisfy a negative row vacuously ([[LSN-035]]). Both directions are
# excluded together.
$K api-resources -o name 2>/dev/null | sort -u >"$WORK/served.txt"
if [ ! -s "$WORK/served.txt" ]; then
  echo "DEFERRED: the API server returned no resource types, so nothing can be filtered or asked." >&2
  exit 2
fi

# One query line per (identity, table row for its tier and role).
: >"$WORK/queries.tsv"
python3 - "$WORK/table.tsv" "$IDENTITIES" "$WORK/served.txt" "$ELSEWHERE_NS" "$WORK/queries.tsv" "$WORK/unservable.txt" <<'PY'
import collections, sys
table, idents, served, elsewhere_ns, out, unservable = sys.argv[1:7]
served_set = {l.strip() for l in open(served) if l.strip()}
rows = []
for line in open(table):
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    rows.append(line.split("\t"))
identities = [l.strip().split("/") for l in open(idents) if l.strip()]
dropped = collections.Counter()
with open(out, "w") as fh:
    for ns, sa, tier, role in identities:
        for tier_r, role_r, kind, group, resource, sub, verb, where, expected in rows:
            if tier_r != tier or role_r != role:
                continue
            key = f"{resource}.{group}" if group else resource
            if key not in served_set:
                dropped[key] += 1
                continue
            qns = elsewhere_ns if where == "elsewhere" else ns
            fh.write("\t".join((ns, sa, tier, role, kind, group, resource, sub, verb, where, expected, qns)) + "\n")
with open(unservable, "w") as fh:
    for key, n in sorted(dropped.items()):
        fh.write(f"{key}\t{n}\n")
PY

n_q="$(count "$WORK/queries.tsv")"
n_unservable="$(awk -F'\t' '{s+=$2} END{print s+0}' "$WORK/unservable.txt")"
echo "  $n_q question(s) to ask; $skipped_wildcard wildcard row(s) not asked (a wildcard REQUEST is"
echo "  not the question the rule makes); $n_unservable row(s) name resource types this API server"
echo "  does not serve and are excluded in BOTH directions:"
if [ -s "$WORK/unservable.txt" ]; then
  sed 's/^/    /' "$WORK/unservable.txt" | head -20
  [ "$(count "$WORK/unservable.txt")" -gt 20 ] && echo "    … $(($(count "$WORK/unservable.txt") - 20)) more"
else
  echo "    (none)"
fi

# ask_one <query-line> -> the transcript row, answer appended.
# `--subresource=` and never a positional `resource/subresource` ([[LSN-044]]: kubectl reads the
# slashed form as a resource NAME, answers about a resource that does not exist, and says `no`).
#
# SPLIT ON \x1f, NOT ON THE TAB THE FILE IS DELIMITED WITH. A tab is an IFS *whitespace* character,
# so `IFS=$'\t' read` collapses runs of tabs into one delimiter and drops leading and trailing ones
# — the `subresource` column is empty on all but four rows, so every one of those parsed shifted by
# a field: `sub` took the verb, `verb` took `where`, and `qns` took nothing, producing
# `auth can-i own fleetfreezes --subresource=get -n ''`. That answers a clean `no`, and a `no` is a
# perfectly ordinary thing for this suite to record. It cost a full live run to find, because the
# only visible symptom was three tiers apparently unable to read `fleetfreezes` — a finding, in the
# shape of a defect. \x1f is not IFS whitespace, so empty fields survive.
#
# `resource_word` (above) refuses a computed word carrying a slash rather than asking about an
# object NAMED `status` and recording the meaningless `no` that comes back ([[LSN-044]]).
ask_one() {
  local ns sa tier role kind group resource sub verb where expected qns res answer
  IFS=$'\x1f' read -r ns sa tier role kind group resource sub verb where expected qns \
    <<<"$(printf '%s' "$1" | tr '\t' '\037')"
  res="$(resource_word "$resource" "$group")"
  if [ "$res" = malformed ]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$ns" "$sa" "$tier" "$role" "$kind" "$group" "$resource" "$sub" "$verb" "$where" \
      "$expected" "malformed"
    return
  fi
  if [ -n "$sub" ]; then
    answer="$($K auth can-i "$verb" "$res" --subresource="$sub" -n "$qns" \
      --as="system:serviceaccount:$ns:$sa" 2>/dev/null | tail -1)"
  else
    answer="$($K auth can-i "$verb" "$res" -n "$qns" \
      --as="system:serviceaccount:$ns:$sa" 2>/dev/null | tail -1)"
  fi
  case "$answer" in
    yes | no) : ;;
    *) answer="" ;;
  esac
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$ns" "$sa" "$tier" "$role" "$kind" "$group" "$resource" "$sub" "$verb" "$where" "$expected" "$answer"
}

# Bounded fan-out in plain bash 3.2 — no `wait -n`, which is bash 4. Each worker writes its own
# file and the parts are concatenated in order, so a slow answer cannot interleave into another
# row.
i=0
running=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  i=$((i + 1))
  ask_one "$line" >"$WORK/part.$(printf '%05d' "$i")" &
  running=$((running + 1))
  if [ "$running" -ge "$PARALLEL" ]; then
    wait
    running=0
  fi
done <"$WORK/queries.tsv"
wait
cat "$WORK"/part.* >"$TRANSCRIPT" 2>/dev/null
echo "  asked $(count "$TRANSCRIPT") question(s) of the live authorizer"

# ------------------------------------------------------------------------------------------------
# S-3: the assertion block
# ------------------------------------------------------------------------------------------------
echo; echo "== S-3: A-1…A-7 over the sweep transcript =="
score_transcript "$TRANSCRIPT" "$IDENTITIES"

# ------------------------------------------------------------------------------------------------
# S-4: the ¬, on the cluster — an unlabelled Role that grants what the spec withholds
# ------------------------------------------------------------------------------------------------
echo; echo "== S-4: negative control — a write grant is bound to a real actor identity =="

# The victim is a real actor ServiceAccount, discovered rather than named: the control has to run
# against an identity the sweep actually swept, or it proves an arm can fail for a row the live
# transcript does not contain.
victim="$(grep '/actor$' "$IDENTITIES" | head -1)"
victim_ns="${victim%%/*}"
victim_sa="$(printf '%s' "$victim" | cut -d/ -f2)"

# THE DECOY'S RULES ARE READ OFF THE VICTIM'S OWN QUERY ROWS, not written out here. A hand-written
# rule is a guess about what the derived table contains for this identity, and the first live run
# made the guess wrong: the Role granted `create deployments.apps`, the cluster-admin table has no
# such row, and the control staged a fault in a question nobody was going to ask. Taking the
# `append-only`/`update` row and the first NAMESPACED `dark-write` row from the queries file means
# the two rows re-asked below are rows that are already in the transcript, and both must flip.
#
# Namespaced, because the decoy is a Role: a RoleBinding cannot grant a cluster-scoped resource, so
# a dark-write row naming one would stay `no` and read as an arm that failed to fire.
$K api-resources --namespaced=true -o name 2>/dev/null | sort -u >"$WORK/nsserved.txt"
awk -F'\t' -v ns="$victim_ns" -v sa="$victim_sa" \
  '$1==ns && $2==sa && $5=="append-only" && $9=="update"' "$WORK/queries.tsv" | head -1 >"$WORK/decoy-queries.tsv"
awk -F'\t' -v ns="$victim_ns" -v sa="$victim_sa" '
  NR==FNR { nsres[$0]=1; next }
  $1==ns && $2==sa && $5=="dark-write" && $10=="own" {
    key = ($6 == "" ? $7 : $7 "." $6)
    if (key in nsres && $8 == "") { print; exit }
  }' "$WORK/nsserved.txt" "$WORK/queries.tsv" >>"$WORK/decoy-queries.tsv"

decoy_rules=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  g="$(printf '%s' "$line" | cut -f6)"
  r="$(printf '%s' "$line" | cut -f7)"
  v="$(printf '%s' "$line" | cut -f9)"
  decoy_rules="$decoy_rules
  - apiGroups: [\"$g\"]
    resources: [\"$r\"]
    verbs: [\"$v\"]"
done <"$WORK/decoy-queries.tsv"

if [ -z "$victim_sa" ]; then
  bad "S-4: no actor identity was discovered, so the negative control has nothing to bind a grant to. Every arm above is unproven — an assertion block that was never shown to fail is not evidence (V-MET-014)"
elif [ "$(count "$WORK/decoy-queries.tsv")" -ne 2 ]; then
  bad "S-4: could not pick two rows of '$victim_sa' to subvert — found $(count "$WORK/decoy-queries.tsv"), needed an append-only 'update' row and a namespaced dark-write row. The control cannot stage a fault in a question the sweep asks, so the arms above are unmeasured (LSN-048)"
else
  # NO `kube-agents/role: actor` LABEL, deliberately, and this is the whole point of the control:
  # the L0 half of V-BRK-013 discovers actor objects BY THAT LABEL, and `vap-agent-readonly`'s actor
  # arm selects on it too. An unlabelled Role is invisible to both and fully effective — RBAC is a
  # union. If this suite cannot see it either, then nothing in the repository can.
  if $K apply -f - >/dev/null 2>&1 <<YAML
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: $DECOY_ROLE
  namespace: $victim_ns
rules:$decoy_rules
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: $DECOY_BINDING
  namespace: $victim_ns
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: $DECOY_ROLE
subjects:
  - kind: ServiceAccount
    name: $victim_sa
    namespace: $victim_ns
YAML
  then
    DECOY_APPLIED=yes

    # RBAC changes are not instantaneous — the authorizer caches. Polled, never slept on (P9).
    waited=0
    while :; do
      got="$($K auth can-i update actionrecords.kubeagents.x-k8s.io -n "$victim_ns" \
        --as="system:serviceaccount:$victim_ns:$victim_sa" 2>/dev/null | tail -1)"
      [ "$got" = yes ] && break
      [ "$waited" -ge 30 ] && break
      sleep 2
      waited=$((waited + 2))
    done

    if [ "$got" != yes ]; then
      bad "S-4: the decoy grant was applied but the authorizer still refuses '$victim_sa' update actionrecords after ${waited}s. The control never staged its fault, so nothing was measured by it (LSN-048)"
    else
      # Re-ask ONLY the affected questions, and splice their new answers into a copy of the real
      # transcript. Spliced rather than judged on their own: the arms are then exercised inside a
      # transcript that is otherwise correct, which is the shape a real defect arrives in. A
      # two-row transcript would fail A-1 and tell us nothing about A-4 or A-5.
      cp "$TRANSCRIPT" "$WORK/decoyed.tsv"
      : >"$WORK/decoy-rows.tsv"
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        ask_one "$line" >>"$WORK/decoy-rows.tsv"
      done <"$WORK/decoy-queries.tsv"

      n_decoy="$(count "$WORK/decoy-rows.tsv")"
      n_flipped="$(awk -F'\t' '$12=="yes"' "$WORK/decoy-rows.tsv" | count)"
      # BOTH rows, not one. The decoy grants exactly the two verbs those two rows ask about, so a
      # single flip means one of the two grants did not take — and the two arms this control claims
      # to exercise are A-4 and A-5, one per row.
      if [ "$n_decoy" -ne 2 ] || [ "$n_flipped" -ne 2 ]; then
        bad "S-4: re-asking the decoyed questions produced $n_decoy row(s) of which $n_flipped answered yes; both were expected to flip. The fault is not fully staged in the transcript the arms are about to judge, so a red below would not be this control's doing"
      else
        python3 - "$WORK/decoyed.tsv" "$WORK/decoy-rows.tsv" <<'PY'
import sys
base, new = sys.argv[1], sys.argv[2]
key = lambda r: tuple(r[:11])
rows = [l.rstrip("\n").split("\t") for l in open(base) if l.strip()]
repl = {key(l.rstrip("\n").split("\t")): l.rstrip("\n").split("\t") for l in open(new) if l.strip()}
out = [repl.get(key(r), r) for r in rows]
open(base, "w").write("".join("\t".join(r) + "\n" for r in out))
PY
        ctl="$(python3 "$ASSERT" "$WORK/decoyed.tsv" "$IDENTITIES" 2>&1)"
        if printf '%s\n' "$ctl" | grep -q '^FAIL:'; then
          pass "S-4a: with an unlabelled Role granting the actor a write verb, the assertion block goes RED — the arms above can fail on this cluster"
        else
          bad "S-4a: an actor identity was given two of the write verbs 06 withholds from it, by a Role no template owns and no label makes discoverable, and the assertion block still passed. Every green above is a statement about a check that cannot tell (V-MET-014)"
        fi
        if printf '%s\n' "$ctl" | grep '^FAIL:' | grep -q 'A-5 append-only' \
          && printf '%s\n' "$ctl" | grep '^FAIL:' | grep -q 'A-4 negative half'; then
          pass "S-4b: it is caught by the two arms that target it — A-5 (the record became mutable) and A-4 (a withheld write verb is held)"
        else
          bad "S-4b: the decoyed transcript went red, but not on A-4 and A-5. The arms that name this defect are not the ones that caught it: $(printf '%s\n' "$ctl" | grep '^FAIL:' | head -2 | tr '\n' ' ')"
        fi
      fi

      # THE OTHER DIRECTION. The PROVEN verdict this suite prints is a claim about the cluster's
      # state, and the control just changed that state. Revoking and re-asking is what puts the
      # claim back on its feet — and it is also the only thing that proves the revoke worked.
      $K -n "$victim_ns" delete rolebinding "$DECOY_BINDING" --ignore-not-found >/dev/null 2>&1
      $K -n "$victim_ns" delete role "$DECOY_ROLE" --ignore-not-found >/dev/null 2>&1
      DECOY_APPLIED=no
      waited=0
      while :; do
        got="$($K auth can-i update actionrecords.kubeagents.x-k8s.io -n "$victim_ns" \
          --as="system:serviceaccount:$victim_ns:$victim_sa" 2>/dev/null | tail -1)"
        [ "$got" = no ] && break
        [ "$waited" -ge 30 ] && break
        sleep 2
        waited=$((waited + 2))
      done
      if [ "$got" = no ]; then
        pass "S-4c: the decoy is revoked and '$victim_sa' is refused 'update actionrecords' again — the cluster is back in the state the verdict above describes"
      else
        bad "S-4c: the decoy Role was deleted but '$victim_sa' STILL holds 'update actionrecords' after ${waited}s. The control left write authority behind on a real actor identity; remove it by hand: kubectl --context $CTX -n $victim_ns delete role/$DECOY_ROLE rolebinding/$DECOY_BINDING"
      fi
    fi
  else
    bad "S-4: could not apply the decoy Role, so the arms above are unmeasured. A negative control that did not run is not a negative control (LSN-048)"
  fi
fi

# ------------------------------------------------------------------------------------------------
echo
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo "FAIL: only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "V-BRK-013 at L2 / Accept (e): PROVEN — $(count "$TRANSCRIPT") questions derived from 06"
  echo "  and asked of the live authorizer over every labelled agent identity in the fleet, three"
  echo "  tiers, both directions: every grant the spec requires is held, every verb it withholds is"
  echo "  refused, the journal is append-only, every tier can read fleetfreezes, and the arms were"
  echo "  shown to go red against an unlabelled Role that granted an actor a write verb."
  exit 0
fi
echo "V-BRK-013 at L2 / Accept (e): FAILED — see the FAIL lines above."
exit 1
