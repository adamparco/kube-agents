#!/usr/bin/env python3
"""Offline tests for the inject-kind card + agent-query rendering (Phase 4 / P4-T2, S2).

session_kv_server imports FastAPI + agent_common_server (which pulls the MCP SDK), neither
of which is present in the offline build inner loop. We stub just those two modules so the
pure rendering logic — the per-kind card and agent-query framing — is provable here, in
particular that non-k8s signals are NOT coerced through the Kubernetes-event path and that
the k8s path is preserved verbatim.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# init_db() runs at module import; point it at a throwaway DB.
_db_fd, _db_path = tempfile.mkstemp()
os.close(_db_fd)
os.environ["SESSION_KV_DB_PATH"] = _db_path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

# --- Minimal stubs so session_kv_server imports without FastAPI / the MCP SDK ------------
_fastapi = types.ModuleType("fastapi")


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class _FastAPI:
    def __init__(self, *a, **k):
        pass

    def post(self, *a, **k):
        return lambda fn: fn

    def get(self, *a, **k):
        return lambda fn: fn


def _Header(default=None, **k):
    return default


class _BackgroundTasks:
    def add_task(self, *a, **k):
        pass


_fastapi.FastAPI = _FastAPI
_fastapi.HTTPException = _HTTPException
_fastapi.Header = _Header
_fastapi.BackgroundTasks = _BackgroundTasks
sys.modules["fastapi"] = _fastapi

_acs = types.ModuleType("agent_common_server")
_acs._run_env = lambda: dict(os.environ)
_acs.CONFIG_PATH = "/tmp/kage-nonexistent-config.yaml"
_acs.DOTENV_PATH = "/tmp/kage-nonexistent.env"
sys.modules["agent_common_server"] = _acs

import session_kv_server as sk


class TestInjectCards(unittest.TestCase):
    def test_k8s_event_card_unchanged(self):
        payload = {
            "kind": "k8s-event",
            "reason": "FailedMount",
            "namespace": "billing",
            "kind_of_object": "Pod",
            "name": "billing-processor-6cfdb6b98b-zwv24",
            "message": "MountVolume.SetUp failed",
            "type": "Warning",
        }
        card = sk._format_inject_card("k8s-event", payload)
        # Exact legacy shape: red critical severity, cleaned workload name, k8s footer.
        self.assertEqual(
            card,
            "🔴 *Critical:* Failed mount `billing/billing-processor` — MountVolume.SetUp failed\n"
            "🌱 _Digging down to the root cause..._",
        )

    def test_alert_card_not_coerced_to_k8s(self):
        payload = {"kind": "alert", "summary": "High error rate", "policy": "5xx-slo", "severity": "critical"}
        card = sk._format_inject_card("alert", payload)
        self.assertIn("*Alert:*", card)
        self.assertIn("High error rate", card)
        self.assertIn("5xx-slo", card)
        # Must NOT have been run through the k8s severity/footer path.
        self.assertNotIn("Digging down to the root cause", card)

    def test_github_card(self):
        payload = {"kind": "github", "action": "opened", "repo": "acme/infra", "number": 42, "title": "Bump limits"}
        card = sk._format_inject_card("github", payload)
        self.assertIn("🐙 *GitHub:*", card)
        self.assertIn("acme/infra", card)
        self.assertIn("#42", card)
        self.assertNotIn("Digging down to the root cause", card)

    def test_escalation_card(self):
        payload = {"kind": "escalation", "from": "developer-team", "summary": "quota exhausted", "namespace": "team-a"}
        card = sk._format_inject_card("escalation", payload)
        self.assertIn("Escalation from developer-team", card)
        self.assertIn("team-a", card)

    def test_unknown_kind_raises(self):
        with self.assertRaises(sk.HTTPException) as ctx:
            sk._format_inject_card("pagerduty", {"kind": "pagerduty"})
        self.assertEqual(ctx.exception.status_code, 400)


class TestBuildAgentQuery(unittest.TestCase):
    K8S = {
        "kind": "k8s-event",
        "reason": "FailedScheduling",
        "namespace": "billing",
        "kind_of_object": "Pod",
        "name": "worker-0",
        "message": "insufficient cpu",
    }

    def setUp(self):
        # Deterministic cluster/project so the golden k8s head is stable.
        self._saved = {k: os.environ.get(k) for k in ("GKE_CLUSTER_NAME", "GCP_PROJECT_ID", "GCP_PROJECT")}
        os.environ["GKE_CLUSTER_NAME"] = "platform-agent-host"
        os.environ.pop("GCP_PROJECT_ID", None)
        os.environ.pop("GCP_PROJECT", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_k8s_head_preserved_verbatim(self):
        q = sk._build_agent_query("k8s-evt-abc", self.K8S)
        self.assertTrue(
            q.startswith(
                "Analyze the following Kubernetes event warning on GKE cluster 'platform-agent-host' "
                "for the active session 'k8s-evt-abc'.\n\n"
                "**Event Details:**\n"
                "• *Resource:* billing/Pod/worker-0\n"
                "• *Event Reason:* FailedScheduling\n"
                "• *Warning Message:* insufficient cpu\n\n"
            )
        )
        # Shared tail still present, and it tells the agent to remediate rather than propose.
        self.assertIn("**Diagnose, then fix it.**", q)
        self.assertIn("submit an Action Envelope with trigger_source 'watch'", q)
        self.assertIn("• *Undo:* `/kage undo <action-id>`", q)
        self.assertIn("You never run kubectl apply/patch/delete/scale", q)
        # The superseded GitOps-proposal path must not come back.
        for gone in ("Proposed Fixes (GitOps)", "Pull Request", "apply Option A"):
            self.assertNotIn(gone, q)

    def test_alert_query_not_k8s_framed(self):
        q = sk._build_agent_query("k8s-evt-1", {"kind": "alert", "summary": "SLO burn", "policy": "p1"})
        self.assertIn("Investigate the following monitoring alert", q)
        self.assertNotIn("Analyze the following Kubernetes event warning", q)
        self.assertIn("**Diagnose, then fix it.**", q)  # shared tail
        # An alert-triggered envelope is trigger_source 'alert', never 'chat' — nobody asked.
        self.assertIn("submit an Action Envelope with trigger_source 'alert'", q)

    def test_github_query_not_k8s_framed(self):
        q = sk._build_agent_query("k8s-evt-2", {"kind": "github", "action": "opened", "repo": "acme/infra"})
        self.assertIn("Review the following GitHub activity", q)
        self.assertNotIn("Analyze the following Kubernetes event warning", q)

    def test_escalation_query_rederives_scope(self):
        q = sk._build_agent_query("k8s-evt-3", {"kind": "escalation", "from": "cluster-admin", "scope": "kube-system"})
        # The callee re-authorizes in its own scope and never inherits the caller's (invariant 5).
        self.assertIn("Re-authorize this yourself", q)
        self.assertIn("untrusted input", q)
        self.assertIn("resolve the work in YOUR scope", q)
        self.assertIn("submit an Action Envelope with trigger_source 'escalation'", q)


if __name__ == "__main__":
    unittest.main()
