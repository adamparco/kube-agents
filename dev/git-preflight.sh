#!/usr/bin/env bash
# git-preflight.sh — LSN-012, "the remote that carries the work is not the one you assume".
#
# THE FAILURE. Local `main` in this clone tracked `upstream/main` (gke-labs/kube-agents), which has
# none of this build. The work lives on the fork. So `git pull` on main fetches the wrong tree, a
# diff against `main` shows tens of unrelated commits or nothing at all, and a PR opened against the
# tracked base contains none of the phase work. Remote NAMES are not a reliable handle either --
# they have already changed once during this build (`fork` then, `origin` now), so a check that
# hardcodes `origin` is checking a spelling, not a property.
#
# WHAT THIS ASSERTS, and why each one is the property rather than a proxy:
#
#   1. The work-carrying remote is identified by CONTENT, not by name: it is the remote whose `main`
#      contains `docs/build/LEDGER.md`, the harness's own build ledger. A remote that does not carry
#      the ledger does not carry the build, whatever it is called.
#   2. `main`'s upstream points at that remote. This is the one that actually bit: everything else
#      can be right while a bare `git pull` still reaches for gke-labs.
#   3. No local branch tracks a DIFFERENT remote than main does. A mixed set is how you end up
#      pushing one branch to the fork and diffing another against upstream without noticing.
#
# WHAT IT DOES NOT ASSERT, and why not:
#
#   * That `origin` is any particular URL. This repository is a fork of a public project and the
#     upstream is a legitimate remote to have; the defect is which one carries the work, not which
#     ones exist. Pinning a URL would also make this check fail for every other contributor, which
#     is a check that punishes the wrong party (see LSN-020 for that mistake).
#   * Freshness of the fetch. `git fetch` is a network operation and a check that requires one is a
#     check that fails on a plane. The diff base being stale produces a WRONG diff, not a wrong
#     remote, and it is caught by the PR view.
#
# IN CI there are no local tracking refs, so assertions 2 and 3 have nothing to read and say so
# explicitly rather than passing silently. Assertion 1 still runs against `origin` and is real,
# provided the checkout has history (`fetch-depth: 0` in l0-checks.yml).
#
# Exit: 0 = pass · 1 = fail · 2 = could not run.
set -uo pipefail

LEDGER_PATH="docs/build/LEDGER.md"
fail=0
ran=0

say() { printf '%s\n' "$*"; }
ok() { say "ok:   $*"; }
bad()  { say "FAIL: $*"; fail=1; }
note() { say "note: $*"; }

say "== git preflight (LSN-012) =="

git rev-parse --git-dir >/dev/null 2>&1 || { say "REFUSING: not a git repository."; exit 2; }

# --- 1. Which remote actually carries the build? ------------------------------------------------
remotes="$(git remote)"
[ -n "$remotes" ] || { say "REFUSING: no remotes configured."; exit 2; }

carriers=""
for r in $remotes; do
  # `main` on that remote must exist AND contain the ledger. Both halves matter: a remote can have a
  # `main` that is simply the upstream project, which is precisely the case being distinguished.
  if git cat-file -e "refs/remotes/$r/main:$LEDGER_PATH" 2>/dev/null; then
    carriers="$carriers $r"
  fi
done
carriers="${carriers# }"

if [ -z "$carriers" ]; then
  note "no remote's main contains $LEDGER_PATH."
  note "  In a shallow or single-ref checkout this is expected -- the ref is simply not present."
  note "  Locally it is not: it means the fetch is stale, or the build is not on any remote yet."
  if [ -n "${GITHUB_ACTIONS:-}" ] || [ -n "${CI:-}" ]; then
    note "  CI detected; treated as not-applicable rather than as a failure."
  else
    bad "cannot identify the work-carrying remote. Run: git fetch --all"
  fi
  work_remote=""
else
  ran=$((ran + 1))
  set -- $carriers
  work_remote="$1"
  if [ "$#" -gt 1 ]; then
    ok "remotes carrying the build:$carriers (using '$work_remote' as the base)"
    note "  More than one is fine -- a mirror, or the fork and a backup. It becomes a problem only"
    note "  if they disagree, which is a divergence question and not this check's business."
  else
    ok "work-carrying remote is '$work_remote' ($(git remote get-url "$work_remote"))"
  fi
fi

# --- 2. Does `main` point at it? ------------------------------------------------------------------
# Quoted: `@{upstream}` is git revision syntax, not a shell brace expansion (shellcheck SC1083).
main_upstream="$(git rev-parse --abbrev-ref --symbolic-full-name "main@{upstream}" 2>/dev/null || true)"

if [ -z "$main_upstream" ]; then
  note "local branch 'main' has no upstream (or does not exist): nothing to check."
  note "  Expected in a CI checkout. Locally, an untracked main is safer than a wrongly-tracked one."
elif [ -z "$work_remote" ]; then
  note "main tracks '$main_upstream', but the work-carrying remote could not be identified;"
  note "  skipping the comparison rather than guessing which one is wrong."
else
  ran=$((ran + 1))
  if [ "$main_upstream" = "$work_remote/main" ]; then
    ok "main tracks $main_upstream, which is the work-carrying remote"
  else
    bad "main tracks '$main_upstream', which does NOT carry the build."
    say  "      A bare 'git pull' on main fetches a tree with none of this work, and a diff against"
    say  "      main is then meaningless. This is LSN-012 exactly. Fix:"
    say  "          git branch -u $work_remote/main main"
  fi
fi

# --- 3. Do the other branches agree? --------------------------------------------------------------
if [ -n "$work_remote" ]; then
  strays=""
  while IFS= read -r line; do
    br="${line%% *}"
    up="${line#* }"
    [ -n "$up" ] || continue
    [ "$up" = "$br" ] && continue
    case "$up" in
      "$work_remote"/*) ;;
      *) strays="$strays $br->$up" ;;
    esac
  done <<EOF
$(git for-each-ref --format='%(refname:short) %(upstream:short)' refs/heads/)
EOF
  ran=$((ran + 1))
  if [ -z "$strays" ]; then
    ok "every tracking branch points at '$work_remote'"
  else
    bad "branches tracking a remote other than '$work_remote':$strays"
    say  "      A mixed set is how one branch gets pushed to the fork while another is diffed"
    say  "      against upstream. Re-point them, or delete the ones that are finished."
  fi
fi

say ""
if [ "$ran" -eq 0 ]; then
  say "NOT APPLICABLE: no assertion had anything to read (shallow checkout, no remotes with main)."
  say "This is reported, not passed. A check with nothing to check is not evidence (V-MET-014)."
  exit 0
fi
if [ "$fail" -ne 0 ]; then
  say "git preflight FAILED ($ran assertions ran)"
  exit 1
fi
say "git preflight ok ($ran assertions ran)"
