#!/usr/bin/env python3
"""V-CMP-006 — every MCP server whose script reads a credential from the environment declares
that variable in its OWN `env:` block.

The failure this catches has no symptom a normal check can see. Hermes hands an MCP server only
what the server's config declares; the gateway container holding `API_SERVER_KEY` in its pod env
does not put that value into the server's process. `agent_common` shipped with `command` and
`args` and no `env:`, so `agent_common_server.resolve_agent_credentials()` read an empty key and
correctly refused every inter-agent call — on a pod that was Ready, with green unit tests, against
a Secret that was present and correct. Nothing in the build looked at the two facts together.

09 §5.1 makes the rendered ConfigMap authoritative, not the image-baked `agents/*/config.yaml`
(§11.3): the operator's `renderConfigYAML()` output is mounted over `/opt/data/config.yaml`, so a
correct baked file cannot save a renderer that forgot the block. The rendered artifact reachable
at L0 is the golden — `k8s-operator/internal/testing/testdata/*/expected/agent.yaml`, byte-locked
by `internal/testing/golden_test.go` against the live renderer. The baked configs are checked too,
as a SECOND assertion and never as a substitute: `developer-team` has no golden, and without this
a tier could regress unseen.

Three properties:
  1. declared     — every credential-shaped variable a server's script reads is in that server's
                    `env:`, or in ALLOWED_UNDECLARED with a stated reason.
  2. no phantoms  — every `mcp-<name>` in `platform_toolsets` names a declared `mcp_servers` entry.
  3. resolvable   — every declared server's script exists in the tree.

Reads are followed one hop into same-directory imports: `agent_common_server` imports
`SessionManager`, and a credential read there is just as invisible.

Negative control (09 §5.1 requires one): `test_negative_control_stripped_api_server_key` strips
`API_SERVER_KEY` from a `platform_control` fixture and asserts this check goes red.
"""

import ast
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The rendered ConfigMap goldens — the runtime-authoritative artifact (09 §11.3), reachable at L0
# because golden_test.go locks them byte-for-byte to renderConfigYAML().
GOLDENS = {
    "platform": "k8s-operator/internal/testing/testdata/platform/expected/agent.yaml",
    "cluster-admin": "k8s-operator/internal/testing/testdata/cluster-admin/expected/agent.yaml",
}

# The image-baked fallbacks. Checked in addition, never instead.
BAKED = {
    tier: f"agents/{tier}/config.yaml"
    for tier in ("platform", "cluster-admin", "developer-team")
}

# Where a script path in `args` resolves to. MCP servers are invoked as /opt/data/scripts/<f>.py;
# that directory is assembled from the tier's own scripts plus the shared defaults.
SCRIPT_ROOTS = ("agents/{tier}/scripts", "deploy/shared/defaults/scripts")

# A variable is credential-shaped if its name says so. Deliberately a name test and not a value
# test: this runs on config, where every value is a `${VAR}` placeholder.
#
# Suffix only, not "contains". `MCP_BRIDGE_TOKEN_TIMEOUT` is a number of seconds, and a
# contains-match flags it as a secret, which teaches the reader that this check cries wolf. The
# trailing word is what names the thing: `..._KEY` is a key, `..._TOKEN_TIMEOUT` is a timeout.
CREDENTIAL_RE = re.compile(
    r"(^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|AUTH)$"
)

# Credential-shaped reads that must NOT be declared, each with the reason it is an exception.
# Keyed by (server, variable). An entry here is a claim that declaring the variable would make the
# software worse — not that nobody got round to it. Same shape as the P4 dataplane allow-list: the
# reasoning is visible in the diff rather than absent from it.
ALLOWED_UNDECLARED = {
    ("agent_common", "SLACK_BOT_TOKEN"): (
        "agent_common_server.load_slack_token() keys on `\"SLACK_BOT_TOKEN\" not in os.environ` and "
        "reads the Secret directly when absent. Declaring it substitutes an empty string on the "
        "pods that have no Slack wiring, which turns 'absent' into 'present and blank' and skips "
        "the fallback. The pod injects the real value when Slack is enabled."
    ),
    ("platform_control", "SLACK_BOT_TOKEN"): (
        "platform_mcp_server.get_active_platform() reads it as a presence heuristic to pick the "
        "chat backend after config inspection fails; the value is never used as a credential and "
        "never leaves the process."
    ),
}


# ---------------------------------------------------------------------------
# YAML, without PyYAML. The L0 workflow installs no dependencies on purpose -- "a check that needs
# a package is not L0" -- so this parses the subset these two file shapes actually use: nested
# mappings, sequences of scalars, and quoted or bare scalar values. It accepts sequence items both
# at the parent key's indent (Go's yaml.Marshal) and indented under it (prettier), because the
# goldens are written by the first and reformatted by the second. Anything outside the subset
# raises rather than returning a silently truncated document.
# ---------------------------------------------------------------------------


def _significant_lines(text: str) -> list:
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        body = raw.lstrip(" ")
        if body.startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(body)]:
            raise ValueError("tab indentation is not YAML")
        out.append((len(raw) - len(body), body.rstrip()))
    return out


def _scalar(value: str):
    value = value.strip()
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    head = value.split(" #", 1)[0].rstrip()
    return head


def _parse_block(lines: list, i: int, indent: int):
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        seq = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("-"):
            seq.append(_scalar(lines[i][1][1:]))
            i += 1
        return seq, i

    mapping = {}
    while i < len(lines):
        ind, content = lines[i]
        if ind < indent or content.startswith("-"):
            break
        if ind > indent:
            raise ValueError(f"unexpected indent at {content!r}")
        if ":" not in content:
            raise ValueError(f"not a mapping entry: {content!r}")
        key, _, rest = content.partition(":")
        key = _scalar(key)
        rest = rest.strip()
        i += 1
        if rest:
            mapping[key] = _scalar(rest)
            continue
        nested = i < len(lines) and (
            lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("-"))
        )
        if nested:
            mapping[key], i = _parse_block(lines, i, lines[i][0])
        else:
            mapping[key] = None
    return mapping, i


def parse_yaml_subset(text: str) -> dict:
    lines = _significant_lines(text)
    if not lines:
        return {}
    parsed, consumed = _parse_block(lines, 0, lines[0][0])
    if consumed != len(lines):
        raise ValueError(f"stopped parsing at {lines[consumed][1]!r}")
    return parsed


def extract_embedded_config(golden_text: str) -> str:
    """Pull the `config.yaml: |` block scalar out of a rendered ConfigMap and dedent it."""
    lines = golden_text.splitlines()
    for n, line in enumerate(lines):
        match = re.match(r"^(\s*)config\.yaml: \|\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        body = []
        for rest in lines[n + 1:]:
            if rest.strip() and (len(rest) - len(rest.lstrip(" "))) <= indent:
                break
            body.append(rest[indent + 2:] if rest.strip() else "")
        return "\n".join(body)
    raise AssertionError("no `config.yaml: |` block in this golden")


def env_vars_read(source: str) -> set:
    """Every environment variable name a module reads, from the AST rather than a regex.

    Covers os.environ["X"], os.environ.get("X"), os.getenv("X"), and "X" in os.environ.
    """
    names = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # os.environ["X"] / os.environ.get("X", ...)
        if isinstance(node, ast.Subscript):
            if _is_environ(node.value) and isinstance(node.slice, ast.Constant):
                if isinstance(node.slice.value, str):
                    names.add(node.slice.value)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "get" and _is_environ(fn.value):
                if node.args and isinstance(node.args[0], ast.Constant):
                    if isinstance(node.args[0].value, str):
                        names.add(node.args[0].value)
            elif isinstance(fn, ast.Attribute) and fn.attr == "getenv":
                if node.args and isinstance(node.args[0], ast.Constant):
                    if isinstance(node.args[0].value, str):
                        names.add(node.args[0].value)
        # "X" in os.environ
        elif isinstance(node, ast.Compare):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                for comparator in node.comparators:
                    if _is_environ(comparator) and isinstance(node.left, ast.Constant):
                        if isinstance(node.left.value, str):
                            names.add(node.left.value)
    return names


def _is_environ(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def local_imports(source: str) -> set:
    """Module names imported that could resolve to a sibling script in the same directory."""
    mods = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
            elif node.module:
                mods.add(node.module.split(".")[0])
    return mods


def resolve_script(tier: str, container_path: str):
    """Map an /opt/data/scripts/<f>.py arg back to a file in the tree, or None."""
    base = os.path.basename(container_path)
    for root in SCRIPT_ROOTS:
        candidate = os.path.join(REPO, root.format(tier=tier), base)
        if os.path.isfile(candidate):
            return candidate
    return None


def server_script_arg(server: dict):
    """The first .py argument of an MCP server's command line, if any."""
    for arg in server.get("args") or []:
        if isinstance(arg, str) and arg.endswith(".py"):
            return arg
    return None


def credential_reads(script_path: str) -> set:
    """Credential-shaped env reads in a script and in its same-directory imports (one hop)."""
    with open(script_path) as fh:
        source = fh.read()
    names = env_vars_read(source)
    directory = os.path.dirname(script_path)
    for mod in local_imports(source):
        sibling = os.path.join(directory, mod + ".py")
        if os.path.isfile(sibling) and sibling != script_path:
            with open(sibling) as fh:
                names |= env_vars_read(fh.read())
    return {n for n in names if CREDENTIAL_RE.search(n)}


def check_config(tier: str, config: dict, origin: str) -> list:
    """Return a list of violation strings for one parsed Hermes config."""
    violations = []
    servers = config.get("mcp_servers") or {}

    for name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            violations.append(f"{origin}: mcp_servers.{name} is not a mapping")
            continue
        arg = server_script_arg(server)
        if arg is None:
            continue  # not a script-backed server; nothing to read its env from
        script = resolve_script(tier, arg)
        if script is None:
            violations.append(
                f"{origin}: mcp_servers.{name} runs {arg}, which resolves to no file in the tree"
            )
            continue
        declared = set((server.get("env") or {}).keys())
        for var in sorted(credential_reads(script)):
            if var in declared:
                continue
            if (name, var) in ALLOWED_UNDECLARED:
                continue
            violations.append(
                f"{origin}: mcp_servers.{name} runs {os.path.relpath(script, REPO)}, which reads "
                f"the credential {var}, but its env: block declares "
                f"{sorted(declared) or 'nothing'}. Hermes passes an MCP server only what it "
                f"declares, so this server reads an empty {var} and fails closed at the first call."
            )

    for toolset, entries in sorted((config.get("platform_toolsets") or {}).items()):
        for entry in entries or []:
            if not (isinstance(entry, str) and entry.startswith("mcp-")):
                continue
            target = entry[len("mcp-"):]
            if target not in servers:
                violations.append(
                    f"{origin}: platform_toolsets.{toolset} lists {entry}, but mcp_servers "
                    f"declares no {target}"
                )
    return violations


def load_rendered_config(golden_path: str) -> dict:
    """Pull the agent's config.yaml out of the rendered ConfigMap in a golden file."""
    with open(golden_path) as fh:
        return parse_yaml_subset(extract_embedded_config(fh.read()))


def load_baked_config(path: str) -> dict:
    with open(path) as fh:
        return parse_yaml_subset(fh.read())


class TestMCPEnvDeclared(unittest.TestCase):
    """V-CMP-006. Reports fail, never partial (09 §5.1)."""

    def test_rendered_configmap_declares_every_credential(self):
        """The authoritative artifact: the ConfigMap the operator renders and mounts."""
        violations = []
        for tier, rel in GOLDENS.items():
            path = os.path.join(REPO, rel)
            self.assertTrue(os.path.isfile(path), f"missing golden {rel}")
            violations += check_config(tier, load_rendered_config(path), f"rendered/{tier}")
        self.assertEqual([], violations, "\n" + "\n".join(violations))

    def test_baked_config_declares_every_credential(self):
        """The image-baked fallback. Second assertion, not a substitute for the one above —
        but it is the only coverage developer-team has, which has no golden."""
        violations = []
        for tier, rel in BAKED.items():
            path = os.path.join(REPO, rel)
            self.assertTrue(os.path.isfile(path), f"missing config {rel}")
            violations += check_config(tier, load_baked_config(path), f"baked/{tier}")
        self.assertEqual([], violations, "\n" + "\n".join(violations))

    def test_agent_common_declares_api_server_key_everywhere(self):
        """The specific regression this unit repaired, named so a future refactor that drops the
        block fails with the reason rather than with a diff."""
        for tier, rel in GOLDENS.items():
            cfg = load_rendered_config(os.path.join(REPO, rel))
            env = (cfg["mcp_servers"]["agent_common"].get("env") or {})
            self.assertEqual(
                "${API_SERVER_KEY}", env.get("API_SERVER_KEY"),
                f"rendered/{tier}: agent_common must declare API_SERVER_KEY or every inter-agent "
                f"call fails closed",
            )
        for tier, rel in BAKED.items():
            cfg = load_baked_config(os.path.join(REPO, rel))
            env = cfg["mcp_servers"]["agent_common"].get("env") or {}
            self.assertEqual("${API_SERVER_KEY}", env.get("API_SERVER_KEY"), f"baked/{tier}")

    def test_negative_control_stripped_api_server_key(self):
        """09 §5.1: strip API_SERVER_KEY from a fixture's platform_control block and confirm the
        check goes red. A check that cannot fail is V-MET-014, not a check."""
        cfg = load_rendered_config(os.path.join(REPO, GOLDENS["platform"]))
        self.assertEqual([], check_config("platform", cfg, "control"), "fixture must start green")
        del cfg["mcp_servers"]["platform_control"]["env"]["API_SERVER_KEY"]
        violations = check_config("platform", cfg, "control")
        self.assertTrue(
            any("platform_control" in v and "API_SERVER_KEY" in v for v in violations),
            f"stripping API_SERVER_KEY must be caught; got {violations}",
        )

    def test_negative_control_phantom_toolset_entry(self):
        """And a toolset naming a server nobody declares."""
        cfg = load_rendered_config(os.path.join(REPO, GOLDENS["platform"]))
        cfg["platform_toolsets"]["cli"].append("mcp-does_not_exist")
        violations = check_config("platform", cfg, "control")
        self.assertTrue(
            any("does_not_exist" in v for v in violations),
            f"a phantom toolset entry must be caught; got {violations}",
        )

    def test_allowlist_entries_are_live(self):
        """An allow-list entry for a read that no longer happens is a stale exemption. Every entry
        must name a server that exists and a variable that server's script actually reads."""
        reads = {}
        for tier, rel in BAKED.items():
            cfg = load_baked_config(os.path.join(REPO, rel))
            for name, server in (cfg.get("mcp_servers") or {}).items():
                arg = server_script_arg(server)
                script = resolve_script(tier, arg) if arg else None
                if script:
                    reads.setdefault(name, set()).update(credential_reads(script))
        for (server, var), reason in ALLOWED_UNDECLARED.items():
            self.assertIn(server, reads, f"allow-list names unknown server {server}")
            self.assertIn(
                var, reads[server],
                f"allow-list exempts {server}.{var}, but no script for {server} reads it any more",
            )
            self.assertGreater(len(reason), 60, f"{server}.{var} needs a real reason")


if __name__ == "__main__":
    unittest.main()
