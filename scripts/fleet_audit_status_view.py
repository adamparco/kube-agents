#!/usr/bin/env python3
"""Render the fleet-audit report store as one table, eight rows.

Read side of docs/designs/fleet-audit-collectors-and-status.md §4.6. The store
lives on the agent pod's PVC (`/opt/data/fleet-audit/reports/<audit-id>/`), so
this tool reads it through one projection: it streams the harness's own
`report_status.py` into the pod (`kubectl exec -i … -- python3 -`) and parses
the single JSON document that comes back. Streaming the script in rather than
calling an installed path keeps the view working against any image, including
one built before the store existed.

The projection is also the offline format: `--json` emits exactly what `--file`
consumes, so the view is reproducible without a cluster.

The ENABLED and SCHEDULE columns come from the checked-in cron roster
(`agents/platform/cron/jobs.json`), not the runtime copy on the pod, and the
header says which file it read; a stream disabled at runtime therefore shows
its seed state.

Four flags the raw rows cannot be trusted without:
  - NO STORE: the store directory is absent, or this stream's files could not
    be read. "I could not look" is not "nothing is wrong", and the exit code
    says so too.
  - DIED: a run claimed the stream and never released it, from `started.json`'s
    presence and the projection's two-hour ceiling alone. No roster and no
    schedule parsing, so it fires within two hours on every cron shape and on
    kanban-dispatched runs that have no schedule at all.
  - NEVER: roster-enabled, the store was readable, and the stream has neither
    file — it has genuinely never run.
  - STALE: now is past the next expected fire plus slack. A silent stream is
    rendered loudly — this is the whole reason the surface exists.

Only STALE and NEVER consult the roster, so roster drift can no longer suppress
a death. `⚠` on STATUS marks a partial run; its coverage gaps print below the
table. Unknown status values render as themselves with the warning marker,
never as success. Model-influenced text is scrubbed of terminal control
characters at exactly one boundary, `scrub()`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROSTER = REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json"
PROJECTION_SCRIPT = (
    REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" /
    "report_status.py"
)
DEFAULT_NAMESPACE = "kubeagents-system"
# The agent container. The pod also runs `fluent-bit`, which has no store and
# no python3, so defaulting to the wrong one fails in a confusing way.
DEFAULT_CONTAINER = "platform-agent"
POD_SELECTOR = "app.kubernetes.io/name=platform-agent"
# How long past the expected fire a stream may run before it is called stale:
# the longest observed audit run is ~20 minutes, so an hour of slack flags
# real silence without paging on a slow morning.
STALE_SLACK = timedelta(hours=1)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b\x90\x9d]")


class ProjectionError(RuntimeError):
    """The projection could not be run at all — exit 2, never a blank table."""


def scrub(text: object) -> str:
    """The one boundary untrusted text crosses on its way to a terminal."""
    return _CONTROL.sub("�", str(text or ""))


def _oneline(text: object) -> str:
    return " ".join(str(text or "").split())


def discover_pod(namespace: str) -> list[str]:
    """Running agent pods in the namespace, sorted. Never empty — an empty
    result is the "no agent pod" failure, which the caller must not render as
    an empty fleet."""
    res = subprocess.run(
        [
            "kubectl", "get", "pods", "-n", namespace,
            "-l", POD_SELECTOR,
            "--field-selector", "status.phase=Running",
            "-o", "jsonpath={.items[*].metadata.name}",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise ProjectionError(
            f"no agent pod found in namespace {namespace}: "
            f"{_oneline(res.stderr) or 'kubectl get pods failed'}"
        )
    names = sorted((res.stdout or "").split())
    if not names:
        raise ProjectionError(
            f"no agent pod found in namespace {namespace} "
            f"(label {POD_SELECTOR}, phase Running)"
        )
    return names


def fetch_projection(pod: str, container: str, namespace: str) -> dict:
    """Run report_status.py inside the pod and parse its one JSON document.

    The script is piped in on stdin rather than invoked by path so the view
    reads a store on an image that ships no copy of it.
    """
    try:
        script = PROJECTION_SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectionError(
            f"projection script unreadable at {PROJECTION_SCRIPT}: {_oneline(exc)}"
        ) from exc
    res = subprocess.run(
        [
            "kubectl", "exec", "-i", pod,
            "-c", container, "-n", namespace,
            "--", "python3", "-",
        ],
        input=script,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise ProjectionError(
            f"pod {namespace}/{pod} found but exec failed: "
            f"{_oneline(res.stderr) or f'kubectl exec exited {res.returncode}'}"
        )
    return as_projection(res.stdout, f"{namespace}/{pod}")


def as_projection(text: str, origin: str) -> dict:
    """One JSON object out of the projection's stdout, or the exit-2 error.

    Anything the pod printed before the document — a shell warning, a Python
    traceback — lands here rather than becoming a table of empty rows.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        raise ProjectionError(
            f"the projection returned output that is not JSON, from {origin}: "
            f"{_oneline(text)[:200] or '(no output)'}"
        ) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("streams"), dict):
        raise ProjectionError(
            f"the projection returned output that is not JSON, from {origin}: "
            "JSON without a `streams` object"
        )
    return doc


def load_roster(path: Path) -> dict[str, dict]:
    try:
        jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs") or []
    except (OSError, ValueError):
        return {}
    out = {}
    for job in jobs:
        if "fleet-audit" in (job.get("skills") or []):
            out[str(job.get("id", ""))] = {
                "enabled": bool(job.get("enabled")),
                "expr": str(((job.get("schedule") or {}).get("expr")) or ""),
            }
    return out


def next_fire(expr: str, after: datetime) -> datetime | None:
    """Next fire for the roster's cron shapes: `M H * * *` and `M H * * D`.

    The governance roster only uses these two forms. Anything fancier returns
    None and the STALE flag abstains for that stream rather than guessing.
    """
    parts = expr.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
        dows = None if parts[4] == "*" else {int(d) % 7 for d in parts[4].split(",")}
    except ValueError:
        return None
    candidate = after.replace(minute=minute, second=0, microsecond=0)
    candidate = candidate.replace(hour=hour)
    if candidate <= after:
        candidate += timedelta(days=1)
    for _ in range(8):
        # cron dow: 0=Sunday; Python: Monday=0 → cron = (weekday+1) % 7
        if dows is None or ((candidate.weekday() + 1) % 7) in dows:
            return candidate
        candidate += timedelta(days=1)
    return None


def parse_iso(value: object) -> datetime | None:
    """An aware timestamp, or None. A naive one is not comparable to `now`."""
    try:
        at = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return at if at.tzinfo else None


def local_time(at: datetime | None, tz: timezone | None = None) -> str:
    """The system's local zone, 12-hour, lowercase am/pm. `tz` exists for
    tests; every real caller leaves it None and gets the machine's zone."""
    if at is None:
        return "—"
    text = at.astimezone(tz).strftime("%b %d %I:%M %p").replace(" 0", " ")
    return text[:-2] + text[-2:].lower()


def duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def count_cell(value: object) -> str:
    """A URL list rendered as its length. The envelope stores `prs_opened` as
    URLs; the column has always been a number and a cell has no room for
    three GitHub links."""
    if isinstance(value, list):
        return str(len(value))
    return str(value) if isinstance(value, int) else "—"


def flags_for(stream: dict, job: dict, now: datetime, root_exists: bool) -> list[str]:
    """The four flags, in severity order.

    DIED reads off the projection's liveness alone, so a roster that has
    drifted from runtime cannot suppress the death of a running stream — the
    defect §4.6 names, where `at is None` → `expected is None` → `stale is
    False` left every failure rendering as the same calm blank row.
    """
    flags = []
    unreadable = not root_exists or bool(stream.get("error"))
    if unreadable:
        flags.append("NO STORE")
    liveness = stream.get("liveness") or "never"
    if liveness == "died":
        flags.append("DIED")
    enabled = bool(job.get("enabled"))
    if liveness == "never" and enabled and not unreadable:
        flags.append("NEVER")
    if enabled:
        at = parse_iso((stream.get("latest") or {}).get("finished_at"))
        expected = next_fire(job.get("expr", ""), at) if at else None
        if expected is not None and now > expected + STALE_SLACK:
            flags.append("STALE")
    return flags


def issue_ref(url: object) -> str:
    match = re.search(r"/issues/(\d+)$", str(url or ""))
    return f"#{match.group(1)}" if match else "—"


def exit_code(projection: dict) -> int:
    """1 when the store could not be read — the case the retired ConfigMap
    reported as a healthy fleet for thirty hours."""
    if not projection.get("root_exists"):
        return 1
    streams = projection.get("streams") or {}
    return 1 if any((s or {}).get("error") for s in streams.values()) else 0


def unreadable_reason(projection: dict) -> str | None:
    if not projection.get("root_exists"):
        return f"store directory absent on the pod: {projection.get('root')}"
    bad = sorted(
        audit_id
        for audit_id, stream in (projection.get("streams") or {}).items()
        if (stream or {}).get("error")
    )
    return f"unreadable stream files: {', '.join(bad)}" if bad else None


GAP_WIDTH = 160


def clip_gap(text: str, width: int = GAP_WIDTH) -> str:
    """One line per gap, because a collector writes these at whatever length.

    The live install's are four-sentence paragraphs explaining a refused
    `gcloud` flag, and six of them push the table off the top of the terminal —
    the one thing this view exists to show. The whole text is still in the
    envelope, which is what `fleet-audit-reports` reads; this is the index.
    """
    line = " ".join(str(text).split())
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def render(
    projection: dict,
    roster: dict,
    now: datetime,
    roster_path: str,
    source: str,
) -> str:
    streams = projection.get("streams") or {}
    root_exists = bool(projection.get("root_exists"))
    ids = sorted(set(roster) | set(streams))
    header = [
        "STREAM", "ENABLED", "SCHEDULE", "LAST RUN (local)", "STATUS",
        "FINDINGS", "Δ", "PRS", "COLLECT", "INSPECT", "PUBLISH", "ISSUE", "FLAGS",
    ]
    rows = [header]
    gaps: list[str] = []
    for audit_id in ids:
        stream = streams.get(audit_id) or {}
        job = roster.get(audit_id) or {}
        latest = stream.get("latest") or {}
        status = scrub(latest.get("status") or stream.get("error") or "never ran")
        if stream.get("liveness") == "running":
            status = "running…"
        known = {"CLEAN", "OPENED", "UPDATED", "REMEDIATED", "running…", "never ran"}
        if latest.get("partial"):
            status += " ⚠"
        elif status not in known:
            status += " ?"  # unknown outcome renders as a warning, never green
        delta = (
            f"+{latest['new']} / −{latest['resolved']}"
            if isinstance(latest.get("new"), int)
            and isinstance(latest.get("resolved"), int)
            else "—"
        )
        findings = latest.get("findings")
        crit = latest.get("critical")
        findings_cell = (
            f"{findings} ({crit} c)" if isinstance(findings, int) and crit else
            str(findings) if isinstance(findings, int) else "—"
        )
        rows.append([
            audit_id,
            "yes" if job.get("enabled") else ("no" if job else "?"),
            job.get("expr", "?"),
            local_time(parse_iso(latest.get("finished_at"))),
            status,
            findings_cell,
            delta,
            count_cell(latest.get("prs_opened")),
            duration(latest.get("collect_s")),
            duration(latest.get("inspect_s")),
            duration(latest.get("publish_s")),
            issue_ref(latest.get("issue_url")),
            ",".join(flags_for(stream, job, now, root_exists)) or "",
        ])
        for gap in latest.get("coverage_gaps") or []:
            gaps.append(f"{audit_id}: {scrub(gap)}")
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    out = [
        f"fleet-audit status — store: {scrub(projection.get('root'))} ({source})",
        f"roster: {roster_path}",
        "",
    ]
    out += lines
    if gaps:
        out += ["", "coverage gaps:"] + [f"  {clip_gap(g)}" for g in gaps]
    reason = unreadable_reason(projection)
    if reason:
        out += ["", f"! {scrub(reason)}"]
    return "\n".join(out)


def load_projection(args: argparse.Namespace) -> tuple[dict, str]:
    """The projection and a one-line description of where it came from."""
    if args.file:
        if args.file == "-":
            return as_projection(sys.stdin.read(), "stdin"), "stdin"
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectionError(f"could not read {args.file}: {_oneline(exc)}") from exc
        return as_projection(text, args.file), f"file {args.file}"
    pod = args.pod
    if not pod:
        pods = discover_pod(args.namespace)
        pod = pods[0]
        if len(pods) > 1:
            print(
                f"note: {len(pods)} Running agent pods in {args.namespace}; "
                f"reading {pod} (--pod overrides)",
                file=sys.stderr,
            )
    return (
        fetch_projection(pod, args.container, args.namespace),
        f"{args.namespace}/{pod} [{args.container}]",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--pod",
        help=f"agent pod to read (default: discover by {POD_SELECTOR})",
    )
    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help=f"container to exec into (default: {DEFAULT_CONTAINER})",
    )
    parser.add_argument(
        "--file",
        help="read the projection's JSON from a file instead of the pod ('-' for stdin)",
    )
    parser.add_argument(
        "--roster",
        default=str(DEFAULT_ROSTER),
        help="cron roster for ENABLED/SCHEDULE (default: the checked-in seed)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the projection as JSON — exactly what --file consumes",
    )
    args = parser.parse_args(argv)

    try:
        projection, source = load_projection(args)
    except ProjectionError as exc:
        print(f"fleet-audit view: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(projection, indent=2, sort_keys=True))
        return exit_code(projection)
    roster = load_roster(Path(args.roster))
    print(render(projection, roster, datetime.now(timezone.utc), args.roster, source))
    return exit_code(projection)


if __name__ == "__main__":
    sys.exit(main())
