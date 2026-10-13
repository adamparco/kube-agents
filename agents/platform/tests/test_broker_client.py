import http.server
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import broker_client


class TestBrokerClientJCSAndIdempotency(unittest.TestCase):

  def test_jcs_canonicalize_utf16_key_order_and_numbers(self):
    # U+10000 (surrogate pair D800 DC00) sorts BEFORE U+E000 in UTF-16 code units,
    # even though U+10000 > U+E000 in UTF-8 / code point order.
    obj = {
        "\ue000": 1,
        "\U00010000": 2,
        "a": -0.0,
        "b": 100000000000000000000,
    }
    canon = broker_client.jcs_canonicalize(obj).decode("utf-8")
    self.assertEqual(canon, '{"a":0,"b":100000000000000000000,"\U00010000":2,"\ue000":1}')

  def test_idempotency_key_order_independent_and_strips_volatile_metadata(self):
    op1 = {
        "op": "apply",
        "target": {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "namespace": "prod",
            "name": "web",
        },
        "desiredState": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "web",
                "namespace": "prod",
                "managedFields": [{"manager": "kubectl"}],
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": '{"old":true}',
                    "keep": "yes",
                },
            },
        },
    }
    op1_clean = {
        "op": "apply",
        "target": {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "namespace": "prod",
            "name": "web",
        },
        "desiredState": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "web",
                "namespace": "prod",
                "annotations": {
                    "keep": "yes",
                },
            },
        },
    }
    op2 = {
        "op": "scale",
        "target": {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "namespace": "prod",
            "name": "api",
        },
        "scale": {"replicas": 3},
    }
    agent_id = "system:serviceaccount:prod:sre-agent-reader"
    k1 = broker_client.compute_idempotency_key(agent_id, [op1, op2], dry_run=False)
    k2 = broker_client.compute_idempotency_key(agent_id, [op2, op1_clean], dry_run=False)
    self.assertEqual(k1, k2)
    self.assertTrue(k1.startswith("sha256:"))
    self.assertEqual(len(k1), len("sha256:") + 64)

  def test_secret_data_and_json_patch_digested(self):
    op_secret_a = {
        "op": "create",
        "target": {"version": "v1", "kind": "Secret", "namespace": "prod", "name": " creds"},
        "desiredState": {
            "apiVersion": "v1",
            "kind": "Secret",
            "stringData": {"password": "super-secret-value"},
        },
    }
    key = broker_client.compute_idempotency_key(
        "system:serviceaccount:prod:sre-agent-reader",
        [op_secret_a],
    )
    reduced = broker_client._reduce_operation_for_key(op_secret_a)
    self.assertTrue(
        reduced["desiredState"]["stringData"]["password"].startswith("sha256:"),
    )
    self.assertNotIn("super-secret-value", json.dumps(reduced))
    self.assertTrue(key.startswith("sha256:"))

  def test_http_transport_nonce_and_actions(self):
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):

      def log_message(self, format, *args):
        pass

      def do_GET(self):
        if self.path == "/v1alpha1/nonce":
          self.send_response(200)
          self.send_header("Content-Type", "application/json")
          self.end_headers()
          self.wfile.write(json.dumps({"nonce": "a" * 32, "expiresInSeconds": 120}).encode("utf-8"))
        else:
          self.send_response(404)
          self.end_headers()

      def do_POST(self):
        if self.path == "/v1alpha1/actions":
          length = int(self.headers.get("Content-Length", "0"))
          received["auth"] = self.headers.get("Authorization")
          received["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
          self.send_response(202)
          self.send_header("Content-Type", "application/json")
          self.end_headers()
          self.wfile.write(
              json.dumps({
                  "actionId": "01JQZK0000HTTP1",
                  "namespace": "prod",
                  "decision": "accepted",
                  "phase": "Verified",
              }).encode("utf-8")
          )
        else:
          self.send_response(404)
          self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)

    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
      tf.write("test-projected-token")
      token_path = tf.name
    self.addCleanup(lambda: os.unlink(token_path))

    rc = broker_client.main([
        "--endpoint",
        f"http://127.0.0.1:{port}",
        "--token-path",
        token_path,
        "--agent-identity",
        "system:serviceaccount:prod:sre-agent-reader",
        "--op",
        "scale",
        "--api-version",
        "apps/v1",
        "--kind",
        "Deployment",
        "--namespace",
        "prod",
        "--name",
        "web",
        "--replicas",
        "3",
    ])
    self.assertEqual(rc, 0)
    self.assertEqual(received["auth"], "Bearer test-projected-token")
    env = received["body"]
    self.assertEqual(env["nonce"], "a" * 32)
    self.assertEqual(env["operations"][0]["op"], "scale")
    self.assertEqual(env["operations"][0]["target"]["group"], "apps")
    self.assertEqual(env["operations"][0]["target"]["version"], "v1")
    self.assertEqual(env["operations"][0]["scale"]["replicas"], 3)
    expected_idem = broker_client.compute_idempotency_key(
        "system:serviceaccount:prod:sre-agent-reader",
        env["operations"],
        dry_run=False,
    )
    self.assertEqual(env["idempotencyKey"], expected_idem)

  def test_nats_transport_shells_out_to_a2a_propose(self):
    with tempfile.TemporaryDirectory() as tmpdir:
      fake_a2a = pathlib.Path(tmpdir) / "a2a"
      captured_file = pathlib.Path(tmpdir) / "stdin.json"
      fake_a2a.write_text(
          f"""#!/usr/bin/env python3
import json, sys
data = sys.stdin.read()
with open({str(captured_file)!r}, "w") as f:
    f.write(data)
print(json.dumps({{"actionId": "01JQZK0000NATS1", "decision": "accepted", "phase": "Verified"}}))
""",
          encoding="utf-8",
      )
      fake_a2a.chmod(fake_a2a.stat().st_mode | stat.S_IEXEC)

      old_env = os.environ.copy()
      try:
        os.environ.pop("KUBEAGENTS_BROKER_ENDPOINT", None)
        os.environ["A2A_NATS_URL"] = "nats://127.0.0.1:4222"
        os.environ["A2A_BIN"] = str(fake_a2a)
        os.environ["A2A_REQUESTER_ID"] = "slack:U12345"
        os.environ["A2A_CAPABILITY_KEY"] = "root.task-1"
        os.environ["A2A_CAPABILITY_REVISION"] = "3"

        rc = broker_client.main([
            "propose",
            "--agent-identity",
            "system:serviceaccount:prod:sre-agent-reader",
            "--op",
            "scale",
            "--api-version",
            "apps/v1",
            "--kind",
            "Deployment",
            "--namespace",
            "prod",
            "--name",
            "web",
            "--replicas",
            "5",
        ])
        self.assertEqual(rc, 0)
        sent = json.loads(captured_file.read_text(encoding="utf-8"))
        self.assertEqual(sent["requester"]["id"], "slack:U12345")
        self.assertEqual(sent["requester"]["assertion"], "root.task-1 @3")
        self.assertEqual(sent["operations"][0]["scale"]["replicas"], 5)
      finally:
        os.environ.clear()
        os.environ.update(old_env)


if __name__ == "__main__":
  unittest.main()
