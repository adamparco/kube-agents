#!/usr/bin/env python3
"""V-REV-004: the undo round-trip corpus covers every verb, in both directions.

The Go table test in k8s-operator/internal/broker/undo/corpus_test.go runs the cases. It cannot
notice a case that was never written. That is the whole job of this file: 09 §7.3 asks for "one
fixture per supported verb x resource-kind class ... includes the negative set", and a corpus that
quietly loses the `scale` row still passes every test in it.

What it checks, and why each one is here rather than left to review:

  1. Every verb in the 06 §4.3.1 strategy table -- read out of strategy.go, not retyped -- has at
     least one case that produces a PLAN. A verb with only refusals is a verb whose inverse has
     never been seen to work.
  2. Every strategy the table can produce has at least one case that produces it. `inverse` is the
     one that goes missing: it applies to a single kind today, and a corpus author writing the
     Kubernetes cases has no reason to think of it.
  3. The negative set covers the three effects 09 §7.3 names by hand -- deleted volume data, a
     released IP, a rotated credential -- plus the two fail-closed rows of 06 §4.4. These are
     checked by looking for a case whose target kind is on the relevant list, so renaming a fixture
     does not silently drop the coverage.
  4. Every kind in `nonRecreatableKinds` that the corpus mentions at all is asserted NOT undoable.
     A fixture that deletes a StorageBucket and expects a plan is a corpus that has been edited to
     agree with a bug.
  5. Structural validity: unique ids, known keys, a stated expectation, and no case that asserts
     undoable-with-no-steps.

Run:  python3 dev/tests/undo-corpus-lint.py
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yamlsubset import CorpusSyntaxError, load_corpus  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
CORPUS = REPO / "verification" / "fixtures" / "undo" / "round-trip.yaml"
STRATEGY_GO = REPO / "k8s-operator" / "internal" / "broker" / "undo" / "strategy.go"

MIN_CASES = 25

STRATEGIES = {"delete", "restore", "recreate", "inverse", "none"}

CASE_KEYS = {"id", "description", "operation", "operations", "expect"}
OP_KEYS = {
    "verb",
    "target",
    "existed",
    "preState",
    "snapshotFailed",
    "isStatusTarget",
    "priorReplicas",
    "inboundRefs",
    "noReferenceIndex",
    "referenceIndexError",
}
TARGET_KEYS = {"group", "version", "kind", "namespace", "name", "uid"}
EXPECT_KEYS = {
    "strategy",
    "undoable",
    "stepOps",
    "refusalContains",
    "noSteps",
    "redactedKeys",
    "classifierRejectsInput",
}

# The three effects 09 §7.3 names in prose, each mapped to the kinds that stand for it. Written as
# kinds rather than as fixture ids so that the coverage survives a rename, and so that adding a new
# storage kind to strategy.go without a fixture is a lint failure rather than a gap.
NEGATIVE_SET = {
    "deleted volume data": {"PersistentVolumeClaim", "PersistentVolume", "ComputeDisk", "StorageBucket", "SQLInstance"},
    "a released address": {"ComputeAddress", "ComputeGlobalAddress"},
    "a rotated credential": {"IAMServiceAccountKey"},
}

# The fail-closed rows of 06 §4.4, matched on the op field that triggers them.
FAIL_CLOSED = {
    "snapshot capture failed": "snapshotFailed",
    "no reference index wired": "noReferenceIndex",
}

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def table_verbs() -> list[str]:
    """Read the supported verbs out of StrategyFor's switch.

    Parsed from the source for the same reason the classifier lint parses floor.go: a verb added to
    the table and not to the corpus is exactly the gap this check exists to find, and a list retyped
    here would be updated in the same commit that added the verb, by the same person, and would
    therefore never catch anything.
    """
    text = STRATEGY_GO.read_text()
    fn = re.search(r"func StrategyFor\(.*?\n}", text, re.S)
    if not fn:
        fail(f"could not find StrategyFor in {STRATEGY_GO}")
        return []
    verbs = re.findall(r'^\tcase "([a-z]+)":', fn.group(0), re.M)
    if not verbs:
        fail("StrategyFor has no case arms; the parse is wrong or the table moved")
    return verbs


def non_recreatable_kinds() -> set[str]:
    """Read `nonRecreatableKinds` out of strategy.go.

    Scoped to that one var block on purpose. A regex over the whole file for `Kind: "..."` also
    picks up `effectfulKinds` and `cloudInverses`, and the false positive it produces is a nasty
    one: `create Job` is refused for a completely different reason, and a lint that called Job
    non-recreatable would demand a fixture asserting something untrue about it.
    """
    text = STRATEGY_GO.read_text()
    block = re.search(r"^var nonRecreatableKinds = \[\]classify\.KindRef\{$(.*?)^\}$", text, re.S | re.M)
    if not block:
        fail(f"could not find nonRecreatableKinds in {STRATEGY_GO}")
        return set()
    kinds = set(re.findall(r'Kind:\s*"([A-Za-z]+)"', block.group(1)))
    if len(kinds) < 10:
        fail(f"parsed only {len(kinds)} non-recreatable kinds; the parse is wrong or the list moved")
    return kinds


def ops_of(case: dict) -> list[dict]:
    if case.get("operation") is not None:
        return [case["operation"]]
    return case.get("operations") or []


def main() -> int:
    try:
        doc = load_corpus(CORPUS.read_text())
    except CorpusSyntaxError as e:
        print(f"FAIL: {CORPUS.name}: {e}", file=sys.stderr)
        return 1

    cases = doc.get("cases") or []
    if len(cases) < MIN_CASES:
        fail(f"{len(cases)} cases, want at least {MIN_CASES}: a corpus that shrinks is one somebody deleted the awkward cases out of")

    verbs = table_verbs()
    planned_verbs: set[str] = set()
    seen_strategies: set[str] = set()
    negative_kinds: set[str] = set()
    fail_closed_seen: set[str] = set()
    ids: set[str] = set()

    for case in cases:
        cid = case.get("id")
        if not cid:
            fail("a case has no id")
            continue
        if cid in ids:
            fail(f"{cid}: duplicate id; the second one silently replaces the first in every report")
        ids.add(cid)

        unknown = set(case) - CASE_KEYS
        if unknown:
            fail(f"{cid}: unknown key(s) {sorted(unknown)}")

        if (case.get("operation") is None) == (not case.get("operations")):
            fail(f"{cid}: set exactly one of `operation` and `operations`")

        expect = case.get("expect")
        if not isinstance(expect, dict):
            fail(f"{cid}: no expect block, so the case asserts nothing")
            continue
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            fail(f"{cid}: unknown expect key(s) {sorted(unknown)}")

        strategy = expect.get("strategy")
        if strategy not in STRATEGIES:
            fail(f"{cid}: strategy {strategy!r} is not one of {sorted(STRATEGIES)}")
            continue
        seen_strategies.add(strategy)

        undoable = bool(expect.get("undoable"))
        if undoable != (strategy != "none"):
            fail(f"{cid}: undoable={undoable} contradicts strategy={strategy!r}; Undoable() is defined as strategy != none")
        if undoable and not expect.get("stepOps"):
            fail(f"{cid}: undoable with no expected stepOps, so the case does not check what the plan would do")
        if not undoable and not expect.get("refusalContains"):
            fail(f"{cid}: refuses without asserting a reason; the reason is what a human reads in the approval prompt")

        for op in ops_of(case):
            if not isinstance(op, dict):
                fail(f"{cid}: an operation is not a mapping")
                continue
            unknown = set(op) - OP_KEYS
            if unknown:
                fail(f"{cid}: unknown operation key(s) {sorted(unknown)}")
            target = op.get("target") or {}
            unknown = set(target) - TARGET_KEYS
            if unknown:
                fail(f"{cid}: unknown target key(s) {sorted(unknown)}")
            if not target.get("kind") or not target.get("name"):
                fail(f"{cid}: every target needs a kind and a name")

            verb = op.get("verb")
            if undoable and verb:
                planned_verbs.add(verb)
            if not undoable:
                if target.get("kind"):
                    negative_kinds.add(target["kind"])
                for label, field in FAIL_CLOSED.items():
                    if op.get(field):
                        fail_closed_seen.add(label)

    # 1. every verb in the table has a case that produces a plan
    for verb in verbs:
        if verb not in planned_verbs:
            fail(
                f"verb {verb!r} is in the 06 §4.3.1 table and no fixture produces a plan for it; "
                "its inverse has never been seen to work"
            )

    # 2. every strategy is reachable from the corpus
    for s in sorted(STRATEGIES):
        if s not in seen_strategies:
            fail(f"no fixture produces strategy {s!r}")

    # 3. the negative set of 09 §7.3
    for effect, kinds in NEGATIVE_SET.items():
        if not (kinds & negative_kinds):
            fail(
                f"the negative set has no case for {effect}; 09 §7.3 names it by hand. "
                f"Any of {sorted(kinds)} would cover it."
            )
    for label in FAIL_CLOSED:
        if label not in fail_closed_seen:
            fail(f"no fixture covers the 06 §4.4 fail-closed row for {label}")

    # 4. no fixture claims a non-recreatable kind is undoable
    nonrecreatable = non_recreatable_kinds()
    for case in cases:
        if not case.get("expect", {}).get("undoable"):
            continue
        for op in ops_of(case):
            if op.get("verb") != "delete":
                continue
            kind = (op.get("target") or {}).get("kind")
            if kind in nonrecreatable:
                fail(
                    f"{case['id']}: expects a plan for `delete {kind}`, which strategy.go lists as "
                    "non-recreatable. One of the two has been edited to agree with a bug."
                )

    if FAILURES:
        print(f"FAIL: undo round-trip corpus ({len(FAILURES)} problem(s))", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: undo round-trip corpus -- {len(cases)} cases, "
        f"{len(verbs)} verbs planned, {len(NEGATIVE_SET)} negative-set effects covered"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
