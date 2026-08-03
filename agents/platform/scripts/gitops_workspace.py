#!/opt/hermes/.venv/bin/python3
"""One private git clone per concurrent operation, so agents stop stomping.

The pod does not run in a checkout. `hermes run` starts an agent in its profile
directory, and nothing clones the GitOps repository — so every `git` the skills
issued was landing outside a working tree. This module establishes the clone
lazily, on the first run that needs it.

Why the clone is *leased*
-------------------------
The first version of this file put every repository at one flat path, a pure
function of `owner/name`. That is exactly one working tree for the whole pod,
and the pod runs many agents at once: five audit crons, plus every kanban worker
the dispatcher spawns, plus whatever the operator is doing interactively. In the
incident that prompted this design, the `submit-suggestion` skill ran
`git checkout -b …` and `git push -f` inside the tree a fleet audit was midway
through using, because neither skill named a directory and both defaulted to the
same one.

Serialising that with a lock cannot work, for two independent reasons:

* The window that needs protecting spans processes. A fleet audit writes its
  remediation manifests into the tree *between* `audit_report.py start` and
  `audit_report.py finish` — minutes apart, two separate invocations. An
  `fcntl.flock` fd dies with the process that opened it, so it cannot span them.
* Even if it could, serialising is the wrong answer. Concurrent work by
  different agents is the steady state here, not an anomaly; a ten-minute audit
  must not block an interactive provisioning request.

So each concurrent operation takes a **lease** and gets its own clone under it:

    <root>/<lease>/<owner>__<name>

The lease is a caller-chosen, stable string — the audit id for a fleet audit
stream, the kanban task id for a dispatcher-spawned worker. Being stable is what
lets `start` and `finish` find the same tree with no lookup state; being
per-operation is what keeps two operations out of each other's way. Nobody
waits on anybody.

`<root>/<lease>/.lease` records who holds it. It is the reaper's TTL anchor (its
mtime is refreshed on every `ensure_workspace`), the marker the credential proxy
looks for before it will run a tree-mutating `git`, and the record a client
checks before writing in a tree that might not be its own.

Everything here runs through an injected `runner` so the caller's logging, error
handling and credential proxying apply unchanged, and so the existing test
harness sees these calls like any other.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

# Must be under the credential proxy's workspace root (`/opt/data`, the shared
# PVC). The sidecar executes `git` and `gh` in *its own* filesystem at the cwd
# the client reports, and refuses any path outside that root — so a clone in
# /tmp, which is a per-container emptyDir, is invisible to the process that
# would have to run git in it.
DEFAULT_ROOT = "/opt/data/gitops"

# The name the credential proxy also looks for. Changing it here without
# changing `credential_proxy._GIT_LEASE_MARKER` locks every skill out of git.
LEASE_FILENAME = ".lease"

# How long a lease directory may go untouched before the next caller reclaims
# its disk. Generous on purpose: `ensure_workspace` refreshes the marker on
# every call, so the only trees this reaps are ones whose owner died. A run that
# somehow straddles the TTL loses its untracked manifests and re-clones, which
# is the same outcome a crashed run already had.
DEFAULT_LEASE_TTL_HOURS = 24.0

# Written by the operator at provisioning time, so it is present from the first
# second of the pod's life — unlike a clone, which is what makes it the only
# usable repository source before anything has been cloned.
DEFAULT_SETTINGS_PATH = "/opt/data/SETTINGS.md"

# Tolerates the operator's Markdown bullet and bold markers, and the literal
# `None` when the CR leaves it unset.
SETTINGS_REPO_RE = re.compile(r"^\s*[-*]?\s*\**Git Repo:\**\s*(\S+)\s*$", re.M)

_LEASE_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_LEASE_CHARS = 64

Runner = Callable[..., object]


def settings_path() -> str:
    return (
        os.environ.get("GITOPS_SETTINGS")
        or os.environ.get("FLEET_AUDIT_SETTINGS")
        or DEFAULT_SETTINGS_PATH
    )


def lease_ttl_hours() -> float:
    raw = os.environ.get("GITOPS_LEASE_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_LEASE_TTL_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LEASE_TTL_HOURS


def sanitize_lease(lease: str) -> str:
    """A lease id reduced to something safe to use as one path segment.

    The lease reaches this module from an environment variable and, for
    `submit-suggestion`, from an agent-supplied flag. It becomes a directory
    name directly under the shared root, so `../../etc` or a bare `..` has to
    be impossible rather than merely unlikely.
    """
    cleaned = _LEASE_SAFE_RE.sub("-", str(lease or "").strip()).strip("-.")[
        :_MAX_LEASE_CHARS
    ]
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unusable lease id {lease!r}")
    return cleaned


def lease_id(explicit: str | None = None) -> str:
    """Resolve the lease this process should work under.

    The identifier has to be stable across invocations, because the agent runs
    each shell command in a fresh process: a pid would hand `git commit` and the
    `submit` that follows it two different clones. `HERMES_KANBAN_TASK` is
    pinned into every dispatcher-spawned worker and is exactly the granularity
    wanted — one card, one unit of work, one tree.
    """
    for candidate in (
        explicit,
        os.environ.get("HERMES_KANBAN_TASK"),
        os.environ.get("HERMES_SESSION_ID"),
    ):
        if candidate and str(candidate).strip():
            return sanitize_lease(candidate)
    # No session identity to key off. A fresh lease is still correct — it is
    # isolated from everyone else, which is the point — it just is not
    # recoverable by a later process that did not keep the path.
    return f"adhoc-{os.urandom(4).hex()}"


def lease_dir(root: str | Path, lease: str) -> Path:
    return Path(root) / sanitize_lease(lease)


def workspace_path(repo: str, root: str | Path = DEFAULT_ROOT, *, lease: str) -> Path:
    """Where `owner/name` is cloned for `lease`. One clone per lease, per repo."""
    owner, _, name = str(repo).partition("/")
    if not owner or not name:
        raise ValueError(f"expected a repository as owner/name, got {repo!r}")
    return lease_dir(root, lease) / f"{owner}__{name}"


@contextmanager
def workspace_lock(root: str | Path = DEFAULT_ROOT) -> Iterator[None]:
    """Serialise the shared *bookkeeping* under the root — nothing more.

    An advisory `flock` on a file beside the lease directories. What it guards
    is small and fast by design: reaping expired leases, creating a lease
    directory, and writing its marker. It deliberately does **not** span the
    clone, the fetch, or the caller's work — those happen inside a lease nobody
    else can name, so there is nothing left to serialise, and holding a lock
    across them would reintroduce the queueing this layout exists to avoid.

    Best-effort: if the lock file cannot be created — a read-only or absent PVC
    — the caller proceeds unserialised rather than refusing to run. A missed
    lock costs a retry; a refused run costs the day's audit.
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


def read_lease(holder: str | Path) -> dict | None:
    """The lease record in `holder`, or None if there is not a readable one."""
    try:
        text = (Path(holder) / LEASE_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def write_lease(
    holder: str | Path,
    lease: str,
    repo: str | None = None,
    *,
    owner: str | None = None,
) -> dict:
    """Stamp (or refresh) the lease marker in `holder`.

    Refreshing keeps the original `created_at` so the record still says when the
    work started, and rewrites the file so its mtime — which is what the reaper
    reads — moves forward.
    """
    holder = Path(holder)
    previous = read_lease(holder) or {}
    record = {
        "lease": sanitize_lease(lease),
        "owner": owner or previous.get("owner") or "unknown",
        "repo": repo or previous.get("repo"),
        "created_at": previous.get("created_at") or _now_iso(),
        "refreshed_at": _now_iso(),
        "pid": os.getpid(),
    }
    holder.mkdir(parents=True, exist_ok=True)
    (holder / LEASE_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def assert_lease_owner(workspace: str | Path, lease: str) -> dict:
    """Refuse to act inside a working tree this lease does not hold.

    The credential proxy can tell that a `git push` is happening inside *some*
    lease, but not whose — the shim reports argv and a working directory, not a
    caller identity. That last mile is here: a skill that was handed a path
    checks the marker beside it before it writes, which is the check that would
    have stopped `submit-suggestion` from branching inside a running audit's
    tree.
    """
    holder = Path(workspace).resolve().parent
    record = read_lease(holder)
    if record is None:
        raise PermissionError(
            f"{workspace} is not inside a leased GitOps workspace (no "
            f"{LEASE_FILENAME} in {holder}). Run the skill's `prepare` step to "
            "get a workspace of your own instead of writing in a shared clone."
        )
    held = str(record.get("lease", ""))
    if held != sanitize_lease(lease):
        raise PermissionError(
            f"{workspace} belongs to lease {held!r} (owner {record.get('owner')!r}), "
            f"not to {sanitize_lease(lease)!r}. Another agent is working in that "
            "tree; run the skill's `prepare` step to get your own."
        )
    return record


def reap_stale_leases(
    root: str | Path,
    *,
    ttl_hours: float | None = None,
    keep: Iterable[str] = (),
) -> list[str]:
    """Delete lease directories nobody has touched inside the TTL.

    Only directories holding a `.lease` marker are ever considered, which is
    what makes this safe to run under a root shared with anything else: the
    legacy flat `<root>/<owner>__<name>` clone from before leases existed, the
    lock file, and any directory a human made are all invisible to it.
    """
    root = Path(root)
    ttl = lease_ttl_hours() if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return []
    spared = {sanitize_lease(name) for name in keep if name}
    cutoff = time.time() - ttl * 3600.0
    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name in spared or not entry.is_dir() or entry.is_symlink():
            continue
        marker = entry / LEASE_FILENAME
        try:
            if not marker.is_file() or marker.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        _remove_tree(entry)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def _is_clone(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_workspace(
    repo: str,
    runner: Runner,
    *,
    lease: str,
    root: str | Path = DEFAULT_ROOT,
    base_branch: str = "main",
    remote_url: str | None = None,
    reset: bool = True,
    owner: str | None = None,
) -> Path:
    """Return this lease's clone of `repo`, creating it if it is not there yet.

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
    lease = sanitize_lease(lease)
    holder = lease_dir(root, lease)
    target = workspace_path(repo, root, lease=lease)
    url = remote_url or f"https://github.com/{repo}.git"

    # Short and shared: everything after it happens inside a directory no other
    # caller will name.
    with workspace_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        reap_stale_leases(root, keep={lease})
        write_lease(holder, lease, repo, owner=owner)

    if not _is_clone(target):
        # A partial directory from a clone that died mid-transfer is not a
        # working tree and will never become one; clear it rather than letting
        # `git clone` refuse a non-empty destination forever.
        if target.exists():
            _remove_tree(target)
        runner(["git", "clone", "--quiet", url, str(target)], cwd=str(holder))
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


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _remove_tree(path: Path) -> None:
    """Delete a directory tree without importing shutil's whole surface.

    Scoped deliberately: only ever called on a path this module composed under
    its own root, and only on a lease directory or a destination that is not a
    git working tree.
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


def repo_from_settings(path: str | None = None) -> str | None:
    """The target repository as `owner/name`, from SETTINGS.md, or None.

    This is the only repo source that works before the clone exists, which is
    why it is tried first. `github-issue-resolver/scripts/resolver.py` reads the
    same line; the skills agree by construction rather than by coincidence.
    """
    try:
        text = Path(path or settings_path()).read_text(encoding="utf-8")
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


def resolve_repo(settings: str | None = None) -> str:
    """Resolve the GitOps repository as `owner/name`, without needing a clone.

    Order matters. The git remote used to be the only source, and it cannot
    work on this path: the audit crons start in the agent's profile directory,
    which is not a working tree, so `git config --get remote.origin.url`
    returned nothing and the run died before it could clone anything. SETTINGS.md
    is written by the operator at provisioning time and is present from the
    first second of the pod's life.
    """
    settings = settings or settings_path()
    repo = repo_from_settings(settings)
    if repo:
        return repo

    from github_token_refresh import get_current_git_repo

    repo = get_current_git_repo()
    if not repo or "/" not in repo:
        raise RuntimeError(
            f"Could not resolve the target repository as owner/name: no usable "
            f"'Git Repo:' line in {settings} and no origin remote in {Path.cwd()}"
        )
    return repo


def run_git(argv: list[str], cwd: str | Path, *, check: bool = True):
    """A plain `git` runner for callers with no logging seam of their own.

    `submit_suggestion.py` uses this; `audit_report.py` injects its own recorded
    runner instead. `cwd` is mandatory rather than defaulted — a git command
    with no stated working directory is the bug this whole module exists to fix.
    """
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), check=check, capture_output=True, text=True
    )
