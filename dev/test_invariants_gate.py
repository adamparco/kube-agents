#!/usr/bin/env python3
"""Negative controls for the two arms of `invariants-gate.py` that were found lying (LSN-038).

The gate has sixteen checks and, until this file, no mutation test of its own. That is tolerable
for a check whose failure is loud and immediate; it is not tolerable for a check whose failure is
*silent in the safe direction*, because nothing distinguishes "this property holds" from "this
check has not been able to see the property for six units".

Both arms tested here had that shape.

  * **The machinery probe.** Invariant 7 forbids a write verb on an agent identity until the broker,
    the risk classifier, the journal and the undo path exist and are tested. It probed for them with
    a hardcoded list of two candidate directories each. Two of the four lists were wrong — the
    classifier landed at `internal/broker/classify/` and undo at `internal/broker/undo/`, neither of
    which was guessed — so the gate believed half the machinery was missing from P9-T3a onward. The
    only effect of a false "absent" is extra strictness, and no agent identity had a write verb yet,
    so the gate printed a green tick on every run while holding a false belief. The unit that first
    adds the 06 §2.2.1 broker-operations grant is the one that pays: correct code goes red, and the
    one-line fix is to edit the list — which is [[LSN-036]], the lesson about exactly that reflex.

  * **The deferral closure marker.** `"CLOSED" in blocker.upper()` treats any blocker that merely
    contains the word as a closed row, and a closed row is not asked for a promote-when condition.
    A deferral reading "blocked until the maintenance window closed" would quietly lose the one
    question that makes it a deferral rather than a shrug.

Each test below breaks the property on purpose and asserts the check goes red. A check that cannot
fail is not evidence (09 §6, V-MET-014). Run by `python3 -m unittest discover dev`, which is in
`dev/L0-CHAIN.txt`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_GATE = _REPO / "dev/tests/invariants-gate.py"

# Dashed filename: loaded by path, the same way test_git_destructive_guard.py loads its subject.
_spec = importlib.util.spec_from_file_location("invariants_gate", _GATE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


class MachineryProbesResolve(unittest.TestCase):
    """The positive arm: every declared machinery item is findable in the tree as it stands."""

    def test_all_four_resolve_today(self):
        for name, _globs, _ in gate.MACHINERY:
            found, why = gate.discover_machinery(name)
            self.assertTrue(found, f"{name} did not resolve: {why}")

    def test_the_probe_names_where_it_found_each_one(self):
        # Evidence, not just a boolean. The stale-list bug survived because the probe's answer was
        # never printed; a reviewer reading a green run had nothing to disagree with.
        found, _ = gate.discover_machinery("risk classifier")
        rels = [p.relative_to(gate.REPO).as_posix() for p in found]
        self.assertTrue(
            any("classify" in r for r in rels), f"classifier resolved to {rels}"
        )

    def test_the_classifier_probe_does_not_match_the_routers_intent_classifier(self):
        # `internal/router/classify.go` classifies chat intents. Matching it would report the risk
        # classifier present on the strength of an unrelated subsystem — a probe resolving against
        # the wrong thing is worse than one resolving against nothing, because it fails open.
        found, _ = gate.discover_machinery("risk classifier")
        for p in found:
            self.assertNotIn(
                "internal/router", p.relative_to(gate.REPO).as_posix(),
                "the risk-classifier probe matched the router's intent classifier",
            )

    def test_check_is_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_machinery_probes_resolve())


class MachineryProbeNegativeControls(unittest.TestCase):
    """Mutations of the probe's inputs, each of which must turn the check red."""

    def setUp(self):
        self._machinery = list(gate.MACHINERY)
        self._unbuilt = dict(gate.UNBUILT_UNTIL_PHASE)

    def tearDown(self):
        gate.MACHINERY[:] = self._machinery
        gate.UNBUILT_UNTIL_PHASE.clear()
        gate.UNBUILT_UNTIL_PHASE.update(self._unbuilt)

    def test_a_probe_that_matches_nothing_and_is_undeclared_fails(self):
        # This is the LSN-038 mutation itself: the exact state the gate was in for six units.
        gate.MACHINERY[:] = [("undo path", ["k8s-operator/internal/undo"], None)]
        problems = gate.check_machinery_probes_resolve()
        self.assertTrue(problems)
        self.assertIn("undo path", problems[0])

    def test_declaring_it_unbuilt_for_a_future_phase_is_accepted(self):
        gate.MACHINERY[:] = [("undo path", ["k8s-operator/internal/undo"], None)]
        gate.UNBUILT_UNTIL_PHASE.clear()
        gate.UNBUILT_UNTIL_PHASE["undo path"] = 99
        self.assertEqual([], gate.check_machinery_probes_resolve())

    def test_a_declaration_that_has_expired_fails(self):
        # The ledger says phase 9. A thing declared unbuilt until phase 2 and still not there is
        # either a phase that closed without building it or a probe that cannot see it.
        gate.MACHINERY[:] = [("undo path", ["k8s-operator/internal/undo"], None)]
        gate.UNBUILT_UNTIL_PHASE.clear()
        gate.UNBUILT_UNTIL_PHASE["undo path"] = 2
        problems = gate.check_machinery_probes_resolve()
        self.assertTrue(problems)
        self.assertIn("phase", problems[0])

    def test_a_stale_declaration_on_machinery_that_exists_fails(self):
        gate.UNBUILT_UNTIL_PHASE.clear()
        gate.UNBUILT_UNTIL_PHASE["Action Broker"] = 99
        problems = gate.check_machinery_probes_resolve()
        self.assertTrue(problems)
        self.assertIn("Action Broker", problems[0])

    def test_go_code_with_no_test_beside_it_is_not_machinery(self):
        # Invariant 7 says the machinery must "exist and be tested", so a package with source and
        # no test does not satisfy it. Asserted against a real directory rather than a mock,
        # because the predicate reads files.
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal") as tmp:
            d = pathlib.Path(tmp)
            (d / "undo.go").write_text("package undo\n\nfunc Undo() {}\n")
            gate.MACHINERY[:] = [
                ("undo path", [f"k8s-operator/internal/{d.name}"], None)
            ]
            found, why = gate.discover_machinery("undo path")
            self.assertEqual([], found)
            self.assertIn("no `func Test`", why)

            (d / "undo_test.go").write_text("package undo\n\nfunc TestUndo() {}\n")
            found, why = gate.discover_machinery("undo path")
            self.assertTrue(found, why)


class InvokedByGoTests(unittest.TestCase):
    """`_invoked_by` had a Python-shaped hole: `make test` names no file, so no Go test counted.

    Every branch is exercised here rather than through a lesson row, because today every lesson
    citing a Go test also cites a workflow or a `.py` that already resolves — so the branch is
    reachable in principle and unreached in practice. That is exactly the [[LSN-035]] shape (a rule
    no input reaches looks identical to a rule that holds), and the answer to it is a direct test.
    """

    CHAIN = "make -C k8s-operator test\npython3 -m unittest discover dev\n"

    def test_a_real_controller_test_counts_as_invoked(self):
        self.assertTrue(gate._invoked_by("diff_test.go", self.CHAIN))
        self.assertTrue(
            gate._invoked_by(
                "k8s-operator/internal/broker/execute/diff_test.go", self.CHAIN
            )
        )

    def test_it_is_not_invoked_when_the_chain_never_runs_go(self):
        self.assertFalse(
            gate._invoked_by("diff_test.go", "python3 -m unittest discover dev\n")
        )

    def test_a_go_test_file_that_does_not_exist_is_not_invoked(self):
        self.assertFalse(gate._invoked_by("no_such_thing_test.go", self.CHAIN))

    def test_a_go_file_with_no_test_function_is_not_invoked(self):
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal") as tmp:
            f = pathlib.Path(tmp) / "helpers_only_test.go"
            f.write_text("package x\n\nfunc helper() {}\n")
            self.assertFalse(gate._invoked_by("helpers_only_test.go", self.CHAIN))
            f.write_text("package x\n\nfunc TestX() {}\n")
            self.assertTrue(gate._invoked_by("helpers_only_test.go", self.CHAIN))

    def test_an_e2e_test_is_not_invoked_because_make_test_filters_it_out(self):
        # `go test $(go list ./... | grep -v /e2e)`. Counting an e2e test would close a lesson
        # against something no required check runs.
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal") as tmp:
            d = pathlib.Path(tmp) / "e2e"
            d.mkdir()
            (d / "smoke_e2e_test.go").write_text("package e2e\n\nfunc TestSmoke() {}\n")
            self.assertFalse(gate._invoked_by("smoke_e2e_test.go", self.CHAIN))

    def test_a_go_test_outside_the_operator_module_is_not_invoked(self):
        # `make -C k8s-operator test` only walks that module. A `_test.go` anywhere else is run by
        # nothing automatic, whatever the chain says.
        with tempfile.TemporaryDirectory(dir=gate.REPO / "dev") as tmp:
            f = pathlib.Path(tmp) / "stray_test.go"
            f.write_text("package stray\n\nfunc TestStray() {}\n")
            self.assertFalse(gate._invoked_by("stray_test.go", self.CHAIN))


class DeferralClosureMarker(unittest.TestCase):
    """The marker must mean "this row declares itself closed", not "this row says the word"."""

    def test_the_ledgers_own_closed_row_still_matches(self):
        self.assertTrue(
            gate.CLOSED_MARKER.match("**CLOSED 2026-07-25 by P8-T8a/b.** The scope is now …")
        )

    def test_lowercase_and_struck_through_forms_match(self):
        self.assertTrue(gate.CLOSED_MARKER.match("~~closed 2026-01-01 by X~~"))
        self.assertTrue(gate.CLOSED_MARKER.match("  _Closed_ 2026-01-01 by X"))

    def test_a_blocker_that_merely_uses_the_word_does_not_match(self):
        for blocker in (
            "The L3 maintenance window closed before the run started",
            "No cluster: the closed beta ended",
            "Vendor ticket CLOSED as won't-fix, so the blocker stands",
        ):
            self.assertIsNone(
                gate.CLOSED_MARKER.match(blocker),
                f"{blocker!r} was read as a closed deferral, which drops the promote-when question",
            )

    def test_a_still_open_row_that_mentions_closed_is_still_asked_for_promote_when(self):
        # End to end through the arm that consumes the marker: blank the promote-when cell on a row
        # whose blocker uses the word, and the check must still complain.
        rows = [
            [
                "2026-07-27",
                "V-FAKE-001",
                "The vendor ticket was CLOSED as won't-fix, so nothing runs",
                "harness",
                "",
            ]
        ]
        blocker = rows[0][2]
        self.assertIsNone(gate.CLOSED_MARKER.match(blocker))


if __name__ == "__main__":
    unittest.main()
