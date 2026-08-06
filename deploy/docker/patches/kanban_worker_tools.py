"""Hide worker-only kanban tools from orchestrator profiles.

Installed into the image at ``/opt/hermes/tools/kanban_worker_tools.py`` and
wired into ``tools/kanban_tools.py`` by ``deploy/docker/Dockerfile`` via
``apply_kanban_worker_tools.py``.

Upstream gates each kanban tool with a ``check_fn``, and has two of them:

``_check_kanban_mode``
    True for a dispatcher-spawned worker (``HERMES_KANBAN_TASK`` is set) *or*
    any profile carrying the ``kanban`` toolset.

``_check_kanban_orchestrator_mode``
    The board-routing surface — the mirror image, false for workers. Upstream
    applies it to ``kanban_list`` and ``kanban_unblock``.

The third case has no implementation: tools that only make sense *inside* a
run. Closing a card, blocking it, heartbeating it, linking it, attaching an
artifact to it — all of these belong to the specialist doing the work. Every
one of them is nonetheless registered with ``_check_kanban_mode``, so the Chat
Agent front door is handed the full lifecycle surface purely because it carries
the ``kanban`` toolset to create cards.

``agents/chat/SOUL.md`` §1.5 already states the boundary in prose:

    Never call ``kanban_complete``, ``kanban_block``, ``kanban_heartbeat``, or
    ``kanban_link`` — those belong to the specialist actually doing the work,
    not the front door.

Prose is not free. Hermes has no per-tool denylist — ``agent.disabled_toolsets``
is toolset-level, and ``_strip_blocked_tools`` in ``tools/delegate_tool.py`` is
scoped to delegation — so those schemas ship on every single front-door model
call and the rule has to be re-read and re-obeyed each time. Measured with
``hermes prompt-size`` on 2026-08-05, the tools below cost the Chat Agent
**10,041 characters (~2,510 tokens) per call** for capabilities it is forbidden
to use:

======================  ======
tool                    chars
======================  ======
``kanban_complete``      3,410
``kanban_block``         1,700
``kanban_attach``        1,356
``kanban_attach_url``    1,156
``kanban_heartbeat``       918
``kanban_link``            773
``kanban_attachments``     728
======================  ======

``check_kanban_worker_mode`` supplies the missing third gate. Nothing changes
for a worker: the dispatcher sets ``HERMES_KANBAN_TASK`` before spawning it
(``hermes_cli/kanban_db.py``, ``_default_spawn``), so a worker keeps every tool
it has today. Orchestrator profiles keep ``kanban_create``, ``kanban_show``,
``kanban_list``, ``kanban_comment`` and ``kanban_unblock`` — exactly the surface
SOUL.md permits them.

A cron run is deliberately treated as *not* a worker's own card here, which is
consistent with ``tools/cron_run_scope.py``: a dispatched run borrows the
worker's environment but owns no card, and is already refused if it tries to
close the caller's.
"""

from __future__ import annotations

import os

#: Tools registered upstream with ``_check_kanban_mode`` that only make sense
#: inside a dispatcher-spawned run. Kept here as documentation and as the
#: contract ``apply_kanban_worker_tools.py`` asserts against.
WORKER_ONLY_TOOLS = (
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_link",
    "kanban_attach",
    "kanban_attach_url",
    "kanban_attachments",
)


def check_kanban_worker_mode() -> bool:
    """True only inside a dispatcher-spawned kanban run.

    The mirror of upstream's ``_check_kanban_orchestrator_mode``. Deliberately
    does *not* consult the profile's toolset list: carrying the ``kanban``
    toolset is what lets a profile route work, and routing work is precisely
    the case these tools must stay hidden for.
    """
    return bool(os.environ.get("HERMES_KANBAN_TASK"))
