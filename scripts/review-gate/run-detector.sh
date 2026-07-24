#!/usr/bin/env bash
# Review-gate detector (Phase 5 / P5-T4, decision R-A).
#
# Runs the agent-driven review skills HEADLESSLY and writes the aggregated JSON finding array to $OUT.
# The detector only SUPPLIES evidence; score_findings.py is the authoritative gate (06 §7: CI is
# authoritative and runs outside the agent). This step needs a model credential + repo context, so on
# a fork PR / when ANTHROPIC_API_KEY is unset it SKIPS LOUDLY (annotation + job summary) and emits an
# empty finding set — never a silent pass (decision R-A). We never `set -e`: the detector must always
# leave a well-formed $OUT and exit 0 so the *scorer* is the gate, not this glue.
set -uo pipefail

OUT="${1:-findings.json}"
HERE="$(cd "$(dirname "$0")" && pwd)"

note() { echo "review-gate/detector: $*"; }
summary() { if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$*" >>"$GITHUB_STEP_SUMMARY"; fi; }

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "::warning title=Review-gate detector skipped::No ANTHROPIC_API_KEY (fork PR or secret unset) — the agent-driven review-security-k8s-* review did NOT run. A maintainer must run it before merge."
  summary "### Review-gate: detector SKIPPED ⚠️"
  summary ""
  summary "No \`ANTHROPIC_API_KEY\` available (fork PR / secret unset). The agent-driven \`review-security-k8s-main\` + \`review-security-k8s-agents-main\` review did **not** run; a maintainer must run it before merge. The hermetic scorer still ran (on an empty finding set)."
  echo "[]" >"$OUT"
  note "skipped — wrote empty findings to $OUT"
  exit 0
fi

PROMPT="You are the kube-agents review-gate detector. Invoke the review-security-k8s-main and \
review-security-k8s-agents-main skills against the changes in this repository. Triage findings per \
each skill's step 3 against the project context in docs/design/. Output ONLY the aggregated JSON \
finding array in the schema [{\"agent\":\"<skill>\",\"findings\":[{\"message\":\"<desc>\",\"file\":\"<path>\",\"line\":\"<num>\",\"severity\":\"<critical|high|medium|low>\"}]}]. \
No prose, no explanation — JSON only."

if ! command -v claude >/dev/null 2>&1; then
  note "claude CLI not found — installing @anthropic-ai/claude-code"
  if ! npm install -g @anthropic-ai/claude-code >/dev/null 2>&1; then
    echo "::warning title=Review-gate detector unavailable::Could not install the claude CLI; the agent-driven review did NOT run."
    summary "### Review-gate: detector UNAVAILABLE ⚠️ (claude CLI install failed) — scorer ran on an empty set."
    echo "[]" >"$OUT"
    exit 0
  fi
fi

note "running headless review (read-only)"
RAW="$(claude -p "$PROMPT" --permission-mode plan 2>/dev/null || true)"
printf '%s' "$RAW" | python3 "$HERE/extract_findings.py" >"$OUT" || echo "[]" >"$OUT"

COUNT="$(python3 -c "import json,sys; d=json.load(open('$OUT')); print(sum(len(a.get('findings',[])) for a in d if isinstance(a,dict)))" 2>/dev/null || echo '?')"
note "wrote $OUT ($COUNT finding(s))"
summary "### Review-gate: detector ran — ${COUNT} finding(s) before scoring."
exit 0
