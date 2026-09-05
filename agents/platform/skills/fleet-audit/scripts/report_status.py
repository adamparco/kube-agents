#!/opt/hermes/.venv/bin/python3
"""report_status.py — the read side of the fleet-audit report store.

Projects `reports/<audit-id>/{started.json, latest.json, runs/}` into one small
JSON document: each stream's liveness, the last run's outcome without its
findings document, and the run ring's filenames. Design of record:
docs/designs/fleet-audit-collectors-and-status.md §4.5 (the two status files
and the liveness rule) and §4.6 (the projection the view consumes).

Two consumers, each pinning one property of this file:

- `scripts/fleet_audit_status_view.py` runs it off-pod by streaming this file
  into the pod on stdin (`kubectl exec -i … -- python3 -`), which keeps the
  view working against an image built before this change. Streamed stdin has
  no `__file__`, so nothing here may reference one, import a sibling module,
  or reach outside the standard library.
- `fleet-audit-reports/scripts/report_query.py` imports the reading helpers
  below so the two do not grow two parsers of the same files (§4.9). The
  module therefore does no work at import time: every entry point is a
  function and the CLI sits behind `__main__`.

`root_exists` and the per-stream `error` are the keys that make failure
legible. The status ConfigMap this replaces had its every write refused for
thirty hours while the view printed a calm table of streams that had "never
run" and exited 0; "I could not look" must never render as "nothing is wrong".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

# Identical to audit_report.py's, and deliberately not imported from it: this
# file is streamed into a pod whose image may predate that module's copy.
REPORTS_DIR = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or "/opt/data/fleet-audit/reports"

# The run lock's ceiling (audit_report.RUN_LOCK_CEILING_S), duplicated for the
# same reason: an unfinished run older than this is DIED by §4.5's rule, and
# the two numbers must be the same number or the view and the lock disagree
# about whether a stream is stealable.
CEILING_S = 7200

# Always present on a projected `latest`, null when the envelope lacks them, so
# a reader never has to tell an absent key from a null one. Everything else the
# envelope carries except `document` rides along untouched (`_project_latest`),
# so a key added to the envelope later reaches a reader without an edit here.
LATEST_KEYS = (
    "audit_id",
    "finished_at",
    "status",
    "issue_number",
    "issue_url",
    "partial",
    "coverage_gaps",
    "collect_s",
    "inspect_s",
    "publish_s",
    "prs_opened",
    "prs_closed",
    "silent_ok",
    "id_scheme",
)

# Keys the projection never carries, so that a key added to `report_envelope`
# later still reaches the view without an edit here while these four do not.
# `document` is the whole findings document and runs to megabytes; the three id
# lists are already summarised as `new`/`resolved`/`current`, and the reader
# that wants the ids themselves reads the envelope through `load_latest`
# instead of through this projection.
_NEVER_PROJECTED = frozenset({"document", "new_ids", "resolved_ids", "current_ids"})


def reports_root(root: str | None = None) -> str:
    """The store root: argument, then environment, then the module default.

    The environment is re-read here rather than trusted from import time so a
    caller that sets `FLEET_AUDIT_REPORTS_DIR` after importing the module gets
    the root it set; patching `REPORTS_DIR` on the module works too.
    """
    return str(root or os.environ.get("FLEET_AUDIT_REPORTS_DIR") or REPORTS_DIR)


def stream_ids(root: str) -> list[str]:
    """Every stream directory under the root, sorted; [] when it is missing.

    A stream is a directory, so the lock's `.claim-*`/`.steal-*` files and any
    stray temp file never read as a stream. Any other OSError propagates —
    `project` turns "the root is there but unreadable" into `root_exists:
    false` rather than into an empty fleet.
    """
    try:
        with os.scandir(root) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir())
    except FileNotFoundError:
        return []


def read_json(path: str) -> object:
    """One JSON file, parsed. Raises OSError or ValueError; callers catch."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_object(path: str) -> dict | None:
    """A JSON object, None when the file is absent, ValueError when it is not
    an object. A file holding a list parses fine and is still corrupt."""
    try:
        value = read_json(path)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def load_started(root: str, audit_id: str) -> dict | None:
    """The raw `started.json`. Its presence is the claim: a run holds this
    stream (§4.5), because `finish` releases the lock by unlinking it."""
    return _read_object(os.path.join(root, audit_id, "started.json"))


def load_latest(root: str, audit_id: str) -> dict | None:
    """The raw, whole `latest.json`, `document` included.

    The projection strips `document`; report_query.py needs it, so this helper
    is the one that does not.
    """
    return _read_object(os.path.join(root, audit_id, "latest.json"))


def list_runs(root: str, audit_id: str) -> list[str]:
    """Filenames in `runs/`, sorted ascending — which is time order, because
    the stamp is UTC. [] when the ring does not exist yet."""
    try:
        names = os.listdir(os.path.join(root, audit_id, "runs"))
    except FileNotFoundError:
        return []
    # `_atomic_write` replaces from a `.tmp` file in the same directory, so a
    # read that lands mid-write must not report the temp file as a run.
    return sorted(name for name in names if name.endswith(".json"))


def load_run(root: str, audit_id: str, name: str) -> dict | None:
    """One ring entry, whole. None when that stamp is not in the ring."""
    return _read_object(os.path.join(root, audit_id, "runs", name))


def liveness(
    started: dict | None,
    latest: dict | None,
    now_epoch: float,
    ceiling: float = CEILING_S,
    error: str | None = None,
) -> str:
    """Which of the five states this stream is in (§4.5).

    Presence, not timestamp comparison: `finish` releases the lock, so "a start
    record exists" and "a run holds the stream" are the same fact and there is
    no ordering between two files to get wrong. `started` may be the raw claim
    or the projection's copy of it — both carry `epoch`.
    """
    if error:
        return "error"
    if started is not None:
        epoch = _as_float(started.get("epoch") if isinstance(started, dict) else None)
        if epoch is None:
            # A claim with no readable start time can never age out, so it
            # would wedge the stream forever. audit_report._claim_is_dead reads
            # the same corrupt claim as already dead; this agrees with it.
            return "died"
        age = now_epoch - epoch
        # The future-dated half mirrors `_claim_is_dead` too: a start time two
        # hours ahead is not credible, and honouring it holds the stream for
        # good on a clock that stepped backwards.
        #
        # `>=`, not `>`, for the same reason as `_claim_is_dead`: this surface
        # and the lock must not disagree about whether a run holds the stream
        # (§4.5). At exactly the ceiling the lock is stealable, so the table
        # says DIED rather than showing a run as healthy that the next `start`
        # will step over.
        if age >= ceiling or age <= -ceiling:
            return "died"
        return "running"
    if latest is not None:
        return "completed"
    return "never"


def project(root: str | None = None, now: float | datetime | None = None) -> dict:
    """The whole store as one document the view can render off-pod.

    One unreadable stream may not cost the other seven, so each stream's reads
    are wrapped and a failure becomes that stream's `error` plus `liveness:
    "error"` while the sweep continues.
    """
    root = reports_root(root)
    now_epoch = _now_epoch(now)
    root_exists = os.path.isdir(root)
    try:
        ids = stream_ids(root)
    except OSError:
        # A root that is present but cannot be listed is a store the view could
        # not read, not a fleet with no streams — and `root_exists` is the key
        # its exit code hangs on.
        ids, root_exists = [], False
    return {
        "root": root,
        "root_exists": root_exists,
        "generated_at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "ceiling_s": CEILING_S,
        "streams": {
            audit_id: _project_stream(root, audit_id, now_epoch) for audit_id in ids
        },
    }


def _project_stream(root: str, audit_id: str, now_epoch: float) -> dict:
    started: dict | None = None
    latest: dict | None = None
    runs: list[str] = []
    error: str | None = None
    try:
        started = load_started(root, audit_id)
    except (OSError, ValueError) as exc:
        error = _failure("started.json", exc)
    try:
        latest = load_latest(root, audit_id)
    except (OSError, ValueError) as exc:
        error = error or _failure("latest.json", exc)
    try:
        runs = list_runs(root, audit_id)
    except OSError as exc:
        error = error or _failure("runs/", exc)
    return {
        "started": _project_started(started, now_epoch),
        "latest": _project_latest(latest),
        "runs": runs,
        "liveness": liveness(started, latest, now_epoch, error=error),
        "error": error,
    }


def _project_started(claim: dict | None, now_epoch: float) -> dict | None:
    if claim is None:
        return None
    epoch = _as_float(claim.get("epoch"))
    return {
        "t0": _as_str(claim.get("t0")),
        "epoch": epoch,
        "age_s": None if epoch is None else round(now_epoch - epoch, 1),
        "pid": _as_int(claim.get("pid")),
        "nonce": _as_str(claim.get("nonce")),
    }


def _project_latest(envelope: dict | None) -> dict | None:
    """The last run's envelope minus `document`, plus the counts derived from
    it. `document` is the one key that never crosses this boundary: it carries
    the whole findings document, un-clipped, and envelopes run to megabytes.

    Every count is guarded into null rather than a number, because a malformed
    envelope must read as "unknown" and not as "zero findings".
    """
    if envelope is None:
        return None
    row: dict = {key: envelope.get(key) for key in LATEST_KEYS}
    row.update(
        {
            key: value
            for key, value in envelope.items()
            if key not in _NEVER_PROJECTED and key not in row
        }
    )
    row["new"] = _count(envelope.get("new_ids"))
    row["resolved"] = _count(envelope.get("resolved_ids"))
    row["current"] = _count(envelope.get("current_ids"))
    document = envelope.get("document")
    document = document if isinstance(document, dict) else {}
    findings = document.get("findings")
    row["findings"] = _count(findings)
    row["critical"] = (
        sum(1 for finding in findings if _is_critical(finding))
        if isinstance(findings, list)
        else None
    )
    scope = document.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    row["clusters"] = _count(scope.get("clusters"))
    row["skipped"] = _count(scope.get("skipped"))
    return row


def _is_critical(finding: object) -> bool:
    return (
        isinstance(finding, dict)
        and str(finding.get("severity", "")).strip().lower() == "critical"
    )


def _count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    # bool is an int; a pid of `true` is corruption, not process 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _failure(label: str, exc: Exception) -> str:
    """One line, always — the view prints it in a table cell."""
    return f"{label}: {' '.join(str(exc).split()) or type(exc).__name__}"


def _now_epoch(now: float | datetime | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project the fleet-audit report store as one JSON document."
    )
    parser.add_argument(
        "--root",
        help=f"store root to read (default: $FLEET_AUDIT_REPORTS_DIR or {REPORTS_DIR})",
    )
    args = parser.parse_args(argv)
    # Exit 0 even with no store: `root_exists: false` is the answer, and the
    # view decides what a missing store costs.
    print(json.dumps(project(args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
