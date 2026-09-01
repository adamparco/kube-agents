#!/usr/bin/env bash
# ==============================================================================
# 🔍 Documentation terminology guard
# ==============================================================================
# Fails when documentation uses an identifier that does not match the source of
# truth. These are not style preferences: every rule below corresponds to a
# real defect that shipped, where a reader copy-pasting from the docs would
# have got a name that does not resolve.
#
# Portable to bash 3.2 (macOS) as well as CI. Deliberately fails loudly rather
# than skipping checks it cannot run.
# ==============================================================================

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

FAILED=0

# Documentation this guard inspects. The guard itself is excluded, since it
# necessarily contains the strings it forbids.
FILE_LIST=$(mktemp)
GREP_ERR=$(mktemp)
GREP_FAILURES=$(mktemp)
trap 'rm -f "$FILE_LIST" "$GREP_ERR" "$GREP_FAILURES"' EXIT

git ls-files '*.md' '*.mdx' \
  | grep -v '^docs/site/node_modules/' \
  | grep -v '^hack/check-docs-terminology.sh$' \
  > "$FILE_LIST"

FILE_COUNT=$(wc -l < "$FILE_LIST" | tr -d ' ')
if [ "$FILE_COUNT" -eq 0 ]; then
  echo "ERROR: no documentation files found - the guard cannot run." >&2
  exit 1
fi

echo "Checking terminology across ${FILE_COUNT} documentation files..."

# search <extended-regex> -> prints "path:line:text" matches, empty if none.
#
# "No matches" and "grep would not run" are different answers and used to be
# reported the same way. A pattern grep refuses to compile, or a file it cannot
# read, is a check that did not happen; folded into an empty result by
# `2>/dev/null || true` it becomes a check that passed, which is the one failure
# mode a guard must not have. The exit status cannot tell them apart -- xargs
# reports the same code whether grep found nothing or rejected the pattern -- so
# the discriminator is stderr, recorded here and reported at the end so every
# broken pattern is named rather than only the first.
search() {
  local out
  : > "$GREP_ERR"
  out=$(tr '\n' '\0' < "$FILE_LIST" | xargs -0 grep -nEI -H "$1" 2>"$GREP_ERR")
  if [ -s "$GREP_ERR" ]; then
    printf 'pattern: %s\n' "$1" >> "$GREP_FAILURES"
    sed 's/^/  /' "$GREP_ERR" >> "$GREP_FAILURES"
    return 2
  fi
  printf '%s' "$out"
}

# forbid <extended-regex> <explanation>
forbid() {
  local hits
  hits=$(search "$1")
  if [ -n "$hits" ]; then
    echo "::error::$2"
    printf '%s\n\n' "$hits" | sed 's/^/    /'
    FAILED=1
  fi
}

# --- Identity names -------------------------------------------------------
# Ground truth: k8s-operator/scripts/common.sh
forbid 'kubeagents-platform-agent-gsa' \
  "Wrong GCP service account name. common.sh sets PLATFORM_AGENT_GSA_NAME=kubeagents-platform-gsa."

forbid 'platform-agent-ksa' \
  "Wrong Kubernetes service account name. common.sh sets PLATFORM_AGENT_KSA_NAME=kubeagents-platform-agent (no -ksa suffix)."

# --- Namespace ------------------------------------------------------------
forbid 'platform-agent-system' \
  "Stale namespace. The namespace is kubeagents-system."

# --- GKE host-discovery label -------------------------------------------
# Ground truth: k8s-operator/scripts/common.sh. The Terraform full-install
# composition must mirror the stable key because it cannot source Bash.
HOST_LABEL=$(awk -F'"' '/^KUBE_AGENTS_HOST_LABEL=/{print $2; exit}' k8s-operator/scripts/common.sh)
if [ -z "$HOST_LABEL" ]; then
  echo "ERROR: could not read KUBE_AGENTS_HOST_LABEL from k8s-operator/scripts/common.sh." >&2
  exit 1
fi

WRONG_HOST_LABEL=$(search 'kube-?agents-host=true' | grep -vF "${HOST_LABEL}=true" || true)
if [ -n "$WRONG_HOST_LABEL" ]; then
  echo "::error::Documented host-discovery label does not match KUBE_AGENTS_HOST_LABEL=${HOST_LABEL}."
  printf '%s\n\n' "$WRONG_HOST_LABEL" | sed 's/^/    /'
  FAILED=1
fi

if ! grep -qE "\"${HOST_LABEL}\"[[:space:]]*=[[:space:]]*\"true\"" terraform/examples/full-install/main.tf; then
  echo "::error::Terraform full-install composition does not apply ${HOST_LABEL}=true."
  FAILED=1
fi

# --- Go toolchain ---------------------------------------------------------
# Ground truth: k8s-operator/go.mod
GO_MOD_VERSION=$(awk '/^go /{print $2; exit}' k8s-operator/go.mod)
if [ -z "$GO_MOD_VERSION" ]; then
  echo "ERROR: could not read the go directive from k8s-operator/go.mod." >&2
  exit 1
fi
GO_MINOR=$(printf '%s' "$GO_MOD_VERSION" | cut -d. -f1,2)   # 1.25.8 -> 1.25

WRONG_GO=$(search 'Go[^0-9]{0,20}1\.[0-9]+\+' | grep -vF "${GO_MINOR}+" || true)
if [ -n "$WRONG_GO" ]; then
  echo "::error::Documented Go version does not match k8s-operator/go.mod (go ${GO_MOD_VERSION}; expected \"${GO_MINOR}+\")."
  printf '%s\n\n' "$WRONG_GO" | sed 's/^/    /'
  FAILED=1
fi

# --- cert-manager version -------------------------------------------------
# Ground truth: images.json, which the mirror tooling also reads to build the release
# URL it applies. Three pages quote the version back — two of them inside a
# copy-pasteable `kubectl apply` URL — and there is nothing else to catch them,
# because the pin has no other consumer to disagree with. Left unguarded, the
# next cert-manager bump would hand readers a manifest the install does not use.
CERT_MANAGER_VERSION=$(jq -r '.images[] | select(.name == "cert-manager-controller") | .tag' images.json)
if [ -z "$CERT_MANAGER_VERSION" ] || [ "$CERT_MANAGER_VERSION" = "null" ]; then
  echo "ERROR: no cert-manager-controller pin in images.json; the version guard cannot run." >&2
  exit 1
fi

# `v1.13.0+` is a floor — the oldest release the operator's webhook works
# against — not a claim about what provisioning installs, so the trailing `+`
# form is exempt. A line carrying both a floor and a pin is exempted too; that
# has not happened, and the alternative is a regex nobody can maintain.
WRONG_CERT_MANAGER=$(search 'cert-manager[^.]{0,40}v[0-9]+\.[0-9]+\.[0-9]+([^+]|$)' \
  | grep -Ev 'v[0-9]+\.[0-9]+\.[0-9]+\+' \
  | grep -vF "$CERT_MANAGER_VERSION" || true)
if [ -n "$WRONG_CERT_MANAGER" ]; then
  echo "::error::Documented cert-manager version does not match images.json (${CERT_MANAGER_VERSION})."
  printf '%s\n\n' "$WRONG_CERT_MANAGER" | sed 's/^/    /'
  FAILED=1
fi

# --- fleet-audit finding id pattern ---------------------------------------
# Ground truth: FINDING_ID_RE in the fleet-audit harness. Two documents quote
# this pattern back to the model — SKILL.md and the ledger design doc — and an
# id that does not match is rejected before anything is published. When the
# pattern was last relaxed, every copy silently went stale, so the docs told
# the model to generate ids the validator refused.
AUDIT_SCRIPT=agents/platform/skills/fleet-audit/scripts/audit_report.py
if [ ! -f "$AUDIT_SCRIPT" ]; then
  echo "ERROR: ${AUDIT_SCRIPT} not found; the finding-id guard cannot run." >&2
  exit 1
fi

# The source pattern, rewritten into the form prose should use: a non-capturing
# group reads as noise to a human, and `\Z` is a Python-ism whose only
# difference from `$` (not matching before a trailing newline) is precisely why
# the code uses it and precisely what prose does not need to say.
ID_PATTERN=$(awk -F"r\"" '/^FINDING_ID_RE = re\.compile\(/{print $2; exit}' "$AUDIT_SCRIPT" \
  | sed 's/)$//; s/"$//; s/(?:/(/g; s/\\Z/$/')
if [ -z "$ID_PATTERN" ]; then
  echo "ERROR: could not read FINDING_ID_RE from ${AUDIT_SCRIPT}." >&2
  exit 1
fi

# Any line quoting a finding-id-shaped character class must quote this exact
# pattern. Anchored on `[a-z0-9._-]`, which is specific enough not to collide
# with the unrelated slug rules elsewhere in the docs.
WRONG_ID=$(search '\[a-z0-9\._-\]' | grep -vF "$ID_PATTERN" || true)
if [ -n "$WRONG_ID" ]; then
  echo "::error::Documented finding-id pattern does not match FINDING_ID_RE in ${AUDIT_SCRIPT} (expected ${ID_PATTERN})."
  printf '%s\n\n' "$WRONG_ID" | sed 's/^/    /'
  FAILED=1
fi

# A guard that passes because every copy disappeared is not a passing guard.
ID_COPIES=$(search '\[a-z0-9\._-\]' | grep -cF "$ID_PATTERN" || true)
if [ "${ID_COPIES:-0}" -lt 1 ]; then
  echo "::error::No document quotes the finding-id pattern any more; either restore it or drop this guard."
  FAILED=1
fi

# --- fleet-audit rendering caps -------------------------------------------
# Ground truth: the module constants in the fleet-audit harness. Every one is
# hand-copied into SKILL.md, the governance SOPs, the design doc, and the site
# so the model knows what the renderer will do to its output, and until now
# nothing checked a single copy. A stale cap is not cosmetic: it teaches the
# model to pad an excerpt the clipper then truncates mid-object, or to withhold
# findings against a budget that has moved.

# cap_value <CONSTANT> -> its integer literal, underscore separators removed
cap_value() {
  awk -v name="$1" '$1 == name && $2 == "=" { gsub(/_/, "", $3); print $3; exit }' \
    "$AUDIT_SCRIPT"
}

# group_digits 60000 -> 60,000. Prose uses both spellings; source uses neither.
group_digits() {
  printf '%s' "$1" | sed -e :a -e 's/\(.*[0-9]\)\([0-9]\{3\}\)/\1,\2/;ta'
}

# spellings <CONSTANT> -> alternation of every form documentation may use.
# Small values get their English word too, because the SOPs write "five per
# run" and "under sixteen characters"; without the word form, correcting a
# changed cap by hand would leave the guard permanently red.
WORDS="zero one two three four five six seven eight nine ten eleven twelve \
thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
spellings() {
  local value grouped word alt
  value=$(cap_value "$1")
  [ -n "$value" ] || return 1
  # The result is interpolated into an extended regex below, so anything that is
  # not a bare integer is a constant this function cannot spell -- and a stray
  # `(` or `+` from, say, `MAX_X = int(1e5)` would make grep reject the whole
  # pattern rather than say so. Refuse it here; the loop that calls this treats
  # a refusal as fatal.
  case "$value" in *[!0-9]*) return 1 ;; esac
  alt="$value"
  grouped=$(group_digits "$value")
  [ "$grouped" != "$value" ] && alt="${alt}|${grouped}"
  word=$(printf '%s' "$WORDS" | awk -v n="$value" '{ print (n < NF) ? $(n + 1) : "" }')
  [ -n "$word" ] && alt="${alt}|${word}"
  printf '(%s)' "$alt"
}

# cap_guard <probe> <good> <explanation> [<except>]
# The probe pins the number to the phrase that names it, so an unrelated figure
# elsewhere on the same line can neither satisfy nor trip the check. Anything
# the probe finds and the good pattern does not is a stale copy.
cap_guard() {
  local probe="$1" good="$2" why="$3" except="${4:-}" all hits copies
  all=$(search "$probe")
  if [ -n "$all" ] && [ -n "$except" ]; then
    all=$(printf '%s\n' "$all" | grep -vE "$except" || true)
  fi
  hits=$(printf '%s' "$all" | grep -vE "$good" || true)
  if [ -n "$hits" ]; then
    echo "::error::$why"
    printf '%s\n\n' "$hits" | sed 's/^/    /'
    FAILED=1
  fi
  # A guard that passes because every copy disappeared is not a passing guard.
  copies=$(printf '%s' "$all" | grep -cE "$good" || true)
  if [ "${copies:-0}" -lt 1 ]; then
    echo "::error::No document states this cap any more; restore it or drop the guard. ($why)"
    FAILED=1
  fi
}

for CONSTANT in MAX_EXCERPT_LINES MAX_EXCERPT_CHARS MAX_COMMAND_CHARS \
  MAX_BODY_CHARS BODY_BUDGET MAX_SCOPE_ROWS AUTO_PROMOTION_CAP MIN_NA_REASON_CHARS \
  MIN_CHECK_COMMAND_CHARS; do
  if ! spellings "$CONSTANT" > /dev/null; then
    echo "ERROR: could not read ${CONSTANT} from ${AUDIT_SCRIPT} as an integer." >&2
    exit 1
  fi
done

# The excerpt clip is always written as a pair — "40 lines / 2,000 characters"
# — so one probe pins both halves; splitting them would let a line quote the
# right line count beside the wrong character count and pass twice. The second
# alternative is the skill's bolded phrasing, `**40 lines and** 2,000
# characters`: the closing `**` falls between "and" and the number, with no
# space before it. Spelling it `and \*\* ` instead matches nothing, which the
# copies floor cannot catch while the slash form still matches six files.
JOIN='( ?/ ?| and\*\* )'
cap_guard \
  "[0-9]+ lines${JOIN}[0-9,]+" \
  "$(spellings MAX_EXCERPT_LINES) lines${JOIN}$(spellings MAX_EXCERPT_CHARS)" \
  "Documented excerpt clip does not match MAX_EXCERPT_LINES/MAX_EXCERPT_CHARS in ${AUDIT_SCRIPT}."

cap_guard \
  'command to [0-9,]+' \
  "command to $(spellings MAX_COMMAND_CHARS)" \
  "Documented command clip does not match MAX_COMMAND_CHARS in ${AUDIT_SCRIPT}."

# GitHub's hard limit and the renderer's own budget share the "body at N"
# idiom, so each excludes the other's subject rather than accepting both
# numbers — a line that swapped them would otherwise read as correct.
cap_guard \
  'GitHub caps an? [a-z]* ?body at [0-9,]+' \
  "GitHub caps an? [a-z]* ?body at $(spellings MAX_BODY_CHARS)" \
  "Documented GitHub body limit does not match MAX_BODY_CHARS in ${AUDIT_SCRIPT}."

# `targets` is an ordinary English verb, so its arm requires a four-or-more
# digit figure — otherwise a future "the sweep targets 3 clusters" would be read
# as a stale copy of this cap. `body at` is specific enough to take any number.
cap_guard \
  'body at [0-9][0-9,]*|targets \*{0,2}[0-9]{1,3},?[0-9]{3}' \
  "(body at |targets \*{0,2})$(spellings BODY_BUDGET)" \
  "Documented body budget does not match BODY_BUDGET in ${AUDIT_SCRIPT}." \
  'GitHub caps an? '

cap_guard \
  'scope tables? (at|to) [0-9,]+ rows' \
  "scope tables? (at|to) $(spellings MAX_SCOPE_ROWS) rows" \
  "Documented scope-table cap does not match MAX_SCOPE_ROWS in ${AUDIT_SCRIPT}."

PROMOTION_TAIL='(per run|auto-promotions per run|PRs per)'
cap_guard \
  "([Aa]t most|[Cc]apped at) \*{0,2}[a-z0-9]+\*{0,2} ${PROMOTION_TAIL}" \
  "([Aa]t most|[Cc]apped at) \*{0,2}$(spellings AUTO_PROMOTION_CAP)\*{0,2} ${PROMOTION_TAIL}" \
  "Documented auto-promotion cap does not match AUTO_PROMOTION_CAP in ${AUDIT_SCRIPT}."

cap_guard \
  'reason under [a-z0-9]+ characters' \
  "reason under $(spellings MIN_NA_REASON_CHARS) characters" \
  "Documented not-applicable reason floor does not match MIN_NA_REASON_CHARS in ${AUDIT_SCRIPT}."

# The check-command floor. The six SOPs and the skill write it "anything under
# eight characters"; the ledger design writes it "shorter than eight
# characters". Both spellings are in the probe so the ledger is covered too, and
# the subject word keeps this distinct from the reason floor above, so the two
# cannot satisfy each other. This probe subsumes the bare "anything under" form,
# so the audit stream's narrower copy of the same guard is not carried alongside
# it — two guards on one cap would report every drift twice.
cap_guard \
  '(anything under|shorter than) [a-z0-9]+ characters' \
  "(anything under|shorter than) $(spellings MIN_CHECK_COMMAND_CHARS) characters" \
  "Documented check-command floor does not match MIN_CHECK_COMMAND_CHARS in ${AUDIT_SCRIPT}."

# `checks_run[].command` is published at MAX_COMMAND_CHARS, not MAX_CELL_CHARS:
# the appendix exists so a reader can re-run a command, and one clipped to an
# ellipsis cannot be re-run while still reading as though it could. Two guards
# here used to pin MAX_CELL_CHARS to that field instead, which taught the model
# a 120-character ceiling the renderer stopped enforcing. The allowance is
# quoted twice in the same breath — once as what the field gets, once as what
# `evidence.command` gets — so both spellings get a probe, because a document
# that corrected one and not the other would still teach a length the renderer
# disagrees with. MAX_CELL_CHARS itself is now an internal legibility clip on
# the remaining columns and is deliberately not documented anywhere; a guard on
# an undocumented cap can only be satisfied by inventing prose for it.
cap_guard \
  '[0-9,]+-character allowance' \
  "$(spellings MAX_COMMAND_CHARS)-character allowance" \
  "Documented checks_run command allowance does not match MAX_COMMAND_CHARS in ${AUDIT_SCRIPT}."

cap_guard \
  'allowed [0-9,]+ characters' \
  "allowed $(spellings MAX_COMMAND_CHARS) characters" \
  "Documented evidence.command allowance does not match MAX_COMMAND_CHARS in ${AUDIT_SCRIPT}."

# `evidence.command` is the roomy field, and the SOPs contrast it against the
# cell clip above. Anchored on "allowed", since "command to 2,000" already has
# its own guard and the two idioms must not satisfy each other.
cap_guard \
  'allowed [0-9,]+ characters' \
  "allowed $(spellings MAX_COMMAND_CHARS) characters" \
  "Documented evidence.command budget does not match MAX_COMMAND_CHARS in ${AUDIT_SCRIPT}."

# --- fleet-audit cron prompts ---------------------------------------------
# Ground truth: the prompts in the cron manifest. Two site pages quote the
# compliance watchdog's prompt verbatim to show what an anti-skim prompt looks
# like, and both went stale the moment the SOP grew — they told readers the SOP
# was 348 lines with its checks at 56-270 when it was 406 lines with its checks
# at 102-314. A quotation that has drifted from the thing it quotes is worse
# than no quotation, so require it to be a literal substring of the manifest.
#
# The anchor is the JSON key, not anything the prompt says. It used to be the
# phrase `all N lines of it`, and that is precisely how this guard stopped
# working: the prompts dropped their line-count geography to name a collector
# instead, no document matched the phrase any more, the loop below ran zero
# times, and the check went green while both pages still carried the superseded
# quotation it exists to catch. An anchor drawn from the text being checked
# fails silent the one time it matters — when that text changes. `"prompt": "`
# is structural, so a rewording cannot make the guard stop looking.
#
# The consequence to know before you write: a fenced `"prompt": "…"` line is
# treated as a claim to be quoting a roster entry. Paraphrase a watchdog in
# prose and nothing here objects; render it as manifest JSON and the value has
# to be the manifest's.
#
# Both rosters are ground truth. The governance prompts live on the Platform
# Agent's; the Chat Agent's is checked too so a job that moves between them
# does not turn every quotation stale on the way.
CRON_JOBS="agents/platform/cron/jobs.json agents/chat/defaults/cron/jobs.json"
for JOBS_FILE in $CRON_JOBS; do
  if [ ! -f "$JOBS_FILE" ]; then
    echo "ERROR: ${JOBS_FILE} not found; the cron-prompt guard cannot run." >&2
    exit 1
  fi
done

PROMPT_HITS=$(mktemp)
ORPHAN_HITS=$(mktemp)
ROSTER_ID_FILE=$(mktemp)
trap 'rm -f "$FILE_LIST" "$GREP_ERR" "$GREP_FAILURES" "$PROMPT_HITS" "$ORPHAN_HITS" "$ROSTER_ID_FILE"' EXIT

# Which documents are claiming to quote the roster at all. The anchor below is
# structural for the reason above, but `"prompt": "` is not structure unique to
# a cron manifest -- it is the shape of a LiteLLM request body, a Vertex
# payload, and a bench fixture, none of which this guard has an opinion about
# and all of which live in `examples/`, `bench/` and the reference pages that
# document them. Applied to all 280 markdown files in the repository, the
# anchor turns any unrelated pull request that renders one of those into a red
# CI run with an error message about cron prompts.
#
# So narrow the population, not the anchor. A block renders a roster *entry*
# when it renders a real job's `"id"`, which every one of the three quotations
# in the tree today does, one line above its `"prompt"`. That keeps the
# property the structural anchor was adopted for -- nothing here is drawn from
# what a prompt says, so a rewording cannot make the guard stop looking -- while
# a `prompt` key in a block that never names a job is left alone.
#
# `jq` over both rosters rather than a hand-kept list: a new job is then covered
# the day it is added, and a renamed one does not quietly narrow this to zero.
# The exit status is checked and stderr is left alone: read through
# `2>/dev/null` a parse error silently *narrowed* the id set -- to one roster's
# ids, or to none -- and every document the missing ids covered dropped out of
# the check without a word.
# shellcheck disable=SC2086 -- CRON_JOBS is a deliberate word-split list.
if ! jq -r '(.jobs // .)[].id' $CRON_JOBS | sort -u > "$ROSTER_ID_FILE"; then
  echo "ERROR: could not read job ids from ${CRON_JOBS}; the cron-prompt guard cannot run." >&2
  exit 1
fi
if [ ! -s "$ROSTER_ID_FILE" ]; then
  echo "ERROR: no job ids read from ${CRON_JOBS}; the cron-prompt guard cannot run." >&2
  exit 1
fi

# The scan below decides three things at once, per fenced block rather than per
# document, and the block is the point:
#
#   R  the block renders a roster id, so every `"prompt"` in it is graded.
#   O  the block is shaped like a roster entry -- `schedule`, `skills`,
#      `deliver` or `no_agent` sits beside its prompt -- but names no id this
#      roster knows. That is reported, not skipped.
#
# Deciding by document, the way this used to, meant a page that named a job in
# one section and rendered an unrelated `"prompt"` key in another failed CI with
# an error about cron prompts, pointing at a block that was never claiming to
# quote anything. A block is what a reader sees as one manifest, so the id and
# the prompt it labels have to be in the same fence.
#
# `O` closes the other half of the same hole. Eliding the `"id"` line from a
# rendered entry used to remove it from the check entirely -- no error, no
# coverage -- so deleting one line was all it took to silence the guard on a
# quotation. A block that still looks like a roster entry now has to name a job.
#
# The ids are compared whole, inside awk, and never become part of a pattern.
# Interpolated into an alternation instead, a single `(`, `|` or `+` in a job id
# made `grep -E` reject the pattern outright; the error went to /dev/null, the
# status went to `|| true`, and the guard reported PASS having read nothing.
#
# Matching the key wherever on the line it falls and however it is spaced is
# deliberate. An anchor that insisted on the key starting the line, followed by
# exactly one space, saw only prettier's rendering of a multi-line object — a
# one-line `{"id": …, "prompt": …}` entry, or a hand-spaced one, went unchecked,
# which is the same silent skip the structural anchor was adopted to end.
# Prettier normalises much of this back, but only inside a fence tagged `json`,
# and CI does not run it over `.mdx` at all. What no line-based anchor can see
# is a quotation whose value sits on the line after its key; those are unchecked.
#
# The awk program is single-quoted on purpose; `$0` below is awk's, not the
# shell's, and the ids reach it through -v rather than through interpolation.
# shellcheck disable=SC2016
if ! SCAN=$(tr '\n' '\0' < "$FILE_LIST" | xargs -0 awk -v idfile="$ROSTER_ID_FILE" '
  function flush(   i, kind) {
    kind = hasid ? "R" : (cronish ? "O" : "")
    if (kind != "")
      for (i = 1; i <= n; i++)
        printf "%s:%s:%s:%s\n", kind, pfile[i], pline[i], ptext[i]
    n = 0; hasid = 0; cronish = 0
  }
  BEGIN { while ((getline id < idfile) > 0) if (id != "") ids["\"" id "\""] = 1 }
  FNR == 1 { flush() }
  /^[ \t]*(```|~~~)/ { flush(); next }
  {
    rest = $0
    while (match(rest, /"id"[ \t]*:[ \t]*"[^"]*"/)) {
      seg = substr(rest, RSTART, RLENGTH)
      sub(/^"id"[ \t]*:[ \t]*/, "", seg)
      if (seg in ids) hasid = 1
      rest = substr(rest, RSTART + RLENGTH)
    }
    if ($0 ~ /"(schedule|skills|deliver|no_agent)"[ \t]*:/) cronish = 1
    if ($0 ~ /"prompt"[ \t]*:[ \t]*"/) {
      n++; pfile[n] = FILENAME; pline[n] = FNR; ptext[n] = $0
    }
  }
  END { flush() }
'); then
  echo "ERROR: could not scan the documentation for cron prompts; the guard cannot run." >&2
  exit 1
fi
printf '%s\n' "$SCAN" | sed -n 's/^R://p' > "$PROMPT_HITS"
printf '%s\n' "$SCAN" | sed -n 's/^O://p' > "$ORPHAN_HITS"

if [ -s "$ORPHAN_HITS" ]; then
  echo "::error::A rendered cron roster entry names no job id from ${CRON_JOBS}, so its prompt cannot be checked. Restore the \"id\" line, or correct it if the job was renamed."
  sed 's/^/    /' "$ORPHAN_HITS"
  FAILED=1
fi

# No floor here, unlike the caps above: the site owes nobody a quotation of a
# cron prompt, and zero copies is zero stale copies. That is safe only because
# the anchor above is structural — see the note on why it used to not be.
#
# There is a floor on how much of a prompt a quotation has to show, though. The
# ellipsis trim below checks as far as the elision and no further, so it can
# leave almost nothing to check: `"prompt": "R…"` reduces to `R`, and a
# one-character `grep -qF` matches every manifest that has ever existed. A
# needle that short is not verification, so require enough of the prompt to
# identify which one is being quoted. The shortest real quotation in the tree
# today is `concepts/skills.md`'s opening sentence, at 51 characters.
MIN_QUOTED_CHARS=24
STALE_PROMPTS=""
SHORT_PROMPTS=""
while IFS= read -r HIT; do
  [ -n "$HIT" ] || continue
  # Strip the grep `path:line:` prefix and the JSON key, then cut at the first
  # unescaped `"` — the close of the value, whether what follows it is nothing,
  # a comma, `}`, or the rest of a one-line object. Matching only `"` and `",`
  # at end of line left the tail attached and turned a verbatim quotation into
  # a false stale report.
  #
  # A trailing ellipsis is an explicit elision — `concepts/skills.md` quotes the
  # first sentence to show the shape of an entry, not the prompt — so check as
  # far as the ellipsis and no further. Abbreviating is allowed; misquoting the
  # part you did show is not. Both spellings count: house style is `…`, and
  # accepting only `...` failed the quotations that follow it.
  # Every `"prompt": "…"` on the line, not just the last one. The single `sed`
  # this replaced led with `^.*"prompt"`, and `.*` is greedy: on a line carrying
  # two of them it stripped past the first and graded only the second, so a
  # fabricated first value passed green. The value's own character class has no
  # such preference — `[^"\\]|\\.` stops at the first unescaped quote by
  # construction rather than by a following substitution — and `grep -o` emits
  # one match per occurrence, so both get graded.
  VALUES=$(printf '%s\n' "$HIT" \
    | grep -oE '"prompt"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"')
  # A value with no closing quote on its line matches nothing above. Fall back
  # to the old read — the rest of the line — rather than skipping the hit,
  # because a hit the loop declines to grade is the silent pass this whole
  # guard exists to stop, and an unterminated value is exactly the shape a
  # half-pasted quotation takes.
  if [ -z "$VALUES" ]; then
    VALUES=$(printf '%s\n' "$HIT" | sed -E 's/^.*"prompt"[[:space:]]*:[[:space:]]*/"prompt": /')
  fi
  while IFS= read -r VALUE; do
    [ -n "$VALUE" ] || continue
    QUOTED=$(printf '%s\n' "$VALUE" \
      | sed -E 's/^"prompt"[[:space:]]*:[[:space:]]*"?//; s/"$//; s/[[:space:]]*(\.\.\.|…)$//')
    if [ "${#QUOTED}" -lt "$MIN_QUOTED_CHARS" ]; then
      SHORT_PROMPTS="${SHORT_PROMPTS}${HIT}
"
      break
    fi
    # shellcheck disable=SC2086 -- CRON_JOBS is a deliberate word-split list.
    grep -qF -- "$QUOTED" $CRON_JOBS
    MATCH_STATUS=$?
    if [ "$MATCH_STATUS" -ge 2 ]; then
      echo "ERROR: grep could not read ${CRON_JOBS}; the cron-prompt guard cannot run." >&2
      exit 1
    fi
    if [ "$MATCH_STATUS" -ne 0 ]; then
      STALE_PROMPTS="${STALE_PROMPTS}${HIT}
"
      break
    fi
  done <<VALUES_EOF
$VALUES
VALUES_EOF
done < "$PROMPT_HITS"

if [ -n "$STALE_PROMPTS" ]; then
  echo "::error::Documented cron prompt is not a verbatim copy of any prompt in ${CRON_JOBS}."
  printf '%s\n' "$STALE_PROMPTS" | sed '/^$/d; s/^/    /'
  FAILED=1
fi

if [ -n "$SHORT_PROMPTS" ]; then
  echo "::error::Documented cron prompt is elided down to fewer than ${MIN_QUOTED_CHARS} characters, which verifies nothing. Quote more of it before the ellipsis, or paraphrase it in prose instead of rendering it as manifest JSON."
  printf '%s\n' "$SHORT_PROMPTS" | sed '/^$/d; s/^/    /'
  FAILED=1
fi

# --- Checks that could not run --------------------------------------------
# Reported last so every broken pattern is named at once, and reported at all
# because a check that did not run is not a check that passed.
if [ -s "$GREP_FAILURES" ]; then
  echo "::error::A terminology check could not run: grep rejected a pattern, or could not read a file."
  sed 's/^/    /' "$GREP_FAILURES"
  FAILED=1
fi

# --- Result ---------------------------------------------------------------
if [ "$FAILED" -eq 0 ]; then
  echo "Terminology check passed."
else
  echo "Terminology check failed. Fix the identifiers above, or update this guard"
  echo "if the source of truth itself has changed."
fi

exit "$FAILED"
