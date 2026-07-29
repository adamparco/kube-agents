#!/usr/bin/env python3
"""The install overlay renders, and renders FAITHFULLY — V-CMP-008 (Phase 9, P9-T7d-6).

`make render` (a prerequisite of `make build` and `make test`) proves the overlay *builds*. This
check proves the much harder second thing: that what it builds is the install anyone actually
wants. The two are not the same property, and the gap between them is where this unit's defect
lived.

## What happened

The mesh CA (08 §2.3) landed in `config/mesh-ca/` and was wired into the install by adding
`- ../mesh-ca` to `config/default/kustomization.yaml`. In the same overlay, above it, sit two
transformers and a set of replacements:

    namePrefix: kubeagents-
    namespace: kubeagents-system

A kustomize transformer applies to every resource beneath it, with no per-resource opt-out. So the
CA -- three objects whose names and namespace are load-bearing, referenced by a hardcoded constant
in Go and by cert-manager's own cluster-resource-namespace rule -- was rewritten into:

    ClusterIssuer/kubeagents-kubeagents-mesh-ca          (nothing references this name)
    Certificate/kubeagents-kubeagents-mesh-ca            in namespace kubeagents-system
                                                          (a ClusterIssuer will not look there)

Neither rewrite is an error. Both render. Both apply. And the install that results has no working
trust root: every per-agent `Certificate` asks to be signed by `ClusterIssuer/kubeagents-mesh-ca`,
which does not exist, and a Certificate whose `issuerRef` does not resolve does not fail loudly --
it sits Pending forever. The visible symptom is brokers that never become Ready, several layers and
some hours away from the cause.

Nobody saw any of this, because a *third* consequence hid it: adding those three objects made two
`replacements` selectors ambiguous (`Certificate` with no `name:` now matched three objects instead
of one), so `kustomize build config/default` exited non-zero. For a month, the sanctioned deploy
path -- `make deploy`, and therefore `provision_03_gcp_gke_operator.sh` -- did not work at all, and
no build, no test, and no CI job noticed, because nothing in the repository ever rendered it.

The fix has two halves and they must ship together: pin the selectors so the render succeeds, and
lift `../mesh-ca` out of the transforming overlay into `config/install`, which applies none. Pinning
alone is strictly worse than the status quo -- today nothing installs; with only the pin, a broken
trust root installs silently.

## What this check asserts

Nine properties, all structural, all true without a cluster and without kustomize:

  1. **The cluster-facing targets render `config/install`.** `deploy` and `undeploy` must pipe
     `config/install` into kubectl, and no Makefile line may pipe `config/default` there.
     `config/default` is the transformed half; it is a legitimate build input and an illegitimate
     install.
  2. **`config/install` applies no transformers.** Its whole reason to exist is being a pure union.
     The moment it grows a `namePrefix` or a `namespace`, the mesh CA is back inside the blast
     radius and the fix is silently undone.
  3. **`config/install` includes both halves** -- `../default` and `../mesh-ca`. Dropping either
     yields an install that is missing a control plane or missing its trust root.
  4. **No transforming kustomization can reach `config/mesh-ca`** -- over the whole inclusion graph,
     not just the one edge that broke. This is the general form of the defect: re-nesting the CA
     under a *new* transforming layer next year is the same bug with a different diff, and a check
     that only knew about `config/default` would pass it.
  5. **The Go constant and the rendered ClusterIssuer agree.** `meshCAIssuerName` in
     `internal/controller/mesh_trust.go` is the one string the operator hardcodes about the trust
     root; a `ClusterIssuer` with exactly that name must exist in the untransformed tree.
  6. **The CA Certificate lives in `cert-manager`.** A ClusterIssuer resolves `ca.secretName` from
     the cluster resource namespace and nowhere else, so this namespace is not a preference.
  7. **The CA Certificate's `secretName` is the one the ClusterIssuer reads.** The pair has to
     agree; if they drift, the issuer points at a Secret nothing creates and fails the same silent
     way.
  8. **`build` and `test` depend on `render`, and `render` renders `config/install`.** This is the
     property whose absence let the whole thing sit undetected. Without it the other eight assert a
     shape nothing ever builds.
  9. **Every remote reference to the operator's overlay names `config/install`.** The GitOps
     bootstrap wave and the `propose-cluster-admin` template pull the overlay by URL. They were
     pinned to `config/default`, so a cluster bootstrapped through GitOps would come up with no
     trust root even after the local fix -- the same defect, reached by the path a real cluster
     actually takes.

Deliberately NOT checked: that the render output is correct object-by-object. That needs kustomize,
which needs a downloaded binary, which is not L0 (see the header of `.github/workflows/l0-checks.yml`
-- no dependencies are installed on purpose). `make render` covers "it builds" at build time; this
covers the reference graph that decides whether building it means anything.

Negative control (`--negative-control`): each property is re-run against a copy of the tree with the
defect it guards reintroduced, and must fail. A check that cannot fail is not evidence (09 §6,
V-MET-014). Property 4's control reintroduces the *original* bug verbatim -- `../mesh-ca` back under
`config/default` -- which is the only real proof that this check would have caught it.

Exit 0 if every property holds. Exit 1 otherwise.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CONFIG = "k8s-operator/config"
MAKEFILE = "k8s-operator/Makefile"
MESH_TRUST_GO = "k8s-operator/internal/controller/mesh_trust.go"
MESH_CA_DIR = "k8s-operator/config/mesh-ca"
INSTALL_DIR = "k8s-operator/config/install"

# Keys that rewrite, relabel, or patch every resource beneath them. Any one of these on a path to
# the mesh CA is the defect. `configMapGenerator`/`secretGenerator` are absent on purpose: they add
# resources, they do not rewrite existing ones.
TRANSFORMER_KEYS = (
    "namePrefix",
    "nameSuffix",
    "namespace",
    "labels",
    "commonLabels",
    "commonAnnotations",
    "patches",
    "patchesStrategicMerge",
    "patchesJson6902",
    "replacements",
    "images",
    "replicas",
    "transformers",
    "components",
)

# The overlay a cluster is entitled to see. Anything else piped into kubectl, or pulled by URL, is a
# partial install wearing the name of a whole one.
INSTALL_OVERLAY = "config/install"
FORBIDDEN_OVERLAY = "config/default"

TOP_LEVEL_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):")
LIST_ITEM = re.compile(r"^\s+-\s+(.+?)\s*$")
KUSTOMIZE_APPLY = re.compile(
    r"\$\(KUSTOMIZE\)\s+build\s+(\S+)\s*\|\s*\$\(KUBECTL\)\s+(apply|delete)"
)
KUSTOMIZE_BUILD = re.compile(r"\$\(KUSTOMIZE\)\s+build\s+(\S+)")
MAKE_TARGET = re.compile(r"^([a-zA-Z][a-zA-Z0-9_.-]*):(?!=)\s*(.*?)(?:\s*##.*)?$")
MESH_CA_CONST = re.compile(r'meshCAIssuerName\s*=\s*"([^"]+)"')
REMOTE_OVERLAY = re.compile(r"kube-agents/k8s-operator/(config/[A-Za-z0-9_-]+)")


class Failure(Exception):
    """A property did not hold. The message is the whole report."""


# ---------------------------------------------------------------------------------------------
# Minimal YAML reading. PyYAML is not available on every host that runs the L0 chain, and these
# files are small and hand-written, so the parsing is deliberately literal: top-level keys, list
# items under a named key, and the two or three scalars we care about inside a document.
# ---------------------------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Drop whole-line comments. Enough for these files; nothing here has a trailing `#` in a value."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def top_level_keys(text: str) -> set[str]:
    return {m.group(1) for m in (TOP_LEVEL_KEY.match(l) for l in strip_comments(text).splitlines()) if m}


def list_under(text: str, key: str) -> list[str]:
    """The list items directly under a top-level `key:`, in order."""
    items: list[str] = []
    collecting = False
    for line in strip_comments(text).splitlines():
        if TOP_LEVEL_KEY.match(line):
            collecting = TOP_LEVEL_KEY.match(line).group(1) == key
            continue
        if not collecting:
            continue
        m = LIST_ITEM.match(line)
        if m:
            items.append(m.group(1))
        elif line.strip():
            collecting = False
    return items


def documents(text: str) -> list[str]:
    return [d for d in re.split(r"^---\s*$", strip_comments(text), flags=re.M) if d.strip()]


def scalar(doc: str, key: str, indent: int) -> str | None:
    m = re.search(rf"^{' ' * indent}{re.escape(key)}:\s*(\S.*?)\s*$", doc, re.M)
    return m.group(1) if m else None


def kustomizations(root: Path) -> dict[str, str]:
    """Every kustomization under k8s-operator/config, keyed by its repo-relative directory."""
    out = {}
    for p in sorted((root / CONFIG).rglob("kustomization.yaml")):
        out[p.parent.relative_to(root).as_posix()] = p.read_text()
    return out


# ---------------------------------------------------------------------------------------------
# The nine properties. Each raises Failure with a report, or returns a one-line summary.
# ---------------------------------------------------------------------------------------------


def p1_cluster_targets_render_install(root: Path) -> str:
    text = (root / MAKEFILE).read_text()
    piped = [(m.group(2), m.group(1)) for m in KUSTOMIZE_APPLY.finditer(text)]
    if not piped:
        raise Failure(
            f"{MAKEFILE}: no `$(KUSTOMIZE) build <overlay> | $(KUBECTL) apply/delete` line at all.\n"
            "  Either the deploy path was rewritten in a shape this check cannot see, or it is gone.\n"
            "  Finding nothing is a FAIL, not a vacuous pass (LSN-035)."
        )
    bad = [f"  {verb}s {overlay}" for verb, overlay in piped if overlay == FORBIDDEN_OVERLAY]
    if bad:
        raise Failure(
            f"{MAKEFILE}: a cluster-facing target renders `{FORBIDDEN_OVERLAY}`:\n"
            + "\n".join(bad)
            + f"\n  `{FORBIDDEN_OVERLAY}` is the TRANSFORMED half of the install. It has no mesh CA,"
            f"\n  so the cluster comes up with no trust root and every broker waits forever on a"
            f"\n  Certificate that cannot be signed. Render `{INSTALL_OVERLAY}`."
        )
    for target in ("deploy", "undeploy"):
        recipe = make_recipe(text, target)
        if recipe is None:
            raise Failure(f"{MAKEFILE}: no `{target}:` target. It is the install path; it may not vanish.")
        if INSTALL_OVERLAY not in recipe:
            raise Failure(
                f"{MAKEFILE}: `{target}` does not render `{INSTALL_OVERLAY}`.\n  recipe: {recipe.strip()}"
            )
    return f"{len(piped)} cluster-facing render(s), all `{INSTALL_OVERLAY}`"


def p2_install_applies_no_transformers(root: Path) -> str:
    text = (root / INSTALL_DIR / "kustomization.yaml").read_text()
    found = sorted(top_level_keys(text) & set(TRANSFORMER_KEYS))
    if found:
        raise Failure(
            f"{INSTALL_DIR}/kustomization.yaml declares transformer key(s): {', '.join(found)}\n"
            "  This layer exists to apply NONE. A transformer here reaches the mesh CA, which is the\n"
            "  exact defect `config/install` was created to make impossible. Put it in `config/default`,\n"
            "  where it only touches what the operator owns."
        )
    return "no transformer keys"


def p3_install_includes_both_halves(root: Path) -> str:
    text = (root / INSTALL_DIR / "kustomization.yaml").read_text()
    resources = list_under(text, "resources")
    missing = [r for r in ("../default", "../mesh-ca") if r not in resources]
    if missing:
        raise Failure(
            f"{INSTALL_DIR}/kustomization.yaml is missing resource(s): {', '.join(missing)}\n"
            f"  resources: {resources}\n"
            "  `../default` is the control plane and `../mesh-ca` is its trust root. An install with\n"
            "  only one of them is not an install."
        )
    return "includes ../default and ../mesh-ca"


def p4_no_transformer_reaches_the_mesh_ca(root: Path) -> str:
    """Walk the inclusion graph. Every path to config/mesh-ca must be transformer-free."""
    trees = kustomizations(root)
    if MESH_CA_DIR not in trees:
        raise Failure(f"{MESH_CA_DIR}/kustomization.yaml is missing; there is no mesh CA to protect.")

    # `root` is resolved too: on macOS a tempdir under /var resolves to /private/var, and a
    # resolved child is then not relative_to an unresolved parent.
    real_root = root.resolve()
    edges: dict[str, list[str]] = {}
    for d, text in trees.items():
        targets = []
        for item in list_under(text, "resources"):
            if "://" in item or item.startswith("github.com/"):
                continue
            resolved = ((root / d) / item).resolve()
            if resolved.is_dir() and (resolved / "kustomization.yaml").exists():
                targets.append(resolved.relative_to(real_root).as_posix())
        edges[d] = targets

    includers = [d for d, targets in edges.items() if MESH_CA_DIR in targets]
    if not includers:
        raise Failure(
            f"nothing under {CONFIG} includes {MESH_CA_DIR}.\n"
            "  The mesh CA is not part of any overlay, so no install applies it. Finding no path is a\n"
            "  FAIL, not a vacuous pass (LSN-035)."
        )

    offenders = []
    for start in includers:
        # Every kustomization that can reach `start`, plus `start` itself: a transformer at any of
        # them lands on the CA.
        reachers = {start}
        changed = True
        while changed:
            changed = False
            for d, targets in edges.items():
                if d not in reachers and reachers & set(targets):
                    reachers.add(d)
                    changed = True
        for d in sorted(reachers):
            found = sorted(top_level_keys(trees[d]) & set(TRANSFORMER_KEYS))
            if found:
                offenders.append(f"  {d} -> ... -> {MESH_CA_DIR}   applies: {', '.join(found)}")

    if offenders:
        raise Failure(
            "a transforming kustomization can reach the mesh CA:\n"
            + "\n".join(sorted(set(offenders)))
            + "\n  A transformer applies to every resource beneath it and the CA cannot survive one:\n"
            "  `namePrefix` renames the ClusterIssuer that `meshCAIssuerName` hardcodes, and `namespace`\n"
            "  moves the CA Secret out of `cert-manager`, which is the only namespace a ClusterIssuer\n"
            "  resolves it from. Neither fails at apply time. Both leave every agent Certificate Pending."
        )
    return f"{len(includers)} path(s) to the mesh CA, all transformer-free"


def mesh_ca_documents(root: Path) -> list[str]:
    return documents((root / MESH_CA_DIR / "clusterissuer.yaml").read_text())


def p5_go_constant_matches_the_clusterissuer(root: Path) -> str:
    go = (root / MESH_TRUST_GO).read_text()
    m = MESH_CA_CONST.search(go)
    if not m:
        raise Failure(f"{MESH_TRUST_GO}: no `meshCAIssuerName = \"...\"` constant found.")
    want = m.group(1)
    names = [
        scalar(d, "name", 2)
        for d in mesh_ca_documents(root)
        if scalar(d, "kind", 0) == "ClusterIssuer" and "ca:" in d
    ]
    if want not in names:
        raise Failure(
            f"the operator signs every agent leaf against ClusterIssuer/{want}\n"
            f"  ({MESH_TRUST_GO}: meshCAIssuerName), but {MESH_CA_DIR} defines CA issuer(s): {names or 'none'}\n"
            "  A Certificate whose issuerRef does not resolve is not rejected. It stays Pending, so the\n"
            "  fleet fails to come up with no error anywhere that names the cause."
        )
    return f"meshCAIssuerName == ClusterIssuer/{want}"


def p6_ca_certificate_is_in_cert_manager(root: Path) -> str:
    certs = [d for d in mesh_ca_documents(root) if scalar(d, "kind", 0) == "Certificate"]
    if not certs:
        raise Failure(f"{MESH_CA_DIR}: no CA Certificate.")
    bad = [
        f"  Certificate/{scalar(d, 'name', 2)} in namespace {scalar(d, 'namespace', 2)!r}"
        for d in certs
        if scalar(d, "namespace", 2) != "cert-manager"
    ]
    if bad:
        raise Failure(
            "the mesh CA Certificate is not in `cert-manager`:\n"
            + "\n".join(bad)
            + "\n  A ClusterIssuer resolves `ca.secretName` from the cluster resource namespace, which is\n"
            "  cert-manager's own. Anywhere else and the key is where the issuer will not look -- and\n"
            "  anywhere an agent can be scheduled puts the CA private key in a tenant namespace."
        )
    return f"{len(certs)} CA Certificate(s) in cert-manager"


def p7_secret_name_agrees(root: Path) -> str:
    docs = mesh_ca_documents(root)
    produced = {scalar(d, "secretName", 2) for d in docs if scalar(d, "kind", 0) == "Certificate"}
    consumed = {
        scalar(d, "secretName", 4)
        for d in docs
        if scalar(d, "kind", 0) == "ClusterIssuer" and "ca:" in d
    }
    consumed.discard(None)
    if not consumed:
        raise Failure(f"{MESH_CA_DIR}: no CA ClusterIssuer reads a `ca.secretName`.")
    orphans = sorted(consumed - produced)
    if orphans:
        raise Failure(
            f"the CA ClusterIssuer reads Secret(s) {orphans} that no Certificate in {MESH_CA_DIR} creates.\n"
            f"  Certificates produce: {sorted(s for s in produced if s)}\n"
            "  The issuer never becomes Ready, and every leaf it was supposed to sign stays Pending."
        )
    return f"ca.secretName {sorted(consumed)} is produced in-tree"


def make_recipe(text: str, target: str) -> str | None:
    """The recipe body of a Makefile target, or None if the target is absent."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = MAKE_TARGET.match(line)
        if m and m.group(1) == target:
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("\t"):
                    body.append(nxt)
                elif nxt.strip() and not nxt.lstrip().startswith("#"):
                    break
            return "\n".join(body)
    return None


def make_prereqs(text: str, target: str) -> list[str] | None:
    for line in text.splitlines():
        m = MAKE_TARGET.match(line)
        if m and m.group(1) == target:
            return m.group(2).split()
    return None


def p8_render_is_a_build_prerequisite(root: Path) -> str:
    text = (root / MAKEFILE).read_text()
    recipe = make_recipe(text, "render")
    if recipe is None:
        raise Failure(
            f"{MAKEFILE}: no `render:` target.\n"
            "  Without it nothing in the repository builds the install overlay, which is precisely how\n"
            "  `make deploy` stayed broken for a month while every check stayed green."
        )
    built = KUSTOMIZE_BUILD.findall(recipe)
    if INSTALL_OVERLAY not in built:
        raise Failure(f"{MAKEFILE}: `render` does not build `{INSTALL_OVERLAY}` (builds: {built or 'nothing'}).")
    for target in ("build", "test"):
        prereqs = make_prereqs(text, target)
        if prereqs is None:
            raise Failure(f"{MAKEFILE}: no `{target}:` target.")
        if "render" not in prereqs:
            raise Failure(
                f"{MAKEFILE}: `{target}` does not depend on `render` (prereqs: {' '.join(prereqs)}).\n"
                "  CI runs `make -C k8s-operator test`. If the render is not one of its prerequisites,\n"
                "  an unbuildable overlay is green in CI and red only on a cluster."
            )
    return "render builds config/install; build and test depend on it"


def p9_remote_references_name_install(root: Path, tracked: list[str]) -> str:
    refs = []
    for rel in tracked:
        if not Path(rel).name.startswith("kustomization.yaml"):
            continue
        p = root / rel
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in REMOTE_OVERLAY.finditer(line):
                refs.append((rel, i, m.group(1)))
    if not refs:
        raise Failure(
            "no kustomization pulls the operator overlay by URL.\n"
            "  The GitOps bootstrap wave and the propose-cluster-admin template both do. Finding none\n"
            "  means the search stopped matching, not that the risk went away (LSN-035)."
        )
    bad = [f"  {rel}:{i} -> {overlay}" for rel, i, overlay in refs if overlay != INSTALL_OVERLAY]
    if bad:
        raise Failure(
            f"a remote reference pulls an overlay other than `{INSTALL_OVERLAY}`:\n"
            + "\n".join(bad)
            + "\n  This is the path a real cluster takes. Pinned to `config/default` it bootstraps a\n"
            "  control plane with no mesh CA -- the local fix does not reach it."
        )
    return f"{len(refs)} remote reference(s), all `{INSTALL_OVERLAY}`"


PROPERTIES = [
    ("1", "cluster-facing targets render config/install", p1_cluster_targets_render_install),
    ("2", "config/install applies no transformers", p2_install_applies_no_transformers),
    ("3", "config/install includes both halves", p3_install_includes_both_halves),
    ("4", "no transformer can reach the mesh CA", p4_no_transformer_reaches_the_mesh_ca),
    ("5", "meshCAIssuerName matches the ClusterIssuer", p5_go_constant_matches_the_clusterissuer),
    ("6", "the CA Certificate is in cert-manager", p6_ca_certificate_is_in_cert_manager),
    ("7", "the CA secretName agrees end to end", p7_secret_name_agrees),
    ("8", "render is a prerequisite of build and test", p8_render_is_a_build_prerequisite),
    ("9", "remote references name config/install", p9_remote_references_name_install),
]


def run(root: Path, tracked: list[str], quiet: bool = False) -> list[str]:
    """Run every property. Returns the list of failure reports (empty means pass)."""
    failures = []
    for num, title, fn in PROPERTIES:
        try:
            detail = fn(root, tracked) if fn is p9_remote_references_name_install else fn(root)
        except Failure as exc:
            failures.append(f"{num}. {title}")
            if not quiet:
                print(f"FAIL  {num}. {title}\n{exc}\n")
        else:
            if not quiet:
                print(f"ok    {num}. {title} — {detail}")
    return failures


# ---------------------------------------------------------------------------------------------
# Negative control
# ---------------------------------------------------------------------------------------------


def tracked_paths() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


def materialize(tracked: list[str], dest: Path) -> None:
    """Copy just the files the properties read. `config/install` is untracked on a fresh branch, so
    the config tree is copied from disk rather than from the git index."""
    wanted = [
        rel
        for rel in tracked
        if rel in (MAKEFILE, MESH_TRUST_GO) or Path(rel).name.startswith("kustomization.yaml")
    ]
    for rel in wanted:
        src = REPO / rel
        if not src.exists():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
    shutil.copytree(REPO / CONFIG, dest / CONFIG, dirs_exist_ok=True)


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if old not in text:
        raise SystemExit(
            f"negative control cannot break {path}: anchor not found.\n"
            f"  anchor: {old!r}\n"
            "  The control is now asserting nothing. Fix the anchor before trusting this check."
        )
    path.write_text(text.replace(old, new, 1))


# Each entry: the property it must break, and a mutation that reintroduces exactly that defect.
BREAKAGES = [
    (
        "1",
        "deploy renders config/default again",
        lambda d: patch(
            d / MAKEFILE,
            "$(KUSTOMIZE) build config/install | $(KUBECTL) apply -f -",
            "$(KUSTOMIZE) build config/default | $(KUBECTL) apply -f -",
        ),
    ),
    (
        "2",
        "config/install grows a namePrefix",
        lambda d: patch(
            d / INSTALL_DIR / "kustomization.yaml", "resources:", "namePrefix: kubeagents-\nresources:"
        ),
    ),
    (
        "3",
        "config/install drops the mesh CA",
        lambda d: patch(d / INSTALL_DIR / "kustomization.yaml", "  - ../mesh-ca\n", ""),
    ),
    (
        "4",
        "the ORIGINAL bug: ../mesh-ca back under config/default",
        lambda d: (
            patch(d / INSTALL_DIR / "kustomization.yaml", "  - ../mesh-ca\n", ""),
            patch(d / CONFIG / "default/kustomization.yaml", "  - ../certmanager\n", "  - ../certmanager\n  - ../mesh-ca\n"),
        ),
    ),
    (
        "5",
        "the ClusterIssuer is renamed out from under the Go constant",
        lambda d: patch(
            d / MESH_CA_DIR / "clusterissuer.yaml",
            "  name: kubeagents-mesh-ca\n  labels:\n    app.kubernetes.io/name: kube-agents-operator\n    app.kubernetes.io/component: mesh-ca\nspec:\n  ca:",
            "  name: kubeagents-mesh-ca-v2\n  labels:\n    app.kubernetes.io/name: kube-agents-operator\n    app.kubernetes.io/component: mesh-ca\nspec:\n  ca:",
        ),
    ),
    (
        "6",
        "the CA Certificate is moved to kubeagents-system",
        lambda d: patch(d / MESH_CA_DIR / "clusterissuer.yaml", "  namespace: cert-manager", "  namespace: kubeagents-system"),
    ),
    (
        "7",
        "the issuer reads a Secret no Certificate creates",
        lambda d: patch(
            d / MESH_CA_DIR / "clusterissuer.yaml",
            "  ca:\n    secretName: kubeagents-mesh-ca-key-pair",
            "  ca:\n    secretName: kubeagents-mesh-ca-tls",
        ),
    ),
    (
        "8",
        "test no longer depends on render",
        lambda d: patch(d / MAKEFILE, "test: manifests generate fmt vet render setup-envtest", "test: manifests generate fmt vet setup-envtest"),
    ),
    (
        "9",
        "the GitOps bootstrap wave is repinned to config/default",
        lambda d: patch(
            d / "examples/gitops-repo/clusters/cluster-a/bootstrap/10-controller/kustomization.yaml",
            "config/install?ref=",
            "config/default?ref=",
        ),
    ),
]


def negative_control() -> int:
    tracked = tracked_paths()
    print(f"negative control: {len(BREAKAGES)} breakages, each must be caught\n")
    unc = []
    for num, description, mutate in BREAKAGES:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            materialize(tracked, dest)
            mutate(dest)
            failures = run(dest, tracked, quiet=True)
            if num in failures_numbers(failures):
                print(f"ok    property {num} caught: {description}")
            else:
                unc.append(f"  property {num} did NOT catch: {description}  (fired: {failures or 'nothing'})")
    if unc:
        print("\nUNCAUGHT — this check cannot fail the way it claims to:\n" + "\n".join(unc))
        return 1
    print(f"\nPASS — all {len(BREAKAGES)} breakages caught.")
    return 0


def failures_numbers(failures: list[str]) -> set[str]:
    return {f.split(".", 1)[0] for f in failures}


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()
    failures = run(REPO, tracked_paths())
    if failures:
        print(f"\nFAIL — {len(failures)} of {len(PROPERTIES)} properties: {', '.join(failures)}")
        return 1
    print(f"\nPASS — all {len(PROPERTIES)} properties hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
