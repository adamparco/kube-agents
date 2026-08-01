#!/usr/bin/env bash
# vap-corpus-l2.sh — the actor VAP corpus, ONE DOCUMENT AT A TIME (V-CTN-012, at L2).
#
# THE READING THIS SUITE IS SCORED UNDER, stated before anything else, because the row it claims
# names an object that does not exist yet.
#   09 §6's V-CTN-012 reads: "Attenuation: a `Role`/`ClusterRole` exceeding its tier template is
#   denied by `vap-agent-scope`", spec `03 §4.2`, levels `L0, L2`, phase 8, BLOCKING-ALWAYS.
#   `vap-agent-scope` is not on any cluster today; it arrives at P10-T1. Reading the row as
#   unmeasurable until then is the wrong reading, and 03 §4.2 says why in its own words:
#
#     "This is the same policy object as the read-only generation's `vap-agent-readonly`,
#      **inverted**: reader SAs keep the read-verb allow-list; actor SAs get a scope-and-template
#      allow-list instead of a blanket write denial."
#
#   The object is one object across both generations. In the read-only generation it is deployed as
#   `kube-agents-agent-readonly`, and the attenuation clause — the one V-CTN-012 is about — is that
#   policy's **validation #3**, whose own `message:` cites `03 §4.2` verbatim and bounds actor RBAC
#   "by its tier's 06 §2.2 template". Measuring validation #3 document-by-document against a live
#   API server is therefore not a proxy for V-CTN-012; it is V-CTN-012's property, enforced by the
#   generation of the policy that is currently deployed. Six of the seven negative documents below
#   name validation #3; the seventh names #1.
#
#   WHAT THAT READING DOES NOT BUY. It does not close the row at L0, and it does not carry over
#   unexamined at P10-T1. When `vap-agent-scope` is emitted, this suite must be re-pointed at it and
#   the corpus regenerated from the inlined tier template, because "exceeds its tier template" will
#   then be decidable against a literal allow-list that does not exist in today's generation. Until
#   then the L2 half is green here and the L0 half is an open finding, recorded as one in
#   verification/results.csv rather than as a deferral — V-CTN-012 is BLOCKING-ALWAYS and 09 §9.6
#   forbids deferring it at all.
#
# OTHER ROWS THIS SUITE CORROBORATES BUT DOES NOT CLAIM. Every arm below is also evidence for
# V-CTN-004 (readers hold no write verb — validation #1, one negative document) and for V-BRK-013's
# admission half. Both are already green under their own implementations
# (`dev/tests/reader-holds-only-read-verbs.py` and `dev/verify/actor-grant-sweep-l2.sh`), and
# `verification/implementations.yaml` is one entry per check ID, so neither is claimed here. A
# second suite agreeing is worth having; a second suite claiming the row would hide which one broke.
#
# WHAT THIS IS FOR
#   `examples/gitops-repo/policy/tests/` holds two hand-built corpora against
#   `kube-agents-agent-readonly`: `vap_actor_positive.yaml` (5 documents, every one EXPECT ADMITTED)
#   and `vap_actor_negatives.yaml` (7 documents, every one EXPECT DENIED). Both headers name a
#   consumer, `tests/e2e/vap_negative_test.sh`, WHICH DOES NOT EXIST. What does touch them is
#   `gitops-tree-applies-l2.sh` (V-CMP-003), which sweeps every YAML under `examples/gitops-repo/`
#   and asks the coarsest question available: one `kubectl apply --dry-run=server` over the WHOLE
#   multi-document file.
#
#   For the negatives that is ONE verdict for SEVEN documents, and it is satisfied by ONE of them.
#   The arm's predicate is that the file was refused and that the refusal text names the declared
#   `# kube-agents/expect-denied-by:` policy, so six of the seven could be ADMITTED — the wildcard
#   escape, the vacuous `nonResourceURLs` rule, the empty `apiGroups` list, the unlabelled writer —
#   and the line still reports the fixture refused, as it declares it must be.
#
#   For the positives it is a verdict about the wrong property. The file is swept as an ordinary
#   shipped manifest and required to apply, which is V-CMP-003's question: can the tree land. There
#   is no P2 gate anywhere in that suite, so "all five applied" is the answer a cluster with NO
#   POLICY INSTALLED gives too (LSN-006), and a refusal there is reported as the gitops tree being
#   un-appliable rather than as the actor carve-out having narrowed. The corpus's own stated job —
#   to fail when the policy is too STRICT — is not a claim anything in the tree makes.
#
#   A corpus that is real, correct, and executed by nothing that asks its question is the finding
#   V-MET-001 was built after (see `verification/implementations.yaml`'s header). This suite is the
#   other half: split each file on its own `^---$` separators and submit each document ALONE, so
#   that every document gets its own verdict and no document's verdict can stand in for another's.
#
# WHAT IS ASSERTED, in order:
#   L2-0  P2 — THE POLICY IS LIVE, established by experiment rather than by `kubectl get`. Five of
#         the twelve arms below expect an ADMIT, and an ADMIT reads identically against a cluster
#         with no policy installed, a binding that has not activated, and a binding whose
#         validationActions omit Deny. That is LSN-006 aimed at admission instead of at the
#         dataplane, and it is why this whole file sits behind `p2_assert_policy_live`. The probe is
#         a WRONG-SCOPE ClusterRole — validation 2, the one validation no document in either corpus
#         is about — so establishing liveness cannot pre-establish any arm below.
#   L2-1  THE NEEDLES ARE THE DEPLOYED POLICY'S, AND THEY DISCRIMINATE. Each negative document
#         names the validation that must refuse it (`EXPECT: DENIED by validation #N`), and this
#         suite turns N into a substring the API server's answer must carry. Two properties: the
#         substring appears in THAT validation's message on the DEPLOYED policy, and it appears in
#         no other validation's message. Without the second, a document refused by the wrong rule
#         reads as a document refused by the right one — and a rejection for a reason that is about
#         to be removed is a rejection that expires silently.
#   L2-2  THE CORPUS IS THE CORPUS. 5 positive documents and 7 negative ones, pinned; every document
#         carries an `EXPECT:` marker; the negatives' expectations tally 6 against validation 3 and
#         1 against validation 1. A document deleted from a corpus is a check deleted from the
#         suite, and it leaves no other trace — the per-document arms below would simply run one
#         fewer time and still report all-green.
#   L2-3  EVERY NEGATIVE DOCUMENT, ALONE, IS REFUSED — by `kube-agents-agent-readonly` (named in the
#         answer, because four other policies are bound on this cluster), carrying the needle of the
#         validation it names, and carrying NO other validation's needle. A denial that cannot be
#         attributed to one rule is not evidence about that rule.
#   L2-4  EVERY POSITIVE DOCUMENT, ALONE, IS ADMITTED — and is SELECTED by the policy while being
#         admitted. The match condition fires on `kube-agents/tier` or `kube-agents/role`, so a
#         positive document that lost its labels would be admitted by not being looked at, which is
#         a pass produced by absence. The label is asserted from the document text before the
#         document is submitted.
#
# WHY THERE IS NO SYNTHESISED CONTROL MODE. The two-sidedness is in the INPUT, which is the stronger
# place for it. The negatives are the control against a policy written too wide; the positive file
# is the control against a policy written too strict (its own header: "the cheapest way to get every
# negative fixture passing is a policy that admits everything"). Both are submitted to a real API
# server here, so neither direction is synthesised and neither bypasses the apply, the parse or the
# message read — the three statements LSN-060 is about.
#
# WHAT THIS DOES NOT CLAIM
#   The authorizer's answer. Admission decides what may be WRITTEN into an RBAC object; it says
#   nothing about what a token may then DO. `actor-grant-sweep-l2.sh` is the suite that asks the
#   authorizer, over ~650 derived questions, and it is V-BRK-013's other L2 arm — not a substitute
#   for this one and not substituted by it. An object admission never sees (no `kube-agents/*`
#   label) is invisible here and visible there; a rule that was written correctly and never applied
#   is the reverse.
#   The per-tier actor templates of 06 §2.2, and therefore V-CTR-004. Those arrive with
#   `vap-agent-scope` at P10-T1. When they do, this runner is the shape their corpus wants.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. Every document in both corpora is
# submitted with `--dry-run=server` and nothing from either corpus is ever persisted — but the
# negatives are twelve deliberately-bad RBAC objects aimed at the fleet's own actor selector, and a
# dry run is one dropped flag away from an apply. The live install is not a target for that risk.
# Exit: 0 = every document got the verdict it declares · 1 = one did not · 2 = refused target,
# unreachable, or P10 · 3 = DEFERRED (P2: the policy is not live, so no ADMIT below means anything).
# Usage: dev/verify/vap-corpus-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — nothing under test here runs from an image this repository builds.
#      The subject is the API server's admission chain plus two YAML corpora in the working tree, and
#      the policy those corpora are judged against is read off the cluster. Pinning an operator
#      digest this suite never inspects would read as coverage while establishing nothing. Named
#      expiry: when P10-T1's `vap-agent-scope` is emitted by a build step rather than hand-written,
#      the generator becomes a first-party artifact and its successor check needs P1 in full.
#   P3 admission-recreate: none — every object judged here is submitted with `--dry-run=server`,
#      which runs the full admission chain against an object that has never existed and persists
#      nothing, so "grandfathered" is unrepresentable rather than merely unlikely. The single real
#      write in the file is an idempotent Namespace apply for the namespaces the corpus's namespaced
#      documents name, and no verdict is read from a Namespace.
#   P6 runtime-authoritative: the policy under test is read from the CLUSTER
#      (`kubectl get validatingadmissionpolicy kube-agents-agent-readonly`) and every verdict is the
#      API server's own answer to a submitted object. The tree's copy of the policy is used for
#      exactly one thing — counting the validations the deployed object must carry, so a corpus that
#      indexes validations BY NUMBER cannot be judged against some other generation of the policy
#      whose numbering means something else.
#   P10 control-plane-healthy: asserted below, before any verdict. An API server that is not
#      admitting refuses everything, which would turn all 7 negative arms green and all 5 positive
#      arms red — a shape that reads like a policy working perfectly and half a corpus rotting
#      (LSN-026). rc 2 is could-not-run, never a failed property.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

K="kubectl --context $CTX"

fail=0
checks=0
pass() { checks=$((checks + 1)); echo "PASS: $1"; }
bad()  { checks=$((checks + 1)); echo "FAIL: $1"; fail=1; }
note() { echo "  NOTE: $1"; }
cd "$REPO_ROOT" || { echo "REFUSING: cannot cd to $REPO_ROOT" >&2; exit 2; }

POLICY=kube-agents-agent-readonly
VAP_FILE=examples/gitops-repo/policy/vap-agent-readonly.yaml
POSITIVE=examples/gitops-repo/policy/tests/vap_actor_positive.yaml
NEGATIVES=examples/gitops-repo/policy/tests/vap_actor_negatives.yaml
# Pinned counts. These are the whole point of L2-2: a corpus that silently loses a document loses a
# check with it, and every other arm in this file keeps passing. Change them only in the same commit
# that changes the corpus, and say in that commit which property left.
POSITIVE_DOCS=5
NEGATIVE_DOCS=7

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "===================================================================="
echo " V-CTN-012 (L2) — attenuation, judged one corpus document at a time"
echo " against validation #3 of the deployed kube-agents-agent-readonly"
echo " (03 §4.2: the same object as vap-agent-scope, inverted)"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

. "$REPO_ROOT/dev/lib/preconditions.sh"

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

for f in "$POSITIVE" "$NEGATIVES" "$VAP_FILE"; do
  [ -f "$f" ] || { echo "FAIL: $f is missing — this suite has no subject." >&2; exit 1; }
done

# --- L2-0 · P2 — the policy is live ---------------------------------------------------------------
#
# Validation 2's subject, and deliberately nothing else's: no document in either corpus is about
# wrong-scope, so a policy proved live by this probe has had none of the twelve arms below decided
# for it in advance.
echo
echo "== L2-0. P2 — the policy is LIVE, proved by making it reject something =="
cat >"$WORK/p2-probe.yaml" <<'YAML'
# A wrong-scope ClusterRole: the namespace tier may not hold one (validation 2). Adversarial input,
# never applied for real — `--dry-run=server` runs the full admission chain and writes nothing.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubeagents-vap-corpus-liveness-probe
  labels:
    kube-agents/tier: developer-team
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get"]
YAML
p2_assert_policy_live "$K" "$POLICY" "$WORK/p2-probe.yaml" || exit 3
pass "L2-0 the admission chain is enforcing $POLICY — every ADMIT below is a measurement"

# --- L2-1 · the needles belong to the deployed policy, and they discriminate ----------------------
#
# The corpus refers to validations BY NUMBER, which is positional and only means anything against
# the generation of the policy the corpus was written for. So: the deployed validation count must
# equal the tree's, and each number must map to a substring that is present in that validation's
# deployed message and absent from every other one.
echo
echo "== L2-1. the denial needles are the DEPLOYED policy's, and each names one validation =="

needle_for_validation() {
  case "$1" in
    1) printf '%s' "agent RBAC may grant" ;;    # read-verb allow-list
    2) printf '%s' "wrong-scope" ;;             # the namespace tier holds no ClusterRole
    3) printf '%s' "broker-operations grant" ;; # the compiled 06 §2.2.1 actor allow-list
    *) return 1 ;;
  esac
}

tree_n="$(grep -c '^ *message: ".*"$' "$VAP_FILE")"
# No `mapfile`: macOS ships bash 3.2 and the harness runs on it. Read BY INDEX rather than by
# splitting a newline-joined list, too — this suite's whole subject is validation NUMBERS, and a
# validation carrying an empty message would drop a line and silently renumber every one after it.
deployed_n="$($K get validatingadmissionpolicy "$POLICY" \
  -o jsonpath='{range .spec.validations[*]}x{end}' 2>/dev/null | tr -cd 'x' | wc -c | tr -d ' ')"
deployed_msgs=()
i=0
while [ "$i" -lt "$deployed_n" ]; do
  deployed_msgs[i]="$($K get validatingadmissionpolicy "$POLICY" \
    -o jsonpath="{.spec.validations[$i].message}" 2>/dev/null)"
  i=$((i + 1))
done

if [ "$tree_n" -eq 0 ]; then
  bad "L2-1 read ZERO validations out of $VAP_FILE — this script's parse is broken, not the cluster"
elif [ "$deployed_n" -ne "$tree_n" ]; then
  bad "L2-1 the deployed $POLICY has $deployed_n validations; $VAP_FILE declares $tree_n."
  bad "  The corpus indexes validations by number, so those numbers now point somewhere else."
  bad "  Fix the environment, not this check: kubectl --context $CTX apply -f $VAP_FILE"
else
  needles_ok=1
  for n in 1 2 3; do
    needle="$(needle_for_validation "$n")"
    idx=$((n - 1))
    if [ "$idx" -ge "$deployed_n" ]; then
      bad "L2-1 the deployed policy has no validation #$n, which the corpus refers to"
      needles_ok=0
      continue
    fi
    case "${deployed_msgs[$idx]}" in
      *"$needle"*) ;;
      *) bad "L2-1 validation #$n's deployed message does not carry '$needle'."
         bad "  got: ${deployed_msgs[$idx]:0:110}…"
         needles_ok=0 ;;
    esac
    for other in $(seq 0 $((deployed_n - 1))); do
      [ "$other" -eq "$idx" ] && continue
      case "${deployed_msgs[$other]}" in
        *"$needle"*)
          bad "L2-1 '$needle' also appears in validation #$((other + 1))'s message, so it does not"
          bad "  discriminate: a denial by the wrong rule would be read as a denial by validation #$n"
          needles_ok=0 ;;
      esac
    done
  done
  [ "$needles_ok" -eq 1 ] &&
    pass "L2-1 all $tree_n deployed validations, and each corpus needle names exactly one of them"
fi

# --- L2-2 · the corpus is the corpus --------------------------------------------------------------
echo
echo "== L2-2. the corpora split into the documents they are pinned at =="

# Split on the file's OWN separators. Everything before the first `^---$` is the file header and is
# not a document; each `# DOC n — … EXPECT: …` comment sits AFTER its separator, so it travels with
# the document it describes and every split file carries its own declared expectation.
split_docs() {
  awk -v p="$2" '/^---$/ { n++; next } n > 0 { print > (p "-" n ".yaml") }' "$1"
}

count_docs() {
  local prefix="$1" n=0
  while [ -f "$prefix-$((n + 1)).yaml" ]; do n=$((n + 1)); done
  printf '%s' "$n"
}

split_docs "$POSITIVE" "$WORK/pos"
split_docs "$NEGATIVES" "$WORK/neg"
pos_n="$(count_docs "$WORK/pos")"
neg_n="$(count_docs "$WORK/neg")"

if [ "$pos_n" -ne "$POSITIVE_DOCS" ] || [ "$neg_n" -ne "$NEGATIVE_DOCS" ]; then
  bad "L2-2 the corpus is not the pinned size: $POSITIVE has $pos_n documents (pinned"
  bad "  $POSITIVE_DOCS), $NEGATIVES has $neg_n (pinned $NEGATIVE_DOCS). A document that leaves a"
  bad "  corpus takes a check with it and leaves no other trace — every arm below still passes."
else
  pass "L2-2a $pos_n positive documents and $neg_n negative documents, as pinned"
fi

# Each document's declared expectation, read out of the document itself.
# `want_of <file>` -> "admit" | "deny:<validation-number>" | "" (undeclared).
want_of() {
  if grep -q 'EXPECT: ADMITTED' "$1"; then
    printf 'admit'
  elif grep -q 'EXPECT: DENIED by structural schema validation' "$1"; then
    printf 'schema'
  elif grep -q 'EXPECT: DENIED' "$1"; then
    printf 'deny:%s' "$(sed -n 's/.*EXPECT: DENIED by validation #\([0-9][0-9]*\).*/\1/p' "$1" | head -1)"
  fi
}

undeclared=0
by_v1=0
by_v3=0
by_schema=0
for i in $(seq 1 "$pos_n"); do
  [ "$(want_of "$WORK/pos-$i.yaml")" = "admit" ] || {
    bad "L2-2 positive document $i does not declare EXPECT: ADMITTED"
    undeclared=1
  }
done
for i in $(seq 1 "$neg_n"); do
  case "$(want_of "$WORK/neg-$i.yaml")" in
    deny:1) by_v1=$((by_v1 + 1)) ;;
    deny:3) by_v3=$((by_v3 + 1)) ;;
    schema) by_schema=$((by_schema + 1)) ;;
    deny:*) bad "L2-2 negative document $i names a validation this suite has no needle for"
            undeclared=1 ;;
    *) bad "L2-2 negative document $i does not declare EXPECT: DENIED by validation #N"
       undeclared=1 ;;
  esac
done
if [ "$undeclared" -eq 0 ]; then
  if [ "$by_v3" -eq 5 ] && [ "$by_v1" -eq 1 ] && [ "$by_schema" -eq 1 ]; then
    pass "L2-2b every document declares its own verdict; negatives tally 5 by validation 3, 1 by 1,"
    pass "  1 refused a layer earlier by the API server's own structural schema"
  else
    bad "L2-2b the negatives tally $by_v3 against validation 3, $by_v1 against validation 1 and"
    bad "  $by_schema against the structural schema; the pinned mapping is 5, 1 and 1."
    bad "  The corpus changed shape."
  fi
fi

# A server dry-run of a NAMESPACED object still needs its namespace to exist, and a missing one is
# `namespaces "team-x" not found` — a red on the positive side and a denial for the wrong reason on
# the negative side. Applied rather than created: `create` first leaves the object without the
# last-applied annotation that a later `apply` then warns about.
sed -n 's/^  namespace: *//p' "$WORK"/pos-*.yaml "$WORK"/neg-*.yaml | sort -u |
  while IFS= read -r ns; do
    [ -n "$ns" ] || continue
    printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$ns" |
      $K apply -f - >/dev/null 2>&1 ||
      note "could not ensure namespace '$ns' exists; a document in it may fail for that reason"
  done

# --- the per-document submission ------------------------------------------------------------------
#
# submit_doc <file> <label> <admit|deny:N>
#   One `--dry-run=server` apply of ONE document. For a denial the TEXT is judged, not merely the
#   non-zero exit: a schema error, a missing namespace, an RBAC refusal and a quota all exit
#   non-zero too, and reading any of them as "the policy denied it" keeps the arm green on the day
#   the policy stops selecting the object. Four other ValidatingAdmissionPolicyBindings are live on
#   this cluster, so the answer must also NAME this policy, and must not carry a second validation's
#   needle — a denial that cannot be attributed to one rule is not evidence about that rule.
submit_doc() {
  local file="$1" label="$2" want="$3" out rc needle n other
  out="$($K apply --dry-run=server -f "$file" 2>&1)"; rc=$?

  case "$want" in
    admit)
      # Admitted-because-unselected is not admitted. The match condition fires on either label; a
      # document that lost both would be admitted by never being looked at.
      if ! grep -qE '^ +kube-agents/(tier|role): ' "$file"; then
        bad "$label carries neither kube-agents/tier nor kube-agents/role, so $POLICY does not"
        bad "  select it and an ADMIT here would measure nothing"
        return 1
      fi
      if [ "$rc" -ne 0 ]; then
        bad "$label was REFUSED; the corpus declares it ADMITTED. Answer: $out"
        return 1
      fi
      pass "$label — admitted, and selected by $POLICY while being admitted"
      ;;
    deny:*)
      n="${want#deny:}"
      needle="$(needle_for_validation "$n")"
      if [ "$rc" -eq 0 ]; then
        bad "$label was ADMITTED; the corpus declares it DENIED by validation #$n"
        return 1
      fi
      if ! printf '%s' "$out" | grep -qF -- "$POLICY"; then
        bad "$label was refused, but not by $POLICY — a schema, RBAC or namespace error is not"
        bad "  evidence about this policy. Answer: $out"
        return 1
      fi
      if ! printf '%s' "$out" | grep -qF -- "$needle"; then
        bad "$label was refused by $POLICY, but not by the validation the document names."
        bad "  wanted validation #$n's message to carry: $needle"
        bad "  got: $out"
        return 1
      fi
      for other in 1 2 3; do
        [ "$other" = "$n" ] && continue
        if printf '%s' "$out" | grep -qF -- "$(needle_for_validation "$other")"; then
          bad "$label was refused by validation #$n AND by validation #$other. The document is a"
          bad "  fixture for one rule; a denial two rules deep survives the removal of either."
          return 1
        fi
      done
      pass "$label — denied by validation #$n, and by that validation alone"
      ;;
    schema)
      # THE THIRD REFUSAL LAYER. A `Role`/`ClusterRole` rule with an empty `apiGroups`, `resources`
      # or `verbs` list is refused by the API server's own STRUCTURAL SCHEMA, before any admission
      # plugin runs. The object is contained — more cheaply and more unconditionally than by policy
      # — but it is not evidence about the policy, and a document that declares a validation it can
      # never reach is a fixture asserting nothing while reading green.
      #
      # This arm is not the weaker one. It pins THREE things the `deny:` arm cannot: that the object
      # is still refused, that the refusal is the schema's and not the policy's, and — the reason it
      # exists — that the day the schema stops refusing it, this goes RED rather than quietly
      # becoming a real admission test whose expectation nobody re-derived.
      if [ "$rc" -eq 0 ]; then
        bad "$label was ADMITTED. It declares itself unreachable-by-schema, and it just reached the"
        bad "  API server. Structural validation has relaxed: re-point this document at the"
        bad "  validation that must now catch it and restore its EXPECT marker to 'validation #N'."
        return 1
      fi
      if ! printf '%s' "$out" | grep -qE 'is invalid:.*Required value'; then
        bad "$label was refused, but not by structural schema validation, which is what it declares."
        bad "  Answer: $out"
        return 1
      fi
      if printf '%s' "$out" | grep -qF -- "$POLICY"; then
        bad "$label is declared unreachable-by-schema but $POLICY answered it. The document now DOES"
        bad "  reach admission; give it back its 'EXPECT: DENIED by validation #N' marker."
        return 1
      fi
      pass "$label — refused by the API server's structural schema before admission; contained, and"
      pass "  correctly declaring that it is no evidence about $POLICY"
      ;;
    *)
      # L2-2 has already failed for this document; say it again at the point of use, so a reader
      # of the per-document section does not see a document silently skipped and count twelve.
      bad "$label declares no verdict this suite can submit — nothing was asked of the API server"
      return 1
      ;;
  esac
  return 0
}

# --- L2-3 · every negative document, alone --------------------------------------------------------
echo
echo "== L2-3. each of the $neg_n negative documents, submitted ALONE, is refused =="
note "the blob form of this question is satisfied by ONE refusal; these are $neg_n verdicts"
for i in $(seq 1 "$neg_n"); do
  name="$(sed -n 's/^  name: *//p' "$WORK/neg-$i.yaml" | head -1)"
  submit_doc "$WORK/neg-$i.yaml" "L2-3.$i negatives DOC $i (${name:-unnamed})" \
    "$(want_of "$WORK/neg-$i.yaml")"
done

# --- L2-4 · every positive document, alone --------------------------------------------------------
echo
echo "== L2-4. each of the $pos_n positive documents, submitted ALONE, is admitted =="
note "the control against a policy that denies too much — which is the cheapest way to make all"
note "  $neg_n arms above pass. V-CMP-003 applies this file too, but asks whether the TREE lands,"
note "  behind no P2 gate, so its green is also what an uninstalled policy produces"
for i in $(seq 1 "$pos_n"); do
  name="$(sed -n 's/^  name: *//p' "$WORK/pos-$i.yaml" | head -1)"
  submit_doc "$WORK/pos-$i.yaml" "L2-4.$i positive DOC $i (${name:-unnamed})" \
    "$(want_of "$WORK/pos-$i.yaml")"
done

echo
if [ "$fail" -eq 0 ]; then
  echo "V-CTN-012: PASS at L2 on $CTX — $checks/$checks assertions,"
  echo "  $((pos_n + neg_n)) corpus documents each judged on their own"
else
  echo "V-CTN-012: FAIL on $CTX — $checks assertions, see above"
fi
exit "$fail"
