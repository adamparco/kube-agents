#!/usr/bin/env python3
"""One corpus enumerator, shared by every L0 check that discovers its inputs from git.

Not a check. Imported the way `golex.py` and `yamlsubset.py` are:

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from gitcorpus import repo_files  # noqa: E402

Why this exists ([[LSN-050]]). Seven L0 scripts each carried their own `git ls-files` discovery
with no `--others`. `ls-files` without `--others` lists the INDEX, and a file created in the
current unit is not in the index until it is staged -- so every one of those checks was blind to
exactly the code nothing has ever reviewed. `api-group-single-sourced.py` ran in full, scanned 115
files, and passed, over a corpus that did not contain the new file carrying the defect it exists to
catch. CI failed a push later.

The blindness is worse than an ordinary gap for two reasons. It is perfectly correlated with
novelty: old files are tracked and fully scanned, new ones are invisible. And it is self-concealing
in the ordinary workflow, because the correct habit -- run the chain, then stage, then commit --
means any unit that happens to re-run the chain after `git add` sees the check work.

Why not a plain `rglob`. `k8s-operator/scripts/vars.sh` is gitignored precisely because it holds
live secrets in plaintext, and whatever a check reads it may print in a failure message.
`--exclude-standard` keeps ignored files -- that one, plus build output -- out of the corpus, so
the corpus grows by exactly the new source files and nothing else.

Why the helper is not enough on its own. A convention is a thing the eighth script copies wrong, so
`invariants-gate.py` carries the paired rule: no script on the L0 chain may invoke `git ls-files`
without `--others`, and this module is the sanctioned way to satisfy it.
"""

from __future__ import annotations

import pathlib
import subprocess


def repo_files(repo: pathlib.Path | str, *pathspecs: str) -> list[str]:
    """Repo-relative paths of every tracked-or-new, non-ignored file, sorted.

    `pathspecs` are git pathspecs (`"*.sh"`, `"k8s-operator"`, `":!vendor"`), passed after `--`.
    With none, the whole worktree.

    Paths that do not resolve to a file on disk are dropped. `--cached` lists what the index holds,
    which includes a tracked file deleted from the worktree; a check that then opens it dies with
    an `ENOENT` that has nothing to do with the property under test.
    """
    root = pathlib.Path(repo)
    argv = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    if pathspecs:
        argv += ["--", *pathspecs]

    listing = subprocess.run(
        argv, check=True, capture_output=True, text=True
    ).stdout

    # `--cached` and `--others` are disjoint sets, but a path can still repeat across pathspecs.
    seen: set[str] = set()
    for rel in listing.split("\0"):
        if rel and rel not in seen and (root / rel).is_file():
            seen.add(rel)
    return sorted(seen)


def read_repo_files(
    repo: pathlib.Path | str, *pathspecs: str, encoding: str = "utf-8"
) -> dict[str, str]:
    """`repo_files`, already read, keyed by repo-relative path.

    Undecodable files are skipped rather than fatal: the corpus is source, a binary that matched a
    pathspec is not the check's business, and dying on it would make the check's reach depend on
    what else happens to live in the tree.
    """
    root = pathlib.Path(repo)
    out: dict[str, str] = {}
    for rel in repo_files(root, *pathspecs):
        try:
            out[rel] = (root / rel).read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return out
