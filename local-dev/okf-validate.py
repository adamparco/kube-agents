#!/usr/bin/env python3
"""OKF validator (kube-agents Phase 0, 06 §5).

Validates an Operational Knowledge Framework tree: every `*.md` entry must carry YAML frontmatter
with a non-empty `type`, and every relative markdown link must resolve to an existing file.

Usage:
    python3 local-dev/okf-validate.py [KNOWLEDGE_DIR]

Default KNOWLEDGE_DIR: examples/gitops-repo/knowledge

Exit code 0 = all good; 1 = one or more violations (prints them). No third-party deps.
"""
from __future__ import annotations

import os
import sys

# The frontmatter/link parser is shared with the in-pod read path
# (agents/*/skills/read-knowledge) so CI and the agent read the SAME schema — no drift between "what
# validates" and "what an agent retrieves". Python puts this script's own dir (local-dev/) on
# sys.path[0], so the sibling module imports directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from okf_frontmatter import (  # noqa: E402
    CANONICAL_TYPES,
    LINK_RE,
    is_external,
    link_path,
    parse_frontmatter,
)


def check_file(path: str, root: str, errors: list[str], notes: list[str]) -> None:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rel = os.path.relpath(path, root)

    fm = parse_frontmatter(text)
    if fm is None:
        errors.append(f"{rel}: missing YAML frontmatter (--- block)")
    elif not fm.get("type"):
        errors.append(f"{rel}: frontmatter missing non-empty `type`")
    elif fm["type"] not in CANONICAL_TYPES:
        notes.append(f"{rel}: non-canonical type '{fm['type']}' (allowed — open convention)")

    for target in LINK_RE.findall(text):
        if is_external(target):
            continue
        # Resolve the path portion via the shared parser (handles titles, <angle-brackets>, #anchors).
        lp = link_path(target)
        if not lp:
            continue  # pure anchor
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), lp))
        if not os.path.exists(resolved):
            errors.append(f"{rel}: broken link -> {target}")


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "examples/gitops-repo/knowledge"
    if not os.path.isdir(root):
        print(f"ERROR: knowledge dir not found: {root}", file=sys.stderr)
        return 1

    md_files = [
        os.path.join(dirpath, name)
        for dirpath, _, filenames in os.walk(root)
        for name in filenames
        if name.endswith(".md")
    ]
    if not md_files:
        print(f"ERROR: no markdown entries under {root}", file=sys.stderr)
        return 1

    errors: list[str] = []
    notes: list[str] = []
    for path in sorted(md_files):
        check_file(path, root, errors, notes)

    for note in notes:
        print(f"note: {note}")
    if errors:
        print(f"\nOKF validation FAILED ({len(errors)} issue(s)) in {root}:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"OKF validation PASSED: {len(md_files)} entr(y/ies) in {root} — all typed, links resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
