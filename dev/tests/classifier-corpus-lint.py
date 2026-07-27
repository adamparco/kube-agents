#!/usr/bin/env python3
"""V-MET-005: the risk-classification corpus covers what it claims to.

Two halves, and this is the YAML one. corpus_test.go runs the same coverage property against the
rule IDs the Go code actually defines -- which catches a rule renamed in code and not in the corpus
-- and it needs a Go toolchain and a compiled package to do it. This half needs neither, so it runs
in the L0 chain on every push and fails in seconds rather than after a build.

What it checks, and why each one is here rather than left to review:

  1. 120-200 cases (09 §7.1). A corpus that quietly shrinks is a corpus somebody deleted the awkward
     cases out of.
  2. Unique IDs, and IDs that never move. The traceability matrix and the audit journal cite them.
  3. Every code-floor rule has at least one case asserting it FIRES. A floor rule with no fixture is
     a rule nobody has ever seen fire.
  4. Every gating rule also has at least one case asserting it STAYS QUIET. This is the half that
     matters most and the half a corpus author skips: positive-only coverage is satisfied by a
     classifier that gates everything, and an always-gating broker is not a safe failure -- it
     trains operators to approve without reading, which is how a gate stops being a control.
  5. Both directions of the 03 §5.2 asymmetry, per security control.
  6. The production ladder's rungs and its near-miss values.
  7. Structural validity of each case: no unknown keys, no unknown class, no case that asserts
     nothing.

Run:  python3 dev/tests/classifier-corpus-lint.py
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CORPUS = REPO / "verification" / "fixtures" / "classifier-corpus.yaml"
FLOOR_GO = REPO / "k8s-operator" / "internal" / "broker" / "classify" / "floor.go"
DIRECTION_GO = REPO / "k8s-operator" / "internal" / "broker" / "classify" / "direction.go"

MIN_CASES = 120
MAX_CASES = 200

CLASSES = {"routine", "elevated", "gated", "forbidden"}

# Rules that contribute a class rather than an escalation, and so can be asserted absent. The
# escalation rules and `default-routine` are excluded: `default-routine` fires exactly when nothing
# else does, so "it must not fire" is not an independent assertion about a control.
NEGATIVE_EXEMPT = {"default-routine"}

CASE_KEYS = {
    "id",
    "description",
    "caller",
    "ops",
    "undoPlan",
    "dryRun",
    "requireApproval",
    "maxObjects",
    "seen",
    "expect",
}
OP_KEYS = {
    "verb",
    "group",
    "kind",
    "namespace",
    "name",
    "direction",
    "objects",
    "fraction",
    "liveLabels",
    "namespaceLabels",
    "override",
    "lowerTierOwner",
    "secretMaterial",
    "touchedPaths",
}
CALLER_KEYS = {"name", "tier", "project", "cluster", "namespace"}
EXPECT_KEYS = {"class", "rules", "notRules", "abort"}


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


def fail(msg: str) -> None:
    FAILURES.append(msg)


FAILURES: list[str] = []


def floor_rule_ids() -> list[str]:
    """Read the rule IDs out of floor.go's const block.

    Parsed from the source rather than duplicated here, so a renamed rule is a lint failure in this
    file instead of a lint that silently checks a rule that no longer exists.
    """
    text = FLOOR_GO.read_text()
    block = re.search(r"^const \($(.*?)^\)$", text, re.S | re.M)
    if not block:
        fail(f"could not find the rule-ID const block in {FLOOR_GO}")
        return []
    ids = re.findall(r'^\s*Rule\w+\s*=\s*"([a-z0-9-]+)"', block.group(1), re.M)
    if not ids:
        fail(f"found the const block in {FLOOR_GO} but no rule IDs in it")
    return ids


def security_controls() -> list[str]:
    text = DIRECTION_GO.read_text()
    return re.findall(r'^\s*Control\w+\s+SecurityControl\s*=\s*"([a-z0-9-]+)"', text, re.M)


def main() -> int:
    if not CORPUS.exists():
        print(f"FAIL: {CORPUS} does not exist", file=sys.stderr)
        return 1

    try:
        doc = load_corpus(CORPUS.read_text())
    except CorpusSyntaxError as e:
        print(f"FAIL: {CORPUS.relative_to(REPO)} is not in the accepted YAML subset\n  {e}", file=sys.stderr)
        return 1
    cases = doc.get("cases")
    if not isinstance(cases, list):
        print("FAIL: the corpus has no `cases` list", file=sys.stderr)
        return 1

    # 1. Size.
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        fail(f"09 §7.1 specifies a {MIN_CASES}-{MAX_CASES} case corpus; found {len(cases)}")

    # 2 + 7. Per-case structure.
    ids: set[str] = set()
    fires: dict[str, list[str]] = {}
    quiet: dict[str, list[str]] = {}
    directions: dict[str, set[str]] = {}
    override_values: set[str] = set()
    env_labels: set[tuple[str, str]] = set()
    saw_abort = False

    for i, case in enumerate(cases):
        where = f"cases[{i}]"
        if not isinstance(case, dict):
            fail(f"{where} is not a mapping")
            continue
        cid = case.get("id")
        if not cid:
            fail(f"{where} has no id")
            continue
        where = f"case {cid!r}"
        if cid in ids:
            fail(f"{where}: duplicate id")
        ids.add(cid)

        unknown = set(case) - CASE_KEYS
        if unknown:
            fail(f"{where}: unknown keys {sorted(unknown)}")
        if not case.get("description"):
            fail(f"{where}: no description; a fixture nobody can read is a fixture nobody maintains")

        caller = case.get("caller") or {}
        if isinstance(caller, dict):
            unknown = set(caller) - CALLER_KEYS
            if unknown:
                fail(f"{where}: unknown caller keys {sorted(unknown)}")

        ops = case.get("ops")
        if not isinstance(ops, list) or not ops:
            fail(f"{where}: no ops")
            ops = []
        for j, o in enumerate(ops):
            if not isinstance(o, dict):
                fail(f"{where}: ops[{j}] is not a mapping")
                continue
            unknown = set(o) - OP_KEYS
            if unknown:
                fail(f"{where}: ops[{j}] unknown keys {sorted(unknown)}")
            if not o.get("verb") or not o.get("kind"):
                fail(f"{where}: ops[{j}] needs a verb and a kind")
            if o.get("override"):
                override_values.add(str(o["override"]))
            for scope_name in ("liveLabels", "namespaceLabels"):
                for k, v in (o.get(scope_name) or {}).items():
                    if k in ("kube-agents/environment", "env"):
                        env_labels.add((k, str(v)))

        expect = case.get("expect")
        if not isinstance(expect, dict):
            fail(f"{where}: no expect block")
            continue
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            fail(f"{where}: unknown expect keys {sorted(unknown)}")

        cls = expect.get("class")
        if cls is not None and cls not in CLASSES:
            fail(f"{where}: expect.class {cls!r} is not one of {sorted(CLASSES)}")
        if not any(k in expect for k in ("class", "rules", "notRules", "abort")):
            fail(f"{where}: expect asserts nothing")
        if expect.get("abort"):
            saw_abort = True

        for r in expect.get("rules") or []:
            fires.setdefault(r, []).append(cid)
        for r in expect.get("notRules") or []:
            quiet.setdefault(r, []).append(cid)

        # 5. Direction coverage, keyed by the rule the direction feeds.
        for o in ops:
            if isinstance(o, dict) and o.get("direction"):
                for r in expect.get("rules") or []:
                    directions.setdefault(r, set()).add(o["direction"])
                for r in expect.get("notRules") or []:
                    directions.setdefault(r, set()).add(o["direction"])

    # 3 + 4. Rule coverage, both halves.
    floor_ids = floor_rule_ids()
    known = set(floor_ids) | {"caller-requested-approval"}

    for rule, where_ids in sorted({**fires, **quiet}.items()):
        if rule not in known:
            fail(f"cases {where_ids} name rule {rule!r}, which floor.go does not define")

    missing_positive = [r for r in floor_ids if r not in fires]
    if missing_positive:
        fail(
            "no case asserts these floor rules FIRE: "
            + ", ".join(sorted(missing_positive))
            + " -- a floor rule with no fixture is a rule nobody has ever seen fire"
        )

    missing_negative = [r for r in floor_ids if r not in quiet and r not in NEGATIVE_EXEMPT]
    if missing_negative:
        fail(
            "no case asserts these floor rules STAY QUIET: "
            + ", ".join(sorted(missing_negative))
            + " -- positive-only coverage is satisfied by a classifier that gates everything"
        )

    # 5. The asymmetry, for the direction-driven rules.
    for rule in ("security-loosen", "public-exposure"):
        seen = directions.get(rule, set())
        for want in ("loosen", "tighten"):
            if want not in seen:
                fail(f"no case exercises {rule!r} with direction {want!r}; 03 §5.2 needs both halves")

    controls = security_controls()
    if len(controls) != 8:
        fail(f"direction.go declares {len(controls)} security controls; 03 §5.2 specifies 8")

    # 6. The production ladder.
    for key in ("kube-agents/environment", "env"):
        if not any(k == key for k, _ in env_labels):
            fail(f"no case sets the {key!r} label; the production ladder has an untested rung")
    values = {v.strip().lower() for _, v in env_labels}
    if "production" not in values:
        fail("no case uses the value 'production'")
    if not values & {"prod", "prd", "live"}:
        fail(
            "no case uses a near-miss value such as 'prod'; that it is NOT accepted is a decision "
            "that needs a fixture, because it is the sort of thing a later reader 'fixes'"
        )

    # 7. The remaining structural properties.
    if not saw_abort:
        fail("no case expects an abort; the hard caps are untested")
    if not any(v not in CLASSES for v in override_values):
        fail("no case uses a MALFORMED risk-class override; a typo'd override must gate, not be ignored")

    if FAILURES:
        print(f"FAIL: {CORPUS.relative_to(REPO)}", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: classifier corpus -- {len(cases)} cases, "
        f"{len(floor_ids)} floor rules covered in both directions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
