#!/usr/bin/env bash
# l2-lock.sh — a per-machine advisory lock, so two L2 suites never score the same cluster at once.
#
# WHY THIS EXISTS
#   On 2026-07-31 `reader-scope-l2.sh` (V-CTN-001, BLOCKING-ALWAYS) came back rc 1 with `readers=4`
#   and 102 failures, every one of them against a ServiceAccount belonging to a DIFFERENT L2 suite
#   that happened to be running at the same time. Re-run after that namespace went away: rc 0, 1389
#   questions, 8/8 arms, identical per-arm scales ([[LSN-066]]).
#
#   Neither suite was wrong, and that is the whole point. `reader-scope-l2.sh` derives its roster
#   cluster-wide from `kube-agents/role=reader` rather than from a curated list, which is [[LSN-036]]
#   applied correctly. `startup-ordering-l2.sh` mints its fixture identity through the SHIPPED
#   `render_agent_identity`, so the fixture carries the same labels the real install carries, which
#   is [[LSN-024]] applied correctly. The label that makes the fixture honest is the same label that
#   makes it visible to the derived roster. Both obvious repairs are refused: dropping the label
#   re-introduces LSN-024, and narrowing the roster WEAKENS a BLOCKING-ALWAYS check — a reader
#   identity nobody bound, standing in a namespace nobody expected, is precisely what V-CTN-001
#   exists to find.
#
#   The defect is in neither artifact. It is that two L2 suites ran concurrently, which
#   `dev/L2-CHAIN.txt` never does and which nothing forbade. The general property, which is worth
#   more than the incident: A CHECK THAT DERIVES ITS SUBJECT SET FROM CLUSTER STATE IS MEASURING
#   EVERY OTHER TENANT OF THAT CLUSTER TOO. Every future derived-roster suite inherits this, and a
#   paragraph in `dev/L2-CHAIN.txt` is reachable only by someone already reading that file
#   ([[LSN-019]]: prose on the artifact is not a mechanization).
#
# WHAT IT DELIBERATELY DOES NOT DO
#   This file touches NO cluster, opens no network connection, mints no credential and reads no
#   kubeconfig. It creates one directory under a local lock root and writes one line into it: a pid,
#   a suite name, a context name and a timestamp. It cannot widen anyone's blast radius, and the
#   `gke-scratch-*` guards in its callers are unaffected — it runs before them and grants nothing.
#
#   It is ADVISORY. A suite that never calls it is not blocked, which is why the companion arm in
#   `dev/tests/invariants-gate.py` asserts that every script on `dev/L2-CHAIN.txt` takes it. A lock
#   nobody takes is prose again.
#
#   It is KEYED BY CONTEXT, not global. Two suites against two different scratch clusters share no
#   subject set and have no reason to serialize; the cost of the finer key is one `tr` and the
#   benefit is that the lock is never a reason to weaken the chain into one cluster.
#
#   It FAILS SAFE, in the one direction that matters. A holder that died without releasing leaves a
#   lock whose pid answers no `kill -0`; the next acquirer breaks it loudly rather than waiting out
#   the timeout. A wedged lock would otherwise convert a killed session into a permanently red
#   chain, which is a worse failure than the one this file is for.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/l2-lock.sh"
#   l2_lock_guard "$CTX" "reader-scope-l2"      # acquire + release on EXIT, chaining any prior trap
#   ...
#   # or, when the suite manages its own teardown ordering:
#   l2_lock_acquire "$CTX" "reader-scope-l2" || exit 1
#   trap 'my_teardown; l2_lock_release' EXIT
#
# Offline self-test:
#   bash dev/lib/l2-lock.sh --negative-control

# The lock root is overridable so the negative control can exercise real acquisition without
# touching the root a live suite would be using. Defaulting under $TMPDIR keeps it per-machine and
# per-boot-ish; the lock has no meaning across machines because the cluster it guards is shared but
# the pids it tracks are not.
: "${KAGE_L2_LOCK_ROOT:=${TMPDIR:-/tmp}/kage-l2-locks}"

# Default wait. Long enough that a suite queued behind a normal L2 run gets in rather than failing
# the chain, short enough that a genuinely wedged machine reports rather than hangs until a human
# notices. The chain is serial, so in the intended case this never waits at all.
: "${KAGE_L2_LOCK_TIMEOUT:=1800}"
: "${KAGE_L2_LOCK_POLL:=2}"

_L2_LOCK_HELD_DIR=""
_L2_LOCK_PRIOR_EXIT=""

# l2_lock_path <context>
#   The well-known lock directory for one context. Sanitised because a context name is allowed
#   characters a path is not (`gke_project_region_cluster` is fine, but nothing guarantees it).
l2_lock_path() {
  local ctx="$1" slug
  slug="$(printf '%s' "$ctx" | tr -c 'A-Za-z0-9._-' '_')"
  printf '%s/%s.lock' "$KAGE_L2_LOCK_ROOT" "$slug"
}

# _l2_lock_owner_pid <lockdir> — the pid recorded in the lock, or empty.
_l2_lock_owner_pid() {
  [ -f "$1/owner" ] || return 0
  awk -F'\t' 'NR==1 {print $1}' "$1/owner" 2>/dev/null
}

# _l2_lock_owner_desc <lockdir> — a human line naming who holds it, for the refusal message. A
# refusal that does not name the holder sends the reader to `ps` and then to guesswork.
_l2_lock_owner_desc() {
  if [ -f "$1/owner" ]; then
    awk -F'\t' 'NR==1 {printf "pid %s, suite %s, context %s, since %s", $1, $2, $3, $4}' "$1/owner" 2>/dev/null
  else
    printf 'an unreadable lock (no owner file) at %s' "$1"
  fi
}

# _l2_lock_break_if_stale <lockdir>
#   rc 0 = the lock is gone (it was stale and we removed it, or it never existed)
#   rc 1 = the lock is held by a live process
#
#   The break is done by an atomic `mv` to a unique name and only then a delete, so two acquirers
#   racing to break the same stale lock cannot both decide they won: `mv` succeeds for exactly one
#   of them, and the loser's next `mkdir` either succeeds or finds the winner's fresh lock. Deleting
#   in place would let both remove and both create.
_l2_lock_break_if_stale() {
  local dir="$1" pid grave
  [ -d "$dir" ] || return 0
  pid="$(_l2_lock_owner_pid "$dir")"

  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    return 1
  fi

  grave="${dir}.stale.$$"
  if mv "$dir" "$grave" 2>/dev/null; then
    echo "  l2-lock: BROKE A STALE LOCK — its holder ($(_l2_lock_owner_desc "$grave")) is not running." >&2
    rm -rf "$grave" 2>/dev/null || true
    return 0
  fi

  # Someone else won the break, or the holder released between our read and our mv. Either way the
  # lock we saw is not ours to reason about any more; re-test from scratch.
  [ -d "$dir" ] && return 1
  return 0
}

# l2_lock_acquire <context> [suite-name] [timeout-seconds]
#   rc 0 = held · rc 1 = timed out (message names the holder) · rc 2 = refused (bad arguments)
l2_lock_acquire() {
  local ctx="${1:-}" suite="${2:-$(basename "${0:-unknown}")}" timeout="${3:-$KAGE_L2_LOCK_TIMEOUT}"
  local dir waited=0

  # An empty context is refused rather than defaulted. The ambient kubectl context on a developer
  # machine belongs to a third cluster, and a lock keyed on "" serializes suites that share no
  # cluster while letting through the two that do — the exact inversion of the property
  # ([[LSN-018]]: a cluster-touching path names its context explicitly or it is wrong).
  if [ -z "$ctx" ]; then
    echo "  l2-lock: REFUSED — l2_lock_acquire needs an explicit kubectl context as \$1." >&2
    return 2
  fi

  dir="$(l2_lock_path "$ctx")"
  mkdir -p "$KAGE_L2_LOCK_ROOT" 2>/dev/null || {
    echo "  l2-lock: REFUSED — cannot create the lock root $KAGE_L2_LOCK_ROOT." >&2
    return 2
  }

  while :; do
    if mkdir "$dir" 2>/dev/null; then
      printf '%s\t%s\t%s\t%s\n' "$$" "$suite" "$ctx" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$dir/owner"
      _L2_LOCK_HELD_DIR="$dir"
      echo "  l2-lock: held by $suite (pid $$) for context $ctx"
      return 0
    fi

    if _l2_lock_break_if_stale "$dir"; then
      continue
    fi

    if [ "$waited" -ge "$timeout" ]; then
      echo "  l2-lock: TIMED OUT after ${waited}s waiting for context $ctx." >&2
      echo "           Held by: $(_l2_lock_owner_desc "$dir")" >&2
      echo "           Two L2 suites against one cluster produce a red that points at the wrong" >&2
      echo "           artifact: a suite deriving its subject set from cluster state measures the" >&2
      echo "           other suite's fixtures too ([[LSN-066]]). Waiting is correct; so is failing." >&2
      return 1
    fi

    if [ "$waited" -eq 0 ]; then
      echo "  l2-lock: waiting for context $ctx — held by $(_l2_lock_owner_desc "$dir")" >&2
    fi
    sleep "$KAGE_L2_LOCK_POLL"
    waited=$((waited + KAGE_L2_LOCK_POLL))
  done
}

# l2_lock_release [context]
#   Idempotent. Releases ONLY a lock this process owns — a release that does not check the recorded
#   pid will happily delete the lock a stale-break just handed to somebody else, and the shape of
#   that bug is two suites running concurrently, which is the thing this file prevents.
l2_lock_release() {
  local dir="${1:-}" pid
  if [ -n "$dir" ]; then
    dir="$(l2_lock_path "$dir")"
  else
    dir="$_L2_LOCK_HELD_DIR"
  fi

  [ -n "$dir" ] || return 0
  [ -d "$dir" ] || {
    _L2_LOCK_HELD_DIR=""
    return 0
  }

  pid="$(_l2_lock_owner_pid "$dir")"
  if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
    echo "  l2-lock: REFUSED to release $dir — it is held by pid $pid, not by this process ($$)." >&2
    return 1
  fi

  rm -rf "$dir" 2>/dev/null || true
  [ "$dir" = "$_L2_LOCK_HELD_DIR" ] && _L2_LOCK_HELD_DIR=""
  echo "  l2-lock: released $dir"
  return 0
}

# _l2_lock_exit_handler — release, then run whatever EXIT trap the caller already had.
_l2_lock_exit_handler() {
  local rc=$?
  l2_lock_release || true
  # `trap -p EXIT` emits a re-usable shell command with bash's own '\'' escaping, so eval on the
  # extracted, still-quoted word restores the caller's command exactly. Parsing it by hand would
  # not survive a teardown function whose arguments contain quotes, and every L2 suite's teardown
  # does.
  if [ -n "$_L2_LOCK_PRIOR_EXIT" ]; then
    eval "$_L2_LOCK_PRIOR_EXIT" || true
  fi
  return $rc
}

# l2_lock_guard <context> [suite-name] [timeout-seconds]
#   Acquire, and release on EXIT without stomping the caller's existing EXIT trap. Exits the calling
#   shell on failure — a suite that could not take the lock must not go on to score the cluster.
#
#   ORDERING MATTERS AND IS NOT ENFORCEABLE FROM HERE: a caller that installs its own EXIT trap
#   AFTER this call replaces ours, and the lock is then released only by the next acquirer's stale
#   break. That degrades to a delay, never to two concurrent suites, which is why it is documented
#   rather than defended against. Call this first, or call l2_lock_acquire and put l2_lock_release
#   in your own trap.
l2_lock_guard() {
  local prior
  l2_lock_acquire "$@" || exit 1

  prior="$(trap -p EXIT 2>/dev/null)"
  if [ -n "$prior" ]; then
    prior="${prior#trap -- }"
    prior="${prior% EXIT}"
    _L2_LOCK_PRIOR_EXIT="$prior"
  fi
  trap '_l2_lock_exit_handler' EXIT
}

# -------------------------------------------------------------------------------------------------
# `--negative-control` — the mandatory offline ¬ arm (V-MET-014).
#
# Every row names the rule it exercises, in the row name AND in the reason, because a control that
# only proves the code went red proves almost nothing ([[LSN-035]], and
# dev/tests/negative-controls-name-their-rule.py enforces it). Nothing here touches a cluster: the
# lock root is a mktemp directory and every "holder" is a real local process.
# -------------------------------------------------------------------------------------------------

nc_total=0
nc_pass=0
nc_fail=0

nc_ok() {
  nc_total=$((nc_total + 1))
  nc_pass=$((nc_pass + 1))
  printf 'PASS: %-58s %s\n' "$1" "$2"
}

nc_bad() {
  nc_total=$((nc_total + 1))
  nc_fail=$((nc_fail + 1))
  printf 'FAIL: %-58s %s\n' "$1" "$2"
}

# A pid that is certainly not running, found rather than assumed — a hardcoded 99999 is a literal
# that expires the same way a literal line number does ([[LSN-063]]).
nc_dead_pid() {
  local p=99999
  while [ "$p" -gt 4096 ]; do
    if ! kill -0 "$p" 2>/dev/null; then
      printf '%s' "$p"
      return 0
    fi
    p=$((p - 1))
  done
  return 1
}

run_negative_control() {
  local root ctx_a ctx_b dir out rc dead sleeper

  root="$(mktemp -d "${TMPDIR:-/tmp}/l2-lock-nc.XXXXXX")" || return 1
  KAGE_L2_LOCK_ROOT="$root"
  KAGE_L2_LOCK_POLL=1
  ctx_a="gke-scratch-nc-alpha"
  ctx_b="gke-scratch-nc-beta"

  echo
  echo "-- THE EXPLICIT-CONTEXT RULE: a lock keyed on the ambient context guards the wrong cluster --"
  out="$(l2_lock_acquire "" nc-suite 1 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -q 'needs an explicit kubectl context'; then
    nc_ok "an-acquire-with-no-context-is-refused" "rc 2, and the refusal names the missing context"
  else
    nc_bad "an-acquire-with-no-context-is-refused" "wanted rc 2 naming the context; got rc $rc: $out"
  fi

  echo
  echo "-- THE MUTUAL-EXCLUSION RULE: the one LSN-066 is about --"
  out="$(l2_lock_acquire "$ctx_a" nc-holder 1 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ] && [ -d "$(l2_lock_path "$ctx_a")" ]; then
    nc_ok "a-free-lock-is-acquired" "rc 0 and the lock directory exists, so the rows below are not always-red"
  else
    nc_bad "a-free-lock-is-acquired" "wanted rc 0 and a lock directory; got rc $rc: $out"
  fi

  # A live holder that is NOT this process: a real background `sleep`, so `kill -0` genuinely
  # succeeds. Rewriting the owner file rather than forking an acquirer keeps the row deterministic.
  dir="$(l2_lock_path "$ctx_a")"
  sleep 30 &
  sleeper=$!
  printf '%s\t%s\t%s\t%s\n' "$sleeper" "nc-live-holder" "$ctx_a" "1970-01-01T00:00:00Z" >"$dir/owner"

  out="$(l2_lock_acquire "$ctx_a" nc-contender 2 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "pid $sleeper"; then
    nc_ok "a-second-suite-is-refused-not-admitted" "rc 1 after the timeout, and the refusal names pid $sleeper"
  else
    nc_bad "a-second-suite-is-refused-not-admitted" "wanted rc 1 naming pid $sleeper; got rc $rc: $out"
  fi

  echo
  echo "-- THE OWNERSHIP RULE: releasing a lock you do not hold re-creates the concurrency it prevents --"
  out="$(l2_lock_release "$ctx_a" 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'REFUSED to release'; then
    nc_ok "release-by-a-non-owner-is-refused" "rc 1, and the lock survives for its real holder"
  else
    nc_bad "release-by-a-non-owner-is-refused" "wanted rc 1 and a REFUSED message; got rc $rc: $out"
  fi

  echo
  echo "-- THE FAIL-SAFE RULE: a dead holder must not wedge the chain until a human notices --"
  kill "$sleeper" 2>/dev/null || true
  wait "$sleeper" 2>/dev/null || true
  dead="$(nc_dead_pid)" || {
    nc_bad "a-lock-held-by-a-dead-process-is-broken" "could not find a pid that is not running"
    dead=""
  }
  if [ -n "$dead" ]; then
    printf '%s\t%s\t%s\t%s\n' "$dead" "nc-dead-holder" "$ctx_a" "1970-01-01T00:00:00Z" >"$dir/owner"
    out="$(l2_lock_acquire "$ctx_a" nc-reclaimer 1 2>&1)" && rc=0 || rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'BROKE A STALE LOCK'; then
      nc_ok "a-lock-held-by-a-dead-process-is-broken" "rc 0, and the break is announced rather than silent"
    else
      nc_bad "a-lock-held-by-a-dead-process-is-broken" "wanted rc 0 and a BROKE A STALE LOCK line; got rc $rc: $out"
    fi
  fi

  echo
  echo "-- THE IDEMPOTENCE RULE: a teardown path that can fail is a teardown path that leaks locks --"
  l2_lock_release "$ctx_a" >/dev/null 2>&1 || true
  out="$(l2_lock_release "$ctx_a" 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then
    nc_ok "release-of-an-unheld-lock-is-a-no-op" "rc 0 on the second release, so a chained EXIT trap cannot fail here"
  else
    nc_bad "release-of-an-unheld-lock-is-a-no-op" "wanted rc 0; got rc $rc: $out"
  fi

  echo
  echo "-- THE KEYING RULE: suites against different clusters share no subject set --"
  l2_lock_acquire "$ctx_a" nc-alpha 1 >/dev/null 2>&1 || true
  out="$(l2_lock_acquire "$ctx_b" nc-beta 1 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then
    nc_ok "two-different-contexts-do-not-block-each-other" "rc 0 while $ctx_a is held, so the lock is per-cluster"
  else
    nc_bad "two-different-contexts-do-not-block-each-other" "wanted rc 0; got rc $rc: $out"
  fi

  # A holder is a holder regardless of the row above: the SAME context must still block.
  printf '%s\t%s\t%s\t%s\n' "$$" "nc-alpha" "$ctx_a" "1970-01-01T00:00:00Z" >"$(l2_lock_path "$ctx_a")/owner"
  sleep 30 &
  sleeper=$!
  printf '%s\t%s\t%s\t%s\n' "$sleeper" "nc-alpha" "$ctx_a" "1970-01-01T00:00:00Z" >"$(l2_lock_path "$ctx_a")/owner"
  out="$(l2_lock_acquire "$ctx_a" nc-alpha-again 2 2>&1)" && rc=0 || rc=$?
  if [ "$rc" -eq 1 ]; then
    nc_ok "the-same-context-still-blocks-after-the-keying-row" "rc 1, so the row above discriminates the KEY and not the lock"
  else
    nc_bad "the-same-context-still-blocks-after-the-keying-row" "wanted rc 1; got rc $rc: $out"
  fi
  kill "$sleeper" 2>/dev/null || true
  wait "$sleeper" 2>/dev/null || true

  rm -rf "$root"

  echo
  echo "negative control: $nc_pass/$nc_total rows scored as expected, $nc_fail wrong"
  [ "$nc_fail" -eq 0 ] || return 1
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -uo pipefail
  case "${1:-}" in
    --negative-control)
      echo "l2-lock.sh --negative-control — the offline ¬ for the LSN-066 mutual-exclusion property"
      run_negative_control
      exit $?
      ;;
    *)
      echo "l2-lock.sh is a library. Source it, or run: bash dev/lib/l2-lock.sh --negative-control" >&2
      exit 2
      ;;
  esac
fi
