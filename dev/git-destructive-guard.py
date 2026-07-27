#!/usr/bin/env python3
"""Refuse a tree-destroying git command while the working tree holds work git has never seen.

WHY THIS EXISTS (LSN-030, and LSN-022 one level up).

`git reset --hard origin/main` was typed at the tail of a compound one-liner whose earlier
half — `git checkout main` — had already ABORTED on the dirty tree and said so. `reset --hard`
does not abort. It discarded seven modified files that had been written, verified against a
live cluster, and not yet staged; `git fsck` found nothing, because git had never been shown
those bytes. The work was reconstructed from the session transcript, which is luck, not a
recovery path.

LSN-022 recorded the same failure for `git checkout <path>` and mechanized it as `dev/mutate.sh`,
a primitive scoped to mutation tests. The lesson generalized correctly and the mechanization did
not: the next instance arrived through a different verb, in an ad-hoc shell line, nowhere near a
mutation test. So this guard is scoped to the ACT rather than to the caller — any Bash command
this agent runs, at any point, for any reason.

WHAT IT REFUSES, AND WHAT IT DELIBERATELY DOES NOT.

Refused only when `git status --porcelain` reports uncommitted work:

    git reset --hard ...        discards tracked modifications, silently, with no reflog for them
    git checkout -- <path>      restores from the INDEX, which is not where your edit lives
    git checkout <ref> -- <p>   same, from a commit
    git restore <path>          the modern spelling of the same operation
    git clean -f / -fd / -fx    deletes untracked files outright
    git stash drop / clear      throws away the one copy a stash was holding

Not refused, because git already protects the tree itself: `git checkout <branch>`,
`git switch`, `git merge`, `git rebase`, `git pull` — each aborts rather than overwriting a
dirty tree. That distinction IS the lesson: the dangerous verbs are precisely the ones whose
whole purpose is to overwrite, so they cannot warn you.

A clean tree refuses nothing. This never blocks a legitimate reset on a tree with nothing to
lose, which is the common case, and is why it is safe to leave on permanently.

USAGE
    As a Claude Code PreToolUse hook on Bash (see `.claude/settings.json`): reads the hook JSON
    on stdin and exits 2 with the reason on stderr to block. Exit 0 allows.
    Standalone:  echo '<command>' | dev/git-destructive-guard.py --command-stdin
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

# Splitting on the shell operators that separate commands. Deliberately naive about quoting: the
# cost of a false positive is one explanatory refusal, and the cost of a false negative is the
# incident this file is named after. `git reset --hard` inside a quoted string is not a command
# anyone writes by accident.
_SEPARATORS = re.compile(r"(?:\|\||&&|[;\n|&])")

# A heredoc body is DATA — the shell never executes it. Skipping it is not a weakening, and it is
# not hypothetical: the first command this guard ever blocked was the heredoc that appended
# LSN-030's own write-up to LESSONS.md, whose prose quotes the incident command verbatim. A guard
# that cannot let you write down the thing it guards against gets switched off.
_HEREDOC = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")


def strip_heredocs(line: str) -> str:
    """Remove heredoc bodies, keeping the header line that introduced them."""
    out, lines, i = [], line.split("\n"), 0
    while i < len(lines):
        cur = lines[i]
        out.append(cur)
        i += 1
        # One line can open several (`cmd <<A <<B`); bash closes them in order.
        for delim in _HEREDOC.findall(cur):
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # consume the terminator itself
    return "\n".join(out)


def split_commands(line: str) -> list[str]:
    """Split a shell line into candidate commands. The instance that cost the work was the THIRD
    segment of a `;`-joined line whose first segment had already failed."""
    return [seg.strip() for seg in _SEPARATORS.split(strip_heredocs(line)) if seg.strip()]


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:  # unbalanced quotes — fall back to whitespace, never to "allow"
        return cmd.split()


def destructive_reason(cmd: str) -> str | None:
    """Name the destructive operation in `cmd`, or None. One sentence, in the caller's terms."""
    tok = _tokens(cmd)
    # Skip a leading env-assignment or `sudo`-style prefix so `FOO=bar git reset --hard` is seen.
    while tok and ("=" in tok[0] and not tok[0].startswith("-") or tok[0] in ("sudo", "command", "time")):
        tok = tok[1:]
    if len(tok) < 2 or os.path.basename(tok[0]) != "git":
        return None
    # Global flags (`git -C <dir> reset --hard`) sit between `git` and the subcommand.
    i = 1
    while i < len(tok) and tok[i].startswith("-"):
        i += 2 if tok[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    if i >= len(tok):
        return None
    sub, rest = tok[i], tok[i + 1 :]

    if sub == "reset" and "--hard" in rest:
        return "`git reset --hard` overwrites every tracked file from the target commit"
    if sub == "restore" and any(not a.startswith("-") for a in rest):
        return "`git restore <path>` overwrites the working file from the index or a commit"
    if sub == "checkout":
        # Path form: an explicit `--`, or a `-- <path>` implied by naming a file that exists.
        if "--" in rest:
            return "`git checkout -- <path>` restores from the index, not from your edit"
        if any(not a.startswith("-") and os.path.exists(a) for a in rest):
            return "`git checkout <path>` restores from the index, not from your edit"
        return None  # branch form: git aborts on a dirty tree by itself
    if sub == "clean" and any(a.startswith("-") and ("f" in a or a == "--force") for a in rest):
        return "`git clean -f` deletes untracked files outright"
    if sub == "stash" and rest and rest[0] in ("drop", "clear"):
        return f"`git stash {rest[0]}` discards stashed work with no reflog entry for it"
    return None


def uncommitted(cwd: str) -> list[str]:
    """Porcelain lines for work that would be lost. Ignored files are not reported by
    --porcelain without -uall/--ignored, so this is exactly the at-risk set."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []  # not a repo, or git unavailable: nothing to protect, nothing to claim
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def verdict(command: str, cwd: str) -> str | None:
    """The refusal message, or None to allow."""
    for cmd in split_commands(command):
        reason = destructive_reason(cmd)
        if not reason:
            continue
        dirty = uncommitted(cwd)
        if not dirty:
            return None  # nothing to lose — this is the common case and it stays fast
        listing = "\n".join("      " + d for d in dirty[:20])
        more = f"\n      … and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        return (
            f"REFUSED (LSN-030): {reason}, and this tree has uncommitted work:\n"
            f"{listing}{more}\n"
            f"    In `{cmd}`.\n"
            "    Those bytes are in no commit, no index, and no reflog, so there is no recovery\n"
            "    path afterwards — LSN-022 cost two hours this way and LSN-030 cost seven files.\n"
            "    Snapshot first, then re-run:\n"
            "        git stash push -u -m pre-destructive\n"
            "    or commit the work on a branch. If the loss is genuinely intended, say so by\n"
            "    stashing anyway: a stash you never pop costs nothing."
        )
    return None


def main(argv: list[str]) -> int:
    if "--command-stdin" in argv:
        command, cwd = sys.stdin.read(), os.getcwd()
    else:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return 0  # a hook that cannot parse its input must not block the session
        if payload.get("tool_name") not in (None, "Bash"):
            return 0
        command = (payload.get("tool_input") or {}).get("command", "")
        cwd = payload.get("cwd") or os.getcwd()
    message = verdict(command or "", cwd)
    if message:
        print(message, file=sys.stderr)
        return 2  # PreToolUse: block, and feed stderr back to the model
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
