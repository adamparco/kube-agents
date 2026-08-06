"""Build-time behaviour gate for the kanban result-delivery patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_kanban_result_delivery.py``. The applier proves the
anchors matched and the file still parses; this proves the patched code does
what the patch exists to do. A failure here fails the image build.

The unit suite in ``test_kanban_result_delivery.py`` covers ``result_message``
in isolation. Three things it cannot cover, all of which have to hold in the
image and none of which ``ast.parse`` can see:

* the injected ``from gateway.kanban_result_delivery import …`` resolves, and
  importing ``gateway.kanban_watchers`` still works — a circular import here
  would take the whole gateway down at boot, long after the build passed;
* the call site reads ``handoff``, which is a local of the notifier loop. If
  that name is ever renamed upstream the patch still parses and still applies,
  and then raises ``NameError`` on the first completed card;
* the call sits inside ``if kind == "completed"`` and ahead of the artifact
  upload, so the chat reads status → report → attachments and no other event
  kind gets a result message.

The rest drives the real ``deliver_result`` against a recording adapter, so the
checks assert on what was and was not sent.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

WATCHERS = Path("/opt/hermes/gateway/kanban_watchers.py")

CARD = "t_7f3e0a5e"
CHAT = "spaces/AAAA1111"
META = {"thread_id": "spaces/AAAA1111/threads/BBBB"}

#: The status line the incident's completion actually carried.
SUMMARY = (
    "Successfully audited and cataloged all platform cron jobs, scheduled "
    "audits, background tasks, GitOps declarations, and GKE controller "
    "states. Provided a detailed manifest mapping schedules, targets, active "
    "states, and recent execution statuses."
)
#: The manifest that should have gone with it.
ANSWER = (
    "Active platform cron jobs (6 of 11 enabled):\n"
    "- compliance-audit — 20 6 * * * — Security & RBAC Posture Audit\n"
    "- obtainability-audit — 50 6 * * * — Workload Reliability Audit\n"
    "- github-issue-resolver — */30 * * * * — GitHub Issue Resolver"
)

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok  {label}")


class _Adapter:
    """Records sends instead of performing them."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return True


def _task(result):
    class _T:
        pass

    t = _T()
    t.result = result
    return t


def _enclosing_function(tree: ast.AST, target: ast.AST):
    """The def whose body contains ``target``, or None."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                return node
    return None


def _static_checks() -> None:
    tree = ast.parse(WATCHERS.read_text())

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_deliver_kanban_result"
    ]
    check("the delivery is called exactly once", len(calls), 1)
    if not calls:
        return
    call = calls[0]

    check(
        "it is called with the six arguments it needs",
        sorted(kw.arg for kw in call.keywords),
        ["adapter", "chat_id", "delivered", "metadata", "task", "task_id"],
    )

    fn = _enclosing_function(tree, call)
    check("the call site is inside a function", fn is not None, True)
    if fn is None:
        return

    # ``delivered=handoff`` reads a local of the notifier loop. Prove it is
    # assigned in the same function, or the first completed card raises
    # NameError in production and nothing in the build would have said so.
    assigned = {
        t.id
        for n in ast.walk(fn)
        for t in ast.walk(n)
        if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)
    }
    for name in ("handoff", "metadata", "task", "kind"):
        check(f"`{name}` is a local of the enclosing function", name in assigned, True)

    guards = {
        ast.unparse(n.test)
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and any(child is call for child in ast.walk(n))
    }
    check(
        "the send is guarded by the completed event",
        "kind == 'completed'" in guards,
        True,
    )

    source = WATCHERS.read_text()
    check(
        "the result is posted before the attachments",
        source.index("_deliver_kanban_result(")
        < source.index("self._deliver_kanban_artifacts("),
        True,
    )


async def _behaviour_checks() -> None:
    from gateway.kanban_result_delivery import (
        CLIPPED_TAIL,
        RESULT_LIMIT,
        deliver_result,
        result_message,
    )
    from gateway.kanban_result_delivery import clip_handoff

    # Resolved through the gateway package, not the host-side test fallback.
    check(
        "the in-image clip is the gateway one",
        clip_handoff.__module__,
        "gateway.kanban_handoff_clip",
    )

    async def deliver(result, delivered=f"\n{SUMMARY}"):
        adapter = _Adapter()
        sent = await deliver_result(
            adapter=adapter,
            chat_id=CHAT,
            metadata=META,
            task_id=CARD,
            delivered=delivered,
            task=_task(result),
        )
        return sent, adapter.sent

    # --- the completion that shipped on 2026-08-05, retried correctly --------
    sent, posts = await deliver(ANSWER)
    check("a result alongside a status line is delivered", sent, True)
    check("exactly one follow-up message", len(posts), 1)
    check("it goes to the card's chat", posts[0][0], CHAT)
    check("it carries the thread metadata", posts[0][2], META)
    check("it carries the manifest whole", ANSWER in posts[0][1], True)
    check("it names the card", CARD in posts[0][1], True)
    check("nothing was clipped", CLIPPED_TAIL in posts[0][1], False)

    # --- nothing to add ------------------------------------------------------
    for label, result in (
        ("no result", None),
        ("an empty result", ""),
        ("a whitespace-only result", "   \n\t "),
    ):
        sent, posts = await deliver(result)
        check(f"{label} sends nothing", (sent, posts), (False, []))

    # The legacy branch already put task.result in the completion line. A
    # second, identical copy is noise.
    sent, posts = await deliver(ANSWER, delivered=f"\n{ANSWER}")
    check("a result already in the line is not repeated", (sent, posts), (False, []))

    # …but if that line was clipped, the rest has still not been delivered.
    sent, posts = await deliver(ANSWER, delivered="\n" + ANSWER[:40])
    check("a clipped line still gets the full result", sent, True)

    # --- a runaway result is bounded, and says so ----------------------------
    flood = "spam " * 4000
    sent, posts = await deliver(flood)
    check("a runaway result is still delivered", sent, True)
    check("it is bounded", len(posts[0][1]) < RESULT_LIMIT + 200, True)
    check("and it says it was clipped", posts[0][1].endswith(CLIPPED_TAIL), True)

    # --- the pure half -------------------------------------------------------
    check("result_message returns nothing for nothing", result_message(CARD, "", None), "")
    check(
        "result_message ignores case and whitespace when deduplicating",
        result_message(CARD, "\n  ACTIVE   JOBS: none  ", "Active jobs: none"),
        "",
    )


def main() -> int:
    if not WATCHERS.is_file():
        print(f"VERIFY FAILED: {WATCHERS} does not exist")
        return 1
    _static_checks()

    # Importing the notifier proves the injected import resolves and does not
    # cycle. It is the check most likely to catch a base-image bump.
    import gateway.kanban_watchers as kw

    check("the notifier still imports", hasattr(kw, "_deliver_kanban_result"), True)

    asyncio.run(_behaviour_checks())

    if failures:
        print("\nVERIFY FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nkanban_result_delivery verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
