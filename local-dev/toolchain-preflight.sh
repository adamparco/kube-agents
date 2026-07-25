#!/usr/bin/env bash
# Toolchain preflight — LSN-020. Run this BEFORE recording any lint result as evidence.
#
# The lesson it closes: P8-T5 recorded "`actionlint` on all 22 workflows -> exit 0" in the ledger,
# then went red on the pull request with four SC2140 findings. actionlint shells out to `shellcheck`
# for `run:` blocks and reports NOTHING AT ALL when that binary is absent. It was not installed.
# The local run and the CI run were different checks wearing the same name and the same exit code.
#
# The general shape is worth stating plainly, because it is not specific to actionlint: an exit code
# cannot distinguish "the rule held" from "the rule never ran". Optional-dependency degradation is
# the normal, deliberate design of lint tooling -- it is a kindness to contributors who lack a
# plugin -- so nothing is misconfigured and nothing warns. A MISSING linter is loud. A PARTIAL one
# is silent. That is V-MET-014 ("a check that cannot fail is not evidence") turned on the toolchain
# instead of on the assertions.
#
# The rule this encodes:
#
#     Anything the binding CI environment has and this host lacks is a FAILURE -- local is
#     silently weaker than the verdict that counts. Anything neither has is not: CI is the
#     binding verdict, so both being blind produces a red PR, not a false green. Anything
#     local has and CI lacks is fine; local is merely stricter.
#
# Deliberately NOT a unittest. `python3 -m unittest discover local-dev` runs for contributors, and
# asserting `shellcheck` is installed there would fail their build for a defect that is the
# harness's to avoid. This belongs to evidence-gathering, not to the repository's test suite --
# which is also why it is wired into .github/workflows/actionlint.yml, the one workflow whose
# verdict on shell rules is binding, rather than into the L0 chain.
#
# Exit 0 = the local rule set matches the binding one. Exit 1 = it does not; any lint pass recorded
# from this host is evidence for less than it appears to be.
#
# Usage: bash local-dev/toolchain-preflight.sh

set -uo pipefail

fail=0
note=0

have() { command -v "$1" >/dev/null 2>&1; }

# `go install`ed tools land in GOPATH/bin, which is frequently not on PATH in a non-login shell.
# Resolving actionlint only through PATH is not a detail: on the host where LSN-020 happened,
# actionlint WAS installed there and shellcheck was absent, so a PATH-only lookup would report
# "actionlint not installed", skip the shellcheck question entirely, and exit 0 -- reproducing the
# lesson inside the script written to close it.
ACTIONLINT="$(command -v actionlint 2>/dev/null || true)"
if [ -z "$ACTIONLINT" ] && command -v go >/dev/null 2>&1; then
  cand="$(go env GOPATH 2>/dev/null)/bin/actionlint"
  [ -x "$cand" ] && ACTIONLINT="$cand"
fi

ver() {
  case "$1" in
    actionlint) "$ACTIONLINT" --version 2>/dev/null | head -1 ;;
    shellcheck) shellcheck --version 2>/dev/null | awk '/^version:/ {print $2}' ;;
    prettier) prettier --version 2>/dev/null ;;
    pyflakes) pyflakes --version 2>/dev/null ;;
    *) echo "?" ;;
  esac
}

bad() {
  echo "FAIL: $1" >&2
  fail=1
}
ok() { echo "ok:   $1"; }
adv() {
  echo "note: $1"
  note=1
}

echo "== toolchain preflight (LSN-020) =="

# ---------------------------------------------------------------------------------------------
# actionlint and the rules it delegates.
#
# actionlint itself is only the YAML/expression layer. Every `run:` block is handed to shellcheck,
# and every `run:` block with `shell: python` is handed to pyflakes. Neither delegation is
# announced when the binary is missing.
# ---------------------------------------------------------------------------------------------
if [ -z "$ACTIONLINT" ]; then
  adv "actionlint is not installed; the workflow lint cannot be run from this host at all."
  adv "  That is the LOUD failure mode, not the silent one. Install: go install github.com/rhysd/actionlint/cmd/actionlint@latest"
else
  ok "actionlint $(ver actionlint)  ($ACTIONLINT)"

  # The shellcheck delegation. ubuntu-latest ships shellcheck preinstalled, so CI ALWAYS runs
  # these rules; a host without it produces a green that means only "the YAML parsed".
  # (Written this way and not as "# shellcheck ..." -- that prefix is a directive, and shellcheck
  #  refuses to parse the file when it cannot understand one. Fitting.)
  if have shellcheck; then
    ok "shellcheck $(ver shellcheck)  (actionlint delegates every run: block to it)"
  else
    bad "shellcheck is NOT installed, and actionlint delegates every run: block to it."
    echo "      actionlint will exit 0 having checked no shell at all, and say nothing." >&2
    echo "      CI's ubuntu-latest runner HAS shellcheck, so its verdict is strictly stronger" >&2
    echo "      than anything this host can produce. Install: brew install shellcheck" >&2
  fi

  # pyflakes: actionlint delegates `shell: python` run blocks to it. Whether the GitHub runner
  # image provides it is not something this script can know, so its absence is reported and not
  # failed -- claiming CI-parity we have not established would be the same error one level up.
  if have pyflakes; then
    ok "pyflakes $(ver pyflakes)  (actionlint delegates shell: python blocks to it)"
  else
    adv "pyflakes absent: actionlint will skip any 'shell: python' run block silently."
    adv "  Not a failure — whether the runner image supplies it is unverified, and asserting"
    adv "  parity we have not measured is the same mistake one level up."
  fi
fi

# ---------------------------------------------------------------------------------------------
# prettier. Not plugin-extensible in this repo (no .prettierrc plugins), so the only failure mode
# is a version skew: a formatter that reformats differently is a red Prettier Check on the PR.
# LSN-010 is that lesson; this records the version so the ledger row can name it.
# ---------------------------------------------------------------------------------------------
PRETTIER_BIN="${PRETTIER_BIN:-/opt/homebrew/bin/prettier}"
if have prettier; then
  ok "prettier $(ver prettier)"
elif [ -x "$PRETTIER_BIN" ]; then
  ok "prettier $("$PRETTIER_BIN" --version) (at $PRETTIER_BIN, not on PATH)"
else
  adv "prettier not found; .github/workflows/prettier.yml is the binding verdict on formatting."
fi

# ---------------------------------------------------------------------------------------------
# Version parity with the workflow that pins one. Not a failure: a newer local actionlint finds a
# superset, which is the harmless direction. Reported because LSN-020's standing rule is that a
# ledger row citing a linter must name its version and plugin set, not just its exit code -- and
# the row is easier to write honestly if the script prints the line.
# ---------------------------------------------------------------------------------------------
WF=".github/workflows/actionlint.yml"
if [ -n "$ACTIONLINT" ] && [ -f "$WF" ]; then
  pinned="$(grep -Eo 'ACTIONLINT_VERSION: *[0-9.]+' "$WF" | head -1 | awk '{print $2}')"
  local_v="$(ver actionlint | tr -d 'v')"
  if [ -n "$pinned" ] && [ "$pinned" != "$local_v" ]; then
    adv "actionlint version skew: local $local_v, $WF pins $pinned."
    adv "  Harmless while local is the newer one (superset of rules). Reversed, it is LSN-020 again."
  fi
fi

echo
echo "provenance (paste into the ledger row, not just the exit code):"
p_actionlint=absent; [ -n "$ACTIONLINT" ] && p_actionlint="$(ver actionlint)"
p_shellcheck=absent; have shellcheck && p_shellcheck="$(ver shellcheck)"
p_pyflakes=absent; have pyflakes && p_pyflakes="$(ver pyflakes)"
p_prettier=absent
if have prettier; then p_prettier="$(ver prettier)"; elif [ -x "$PRETTIER_BIN" ]; then p_prettier="$("$PRETTIER_BIN" --version)"; fi
echo "  actionlint=$p_actionlint shellcheck=$p_shellcheck pyflakes=$p_pyflakes prettier=$p_prettier"

echo
if [ "$fail" -ne 0 ]; then
  echo "PREFLIGHT FAILED — a linter on this host runs FEWER rules than the one whose verdict binds."
  echo "Do not record a lint pass as evidence until this exits 0. LSN-020."
  exit 1
fi
if [ "$note" -ne 0 ]; then
  echo "preflight ok (with notes above — read them before citing a lint result)"
else
  echo "preflight ok — local lint rule set matches the binding one"
fi
exit 0
