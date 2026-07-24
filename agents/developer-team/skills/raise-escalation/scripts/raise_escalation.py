#!/opt/hermes/.venv/bin/python3
"""raise-escalation — a lower tier raises a cross-tier request as an OKF escalation entry (Phase 4 D5).

This is the WRITE half of indirect coordination (invariant 3): agents never call each other. When a
lower tier needs its parent to act, it does NOT reach across — it writes an Operational Knowledge
Framework `escalation` entry (`knowledge/escalation/<slug>.md`, `type: escalation`, 06 §5) on a
proposal branch and submits it through the `submit-suggestion` skill. The request therefore travels
only as a reviewed GitOps PR; the parent picks it up on its next escalation-triage sweep via
`read-knowledge`. The only egress is the Git remote (+ loopback for the submit helper) — there is no
direct agent→agent network path.

The `to:` field is ADVISORY only — a hint for humans. The parent triage SOP re-derives its own scope
from its own CR and ignores `to:`, so a forged or misrouted escalation can never widen anyone's
authority.

Use --dry-run to produce the escalation entry + the corrective-PR artifact (branch + diff) with no
push and no PR — fully hermetic, so the round-trip is provable on Kind with no real GitHub.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Resolve the colocated submit-suggestion helper and the shared token-refresh module for BOTH the
# in-pod layout (/opt/hermes/skills, /opt/defaults/scripts) and the repo/test layout. submit_suggestion
# imports github_token_refresh at import time, so the token candidates must be on sys.path first.
_HERE = Path(__file__).resolve()
_TOKEN_CANDIDATES = ["/opt/defaults/scripts", str(_HERE.parents[3] / "scripts")]
_SUBMIT_CANDIDATES = [str(_HERE.parents[2] / "submit-suggestion" / "scripts")]
for _p in _TOKEN_CANDIDATES + _SUBMIT_CANDIDATES:
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

import submit_suggestion  # noqa: E402

# Each lower tier escalates to exactly one parent. Platform is the top tier and does not escalate.
PARENT_TIER = {"developer-team": "cluster-admin", "cluster-admin": "platform"}


def resolve_tier(explicit: str | None) -> str:
    """Resolve the raising tier: explicit flag > $AGENT_TIER. Must be a lower tier (has a parent)."""
    tier = (explicit or os.environ.get("AGENT_TIER") or "").strip().lower()
    if tier not in PARENT_TIER:
        raise ValueError(
            f"raise-escalation is for lower tiers only (one of {', '.join(sorted(PARENT_TIER))}); "
            f"got '{tier or '<unset>'}'. The platform tier is the top of the chain and does not escalate."
        )
    return tier


def slugify(title: str) -> str:
    """Kebab-case a title into a filesystem/branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if not slug:
        raise ValueError("could not derive a slug from the title; pass --slug explicitly.")
    return slug[:60].strip("-")


def render_escalation(
    *, title: str, summary: str, tier: str, parent: str, severity: str, created: str
) -> str:
    """Render a valid OKF `escalation` entry. Kept link-free so it validates standalone (okf-validate
    requires a non-empty `type` and that every relative link resolves)."""
    return (
        "---\n"
        "type: escalation\n"
        f"title: {title}\n"
        "status: open\n"
        f"from: {tier}\n"
        f"to: {parent}  # ADVISORY only — the parent re-derives its own scope and ignores this\n"
        f"severity: {severity}\n"
        f"created: {created}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"**Raised by:** `{tier}` agent · **Intended parent (advisory):** `{parent}`  \n"
        f"**Severity:** {severity} · **Status:** open\n\n"
        "## Request\n\n"
        f"{summary}\n\n"
        "## Indirect-coordination note\n\n"
        "This is a request, not a change. The parent tier picks it up on its next escalation-triage\n"
        "sweep via the `read-knowledge` skill, **re-derives scope from its own CR** (it does not trust\n"
        "the `to:` field above), and — only if the request is within its own authority — proposes the\n"
        "change via `submit-suggestion`. No agent calls another agent directly (invariant 3).\n"
    )


def _git(work: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", work, *args], check=True, capture_output=True, text=True
    ).stdout


def prepare_working_copy(repo: str | None, work_dir: str | None, ref: str, token: str | None) -> tuple[str, bool]:
    """Return (work, is_temp): reuse an existing working tree, or clone `repo` at `ref` into a temp dir.
    A shallow full clone (not sparse) — we need to write a new commit."""
    if work_dir:
        return work_dir, False
    tmp = tempfile.mkdtemp(prefix="escalation-work-")
    clone = ["clone", "--depth=1", "--branch", ref]
    if token and repo and repo.startswith(("http://", "https://")):
        clone = ["-c", f"http.extraHeader=Authorization: Bearer {token}", *clone]
    clone += [repo, tmp]
    subprocess.run(["git", *clone], check=True, capture_output=True, text=True)
    return tmp, True


def write_and_commit(work: str, tier: str, slug: str, content: str) -> str:
    """Create the tier's proposal branch, write the escalation entry, and commit ONLY that file.
    Returns the branch name."""
    branch = f"{tier}-agent/escalation-{slug}"
    _git(work, "checkout", "-b", branch)
    # Ensure a commit identity exists (a fresh clone doesn't inherit global git config).
    _git(work, "config", "user.email", f"{tier}-agent@kube-agents.local")
    _git(work, "config", "user.name", f"kube-agents {tier} agent")

    esc_dir = os.path.join(work, "knowledge", "escalation")
    os.makedirs(esc_dir, exist_ok=True)
    rel_path = os.path.join("knowledge", "escalation", f"{slug}.md")
    with open(os.path.join(work, rel_path), "w", encoding="utf-8") as fh:
        fh.write(content)

    # Stage ONLY the escalation entry (never `git add .` — the submit-suggestion safety rule).
    _git(work, "add", rel_path)
    _git(work, "commit", "-m", f"docs(escalation): raise {slug}")
    return branch


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Raise an OKF escalation as a reviewed GitOps PR (read-only tiers).")
    p.add_argument("--title", required=True, help="Short escalation title.")
    p.add_argument("--summary", required=True, help="The request body (what the parent should consider).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="GitOps repo URL or local path (cloned to write the entry).")
    src.add_argument("--work-dir", help="Reuse an existing working tree of the GitOps repo.")
    p.add_argument("--ref", default="main", help="Base branch to branch from (default: main).")
    p.add_argument("--slug", help="Override the entry slug (default: derived from the title).")
    p.add_argument("--severity", default="medium", help="Severity hint (default: medium).")
    p.add_argument("--to", dest="to_hint", help="Advisory parent tier (default: derived from your tier).")
    p.add_argument("--created", help="Created date (YYYY-MM-DD; default: today, UTC).")
    p.add_argument("--tier", default=None, help="Raising tier (default: $AGENT_TIER). Must be a lower tier.")
    p.add_argument(
        "--token-env",
        default="GITHUB_READ_TOKEN",
        help="Env var holding a token for cloning a private https repo (default GITHUB_READ_TOKEN).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Produce the entry + corrective-PR artifact (branch + diff) with no push and no PR.",
    )
    p.add_argument("--artifact-dir", default=None, help="With --dry-run, write the artifact here.")
    return p.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    tier = resolve_tier(args.tier)
    parent = (args.to_hint or PARENT_TIER[tier]).strip().lower()
    slug = args.slug or slugify(args.title)
    created = args.created or datetime.date.today().isoformat()

    content = render_escalation(
        title=args.title,
        summary=args.summary,
        tier=tier,
        parent=parent,
        severity=args.severity,
        created=created,
    )

    token = os.environ.get(args.token_env) or None
    work, is_temp = prepare_working_copy(args.repo, args.work_dir, args.ref, token)
    saved_cwd = os.getcwd()
    try:
        branch = write_and_commit(work, tier, slug, content)
        # Hand off to submit-suggestion from within the working tree — the write reaches the parent
        # ONLY as a reviewed PR (or, with --dry-run, as an observable local artifact).
        os.chdir(work)
        title = f"escalation({tier}): {args.title}"
        body = (
            f"Automated OKF escalation raised by the **{tier}** agent (advisory parent: `{parent}`).\n\n"
            f"Adds `knowledge/escalation/{slug}.md` (`type: escalation`). The parent picks this up via "
            f"its escalation-triage SOP and re-derives its own scope — this PR is a request, not a change."
        )
        if args.dry_run:
            submit_suggestion.dry_run(branch, tier, title, body, args.artifact_dir)
        else:
            submit_suggestion.refresh_git_credentials()
            submit_suggestion.push_branch(branch, tier)
            url = submit_suggestion.create_pull_request(None, branch, title, body)
            print(url)
        return 0
    finally:
        os.chdir(saved_cwd)
        if is_temp:
            subprocess.run(["rm", "-rf", work], check=False)


def main() -> int:
    try:
        return run(sys.argv[1:])
    except Exception as e:  # noqa: BLE001 — surface a clean error, non-zero exit
        print(f"raise-escalation: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
