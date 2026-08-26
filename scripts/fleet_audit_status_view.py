#!/usr/bin/env python3
"""Render the fleet-audit status ConfigMap as one table, eight rows.

Read side of docs/designs/fleet-audit-collectors-and-status.md §4.6. The
ConfigMap (`kube-agents-fleet-audit-status`) is written by the fleet-audit
harness at `start` and `finish`; this tool only reads it — through `kubectl`,
or from `--file` for offline use — and joins it with the checked-in cron
roster for the ENABLED and SCHEDULE columns. The roster it reads is the seed
(`agents/platform/cron/jobs.json`), not the runtime copy on the pod's PVC, and
the header says which file it read; a stream disabled at runtime therefore
shows its seed state, while the STALE flag still fires on the missing runs.

Three flags the raw rows cannot be trusted without:
  - STALE: now is past the next expected fire plus slack. A silent stream is
    rendered loudly — this is the whole reason the surface exists.
  - DIED: the newest row is a `started` stub and the run is stale, so the run
    began and never reached `finish`. A healthy in-flight run never trips it.
  - `⚠` on STATUS: the run was partial; its coverage gaps print below the
    table.

Unknown status values render as themselves with the warning marker, never as
success. Model-influenced text (the `note` column) is scrubbed of terminal
control characters at exactly one boundary, `scrub()`.
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
DEFAULT_CONFIGMAP = "kube-agents-fleet-audit-status"
DEFAULT_ROSTER = REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json"
# How long past the expected fire a stream may run before it is called stale:
# the longest observed audit run is ~20 minutes, so an hour of slack flags
# real silence without paging on a slow morning.
STALE_SLACK = timedelta(hours=1)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b\x90\x9d]")


def scrub(text: object) -> str:
    """The one boundary untrusted text crosses on its way to a terminal."""
    return _CONTROL.sub("�", str(text or ""))


def read_configmap(name: str, namespace: str) -> dict:
    res = subprocess.run(
        ["kubectl", "get", "configmap", name, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise SystemExit(
            f"could not read ConfigMap {namespace}/{name}: "
            f"{(res.stderr or '').strip() or 'kubectl failed'}"
        )
    return json.loads(res.stdout)


def load_streams(cm: dict) -> dict[str, dict]:
    """Per-stream documents out of the ConfigMap; unparseable keys become an
    error row rather than a crash or a silent omission."""
    streams: dict[str, dict] = {}
    for key, raw in (cm.get("data") or {}).items():
        if not key.endswith(".json"):
            continue
        audit_id = key[: -len(".json")]
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            streams[audit_id] = doc
        except ValueError:
            streams[audit_id] = {"error": "unparseable stream document"}
    return streams


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


def parse_at(row: dict) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(row.get("at", "")))
        return at if at.tzinfo else None
    except ValueError:
        return None


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


def flags_for(doc: dict, roster: dict, now: datetime) -> list[str]:
    flags = []
    last = doc.get("last") or {}
    at = parse_at(last)
    expected = None
    if roster.get("enabled") and at is not None:
        expected = next_fire(roster.get("expr", ""), at)
    stale = expected is not None and now > expected + STALE_SLACK
    if stale:
        flags.append("STALE")
        if last.get("phase") == "started":
            flags.append("DIED")
    return flags


def issue_ref(url: object) -> str:
    match = re.search(r"/issues/(\d+)$", str(url or ""))
    return f"#{match.group(1)}" if match else "—"


def render(streams: dict, roster: dict, now: datetime, roster_path: str) -> str:
    ids = sorted(set(roster) | set(streams))
    header = [
        "STREAM", "ENABLED", "SCHEDULE", "LAST RUN (local)", "STATUS",
        "FINDINGS", "Δ", "PRS", "INSPECT", "PUBLISH", "ISSUE", "FLAGS",
    ]
    rows = [header]
    gaps: list[str] = []
    for audit_id in ids:
        doc = streams.get(audit_id) or {}
        job = roster.get(audit_id) or {}
        last = doc.get("last") or {}
        status = scrub(last.get("status") or doc.get("error") or "never ran")
        if last.get("phase") == "started":
            status = "running…"
        known = {"CLEAN", "OPENED", "UPDATED", "REMEDIATED", "running…", "never ran"}
        if last.get("partial"):
            status += " ⚠"
        elif status not in known:
            status += " ?"  # unknown outcome renders as a warning, never green
        delta = (
            f"+{last['new']} / −{last['resolved']}"
            if isinstance(last.get("new"), int) and isinstance(last.get("resolved"), int)
            else "—"
        )
        findings = last.get("findings")
        crit = last.get("critical")
        findings_cell = (
            f"{findings} ({crit} c)" if isinstance(findings, int) and crit else
            str(findings) if isinstance(findings, int) else "—"
        )
        rows.append([
            audit_id,
            "yes" if job.get("enabled") else ("no" if job else "?"),
            job.get("expr", "?"),
            local_time(parse_at(last)),
            status,
            findings_cell,
            delta,
            str(last.get("prs_opened", "—")),
            duration(last.get("inspect_s")),
            duration(last.get("publish_s")),
            issue_ref(last.get("issue_url")),
            ",".join(flags_for(doc, job, now)) or "",
        ])
        if last.get("partial") and last.get("note"):
            gaps.append(f"{audit_id}: {scrub(last['note'])}")
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    out = [f"fleet-audit status — roster: {roster_path}", ""]
    out += lines
    if gaps:
        out += ["", "coverage gaps:"] + [f"  {g}" for g in gaps]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", "-n", default="kubeagents-system")
    parser.add_argument("--configmap", default=DEFAULT_CONFIGMAP)
    parser.add_argument(
        "--file", help="read the ConfigMap from a JSON file instead of kubectl"
    )
    parser.add_argument(
        "--roster",
        default=str(DEFAULT_ROSTER),
        help="cron roster for ENABLED/SCHEDULE (default: the checked-in seed)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the merged documents as JSON"
    )
    args = parser.parse_args(argv)

    if args.file:
        cm = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        cm = read_configmap(args.configmap, args.namespace)
    streams = load_streams(cm)
    roster = load_roster(Path(args.roster))
    if args.json:
        print(json.dumps({"streams": streams, "roster": roster}, indent=2))
        return 0
    print(render(streams, roster, datetime.now(timezone.utc), args.roster))
    return 0


if __name__ == "__main__":
    sys.exit(main())
