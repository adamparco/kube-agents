#!/opt/hermes/.venv/bin/python3
"""
audit_pr.py — Deterministic PR harness for the fleet-audit skill.

Every autonomous audit watchdog (compliance, security patch, obtainability,
cost, consistency drift) funnels its findings through this script so that each
audit stream owns exactly ONE continuously-updated Pull Request. The LLM's role
is strictly constrained to **inspecting the fleet read-only and emitting a
findings.json**; every git/gh operation, the PR body, the commit subject, the
timestamps, and the run-over-run delta are produced here, deterministically.

Two-command lifecycle:

    audit_pr.py start  --audit <audit-id>
    audit_pr.py finish --audit <audit-id> --findings-file <path> [--dry-run]

The pure functions (validate/render/delta) carry no I/O and are unit tested in
test_audit_pr.py; the thin shell below them owns all subprocess execution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# The shared scripts dir holds github_token_refresh (see docker-entrypoint.sh:
# executable scripts are shared across profiles, not copied per-profile). The
# import itself is lazy so `--dry-run` works on a dev machine with no sandbox.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# The audit streams allowed to own a PR. An id not listed here is rejected
# before any git/gh call: a typo must not silently open a sixth PR stream.
# The human names mirror the `name` of the matching watchdog in
# agents/platform/cron/jobs.json — keep the two in step so the PR title and the
# cron catalogue name the same thing.
AUDITS: dict[str, str] = {
    "compliance-audit": "Security & RBAC Posture Audit",
    "security-patch-orchestrator": "Upgrade & Patch Readiness Audit",
    "obtainability-audit": "Workload Reliability Audit",
    "fleet-wide-cost-analysis": "Fleet Waste Audit",
    "fleet-consistency-drift": "Fleet Consistency Drift Audit",
}

SEVERITIES = ("critical", "major", "minor")
REMEDIATION_KINDS = ("manifest", "gcloud", "manual")
PROTECTED_BRANCHES = {"main", "master", "production"}
BASE_BRANCH = "main"
SCRATCH_DIR = "/opt/data/scratch"

# Wildcard stagers that must never reach `git add` — an audit stages named
# remediation files only, never the whole working tree.
FORBIDDEN_ADD_PATHSPECS = {".", "-A", "--all", "-a", "*", ":/", "./", ":"}

# The hidden block that makes the run-over-run delta computable without keeping
# any state outside the PR itself.
DELTA_RE = re.compile(r"<!--\s*audit-findings:\s*(\[.*?\])\s*-->", re.S)
# Per-finding marker on each heading, so a *resolved* finding can still be named
# by title when it no longer exists in the current findings.json.
FINDING_MARKER_RE = re.compile(
    r"^####\s+(.*?)\s*<!--\s*finding:\s*(\S+?)\s*-->\s*$", re.M
)

MAX_EXCERPT_LINES = 40
MAX_EXCERPT_CHARS = 2000


class ValidationError(ValueError):
    """A findings.json (or audit id) that the harness refuses to publish."""


def log(msg: str) -> None:
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [FLEET-AUDIT] {msg}",
        file=sys.stderr,
        flush=True,
    )


# --------------------------------------------------------------------------- #
# Pure helpers — identity
# --------------------------------------------------------------------------- #


def validate_audit_id(audit_id: str) -> str:
    if audit_id not in AUDITS:
        raise ValidationError(
            f"--audit: unknown audit id {audit_id!r}; must be one of "
            + ", ".join(sorted(AUDITS))
        )
    return audit_id


def audit_name(audit_id: str) -> str:
    return AUDITS[audit_id]


def branch_for(audit_id: str) -> str:
    return f"platform-agent/audit-{audit_id}"


def findings_path_for(audit_id: str) -> str:
    return f"{SCRATCH_DIR}/findings_{audit_id}.json"


def assert_pushable(branch: str) -> str:
    """Refuse to force-push a protected branch (same guardrail as submit_suggestion.py)."""
    if branch.strip().lower() in PROTECTED_BRANCHES:
        raise ValueError(
            f"CRITICAL SECURITY REFUSAL: Force-pushing to protected branch "
            f"'{branch}' is strictly blocked by GKE SRE guardrails!"
        )
    return branch


# --------------------------------------------------------------------------- #
# Pure helpers — validation
# --------------------------------------------------------------------------- #


def _require_str(value: object, where: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(
            f"{where}: expected a string, got {type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise ValidationError(f"{where}: required, must be a non-empty string")
    return value


def _require_repo_relative(path: str, where: str) -> str:
    """A remediation path is staged with `git add` — keep it inside the repo."""
    if "\\" in path or path.startswith("/") or PurePosixPath(path).is_absolute():
        raise ValidationError(
            f"{where}: must be a POSIX path relative to the repository root, got {path!r}"
        )
    if ".." in PurePosixPath(path).parts:
        raise ValidationError(
            f"{where}: must not escape the repository root ('..' segment), got {path!r}"
        )
    return path


def validate_findings(data: object, audit_id: str) -> dict:
    """Validate a findings document. Raises ValidationError naming index + field."""
    validate_audit_id(audit_id)

    if not isinstance(data, dict):
        raise ValidationError(
            f"findings file: top level must be a JSON object, got {type(data).__name__}"
        )

    declared = data.get("audit")
    if declared != audit_id:
        raise ValidationError(
            f"audit: findings file declares {declared!r} but --audit is {audit_id!r}; "
            "an audit may only publish to its own PR stream"
        )

    scope = data.get("scope")
    if not isinstance(scope, dict):
        raise ValidationError(
            "scope: required object with 'clusters' and 'skipped'"
        )

    clusters = scope.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError(
            "scope.clusters: must be a non-empty list — an audit that enumerated "
            "no clusters is a failure, not a clean run"
        )
    for i, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"scope.clusters[{i}]: expected an object")
        for field in ("name", "location", "project"):
            _require_str(
                cluster.get(field), f"scope.clusters[{i}].{field}", allow_empty=False
            )

    skipped = scope.get("skipped", [])
    if not isinstance(skipped, list):
        raise ValidationError(
            "scope.skipped: must be a list (use [] when nothing was skipped)"
        )
    for i, entry in enumerate(skipped):
        if not isinstance(entry, dict):
            raise ValidationError(f"scope.skipped[{i}]: expected an object")
        _require_str(
            entry.get("cluster"), f"scope.skipped[{i}].cluster", allow_empty=False
        )
        _require_str(
            entry.get("reason"), f"scope.skipped[{i}].reason", allow_empty=False
        )

    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ValidationError(
            "findings: must be a list (use [] for a clean audit)"
        )

    seen_ids: dict[str, int] = {}
    for i, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValidationError(f"findings[{i}]: expected an object")

        fid = finding.get("id")
        _require_str(fid, f"findings[{i}].id", allow_empty=False)
        assert isinstance(fid, str)
        if fid in seen_ids:
            raise ValidationError(
                f"findings[{i}].id: duplicate id {fid!r} (first seen at "
                f"findings[{seen_ids[fid]}]); ids must be unique within the file"
            )
        seen_ids[fid] = i

        severity = finding.get("severity")
        if severity not in SEVERITIES:
            raise ValidationError(
                f"findings[{i}].severity: must be one of "
                f"{', '.join(SEVERITIES)}, got {severity!r}"
            )

        _require_str(finding.get("title"), f"findings[{i}].title", allow_empty=False)
        _require_str(
            finding.get("cluster"), f"findings[{i}].cluster", allow_empty=False
        )
        # namespace may legitimately be empty for cluster-scoped objects.
        _require_str(finding.get("namespace", ""), f"findings[{i}].namespace")
        _require_str(finding.get("object"), f"findings[{i}].object", allow_empty=False)
        _require_str(finding.get("impact"), f"findings[{i}].impact", allow_empty=False)

        evidence = finding.get("evidence")
        if not isinstance(evidence, dict):
            raise ValidationError(
                f"findings[{i}].evidence: required object with 'command' and 'excerpt'"
            )
        if not isinstance(evidence.get("command"), str) or not evidence[
            "command"
        ].strip():
            raise ValidationError(
                f"findings[{i}].evidence.command: required, must be a non-empty "
                "string — a finding with no reproducible command is dropped, not softened"
            )
        _require_str(
            evidence.get("excerpt", ""), f"findings[{i}].evidence.excerpt"
        )

        remediation = finding.get("remediation")
        if not isinstance(remediation, dict):
            raise ValidationError(
                f"findings[{i}].remediation: required object with 'kind' and 'note'"
            )
        kind = remediation.get("kind")
        if kind not in REMEDIATION_KINDS:
            raise ValidationError(
                f"findings[{i}].remediation.kind: must be one of "
                f"{', '.join(REMEDIATION_KINDS)}, got {kind!r}"
            )
        path = remediation.get("path", "")
        if kind == "manifest":
            _require_str(
                path, f"findings[{i}].remediation.path", allow_empty=False
            )
            assert isinstance(path, str)
            _require_repo_relative(path, f"findings[{i}].remediation.path")
        elif path:
            raise ValidationError(
                f"findings[{i}].remediation.path: only permitted when kind == "
                f"'manifest' (kind is {kind!r}); {kind!r} remediations stage no files"
            )
        _require_str(
            remediation.get("note", ""), f"findings[{i}].remediation.note"
        )

    return data


# --------------------------------------------------------------------------- #
# Pure helpers — derivation
# --------------------------------------------------------------------------- #


def severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def finding_ids(findings: list[dict]) -> list[str]:
    return [str(f.get("id", "")) for f in findings]


def manifest_paths(findings: list[dict]) -> list[str]:
    """The distinct remediation manifest paths — the ENTIRE staging set."""
    paths: set[str] = set()
    for finding in findings:
        remediation = finding.get("remediation") or {}
        if remediation.get("kind") == "manifest":
            path = remediation.get("path")
            if path:
                paths.add(str(path))
    return sorted(paths)


def build_git_add_command(paths: list[str]) -> list[str]:
    """Build the ONLY `git add` this harness ever runs: explicit paths, never a wildcard."""
    if not paths:
        raise ValueError(
            "refusing to build a `git add` with no explicit paths; "
            "an empty staging set must produce an --allow-empty commit instead"
        )
    for path in paths:
        if path.strip() in FORBIDDEN_ADD_PATHSPECS:
            raise ValueError(
                f"refusing to stage wildcard pathspec {path!r}: audits stage only "
                "the named remediation files (never `git add .` / `git add -A`)"
            )
        _require_repo_relative(path, "git add pathspec")
    return ["git", "add", "--", *paths]


def findings_phrase(count: int) -> str:
    """`1 finding` / `2 findings` — the title is the daily-visible artifact."""
    return f"{count} finding" if count == 1 else f"{count} findings"


def commit_subject(audit_id: str, findings: list[dict]) -> str:
    counts = severity_counts(findings)
    return (
        f"chore(audit): {audit_id} — {findings_phrase(len(findings))} "
        f"({counts['critical']} critical, {counts['major']} major, "
        f"{counts['minor']} minor)"
    )


def pr_title(audit_id: str, findings: list[dict]) -> str:
    counts = severity_counts(findings)
    return (
        f"[audit] {audit_name(audit_id)} — {findings_phrase(len(findings))} "
        f"({counts['critical']} critical)"
    )


def delta_block(ids: list[str]) -> str:
    """The hidden, machine-read block that carries this run's finding ids."""
    payload = json.dumps(sorted(set(ids)), separators=(",", ":"))
    return f"<!-- audit-findings: {payload} -->"


def parse_delta_block(body: str | None) -> list[str]:
    """Read the finding ids out of a previous PR body ([] when absent/unparseable)."""
    if not body:
        return []
    matches = DELTA_RE.findall(body)
    if not matches:
        return []
    try:
        ids = json.loads(matches[-1])
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)]


def parse_finding_titles(body: str | None) -> dict[str, str]:
    """Recover {finding id: title} from a previous PR body, to name resolved findings."""
    if not body:
        return {}
    return {fid: title.strip() for title, fid in FINDING_MARKER_RE.findall(body)}


def compute_delta(
    previous_ids: list[str], current_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Return (newly appeared ids, newly resolved ids), both sorted."""
    previous, current = set(previous_ids), set(current_ids)
    return sorted(current - previous), sorted(previous - current)


# --------------------------------------------------------------------------- #
# Pure helpers — rendering
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    """Make a value safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _fence(text: str) -> str:
    """A backtick fence long enough to wrap text that itself contains fences."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def trim_excerpt(excerpt: str) -> str:
    """Clip evidence output so one noisy finding cannot blow the PR body limit."""
    text = (excerpt or "").strip("\n").rstrip()
    if not text:
        return ""
    lines = text.splitlines()
    clipped = False
    if len(lines) > MAX_EXCERPT_LINES:
        lines = lines[:MAX_EXCERPT_LINES]
        clipped = True
    text = "\n".join(lines)
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[:MAX_EXCERPT_CHARS].rstrip()
        clipped = True
    if clipped:
        text += "\n… (excerpt truncated by audit_pr.py — re-run the command above for the full output)"
    return text


def _finding_sort_key(finding: dict) -> tuple:
    """Stable ordering, so an unchanged fleet renders a byte-identical body."""
    return (
        str(finding.get("cluster", "")),
        str(finding.get("namespace", "")),
        str(finding.get("object", "")),
        str(finding.get("title", "")),
        str(finding.get("id", "")),
    )


def render_finding(finding: dict) -> list[str]:
    fid = str(finding.get("id", ""))
    title = str(finding.get("title", "")).strip()
    namespace = str(finding.get("namespace", "")).strip()
    where = f"`{finding.get('cluster', '')}`"
    where += f" / `{namespace}`" if namespace else " / _cluster-scoped_"

    lines = [f"#### {title} <!-- finding:{fid} -->", ""]
    lines.append(f"- **Where:** {where} — `{finding.get('object', '')}`")
    lines.append(f"- **Impact:** {finding.get('impact', '')}")
    lines.append("")

    evidence = finding.get("evidence") or {}
    command = str(evidence.get("command", "")).strip()
    lines.append("Evidence — reproduce with:")
    lines.append("")
    fence = _fence(command)
    lines += [f"{fence}bash", command, fence]

    excerpt = trim_excerpt(str(evidence.get("excerpt", "")))
    if excerpt:
        lines.append("")
        fence = _fence(excerpt)
        lines += [f"{fence}text", excerpt, fence]

    remediation = finding.get("remediation") or {}
    kind = remediation.get("kind")
    note = str(remediation.get("note", "")).strip()
    lines.append("")
    if kind == "manifest":
        path = str(remediation.get("path", ""))
        suffix = f" — {note}" if note else ""
        lines.append(f"- **Remediation (manifest):** [`{path}`]({path}){suffix}")
    elif kind == "gcloud":
        lines.append("- **Remediation (gcloud):**")
        lines.append("")
        fence = _fence(note)
        lines += [f"{fence}bash", note or "# (no command supplied)", fence]
    else:
        lines.append(f"- **Remediation (manual):** {note or '_none supplied_'}")
    return lines


def render_body(
    data: dict,
    *,
    staged_paths: list[str],
    generated_at: datetime,
    audit_id: str | None = None,
) -> str:
    """Render the complete PR body. The model never hand-writes this."""
    audit_id = audit_id or str(data.get("audit", ""))
    findings = list(data.get("findings") or [])
    scope = data.get("scope") or {}
    clusters = list(scope.get("clusters") or [])
    skipped = list(scope.get("skipped") or [])
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = []
    out.append(
        f"This pull request is maintained in place by the `{audit_id}` watchdog and is "
        "rewritten in full on every run — hand edits to this description will be lost, "
        "and the audit will never open a second PR for this stream."
    )

    # --- Scope ---
    out += ["", "## Scope", ""]
    out.append(f"Audited {len(clusters)} cluster(s) on {stamp}.")
    out += ["", "| Cluster | Location | Project |", "| ------- | -------- | ------- |"]
    for cluster in clusters:
        out.append(
            f"| `{_cell(cluster.get('name', ''))}` "
            f"| {_cell(cluster.get('location', ''))} "
            f"| `{_cell(cluster.get('project', ''))}` |"
        )
    if skipped:
        out += [
            "",
            "### Skipped",
            "",
            f"**Coverage is partial.** {len(skipped)} cluster(s) could not be audited, "
            "so this report says nothing about them — treat them as unknown, not clean.",
            "",
            "| Cluster | Reason |",
            "| ------- | ------ |",
        ]
        for entry in skipped:
            out.append(
                f"| `{_cell(entry.get('cluster', ''))}` | {_cell(entry.get('reason', ''))} |"
            )

    # --- Findings ---
    out += ["", "## Findings", ""]
    if not findings:
        out.append("No findings. Every audited cluster is compliant with this audit.")
    else:
        counts = severity_counts(findings)
        out.append(
            f"{findings_phrase(len(findings))}: {counts['critical']} critical, "
            f"{counts['major']} major, {counts['minor']} minor."
        )
        for severity in SEVERITIES:
            group = sorted(
                (f for f in findings if f.get("severity") == severity),
                key=_finding_sort_key,
            )
            if not group:
                continue
            out += ["", f"### {severity.capitalize()} ({len(group)})"]
            for finding in group:
                out.append("")
                out += render_finding(finding)

    # --- Remediation files ---
    out += ["", "## Remediation files in this PR", ""]
    if staged_paths:
        out += [f"- `{path}`" for path in staged_paths]
    else:
        out.append(
            "No files changed — every remediation in this audit is a `gcloud` or "
            "`manual` action, so this pull request carries the report only."
        )

    # --- Footer + hidden delta block ---
    out += [
        "",
        "---",
        "",
        f"Generated by the Platform Agent `{audit_id}` watchdog at "
        f"{generated_at.isoformat()}. Findings come from read-only inspection of the "
        "live fleet; every one carries the exact command it was derived from.",
        "",
        delta_block(finding_ids(findings)),
        "",
    ]
    return "\n".join(out)


def render_delta_comment(
    audit_id: str,
    new_ids: list[str],
    resolved_ids: list[str],
    findings: list[dict],
    previous_titles: dict[str, str],
    generated_at: datetime,
) -> str | None:
    """The delta comment, or None when nothing changed (silence beats noise)."""
    if not new_ids and not resolved_ids:
        return None

    by_id = {str(f.get("id", "")): f for f in findings}
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [f"### `{audit_id}` audit delta — {stamp}", ""]

    if new_ids:
        out.append(f"**{len(new_ids)} new**")
        out.append("")
        for fid in new_ids:
            finding = by_id.get(fid, {})
            severity = str(finding.get("severity", "unknown"))
            title = str(finding.get("title", fid))
            out.append(f"- **{severity}** — {title} (`{fid}`)")
        out.append("")

    if resolved_ids:
        out.append(f"**{len(resolved_ids)} resolved**")
        out.append("")
        for fid in resolved_ids:
            title = previous_titles.get(fid) or fid
            out.append(f"- {title} (`{fid}`)")
        out.append("")

    out.append(
        "The pull request description has been rewritten to the current state of the fleet."
    )
    return "\n".join(out)


def render_clean_comment(
    audit_id: str, data: dict, generated_at: datetime
) -> str:
    """Comment posted when an audit that previously had findings comes back clean."""
    scope = data.get("scope") or {}
    clusters = list(scope.get("clusters") or [])
    skipped = list(scope.get("skipped") or [])
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    names = ", ".join(f"`{c.get('name', '')}`" for c in clusters)

    out = [
        f"### `{audit_id}` is now clean — closing",
        "",
        f"The {audit_name(audit_id)} run on {stamp} found **0 findings** across "
        f"{len(clusters)} audited cluster(s): {names}.",
        "",
        "Every finding previously reported here is gone, so this pull request is "
        "being closed. The next run that finds anything will open a fresh one.",
    ]
    if skipped:
        out += [
            "",
            f"**Caveat:** {len(skipped)} cluster(s) were skipped this run and are not "
            "covered by this all-clear:",
            "",
        ]
        out += [
            f"- `{_cell(entry.get('cluster', ''))}` — {_cell(entry.get('reason', ''))}"
            for entry in skipped
        ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# I/O shell — every subprocess call funnels through run_cmd
# --------------------------------------------------------------------------- #


def run_cmd(
    cmd: list[str], *, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, check=check, text=True, capture_output=capture)
    except subprocess.CalledProcessError as exc:
        if check:
            log(f"FAILED ({exc.returncode}): {' '.join(cmd)}")
            if exc.stderr:
                log(exc.stderr.strip())
            raise
        return exc


def git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["git"] + args, check=check)


def gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["gh"] + args, check=check)


def refresh_credentials() -> None:
    """Mint the short-lived repo-scoped GitHub App token into gh + the git credential store."""
    from github_token_refresh import refresh_git_credentials

    refresh_git_credentials()


def resolve_repo() -> str:
    from github_token_refresh import get_current_git_repo

    repo = get_current_git_repo()
    if not repo or "/" not in repo:
        raise RuntimeError(
            "Could not resolve the target repository as owner/name from the git remote"
        )
    return repo


def repo_root() -> Path:
    res = run_cmd(["git", "rev-parse", "--show-toplevel"], check=False)
    root = (res.stdout or "").strip()
    if res.returncode != 0 or not root:
        raise RuntimeError(
            "Not inside a git working tree; run `audit_pr.py start` first"
        )
    return Path(root)


def repo_root_best_effort() -> Path:
    try:
        return repo_root()
    except Exception:
        return Path.cwd()


def current_branch() -> str:
    res = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return (res.stdout or "").strip()


def ensure_labels(repo: str, audit_id: str) -> None:
    labels = [
        (
            "agent:audit",
            "5319E7",
            "Continuously-updated audit report owned by a Platform Agent watchdog",
        ),
        (
            f"audit:{audit_id}",
            "1D76DB",
            f"Findings stream for the {audit_name(audit_id)} audit",
        ),
        ("severity:critical", "B60205", "Highest audit finding severity: critical"),
        ("severity:major", "D93F0B", "Highest audit finding severity: major"),
        ("severity:minor", "FBCA04", "Highest audit finding severity: minor"),
    ]
    for name, color, description in labels:
        gh(
            [
                "label",
                "create",
                name,
                "-R",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            check=False,
        )


def find_existing_pr(repo: str, audit_id: str) -> tuple[int | None, str | None]:
    """The audit's single open PR, if any. Lowest number wins (deterministic)."""
    res = gh(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--label",
            f"audit:{audit_id}",
            "--state",
            "open",
            "--json",
            "number,url",
            "--limit",
            "20",
        ],
        check=False,
    )
    if res.returncode != 0:
        return None, None
    try:
        prs = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(prs, list) or not prs:
        return None, None
    prs.sort(key=lambda p: int(p.get("number", 0)))
    if len(prs) > 1:
        log(
            f"WARNING: {len(prs)} open PRs carry label audit:{audit_id}; updating "
            f"#{prs[0].get('number')} and leaving the rest alone. Close the duplicates."
        )
    return int(prs[0]["number"]), prs[0].get("url")


def fetch_pr_body(repo: str, number: int) -> str:
    res = gh(["pr", "view", str(number), "-R", repo, "--json", "body"], check=False)
    if res.returncode != 0:
        return ""
    try:
        return str(json.loads(res.stdout or "{}").get("body") or "")
    except json.JSONDecodeError:
        return ""


def fetch_pr_url(repo: str, number: int) -> str | None:
    res = gh(["pr", "view", str(number), "-R", repo, "--json", "url"], check=False)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "{}").get("url")
    except json.JSONDecodeError:
        return None


def _write_temp(text: str, suffix: str = ".md") -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(text)
    return handle.name


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def apply_severity_label(repo: str, number: int, findings: list[dict]) -> None:
    """Tag the PR with its highest live severity so triage can sort by it."""
    counts = severity_counts(findings)
    highest = next((s for s in SEVERITIES if counts[s]), None)
    if highest is None:
        return
    args = ["pr", "edit", str(number), "-R", repo, "--add-label", f"severity:{highest}"]
    for severity in SEVERITIES:
        if severity != highest:
            args += ["--remove-label", f"severity:{severity}"]
    gh(args, check=False)


def load_findings(path: str, audit_id: str) -> dict:
    findings_file = Path(path)
    if not findings_file.is_file():
        raise ValidationError(f"--findings-file: {path} does not exist")
    try:
        data = json.loads(findings_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"--findings-file: {path} is not valid JSON: {exc}") from exc
    return validate_findings(data, audit_id)


def assert_remediation_files_exist(findings: list[dict], root: Path) -> None:
    """A manifest remediation must already be written to disk before finish."""
    for i, finding in enumerate(findings):
        remediation = finding.get("remediation") or {}
        if remediation.get("kind") != "manifest":
            continue
        path = str(remediation.get("path", ""))
        if not (root / path).is_file():
            raise ValidationError(
                f"findings[{i}].remediation.path: {path!r} does not exist under the "
                f"repository root ({root}); write the remediation file before calling "
                "finish, or use remediation kind 'gcloud'/'manual'"
            )


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def handle_start(args: argparse.Namespace) -> None:
    audit_id = validate_audit_id(args.audit)
    branch = branch_for(audit_id)

    refresh_credentials()
    repo = resolve_repo()
    ensure_labels(repo, audit_id)

    git(["checkout", BASE_BRANCH])
    git(["pull", "--ff-only", "origin", BASE_BRANCH])
    # -B: create the branch, or reset an existing one back onto main. The audit
    # rewrites its report from scratch on every run, so history is disposable.
    git(["checkout", "-B", branch])

    existing_pr, _ = find_existing_pr(repo, audit_id)

    try:
        os.makedirs(SCRATCH_DIR, exist_ok=True)
    except OSError:
        pass

    print(
        json.dumps(
            {
                "branch": branch,
                "existing_pr": existing_pr,
                "repo": repo,
                "findings_path": findings_path_for(audit_id),
            }
        )
    )


def _handle_finish_dry_run(
    audit_id: str, data: dict, now: datetime
) -> None:
    findings = list(data["findings"])
    paths = manifest_paths(findings)

    log("DRY RUN: validated findings; nothing will be committed, pushed, or published.")
    root = repo_root_best_effort()
    for path in paths:
        if not (root / path).is_file():
            log(
                f"WARNING: remediation path {path!r} not found under {root} "
                "(this is a hard error outside --dry-run)"
            )

    if not findings:
        log("STATUS: CLEAN — 0 findings; the open PR (if any) would be closed.")
        print(render_clean_comment(audit_id, data, now))
        return

    log(f"TITLE: {pr_title(audit_id, findings)}")
    log(f"COMMIT: {commit_subject(audit_id, findings)}")
    log(f"STAGES: {', '.join(paths) if paths else '(nothing — empty commit)'}")
    print(
        render_body(
            data, staged_paths=paths, generated_at=now, audit_id=audit_id
        )
    )


def handle_finish(args: argparse.Namespace) -> None:
    audit_id = validate_audit_id(args.audit)
    data = load_findings(args.findings_file, audit_id)
    findings = list(data["findings"])
    now = datetime.now(timezone.utc)

    if args.dry_run:
        _handle_finish_dry_run(audit_id, data, now)
        return

    refresh_credentials()
    repo = resolve_repo()
    ensure_labels(repo, audit_id)

    existing_pr, existing_url = find_existing_pr(repo, audit_id)
    previous_body = fetch_pr_body(repo, existing_pr) if existing_pr else ""
    previous_ids = parse_delta_block(previous_body)
    previous_titles = parse_finding_titles(previous_body)
    current_ids = finding_ids(findings)
    new_ids, resolved_ids = compute_delta(previous_ids, current_ids)

    # --- Clean run: retire the stream's PR, touch nothing else. ---
    if not findings:
        if existing_pr:
            comment_file = _write_temp(render_clean_comment(audit_id, data, now))
            try:
                gh(["pr", "comment", str(existing_pr), "-R", repo, "-F", comment_file])
            finally:
                _unlink(comment_file)
            gh(["pr", "close", str(existing_pr), "-R", repo])
            log(f"Audit {audit_id} is clean; closed PR #{existing_pr}.")
        else:
            log(f"Audit {audit_id} is clean and has no open PR; nothing to do.")
        print(
            json.dumps(
                {
                    "status": "CLEAN",
                    "pr_url": existing_url,
                    "new": 0,
                    "resolved": len(previous_ids),
                }
            )
        )
        return

    # --- Findings: stage, commit, force-push, publish. ---
    root = repo_root()
    assert_remediation_files_exist(findings, root)
    paths = manifest_paths(findings)

    branch = branch_for(audit_id)
    assert_pushable(branch)
    if current_branch() != branch:
        log(f"Not on {branch}; resetting the audit branch onto HEAD.")
        git(["checkout", "-B", branch])

    if paths:
        run_cmd(build_git_add_command(paths))
    else:
        log("No manifest remediations; committing an empty report commit.")

    subject = commit_subject(audit_id, findings)
    # --allow-empty unconditionally: an audit whose remediation files are all
    # byte-identical to main still needs a commit for the branch/PR to exist.
    git(
        [
            "commit",
            "--allow-empty",
            "-m",
            subject,
            "-m",
            f"Generated by the Platform Agent {audit_id} watchdog at {now.isoformat()}.",
        ]
    )
    git(["push", "-f", "origin", branch])

    title = pr_title(audit_id, findings)
    body_file = _write_temp(
        render_body(data, staged_paths=paths, generated_at=now, audit_id=audit_id)
    )
    try:
        if existing_pr is None:
            res = gh(
                [
                    "pr",
                    "create",
                    "-R",
                    repo,
                    "--base",
                    BASE_BRANCH,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                    "--label",
                    "agent:audit",
                    "--label",
                    f"audit:{audit_id}",
                ]
            )
            status = "OPENED"
            lines = [ln for ln in (res.stdout or "").strip().splitlines() if ln.strip()]
            pr_url = lines[-1] if lines else None
            number = existing_pr
            if pr_url:
                tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
                number = int(tail) if tail.isdigit() else None
        else:
            gh(
                [
                    "pr",
                    "edit",
                    str(existing_pr),
                    "-R",
                    repo,
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                ]
            )
            status = "UPDATED"
            number = existing_pr
            pr_url = existing_url or fetch_pr_url(repo, existing_pr)
    finally:
        _unlink(body_file)

    if number is not None:
        apply_severity_label(repo, number, findings)

    if status == "UPDATED" and number is not None:
        comment = render_delta_comment(
            audit_id, new_ids, resolved_ids, findings, previous_titles, now
        )
        if comment:
            comment_file = _write_temp(comment)
            try:
                gh(["pr", "comment", str(number), "-R", repo, "-F", comment_file])
            finally:
                _unlink(comment_file)
        else:
            log("No new or resolved findings; body refreshed without a comment.")

    print(
        json.dumps(
            {
                "status": status,
                "pr_url": pr_url,
                "new": len(new_ids),
                "resolved": len(resolved_ids),
            }
        )
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic audit-PR harness for the fleet-audit skill."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    start_parser = subparsers.add_parser(
        "start", help="Refresh credentials, reset the audit branch, locate the open PR."
    )
    start_parser.add_argument(
        "--audit", required=True, help=f"Audit id: one of {', '.join(sorted(AUDITS))}."
    )

    finish_parser = subparsers.add_parser(
        "finish", help="Validate findings and publish/refresh/close the audit PR."
    )
    finish_parser.add_argument("--audit", required=True, help="Audit id.")
    finish_parser.add_argument(
        "--findings-file", required=True, help="Path to the findings.json to publish."
    )
    finish_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and render to stdout; perform zero git/gh side effects.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "start":
            handle_start(args)
        else:
            handle_finish(args)
    except ValidationError as exc:
        log(f"FINDINGS REJECTED: {exc}")
        return 2
    except subprocess.CalledProcessError as exc:
        log(f"FATAL: subprocess failed with exit code {exc.returncode}")
        return 1
    except Exception as exc:  # noqa: BLE001 — one actionable line beats a traceback in cron logs
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
