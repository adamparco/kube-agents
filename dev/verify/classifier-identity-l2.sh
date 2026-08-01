#!/usr/bin/env bash
# V-GAT-002 at L2 — the wired-in classifier is the same one the corpus tests, walked one rung per
# risk class against a DEPLOYED broker (09 §6, 03 §5, 06 §4.2).
#
# The row is `| V-GAT-002 | The wired-in classifier is the same one the corpus tests — an L2
# spot-check per class | 03 §5 | L2 | 9 |`. Its subject is IDENTITY: `classify.Classify` is scored
# against `verification/fixtures/classifier-corpus.yaml` by a Go table test that never boots a
# broker, and this file asks whether the binary the cluster is actually running answers the same way
# for the same staged world. Nothing else in the tree asks that. Every other L2 suite that touches a
# class reads whatever class the broker happened to choose and partitions on it.
#
# THE SHAPE IS A LADDER, NOT A SOAK, AND THAT IS DELIBERATE
#   The obvious construction — bolt a per-case class arm onto `undo-coverage-l2.sh`'s thirty-seven
#   envelope soak — was built, tested and REJECTED, on three counts that hold regardless of how it
#   is written:
#     1. THE SOAK POPULATION HAS TWO CLASSES, NOT FOUR. `soak_corpus.derive()` excludes every gated
#        and forbidden corpus row BY DESIGN (29 routine / 8 elevated), so a spot-check "per class"
#        run over it would be a spot-check per HALF the classes and would say so in no output.
#     2. THE SOAK DOES NOT REPRODUCE THE FIXTURE WORLD. It re-addresses every case to one tenant
#        namespace and synthesizes its own patch body (`undo_coverage_probe.patch_body`), never
#        replaying `touchedPaths`. All eight elevated expectations are facts about the fixture's
#        world, so the arm would go red eight times for reasons that are the harness's, not the
#        classifier's.
#     3. BOTH FIXTURES FORBID IT IN THEIR OWN WORDS. `soak_corpus.py:25-38` — "An assertion that the
#        live class equals `expect.class` would be a second V-MET-005 wearing V-REV-001's ID" —
#        and `undo_coverage_probe.py:52-57` — "the suite is under standing instruction never to
#        assert the live class equals it".
#   So this file stages FOUR worlds, one per class, each differing from the last by exactly ONE
#   object-scoped lever, and asks the deployed broker one question per rung. It reads the corpus for
#   the LEVERS, never for a per-case expectation.
#
# ================================================================================================
# THE BLOCKER, NAMED LOUDLY: `routine` IS NOT OBSERVABLE ON A PHASE-9 BROKER
# ================================================================================================
#   `history.derive()` (k8s-operator/internal/broker/history/history.go) skips every record with
#   `spec.dryRun` set, under the comment "An action that was never executed is not experience. This
#   is the whole of Phase 9." 07 §2 (docs/design/07-implementation-roadmap.md:95-97) requires this
#   phase to run "with **no write authority anywhere** … the broker runs every action in dry-run",
#   so nothing can ever reach `PhaseVerified` and `KnownActions.Seen()` is false for every caller,
#   verb, kind and namespace. `novel-action (+1)` therefore fires on 100% of traffic.
#
#   This is not a deduction from source. A survey of every ActionRecord on the scratch cluster found
#   ZERO in `PhaseVerified`, ZERO classified `routine`, and every classified record carrying
#   `novel-action | +1 | this agent has not done <verb> on a <kind> in <ns> before`.
#
#   `Escalate` is applied ONCE (classify.go:305, "Applied ONCE, not once per escalation") and caps
#   at gated, so the whole class map is displaced upward by exactly one step, in the SAFE direction:
#
#       corpus base    routine → elevated | elevated → gated | gated → gated | forbidden → forbidden
#
#   Three of the four rungs below therefore match the corpus class EXACTLY. The routine rung does
#   not, and cannot, until an agent has write authority; it is asserted at `elevated` WITH
#   `novel-action` as its only escalation and with `production-environment` and `object-override`
#   asserted ABSENT, which is the strongest statement about the routine base that a dry-run-only
#   phase admits. Warming the history is not an option a harness has: it would require a real
#   execution, and Phase 9 forbids one by design. THE ROUTINE RUNG'S CORPUS-CLASS AGREEMENT IS
#   DEFERRED TO THE PHASE THAT GRANTS WRITE AUTHORITY (P10), and this file says so in its output.
#
#   The same `+1` kills one of the levers the corpus offers. `gat-141` (`kube-agents/risk-class:
#   elevated`) is DEAD at Phase 9: novelty has already raised the class to elevated by the time the
#   override arm runs, `oc > class` is false (classify.go:328), the override never fires, and the
#   rung would be byte-identical to the routine rung while claiming to be a different one.
#
# WHAT IS ASSERTED, in order:
#   L2-0  THE LADDER IS COMPLETE OVER THE CLASSES THE API ADMITS. The four rung names are read
#         against the `spec.classification.class` enum of the ActionRecord CRD THE API SERVER IS
#         SERVING, not against a list in this file. A product that grew a fifth class fails here
#         rather than shipping a "per class" suite that silently covers four of five.
#   L2-1  EVERY RUNG REACHED THE PIPELINE. All four envelopes got an HTTP answer from the deployed
#         broker (a 403 refusal counts — it is an answer). A run that stops short has learned
#         nothing about the classifier, so it is reported as could-not-run, not as a red.
#   L2-2  FOUR RUNGS × TWO ARMS. Per rung: the CLASS the deployed broker wrote on the journaled
#         record, and the ATTRIBUTION it wrote next to it. Both, because the class alone is not
#         identity — rungs `routine` and `elevated` land on the SAME live class (see the blocker
#         above), and only the attribution distinguishes "the classifier read the production label"
#         from "the classifier ignored it and novelty did all the work". A classifier that answered
#         the right word for the wrong reason is precisely the substitution this row exists to
#         catch, and the `production-lever-ignored` mutant in the ¬ arm is that defect exactly.
#
# THE RUNGS, AND WHY EACH LEVER (the justification the row's "spot-check per class" needs)
#   All four are `patch` of a ConfigMap in ONE tenant namespace, with the IDENTICAL merge-patch body
#   — `undo_coverage_probe.patch_body` returns the same annotation patch for every kind that is not
#   a Deployment — so exactly one variable moves per rung: the object the patch is aimed at.
#
#   routine   LEVER: none. A bare ConfigMap carrying no environment label and no risk-class
#             annotation. CORPUS: `gat-001` (routine, `default-routine`) and `gat-146` (routine,
#             `notRules: [object-override]`). Live expectation `elevated` for the reason named in
#             the blocker; `production-environment` and `object-override` are asserted ABSENT, which
#             is what makes this rung a control for the two below rather than a duplicate of them.
#   elevated  LEVER: label `kube-agents/environment: production` on the target object. CORPUS:
#             `gat-110` (elevated, `production-environment`). Chosen over `gat-111`'s `env:
#             production` alias because `EnvironmentOf` (production.go:63) prefers the canonical key
#             and the alias only ever fires in its absence — testing the fallback would leave the
#             primary path unexercised. Chosen over `gat-141`'s `override: elevated` because that
#             lever is dead at Phase 9 (blocker, above).
#   gated     LEVER: annotation `kube-agents/risk-class: gated` on the target object. CORPUS:
#             `gat-140` (gated, `object-override`). This is the ONLY per-object, per-case stageable
#             route to `gated` available here: every other gated floor rule in `CodeFloor()` keys
#             off the KIND or the VERB (`destructive-stateful-delete` is delete-only,
#             `identity-change`/`public-exposure`/`traffic-shift-production` need other kinds,
#             `secret-material-egress` is prefiltered on `len(op.SecretMaterial) > 0`,
#             `cross-tier-direct-operation` needs a lower-tier owner) — so reaching gated any other
#             way would change the verb or the kind as well as the class, and two moving variables
#             is not a ladder.
#   forbidden LEVER: annotation `kube-agents/risk-class: forbidden` on the target object. CORPUS:
#             `gat-145` (forbidden, `object-override`). The only alternative is the `forbiddenSet`
#             (floor.go), whose every entry names a kube-agents kind — ActionRecord, ChangePolicy,
#             FleetFreeze, ApprovalRoster, Agent — and this actor's grant does not reach any of
#             them, so an attempt would be refused `target-forbidden` by RBAC BEFORE the classifier
#             ran and the rung would prove the opposite of what it claims.
#
# THE ANNOTATION KEY IS `kube-agents/risk-class`, AND THE SPEC SAYS OTHERWISE
#   03 §5.2 and V-GAT-012 spell the per-object override `kube-agents/change-policy`. The code
#   constant is `AnnotationRiskClass = "kube-agents/risk-class"` (classify/production.go:22), it is
#   the key `Resolve` reads (classify/resolve.go:81), and it is the key
#   `verification/fixtures/classifier-corpus.yaml` stages. This file uses THE CODE'S KEY, because a
#   suite whose subject is "the wired-in classifier is the one the corpus tests" must speak the
#   corpus's language or it is testing a third thing. THE DIVERGENCE IS REAL AND IS NOT CLOSED HERE:
#   V-GAT-012 is the row that owns it and it is not this file's.
#
# WHAT THIS DOES NOT CLAIM, and where each went instead
#   PER-CASE FIDELITY OVER THE CORPUS. Four rungs is a spot-check, which is the word the row uses.
#     The 168-case corpus is scored at L1 by `make -C k8s-operator test` (V-GAT-001, V-GAT-009), and
#     V-MET-005 asserts the corpus exercises every floor rule. This file is the L2 join between
#     them: it does not re-score the corpus, it checks that the deployed binary is the thing the
#     corpus was scoring.
#   V-GAT-012 (the per-object override, and its key). Not closed here — see above. The gated and
#     forbidden rungs USE the override as a lever, which is not the same as certifying it.
#   V-GAT-003 (escalation composition) and the blast-radius arms. One operation over one object is
#     the wrong instrument for either.
#   THE ROUTINE RUNG'S CORPUS-CLASS AGREEMENT. Deferred with the blocker named, to the phase that
#     grants write authority. Reported in the output as a deferral, not swallowed.
#   THAT THE PUBLISHED AGENT IMAGE CARRIES THIS TRANSPORT CODE. The driver pod mounts
#     `agents/platform/scripts/` from the working tree onto a stock `python:3.12-slim`, for
#     `broker-auth-l2.sh`'s reason: what is under test is the shipped source against the deployed
#     broker, and image parity is P1's job on a different row.
#
# ONE SHARED TENANT NAMESPACE, AND ONLY OBJECT-SCOPED LEVERS
#   The corpus also reaches elevated through NAMESPACE labels (`gat-112`, `gat-113`) and through
#   seen-history (`gat-130`, `gat-132`). Neither is stageable per-case inside one namespace: a
#   namespace label is shared by every rung in it, and history is a property of the caller. The
#   choice made here is ONE namespace and OBJECT-SCOPED LEVERS ONLY, rather than a namespace per
#   rung, for two reasons: `actor_overlay_apply_write` grants exactly one namespace and four grants
#   would put four widened identities on the cluster for the length of the run, and the seen-history
#   lever is unreachable at Phase 9 in any number of namespaces (blocker, above). A namespace-label
#   rung is the right shape once there is a second tenant namespace to spare; it would prove
#   `SourceNamespaceCanonical` and nothing this file's rungs prove.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent` in `kubeagents-system`, grants the actor identity WRITE authority over a
# throwaway tenant namespace, deletes and recreates four ConfigMaps in it, runs a pod, and submits
# four actions to a live broker — one of which is designed to be refused and journaled as a security
# event. On the live install that is a test deleting the fleet's own agent, widening a production
# identity, and filing a false security event.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1 or the run itself).
# Usage: dev/verify/classifier-identity-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`) — both, and in full. The whole row
#      is a claim about WHICH BINARY holds the classifier: a broker one generation behind the tree
#      would answer all eight rung arms about the previous build's `CodeFloor()` and read green,
#      which is the exact substitution V-GAT-002 exists to detect. The controller matters too, since
#      it renders the broker Deployment, its mesh Certificates and the pair NetworkPolicies.
#      Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with `--wait=true` and re-applied on every run,
#      so the broker Deployment is rendered by the controller running NOW, and the broker pod is
#      resolved through `p3_pod_of_deploy`, by ownership, so a pod from the previous generation of
#      the same Deployment can never be read as this one's. The four staged ConfigMaps are DELETED
#      and recreated every run rather than applied over: the routine rung's whole content is the
#      ABSENCE of a label and an annotation, and an apply over a survivor from an earlier ladder
#      would leave a lever standing that this run believes it removed. The ActionRecords are
#      deliberately NOT recreated — they are the output — and are disambiguated by the actionIds and
#      the trace id of THIS run.
#   P6 runtime-authoritative: every class and every reason is read from the `ActionRecord` object in
#      the API server, never from the broker's reply body, its log, or a golden. The four rung names
#      are checked against the enum of the CRD THE API SERVER IS SERVING, not against the Go
#      constants in `classify/class.go`, so a cluster serving a different class set is a FAIL and
#      not an agreement between two copies of one list. The driver's environment — endpoint, SAN,
#      identity, token path, TLS dir — comes off the RENDERED agent Deployment through
#      `broker_driver_env`.
#   P9 polled-not-slept: the record mine polls to a deadline. Four journal writes do not land at the
#      same instant, one of them travels the refusal path instead of the executor's, and a fixed
#      wait is how "the broker is slow" becomes "the classifier answered nothing".
set -uo pipefail

# MODES. `live` submits to a real broker and is what every claim above is about.
# `--negative-control` is the mandatory `¬` arm (V-MET-014): it replays the per-rung assertion block
# against the classifications a MISBEHAVING broker would have written, and requires each to go red
# by the arm that targets it.
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

# Its OWN tenant namespace. `broker-execute-l2.sh`'s and `undo-coverage-l2.sh`'s namespaces are full
# of objects whose labels and annotations those suites chose, and a ladder whose whole method is
# "one lever moves" cannot share a namespace with a suite that seeds thirty-seven objects into it.
# Created once and REUSED forever — never deleted, per [[LSN-045]]: the journal-retention policy
# denies DELETE of an ActionRecord until export confirms, so a namespace holding one never finishes
# terminating and a suite that tried would hang on its own evidence.
TENANT_NS=kubeagents-classifier-ladder-tenant

DRIVER_POD=classifier-identity-l2-driver
DRIVER_CM=classifier-identity-l2-code
UNTRUSTED_SECRET=classifier-identity-l2-untrusted

# The soak probe, reused verbatim rather than forked. It takes its population from
# `PROBE_CORPUS_B64`, builds one single-operation envelope per row with a fresh nonce, and reports
# where the broker says it put each answer — which is exactly this suite's transport need. It also
# carries the corpus `class` column through to the transcript as `expectClass`, and this file NEVER
# READS THAT FIELD: the rung expectations below are the ladder's, derived from 03 §5 and stated in
# the header, and reading the probe's copy would be the assertion its docstring forbids.
PROBE=dev/verify/fixtures/undo_coverage_probe.py

# The rung object names, in the tenant namespace. `<prefix><rung>`.
OBJ_PREFIX=ladder-

# The rungs, in ladder order. Also the corpus-row ids handed to the probe, so the transcript reads
# one line per class.
RUNG_ORDER="routine elevated gated forbidden"

# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) The control hands SYNTHESIZED (class,
# attribution) pairs straight to the per-rung assertion block, so everything upstream of it is
# unmeasured by the ¬ arm:
#   - the STAGING. It never applies a label or an annotation, and never reads one back. A staging
#     step that silently did nothing would make the routine and elevated rungs the same world, and
#     the ¬ arm would stay green while the live run went red for a harness reason
#   - the corpus TSV, its base64 transport, and the four HTTP submissions (L2-1). A synthesized pair
#     is not a broker's answer; the ¬ arm cannot tell a running broker from an absent one
#   - the RECORD MINE — the poll, the `ar-<lowercase>` name derivation, and the trace-id fallback
#     that locates the refused rung. `broker-execute-l2.sh` shipped a lookup that could not have
#     found a record against any commit, green in its ¬ arm throughout, for exactly this reason
#   - the served-CRD enum read (L2-0), the P1 digest arms, and the broker's Availability, all of
#     which run before either mode
# What it does prove, and all it proves: the per-rung block distinguishes a classifier that answered
# the right class for the right reason from one that did neither, or one and not the other.
fail=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END. `broker-auth-l2.sh` carries the full
# argument; the one-line version is that `fail` stays 0 when no assertion runs, so a suite that
# skipped its body would print a PROVEN banner. Change EXPECTED_ASSERTIONS deliberately, in the same
# commit as the arm.
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

# 2 x P1 + broker Available + L2-0 class-set + L2-1 all four answered + 4 rungs x (class,
# attribution).
EXPECTED_ASSERTIONS=13

# ------------------------------------------------------------------------------------------------
# The per-rung assertion block. A function because `--negative-control` replays exactly these arms
# against classifications nobody's broker wrote.
#
# ATTRIBUTION IS A FLAT STRING, and the needles are substrings of it. Each reason is rendered
# `rule=<rule>~class=<class>~detail=<detail>` and the reasons are joined with ` ;; `. The `~`
# terminator is what makes `rule=broker/forbidden~` not match `rule=broker/target-forbidden~`, which
# is the single most important discrimination in this file: EVERY refusal the broker journals is
# written with `Class: RiskForbidden` by `StoreRejectionJournal.Reject` (rejection.go:148), whatever
# it refused for. An arm that read only the class would score an RBAC denial, a replayed nonce and a
# malformed envelope as "the classifier said forbidden".
#
# THE FORBIDDEN RUNG'S `object-override` NEEDLE MATCHES INSIDE A DETAIL, NOT A RULE. On the refusal
# path the classifier's own reasons are collapsed by `reasonsDetail` (pipeline.go:1668) into ONE
# reason whose rule is `broker/forbidden` and whose detail is the joined `Reason.String()` list, so
# `object-override` appears as text rather than as a rule id. That is a fact about the refusal
# journal, it is disclosed here rather than worked around, and it is still a discrimination: a
# `target-forbidden` refusal's detail is an RBAC message and contains neither `object-override` nor
# `kube-agents/risk-class:`.
# ------------------------------------------------------------------------------------------------

# judge_rung <rung> <want-class> <require-csv> <forbid-csv> <got-class> <attribution>
#   Two arms, always both, always in this order. Needles carry no spaces, so the CSVs split on
#   word boundaries after a `tr`.
judge_rung() {
  local rung="$1" want="$2" req="$3" forbid="$4" got="$5" attrib="$6"
  local miss="" extra="" n

  if [ -z "$got" ]; then
    bad "V-GAT-002 $rung: the journaled record carries no spec.classification.class. The rung staged for '$want' produced a record the classifier never wrote a class onto."
  elif [ "$got" = "$want" ]; then
    pass "V-GAT-002 $rung: the deployed classifier answered '$got' — the class this rung's staged world calls for"
  else
    bad "V-GAT-002 $rung: LADDER BROKEN — the deployed classifier answered '$got' where the staged world requires '$want'. The binary this cluster is running is not the one the corpus scores."
  fi

  for n in $(printf '%s' "$req" | tr ',' ' '); do
    case "$attrib" in
      *"$n"*) ;;
      *) miss="$miss $n" ;;
    esac
  done
  for n in $(printf '%s' "$forbid" | tr ',' ' '); do
    case "$attrib" in
      *"$n"*) extra="$extra $n" ;;
    esac
  done

  if [ -n "$miss" ] || [ -n "$extra" ]; then
    bad "V-GAT-002 $rung: ATTRIBUTION WRONG — absent: [${miss# }]; present-but-forbidden: [${extra# }]. The class may be right and the REASON is not, which is a classifier agreeing by coincidence rather than the one the corpus tests. Reasons read: ${attrib:-<none>}"
  else
    pass "V-GAT-002 $rung: attribution names [$req]${forbid:+, and none of [$forbid]} — the class came from the lever this rung staged, not from somewhere else"
  fi
}

# ------------------------------------------------------------------------------------------------
# The `¬` arm
# ------------------------------------------------------------------------------------------------
# WHY A TRANSCRIPT AND NOT A MUTATION. Making a REAL deployed broker misclassify means editing
# `classify/floor.go` and rolling a new image, which is `dev/mutate.py`'s job at L1 and not
# something an L2 suite can stage against a running binary. What this arm proves is the thing an L2
# suite CAN get wrong on its own: that the per-rung block tells a correct classification from an
# incorrect one, AND that it tells a correct class with a wrong reason from a correct one — the
# `production-lever-ignored` row is the whole reason the attribution arm exists.
#
# EACH MUTANT MUST BE CAUGHT BY THE ARM THAT TARGETS IT ([[LSN-035]]). Every row carries a needle,
# and a row counts as caught only when a FAIL line CONTAINS that needle. Without that, breaking the
# needle split would "catch" every mutant at once by failing every arm on every one of them, and the
# control would read green while asserting that the suite is broken.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`. `judge_rung` runs inside a command
# substitution, which is a subshell, so every `fail=1` it sets dies with it.
run_negative_control() {
  local name expect needle rung want req forbid got attrib out n_fail rc=0 total=0 caught=0

  # name|expect|needle|rung|want|require|forbid|got-class|attribution
  while IFS='|' read -r name expect needle rung want req forbid got attrib; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    out="$(judge_rung "$rung" "$want" "$req" "$forbid" "$got" "$attrib" 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — the correct classification passes, so the arms are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT classification was failed $n_fail time(s); every mutant below would be caught for the wrong reason"
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
ok-routine|green|-|routine|elevated|rule=novel-action~|rule=production-environment~,rule=object-override~|elevated|rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
ok-elevated|green|-|elevated|elevated|rule=production-environment~,rule=novel-action~||elevated|rule=production-environment~class=+1~detail=the target is production, per its object label ;; rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
ok-gated|green|-|gated|gated|rule=object-override~,rule=novel-action~||gated|rule=object-override~class=gated~detail=the target carries kube-agents/risk-class: gated ;; rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
ok-forbidden|green|-|forbidden|forbidden|rule=broker/forbidden~,object-override,kube-agents/risk-class:||forbidden|rule=broker/forbidden~class=forbidden~detail=novel-action (+1): this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before; object-override (forbidden): the target carries kube-agents/risk-class: forbidden
override-ignored|red|gated: LADDER BROKEN — the deployed classifier answered 'elevated'|gated|gated|rule=object-override~,rule=novel-action~||elevated|rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
gates-everything|red|routine: LADDER BROKEN — the deployed classifier answered 'gated'|routine|elevated|rule=novel-action~|rule=production-environment~,rule=object-override~|gated|rule=blast-radius-cap~class=gated~detail=this action touches more objects than the scope allows
production-lever-ignored|red|elevated: ATTRIBUTION WRONG — absent: [rule=production-environment~|elevated|elevated|rule=production-environment~,rule=novel-action~||elevated|rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
override-unattributed|red|gated: ATTRIBUTION WRONG — absent: [rule=object-override~|gated|gated|rule=object-override~,rule=novel-action~||gated|rule=blast-radius-cap~class=gated~detail=this action touches more objects than the scope allows ;; rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before
routine-rung-secretly-overridden|red|routine: ATTRIBUTION WRONG — absent: []; present-but-forbidden: [rule=object-override~|routine|elevated|rule=novel-action~|rule=production-environment~,rule=object-override~|elevated|rule=novel-action~class=+1~detail=this agent has not done patch on a ConfigMap in kubeagents-classifier-ladder-tenant before ;; rule=object-override~class=elevated~detail=the target carries kube-agents/risk-class: elevated
forbidden-downgraded|red|forbidden: LADDER BROKEN — the deployed classifier answered 'gated'|forbidden|forbidden|rule=broker/forbidden~,object-override,kube-agents/risk-class:||gated|rule=object-override~class=gated~detail=the target carries kube-agents/risk-class: gated
forbidden-for-another-reason|red|forbidden: ATTRIBUTION WRONG — absent: [rule=broker/forbidden~ object-override|forbidden|forbidden|rule=broker/forbidden~,object-override,kube-agents/risk-class:||forbidden|rule=broker/target-forbidden~class=forbidden~detail=step 3: capturing pre-state for 1 targets: configmaps ladder-forbidden is forbidden: User system:serviceaccount:kubeagents-system:platform-actor cannot patch resource configmaps
no-class-at-all|red|routine: the journaled record carries no spec.classification.class|routine|elevated|rule=novel-action~|rule=production-environment~,rule=object-override~||
no-reasons-at-all|red|elevated: ATTRIBUTION WRONG — absent: [rule=production-environment~ rule=novel-action~]|elevated|elevated|rule=production-environment~,rule=novel-action~||elevated|
CASES

  echo
  echo "negative control: $caught/$total"
  return $rc
}

if [ "$MODE" = negative-control ]; then
  echo "== classifier-identity-l2.sh --negative-control: does the per-rung block tell the right class for the right reason from everything else? =="
  run_negative_control
  exit $?
fi

# ================================================================================================
# LIVE
# ================================================================================================
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSED: '$CTX' is not an anchored gke-scratch-* context." >&2
    echo "  This suite deletes and re-applies the Agent CR '$AGENT' in $NS, grants its actor identity" >&2
    echo "  WRITE authority over a namespace, recreates four ConfigMaps in it, and submits four" >&2
    echo "  actions to a live broker — one of them deliberately forbidden, which the broker journals" >&2
    echo "  as a security event. On the live install that is a test deleting the fleet's own agent," >&2
    echo "  widening a production identity, and filing a false security event against it." >&2
    exit 2
    ;;
esac

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  exit 1
}

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/actor-overlay.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/broker-driver.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

TMP="$(mktemp -d)" || exit 1
cleanup() {
  # The write grant first and unconditionally: a widened identity is the one thing here that is
  # dangerous if the run dies halfway, and it is cheap to revoke twice.
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  rm -rf "$TMP"
  echo
  echo "CLEANED UP: the write and read overlays are revoked, the driver pod and its ConfigMap are"
  echo "  gone, and the Agent CR is deleted. THE TENANT NAMESPACE, THE FOUR STAGED ConfigMaps AND"
  echo "  EVERY ActionRecord ARE LEFT STANDING — [[LSN-045]]: the journal-retention policy denies"
  echo "  DELETE of an ActionRecord until export confirms, so a namespace holding one never finishes"
  echo "  terminating and a suite that tried would hang on its own evidence. The staged objects are"
  echo "  also the world a human needs to see when a rung goes red."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# The population: four rows, hand-made, one per class
# ------------------------------------------------------------------------------------------------
# NOT `soak_corpus.py --table`. That deriver's whole job is to select the corpus cases this actor is
# authorized to attempt, and it excludes gated and forbidden BY DESIGN — asking it for a ladder that
# needs both would be asking it for the thing it exists to refuse. The columns are its
# `soak_corpus.COLUMNS`, in its order, because `undo_coverage_probe.parse_corpus` COMPARES the
# header rather than skipping it: a column added upstream fails here loudly instead of shifting
# every field one to the left.
#
# The `class` column carries the CORPUS class, which the probe echoes as `expectClass` and which
# this file never reads. `rbacVerbs` and `srcNs` are the deriver's own bookkeeping and are unread by
# the probe; they are filled rather than blanked so the row is a well-formed one of its kind.
TABLE="$(
  printf '#id\tclass\tverb\tgroup\tkind\tresource\tsubresource\trbacVerbs\ttarget\tseed\tsrcNs\n'
  for rung in $RUNG_ORDER; do
    printf '%s\t%s\tpatch\t\tConfigMap\tconfigmaps\t\tpatch\t%s%s\tpresent\tladder\n' \
      "$rung" "$rung" "$OBJ_PREFIX" "$rung"
  done
)"

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

# NO ENVIRONMENT LABEL ON THE NAMESPACE, EVER. `EnvironmentOf` falls through from the object to the
# namespace (production.go:63), so a namespace labelled `kube-agents/environment: production` would
# make the routine rung elevated-by-production and the elevated rung indistinguishable from it. The
# namespace manifest above carries no labels at all for that reason, and the staging check below
# reads the namespace back to prove nothing added one.

# P3: deleted with --wait=true before it is applied, so everything the controller renders from it is
# this generation's.
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

# Read AND write. The classifier's live read of the target's labels and annotations — the whole
# input to three of the four levers — needs the first, and a server-side dry-run patch needs the
# second, because the API server AUTHORIZES a dry run before it dry-runs it. Without the read half
# `Resolve` returns no labels and every lever silently disappears, which is the failure mode that
# would make this suite report a broken classifier when the grant is what broke.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS; the classifier would"
  echo "  read no live state and every rung would collapse onto the same answer."
  exit 3
}

# ------------------------------------------------------------------------------------------------
# Staging the four worlds
# ------------------------------------------------------------------------------------------------
# DELETED, then created. Not applied over: the routine rung's entire content is the ABSENCE of an
# environment label and a risk-class annotation, and an apply that inherited either from an earlier
# ladder — or from a human poking at the namespace — would leave a lever standing that this run
# believes it removed, and the routine and elevated rungs would agree for a reason nothing reports.
echo
echo "== staging four worlds in $TENANT_NS, one lever apart =="
for rung in $RUNG_ORDER; do
  $K -n "$TENANT_NS" delete configmap "$OBJ_PREFIX$rung" --ignore-not-found --wait=true >/dev/null 2>&1
done

{
  printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %sroutine\n  namespace: %s\n' "$OBJ_PREFIX" "$TENANT_NS"
  printf '  labels:\n    kube-agents/ladder-rung: routine\ndata:\n  rung: "routine"\n'
  printf -- '---\n'
  printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %selevated\n  namespace: %s\n' "$OBJ_PREFIX" "$TENANT_NS"
  printf '  labels:\n    kube-agents/ladder-rung: elevated\n    kube-agents/environment: production\ndata:\n  rung: "elevated"\n'
  printf -- '---\n'
  printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %sgated\n  namespace: %s\n' "$OBJ_PREFIX" "$TENANT_NS"
  printf '  labels:\n    kube-agents/ladder-rung: gated\n  annotations:\n    kube-agents/risk-class: gated\ndata:\n  rung: "gated"\n'
  printf -- '---\n'
  printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %sforbidden\n  namespace: %s\n' "$OBJ_PREFIX" "$TENANT_NS"
  printf '  labels:\n    kube-agents/ladder-rung: forbidden\n  annotations:\n    kube-agents/risk-class: forbidden\ndata:\n  rung: "forbidden"\n'
} >"$TMP/rungs.yaml"

$K apply -f "$TMP/rungs.yaml" >/dev/null || {
  echo "FAIL: could not stage the four rung objects in $TENANT_NS" >&2
  exit 1
}

# READ BACK. A staging that silently did not take would not fail this run honestly — it would make
# two rungs the same world and report the collapse as a classifier defect. This is a could-not-run
# gate, not an assertion: it is about the harness, and V-GAT-002 says nothing about ConfigMaps.
$K -n "$TENANT_NS" get configmaps -o json >"$TMP/staged.json" 2>/dev/null
$K get namespace "$TENANT_NS" -o json >"$TMP/staged-ns.json" 2>/dev/null
staging="$(python3 -c '
import json, sys

objs, nsdoc, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
ENV = "kube-agents/environment"
ALIAS = "env"
RISK = "kube-agents/risk-class"
# rung -> (env label value or None, risk annotation value or None)
want = {
    "routine": (None, None),
    "elevated": ("production", None),
    "gated": (None, "gated"),
    "forbidden": (None, "forbidden"),
}
bad = []
try:
    nslbl = (json.load(open(nsdoc)).get("metadata") or {}).get("labels") or {}
except Exception as exc:
    bad.append("could not read the tenant namespace: %s" % exc)
    nslbl = {}
for k in (ENV, ALIAS):
    if k in nslbl:
        bad.append(
            "the tenant NAMESPACE carries %s=%r; EnvironmentOf falls through to it and every rung "
            "would inherit one environment" % (k, nslbl[k])
        )
by_name = {}
try:
    for it in json.load(open(objs)).get("items", []):
        by_name[(it.get("metadata") or {}).get("name", "")] = it
except Exception as exc:
    bad.append("could not read the staged ConfigMaps: %s" % exc)
for rung, (env, risk) in want.items():
    name = prefix + rung
    it = by_name.get(name)
    if it is None:
        bad.append("%s was not created" % name)
        continue
    md = it.get("metadata") or {}
    lbl = md.get("labels") or {}
    ann = md.get("annotations") or {}
    got_env = lbl.get(ENV)
    got_alias = lbl.get(ALIAS)
    got_risk = ann.get(RISK)
    if got_env != env:
        bad.append("%s: %s is %r, the rung stages %r" % (name, ENV, got_env, env))
    if got_alias is not None:
        bad.append("%s: carries the alias label %s=%r, which no rung stages" % (name, ALIAS, got_alias))
    if got_risk != risk:
        bad.append("%s: %s is %r, the rung stages %r" % (name, RISK, got_risk, risk))
if bad:
    print("\n".join(bad))
' "$TMP/staged.json" "$TMP/staged-ns.json" "$OBJ_PREFIX")"
if [ -n "$staging" ]; then
  echo "DEFERRED: the staged world is not the world the ladder describes, so no rung below would be"
  echo "  a statement about the lever it names:"
  printf '%s\n' "$staging" | sed 's/^/    /'
  exit 3
fi
echo "  four ConfigMaps, one lever apart, read back from the API server and confirmed"

# ------------------------------------------------------------------------------------------------
# P1
# ------------------------------------------------------------------------------------------------
echo
echo "== P1: the controller and this agent's broker are the build under test =="
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the controller. It renders the broker, its mesh Certificates"
    echo "  and the pair NetworkPolicies; nothing below would be evidence about this commit."
    exit 3
    ;;
  *)
    bad "P1: the controller is not running the build under test"
    exit 1
    ;;
esac

broker_deploy="${AGENT}-broker"
broker_pod="$(p3_pod_of_deploy "$K" "$NS" "$broker_deploy" 180)"
if [ -z "$broker_pod" ]; then
  echo "DEFERRED: no pod is owned by deploy/$broker_deploy after 180s. There is no classifier to ask."
  $K -n "$NS" describe "deploy/$broker_deploy" 2>&1 | tail -20
  exit 3
fi
echo "  broker pod (by ownership, P3): $broker_pod"

p1_assert_build_under_test "$K" "$NS" "kube-agents/agent=$AGENT,kube-agents/role=actor"
case "$?" in
  0) pass "P1: the broker is running the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the broker. V-GAT-002 is a claim about WHICH BINARY holds"
    echo "  the classifier; an unidentifiable one makes every rung below a statement about an"
    echo "  unknown build, which is the substitution this row exists to detect."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

# Polled, not slept on (P9).
echo
echo "== waiting for the broker to become Available =="
avail=""
deadline=$((SECONDS + 240))
while [ "$SECONDS" -lt "$deadline" ]; do
  avail="$($K -n "$NS" get "deploy/$broker_deploy" \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)"
  [ "$avail" = "True" ] && break
  sleep 3
done
if [ "$avail" != "True" ]; then
  echo "DEFERRED: deploy/$broker_deploy never became Available (condition='${avail:-none}')."
  $K -n "$NS" describe "pod/$broker_pod" 2>&1 | tail -25
  exit 3
fi
pass "deploy/$broker_deploy is Available"

# ------------------------------------------------------------------------------------------------
# L2-0: the ladder is complete over the classes the SERVED CRD admits
# ------------------------------------------------------------------------------------------------
# From the API server, not from `classify/class.go` and not from a list in this file: either of
# those would be this suite agreeing with a copy of the same enum, and "a spot-check per class" is
# a claim about a SET. A product that grew a fifth class must fail here rather than ship a suite
# that quietly covers four of five.
echo
echo "== L2-0: the four rungs are exactly the classes the served ActionRecord CRD admits =="
SERVED_CLASSES="$($K get crd actionrecords.kubeagents.x-k8s.io \
  -o jsonpath='{.spec.versions[?(@.name=="v1alpha1")].schema.openAPIV3Schema.properties.spec.properties.classification.properties.class.enum[*]}' 2>/dev/null)"
if [ -z "$SERVED_CLASSES" ]; then
  bad "the served ActionRecord CRD publishes no enum for spec.classification.class, so 'one rung per class' cannot be checked against anything. The ladder's completeness would be this file's own word for it."
else
  echo "  served classes: $SERVED_CLASSES"
  ladder_sorted="$(printf '%s' "$RUNG_ORDER" | tr ' ' '\n' | sed '/^$/d' | sort | tr '\n' ' ')"
  served_sorted="$(printf '%s' "$SERVED_CLASSES" | tr ' ' '\n' | sed '/^$/d' | sort | tr '\n' ' ')"
  if [ "$ladder_sorted" = "$served_sorted" ]; then
    pass "V-GAT-002: the ladder walks [$RUNG_ORDER], which is exactly the class set the served CRD admits"
  else
    bad "V-GAT-002: the ladder walks [$ladder_sorted] and the served CRD admits [$served_sorted]. A spot-check 'per class' over the wrong set of classes is not the row's claim."
  fi
fi

# ------------------------------------------------------------------------------------------------
# Submit
# ------------------------------------------------------------------------------------------------
echo
echo "== submitting four envelopes from inside the cluster, one per rung =="

broker_driver_use_probe "$PROBE" || {
  echo "FAIL: $PROBE is not where this suite says it is" >&2
  exit 1
}
# shellcheck disable=SC2034
BROKER_DRIVER_TENANT_NS="$TENANT_NS"

# ONE TRACE ID FOR THE RUN, PINNED. `broker_client.session_trace()` takes `TRACE_ID` from the
# environment when it is 32 hex characters and generates a fresh one otherwise, and
# `rejection.traceFromBody` stamps whatever the envelope carried onto the record it writes for a
# REFUSAL. That matters because the forbidden rung's reply carries NO actionId — `server.refuse`
# renders the Refusal without one — so the trace is the only thread from this run to the record it
# produced. `SPAN_ID` is deliberately left unset: when it is set, `session_trace()` emits `spanId`,
# and that is a different suite's filed finding.
TRACE_ID="$(openssl rand -hex 16 | tr -d '\n')"
case "$TRACE_ID" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *)
    echo "DEFERRED: could not generate a 32-hex trace id (got '${TRACE_ID:-<empty>}'); the refused"
    echo "  rung's record would be unfindable and the ladder would be three rungs long."
    exit 3
    ;;
esac
echo "  trace id for this run: $TRACE_ID"

# The corpus and the trace, base64'd and plain: `broker_driver_run` renders extra env into an
# unquoted heredoc, refuses a quote, dollar, backtick or backslash, and reads the list line by line.
# Base64's alphabet and lowercase hex are both entirely inside what it permits.
BROKER_DRIVER_EXTRA_ENV="PROBE_CORPUS_B64=$(printf '%s\n' "$TABLE" | base64 | tr -d '\n')
TRACE_ID=$TRACE_ID"
# shellcheck disable=SC2034
export BROKER_DRIVER_EXTRA_ENV

broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the placeholder keypair the driver pod mounts" >&2
  exit 1
}

driver_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion, so no rung was ever submitted."
  echo "  An inability to run the experiment, not a property that failed (P10's distinction)."
  exit 3
fi
printf '%s\n' "$driver_out" >"$TMP/probe.jsonl"
printf '%s\n' "$driver_out" | sed 's/^/  | /'

# ------------------------------------------------------------------------------------------------
# L2-1: every rung reached the pipeline
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-1: all four rungs got an answer out of the deployed broker =="
unanswered="$(python3 -c '
import json, sys

probe, order = sys.argv[1], sys.argv[2]
seen = {}
for line in open(probe):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("scenario"):
        seen[r["scenario"]] = r
door = seen.get("nonce-accepted") or {}
if door.get("outcome") != "http":
    print("the door never opened: nonce-accepted is %r (%s)" % (door.get("outcome"), door.get("detail", "")[:200]))
for rung in order.split():
    r = seen.get(rung)
    if r is None:
        print("%s: the probe emitted no line at all" % rung)
    elif r.get("outcome") != "http":
        print("%s: outcome %r, %s" % (rung, r.get("outcome"), str(r.get("detail", ""))[:200]))
' "$TMP/probe.jsonl" "$RUNG_ORDER")"
if [ -n "$unanswered" ]; then
  echo "DEFERRED: at least one rung never reached the classifier, so the ladder is incomplete and"
  echo "  the rungs that did run would be a spot-check over fewer classes than the row names:"
  printf '%s\n' "$unanswered" | sed 's/^/    /'
  echo
  echo "  Diagnose here first: a 400 'invalid-envelope' means the shipped builder and the broker's"
  echo "  schema disagree; a 401 is broker-auth-l2.sh's problem, not this suite's; a transport error"
  echo "  is the mesh."
  $K -n "$NS" logs "pod/$broker_pod" --tail=40 2>/dev/null | sed 's/^/  broker| /'
  exit 3
fi
pass "V-GAT-002: all four rungs reached the deployed broker and were answered — three accepted, one refused, every one of them classified"

# ------------------------------------------------------------------------------------------------
# The mine
# ------------------------------------------------------------------------------------------------
# WHERE EACH RUNG'S RECORD IS. Two locators, because the pipeline has two journal paths:
#   accepted  the reply carries an actionId, and the object name is `"ar-" + strings.ToLower(id)`
#             (06 §4.3, journal/ulid.go). Derived the way the broker derives it rather than read off
#             the reply, so the two are joined by the rule and not by a returned string.
#   refused   the reply carries NO actionId (`server.refuse` renders the Refusal without one), so
#             the record is the one this run's TRACE wrote that no reply named. Not located by
#             `status.phase == Rejected`: `StoreRejectionJournal.Reject` sets the phase on the
#             object it creates but the phase lives on the STATUS subresource, so it is the
#             reconciler that persists it — a survey of this cluster found refusal records sitting
#             with an empty phase, and a locator keyed on it would silently skip them.
# The fallback is keyed on the reply, not on the rung, so a forbidden rung that was WRONGLY accepted
# is still found — by its actionId — and goes red on the class arm rather than vanishing into a
# could-not-run.
echo
echo "== mining $NS for the four records this run produced =="
RUNG_ROWS=""
deadline=$((SECONDS + 90))
while [ "$SECONDS" -lt "$deadline" ]; do
  $K -n "$NS" get actionrecords -o json >"$TMP/records.json" 2>/dev/null
  RUNG_ROWS="$(python3 -c '
import json, sys

probe, records, trace, order = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

replies = {}
for line in open(probe):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("scenario"):
        replies[r["scenario"]] = r

try:
    items = json.load(open(records)).get("items", [])
except Exception:
    items = []
by_name = {(i.get("metadata") or {}).get("name", ""): i for i in items}
mine = [i for i in items if ((i.get("spec") or {}).get("trace") or {}).get("traceId", "") == trace]

rungs = order.split()
named = set()
for rung in rungs:
    aid = (replies.get(rung) or {}).get("actionId") or ""
    if aid:
        named.add("ar-" + aid.lower())
orphans = [i for i in mine if (i.get("metadata") or {}).get("name", "") not in named]


def attribution(rec):
    cl = ((rec.get("spec") or {}).get("classification") or {})
    parts = []
    for r in cl.get("reasons") or []:
        detail = " ".join(str(r.get("detail", "")).split())
        parts.append("rule=%s~class=%s~detail=%s" % (r.get("rule", ""), r.get("class", ""), detail))
    return str(cl.get("class") or ""), " ;; ".join(parts)


for rung in rungs:
    aid = (replies.get(rung) or {}).get("actionId") or ""
    rec = None
    if aid:
        loc = "actionId " + aid
        rec = by_name.get("ar-" + aid.lower())
        if rec is None:
            loc = "MISSING: the reply named actionId %s and no record ar-%s exists" % (aid, aid.lower())
    elif len(orphans) == 1:
        rec = orphans[0]
        loc = "trace %s, the one record this run wrote that no reply named" % trace
    elif len(orphans) > 1:
        loc = "MISSING: %d records carry trace %s and no reply named any of them, so the refused rung is ambiguous" % (len(orphans), trace)
    else:
        loc = "MISSING: the reply carried no actionId and no record carries trace %s" % trace
    if rec is None:
        print("\t".join([rung, "", "", loc]))
        continue
    cls, attrib = attribution(rec)
    print("\t".join([rung, cls, attrib, (rec.get("metadata") or {}).get("name", "") + " via " + loc]))
' "$TMP/probe.jsonl" "$TMP/records.json" "$TRACE_ID" "$RUNG_ORDER")"
  printf '%s\n' "$RUNG_ROWS" | grep -q 'MISSING:' || break
  sleep 3
done

resolved="$(printf '%s\n' "$RUNG_ROWS" | grep -cv 'MISSING:')"
if [ "$resolved" -eq 0 ]; then
  echo "DEFERRED: not one of the four rungs produced a readable ActionRecord in 90s. Nothing was"
  echo "  journaled, so there is no classifier answer to judge — a run that did not happen, not a"
  echo "  property that failed."
  printf '%s\n' "$RUNG_ROWS" | sed 's/^/    /'
  exit 3
fi
echo "  $resolved of 4 rungs located"

# ------------------------------------------------------------------------------------------------
# L2-2: four rungs, two arms each
# ------------------------------------------------------------------------------------------------
# A rung whose record is MISSING is judged, not skipped: `judge_rung` receives an empty class and an
# empty attribution and goes red on both arms. A PARTIAL landing is a real disagreement — three
# rungs journaled and one not is a statement about the pipeline — and only the all-four-missing case
# above is a could-not-run.
echo
echo "== L2-2: the deployed classifier, one rung per class =="
while IFS='|' read -r rung want req forbid; do
  [ -n "$rung" ] || continue
  row="$(printf '%s\n' "$RUNG_ROWS" | awk -F'\t' -v s="$rung" '$1 == s { print; exit }')"
  got="$(printf '%s\n' "$row" | awk -F'\t' '{ print $2 }')"
  attrib="$(printf '%s\n' "$row" | awk -F'\t' '{ print $3 }')"
  where="$(printf '%s\n' "$row" | awk -F'\t' '{ print $4 }')"
  echo
  echo "-- rung '$rung' (want $want) — $where"
  judge_rung "$rung" "$want" "$req" "$forbid" "$got" "$attrib"
done <<'RUNGS'
routine|elevated|rule=novel-action~|rule=production-environment~,rule=object-override~
elevated|elevated|rule=production-environment~,rule=novel-action~|
gated|gated|rule=object-override~,rule=novel-action~|
forbidden|forbidden|rule=broker/forbidden~,object-override,kube-agents/risk-class:|
RUNGS

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-GAT-002 at L2 — a spot-check per class against the DEPLOYED classifier"
  echo " Four worlds one lever apart, four envelopes, and the binary this cluster is running chose"
  echo " the class the corpus stages for each of them AND named the corpus's rule as the reason."
  echo
  echo " DEFERRED WITHIN THIS PASS, and not swallowed: the routine rung's agreement with the CORPUS"
  echo " class 'routine'. It is asserted at 'elevated' because Phase 9 runs with no write authority"
  echo " (07 §2), so no ActionRecord ever reaches PhaseVerified, history.derive() keeps none, and"
  echo " novel-action (+1) fires on 100% of traffic. Every rung is displaced upward by exactly that"
  echo " one step, in the safe direction, and the routine base is asserted here as 'elevated with"
  echo " novel-action as its ONLY escalation'. The corpus-class agreement needs the phase that"
  echo " grants write authority; it is not obtainable by any staging a harness can do."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
