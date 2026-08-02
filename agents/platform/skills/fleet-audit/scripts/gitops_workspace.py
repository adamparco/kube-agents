#!/opt/hermes/.venv/bin/python3
"""A git clone the fleet-audit skill can actually write in.

The audit crons do not run in a checkout. `hermes run` starts the agent in its
profile directory, and nothing in the pod ever clones the GitOps repository —
so every `git` the fleet-audit harness issued was landing outside a working
tree. `git rev-parse --show-toplevel` failed, `git checkout -B` had nothing to
branch from, and the remediation path could not have opened a pull request on
any cluster, ever. It was not a subtle failure; it was an unexercised one,
because the tests mock the runner and a mocked `git clone` always succeeds.

This module establishes that clone lazily, on the first run that needs it, and
guards it with a lock. The laziness matters: five audit streams share one pod
and one PersistentVolumeClaim, and a clone at image build time would be stale
by the first cron and duplicated five times over. The lock matters for the same
reason — two streams whose schedules collide would otherwise `checkout -B` over
each other's working tree and push whichever tree lost the race.

Everything here runs through an injected `runner` so the caller's logging,
error handling and credential proxying apply unchanged, and so the existing
test harness sees these calls like any other.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

# Must be under the credential proxy's workspace root (`/opt/data`, the shared
# PVC). The sidecar executes `git` and `gh` in *its own* filesystem at the cwd
# the client reports, and refuses any path outside that root — so a clone in
# /tmp, which is a per-container emptyDir, is invisible to the process that
# would have to run git in it.
DEFAULT_ROOT = "/opt/data/gitops"

Runner = Callable[..., object]


def workspace_path(repo: str, root: str | Path = DEFAULT_ROOT) -> Path:
    """Where `owner/name` is cloned. One directory per repository, flat."""
    owner, _, name = str(repo).partition("/")
    if not owner or not name:
        raise ValueError(f"expected a repository as owner/name, got {repo!r}")
    return Path(root) / f"{owner}__{name}"


@contextmanager
def workspace_lock(root: str | Path = DEFAULT_ROOT) -> Iterator[None]:
    """Serialise workspace mutation across concurrently firing audit crons.

    An advisory `flock` on a file beside the clones. Five streams share this
    pod; two that overlap would otherwise interleave `checkout -B`, `add` and
    `push` in one working tree and push a mixture of both.

    Best-effort by design: if the lock file cannot be created — a read-only or
    absent PVC — the audit proceeds unserialised rather than refusing to run.
    A missed lock costs a retry; a refused run costs the day's audit.
    """
    root = Path(root)
    handle = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = open(root / ".lock", "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _is_clone(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_workspace(
    repo: str,
    runner: Runner,
    *,
    root: str | Path = DEFAULT_ROOT,
    base_branch: str = "main",
    remote_url: str | None = None,
    reset: bool = True,
) -> Path:
    """Return the clone of `repo`, creating it if it is not there yet.

    With `reset=True` the working tree is returned scrubbed and positioned on
    `base_branch` at the remote's tip, which is what an audit wants before it
    starts: a leftover branch or a dirty tree from a run that crashed is not
    authored by a human and there is nothing in it to preserve.

    With `reset=False` the tree is left exactly as the caller found it and only
    `origin` is fetched. This is not a nicety — it is the difference between a
    working Tier 2 and a dead one. The agent writes its remediation manifests
    into this tree *between* `start` and `finish`, and those files are untracked
    until the remediation branch stages them, so a `git clean -fd` on the way
    into `finish` deletes every fix the audit just produced and the run reports
    them all as "the file was never written".
    """
    root = Path(root)
    target = workspace_path(repo, root)
    url = remote_url or f"https://github.com/{repo}.git"

    root.mkdir(parents=True, exist_ok=True)

    if not _is_clone(target):
        # A partial directory from a clone that died mid-transfer is not a
        # working tree and will never become one; clear it rather than letting
        # `git clone` refuse a non-empty destination forever.
        if target.exists():
            _remove_tree(target)
        runner(["git", "clone", "--quiet", url, str(target)], cwd=str(root))
        if not _is_clone(target):
            raise RuntimeError(f"clone of {repo} into {target} produced no working tree")

    runner(["git", "remote", "set-url", "origin", url], cwd=str(target), check=False)
    runner(["git", "fetch", "--quiet", "--prune", "origin"], cwd=str(target))
    if not reset:
        return target

    runner(["git", "reset", "--hard", "--quiet"], cwd=str(target), check=False)
    runner(["git", "clean", "-fdq"], cwd=str(target), check=False)
    runner(
        ["git", "checkout", "-B", base_branch, f"origin/{base_branch}"],
        cwd=str(target),
    )
    return target


def _remove_tree(path: Path) -> None:
    """Delete a directory tree without importing shutil's whole surface.

    Scoped deliberately: only ever called on a path this module composed under
    its own root, and only when that path is not a git working tree.
    """
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def configure_identity(
    target: Path,
    runner: Runner,
    *,
    name: str | None = None,
    email: str | None = None,
) -> None:
    """Give the clone a committer identity.

    `git commit` fails outright with "Please tell me who you are" when neither
    `user.name` nor an env fallback is set, and the container image sets
    neither. The failure surfaces as a non-zero commit that the caller has to
    tell apart from "nothing staged" — so the cheaper fix is to make it
    impossible. Repository-local, never `--global`: the clone is disposable and
    the agent should not be rewriting a shared gitconfig.
    """
    name = name or os.environ.get("GIT_AUTHOR_NAME") or "Platform Agent"
    email = (
        email
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or "platform-agent@users.noreply.github.com"
    )
    runner(["git", "config", "user.name", name], cwd=str(target))
    runner(["git", "config", "user.email", email], cwd=str(target))
