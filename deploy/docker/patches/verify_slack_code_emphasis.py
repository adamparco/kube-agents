#!/usr/bin/env python3
"""Build-time behaviour gate for the Slack code-emphasis patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_code_emphasis.py``. The applier proves the four
anchors matched and that the file still parses; this proves the shipped renderer
actually stops emitting the literal asterisks that card ``t_549d081c`` put in a
user's Slack thread.

The distinction matters because every way this patch can fail is silent. A
sentinel that never gets restored, a style dict that comes back empty, an
emphasis rule that stops pairing across the mask — none of them raise. They all
look exactly like a report that happened to contain no bolded code, and the next
person to notice is the user reading the thread. So this drives the real
``render_blocks`` over the real reported line and asserts on the elements it
returns, rather than grepping for the text the applier just inserted.

``test_slack_code_emphasis.py`` covers the applier against a fixture and cannot
cover any of this: the edit lives inside Hermes' own module, and the unit suite
never sees the file that ships.

The module is loaded by path rather than imported as
``plugins.platforms.slack.block_kit`` so the gate does not depend on the
package's ``__init__`` — and therefore on the Slack SDK — being importable at
build time. ``block_kit.py`` imports only ``re`` and ``typing``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

RELATIVE = "plugins/platforms/slack/block_kit.py"

# The list item from card t_549d081c, verbatim. Standard Markdown: a bolded
# code span, which the report-format stanza and the platform persona between
# them actively steer workers towards writing.
REPORTED = "- **`adam-new-cluster`** (us-east4) -> Spawning worker card `t_79d6d3d1`"


def _fail(detail: str) -> "SystemExit":
    return SystemExit(f"slack_code_emphasis verify: {detail}")


def _load(root: Path):
    path = root / RELATIVE
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("block_kit_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text_elements(blocks) -> list:
    """Every leaf element of every rich_text block, in document order."""
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in ("text", "link"):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blocks)
    return found


def main(root: Path) -> None:
    block_kit = _load(root)

    blocks = block_kit.render_blocks(REPORTED)
    if not blocks:
        raise _fail(f"render_blocks returned {blocks!r} for the reported line")

    elements = _text_elements(blocks)
    texts = [e.get("text", "") for e in elements]

    # 1) The defect itself: no delimiter may survive into a rendered element.
    for text in texts:
        if "**" in text:
            raise _fail(
                "the reported line still renders a literal '**' — the emphasis "
                f"scan is not matching across a masked code span. Got: {elements!r}"
            )

    # 2) The repair: the cluster name is one element, styled bold *and* code.
    #    Asserted positively so a patch that fixed (1) by deleting the markup
    #    rather than honouring it cannot pass.
    match = [e for e in elements if e.get("text") == "adam-new-cluster"]
    if len(match) != 1:
        raise _fail(
            f"expected exactly 1 'adam-new-cluster' element, got {len(match)}: "
            f"{elements!r}"
        )
    style = match[0].get("style") or {}
    if not (style.get("bold") and style.get("code")):
        raise _fail(
            f"'adam-new-cluster' should be styled bold+code, got {style!r}"
        )

    # 3) Code with no emphasis around it is still plain code, so the patch has
    #    not simply started bolding every span it restores.
    plain = [e for e in elements if e.get("text") == "t_79d6d3d1"]
    if len(plain) != 1 or (plain[0].get("style") or {}) != {"code": True}:
        raise _fail(
            f"'t_79d6d3d1' should be styled code-only, got {plain!r}"
        )

    # 4) A code span stays opaque: masking must not have started interpreting
    #    markdown inside one.
    opaque = _text_elements(block_kit.render_blocks("- `a **b** c`"))
    if [e.get("text") for e in opaque] != ["a **b** c"]:
        raise _fail(
            f"markdown inside a code span is no longer opaque: {opaque!r}"
        )

    print("slack_code_emphasis verify: ok")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
