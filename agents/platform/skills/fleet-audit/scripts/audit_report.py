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
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import NamedTuple

# The shared scripts dir holds github_token_refresh (see docker-entrypoint.sh:
# executable scripts are shared across profiles, not copied per-profile). The
# import itself is lazy so `--dry-run` works on a dev machine with no sandbox.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# The audit streams allowed to own a ledger. An id not listed here is rejected
# before any git/gh call: a typo must not silently open a sixth ledger stream.
# The human names mirror the `name` of the matching watchdog in
# agents/platform/cron/jobs.json — keep the two in step so the issue title and
# the cron catalogue name the same thing.
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

# Both directories must live on the PVC. `gh` and `git` are not binaries in the
# agent container: /opt/credential-proxy/bin/{gh,git} POST argv and cwd to a
# sidecar that runs the real tool in *its* filesystem. Only /opt/data is shared
# between the two containers — /tmp is a per-container emptyDir — so a body file
# written to the default temp dir names a path the sidecar cannot open, and a
# checkout outside the workspace root is rejected by the sidecar outright.
#
# Overridable so the suite can point them at a temp directory. Off-cluster
# /opt/data does not exist and is not creatable, and a harness that can only be
# exercised where it is deployed is a harness whose failure paths are never
# tested — which is how the clone that never happened survived this long.
SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"
GITOPS_WORKSPACE = os.environ.get("FLEET_AUDIT_GITOPS_ROOT") or "/opt/data/gitops"

# Applied to a pull request the harness itself closed as stale. It is the
# discriminator that keeps a *human's* close final while letting the audit
# re-propose a fix it withdrew on its own: strip the label and the close becomes
# a veto. Requested in the `gh pr list` projection, so it costs no extra call.
STALE_CLOSED_LABEL = "audit:stale-closed"

# Wildcard stagers that must never reach `git add` — an audit stages named
# remediation files only, never the whole working tree.
FORBIDDEN_ADD_PATHSPECS = {".", "-A", "--all", "-a", "*", ":/", "./", ":"}

# Glob metacharacters git expands in a pathspec. `git --literal-pathspecs` is
# the real guard (see build_git_add_command); rejecting these at validation time
# means the refusal names the offending finding instead of silently staging the
# wrong files.
GLOB_METACHARACTERS = "*?[]"

# The id is the join key of the hidden delta block and of the
# `audit-persists:<id>` marker, both matched by line-anchored regexes that a
# stray newline would silently break — and a silent break there reports every
# finding as new. It is also typed by a human in `/remediate <id>`, which rules
# out case variation and shell metacharacters. `\Z` rather than `$` on purpose:
# Python's `$` also matches immediately before a trailing newline, so `"abc\n"`
# would pass. The `git check-ref-format` shape — no ':', no whitespace, no '..'
# run, no '.lock' suffix — is kept as a superset even though the path digest
# took the id back out of the branch name; it costs nothing and the gate is
# already in place the day an id returns to a ref.
#
# The five SOPs and SKILL.md quote a *normalised* form of this pattern —
# capturing group, `$` for `\Z` — because neither difference means anything to
# a model reading prose. `hack/check-docs-terminology.sh` derives that form
# from this constant and fails the build if any of the seven copies drifts, so
# edit here and let the gate tell you which document to follow.
#
# The optional tail makes a one-character id legal. Nothing about a single
# letter is unsafe, and the SOP fixtures use them.
FINDING_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?\Z")

# The hidden block that makes the run-over-run delta computable without keeping
# any state outside the report itself.
#
# Every character class here is single-line (`[ \t]`, `[^\n]`) and the flags are
# `re.M` alone. An earlier version combined `re.M` with `re.S`, which let the
# lazy `.*?` cross newlines: an unterminated `<!-- audit-findings: [` pasted
# from a cluster excerpt — exactly the text the SOPs mandate pasting verbatim —
# started a match that ran past the real block at the bottom of the body and
# consumed it, so every finding read as new forever and no stale pull request
# was ever closed.
DELTA_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-findings:[ \t]*(\[[^\n]*?\])[ \t]*-->[ \t]*$", re.M
)
# Per-finding marker on each heading, so a *resolved* finding can still be named
# by title when it no longer exists in the current findings.json.
FINDING_MARKER_RE = re.compile(
    r"^####[ \t]+(.*?)[ \t]*<!--[ \t]*finding:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.M
)

# Idempotency markers. Design §3.1 deliberately never mutates a `/remediate`
# comment — a repo writer must be able to re-issue one after closing a PR — so
# "act exactly once" is carried instead by hidden markers in the bodies this
# harness already owns, the same technique the delta block uses.
PERSISTS_MARKER_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-persists:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.M
)
REFUSED_MARKER_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-refused:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.M
)
# Answered exactly once, which is what stops a `/remediate` becoming a standing
# order that force-pushes over a reviewer's fixup commits every morning.
ACKED_MARKER_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-acked:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.M
)
# Written into the closing comment of a pull request the *harness* closed, so
# the audit trail says who closed it. The machine-readable half of the same
# fact is the `audit:stale-closed` label — see STALE_CLOSED_LABEL.
STALE_CLOSED_MARKER_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-stale-closed:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.M
)

# `/remediate <finding-id>` / `/remediate all`, at the start of a line. The
# argument is captured loosely — anything up to end of line — so that a
# malformed request like `/remediate f-1 please` is *answered* with a refusal
# rather than silently matching nothing and leaving the requester waiting.
REMEDIATE_RE = re.compile(r"^[ \t]*/remediate\b[ \t]*(.*?)[ \t]*$", re.M)
# The same word *anywhere*, used only to notice a request the line-anchored
# regex above will not honour — `we should /remediate f-1` buried in a
# sentence. Matching it is not accepting it; it exists so the answer can be
# "not like that" rather than nothing at all.
REMEDIATE_MENTION_RE = re.compile(r"/remediate\b")
# An inline code span, removed before the mention search so that prose *about*
# the command — `see `/remediate <id>` above` — is not mistaken for an attempt
# to use it. Every `/remediate` this harness itself writes into a comment is
# backticked for exactly this reason: the ledger's own replies are read back on
# the next run, and a bot that answers itself never stops.
#
# One line, shortest run-delimited span, which is CommonMark's rule for
# everything but a span that wraps a newline. Those are vanishingly rare in an
# issue comment and erring towards *not* stripping only risks one extra reply.
INLINE_CODE_RE = re.compile(r"(`+)[^\n]*?\1")
# How many finding ids a refusal lists back before it gives up and says "and N
# more". A refusal is help, not a second copy of the report.
MAX_HINT_IDS = 10

# `finish --dry-run` writes the ledger body and then every pull request body to
# the same stdout, so the boundary has to be machine-findable: splitting on this
# line is how a size check measures each body against GitHub's limit separately
# rather than measuring the concatenation and reporting a failure that is not
# real. Deliberately not valid Markdown — nothing a renderer would produce.
DRY_RUN_PR_SEPARATOR = "=== WOULD OPEN PULL REQUEST ==="
# A fence opener. Fenced code blocks are stripped before command matching, so a
# `/remediate` quoted inside an evidence excerpt never fires; strip_fenced_blocks
# scans line by line rather than with one regex, because the regex form missed
# both an unterminated fence and a block closed by a longer run of backticks.
# CommonMark, and the indentation is load-bearing rather than cosmetic. A fence
# opener may be indented up to three spaces; at four it is an indented code
# block and not a fence at all, and a closer follows the same rule. Matching a
# stripped line instead treats `    ``` ` — which every Markdown renderer shows
# as literal text *inside* the surrounding block — as a real delimiter, which
# ends the block early and leaves the lines after it exposed. That is how a
# `/remediate` a reader quoted inside a code block gets read as a command.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

MAX_EXCERPT_LINES = 40
MAX_EXCERPT_CHARS = 2000
# The SOPs mandate pasting the evidence command verbatim, which makes it the
# dominant per-finding term; trim_excerpt guards the wrong field on its own.
MAX_COMMAND_CHARS = 2000
# The free-text schema fields. Without these a schema-valid document can render
# one finding larger than the whole body budget, and since at least one finding
# always renders that overflows the body and publishes nothing at all.
MAX_TITLE_CHARS = 300
MAX_TEXT_CHARS = 1500
MAX_NOTE_CHARS = 2000
MAX_CELL_CHARS = 120
# `cluster`, `namespace` and `object` are Kubernetes identifiers, and the API
# bounds every one of them: a namespace is a 63-character DNS label, a resource
# name a 253-character DNS subdomain, a GKE cluster name 40. 320 clears
# `Kind/name` at its longest with room to spare and still caps a hostile value.
# They are the last free-text fields the renderer interpolated raw, and the
# selection loop always renders the first finding whatever it costs — so one
# oversized identifier on one finding overflowed the body and published
# nothing at all, every morning, for as long as the finding reproduced.
MAX_IDENT_CHARS = 320

# GitHub rejects an issue or pull-request body over 65,536 characters with a
# 422. Issue bodies carry the identical limit, so this budget is the difference
# between a stream that publishes and one that 422s every morning forever.
MAX_BODY_CHARS = 65_536
BODY_BUDGET = 60_000
MAX_SCOPE_ROWS = 60
MAX_DELTA_ROWS = 50

# `gh pr list` takes a limit, not a cursor. A full page means the oldest
# remediation branches fell off the end, and a branch that reads as "no pull
# request exists" is one the harness will force-push over. Detect it and stop.
MAX_PR_PAGE = 1000

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
# Pure helpers — text hygiene
# --------------------------------------------------------------------------- #


def normalise_newlines(text: str | None) -> str:
    """Fold CRLF and lone CR to LF before any line-anchored regex sees the text.

    Every marker pattern in this file ends `[ \\t]*$`, and `\\r` is neither a
    space nor a tab. GitHub's web comment box submits CRLF, so without this a
    `/remediate` typed in a browser is ignored, a body a human edited loses its
    delta block, and both comment-once guards fail open and comment again. This
    is the most reachable defect class in the harness: it fires on ordinary
    browser use, not on hostile input.
    """
    if not text:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


REDACTED = "[redacted by audit_report.py]"

# Field names whose value is a credential often enough that publishing it to a
# GitHub issue is never worth the convenience. Matched as a YAML/JSON key with a
# value on the same line, so `kubectl get secret -o jsonpath='{.data.token}'`
# in an evidence *command* is untouched — there is no value after the colon.
_SECRET_KEY_RE = re.compile(
    r"""(?ix)
    ^(?P<lead>[\s"'\-]*"?)
    (?P<key>password|passwd|token|secret|api[_-]?key|access[_-]?key
        |auth|authorization|credentials?
        |private[_-]?key|privatekey|clientkey|clientcertificate
        |client-key-data|client-certificate-data|cluster-?ca-?certificate
        |access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?key)
    (?P<sep>"?\s*[:=]\s*)
    (?P<value>\S.*?)
    (?P<trail>\s*,?)$
    """,
    re.M,
)

# Token shapes that identify themselves. Redacted anywhere they appear, because
# a bearer token in the middle of a log line is still a bearer token.
_TOKEN_SHAPE_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|ya29\.[A-Za-z0-9._\-]{20,}"
    r"|AIza[A-Za-z0-9_\-]{30,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)

# The body between a PEM header and its footer, header and footer preserved so
# the reader can still see *what* was redacted.
_PEM_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*-----)(.*?)(-----END [A-Z0-9 ]*-----)", re.S
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{12,})")

# The opener of a Kubernetes Secret payload. Everything indented under it is a
# credential by definition, whatever the individual keys are called.
_SECRET_BLOCK_RE = re.compile(r"^(\s*)(data|stringData)\s*:\s*$")
_INDENTED_PAIR_RE = re.compile(r"^(\s*)([\w.\-/]+)\s*:\s*(\S.*)$")


def _redact_secret_blocks(text: str) -> str:
    """Blank every value indented under a `data:` / `stringData:` key.

    A Secret's payload is credential material regardless of what the individual
    keys are named, so the key-name heuristic below cannot see it. Indentation
    is the only structure available in an excerpt, which is why this is a line
    scan rather than a YAML parse — the excerpt is a fragment, not a document.
    """
    out: list[str] = []
    block_indent: int | None = None
    for line in text.split("\n"):
        opener = _SECRET_BLOCK_RE.match(line)
        if opener:
            block_indent = len(opener.group(1))
            out.append(line)
            continue
        if block_indent is not None:
            pair = _INDENTED_PAIR_RE.match(line)
            if pair and len(pair.group(1)) > block_indent:
                out.append(f"{pair.group(1)}{pair.group(2)}: {REDACTED}")
                continue
            if line.strip() and (len(line) - len(line.lstrip())) <= block_indent:
                block_indent = None
        out.append(line)
    return "\n".join(out)


def redact_secrets(text: str | None) -> str:
    """Strip high-confidence credential shapes out of model-authored text.

    The five governance SOPs tell the model never to paste a Secret's `data:`,
    a token, or a private key into evidence, and promise this backstop for when
    it does anyway. It is deliberately conservative: it fires on a *named* field,
    a self-identifying token prefix, or a PEM header — never on bare base64,
    because audit evidence legitimately contains base64 and long opaque
    identifiers, and over-redaction destroys the artifact's whole purpose.

    A backstop, not a licence. Anything that reaches here has already been
    written into a file on disk.
    """
    if not text:
        return ""
    out = _PEM_RE.sub(rf"\1\n{REDACTED}\n\3", str(text))
    out = _redact_secret_blocks(out)
    out = _SECRET_KEY_RE.sub(
        lambda m: f"{m.group('lead')}{m.group('key')}{m.group('sep')}{REDACTED}{m.group('trail')}",
        out,
    )
    out = _BEARER_RE.sub(rf"\1 {REDACTED}", out)
    return _TOKEN_SHAPE_RE.sub(REDACTED, out)


def clip_text(text: str | None, limit: int) -> str:
    """Redact, then clip a free-text schema field to `limit` characters.

    Every free-text field is capped, not only the evidence: `title`, `impact`,
    the three `recommendation` sub-fields and `remediation.note` were uncapped,
    and since `select_rendered_findings` guarantees at least one finding always
    renders, a single oversized field could push the body past GitHub's limit
    and publish nothing at all.
    """
    value = redact_secrets(text).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + " …(truncated)"


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
    """A remediation path is staged with `git add` — keep it inside the repo.

    Returns the **normalised** path. Normalising here rather than at each call
    site is what makes `remediation_groups` correct: `a/b.yaml` and `./a/b.yaml`
    name one file, and a grouper that compares raw strings puts them in two
    groups, opens two pull requests against the same file, and the second one
    conflicts. Every downstream consumer — grouping, the branch digest, the
    `git add` pathspec, the existence check — sees the same spelling.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValidationError(f"{where}: required, must be a non-empty path")
    if "\x00" in path:
        raise ValidationError(f"{where}: must not contain a NUL byte")
    if "\\" in path or path.startswith("/") or PurePosixPath(path).is_absolute():
        raise ValidationError(
            f"{where}: must be a POSIX path relative to the repository root, got {path!r}"
        )
    if path.startswith(":"):
        raise ValidationError(
            f"{where}: must not begin with ':' — git reads a leading colon as a "
            f"pathspec magic prefix, not a filename; got {path!r}"
        )
    found = [char for char in GLOB_METACHARACTERS if char in path]
    if found:
        raise ValidationError(
            f"{where}: must name one literal file, not a glob "
            f"(contains {', '.join(repr(c) for c in found)}), got {path!r}"
        )

    # Drop the no-op segments git itself would drop, then judge what remains.
    # Checking `..` before this would pass `a/./../../etc`, whose *parts* start
    # with a legitimate-looking `a`.
    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if ".." in parts:
        raise ValidationError(
            f"{where}: must not escape the repository root ('..' segment), got {path!r}"
        )
    if not parts:
        raise ValidationError(
            f"{where}: must name a file inside the repository, got {path!r}"
        )
    # Every part, case-folded — not just the first, and not case-sensitively.
    # `sub/.git/config` is a submodule's repository state and writing there
    # rewrites where that submodule points; `.GIT/config` is the same file as
    # `.git/config` on the case-insensitive filesystems this runs on
    # (macOS by default, and any repo checked out on one), so a case-sensitive
    # test is a guard the attacker picks the spelling around.
    for part in parts:
        if part.casefold() == ".git":
            raise ValidationError(
                f"{where}: must not write inside '.git' — that is a repository's "
                f"own state, not a manifest; got {path!r}"
            )
    if path.endswith("/"):
        raise ValidationError(
            f"{where}: must name one file, not a directory, got {path!r}"
        )
    return "/".join(parts)


def validate_finding_id(fid: str, where: str) -> str:
    """A finding id is a join key and an operator types it, so its charset is narrow.

    The remediation branch is
    `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the slug is derived
    from a manifest path — so an id no longer reaches the ref name directly.
    The constraint stays anyway, for two reasons that outlive the naming
    scheme: the id is the join key of the hidden delta block and of the
    `audit-persists:<id>` marker, both of which are line-anchored regexes that
    a whitespace-bearing or case-varying id would quietly break; and it is
    interpolated into `/remediate <id>`, which an operator types by hand.
    """
    if not FINDING_ID_RE.match(fid):
        raise ValidationError(
            f"{where}: {fid!r} is not a usable id. Use 1-100 characters matching "
            "[a-z0-9._-], starting and ending alphanumeric — the id is the join key "
            "of the delta block and the audit-persists marker, both line-anchored, "
            "and an operator types it in '/remediate <id>', so ':', whitespace and "
            "uppercase are refused"
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
            # Write the normalised spelling back. Grouping, the branch digest
            # and the `git add` pathspec all key on this string; if `a/b.yaml`
            # and `./a/b.yaml` survive as two spellings they become two groups,
            # two pull requests against one file, and a conflict on the second.
            remediation["path"] = _require_repo_relative(
                path, f"findings[{i}].remediation.path"
            )
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


def coverage_gaps(data: dict) -> list[str]:
    """Why this run cannot speak for the whole fleet, if it cannot.

    Two different gaps, one consequence. A cluster in `scope.skipped` was never
    read; a cluster carrying `limitations` was read but not fully checked.
    Either way a finding's *absence* proves nothing, so the run must not treat
    "absent from this document" as "fixed" — not in the delta it announces, not
    in the remediation pull requests it closes, and not by retiring the ledger
    and declaring the stream clean.
    """
    scope = data.get("scope") or {}
    gaps: list[str] = []
    for entry in scope.get("skipped") or []:
        cluster = str(entry.get("cluster", "")).strip() or "(unnamed)"
        gaps.append(f"{cluster}: not audited — {entry.get('reason', 'no reason given')}")
    for cluster in scope.get("clusters") or []:
        limitation = str(cluster.get("limitations", "")).strip()
        if limitation:
            name = str(cluster.get("name", "")).strip() or "(unnamed)"
            gaps.append(f"{name}: partially audited — {limitation}")
    return gaps


class ContainmentError(ValidationError):
    """A remediation path that passed the string check still escapes the repo."""


def resolve_inside_repo(root: Path, path: str, where: str) -> Path:
    """The absolute path of a remediation file, proven to be inside `root`.

    `_require_repo_relative` is a *string* check and cannot be more than that:
    it runs during validation, before the harness knows where the checkout is.
    Every string it accepts still becomes a real path, and on a real filesystem
    a relative path with no `..` in it escapes anyway the moment a directory
    component is a symlink. `manifests/vendor/x.yaml` is beyond reproach until
    `manifests/vendor` is a link to `/etc`, at which point the existence check
    passes, the snapshot reads `/etc/x.yaml`, and the contents are committed to
    a public pull request. The audit is supposed to be read-only against the
    fleet and narrow against the repo; this is the check that makes the second
    half true.

    Two independent tests, because either alone has a hole:

    * no component may be a symlink — catches an escape whose target happens to
      resolve back inside the repo today and stops being contained tomorrow,
      and stops the harness *writing through* a link;
    * the fully resolved path must sit under the fully resolved root — catches
      everything the walk misses, including a root that is itself reached
      through a link.

    Raises ContainmentError. Never returns a path it has not proven.
    """
    relative = _require_repo_relative(path, where)
    resolved_root = Path(root).resolve()

    probe = resolved_root
    for part in PurePosixPath(relative).parts:
        probe = probe / part
        if probe.is_symlink():
            raise ContainmentError(
                f"{where}: {part!r} in {path!r} is a symbolic link. A remediation "
                "may only touch real files inside the repository — following a "
                "link would read, or write, outside it."
            )

    resolved = (resolved_root / relative).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ContainmentError(
            f"{where}: {path!r} resolves to {resolved}, which is outside the "
            f"repository at {resolved_root}"
        )
    return resolved


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
    """Read the finding ids out of a previous issue body ([] when absent/unparseable)."""
    body = normalise_newlines(body)
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
    """Recover {finding id: title} from a previous issue body, to name resolved findings."""
    body = normalise_newlines(body)
    if not body:
        return {}
    return {fid: title.strip() for title, fid in FINDING_MARKER_RE.findall(body)}


def compute_delta(
    previous_ids: list[str],
    rendered_ids: list[str],
    all_current_ids: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (newly appeared ids, newly resolved ids), both sorted.

    The two halves are deliberately measured against *different* sets, because
    "appeared" and "was fixed" are different claims and the body budget breaks
    them apart.

    `previous_ids` comes out of the last run's hidden block, which records what
    that body **rendered** — so `new` has to be measured against what this body
    rendered too. Compare a rendered set to a full finding set and every
    finding the budget dropped is announced as new, every run, forever.

    `all_current_ids` is every finding in the document, rendered or not, and
    resolution is judged against it alone. A finding cut for space still
    reproduces; calling it resolved claims a fix that never happened, in
    writing, on a finding nobody can see. It defaults to `rendered_ids` for
    callers where nothing was truncated.
    """
    previous = set(previous_ids)
    rendered = set(rendered_ids)
    current = set(rendered_ids if all_current_ids is None else all_current_ids)
    return sorted(rendered - previous), sorted(previous - current)


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


def _branch_slug(text: str) -> str:
    """A short, legible, ref-safe fragment — decoration, never the join key."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:24].strip("-")


def group_branch_for(audit_id: str, group: list[dict]) -> str:
    """Name a remediation branch after the *files* the group stages.

    The branch name is the only durable link between a finding and its pull
    request — one `gh pr list --json headRefName` reconstructs the whole mapping
    with no state kept anywhere else. That makes its stability load-bearing.

    Keying it on the lowest finding id looked reasonable and was not: finding
    ids are regenerated from scratch every run, so the day a group's lowest id
    resolves, the survivors rename their branch, the open pull request is
    orphaned, and a duplicate opens against the same file. The path set is the
    thing that actually identifies the work — it is what makes the group a
    group — and it is stable across id churn. A leading slug from the first
    path keeps the name readable in the GitHub UI; the digest is what joins.
    """
    paths = group_paths(group)
    if not paths:
        raise ValueError("cannot name a remediation branch for an empty group")
    digest = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()[:10]
    slug = _branch_slug(PurePosixPath(paths[0]).stem)
    suffix = f"{slug}-{digest}" if slug else digest
    return f"platform-agent/fix-{audit_id}-{suffix}"


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
    """Drop fenced code blocks so a `/remediate` quoted in evidence never fires.

    A non-greedy ```…``` regex is the obvious implementation and it is wrong in
    the direction that matters. Given three fences it pairs the first with the
    second and leaves the third dangling, so text between fence 2 and fence 3 —
    text that is *inside* a code block to every Markdown renderer, and to the
    human who wrote it — survives stripping and its `/remediate` fires. Quoting
    a command to discuss it is the single most likely thing to be written in
    one of these issues.

    So: CommonMark's actual rule. A fence opens on a run of three or more
    backticks or tildes, indented at most three spaces; it closes on a run of
    the same character, at least as long, indented at most three spaces, with
    nothing else on the line. An unterminated fence runs to the end.

    The indentation bound is the half that is easy to drop and expensive to
    lose. Strip each line first and `    ``` ` — four spaces, which CommonMark
    and GitHub both render as literal text inside the enclosing block — reads
    as a closer, the block ends four lines early, and the `/remediate` the
    author put inside it to talk *about* fires as a command.
    """
    if not text:
        return ""
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        if fence_char:
            closer = line.rstrip()
            if (
                len(closer) - len(closer.lstrip(" ")) <= 3
                and set(closer.lstrip(" ")) == {fence_char}
                and len(closer.lstrip(" ")) >= fence_len
            ):
                fence_char = ""
                fence_len = 0
            continue
        match = FENCE_OPEN_RE.match(line)
        if match:
            fence_char = match.group(1)[0]
            fence_len = len(match.group(1))
            continue
        out.append(line)
    return "\n".join(out)


def parse_gh_timestamp(value: str | None) -> datetime | None:
    """A `gh --json` RFC 3339 timestamp as an aware `datetime`, or None.

    `gh` emits `2026-07-30T09:14:22Z`; `fromisoformat` did not accept the `Z`
    suffix before 3.11 and the container's interpreter is not guaranteed to be
    newer, so the suffix is normalised by hand. Anything unparseable returns
    None and every caller must treat that as "unknown", never as "old" — a
    missing timestamp is a `gh` schema change, not evidence about the past.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def newer_timestamp(current: str | None, candidate: str | None) -> bool:
    """True when `candidate` is a parseable timestamp later than `current`.

    For *accumulating* a newest-wins value: an unparseable `current` is
    replaced by anything parseable, an unparseable `candidate` never wins.
    Not a substitute for `timestamp_strictly_after` — this one deliberately
    treats "unknown" as "infinitely old", which is the wrong default for a
    decision about overruling somebody.
    """
    parsed_candidate = parse_gh_timestamp(candidate)
    if parsed_candidate is None:
        return False
    parsed_current = parse_gh_timestamp(current)
    return parsed_current is None or parsed_candidate > parsed_current


def timestamp_strictly_after(candidate: str | None, reference: str | None) -> bool:
    """True only when both timestamps parse and `candidate` is strictly later.

    Unknown on either side is False, and the asymmetry with `newer_timestamp`
    is the point. This decides whether a request may overrule a human's close,
    so a missing `closedAt` — a `gh` schema change, not evidence the close
    never happened — must not read as "nothing to overrule". Equal instants
    lose too: they cannot distinguish cause from effect, and the cheaper
    mistake is the one a second `/remediate` fixes.
    """
    parsed_candidate = parse_gh_timestamp(candidate)
    parsed_reference = parse_gh_timestamp(reference)
    if parsed_candidate is None or parsed_reference is None:
        return False
    return parsed_candidate > parsed_reference


class RemediateRequests(NamedTuple):
    """Everything the ledger's comments asked for, who asked, and when.

    `requested_at` carries the newest accepting comment's timestamp per finding
    id. Without it a request has no age, and a `/remediate` written in March is
    indistinguishable from one written this morning — which matters the moment
    somebody closes the resulting pull request, because the ledger's comments
    are never edited or deleted and the old command would otherwise re-open it
    every single run.
    """

    targets: list[str]
    refusals: list[dict]
    accepted_by_comment: dict[str, list[str]]
    requested_at: dict[str, str] = {}


def strip_inline_code(text: str) -> str:
    """Drop inline code spans, so quoting the command is not using it."""
    return INLINE_CODE_RE.sub(" ", text or "")


def _promotable_hint(promotable: set[str]) -> str:
    """The tail of a refusal: which ids the requester could have named.

    A refusal that only says "wrong" makes the requester read the whole ledger
    again to find the right spelling, and the ledger is the document that was
    already too long to read. Naming the ids turns two round trips on a daily
    cron — two days — into one.
    """
    if not promotable:
        return (
            ". Nothing in this report has a manifest fix, so there is nothing to "
            "promote right now."
        )
    ids = sorted(promotable)
    shown = ", ".join(f"`{fid}`" for fid in ids[:MAX_HINT_IDS])
    if len(ids) > MAX_HINT_IDS:
        return f". Promotable ids here: {shown}, and {len(ids) - MAX_HINT_IDS} more."
    return f". Promotable ids here: {shown}."


def parse_remediate_commands(
    comments: list[dict], findings: list[dict]
) -> RemediateRequests:
    """Read `/remediate` requests off the ledger issue.

    A refusal is one entry per comment, not per bad target, because the reply is
    posted once per comment and marked with that comment's node id.

    `accepted_by_comment` exists so a request that *worked* gets an answer too.
    A command that silently succeeds is indistinguishable from one that was
    never read — the requester waits, sees nothing, and comments again.

    The same reasoning covers the two ways of getting the syntax wrong. A
    `/remediate` mid-sentence and a `/remediate` with nothing after it are both
    somebody asking for a fix, and both used to produce exactly the observable
    behaviour of an audit that had not run yet. Neither is honoured — the
    line-anchored form is what keeps a quoted command from firing, and guessing
    a target from an empty one is how the wrong pull request gets opened — but
    both are now answered, once, with the syntax and the ids that would work.
    """
    by_id = {str(f.get("id", "")): f for f in findings}
    promotable = {
        fid
        for fid, finding in by_id.items()
        if (finding.get("remediation") or {}).get("kind") == "manifest"
    }

    targets: set[str] = set()
    refusals: list[dict] = []
    accepted_by_comment: dict[str, list[str]] = {}
    requested_at: dict[str, str] = {}

    for comment in comments or []:
        body = strip_fenced_blocks(normalise_newlines(comment.get("body", "")))
        matches = REMEDIATE_RE.findall(body)
        # Nothing at the start of a line, but the word is in there somewhere and
        # not inside a code span: an attempt at the command, not a discussion of
        # it. Worth a reply; never worth acting on.
        mention_only = not matches and bool(
            REMEDIATE_MENTION_RE.search(strip_inline_code(body))
        )
        if not matches and not mention_only:
            continue

        node_id = str(comment.get("id", "") or "")
        created_at = str(comment.get("createdAt", "") or "")
        author = str((comment.get("author") or {}).get("login", "") or "") or "someone"
        association = str(comment.get("authorAssociation", "") or "").upper()
        reasons: list[str] = []

        if association not in WRITE_ASSOCIATIONS:
            if mention_only:
                # Prose, from somebody whose correctly-typed command would have
                # been refused anyway. Two refusals for one comment that was
                # probably never a command is a bot picking an argument.
                continue
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

        if mention_only:
            refusals.append(
                {
                    "comment_id": node_id,
                    "author": author,
                    "reasons": [
                        "`/remediate` is only read at the start of its own line, and "
                        "that comment has it mid-sentence — the same rule is what "
                        "stops a command quoted in a discussion from firing. Post it "
                        "on a line of its own: `/remediate <finding-id>`, or "
                        "`/remediate all`" + _promotable_hint(promotable)
                    ],
                }
            )
            continue

        accepted: list[str] = []
        for raw in matches:
            target = raw.strip().strip("`")
            if not target:
                # An empty target is not a wildcard. Reading it as one would
                # open every promotable pull request the cap allows on somebody
                # who typed the command and then went to look up the id.
                reasons.append(
                    "`/remediate` on its own does not say what to fix. Name a "
                    "finding — `/remediate <finding-id>` — or ask for every "
                    "promotable one at once with `/remediate all`"
                    + _promotable_hint(promotable)
                )
                continue
            if target == "all":
                targets |= promotable
                accepted.extend(sorted(promotable))
                continue
            if target not in by_id:
                # The hint belongs on precisely this reason. A typo'd id is the
                # one refusal where the requester's next move is to find the
                # right spelling, and the document they would search is the
                # ledger that was already too long to read.
                reasons.append(
                    f"`{target}` is not a finding in the current report — it may have "
                    "been resolved, or the id may be a typo"
                    + _promotable_hint(promotable)
                )
                continue
            if target not in promotable:
                kind = (by_id[target].get("remediation") or {}).get("kind")
                reasons.append(
                    f"`{target}` has a `{kind}` remediation, not a `manifest` one. "
                    "Only a finding whose fix is a file in this repository can become "
                    "a pull request; run the command in the report instead"
                    + _promotable_hint(promotable)
                )
                continue
            targets.add(target)
            accepted.append(target)

        if accepted:
            accepted_by_comment.setdefault(node_id, [])
            for target in accepted:
                if target not in accepted_by_comment[node_id]:
                    accepted_by_comment[node_id].append(target)
                # Newest wins. Ask twice and it is the second ask that has to
                # clear a close, because the first one already did its work.
                if newer_timestamp(requested_at.get(target), created_at):
                    requested_at[target] = created_at
        if reasons:
            refusals.append(
                {"comment_id": node_id, "author": author, "reasons": reasons}
            )

    return RemediateRequests(
        sorted(targets), refusals, accepted_by_comment, requested_at
    )


def unanswered_remediate_comments(comments: list[dict]) -> list[dict]:
    """`/remediate` comments still owed an answer, for a run with no findings.

    The clean branch of `finish` returns long before `parse_remediate_commands`
    runs, so a command standing on the ledger the morning the fleet came back
    clean used to get nothing at all — and then the ledger closed underneath it.
    From the requester's chair that is indistinguishable from the audit never
    having read the comment, which is the exact failure the acknowledgement
    machinery exists to prevent; worse, the issue they would have re-asked on is
    gone.

    Authorization is deliberately not consulted here. It decides whether a
    command is *acted on*, and on a clean run nothing is acted on for anybody —
    so "that finding no longer reproduces" is both the true answer and the more
    useful one, for a writer and a non-writer alike. Mention-only comments are
    included for the same reason: there is no pull request to open by mistake,
    so the only cost of answering is a comment, and the cost of not answering is
    a person waiting on a closed issue.

    The guard is the same pair of hidden markers the findings path uses, so a
    ledger that stays open over a coverage gap does not re-answer every morning.
    """
    answered = "\n".join(str(c.get("body", "")) for c in comments or [])
    out: list[dict] = []
    for comment in comments or []:
        body = strip_fenced_blocks(normalise_newlines(comment.get("body", "")))
        targets = [raw.strip().strip("`") for raw in REMEDIATE_RE.findall(body)]
        if not targets and not REMEDIATE_MENTION_RE.search(strip_inline_code(body)):
            continue
        node_id = str(comment.get("id", "") or "")
        if node_id and (
            has_marker(answered, ACKED_MARKER_RE, node_id)
            or has_marker(answered, REFUSED_MARKER_RE, node_id)
        ):
            continue
        out.append(
            {
                "comment_id": node_id,
                "author": str((comment.get("author") or {}).get("login", "") or "")
                or "someone",
                "targets": sorted({t for t in targets if t}),
            }
        )
    return out


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
        body = strip_fenced_blocks(normalise_newlines(comment.get("body", "")))
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
STATE_WITHDRAWN = "withdrawn"

STATE_LABELS = {
    STATE_OPEN: "open",
    STATE_PR_OPEN: "fix proposed",
    STATE_PR_MERGED_PERSISTS: "⚠ fix merged, still reproduces",
    STATE_RESOLVED_MERGED: "resolved (fix merged)",
    STATE_RESOLVED: "resolved",
    STATE_REFUSED: "fix refused",
    STATE_WITHDRAWN: "fix withdrawn, awaiting re-proposal",
}


def derive_finding_state(reproduces: bool, pr: dict | None) -> str:
    """The §4 state of one finding, from whether it reproduces and its PR.

    `pr` is the remediation pull request found on the finding's branch, or None.
    A merged PR whose finding still reproduces is the case the old rolling-PR
    model could not express at all.

    A closed pull request is two different events and they must not share a row.
    One the harness closed — labelled `audit:stale-closed`, because the finding
    had stopped reproducing — is a withdrawal, and the finding coming back means
    a fresh pull request is due. One a *human* closed is a considered rejection.
    Rendering the first as "fix refused" tells the reader a person declined the
    fix when no person was involved, which is exactly backwards: it invites them
    to leave alone the one case the harness is waiting to re-propose.
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
        return STATE_WITHDRAWN if pr_closed_by_harness(pr) else STATE_REFUSED
    return STATE_RESOLVED_MERGED if merged else STATE_RESOLVED


def pr_labels(pr: dict | None) -> set[str]:
    """The label names on a `gh pr list --json labels` record."""
    labels = (pr or {}).get("labels") or []
    names: set[str] = set()
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if isinstance(name, str) and name:
            names.add(name)
    return names


def pr_is_merged(pr: dict | None) -> bool:
    if not pr:
        return False
    return str(pr.get("state", "")).upper() == "MERGED" or bool(pr.get("mergedAt"))


def pr_closed_by_harness(pr: dict | None) -> bool:
    """True when *this harness* closed the pull request, not a human.

    The distinction is the whole of the close-semantics decision. When a finding
    stops reproducing the harness closes its pull request as stale and labels it
    `audit:stale-closed`; if the finding comes back, re-opening a fix is exactly
    right. When a *human* closes one, that is a considered rejection of the
    proposed fix and the harness must never overrule it by opening the same
    pull request again tomorrow morning, and the morning after that.

    The escape hatch for a human who changes their mind is `/remediate <id>` —
    an explicit request, from someone with write access, on the record.
    """
    if not pr or pr_is_merged(pr):
        return False
    if str(pr.get("state", "")).upper() != "CLOSED":
        return False
    return STALE_CLOSED_LABEL in pr_labels(pr)


class PromotionPlan(NamedTuple):
    """What `finish` will do about remediation pull requests this run."""

    promote: list[str]
    withheld: list[str]
    already_open: list[str]
    superseded: list[str] = []


def promotion_candidates(
    findings: list[dict],
    pr_by_finding: dict[str, dict | None],
    requested: list[str] | None = None,
    cap: int = AUTO_PROMOTION_CAP,
    requested_at: dict[str, str] | None = None,
    auto_promote: bool = True,
) -> PromotionPlan:
    """Decide which findings become pull requests this run.

    Auto-promotion is deliberately narrow — `critical`, `manifest`, and no
    live pull request on its branch — and capped, so one bad night cannot bury
    the repository in generated pull requests. An explicit `/remediate` bypasses
    the cap: a human asked for that one by name.

    `auto_promote=False` turns the sweep off entirely and is what the `remediate`
    subcommand passes. That command is a person naming ids, and a person who
    names one id and receives six pull requests has been surprised by their own
    tool — the five they did not ask for are indistinguishable, in the
    repository, from five they did. Auto-promotion belongs to the cron, where a
    named cap and a ledger line explain it; here the request *is* the whole
    instruction.

    Two states are *not* "no pull request", and conflating them is how this goes
    wrong in opposite directions. A pull request the harness closed as stale is
    re-promotable — otherwise a finding that flaps can never be fixed again
    after its first quiet day. A pull request a human closed is not, and neither
    is one they merged: re-opening either overrules a person, daily, forever.

    `already_open` is neither promoted nor withheld — the work exists. It is
    reported so an explicit request gets an answer instead of silence. So is
    `superseded`: a request a human answered by closing the pull request.

    The cap counts findings, and a group of findings sharing a path collapses to
    one pull request, so the number of PRs opened is at most `cap`.
    """
    by_id = {str(f.get("id", "")): f for f in findings}
    requested_set = {fid for fid in (requested or []) if fid in by_id}
    asked_at = requested_at or {}

    promote: list[str] = []
    already_open: list[str] = []
    superseded: list[str] = []

    for fid in sorted(requested_set):
        if (by_id[fid].get("remediation") or {}).get("kind") != "manifest":
            continue
        pr = pr_by_finding.get(fid)
        pr_state = str((pr or {}).get("state", "") or "").upper()
        if pr and pr_state == "OPEN":
            # Force-pushing over a live pull request would discard whatever a
            # reviewer pushed onto it. The ledger already links it.
            already_open.append(fid)
            continue
        # A `/remediate` is an override of the auto-promotion rules, not a
        # standing order. Comments on the ledger are never edited away, so
        # without an age the same March command re-opens a pull request a human
        # closed in April, every morning, forever — the precise loop
        # `pr_closed_by_harness` exists to prevent, re-entered through the
        # escape hatch. A request only overrules a human close if it was
        # written *after* it. Unknown timestamps on either side lose: an
        # unrequestable finding costs one `/remediate`, an un-closeable pull
        # request costs the reader's trust in the close button.
        if pr_state == "CLOSED" and not pr_closed_by_harness(pr):
            closed_at = str((pr or {}).get("closedAt", "") or "")
            if not timestamp_strictly_after(asked_at.get(fid), closed_at):
                superseded.append(fid)
                continue
        promote.append(fid)

    auto: list[str] = []
    for finding in sort_findings(findings) if auto_promote else []:
        fid = str(finding.get("id", ""))
        if fid in requested_set:
            continue
        if finding.get("severity") != "critical":
            continue
        if (finding.get("remediation") or {}).get("kind") != "manifest":
            continue
        pr = pr_by_finding.get(fid)
        if pr is not None and not pr_closed_by_harness(pr):
            continue
        auto.append(fid)

    promote.extend(auto[:cap])
    return PromotionPlan(promote, auto[cap:], already_open, superseded)


# --------------------------------------------------------------------------- #
# Pure helpers — idempotency markers
# --------------------------------------------------------------------------- #


def persists_marker(finding_id: str) -> str:
    return f"<!-- audit-persists:{finding_id} -->"


def refused_marker(comment_id: str) -> str:
    return f"<!-- audit-refused:{comment_id} -->"


def acked_marker(comment_id: str) -> str:
    return f"<!-- audit-acked:{comment_id} -->"


def stale_closed_marker(pr_number: int | str) -> str:
    return f"<!-- audit-stale-closed:{pr_number} -->"


def has_marker(text: str | None, pattern: re.Pattern[str], value: str) -> bool:
    """True when `text` already carries this marker.

    Design §3.1 keeps `/remediate` comments unmutated on purpose, so a repo
    writer can re-issue one after closing a pull request. "Act exactly once"
    therefore lives in the bodies the harness owns, not in the command.
    """
    text = normalise_newlines(text)
    if not text:
        return False
    return value in set(pattern.findall(text))


def any_marker(texts: list[str | None], pattern: re.Pattern[str], value: str) -> bool:
    """True when any of `texts` carries this marker (a body plus its comments)."""
    return any(has_marker(text, pattern, value) for text in texts)


# --------------------------------------------------------------------------- #
# Pure helpers — rendering
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    """Make a value safe, and short, inside a Markdown table cell.

    A cell is a summary line — a title that runs to two thousand characters
    turns the findings table into an unreadable wall and spends budget the
    detail section needs. Clipped here rather than at validation so an
    over-long title costs its own legibility and nothing else.
    """
    value = redact_secrets(text).replace("|", "\\|").replace("\n", " ").strip()
    if len(value) > MAX_CELL_CHARS:
        value = value[: MAX_CELL_CHARS - 1].rstrip() + "…"
    return value


def _ident(value: str) -> str:
    """A Kubernetes identifier, safe and short inside an inline code span.

    Three things at once, because all three failed on the same fields.

    *Clipped*, because `cluster`, `namespace` and `object` were the last
    free-text values the renderer interpolated raw, and `select_rendered_
    findings` always renders the first finding whatever it costs — so one
    oversized identifier overflowed the body and published nothing at all.

    *Flattened*, because a newline ends an inline code span. *Backticks
    replaced*, because one closes it. Either way the rest of the value is
    rendered as Markdown in the reader's browser, and these fields arrive
    verbatim from the model's document.
    """
    text = " ".join(redact_secrets(value).replace("`", "'").split())
    if len(text) <= MAX_IDENT_CHARS:
        return text
    return text[:MAX_IDENT_CHARS].rstrip() + " …(truncated)"


def _fence_for(text: str) -> str:
    """A backtick run longer than any inside `text`, so the block cannot break out."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code_block(text: str, lang: str = "", *, placeholder: str = "") -> list[str]:
    """Render a complete fenced block — opener, body, closer.

    Returning the whole block rather than just the delimiter is the point.
    `_fence()` returned a bare run of backticks, and two callers used that
    return value as though it were the rendered block, emitting a stray ```
    into a comment and dropping the command it was supposed to be showing.
    A helper whose result is unusable on its own invites exactly that.
    """
    body = text if text.strip() else placeholder
    fence = _fence_for(body)
    return [f"{fence}{lang}", body, fence]


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
    """Redact, then clip evidence output so one noisy finding cannot blow the body limit."""
    text = redact_secrets(normalise_newlines(excerpt)).strip("\n").rstrip()
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
    text = redact_secrets(normalise_newlines(command)).strip()
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
    # Every free-text field is clipped, not only the evidence. The body budget
    # guarantees at least one finding always renders, so a single uncapped
    # field on that one finding could push the description past GitHub's limit
    # and publish nothing at all — the noisiest possible failure for the least
    # important reason.
    title = clip_text(finding.get("title", ""), MAX_TITLE_CHARS)
    cluster = _ident(str(finding.get("cluster", "")))
    namespace = _ident(str(finding.get("namespace", "")))
    obj = _ident(str(finding.get("object", "")))
    where = f"`{cluster}`"
    where += f" / `{namespace}`" if namespace else " / _cluster-scoped_"

    lines = [f"#### {title} <!-- finding:{fid} -->", ""]
    lines.append(f"- **Where:** {where} — `{obj}`")
    lines.append(f"- **Impact:** {clip_text(finding.get('impact', ''), MAX_TEXT_CHARS)}")
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
    lines += _code_block(command, "bash", placeholder="# (no command supplied)")

    excerpt = trim_excerpt(str(evidence.get("excerpt", "")))
    if excerpt:
        lines.append("")
        lines += _code_block(excerpt, "text")

    recommendation = finding.get("recommendation") or {}
    lines.append("")
    lines.append(
        f"- **Recommendation:** {clip_text(recommendation.get('action', ''), MAX_TEXT_CHARS)}"
    )
    lines.append(
        f"- **Why this fix:** {clip_text(recommendation.get('rationale', ''), MAX_TEXT_CHARS)}"
    )
    lines.append(
        f"- **Risk on apply:** {clip_text(recommendation.get('risk', ''), MAX_TEXT_CHARS)}"
    )

    remediation = finding.get("remediation") or {}
    kind = remediation.get("kind")
    lines.append("")
    if kind == "manifest":
        path = str(remediation.get("path", ""))
        note = clip_text(remediation.get("note", ""), MAX_NOTE_CHARS)
        suffix = f" — {note}" if note else ""
        lines.append(f"- **Remediation (manifest):** [`{path}`]({path}){suffix}")
    elif kind == "gcloud":
        lines.append("- **Remediation (gcloud):**")
        lines.append("")
        lines += _code_block(
            trim_command(str(remediation.get("note", ""))),
            "bash",
            placeholder="# (no command supplied)",
        )
    else:
        note = clip_text(remediation.get("note", ""), MAX_NOTE_CHARS)
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
                f"| `{_cell(_ident(str(finding.get('cluster', ''))))}` "
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
        out.append(f"- `{fid}` — {_cell(finding.get('title', ''))}")
    return out


class RenderedIssue(NamedTuple):
    """A ledger body together with what it actually managed to say.

    `rendered_ids` is the reason this is not just a string. The delta the next
    run computes, and the delta comment this run posts, must both be taken
    against the ids the body *rendered* — never against the full finding set.
    Get that wrong and a finding dropped for space is announced as resolved:
    the harness claims a fix that never happened, on a critical, in writing.
    """

    body: str
    rendered_ids: list[str]
    omitted: list[dict]

    @property
    def partial(self) -> bool:
        """True when the body could not carry every finding."""
        return bool(self.omitted)


def render_issue_body(
    data: dict,
    *,
    generated_at: datetime,
    audit_id: str | None = None,
    states: dict[str, str] | None = None,
    pr_urls: dict[str, str] | None = None,
    withheld: list[str] | None = None,
) -> RenderedIssue:
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
    return RenderedIssue(body, rendered_ids, omitted)


def _delta_order(ids: list[str], by_id: dict[str, dict]) -> list[str]:
    """Delta rows in the body's own order: severity first, then the stable key.

    An id with no finding behind it sorts last under an unknown severity rather
    than raising — the delta is a notification, and a malformed id must not be
    the thing that stops it being sent.
    """
    return [
        str(f.get("id", ""))
        for f in sort_findings([by_id[fid] for fid in ids if fid in by_id])
    ] + [fid for fid in ids if fid not in by_id]


def render_delta_comment(
    audit_id: str,
    new_ids: list[str],
    resolved_ids: list[str],
    findings: list[dict],
    previous_titles: dict[str, str],
    generated_at: datetime,
    *,
    omitted: int = 0,
) -> str | None:
    """The delta comment, or None when nothing changed (silence beats noise)."""
    if not new_ids and not resolved_ids and not omitted:
        return None

    by_id = {str(f.get("id", "")): f for f in findings}
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [f"### `{audit_id}` audit delta — {stamp}", ""]

    if new_ids:
        out.append(f"**{len(new_ids)} new**")
        out.append("")
        # Severity-first, exactly as the body orders its findings. `new_ids`
        # arrives sorted by id, and slicing an alphabetical list at
        # MAX_DELTA_ROWS decides what a reader sees by the first letter of an
        # id — on a bad night that silently drops the new criticals and keeps
        # fifty minors. This is the one notification that says "look now", so
        # what survives the cut has to be the worst of it.
        for fid in _delta_order(new_ids, by_id)[:MAX_DELTA_ROWS]:
            finding = by_id.get(fid, {})
            severity = str(finding.get("severity", "unknown"))
            title = _cell(finding.get("title", fid))
            out.append(f"- **{severity}** — {title} (`{fid}`)")
        if len(new_ids) > MAX_DELTA_ROWS:
            out.append(
                f"- _…and {len(new_ids) - MAX_DELTA_ROWS} more, lower severity "
                "first to be cut — all of them are in the description above_"
            )
        out.append("")

    if resolved_ids:
        out.append(f"**{len(resolved_ids)} resolved**")
        out.append("")
        # Id order, and it has to stay that way: a resolved finding is absent
        # from this run's document, so its severity is not knowable — only the
        # title the previous body recorded survives. Good news truncated in the
        # wrong order costs nobody anything.
        for fid in resolved_ids[:MAX_DELTA_ROWS]:
            title = _cell(previous_titles.get(fid) or fid)
            out.append(f"- {title} (`{fid}`)")
        if len(resolved_ids) > MAX_DELTA_ROWS:
            out.append(f"- _…and {len(resolved_ids) - MAX_DELTA_ROWS} more_")
        out.append("")

    out.append(
        "The ledger description has been rewritten to the current state of the fleet."
    )
    if omitted:
        # Said here because the delta is computed against what the body could
        # carry. Without this line, "0 resolved" on a partial body reads as a
        # complete picture of a fleet the description only half describes.
        out += [
            "",
            f"**Coverage of this description is partial:** {omitted} further "
            "finding(s) did not fit GitHub's body limit and are not listed above "
            "or below. They are still counted in the title. Resolve some findings, "
            "or narrow the audit's scope, to see them.",
        ]
    # Capping the body made this path reachable: previously the body failed
    # first at ~67 findings, so a delta this large could never be produced.
    return _clip_comment("\n".join(out))


def render_clean_comment(
    audit_id: str, data: dict, generated_at: datetime
) -> str:
    """Comment posted when an audit that previously had findings comes back clean.

    Two comments, really, because a clean run has two very different endings and
    saying the wrong one is worse than saying nothing. Over complete coverage the
    ledger closes and the comment is an all-clear. Over a coverage gap the ledger
    stays open (see `handle_finish`), so the comment must not announce a closure
    that is not happening — a reader who takes "closed as completed" at face
    value on a still-open issue learns to distrust every other line the harness
    writes.
    """
    scope = data.get("scope") or {}
    clusters = list(scope.get("clusters") or [])
    gaps = coverage_gaps(data)
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    shown = clusters[:MAX_SCOPE_ROWS]
    names = ", ".join(f"`{c.get('name', '')}`" for c in shown)
    if len(clusters) > len(shown):
        names += f", and {len(clusters) - len(shown)} more"

    if gaps:
        out = [
            f"### `{audit_id}` found nothing — but did not see the whole fleet",
            "",
            f"The {audit_name(audit_id)} run on {stamp} found **0 findings** across "
            f"{len(clusters)} audited cluster(s): {names}.",
            "",
            "**This is not an all-clear, and the ledger stays open.** A finding's "
            "absence only means it was fixed if the audit actually looked, so "
            "nothing has been reported as resolved and no remediation pull request "
            "has been closed. The ledger closes on the next run that reads the "
            "whole fleet and still finds nothing.",
            "",
            f"Not covered by this run ({len(gaps)}):",
            "",
        ]
    else:
        out = [
            f"### `{audit_id}` is now clean — closing",
            "",
            f"The {audit_name(audit_id)} run on {stamp} found **0 findings** across "
            f"{len(clusters)} audited cluster(s): {names}.",
            "",
            "Every finding previously reported here is gone, so this ledger is being "
            "closed as completed. The next run that finds anything opens a fresh one.",
        ]

    if gaps:
        out += [f"- {_cell(gap)}" for gap in gaps[:MAX_SCOPE_ROWS]]
        if len(gaps) > MAX_SCOPE_ROWS:
            out.append(f"- _…and {len(gaps) - MAX_SCOPE_ROWS} more_")
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


def remediation_pr_title(audit_id: str, group: list[dict]) -> str:
    """One subject for the whole group, named after what it fixes."""
    ordered = sort_findings(group)
    head = ordered[0]
    extra = len(ordered) - 1
    suffix = f" (+{extra} more)" if extra else ""
    return f"fix({audit_id}): {_cell(head.get('title', ''))}{suffix}"


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
    audit_id: str,
    findings: list[dict],
    generated_at: datetime,
    *,
    pr_number: int | str = 0,
    reason: str = "",
) -> str:
    """Why a remediation pull request is being closed unmerged."""
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [
        reason
        or (
            f"Closing unmerged: as of {stamp} the `{audit_id}` audit no longer "
            "reproduces the finding(s) this pull request was opened for. Something "
            "else fixed them, or the objects are gone."
        ),
        "",
    ]
    for finding in sort_findings(findings):
        out += [
            f"**`{finding.get('id', '')}` — {_cell(finding.get('title', ''))}**",
            "",
        ]
        # A resolved finding is absent from the current document, so its command
        # is only known when a previous body recorded it. Say nothing rather
        # than print an empty code fence.
        command = trim_command(str((finding.get("evidence") or {}).get("command", "")))
        if command:
            out += ["The command below no longer shows the deviation:", ""]
            out += _code_block(command, "bash")
            out.append("")
    out += [
        # Say what actually happens on return, not what would be nicest. Only a
        # `critical` finding with a manifest fix is re-proposed without being
        # asked, and only `AUTO_PROMOTION_CAP` of those per run — so promising
        # every reader a fresh pull request writes a cheque the harness does not
        # cash, and the ones it silently fails are precisely the low-severity
        # findings nobody is watching for.
        "The branch is left in place, and this pull request is labelled "
        f"`{STALE_CLOSED_LABEL}` — a close made *here*, by the harness, is never "
        "read as a rejection of the fix.",
        "",
        "If the finding comes back: a `critical` finding with a manifest "
        f"remediation is re-proposed automatically on this same branch (at most "
        f"{AUTO_PROMOTION_CAP} per run). Anything else is listed on the ledger "
        "as awaiting `/remediate <finding-id>`, which re-opens it on request.",
        "",
        stale_closed_marker(pr_number),
    ]
    return "\n".join(out)


def render_persists_comment(
    audit_id: str, finding: dict, generated_at: datetime
) -> str:
    """Said once, on a merged pull request whose finding still reproduces."""
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    evidence = finding.get("evidence") or {}
    out = [
        f"This fix merged, but as of {stamp} the `{audit_id}` audit still "
        f"reproduces `{finding.get('id', '')}` — {_cell(finding.get('title', ''))}.",
        "",
        "Either the remediation was incomplete, or something outside this "
        "repository reverted it. This pull request is **not** reopened: it "
        "merged, and reopening it would misrepresent history. The finding "
        "stays on the ledger, flagged, until it stops reproducing.",
        "",
        "Current evidence:",
        "",
    ]
    out += _code_block(
        trim_command(str(evidence.get("command", ""))),
        "bash",
        placeholder="# (no command supplied)",
    )
    excerpt = trim_excerpt(str(evidence.get("excerpt", "")))
    if excerpt:
        out.append("")
        out += _code_block(excerpt, "text")
    out += ["", persists_marker(str(finding.get("id", "")))]
    return "\n".join(out)


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


def render_ack_comment(
    comment_id: str,
    accepted: list[str],
    outcomes: dict[str, str],
    generated_at: datetime,
) -> str:
    """Said once per `/remediate` the harness *did* act on.

    Silence is not an acceptable answer to a command. A requester who sees
    nothing cannot tell "the audit has not run yet" from "the audit ignored
    me", so they comment again, and the ledger fills with duplicate requests
    for work that is already done.
    """
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    out = [f"That `/remediate` was processed on {stamp}:", ""]
    for fid in accepted:
        out.append(f"- `{fid}` — {outcomes.get(fid, 'no pull request was opened')}")
    out += ["", acked_marker(comment_id)]
    return "\n".join(out)


def render_clean_remediate_answer(
    audit_id: str, request: dict, generated_at: datetime, *, closing: bool
) -> str:
    """Said once per `/remediate` standing on a ledger that came back clean.

    Not a refusal — nothing was wrong with the request. The finding it named has
    simply stopped reproducing, which is the outcome the requester wanted, and
    saying so is what stops them from re-asking on an issue that is about to
    close.
    """
    stamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    targets = request.get("targets") or []
    named = ", ".join(f"`{_ident(t)}`" for t in targets)
    out = [
        f"@{request.get('author', 'someone')} — that `/remediate` was read on "
        f"{stamp}, and no pull request was opened.",
        "",
        f"The {audit_name(audit_id)} audit found **0 findings** on this run"
        + (
            f", so {named} no longer reproduces."
            if targets
            else ", so there is nothing left to remediate."
        )
        + " A pull request here would propose a change nobody needs.",
        "",
        (
            "This ledger is closing as completed. If the finding comes back, the "
            "next run opens a fresh ledger issue — ask again there."
            if closing
            else "This ledger stays open because the run could not see the whole "
            "fleet; ask again once it reports complete coverage."
        ),
        "",
        acked_marker(str(request.get("comment_id", ""))),
    ]
    return _clip_comment("\n".join(out))


# --------------------------------------------------------------------------- #
# I/O shell — every subprocess call funnels through run_cmd
# --------------------------------------------------------------------------- #


# The clone every `git` and `gh` call runs inside, once `ensure_workspace` has
# established it. Module-level rather than threaded through forty call sites,
# but *never* implicit at the boundary: `run_cmd` still takes an explicit `cwd`
# and only falls back to this.
_WORKSPACE: Path | None = None


def workspace() -> Path | None:
    return _WORKSPACE


def set_workspace(path: Path | None) -> None:
    global _WORKSPACE
    _WORKSPACE = path


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run one subprocess, always from a known directory.

    `cwd` is not a convenience. `gh` and `git` are not binaries in this
    container: `/opt/credential-proxy/bin/{gh,git}` are shims that POST their
    argv **and `os.getcwd()`** to a sidecar, which runs the real tool in its own
    filesystem at that path and rejects anything outside the workspace root. A
    call made from whatever directory the agent happened to be in is not merely
    untidy — it is a call the sidecar refuses, or worse, one that lands in the
    wrong clone.
    """
    target = Path(cwd) if cwd is not None else _WORKSPACE
    where = f" (in {target})" if target is not None else ""
    log("$ " + " ".join(cmd) + where)
    try:
        result = subprocess.run(
            cmd,
            check=check,
            text=True,
            capture_output=capture,
            cwd=str(target) if target is not None else None,
        )
    except subprocess.CalledProcessError as exc:
        log(f"FAILED ({exc.returncode}): {' '.join(cmd)}")
        if exc.stderr:
            log(exc.stderr.strip())
        raise
    # `check=False` callers used to fail in silence: the logging lived in the
    # except arm, which a non-raising call never reaches, so a `gh` outage on
    # the comment path left no trace anywhere in the run's output.
    if result.returncode != 0:
        log(f"FAILED ({result.returncode}): {' '.join(cmd)}")
        if capture and result.stderr:
            log(result.stderr.strip())
    return result


def git(
    args: list[str], *, check: bool = True, cwd: str | Path | None = None
) -> subprocess.CompletedProcess:
    return run_cmd(["git"] + args, check=check, cwd=cwd)


def gh(
    args: list[str], *, check: bool = True, cwd: str | Path | None = None
) -> subprocess.CompletedProcess:
    return run_cmd(["gh"] + args, check=check, cwd=cwd)


def refresh_credentials(repo: str | None = None) -> None:
    """Mint the short-lived repo-scoped GitHub App token into gh + the git credential store.

    `repo` is passed explicitly because the fallback is not usable here:
    `refresh_git_credentials()` with no argument re-derives the repository by
    running `git config --get remote.origin.url` in the *current* directory, and
    on this path there is no clone in the current directory yet — establishing
    one is what the token is for.
    """
    from github_token_refresh import refresh_git_credentials

    refresh_git_credentials(repo)


SETTINGS_PATH = os.environ.get("FLEET_AUDIT_SETTINGS") or "/opt/data/SETTINGS.md"

# "- **Git Repo:** https://github.com/owner/repo.git". The operator writes this
# line into SETTINGS.md from the PlatformAgent CR (see
# k8s-operator/internal/controller/platformagent_manifests.go), and writes the
# literal `None` when the CR leaves it unset.
SETTINGS_REPO_RE = re.compile(r"^\s*[-*]?\s*\**Git Repo:\**\s*(\S+)\s*$", re.M)


def repo_from_settings(path: str | None = None) -> str | None:
    """The target repository as `owner/name`, from SETTINGS.md, or None.

    This is the only repo source that works before the clone exists, which is
    why it is tried first. `github-issue-resolver/scripts/resolver.py` reads the
    same line; the two skills now agree by construction rather than by
    coincidence.
    """
    try:
        text = Path(path or SETTINGS_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    match = SETTINGS_REPO_RE.search(text)
    if not match:
        return None
    url = match.group(1).strip().strip("/")
    if url.lower() in {"none", "null", ""}:
        return None
    url = re.sub(r"^https?://(www\.)?github\.com/", "", url)
    url = re.sub(r"^git@github\.com:", "", url)
    url = re.sub(r"\.git$", "", url)
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def resolve_repo() -> str:
    """Resolve the GitOps repository as `owner/name`, without needing a clone.

    Order matters. The git remote used to be the only source, and it cannot
    work on this path: the audit crons start in the agent's profile directory,
    which is not a working tree, so `git config --get remote.origin.url`
    returned nothing and the run died before it could clone anything. SETTINGS.md
    is written by the operator at provisioning time and is present from the
    first second of the pod's life.
    """
    repo = repo_from_settings()
    if repo:
        return repo

    from github_token_refresh import get_current_git_repo

    repo = get_current_git_repo()
    if not repo or "/" not in repo:
        raise RuntimeError(
            f"Could not resolve the target repository as owner/name: no usable "
            f"'Git Repo:' line in {SETTINGS_PATH} and no origin remote in "
            f"{Path.cwd()}"
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


def dry_run_repo_root() -> Path:
    """Where a dry run looks for the manifests the real run would stage.

    The real run resolves every `remediation.path` inside the GitOps clone that
    `ensure_workspace` establishes. A dry run that looked somewhere else would
    report every manifest as missing *precisely when the agent had written it in
    the right place*: the SOPs tell the model it is not in a checkout, so the
    working directory is the agent's profile, never the clone. That turned the
    one command whose job is "show me what would happen" into a command that
    degraded every finding to `manual` and printed no pull request body at all.

    The clone's location is a pure function of the repository name, so it can be
    derived without cloning, fetching, or any other side effect — which keeps
    the dry run's promise intact. If it is not on disk yet (nothing has cloned
    it, or `SETTINGS.md` is absent because this is a laptop and not the pod),
    fall back rather than fail: a command that is safe to run anywhere has to
    run anywhere.
    """
    try:
        import gitops_workspace

        target = gitops_workspace.workspace_path(resolve_repo(), GITOPS_WORKSPACE)
    except Exception:
        return repo_root_best_effort()
    return target if target.is_dir() else repo_root_best_effort()


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
        (
            # Load-bearing, not decorative: `pr_closed_by_harness` reads this
            # label to tell a close the harness made from a close a human made,
            # and that is the whole of the close-semantics decision. It has to
            # be *created* here because `gh pr edit --add-label` does not create
            # a missing label — it resolves the name to an id and errors — and
            # the call site closes with `check=False`. Leave it out and every
            # harness close lands unlabelled, every close then reads as a human
            # rejection, and no finding is ever re-proposed after its first
            # quiet day.
            STALE_CLOSED_LABEL,
            "C5DEF5",
            "Closed by the audit because the finding stopped reproducing; re-opened as a fresh pull request if it returns",
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
    """The audit's single open ledger issue, if any. Highest number wins.

    Raises rather than reporting "none" when the lookup itself fails. The old
    code returned (None, None) on a non-zero exit, which made a `gh` outage
    indistinguishable from an empty result: the run would open a duplicate
    ledger, or on a clean run report CLEAN having closed nothing.

    Highest, not lowest, because the choice has to *converge*. Duplicates only
    exist because a run created one — and that run created the higher number,
    wrote this stream's current state into it, and linked it from every
    remediation pull request it opened. Preferring the lower one abandons that
    work on the very next run, then the run after that creates another, and the
    audit alternates between two ledgers indefinitely. Preferring the higher one
    settles on the ledger everything already points at.
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
    chosen = issues[-1]
    if len(issues) > 1:
        others = ", ".join(f"#{i.get('number')}" for i in issues[:-1])
        log(
            f"WARNING: {len(issues)} open issues carry label audit:{audit_id}; "
            f"updating #{chosen.get('number')} and leaving {others} alone. "
            "Close the duplicates by hand — this harness will not close an issue "
            "it cannot prove it opened."
        )
    return int(chosen["number"]), chosen.get("url")


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
    """Comments on the ledger, for `/remediate` parsing. Empty on failure.

    No sub-projection is possible or needed: `gh issue view --json comments`
    returns a fixed comment struct that already carries `id`, `author`,
    `authorAssociation`, and the `createdAt` the close-vs-request comparison
    reads.
    """
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
    """Write a body to a file `gh` can actually open.

    Not `/tmp`. `gh` is not a binary in this container — the shim POSTs argv to
    a sidecar which runs the real `gh` **in its own filesystem**, and `/tmp` is
    a per-container emptyDir. A `--body-file /tmp/…` path therefore names a file
    that exists in the agent container and does not exist in the one running the
    command, so every issue create, every issue edit and every comment fails
    with "no such file". The shared PersistentVolumeClaim at /opt/data is the
    only filesystem both containers can see.

    Falls back to the system temp directory when the PVC is absent, so the unit
    tests and a local `--dry-run` still work off-cluster.
    """
    directory: str | None = SCRATCH_DIR
    try:
        Path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = None
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8", dir=directory
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

    `labels` is in the projection because the close-semantics rule needs it —
    `audit:stale-closed` is what tells a close the harness made from a close a
    human made, and asking for it here costs nothing over the request already
    being sent. `closedAt` is there for the other half of the same rule: a
    `/remediate` only overrules a human close if it was written after it, and
    that comparison needs a time on both sides.
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
            "number,headRefName,state,mergedAt,closedAt,url,body,labels",
            "--limit",
            str(MAX_PR_PAGE),
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
    prs = [p for p in prs if isinstance(p, dict)]
    if len(prs) >= MAX_PR_PAGE:
        # A silently truncated page is the worst possible answer here: the
        # missing pull requests read as "no pull request", so the harness
        # re-opens fixes that already exist and re-closes ones already closed.
        # Refuse instead — a stream with a thousand remediation pull requests
        # needs a human, not another cron run.
        raise GitHubLookupError(
            f"audit:{audit_id} has at least {MAX_PR_PAGE} remediation pull "
            "requests, so this listing is truncated and the finding-to-PR "
            "mapping cannot be trusted. Merge or close the backlog before the "
            "audit runs again."
        )
    return prs


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
    """Read the remediation files before any branch switch touches them.

    Containment is re-proven here rather than assumed. `degrade_missing_
    remediations` already settled it, so a failure at this point is an
    invariant violation and raises — this is the last read before the bytes go
    into a pull request, and it is cheap to be sure.
    """
    return {
        path: resolve_inside_repo(root, path, "snapshot").read_bytes()
        for path in paths
    }


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
        # Re-proven after the checkout, not carried over from before it: the
        # branch switch replaced the working tree, so `manifests/vendor` may be
        # a directory on the audit's branch and a symlink on `main`. Writing
        # through it would land the manifest outside the repository entirely.
        target = resolve_inside_repo(root, path, "remediation write")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot[path])

    git(build_git_add_command(paths)[1:])

    # Ask git what is staged instead of inferring it from the commit's exit
    # code. `git commit` exits non-zero for "nothing to commit" *and* for a
    # missing committer identity, a failed hook, an unwritable object store and
    # a corrupt index — and the old code read every one of them as "already
    # fixed on main", logged a reassuring line, and returned success having
    # opened nothing. A real failure has to be loud.
    staged = git(["diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        log(
            f"{branch}: the remediation is already present on {BASE_BRANCH}; "
            "no pull request opened."
        )
        return None
    if staged.returncode != 1:
        raise RuntimeError(
            f"{branch}: `git diff --cached --quiet` exited {staged.returncode}; "
            "the index could not be read, so it is not safe to say whether this "
            "remediation is already applied"
        )
    git(["commit", "-m", group_commit_subject(audit_id, group)])
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
    *,
    live_branches: set[str] | None = None,
) -> list[str]:
    """Close every open remediation PR the current findings no longer justify.

    Two reasons a pull request is stale, and the second one is why this cannot
    just read the hidden block. A pull request is stale when every finding it
    covers has stopped reproducing — and also when its branch is no longer any
    group's branch, which happens whenever a group splits or merges because its
    file set changed. The second rule subsumes the first for grouped findings
    and catches the orphans the first rule cannot see.

    The branch is never deleted: if the finding returns, the audit pushes to it
    again. The `audit:stale-closed` label is applied *before* the close and the
    close is abandoned if the label does not stick, so a later run can always
    tell this close from a human's rejection. The comment is posted at most once
    but the close is retried until it succeeds — the marker records that the
    announcement happened, not that the pull request shut.
    """
    closed: list[str] = []
    for pr in prs:
        if str(pr.get("state", "")).upper() != "OPEN":
            continue
        number = int(pr.get("number", 0))
        head = str(pr.get("headRefName", ""))
        covered = parse_delta_block(str(pr.get("body", "")))

        orphaned = bool(live_branches) and not any(
            head == branch or head.endswith(f":{branch}") for branch in live_branches
        )
        if not orphaned:
            if not covered or any(fid in current_ids for fid in covered):
                continue

        # Announce at most once; close as many times as it takes. Every pull
        # request reaching this point is OPEN — the top of the loop skipped the
        # rest — so a marker already on the record does not mean "already
        # closed". It means an earlier run posted the comment and then failed to
        # close, and treating the marker as proof of the close is how a pull
        # request stays open forever while the ledger and the run summary both
        # report it closed.
        announced = has_marker(str(pr.get("body", "")), STALE_CLOSED_MARKER_RE, str(number)) or any(
            has_marker(str(c.get("body", "")), STALE_CLOSED_MARKER_RE, str(number))
            for c in fetch_pr_comments(repo, number)
        )

        findings = [
            resolved_findings.get(fid)
            or {"id": fid, "title": previous_titles.get(fid, ""), "evidence": {}}
            for fid in covered
        ]
        reason = ""
        if orphaned:
            reason = (
                f"Closing unmerged: the `{audit_id}` audit no longer groups its "
                f"findings onto `{head}`. The set of files this fix would touch "
                "has changed, so the work now lives on a different branch — "
                "this pull request would conflict with it."
            )

        # Label first, and refuse to close without it. The label is the only
        # thing that tells a later run this close was the harness's and not a
        # human's, so an *unlabelled* close is worse than no close at all: it
        # reads as a considered rejection and retires the finding permanently.
        # A labelled pull request that is still open, by contrast, costs one
        # line of noise and is fixed on the next run.
        if STALE_CLOSED_LABEL not in pr_labels(pr):
            res = gh(
                ["pr", "edit", str(number), "-R", repo, "--add-label", STALE_CLOSED_LABEL],
                check=False,
            )
            if res.returncode != 0:
                log(
                    f"WARNING: could not label PR #{number} as `{STALE_CLOSED_LABEL}`; "
                    "leaving it open. Closing it unlabelled would read as a human "
                    "rejection and the finding would never be re-proposed."
                )
                continue

        if announced:
            log(
                f"PR #{number} was already announced as stale but is still open; "
                "retrying the close without repeating the comment."
            )
        else:
            post_pr_comment(
                repo,
                number,
                render_stale_close_comment(
                    audit_id, findings, generated_at, pr_number=number, reason=reason
                ),
                what="stale-close comment",
            )
        # Never --delete-branch: a returning finding pushes to this branch again.
        res = gh(["pr", "close", str(number), "-R", repo], check=False)
        if res.returncode != 0:
            # Reporting a close that did not happen is how a run's own summary
            # stops describing the repository.
            log(f"WARNING: could not close PR #{number}; it stays open.")
            continue
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


def ack_remediate_requests(
    repo: str,
    issue_number: int,
    accepted_by_comment: dict[str, list[str]],
    outcomes: dict[str, str],
    existing_comments: list[dict],
    generated_at: datetime,
) -> None:
    """Answer each acted-on `/remediate` exactly once, on the same guard as refusals."""
    answered = "\n".join(str(c.get("body", "")) for c in existing_comments)
    for comment_id, accepted in accepted_by_comment.items():
        if not accepted:
            continue
        if comment_id and has_marker(answered, ACKED_MARKER_RE, comment_id):
            continue
        post_comment(
            repo,
            issue_number,
            render_ack_comment(comment_id, accepted, outcomes, generated_at),
            what="/remediate acknowledgement",
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


def remediation_file_problem(finding: dict, root: Path) -> str | None:
    """`None` if this finding's fix is a readable file inside `root`; else why not.

    Split out so the question can be asked without being answered destructively.
    `degrade_missing_remediations` asks it and then rewrites the finding; the two
    `--dry-run` paths ask it to *warn*, and a dry run that quietly rewrote the
    document it is previewing would be showing the reader a body the real run
    never produces.

    Both failures land here rather than at the point of use, so the containment
    check has exactly one implementation and the `SECURITY:` line is logged
    wherever the question is asked — including from a dry run, which is the
    cheapest place for an operator to discover the problem.
    """
    remediation = finding.get("remediation") or {}
    if remediation.get("kind") != "manifest":
        return None
    path = str(remediation.get("path", ""))
    try:
        resolved = resolve_inside_repo(
            root, path, f"{finding.get('id', '?')}.remediation.path"
        )
    except ValidationError as exc:
        log(f"SECURITY: refusing remediation path {path!r}: {exc}")
        return (
            f"named `{path}` as the fix, but that path does not resolve to a "
            "real file inside the repository, so nothing was read from it and "
            "no pull request can be opened"
        )
    if resolved.is_file():
        return None
    return (
        f"named `{path}` as the fix but did not write it, so no pull "
        "request can be opened for this finding"
    )


def degrade_missing_remediations(findings: list[dict], root: Path) -> list[str]:
    """Downgrade manifest findings whose file was never written, and report them.

    This used to raise, which is the wrong shape of failure by a wide margin.
    A manifest path the model promised and did not write is a defect in one
    *finding*; aborting the run over it suppresses the entire stream, so a
    fleet with nine critical findings publishes nothing at all because the
    tenth finding's author forgot a file. The audit's job is to report what it
    saw. A fix it cannot supply degrades to `manual` — the finding, its
    evidence and its recommendation all survive — and the omission is stated
    in the ledger rather than swallowed.

    A path that escapes the repository degrades the same way, and this is the
    chokepoint for that: every later stage — the snapshot, the checkout write,
    the `git add` — assumes containment was settled here. It is logged as a
    security event rather than a missing file, because it is one.

    Returns the ids that were degraded, for the caller to log.
    """
    degraded: list[str] = []
    for finding in findings:
        reason = remediation_file_problem(finding, root)
        if reason is None:
            continue
        remediation = finding.get("remediation") or {}
        fid = str(finding.get("id", ""))
        note = str(remediation.get("note", "")).strip()
        remediation["kind"] = "manual"
        remediation["path"] = ""
        remediation["note"] = (f"{note} " if note else "") + (
            f"_(The audit {reason}. Apply the recommendation above by hand, or "
            "re-run the audit.)_"
        )
        degraded.append(fid)
    return degraded


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def ensure_workspace(repo: str, *, reset: bool = False) -> Path:
    """Establish (and enter) the clone every git and gh call runs inside.

    Lazy and idempotent: the first audit of the day clones, the other four
    fetch. Nothing in the pod does this at startup, and nothing should — a
    clone baked into the image is stale before the first cron fires.

    `reset` scrubs the working tree, and only `start` may ask for it. Between
    `start` and `finish` the agent writes its remediation manifests into this
    tree; they are untracked until a remediation branch stages them, so a reset
    on the way into `finish` would delete every fix the audit just wrote and
    then report each one as a file the model forgot to produce.
    """
    import gitops_workspace

    with gitops_workspace.workspace_lock(GITOPS_WORKSPACE):
        target = gitops_workspace.ensure_workspace(
            repo,
            _workspace_runner,
            root=GITOPS_WORKSPACE,
            base_branch=BASE_BRANCH,
            reset=reset,
        )
        gitops_workspace.configure_identity(target, _workspace_runner)
    set_workspace(target)
    return target


def _workspace_runner(
    cmd: list[str], *, cwd: str | Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Adapter so gitops_workspace runs through this module's logged runner.

    Named rather than a lambda so the test harness, which patches `run_cmd`,
    covers the clone path like every other subprocess in the skill.
    """
    return run_cmd(cmd, cwd=cwd, check=check)


def handle_start(args: argparse.Namespace) -> None:
    audit_id = validate_audit_id(args.audit)

    # Resolve first, then mint: the token is repo-scoped, and the repository
    # cannot be read off a clone that does not exist yet.
    repo = resolve_repo()
    refresh_credentials(repo)
    # The one place a scrub is correct: the audit has not written anything yet,
    # so whatever is in the tree is debris from a run that did not finish.
    root = ensure_workspace(repo, reset=True)
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
                # Where the GitOps clone actually is. The agent does not start
                # in a working tree and cannot guess this: a `remediation.path`
                # is resolved against this directory, so a manifest written
                # anywhere else is a file the harness will never find.
                "workspace": str(root),
                "findings_path": findings_path,
                "pending_remediation_requests": pending,
            }
        )
    )


def _handle_finish_dry_run(audit_id: str, data: dict, now: datetime) -> None:
    findings = list(data["findings"])

    log("DRY RUN: validated findings; nothing will be committed, pushed, or published.")
    root = dry_run_repo_root()
    log(f"DRY RUN: resolving remediation paths under {root}.")

    # The same degradation the real run applies, so a dry run shows the body
    # that would actually be published rather than an optimistic one. Every
    # step below therefore sees the post-degradation findings, exactly as
    # `handle_finish` does.
    degraded = degrade_missing_remediations(findings, root)
    for fid in degraded:
        log(
            f"WARNING: {fid}'s remediation path is not a readable file inside "
            f"{root}; it degrades to a manual remediation and opens no pull request."
        )
    paths = manifest_paths(findings)

    gaps = coverage_gaps(data)
    for gap in gaps:
        log(f"COVERAGE GAP: {gap}")

    if not findings:
        if gaps:
            log(
                "STATUS: CLEAN but coverage is partial — the ledger would be "
                "refreshed and left OPEN, not closed."
            )
        else:
            log("STATUS: CLEAN — 0 findings; the open ledger (if any) would be closed.")
        print(render_clean_comment(audit_id, data, now))
        return

    states = {str(f.get("id", "")): STATE_OPEN for f in findings}
    plan = promotion_candidates(findings, {})

    # Groups over the whole finding set, filtered to those holding a promoted
    # id — identical to `_open_promoted_prs`. Grouping the promoted subset in
    # isolation reported a different branch name than the run would use, and a
    # dry run whose branch names are wrong is worse than no dry run.
    promoted = set(plan.promote)
    groups = [
        group
        for group in remediation_groups(findings)
        if any(str(f.get("id", "")) in promoted for f in group)
    ]

    log(f"TITLE: {issue_title(audit_id, findings)}")
    # "declared", not "on disk": degradation above already removed the missing ones.
    log(f"MANIFESTS DECLARED: {', '.join(paths) if paths else '(none)'}")
    log(
        "WOULD OPEN: "
        + (
            ", ".join(group_branch_for(audit_id, g) for g in groups)
            if groups
            else "(no remediation pull requests)"
        )
    )
    if plan.withheld:
        log(f"WITHHELD BY THE CAP: {', '.join(plan.withheld)}")
    rendered = render_issue_body(
        data,
        generated_at=now,
        audit_id=audit_id,
        states=states,
        withheld=plan.withheld,
    )
    if rendered.partial:
        log(
            f"WARNING: {len(rendered.omitted)} finding(s) do not fit GitHub's body "
            "limit and would be omitted from the description."
        )
    print(rendered.body)

    # And every pull request body the run would open. Printing the ledger alone
    # left the only *reviewable* artifact — the thing a person is asked to merge
    # — visible nowhere but in production, which is the opposite of what a dry
    # run is for. The issue number is not available here: looking it up is a
    # `gh` call, and this path makes none.
    if groups:
        log(
            "DRY RUN: no --issue is looked up on this path, so the 'Part of #N' "
            "link is omitted from the pull request bodies below."
        )
    for group in groups:
        branch = group_branch_for(audit_id, group)
        log(f"PR BODY FOLLOWS FOR: {branch}")
        print("")
        print(DRY_RUN_PR_SEPARATOR)
        print(f"branch: {branch}")
        print(f"title: {remediation_pr_title(audit_id, group)}")
        print("")
        print(
            render_remediation_pr_body(
                audit_id, group, issue_number=None, generated_at=now
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
            # Same containment proof as the outbound write, and the same reason
            # — the tree just changed under us again. Logged rather than
            # raised: this is a `finally`, and an exception here would replace
            # whatever real failure sent us into it.
            try:
                target = resolve_inside_repo(root, path, "snapshot restore")
            except ValidationError as exc:
                log(f"SECURITY: not restoring {path!r} after the checkout: {exc}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
    return opened


def _remediation_outcomes(
    requests: RemediateRequests,
    plan: PromotionPlan,
    pr_by_finding: dict[str, dict | None],
    opened: list[str],
) -> dict[str, str]:
    """One sentence per accepted `/remediate` target, for the acknowledgement.

    Pure: `pr_by_finding` is expected to be the mapping *after* this run's pull
    requests were opened, so a freshly opened request is named by its URL
    rather than reported as missing.
    """
    just_opened = set(opened)
    outcomes: dict[str, str] = {}
    for fid in requests.targets:
        pr = pr_by_finding.get(fid) or {}
        url = str(pr.get("url") or "")
        if url and url in just_opened:
            outcomes[fid] = f"pull request opened — {url}"
        elif fid in plan.already_open:
            outcomes[fid] = (
                f"a pull request is already open — {url or 'see the table above'}; "
                "it was left untouched rather than force-pushed over"
            )
        elif fid in plan.superseded:
            outcomes[fid] = (
                f"not re-opened — {url or 'the pull request'} was closed by a "
                "person *after* this request was written, so the close answers "
                f"it. Comment `/remediate {fid}` again to overrule that."
            )
        elif url:
            outcomes[fid] = f"pull request refreshed — {url}"
        else:
            outcomes[fid] = (
                "no pull request was opened; the harness could not publish it "
                "this run and will retry on the next audit"
            )
    return outcomes


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
        log("DRY RUN: no branch is created, nothing is pushed, no PR is opened.")
        # The same missing-manifest question the real run asks, against the same
        # clone — but only to warn. The body still renders: the point of this
        # command is to show what the pull request would say, and an operator
        # drafting a document before writing its manifests would otherwise get a
        # blank preview and no explanation.
        dry_root = dry_run_repo_root()
        log(f"DRY RUN: resolving remediation paths under {dry_root}.")
        for fid in args.finding:
            if remediation_file_problem(by_id[fid], dry_root):
                log(
                    f"WOULD REFUSE {fid}: its remediation path is not a readable "
                    f"file inside {dry_root}. The body below is a preview; the "
                    "real run refuses this target until the file is written."
                )
        promoted = set(args.finding)
        groups = [
            g
            for g in remediation_groups(findings)
            if any(str(f.get("id", "")) in promoted for f in g)
        ]
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

    repo = resolve_repo()
    refresh_credentials(repo)
    root = ensure_workspace(repo)
    ensure_labels(repo, audit_id)

    # A named finding whose manifest was never written cannot become a pull
    # request, so it is refused — but only it. `/remediate all` expands to
    # every id in the document, and failing the whole batch over one unwritten
    # file would answer a request for thirty fixes with zero, which is both
    # the least useful outcome and the hardest to act on. Refuse by name,
    # proceed with the rest, and let the operator see exactly which is which.
    degraded = set(degrade_missing_remediations(findings, root))
    refused = [fid for fid in args.finding if fid in degraded]
    requested = [fid for fid in args.finding if fid not in degraded]
    for fid in refused:
        log(
            f"REFUSED {fid}: its remediation path is not a readable file inside "
            f"{root} — either nothing was written there, or the path does not "
            "resolve inside the clone (a `SECURITY:` line above says which). "
            "Write the manifest inside the clone, then ask again."
        )
    if refused and not requested:
        # Nothing survives, so there is no partial success to report and an
        # exit 0 with an empty list would read as "done".
        raise ValidationError(
            f"--finding: the remediation path for {', '.join(refused)} is not a "
            f"readable file inside {root} — it was never written, or it does not "
            "resolve inside the clone; fix that before calling remediate"
        )

    issue_number = args.issue
    if issue_number is None:
        issue_number, _ = find_existing_issue(repo, audit_id)

    pr_by_finding, _ = reconcile_remediation_prs(
        audit_id, findings, list_remediation_prs(repo, audit_id)
    )
    # Routed through the same gate as every other promotion, so an explicit
    # request cannot force-push over a pull request someone is reviewing.
    #
    # The request time is *now*: this is a person typing the command, not a
    # months-old comment being re-read on a cron. That makes it later than any
    # close already on the record, which is exactly the escape hatch
    # `pr_closed_by_harness` documents — a human who changed their mind gets
    # their pull request back, and only a human can reach this path.
    #
    # `auto_promote=False` because this command opens what was named and nothing
    # else. The cron's sweep would otherwise ride along on it, so
    # `remediate --finding one-id` could open six pull requests, five of them
    # for findings the operator never mentioned and cannot tell apart from the
    # one they did.
    plan = promotion_candidates(
        findings,
        pr_by_finding,
        requested,
        requested_at={fid: now.isoformat() for fid in requested},
        auto_promote=False,
    )
    for fid in plan.already_open:
        pr = pr_by_finding.get(fid) or {}
        log(
            f"{fid} already has an open remediation pull request "
            f"({pr.get('url') or '#' + str(pr.get('number', '?'))}); not replacing it."
        )
    opened = _open_promoted_prs(
        repo,
        audit_id,
        findings,
        plan.promote,
        pr_by_finding,
        root=root,
        issue_number=issue_number,
        generated_at=now,
    )
    print(
        json.dumps(
            {
                "status": "REMEDIATED",
                "prs_opened": opened,
                "already_open": plan.already_open,
                "refused": refused,
            }
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

    repo = resolve_repo()
    refresh_credentials(repo)
    root = ensure_workspace(repo)
    ensure_labels(repo, audit_id)

    # A fix the audit promised but did not write degrades that one finding to
    # `manual`; it never suppresses the report.
    for fid in degrade_missing_remediations(findings, root):
        log(
            f"WARNING: {fid}'s remediation file is missing under {root}; the "
            "finding is published with a manual remediation instead."
        )

    # "Absent from this document" only means "fixed" if the audit actually
    # looked. When it could not, resolution is unknowable — so nothing is
    # announced as resolved, no remediation pull request is retired, and the
    # ledger is not closed.
    gaps = coverage_gaps(data)
    for gap in gaps:
        log(f"COVERAGE GAP: {gap}")

    existing_issue, existing_url = find_existing_issue(repo, audit_id)
    previous_body = fetch_issue_body(repo, existing_issue) if existing_issue else ""
    # None means the body was unreadable, which is not the same as empty: the
    # delta is unknowable, so report no delta rather than a fabricated one.
    delta_known = previous_body is not None
    previous_ids = parse_delta_block(previous_body or "")
    previous_titles = parse_finding_titles(previous_body or "")
    # Every finding in the document, rendered or not. The stale-close pass
    # below reads this set and must keep reading it: a finding the body budget
    # dropped still reproduces, and retiring its pull request on that basis
    # would be closing a fix because the report ran out of room.
    current_ids = finding_ids(findings)

    remediation_prs = list_remediation_prs(repo, audit_id)

    # --- Clean run: retire the stream's ledger and every fix it was waiting on. ---
    if not findings:
        prs_closed = (
            []
            if gaps
            else close_stale_remediation_prs(
                repo, audit_id, remediation_prs, set(), previous_titles, {}, now
            )
        )
        if existing_issue:
            # A command standing on the ledger is answered *before* anything
            # closes. "Every /remediate gets exactly one answer" cannot have a
            # clean run as its exception: that is the one morning the issue
            # disappears, taking the thread the requester would re-ask on with
            # it.
            for request in unanswered_remediate_comments(
                fetch_issue_comments(repo, existing_issue)
            ):
                post_comment(
                    repo,
                    existing_issue,
                    render_clean_remediate_answer(
                        audit_id, request, now, closing=not gaps
                    ),
                    what="/remediate answer on a clean run",
                )

        if existing_issue and gaps:
            # Zero findings over incomplete coverage is not an all-clear. The
            # ledger stays open and says why, so the stream self-heals the day
            # the unreadable clusters come back.
            post_comment(
                repo,
                existing_issue,
                render_clean_comment(audit_id, data, now),
                what="partial all-clear comment",
            )
            log(
                f"Audit {audit_id} found nothing, but {len(gaps)} coverage gap(s) "
                f"mean it cannot speak for the fleet; issue #{existing_issue} stays "
                "open and no remediation pull request was closed."
            )
        elif existing_issue:
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
                    "resolved": 0 if gaps else len(previous_ids),
                    "prs_opened": [],
                    "prs_closed": prs_closed,
                    "partial": bool(gaps),
                    "coverage_gaps": gaps,
                }
            )
        )
        return

    # --- Findings: publish the ledger, then propose fixes separately. ---
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
    requests = parse_remediate_commands(ledger_comments, findings)
    plan = promotion_candidates(
        findings,
        pr_by_finding,
        requests.targets,
        requested_at=requests.requested_at,
    )
    for fid in plan.already_open:
        log(f"{fid} already has an open remediation pull request; not replacing it.")
    for fid in plan.superseded:
        pr = pr_by_finding.get(fid) or {}
        log(
            f"{fid} was requested by a `/remediate` older than the close of "
            f"#{pr.get('number', '?')}; the close answers the request. Comment "
            "`/remediate " + fid + "` again to re-open it."
        )

    title = issue_title(audit_id, findings)
    rendered = render_issue_body(
        data,
        generated_at=now,
        audit_id=audit_id,
        states=states,
        pr_urls=pr_urls,
        withheld=plan.withheld,
    )
    if rendered.partial:
        log(
            f"WARNING: {len(rendered.omitted)} finding(s) did not fit GitHub's body "
            "limit and are omitted from the description; the title counts are still "
            "the true totals."
        )

    # Now, and not before: `new` is measured against what this body rendered,
    # because that is what the hidden block records and what the next run will
    # read back. Measured against the full finding set instead, every finding
    # the budget dropped is announced as new every single morning. `resolved`
    # keeps its own, wider yardstick — see `compute_delta`.
    new_ids, resolved_ids = compute_delta(
        previous_ids, rendered.rendered_ids, current_ids
    )
    body_file = _write_temp(rendered.body)
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
        reply_to_refusals(repo, number, requests.refusals, ledger_comments, now)

    # A merged fix whose finding still reproduces is said once, on the pull
    # request, and the pull request is never reopened.
    comment_on_merged_but_persisting(repo, audit_id, findings, pr_by_finding, now)

    # Retiring a pull request means asserting its finding no longer reproduces.
    # Over incomplete coverage that assertion is unfounded, so nothing is
    # closed and every open fix survives to the next complete run.
    if gaps:
        prs_closed = []
        log(
            "Coverage is partial, so no remediation pull request was closed as "
            "stale; a fix cannot be retired on evidence the audit never gathered."
        )
    else:
        prs_closed = close_stale_remediation_prs(
            repo,
            audit_id,
            remediation_prs,
            set(current_ids),
            previous_titles,
            {},
            now,
            live_branches={
                group_branch_for(audit_id, group)
                for group in remediation_groups(findings)
            },
        )

    prs_opened = _open_promoted_prs(
        repo,
        audit_id,
        findings,
        plan.promote,
        pr_by_finding,
        root=root,
        issue_number=number,
        generated_at=now,
    )

    if prs_opened:
        # The ledger was written before those pull requests existed, so it does
        # not yet link them, and neither would the acknowledgement below.
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
        # One extra edit is cheaper than making a reader wait a day.
        if number is not None:
            relink = _write_temp(
                render_issue_body(
                    data,
                    generated_at=now,
                    audit_id=audit_id,
                    states=states,
                    pr_urls=pr_urls,
                    withheld=plan.withheld,
                ).body
            )
            try:
                gh(
                    ["issue", "edit", str(number), "-R", repo, "--body-file", relink],
                    check=False,
                )
            finally:
                _unlink(relink)

    # A command that succeeds silently is indistinguishable from one that was
    # never read, so every accepted `/remediate` gets an answer naming what it
    # produced — once, on the requesting comment's node id.
    if number is not None and requests.accepted_by_comment:
        ack_remediate_requests(
            repo,
            number,
            requests.accepted_by_comment,
            _remediation_outcomes(requests, plan, pr_by_finding, prs_opened),
            ledger_comments,
            now,
        )

    if status == "UPDATED" and number is not None:
        if not delta_known:
            log(
                "Previous ledger body was unreadable; skipping the delta comment "
                "rather than announcing every live finding as new."
            )
        else:
            comment = render_delta_comment(
                audit_id,
                new_ids,
                # Absence is only evidence of a fix when the audit looked. Over
                # a coverage gap it means "not checked", so nothing is announced
                # as resolved.
                [] if gaps else resolved_ids,
                findings,
                previous_titles,
                now,
                omitted=len(rendered.omitted),
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
                "resolved": (
                    0 if (gaps or not delta_known) else len(resolved_ids)
                ),
                "prs_opened": prs_opened,
                "prs_closed": prs_closed,
                # Coverage, and only coverage: `partial` is true iff
                # `coverage_gaps` is non-empty, on this branch and on the CLEAN
                # one alike. It used to also be set by `rendered.partial` —
                # findings dropped for the body budget — which made
                # `partial: true, coverage_gaps: []` reachable and left the
                # agent with a flag it was told to explain and nothing to
                # explain it with.
                #
                # The two are not the same kind of incomplete. A coverage gap
                # means the audit did not *look*, which is why it suppresses
                # the resolved count and the stale-closes above: absence of a
                # finding is not evidence of a fix. Truncation means it looked,
                # found everything, and could not *print* it all — the counts
                # in the title are still true, the delta block still lists
                # exactly what the body rendered, and resolution accounting is
                # unaffected. It is presentational, and it is already surfaced
                # where a reader will meet it: a line in the body itself and a
                # WARNING in the run log.
                "partial": bool(gaps),
                "coverage_gaps": gaps,
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
