#!/usr/bin/env bash
# V-RUN-001, V-RUN-002, V-RUN-004 and V-RUN-009 at L2 — the two-workload pair the controller renders
# for an Agent CR: how many there are, whose identity each one runs as, what they are labelled, and
# what survives the CR being deleted.
#
# WHY ALL FOUR IN ONE SUITE, AND WHY L2 IS THE ONLY LEVEL THEY HAVE
#   09 §6.8 marks every one of these rows L2 and nothing else:
#
#     :474  V-RUN-001  Exactly two workloads per Agent CR, both owner-referenced; no third
#                      workload, no minted SA ¬            | 08 §7 | L2 | 9
#     :475  V-RUN-002  Correct identity on each; neither is settable to the other's value ¬
#                                                          | 08 §7 | L2 | 9
#     :477  V-RUN-004  Labels tier/scope/parent/role stamped on Deployments, pods, and Services,
#                      and selectable                      | 05 §8 | L2 | 9
#     :482  V-RUN-009  Deleting the CR removes both workloads and leaves both SAs intact
#                                                          | 08 §7 | L2 | 9
#
#   There is no L0 arm for any of them, and that is not an omission to be quietly filled by a lint.
#   Every one of these is a claim about what the API server HOLDS after the controller has run:
#   cardinality across a live namespace, an ownerReference graph, garbage collection, and what a
#   selector returns cluster-wide. A renderer golden can assert what `BuildPair` returns and say
#   nothing about whether a third workload is sitting next to it, whether the ownerReference the
#   controller set actually causes the cascade, or whether some other component minted a
#   ServiceAccount on the side. A hermetic lint here would be a false green with a check ID on it.
#
#   They share a suite because they share the fixture and, more to the point, they share a FAILURE:
#   all four are statements about one rendered pair, and a pair that is wrong is usually wrong in
#   several of these at once. Splitting them into four suites would mean seeding the pair four times
#   and would still not let any one of them fail independently of the others.
#
# WHAT IS ASSERTED, in order:
#   PAIR-1  PRECONDITIONS AND THE FIXTURE. P10, then P1 on the CONTROLLER, then two Agent CRs
#           applied as shipped, their reader/actor identities seeded, and both halves of both pairs
#           polled into existence.
#   PAIR-2  V-RUN-001 — CARDINALITY, OWNERSHIP, AND THE TWO NEGATIVES.
#           (a) exactly one `<a>-gateway` and exactly one `<a>-broker` Deployment per CR;
#           (b) each one's ownerReferences[0] is that CR, by kind AND name AND uid;
#           (c) the UNION: `-l kube-agents/agent=<a>` returns exactly two Deployments — this is the
#               arm `dev/verify/broker-per-agent-l2.sh` cannot make, because it lists only
#               `-l kube-agents/role=actor` (:219, :240, :248) and is therefore blind to the
#               gateway half. Its counts can be right while the reader half is missing entirely;
#           (d) NO THIRD WORKLOAD: no Deployment, StatefulSet, DaemonSet, Job or CronJob in the
#               namespace is owner-referenced to the CR other than those two;
#           (e) NO MINTED SA, twice over: no ServiceAccount in the namespace carries an
#               ownerReference to either CR, and every ServiceAccount that appeared during this run
#               is attributable to a fixture this script called;
#           (f) the ¬: a decoy Deployment labelled `kube-agents/agent=<a>` + `role=reader`, owned by
#               nothing, is injected and (c)+(d) MUST reject it. The decoy wears `role=reader` and
#               not `role=actor` on purpose — broker-per-agent-l2.sh:394-426 already controls the
#               actor half, and a second copy of that control would be a negative control for a
#               check that is not this one.
#   PAIR-3  V-RUN-002 — IDENTITY ON EACH HALF. The gateway Deployment's pod-template
#           `serviceAccountName` is the tier's reader SA and the broker's is the `<tier>-<leaf>-actor`
#           actor SA the CR itself publishes; both SAs exist; and the two are different names.
#           THE `¬` HALF OF THIS ROW IS ALREADY GREEN AND IS NOT REBUILT HERE. "Neither is settable
#           to the other's value" is admission's claim, and `dev/verify/webhook-negatives-l2.sh:487`
#           (`# --- V-10: the reader-only ServiceAccount override ---`, banner at :488) rejects
#           `platform-<project>-actor`, `default` and another tier's reader SA on
#           `spec.security.serviceAccountName`, with a positive control admitting the tier's own
#           reader SA. The converse — naming the reader SA as the BROKER's identity — is not
#           expressible at all: the actor name is derived, never read from the spec
#           (`broker_manifests.go:187-201`), and V-CTR-003 is green on the derivation. Re-asserting
#           either here would be a second definition site for a decision made elsewhere (V-MET-013).
#   PAIR-4  V-RUN-004 — FIVE LABELS, SIX OBJECTS, THEN SELECTABILITY.
#           Every one of `kube-agents/tier`, `/scope`, `/parent`, `/role`, `/agent` is PRESENT and
#           carries the expected VALUE on: both Deployments, both pod templates, both live pods, and
#           both Services (`<a>` and `<a>-broker`) — for both CRs. Presence is tested separately
#           from value because `kube-agents/parent` is legitimately EMPTY on a platform agent, and
#           `agentlabels.parentOf`'s doc is explicit that absent and empty mean different things:
#           a jsonpath read returns "" for both, so the labels are read as a key=value map instead.
#           Then the "and selectable" clause, cluster-wide and not namespace-scoped, because that is
#           how 05 §8:1341 and 08 §7 state it: `get pods -l kube-agents/role=actor -A` returns
#           EXACTLY the set of broker pods, `-l kube-agents/role=reader -A` exactly the gateway
#           pods, and `-l kube-agents/agent=<a> -A` exactly that CR's own two.
#
#           FOUR OR FIVE LABELS — THE ROW SAYS FOUR, THIS SUITE ASSERTS FIVE. 09 §6.8:477 names
#           `tier/scope/parent/role` and stops. Its own Source column points at 05 §8, and 05
#           §8:1341 is titled "**Labels — five, not three:**" and enumerates `kube-agents/role`,
#           `/agent`, `/tier`, `/scope` and `/parent` on "every agent and broker Deployment, pod, and
#           `Service`". 08 §2.5:196-206 tables all five; 08 §7 says "**(new) All five labels are
#           stamped and selectable**"; and `internal/agentlabels.For()` — the single definition site
#           — renders exactly those five. The row is an abbreviation of its own source, and
#           `kube-agents/agent` is the one that pairs the halves, so dropping it would leave the
#           `-l kube-agents/agent=<a>` selector in the same row's "and selectable" clause unbacked.
#           Five is a strict superset of four: this suite cannot pass where the four-label reading
#           would fail.
#   PAIR-5  V-RUN-009 — DELETION. Both CRs are deleted; a BOUNDED POLL (P9) waits for all four
#           Deployments to become NotFound; then all four ServiceAccounts must still `get` cleanly
#           AND carry the same uid they had before the delete.
#
#           THIS EXPERIMENT WAS ALREADY BEING RUN AND WAS ASSERTING NOTHING. broker-per-agent-l2.sh
#           deletes its CRs in an EXIT trap (:145-154) and states this exact property as prose on
#           :150 — `echo "  by ownerReference. The actor ServiceAccounts and the broker-operations
#           grant are LEFT — a"`. An echo is not a measurement; that is LSN-019 at the level of a
#           single line. Two things follow for this file. The assertion lives in a NAMED function,
#           `assert_deletion_leaves_identities`, CALLED EXPLICITLY before the trap can fire — a trap
#           runs after the verdict is printed, cannot set the exit status the caller sees, and on an
#           early `exit` runs in a context where none of the fixture state is established. And the
#           trap stays idempotent, so the run still cleans up whether or not PAIR-5 got that far.
#
# WHY THE FIXTURE IS THE TWO SHIPPED CRs, APPLIED AND NOT SEEDED
#   `examples/gitops-repo/fleet/platform-agent.yaml` and
#   `examples/gitops-repo/clusters/cluster-a/agents/agent.yaml` are co-located in
#   `kubeagents-system` — 08 §2.6's shape. TWO CRs and not one, because V-RUN-001's exactness clause
#   ("no third workload") and V-RUN-004's selectability clause ("EXACTLY that CR's two pods") are
#   both unfalsifiable against a namespace holding a single pair: with one CR, "every Deployment
#   carrying an agent label belongs to this CR" is true by having nothing else to be wrong about.
#   The cluster-admin CR needs the platform one as its parent anyway (06 §1.2 V-6).
#
#   They are applied DIRECTLY rather than through `seed_parent_agent`, and this is load-bearing in
#   two independent ways. First, `dev/lib/parent-chain.sh:60-65` INJECTS `scaleToZero: true`, which
#   `resolveDeploymentReplicasAndStrategy` (`manifest_helpers.go:209-224`) turns into `replicas: 0`
#   — so a CR seeded that way renders a gateway Deployment with NO POD, and V-RUN-004's "pods" arm
#   and the whole of its selectability clause would be measuring an empty set. Second, that library
#   says outright (:27-28) that the seeded parent "is never a subject. No caller may assert a
#   property OF the seeded parent". Both CRs here are subjects. `dev/verify/verify-phase2.sh:95-200`
#   is the precedent for the correct shape.
#
#   THE COST, STATED: the shipped manifests pin `ghcr.io/gke-labs/kube-agents/*:v0.1.0`, which
#   answers an anonymous pull with 403, so both gateway pods sit in ImagePullBackOff for the life of
#   the run. That is sufficient and not a compromise — every field this suite reads (labels,
#   `serviceAccountName`, ownerReferences) lives in the pod SPEC, which a pod that has not pulled
#   already has in full. Nothing here waits for Ready and nothing here reads a container status. The
#   residue is removed by V-RUN-009 itself, which deletes the CRs as its experiment, and again by the
#   EXIT trap.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates AND DELETES Agent CRs
# named `platform-agent` and `cluster-admin-cluster-a` in `kubeagents-system`, applies RBAC, and
# creates a decoy Deployment. On the live install `platform-agent-host` those are the fleet's own
# agents and PAIR-5 is a test that deletes them.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED (P1/P10 unverifiable).
# Usage: dev/verify/workload-pair-l2.sh [kube-context]
#        dev/verify/workload-pair-l2.sh --negative-control
#
# ------------------------------------------------------------------------------------------------
# THE `¬` ARM — WHY THIS FILE HAS ONE AT ALL, AND WHAT SHAPE IT HAD TO TAKE
# ------------------------------------------------------------------------------------------------
# 09 §6 marks V-RUN-001 and V-RUN-002 with `¬`, which V-MET-014 reads as "a negative control is
# mandatory: break the property, watch the check fail". Until this mode existed, all four rows had
# been measured green on a live cluster and NONE of them had been shown capable of going red — the
# one exception being PAIR-2f, which is a live control for V-RUN-001's cardinality clause and needs
# a cluster to run at all. A suite of vacuous passes reads exactly like a suite of real ones; the
# control is the only thing that tells them apart.
#
# THE ARMS ARE REPLAYED, NEVER RE-STATED. Every judgement below has been lifted out of the live
# collection that feeds it and into a function that takes ALREADY-READ VALUES as arguments —
# `judge_pair_topology`, `judge_pair_identity`, `judge_target_labels`, `judge_scope_distinct`,
# `judge_selector_exact`, `judge_workload_gone`, `judge_sa_survival`, `judge_sa_watchable`,
# `judge_deploy_present_before`. The control feeds those SAME functions synthesised inputs. It does
# not contain a second copy of any assertion, and that is the load-bearing property of the whole
# arm ([[LSN-024]] in the general form the reference implementation states it: a control that
# re-states the assertion is a second definition site, and the day the live arm is edited the copy
# stays green about the previous rule). If an arm below is deleted, the control's row for it stops
# being an arm about anything and its needle stops matching, which is the failure that reports it.
#
# EVERY RED ROW NAMES THE RULE THAT MUST CATCH IT ([[LSN-035]]). A row counts as caught only when a
# `FAIL:` line CONTAINS that row's needle. Without the needle, breaking the shared five-key loop
# would "catch" every mutant at once by failing every arm on every one of them, and the control
# would read 33/33 while asserting that the suite is broken. Most rows go further and demand
# EXACTLY ONE red line, because "the class is right and the reason is not" has an analogue here:
# a pair whose two halves run as the same ServiceAccount satisfies the three identity arms that
# read a name and fails only the one that compares the two, and a control that accepted "it went
# red somewhere" would not distinguish that from a suite that simply cannot read a Deployment.
#
# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) The control runs with NO cluster — it hands
# synthesised strings straight to the judgement functions, so every statement that ACQUIRES those
# strings on a live run is bypassed by it and is unmeasured by the 33/33:
#   - every `kubectl` invocation in the file. The `-o go-template` label renders, the `-o jsonpath`
#     reads, `deploy_names_labelled`'s selector, `owned_workloads`' five-kind sweep and
#     `pods_selected_by`'s `-A` listing are all replaced by literals. A go-template that emitted
#     nothing — the exact shape of [[LSN-024]]'s early read — would give every target an empty
#     label blob live and a full one under the control.
#   - THE FIXTURE. No Agent CR is applied, no identity is seeded, no Deployment is polled into
#     existence, and the two shipped manifests are never parsed. The header's whole argument for
#     applying them directly rather than through `seed_parent_agent` (the `scaleToZero` injection
#     that would render `replicas: 0` and empty V-RUN-004's pod arms) is untested by this mode.
#   - THE DECOY, and therefore PAIR-2f. The live control creates a real unowned Deployment and
#     re-runs the real predicate over the real namespace; the row named `topology-unowned-decoy`
#     below replays only the JUDGEMENT half of it against a hand-written owner-reference list. An
#     `apply` that silently failed is a live-only failure mode, which is why PAIR-2f keeps its own
#     "could not create the decoy" red ([[LSN-048]]).
#   - THE CASCADE ITSELF. V-RUN-009's subject is Kubernetes garbage collection: that deleting the
#     CR actually removes the children the ownerReference names. The control synthesises the
#     ANSWER to that question (`gone=1` / `gone=0`) and can say nothing about whether the API
#     server produces it. Same for the ServiceAccount uids — the control is handed two strings and
#     asked whether it can tell them apart; it never watches an SA survive anything.
#   - the destructive-test guard, `$K version`, P10 and P1. All of them run in live mode only, and
#     P1 in particular is what makes the live greens statements about THIS commit rather than about
#     whatever controller the cluster happens to be running.
#   - the SCOPE DERIVATION. `expected_scope_label` restates RenderScope's rule 1 against live
#     `spec.scope.*` fields; the control passes `scope_exact` and the expected value in as arguments
#     and never calls it. Whether rule 1 was the applicable rule is a live-only question.
# What the control proves, and all it proves: 33 rows — 23 injected defects, each caught by the arm
# that targets it and named in the output, and 10 correct inputs, each accepted, so the arms are
# neither always-green nor always-red. A run reports `negative control: 33/33` and exits 0.
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager, asserted with
#      p1_assert_build_under_test. The CONTROLLER is the whole subject: every property below —
#      cardinality, the ownerReferences that drive the cascade, the identity on each half, the five
#      labels — is authored by it at render time, so a controller one generation behind makes all
#      four rows statements about the previous build.
#      DELIBERATELY NOT ASSERTED AGAINST THE WORKLOADS THEMSELVES, and this is a ruling rather than
#      an oversight. The gateway runs the shipped `ghcr.io/gke-labs/kube-agents/*:v0.1.0` agent
#      image, which has no entry in preconditions.sh `_p1_build_inputs` and by that function's own
#      design returns 3 — "could not verify" — for every run, forever. Wiring it in would convert a
#      real result into a permanent deferral (LSN-008), and it would be a deferral about a binary
#      that authors none of these properties. The broker's own image is covered where it is the
#      subject, in broker-per-agent-l2.sh L2-1.
#   P3 admission-recreate: both Agent CRs are deleted before they are applied on every run, so the
#      pair under test is rendered by the controller now running and admitted by the webhook now
#      installed — never inherited from an earlier generation. The decoy Deployment of PAIR-2f is
#      likewise created and removed within the run. Pods are pinned once through p3_pod_of_deploy,
#      by ownership rather than by selector, so a pod left over from a previous generation of the
#      same Deployment can never be read as this one's (LSN-025).
#   P6 runtime-authoritative: every claim is read off LIVE objects — the Deployments and Services
#      the controller wrote, their ownerReference graph, the labels as the API server stores them,
#      the ServiceAccount names on the running pod templates, the actor name the CR publishes in its
#      own status, and the ServiceAccount objects that outlive the CRs. No render golden and no
#      manifest in the tree is consulted; the fixture manifests are inputs to the controller, never
#      the artifact any assertion reads.
set -uo pipefail

# MODES. `live` reads and mutates a real cluster and is what every claim in the header is about.
# `--negative-control` is the mandatory `¬` arm (V-MET-014): it replays the judgement functions
# below against synthesised inputs and requires each injected defect to be caught by the arm that
# targets it. It contacts nothing — see the `NEGATIVE CONTROL DOES NOT EXERCISE` block above for
# the honest list of what that buys and what it costs.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
PLATFORM_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml
CLUSTER_ADMIN_MANIFEST=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
PLATFORM_AGENT=platform-agent
CLUSTER_ADMIN_AGENT=cluster-admin-cluster-a
DECOY=workload-pair-l2-decoy

# Positionally-parallel indexed arrays rather than one associative array: bash 3.2 is what
# /usr/bin/env bash resolves to on the macOS hosts this suite is driven from, and `declare -A` is a
# bash-4 PARSE error there — the script would die before the destructive-test guard ever ran.
AGENTS=("$PLATFORM_AGENT" "$CLUSTER_ADMIN_AGENT")
AGENT_UIDS=()
GATEWAY_PODS=()
BROKER_PODS=()
READER_SAS=()
ACTOR_SAS=()

# The five 08 §2.5 keys, spelled once here. They are compared against by admission policy, so a typo
# in this list is a check that agrees with a controller that agrees with nothing.
LABEL_KEYS=(kube-agents/tier kube-agents/scope kube-agents/parent kube-agents/role kube-agents/agent)

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero; that is the
# half that makes the rest of it a guard.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not an ephemeral scratch cluster (destructive-test guard)." >&2
    echo "  V-RUN-009 DELETES the Agent CRs platform-agent and cluster-admin-cluster-a as its" >&2
    echo "  experiment. Name the dev cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }

cd "$REPO_ROOT" || exit 1

# ================================================================================================
# THE JUDGEMENT BLOCK — every arm this suite scores, as functions over ALREADY-READ VALUES
# ================================================================================================
# NOTHING BELOW CALLS KUBECTL, and that is not a tidiness rule. It is the single property that makes
# `--negative-control` a control rather than a second implementation of the same opinion: the ¬ arm
# replays THESE functions against synthesised arguments, so an edit to an arm is an edit to the
# thing the control scores and the two cannot drift apart. The live path keeps every collection —
# the `-o go-template` label renders, the selector listings, the ownership sweeps, the bounded
# polls — in the `PAIR-n` sections further down, each of which reads its values from the API server
# and then calls in here to be judged.
#
# NAMED `judge_*`, NEVER `check_*` OR `assert_*`, for the reason `expect_label` already carries:
# `invariants-gate.py`'s SH_CHECK discovers a shell suite's named checks as
# `^(check|assert)_[a-z_]* "<name>"` and folds them into the V-MET-003 assertion ratchet, so a
# helper spelled that way whose first argument is a VARIABLE registers the literal string `$desc`
# as a test name that can never be deleted because it never existed.
#
# THE ARGUMENT LISTS ARE FLAT STRINGS, newline-separated where they are sets. bash 3.2 is what
# `/usr/bin/env bash` resolves to on the macOS hosts this suite is driven from — the same constraint
# that forces the positionally-parallel arrays above — so a function cannot take an associative
# array and cannot take an array by reference. A tab-separated table (`judge_pair_topology`'s
# `refs_table`) is the honest way to hand in a one-to-many mapping under that constraint.

# --- V-RUN-004: label primitives ----------------------------------------------------------------

# label_present <key> <blob> · label_value <key> <blob>
label_present() {
  printf '%s\n' "$2" | awk -v k="$1" 'index($0, k "=") == 1 { f = 1 } END { exit !f }'
}
label_value() {
  printf '%s\n' "$2" | awk -v k="$1" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }'
}

# expect_label <what> <blob> <key> <want>
#
# NOT named `assert_label`. `invariants-gate.py` SH_CHECK discovers a shell suite's named checks as
# `^(check|assert)_[a-z_]* "<name>"`, so a helper spelled that way whose first argument is a
# VARIABLE registers the literal string `$desc` in the V-MET-003 assertion ratchet — a name that can
# never be deleted because it never existed. Four suites in dev/verify already carry that wart in
# dev/assertion-baseline.json; this one does not add a fifth.
expect_label() {
  local what="$1" blob="$2" key="$3" want="$4" got
  if ! label_present "$key" "$blob"; then
    bad "V-RUN-004: $what does not carry the key $key at all. An absent label is not an empty one — every selector naming it silently matches nothing, and no policy keyed on it reports an error (agentlabels package doc)"
    return
  fi
  got="$(label_value "$key" "$blob")"
  if [ "$got" = "$want" ]; then
    pass "V-RUN-004: $what carries $key=${want:-<empty, and stamped>}"
  else
    bad "V-RUN-004: $what carries $key='$got' but the CR says it should be '$want'"
  fi
}

# SEEN_SCOPE — the cross-object carry for the hashed-scope path, one CR at a time.
#
# A GLOBAL AND NOT A PARAMETER, deliberately. When `expected_scope_label` cannot re-derive the
# label (RenderScope rules 2 and 3, the truncate-and-digest path, which this file refuses to
# reimplement) the fallback claim is "every object of this pair renders the SAME scope", and a claim
# about a pair cannot be judged one object at a time without state that outlives the call. The
# alternative — passing the carry in and echoing it back out — would put the value on the same
# stdout the PASS/FAIL lines use, which is the stream the ¬ arm scores. Reset per CR by the caller,
# and reset by each control row that touches it, so a leaked value cannot make a later arm agree.
SEEN_SCOPE=""

# judge_target_labels <desc> <blob> <tier> <want-scope> <parent> <role> <agent> <scope-exact>
#   One target, five keys. `blob` is the object's labels as `key=value` lines; `scope-exact` is 1
#   when the caller could re-derive the expected scope value and 0 when it fell back to the weaker
#   present / non-empty / identical-across-the-pair claim.
judge_target_labels() {
  local desc="$1" blob="$2" tier="$3" want_scope="$4" parent="$5" role="$6" agent="$7" scope_exact="$8"
  local kx key got_scope
  local want_vals

  if [ -z "$blob" ]; then
    bad "V-RUN-004: $desc has no labels at all (or does not exist)"
    return
  fi
  # Positionally parallel to LABEL_KEYS. Iterating the key list rather than writing five calls is
  # what makes LABEL_KEYS the definition site: a sixth key added there is asserted here, and a key
  # deleted there stops being asserted visibly rather than by a line quietly not existing.
  want_vals=("$tier" "$want_scope" "$parent" "$role" "$agent")
  for kx in $(seq 0 $((${#LABEL_KEYS[@]} - 1))); do
    key="${LABEL_KEYS[$kx]}"
    if [ "$key" != "kube-agents/scope" ] || [ "$scope_exact" = "1" ]; then
      expect_label "$desc" "$blob" "$key" "${want_vals[$kx]}"
    elif ! label_present "$key" "$blob"; then
      bad "V-RUN-004: $desc does not carry the key $key at all"
    else
      got_scope="$(label_value "$key" "$blob")"
      if [ -z "$got_scope" ]; then
        bad "V-RUN-004: $desc carries an EMPTY $key, but $agent declares a scope"
      elif [ -z "$SEEN_SCOPE" ] || [ "$got_scope" = "$SEEN_SCOPE" ]; then
        SEEN_SCOPE="$got_scope"
        pass "V-RUN-004: $desc carries $key=$got_scope"
      else
        bad "V-RUN-004: $desc carries $key='$got_scope' while another object of the same pair carries '$SEEN_SCOPE'. One agent, two scope renderings, and every per-scope policy now selects half a pair"
      fi
    fi
  done
}

# judge_scope_distinct <scope-a> <scope-b>
#   Without this, a RenderScope that returned a constant would satisfy every per-object arm above.
judge_scope_distinct() {
  if [ "$1" != "$2" ]; then
    pass "V-RUN-004: the two CRs render distinct scope labels ('$1' vs '$2')"
  else
    bad "V-RUN-004: both CRs carry kube-agents/scope='$1' despite being in different scopes. A scope label that does not distinguish scopes makes every per-scope selector above pass vacuously (V-RUN-011)"
  fi
}

# judge_selector_exact <selector> <what> <want> <got>
#   The "and selectable" clause, for one selector. `want` is the set built BY OWNERSHIP and `got`
#   the set the selector returned; both are sorted `<ns>/<pod>` lines. An empty `want` is not a
#   quiet pass: a selector compared against nothing is exact about nothing ([[LSN-035]]).
#
#   ONE FUNCTION FOR BOTH THE ROLE AND THE AGENT SELECTORS. They were two near-identical inline
#   blocks whose only real difference was the noun in the message; keeping two would mean the ¬ arm
#   controls one of them and the other is a copy that nothing scores.
judge_selector_exact() {
  local sel="$1" what="$2" want="$3" got="$4"
  if [ -z "$want" ]; then
    bad "V-RUN-004: no $what could be resolved by ownership anywhere on the cluster, so 'get pods -l $sel -A returns exactly the $what' has nothing to be exact about (LSN-035)"
  elif [ "$want" = "$got" ]; then
    pass "V-RUN-004: 'get pods -l $sel -A' returns exactly the $what ($(printf '%s' "$got" | grep -c .))"
  else
    bad "V-RUN-004: 'get pods -l $sel -A' returned '$(printf '%s' "$got" | tr '\n' ' ')' but the $what resolved by ownership are '$(printf '%s' "$want" | tr '\n' ' ')' (05 §8:1341, 08 §7)"
  fi
}

# --- V-RUN-001: cardinality, ownership, and the two negatives -------------------------------------

# judge_pair_topology <agent> <uid> <named-halves> <labelled> <refs-table> <owned>
#
#   named-halves  the Deployment names a by-NAME get resolved, one per line — clause (a)
#   labelled      the Deployment names carrying `kube-agents/agent=<agent>`, one per line — (c)
#   refs-table    `<deployment-name>\t<Kind>/<name>/<uid>` lines, one per ownerReference — (b)
#   owned         `<kind>/<name>` for every workload object owner-referenced to <uid> — (d)
#
# Reports a COUNT of problems on stdout and the prose on stderr, which is what lets the same
# function be an assertion in PAIR-2 and an experiment in PAIR-2f: the decoy run wants the count and
# not the noise. A caller that only reads stdout is reading a number that means "how many ways is
# this pair not a pair", and 0 is the only passing value.
judge_pair_topology() {
  local a="$1" uid="$2" named="$3" labelled="$4" refs_table="$5" owned="$6"
  local problems=0 n dname refs want half
  want="Agent/$a/$uid"

  # (a) exactly one of each half, by NAME.
  for half in gateway broker; do
    n="$(printf '%s\n' "$named" | grep -cxF "$a-$half")"
    if [ "$n" != "1" ]; then
      echo "  no Deployment named $a-$half in $NS" >&2
      problems=$((problems + 1))
    fi
  done

  # (c) THE UNION. Exactly two Deployments carry this CR's agent label — the arm a broker-only
  # listing cannot make. A third one here is a workload nobody accounted for; a first or second
  # missing one is half a pair.
  n="$(printf '%s\n' "$labelled" | grep -c .)"
  if [ "$n" != "2" ]; then
    echo "  $n Deployment(s) carry kube-agents/agent=$a in $NS; the pair is exactly two" >&2
    problems=$((problems + 1))
  fi

  # (b) OWNERSHIP, by kind AND name AND uid. The uid is what makes this a statement about THIS CR
  # and not about a same-named CR deleted and recreated between two runs.
  while IFS= read -r dname; do
    [ -n "$dname" ] || continue
    refs="$(printf '%s\n' "$refs_table" | awk -F'\t' -v d="$dname" '$1 == d { print $2 }')"
    if ! printf '%s\n' "$refs" | grep -qxF "$want"; then
      echo "  Deployment $dname carries kube-agents/agent=$a but its ownerReferences are '$(printf '%s' "$refs" | tr '\n' ' ')', not $want" >&2
      problems=$((problems + 1))
    fi
  done <<EOF
$labelled
EOF

  # (d) NO THIRD WORKLOAD, asked of the ownership graph rather than of the label — a workload the
  # controller minted without labelling it is exactly the case a label-scoped count cannot see.
  n="$(printf '%s\n' "$owned" | grep -c .)"
  if [ "$n" != "2" ]; then
    echo "  $n workload object(s) in $NS are owner-referenced to $a: $(printf '%s' "$owned" | tr '\n' ' ')" >&2
    problems=$((problems + 1))
  fi

  echo "$problems"
}

# --- V-RUN-002: the identity on each half ---------------------------------------------------------

# judge_pair_identity <agent> <tier> <cr-reader-sa> <published-actor-sa> <got-reader> <got-actor> <existing-sas>
#   `got-*` are the `serviceAccountName`s the two rendered pod templates actually carry;
#   `existing-sas` is the subset of those two names that resolve to an object in $NS, one per line.
#
# FOUR ARMS AND THEN EXISTENCE, ALWAYS IN THIS ORDER. The fourth — that the two names DIFFER — is
# not implied by the first three: a controller that rendered the reader SA onto both halves would
# satisfy the gateway clause, and would satisfy the broker clause too if the derivation that
# publishes the actor name collapsed with it. That case is three greens and one red, which is why
# the ¬ arm demands EXACTLY ONE red line for it rather than merely "it went red".
judge_pair_identity() {
  local a="$1" tier="$2" reader="$3" actor="$4" got_reader="$5" got_actor="$6" existing="$7"
  local sa

  # The gateway. `agent_manifests.go:331-332` takes spec.security.serviceAccountName and falls back
  # to the CR name; 08 §7 states the same thing as "the `<tier>-agent` reader SA", and the two
  # coincide only because webhook rule V-10 restricts what the field may hold. Both forms are
  # asserted: the derivation, and the shape the spec names.
  if [ -n "$got_reader" ] && [ "$got_reader" = "$reader" ]; then
    pass "V-RUN-002: $a-gateway runs as '$got_reader', the reader SA its CR names"
  else
    bad "V-RUN-002: $a-gateway runs as '${got_reader:-<unset>}' but its CR names reader SA '$reader'"
  fi
  if [ "$got_reader" = "$tier-agent" ]; then
    pass "V-RUN-002: '$got_reader' is the <tier>-agent form 08 §7 requires for tier '$tier'"
  else
    bad "V-RUN-002: $a-gateway's SA '$got_reader' is not '$tier-agent'. 08 §7 names the reader identity by tier, and webhook rule V-10 is what is supposed to make the CR's field unable to say anything else"
  fi

  # The broker, against the name the CR itself publishes.
  if [ -n "$actor" ] && [ "$got_actor" = "$actor" ]; then
    pass "V-RUN-002: $a-broker runs as '$got_actor', the actor SA its own status publishes"
  else
    bad "V-RUN-002: $a-broker runs as '${got_actor:-<unset>}' but the CR publishes actor SA '${actor:-<none>}'"
  fi

  # The two are different names. A broker running as a read-only identity fails closed at the API
  # server and looks like a broker bug, which is how this defect gets attributed to the wrong
  # component for a week.
  if [ -n "$got_reader" ] && [ "$got_reader" != "$got_actor" ]; then
    pass "V-RUN-002: the two halves of $a run as different identities"
  else
    bad "V-RUN-002: both halves of $a run as '$got_reader'. The reader/actor split is 08 §2.5's whole point; one identity on both halves is the split not existing"
  fi

  # Both names resolve to an object. An SA name that resolves to nothing is not an identity: the
  # pod is refused by the ServiceAccount admission plugin and the Deployment reports no pods.
  for sa in "$got_reader" "$got_actor"; do
    [ -n "$sa" ] || continue
    if printf '%s\n' "$existing" | grep -qxF "$sa"; then
      pass "V-RUN-002: ServiceAccount $NS/$sa exists"
    else
      bad "V-RUN-002: $a names ServiceAccount '$sa', which does not exist in $NS"
    fi
  done
}

# --- V-RUN-009: what the delete takes and what it leaves ------------------------------------------

# judge_sa_watchable <agent> <sa> <before-uid>
#   NON-VACUITY, BEFORE THE EXPERIMENT. "The ServiceAccount survived" is trivially true of a
#   ServiceAccount that was never there ([[LSN-035]]).
judge_sa_watchable() {
  local a="$1" sa="$2" before_uid="$3"
  if [ -z "$sa" ]; then
    bad "V-RUN-009: $a has no ServiceAccount name to watch across the delete"
  elif [ -z "$before_uid" ]; then
    bad "V-RUN-009: ServiceAccount $NS/$sa does not exist BEFORE the delete, so its survival afterwards would be a statement about nothing (LSN-035)"
  fi
}

# judge_deploy_present_before <agent> <half> <uid>
#   The other half of the same non-vacuity: "the delete removed it" is trivially true of a
#   Deployment that never existed.
judge_deploy_present_before() {
  if [ -z "$3" ]; then
    bad "V-RUN-009: Deployment $1-$2 is already absent BEFORE the delete; 'the delete removed it' cannot be measured"
  fi
}

# judge_workload_gone <agent> <half> <gone 0|1> <waited-seconds>
judge_workload_gone() {
  local a="$1" half="$2" gone="$3" waited="$4"
  if [ "$gone" = "1" ]; then
    pass "V-RUN-009: Deployment $a-$half is gone ${waited}s after the CR was deleted"
  else
    bad "V-RUN-009: Deployment $a-$half still exists 180s after Agent $a was deleted. The cascade is the ownerReference PAIR-2 asserted, so either it is not being set or garbage collection is not honouring it — a workload outliving its CR is an agent nobody can see and nobody can stop"
  fi
}

# judge_sa_survival <agent> <sa> <before-uid> <after-uid>
#   THE UID, NOT THE NAME, AND THIS IS THE WHOLE ARM. 08 §7 says the SAs are LEFT. An SA that the
#   next reconcile deleted and recreated answers a bare existence check `yes` under the same name
#   while every token minted against the old one has already been invalidated — so a control that
#   only proved "the check notices a missing SA" would leave the interesting half unmeasured. The
#   ¬ arm's `deletion-sa-uid-changed` row is that case exactly.
judge_sa_survival() {
  local a="$1" sa="$2" before_uid="$3" after_uid="$4"
  if [ -z "$after_uid" ]; then
    bad "V-RUN-009: ServiceAccount $NS/$sa was REMOVED when Agent $a was deleted. 08 §7 requires both identities to survive: they are installed once per namespace by the provisioning path and outlive every CR in it, and a controller that garbage-collects them is deleting RBAC 08 §4 forbids it from owning"
  elif [ -n "$before_uid" ] && [ "$after_uid" != "$before_uid" ]; then
    bad "V-RUN-009: ServiceAccount $NS/$sa exists but its uid changed across the delete ($before_uid -> $after_uid) — it was destroyed and recreated, not left intact"
  else
    pass "V-RUN-009: ServiceAccount $NS/$sa survived the deletion of $a, same uid"
  fi
}

# ================================================================================================
# THE `¬` ARM
# ================================================================================================
# WHY SYNTHESISED INPUTS AND NOT A MUTATED CONTROLLER. Making a REAL controller render a third
# workload, collapse the two identities onto one SA or drop a label key means editing
# `pod_launcher.go` and rolling an image, which is `dev/mutate.py`'s job at L1 and not something an
# L2 suite can stage against a running cluster. What this arm proves is the thing an L2 suite CAN
# get wrong on its own: that the judgement functions tell a correct pair from an incorrect one, and
# that they tell each SPECIFIC defect apart from the others rather than going red as a block.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`. Every judged call runs inside a command
# substitution, which is a subshell, so every `fail=1` a `bad` sets inside one dies with it. That is
# also why the scorers below count `^FAIL:` lines rather than inspecting a variable.
#
# BROKEN IS NOT MISS ([[LSN-063]]). Three ways a row can fail to be an experiment, each reported
# under its own word rather than folded into MISS — which would invite strengthening a check that
# was never asked anything:
#   - a judgement that emitted no PASS and no FAIL at all. The arm it was pointed at has been
#     deleted or returns early; nothing was evaluated.
#   - a topology row whose four synthesised blobs are byte-identical to the clean baseline. That is
#     a mutation that stopped mutating — LSN-063's shape exactly, guarded rather than described.
#   - a topology judgement whose stdout is not an integer. The function's contract is a count.

NC_A=platform-agent
NC_UID_A=11111111-1111-4111-8111-111111111111
NC_B=cluster-admin-cluster-a

# The clean world, as the four arguments `judge_pair_topology` takes. Every topology mutant below is
# written as an edit of one of these, so the diff between a row and the baseline IS the defect.
nc_clean_named() { printf '%s\n%s\n' "$NC_A-gateway" "$NC_A-broker"; }
nc_clean_labelled() { printf '%s\n%s\n' "$NC_A-gateway" "$NC_A-broker"; }
nc_clean_refs() {
  printf '%s\tAgent/%s/%s\n%s\tAgent/%s/%s\n' \
    "$NC_A-gateway" "$NC_A" "$NC_UID_A" "$NC_A-broker" "$NC_A" "$NC_UID_A"
}
nc_clean_owned() { printf 'deployments/%s\ndeployments/%s\n' "$NC_A-gateway" "$NC_A-broker"; }

# nc_labels <role> <scope> — a well-formed label blob for one target of the platform pair.
# `kube-agents/parent=` is PRESENT AND EMPTY on purpose: that is what a root agent's labels look
# like (agentlabels.parentOf — "absent means this controller did not stamp it, empty means this
# agent is a root"), and a control whose clean input had a non-empty parent would never exercise the
# distinction `expect_label` exists to draw.
nc_labels() {
  printf 'kube-agents/agent=%s\nkube-agents/parent=\nkube-agents/role=%s\nkube-agents/scope=%s\nkube-agents/tier=platform\n' \
    "$NC_A" "$1" "$2"
}

nc_total=0
nc_caught=0
nc_rc=0
NC_ERR=""

# nc_ok <name> <why> · nc_miss <name> <why> [<output>] · nc_broken <name> <why> [<output>]
nc_ok() {
  nc_caught=$((nc_caught + 1))
  echo "  ok     $1 — $2"
}
nc_miss() {
  nc_rc=1
  echo "  MISS   $1 — $2"
  [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/         /'
  return 0
}
nc_broken() {
  nc_rc=1
  echo "  BROKEN $1 — $2"
  [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/         /'
  return 0
}

# nc_score <name> <green|red|red1|silent> <needle> -- <judge fn> <args...>
#
#   green   the judgement accepts the input: zero FAIL lines, at least one PASS. Non-vacuity — a
#           control made only of defects proves the arms are always-red, which is not better.
#   red     at least one FAIL line, and one of them CONTAINS the needle ([[LSN-035]]).
#   red1    EXACTLY one FAIL line, and it contains the needle. Used where the defect is supposed to
#           be caught by ONE named arm while the arms around it stay green — the identity-collapse
#           row is the reason this verdict exists, since "three arms that read a name are happy and
#           only the arm that compares them is not" is the whole content of that mutant.
#   silent  the judgement is a NON-VACUITY GUARD (`judge_sa_watchable`,
#           `judge_deploy_present_before`) which speaks only when the experiment is unrunnable, so
#           its clean input must produce no line at all. Scored separately from `green` precisely so
#           the "no output means the arm was deleted" guard can stay strict everywhere else.
nc_score() {
  local name="$1" expect="$2" needle="$3"
  shift 3
  [ "${1:-}" = "--" ] && shift
  local out n_fail n_any
  nc_total=$((nc_total + 1))
  out="$("$@" 2>&1)"
  n_fail="$(printf '%s\n' "$out" | grep -cE '^FAIL:')"
  n_any="$(printf '%s\n' "$out" | grep -cE '^(PASS|FAIL):')"

  if [ "$expect" = silent ]; then
    if [ "$n_any" -eq 0 ] && [ -z "$out" ]; then
      nc_ok "$name" "the guard stays quiet on an experiment that CAN be run"
    else
      nc_miss "$name" "a runnable experiment tripped its own non-vacuity guard" "$out"
    fi
    return 0
  fi

  if [ "$n_any" -eq 0 ]; then
    nc_broken "$name" "the judgement emitted no PASS and no FAIL. Nothing was evaluated, so this row is not a finding about the check — the arm it targets has been deleted or returns early ([[LSN-063]])" "$out"
    return 0
  fi

  case "$expect" in
    green)
      if [ "$n_fail" -eq 0 ]; then
        nc_ok "$name" "a CORRECT input is accepted ($n_any arm(s) ran), so the arms below are not always-red"
      else
        nc_miss "$name" "a CORRECT input was failed $n_fail time(s); every defect below would then be 'caught' for a reason that has nothing to do with it" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
      fi
      ;;
    red1)
      if [ "$n_fail" -ne 1 ]; then
        nc_miss "$name" "expected EXACTLY one red arm and got $n_fail. The defect is supposed to be visible to one named arm while its neighbours stay green; a block of reds does not distinguish it from a suite that cannot read the object at all" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
      elif printf '%s\n' "$out" | grep -E '^FAIL:' | grep -qF "$needle"; then
        nc_ok "$name" "caught by exactly the one arm that targets it ('$needle')"
      else
        nc_miss "$name" "went red once and the line does not mention '$needle', so the property it targets is not what caught it" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
      fi
      ;;
    red)
      if printf '%s\n' "$out" | grep -E '^FAIL:' | grep -qF "$needle"; then
        nc_ok "$name" "caught by the arm that targets it ('$needle'), $n_fail red arm(s) in total"
      else
        nc_miss "$name" "went red $n_fail time(s) but no FAIL line mentions '$needle', so the property it targets is not what caught it" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
      fi
      ;;
    *)
      nc_broken "$name" "unknown expectation '$expect'"
      ;;
  esac
  return 0
}

# nc_topology <name> <zero|nonzero> <needle> <named> <labelled> <refs-table> <owned>
#
# `judge_pair_topology` answers with a COUNT on stdout and its prose on stderr — the contract PAIR-2
# asserts against and PAIR-2f experiments with — so it needs its own scorer. The clean-baseline
# comparison is [[LSN-063]]'s guard: a `nonzero` row whose four blobs are byte-identical to the
# clean world injected nothing, and reporting that as MISS would invite someone to "strengthen" a
# predicate that was never handed a defect.
nc_topology() {
  local name="$1" expect="$2" needle="$3" named="$4" labelled="$5" refs="$6" owned="$7"
  local out n err
  nc_total=$((nc_total + 1))
  : >"$NC_ERR"
  out="$(judge_pair_topology "$NC_A" "$NC_UID_A" "$named" "$labelled" "$refs" "$owned" 2>"$NC_ERR")"
  err="$(cat "$NC_ERR")"

  case "$out" in
    '' | *[!0-9]*)
      nc_broken "$name" "the predicate answered '$out', which is not a problem count. Its contract is a number on stdout and prose on stderr" "$err"
      return 0
      ;;
  esac
  n="$out"

  if [ "$expect" = nonzero ] &&
    [ "$named" = "$(nc_clean_named)" ] && [ "$labelled" = "$(nc_clean_labelled)" ] &&
    [ "$refs" = "$(nc_clean_refs)" ] && [ "$owned" = "$(nc_clean_owned)" ]; then
    nc_broken "$name" "the synthesised world is byte-identical to the clean baseline, so no defect was injected and the predicate was asked nothing ([[LSN-063]])"
    return 0
  fi

  if [ "$expect" = zero ]; then
    if [ "$n" -eq 0 ] && [ -z "$err" ]; then
      nc_ok "$name" "the correct pair scores 0 problems, so 'exactly two, both owned' is a measurement and not a tautology"
    else
      nc_miss "$name" "the CORRECT pair scored $n problem(s); every mutant below would be caught for the wrong reason" "$err"
    fi
    return 0
  fi

  if [ "$n" -eq 0 ]; then
    nc_miss "$name" "the predicate scored 0 problems on a world it must reject. The 'exactly two' green in PAIR-2 would not be evidence (V-MET-014)" "$err"
  elif printf '%s\n' "$err" | grep -qF "$needle"; then
    nc_ok "$name" "rejected with $n problem(s), by the clause that targets it ('$needle')"
  else
    nc_miss "$name" "rejected with $n problem(s) but no line mentions '$needle', so the clause it targets is not what rejected it" "$err"
  fi
  return 0
}

# nc_probe_scope_pair <gateway-scope> <broker-scope>
#   Two targets of ONE pair, judged in sequence so the `SEEN_SCOPE` carry is what it is on a live
#   run. It has to be a function rather than two rows: the carry lives in a shell variable and every
#   row is scored inside a command substitution, so two rows would be two subshells and the second
#   would start with an empty carry — a control that could never see the drift it exists to catch.
nc_probe_scope_pair() {
  SEEN_SCOPE=""
  judge_target_labels "Deployment $NC_A-gateway" "$(nc_labels reader "$1")" \
    platform "" "" reader "$NC_A" 0
  judge_target_labels "Deployment $NC_A-broker" "$(nc_labels actor "$2")" \
    platform "" "" actor "$NC_A" 0
}

# nc_probe_labels <desc> <blob> <role>  — one target, the re-derivable-scope path.
nc_probe_labels() {
  SEEN_SCOPE=""
  judge_target_labels "$1" "$2" platform adamparco-kage "" "$3" "$NC_A" 1
}

run_negative_control() {
  NC_ERR="$(mktemp "${TMPDIR:-/tmp}/workload-pair-l2-nc.XXXXXX")" || return 1

  echo
  echo "-- V-RUN-001: cardinality, ownership, and the two negatives (judge_pair_topology) --"
  nc_topology clean-pair zero '-' \
    "$(nc_clean_named)" "$(nc_clean_labelled)" "$(nc_clean_refs)" "$(nc_clean_owned)"
  # A THIRD DEPLOYMENT THE CR OWNS. The union no longer exhausts: three objects wear the agent
  # label, three are owner-referenced. This is the row 09 §6.8's "no third workload" clause is for.
  nc_topology third-deployment-owned-by-the-cr nonzero 'the pair is exactly two' \
    "$(nc_clean_named)" \
    "$(nc_clean_labelled; printf '%s\n' "$NC_A-sidecar")" \
    "$(nc_clean_refs; printf '%s\tAgent/%s/%s\n' "$NC_A-sidecar" "$NC_A" "$NC_UID_A")" \
    "$(nc_clean_owned; printf 'deployments/%s\n' "$NC_A-sidecar")"
  # A THIRD WORKLOAD THE CONTROLLER MINTED WITHOUT LABELLING IT. Invisible to the label count by
  # construction; only the ownership sweep can see it, and this row is what proves the sweep runs.
  nc_topology third-workload-unlabelled nonzero 'workload object(s) in kubeagents-system are owner-referenced to' \
    "$(nc_clean_named)" "$(nc_clean_labelled)" "$(nc_clean_refs)" \
    "$(nc_clean_owned; printf 'statefulsets/%s\n' "$NC_A-cache")"
  # PAIR-2f's decoy, judged offline: labelled with the CR's agent key, owned by nothing.
  nc_topology unowned-decoy-wearing-the-agent-label nonzero "its ownerReferences are '', not Agent/$NC_A/" \
    "$(nc_clean_named)" \
    "$(nc_clean_labelled; printf '%s\n' "$DECOY")" \
    "$(nc_clean_refs)" "$(nc_clean_owned)"
  # THE PAIR IS OWNED BY THE OTHER CR. Same count, same names, same labels — only the uid moves,
  # which is the whole reason clause (b) compares uids and not kind-and-name.
  nc_topology pair-owned-by-another-cr nonzero "not Agent/$NC_A/$NC_UID_A" \
    "$(nc_clean_named)" "$(nc_clean_labelled)" \
    "$(printf '%s\tAgent/%s/%s\n%s\tAgent/%s/%s\n' \
      "$NC_A-gateway" "$NC_B" 22222222-2222-4222-8222-222222222222 \
      "$NC_A-broker" "$NC_B" 22222222-2222-4222-8222-222222222222)" \
    "$(nc_clean_owned)"
  nc_topology half-a-pair nonzero "no Deployment named $NC_A-broker in" \
    "$(printf '%s\n' "$NC_A-gateway")" "$(printf '%s\n' "$NC_A-gateway")" \
    "$(printf '%s\tAgent/%s/%s\n' "$NC_A-gateway" "$NC_A" "$NC_UID_A")" \
    "$(printf 'deployments/%s\n' "$NC_A-gateway")"

  echo
  echo "-- V-RUN-002: the identity on each half (judge_pair_identity) --"
  nc_score clean-identities green '-' -- \
    judge_pair_identity "$NC_A" platform platform-agent platform-kage-actor \
    platform-agent platform-kage-actor "$(printf 'platform-agent\nplatform-kage-actor\n')"
  # THE LOAD-BEARING ROW. Both halves run as the reader SA and the CR's own status has collapsed
  # onto it too, so the three arms that merely READ a name are all satisfied — the gateway matches
  # its CR field, the field is the `<tier>-agent` form, the broker matches what the status
  # publishes. Only the arm that COMPARES the two can see it, which is why this row demands exactly
  # one red line: a control that accepted "it went red somewhere" would score this identically to a
  # suite that could not read either Deployment.
  nc_score both-halves-run-as-one-serviceaccount red1 'the split not existing' -- \
    judge_pair_identity "$NC_A" platform platform-agent platform-agent \
    platform-agent platform-agent "$(printf 'platform-agent\n')"
  nc_score gateway-runs-as-the-actor red "but its CR names reader SA 'platform-agent'" -- \
    judge_pair_identity "$NC_A" platform platform-agent platform-kage-actor \
    platform-kage-actor platform-kage-actor "$(printf 'platform-kage-actor\n')"
  # The `<tier>-agent` arm alone: the CR names its own metadata.name as the reader SA — the
  # `agent_manifests.go:331-332` fallback — which webhook rule V-10 is supposed to make impossible.
  nc_score reader-is-not-the-tier-agent-form red1 "is not 'cluster-admin-agent'" -- \
    judge_pair_identity "$NC_B" cluster-admin "$NC_B" cluster-admin-a-actor \
    "$NC_B" cluster-admin-a-actor "$(printf '%s\ncluster-admin-a-actor\n' "$NC_B")"
  nc_score broker-sa-names-no-object red1 'which does not exist in kubeagents-system' -- \
    judge_pair_identity "$NC_A" platform platform-agent platform-kage-actor \
    platform-agent platform-kage-actor "$(printf 'platform-agent\n')"
  nc_score broker-template-carries-no-sa red1 "runs as '<unset>'" -- \
    judge_pair_identity "$NC_A" platform platform-agent platform-kage-actor \
    platform-agent "" "$(printf 'platform-agent\n')"

  echo
  echo "-- V-RUN-004: five keys on one target, and the scope carry (judge_target_labels) --"
  nc_score clean-labels-with-an-empty-parent green '-' -- \
    nc_probe_labels "Deployment $NC_A-gateway" "$(nc_labels reader adamparco-kage)" reader
  # ONE OF THE FIVE KEYS IS GONE. Not merely empty — gone. This is the case a
  # `jsonpath={.metadata.labels.kube-agents/parent}` read cannot see, because it returns "" for an
  # absent key and "" for the legitimately-empty one directly above.
  nc_score one-of-the-five-keys-absent red1 'does not carry the key kube-agents/parent at all' -- \
    nc_probe_labels "Deployment $NC_A-gateway" \
    "$(nc_labels reader adamparco-kage | grep -v '^kube-agents/parent=')" reader
  nc_score role-label-stamped-with-the-wrong-value red1 "but the CR says it should be 'actor'" -- \
    nc_probe_labels "Deployment $NC_A-broker" "$(nc_labels reader adamparco-kage)" actor
  nc_score target-carries-no-labels-at-all red1 'has no labels at all' -- \
    nc_probe_labels "Deployment $NC_A-gateway" "" reader
  nc_score hashed-scope-identical-across-the-pair green '-' -- \
    nc_probe_scope_pair agents-3f2a1b9c04 agents-3f2a1b9c04
  nc_score hashed-scope-drifts-within-the-pair red1 'two scope renderings' -- \
    nc_probe_scope_pair agents-3f2a1b9c04 agents-9d81c7e520

  echo
  echo "-- V-RUN-004: the two CRs are in different scopes (judge_scope_distinct) --"
  nc_score distinct-scopes green '-' -- \
    judge_scope_distinct adamparco-kage adamparco-kage.cluster-a
  # A RenderScope that returned a constant satisfies every per-object arm above and only this one.
  nc_score both-crs-share-one-scope red1 'does not distinguish scopes' -- \
    judge_scope_distinct adamparco-kage adamparco-kage

  echo
  echo "-- V-RUN-004: 'and selectable', cluster-wide (judge_selector_exact) --"
  nc_score selector-returns-exactly-the-owned-set green '-' -- \
    judge_selector_exact kube-agents/role=actor 'broker pods' \
    "$(printf '%s\n' "$NS/$NC_A-broker-abc" "$NS/$NC_B-broker-def")" \
    "$(printf '%s\n' "$NS/$NC_A-broker-abc" "$NS/$NC_B-broker-def")"
  nc_score selector-returns-a-stranger red1 'resolved by ownership are' -- \
    judge_selector_exact kube-agents/role=actor 'broker pods' \
    "$(printf '%s\n' "$NS/$NC_A-broker-abc" "$NS/$NC_B-broker-def")" \
    "$(printf '%s\n' "$NS/$NC_A-broker-abc" "$NS/$NC_B-broker-def" "$NS/somebody-elses-pod")"
  # The vacuity arm: a selector compared against an empty expected set agrees with everything.
  nc_score selector-has-nothing-to-be-exact-about red1 'has nothing to be exact about' -- \
    judge_selector_exact kube-agents/role=actor 'broker pods' "" ""

  echo
  echo "-- V-RUN-009: what the delete takes and what it leaves --"
  nc_score cascade-removed-the-workload green '-' -- \
    judge_workload_gone "$NC_A" gateway 1 5
  nc_score workload-outlived-its-cr red1 'still exists 180s after Agent' -- \
    judge_workload_gone "$NC_A" gateway 0 180
  nc_score identity-survived-with-the-same-uid green '-' -- \
    judge_sa_survival "$NC_A" platform-agent "$NC_UID_A" "$NC_UID_A"
  # SAME NAME, NEW UID. The row this arm exists for: an SA the next reconcile deleted and recreated
  # answers a bare `get` yes, and every token minted against the old one is already invalid. An arm
  # that compared NAMES would score this as survival and 08 §7 would read green through it.
  nc_score identity-recreated-under-the-same-name red1 'its uid changed across the delete' -- \
    judge_sa_survival "$NC_A" platform-agent "$NC_UID_A" 33333333-3333-4333-8333-333333333333
  nc_score identity-garbage-collected-with-the-cr red1 'was REMOVED when Agent' -- \
    judge_sa_survival "$NC_A" platform-agent "$NC_UID_A" ""
  nc_score watchable-identity-is-not-flagged silent '-' -- \
    judge_sa_watchable "$NC_A" platform-agent "$NC_UID_A"
  nc_score identity-absent-before-the-delete red1 'does not exist BEFORE the delete' -- \
    judge_sa_watchable "$NC_A" platform-agent ""
  nc_score no-identity-name-to-watch red1 'has no ServiceAccount name to watch' -- \
    judge_sa_watchable "$NC_A" "" ""
  nc_score present-workload-is-not-flagged silent '-' -- \
    judge_deploy_present_before "$NC_A" gateway "$NC_UID_A"
  nc_score workload-absent-before-the-delete red1 'is already absent BEFORE the delete' -- \
    judge_deploy_present_before "$NC_A" gateway ""

  rm -f "$NC_ERR"

  echo
  echo "===================================================================="
  echo " negative control: $nc_caught/$nc_total"
  if [ "$nc_rc" -eq 0 ]; then
    echo " NEGATIVE CONTROL PASSED — every synthesised defect was rejected by the arm that targets"
    echo " it, and every correct input was accepted. The four rows' live greens are measurements."
    echo " NOT COVERED BY THIS MODE: see the NEGATIVE CONTROL DOES NOT EXERCISE block in the header."
    echo "===================================================================="
    return 0
  fi
  echo " NEGATIVE CONTROL FAILED — a MISS is an arm that cannot see its own defect; a BROKEN is a"
  echo " row that was never an experiment. They call for opposite repairs ([[LSN-063]])."
  echo "===================================================================="
  return 1
}

if [ "$MODE" = negative-control ]; then
  echo "===================================================================="
  echo " workload-pair-l2.sh --negative-control — the mandatory ¬ for V-RUN-001 and V-RUN-002"
  echo " (09 §6, V-MET-014), extended over V-RUN-004 and V-RUN-009 in the same suite"
  echo " Can the judgement functions tell a correctly rendered pair from a broken one?"
  echo "===================================================================="
  run_negative_control
  exit $?
fi

echo "===================================================================="
echo " V-RUN-001 / V-RUN-002 / V-RUN-004 / V-RUN-009 at L2"
echo " the rendered workload pair, deployed — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/parent-chain.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

seeded=()
cleanup() {
  # Idempotent, and it has to be: PAIR-5 deletes these same CRs as its experiment, so on a complete
  # run this trap finds nothing left and must say nothing alarming about that.
  $K -n "$NS" delete deploy "$DECOY" --ignore-not-found --wait=false >/dev/null 2>&1
  unseed_parent_agents "$K" "${seeded[@]:-}"
  echo
  echo "CLEANED UP: both Agent CRs and the decoy Deployment are gone; their Deployments, Services"
  echo "  and pods go with them by ownerReference. The four ServiceAccounts and the 06 §2.2.1 grant"
  echo "  are LEFT STANDING — that is not tidiness, it is the property V-RUN-009 just measured, and"
  echo "  deleting them here would also change what the next suite in the chain runs against."
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT

# ------------------------------------------------------------------------------------------------
# Small readers. Each one pins ONE object and reads from it; none of them re-list a moving set.
# ------------------------------------------------------------------------------------------------

# labels_of <resource> — the object's own labels as `key=value` lines.
#
# go-template and not jsonpath, because the question "is this key PRESENT" is not the question "is
# its value non-empty", and `kube-agents/parent` is empty by design on a platform agent
# (agentlabels.parentOf: "absent means this controller did not stamp it, empty means this agent is
# a root"). `jsonpath='{.metadata.labels.kube-agents/parent}'` returns the same empty string for
# both, so a check built on it cannot see a controller that stopped stamping the key at all.
labels_of() {
  # shellcheck disable=SC2016  # `$k`/`$v` are go-template variables; single quotes are required.
  $K -n "$NS" get "$1" \
    -o go-template='{{range $k, $v := .metadata.labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' 2>/dev/null
}

# template_labels_of <deployment> — the POD TEMPLATE's labels, same shape.
# This is the copy that matters most: `vap-agent-pod-hardening` and the 03 §4.2 pod-to-SA pinning
# select pods, not Deployments, so a pair labelled only on its Deployments is unpinned at runtime.
template_labels_of() {
  # shellcheck disable=SC2016  # `$k`/`$v` are go-template variables; single quotes are required.
  $K -n "$NS" get "deploy/$1" \
    -o go-template='{{range $k, $v := .spec.template.metadata.labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' 2>/dev/null
}

# `label_present`, `label_value` and `expect_label` used to live here. They are pure — they read a
# label blob that has already been fetched — so they moved up into the judgement block above, where
# `--negative-control` can reach them without a cluster.

# owner_refs_of <resource> — `kind/name/uid` lines for every ownerReference on the object.
owner_refs_of() {
  $K -n "$NS" get "$1" \
    -o go-template='{{range .metadata.ownerReferences}}{{.kind}}/{{.name}}/{{.uid}}{{"\n"}}{{end}}' 2>/dev/null
}

# deploy_names_labelled <selector> — Deployment names in $NS matching a label selector.
# `{range .items[*]}` and never `{.items[N]}`: the index form re-resolves a moving list once per
# field, which is LSN-024's signature.
deploy_names_labelled() {
  $K -n "$NS" get deploy -l "$1" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -v '^$'
}

# owned_workloads <uid> — `<kind>/<name>` for every workload object in $NS owned by that uid.
# Kinds are iterated one at a time rather than passed as a comma list, because `-o go-template` over
# a multi-resource get returns items whose `kind` is only sometimes populated, and a check that
# reads an empty kind reports every workload as `/name`.
owned_workloads() {
  local uid="$1" kind
  for kind in deployments statefulsets daemonsets jobs cronjobs; do
    $K -n "$NS" get "$kind" \
      -o go-template="{{range .items}}{{\$n := .metadata.name}}{{range .metadata.ownerReferences}}{{if eq .uid \"$uid\"}}$kind/{{\$n}}{{\"\\n\"}}{{end}}{{end}}{{end}}" 2>/dev/null
  done | grep -v '^$'
}

# sa_names — every ServiceAccount name in $NS, sorted.
sa_names() {
  $K -n "$NS" get serviceaccounts \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -v '^$' | sort
}

# actor_sa_of <agent> — the actor ServiceAccount name the CR PUBLISHES, polled (P9).
#
# Read from `status.broker.actorServiceAccount` rather than re-derived from tier+leaf: the
# derivation already has a Go definition site (`broker_manifests.go:187-201`) and a bash one in
# `k8s-operator/scripts/common.sh`, and a third here would be a third thing to keep in step
# (V-MET-013). The status field is also what the broker pod's SA is actually built from, which makes
# this the runtime-authoritative read (P6) rather than a guess that happens to agree.
actor_sa_of() {
  local a="$1" timeout="${2:-90}" waited=0 v=""
  while [ "$waited" -le "$timeout" ]; do
    v="$($K -n "$NS" get agent "$a" -o jsonpath='{.status.broker.actorServiceAccount}' 2>/dev/null)"
    [ -n "$v" ] && break
    sleep 3
    waited=$((waited + 3))
  done
  [ -n "$v" ] || return 1
  printf '%s\n' "$v"
}

# ------------------------------------------------------------------------------------------------
# PAIR-1: preconditions, then the fixture
# ------------------------------------------------------------------------------------------------
echo; echo "== PAIR-1: the build under test, then two shipped Agent CRs applied as shipped =="

p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable for the controller (see above). The controller renders the pair,"
     echo "  sets its ownerReferences and stamps its labels, so none of V-RUN-001/002/004/009 would"
     echo "  be evidence about this commit."
     exit 3 ;;
  *) bad "P1: the controller is not running the build under test"; exit 1 ;;
esac

# The ServiceAccount snapshot is taken HERE, before a single CR exists, so that PAIR-2e can attribute
# every ServiceAccount that appears during the run. Taken after this point it would already contain
# whatever the controller minted, which is the thing being looked for.
SA_BEFORE="$(sa_names)"
echo "  $(printf '%s\n' "$SA_BEFORE" | grep -c .) ServiceAccount(s) in $NS before any Agent CR exists"

for m in "$PLATFORM_MANIFEST" "$CLUSTER_ADMIN_MANIFEST"; do
  # Order matters: the cluster-admin CR's parentRef names the platform agent, and 06 §1.2 V-6
  # rejects a child whose parent does not exist.
  # Identity read off the manifest rather than taken as a constant here, for the reason
  # parent-chain.sh:55-58 gives: a caller that names the object separately is a second definition
  # site that drifts the first time the tree is renamed.
  name="$(sed -n 's|^  name: *||p' "$m" | head -1 | tr -d '"'"'"' \r')"
  mns="$(sed -n 's|^  namespace: *||p' "$m" | head -1 | tr -d '"'"'"' \r')"
  [ -n "$name" ] || { echo "FAIL: could not read metadata.name from $m" >&2; exit 1; }
  # CO-LOCATION IS THE FIXTURE, not a coincidence of the tree. If a manifest moves out of $NS the
  # exactness clauses stop being about one namespace and the delete below would target the wrong
  # one — refuse rather than silently measure something else.
  if [ "$mns" != "$NS" ]; then
    echo "FAIL: $m declares namespace '${mns:-<none>}'; this suite's fixture is both CRs co-located in $NS (08 §2.6)" >&2
    exit 1
  fi
  # P3: never reuse a leftover. A CR left by an earlier run was admitted by an earlier webhook and
  # rendered by an earlier controller, and every claim below would be about that generation.
  $K -n "$NS" delete agent "$name" --ignore-not-found >/dev/null 2>&1
  if out="$($K -n "$NS" apply -f "$m" 2>&1)"; then
    seeded+=("$NS/$name")
    echo "  applied $NS/$name from $m, AS SHIPPED (no scaleToZero injection — see the header)"
  else
    echo "FAIL: could not apply $m: $out" >&2
    exit 1
  fi
done

for a in "${AGENTS[@]}"; do
  seed_agent_fixtures "$K" "$NS" "$a" || { echo "FAIL: could not seed fixtures for $a" >&2; exit 1; }
  seed_agent_identity "$K" "$NS" "$a" || { echo "FAIL: could not seed the actor identity for $a" >&2; exit 1; }
done

# Both halves of both pairs, polled into existence (P9). The Deployments are controller-written and
# appear an unknown time after the CRs are accepted; the pods cannot be created until the
# ServiceAccounts seeded above exist, so the ReplicaSet controller retries with backoff and the
# first pod can be a minute behind the Deployment.
for a in "${AGENTS[@]}"; do
  uid="$($K -n "$NS" get agent "$a" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
  AGENT_UIDS+=("$uid")
  [ -n "$uid" ] || { bad "Agent CR $a has no uid; it was not admitted"; continue; }

  for half in gateway broker; do
    waited=0
    while :; do
      [ -n "$($K -n "$NS" get "deploy/$a-$half" -o jsonpath='{.metadata.uid}' 2>/dev/null)" ] && break
      if [ "$waited" -ge 180 ]; then
        bad "Deployment $a-$half never appeared after 180s. The controller renders both halves of the pair together (pod_launcher.go BuildPair), so a missing one is V-RUN-001 failing at the first clause"
        break
      fi
      sleep 5
      waited=$((waited + 5))
    done
  done

  # Pods pinned ONCE, by ownership (LSN-025). Not waited on for Ready and deliberately so: the
  # shipped image 403s, and every field read from these pods is in the spec.
  if pod="$(p3_pod_of_deploy "$K" "$NS" "$a-gateway" 240)"; then
    GATEWAY_PODS+=("$pod")
    pass "$a-gateway owns pod $pod (pinned by ownerReference, not by selector)"
  else
    GATEWAY_PODS+=("")
    bad "$a-gateway never produced a pod. Without a scaleToZero injection this Deployment is replicas=1, so no pod means the ServiceAccount admission plugin refused it — read: kubectl --context $CTX -n $NS describe deploy/$a-gateway"
  fi
  if pod="$(p3_pod_of_deploy "$K" "$NS" "$a-broker" 240)"; then
    BROKER_PODS+=("$pod")
    pass "$a-broker owns pod $pod (pinned by ownerReference, not by selector)"
  else
    BROKER_PODS+=("")
    bad "$a-broker never produced a pod — read: kubectl --context $CTX -n $NS describe deploy/$a-broker"
  fi
done

# ------------------------------------------------------------------------------------------------
# PAIR-2: V-RUN-001 — exactly two workloads, both owner-referenced, no third, no minted SA
# ------------------------------------------------------------------------------------------------
echo; echo "== PAIR-2: V-RUN-001 — exactly two workloads per Agent CR, both owned by it =="

# THE COLLECTION IS A FUNCTION TOO, because PAIR-2f re-runs it against an injected decoy: the decoy
# has to be seen by the same LISTING, not merely judged by the same rule, or the control would prove
# the predicate rejects a hand-written third row and say nothing about whether a real stray
# Deployment is visible to the selector at all. It reads the four inputs off the cluster and hands
# them to `judge_pair_topology`, which owns every opinion; a copy-pasted second version of that
# judgement here would be a negative control for a check that is not this one.
#
# The four reads are taken ONCE each and passed down, rather than re-run per clause. The previous
# shape called `deploy_names_labelled` twice and `owned_workloads` twice — once for the count and
# once for the message — which is a moving list re-resolved between two reads of it ([[LSN-024]]'s
# signature); the count and the names it names could disagree, and the disagreement would print.
pair_topology_problems() {
  local a="$1" uid="$2" named="" labelled refs_table="" dname

  local half
  for half in gateway broker; do
    named="$named$($K -n "$NS" get deploy "$a-$half" -o jsonpath='{.metadata.name}' 2>/dev/null)
"
  done

  labelled="$(deploy_names_labelled "kube-agents/agent=$a")"

  # `<deployment>\t<Kind>/<name>/<uid>` — a flat table because bash 3.2 has no associative arrays
  # (see the array note at the top of the file).
  while IFS= read -r dname; do
    [ -n "$dname" ] || continue
    refs_table="$refs_table$(owner_refs_of "deploy/$dname" | awk -v d="$dname" 'NF { print d "\t" $0 }')
"
  done <<EOF
$labelled
EOF

  judge_pair_topology "$a" "$uid" "$named" "$labelled" "$refs_table" "$(owned_workloads "$uid")"
}

for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  uid="${AGENT_UIDS[$i]:-}"
  [ -n "$uid" ] || { bad "V-RUN-001: no uid for $a; cannot judge its topology"; continue; }
  problems="$(pair_topology_problems "$a" "$uid")"
  if [ "$problems" = "0" ]; then
    pass "V-RUN-001: $a has exactly two workloads — $a-gateway and $a-broker — both owner-referenced to it by uid, and no third"
  else
    bad "V-RUN-001: $problems topology problem(s) for $a (listed above)"
  fi
done

# (e) NO MINTED SA. Two independent forms, because they fail in different ways.
echo
for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  uid="${AGENT_UIDS[$i]:-}"
  [ -n "$uid" ] || continue
  owned_sa="$($K -n "$NS" get serviceaccounts \
    -o go-template="{{range .items}}{{\$n := .metadata.name}}{{range .metadata.ownerReferences}}{{if eq .uid \"$uid\"}}{{\$n}}{{\"\\n\"}}{{end}}{{end}}{{end}}" 2>/dev/null | grep -v '^$')"
  if [ -z "$owned_sa" ]; then
    pass "V-RUN-001: no ServiceAccount in $NS is owner-referenced to $a — the controller minted none"
  else
    bad "V-RUN-001: the controller owns ServiceAccount(s) '$(printf '%s' "$owned_sa" | tr '\n' ' ')' in $NS. 08 §4 forbids the controller from minting identity: an SA it creates is one it can also change, and the actor grant would then be a thing the controller could widen"
  fi
done

# The attribution arm. Every SA that appeared between the snapshot and now must be one this script's
# own fixtures created — the two reader SAs named by the CRs, and the two actor SAs the CRs publish.
# A naive "the set did not change" would fail for the right reason on a fresh cluster and pass for
# the wrong one on a cluster where the identities were already installed.
for a in "${AGENTS[@]}"; do
  rs="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.security.serviceAccountName}' 2>/dev/null)"
  READER_SAS+=("$rs")
  if as_="$(actor_sa_of "$a")"; then
    ACTOR_SAS+=("$as_")
  else
    ACTOR_SAS+=("")
    bad "V-RUN-002: $a never published status.broker.actorServiceAccount; there is no actor identity to compare against"
  fi
done

expected_new="$(printf '%s\n' "${READER_SAS[@]}" "${ACTOR_SAS[@]}" | grep -v '^$' | sort -u)"
actual_new="$(comm -13 <(printf '%s\n' "$SA_BEFORE") <(sa_names))"
unattributed="$(comm -23 <(printf '%s\n' "$actual_new" | grep -v '^$') <(printf '%s\n' "$expected_new"))"
if [ -z "$unattributed" ]; then
  pass "V-RUN-001: every ServiceAccount that appeared during this run is one the fixtures created ($(printf '%s' "$actual_new" | grep -c . || true) new, all attributed)"
else
  bad "V-RUN-001: ServiceAccount(s) '$(printf '%s' "$unattributed" | tr '\n' ' ')' appeared in $NS and no fixture in this script created them. Something in the deployed system mints identity"
fi

# (f) THE ¬. An unowned Deployment wearing this CR's own agent label and the READER role — the shape
# a stray third workload actually has. `role=reader` and not `role=actor`: the actor half of this
# control already exists at broker-per-agent-l2.sh:394-426, and duplicating it would leave the
# reader half — the half this suite added — uncontrolled. `replicas: 0` because the claim under test
# is what the topology predicate SEES; a decoy that scheduled a pod would also have to be
# admissible and pullable, neither of which is the property being controlled for.
echo; echo "== PAIR-2f: negative control — an unowned third workload is caught =="
decoy_agent="${AGENTS[0]}"
decoy_uid="${AGENT_UIDS[0]:-}"
if [ -z "$decoy_uid" ]; then
  bad "V-RUN-001: no uid for $decoy_agent, so the negative control cannot run. A control that did not run is not a control (LSN-048)"
elif $K -n "$NS" apply -f - >/dev/null 2>&1 <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $DECOY
  namespace: $NS
  labels:
    kube-agents/agent: $decoy_agent
    kube-agents/role: reader
spec:
  replicas: 0
  selector:
    matchLabels:
      app: $DECOY
  template:
    metadata:
      labels:
        app: $DECOY
    spec:
      containers:
        - name: decoy
          image: registry.k8s.io/pause:3.10
YAML
then
  caught="$(pair_topology_problems "$decoy_agent" "$decoy_uid" 2>/dev/null)"
  if [ "$caught" != "0" ]; then
    pass "V-RUN-001: the pair predicate rejects an unowned Deployment labelled kube-agents/agent=$decoy_agent ($caught problem(s)) — the green above is a measurement, not a tidy namespace"
  else
    bad "V-RUN-001: the pair predicate accepted a third Deployment carrying kube-agents/agent=$decoy_agent and owned by no Agent CR. The 'exactly two' assertion above cannot fail, so its pass is not evidence (V-MET-014)"
  fi
  $K -n "$NS" delete deploy "$DECOY" --ignore-not-found >/dev/null 2>&1
else
  bad "V-RUN-001: could not create the decoy Deployment, so 'exactly two' is unmeasured for non-vacuity. A negative control that did not run is not a negative control (LSN-048)"
fi

# ------------------------------------------------------------------------------------------------
# PAIR-3: V-RUN-002 — the identity on each half
# ------------------------------------------------------------------------------------------------
echo; echo "== PAIR-3: V-RUN-002 — the reader SA on the gateway, the actor SA on the broker =="
echo "  the ¬ half of this row is asserted where admission lives, and is not rebuilt here:"
echo "    dev/verify/webhook-negatives-l2.sh:487-521 (V-10) rejects an actor SA, 'default' and"
echo "    another tier's reader SA on spec.security.serviceAccountName, with a positive control."
echo "    The converse is unrepresentable — the broker's SA is derived, never read from the spec"
echo "    (broker_manifests.go:187-201) — and V-CTR-003 is green on that derivation."

for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  reader="${READER_SAS[$i]:-}"
  actor="${ACTOR_SAS[$i]:-}"
  tier="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.tier}' 2>/dev/null)"
  : "${tier:=platform}"

  got_reader="$($K -n "$NS" get "deploy/$a-gateway" -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)"
  got_actor="$($K -n "$NS" get "deploy/$a-broker" -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null)"

  # Existence is resolved by asking for each name INDIVIDUALLY, not by listing the namespace and
  # taking the intersection: a `get serviceaccounts` that returned a partial list would make a
  # present identity look absent, and a per-name `get` cannot. The judgement then works from the
  # resolved subset, which is a value rather than a cluster.
  existing_sas=""
  for sa in "$got_reader" "$got_actor"; do
    [ -n "$sa" ] || continue
    if $K -n "$NS" get serviceaccount "$sa" >/dev/null 2>&1; then
      existing_sas="$existing_sas$sa
"
    fi
  done

  judge_pair_identity "$a" "$tier" "$reader" "$actor" "$got_reader" "$got_actor" "$existing_sas"
done

# ------------------------------------------------------------------------------------------------
# PAIR-4: V-RUN-004 — five labels on six objects, then selectability
# ------------------------------------------------------------------------------------------------
echo; echo "== PAIR-4: V-RUN-004 — the five 08 §2.5 labels, stamped and selectable =="
echo "  09 §6.8:477 abbreviates its own source to four keys; 05 §8:1341 ('Labels — five, not"
echo "  three'), 08 §2.5:196-206 and 08 §7 all name five, and agentlabels.For() renders five."
echo "  Five is asserted — a strict superset of the four-key reading."

# expected_scope_label <agent> — the scope label value, or the empty string with rc 1 when this
# script cannot say what it should be.
#
# This restates RenderScope's RULE 1 ONLY — the pass-through case, where the readable
# `<project>.<cluster>.<namespace>` join is itself the label. Rules 2 and 3 (truncate-and-digest)
# are deliberately NOT reimplemented: the digest has one definition site, V-RUN-011 is the check
# that it is injective, and a second bash implementation here would be a thing to keep in step for
# no gain. When rule 1 does not apply the caller falls back to the weaker claims — present,
# non-empty, identical across the pair, distinct between the two CRs — and says so out loud.
expected_scope_label() {
  local a="$1" proj clus nsx key="" p
  proj="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.scope.projectId}' 2>/dev/null)"
  clus="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.scope.clusterName}' 2>/dev/null)"
  nsx="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.scope.namespace}' 2>/dev/null)"
  # Well-formed: no hole in the middle. A malformed scope takes the hashed path (agentlabels
  # RenderScope rule 1's IsWellFormed clause), which this function does not model.
  { [ -n "$nsx" ] && [ -z "$clus" ]; } && return 1
  { [ -n "$clus" ] && [ -z "$proj" ]; } && return 1
  for p in "$proj" "$clus" "$nsx"; do
    [ -n "$p" ] || continue
    # A plain DNS-1123 label, or the join is ambiguous and the hashed path is taken.
    case "$p" in *[!a-z0-9-]* | -* | *-) return 1 ;; esac
    key="${key:+$key.}$p"
  done
  [ ${#key} -le 63 ] || return 1
  # Rule 3: a value that already LOOKS hashed is pushed into the hashed set.
  case "$key" in
    *-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) return 1 ;;
  esac
  printf '%s\n' "$key"
}

SCOPE_LABELS=()
for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  tier="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.tier}' 2>/dev/null)"
  : "${tier:=platform}"
  parent="$($K -n "$NS" get agent "$a" -o jsonpath='{.spec.parentRef.name}' 2>/dev/null)"
  if want_scope="$(expected_scope_label "$a")"; then
    scope_exact=1
  else
    want_scope=""
    scope_exact=0
    echo "  NOTE: $a's scope takes agentlabels.RenderScope's hashed path; asserting presence,"
    echo "    cross-object identity and inter-CR distinctness rather than a re-derived value."
  fi

  # Six objects: two Deployments, two pod templates, two Services. Plus the two live pods, which is
  # where 03 §4.2's pod-to-SA pinning and vap-agent-pod-hardening actually read.
  #                    description                        how to read it              role
  targets_desc=("Deployment $a-gateway" "Deployment $a-broker" \
    "pod template of $a-gateway" "pod template of $a-broker" \
    "Service $a" "Service $a-broker" \
    "pod ${GATEWAY_PODS[$i]:-<none>}" "pod ${BROKER_PODS[$i]:-<none>}")
  targets_blob=("$(labels_of "deploy/$a-gateway")" "$(labels_of "deploy/$a-broker")" \
    "$(template_labels_of "$a-gateway")" "$(template_labels_of "$a-broker")" \
    "$(labels_of "svc/$a")" "$(labels_of "svc/$a-broker")" \
    "$([ -n "${GATEWAY_PODS[$i]:-}" ] && labels_of "pod/${GATEWAY_PODS[$i]}")" \
    "$([ -n "${BROKER_PODS[$i]:-}" ] && labels_of "pod/${BROKER_PODS[$i]}")")
  targets_role=(reader actor reader actor reader actor reader actor)

  # Reset PER CR. The carry is a claim about one pair rendering one scope; leaking it across the two
  # CRs would make the second pair agree with the first and turn the distinctness arm below into the
  # only thing standing between a constant RenderScope and a green run.
  SEEN_SCOPE=""
  for t in $(seq 0 $((${#targets_desc[@]} - 1))); do
    judge_target_labels "${targets_desc[$t]}" "${targets_blob[$t]}" \
      "$tier" "$want_scope" "$parent" "${targets_role[$t]}" "$a" "$scope_exact"
  done
  SCOPE_LABELS+=("$(label_value kube-agents/scope "${targets_blob[0]}")")
done

# The two CRs are in different scopes (a project-scoped platform agent and a cluster-scoped
# cluster-admin agent), so their scope labels must differ. Without this, a RenderScope that returned
# a constant would satisfy every assertion above.
judge_scope_distinct "${SCOPE_LABELS[0]:-}" "${SCOPE_LABELS[1]:-}"

# --- "and selectable", cluster-wide -------------------------------------------------------------
echo; echo "== PAIR-4b: V-RUN-004 — the selectors return exactly the pods they name =="

# EXPECTED sets are built by OWNERSHIP, not by the label under test — otherwise this compares a
# selector against itself and cannot fail (V-MET-014). For every Agent CR on the cluster, the pod of
# `<name>-<half>` is resolved through p3_pod_of_deploy, which walks Deployment uid -> ReplicaSet ->
# Pod. Deployments at replicas 0 contribute nothing, which is how a scaleToZero CR seeded by another
# suite is accounted for rather than reported as a discrepancy. One pod per Deployment is exact
# here: brokerReplicas is a const 1 and the gateway default is 1.
expected_pods_for_half() {
  local half="$1" ref ns name reps pod
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    ns="${ref%%/*}"
    name="${ref#*/}"
    reps="$($K -n "$ns" get "deploy/$name-$half" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
    [ "${reps:-0}" != "0" ] || continue
    if pod="$(p3_pod_of_deploy "$K" "$ns" "$name-$half" 60)"; then
      echo "$ns/$pod"
    fi
  done <<EOF
$($K get agents -A -o go-template='{{range .items}}{{.metadata.namespace}}/{{.metadata.name}}{{"\n"}}{{end}}' 2>/dev/null)
EOF
}

pods_selected_by() {
  $K get pods -A -l "$1" \
    -o go-template='{{range .items}}{{.metadata.namespace}}/{{.metadata.name}}{{"\n"}}{{end}}' 2>/dev/null |
    grep -v '^$' | sort
}

for pair in "reader:gateway" "actor:broker"; do
  role="${pair%%:*}"
  half="${pair#*:}"
  judge_selector_exact "kube-agents/role=$role" "$half pods" \
    "$(expected_pods_for_half "$half" | sort)" "$(pods_selected_by "kube-agents/role=$role")"
done

for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
  a="${AGENTS[$i]}"
  gw="${GATEWAY_PODS[$i]:-}"
  bk="${BROKER_PODS[$i]:-}"
  if [ -z "$gw" ] || [ -z "$bk" ]; then
    bad "V-RUN-004: $a has no pinned pair of pods, so 'kube-agents/agent=$a returns exactly its two pods' cannot be judged"
    continue
  fi
  judge_selector_exact "kube-agents/agent=$a" "own two pods of $a" \
    "$(printf '%s\n%s\n' "$NS/$gw" "$NS/$bk" | sort)" "$(pods_selected_by "kube-agents/agent=$a")"
done

# ------------------------------------------------------------------------------------------------
# PAIR-5: V-RUN-009 — deleting the CR removes both workloads and leaves both SAs intact
# ------------------------------------------------------------------------------------------------
#
# A NAMED FUNCTION, CALLED EXPLICITLY, NEVER A TRAP. broker-per-agent-l2.sh:145-154 performs exactly
# this experiment in its EXIT trap and asserts nothing about it — line 150 states the property as an
# `echo`, which is LSN-019 in one line. A trap cannot be the assertion site: it runs after the
# verdict has been printed, its `bad` calls cannot change the exit status the caller already sees,
# and on any early `exit` above it runs against a fixture that was never established.
assert_deletion_leaves_identities() {
  local i a sa uid before_uid after_uid waited half gone

  echo; echo "== PAIR-5: V-RUN-009 — delete the CRs; the workloads go, the identities stay =="

  # NON-VACUITY FIRST. "The ServiceAccounts survived" is trivially true of ServiceAccounts that were
  # never there, and "the Deployments are gone" is trivially true of Deployments that never existed.
  # Both preconditions are re-established here so this function cannot pass on an empty namespace.
  SA_UIDS_BEFORE=()
  for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
    a="${AGENTS[$i]}"
    for sa in "${READER_SAS[$i]:-}" "${ACTOR_SAS[$i]:-}"; do
      before_uid=""
      [ -n "$sa" ] && before_uid="$($K -n "$NS" get serviceaccount "$sa" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
      SA_UIDS_BEFORE+=("$before_uid")
      judge_sa_watchable "$a" "$sa" "$before_uid"
    done
    for half in gateway broker; do
      judge_deploy_present_before "$a" "$half" \
        "$($K -n "$NS" get "deploy/$a-$half" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
    done
  done

  for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
    a="${AGENTS[$i]}"
    uid="${AGENT_UIDS[$i]:-}"
    $K -n "$NS" delete agent "$a" --ignore-not-found --wait=false >/dev/null 2>&1
    echo "  deleted Agent $NS/$a (uid ${uid:-<unknown>})"
  done

  # BOUNDED POLL (P9), never a bare read. Garbage collection of ownerReferenced children is
  # asynchronous and unrelated to when the delete call returns; reading once here would report
  # "still present" on a fast cluster and "gone" on a slow one for the same code.
  for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
    a="${AGENTS[$i]}"
    for half in gateway broker; do
      waited=0
      gone=0
      while [ "$waited" -le 180 ]; do
        if [ -z "$($K -n "$NS" get "deploy/$a-$half" -o jsonpath='{.metadata.uid}' 2>/dev/null)" ]; then
          gone=1
          break
        fi
        sleep 5
        waited=$((waited + 5))
      done
      judge_workload_gone "$a" "$half" "$gone" "$waited"
    done
  done

  # And the identities. `get`s cleanly AND the same uid: 08 §7 says the SAs are LEFT, and an SA that
  # was deleted and recreated by the next reconcile would answer a bare existence check yes while
  # every token issued against the old one had already been invalidated.
  local n=0
  for i in $(seq 0 $((${#AGENTS[@]} - 1))); do
    a="${AGENTS[$i]}"
    for sa in "${READER_SAS[$i]:-}" "${ACTOR_SAS[$i]:-}"; do
      before_uid="${SA_UIDS_BEFORE[$n]:-}"
      n=$((n + 1))
      [ -n "$sa" ] || continue
      after_uid="$($K -n "$NS" get serviceaccount "$sa" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
      judge_sa_survival "$a" "$sa" "$before_uid" "$after_uid"
    done
  done
}

assert_deletion_leaves_identities

echo
if [ "$fail" -eq 0 ]; then
  echo "V-RUN-001 at L2: PROVEN — exactly two workloads per Agent CR, both owner-referenced to it by"
  echo "  uid, no third workload owned by it, no ServiceAccount minted, and the predicate was shown"
  echo "  to reject an injected unowned third workload."
  echo "V-RUN-002 at L2: PROVEN — the gateway runs as the tier's reader SA, the broker as the"
  echo "  <tier>-<leaf>-actor SA the CR publishes, the two are different, and both resolve. The ¬ is"
  echo "  webhook-negatives-l2.sh V-10 (cited, not rebuilt)."
  echo "V-RUN-004 at L2: PROVEN — all five 08 §2.5 labels present with the expected values on both"
  echo "  Deployments, both pod templates, both pods and both Services of both pairs, and the"
  echo "  role/agent selectors return exactly the pods they name, cluster-wide."
  echo "V-RUN-009 at L2: PROVEN — deleting each CR removed both of its workloads and left both of"
  echo "  its ServiceAccounts standing, same uid."
  exit 0
fi
echo "V-RUN-001 / V-RUN-002 / V-RUN-004 / V-RUN-009 at L2: FAILED — see the FAIL lines above."
exit 1
