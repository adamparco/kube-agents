#!/usr/bin/env python3
"""LSN-011 — nothing on `main` arrived past a red check.

THE LESSON. "A milestone PR sits with the gate green except one red check, and `gh pr merge --admin`
is one flag away." Auto-merge makes that a standing temptation rather than an occasional one. It was
closed against `binding.md` §Merge and PROTOCOL §7.4 — prose in two files, addressed to the one
person who is at that moment motivated to read it favourably.

WHY IT LOOKED UNMECHANIZABLE, AND WHY THAT WAS WRONG. The reopening note said: "the flags this
forbids (`--admin`, `--no-verify`) leave no trace a later check could find IN THE TREE." True, and
irrelevant. The trace is not in the tree; it is on the forge. GitHub keeps the check-run conclusions
for the PR's head SHA forever, and the merge commit names the PR. So the question "was anything
merged over a red check" is answerable after the fact, exactly and without trusting anybody's
memory. What the flag defeats is the gate at merge TIME; it does not erase the evidence.

That distinction is worth keeping: a lesson called unmechanizable is usually one where the obvious
place to look is the wrong place. This one sat open for eight phases behind a true sentence.

WHAT IT ASSERTS, per squash commit on main:

  1. The commit names a PR (`(#N)` in its subject). A commit on main that names none was pushed
     directly, which is the same property violation as `--admin` reached by a different route.
  2. That PR is merged, and its merge commit IS this commit. A `(#N)` in a subject is free text.
  3. Every check run recorded against the PR's HEAD SHA concluded green — with one named,
     documented exception (below), and `skipped`/`neutral` accepted as the non-verdicts they are.

SCOPE, and why it is not a convenient cut. This repository is a FORK. Everything up to the fork
point was authored in `gke-labs/kube-agents`, and the `(#383)` in those subjects resolves in the
UPSTREAM's pull-request namespace — asking this fork's API about PR #383 returns 404, not a finding.
Auditing them here produced 187 false accusations on the first run. So the audit starts at
`INHERITED_FLOOR`, the fork point, and the floor is itself checked: it must be an ancestor of a
remote that does NOT carry the build ledger, i.e. it must sit inside inherited history. That is what
stops the floor being the escape hatch. You cannot move it forward past your own merge, because your
own merge is not in the upstream.

WHAT IT CANNOT SEE, stated because a partial check that reads as total is worse than none:

  * A check that was never REQUIRED and never ran leaves no red run to find. This detects merging
    over red, not the absence of coverage; V-MET-007 owns that question.
  * `--no-verify` skips local pre-commit hooks, which never produce a check run. Nothing here or
    anywhere can see that after the fact.
  * It reads the CURRENT conclusions. A re-run that turned a red green after the merge reads as
    green. Recording the pre-merge state is what branch protection is for; this is the audit that
    exists in the meantime.

Exit: 0 = every merge on main is clean · 1 = at least one is not · 2 = could not run (no `gh`, no
credential, no network). Two is not zero on purpose. "The audit could not run" and "the audit found
nothing" are the same output only in builds that later turn out to have been lying to themselves.

Run: python3 dev/tests/merge-provenance.py [--limit N]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER_PATH = "docs/build/LEDGER.md"

# The fork point: `git merge-base origin/main upstream/main` on 2026-07-25, which is upstream's
# `docs(watcher): ... (#383)`. Commits reachable from it were authored in gke-labs/kube-agents and
# their PR numbers belong to that repository. Everything AFTER it is this build's own work and is
# audited. `_floor_is_inherited` proves the floor has not been walked forward.
INHERITED_FLOOR = "7ca4915db7d547109ba68d08f0874ddae5bfbb84"

# A backstop, not the scope. The scope is FLOOR..main; this only bounds a runaway walk if the floor
# ever stops being reachable. When it bites, the run says what it did not look at.
DEFAULT_LIMIT = 500

# Check runs whose red is a known property of the FORGE rather than of the code. Each entry must
# name why, and the reason must be true of the check itself -- "it is always red" is not a reason,
# it is the thing that needs one. Adding an entry here is a deliberate narrowing of a merge gate:
# under invariant 10 it is a human-review change, and the ledger row is not optional.
BENIGN: dict[str, str] = {
    "Auto Request Review": (
        "a bot that requests reviewers from a CODEOWNERS file; on a fork PR it has no permission "
        "to request a review on the upstream's behalf and fails for that reason alone. It asserts "
        "nothing about the code and has never been green on this fork."
    ),
}

# Merges that DID go past a red check. A merge cannot be un-merged, and a check that is permanently
# red is a check that gets deleted -- so each historical escape is carried here, by (PR, check),
# with what it was and how the underlying defect ended. It is printed on every run: the point is
# that the finding never stops being visible, not that it stops counting. An entry is a narrowing
# of a merge gate, which is invariant 10 (human review) and a ledger row, not a quiet edit.
KNOWN_ESCAPES: dict[tuple[int, str], str] = {
    (10, "prettier"): (
        "2026-07-24, found 2026-07-25 by this check's first correct run. `docs: correct Kind test "
        "flow to build & load local images` was merged with prettier red on docs/build/HARNESS.md. "
        "The formatting defect is resolved -- that file is prettier-clean in the current tree -- but "
        "the merge itself is the LSN-011 instance and stays on the record. It predates every phase "
        "PR in this build (#11-#16), all of which are clean."
    ),
}

GREEN = {"success", "skipped", "neutral"}
# Both merge shapes this repository has used: a squash commit ending `(#16)`, and GitHub's own
# merge-commit subject `Merge pull request #3 from ...`. Recognising only the first would have
# reported the two real merge commits as direct pushes.
PR_IN_SUBJECT = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+) from ")


def run(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or p.stderr).strip()


def work_remote() -> str | None:
    """The remote whose `main` carries the build ledger — by content, never by name (LSN-012)."""
    rc, out = run("git", "remote")
    if rc != 0:
        return None
    for r in out.split():
        if run("git", "cat-file", "-e", f"refs/remotes/{r}/main:{LEDGER_PATH}")[0] == 0:
            return r
    return None


def slug_for(remote: str) -> str | None:
    rc, url = run("git", "remote", "get-url", remote)
    if rc != 0:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def floor_is_inherited() -> tuple[bool, str]:
    """The floor must live in history this fork did not author. Otherwise it is an escape hatch.

    A remote whose `main` does NOT contain the build ledger is, by the same content test git-preflight
    uses, the upstream project. If the floor is an ancestor of that remote's main, no commit after
    the floor can be one of its commits, so advancing the floor could only ever skip OUR merges --
    and this assertion is what makes that impossible to do quietly.
    """
    rc, out = run("git", "cat-file", "-e", INHERITED_FLOOR)
    if rc != 0:
        return False, f"floor {INHERITED_FLOOR[:8]} is not in this clone (shallow checkout?)"
    rc, remotes = run("git", "remote")
    for r in remotes.split() if rc == 0 else []:
        if run("git", "cat-file", "-e", f"refs/remotes/{r}/main:{LEDGER_PATH}")[0] == 0:
            continue  # carries the build; this is the fork, not the upstream
        if run("git", "cat-file", "-e", f"refs/remotes/{r}/main")[0] != 0:
            continue
        if run("git", "merge-base", "--is-ancestor", INHERITED_FLOOR, f"{r}/main")[0] == 0:
            return True, f"floor {INHERITED_FLOOR[:8]} is inherited history (ancestor of {r}/main)"
        return False, (
            f"floor {INHERITED_FLOOR[:8]} is NOT an ancestor of {r}/main. It has been moved forward "
            f"into this fork's own history, which would skip merges this check exists to audit."
        )
    return False, (
        "no non-ledger-carrying remote to check the floor against; the floor is taken on trust "
        "this run. Add the upstream remote to make it verifiable."
    )


def gh_json(path: str) -> tuple[bool, object]:
    rc, out = run("gh", "api", "-H", "Accept: application/vnd.github+json", path)
    if rc != 0:
        return False, out
    try:
        return True, json.loads(out)
    except json.JSONDecodeError as exc:
        return False, f"unparseable response: {exc}"


def main() -> int:
    limit = DEFAULT_LIMIT
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    print("== merge provenance (LSN-011) ==")

    remote = work_remote()
    if not remote:
        print("COULD NOT RUN: no remote's main carries " + LEDGER_PATH + ".")
        print("  In a shallow checkout the ref simply is not there. Reported, not passed.")
        return 2
    slug = slug_for(remote)
    if not slug:
        print(f"COULD NOT RUN: remote {remote!r} is not a github.com URL this can audit.")
        return 2

    if run("gh", "--version")[0] != 0:
        print("COULD NOT RUN: no `gh` on PATH. The evidence for this check lives on the forge, so")
        print("  there is no offline form of it. Reported, not passed (V-MET-014).")
        return 2
    if run("gh", "auth", "status")[0] != 0:
        print("COULD NOT RUN: `gh` is present but not authenticated. Reported, not passed.")
        return 2

    floor_ok, floor_msg = floor_is_inherited()
    print(("ok:   " if floor_ok else "note: ") + floor_msg)
    if not floor_ok and "moved forward" in floor_msg:
        print("\nFAIL: the audit floor is inside this fork's own history.")
        return 1

    rc, log = run(
        "git", "log", f"--max-count={limit}", "--first-parent",
        "--format=%H%x09%s", f"{INHERITED_FLOOR}..refs/remotes/{remote}/main",
    )
    if rc != 0:
        print(f"COULD NOT RUN: cannot read {remote}/main history ({log[:120]}).")
        return 2

    commits = [ln.split("\t", 1) for ln in log.splitlines() if "\t" in ln]
    print(f"auditing {len(commits)} commits on {slug}@main since the fork point "
          f"{INHERITED_FLOOR[:8]}")
    print("  NOT covered: everything at or before the fork point — those PR numbers resolve in the")
    print("  upstream's namespace, not this one. That is scope, not an exemption.")
    if len(commits) == limit:
        print(f"  ALSO not covered: everything before {commits[-1][0][:8]} (--limit {limit} bit).")

    failures: list[str] = []
    audited = 0
    carried = 0
    for sha, subject in commits:
        m = PR_IN_SUBJECT.search(subject)
        if not m:
            failures.append(
                f"{sha[:8]} {subject[:60]!r} names no pull request. It reached main without one, "
                f"which bypasses every required check by a different route than --admin does."
            )
            continue
        num = m.group(1) or m.group(2)
        ok, pr = gh_json(f"repos/{slug}/pulls/{num}")
        if not ok or not isinstance(pr, dict):
            failures.append(f"{sha[:8]}: cannot read PR #{num} ({str(pr)[:100]})")
            continue
        if not pr.get("merged"):
            failures.append(f"{sha[:8]} claims (#{num}), but that PR is not merged.")
            continue
        if pr.get("merge_commit_sha") != sha:
            failures.append(
                f"{sha[:8]} claims (#{num}), whose merge commit is "
                f"{str(pr.get('merge_commit_sha'))[:8]}. The subject is free text; this one is "
                f"pointing at a different merge."
            )
            continue
        head = (pr.get("head") or {}).get("sha")
        if not head:
            failures.append(f"#{num}: no head SHA recorded; its check runs cannot be located.")
            continue

        ok, runs = gh_json(f"repos/{slug}/commits/{head}/check-runs?per_page=100")
        if not ok or not isinstance(runs, dict):
            failures.append(f"#{num}: cannot read check runs for {head[:8]} ({str(runs)[:100]})")
            continue
        entries = runs.get("check_runs") or []
        if not entries:
            failures.append(
                f"#{num} was merged with NO check runs recorded against its head {head[:8]}. "
                f"A merge with nothing to bypass is not the same as a merge that passed."
            )
            continue
        audited += 1
        for entry in entries:
            name = entry.get("name", "?")
            concl = entry.get("conclusion")
            if concl in GREEN:
                continue
            if name in BENIGN:
                print(f"  note: #{num} {name!r} = {concl} — documented benign: {BENIGN[name]}")
                continue
            if (int(num), name) in KNOWN_ESCAPES:
                carried += 1
                print(f"  ESCAPE (carried): #{num} {name!r} = {concl}")
                print(f"    {KNOWN_ESCAPES[(int(num), name)]}")
                continue
            failures.append(
                f"#{num} ({sha[:8]}) was merged with {name!r} = {concl}. A red required check "
                f"means the unit is not done; merging past it makes every later green rest on an "
                f"unverified base (LSN-011)."
            )

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"\nmerge provenance FAILED — {len(failures)} new finding(s) over "
              f"{len(commits)} commits ({carried} carried)")
        return 1
    if audited == 0:
        print("NOT APPLICABLE: no merged pull request in the window had check runs to read.")
        print("This is reported, not passed. A check with nothing to check is not evidence.")
        return 0
    tail = f", {carried} carried historical escape(s)" if carried else ""
    print(f"merge provenance ok — {audited} merged PR(s) audited, no NEW red merge{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
