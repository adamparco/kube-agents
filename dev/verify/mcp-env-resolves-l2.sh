#!/usr/bin/env bash
# mcp-env-resolves-l2.sh — V-CMP-006 at L2.
#
# 09 §5.1:
#
#     every MCP server whose script reads a credential from the environment declares that variable
#     in its **own** `env:` block, asserted against the **runtime-authoritative** rendered ConfigMap
#     and not the image-baked `config.yaml` (§11.3). A container that holds the secret is not the
#     process that needs it: Hermes passes an MCP server only what its config declares, so a server
#     with no `env:` block reads an empty value and fails closed at the first call. Reports **fail**,
#     never partial — the symptom surfaces as a runtime refusal in a component whose pod is Ready
#     and whose unit tests are green.
#
# 09 §6 dates the row `L0, L2` at phase 9. The L0 arm — `dev/test_mcp_env_declared.py` — has been
# green since phase 8. The L2 arm was `deferred` in `verification/results.csv` with the blocker "no
# live-pod env reader exists yet", and the promotion condition written beside it was: *a
# dev/L2-CHAIN.txt arm that execs into a running agent pod, reads the resolved env of each
# mcpServers entry, and asserts it matches the rendered ConfigMap — exits 0.* This file is that arm.
#
# ------------------------------------------------------------------------------------------------
# WHAT L2 CAN SEE THAT L0 CANNOT — the reason this is not a second copy of the unit test
# ------------------------------------------------------------------------------------------------
# The L0 arm reads two artifacts that stand in for the runtime: the rendered ConfigMap GOLDEN (byte-
# locked to `renderConfigYAML()` by `internal/testing/golden_test.go`) and the scripts in the TREE.
# Both are inputs. Four things are decided after them, and only one of the four is a re-run of L0:
#
#   A1 SHADOWED   The bytes the agent process actually opens at its Hermes config path are the
#                 ConfigMap the operator rendered for THIS CR — not the copy baked into the image at
#                 the same path (LSN-003, and P6's whole content). L0 asserts about a golden and
#                 trusts the mount; this reads the file through the container's own filesystem view
#                 and compares it to the ConfigMap object, which is the only place the two can be
#                 shown to be the same bytes.
#   A2 DECLARED   The property statement itself, re-asked against the config the pod is running and
#                 the scripts the IMAGE shipped. The tree and the image are different artifacts: a
#                 script fixed in the tree and not rebuilt reads a credential the config never
#                 declares, and every L0 arm is green about it (LSN-001's shape, aimed at the agent
#                 image rather than at the operator's).
#   A3 RESOLVED   The half no static arm can reach. A declaration is a promise that the MCP server's
#                 process will RECEIVE the value; `env: {API_SERVER_KEY: ${API_SERVER_KEY}}` is
#                 satisfied on paper by a container that never sets `API_SERVER_KEY`, and Hermes
#                 then hands the server the literal seven-character string `${API_SERVER_KEY}`. That
#                 is 09 §5.1's own harm sentence — "reads an empty value and fails closed at the
#                 first call" — arriving through the substitution rather than through the block.
#   A4 SPAWNED   Every declared server is actually RUNNING in the container. A3 over zero processes
#                 is a green produced by not asking ([[LSN-035]]), so the census is an assertion and
#                 not a filter.
#
# ------------------------------------------------------------------------------------------------
# IMPORTED, NOT RESTATED
# ------------------------------------------------------------------------------------------------
# `verification/results.csv`'s V-CTN-004 row argues this for its own L2 arm and this file follows
# it. `CREDENTIAL_RE`, `ALLOWED_UNDECLARED`, `parse_yaml_subset`, `server_script_arg`,
# `credential_reads`, `env_vars_read`, `local_imports` and **`check_config` itself** are imported
# from `dev/test_mcp_env_declared.py` by path with importlib — the idiom `manager-role-l2.sh`
# established and `reader-scope-l2.sh` generalized. Arm A2 does not re-implement the rule; it calls
# the L0 function, verbatim, on runtime inputs.
#
# The ONE thing this file defines is the difference between the two levels: where a script named in
# `args` is resolved. L0 resolves `/opt/data/scripts/x.py` against `SCRIPT_ROOTS` in the tree; L2
# resolves it against the file the container actually has. So `resolve_script` is rebound to a
# directory holding the scripts read OUT OF THE POD and `check_config` is then called unchanged.
# A second copy of the expected credential set at L2 would be a second definition site, and its
# failure mode is L0 tightening while L2 keeps passing yesterday's set ([[LSN-024]]).
#
# ------------------------------------------------------------------------------------------------
# THE SUBJECT SET IS DERIVED, NEVER LISTED ([[LSN-036]])
# ------------------------------------------------------------------------------------------------
#   * the TIERS come from `agents/*/SOUL.md` — one directory per tier, the same set `make validate`
#     polices — cross-checked against the `spec.tier` enum in the Agent CRD. Disagreement is a
#     finding: two definition sites that drift is how a fourth tier ships unmeasured.
#   * the PODS are resolved from each CR's Deployment by OWNERSHIP (`p3_pod_of_deploy`), never by
#     `.items[0]` off a selector both generations answer to ([[LSN-024]]).
#   * the CONTAINERS are every container in that pod whose volumeMounts reach the volume backed by
#     the operator's rendered ConfigMap. Not "the agent container": the dashboard mounts the same
#     ConfigMap at the same path and therefore starts the same MCP servers, and a roster naming one
#     of them would have been a roster that could not see the other.
#   * the MOUNT PATH and the ConfigMap DATA KEY come off that volumeMount's `mountPath`/`subPath`.
#     Nothing below spells `/opt/data/config.yaml`.
#   * the MCP SERVERS come from the `mcp_servers` mapping in the config the pod is running.
#   * the VARIABLES come from each server's own `env:` block; which of them is a CREDENTIAL is
#     decided by the imported `CREDENTIAL_RE` and by nothing written here.
#   * the PROCESSES are the ones the kernel says are in the container under test: `/proc/<pid>` whose
#     `cgroup` equals the collector's own. The pod runs with a shared PID namespace, so a naive walk
#     of `/proc` sees every container's processes at once and would attribute the dashboard's
#     environment to the agent.
#
# ------------------------------------------------------------------------------------------------
# NON-CREDENTIAL PLACEHOLDERS ARE A NOTE, NOT A FAILURE — and why that is the row's own line
# ------------------------------------------------------------------------------------------------
# `platform_control` declares `GOOGLE_CHAT_PROJECT_ID` and `GOOGLE_CHAT_SUBSCRIPTION_NAME`
# unconditionally, and the operator injects them into the container only when
# `spec.integration.googleChat.enabled`. With the integration off, Hermes finds no variable to
# substitute and passes the placeholder through verbatim. Failing that would make this suite red on
# every correctly-configured pod that has no Google Chat, which is a lint people learn to skip.
#
# The line drawn is not an exemption list, it is V-CMP-006's own subject: the row is about the
# variables a script reads AS A CREDENTIAL, and `CREDENTIAL_RE` — imported — is the tree's single
# definition site for which names those are. Non-credential placeholders are reported as `NOTE:`
# lines with their values, so a reader sees them and no assertion is scored on them. A control row
# (`non-credential-placeholder-is-a-note`) asserts the distinction is real, because a rule that
# quietly widened to "any unresolved variable" would be red everywhere and a rule that quietly
# narrowed to nothing would be green everywhere.
#
# ------------------------------------------------------------------------------------------------
# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]])
# ------------------------------------------------------------------------------------------------
# `--negative-control` runs with NO cluster: it synthesises the observation directory — config text,
# script sources, process table and environments — for a two-container pod, injects one defect per
# row, and runs the SAME analyzer and the SAME assertion functions. Every statement that would have
# OBTAINED that input from a running cluster is therefore bypassed, and a green control says nothing
# whatever about any of them:
#   - the COLLECTION. Every `kubectl exec`, the cgroup comparison that decides which processes
#     belong to this container, the base64 round-trip, and the ConfigMap read are bypassed. A
#     collector that returned nothing would produce an empty observation live and a full one under
#     the control; only the census arm distinguishes them, and only on the live path.
#   - the SEEDING. The Agent CRs, the parent chain, `seed_agent_fixtures` and the wait for Available
#     are live-only. A CR that admission rejects is a live-only failure mode.
#   - P1/P3/P10/P12. Preconditions are asserted on the live path and skipped entirely offline, which
#     is deliberate: a suite that blocked on the one-suite-per-cluster lock during an L0 chain run
#     would be a worse outage than the one the lock prevents.
#   - WHETHER HERMES ACTUALLY USES the value it was handed. This suite asserts the value arrived at
#     the process. That an MCP server then authenticates with it is V-BRK's question, not this row's.
#
# ------------------------------------------------------------------------------------------------
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored ([[LSN-005]], [[LSN-018]]). This
# creates a namespace and three Agent CRs, so the guard is load-bearing. Every kubectl invocation
# below goes through `$K`, which carries `--context` explicitly; there is no bare kubectl in this
# file and the ambient context is never consulted.
#
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / could-not-run · 3 = DEFERRED.
# Usage: dev/verify/mcp-env-resolves-l2.sh [kube-context]
#        dev/verify/mcp-env-resolves-l2.sh --negative-control
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions).
#   P1 image-under-test: kubeagents-system/control-plane=controller-manager, asserted with
#      p1_assert_build_under_test. The ConfigMap under test and the container env that resolves its
#      placeholders are BOTH rendered by the operator (`agent_manifests.go`), so a stale operator
#      answers every arm below with last week's opinion. The AGENT image is a second artifact and is
#      deliberately NOT held to P1: it only has to be pullable and to carry the scripts, and its own
#      staleness is what arm A2 exists to detect rather than to assume away.
#   P3 admission-recreate: every Agent CR, via p3_force_recreate before each apply. The ConfigMap,
#      the Deployment and the pod env are reconcile OUTPUTS; a CR left over from an earlier run
#      carries the rendering of whatever operator was live when it was admitted ([[LSN-002]]).
#   P6 runtime-authoritative: the rendered ConfigMap the operator wrote for each CR, read BOTH from
#      the API server and through the container's own filesystem view, and compared. Never the
#      image-baked agents/<tier> config, which the mount shadows — arm A1 is the assertion that the
#      shadowing happened ([[LSN-003]]).
#   P10 control-plane health and P12 exclusive-L2 are taken together by
#      p10_assert_control_plane_healthy before the first claim; rc 2 is could-not-run, never a fail.
#   P11 is not applicable: nothing here needs name resolution from inside the pod. The suite reads
#      files and process state through the API server's exec channel, which the kubelet serves.
set -uo pipefail

MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=mcp-env-l2
# The scope these fixtures occupy. Distinct from every other suite's so the cardinality webhook
# (one agent per tier per scope) never sees this run as a collision with a neighbour's leftovers.
PROJECT=mcp-env-l2-project
CLUSTER=cluster-a

TAB="$(printf '\t')"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mcp-env-l2.XXXXXX")" || exit 2
OBS="$WORK/obs"
ANALYZE="$WORK/analyze.py"
mkdir -p "$OBS"

fail=0
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}
note() { echo "NOTE: $1"; }

# ------------------------------------------------------------------------------------------------
# The analyzer. Written out rather than kept in a sibling file so the L0 import, the judgement and
# the synthesised control world all move in one diff — and so `probe-tags-match-their-suite.py`
# has no suite/probe pair to police that would only ever restate what is three inches above it.
# ------------------------------------------------------------------------------------------------
cat >"$ANALYZE" <<'PYEOF'
"""Turn an observation directory into a line-tag transcript for mcp-env-resolves-l2.sh.

Not a standalone check. The suite owns every cluster call and writes what it collected into
$OBS/<subject>/; this file owns every judgement and prints one JSON object per scenario.

Subcommands:
  live <obsdir> <repo>              judge what the suite collected
  control <mutation> <obsdir> <repo>  synthesise a two-container pod, inject one defect, judge it
  mutations                         list the mutation names, one per line
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

# What a `${VAR}` looks like after Hermes has FAILED to substitute it. Not a formatting detail: the
# whole of arm A3 is the difference between this string and a value.
PLACEHOLDER_OPEN = "${"


def load_l0(repo: str):
    """V-CMP-006's L0 arm, imported by path. Its constants are the expectation this file scores."""
    rel = "dev/test_mcp_env_declared.py"
    path = pathlib.Path(repo) / rel
    if not path.exists():
        raise SystemExit(
            "FAIL: %s is missing; there is nothing to import, and restating CREDENTIAL_RE and "
            "ALLOWED_UNDECLARED here would make this file a second definition site for them "
            "(V-MET-013, [[LSN-024]])." % rel
        )
    spec = importlib.util.spec_from_file_location("test_mcp_env_declared", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit(scenario: str, **fields) -> None:
    fields["scenario"] = scenario
    print(json.dumps(fields, sort_keys=True))


# -------------------------------------------------------------------------------------------
# Reading one subject's observation
# -------------------------------------------------------------------------------------------


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def load_subject(d: pathlib.Path) -> dict:
    """One (pod, container) pair, as the suite's collector left it on disk."""
    procs = []
    for line in _read(d / "procs.txt").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        kind, pid, payload = parts
        if kind != "P":
            continue
        procs.append({"pid": pid, "cmdline": payload, "env": {}})
    by_pid = {p["pid"]: p for p in procs}
    for line in _read(d / "procs.txt").splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] != "E":
            continue
        _, pid, payload = parts
        target = by_pid.get(pid)
        if target is None:
            continue
        for item in payload.split("\x00"):
            if "=" in item:
                name, _, value = item.partition("=")
                target["env"][name] = value
    scripts = {}
    sdir = d / "scripts"
    if sdir.is_dir():
        for f in sorted(sdir.iterdir()):
            if f.is_file():
                scripts[f.name] = f
    return {
        "name": d.name,
        "tier": _read(d / "tier").strip(),
        "container": _read(d / "container").strip(),
        "cm": _read(d / "cm-config.yaml"),
        "pod": _read(d / "pod-config.yaml"),
        "scripts": scripts,
        "script_dir": sdir,
        "procs": procs,
    }


# -------------------------------------------------------------------------------------------
# The four arms
# -------------------------------------------------------------------------------------------


def arm_shadowed(s: dict) -> None:
    """A1. The bytes the container opens ARE the ConfigMap the operator rendered."""
    tag = "shadow:" + s["name"]
    if not s["cm"].strip():
        emit(tag, outcome="no-configmap", detail="the ConfigMap carries no config.yaml key")
        return
    if not s["pod"].strip():
        emit(tag, outcome="no-file", detail="the container has nothing at its Hermes config path")
        return
    if s["cm"] == s["pod"]:
        emit(tag, outcome="same", detail="%d bytes, identical" % len(s["pod"]))
        return
    emit(
        tag,
        outcome="differs",
        detail="ConfigMap %d bytes, in-container file %d bytes; first difference at offset %d"
        % (len(s["cm"]), len(s["pod"]), _first_diff(s["cm"], s["pod"])),
    )


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def arm_declared(s: dict, L0) -> None:
    """A2. V-CMP-006's own sentence, asked of the running config and the shipped scripts.

    `check_config` is called UNCHANGED. Only `resolve_script` is rebound, because where a script
    lives is exactly and only what differs between L0 and L2.
    """
    tag = "declare:" + s["name"]
    try:
        cfg = L0.parse_yaml_subset(s["pod"])
    except Exception as exc:  # the parser raises rather than truncating; say so
        emit(tag, outcome="unparseable", count=0, detail="the running config did not parse: %s" % exc)
        return
    if not isinstance(cfg, dict):
        emit(tag, outcome="unparseable", count=0, detail="the running config is not a mapping")
        return

    scripts = s["scripts"]
    saved_resolve, saved_repo = L0.resolve_script, L0.REPO
    resolved = lambda tier, arg, _m=scripts: (  # noqa: E731 — a one-line rebinding, not a helper
        str(_m[os.path.basename(arg)]) if os.path.basename(arg) in _m else None
    )
    L0.resolve_script = resolved
    # So the imported message renders `scripts/x.py` instead of forty levels of `../`.
    L0.REPO = str(s["script_dir"].parent)
    try:
        violations = L0.check_config(s["tier"], cfg, "pod/" + s["name"])
    except Exception as exc:
        emit(tag, outcome="unparseable", count=0,
             detail="the imported L0 rule could not be applied: %r" % (exc,))
        return
    finally:
        L0.resolve_script, L0.REPO = saved_resolve, saved_repo

    if violations:
        emit(tag, outcome="violations", count=len(violations), detail=" | ".join(violations))
    else:
        emit(tag, outcome="clean", count=0, detail="")


def _servers(s: dict, L0) -> dict:
    try:
        cfg = L0.parse_yaml_subset(s["pod"])
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    servers = cfg.get("mcp_servers") or {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def arm_processes_and_resolution(s: dict, L0) -> tuple:
    """A4 then A3. Which servers are running here, and what did each one actually receive?

    Returned counts feed the census; the per-variable verdicts are emitted as their own tags so the
    suite can score each one by name rather than on a total.
    """
    servers = _servers(s, L0)
    cred_declarations = 0
    resolve_rows = 0
    for name in sorted(servers):
        server = servers[name]
        arg = L0.server_script_arg(server)
        if arg is None:
            continue  # not a script-backed server; it has no process of its own to inspect
        procs = [p for p in s["procs"] if arg in p["cmdline"]]
        declared = server.get("env") or {}
        if not isinstance(declared, dict):
            declared = {}
        # Does this server have anything for V-CMP-006 to be about? A server that declares no
        # credential-shaped variable is not a subject of the row, so its absence from the process
        # table costs the measurement nothing. Observed live: `developer_knowledge` runs
        # mcp_http_bridge.py against a remote endpoint, carries no `env:` block at all, and is not
        # spawned until something calls it. Failing on that would be a red about Hermes' spawn
        # policy wearing V-CMP-006's name.
        server_creds = sorted(v for v in declared if L0.CREDENTIAL_RE.search(v))
        ptag = "spawned:%s:%s" % (s["name"], name)
        if not procs:
            emit(
                ptag,
                outcome="none",
                count=0,
                credential="yes" if server_creds else "no",
                detail="no live process in this container runs %s%s"
                % (arg, (" — and it declares the credential(s) " + ", ".join(server_creds))
                   if server_creds else ", and it declares no credential"),
            )
        else:
            emit(
                ptag,
                outcome="running",
                count=len(procs),
                credential="yes" if server_creds else "no",
                detail="pids " + ",".join(p["pid"] for p in procs),
            )

        for var in sorted(declared):
            credential = bool(L0.CREDENTIAL_RE.search(var))
            if credential:
                cred_declarations += 1
            if not procs:
                continue  # A4 already owns this; a second red per variable would drown it
            for p in procs:
                rtag = "resolve:%s:%s:%s:%s" % (s["name"], name, var, p["pid"])
                resolve_rows += 1
                if var not in p["env"]:
                    emit(
                        rtag,
                        outcome="absent",
                        credential="yes" if credential else "no",
                        declared=str(declared[var]),
                        detail="the config declares it and the process never received it",
                    )
                elif PLACEHOLDER_OPEN in p["env"][var]:
                    emit(
                        rtag,
                        outcome="placeholder",
                        credential="yes" if credential else "no",
                        declared=str(declared[var]),
                        detail="arrived as %r — the substitution did not happen, so the container "
                        "this server runs in never set the variable" % p["env"][var],
                    )
                elif p["env"][var] == "":
                    emit(
                        rtag,
                        outcome="empty",
                        credential="yes" if credential else "no",
                        declared=str(declared[var]),
                        detail="arrived empty",
                    )
                else:
                    emit(
                        rtag,
                        outcome="resolved",
                        credential="yes" if credential else "no",
                        declared=str(declared[var]),
                        detail="%d bytes" % len(p["env"][var]),
                    )
    return len(servers), cred_declarations, resolve_rows


def judge(obsdir: pathlib.Path, repo: str) -> None:
    L0 = load_l0(repo)
    tiers = [t for t in _read(obsdir / "tiers.txt").split() if t]
    subjects = sorted(d for d in obsdir.iterdir() if d.is_dir())
    seen_tiers = set()
    for d in subjects:
        s = load_subject(d)
        seen_tiers.add(s["tier"])
        arm_shadowed(s)
        arm_declared(s, L0)
        n_servers, n_cred, n_rows = arm_processes_and_resolution(s, L0)
        emit(
            "census:" + s["name"],
            tier=s["tier"],
            container=s["container"],
            servers=n_servers,
            credentials=n_cred,
            resolveRows=n_rows,
            procs=len(s["procs"]),
            scripts=len(s["scripts"]),
        )
    emit(
        "roster",
        tiers=" ".join(tiers),
        covered=" ".join(sorted(seen_tiers)),
        subjects=len(subjects),
    )


# -------------------------------------------------------------------------------------------
# The synthesised world the ¬ mode judges. One clean pod, then one defect at a time.
# -------------------------------------------------------------------------------------------

CLEAN_CONFIG = """\
mcp_servers:
  agent_common:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/agent_common_server.py
    env:
      API_SERVER_KEY: ${API_SERVER_KEY}
      HERMES_HOME: ${HERMES_HOME}
  platform_control:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/platform_mcp_server.py
    env:
      API_SERVER_KEY: ${API_SERVER_KEY}
      GOOGLE_CHAT_PROJECT_ID: ${GOOGLE_CHAT_PROJECT_ID}
  developer_knowledge:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/mcp_http_bridge.py
      - https://developerknowledge.example/mcp
platform_toolsets:
  cli:
    - mcp-agent_common
    - mcp-platform_control
"""

CLEAN_SCRIPTS = {
    "agent_common_server.py": 'import os\n\n\ndef k():\n    return os.environ["API_SERVER_KEY"]\n',
    "platform_mcp_server.py": 'import os\n\n\ndef k():\n    return os.getenv("API_SERVER_KEY", "")\n',
    # No credential read and no `env:` block in the config above — the shape of a bridge to a
    # remote endpoint, which is what `developer_knowledge` is on a live platform pod.
    "mcp_http_bridge.py": "import sys\n\n\ndef url():\n    return sys.argv[1]\n",
}

# What each server's process holds when everything worked. `GOOGLE_CHAT_PROJECT_ID` is left as a
# placeholder in the CLEAN world on purpose: that is what a correct pod with the Google Chat
# integration switched off looks like, and a control whose clean input had it resolved would never
# exercise the credential/non-credential line arm A3 draws.
CLEAN_ENV = {
    "agent_common_server.py": {
        "API_SERVER_KEY": "c27c7f0f665c9200c488ca76d22bedfd",
        "HERMES_HOME": "/opt/data",
    },
    "platform_mcp_server.py": {
        "API_SERVER_KEY": "c27c7f0f665c9200c488ca76d22bedfd",
        "GOOGLE_CHAT_PROJECT_ID": "${GOOGLE_CHAT_PROJECT_ID}",
    },
    "mcp_http_bridge.py": {"HERMES_HOME": "/opt/data"},
}

MUTATIONS = (
    "clean",
    "pod-config-differs-from-the-configmap",
    "configmap-has-no-config-key",
    "api-server-key-stripped-from-platform_control",
    "toolset-names-a-server-nobody-declares",
    "script-is-not-in-the-image",
    "credential-arrives-as-an-unexpanded-placeholder",
    "credential-arrives-empty",
    "credential-never-reached-the-process",
    "non-credential-placeholder-is-a-note",
    "declared-server-has-no-process",
    "server-with-no-credentials-has-no-process",
    "a-tier-contributed-no-subject",
    "no-subject-mounts-the-rendered-config",
    "no-server-declares-a-credential",
)


def _write_subject(root: pathlib.Path, name, tier, container, cm, pod, scripts, procs) -> None:
    d = root / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "tier").write_text(tier)
    (d / "container").write_text(container)
    (d / "cm-config.yaml").write_text(cm)
    (d / "pod-config.yaml").write_text(pod)
    for fname, body in scripts.items():
        (d / "scripts" / fname).write_text(body)
    lines = []
    for pid, cmdline, env in procs:
        lines.append("P\t%s\t%s" % (pid, cmdline))
        lines.append("E\t%s\t%s" % (pid, "\x00".join("%s=%s" % kv for kv in sorted(env.items()))))
    (d / "procs.txt").write_text("\n".join(lines) + ("\n" if lines else ""))


def _clean_procs(env_overrides=None, drop=()):
    """The two MCP server processes a healthy container runs, one per script."""
    out = []
    pid = 100
    for script, env in sorted(CLEAN_ENV.items()):
        if script in drop:
            continue
        pid += 1
        e = dict(env)
        if env_overrides and script in env_overrides:
            for k, v in env_overrides[script].items():
                if v is None:
                    e.pop(k, None)
                else:
                    e[k] = v
        out.append(
            (
                str(pid),
                "/opt/hermes/.venv/bin/python3 /opt/data/scripts/%s " % script,
                e,
            )
        )
    return out


def synthesise(mutation: str, root: pathlib.Path) -> None:
    """A two-container pod — the agent and its dashboard — with one defect injected.

    TWO containers and not one, because the shape this suite found on its first live run is a
    SECOND container mounting the same ConfigMap without the environment that resolves it. A
    single-container control could not have exhibited it ([[LSN-015]]).
    """
    tiers = ["platform"]
    cfg = CLEAN_CONFIG
    cm = CLEAN_CONFIG
    scripts = dict(CLEAN_SCRIPTS)
    procs = _clean_procs()
    dash_procs = _clean_procs()

    if mutation == "pod-config-differs-from-the-configmap":
        cfg = CLEAN_CONFIG.replace("HERMES_HOME: ${HERMES_HOME}", "HERMES_HOME: /baked/data")
    elif mutation == "configmap-has-no-config-key":
        cm = ""
    elif mutation == "api-server-key-stripped-from-platform_control":
        # 09 §5.1's mandated control, verbatim: strip API_SERVER_KEY from a fixture's
        # platform_control block and confirm the check goes red.
        cfg = cm = CLEAN_CONFIG.replace(
            "      API_SERVER_KEY: ${API_SERVER_KEY}\n"
            "      GOOGLE_CHAT_PROJECT_ID: ${GOOGLE_CHAT_PROJECT_ID}\n",
            "      GOOGLE_CHAT_PROJECT_ID: ${GOOGLE_CHAT_PROJECT_ID}\n",
        )
    elif mutation == "toolset-names-a-server-nobody-declares":
        cfg = cm = CLEAN_CONFIG.replace(
            "    - mcp-platform_control\n",
            "    - mcp-platform_control\n    - mcp-does_not_exist\n",
        )
    elif mutation == "script-is-not-in-the-image":
        scripts.pop("platform_mcp_server.py")
    elif mutation == "credential-arrives-as-an-unexpanded-placeholder":
        procs = _clean_procs({"agent_common_server.py": {"API_SERVER_KEY": "${API_SERVER_KEY}"}})
    elif mutation == "credential-arrives-empty":
        procs = _clean_procs({"agent_common_server.py": {"API_SERVER_KEY": ""}})
    elif mutation == "credential-never-reached-the-process":
        procs = _clean_procs({"agent_common_server.py": {"API_SERVER_KEY": None}})
    elif mutation == "non-credential-placeholder-is-a-note":
        procs = _clean_procs({"agent_common_server.py": {"HERMES_HOME": "${HERMES_HOME}"}})
    elif mutation == "declared-server-has-no-process":
        procs = _clean_procs(drop=("platform_mcp_server.py",))
    elif mutation == "server-with-no-credentials-has-no-process":
        procs = _clean_procs(drop=("mcp_http_bridge.py",))
    elif mutation == "a-tier-contributed-no-subject":
        tiers = ["platform", "developer-team"]
    elif mutation == "no-server-declares-a-credential":
        cfg = cm = CLEAN_CONFIG.replace("      API_SERVER_KEY: ${API_SERVER_KEY}\n", "")
        scripts = {k: "import os\n" for k in CLEAN_SCRIPTS}
        procs = dash_procs = _clean_procs(
            {k: {"API_SERVER_KEY": None} for k in CLEAN_ENV}
        )

    (root / "tiers.txt").write_text(" ".join(tiers) + "\n")
    if mutation == "no-subject-mounts-the-rendered-config":
        return
    _write_subject(root, "platform.platform-agent.platform-agent", "platform",
                   "platform-agent", cm, cfg, scripts, procs)
    _write_subject(root, "platform.platform-agent.platform-agent-dashboard", "platform",
                   "platform-agent-dashboard", cm, cfg, scripts, dash_procs)


def main(argv: list) -> int:
    if not argv:
        raise SystemExit("usage: analyze.py live|control|mutations ...")
    if argv[0] == "mutations":
        for m in MUTATIONS:
            print(m)
        return 0
    if argv[0] == "live":
        judge(pathlib.Path(argv[1]), argv[2])
        return 0
    if argv[0] == "control":
        mutation, obsdir, repo = argv[1], pathlib.Path(argv[2]), argv[3]
        if mutation not in MUTATIONS:
            raise SystemExit("unknown mutation %r" % mutation)
        obsdir.mkdir(parents=True, exist_ok=True)
        synthesise(mutation, obsdir)
        judge(obsdir, repo)
        return 0
    raise SystemExit("unknown subcommand %r" % argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PYEOF

# ------------------------------------------------------------------------------------------------
# The transcript, and the accessors that read it. Same shape as broker-auth-l2.sh: the analyzer
# emits one JSON object per scenario, the suite flattens it to TSV keyed on the scenario name, and
# every assertion below reads named fields off that one string. The `¬` mode replaces $FLAT with a
# transcript computed from a synthesised world and calls the identical functions.
# ------------------------------------------------------------------------------------------------
FLAT=""

flatten() { # stdin: JSON lines -> TSV: scenario, outcome, count, credential, detail, tier, extra
  python3 -c '
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    print("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in (
        r.get("scenario") or "",
        r.get("outcome") or "",
        r.get("count") if r.get("count") is not None else "",
        r.get("credential") or "",
        (r.get("detail") or "")[:1200],
        r.get("tier") or r.get("tiers") or "",
        r.get("covered") or r.get("declared") or r.get("container") or "",
        r.get("servers") if r.get("servers") is not None else r.get("subjects", ""),
        r.get("credentials") if r.get("credentials") is not None else "",
        r.get("procs") if r.get("procs") is not None else "",
    )))
'
}

field() { # <scenario> <1=outcome 2=count 3=credential 4=detail 5=tier 6=covered 7=n1 8=n2 9=procs>
  printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" -v i="$(($2 + 1))" '$1 == s { print $i; exit }'
}
seen() { printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" '$1 == s { found = 1 } END { exit !found }'; }
scenarios_with_prefix() {
  printf '%s\n' "$FLAT" | awk -F'\t' -v p="$1" 'index($1, p) == 1 { print $1 }'
}

# subjects — every (pod, container) the analyzer judged, in transcript order.
subjects() { scenarios_with_prefix "census:" | sed 's/^census://'; }

# ------------------------------------------------------------------------------------------------
# The arms. Pure functions of $FLAT — no cluster, no globals but the transcript — so the `¬` mode
# reaches every one of them ([[LSN-060]]: an arm the control cannot call is an arm the control does
# not cover, whatever the row count says).
# ------------------------------------------------------------------------------------------------

# A1 — the bytes the container opens are the ConfigMap the operator rendered (P6, LSN-003).
assert_shadowed() {
  local s outcome detail
  for s in $(subjects); do
    outcome="$(field "shadow:$s" 1)"
    detail="$(field "shadow:$s" 4)"
    case "$outcome" in
      same) pass "A1 $s: the running config IS the rendered ConfigMap ($detail)" ;;
      differs)
        bad "A1 $s: the file the container opens differs from the ConfigMap the operator rendered — the mount is not shadowing the image-baked copy, so every arm below would be judging an input instead of the artifact ($detail)"
        ;;
      no-configmap)
        bad "A1 $s: the operator rendered no config.yaml key for this agent, so there is no runtime-authoritative artifact to compare against ($detail)"
        ;;
      no-file)
        bad "A1 $s: the container has no file at its Hermes config path ($detail)"
        ;;
      *) bad "A1 $s: the probe reported no shadowing verdict at all" ;;
    esac
  done
}

# A2 — V-CMP-006's own sentence, via the imported L0 check_config.
assert_declared() {
  local s outcome n detail
  for s in $(subjects); do
    outcome="$(field "declare:$s" 1)"
    n="$(field "declare:$s" 2)"
    detail="$(field "declare:$s" 4)"
    case "$outcome" in
      clean) pass "A2 $s: every credential an MCP server's shipped script reads is declared in that server's own env: block" ;;
      violations) bad "A2 $s: $n undeclared-credential violation(s) in the running config: $detail" ;;
      unparseable) bad "A2 $s: the running config did not parse, so the declaration rule could not be applied to it: $detail" ;;
      *) bad "A2 $s: the probe reported no declaration verdict at all" ;;
    esac
  done
}

# A4 — every script-backed server that DECLARES A CREDENTIAL is running here, so that A3 has a
# process to read. Scored before A3 because a server with no process makes A3 vacuous for it, and a
# vacuous pass is a failure ([[LSN-035]]).
#
# The scope is the credential-declaring servers and not all of them, for the same reason A3's is:
# V-CMP-006 is about the variables a server declares, so a server that declares none has nothing
# here to go unmeasured. It is reported either way — the NOTE is what keeps the narrowing visible
# instead of silent.
assert_spawned() {
  local s n outcome credential detail
  for s in $(scenarios_with_prefix "spawned:"); do
    outcome="$(field "$s" 1)"
    n="$(field "$s" 2)"
    credential="$(field "$s" 3)"
    detail="$(field "$s" 4)"
    if [ "$outcome" = running ]; then
      pass "A4 ${s#spawned:}: $n live process(es) ($detail)"
    elif [ "$credential" = yes ]; then
      bad "A4 ${s#spawned:}: no live process. The config declares this MCP server WITH a credential and the container is not running it, so the one thing V-CMP-006 asks about it — what its process received — went unmeasured, and every green below is green over a smaller population than the config describes ($detail)"
    else
      note "A4 ${s#spawned:} is not running ($detail), so nothing was measured about it; it declares no credential, so there was nothing to measure"
    fi
  done
}

# A3 — the resolved environment of each declared variable, at the process Hermes spawned.
assert_resolved() {
  local s outcome credential detail
  for s in $(scenarios_with_prefix "resolve:"); do
    outcome="$(field "$s" 1)"
    credential="$(field "$s" 3)"
    detail="$(field "$s" 4)"
    if [ "$credential" != yes ]; then
      # Not this row's subject. Reported so a reader sees it; never scored. See the header.
      [ "$outcome" = resolved ] || note "A3 ${s#resolve:} is not credential-shaped and $outcome: $detail"
      continue
    fi
    case "$outcome" in
      resolved) pass "A3 ${s#resolve:}: the credential reached the MCP server's process with a value ($detail)" ;;
      placeholder)
        bad "A3 ${s#resolve:}: the credential reached the process as an unexpanded placeholder. Hermes passes an MCP server only what its config declares, and it can only substitute a variable the CONTAINER holds — so this server authenticates with the literal text of its own config and fails closed at the first call (09 §5.1). $detail"
        ;;
      empty)
        bad "A3 ${s#resolve:}: the credential arrived empty at the MCP server's process, which is the failure 09 §5.1 describes with the declaration present. $detail"
        ;;
      absent)
        bad "A3 ${s#resolve:}: the credential never reached the process. Its env: block declares it and the environment Hermes handed the server does not carry it at all. $detail"
        ;;
      *) bad "A3 ${s#resolve:}: the probe reported no resolution verdict at all" ;;
    esac
  done
}

# A5 — the floors. Every population this suite enumerates has one, because each of them is a set
# that can silently empty and read as containment ([[LSN-035]], [[LSN-038]]).
assert_census() {
  local s n_subjects tiers covered t n_servers n_cred n_procs missing=""
  n_subjects="$(subjects | grep -c .)"
  if [ "${n_subjects:-0}" -lt 1 ]; then
    bad "A5 VACUOUS: no (pod, container) pair mounts the operator-rendered config, so every arm above ran over an empty set and reported nothing as compliance"
    return
  fi
  pass "A5: $n_subjects (pod, container) subject(s) judged"

  if ! seen roster; then
    bad "A5 VACUOUS: the probe emitted no roster line, so the tier coverage below cannot be checked"
    return
  fi
  tiers="$(field roster 5)"
  covered="$(field roster 6)"
  for t in $tiers; do
    case " $covered " in
      *" $t "*) : ;;
      *) missing="$missing $t" ;;
    esac
  done
  if [ -n "$missing" ]; then
    bad "A5 VACUOUS: tier(s)${missing} contributed no subject. The tree defines them, the CRD accepts them, and this run measured none of their pods — a sweep that skipped a tier is not a smaller sweep, it is an unmeasured one"
  else
    pass "A5: every tier the tree defines contributed at least one subject ($tiers)"
  fi

  for s in $(subjects); do
    n_servers="$(field "census:$s" 7)"
    n_cred="$(field "census:$s" 8)"
    n_procs="$(field "census:$s" 9)"
    if [ "${n_servers:-0}" -lt 1 ]; then
      bad "A5 VACUOUS: $s declares no mcp_servers at all, so A2/A3/A4 asked nothing of it"
    elif [ "${n_cred:-0}" -lt 1 ]; then
      bad "A5 VACUOUS: $s declares $n_servers MCP server(s) and not one credential-shaped variable among them, so A3 scored nothing — V-CMP-006 has no subject on this pod"
    elif [ "${n_procs:-0}" -lt 1 ]; then
      bad "A5 VACUOUS: $s reported no processes at all; the collector saw nothing in this container's cgroup and A3/A4 are green over an empty process table"
    else
      pass "A5 $s: $n_servers server(s), $n_cred credential declaration(s), $n_procs process(es)"
    fi
  done
}

run_arms() {
  assert_shadowed
  assert_declared
  assert_spawned
  assert_resolved
  assert_census
}

# ------------------------------------------------------------------------------------------------
# `--negative-control` — the mandatory ¬ (09 §5.1 names one; 09 §6.14/V-MET-014 requires it carry a
# per-row signal). Every row synthesises the whole observation directory, injects exactly ONE
# defect, and asserts the arm that targets it — and only that arm — goes red on the needle.
# ------------------------------------------------------------------------------------------------
nc_total=0
nc_caught=0
nc_rc=0

nc_ok() {
  nc_caught=$((nc_caught + 1))
  echo "  ok     $1 — $2"
}
nc_miss() {
  nc_rc=1
  echo "  MISS   $1 — $2"
  [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/         /'
  return 0
}
nc_broken() {
  nc_rc=1
  echo "  BROKEN $1 — $2"
  [ -n "${3:-}" ] && printf '%s\n' "$3" | sed 's/^/         /'
  return 0
}

# nc_row <mutation> <green|red|red1|redn> <needle>
#   Builds the world, runs the analyzer, runs EVERY arm over the result, and scores.
#
#   green   the arms accept a correct world: no FAIL, at least one PASS. Without these rows a suite
#           of always-red arms would score a perfect control and be worthless live.
#   red1    exactly one FAIL, containing the needle. Used where the defect is planted in ONE
#           container's process table, so exactly one subject should notice it and its neighbour in
#           the same pod should stay green.
#   redn    exactly one FAIL PER SUBJECT, every one of them containing the needle. Used where the
#           defect is in the CONFIG, which both containers of the pod mount: a red from only one of
#           them would mean the sweep stopped early, and a red count above the subject count would
#           mean a second arm went off for an unrelated reason.
#   red     at least one FAIL, one of which contains the needle. The weakest form, for a defect that
#           legitimately trips more than one arm.
nc_row() {
  local mutation="$1" expect="$2" needle="$3"
  local dir out transcript n_fail n_any n_sub n_needle
  nc_total=$((nc_total + 1))
  dir="$WORK/nc/$mutation"
  rm -rf "$dir"
  mkdir -p "$dir"
  transcript="$(python3 "$ANALYZE" control "$mutation" "$dir" "$REPO_ROOT" 2>&1)"
  case "$transcript" in
    *'{'*) : ;;
    *)
      nc_broken "$mutation" "the analyzer produced no transcript, so this row judged nothing" "$transcript"
      return 0
      ;;
  esac
  FLAT="$(printf '%s\n' "$transcript" | flatten)"
  if [ -z "$FLAT" ]; then
    nc_broken "$mutation" "the transcript flattened to nothing" "$transcript"
    return 0
  fi
  out="$(run_arms 2>&1)"
  n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
  n_any="$(printf '%s\n' "$out" | grep -cE '^(PASS|FAIL):')"

  if [ "$n_any" -eq 0 ]; then
    nc_broken "$mutation" "the arms emitted no PASS and no FAIL. Nothing was evaluated, so this row is not a finding about the check — the arm it targets returns early or has been deleted ([[LSN-063]])" "$out"
    return 0
  fi

  case "$expect" in
    green)
      if [ "$n_fail" -eq 0 ]; then
        nc_ok "$mutation" "a CORRECT world is accepted ($n_any arm(s) ran), so the rows below are not always-red"
      else
        nc_miss "$mutation" "a CORRECT world was failed $n_fail time(s); every defect below would then be 'caught' for a reason that has nothing to do with it" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      fi
      ;;
    red1)
      if [ "$n_fail" -ne 1 ]; then
        nc_miss "$mutation" "expected EXACTLY one red arm and got $n_fail. The defect is supposed to be visible to one named arm while its neighbours stay green; a block of reds does not distinguish it from a probe that could not read the pod at all" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      elif printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
        nc_ok "$mutation" "caught by exactly the one arm that targets it ('$needle')"
      else
        nc_miss "$mutation" "went red once and the line does not mention '$needle', so the property it targets is not what caught it" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      fi
      ;;
    redn)
      n_sub="$(subjects | grep -c .)"
      n_needle="$(printf '%s\n' "$out" | grep '^FAIL:' | grep -cF "$needle")"
      if [ "$n_fail" -ne "$n_sub" ]; then
        nc_miss "$mutation" "the defect is in the config both containers mount, so every one of the $n_sub subject(s) should have gone red exactly once; $n_fail did. Fewer means the sweep stopped before it reached a subject, more means a second arm fired for an unrelated reason" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      elif [ "$n_needle" -ne "$n_sub" ]; then
        nc_miss "$mutation" "$n_fail red arm(s) and only $n_needle of them mention '$needle', so at least one subject went red for a different reason than the planted defect" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      else
        nc_ok "$mutation" "every one of the $n_sub subject(s) that mounts the defective config caught it ('$needle'), and nothing else went red"
      fi
      ;;
    red)
      if printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
        nc_ok "$mutation" "caught by the arm that targets it ('$needle'), $n_fail red arm(s) in total"
      else
        nc_miss "$mutation" "went red $n_fail time(s) and no FAIL line mentions '$needle', so the property it targets is not what caught it" "$(printf '%s\n' "$out" | grep '^FAIL:')"
      fi
      ;;
    *) nc_broken "$mutation" "unknown expectation '$expect'" ;;
  esac
  return 0
}

# nc_note_row <mutation> <needle>
#   The one row whose property is that an arm STAYS QUIET. A non-credential variable that did not
#   resolve must produce a NOTE and no assertion: an arm that widened to "any unresolved variable"
#   would be red on every pod with Google Chat switched off, and one that narrowed to nothing would
#   be green on the placeholder defect above. Scored separately so the "no output means the arm was
#   deleted" guard can stay strict everywhere else.
nc_note_row() {
  local mutation="$1" needle="$2" dir out transcript n_fail
  nc_total=$((nc_total + 1))
  dir="$WORK/nc/$mutation"
  rm -rf "$dir"
  mkdir -p "$dir"
  transcript="$(python3 "$ANALYZE" control "$mutation" "$dir" "$REPO_ROOT" 2>&1)"
  FLAT="$(printf '%s\n' "$transcript" | flatten)"
  out="$(run_arms 2>&1)"
  n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
  if [ "$n_fail" -ne 0 ]; then
    nc_miss "$mutation" "an unresolved NON-credential variable was scored as a failure $n_fail time(s). CREDENTIAL_RE is imported from the L0 arm precisely so this line is drawn in one place" "$(printf '%s\n' "$out" | grep '^FAIL:')"
  elif printf '%s\n' "$out" | grep '^NOTE:' | grep -qF "$needle"; then
    nc_ok "$mutation" "reported as a NOTE naming the variable ('$needle') and scored as nothing"
  else
    nc_miss "$mutation" "no NOTE mentions '$needle', so the unresolved non-credential variable was silently dropped rather than reported" "$out"
  fi
  return 0
}

run_negative_control() {
  echo
  echo "-- A1: the running config IS the rendered ConfigMap (assert_shadowed) --"
  nc_row clean green '-'
  nc_row pod-config-differs-from-the-configmap redn "differs from the ConfigMap the operator rendered"
  nc_row configmap-has-no-config-key redn "rendered no config.yaml key"

  echo
  echo "-- A2: every credential a script reads is declared in its own env: block (imported check_config) --"
  # 09 §5.1's mandated control, run through the L0 function rather than beside it.
  nc_row api-server-key-stripped-from-platform_control redn "API_SERVER_KEY"
  nc_row toolset-names-a-server-nobody-declares redn "does_not_exist"
  nc_row script-is-not-in-the-image redn "resolves to no file"

  echo
  echo "-- A3: the resolved environment at the process Hermes spawned (assert_resolved) --"
  nc_row credential-arrives-as-an-unexpanded-placeholder red1 "unexpanded placeholder"
  nc_row credential-arrives-empty red1 "arrived empty"
  nc_row credential-never-reached-the-process red1 "never reached the process"
  nc_note_row non-credential-placeholder-is-a-note "HERMES_HOME"

  echo
  echo "-- A4: every credential-declaring server is running here (assert_spawned) --"
  nc_row declared-server-has-no-process red1 "no live process"
  nc_note_row server-with-no-credentials-has-no-process "declares no credential"

  echo
  echo "-- A5: the floors (assert_census) --"
  nc_row a-tier-contributed-no-subject red1 "contributed no subject"
  nc_row no-subject-mounts-the-rendered-config red1 "no (pod, container) pair mounts"
  nc_row no-server-declares-a-credential redn "not one credential-shaped variable"

  echo
  echo "===================================================================="
  echo " negative control: $nc_caught/$nc_total"
  if [ "$nc_rc" -eq 0 ]; then
    echo " NEGATIVE CONTROL PASSED — every synthesised defect was rejected by the arm that targets"
    echo " it, and the correct world was accepted. V-CMP-006's live green is a measurement."
    echo " NOT COVERED BY THIS MODE: see the NEGATIVE CONTROL DOES NOT EXERCISE block in the header."
    echo "===================================================================="
    return 0
  fi
  echo " NEGATIVE CONTROL FAILED — a MISS is an arm that cannot see its own defect; a BROKEN is a"
  echo " row that was never an experiment. They call for opposite repairs ([[LSN-063]])."
  echo "===================================================================="
  return 1
}

if [ "$MODE" = negative-control ]; then
  echo "===================================================================="
  echo " mcp-env-resolves-l2.sh --negative-control — the mandatory ¬ for V-CMP-006"
  echo " Can the arms tell a pod that resolves its MCP credentials from one that does not?"
  echo "===================================================================="
  run_negative_control
  rc=$?
  rm -rf "$WORK"
  exit $rc
fi

case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    rm -rf "$WORK"
    exit 2
    ;;
esac

echo "===================================================================="
echo " V-CMP-006 at L2 — the MCP credential a server declares is the one its"
echo " process actually receives — ctx: $CTX"
echo "===================================================================="

if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable."
  rm -rf "$WORK"
  exit 3
fi

# The tier roster, from two definition sites, compared rather than merged.
TIERS_TREE="$(cd "$REPO_ROOT/agents" 2>/dev/null && for d in */; do
  [ -f "${d}SOUL.md" ] && printf '%s\n' "${d%/}"
done | sort | tr '\n' ' ')"
TIERS_CRD="$(python3 - "$REPO_ROOT" <<'PY'
import pathlib, re, sys

path = pathlib.Path(sys.argv[1]) / "k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agents.yaml"
if not path.exists():
    raise SystemExit("")
m = re.search(r"^(?P<ind>\s+)tier:\s*$(?P<body>(?:\n(?P=ind)\s+.*|\n\s*)*)",
              path.read_text(encoding="utf-8"), re.MULTILINE)
if not m:
    raise SystemExit("")
head = m.group("body").split("type: string", 1)[0]
if "enum:" not in head:
    raise SystemExit("")
print(" ".join(sorted(set(re.findall(r"^\s+-\s+([a-z][a-z0-9-]*)\s*$",
                                     head.split("enum:", 1)[1], re.MULTILINE)))))
PY
)"
TIERS_TREE="$(printf '%s' "$TIERS_TREE" | tr -s ' ' | sed 's/ *$//')"
TIERS_CRD="$(printf '%s' "$TIERS_CRD" | tr -s ' ' | sed 's/ *$//')"
if [ -z "$TIERS_TREE" ] || [ "$TIERS_TREE" != "$TIERS_CRD" ]; then
  echo "FAIL: the tier roster disagrees between its two definition sites."
  echo "  agents/*/SOUL.md: '${TIERS_TREE:-<none>}'"
  echo "  CRD spec.tier enum: '${TIERS_CRD:-<none>}'"
  echo "  A tier the tree ships and the CRD refuses cannot be measured; a tier the CRD accepts and"
  echo "  the tree does not ship would be swept by nothing. Reconcile them before believing a green."
  rm -rf "$WORK"
  exit 1
fi
echo "  tiers under test: $TIERS_TREE"
printf '%s\n' "$TIERS_TREE" >"$OBS/tiers.txt"

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"

# The EXIT trap is installed BEFORE p10, whose p12_assert_exclusive_l2 puts `_l2_lock_exit_handler`
# on EXIT and chains whatever is already there. Installed after, this would silently replace the
# lock's release and leak it to the next acquirer's stale break ([[LSN-066]]).
#
# The Agent CRs go and their Deployments, ConfigMaps, Services and pods go with them by
# ownerReference. The NAMESPACE stays: it is reused every run, which is what makes the suite
# re-runnable without waiting on a terminating namespace, and the ServiceAccounts and Secrets
# `seed_agent_fixtures` created are cheap to leave and re-apply.
cleanup() {
  local a
  for a in $AGENT_NAMES; do
    $K -n "$NS" delete agent "$a" --ignore-not-found --wait=false >/dev/null 2>&1
  done
  rm -rf "$WORK"
}
AGENT_NAMES=""
trap 'cleanup' EXIT

echo "== 0) preconditions =="
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

if ! $K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the Agent CRD is not installed on '$CTX' — nothing would render a ConfigMap."
  echo "  Stand it up: dev/cluster/up.sh"
  exit 3
fi
if ! $K -n kubeagents-system get deploy kubeagents-controller-manager >/dev/null 2>&1; then
  echo "DEFERRED: no controller-manager on '$CTX'; the CRs would sit unreconciled and every arm"
  echo "  below would fail for a reason that has nothing to do with MCP credentials."
  exit 3
fi

p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable (see above). The ConfigMap and the container env are both"
    echo "  operator output, so every claim below would be about unknown code."
    exit 3
    ;;
  *)
    bad "P1: the cluster is not running the build under test"
    exit 1
    ;;
esac

# The agent image. It only has to be PULLABLE and to carry the scripts — the property under test is
# about the operator's rendering and about what the image's own scripts read, so pinning the agent
# image to this commit would be a precondition on the wrong artifact. Discovered the way
# multi-agent-namespace-l2.sh discovers it, newest dirty build first.
PROJECT_ID="${PROJECT_ID:-$(gcloud config get core/project 2>/dev/null)}"
REGION="${REGION:-us-east4}"
AR_REPO="${AR_REPO:-kube-agents}"
AGENT_IMAGE_REPO="${AGENT_IMAGE_REPO:-$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO}"
if [ -z "${AGENT_IMAGE_TAG:-}" ]; then
  _sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  AGENT_IMAGE_TAG="dev-$_sha"
  if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
    _dirty="$(gcloud artifacts docker tags list "$AGENT_IMAGE_REPO/platform-agent" \
      --project "$PROJECT_ID" --format='value(tag)' 2>/dev/null |
      grep "^dev-$_sha-dirty-[0-9]\{1,\}$" | sort -t- -k4,4n | tail -1)"
    [ -n "$_dirty" ] && AGENT_IMAGE_TAG="$_dirty"
  fi
fi
echo "  agent image tag: $AGENT_IMAGE_TAG"

# --- 1) one Agent per tier, force-recreated -----------------------------------------------------
echo "== 1) seeding one Agent per tier in namespace $NS =="
$K create namespace "$NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true

# The parent chain is positional: platform is the root, cluster-admin names it, developer-team names
# the cluster-admin. Derived from the tier ORDER the CRD enum happens to declare would be a guess;
# the relationship is 06 §1.2's and is stated here once, keyed by tier.
agent_name_for() { printf '%s-agent\n' "$1"; }

create_agent() { # <tier> <parent-or-empty>  — prints nothing; the name is agent_name_for <tier>
  local tier="$1" parent="$2" name scope_extra="" spec_extra=""
  name="$(agent_name_for "$tier")"
  case "$tier" in
    cluster-admin) scope_extra="    clusterName: $CLUSTER" ;;
    developer-team) scope_extra="    clusterName: $CLUSTER
    namespace: $NS" ;;
  esac
  [ -n "$parent" ] && spec_extra="  parentRef:
    name: $parent"
  # P3: the ConfigMap and the pod env are reconcile outputs, so a CR left over from an earlier run
  # carries the previous operator's rendering ([[LSN-002]]).
  p3_force_recreate "$K" "$NS" "agent/$name" 60 >/dev/null 2>&1
  $K delete agent "$name" -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1
  cat <<YAML | $K apply -f - >/dev/null || bad "could not create Agent $name"
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $name
  namespace: $NS
spec:
  tier: $tier
  scope:
    projectId: $PROJECT
${scope_extra}
  harness:
    clusterName: $CLUSTER
    location: us-central1
    hermes:
      agentHome: /opt/data
      dashboardEnabled: true
      apiServerSecretRef:
        name: ${name}-secrets
        key: API_SERVER_KEY
  deployment:
    image: ${AGENT_IMAGE_REPO}/${tier}-agent
    tag: ${AGENT_IMAGE_TAG}
    imagePullPolicy: IfNotPresent
  security:
    serviceAccountName: ${tier}-agent
${spec_extra}
YAML
}

for tier in $TIERS_TREE; do
  AGENT_NAMES="$AGENT_NAMES $(agent_name_for "$tier")"
done
# platform first, then whatever names it, then whatever names that. Written as an explicit walk so
# a tier added tomorrow shows up as a root with no parent rather than silently inheriting one, and
# so the ceiling webhook — which refuses a child whose parent it cannot READ (06 §1.2 V-6) — always
# finds the parent already admitted.
parent=""
for tier in platform cluster-admin developer-team; do
  case " $TIERS_TREE " in
    *" $tier "*) : ;;
    *) continue ;;
  esac
  create_agent "$tier" "$parent"
  parent="$(agent_name_for "$tier")"
  echo "  created Agent $parent (tier $tier)"
done

# The fixtures the pods need to START and which nothing in dev creates: the ServiceAccount the CR
# names and the Secret holding its own API key. AFTER the CRs, not before — `seed_agent_fixtures`
# reads both names off the CR itself rather than reconstructing them, so there has to be a CR to
# read (lib/agent-fixtures.sh). A missing SA blocks pod creation and a missing Secret wedges the
# container in CreateContainerConfigError; both clear on the next retry once these exist.
echo "  seeding the fixtures the pods need to start:"
for tier in $TIERS_TREE; do
  seed_agent_fixtures "$K" "$NS" "$(agent_name_for "$tier")" ||
    bad "could not seed the ServiceAccount and API-key Secret for the $tier agent"
done

# --- 2) wait for a pod per agent, resolved by ownership -----------------------------------------
echo "== 2) waiting for one gateway pod per agent =="
PODS=""
for tier in $TIERS_TREE; do
  name="$(agent_name_for "$tier")"
  if ! $K -n "$NS" wait --for=condition=Available "deployment/${name}-gateway" --timeout=300s >/dev/null 2>&1; then
    waiting="$($K -n "$NS" get deploy "${name}-gateway" -o jsonpath='{.status.conditions[*].message}' 2>/dev/null)"
    echo "DEFERRED: ${name}-gateway never became Available. Nothing about MCP credentials can be"
    echo "  measured on a pod that does not run, and calling that a failed security property is"
    echo "  exactly the confusion P10 exists to prevent. Deployment says: ${waiting:-<nothing>}"
    exit 3
  fi
  pod="$(p3_pod_of_deploy "$K" "$NS" "${name}-gateway" 120)"
  if [ -z "$pod" ]; then
    echo "DEFERRED: could not resolve the pod the current ${name}-gateway Deployment owns."
    exit 3
  fi
  PODS="$PODS $tier:$name:$pod"
  echo "  $tier -> $pod"
done

# --- 3) collect, per (pod, container that mounts the rendered config) ---------------------------
echo "== 3) reading each pod's config, scripts and process environments =="

# The in-container collector. One exec per container: the file at the Hermes config path, every
# script the image ships beside it, and — for the processes the KERNEL says are in this container's
# cgroup — the command line and the environment as the process received it.
#
# THE CGROUP COMPARISON IS THE LOAD-BEARING LINE. The pod runs with a shared PID namespace, so
# /proc holds every container's processes. Read naively, the dashboard's MCP servers are
# indistinguishable from the agent's, and a finding in one would be reported against the other.
# `/proc/self/cgroup` is the collector's own, so the test is "same container as me" and no container
# name, id or index appears anywhere ([[LSN-036]]).
#
# base64 with the newlines stripped rather than `base64 -w0`: the flag is GNU-only and the answer
# has to survive whatever userland the agent image happens to ship.
#
# Written to a FILE and fed to `sh -s` on stdin rather than interpolated into an unquoted heredoc.
# Interpolated, `$(dirname "$cfg")` and `$(cat /proc/self/cgroup)` would be expanded HERE, by this
# shell, on this laptop, and the pod would be handed the answers to questions about the wrong
# machine — with `/proc/self/cgroup` silently empty on a Mac, which would make the container filter
# match nothing and every subject look like it had no processes.
COLLECT="$WORK/collect.sh"
cat >"$COLLECT" <<'SH'
cfg="$1"
self="$(cat /proc/self/cgroup 2>/dev/null)"
if [ -r "$cfg" ]; then
  printf 'C\t\t%s\n' "$(base64 <"$cfg" | tr -d '\n')"
fi
for f in "$(dirname "$cfg")"/scripts/*.py; do
  [ -f "$f" ] || continue
  printf 'F\t%s\t%s\n' "$(basename "$f")" "$(base64 <"$f" | tr -d '\n')"
done
for d in /proc/[0-9]*; do
  [ -r "$d/environ" ] || continue
  cg="$(cat "$d/cgroup" 2>/dev/null)"
  [ "$cg" = "$self" ] || continue
  printf 'P\t%s\t%s\n' "${d#/proc/}" "$(base64 <"$d/cmdline" | tr -d '\n')"
  printf 'E\t%s\t%s\n' "${d#/proc/}" "$(base64 <"$d/environ" | tr -d '\n')"
done
SH

# The MCP servers are spawned by Hermes after the container is Ready, so poll for them rather than
# sleeping a guessed interval. `settled` is the first pass on which at least one process in this
# container runs something out of the scripts directory.
SUBJECTS=0
for entry in $PODS; do
  tier="${entry%%:*}"
  rest="${entry#*:}"
  agent="${rest%%:*}"
  pod="${rest##*:}"

  # Which containers mount the operator's rendered ConfigMap, and at what path. Derived from the pod
  # spec: volume -> configMap.name, then every container whose volumeMounts names that volume.
  MOUNTS="$($K -n "$NS" get pod "$pod" -o json 2>/dev/null | python3 -c '
import json, sys

pod = json.load(sys.stdin)
by_volume = {}
for v in pod["spec"].get("volumes") or []:
    cm = v.get("configMap")
    if cm and cm.get("name"):
        by_volume[v["name"]] = cm["name"]
for c in pod["spec"].get("containers") or []:
    for m in c.get("volumeMounts") or []:
        cm = by_volume.get(m.get("name"))
        if not cm or not m.get("subPath"):
            continue
        print("\t".join([c["name"], cm, m["subPath"], m["mountPath"]]))
' 2>/dev/null)"

  if [ -z "$MOUNTS" ]; then
    bad "no container in $pod mounts a ConfigMap key at a file path, so there is no rendered artifact for this agent to judge"
    continue
  fi

  # WHICH OF THOSE MOUNTS IS A HERMES CONFIG is decided by the RENDERED ARTIFACT'S OWN CONTENT: the
  # key that declares an `mcp_servers` mapping. Measured on a live pod, the pod spec offers five
  # ConfigMap-key mounts across three containers — the agent's config.yaml and its SETTINGS.md, the
  # dashboard's config.yaml, and fluent-bit's two .conf files. Taking all five made SETTINGS.md
  # overwrite the agent's own observation (same container, second mount) and made fluent-bit a
  # subject with no scripts and no processes, which the vacuity floor then reported as a failure of
  # V-CMP-006. Both are the same mistake: "a file the operator rendered" is not the subject, and
  # "config.yaml" would be a hardcoded basename ([[LSN-036]]) that the next renamed key breaks.
  #
  # `mcp_servers` is not a heuristic here, it is the row's subject — V-CMP-006 is about the servers
  # in that mapping — and it is read off the ConfigMap rather than off the container, because the
  # ConfigMap is the runtime-authoritative artifact P6 names. If the operator ever stops rendering
  # the mapping, no subject qualifies, and A5's tier-coverage floor fails rather than going quiet.
  qualified=""
  while IFS="$TAB" read -r container cmname subpath mountpath; do
    [ -n "$container" ] || continue
    name="$tier.$agent.$container"
    case " $qualified " in
      *" $container "*) continue ;; # already collected this container's MCP config
    esac
    dir="$OBS/$name"
    mkdir -p "$dir/scripts"
    printf '%s\n' "$tier" >"$dir/tier"
    printf '%s\n' "$container" >"$dir/container"

    # The runtime-authoritative artifact, from the API server. jsonpath escapes the dot in the key.
    $K -n "$NS" get configmap "$cmname" \
      -o "jsonpath={.data.${subpath//./\\.}}" >"$dir/cm-config.yaml" 2>/dev/null
    if ! grep -q '^mcp_servers:' "$dir/cm-config.yaml"; then
      rm -rf "$dir"
      continue
    fi
    qualified="$qualified $container"

    raw=""
    for _ in $(seq 1 30); do
      raw="$($K -n "$NS" exec -i "$pod" -c "$container" -- sh -s "$mountpath" <"$COLLECT" 2>/dev/null)"
      printf '%s\n' "$raw" | grep -q "^P${TAB}" && printf '%s\n' "$raw" | grep -q '/scripts/' && break
      sleep 2
    done
    printf '%s\n' "$raw" >"$dir/raw.txt"

    python3 - "$dir" <<'PY'
import base64
import pathlib
import sys

d = pathlib.Path(sys.argv[1])
procs = []
for line in (d / "raw.txt").read_text(encoding="utf-8", errors="replace").splitlines():
    parts = line.split("\t")
    if len(parts) != 3:
        continue
    kind, key, payload = parts
    try:
        blob = base64.b64decode(payload)
    except Exception:
        continue
    if kind == "C":
        (d / "pod-config.yaml").write_bytes(blob)
    elif kind == "F":
        (d / "scripts" / pathlib.Path(key).name).write_bytes(blob)
    elif kind in ("P", "E"):
        # One record per line, so a newline INSIDE an environment value would otherwise split one
        # process's environment into two unreadable halves. Tabs likewise: the record separator.
        text = blob.decode("utf-8", "replace").rstrip("\x00").replace("\n", " ").replace("\t", " ")
        procs.append("%s\t%s\t%s" % (kind, key, text))
(d / "procs.txt").write_text("\n".join(procs) + ("\n" if procs else ""))
if not (d / "pod-config.yaml").exists():
    (d / "pod-config.yaml").write_text("")
PY
    rm -f "$dir/raw.txt"
    SUBJECTS=$((SUBJECTS + 1))
    echo "  collected $name ($mountpath from ConfigMap $cmname key $subpath)"
  done <<EOT
$MOUNTS
EOT
  if [ -z "$qualified" ]; then
    bad "$pod mounts ConfigMap keys but none of them declares an mcp_servers mapping, so the operator rendered this agent no MCP configuration at all and there is nothing for V-CMP-006 to be about"
  fi
done

if [ "$SUBJECTS" -eq 0 ]; then
  echo "DEFERRED: no (pod, container) pair could be collected, so there is nothing to judge."
  exit 3
fi

# --- 4) judge ------------------------------------------------------------------------------------
echo "== 4) V-CMP-006 =="
TRANSCRIPT="$(python3 "$ANALYZE" live "$OBS" "$REPO_ROOT" 2>&1)"
case "$TRANSCRIPT" in
  *'{'*) : ;;
  *)
    echo "DEFERRED: the analyzer produced no transcript:"
    printf '%s\n' "$TRANSCRIPT" | sed 's/^/  /'
    exit 3
    ;;
esac
FLAT="$(printf '%s\n' "$TRANSCRIPT" | flatten)"
run_arms

echo
# An arm that was moved into a function and never called back is a suite reporting a verdict it did
# not compute (broker-auth-l2.sh paid for that one). The floor is per subject: five arms score at
# least one line each for every (pod, container), and the census scores two more overall.
MIN_ASSERTIONS=$((SUBJECTS * 5 + 2))
if [ "$assertions" -lt "$MIN_ASSERTIONS" ]; then
  echo "FAIL: only $assertions assertion(s) ran over $SUBJECTS subject(s); at least $MIN_ASSERTIONS were expected."
  echo "  A suite that reports a verdict it did not compute is worse than one that fails."
  exit 1
fi

if [ "$fail" -eq 0 ]; then
  echo "RESULT: PROVEN — V-CMP-006 at L2: $assertions assertions over $SUBJECTS (pod, container)"
  echo "  subject(s); every MCP server declares the credentials its shipped script reads, and every"
  echo "  declared credential reached the process Hermes spawned for it."
  exit 0
fi
echo "RESULT: FAILED — V-CMP-006 at L2 ($assertions assertions over $SUBJECTS subject(s))"
exit 1
