#!/usr/bin/env bash
# Snapshot files, run a command against them, always put them back.
#
#   dev/mutate.sh FILE [FILE...] -- COMMAND [ARGS...]
#
# For mutation-testing a check: break something on purpose, confirm the check notices, restore.
# The restore is a byte copy of what was there when this script started — NOT `git checkout`,
# NOT `git restore`, NOT `git stash`.
#
# Why that distinction is the whole point. On 2026-07-25 a mutation test was reverted with
# `git checkout <path>`. Those paths held two hours of finished, unstaged work; `git checkout`
# restores from the index, the index was HEAD, and the work was gone with no reflog entry and no
# stash to recover it. It was noticed only because the next mutation's output named three fields
# instead of the one that had been mutated. A git-shaped revert asks git what the file should
# contain; the only correct answer here is "what it contained a moment ago", and git does not
# know that for changes it has never seen.
#
# Restores on success, on failure, and on SIGINT/SIGTERM. The command's exit code is passed
# through, so this composes: `dev/mutate.sh f -- sh -c 'mutate && ! check'`.
#
# THIS IS THE LAYER BELOW A SWEEP, and it cannot see any of LSN-047/048/049. It does not know what
# your mutation was, whether it landed, or which test was supposed to catch it — it only guarantees
# the files come back. A sweep that scores mutants belongs in `dev/mutate.py`, driven by a spec
# under `verification/mutants/<CHECK-ID>.json`; `harness-run` §5 requires it.
#
# Even for a one-off, keep the mutation out of the shell. On 2026-07-29 a needle containing
# backticks was pasted into an unquoted heredoc inside `-- sh -c '...'`; the backticks ran as
# command substitution, the needle never matched, and the check under test reported ✓ — a
# fabricated ESCAPE from a mutation that was never applied. Write the mutator to a file, and have
# it assert the edit landed before running anything.
set -uo pipefail

usage() {
  echo "usage: dev/mutate.sh FILE [FILE...] -- COMMAND [ARGS...]" >&2
  exit 2
}

files=()
while [ $# -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    -h|--help) usage ;;
    *) files+=("$1"); shift ;;
  esac
done

[ ${#files[@]} -gt 0 ] || usage
[ $# -gt 0 ] || usage

for f in "${files[@]}"; do
  if [ ! -f "$f" ]; then
    echo "mutate: no such file: $f" >&2
    echo "  Refusing to run. A snapshot of a file that does not exist restores nothing, and the" >&2
    echo "  command would mutate a path this script has no copy of." >&2
    exit 2
  fi
done

snap="$(mktemp -d)"
n=0
for f in "${files[@]}"; do
  cp -p "$f" "$snap/$n" || { echo "mutate: could not snapshot $f" >&2; exit 2; }
  n=$((n + 1))
done

restored=0
restore() {
  [ "$restored" -eq 1 ] && return 0 # the signal path restores, then EXIT fires too
  restored=1
  local i=0 f
  for f in "${files[@]}"; do
    # `cp` back onto the original path: same inode target, same permissions, no index consulted.
    cp -p "$snap/$i" "$f" 2>/dev/null || echo "mutate: FAILED to restore $f from $snap/$i" >&2
    i=$((i + 1))
  done
  rm -rf "$snap"
}

child=""
on_signal() {
  # Kill the command and WAIT for it before restoring: a command still running while we copy
  # files back would win the race and leave the mutation in place with the script reporting a
  # clean restore. Signal the whole process GROUP (`-$child`), not just the command — killing
  # `sh -c 'mutate; check'` leaves whatever it spawned orphaned and still holding the file open,
  # which is the same race one level down. Falls back to the bare pid if the group is gone.
  if [ -n "$child" ]; then
    kill -"$1" -"$child" 2>/dev/null || kill -"$1" "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  restore
  exit "$2"
}
trap 'restore' EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

# Backgrounded deliberately. bash runs a trap only when the foreground command returns, so with
# `"$@"` on its own an interrupted mutation test — Ctrl-C, a timeout, a killed session — would
# hang until the command finished and leave the tree mutated in the meantime. `wait` is
# interruptible; a foreground command is not. fd 3 carries stdin across, because a background
# job in a non-interactive shell otherwise gets /dev/null. `set -m` for the launch alone gives
# the command its own process group, so on_signal can reach its descendants.
exec 3<&0
set -m
"$@" <&3 &
child=$!
set +m
exec 3<&-
wait "$child"
rc=$?

# The EXIT trap does the restoring; exiting here fires it.
exit "$rc"
