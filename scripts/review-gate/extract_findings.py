#!/usr/bin/env python3
"""Extract the aggregated JSON finding array from the detector's headless output (Phase 5 / P5-T4).

The agent-driven review skills emit the finding array but may wrap it in prose or a ```json fence
(their SKILL.md says "markdown blocks okay"). This pulls the first top-level JSON array out of the raw
text and validates it is a list; on any failure it prints `[]` so the scorer runs on a well-formed
(empty) input rather than crashing. Dependency-free (stdlib only), matching the review-gate idiom.
"""

import json
import sys


def extract_json_array(text):
    """Return the first balanced top-level JSON array in `text` as a Python list, or [] on failure."""
    if not text:
        return []
    # Fast path: the whole payload is already a JSON array.
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except ValueError:
        pass
    # Scan for the first '[' that begins a balanced array, honoring strings/escapes.
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, list):
                            return parsed
                    except ValueError:
                        start = -1  # not valid JSON; keep scanning for the next array
    return []


def main(argv=None):
    raw = sys.stdin.read()
    print(json.dumps(extract_json_array(raw)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
