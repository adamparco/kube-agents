"""V-BRK-032 — every key the agent puts on the wire is a key the broker's decoder accepts.

WHAT THIS CHECK IS FOR
----------------------
`DecodeEnvelope` runs `json.Decoder.DisallowUnknownFields()`. That is the right choice — a broker
that silently drops a field it does not understand is a broker that executes something other than
what was asked — but it makes the envelope schema **closed in the direction the agent writes**. One
misspelled key does not degrade the write path; it deletes it, for every action, with a `400
unknown-field` naming a field the agent's author never typed.

That is not hypothetical. `session_trace()` in the shipped `broker_client.py` emitted
`parentSpanId` whenever `SPAN_ID` was set in the pod's environment. `broker.Trace` has `traceId`,
`spanId`, `sessionId` and `threadId` and never had a parent. Nothing in the repository sets
`SPAN_ID` today, so every test passed, every review passed, and the defect was scheduled to arrive
on the day someone wired tracing — at which point the symptom is *the broker refuses everything*
and the cause is four characters in a different language, in a different image.

WHAT IT ASSERTS, AND WHY IN TWO LAYERS
--------------------------------------
**Statically** (always): every wire key the shipped builders can emit is a json tag on the Go
struct that receives it. The emitted keys are read out of the Python with `ast`, the accepted keys
out of the Go struct tags — neither side is restated here, so this file is a comparison and not a
third copy of the schema ([[LSN-036]]). It also runs the other direction: a tag the Go struct
declares **without** `omitempty` is one the broker expects on every envelope, so a builder that
never emits it is building an envelope that is missing a required field.

**Executably** (when a Go toolchain is present): the envelope the shipped Python actually builds,
with every optional field populated, is handed to `broker.DecodeEnvelope` itself. The static half
can only compare the keys it manages to see; this half is the decoder's own answer, and it is the
one that reproduces the original defect exactly. Skipped, not failed, without `go` — "no toolchain
on this machine" is evidence about the machine ([[LSN-020]]).

WHAT IT DOES NOT CLAIM
----------------------
Nothing about *values*. An envelope can carry every correct key and still be refused for a bad
nonce, a stale `issuedAt`, or an op the caller is not scoped for; those are other checks' business
(V-BRK-002, V-BRK-028, the anti-replay rules of 06 §4.3). This is the narrower and more total
property: whether the bytes can be decoded into the struct at all.

TIER PARITY
-----------
Every property runs against all three tiers' copies rather than the platform one, so "the platform
tier is right and the other two rotted" is not a shape this check can pass in. Byte-parity itself
belongs to `dev/test_action_envelope.py` and `dev/test_broker_client.py`, which already assert it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TIERS = ("platform", "cluster-admin", "developer-team")
ENVELOPE_GO = REPO / "k8s-operator" / "internal" / "broker" / "envelope.go"


def tier_dir(tier: str) -> Path:
    return REPO / "agents" / tier / "scripts"


def load(tier: str, module: str):
    """Import a tier's copy under its own name, so three copies coexist in one process.

    `broker_client` imports `action_envelope` by bare name, the way it does inside the image where
    both sit in the same directory, so the tier's script directory goes on `sys.path` first.
    """
    path = tier_dir(tier) / f"{module}.py"
    saved = list(sys.path)
    for stale in ("action_envelope", "broker_client"):
        sys.modules.pop(stale, None)
    sys.path.insert(0, str(tier_dir(tier)))
    try:
        spec = importlib.util.spec_from_file_location(f"{module}_{tier.replace('-', '_')}", path)
        assert spec and spec.loader, path
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


# --- reading the two sides -------------------------------------------------------------------------


def go_struct_tags(struct: str) -> dict[str, bool]:
    """`{json tag: is_optional}` for one struct in `envelope.go`.

    Optional means the tag carries `omitempty`; a tag without it is a field the broker expects on
    every envelope, which is the direction the second property reads.
    """
    text = ENVELOPE_GO.read_text()
    block = re.search(rf"type {re.escape(struct)} struct \{{(.*?)\n\}}", text, re.DOTALL)
    assert block, f"type {struct} not found in {ENVELOPE_GO.name}"
    tags = {}
    for raw in re.findall(r'json:"([^"]+)"', block.group(1)):
        parts = raw.split(",")
        if parts[0] and parts[0] != "-":
            tags[parts[0]] = "omitempty" in parts[1:]
    assert tags, f"type {struct} parsed with no json tags; this check is now blind"
    return tags


def emitted_keys(source: str, function: str) -> set[str]:
    """Every string-literal key `function` can put into the mapping it returns.

    Covers both shapes the shipped code uses: keys in a dict literal (`{"traceId": ...}`) and keys
    assigned by subscript afterwards (`out["spanId"] = span`). A key computed at runtime would be
    invisible here, which is why the executable half exists — but there are none, and a `Constant`
    check rather than a bare `.value` read means a computed key is skipped rather than crashing
    this parse and taking the whole property down with it.
    """
    fn = next((n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == function), None)
    assert fn, f"{function} not found; this check is reading the wrong file"
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.add(node.slice.value)
    return keys


# --- 1. the static join ------------------------------------------------------------------------------


class TestEveryEmittedKeyIsDecodable(unittest.TestCase):
    """The keys the builders emit, against the tags the structs accept. Both directions."""

    #: builder function -> the Go struct its return value is decoded into.
    BUILDERS = (
        ("broker_client", "session_trace", "Trace"),
        ("action_envelope", "build_envelope", "Envelope"),
    )

    def test_no_builder_emits_a_key_the_decoder_would_refuse(self):
        """The original defect, in the general form. `DisallowUnknownFields` makes this total."""
        for tier in TIERS:
            for module, function, struct in self.BUILDERS:
                with self.subTest(tier=tier, function=function):
                    emitted = emitted_keys((tier_dir(tier) / f"{module}.py").read_text(), function)
                    self.assertTrue(emitted, f"{function} parsed with no emitted keys; the property is vacuous")
                    accepted = set(go_struct_tags(struct))
                    unknown = emitted - accepted
                    self.assertEqual(
                        set(),
                        unknown,
                        f"{module}.py:{function} can emit {sorted(unknown)}, which broker.{struct} has no json tag for. "
                        "DecodeEnvelope runs DisallowUnknownFields, so this is a 400 on every action, not a dropped field.",
                    )

    def test_every_field_the_broker_requires_is_one_a_builder_emits(self):
        """The other direction. A tag without `omitempty` is expected on every envelope."""
        for tier in TIERS:
            for module, function, struct in self.BUILDERS:
                with self.subTest(tier=tier, function=function):
                    emitted = emitted_keys((tier_dir(tier) / f"{module}.py").read_text(), function)
                    required = {tag for tag, optional in go_struct_tags(struct).items() if not optional}
                    self.assertTrue(required, f"broker.{struct} has no required tags; this direction is vacuous")
                    self.assertEqual(
                        set(),
                        required - emitted,
                        f"broker.{struct} requires {sorted(required - emitted)} and {module}.py:{function} never emits it",
                    )

    def test_the_trace_key_the_defect_was_is_not_back(self):
        """A named regression, kept alongside the general rule rather than instead of it.

        The general property above is the real control. This one costs a line and names the string,
        so a reviewer reading a diff that reintroduces it sees a test whose name is the answer.

        Scoped to **string literals in the AST**, not to the file's bytes. The first draft grepped
        the text and went red on the comment at `session_trace` explaining why the key is gone —
        [[LSN-023]] in miniature, a check satisfied (here, refuted) by prose about the code rather
        than by the code. Prose may discuss it; nothing may build it.
        """
        for tier in TIERS:
            with self.subTest(tier=tier):
                tree = ast.parse((tier_dir(tier) / "broker_client.py").read_text())
                literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                docstrings = {ast.get_docstring(n) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}
                self.assertNotIn(
                    "parentSpanId",
                    literals - docstrings,
                    "parentSpanId is not a field of broker.Trace; it was a 400 on every action in a traced session",
                )


# --- 2. the decoder's own answer ---------------------------------------------------------------------


class TestTheBrokerActuallyDecodesWhatTheAgentBuilds(unittest.TestCase):
    """The half that cannot be fooled by a key this file's parser failed to see.

    Everything above reads source. This hands the bytes to `broker.DecodeEnvelope` and reports what
    the broker itself says, which is the only opinion that decides whether an action happens.
    """

    PROGRAM = """
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

func main() {
	body, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if _, err := broker.DecodeEnvelope(body); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Print("decoded")
}
"""

    #: Compiled once per class, then executed per envelope. `go run` would conflate the two, and
    #: it already did: an unused import made every decode exit 1, which reads identically to the
    #: broker refusing the envelope. A harness that cannot compile must say so, not vote
    #: ([[LSN-048]], [[LSN-049]] — a run the sweep could not evaluate is not a verdict).
    _binary: Path | None = None
    _tmp: Any = None

    @classmethod
    def setUpClass(cls):
        if subprocess.run(["which", "go"], capture_output=True).returncode != 0:
            raise unittest.SkipTest("no go toolchain; the static join above still compares every key")
        cls._tmp = tempfile.TemporaryDirectory(dir=REPO / "k8s-operator")
        main = Path(cls._tmp.name) / "main.go"
        main.write_text(cls.PROGRAM)
        cls._binary = Path(cls._tmp.name) / "decode"
        build = subprocess.run(
            ["go", "build", "-o", str(cls._binary), str(main)],
            cwd=REPO / "k8s-operator",
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, f"the decode harness does not compile, so no result below is about the agent:\n{build.stderr}"

    @classmethod
    def tearDownClass(cls):
        if cls._tmp is not None:
            cls._tmp.cleanup()
            cls._tmp = None

    def decode(self, envelope: dict[str, Any]) -> tuple[int, str]:
        assert self._binary
        result = subprocess.run([str(self._binary)], input=json.dumps(envelope), capture_output=True, text=True)
        return result.returncode, (result.stderr or result.stdout).strip()

    def build(self, tier: str, **trace_env: str) -> dict[str, Any]:
        """A maximal envelope from the shipped builders — every optional field populated.

        Maximal on purpose: an envelope that sets only the required fields exercises none of the
        keys a defect can hide in, and `parentSpanId` lived in exactly such a branch for weeks.
        """
        client = load(tier, "broker_client")
        envelope_mod = load(tier, "action_envelope")
        import os

        saved = {k: os.environ.get(k) for k in ("TRACE_ID", "SPAN_ID")}
        os.environ["TRACE_ID"] = "4bf92f3577b34da6a3ce929d0e0e4736"
        os.environ.update(trace_env)
        try:
            trace = client.session_trace()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.update({k: v})
        return envelope_mod.build_envelope(
            agent_identity="platform/adamparco-kage",
            intent="raise the worker memory limit",
            operations=[
                {
                    "op": "patch",
                    "target": {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "worker"},
                    "patch": {"type": "application/merge-patch+json", "body": {"spec": {"replicas": 2}}},
                },
                {
                    "op": "delete",
                    "target": {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "worker-v0"},
                    "delete": {"propagationPolicy": "Foreground", "gracePeriodSeconds": 30, "preconditions": {"uid": "abc-123"}},
                },
            ],
            requester={"kind": "human", "id": "slack:U02ABCDEF", "platform": "slack", "displayName": "A. Parco"},
            trigger={"source": "watch", "ref": "pod/worker-7d9c", "detail": "OOMKilled x3/10m"},
            trace=trace,
            nonce="8f14e45fceea167a5a36dedd4bea2543",
            rationale="the container is being OOM-killed at the current limit",
            dry_run=True,
            require_approval=True,
            max_objects=5,
            deadline_seconds=120,
        )

    def test_a_maximal_envelope_from_every_tier_decodes(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                rc, out = self.decode(self.build(tier))
                self.assertEqual(0, rc, f"{tier}'s builders produced an envelope the broker refuses: {out}")

    def test_an_envelope_from_a_traced_session_decodes(self):
        """The exact original defect. With `SPAN_ID` set, this failed `400 unknown-field`."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                envelope = self.build(tier, SPAN_ID="00f067aa0ba902b7")
                self.assertEqual(
                    "00f067aa0ba902b7",
                    envelope["trace"].get("spanId"),
                    "SPAN_ID reached the wire under some other name, so this test is not measuring the defect",
                )
                rc, out = self.decode(envelope)
                self.assertEqual(0, rc, f"{tier} in a traced session builds an envelope the broker refuses: {out}")

    def test_the_decoder_is_the_strict_one_this_check_assumes(self):
        """A control on the control. If `DisallowUnknownFields` ever goes, the two tests above pass
        for a reason that has nothing to do with the agent being correct, and this file becomes a
        very confident measurement of nothing."""
        envelope = self.build("platform")
        envelope["trace"]["parentSpanId"] = "00f067aa0ba902b7"
        rc, out = self.decode(envelope)
        self.assertNotEqual(0, rc, "DecodeEnvelope accepted an unknown trace field; the strictness this check relies on is gone")
        self.assertIn("parentSpanId", out, f"refused, but not for the unknown field: {out}")


if __name__ == "__main__":
    unittest.main()
