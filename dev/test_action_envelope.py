"""V-BRK-028 — the agent-side envelope builder computes the broker's idempotency key.

WHAT THIS CHECK IS FOR
----------------------
`agents/*/scripts/action_envelope.py` is a second implementation of a rule whose first
implementation is Go (`k8s-operator/internal/broker/idempotency.go` + `jcs.go` +
`internal/journal.Sanitize`). It has to exist: 06 §9 puts `idempotencyKey` in the `submit_action`
MCP tool and the agent image is Python. A second definition site of a security rule is only allowed
in this repo when something mechanically compares it to the first ([[LSN-040]], [[LSN-041]]) --
this file is that comparison.

The failure it prevents is not subtle and not gradual. The broker **recomputes** the key and
`CompareIdempotencyKey` refuses a mismatch, so one byte of drift between the two implementations
makes every write from every agent in the fleet refused, reported as `idempotency-key-mismatch`
rather than as the drift it is. And the drift has a security face too: the key is hashed over
operations that have already been through the journal's §4.3.1 redaction, so a Python side that
forgets to digest a Secret's `data` gets both a wrong key and credential material in the hash input.

WHY THERE IS NO GOLDEN FILE
---------------------------
`verification/fixtures/envelopes/valid/` already holds six envelopes, each carrying the key its own
operations hash to, plus `identities.json` naming the identity each was submitted under. The Go
side pins itself against exactly that corpus in `TestValidFixtureIdempotencyKeys`. This file runs
the Python builder over the same six files and asserts the same six keys. The two implementations
are therefore joined through an artifact both already depend on -- there is no second corpus, no
golden output, and nothing that can be regenerated to make a failure go away.

The corpus is not incidental. It covers a Secret `apply` (the sanitizer), a selector fan-out delete
and a three-operation envelope with mixed verbs (the sort order), which are the three places a
re-implementation actually goes wrong.

TIER PARITY
-----------
The module ships in all three tiers, byte-identical, the same way `platform_mcp_server.py` and
`agent_common_server.py` do. Nothing else in the repo currently asserts that for
`agents/*/scripts/` at all -- the three copies of `platform_mcp_server.py` are identical by luck.
This file asserts it for the file it owns, and runs every conformance case against every tier's
copy rather than against one of them, so "the platform tier is right and the other two rotted" is
not a shape this check can pass in.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TIERS = ("platform", "cluster-admin", "developer-team")
MODULE = "action_envelope.py"
FIXTURES = REPO / "verification" / "fixtures" / "envelopes" / "valid"
ENVELOPE_GO = REPO / "k8s-operator" / "internal" / "broker" / "envelope.go"


def tier_path(tier: str) -> Path:
    return REPO / "agents" / tier / "scripts" / MODULE


def load(tier: str):
    """Import a tier's copy under its own module name, so three copies can coexist in one process."""
    path = tier_path(tier)
    spec = importlib.util.spec_from_file_location(f"action_envelope_{tier.replace('-', '_')}", path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixtures() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if p.name != "identities.json")


def identities() -> dict[str, str]:
    return json.loads((FIXTURES / "identities.json").read_text())


class TestTierParity(unittest.TestCase):
    """The module exists in every tier and the copies are byte-identical."""

    def test_every_tier_ships_the_module(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertTrue(
                    tier_path(tier).is_file(),
                    f"{tier} has no {MODULE}; an agent in this tier cannot build an envelope at all",
                )

    def test_the_copies_are_byte_identical(self):
        digests = {tier: hashlib.sha256(tier_path(tier).read_bytes()).hexdigest() for tier in TIERS}
        self.assertEqual(
            len(set(digests.values())),
            1,
            "the tiers' copies of "
            + MODULE
            + " have diverged, so the same operations hash to different keys depending on which "
            f"image the agent runs: {digests}",
        )


class TestFixtureCorpus(unittest.TestCase):
    """The corpus this check is joined through is present and is what we think it is."""

    def test_the_corpus_is_not_empty(self):
        # A conformance test whose corpus vanished passes silently and asserts nothing. The count
        # is pinned low rather than exact: adding a valid fixture should not fail this, deleting
        # them all must.
        self.assertGreaterEqual(len(fixtures()), 6, f"only {len(fixtures())} valid envelope fixtures found in {FIXTURES}")

    def test_every_fixture_has_an_identity(self):
        known = identities()
        for path in fixtures():
            with self.subTest(fixture=path.name):
                self.assertIn(path.name, known, "no entry in identities.json; the key cannot be recomputed without one")

    def test_the_corpus_covers_the_three_places_a_reimplementation_breaks(self):
        """Coverage of the corpus itself, so a fixture deletion cannot quietly narrow this check."""
        bodies = [json.loads(p.read_text()) for p in fixtures()]
        ops = [op for body in bodies for op in body["operations"]]

        secret = [o for o in ops if (o.get("target") or {}).get("kind") == "Secret"]
        self.assertTrue(secret, "no Secret operation in the corpus, so the sanitizer path is unexercised")

        selector = [o for o in ops if o.get("targetSelector")]
        self.assertTrue(selector, "no targetSelector operation, so the fan-out sort-key slot is unexercised")

        multi = [b for b in bodies if len(b["operations"]) > 1]
        self.assertTrue(multi, "no multi-operation envelope, so the operation sort order is unexercised")


class TestIdempotencyKeyConformance(unittest.TestCase):
    """THE JOIN. Every tier's builder, over every valid fixture, against the broker's own answer."""

    def test_every_tier_reproduces_every_fixture_key(self):
        known = identities()
        for tier in TIERS:
            module = load(tier)
            for path in fixtures():
                with self.subTest(tier=tier, fixture=path.name):
                    envelope = json.loads(path.read_text())
                    got = module.compute_idempotency_key(
                        known[path.name], envelope["operations"], envelope.get("dryRun", False)
                    )
                    self.assertEqual(
                        got,
                        envelope["idempotencyKey"],
                        "the Python builder and the Go broker disagree on this envelope's key. The broker "
                        "recomputes and refuses a mismatch, so this is not a cosmetic difference: it is "
                        "every write from this tier being rejected.",
                    )

    def test_dry_run_changes_the_key(self):
        """`dryRun` is inside the hash, so a plan and its apply are different actions.

        Without this the anti-replay index would treat `plan_action` and the `submit_action` that
        follows it as the same submission, and the real write would return the plan's outcome
        without ever executing.
        """
        module = load("platform")
        path = FIXTURES / "platform.scale-deployment.json"
        envelope = json.loads(path.read_text())
        identity = identities()[path.name]

        wet = module.compute_idempotency_key(identity, envelope["operations"], False)
        dry = module.compute_idempotency_key(identity, envelope["operations"], True)
        self.assertNotEqual(wet, dry)
        self.assertEqual(wet, envelope["idempotencyKey"])

    def test_identity_changes_the_key(self):
        """Two tiers submitting the same operations are two different actions.

        A key that ignored the identity would let one agent's submission dedup another's away.
        """
        module = load("platform")
        envelope = json.loads((FIXTURES / "platform.scale-deployment.json").read_text())
        a = module.compute_idempotency_key("platform/adamparco-kage", envelope["operations"])
        b = module.compute_idempotency_key("cluster-admin/adamparco-kage", envelope["operations"])
        self.assertNotEqual(a, b)

    def test_an_empty_identity_is_refused_not_defaulted(self):
        """A key over an empty identity collides across every agent in the cluster."""
        module = load("platform")
        envelope = json.loads((FIXTURES / "platform.scale-deployment.json").read_text())
        with self.assertRaises(module.EnvelopeError):
            module.compute_idempotency_key("", envelope["operations"])


class TestTheCheckCanFail(unittest.TestCase):
    """Negative controls. A conformance test that cannot fail is a conformance test that lies."""

    def setUp(self):
        self.module = load("platform")
        self.path = FIXTURES / "developer-team.apply-secret.json"
        self.envelope = json.loads(self.path.read_text())
        self.identity = identities()[self.path.name]

    def key(self, operations, dry_run=False):
        return self.module.compute_idempotency_key(self.identity, operations, dry_run)

    def test_baseline_matches(self):
        self.assertEqual(self.key(self.envelope["operations"]), self.envelope["idempotencyKey"])

    def test_skipping_the_secret_digest_would_be_caught(self):
        """The sanitizer is inside the hash, not beside it.

        This is the mutation that matters most: it is the one whose *other* consequence is
        credential material in the hash input, and the one a reimplementation is most likely to
        omit because the envelope looks fine without it.
        """
        raw = self.module.canonicalize(
            {
                "agentIdentity": self.identity,
                "dryRun": False,
                # The same operation with sanitisation skipped -- desiredState verbatim.
                "operations": [
                    {"op": "apply", "target": self.envelope["operations"][0]["target"], "desiredState": self.envelope["operations"][0]["desiredState"]}
                ],
            }
        )
        undigested = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertNotEqual(undigested, self.envelope["idempotencyKey"])
        self.assertIn(
            "c3VwZXItc2VjcmV0LXRva2Vu",
            raw.decode(),
            "the fixture no longer carries Secret material, so this negative control proves nothing",
        )

    def test_reordering_operations_does_not_change_the_key(self):
        """The positive half of the sort rule: a retry that reorders is the same write."""
        path = FIXTURES / "platform.multi-operation.json"
        envelope = json.loads(path.read_text())
        identity = identities()[path.name]
        forward = self.module.compute_idempotency_key(identity, envelope["operations"], False)
        reverse = self.module.compute_idempotency_key(identity, list(reversed(envelope["operations"])), False)
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, envelope["idempotencyKey"])

    def test_changing_an_operation_changes_the_key(self):
        """The negative half: two different writes are not one action."""
        mutated = json.loads(json.dumps(self.envelope["operations"]))
        mutated[0]["target"]["name"] = "downstream-api-2"
        self.assertNotEqual(self.key(mutated), self.envelope["idempotencyKey"])

    def test_an_unknown_field_is_dropped_before_hashing(self):
        """The Go side decodes into a struct, so a field it has no name for cannot reach the hash.

        A Python builder that hashed the caller's dict verbatim would compute a key the broker
        never reproduces the moment anything adds a field -- including a future envelope version
        this agent image predates.
        """
        padded = json.loads(json.dumps(self.envelope["operations"]))
        padded[0]["someFutureField"] = {"a": 1}
        self.assertEqual(self.key(padded), self.envelope["idempotencyKey"])


class TestSanitizer(unittest.TestCase):
    """§4.3.1 redaction, at the level the corpus cannot reach.

    Every fixture's Secret payload declares its own `kind`, so the corpus exercises the digest but
    never the *injection* that makes the digest happen for payloads that do not. That is the shape
    the real caller produces: a merge-patch body is `{"data": {...}}` with no apiVersion and no kind
    at all, and without the injection it sails through undigested -- a different key from the
    broker's, and credential material in the hash input.
    """

    def setUp(self):
        self.module = load("platform")

    def test_a_kindless_secret_payload_is_digested_via_the_target_kind(self):
        clean = self.module._sanitize_payload({"data": {"token": "super-secret-token"}}, "Secret")
        self.assertNotIn("super-secret-token", json.dumps(clean))
        self.assertEqual(clean["data"]["token"], "sha256:" + hashlib.sha256(b"super-secret-token").hexdigest())

    def test_the_injected_kind_does_not_become_part_of_the_key(self):
        """A payload that declared its kind and one that relied on the target's are one write."""
        injected = self.module._sanitize_payload({"data": {"token": "t"}}, "Secret")
        self.assertNotIn("kind", injected)

    def test_a_declared_kind_survives(self):
        clean = self.module._sanitize_payload({"kind": "Secret", "data": {"token": "t"}}, "Secret")
        self.assertEqual(clean["kind"], "Secret")

    def test_a_non_secret_payload_is_left_alone(self):
        payload = {"kind": "ConfigMap", "data": {"mode": "fast"}}
        self.assertEqual(self.module._sanitize_payload(payload, "ConfigMap")["data"]["mode"], "fast")

    def test_managed_fields_and_last_applied_are_stripped(self):
        clean = self.module.sanitize(
            {
                "kind": "ConfigMap",
                "metadata": {
                    "name": "settings",
                    "managedFields": [{"manager": "kubectl"}],
                    "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}", "keep": "yes"},
                },
            }
        )
        self.assertNotIn("managedFields", clean["metadata"])
        self.assertNotIn("kubectl.kubernetes.io/last-applied-configuration", clean["metadata"]["annotations"])
        self.assertEqual(clean["metadata"]["annotations"]["keep"], "yes")

    def test_a_non_string_secret_value_is_digested_rather_than_passed_through(self):
        """An unexpected type is not a reason to relax the rule the redaction exists for."""
        clean = self.module.sanitize({"kind": "Secret", "data": {"weird": {"nested": "material"}}})
        self.assertNotIn("material", json.dumps(clean))
        self.assertTrue(clean["data"]["weird"].startswith("sha256:"))

    def test_a_stringdata_json_patch_value_is_digested_by_path(self):
        """In a JSON Patch the field name lives in `path` and nowhere else."""
        body = self.module._sanitize_patch_body(
            {
                "type": "application/json-patch+json",
                "body": [
                    {"op": "replace", "path": "/stringData/token", "value": "plaintext"},
                    {"op": "replace", "path": "/metadata/labels/x", "value": "plaintext"},
                ],
            },
            "Secret",
        )
        self.assertTrue(body[0]["value"].startswith("sha256:"))
        self.assertEqual(body[1]["value"], "plaintext", "a non-Secret path must not be digested; the key would drift")


class TestCanonicalization(unittest.TestCase):
    """RFC 8785 properties that the fixture corpus does not happen to exercise.

    These are here because the corpus is all ASCII keys and small integers. The two rules below are
    the ones a Python implementation gets wrong by default -- `sorted()` compares code points and
    `json.dumps` renders numbers with `repr` -- and neither would show up as a fixture failure until
    the day an agent submits an object that trips them.
    """

    def setUp(self):
        self.module = load("platform")

    def test_keys_sort_by_utf16_code_unit_not_code_point(self):
        # U+FFFD is one UTF-16 unit 0xFFFD; U+1F600 is the surrogate pair D83D DE00. By code point
        # the emoji is greater; by UTF-16 it is lesser. Python's default sort gets this backwards.
        out = self.module.canonicalize({"\U0001f600": 1, "�": 2}).decode()
        self.assertLess(out.index("\U0001f600"), out.index("�"), f"keys sorted by code point, not UTF-16: {out!r}")

    def test_number_rendering_follows_ecmascript(self):
        cases = {
            0: "0",
            -0.0: "0",
            1: "1",
            100: "100",
            1.5: "1.5",
            1e21: "1e+21",
            1e-7: "1e-7",
            0.000001: "0.000001",
            1e22: "1e+22",
            -1.5: "-1.5",
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.module.canonicalize(value).decode(), want)

    def test_control_characters_escape_as_lowercase_hex(self):
        self.assertEqual(self.module.canonicalize("\x01\n").decode(), '"\\u0001\\n"')

    def test_non_ascii_is_emitted_raw(self):
        self.assertEqual(self.module.canonicalize("é").decode(), '"é"')

    def test_an_unsupported_type_is_refused_not_coerced(self):
        with self.assertRaises(self.module.EnvelopeError):
            self.module.canonicalize({"when": object()})


class TestEnumsMatchTheBroker(unittest.TestCase):
    """The other duplicated constants, joined the same way as the key.

    `VALID_OPS` and friends are copies of closed enums declared in `envelope.go`. Parsed out of the
    Go source rather than restated here, so this file is a comparison and not a third copy
    ([[LSN-036]]): a verb added to the broker and not to the agent is a verb the agent cannot spell,
    and the failure mode is a local refusal with a message about an unknown op.

    **Strengthened in P9-T8b-4c, because this class had the hole it exists to prevent.** It was
    three tests -- `validOps`, `validPatchTypes`, `validRequesterKinds` -- naming the three enums
    Python mirrored. `envelope.go` declared six. The three nobody named were exactly the three
    nobody mirrored, so the check was a comparison of the set that agreed with itself, and it was
    green for every day the other three did not exist on the Python side. It cost a live 400 on the
    first in-cluster probe (`trigger.source: "verification"`, a word that is not one of the seven).
    The enums are now **discovered** from the Go source and each one demands a Python counterpart
    by derived name, so a seventh enum is a failing build rather than a silent omission -- which is
    the difference between a join and a list of joins somebody remembered to write.
    """

    #: `validTriggerSources` -> `VALID_TRIGGER_SOURCES`. A derivation, not a table: a table is the
    #: same enumeration this class was just cured of, one indirection further away.
    #: `[^}]*` rather than a DOTALL `.*?` because two of the six literals are written on one line
    #: and four are spread over several; a lazy dot run to the next `^\t}` swallows the one-liner's
    #: successor whole and reports five enums where there are six -- which is how this regex was
    #: wrong the first time it ran, and it reported the miscount as a member mismatch.
    GO_ENUM = re.compile(r"\bvalid([A-Za-z]+)\s*=\s*map\[string\]bool\{([^}]*)\}")

    def setUp(self):
        self.module = load("platform")
        self.source = ENVELOPE_GO.read_text()

    def go_set(self, name: str) -> set[str]:
        match = re.search(rf"{name}\s*=\s*map\[string\]bool\{{(.*?)\}}", self.source, re.DOTALL)
        self.assertIsNotNone(match, f"{name} is no longer a map[string]bool literal in {ENVELOPE_GO.name}")
        return set(re.findall(r'"([^"]+)"\s*:\s*true', match.group(1)))

    @staticmethod
    def python_name(go_suffix: str) -> str:
        """`TriggerSources` -> `VALID_TRIGGER_SOURCES`."""
        return "VALID_" + re.sub(r"(?<!^)(?=[A-Z])", "_", go_suffix).upper()

    def go_enums(self) -> dict[str, set[str]]:
        """`{VALID_OPS: {...}}` -- keyed by the Python name the Go name derives to."""
        return {self.python_name(suffix): set(re.findall(r'"([^"]*)"\s*:\s*true', body)) for suffix, body in self.GO_ENUM.findall(self.source)}

    def python_enums(self) -> set[str]:
        return {n for n in dir(self.module) if n.startswith("VALID_")}

    def test_the_two_sides_declare_the_same_closed_enums(self):
        """Both directions, which is also this class's vacuity guard.

        A regex that stops matching yields `{}`, and a per-enum loop over `{}` passes by never
        running -- the precise shape of green that let three unmirrored enums sit here. Comparing
        the two NAME sets cannot pass vacuously: zero on the Go side is six unexplained constants
        on the Python side. It needs no floor constant, and it fails in the other direction too, on
        a Python `VALID_*` naming a closed set the broker does not have.
        """
        self.assertEqual(
            set(self.go_enums()),
            self.python_enums(),
            f"{ENVELOPE_GO.name} and {MODULE} do not declare the same closed enums. An enum on only one side "
            "means the agent can build an envelope carrying a value the broker refuses, and finds out over HTTP.",
        )

    def test_every_closed_enum_in_the_broker_is_mirrored_by_the_agent(self):
        """Discovered, so the set under test cannot quietly become the set that agrees."""
        for py_name, members in sorted(self.go_enums().items()):
            with self.subTest(enum=py_name):
                self.assertTrue(hasattr(self.module, py_name), f"{ENVELOPE_GO.name} declares it and {MODULE} has no {py_name}")
                self.assertEqual(members, set(getattr(self.module, py_name)), f"{py_name} and its Go original disagree")

    def test_every_mirrored_enum_is_actually_consulted_by_the_validator(self):
        """A frozenset nobody reads is decoration, and reads as coverage ([[LSN-041]]).

        Scoped to the names that appear as an operand of a **comparison**, not to the names that
        appear in the function's text. Every one of these enums is also interpolated into the
        `EnvelopeError` it raises, so a substring search is satisfied by the error message alone —
        which means the check passes for a validator that inlines the members and mentions the
        constant only when explaining the refusal. That drift is not hypothetical; it is what the
        mutant `M19-a-mirrored-enum-is-never-consulted` does, and it escaped the first draft of
        this test. Mentioning a rule is not applying it ([[LSN-023]]).
        """
        fn = next(n for n in ast.walk(ast.parse(tier_path("platform").read_text())) if isinstance(n, ast.FunctionDef) and n.name == "_check_client_side")
        compared = {
            node.id for cmp in ast.walk(fn) if isinstance(cmp, ast.Compare) for node in ast.walk(cmp) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        for py_name in sorted(self.go_enums()):
            with self.subTest(enum=py_name):
                self.assertIn(
                    py_name,
                    compared,
                    f"{py_name} exists and `_check_client_side` never compares anything against it, so nothing local enforces it",
                )

    def test_limits(self):
        for go_name, py_name in (
            ("MaxIntentLen", "MAX_INTENT_LEN"),
            ("MaxRationaleLen", "MAX_RATIONALE_LEN"),
            ("MaxOperations", "MAX_OPERATIONS"),
        ):
            with self.subTest(constant=go_name):
                match = re.search(rf"{go_name}\s*=\s*(\d+)", self.source)
                self.assertIsNotNone(match, f"{go_name} is gone from {ENVELOPE_GO.name}")
                self.assertEqual(int(match.group(1)), getattr(self.module, py_name))


class TestBuildEnvelope(unittest.TestCase):
    """The assembled envelope: shape, and the things it must refuse to assemble."""

    def setUp(self):
        self.module = load("platform")
        self.args: dict[str, Any] = dict(
            agent_identity="platform/adamparco-kage",
            intent="scale the api gateway back up",
            operations=[
                {
                    "op": "scale",
                    "target": {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "api-gateway"},
                    "scale": {"replicas": 4},
                }
            ],
            requester={"kind": "human", "id": "slack:U02ABCDEF", "platform": "slack"},
            trigger={"source": "chat", "ref": "slack:C01ABC/1721840000.000100"},
            trace={"traceId": "4bf92f3577b34da6a3ce929d0e0e4736"},
            nonce="9f2b1c7d4e6a8b0c3d5e7f9a1b2c3d4e",
        )

    def test_the_shape_the_broker_expects(self):
        env = self.module.build_envelope(**self.args)
        self.assertEqual(env["apiVersion"], "kubeagents.x-k8s.io/v1alpha1")
        self.assertEqual(env["kind"], "ActionEnvelope")
        self.assertRegex(env["idempotencyKey"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(env["issuedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn("dryRun", env, "dryRun: false is omitted, matching the fixtures")

    def test_the_identity_never_reaches_the_wire(self):
        """`agent_identity` is an input to the hash, not a claim in the body.

        03 §4.1 step 1 derives (tier, scope) from the authenticated identity. An envelope field
        asserting either could only be an attempt to override it, and the broker's ReservedKeys
        refuses the whole submission rather than ignoring the field.
        """
        env = self.module.build_envelope(**self.args)
        serialized = json.dumps(env)
        self.assertNotIn("agentIdentity", serialized)
        self.assertNotIn("platform/adamparco-kage", serialized)

    def test_dry_run_is_carried_and_hashed(self):
        wet = self.module.build_envelope(**self.args)
        dry = self.module.build_envelope(**dict(self.args, dry_run=True))
        self.assertTrue(dry["dryRun"])
        self.assertNotEqual(wet["idempotencyKey"], dry["idempotencyKey"])

    def test_a_bad_verb_is_refused_locally(self):
        args = dict(self.args, operations=[dict(self.args["operations"][0], op="destroy")])
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**args)

    def test_two_target_shapes_are_refused(self):
        """Both readings are defensible, and picking one silently turns a patch into a fan-out."""
        op = dict(self.args["operations"][0])
        op["targetSelector"] = {"version": "v1", "kind": "Pod", "namespace": "checkout", "labelSelector": "app=x"}
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**dict(self.args, operations=[op]))

    def test_no_target_shape_is_refused(self):
        op = {"op": "delete"}
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**dict(self.args, operations=[op]))

    def test_a_malformed_nonce_is_refused(self):
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**dict(self.args, nonce="not-a-nonce"))

    def test_a_malformed_trace_id_is_refused(self):
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**dict(self.args, trace={"traceId": "short"}))

    def test_no_operations_is_refused(self):
        with self.assertRaises(self.module.EnvelopeError):
            self.module.build_envelope(**dict(self.args, operations=[]))

    def test_generated_values_satisfy_the_brokers_own_regexes(self):
        self.assertRegex(self.module.new_nonce(), r"^[0-9a-f]{32}$")
        self.assertRegex(self.module.utc_now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestTheModuleIsHermetic(unittest.TestCase):
    """No network, no credentials, no cluster -- and no way for that to change unnoticed.

    06 §10 requires that exactly one mutation path exists in the agent image. This module is a pure
    request builder: the moment it can also *send*, the "one mutation path" claim moves from
    `submit_action` to two places, and the audit that reads this file for the answer gets the wrong
    one. Transport is T8b-2's, in the MCP tool, where it is visible.
    """

    FORBIDDEN = ("requests", "urllib", "http.client", "httpx", "socket", "subprocess", "kubernetes", "google.")

    def test_the_builder_imports_nothing_that_can_reach_the_network(self):
        source = tier_path("platform").read_text()
        imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", source, re.MULTILINE)
        for name in imports:
            for banned in self.FORBIDDEN:
                with self.subTest(imported=name, banned=banned):
                    self.assertFalse(
                        name == banned.rstrip(".") or name.startswith(banned),
                        f"{MODULE} imports {name}; it is a request builder and must not be able to send one",
                    )

    def test_it_names_no_mutating_kubectl_or_gcloud_verb(self):
        source = tier_path("platform").read_text()
        for verb in ("kubectl apply", "kubectl delete", "kubectl patch", "kubectl scale", "gcloud "):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, source)


class TestGoAndPythonAgreeOnAFreshEnvelope(unittest.TestCase):
    """End to end against the real broker code, for an envelope no fixture contains.

    The fixture corpus is a pinned set, so it can only ever prove agreement on inputs someone
    already thought of. This drives `broker.ComputeIdempotencyKey` directly over an envelope built
    by the Python side *now*, which is the only way a divergence on a shape nobody wrote a fixture
    for gets caught. Skipped rather than failed when the Go toolchain is absent, because "no `go` on
    this machine" is not evidence about the builder.
    """

    PROGRAM = """
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

func main() {
	var in struct {
		Identity string           `json:"identity"`
		Envelope *broker.Envelope `json:"envelope"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	key, err := broker.ComputeIdempotencyKey(in.Identity, in.Envelope)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Print(key)
}
"""

    def setUp(self):
        if subprocess.run(["which", "go"], capture_output=True).returncode != 0:
            self.skipTest("no go toolchain; the fixture-corpus join still covers the pinned shapes")
        self.module = load("platform")

    def go_key(self, identity: str, envelope: dict[str, Any]) -> str:
        import tempfile

        with tempfile.TemporaryDirectory(dir=REPO / "k8s-operator") as tmp:
            main = Path(tmp) / "main.go"
            main.write_text(self.PROGRAM)
            result = subprocess.run(
                ["go", "run", str(main)],
                cwd=REPO / "k8s-operator",
                input=json.dumps({"identity": identity, "envelope": envelope}),
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_shape_no_fixture_covers(self):
        # A JSON Patch against a Secret's /data (digested by PATH, not by field -- the one place the
        # two implementations take genuinely different code paths), a delete with preconditions, and
        # a cloud target, all in one envelope.
        operations = [
            {
                "op": "patch",
                "target": {"version": "v1", "kind": "Secret", "namespace": "checkout", "name": "downstream-api"},
                "patch": {
                    "type": "application/json-patch+json",
                    "body": [
                        {"op": "replace", "path": "/data/token", "value": "bmV3LXRva2Vu"},
                        {"op": "remove", "path": "/data/legacy"},
                        {"op": "add", "path": "/metadata/labels/rotated", "value": "true"},
                    ],
                },
            },
            {
                "op": "delete",
                "target": {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "worker-v0"},
                "delete": {"propagationPolicy": "Foreground", "gracePeriodSeconds": 30, "preconditions": {"uid": "abc-123"}},
            },
            {
                "op": "apply",
                "targetSelector": {"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "labelSelector": "tier=web"},
                "desiredState": {"metadata": {"labels": {"reviewed": "yes"}}, "data": {"mode": "fast"}},
            },
        ]
        envelope = self.module.build_envelope(
            agent_identity="developer-team/adamparco-kage/gke-scratch-kube-agents-dev/checkout",
            intent="rotate the token, retire worker-v0, and relabel the web configmaps",
            operations=operations,
            requester={"kind": "agent", "id": "developer-team/adamparco-kage", "platform": "mesh"},
            trigger={"source": "cron", "ref": "cron:rotation"},
            trace={"traceId": "4bf92f3577b34da6a3ce929d0e0e4736"},
            nonce="9f2b1c7d4e6a8b0c3d5e7f9a1b2c3d4e",
        )
        self.assertEqual(
            self.go_key("developer-team/adamparco-kage/gke-scratch-kube-agents-dev/checkout", envelope),
            envelope["idempotencyKey"],
        )

    def test_the_go_join_can_fail(self):
        """Negative control for the arm above: a tampered key is not reported as agreement."""
        envelope = json.loads((FIXTURES / "platform.scale-deployment.json").read_text())
        envelope["idempotencyKey"] = "sha256:" + "0" * 64
        self.assertNotEqual(self.go_key(identities()["platform.scale-deployment.json"], envelope), envelope["idempotencyKey"])


if __name__ == "__main__":
    unittest.main()
