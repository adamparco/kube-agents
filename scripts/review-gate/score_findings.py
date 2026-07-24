#!/usr/bin/env python3
"""Review-gate scorer — the authoritative, hermetic enforcer (Phase 5 / P5-T3, decision R-A/B/C).

06 §7 mandates that CI is authoritative, runs *outside* the agent, and that a scoring step turns the
review skills' JSON findings into the blocking decision. This is that scoring step. It is deliberately
**dependency-free** (stdlib only) so the security decision is reproducible in any environment — the
offline build inner loop, an agent pod, and GitHub Actions alike (matching the Phase-4 test idiom;
PyYAML is not available offline, so the constrained waiver YAML is parsed with a small stdlib parser).

Contract (docs/build/phase-5.md R-B/R-C, docs/build/phase-5/review-gate-waivers.md):
  - Findings carry `severity in {critical,high,medium,low}`; a missing/unknown severity => `high`.
  - `critical`/`high` BLOCK merge unless mitigated by a matching, non-expired waiver;
    `medium`/`low` are advisory and never block.
  - A finding's fingerprint = sha256(agent + "\\n" + file + "\\n" + normalize(message))[:16].
  - A malformed waiver entry is ignored (fails safe toward blocking) and reported.

Usage:
  score_findings.py [--waivers FILE] [--today YYYY-MM-DD] [FINDINGS.json]   # score -> exit 1 if blocked
  score_findings.py --fingerprint [FINDINGS.json]                           # print each finding's id
FINDINGS defaults to stdin. Exit code is the merge decision: non-zero => blocked.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys

BLOCKING_SEVERITIES = ("critical", "high")
VALID_SEVERITIES = ("critical", "high", "medium", "low")

# Line/line-number tokens stripped from a message before fingerprinting, so a finding that shifts by a
# few lines (or whose wording tweaks a number) keeps a stable id. Order matters: phrase forms first.
_LINE_TOKEN_RE = re.compile(r"\b(?:lines?|l)\s*[:#]?\s*\d+(?:\s*[-–]\s*\d+)?\b", re.IGNORECASE)
_COLON_NUM_RE = re.compile(r":\d+\b")
_BARE_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


def normalize_message(message):
    """Lowercase, strip line-number tokens, collapse whitespace, trim (docs/.../review-gate-waivers.md)."""
    text = (message or "").lower()
    text = _LINE_TOKEN_RE.sub(" ", text)
    text = _COLON_NUM_RE.sub(" ", text)
    text = _BARE_NUM_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def fingerprint(agent, file, message):
    """Stable 16-hex id: sha256(agent \\n file \\n normalize(message))[:16] (decision R-C)."""
    basis = "{}\n{}\n{}".format(agent or "", file or "", normalize_message(message))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def severity_of(finding):
    """The finding's severity, fail-safe: missing/unknown => 'high' (decision R-B)."""
    sev = str(finding.get("severity", "") or "").strip().lower()
    return sev if sev in VALID_SEVERITIES else "high"


def flatten_findings(raw):
    """Normalize the skills' output into a flat list of finding dicts, each carrying its `agent`.

    Accepts the aggregated shape `[{"agent": ..., "findings": [{...}]}]`, a bare flat list of finding
    dicts, or a single object of either shape. Each returned dict has: agent, file, line, message,
    severity (severity untouched here; severity_of() applies the fail-safe).
    """
    out = []
    if raw is None:
        return out
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if not isinstance(item, dict):
            continue
        if "findings" in item and isinstance(item["findings"], list):
            agent = item.get("agent", "")
            for f in item["findings"]:
                if isinstance(f, dict):
                    out.append(
                        {
                            "agent": f.get("agent", agent),
                            "file": f.get("file", ""),
                            "line": f.get("line", ""),
                            "message": f.get("message", ""),
                            "severity": f.get("severity", ""),
                        }
                    )
        elif "message" in item:
            out.append(
                {
                    "agent": item.get("agent", ""),
                    "file": item.get("file", ""),
                    "line": item.get("line", ""),
                    "message": item.get("message", ""),
                    "severity": item.get("severity", ""),
                }
            )
    return out


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_waivers(text):
    """Parse the constrained waiver-YAML subset with stdlib only (no PyYAML — offline/in-pod safe).

    Recognizes `waivers: []` (empty) and a `waivers:` list of `- key: value` blocks with flat scalar
    values; full-line `#` comments and blank lines are ignored. Returns (waivers, warnings) where each
    waiver is {fingerprint, justification, approved_by, expires(date)}. A malformed entry (missing
    field or unparseable date) is dropped with a warning — fail-safe toward blocking (R-C).
    """
    entries = []
    warnings = []
    current = None
    in_waivers = False

    def _finish(entry):
        if entry is not None:
            entries.append(entry)

    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_waivers:
            if re.match(r"^waivers\s*:", stripped):
                in_waivers = True
                rest = stripped.split(":", 1)[1].strip()
                if rest in ("[]", "[ ]"):
                    return [], warnings  # explicitly empty
            continue
        # Inside the waivers list.
        if stripped.startswith("- "):
            _finish(current)
            current = {}
            stripped = stripped[2:].strip()  # first key on the dash line
        if current is None:
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = _strip_quotes(value)
    _finish(current)

    parsed = []
    required = ("fingerprint", "justification", "approved_by", "expires")
    for idx, entry in enumerate(entries, start=1):
        missing = [k for k in required if not str(entry.get(k, "")).strip()]
        if missing:
            warnings.append("waiver #{} ignored: missing field(s) {}".format(idx, ", ".join(missing)))
            continue
        try:
            expires = datetime.date.fromisoformat(str(entry["expires"]).strip())
        except ValueError:
            warnings.append(
                "waiver #{} ({}) ignored: bad expires date {!r}".format(
                    idx, entry.get("fingerprint"), entry.get("expires")
                )
            )
            continue
        parsed.append(
            {
                "fingerprint": str(entry["fingerprint"]).strip().lower(),
                "justification": entry["justification"],
                "approved_by": entry["approved_by"],
                "expires": expires,
            }
        )
    return parsed, warnings


def active_waiver_fingerprints(waivers, today):
    """Fingerprints of waivers not yet expired (expires >= today)."""
    return {w["fingerprint"] for w in waivers if w["expires"] >= today}


def score(findings, waivers, today):
    """Apply the block rule. Returns a report dict; report['blocked'] drives the exit code.

    findings: flat list from flatten_findings(); waivers: parsed list from parse_waivers();
    today: datetime.date used for expiry (injectable for deterministic tests).
    """
    active = active_waiver_fingerprints(waivers, today)
    blockers, waived, advisory = [], [], []
    for f in findings:
        sev = severity_of(f)
        fp = fingerprint(f.get("agent", ""), f.get("file", ""), f.get("message", ""))
        record = {
            "fingerprint": fp,
            "severity": sev,
            "agent": f.get("agent", ""),
            "file": f.get("file", ""),
            "line": f.get("line", ""),
            "message": f.get("message", ""),
        }
        if sev in BLOCKING_SEVERITIES:
            if fp in active:
                waived.append(record)
            else:
                blockers.append(record)
        else:
            advisory.append(record)
    return {
        "blocked": len(blockers) > 0,
        "blockers": blockers,
        "waived": waived,
        "advisory": advisory,
    }


def _render(report, warnings):
    lines = []
    for w in warnings:
        lines.append("WARN  {}".format(w))
    for b in report["blockers"]:
        lines.append(
            "BLOCK [{}] {} {}: {}  (fingerprint {})".format(
                b["severity"], b["agent"], b["file"], b["message"], b["fingerprint"]
            )
        )
    for w in report["waived"]:
        lines.append(
            "WAIVED [{}] {} {}: {}  (fingerprint {})".format(
                w["severity"], w["agent"], w["file"], w["message"], w["fingerprint"]
            )
        )
    for a in report["advisory"]:
        lines.append("advisory [{}] {} {}: {}".format(a["severity"], a["agent"], a["file"], a["message"]))
    n_block, n_waived, n_adv = len(report["blockers"]), len(report["waived"]), len(report["advisory"])
    if report["blocked"]:
        lines.append(
            "\nREVIEW-GATE: BLOCKED — {} unmitigated high/critical finding(s) "
            "({} waived, {} advisory).".format(n_block, n_waived, n_adv)
        )
    else:
        lines.append(
            "\nREVIEW-GATE: PASS — no unmitigated high/critical findings "
            "({} waived, {} advisory).".format(n_waived, n_adv)
        )
    return "\n".join(lines)


def _read_findings(path):
    data = sys.stdin.read() if path in (None, "-") else open(path, "r", encoding="utf-8").read()
    return flatten_findings(json.loads(data))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score review-gate findings into a merge decision.")
    parser.add_argument("findings", nargs="?", default="-", help="findings JSON file (default: stdin)")
    parser.add_argument("--waivers", default="security-review-waivers.yaml", help="waiver file")
    parser.add_argument("--today", default=None, help="override today (YYYY-MM-DD) for testing")
    parser.add_argument(
        "--fingerprint",
        action="store_true",
        help="print each finding's fingerprint instead of scoring",
    )
    args = parser.parse_args(argv)

    findings = _read_findings(args.findings)

    if args.fingerprint:
        for f in findings:
            fp = fingerprint(f.get("agent", ""), f.get("file", ""), f.get("message", ""))
            print("{}  {}  {}: {}".format(fp, severity_of(f), f.get("file", ""), f.get("message", "")))
        return 0

    try:
        waiver_text = open(args.waivers, "r", encoding="utf-8").read()
    except FileNotFoundError:
        waiver_text = ""
    waivers, warnings = parse_waivers(waiver_text)

    today = (
        datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    )
    report = score(findings, waivers, today)
    print(_render(report, warnings))
    return 1 if report["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
