#!/opt/hermes/.venv/bin/python3
"""
audit_report.py — Deterministic reporting harness for the fleet-audit skill.

Every autonomous audit watchdog (compliance, security patch, obtainability,
cost, consistency drift) funnels its findings through this script. Each audit
stream owns exactly ONE open GitHub **issue** — its ledger — rewritten in place
on every run and closed as completed when the fleet comes back clean. Fixes
travel separately, as narrow remediation pull requests carrying only the files
that fix a specific group of findings, so a report is never a pull request with
no diff. The LLM's role is strictly constrained to **inspecting the fleet
read-only and emitting a findings.json**; every git/gh operation, every rendered
body, the commit subjects, the timestamps, and the run-over-run delta are
produced here, deterministically.

Two-command lifecycle, plus one on-demand command:

    audit_report.py start     --audit <audit-id>
    audit_report.py finish    --audit <audit-id> --findings-file <path> [--dry-run]
    audit_report.py remediate --audit <audit-id> --findings-file <path>
                          --finding <id> [--finding <id>...] [--dry-run]

The pure functions (validate/render/delta) carry no I/O and are unit tested in
test_audit_report.py; the thin shell below them owns all subprocess execution.
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
SEVERITY_RANK = {severity: i for i, severity in enumerate(SEVERITIES)}
REMEDIATION_KINDS = ("manifest", "gcloud", "manual")

# Every finding must carry all three. The hint is quoted back in the rejection,
# because "recommendation.rationale is required" does not tell the model what
# distinguishes a rationale from a restated action.
RECOMMENDATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("action", "what to do, imperative, one or two sentences"),
    (
        "rationale",
        "why this fix and not the obvious alternative; name the alternative you "
        "considered and why you rejected it",
    ),
    ("risk", "what breaks on apply, and the read-only check to run first"),
)

PROTECTED_BRANCHES = {"main", "master", "production"}
BASE_BRANCH = "main"
SCRATCH_DIR = "/opt/data/scratch"

# Wildcard stagers that must never reach `git add` — an audit stages named
# remediation files only, never the whole working tree.
FORBIDDEN_ADD_PATHSPECS = {".", "-A", "--all", "-a", "*", ":/", "./", ":"}

# Glob metacharacters git expands in a pathspec. `git --literal-pathspecs` is
# the real guard (see build_git_add_command); rejecting these at validation time
# means the refusal names the offending finding instead of silently staging the
# wrong files.
GLOB_METACHARACTERS = "*?[]"

# A finding id becomes a component of the remediation branch
# `platform-agent/fix-<audit-id>-<finding-id>`, so it must survive
# `git check-ref-format`: no ':', no whitespace, no '..' run, no '.lock' suffix.
FINDING_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?$")

# The hidden block that makes the run-over-run delta computable without keeping
# any state outside the report itself. Anchored to line boundaries so an opener
# pasted inside a fenced evidence excerpt cannot start a match that swallows the
# real block further down the body.
DELTA_RE = re.compile(
    r"^[ \t]*<!--\s*audit-findings:\s*(\[.*?\])\s*-->[ \t]*$", re.M | re.S
)
# Per-finding marker on each heading, so a *resolved* finding can still be named
# by title when it no longer exists in the current findings.json.
FINDING_MARKER_RE = re.compile(
    r"^####\s+(.*?)\s*<!--\s*finding:\s*(\S+?)\s*-->\s*$", re.M
)

# Idempotency markers. Design §3.1 deliberately never mutates a `/remediate`
# comment — a repo writer must be able to re-issue one after closing a PR — so
# "act exactly once" is carried instead by hidden markers in the bodies this
# harness already owns, the same technique the delta block uses.
PERSISTS_MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*audit-persists:\s*(\S+?)\s*-->[ \t]*$", re.M
)
REFUSED_MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*audit-refused:\s*(\S+?)\s*-->[ \t]*$", re.M
)

# `/remediate <finding-id>` / `/remediate all`, at the start of a line.
REMEDIATE_RE = re.compile(r"^[ \t]*/remediate[ \t]+(\S+)[ \t]*$", re.M)
# Fenced code blocks are stripped before command matching, so a `/remediate`
# quoted inside an evidence excerpt never fires.
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.M | re.S)

MAX_EXCERPT_LINES = 40
MAX_EXCERPT_CHARS = 2000
# The SOPs mandate pasting the evidence command verbatim, which makes it the
# dominant per-finding term; trim_excerpt guards the wrong field on its own.
MAX_COMMAND_CHARS = 2000

# GitHub rejects an issue or pull-request body over 65,536 characters with a
# 422. Issue bodies carry the identical limit, so this budget is the difference
# between a stream that publishes and one that 422s every morning forever.
MAX_BODY_CHARS = 65_536
BODY_BUDGET = 60_000
MAX_SCOPE_ROWS = 60
MAX_DELTA_ROWS = 50

# Auto-promotion ceiling per `finish` run (design §3.1). An explicit
# `/remediate` bypasses it: a human asked for that one by name.
AUTO_PROMOTION_CAP = 5

# `authorAssociation` values that imply write access, and therefore the standing
# to issue `/remediate`.
WRITE_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


class ValidationError(ValueError):
    """A findings.json (or audit id) that the harness refuses to publish."""


class BodyTooLargeError(ValidationError):
    """A rendered body that still exceeds GitHub's limit after budgeting.

    Subclasses ValidationError so it exits 2 — the code every SOP's step 5
    already branches on — rather than surfacing as an opaque fatal.
    """


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
    found = [char for char in GLOB_METACHARACTERS if char in path]
    if found:
        raise ValidationError(
            f"{where}: must name one literal file, not a glob "
            f"(contains {', '.join(repr(c) for c in found)}), got {path!r}"
        )
    if path.startswith(":"):
        raise ValidationError(
            f"{where}: must not begin with ':' — git reads a leading colon as a "
            f"pathspec magic prefix, not a filename; got {path!r}"
        )
    return path


def validate_finding_id(fid: str, where: str) -> str:
    """A finding id is a git ref component, not just a delta key.

    Design §2 names the remediation branch
    `platform-agent/fix-<audit-id>-<finding-id>`, so an unconstrained id yields
    a branch `git check-ref-format` rejects — not merely a churning delta.
    """
    if not FINDING_ID_RE.match(fid):
        raise ValidationError(
            f"{where}: {fid!r} is not a usable id. Use 1-100 characters matching "
            "[a-z0-9._-], starting and ending alphanumeric — the id becomes part "
            "of a git branch name, so ':', whitespace and uppercase are refused"
        )
    if ".." in fid:
        raise ValidationError(
            f"{where}: {fid!r} contains '..', which git refuses in a ref name"
        )
    if fid.endswith(".lock"):
        raise ValidationError(
            f"{where}: {fid!r} ends in '.lock', which git refuses in a ref name"
        )
    return fid


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
    audited_names: set[str] = set()
    for i, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"scope.clusters[{i}]: expected an object")
        for field in ("name", "location", "project"):
            _require_str(
                cluster.get(field), f"scope.clusters[{i}].{field}", allow_empty=False
            )
        audited_names.add(str(cluster["name"]))
        # Optional, but non-empty when present: "I read this cluster fine, but
        # some checks did not run or do not apply" is a different claim from
        # "I could not read this cluster", and conflating the two produces
        # false all-clears.
        if "limitations" in cluster:
            _require_str(
                cluster.get("limitations"),
                f"scope.clusters[{i}].limitations",
                allow_empty=False,
            )

    skipped = scope.get("skipped", [])
    if not isinstance(skipped, list):
        raise ValidationError(
            "scope.skipped: must be a list (use [] when nothing was skipped)"
        )
    skipped_names: set[str] = set()
    for i, entry in enumerate(skipped):
        if not isinstance(entry, dict):
            raise ValidationError(f"scope.skipped[{i}]: expected an object")
        _require_str(
            entry.get("cluster"), f"scope.skipped[{i}].cluster", allow_empty=False
        )
        _require_str(
            entry.get("reason"), f"scope.skipped[{i}].reason", allow_empty=False
        )
        name = str(entry["cluster"])
        if name in audited_names:
            raise ValidationError(
                f"scope.skipped[{i}].cluster: {name!r} is also in scope.clusters. "
                "A cluster belongs to exactly one list — if you read it but some "
                "checks did not run, drop it from scope.skipped and describe the "
                "gap in that cluster's scope.clusters[].limitations instead"
            )
        if name in skipped_names:
            raise ValidationError(
                f"scope.skipped[{i}].cluster: duplicate entry for {name!r}"
            )
        skipped_names.add(name)

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
        validate_finding_id(fid, f"findings[{i}].id")
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
        if str(finding["cluster"]) in skipped_names:
            raise ValidationError(
                f"findings[{i}].cluster: {finding['cluster']!r} is listed in "
                "scope.skipped, so this run claims it could not read it — a finding "
                "against it is a contradiction. Move the cluster to scope.clusters "
                "(with a limitations note) or drop the finding"
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

        # Required on EVERY finding, not only the promotable ones. Deferring the
        # reasoning to promotion time means writing it when the evidence is no
        # longer in front of you.
        recommendation = finding.get("recommendation")
        if not isinstance(recommendation, dict):
            raise ValidationError(
                f"findings[{i}].recommendation: required object with 'action', "
                "'rationale' and 'risk'"
            )
        for field, hint in RECOMMENDATION_FIELDS:
            value = recommendation.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"findings[{i}].recommendation.{field}: required, must be a "
                    f"non-empty string — {hint}"
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
    # --literal-pathspecs is git-level, not add-level (`git add
    # --literal-pathspecs` is an error), and it is the only thing that actually
    # holds: `git add -- '*.yaml'` still expands and stages files the audit
    # never declared. The `--` separator alone does not disable globbing.
    return ["git", "--literal-pathspecs", "add", "--", *paths]


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


def issue_title(audit_id: str, findings: list[dict]) -> str:
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
# Pure helpers — remediation grouping
# --------------------------------------------------------------------------- #


def _finding_paths(finding: dict) -> set[str]:
    """The repo paths a finding's remediation would touch (empty unless manifest)."""
    remediation = finding.get("remediation") or {}
    if remediation.get("kind") != "manifest":
        return set()
    path = str(remediation.get("path", "") or "")
    return {path} if path else set()


def remediation_groups(findings: list[dict]) -> list[list[dict]]:
    """Group manifest findings whose remediation paths intersect, transitively.

    Two findings that write the same file cannot own separate pull requests —
    the second would conflict with the first. This is not hypothetical: the
    compliance SOP tells the agent to point every finding in a namespace at one
    shared `default-sa-automount.yaml`.

    Union-find over paths rather than a plain group-by, so the grouping stays
    correct if a finding ever declares more than one path.
    """
    manifest = [f for f in findings if _finding_paths(f)]
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    for finding in manifest:
        for path in _finding_paths(finding):
            parent.setdefault(path, path)
    for finding in manifest:
        paths = sorted(_finding_paths(finding))
        for path in paths[1:]:
            union(paths[0], path)

    buckets: dict[str, list[dict]] = {}
    for finding in manifest:
        root = find(sorted(_finding_paths(finding))[0])
        buckets.setdefault(root, []).append(finding)

    groups = [
        sorted(group, key=lambda f: str(f.get("id", ""))) for group in buckets.values()
    ]
    groups.sort(key=lambda group: str(group[0].get("id", "")))
    return groups


def group_branch_for(audit_id: str, group: list[dict]) -> str:
    """Name a remediation branch after the lowest-sorted finding in its group.

    The branch name is the only durable link between a finding and its pull
    request — one `gh pr list --json headRefName` reconstructs the whole mapping
    with no state kept anywhere else.
    """
    ids = sorted(str(f.get("id", "")) for f in group if f.get("id"))
    if not ids:
        raise ValueError("cannot name a remediation branch for an empty group")
    return f"platform-agent/fix-{audit_id}-{ids[0]}"


def group_paths(group: list[dict]) -> list[str]:
    """Every path the group stages, sorted and de-duplicated."""
    paths: set[str] = set()
    for finding in group:
        paths |= _finding_paths(finding)
    return sorted(paths)


# --------------------------------------------------------------------------- #
# Pure helpers — /remediate commands
# --------------------------------------------------------------------------- #


def strip_fenced_blocks(text: str) -> str:
    """Drop fenced code blocks so a `/remediate` quoted in evidence never fires."""
    return FENCE_RE.sub("", text or "")


def parse_remediate_commands(
    comments: list[dict], findings: list[dict]
) -> tuple[list[str], list[dict]]:
    """Read `/remediate` requests off the ledger issue.

    Returns (authorized finding ids, refusals). A refusal is one entry per
    comment, not per bad target, because the reply is posted once per comment
    and marked with that comment's node id.
    """
    by_id = {str(f.get("id", "")): f for f in findings}
    promotable = {
        fid
        for fid, finding in by_id.items()
        if (finding.get("remediation") or {}).get("kind") == "manifest"
    }

    targets: set[str] = set()
    refusals: list[dict] = []

    for comment in comments or []:
        body = strip_fenced_blocks(str(comment.get("body", "")))
        matches = REMEDIATE_RE.findall(body)
        if not matches:
            continue

        node_id = str(comment.get("id", "") or "")
        author = str((comment.get("author") or {}).get("login", "") or "") or "someone"
        association = str(comment.get("authorAssociation", "") or "").upper()
        reasons: list[str] = []

        if association not in WRITE_ASSOCIATIONS:
            refusals.append(
                {
                    "comment_id": node_id,
                    "author": author,
                    "reasons": [
                        f"@{author} does not have write access to this repository "
                        f"(`authorAssociation: {association or 'NONE'}`). A remediation "
                        "pull request may only be requested by someone who could merge it."
                    ],
                }
            )
            continue

        for raw in matches:
            target = raw.strip().strip("`")
            if target == "all":
                targets |= promotable
                continue
            if target not in by_id:
                reasons.append(
                    f"`{target}` is not a finding in the current report — it may have "
                    "been resolved, or the id may be a typo."
                )
                continue
            if target not in promotable:
                kind = (by_id[target].get("remediation") or {}).get("kind")
                reasons.append(
                    f"`{target}` has a `{kind}` remediation, not a `manifest` one. "
                    "Only a finding whose fix is a file in this repository can become "
                    "a pull request; run the command in the report instead."
                )
                continue
            targets.add(target)

        if reasons:
            refusals.append(
                {"comment_id": node_id, "author": author, "reasons": reasons}
            )

    return sorted(targets), refusals


def pending_remediate_targets(comments: list[dict]) -> list[str]:
    """Ids named by an authorized `/remediate`, before any findings exist.

    `start` runs before the fleet is inspected, so there is nothing to validate
    a target against yet. This reports what was asked for, so the agent knows
    which remediation files to write while it inspects; `finish` then applies
    the full `parse_remediate_commands` gate against the real finding set.
    `all` is not expanded here — it names no specific file to write.
    """
    targets: set[str] = set()
    for comment in comments or []:
        association = str(comment.get("authorAssociation", "") or "").upper()
        if association not in WRITE_ASSOCIATIONS:
            continue
        body = strip_fenced_blocks(str(comment.get("body", "")))
        for raw in REMEDIATE_RE.findall(body):
            target = raw.strip().strip("`")
            if target and target != "all":
                targets.add(target)
    return sorted(targets)


# --------------------------------------------------------------------------- #
# Pure helpers — finding state and promotion
# --------------------------------------------------------------------------- #

STATE_OPEN = "open"
STATE_PR_OPEN = "pr-open"
STATE_PR_MERGED_PERSISTS = "pr-merged-persists"
STATE_RESOLVED_MERGED = "resolved-merged"
STATE_RESOLVED = "resolved"
STATE_REFUSED = "refused"

STATE_LABELS = {
    STATE_OPEN: "open",
    STATE_PR_OPEN: "fix proposed",
    STATE_PR_MERGED_PERSISTS: "⚠ fix merged, still reproduces",
    STATE_RESOLVED_MERGED: "resolved (fix merged)",
    STATE_RESOLVED: "resolved",
    STATE_REFUSED: "fix refused",
}


def derive_finding_state(reproduces: bool, pr: dict | None) -> str:
    """The §4 state of one finding, from whether it reproduces and its PR.

    `pr` is the remediation pull request found on the finding's branch, or None.
    A merged PR whose finding still reproduces is the case the old rolling-PR
    model could not express at all.
    """
    state = str((pr or {}).get("state", "") or "").upper()
    merged = state == "MERGED" or bool((pr or {}).get("mergedAt"))

    if reproduces:
        if pr is None:
            return STATE_OPEN
        if merged:
            return STATE_PR_MERGED_PERSISTS
        if state == "OPEN":
            return STATE_PR_OPEN
        return STATE_REFUSED
    return STATE_RESOLVED_MERGED if merged else STATE_RESOLVED


def promotion_candidates(
    findings: list[dict],
    pr_by_finding: dict[str, dict | None],
    requested: list[str] | None = None,
    cap: int = AUTO_PROMOTION_CAP,
) -> tuple[list[str], list[str]]:
    """Split findings into (promote now, withheld for an explicit request).

    Auto-promotion is deliberately narrow — `critical`, `manifest`, and no pull
    request on its branch in any state — and capped, so one bad night cannot
    bury the repository in generated pull requests. An explicit `/remediate`
    bypasses the cap: a human asked for that one by name.

    The cap counts findings, and a group of findings sharing a path collapses to
    one pull request, so the number of PRs opened is at most `cap`.
    """
    by_id = {str(f.get("id", "")): f for f in findings}
    requested_set = {fid for fid in (requested or []) if fid in by_id}

    promote = [
        fid
        for fid in sorted(requested_set)
        if (by_id[fid].get("remediation") or {}).get("kind") == "manifest"
    ]

    auto: list[str] = []
    for finding in sort_findings(findings):
        fid = str(finding.get("id", ""))
        if fid in requested_set:
            continue
        if finding.get("severity") != "critical":
            continue
        if (finding.get("remediation") or {}).get("kind") != "manifest":
            continue
        if pr_by_finding.get(fid) is not None:
            continue
        auto.append(fid)

    promote.extend(auto[:cap])
    return promote, auto[cap:]


# --------------------------------------------------------------------------- #
# Pure helpers — idempotency markers
# --------------------------------------------------------------------------- #


def persists_marker(finding_id: str) -> str:
    return f"<!-- audit-persists:{finding_id} -->"


def refused_marker(comment_id: str) -> str:
    return f"<!-- audit-refused:{comment_id} -->"


def has_marker(text: str | None, pattern: re.Pattern[str], value: str) -> bool:
    """True when `text` already carries this marker.

    Design §3.1 keeps `/remediate` comments unmutated on purpose, so a repo
    writer can re-issue one after closing a pull request. "Act exactly once"
    therefore lives in the bodies the harness owns, not in the command.
    """
    if not text:
        return False
    return value in {match for match in pattern.findall(text)}


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


def _clip_comment(text: str) -> str:
    """Last-resort clip for a comment body.

    Comments are already row-capped; unlike the description a clipped comment
    loses nothing durable, so this truncates rather than raising.
    """
    if len(text) <= MAX_BODY_CHARS:
        return text
    keep = MAX_BODY_CHARS - 120
    return text[:keep].rstrip() + "\n\n_… (comment truncated by audit_report.py)_"


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
        text += "\n… (excerpt truncated by audit_report.py — re-run the command above for the full output)"
    return text


def trim_command(command: str) -> str:
    """Clip the evidence command.

    The SOPs require pasting the command verbatim, so on a fleet with long
    `--context`/`-o jsonpath` invocations it — not the excerpt — is the term
    that grows without bound. A truncated command is still a usable pointer;
    an unpublishable body is not.
    """
    text = (command or "").strip()
    if len(text) <= MAX_COMMAND_CHARS:
        return text
    return (
        text[:MAX_COMMAND_CHARS].rstrip()
        + "\n# … (command truncated by audit_report.py)"
    )


def _finding_sort_key(finding: dict) -> tuple:
    """Stable ordering, so an unchanged fleet renders a byte-identical findings section.

    Not a byte-identical *body*: the header and footer each carry a generated
    timestamp, so two runs over an unchanged fleet always differ by those lines.
    """
    return (
        str(finding.get("cluster", "")),
        str(finding.get("namespace", "")),
        str(finding.get("object", "")),
        str(finding.get("title", "")),
        str(finding.get("id", "")),
    )


def render_finding(
    finding: dict, *, state: str | None = None, pr_url: str | None = None
) -> list[str]:
    fid = str(finding.get("id", ""))
    title = str(finding.get("title", "")).strip()
    namespace = str(finding.get("namespace", "")).strip()
    where = f"`{finding.get('cluster', '')}`"
    where += f" / `{namespace}`" if namespace else " / _cluster-scoped_"

    lines = [f"#### {title} <!-- finding:{fid} -->", ""]
    lines.append(f"- **Where:** {where} — `{finding.get('object', '')}`")
    lines.append(f"- **Impact:** {finding.get('impact', '')}")
    if state:
        label = STATE_LABELS.get(state, state)
        suffix = f" — {pr_url}" if pr_url else ""
        lines.append(f"- **State:** {label}{suffix}")
        if state == STATE_PR_MERGED_PERSISTS:
            lines.append(
                "  The proposed fix was merged and this finding still reproduces. "
                "The remediation was incomplete, or something outside this "
                "repository reverted it — the merged pull request is not reopened."
            )
    lines.append("")

    evidence = finding.get("evidence") or {}
    command = trim_command(str(evidence.get("command", "")))
    lines.append("Evidence — reproduce with:")
    lines.append("")
    fence = _fence(command)
    lines += [f"{fence}bash", command, fence]

    excerpt = trim_excerpt(str(evidence.get("excerpt", "")))
    if excerpt:
        lines.append("")
        fence = _fence(excerpt)
        lines += [f"{fence}text", excerpt, fence]

    recommendation = finding.get("recommendation") or {}
    lines.append("")
    lines.append(f"- **Recommendation:** {recommendation.get('action', '')}")
    lines.append(f"- **Why this fix:** {recommendation.get('rationale', '')}")
    lines.append(f"- **Risk on apply:** {recommendation.get('risk', '')}")

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
        command_note = trim_command(note)
        fence = _fence(command_note)
        lines += [f"{fence}bash", command_note or "# (no command supplied)", fence]
    else:
        lines.append(f"- **Remediation (manual):** {note or '_none supplied_'}")
    return lines


def sort_findings(findings: list[dict]) -> list[dict]:
    """Severity-first, then the stable within-severity key.

    A pure function of the finding set, never of its input order — two runs over
    an unchanged fleet must produce the same findings section whatever order the
    model happened to emit.
    """
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_RANK.get(str(f.get("severity", "")), len(SEVERITIES)),
            _finding_sort_key(f),
        ),
    )


def select_rendered_findings(
    findings: list[dict],
    budget: int,
    *,
    states: dict[str, str] | None = None,
    pr_urls: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split the sorted findings into (rendered, omitted) against a char budget.

    Selection walks the severity-first order and stops at the first finding that
    does not fit, so the rendered set is always a prefix: truncation only ever
    eats the least-severe end, and criticals are structurally safe. At least one
    finding always renders — a body with a single oversized finding is still
    more useful than a body with none.

    Each finding is charged for its own rendered text *and* for the slot its id
    occupies in the hidden delta block, because that block is itself unbounded:
    1,250 ids render over 80,000 characters of marker alone.
    """
    ordered = sort_findings(findings)
    used = 0
    fitted = 0
    for finding in ordered:
        fid = str(finding.get("id", ""))
        # Charged against the *rendered* text, state line included: the state
        # and PR link are per-finding, so estimating without them would
        # under-count by a few thousand characters across a full body.
        rendered = render_finding(
            finding,
            state=(states or {}).get(fid),
            pr_url=(pr_urls or {}).get(fid),
        )
        cost = len("\n".join(rendered)) + 2
        cost += len(fid) + 3  # its slot in the hidden delta block
        if fitted and used + cost > budget:
            break
        used += cost
        fitted += 1
    return ordered[:fitted], ordered[fitted:]


def _render_header(audit_id: str) -> list[str]:
    return [
        f"This issue is the ledger for the `{audit_id}` audit. It is rewritten in "
        "full on every run — hand edits to this description will be lost, and the "
        "audit will never open a second ledger for this stream. It closes when the "
        "audit comes back clean.",
        "",
        "Fixes are proposed as separate remediation pull requests, one per group of "
        "findings that share a file, linked from each finding below. To ask for one "
        "that was not opened automatically, comment `/remediate <finding-id>` (or "
        "`/remediate all`) — you need write access to this repository, and only a "
        "finding whose remediation is a file in this repository can become a pull "
        "request.",
    ]


def _render_scope(
    clusters: list[dict], skipped: list[dict], generated_at: datetime
) -> list[str]:
    """The scope tables, row-capped.

    A body with *zero* findings overflows without this cap: 1,200 audited plus
    1,200 skipped clusters render over 148,000 characters of table.
    """
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    show_limitations = any(str(c.get("limitations", "")).strip() for c in clusters)

    out = ["", "## Scope", "", f"Audited {len(clusters)} cluster(s) on {stamp}."]
    if show_limitations:
        out += [
            "",
            "| Cluster | Location | Project | Limitations |",
            "| ------- | -------- | ------- | ----------- |",
        ]
    else:
        out += [
            "",
            "| Cluster | Location | Project |",
            "| ------- | -------- | ------- |",
        ]
    for cluster in clusters[:MAX_SCOPE_ROWS]:
        row = (
            f"| `{_cell(cluster.get('name', ''))}` "
            f"| {_cell(cluster.get('location', ''))} "
            f"| `{_cell(cluster.get('project', ''))}` "
        )
        if show_limitations:
            row += f"| {_cell(cluster.get('limitations', '')) or '—'} "
        out.append(row + "|")
    if len(clusters) > MAX_SCOPE_ROWS:
        remaining = len(clusters) - MAX_SCOPE_ROWS
        columns = 4 if show_limitations else 3
        out.append(f"| _…and {remaining} more_ " + "|  " * (columns - 1) + "|")

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
        for entry in skipped[:MAX_SCOPE_ROWS]:
            out.append(
                f"| `{_cell(entry.get('cluster', ''))}` | {_cell(entry.get('reason', ''))} |"
            )
        if len(skipped) > MAX_SCOPE_ROWS:
            out.append(f"| _…and {len(skipped) - MAX_SCOPE_ROWS} more_ |  |")
    return out


def _render_findings(
    findings: list[dict],
    budget: int,
    *,
    states: dict[str, str] | None = None,
    pr_urls: dict[str, str] | None = None,
) -> tuple[list[str], list[dict]]:
    """The findings section, plus the findings that did not fit the budget."""
    out = ["", "## Findings", ""]
    if not findings:
        out.append("No findings. Every audited cluster is compliant with this audit.")
        return out, []

    states = states or {}
    pr_urls = pr_urls or {}
    counts = severity_counts(findings)
    out.append(
        f"{findings_phrase(len(findings))}: {counts['critical']} critical, "
        f"{counts['major']} major, {counts['minor']} minor."
    )

    rendered, omitted = select_rendered_findings(
        findings, budget, states=states, pr_urls=pr_urls
    )

    # A one-row-per-finding index, so the state of the whole stream is legible
    # without scrolling through every evidence block.
    if any(states.get(str(f.get("id", ""))) for f in rendered):
        out += [
            "",
            "| Finding | Severity | Cluster | State |",
            "| ------- | -------- | ------- | ----- |",
        ]
        for finding in rendered[:MAX_DELTA_ROWS]:
            fid = str(finding.get("id", ""))
            state = states.get(fid, STATE_OPEN)
            label = STATE_LABELS.get(state, state)
            url = pr_urls.get(fid)
            out.append(
                f"| `{_cell(fid)}` | {_cell(str(finding.get('severity', '')))} "
                f"| `{_cell(str(finding.get('cluster', '')))}` "
                f"| {label}{f' ({url})' if url else ''} |"
            )
        if len(rendered) > MAX_DELTA_ROWS:
            out.append(
                f"| _…and {len(rendered) - MAX_DELTA_ROWS} more below_ |  |  |  |"
            )

    for severity in SEVERITIES:
        group = [f for f in rendered if f.get("severity") == severity]
        if not group:
            continue
        total = counts[severity]
        suffix = f"{len(group)} of {total}" if len(group) < total else str(total)
        out += ["", f"### {severity.capitalize()} ({suffix})"]
        for finding in group:
            fid = str(finding.get("id", ""))
            out.append("")
            out += render_finding(
                finding, state=states.get(fid), pr_url=pr_urls.get(fid)
            )

    if omitted:
        out += [
            "",
            f"_{len(omitted)} further finding(s) are omitted from this description to "
            "stay inside GitHub's body limit. The counts in the title and in the "
            "summary above are the true totals; the omitted findings are the "
            "least severe._",
        ]
    return out, omitted


def _render_footer(
    audit_id: str, generated_at: datetime, rendered_ids: list[str]
) -> list[str]:
    return [
        "",
        "---",
        "",
        f"Generated by the Platform Agent `{audit_id}` watchdog at "
        f"{generated_at.isoformat()}. Findings come from read-only inspection of the "
        "live fleet; every one carries the exact command it was derived from.",
        "",
        delta_block(rendered_ids),
        "",
    ]


def _render_withheld(withheld: list[str], findings: list[dict]) -> list[str]:
    """Name the findings eligible for a pull request that the cap held back.

    A cap that silently drops work reads as "nothing more to do". Naming them,
    with the command to ask for one, is what keeps the cap honest.
    """
    if not withheld:
        return []
    by_id = {str(f.get("id", "")): f for f in findings}
    out = [
        "",
        "## Awaiting `/remediate`",
        "",
        f"{len(withheld)} finding(s) qualify for an automatic remediation pull "
        f"request but were held back by the cap of {AUTO_PROMOTION_CAP} per run, so "
        "one bad night cannot bury this repository in generated pull requests. "
        "Comment `/remediate <finding-id>` to open any of them now — an explicit "
        "request is not capped.",
        "",
    ]
    for fid in withheld:
        finding = by_id.get(fid) or {}
        out.append(f"- `{fid}` — {finding.get('title', '')}")
    return out


def render_issue_body(
    data: dict,
    *,
    generated_at: datetime,
    audit_id: str | None = None,
    states: dict[str, str] | None = None,
    pr_urls: dict[str, str] | None = None,
    withheld: list[str] | None = None,
) -> str:
    """Render the complete ledger issue body. The model never hand-writes this.

    Everything but the findings renders and is measured first; whatever is left
    of BODY_BUDGET is the findings budget. The hidden delta block carries the
    ids the body actually **rendered**, not the full finding set — otherwise the
    next run would read a truncated finding as resolved and announce a fix that
    never happened.
    """
    audit_id = audit_id or str(data.get("audit", ""))
    findings = list(data.get("findings") or [])
    scope = data.get("scope") or {}
    clusters = list(scope.get("clusters") or [])
    skipped = list(scope.get("skipped") or [])
    states = states or {}
    pr_urls = pr_urls or {}

    fixed: list[str] = _render_header(audit_id)
    fixed += _render_scope(clusters, skipped, generated_at)
    withheld_section = _render_withheld(list(withheld or []), findings)

    # Measure the footer with an empty delta block: each finding is separately
    # charged for its own id slot inside select_rendered_findings.
    overhead = len("\n".join(fixed + withheld_section))
    overhead += len("\n".join(_render_footer(audit_id, generated_at, [])))
    overhead += len("\n".join(["", "## Findings", "", ""])) + 400  # section chrome
    if states:
        # The state index is one row per rendered finding, capped, and is not
        # part of any single finding's charged cost.
        overhead += MAX_DELTA_ROWS * 160 + 200

    findings_lines, omitted = _render_findings(
        findings,
        max(BODY_BUDGET - overhead, 0),
        states=states,
        pr_urls=pr_urls,
    )
    omitted_ids = {str(f.get("id", "")) for f in omitted}
    rendered_ids = [fid for fid in finding_ids(findings) if fid not in omitted_ids]

    body = "\n".join(
        fixed
        + findings_lines
        + withheld_section
        + _render_footer(audit_id, generated_at, rendered_ids)
    )
    if len(body) > MAX_BODY_CHARS:
        raise BodyTooLargeError(
            f"rendered body is {len(body)} characters, over GitHub's "
            f"{MAX_BODY_CHARS} limit even after budgeting to {BODY_BUDGET}; "
            "this is a harness bug, not a findings error — report it rather than "
            "trimming the audit"
        )
    return body


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
        for fid in new_ids[:MAX_DELTA_ROWS]:
            finding = by_id.get(fid, {})
            severity = str(finding.get("severity", "unknown"))
            title = str(finding.get("title", fid))
            out.append(f"- **{severity}** — {title} (`{fid}`)")
        if len(new_ids) > MAX_DELTA_ROWS:
            out.append(f"- _…and {len(new_ids) - MAX_DELTA_ROWS} more_")
        out.append("")

    if resolved_ids:
        out.append(f"**{len(resolved_ids)} resolved**")
        out.append("")
        for fid in resolved_ids[:MAX_DELTA_ROWS]:
            title = previous_titles.get(fid) or fid
            out.append(f"- {title} (`{fid}`)")
        if len(resolved_ids) > MAX_DELTA_ROWS:
            out.append(f"- _…and {len(resolved_ids) - MAX_DELTA_ROWS} more_")
        out.append("")

    out.append(
        "The ledger description has been rewritten to the current state of the fleet."
    )
    # Capping the body made this path reachable: previously the body failed
    # first at ~67 findings, so a delta this large could never be produced.
    return _clip_comment("\n".join(out))


def render_clean_comment(
    audit_id: str, data: dict, generated_at: datetime
) -> str:
    """Comment posted when an audit that previously had findings comes back clean."""
    scope = data.get("scope") or {}
    clusters = list(scope.get("clusters") or [])
    skipped = list(scope.get("skipped") or [])
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    shown = clusters[:MAX_SCOPE_ROWS]
    names = ", ".join(f"`{c.get('name', '')}`" for c in shown)
    if len(clusters) > len(shown):
        names += f", and {len(clusters) - len(shown)} more"

    out = [
        f"### `{audit_id}` is now clean — closing",
        "",
        f"The {audit_name(audit_id)} run on {stamp} found **0 findings** across "
        f"{len(clusters)} audited cluster(s): {names}.",
        "",
        "Every finding previously reported here is gone, so this ledger is being "
        "closed as completed. The next run that finds anything opens a fresh one.",
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
            for entry in skipped[:MAX_SCOPE_ROWS]
        ]
        if len(skipped) > MAX_SCOPE_ROWS:
            out.append(f"- _…and {len(skipped) - MAX_SCOPE_ROWS} more_")
    return _clip_comment("\n".join(out))


def _select_pr_by_head(prs: list[dict], branch: str) -> dict | None:
    """The most recent pull request on `branch`, or None.

    Highest number wins, so a branch that was merged and then re-opened
    reports its current pull request rather than the historical one.

    The comparison assumes the branch lives in the same repository, which is
    what `open_remediation_pr` does. `headRefName` is a bare branch name for a
    same-repo pull request; if remediation branches ever move to a fork it
    arrives qualified, so the suffix form is accepted too rather than having
    every lookup silently miss.
    """
    matches = [
        pr
        for pr in prs or []
        if str(pr.get("headRefName", "")) == branch
        or str(pr.get("headRefName", "")).endswith(f":{branch}")
    ]
    if not matches:
        return None
    matches.sort(key=lambda p: int(p.get("number", 0)))
    return matches[-1]


def pr_is_merged(pr: dict | None) -> bool:
    if not pr:
        return False
    return str(pr.get("state", "")).upper() == "MERGED" or bool(pr.get("mergedAt"))


def remediation_pr_title(audit_id: str, group: list[dict]) -> str:
    """One subject for the whole group, named after what it fixes."""
    ordered = sort_findings(group)
    head = ordered[0]
    extra = len(ordered) - 1
    suffix = f" (+{extra} more)" if extra else ""
    return f"fix({audit_id}): {head.get('title', '')}{suffix}"


def group_commit_subject(audit_id: str, group: list[dict]) -> str:
    ids = ", ".join(sorted(str(f.get("id", "")) for f in group))
    return f"fix({audit_id}): remediate {ids}"


def render_remediation_pr_body(
    audit_id: str,
    group: list[dict],
    *,
    issue_number: int | None,
    generated_at: datetime,
) -> str:
    """The body of one remediation pull request.

    It carries the same hidden `audit-findings` block the ledger uses, which is
    what makes a pull request self-describing: the next run reads the block to
    learn which findings this pull request was opened for, with no state kept
    anywhere else.
    """
    ordered = sort_findings(group)
    ids = sorted(str(f.get("id", "")) for f in ordered if f.get("id"))
    paths = group_paths(ordered)

    out = [
        f"Proposed fix for {findings_phrase(len(ordered))} from the "
        f"`{audit_id}` audit. The audit inspected the fleet read-only; this pull "
        "request is the only thing it proposes to change, and applying it is a "
        "human decision.",
    ]
    if issue_number:
        # Not "Closes": the ledger closes when the audit comes back clean, not
        # when one of its fixes merges.
        out += ["", f"Part of #{issue_number}"]

    out += ["", "## Findings this fixes", ""]
    for finding in ordered:
        out += render_finding(finding)
        out.append("")

    out += ["## Files", ""]
    for path in paths:
        out.append(f"- [`{path}`]({path})")

    out += [
        "",
        "---",
        "",
        f"Generated by the Platform Agent `{audit_id}` watchdog at "
        f"{generated_at.isoformat()}. If this fix is wrong, close this pull "
        "request — the finding stays on the ledger and no replacement is opened "
        "automatically.",
        "",
        delta_block(ids),
        "",
    ]

    body = "\n".join(out)
    if len(body) > MAX_BODY_CHARS:
        # A group is at most a handful of findings, so this is a harness bug
        # rather than something an audit can talk its way out of.
        raise BodyTooLargeError(
            f"remediation body for {ids} is {len(body)} characters, over "
            f"GitHub's {MAX_BODY_CHARS} limit"
        )
    return body


def render_stale_close_comment(
    audit_id: str, findings: list[dict], generated_at: datetime
) -> str:
    """Why a remediation pull request is being closed unmerged."""
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"Closing unmerged: as of {stamp} the `{audit_id}` audit no longer "
        "reproduces the finding(s) this pull request was opened for. Something "
        "else fixed them, or the objects are gone.",
        "",
    ]
    for finding in sort_findings(findings):
        out += [f"**`{finding.get('id', '')}` — {finding.get('title', '')}**", ""]
        # A resolved finding is absent from the current document, so its command
        # is only known when a previous body recorded it. Say nothing rather
        # than print an empty code fence.
        command = trim_command(str((finding.get("evidence") or {}).get("command", "")))
        if command:
            out += [
                "The command below no longer shows the deviation:",
                "",
                _fence(command),
                "",
            ]
    out.append(
        "The branch is left in place. If the finding comes back, the audit "
        "opens a fresh pull request on it; nothing here is lost."
    )
    return "\n".join(out)


def render_persists_comment(
    audit_id: str, finding: dict, generated_at: datetime
) -> str:
    """Said once, on a merged pull request whose finding still reproduces."""
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    evidence = finding.get("evidence") or {}
    return "\n".join(
        [
            f"This fix merged, but as of {stamp} the `{audit_id}` audit still "
            f"reproduces `{finding.get('id', '')}` — {finding.get('title', '')}.",
            "",
            "Either the remediation was incomplete, or something outside this "
            "repository reverted it. This pull request is **not** reopened: it "
            "merged, and reopening it would misrepresent history. The finding "
            "stays on the ledger, flagged, until it stops reproducing.",
            "",
            "Current evidence:",
            "",
            _fence(trim_command(str(evidence.get("command", "")))),
            "",
            _fence(trim_excerpt(str(evidence.get("excerpt", "")))),
            "",
            persists_marker(str(finding.get("id", ""))),
        ]
    )


def render_refusal_comment(refusal: dict, generated_at: datetime) -> str:
    """Said once per `/remediate` comment the harness will not act on."""
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"@{refusal.get('author', 'someone')} — that `/remediate` was not acted "
        f"on ({stamp}):",
        "",
    ]
    out += [f"- {reason}" for reason in refusal.get("reasons") or []]
    out += ["", refused_marker(str(refusal.get("comment_id", "")))]
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
            "Not inside a git working tree; run `audit_report.py start` first"
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
        (
            "audit:remediation",
            "0E8A16",
            "Pull request proposing a fix for one group of audit findings",
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


class GitHubLookupError(RuntimeError):
    """A GitHub lookup failed in a way that must not be read as 'nothing found'."""


def find_existing_issue(repo: str, audit_id: str) -> tuple[int | None, str | None]:
    """The audit's single open ledger issue, if any. Lowest number wins.

    Raises rather than reporting "none" when the lookup itself fails. The old
    code returned (None, None) on a non-zero exit, which made a `gh` outage
    indistinguishable from an empty result: the run would open a duplicate
    ledger, or on a clean run report CLEAN having closed nothing.
    """
    res = gh(
        [
            "issue",
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
        raise GitHubLookupError(
            f"could not list issues for audit:{audit_id} in {repo} "
            f"(gh exited {res.returncode}): {(res.stderr or '').strip()[:200]}"
        )
    try:
        issues = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubLookupError(
            f"gh issue list returned output that is not JSON: {exc}"
        ) from exc
    if not isinstance(issues, list) or not issues:
        return None, None
    issues.sort(key=lambda p: int(p.get("number", 0)))
    if len(issues) > 1:
        log(
            f"WARNING: {len(issues)} open issues carry label audit:{audit_id}; "
            f"updating #{issues[0].get('number')} and leaving the rest alone. "
            "Close the duplicates."
        )
    return int(issues[0]["number"]), issues[0].get("url")


def fetch_issue_body(repo: str, number: int) -> str | None:
    """The ledger's current body, or None when it could not be read.

    None and "" are different answers. An unreadable body means the delta is
    unknowable; treating it as empty would announce every live finding as new.
    """
    res = gh(["issue", "view", str(number), "-R", repo, "--json", "body"], check=False)
    if res.returncode != 0:
        log(
            f"WARNING: could not read issue #{number} (gh exited {res.returncode}); "
            "skipping the delta comment rather than reporting every finding as new."
        )
        return None
    try:
        return str(json.loads(res.stdout or "{}").get("body") or "")
    except json.JSONDecodeError:
        log(f"WARNING: issue #{number} body came back as non-JSON; skipping the delta.")
        return None


def fetch_issue_url(repo: str, number: int) -> str | None:
    res = gh(["issue", "view", str(number), "-R", repo, "--json", "url"], check=False)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "{}").get("url")
    except json.JSONDecodeError:
        return None


def fetch_issue_comments(repo: str, number: int) -> list[dict]:
    """Comments on the ledger, for `/remediate` parsing. Empty on failure."""
    res = gh(
        ["issue", "view", str(number), "-R", repo, "--json", "comments"], check=False
    )
    if res.returncode != 0:
        log(f"WARNING: could not read comments on issue #{number}; treating as none.")
        return []
    try:
        comments = json.loads(res.stdout or "{}").get("comments") or []
    except json.JSONDecodeError:
        log(f"WARNING: comments on issue #{number} came back as non-JSON.")
        return []
    return [c for c in comments if isinstance(c, dict)]


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
    """Tag the ledger with its highest live severity so triage can sort by it."""
    counts = severity_counts(findings)
    highest = next((s for s in SEVERITIES if counts[s]), None)
    if highest is None:
        return
    args = [
        "issue",
        "edit",
        str(number),
        "-R",
        repo,
        "--add-label",
        f"severity:{highest}",
    ]
    for severity in SEVERITIES:
        if severity != highest:
            args += ["--remove-label", f"severity:{severity}"]
    gh(args, check=False)


def post_comment(repo: str, number: int, text: str, *, what: str) -> None:
    """Comment on an issue, logging rather than aborting when GitHub refuses.

    A 422 on a comment used to skip the close that followed it, because the
    close sat outside the try/finally. A comment is a courtesy; the state
    change is the point.
    """
    comment_file = _write_temp(text)
    try:
        res = gh(
            ["issue", "comment", str(number), "-R", repo, "-F", comment_file],
            check=False,
        )
        if res.returncode != 0:
            log(
                f"WARNING: could not post the {what} on #{number} "
                f"(gh exited {res.returncode}); continuing."
            )
    finally:
        _unlink(comment_file)


def post_pr_comment(repo: str, number: int, text: str, *, what: str) -> None:
    """`gh pr comment`, with the same log-and-continue posture as post_comment."""
    comment_file = _write_temp(text)
    try:
        res = gh(
            ["pr", "comment", str(number), "-R", repo, "-F", comment_file],
            check=False,
        )
        if res.returncode != 0:
            log(
                f"WARNING: could not post the {what} on PR #{number} "
                f"(gh exited {res.returncode}); continuing."
            )
    finally:
        _unlink(comment_file)


# --------------------------------------------------------------------------- #
# I/O shell — remediation pull requests
# --------------------------------------------------------------------------- #


def list_remediation_prs(repo: str, audit_id: str) -> list[dict]:
    """Every remediation pull request this audit has ever opened.

    `--state all` on purpose: a merged pull request whose finding still
    reproduces is a state the report has to be able to show, and a closed one
    is what stops the harness re-opening a fix a human rejected.
    """
    res = gh(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--label",
            f"audit:{audit_id}",
            "--label",
            "audit:remediation",
            "--state",
            "all",
            "--json",
            "number,headRefName,state,mergedAt,url,body",
            "--limit",
            "200",
        ],
        check=False,
    )
    if res.returncode != 0:
        raise GitHubLookupError(
            f"could not list remediation pull requests for audit:{audit_id} in "
            f"{repo} (gh exited {res.returncode}): {(res.stderr or '').strip()[:200]}"
        )
    try:
        prs = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubLookupError(
            f"gh pr list returned output that is not JSON: {exc}"
        ) from exc
    return [p for p in prs if isinstance(p, dict)]


def reconcile_remediation_prs(
    audit_id: str, findings: list[dict], prs: list[dict]
) -> tuple[dict[str, dict | None], dict[str, str]]:
    """Map every live finding to the pull request on its group's branch.

    The branch name is the whole join key — no state is kept anywhere outside
    GitHub. Findings in one group share a branch, so they share a pull request
    and therefore a state.
    """
    pr_by_finding: dict[str, dict | None] = {}
    url_by_finding: dict[str, str] = {}
    for group in remediation_groups(findings):
        pr = _select_pr_by_head(prs, group_branch_for(audit_id, group))
        for finding in group:
            fid = str(finding.get("id", ""))
            pr_by_finding[fid] = pr
            if pr and pr.get("url"):
                url_by_finding[fid] = str(pr["url"])
    return pr_by_finding, url_by_finding


def snapshot_paths(root: Path, paths: list[str]) -> dict[str, bytes]:
    """Read the remediation files before any branch switch touches them."""
    return {path: (root / path).read_bytes() for path in paths}


def open_remediation_pr(
    repo: str,
    audit_id: str,
    group: list[dict],
    *,
    snapshot: dict[str, bytes],
    root: Path,
    issue_number: int | None,
    existing: dict | None,
    generated_at: datetime,
) -> str | None:
    """Branch off main, write the group's files, push, and open/refresh its PR.

    `finish` owns the working tree while it runs: the checkout is forced, and
    the files are re-materialised from `snapshot` afterwards, because a branch
    switch is the only way to get a diff against `main` and an unforced switch
    fails whenever `main` already carries a path the agent left untracked.
    Do not leave unrelated uncommitted work in the tree during an audit.
    """
    branch = assert_pushable(group_branch_for(audit_id, group))
    paths = group_paths(group)

    git(["fetch", "origin", BASE_BRANCH])
    git(["checkout", "--force", "-B", branch, f"origin/{BASE_BRANCH}"])

    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot[path])

    git(build_git_add_command(paths)[1:])
    res = git(["commit", "-m", group_commit_subject(audit_id, group)], check=False)
    if res.returncode != 0:
        # Nothing to commit means main already carries this exact fix. Opening
        # an empty pull request would be the rolling-PR mistake all over again.
        log(
            f"{branch}: the remediation is already present on {BASE_BRANCH}; "
            "no pull request opened."
        )
        return None
    git(["push", "-f", "origin", branch])

    body_file = _write_temp(
        render_remediation_pr_body(
            audit_id, group, issue_number=issue_number, generated_at=generated_at
        )
    )
    title = remediation_pr_title(audit_id, group)
    highest = next(
        (s for s in SEVERITIES if severity_counts(group)[s]), SEVERITIES[-1]
    )
    try:
        if existing and str(existing.get("state", "")).upper() == "OPEN":
            gh(
                [
                    "pr",
                    "edit",
                    str(existing["number"]),
                    "-R",
                    repo,
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                ]
            )
            return str(existing.get("url") or "")
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
                "--label",
                "audit:remediation",
                "--label",
                f"severity:{highest}",
            ]
        )
    finally:
        _unlink(body_file)

    lines = [ln for ln in (res.stdout or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else None


def close_stale_remediation_prs(
    repo: str,
    audit_id: str,
    prs: list[dict],
    current_ids: set[str],
    previous_titles: dict[str, str],
    resolved_findings: dict[str, dict],
    generated_at: datetime,
) -> list[str]:
    """Close every open remediation PR whose findings have all stopped reproducing.

    The pull request says which findings it was opened for, in the same hidden
    block the ledger uses, so this needs no stored state. The branch is never
    deleted: if the finding returns, the audit pushes to it again.
    """
    closed: list[str] = []
    for pr in prs:
        if str(pr.get("state", "")).upper() != "OPEN":
            continue
        covered = parse_delta_block(str(pr.get("body", "")))
        if not covered or any(fid in current_ids for fid in covered):
            continue

        findings = [
            resolved_findings.get(fid)
            or {"id": fid, "title": previous_titles.get(fid, ""), "evidence": {}}
            for fid in covered
        ]
        number = int(pr.get("number", 0))
        post_pr_comment(
            repo,
            number,
            render_stale_close_comment(audit_id, findings, generated_at),
            what="stale-close comment",
        )
        # Never --delete-branch: a returning finding pushes to this branch again.
        gh(["pr", "close", str(number), "-R", repo], check=False)
        closed.append(str(pr.get("url") or number))
    return closed


def comment_on_merged_but_persisting(
    repo: str,
    audit_id: str,
    findings: list[dict],
    pr_by_finding: dict[str, dict | None],
    generated_at: datetime,
) -> None:
    """Say once, on the merged pull request, that its finding still reproduces.

    Guarded by a marker in the pull request's own body-plus-comments rather
    than by mutating the trigger, and the pull request is never reopened: it
    merged, and reopening it would misrepresent history.
    """
    for finding in sort_findings(findings):
        fid = str(finding.get("id", ""))
        pr = pr_by_finding.get(fid)
        if not pr_is_merged(pr):
            continue
        number = int(pr.get("number", 0))
        already = has_marker(str(pr.get("body", "")), PERSISTS_MARKER_RE, fid) or any(
            has_marker(str(c.get("body", "")), PERSISTS_MARKER_RE, fid)
            for c in fetch_pr_comments(repo, number)
        )
        if already:
            continue
        post_pr_comment(
            repo,
            number,
            render_persists_comment(audit_id, finding, generated_at),
            what="merged-but-persists comment",
        )


def fetch_pr_comments(repo: str, number: int) -> list[dict]:
    res = gh(["pr", "view", str(number), "-R", repo, "--json", "comments"], check=False)
    if res.returncode != 0:
        log(f"WARNING: could not read comments on PR #{number}; treating as none.")
        return []
    try:
        comments = json.loads(res.stdout or "{}").get("comments") or []
    except json.JSONDecodeError:
        return []
    return [c for c in comments if isinstance(c, dict)]


def reply_to_refusals(
    repo: str,
    issue_number: int,
    refusals: list[dict],
    existing_comments: list[dict],
    generated_at: datetime,
) -> None:
    """Answer each refused `/remediate` exactly once.

    The guard is the requesting comment's node id, echoed in a hidden marker on
    the reply. A `/remediate` is never edited or hidden, so a repo writer can
    re-issue one after closing a pull request — which is precisely why "once"
    cannot be recorded on the command itself.
    """
    answered = "\n".join(str(c.get("body", "")) for c in existing_comments)
    for refusal in refusals:
        comment_id = str(refusal.get("comment_id", ""))
        if comment_id and has_marker(answered, REFUSED_MARKER_RE, comment_id):
            continue
        post_comment(
            repo,
            issue_number,
            render_refusal_comment(refusal, generated_at),
            what="/remediate refusal",
        )


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

    refresh_credentials()
    repo = resolve_repo()
    ensure_labels(repo, audit_id)

    # No branch is created or reset here. The report branch is gone: the ledger
    # is an issue, and each remediation pull request branches off main on demand.
    existing_issue, _ = find_existing_issue(repo, audit_id)

    pending: list[str] = []
    if existing_issue is not None:
        pending = pending_remediate_targets(fetch_issue_comments(repo, existing_issue))

    try:
        os.makedirs(SCRATCH_DIR, exist_ok=True)
    except OSError:
        pass

    # A crashed run must not leave a document behind for the next one to
    # publish as if it were fresh.
    findings_path = findings_path_for(audit_id)
    Path(findings_path).unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "issue": existing_issue,
                "repo": repo,
                "findings_path": findings_path,
                "pending_remediation_requests": pending,
            }
        )
    )


def _handle_finish_dry_run(audit_id: str, data: dict, now: datetime) -> None:
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
        log("STATUS: CLEAN — 0 findings; the open ledger (if any) would be closed.")
        print(render_clean_comment(audit_id, data, now))
        return

    states = {str(f.get("id", "")): STATE_OPEN for f in findings}
    promote, withheld = promotion_candidates(findings, {})
    groups = remediation_groups([f for f in findings if str(f.get("id", "")) in promote])

    log(f"TITLE: {issue_title(audit_id, findings)}")
    # "declared", not "on disk": the loop above warns about the ones missing.
    log(f"MANIFESTS DECLARED: {', '.join(paths) if paths else '(none)'}")
    log(
        "WOULD OPEN: "
        + (
            ", ".join(group_branch_for(audit_id, g) for g in groups)
            if groups
            else "(no remediation pull requests)"
        )
    )
    if withheld:
        log(f"WITHHELD BY THE CAP: {', '.join(withheld)}")
    print(
        render_issue_body(
            data,
            generated_at=now,
            audit_id=audit_id,
            states=states,
            withheld=withheld,
        )
    )


def _open_promoted_prs(
    repo: str,
    audit_id: str,
    findings: list[dict],
    promote: list[str],
    pr_by_finding: dict[str, dict | None],
    *,
    root: Path,
    issue_number: int | None,
    generated_at: datetime,
) -> list[str]:
    """Open (or refresh) one pull request per group holding a promoted finding.

    Groups are computed over the *whole* finding set, not just the promoted
    ids: if a critical finding shares its remediation file with a minor one,
    the file fixes both, and the pull request has to say so.

    The working tree is restored to the branch and file contents it started
    with, so a run that opens pull requests leaves the workspace exactly as a
    run that opens none.

    A group that fails to publish is logged and skipped rather than aborting
    the run. The ledger is already written by this point, and it records the
    finding as having no pull request — so the next run simply tries again.
    Failing the whole audit would throw away a correct report over a transient
    `gh` error, and lose the groups that came after the broken one.
    """
    if not promote:
        return []

    promoted = set(promote)
    groups = [
        group
        for group in remediation_groups(findings)
        if any(str(f.get("id", "")) in promoted for f in group)
    ]
    if not groups:
        return []

    started_on = current_branch()
    snapshot = snapshot_paths(root, manifest_paths(findings))
    opened: list[str] = []
    try:
        for group in groups:
            fid = str(sort_findings(group)[0].get("id", ""))
            try:
                url = open_remediation_pr(
                    repo,
                    audit_id,
                    group,
                    snapshot=snapshot,
                    root=root,
                    issue_number=issue_number,
                    existing=pr_by_finding.get(fid),
                    generated_at=generated_at,
                )
            except (subprocess.CalledProcessError, ValidationError) as exc:
                log(f"WARNING: could not publish the fix for {fid}: {exc}")
                continue
            if url:
                opened.append(url)
    finally:
        if started_on and started_on != "HEAD":
            git(["checkout", "--force", started_on], check=False)
        for path, blob in snapshot.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
    return opened


def handle_remediate(args: argparse.Namespace) -> None:
    """Open remediation pull requests for findings named on the command line.

    This is the uncapped path: `finish` promotes at most AUTO_PROMOTION_CAP
    findings a run on its own, but a human who asked for one by name gets it.
    """
    audit_id = validate_audit_id(args.audit)
    data = load_findings(args.findings_file, audit_id)
    findings = list(data["findings"])

    by_id = {str(f.get("id", "")): f for f in findings}
    unknown = [fid for fid in args.finding if fid not in by_id]
    if unknown:
        raise ValidationError(
            f"--finding: {', '.join(unknown)} not in {args.findings_file}; "
            f"known ids are {', '.join(sorted(by_id)) or '(none)'}"
        )
    not_manifest = [
        fid
        for fid in args.finding
        if (by_id[fid].get("remediation") or {}).get("kind") != "manifest"
    ]
    if not_manifest:
        raise ValidationError(
            f"--finding: {', '.join(not_manifest)} do not have a 'manifest' "
            "remediation; only a fix that is a file in this repository can become "
            "a pull request"
        )

    now = datetime.now(timezone.utc)
    if args.dry_run:
        promoted = set(args.finding)
        groups = [
            g
            for g in remediation_groups(findings)
            if any(str(f.get("id", "")) in promoted for f in g)
        ]
        log("DRY RUN: no branch is created, nothing is pushed, no PR is opened.")
        if args.issue is None:
            # The real run looks the ledger up; a dry run may not, because
            # that is a gh call. Say so rather than let the missing "Part of
            # #N" read as a defect in the rendering.
            log("DRY RUN: no --issue given, so the 'Part of #N' link is omitted.")
        for group in groups:
            log(f"WOULD OPEN: {group_branch_for(audit_id, group)}")
            print(
                render_remediation_pr_body(
                    audit_id, group, issue_number=args.issue, generated_at=now
                )
            )
        return

    refresh_credentials()
    repo = resolve_repo()
    ensure_labels(repo, audit_id)

    root = repo_root()
    assert_remediation_files_exist(findings, root)

    issue_number = args.issue
    if issue_number is None:
        issue_number, _ = find_existing_issue(repo, audit_id)

    pr_by_finding, _ = reconcile_remediation_prs(
        audit_id, findings, list_remediation_prs(repo, audit_id)
    )
    opened = _open_promoted_prs(
        repo,
        audit_id,
        findings,
        list(args.finding),
        pr_by_finding,
        root=root,
        issue_number=issue_number,
        generated_at=now,
    )
    print(json.dumps({"status": "REMEDIATED", "prs_opened": opened}))


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

    existing_issue, existing_url = find_existing_issue(repo, audit_id)
    previous_body = fetch_issue_body(repo, existing_issue) if existing_issue else ""
    # None means the body was unreadable, which is not the same as empty: the
    # delta is unknowable, so report no delta rather than a fabricated one.
    delta_known = previous_body is not None
    previous_ids = parse_delta_block(previous_body or "")
    previous_titles = parse_finding_titles(previous_body or "")
    current_ids = finding_ids(findings)
    new_ids, resolved_ids = compute_delta(previous_ids, current_ids)

    remediation_prs = list_remediation_prs(repo, audit_id)

    # --- Clean run: retire the stream's ledger and every fix it was waiting on. ---
    if not findings:
        prs_closed = close_stale_remediation_prs(
            repo, audit_id, remediation_prs, set(), previous_titles, {}, now
        )
        if existing_issue:
            post_comment(
                repo,
                existing_issue,
                render_clean_comment(audit_id, data, now),
                what="all-clear comment",
            )
            # Completed, not "not planned": a closed ledger means the fleet is
            # clean, never that the report was rejected.
            gh(
                [
                    "issue",
                    "close",
                    str(existing_issue),
                    "-R",
                    repo,
                    "--reason",
                    "completed",
                ]
            )
            log(f"Audit {audit_id} is clean; closed issue #{existing_issue}.")
        else:
            log(f"Audit {audit_id} is clean and has no open ledger; nothing to do.")
        print(
            json.dumps(
                {
                    "status": "CLEAN",
                    "issue_url": existing_url,
                    "new": 0,
                    "resolved": len(previous_ids),
                    "prs_opened": [],
                    "prs_closed": prs_closed,
                }
            )
        )
        return

    # --- Findings: publish the ledger, then propose fixes separately. ---
    root = repo_root()
    assert_remediation_files_exist(findings, root)

    # Every finding in the document reproduces by definition — the resolved ones
    # are the ids that are absent from it.
    pr_by_finding, pr_urls = reconcile_remediation_prs(
        audit_id, findings, remediation_prs
    )
    states = {
        str(f.get("id", "")): derive_finding_state(
            True, pr_by_finding.get(str(f.get("id", "")))
        )
        for f in findings
    }

    ledger_comments = fetch_issue_comments(repo, existing_issue) if existing_issue else []
    requested, refusals = parse_remediate_commands(ledger_comments, findings)
    promote, withheld = promotion_candidates(findings, pr_by_finding, requested)

    title = issue_title(audit_id, findings)
    body_file = _write_temp(
        render_issue_body(
            data,
            generated_at=now,
            audit_id=audit_id,
            states=states,
            pr_urls=pr_urls,
            withheld=withheld,
        )
    )
    try:
        if existing_issue is None:
            res = gh(
                [
                    "issue",
                    "create",
                    "-R",
                    repo,
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
            issue_url = lines[-1] if lines else None
            number = None
            if issue_url:
                tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
                number = int(tail) if tail.isdigit() else None
        else:
            gh(
                [
                    "issue",
                    "edit",
                    str(existing_issue),
                    "-R",
                    repo,
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                ]
            )
            status = "UPDATED"
            number = existing_issue
            issue_url = existing_url or fetch_issue_url(repo, existing_issue)
    finally:
        _unlink(body_file)

    if number is not None:
        apply_severity_label(repo, number, findings)
        reply_to_refusals(repo, number, refusals, ledger_comments, now)

    # A merged fix whose finding still reproduces is said once, on the pull
    # request, and the pull request is never reopened.
    comment_on_merged_but_persisting(repo, audit_id, findings, pr_by_finding, now)

    prs_closed = close_stale_remediation_prs(
        repo, audit_id, remediation_prs, set(current_ids), previous_titles, {}, now
    )

    prs_opened = _open_promoted_prs(
        repo,
        audit_id,
        findings,
        promote,
        pr_by_finding,
        root=root,
        issue_number=number,
        generated_at=now,
    )

    # The ledger was written before those pull requests existed, so it does not
    # yet link them. One extra edit is cheaper than making a reader wait a day.
    if prs_opened and number is not None:
        refreshed = list_remediation_prs(repo, audit_id)
        pr_by_finding, pr_urls = reconcile_remediation_prs(
            audit_id, findings, refreshed
        )
        states = {
            str(f.get("id", "")): derive_finding_state(
                True, pr_by_finding.get(str(f.get("id", "")))
            )
            for f in findings
        }
        relink = _write_temp(
            render_issue_body(
                data,
                generated_at=now,
                audit_id=audit_id,
                states=states,
                pr_urls=pr_urls,
                withheld=withheld,
            )
        )
        try:
            gh(
                ["issue", "edit", str(number), "-R", repo, "--body-file", relink],
                check=False,
            )
        finally:
            _unlink(relink)

    if status == "UPDATED" and number is not None:
        if not delta_known:
            log(
                "Previous ledger body was unreadable; skipping the delta comment "
                "rather than announcing every live finding as new."
            )
        else:
            comment = render_delta_comment(
                audit_id, new_ids, resolved_ids, findings, previous_titles, now
            )
            if comment:
                post_comment(repo, number, comment, what="delta comment")
            else:
                log("No new or resolved findings; body refreshed without a comment.")

    print(
        json.dumps(
            {
                "status": status,
                "issue_url": issue_url,
                "new": len(new_ids) if delta_known else 0,
                "resolved": len(resolved_ids) if delta_known else 0,
                "prs_opened": prs_opened,
                "prs_closed": prs_closed,
            }
        )
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic audit-reporting harness for the fleet-audit skill."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    start_parser = subparsers.add_parser(
        "start",
        help="Refresh credentials, locate the ledger issue, report pending "
        "/remediate requests.",
    )
    start_parser.add_argument(
        "--audit", required=True, help=f"Audit id: one of {', '.join(sorted(AUDITS))}."
    )

    finish_parser = subparsers.add_parser(
        "finish", help="Validate findings and publish/refresh/close the ledger issue."
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

    remediate_parser = subparsers.add_parser(
        "remediate",
        help="Open a remediation pull request for named findings (uncapped).",
    )
    remediate_parser.add_argument("--audit", required=True, help="Audit id.")
    remediate_parser.add_argument(
        "--findings-file", required=True, help="The findings.json the ids come from."
    )
    remediate_parser.add_argument(
        "--finding",
        required=True,
        action="append",
        metavar="ID",
        help="Finding id to remediate; repeat for more than one.",
    )
    remediate_parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Ledger issue to link with 'Part of #N'. Looked up when omitted.",
    )
    remediate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the pull-request bodies to stdout; zero git/gh side effects.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "start":
            handle_start(args)
        elif args.subcommand == "remediate":
            handle_remediate(args)
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
