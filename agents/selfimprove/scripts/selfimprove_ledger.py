#!/usr/bin/env python3
"""The self-improvement ledger: what has been found, how often, and what was filed.

The loop is hourly and stateless -- a Job that scaffolds an emptyDir, runs one
agent turn, and exits -- so everything that has to survive a run lives here. Two
things do: the occurrence counts the gate reads (docs/designs/self-improvement.md
sec. 7.2), and the record of which findings already became a pull request, so the
next run recognises its own work rather than filing it again.

Storage is one ConfigMap in the install's own namespace, granted by
`resourceNames` on a Role so the grant cannot reach a second object. It is
deliberately NOT a ConfigMap the agent's Deployment references: the operator
SHA256-hashes the ConfigMaps it owns into the agent's pod-template annotations,
so a ledger in that set would roll the Platform Agent on every write -- an
hourly restart caused by the thing that is supposed to be observing it without
touching it (sec. 10).

Everything above `load`/`save` is pure: no Kubernetes import at module scope, so
the fingerprint, the rolling count and the gate are unit-testable without a
cluster. tests/test_selfimprove_ledger.py is where that is exercised.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

LEDGER_VERSION = 1

#: Ordered worst-first, which is also the order the gate reports in.
SEVERITIES = ("critical", "high", "medium", "low")

#: The seven signal classes of sec. 4. `forge` is issue/PR creation; `other` is
#: the catch-all row that still has to clear the same evidence bar.
SIGNALS = (
    "errors",
    "inefficiency",
    "latency",
    "responses",
    "delivery",
    "forge",
    "other",
)

#: Occurrence counts older than this stop contributing. The gate is expressed
#: per day, so the window is a day.
COUNT_WINDOW_HOURS = 24

#: Runs kept for the "did this loop stop finding things, or stop running?"
#: question. Small on purpose: the ledger is a ConfigMap, capped at 1MiB by the
#: API server, and an unbounded history is the only part of it that grows
#: without a finding behind it.
RUN_HISTORY = 48


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def to_iso(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> Optional[_dt.datetime]:
    """Parse a ledger timestamp, returning None rather than raising.

    A ledger is data the previous run wrote and a human may have edited with
    `kubectl edit`. One unparseable timestamp must not take the run down, so
    every caller treats None as "outside the window" -- the conservative answer,
    since it withholds a promotion rather than granting one.
    """
    if not text:
        return None
    try:
        cleaned = text.strip().replace("Z", "+00:00")
        parsed = _dt.datetime.fromisoformat(cleaned)
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

#: Substitutions that turn one occurrence of a finding into the class of
#: findings it belongs to.
#:
#: Ordered, and the order is the whole correctness argument: each pattern must
#: run before any looser one that would eat its input. `<TS>` before `<HEX>`
#: before the digit sweep, or a UUID is chewed into `<N>-<N>-<N>` and two
#: different shapes collide on the dashes that survive.
#:
#: Every pattern is case-insensitive even though `normalise` lowercases first.
#: They were not, and it was silent: `\d{4}-\d{2}-\d{2}[T ]…` cannot match
#: `2026-08-22t09:14:03z`, so every timestamp fell through to the digit sweep
#: and came out as `<N>-<N>-22t09:<N>:03z` -- stable within one second and
#: different from the next sighting, which is the exact failure fingerprinting
#: exists to prevent. Belt and braces: the flag costs nothing and the
#: lowercasing is now not load-bearing.
_NORMALISERS: Tuple[Tuple[re.Pattern, str], ...] = (
    # ISO-8601 timestamps, with or without fractional seconds and offset.
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            re.I,
        ),
        "<TS>",
    ),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    # A Kubernetes pod name's generated suffix: <name>-<replicaset>-<pod>.
    (re.compile(r"-[0-9a-f]{8,10}-[0-9a-z]{5}\b", re.I), "-<POD>"),
    # Bare hex runs long enough to be an id rather than a number: git shas,
    # session ids, trace ids.
    (re.compile(r"\b[0-9a-f]{7,}\b", re.I), "<HEX>"),
    (re.compile(r"\s+"), " "),
)

#: Applied to locations only. A line number drifts on every commit that touches
#: the file above it, so `platformagent_manifests.go:412` and
#: `platformagent_manifests.go:418` are the same place and must fingerprint the
#: same.
#:
#: Deliberately NOT applied to titles, and this is the one judgement call in the
#: module. Sweeping digits out of a title collapses "skill 1 fails" and
#: "skill 2 fails" into one finding whose count is the sum of two unrelated
#: bugs -- and an inflated count is what the gate reads, so over-normalising
#: here does not merely lose information, it manufactures promotions. The
#: counts a title might legitimately carry ("retried 40 times") are the ones
#: SOUL.md sec. 4 already tells the agent to put in the evidence instead, so
#: the case this rule would have covered should not arise.
_LOCATION_NORMALISERS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r":\d+(?::\d+)?\b"), ":<LINE>"),
)


def normalise(text: str) -> str:
    """Strip the parts of a message that differ between two sightings of one bug.

    The point is stability across runs, not readability: the result is hashed,
    never shown. Over-normalising collapses two real findings into one, which
    the ledger cannot tell you about; under-normalising files the same finding
    every hour, which it very much can. When in doubt this errs towards
    under-normalising -- a duplicate is visible in the ledger, a collision is
    not.
    """
    out = (text or "").strip().lower()
    for pattern, replacement in _NORMALISERS:
        out = pattern.sub(replacement, out)
    return out.strip()


def normalise_location(text: str) -> str:
    """`normalise`, plus the line-number collapse. See _LOCATION_NORMALISERS."""
    out = normalise(text)
    for pattern, replacement in _LOCATION_NORMALISERS:
        out = pattern.sub(replacement, out)
    return out.strip()


def fingerprint(signal: str, title: str, location: str = "") -> str:
    """Identity for a finding across runs.

    Signal class, normalised title and code location, because those are the
    three things that stay the same when the same bug fires twice. Severity is
    deliberately NOT in it: a finding that gets re-graded is the same finding,
    and putting the grade in the identity would reset its occurrence count every
    time the agent changed its mind.
    """
    material = "|".join(
        [(signal or "other").strip().lower(), normalise(title), normalise_location(location)]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# The ledger document
# --------------------------------------------------------------------------


def empty_ledger() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "findings": {}, "runs": []}


def coerce(raw: Any) -> Dict[str, Any]:
    """Accept whatever is in the ConfigMap and return something with the right shape.

    A ledger that has been hand-edited into nonsense is recoverable -- the
    counts restart -- while a run that dies on it is not, because the run that
    would have rewritten the file is the one that crashed.
    """
    if not isinstance(raw, dict):
        return empty_ledger()
    out = empty_ledger()
    findings = raw.get("findings")
    if isinstance(findings, dict):
        for key, value in findings.items():
            if isinstance(value, dict):
                out["findings"][str(key)] = value
    runs = raw.get("runs")
    if isinstance(runs, list):
        out["runs"] = [r for r in runs if isinstance(r, dict)][-RUN_HISTORY:]
    return out


def occurrences_in_window(entry: Dict[str, Any], now: _dt.datetime, hours: int = COUNT_WINDOW_HOURS) -> int:
    """How many times this finding was observed in the trailing window.

    Sightings are (timestamp, count) pairs rather than one row per occurrence:
    a run that greps a log and finds an error 4,000 times records one sighting
    of 4,000, not 4,000 rows in a ConfigMap.
    """
    cutoff = now - _dt.timedelta(hours=hours)
    total = 0
    for sighting in entry.get("sightings", []):
        if not isinstance(sighting, dict):
            continue
        at = from_iso(sighting.get("at", ""))
        if at is None or at < cutoff:
            continue
        try:
            total += max(0, int(sighting.get("count", 1)))
        except (TypeError, ValueError):
            total += 1
    return total


def prune(ledger: Dict[str, Any], now: _dt.datetime, retain_days: int = 30) -> None:
    """Drop sightings outside the window and findings nothing has seen for a month.

    A promoted finding is kept regardless of age: its pull-request record is
    what stops the loop re-filing it after the cooldown, and forgetting that is
    strictly worse than carrying the row.
    """
    sighting_cutoff = now - _dt.timedelta(hours=COUNT_WINDOW_HOURS)
    finding_cutoff = now - _dt.timedelta(days=retain_days)
    for key in list(ledger["findings"].keys()):
        entry = ledger["findings"][key]
        kept = []
        for sighting in entry.get("sightings", []):
            at = from_iso(sighting.get("at", "")) if isinstance(sighting, dict) else None
            if at is not None and at >= sighting_cutoff:
                kept.append(sighting)
        entry["sightings"] = kept
        last_seen = from_iso(entry.get("last_seen", ""))
        stale = last_seen is None or last_seen < finding_cutoff
        if stale and not entry.get("promotions"):
            del ledger["findings"][key]


def record_finding(
    ledger: Dict[str, Any],
    finding: Dict[str, Any],
    revision: str,
    now: Optional[_dt.datetime] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Merge one finding from this run into the ledger; return its fingerprint and entry.

    The agent supplies signal, severity, title, location, summary, evidence and
    an occurrence count. Everything else -- identity, first seen, the rolling
    count, the promotion history -- belongs to the ledger and is not the agent's
    to set. That split matters: an agent that could write its own occurrence
    count could talk itself past the gate.
    """
    now = now or utcnow()
    signal = str(finding.get("signal", "other")).strip().lower()
    if signal not in SIGNALS:
        signal = "other"
    severity = str(finding.get("severity", "low")).strip().lower()
    if severity not in SEVERITIES:
        severity = "low"
    title = str(finding.get("title", "")).strip()
    location = str(finding.get("location", "")).strip()
    fp = str(finding.get("fingerprint") or "").strip() or fingerprint(signal, title, location)

    try:
        count = max(1, int(finding.get("occurrences", 1)))
    except (TypeError, ValueError):
        count = 1

    entry = ledger["findings"].get(fp)
    if entry is None:
        entry = {
            "fingerprint": fp,
            "first_seen": to_iso(now),
            "sightings": [],
            "promotions": [],
        }
        ledger["findings"][fp] = entry

    entry["signal"] = signal
    entry["severity"] = severity
    entry["title"] = title
    entry["location"] = location
    entry["summary"] = str(finding.get("summary", "")).strip()
    entry["evidence"] = finding.get("evidence")
    entry["proposed_fix"] = str(finding.get("proposed_fix", "")).strip()
    entry["revision"] = revision
    entry["last_seen"] = to_iso(now)
    entry.setdefault("sightings", []).append({"at": to_iso(now), "count": count})
    entry.setdefault("promotions", [])
    return fp, entry


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def _rule_for(severity: str, rules: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for rule in rules or []:
        if str(rule.get("severity", "")).strip().lower() == severity:
            return rule
    return None


def promotions_today(ledger: Dict[str, Any], now: _dt.datetime) -> int:
    cutoff = now - _dt.timedelta(hours=COUNT_WINDOW_HOURS)
    total = 0
    for entry in ledger["findings"].values():
        for promotion in entry.get("promotions", []) or []:
            at = from_iso(promotion.get("at", "")) if isinstance(promotion, dict) else None
            if at is not None and at >= cutoff:
                total += 1
    return total


def evaluate_gate(
    ledger: Dict[str, Any],
    gate: Dict[str, Any],
    fingerprints: Iterable[str],
    now: Optional[_dt.datetime] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """Decide which of this run's findings become pull requests.

    Returns the promoted fingerprints, worst-severity-first, and a reason per
    fingerprint for everything considered -- including the promoted ones, so a
    run's log says why each decision went the way it did rather than only
    listing the survivors.

    Three conditions, in the order sec. 7.3 states them: the finding matches a
    promotion rule at its own severity with enough occurrences in the window; it
    has not been promoted inside the cooldown; and the day's budget is unspent.
    The budget is counted from the ledger, so promotions this run consumes it
    too -- otherwise a run finding six criticals would open six pull requests
    against a ceiling of two.
    """
    now = now or utcnow()
    rules = gate.get("rules") or []
    try:
        budget = int(gate.get("maxPullRequestsPerDay", 0))
    except (TypeError, ValueError):
        budget = 0
    try:
        cooldown = float(gate.get("cooldownHours", COUNT_WINDOW_HOURS))
    except (TypeError, ValueError):
        cooldown = float(COUNT_WINDOW_HOURS)

    spent = promotions_today(ledger, now)
    remaining = max(0, budget - spent)

    candidates = []
    for fp in fingerprints:
        entry = ledger["findings"].get(fp)
        if entry is None:
            continue
        candidates.append((SEVERITIES.index(entry.get("severity", "low")) if entry.get("severity") in SEVERITIES else len(SEVERITIES), fp, entry))
    candidates.sort(key=lambda item: (item[0], -occurrences_in_window(item[2], now)))

    promoted: List[str] = []
    reasons: Dict[str, str] = {}
    for _, fp, entry in candidates:
        severity = entry.get("severity", "low")
        rule = _rule_for(severity, rules)
        if rule is None:
            reasons[fp] = "held: no promotion rule for severity %s" % severity
            continue
        try:
            threshold = int(rule.get("minOccurrencesPerDay", 1))
        except (TypeError, ValueError):
            threshold = 1
        seen = occurrences_in_window(entry, now)
        if seen < threshold:
            reasons[fp] = "held: %d occurrence(s) in %dh, rule wants %d" % (seen, COUNT_WINDOW_HOURS, threshold)
            continue
        last = None
        for promotion in entry.get("promotions", []) or []:
            at = from_iso(promotion.get("at", "")) if isinstance(promotion, dict) else None
            if at is not None and (last is None or at > last):
                last = at
        if last is not None and (now - last) < _dt.timedelta(hours=cooldown):
            reasons[fp] = "held: promoted %s, inside the %gh cooldown" % (to_iso(last), cooldown)
            continue
        if remaining <= 0:
            reasons[fp] = "held: the day's pull-request budget (%d) is spent" % budget
            continue
        remaining -= 1
        promoted.append(fp)
        reasons[fp] = "promoted: %s at %d occurrence(s) in %dh" % (severity, seen, COUNT_WINDOW_HOURS)
    return promoted, reasons


def record_promotion(
    ledger: Dict[str, Any],
    fp: str,
    url: str,
    revision: str,
    now: Optional[_dt.datetime] = None,
) -> None:
    now = now or utcnow()
    entry = ledger["findings"].get(fp)
    if entry is None:
        return
    entry.setdefault("promotions", []).append(
        {"at": to_iso(now), "url": url, "revision": revision}
    )


def record_run(
    ledger: Dict[str, Any],
    revision: str,
    outcome: str,
    found: int,
    promoted: int,
    note: str = "",
    now: Optional[_dt.datetime] = None,
) -> None:
    now = now or utcnow()
    ledger.setdefault("runs", []).append(
        {
            "at": to_iso(now),
            "revision": revision,
            "outcome": outcome,
            "findings": found,
            "promoted": promoted,
            "note": note,
        }
    )
    ledger["runs"] = ledger["runs"][-RUN_HISTORY:]


def summarise_for_prompt(ledger: Dict[str, Any], now: Optional[_dt.datetime] = None, limit: int = 40) -> str:
    """What the previous runs already know, in the form the agent is handed it.

    This is the loop's memory. Without it every run re-derives the same findings
    from scratch and reports each as new, which is both the largest waste of a
    run and the thing that makes the occurrence count meaningless.
    """
    now = now or utcnow()
    entries = sorted(
        ledger["findings"].values(),
        key=lambda e: (SEVERITIES.index(e["severity"]) if e.get("severity") in SEVERITIES else len(SEVERITIES), e.get("last_seen", "")),
    )
    if not entries:
        return "The ledger is empty: this is the first run, or nothing has been found yet."
    lines = []
    for entry in entries[:limit]:
        promotions = entry.get("promotions") or []
        filed = (" filed=%s" % promotions[-1].get("url", "?")) if promotions else ""
        lines.append(
            "- %s [%s/%s] %s (seen %dx in %dh, last %s, at %s)%s"
            % (
                entry.get("fingerprint", "?"),
                entry.get("severity", "?"),
                entry.get("signal", "?"),
                entry.get("title", "(untitled)"),
                occurrences_in_window(entry, now),
                COUNT_WINDOW_HOURS,
                entry.get("last_seen", "?"),
                entry.get("revision", "?"),
                filed,
            )
        )
    if len(entries) > limit:
        lines.append("- ... and %d more" % (len(entries) - limit))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

LEDGER_KEY = "ledger.json"


def load(namespace: str, name: str) -> Dict[str, Any]:
    """Read the ledger ConfigMap, or start a fresh one if it does not exist.

    A missing ConfigMap is the first run on a chart that renders it, and a
    ConfigMap the Role cannot read is a misconfiguration -- both give an empty
    ledger rather than an exception, because a run that cannot read history is
    still a run that can find things. The difference is visible in the run
    record, which says which happened.
    """
    from kubernetes import client, config as kube_config  # noqa: PLC0415  (cluster-only import)

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover - only reachable outside a pod
        kube_config.load_kube_config()
    api = client.CoreV1Api()
    try:
        cm = api.read_namespaced_config_map(name=name, namespace=namespace)
    except client.exceptions.ApiException as exc:
        if exc.status in (403, 404):
            return empty_ledger()
        raise
    raw = (cm.data or {}).get(LEDGER_KEY)
    if not raw:
        return empty_ledger()
    try:
        return coerce(json.loads(raw))
    except (TypeError, ValueError):
        return empty_ledger()


def save(namespace: str, name: str, ledger: Dict[str, Any]) -> None:
    """Write the ledger back, creating the ConfigMap if the chart did not.

    `patch` rather than `replace`: the chart owns the object's labels and a
    replace would drop them, which takes the ledger out of every
    `-l app.kubernetes.io/part-of=kube-agents` sweep the docs tell an operator
    to run.
    """
    from kubernetes import client, config as kube_config  # noqa: PLC0415

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover
        kube_config.load_kube_config()
    api = client.CoreV1Api()
    body = {"data": {LEDGER_KEY: json.dumps(ledger, indent=1, sort_keys=True)}}
    try:
        api.patch_namespaced_config_map(name=name, namespace=namespace, body=body)
        return
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
    api.create_namespaced_config_map(
        namespace=namespace,
        body=client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={"app.kubernetes.io/part-of": "kube-agents"},
            ),
            data=body["data"],
        ),
    )


def clone(ledger: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(ledger)
