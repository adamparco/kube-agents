#!/usr/bin/env python3
"""Shared OKF frontmatter parser (kube-agents, 06 §5).

This is the SINGLE source of truth for how an Operational Knowledge Framework entry's YAML
frontmatter and inline links are parsed. Both consumers import from here so "what CI validates" and
"what an agent reads" can never drift:

  - `local-dev/okf-validate.py` — the CI/local validator (Phase 0).
  - `agents/*/skills/read-knowledge/scripts/read_knowledge.py` — the in-pod read path (Phase 4 D4).

It ships to every agent image at `/opt/defaults/scripts/okf_frontmatter.py` (baked in the shared
agent-base stage, see `deploy/docker/Dockerfile`), so the read path imports the very same file the
validator does. No third-party dependencies — stdlib only — so it works in the offline build inner
loop and in-pod without a package install.
"""

from __future__ import annotations

import re

# Canonical starting types (06 §5). `type` is an OPEN convention, so an unknown type is a note, not an
# error — only a missing/empty `type` fails validation. The read path uses this set the same way.
CANONICAL_TYPES = {
    "index",
    "cluster-blueprint",
    "tenancy-model",
    "runbook",
    "metric-definition",
    "escalation",
    "observation",
}

# [text](target), skipping images ![...]().
LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")
# A leading `--- ... ---` YAML frontmatter block.
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict | None:
    """Return the leading YAML frontmatter as a flat dict, or None if there is no `---` block.

    Deliberately minimal (no PyYAML): splits each `key: value` line, ignores comments. Nested YAML is
    not modelled — OKF frontmatter is flat by convention (06 §5).
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm


def is_external(target: str) -> bool:
    """True for links that don't resolve to a file on disk (http/https/mailto/pure-anchor)."""
    return target.startswith(("http://", "https://", "mailto:", "#"))


def link_path(target: str) -> str:
    """Extract the on-disk path portion of a CommonMark inline link target.

    Handles an optional link title `path.md "Title"` / `path.md 'Title'`, the angle-bracket form
    `<path.md>`, and a trailing `#anchor`. Returns "" for a pure anchor. Kept here (not in the
    validator) so the read path resolves links exactly as CI does.
    """
    lp = target.strip()
    if lp.startswith("<") and ">" in lp:
        lp = lp[1 : lp.index(">")]
    else:
        parts = lp.split(None, 1)  # path is the first whitespace-delimited token
        lp = parts[0] if parts else ""
    return lp.split("#", 1)[0].strip()
