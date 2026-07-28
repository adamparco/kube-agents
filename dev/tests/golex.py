#!/usr/bin/env python3
"""One Go comment scanner, shared by every L0 check that reads Go source.

Not a check. Imported the way `yamlsubset.py` is:

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from golex import strip_go_comments  # noqa: E402

Every check that greps Go has to blank comments first, because [[LSN-023]] says the sentence
describing a defect must not satisfy or fail the check that prevents it -- and these checks and the
code they read both discuss the forbidden pattern in prose at length. Two implementations of that
blanking existed: a line-oriented one (`line.split("//", 1)[0]`) in most files, and a
character-level scanner in `one-broker-per-agent.py` written after the line-oriented one truncated

    return fmt.Sprintf("https://%s.%s.svc.cluster.local:%d", brokerName(agent), ...)

at the `//` inside the URL. There, the loss was a false POSITIVE and it was found in an afternoon.
In a check scanning for a forbidden pattern, the same bug drops the rest of the line and reports
nothing -- a false negative, silent by construction, in exactly the checks whose whole value is
catching a literal spelled somewhere it should not be.

So the scanner lives here once, and the checks that need it import it. Two copies of a
single-sourcing check's own machinery is the joke telling itself.
"""

from __future__ import annotations


def strip_go_comments(text: str) -> str:
    """Blank comments, preserving line numbering and STRING LITERALS.

    Line count is preserved so a reported `name:lineno` matches what the editor shows. String
    literals are preserved because a key, a URL or an API group inside one is the subject of the
    check, not commentary about it.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"' or ch == "`" or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and quote != "`" and i + 1 < n:
                    # An escaped character cannot close the literal. Raw (backtick) strings have no
                    # escapes, so the backslash is literal there.
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                if text[i] == "\n" and quote != "`":
                    # Unterminated interpreted literal: malformed Go. Stop rather than run on.
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                # Keep newlines so line numbers survive a block comment.
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)
