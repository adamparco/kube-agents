#!/usr/bin/env python3
"""Syncs the upstream gke-mcp skills into the tiers 02 §2.1 allocates them to.

WHICH TIER GETS WHICH SKILL IS NOT WRITTEN DOWN HERE, AND MUST NOT BE.
======================================================================
This script used to carry a fifteen-entry `SKILL_MAPPINGS` table with every skill pinned to
`["platform"]` -- a hand-maintained second copy of an allocation that already has exactly one
definition site, `docs/design/02-agent-personas.md` §2.1. The copy went stale the moment the
persona conversion landed, and because this script `rmtree`s its destination before copying, the
next person to run it would have silently re-created ten directories under
`agents/platform/skills/` that 02 §2.1 grants to `cluster-admin` and `developer-team` instead --
undoing the conversion, and never touching the real homes so those copies went stale too. The
defect was not the wrong values in the table; it was that a table existed to hold values at all.

So the mapping is DERIVED at runtime from 02 §2.1, through the very same parser that
`dev/tests/tier-skills-match-the-allocation.py` (V-CMP-020) uses to hold the tree to that table.
One parser, one definition site: this script cannot disagree with the check that guards it,
because there is nothing here for the check to disagree with. If 02 §2.1 moves a ✅, the next run
of this script follows it.

The parser is imported by path rather than copied, because two parsers of one table is the same
defect twice. Its file name is hyphenated, so `importlib` is the only way in; if that file is
renamed or its API changes, this script exits non-zero with the reason rather than falling back to
a guess. A sync that cannot read the allocation must not run: it holds `shutil.rmtree`.

WHAT IT DOES NOT DO, deliberately:
  - It never deletes a skill directory that 02 §2.1 no longer grants a tier. Adding "prune
    anything unallocated" would make this script a destructive walk over the whole `agents/` tree
    on the strength of one parse. V-CMP-020 property 3/4 already reports a stray directory by
    name, and removing it is a human's `git rm`.
  - It never touches a skill that does not exist upstream. `apply-change`, `delegate`, `escalate`,
    `detect-drift`, `read-knowledge`, `provision-*`, `github-issue-resolver`,
    `kube-agents-observability`, `gke-compute-classes`, `gke-manifest-generation` and
    `gke-workload-troubleshooting` are first-party (or forked from a since-renamed upstream skill)
    and are listed at the end of the run so their absence is visible rather than assumed.
  - It refuses, by default, to overwrite a destination whose content has diverged from upstream.
    Divergence is how a locally rewritten skill looks -- `gke-cluster-creator` and
    `gke-cluster-lifecycle` are rewritten for this repo's read-only/broker model -- and a sync
    that reverts those rewrites is the same silent undo in a second costume. `--force` opts in.

Run:  python3 scripts/sync-upstream-skills.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_REPO = "https://github.com/GoogleCloudPlatform/gke-mcp.git"

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPEC = os.path.join(REPO_ROOT, "docs", "design", "02-agent-personas.md")
ALLOCATION_CHECK = os.path.join(REPO_ROOT, "dev", "tests", "tier-skills-match-the-allocation.py")


def load_allocation() -> tuple[dict[str, list[str]], tuple[str, ...]]:
    """02 §2.1's allocation as `{skill: [tier, ...]}`, read through V-CMP-020's own parser.

    Every failure mode here is fatal. A partial or empty allocation would make this script copy
    nothing, or copy into the wrong place, and it would do so while printing a successful-looking
    run -- so an unreadable table stops the sync instead of narrowing it.
    """
    if not os.path.isfile(SPEC):
        raise SystemExit(
            f"FAIL: {os.path.relpath(SPEC, REPO_ROOT)} does not exist. The skill allocation has "
            f"exactly one definition site and this script derives its mapping from it; there is "
            f"no built-in table to fall back to."
        )
    if not os.path.isfile(ALLOCATION_CHECK):
        raise SystemExit(
            f"FAIL: {os.path.relpath(ALLOCATION_CHECK, REPO_ROOT)} does not exist. This script "
            f"reuses that check's parser for 02 §2.1 rather than keeping a second copy of it. "
            f"Restore the file, or point this script at wherever the parser moved to."
        )

    spec = importlib.util.spec_from_file_location("tier_skills_allocation", ALLOCATION_CHECK)
    if spec is None or spec.loader is None:
        raise SystemExit(f"FAIL: cannot import the allocation parser from {ALLOCATION_CHECK}")
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    for attr in ("parse_spec", "allocate", "TIERS"):
        if not hasattr(parser, attr):
            raise SystemExit(
                f"FAIL: {os.path.relpath(ALLOCATION_CHECK, REPO_ROOT)} no longer exports "
                f"`{attr}`. This script derives the tier mapping from that check's parser; the "
                f"two have to move together."
            )

    with open(SPEC, encoding="utf-8") as fh:
        rows, gripes = parser.parse_spec(fh.read())
    if gripes:
        print("FAIL: 02 §2.1's allocation table did not parse cleanly:", file=sys.stderr)
        for gripe in gripes:
            print(f"  - {gripe}", file=sys.stderr)
        raise SystemExit(
            "Refusing to sync from a table that half-parsed -- this script deletes its "
            "destinations, and a short read would delete the wrong ones."
        )

    by_tier, by_skill = parser.allocate(rows)
    empty = [tier for tier in parser.TIERS if not by_tier.get(tier)]
    if empty:
        raise SystemExit(
            f"FAIL: 02 §2.1 yielded no skills at all for {', '.join(empty)}. Refusing to sync "
            f"against an allocation that parsed to nothing."
        )
    return {skill: sorted(tiers) for skill, tiers in by_skill.items() if tiers}, tuple(parser.TIERS)


def run_cmd(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Runs a shell command and returns the result, raising an exception on failure."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}", file=sys.stderr)
        print(f"Stdout:\n{res.stdout}", file=sys.stderr)
        print(f"Stderr:\n{res.stderr}", file=sys.stderr)
        res.check_returncode()
    return res


def trees_differ(src: str, dest: str) -> bool:
    """True if `dest` is not a byte-for-byte copy of `src`, recursively."""

    def walk(cmp_result: filecmp.dircmp) -> bool:
        if cmp_result.left_only or cmp_result.right_only or cmp_result.diff_files:
            return True
        if cmp_result.funny_files:
            return True
        return any(walk(sub) for sub in cmp_result.subdirs.values())

    return walk(filecmp.dircmp(src, dest))


def sync(upstream_skills_dir: str, allocation: dict[str, list[str]], force: bool, dry_run: bool) -> int:
    absent_upstream: list[str] = []
    diverged: list[str] = []
    synced = 0

    for skill_name, tiers in sorted(allocation.items()):
        src_skill_path = os.path.join(upstream_skills_dir, skill_name)
        if not os.path.isdir(src_skill_path):
            absent_upstream.append(skill_name)
            continue

        for tier in tiers:
            dest_path = os.path.join(REPO_ROOT, "agents", tier, "skills", skill_name)
            rel = os.path.relpath(dest_path, REPO_ROOT)

            if os.path.exists(dest_path) and trees_differ(src_skill_path, dest_path):
                if not force:
                    diverged.append(rel)
                    print(f"Skipping '{rel}' -- it differs from upstream (use --force to overwrite)")
                    continue
                print(f"Overwriting DIVERGED '{rel}' (--force)")

            print(f"{'Would sync' if dry_run else 'Syncing'} '{skill_name}' to {rel}...")
            synced += 1
            if dry_run:
                continue

            # Delete existing destination directory to remove stale files
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copytree(src_skill_path, dest_path)

    print(f"\n{synced} skill copy/copies {'planned' if dry_run else 'synced'}.")
    if absent_upstream:
        print(
            f"\n02 §2.1 allocates {len(absent_upstream)} skill(s) that upstream does not ship; they are "
            f"first-party (or forked from a renamed upstream skill) and were left untouched:"
        )
        for skill_name in absent_upstream:
            print(f"  - {skill_name} -> {', '.join(allocation[skill_name])}")
    if diverged:
        print(
            f"\n{len(diverged)} destination(s) have local edits and were NOT overwritten. Review each "
            f"against upstream by hand, or re-run with --force to discard the local version:"
        )
        for rel in diverged:
            print(f"  - {rel}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite destinations whose content has diverged from upstream (discards local edits)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be copied where, and change nothing",
    )
    args = ap.parse_args(argv)

    allocation, tiers = load_allocation()
    print(
        f"Derived the skill allocation from docs/design/02-agent-personas.md §2.1: "
        f"{len(allocation)} skill(s) over {len(tiers)} tier(s)."
    )

    try:
        print("Creating temporary directory for sparse checkout...")
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"Cloning upstream repository (sparse, depth 1): {UPSTREAM_REPO}...")
            run_cmd(
                [
                    "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                    UPSTREAM_REPO, tmpdir,
                ]
            )

            print("Configuring sparse-checkout to retrieve only skills directory...")
            run_cmd(["git", "sparse-checkout", "set", "skills"], cwd=tmpdir)

            upstream_skills_dir = os.path.join(tmpdir, "skills")
            if not os.path.isdir(upstream_skills_dir):
                print(
                    f"Error: upstream skills directory not found in clone: {upstream_skills_dir}",
                    file=sys.stderr,
                )
                return 1

            print("\nSyncing skills...")
            rc = sync(upstream_skills_dir, allocation, force=args.force, dry_run=args.dry_run)
            print("\nSynchronization complete!" if not rc else "\nSynchronization failed.")
            return rc
    except subprocess.CalledProcessError:
        print("\nError: Synchronization failed due to command error. Details above.", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 -- the original contract: never traceback at a user
        print(f"\nError: An unexpected error occurred: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
