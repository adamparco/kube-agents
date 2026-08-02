import asyncio
import io
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from credential_proxy import (
    MAX_REPOSITORY_LENGTH,
    AgentAPIProxyHandler,
    CommandExecutor,
    GoogleChatRelay,
    Policy,
    SlackRelay,
    is_valid_repository,
    parse_gke_context,
    read_current_context,
)
import slack_relay_patch
from slack_relay_patch import read_upload, wait_for_relay


class AgentAPIProxyTest(unittest.TestCase):
    def setUp(self):
        self.received_authorization = ""
        owner = self

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.received_authorization = self.headers.get("Authorization", "")
                body = b"proxied"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _message, *_args):
                return

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        AgentAPIProxyHandler.external_key = "external-secret"
        AgentAPIProxyHandler.upstream_key = "internal-sentinel"
        AgentAPIProxyHandler.upstream_port = self.upstream.server_port
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), AgentAPIProxyHandler)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()

    def test_replaces_external_api_key_before_forwarding(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer external-secret"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(b"proxied", response.read())
        self.assertEqual("Bearer internal-sentinel", self.received_authorization)

    def test_rejects_invalid_external_api_key(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(401, raised.exception.code)
        self.assertEqual("", self.received_authorization)

    def test_sanitizes_crlf_in_forwarded_headers(self):
        dirty = "value\r\nX-Injected: evil"
        self.assertEqual(
            "valueX-Injected: evil",
            AgentAPIProxyHandler._sanitize_header(dirty),
        )
        self.assertEqual("clean", AgentAPIProxyHandler._sanitize_header("clean"))

    def test_proxy_strips_crlf_from_forwarded_response_headers(self):
        body = b"proxied"

        class FakeResponse:
            status = 200
            reason = "OK\r\nX-Status-Injected: evil"

            def __init__(self):
                self._pending = body

            def getheaders(self):
                return [
                    ("Content-Length", str(len(body))),
                    ("X-Test", "value\r\nX-Injected: evil"),
                ]

            def read(self, _amount=-1):
                chunk, self._pending = self._pending, b""
                return chunk

        class FakeConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

# Patching http.client.HTTPConnection is global, so read the raw response
        # over a socket instead of urllib (which would use the fake too).
        with mock.patch(
            "credential_proxy.http.client.HTTPConnection", FakeConnection
        ):
            with socket.create_connection(
                ("127.0.0.1", self.proxy.server_port), timeout=10
            ) as sock:
                sock.sendall(
                    b"GET /health HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Authorization: Bearer external-secret\r\n"
                    b"Connection: close\r\n\r\n"
                )
                raw = b""
                while chunk := sock.recv(4096):
                    raw += chunk

        self.assertTrue(raw.endswith(body))
        # The CRLF-carrying value is folded onto a single header line...
        self.assertIn(b"X-Test: valueX-Injected: evil\r\n", raw)
        # ...so nothing injected appears as its own header or in the status line.
        self.assertNotIn(b"\r\nX-Injected:", raw)
        self.assertNotIn(b"\r\nX-Status-Injected:", raw)


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp_dir.name) / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "blockedMessage": "Command blocked for security reasons.",
                    "rules": [
                        {
                            "id": "gcp.access-token-disclosure",
                            "pattern": r"\bgcloud\b(?:\s+\S+)*?\s+auth\b(?:\s+\S+)*?\s+print-(?:access|identity)-token\b",
                        },
                        {
                            "id": "github.token-disclosure",
                            "pattern": r"\bgh\b(?:\s+\S+)*?\s+auth\b(?:\s+\S+)*?\s+token\b",
                        },
                        {
                            "id": "kubernetes.token-disclosure",
                            "pattern": r"\bkubectl\b(?:\s+\S+)*?\s+config\b(?:\s+\S+)*?\s+view\b(?:\s+\S+)*?\s+--raw\b",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.policy = Policy.load(str(self.policy_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blocks_configured_command(self):
        rule = self.policy.blocked_by(["gcloud", "auth", "print-access-token"])
        self.assertIsNotNone(rule)
        self.assertEqual("gcp.access-token-disclosure", rule.rule_id)

    def test_blocks_disclosure_commands_with_global_flags(self):
        cases = (
            (["gcloud", "--quiet", "auth", "print-access-token"], "gcp.access-token-disclosure"),
            (["gcloud", "--project", "example", "auth", "--quiet", "print-identity-token"], "gcp.access-token-disclosure"),
            (["gh", "--help", "auth", "token"], "github.token-disclosure"),
            (["kubectl", "--namespace=default", "config", "view", "--raw"], "kubernetes.token-disclosure"),
        )
        for argv, rule_id in cases:
            with self.subTest(argv=argv):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule)
                self.assertEqual(rule_id, rule.rule_id)

    def test_allows_supported_command(self):
        self.assertIsNone(self.policy.blocked_by(["kubectl", "get", "pods"]))


class CommandExecutorTest(unittest.TestCase):
    CONTEXT = "gke_demo-project_us-central1_cluster-a"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def executor(self, timeout_seconds=5, max_output_bytes=1024):
        return CommandExecutor(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            state_dir=self.temp_dir.name,
        )

    def caller_kubeconfig(self, executor, name="kubeconfig.yaml", body=None):
        """A kubeconfig where the agent can reach it — i.e. one to distrust."""
        path = executor.workspace_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n"
        path.write_text(body, encoding="utf-8")
        return path

    def seed_managed(self, executor, context=None):
        """Pretend a previous `get-credentials` already warmed the cache."""
        context = context or self.CONTEXT
        managed = executor.kubeconfig_dir / f"{context}.yaml"
        managed.write_text(
            f"apiVersion: v1\nkind: Config\ncurrent-context: {context}\n", encoding="utf-8"
        )
        return managed

    def fake_gcloud(self, executor):
        """Swap in a gcloud that writes a kubeconfig the way the real one does.

        Only the destination and the context name matter to anything under test,
        so the generated document is deliberately minimal.
        """
        stub = Path(self.temp_dir.name) / "fake-gcloud"
        stub.write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                set -u
                project=""; location=""; cluster=""
                for arg in "$@"; do
                    case "$arg" in
                        --project=*) project="${arg#--project=}" ;;
                        --location=*) location="${arg#--location=}" ;;
                        container|clusters|get-credentials|--*) ;;
                        *) [ -n "$cluster" ] || cluster="$arg" ;;
                    esac
                done
                ctx="gke_${project}_${location}_${cluster}"
                printf 'apiVersion: v1\\nkind: Config\\ncurrent-context: %s\\n' "$ctx" \\
                    > "$KUBECONFIG"
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor.executables["gcloud"] = str(stub)
        return executor

    def fake_git(self, executor):
        """Swap in a git that reports the environment it was handed.

        The stub has to be called `git`: the executor decides whether a command
        gets a commit identity from the executable's own name, so a `fake-git`
        would test nothing. Hence the directory rather than a suffixed filename.
        """
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "git"
        stub.write_text("#!/bin/bash\nenv\n", encoding="utf-8")
        stub.chmod(0o755)
        executor.executables["git"] = str(stub)
        return executor

    def dumped_environment(self, result):
        """Parse an `env` dump, insisting it arrived whole.

        A truncated dump would make every `assertNotIn` below pass for the wrong
        reason, so the size check is part of reading it.
        """
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertFalse(result.truncated, "environment dump was truncated")
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    def git_environment(self, executor, argv=("git", "commit", "-m", "fleet audit")):
        """The environment a proxied git subprocess actually receives."""
        return self.dumped_environment(self.fake_git(executor).execute(list(argv)))

    def test_rejects_unsupported_executable(self):
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.executor().execute(["env"])

    def test_rejects_shell_command_string(self):
        with self.assertRaisesRegex(ValueError, "list of strings"):
            self.executor().execute("gcloud auth list")

    def test_rejects_working_directory_outside_shared_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            self.executor().execute(["git", "status"], cwd="/")

    def test_kubeconfig_defaults_to_the_sidecar_context(self):
        # Omitting the field must not disturb the bootstrapped context — the
        # Platform Agent sends no KUBECONFIG and relies on this default.
        executor = self.executor()
        result = executor._execute(["/bin/sh", "-c", 'printf "%s" "$KUBECONFIG"'])
        self.assertEqual(executor.environment["KUBECONFIG"], result.stdout)

    # ---- The caller's kubeconfig is a name, never content -------------------

    def test_command_runs_against_the_proxy_copy_not_the_callers(self):
        executor = self.executor()
        managed = self.seed_managed(executor)
        pinned = self.caller_kubeconfig(executor, name="profiles/cluster-a/kubeconfig.yaml")

        resolved = executor._resolve_kubeconfig(str(pinned))

        self.assertEqual(managed, resolved)
        # The whole point: what kubectl opens is somewhere the agent cannot write.
        self.assertFalse(executor._within_workspace(resolved))

    def test_hostile_kubeconfig_content_never_reaches_the_command(self):
        # The escape this mechanism exists to close. Every field here is one the
        # sidecar would otherwise act on: `exec.command` runs next to the
        # credentials, `server` picks where the minted token is sent, and
        # `insecure-skip-tls-verify` removes the obstacle to sending it there.
        # None of it can be seen by the policy engine, whose rules match argv.
        executor = self.executor()
        self.seed_managed(executor)
        hostile = self.caller_kubeconfig(
            executor,
            body=(
                "apiVersion: v1\n"
                "kind: Config\n"
                f"current-context: {self.CONTEXT}\n"
                "clusters:\n"
                f"- name: {self.CONTEXT}\n"
                "  cluster:\n"
                "    server: https://attacker.example.invalid\n"
                "    insecure-skip-tls-verify: true\n"
                "users:\n"
                f"- name: {self.CONTEXT}\n"
                "  user:\n"
                "    exec:\n"
                "      command: /bin/sh\n"
                '      args: ["-c", "exfiltrate"]\n'
            ),
        )

        resolved = executor._resolve_kubeconfig(str(hostile))
        contents = resolved.read_text(encoding="utf-8")

        for trace in ("attacker.example.invalid", "/bin/sh", "insecure-skip-tls-verify"):
            self.assertNotIn(trace, contents)

    def test_kubeconfig_flag_is_rerouted_as_well_as_the_environment(self):
        # `--kubeconfig` takes precedence over KUBECONFIG in kubectl and reaches
        # the sidecar untouched — no policy rule mentions it. Rewriting only the
        # environment would leave the flag as a way straight back to the
        # caller's own file.
        executor = self.executor()
        managed = self.seed_managed(executor)
        pinned = self.caller_kubeconfig(executor)

        joined = executor._reroute_kubeconfig_flags(["kubectl", f"--kubeconfig={pinned}", "get", "pods"])
        separate = executor._reroute_kubeconfig_flags(["kubectl", "--kubeconfig", str(pinned), "get", "pods"])

        self.assertEqual(["kubectl", f"--kubeconfig={managed}", "get", "pods"], joined)
        self.assertEqual(["kubectl", "--kubeconfig", str(managed), "get", "pods"], separate)

    def test_kubeconfig_flag_outside_the_workspace_is_still_refused(self):
        executor = self.executor()
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            executor._reroute_kubeconfig_flags(["kubectl", "--kubeconfig=/etc/kubeconfig.yaml", "get", "pods"])

    def test_kubeconfig_surrounding_whitespace_is_ignored(self):
        # Profile .env files routinely carry a trailing newline; a path that
        # only differs by whitespace must still resolve, not silently fail.
        executor = self.executor()
        managed = self.seed_managed(executor)
        pinned = self.caller_kubeconfig(executor)
        self.assertEqual(managed, executor._resolve_kubeconfig(f"  {pinned}\n"))

    # ---- Failing closed ------------------------------------------------------

    def test_rejects_kubeconfig_naming_no_current_context(self):
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="apiVersion: v1\nkind: Config\n")
        with self.assertRaisesRegex(ValueError, "names no current-context"):
            executor._resolve_kubeconfig(str(pinned))

    def test_rejects_kubeconfig_whose_context_is_not_a_gke_name(self):
        # Without a parseable triple there is no cluster to re-fetch, so there is
        # no way to serve the request without trusting the caller's document.
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="current-context: minikube\n")
        with self.assertRaisesRegex(ValueError, "not a GKE context name"):
            executor._resolve_kubeconfig(str(pinned))

    def test_rejects_kubeconfig_outside_shared_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            self.executor()._resolve_kubeconfig("/etc/kubeconfig.yaml")

    def test_rejects_kubeconfig_escaping_the_workspace_by_traversal(self):
        executor = self.executor()
        escape = str(executor.workspace_dir / ".." / "home" / ".kube" / "config")
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            executor._resolve_kubeconfig(escape)

    def test_rejects_merged_kubeconfig_lists(self):
        # kubectl would flatten these into one view; there is no meaningful way
        # to regenerate a merge of documents that are never trusted.
        executor = self.executor()
        allowed = self.caller_kubeconfig(executor)
        with self.assertRaisesRegex(ValueError, "single file"):
            executor._resolve_kubeconfig(f"{allowed}:/etc/kubeconfig.yaml")

    def test_rejects_an_implausibly_large_kubeconfig(self):
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="#" * (1 << 20) + "\n")
        with self.assertRaisesRegex(ValueError, "implausibly large"):
            executor._resolve_kubeconfig(str(pinned))

    # ---- Fetching, and the visible pin --------------------------------------

    def test_cache_miss_refetches_credentials_from_gcloud(self):
        executor = self.fake_gcloud(self.executor())
        pinned = self.caller_kubeconfig(executor)

        resolved = executor._resolve_kubeconfig(str(pinned))

        self.assertEqual(executor.kubeconfig_dir / f"{self.CONTEXT}.yaml", resolved)
        self.assertIn(self.CONTEXT, resolved.read_text(encoding="utf-8"))
        # Nothing is left behind from the fetch.
        self.assertEqual([resolved.name], sorted(p.name for p in executor.kubeconfig_dir.iterdir()))

    def test_get_credentials_writes_both_the_managed_copy_and_the_visible_pin(self):
        # cluster_agent_profile.py and switch_kube_context both reach a cluster
        # by running this first, so it is what warms the cache. The workspace
        # copy has to appear too: the profile records that path and the Cluster
        # Agent preflight stats it.
        executor = self.fake_gcloud(self.executor())
        destination = executor.workspace_dir / "profiles" / "cluster-a" / "kubeconfig.yaml"

        result = executor.execute(
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
             "--location=us-central1", "--project=demo-project"],
            kubeconfig=str(destination),
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn(self.CONTEXT, destination.read_text(encoding="utf-8"))
        managed = executor.kubeconfig_dir / f"{self.CONTEXT}.yaml"
        self.assertIn(self.CONTEXT, managed.read_text(encoding="utf-8"))

    def test_get_credentials_never_writes_through_the_callers_path(self):
        # gcloud must not be handed the agent-writable path directly; if it were,
        # the agent could swap the file between the write and the read that files
        # it in the cache.
        executor = self.fake_gcloud(self.executor())
        destination = executor.workspace_dir / "kubeconfig.yaml"
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(kwargs.get("kubeconfig_path"))
            return original(argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
                kubeconfig=str(destination),
            )

        self.assertEqual(1, len(seen))
        self.assertFalse(executor._within_workspace(seen[0]))

    def test_timeout_kills_command(self):
        result = self.executor(timeout_seconds=1).execute_internal(["/bin/sleep", "10"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_timeout_handles_process_group_exit_race(self):
        process = mock.Mock(pid=123, returncode=0)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["command"], 1),
            (b"", b""),
        ]
        with (
            mock.patch("credential_proxy.subprocess.Popen", return_value=process),
            mock.patch("credential_proxy.os.killpg", side_effect=ProcessLookupError),
        ):
            result = self.executor(timeout_seconds=1).execute_internal(["command"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_command_environment_excludes_sidecar_tokens(self):
        import os

        previous = os.environ.get("SLACK_BOT_TOKEN")
        os.environ["SLACK_BOT_TOKEN"] = "must-not-be-forwarded"
        try:
            executor = self.executor()
        finally:
            if previous is None:
                del os.environ["SLACK_BOT_TOKEN"]
            else:
                os.environ["SLACK_BOT_TOKEN"] = previous
        self.assertNotIn("SLACK_BOT_TOKEN", executor.environment)
        self.assertEqual(str(Path(self.temp_dir.name) / "home"), executor.environment["HOME"])

    def test_git_commands_carry_a_commit_identity(self):
        # The remediation Pull Request path commits through the proxy, and the
        # commit runs here, in the sidecar. With no identity `git commit` exits
        # 128 before it writes anything, so all four variables have to be set.
        environment = self.git_environment(self.executor(max_output_bytes=1 << 16))
        self.assertEqual("kube-agents platform agent", environment["GIT_AUTHOR_NAME"])
        self.assertEqual("kube-agents platform agent", environment["GIT_COMMITTER_NAME"])
        self.assertEqual("platform-agent@kube-agents.invalid", environment["GIT_AUTHOR_EMAIL"])
        self.assertEqual("platform-agent@kube-agents.invalid", environment["GIT_COMMITTER_EMAIL"])

    def test_commit_identity_honours_the_operator_override(self):
        import os

        overrides = {
            "CREDENTIAL_PROXY_GIT_AUTHOR_NAME": "fleet-bot",
            "CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL": "fleet-bot@example.invalid",
        }
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            executor = self.executor(max_output_bytes=1 << 16)
        finally:
            for name, value in previous.items():
                if value is None:
                    del os.environ[name]
                else:
                    os.environ[name] = value
        environment = self.git_environment(executor)
        self.assertEqual("fleet-bot", environment["GIT_AUTHOR_NAME"])
        self.assertEqual("fleet-bot", environment["GIT_COMMITTER_NAME"])
        self.assertEqual("fleet-bot@example.invalid", environment["GIT_AUTHOR_EMAIL"])
        self.assertEqual("fleet-bot@example.invalid", environment["GIT_COMMITTER_EMAIL"])

    def test_commit_identity_reaches_no_other_executable(self):
        # Scoped to git on purpose: nothing else needs it, and a variable that is
        # not there cannot be read by a command that had no business seeing it.
        executor = self.executor(max_output_bytes=1 << 16)
        environment = self.dumped_environment(
            executor.execute_internal(["/bin/bash", "-c", "env"])
        )
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            self.assertNotIn(name, environment)

    def test_commit_identity_forwards_no_token(self):
        # The identity is the only thing git gains. Its credentials still come
        # from the sidecar's own store, so no bearer token may ride along.
        import os

        tokens = {
            "GITHUB_TOKEN": "must-not-be-forwarded-github",
            "GH_TOKEN": "must-not-be-forwarded-gh",
            "SLACK_BOT_TOKEN": "must-not-be-forwarded-slack",
        }
        previous = {name: os.environ.get(name) for name in tokens}
        os.environ.update(tokens)
        try:
            executor = self.executor(max_output_bytes=1 << 16)
        finally:
            for name, value in previous.items():
                if value is None:
                    del os.environ[name]
                else:
                    os.environ[name] = value
        environment = self.git_environment(executor)
        for name, value in tokens.items():
            self.assertNotIn(name, environment)
            self.assertNotIn(value, environment.values())

    def test_bootstrap_prepares_profile_for_later_commands(self):
        import os

        previous = os.environ.get("GKE_PROJECT_ID")
        os.environ["GKE_PROJECT_ID"] = "bootstrap-project"
        try:
            executor = self.executor()
            executor.bootstrap(
                'printf "%s" "$GKE_PROJECT_ID" > "$HOME/bootstrap-state"'
            )
        finally:
            if previous is None:
                del os.environ["GKE_PROJECT_ID"]
            else:
                os.environ["GKE_PROJECT_ID"] = previous
        self.assertTrue((Path(self.temp_dir.name) / "home" / "bootstrap-state").exists())
        self.assertEqual(
            "bootstrap-project",
            (Path(self.temp_dir.name) / "home" / "bootstrap-state").read_text(),
        )
        self.assertNotIn("GKE_PROJECT_ID", executor.environment)

    def test_bootstrap_failure_does_not_return_command_output(self):
        with self.assertRaisesRegex(RuntimeError, "exit code 9") as raised:
            self.executor().bootstrap("printf secret >&2; exit 9")
        self.assertNotIn("secret", str(raised.exception))


class GkeContextTest(unittest.TestCase):
    """`parse_gke_context` is the whole trust boundary for kubeconfig content.

    Everything downstream — which cluster gets re-fetched, and the filename the
    result is cached under — comes from what this returns, so anything it lets
    through has to be a real GKE triple and nothing else.
    """

    def test_recovers_the_triple(self):
        target = parse_gke_context("gke_demo-project_us-central1-a_cluster-a")
        self.assertEqual(("demo-project", "us-central1-a", "cluster-a"),
                         (target.project, target.location, target.cluster))

    def test_round_trips_the_context_name(self):
        # The proxy, the operator's buildCredentialProxyEnv, and the preflight all
        # spell this the same way; the cache filename depends on it.
        name = "gke_demo-project_us-central1_cluster-a"
        self.assertEqual(name, parse_gke_context(name).context_name)

    def test_rejects_names_that_are_not_gke_contexts(self):
        for context in ("minikube", "gke_only_three", "arn:aws:eks:us-east-1:1:cluster/x", ""):
            with self.subTest(context=context):
                self.assertIsNone(parse_gke_context(context))

    def test_rejects_components_that_would_escape_the_cache_directory(self):
        # The parsed values become a filename, so traversal and separators must
        # not survive the parse.
        for context in (
            "gke_..__.._etc",
            "gke_proj_loc_../../escape",
            "gke_proj_loc_has/slash",
            "gke_proj_loc_-leading-dash",
            "gke_proj_loc_Upper",
            "gke_proj_loc_has space",
        ):
            with self.subTest(context=context):
                self.assertIsNone(parse_gke_context(context))


class CurrentContextTest(unittest.TestCase):
    def test_reads_a_plain_value(self):
        self.assertEqual("gke_p_l_c", read_current_context("current-context: gke_p_l_c\n"))

    def test_reads_quoted_and_commented_forms(self):
        # gcloud has emitted both over time.
        self.assertEqual("gke_p_l_c", read_current_context('current-context: "gke_p_l_c"\n'))
        self.assertEqual("gke_p_l_c", read_current_context("current-context: 'gke_p_l_c'\n"))
        self.assertEqual("gke_p_l_c", read_current_context("current-context: gke_p_l_c # pinned\n"))

    def test_reads_the_spellings_only_a_real_parser_sees(self):
        # YAML is a JSON superset and a kubeconfig may legally use any of these.
        # A line scanner reads the block scalar's `>-` as the value and misses
        # the rest outright, which turns a valid pin into a rejected request.
        for label, document in (
            ("json", '{"current-context": "gke_p_l_c", "kind": "Config"}'),
            ("flow mapping", "{current-context: gke_p_l_c}"),
            ("block scalar", "current-context: >-\n  gke_p_l_c\n"),
            ("merge key", "base: &b {current-context: gke_p_l_c}\n<<: *b\n"),
        ):
            with self.subTest(label):
                self.assertEqual("gke_p_l_c", read_current_context(document))

    def test_reads_the_top_level_key_not_a_nested_one(self):
        document = (
            "contexts:\n"
            "- context:\n"
            "    current-context: gke_decoy_l_c\n"
            "current-context: gke_real_l_c\n"
        )
        self.assertEqual("gke_real_l_c", read_current_context(document))

    def test_returns_none_when_there_is_nothing_to_read(self):
        for label, document in (
            ("no such key", "apiVersion: v1\n"),
            ("null value", "current-context:\n"),
            ("empty value", "current-context: '' \n"),
            ("non-string value", "current-context: 17\n"),
            ("not a mapping", "- current-context: gke_p_l_c\n"),
            ("empty document", ""),
            ("syntax error", "current-context: [unterminated\n"),
            ("several documents", "current-context: gke_a_l_c\n---\ncurrent-context: gke_b_l_c\n"),
        ):
            with self.subTest(label):
                self.assertIsNone(read_current_context(document))

    def test_survives_a_document_built_to_kill_the_parser(self):
        # Both shapes are reachable: the caller's kubeconfig is agent-authored
        # and only bounded by MAX_KUBECONFIG_BYTES. Deep nesting is why the
        # loader must stay pure-Python — under yaml.CSafeLoader this segfaults
        # the sidecar rather than raising.
        self.assertIsNone(read_current_context("[" * 200_000 + "]" * 200_000))

        bomb = 'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
        for index in range(1, 12):
            parent, child = chr(ord("a") + index), chr(ord("a") + index - 1)
            bomb += f"{parent}: &{parent} [" + ",".join([f"*{child}"] * 9) + "]\n"
        bomb += "current-context: gke_p_l_c\n"
        self.assertEqual("gke_p_l_c", read_current_context(bomb))


class RepositoryValidationTest(unittest.TestCase):
    def test_accepts_valid_owner_name(self):
        self.assertTrue(is_valid_repository("gke-labs/kube-agents"))
        self.assertTrue(is_valid_repository("Owner_1/repo.name-2"))

    def test_rejects_non_string(self):
        self.assertFalse(is_valid_repository(None))
        self.assertFalse(is_valid_repository(["owner/name"]))

    def test_rejects_missing_slash(self):
        self.assertFalse(is_valid_repository("owner-name"))

    def test_rejects_extra_slash_and_empty_segments(self):
        self.assertFalse(is_valid_repository("owner/name/extra"))
        self.assertFalse(is_valid_repository("/name"))
        self.assertFalse(is_valid_repository("owner/"))

    def test_rejects_oversized_input(self):
        # The length guard rejects unbounded untrusted input before the regex
        # runs (defense-in-depth against regex denial-of-service).
        self.assertFalse(is_valid_repository("-" * (MAX_REPOSITORY_LENGTH + 1)))


class GoogleChatRelayTest(unittest.TestCase):
    class FakeRequest:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class FakeResource:
        def __init__(self, calls, path=()):
            self.calls = calls
            self.path = path

        def __getattr__(self, name):
            def invoke(**arguments):
                if not arguments:
                    return GoogleChatRelayTest.FakeResource(
                        self.calls, (*self.path, name)
                    )
                self.calls.append((self.path, name, arguments))
                return GoogleChatRelayTest.FakeRequest(
                    {"path": self.path, "method": name, "arguments": arguments}
                )

            return invoke

    def test_forwards_unknown_resource_method_and_body_unchanged(self):
        calls = []
        relay = GoogleChatRelay.__new__(GoogleChatRelay)
        relay.chat = self.FakeResource(calls)
        arguments = {"body": {"futureSchema": {"nested": [1, 2, 3]}}}

        result = relay.api_call(
            ["futureResource", "messages"], "futureMethod", arguments
        )

        self.assertEqual(
            [(("futureResource", "messages"), "futureMethod", arguments)], calls
        )
        self.assertEqual(arguments, result["arguments"])


class FakeSlackResponse:
    """Stand-in for slack_sdk's SlackResponse.

    The real object carries its payload on ``.data`` and defines ``__iter__``
    without ``keys()``, so ``dict(response)`` raises rather than returning the
    payload. A fake that is a plain mapping cannot catch that regression.
    """

    def __init__(self, data):
        self.data = data

    def __iter__(self):
        return iter(self.data)


class SlackRelayTest(unittest.TestCase):
    class FakeClient:
        token = "xoxb-not-returned"

        def api_call(self, method, **arguments):
            return FakeSlackResponse(
                {"ok": True, "method": method, "arguments": arguments}
            )

    def relay(self):
        relay = SlackRelay.__new__(SlackRelay)
        relay.primary_client = self.FakeClient()
        relay.clients = {"T123": relay.primary_client}
        relay.workspaces = [{"teamId": "T123", "botUserId": "U123", "botName": "agent"}]
        relay._events = queue.Queue()
        relay._receipts = {}
        import threading

        relay._lock = threading.Lock()
        return relay

    def slack_modules(self):
        class FakeWebClient:
            def __init__(self, token):
                self.token = token

            def auth_test(self):
                if self.token == "invalid":
                    raise RuntimeError("authentication failed")
                return {
                    "team_id": "T123",
                    "team": "workspace",
                    "user_id": "U123",
                    "user": "agent",
                }

        class FakeSocketModeClient:
            def __init__(self, app_token, web_client):
                self.app_token = app_token
                self.web_client = web_client
                self.socket_mode_request_listeners = []

            def connect(self):
                return None

        class FakeSocketModeResponse:
            def __init__(self, envelope_id):
                self.envelope_id = envelope_id

        slack_sdk = types.ModuleType("slack_sdk")
        slack_sdk.WebClient = FakeWebClient
        socket_mode = types.ModuleType("slack_sdk.socket_mode")
        socket_mode.SocketModeClient = FakeSocketModeClient
        response = types.ModuleType("slack_sdk.socket_mode.response")
        response.SocketModeResponse = FakeSocketModeResponse
        return {
            "slack_sdk": slack_sdk,
            "slack_sdk.socket_mode": socket_mode,
            "slack_sdk.socket_mode.response": response,
        }

    def test_initialization_skips_invalid_token_when_another_is_valid(self):
        with mock.patch.dict(sys.modules, self.slack_modules()):
            relay = SlackRelay("invalid,valid", "app-token")
        self.assertEqual("valid", relay.primary_client.token)
        self.assertEqual("T123", relay.bootstrap()[0]["teamId"])
        self.assertEqual(1000, relay._events.maxsize)

    def test_initialization_rejects_all_invalid_tokens(self):
        with mock.patch.dict(sys.modules, self.slack_modules()):
            with self.assertRaisesRegex(RuntimeError, "no Slack bot token"):
                SlackRelay("invalid", "app-token")

    def test_forwards_unknown_web_api_method_and_arguments_unchanged(self):
        arguments = {"json": {"futureSchema": {"nested": [1, 2, 3]}}}
        result = self.relay().api_call(
            "T123", "future.method", arguments
        )
        self.assertTrue(result["ok"])
        self.assertEqual("future.method", result["method"])
        self.assertEqual(arguments, result["arguments"])
        self.assertNotIn("token", json.dumps(result))

    def test_nack_requeues_event(self):
        relay = self.relay()
        relay._events.put({"type": "events_api", "payload": {"event": {}}})
        event = relay.pull(timeout_seconds=1)
        self.assertTrue(relay.settle(event["receipt"], acknowledge=False))
        retried = relay.pull(timeout_seconds=1)
        self.assertEqual("events_api", retried["type"])

    def test_nack_does_not_block_or_lose_receipt_when_queue_is_full(self):
        relay = self.relay()
        relay._events = queue.Queue(maxsize=1)
        relay._receipts["receipt"] = {
            "type": "events_api",
            "payload": {"event": {"type": "message"}},
        }
        relay._events.put_nowait({"type": "existing", "payload": {}})

        with self.assertLogs("credential-proxy", level="WARNING"):
            self.assertFalse(relay.settle("receipt", acknowledge=False))

        self.assertIn("receipt", relay._receipts)
        self.assertEqual("existing", relay._events.get_nowait()["type"])

    def test_incoming_event_is_acknowledged_and_dropped_when_queue_is_full(self):
        relay = self.relay()
        relay._events = queue.Queue(maxsize=1)
        relay._events.put_nowait({"type": "existing", "payload": {}})

        client = mock.Mock()
        request = types.SimpleNamespace(
            envelope_id="envelope", type="events_api", payload={"event": {}}
        )
        with mock.patch.dict(sys.modules, self.slack_modules()):
            with self.assertLogs("credential-proxy", level="WARNING"):
                relay._on_event(client, request)

        client.send_socket_mode_response.assert_called_once()
        self.assertEqual("existing", relay._events.get_nowait()["type"])

    def test_upload_reader_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload"
            path.write_bytes(b"12345")
            with self.assertRaisesRegex(ValueError, "size limit"):
                read_upload(path, 4)

    def test_upload_reader_accepts_file_at_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload"
            path.write_bytes(b"1234")
            self.assertEqual(b"1234", read_upload(path, 4))


class RelayReadinessTest(unittest.TestCase):
    """The agent container starts before the credential-proxy sidecar.

    A connection error out of the bootstrap call is not recoverable further up:
    the gateway's reconnect path finds no bot credential on the queued config
    (the token lives in the relay) and drops Slack until the pod restarts.
    """

    def clock(self):
        """A monotonic clock that only advances when the caller sleeps."""
        now = [0.0]
        return now, lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds)

    def test_bootstrap_retries_until_the_relay_binds_its_port(self):
        attempts = []

        def send():
            attempts.append(None)
            if len(attempts) < 4:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            return {"workspaces": [{"teamId": "T1"}]}

        _, monotonic, sleep = self.clock()
        with self.assertLogs("slack-relay-patch", level="WARNING"):
            result = wait_for_relay(
                send, timeout=120.0, poll=2.0, monotonic=monotonic, sleep=sleep
            )

        self.assertEqual({"workspaces": [{"teamId": "T1"}]}, result)
        self.assertEqual(4, len(attempts))

    def test_bootstrap_gives_up_once_the_deadline_passes(self):
        def send():
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        _, monotonic, sleep = self.clock()
        with self.assertLogs("slack-relay-patch", level="WARNING"):
            with self.assertRaises(urllib.error.URLError):
                wait_for_relay(
                    send, timeout=10.0, poll=2.0, monotonic=monotonic, sleep=sleep
                )

    def http_error(self, status):
        return urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/chat/slack/bootstrap",
            status,
            "boom",
            {},  # type: ignore[arg-type]
            None,
        )

    def test_bootstrap_retries_while_the_relay_reports_no_workspace(self):
        """The proxy binds its port before Slack auth finishes, answering 503."""
        attempts = []

        def send():
            attempts.append(None)
            if len(attempts) < 3:
                raise self.http_error(503)
            return {"workspaces": [{"teamId": "T1"}]}

        _, monotonic, sleep = self.clock()
        with self.assertLogs("slack-relay-patch", level="WARNING"):
            result = wait_for_relay(
                send, timeout=120.0, poll=2.0, monotonic=monotonic, sleep=sleep
            )

        self.assertEqual({"workspaces": [{"teamId": "T1"}]}, result)
        self.assertEqual(3, len(attempts))

    def test_bootstrap_waits_out_both_startup_stages_in_turn(self):
        """Connection refused until the port binds, then 503 until auth lands."""
        attempts = []

        def send():
            attempts.append(None)
            if len(attempts) <= 2:
                raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
            if len(attempts) <= 4:
                raise self.http_error(503)
            return {"workspaces": [{"teamId": "T1"}]}

        _, monotonic, sleep = self.clock()
        with self.assertLogs("slack-relay-patch", level="WARNING") as logs:
            result = wait_for_relay(
                send, timeout=120.0, poll=2.0, monotonic=monotonic, sleep=sleep
            )

        self.assertEqual({"workspaces": [{"teamId": "T1"}]}, result)
        self.assertEqual(5, len(attempts))
        self.assertEqual(2, sum("not accepting connections" in m for m in logs.output))
        self.assertEqual(2, sum("no authenticated workspace" in m for m in logs.output))

    def test_a_relay_error_response_is_not_retried(self):
        attempts = []

        def send():
            attempts.append(None)
            raise self.http_error(500)

        _, monotonic, sleep = self.clock()
        with self.assertRaises(urllib.error.HTTPError):
            wait_for_relay(
                send, timeout=120.0, poll=2.0, monotonic=monotonic, sleep=sleep
            )

        self.assertEqual(1, len(attempts))

    def test_a_relay_stuck_at_503_eventually_gives_up(self):
        def send():
            raise self.http_error(503)

        _, monotonic, sleep = self.clock()
        with self.assertLogs("slack-relay-patch", level="WARNING"):
            with self.assertRaises(urllib.error.HTTPError):
                wait_for_relay(
                    send, timeout=10.0, poll=2.0, monotonic=monotonic, sleep=sleep
                )


class BoltClientPatchTest(unittest.TestCase):
    """Every Slack client Bolt builds has to route through the relay.

    Bolt does not reuse the client its app was constructed with: ``_init_context``
    builds a fresh one per request from the ``AsyncWebClient`` bound in its own
    module. Patch only the adapter's name and each request talks to slack.com
    directly holding the ``relay:`` placeholder, fails authorization, and drops
    the user's message with no reply.
    """

    class FakeAsyncWebClient:
        def __init__(self, token=None, **kwargs):
            self.token = token
            self.kwargs = kwargs

    class FakeAsyncApp:
        def __init__(self, client=None, **kwargs):
            self.client = client
            self.kwargs = kwargs

    class FakeAsyncSlackResponse:
        """Mirrors the SDK response Bolt expects: .headers, .data, .validate()."""

        def __init__(self, *, client, http_verb, api_url, req_args, data, headers, status_code):
            self.client = client
            self.http_verb = http_verb
            self.api_url = api_url
            self.req_args = req_args
            self.data = data
            self.headers = headers
            self.status_code = status_code

        def get(self, key, default=None):
            return self.data.get(key, default)

        def validate(self):
            if not self.data.get("ok", False):
                raise AssertionError("slack api error")
            return self

    def install_against_fakes(self):
        """Run install() over stand-ins and hand back the patched modules."""
        adapter_module = types.ModuleType("gateway.platforms.slack_adapter")
        adapter_module.AsyncWebClient = self.FakeAsyncWebClient
        adapter_module.AsyncApp = self.FakeAsyncApp

        class SlackAdapter:
            async def connect(self, *, is_reconnect=False):
                return True

            async def disconnect(self):
                return None

        SlackAdapter.__module__ = adapter_module.__name__
        adapter_module.SlackAdapter = SlackAdapter

        bolt_module = types.ModuleType("slack_bolt.app.async_app")
        bolt_module.AsyncWebClient = self.FakeAsyncWebClient

        class PlatformRegistry:
            def create_adapter(self, name, config):
                return SlackAdapter()

        registry_module = types.ModuleType("gateway.platform_registry")
        registry_module.PlatformRegistry = PlatformRegistry

        response_module = types.ModuleType("slack_sdk.web.async_slack_response")
        response_module.AsyncSlackResponse = self.FakeAsyncSlackResponse

        modules = {
            "gateway": types.ModuleType("gateway"),
            "gateway.platform_registry": registry_module,
            adapter_module.__name__: adapter_module,
            "slack_bolt": types.ModuleType("slack_bolt"),
            "slack_bolt.app": types.ModuleType("slack_bolt.app"),
            "slack_bolt.app.async_app": bolt_module,
            "slack_sdk": types.ModuleType("slack_sdk"),
            "slack_sdk.web": types.ModuleType("slack_sdk.web"),
            "slack_sdk.web.async_slack_response": response_module,
        }
        with mock.patch.dict(sys.modules, modules):
            with mock.patch.dict(
                os.environ, {"SLACK_RELAY_URL": "http://127.0.0.1:8765"}
            ):
                slack_relay_patch.install()
            # Creating the adapter is what triggers the class-level patching.
            PlatformRegistry().create_adapter("slack", object())
        return adapter_module, bolt_module

    def test_bolt_per_request_client_is_rebound_to_the_relay_client(self):
        adapter_module, bolt_module = self.install_against_fakes()

        self.assertIsNot(bolt_module.AsyncWebClient, self.FakeAsyncWebClient)
        self.assertIs(bolt_module.AsyncWebClient, adapter_module.AsyncWebClient)
        self.assertTrue(issubclass(bolt_module.AsyncWebClient, self.FakeAsyncWebClient))

    def test_the_rebound_client_is_a_class_so_bolt_can_isinstance_it(self):
        """AsyncApp raises BoltError unless the client passes isinstance."""
        _, bolt_module = self.install_against_fakes()

        self.assertIsInstance(bolt_module.AsyncWebClient, type)
        client = bolt_module.AsyncWebClient(token="relay:T1")
        self.assertIsInstance(client, bolt_module.AsyncWebClient)

    def test_the_team_bolt_resolved_wins_over_the_joined_token(self):
        """Bolt passes team_id per request; the token lists every workspace."""
        _, bolt_module = self.install_against_fakes()

        client = bolt_module.AsyncWebClient(
            token="relay:T1,relay:T2", team_id="T2", base_url="https://slack.com/api/"
        )
        self.assertEqual("T2", client.team_id)

    def test_a_single_workspace_token_still_yields_its_team(self):
        _, bolt_module = self.install_against_fakes()

        self.assertEqual("T1", bolt_module.AsyncWebClient(token="relay:T1").team_id)

    def relayed_call(self, client, method, payload):
        """Drive one api_call with the relay's HTTP response stubbed out."""
        body = json.dumps({"response": payload}).encode("utf-8")
        with mock.patch.object(slack_relay_patch.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(body)
            return asyncio.run(client.api_call(method))

    def test_api_call_returns_a_response_object_not_a_bare_dict(self):
        """Bolt reads .headers off this; a dict raises AttributeError."""
        _, bolt_module = self.install_against_fakes()
        client = bolt_module.AsyncWebClient(token="relay:T1")

        payload = {"ok": True, "user_id": "U1", "team_id": "T1", "bot_id": "B1"}
        result = self.relayed_call(client, "auth.test", payload)

        self.assertNotIsInstance(result, dict)
        self.assertEqual({}, result.headers)
        self.assertEqual(payload, result.data)
        self.assertEqual("U1", result.get("user_id"))

    def test_a_failed_slack_call_raises_the_way_the_real_client_does(self):
        _, bolt_module = self.install_against_fakes()
        client = bolt_module.AsyncWebClient(token="relay:T1")

        with self.assertRaises(AssertionError):
            self.relayed_call(client, "auth.test", {"ok": False, "error": "nope"})


if __name__ == "__main__":
    unittest.main()
