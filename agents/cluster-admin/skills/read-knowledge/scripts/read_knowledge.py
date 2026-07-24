#!/opt/hermes/.venv/bin/python3
"""read-knowledge — a read-only OKF retrieval path that can never become a write path (Phase 4 D4, 06 §5).

Retrieves Operational Knowledge Framework entries (runbooks, escalations, blueprints, …) from the
GitOps repo's `knowledge/` tree. It is the READ half of the indirect-coordination model: agents never
call each other; they leave and pick up knowledge here (invariant 3).

Two invariant-preserving properties, enforced structurally (not just by convention):

  1. SPARSE, READ-ONLY CHECKOUT. It fetches ONLY `knowledge/` (cone sparse-checkout + `--depth=1`
     shallow + blob filter), so a read can never materialize the deployable `clusters/` tree or
     accumulate into a commit. After checkout it asserts `clusters/` was not materialized.
  2. HARD-REFUSE WRITES. Every git call goes through a wrapper that permits only a read-only
     subcommand allowlist (clone / sparse-checkout / checkout / config / rev-parse / ls-files); any
     push/commit/add/merge/fetch-write intent — on the command line or in the git call — exits non-zero
     BEFORE touching the repo. There is deliberately no code path that pushes or commits.

The frontmatter parser is imported from the SHARED module (`okf_frontmatter`, shipped at
/opt/defaults/scripts) — the very file `local-dev/okf-validate.py` uses — so an agent reads exactly the
schema CI validates.

Auth: a **contents:read**-scoped token (default env GITHUB_READ_TOKEN), NOT the submit-suggestion write
token. For a local/file path repo (the hermetic Kind test) no token is needed.

Usage:
    read_knowledge.py --repo <url|path> [--ref main] [--type runbook] [--link runbook/x.md] [--json]
    read_knowledge.py --work-dir <existing-sparse-copy> [--type ...]   # reuse an in-pod working copy
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Import the shared OKF parser. In-pod it lives at /opt/defaults/scripts; in the repo (verify/tests) it
# lives at <repo>/local-dev. Try both so read and CI provably share one parser.
_CANDIDATES = [
    "/opt/defaults/scripts",
    str(Path(__file__).resolve().parents[5] / "local-dev"),
]
for _p in _CANDIDATES:
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

from okf_frontmatter import CANONICAL_TYPES, parse_frontmatter  # noqa: E402

# Read-only git subcommands this script is allowed to run. Anything else — push, commit, add, merge,
# rebase, reset, tag, rm, mv — is a write and is refused. `fetch` is intentionally excluded: the shallow
# sparse clone is the only network read we perform.
_READ_ONLY_GIT = {"clone", "sparse-checkout", "checkout", "config", "rev-parse", "ls-files", "-C"}
# Tokens that signal a write/mutation intent anywhere on the command line — a defense-in-depth tripwire.
_WRITE_INTENT = {"push", "commit", "--push", "--commit", "--write", "--allow-write", "-f", "--force"}


class WriteRefused(Exception):
    """Raised when a write/mutation intent is detected — the read path fails closed."""


def refuse_write_intent(argv: list[str]) -> None:
    """Exit closed if the invocation smells like a write. Belt-and-suspenders over the structural
    read-only design: even if a caller tries to smuggle a push/commit through, we stop before any git."""
    for a in argv:
        low = a.strip().lower()
        if low in _WRITE_INTENT:
            raise WriteRefused(
                f"read-knowledge is READ-ONLY and refuses write intent {a!r}; "
                f"use the submit-suggestion skill to propose a change via a reviewed PR."
            )


def git(args: list[str]) -> str:
    """Run a git subcommand, permitting ONLY the read-only allowlist. The first non-flag token is the
    subcommand; a `-C <dir>` prefix is allowed. Refuses everything else."""
    # Find the subcommand (skip a leading `-C <dir>`).
    i = 0
    while i < len(args) and args[i] == "-C":
        i += 2
    sub = args[i] if i < len(args) else ""
    if sub not in _READ_ONLY_GIT:
        raise WriteRefused(f"refusing non-read-only git subcommand: {sub!r}")
    for tok in args:
        if tok.strip().lower() in _WRITE_INTENT:
            raise WriteRefused(f"refusing git call carrying write intent: {tok!r}")
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def sparse_checkout_knowledge(repo: str, ref: str, work: str, token: str | None) -> None:
    """Materialize ONLY knowledge/ from `repo` at `ref` into `work` (cone sparse + shallow + blob
    filter). Never fetches or checks out clusters/ or any other deployable path."""
    clone = [
        "clone",
        "--no-checkout",
        "--depth=1",
        "--filter=blob:none",
        "--branch",
        ref,
    ]
    # A contents:read token for a private https remote, injected as a one-shot header (never persisted
    # to the on-disk config, never logged). File/local-path repos need no token.
    if token and repo.startswith(("http://", "https://")):
        clone = ["-c", f"http.extraHeader=Authorization: Bearer {token}", *clone]
    clone += [repo, work]
    git(clone)
    git(["-C", work, "sparse-checkout", "init", "--cone"])
    git(["-C", work, "sparse-checkout", "set", "knowledge"])
    git(["-C", work, "checkout", ref])


def assert_only_knowledge(work: str) -> None:
    """Fail closed if the checkout materialized anything deployable. In cone mode `set knowledge` yields
    root files + knowledge/ only; clusters/ must be absent."""
    if not os.path.isdir(os.path.join(work, "knowledge")):
        raise RuntimeError(f"sparse checkout did not materialize knowledge/ in {work}")
    for deployable in ("clusters", "fleet", "policy"):
        if os.path.isdir(os.path.join(work, deployable)):
            raise RuntimeError(
                f"sparse checkout leaked a deployable tree ({deployable}/) — read path must fetch "
                f"only knowledge/"
            )


def collect_entries(knowledge_dir: str) -> list[dict]:
    """Parse every knowledge/*.md into {path, type, title, link}. `path`/`link` are relative to
    knowledge/ so callers reference entries the way OKF links do."""
    entries: list[dict] = []
    for dirpath, _, names in os.walk(knowledge_dir):
        for name in sorted(names):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, knowledge_dir)
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            fm = parse_frontmatter(text) or {}
            entries.append(
                {
                    "link": rel,
                    "path": os.path.relpath(full, os.path.dirname(knowledge_dir)),
                    "type": fm.get("type", ""),
                    "title": fm.get("title", ""),
                    "status": fm.get("status", ""),
                }
            )
    return entries


def read_entry(knowledge_dir: str, link: str) -> str:
    """Return the raw content of a single entry addressed by its knowledge/-relative link."""
    target = os.path.normpath(os.path.join(knowledge_dir, link))
    # Contain the read to knowledge/ — a link must never escape the sparse tree.
    if os.path.commonpath([target, knowledge_dir]) != os.path.normpath(knowledge_dir):
        raise ValueError(f"link {link!r} escapes knowledge/")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"no such knowledge entry: {link}")
    with open(target, encoding="utf-8") as fh:
        return fh.read()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read OKF knowledge (read-only, sparse).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="GitOps repo URL or local path (sparse-checkout knowledge/).")
    src.add_argument("--work-dir", help="Reuse an existing sparse working copy (skip clone).")
    p.add_argument("--ref", default="main", help="Branch/ref to read (default: main).")
    p.add_argument("--type", dest="type_filter", help="Filter entries by frontmatter `type`.")
    p.add_argument("--link", help="Fetch one entry by its knowledge/-relative path; prints content.")
    p.add_argument(
        "--token-env",
        default="GITHUB_READ_TOKEN",
        help="Env var holding a contents:read token for a private https repo (default GITHUB_READ_TOKEN).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a text table.")
    return p.parse_args(argv)


def run(argv: list[str]) -> int:
    refuse_write_intent(argv)
    args = parse_args(argv)

    if args.type_filter and args.type_filter not in CANONICAL_TYPES:
        print(
            f"note: '{args.type_filter}' is not a canonical OKF type (open convention) — matching anyway",
            file=sys.stderr,
        )

    tmp: str | None = None
    try:
        if args.work_dir:
            work = args.work_dir
        else:
            tmp = tempfile.mkdtemp(prefix="okf-read-")
            work = tmp
            token = os.environ.get(args.token_env) or None
            sparse_checkout_knowledge(args.repo, args.ref, work, token)
        assert_only_knowledge(work)

        knowledge_dir = os.path.join(work, "knowledge")

        if args.link:
            content = read_entry(knowledge_dir, args.link)
            if args.json:
                print(json.dumps({"link": args.link, "content": content}))
            else:
                print(content)
            return 0

        entries = collect_entries(knowledge_dir)
        if args.type_filter:
            entries = [e for e in entries if e["type"] == args.type_filter]

        if not entries:
            print("no matching knowledge entries", file=sys.stderr)
            return 4

        if args.json:
            print(json.dumps(entries, indent=2))
        else:
            for e in entries:
                title = f" — {e['title']}" if e["title"] else ""
                status = f" [{e['status']}]" if e["status"] else ""
                print(f"{e['type']:<18} {e['link']}{title}{status}")
        return 0
    finally:
        if tmp:
            # Best-effort cleanup of the scratch sparse checkout.
            subprocess.run(["rm", "-rf", tmp], check=False)


def main() -> int:
    try:
        return run(sys.argv[1:])
    except WriteRefused as e:
        print(f"read-knowledge: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — surface a clean error, non-zero exit
        print(f"read-knowledge: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
