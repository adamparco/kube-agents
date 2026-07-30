#!/usr/bin/env python3
"""V-RUN-010 (L0): the broker's supply chain, image, socket count and mount set are minimal.

08 §2.1 puts the broker on "the smallest possible supply chain -- no model, no chat surface, no
plugin loader, no untrusted-input parser beyond the Action Envelope schema", and 08 §2.6 gives it a
read-only root, no shell in the image, and "no volume mounts other than its certificate Secret and
its projected token". Those are four claims about one pod, and the reason they are grouped under one
check is that they defend one thing: this is the only pod in the mesh whose ServiceAccount can
write, so every gram of runtime it carries is reachable by whatever reaches it.

The four claims fail in different places, so this asserts them in different places.

  P1  THE FIRST-PARTY GRAPH. No package this repository owns, reachable from `cmd/broker`, may
      import a shell, a plugin loader, an interpreter, an inference client or a debug HTTP surface.
      This is the half with teeth, because it is the half the repository controls -- and it is the
      half that was already broken. `internal/journal` carried `os/exec` for the Cloud Logging audit
      source, so the broker binary shipped `exec.CommandContext` on an argv whose zeroth element is
      configurable. Nothing constructed one, which is exactly why nothing noticed: an image scan
      sees a distroless base with no `/bin/sh` and reports a shell-free image, and a Go binary's
      shell is `os/exec`, not `/bin/sh`.

  P2  THE MODULE SBOM. No DIRECT require in `k8s-operator/go.mod` names an inference vendor, an
      embedded interpreter or a plugin framework. This is the SBOM half that needs no toolchain: an
      LLM SDK arrives as a module before it arrives as an import, and `go.mod` is in the repository
      while the module graph is not. Direct rather than every line, for the reason given below:
      `// indirect` is the transitive closure of the Kubernetes libraries, which is not a set this
      repository picks. It already contains `github.com/google/cel-go`, an expression interpreter,
      by way of `k8s.io/apiserver`'s admission-policy support.

  P3  `cmd/broker`'s OWN IMPORT BLOCK. Two rules, both about imports whose purpose is a side effect.
      No blank import at all: `_ "k8s.io/client-go/plugin/pkg/client/auth"` was in this file because
      kubebuilder scaffolds it into every command, and it is a plugin loader -- it registers
      out-of-process credential providers so a kubeconfig may name a binary for the client to fork.
      And no `sigs.k8s.io/controller-runtime` root alias, which is a facade over `pkg/manager` and
      therefore over `net/http/pprof`, whose init registers /debug/pprof on http.DefaultServeMux.
      Neither was reachable. Both were linked in, and "unreachable" is a property of today's call
      graph rather than of the binary.

  P4  THE IMAGE. `Dockerfile.broker`'s runtime stage is built from a base with no shell, adds
      nothing but the binary, runs no command, and starts the binary in exec form. Shell-form
      `ENTRYPOINT` is the quiet one: `ENTRYPOINT /broker` compiles to `/bin/sh -c /broker`, so it
      does not merely permit a shell, it requires one, and on a distroless base it fails at start
      rather than at review.

  P5  ONE LISTENING SOCKET. Exactly one listener is constructed across the reachable first-party
      packages, its handler is not `http.DefaultServeMux`, and the rendered Deployment and Service
      each expose exactly one port, agreeing on `broker.Port`. V-BRK-021's route inventory is an
      inventory of one surface; a second listener makes it an inventory of some of the surface.

  P6  THE MOUNT SET. The broker container's mounts and the pod's volumes are a closed set, the root
      filesystem is read-only, and `automountServiceAccountToken` is never disabled -- the last
      because 08 §2.6's sentence has a lower bound as well as an upper one, and a broker with no
      projected token cannot journal, which 06 §2.2.1 says does not fail safe.

WHAT THIS CHECK CANNOT SEE, STATED PLAINLY. Two residues live in the third-party graph and no import
discipline in this repository removes either. `k8s.io/client-go/rest` imports
`plugin/pkg/client/auth/exec` unconditionally, so `os/exec` is in the broker binary by way of having
a Kubernetes client at all; and `k8s.io/apiserver` brings `github.com/google/cel-go`, an expression
interpreter, for the admission-policy machinery. Neither is waved away here, both are moved: they
are properties of the module graph, computing them needs a Go toolchain and a warm module cache, and
the L0 chain deliberately has neither (see the `.github/workflows/l0-checks.yml` comment on
installing no dependencies). What P1 buys is that no first-party package REACHES either one -- a
`cel-go` import in `internal/broker` would fail P1 on the line it was written. What P2 buys is that
the residue is never joined by a deliberate choice. What P3 buys is that it is never widened by a
side effect. The residue itself belongs to an L2 SBOM scan of the built image, which is where the
module graph actually exists.

Exemptions are NAMED AND REASONED in the tables below, never a `# noqa`, for the reason
`scope-label-single-sourced.py` gives: a check with no legitimate way to say "this one is fine" gets
weakened by the first person who needs one.

The inference-vendor vocabulary is NOT restated here. It is loaded from
`dev/tests/classifier-is-model-free.py`, which is its one definition site ([[LSN-040]], [[LSN-041]]);
two hand-maintained lists of "what an LLM SDK is called" would agree on the day they were written
and diverge on the day a vendor is added to one of them.

Self-test (the `¬` of 09 §6): `--negative-control` applies each of eleven plausible regressions to a
copy of the inputs in memory and confirms this check reports every one, each by the property it
targets.

Run:  python3 dev/tests/broker-supply-chain-minimal.py
      python3 dev/tests/broker-supply-chain-minimal.py --negative-control
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from golex import strip_go_comments  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
OPERATOR = REPO / "k8s-operator"
MODULE_PATH = "github.com/gke-labs/kube-agents/k8s-operator"
ENTRY_PKG = "cmd/broker"
GO_MOD = OPERATOR / "go.mod"
DOCKERFILE = OPERATOR / "Dockerfile.broker"
MANIFESTS = OPERATOR / "internal" / "controller" / "broker_manifests.go"

# --- P1 -------------------------------------------------------------------------------------
#
# Forbidden imports, by exact path, for any first-party package the broker binary reaches. Each
# value is the sentence the failure prints, because "not allowed" tells a reader nothing about
# whether the rule or the code is wrong.
FORBIDDEN_IMPORTS: dict[str, str] = {
    "os/exec": (
        "a Go binary's shell. 08 §2.6 takes /bin/sh out of the IMAGE; this takes the ability to "
        "fork one out of the PROCESS, which is the half an image scan cannot see"
    ),
    "net/http/cgi": "runs an external program per request -- os/exec wearing a protocol",
    "plugin": "loads code at runtime, which is the one thing a minimal supply chain rules out",
    "net/http/pprof": (
        "registers /debug/pprof on http.DefaultServeMux at init. A second door beside the write "
        "credential, whose existence does not depend on anyone routing to it"
    ),
    "runtime/pprof": "the profiling surface; it arrives with net/http/pprof and leaves with it",
    "expvar": "registers /debug/vars on http.DefaultServeMux at init, for the same reason",
    "net/rpc": "a second protocol surface, and one with no authentication story here",
}

# Import-path substrings that name an embedded interpreter or an extension host. Substrings rather
# than exact paths because the module path is the part that varies (`github.com/traefik/yaegi`,
# `go.starlark.net`), and the interesting property is the engine, not the fork.
INTERPRETER_MARKERS: tuple[str, ...] = (
    "yaegi",
    "goja",
    "otto",
    "starlark",
    "lua",
    "tengo",
    "wasmtime",
    "wasmer",
    "wazero",
    "cel-go",
    "antonmedv/expr",
    "expr-lang",
    "hashicorp/go-plugin",
)

# Import paths that are a plugin loader by construction. `client-go/plugin/...` is the credential
# plugin machinery: its whole purpose is to let a kubeconfig name a binary the client will fork.
PLUGIN_PATH_PREFIXES: tuple[str, ...] = ("k8s.io/client-go/plugin/",)

# First-party packages allowed to break P1 anyway. Empty, and it should stay that way: a first-party
# package with a legitimate need for a subprocess belongs in a package the broker does not reach,
# which is what `internal/journal/cloudaudit` is.
EXEMPT_FIRST_PARTY: dict[str, str] = {}

# Non-vacuity floor. The walker resolves first-party imports by directory; a rename that breaks the
# resolution turns the reachable set into `{cmd/broker}` and every property above into a statement
# about one file ([[LSN-035]]).
MIN_REACHABLE_PACKAGES = 15
MUST_REACH = ("internal/broker", "internal/journal", "internal/broker/pipeline")

# --- P3 -------------------------------------------------------------------------------------
#
# Blank imports in cmd/broker that are allowed anyway. A blank import is never about the symbols; it
# is about what the package's `init` does to global state, so each one needs an argument.
EXEMPT_BLANK_IMPORTS: dict[str, str] = {}

CTRL_ROOT_ALIAS = "sigs.k8s.io/controller-runtime"

# --- P4 -------------------------------------------------------------------------------------
#
# Runtime images with no shell, no package manager and no busybox, matched on the REPOSITORY exactly
# -- never as a prefix. The distinction is the whole rule: `gcr.io/distroless/static:debug` is the
# same repository with a busybox shell at /busybox/sh, and it is precisely the tag someone reaches
# for at 3am while trying to get a prompt inside the one pod that can write. A `startswith` over the
# repository-and-tag string accepts it, which is what the first draft of this check did until its
# own negative control said otherwise.
SHELL_FREE_REPOS: tuple[str, ...] = (
    "gcr.io/distroless/static",
    "gcr.io/distroless/base-nossl",
    "scratch",
)
SHELLED_TAG_MARKERS: tuple[str, ...] = ("debug", "busybox", "shell")

# --- P5 -------------------------------------------------------------------------------------
LISTENER_RE = re.compile(
    r"\b(?:net\.Listen|net\.ListenTCP|tls\.Listen|http\.ListenAndServe|http\.ListenAndServeTLS)\(|"
    r"\.ListenAndServe\(|\.ListenAndServeTLS\(|\bhttp\.Serve\("
)
DEFAULT_MUX_RE = re.compile(r"Handler:\s*(?:nil|http\.DefaultServeMux)\b")

# --- P6 -------------------------------------------------------------------------------------
#
# The closed mount inventory of the broker container, as `(volume name constant, why)`.
ALLOWED_MOUNTS: dict[str, str] = {
    "brokerTLSVolumeName": "the certificate Secret 08 §2.6 names, mounted read-only at 0400",
    # The one deviation from 08 §2.6's literal wording, and it is here rather than in the spec
    # because this unit does not get to edit the sentence its own check is failing (PROTOCOL §10).
    # An anonymous EmptyDir carries no data in, survives no restart and is not a channel from
    # anywhere -- it is the writable scratch a read-only root has to have somewhere. It is pinned to
    # BE an EmptyDir below, which is the part that matters: the same mount name backed by a Secret,
    # a ConfigMap or a hostPath would be a real third mount wearing an approved label.
    "brokerTmpVolumeName": (
        "an anonymous EmptyDir at /tmp -- the writable scratch a read-only root filesystem needs. "
        "Filed for P9-T9b-4: 08 §2.6 says two mounts and the renderer makes three, and whether the "
        "third is removable has not been tested against a running broker"
    ),
}
EMPTYDIR_VOLUMES = ("brokerTmpVolumeName",)
SECRET_VOLUMES = ("brokerTLSVolumeName",)

IMPORT_BLOCK_RE = re.compile(r"^import \(\s*$(.*?)^\)\s*$", re.S | re.M)
IMPORT_LINE_RE = re.compile(r'^\s*(_|\.|[\w]+)?\s*"([^"]+)"', re.M)
IMPORT_SINGLE_RE = re.compile(r'^import\s+(_|\.|[\w]+)?\s*"([^"]+)"', re.M)


def inference_markers() -> tuple[str, ...]:
    """The one definition site of "what an inference client is called" ([[LSN-040]]).

    Loaded from the sibling check rather than copied. The module name has hyphens in it, so it
    cannot be `import`ed by name; that is a spelling problem, not a reason to keep a second list.
    Loading it also means a failure here if that file is renamed, which is the correct outcome --
    silently falling back to a local copy is how the two lists would drift.
    """
    src = pathlib.Path(__file__).resolve().parent / "classifier-is-model-free.py"
    spec = importlib.util.spec_from_file_location("classifier_is_model_free", src)
    if spec is None or spec.loader is None:  # pragma: no cover -- unreachable while the file exists
        raise SystemExit(f"FAIL: cannot load the inference-marker vocabulary from {src}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    markers = tuple(mod.INFERENCE_MARKERS)
    if len(markers) < 5:
        raise SystemExit(
            f"FAIL: {src.name} defines {len(markers)} inference markers; the vocabulary this check "
            "shares with it has been gutted, and P1/P2 would pass on an SDK it no longer names"
        )
    return markers


def imports_of(text: str) -> list[tuple[str, str]]:
    """(alias-or-empty, path) for every import in a Go source file, comments already stripped."""
    found: list[tuple[str, str]] = []
    for block in IMPORT_BLOCK_RE.findall(text):
        for alias, path in IMPORT_LINE_RE.findall(block):
            found.append((alias, path))
    for alias, path in IMPORT_SINGLE_RE.findall(text):
        found.append((alias, path))
    return found


def package_sources(sources: dict[str, str], pkg: str) -> dict[str, str]:
    """Non-test .go files directly in `pkg` (a repo-relative dir under k8s-operator/)."""
    prefix = f"k8s-operator/{pkg}/"
    return {
        rel: text
        for rel, text in sources.items()
        if rel.startswith(prefix)
        and rel.endswith(".go")
        and not rel.endswith("_test.go")
        and "/" not in rel[len(prefix) :]
    }


def reachable(sources: dict[str, str]) -> tuple[dict[str, list[tuple[str, str, str]]], list[str]]:
    """Walk first-party imports from cmd/broker.

    Returns (package -> [(file, alias, import path)], ordered package list). Every import of every
    reachable package is recorded, third-party and stdlib included: the forbidden set is mostly
    stdlib, and the plugin loader that motivated P3 was third-party.
    """
    seen: dict[str, list[tuple[str, str, str]]] = {}
    order: list[str] = []
    queue = [ENTRY_PKG]
    while queue:
        pkg = queue.pop(0)
        if pkg in seen:
            continue
        files = package_sources(sources, pkg)
        edges: list[tuple[str, str, str]] = []
        for rel, text in sorted(files.items()):
            for alias, path in imports_of(strip_go_comments(text)):
                edges.append((rel, alias, path))
                if path.startswith(MODULE_PATH + "/"):
                    child = path[len(MODULE_PATH) + 1 :]
                    if child not in seen:
                        queue.append(child)
        seen[pkg] = edges
        order.append(pkg)
    return seen, order


def check_graph(sources: dict[str, str], markers: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    graph, order = reachable(sources)

    if len(order) < MIN_REACHABLE_PACKAGES:
        failures.append(
            f"VACUOUS: the import walk from {ENTRY_PKG} reached {len(order)} first-party packages, "
            f"fewer than the floor of {MIN_REACHABLE_PACKAGES}. Either the module path changed or "
            "first-party imports stopped resolving to directories, and P1 is now a statement about "
            "almost nothing"
        )
    for must in MUST_REACH:
        if must not in graph:
            failures.append(
                f"VACUOUS: the import walk did not reach {must}, which the broker links. The walk "
                "has stopped descending and P1 is scanning a subset of the binary"
            )

    for pkg in order:
        if pkg in EXEMPT_FIRST_PARTY:
            continue
        for rel, alias, path in graph[pkg]:
            why = FORBIDDEN_IMPORTS.get(path)
            if why is None and any(path.startswith(p) for p in PLUGIN_PATH_PREFIXES):
                why = (
                    "credential-plugin machinery: it exists so a kubeconfig can name a binary the "
                    "client will fork. 08 §2.1 rules out a plugin loader by name"
                )
            if why is None:
                lowered = path.lower()
                hit = next((m for m in INTERPRETER_MARKERS if m in lowered), None)
                if hit:
                    why = f"an embedded interpreter or extension host ({hit!r} in the path)"
            if why is None:
                lowered = path.lower()
                hit = next((m for m in markers if m in lowered), None)
                if hit:
                    why = f"an inference client ({hit!r} in the path)"
            if why:
                blank = " (blank import)" if alias == "_" else ""
                failures.append(
                    f"{rel} imports {path!r}{blank}, and {rel.split('/', 1)[1].rsplit('/', 1)[0]} "
                    f"is reachable from {ENTRY_PKG}: {why}"
                )

    # P3. cmd/broker's own block, where the two historical regressions both lived.
    for rel, alias, path in graph.get(ENTRY_PKG, []):
        if alias == "_" and path not in EXEMPT_BLANK_IMPORTS:
            failures.append(
                f"{rel} has a blank import of {path!r}. A blank import in the broker's main package "
                "is a registration side effect -- a credential plugin, a metrics exporter, a debug "
                "handler -- and 08 §2.1 does not let this process carry one it does not use. If it "
                "is genuinely needed, add it to EXEMPT_BLANK_IMPORTS with the argument"
            )
        if path == CTRL_ROOT_ALIAS:
            failures.append(
                f"{rel} imports {CTRL_ROOT_ALIAS!r}, the controller-runtime root alias. It is a "
                "facade over pkg/manager, pkg/builder and pkg/controller -- a whole controller "
                "runtime in a process that runs no controller -- and pkg/manager pulls "
                "net/http/pprof, whose init registers /debug/pprof on http.DefaultServeMux. Use "
                "pkg/log, pkg/log/zap, pkg/client/config and pkg/manager/signals"
            )

    # P5's source half.
    listeners: list[str] = []
    for pkg in order:
        for rel in sorted(package_sources(sources, pkg)):
            text = strip_go_comments(sources[rel])
            for match in LISTENER_RE.finditer(text):
                line = text[: match.start()].count("\n") + 1
                listeners.append(f"{rel}:{line}")
    if len(listeners) != 1:
        failures.append(
            f"the broker links {len(listeners)} listener constructions, not exactly one: "
            f"{', '.join(listeners) or '(none)'}. V-BRK-021 inventories the routes of ONE surface, "
            "and 08 §2.3 makes the envelope endpoint the only way in; a metrics port, a debug "
            "socket or a second Service is a door the inventory does not describe"
        )
    entry = "\n".join(strip_go_comments(t) for t in sorted(package_sources(sources, ENTRY_PKG).values()))
    if DEFAULT_MUX_RE.search(entry):
        failures.append(
            f"{ENTRY_PKG} serves http.DefaultServeMux (an explicit nil or DefaultServeMux Handler). "
            "That is the mux every third-party init registers onto, so the surface stops being the "
            "routes this repository wrote and becomes the routes its dependencies did"
        )
    return failures


def check_go_mod(text: str, markers: tuple[str, ...]) -> list[str]:
    """P2. The SBOM half that needs no toolchain."""
    failures: list[str] = []
    modules = [
        mod
        for mod, tail in re.findall(r"^\s*([\w./~-]+\.[\w./~-]+)\s+v[\w.+-]+(.*)$", text, re.M)
        if "indirect" not in tail
    ]
    if len(modules) < 10:
        failures.append(
            f"VACUOUS: {GO_MOD.name} parsed to {len(modules)} direct requires. The require-block "
            "grammar has moved and P2 is scanning an empty SBOM"
        )
    for mod in sorted(set(modules)):
        lowered = mod.lower()
        hit = next((m for m in markers if m in lowered), None)
        kind = "an inference client"
        if hit is None:
            hit = next((m for m in INTERPRETER_MARKERS if m in lowered), None)
            kind = "an embedded interpreter or extension host"
        if hit:
            failures.append(
                f"go.mod directly requires {mod!r}: {kind} ({hit!r}). 08 §2.1 keeps the broker's "
                "supply chain free of one, and the operator's module graph is the broker's module "
                "graph -- they are one module. If this is for the operator alone, it still ships in "
                "the broker image the moment anything the broker reaches imports it"
            )
    return failures


def balanced(text: str, marker: str) -> str:
    """The contents of the brace-delimited literal that `marker` opens, matched by depth.

    Not a regex. The first draft of this function terminated on `\\n\\t+\\},\\n\\t+\\}` and matched the
    end of the nested SecretVolumeSource instead of the end of the Volumes slice, so P6 read the
    first volume and reported the second one absent -- a check that would have gone on passing if
    the two volumes had been declared the other way round. Nested Go composite literals are not a
    regular language and the correct tool for them is a counter.
    """
    start = text.find(marker)
    if start < 0:
        return ""
    i = start + len(marker) - 1  # marker ends with the opening brace
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    return ""


def runtime_stage(text: str) -> list[str]:
    """The lines of the LAST FROM stage, which is what the image actually is."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip().upper().startswith("FROM ")]
    if not starts:
        return []
    return lines[starts[-1] :]


def check_dockerfile(text: str) -> list[str]:
    """P4. No shell in the image, and nothing in the runtime stage that could put one back."""
    failures: list[str] = []
    stage = runtime_stage(text)
    if not stage:
        return [f"VACUOUS: {DOCKERFILE.name} has no FROM line; P4 is reading something else"]
    if len([ln for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]) < 2:
        failures.append(
            f"VACUOUS: {DOCKERFILE.name} has one stage. A single-stage image is the toolchain image, "
            "which has a shell by construction, and the multi-stage split is what P4 assumes"
        )

    base = stage[0].split()[1]
    ref = base.split("@", 1)[0]
    repo, _, tag = ref.partition(":")
    if repo not in SHELL_FREE_REPOS:
        failures.append(
            f"{DOCKERFILE.name} runtime stage is FROM {base!r}, whose repository {repo!r} is not one "
            f"of the shell-free images {SHELL_FREE_REPOS}. 08 §2.6: `kubectl exec` into the one pod "
            "that can write must fail at exec rather than land on a prompt"
        )
    hit = next((m for m in SHELLED_TAG_MARKERS if m in tag.lower()), None)
    if hit:
        failures.append(
            f"{DOCKERFILE.name} runtime stage is FROM {base!r}: the tag says {hit!r}. The debug "
            "variants of these images are the same base plus a busybox shell at /busybox/sh, so the "
            "repository is right and the image still has a shell. 08 §2.6 is about the image that "
            "runs, and a tag is the easiest part of it to change under pressure"
        )

    copies = 0
    for raw in stage[1:]:
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        verb = ln.split()[0].upper()
        if verb in ("RUN", "SHELL", "HEALTHCHECK", "ADD", "ONBUILD"):
            failures.append(
                f"{DOCKERFILE.name} runtime stage has a {verb} instruction: {ln!r}. RUN and SHELL "
                "need an interpreter the base does not have; HEALTHCHECK's shell form runs one per "
                "probe; ADD fetches and unpacks. The liveness probe is a TCP dial for this reason"
            )
        if verb == "COPY":
            copies += 1
            if "--from=" not in ln:
                failures.append(
                    f"{DOCKERFILE.name} runtime stage COPYs from the build CONTEXT, not from a "
                    f"stage: {ln!r}. Anything in the context can land in the image that way"
                )
        if verb in ("ENTRYPOINT", "CMD"):
            arg = ln[len(verb) :].strip()
            if not arg.startswith("["):
                failures.append(
                    f"{DOCKERFILE.name} runtime stage has a shell-form {verb}: {ln!r}. Shell form "
                    "is rewritten to `/bin/sh -c ...`, so it does not merely permit a shell, it "
                    "requires one. Use the JSON exec form"
                )
    if copies != 1:
        failures.append(
            f"{DOCKERFILE.name} runtime stage has {copies} COPY instructions, not exactly one. The "
            "image is one static binary; a second COPY is a config file, a CA bundle or a script, "
            "and each is a thing the process can be pointed at"
        )
    if not any(ln.strip().upper().startswith("USER ") for ln in stage[1:]):
        failures.append(f"{DOCKERFILE.name} runtime stage sets no USER; 08 §2.6 requires non-root")
    return failures


def function_body(text: str, name: str) -> str:
    """The source of a top-level Go function, from its `func` line to the closing brace in column 0."""
    start = text.find(f"\nfunc {name}(")
    if start < 0:
        return ""
    end = text.find("\n}\n", start)
    return text[start : end + 3] if end > 0 else text[start:]


def check_manifests(text: str) -> list[str]:
    """P5's manifest half and P6."""
    failures: list[str] = []
    stripped = strip_go_comments(text)
    dep = function_body(stripped, "buildBrokerDeployment")
    svc = function_body(stripped, "buildBrokerService")
    if not dep or not svc:
        return [
            f"VACUOUS: {MANIFESTS.name} has no buildBrokerDeployment and/or buildBrokerService; "
            "P5's manifest half and P6 are reading nothing"
        ]

    mounts = balanced(dep, "VolumeMounts: []corev1.VolumeMount{")
    if not mounts.strip():
        failures.append("VACUOUS: no VolumeMounts literal found in buildBrokerDeployment")
    else:
        named = re.findall(r"\{Name:\s*([A-Za-z][\w.]*)", mounts)
        unknown = [n for n in named if n not in ALLOWED_MOUNTS]
        if unknown:
            failures.append(
                f"the broker container mounts {unknown}, which is outside the closed set "
                f"{sorted(ALLOWED_MOUNTS)}. 08 §2.6: no volume mounts other than the certificate "
                "Secret and the projected token. A new mount is a new channel into the one pod that "
                "can write -- add it to ALLOWED_MOUNTS with the argument, or do not mount it"
            )
        for want in ALLOWED_MOUNTS:
            if want not in named:
                failures.append(
                    f"the broker container no longer mounts {want}. That is the lower bound of "
                    "08 §2.6's sentence and it matters as much as the upper one: without the "
                    "certificate Secret there is no mTLS, and the failure is a crash loop"
                )

    body = balanced(dep, "Volumes: []corev1.Volume{")
    if not body.strip():
        failures.append("VACUOUS: no Volumes literal found in buildBrokerDeployment")
    # `(?<![A-Za-z])` so this reads `Name:` and not the tail of `SecretName:`.
    declared = re.findall(r"(?<![A-Za-z])Name:\s*([A-Za-z][\w.]*),", body)
    unknown_vols = [n for n in declared if n not in ALLOWED_MOUNTS]
    if unknown_vols:
        failures.append(
            f"the broker pod declares volumes {unknown_vols}, which is outside the closed set "
            f"{sorted(ALLOWED_MOUNTS)}. A volume with no mount is still a projection the kubelet "
            "performs into the pod, and it is one VolumeMounts line from being readable"
        )
    for name in EMPTYDIR_VOLUMES:
        pattern = re.compile(rf"Name:\s*{name},?\s*\n?\s*VolumeSource:\s*corev1\.VolumeSource\{{EmptyDir:")
        if not pattern.search(body):
            failures.append(
                f"volume {name} is no longer an EmptyDir. It is exempted from 08 §2.6's two-mount "
                "rule only BECAUSE it is anonymous scratch that carries nothing in; the same mount "
                "name backed by a Secret, a ConfigMap or a hostPath is a third real mount wearing "
                "an approved label"
            )
    for name in SECRET_VOLUMES:
        if not re.search(rf"Name:\s*{name},?\s*\n\s*VolumeSource:\s*corev1\.VolumeSource\{{\s*\n\s*Secret:", body):
            failures.append(f"volume {name} is no longer sourced from a Secret")
    if not re.search(r"DefaultMode:\s*ptr\.To\(int32\(0400\)\)", body):
        failures.append(
            "the broker's certificate Secret is no longer mounted 0400. The private half of the "
            "mesh identity is in it, and the pod runs as a numeric UID with no user database"
        )

    if not re.search(r"ReadOnlyRootFilesystem:\s*ptr\.To\(true\)", dep):
        failures.append(
            "the broker container no longer sets ReadOnlyRootFilesystem: true (08 §2.6). Without it "
            "the /tmp exemption above stops being the only writable path and becomes one of many"
        )
    if re.search(r"AutomountServiceAccountToken:\s*ptr\.To\(false\)", dep):
        failures.append(
            "the broker pod disables automountServiceAccountToken. 08 §2.6's mount set INCLUDES the "
            "projected token; without it the broker cannot authenticate to the API server and "
            "cannot journal, and 06 §2.2.1 is explicit that losing the journal grant does not fail "
            "safe -- it bricks the tier"
        )

    ports = re.findall(r"ContainerPort:\s*([\w.]+)", dep)
    if ports != ["broker.Port"]:
        failures.append(
            f"the broker container declares ports {ports}, not exactly ['broker.Port']. One "
            "listening socket means one declared port; a second is the metrics or debug listener "
            "P5 rules out, arriving through the manifest instead of through the code"
        )
    svc_ports = re.findall(r"Port:\s*([\w.]+),", svc)
    if svc_ports != ["broker.Port"]:
        failures.append(
            f"the broker Service exposes ports {svc_ports}, not exactly ['broker.Port']. 08 §2.1 "
            "fixes the broker Service at one port named `envelope`"
        )
    return failures


def check(sources: dict[str, str], markers: tuple[str, ...]) -> list[str]:
    failures = check_graph(sources, markers)
    for path, fn in ((GO_MOD, check_go_mod), (DOCKERFILE, check_dockerfile), (MANIFESTS, check_manifests)):
        rel = str(path.relative_to(REPO))
        text = sources.get(rel)
        if text is None:
            failures.append(f"VACUOUS: {rel} is not in the corpus; the property it carries went unchecked")
            continue
        failures.extend(fn(text, markers) if fn is check_go_mod else fn(text))
    return failures


def read_sources() -> dict[str, str]:
    """Every non-ignored file this check reads, keyed repo-relative.

    Read off disk rather than out of the index so the check is usable on the working tree that
    introduces it ([[LSN-050]] is about the opposite mistake; the corpus here is a fixed, named set
    of paths plus two source trees, so there is nothing for `git ls-files` to discover).
    """
    sources: dict[str, str] = {}
    for path in (GO_MOD, DOCKERFILE, MANIFESTS):
        if path.is_file():
            sources[str(path.relative_to(REPO))] = path.read_text()
    for path in OPERATOR.rglob("*.go"):
        rel = str(path.relative_to(REPO))
        if "/bin/" in rel or "/testdata/" in rel:
            continue
        try:
            sources[rel] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return sources


def negative_control() -> int:
    """Break each property in memory and confirm this check notices, by name."""
    markers = inference_markers()
    sources = read_sources()
    main_go = "k8s-operator/cmd/broker/main.go"
    journal_go = "k8s-operator/internal/journal/store.go"
    docker = str(DOCKERFILE.relative_to(REPO))
    manifests = str(MANIFESTS.relative_to(REPO))
    gomod = str(GO_MOD.relative_to(REPO))

    def edit(name: str, old: str, new: str):
        return lambda s: {**s, name: s[name].replace(old, new, 1)}

    # (label, mutate, signal). The signal names the PROPERTY, never merely "something failed":
    # several of these read the same file, and a property that had stopped executing would go on
    # reporting green under a non-emptiness assertion ([[LSN-035]]).
    mutations = [
        (
            "a first-party package the broker links regains os/exec",
            edit(journal_go, '\t"fmt"\n', '\t"fmt"\n\t"os/exec"\n'),
            "imports 'os/exec'",
        ),
        (
            "a reachable package imports an inference client",
            edit(journal_go, '\t"fmt"\n', '\t"fmt"\n\t"github.com/anthropics/anthropic-sdk-go"\n'),
            "an inference client",
        ),
        (
            "a reachable package embeds an interpreter",
            edit(journal_go, '\t"fmt"\n', '\t"fmt"\n\t"go.starlark.net/starlark"\n'),
            "an embedded interpreter",
        ),
        (
            "the credential-plugin blank import comes back",
            edit(main_go, '\t"k8s.io/apimachinery/pkg/runtime"', '\t_ "k8s.io/client-go/plugin/pkg/client/auth"\n\t"k8s.io/apimachinery/pkg/runtime"'),
            "credential-plugin machinery",
        ),
        (
            "the controller-runtime root alias comes back",
            edit(main_go, '\t"sigs.k8s.io/controller-runtime/pkg/client"', '\tctrl "sigs.k8s.io/controller-runtime"\n\t"sigs.k8s.io/controller-runtime/pkg/client"'),
            "the controller-runtime root alias",
        ),
        (
            "a second listener appears",
            edit(main_go, "\thttpServer := &http.Server{", '\tgo func() { _ = http.ListenAndServe(":9090", nil) }()\n\thttpServer := &http.Server{'),
            "listener constructions, not exactly one",
        ),
        (
            "an LLM SDK is added to go.mod",
            edit(gomod, "require (", "require (\n\tgithub.com/anthropics/anthropic-sdk-go v1.0.0"),
            "an inference client",
        ),
        (
            "the runtime base keeps its repository and gains a busybox shell via the tag",
            edit(docker, "FROM gcr.io/distroless/static:nonroot", "FROM gcr.io/distroless/static:debug"),
            "the tag says 'debug'",
        ),
        (
            "the runtime base becomes a distro image",
            edit(docker, "FROM gcr.io/distroless/static:nonroot", "FROM alpine:3.20"),
            "is not one of the shell-free images",
        ),
        (
            "the entrypoint becomes shell form",
            edit(docker, 'ENTRYPOINT ["/broker"]', "ENTRYPOINT /broker"),
            "shell-form ENTRYPOINT",
        ),
        (
            "the broker gains a third mount",
            edit(
                manifests,
                "{Name: brokerTmpVolumeName, MountPath: \"/tmp\"},",
                "{Name: brokerTmpVolumeName, MountPath: \"/tmp\"},\n\t\t\t\t\t\t\t\t{Name: brokerConfigVolumeName, MountPath: \"/etc/kage/config\"},",
            ),
            "outside the closed set",
        ),
        (
            "the /tmp scratch becomes a hostPath",
            edit(
                manifests,
                "VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},",
                'VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: "/tmp"}},',
            ),
            "no longer an EmptyDir",
        ),
        (
            "the read-only root filesystem is relaxed",
            edit(
                manifests,
                "ReadOnlyRootFilesystem:   ptr.To(true),\n\t\t\t\t\t\t\tRunAsNonRoot",
                "ReadOnlyRootFilesystem:   ptr.To(false),\n\t\t\t\t\t\t\tRunAsNonRoot",
            ),
            "ReadOnlyRootFilesystem",
        ),
        (
            "the broker container declares a metrics port",
            edit(
                manifests,
                "\t\t\t\t\t\t\tContainerPort: broker.Port,\n\t\t\t\t\t\t\tProtocol:      corev1.ProtocolTCP,\n\t\t\t\t\t\t}},",
                "\t\t\t\t\t\t\tContainerPort: broker.Port,\n\t\t\t\t\t\t\tProtocol:      corev1.ProtocolTCP,\n\t\t\t\t\t\t}, {\n\t\t\t\t\t\t\tName:          \"metrics\",\n\t\t\t\t\t\t\tContainerPort: 9090,\n\t\t\t\t\t\t\tProtocol:      corev1.ProtocolTCP,\n\t\t\t\t\t\t}},",
            ),
            "not exactly ['broker.Port']",
        ),
        (
            "a volume is declared but not mounted",
            edit(
                manifests,
                "\t\t\t\t\t\t{\n\t\t\t\t\t\t\tName:         brokerTmpVolumeName,",
                "\t\t\t\t\t\t{\n\t\t\t\t\t\t\tName:         brokerCredsVolumeName,\n\t\t\t\t\t\t\tVolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: \"creds\"}},\n\t\t\t\t\t\t},\n\t\t\t\t\t\t{\n\t\t\t\t\t\t\tName:         brokerTmpVolumeName,",
            ),
            "declares volumes",
        ),
    ]

    clean = check(sources, markers)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors: list[str] = []
    for label, mutate, signal in mutations:
        mutated = mutate(sources)
        if mutated == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        found = check(mutated, markers)
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:140]}...)"
            )

    if survivors:
        print("FAIL: the check did not notice these regressions:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(
        f"PASS: negative control -- all {len(mutations)} injected regressions were caught, each by "
        "the property it targets"
    )
    return 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()

    markers = inference_markers()
    sources = read_sources()
    failures = check(sources, markers)
    if failures:
        print("FAIL: V-RUN-010 -- broker supply-chain minimality", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    _, order = reachable(sources)
    print(
        f"PASS: V-RUN-010 -- {len(order)} first-party packages reachable from {ENTRY_PKG}, none "
        f"importing a shell, plugin loader, interpreter, inference client or debug surface; "
        f"go.mod names no inference or interpreter module; the runtime stage is shell-free with one "
        f"COPY and an exec-form entrypoint; one listener, one container port, one Service port; and "
        f"the mount set is the closed {len(ALLOWED_MOUNTS)} on a read-only root"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
