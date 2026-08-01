#!/usr/bin/env python3
"""A retired object shape is only retired where somebody looked — L0, no cluster, no network.

## What happened

`12c509d` retired the namespaced half of the 06 §2.2.1 broker-operations grant. It deleted
`Role kubeagents-broker-operations` from `k8s-operator/scripts/broker-operations-grant.yaml.template`
and `RoleBinding ${AGENT_READER_KSA}-broker-operations` from
`k8s-operator/scripts/agent-identity.yaml.template`, moved what they granted into the per-tier
`actor-grant-<tier>.yaml.template`, and wrote the retirement down in both files' headers. Every check
in the tree stayed green, and the install path really did stop rendering both objects.

A read-only survey of `gke-scratch-kube-agents-dev` on 2026-08-01 found them standing anyway. The
retirement had been carried out on the install path and nowhere else: the reference GitOps tree and —
the part that matters — the assets an agent *proposes* when it provisions a new tier still render
both shapes. `provision-cluster-admin` and `provision-developer-team` create the retired objects **fresh,
from a template, on day one**, in production. "No template renders that object" was true of
`k8s-operator/scripts/**` and false of the repository.

The general shape, which is what this file is about and why it is not a grep for one name: **an
object is retired from a renderer, and the tree has more than one renderer.** The second renderer is
always the one nobody was editing that day — a reference tree, an example, a skill asset — and it is
frequently the one a real cluster is actually built from. Nothing about the retirement diff makes the
other renderers visible, and nothing about the other renderers being wrong makes anything go red.

## The property

Derived from the tree alone, with no git history and no roster of names or files ([[LSN-036]] — a
check that hardcodes `kubeagents-broker-operations` is worthless the morning after somebody renames
it, and catches nothing else the class produces):

> **For every RBAC object NAME the install path renders, the set of KINDS rendered under that name
> by the install path is a superset of the set of kinds rendered under that name anywhere else in
> the repository.**

The install path is `k8s-operator/scripts/**` plus `k8s-operator/config/**`; everything else that
renders Kubernetes YAML is the other path. The domain is every document whose `apiVersion` is in
`rbac.authorization.k8s.io/*` — read off the document, not from a list of kinds, so a fifth RBAC kind
is in scope the day the API group grows one.

The defect falls out of it without being described to it. The install path renders
`kubeagents-broker-operations` as `{ClusterRole}`; the GitOps and skill paths render it as
`{ClusterRole, Role}`. The install path renders the binding as `{ClusterRoleBinding}`; the others
render `{ClusterRoleBinding, RoleBinding}`. `Role` and `RoleBinding` are kinds that exist only
*outside* the install path, under names the install path owns — which is the definition of an object
shape somebody retired in one place.

**Why this is the right predicate and not a sharper one.** The obvious sharpening is to substitute
each install-path variable with the value its call site passes (`identity-has-install-path.py` already
derives those) and compare names exactly. That is strictly more precise and it **misses half the
defect**: the install path renders the binding as `<reader-ksa>-broker-operations` while the GitOps
tree renders it as `<tier>-<scope>-broker-operations`, so an exact comparison finds no shared name and
reports nothing. The two spellings are the same object; only the shape says so.

**Scoping to names the install path also renders is what buys the low false-positive rate.** The
GitOps tree legitimately renders a great deal the install path never does — Namespaces, Agent CRs,
Kustomizations, and sixteen `vaptest-*` Roles and ClusterRoles that exist to be *rejected* by a
ValidatingAdmissionPolicy. None of them is under an install-owned name, so none is examined. Measured
on the tree of 2026-08-01: 82 RBAC documents, 25 on the install path and 57 off it; 37 of the 57 fall
under an install-owned name and are examined; 24 pass; **13 are findings and all 13 are the two
retired shapes. Zero false positives.**

## Normalisation, stated as a rule because it is the hard part

Names in these files are template-substituted — `${AGENT_READER_KSA}`, `@@NAMESPACE@@`, and the
concrete `cluster-a` / `team-x` an example bakes in. Two files render the same object under textually
different names. The rule:

  * A **placeholder** is `${...}`, `$WORD`, `@@...@@` or `{{...}}`. Every install-path name becomes a
    glob by replacing each placeholder with `*`. `${AGENT_READER_KSA}-broker-operations` is the
    pattern `*-broker-operations`, and `cluster-admin-agent-broker-operations` and
    `developer-team-@@NAMESPACE@@-broker-operations` are both matched by it.
  * The justification is not "globs are convenient". The install path's names are
    `<identity>-<function>`: the derived half varies per tier and the literal half names what the
    object *is*. The literal half is the part the install path owns, and an object elsewhere wearing
    the same literal half is the same object under a different identity.
  * **All of the normalisation is on the pattern side, and this is a finding, not an omission.** The
    first draft of this check also normalised the name being tested, substituting each placeholder
    with a fixed literal. Mutation-testing the check against its own controls showed that replacing
    that step with the identity function broke nothing: `fnmatch` interprets metacharacters in the
    pattern only and matches the subject literally, so `@@NAMESPACE@@` is already absorbed by the `*`
    the corresponding install-path placeholder produced. A step no control could kill is a step
    nothing measures, so it was deleted rather than kept and described. What that leaves is one rule
    with teeth — `pattern_of` — and the control below that kills it kills four rows at once.
  * **A pattern that is satisfied by an arbitrary name owns nothing.** `${AGENT_ACTOR_KSA}` is a
    whole-name placeholder and globs to `*`, which would own every name in the repository and make
    the allowed-kind set the union of everything — a check that reports zero findings for the most
    convincing reason there is. Such patterns are tested against decoys and dropped, loudly, with a
    count in the summary. This is load-bearing: without the drop, today's tree produces 0 findings.
  * **Normalisation is required to be total.** A name that still carries a character no Kubernetes
    object name may hold, after every recognised placeholder is removed, is a FAILURE — not a name
    that quietly matches nothing. A new templating syntax must break this check rather than empty it.
  * What the coarseness costs, stated plainly: `${CLUSTER_ADMIN_KSA_NAME}-explorer` and
    `${DEVELOPER_TEAM_KSA_NAME}-explorer` both glob to `*-explorer`, so their kind sets merge and a
    GitOps file that gave the developer-team explorer a *ClusterRole* would not be flagged here. That
    is a real gap and it is the price of matching two spellings of one object. It is covered from the
    other side by `vap-agent-readonly` validation 2 and by `actor-grant-single-sourced.py`.

## The four properties

  1. **The corpus is non-vacuous.** Floors on install-path RBAC documents, off-path RBAC documents,
     surviving name patterns, and — the one that matters — the number of off-path objects that
     actually fall under an install-owned name. Nought of any of them is `rc 1` with a message saying
     the scan found nothing, never a green run ([[LSN-035]]).
  2. **Every RBAC document is read.** A document under the RBAC API group with no `kind:` or no
     `metadata.name` is a FAILURE, and so is a document carrying a kind this tree uses as an RBAC kind
     under some other `apiVersion`. A parser that shrugs at input it cannot understand reports a
     smaller population than the tree renders, and a smaller population is how this check goes quiet.
  3. **Normalisation is total, exercised, and discriminating.** No name, on either side of the
     boundary, may carry a residue that is not an RFC 1123 subdomain once its recognised placeholders
     are removed. At least one *surviving owner pattern* must contain a wildcard, so the rule is
     exercised by the tree rather than merely implemented — a repository whose install-path names
     were all literal would compare only exact spellings and would not notice that
     `${AGENT_READER_KSA}-broker-operations` and `cluster-admin-agent-broker-operations` are the same
     object. And every surviving pattern must refuse a decoy.
  4. **The superset property**, above.

## Deliberately NOT checked

That the objects are *equivalent* — same rules, same subjects, same roleRef. `actor-grant-single-
sourced.py` (V-BRK-013) holds both copies of the grant to 06 §2.2.1 itself, in both directions, which
is the stronger property and the right home for it. This file asserts only that an object SHAPE does
not exist in one renderer and not the other, which is the property that survives a rename.

Also not checked: whether anything *deletes* the retired objects from a cluster that already has
them. `common.sh:1332` deletes the ClusterRoleBinding `${reader_ksa}-broker-operations` and nothing
deletes the identically-named namespaced RoleBinding or the namespaced Role, so a full teardown and
reprovision leaves all three standing. That is a property of a cluster, not of the tree, and it
belongs in `dev/L2-CHAIN.txt`.

## Negative control

`--negative-control` builds a SYNTHETIC repository in memory, confirms it is clean, and then replays
the `CONTROLS` table through the same `check()` the live run calls. Each row names the property it
targets and asserts a needle from that property's own message, so a corpus that goes red for some
other reason scores `MISS` rather than a catch ([[LSN-035]]). A row whose corpus is byte-identical to
the clean baseline, or whose `check()` call raises, scores `BROKEN` and is counted as a failure —
never `MISS`, which would invite strengthening a check that was never asked anything ([[LSN-063]]).

The table was built by mutating THIS FILE, not by listing plausible defects: every decision the check
makes — each floor, `pattern_of`, `discriminates`, `NAME_RESIDUE`, the parser's two refusals, the
install-path prefixes and the file filter — was replaced with a permissive version and the control
re-run. Each mutation is killed, and the row that kills it is the row that names it. That sweep is
what found and deleted the subject-side normalisation described above, and it is the reason the four
floors have a row apiece rather than one row standing in for all of them.

Row 1 is the retirement itself, in miniature: the install path stops rendering the namespaced pair
and the outside renderers keep rendering it. Row 2 is the same class after a **rename**, which is the
row that proves there is no roster. Verified against real history, offline: replayed over the tree at
`12c509d^` this check reports **0 findings**, and over the tree at `12c509d` it reports **13**, naming
both retired shapes in all six files.

NEGATIVE CONTROL DOES NOT EXERCISE ([[LSN-060]]): the control synthesises its corpus, so the following
run only in the live arm and are measured by nothing below —
  - `read_sources()`: the `git ls-files --cached --others --exclude-standard` enumeration, the
    tracked-plus-new corpus rule of [[LSN-050]], and the `.yaml`/`.yml`/`.template`/`.tmpl` filter.
  - The repo-root derivation from `__file__`, and reading the real files off disk.
  - The real tree's multi-document splitting, comment stripping and indentation, none of which the
    synthesised documents stress: they are well-formed by construction.
  - Any templating syntax the real tree uses that the synthetic corpus does not.
  The one arm that closes part of this — `the real tree is visible to the scan` — runs `check()` over
  the REAL corpus and requires that it report no `VACUOUS:` finding. It deliberately does not assert
  which findings appear, so it stays true after the product defect is fixed.

Run:  python3 dev/tests/retired-objects-are-not-rendered.py
      python3 dev/tests/retired-objects-are-not-rendered.py --negative-control

Exit 0 = no RBAC object shape exists outside the install path under a name the install path owns;
1 = violations. Stdlib only. No PyYAML, no cluster, no network.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gitcorpus import read_repo_files  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

# The install path: what `provision.sh` renders, and what `make deploy` applies. Two prefixes, not a
# file list — a template added beside these is in scope the day it lands.
INSTALL_ROOTS = ("k8s-operator/scripts/", "k8s-operator/config/")

# The domain. Read off each document's own `apiVersion`, so the kinds are whatever the API group
# serves rather than four names written down here.
RBAC_GROUP = "rbac.authorization.k8s.io/"

YAMLISH = re.compile(r"\.(ya?ml)(\.(template|tmpl))?$")

# `${VAR}`, `$VAR`, `@@VAR@@`, `{{ .Values.x }}`. Every substitution syntax the tree uses to stand a
# derived identity into an object name.
PLACEHOLDER = re.compile(r"\$\{[^{}]*\}|\$[A-Za-z_]\w*|@@[^@\s]*@@|\{\{[^{}]*\}\}")

# What a name is allowed to be made of once its placeholders are removed. RFC 1123 subdomain, which
# is every name the API server will accept. A residue outside this is a substitution syntax this
# check does not know about, and a name it silently failed to normalise matches no pattern and is
# examined by nothing. It also keeps `[` out of `pattern_of`'s output, where fnmatch would read it
# as a character class.
NAME_RESIDUE = re.compile(r"^[a-z0-9.\-]*$")

# A pattern is only an owner if it is satisfied by the flag it claims and refused by an arbitrary
# name. `*` passes the first test and fails the second, which is the whole difference between an
# install-path name and a wildcard that swallows the repository. Same move as `_satisfies` in
# `negative-controls-name-their-rule.py`, for the same reason.
DECOYS = ("decoy", "an-unrelated-object", "zzz", "kube-root-ca.crt")

# Non-vacuity floors. Each comfortably below the count on the tree of 2026-08-01 (25 / 57 / 12 / 37)
# and comfortably above zero. They are floors on the SCAN, not on the findings: a check of this shape
# fails silent by finding nothing to compare, and "found nothing" and "asked nothing" are the same
# exit code ([[LSN-035]], [[LSN-038]]).
MIN_INSTALL_RBAC = 8
MIN_OTHER_RBAC = 12
MIN_PATTERNS = 4
MIN_OWNED = 8


# ──────────────────────────────────────────────────────────────────────────────
# A narrow, strict YAML reader
# ──────────────────────────────────────────────────────────────────────────────
#
# PyYAML is not available on the hosts that run the L0 chain and `dev/tests/yamlsubset.py` rejects
# flow collections, which every `verbs: ["get", ...]` line in an RBAC file is. What this needs from a
# manifest is four scalars: apiVersion, kind, metadata.name, metadata.namespace. So it reads those
# four by indentation and REFUSES a document it cannot read, rather than skipping it — see property 2.


def _lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, text) for every line that is neither blank nor a whole-line comment."""
    return [
        (i, line.rstrip())
        for i, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _documents(text: str) -> list[list[tuple[int, str]]]:
    docs: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for i, line in _lines(text):
        if line.strip() == "---":
            if current:
                docs.append(current)
            current = []
            continue
        current.append((i, line))
    if current:
        docs.append(current)
    return docs


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(lines: list[tuple[int, str]], key: str, at: int) -> tuple[int | None, str | None]:
    """(line number, value) of `<key>:` at indentation `at`, or (None, None)."""
    prefix = " " * at + key + ":"
    for i, line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :]
        # YAML requires whitespace before an inline `#`, so this cannot eat a `#` inside a value.
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        return i, (value or None)
    return None, None


def _block(lines: list[tuple[int, str]], key: str, at: int) -> list[tuple[int, str]]:
    prefix = " " * at + key + ":"
    for idx, (_, line) in enumerate(lines):
        if line == prefix or line.startswith(prefix + " "):
            body = []
            for entry in lines[idx + 1 :]:
                if _indent(entry[1]) <= at:
                    break
                body.append(entry)
            return body
    return []


class Obj:
    """One Kubernetes document, reduced to the four things this check reasons about."""

    def __init__(self, rel: str, lines: list[tuple[int, str]]) -> None:
        self.rel = rel
        self.start = lines[0][0]
        _, self.api_version = _scalar(lines, "apiVersion", 0)
        kind_line, self.kind = _scalar(lines, "kind", 0)
        self.line = kind_line or self.start
        meta = _block(lines, "metadata", 0)
        _, self.name = _scalar(meta, "name", 2)
        _, self.namespace = _scalar(meta, "namespace", 2)

    @property
    def is_rbac(self) -> bool:
        return bool(self.api_version and self.api_version.startswith(RBAC_GROUP))

    @property
    def on_install_path(self) -> bool:
        return self.rel.startswith(INSTALL_ROOTS)

    def where(self) -> str:
        return f"{self.rel}:{self.line}"


def parse(sources: dict[str, str]) -> list[Obj]:
    objs = []
    for rel, text in sorted(sources.items()):
        if not YAMLISH.search(rel):
            continue
        for doc in _documents(text):
            objs.append(Obj(rel, doc))
    return objs


# ──────────────────────────────────────────────────────────────────────────────
# Normalisation
# ──────────────────────────────────────────────────────────────────────────────


def pattern_of(name: str) -> str:
    """The name as an ownership glob: every substituted span becomes `*`."""
    return PLACEHOLDER.sub("*", name)


def residue_of(name: str) -> str:
    """What is left once every recognised placeholder is deleted."""
    return PLACEHOLDER.sub("", name)


def discriminates(pattern: str) -> bool:
    """Satisfied by names the install path renders, refused by an arbitrary one."""
    return not any(fnmatch.fnmatchcase(decoy, pattern) for decoy in DECOYS)


# ──────────────────────────────────────────────────────────────────────────────
# The properties
# ──────────────────────────────────────────────────────────────────────────────


def check(sources: dict[str, str]) -> list[str]:
    """All four properties. Returns a list of findings; empty means the property holds."""
    bad: list[str] = []
    objs = parse(sources)

    # ── Property 2: every RBAC document is read ──────────────────────────────────────────────
    #
    # The kind set is derived from the tree — whatever this repository presents under the RBAC API
    # group — and then used to find documents wearing one of those kinds under some OTHER
    # apiVersion. That is either a manifest with the wrong group (a real defect: it will not apply)
    # or a document whose apiVersion this reader failed to see, and the second is the one that makes
    # the rest of this file quiet. Deriving the kind set rather than writing one down means a kind
    # that appears ONLY under the wrong group is not detected — the price of [[LSN-036]], and the
    # narrower miss of the two.
    rbac_kinds = {o.kind for o in objs if o.is_rbac and o.kind}
    for obj in objs:
        if obj.is_rbac and not obj.kind:
            bad.append(
                f"{obj.where()} is an RBAC document with no top-level `kind:`. This check cannot "
                f"read it, and a reader that skips what it cannot understand reports a smaller "
                f"population than the tree renders — which is how the comparison below goes quiet "
                f"([[LSN-035]])."
            )
        elif obj.is_rbac and not obj.name:
            bad.append(
                f"{obj.where()} is an RBAC {obj.kind} with no `metadata.name` this check can read "
                f"(flow mappings and anchors are not supported on purpose). An unnamed object is "
                f"invisible to a rule about names, and invisible reports as green."
            )
        elif obj.kind in rbac_kinds and not obj.is_rbac:
            bad.append(
                f"{obj.where()} declares kind {obj.kind}, which this tree renders under "
                f"`{RBAC_GROUP}*` everywhere else, with apiVersion {obj.api_version!r}. Either the "
                f"manifest names the wrong API group and will not apply, or this reader failed to "
                f"see its apiVersion and has been leaving RBAC objects out of the population."
            )

    rbac = [o for o in objs if o.is_rbac and o.kind and o.name]
    install = [o for o in rbac if o.on_install_path]
    other = [o for o in rbac if not o.on_install_path]

    # ── Property 3: normalisation is total, exercised, and discriminating ────────────────────
    for obj in rbac:
        if not NAME_RESIDUE.match(residue_of(obj.name)):
            bad.append(
                f"{obj.where()}: {obj.kind}/{obj.name} — after every placeholder syntax this check "
                f"recognises is removed, {residue_of(obj.name)!r} is left, which is not an RFC 1123 "
                f"subdomain. Either the name is invalid, or it is built with a substitution syntax "
                f"this check does not know. The second is the dangerous one: an un-normalised name "
                f"matches no pattern, so the object it names is compared with nothing and passes."
            )

    owners: dict[str, set[str]] = {}
    universal: list[str] = []
    for obj in install:
        glob = pattern_of(obj.name)
        if not discriminates(glob):
            universal.append(f"{obj.where()}: {obj.kind}/{obj.name} -> {glob}")
            continue
        owners.setdefault(glob, set()).add(obj.kind)

    if rbac and not any("*" in p for p in owners):
        bad.append(
            "VACUOUS: no install-path name pattern contains a wildcard, so every comparison below is "
            "an exact-spelling comparison and the normalisation rule is implemented but unexercised. "
            "This check exists because `${AGENT_READER_KSA}-broker-operations` and "
            "`cluster-admin-agent-broker-operations` are the same object under two spellings; with no "
            "glob, it cannot see that, and it reports the tree clean for that reason."
        )

    # ── Property 4: the superset property ────────────────────────────────────────────────────
    #
    # The name is matched raw. `fnmatch` reads metacharacters in the pattern only and matches the
    # subject literally, so an outside name's own templating needs no normalisation to be recognised
    # — it is absorbed by the `*` the install-path placeholder produced.
    examined = 0
    for obj in other:
        matched = [p for p in owners if fnmatch.fnmatchcase(obj.name, p)]
        if not matched:
            continue
        examined += 1
        allowed: set[str] = set()
        for p in matched:
            allowed |= owners[p]
        if obj.kind in allowed:
            continue
        bad.append(
            f"{obj.where()} renders {obj.kind}/{obj.name} — a kind the install path never renders "
            f"under this name. It owns the name (pattern {', '.join(sorted(matched))}) and renders "
            f"only {sorted(allowed)} under it."
        )

    # ── Property 1: the corpus is non-vacuous ────────────────────────────────────────────────
    roots = ", ".join(INSTALL_ROOTS)
    for count, floor, what in (
        (len(install), MIN_INSTALL_RBAC, f"RBAC document(s) under the install path ({roots})"),
        (len(other), MIN_OTHER_RBAC, "RBAC document(s) outside the install path"),
        (len(owners), MIN_PATTERNS, "install-path name pattern(s) that discriminate"),
        (examined, MIN_OWNED, "non-install RBAC object(s) that fall under a name the install path owns"),
    ):
        if count < floor:
            bad.append(
                f"VACUOUS: the scan found {count} {what}, below the floor of {floor}. Either the "
                f"tree lost something load-bearing or this check stopped seeing it — most likely "
                f"the placeholder normalisation, which fails by matching nothing and reports that "
                f"as a clean run. Both are failures; neither is a pass with nothing to inspect "
                f"([[LSN-035]])."
            )

    if universal and not owners:
        bad.append(
            "VACUOUS: every install-path RBAC name globs to a pattern an arbitrary name satisfies, "
            "so no name is owned and nothing can be compared:\n    " + "\n    ".join(sorted(universal))
        )

    return bad


# ──────────────────────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────────────────────


def read_sources() -> dict[str, str]:
    """Every YAML-ish file in the corpus, keyed by repo-relative path.

    Tracked AND new-but-not-ignored (`gitcorpus`, [[LSN-050]]): a template written by the current
    unit and not yet staged is exactly the one no reviewer and no check has read. Not a plain
    `rglob` either — `k8s-operator/scripts/vars.sh` is gitignored because it holds live secrets in
    plaintext, and whatever a check reads it may print in a failure message.
    """
    return {
        rel: text
        for rel, text in read_repo_files(REPO).items()
        if YAMLISH.search(rel)
    }


# ──────────────────────────────────────────────────────────────────────────────
# Negative control
# ──────────────────────────────────────────────────────────────────────────────
#
# A synthetic repository, not a copy of the real one. The real tree is RED today (the defect above is
# real and unfixed), so a control that used it as its clean baseline could not run at all until the
# product is fixed — and a control that cannot run is a control that proves nothing on exactly the
# days it is most needed. The cost of synthesising is written down in the LSN-060 block in the
# docstring, and the `real tree is visible to the scan` arm below covers the part of it that can be
# covered without asserting a finding that the product fix will delete.


RBAC_V1 = "rbac.authorization.k8s.io/v1"


def _doc(kind: str, name: str, namespace: str | None = None, api: str = RBAC_V1) -> str:
    lines = [f"apiVersion: {api}", f"kind: {kind}", "metadata:", f"  name: {name}"]
    if namespace:
        lines.append(f"  namespace: {namespace}")
    lines.append("rules: []")
    return "\n".join(lines)


def _file(objs: list[tuple[str, str]]) -> str:
    return "\n---\n".join(_doc(kind, name) for kind, name in objs) + "\n"


# Filler that clears the floors without participating in any property: ten literal names the install
# path renders and the reference tree mirrors exactly, which is the shape of a correct repository.
FILLER = [("ClusterRole", f"kubeagents-filler-{i}-role") for i in range(10)]

INSTALL_GRANT = "k8s-operator/scripts/broker-operations-grant.yaml.template"
INSTALL_IDENTITY = "k8s-operator/scripts/agent-identity.yaml.template"
GITOPS_GRANT = "examples/gitops-repo/policy/rbac-overlay/broker-operations.yaml"
GITOPS_IDENTITY = "examples/gitops-repo/policy/rbac-overlay/cluster-admin.yaml"
# The synthetic corpus keys are PATHS OF REAL RENDERERS, so a reader can check the fixture against
# the thing it models. This one spelled `50-identity.yaml.tmpl` under `propose-developer-team`: a
# directory the P13-T5 persona rename retired, and a basename that never existed under either name.
# The key is only a dict key -- `clean_corpus` writes its content inline -- so the phantom opened no
# file and matched nothing; it was a realism defect, not a vacuous scan. Repointed at the renderer
# whose two templated `*-broker-operations` bindings the fixture below is a copy of.
TIER_ASSET = (
    "agents/cluster-admin/skills/provision-developer-team/assets/"
    "50-developer-team-identity.yaml.tmpl"
)


def clean_corpus() -> dict[str, str]:
    """The tree as it stood at `12c509d^`: every renderer renders both halves of the grant.

    Four renderers, matching the real shape: the two install templates, a GitOps reference that
    spells the identity out concretely, and a skill asset that leaves the namespace templated.
    """
    return {
        "k8s-operator/config/rbac/filler.yaml": _file(FILLER),
        INSTALL_GRANT: _file(
            [
                ("ClusterRole", "kubeagents-broker-operations"),
                ("Role", "kubeagents-broker-operations"),
            ]
        ),
        INSTALL_IDENTITY: _file(
            [
                ("ClusterRoleBinding", "${AGENT_READER_KSA}-broker-operations"),
                ("RoleBinding", "${AGENT_READER_KSA}-broker-operations"),
            ]
        ),
        "examples/gitops-repo/filler.yaml": _file(FILLER),
        GITOPS_GRANT: _file(
            [
                ("ClusterRole", "kubeagents-broker-operations"),
                ("Role", "kubeagents-broker-operations"),
            ]
        ),
        GITOPS_IDENTITY: _file(
            [
                ("ClusterRoleBinding", "cluster-admin-agent-broker-operations"),
                ("RoleBinding", "cluster-admin-agent-broker-operations"),
            ]
        ),
        TIER_ASSET: _file(
            [
                ("ClusterRoleBinding", "developer-team-@@NAMESPACE@@-broker-operations"),
                ("RoleBinding", "developer-team-@@NAMESPACE@@-broker-operations"),
            ]
        ),
    }


def _retire(corpus: dict[str, str]) -> dict[str, str]:
    """`12c509d` exactly: the install path stops rendering the two namespaced objects."""
    m = dict(corpus)
    m[INSTALL_GRANT] = _file([("ClusterRole", "kubeagents-broker-operations")])
    m[INSTALL_IDENTITY] = _file([("ClusterRoleBinding", "${AGENT_READER_KSA}-broker-operations")])
    return m


def _retire_after_rename(corpus: dict[str, str]) -> dict[str, str]:
    """The same class of defect, under a name that did not exist when this check was written.

    This is the [[LSN-036]] row. A check that hardcoded `broker-operations`, or the six file paths
    the survey found, passes this mutation and catches nothing the class produces after the first
    rename. Nothing in this file may know the string `envelope-operations`.
    """
    m = {rel: text.replace("broker-operations", "envelope-operations") for rel, text in corpus.items()}
    m[INSTALL_GRANT] = _file([("ClusterRole", "kubeagents-envelope-operations")])
    m[INSTALL_IDENTITY] = _file([("ClusterRoleBinding", "${AGENT_READER_KSA}-envelope-operations")])
    return m


def _templated_subject_only(corpus: dict[str, str]) -> dict[str, str]:
    """The retirement, with every concretely-named outside binding deleted.

    Row 1's outside corpus holds a concrete name AND a templated one, so it is caught even if the
    subject-side normalisation is broken. Here only the skill asset is left: if `@@NAMESPACE@@` is
    not turned into something `*-broker-operations` matches, the object is compared with nothing and
    the check calls the tree clean. The skill assets are the renderers that matter — they are what
    an agent APPLIES when it provisions a tier — and they are the ones whose names are templated.
    """
    m = _retire(corpus)
    del m[GITOPS_IDENTITY]
    return m


def _whole_name_placeholder(corpus: dict[str, str]) -> dict[str, str]:
    """The retirement, plus an install object whose ENTIRE name is substituted.

    `${AGENT_ACTOR_KSA}` is exactly this and it is legitimate. It globs to `*`, which owns every name
    in the repository, so without the decoy test its kind set — Role, RoleBinding, ClusterRole,
    ClusterRoleBinding, all four — becomes the allowed set for everything and the check reports zero
    findings on a tree that has them. The row asserts the retirement is STILL caught.
    """
    m = _retire(corpus)
    m["k8s-operator/scripts/actor-grant-platform.yaml.template"] = _file(
        [("Role", "${AGENT_ACTOR_KSA}"), ("RoleBinding", "${AGENT_ACTOR_KSA}")]
    )
    return m


def _unowned_names(corpus: dict[str, str]) -> dict[str, str]:
    """The retirement, plus objects under names the install path does not render at all.

    The accept arm. The GitOps tree legitimately holds sixteen `vaptest-*` objects that exist to be
    REJECTED by a ValidatingAdmissionPolicy, and flagging those would make this check's output
    unreadable — which is how a real gate gets skimmed and then disabled.
    """
    m = _retire(corpus)
    m["examples/gitops-repo/policy/tests/vap_actor_negatives.yaml"] = _file(
        [
            ("Role", "vaptest-actor-journal-delete"),
            ("RoleBinding", "vaptest-actor-journal-delete"),
            ("ClusterRole", "vaptest-actor-wildcard"),
        ]
    )
    return m


def _no_install_corpus(corpus: dict[str, str]) -> dict[str, str]:
    return {rel: text for rel, text in corpus.items() if not rel.startswith(INSTALL_ROOTS)}


def _no_outside_corpus(corpus: dict[str, str]) -> dict[str, str]:
    """Nothing outside the install path renders RBAC at all — the state in which this check has
    nothing to compare the install path against, which is not the same as agreement."""
    return {rel: text for rel, text in corpus.items() if rel.startswith(INSTALL_ROOTS)}


def _few_distinct_install_names(corpus: dict[str, str]) -> dict[str, str]:
    """Plenty of install-path documents, almost no distinct names.

    A document floor alone does not notice this: the population is healthy and the number of names
    that can own anything has collapsed. Both filler sets are collapsed together so the corpus stays
    otherwise correct and only the pattern floor fires.
    """
    m = dict(corpus)
    collapsed = _file([("ClusterRole", "kubeagents-filler-role")] * 10)
    m["k8s-operator/config/rbac/filler.yaml"] = collapsed
    m["examples/gitops-repo/filler.yaml"] = collapsed
    return m


def _every_install_name_universal(corpus: dict[str, str]) -> dict[str, str]:
    """Every install-path name is nothing but a placeholder, so every pattern is dropped."""
    m = dict(corpus)
    m["k8s-operator/config/rbac/filler.yaml"] = _file(
        [("ClusterRole", "${FILLER_KSA}")] * 10
    )
    m[INSTALL_GRANT] = _file([("ClusterRole", "${GRANT}"), ("Role", "${GRANT}")])
    m[INSTALL_IDENTITY] = _file(
        [("ClusterRoleBinding", "${AGENT_READER_KSA}"), ("RoleBinding", "${AGENT_READER_KSA}")]
    )
    return m


def _nothing_is_owned(corpus: dict[str, str]) -> dict[str, str]:
    """Every outside name moved out from under every install pattern.

    The shape a broken normalisation produces: the scan runs, the parse succeeds, every floor but one
    is met, and the comparison examines zero objects. Without the `examined` floor that is a green.
    """
    m = _retire(corpus)
    for rel in list(m):
        if not rel.startswith(INSTALL_ROOTS):
            # Suffixed, not prefixed: a prefix leaves `*-broker-operations` still matching, which
            # would make this row measure nothing.
            m[rel] = re.sub(r"^(  name: .*)$", r"\1-unowned", m[rel], flags=re.MULTILINE)
    return m


def _unreadable_document(corpus: dict[str, str]) -> dict[str, str]:
    """An RBAC document whose name this reader cannot see. Refused, never skipped."""
    m = _retire(corpus)
    m[GITOPS_IDENTITY] = (
        "apiVersion: rbac.authorization.k8s.io/v1\n"
        "kind: RoleBinding\n"
        "metadata: {name: cluster-admin-agent-broker-operations}\n"
    )
    return m


def _wrong_api_group(corpus: dict[str, str]) -> dict[str, str]:
    """A RoleBinding under some other apiVersion — a manifest that will not apply, and the exact
    shape of an apiVersion this reader failed to see."""
    m = _retire(corpus)
    m[GITOPS_IDENTITY] = _doc("RoleBinding", "cluster-admin-agent-broker-operations", api="v1") + "\n"
    return m


def _unrecognised_templating(corpus: dict[str, str]) -> dict[str, str]:
    """Names built with a substitution syntax this check does not know, on BOTH sides.

    Two, because the totality rule runs over one list containing both, and a rule that had silently
    stopped covering the outside half would still be caught by an install-only mutation.
    """
    m = _retire(corpus)
    m[INSTALL_IDENTITY] = _file([("ClusterRoleBinding", "%READER%-broker-operations")])
    m[TIER_ASSET] = _file([("RoleBinding", "<<TIER>>-broker-operations")])
    return m


def _no_wildcard_owners(corpus: dict[str, str]) -> dict[str, str]:
    """Every install-path name spelled out concretely, so no pattern is a glob.

    Not a defect in itself — it is the state in which this check can no longer see the defect it was
    written for, because two spellings of one object stop being one object. It reports that rather
    than reporting a clean tree.
    """
    m = dict(corpus)
    m[INSTALL_IDENTITY] = m[INSTALL_IDENTITY].replace("${AGENT_READER_KSA}", "cluster-admin-agent")
    return m


# (label, rule, mutate, needles). A needle prefixed `!` must NOT appear. Four properties overlap
# here — an unreadable document, a vacuous corpus, an un-normalised name and the superset rule — and
# several of them fire on the same corpus, so "the check went red" is satisfied by whichever is
# evaluated first and establishes nothing about the others ([[LSN-035]]).
CONTROLS: list[tuple[str, str, object, list[str]]] = [
    (
        "the retirement itself: the install path drops the namespaced pair, the others keep it",
        "property 4 — the superset property",
        _retire,
        [
            "renders Role/kubeagents-broker-operations",
            "renders RoleBinding/cluster-admin-agent-broker-operations",
        ],
    ),
    (
        "the same retirement after the object is RENAMED (no roster, [[LSN-036]])",
        "property 4 — the superset property",
        _retire_after_rename,
        [
            "renders Role/kubeagents-envelope-operations",
            "renders RoleBinding/cluster-admin-agent-envelope-operations",
        ],
    ),
    (
        "the only renderer still carrying the retired object is a templated skill asset",
        "property 3 — a glob owner matches a name spelled differently",
        _templated_subject_only,
        [
            "renders RoleBinding/developer-team-@@NAMESPACE@@-broker-operations",
            "!cluster-admin-agent",
        ],
    ),
    (
        "an install object whose whole name is substituted must not own every name",
        "property 3 — a pattern an arbitrary name satisfies owns nothing",
        _whole_name_placeholder,
        [
            "renders Role/kubeagents-broker-operations",
            "renders RoleBinding/cluster-admin-agent-broker-operations",
        ],
    ),
    (
        "objects under names the install path never renders are not findings",
        "property 4 — scoping to install-owned names",
        _unowned_names,
        ["renders Role/kubeagents-broker-operations", "!vaptest-"],
    ),
    (
        "the install path renders no RBAC at all",
        "property 1 — the corpus is non-vacuous",
        _no_install_corpus,
        ["VACUOUS: the scan found 0 RBAC document(s) under the install path"],
    ),
    (
        "nothing outside the install path renders RBAC",
        "property 1 — the corpus is non-vacuous",
        _no_outside_corpus,
        ["VACUOUS: the scan found 0 RBAC document(s) outside the install path"],
    ),
    (
        "many install-path documents, too few distinct names to own anything",
        "property 1 — the pattern floor, which a document floor does not imply",
        _few_distinct_install_names,
        # No count in the needle: the count is a property of the mutation, and a literal that drifts
        # turns a real catch into a MISS somebody then "fixes" by loosening the row ([[LSN-063]]).
        ["install-path name pattern(s) that discriminate, below the floor"],
    ),
    (
        "every install-path name is nothing but a placeholder",
        "property 3 — a dropped pattern set is reported, not treated as agreement",
        _every_install_name_universal,
        ["VACUOUS: every install-path RBAC name globs to a pattern an arbitrary name satisfies"],
    ),
    (
        "normalisation stops matching, so nothing is compared",
        "property 1 — the examined floor",
        _nothing_is_owned,
        ["VACUOUS: the scan found 0 non-install RBAC object(s) that fall under a name"],
    ),
    (
        "an RBAC document this reader cannot parse",
        "property 2 — every RBAC document is read",
        _unreadable_document,
        ["with no `metadata.name` this check can read"],
    ),
    (
        "an RBAC kind under some other apiVersion",
        "property 2 — the API-group cross-check",
        _wrong_api_group,
        ["declares kind RoleBinding, which this tree renders under"],
    ),
    (
        "names built with an unknown substitution syntax, on both sides of the boundary",
        "property 3 — normalisation is total",
        _unrecognised_templating,
        [
            "'%READER%-broker-operations' is left",
            "'<<TIER>>-broker-operations' is left",
        ],
    ),
    (
        "every install-path name spelled out concretely, so no owner pattern is a glob",
        "property 3 — the normalisation rule is exercised by the tree",
        _no_wildcard_owners,
        ["VACUOUS: no install-path name pattern contains a wildcard"],
    ),
]


def negative_control() -> int:
    """Replay every control. No required arguments: a control nothing can invoke uniformly is a
    control whose crash is scored as a pass."""
    base = clean_corpus()

    baseline = check(base)
    if baseline:
        print("  control DEAD: the synthetic baseline is not clean — every row below proves nothing")
        for f in baseline:
            print(f"      {f}")
        return 1
    print(f"  baseline OK  (a synthetic {len(base)}-file tree with both renderers in agreement is clean)")

    failures = 0
    for label, rule, mutate, needles in CONTROLS:
        mutated = mutate(base)  # type: ignore[operator]
        if mutated == base:
            # NOT a MISS. Nothing was injected, so nothing was evaluated, and the two verdicts point
            # at opposite repairs ([[LSN-063]]).
            print(f"  BROKEN       {label}")
            print("      the mutation did not change its input; the row evaluated the clean tree")
            failures += 1
            continue

        try:
            found = check(mutated)
        except Exception as exc:  # noqa: BLE001 — an unscoreable row is a finding, never a pass
            print(f"  BROKEN       {label}")
            print(f"      check() raised {type(exc).__name__}: {exc}")
            failures += 1
            continue

        missing = [n for n in needles if not n.startswith("!") and not any(n in f for f in found)]
        leaked = [n[1:] for n in needles if n.startswith("!") and any(n[1:] in f for f in found)]
        if missing or leaked:
            print(f"  MISS         {label}")
            print(f"      targets: {rule}")
            if missing:
                print(f"      no finding mentions {missing!r}")
            if leaked:
                print(f"      a finding mentions {leaked!r}, which this mutation did not break")
            if not found:
                print("      the check reported nothing at all")
            failures += 1
        else:
            print(f"  ok           {label}")

    # The one arm that touches the real repository. It asserts only that the scan can SEE the tree —
    # not which findings it produces — so it stays true after the product defect is fixed and it goes
    # red the day the enumeration, the filter or the parser stops reaching the real files.
    real = check(read_sources())
    vacuous = [f for f in real if f.startswith("VACUOUS:")]
    if vacuous:
        print("  MISS         the real tree is visible to the scan")
        print("      targets: property 1 — the corpus is non-vacuous, over the REAL corpus")
        for f in vacuous:
            print(f"      {f}")
        failures += 1
    else:
        print(
            f"  ok           the real tree is visible to the scan "
            f"({len(real)} live finding(s), no VACUOUS)"
        )

    total = len(CONTROLS) + 1
    print(f"\n{total - failures}/{total} controls fire for their own property.")
    return 1 if failures else 0


# ──────────────────────────────────────────────────────────────────────────────


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    sources = read_sources()
    if not sources:
        print(
            f"FAIL: no YAML found under {REPO}. REPO is derived from this file's own location, so a "
            f"copy run from outside the repository resolves it to a directory that measures nothing "
            f"([[LSN-035]])."
        )
        return 1

    violations = check(sources)
    if violations:
        print("Retired object shapes still rendered outside the install path:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nA manifest under examples/ is a design document with YAML syntax; a manifest under\n"
            "agents/*/skills/*/assets/ is what an agent APPLIES when it provisions a tier. Retiring\n"
            "an object from k8s-operator/scripts retires it from one renderer out of several."
        )
        return 1

    objs = [o for o in parse(sources) if o.is_rbac and o.kind and o.name]
    install = [o for o in objs if o.on_install_path]
    print(
        f"Retired object shapes: OK — {len(install)} RBAC document(s) on the install path and "
        f"{len(objs) - len(install)} off it; every kind rendered outside the install path under a "
        f"name the install path owns is also rendered by the install path."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
