"""What the two GitOps write paths have to agree about.

`submit-suggestion` and `fleet-audit` both open pull requests against the
GitOps repository and they are deliberately *not* one skill: the audit's pull
request is a tracked artifact keyed on the files it fixes, refreshed in place
every run and closed again when the finding stops reproducing, while a
suggestion is a one-shot proposal with no lifecycle at all. Merging them would
mean giving one of them the other's idempotency key.

What they do have to share is a way to tell each other's work apart, and that
is all this module is. `fleet-audit` writes down which files its live findings
claim; `submit-suggestion` reads that back to notice when it is about to open a
second, untracked pull request against a file an audit already owns.

The alternative — and what was tried first — is reading the *prose*: matching
"resolves #28" in a title or body and looking up the issue's labels. It does
not work. Of the five duplicate pull requests that motivated this, one
referenced no issue at all and two buried the reference mid-sentence where no
sane pattern finds it, so a prose check caught two of five. Worse, the signal
is the one thing the author controls: refusing on the issue reference makes
deleting the reference the cheapest way past the refusal, which costs the link
back to the ledger and buys nothing. Files are not editorial.
"""

import json
import re
import subprocess

# Written by `fleet-audit` on every ledger issue it opens.
AUDIT_LEDGER_LABEL = "agent:audit"

# Written on every remediation pull request the audit opens for a finding
# group. `submit-suggestion` needs it to tell "I am being asked to update the
# audit's own pull request", which is allowed, from "I am opening a duplicate
# of it", which is not.
AUDIT_REMEDIATION_LABEL = "audit:remediation"

# One line, one JSON array, same shape as the `audit-findings` block that has
# carried finding ids since the audits shipped. Anchored and newline-free for
# the reason that one is: an unterminated marker pasted into evidence must not
# swallow the rest of the body.
PATHS_RE = re.compile(
    r"^[ \t]*<!--[ \t]*audit-paths:[ \t]*(\[[^\n]*?\])[ \t]*-->[ \t]*$", re.M
)

# A ledger body competes with the findings for GitHub's size limit, so this
# block cannot be unbounded. Two hundred paths is far past any real audit —
# findings that share a path are grouped, so this counts distinct files, not
# findings — and it keeps the worst case around 12KB of a 65KB budget.
PATHS_BLOCK_LIMIT = 200


def render_paths_block(paths) -> str:
    """The hidden block naming the files this audit's live findings claim.

    Emitted even when empty, so a reader can tell "this ledger was written by a
    version that records paths and claims none" from "this ledger predates the
    block". The first means the file is free; the second means unknown, and the
    two must not collapse into the same answer.
    """
    ordered = sorted({str(p) for p in paths if p})[:PATHS_BLOCK_LIMIT]
    payload = json.dumps(ordered, separators=(",", ":"))
    return f"<!-- audit-paths: {payload} -->"


def parse_paths_block(body: str | None) -> list[str] | None:
    """Paths claimed by the body's audit, or `None` when it records none.

    `None` rather than `[]` when the block is missing or unreadable: a ledger
    written before this block existed claims an unknown set of files, not an
    empty one, and a caller that treats the two alike would wave through
    exactly the submissions it exists to catch on every repository that has not
    had an audit run since the upgrade.
    """
    if not body:
        return None
    matches = PATHS_RE.findall(body.replace("\r\n", "\n"))
    if not matches:
        return None
    try:
        paths = json.loads(matches[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(paths, list):
        return None
    return [p for p in paths if isinstance(p, str)]


def _gh_json(args: list[str], cwd: str, runner=subprocess.run):
    """Run one read-only `gh` call, or return None if it did not answer.

    Every caller here is asking a question whose unavailability is survivable,
    so no exception escapes: a token that expired between the refresh and this
    call, a repository the App was not installed on, GitHub being down. The
    distinction between "answered, nothing found" and "did not answer" is the
    caller's to act on, so it is carried in the return value rather than
    flattened to an empty list.

    `cwd` is not decoration: `gh` in this container is a shim that POSTs its
    argv *and* `os.getcwd()` to the credential sidecar, which runs the real
    tool at that path.
    """
    res = runner(
        ["gh"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None


def branch_labels(repo: str, branch: str, cwd: str, runner=subprocess.run):
    """Labels on the open pull request for `branch`, or None if there is none.

    Used to recognise the audit's own pull request before refusing anything: a
    submission onto a branch whose pull request already carries
    `audit:remediation` is the documented "address review feedback" path
    updating a pull request that is already tracked, not a duplicate of it.
    """
    payload = _gh_json(
        [
            "pr", "view", branch, "-R", repo,
            "--json", "labels,state",
        ],
        cwd,
        runner,
    )
    if not isinstance(payload, dict):
        return None
    if str(payload.get("state", "")).upper() != "OPEN":
        return None
    labels = payload.get("labels") or []
    return {str(label.get("name", "")) for label in labels}


def audit_claims(repo: str, cwd: str, runner=subprocess.run, warn=None):
    """Which files a live fleet audit already owns, and what owns them.

    Two sources, because neither alone is enough. Open remediation pull
    requests give an exact file list and the most useful thing to say back —
    "this file is pull request #34, push there" — but only cover findings that
    were promoted, and promotion is capped at five per run. Open ledger issues
    cover every live finding including the unpromoted majority, which is
    precisely the set a human asks the agent to fix by hand.

    Returns `{path: claim}` where `claim` is a short human-readable reference,
    or `None` if GitHub could not be reached at all — callers must not read
    that as "no claims".

    `warn` is called once per source that did not answer while the other did.
    That case returns a real dictionary which is nonetheless *narrower* than
    the truth, and it is the quiet failure: `None` makes the caller say GitHub
    was unreachable, but a half-answered lookup looks exactly like a repository
    with fewer open audits and would wave through the submission this exists to
    catch. Still fails open — a routing correction must not become an outage —
    but never silently.
    """
    prs = _gh_json(
        [
            "pr", "list", "-R", repo,
            "--label", AUDIT_REMEDIATION_LABEL,
            "--state", "open", "--limit", "100",
            "--json", "number,files",
        ],
        cwd,
        runner,
    )
    issues = _gh_json(
        [
            "issue", "list", "-R", repo,
            "--label", AUDIT_LEDGER_LABEL,
            "--state", "open", "--limit", "100",
            "--json", "number,body",
        ],
        cwd,
        runner,
    )
    if prs is None and issues is None:
        return None

    if warn is not None:
        if prs is None:
            warn(
                "could not list the open remediation pull requests; checking "
                "this change against the audit ledgers only, so a file claimed "
                "solely by an open remediation pull request will not be seen"
            )
        if issues is None:
            warn(
                "could not list the open audit ledger issues; checking this "
                "change against the open remediation pull requests only, so a "
                "file claimed solely by a ledger will not be seen"
            )

    claims: dict[str, str] = {}
    # Issues first so that a path claimed by both is reported as the pull
    # request: it is the more actionable of the two answers, since the agent
    # can push to it.
    for issue in issues or []:
        for path in parse_paths_block(str(issue.get("body", ""))) or []:
            claims[path] = f"ledger issue #{issue.get('number')}"
    for pr in prs or []:
        for entry in pr.get("files") or []:
            path = str(entry.get("path", ""))
            if path:
                claims[path] = f"remediation pull request #{pr.get('number')}"
    return claims
