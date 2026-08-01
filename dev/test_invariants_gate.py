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

import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
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


_AGENT_ROLE_WRITE = """apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubeagents-actor-platform
  labels:
    kube-agents/tier: platform
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "patch"]
"""
_AGENT_ROLE_READ = """apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubeagents-reader-platform
  labels:
    kube-agents/tier: platform
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
"""
_AGENT_ROLE_WILDCARD = """apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kubeagents-actor-platform
  labels:
    kube-agents/tier: platform
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["*"]
"""


class WriteVerbsNeverPrecedeMachinery(unittest.TestCase):
    """V-CTN-038's `¬`. Invariant 7 (07 §5), four properties, each control naming its own finding.

    Until this class, invariant 7 was the only arm of the gate whose *decision* had no control. Its
    probe did — `MachineryProbeNegativeControls` above breaks `check_machinery_probes_resolve`,
    which is the [[LSN-038]] repair — but that check answers "can I find the machinery", not "what
    do I do when a write verb turns up". Those are different questions and the second one had never
    been asked of a mutated input.

    Not, as it first appeared, because the tree holds no agent-identity write verb — it holds eleven
    documents that do, the three `actor-grant-*.yaml.template`s among them. The gate is green for the
    other conjunct: `missing_machinery()` has returned `[]` since P8 closed the undo path, so
    `if not absent: continue` is reached on every run and the `failures.append` below it on none. The
    conclusion is the same and the reason matters, because a control written against the wrong reason
    would have asserted a prohibition this check deliberately does not make. A gate whose enforcing
    branch is unexecuted is a gate whose greens are produced by one conjunct alone, and it is the day
    a piece of machinery is removed or renamed — not the day a write verb lands — that finds out.

    Both of the check's inputs are module-level calls — `agent_rbac_documents()` and
    `missing_machinery()` — so replacing those two names substitutes the whole world it can see,
    exactly as `TestOnlyGrantsAreConfined` does with `read_repo_files`. Nothing is written to the
    tree, so a control that fails partway cannot leave an agent-identity over-grant behind it; and
    the two axes can be varied independently, which is the only way to show that the check is a
    conjunction rather than "any write verb fails".

    The four properties:

      P1  a non-read verb on an agent identity, with machinery absent, is a finding naming the
          file, the verb and what is missing;
      P2  the same verb with the machinery present is NOT a finding — invariant 7 is about ordering,
          and a check that refused write verbs outright would be a different, stricter rule that
          Phase 10 could not satisfy;
      P3  a read-only grant is never a finding, however absent the machinery;
      P4  an empty corpus is `VACUOUS:`, not green — [[LSN-035]], and the failure mode the whole
          class exists for.
    """

    def setUp(self):
        self._docs = gate.agent_rbac_documents
        self._absent = gate.missing_machinery

    def tearDown(self):
        gate.agent_rbac_documents = self._docs
        gate.missing_machinery = self._absent

    def _run(self, docs, absent):
        gate.agent_rbac_documents = lambda: [
            (gate.REPO / rel, 0, text) for rel, text in docs
        ]
        gate.missing_machinery = lambda: list(absent)
        return gate.check_write_verbs_have_machinery()

    # -- the positive arm, against the tree as it stands ------------------------------------

    def test_the_real_tree_satisfies_invariant_7(self):
        # Named for the property and not `test_the_real_tree_is_green`, which is what it wanted to
        # be called: `TestOnlyGrantsAreConfined` already has a test by that name, the assertion
        # ratchet's unit is `file::name` over a SET, and two classes in one file sharing a name
        # collapse to one entry -- so either could be deleted with the ratchet green.
        self.assertEqual([], gate.check_write_verbs_have_machinery())

    def test_the_real_tree_is_green_for_the_reason_it_claims(self):
        # Three very different trees print the same tick: one with no agent RBAC at all, one whose
        # grants are read-only, and one whose write grants are legal because the machinery is
        # complete. Only the third is what this tree actually is, so the green is decomposed rather
        # than trusted. If the `missing_machinery()` assertion fails, invariant 7 has a real finding
        # to make and the gate is red. If the last one fails, the tree has become the read-only one
        # and the docstring above is describing something else.
        docs = gate.agent_rbac_documents()
        self.assertTrue(docs, "no agent-identity RBAC in the tree; the check is over nothing")
        self.assertEqual([], gate.missing_machinery())
        writes = [
            (path.relative_to(gate.REPO).as_posix(), verb)
            for path, _n, doc in docs
            for _line, verb in gate._verbs_in(doc)
            if verb not in gate.READ_VERBS
        ]
        self.assertTrue(
            writes,
            "no agent identity holds a non-read verb, so this tree is green by the read-only "
            "conjunct and the docstring above is describing a different tree",
        )

    # -- P1: authority after machinery is the whole invariant ---------------------------------

    def test_p1_a_write_verb_with_machinery_absent_is_a_finding(self):
        problems = self._run(
            [("k8s-operator/config/rbac/actor_platform.yaml", _AGENT_ROLE_WRITE)],
            ["undo path"],
        )
        self.assertEqual(1, len(problems), problems)
        self.assertIn("'patch'", problems[0])
        self.assertIn("undo path", problems[0])
        self.assertIn("authority never precedes machinery", problems[0])
        self.assertIn("actor_platform.yaml", problems[0])

    def test_p1_a_wildcard_verb_is_a_write_verb(self):
        # `*` is not in READ_VERBS, so it must land in the enforcing branch. Spelled out because
        # `*` is the grant a template renders when someone writes `verbs: ["*"]` meaning "the ones
        # I already have", and a set-membership test that happened to be inverted would let the
        # single most dangerous verb through while catching `patch`.
        problems = self._run(
            [("dev/rbac.yaml", _AGENT_ROLE_WILDCARD)],
            ["Action Broker", "undo path"],
        )
        self.assertEqual(1, len(problems), problems)
        self.assertIn("'*'", problems[0])
        self.assertIn("Action Broker, undo path", problems[0])

    def test_p1_every_absent_item_is_named_not_just_the_first(self):
        # The finding is what a human acts on. "the machinery does not exist: undo path" sends them
        # to build one thing; three are missing.
        problems = self._run(
            [("k8s-operator/config/rbac/actor_platform.yaml", _AGENT_ROLE_WRITE)],
            ["Action Broker", "risk classifier", "undo path"],
        )
        self.assertEqual(1, len(problems), problems)
        for name in ("Action Broker", "risk classifier", "undo path"):
            self.assertIn(name, problems[0])

    # -- P2: ordering, not prohibition --------------------------------------------------------

    def test_p2_the_same_write_verb_with_machinery_present_is_allowed(self):
        # The discrimination the class turns on. Without this arm P1's red is equally consistent
        # with a check that fails on every write verb forever, which is not invariant 7 and would
        # have to be deleted in Phase 10 rather than satisfied.
        self.assertEqual(
            [], self._run([("k8s-operator/config/rbac/actor_platform.yaml", _AGENT_ROLE_WRITE)], [])
        )

    # -- P3: read verbs are not authority -----------------------------------------------------

    def test_p3_a_read_only_grant_is_never_a_finding(self):
        self.assertEqual(
            [], self._run([("k8s-operator/config/rbac/reader_platform.yaml", _AGENT_ROLE_READ)],
                          ["Action Broker", "risk classifier", "ActionRecord journal", "undo path"])
        )

    # -- P4: an empty corpus is VACUOUS, not green --------------------------------------------

    def test_p4_no_agent_rbac_at_all_is_vacuous(self):
        problems = self._run([], ["undo path"])
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("VACUOUS:"), problems[0])
        self.assertIn(gate.TIER_LABEL, problems[0])

    def test_p4_vacuity_is_reported_even_when_the_machinery_is_all_present(self):
        # The dangerous spelling is `if not docs and absent`. With the machinery in place the check
        # would then return `[]` for a tree that has lost its tier labels entirely — a green that
        # means the discriminator broke, arriving in exactly the state (all machinery built) that
        # the repo is in from Phase 9 on.
        problems = self._run([], [])
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("VACUOUS:"), problems[0])


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

    # The dispatch arm is load-bearing, not decoration. Until 2026-08-01 this fixture only
    # MENTIONED `--negative-control`, in a comment, and the check admitted it because the check
    # asked `"--negative-control" in text` -- the exact substring bug B-011 was filed about. Now
    # that the corpus gate is `handles()` (AST for Python, a quote-aware lex for shell), a suite
    # that merely names the flag is correctly not a suite that has a ¬ form, and a fixture standing
    # in for "a suite that HAS one" has to actually have one.
    WELL_FORMED = (
        '#!/usr/bin/env bash\n'
        '# --negative-control replays the assertion block.\n'
        'if [ "${1:-}" = "--negative-control" ]; then\n'
        '  replay_assertion_block\n'
        '  exit 0\n'
        'fi\n'
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


class PhaseGateRunsItsOwnRatchet(unittest.TestCase):
    """Planning defect 4: the gate that would have caught the gap had no arm for it.

    The controls here all leave a tree where `verify-phase9.sh` still runs, still has sections A-I,
    and still reports a coherent verdict. That is the shape of the defect being prevented: nothing
    about a gate that omits its own ratchet looks wrong from the outside, which is how 23 unrun
    required checks -- 8 of them BLOCKING-ALWAYS -- sat under a phase whose task ladder was 70/70.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=gate.REPO / "dev")
        self.addCleanup(self._tmp.cleanup)
        self._saved: dict[str, object] = {}
        self._dir = pathlib.Path(self._tmp.name) / "verify"
        self._dir.mkdir()
        self._real = gate.REPO / "dev" / "verify" / "verify-phase9.sh"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(gate, name, value)

    def repoint(self, name: str, value) -> None:
        self._saved.setdefault(name, getattr(gate, name))
        setattr(gate, name, value)

    def stage(self, *replacements: tuple[str, str], name: str = "verify-phase9.sh") -> None:
        """Copy the real phase-9 gate into a scratch dir, mutate it, and point the check there."""
        text = self._real.read_text(encoding="utf-8")
        for find, replace in replacements:
            self.assertIn(find, text, f"stale needle: {find!r}")
            text = text.replace(find, replace)
        (self._dir / name).write_text(text, encoding="utf-8")
        self.repoint("VERIFY_DIR", self._dir)

    def test_green_on_the_tree_as_it_stands(self):
        self.assertEqual([], gate.check_phase_gate_runs_its_own_ratchet())

    def test_the_tree_really_has_a_non_grandfathered_gate(self):
        # Without this, the arm above could be the "everything is grandfathered" branch -- which is
        # itself a red, but a reader skimming a green suite would not know which branch produced it.
        gates = sorted((gate.REPO / "dev" / "verify").glob("verify-phase*.sh"))
        self.assertTrue([g for g in gates if g.name not in {f"verify-phase{n}.sh" for n in range(2, 9)}])

    def test_a_gate_with_no_ratchet_arm_fails(self):
        self.stage(("python3 dev/tests/phase-ratchet-is-asserted.py", "true #"))
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("never invokes" in p for p in problems), problems)

    def test_a_commented_out_ratchet_arm_does_not_count(self):
        # The regression that looks least like one: somebody comments the line out to get a green
        # gate for an unrelated PR and never puts it back.
        self.stage(
            (
                "if python3 dev/tests/phase-ratchet-is-asserted.py",
                "# if python3 dev/tests/phase-ratchet-is-asserted.py\nif true;",
            )
        )
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("never invokes" in p for p in problems), problems)

    def test_auditing_the_wrong_phase_fails(self):
        # One character. A phase-9 gate that audits phase 8's ratchet passes on a phase-8 tree
        # forever, and its own phase is never checked at all.
        self.stage(("--phase 9", "--phase 8"))
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("--phase 9" in p and "one character" in p for p in problems), problems)

    def test_a_prefix_match_on_the_phase_number_is_not_enough(self):
        # `--phase 90` contains `--phase 9`. A substring test would accept it ([[LSN-005]]).
        self.stage(("--phase 9 ", "--phase 90 "))
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("one character" in p for p in problems), problems)

    def test_an_arm_whose_red_never_reaches_the_exit_code_fails(self):
        # The subtlest one: the invocation is there, with the right phase, and its failure branch
        # only prints. A gate section that cannot fail the gate is a comment.
        text = self._real.read_text(encoding="utf-8")
        start = text.index("# ==== J.")
        end = text.index("echo\necho \"====", start)
        section = text[start:end]
        (self._dir / "verify-phase9.sh").write_text(
            text[:start] + section.replace("bad ", "echo ") + text[end:], encoding="utf-8"
        )
        self.repoint("VERIFY_DIR", self._dir)
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("is a comment" in p for p in problems), problems)

    def test_deleting_the_runner_fails(self):
        self.repoint("RATCHET_RUNNER", pathlib.Path(self._tmp.name) / "gone.py")
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_an_empty_corpus_is_a_failure_and_not_a_green(self):
        # A check whose corpus is empty prints the same green as one that passed. This is the arm
        # that stops "grandfather everything" from being the cheapest route to a quiet gate.
        self.repoint("VERIFY_DIR", self._dir)  # empty
        problems = gate.check_phase_gate_runs_its_own_ratchet()
        self.assertTrue(problems)
        self.assertTrue(any("no verify-phase*.sh at all" in p for p in problems), problems)


# RETIRED 2026-07-31 with the arm it tested: `PhaseGatePublishesTheCoverageRemainder`, eight
# controls over `check_phase_gate_publishes_the_coverage_remainder`. That arm asserted that
# `verify-phase<N>.sh` runs V-MET-002, because V-MET-002 was BLOCKING-ALWAYS and off the L0 chain,
# making section K the only place it ran. The load-bearing remainder reached zero on 2026-07-31, the
# live arm moved onto `dev/L0-CHAIN.txt`, and the chain file became the artifact that answers "does
# it run". Retired in the same commit that moved the line, as the arm's own docstring required, so
# neither ever covered it alone. `dev/assertion-baseline.json` is wound down by the same commit --
# a ratchet that falls silently is [[LSN-056]], and one that falls for a recorded reason is not.


def _load_phase_ratchet():
    """Import the ratchet arm by path -- its filename is not an identifier."""
    sys.path.insert(0, str(gate.REPO / "dev" / "tests"))
    try:
        spec = importlib.util.spec_from_file_location(
            "phase_ratchet", gate.REPO / "dev" / "tests" / "phase-ratchet-is-asserted.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


class PhaseRatchetIsAsserted(unittest.TestCase):
    """The derived ratchet audit itself: its own negative control has to stay green and non-empty.

    The check is RED on today's tree by construction, so the suite cannot assert its exit code. What
    it can assert is that the machinery still evaluates something -- including the FUTURE tree case,
    which is the one that proves the arm can go green once T11b-d land ([[LSN-053]]).
    """

    def test_the_negative_control_passes_and_covers_the_future_tree(self):
        proc = subprocess.run(
            [sys.executable, "dev/tests/phase-ratchet-is-asserted.py", "--phase", "9", "--negative-control"],
            cwd=gate.REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("FUTURE TREE", proc.stdout)
        self.assertIn("PASS as required", proc.stdout)

    def test_the_required_set_is_derived_and_not_a_hand_list(self):
        # The whole point: a hand-written list in the checker is one more place to forget, which is
        # the artifact that failed. Both sources must contribute, and neither may be empty.
        mod = _load_phase_ratchet()
        catalog = mod.parse_catalog(mod.SPEC.read_text())
        ratchet = mod.parse_ratchet(mod.SPEC.read_text(), 9, catalog)
        table = mod.parse_acceptance_table((gate.REPO / "docs/build/phase-9.md").read_text(), 9)
        self.assertGreater(len(catalog), 200, "the 09 §6 catalog parse collapsed")
        self.assertGreater(len(ratchet.required), 50, "the 09 §10 phase-9 ratchet parse collapsed")
        # A floor against a COLLAPSE, and deliberately nowhere near today's count. The previous
        # floor was 40 -- one above the 39 the table legitimately holds once the sixteen IDs 09 §6
        # dates after phase 9 are retargeted out of it. A floor set just under the current value is
        # a fingerprint of the artifact rather than a property of the parser: it reddens on the
        # artifact's own correction, and the cheapest way out is to retune it to whatever the
        # document now says, which is how a check stops being evidence.
        self.assertGreater(len(table), 10, "the phase-9 acceptance table parse collapsed")
        # V-ISO-001/002/006 is a slash run in the 09 §10 cell.
        for cid in ("V-ISO-001", "V-ISO-002", "V-ISO-006"):
            self.assertIn(cid, ratchet.required)

    def _control_against(self, table_ids):
        """Run the arm's own `--negative-control` against a phase file naming exactly `table_ids`.

        `stage()` reads three files off disk, so the future tree is exhibited by giving the module a
        temporary repository: the real 09 and the real results.csv, and a phase-9.md whose
        acceptance section has been rewritten. Returns the control's exit code and its output.
        """
        mod = _load_phase_ratchet()
        real = (gate.REPO / "docs/build/phase-9.md").read_text()
        head, rest = real.split(mod.ACCEPTANCE_HEADING, 1)
        tail = "\n## " + rest.split("\n## ", 1)[1] if "\n## " in rest else ""
        synthetic = (
            head
            + mod.ACCEPTANCE_HEADING
            + "\n\n| (a) | "
            + ", ".join(sorted(table_ids))
            + " | L0 | tree |\n"
            + tail
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "docs/build").mkdir(parents=True)
            (root / "docs/design").mkdir(parents=True)
            (root / "verification").mkdir(parents=True)
            (root / "docs/build/phase-9.md").write_text(synthetic)
            (root / "docs/design/09-verification-and-validation.md").write_text(mod.SPEC.read_text())
            (root / "verification/results.csv").write_text(mod.RESULTS.read_text())
            mod.REPO = root
            mod.SPEC = root / "docs/design/09-verification-and-validation.md"
            mod.RESULTS = root / "verification/results.csv"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = mod.negative_control(9)
            return rc, buf.getvalue()

    def _fixture_tables(self, mod=None):
        """The two synthetic acceptance tables the future-tree tests run against.

        ONE definition site, and it reads **09 only** -- never `docs/build/phase-9.md`. That is the
        whole property; see the PARTIAL test's docstring for the failure that bought it.

        `mod` is threaded in rather than loaded here so the test below can hand it a module pointed
        at a different repository. Calling `_load_phase_ratchet()` internally would return a fresh
        module every time and silently ignore the caller's patch -- which it did, and the mutant
        for the sampled-off-the-document defect ESCAPED as a result.
        """
        mod = mod or _load_phase_ratchet()
        spec_text = mod.SPEC.read_text()
        required = sorted(mod.parse_ratchet(spec_text, 9, mod.parse_catalog(spec_text)).required)
        return set(required), set(required[::2])

    def test_the_stageability_guard_refuses_two_identical_hypothetical_tables(self):
        """The detector added above, exercised directly rather than assumed.

        `assert_hypotheticals_distinct` is the only arm that can see a hypothetical acceptance
        table sampled off the live one while the live one still looks wrong. A detector whose only
        evidence is "the thing it detects is not happening today" can be deleted with every gate
        green, so it gets a test of its own.
        """
        mod = _load_phase_ratchet()
        same = ({"V-GAT-001", "V-GAT-002"}, {"V-GAT-001", "V-GAT-002"})
        with self.assertRaises(mod.ParseError) as caught:
            mod.assert_hypotheticals_distinct((("complete", same[0]), ("partial", same[1])))
        self.assertIn("audits one tree twice", str(caught.exception))
        # ...and it must not fire on two genuinely different tables.
        mod.assert_hypotheticals_distinct(
            (("complete", {"V-GAT-001", "V-GAT-002"}), ("partial", {"V-GAT-001"}))
        )

    def test_the_future_tree_fixtures_are_derived_from_09_and_not_from_the_phase_file(self):
        """Swap the phase file for a mangled one; the fixtures must not move.

        This is the arm that catches the defect on TODAY's tree. Both future-tree fixtures were
        once sampled off the live acceptance table, and that is invisible until the day the table
        is corrected -- at which point the fixture collapses onto the real document and the test it
        feeds stops describing a future tree at all. A time bomb with a commit date on it.

        There is no way to see that by running the fixture once, so the property asserted here is
        the structural one: recompute the fixtures against a repository whose phase-9.md carries a
        completely different acceptance table, and require the answer to be identical.
        """
        before = self._fixture_tables()
        mod = _load_phase_ratchet()
        real_repo, real_spec, real_results = mod.REPO, mod.SPEC, mod.RESULTS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root / "docs/build").mkdir(parents=True)
                (root / "docs/design").mkdir(parents=True)
                (root / "verification").mkdir(parents=True)
                (root / "docs/build/phase-9.md").write_text(
                    mod.ACCEPTANCE_HEADING + "\n\n| (a) | V-GAT-001 |\n\n## Something else\n"
                )
                (root / "docs/design/09-verification-and-validation.md").write_text(real_spec.read_text())
                (root / "verification/results.csv").write_text(real_results.read_text())
                mod.REPO, mod.SPEC = root, root / "docs/design/09-verification-and-validation.md"
                mod.RESULTS = root / "verification/results.csv"
                after = self._fixture_tables(mod)
        finally:
            mod.REPO, mod.SPEC, mod.RESULTS = real_repo, real_spec, real_results
        self.assertEqual(
            before,
            after,
            "a future-tree fixture moved when phase-9.md did, so it is sampled off the document it "
            "is supposed to be a hypothetical alternative to",
        )

    def test_the_control_still_stages_when_the_acceptance_table_is_COMPLETE(self):
        """[[LSN-053]] -- the tree the next unit builds, asserted rather than probed once.

        On 2026-07-31 `P9-T11c′` set out to correct phase-9.md's acceptance table and discovered
        that completing it took this control from 20/20 to *unstageable*:

            FAIL: negative control could not be staged: control: no victim for the push-later case

        Three of the five cases `P9-T11a-3` added picked their victim from "required by the
        derivation and NOT named by the table", and completing the table empties that pool. The
        pools only needed the term because `name_in_table` PREPENDED to the real acceptance section
        instead of replacing it, so the live document's IDs leaked into every synthesised tree.

        `P9-T11a-3` exhibited a future tree for the ratchet cases and did not exhibit one for
        "the acceptance table is complete" -- which was the very next unit's tree. This is that
        debt paid: the control is run against a phase file that names every required ID, and it
        must still stage and still catch everything.
        """
        rc, out = self._control_against(self._fixture_tables()[0])
        self.assertNotIn("could not be staged", out, out)
        self.assertEqual(0, rc, out)
        # ...and it must still be the whole control, not the subset that could be staged.
        self.assertIn("20/20", out, out)

    def test_the_control_still_stages_against_a_PARTIAL_acceptance_table(self):
        """The other shape on the ladder: a table naming a proper subset of the required set.

        The complementary direction to the test above -- there the table grows to the full required
        set, here it names only some of it, which is the shape both today's phase file and the one
        `P9-T11c′` writes have. A control that only works on the table as written today is a
        control that expires the next time the phase file is corrected in either direction.

        THE SUBSET IS DERIVED FROM 09, NOT FROM THE LIVE TABLE. The first version of this test
        built its fixture as "the live acceptance table minus what §6 dates after phase 9", and
        `P9-T11c′` -- the unit this test was written to unblock -- then performed exactly that
        subtraction on the real document, leaving nothing to subtract:

            AssertionError: 39 not less than 39 : nothing to retarget -- the fixture proves nothing

        That is the same borrow-the-artifact defect the whole `P9-T11c″` unit was about, sitting
        one level up in the test that was supposed to prove it fixed. Sampling a hypothetical tree
        off the real one produces a hypothetical that expires when the real one moves.
        """
        complete, partial = self._fixture_tables()
        self.assertLess(len(partial), len(complete), "the fixture must be a PROPER subset")
        self.assertTrue(partial, "the fixture must not be empty")
        rc, out = self._control_against(partial)
        self.assertNotIn("could not be staged", out, out)
        self.assertEqual(0, rc, out)
        self.assertIn("20/20", out, out)

    def test_the_acceptance_parse_expands_runs_and_stops_at_the_next_top_level_heading(self):
        # Both halves are asserted on a FIXTURE rather than on the live phase file, because both
        # were previously sampled off it: the ellipsis case read V-RUN-001…006 out of the real
        # table, and 09 §6 dates V-RUN-006 to phase 10, so correcting the phase file broke a test
        # of the parser. A parser test that fails when the document it samples changes legitimately
        # is a test that gets retuned instead of read.
        mod = _load_phase_ratchet()
        fixture = (
            "## Acceptance → check binding\n\n"
            "| (a) | V-RUN-001…005, V-GAT-002 |\n"
            "| (b) | **V-BRK-022…027** |\n\n"
            "### A subsection is still INSIDE the section\n\n"
            "| (c) | V-CTN-004 |\n\n"
            "## Retargeted out of Phase 9 by 09 §6\n\n"
            "| V-BRK-016 | postponed to phase 10 |\n"
        )
        got = mod.parse_acceptance_table(fixture, 9)
        self.assertEqual(
            {f"V-RUN-{n:03d}" for n in range(1, 6)}
            | {f"V-BRK-{n:03d}" for n in range(22, 28)}
            | {"V-GAT-002", "V-CTN-004"},
            got,
        )
        # The two properties the phase file's structure now depends on, stated separately so a
        # failure says which one went. A `###` subsection is INSIDE -- which is why the retarget
        # list has to be a sibling `##` and not a subsection of the acceptance section...
        self.assertIn("V-CTN-004", got)
        # ...and a sibling `##` is OUT, which is the only reason a phase file can carry the record
        # of what it retargeted without going on requiring it.
        self.assertNotIn("V-BRK-016", got)
        # An ellipsis run wrapped in bold still expands: `**V-BRK-022…027**` is how the table
        # writes them, and the run regex has to tolerate the markers sitting outside it.
        self.assertIn("V-BRK-025", got)

    def test_the_ratchet_accumulates_prior_phases_and_honours_the_due_date(self):
        # 09 §10: "once a suite enters the ratchet it never leaves", and 09 §6's Phase column is the
        # per-check due date the preamble calls authoritative. Asserted on named IDs rather than on
        # counts, because a count moves every time the spec gains a row and would be rewritten to
        # whatever the code then produced.
        mod = _load_phase_ratchet()
        catalog = mod.parse_catalog(mod.SPEC.read_text())
        r = mod.parse_ratchet(mod.SPEC.read_text(), 9, catalog)
        # Entered at phase 8 (V-CTN read-side, V-CTR core, V-MET); still required at 9.
        for cid in ("V-CTN-001", "V-CTR-001", "V-MET-013"):
            self.assertIn(cid, r.required, "a suite that entered at phase 8 left the ratchet")
        # 09 §6 dates these after phase 9 -- V-RUN-014 to phase 15 -- so the shadow phase may not be
        # asked for them. They are the reason the filter exists.
        for cid in ("V-BRK-019", "V-RUN-014"):
            self.assertNotIn(cid, r.required, f"{cid} is dated after phase 9 by 09 §6")
            self.assertIn(cid, r.deferred, f"{cid} was dropped without being reported")
        # The 09 §11 V-MET rows carry no phase cell at all; undated means required, not exempt.
        self.assertIsNone(catalog["V-MET-001"].phase)
        self.assertIn("V-MET-001", r.required)
        self.assertIn("V-MET-001", r.undated)
        # Nothing may be removed silently: everything the filter set aside is in a printable note.
        notes = " ".join(r.notes())
        for cid in sorted(r.deferred) + sorted(r.undated):
            self.assertIn(cid, notes, f"{cid} was filtered out of the ratchet without a note")


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


class BacklogStructureIsUniform(unittest.TestCase):
    """`_backlog_structure`: who writes which section, and each table agreeing with its subsections.

    Every mutant below is a shape `docs/build/BACKLOG.md` was actually in on 2026-07-31, which is
    why the arm exists. Four subsections sat under `## Scheduled` for items that had already landed
    or been refused, and five more were titled exactly like undrained inbox items -- so the file's
    two halves disagreed about the state of four ids, and a reader could not tell an archived copy
    from a live request. Both are silent: every other backlog check was green throughout.
    """

    ROW = "| ID | Title | X | On |\n| -- | -- | -- | -- |\n"

    def bodies(self, inbox="_(empty)_\n", scheduled="", refused="", done=""):
        return {
            "Inbox": "**Last drained:** 2026-07-31\n\n" + inbox,
            "Scheduled": scheduled,
            "Refused": refused,
            "Done": done,
        }

    def one_of_each(self):
        """A minimal well-formed file: one row and its subsection, in each archive section."""
        return self.bodies(
            scheduled=self.ROW + "| B-007 | a | x | 2026-07-31 |\n\n### B-007 — a\n\nwhy\n",
            refused=self.ROW + "| B-009 | b | x | 2026-07-31 |\n\n### B-009 — b\n\nwhy\n",
            done=self.ROW + "| B-004 | c | x | 2026-07-30 |\n\n### B-004 — c\n\nwhy\n",
        )

    def test_the_committed_file_satisfies_every_rule(self):
        # The positive arm. Without it the mutants below could all be caught by an arm that simply
        # always fails, which is the failure mode a suite of negative controls cannot see.
        self.assertEqual([], gate._backlog_structure(self.one_of_each()))
        self.assertEqual([], gate.check_backlog_is_drained())

    def test_a_subsection_titled_like_an_inbox_item_is_rejected(self):
        # The shape five subsections were in: `### Reap the envtest control planes — ...` reads as
        # an undrained item, so the archive and the inbox stop being distinguishable.
        b = self.one_of_each()
        b["Scheduled"] = b["Scheduled"].replace(
            "### B-007 — a", "### Reap the envtest control planes"
        )
        problems = gate._backlog_structure(b)
        self.assertTrue(any("names no id" in p for p in problems), problems)

    def test_a_subsection_left_behind_when_its_item_changed_section_is_rejected(self):
        # B-004's reasoning stayed under `## Scheduled` after the item landed in `## Done`. Both
        # directions fire: an orphaned heading here, and a row with no argument there.
        b = self.one_of_each()
        b["Scheduled"] += "\n### B-004 — c\n\nstale reasoning\n"
        b["Done"] = b["Done"].replace("### B-004 — c\n\nwhy\n", "")
        problems = gate._backlog_structure(b)
        self.assertTrue(any("B-004" in p and "no row in that section" in p for p in problems), problems)
        self.assertTrue(any("B-004" in p and "no `### B-004 — …` subsection" in p for p in problems), problems)

    def test_a_row_with_no_subsection_is_rejected(self):
        b = self.one_of_each()
        b["Refused"] += "| B-013 | d | x | 2026-07-31 |\n"
        problems = gate._backlog_structure(b)
        self.assertTrue(any("B-013" in p and "no `###" in p for p in problems), problems)

    def test_an_inbox_item_carrying_an_id_is_rejected(self):
        # A human does not assign ids. An id in the inbox means either a collision with a real item
        # or the harness filing into the one channel it may not write to.
        b = self.one_of_each()
        b["Inbox"] += "\n### B-014 — something the harness wants\n\n- **Added:** 2026-07-31\n"
        problems = gate._backlog_structure(b)
        self.assertTrue(any("carries a `B-nnn` id" in p for p in problems), problems)

    def test_a_bare_id_prefix_in_the_inbox_is_rejected_too(self):
        # `### B-014 the thing` names an id without the ` — ` separator, so `_heading_ids` returns
        # None for it. Keying the inbox rule on that alone would let the harness file here by
        # dropping one dash.
        b = self.one_of_each()
        b["Inbox"] += "\n### B-014 something the harness wants\n"
        problems = gate._backlog_structure(b)
        self.assertTrue(any("carries a `B-nnn` id" in p for p in problems), problems)

    def test_a_joint_heading_covers_both_of_its_ids(self):
        # `### B-001 · B-002 — ...` is legal: one argument really did resolve two items. It must
        # satisfy both rows, and it must not satisfy a third.
        b = self.bodies(
            done=self.ROW
            + "| B-001 | a | x | 2026-07-29 |\n| B-002 | b | x | 2026-07-29 |\n\n"
            + "### B-001 · B-002 — one argument, two items\n\nwhy\n"
        )
        self.assertEqual([], gate._backlog_structure(b))
        b["Done"] = b["Done"].replace(
            "| B-002 | b | x | 2026-07-29 |\n", "| B-002 | b | x | 2026-07-29 |\n| B-003 | c | x | 2026-07-29 |\n"
        )
        self.assertTrue(any("B-003" in p for p in gate._backlog_structure(b)))

    def test_a_fenced_example_heading_is_not_read_as_a_subsection(self):
        # `## How to add an item` shows the block format in a fence. A fenced heading inside an
        # archive section must not be scored as a real subsection either way.
        b = self.one_of_each()
        b["Done"] += "\n```markdown\n### <one-line title>\n```\n"
        self.assertEqual([], gate._backlog_structure(b))

    def test_an_empty_archive_reports_VACUOUS_rather_than_passing(self):
        # Delete every subsection and the row/heading comparison compares two empty sets. That is
        # not the property holding; it is the check having nothing to look at (LSN-035, LSN-038).
        problems = gate._backlog_structure(self.bodies())
        self.assertTrue(any(p.startswith("VACUOUS:") for p in problems), problems)

    def test_it_fails_when_the_skill_stops_telling_the_harness_not_to_write_to_the_inbox(self):
        # The procedural half. The gate and the sentence the loop reads at ORIENT move together, or
        # the next reader of the skill learns a workflow the gate rejects.
        original = gate.HARNESS_RUN_SKILL
        # Inside the repo: the failure messages render paths with `.relative_to(REPO)`, so a
        # fixture parked in /tmp makes the check raise instead of reporting.
        with tempfile.TemporaryDirectory(dir=gate.REPO) as tmp:
            stripped = pathlib.Path(tmp) / "SKILL.md"
            stripped.write_text(original.read_text().replace("never writes to the inbox", "drains"))
            gate.HARNESS_RUN_SKILL = stripped
            try:
                problems = gate._backlog_structure(self.one_of_each())
            finally:
                gate.HARNESS_RUN_SKILL = original
        self.assertTrue(any("never writes to the inbox" in p for p in problems), problems)

    def test_removing_the_structure_rules_from_the_backlog_fails_the_gate(self):
        # `_backlog_structure`'s failures name a convention; deleting its definition site leaves
        # them naming nothing. Asserted through the public check, which is where that half lives.
        original = gate.BACKLOG
        with tempfile.TemporaryDirectory(dir=gate.REPO) as tmp:
            stripped = pathlib.Path(tmp) / "BACKLOG.md"
            stripped.write_text(
                original.read_text().replace("## How this file is structured", "## Notes")
            )
            gate.BACKLOG = stripped
            try:
                problems = gate.check_backlog_is_drained()
            finally:
                gate.BACKLOG = original
        self.assertTrue(any("How this file is structured" in p for p in problems), problems)


class L2LockIsWired(unittest.TestCase):
    """[[LSN-066]] / P12: the lock is taken from one site, and no later trap throws the release away.

    Each arm here is a shape the tree was actually in. The trap arm in particular: seventeen suites
    installed their own `trap cleanup EXIT` after taking the lock, which REPLACES the release
    `l2_lock_guard` had chained, so the lock outlived every one of them. Nothing was red — the next
    acquirer broke the lock as stale and printed a warning — which is why it needs a check rather
    than a reader.

    The link arms repoint `L2_LOCK_LIB` / `PRECONDITIONS_LIB` at synthetic files, and the trap arm
    repoints `_l2_scripts_in_scope`, because the property is about the shape of the wiring and an
    arm that edited the real library to prove a link is load-bearing would be mutating what every
    other check in the gate reads.
    """

    LIB = "l2_lock_guard() { :; }\nl2_lock_release() { :; }\nl2_lock_path() { echo /tmp/x; }\n"
    PRE = (
        "p12_assert_exclusive_l2() {\n"
        '  l2_lock_guard "$ctx"\n'
        "}\n"
        "p10_assert_control_plane_healthy() {\n"
        '  p12_assert_exclusive_l2 "$K" || return 2\n'
        "}\n"
    )
    # Takes the lock (transitively, via P10) and then installs its own EXIT trap. The chained
    # release is what keeps this green.
    SUITE = (
        "#!/usr/bin/env bash\n"
        'p10_assert_control_plane_healthy "$K" "$CTX" || exit 2\n'
        "trap 'cleanup; l2_lock_release' EXIT\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=gate.REPO / "dev")
        self.addCleanup(self._tmp.cleanup)
        self._dir = pathlib.Path(self._tmp.name)
        for attr in ("L2_LOCK_LIB", "PRECONDITIONS_LIB", "POST_LOCK_TRAP_FLOOR",
                     "_l2_scripts_in_scope"):
            saved = getattr(gate, attr)
            self.addCleanup(lambda a=attr, v=saved: setattr(gate, a, v))
        gate.L2_LOCK_LIB = self._dir / "l2-lock.sh"
        gate.PRECONDITIONS_LIB = self._dir / "preconditions.sh"
        gate.L2_LOCK_LIB.write_text(self.LIB, encoding="utf-8")
        gate.PRECONDITIONS_LIB.write_text(self.PRE, encoding="utf-8")
        gate.POST_LOCK_TRAP_FLOOR = 1
        self._real = {
            "L2_LOCK_LIB": gate.REPO / "dev" / "lib" / "l2-lock.sh",
            "PRECONDITIONS_LIB": gate.REPO / "dev" / "lib" / "preconditions.sh",
            "POST_LOCK_TRAP_FLOOR": 17,
            "_l2_scripts_in_scope": gate._l2_scripts_in_scope,
        }
        self.scripts: list[pathlib.Path] = []
        gate._l2_scripts_in_scope = lambda: list(self.scripts)

    def suite(self, name: str, body: str) -> None:
        p = self._dir / name
        p.write_text(body, encoding="utf-8")
        self.scripts.append(p)

    def test_green_on_the_tree_as_it_stands(self):
        """The arm that matters most: the real wiring, real library, real suites, no findings."""
        for attr, value in self._real.items():
            setattr(gate, attr, value)
        self.assertEqual([], gate.check_l2_lock_is_wired())

    def test_green_on_a_well_wired_synthetic_tree(self):
        self.suite("good-l2.sh", self.SUITE)
        self.assertEqual([], gate.check_l2_lock_is_wired())

    def test_an_unchained_post_lock_trap_fails(self):
        # THE defect, verbatim: `trap cleanup EXIT` after the lock. bash replaces the handler.
        self.suite("bad-l2.sh", self.SUITE.replace("'cleanup; l2_lock_release'", "cleanup"))
        problems = gate.check_l2_lock_is_wired()
        self.assertTrue(any("does not chain l2_lock_release" in p for p in problems), problems)

    def test_a_trap_installed_BEFORE_the_lock_is_not_flagged(self):
        # Nine suites in the corpus are in this shape and must stay green: `l2_lock_guard` chains
        # whatever trap it finds, so a trap set earlier is the one it chains, not one that stomps
        # it. A check that flagged these would have its first finding be a false positive.
        self.suite(
            "early-l2.sh",
            "#!/usr/bin/env bash\ntrap cleanup EXIT\n"
            'p10_assert_control_plane_healthy "$K" "$CTX" || exit 2\n',
        )
        self.suite("good-l2.sh", self.SUITE)
        self.assertEqual([], gate.check_l2_lock_is_wired())

    def test_an_indented_trap_is_not_flagged(self):
        # `startup-ordering-l2.sh` clears EXIT inside a subshell. That handler is the subshell's.
        self.suite("nested-l2.sh", self.SUITE + "run() {\n  trap - EXIT\n}\n")
        self.assertEqual([], gate.check_l2_lock_is_wired())

    def test_p10_not_calling_p12_fails(self):
        # The single definition site removed: 30 suites silently stop locking, no per-script diff.
        gate.PRECONDITIONS_LIB.write_text(
            self.PRE.replace('  p12_assert_exclusive_l2 "$K" || return 2\n', "  :\n"),
            encoding="utf-8",
        )
        self.suite("good-l2.sh", self.SUITE)
        problems = gate.check_l2_lock_is_wired()
        self.assertTrue(any("no longer calls p12_assert_exclusive_l2" in p for p in problems),
                        problems)

    def test_p12_not_taking_the_lock_fails(self):
        gate.PRECONDITIONS_LIB.write_text(
            self.PRE.replace('  l2_lock_guard "$ctx"\n', "  :\n"), encoding="utf-8"
        )
        self.suite("good-l2.sh", self.SUITE)
        problems = gate.check_l2_lock_is_wired()
        self.assertTrue(any("no longer calls l2_lock_guard" in p for p in problems), problems)

    def test_a_library_missing_its_release_fails(self):
        gate.L2_LOCK_LIB.write_text(self.LIB.replace("l2_lock_release() { :; }\n", ""),
                                    encoding="utf-8")
        self.suite("good-l2.sh", self.SUITE)
        problems = gate.check_l2_lock_is_wired()
        self.assertTrue(any("no longer defines l2_lock_release" in p for p in problems), problems)

    def test_a_tree_with_no_post_lock_trap_is_reported_as_vacuous(self):
        # The way this check would silently empty out: the scope shrinks, or the trap predicate
        # stops matching. Either way the trap arm is then green about nothing.
        gate.POST_LOCK_TRAP_FLOOR = 17
        self.suite("good-l2.sh", self.SUITE)
        problems = gate.check_l2_lock_is_wired()
        self.assertTrue(any("VACUOUS" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
