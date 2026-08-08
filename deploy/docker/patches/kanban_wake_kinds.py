"""Make the kanban notifier's agent wake configurable.

Installed into the image at ``/opt/hermes/gateway/kanban_wake_kinds.py`` and
wired into ``gateway/kanban_watchers.py`` by ``deploy/docker/Dockerfile``.

When a card reaches a terminal state the notifier does two separate things for
the same event:

1. ``adapter.send(...)`` posts the completion line — the worker's own summary —
   straight into the originating chat thread. The user has the answer at this
   point.
2. ``adapter.handle_message(...)`` then injects a synthetic ``MessageEvent`` to
   *wake the agent that created the card*, which costs a full model turn.

Upstream hardcodes which event kinds trigger step 2::

    _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")

There is no config key for it anywhere in Hermes. For the Chat Agent front door
that makes ``completed`` pure overhead: the summary has already been delivered,
so the woken turn re-reads the card with ``kanban_show`` and paraphrases a
message the user is already looking at. Measured on task ``t_c31a1f00``
(2026-08-05): **5.9 s and 32,460 input tokens** for that third hop, on a request
whose actual work was a single 477 ms ``list_clusters`` call.

The failure kinds are a different matter. ``gave_up`` / ``crashed`` /
``timed_out`` / ``blocked`` produce a terse status line and nothing else, and
the front door genuinely should react — retry, escalate, or tell the user what
broke. So this is not "turn the wake off", it is "wake for the events that need
a decision, not for the one that already answered itself".

``resolve_wake_kinds`` reads ``kanban.wake_on_events`` from config and falls
back to the upstream tuple, so an image built without that key set behaves
exactly as upstream does.

All of that reasoning is conditional on step 1 having happened, which is why
``wake_kinds_for`` takes the adapter and leaves a non-push one alone: where the
notifier skips the send, the wake is not a third hop over a delivered answer,
it is the only delivery there is.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional, Tuple

logger = logging.getLogger("gateway.run")

#: The upstream hardcoded set, and the fallback for any config that does not
#: say otherwise. Also the whitelist: a kind outside this set can never match
#: ``ev.kind`` for a terminal event, so allowing it through would only hide a
#: typo.
DEFAULT_WAKE_KINDS: Tuple[str, ...] = (
    "completed",
    "gave_up",
    "crashed",
    "timed_out",
    "blocked",
)

CONFIG_KEY = "wake_on_events"

#: Reasons :func:`_load_kanban_config` has already reported this process.
#: The notifier reaches it on every delivery, so a config that is permanently
#: unreadable would otherwise warn every five seconds for the life of the
#: gateway; one line per distinct cause is enough to explain the behaviour.
#: Tests clear it between cases.
_warned_config: set = set()


def _warn_config_once(cause: str, message: str, *args: object) -> None:
    if cause in _warned_config:
        return
    _warned_config.add(cause)
    logger.warning(message, *args)


def _load_kanban_config(load_config: Optional[Callable[[], object]]) -> Optional[dict]:
    """Return the ``kanban`` config subtree, or None if it cannot be read.

    Returning None sends :func:`resolve_wake_kinds` back to
    :data:`DEFAULT_WAKE_KINDS`, which is the safe answer but an invisible one:
    it is byte-for-byte what an operator who never set ``kanban.wake_on_events``
    gets, so a loader that has genuinely broken presents as a key that was
    never configured, and the redundant turn comes back with nothing in the
    logs. Each failure therefore says so once before degrading.
    """
    if load_config is None:
        try:
            from hermes_cli.config import load_config as _lc
        except Exception as exc:
            _warn_config_once(
                "import",
                "kanban notifier: hermes_cli.config is not importable (%s); "
                "kanban.%s cannot be read and the upstream wake set applies to "
                "every card",
                exc,
                CONFIG_KEY,
            )
            return None
        load_config = _lc
    try:
        cfg = load_config()
    except Exception as exc:
        _warn_config_once(
            "read",
            "kanban notifier: reading the Hermes config failed (%s); "
            "kanban.%s is being ignored and the upstream wake set applies",
            exc,
            CONFIG_KEY,
        )
        return None
    if not isinstance(cfg, dict):
        _warn_config_once(
            "shape",
            "kanban notifier: load_config() returned %s rather than a mapping; "
            "kanban.%s is being ignored and the upstream wake set applies",
            type(cfg).__name__,
            CONFIG_KEY,
        )
        return None
    kcfg = cfg.get("kanban", {})
    return kcfg if isinstance(kcfg, dict) else {}


def resolve_wake_kinds(
    load_config: Optional[Callable[[], object]] = None,
) -> Tuple[str, ...]:
    """Return the event kinds that should wake the card's creator.

    Read fresh on every delivery rather than captured at gateway boot, so
    changing ``kanban.wake_on_events`` takes effect on the next tick instead of
    requiring a restart. The config read is cheap: ``load_config()`` is
    mtime-cached upstream.

    Fails **towards upstream behaviour**, loudly. A missing key, an unreadable
    config, or a value of the wrong shape all yield :data:`DEFAULT_WAKE_KINDS`
    — a transient read error must not stop waking an agent on a crash — but
    every case except the missing key logs its reason first, so a degraded
    read is distinguishable from a key nobody set.
    Only an explicit, well-formed value narrows the set; an explicit empty list
    disables the wake entirely, which is a deliberate choice a user can make.
    """
    kcfg = _load_kanban_config(load_config)
    if kcfg is None or CONFIG_KEY not in kcfg:
        return DEFAULT_WAKE_KINDS

    raw = kcfg.get(CONFIG_KEY)
    if raw is None:
        # `wake_on_events:` with nothing after it parses as None. Read that as
        # "no wake", matching the explicit empty list rather than falling back
        # to the default the user was plainly trying to override.
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        logger.warning(
            "kanban notifier: kanban.%s must be a list of event kinds, got %r; "
            "using the default wake set",
            CONFIG_KEY,
            type(raw).__name__,
        )
        return DEFAULT_WAKE_KINDS

    kinds: list[str] = []
    unknown: list[str] = []
    for item in raw:
        kind = str(item).strip()
        if not kind:
            continue
        if kind not in DEFAULT_WAKE_KINDS:
            unknown.append(kind)
            continue
        if kind not in kinds:
            kinds.append(kind)
    if unknown:
        # Loud, because the failure mode is silent: an unknown kind never
        # matches a real event, so a typo reads as "the wake just stopped
        # working" with nothing in the logs to explain it.
        logger.warning(
            "kanban notifier: ignoring unknown kanban.%s value(s) %s; "
            "valid kinds are %s",
            CONFIG_KEY,
            ", ".join(sorted(unknown)),
            ", ".join(DEFAULT_WAKE_KINDS),
        )
    return tuple(kinds)


def _adapter_can_push(adapter: object) -> bool:
    """Whether *adapter* has a push channel.

    Defers to ``gateway.wake.adapter_supports_push`` so this stays correct if
    upstream ever makes the capability something richer than one attribute. That
    module is not importable outside the image, so the fallback re-states its
    current one-line contract rather than guessing: an adapter that does not
    declare the flag is push-capable.
    """
    try:
        from gateway.wake import adapter_supports_push
    except Exception:
        # Silent on purpose: outside the image this import is *expected* to
        # fail, so warning here would fire on every host-side unit test while
        # saying nothing about the deployed gateway. That the real module is
        # reachable in the image is asserted by verify_kanban_wake_kinds.py,
        # which drives this against the actual APIServerAdapter.
        return bool(getattr(adapter, "supports_async_delivery", True))
    try:
        return bool(adapter_supports_push(adapter))
    except Exception:
        logger.warning(
            "kanban notifier: adapter_supports_push(%s) raised; treating it as "
            "push-capable and applying kanban.%s as configured",
            type(adapter).__name__,
            CONFIG_KEY,
        )
        return True


def wake_kinds_for(
    events: Iterable[object],
    load_config: Optional[Callable[[], object]] = None,
    adapter: object = None,
) -> set:
    """Return the subset of ``events``' kinds that should wake the creator.

    Mirrors the upstream expression it replaces::

        {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}

    ``adapter`` opts a non-push adapter out of the narrowing entirely, and
    passing it is not optional in the notifier. The whole argument for dropping
    ``completed`` is that ``adapter.send()`` already put the worker's summary in
    the thread, so the wake is a third hop over an answer the user is looking
    at. On an adapter with no push channel — the API server, whose ``send()``
    returns ``SendResult(success=False)`` by design — the notifier skips that
    send and says so in its own comment: *"the wake self-post below IS the
    delivery"*. Narrow the set there and a card that completes successfully is
    never announced to anyone; upstream added that self-post to fix the
    api_server wrong-session bug, and dropping ``completed`` re-breaks it.

    So the config key governs the push path only. On the non-push path the full
    upstream set always applies, including an explicit ``wake_on_events: []``:
    that key means "do not spend a turn re-reading an answer already
    delivered", which is not a thing anyone can be asking for where nothing was
    delivered.
    """
    allowed = resolve_wake_kinds(load_config)
    if adapter is not None and not _adapter_can_push(adapter):
        allowed = DEFAULT_WAKE_KINDS
    return {ev.kind for ev in events if getattr(ev, "kind", None) in allowed}
