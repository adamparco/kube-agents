#!/usr/bin/env python3
"""Build gate for the configurable kanban wake patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier.
The applier only proves the anchor matched, and a matched anchor is the weaker
half of this patch. Every failure mode that actually costs anything here is
silent by construction: ``resolve_wake_kinds`` fails *towards* upstream, so a
``hermes_cli.config`` that moved, a ``gateway.wake`` that moved, or a
``DEFAULT_WAKE_KINDS`` whitelist that fell behind the notifier all present
identically to "the operator never set ``kanban.wake_on_events``". The symptom
is not an exception, it is the 5.9 s / 32,460-token redundant turn from task
``t_c31a1f00`` quietly coming back.

So this drives the *patched* runtime rather than reading it: the real
``hermes_cli.config.load_config``, the real
``gateway.wake.adapter_supports_push``, the real ``APIServerAdapter`` whose
``supports_async_delivery = False`` is the reason the narrowing is scoped to
the push path at all, and the real ``_wake_kinds_for`` name the notifier loop
resolved.

Usage::

    cd /opt/hermes && python3 verify_kanban_wake_kinds.py
"""

from __future__ import annotations

import re
import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


from gateway.kanban_wake_kinds import (  # noqa: E402
    CONFIG_KEY,
    DEFAULT_WAKE_KINDS,
    _adapter_can_push,
    _load_kanban_config,
    resolve_wake_kinds,
    wake_kinds_for,
)

FAILURE_ONLY = {"wake_on_events": ["gave_up", "crashed", "timed_out", "blocked"]}


def cfg(kanban):
    return lambda: {"kanban": kanban}


class Event:
    def __init__(self, kind):
        self.kind = kind


# --- 1. The wiring resolved ---------------------------------------------------
print("import wiring:")
import gateway.kanban_watchers as watchers  # noqa: E402

check(
    "the notifier resolved the wake-kinds import",
    hasattr(watchers, "_wake_kinds_for"),
    "the trailer import did not execute",
)

NOTIFIER_SOURCE = open("gateway/kanban_watchers.py").read()
check(
    "the notifier calls the helper with the adapter",
    '_wake_kinds = _wake_kinds_for(d["events"], adapter=adapter)' in NOTIFIER_SOURCE,
)
check(
    "upstream's hardcoded tuple is gone",
    "_WAKE_KINDS = (" not in NOTIFIER_SOURCE and "in _WAKE_KINDS}" not in NOTIFIER_SOURCE,
    "a second definition would shadow the configurable one",
)

# --- 2. The whitelist still covers every kind the notifier knows about --------
# `resolve_wake_kinds` treats DEFAULT_WAKE_KINDS as a whitelist so a typo in
# config cannot be mistaken for a real kind. The cost of that is drift: if a
# base-image bump teaches the notifier a sixth terminal kind, the whitelist
# silently filters it out and an operator listing it gets a warning about an
# "unknown" kind their gateway plainly understands. The notifier enumerates the
# kinds it can describe in its own `_parts` block, so that block is the source
# of truth to compare against.
print("whitelist drift:")
notifier_kinds = set(re.findall(r'if "(\w+)" in _wake_kinds', NOTIFIER_SOURCE))
check(
    "the notifier's own kind list was located",
    notifier_kinds,
    "the `if \"<kind>\" in _wake_kinds` block moved; re-derive this check",
)
check(
    "every kind the notifier can describe is in DEFAULT_WAKE_KINDS",
    notifier_kinds <= set(DEFAULT_WAKE_KINDS),
    f"notifier knows {sorted(notifier_kinds - set(DEFAULT_WAKE_KINDS))}, "
    f"which the whitelist would drop as a typo",
)
check(
    "DEFAULT_WAKE_KINDS claims nothing the notifier cannot describe",
    set(DEFAULT_WAKE_KINDS) <= notifier_kinds,
    f"whitelist has {sorted(set(DEFAULT_WAKE_KINDS) - notifier_kinds)} extra",
)

# --- 3. The real config loader is reachable ----------------------------------
# The exact silent no-op this patch is most exposed to. `_load_kanban_config`
# returns None when `hermes_cli.config` cannot be imported or read, and None
# means DEFAULT_WAKE_KINDS on every single delivery — the key stops working and
# nothing distinguishes that from an operator who never set it.
print("real config path:")
subtree = _load_kanban_config(None)
check(
    "hermes_cli.config.load_config is importable and readable",
    isinstance(subtree, dict),
    "the module falls back to upstream on every call; kanban."
    f"{CONFIG_KEY} would be dead config",
)
check(
    "the no-argument path returns a usable set",
    isinstance(resolve_wake_kinds(), tuple)
    and set(resolve_wake_kinds()) <= set(DEFAULT_WAKE_KINDS),
)

# --- 4. The real push-capability probe ---------------------------------------
# `_adapter_can_push` prefers `gateway.wake.adapter_supports_push` and only
# falls back to re-stating its one-line contract when that import fails —
# which is the host-side test condition, not an in-image one. Drive it with the
# two real adapter classes so a `gateway.wake` that moved is caught here rather
# than by a completed card that never gets announced.
print("push capability:")
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from gateway.wake import adapter_supports_push  # noqa: E402

check(
    "the api_server adapter is still the non-push one",
    adapter_supports_push(APIServerAdapter) is False,
    "the whole non-push carve-out is predicated on this adapter existing",
)
check("_adapter_can_push agrees on api_server", _adapter_can_push(APIServerAdapter) is False)
check("_adapter_can_push agrees on the base adapter", _adapter_can_push(BasePlatformAdapter) is True)
check(
    "an adapter that does not declare the flag counts as push",
    _adapter_can_push(object()) is True,
    "reading it as non-push restores the redundant turn on every Slack card",
)

# --- 5. The decision the notifier actually makes ------------------------------
print("wake decision:")
completed = [Event("completed"), Event("commented")]
mixed = [Event("completed"), Event("crashed")]

check(
    "an unset key behaves exactly like upstream",
    wake_kinds_for(completed, cfg({}), adapter=BasePlatformAdapter) == {"completed"},
)
check(
    "a delivered completion costs no turn on a push adapter",
    wake_kinds_for(completed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter) == set(),
    "this is the 5.9s / 32,460-token hop the patch exists to remove",
)
check(
    "a failure in the same batch still wakes the creator",
    wake_kinds_for(mixed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter) == {"crashed"},
)
check(
    "a non-push adapter still wakes on completion",
    wake_kinds_for(completed, cfg(FAILURE_ONLY), adapter=APIServerAdapter) == {"completed"},
    "on api_server the wake self-post IS the delivery; narrowing loses the answer",
)
check(
    "even an explicit empty list cannot silence the non-push path",
    wake_kinds_for(completed, cfg({"wake_on_events": []}), adapter=APIServerAdapter)
    == {"completed"},
)

# --- 6. Failure posture -------------------------------------------------------
print("fail-soft posture:")


def raising():
    raise RuntimeError("config unreadable")


check(
    "an unreadable config still wakes on a crash",
    resolve_wake_kinds(raising) == DEFAULT_WAKE_KINDS,
    "failing closed would mean a crashed card silently never escalating",
)
check(
    "a config of the wrong shape still wakes on a crash",
    resolve_wake_kinds(lambda: "not a mapping") == DEFAULT_WAKE_KINDS,
)
check(
    "a value of the wrong shape still wakes on a crash",
    resolve_wake_kinds(cfg({"wake_on_events": {"crashed": True}})) == DEFAULT_WAKE_KINDS,
)
check(
    "an unknown kind is dropped rather than trusted",
    resolve_wake_kinds(cfg({"wake_on_events": ["crashed", "compleeted"]})) == ("crashed",),
)

print()
if FAILURES:
    print(f"verify_kanban_wake_kinds: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_wake_kinds: all checks passed")
