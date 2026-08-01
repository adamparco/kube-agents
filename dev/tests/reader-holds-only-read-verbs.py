#!/usr/bin/env python3
"""V-CTN-004 (L0 arm): no reader identity in the tree is granted a verb outside get/list/watch.

09 §6.1 states it as *"Reader SA holds **no** write verb on anything, universally"*, sourced to 03
§11 (*"`auth can-i create|update|delete <any>` as any reader SA returns no, universally"*) and 08 §7
(*"Sweep … across `create|update|patch|delete|deletecollection|escalate|bind|impersonate` × a
representative resource set … every answer is `no`"*). The L2 arm asks the live authorizer. This is
the arm that runs before anything is applied, on the manifests themselves, so a reader grant that
would widen the boundary is a red PR rather than a red cluster.

THE ALLOW-LIST IS THE PROPERTY, NOT AN IMPLEMENTATION DETAIL. 09 §11.4 is a recorded incident from
this repository -- *"a write-verb deny-list admitted `impersonate`, which is equivalent to
cluster-admin"* -- and the ruling it produced is in the phase-0 ledger, dated 2026-07-23: *"VAP
read-only ceiling expressed as a read-verb allow-list (verbs ⊆ get/list/watch), not a write-verb
deny-list … Allow-list also closes any future non-read verb."* So this check never enumerates
forbidden verbs. It enumerates the three a reader may hold and fails on everything else. That is why
`impersonate`, `escalate`, `bind`, `approve`, `sign`, `use`, `deletecollection`, `*`, and whatever
verb Kubernetes adds next are all caught here **by construction and not by name** -- there is no
list to forget to update, which is precisely the maintenance failure 09 §11.4 is about. Only three
verbs are named anywhere below, and they are the three that are allowed.

WHY THOSE THREE. The read set is not this check's invention: it is `vap-agent-readonly`'s validation
1, `r.verbs.all(v, v in ['get','list','watch'])`, applied to every agent-labelled
`Role`/`ClusterRole` that is not explicitly `role: actor`. This file is that expression's static
shadow, run on the manifests before admission ever sees them, and it holds the same three so that a
manifest which would be rejected at apply time is rejected at review time by the same rule rather
than by a second opinion. No reader in the tree renders a fourth verb, and the ones that get
proposed -- `proxy`, `use`, `impersonate` -- are not reads.

WHAT THIS CHECK IS NOT. `actor-grant-single-sourced.py` (V-BRK-013) holds the **actor** objects to 06
§2.2/§2.2.1 and never looks at a reader's verbs; `identity-has-install-path.py` (V-CMP-007) proves
that subjects and roleRefs RESOLVE and never looks at a verb at all. This is the reader half, and it
is the only thing in the tree that reads one. It also closes, statically, two holes the phase-0
ledger recorded as deferred to a webhook that does not exist yet -- *"Binding / `aggregationRule` /
non-tier-labeled RBAC checks deferred to the cross-object child⊆parent admission webhook … held by
human review + CODEOWNERS meanwhile"* -- see properties 5 and 6.

Seven properties.

  1. NON-VACUITY, AND THE CLASSIFIER DISCRIMINATES. Every file named in CORPUS exists and parses;
     the corpus yields at least MIN_READER_ROLES reader roles and MIN_ACTOR_ROLES actor ones, at
     least MIN_READER_SAS reader ServiceAccounts, and every file in ANCHORS contributes a reader
     role of its own. This matters more here than in most checks: properties 2, 3 and 6 are
     ABSENCE assertions over the reader set, and an absence assertion over an empty set is the
     greenest thing in this repository. A classifier that answered `actor` to everything would make
     the whole file pass, so it is required to answer `reader` at least nine times and `actor` at
     least three over the same corpus -- a split proved in both directions, not assumed.
  2. EVERY VERB IN EVERY READER RULE IS IN THE READ ALLOW-LIST. `{get, list, watch}`. A rule that
     declares no verbs at all is also a finding: the API server rejects `verbs: []` outright, so it
     can only ever be a manifest that fails to apply, and `vap-agent-readonly` would have admitted
     it -- validation 1 short-circuits on `!has(r.verbs)` before the allow-list is evaluated.
  3. THE WILDCARD IS REJECTED PER AXIS, AND EACH AXIS IS ITS OWN FINDING.
       * `verbs: ["*"]` -- always. Property 2 catches it too, since `*` is not one of the three;
         this states it separately because a wildcard verb is not "an unrecognised verb", it is
         every verb including the four 03 §3.3 forbids, and the message should say so.
       * `apiGroups: ["*"]` -- always. The group axis is the reader's outer bound. A wildcard there
         reaches every CRD group installed now or later, including `kubeagents.x-k8s.io` (the
         agent's own CR, the ActionRecords of 03 §3.3 rule 4, the FleetFreeze) and
         `admissionregistration.k8s.io` (the policies that bound it) -- and it does so silently, at
         the moment somebody installs an operator, with no edit to this file.
       * `resources: ["*"]` -- a finding only inside a rule whose apiGroups are NOT a closed
         enumeration. This asymmetry is deliberate and is the one place this check declines to be
         maximal. `resources: ["*"]` under enumerated groups is what every reader role in the tree
         renders, what 06 §2.2's platform read template requires, and what the VAP corpus pins as
         `EXPECT: ADMITTED` in `policy/tests/vap_actor_positive.yaml` DOC 4 -- the document whose
         own comment calls it *"the regression guard for V-CTN-004"*. Flagging it unconditionally
         would put this check in contradiction with the fixture that guards the very ID it
         implements. Paired with a group wildcard it stops being bounded by anything and becomes
         read-the-whole-cluster-forever, and that pairing gets its own finding.
     `nonResourceURLs` is deliberately not a wildcard axis: `/healthz` and `/version` are legitimate
     reader targets and a wildcard over them confers no verb. Its verbs are checked by property 2
     like any other rule's, which is where a `post` on `/apis` would surface.
  4. THE CLASSIFIER IS TOTAL. Every `Role`/`ClusterRole` in the corpus carrying either
     `kube-agents/role` or `kube-agents/tier` -- the exact population `vap-agent-readonly`'s
     `is-agent-rbac` match condition selects -- must classify as reader or actor. A role that
     matches neither is a silent skip, and a silent skip in an absence assertion is a hole shaped
     exactly like the object somebody forgot to label.
  5. A READER SUBJECT IS BOUND ONLY TO A READER ROLE ("universally"). The label on a `Role` bounds
     what that object grants; it says nothing about what the reader SA HOLDS. A `ClusterRoleBinding`
     pointing a reader SA at `view`, at `cluster-admin`, or at an actor's own grant is a legal
     object that no rules-scoped policy sees -- the v1 VAP evaluates a role's own rules and does not
     evaluate bindings, which the phase-0 ledger records as a confirmed-real deferred finding. So
     every binding that is labelled `kube-agents/role: reader`, or whose subjects name a reader
     ServiceAccount declared in the corpus, must `roleRef` a role that IS in the corpus and IS
     classified reader. A roleRef this check cannot resolve is a finding, not a pass: an unresolved
     target is an unbounded one.
  6. A READER CLUSTERROLE DECLARES NO `aggregationRule`. An aggregated ClusterRole's `rules` block
     is OVERWRITTEN by the controller with the union of every ClusterRole matching its selectors, so
     the static rules this check just read are not the rules the object will have. Properties 2 and
     3 would then be asserting something true about a document and false about the cluster -- 09
     §11.3's wrong-config-layer shape, one artifact further out. Phase 0 recorded this as admitted
     by the VAP and deferred; there is no reason for a reader to aggregate, so the allow-list of
     fields it may carry excludes it.
  7. THE CORPUS IS CLOSED. The tree is swept for every YAML document of an RBAC kind carrying a
     `kube-agents/` role or tier label, and each file it finds must be either in CORPUS or in
     TEST_CORPORA, which names the policy-test and L2-probe inputs one at a time WITH the reason
     each is not install-path RBAC. A new install path with a reader role in it is then a finding
     here rather than an object nobody checked, and a TEST_CORPORA entry whose file has moved is a
     finding too -- a stale exclusion hides whatever takes the path over next.

Run:  python3 dev/tests/reader-holds-only-read-verbs.py
      python3 dev/tests/reader-holds-only-read-verbs.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

# The three verbs a reader may hold. THE ONLY VERB LIST IN THIS FILE, and it is the permitted one
# (09 §11.4). Nothing below names a forbidden verb, because naming them is the defect.
READ_VERBS = ("get", "list", "watch")

ROLE_KINDS = ("Role", "ClusterRole")
BINDING_KINDS = ("RoleBinding", "ClusterRoleBinding")
RBAC_KINDS = ROLE_KINDS + BINDING_KINDS + ("ServiceAccount",)

ROLE_LABEL = "kube-agents/role"
TIER_LABEL = "kube-agents/tier"

# Install-path agent RBAC: every file that puts a reader or actor identity on a real cluster, by any
# of the four routes -- the provisioning templates, the GitOps reference tree's per-tier exemplars,
# its per-cluster materialized copies, and the cascade templates an agent renders for a cluster or
# namespace that does not exist yet. Held as an allow-list rather than a glob because property 7
# makes an omission a finding: a file the sweep finds and this list does not name fails the run.
CORPUS = (
    "agents/cluster-admin/skills/provision-developer-team/assets/50-developer-team-identity.yaml.tmpl",
    "agents/platform/skills/provision-cluster-admin/assets/identity/broker-operations.yaml.tmpl",
    "agents/platform/skills/provision-cluster-admin/assets/identity/cluster-admin-identity.yaml.tmpl",
    "examples/gitops-repo/clusters/cluster-a/agents/identity/broker-operations.yaml",
    "examples/gitops-repo/clusters/cluster-a/agents/identity/cluster-admin-identity.yaml",
    "examples/gitops-repo/clusters/cluster-a/namespaces/team-x/50-developer-team-identity.yaml",
    "examples/gitops-repo/policy/rbac-overlay/broker-operations.yaml",
    "examples/gitops-repo/policy/rbac-overlay/cluster-admin.yaml",
    "examples/gitops-repo/policy/rbac-overlay/developer-team.yaml",
    "examples/gitops-repo/policy/rbac-overlay/platform.yaml",
    "k8s-operator/scripts/actor-grant-cluster-admin.yaml.template",
    "k8s-operator/scripts/actor-grant-developer-team.yaml.template",
    "k8s-operator/scripts/actor-grant-platform.yaml.template",
    "k8s-operator/scripts/agent-identity.yaml.template",
    "k8s-operator/scripts/broker-operations-grant.yaml.template",
    "k8s-operator/scripts/cluster-admin-agent.yaml.template",
    "k8s-operator/scripts/developer-team-agent.yaml.template",
)

# Files the sweep finds that are inputs to a test rather than manifests bound for a cluster. Each is
# named individually, with the reason: an exclusion by directory glob would swallow the next thing
# somebody puts in that directory.
TEST_CORPORA = {
    "examples/gitops-repo/policy/tests/vap_actor_negatives.yaml": (
        "VAP conformance corpus -- objects written to be DENIED by vap-agent-readonly"
    ),
    "examples/gitops-repo/policy/tests/vap_actor_positive.yaml": (
        "VAP conformance corpus -- objects written to be ADMITTED, including DOC 4, which is "
        "V-CTN-004's own regression guard"
    ),
    "examples/gitops-repo/policy/tests/vap_clusteradmin_negatives.yaml": (
        "VAP conformance corpus -- deliberately over-broad roles, expected DENIED"
    ),
    "examples/gitops-repo/policy/tests/vap_developerteam_positive.yaml": (
        "VAP conformance corpus -- expected ADMITTED"
    ),
    "examples/gitops-repo/policy/tests/vap_positive.yaml": (
        "VAP conformance corpus -- expected ADMITTED"
    ),
    "dev/verify/fixtures/actor-tenant-grant.yaml": (
        "L2 probe fixture, applied only against the scratch cluster; its own header explains why it "
        "carries no kube-agents/role label"
    ),
}

# Non-vacuity floors. The two reader floors sit at what the tree renders today and only move up: a
# reader role or a reader identity that disappears is a corpus that stopped covering something,
# which is the failure this check has to be loud about.
MIN_READER_ROLES = 9
MIN_READER_SAS = 5

# The actor floor is deliberately NOT set at today's count, which is fifteen. It exists for one
# reason -- to prove the classifier answers both ways over the same corpus, so that "no reader holds
# a write verb" is a fact about a discriminated set rather than about a function that returns
# `actor` for everything. Three, one per tier, does that. Pinning it at fifteen would additionally
# assert that the ACTOR corpus has not shrunk, which is V-BRK-013's property over an artifact this
# check does not read, and would turn a legitimate consolidation of the actor grants into a red here
# for a reason this file cannot explain.
MIN_ACTOR_ROLES = 3

# One reader role from each install ROUTE, so a route going dark is a named finding rather than a
# count that happens to stay above the floor because another route grew.
ANCHORS = (
    "k8s-operator/scripts/cluster-admin-agent.yaml.template",
    "k8s-operator/scripts/developer-team-agent.yaml.template",
    "examples/gitops-repo/policy/rbac-overlay/platform.yaml",
    "examples/gitops-repo/clusters/cluster-a/agents/identity/cluster-admin-identity.yaml",
    "agents/platform/skills/provision-cluster-admin/assets/identity/cluster-admin-identity.yaml.tmpl",
    "agents/cluster-admin/skills/provision-developer-team/assets/50-developer-team-identity.yaml.tmpl",
)

SWEEP_SUFFIXES = (".yaml", ".yml", ".tmpl", ".template")
SWEEP_SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv", "dist", "build", "vendor"})

_DOC_KIND = re.compile(r"^kind:[ \t]*(\w+)[ \t]*$", re.M)
_SWEEP_KIND = re.compile(r"^kind:[ \t]*(?:Role|ClusterRole|RoleBinding|ClusterRoleBinding|ServiceAccount)[ \t]*$", re.M)
_SWEEP_LABEL = re.compile(r"^[ \t]*kube-agents/(?:role|tier):[ \t]*\S", re.M)

# A whole line that is nothing but a shell or `@@`-style placeholder. `agent-identity.yaml.template`
# has `${AGENT_READER_ANNOTATIONS}` sitting at column 0 inside a `metadata:` block -- the renderer
# substitutes an indented YAML fragment there. It is not YAML until it is rendered, it carries no
# label, verb or subject, and the alternative to dropping it is a parser that guesses.
_BLOCK_PLACEHOLDER = re.compile(r"^[ \t]*(?:\$\{[A-Za-z0-9_]+\}|@@[A-Za-z0-9_]+@@)[ \t]*$")


class YamlError(Exception):
    """The document is outside the subset below. Loud, on purpose -- see `parse_documents`."""


# ----------------------------------------------------------------------------------------------
# A strict reader for the YAML subset these manifests are written in.
#
# Hand-rolled because L0 installs nothing and the system python3 has no PyYAML; a check that needs a
# package is not L0. `dev/tests/yamlsubset.py` exists and is deliberately NOT reused: it rejects flow
# collections, and `verbs: ["get", "list", "watch"]` -- the exact line this check is about -- is a
# flow sequence in every RBAC file in the tree, because that is how prettier formats them. The
# subsets are disjoint in the one construct that matters, so sharing would mean widening a parser two
# corpus lints depend on to accept a notation they deliberately refuse.
#
# Accepted: block mappings, block sequences, single- and multi-line flow sequences of scalars,
# quoted and plain scalars, `# comments`. Rejected, by raising rather than by guessing: flow
# mappings, anchors, tags, and anything else. A raise is a finding (property 1), never a skip.
# ----------------------------------------------------------------------------------------------


def parse_documents(text: str) -> list[tuple[int, str, dict]]:
    """Every RBAC document in one file, as `(line number of the document, kind, mapping)`.

    Documents of other kinds -- the `Agent` CRs and `Namespace`s that share these files -- are
    skipped before parsing rather than after. They carry render placeholders in value position that
    are not YAML at all, and there is nothing in them this check reads.
    """
    out: list[tuple[int, str, dict]] = []
    for lineno, lines in _split_documents(text):
        body = "\n".join(lines)
        kind = _DOC_KIND.search(body)
        if not kind or kind.group(1) not in RBAC_KINDS:
            continue
        keep = [("" if _BLOCK_PLACEHOLDER.match(ln) else ln) for ln in lines]
        out.append((lineno, kind.group(1), _Parser(keep, lineno).document()))
    return out


def _split_documents(text: str) -> list[tuple[int, list[str]]]:
    docs: list[tuple[int, list[str]]] = []
    cur: list[str] = []
    start = 1
    for n, line in enumerate(text.splitlines(), start=1):
        if line.rstrip() == "---":
            docs.append((start, cur))
            cur, start = [], n + 1
            continue
        cur.append(line)
    docs.append((start, cur))
    return docs


class _Parser:
    def __init__(self, lines: list[str], first: int) -> None:
        self.lines = list(lines)
        self.i = 0
        self.first = first

    def document(self) -> dict:
        if self._at_end():
            return {}
        value = self._block(self._indent())
        if not self._at_end():
            raise self._oops("trailing content after the document body")
        if not isinstance(value, dict):
            raise YamlError("an RBAC document must be a mapping")
        return value

    # -- cursor ---------------------------------------------------------------------------------
    def _at_end(self) -> bool:
        while self.i < len(self.lines):
            stripped = self.lines[self.i].strip()
            if stripped == "" or stripped.startswith("#"):
                self.i += 1
            else:
                return False
        return True

    def _indent(self) -> int:
        line = self.lines[self.i]
        return len(line) - len(line.lstrip(" "))

    def _oops(self, msg: str) -> YamlError:
        return YamlError(f"line {self.first + self.i}: {msg}\n  {self.lines[self.i]!r}")

    # -- grammar --------------------------------------------------------------------------------
    def _block(self, indent: int):
        if self._at_end():
            return None
        body = self.lines[self.i].lstrip(" ")
        if body == "-" or body.startswith("- "):
            return self._sequence(indent)
        if body.startswith("["):
            self.i += 1
            return self._flow_from(_strip_comment(body))
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict:
        out: dict = {}
        while not self._at_end():
            here = self._indent()
            if here < indent:
                break
            if here > indent:
                raise self._oops(f"unexpected indent {here}, expected {indent}")
            body = self.lines[self.i].strip()
            if body.startswith("- "):
                break
            key, sep, rest = body.partition(":")
            if not sep:
                raise self._oops("expected `key: value`")
            key = key.strip()
            if key in out:
                raise self._oops(f"duplicate key {key!r}")
            self.i += 1
            rest = _strip_comment(rest.strip()).strip()
            if rest == "":
                out[key] = self._nested(indent)
            elif rest.startswith("["):
                out[key] = self._flow_from(rest)
            else:
                out[key] = _scalar(rest)
        return out

    def _sequence(self, indent: int) -> list:
        out: list = []
        while not self._at_end():
            here = self._indent()
            if here < indent:
                break
            if here > indent:
                raise self._oops(f"unexpected indent {here}, expected {indent}")
            body = self.lines[self.i].lstrip(" ")
            if body != "-" and not body.startswith("- "):
                break
            item = body[2:] if len(body) > 2 else ""
            if item == "":
                self.i += 1
                out.append(self._nested(indent))
            elif _MAP_ITEM.match(item):
                # `- key: value` opens a mapping whose remaining keys are aligned two columns in.
                # Rewriting the dash to spaces lets the mapping parser see all of them at one indent.
                self.lines[self.i] = " " * (indent + 2) + item
                out.append(self._mapping(indent + 2))
            else:
                self.i += 1
                out.append(_scalar(_strip_comment(item)))
        return out

    def _nested(self, parent_indent: int):
        if self._at_end() or self._indent() <= parent_indent:
            return None
        return self._block(self._indent())

    def _flow_from(self, opening: str) -> list:
        """A `[a, b, c]` sequence, continued across lines until the bracket closes."""
        text = opening
        while text.count("[") > text.count("]"):
            if self.i >= len(self.lines):
                raise YamlError(f"line {self.first}: unterminated flow sequence: {text!r}")
            text += " " + _strip_comment(self.lines[self.i].strip())
            self.i += 1
        return _flow(text)


_MAP_ITEM = re.compile(r'^(?:"[^"]*"|\'[^\']*\'|[^\s"\'#\[{][^:]*):(?:\s|$)')
_INT = re.compile(r"^-?\d+$")


def _strip_comment(text: str) -> str:
    """Drop a trailing `# comment`, respecting quotes."""
    out, quote, j = [], "", 0
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\" and quote == '"':
                out.append(text[j : j + 2])
                j += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (j == 0 or text[j - 1] in " \t"):
            break
        out.append(ch)
        j += 1
    return "".join(out).rstrip()


def _flow(text: str) -> list:
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise YamlError(f"not a flow sequence: {text!r}")
    inner = text[1:-1].strip()
    if inner == "":
        return []
    items, buf, quote = [], [], ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            raise YamlError(f"nested flow collections are not part of this subset: {text!r}")
        if ch == ",":
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    items.append("".join(buf).strip())
    return [_scalar(item) for item in items]


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text in ("", "null", "~"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if _INT.match(text):
        return int(text)
    if text[:1] in ("[", "{", "&", "!", "*", "|", ">"):
        raise YamlError(
            f"{text!r}: flow mappings, anchors, tags and block scalars are not part of the subset "
            "this check reads -- see parse_documents"
        )
    return text


# ----------------------------------------------------------------------------------------------
# The check
# ----------------------------------------------------------------------------------------------


class Obj:
    """One RBAC document, reduced to the fields this check reads."""

    def __init__(self, path: str, lineno: int, kind: str, doc: dict) -> None:
        meta = doc.get("metadata") or {}
        self.path = path
        self.lineno = lineno
        self.kind = kind
        self.name = meta.get("name")
        self.namespace = meta.get("namespace")
        self.labels = meta.get("labels") or {}
        self.rules = doc.get("rules") or []
        self.role_ref = doc.get("roleRef") or {}
        self.subjects = doc.get("subjects") or []
        self.aggregation = doc.get("aggregationRule")

    @property
    def where(self) -> str:
        ns = f" in {self.namespace}" if self.namespace else ""
        return f"{self.path}:{self.lineno} {self.kind}/{self.name}{ns}"

    @property
    def selected(self) -> bool:
        """Does `vap-agent-readonly`'s `is-agent-rbac` match condition select this object?"""
        return ROLE_LABEL in self.labels or TIER_LABEL in self.labels

    @property
    def role(self) -> str | None:
        value = self.labels.get(ROLE_LABEL)
        return value if value in ("reader", "actor") else None

    def key(self) -> tuple:
        return (self.kind, self.name, None if self.kind == "ClusterRole" else self.namespace)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def check(files: tuple[tuple[str, str | None], ...], swept: tuple[str, ...]) -> list[str]:
    objects: list[Obj] = []
    vacuous: list[str] = []

    for path, text in files:
        if text is None:
            vacuous.append(
                f"VACUOUS: {path} is named in CORPUS and is not in the tree. Every property below "
                f"is an absence assertion over the reader roles this check can read, and a file it "
                f"cannot open contributes none of them -- silently, and greenly."
            )
            continue
        try:
            for lineno, kind, doc in parse_documents(text):
                objects.append(Obj(path, lineno, kind, doc))
        except YamlError as exc:
            vacuous.append(
                f"VACUOUS: {path} did not parse, so none of its RBAC objects were examined: {exc}"
            )

    roles = [o for o in objects if o.kind in ROLE_KINDS]
    readers = [o for o in roles if o.role == "reader"]
    actors = [o for o in roles if o.role == "actor"]
    reader_sas = {
        (o.name, o.namespace) for o in objects if o.kind == "ServiceAccount" and o.role == "reader"
    }

    # --- 1. non-vacuity, and the classifier discriminates -------------------------------------
    if len(readers) < MIN_READER_ROLES:
        vacuous.append(
            f"VACUOUS: the corpus yielded {len(readers)} reader Role/ClusterRole(s), below the "
            f"floor of {MIN_READER_ROLES}. Properties 2, 3 and 6 assert that a set contains no "
            f"write verb; over a set this small they are asserting it about a tree that is no "
            f"longer there. Found: {sorted(o.where for o in readers)}"
        )
    if len(actors) < MIN_ACTOR_ROLES:
        vacuous.append(
            f"VACUOUS: the corpus yielded {len(actors)} actor Role/ClusterRole(s), below the floor "
            f"of {MIN_ACTOR_ROLES}. The reader/actor split is what makes an absence assertion over "
            f"readers mean anything: a classifier that answered `reader` to everything would fail "
            f"loudly, and one that answered `actor` to everything would pass in silence. It is "
            f"required to answer both ways over the same corpus."
        )
    if len(reader_sas) < MIN_READER_SAS:
        vacuous.append(
            f"VACUOUS: the corpus declares {len(reader_sas)} reader ServiceAccount(s), below the "
            f"floor of {MIN_READER_SAS}. Property 5 follows bindings FROM these subjects; with none "
            f"of them read, every binding in the tree looks like somebody else's."
        )
    contributed = {o.path for o in readers}
    for anchor in ANCHORS:
        if anchor not in contributed:
            vacuous.append(
                f"VACUOUS: {anchor} contributed no reader role. It is one of the install routes "
                f"this check anchors on, and a route that stops rendering a reader identity has to "
                f"be a finding here -- the count alone would stay above its floor while a whole way "
                f"of installing the fleet went unread."
            )
    if vacuous:
        return vacuous

    findings: list[str] = []
    reader_paths = {o.path for o in readers}

    # --- 2 & 3. the read allow-list, and the wildcard, per axis --------------------------------
    for obj in readers:
        for n, rule in enumerate(obj.rules):
            if not isinstance(rule, dict):
                findings.append(f"{obj.where} rule {n} is not a mapping: {rule!r}")
                continue
            verbs = [v for v in _as_list(rule.get("verbs")) if v is not None]
            groups = _as_list(rule.get("apiGroups"))
            resources = _as_list(rule.get("resources"))

            if not verbs:
                findings.append(
                    f"{obj.where} rule {n} declares no verbs. The API server rejects `verbs: []` "
                    f"with a required-value error, so this manifest cannot apply; and "
                    f"vap-agent-readonly's validation 1 short-circuits on `!has(r.verbs)`, so the "
                    f"policy would have admitted it without evaluating the allow-list at all."
                )
            for verb in verbs:
                if verb == "*":
                    findings.append(
                        f"{obj.where} rule {n} grants the wildcard verb `*` on "
                        f"{groups or ['(none)']} / {resources or ['(none)']}. That is not an "
                        f"unrecognised verb, it is every verb -- create, delete, escalate, bind, "
                        f"impersonate -- on everything the rule reaches, which is cluster-admin "
                        f"wherever the rule is broad. A reader holds {list(READ_VERBS)} and "
                        f"nothing else (03 §11, 08 §7)."
                    )
                elif verb not in READ_VERBS:
                    findings.append(
                        f"{obj.where} rule {n} grants `{verb}`, which is not one of "
                        f"{list(READ_VERBS)}. V-CTN-004 is universal: a reader identity holds no "
                        f"verb outside the read set on any resource, in any group, anywhere. This "
                        f"is an allow-list and `{verb}` was rejected by not being in it, not by "
                        f"being on a list of bad verbs -- 09 §11.4 is the incident where a "
                        f"deny-list omitted `impersonate` and admitted cluster-admin."
                    )

            if "*" in groups:
                findings.append(
                    f"{obj.where} rule {n} grants the wildcard apiGroup `*`. The group axis is a "
                    f"reader's outer bound: `*` reaches every API group installed now or later, "
                    f"including kubeagents.x-k8s.io -- the agent's own CR, the ActionRecords 03 "
                    f"§3.3 rule 4 protects, the FleetFreeze -- and admissionregistration.k8s.io, "
                    f"the policies that are supposed to bound it. It widens on the day somebody "
                    f"installs an operator, with no edit to this file to review."
                )
                if "*" in resources:
                    findings.append(
                        f"{obj.where} rule {n} pairs the wildcard resource `*` with a wildcard "
                        f"apiGroup, so the rule's extent is nothing at all. `resources: [\"*\"]` "
                        f"inside an enumerated group list is what 06 §2.2's read templates render "
                        f"and what the VAP corpus pins as admitted; inside `apiGroups: [\"*\"]` it "
                        f"is read-everything-in-the-cluster-forever and is bounded by no artifact "
                        f"in the tree."
                    )

    # --- 4. the classifier is total ------------------------------------------------------------
    for obj in roles:
        if obj.selected and obj.role is None:
            findings.append(
                f"{obj.where} carries {sorted(k for k in obj.labels if k.startswith('kube-agents/'))} "
                f"but no `{ROLE_LABEL}` of `reader` or `actor`, so this check classified it as "
                f"neither and asserted nothing about its verbs. It is inside "
                f"vap-agent-readonly's `is-agent-rbac` population, so the cluster governs it and "
                f"the tree does not. A role that matches neither classifier is a skip, and a skip "
                f"inside an absence assertion is a hole the exact shape of the object that fell in."
            )

    # --- 5. a reader subject is bound only to a reader role ------------------------------------
    by_key: dict[tuple, list[Obj]] = {}
    for obj in roles:
        by_key.setdefault(obj.key(), []).append(obj)

    for obj in objects:
        if obj.kind not in BINDING_KINDS:
            continue
        named = [
            s
            for s in obj.subjects
            if isinstance(s, dict)
            and s.get("kind") == "ServiceAccount"
            and (s.get("name"), s.get("namespace")) in reader_sas
        ]
        if obj.role != "reader" and not named:
            continue
        why = (
            f"labelled `{ROLE_LABEL}: reader`"
            if obj.role == "reader"
            else f"a binding of the reader ServiceAccount {named[0].get('name')!r}"
        )
        ref_kind, ref_name = obj.role_ref.get("kind"), obj.role_ref.get("name")
        if obj.kind == "ClusterRoleBinding" and ref_kind != "ClusterRole":
            findings.append(
                f"{obj.where} is {why} and its roleRef is a {ref_kind!r}; a ClusterRoleBinding may "
                f"only reference a ClusterRole, so this object confers nothing and hides whatever "
                f"it was meant to bound."
            )
            continue
        key = (ref_kind, ref_name, None if ref_kind == "ClusterRole" else obj.namespace)
        targets = by_key.get(key, [])
        if not targets:
            findings.append(
                f"{obj.where} is {why} and points at {ref_kind}/{ref_name}, which is not a role "
                f"this check can read. An unresolved target is an unbounded one: a reader SA bound "
                f"to a built-in like `view` or `cluster-admin` is a legal object, and the v1 VAP "
                f"evaluates a role's own rules and never looks at a binding -- the phase-0 ledger "
                f"records that gap as deferred to a webhook that does not exist yet. Either add "
                f"the target to CORPUS or stop binding a reader to it."
            )
            continue
        # `roleRef` is a (kind, name, namespace) triple and the corpus holds the same logical object
        # under several renderings -- the tier exemplar, its per-cluster materialized copy, the
        # cascade template -- so one binding can resolve to several. They are collapsed into one
        # finding: four identical sentences about one edit reads as four defects.
        wrong = sorted({t.role or "unlabelled" for t in targets} - {"reader"})
        if wrong:
            findings.append(
                f"{obj.where} is {why} and binds it to {ref_kind}/{ref_name}, which is classified "
                f"{wrong} by {sorted({t.path for t in targets})}. What a reader SA HOLDS is the "
                f"union of every role bound to it; the label on one object bounds that object and "
                f"says nothing about the subject. This binding hands a read-only identity somebody "
                f"else's authority without touching a single verb in a reader role."
            )

    # --- 6. no aggregationRule on a reader ClusterRole -----------------------------------------
    for obj in readers:
        if obj.aggregation is not None:
            findings.append(
                f"{obj.where} declares an `aggregationRule`. The controller OVERWRITES `rules` with "
                f"the union of every ClusterRole matching its selectors, so the rules this check "
                f"just read are not the rules the object will have -- properties 2 and 3 would be "
                f"true of the document and false of the cluster. Phase 0 recorded aggregated "
                f"ClusterRoles as admitted by the VAP (empty `.rules` at create, populated later) "
                f"and deferred; a reader has no reason to aggregate."
            )

    # --- 7. the corpus is closed ---------------------------------------------------------------
    named = set(CORPUS) | set(TEST_CORPORA)
    for path in swept:
        if path not in named:
            findings.append(
                f"{path} holds an agent-labelled RBAC object and is named neither in CORPUS nor in "
                f"TEST_CORPORA, so nothing in it was checked. If it is an install path, add it to "
                f"CORPUS; if it is a fixture, add it to TEST_CORPORA with the reason. A reader role "
                f"in a file this check does not open is exactly the green V-CTN-004 must not have."
            )
    for path, reason in sorted(TEST_CORPORA.items()):
        if path not in swept:
            findings.append(
                f"TEST_CORPORA excludes {path} ({reason}) and the sweep no longer finds it. A stale "
                f"exclusion is a standing waiver for whatever takes that path over next; drop the "
                f"entry or point it at where the fixture moved."
            )
    if set(CORPUS) & set(TEST_CORPORA):
        findings.append(
            f"{sorted(set(CORPUS) & set(TEST_CORPORA))} is named in CORPUS and in TEST_CORPORA. The "
            f"two lists partition the sweep; a path in both makes property 7 unfalsifiable for it."
        )

    return findings


def sweep(root: pathlib.Path) -> tuple[str, ...]:
    """Every file in the tree holding an RBAC document with a `kube-agents/` role or tier label.

    Deliberately textual rather than parsed. This is the arm that decides WHICH files get parsed, so
    it must not be able to lose one by failing to read it; an anchored `kind:` at column 0 plus the
    label the admission policy itself selects on is the cheapest predicate that cannot.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SWEEP_SKIP for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in SWEEP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _SWEEP_KIND.search(text) and _SWEEP_LABEL.search(text):
            found.append(path.relative_to(root).as_posix())
    return tuple(found)


def _inputs() -> tuple[tuple[tuple[str, str | None], ...], tuple[str, ...]]:
    files = []
    for rel in CORPUS:
        path = REPO / rel
        files.append((rel, path.read_text(encoding="utf-8") if path.exists() else None))
    return tuple(files), sweep(REPO)


def run() -> int:
    files, swept = _inputs()
    findings = check(files, swept)
    if findings:
        print(
            "FAIL: V-CTN-004 (L0) -- a reader identity is granted something outside "
            f"{list(READ_VERBS)}",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    objects = [
        Obj(path, lineno, kind, doc)
        for path, text in files
        for lineno, kind, doc in parse_documents(text or "")
    ]
    readers = [o for o in objects if o.kind in ROLE_KINDS and o.role == "reader"]
    actors = [o for o in objects if o.kind in ROLE_KINDS and o.role == "actor"]
    rules = sum(len(o.rules) for o in readers)
    print(
        f"PASS: V-CTN-004 (L0) -- {len(readers)} reader Role/ClusterRole(s) across "
        f"{len(CORPUS)} install-path file(s), {rules} rule(s), every verb inside "
        f"{list(READ_VERBS)} by allow-list; no wildcard verb or apiGroup; no aggregationRule; "
        f"{len(actors)} actor role(s) classified the other way over the same corpus; every "
        f"reader-bound roleRef resolves to a reader role; {len(swept)} agent-labelled RBAC "
        f"file(s) in the tree, all accounted for"
    )
    return 0


def _mutate(base, index: int, fn):
    out = list(base)
    out[index] = fn(out[index])
    return (out[0], out[1])


def _edit(base, relpath: str, old: str, new: str):
    """Rewrite one corpus file's text. The named path must be in CORPUS and the old text present."""

    def apply(files):
        return tuple(
            (p, (t.replace(old, new, 1) if p == relpath and t is not None else t)) for p, t in files
        )

    return _mutate(base, 0, apply)


def _drop_file(base, relpath: str):
    def apply(files):
        return tuple((p, (None if p == relpath else t)) for p, t in files)

    return _mutate(base, 0, apply)


# A reader ClusterRole a mutation can append to a corpus file. Written once because four rows need a
# fresh object rather than a perturbation of an existing one -- perturbing a reader role in place
# would move it out of the reader set and trip property 1's floor before the property under test
# ever ran, which is the overlap [[LSN-035]] is about.
def _role(name: str, labels: str, rule: str, extra: str = "") -> str:
    return (
        "---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\n"
        "kind: ClusterRole\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  labels:\n"
        f"{labels}"
        f"{extra}"
        "rules:\n"
        f"{rule}"
    )


READER_LABELS = "    kube-agents/tier: cluster-admin\n    kube-agents/role: reader\n"
READ_RULE = '  - apiGroups: [""]\n    resources: ["pods"]\n    verbs: ["get", "list", "watch"]\n'


def negative_control() -> int:
    """Every way this check could go quiet, each row naming the signal only its own property emits.

    The rows are built to land on ONE property each. Where a mutation would trip an earlier property
    first -- almost always the non-vacuity floor, which is broad by design -- it appends a new object
    instead of perturbing an existing one, so the floor stays satisfied and the narrow property is
    the thing that fires ([[LSN-035]]).
    """
    base = _inputs()
    findings = check(*base)
    if findings:
        print("  BROKEN   the tree is not green, so no row below can be attributed")
        for f in findings[:6]:
            print(f"           {f}")
        print("FAIL: V-CTN-004 negative control -- 0 mutations evaluated")
        return 1

    reader_role = "k8s-operator/scripts/cluster-admin-agent.yaml.template"
    overlay = "examples/gitops-repo/policy/rbac-overlay/platform.yaml"
    devteam = "examples/gitops-repo/policy/rbac-overlay/developer-team.yaml"
    identity = "k8s-operator/scripts/agent-identity.yaml.template"

    mutations = [
        (
            "the reader ClusterRole gains `impersonate` — 09 §11.4's own verb, which a deny-list "
            "admitted",
            _edit(
                base,
                reader_role,
                'verbs: ["get", "list", "watch"]',
                'verbs: ["get", "list", "watch", "impersonate"]',
            ),
            "grants `impersonate`, which is not one of ['get', 'list', 'watch']",
        ),
        (
            "the reader ClusterRole's verb list becomes the wildcard",
            _edit(base, reader_role, 'verbs: ["get", "list", "watch"]', 'verbs: ["*"]'),
            "grants the wildcard verb `*`",
        ),
        (
            "a reader role gains `escalate` — the verb that rewrites the boundary itself",
            _edit(
                base,
                overlay,
                'resources: ["customresourcedefinitions"]\n    verbs: ["get", "list", "watch"]',
                'resources: ["customresourcedefinitions"]\n    verbs: ["get", "escalate"]',
            ),
            "grants `escalate`",
        ),
        (
            "a reader Role gains an ordinary write verb on the tenant's own workloads",
            _edit(
                base,
                devteam,
                'apiGroups: ["", "apps", "batch", "networking.k8s.io"]\n'
                '    resources: ["*"]\n'
                '    verbs: ["get", "list", "watch"]',
                'apiGroups: ["", "apps", "batch", "networking.k8s.io"]\n'
                '    resources: ["*"]\n'
                '    verbs: ["get", "list", "watch", "patch"]',
            ),
            "grants `patch`",
        ),
        (
            "a reader rule declares no verbs, the shape validation 1 admits without evaluating",
            _edit(
                base,
                overlay,
                '  - apiGroups: ["apiextensions.k8s.io"]\n'
                '    resources: ["customresourcedefinitions"]\n'
                '    verbs: ["get", "list", "watch"]\n',
                '  - apiGroups: ["apiextensions.k8s.io"]\n'
                '    resources: ["customresourcedefinitions"]\n',
            ),
            "declares no verbs",
        ),
        (
            "a reader role widens to the wildcard apiGroup while staying read-only",
            _edit(
                base,
                overlay,
                'apiGroups: ["apiextensions.k8s.io"]',
                'apiGroups: ["*"]',
            ),
            "grants the wildcard apiGroup `*`",
        ),
        (
            "the wildcard apiGroup is paired with the wildcard resource — the unbounded rule",
            _edit(
                base,
                overlay,
                '  - apiGroups: ["apiextensions.k8s.io"]\n'
                '    resources: ["customresourcedefinitions"]\n',
                '  - apiGroups: ["*"]\n    resources: ["*"]\n',
            ),
            "pairs the wildcard resource `*` with a wildcard apiGroup",
        ),
        (
            "a role holding `*` on everything arrives carrying a tier and no role label — properties "
            "2 and 3 never see it, and totality is the only thing between it and a green",
            _edit(
                base,
                overlay,
                "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding",
                _role(
                    "platform-agent-unlabelled",
                    "    kube-agents/tier: platform\n",
                    '  - apiGroups: ["*"]\n    resources: ["*"]\n    verbs: ["*"]\n',
                )
                + "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding",
            ),
            "no `kube-agents/role` of `reader` or `actor`",
        ),
        (
            "a reader ClusterRole grows an aggregationRule, so its rules are written by the "
            "controller and not by the tree",
            _edit(
                base,
                overlay,
                "  name: platform-agent-explorer\n  labels:\n"
                "    kube-agents/tier: platform\n    kube-agents/role: reader\nrules:",
                "  name: platform-agent-explorer\n  labels:\n"
                "    kube-agents/tier: platform\n    kube-agents/role: reader\n"
                "aggregationRule:\n  clusterRoleSelectors:\n"
                "    - matchLabels:\n        rbac.example.com/aggregate: \"true\"\nrules:",
            ),
            "declares an `aggregationRule`",
        ),
        (
            "the broker-operations ClusterRoleBinding's subject is flipped from the actor SA to "
            "the reader SA — one word, and the reader holds `create` on the journal",
            _edit(
                base,
                identity,
                "subjects:\n  - kind: ServiceAccount\n    name: ${AGENT_ACTOR_KSA}",
                "subjects:\n  - kind: ServiceAccount\n    name: ${AGENT_READER_KSA}",
            ),
            "is a binding of the reader ServiceAccount '${AGENT_READER_KSA}'",
        ),
        (
            "a reader-labelled binding points at a Kubernetes built-in this check cannot read",
            _edit(
                base,
                overlay,
                "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
                "  name: platform-agent-explorer",
                "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
                "  name: cluster-admin",
            ),
            "which is not a role this check can read",
        ),
        (
            "a reader role file is deleted, so its absence assertions have nothing to assert over",
            _drop_file(base, reader_role),
            "VACUOUS: k8s-operator/scripts/cluster-admin-agent.yaml.template is named in CORPUS",
        ),
        (
            "a reader role loses its reader label, so the corpus quietly shrinks below its floor",
            _edit(base, devteam, "    kube-agents/role: reader\nrules:", "rules:"),
            "below the floor of 9",
        ),
        (
            "a corpus file stops parsing, so its objects are read by nobody",
            _edit(base, reader_role, "kind: ClusterRole\nmetadata:", "kind: ClusterRole\n metadata:"),
            "did not parse, so none of its RBAC objects were examined",
        ),
        (
            "a new install path appears with agent RBAC in it and is named in neither list",
            _mutate(base, 1, lambda swept: swept + ("clusters/cluster-b/identity.yaml",)),
            "is named neither in CORPUS nor in TEST_CORPORA",
        ),
        (
            "a named test corpus moves, leaving a standing exclusion pointed at nothing",
            _mutate(
                base,
                1,
                lambda swept: tuple(
                    p for p in swept if p != "examples/gitops-repo/policy/tests/vap_actor_positive.yaml"
                ),
            ),
            "and the sweep no longer finds it",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        # A mutation that did not change its input cannot be evaluated: the unmutated base is
        # re-checked, comes back clean, and the row prints MISS -- the verdict for "the check let the
        # defect through" -- over a defect that was never applied ([[LSN-063]]).
        if args == base:
            failures += 1
            print(f"  BROKEN  {name}")
            print("           the mutation did not change its input; nothing was evaluated")
            continue
        found = check(*args)
        hit = any(needle in f for f in found)
        print(f"  {'caught ' if hit else 'MISS   '} {name}")
        if not hit:
            failures += 1
            print(f"           expected a finding containing {needle!r}; got {found[:2] or 'none'}")
    print(
        f"{'PASS' if not failures else 'FAIL'}: V-CTN-004 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught, each by the property it "
        f"was written for"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
