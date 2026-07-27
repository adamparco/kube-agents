#!/usr/bin/env python3
"""A strict, dependency-free reader for the one YAML subset this repo's fixture corpora use.

Lives on its own because it has two callers -- dev/tests/classifier-corpus-lint.py and
dev/tests/undo-corpus-lint.py -- and a parser copied into both is a decision made twice. The two
copies would agree on the day they were forked and drift silently afterwards, each individually
correct, which is the shape LSN-031 already cost this build once.

Not importable as `import yamlsubset` from an arbitrary cwd: the lints add this directory to
sys.path first. That is deliberate too -- dev/tests/ is not a package and making it one would put an
__init__.py in a directory of scripts.
"""

from __future__ import annotations

import re

class CorpusSyntaxError(Exception):
    """The corpus is not in the subset below. Loud, on purpose -- see load_corpus."""


def load_corpus(text: str) -> dict:
    """Parse the corpus with the standard library and nothing else.

    A hand-rolled parser instead of PyYAML for the same reason dev/tests/spec-ids.py has one: this
    runs in the L0 chain, and L0 installs no dependencies -- a check that needs a package is not L0.
    The restriction is cheap here and buys something worth having, which is that the corpus can only
    be written in one shape. The accepted subset:

      * block mappings   `key: value` and `key:` followed by an indented block
      * block sequences  `- scalar` and `- key: value` with the rest of the mapping aligned under it
      * scalars          plain, "double-quoted", 'single-quoted', and `>-` folded blocks
      * `# comments` on their own line or trailing a plain scalar
      * ints, floats, true/false, and empty (null)

    Notably NOT accepted: flow collections. `{a: b}` and `[a, b]` parse in every other YAML reader
    and are rejected here deliberately, because prettier owns this file's formatting and explodes a
    flow mapping that exceeds the print width into a nine-line tower with trailing commas. The
    corpus is read far more often than it is written; refusing the notation that survives one
    `prettier --write` looking like a stack trace is worth a dozen lines of parser.

    Anything outside the subset raises rather than being reinterpreted into something plausible.
    """
    return _Parser(text).document()


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.i = 0

    # -- cursor ------------------------------------------------------------------------------
    def _at_end(self) -> bool:
        self._skip_ignorable()
        return self.i >= len(self.lines)

    def _skip_ignorable(self) -> None:
        while self.i < len(self.lines):
            stripped = self.lines[self.i].strip()
            if stripped == "" or stripped.startswith("#"):
                self.i += 1
            else:
                return

    def _indent(self) -> int:
        line = self.lines[self.i]
        return len(line) - len(line.lstrip(" "))

    def _oops(self, msg: str) -> CorpusSyntaxError:
        return CorpusSyntaxError(f"line {self.i + 1}: {msg}\n  {self.lines[self.i]!r}")

    # -- grammar -----------------------------------------------------------------------------
    def document(self) -> dict:
        if self._at_end():
            return {}
        value = self._block(self._indent())
        if not self._at_end():
            raise self._oops("trailing content after the document body")
        if not isinstance(value, dict):
            raise CorpusSyntaxError("the corpus document must be a mapping")
        return value

    def _block(self, indent: int):
        if self._at_end():
            return None
        body = self.lines[self.i].lstrip(" ")
        if body == "-" or body.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict:
        out: dict = {}
        while not self._at_end():
            here = self._indent()
            if here < indent:
                break
            if here > indent:
                raise self._oops(f"unexpected indent {here}, expected {indent}")
            body = self.lines[self.i].strip()
            if body.startswith("- "):
                break
            key, sep, rest = body.partition(":")
            if not sep:
                raise self._oops("expected `key: value`")
            key = _scalar_text(key.strip())
            if key in out:
                raise self._oops(f"duplicate key {key!r}")
            self.i += 1
            rest = rest.strip()
            if rest in (">", ">-", "|", "|-"):
                out[key] = self._folded(indent)
            elif rest == "":
                out[key] = self._nested(indent)
            else:
                out[key] = _scalar(_strip_comment(rest))
        return out

    def _sequence(self, indent: int) -> list:
        out: list = []
        while not self._at_end():
            here = self._indent()
            if here < indent:
                break
            if here > indent:
                raise self._oops(f"unexpected indent {here}, expected {indent}")
            body = self.lines[self.i].lstrip(" ")
            if body != "-" and not body.startswith("- "):
                break
            item = body[2:] if len(body) > 2 else ""
            if item == "":
                self.i += 1
                out.append(self._nested(indent))
            elif _MAP_ITEM.match(item):
                # `- key: value`: the first pair of a mapping whose remaining keys are aligned two
                # columns in. Rewriting the dash to spaces lets the mapping parser see all of them,
                # including this one, at the same indent.
                self.lines[self.i] = " " * (indent + 2) + item
                out.append(self._mapping(indent + 2))
            else:
                self.i += 1
                out.append(_scalar(_strip_comment(item)))
        return out

    def _nested(self, parent_indent: int):
        """The block under a `key:` with nothing after it, or None if there is none."""
        if self._at_end() or self._indent() <= parent_indent:
            return None
        return self._block(self._indent())

    def _folded(self, parent_indent: int) -> str:
        parts: list[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                parts.append("")
                self.i += 1
                continue
            if len(line) - len(line.lstrip(" ")) <= parent_indent:
                break
            parts.append(line.strip())
            self.i += 1
        while parts and parts[-1] == "":
            parts.pop()
        return " ".join(parts)


_MAP_ITEM = re.compile(r'^(?:"[^"]*"|\'[^\']*\'|[^\s"\'#][^:]*):(?:\s|$)')
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _strip_comment(text: str) -> str:
    """Drop a trailing `# comment`, respecting quotes."""
    if text[:1] in ('"', "'"):
        quote = text[0]
        j = 1
        while j < len(text):
            if quote == '"' and text[j] == "\\":
                j += 2
                continue
            if text[j] == quote:
                return text[: j + 1]
            j += 1
        raise CorpusSyntaxError(f"unterminated quoted scalar: {text!r}")
    cut = text.find(" #")
    return text[:cut].rstrip() if cut >= 0 else text


def _scalar_text(text: str) -> str:
    """A scalar that must come out a string -- mapping keys."""
    value = _scalar(text)
    if not isinstance(value, str):
        raise CorpusSyntaxError(f"expected a string, got {value!r}")
    return value


def _scalar(text: str):
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text in ("", "null", "~"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    if text[:1] in ("[", "{", "&", "*", "!"):
        raise CorpusSyntaxError(
            f"{text!r}: flow collections, anchors and tags are not part of this corpus's YAML "
            "subset -- see load_corpus for why"
        )
    return text
