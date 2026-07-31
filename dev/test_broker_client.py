"""V-BRK-029 — the agent's write path reaches the broker, over mTLS, and nowhere else.

WHAT THIS CHECK IS FOR
----------------------
`agents/*/scripts/broker_client.py` is the transport half of the one write path in the system. It
sits between a builder whose correctness is already proven (V-BRK-028) and a broker whose
correctness is already proven (the V-BRK suite), and it is the only part of the path where a
mistake produces no visible failure at all until a real cluster is in front of it.

Everything it depends on lives in Go, in another language, in another image:

    the five env var names   `agentBrokerEnvVars` in broker_manifests.go
    the two route paths      `ActionsPath` / `NoncePath` in server.go
    the reply's field names  the json tags on `broker.Response` in server.go
    the ten refused headers  `bypassHeaders` in server.go

Python cannot import any of it, so `broker_client.py` restates all four. A restatement nothing
compares is exactly the defect [[LSN-041]] is about, so this file **reads each of the four back out
of the Go source and asserts the Python agrees**. That is the whole reason the check exists at this
level rather than waiting for an L2 soak: every one of these four failures is silent in unit tests,
silent in review, and total at runtime. A renamed env var does not degrade the write path, it
deletes it — and reports the deletion as "the broker refused", because from inside the pod an
unset endpoint and a hostile broker look the same.

WHAT IT DOES NOT CLAIM
----------------------
It does not prove the client can complete a TLS handshake with a real broker; that needs a listener
and a certificate authority and belongs to T8b-4's L2 soak. What it proves is that the client
**cannot be talked out of trying to** — that there is no code path in which verification is off, no
parameter that turns it off, and no retry that drops it. Those are the properties an L2 run cannot
check, because an L2 run only exercises the path where everything works.

THE FAKE TRANSPORT, AND WHY IT IS NOT A SELF-COMPARISON
--------------------------------------------------------
`BrokerClient` takes an `opener` so the request path — headers, body, method, route, response
decoding — runs end to end without a socket. The obvious objection ([[LSN-034]]) is that a fake I
wrote will accept whatever the client sends. It would, so the fake asserts nothing: it *records*,
and every assertion is then made against a value read out of the Go source or out of the production
envelope builder. The fake is a tape recorder, not a judge.

TIER PARITY
-----------
The module ships in all three tiers, byte-identical, like `action_envelope.py` and
`platform_mcp_server.py`. Every behavioural case runs against every tier's copy rather than one of
them, so "the platform tier is right and the other two rotted" is not a shape this check can pass
in.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import ssl
import sys
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TIERS = ("platform", "cluster-admin", "developer-team")
MODULE = "broker_client.py"
MCP_MODULE = "platform_mcp_server.py"

SERVER_GO = REPO / "k8s-operator" / "internal" / "broker" / "server.go"
BROKER_MANIFESTS_GO = REPO / "k8s-operator" / "internal" / "controller" / "broker_manifests.go"


def tier_dir(tier: str) -> Path:
    return REPO / "agents" / tier / "scripts"


def load(tier: str):
    """Import a tier's copy under its own module name, so three copies can coexist in one process.

    `broker_client` imports `action_envelope` by bare name, the way it does inside the image where
    both sit in the same directory, so the tier's script directory goes on `sys.path` first.
    """
    path = tier_dir(tier) / MODULE
    d = str(tier_dir(tier))
    saved = list(sys.path)
    for stale in ("action_envelope", "broker_client"):
        sys.modules.pop(stale, None)
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location(f"broker_client_{tier.replace('-', '_')}", path)
        assert spec and spec.loader, path
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved


def config(module, **overrides):
    """A complete, valid config, so a test that wants one field wrong sets exactly that field."""
    values: dict[str, Any] = {
        "endpoint": "https://demo-broker.kubeagents-system.svc.cluster.local:8443",
        "san": "demo-broker.kubeagents-system.svc.cluster.local",
        "token_file": "/var/run/secrets/kubeagents.x-k8s.io/broker/token",
        "tls_dir": "/etc/kage/tls",
        "identity": "platform/adamparco-kage",
    }
    values.update(overrides)
    return module.BrokerConfig(**values)


class Recorder:
    """A tape recorder standing in for the network. It judges nothing.

    Every request handed to it is kept verbatim, and it replies with whatever the test queued. The
    assertions live in the tests and are made against Go source or against the production builder,
    never against this class.
    """

    def __init__(self, replies: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[Any] = []
        self.bodies: list[bytes | None] = []
        self.replies = list(replies or [])

    def __call__(self, req, timeout):
        self.requests.append(req)
        self.bodies.append(req.data)
        if self.replies:
            return self.replies.pop(0)
        return {"_status": 202, "actionId": "01J8Z2K9Q7V3X5M6N8P0R2T4W6", "decision": "accepted"}

    def nonce_then(self, *replies: dict[str, Any]) -> "Recorder":
        self.replies = [{"_status": 200, "nonce": "0123456789abcdef0123456789abcdef"}, *replies]
        return self

    @property
    def envelope(self) -> dict[str, Any]:
        """The last POSTed envelope, decoded."""
        for body in reversed(self.bodies):
            if body:
                return json.loads(body)
        raise AssertionError("nothing was POSTed")

    def paths(self) -> list[str]:
        return [r.full_url.split("8443", 1)[-1] for r in self.requests]


def token_reader(module, value: str = "tkn-abc"):
    """Replace the token file read, which is the only filesystem touch on the request path."""
    module.BrokerConfig.read_token = lambda self: value  # type: ignore[method-assign]


# --- reading the Go side --------------------------------------------------------------------------


def go_const(path: Path, name: str) -> str:
    """A `Name = "value"` const from Go source."""
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', path.read_text(), re.M)
    assert m, f"{name} not found in {path.name}"
    return m.group(1)


def go_bypass_headers() -> list[str]:
    text = SERVER_GO.read_text()
    block = re.search(r"var bypassHeaders = \[\]string\{(.*?)\n\}", text, re.S)
    assert block, "bypassHeaders not found in server.go"
    return re.findall(r'"([^"]+)"', block.group(1))


def go_response_json_tags() -> set[str]:
    text = SERVER_GO.read_text()
    block = re.search(r"type Response struct \{(.*?)\n\}", text, re.S)
    assert block, "type Response not found in server.go"
    return {t.split(",")[0] for t in re.findall(r'json:"([^"]+)"', block.group(1))}


def go_agent_broker_env_names() -> list[str]:
    """The env var names `agentBrokerEnvVars` renders onto the agent container."""
    text = BROKER_MANIFESTS_GO.read_text()
    block = re.search(r"func agentBrokerEnvVars\(.*?\n\}", text, re.S)
    assert block, "agentBrokerEnvVars not found in broker_manifests.go"
    return re.findall(r'\{Name:\s*"([^"]+)"', block.group(0))


# --- 1. the four joins ------------------------------------------------------------------------------


class TestTheGoSideIsTheDefinition(unittest.TestCase):
    """Every value Python restates is read back out of Go and compared.

    Each of these four failures is total and silent: the code runs, the tests pass, and the write
    path is dead in a way that reports itself as a broker problem.
    """

    def setUp(self):
        self.mod = load("platform")

    def test_the_env_var_names_are_the_ones_the_operator_renders(self):
        rendered = go_agent_broker_env_names()
        for const in ("ENV_IDENTITY", "ENV_ENDPOINT", "ENV_SAN", "ENV_TOKEN_FILE", "ENV_TLS_DIR"):
            name = getattr(self.mod, const)
            self.assertIn(
                name,
                rendered,
                f"{MODULE} reads {name}, which agentBrokerEnvVars does not render. "
                "The agent would read an empty string and refuse every write.",
            )

    def test_every_rendered_broker_variable_is_read_by_the_client(self):
        """The other direction. A variable the operator renders and nobody reads is dead wiring."""
        read = {getattr(self.mod, c) for c in ("ENV_IDENTITY", "ENV_ENDPOINT", "ENV_SAN", "ENV_TOKEN_FILE", "ENV_TLS_DIR")}
        rendered = set(go_agent_broker_env_names())
        # The status file is consumed by the pod's own startup gate, not by this client.
        rendered.discard("KUBEAGENTS_BROKER_STATUS_FILE")
        self.assertEqual(rendered, read, "agentBrokerEnvVars and broker_client.py disagree about the wiring")

    def test_the_routes_are_the_brokers_routes(self):
        self.assertEqual(self.mod.ACTIONS_PATH, go_const(SERVER_GO, "ActionsPath"))
        self.assertEqual(self.mod.NONCE_PATH, go_const(SERVER_GO, "NoncePath"))

    def test_the_endpoint_the_agent_is_handed_carries_the_brokers_own_port(self):
        """Composed, not restated — the endpoint interpolates `broker.Port` rather than a literal.

        A literal would pass every test in both languages on the day it was written and be wrong
        the day the port moved, in the one direction nothing else can see.
        """
        endpoint = re.search(r'return fmt\.Sprintf\("https://.*?"(.*?)\)\n', BROKER_MANIFESTS_GO.read_text())
        assert endpoint, "brokerEndpoint no longer builds the URL with Sprintf"
        self.assertIn("broker.Port", endpoint.group(1), "the agent's endpoint hardcodes a port instead of using broker.Port")
        self.assertIsNotNone(re.search(r"^\s*Port = \d+", SERVER_GO.read_text(), re.M), "broker.Port is gone")

    def test_every_reply_field_read_anywhere_exists_on_the_go_response(self):
        """Both routes' replies. A typo here reads as an absent field, which reads as a silent success."""
        tags = go_response_json_tags()
        source = (tier_dir("platform") / MODULE).read_text()
        names = {a or b for a, b in re.findall(r'reply\.get\("([^"]+)"\)|reply\["([^"]+)"\]', source)}
        names -= {"_status"}  # attached locally by `_decode`; not a wire field
        self.assertTrue(names, "no reply fields are read at all; the parse is not being exercised")
        for field in names | set(self.mod.RESPONSE_FIELDS):
            self.assertIn(field, tags, f"broker_client reads reply[{field!r}] and broker.Response has no such json tag")

    def test_the_rendered_fields_are_the_ones_render_response_actually_reads(self):
        """`RESPONSE_FIELDS` is what the join is made against, so it must not be aspirational."""
        tree = ast.parse((tier_dir("platform") / MODULE).read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "render_response")
        # `ast.unparse` normalizes string quotes to single, so both forms are accepted here.
        read = {a or b for a, b in re.findall(r"""reply\.get\(['"]([^'"]+)['"]|reply\[['"]([^'"]+)['"]\]""", ast.unparse(fn))}
        read -= {"_status"}
        self.assertEqual(
            read,
            set(self.mod.RESPONSE_FIELDS),
            "RESPONSE_FIELDS and render_response disagree, so the join above checks the wrong set",
        )

    # Every closed-enum envelope field this module can name in a literal, mapped to the mirror that
    # closes it. `kind` is unambiguous inside this scan because the only `kind` this module writes is
    # the requester's -- a target's Kubernetes kind arrives inside `operations`, which is a caller
    # argument and never a literal here. The scan asserts that (grep for the four keys turns up
    # nothing else), so a future literal that collides is a failure and not a silent widening.
    ENUM_BY_FIELD = {
        "source": "VALID_TRIGGER_SOURCES",
        "kind": "VALID_REQUESTER_KINDS",
        "platform": "VALID_PLATFORMS",
        "op": "VALID_OPS",
    }

    def test_every_enum_value_this_module_writes_itself_is_inside_the_closed_enum(self):
        """A value the module supplies is a value nobody reviews, and one had been wrong from the start.

        `submit_action` passed `trigger or {"source": "agent"}` and `agent` is not one of 06 §4.1's
        seven sources. Because `envelope.go` validates the enum, that made **every** MCP submission
        a `400 invalid-envelope` -- not a degraded field, the entire write path, for every agent, on
        the default call. Nothing caught it: the enum mirror agrees with Go (V-BRK-028), the wire
        keys are all decodable (V-BRK-032), the transport is correct (the rest of this file), and a
        supplied value is none of those things. It is a *value*, and until this test the only
        assertion about a value was made against values the tests themselves supplied.

        Scoped to every dict literal in the module rather than to `build_envelope`'s keyword
        defaults, which is where the defect happened to live. T8b-4d removed that default -- the
        trigger is a parameter now -- and a scan pinned to the old shape would have gone from
        catching one thing to catching nothing while still reporting green. What is actually being
        asserted is "no closed-enum value originates in this file unless it is a member", and
        `session_requester`'s two `kind`s are inside it for the same reason the trigger was.
        """
        tree = ast.parse((tier_dir("platform") / MODULE).read_text())
        envelope_mod = self.mod.action_envelope  # the same copy the shipped client validates against
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, val in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and isinstance(val, ast.Constant) and isinstance(val.value, str)):
                    continue
                enum_name = self.ENUM_BY_FIELD.get(key.value)
                if enum_name is None:
                    continue
                checked += 1
                with self.subTest(field=key.value, value=val.value):
                    self.assertIn(
                        val.value,
                        getattr(envelope_mod, enum_name),
                        f"{MODULE} writes {key.value}={val.value!r}, which is not in {enum_name}. "
                        "The broker validates this enum, so every envelope carrying it is a 400.",
                    )
        self.assertTrue(checked, "no enum-typed literal was found at all, so this scan proves nothing")


# --- 2. mTLS has no off switch ------------------------------------------------------------------------


class TestVerificationCannotBeTurnedOff(unittest.TestCase):
    """The failure being prevented is a debugging fallback that outlived its debugging session.

    A client that retries without verification when the handshake fails is the single most common
    way mTLS becomes decorative, and it is added for good reasons by someone with a broken cert.
    """

    def setUp(self):
        self.mod = load("platform")
        self.source = (tier_dir("platform") / MODULE).read_text()

    def test_the_context_verifies_explicitly(self):
        tree = ast.parse(self.source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_ssl_context")
        assigned = {
            ast.unparse(t): ast.unparse(n.value)
            for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Attribute)
        }
        self.assertEqual(assigned.get("ctx.check_hostname"), "True")
        self.assertEqual(assigned.get("ctx.verify_mode"), "ssl.CERT_REQUIRED")

    def test_the_context_loads_a_ca_and_a_client_keypair(self):
        self.assertIn("cafile=cfg.cert_path(CA_CERT)", self.source)
        self.assertIn("load_cert_chain(", self.source)

    def test_nothing_weakens_verification_anywhere_in_the_module(self):
        for forbidden in (
            "CERT_NONE",
            "check_hostname = False",
            "_create_unverified_context",
            "VERIFY_NONE",
            "ssl._create_default_https_context",
        ):
            self.assertNotIn(forbidden, self.source, f"{MODULE} contains {forbidden}")

    def test_a_handshake_failure_is_not_retried(self):
        """An `SSLError` arm exists and it raises, rather than falling through to a second attempt."""
        tree = ast.parse(self.source)
        handlers = [
            h
            for n in ast.walk(tree)
            if isinstance(n, ast.Try)
            for h in n.handlers
            if h.type is not None and "SSLError" in ast.unparse(h.type)
        ]
        self.assertTrue(handlers, "no ssl.SSLError handler; a handshake failure would surface as an opaque crash")
        for h in handlers:
            self.assertTrue(
                any(isinstance(s, ast.Raise) for s in ast.walk(h)),
                "the ssl.SSLError handler does not raise, so it continues after a failed handshake",
            )

    def test_the_client_exposes_no_way_to_choose_a_destination(self):
        tree = ast.parse(self.source)
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BrokerClient")
        init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        args = {a.arg for a in init.args.args + init.args.kwonlyargs}
        self.assertFalse(
            args & {"url", "endpoint", "verify", "headers", "insecure"},
            "BrokerClient takes a destination or a verification flag as an argument; in this process the "
            "caller is an LLM",
        )


# --- 3. what actually goes on the wire ------------------------------------------------------------------


class TestTheRequest(unittest.TestCase):
    """Driven through the real request path with a recorder in place of the socket."""

    def run_submit(self, tier: str, *, dry_run: bool = False, replies=None):
        mod = load(tier)
        token_reader(mod)
        rec = Recorder().nonce_then(*(replies or [{"_status": 202, "actionId": "A1", "decision": "accepted"}]))
        client = mod.BrokerClient(config(mod), opener=rec)
        ops = [{"op": "apply", "target": {"version": "v1", "kind": "ConfigMap", "namespace": "team-x", "name": "cm"}, "desiredState": {"data": {"a": "b"}}}]
        fn = mod.plan_action if dry_run else mod.submit_action
        out = fn("raise the memory limit", ops, trigger_source="chat", client=client)
        return mod, rec, out

    def test_it_fetches_a_nonce_then_posts_to_the_actions_route(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                mod, rec, _ = self.run_submit(tier)
                self.assertEqual(rec.paths(), [mod.NONCE_PATH, mod.ACTIONS_PATH])
                self.assertEqual([r.get_method() for r in rec.requests], ["GET", "POST"])

    def test_every_request_carries_the_projected_token(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                _, rec, _ = self.run_submit(tier)
                for req in rec.requests:
                    self.assertEqual(req.get_header("Authorization"), "Bearer tkn-abc")

    def test_no_request_carries_a_bypass_header(self):
        """The ten headers the broker refuses, journals, and raises a security event for."""
        forbidden = {h.lower() for h in go_bypass_headers()}
        self.assertEqual(len(forbidden), 10, "bypassHeaders changed size; this check reads it from server.go")
        for tier in TIERS:
            with self.subTest(tier=tier):
                _, rec, _ = self.run_submit(tier)
                for req in rec.requests:
                    sent = {k.lower() for k in req.headers}
                    self.assertFalse(sent & forbidden, f"a bypass header was sent: {sorted(sent & forbidden)}")
                    self.assertFalse(
                        [k for k in sent if k.startswith("x-kube-agents-")],
                        "the client sets an X-Kube-Agents-* header; the broker treats all of them as an attack",
                    )

    def test_the_posted_envelope_is_the_builders_output(self):
        """Not re-derived here. The key is recomputed by the production builder and compared."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                mod, rec, _ = self.run_submit(tier)
                env = rec.envelope
                expected = mod.action_envelope.compute_idempotency_key(config(mod).identity, env["operations"], False)
                self.assertEqual(env["idempotencyKey"], expected)
                self.assertEqual(env["nonce"], "0123456789abcdef0123456789abcdef")
                self.assertNotIn("agentIdentity", env, "the identity is an input to the hash, never a claim on the wire")

    def test_a_plan_and_the_write_it_previews_have_different_keys(self):
        """`dryRun` is inside the key, so a preview cannot deduplicate the real write away."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                _, plan_rec, _ = self.run_submit(tier, dry_run=True)
                _, real_rec, _ = self.run_submit(tier, dry_run=False)
                self.assertTrue(plan_rec.envelope.get("dryRun"))
                self.assertNotIn("dryRun", real_rec.envelope)
                self.assertNotEqual(plan_rec.envelope["idempotencyKey"], real_rec.envelope["idempotencyKey"])
                self.assertEqual(plan_rec.envelope["operations"], real_rec.envelope["operations"])

    def test_a_nonce_is_fetched_for_every_submission(self):
        """Never cached. The broker's guard retires a nonce on use; a reused one reads as a replay."""
        mod = load("platform")
        token_reader(mod)
        rec = Recorder(
            [
                {"_status": 200, "nonce": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                {"_status": 202, "actionId": "A1", "decision": "accepted"},
                {"_status": 200, "nonce": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                {"_status": 202, "actionId": "A2", "decision": "accepted"},
            ]
        )
        client = mod.BrokerClient(config(mod), opener=rec)
        ops = [{"op": "delete", "target": {"version": "v1", "kind": "Pod", "namespace": "team-x", "name": "p"}}]
        mod.submit_action("first", ops, trigger_source="chat", client=client)
        mod.submit_action("second", ops, trigger_source="chat", client=client)
        self.assertEqual(rec.paths(), [mod.NONCE_PATH, mod.ACTIONS_PATH, mod.NONCE_PATH, mod.ACTIONS_PATH])
        nonces = [json.loads(b)["nonce"] for b in rec.bodies if b]
        self.assertEqual(len(set(nonces)), 2, "the same nonce was submitted twice")

    def test_the_origin_the_caller_gave_is_the_origin_on_the_wire(self):
        """The first assertion anywhere that `trigger` survives the trip. It had never been driven.

        Both defects T8b-4d can introduce are here: a `plan_action` that forgets to forward its own
        `trigger_source`, and either function substituting a constant for it. `alert` is used rather
        than `chat` precisely because `chat` is what a hardcode would most plausibly say.
        """
        for tier in TIERS:
            for dry_run in (False, True):
                with self.subTest(tier=tier, dry_run=dry_run):
                    mod = load(tier)
                    token_reader(mod)
                    rec = Recorder().nonce_then({"_status": 202, "actionId": "A1", "decision": "accepted"})
                    client = mod.BrokerClient(config(mod), opener=rec)
                    ops = [{"op": "delete", "target": {"version": "v1", "kind": "Pod", "namespace": "team-x", "name": "p"}}]
                    fn = mod.plan_action if dry_run else mod.submit_action
                    fn("drain it", ops, trigger_source="alert", trigger_ref="KubePodCrashLooping", client=client)
                    self.assertEqual(rec.envelope["trigger"], {"source": "alert", "ref": "KubePodCrashLooping"})

    def test_an_unsupplied_ref_or_detail_is_left_off_the_wire_rather_than_sent_blank(self):
        """Both are `omitempty` on `broker.Trigger`. A blank string is a value, and it is not one."""
        _, rec, _ = self.run_submit("platform")
        self.assertEqual(rec.envelope["trigger"], {"source": "chat"})

    def test_the_origin_has_no_default_anywhere_on_the_way_in(self):
        """A required parameter is the whole of T8b-4d, and a default is how it stops being one.

        Not a check on the default's *value*: `chat` was the correct value and was still wrong here.
        `trigger.source` is what splits 06 §4.1's two autonomy buckets, so whatever a default says,
        it says it for every caller that did not think about the question -- and the 01 §7 count of
        "how much of this did the agents decide" is then measuring the default. The failure is
        silent by construction, because a defaulted enum member is a legal envelope.
        """
        for tier in TIERS:
            for module, names in ((MODULE, ("submit_action", "plan_action")), (MCP_MODULE, ("submit_action", "plan_action"))):
                tree = ast.parse((tier_dir(tier) / module).read_text())
                fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}
                for name in names:
                    with self.subTest(tier=tier, module=module, function=name):
                        fn = fns[name]
                        positional = fn.args.posonlyargs + fn.args.args
                        required = {a.arg for a in positional[: len(positional) - len(fn.args.defaults)]}
                        required |= {a.arg for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is None}
                        self.assertIn(
                            "trigger_source",
                            required,
                            f"{module}:{name} gives trigger_source a default; the autonomy split then reports the "
                            "default for every caller that did not think about it",
                        )

    def test_the_envelope_names_no_tier_scope_or_risk_class(self):
        """03 §4.1 step 1 derives those from the identity; naming one could only be an override."""
        _, rec, _ = self.run_submit("platform")
        for reserved in ("tier", "scope", "riskClass", "approved", "bypass"):
            self.assertNotIn(reserved, rec.envelope)


# --- 4. refusing before sending, and reporting a refusal as one -----------------------------------------


class TestFailClosed(unittest.TestCase):
    def test_a_missing_identity_is_refused_not_defaulted(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                mod = load(tier)
                with self.assertRaises(mod.BrokerError) as cm:
                    mod.BrokerClient(config(mod, identity=""), opener=Recorder())
                self.assertIn(mod.ENV_IDENTITY, str(cm.exception))

    def test_a_missing_endpoint_or_tls_dir_is_refused(self):
        mod = load("platform")
        for field in ("endpoint", "san", "tls_dir"):
            with self.subTest(field=field), self.assertRaises(mod.BrokerError):
                mod.BrokerClient(config(mod, **{field: ""}), opener=Recorder())

    def test_an_empty_token_file_is_refused_before_anything_is_sent(self):
        mod = load("platform")
        mod.BrokerConfig.read_token = lambda self: (_ for _ in ()).throw(mod.BrokerError("empty"))
        rec = Recorder()
        client = mod.BrokerClient(config(mod), opener=rec)
        with self.assertRaises(mod.BrokerError):
            client.fetch_nonce()
        self.assertEqual(rec.requests, [], "a request was sent without a token")

    def test_a_malformed_envelope_is_refused_locally_and_nothing_is_posted(self):
        mod = load("platform")
        token_reader(mod)
        rec = Recorder().nonce_then()
        client = mod.BrokerClient(config(mod), opener=rec)
        out = mod.submit_action(
            "bad", [{"op": "conjure", "target": {"version": "v1", "kind": "Pod", "name": "p"}}], trigger_source="chat", client=client
        )
        self.assertTrue(out.startswith("REFUSED (not sent)"), out)
        self.assertEqual(rec.paths(), [mod.NONCE_PATH], "a rejected envelope was POSTed anyway")

    def test_a_refusal_is_never_rendered_as_a_completed_action(self):
        """The one wrong answer worse than an error: telling a human the cluster changed."""
        mod = load("platform")
        for reply in (
            {"_status": 403, "reason": "scope-violation", "message": "target outside scope", "decision": "rejected"},
            {"_status": 400, "reason": "idempotency-key-mismatch", "decision": "rejected"},
            {"_status": 429, "reason": "brake-engaged", "decision": "rejected", "retryAfterSeconds": 90},
        ):
            with self.subTest(reason=reply["reason"]):
                out = mod.render_response(reply)
                self.assertTrue(out.startswith("REFUSED by the broker: " + reply["reason"]), out)
                self.assertNotIn("SUBMITTED", out)
        self.assertIn("retry after 90s", mod.render_response({"_status": 429, "reason": "brake-engaged", "retryAfterSeconds": 90}))

    def test_a_dry_run_never_reports_that_something_changed(self):
        mod = load("platform")
        out = mod.render_response({"_status": 200, "decision": "dry-run", "actionId": "A1"}, dry_run=True)
        self.assertIn("nothing was changed", out)

    def test_a_non_json_reply_is_an_error_not_an_empty_success(self):
        mod = load("platform")
        with self.assertRaises(mod.BrokerError):
            mod._decode(b"<html>502 Bad Gateway</html>", 502)


# --- 5. the tools exist, in every tier, and hold no logic -----------------------------------------------


class TestTheToolsAreWiredAndThin(unittest.TestCase):
    """`platform_mcp_server.py` imports `mcp` and `pydantic`, which this environment lacks.

    So it is parsed, not imported. Parsing is enough for the two properties that matter: the tools
    are registered, and their bodies delegate rather than reimplement — because logic that lives in
    that module is logic no L0 check can execute ([[LSN-007]]).
    """

    def tools(self, tier: str) -> dict[str, ast.FunctionDef]:
        tree = ast.parse((tier_dir(tier) / MCP_MODULE).read_text())
        return {
            n.name: n
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name in ("submit_action", "plan_action")
            and any("mcp.tool" in ast.unparse(d) for d in n.decorator_list)
        }

    def test_both_tools_are_registered_in_every_tier(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(set(self.tools(tier)), {"submit_action", "plan_action"})

    def test_each_tool_delegates_to_the_client_and_does_nothing_else(self):
        for tier in TIERS:
            for name, fn in self.tools(tier).items():
                with self.subTest(tier=tier, tool=name):
                    body = [s for s in fn.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
                    self.assertEqual(len(body), 1, f"{name} has {len(body)} statements; it must only delegate")
                    self.assertIsInstance(body[0], ast.Return)
                    self.assertEqual(ast.unparse(body[0].value.func), f"broker_client.{name}")

    def test_every_parameter_a_tool_takes_reaches_the_client_under_its_own_name(self):
        """A one-statement body proves the tool does not reimplement. It does not prove it forwards.

        T8b-4d gave both tools `trigger_source`, `trigger_ref` and `trigger_detail`. A tool that
        declares a parameter and then drops it still delegates, still has one statement, and still
        passes every other assertion in this class -- and the MCP schema the model reads would go on
        advertising it. The model would keep supplying an origin, the client would keep not
        receiving one, and the field 01 §7 counts would be wrong in a way no test looked at.

        The crossing case (`trigger_ref=trigger_detail`) is the same defect one line over, so the
        keyword's name and the name of what is passed under it are compared, not just the set.
        """
        for tier in TIERS:
            client_fns = {
                n.name: n
                for n in ast.parse((tier_dir(tier) / MODULE).read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name in ("submit_action", "plan_action")
            }
            for name, fn in self.tools(tier).items():
                with self.subTest(tier=tier, tool=name):
                    declared = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
                    call = fn.body[-1].value
                    forwarded = [a.id for a in call.args if isinstance(a, ast.Name)]
                    for kw in call.keywords:
                        self.assertIsInstance(kw.value, ast.Name, f"{name} passes {kw.arg}= something that is not a parameter")
                        self.assertEqual(kw.arg, kw.value.id, f"{name} passes {kw.value.id} under the name {kw.arg}")
                        forwarded.append(kw.arg)
                    self.assertEqual(
                        sorted(declared),
                        sorted(forwarded),
                        f"{name} declares {sorted(declared)} and forwards {sorted(forwarded)}; the difference is a "
                        "parameter the MCP schema advertises and the client never sees",
                    )
                    accepts = {a.arg for a in client_fns[name].args.args + client_fns[name].args.kwonlyargs}
                    self.assertFalse(
                        set(forwarded) - accepts,
                        f"broker_client.{name} does not accept {sorted(set(forwarded) - accepts)}; the tool would "
                        "raise TypeError on its first call",
                    )

    def test_the_mcp_module_imports_the_client(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertIn("import broker_client", (tier_dir(tier) / MCP_MODULE).read_text())

    def test_no_other_tool_writes(self):
        """`submit_action` is the one mutation tool (06 §9). A second one would be a second door."""
        tree = ast.parse((tier_dir("platform") / MCP_MODULE).read_text())
        writers = [
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and any("mcp.tool" in ast.unparse(d) for d in n.decorator_list)
            and "broker_client." in ast.unparse(n)
        ]
        self.assertEqual(sorted(writers), ["plan_action", "submit_action"])


# --- 6. tier parity ---------------------------------------------------------------------------------


class TestTierParity(unittest.TestCase):
    def test_every_tier_ships_the_client(self):
        for tier in TIERS:
            self.assertTrue((tier_dir(tier) / MODULE).is_file(), f"{tier} has no {MODULE}")

    def test_the_copies_are_byte_identical(self):
        for name in (MODULE, MCP_MODULE):
            with self.subTest(module=name):
                digests = {t: hashlib.sha256((tier_dir(t) / name).read_bytes()).hexdigest() for t in TIERS}
                self.assertEqual(
                    len(set(digests.values())),
                    1,
                    f"{name} differs across tiers: {digests}. One tier having the fix is the shape "
                    "this repo has already paid for.",
                )

    def test_the_client_imports_only_the_standard_library(self):
        """Anything else and the check environment diverges from the image, or the reverse."""
        tree = ast.parse((tier_dir("platform") / MODULE).read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names.add(n.module.split(".")[0])
        self.assertEqual(names - set(sys.stdlib_module_names), {"action_envelope"})


if __name__ == "__main__":
    unittest.main()
