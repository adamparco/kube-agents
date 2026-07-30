#!/usr/bin/env python3
"""Identity install-path reachability — V-CMP-007 (Phase 9, P9-T7d-5).

LSN-039. `install-path-wired.py` (V-CMP-001) asserts that every numbered step script is invoked by
its driver, and it is green, and it has been green the whole time. It walks the SCRIPT graph. It
would report exactly the same green on a repository whose steps run perfectly and apply none of the
security manifests -- which is the repository this one was, for eight phases:

  * `kubeagents-platform-agent`, the platform reader ServiceAccount, was REFERENCED by common.sh, by
    provision_08 and by the operator's own examples, and CREATED BY NOTHING. It exists on the live
    cluster only because somebody ran `kubectl create sa` by hand: its last-applied-configuration is
    a bare SA with no labels, and the Workload Identity annotation is not even in that.
  * The actor identity of 06 §2, and the broker-operations grant of 06 §2.2.1 that gives it its only
    authority, existed solely under `examples/gitops-repo/` -- a reference tree that no install path
    reads. A freshly provisioned cluster would have run a broker that cannot create the TokenReview
    it authenticates its own caller with, and fails closed on step 1 of its own pipeline.
  * Every reader ServiceAccount the install path did create carried `kube-agents/tier` alone, while
    both `vap-agent-pod-hardening` and `vap-agent-readonly`'s actor arm select on `kube-agents/role`.
    A policy whose selector matches nothing reports the same green as a policy that passes.

The tell was in V-CMP-001's own docstring: "Deliberately NOT checked: that a step does the right
thing. This is reachability only." That sentence is true and the claim it narrows to is one link
short. A step is reachable from a driver; a TEMPLATE has to be reachable from a step; a manifest's
identities have to be reachable from a template. This check walks the other three links.

Properties (all must hold for exit 0):

  1. **Every `*.yaml.template` under the scripts tree is read by the install path** -- named by a
     numbered step, or by a common.sh function transitively called by one. A template nobody renders
     is the LSN-007 defect one layer down from the one V-CMP-001 catches.

  2. **Every manifest-emitting shell function is reachable from a numbered step.** "Manifest-
     emitting" is discovered, not listed (LSN-036): any function in common.sh whose body names a
     template or runs `kubectl apply`/`kubectl delete`. `render_agent_identity` called only by
     `apply_agent_identity` called only by another orphan is still an orphan, so reachability is
     computed as a transitive closure rather than as "is it mentioned in a step".

  3. **Every ServiceAccount the install path creates carries both `kube-agents/tier` and
     `kube-agents/role`.** These two labels are the entire selector surface of both admission
     policies. This is the property that was false on the live cluster while everything was green.

  4. **Every RBAC subject naming a ServiceAccount names one the install path creates.** A
     RoleBinding whose subject does not exist is accepted by the API server and grants nothing --
     there is no apply-time error, only an authorization denial later, in the broker, at runtime.

  5. **Every `roleRef` names a Role/ClusterRole the install path creates**, or a Kubernetes built-in
     named in BUILTIN_ROLES with the reason it is trusted. Same silent-failure shape as 4: a
     roleRef to a nonexistent role is a legal object that confers nothing.

  6. **The two implementations of the actor name agree, and the floors are non-vacuous.** The actor
     ServiceAccount name is derived twice -- by `actorServiceAccountName` in Go, which decides what
     the broker looks up, and by `actor_service_account_name` in bash, which decides what the
     install path creates. 06 §2.2.1 forbids the CR from naming its own actor, so there is no third
     place to reconcile them and no runtime error if they drift: the broker resolves a
     ServiceAccount that does not exist and comes up BrokerReady=false. The floors are FAILs rather
     than vacuous passes (LSN-035) -- a refactor that moves the templates elsewhere must break this
     check loudly rather than pass it with nothing to inspect.

Negative control (`--negative-control`): each property is re-run against the real sources with one
mutation applied in memory, and must fail. A check that cannot fail is not evidence (09 §6, V-MET-014).

Usage:
    python3 dev/tests/identity-has-install-path.py
    python3 dev/tests/identity-has-install-path.py --negative-control

Exit 0 = every identity the broker resolves is created by something the install path runs;
1 = violations. Stdlib only, no cluster, no PyYAML.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gitcorpus import repo_files  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

SCRIPTS = "k8s-operator/scripts"
COMMON = f"{SCRIPTS}/common.sh"
GO_ACTOR_NAME = "k8s-operator/internal/controller/broker_manifests.go"

# The install path's entry points. Deliberately NOT every *.sh in the tree: `live_refresh.sh` and the
# dev-cluster helpers are convenience wrappers, and a template reachable only from one of those is a
# template a real `provision.sh` run never renders -- which is the finding, not an exemption.
ROOT = re.compile(r"^(provision|teardown)(_\d+_[a-z0-9_]+)?\.sh$")

# The two labels both ValidatingAdmissionPolicies select on (08 §2.5).
REQUIRED_SA_LABELS = ("kube-agents/tier", "kube-agents/role")

# Roles the install path may bind to without creating. Each entry carries WHY it is trusted, because
# "it is built in" is the argument that would also admit `cluster-admin`.
BUILTIN_ROLES: dict[str, str] = {
    # Nothing yet. The broker-operations grant is first-party and rendered from
    # broker-operations-grant.yaml.template; the explorer roles are first-party too. If an entry
    # ever appears here it needs a sentence, and a reviewer who reads the sentence.
}

# Non-vacuity floors. Each is comfortably below the real count and comfortably above zero.
MIN_TEMPLATES = 8
MIN_SERVICE_ACCOUNTS = 2
MIN_SUBJECT_REFS = 2
MIN_ROLE_REFS = 2
MIN_MANIFEST_FUNCS = 8

# `printf '%s-%s-actor\n' "$1" "$2"` and `fmt.Sprintf("%s-%s-actor", tier, leaf)`.
BASH_ACTOR_FMT = re.compile(r"actor_service_account_name\(\)[^\n]*\n\s*printf\s+'([^']+)'")
GO_ACTOR_FMT = re.compile(r'name\s*:?=\s*fmt\.Sprintf\("([^"]+)",\s*tier,\s*leaf\)')

SHELL_FUNC = re.compile(r"^([a-z_][a-z0-9_]*)\(\)\s*\{", re.MULTILINE)
TEMPLATE_REF = re.compile(r"([a-z0-9-]+\.yaml\.template)")
APPLIES_MANIFEST = re.compile(r"kubectl\s+(apply|delete)\b")

# `apply_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf> [gsa]`, as invoked by a step.
IDENTITY_CALL = re.compile(r"^\s*apply_agent_identity\s+(?P<args>.+?)(?:\s*\|\|.*)?$", re.MULTILINE)

# Every tier that runs an agent pod, and therefore a broker, and therefore needs both identities.
# Read off `agents/` rather than written down here, so a fourth tier is covered the day it lands
# (LSN-036) — the directory IS the tier list, and `make validate` already polices its shape.
AGENTS_DIR = "agents"


class ManifestSyntaxError(Exception):
    """Raised when a document cannot be read.

    Loud rather than skipped, on purpose. A parser that shrugs at an object it cannot understand
    reports a smaller creation set than the tree creates, and a smaller creation set makes
    properties 4 and 5 fail for the wrong reason -- or, worse, makes property 3 pass because the
    ServiceAccount it should have inspected was never in the list.
    """


# ──────────────────────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────────────────────


def read_sources() -> dict[str, str]:
    """Every file this check reads, keyed by repo-relative path.

    Enumerated with `git ls-files --cached --others --exclude-standard`: tracked files PLUS
    untracked ones that are not ignored.

    Not tracked-only, because a brand-new template is untracked at exactly the moment this check
    most needs to see it -- the pre-commit run, on the unit that added it.

    Not a plain rglob either. `k8s-operator/scripts/vars.sh` is gitignored precisely because it
    holds live secrets in plaintext, and whatever this check reads it may print in a failure
    message.
    """
    wanted = {}
    for rel in repo_files(REPO):
        if (
            rel == GO_ACTOR_NAME
            or (rel.startswith(f"{SCRIPTS}/") and (rel.endswith(".sh") or rel.endswith(".yaml.template")))
            or (rel.startswith(f"{AGENTS_DIR}/") and rel.count("/") == 2 and rel.endswith("/config.yaml"))
        ):
            path = REPO / rel
            if path.is_file():
                wanted[rel] = path.read_text(encoding="utf-8")
    return wanted


# ──────────────────────────────────────────────────────────────────────────────
# A narrow YAML reader
# ──────────────────────────────────────────────────────────────────────────────
#
# PyYAML is not available here and `dev/tests/yamlsubset.py` rejects flow collections, which is
# exactly what every `verbs: ["get", ...]` line in an RBAC file is. What this needs from a manifest
# is small and fixed: kind, metadata.name/namespace/labels, roleRef, and the subjects list. So it
# reads those five things by indentation and refuses anything it does not recognise, rather than
# pretending to be a YAML parser.


def _strip(text: str) -> list[str]:
    """Drop whole-line comments and blank lines, keeping indentation."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        out.append(line.rstrip())
    return out


def _documents(text: str) -> list[list[str]]:
    docs, current = [], []
    for line in _strip(text):
        if line == "---":
            if current:
                docs.append(current)
            current = []
            continue
        current.append(line)
    if current:
        docs.append(current)
    return docs


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], key: str, at: int) -> list[str]:
    """The lines nested under `<key>:` appearing at indentation `at`."""
    prefix = " " * at + key + ":"
    for i, line in enumerate(lines):
        if line == prefix or line.startswith(prefix + " "):
            body = []
            for nxt in lines[i + 1 :]:
                if _indent(nxt) <= at:
                    break
                body.append(nxt)
            return body
    return []


def _scalar(lines: list[str], key: str, at: int) -> str | None:
    prefix = " " * at + key + ":"
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'") or None
    return None


def _pairs(lines: list[str], at: int) -> dict[str, str]:
    out = {}
    for line in lines:
        if _indent(line) != at or ":" not in line:
            continue
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Obj:
    """One Kubernetes object, reduced to the five things this check reasons about."""

    def __init__(self, source: str, lines: list[str]) -> None:
        self.source = source
        self.kind = _scalar(lines, "kind", 0)
        meta = _block(lines, "metadata", 0)
        self.name = _scalar(meta, "name", 2)
        self.namespace = _scalar(meta, "namespace", 2)
        self.labels = _pairs(_block(meta, "labels", 2), 4)

        role_ref = _block(lines, "roleRef", 0)
        self.role_ref = (
            (_scalar(role_ref, "kind", 2), _scalar(role_ref, "name", 2)) if role_ref else None
        )

        self.subjects: list[tuple[str | None, str | None]] = []
        for item in _block(lines, "subjects", 0):
            stripped = item.strip()
            if stripped.startswith("- "):
                self.subjects.append([None, None])  # type: ignore[arg-type]
                stripped = stripped[2:]
            elif not self.subjects:
                continue
            k, _, v = stripped.partition(":")
            v = v.strip().strip('"').strip("'")
            if k.strip() == "kind":
                self.subjects[-1][0] = v  # type: ignore[index]
            elif k.strip() == "name":
                self.subjects[-1][1] = v  # type: ignore[index]
        self.subjects = [tuple(s) for s in self.subjects]  # type: ignore[misc]

    def where(self) -> str:
        ns = f"{self.namespace}/" if self.namespace else ""
        return f"{self.source}: {self.kind} {ns}{self.name}"


def parse_manifests(sources: dict[str, str]) -> list[Obj]:
    objs = []
    for rel, text in sorted(sources.items()):
        if not rel.endswith(".yaml.template"):
            continue
        for doc in _documents(text):
            obj = Obj(rel, doc)
            if obj.kind is None:
                raise ManifestSyntaxError(f"{rel}: a document has no top-level `kind:`")
            if obj.name is None:
                raise ManifestSyntaxError(f"{rel}: {obj.kind} has no metadata.name")
            objs.append(obj)
    return objs


# ──────────────────────────────────────────────────────────────────────────────
# The shell call graph
# ──────────────────────────────────────────────────────────────────────────────


def _functions(common: str) -> dict[str, str]:
    """{name: body} for every function defined in common.sh, by brace-column-zero."""
    starts = [(m.group(1), m.start()) for m in SHELL_FUNC.finditer(common)]
    bodies = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(common)
        bodies[name] = common[start:end]
    return bodies


def reachable_functions(sources: dict[str, str]) -> tuple[set[str], dict[str, str], list[str]]:
    """Functions transitively invoked by a numbered step, plus the bodies and the root texts."""
    funcs = _functions(sources.get(COMMON, ""))
    roots = [
        text
        for rel, text in sorted(sources.items())
        if rel.startswith(f"{SCRIPTS}/") and ROOT.match(rel.rsplit("/", 1)[-1])
    ]

    reached: set[str] = set()
    frontier = list(roots)
    while frontier:
        text = frontier.pop()
        for name, body in funcs.items():
            if name in reached:
                continue
            # Word-boundary match, so `apply_tenant_quota` does not light up `apply_tenant_quota_v2`
            # and a function is not counted as calling itself via its own definition line.
            probe = text if name not in funcs or text not in (funcs.get(name),) else ""
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", probe.replace(f"{name}() {{", "")):
                reached.add(name)
                frontier.append(body)
    return reached, funcs, roots


def identity_call_sites(roots: list[str]) -> list[list[str]]:
    """The positional arguments of every `apply_agent_identity` invocation in a numbered step.

    This is the bridge the manifest graph cannot see on its own. `agent-identity.yaml.template`
    creates `${AGENT_READER_KSA}`, while the tier templates beside it bind `${CLUSTER_ADMIN_KSA_NAME}`
    — the same ServiceAccount under a different variable name, joined only at this call site. A
    check that compared the two spellings would report a dangling subject; a check that ignored the
    difference would accept any spelling at all. Reading the call site instead gets both: the alias
    is derived from the wiring that actually creates it, and a tier whose identity is never applied
    contributes no alias and fails property 4 exactly as it should.
    """
    calls = []
    for text in roots:
        joined = re.sub(r"\\\n\s*", " ", text)  # honour backslash continuations
        for m in IDENTITY_CALL.finditer(joined):
            try:
                import shlex

                args = shlex.split(m.group("args"), comments=True)
            except ValueError:
                continue
            if args:
                calls.append(args)
    return calls


def manifest_functions(funcs: dict[str, str]) -> set[str]:
    """Discovered, not listed (LSN-036): a function that renders a template or applies a manifest."""
    return {
        name
        for name, body in funcs.items()
        if TEMPLATE_REF.search(body) or APPLIES_MANIFEST.search(body)
    }


# ──────────────────────────────────────────────────────────────────────────────
# The properties
# ──────────────────────────────────────────────────────────────────────────────


def check(sources: dict[str, str]) -> list[str]:
    bad: list[str] = []
    objs = parse_manifests(sources)
    reached, funcs, roots = reachable_functions(sources)
    emitters = manifest_functions(funcs)

    # Text the install path actually executes: the numbered steps, plus the bodies of every function
    # they can reach. A template named only inside an orphaned function is not read by anything.
    executed = "\n".join(roots) + "\n" + "\n".join(funcs[n] for n in sorted(reached))

    templates = sorted(r for r in sources if r.endswith(".yaml.template"))

    # 1. Template reachability.
    for rel in templates:
        base = rel.rsplit("/", 1)[-1]
        if base not in executed:
            bad.append(
                f"{rel} is never named by a numbered provisioning step, nor by any common.sh "
                f"function one can reach. It renders to nothing on a real install — whatever it "
                f"declares is not applied (LSN-007/LSN-039)."
            )

    # 2. Emitter reachability.
    for name in sorted(emitters - reached):
        bad.append(
            f"{COMMON}: `{name}` renders or applies a manifest and no numbered step reaches it, "
            f"directly or through another function. Wire it into a step or delete it — a renderer "
            f"nobody calls is the same defect as a step nobody invokes."
        )

    # 3/4/5. The manifest graph.
    created_sas = {(o.namespace, o.name) for o in objs if o.kind == "ServiceAccount"}
    created_sa_names = {o.name for o in objs if o.kind == "ServiceAccount"}
    created_roles = {(o.kind, o.name) for o in objs if o.kind in ("Role", "ClusterRole")}

    # Each call site instantiates the identity template's symbolic names with this tier's real ones.
    calls = identity_call_sites(roots)
    for args in calls:
        if len(args) >= 3:
            created_sa_names.add(args[2])
        if len(args) >= 4:
            created_sa_names.add(f"{args[0]}-{args[3]}-actor")

    subject_refs = 0
    role_refs = 0

    for obj in objs:
        if obj.kind == "ServiceAccount":
            missing = [k for k in REQUIRED_SA_LABELS if k not in obj.labels]
            if missing:
                bad.append(
                    f"{obj.where()} is missing {', '.join(missing)}. Both admission policies select "
                    f"on these two labels: vap-agent-pod-hardening decides whether this identity's "
                    f"pod may mount an actor token, and vap-agent-readonly's write carve-out applies "
                    f"only to `kube-agents/role: actor`. An unlabelled ServiceAccount is not denied "
                    f"— it is invisible to the rules written to bound it, which reports as green."
                )

        for kind, name in obj.subjects:
            if kind != "ServiceAccount":
                continue
            subject_refs += 1
            if name not in created_sa_names:
                bad.append(
                    f"{obj.where()} binds ServiceAccount '{name}', which no install-path manifest "
                    f"creates. The API server accepts a binding whose subject does not exist and "
                    f"grants nothing — there is no apply-time error, only an authorization denial "
                    f"later, inside the broker, at runtime."
                )

        if obj.role_ref:
            kind, name = obj.role_ref
            role_refs += 1
            if (kind, name) not in created_roles and name not in BUILTIN_ROLES:
                bad.append(
                    f"{obj.where()} has roleRef {kind}/{name}, which no install-path manifest "
                    f"creates and which is not in BUILTIN_ROLES. Same silent shape as a missing "
                    f"subject: a legal object that confers nothing."
                )

    # 7. Every tier gets an identity. This is the assertion that was false: `agents/` has carried
    # three tiers since phase 3, and until P9-T7d-5 the install path applied an identity for none of
    # them — two by hand inside their tier templates, and the platform one not at all.
    tiers = sorted(
        rel.split("/")[1] for rel in sources if rel.startswith(f"{AGENTS_DIR}/") and rel.endswith("/config.yaml")
    )
    applied_for = {args[0] for args in calls if args}
    for tier in tiers:
        if tier not in applied_for:
            bad.append(
                f"tier '{tier}' has {AGENTS_DIR}/{tier}/config.yaml and no `apply_agent_identity "
                f"{tier} ...` in any numbered step. Its reader and actor ServiceAccounts are created "
                f"by nothing, so the controller references a KSA that does not exist and the broker "
                f"authenticates its caller with a TokenReview it has no permission to create."
            )
    if not tiers:
        bad.append(
            f"VACUOUS: no {AGENTS_DIR}/*/config.yaml found, so property 7 has no tiers to check "
            f"and would pass on an install path that applies no identity at all (LSN-035)."
        )

    # 6a. The two actor-name implementations.
    bash_fmt = BASH_ACTOR_FMT.search(sources.get(COMMON, ""))
    go_fmt = GO_ACTOR_FMT.search(sources.get(GO_ACTOR_NAME, ""))
    if not bash_fmt:
        bad.append(
            f"{COMMON}: `actor_service_account_name` is gone or no longer a one-line printf. It is "
            f"the install path's only copy of the actor name rule; without it this check cannot "
            f"compare the two (LSN-035)."
        )
    elif not go_fmt:
        bad.append(
            f"{GO_ACTOR_NAME}: `fmt.Sprintf(..., tier, leaf)` not found. The controller's actor name "
            f"derivation moved; the bash copy is now comparing against nothing."
        )
    else:
        bash_shape = bash_fmt.group(1).replace("\\n", "")
        if bash_shape != go_fmt.group(1):
            bad.append(
                f"Actor ServiceAccount name derived two different ways: Go renders "
                f"'{go_fmt.group(1)}' and {COMMON} renders '{bash_shape}'. 06 §2.2.1 forbids the CR "
                f"from naming its own actor, so there is no third place these get reconciled — the "
                f"broker looks up a ServiceAccount the install path never created and comes up "
                f"BrokerReady=false."
            )

    # 6b. Floors. Each is a FAIL, never a skip.
    for count, floor, what in (
        (len(templates), MIN_TEMPLATES, f"templates under {SCRIPTS}/"),
        (len(created_sas), MIN_SERVICE_ACCOUNTS, "ServiceAccounts created by the install path"),
        (subject_refs, MIN_SUBJECT_REFS, "RBAC ServiceAccount subject references"),
        (role_refs, MIN_ROLE_REFS, "roleRefs"),
        (len(emitters), MIN_MANIFEST_FUNCS, "manifest-emitting shell functions"),
    ):
        if count < floor:
            bad.append(
                f"VACUOUS: found {count} {what}, below the floor of {floor}. Either the install "
                f"path lost something load-bearing or this check is looking in the wrong place. "
                f"Both are failures; neither is a pass with nothing to inspect (LSN-035)."
            )

    return bad


# ──────────────────────────────────────────────────────────────────────────────
# Negative control
# ──────────────────────────────────────────────────────────────────────────────


def _drop_label(sources: dict[str, str]) -> dict[str, str]:
    m = dict(sources)
    key = f"{SCRIPTS}/agent-identity.yaml.template"
    m[key] = m[key].replace("    kube-agents/role: reader\n", "", 1)
    return m


def _dangling_subject(sources: dict[str, str]) -> dict[str, str]:
    m = dict(sources)
    key = f"{SCRIPTS}/agent-identity.yaml.template"
    m[key] = m[key].replace(
        "    name: ${AGENT_ACTOR_KSA}\n    namespace: ${AGENT_NAMESPACE}",
        "    name: some-actor-nobody-creates\n    namespace: ${AGENT_NAMESPACE}",
        1,
    )
    return m


def _dangling_role_ref(sources: dict[str, str]) -> dict[str, str]:
    m = dict(sources)
    key = f"{SCRIPTS}/agent-identity.yaml.template"
    m[key] = m[key].replace("  name: kubeagents-broker-operations", "  name: kubeagents-legacy-broker", 1)
    return m


def _orphan_template(sources: dict[str, str]) -> dict[str, str]:
    """The exact LSN-039 shape: the manifest is still there, nothing renders it any more."""
    m = dict(sources)
    m[COMMON] = m[COMMON].replace("broker-operations-grant.yaml.template", "unused-grant.yaml.bak")
    return m


def _orphan_emitter(sources: dict[str, str]) -> dict[str, str]:
    """Every step stops calling apply_agent_identity; three renderers go dark behind it."""
    m = dict(sources)
    for rel in list(m):
        if rel.startswith(f"{SCRIPTS}/") and ROOT.match(rel.rsplit("/", 1)[-1]):
            m[rel] = re.sub(r"\bapply_agent_identity\b", "true # apply_agent_identity", m[rel])
    return m


def _tier_without_identity(sources: dict[str, str]) -> dict[str, str]:
    """LSN-039 itself, reproduced exactly: one tier's identity stops being applied and the other
    two keep working, so nothing else in the repository notices."""
    m = dict(sources)
    key = f"{SCRIPTS}/provision_08_deploy_platform_agent.sh"
    m[key] = re.sub(r"^(\s*)apply_agent_identity\b", r"\1true # apply_agent_identity", m[key], flags=re.MULTILINE)
    return m


def _actor_name_drift(sources: dict[str, str]) -> dict[str, str]:
    m = dict(sources)
    m[COMMON] = m[COMMON].replace("'%s-%s-actor\\n'", "'%s-%s-writer\\n'", 1)
    return m


def _no_templates(sources: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in sources.items() if not k.endswith(".yaml.template")}


# (label, mutate, signals). Each signal is a substring the findings must contain; one prefixed `!`
# is a substring they must NOT contain. Seven properties overlap here — a dangling subject, a
# dangling roleRef, a missing label, an unrendered template, an unreachable emitter, an unserved
# tier, a name that drifts — and several of them fire on the same manifest, so "the check went red"
# is satisfied by whichever is evaluated first and establishes nothing about the other six
# ([[LSN-035]]). The `!` form is what makes the two apply_agent_identity controls distinguishable:
# dropping ALL the calls must name all three tiers, dropping ONE must name only that tier, and
# without the negative arm the narrower mutation is fully satisfied by the broader one's output.
CONTROLS: list[tuple[str, object, list[str]]] = [
    ("a reader ServiceAccount loses kube-agents/role", _drop_label,
     ["is missing kube-agents/role"]),
    ("a RoleBinding names a ServiceAccount nobody creates", _dangling_subject,
     ["binds ServiceAccount 'some-actor-nobody-creates', which no install-path manifest creates"]),
    ("a roleRef names a Role nobody creates", _dangling_role_ref,
     ["has roleRef ClusterRole/kubeagents-legacy-broker, which no install-path manifest creates"]),
    ("the grant template is still there and nothing renders it", _orphan_template,
     ["broker-operations-grant.yaml.template is never named by a numbered provisioning step"]),
    ("no step calls apply_agent_identity any more", _orphan_emitter,
     ["`apply_agent_identity platform ...`",
      "`apply_agent_identity cluster-admin ...`",
      "`apply_agent_identity developer-team ...`"]),
    ("one tier's identity stops being applied and the others keep working", _tier_without_identity,
     ["`apply_agent_identity platform ...`",
      "!`apply_agent_identity cluster-admin ...`",
      "!`apply_agent_identity developer-team ...`"]),
    ("the bash actor name drifts from the Go one", _actor_name_drift,
     ["Actor ServiceAccount name derived two different ways",
      "Go renders '%s-%s-actor'", "common.sh renders '%s-%s-writer'"]),
    ("every template disappears (no vacuous pass)", _no_templates,
     ["VACUOUS: found 0 templates under"]),
]


def negative_control(sources: dict[str, str] | None = None) -> int:
    """Run every control. `sources` is optional so this is invocable as `negative_control()`."""
    if sources is None:
        sources = read_sources()

    if check(sources):
        print("  control DEAD: the real tree does not pass — the controls below prove nothing")
        return 1
    print("  baseline OK  (the real tree passes)")

    failures = 0
    for label, mutate, signals in CONTROLS:
        try:
            found = check(mutate(sources))  # type: ignore[operator]
        except ManifestSyntaxError as exc:
            # A manifest this cannot parse IS a detection — but it is not necessarily a detection of
            # the property the mutation was about, so it goes through the same signal test as any
            # other finding rather than counting as a free pass ([[LSN-038]]).
            found = [f"ManifestSyntaxError: {exc}"]

        if not found:
            print(f"  control DEAD (silent): {label}")
            failures += 1
            continue

        missing = [s for s in signals if not s.startswith("!") and not any(s in f for f in found)]
        leaked = [s[1:] for s in signals if s.startswith("!") and any(s[1:] in f for f in found)]
        if missing or leaked:
            print(f"  control DEAD (fires, but not for its property): {label}")
            if missing:
                print(f"      no finding mentions {missing!r}")
            if leaked:
                print(f"      a finding mentions {leaked!r}, which this mutation did not break")
            failures += 1
        else:
            print(f"  control OK   (fires, naming its property): {label}")

    print(f"\n{len(CONTROLS) - failures}/{len(CONTROLS)} negative controls fire for their own property.")
    return 1 if failures else 0


def main() -> int:
    sources = read_sources()

    if "--negative-control" in sys.argv:
        return negative_control(sources)

    try:
        violations = check(sources)
    except ManifestSyntaxError as exc:
        print(f"Unreadable install-path manifest: {exc}")
        return 1

    if violations:
        print("Identity install-path violations:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nA manifest under examples/ is a design document with YAML syntax. Every identity the\n"
            "broker resolves by name must be created by something `provision.sh` actually runs."
        )
        return 1

    objs = parse_manifests(sources)
    reached, funcs, _ = reachable_functions(sources)
    print(
        f"Identity install path: OK — {len([o for o in objs if o.kind == 'ServiceAccount'])} "
        f"ServiceAccounts created and labelled, every RBAC subject and roleRef resolves, "
        f"{len(manifest_functions(funcs) & reached)} manifest-emitting functions all reachable from "
        f"a numbered step, and the actor name agrees between Go and bash."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
