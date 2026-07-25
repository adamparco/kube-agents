#!/usr/bin/env python3
"""P8-T7 — the documentation says only true things, and stays that way.

WHY A LINT AND NOT A CLEANUP. P8-T7 was scoped as "remove the retired `PlatformAgent`
kind from INSTALL.md". A one-time edit fixes the five lines the finding happened to
name and does nothing about the twenty-nine it did not, nor about the next rename.
Worse, three of the falsehoods in this tree were *created by earlier units of this very
phase*: P8-T1 deleted the allow-all escape hatch and left two documents still telling
readers an empty allowlist admits everyone, and P8-T3 shipped provisioning step 13 with
no `make` target for it while the docs list stops at 11. Documentation drifts on exactly
the schedule that code changes, so the check has to run on the same schedule.

WHAT "TRUE" MEANS HERE, mechanically. Five of the six checks derive their expectation
from an artifact the build already maintains, so they cannot go stale:

  1. kinds       <- the shipped CRDs (`config/crd/bases/*.yaml`)
  2. resources   <- the same CRDs' plural/singular names
  3. file paths  <- the filesystem
  4. steps       <- `scripts/provision_*.sh` + the Makefile targets
  5. MCP surface <- the golden RENDERED ConfigMap, which is what the pod actually
                    mounts (precondition P6: the image-baked `config.yaml` is shadowed
                    by the operator's render, so the render is the truth)

The sixth is a curated list of retired identifiers, and it is labelled curated rather
than dressed up as derived. "This string used to mean something and now means nothing"
is not recoverable from the tree — the whole point is that the thing is gone. It is kept
honest by an allowlist that must be exact: an entry that stops matching FAILS, so the
allowlist cannot quietly accumulate permissions for problems that were fixed.

CORPUS. Documents a reader could act on: the install guide, the published docs site, the
agents' own system prompts, and the operator's script reference. Deliberately excluded,
each for a stated reason:

  docs/build/**   the build's own history. LEDGER.md records that phase 2 renamed
                  `PlatformAgent`; scrubbing the name would erase the record of the
                  rename. History is allowed to describe the past.
  docs/design/**  the specifications, which state the migration ("today's `PlatformAgent`
                  becomes the platform-tier instance"). Editing them is a spec change and
                  belongs to a spec unit, not a documentation unit.
  **/testdata/**  fixtures exist to be compared against, not read.

Exit codes: 0 pass, 1 findings, 2 the check could not run (distinct from a pass -- LSN-019).
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# --- corpus ---------------------------------------------------------------------------
CORPUS_FILES = [
    "INSTALL.md",
    "README.md",
    "AGENTS.md",
    "GKE_SETUP.md",
    "k8s-operator/README.md",
    "k8s-operator/scripts/README.md",
    # Not prose, but the most acted-upon document in the install path: readers copy it to
    # `vars.sh` and run it. Its comments carried one of the two stale allow-all claims.
    "k8s-operator/scripts/vars.sh.example",
]
CORPUS_GLOBS = [
    "docs/site/src/content/docs/**/*.md",
    "docs/site/src/content/docs/**/*.mdx",
    "agents/*/SOUL.md",
    "agents/*/AGENTS.md",
]
EXCLUDED_DIRS = ("docs/build/", "docs/design/", "testdata/", "node_modules/")

CRD_DIR = REPO / "k8s-operator" / "config" / "crd" / "bases"
GOLDEN = REPO / "k8s-operator/internal/testing/testdata/platform/expected/agent.yaml"
SCRIPTS = REPO / "k8s-operator" / "scripts"
OPERATOR_MAKEFILE = REPO / "k8s-operator" / "Makefile"
INSTALL = REPO / "INSTALL.md"
STEP_DOC = REPO / "docs/site/src/content/docs/operator/provisioning-scripts.md"

API_GROUP = "kubeagents.x-k8s.io"

# --- check 6's curated list -----------------------------------------------------------
# Each entry: identifier -> why it is retired. The reason is printed in the failure, so
# whoever trips it learns what to write instead without going archaeology.
RETIRED = {
    "PlatformAgent": "renamed to the tier-discriminated `Agent` kind in phase 2 (06 §1.1)",
    "platformagents": "the CRD plural went with the rename; `kubectl get platformagents` errors",
    "platformagent-crd": "the docs page was renamed to agent-crd; the old slug 404s",
    "mcp-gke": "the toolset for the dropped cluster-mutating `gke` remote MCP (03 §4, 06 §9)",
    "create_cluster": "retired with the `gke` MCP; provisioning is now author-KCC-and-open-a-PR",
    "mcp-remote": "replaced by mcp_http_bridge.py in P8-T4; it wanted a browser a pod does not have",
    "proxy.js": "the mcp-remote entrypoint, removed with it",
    "ALLOW_ALL_USERS": "the permissive escape hatch, deleted in P8-T1 (V-CTR-014)",
}
# (path, identifier) -> reason it is legitimately there. Every entry must match, or fail:
# an exemption that stops matching is a permission nobody is watching any more.
#
# Empty is the intended steady state, and it is not a dormant check — the stale-entry
# arm below is what makes the allowlist self-invalidating, and it fires the moment an
# entry is added for a problem that later gets fixed properly. The one entry this
# started with (a glossary exemption for `create_cluster`) was written from memory and
# matched nothing; reading the glossary showed it named the `gke` remote server instead,
# so the honest fix was to correct the glossary, not to license the name.
#
# The corpus is documentation a reader ACTS on, so there is no "but the changelog has to
# say PlatformAgent" case here: the build's own record of the rename lives in
# docs/build/LEDGER.md and the migration lives in docs/design/06, both excluded above.
RETIRED_ALLOW: dict[tuple[str, str], str] = {}

USAGE = "usage: python3 local-dev/tests/docs-truth.py"


def die(msg: str) -> None:
    print(f"docs-truth: COULD NOT RUN: {msg}", file=sys.stderr)
    print("  This is exit 2, not a pass. Nothing was verified.", file=sys.stderr)
    sys.exit(2)


def corpus() -> list[Path]:
    out = []
    for rel in CORPUS_FILES:
        p = REPO / rel
        if p.is_file():
            out.append(p)
    for g in CORPUS_GLOBS:
        out.extend(sorted(REPO.glob(g)))
    keep = []
    for p in out:
        rel = p.relative_to(REPO).as_posix()
        if any(x in rel for x in EXCLUDED_DIRS):
            continue
        keep.append(p)
    if not keep:
        die("the corpus is empty — every glob matched nothing, so this would pass vacuously")
    return sorted(set(keep))


def rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


# --- sources of truth -----------------------------------------------------------------
def shipped_kinds() -> tuple[set[str], set[str]]:
    """(kinds, resource names) the shipped CRDs actually serve."""
    if not CRD_DIR.is_dir():
        die(f"{rel(CRD_DIR)} is missing; there is nothing to compare the docs against")
    kinds, resources = set(), set()
    for f in sorted(CRD_DIR.glob("*.yaml")):
        t = f.read_text()
        for key, dest in (("kind", kinds), ("listKind", kinds)):
            for m in re.finditer(rf"^\s{{4}}{key}:\s*(\S+)$", t, re.M):
                dest.add(m.group(1))
        for key in ("plural", "singular"):
            for m in re.finditer(rf"^\s{{4}}{key}:\s*(\S+)$", t, re.M):
                resources.add(m.group(1))
    if not kinds:
        die(f"parsed no kinds out of {rel(CRD_DIR)} — the CRD layout changed under this check")
    return kinds, resources


def rendered_mcp_surface() -> tuple[set[str], set[str]]:
    """(mcp server names, toolset entries) the operator's render actually emits.

    Read from the golden rendered ConfigMap rather than from any `config.yaml` on disk.
    Precondition P6: the image-baked config is shadowed by the operator's render at
    runtime, so a doc that matches the baked file and not the render is still wrong.
    """
    if not GOLDEN.is_file():
        die(f"{rel(GOLDEN)} is missing; the runtime-authoritative render has no witness")
    lines = GOLDEN.read_text().splitlines()
    servers, toolsets = set(), set()
    in_servers = in_toolsets = False
    for line in lines:
        s = line.strip()
        if re.match(r"^\s{4}mcp_servers:\s*$", line):
            in_servers, in_toolsets = True, False
            continue
        if re.match(r"^\s{4}platform_toolsets:\s*$", line):
            in_servers, in_toolsets = False, True
            continue
        if re.match(r"^\s{4}\S", line):  # any other top-level key inside the block scalar
            in_servers = in_toolsets = False
            continue
        if in_servers and re.match(r"^\s{6}([A-Za-z0-9_]+):\s*$", line):
            servers.add(re.match(r"^\s{6}([A-Za-z0-9_]+):", line).group(1))
        if in_toolsets and s.startswith("- "):
            toolsets.add(s[2:].strip())
    if not servers or not toolsets:
        die(
            f"parsed no MCP servers/toolsets out of {rel(GOLDEN)} — the render layout "
            "changed and this check would pass vacuously"
        )
    return servers, toolsets


def provision_steps() -> dict[str, str]:
    """NN -> script basename, from the filesystem."""
    if not SCRIPTS.is_dir():
        die(f"{rel(SCRIPTS)} is missing")
    steps = {}
    for f in sorted(SCRIPTS.glob("provision_[0-9][0-9]_*.sh")):
        steps[f.name[10:12]] = f.name
    if not steps:
        die("no provision_NN_*.sh scripts found; the step list would be vacuously consistent")
    return steps


# --- checks ---------------------------------------------------------------------------
FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$")


def fenced_blocks(text: str):
    """Yield (lang, start_line_1indexed, [lines]) for each fenced code block."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        indent, fence, lang = m.group(1), m.group(2), m.group(3)
        body, j = [], i + 1
        while j < len(lines):
            c = FENCE.match(lines[j])
            if c and c.group(2)[0] == fence[0] and len(c.group(2)) >= len(fence) and not c.group(3):
                break
            body.append(lines[j])
            j += 1
        yield lang, i + 2, body, indent
        i = j + 1


def check_kinds_exist(files, kinds, findings):
    """A `kind:` in a docs YAML block must be a kind the cluster can actually admit."""
    ours = re.compile(r"^\s*kind:\s*([A-Z]\w+)\s*$")
    for p in files:
        text = p.read_text()
        for lang, start, body, _ in fenced_blocks(text):
            if lang not in ("yaml", "yml", ""):
                continue
            if API_GROUP not in "\n".join(body):
                continue  # not one of our objects; a Deployment is not this check's business
            for n, line in enumerate(body):
                m = ours.match(line)
                if not m:
                    continue
                k = m.group(1)
                if k in ("CustomResourceDefinition",) or k in kinds:
                    continue
                findings.append(
                    (
                        f"{rel(p)}:{start + n}",
                        f"YAML block declares `kind: {k}` in the {API_GROUP} group, but the "
                        f"shipped CRDs serve only {sorted(kinds)}. Applying this fails.",
                    )
                )


KUBECTL_VERB = re.compile(
    r"\bkubectl\s+(?:[a-z-]+\s+)*?(get|delete|describe|edit|patch)\s+([a-z][a-z0-9.-]*)"
)


def check_kubectl_resources(files, resources, findings):
    """`kubectl get <plural>` in a doc must name a resource the API serves."""
    suspicious = re.compile(r"agent", re.I)  # only our own resource family
    for p in files:
        for n, line in enumerate(p.read_text().splitlines(), 1):
            for verb, res in KUBECTL_VERB.findall(line):
                base = res.split(".")[0]
                if not suspicious.search(base) or base in resources:
                    continue
                findings.append(
                    (
                        f"{rel(p)}:{n}",
                        f"`kubectl {verb} {res}` names a resource the API does not serve "
                        f"(served: {sorted(resources)}). This command fails when run.",
                    )
                )


PATH_IN_CMD = re.compile(r"(?:-f|--filename|--values)[= ]([A-Za-z0-9_./-]+\.(?:ya?ml|json))")


def check_referenced_paths_exist(files, findings):
    """`kubectl apply -f <path>` in a doc must point at a file that exists.

    Roots tried in order, because the docs legitimately mix repo-root-relative and
    `k8s-operator/`-relative commands (INSTALL.md Method 2 says "run inside k8s-operator/").
    """
    roots = [REPO, REPO / "k8s-operator", REPO / "deploy", REPO / "examples"]
    for p in files:
        text = p.read_text()
        for lang, start, body, _ in fenced_blocks(text):
            if lang not in ("bash", "sh", "shell", "console", "text", ""):
                continue
            for n, line in enumerate(body):
                for path in PATH_IN_CMD.findall(line):
                    if path.startswith(("http", "https", "<", "$", "/")) or "REPLACE" in path:
                        continue
                    if any((r / path).exists() for r in roots):
                        continue
                    findings.append(
                        (
                            f"{rel(p)}:{start + n}",
                            f"command references `{path}`, which does not exist under any of "
                            f"{[rel(r) if r != REPO else '.' for r in roots]}.",
                        )
                    )


def check_provisioning_steps(steps, findings):
    """Every step on disk is invokable and documented; nothing documented is imaginary."""
    mk = OPERATOR_MAKEFILE.read_text() if OPERATOR_MAKEFILE.is_file() else ""
    if not mk:
        die(f"{rel(OPERATOR_MAKEFILE)} is missing")
    install = INSTALL.read_text() if INSTALL.is_file() else ""
    stepdoc = STEP_DOC.read_text() if STEP_DOC.is_file() else ""
    if not install or not stepdoc:
        die("INSTALL.md or the provisioning-scripts page is missing")

    for nn, script in sorted(steps.items()):
        if not re.search(rf"^gcp-provision-{nn}-[\w-]+:", mk, re.M):
            findings.append(
                (
                    f"k8s-operator/Makefile",
                    f"`{script}` exists but no `gcp-provision-{nn}-*` target invokes it. The "
                    "documented per-step path cannot reach it; only the all-in-one "
                    "provision.sh can.",
                )
            )
        if (SCRIPTS / script.replace("provision_", "teardown_", 1)).is_file() and not re.search(
            rf"^gcp-teardown-{nn}-[\w-]+:", mk, re.M
        ):
            findings.append(
                (
                    f"k8s-operator/Makefile",
                    f"teardown_{nn}_* exists but no `gcp-teardown-{nn}-*` target invokes it.",
                )
            )
        if not re.search(rf"\*\*{nn}:", install):
            findings.append(
                (
                    "INSTALL.md",
                    f"step {nn} (`{script}`) is missing from the Modular Pipeline Stages list.",
                )
            )
        if not re.search(rf"### {nn}\.", stepdoc):
            findings.append(
                (
                    rel(STEP_DOC),
                    f"step {nn} (`{script}`) has no section on the provisioning-scripts page.",
                )
            )

    for m in re.finditer(r"^\d+\.\s+\*\*(\d{2}):", install, re.M):
        if m.group(1) not in steps:
            findings.append(
                ("INSTALL.md", f"documents step {m.group(1)}, which has no provision script.")
            )


def check_mcp_surface(files, servers, toolsets, findings):
    """A config excerpt in the docs may only name MCP servers/toolsets the render emits."""
    for p in files:
        text = p.read_text()
        for lang, start, body, _ in fenced_blocks(text):
            joined = "\n".join(body)
            if "mcp_servers:" not in joined and "platform_toolsets:" not in joined:
                continue
            in_servers = in_toolsets = False
            base = None
            name_indent = None  # exactly one nesting level in; `args:`/`env:` sit deeper
            for n, line in enumerate(body):
                stripped = line.strip()
                indent = len(line) - len(line.lstrip())
                if stripped.startswith("mcp_servers:"):
                    in_servers, in_toolsets, base, name_indent = True, False, indent, None
                    continue
                if stripped.startswith("platform_toolsets:"):
                    in_servers, in_toolsets, base, name_indent = False, True, indent, None
                    continue
                if stripped and base is not None and indent <= base and not stripped.startswith("#"):
                    in_servers = in_toolsets = False
                if in_servers and stripped and not stripped.startswith("#"):
                    if name_indent is None:
                        name_indent = indent
                    m = re.match(r"^([A-Za-z0-9_]+):\s*$", stripped) if indent == name_indent else None
                    if m and m.group(1) not in servers:
                        findings.append(
                            (
                                f"{rel(p)}:{start + n}",
                                f"config excerpt declares MCP server `{m.group(1)}`, which the "
                                f"operator's render does not emit (emits: {sorted(servers)}). "
                                "Copy-pasting this block configures something that is not shipped.",
                            )
                        )
                if in_toolsets and stripped.startswith("- "):
                    name = stripped[2:].strip()
                    if name and name not in toolsets:
                        findings.append(
                            (
                                f"{rel(p)}:{start + n}",
                                f"config excerpt lists toolset `{name}`, which the operator's "
                                f"render does not emit (emits: {sorted(toolsets)}).",
                            )
                        )


def check_retired_identifiers(files, findings):
    """Curated: names that used to mean something and now mean nothing."""
    used_allow = set()
    for p in files:
        r = rel(p)
        for n, line in enumerate(p.read_text().splitlines(), 1):
            for ident, why in RETIRED.items():
                if ident not in line:
                    continue
                key = (r, ident)
                if key in RETIRED_ALLOW:
                    used_allow.add(key)
                    continue
                findings.append((f"{r}:{n}", f"names retired `{ident}` — {why}"))
    for key, why in RETIRED_ALLOW.items():
        if key not in used_allow:
            findings.append(
                (
                    "local-dev/tests/docs-truth.py",
                    f"RETIRED_ALLOW carries a stale exemption for {key[1]} in {key[0]} "
                    f'("{why}") that no longer matches anything. Delete it: an allowlist '
                    "entry nobody needs is a permission waiting to be misused.",
                )
            )


def main() -> int:
    if len(sys.argv) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    files = corpus()
    kinds, resources = shipped_kinds()
    servers, toolsets = rendered_mcp_surface()
    steps = provision_steps()

    print(f"docs-truth: corpus = {len(files)} documents a reader could act on")
    print(f"  kinds served      : {sorted(kinds)}")
    print(f"  resources served  : {sorted(resources)}")
    print(f"  MCP servers        : {sorted(servers)}")
    print(f"  provisioning steps : {sorted(steps)}")

    findings: list[tuple[str, str]] = []
    check_kinds_exist(files, kinds, findings)
    check_kubectl_resources(files, resources, findings)
    check_referenced_paths_exist(files, findings)
    check_provisioning_steps(steps, findings)
    check_mcp_surface(files, servers, toolsets, findings)
    check_retired_identifiers(files, findings)

    if findings:
        print(f"\ndocs-truth: {len(findings)} finding(s)\n")
        for where, what in findings:
            print(f"  {where}\n      {what}")
        return 1
    print("\ndocs-truth: 6 checks, no findings — the documentation matches the build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
