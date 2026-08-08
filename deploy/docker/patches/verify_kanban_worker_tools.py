#!/usr/bin/env python3
"""Build gate for the worker-only kanban tool patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_worker_tools.py``. The applier proves only that eight anchors
matched exactly once, and the Dockerfile's ``grep -c`` proves only that the
string ``check_fn=_check_kanban_worker_mode`` appears seven times. Neither says
anything about which function that name is bound to at import time, nor about
what it answers, and the gate has two answers to get right:

1. **A dispatcher-spawned worker keeps everything.** This patch exists to shrink
   the Chat Agent's prompt, and a version of it that also shrank a worker's tool
   surface would strand cards rather than save tokens: without
   ``kanban_complete`` or ``kanban_block`` a worker cannot end its run, exits
   rc=0, and is reaped as a ``protocol_violation``.
2. **A ``delegate_task`` child is not a worker.** The child runs
   ``run_conversation`` in the parent's own process and inherits its
   ``HERMES_KANBAN_TASK``, so an env-var read alone answers True for it. That is
   asserted here against the real ``agent.delegation_context``, because the unit
   tests can only reach that module through a fake in ``sys.modules`` — this is
   the only place the import the gate actually performs is exercised. Upstream's
   own two gates are measured alongside it: all three must agree that a child is
   neither a worker nor an orchestrator.

The last section drives the whole assembly path, ``model_tools`` through
``tools/registry.py``, because that is the only check that proves a schema
really disappears rather than a boolean flipping in isolation.

Usage::

    cd /opt/hermes && python3 verify_kanban_worker_tools.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from tools.kanban_worker_tools import (  # noqa: E402
    WORKER_ONLY_TOOLS,
    check_kanban_worker_mode,
)

# The surface agents/chat/SOUL.md §1.5 leaves an orchestrator, and the gate
# upstream registers each one with. Asserted so that a future anchor edit cannot
# quietly re-gate the tools the front door still needs.
ORCHESTRATOR_GATES = {
    "kanban_show": "_check_kanban_mode",
    "kanban_comment": "_check_kanban_mode",
    "kanban_create": "_check_kanban_mode",
    "kanban_list": "_check_kanban_orchestrator_mode",
    "kanban_unblock": "_check_kanban_orchestrator_mode",
}


# --- 1. Every registration names the gate it should -------------------------
print("registrations (tools/kanban_tools.py):")

tree = ast.parse((HERMES / "tools" / "kanban_tools.py").read_text())

gates: dict[str, str] = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    name = kwargs.get("name")
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        continue
    if "check_fn" not in kwargs:
        continue
    gates[name.value] = ast.unparse(kwargs["check_fn"])

for tool in WORKER_ONLY_TOOLS:
    check(
        f"{tool} is gated on the worker check",
        gates.get(tool) == "_check_kanban_worker_mode",
        f"registered with {gates.get(tool)!r}",
    )

for tool, gate in ORCHESTRATOR_GATES.items():
    check(
        f"{tool} still uses upstream's {gate}",
        gates.get(tool) == gate,
        f"registered with {gates.get(tool)!r}",
    )


# --- 2. The name resolves to this module's function -------------------------
# An import that landed below the registrations would raise NameError at module
# load; one that landed anywhere and shadowed the wrong callable would not. The
# live registry is the only place that distinction is visible.
print("live registry:")

import tools.kanban_tools as kanban_tools  # noqa: E402
from tools.registry import invalidate_check_fn_cache, registry  # noqa: E402

for tool in WORKER_ONLY_TOOLS:
    entry = registry.get_entry(tool)
    check(
        f"{tool} is registered with this module's gate",
        entry is not None and entry.check_fn is check_kanban_worker_mode,
        "missing from the registry" if entry is None else f"check_fn={entry.check_fn!r}",
    )


# --- 3. What the gate answers -----------------------------------------------
print("the gate:")

os.environ.pop("HERMES_KANBAN_TASK", None)
check("an orchestrator profile is not a worker", check_kanban_worker_mode() is False)

os.environ["HERMES_KANBAN_TASK"] = "t_c31a1f00"
check("a dispatcher-spawned worker is", check_kanban_worker_mode() is True)

from agent.delegation_context import (  # noqa: E402
    delegated_child_context,
    is_delegated_child_context,
)

check(
    "outside a delegation the real context says 'not a child'",
    is_delegated_child_context() is False,
)
with delegated_child_context():
    # The parent's HERMES_KANBAN_TASK is deliberately still set here: that is
    # the whole hazard, and scrubbing it would test nothing.
    check(
        "a delegate_task child is not a worker",
        check_kanban_worker_mode() is False,
        "the child inherits HERMES_KANBAN_TASK from the parent's process and "
        "owns no card; tools/kanban_tools.py refuses its board mutations for "
        "the same reason",
    )
    check(
        "and upstream's own two gates say the same",
        (
            kanban_tools._check_kanban_mode() is False
            and kanban_tools._check_kanban_orchestrator_mode() is False
        ),
        "upstream's own gates changed shape — re-derive this patch",
    )
check(
    "the delegation context hands the worker its tools back on the way out",
    check_kanban_worker_mode() is True,
)


# --- 4. The schemas really disappear ----------------------------------------
# The booleans above are only worth what the assembly does with them, and the
# assembly caches: tools/registry.py memoizes check_fn results for 30s keyed on
# (fn, profile), which is blind to the delegated-child ContextVar. Invalidate
# between measurements or the second one reads the first one's answer.
print("assembled tool definitions:")

import model_tools  # noqa: E402


def kanban_tool_names() -> set[str]:
    invalidate_check_fn_cache()
    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["kanban"],
        disabled_toolsets=[],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    return {
        (d.get("function") or {}).get("name")
        for d in (defs or [])
        if (d.get("function") or {}).get("name", "").startswith("kanban_")
    }


worker_tools = kanban_tool_names()
check(
    "a worker is offered every worker-only tool",
    set(WORKER_ONLY_TOOLS) <= worker_tools,
    f"missing {sorted(set(WORKER_ONLY_TOOLS) - worker_tools)}",
)

with delegated_child_context():
    child_tools = kanban_tool_names()
check(
    "a delegate_task child is offered none of them",
    not (set(WORKER_ONLY_TOOLS) & child_tools),
    f"leaked {sorted(set(WORKER_ONLY_TOOLS) & child_tools)}",
)

invalidate_check_fn_cache()


if FAILURES:
    print("\nkanban_worker_tools verification FAILED:")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("\nkanban_worker_tools verification passed")
