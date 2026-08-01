#!/usr/bin/env python3
"""A negative control that only proves the suite went red proves almost nothing (LSN-035).

This file is V-MET-014's implementation (09 §6.14, "negative-control discipline"), registered in
`verification/implementations.yaml`.

`¬` in 09 §6 means a negative control is mandatory: break the property, watch the check fail. What a
negative control establishes is that the check *can* fail. What it does not establish -- and what
everyone reads it as establishing -- is that the check fails **for the reason the mutation was
about**.

The ladder found this the expensive way. Eleven invariants in `ladder.go`, mutation-tested one at a
time; nine died, two survived. The two survivors were the two properties the check text named by
hand, and both had tests, and both tests passed. Each test built a history that some *broader* rule
rejected first, so the narrow rule under test never executed. A test that asserts "this input is
rejected" cannot tell you which rule did the rejecting. Within a chain-valid history those two rules
turned out to be logically unreachable -- dead code that read as a safety rail for three phases.

The general shape, which is what this file mechanizes: when several properties overlap, every
mutation lands on whichever one fires first, usually the broadest, and the narrow properties
underneath accumulate controls that have never once executed them. Nothing about that is visible
from a green run. `install-render-is-faithful.py` is the one check in this tree that got it right
from the start -- each of its breakages carries the property NUMBER it targets and asserts that
number appears in the failures -- and this file generalizes that convention to the rest.

THE PROPERTY. For every check advertising `--negative-control`: replace the probe's findings with a
single constant failure that names no property, and re-run the control. A control that asserts a
per-mutation signal now FAILS, because the constant matches no signal. A control that only asks "is
the failure list non-empty" still PASSES -- and that pass is the finding. The instrument is the
defect itself, injected deliberately.

THE BLINDING PRESERVES EMPTINESS. A non-empty result becomes one constant; an empty result stays
empty. This matters, and the first draft got it wrong by always substituting the constant. Doing
that changes *whether* the probe fired as well as *why*, so any control with a false-positive arm
("this correct spelling must NOT be flagged") went red on the arm rather than the mutation and
scored as discriminating without distinguishing anything. Preserving emptiness leaves the boolean
signal exactly intact and destroys only the identity of the finding -- which is the property under
test, stated precisely. It also removes the need to exempt the control's opening baseline probe
from blinding: a baseline that really is clean stays clean by construction.

WHY BEHAVIOURAL AND NOT STRUCTURAL. The obvious version greps for a three-element mutation tuple.
That is a check on today's code shape, it is defeated by a control written in a different style, and
it would have scored `install-render-is-faithful.py` -- the one file that already did this properly
-- as a violation, because its breakages are numbered rather than signalled. Running the control and
watching what it can distinguish is the property; the tuple arity is a spelling.

THE CORPUS is every file that ADVERTISES the flag, and advertising it is a behaviour, not a
substring. A file advertises `--negative-control` when its own argv handling dispatches on it, or
when a usage line offers the flag against the file's own name. Merely CONTAINING the string is
neither, and the first draft's `"--negative-control" in p.read_text()` had exactly one consumer of
that distinction waiting for it: `invariants-gate.py` grew an arm that SEARCHES other files for the
flag, on behalf of a rule requiring a check-only unit to exhibit its future tree as a committed
control row -- and the substring corpus swept the searcher in and reported it as a control file with
no control ([[LSN-057]]). Discovery by mention is discovery by topic. A check that talks about the
convention is not a participant in it, and the paragraph above had already won this argument for the
SCORING half of the file while the DISCOVERY half stayed textual.

Splitting the two signals also buys a property the substring version could not state: a file whose
usage line offers the flag while nothing dispatches on it is a finding of its own. The documented
command then runs the ordinary check and prints its ordinary PASS, and the reader who followed the
docstring reads that as the control passing -- a green produced by not asking, which is the whole
family this file belongs to.

DISPATCH IS THE HANDLER, NOT THE SPELLING ([[B-011]], [[LSN-036]]). LSN-057 replaced the substring
with a regex, which is a shorter roster rather than a different kind of thing: `"--negative-control"
in argv` is one way to spell a dispatch and the tree has several others, so a pattern over the
invocation's characters goes stale the day a script parses its arguments in a `case`, routes them
through a variable, or matches a prefix instead of the whole word. LSN-036's rule -- derive, never
enumerate -- says the recogniser must be a property of the file's STRUCTURE, and the structure it is
looking for is the same in every language: a construct whose condition the flag SATISFIES and whose
body is therefore reached. So the test is now built from each language's own definition of the
command line and propagated forward from there:

  * Python is read as an AST. The seed is `sys.argv`, and any name bound from an expression
    mentioning it -- by assignment, by a loop target, by a parameter default, or by a parameter
    whose call site passes it -- is argv-derived too, to a fixpoint. A handler is then an `if`,
    `while` or conditional expression whose test compares an argv-derived operand against the flag
    and which has a body, or an `add_argument("--negative-control", ...)`, which IS the dispatch
    argparse builds. A docstring is a `Constant`, a comment is not in the tree at all, and an error
    message compares nothing, so none of the three can be mistaken for an arm.
  * Shell is read after comments and heredoc bodies are removed, quote-aware. The seed is `$1..$n`,
    `$@` and `$*`, propagated the same way through assignments and `for` targets. A handler is a
    `case` whose subject is one of those and one of whose arms has a pattern the flag satisfies AND
    a non-empty body, or an `if`/`elif`/`while`/`until` condition comparing such an expansion with
    the flag. An arm's pattern has to DISCRIMINATE: `*)` is satisfied by the flag and by everything
    else, so it is the default arm and not a control mode -- the pattern must match the flag and
    fail a decoy, which is this file's own thesis applied to its own recogniser.

Both halves accept a shape the tree does not use yet -- a glob arm, a prefix match, an inverted
test -- because sizing the recogniser to today's twelve identical `if [ "${1:-}" = ... ]` lines is
how it becomes a roster again on the thirteenth. Both refuse the shapes the substring accepted: the
finding B-011 was filed for is a gate arm taxing every script that WRITES about controls, whose
cheapest fix is to stop naming them.

THE FLOOR IS TWO FLOORS. A recogniser is a filter, and the failure mode of a filter is silence:
tighten it too far and the corpus empties, every remaining member passes, and the check reports a
green over nothing ([[LSN-035]], [[LSN-038]]). So the absolute floor (`MIN_CONTROLS`,
`MIN_SUITE_CONTROLS`) is joined by a relative one -- of the files that MENTION the flag, the handler
test must still recognise most -- because a tree that follows one convention does not contain a
majority of files that merely discuss it, and a rule stated against the tree's own mention count
cannot be satisfied by deleting checks.

THE PROBE is the function whose findings the control inspects: `check` in most files, `run` in
`install-render-is-faithful.py`, `scan_text` in `go-build-targets-packages.py`. It is read off the
control's OWN bytecode -- which of PROBE_NAMES does `negative_control` actually call -- rather than
off the module, because those are not the same question. `go-build-targets-packages.py` defines both
`check` and `scan_text`; `check` walks the repository and the control never calls it, so blinding it
by module-order was a no-op and the file scored as discriminating while distinguishing nothing. A
control that calls none of PROBE_NAMES is a finding, not a skip, because "could not instrument it"
and "it passed" must never look alike ([[LSN-038]]).

THE CALLING CONVENTION is `negative_control()` with no required arguments -- a control that needs
inputs reads them itself. It is not a style preference: a control this file cannot invoke is a
control nothing can invoke uniformly, and the failure mode is a crash scored as a pass.

Self-test (the `¬` of the `¬`): `--negative-control` confirms this file scores a deliberately
non-discriminating control as non-discriminating and a discriminating one as discriminating.

Run:  python3 dev/tests/negative-controls-name-their-rule.py
      python3 dev/tests/negative-controls-name-their-rule.py --negative-control
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import importlib.util
import io
import pathlib
import re
import sys
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
TESTS = REPO / "dev" / "tests"
VERIFY = REPO / "dev" / "verify"
SELF = pathlib.Path(__file__).name

FLAG = "--negative-control"

# Tried in order. The first one a module defines is its probe.
PROBE_NAMES = ("check", "run", "scan_text")

# A failure that names no property. Any control that accepts this as proof its mutation was caught
# is accepting "something went wrong" as proof that the right thing went wrong.
SENTINEL = "SENTINEL: a constant failure that identifies no property"

# Non-vacuity. The corpus only grows; a scan that finds less than this stopped scanning.
MIN_CONTROLS = 9
MIN_SUITE_CONTROLS = 8

# Arguments a control mode must NOT be confused with. An arm is only a handler for the flag if its
# pattern is satisfied by the flag and refused by all of these -- `*)` passes the first test and
# fails the second, which is exactly the difference between a default arm and a mode.
DECOYS = ("--live", "-h", "run", "")

# The promise-without-a-handler finding, in one place because both corpora produce it.
_NO_HANDLER = (
    "offers `--negative-control` in a usage line while no arm in the file dispatches on it, so the "
    "documented command runs the ORDINARY check and prints its ordinary PASS. A reader who follows "
    "the docstring reads that as the control passing"
)


# --------------------------------------------------------------------------------- python -----
# The command line, and everything reachable from it. `sys.argv` is the seed because it is the
# language's own definition site; nothing here knows that the repo happens to spell its parameters
# `argv` and `args`, and a file that spells them `cli` is recognised for free.


def _root(node) -> str | None:
    """The name an operand is rooted at: `argv[1:]` -> `argv`, `sys.argv` -> `sys.argv`."""
    while isinstance(node, (ast.Subscript, ast.Starred, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _from_argv(node, names: set[str]) -> bool:
    return any(
        (isinstance(s, ast.Attribute) and s.attr == "argv" and getattr(s.value, "id", None) == "sys")
        or (isinstance(s, ast.Name) and s.id in names)
        for s in ast.walk(node)
    )


def _argv_derived(tree: ast.AST) -> set[str]:
    """Every name that carries the command line, to a fixpoint over four kinds of binding."""
    names: set[str] = set()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for _ in range(4):
        before = len(names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if _from_argv(node.value, names):
                    names |= {s.id for t in targets for s in ast.walk(t) if isinstance(s, ast.Name)}
            elif isinstance(node, (ast.For, ast.comprehension)) and _from_argv(node.iter, names):
                names |= {s.id for s in ast.walk(node.target) if isinstance(s, ast.Name)}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spec = node.args
                params = [*spec.posonlyargs, *spec.args]
                pairs = list(zip(params[len(params) - len(spec.defaults) :], spec.defaults))
                pairs += list(zip(spec.kwonlyargs, spec.kw_defaults))
                names |= {a.arg for a, d in pairs if d is not None and _from_argv(d, names)}
                # The call site is the other half of a parameter's binding: `main(sys.argv[1:])` is
                # what makes `argv` the command line inside `def main(argv)`.
                for call in calls:
                    fn = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
                    if fn != node.name:
                        continue
                    names |= {
                        params[i].arg
                        for i, a in enumerate(call.args)
                        if i < len(params) and _from_argv(a, names)
                    }
                    names |= {k.arg for k in call.keywords if k.arg and _from_argv(k.value, names)}
        if len(names) == before:
            break
    return names


def _py_handler(text: str) -> bool | None:
    """None means the file could not be parsed, which is a finding and never a silent exclusion."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        # argparse: registering the option IS the dispatch. There is no branch to find because the
        # parser builds one.
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument":
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == FLAG:
                return True

    names = _argv_derived(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            test, body = node.test, node.body
        elif isinstance(node, ast.IfExp):
            test, body = node.test, [node.body]
        else:
            continue
        for cmp in (c for c in ast.walk(test) if isinstance(c, ast.Compare)):
            if not {type(o) for o in cmp.ops} <= {ast.In, ast.NotIn, ast.Eq, ast.NotEq}:
                continue
            operands = [cmp.left, *cmp.comparators]
            named = any(isinstance(o, ast.Constant) and o.value == FLAG for o in operands)
            argvish = any(_root(o) in names or _root(o) == "sys.argv" for o in operands)
            if named and argvish and body:
                return True
    return False


# ---------------------------------------------------------------------------------- shell -----


def _sh_code(text: str) -> str:
    """The script with comments and heredoc bodies blanked, quote state carried across lines.

    This is where a mention stops being a candidate for a handler. A comment, a `usage()` heredoc
    and an error string are the three places the flag appears in a script that does not implement
    it, and the first two are not code at all.
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
        # `<<'EOF'` / `<<-WORD`, but not `<<<` and not the arithmetic shift, whose operand cannot
        # start with a letter.
        opener = re.search(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", clean)
        if opener:
            heredoc = opener.group(2)
        out.append(clean)
    return "\n".join(out)


def _expansion(names: set[str]) -> re.Pattern:
    body = "|".join(["[1-9][0-9]*", *sorted(re.escape(n) for n in names)])
    return re.compile(r"\$\{?\s*(?:" + body + r")\b|\$\{?\s*[@*]")


def _sh_argv(code: str) -> re.Pattern:
    """`$1..$n`, `$@`, `$*`, and every variable or loop target bound from one of them."""
    names: set[str] = set()
    for _ in range(4):
        pat = _expansion(names)
        before = len(names)
        for m in re.finditer(r"(?m)^\s*(?:local|declare|export|readonly)?\s*([A-Za-z_]\w*)=(\S[^\n]*)", code):
            if pat.search(m.group(2)):
                names.add(m.group(1))
        for m in re.finditer(r"\bfor\s+([A-Za-z_]\w*)\s+in\s+([^\n;]*)", code):
            if pat.search(m.group(2)):
                names.add(m.group(1))
        if len(names) == before:
            break
    return _expansion(names)


def _satisfies(token: str) -> bool:
    """Is this pattern or comparand satisfied BY THE FLAG AND NOT BY AN ARBITRARY ARGUMENT?

    The second half is the whole reason this is a function. `*)` is satisfied by the flag, and by
    `--live`, and by nothing being passed at all; reading it as a control mode makes every script
    in `dev/verify/` a control, which is a recogniser that has stopped distinguishing anything --
    the defect this file exists to find, in this file's own machinery.
    """
    word = token.replace('"', "").replace("'", "")
    if word == FLAG:
        return True
    if not any(ch in word for ch in "*?["):
        return False
    if not any(fnmatch.fnmatchcase(c, word) for c in (FLAG, f" {FLAG} ")):
        return False
    return not any(fnmatch.fnmatchcase(d, word) for d in DECOYS)


# A keyword only opens a construct at the start of a statement; the same letters inside a string
# are a word in a sentence.
_STATEMENT = r"(?:^|[\n;&|(){}]|\bthen\b|\bdo\b|\belse\b)\s*"


def _sh_cases(code: str):
    """Every `case <subject> in ... esac`, nesting counted so an inner `esac` cannot close it."""
    for m in re.finditer(_STATEMENT + r"case\s+(?P<subject>.+?)\s+in\b", code):
        depth, tail, end = 1, code[m.end() :], None
        for t in re.finditer(r"\b(case|esac)\b", tail):
            depth += 1 if t.group(1) == "case" else -1
            if depth == 0:
                end = t.start()
                break
        yield m.group("subject"), tail[:end]


def _sh_handler(text: str) -> bool:
    code = _sh_code(text)
    arg = _sh_argv(code)

    for subject, block in _sh_cases(code):
        if not arg.search(subject):
            continue
        for chunk in re.split(r";;&?|;&", block):
            arm = re.match(r"\s*\(?\s*([^()\n]*?)\)", chunk)
            # A pattern with nothing under it accepts the flag and then does what the default arm
            # would have done, so the mode it declares is not a mode.
            if not arm or not chunk[arm.end() :].strip():
                continue
            if any(_satisfies(alt.strip()) for alt in arm.group(1).split("|")):
                return True

    for m in re.finditer(_STATEMENT + r"(?:if|elif|while|until)\b", code):
        rest = code[m.end() :]
        opener = re.search(r"\b(?:then|do)\b", rest)
        if not opener:
            continue
        stop = re.search(r"\b(?:fi|done|else|elif)\b", rest[opener.end() :])
        if not rest[opener.end() :][: stop.start() if stop else None].strip():
            continue
        for cmp in re.finditer(r"(\S+)\s+(?:==?|!=)\s+(\S+)", rest[: opener.start()]):
            lhs, rhs = cmp.group(1), cmp.group(2)
            if (arg.search(lhs) and _satisfies(rhs)) or (arg.search(rhs) and _satisfies(lhs)):
                return True
    return False


def handles(path: pathlib.Path, text: str | None = None) -> bool | None:
    """Does this file DISPATCH on `--negative-control` -- an arm the flag reaches, not a mention?

    The one recogniser both halves of the convention are read through, and the function
    `invariants-gate.py`'s LSN-060 arm is meant to consult in place of its own substring test
    (B-011). `None` is "could not be determined", never "no".
    """
    text = path.read_text() if text is None else text
    if path.suffix == ".py" or re.match(r"#!.*python", text):
        return _py_handler(text)
    return _sh_handler(text)


def _promises(path: pathlib.Path, text: str) -> bool:
    """Does a usage line offer the flag against THIS file's own name?

    The interpreter is not part of the property -- `python3 x.py --flag`, `bash x.sh --flag` and a
    bare `dev/verify/x.sh --flag` are the same promise -- but the file's OWN name is: a script that
    documents someone else's control mode has promised nothing.
    """
    pattern = r"(?:^|[\s`(])(?:python3?|bash|sh)?\s*\S*" + re.escape(path.name) + r"\s+" + re.escape(FLAG)
    return re.search(pattern, text) is not None


def _rel(path: pathlib.Path) -> str:
    """Repo-relative where possible; a synthetic tree under /tmp has no repo-relative form."""
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def controls(root: pathlib.Path = TESTS) -> list[pathlib.Path]:
    found = []
    for p in sorted(root.glob("*.py")):
        if p.name == SELF:
            continue
        text = p.read_text()
        verdict = handles(p, text)
        # `verdict is None` -- unparseable -- keeps the file IN. `score()` then reports it as
        # unimportable, which is the honest outcome; dropping it from the corpus would turn "could
        # not be read" into "has no control", and those must never look alike ([[LSN-038]]).
        if verdict or _promises(p, text) or (verdict is None and FLAG in text):
            found.append(p)
    return found


def suites(root: pathlib.Path = VERIFY) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """(mentions, handlers) over the shell suites -- the corpus B-011 is about."""
    mentions, handlers = [], []
    for p in sorted(root.glob("*.sh")):
        text = p.read_text()
        if FLAG not in text:
            continue
        mentions.append(p)
        if handles(p, text):
            handlers.append(p)
    return mentions, handlers


def _load(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"_nc_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _called_names(fn) -> set[str]:
    """Every global name `fn` references, including from lambdas and comprehensions inside it."""
    names: set[str] = set()
    stack = [fn.__code__]
    while stack:
        code = stack.pop()
        names |= set(code.co_names)
        stack.extend(c for c in code.co_consts if hasattr(c, "co_names"))
    return names


def _sentinel_like(finding):
    """A finding of the same SHAPE carrying none of the same information.

    Shape is preserved because the control is entitled to unpack what the probe returns --
    `go-build-targets-packages.py` reads `(line, invocation, operand)` triples, and handing it a
    bare string is a crash, not a blinding. A crash scores as "could not be scored" (LSN-038),
    which is honest but useless: it says nothing about whether the control can tell its properties
    apart. So every string becomes the sentinel and every number becomes zero, in place.
    """
    if isinstance(finding, str):
        return SENTINEL
    if isinstance(finding, bool):
        return finding
    if isinstance(finding, (int, float)):
        return 0
    if isinstance(finding, tuple):
        return tuple(_sentinel_like(x) for x in finding)
    if isinstance(finding, list):
        return [_sentinel_like(x) for x in finding]
    if isinstance(finding, dict):
        return {k: _sentinel_like(v) for k, v in finding.items()}
    return finding


def _blind(mod: types.ModuleType, probe: str, state: dict):
    """Wrap the probe so a non-empty finding list collapses to one finding that names no property.

    An EMPTY result is passed through untouched -- see the module docstring. The real probe is still
    called, so the wrapper mirrors whatever shape it returns -- a bare list, or a `(failures, stats)`
    tuple -- without knowing which in advance.

    `state["blinded"]` records whether any call was actually rewritten. A probe whose findings never
    took a shape this could substitute was not instrumented, and an uninstrumented run must not be
    scored as if it had been.
    """
    real = getattr(mod, probe)

    def blinded(*a, **k):
        result = real(*a, **k)
        istuple = isinstance(result, tuple)
        findings = result[0] if istuple else result
        if isinstance(findings, list) and findings:
            replacement = [_sentinel_like(findings[0])]
            state["blinded"] = True
        else:
            replacement = findings
        return (replacement, *result[1:]) if istuple else replacement

    setattr(mod, probe, blinded)


def score(path: pathlib.Path) -> tuple[bool, str]:
    """(discriminating, note). Blind the probe; a control that still passes is not discriminating."""
    if handles(path) is False:
        return False, _NO_HANDLER

    try:
        mod = _load(path)
    except Exception as exc:  # noqa: BLE001 -- an unimportable check is a finding, not a skip
        return False, f"could not be imported ({type(exc).__name__}: {exc})"

    control = getattr(mod, "negative_control", None)
    if not callable(control):
        return False, "advertises `--negative-control` but defines no `negative_control()`"

    called = _called_names(control)
    probes = [n for n in PROBE_NAMES if n in called and callable(getattr(mod, n, None))]
    if not probes:
        return False, (
            f"its `negative_control()` calls none of {list(PROBE_NAMES)}, so its findings cannot be "
            f"blinded. Name the function whose findings the control inspects `check`, `run` or "
            f"`scan_text` and call it directly from the control"
        )

    state = {"blinded": False}
    for probe in probes:
        _blind(mod, probe, state)
    shown = " / ".join(f"{p}()" for p in probes)

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = mod.negative_control()
    except TypeError as exc:
        if "positional argument" in str(exc):
            return False, (
                f"could not be invoked as `negative_control()` ({exc}). A control that takes "
                f"required arguments cannot be run uniformly; give it defaults and let it read its "
                f"own inputs"
            )
        return False, (
            f"raised TypeError when {shown} returned a constant ({exc}), so it could not be scored "
            f"at all. Make the control tolerate an unexpected finding shape"
        )
    except Exception as exc:  # noqa: BLE001
        # NOT a pass. A control that crashes on a constant failure has not demonstrated that it can
        # tell its properties apart -- it has demonstrated that the instrument could not run, which
        # is the one outcome that must never be scored as success ([[LSN-038]]). The first draft
        # returned True here and `identity-has-install-path.py` sailed through on a TypeError.
        return False, (
            f"raised {type(exc).__name__} when {shown} returned a constant ({exc}), so it could "
            f"not be scored at all. Make the control tolerate an unexpected finding shape"
        )

    if not state["blinded"]:
        return False, (
            f"ran to completion without {shown} ever returning a non-empty list, so nothing was "
            f"actually blinded and this run proves nothing either way. Either the control's "
            f"mutations stopped applying, or its findings are not a list this can substitute"
        )
    if rc == 0:
        return False, f"passed with every finding from {shown} replaced by one constant string"
    return True, f"failed when {shown} stopped distinguishing its properties"


def check(paths: list[pathlib.Path], verify: pathlib.Path = VERIFY) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    ok: list[str] = []
    for p in paths:
        good, note = score(p)
        (ok if good else failures).append(f"{_rel(p)}: {note}")
    out = [
        f"{f} -- so it asserts only that SOMETHING failed, not that the property the mutation "
        f"targets is what caught it. Give each mutation a signal naming its property and assert "
        f"the signal appears in the findings; `dev/tests/install-render-is-faithful.py` is the "
        f"worked example (LSN-035)."
        for f in failures
    ]

    # THE SHELL HALF (B-011). The L2 suites carry the same convention and the same failure mode, and
    # they are not importable, so the promise arm is the half of the property that can be asserted
    # here. It is also what keeps the shell recogniser honest: a handler test nothing runs against
    # the real tree is a rule about a shape nobody has checked.
    mentioned_sh, handled_sh = suites(verify)
    for p in mentioned_sh:
        if p not in handled_sh and _promises(p, p.read_text()):
            out.append(f"{_rel(p)}: {_NO_HANDLER}.")

    # NON-VACUITY, absolutely and then relatively. The absolute floor catches a recogniser that
    # stopped matching; the relative one catches a recogniser that got strict enough to reject the
    # convention it is supposed to recognise, which the absolute floor cannot see while the corpus
    # is still growing (LSN-035, LSN-038).
    mentioned_py = [
        p for p in sorted(TESTS.glob("*.py")) if p.name != SELF and FLAG in p.read_text()
    ]
    corpora = [
        ("dev/tests", "negative controls", mentioned_py, list(paths), MIN_CONTROLS),
        (_rel(verify), "suites with a control mode", mentioned_sh, handled_sh, MIN_SUITE_CONTROLS),
    ]
    for where, what, mentioned, found, floor in corpora:
        if not mentioned:
            out.append(
                f"VACUOUS: no file under {where} so much as mentions `{FLAG}`, so the handler test "
                f"examined zero candidates there and could not have rejected one. 'Found nothing' "
                f"and 'asked nothing' are the same exit code, and only one of them is a pass."
            )
        elif len(found) < floor:
            out.append(
                f"VACUOUS: found {len(found)} {what} under {where}, expected at least {floor}. The "
                f"discovery broke, and a meta-check over an empty corpus is the most convincing "
                f"green in the repository."
            )
        elif mentioned and len(found) * 2 < len(mentioned):
            out.append(
                f"VACUOUS: the handler test recognised {len(found)} of the {len(mentioned)} files "
                f"under {where} that mention `{FLAG}`. A tree that follows one convention does not "
                f"contain a majority of files that only discuss it, so this is the recogniser "
                f"having gone wrong and not the tree (B-011, LSN-036)."
            )
    return out, ok


def negative_control() -> int:
    """Score controls of known quality, recognise dispatch shapes of known kind, starve the floors.

    Three blocks, because the file now makes three claims. The controls say what a discriminating
    control looks like; the shapes say what a HANDLER looks like, in both directions and in both
    languages (B-011); the floors say that a recogniser which has stopped recognising is a red and
    not a quiet green.
    """
    import tempfile

    NON_DISCRIMINATING = '''
"""A control with --negative-control that only checks non-emptiness."""
import sys
def check(s):
    return ["property A violated"] if "bad" in s else []
def negative_control():
    if check("good"):
        return 1
    for label, m in [("A", "bad"), ("B", "bad")]:
        if not check(m):
            print("survivor", label); return 1
    print("PASS"); return 0
def main():
    return negative_control() if "--negative-control" in sys.argv else 0
'''
    DISCRIMINATING = NON_DISCRIMINATING.replace(
        'for label, m in [("A", "bad"), ("B", "bad")]:\n        if not check(m):',
        'for label, m, sig in [("A", "bad", "property A")]:\n        if not any(sig in f for f in check(m)):',
    )
    NO_PROBE = (
        '"""--negative-control"""\nimport sys\ndef negative_control():\n    return 1\n'
        'def main():\n    return negative_control() if "--negative-control" in sys.argv else 0\n'
    )

    # The case the emptiness-preserving blinding exists for: a control with a reject arm AND an
    # accept arm, asserting only non-emptiness on each. A blinding that substituted the constant
    # unconditionally would trip the accept arm and score this as discriminating.
    ACCEPT_REJECT = '''
"""--negative-control"""
import sys
def scan_text(s):
    return ["property A violated"] if "bad" in s else []
def negative_control():
    wrong = 0
    for snippet in ("bad", "bad bad"):
        if not scan_text(snippet):
            wrong += 1
    for snippet in ("good",):
        if scan_text(snippet):
            wrong += 1
    return 1 if wrong else 0
def main():
    return negative_control() if "--negative-control" in sys.argv else 0
'''

    # The probe is read off the control's bytecode, not the module: this one defines `check` too,
    # and blinding `check` would be a no-op because the control never calls it.
    DECOY_PROBE = ACCEPT_REJECT.replace(
        "def scan_text(s):", "def check(root):\n    return [\"walked the tree\"]\ndef scan_text(s):"
    )

    # Findings that are tuples, not strings. The blinding must keep the arity or the control
    # crashes on unpacking, and a crash is scored as unscoreable rather than as a verdict.
    TUPLE_FINDINGS = '''
"""--negative-control"""
import sys
def scan_text(s):
    return [(1, s, "cmd/main.go")] if "bad" in s else []
def negative_control():
    for snippet, operand in (("bad", "cmd/main.go"),):
        named = [op for _, _, op in scan_text(snippet)]
        if operand not in named:
            return 1
    return 0
def main():
    return negative_control() if "--negative-control" in sys.argv else 0
'''

    cases = [
        ("a control that only asserts non-emptiness", NON_DISCRIMINATING, False),
        ("a control that asserts which property fired", DISCRIMINATING, True),
        ("a control whose probe cannot be found", NO_PROBE, False),
        ("a control with both a reject and an accept arm", ACCEPT_REJECT, False),
        ("a control whose module defines a probe it never calls", DECOY_PROBE, False),
        ("a control whose findings are tuples, not strings", TUPLE_FINDINGS, True),
    ]

    # The corpus half (LSN-057). Discovery is by BEHAVIOUR, so a file that merely talks about the
    # flag stays out and a file that promises it without implementing it comes in as a finding.
    TALKS_ABOUT_IT = (
        'def check(body):\n'
        '    return [] if "--negative-control" in body else ["no control row"]\n'
    )
    EMPTY_PROMISE = (
        '"""Run:  python3 empty_promise.py --negative-control"""\n'
        'def negative_control():\n    return 1\n'
    )

    # The recogniser half (B-011). One row per shape `handles()` claims to support, plus the two
    # rows the claim is actually about: a file where the flag appears only where a reader can see
    # it and no arm can reach it, in each language. Every row names the structure it exercises,
    # because a row that scores a boolean without saying which structure produced it is this file's
    # own finding turned inward.
    SH_IF_TEST = '''#!/usr/bin/env bash
# Run:  dev/verify/sh_if_test.sh --negative-control
set -euo pipefail
if [ "${1:-}" = "--negative-control" ]; then
  replay_assertions
  exit $?
fi
'''
    SH_BRACKET_TEST = '''#!/usr/bin/env bash
if [[ "$1" == "--negative-control" ]]; then
  replay_assertions; exit $?
fi
'''
    SH_INVERTED_TEST = '''#!/usr/bin/env bash
if [ "${1:-}" != "--negative-control" ]; then
  echo "usage: sh_inverted_test.sh --negative-control" >&2
  exit 2
fi
replay_assertions
'''
    SH_CASE_LITERAL = '''#!/usr/bin/env bash
case "${1:-}" in
  --negative-control)
    replay_assertions
    exit $?
    ;;
  *) : ;;
esac
'''
    SH_CASE_GLOB = '''#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    -n | --negative-*)
      MODE=control
      ;;
  esac
done
'''
    SH_CASE_MEMBERSHIP = '''#!/usr/bin/env bash
case " $* " in
  *" --negative-control "*)
    replay_assertions; exit $?
    ;;
esac
'''
    SH_EMPTY_ARM = '''#!/usr/bin/env bash
case "$1" in
  --negative-control) ;;
esac
'''
    # The default arm is satisfied by the flag and by everything else. Reading it as a mode is how
    # a recogniser comes to match every suite in dev/verify, twelve of which have no control at all.
    SH_DEFAULT_ARM = '''#!/usr/bin/env bash
case "${1:-}" in
  --live)
    run_live
    ;;
  *)
    echo "usage: sh_default_arm.sh --live   (there is no --negative-control mode here)" >&2
    exit 2
    ;;
esac
'''
    # The three places the flag appears in a suite that does not implement it, and each one falls
    # to a different rule: a comment that QUOTES the missing arm (comments are stripped), a usage
    # heredoc that quotes it again (heredoc bodies are stripped), and an error message that quotes
    # it a third time in the BODY of a construct whose condition is about something else (only the
    # condition is read). Each of the three is written the way this tree actually writes them --
    # showing the code -- because a mention that could not be mistaken for an arm proves nothing.
    SH_MENTIONS_ONLY = '''#!/usr/bin/env bash
# MODES. `--live` reads the cluster. The offline arm this suite still owes is one line and it is
# NOT written yet:  set -eu; if [ "${1:-}" = "--negative-control" ]; then replay_assertions; fi
usage() {
  cat <<'EOF'
usage: sh_mentions_only.sh [--live]
       sh_mentions_only.sh --negative-control     (planned; it isn't wired up yet)
the arm it needs:  set -eu; if [ "${1:-}" = "--negative-control" ]; then replay_assertions; fi
EOF
}
if [ "${1:-}" = "--live" ]; then
  run_live
  exit $?
fi
usage
echo 'no such mode. the missing test is  [ "${1:-}" = "--negative-control" ]' >&2
exit 2
'''
    PY_SYS_ARGV = 'import sys\nif "--negative-control" in sys.argv:\n    raise SystemExit(negative_control())\n'
    PY_PARAMETER = (
        "import sys\n"
        "def main(cli):\n"
        '    if "--negative-control" in cli:\n'
        "        return negative_control()\n"
        "    return 0\n"
        "sys.exit(main(sys.argv[1:]))\n"
    )
    PY_ARGPARSE = (
        "import argparse\n"
        "ap = argparse.ArgumentParser()\n"
        'ap.add_argument("--negative-control", action="store_true")\n'
    )
    PY_MENTIONS_ONLY = (
        '"""Run:  python3 py_mentions_only.py --negative-control  (planned)."""\n'
        "# --negative-control would replay each assertion against a synthesised input.\n"
        "def check(text):\n"
        '    return [] if "--negative-control" in text else ["no control mode declared"]\n'
    )

    shapes = [
        ("sh_if_test.sh", "an `if [ \"${1:-}\" = ... ]`, the shape all twelve dev/verify suites use", SH_IF_TEST, True),
        ("sh_bracket_test.sh", "an `if [[ \"$1\" == ... ]]`", SH_BRACKET_TEST, True),
        ("sh_inverted_test.sh", "an `if [ ... != ... ]` that rejects everything else", SH_INVERTED_TEST, True),
        ("sh_case_literal.sh", "a `case` arm on `${1:-}` with a literal pattern", SH_CASE_LITERAL, True),
        ("sh_case_glob.sh", "a `case` arm whose pattern is a GLOB, over a name bound from \"$@\"", SH_CASE_GLOB, True),
        ("sh_case_membership.sh", "a `case` on `$*` testing membership of the whole line", SH_CASE_MEMBERSHIP, True),
        ("sh_empty_arm.sh", "a `case` arm the flag matches that runs nothing", SH_EMPTY_ARM, False),
        ("sh_default_arm.sh", "a `case` whose only satisfied arm is the default `*)`", SH_DEFAULT_ARM, False),
        ("sh_mentions_only.sh", "a comment, a usage heredoc and an error string, and no arm", SH_MENTIONS_ONLY, False),
        ("py_sys_argv.py", "a membership test against `sys.argv`", PY_SYS_ARGV, True),
        ("py_parameter.py", "a membership test against a parameter the call site binds to `sys.argv`", PY_PARAMETER, True),
        ("py_argparse.py", "an `add_argument` action", PY_ARGPARSE, True),
        ("py_mentions_only.py", "a docstring, a comment and a search of ANOTHER file's text, and no dispatch", PY_MENTIONS_ONLY, False),
    ]

    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        for label, src, expected in cases:
            p = pathlib.Path(tmp) / f"{abs(hash(label))}.py"
            p.write_text(src)
            got, note = score(p)
            if got != expected:
                survivors.append(f"{label}: scored discriminating={got}, expected {expected} ({note})")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "talks_about_it.py").write_text(TALKS_ABOUT_IT)
        (root / "empty_promise.py").write_text(EMPTY_PROMISE)
        (root / "real_control.py").write_text(cases[1][1])
        found = {p.name for p in controls(root)}
        if "talks_about_it.py" in found:
            survivors.append(
                "a file that only SEARCHES other files for `--negative-control` was swept into the "
                "corpus -- discovery is back to a substring match (LSN-057)"
            )
        if "real_control.py" not in found:
            survivors.append("a file that dispatches on the flag was not discovered at all")
        if "empty_promise.py" not in found:
            survivors.append("a file whose usage line offers the flag was not discovered at all")
        else:
            good, note = score(root / "empty_promise.py")
            if good:
                survivors.append(
                    f"a usage line offering `--negative-control` with nothing dispatching on it "
                    f"scored as discriminating ({note})"
                )

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for name, rule, src, expected in shapes:
            p = root / name
            p.write_text(src)
            got = handles(p, src)
            if got is not expected:
                survivors.append(
                    f"{rule}: the handler test recognised={got}, expected {expected}. A mode is "
                    f"recognised by the arm the flag REACHES, never by the characters appearing in "
                    f"the file, and never by a roster of the spellings the tree happens to use "
                    f"today (B-011, LSN-036)"
                )
        # The other direction of the mention-only rows: rejecting them is only half the finding.
        # A script that documents a mode it does not have sends its reader to a command that runs
        # the ORDINARY check and prints an ordinary PASS, so it has to be REPORTED, not skipped.
        promised = [n for n, _, s, _ in shapes if _promises(root / n, s)]
        if "sh_mentions_only.sh" not in promised:
            survivors.append(
                "a suite whose usage heredoc offers `--negative-control` against its own name did "
                "not register as a promise, so the handler test would drop it silently instead of "
                "reporting a documented mode that does not exist (B-011)"
            )
        reported, _ = check([], root)
        if not any("sh_mentions_only.sh" in f and "usage line" in f for f in reported):
            survivors.append(
                "the shell half of `check()` did not report `sh_mentions_only.sh`, whose usage "
                "heredoc promises a mode no arm implements -- the finding B-011 asks the "
                "recogniser to make possible"
            )

    # THE FLOORS, both registers. A recogniser is a filter and a filter fails silent; if these two
    # arms cannot fire, every tightening of `handles()` above is one commit away from a green over
    # an empty corpus (LSN-035, LSN-038).
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        starved, _ = check([], root)
        if not any(f.startswith("VACUOUS:") and "negative controls under dev/tests" in f for f in starved):
            survivors.append(
                "`check()` accepted an EMPTY python corpus without a VACUOUS finding, so the "
                "absolute floor under dev/tests cannot fire (LSN-035)"
            )
        if not any(f.startswith("VACUOUS:") and "zero candidates" in f for f in starved):
            survivors.append(
                "`check()` accepted a verify tree in which nothing mentions the flag without "
                "saying it examined zero candidates -- 'found nothing' and 'asked nothing' read "
                "identically from the exit code (LSN-038)"
            )
        (root / "one_suite.sh").write_text(SH_IF_TEST)
        thin, _ = check(controls(), root)
        if not any(f.startswith("VACUOUS:") and "suites with a control mode" in f for f in thin):
            survivors.append(
                f"`check()` accepted a verify tree holding ONE suite with a control mode without a "
                f"VACUOUS finding, so the floor of {MIN_SUITE_CONTROLS} is not asserted and a "
                f"recogniser that suddenly matches two files reads as strict rather than broken"
            )

    if not controls():
        survivors.append("the corpus discovery found no negative controls at all")

    if survivors:
        print("FAIL: negative-controls-name-their-rule negative control:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1
    print(
        f"PASS: negative-controls-name-their-rule negative control -- all {len(cases)} controls and "
        f"{len(shapes)} dispatch shapes scored correctly"
    )
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    paths = controls()
    failures, ok = check(paths)
    if failures:
        print("FAIL: negative-controls-name-their-rule (LSN-035)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(
        f"PASS: negative-controls-name-their-rule (L0) -- all {len(ok)} negative controls under "
        f"dev/tests assert WHICH property caught each mutation, not merely that something did"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
