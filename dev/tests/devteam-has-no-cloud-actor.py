#!/usr/bin/env python3
"""V-CTN-039: the developer-team tier has a Kubernetes actor and no cloud one.

R-06.2.3-6, from 06 §2.3's Cloud IAM table. Five of the six identities in that table get a Google
service account; the sixth row reads, in full:

    | developer-team **actor** | **none in v1** | — | a namespace tier has no cloud write surface;
      add a narrowly-conditioned GSA only when a concrete need appears |

Every containment check in this tree asserts what a principal CANNOT DO -- V-BRK-012 forbids a
fleet-wide writer, `vap-agent-readonly` denies the write verbs, `actor-grant-single-sourced.py`
holds the rendered grant to the read half of the template. Not one of them asserts that a principal
DOES NOT EXIST. That is a different shape and it fails differently: a new GSA is not a widening of
something a check already watches, it is a new subject that no existing selector names, so every
containment check in the tree stays green while the tier acquires a cloud write path.

The obligation is written down today in exactly one place, as prose, in
k8s-operator/scripts/agent-identity.yaml.template lines 48-51 ([[LSN-019]] -- a comment is not a
check):

    "NO WORKLOAD IDENTITY ANNOTATION HERE, deliberately. 06 §2.3 eventually gives the actor a cloud
    GSA that can write cloud resources. Phase 9's actor authority is the broker-operations grant and
    nothing else, and binding a cloud-write credential to it now would hand the actor months of
    authority ahead of the controls that are supposed to bound it (P10-T1 owns that)."

A comment cannot survive a refactor that moves the annotation block, and it says nothing at all
about the other fifteen ServiceAccount documents in the tree. This is that paragraph, mechanized.

Five properties.

  1. NON-VACUITY, AND THE SPEC ROW ITSELF. 06 §2.3's Cloud IAM table parses to exactly SIX rows --
     three tiers times reader/actor -- the `developer-team **actor**` row's GSA cell is the "none in
     v1" marker, and the OTHER two actor rows each name a GSA. The corpus floors come with it: the
     ServiceAccount sweep found a plausible number of documents, some of them actor-labelled, the
     GSA sweep found a plausible number of identifiers, and common.sh still has a variable that
     renders a Workload Identity annotation. Properties 2-5 are all absence assertions, and an
     absence asserted over a corpus that did not parse is the greenest thing in this repo.
  2. NO ACTOR SERVICEACCOUNT CARRIES A WORKLOAD IDENTITY ANNOTATION. Every ServiceAccount document
     in the tree that bears `kube-agents/role: actor` must carry neither the
     `iam.gke.io/gcp-service-account` annotation nor a placeholder that common.sh expands into one.
     The second half matters as much as the first: the reader in agent-identity.yaml.template gets
     its annotation from `${AGENT_READER_ANNOTATIONS}`, so the one-line edit that would grant the
     actor a cloud identity is moving that placeholder down eleven lines, and it never types the
     annotation key at all. The placeholder set is DISCOVERED from common.sh's own assignments
     ([[LSN-036]]), not listed here, so a second renderer is covered the day it lands.
  3. NO GSA IDENTIFIER IN THE TREE CLASSIFIES AS (developer-team, actor). A GSA identifier is
     anything this tree presents as a Google service account: the local part of a
     `*.iam.gserviceaccount.com` email, the value of an `iam.gke.io/gcp-service-account` annotation,
     the name or default of a shell variable whose name contains `GSA`, the argument of a `gcloud
     iam service-accounts create|describe|delete`, and the backticked cells of 06 §2.3's own GSA
     column. Each is mapped to a (tier, role) pair and held to the ALLOW-LIST derived from property
     1 -- the five (tier, role) pairs whose §2.3 row names a GSA (09 §11.4: an allow-list, never a
     deny-list; the permitted set is read off the spec rather than written down twice). Identifiers
     that classify to no tier -- the router, the controller, the GitHub minter, CI -- are out of
     scope here; whether THEY match §2.3 is R-06.2.3-5's territory, and V-CTN-030 owns it.
     The recogniser SELF-TESTS before it asserts anything: a recogniser that recognises nothing
     would make this property a sweep that can never fire, which is the [[LSN-035]] shape exactly.
     It is fed 06 §2.3's own two actor GSA names and must return (platform, actor) and
     (cluster-admin, actor), and it is fed two synthetic developer-team actor names and must return
     the pair this property forbids -- proving it can produce the verdict before it reports never
     having seen it.
  4. THE DEVELOPER-TEAM CLOUD ROLES ARE READ-ONLY AND NON-EMPTY. provision_04_gcp_iam.sh's
     `DEVELOPER_TEAM_ROLES=(...)` is the tier's whole cloud authority, and the header three lines
     above it states the invariant: *"Both stay VIEWER-ONLY: invariant #1 says the agent's cloud
     identity never holds a write role -- the only mutation path is a reviewed GitOps PR."* Every
     entry must be in READ_ONLY_CLOUD_ROLES, and the array must be NON-EMPTY: an empty array
     satisfies any allow-list, so "no write role" and "no role at all" would report the same green,
     and the second one is a broken install rather than a safe one.
  5. "NO CLOUD ACTOR" IS DISTINGUISHABLE FROM "NO ACTOR AT ALL". The developer-team tier DOES have a
     Kubernetes actor: actor-grant-developer-team.yaml.template, carrying `kube-agents/role: actor`.
     Without this property the cheapest way to turn properties 2 and 3 green is to delete the
     developer-team actor identity outright, which is precisely backwards -- the tier is supposed to
     have an actor with the broker-operations grant and nothing else.

THE SPELLING DRIFT THIS CHECK DELIBERATELY TOLERATES. 06 §2.3 spells the developer-team READER GSA
`kubeagents-devteam-<ns>-gsa`; provision_04_gcp_iam.sh creates `kubeagents-developer-team-gsa`. Those
two are the same identity under two names and they do not agree. That is a real defect and it is NOT
this check's -- it belongs to R-06.2.3-5 / V-CTN-030, phase 11, which reconciles §2.3's GSA column
against what provisioning actually creates. The recogniser here treats `devteam` and `developer-team`
as the same tier on purpose so that this check reports on the ROLE axis alone. Do not "fix" the
tolerance: narrowing it would make V-CTN-039 fail for V-CTN-030's reason, and the next reader would
have two red checks pointing at one defect and no way to tell which.

WHAT THIS CHECK DOES NOT ASSERT. That the platform and cluster-admin actor GSAs exist on a cluster,
or carry the IAM Conditions §2.3 pins on them -- both need cloud API access and are L2. That an
`Agent` CR's `spec.identity.serviceAccountAnnotations` never carries a Workload Identity annotation
for an actor: the CRD has one annotation map and the operator renders it onto the READER
ServiceAccount, so there is no actor-shaped hole there today. If that ever changes, this is the
check that should grow the arm. And anything under `dev/tests/` -- the L0 harness itself, which is
never applied to anything and which necessarily carries the forbidden identifier as a fixture; see
EXCLUDED_PREFIX.

Run:  python3 dev/tests/devteam-has-no-cloud-actor.py
      python3 dev/tests/devteam-has-no-cloud-actor.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gitcorpus import read_repo_files  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

# The L0 check directory is not part of the corpus these checks sweep. This file necessarily
# contains the very identifier property 3 forbids -- twice as a recogniser probe, once more as a
# negative-control mutation -- and a check that reports its own fixtures as findings is a check that
# cannot have a negative control. The exclusion is the DIRECTORY rather than this one path because
# the next check to reason about cloud identity will carry the same fixtures, and a V-CTN-039 that
# goes red the day a sibling check is written is a V-CTN-039 somebody turns off.
#
# It costs nothing that matters. Nothing under dev/tests/ is ever applied to a cluster: these files
# read the tree, compare it to a spec and exit. `dev/verify/` -- the L2 shell scripts that DO apply
# manifests, and their fixtures -- stays in scope, as does everything else.
EXCLUDED_PREFIX = "dev/tests/"
SELF = f"{EXCLUDED_PREFIX}{pathlib.Path(__file__).name}"

SPEC = "docs/design/06-api-and-data-contracts.md"
PROVISION = "k8s-operator/scripts/provision_04_gcp_iam.sh"
COMMON = "k8s-operator/scripts/common.sh"
IDENTITY_TEMPLATE = "k8s-operator/scripts/agent-identity.yaml.template"
DEVTEAM_ACTOR_GRANT = "k8s-operator/scripts/actor-grant-developer-team.yaml.template"

TIER_LABEL = "kube-agents/tier"
ROLE_LABEL = "kube-agents/role"
WI_ANNOTATION = "iam.gke.io/gcp-service-account"

DEVTEAM = "developer-team"

# Which suffixes hold Kubernetes objects. Markdown is excluded on purpose: a fenced ServiceAccount in
# a design doc is an illustration, and holding an illustration to an install-path rule turns every
# doc edit into a security finding.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".yaml.template", ".yml.template", ".yaml.tmpl", ".yml.tmpl")

# The tier axis of the recogniser in property 3. Keys are token sequences, matched against the
# identifier's normalised tokens; the longest match wins so `cluster-admin` is not read as no tier at
# all. `devteam` is 06 §2.3's spelling and `developer-team` is provisioning's -- see the tolerance
# paragraph in the module docstring before touching either.
TIER_ALIASES: dict[tuple[str, ...], str] = {
    ("platform",): "platform",
    ("cluster", "admin"): "cluster-admin",
    ("developer", "team"): DEVTEAM,
    ("devteam",): DEVTEAM,
}

# The role axis. An identifier is an actor's iff it carries one of these tokens; everything else is a
# reader, which is the conservative direction -- a misread actor becomes a reader and this check goes
# quiet, so the token list is the thing to grow when a new naming convention appears.
ACTOR_TOKENS = frozenset({"actor"})

# Property 4's allow-list. Enumerated rather than pattern-matched, and each entry carries WHY it is
# read-only, because "it has 'viewer' in the name" is the argument that would also admit
# `roles/logging.viewAccessor` (which grants access to _restricted_ log buckets) and
# `roles/iam.serviceAccountTokenCreator` is one rename away from looking harmless too.
READ_ONLY_CLOUD_ROLES: dict[str, str] = {
    "roles/viewer": "the project-wide primitive read role; no mutating permission at all",
    "roles/container.viewer": "GKE object reads; the write twin is container.developer/admin",
    "roles/container.clusterViewer": "cluster metadata + get-credentials, no object writes",
    "roles/monitoring.viewer": "metric reads; the write twin is monitoring.editor",
    "roles/logging.viewer": "log reads over the _Default bucket; grants no bucket or sink writes",
    "roles/iam.securityReviewer": "reads IAM policy everywhere and sets it nowhere",
}

MIN_SA_DOCUMENTS = 12
MIN_ACTOR_SA_DOCUMENTS = 4
MIN_GSA_IDENTIFIERS = 20
MIN_TIER_GSA_IDENTIFIERS = 6

# ── 06 §2.3's table ────────────────────────────────────────────────────────────────────────────
IAM_TABLE_HEADER = re.compile(
    r"^\|\s*Identity\s*\|\s*GSA\s*\|\s*Roles\s*\|\s*IAM condition\s*\|\s*$", re.M
)
IDENTITY_CELL = re.compile(r"^([a-z][a-z-]*?)\s+\*\*(reader|actor)\*\*$")
NONE_MARKER = re.compile(r"^\*\*none in v1\*\*$")
BACKTICKED = re.compile(r"`([^`]+)`")

# ── the GSA sweep ──────────────────────────────────────────────────────────────────────────────
GSA_EMAIL = re.compile(r"([A-Za-z0-9._$<>{}%|-]+)@[A-Za-z0-9._$<>{}%|-]*\.iam\.gserviceaccount\.com")
GSA_WI_VALUE = re.compile(rf"{re.escape(WI_ANNOTATION)}\s*[:=]\s*\\?[\"']?([^\"'\s\\]+)")
GSA_SHELL_VAR = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*GSA[A-Za-z0-9_]*)="
    r"[\"']?(?:\$\{\1:-)?([A-Za-z0-9._$<>{}-]+?)\}?[\"']?\s*$",
    re.M,
)
GSA_GCLOUD = re.compile(
    r"gcloud\s+iam\s+service-accounts\s+(?:create|describe|delete)\s+[\"']?([A-Za-z0-9._$<>{}@-]+)"
)

# `annotations="  annotations:\n    iam.gke.io/gcp-service-account: \"${gsa_email}\""` and the
# `AGENT_READER_ANNOTATIONS="${annotations}"` that carries it into envsubst. DOTALL because the
# first one spans two lines, and `\\.` so the escaped quotes inside do not end the match early.
SHELL_ASSIGN = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=\"((?:[^\"\\]|\\.)*)\"", re.S)
SHELL_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
PLACEHOLDER_LINE = re.compile(r"^\s*\$\{([A-Za-z_][A-Za-z0-9_]*)\}\s*$")

# ── provision_04's role arrays ─────────────────────────────────────────────────────────────────
DEVTEAM_ROLES_ARRAY = re.compile(r"^DEVELOPER_TEAM_ROLES=\((.*?)^\)", re.S | re.M)
QUOTED_ROLE = re.compile(r"[\"']([^\"']+)[\"']")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# A narrow YAML reader
# ──────────────────────────────────────────────────────────────────────────────────────────────
#
# PyYAML is not installed on the L0 runner and dev/tests/yamlsubset.py rejects flow collections,
# which is what every `verbs: ["get", ...]` line in this tree's RBAC is. What this needs from a
# manifest is three things -- the top-level kind, metadata.labels, metadata.annotations -- plus the
# raw lines, so it reads those by indentation and looks at nothing else.


def _documents(text: str) -> list[list[str]]:
    """Split a multi-document YAML file, dropping blank and whole-line-comment lines."""
    docs: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.rstrip() == "---":
            if current:
                docs.append(current)
            current = []
            continue
        current.append(line.rstrip())
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
    out: dict[str, str] = {}
    for line in lines:
        if _indent(line) != at or ":" not in line:
            continue
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class ServiceAccountDoc:
    """One `kind: ServiceAccount` document, reduced to what property 2 reasons about."""

    def __init__(self, source: str, lines: list[str]) -> None:
        meta = _block(lines, "metadata", 0)
        self.source = source
        self.name = _scalar(meta, "name", 2) or "<unnamed>"
        self.namespace = _scalar(meta, "namespace", 2)
        self.labels = _pairs(_block(meta, "labels", 2), 4)
        self.annotations = _pairs(_block(meta, "annotations", 2), 4)
        self.placeholders = {
            m.group(1) for m in (PLACEHOLDER_LINE.match(line) for line in lines) if m
        }

    @property
    def role(self) -> str | None:
        return self.labels.get(ROLE_LABEL)

    @property
    def tier(self) -> str | None:
        return self.labels.get(TIER_LABEL)

    def where(self) -> str:
        ns = f"{self.namespace}/" if self.namespace else ""
        return f"{self.source}: ServiceAccount {ns}{self.name}"


def service_accounts(files: dict[str, str]) -> list[ServiceAccountDoc]:
    """Every top-level ServiceAccount document in the manifest corpus.

    Top-level only: `verification/fixtures/classifier-corpus.yaml` and both actor-grant fixtures
    carry `- kind: ServiceAccount` as an RBAC *subject*, which is a reference to an identity rather
    than a declaration of one, and counting those would make the floor in property 1 pass on a tree
    that declares no ServiceAccounts at all.
    """
    out: list[ServiceAccountDoc] = []
    for rel, text in sorted(files.items()):
        if not rel.endswith(MANIFEST_SUFFIXES) or "kind: ServiceAccount" not in text:
            continue
        for doc in _documents(text):
            if _scalar(doc, "kind", 0) == "ServiceAccount":
                out.append(ServiceAccountDoc(rel, doc))
    return out


def wi_placeholders(common: str) -> set[str]:
    """Shell variables whose value reaches the Workload Identity annotation key.

    Discovered rather than listed ([[LSN-036]]). `render_agent_identity` builds the annotation block
    in a local called `annotations` and hands it to envsubst as `AGENT_READER_ANNOTATIONS`, so the
    key and the placeholder that lands in the template are two hops apart; the closure below walks
    the hop. A second renderer built the same way is covered the day it is written, and one built a
    different way makes property 1's floor go red rather than making property 2 go quiet.
    """
    values = {name: value for name, value in SHELL_ASSIGN.findall(common)}
    bearing = {name for name, value in values.items() if WI_ANNOTATION in value}
    changed = True
    while changed:
        changed = False
        for name, value in values.items():
            if name in bearing:
                continue
            if any(ref in bearing for ref in SHELL_REF.findall(value)):
                bearing.add(name)
                changed = True
    return bearing


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 06 §2.3's Cloud IAM table
# ──────────────────────────────────────────────────────────────────────────────────────────────


def parse_iam_table(spec: str) -> list[tuple[str, str]]:
    """§2.3's table as [(identity cell, GSA cell)], in document order, separator dropped."""
    rows: list[tuple[str, str]] = []
    header = IAM_TABLE_HEADER.search(spec)
    if not header:
        return rows
    for line in spec[header.end() :].lstrip("\n").splitlines():
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            break
        if set(cells[0]) <= {"-", ":"}:
            continue  # the |---|---| separator
        rows.append((cells[0], cells[1]))
    return rows


# ──────────────────────────────────────────────────────────────────────────────────────────────
# The recogniser
# ──────────────────────────────────────────────────────────────────────────────────────────────


def normalise(identifier: str) -> str:
    """A GSA identifier reduced to lowercase dash-separated tokens.

    The tree spells the same account five ways -- `kubeagents-developer-team-gsa`,
    `${DEVELOPER_TEAM_GSA_NAME}`, `kubeagents-devteam-<ns>-gsa`, an email local part, and the shell
    variable name itself -- so every separator this repo uses to build one (`_`, `.`, `${}`, `<>`,
    `@@`) collapses to a single dash before the tokens are read.
    """
    local = identifier.split("@", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")


def classify(identifier: str) -> tuple[str | None, str]:
    """Map a GSA identifier to `(tier | None, "reader" | "actor")`.

    `None` for the tier means "not an agent-tier GSA" -- the router, the controller, the GitHub
    minter, CI. Those are outside R-06.2.3-6 and property 3 says so explicitly rather than guessing.
    """
    tokens = [t for t in normalise(identifier).split("-") if t]
    tier: str | None = None
    matched = 0
    for alias, name in TIER_ALIASES.items():
        width = len(alias)
        if width <= matched:
            continue
        for i in range(len(tokens) - width + 1):
            if tuple(tokens[i : i + width]) == alias:
                tier, matched = name, width
                break
    role = "actor" if ACTOR_TOKENS & set(tokens) else "reader"
    return tier, role


def gsa_identifiers(files: dict[str, str]) -> dict[str, set[str]]:
    """Every string this tree presents as a Google service account -> the files it appears in.

    Four of the five anchored sources; the fifth is 06 §2.3's own backticked GSA column, which is
    markdown rather than an email or a shell assignment and is merged in by `check`, where the
    parsed table lives. Each source is unambiguous on its own -- a bare `kubeagents-*` token is NOT
    one of them, because half the tree's Kubernetes ServiceAccounts, ClusterRoles and Pub/Sub
    subscriptions are spelled that way and sweeping them would drown the property in identities that
    have no cloud existence at all.
    """
    found: dict[str, set[str]] = {}

    def add(value: str, rel: str) -> None:
        value = value.strip().strip("\\\"'")
        if value:
            found.setdefault(value, set()).add(rel)

    # Each pattern is preceded by the literal substring it cannot match without. The corpus is ~14MB
    # and the email pattern's character class backtracks over long runs of identifier characters, so
    # the guard is the difference between a second and a tenth of one. It changes nothing about what
    # is found -- a text with no `gserviceaccount.com` in it has no email match by construction.
    for rel, text in sorted(files.items()):
        for literal, pattern in (
            ("gserviceaccount.com", GSA_EMAIL),
            (WI_ANNOTATION, GSA_WI_VALUE),
            ("service-accounts", GSA_GCLOUD),
        ):
            if literal not in text:
                continue
            for m in pattern.finditer(text):
                add(m.group(1), rel)
        if "GSA" in text:
            for m in GSA_SHELL_VAR.finditer(text):
                add(m.group(1), rel)  # the variable NAME: DEVELOPER_TEAM_ACTOR_GSA_NAME says it all
                add(m.group(2), rel)  # and its default value
    return found


# The synthetic half of the recogniser's self-test; the other half is read off 06 §2.3's own GSA
# column at run time, so the two names the spec really uses are probes rather than copies. These
# four are the load-bearing ones -- the first two prove the recogniser can EMIT the pair property 3
# reports never having seen, and the last two pin the spelling tolerance in place. Without them a
# recogniser that returned `(None, "reader")` for everything would pass property 3 on a tree full of
# developer-team actor GSAs, which is [[LSN-035]] with a security consequence.
RECOGNISER_PROBES: list[tuple[str, tuple[str | None, str]]] = [
    ("kubeagents-devteam-team-x-actor", (DEVTEAM, "actor")),
    ("kubeagents-developer-team-actor-gsa@p.iam.gserviceaccount.com", (DEVTEAM, "actor")),
    ("kubeagents-devteam-<ns>-gsa", (DEVTEAM, "reader")),
    ("kubeagents-developer-team-gsa", (DEVTEAM, "reader")),
]


# ──────────────────────────────────────────────────────────────────────────────────────────────
# The properties
# ──────────────────────────────────────────────────────────────────────────────────────────────


def check(spec_text: str, provision_text: str, corpus: dict[str, str]) -> list[str]:
    files = dict(corpus)
    files[SPEC] = spec_text
    files[PROVISION] = provision_text

    rows = parse_iam_table(spec_text)
    sas = service_accounts(files)
    actor_sas = [sa for sa in sas if sa.role == "actor"]
    placeholders = wi_placeholders(files.get(COMMON, ""))

    identifiers = gsa_identifiers(files)
    # The fifth source: 06 §2.3's own GSA column. It is markdown rather than an email or a shell
    # assignment, so the sweep's four anchored patterns cannot see it -- and the spec is the one
    # artifact where a developer-team actor GSA would arrive as a decision rather than as a mistake.
    for _identity, gsa_cell in rows:
        for m in BACKTICKED.finditer(gsa_cell):
            identifiers.setdefault(m.group(1), set()).add(f"{SPEC} §2.3")

    # --- 1. non-vacuity, and the spec row itself --------------------------------------------
    vacuous: list[str] = []

    if not rows:
        vacuous.append(
            f"VACUOUS: 06 §2.3's Cloud IAM table did not parse -- no "
            f"`| Identity | GSA | Roles | IAM condition |` header in {SPEC}. The allow-list "
            f"property 3 holds every GSA to is READ OFF that table, so without it the sweep "
            f"compares against nothing and reports the same green as a clean tree."
        )
    else:
        parsed: dict[tuple[str, str], str] = {}
        for identity, gsa in rows:
            m = IDENTITY_CELL.match(identity)
            if not m:
                vacuous.append(
                    f"VACUOUS: 06 §2.3's Identity cell {identity!r} is not `<tier> **reader**` or "
                    f"`<tier> **actor**`, so this check cannot tell which identity the row's GSA "
                    f"cell belongs to."
                )
                continue
            parsed[(m.group(1), m.group(2))] = gsa
        expected = {(t, r) for t in ("platform", "cluster-admin", DEVTEAM) for r in ("reader", "actor")}
        if len(rows) != len(expected):
            vacuous.append(
                f"06 §2.3's Cloud IAM table parses to {len(rows)} row(s); it is three tiers times "
                f"reader/actor, so it is {len(expected)}. A row that arrived is an identity nobody "
                f"decided about, and a row that left is one this check silently stops covering."
            )
        for missing in sorted(expected - set(parsed)):
            vacuous.append(
                f"VACUOUS: 06 §2.3 has no `{missing[0]} **{missing[1]}**` row. That pair is either "
                f"absent from the allow-list property 3 derives (so a legitimate GSA reports as a "
                f"violation) or -- for developer-team/actor -- the row this whole check exists to "
                f"read."
            )
        for extra in sorted(set(parsed) - expected):
            vacuous.append(
                f"06 §2.3 has a `{extra[0]} **{extra[1]}**` row and this check knows no such "
                f"identity. Teach it the tier before the row grants anything: an unrecognised tier "
                f"classifies as no tier at all, and property 3 skips those."
            )
        devteam_actor = parsed.get((DEVTEAM, "actor"))
        if devteam_actor is not None and not NONE_MARKER.match(devteam_actor):
            vacuous.append(
                f"06 §2.3's `{DEVTEAM} **actor**` row now names {devteam_actor!r} where it said "
                f"**none in v1**. That is the requirement itself changing, not a drift: R-06.2.3-6 "
                f"is the row. If P10-T1 really has landed the controls that bound a cloud-writing "
                f"actor, retire V-CTN-039 in 09 §11 deliberately -- do not let it pass by reading a "
                f"table that no longer says what it is checking."
            )
        for named in (("platform", "actor"), ("cluster-admin", "actor")):
            cell = parsed.get(named)
            if cell is not None and not BACKTICKED.search(cell):
                vacuous.append(
                    f"VACUOUS: 06 §2.3's `{named[0]} **{named[1]}**` row names no GSA (cell is "
                    f"{cell!r}). The other two actor rows are what make `**none in v1**` on the "
                    f"third one mean something; if none of them names a GSA then 'the developer-team "
                    f"actor has no GSA' is a statement about a table where nobody does."
                )

    if len(sas) < MIN_SA_DOCUMENTS:
        vacuous.append(
            f"VACUOUS: found {len(sas)} ServiceAccount document(s) in the corpus, below the floor "
            f"of {MIN_SA_DOCUMENTS}. Property 2 is an absence assertion over this set; over an "
            f"empty set it is a tautology."
        )
    if len(actor_sas) < MIN_ACTOR_SA_DOCUMENTS:
        vacuous.append(
            f"VACUOUS: {len(actor_sas)} of {len(sas)} ServiceAccount document(s) carry "
            f"`{ROLE_LABEL}: actor`, below the floor of {MIN_ACTOR_SA_DOCUMENTS}. Property 2 "
            f"inspects exactly the actor-labelled ones, so a label rename would empty its input and "
            f"turn it green."
        )
    if not placeholders:
        vacuous.append(
            f"VACUOUS: no shell variable in {COMMON} renders a `{WI_ANNOTATION}` annotation, so "
            f"property 2's placeholder arm has nothing to look for. The one-line way to give the "
            f"actor a cloud identity is to move the reader's annotation PLACEHOLDER onto it, which "
            f"never types the annotation key -- without the placeholder set that edit is invisible."
        )
    if len(identifiers) < MIN_GSA_IDENTIFIERS:
        vacuous.append(
            f"VACUOUS: the GSA sweep found {len(identifiers)} identifier(s), below the floor of "
            f"{MIN_GSA_IDENTIFIERS}. Property 3 asserts that none of them classifies as "
            f"({DEVTEAM}, actor); over a set this small the sweep has stopped reaching the tree."
        )
    tiered = {i for i in identifiers if classify(i)[0] is not None}
    if len(tiered) < MIN_TIER_GSA_IDENTIFIERS:
        vacuous.append(
            f"VACUOUS: only {len(tiered)} of {len(identifiers)} GSA identifier(s) classify to an "
            f"agent tier, below the floor of {MIN_TIER_GSA_IDENTIFIERS}. Property 3 skips untiered "
            f"identifiers, so a recogniser that stopped recognising tiers would skip everything."
        )

    if vacuous:
        return vacuous

    findings: list[str] = []

    # --- 2. no actor ServiceAccount carries a Workload Identity annotation -------------------
    for sa in actor_sas:
        if WI_ANNOTATION in sa.annotations:
            findings.append(
                f"{sa.where()} is labelled `{ROLE_LABEL}: actor` and carries "
                f"`{WI_ANNOTATION}` directly ({sa.annotations[WI_ANNOTATION]!r}). That annotation "
                f"is the entire Workload Identity binding: the actor's pod would exchange its "
                f"projected token for a Google credential on the first cloud call. 06 §2.3 gives "
                f"the actor a cloud GSA only once P10-T1's controls bound it; until then the actor's "
                f"authority is the broker-operations grant and nothing else."
            )
        leaked = sorted(sa.placeholders & placeholders)
        if leaked:
            findings.append(
                f"{sa.where()} is labelled `{ROLE_LABEL}: actor` and carries the placeholder "
                f"`${{{leaked[0]}}}`, which {COMMON} expands to a Workload Identity annotation. The "
                f"annotation key never appears in this file, so nothing that greps for "
                f"`{WI_ANNOTATION}` would see it -- and the edit that puts it here is moving one "
                f"line down the template, past the comment that says not to."
            )

    # --- 3. no GSA identifier classifies as (developer-team, actor) --------------------------
    # The allow-list is DERIVED, not written down: exactly the (tier, role) pairs whose §2.3 row
    # names a GSA. 09 §11.4 -- the permitted set is enumerated and everything else is a finding.
    allowed: set[tuple[str, str]] = set()
    spec_actor_gsas: dict[tuple[str, str], str] = {}
    for identity, gsa in rows:
        m = IDENTITY_CELL.match(identity)
        if not m or NONE_MARKER.match(gsa):
            continue
        named = BACKTICKED.search(gsa)
        if not named:
            continue
        allowed.add((m.group(1), m.group(2)))
        if m.group(2) == "actor":
            spec_actor_gsas[(m.group(1), m.group(2))] = named.group(1)

    probes = sorted(
        ((name, want) for want, name in spec_actor_gsas.items()), key=lambda p: p[0]
    ) + RECOGNISER_PROBES
    blind = [
        (name, want, classify(name)) for name, want in probes if classify(name) != want
    ]
    if blind:
        for name, want, got in blind:
            findings.append(
                f"VACUOUS: the recogniser cannot classify 06 §2.3's own GSA naming -- {name!r} "
                f"reads as {got} and must read as {want}. Property 3 reports that nothing in the "
                f"tree classifies as ({DEVTEAM}, actor); a recogniser that cannot produce that "
                f"verdict makes the report true and worthless."
            )
    else:
        for identifier in sorted(identifiers):
            tier, role = classify(identifier)
            if tier is None or (tier, role) in allowed:
                continue
            where = ", ".join(sorted(identifiers[identifier])[:3])
            findings.append(
                f"the GSA identifier {identifier!r} ({where}) classifies as ({tier}, {role}), which "
                f"06 §2.3 does not put in its GSA column -- the allow-list is {sorted(allowed)}. "
                f"R-06.2.3-6: the {DEVTEAM} actor has no cloud identity in v1, because a namespace "
                f"tier has no cloud write surface and the controls that would bound one (P10-T1) do "
                f"not exist yet. A new GSA is not a widening of a grant any other check watches; it "
                f"is a new subject no selector names, so every containment check stays green."
            )

    # --- 4. the developer-team cloud roles are read-only and non-empty -----------------------
    array = DEVTEAM_ROLES_ARRAY.search(provision_text)
    if not array:
        findings.append(
            f"{PROVISION} declares no `DEVELOPER_TEAM_ROLES=(...)` array. That array is the tier's "
            f"whole cloud authority and the only place this check can read it; if the roles moved, "
            f"move this property with them rather than leaving it asserting nothing."
        )
    else:
        roles = QUOTED_ROLE.findall(array.group(1))
        if not roles:
            findings.append(
                f"{PROVISION}: `DEVELOPER_TEAM_ROLES` is empty. An empty array satisfies any "
                f"allow-list, so 'the tier holds no write role' and 'the tier holds no role' would "
                f"report the same green -- and the second one is a tier whose reader cannot read "
                f"its own workloads' logs, which is a broken install rather than a safe one."
            )
        for role in roles:
            if role not in READ_ONLY_CLOUD_ROLES:
                findings.append(
                    f"{PROVISION}: `DEVELOPER_TEAM_ROLES` holds {role!r}, which is not in this "
                    f"check's read-only allow-list ({sorted(READ_ONLY_CLOUD_ROLES)}). The header "
                    f"three lines above the array states the rule: \"Both stay VIEWER-ONLY: "
                    f"invariant #1 says the agent's cloud identity never holds a write role -- the "
                    f"only mutation path is a reviewed GitOps PR.\" If {role!r} really is read-only, "
                    f"add it to READ_ONLY_CLOUD_ROLES with the sentence that says why."
                )

    # --- 5. "no cloud actor" is not "no actor at all" ----------------------------------------
    grant = files.get(DEVTEAM_ACTOR_GRANT)
    if grant is None:
        findings.append(
            f"{DEVTEAM_ACTOR_GRANT} is not in the corpus. The {DEVTEAM} tier is supposed to HAVE an "
            f"actor -- a Kubernetes one, holding 06 §2.2's read half and §2.2.1's broker-operations "
            f"grant -- and to have no CLOUD one. Deleting the actor outright is the cheapest way to "
            f"make properties 2 and 3 green, and it is exactly backwards."
        )
    else:
        labelled = [
            doc
            for doc in _documents(grant)
            if _pairs(_block(_block(doc, "metadata", 0), "labels", 2), 4).get(ROLE_LABEL) == "actor"
        ]
        if not labelled:
            findings.append(
                f"{DEVTEAM_ACTOR_GRANT} has no document carrying `{ROLE_LABEL}: actor`. Both "
                f"admission policies select on that label -- `vap-agent-readonly`'s single write "
                f"carve-out applies only to `actor` -- so an unlabelled grant is not denied, it is "
                f"invisible to the rules written to bound it. And this check would then be asserting "
                f"the absence of a cloud actor for a tier with no actor at all."
            )

    return findings


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────────────────────────────────────────────────────


def _inputs() -> tuple[str, str, dict[str, str]]:
    """06 §2.3, provision_04, and everything else, in that order.

    The whole worktree, via `gitcorpus.repo_files` -- tracked files PLUS untracked non-ignored ones,
    because a template that first grants the actor a GSA is untracked at exactly the moment this
    check most needs to see it ([[LSN-050]]). The two named inputs are lifted OUT of the corpus dict
    so that each text has exactly one home and a mutation cannot change one copy and leave the other.
    """
    corpus = read_repo_files(REPO)
    for rel in (SPEC, PROVISION, COMMON, DEVTEAM_ACTOR_GRANT, IDENTITY_TEMPLATE, SELF):
        if rel not in corpus:
            raise SystemExit(f"FAIL: V-CTN-039 -- {rel} is not in the repository corpus")
    for rel in [r for r in corpus if r.startswith(EXCLUDED_PREFIX)]:
        del corpus[rel]  # see the comment on EXCLUDED_PREFIX
    return corpus.pop(SPEC), corpus.pop(PROVISION), corpus


def run() -> int:
    spec_text, provision_text, corpus = _inputs()
    findings = check(spec_text, provision_text, corpus)
    if findings:
        print(
            "FAIL: V-CTN-039 -- the developer-team actor's cloud identity is not absent "
            "(R-06.2.3-6, 06 §2.3)",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    files = dict(corpus)
    files[SPEC] = spec_text
    files[PROVISION] = provision_text
    sas = service_accounts(files)
    actors = [sa for sa in sas if sa.role == "actor"]
    identifiers = gsa_identifiers(files)
    tiered = sorted(i for i in identifiers if classify(i)[0] is not None)
    roles = QUOTED_ROLE.findall(DEVTEAM_ROLES_ARRAY.search(provision_text).group(1))
    print(
        f"PASS: V-CTN-039 (L0) -- 06 §2.3's {len(parse_iam_table(spec_text))}-row Cloud IAM table "
        f"gives the {DEVTEAM} actor no GSA, and the tree agrees: none of {len(actors)} "
        f"actor-labelled ServiceAccount(s) (of {len(sas)}) carries a Workload Identity annotation "
        f"or a placeholder that renders one; none of {len(tiered)} tier-classified GSA "
        f"identifier(s) (of {len(identifiers)} swept) is ({DEVTEAM}, actor); DEVELOPER_TEAM_ROLES "
        f"holds {len(roles)} role(s), all read-only; and the tier's Kubernetes actor grant is still "
        f"there, labelled `{ROLE_LABEL}: actor`"
    )
    return 0


def _mutate(base: tuple[str, str, dict[str, str]], index: int, fn):
    out = list(base)
    out[index] = fn(out[index])
    return (out[0], out[1], out[2])


def _edit(rel: str, old: str, new: str, count: int = 1):
    """A corpus mutation: replace `old` with `new` in one file, leaving the rest untouched."""

    def apply(corpus: dict[str, str]) -> dict[str, str]:
        m = dict(corpus)
        m[rel] = m[rel].replace(old, new, count)
        return m

    return apply


def _drop(rel: str):
    def apply(corpus: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in corpus.items() if k != rel}

    return apply


def _drop_service_accounts(corpus: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in corpus.items() if "kind: ServiceAccount" not in v}


SILENT = object()  # the expectation "this mutation must produce NO findings"


def negative_control() -> int:
    """Each mutation is a way this check could go quiet, and each names the signal it must produce.

    Per-mutation needles, not "findings is non-empty" ([[LSN-035]]). Five properties overlap on the
    same handful of files -- the identity template holds both the actor SA of property 2 and, one
    document up, the reader whose annotation placeholder property 2's second arm looks for; 06 §2.3
    feeds property 1's row count AND property 3's allow-list AND the recogniser's self-test -- so
    "the check went red" is satisfied by whichever property is evaluated first and establishes
    nothing about the other four.

    One row expects SILENCE rather than a finding: the developer-team reader GSA spelled 06 §2.3's
    way instead of provisioning's. That drift is real and it is V-CTN-030's, not this check's, and a
    control that only ever asserts redness cannot show that the tolerance is still there. A check
    that fires on somebody else's defect is a check the next reader turns off.
    """
    base = _inputs()
    findings = check(*base)
    if findings:
        print("  BROKEN   the tree is not green, so no row below can be attributed")
        for f in findings[:4]:
            print(f"           {f}")
        print("FAIL: V-CTN-039 negative control -- 0 mutations evaluated")
        return 1

    mutations = [
        # ── property 1: the spec row, and the corpus floors ──────────────────────────────────
        (
            "06 §2.3's Cloud IAM table is renamed out from under the parser",
            _mutate(base, 0, lambda t: t.replace(
                "| Identity                  | GSA  ", "| Who                       | Account",
            )),
            "VACUOUS: 06 §2.3's Cloud IAM table did not parse",
        ),
        (
            "a seventh identity row appears in the table",
            _mutate(base, 0, lambda t: t.replace(
                "| developer-team **actor**  | **none in v1**",
                "| edge-team **reader**      | `kubeagents-edge-gsa`                      |"
                " `roles/monitoring.viewer` | namespace |\n"
                "| developer-team **actor**  | **none in v1**",
            )),
            "parses to 7 row(s)",
        ),
        (
            "06 §2.3 gives the developer-team actor a GSA -- the requirement itself changing",
            _mutate(base, 0, lambda t: t.replace(
                "| developer-team **actor**  | **none in v1**                             |",
                "| developer-team **actor**  | `kubeagents-devteam-<ns>-actor`            |",
            )),
            "row now names",
        ),
        (
            "the platform actor row stops naming a GSA, so `none in v1` says nothing",
            _mutate(base, 0, lambda t: t.replace(
                "| platform **actor**        | `kubeagents-platform-actor-gsa`            |",
                "| platform **actor**        | —                                          |",
            )),
            "row names no GSA",
        ),
        (
            "every ServiceAccount document leaves the corpus",
            _mutate(base, 2, _drop_service_accounts),
            "VACUOUS: found 0 ServiceAccount document(s)",
        ),
        (
            "common.sh stops rendering the annotation, so the placeholder arm has nothing to match",
            _mutate(base, 2, _edit(
                COMMON,
                'annotations="  annotations:\n    iam.gke.io/gcp-service-account:',
                'annotations="  annotations:\n    iam.gke.io/gcp-sa-do-not-grep:',
            )),
            "VACUOUS: no shell variable in",
        ),
        # ── property 2: the annotation, and the placeholder that hides it ────────────────────
        (
            "the actor ServiceAccount is handed a Workload Identity annotation outright",
            _mutate(base, 2, _edit(
                IDENTITY_TEMPLATE,
                "    kube-agents/tier: ${AGENT_TIER}\n    kube-agents/role: actor\n",
                "    kube-agents/tier: ${AGENT_TIER}\n    kube-agents/role: actor\n"
                "  annotations:\n"
                "    iam.gke.io/gcp-service-account: "
                '"kubeagents-devteam-actor-gsa@${PROJECT_ID}.iam.gserviceaccount.com"\n',
            )),
            "carries `iam.gke.io/gcp-service-account` directly",
        ),
        (
            "the reader's annotation PLACEHOLDER is moved onto the actor -- the key never appears",
            _mutate(base, 2, _edit(
                IDENTITY_TEMPLATE,
                "    kube-agents/tier: ${AGENT_TIER}\n    kube-agents/role: actor\n",
                "    kube-agents/tier: ${AGENT_TIER}\n    kube-agents/role: actor\n"
                "${AGENT_READER_ANNOTATIONS}\n",
            )),
            "expands to a Workload Identity annotation",
        ),
        # ── property 3: the sweep, and the recogniser that makes it non-vacuous ──────────────
        (
            "P10-T1 lands early: provisioning gains a developer-team actor GSA variable",
            _mutate(base, 1, lambda t: t.replace(
                'DEVELOPER_TEAM_GSA_NAME="${DEVELOPER_TEAM_GSA_NAME:-kubeagents-developer-team-gsa}"',
                'DEVELOPER_TEAM_GSA_NAME="${DEVELOPER_TEAM_GSA_NAME:-kubeagents-developer-team-gsa}"\n'
                'DEVELOPER_TEAM_ACTOR_GSA_NAME='
                '"${DEVELOPER_TEAM_ACTOR_GSA_NAME:-kubeagents-developer-team-actor-gsa}"',
            )),
            "classifies as (developer-team, actor)",
        ),
        (
            "06 §2.3 renames the cluster-admin actor GSA to something the recogniser cannot read",
            _mutate(base, 0, lambda t: t.replace(
                "`kubeagents-cluster-admin-<cluster>-actor`", "`kubeagents-ca-<c>-writer`      ",
            )),
            "the recogniser cannot classify 06 §2.3's own GSA naming",
        ),
        (
            "TOLERATED: the developer-team reader takes 06 §2.3's spelling (V-CTN-030's defect)",
            _mutate(base, 1, lambda t: t.replace(
                "kubeagents-developer-team-gsa", "kubeagents-devteam-team-x-gsa",
            )),
            SILENT,
        ),
        # ── property 4: the cloud roles ──────────────────────────────────────────────────────
        (
            "DEVELOPER_TEAM_ROLES picks up a write role",
            _mutate(base, 1, lambda t: t.replace(
                '  "roles/logging.viewer"\n)', '  "roles/logging.viewer"\n  "roles/container.developer"\n)',
            )),
            "'roles/container.developer', which is not in this check's read-only allow-list",
        ),
        (
            "DEVELOPER_TEAM_ROLES is emptied, which satisfies any allow-list",
            _mutate(base, 1, lambda t: t.replace(
                '  "roles/container.viewer"\n  "roles/monitoring.viewer"\n  "roles/logging.viewer"\n)',
                ")",
            )),
            "`DEVELOPER_TEAM_ROLES` is empty",
        ),
        # ── property 5: the Kubernetes actor that must still be there ────────────────────────
        (
            "the developer-team actor grant loses its `role: actor` label",
            _mutate(base, 2, _edit(
                DEVTEAM_ACTOR_GRANT, "    kube-agents/role: actor\n", "", count=2,
            )),
            "has no document carrying `kube-agents/role: actor`",
        ),
        (
            "the developer-team actor is deleted outright, which is the greenest edit of all",
            _mutate(base, 2, _drop(DEVTEAM_ACTOR_GRANT)),
            "is not in the corpus",
        ),
    ]

    failures = 0
    for name, args, expect in mutations:
        # A mutation that did not change its input cannot be evaluated: the unmutated base is
        # re-checked, comes back clean, and the row prints MISS -- the verdict for "the check let
        # the defect through" -- over a defect that was never applied ([[LSN-063]]).
        if args == base:
            failures += 1
            print(f"  BROKEN  {name}")
            print("           the mutation did not change its input; nothing was evaluated")
            continue
        found = check(*args)
        if expect is SILENT:
            if found:
                failures += 1
                print(f"  MISS    {name}")
                print(f"           expected silence; got {found[:2]}")
            else:
                print(f"  ignored {name}")
            continue
        hit = any(expect in f for f in found)
        print(f"  {'caught ' if hit else 'MISS   '} {name}")
        if not hit:
            failures += 1
            print(f"           expected a finding containing {expect!r}; got {found[:2] or 'none'}")

    print(
        f"{'PASS' if not failures else 'FAIL'}: V-CTN-039 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations resolved as expected, across all "
        f"five properties"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
