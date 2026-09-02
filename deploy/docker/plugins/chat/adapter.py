"""``deliver: "chat"`` — hand a cron job's report to the Chat Agent.

Installed at ``/opt/hermes/plugins/platforms/chat/`` so Hermes discovers it as a
bundled platform plugin. Nothing in the Hermes tree is edited: the scheduler
already routes ``deliver=<name>`` through the platform registry, and
``cron/scheduler.py::_plugin_cron_env_var`` says so in its own words —
plugins that set ``cron_deliver_env_var`` on their ``PlatformEntry`` "get cron
delivery support without editing this module".

Why a delivery mode and not a prompt instruction
------------------------------------------------

The relay itself — the Chat Agent composing the message, and the report being
stored against the thread it lands in — is ``docs/designs/cron-report-relay.md``.
This module is only about *what triggers* it.

The first cut triggered it from the job's prompt: call ``report_to_chat``, then
return ``[SILENT]``. That works for a roster shipped in the image, where the
prompt is reviewed alongside the job, and fails for everything else. A job the
user asks for at runtime ("watch that rollout every ten minutes and tell me when
it settles") is created through ``cronjob(action='create')`` with whatever prompt
the moment produced, so it carries no such contract — and its ``deliver`` then
resolves to a Google Chat home channel this image cannot hand a child profile,
i.e. to nowhere. The result is a job that runs forever and is never heard from,
which is the exact failure the relay exists to end.

``deliver`` is the field that already means "where does the output go", every
creation path can set it, and ``create_job``'s fixed keyword signature makes it
the only field a runtime-created job *can* set. So the relay is a platform, and
asking for it is one field.

Why this platform is not a platform
-----------------------------------

It has no inbound side and no adapter. ``adapter_factory`` exists because
``PlatformEntry`` requires one and raises if the gateway ever tries to build it —
which it will not, because ``_is_connected`` returns False unless
``CHAT_HOME_CHANNEL`` is set, and only ``profile_cron_tick.py`` sets it, for the
cron children it spawns. In the gateway process the platform stays unregistered
in the config, invisible to ``gateway status``, and starts nothing.

That one variable is the whole switch: it gates enablement (through
``is_connected``), it is the ``cron_deliver_env_var`` the scheduler reads to
resolve ``deliver: "chat"`` to a target, and where it is unset the platform
behaves as if this directory were not there.

What the sender can and cannot see
----------------------------------

``standalone_sender_fn`` is handed the delivery text, not the job. The job's id
and name are in the text, because ``_deliver_result`` wraps every cron delivery
in a two-line header before sending it, and :func:`parse_cron_wrapper` reads them
back out. That coupling is checked at image build time by
``deploy/docker/plugins/verify_chat_relay.py``, which drives the real
``_deliver_result`` and asserts on what arrived — so upstream changing the
wrapper fails the build rather than degrading in production.

If the header is ever absent (``cron.wrap_response: false`` turns it off), the
report still relays: it just arrives under a per-profile session for the day
instead of a per-job one, and says so in the log.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

#: The platform name, and therefore the ``deliver`` token. Must equal this
#: directory's basename: ``Platform._missing_`` admits a plugin platform by
#: scanning ``plugins/platforms/`` for directory names.
PLATFORM_NAME = "chat"

#: Set => the relay is on in this process. See the module docstring.
HOME_CHANNEL_ENV = "CHAT_HOME_CHANNEL"

#: The Session KV server's relay route. Loopback: it runs in this Pod.
DEFAULT_RELAY_URL = "http://127.0.0.1:8699/v1/cron-reports"
RELAY_URL_ENV = "CRON_REPORT_RELAY_URL"

#: Every route on the Session KV server except ``/healthz`` needs this.
API_KEY_ENV = "SESSION_KV_API_KEY"

#: The route relays synchronously and answers with the outcome, so this has to
#: cover a whole Chat Agent turn plus the chat round trip -- not just a connect
#: stall. Sized above ``_run_relay_turn``'s own 300s so the server's verdict is
#: what the scheduler records; time out first and a delivered report would be
#: written down as a failure.
RELAY_TIMEOUT_SECONDS = 360.0

#: Markdown a model wraps around a bare token: emphasis, code spans, and the
#: whitespace either side. Stripped from both ends of a report before it is
#: tested for silence — see :func:`is_silent_report`.
_MARKDOWN_DRESS = "`*_~ \t\r\n"

#: The token a cron run emits to say it has nothing to report.
#:
#: Restated rather than imported from ``cron.scheduler``, which lives in the
#: pinned base image and is absent from this checkout — importing it would make
#: this module unimportable under its own tests and take the whole silence
#: predicate with it. Restating a constant is only safe if something notices
#: when the two diverge, so ``verify_chat_relay.py`` imports the upstream one at
#: image-build time and fails the build if it is not this string.
SILENT_MARKER = "[SILENT]"

#: ``_deliver_result``'s wrapper. Matched, not assumed — see
#: :func:`parse_cron_wrapper`.
_WRAPPER_RE = re.compile(
    r"\ACronjob Response: (?P<title>.*)\n\(job_id: (?P<job_id>.*)\)\n-{5,}\n\n",
)


def parse_cron_wrapper(message: str) -> Tuple[str, str, str]:
    """Split ``_deliver_result``'s wrapper into ``(job_id, title, report)``.

    Returns empty strings for the two identifiers when the wrapper is absent,
    leaving ``report`` as the whole message. The caller relays either way: a
    report that lands in the wrong thread is worth more than one that does not
    land at all.

    The footer is removed by exact suffix rather than by pattern. It is built
    from the job's own name, so once the header has given us that name the exact
    string is known — and a report that happens to quote the footer keeps it.
    """
    match = _WRAPPER_RE.match(message or "")
    if not match:
        return "", "", message or ""
    title = match.group("title").strip()
    body = message[match.end() :]
    footer = (
        "\n\nTo stop or manage this job, send me a new message "
        f'(e.g. "stop reminder {title}").'
    )
    if body.endswith(footer):
        body = body[: -len(footer)]
    return match.group("job_id").strip(), title, body.strip()


def profile_name() -> str:
    """The profile this cron child runs as, from its ``HERMES_HOME``.

    Named profiles live at ``<root>/profiles/<name>``, which is the shape
    ``profile_cron_tick.py`` hands the child. Anything else is the root home —
    the Chat Agent's own store — and reports as ``default``. Only used to name
    the relay session, so a wrong answer costs a thread, not a delivery.
    """
    home = Path(os.getenv("HERMES_HOME", "") or "/opt/data")
    return home.name if home.parent.name == "profiles" else "default"


def relay_url() -> str:
    return (os.getenv(RELAY_URL_ENV, "") or "").strip() or DEFAULT_RELAY_URL


#: Suffix of the variable a platform's home channel is configured in
#: (``SLACK_HOME_CHANNEL``, ``GOOGLE_CHAT_HOME_CHANNEL``, and this plugin's own
#: ``CHAT_HOME_CHANNEL``). The scheduler resolves a delivery target by reading
#: exactly this, so scanning for it answers "which platforms would ``all``
#: expand to *here*" without importing anything from ``cron.scheduler``.
_HOME_CHANNEL_SUFFIX = "_HOME_CHANNEL"


def sibling_delivery_targets(job_id: str) -> list[str]:
    """Platforms the scheduler is posting this same report to, besides the relay.

    ``deliver`` takes a list, and the relay is one entry in it. ``deliver:
    "chat"`` is relay-only and this returns nothing; ``deliver: "all"`` also
    posts the raw report to every home channel, so unless the relay is told, its
    fan-out puts a second, composed copy in each of those channels. The route
    subtracts what this names — see ``relay_cron_report``.

    Answered here rather than on the server because this process is the one that
    knows. ``all`` expands over the platforms with a home channel in the *cron
    child*, and ``profile_cron_tick.home_target_env`` rebuilds those from the
    root ``config.yaml``: an install whose config carries ``slack: {}`` has no
    ``SLACK_HOME_CHANNEL`` here, the scheduler silently drops Slack from the
    expansion, and the relay leg is the only thing that reaches it. The server
    cannot see any of that — it runs in the gateway, with the full pod
    environment — so deciding there would suppress a leg nobody sent.

    Best effort in both directions, and the direction matters: an unreadable
    roster returns nothing, which relays as before rather than dropping a
    channel. Over-reporting would lose a delivery; under-reporting only risks
    the duplicate this exists to prevent.
    """
    home = Path(os.getenv("HERMES_HOME", "") or "/opt/data")
    try:
        with open(home / "cron" / "jobs.json", encoding="utf-8") as handle:
            store = json.load(handle)
    except (OSError, ValueError):
        return []

    jobs = store.get("jobs") if isinstance(store, dict) else store
    raw: object = ""
    # An empty `job_id` is every delivery that carries no cron wrapper -- the
    # `cron.wrap_response: false` case this module still relays -- and matching
    # it against `job.get("id") or ""` made it equal to the first job in the
    # store with a missing or empty id. A hand-edited `jobs.json` is all that
    # takes, and the delivery then subtracts platforms on a different job's
    # `deliver`. There is no job to look up here, so look none up.
    for job in jobs if isinstance(jobs, list) and job_id else []:
        if isinstance(job, dict) and str(job.get("id") or "") == job_id:
            raw = job.get("deliver") or ""
            break

    # A list is the shape the paragraph above describes and the one hermes
    # treats as native -- `hermes_cli/cron.py` coerces a string *into* a list,
    # never the reverse -- so it is the string form here that is the shorthand.
    # `str()` over the list gave `"['slack', 'gchat']"`, whose comma split
    # yields two tokens matching no platform and no `<NAME>_HOME_CHANNEL`. The
    # fan-out then came back empty, which is indistinguishable from the honest
    # empty answer for `deliver: "chat"` -- so every sibling channel quietly got
    # the duplicate copy this function exists to subtract.
    text = ",".join(str(entry) for entry in raw) if isinstance(raw, (list, tuple)) else str(raw)
    # Split the way the scheduler does and no other way. It is
    # `cron/scheduler.py::_resolve_delivery_targets`, and it splits on `,`
    # alone. Accepting `;` as well made this the looser of the two parsers,
    # which is the direction the docstring above says never to err in:
    # `deliver: "chat,slack;x"` gave the scheduler one part it cannot resolve,
    # so it delivered nowhere, while this named `slack` as handled and the
    # relay subtracted it. Nothing was posted anywhere and the run recorded
    # `ok`. On `,` alone, `slack;x` matches no platform, so the relay posts and
    # the channel gets one copy.
    #
    # `platform:chat_id[:thread]` is a target the scheduler resolves --
    # `_resolve_single_delivery_target` splits on the first `:` and looks the
    # platform up -- so the prefix is the token here too. Reading the part
    # whole left `slack:D0BKGRBM6RH` matching no platform and no
    # `<NAME>_HOME_CHANNEL`, so the relay posted a second copy of the report
    # into the channel the scheduler had just delivered it to.
    tokens = {
        part.split(":", 1)[0].strip().lower() for part in text.split(",") if part.strip()
    }
    if not tokens or tokens <= {PLATFORM_NAME}:
        return []

    if "all" in tokens:
        # What `all` resolves to in this process: every platform whose home
        # channel is actually set. A variable that is present but empty is not a
        # target -- the scheduler requires a non-empty chat id -- so the value is
        # tested, not just the key.
        tokens = {
            key[: -len(_HOME_CHANNEL_SUFFIX)].lower()
            for key, value in os.environ.items()
            if key.endswith(_HOME_CHANNEL_SUFFIX) and value.strip()
        }

    return sorted(
        name
        for name in tokens
        if name != PLATFORM_NAME and os.getenv(f"{name.upper()}{_HOME_CHANNEL_SUFFIX}", "").strip()
    )


def is_silent_report(report: str) -> bool:
    """Should this report be swallowed rather than relayed?

    True for an empty report, and for one whose *entire* content is the silence
    marker however the model dressed it. ``` `[SILENT]` ``` and ``**[SILENT]**``
    are the forms to expect: these reports are written by agents that write
    markdown by default, and every audit SOP tells the run to copy
    ``chat_summary`` — a field whose value *is* ``[SILENT]`` on a quiet run —
    verbatim into its final response. Emphasise it once and the run that meant
    to say nothing posts the word "[SILENT]" to the home channel instead, which
    is the one outcome the silent path exists to prevent. So undress the report
    before testing it. On a real report this changes nothing: stripping
    punctuation off the two ends of a multi-line audit summary cannot turn it
    into the marker.

    **Entire** is load-bearing, and it is why this does not call the scheduler's
    ``_is_cron_silence_response``. That matcher accepts the marker on its own
    line among prose, which is right for the thing it grades — a model's final
    response to a cron prompt, where the marker anywhere means the model chose
    silence and the prose is its reasoning. It is wrong for what reaches here.
    ``standalone_send`` is the sender for every out-of-process ``hermes send``,
    not only cron: ``session_kv_server._post_initial_alert`` posts RCA and
    incident alerts through it, and ``_send_to_chat`` posts the composed cron
    report itself. An alert that quotes the marker while explaining why a run
    published nothing — which is exactly what an alert about a silent run says —
    matched, and was dropped with ``{"success": True, "skipped": "empty_text"}``
    and no ``message_id``, so the caller could not tell it had lost the page.

    Delegating bought nothing against that cost. Where the scheduler's matcher
    applies it suppresses delivery before either sender runs, so its extra
    leniency is redundant here; the only calls it changed were the ones it had
    never graded. Everything the marker legitimately arrives as when it is the
    whole message — bare, lowercased, dressed, padded — the two lines below
    already catch.

    Bare ``strip()`` on both sides of the dress, because this predicate
    replaced a plain ``not report.strip()`` and has to stay a superset of it.
    ``_MARKDOWN_DRESS`` can only list ASCII characters, while ``str.strip()``
    also takes NBSP, ``\\x0b``, ``\\x0c``, ``\\x1c``, ``\\u2028``, ``\\u2003``
    and ``\\u3000`` — so stripping the dress alone called a report of one NBSP
    non-empty and relayed it. `submit_cron_report` then rejects it as blank
    with an HTTP 400 that lands in ``last_delivery_error``, which is the exact
    failure this guard exists to prevent. The trailing strip catches the same
    characters once the dress around them is gone.
    """
    bare = report.strip().strip(_MARKDOWN_DRESS).strip()
    return not bare or bare.upper() == SILENT_MARKER


#: Where ``audit_report.py finish`` records each stream's last run. Same
#: environment variable and same default, so an install that moves the store
#: moves this lookup with it.
_REPORTS_DIR = os.getenv("FLEET_AUDIT_REPORTS_DIR", "") or "/opt/data/fleet-audit/reports"

#: How old ``finished_at`` may be and still describe the delivery in hand.
#: Measured rather than guessed: across 92 runs on the reference install the
#: gap from ``finish`` returning to the run's final response was 21s median and
#: 237s at worst, so this is roughly four times the observed ceiling.
_SILENCE_WINDOW_SECONDS = 900

#: How far back into the history ring ``_spans_several_repos`` looks. The ring
#: itself is bounded at 14 entries by ``audit_report.REPORT_HISTORY``, so this
#: reads all of it; it is named rather than inlined so the two ceilings are
#: visibly the same number and a change to one is a question about the other.
_SILENCE_WINDOW_RUNS = 14


def _spans_several_repos(runs: Path, latest: dict) -> bool:
    """Did more than one repository publish into this stream since the delivery began?

    The store is keyed by stream, and every audit SOP's §0 tells an unattended
    run to iterate ``managed_repos`` in sequence — one ``start``/``finish`` pair
    per repository, all writing here. The chat delivery, though, happens once
    for the whole turn, so ``latest.json`` is the *last* repository's verdict
    being asked to speak for all of them.

    That is the direction that loses findings. Audit the production repository
    first and find four criticals, then a clean staging repository second, and
    ``declared_silent`` reads staging's ``silent_ok: true`` and suppresses the
    entire reply. The four criticals are never posted, the run records ``ok``,
    and nothing is logged. ``recorded_summary`` fails the same way one notch
    down, replacing the model's multi-repo text with staging's one-liner.

    Aggregating the repositories into one verdict would be the richer answer,
    but a run group is a concept neither the store nor the scheduler has — so
    abstain instead. ``{}`` is "no opinion", the state both callers already
    handle by keeping the model's own text, which on a multi-repo run is the
    text that covers every repository. Single-repo installs, which is every
    install that has not registered a second ``managed_repos`` entry, see one
    repository in the window and are unaffected.

    Envelopes written before ``repo`` existed carry ``None``, which is unknown
    rather than a distinct repository; a store holding only those looks
    single-repo, exactly as it did before this function.
    """
    seen = {latest.get("repo")}
    try:
        recent = sorted(runs.glob("*.json"))[-_SILENCE_WINDOW_RUNS:]
    except OSError:
        return False
    for entry in recent:
        try:
            envelope = json.loads(entry.read_text("utf-8"))
            finished = datetime.datetime.fromisoformat(envelope["finished_at"])
        except Exception:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds()
        if 0 <= age <= _SILENCE_WINDOW_SECONDS:
            seen.add(envelope.get("repo"))
    return len({r for r in seen if r}) > 1


def _fresh_report(job_id: str) -> dict:
    """This audit stream's last run, if it is recent enough to be this delivery's.

    ``{}`` for everything else: a job that is not an audit stream, a store that
    is not there, an unreadable or timestampless report, and one left by an
    earlier run. Both callers treat ``{}`` as "no opinion" and fall back to the
    behaviour that shipped before them, so a bad read never costs a delivery.
    """
    # A job id is one path segment. It is parsed out of message text, so
    # anything else -- `../scratch/x` from a runtime job, an absolute path,
    # `.` -- is steering this read rather than naming a stream, and the join
    # would follow it off the store.
    #
    # `..` needs saying separately, because it is a path segment: pathlib keeps
    # it (`Path("..").name == ".."`, unlike `os.path.basename`), so the segment
    # test alone admits the one input the test exists to stop. It is a real
    # escape and not a theoretical one -- a `latest.json` one level above the
    # store, which is inside the agent's own writable volume, would otherwise
    # supply both the silence verdict and the text posted in its place.
    if not job_id or job_id == ".." or job_id != Path(job_id).name:
        return {}
    # Re-read rather than trust the import-time constant: a cron child that
    # sets the variable after this module loads writes to one store and would
    # otherwise be read from another, with no error either side.
    root = os.getenv("FLEET_AUDIT_REPORTS_DIR", "") or _REPORTS_DIR
    # A run holds the stream, so `latest.json` is the run *before* this one and
    # this delivery is not its. `audit_report.release_run_lock` unlinks
    # `started.json` as the last act of a successful `finish`, after the
    # envelope is written -- "a start record exists" and "a run holds the
    # stream" are the same fact, as that function's docstring puts it -- so the
    # file being here means no `finish` has completed since the current run
    # began.
    #
    # The age test alone cannot see that. It asks how old the record is, not
    # whose it is, and answers "recent enough" for any second run inside the
    # window: a re-trigger, a retry, a nudged schedule. What arrives then is
    # hermes' own failure text for the crashed run, and the two callers below
    # would silence it as a silent tick or, worse, replace it with the previous
    # run's `chat_summary` and post that as a success -- telling the channel
    # "0 new, 2 open" about a run that never got that far.
    #
    # Ordering makes this safe in the other direction too. The envelope lands
    # before the unlink, so the only window this closes wrongly is between those
    # two writes, and it closes it by returning {} -- "no opinion", the
    # pre-store behaviour both callers already fall back to.
    #
    # Inside the `try` with the read, not ahead of it, so a store that raises on
    # stat costs the opinion rather than the delivery.
    try:
        if (Path(root) / job_id / "started.json").exists():
            return {}
        report = json.loads((Path(root) / job_id / "latest.json").read_text("utf-8"))
        if _spans_several_repos(Path(root) / job_id / "runs", report):
            return {}
        finished = datetime.datetime.fromisoformat(report["finished_at"])
        # `finish` writes an aware stamp today. A naive one would otherwise
        # raise inside the blanket handler below and take the whole feature
        # inert fleet-wide, indistinguishable from having no store at all.
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - finished).total_seconds()
    except Exception:
        return {}
    return report if 0 <= age <= _SILENCE_WINDOW_SECONDS else {}


def recorded_summary(job_id: str) -> str:
    """The one line ``finish`` composed for the channel, or ``""`` to keep what came.

    ``chat_summary`` is built by the harness from the counts, the delta and the
    ledger URL, and every audit SOP ends by telling the run to make that string
    its entire final response. Measured against what the reference install
    actually posted, 36 of 38 non-silent runs wrote their own report instead —
    a median of 1.6kB of headed markdown per delivery, eight streams a day, in
    place of the line the harness had already composed.

    Nothing is lost by preferring the recorded line: all 38 of those responses
    carried the ledger URL, so the detail they added over it is detail the
    ledger holds, one click away, in the form the operator is meant to read it.

    ``""`` leaves the model's text alone, and that is the answer whenever there
    is no fresh report, whenever the harness recorded no summary — every report
    written before the field existed — and on a silent run, which
    :func:`declared_silent` has already stopped.
    """
    report = _fresh_report(job_id)
    if report.get("silent_ok"):
        return ""
    summary = str(report.get("chat_summary") or "").strip()
    return "" if not summary or is_silent_report(summary) else summary


def declared_silent(job_id: str) -> bool:
    """Did this audit stream's own run just decide it had nothing to say?

    :func:`is_silent_report` catches the marker. This catches the run that was
    supposed to emit the marker and wrote a paragraph instead — "the audit
    published successfully: ``silent_ok: true``, ledger #38 rewritten, nothing
    moved". Fourteen of the reference install's fifty-three silent runs posted
    prose like that. It carries no marker for the text test to find, and it is
    the exact message the silent path exists to prevent.

    The cause is structural rather than a wording problem, so the SOPs cannot
    fix it: every audit stream ends by telling the model to copy ``chat_summary``
    verbatim, which makes staying quiet an instruction to be followed rather
    than a decision already taken. Read the decision from where ``finish``
    wrote it down instead. ``job_id`` is the audit stream, which is also the
    report store's directory name, so a cron job that is not an audit stream
    finds nothing here and is unaffected. :func:`_fresh_report` is what keeps
    an earlier run's decision from silencing this one.
    """
    return bool(_fresh_report(job_id).get("silent_ok"))


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI's ``detail`` off an error response, as ``": <detail>"`` or ``""``.

    Best effort by design: the body is read once, may be empty or not JSON, and
    is never allowed to turn a delivery failure into an exception. Bounded
    because it becomes ``last_delivery_error``, which is stored per job run.
    """
    try:
        detail = (json.loads(exc.read().decode("utf-8", "replace")) or {}).get("detail")
    except Exception:
        return ""
    if not isinstance(detail, str) or not detail.strip():
        return ""
    return f": {detail.strip()[:200]}"


def _relay_receipt(response) -> dict:
    """The route's 2xx body, or ``{}`` if it could not be read.

    Three fields are read off it, and each says something a bare 200 does not:
    ``relay`` (``degraded`` when the report is in a channel but the delivery did
    not go as intended), ``relay_detail`` (which of the degradations it was, in
    words) and ``undelivered`` (the enabled chat platforms this report did not
    reach while another one did).

    Best effort, like :func:`_http_error_detail`: the body is read once and a
    delivery that worked is never turned into an exception by failing to parse
    the receipt for it. ``{}`` therefore means "the route did not say", not
    "nothing to report" — the caller acts only on explicit values.

    The whole body comes back rather than one field off it because the caller
    reads three, and a tuple carrying some of them is one the two ends have to
    keep in step. ``relay_detail`` in particular cannot be re-derived here:
    ``degraded`` covers a failed Chat Agent turn and a channel the send never
    reached, and only the route knows which happened and to which platform. A
    route too old to send it leaves the field absent, and the caller falls back
    to the sentence it used to print unconditionally.
    """
    try:
        body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _post(url: str, payload: dict, api_key: str) -> Tuple[Optional[str], dict]:
    """POST *payload* as JSON. ``(None, receipt)`` on success, else ``(why, {})``.

    Blocking, and called through :func:`asyncio.to_thread`. ``urllib`` rather
    than ``httpx`` keeps this module stdlib-only, so its tests run wherever the
    repo is checked out and not only inside the image.
    """
    try:
        # Building the Request is inside the try: a malformed CRON_REPORT_RELAY_URL
        # raises here, not at urlopen, and that is a delivery failure like any
        # other rather than an exception for the scheduler to catch.
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status >= 300:
                return f"chat relay answered HTTP {status}", {}
            return None, _relay_receipt(response)
    except urllib.error.HTTPError as exc:
        # The route names the failing leg in `detail` ("composed but not
        # delivered to google_chat"), which is the difference between a
        # last_delivery_error someone can act on and a bare status code.
        return f"chat relay answered HTTP {exc.code}{_http_error_detail(exc)}", {}
    except Exception as exc:  # URLError, socket timeout, malformed URL
        return f"chat relay unreachable: {type(exc).__name__}: {exc}", {}


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """POST one finished report to the Chat Agent relay.

    Called by ``tools/send_message_tool._send_via_adapter`` — the cron child has
    no in-process gateway adapter, which is precisely the case this hook exists
    for.

    ``chat_id``, ``thread_id``, ``media_files`` and ``force_document`` are
    accepted for signature parity and ignored: this sender names no destination
    at all. The route resolves them itself — one per enabled chat platform — and
    the Chat Agent decides what its own message says.

    The error strings become ``last_delivery_error``, so they name the condition
    and never the key.
    """
    job_id, title, report = parse_cron_wrapper(message)

    # A silent tick is a success, not a delivery. `github-repo-watcher` runs
    # every ten minutes and prints nothing on a clean sweep -- "Empty means a
    # clean, quiet tick and the scheduler posts nothing" (github_scan_gate.py)
    # -- and the relay route rejects an empty `report` with HTTP 400, which
    # would land in `last_delivery_error` 144 times a day on a watchdog that is
    # working as intended, inverting the audibility this whole change is for.
    #
    # Upstream stops it twice before it gets here: a no_agent job with empty
    # stdout returns SILENT_MARKER rather than its output, and `should_deliver`
    # is `bool(deliver_content.strip())`, so `_deliver_result` is never called.
    # Both are pinned by verify_chat_relay.py, because both are quiet. This is
    # the third stop, and it is the sibling's: `slack_relay_patch.py` guards the
    # identical case for the identical reason. A sender that behaves differently
    # from its sibling on the same input is a difference someone eventually has
    # to debug.
    #
    # Ahead of the credential check on purpose. There is nothing to send, so a
    # missing key is not this tick's problem, and reporting one would be the
    # same error-every-ten-minutes by another name.
    #
    # :func:`is_silent_report` also covers the marker upstream lets through
    # because a model emphasised it. Relaying that would be worse than posting
    # it raw: the route runs a Chat Agent turn over the text, and the Chat Agent
    # asked to relay "[SILENT]" writes a sentence about it.
    if is_silent_report(report) or declared_silent(job_id):
        logger.info(
            "chat relay: nothing to relay for job_id=%s — silent tick", job_id or "?"
        )
        return {"success": True, "platform": PLATFORM_NAME, "skipped": "empty_report"}

    # The other half of the same disagreement. Having decided it does have
    # something to say, an audit run is supposed to say it in the one line
    # `finish` composed — counts, delta, ledger URL — and usually writes its own
    # multi-section report instead. Prefer the recorded line where there is one,
    # so what reaches the channel is the same shape from every stream on every
    # run rather than whatever the turn felt like writing.
    summary = recorded_summary(job_id)
    if summary and summary != report:
        logger.info(
            "chat relay: job_id=%s — relaying the recorded summary in place of "
            "a %d-character composed report",
            job_id,
            len(report),
        )
        report = summary

    api_key = (os.getenv(API_KEY_ENV, "") or "").strip()
    if not api_key:
        return {
            "error": (
                f"chat relay: {API_KEY_ENV} is unset, so the Session KV server "
                f"cannot be authenticated"
            )
        }

    if not job_id:
        logger.warning(
            "chat relay: no cron wrapper on this delivery — relaying without a "
            "job id, so the report shares its profile's thread for the day"
        )

    payload = {
        "job_id": job_id,
        "profile": profile_name(),
        "title": title,
        "report": report,
        # Without this the route fans the composed report out to every enabled
        # platform, and `deliver: "all"` -- which posts the raw report to those
        # same platforms itself -- lands twice in each of them.
        "also_delivered_to": sibling_delivery_targets(job_id),
    }
    error, receipt = await asyncio.to_thread(_post, relay_url(), payload, api_key)
    if error:
        return {"error": error}

    # Two ways a 200 is still worth recording, and a run can hit both at once,
    # so they accumulate rather than returning early. In each case the report IS
    # in a channel: `error` is the only field of this dict the scheduler reads
    # and `last_delivery_error` is the only place a run record can carry the
    # fact, so the verdict goes there rather than into a log line nobody greps —
    # and each string says plainly that the report arrived, because `cronjob
    # list` showing a delivery error is otherwise read as "nothing was sent" and
    # invites a re-run that would post the same finding twice.
    #
    # Not doing this is what the relay was built to stop, one layer out: a front
    # door that has been down all week would otherwise produce run records
    # byte-identical to healthy ones.
    notes = []

    if receipt.get("relay") == "degraded":
        # Which degradation comes from the route, because the ones it reports
        # want opposite responses and this end cannot tell them apart: a failed
        # Chat Agent turn leaves raw text in every channel, a send that never
        # landed leaves one channel with nothing. The fallback is the sentence
        # that used to be printed for both, which is correct only for the
        # failed-turn case — so it is what a route too old to send a detail
        # gets, and nothing else.
        #
        # Bounded at the same 200 characters as :func:`_http_error_detail`, and
        # for the same reason: this ends up inside the `error` string the
        # scheduler stores as `last_delivery_error`, once per job run. Left
        # unbounded, a route that echoes a stack trace or the report itself
        # writes the whole thing into the job record.
        detail = str(receipt.get("relay_detail") or "").strip()[:200]
        notes.append(
            "chat relay degraded: the report was posted but "
            + (
                detail
                or "the Chat Agent turn failed, so the channel has the raw text "
                "marked [unrelayed] rather than a composed message"
            )
            + "."
        )

    # A fan-out that reached one platform and missed another — the audience that
    # heard nothing is exactly the silence #1094 is about. Separate from
    # `relay_detail` above rather than folded into it: a route can report a
    # clean `relay: "ok"` and still have missed a platform, and that case has no
    # detail string to carry it.
    undelivered = str(receipt.get("undelivered") or "").strip()
    if undelivered:
        notes.append(f"chat relay partial: the report did not reach {undelivered}.")

    if notes:
        message = " ".join(notes) + " Delivered — do not re-run to resend."
        logger.warning("chat relay: %s (job_id=%s)", message, job_id or "?")
        return {"error": message}

    logger.info(
        "chat relay: report handed to the Chat Agent (job_id=%s)", job_id or "?"
    )
    return {
        "success": True,
        "platform": PLATFORM_NAME,
        "chat_id": chat_id,
        "message_id": job_id or "cron-report",
    }


def check_requirements() -> bool:
    """Whether this platform can run at all. It is stdlib only, so: always."""
    return True


def is_connected(config: Any) -> bool:
    """Whether the relay is switched on in *this* process.

    ``load_gateway_config`` consults this before enabling a plugin platform, so
    returning False here is what keeps the gateway from registering a delivery
    target it has no adapter for. ``profile_cron_tick.py`` sets the variable for
    the cron children it spawns and nothing else does.
    """
    return bool((os.getenv(HOME_CHANNEL_ENV, "") or "").strip())


def _no_adapter(_config: Any):
    """There is no inbound side to build. ``create_adapter`` catches this."""
    raise NotImplementedError(
        "The chat relay is delivery-only: it has no gateway adapter. Reaching "
        "here means the platform was enabled in a process that then tried to "
        "start it — see the module docstring."
    )


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Chat Agent",
        adapter_factory=_no_adapter,
        check_fn=check_requirements,
        is_connected=is_connected,
        # Nothing to install: this module is stdlib only.
        install_hint="",
        # What makes `deliver: "chat"` a target the scheduler will resolve.
        cron_deliver_env_var=HOME_CHANNEL_ENV,
        # Out-of-process delivery: the cron child is not the gateway.
        standalone_sender_fn=standalone_send,
        # No chunking. The Chat Agent is composing a message from this text, not
        # posting it; the length bound that matters is CRON_REPORT_MAX_CHARS on
        # the relay route, which truncates to a single marked report rather than
        # splitting it into pieces that each start a separate turn.
        max_message_length=0,
        emoji="🗣️",
        # Never offer /update from a channel that has no inbound side.
        allow_update_command=False,
    )
