#!/usr/bin/env python3
"""A negative control that only proves the suite went red proves almost nothing (LSN-035).

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

import contextlib
import importlib.util
import io
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parents[2]
TESTS = REPO / "dev" / "tests"
SELF = pathlib.Path(__file__).name

# Tried in order. The first one a module defines is its probe.
PROBE_NAMES = ("check", "run", "scan_text")

# A failure that names no property. Any control that accepts this as proof its mutation was caught
# is accepting "something went wrong" as proof that the right thing went wrong.
SENTINEL = "SENTINEL: a constant failure that identifies no property"

# Non-vacuity. The corpus only grows; a scan that finds less than this stopped scanning.
MIN_CONTROLS = 9


def controls() -> list[pathlib.Path]:
    return sorted(
        p for p in TESTS.glob("*.py")
        if p.name != SELF and "--negative-control" in p.read_text()
    )


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


def check(paths: list[pathlib.Path]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    ok: list[str] = []
    for p in paths:
        rel = p.relative_to(REPO)
        good, note = score(p)
        (ok if good else failures).append(f"{rel}: {note}")
    out = [
        f"{f} -- so it asserts only that SOMETHING failed, not that the property the mutation "
        f"targets is what caught it. Give each mutation a signal naming its property and assert "
        f"the signal appears in the findings; `dev/tests/install-render-is-faithful.py` is the "
        f"worked example (LSN-035)."
        for f in failures
    ]
    if len(paths) < MIN_CONTROLS:
        out.append(
            f"VACUOUS: found {len(paths)} negative controls under dev/tests, expected at least "
            f"{MIN_CONTROLS}. The discovery broke, and a meta-check over an empty corpus is the "
            f"most convincing green in the repository."
        )
    return out, ok


def negative_control() -> int:
    """Score two controls of known quality: one that names its rule, one that does not."""
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
    NO_PROBE = '"""--negative-control"""\ndef negative_control():\n    return 1\n'

    # The case the emptiness-preserving blinding exists for: a control with a reject arm AND an
    # accept arm, asserting only non-emptiness on each. A blinding that substituted the constant
    # unconditionally would trip the accept arm and score this as discriminating.
    ACCEPT_REJECT = '''
"""--negative-control"""
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
def scan_text(s):
    return [(1, s, "cmd/main.go")] if "bad" in s else []
def negative_control():
    for snippet, operand in (("bad", "cmd/main.go"),):
        named = [op for _, _, op in scan_text(snippet)]
        if operand not in named:
            return 1
    return 0
'''

    cases = [
        ("a control that only asserts non-emptiness", NON_DISCRIMINATING, False),
        ("a control that asserts which property fired", DISCRIMINATING, True),
        ("a control whose probe cannot be found", NO_PROBE, False),
        ("a control with both a reject and an accept arm", ACCEPT_REJECT, False),
        ("a control whose module defines a probe it never calls", DECOY_PROBE, False),
        ("a control whose findings are tuples, not strings", TUPLE_FINDINGS, True),
    ]

    survivors = []
    with tempfile.TemporaryDirectory() as tmp:
        for label, src, expected in cases:
            p = pathlib.Path(tmp) / f"{abs(hash(label))}.py"
            p.write_text(src)
            got, note = score(p)
            if got != expected:
                survivors.append(f"{label}: scored discriminating={got}, expected {expected} ({note})")

    if not controls():
        survivors.append("the corpus discovery found no negative controls at all")

    if survivors:
        print("FAIL: negative-controls-name-their-rule negative control:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1
    print(f"PASS: negative-controls-name-their-rule negative control -- all {len(cases)} scored correctly")
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
