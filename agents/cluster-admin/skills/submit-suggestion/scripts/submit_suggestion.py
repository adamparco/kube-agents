#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — GitOps PR Suggestion Submitter

This script automates GKE-to-GitHub App branch pushing and Pull Request creation.
It cleanly reuses the secure token refresh logic from github_token_refresh.py natively.

It supports a --dry-run mode: the agent has already made a local branch + commit, and
--dry-run halts there — running the same tier-scoping guardrails, then emitting the
observable corrective-PR artifact (branch name + diff vs main) WITHOUT any `git push`
or `gh pr create`. Dry-run is fully hermetic (no token broker, no network), so a
drift/heartbeat SOP can produce a reviewable proposal on Kind with no real GitHub.
"""

import argparse
import os
import subprocess
import sys
# Append global scripts path to allow importing the token refresher
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

from github_token_refresh import refresh_git_credentials, log

# The agent tiers that may propose changes. The branch namespace an agent may push to
# is derived from its tier (`<tier>-agent/...`), so one tier's agent cannot open a PR in
# another tier's namespace. This mirrors the pre-created `<tier>-agent` KSA convention.
VALID_TIERS = {"platform", "cluster-admin", "developer-team"}

# Bases a corrective-PR diff is computed against, in order of preference. Local `main`
# first (the hermetic Kind test / a full clone), then the tracked remote, then `master`.
_DIFF_BASES = ("main", "origin/main", "master")


def resolve_tier(explicit):
    """Resolve the proposing agent's tier: explicit flag > AGENT_TIER env > default 'platform'."""
    tier = (explicit or os.environ.get("AGENT_TIER") or "platform").strip().lower()
    if tier not in VALID_TIERS:
        raise ValueError(
            f"Invalid tier '{tier}'. Must be one of: {', '.join(sorted(VALID_TIERS))}."
        )
    return tier


def validate_branch(branch_name: str, tier: str):
    """Fail closed unless `branch_name` is a legal proposal branch for `tier`.

    Two guardrails, shared by the real push and by --dry-run so the dry-run artifact can
    only ever represent a proposal that would actually be allowed: never a protected
    branch, and only within the agent's own `<tier>-agent/` namespace so tier scoping
    holds at the propose boundary too.
    """
    protected_branches = {"main", "master", "production"}
    clean_branch = branch_name.strip().lower()
    if clean_branch in protected_branches:
        raise ValueError(f"CRITICAL SECURITY REFUSAL: Force-pushing to protected branch '{branch_name}' is strictly blocked by GKE SRE guardrails!")

    tier_prefix = f"{tier}-agent/"
    if not clean_branch.startswith(tier_prefix):
        raise ValueError(
            f"CRITICAL SECURITY REFUSAL: the '{tier}' agent may only push branches under "
            f"'{tier_prefix}' (got '{branch_name}'). Tier scoping is enforced at the propose boundary."
        )


def push_branch(branch_name: str, tier: str):
    """Push the active git branch to the remote origin securely (after tier validation)."""
    validate_branch(branch_name, tier)
    log(f"Pushing active branch '{branch_name}' securely to origin...")
    subprocess.run(["git", "push", "-f", "origin", branch_name], check=True)


def _resolve_diff_base(branch: str) -> str | None:
    """Pick the first existing ref in _DIFF_BASES to diff a proposal branch against."""
    for base in _DIFF_BASES:
        if base.strip().lower() == branch.strip().lower():
            continue
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", base],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return base
    return None


def dry_run(branch: str, tier: str, title: str, body: str, artifact_dir: str | None) -> str:
    """Halt after the local branch + commit and emit the corrective-PR artifact.

    Runs the same tier-scoping guardrails as the real path, confirms the local branch
    exists, then computes the diff vs main and emits it. NEVER pushes and NEVER opens a
    PR, and never touches the token broker — so it is provable on Kind with no real
    GitHub. Returns the diff text (also written under artifact_dir when given).
    """
    validate_branch(branch, tier)

    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        raise ValueError(
            f"--dry-run expects the local proposal branch '{branch}' to already exist "
            f"(the agent commits its change first); it was not found."
        )

    base = _resolve_diff_base(branch)
    if base:
        diff_range = f"{base}...{branch}"
        names = subprocess.run(
            ["git", "diff", "--name-only", diff_range],
            capture_output=True, text=True, check=True,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", diff_range],
            capture_output=True, text=True, check=True,
        ).stdout
    else:
        # No base ref to compare against — fall back to the branch tip's own patch.
        diff_range = f"{branch} (no base ref; showing tip commit)"
        names = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", branch],
            capture_output=True, text=True, check=True,
        ).stdout
        diff = subprocess.run(
            ["git", "show", branch],
            capture_output=True, text=True, check=True,
        ).stdout

    log("DRY RUN — no branch was pushed and no PR was opened.")
    log(f"Proposal branch : {branch}  (tier '{tier}')")
    log(f"Diff range      : {diff_range}")
    files = [n for n in names.splitlines() if n.strip()]
    log(f"Files changed   : {len(files)}")
    for name in files:
        log(f"  - {name}")

    if artifact_dir:
        os.makedirs(artifact_dir, exist_ok=True)
        with open(os.path.join(artifact_dir, "branch.txt"), "w", encoding="utf-8") as fh:
            fh.write(branch + "\n")
        with open(os.path.join(artifact_dir, "suggestion.diff"), "w", encoding="utf-8") as fh:
            fh.write(diff)
        with open(os.path.join(artifact_dir, "pr.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {title}\n\n{body}\n")
        log(f"Artifact written: {artifact_dir}/ (branch.txt, suggestion.diff, pr.md)")

    # Emit the diff to stdout so a caller (or the Kind gate) can capture the proposal
    # without a real remote. This replaces the PR URL the normal path would print.
    print(diff, end="" if diff.endswith("\n") else "\n")
    return diff


def create_pull_request(token: str, branch: str, title: str, body: str) -> str:
    """Submit the Pull Request securely using the GitHub CLI (gh)."""
    log(f"Submitting GitOps Pull Request for branch '{branch}'...")

    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", "main",
        "--head", branch
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    pr_url = res.stdout.strip()
    return pr_url

def main():
    parser = argparse.ArgumentParser(description="Secure GitOps PR Suggestion Submitter")
    parser.add_argument("--branch", required=True, help="Active Git branch name (must start with '<tier>-agent/')")
    parser.add_argument("--title", required=True, help="Pull Request title")
    parser.add_argument("--body", required=True, help="Pull Request description body")
    parser.add_argument("--tier", default=None, help="Proposing agent tier (platform, cluster-admin, developer-team). Defaults to $AGENT_TIER or 'platform'.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Halt after the local branch + commit: validate tier scoping and emit the diff "
             "artifact (branch + diff vs main) WITHOUT any git push or PR. Hermetic, no token broker.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="With --dry-run, also write the artifact (branch.txt, suggestion.diff, pr.md) here.",
    )

    args = parser.parse_args()

    try:
        tier = resolve_tier(args.tier)

        if args.dry_run:
            # Corrective-PR artifact only: no token exchange, no push, no PR.
            dry_run(args.branch, tier, args.title, args.body, args.artifact_dir)
            return

        # Secure dynamic token exchange & Git/gh credentials configuration
        token = refresh_git_credentials()

        # Git branch pushing (scoped to the agent's own tier namespace)
        push_branch(args.branch, tier)

        # Submit Pull Request
        pr_url = create_pull_request(token, args.branch, args.title, args.body)
        log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {pr_url}")

        # Print raw URL to stdout for the MCP tool to parse
        print(pr_url)

    except subprocess.CalledProcessError as e:
        log("FATAL ERROR: GitOps subprocess execution failed!")
        log(f"Exit Code: {e.returncode}")
        if e.stderr:
            log(f"Stderr Output:\n{e.stderr.strip()}")
        if e.stdout:
            log(f"Stdout Output:\n{e.stdout.strip()}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: GitOps suggestion submission failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
