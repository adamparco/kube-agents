#!/usr/bin/env python3
"""A suite reads a line tag its probe cannot emit, gets "", and reports the property ABSENT.

This is [[B-008]] mechanized. It is an L0 check: it reads the source tree, offline, and needs no
cluster, no network and no probe run.

--------------------------------------------------------------------------------------------------
THE CONTRACT
--------------------------------------------------------------------------------------------------

A probe under `dev/verify/fixtures/*_probe.py` writes one JSON object per line on stdout. Every
object carries a `"scenario"` key, and that key is a **LINE TAG, not a scenario name** --
`nonce-accepted`, `shadow-submit`, `target`, `config`, `scenario-note`. `broker_refuse_probe.py`'s
`emit()` docstring states the whole of it:

    `tag` is the LINE TAG, not the scenario name: the suite's `field()` matches on it and takes the
    first hit, so two lines sharing a tag make one of them unreadable.

The consuming L2 suite flattens that transcript into TSV with the tag in column 1 and then pulls
individual fields out with a local helper:

    field() { printf '%s\\n' "$1" | awk -F'\\t' -v s="$2" -v i="$(($3 + 1))" '$1 == s { print $i; exit }'; }

    field "$A_FLAT" nonce-accepted 1

Two vocabularies, defined in two languages, in two files, with nothing joining them.

--------------------------------------------------------------------------------------------------
THE INCIDENT
--------------------------------------------------------------------------------------------------

`awk` prints nothing when no record matches, so a suite that asks for a tag the probe cannot emit
gets back an empty string -- exactly the same bytes it gets back when the probe ran, emitted the
line, and the field was genuinely empty. The arm downstream cannot tell those apart, so it reports
the property ABSENT. In `dev/verify/broker-refuse-l2.sh` that path ends here:

    DEFERRED: the door never opened for scenario B ($(field "$B_FLAT" nonce-accepted 9)).

which reads as a cluster problem -- the broker refused the nonce, the driver pod never came up, the
endpoint moved -- and is, in the failure this file exists for, a typo. A misspelled tag produces a
transcript-shaped alibi for a check that never asked its question. Nothing in the tree asserted the
two vocabularies agree; the negative control could not see the contract at all, because both halves
of it are correct on their own and only the JOIN is wrong.

--------------------------------------------------------------------------------------------------
THE TWO DIRECTIONS ARE NOT THE SAME DEFECT, AND THIS CHECK DOES NOT PRETEND THEY ARE
--------------------------------------------------------------------------------------------------

**reads not-subset-of emits is a FAILURE.** It produces a WRONG VERDICT: an absent field read as an
absent property, a green or a deferral over a question nobody asked. That is the defect above and it
is the only thing here that fails a build.

**emits not-subset-of reads is REPORTED, under its own heading, and does not by itself fail.** A tag
the probe emits and no suite reads costs one extra line of transcript and nothing else. Several
probes emit deliberate diagnostics that no suite is obliged to consume -- `scenario-note` exists so a
human can tell two runs apart when reading them back, `config` and the `probe-error` shapes exist so
a probe that died before the door can say why. Treating the two directions identically would put
those lines on a collision course with the check the first time one of them is legitimately
unconsumed, and the cheapest way to make such a check green is to delete the diagnostic. A check
that gets weakened is worse than a check that was narrower from the start, so the reporting
direction is advisory by construction rather than by exemption list.

The asymmetry is affordable because of what it does NOT let through. A **renamed** tag -- the probe
emits `nonce_accepted`, the suite reads `nonce-accepted` -- shows up in the failing direction
anyway: the read has no emit to match. Every rename, every typo, every drift that can change a
verdict lands on the strict side. What the advisory side holds is exactly the residue that cannot:
lines written and not consumed.

--------------------------------------------------------------------------------------------------
EVERYTHING IS DERIVED. NOTHING IS ENUMERATED ([[LSN-036]])
--------------------------------------------------------------------------------------------------

There is no list of pairs here, no list of probes, no list of accessor function names, and no list
of tags. A probe/suite pair added tomorrow is scored tomorrow, with no edit to this file.

  * **PAIRS.** Every `dev/verify/*.sh` is scanned for references to `dev/verify/fixtures/*.py`. A
    suite may drive several probes (`undo-coverage-l2.sh` drives two), so the emit vocabulary
    compared against a suite's reads is the UNION over the probes that suite drives.

    One probe is reached indirectly and the naive grep misses it: `broker_probe.py` appears in no
    suite, because `dev/lib/broker-driver.sh` holds it as the DEFAULT value of
    `BROKER_DRIVER_PROBE` and six of the seven suites that source the driver call
    `broker_driver_use_probe` to replace it. `broker-auth-l2.sh` is the one that does not, and it is
    the largest pair in the tree. That is resolved structurally rather than by naming the variable:
    a library's top-level assignment of a fixture path binds a default; a function in that library
    that reassigns the same variable is a rebinder; and a suite that sources the library inherits
    the default unless it calls a rebinder or assigns the variable itself.

  * **THE EMIT SET.** The probe is read as a Python AST. An emit helper is a function that writes a
    JSON object carrying a `"scenario"` key whose value is one of the function's own parameters --
    that is the output contract expressed as a shape, and it recognises `emit()` in all four probes
    without knowing the name. Helpers are closed transitively: a function that forwards one of its
    parameters into a known helper's tag slot is itself a helper, which is how `emit_reply()` and
    `broker_probe.py`'s `raw_get` / `raw_request` are found. The emit set is then the literal first
    argument at every call site of every helper.

    A tag built from an expression rather than a literal is **not an unknown to be ignored**. An
    emit set that could not be fully enumerated makes the comparison PARTIAL, and a partial
    comparison reported as complete is the shape of a false green -- so unenumerable tags are
    counted, printed with their line numbers, and the pair's verdict is labelled `pass (partial)`
    rather than `pass`. Two kinds are distinguished, because they are not equally opaque: an
    f-string with a literal head (`f"route:{path}"`) contributes a PREFIX, against which a read tag
    can still be matched exactly; anything else (`emit(cid, ...)`) contributes nothing and is
    reported as unresolved.

  * **THE ACCESSOR.** The suite is read after comments and heredoc bodies are removed, quote-aware.
    An accessor is a shell function whose body selects transcript lines keyed on one of its own
    positional parameters -- concretely, an `awk` invocation binding `-v <var>="$N"` and comparing
    the tag column `$1` against `<var>` for EQUALITY. Equality is the contract ("matches on it and
    takes the first hit"); a prefix selector such as `broker-auth-l2.sh`'s `scenarios_with_prefix`
    is a different instrument and is deliberately not one of these. The tag's POSITION is derived
    too, and it is not the same everywhere: `broker-execute-l2.sh` spells it `field <tag> <col>`
    while `broker-refuse-l2.sh` spells it `field <flat> <tag> <col>`.

    Accessors are closed transitively here as well, through shell's own indirection: `expect_http()`
    binds `local s="$1"` and calls `seen "$s"`, so it is an accessor at position 1 and its call
    sites carry the literal tags. The read set is the literal tag argument at every call site of
    every accessor, forwarding call sites inside other accessors excluded because they carry a
    parameter and not a literal.

--------------------------------------------------------------------------------------------------
NON-VACUITY: `VACUOUS:` IS A FAILURE HERE ([[LSN-035]], [[LSN-038]])
--------------------------------------------------------------------------------------------------

Every recogniser above is a filter, and the failure mode of a filter is silence. A structural
recogniser that stops matching does not report that it stopped; it reports that nothing is wrong. So
the floors are failures and not skips:

  * zero pairs discovered at all;
  * zero pairs PARTICIPATING -- every suite's accessor discovery came up empty. That is the shell
    recogniser having broken, not a clean tree; a tree with no tag-reading suites in it would also
    have no probes;
  * fewer participating pairs than `MIN_PARTICIPATING_PAIRS`, which is the same argument stated
    against the tree's actual size rather than against zero;
  * a participating pair whose emit set is empty, or whose read set is empty;
  * a participating pair whose emit set is ENTIRELY unread -- the join is mis-wired, or one of the
    two extractors died holding a plausible-looking half.

And where a pair does not participate, it SAYS SO, by name, on its own line: `no tag accessor found`
or `no emit helper found`. Silence there is indistinguishable from a pass, which is the family of
defect this whole file belongs to.

Run:  python3 dev/tests/probe-tags-match-their-suite.py
      python3 dev/tests/probe-tags-match-their-suite.py --negative-control
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# ------------------------------------------------------------------------------------------------
# Where the tree is
# ------------------------------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[2]
DEV = REPO / "dev"

# The three roles a directory plays, relative to whatever root is being scanned. Parameterised on
# the root rather than on absolute paths so `--negative-control` can score synthetic trees with the
# same code the real tree gets ([[LSN-038]]: an instrument the control cannot reach is an instrument
# nothing has established works).
VERIFY_DIR = "verify"
FIXTURES_DIR = "verify/fixtures"
LIB_DIR = "lib"

# The tag column. Every flattener in the tree puts `scenario` first, which is what makes column 1
# "the tag column"; an accessor keyed on any other column is selecting by something that is not a
# tag and is not one of these.
TAG_COLUMN = 1

# The absolute floor on participation, stated against the tree's size rather than against zero.
#
# The unit of comparison is a SUITE together with the probes it drives, because a suite may drive
# several (`undo-coverage-l2.sh` drives two) and a tag it reads may legitimately come from any of
# them. Five participate today: brake, broker-auth, broker-execute, broker-gate, broker-refuse. The
# floor is set below that on purpose -- this is the "a recogniser stopped matching" alarm, not a
# coverage ratchet, and a floor equal to today's count turns every legitimate deletion into a red.
MIN_PARTICIPATING_PAIRS = 4


# ------------------------------------------------------------------------------------------------
# Shell: reading a suite without being fooled by its own prose
# ------------------------------------------------------------------------------------------------


def _sh_code(text: str) -> str:
    """The script with comments and heredoc bodies blanked, quote state carried across lines.

    This matters more here than it looks. Every accessor in the tree is documented by a comment
    directly above it that spells out the calling convention --

        # field <flat> <tag> <1=outcome 2=status ...>

    -- and a textual scan reads that as a call site with the literal tag `<tag>`. The read set would
    then contain a tag no probe can emit, and this check would report a failure it invented.
    """
    out: list[str] = []
    quote: str | None = None
    heredoc: str | None = None
    for line in text.splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            out.append("")
            continue
        buf: list[str] = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and quote != "'" and i + 1 < len(line):
                buf.append(line[i + 1])
                i += 2
                continue
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf.append(ch)
            elif ch == "#" and (not buf or buf[-1] in " \t;&|(<"):
                break
            else:
                buf.append(ch)
            i += 1
        clean = "".join(buf)
        opener = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", clean)
        if opener is not None and "<<<" not in clean:
            heredoc = opener.group(2)
        out.append(clean)
    return "\n".join(out)


_FUNC_OPEN = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][A-Za-z0-9_:.-]*)\s*\(\)\s*\{")


def _sh_functions(code: str) -> dict[str, list[tuple[int, int, str]]]:
    """Every `name() { ... }` in the script, as (first line, last line, body), 1-based inclusive.

    The terminator is a line whose whole content is `}` at an indentation no deeper than the
    definition's, or -- for the one-liner form `seen() { ...; }` -- the definition line itself.
    Counting braces would be the general answer and is the wrong one here: half the bodies in these
    suites are `awk` programs, whose braces are shell-quoted, plus `${...}` expansions and YAML
    heredocs, and a brace counter walks straight out of the function it is reading.
    """
    lines = code.splitlines()
    found: dict[str, list[tuple[int, int, str]]] = {}
    i = 0
    while i < len(lines):
        m = _FUNC_OPEN.match(lines[i])
        if m is None:
            i += 1
            continue
        indent, name = m.group(1), m.group(2)
        if lines[i].rstrip().endswith("}"):
            body, end = lines[i], i
        else:
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped in ("}", "};") and len(lines[j]) - len(lines[j].lstrip()) <= len(indent):
                    break
                j += 1
            body, end = "\n".join(lines[i : j + 1]), min(j, len(lines) - 1)
        found.setdefault(name, []).append((i + 1, end + 1, body))
        i = end + 1
    return found


_AWK_BIND = re.compile(r"-v\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"?\$\{?(\d)\}?\"?")


def _keyed_on_positional(body: str) -> int | None:
    """The positional parameter this body selects transcript lines by, if it does.

    The shape: `awk -v s="$N" '$1 == s ...'`. Both orders of the comparison are accepted, since
    `s == $1` is the same selection written the other way round.
    """
    for var, pos in _AWK_BIND.findall(body):
        col = re.escape(f"${TAG_COLUMN}")
        if re.search(rf"{col}\s*==\s*{var}\b", body) or re.search(rf"\b{var}\s*==\s*{col}", body):
            return int(pos)
    return None


def _positional_aliases(body: str, pos: int) -> set[str]:
    """Every variable name in this body that carries positional parameter `pos`.

    `local s="$1"` is the tree's idiom and `expect_http` is unreadable without it: the function
    never mentions `$1` again after the first line, so a scan for `seen "$1"` finds nothing while
    `seen "$s"` is right there. Iterated to a fixpoint so a second hop (`local s="$1"; t="$s"`) is
    carried too.
    """
    names = {f"{pos}"}
    for _ in range(4):
        before = len(names)
        for lhs, rhs in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=\"?\$\{?([A-Za-z_0-9]+)\}?\"?", body):
            if rhs in names:
                names.add(lhs)
        if len(names) == before:
            break
    return names


def _call_sites(code: str, name: str) -> list[tuple[int, list[str]]]:
    """Every place `name` is invoked as a command, with its argument words.

    The command ends at the first `)`, `|`, `;`, `&`, `<`, `>` or newline -- enough to lift the
    arguments out of `"$(field shadow-submit 1)"` without a shell parser, and comments are already
    gone by the time this runs.
    """
    sites: list[tuple[int, list[str]]] = []
    for m in re.finditer(rf"(?:^|[\s;|&(`]|\$\(){re.escape(name)}(?=[ \t])", code):
        rest = re.split(r"[)\n;|&<>]", code[m.end() :])[0]
        words = rest.split()
        if words:
            sites.append((code.count("\n", 0, m.start()) + 1, words))
    return sites


def _accessors(code: str) -> dict[str, int]:
    """Tag-reading helpers defined in this script, name -> 1-based tag argument position.

    Two rounds of the same idea. Round one is direct: the function's own body does the `awk`
    selection. Round two is transitive: the function hands one of its positional parameters to a
    function round one already recognised, which is how a suite's `expect_*` wrappers -- the layer
    that actually carries the literal tags -- become accessors.
    """
    functions = _sh_functions(code)
    accessors: dict[str, int] = {}
    for name, definitions in functions.items():
        for _, _, body in definitions:
            pos = _keyed_on_positional(body)
            if pos is not None:
                accessors[name] = pos
    for _ in range(4):
        before = len(accessors)
        for name, definitions in functions.items():
            if name in accessors:
                continue
            for _, _, body in definitions:
                for callee, callee_pos in list(accessors.items()):
                    for _, words in _call_sites(body, callee):
                        if len(words) < callee_pos:
                            continue
                        arg = words[callee_pos - 1].strip("\"'")
                        ref = re.fullmatch(r"\$\{?([A-Za-z_0-9]+)\}?", arg)
                        if ref is None:
                            continue
                        for candidate in range(1, 10):
                            if ref.group(1) in _positional_aliases(body, candidate):
                                accessors[name] = candidate
                                break
                        if name in accessors:
                            break
                    if name in accessors:
                        break
                if name in accessors:
                    break
        if len(accessors) == before:
            break
    return accessors


def _read_tags(code: str, accessors: dict[str, int]) -> tuple[set[str], list[str]]:
    """The literal tags this script asks for, and the call sites whose tag is computed.

    A non-literal tag inside an accessor's OWN body is a forwarding call, not an unenumerable read:
    `expect_http()` exists precisely to hand its `$1` to `seen` and `field`, and its twenty-four
    `field "$s"` sites are one read apiece at ITS call sites, which are literal. Counting them as
    unknowns would label a fully enumerated comparison partial, which is the same lie as labelling a
    partial one complete -- in the direction that makes a real gap harder to see.
    """
    functions = _sh_functions(code)
    spans = [(start, end) for name in accessors for start, end, _ in functions.get(name, [])]
    literal: set[str] = set()
    computed: list[str] = []
    for name, pos in accessors.items():
        for line, words in _call_sites(code, name):
            if len(words) < pos:
                continue
            raw = words[pos - 1]
            arg = raw.strip("\"'")
            if "$" in arg or "`" in arg or not arg:
                if any(start <= line <= end for start, end in spans):
                    continue
                computed.append(f"{name}() at line {line}: {raw}")
            else:
                literal.add(arg)
    return literal, computed


# ------------------------------------------------------------------------------------------------
# Python: reading a probe's emit vocabulary out of its own shape
# ------------------------------------------------------------------------------------------------


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], list[str]]:
    """(positional parameter names, all parameter names)."""
    positional = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
    return positional, [*positional, *[a.arg for a in fn.args.kwonlyargs]]


def _tag_argument(call: ast.Call, slot: tuple[int | None, str]) -> ast.expr | None:
    """The expression in a helper call's tag slot, positionally or by keyword."""
    index, keyword = slot
    arg: ast.expr | None = None
    if index is not None and len(call.args) > index:
        arg = call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            arg = kw.value
    return arg


def _emit_helpers(tree: ast.Module) -> dict[str, tuple[int | None, str]]:
    """Functions that put a line on the transcript, name -> (tag position, tag parameter name).

    Direct: the function writes something AND builds a dict whose `"scenario"` key is one of its own
    parameters. Both halves are required -- a dict with that key that never reaches stdout is a
    fixture, not an emitter. Transitive: the function forwards a parameter into a known helper's tag
    slot, which is `emit_reply()` in three probes and `raw_get` / `raw_request` in a fourth.
    """
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helpers: dict[str, tuple[int | None, str]] = {}

    for name, fn in functions.items():
        writes = any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in ("dumps", "dump", "write"))
                or (isinstance(n.func, ast.Name) and n.func.id == "print")
            )
            for n in ast.walk(fn)
        )
        if not writes:
            continue
        positional, every = _params(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "scenario"
                    and isinstance(value, ast.Name)
                    and value.id in every
                ):
                    index = positional.index(value.id) if value.id in positional else None
                    helpers[name] = (index, value.id)

    for _ in range(4):
        before = len(helpers)
        for name, fn in functions.items():
            if name in helpers:
                continue
            positional, _ = _params(fn)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id not in helpers:
                    continue
                arg = _tag_argument(node, helpers[node.func.id])
                if isinstance(arg, ast.Name) and arg.id in positional:
                    helpers[name] = (positional.index(arg.id), arg.id)
        if len(helpers) == before:
            break
    return helpers


def _enclosing(tree: ast.Module) -> dict[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Nearest enclosing function for every node, so a Name can be read in its own scope.

    The first draft resolved a tag Name against the CALLEE's parameters, which is the wrong scope
    and silently the most permissive one available: every probe's helper happens to call its tag
    parameter `scenario`, so `emit(scenario, ...)` was skipped as a forwarding call wherever it
    appeared -- including in `broker_probe.probe_port`, where `scenario` is a LOCAL holding
    `f"port:{port}"`. Four `port:` emissions vanished from the emit set and the pair still reported
    itself fully enumerated.
    """
    parent: dict[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if node is not fn:
                parent[node] = fn
    return parent


def _bindings(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """Every simple `name = <expr>` in this scope, name -> the expressions bound to it."""
    bound: dict[str, list[ast.expr]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            target = node.target
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).append(node.value)
    return bound


def _emit_tags(
    tree: ast.Module, helpers: dict[str, tuple[int | None, str]]
) -> tuple[set[str], set[str], list[str]]:
    """(literal tags, literal prefixes, unresolved tag expressions).

    A prefix comes from an f-string with a literal head -- `f"route:{path}"` in `broker_probe.py`
    means the emit set holds an open family whose members all start `route:`, and a read of
    `route:/healthz` is genuinely satisfied by it. A tag held in a local first
    (`scenario = f"port:{port}"; emit(scenario, ...)`) is resolved through its binding, because a
    variable that can be followed is not an unknown. Anything left over is unresolved and makes the
    pair's comparison PARTIAL rather than complete -- counted, printed, and reflected in the pair's
    verdict, never dropped.
    """
    parent = _enclosing(tree)
    module_bindings = _bindings(tree)
    literal: set[str] = set()
    prefixes: set[str] = set()
    unresolved: list[str] = []

    def classify(node: ast.expr | None, scope: ast.AST | None, depth: int) -> bool:
        if node is None or depth > 3:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal.add(node.value)
            return True
        if (
            isinstance(node, ast.JoinedStr)
            and node.values
            and isinstance(node.values[0], ast.Constant)
            and isinstance(node.values[0].value, str)
        ):
            prefixes.add(node.values[0].value)
            return True
        if isinstance(node, ast.Name):
            bound = (_bindings(scope) if scope is not None else {}).get(node.id) or module_bindings.get(node.id)
            if bound:
                return all(classify(value, scope, depth + 1) for value in bound)
        return False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in helpers:
            continue
        arg = _tag_argument(node, helpers[node.func.id])
        if arg is None:
            continue
        scope = parent.get(node)
        # A helper forwarding its OWN tag parameter into another helper carries a parameter and not
        # a tag; the literal lives at that helper's call sites and is collected there.
        if (
            isinstance(arg, ast.Name)
            and scope is not None
            and scope.name in helpers
            and arg.id in _params(scope)[1]
        ):
            continue
        if not classify(arg, scope, 0):
            unresolved.append(f"{node.func.id}(...) at line {node.lineno}")
    return literal, prefixes, unresolved


# ------------------------------------------------------------------------------------------------
# Pair discovery: which suite drives which probe, including the one reached through a library
# ------------------------------------------------------------------------------------------------

_FIXTURE_REF = re.compile(r"fixtures/([A-Za-z0-9_]+\.py)")


def _library_defaults(lib_code: str) -> tuple[dict[str, str], set[str]]:
    """(variable -> default probe file, functions that reassign one of those variables).

    `dev/lib/broker-driver.sh` sets `BROKER_DRIVER_PROBE="dev/verify/fixtures/broker_probe.py"` at
    the top and offers `broker_driver_use_probe` to replace it. Neither the variable's name nor the
    setter's is written down here: the binding is "a top-level assignment of a fixture path" and the
    rebinder is "a function that assigns that same variable".
    """
    defaults: dict[str, str] = {}
    for var, probe in re.findall(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)=\"?[^\s\"]*fixtures/([A-Za-z0-9_]+\.py)\"?",
        lib_code,
        re.M,
    ):
        defaults[var] = probe
    rebinders = {
        name
        for name, definitions in _sh_functions(lib_code).items()
        for _, _, body in definitions
        if any(re.search(rf"\b{re.escape(var)}=", body) for var in defaults)
    }
    return defaults, rebinders


def _probes_for(suite: pathlib.Path, code: str, libraries: dict[pathlib.Path, str]) -> set[str]:
    """Every probe file this suite drives: named directly, or inherited from a sourced library."""
    probes = set(_FIXTURE_REF.findall(code))
    for lib, lib_code in libraries.items():
        if not re.search(rf"^\s*(?:\.|source)\s+.*{re.escape(lib.name)}", code, re.M):
            continue
        defaults, rebinders = _library_defaults(lib_code)
        overridden = any(
            re.search(rf"^\s*{re.escape(var)}=", code, re.M) for var in defaults
        ) or any(_call_sites(code, fn) for fn in rebinders)
        if not overridden:
            probes |= set(defaults.values())
    return probes


# ------------------------------------------------------------------------------------------------
# The scan
# ------------------------------------------------------------------------------------------------


class Pair:
    """One suite/probe edge, with both vocabularies and the reason it does or does not participate."""

    def __init__(self, suite: str, probe: str) -> None:
        self.suite = suite
        self.probe = probe
        self.emits: set[str] = set()
        self.prefixes: set[str] = set()
        self.unresolved: list[str] = []
        self.probe_missing = False
        self.no_emit_helper = False


class SuiteReport:
    def __init__(self, suite: str) -> None:
        self.suite = suite
        self.pairs: list[Pair] = []
        self.accessors: dict[str, int] = {}
        self.reads: set[str] = set()
        self.computed_reads: list[str] = []

    @property
    def emits(self) -> set[str]:
        return set().union(*[p.emits for p in self.pairs]) if self.pairs else set()

    @property
    def prefixes(self) -> set[str]:
        return set().union(*[p.prefixes for p in self.pairs]) if self.pairs else set()

    @property
    def unresolved(self) -> list[str]:
        return sorted(f"{p.probe}: {u}" for p in self.pairs for u in p.unresolved)

    @property
    def has_emitter(self) -> bool:
        return any(not p.no_emit_helper and not p.probe_missing for p in self.pairs)

    @property
    def participates(self) -> bool:
        return bool(self.accessors) and self.has_emitter

    def unmatched_reads(self) -> set[str]:
        return {t for t in self.reads if t not in self.emits and not any(t.startswith(p) for p in self.prefixes)}

    def unread_emits(self) -> set[str]:
        return self.emits - self.reads


def scan(dev_root: pathlib.Path) -> list[SuiteReport]:
    """Every suite under `<dev_root>/verify`, with the probes it drives and the tags it reads."""
    verify = dev_root / VERIFY_DIR
    fixtures = dev_root / FIXTURES_DIR
    lib = dev_root / LIB_DIR
    libraries = {p: _sh_code(p.read_text()) for p in sorted(lib.glob("*.sh"))} if lib.is_dir() else {}

    reports: list[SuiteReport] = []
    for suite in sorted(verify.glob("*.sh")):
        code = _sh_code(suite.read_text())
        probes = _probes_for(suite, code, libraries)
        if not probes:
            continue
        report = SuiteReport(suite.name)

        # Accessors may be defined in the suite or in a library it sources; the CALL SITES that
        # carry literal tags are the suite's own, since a library's calls belong to whichever suite
        # sourced it and cannot be attributed to one.
        accessor_code = code
        for library, lib_code in libraries.items():
            if re.search(rf"^\s*(?:\.|source)\s+.*{re.escape(library.name)}", code, re.M):
                accessor_code += "\n" + lib_code
        report.accessors = _accessors(accessor_code)
        report.reads, report.computed_reads = _read_tags(code, report.accessors)

        for probe_name in sorted(probes):
            pair = Pair(suite.name, probe_name)
            path = fixtures / probe_name
            if not path.is_file():
                pair.probe_missing = True
            else:
                try:
                    tree = ast.parse(path.read_text())
                except SyntaxError:
                    pair.probe_missing = True
                else:
                    helpers = _emit_helpers(tree)
                    if not helpers:
                        pair.no_emit_helper = True
                    else:
                        pair.emits, pair.prefixes, pair.unresolved = _emit_tags(tree, helpers)
            report.pairs.append(pair)
        reports.append(report)
    return reports


# ------------------------------------------------------------------------------------------------
# The judgement
# ------------------------------------------------------------------------------------------------


def check(
    dev_root: pathlib.Path = DEV,
    min_participating: int = MIN_PARTICIPATING_PAIRS,
) -> list[str]:
    """Findings. Only the failing direction and the non-vacuity floors are in here.

    `emits not-subset-of reads` is deliberately absent: it is printed by `report()` under its own
    heading and is advisory. See the module docstring for why the two directions are not the same
    defect.
    """
    reports = scan(dev_root)
    out: list[str] = []

    pairs = [p for r in reports for p in r.pairs]
    if not pairs:
        return [
            "VACUOUS: no suite under dev/verify names a probe under dev/verify/fixtures, so this "
            "check compared zero vocabularies. The pair discovery broke before the property was "
            "ever asked (LSN-035)."
        ]

    participating = [r for r in reports if r.participates]
    if not participating:
        return [
            f"VACUOUS: {len(pairs)} suite/probe pair(s) were discovered and NOT ONE of them "
            f"participates -- every suite's tag-accessor discovery came up empty. A tree of probes "
            f"with no suite reading them by tag is not a state this repository can be in, so this "
            f"is the shell recogniser having broken (LSN-036, LSN-038)."
        ]
    if len(participating) < min_participating:
        out.append(
            f"VACUOUS: {len(participating)} suite(s) participate, over {len(pairs)} discovered "
            f"suite/probe pair(s), and the floor is {min_participating}. The structural recognisers "
            f"have stopped matching most of the tree, and a comparison over what is left is a green "
            f"produced by not asking (LSN-035)."
        )

    for report in participating:
        if not report.emits and not report.prefixes:
            out.append(
                f"VACUOUS: {report.suite} reads tags by {_accessor_shape(report)} but the "
                f"probe(s) it drives ({', '.join(p.probe for p in report.pairs)}) yielded an EMPTY "
                f"emit set. The emit extractor found a helper and then no call site -- so nothing "
                f"was compared, and every read tag would have been reported as unmatched."
            )
            continue
        if not report.reads:
            out.append(
                f"VACUOUS: {report.suite} defines {_accessor_shape(report)} and calls it with no "
                f"literal tag anywhere. Either the accessor is dead, or the call-site extractor is "
                f"reading the wrong argument -- both leave the pair scored without asking anything."
            )
            continue
        if not (report.emits & report.reads) and not report.prefixes:
            out.append(
                f"VACUOUS: {report.suite} reads {len(report.reads)} tag(s) and its probe(s) emit "
                f"{len(report.emits)}, and the two sets do not INTERSECT AT ALL. That is not drift "
                f"in one tag; the pair is mis-wired, or one of the two extractors returned a "
                f"plausible-looking set from the wrong place."
            )

        for tag in sorted(report.unmatched_reads()):
            partial = (
                " (and note this pair's emit set is PARTIAL -- see the unresolved tags above -- so "
                "the tag may be one this check could not enumerate)"
                if report.unresolved
                else ""
            )
            out.append(
                f"{report.suite} reads the line tag `{tag}`, which none of the probe(s) it drives "
                f"({', '.join(p.probe for p in report.pairs)}) can emit. `awk` prints nothing for a "
                f"tag with no record, so the arm reading it gets \"\" and reports the property "
                f"ABSENT -- a wrong verdict dressed as a cluster problem{partial}."
            )
    return out


def _accessor_shape(report: SuiteReport) -> str:
    return ", ".join(f"{name}(tag@{pos})" for name, pos in sorted(report.accessors.items())) or "-"


# ------------------------------------------------------------------------------------------------
# The report: one row per pair, one line per offending tag. A single OK is not evidence.
# ------------------------------------------------------------------------------------------------


def report(reports: list[SuiteReport], findings: list[str]) -> list[str]:
    lines: list[str] = []
    offenders = {r.suite: r.unmatched_reads() for r in reports}

    lines.append("")
    lines.append("== the line-tag vocabularies, one row per suite/probe pair ==")
    lines.append(f"{'SUITE':<28} {'PROBE':<30} {'EMITS':>5} {'READS':>5}  VERDICT")
    for r in sorted(reports, key=lambda x: x.suite):
        for pair in r.pairs:
            if pair.probe_missing:
                verdict = "skip: probe not readable"
            elif pair.no_emit_helper:
                verdict = "skip: no emit helper found -- not a line-tag probe"
            elif not r.accessors:
                verdict = "skip: no tag accessor found -- this pair does not participate"
            elif offenders[r.suite]:
                verdict = f"FAIL: {len(offenders[r.suite])} read tag(s) unemittable"
            elif pair.unresolved:
                verdict = "pass (partial)"
            else:
                verdict = "pass"
            emits = "-" if pair.no_emit_helper or pair.probe_missing else str(len(pair.emits))
            reads = str(len(r.reads)) if r.accessors else "-"
            lines.append(f"{r.suite:<28} {pair.probe:<30} {emits:>5} {reads:>5}  {verdict}")
        if r.accessors:
            lines.append(f"{'':<28} reader: {_accessor_shape(r)}")

    # NON-PARTICIPATION, SAID OUT LOUD. Silence here is indistinguishable from a pass.
    quiet = [r for r in reports if not r.participates]
    if quiet:
        lines.append("")
        lines.append("== pairs that do not participate (named, because silence reads as a pass) ==")
        for r in sorted(quiet, key=lambda x: x.suite):
            why = (
                "no tag accessor found -- this pair does not participate"
                if not r.accessors
                else "no emit helper found in any probe it drives -- not a line-tag pair"
            )
            lines.append(f"  {r.suite}: {why} ({', '.join(p.probe for p in r.pairs)})")

    # PARTIAL ENUMERATION. Counted and reported, never quietly dropped: an emit set that could not
    # be fully enumerated makes the comparison partial, and a partial comparison reported as
    # complete is the shape of a false green.
    partial = [r for r in reports if r.unresolved or r.computed_reads]
    if partial:
        lines.append("")
        lines.append("== tags this check could not enumerate (the comparison is PARTIAL here) ==")
        for r in sorted(partial, key=lambda x: x.suite):
            mark = "" if r.participates else "  [pair does not participate]"
            for u in r.unresolved:
                lines.append(f"  {r.suite}: emit tag built from an expression -- {u}{mark}")
            for c in r.computed_reads:
                lines.append(f"  {r.suite}: read tag built from a variable -- {c}{mark}")
            if r.prefixes:
                lines.append(
                    f"  {r.suite}: resolvable emit prefixes: {', '.join(sorted(r.prefixes))}"
                )
        lines.append(
            f"  ({sum(len(r.unresolved) for r in partial)} unenumerable emit site(s), "
            f"{sum(len(r.computed_reads) for r in partial)} computed read site(s). A pair with any "
            f"of the former is scored `pass (partial)`, never `pass`.)"
        )

    # THE ADVISORY DIRECTION. Reported, never fatal -- see the module docstring.
    unread = [(r.suite, sorted(r.unread_emits())) for r in reports if r.participates and r.unread_emits()]
    if unread:
        lines.append("")
        lines.append("== ADVISORY: tags a probe emits and no suite reads (not a failure) ==")
        lines.append(
            "   A tag nobody reads costs one line of transcript. Several are deliberate "
            "diagnostics. This heading exists so drift is visible, not so it is punished."
        )
        for suite, tags in unread:
            for tag in tags:
                lines.append(f"  {suite}: nothing reads `{tag}`")

    if findings:
        lines.append("")
        lines.append("== FAILURES ==")
        for f in findings:
            lines.append(f"  {f}")
    return lines


# ------------------------------------------------------------------------------------------------
# The negative control
# ------------------------------------------------------------------------------------------------

_CLEAN_PROBE = '''
import json
def emit(tag, *, outcome="note", detail=""):
    print(json.dumps({"scenario": tag, "outcome": outcome, "detail": detail}))
def emit_reply(tag, reply):
    emit(tag, outcome="http", detail=str(reply))
def main():
    emit("config")
    emit("nonce-accepted")
    emit_reply("submit", {})
'''

_CLEAN_SUITE = """#!/usr/bin/env bash
# a suite driving dev/verify/fixtures/clean_probe.py
FLAT="$(python3 fixtures/clean_probe.py)"
# field <tag> <col>
field() {
  printf '%s\\n' "$FLAT" | awk -F'\\t' -v s="$1" -v i="$(($2 + 1))" '$1 == s { print $i; exit }'
}
echo "$(field config 1)"
echo "$(field nonce-accepted 1)"
echo "$(field submit 1)"
"""


def _tree(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def negative_control() -> int:
    """Score six synthetic probe/suite trees of known quality. Every row names the rule it exercises.

    The rows that must be REJECTED assert a NEEDLE naming their property rather than merely "some
    finding appeared" -- a control that only asks whether the list is non-empty cannot tell the rule
    it targets from a broader one that fired first ([[LSN-035]], and
    `dev/tests/negative-controls-name-their-rule.py` enforces the shape).
    """
    import tempfile

    clean = {
        f"{FIXTURES_DIR}/clean_probe.py": _CLEAN_PROBE,
        f"{VERIFY_DIR}/clean-l2.sh": _CLEAN_SUITE,
    }

    def variant(**edits: dict[str, str]) -> dict[str, str]:
        merged = dict(clean)
        for group in edits.values():
            merged.update(group)
        return merged

    # (label naming the RULE, files, min_participating, expect_rejected, needle, report needle)
    cases: list[tuple[str, dict[str, str], int, bool, str, str]] = [
        (
            "RULE reads-subset-of-emits: a pair whose vocabularies agree is ACCEPTED",
            clean,
            1,
            False,
            "",
            "clean-l2.sh",
        ),
        (
            "RULE reads-subset-of-emits: a tag the suite reads and the probe cannot emit is a "
            "WRONG VERDICT, so it is REJECTED",
            variant(
                suite={
                    f"{VERIFY_DIR}/clean-l2.sh": _CLEAN_SUITE.replace(
                        "field nonce-accepted 1", "field door-opened 1"
                    )
                }
            ),
            1,
            True,
            "`door-opened`",
            "",
        ),
        (
            "RULE reads-subset-of-emits is spelling-exact: a tag differing only by `-` vs `_` is a "
            "rename, and a rename is REJECTED by the failing direction",
            variant(
                suite={
                    f"{VERIFY_DIR}/clean-l2.sh": _CLEAN_SUITE.replace(
                        "field nonce-accepted 1", "field nonce_accepted 1"
                    )
                }
            ),
            1,
            True,
            "`nonce_accepted`",
            "",
        ),
        (
            "RULE emits-not-subset-of-reads is ADVISORY: a tag the probe emits and nobody reads is "
            "ACCEPTED, and appears under the advisory heading",
            variant(
                probe={
                    f"{FIXTURES_DIR}/clean_probe.py": _CLEAN_PROBE.replace(
                        'emit("config")', 'emit("config")\n    emit("scenario-note")'
                    )
                }
            ),
            1,
            False,
            "",
            "nothing reads `scenario-note`",
        ),
        (
            "RULE non-participation is ANNOUNCED, not silently accepted: a suite with no tag "
            "accessor is named on its own line while the clean pair keeps the corpus non-vacuous",
            variant(
                extra={
                    f"{FIXTURES_DIR}/mute_probe.py": _CLEAN_PROBE,
                    f"{VERIFY_DIR}/mute-l2.sh": (
                        "#!/usr/bin/env bash\n"
                        "OUT=\"$(python3 fixtures/mute_probe.py)\"\n"
                        'grep -q submit <<<"$OUT"\n'
                    ),
                }
            ),
            1,
            False,
            "",
            "mute-l2.sh: no tag accessor found",
        ),
        (
            "RULE non-vacuity floor: a participating pair whose probe emits nothing is REJECTED, "
            "because an empty comparison is the most convincing green in the repository",
            variant(
                probe={
                    f"{FIXTURES_DIR}/clean_probe.py": _CLEAN_PROBE.replace(
                        '    emit("config")\n    emit("nonce-accepted")\n    emit_reply("submit", {})\n',
                        "    pass\n",
                    )
                }
            ),
            1,
            True,
            "VACUOUS",
            "",
        ),
    ]

    wrong: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (label, files, floor, expect_rejected, needle, report_needle) in enumerate(cases):
            root = _tree(pathlib.Path(tmp) / f"case{i}", files)
            findings = check(root, min_participating=floor)
            printed = "\n".join(report(scan(root), findings))
            got_rejected = bool(findings)
            verdict = "REJECTED" if got_rejected else "ACCEPTED"
            want = "REJECTED" if expect_rejected else "ACCEPTED"
            ok = got_rejected == expect_rejected
            if ok and needle:
                ok = any(needle in f for f in findings)
                if not ok:
                    verdict += f" (but no finding named {needle})"
            if ok and report_needle:
                ok = report_needle in printed
                if not ok:
                    verdict += f" (but the report never said {report_needle!r})"
            print(f"  {'ok  ' if ok else 'MISS'} want {want:<8} got {verdict:<12} -- {label}")
            if not ok:
                wrong.append(label)

    if wrong:
        print(
            f"FAIL: probe-tags-match-their-suite negative control: {len(wrong)}/{len(cases)} rows "
            f"scored wrong",
            file=sys.stderr,
        )
        return 1
    print(f"PASS: probe-tags-match-their-suite negative control -- all {len(cases)} rows scored correctly")
    return 0


# ------------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------------


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    findings = check()
    reports = scan(DEV)
    for line in report(reports, findings):
        print(line)

    participating = [r for r in reports if r.participates]
    if findings:
        print("", file=sys.stderr)
        print(
            f"FAIL: probe-tags-match-their-suite -- {len(findings)} finding(s) over "
            f"{len(participating)} participating pair(s) (B-008)",
            file=sys.stderr,
        )
        return 1
    print("")
    print(
        f"PASS: probe-tags-match-their-suite (L0) -- every tag read by a suite is a tag its "
        f"probe(s) can emit, across {len(participating)} participating suite(s) and "
        f"{sum(len(r.pairs) for r in reports)} suite/probe pair(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
