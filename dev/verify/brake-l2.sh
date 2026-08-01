#!/usr/bin/env bash
# V-RUN-007 · V-CTR-007 · V-RUN-008 at L2 — the brake, against a deployed broker (09 §6, 08 §2.4,
# 06 §4.4, 03 §6).
#
# THE THREE ROWS, verbatim from `docs/design/09-verification-and-validation.md` §6:
#   480 | V-RUN-007 | `pause` is **not** implemented by scaling the agent to zero — the pod keeps
#                     observing ¬                                              | 08 §2.4 | L2 | 9 |
#   481 | V-RUN-008 | The brake works with the controller down and with inference down ¬
#                                                                              | 03 §6   | L2 | 9 |
#   495 | V-CTR-007 | Brake objects behave per contract, including fail-closed on unreadable
#                     `FleetFreeze` ¬                                          | 06 §4.4 | L2 | 9 |
#
# WHY ONE FILE AND NOT THREE. `docs/build/phase-9.md:7604`: "They share every fixture: a deployed
# pair, a paused `Agent`, a `FleetFreeze`, the controller scaled to 0, and an RBAC revoke/restore
# cycle `broker-refuse-l2.sh` arms B/C already implement. Splitting them stands the same fixture up
# three times." The three rows are also not independent — V-RUN-008 is V-RUN-007's control and
# V-CTR-007's control run with the control plane removed, and that is only meaningful if the same
# submission path answered differently five minutes earlier with the control plane present.
#
# WHAT WAS THERE BEFORE, and why none of it is these rows. `pause_not_scale_to_zero_test.go:29`
# names V-RUN-007 in a comment — "V-RUN-012 / V-RUN-007: 'pause' is structurally not a
# scale-to-zero" — and then every assertion message in the file cites V-RUN-012 alone;
# `dev/tests/pause-is-not-scale-to-zero.py` says in its own words that it is "V-RUN-012 (the L0
# half)". Both are claims about what the RENDERER does with a manifest. Neither pauses a running
# agent, and a structural argument about a template is not the row: 08 §2.4's sentence is about a
# pod that is still there afterwards. `verification/results.csv:71` and `:107` already say so — two
# `finding` rows, target `none`, recording that all three of these are L2, deliberately not counted
# as passes, and routed here.
#
# WHAT IS ASSERTED, in order:
#
#   A — V-RUN-007. `pause` closes the WRITE PATH and touches nothing else.
#     A-0  BASELINE. Unpaused, unfrozen, an envelope is ACCEPTED with an actionId. Without this the
#          six refusals below are compatible with a broker that refuses everything.
#     A-1  PAUSED ⇒ REFUSED. 403, reason `agent-paused` (`envelope.go:345`), decision `rejected`.
#     A-2  THE BROKER READ *THIS* OBJECT. `spec.operations.pauseReason` is set to a nonce minted by
#          this run, and the refusal detail carries it — `brake.go:479-482` appends it. A broker
#          answering from a global flag, a cached decision, or another agent's CR cannot produce a
#          string that did not exist before this process started.
#     A-3  THE DEPLOYMENT WAS NOT SCALED. `spec.replicas` on the agent's gateway Deployment is the
#          same non-zero number after the pause as before it. This is the row's literal sentence.
#     A-4  THE POD IS THE SAME POD. Same `metadata.uid`, same `status.startTime`, same
#          `status.phase` across the pause. A UID is assigned once; a restarted or replaced pod
#          cannot carry the old one, so this distinguishes "kept running" from "came back".
#     A-5  ¬ THE MEASUREMENT CAN FAIL. `spec.deployment.scaleToZero: true` is what a real
#          scale-to-zero looks like on this CRD (`manifest_helpers.go:211`, sole caller
#          `agent_manifests.go:326`): the gateway Deployment converges to 0 and no pod is owned by
#          it. A-3 and A-4 are therefore not statements that hold no matter what.
#     A-6  ¬ AND IT COMES BACK DIFFERENT. Restoring `scaleToZero: false` returns replicas to 1 with
#          a pod carrying a DIFFERENT uid. That is the contrast A-4 is claiming against: after a
#          scale cycle the uid moves, and after a pause it does not.
#     A-7  THE BRAKE RELEASES. Unpaused, the next envelope is ACCEPTED again.
#
#   B — V-CTR-007. The brake OBJECT behaves per 06 §4.4's contract, including its fail-closed row.
#     B-1  THE SERVED SCHEMA'S DEFAULTS. A FleetFreeze created without `allowUndo` reads back
#          `true`, and without `allowClasses` reads back empty. Both are load-bearing and in
#          opposite directions: `UndoAllowed()` defaults OPEN so a freeze cannot strand the fleet in
#          the state a human is trying to reverse, and `Allows()` defaults CLOSED so an empty list
#          refuses every class. Read off the object the API server stored, not off the Go tags.
#     B-2  ¬ THE CONTRACT REJECTS. Three malformed freezes are refused by the served API:
#          `requestedBy: nobody` (pattern `^(slack|googlechat|k8s):\S+$`), `allowClasses: [gated]`
#          (enum: `routine` only), and `reason: ""` (MinLength 1). Submitted with
#          `--dry-run=server`, which admits in full and persists nothing.
#     B-3  A COVERING FREEZE STOPS THE ACTION. 403, reason `scope-frozen`, and the detail names the
#          object — `frozenDetail()` at `brake.go:761` renders "FleetFreeze <name> covers this
#          scope: <reason> (no expiry; it is cleared by deleting the object)".
#     B-4  ¬ A NON-COVERING FREEZE DOES NOT. The same object with a different `scope.projectId` is
#          created, and the envelope is ACCEPTED. `Covers()` (`fleetfreeze_types.go:199`) is
#          therefore doing a comparison and not returning true; without this arm B-3 is satisfied by
#          a broker that refuses whenever any freeze exists anywhere in the cluster.
#     B-5  FAIL-CLOSED ON AN UNREADABLE FLEETFREEZE — 06 §4.4 row 1, and the clause the row names.
#          With NO freeze object in the cluster, `fleetfreezes` is revoked from every role object
#          actually bound to the actor, and the envelope is REFUSED 403 `scope-frozen` with row 1's
#          detail ("…undo still runs", `brake.go:749-759`) and NOT with B-3's. Two refusals with the
#          same reason code and different details is exactly the distinction the row is about: the
#          scope is treated as frozen when nothing is frozen.
#     B-6  IT DEGRADED WITHOUT RESTARTING. Same broker pod, same per-container restartCounts across
#          the fault. `brake/brake.go`'s cache "degrades into row 1 by itself… This is the opposite
#          arrangement from an informer, and deliberately so" — a broker that had to crash to notice
#          would satisfy B-5 and violate the design.
#     B-7  AND RECOVERS WITHOUT RESTARTING. The grant is put back and the next envelope is ACCEPTED,
#          still from the same pod with the same restartCounts.
#
#   C — V-RUN-008. The brake works with the CONTROLLER down and with INFERENCE down.
#     C-1  THE FAULT IS REAL. `kubeagents-controller-manager` and `kubeagents-brake-controller` are
#          scaled to 0, `kubeagents-webhook-service` has no endpoints, and an `Agent` UPDATE is
#          REJECTED by admission — `vagent.kb.io` is `failurePolicy: Fail` over
#          CREATE/UPDATE/DELETE on `agents` (`config/webhook/manifests.yaml`) and its Service
#          selects `control-plane: controller-manager`. A run that skipped this could scale a
#          Deployment that was already 0 and call the rest of the arm proven.
#     C-2  INFERENCE IS DOWN. The agent's gateway Deployment is scaled to 0 and no pod is owned by
#          it; `kubeagents-router` is scaled to 0 and its Service has no endpoints. 06 §4.4's
#          sentence is "no dependency on the model, the router, or the agent pod", and this removes
#          the two of those three that exist in-cluster.
#     C-3  THE BROKER STILL ANSWERS. With all of the above down, an envelope is ACCEPTED. The
#          positive control for C-4 and C-6: a refusal from a broker that had also fallen over is
#          not the brake working.
#     C-4  THE BRAKE IS ENGAGEABLE WITH THE CONTROLLER DOWN. A FleetFreeze is created — its webhook
#          is `failurePolicy: Ignore`, which is what makes the control reachable when the control
#          plane is not — and the next envelope is REFUSED 403 `scope-frozen`.
#     C-5  AND RELEASABLE. The freeze is deleted, still with the controller down, and the next
#          envelope is ACCEPTED.
#     C-6  PAUSE IS ENFORCED WITH THE CONTROLLER DOWN. The pause is staged in a brief
#          controller-up window (see the NOTE below), the controller is removed again, and the
#          envelope is REFUSED 403 `agent-paused`.
#
# NOTE, AND IT IS NOT A FAILURE: with the controller at 0 the Agent CR cannot be PATCHED at all —
# `vagent.kb.io` fails closed over UPDATE, so `paused: true` cannot be SET during the outage. That
# is a property of admission, not of the brake, and 09 §6's row scopes the claim to the brake
# WORKING (03 §6's "the brake works with the controller down"), not to every control being
# invocable through the API server while the webhook's backend is gone. It is why C-4/C-5 use
# FleetFreeze — whose webhook is `Ignore` precisely so the fleet-wide stop stays reachable — and why
# C-6 stages its pause before the second outage rather than during it. The arm still proves the
# thing the row asks: enforcement does not depend on the controller.
#
# WHAT THIS DOES NOT CLAIM
#   THAT THE PAUSED POD IS STILL DOING USEFUL WORK. 08 §2.4's "the pod keeps observing" is a
#     behavioural clause and this suite proves the structural one: the pod is still there, unchanged
#     and unrestarted. Reading its observation loop needs a gateway that reaches Running, and the
#     shipped `platform-agent` tag is not published to a registry this cluster can pull from
#     (`examples/gitops-repo/fleet/platform-agent.yaml`, `v0.1.0` — the pod sits in ImagePullBackOff
#     on any cluster that has not been handed an override). A-4 is written against uid, startTime
#     and phase for that reason: all three are answerable about a pod that never pulled, and a
#     scale-to-zero moves all three.
#   THE OTHER SEVEN FAIL-CLOSED ROWS OF 06 §4.4. Rows 2 and 3 are `broker-refuse-l2.sh`'s (agent
#     unreadable, journal unreachable); rows 4-9 are fault injection inside the pipeline and belong
#     to the units that can stage them. This file is row 1 plus the two controls.
#   THAT A REAL MUTATION WAS STOPPED. Phase 9 is a shadow: `spec.dryRun` is true on every submission
#     and the terminal phase is `DryRun`. What is measured is the broker's DECISION, which is where
#     06 §4.4 puts the brake — `decideGate` runs before the executor, so a refusal here is the same
#     refusal a non-shadow run would get. Proving that the stopped action would otherwise have
#     landed needs an execution path Phase 9 does not have.
#   ANY CLAIM ABOUT `stop`. 08 §2.4's third control is not one of these three rows.
#   IMAGE PARITY FOR THE AGENT SIDE. The driver pod mounts `agents/platform/scripts/` from the
#     working tree, for the reason `broker-auth-l2.sh` gives. P1 covers the two binaries whose
#     behaviour is under test — the controller and the broker.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent`, SCALES THE OPERATOR'S OWN CONTROLLER AND THE ROUTER TO ZERO, revokes an RBAC
# grant out from under a running broker, creates cluster-scoped FleetFreeze objects that stop every
# agent in the fleet, and widens an identity over a throwaway namespace. On the live install that is
# a test that pauses production, blinds the control plane, and freezes the fleet.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1, or a fault that could
# not be staged — an inability to run the experiment is never a red).
# Usage: dev/verify/brake-l2.sh [kube-context]
#        dev/verify/brake-l2.sh --negative-control
#
# ------------------------------------------------------------------------------------------------
# THE OFFLINE `¬` ARM — WHY THIS FILE NEEDS ONE WHEN IT ALREADY CARRIES SEVEN
# ------------------------------------------------------------------------------------------------
# 09 §6 marks all three of this file's rows with `¬` — 480, 481 and 495 each end in it — and
# V-MET-014 reads that as "a negative control is mandatory: break the property, watch the check
# fail", satisfied by a declared control or by an argued exemption and by nothing else.
#
# THIS FILE ALREADY HAS SEVEN IN-RUN `¬` ARMS, AND NONE OF THEM IS THAT CONTROL. A-5 (`scaleToZero`
# really does drain the gateway), A-6 (and the pod that comes back carries a different uid), B-2
# (the served API refuses three malformed FleetFreezes), B-4 (a freeze scoped to another projectId
# does not stop this agent), plus the three positive controls A-0, C-3 and the pre-strip
# `auth can-i`, are LIVE CLUSTER CONTRASTS. Each moves a real lever and watches a real answer
# change, and they are exactly why A-3, A-4, B-3 and B-5 are not sentences that hold no matter what.
# They are excellent and nothing below replaces them.
#
# They are also, all seven, the wrong instrument for two reasons. They cannot run in PR CI — every
# one needs a scratch GKE cluster, an operator, a deployed broker and an RBAC revoke — so on the
# merge path where these rows are read they measure nothing. And they are all scored by the SAME
# judging code as the arms they control: `expect_refused`, `expect_accepted` and a dozen string
# comparisons. A judgement that cannot go red carries its own controls green with it. B-4 is the
# sharpest case: it is the control for B-3, it is scored by `expect_accepted`, and an
# `expect_accepted` that accepted anything would report B-3 and B-4 both green while the broker
# refused every envelope in the cluster. That is the gap this mode fills. It is the complement of
# the seven, not a second copy of them: they ask whether the CLUSTER can answer differently, and
# this asks whether the SUITE can score differently.
#
# THE ARMS ARE REPLAYED, NEVER RE-STATED. Every judgement has been lifted out of the live collection
# that feeds it into a function over ALREADY-READ VALUES — `judge_accepted`, `judge_refused`,
# `judge_pause_nonce`, `judge_gateway_replicas`, `judge_same_pod`, `judge_scale_to_zero_drained`,
# `judge_scale_cycle_replaced_the_pod`, `judge_freeze_defaults`, `judge_malformed_freezes_refused`,
# `judge_broker_in_place`, `judge_outage_real`, `judge_inference_down`. The live path still does
# every read it did before and then calls in here to be judged; the control feeds those SAME
# functions synthesised inputs. It contains no second copy of any assertion, and that is the
# load-bearing property of the whole arm ([[LSN-024]] in its general form: a control that re-states
# the assertion is a second definition site, and the day the live arm is edited the copy stays green
# about the previous rule). Delete an arm and the control's row for it stops matching its needle,
# which is the failure that reports the deletion.
#
# EVERY RED ROW NAMES THE RULE THAT MUST CATCH IT ([[LSN-035]]). A row counts as caught only when a
# `FAIL:` line CONTAINS that row's needle. Most rows go further and demand EXACTLY ONE red line,
# because this suite's whole method is that two answers with the same status code mean different
# things: 403 `scope-frozen` is returned by 06 §4.4 row 1 AND by an ordinary covering freeze, and a
# broker that refused because it RESTARTED renders the identical reply to one that degraded in
# place. A control that accepted "it went red somewhere" would score those pairs the same, which is
# precisely the discrimination B-5 and B-6 exist to make.
#
# BROKEN IS NOT MISS ([[LSN-063]]). A row that was never an experiment is reported under its own
# word — a judgement that emitted no PASS and no FAIL (the arm was deleted or returns early), a red
# row whose synthesised arguments are byte-identical to the clean baseline for the same function
# (a mutation that stopped mutating), and a red row whose function has no clean baseline recorded at
# all (nothing establishes that the arm accepts anything, so "it went red" is not a finding). MISS
# invites strengthening a check; BROKEN calls for repairing the row. They are opposite repairs.
#
# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) The control hands synthesised strings straight
# to the judgement functions, so every statement that ACQUIRES those strings on a live run is
# bypassed and is unmeasured by the count it prints:
#   - every `kubectl` in this file. `field`'s awk over the driver transcript, `gateway_replicas`,
#     `gateway_pod_identity`, `broker_restarts`, `endpoint_count`, `replicas_of`, `p3_pod_of_deploy`
#     and the FleetFreeze `-o jsonpath` reads are all replaced by literals. A `jsonpath` that
#     silently returned "" — [[LSN-024]]'s exact shape — would give A-4 an empty identity live and a
#     full one here.
#   - THE SUBMISSION PATH. `submit()`, `broker_driver_run`, the driver pod, the probe, the JSON
#     flattener and `BROKER_DRIVER_TARGET_NAME="brake-l2-shadow-target-$SUBMIT_N"` — the [[LSN-067]]
#     fix itself — never run. The control proves the JUDGING rejects a `deduplicated` answer where
#     an arm wants a fresh acceptance; it cannot prove the transport still varies the target, and
#     nothing in this file asserts that `distinct(actionId) == submissions` (LSN-067's proposed
#     mechanization, which belongs in `broker_driver_run`).
#   - EVERY FAULT. No Agent is paused, no `scaleToZero` is set, no FleetFreeze is created or
#     deleted, no RBAC grant is stripped, nothing is scaled to zero. The synthesised inputs are the
#     ANSWERS those faults are supposed to produce; whether staging them produces those answers is
#     the live question and is untouched here. B-5's discovery-based role walk (`bound_role_refs`,
#     `drop_fleetfreeze_rules`) is the largest single bypass: it is the one piece of machinery whose
#     failure mode is "the fault was never staged and the arm passed anyway".
#   - THE SETTLE WINDOWS. `settle_brake`'s three cache TTLs and B-5's 40s past `MaxFreezeStaleness`
#     are the reason a live answer is about the control's CURRENT position. A control with no clock
#     in it cannot say whether either window is long enough.
#   - THE DEFERRAL BRANCHES. A-0's baseline triage, the `auth can-i` positive control, the
#     `stripped -eq 0` branch and every `exit 3` are live-only, and they are what stop this suite
#     reporting a red about an experiment it could not run.
#   - the destructive-test guard, `$K version`, P10, both P1 arms, and the `Available` poll. All
#     live-only, and P1 in particular is what makes the live greens statements about THIS commit.
#   - THE ASSERTION COUNT. `EXPECTED_ASSERTIONS=25` is checked at the end of a live run only; the
#     control scores rows, not assertions, and cannot notice an arm that stopped being called from
#     the live path — only one that stopped existing.
# What the control proves, and all it proves: the judgement functions tell a correct answer from an
# incorrect one, and tell each SPECIFIC defect from its neighbours rather than going red as a block.
# A run reports `negative control: N/N` and exits 0.
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`). Both, and neither is decoration.
#      Every arm of A is a claim about what the CONTROLLER did with `spec.operations.paused` versus
#      `spec.deployment.scaleToZero` — the renderer decides whether a pause moves `replicas`, so a
#      controller one generation behind the tree would answer A-3 about the previous build. Every
#      arm of B and C is a claim about what `decideGate` did, which is the broker binary. An
#      unidentifiable image on either makes every verdict below a statement about an unknown build.
#      Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with an explicit `delete --wait=true` and
#      re-applied on every run, so the broker Deployment, the gateway Deployment, the mesh
#      Certificates and the pair NetworkPolicies are all rendered by the controller running NOW.
#      Pods are resolved through `p3_pod_of_deploy`, by ownership through the ReplicaSet, so a pod
#      left over from a previous generation of the same Deployment can never be read as this one's —
#      which matters more here than anywhere, because A-4's whole content is "this is the same pod".
#      The FleetFreeze objects are deleted before they are created. B-2's three malformed objects go
#      through `--dry-run=server`, which runs the full admission chain and stores nothing, so there
#      is no grandfathered object for them to be compared against.
#   P6 runtime-authoritative: every assertion reads the API server or the broker's live answer.
#      The FleetFreeze defaults in B-1 are read back off the STORED object, never from the
#      `+kubebuilder:default` tags that produced them; B-2's rejections are the served schema's, not
#      the Go validation markers'. The driver's endpoint, SAN, identity, token path and TLS dir all
#      come off the RENDERED gateway Deployment through `broker_driver_env`, never recomputed from
#      `broker_manifests.go`. The roles stripped in B-5 are DISCOVERED from the bindings the cluster
#      actually has, not from the two objects the templates render — `broker-refuse-l2.sh` learned
#      on 2026-07-31 that this cluster also carries residue no template owns, and a hardcoded pair
#      tests the grant this repo believes it ships rather than the one the authorizer consults.
set -uo pipefail

# MODES. `live` breaks a real cluster and is what every claim in the header is about.
# `--negative-control` is the mandatory offline `¬` arm (V-MET-014): it replays the judgement
# functions below against synthesised inputs and requires each injected defect to be caught by the
# arm that targets it. It contacts nothing — no cluster, no network, no `kubectl` — which is what
# lets it sit on `dev/L0-CHAIN.txt` and run in PR CI, where the seven in-run `¬` arms cannot.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
AGENT=platform-agent
AGENT_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml

# Derived from AGENT and nothing else, so they are spelled here with the other constants rather than
# beside the P1 block that used to compute them. The judgement functions name both Deployments in
# their messages and the ¬ arm reaches those functions before a single cluster read has happened;
# a name computed halfway down the live path would be unset there, and `set -u` would take the
# control out at the first row.
BROKER_DEPLOY="${AGENT}-broker"
GATEWAY_DEPLOY="${AGENT}-gateway"

CTRL_DEPLOY=kubeagents-controller-manager
BRAKE_DEPLOY=kubeagents-brake-controller
ROUTER_DEPLOY=kubeagents-router
ROUTER_SVC=kubeagents-router
WEBHOOK_SVC=kubeagents-webhook-service

TENANT_NS=kubeagents-brake-tenant
DRIVER_POD=brake-l2-driver
DRIVER_CM=brake-l2-code
UNTRUSTED_SECRET=brake-l2-untrusted
PROBE=dev/verify/fixtures/broker_execute_probe.py

# Cluster-scoped, so these names are global. Both are deleted before either is created.
FREEZE_COVER=brake-l2-freeze-covering
FREEZE_ELSEWHERE=brake-l2-freeze-elsewhere

# The nonce A-2 hunts for in the refusal detail. Minted here, so it cannot have been observed by
# anything before this process started.
PAUSE_NONCE="brake-l2 pause nonce $$-$(date +%s)"

# 06 §4.4's staleness limit is 30s and `brake.DefaultCacheTTL` is 5s; the source re-probes lazily on
# the next Observe. 15s is three TTLs — long enough that the next submission cannot be answered from
# a cache filled before the control moved, short enough not to double the runtime of eleven
# submissions. There is nothing in-cluster to poll for it: the cache is inside the broker process
# and publishes no status, which is the same reason `broker-refuse-l2.sh` sleeps here too.
BRAKE_TTL_SETTLE=15

fail=0
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

# 2 x P1 + broker Available
# + A-0 baseline + A-1 refused + A-2 nonce in detail + A-3 replicas + A-4 same pod
#   + A-5 scaleToZero drains + A-6 restore brings a new pod + A-7 release
# + B-1 defaults + B-2 rejections + B-3 covering + B-4 non-covering + B-5 fail-closed
#   + B-6 no restart under the fault + B-7a recovery + B-7b recovered in place
# + C-1 fault real + C-2 inference down + C-3 broker answers + C-4 engageable + C-5 releasable
#   + C-6 pause enforced
#
# Every branch that cannot run an arm renders a `bad` IN ITS PLACE rather than skipping it, so this
# number is the same on a green run and on a red one. A count that drops when a fixture fails is how
# a suite reports "nothing went wrong" about arms that never executed (LSN-035/048).
EXPECTED_ASSERTIONS=25

# ================================================================================================
# THE JUDGEMENT BLOCK — every arm this suite scores, as functions over ALREADY-READ VALUES
# ================================================================================================
# NOTHING BELOW CALLS KUBECTL, AND NOTHING BELOW READS A GLOBAL THAT A CLUSTER WROTE. That is not a
# tidiness rule; it is the single property that makes `--negative-control` a control rather than a
# second opinion about the same thing. The ¬ arm replays THESE functions against synthesised
# arguments, so editing an arm edits the thing the control scores and the two cannot drift.
# The live path keeps every collection — the submissions, the `-o jsonpath` reads, the bounded polls,
# the RBAC walk — in the A/B/C sections further down, each of which reads its values off the API
# server or off the driver transcript and then calls in here to be judged.
#
# NAMED `judge_*`, NEVER `check_*` OR `assert_*`. `invariants-gate.py`'s SH_CHECK discovers a shell
# suite's named checks as `^(check|assert)_[a-z_]* "<name>"` and folds them into the V-MET-003
# assertion ratchet, so a helper spelled that way whose first argument is a VARIABLE registers the
# literal string `$why` as a test name that can never be deleted because it never existed.
# `dev/assertion-baseline.json` has no entry for this file and this change does not add one.
#
# THE MESSAGES ARE VERBATIM WHAT THEY WERE INLINE. A refactor that reworded a verdict would move
# every needle in the ¬ arm and, worse, would move the strings a human greps for in a red run.

# --- The two reply judgements, shared by all three rows -------------------------------------------

# judge_accepted <check-id> <why> <outcome> <status> <decision> <action-id> <summary>
#   The positive arms: A-0's baseline, A-7's release, B-4's non-covering control, B-7a's recovery,
#   C-3's "the broker still answers" and C-5's release. `summary` is `reply_summary`'s five words
#   plus the detail, passed in rather than recomputed, because recomputing it would mean reading
#   `FLAT` from inside a function the ¬ arm calls with no `FLAT` in scope.
judge_accepted() {
  local id="$1" why="$2" o="$3" s="$4" d="$5" a="$6" summary="$7"
  case "$o/$s/$d" in
    http/2[0-9][0-9]/accepted | http/2[0-9][0-9]/gated | http/2[0-9][0-9]/executed)
      if [ -z "$a" ]; then
        bad "$id: $why — the broker answered $s '$d' but named no actionId, so nothing was journaled: $summary"
        return 1
      fi
      pass "$id: $why — HTTP $s, decision '$d', actionId $a"
      return 0
      ;;
    http/2[0-9][0-9]/deduplicated)
      # ITS OWN CLAUSE, AND [[LSN-067]] IS WHY. On 2026-07-31 this suite submitted a FIXED envelope
      # eleven times and got one actionId — 1 `accepted`, 10 `deduplicated`. 06 §4.1's idempotency
      # key is a sha256 over identity + operations + dryRun, so an unvaried probe IS the same action
      # every time and the broker correctly answers the retries with the FIRST submission's record.
      # Nine arms passed vacuously against a record minted before the fault they claimed to measure,
      # and two went red where the stale answer happened to disagree. The fix is upstream, in
      # `submit()`'s per-submission target name — but a dedup is a 200, it is the last shape anyone
      # re-reads, and folding it into the generic "not accepted" line below would send the next
      # reader hunting for a brake refusal that never happened. Same verdict, named rule.
      bad "$id: $why — the broker answered $s decision 'deduplicated', which is 06 §4.1's retry path and NOT a fresh acceptance. The reply carries the FIRST submission with this idempotency key (actionId ${a:-<none>}), minted before whatever fault this arm just cleared, so it says nothing about the brake's state now — see [[LSN-067]] and submit()'s per-submission target name: $summary"
      return 1
      ;;
  esac
  bad "$id: $why — the envelope was NOT accepted: $summary"
  return 1
}

# judge_refused <check-id> <want-status> <want-reason> <needle> <why>
#              <outcome> <status> <reason> <decision> <detail> <summary>
#   The refusal arms: A-1's pause, B-3's covering freeze, B-5's fail-closed row 1, C-4's freeze
#   during the outage and C-6's pause during it. The `needle` is the whole reason this takes five
#   expectations instead of three — see the comment on the detail clause.
judge_refused() {
  local id="$1" ws="$2" wr="$3" needle="$4" why="$5"
  local o="$6" s="$7" r="$8" d="$9" det="${10}" summary="${11}"
  if [ "$o" != http ]; then
    bad "$id: $why — the submission never reached the broker: $summary"
    return 1
  fi
  if [ "$s" != "$ws" ] || [ "$r" != "$wr" ] || [ "$d" != rejected ]; then
    bad "$id: $why — wanted HTTP $ws reason '$wr' decision 'rejected', got: $summary"
    return 1
  fi
  case "$det" in
    *"$needle"*) ;;
    *)
      # The status code and the reason word are shared by several rows of 06 §4.4 — `scope-frozen`
      # is returned by both row 1 and the ordinary freeze — so the detail is the only part of the
      # reply that says WHICH rule fired. A refusal with the right code and the wrong detail is a
      # brake that stopped the action for a reason nobody can act on.
      bad "$id: $why — right code, wrong rule: the detail must contain '$needle', the string this rule renders and no other rule does. $summary"
      return 1
      ;;
  esac
  pass "$id: $why — HTTP $s, reason '$r', and the detail names the control that fired: $det"
  return 0
}

# --- A: V-RUN-007 ---------------------------------------------------------------------------------

# judge_pause_nonce <detail> <nonce>
#   A-2, and it is a SEPARATE assertion from A-1 on purpose: A-1 says the brake fired, A-2 says it
#   fired BECAUSE OF THIS OBJECT. A broker with a stuck global flag, a cached decision or a read of
#   the neighbouring agent's CR passes the first and cannot pass the second, because the nonce did
#   not exist when the flag stuck.
judge_pause_nonce() {
  case "$1" in
    *"$2"*)
      pass "V-RUN-007: the refusal quotes THIS run's spec.operations.pauseReason ('$2'), so the brake read this Agent CR and not a cached or global decision"
      ;;
    *)
      bad "V-RUN-007: the refusal does not carry this run's pauseReason. brake.go:479-482 appends it verbatim, so its absence means the refusal was decided from something other than this object. detail='$1'"
      ;;
  esac
}

# judge_gateway_replicas <before> <after>
#   A-3, the row's literal sentence. The `!= 0` half is not redundant with the equality: an agent
#   whose gateway was already at zero satisfies "unchanged" while proving nothing about a pause.
judge_gateway_replicas() {
  if [ "$2" = "$1" ] && [ "$2" != "0" ]; then
    pass "V-RUN-007: deploy/$GATEWAY_DEPLOY still asks for $2 replica(s) while the agent is paused — the controller did not implement the pause by scaling"
  else
    bad "V-RUN-007: deploy/$GATEWAY_DEPLOY went from $1 to $2 replicas across the pause. 08 §2.4: the pod keeps running; only the write path closes"
  fi
}

# judge_same_pod <before-pod> <before-identity> <after-pod> <after-identity>
#   A-4. The identity strings are `<uid> <startTime> <phase>`, and comparing them is the arm: a NAME
#   is reused by a Deployment that recreated its pod within the same ReplicaSet generation, and a
#   uid is assigned once and never again. An arm comparing names alone would score a replacement as
#   a survival, which is the whole distinction 08 §2.4 is drawing.
judge_same_pod() {
  if [ -n "$3" ] && [ "$3" = "$1" ] && [ "$4" = "$2" ]; then
    pass "V-RUN-007: the agent pod is the SAME object across the pause — $3, uid/startTime/phase unchanged [$4]"
  else
    bad "V-RUN-007: the agent pod changed across the pause. before: $1 [$2]; after: ${3:-<none>} [${4:-<none>}]. A uid is assigned once, so this is a replacement, not a pause"
  fi
}

# judge_scale_to_zero_drained <drained yes|no> <replicas> <pod>
#   A-5's ¬: what a real scale-to-zero looks like on this CRD, so A-3 and A-4 are falsifiable.
judge_scale_to_zero_drained() {
  if [ "$1" = yes ] && [ -z "$3" ]; then
    pass "V-RUN-007 ¬: spec.deployment.scaleToZero really does drive deploy/$GATEWAY_DEPLOY to 0 replicas with no pod owned by it — so the two assertions above measure something that can fail"
  else
    bad "V-RUN-007 ¬: scaleToZero did not drain the gateway (spec.replicas=$2, pod='${3:-<none>}'). The negative control did not run, so the pause arms are unfalsified"
  fi
}

# judge_scale_cycle_replaced_the_pod <pod> <restored-identity> <before-identity> <replicas>
#   A-6's ¬: after a scale cycle the uid MOVES. That is the contrast A-4 claims against — without
#   it, "same uid" has not been shown to be a reading that can ever come back different.
judge_scale_cycle_replaced_the_pod() {
  local pod="$1" restored_uid="${2%% *}" before_uid="${3%% *}" replicas="$4"
  if [ -n "$pod" ] && [ -n "$restored_uid" ] && [ "$restored_uid" != "$before_uid" ]; then
    pass "V-RUN-007 ¬: after the scale cycle the gateway is back at $replicas replica(s) with a DIFFERENT pod — $pod, uid $restored_uid, not $before_uid. That is the contrast the pause arm claims against"
  else
    bad "V-RUN-007 ¬: the gateway did not come back with a distinguishable pod (pod='${pod:-<none>}' uid='${restored_uid:-<none>}' vs before '$before_uid'), so 'same uid' has not been shown to be a discriminating reading"
  fi
}

# --- B: V-CTR-007 ---------------------------------------------------------------------------------

# judge_freeze_defaults <allowUndo> <allowClasses>
#   B-1, read off the object the API server STORED (P6). The two defaults point in opposite
#   directions and both are load-bearing: `UndoAllowed()` defaults OPEN so a freeze cannot strand
#   the fleet in the state a human is trying to reverse, and `Allows()` defaults CLOSED so an empty
#   list refuses every class.
judge_freeze_defaults() {
  if [ "$1" = "true" ] && [ -z "$2" ]; then
    pass "V-CTR-007: the stored FleetFreeze defaults allowUndo=true and allowClasses=[] — undo stays open so a freeze cannot strand the fleet in the state a human is reversing, and every risk class is closed because Allows() refuses what is not named"
  else
    bad "V-CTR-007: the stored defaults are allowUndo='${1:-<unset>}' allowClasses='${2:-[]}'; 06 §4.4 wants undo open by default and every class closed by default"
  fi
}

# judge_malformed_freezes_refused <admitted> <total>
#   B-2's ¬, over the served schema rather than over the Go markers.
judge_malformed_freezes_refused() {
  if [ "$1" -eq 0 ]; then
    pass "V-CTR-007 ¬: the served API refuses all three malformed brake objects — an unattributable requester, a risk class outside the enum, and a freeze with no stated reason"
  else
    bad "V-CTR-007 ¬: $1 of $2 malformed FleetFreezes were ADMITTED by the served API. The contract in fleetfreeze_types.go is not the contract the cluster is enforcing"
  fi
}

# judge_broker_in_place <phase: degraded|recovered> <pod-before> <restarts-before> <pod-now> <restarts-now>
#   B-6 and B-7b. ONE COMPARISON, TWO MESSAGE SETS, and not two blocks: they were near-identical
#   inline copies whose only real difference was the direction of the sentence, and keeping two
#   would mean the ¬ arm controls one of them while the other is a copy nothing scores.
#
#   WHY THIS ARM IS NOT DECORATION. `brake/brake.go`'s cache "degrades into row 1 by itself… This is
#   the opposite arrangement from an informer, and deliberately so". A broker that CRASHED when the
#   grant went away renders exactly the same 403 `scope-frozen` with exactly the same row-1 detail —
#   the reply cannot tell the two apart, so B-5 is worth nothing unless something else does. This is
#   that something else, and the restart counts are compared PER CONTAINER rather than summed: a pod
#   where one container restarts and another is added must not cancel out to the same number.
judge_broker_in_place() {
  local phase="$1" pod0="$2" r0="$3" pod1="$4" r1="$5"
  if [ "$pod1" = "$pod0" ] && [ "$r1" = "$r0" ]; then
    case "$phase" in
      degraded)
        pass "V-CTR-007: the broker degraded into row 1 WITHOUT restarting — same pod $pod0, same restartCounts ($r1). brake/brake.go: 'the cache degrades into row 1 by itself… the opposite arrangement from an informer, and deliberately so'"
        ;;
      *)
        pass "V-CTR-007: it recovered IN PLACE — still pod $pod1 with restartCounts $r1, unchanged since before the fault. The brake came back the way it degraded, with no restart and no redeploy"
        ;;
    esac
  else
    case "$phase" in
      degraded)
        bad "V-CTR-007: the broker did not survive the fault in place — pod '${pod1:-<none>}' (was $pod0), restarts '${r1:-<none>}' (were ${r0:-<none>}). A brake that has to crash to fail closed loses every in-flight request as well"
        ;;
      *)
        bad "V-CTR-007: the broker recovered by being replaced — pod '${pod1:-<none>}' (was $pod0), restarts '${r1:-<none>}' (were ${r0:-<none>}). A brake that needs a restart to stop failing closed keeps the fleet stopped until somebody notices"
        ;;
    esac
  fi
}

# --- C: V-RUN-008 ---------------------------------------------------------------------------------

# judge_outage_real <webhook-endpoints> <agent-write-rc> <agent-write-output>
#   C-1, the positive control for the whole of arm C. TWO READINGS AND BOTH REQUIRED: the Service
#   has no backend AND an Agent UPDATE is actually rejected. `vagent.kb.io` is failurePolicy Fail
#   over CREATE/UPDATE/DELETE on `agents` and its Service selects `control-plane: controller-manager`
#   (`config/webhook/manifests.yaml`), so with no endpoint the write MUST fail. A run that asked
#   only for the endpoint count could scale a Deployment that was already at zero and call the rest
#   of the arm proven.
judge_outage_real() {
  local eps="${1:-1}" rc="$2" out="$3"
  if [ "$eps" -eq 0 ] && [ "$rc" -ne 0 ]; then
    pass "V-RUN-008: the controller is genuinely gone — $CTRL_DEPLOY and $BRAKE_DEPLOY at 0, $WEBHOOK_SVC has no endpoints, and an Agent UPDATE is now rejected by admission (vagent.kb.io, failurePolicy Fail): $(printf '%s' "$out" | tail -1)"
  else
    bad "V-RUN-008: the outage is not real — webhook endpoints='$1' and an Agent UPDATE still succeeded (rc=$rc). Every arm below would be measuring a healthy cluster"
  fi
}

# judge_inference_down <gw-replicas> <gw-pod> <router-replicas> <router-endpoints>
#   C-2. FOUR READINGS, ALL REQUIRED, and the two that are not replica counts are the point. A
#   Deployment's `spec.replicas` is a REQUEST, not an observation: it reads 0 the instant the scale
#   lands and says nothing about the pod that is still terminating, still Running, and still holding
#   the connection this arm claims does not exist. Likewise a Service with an endpoint still routes.
#   06 §4.4's sentence is "no dependency on the model, the router, or the agent pod", so the absence
#   has to be proven of the POD and of the ENDPOINT, and the replica count is only how it was asked
#   for.
judge_inference_down() {
  local gw_replicas="${1:-1}" gw_pod="$2" router_replicas="${3:-1}" router_eps="${4:-1}"
  if [ "$gw_replicas" = "0" ] && [ -z "$gw_pod" ] && [ "$router_replicas" = "0" ] && [ "$router_eps" -eq 0 ]; then
    pass "V-RUN-008: inference is down too — deploy/$GATEWAY_DEPLOY at 0 with no pod owned by it, and $ROUTER_DEPLOY at 0 with no endpoints behind $ROUTER_SVC. 06 §4.4: 'no dependency on the model, the router, or the agent pod'"
  else
    bad "V-RUN-008: the inference path is still up — gateway replicas='$1' pod='${2:-<none>}', router replicas='${3:-?}' endpoints='${4:-?}'. The claim below would not be about a brake standing alone"
  fi
}

# ================================================================================================
# THE `¬` ARM
# ================================================================================================
# WHY SYNTHESISED REPLIES AND NOT A MUTATED BROKER. Making a REAL broker answer `deduplicated` where
# an arm wants an acceptance, or crash instead of degrading into 06 §4.4 row 1, means editing
# `brake/brake.go` or `antireplay.go` and rolling an image — `dev/mutate.py`'s job at L1, and not
# something an L2 suite can stage against a running cluster. The seven in-run `¬` arms already cover
# what a live lever can be made to do. What this arm proves is the thing an L2 suite CAN get wrong
# entirely on its own: that its judgements tell a correct answer from an incorrect one, and tell each
# SPECIFIC defect apart from its neighbours rather than going red as a block.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`. Every judged call runs inside a command
# substitution, which is a subshell, so every `fail=1` a `bad` sets inside one dies with it — and so
# does every `assertions` increment, which is why this mode scores ROWS and says nothing about
# `EXPECTED_ASSERTIONS`. The scorers below count `^FAIL:` lines rather than inspecting a variable.

# The synthesised world. Names are this suite's real ones so a red row reads like a real red run;
# the pause nonce is THIS PROCESS'S, not a literal, for [[LSN-063]]'s reason — a control that
# hardcodes a value from the tree is a bet with an expiry date on it.
NC_POD_GW=platform-agent-gateway-7d9f4c8b5-k2xqp
NC_UID_GW=aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa
NC_UID_GW2=bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb
NC_IDENT_GW="$NC_UID_GW 2026-07-31T09:14:02Z Running"
NC_IDENT_GW_NEWUID="$NC_UID_GW2 2026-07-31T09:41:55Z Running"
NC_POD_BROKER=platform-agent-broker-6c8d5f9a4-mn7wt
NC_POD_BROKER2=platform-agent-broker-6c8d5f9a4-zz19r
NC_RESTARTS='broker=0,'
NC_RESTARTS_BUMPED='broker=1,'
NC_ACTION_1=01KYXM37W6Z6NN4P9SRKASRP1X
NC_ACTION_2=01KYXM9QF4T3BB2R7HDVE0MJ8C
NC_STALE_NONCE='brake-l2 pause nonce 4242-1753900000'

# The two detail strings 06 §4.4 renders for its two `scope-frozen` answers, which are the whole
# reason `judge_refused` takes a needle. `frozenDetail()` (brake.go:761) names the object a human
# deletes; `freezeUnreadableDetail()` (brake.go:749-759) says the list could not be read and that
# undo still runs. Same 403, same reason word, opposite operator actions.
NC_DETAIL_COVERED="FleetFreeze $FREEZE_COVER covers this scope: brake-l2: V-CTR-007 covering freeze (no expiry; it is cleared by deleting the object)"
NC_DETAIL_ROW1="the FleetFreeze list could not be read, so this scope is treated as frozen; undo still runs"

nc_total=0
nc_caught=0
nc_rc=0
NC_BASELINES=""

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

# nc_join — the argument vector as one line, so a row's input can be compared with another row's.
# The separator only has to be a string no argument contains; a collision would produce a false
# BROKEN, which is loud, where a silent miss is the thing being guarded against.
nc_join() {
  local out="" a
  for a in "$@"; do out="$out|@|$a"; done
  printf '%s' "$out"
}

# nc_score <name> <green|red|red1> <needle> -- <judge-or-probe fn> <args...>
#
#   green   the judgement ACCEPTS the input: zero FAIL lines, at least one PASS. Non-vacuity — a
#           control made only of defects proves the arms are always-red, which is not better. A
#           green row also RECORDS its argument vector as a clean baseline for that function, which
#           is what the [[LSN-063]] guard below compares against.
#   red     at least one FAIL line, and one of them CONTAINS the needle ([[LSN-035]]). Used where the
#           defect is genuinely visible to more than one arm — a refusal that is wrong in its status
#           AND unattributable is two findings, and demanding one would be demanding the wrong thing.
#   red1    EXACTLY one FAIL line, and it contains the needle. This is the default here and the
#           reason the composite probes exist: this suite's method is that two replies with the same
#           status code mean different things, so almost every row must leave its neighbours GREEN.
#           "A broker that restarted" and "a broker that degraded in place" render the identical 403;
#           a control that accepted "it went red somewhere" would score them the same.
#
# BROKEN, under its own word and never folded into MISS ([[LSN-063]]): they call for opposite
# repairs. MISS says the arm cannot see its own defect and invites strengthening it. BROKEN says the
# row was never an experiment, and strengthening an arm that was asked nothing produces a check that
# passes on the first run, looks exactly like the fix, and leaves the mutant still unmeasured.
nc_score() {
  local name="$1" expect="$2" needle="$3"
  shift 3
  [ "${1:-}" = "--" ] && shift
  local fn="$1"
  shift
  local joined out n_fail n_any
  nc_total=$((nc_total + 1))
  joined="$(nc_join "$@")"

  if [ "$expect" != green ]; then
    if ! grep -qE "^$fn	" "$NC_BASELINES"; then
      nc_broken "$name" "no clean baseline has been recorded for $fn, so nothing establishes that it accepts ANY input. 'It went red' is not a finding about a function that may be red on everything ([[LSN-035]]); put a green row for $fn above this one"
      return 0
    fi
    if printf '%s\t%s\n' "$fn" "$joined" | grep -qxFf - "$NC_BASELINES"; then
      nc_broken "$name" "the synthesised input is byte-identical to a clean baseline for $fn, so no defect was injected and the judgement was asked nothing ([[LSN-063]])"
      return 0
    fi
  fi

  out="$("$fn" "$@" 2>&1)"
  n_fail="$(printf '%s\n' "$out" | grep -cE '^FAIL:')"
  n_any="$(printf '%s\n' "$out" | grep -cE '^(PASS|FAIL):')"

  if [ "$n_any" -eq 0 ]; then
    nc_broken "$name" "$fn emitted no PASS and no FAIL. Nothing was evaluated, so this row is not a finding about the arm — the arm has been deleted or returns early ([[LSN-063]])" "$out"
    return 0
  fi

  case "$expect" in
    green)
      if [ "$n_fail" -eq 0 ]; then
        printf '%s\t%s\n' "$fn" "$joined" >>"$NC_BASELINES"
        nc_ok "$name" "a CORRECT input is accepted ($n_any arm(s) ran), so the arms below are not always-red"
      else
        nc_miss "$name" "a CORRECT input was failed $n_fail time(s); every defect below would then be 'caught' for a reason that has nothing to do with it" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
      fi
      ;;
    red1)
      if [ "$n_fail" -ne 1 ]; then
        nc_miss "$name" "expected EXACTLY one red arm and got $n_fail. The defect is supposed to be visible to one named arm while its neighbours stay green; a block of reds does not distinguish it from a suite that cannot read the reply at all" "$(printf '%s\n' "$out" | grep -E '^FAIL:')"
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

# nc_reply — the five-word summary `reply_summary` renders, built from the same fields so a red row
# in this mode reads the way a red row reads live. It is an ARGUMENT to the judgements, never a
# read: `reply_summary` itself calls `field`, which awks over the driver transcript, and there is no
# transcript here.
nc_reply() { # <outcome> <status> <reason> <decision> <actionId> <detail>
  printf "outcome='%s' status='%s' reason='%s' decision='%s' actionId='%s' detail='%s'" \
    "$1" "$2" "$3" "$4" "$5" "$6"
}

# --- The composite probes -------------------------------------------------------------------------
# THREE PLACES WHERE THE LIVE PATH RUNS TWO JUDGEMENTS OVER ONE OBSERVATION, replayed as one row
# each. They have to be functions rather than pairs of rows for the same reason `red1` exists: the
# content of each is that ONE of the two arms goes red and the other stays green, and two rows would
# be two subshells that could never see each other's verdict.

# nc_probe_pause_refusal <outcome> <status> <reason> <decision> <detail>
#   A-1 + A-2. The live path judges the refusal, then judges whether the refusal is about THIS CR.
nc_probe_pause_refusal() {
  judge_refused "V-RUN-007" 403 agent-paused "spec.operations.paused" \
    "a paused agent's envelope is refused by the brake" \
    "$1" "$2" "$3" "$4" "$5" "$(nc_reply "$1" "$2" "$3" "$4" "" "$5")"
  judge_pause_nonce "$5" "$PAUSE_NONCE"
}

# nc_probe_row1_and_the_broker <detail> <pod-now> <restarts-now>
#   B-5 + B-6, and this pair is the reason the arm exists at all. 06 §4.4 row 1 says an unreadable
#   FleetFreeze list freezes the scope; `brake/brake.go` says the cache degrades into that row BY
#   ITSELF, without restarting. A broker that CRASHED when the grant went away produces a
#   byte-identical 403 with a byte-identical row-1 detail, so B-5 alone cannot tell the design from
#   its opposite. B-6 is the arm that can, and this probe is where that claim is tested.
nc_probe_row1_and_the_broker() {
  judge_refused "V-CTR-007" 403 scope-frozen "undo still runs" \
    "with NO FleetFreeze in the cluster and the list unreadable, the scope is treated as frozen — 06 §4.4 row 1, fail-closed" \
    http 403 scope-frozen rejected "$1" "$(nc_reply http 403 scope-frozen rejected "" "$1")"
  judge_broker_in_place degraded "$NC_POD_BROKER" "$NC_RESTARTS" "$2" "$3"
}

# nc_probe_outage <wh-eps> <write-rc> <write-out> <gw-replicas> <gw-pod> <router-replicas> <router-eps>
#   C-1 + C-2, the absence proof V-RUN-008 asserts BEFORE it submits anything. Both halves are
#   replayed together because the defect this pair is written against is a run that reads a replica
#   count of 0 and calls the workload gone: `spec.replicas` is a REQUEST, and the pod that is still
#   serving and the endpoint that is still routing are separate readings.
nc_probe_outage() {
  judge_outage_real "$1" "$2" "$3"
  judge_inference_down "$4" "$5" "$6" "$7"
}

run_negative_control() {
  NC_BASELINES="$(mktemp "${TMPDIR:-/tmp}/brake-l2-nc.XXXXXX")" || return 1

  echo
  echo "-- the ACCEPT judgement: A-0, A-7, B-4, B-7a, C-3 and C-5 all rest on it (judge_accepted) --"
  nc_score accepted-202-with-an-actionid green '-' -- \
    judge_accepted "V-RUN-007" "unpausing releases the brake — the write path opens again" \
    http 202 accepted "$NC_ACTION_1" \
    "$(nc_reply http 202 accepted rejected "$NC_ACTION_1" '')"
  # `gated` and `executed` are accepts too: 06 §4.4 puts the brake in `decideGate`, BEFORE the
  # classifier's verdict is applied, so an envelope the pipeline then gates is one the brake let
  # through — which is the only thing every positive arm here claims.
  nc_score accepted-200-gated-is-still-an-accept green '-' -- \
    judge_accepted "V-CTR-007 ¬" "a FleetFreeze scoped to a different projectId does not stop this agent" \
    http 200 gated "$NC_ACTION_2" \
    "$(nc_reply http 200 gated gated "$NC_ACTION_2" '')"
  # THE MANDATORY ROW, AND THE ONE THIS FILE HAS ALREADY PAID FOR. [[LSN-067]]: on 2026-07-31 this
  # suite submitted a fixed envelope eleven times, got ONE actionId, and nine arms passed vacuously
  # against a record minted before the fault they claimed to measure. A dedup is a 200 and reads like
  # a success. If `judge_accepted` ever stops rejecting one, the regression comes back silent.
  nc_score a-retry-answered-with-the-first-submissions-record red1 'NOT a fresh acceptance' -- \
    judge_accepted "V-CTR-007" "restoring the grant lifts the fail-closed refusal" \
    http 200 deduplicated "$NC_ACTION_1" \
    "$(nc_reply http 200 deduplicated deduplicated "$NC_ACTION_1" '')"
  # B-4's world, gone wrong: a freeze scoped to ANOTHER projectId stopped this agent anyway, which
  # is `Covers()` returning true without comparing. B-4 is B-3's control, so an `expect_accepted`
  # that let this through would report B-3 and B-4 both green against a broker refusing everything.
  nc_score a-freeze-scoped-elsewhere-stopped-this-agent red1 'the envelope was NOT accepted' -- \
    judge_accepted "V-CTR-007 ¬" "a FleetFreeze scoped to a different projectId does not stop this agent" \
    http 403 rejected '' \
    "$(nc_reply http 403 scope-frozen rejected '' "$NC_DETAIL_COVERED")"
  nc_score accepted-but-nothing-was-journaled red1 'named no actionId' -- \
    judge_accepted "V-RUN-008" "the broker still serves with everything at zero" \
    http 202 accepted '' \
    "$(nc_reply http 202 accepted accepted '' '')"
  # Same clause as the row two above, deliberately and disclosed: a transport that never reached the
  # broker and a broker that refused are two worlds, and exactly one rule answers both — "this is not
  # an acceptance". The row is here because the input class is what an outage looks like, and C-3 is
  # the arm that must not read it as the brake working.
  nc_score the-submission-never-reached-the-broker red1 'the envelope was NOT accepted' -- \
    judge_accepted "V-RUN-008" "the broker still serves with everything at zero" \
    error '' '' '' \
    "$(nc_reply error '' '' '' '' 'connection refused')"

  echo
  echo "-- the REFUSE judgement: A-1, B-3, B-5, C-4 and C-6 all rest on it (judge_refused) --"
  nc_score covering-freeze-refusal-names-the-object green '-' -- \
    judge_refused "V-CTR-007" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
    "a FleetFreeze whose scope matches the agent's projectId stops the action" \
    http 403 scope-frozen rejected "$NC_DETAIL_COVERED" \
    "$(nc_reply http 403 scope-frozen rejected '' "$NC_DETAIL_COVERED")"
  nc_score the-covering-freeze-did-not-stop-the-action red1 "wanted HTTP 403 reason 'scope-frozen'" -- \
    judge_refused "V-CTR-007" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
    "a FleetFreeze whose scope matches the agent's projectId stops the action" \
    http 202 '' accepted '' \
    "$(nc_reply http 202 '' accepted "$NC_ACTION_2" '')"
  nc_score a-freeze-refusal-that-never-reached-the-broker red1 'the submission never reached the broker' -- \
    judge_refused "V-CTR-007" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
    "a FleetFreeze whose scope matches the agent's projectId stops the action" \
    error '' '' '' '' \
    "$(nc_reply error '' '' '' '' 'connection refused')"
  # THE DISCRIMINATION 06 §4.4 IS BUILT ON, in the direction B-3 owns. The broker answered 403
  # `scope-frozen` — right code, right reason word — with row 1's detail, i.e. "I could not read the
  # list", while a freeze that covers this scope is sitting in the cluster. An operator reading that
  # deletes the wrong thing, or nothing.
  nc_score covering-freeze-answered-with-row-1s-detail red1 'right code, wrong rule' -- \
    judge_refused "V-CTR-007" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
    "a FleetFreeze whose scope matches the agent's projectId stops the action" \
    http 403 scope-frozen rejected "$NC_DETAIL_ROW1" \
    "$(nc_reply http 403 scope-frozen rejected '' "$NC_DETAIL_ROW1")"

  echo
  echo "-- A-1 + A-2 together: the brake fired, and it fired because of THIS CR (nc_probe_pause_refusal) --"
  nc_score pause-refusal-quotes-this-runs-nonce green '-' -- \
    nc_probe_pause_refusal http 403 agent-paused rejected \
    "spec.operations.paused is set on this Agent: $PAUSE_NONCE"
  # THE MANDATORY ROW. Status, reason, decision and even the rule needle are all correct, and the
  # nonce in the detail belongs to some other run. A-1 cannot see it — the refusal IS well-formed —
  # and A-2 is the only thing standing between "this CR is paused" and "a global flag is stuck", "a
  # cached decision is being replayed" or "the broker read the neighbouring agent". The nonce did not
  # exist before this process started, so nothing but a read of this object can produce it.
  nc_score pause-refusal-quotes-a-stale-nonce red1 "does not carry this run's pauseReason" -- \
    nc_probe_pause_refusal http 403 agent-paused rejected \
    "spec.operations.paused is set on this Agent: $NC_STALE_NONCE"
  # Both arms red, and `red` rather than `red1` for that reason: a refusal that is the WRONG refusal
  # is also, necessarily, one that cannot carry this run's pause reason. Demanding one red here would
  # be demanding that A-2 stay green about a reply A-1 has already rejected.
  nc_score a-pause-arm-answered-with-a-freeze-refusal red "wanted HTTP 403 reason 'agent-paused'" -- \
    nc_probe_pause_refusal http 403 scope-frozen rejected "$NC_DETAIL_COVERED"

  echo
  echo "-- A-3: pause is not scale-to-zero, in replicas (judge_gateway_replicas) --"
  nc_score gateway-still-asks-for-one-replica green '-' -- \
    judge_gateway_replicas 1 1
  nc_score the-pause-scaled-the-gateway-to-zero red1 'went from 1 to 0 replicas across the pause' -- \
    judge_gateway_replicas 1 0
  # UNCHANGED IS NOT ENOUGH, and this row is why the arm carries a second clause. An agent whose
  # gateway was already at zero before anything paused it satisfies "the replica count did not move"
  # perfectly, and V-RUN-007 is a claim about a pod that SURVIVES the pause.
  nc_score the-gateway-was-already-at-zero red1 'went from 0 to 0 replicas across the pause' -- \
    judge_gateway_replicas 0 0

  echo
  echo "-- A-4: pause is not scale-to-zero, in pod identity (judge_same_pod) --"
  nc_score the-pod-is-the-same-object green '-' -- \
    judge_same_pod "$NC_POD_GW" "$NC_IDENT_GW" "$NC_POD_GW" "$NC_IDENT_GW"
  # THE MANDATORY ROW. Same Deployment, same ReplicaSet, same POD NAME — and a new uid and a new
  # startTime, which is a pod that was destroyed and recreated inside the pause. An arm comparing
  # names alone scores this as "the pod kept running", and 08 §2.4's sentence would read green
  # through the exact event it forbids. A uid is assigned once and never reissued.
  nc_score the-pod-was-replaced-under-the-same-name red1 'A uid is assigned once, so this is a replacement, not a pause' -- \
    judge_same_pod "$NC_POD_GW" "$NC_IDENT_GW" "$NC_POD_GW" "$NC_IDENT_GW_NEWUID"
  nc_score no-pod-is-owned-by-the-gateway-after-the-pause red1 'A uid is assigned once, so this is a replacement, not a pause' -- \
    judge_same_pod "$NC_POD_GW" "$NC_IDENT_GW" '' ''

  echo
  echo "-- A-5 / A-6 ¬: what a real scale-to-zero looks like, so A-3 and A-4 are falsifiable --"
  nc_score scale-to-zero-drains-the-gateway green '-' -- \
    judge_scale_to_zero_drained yes 0 ''
  # The same shape as the C-2 defect below, one CRD field away: the Deployment converged to 0 and a
  # pod is still owned by it. A control that accepted this would be claiming the contrast A-3 and A-4
  # measure against has been demonstrated when it has not.
  nc_score scale-to-zero-left-a-pod-behind red1 'The negative control did not run, so the pause arms are unfalsified' -- \
    judge_scale_to_zero_drained yes 0 "$NC_POD_GW"
  nc_score the-scale-cycle-brings-back-a-different-pod green '-' -- \
    judge_scale_cycle_replaced_the_pod "$NC_POD_GW" "$NC_IDENT_GW_NEWUID" "$NC_IDENT_GW" 1
  nc_score the-scale-cycle-brought-back-the-same-uid red1 'has not been shown to be a discriminating reading' -- \
    judge_scale_cycle_replaced_the_pod "$NC_POD_GW" "$NC_IDENT_GW" "$NC_IDENT_GW" 1

  echo
  echo "-- B-1: the served schema's defaults, in both directions (judge_freeze_defaults) --"
  nc_score undo-open-and-every-class-closed green '-' -- \
    judge_freeze_defaults true ''
  # `UndoAllowed()` defaulting CLOSED is a freeze that strands the fleet in the state a human is
  # trying to reverse — the brake becoming the incident.
  nc_score undo-closed-by-default red1 '06 §4.4 wants undo open by default' -- \
    judge_freeze_defaults false ''
  # And the other direction, which fails open rather than shut: a default `allowClasses` with a class
  # in it is a freeze that does not freeze that class.
  nc_score a-risk-class-admitted-by-default red1 '06 §4.4 wants undo open by default' -- \
    judge_freeze_defaults true '["routine"]'

  echo
  echo "-- B-2 ¬: the served API refuses malformed brake objects (judge_malformed_freezes_refused) --"
  nc_score all-three-malformed-freezes-refused green '-' -- \
    judge_malformed_freezes_refused 0 3
  nc_score one-malformed-freeze-was-admitted red1 'were ADMITTED by the served API' -- \
    judge_malformed_freezes_refused 1 3

  echo
  echo "-- B-5 + B-6 together: fail-closed row 1, and the broker that got there without dying --"
  nc_score row-1-refusal-from-a-broker-that-degraded-in-place green '-' -- \
    nc_probe_row1_and_the_broker "$NC_DETAIL_ROW1" "$NC_POD_BROKER" "$NC_RESTARTS"
  # THE MANDATORY ROW, and the sharpest one in the file. The reply is byte-identical to the green
  # above — 403, `scope-frozen`, row 1's detail, everything B-5 asks for — and the broker producing
  # it is a NEW POD. It refused because it was gone, not because its cache aged into row 1. B-5 stays
  # green, correctly; only B-6 can see the difference, and 06 §4.4's fail-closed row is worth nothing
  # if the two are indistinguishable.
  nc_score row-1-refusal-from-a-broker-that-restarted red1 'did not survive the fault in place' -- \
    nc_probe_row1_and_the_broker "$NC_DETAIL_ROW1" "$NC_POD_BROKER2" "$NC_RESTARTS"
  # The same defect one layer down, where the pod NAME does not move: the container crash-looped and
  # came back inside the same pod. This is why `broker_restarts` reads per-container counts and joins
  # them rather than summing — a container that restarts while another is added sums to the same
  # number.
  nc_score row-1-refusal-from-a-broker-whose-container-restarted red1 'did not survive the fault in place' -- \
    nc_probe_row1_and_the_broker "$NC_DETAIL_ROW1" "$NC_POD_BROKER" "$NC_RESTARTS_BUMPED"
  # And the other direction: the broker stayed up and answered with the ORDINARY freeze detail while
  # no FleetFreeze exists in the cluster. B-6 stays green; only the needle in B-5 can see it.
  nc_score row-1-answered-with-a-covering-freezes-detail red1 'right code, wrong rule' -- \
    nc_probe_row1_and_the_broker "$NC_DETAIL_COVERED" "$NC_POD_BROKER" "$NC_RESTARTS"

  echo
  echo "-- B-7b: and it recovers the way it degraded (judge_broker_in_place) --"
  nc_score the-broker-recovered-in-place green '-' -- \
    judge_broker_in_place recovered "$NC_POD_BROKER" "$NC_RESTARTS" "$NC_POD_BROKER" "$NC_RESTARTS"
  nc_score the-broker-recovered-by-being-replaced red1 'recovered by being replaced' -- \
    judge_broker_in_place recovered "$NC_POD_BROKER" "$NC_RESTARTS" "$NC_POD_BROKER2" "$NC_RESTARTS"

  echo
  echo "-- C-1 + C-2: the absence proof, before a single brake decision is submitted (nc_probe_outage) --"
  nc_score the-control-plane-and-inference-are-both-gone green '-' -- \
    nc_probe_outage 0 1 'Error from server (InternalError): failed calling webhook "vagent.kb.io"' \
    0 '' 0 0
  # THE MANDATORY PAIR, both halves. `spec.replicas: 0` is a REQUEST the API server recorded, not an
  # observation: the pod may still be terminating, still Running, still holding the connection this
  # arm claims does not exist, and the Service may still be routing to it. If the absence proof were
  # satisfiable by the replica count alone, C-3/C-4/C-6 would be measurements taken against a
  # gateway that was still up and a router that was still answering.
  nc_score the-gateway-reads-zero-and-still-owns-a-pod red1 'The claim below would not be about a brake standing alone' -- \
    nc_probe_outage 0 1 'Error from server (InternalError): failed calling webhook "vagent.kb.io"' \
    0 "$NC_POD_GW" 0 0
  nc_score the-router-reads-zero-and-its-service-still-has-endpoints red1 'The claim below would not be about a brake standing alone' -- \
    nc_probe_outage 0 1 'Error from server (InternalError): failed calling webhook "vagent.kb.io"' \
    0 '' 0 2
  nc_score the-webhook-service-still-has-a-backend red1 'the outage is not real' -- \
    nc_probe_outage 1 1 'Error from server (InternalError): failed calling webhook "vagent.kb.io"' \
    0 '' 0 0
  # The reading that matters most, because it is the one that cannot be faked by a scale command: the
  # endpoint list is empty and an Agent UPDATE went through anyway. `vagent.kb.io` is failurePolicy
  # Fail, so the controller is reachable by some path this suite did not remove, and every refusal in
  # arm C would be a normal-service refusal wearing an outage costume.
  nc_score an-agent-update-succeeded-during-the-outage red1 'the outage is not real' -- \
    nc_probe_outage 0 0 'agent.kubeagents.x-k8s.io/platform-agent patched' \
    0 '' 0 0

  rm -f "$NC_BASELINES"

  echo
  echo "===================================================================="
  echo " negative control: $nc_caught/$nc_total"
  if [ "$nc_rc" -eq 0 ]; then
    echo " NEGATIVE CONTROL PASSED — every synthesised defect was rejected by the arm that targets"
    echo " it, and every correct answer was accepted. V-RUN-007, V-CTR-007 and V-RUN-008's live"
    echo " greens are measurements and not the shape of a suite that cannot go red."
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
  echo " brake-l2.sh --negative-control — the offline ¬ for V-RUN-007, V-CTR-007 and V-RUN-008"
  echo " (09 §6 marks all three; V-MET-014 makes the control mandatory)"
  echo " Can this suite's judgements tell a working brake from a broken one — and each specific"
  echo " defect from its neighbour, when several of them render the identical HTTP reply?"
  echo "===================================================================="
  run_negative_control
  exit $?
fi

# ------------------------------------------------------------------------------------------------
# Guard
# ------------------------------------------------------------------------------------------------
case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  exit 1
}

echo "===================================================================="
echo " V-RUN-007 · V-CTR-007 · V-RUN-008 at L2 — the brake"
echo " context: $CTX"
echo "===================================================================="

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/actor-overlay.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/broker-driver.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# ------------------------------------------------------------------------------------------------
# Repair state. Everything below that breaks the cluster records how to put it back BEFORE it
# breaks it, and the trap is the safety net for a run that dies in between.
# ------------------------------------------------------------------------------------------------
CONTROL_PLANE_DOWN=no
CTRL_REPLICAS_BEFORE=""
BRAKE_REPLICAS_BEFORE=""
ROUTER_REPLICAS_BEFORE=""

FREEZE_GRANT_STRIPPED=no
FREEZE_SNAPSHOT_DIR=""
ACTOR_SA=""
ACTOR_SUBJECT=""

replicas_of() { # <deploy> — desired replicas, or "" if the Deployment is gone
  $K -n "$NS" get "deploy/$1" -o jsonpath='{.spec.replicas}' 2>/dev/null
}

scale_to() { # <deploy> <n>
  $K -n "$NS" scale "deploy/$1" --replicas="$2" >/dev/null 2>&1
}

# endpoint_count — how many addresses back a Service right now. Used both to confirm the fault
# (C-1, C-2) and to confirm the repair, because "the Deployment says 1" is not the same statement as
# "admission has a backend again", and deleting the Agent CR in cleanup needs the second one.
endpoint_count() { # <svc> — always a number; a missing Endpoints object counts as zero backends
  $K -n "$NS" get "endpoints/$1" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null |
    wc -w | tr -d ' '
}

# restore_control_plane — put the controller, the brake controller and the router back, and wait
# until admission has a backend again. rc 0 = restored.
#
# IDEMPOTENT AND CALLED TWICE, for `broker-refuse-l2.sh`'s reason: once from the body, where the run
# needs the control plane back to unpause and to tear down, and once from the EXIT trap, which is
# the only thing standing between a run that died inside arm C and a cluster whose operator is
# switched off with no note saying why.
restore_control_plane() {
  [ "$CONTROL_PLANE_DOWN" = yes ] || return 0
  echo "  restoring the control plane (controller, brake controller, router)"
  scale_to "$CTRL_DEPLOY" "${CTRL_REPLICAS_BEFORE:-1}"
  scale_to "$BRAKE_DEPLOY" "${BRAKE_REPLICAS_BEFORE:-1}"
  scale_to "$ROUTER_DEPLOY" "${ROUTER_REPLICAS_BEFORE:-1}"
  # Converged on, not slept through (P9): every `kubectl` against an Agent from here on is subject
  # to `vagent.kb.io`, which fails closed. A fixed wait is how cleanup's own delete gets refused.
  local eps=0 deadline=$((SECONDS + 180))
  while [ "$SECONDS" -lt "$deadline" ]; do
    eps="$(endpoint_count "$WEBHOOK_SVC")"
    [ "${eps:-0}" -ge 1 ] && break
    sleep 3
  done
  if [ "${eps:-0}" -lt 1 ]; then
    echo "  WARNING: $WEBHOOK_SVC still has no endpoints after 180s. The Agent admission webhook" >&2
    echo "  fails closed, so agents cannot be created, updated or deleted until it does." >&2
    return 1
  fi
  CONTROL_PLANE_DOWN=no
  echo "  the webhook service has a backend again"
  return 0
}

# The volatile metadata a snapshot must not carry back: `kubectl apply` of a document with a stale
# `resourceVersion` is rejected as a conflict, which turns the restore into a no-op nobody reads.
clean_meta() {
  python3 -c '
import json, sys
doc = json.load(sys.stdin)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
    doc.get("metadata", {}).pop(k, None)
json.dump(doc, sys.stdout)
'
}

# restore_freeze_grant — put the actor's `fleetfreezes` authority back. rc 0 = restored.
#
# Snapshots first, `seed_agent_identity` second, and both, for the reason `broker-refuse-l2.sh`
# gives: the strip is discovery-based and can reach objects no template renders, which a re-seed
# alone would leave stripped forever — and the NEXT run would then find the fault already staged and
# report B-5 green having done nothing.
restore_freeze_grant() {
  [ "$FREEZE_GRANT_STRIPPED" = yes ] || return 0
  echo "  restoring the actor's fleetfreezes grant (snapshots, then a re-seed)"
  local restore_failed=no snap
  if [ -n "$FREEZE_SNAPSHOT_DIR" ] && [ -d "$FREEZE_SNAPSHOT_DIR" ]; then
    for snap in "$FREEZE_SNAPSHOT_DIR"/*.json; do
      [ -f "$snap" ] || continue
      $K apply -f "$snap" >/dev/null 2>&1 || {
        restore_failed=yes
        echo "  WARNING: could not restore $snap" >&2
      }
    done
  fi
  seed_agent_identity "$K" "$NS" "$AGENT" >/dev/null 2>&1 || {
    restore_failed=yes
    echo "  WARNING: the re-seed failed. The actor may still be unable to read FleetFreezes, which" >&2
    echo "  is a broker that refuses EVERY submission with 'scope-frozen' and no freeze in sight." >&2
  }
  if [ "$restore_failed" = no ]; then
    FREEZE_GRANT_STRIPPED=no
    [ -n "$FREEZE_SNAPSHOT_DIR" ] && rm -rf "$FREEZE_SNAPSHOT_DIR"
    return 0
  fi
  if [ -n "$FREEZE_SNAPSHOT_DIR" ]; then
    echo "  the pre-strip grant snapshots are KEPT at $FREEZE_SNAPSHOT_DIR — re-apply them by hand." >&2
  fi
  return 1
}

cleanup() {
  echo
  echo "== cleanup =="
  # ORDER IS THE WHOLE POINT HERE.
  #   1. The control plane, because a cluster left with its operator at zero replicas is the worst
  #      thing this file can do, and because every step after this one needs admission to have a
  #      backend — `vagent.kb.io` fails closed over DELETE as well as UPDATE, so the Agent teardown
  #      below is REFUSED while the controller is gone.
  #   2. The freeze grant, because a broker that cannot read FleetFreezes refuses every submission
  #      in the cluster with `scope-frozen` and gives no clue why.
  #   3. The FleetFreeze objects, because they are cluster-scoped: one left behind stops every agent
  #      in the fleet, including the ones the next suite is about to test.
  restore_control_plane
  restore_freeze_grant
  $K delete fleetfreeze "$FREEZE_COVER" "$FREEZE_ELSEWHERE" --ignore-not-found >/dev/null 2>&1
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  echo
  echo "CLEANED UP: the controller, the brake controller and the router are back at their original"
  echo "  replica counts, the fleetfreezes grant is restored, both FleetFreeze objects are deleted,"
  echo "  the overlays are revoked, the driver pod and its ConfigMap are gone, and the Agent CR is"
  echo "  deleted. THE TENANT NAMESPACE IS LEFT STANDING and so are the ActionRecords in it —"
  echo "  [[LSN-045]]: journal retention denies DELETE of an ActionRecord until export confirms, so"
  echo "  a namespace holding one never finishes terminating and a suite that tried would hang on"
  echo "  its own evidence."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: the tenant namespace, the Agent CR, and the identity its broker runs as =="

printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$TENANT_NS" | $K apply -f - >/dev/null || {
  echo "FAIL: could not create the tenant namespace $TENANT_NS" >&2
  exit 1
}
echo "  tenant namespace: $TENANT_NS"

# Cluster-scoped and therefore fleet-wide. Removed before anything else runs, so a freeze left by a
# killed previous run cannot be the thing that makes A-0 refuse.
$K delete fleetfreeze "$FREEZE_COVER" "$FREEZE_ELSEWHERE" --ignore-not-found >/dev/null 2>&1

# P3: deleted with --wait=true before it is applied, so everything the controller renders from it is
# this generation's — including the gateway Deployment whose `spec.replicas` A-3 is about.
$K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=true >/dev/null 2>&1
$K apply -f "$REPO_ROOT/$AGENT_MANIFEST" >/dev/null || {
  echo "FAIL: could not apply $AGENT_MANIFEST" >&2
  exit 1
}
seed_agent_fixtures "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed fixtures for $AGENT" >&2
  exit 1
}
seed_agent_identity "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed the actor identity for $AGENT" >&2
  exit 1
}

# The write half of the overlay: the executor's server-side dry run is AUTHORIZED before it is
# dry-run, so without it every ACCEPT arm here comes back 403 from inside the executor and looks
# exactly like a brake refusal that is not one.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS; the accepted arms would"
  echo "  be refused by the API server before the pipeline reached anything this suite measures."
  exit 3
}

# The scope the freeze objects have to match, read off the CR (P6) rather than copied out of
# `examples/gitops-repo/fleet/platform-agent.yaml`. `Covers()` compares against exactly this, so a
# suite holding its own copy of the project id would create a freeze that silently covers nothing
# the day the example changes — and B-3 would go red for a reason that has nothing to do with 06.
AGENT_PROJECT="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.spec.scope.projectId}' 2>/dev/null)"
if [ -z "$AGENT_PROJECT" ]; then
  echo "DEFERRED: $AGENT has no spec.scope.projectId, so this suite cannot build a freeze that"
  echo "  provably covers it — and a freeze with a blank scope covers everything, which would make"
  echo "  B-4's non-covering control impossible to write."
  exit 3
fi
echo "  agent scope.projectId: $AGENT_PROJECT"

# Polled, not read once (P9): `status.broker.*` is controller-written and the CR was applied seconds
# ago. A single read here is a race whose loser reports "the controller never published an actor",
# which is a DEFERRED against a cluster that was about to be fine.
ACTOR_SA=""
deadline=$((SECONDS + 120))
while [ "$SECONDS" -lt "$deadline" ]; do
  ACTOR_SA="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.status.broker.actorServiceAccount}' 2>/dev/null)"
  [ -n "$ACTOR_SA" ] && break
  sleep 3
done
if [ -z "$ACTOR_SA" ]; then
  echo "DEFERRED: the controller has not published status.broker.actorServiceAccount for $AGENT, so"
  echo "  arm B cannot discover which role objects to strip."
  exit 3
fi
ACTOR_SUBJECT="system:serviceaccount:$NS:$ACTOR_SA"
echo "  actor subject: $ACTOR_SUBJECT"

# ------------------------------------------------------------------------------------------------
# P1
# ------------------------------------------------------------------------------------------------
echo
echo "== P1: the controller and this agent's broker are the build under test =="

p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the controller. Arm A asks what the RENDERER does with"
    echo "  spec.operations.paused versus spec.deployment.scaleToZero; an unidentifiable controller"
    echo "  makes every replica count below a fact about an unknown build."
    exit 3
    ;;
  *)
    bad "P1: the controller is not running the build under test"
    exit 1
    ;;
esac

BROKER_POD="$(p3_pod_of_deploy "$K" "$NS" "$BROKER_DEPLOY" 180)"
if [ -z "$BROKER_POD" ]; then
  echo "DEFERRED: no pod is owned by deploy/$BROKER_DEPLOY after 180s. There is no brake to test."
  $K -n "$NS" describe "deploy/$BROKER_DEPLOY" 2>&1 | tail -20
  exit 3
fi
echo "  broker pod (by ownership, P3): $BROKER_POD"

p1_assert_build_under_test "$K" "$NS" "kube-agents/agent=$AGENT,kube-agents/role=actor"
case "$?" in
  0) pass "P1: the broker is running the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the broker. Every refusal below is a claim about what"
    echo "  decideGate did with 06 §4.4's table."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

# Polled, not slept on (P9). A fixed wait is how a slow image pull becomes "the brake refused".
echo
echo "== waiting for the broker to become Available =="
avail=""
deadline=$((SECONDS + 240))
while [ "$SECONDS" -lt "$deadline" ]; do
  avail="$($K -n "$NS" get "deploy/$BROKER_DEPLOY" \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)"
  [ "$avail" = "True" ] && break
  sleep 3
done
if [ "$avail" != "True" ]; then
  echo "DEFERRED: deploy/$BROKER_DEPLOY never became Available (condition='${avail:-none}')."
  $K -n "$NS" describe "pod/$BROKER_POD" 2>&1 | tail -25
  exit 3
fi
pass "deploy/$BROKER_DEPLOY is Available"

# ------------------------------------------------------------------------------------------------
# The submission machinery. Eleven runs of the same probe, each in a fresh pod with a fresh nonce
# and a fresh traceId — the dedup triple is (identity, traceId, idempotencyKey) at
# `antireplay.go:227`, so no two of these can collide and a `deduplicated` answer would mean the
# transport stopped varying the trace, which is worth knowing and is not an accept.
# ------------------------------------------------------------------------------------------------
broker_driver_use_probe "$PROBE" || {
  echo "FAIL: $PROBE is not where this suite says it is" >&2
  exit 1
}
# Read by `broker_driver_run`, which wires it into the pod as PROBE_TENANT_NAMESPACE.
# shellcheck disable=SC2034
BROKER_DRIVER_TENANT_NS="$TENANT_NS"
broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
# Unused by this probe; the driver mounts it unconditionally and a pod referencing an absent Secret
# never starts.
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the placeholder keypair the driver pod mounts" >&2
  exit 1
}

FLAT=""
SUBMIT_N=0

submit() { # <label> — sets FLAT. rc 0 = the driver ran and produced a transcript.
  SUBMIT_N=$((SUBMIT_N + 1))
  local label="$1" out rc
  echo
  echo "  -- submission #$SUBMIT_N: $label --"
  # A DISTINCT TARGET PER SUBMISSION, OR THIS SUITE MEASURES ONE ACTION ELEVEN TIMES. 06 §9's
  # idempotency key is a hash over identity + operations + dryRun; the identity and the dryRun flag
  # are fixed for the whole run, so an unvaried target name makes all eleven probes the SAME action.
  # The broker then answers every one after the first `200 decision=deduplicated`, returning the
  # FIRST submission's actionId — which is correct of the broker and fatal to the arms here, because
  # every "the envelope is accepted again now the fault is cleared" arm would be answered by a record
  # minted BEFORE the fault was injected. Measured, not theorised: the run that found this scored one
  # `accepted` and ten `deduplicated` against a single actionId ([[LSN-067]]).
  BROKER_DRIVER_TARGET_NAME="brake-l2-shadow-target-$SUBMIT_N"
  out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
  rc=$?
  echo "$out" | sed 's/^/  | /'
  if [ "$rc" -ne 0 ]; then
    FLAT=""
    return 1
  fi
  FLAT="$(printf '%s\n' "$out" | python3 -c '
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    status = r.get("status")
    print("\t".join([
        r.get("scenario") or "",
        r.get("outcome") or "",
        "" if status is None else str(status),
        r.get("reason") or "",
        r.get("decision") or "",
        r.get("phase") or "",
        r.get("actionId") or "",
        r.get("namespace") or "",
        (r.get("detail") or "").replace("\t", " ")[:1000],
    ]))
')"
  [ -n "$FLAT" ] || return 1
  return 0
}

field() { # <scenario> <1=outcome 2=status 3=reason 4=decision 5=phase 6=actionId 7=namespace 8=detail>
  printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" -v i="$(($2 + 1))" '$1 == s { print $i; exit }'
}

# reply — "<outcome> <status> <reason> <decision> <actionId>" as five words plus the detail, printed
# for the human on every judgement so a red line carries its own diagnosis.
reply_summary() {
  printf "outcome='%s' status='%s' reason='%s' decision='%s' actionId='%s' detail='%s'" \
    "$(field shadow-submit 1)" "$(field shadow-submit 2)" "$(field shadow-submit 3)" \
    "$(field shadow-submit 4)" "$(field shadow-submit 6)" "$(field shadow-submit 8)"
}

# expect_accepted / expect_refused — COLLECTORS, not judgements. Each pulls the fields the arm needs
# out of this submission's transcript and hands them to `judge_accepted` / `judge_refused` in the
# judgement block at the top of the file, which is where the verdicts now live and where
# `--negative-control` can reach them with no driver pod, no broker and no cluster. The call sites
# below are unchanged, deliberately: the ¬ arm is worth nothing if adding it moved the live path.
expect_accepted() { # <check-id> <why>
  judge_accepted "$1" "$2" \
    "$(field shadow-submit 1)" "$(field shadow-submit 2)" \
    "$(field shadow-submit 4)" "$(field shadow-submit 6)" "$(reply_summary)"
}

expect_refused() { # <check-id> <want-status> <want-reason> <needle> <why>
  judge_refused "$1" "$2" "$3" "$4" "$5" \
    "$(field shadow-submit 1)" "$(field shadow-submit 2)" "$(field shadow-submit 3)" \
    "$(field shadow-submit 4)" "$(field shadow-submit 8)" "$(reply_summary)"
}

# gateway_replicas / gateway_pod_identity — the two readings arm A is built on.
gateway_replicas() { $K -n "$NS" get "deploy/$GATEWAY_DEPLOY" -o jsonpath='{.spec.replicas}' 2>/dev/null; }
gateway_pod_identity() { # <pod> — "<uid> <startTime> <phase>"
  $K -n "$NS" get "pod/$1" \
    -o jsonpath='{.metadata.uid} {.status.startTime} {.status.phase}' 2>/dev/null
}

broker_restarts() { # per-container restartCounts, joined not summed: a pod where one container
                    # restarts and another is added must not cancel out to the same number.
  $K -n "$NS" get "pod/$BROKER_POD" \
    -o jsonpath='{range .status.containerStatuses[*]}{.name}={.restartCount},{end}' 2>/dev/null
}

# wait_paused_status — the CONTROLLER's own view of the brake, polled (P9). Advisory: it is a second
# witness that the patch landed, not the property under test, and it cannot be used in arm C at all
# because there is no controller there to write it. A run where it times out still proceeds — the
# broker reads the SPEC, and the spec is what was patched.
wait_paused_status() { # <want: true|false>
  local want="$1" got="" deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    got="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.status.operations.paused}' 2>/dev/null)"
    [ -z "$got" ] && got=false
    [ "$got" = "$want" ] && break
    sleep 2
  done
  echo "  controller's status.operations.paused = ${got:-<unset>} (wanted $want)"
}

settle_brake() {
  echo "  waiting ${BRAKE_TTL_SETTLE}s — three brake cache TTLs — so the next answer cannot come"
  echo "  from a snapshot taken before the control moved"
  sleep "$BRAKE_TTL_SETTLE"
}

patch_agent() { # <merge-patch json>
  $K -n "$NS" patch agent "$AGENT" --type=merge -p "$1" >/dev/null 2>&1
}

# ================================================================================================
# A — V-RUN-007
# ================================================================================================
echo
echo "===================================================================="
echo " A — V-RUN-007: pause is not scale-to-zero"
echo "===================================================================="

GW_REPLICAS_BEFORE="$(gateway_replicas)"
[ -n "$GW_REPLICAS_BEFORE" ] || GW_REPLICAS_BEFORE=0
GW_POD_BEFORE="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY_DEPLOY" 120)"
if [ -z "$GW_POD_BEFORE" ] || [ "$GW_REPLICAS_BEFORE" = "0" ]; then
  echo "DEFERRED: deploy/$GATEWAY_DEPLOY has no pod (replicas=$GW_REPLICAS_BEFORE) before anything"
  echo "  here has paused it. V-RUN-007 is a claim about a pod that survives the pause; with no pod"
  echo "  to survive, A-3 and A-4 would both be satisfied by an agent that was never running."
  exit 3
fi
GW_IDENTITY_BEFORE="$(gateway_pod_identity "$GW_POD_BEFORE")"
echo "  gateway before: deploy replicas=$GW_REPLICAS_BEFORE pod=$GW_POD_BEFORE [$GW_IDENTITY_BEFORE]"

submit "A-0 baseline: unpaused, unfrozen" || {
  echo "DEFERRED: the baseline submission could not be run to completion, so nothing below has a"
  echo "  control. Every refusal in this file is only evidence against an accept that worked."
  exit 3
}
# The baseline is judged BEFORE anything is scored, and a failure here is could-not-run rather than
# a red — `broker-execute-l2.sh`'s L2-0 makes the same distinction for the same reason. An envelope
# refused with no brake engaged says the fixture is wrong, not that the brake is; filing it as a
# V-RUN-007 failure would put a red against a row this run never reached.
b_outcome="$(field shadow-submit 1)"
b_decision="$(field shadow-submit 4)"
b_action="$(field shadow-submit 6)"
case "$b_outcome/$b_decision" in
  http/accepted | http/gated | http/executed) : ;;
  *)
    echo
    echo "DEFERRED: the broker refused an envelope with no brake engaged, so nothing below has a"
    echo "  control. $(reply_summary)"
    echo "  Diagnose here: a 'scope-frozen' answer with no FleetFreeze in the cluster is 06 §4.4"
    echo "  row 1 already firing, which means the actor's fleetfreezes grant was missing before this"
    echo "  suite touched it; a 403 from inside the executor is the write overlay not taking; a 401"
    echo "  is broker-auth-l2.sh's problem and not this suite's."
    $K -n "$NS" logs "pod/$BROKER_POD" --tail=40 2>/dev/null | sed 's/^/  broker| /'
    exit 3
    ;;
esac
if [ -z "$b_action" ]; then
  echo
  echo "DEFERRED: the baseline was accepted and named no actionId, so there is no evidence the"
  echo "  pipeline ran at all. $(reply_summary)"
  exit 3
fi
pass "V-RUN-007: the baseline envelope is ACCEPTED — HTTP $(field shadow-submit 2), decision '$b_decision', actionId $b_action. Every refusal below is measured against this one accept"

echo
echo "-- pausing, with a reason string minted by this process --"
patch_agent "$(printf '{"spec":{"operations":{"paused":true,"pauseReason":"%s"}}}' "$PAUSE_NONCE")" || {
  echo "DEFERRED: could not patch spec.operations.paused on $AGENT. The brake was never engaged."
  exit 3
}
wait_paused_status true
settle_brake

submit "A-1 paused" || {
  echo "DEFERRED: the paused submission could not be run to completion."
  exit 3
}
expect_refused "V-RUN-007" 403 agent-paused "spec.operations.paused" \
  "a paused agent's envelope is refused by the brake"

# A-2 is a separate assertion from A-1 on purpose — see `judge_pause_nonce`.
a1_detail="$(field shadow-submit 8)"
judge_pause_nonce "$a1_detail" "$PAUSE_NONCE"

gw_replicas_paused="$(gateway_replicas)"
[ -n "$gw_replicas_paused" ] || gw_replicas_paused=0
judge_gateway_replicas "$GW_REPLICAS_BEFORE" "$gw_replicas_paused"

gw_pod_paused="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY_DEPLOY" 30)"
gw_identity_paused="$(gateway_pod_identity "$gw_pod_paused")"
judge_same_pod "$GW_POD_BEFORE" "$GW_IDENTITY_BEFORE" "$gw_pod_paused" "$gw_identity_paused"

echo
echo "-- ¬ control: what a real scale-to-zero looks like on this CRD --"
if ! patch_agent '{"spec":{"deployment":{"scaleToZero":true}}}'; then
  bad "V-RUN-007: could not set spec.deployment.scaleToZero, so the two assertions above are unfalsified — they may hold because nothing here can move the replica count at all"
  bad "V-RUN-007: (the paired restore arm cannot run either)"
else
  drained=no
  deadline=$((SECONDS + 180))
  while [ "$SECONDS" -lt "$deadline" ]; do
    # `.status.replicas` disappears entirely at zero, so an empty read is the converged answer.
    if [ -z "$($K -n "$NS" get "deploy/$GATEWAY_DEPLOY" -o jsonpath='{.status.replicas}' 2>/dev/null)" ] &&
      [ "$(gateway_replicas)" = "0" ]; then
      drained=yes
      break
    fi
    sleep 3
  done
  gw_pod_zero="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY_DEPLOY" 20)"
  # Read unconditionally rather than inside the failure message it used to sit in: the judgement is
  # a function over already-read values now, and a message that goes and fetches its own evidence is
  # a second read of a moving object taken after the verdict was decided.
  gw_replicas_zero="$(gateway_replicas)"
  judge_scale_to_zero_drained "$drained" "$gw_replicas_zero" "$gw_pod_zero"

  patch_agent '{"spec":{"deployment":{"scaleToZero":false}}}'
  gw_pod_restored=""
  deadline=$((SECONDS + 180))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$(gateway_replicas)" != "0" ]; then
      gw_pod_restored="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY_DEPLOY" 20)"
      [ -n "$gw_pod_restored" ] && break
    fi
    sleep 3
  done
  gw_identity_restored="$(gateway_pod_identity "$gw_pod_restored")"
  gw_replicas_restored="$(gateway_replicas)"
  judge_scale_cycle_replaced_the_pod \
    "$gw_pod_restored" "$gw_identity_restored" "$GW_IDENTITY_BEFORE" "$gw_replicas_restored"
fi

echo
echo "-- releasing the pause --"
patch_agent '{"spec":{"operations":{"paused":false,"pauseReason":null}}}'
wait_paused_status false
settle_brake

if submit "A-7 unpaused"; then
  expect_accepted "V-RUN-007" "unpausing releases the brake — the write path opens again"
else
  bad "V-RUN-007: the post-unpause submission could not be run, so it is not known whether the brake released"
fi

# ================================================================================================
# B — V-CTR-007
# ================================================================================================
echo
echo "===================================================================="
echo " B — V-CTR-007: the brake objects, per 06 §4.4's contract"
echo "===================================================================="

apply_freeze() { # <name> <projectId> <reason>
  cat <<EOF | $K apply -f - >/dev/null 2>&1
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata:
  name: $1
spec:
  scope:
    projectId: "$2"
  reason: "$3"
  requestedBy: "k8s:brake-l2"
EOF
}

echo
echo "-- B-1: the served schema's defaults, read back off the stored object --"
$K delete fleetfreeze "$FREEZE_COVER" --ignore-not-found >/dev/null 2>&1
if ! apply_freeze "$FREEZE_COVER" "$AGENT_PROJECT" "brake-l2: V-CTR-007 covering freeze"; then
  echo "DEFERRED: a well-formed FleetFreeze was refused by the API server. Arm B cannot ask its"
  echo "  question until a valid brake object can be created at all."
  $K apply --dry-run=server -f - <<EOF 2>&1 | sed 's/^/  | /'
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata:
  name: $FREEZE_COVER
spec:
  scope:
    projectId: "$AGENT_PROJECT"
  reason: "brake-l2: V-CTR-007 covering freeze"
  requestedBy: "k8s:brake-l2"
EOF
  exit 3
fi
ff_allow_undo="$($K get fleetfreeze "$FREEZE_COVER" -o jsonpath='{.spec.allowUndo}' 2>/dev/null)"
ff_allow_classes="$($K get fleetfreeze "$FREEZE_COVER" -o jsonpath='{.spec.allowClasses}' 2>/dev/null)"
judge_freeze_defaults "$ff_allow_undo" "$ff_allow_classes"

echo
echo "-- B-2 ¬: the contract rejects malformed brake objects (--dry-run=server, nothing persisted) --"
try_bad_freeze() { # <label> <yaml-body> — rc 0 when the API server REFUSED it
  local label="$1" body="$2" out rc
  out="$(printf '%s\n' "$body" | $K apply --dry-run=server -f - 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "  ACCEPTED (it should not have been): $label"
    return 1
  fi
  echo "  refused, as it must be: $label"
  printf '%s\n' "$out" | tail -2 | sed 's/^/    | /'
  return 0
}
b2_bad=0
try_bad_freeze "requestedBy: 'nobody' (pattern ^(slack|googlechat|k8s):\\S+\$)" "$(
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata:
  name: brake-l2-bad-requestedby
spec:
  scope: {projectId: "$AGENT_PROJECT"}
  reason: "brake-l2 contract probe"
  requestedBy: "nobody"
EOF
)" || b2_bad=$((b2_bad + 1))
try_bad_freeze "allowClasses: [gated] (enum admits 'routine' only)" "$(
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata:
  name: brake-l2-bad-classes
spec:
  scope: {projectId: "$AGENT_PROJECT"}
  reason: "brake-l2 contract probe"
  requestedBy: "k8s:brake-l2"
  allowClasses: ["gated"]
EOF
)" || b2_bad=$((b2_bad + 1))
try_bad_freeze "reason: '' (MinLength 1 — a freeze with no stated cause)" "$(
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata:
  name: brake-l2-bad-reason
spec:
  scope: {projectId: "$AGENT_PROJECT"}
  reason: ""
  requestedBy: "k8s:brake-l2"
EOF
)" || b2_bad=$((b2_bad + 1))
judge_malformed_freezes_refused "$b2_bad" 3

echo
echo "-- B-3: a covering freeze stops the action --"
# `frozenBy` is the controller's own view, polled (P9) so the arm does not race the reconcile.
frozen_by=""
deadline=$((SECONDS + 90))
while [ "$SECONDS" -lt "$deadline" ]; do
  frozen_by="$($K -n "$NS" get agent "$AGENT" -o jsonpath='{.status.operations.frozenBy}' 2>/dev/null)"
  [ "$frozen_by" = "$FREEZE_COVER" ] && break
  sleep 3
done
echo "  controller's status.operations.frozenBy = '${frozen_by:-<unset>}'"
settle_brake

if submit "B-3 covering freeze"; then
  expect_refused "V-CTR-007" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
    "a FleetFreeze whose scope matches the agent's projectId stops the action, and the refusal names the object a human has to delete"
else
  bad "V-CTR-007: the covering-freeze submission could not be run"
fi

echo
echo "-- B-4 ¬: a freeze that does not cover this scope must NOT stop it --"
$K delete fleetfreeze "$FREEZE_COVER" --ignore-not-found >/dev/null 2>&1
apply_freeze "$FREEZE_ELSEWHERE" "brake-l2-some-other-project" "brake-l2: V-CTR-007 non-covering control"
settle_brake
if submit "B-4 non-covering freeze"; then
  expect_accepted "V-CTR-007 ¬" "a FleetFreeze scoped to a different projectId does not stop this agent — Covers() is comparing, so B-3 is not 'any freeze anywhere refuses'"
else
  bad "V-CTR-007 ¬: the non-covering-freeze submission could not be run, so B-3 is unfalsified"
fi
$K delete fleetfreeze "$FREEZE_ELSEWHERE" --ignore-not-found >/dev/null 2>&1

echo
echo "-- B-5: 06 §4.4 row 1 — an UNREADABLE FleetFreeze list freezes the scope --"
echo "  no FleetFreeze exists in the cluster now; anything that refuses from here is refusing"
echo "  because it cannot SEE the list, which is the row."

BROKER_POD_BEFORE_FAULT="$BROKER_POD"
RESTARTS_BEFORE_FAULT="$(broker_restarts)"
echo "  broker before the fault: $BROKER_POD_BEFORE_FAULT (restarts: ${RESTARTS_BEFORE_FAULT:-<none>})"

# Every object that carries the grant, DISCOVERED. `broker-operations-grant.yaml.template:66` puts
# `fleetfreezes get/list/watch` on the shared ClusterRole and `actor-grant-platform.yaml.template`
# puts it on the per-tier one, so stripping either alone leaves the other satisfying the List and
# the fault is never staged. And naming those two is still not enough — `broker-refuse-l2.sh` found
# residue role objects on this very cluster that no template renders and nothing removes. Walking
# the bindings tests the grant the authorizer actually consults.
#
# `fleetfreezes` is the ONLY resource removed. `tokenreviews` (authentication.k8s.io) and
# `changepolicies` sit in separate rules in both templates, so the actor keeps its authentication
# and its classification input: the fault under test is "the freeze list is unreadable", not "the
# broker's identity has no authority", and a refusal satisfying two rows at once is evidence for
# neither.
drop_fleetfreeze_rules() {
  python3 -c '
import json, sys
GONE = {"fleetfreezes", "fleetfreezes/status"}
doc = json.load(sys.stdin)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
    doc.get("metadata", {}).pop(k, None)
kept = []
for rule in doc.get("rules") or []:
    res = [r for r in (rule.get("resources") or []) if r not in GONE]
    if not res:
        continue
    rule["resources"] = res
    kept.append(rule)
doc["rules"] = kept
json.dump(doc, sys.stdout)
'
}

# `<kind> <name>` for every role object bound to the actor's ServiceAccount. The kind comes out of
# each `roleRef` rather than being assumed, because a RoleBinding may point at a ClusterRole.
# Subjects are matched on kind, name AND namespace — a same-named ServiceAccount elsewhere is a
# different identity, and stripping its grant is collateral damage in a namespace nobody named.
bound_role_refs() {
  $K get clusterrolebindings -o json 2>/dev/null | python3 -c '
import json, sys
name, ns = sys.argv[1], sys.argv[2]
for b in (json.load(sys.stdin).get("items") or []):
    for s in (b.get("subjects") or []):
        if s.get("kind") == "ServiceAccount" and s.get("name") == name and s.get("namespace") == ns:
            print("clusterrole", (b.get("roleRef") or {}).get("name", ""))
            break
' "$ACTOR_SA" "$NS"
  $K -n "$NS" get rolebindings -o json 2>/dev/null | python3 -c '
import json, sys
name, ns = sys.argv[1], sys.argv[2]
for b in (json.load(sys.stdin).get("items") or []):
    for s in (b.get("subjects") or []):
        if s.get("kind") == "ServiceAccount" and s.get("name") == name and s.get("namespace") in (ns, None):
            ref = b.get("roleRef") or {}
            print(ref.get("kind", "").lower(), ref.get("name", ""))
            break
' "$ACTOR_SA" "$NS"
}

# The positive control for the strip: if the actor could not read freezes BEFORE, there is no fault
# to stage and B-5 would be measuring a cluster that was already broken.
if [ "$($K auth can-i list fleetfreezes --as="$ACTOR_SUBJECT" 2>/dev/null)" != "yes" ]; then
  echo "DEFERRED: $ACTOR_SUBJECT cannot list FleetFreezes before this suite revoked anything, so"
  echo "  row 1 may already have been firing throughout arms A and B. Nothing here would be"
  echo "  evidence about a revocation this run performed."
  exit 3
fi

FREEZE_SNAPSHOT_DIR="$(mktemp -d)"
# Set BEFORE the first apply, not after the loop: a run killed mid-loop has stripped something, and
# the trap must know to repair even when it cannot know how far the loop got.
FREEZE_GRANT_STRIPPED=yes
stripped=0
# A heredoc rather than a pipe so the loop runs in THIS shell and `stripped` survives it.
while read -r ref_kind ref_name; do
  [ -n "$ref_name" ] || continue
  case "$ref_kind" in
    clusterrole) doc="$($K get clusterrole "$ref_name" -o json 2>/dev/null)" ;;
    role) doc="$($K -n "$NS" get role "$ref_name" -o json 2>/dev/null)" ;;
    *) continue ;;
  esac
  [ -n "$doc" ] || continue
  printf '%s' "$doc" | grep -q '"fleetfreezes' || continue
  printf '%s' "$doc" | clean_meta >"$FREEZE_SNAPSHOT_DIR/$ref_kind.$ref_name.json"
  printf '%s' "$doc" | drop_fleetfreeze_rules | $K apply -f - >/dev/null 2>&1
  echo "  stripped fleetfreezes from $ref_kind/$ref_name"
  stripped=$((stripped + 1))
done <<EOF
$(bound_role_refs | sort -u)
EOF

if [ "$stripped" -eq 0 ]; then
  echo "DEFERRED: no role object bound to $ACTOR_SUBJECT grants fleetfreezes, so there was nothing"
  echo "  to revoke and no fault to stage — even though the authorizer said yes a moment ago, which"
  echo "  means the grant arrives from somewhere this walk does not reach."
  exit 3
fi

# Converged on, not slept through (P9). RBAC propagates through the authorizer's caches, and a fixed
# sleep is how "the broker never saw the fault" becomes "the broker ignored the fault".
echo "  waiting for the authorizer to agree the grant is gone"
gone=no
deadline=$((SECONDS + 60))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ "$($K auth can-i list fleetfreezes --as="$ACTOR_SUBJECT" 2>/dev/null)" = "no" ]; then
    gone=yes
    break
  fi
  sleep 2
done
if [ "$gone" != yes ]; then
  echo "DEFERRED: the actor can still list FleetFreezes after 60s, so the fault was never staged."
  echo "  B-5 would submit into a healthy brake and assert a refusal that is not coming."
  exit 3
fi
echo "  the authorizer says no; the FleetFreeze list is unreadable to the broker's identity"

# Row 1 fires on STALENESS, not only on an error: `MaxFreezeStaleness` is 30s and the source keeps
# serving its last observation until then. The wait has to clear that window, not just the 5s TTL.
echo "  waiting 40s — past 06 §4.4's 30s MaxFreezeStaleness — so the cache ages into row 1 on its own"
sleep 40

if submit "B-5 fleetfreezes unreadable"; then
  # "undo still runs" is row 1's phrase. It appears in BOTH branches of `freezeUnreadableDetail`
  # (brake.go:751 and 757-758) — the outright-unreadable one and the aged-past-30s one, either of
  # which is the row — and in NEITHER branch of `frozenDetail`. So it is the one needle that
  # separates "a freeze covers you" from "we cannot tell whether one does", which is the whole
  # difference this arm exists to measure: both answers are 403 `scope-frozen`, and only one of them
  # is a state an operator can fix by deleting an object.
  expect_refused "V-CTR-007" 403 scope-frozen "undo still runs" \
    "with NO FleetFreeze in the cluster and the list unreadable, the scope is treated as frozen — 06 §4.4 row 1, fail-closed"
else
  bad "V-CTR-007: the fail-closed submission could not be run, so 06 §4.4 row 1 is unmeasured"
fi

broker_pod_during_fault="$(p3_pod_of_deploy "$K" "$NS" "$BROKER_DEPLOY" 30)"
restarts_during_fault="$(broker_restarts)"
judge_broker_in_place degraded \
  "$BROKER_POD_BEFORE_FAULT" "$RESTARTS_BEFORE_FAULT" \
  "$broker_pod_during_fault" "$restarts_during_fault"

echo
echo "-- B-7: and it recovers, also without restarting --"
# Exactly two assertions on every path through this block — B-7a (the refusal lifted) and B-7b (it
# lifted in place) — because a branch that renders fewer is a branch that answers the count check
# instead of the question.
b7_ran=no
if restore_freeze_grant; then
  echo "  waiting for the authorizer to agree the grant is back"
  back=no
  deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$($K auth can-i list fleetfreezes --as="$ACTOR_SUBJECT" 2>/dev/null)" = "yes" ]; then
      back=yes
      break
    fi
    sleep 2
  done
  if [ "$back" = yes ]; then
    settle_brake
    if submit "B-7 grant restored"; then
      b7_ran=yes
      expect_accepted "V-CTR-007" "restoring the grant lifts the fail-closed refusal — the scope stops being treated as frozen the moment the list is legible again"
      broker_pod_after="$(p3_pod_of_deploy "$K" "$NS" "$BROKER_DEPLOY" 30)"
      restarts_after="$(broker_restarts)"
      judge_broker_in_place recovered \
        "$BROKER_POD_BEFORE_FAULT" "$RESTARTS_BEFORE_FAULT" \
        "$broker_pod_after" "$restarts_after"
    fi
  else
    echo "  the authorizer still refuses the actor after the restore"
  fi
fi
if [ "$b7_ran" != yes ]; then
  bad "V-CTR-007: the fleetfreezes grant could not be restored and re-tested, so it is not known whether the fail-closed refusal ever lifts — and the cluster may be left with a broker that refuses every submission"
  bad "V-CTR-007: (the paired in-place-recovery reading could not be taken either)"
fi

# ================================================================================================
# C — V-RUN-008
# ================================================================================================
echo
echo "===================================================================="
echo " C — V-RUN-008: the brake with the controller down and inference down"
echo "===================================================================="

CTRL_REPLICAS_BEFORE="$(replicas_of "$CTRL_DEPLOY")"
BRAKE_REPLICAS_BEFORE="$(replicas_of "$BRAKE_DEPLOY")"
ROUTER_REPLICAS_BEFORE="$(replicas_of "$ROUTER_DEPLOY")"
echo "  replica counts to restore: $CTRL_DEPLOY=${CTRL_REPLICAS_BEFORE:-?} $BRAKE_DEPLOY=${BRAKE_REPLICAS_BEFORE:-?} $ROUTER_DEPLOY=${ROUTER_REPLICAS_BEFORE:-?}"
if [ "${CTRL_REPLICAS_BEFORE:-0}" = "0" ]; then
  echo "DEFERRED: $CTRL_DEPLOY is already at 0 replicas. Scaling a Deployment that is already down"
  echo "  and calling the result 'the controller is down' proves nothing about this run."
  exit 3
fi

echo
echo "-- taking the control plane and the inference path down --"
CONTROL_PLANE_DOWN=yes
scale_to "$CTRL_DEPLOY" 0
scale_to "$BRAKE_DEPLOY" 0
scale_to "$ROUTER_DEPLOY" 0

echo "  waiting for the webhook service to lose its backend"
wh_eps=1
deadline=$((SECONDS + 180))
while [ "$SECONDS" -lt "$deadline" ]; do
  wh_eps="$(endpoint_count "$WEBHOOK_SVC")"
  [ "${wh_eps:-1}" -eq 0 ] && break
  sleep 3
done

# The gateway is scaled DIRECTLY rather than through `spec.deployment.scaleToZero`, and only AFTER
# the controller has stopped serving: with nothing reconciling the Deployment the zero sticks, and
# it is visibly not the controller's doing. Ordering matters — issued before the controller is gone,
# this is a race the reconcile loop wins, and C-2 goes red for a reason that is not the property.
# Arm A already proved the CRD field works, so nothing is lost by using the blunt lever here.
scale_to "$GATEWAY_DEPLOY" 0

# The positive control for the outage: `vagent.kb.io` is failurePolicy Fail over CREATE/UPDATE/
# DELETE on `agents`, and its Service selects `control-plane: controller-manager`. With no endpoint,
# an Agent write MUST be rejected. If it is not, the controller is still reachable somehow and every
# refusal below would be a normal-service refusal wearing an outage costume.
agent_write_out="$($K -n "$NS" patch agent "$AGENT" --type=merge \
  -p '{"metadata":{"annotations":{"brake-l2/outage-probe":"1"}}}' 2>&1)"
agent_write_rc=$?
judge_outage_real "$wh_eps" "$agent_write_rc" "$agent_write_out"

gw_replicas_down="$(gateway_replicas)"
gw_pod_down="$(p3_pod_of_deploy "$K" "$NS" "$GATEWAY_DEPLOY" 60)"
router_eps="$(endpoint_count "$ROUTER_SVC")"
router_replicas_down="$(replicas_of "$ROUTER_DEPLOY")"
judge_inference_down "$gw_replicas_down" "$gw_pod_down" "$router_replicas_down" "$router_eps"

settle_brake
if submit "C-3 everything down, no brake engaged"; then
  expect_accepted "V-RUN-008" "the broker still serves with the controller, the brake controller, the router and the agent pod all at zero — so the two refusals below are the brake, not a corpse"
else
  echo "DEFERRED: the broker could not be reached with the control plane down. That is a finding"
  echo "  about the broker's own dependencies and it belongs in a row about availability; without"
  echo "  it, C-4 and C-6 cannot distinguish a brake from an outage."
  exit 3
fi

echo
echo "-- C-4: engaging the brake with no controller --"
# `vfleetfreeze.kb.io` is failurePolicy Ignore, which is why this is possible at all: the fleet-wide
# stop stays reachable when the thing that would validate it is the thing that is broken.
if ! apply_freeze "$FREEZE_COVER" "$AGENT_PROJECT" "brake-l2: V-RUN-008 freeze with the controller down"; then
  bad "V-RUN-008: a FleetFreeze could not be created with the controller down. 03 §6's brake is not reachable during the outage it exists for"
  bad "V-RUN-008: (the paired release arm cannot run either)"
else
  echo "  FleetFreeze $FREEZE_COVER created with no controller running"
  settle_brake
  if submit "C-4 frozen, controller down"; then
    expect_refused "V-RUN-008" 403 scope-frozen "FleetFreeze $FREEZE_COVER covers this scope" \
      "a freeze created during the outage is ENFORCED during the outage — the brake decision needs no controller"
  else
    bad "V-RUN-008: the frozen-with-controller-down submission could not be run"
  fi

  echo
  echo "-- C-5: releasing it, still with no controller --"
  $K delete fleetfreeze "$FREEZE_COVER" --ignore-not-found >/dev/null 2>&1
  settle_brake
  if submit "C-5 freeze deleted, controller still down"; then
    expect_accepted "V-RUN-008" "deleting the freeze releases the fleet during the outage — the brake is reversible without the controller, which is what stops it becoming a second incident"
  else
    bad "V-RUN-008: the post-release submission could not be run, so it is not known whether the freeze could be lifted"
  fi
fi

echo
echo "-- C-6: pause, enforced with the controller down --"
# The pause has to be SET while admission has a backend — see the NOTE in this file's header. The
# controller then goes away again, and what is measured is enforcement, not invocability.
if ! restore_control_plane; then
  bad "V-RUN-008: the control plane could not be brought back to stage the pause, so C-6 did not run"
else
  if ! patch_agent "$(printf '{"spec":{"operations":{"paused":true,"pauseReason":"%s (controller-down arm)"}}}' "$PAUSE_NONCE")"; then
    bad "V-RUN-008: could not pause the agent while the controller was up, so C-6 did not run"
  else
    wait_paused_status true
    CONTROL_PLANE_DOWN=yes
    scale_to "$CTRL_DEPLOY" 0
    scale_to "$BRAKE_DEPLOY" 0
    scale_to "$ROUTER_DEPLOY" 0
    echo "  waiting for the webhook service to lose its backend again"
    wh_eps=1
    deadline=$((SECONDS + 180))
    while [ "$SECONDS" -lt "$deadline" ]; do
      wh_eps="$(endpoint_count "$WEBHOOK_SVC")"
      [ "${wh_eps:-1}" -eq 0 ] && break
      sleep 3
    done
    # After the controller has stopped, for the ordering reason given in window 1.
    scale_to "$GATEWAY_DEPLOY" 0
    settle_brake
    if [ "${wh_eps:-1}" -ne 0 ]; then
      bad "V-RUN-008: the controller did not go back down (webhook endpoints='$wh_eps'), so C-6 would be measuring a healthy cluster"
    elif submit "C-6 paused, controller down"; then
      expect_refused "V-RUN-008" 403 agent-paused "spec.operations.paused" \
        "a paused agent stays paused with the controller, the brake controller, the router and its own pod all gone — enforcement is the broker's, read straight off the CR"
    else
      bad "V-RUN-008: the paused-with-controller-down submission could not be run"
    fi
  fi
fi

# The body's own restore, so a run that reaches here leaves a working cluster whether or not the
# trap fires cleanly, and so the unpause below has admission to go through.
restore_control_plane
patch_agent '{"spec":{"operations":{"paused":false,"pauseReason":null}}}'

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-RUN-007 · V-CTR-007 · V-RUN-008 at L2"
  echo " Pausing an agent closed its write path and left its pod — same uid, same startTime, same"
  echo " replica count — where a real scale-to-zero replaced it. A FleetFreeze stopped only the"
  echo " scope it covers, defaulted undo open and every risk class closed, refused three malformed"
  echo " forms, and froze the fleet when the broker could no longer READ the list — degrading and"
  echo " recovering in place, with no restart. And every one of those decisions was still made with"
  echo " the controller, the brake controller, the router and the agent's own pod at zero replicas."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
