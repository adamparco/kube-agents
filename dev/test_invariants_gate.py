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
        # `prefix="."` is load-bearing: this directory lives inside the Go module, and `./...`
        # (controller-gen, go build, go vet) skips dot-directories. Without it a concurrent
        # `make -C k8s-operator test` walks in, the directory is deleted underneath it, and
        # `manifests` dies on a file nobody wrote ([[LSN-058]]). Python globbing still sees it.
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal", prefix=".") as tmp:
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
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal", prefix=".") as tmp:
            f = pathlib.Path(tmp) / "helpers_only_test.go"
            f.write_text("package x\n\nfunc helper() {}\n")
            self.assertFalse(gate._invoked_by("helpers_only_test.go", self.CHAIN))
            f.write_text("package x\n\nfunc TestX() {}\n")
            self.assertTrue(gate._invoked_by("helpers_only_test.go", self.CHAIN))

    def test_an_e2e_test_is_not_invoked_because_make_test_filters_it_out(self):
        # `go test $(go list ./... | grep -v /e2e)`. Counting an e2e test would close a lesson
        # against something no required check runs.
        with tempfile.TemporaryDirectory(dir=gate.REPO / "k8s-operator/internal", prefix=".") as tmp:
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


_MARKED_ROLE = """apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kubeagents-actor-tenant-readonly
  namespace: kubeagents-tenant-probe
  labels:
    kube-agents/tier: platform
    kube-agents/test-only-grant: "true"
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
"""
_FIXTURE = "dev/verify/fixtures/actor-tenant-grant.yaml"


class TestOnlyGrantsAreConfined(unittest.TestCase):
    """V-CTN-037's `¬`. Five properties, one control each, each control blinded to the other four.

    The corpus is injected rather than written to disk. `check_test_only_grants_are_confined`
    reaches the tree through exactly one call — `read_repo_files(REPO)` — so replacing that name in
    the module's namespace substitutes the whole world the check can see, and a control cannot
    accidentally leave an over-grant in the working tree if it fails partway. The properties are
    about paths, and a `tempfile` corpus would have to fake those anyway.

    Each control asserts WHICH property caught it, and most assert that exactly one did
    ([[LSN-035]]): five properties over the same documents overlap enough that a mutation aimed at
    P5 can land on P4 first, and the narrow rule underneath then accumulates a control that has
    never executed it.
    """

    def setUp(self):
        self._read = gate.read_repo_files

    def tearDown(self):
        gate.read_repo_files = self._read

    def _corpus(self, files):
        gate.read_repo_files = lambda *a, **k: dict(files)
        return gate.check_test_only_grants_are_confined()

    # -- the positive arm, against the tree as it stands ------------------------------------

    def test_the_real_tree_is_green(self):
        self.assertEqual([], gate.check_test_only_grants_are_confined())

    def test_the_real_tree_actually_contains_a_marked_grant(self):
        # The green above is only evidence if the population is non-empty. This is the half of
        # [[LSN-038]] that a passing check cannot tell you: `_rbac_documents` returning nothing and
        # every property holding print the same tick.
        corpus = gate.read_repo_files(gate.REPO)
        marked = [d for d in gate._rbac_documents(corpus) if gate.TEST_ONLY_MARKER in d[3]]
        self.assertTrue(marked, "no marked RBAC document in the tree; the check is over nothing")
        self.assertTrue(all(rel.startswith("dev/") for rel, _, _, _ in marked))

    # -- P1: the marker is confined to dev/ ---------------------------------------------------

    def test_p1_the_same_grant_in_an_install_path_fails(self):
        problems = self._corpus({"k8s-operator/scripts/tenant-grant.yaml": _MARKED_ROLE})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("not under dev/", problems[0])

    def test_p1_prose_may_discuss_the_marker(self):
        # 09 §6, invariants.md and the phase breakdown all name it. A rule that forbade that would
        # be a rule against documenting the rule.
        self.assertEqual(
            [],
            self._corpus(
                {
                    _FIXTURE: _MARKED_ROLE,
                    "docs/design/09-verification-and-validation.md": (
                        f"V-CTN-037 asserts `{gate.TEST_ONLY_MARKER}` never leaves dev/."
                    ),
                }
            ),
        )

    # -- P2: an unmarked fixture cannot dodge P1 ----------------------------------------------

    def test_p2_an_unmarked_rbac_document_under_dev_fails(self):
        problems = self._corpus(
            {
                _FIXTURE: _MARKED_ROLE,
                "dev/verify/fixtures/other.yaml": _MARKED_ROLE.replace(
                    f'    {gate.TEST_ONLY_MARKER}: "true"\n', ""
                ),
            }
        )
        self.assertEqual(1, len(problems), problems)
        self.assertIn("carries no", problems[0])

    # -- P3: nothing outside dev/ names the file ----------------------------------------------

    def test_p3_an_install_script_referencing_the_fixture_fails(self):
        problems = self._corpus(
            {
                _FIXTURE: _MARKED_ROLE,
                "k8s-operator/scripts/provision_99_grant.sh": (
                    "kubectl apply -f ../../dev/verify/fixtures/actor-tenant-grant.yaml\n"
                ),
            }
        )
        self.assertEqual(1, len(problems), problems)
        self.assertIn("references", problems[0])

    # -- P4: a test-only grant is never cluster-scoped ----------------------------------------

    def test_p4_a_cluster_scoped_marked_grant_fails(self):
        problems = self._corpus({_FIXTURE: _MARKED_ROLE.replace("kind: Role", "kind: ClusterRole")})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("must be namespaced", problems[0])

    def test_p4_a_marked_rolebinding_is_fine(self):
        binding = (
            "apiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: RoleBinding\n"
            "metadata:\n"
            "  name: t\n"
            "  namespace: kubeagents-tenant-probe\n"
            "  labels:\n"
            f'    {gate.TEST_ONLY_MARKER}: "true"\n'
            "roleRef:\n"
            "  apiGroup: rbac.authorization.k8s.io\n"
            "  kind: Role\n"
            "  name: t\n"
        )
        # The binding's roleRef legitimately names the RBAC API group. P5 must not read that as a
        # grant over RBAC — a binding carries no rules at all.
        self.assertEqual([], self._corpus({_FIXTURE: _MARKED_ROLE + "---\n" + binding}))

    # -- P5: the blast radius of a fixture that did escape ------------------------------------

    def test_p5_an_escalation_verb_fails(self):
        for verb in sorted(gate.ESCALATION_VERBS):
            with self.subTest(verb=verb):
                problems = self._corpus(
                    {_FIXTURE: _MARKED_ROLE.replace('"watch"', f'"watch", "{verb}"')}
                )
                self.assertEqual(1, len(problems), problems)
                self.assertIn("LSN-004", problems[0])

    def test_p5_a_grant_over_rbac_itself_fails(self):
        problems = self._corpus(
            {_FIXTURE: _MARKED_ROLE.replace('apiGroups: [""]', f'apiGroups: ["{gate.RBAC_API_GROUP}"]')}
        )
        self.assertEqual(1, len(problems), problems)
        self.assertIn("widen", problems[0])

    # -- P6: the check refuses to be green over nothing ---------------------------------------

    def test_p6_an_empty_corpus_is_vacuous_not_green(self):
        problems = self._corpus({})
        self.assertEqual(1, len(problems))
        self.assertIn("VACUOUS", problems[0])

    def test_p6_renaming_the_marker_is_vacuous_not_green(self):
        # The [[LSN-038]] mutation: the fixture is still there, still an over-grant risk, and the
        # check can no longer see it. Silent in the safe direction is the failure mode this whole
        # file exists for.
        problems = self._corpus(
            {_FIXTURE: _MARKED_ROLE.replace(gate.TEST_ONLY_MARKER, "kube-agents/fixture")}
        )
        self.assertTrue(problems)
        self.assertIn("VACUOUS", problems[0])


# ==================================================================================================
# The 2026-07-30 improvement pass: LSN-051 through LSN-055
#
# All five of these arms pin PROSE — a row in `binding.md`, a sentence in a skill, a trigger in a
# workflow. That is a shape worth being suspicious of, because a check that greps a document is one
# rewording away from being green over nothing, and it fails silent in the safe direction exactly
# like the two arms at the top of this file did. So every arm below gets the same treatment: green
# on the tree as it stands, and red on a tree where the thing it names has been removed.
#
# The mutations are applied to COPIES in a temp directory, with the gate's path constant repointed
# at them. Not to the real files: a mutation test that edits the repo and restores it is the
# LSN-022 shape, and the restore is the half that fails.
# ==================================================================================================


class _PinnedProse(unittest.TestCase):
    """Base for the five arms: repoint one of the gate's path constants at a mutated copy."""

    def setUp(self):
        # Inside the repo, because every failure message the gate builds ends in
        # `.relative_to(REPO)`. A copy under /tmp makes the check raise instead of reporting, and
        # `main()` turns a raise into GATE ERROR — which is a different outcome from the red these
        # controls are asserting. Untracked, so no corpus-reading arm sees it.
        self._tmp = tempfile.TemporaryDirectory(dir=gate.REPO / "dev")
        self.addCleanup(self._tmp.cleanup)
        self._saved: dict[str, object] = {}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(gate, name, value)

    def mutate(self, const: str, *replacements: tuple[str, str]) -> None:
        """Copy the file at `gate.<const>`, apply `find -> replace`, and repoint the constant.

        Each needle must appear at least once; a mutation that lands nowhere produces a green run
        that reads as "the check caught it" and is the [[LSN-048]] shape one layer up.
        """
        original = getattr(gate, const)
        self._saved.setdefault(const, original)
        text = original.read_text(encoding="utf-8")
        for find, replace in replacements:
            self.assertIn(find, text, f"stale needle for {const}: {find!r}")
            text = text.replace(find, replace)
        copy = pathlib.Path(self._tmp.name) / original.name
        copy.write_text(text, encoding="utf-8")
        setattr(gate, const, copy)


class EnvtestIsRunByTheCommandCheckpointNames(_PinnedProse):
    """[[LSN-054]]: a skipped envtest reports `ok`, so the rule is about WHICH command is named."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_envtest_is_run_by_the_command_checkpoint_names())

    def test_the_tree_really_does_have_envtest_packages_to_protect(self):
        # Without this the arm above is VACUOUS-but-green-looking on a tree that dropped envtest.
        self.assertTrue(gate._envtest_gated_packages())

    def test_binding_dropping_the_make_target_fails(self):
        self.mutate("BINDING", ("make -C k8s-operator test", "go test ./..."))
        problems = gate.check_envtest_is_run_by_the_command_checkpoint_names()
        self.assertTrue(problems)
        self.assertTrue(any("§Build" in p for p in problems), problems)

    def test_binding_keeping_the_command_but_dropping_the_reason_fails(self):
        # The subtler regression: two rows that look interchangeable, and the faster one wins.
        self.mutate("BINDING", ("KUBEBUILDER_ASSETS", "the envtest binaries"))
        problems = gate.check_envtest_is_run_by_the_command_checkpoint_names()
        self.assertTrue(problems)
        self.assertTrue(any("interchangeable" in p or "explains" in p for p in problems), problems)

    def test_checkpoint_dropping_the_make_target_fails(self):
        self.mutate("HARNESS_RUN_SKILL", ("make -C k8s-operator test", "go test ./..."))
        problems = gate.check_envtest_is_run_by_the_command_checkpoint_names()
        self.assertTrue(problems)
        self.assertTrue(any("CHECKPOINT" in p for p in problems), problems)

    def test_protocol_dropping_tests_from_the_done_conditions_fails(self):
        self.mutate(
            "PROTOCOL",
            ("## 3. The unit of work", "## 3. The unit of work\n\n<!--STRIP-->"),
        )
        # Strip §3 down to what it said before this pass: build/format/lint, no mention of a test.
        p = getattr(gate, "PROTOCOL")
        body = p.read_text(encoding="utf-8")
        head, _, rest = body.partition("<!--STRIP-->")
        _, _, after = rest.partition("\n## 4.")
        p.write_text(head + "\n1. The implementation exists and build/format/lint pass.\n"
                     "2. Every check ID is run and green.\n3. The ledger is updated.\n"
                     "4. Work is committed on the phase branch.\n\n## 4." + after,
                     encoding="utf-8")
        problems = gate.check_envtest_is_run_by_the_command_checkpoint_names()
        self.assertTrue(problems)
        self.assertTrue(any("PROTOCOL" in p for p in problems), problems)


class MutationSpecsDeclareRequiredEnv(_PinnedProse):
    """[[LSN-054]] one level down: `go test -list` compiles, so rule 5 cannot see a skipping test."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_mutation_specs_declare_required_env())

    def test_a_spec_over_an_envtest_package_without_the_declaration_fails(self):
        import json

        with tempfile.TemporaryDirectory(dir=gate.REPO / "verification/mutants") as tmp:
            # A spec in a subdirectory is not globbed, so write it beside the real ones instead and
            # remove it in the same block. glob("*.json") is non-recursive, hence the explicit path.
            stray = gate.REPO / "verification/mutants" / f"{pathlib.Path(tmp).name}.json"
            stray.write_text(
                json.dumps(
                    {
                        "suite": {
                            "kind": "go",
                            "dir": "k8s-operator",
                            "packages": ["./internal/controller/..."],
                        },
                        "mutants": [],
                    }
                )
            )
            try:
                problems = gate.check_mutation_specs_declare_required_env()
            finally:
                stray.unlink()
        self.assertTrue(problems)
        self.assertIn("requires_env", problems[0])

    def test_a_spec_over_a_package_with_no_envtest_is_not_asked_for_one(self):
        import json

        with tempfile.TemporaryDirectory(dir=gate.REPO / "verification/mutants") as tmp:
            stray = gate.REPO / "verification/mutants" / f"{pathlib.Path(tmp).name}.json"
            stray.write_text(
                json.dumps(
                    {
                        "suite": {
                            "kind": "go",
                            "dir": "k8s-operator",
                            "packages": ["./internal/agentlabels/"],
                        },
                        "mutants": [],
                    }
                )
            )
            try:
                self.assertEqual([], gate.check_mutation_specs_declare_required_env())
            finally:
                stray.unlink()


class CheckpointCommitsReachCI(_PinnedProse):
    """[[LSN-055]]: two halves, each useless alone, so the check asserts the pair."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_checkpoint_commits_reach_ci())

    def test_dropping_the_push_cadence_fails(self):
        self.mutate(
            "BINDING",
            ("`git push origin HEAD` at every CHECKPOINT", "`git push origin HEAD` before the PR"),
        )
        problems = gate.check_checkpoint_commits_reach_ci()
        self.assertTrue(problems)
        self.assertTrue(any("§Branching" in p for p in problems), problems)

    def test_checkpoint_not_naming_the_push_fails(self):
        self.mutate("HARNESS_RUN_SKILL", ("git push origin HEAD", "commit"))
        problems = gate.check_checkpoint_commits_reach_ci()
        self.assertTrue(problems)
        self.assertTrue(any("no longer tells the unit to push" in p for p in problems), problems)

    def test_restoring_the_main_only_push_trigger_fails(self):
        # The exact configuration that produced the lesson: 25 CHECKPOINT commits, one CI run.
        self._saved.setdefault("WORKFLOWS", gate.WORKFLOWS)
        tmp = pathlib.Path(self._tmp.name) / "workflows"
        tmp.mkdir()
        src = gate.WORKFLOWS / "k8s-operator-test.yml"
        (tmp / "k8s-operator-test.yml").write_text(
            src.read_text(encoding="utf-8").replace("  push:\n", "  push:\n    branches: [main]\n"),
            encoding="utf-8",
        )
        gate.WORKFLOWS = tmp
        problems = gate.check_checkpoint_commits_reach_ci()
        self.assertTrue(problems)
        self.assertTrue(any("branch-filtered" in p for p in problems), problems)

    def test_removing_the_push_trigger_entirely_fails(self):
        self._saved.setdefault("WORKFLOWS", gate.WORKFLOWS)
        tmp = pathlib.Path(self._tmp.name) / "workflows2"
        tmp.mkdir()
        src = gate.WORKFLOWS / "k8s-operator-test.yml"
        (tmp / "k8s-operator-test.yml").write_text(
            src.read_text(encoding="utf-8").replace("  push:\n", ""), encoding="utf-8"
        )
        gate.WORKFLOWS = tmp
        problems = gate.check_checkpoint_commits_reach_ci()
        self.assertTrue(problems)
        self.assertTrue(any("no `push:` trigger" in p for p in problems), problems)

    def test_a_missing_workflow_is_vacuous_not_green(self):
        self._saved.setdefault("WORKFLOWS", gate.WORKFLOWS)
        gate.WORKFLOWS = pathlib.Path(self._tmp.name) / "gone"
        problems = gate.check_checkpoint_commits_reach_ci()
        self.assertTrue(problems)
        self.assertIn("VACUOUS", problems[-1])


class TheRatchetBaselineCoversTheCorpus(_PinnedProse):
    """[[LSN-056]]: the ratchet was wound at 194 tests, the suite reached 1290, and it kept ticking."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_the_ratchet_baseline_covers_the_corpus())

    def _with_baseline(self, payload: dict) -> list[str]:
        import json

        self._saved.setdefault("BASELINE", gate.BASELINE)
        f = pathlib.Path(self._tmp.name) / "assertion-baseline.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        gate.BASELINE = f
        return gate.check_the_ratchet_baseline_covers_the_corpus()

    def test_the_state_this_lesson_was_found_in_fails(self):
        # A baseline holding a strict subset of the corpus: exactly what was committed on
        # 2026-07-30, and exactly what `check_assertion_ratchet` reports as green.
        full = gate.inventory()
        one_file = sorted(full)[0]
        problems = self._with_baseline({"inventory": {one_file: full[one_file]}, "retired": {}})
        self.assertTrue(problems)
        self.assertIn("--update-baseline", problems[0])
        self.assertIn("does not protect them", problems[0])

    def test_the_old_ratchet_arm_is_green_on_that_same_baseline(self):
        # The point of the new arm. If this ever starts failing, the two arms have converged and
        # one of them is redundant -- which is a finding, not a reason to delete this test.
        full = gate.inventory()
        one_file = sorted(full)[0]
        self._saved.setdefault("BASELINE", gate.BASELINE)
        import json

        f = pathlib.Path(self._tmp.name) / "subset.json"
        f.write_text(json.dumps({"inventory": {one_file: full[one_file]}, "retired": {}}))
        gate.BASELINE = f
        self.assertEqual([], gate.check_assertion_ratchet())

    def test_a_retired_test_is_not_demanded_back(self):
        full = gate.inventory()
        rel = sorted(full)[0]
        name = full[rel][0]
        trimmed = {k: (v if k != rel else v[1:]) for k, v in full.items()}
        problems = self._with_baseline(
            {"inventory": trimmed, "retired": {f"{rel}::{name}": "replaced by something"}}
        )
        self.assertEqual([], problems)

    def test_an_empty_extractor_is_vacuous_not_green(self):
        self._saved.setdefault("inventory", gate.inventory)
        gate.inventory = lambda: {}
        problems = gate.check_the_ratchet_baseline_covers_the_corpus()
        self.assertTrue(problems)
        self.assertIn("VACUOUS", problems[0])


class SpecContradictionHaltsCiteBothSides(_PinnedProse):
    """[[LSN-051]]: a contradiction is a relation between two sentences; one halt carried one."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_spec_contradiction_halts_cite_both_sides())

    def test_the_skill_dropping_the_rule_fails(self):
        self.mutate(
            "HARNESS_RUN_SKILL",
            ("must quote BOTH statements", "should be recorded carefully"),
            ("Two\ncitations, two verbatim quotes", "One\ncitation, one verbatim quote"),
        )
        problems = gate.check_spec_contradiction_halts_cite_both_sides()
        self.assertTrue(problems)
        self.assertTrue(any("BOTH" in p for p in problems), problems)

    def test_a_halt_row_citing_one_section_fails(self):
        self._saved.setdefault("LEDGER", gate.LEDGER)
        led = pathlib.Path(self._tmp.name) / "LEDGER.md"
        led.write_text(
            "| 2026-07-30 | 9 | **T HALTED (PROTOCOL §8.5)** — 06 §2.2.1's grant cannot run "
            "the gates it is required to run. |\n",
            encoding="utf-8",
        )
        gate.LEDGER = led
        problems = gate.check_spec_contradiction_halts_cite_both_sides()
        self.assertTrue(problems)
        self.assertIn("fewer than two citations", problems[-1])

    def test_a_halt_row_citing_both_sections_passes(self):
        self._saved.setdefault("LEDGER", gate.LEDGER)
        led = pathlib.Path(self._tmp.name) / "LEDGER2.md"
        led.write_text(
            "| 2026-07-30 | 9 | **T HALTED (PROTOCOL §8.5)** — 06 §2.2 says _\"a\"_ and "
            "03 §4.2 says _\"not a\"_. |\n",
            encoding="utf-8",
        )
        gate.LEDGER = led
        self.assertEqual([], gate.check_spec_contradiction_halts_cite_both_sides())

    def test_a_withdrawn_halt_row_is_left_alone(self):
        # Struck-through rows are kept deliberately as a record of a halt that was wrong. Judging
        # them every run is how a check becomes noise and then becomes deleted.
        self._saved.setdefault("LEDGER", gate.LEDGER)
        led = pathlib.Path(self._tmp.name) / "LEDGER3.md"
        led.write_text(
            "| ~~2026-07-30~~ | ~~9~~ | ~~**T HALTED (PROTOCOL §8.5)** — 06 §2.2.1.~~ **WITHDRAWN** |\n",
            encoding="utf-8",
        )
        gate.LEDGER = led
        self.assertEqual([], gate.check_spec_contradiction_halts_cite_both_sides())


class ACheckOnlyUnitExhibitsBothTrees(_PinnedProse):
    """[[LSN-053]]: green on one tree presents the next unit as the thing that broke the check."""

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_a_check_only_unit_exhibits_both_trees())

    def test_the_skill_dropping_the_both_trees_rule_fails(self):
        self.mutate("HARNESS_RUN_SKILL", ("exhibits **both** trees", "is verified"))
        problems = gate.check_a_check_only_unit_exhibits_both_trees()
        self.assertTrue(problems)
        self.assertTrue(any("BOTH trees" in p for p in problems), problems)

    def test_the_skill_dropping_the_negative_control_requirement_fails(self):
        # Demoting the evidence back to a `/tmp` probe is the regression that matters: the rule
        # still reads as satisfied, and the proof stops re-running.
        self.mutate("HARNESS_RUN_SKILL", ("`--negative-control` row", "one-off probe"))
        problems = gate.check_a_check_only_unit_exhibits_both_trees()
        self.assertTrue(problems)
        self.assertTrue(any("negative-control" in p for p in problems), problems)


class NegativeControlsExerciseTheStatementUnderTest(unittest.TestCase):
    """[[LSN-060]]: the ¬ arm was 13/13 green for a statement that had never executed.

    Every arm repoints `gate.VERIFY_DIR` at a synthetic suite, because the property is about the
    SHAPE of a suite rather than about any file in the tree, and a control that edits a real one
    would be measuring today's `dev/verify/` instead of the rule.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=gate.REPO / "dev")
        self.addCleanup(self._tmp.cleanup)
        self._dir = pathlib.Path(self._tmp.name)
        self._saved = gate.VERIFY_DIR
        gate.VERIFY_DIR = self._dir
        self.addCleanup(lambda: setattr(gate, "VERIFY_DIR", self._saved))

    def write(self, name: str, body: str) -> None:
        (self._dir / name).write_text(body, encoding="utf-8")

    WELL_FORMED = (
        '#!/usr/bin/env bash\n'
        '# --negative-control replays the assertion block.\n'
        '# NEGATIVE CONTROL DOES NOT EXERCISE:\n'
        '#   - the HTTP POST to the broker\n'
        '#   - the API-server lookup of the record\n'
        'record_name="ar-$(printf \'%s\' "$action_id" | tr \'[:upper:]\' \'[:lower:]\')"\n'
        '$K get actionrecord "$record_name" -o json\n'
    )

    def test_green_on_a_well_formed_suite(self):
        self.write("good-l2.sh", self.WELL_FORMED)
        self.assertEqual([], gate.check_negative_controls_exercise_the_statement_under_test())

    def test_green_on_the_tree_as_it_stands(self):
        gate.VERIFY_DIR = self._saved
        self.assertEqual([], gate.check_negative_controls_exercise_the_statement_under_test())

    def test_a_lookup_by_raw_action_id_fails(self):
        # THE defect: `journal.RecordName` lowercases and prefixes, so this finds nothing, and the
        # ¬ arm never ran the line because it synthesises the document.
        self.write("bad-l2.sh", self.WELL_FORMED.replace('"$record_name"', '"$action_id"'))
        problems = gate.check_negative_controls_exercise_the_statement_under_test()
        self.assertTrue(problems)
        self.assertTrue(any("looks an ActionRecord up by" in p for p in problems), problems)

    def test_a_control_with_no_declaration_fails(self):
        self.write(
            "bare-l2.sh",
            self.WELL_FORMED.replace("# NEGATIVE CONTROL DOES NOT EXERCISE:\n", "")
            .replace("#   - the HTTP POST to the broker\n", "")
            .replace("#   - the API-server lookup of the record\n", ""),
        )
        problems = gate.check_negative_controls_exercise_the_statement_under_test()
        self.assertTrue(problems)
        self.assertTrue(any("DOES NOT EXERCISE" in p for p in problems), problems)

    def test_an_empty_declaration_fails(self):
        # An empty list is the claim that the control exercises everything, which is the claim the
        # lesson is about. A header with nothing under it must not read as compliance.
        self.write(
            "empty-l2.sh",
            self.WELL_FORMED.replace("#   - the HTTP POST to the broker\n", "").replace(
                "#   - the API-server lookup of the record\n", ""
            ),
        )
        problems = gate.check_negative_controls_exercise_the_statement_under_test()
        self.assertTrue(problems)
        self.assertTrue(any("with no entries under it" in p for p in problems), problems)

    def test_a_suite_with_no_control_needs_no_declaration(self):
        # The declaration is owed by suites that HAVE a ¬ form. Demanding it of every script would
        # make the marker noise, and noise is what the next reader skims.
        self.write("plain-l2.sh", '#!/usr/bin/env bash\n$K get actionrecord "$record_name"\n')
        self.write("good-l2.sh", self.WELL_FORMED)
        self.assertEqual([], gate.check_negative_controls_exercise_the_statement_under_test())

    def test_a_correct_derived_name_containing_id_is_not_flagged(self):
        # `$REC_QUIET_IDLE` in brake-fanout-l2.sh is a DERIVED name that happens to contain `ID`.
        # The first version of this check flagged it, and a check whose first finding is a false
        # positive teaches the next reader to skim its output.
        self.write("good-l2.sh", self.WELL_FORMED)
        self.write(
            "derived-l2.sh",
            '#!/usr/bin/env bash\nREC_QUIET_IDLE="$(rec_name "$X")"\n'
            '$K patch actionrecord "$REC_QUIET_IDLE" --subresource=status\n',
        )
        self.assertEqual([], gate.check_negative_controls_exercise_the_statement_under_test())

    def test_a_tree_with_no_control_at_all_is_reported_as_vacuous(self):
        self.write("plain-l2.sh", '#!/usr/bin/env bash\necho hi\n')
        problems = gate.check_negative_controls_exercise_the_statement_under_test()
        self.assertTrue(any("VACUOUS" in p for p in problems), problems)


class EnvtestControlPlanesAreReaped(_PinnedProse):
    """[[LSN-059]]: the leak is invisible — the suite is green, the machine is just slower.

    Every arm below removes one piece of the fix and leaves a tree where `make test` still runs and
    still passes. That is the whole reason the check exists: nothing about this defect shows up in
    an exit code, so a silent deletion of the wiring reads exactly like a green.
    """

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_envtest_control_planes_are_reaped())

    def test_the_tree_really_does_have_envtest_packages_to_protect(self):
        # Without this the arm above could be the VACUOUS branch, which is a red — but a reader
        # skimming a green suite would not know which branch produced it.
        self.assertTrue(gate._envtest_gated_packages())

    def test_deleting_the_reaper_fails(self):
        self._saved.setdefault("REAPER", gate.REAPER)
        gate.REAPER = pathlib.Path(self._tmp.name) / "reap-envtest.sh"  # never created
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("is gone" in p for p in problems), problems)

    def test_dropping_the_prerequisite_and_keeping_the_trap_fails(self):
        # The regression that would look most reasonable: "the trap already covers it." It does not
        # cover SIGKILL, which is the only death that causes the leak.
        self.mutate(
            "OPERATOR_MAKEFILE",
            ("setup-envtest reap-envtest ## Run tests.", "setup-envtest ## Run tests."),
        )
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("prerequisite" in p for p in problems), problems)

    def test_dropping_the_trap_and_keeping_the_prerequisite_fails(self):
        self.mutate("OPERATOR_MAKEFILE", ("\ttrap 'bash $(REPO_ROOT)/dev/reap-envtest.sh", "\t"))
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("trap" in p for p in problems), problems)

    def test_pointing_the_target_at_something_else_fails(self):
        # The prerequisite is still named, so the Makefile reads as wired. The recipe no longer
        # sweeps anything.
        self.mutate(
            "OPERATOR_MAKEFILE",
            ("\t@bash $(REPO_ROOT)/dev/reap-envtest.sh --dir", "\t@true --dir"),
        )
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("does something else" in p for p in problems), problems)

    def test_dropping_the_ppid_predicate_fails(self):
        # `--all` semantics by default: the sweep the Makefile runs on every `test` would then kill
        # a concurrent `make test` in another terminal. Worse than the leak it fixes.
        self.mutate("REAPER", ("in_scope | awk '$2 == 1'", "in_scope"))
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("ppid == 1" in p for p in problems), problems)

    def test_dropping_the_left_anchor_fails(self):
        # [[LSN-005]] applied to a process: a substring match still finds every orphan, and also
        # finds the etcd somebody is running for real work.
        self.mutate("REAPER", ("index(argv0, root) == 1", "index(argv0, root) > 0"))
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("LEFT EDGE" in p for p in problems), problems)

    def test_dropping_the_root_refusals_fails(self):
        self.mutate("REAPER", ("REFUSING", "warning"))
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("refuses any asset root" in p for p in problems), problems)

    def test_dropping_the_timeout_warning_from_binding_fails(self):
        # The caller half. Deleting this leaves a tree where the sweep still runs and the cohort it
        # sweeps is manufactured fresh on every single invocation.
        self.mutate("BINDING", ("timeout", "bound"))
        problems = gate.check_envtest_control_planes_are_reaped()
        self.assertTrue(problems)
        self.assertTrue(any("explicit timeout" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
