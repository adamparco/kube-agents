"""Build-time behaviour gate for the report-back completion patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_report_back_completion.py``. The applier proves the
anchors matched and the file still parses; this proves the patched code does
what the patch exists to do. A failure here fails the image build.

The unit suite in ``test_report_back_completion.py`` covers the predicates in
isolation. What it cannot cover is the in-place edit — whether the gate is
actually reached inside ``_handle_complete``, ahead of the write, with the real
card's title and body, and whether a rejection genuinely leaves the card
unwritten. That last part is the whole promise of the rejection message ("your
task is still in-flight"), and it is only true because the gate sits before
``kb.complete_task``. So the fake kernel below records every write and the
checks assert on what it did and did not receive.
"""

from __future__ import annotations

import json
import os
import sys
import types

CARD = "t_7f3e0a5e"
SESSION = "20260805_223456_eeda96"
PROFILE = "platform"      # HERMES_PROFILE — how kanban_comment stamps an author
REQUESTER = "default"     # the Chat Agent, relaying the user's ask

TITLE = "List enabled cron jobs and scheduled audits"
BODY = (
    "Please list all enabled cron jobs, scheduled audits, and background "
    "tasks configured across the platform, the GitOps repository, or GKE "
    "clusters. Report back with their schedules, targets, and active states."
)
SUMMARY = (
    "Successfully audited and cataloged all platform cron jobs, scheduled "
    "audits, background tasks, GitOps declarations, and GKE controller "
    "states. Provided a detailed manifest mapping schedules, targets, active "
    "states, and recent execution statuses."
)
ANSWER = (
    "Active platform cron jobs (6 of 11 enabled):\n"
    "- compliance-audit — 20 6 * * * — Security & RBAC Posture Audit\n"
    "- obtainability-audit — 50 6 * * * — Workload Reliability Audit\n"
    "- github-issue-resolver — */30 * * * * — GitHub Issue Resolver"
)

failures: list[str] = []
writes: list[dict] = []
comment_reads: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok  {label}")


class _Err(Exception):
    """Stand-in for the kernel's structured completion errors."""


def _fake_kernel(task, comments=(), comments_raise=False):
    """A kanban kernel that records writes instead of performing them.

    ``comments`` is a sequence of ``(author, body)`` pairs — the real rows
    carry an author (``kanban_tools._handle_comment`` stamps it from
    ``HERMES_PROFILE``) and the gate filters on it.
    """

    def get_task(conn, tid):
        return task

    def complete_task(conn, tid, **kw):
        writes.append(dict(kw, task_id=tid))
        return True

    def list_comments(conn, tid):
        comment_reads.append(tid)
        if comments_raise:
            raise RuntimeError("comment table is on fire")
        return [types.SimpleNamespace(author=a, body=b) for a, b in comments]

    return types.SimpleNamespace(
        get_task=get_task,
        complete_task=complete_task,
        list_comments=list_comments,
        latest_run=lambda conn, tid: types.SimpleNamespace(id=46),
        ArtifactPreservationError=_Err,
        HallucinatedCardsError=_Err,
    )


def _card(title=TITLE, body=BODY):
    return types.SimpleNamespace(id=CARD, title=title, body=body, goal_mode=0)


def main() -> int:
    # The environment a dispatched kanban worker runs in.
    os.environ["HERMES_KANBAN_TASK"] = CARD
    os.environ["HERMES_SESSION_ID"] = SESSION
    os.environ["HERMES_PROFILE"] = PROFILE

    import tools.kanban_tools as kt

    def run(args, task=None, comments=(), comments_raise=False):
        """Call the real _handle_complete against a recording kernel."""
        del writes[:], comment_reads[:]
        kernel = _fake_kernel(task or _card(), comments, comments_raise)
        kt._connect = lambda board=None: (kernel, types.SimpleNamespace(close=lambda: None))
        return kt._handle_complete(dict(args, task_id=CARD))

    # --- the completion that shipped on 2026-08-05 --------------------------
    out = run({"summary": SUMMARY})
    check("the incident completion is refused", "error" in out.lower(), True)
    check("it names the blocked tool", "kanban_complete blocked" in out, True)
    check("it points at result", "`result`" in out, True)
    # The rejection promises the card is untouched. It has to be true.
    check("the card was not written", writes, [])
    check("the comments were consulted", comment_reads, [CARD])

    # --- the retry that should have happened --------------------------------
    out = run({"summary": SUMMARY, "result": ANSWER})
    check("the retry is accepted", json.loads(out).get("ok"), True)
    check("exactly one write", len(writes), 1)
    check("the answer reached the card", writes[0].get("result"), ANSWER)
    check("the summary rode along", writes[0].get("summary"), SUMMARY)
    # No cheaper check failed, so nothing should have paid for a comment read.
    check("no comment query on the happy path", comment_reads, [])

    # --- the other half of the rule: the card asked, the handoff is silent ---
    # Nothing here claims a deliverable, so only requests_report(title, body)
    # can fire — which is the only check that proves task.title and task.body
    # are wired through to the gate at all.
    out = run({"summary": "Checked every namespace. Done."})
    check("an ask with a claimless handoff is refused", "kanban_complete blocked" in out, True)
    check("that card was not written either", writes, [])

    # --- content already on the card ----------------------------------------
    out = run({"summary": SUMMARY}, comments=[(PROFILE, "chatter"), (PROFILE, ANSWER)])
    check("a comment carrying the report is accepted", json.loads(out).get("ok"), True)
    check("the card was written", len(writes), 1)

    # The requester's own comment relays the ask, often as a list. It is not
    # the answer, and must not be mistaken for one.
    out = run({"summary": SUMMARY}, comments=[(REQUESTER, ANSWER)])
    check("a comment from the requester does not count", "kanban_complete blocked" in out, True)
    check("and the card stays unwritten", writes, [])

    out = run(
        {"summary": SUMMARY, "artifacts": ["/tmp/cron-manifest.md"]},
    )
    check("a declared artifact is accepted", json.loads(out).get("ok"), True)

    # --- ordinary work is untouched ------------------------------------------
    out = run(
        {"summary": "Fixed by widening the timeout to 30s."},
        task=_card(title="Fix the flaky checkout test", body="Fails 1 run in 5."),
    )
    check("a fix card still completes", json.loads(out).get("ok"), True)
    check("no comment query for a card that asked nothing", comment_reads, [])

    # --- a broken comment read must not crash the completion -----------------
    # Without the try/except around list_comments this raises out of
    # _handle_complete and the script dies here, so reaching either check at
    # all is the assertion. The handler logs the failure with a traceback, so
    # one appears in the build output. It is the guard working, not the build
    # breaking.
    print("  (expect one logged traceback below — the comment read is meant to fail)")
    out = run({"summary": SUMMARY}, comments_raise=True)
    check(
        "a failed comment read still refuses, with the guidance intact",
        "kanban_complete blocked" in out,
        True,
    )
    check("and the card was not written", writes, [])

    # --- the schema no longer steers the model away from result --------------
    prop = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]["result"]
    desc = prop["description"]
    check("result is no longer 'legacy'", "legacy field" in desc, False)
    check("result is described as the deliverable", "deliverable" in desc, True)
    check(
        "the tool description names result",
        "the content belongs in ``result``"
        in kt.KANBAN_COMPLETE_SCHEMA["description"],
        True,
    )

    if failures:
        print("\nVERIFY FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nreport_back_completion verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
