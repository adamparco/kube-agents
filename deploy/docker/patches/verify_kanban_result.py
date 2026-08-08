#!/usr/bin/env python3
"""Build gate for the kanban result capture/delivery patches.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after both appliers.
The appliers only prove their anchors matched; this exercises the patched code
in the image the way the notifier and a worker actually reach it.

It is deliberately behavioural rather than textual. The regression that
motivated the whole change was not a failed edit — it was a field the model was
told not to use, so the checks that matter are "does a worker get refused when
it drops the answer" and "does the answer reach the message". Both are asserted
against the live, patched modules.

Usage::

    cd /opt/hermes && python3 verify_kanban_result.py
"""

from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# --- 1. The tool schema a worker actually reads -----------------------------
print("kanban_complete schema:")
from tools.kanban_tools import KANBAN_COMPLETE_SCHEMA as SCHEMA  # noqa: E402

props = SCHEMA["parameters"]["properties"]
blob = repr(SCHEMA)

check("result is a required parameter", "result" in SCHEMA["parameters"]["required"])
check(
    "result is described as REQUIRED",
    "REQUIRED" in props["result"]["description"],
)
check(
    "result no longer described as a legacy log line",
    "legacy field" not in blob,
    "upstream's discouraging wording survived the patch",
)
check(
    "summary no longer invites a 1-3 sentence report",
    "1-3 sentence" not in blob,
)
check(
    "summary states the real 400-character single-line cut",
    "400" in props["summary"]["description"]
    and "FIRST LINE" in props["summary"]["description"],
)
check(
    "the tool description's tail is preserved",
    "created_cards" in SCHEMA["description"] and "artifacts" in SCHEMA["description"],
    "the description was replaced wholesale instead of having its head swapped",
)

# --- 2. The gate ------------------------------------------------------------
print("require_result gate:")
import tools.kanban_result_required as krr  # noqa: E402

krr._refused_at.clear()
err, out = krr.require_result("t_verify", "a status line", None)
check("a completion with no result is refused", err is not None)
check("the refusal names the field", err and "result" in err)
err2, out2 = krr.require_result("t_verify", "a status line", "the answer")
check("the retry carrying a result is accepted", err2 is None and out2 == "the answer")
check(
    "the answered refusal is forgotten",
    krr._refused_at == {},
    "the gate keeps a ledger of every card it ever refused, so a card reopened "
    "and retried in this process would be closed empty in silence",
)
err2b, _ = krr.require_result("t_verify", "a status line", None)
check("a card reopened after a refusal is nudged again", err2b is not None)

krr._refused_at.clear()
krr.require_result("t_wedge", "a status line", None)
err3, out3 = krr.require_result("t_wedge", "a status line", None)
check(
    "a card is never wedged shut by the gate",
    err3 is None,
    "a second empty completion was still refused",
)
check("summary is promoted so the card carries something", out3 == "a status line")

krr._refused_at.clear()
err4, _ = krr.require_result("t_ok", "s", "  \n ")
check("a whitespace-only result counts as empty", err4 is not None)

krr._refused_at.clear()
krr.require_result("t_blank", None, "  \n ")
_, blank_out = krr.require_result("t_blank", None, "  \n ")
check(
    "a blank result is never handed back as the value to store",
    blank_out is None,
    "complete_task indexes line zero of the stripped value inside write_txn, so "
    "whitespace rolls the completion back with IndexError and the card, whose "
    "nudge is already spent, fails the same way on every retry",
)
krr._refused_at.clear()

_kanban_tools_src = open("tools/kanban_tools.py").read()
check(
    "the gate is wired into the completion handler",
    "_require_result(tid, summary, result)" in _kanban_tools_src,
)
check(
    "the handler's own summary is folded before it reaches complete_task",
    "summary = _blank_to_none(summary)" in _kanban_tools_src,
    "a whitespace-only summary still wedges the card however good the result is",
)

# --- 3. Delivery ------------------------------------------------------------
print("result delivery:")
import gateway.kanban_watchers as watchers  # noqa: E402
from gateway.kanban_handoff_clip import DEFAULT_LIMIT, clip_handoff  # noqa: E402
from gateway.kanban_result_delivery import RESULT_LIMIT, result_block  # noqa: E402

check(
    "the notifier resolved the delivery import",
    hasattr(watchers, "_kanban_handoff_with_result"),
    "the trailer import did not execute",
)
check(
    "the completion message's tail is built by the patch",
    "handoff = _kanban_handoff_with_result(handoff, task)"
    in open("gateway/kanban_watchers.py").read(),
    "an appending hook cannot drop the clip the notifier already built",
)

catalogue = "\n".join(f"{i}. cron-job-{i} — 0 {i} * * *" for i in range(1, 10))
block = result_block("Cataloged all 9 cron jobs.", catalogue)
check("a multi-line result survives whole", block.count("\n") >= 9)
check("the last line is delivered", "cron-job-9" in block)
check(
    "a result already shown in the status line is not repeated",
    result_block(catalogue, catalogue) == "",
)
check("an empty result adds nothing", result_block("status", None) == "")

huge = " ".join(f"token{i}" for i in range(20000))
clipped = result_block("status", huge)
check(
    "a runaway result is clipped and says so",
    len(clipped) <= RESULT_LIMIT + 200 and "clipped" in clipped.lower(),
)
check(
    "clipping never severs a URL",
    "https://" not in result_block("s", huge + " https://example.invalid/issues/27", limit=200),
)
check(
    "a dead task row leaves the status line the notifier already built",
    watchers._kanban_handoff_with_result("\nstatus", None) == "\nstatus",
)

# The branch that has no event summary: kanban_watchers.py builds the status
# line out of a clip of task.result, so the message used to carry the opening
# of the report and then the report. On the 60-line catalogue below, jobs 1 to
# 19 arrived twice.
long_catalogue = "\n".join(
    f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled" for i in range(1, 61)
)


class _ClippedTask:
    result = long_catalogue


check(
    "the fixture is over the status line's budget",
    len(long_catalogue) > DEFAULT_LIMIT,
)
no_summary_tail = watchers._kanban_handoff_with_result(
    "\n" + clip_handoff(long_catalogue), _ClippedTask()
)
check(
    "an over-budget report is delivered once rather than clipped and repeated",
    no_summary_tail.count("1. cron-job-1 ") == 1,
    "the clipped status line was kept above the full report",
)
check(
    "the report the reader gets is the whole one",
    "60. cron-job-60" in no_summary_tail,
)
check(
    "no clip marker is left promising text that is already there",
    "[…]" not in no_summary_tail,
)
check(
    "a status line that is not the report is kept",
    "Cataloged all 9" in watchers._kanban_handoff_with_result(
        "\nCataloged all 9 cron jobs.", _ClippedTask()
    ),
)

# --- 4. The incident, replayed ---------------------------------------------
print("2026-08-07 incident replay:")
krr._refused_at.clear()
incident_summary = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
err, _ = krr.require_result("t_8d1cf5cf", incident_summary, None)
check("the completion that lost the catalogue is refused", err is not None)
_, stored = krr.require_result("t_8d1cf5cf", incident_summary, catalogue)


class _Task:
    result = catalogue


delivered = result_block("\n" + incident_summary, stored)
check("the retry's catalogue reaches the message", "cron-job-1" in delivered)
check("every job in the catalogue is delivered", all(f"cron-job-{i}" in delivered for i in range(1, 10)))
krr._refused_at.clear()

# --- Result -----------------------------------------------------------------
if FAILURES:
    print(f"\nverify_kanban_result: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_kanban_result: all checks passed")
