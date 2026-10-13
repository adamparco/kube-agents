#!/usr/bin/env python3
"""Unit tests for Action Broker escalation hints in command_policy, credential_proxy, and credential_proxy_client."""

from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import command_policy  # noqa: E402
import credential_proxy  # noqa: E402
import credential_proxy_client  # noqa: E402


class TestBrokerEscalationHint(unittest.TestCase):
    def setUp(self) -> None:
        self.original_enforce = credential_proxy.CredentialProxyHandler.enforce_read_only
        credential_proxy.CredentialProxyHandler.enforce_read_only = True

    def tearDown(self) -> None:
        credential_proxy.CredentialProxyHandler.enforce_read_only = self.original_enforce

    def test_read_only_refusal_includes_broker_escalation_hint(self) -> None:
        argv = [
            "kubectl",
            "scale",
            "deployment/test-nginx",
            "-n",
            "kage-live-test",
            "--replicas=3",
        ]
        result = credential_proxy.read_only_refusal(argv)
        self.assertIsNotNone(result)
        assert result is not None
        refusal, log_hint = result
        self.assertEqual("scale.deployment/test-nginx", log_hint)
        self.assertEqual("SECURITY_POLICY_BLOCKED", refusal["code"])
        self.assertEqual("kubernetes.read-only", refusal["rule"])
        self.assertIn("escalation", refusal)
        escalation = refusal["escalation"]
        assert isinstance(escalation, dict)
        self.assertEqual("propose_to_broker", escalation["action"])
        self.assertTrue(escalation["brokerSupported"])
        self.assertEqual("scale", escalation["suggestedOp"])
        self.assertEqual(
            "python3 /opt/scripts/broker_client.py --op scale "
            "--api-version apps/v1 --kind Deployment "
            "--namespace kage-live-test --name test-nginx --replicas 3",
            escalation["cliHint"],
        )
        self.assertIn("Action Broker", str(refusal.get("error", "")))

    def test_build_broker_escalation_hint_subcommand_signature(self) -> None:
        hint = command_policy.build_broker_escalation_hint(
            "scale",
            ["deployment/test-nginx", "--replicas=3"],
            "kage-live-test",
        )
        self.assertEqual("propose_to_broker", hint["action"])
        self.assertTrue(hint["brokerSupported"])
        self.assertEqual("scale", hint["suggestedOp"])
        self.assertEqual("apps/v1", hint["apiVersion"])
        self.assertEqual("Deployment", hint["kind"])
        self.assertEqual("kage-live-test", hint["namespace"])
        self.assertEqual("test-nginx", hint["name"])
        self.assertEqual(3, hint["replicas"])
        self.assertEqual(
            "python3 /opt/scripts/broker_client.py --op scale "
            "--api-version apps/v1 --kind Deployment "
            "--namespace kage-live-test --name test-nginx --replicas 3",
            hint["cliHint"],
        )

    def test_build_broker_escalation_hint_supported_kubectl_mutations(self) -> None:
        cases = [
            (["kubectl", "apply", "-f", "manifest.yaml", "-n", "prod"], "apply"),
            (["kubectl", "create", "configmap", "app-cfg", "-n", "prod"], "create"),
            (["kubectl", "delete", "pod", "bad-pod", "-n", "prod"], "delete"),
            (["kubectl", "patch", "deployment", "api", "-n", "prod", "-p", "{}"], "patch"),
            (["kubectl", "rollout", "restart", "deployment/api", "-n", "prod"], "patch"),
            (["kubectl", "set", "image", "deployment/api", "api=nginx:1.27", "-n", "prod"], "patch"),
            (["kubectl", "label", "pod", "web-0", "env=prod", "-n", "prod"], "patch"),
            (["kubectl", "annotate", "service", "web", "a=b", "-n", "prod"], "patch"),
        ]
        for argv, want_op in cases:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed)
                hint = command_policy.build_broker_escalation_hint("kubectl", argv, decision)
                self.assertEqual("propose_to_broker", hint["action"])
                self.assertTrue(hint["brokerSupported"])
                self.assertEqual(want_op, hint["suggestedOp"])
                self.assertIn("broker_client.py", str(hint["cliHint"]))

    def test_build_broker_escalation_hint_forbidden_interactive_verbs(self) -> None:
        for verb in ("exec", "attach", "cp", "port-forward", "proxy", "debug"):
            argv = ["kubectl", verb, "pod/my-pod", "-n", "default"]
            with self.subTest(verb=verb):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed)
                hint = command_policy.build_broker_escalation_hint("kubectl", argv, decision)
                self.assertEqual("forbidden_interactive", hint["action"])
                self.assertFalse(hint["brokerSupported"])
                self.assertIn("forbidden", str(hint["cliHint"]).lower())

    def test_build_broker_escalation_hint_gcloud_mutation(self) -> None:
        argv = ["gcloud", "compute", "instances", "delete", "vm-1"]
        decision = command_policy.evaluate(argv)
        self.assertFalse(decision.allowed)
        hint = command_policy.build_broker_escalation_hint("gcloud", argv, decision)
        self.assertEqual("emit_suggestion", hint["action"])
        self.assertFalse(hint["brokerSupported"])
        self.assertIn("submit-suggestion", str(hint["cliHint"]))

    def test_credential_proxy_client_prints_hint_to_stderr(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        token_path = tmp / "token"
        token_path.write_text("valid-token\n", encoding="utf-8")

        argv = [
            "kubectl",
            "scale",
            "deployment/test-nginx",
            "-n",
            "kage-live-test",
            "--replicas=3",
        ]
        refusal_result = credential_proxy.read_only_refusal(argv)
        assert refusal_result is not None
        refusal_payload, _ = refusal_result
        body = json.dumps(refusal_payload).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="http://proxy/v1/exec",
            code=403,
            msg="Forbidden",
            hdrs=http.client.HTTPMessage(),
            fp=io.BytesIO(body),
        )

        def fake_open(_request, *_args, **_kwargs):
            raise http_error

        stderr = io.StringIO()
        environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(token_path),
            "KUBECONFIG": "",
            "HERMES_HOME": str(tmp),
        }
        with mock.patch.dict("os.environ", environ, clear=False):
            with mock.patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with mock.patch("sys.stderr", new=stderr):
                    exit_code = credential_proxy_client.execute("http://proxy", argv)

        self.assertEqual(credential_proxy_client.EXIT_SECURITY_POLICY_BLOCKED, exit_code)
        err_text = stderr.getvalue()
        self.assertIn(
            "Hint: python3 /opt/scripts/broker_client.py --op scale "
            "--api-version apps/v1 --kind Deployment "
            "--namespace kage-live-test --name test-nginx --replicas 3",
            err_text,
        )


if __name__ == "__main__":
    unittest.main()
