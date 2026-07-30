#!/usr/bin/env python3
"""V-BRK-013 (L0): the broker-operations grant is written down once, in 06 §2.2.1, and every copy
of it in the tree is exactly that -- no wider, no narrower.

06 §2.2.1 names three properties of this rule set as load-bearing and says they "are asserted
separately (09 §6.14, `V-BRK-013`)". This is that assertion. The three:

  * `create` but never `update`/`delete` on `actionrecords`. The broker appends to the journal and
    advances `status`; it can never rewrite or remove a record, including its own. An actor that can
    update an ActionRecord can rewrite the evidence of what it did, which turns invariant 3 -- every
    action is journalled -- into a claim about a mutable log.
  * `fleetfreezes` readable by EVERY tier. A tier that cannot read the freeze object fails closed
    permanently (06 §4.4). Dropping this grant does not fail safe; it bricks the tier.
  * The grant is identical across tiers and confers nothing over tenant resources.

None of the three survives being restated. The grant currently appears in SEVEN places -- the spec,
the canonical VAP and its two materialized copies, the tier overlays, the per-cluster applied
instances, the cascade templates the platform and cluster-admin agents render for clusters and
namespaces that do not exist yet, and `k8s-operator/scripts/*.yaml.template`, which is the copy the
provisioning path actually renders onto a live cluster. Seven copies of an allow-list is six chances
for one of them to gain a verb, and the one that gains it is the one enforced in the cluster nobody
is looking at. So the copies are not asked to agree with each other -- two copies agreeing is not the
property that matters -- they are each checked against the spec.

WHY THIS CANNOT BE LEFT TO REVIEW. A wrong triple in an RBAC file is not a compile error, is not a
runtime error, and is not visible from either side: the Role looks right, the VAP that is supposed to
bound it looks right, and if they drifted together they agree with each other. The API server never
complains -- RBAC is a union, so an extra rule silently grants, and an allow-list entry for a triple
nobody requests silently does nothing. The only artifact that disagrees is a design document, and
design documents do not run.

TWO DEFINITION SITES, ONE PER TIER DIMENSION. 06 §2.2 gives three per-tier ACTOR templates -- what
an agent **acts on**, different for each of `developer-team`, `cluster-admin`, `platform` -- and
§2.2.1 gives the broker-operations grant every actor identity **additionally** receives, byte-
identical across tiers. So what an actor object is owed depends on whether it is stamped with a
tier:

  * A **tier-neutral** actor object (no `kube-agents/tier`) is the shared broker-operations pair. It
    is owed §2.2.1 and nothing else. That is every actor Role/ClusterRole in the tree today.
  * A **tier-stamped** actor object is owed its tier's §2.2 template PLUS §2.2.1's grant.

The tier arm describes a tree that does not exist yet -- nothing renders 06 §2.2's templates, which
is why the platform tier cannot list Secrets and the material-egress scan of 06 §4.2 fails closed on
a real cluster (P9-T9b-5b-0-ii). The arm is written now, and separately from the render that will
satisfy it, because a check may not be changed in the same unit as the implementation whose failure
motivated the change. Both arms are exercised: the tier-neutral one by the real tree, the tier one by
the negative control, which synthesises the object the render will emit.

Seven properties:

  1. THE SPEC PARSES, AND IS THE ONLY DEFINITION SITE. 06 §2.2.1's fenced YAML block expands to a set
     of (apiGroup, resource, verb) triples, and each of 06 §2.2's three fenced templates expands to
     one more, keyed by the tier its own labels declare. Everything else is compared against those.
  2. EVERY VAP COPY'S ACTOR ALLOW-LIST EQUALS THE BROKER-OPERATIONS GRANT, IN BOTH DIRECTIONS. A
     missing triple is a policy that denies a legitimate actor identity; an extra one is a hole.
     Copies are DISCOVERED BY GLOB, not by a list of paths ([[LSN-036]], [[LSN-038]]) -- the cascade
     template renders a fourth copy into every cluster the fleet grows, and a hardcoded list would
     not know about it.
  3. NO ACTOR RBAC OBJECT GRANTS ANYTHING OUTSIDE WHAT ITS TIER IS OWED. Every Role/ClusterRole
     labelled `kube-agents/role: actor` is checked rule by rule against the set above. This is the
     property that fails if someone lands a tenant grant by editing an RBAC file rather than by
     rendering the template 06 §2.2 actually specifies -- and, for a tier-stamped object, if the
     rules drift from that template by one resource.
  4. WHAT A TIER IS OWED IS FULLY REALISED. Per tier, the UNION of that tier's actor objects must
     EQUAL what the tier is owed, not merely be contained in it. Containment alone is satisfied by an
     empty tree, and by the far more likely accident of a rule silently dropped in one of six copies
     -- 06 §2.2.1's point about `fleetfreezes` is that the missing-grant direction is the one that
     bricks a tier. A union rather than a per-object equality because the grant is deliberately SPLIT
     across a ClusterRole and a Role: cluster-scoped reads above, persisted writes below. Realisation
     is only demanded of a tier that has at least one object; a tier with none has not been rendered
     yet, and a check that fails until an unrelated future unit lands is a check that would have to
     be deferred -- which 09 §9.6 forbids for this one.
  5. NON-VACUITY. The spec blocks must parse to non-trivial grants, and the globs must actually find
     VAP copies and actor objects. A check that found nothing to compare would print PASS forever
     after a refactor moved the tree it reads ([[LSN-035]]).
  6. NOTHING IN THE TREE GRANTS WHAT ADMISSION WOULD REJECT. `vap-agent-readonly` validation 3 bounds
     every actor object to a literal allow-list, under `failurePolicy: Fail`. By property 2 that
     allow-list is §2.2.1's grant -- so a tier-stamped actor object carrying its 06 §2.2 rules is
     DENIED AT APPLY TIME by the very policy this check keeps honest. The spec permits the object and
     the installed policy refuses it, and the two disagreeing quietly is how a render lands that no
     cluster will ever accept. 06 §2.2 names `vap-agent-scope` as the validator for the tier
     templates and it does not exist (P10-T1); until the bound moves, this property is the standing
     record of what the render owes.
  7. PROPERTY 6'S PREMISE, ASSERTED RATHER THAN ASSUMED. Property 6 reduces "admission would reject
     it" to "it is outside the allow-list" only because `variables.isActor` selects on the actor
     label ALONE, with no tier condition -- so validation 3 governs tier-stamped objects too. Narrow
     `isActor`, and the reduction is silently false and property 6 silently over-reports. The
     expression is therefore compared, in every copy, against the one form the reduction holds for.

FIXTURES. Objects named `vaptest-*` are admission fixtures: policy/tests/ is full of actor-labelled
Roles that deliberately violate the grant, and feeding them to property 3 would make this check fail
on a correct tree. They are skipped by NAME rather than by directory, and the escape is then closed
from the other side -- property 3 also fails if a `vaptest-` object appears anywhere but
policy/tests/, so the prefix cannot be used to walk a real grant past the check.

Self-test (the `¬` of 09 §6): `--negative-control` applies each of eight plausible regressions to a
copy of the sources in memory and confirms this check reports every one.

Run:  python3 dev/tests/actor-grant-single-sourced.py
      python3 dev/tests/actor-grant-single-sourced.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gitcorpus import repo_files  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

SPEC = "docs/design/06-api-and-data-contracts.md"
# The fence is located by the heading it follows, not by an index into the file's code blocks. A
# document gains and loses fences constantly; it gains a second `#### 2.2.1` never.
SPEC_HEADING = "#### 2.2.1 Broker operations grant"
# §2.2's three per-tier ACTOR templates live between this heading and §2.2.1's. The span holds other
# fences too -- a Go code constant for the kube-system add-on allowlist among them -- so the tier
# blocks are selected by what they CONTAIN (an actor label and a tier label), never by position.
SPEC_TIER_HEADING = "### 2.2 Actor templates (3 tiers)"

# The label that makes a Role/ClusterRole an ACTOR identity. Same discriminator the admission policy
# uses (`variables.isActor`), so the check and the runtime enforcement agree by construction rather
# than by two independent guesses.
ACTOR_LABEL = "kube-agents/role: actor"
# The label that says which of 06 §2.2's three templates an actor object is answerable to. Absent on
# the shared broker-operations pair, deliberately and by that object's own comment: stamping a tier
# on a tier-neutral object would be a lie `vap-agent-readonly` validation 2 would then enforce.
TIER_LABEL = re.compile(r"kube-agents/tier:\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
TIERS = ("developer-team", "cluster-admin", "platform")

# Admission fixtures. See FIXTURES in the module docstring.
FIXTURE_PREFIX = "vaptest-"
FIXTURE_DIR = "examples/gitops-repo/policy/tests/"

# Where a copy of the policy can legitimately live. Globs, not paths.
VAP_GLOBS = ("**/vap-agent-readonly.yaml", "**/vap-agent-readonly.yaml.tmpl")

# Non-vacuity floors. Low enough not to be a maintenance burden, high enough that a glob which
# stopped matching cannot pass.
MIN_SPEC_TRIPLES = 10
MIN_VAP_COPIES = 3
MIN_ACTOR_OBJECTS = 6
# Per-tier floors. The narrowest of the three templates (developer-team) is well over a hundred
# triples, so a floor of 20 says only "this fence was read", which is all a vacuity floor is for.
MIN_TIER_TRIPLES = 20

# A flow sequence: `[a, b]` or `["a", "b"]`, on one line or reflowed across several. The only form
# either the spec blocks or the RBAC files use, and the only form accepted -- see parse_rules.
FLOW_SEQ = re.compile(r"^\[(.*)\]$", re.DOTALL)

# `variables.isActor`, in the one shape property 6's reduction holds for: the actor label alone, with
# no tier condition. Compared after collapsing whitespace, because the expression is a folded YAML
# scalar and Prettier owns where it wraps.
ISACTOR_ANCHOR = "- name: isActor"
ISACTOR_CANONICAL = (
    "'kube-agents/role' in object.metadata.labels && "
    "object.metadata.labels['kube-agents/role'] == 'actor'"
)

# The literal allow-list inside the VAP's actor validation. Anchored on the CEL expression that
# consumes it so a quoted string elsewhere in the file cannot be mistaken for a grant entry.
VAP_ALLOWLIST = re.compile(
    r"\(g \+ '/' \+ res \+ ':' \+ v\)\s*in\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)
VAP_TRIPLE = re.compile(r"'([^']+)'")


class GrantSyntaxError(Exception):
    """A rules block is not in the shape this check reads. Loud, on purpose.

    Silence here is the failure mode that matters: a parser that shrugs at a rule it cannot read
    reports a smaller grant than the file grants, and a smaller grant passes property 3 for the
    wrong reason. If this fires, widen the parser -- do not reshape the file to suit it, and above
    all do not let it skip.
    """


def triple(group: str, resource: str, verb: str) -> str:
    """The (apiGroup, resource, verb) key, spelled the way the VAP's CEL spells it.

    One encoding for both sides of the comparison so the two cannot disagree about how a subresource
    or the core (empty) group is written.
    """
    return f"{group}/{resource}:{verb}"


def _flow_items(value: str, where: str) -> list[str]:
    m = FLOW_SEQ.match(value.strip())
    if not m:
        raise GrantSyntaxError(
            f"{where}: expected a flow sequence like [a, b], got {value.strip()!r}. "
            f"Block sequences are not read by this check and would be silently dropped."
        )
    body = m.group(1).strip()
    if not body:
        return []
    # A reflowed sequence arrives with newlines still in it and a trailing comma before the `]`;
    # both are Prettier's, not the author's. The empty-item filter absorbs the trailing comma.
    return [item.strip().strip("\"'") for item in body.split(",") if item.strip()]


def _strip_comment(line: str) -> str:
    """Drop a trailing YAML comment, respecting quotes.

    The spec block annotates most of its rules (`# step 1: authenticate the calling agent`), and the
    RBAC files annotate several. An unstripped `#` lands inside the last verb.
    """
    sq = dq = 0
    for i, ch in enumerate(line):
        if ch == "'" and dq % 2 == 0:
            sq += 1
        elif ch == '"' and sq % 2 == 0:
            dq += 1
        elif ch == "#" and sq % 2 == 0 and dq % 2 == 0 and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _join_flow_sequences(block: str, where: str) -> list[str]:
    """Collapse a flow sequence Prettier reflowed across several lines back onto one.

    06 §2.2's tier templates have long `resources:` lists, and Prettier breaks any flow sequence that
    will not fit:

        resources:
          [
            pods,
            services,
          ]

    §2.2.1's block is short enough that Prettier never breaks it, which is why this did not exist
    before. Joining is done HERE, on physical lines, and not by making the key/value reader tolerant
    of newlines: a `resources:` with nothing after it is indistinguishable from the start of a BLOCK
    sequence until you have counted brackets, and quietly reading a block sequence as empty is
    exactly the silently-smaller-grant failure GrantSyntaxError exists to prevent. Depth only ever
    rises at a `[`, so a bare `resources:` still reaches _flow_items and still raises.

    Comments are stripped per physical line before joining, so an annotation on a rule cannot swallow
    the continuation lines that follow it.
    """
    lines = [_strip_comment(raw).rstrip() for raw in block.splitlines()]
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            out.append(line)
            continue
        # `key:` with nothing after it, and the next line opening a flow sequence, is one reflowed
        # key/value pair. `key:` followed by anything else -- a `- item`, another key -- is NOT, and
        # is left alone so that _flow_items raises on it.
        if line.endswith(":") and i < len(lines) and lines[i].lstrip().startswith("["):
            line = f"{line} {lines[i].lstrip()}"
            i += 1
        depth = _bracket_delta(line)
        while depth > 0 and i < len(lines):
            line = f"{line} {lines[i].strip()}"
            depth += _bracket_delta(lines[i])
            i += 1
        if depth > 0:
            raise GrantSyntaxError(
                f"{where}: a flow sequence is still open at the end of the block ({depth} unclosed "
                f"`[`). Reading on would fold the next rule into this one and report a grant that "
                f"nothing in the tree holds."
            )
        if depth < 0:
            raise GrantSyntaxError(
                f"{where}: a `]` closes a flow sequence that never opened, near {line.strip()!r}."
            )
        out.append(line)
    return out


def _bracket_delta(line: str) -> int:
    """Net `[` minus `]` on one line, ignoring brackets inside quotes."""
    depth = 0
    quote = ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def parse_rules(block: str, where: str) -> list[dict[str, list[str]]]:
    """Read an RBAC `rules:` body into a list of {key: [values]}.

    A narrow reader rather than a YAML library because this runs in the L0 chain and L0 installs no
    dependencies -- the same call dev/tests/yamlsubset.py documents at length. The accepted shape is
    the one every rules block in this repo is written in:

        - apiGroups: ["a", "b"]     # or [a, b], or the same list reflowed over several lines
          resources: ["x"]
          verbs: ["get"]

    Anything else raises rather than parsing partially.
    """
    rules: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    # Lines here are LOGICAL: a flow sequence Prettier broke over several physical lines arrives as
    # one, so an error message quotes what the parser saw rather than a fragment of it.
    for line in _join_flow_sequences(block, where):
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            current = {}
            rules.append(current)
            stripped = stripped[2:]
        elif current is None:
            raise GrantSyntaxError(f"{where}: content before the first `- ` rule item: {line!r}")
        if ":" not in stripped:
            raise GrantSyntaxError(f"{where}: not a `key: value` line: {line!r}")
        key, _, value = stripped.partition(":")
        current[key.strip()] = _flow_items(value, where)
    return rules


def triples_of(rules: list[dict[str, list[str]]], where: str) -> set[str]:
    out: set[str] = set()
    for rule in rules:
        if "nonResourceURLs" in rule:
            # Not expressible as a (group, resource, verb) triple, and not in the grant. Surfacing it
            # as a syntax error rather than skipping keeps the "silently smaller grant" failure shut.
            raise GrantSyntaxError(
                f"{where}: a rule uses nonResourceURLs, which the grant does not contain and this "
                f"check cannot express as a triple."
            )
        for g in rule.get("apiGroups", []):
            for res in rule.get("resources", []):
                for verb in rule.get("verbs", []):
                    out.add(triple(g, res, verb))
    return out


def spec_grant(text: str) -> tuple[set[str], list[str]]:
    """The grant, read from 06 §2.2.1's fenced YAML block. The single definition site."""
    idx = text.find(SPEC_HEADING)
    if idx < 0:
        return set(), [
            f"VACUOUS: {SPEC} no longer contains the heading {SPEC_HEADING!r}, so there is no "
            f"definition site to read and every comparison below would be against the empty set. "
            f"Fix the locator, not the spec."
        ]
    fence = re.search(r"```ya?ml\n(.*?)```", text[idx:], re.DOTALL)
    if not fence:
        return set(), [
            f"VACUOUS: no fenced YAML block follows {SPEC_HEADING!r} in {SPEC}. The grant moved out "
            f"of a code fence, and a check that reads no rules passes on every wrong copy."
        ]
    try:
        rules = parse_rules(fence.group(1), f"{SPEC} §2.2.1")
        grant = triples_of(rules, f"{SPEC} §2.2.1")
    except GrantSyntaxError as e:
        return set(), [str(e)]
    if len(grant) < MIN_SPEC_TRIPLES:
        return grant, [
            f"VACUOUS: 06 §2.2.1 parsed to only {len(grant)} triple(s), below the floor of "
            f"{MIN_SPEC_TRIPLES}. Either the block shrank or the parser stopped reading it."
        ]
    return grant, []


def spec_tier_templates(text: str) -> tuple[dict[str, set[str]], list[str]]:
    """06 §2.2's three per-tier ACTOR templates, keyed by the tier each one labels itself with.

    The tier comes out of the block's OWN labels rather than out of the order the blocks appear in.
    Position is not a property of the spec: §2.2's span also carries a Go code fence for the
    kube-system add-on allowlist and a prose table, and an index into "the fenced blocks after this
    heading" is one inserted example away from reading the wrong one and reporting it confidently.
    """
    start = text.find(SPEC_TIER_HEADING)
    if start < 0:
        return {}, [
            f"VACUOUS: {SPEC} no longer contains the heading {SPEC_TIER_HEADING!r}, so the per-tier "
            f"actor templates have no definition site and property 3 would hold a tier-stamped "
            f"object to the broker-operations grant alone. Fix the locator, not the spec."
        ]
    end = text.find(SPEC_HEADING, start)
    if end < 0:
        end = len(text)
    span = text[start:end]

    templates: dict[str, set[str]] = {}
    failures: list[str] = []
    for fence in re.findall(r"```ya?ml\n(.*?)```", span, re.DOTALL):
        if ACTOR_LABEL not in fence:
            continue
        tier_m = TIER_LABEL.search(fence)
        if not tier_m:
            failures.append(
                f"{SPEC} §2.2: a fenced actor template carries no `kube-agents/tier` label, so there "
                f"is no tier to hold an object to. Every template in §2.2 is per-tier by definition."
            )
            continue
        tier = tier_m.group(1)
        rules_m = re.search(r"^rules:\n(.*)\Z", fence, re.DOTALL | re.MULTILINE)
        if not rules_m:
            failures.append(f"{SPEC} §2.2: the {tier} actor template has no `rules:` body.")
            continue
        try:
            got = triples_of(
                parse_rules(rules_m.group(1), f"{SPEC} §2.2 {tier}"), f"{SPEC} §2.2 {tier}"
            )
        except GrantSyntaxError as e:
            failures.append(str(e))
            continue
        if tier in templates:
            failures.append(
                f"{SPEC} §2.2: two fenced actor templates both label themselves {tier!r}. One of "
                f"them is not the template that tier's objects will be held to, and this check "
                f"cannot tell which."
            )
            continue
        if len(got) < MIN_TIER_TRIPLES:
            failures.append(
                f"VACUOUS: {SPEC} §2.2's {tier} template parsed to only {len(got)} triple(s), below "
                f"the floor of {MIN_TIER_TRIPLES}. Either the block shrank or the parser stopped "
                f"reading it, and a tier held to a nearly-empty template is held to nothing."
            )
        templates[tier] = got

    missing = [t for t in TIERS if t not in templates]
    if missing:
        failures.append(
            f"VACUOUS: {SPEC} §2.2 is titled 'Actor templates (3 tiers)' but no fenced block labels "
            f"itself with tier(s) {', '.join(missing)}. A tier with no template is a tier whose "
            f"actor objects this check would silently hold to the broker-operations grant alone."
        )
    return templates, failures


def split_documents(text: str) -> list[str]:
    """YAML documents, split on a `---` at column zero. Sufficient for these files and no more."""
    return re.split(r"^---\s*$", text, flags=re.MULTILINE)


def _doc_tier(doc: str) -> str:
    """The `kube-agents/tier` an object stamps on itself, or "" for a tier-neutral one.

    Comments are stripped from the whole document before looking, because the tier-neutral objects
    announce themselves with the words "No `kube-agents/tier`" and a regex that read prose would
    conclude the opposite of what the prose says.
    """
    body = "\n".join(_strip_comment(line) for line in doc.splitlines())
    m = TIER_LABEL.search(body)
    return m.group(1) if m else ""


def rbac_documents(text: str) -> list[tuple[str, str, str, str]]:
    """(kind, name, tier, rules-block) for each Role/ClusterRole document carrying the actor label."""
    out: list[tuple[str, str, str, str]] = []
    for doc in split_documents(text):
        m = re.search(r"^kind:\s*(Role|ClusterRole)\s*$", doc, re.MULTILINE)
        if not m:
            continue
        if ACTOR_LABEL not in doc:
            continue
        name_m = re.search(r"^  name:\s*(\S+)\s*$", doc, re.MULTILINE)
        rules_m = re.search(r"^rules:\n(.*)\Z", doc, re.DOTALL | re.MULTILINE)
        out.append(
            (
                m.group(1),
                name_m.group(1) if name_m else "<unnamed>",
                _doc_tier(doc),
                rules_m.group(1) if rules_m else "",
            )
        )
    return out


def read_sources() -> dict[str, str]:
    """Every file this check reads, keyed by repo-relative path.

    Enumerated with `git ls-files --cached --others --exclude-standard`: tracked files PLUS untracked
    ones that are not ignored. Both halves are deliberate.

    Not tracked-only, because a brand-new RBAC file is untracked at exactly the moment this check
    most needs to see it -- the pre-commit run. A tracked-only listing would report PASS on a working
    tree containing a wide-open actor Role and only notice after it was committed, which is the
    [[LSN-035]] shape: the check runs, prints PASS, and its subject was never in scope.

    Not a plain rglob either, because ignored paths are not the fleet's policy: docs/site/node_modules
    is enormous, and `k8s-operator/scripts/vars.sh` is gitignored precisely because it holds live
    secrets. Whatever this check reads, it may print in a failure message.
    """
    sources: dict[str, str] = {}
    for rel in repo_files(REPO):
        if rel != SPEC and not _is_manifest(rel):
            continue
        path = REPO / rel
        try:
            sources[rel] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return sources


def _is_manifest(rel: str) -> bool:
    """Every extension a Kubernetes manifest is written under in this tree.

    `.yaml.template` is the one the provisioning path renders (`k8s-operator/scripts/`), and it was
    outside this corpus until the tier arm was written -- so the copy of the grant that actually
    lands on a live cluster was the one copy nothing compared to the spec. It is also where 06 §2.2's
    per-tier templates will be rendered from, which would have made the tier arm describe a tree it
    could not see ([[LSN-036]], [[LSN-050]]).
    """
    return rel.endswith((".yaml", ".yaml.tmpl", ".yaml.template"))


def _is_vap_copy(rel: str) -> bool:
    p = pathlib.PurePosixPath(rel)
    return any(p.match(g) for g in VAP_GLOBS)


def check(sources: dict[str, str]) -> list[str]:
    failures: list[str] = []

    spec_text = sources.get(SPEC)
    if spec_text is None:
        return [f"VACUOUS: {SPEC} was not read at all; the definition site is missing."]
    grant, spec_failures = spec_grant(spec_text)
    if spec_failures:
        return spec_failures
    templates, tier_failures = spec_tier_templates(spec_text)
    failures.extend(tier_failures)

    def owed(tier: str) -> set[str]:
        """What an actor object stamped with `tier` may hold. 06 §2.2 ∪ §2.2.1.

        06 §2.2.1: the three templates "cover what an agent acts on. They do not cover what the
        broker needs to run its own pipeline"; every actor identity "additionally receives exactly
        this rule set". Additionally -- so the union, and for a tier-neutral object the grant alone.
        """
        return grant | templates.get(tier, set())

    # --- property 2: every VAP copy's allow-list equals the grant --------------------------------
    vap_copies = sorted(rel for rel in sources if _is_vap_copy(rel))
    admissible: set[str] | None = None  # intersection over copies; property 6's bound
    if len(vap_copies) < MIN_VAP_COPIES:
        failures.append(
            f"VACUOUS: found {len(vap_copies)} copy/copies of vap-agent-readonly, below the floor of "
            f"{MIN_VAP_COPIES}. The globs {VAP_GLOBS} stopped matching, so the policy that bounds "
            f"actor RBAC is no longer being compared to anything."
        )
    for rel in vap_copies:
        m = VAP_ALLOWLIST.search(sources[rel])
        if not m:
            failures.append(
                f"{rel}: no actor allow-list found. The CEL that consumes it "
                f"(`(g + '/' + res + ':' + v) in [...]`) is gone or reshaped, which means this copy "
                f"of the policy imposes no bound on an actor Role at all -- and every negative "
                f"fixture aimed at validation 3 would be admitted by it."
            )
            continue
        found = set(VAP_TRIPLE.findall(m.group("body")))
        # Admission is conjunctive: a triple must survive EVERY policy that matches the object, so
        # the bound property 6 measures against is the intersection, not the union. A tier grant
        # admitted by one copy and rejected by another is rejected.
        admissible = found if admissible is None else (admissible & found)

        # --- property 7: property 6's premise, in this copy -------------------------------------
        iso = sources[rel].find(ISACTOR_ANCHOR)
        if iso < 0:
            failures.append(
                f"{rel}: no `{ISACTOR_ANCHOR}` variable. The actor discriminator this check and the "
                f"policy are supposed to share by construction is gone, so which objects validation "
                f"3 governs is now a guess -- and property 6 reduces admission to the allow-list "
                f"only while that discriminator is the actor label alone."
            )
        else:
            tail = sources[rel][iso + len(ISACTOR_ANCHOR) :]
            stop = min(
                (i for i in (tail.find("\n  validations:"), tail.find("\n    - name:")) if i >= 0),
                default=len(tail),
            )
            expr = tail[:stop].partition("expression:")[2].lstrip().lstrip(">|-").strip()
            if " ".join(expr.split()) != ISACTOR_CANONICAL:
                failures.append(
                    f"{rel}: `isActor` is no longer the bare actor-label test. Property 6 says a "
                    f"tier-stamped actor object is denied at apply time BECAUSE validation 3 governs "
                    f"it with no tier condition; narrow `isActor` and that stops being true and this "
                    f"check starts over-reporting. Re-derive property 6, then update "
                    f"ISACTOR_CANONICAL. Got: {' '.join(expr.split())!r}"
                )

        for extra in sorted(found - grant):
            failures.append(
                f"{rel}: allow-list admits {extra!r}, which 06 §2.2.1 does not grant. An actor Role "
                f"carrying it would be admitted by this policy."
            )
        for missing in sorted(grant - found):
            failures.append(
                f"{rel}: allow-list omits {missing!r}, which 06 §2.2.1 grants. This policy denies a "
                f"legitimate actor identity at apply time."
            )

    # --- properties 3, 4 and 6: actor RBAC objects -----------------------------------------------
    realised: dict[str, set[str]] = {}
    actor_objects = 0
    for rel in sorted(sources):
        if not _is_manifest(rel):
            continue
        if ACTOR_LABEL not in sources[rel]:
            continue
        for kind, name, tier, block in rbac_documents(sources[rel]):
            if name.startswith(FIXTURE_PREFIX):
                if not rel.startswith(FIXTURE_DIR):
                    failures.append(
                        f"{rel}: {kind}/{name} uses the {FIXTURE_PREFIX!r} fixture prefix outside "
                        f"{FIXTURE_DIR}. That prefix is how this check knows to skip admission "
                        f"fixtures; used anywhere else it is a way to walk a real grant past "
                        f"property 3."
                    )
                continue
            actor_objects += 1
            try:
                got = triples_of(parse_rules(block, f"{rel} {kind}/{name}"), f"{rel} {kind}/{name}")
            except GrantSyntaxError as e:
                failures.append(str(e))
                continue
            if not got:
                failures.append(
                    f"{rel}: {kind}/{name} carries `{ACTOR_LABEL}` but grants nothing this check "
                    f"could read. An actor object with no readable rules is indistinguishable from "
                    f"one this parser failed on."
                )
                continue
            if tier and tier not in templates:
                failures.append(
                    f"{rel}: {kind}/{name} stamps itself `kube-agents/tier: {tier}`, which 06 §2.2 "
                    f"has no actor template for. Either the label is wrong or the spec owes this "
                    f"tier a template; either way there is nothing to hold the object to."
                )
                continue

            realised.setdefault(tier, set())
            realised[tier] |= got

            # --- property 3: nothing outside what the tier is owed ------------------------------
            for extra in sorted(got - owed(tier)):
                if tier:
                    failures.append(
                        f"{rel}: {kind}/{name} (tier {tier}) grants {extra!r}, which is in neither "
                        f"06 §2.2's {tier} actor template nor §2.2.1's broker-operations grant. An "
                        f"actor identity holds the union of those two and nothing else."
                    )
                else:
                    failures.append(
                        f"{rel}: {kind}/{name} grants {extra!r}, which is outside 06 §2.2.1's "
                        f"broker-operations grant. This object carries no `kube-agents/tier`, so it "
                        f"is the shared grant, which is tier-neutral and owed §2.2.1 alone -- a "
                        f"tenant rule belongs on a tier-stamped object rendered from 06 §2.2."
                    )

            # --- property 6: nothing admission would reject -------------------------------------
            if admissible is not None:
                for rejected in sorted(got - admissible):
                    failures.append(
                        f"{rel}: {kind}/{name} grants {rejected!r}, which no installed "
                        f"vap-agent-readonly admits. `isActor` selects on the actor label alone, so "
                        f"validation 3 governs this object under failurePolicy: Fail and the API "
                        f"server will REFUSE the apply -- the spec permits the rule and the shipped "
                        f"policy does not. 06 §2.2 names `vap-agent-scope` as the validator for the "
                        f"tier templates and it does not exist yet (P10-T1); until the bound moves, "
                        f"a tier-stamped actor object cannot be rendered onto a cluster."
                    )

    if actor_objects < MIN_ACTOR_OBJECTS:
        failures.append(
            f"VACUOUS: found {actor_objects} actor RBAC object(s), below the floor of "
            f"{MIN_ACTOR_OBJECTS}. Either the tree lost its `{ACTOR_LABEL}` labels or the document "
            f"splitter stopped seeing them; both make a PASS here meaningless."
        )
    elif not failures or realised:
        # --- property 4: per tier, the union EQUALS what that tier is owed ------------------------
        # Only for a tier that has objects. A tier with none has not been rendered yet, and demanding
        # it here would make V-BRK-013 fail until an unrelated unit lands -- which for a
        # BLOCKING-ALWAYS check is not a deferral that 09 §9.6 permits, it is a red gate.
        for tier in sorted(realised):
            for missing in sorted(owed(tier) - realised[tier]):
                where = (
                    f"06 §2.2's {tier} actor template or §2.2.1"
                    if tier
                    else "06 §2.2.1"
                )
                who = f"tier-{tier} actor" if tier else "tier-neutral actor"
                failures.append(
                    f"no {who} RBAC object in the tree grants {missing!r}, which {where} requires. A "
                    f"grant that is merely CONTAINED in the spec is satisfied by an empty tree; the "
                    f"missing-grant direction is the one that fails closed permanently (06 §4.4 on "
                    f"`fleetfreezes`), so it is checked too."
                )

    return failures


def negative_control() -> int:
    sources = read_sources()

    def edit(s: dict[str, str], rel: str, old: str, new: str) -> dict[str, str]:
        return {**s, rel: s[rel].replace(old, new, 1)}

    canon_vap = "examples/gitops-repo/policy/vap-agent-readonly.yaml"
    canon_rbac = "examples/gitops-repo/policy/rbac-overlay/broker-operations.yaml"
    boot_vap = "examples/gitops-repo/clusters/cluster-a/bootstrap/20-policy/vap-agent-readonly.yaml"
    overlay = "examples/gitops-repo/policy/rbac-overlay/platform.yaml"

    # --- the tier arm's subject, which the real tree does not contain yet -------------------------
    # Nothing renders 06 §2.2's templates, so properties 3, 4 and 6 have no tier-stamped object to
    # run against and would be unexercised prose ([[LSN-035]]). The control synthesises the object
    # P9-T9b-5b-0-ii will render, built FROM the spec rather than from a literal copied out of it --
    # a literal would be a fourth definition site, and the one thing this check exists to forbid.

    def fence_rules(tier: str) -> str:
        start = sources[SPEC].find(SPEC_TIER_HEADING)
        end = sources[SPEC].find(SPEC_HEADING, start)
        for fence in re.findall(r"```ya?ml\n(.*?)```", sources[SPEC][start:end], re.DOTALL):
            if ACTOR_LABEL in fence and f"kube-agents/tier: {tier}" in fence:
                return re.search(r"^rules:\n(.*)\Z", fence, re.DOTALL | re.MULTILINE).group(1)
        raise AssertionError(f"06 §2.2 has no actor template for {tier!r} to build the control from")

    def grant_rules() -> str:
        idx = sources[SPEC].find(SPEC_HEADING)
        return re.search(r"```ya?ml\n(.*?)```", sources[SPEC][idx:], re.DOTALL).group(1)

    def rendered_actor(tier: str, *, extra: str = "", drop: str = "", stamp: str = "") -> str:
        """The per-tier actor object as 06 §2.2 ∪ §2.2.1 specifies it, optionally perturbed."""
        body = fence_rules(tier)
        if drop:
            assert drop in body, f"the drop anchor has moved: {drop!r}"
            body = body.replace(drop, "", 1)
        return (
            "\n---\n"
            "apiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: ClusterRole\n"
            "metadata:\n"
            f"  name: {tier}-rendered-actor\n"
            "  labels:\n"
            f"    kube-agents/tier: {stamp or tier}\n"
            "    kube-agents/role: actor\n"
            "rules:\n" + body.rstrip("\n") + "\n" + extra + grant_rules().rstrip("\n") + "\n"
        )

    def with_actor(s: dict[str, str], doc: str) -> dict[str, str]:
        return {**s, overlay: s[overlay] + doc}

    # 06 §2.2's platform block names apiextensions.k8s.io in its NOT GRANTED footer by name.
    OUTSIDE_TEMPLATE = (
        "  - apiGroups: [apiextensions.k8s.io]\n"
        "    resources: [customresourcedefinitions]\n"
        "    verbs: [create]\n"
    )
    # A platform-only rule, so dropping it is visible in `owed` -- unlike anything §2.2.1 re-grants.
    PLATFORM_ONLY = (
        "  - apiGroups: [constraints.gatekeeper.sh, templates.gatekeeper.sh, kyverno.io]\n"
        '    resources: ["*"]\n'
        "    verbs: [get, list, watch, create, update, patch, delete]\n"
    )

    # (label, mutate, signal). The signal is a substring that only the property this mutation is
    # about would produce, and the loop below asserts it appears. Six properties overlap in this
    # check -- widening, narrowing, the missing bound, the missing grant, the fixture escape -- so
    # "the check went red" is satisfied by whichever fires first and says nothing about the rest
    # ([[LSN-035]]).
    mutations = [
        (
            # The defect 06 §2.2.1 names first. `update` on actionrecords lets the broker rewrite the
            # record of what it did, and the diff is one word on a line that already says `create`.
            "an actor Role gains `update` on actionrecords",
            lambda s: edit(
                s,
                canon_rbac,
                'resources: ["actionrecords"]\n    verbs: ["get", "list", "watch", "create"]',
                'resources: ["actionrecords"]\n    verbs: ["get", "list", "watch", "create", "update"]',
            ),
            "grants 'kubeagents.x-k8s.io/actionrecords:update'",
        ),
        (
            # The tenant template landing early, by way of an RBAC file rather than the policy.
            "an actor Role gains a tenant resource",
            lambda s: edit(
                s,
                canon_rbac,
                'rules:\n  # step 1',
                'rules:\n  - apiGroups: ["apps"]\n    resources: ["deployments"]\n    verbs: ["create"]\n  # step 1',
            ),
            "grants 'apps/deployments:create'",
        ),
        (
            # 06 §2.2.1: a tier that cannot read the freeze object fails closed permanently. The
            # missing-grant direction, which a containment-only check would pass.
            "the fleetfreezes read is dropped from every actor object",
            lambda s: {
                rel: text.replace(
                    'resources: ["fleetfreezes"]\n    verbs: ["get", "list", "watch"]\n', ""
                )
                for rel, text in s.items()
            },
            "no tier-neutral actor RBAC object in the tree grants 'kubeagents.x-k8s.io/fleetfreezes:",
        ),
        (
            "a VAP copy's allow-list gains a triple the spec does not grant",
            lambda s: edit(
                s,
                boot_vap,
                "'kubeagents.x-k8s.io/actionrecords:create',",
                "'kubeagents.x-k8s.io/actionrecords:create',\n                  'kubeagents.x-k8s.io/actionrecords:update',",
            ),
            "allow-list admits 'kubeagents.x-k8s.io/actionrecords:update'",
        ),
        (
            # The copy that drifts is the one enforced in the cluster nobody is looking at, so the
            # mutation is applied to the bootstrap copy rather than the canonical one.
            "a VAP copy's allow-list loses a triple the spec grants",
            lambda s: edit(s, boot_vap, "'kubeagents.x-k8s.io/fleetfreezes:watch',", ""),
            "allow-list omits 'kubeagents.x-k8s.io/fleetfreezes:watch'",
        ),
        (
            # Not a narrowing and not a widening: the bound disappears. Every negative fixture aimed
            # at validation 3 would be admitted, and the suite would still be green without this.
            "a VAP copy loses its actor validation entirely",
            lambda s: edit(s, canon_vap, "(g + '/' + res + ':' + v)", "(g + '/' + res)"),
            "no actor allow-list found",
        ),
        (
            "the spec's own grant is widened",
            lambda s: edit(
                s,
                SPEC,
                "  resources: [actionrecords]\n  verbs: [get, list, watch, create]",
                "  resources: [actionrecords]\n  verbs: [get, list, watch, create, delete]",
            ),
            # Naming the triple, not just "omits". A check with 06 §2.2.1's grant hardcoded rather
            # than parsed would still say "omits" about something; only a check that actually read
            # `delete` out of the mutated spec can name `actionrecords:delete`.
            "allow-list omits 'kubeagents.x-k8s.io/actionrecords:delete'",
        ),
        (
            # The fixture escape, used from the other side: a real grant wearing a fixture's name.
            "a real actor object hides behind the fixture prefix",
            lambda s: edit(s, canon_rbac, "name: kubeagents-broker-operations", "name: vaptest-broker-operations"),
            "uses the 'vaptest-' fixture prefix outside",
        ),
        (
            # Property 6, and the reason P9-T9b-5b-0-ii is not a one-file change: the object is
            # exactly what 06 §2.2 ∪ §2.2.1 specifies -- properties 3 and 4 are clean on it -- and
            # the shipped admission policy still refuses it. `secrets:list` is named in the signal
            # because it is the specific authority the material-egress scan of 06 §4.2 fails closed
            # without, on a real cluster, today.
            "the conformant per-tier actor object is rendered while the VAP still bounds actors to §2.2.1",
            lambda s: with_actor(s, rendered_actor("platform")),
            "grants '/secrets:list', which no installed vap-agent-readonly admits",
        ),
        (
            # Property 3's tier arm. 06 §2.2's platform footer names apiextensions.k8s.io as NOT
            # GRANTED (it is the Agent CRD itself), so this is a rule a render could plausibly add
            # and no reader would question.
            "a tier-stamped actor object gains a rule its own template excludes",
            lambda s: with_actor(s, rendered_actor("platform", extra=OUTSIDE_TEMPLATE)),
            "grants 'apiextensions.k8s.io/customresourcedefinitions:create', which is in neither",
        ),
        (
            # Property 4's tier arm -- the direction a containment-only check passes, and the one
            # that silently under-provisions a tier instead of over-provisioning it.
            "a tier-stamped actor object silently drops a rule its template requires",
            lambda s: with_actor(s, rendered_actor("platform", drop=PLATFORM_ONLY)),
            "no tier-platform actor RBAC object in the tree grants 'constraints.gatekeeper.sh/*:",
        ),
        (
            # A typo'd tier is the quiet version of the same failure: the old check held every actor
            # object to §2.2.1 alone, so a mis-stamped object would be measured against the wrong
            # template and pass for it.
            "a tier-stamped actor object names a tier 06 §2.2 has no template for",
            lambda s: with_actor(s, rendered_actor("platform", stamp="platfrm")),
            "which 06 §2.2 has no actor template for",
        ),
        (
            # Property 7. Narrowing `isActor` is the most natural way to make property 6's finding go
            # away, it looks like a scoping fix, and it silently turns the reduction property 6 rests
            # on into a false statement.
            "isActor is narrowed so validation 3 stops governing tier-stamped actors",
            lambda s: edit(
                s,
                canon_vap,
                "object.metadata.labels['kube-agents/role'] == 'actor'\n  validations:",
                "object.metadata.labels['kube-agents/role'] == 'actor' &&\n"
                "        !('kube-agents/tier' in object.metadata.labels)\n  validations:",
            ),
            "`isActor` is no longer the bare actor-label test",
        ),
        (
            # The joiner's own risk. Reading 06 §2.2 at all required accepting flow sequences
            # reflowed across lines; the failure that buys is a parser that shrugs at a BLOCK
            # sequence and reports a smaller template, which passes property 3 for the wrong reason.
            "a spec template switches a flow sequence for a block sequence",
            lambda s: edit(
                s,
                SPEC,
                "    resources: [containerclusters, containernodepools]",
                "    resources:\n      - containerclusters\n      - containernodepools",
            ),
            "expected a flow sequence like [a, b]",
        ),
    ]

    clean = check(sources)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors: list[str] = []
    for label, mutate, signal in mutations:
        mutated = mutate(dict(sources))
        if mutated == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        found = check(mutated)
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:120]}...)"
            )

    if survivors:
        print("FAIL: the negative control found regressions this check does not detect:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(
        f"PASS: negative control -- all {len(mutations)} injected regressions were detected, each "
        f"by the property it targets"
    )
    return 0


def main() -> int:
    if "--negative-control" in sys.argv[1:]:
        return negative_control()

    sources = read_sources()
    failures = check(sources)
    if failures:
        print("FAIL: V-BRK-013 -- the broker-operations grant is restated somewhere that disagrees with 06 §2.2.1", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    grant, _ = spec_grant(sources[SPEC])
    templates, _ = spec_tier_templates(sources[SPEC])
    copies = sum(1 for rel in sources if _is_vap_copy(rel))
    tiers = ", ".join(f"{t}={len(templates[t])}" for t in sorted(templates))
    stamped = sorted(
        {
            tier
            for rel in sources
            if _is_manifest(rel) and ACTOR_LABEL in sources[rel]
            for _, name, tier, _ in rbac_documents(sources[rel])
            if tier and not name.startswith(FIXTURE_PREFIX)
        }
    )
    print(
        f"PASS: V-BRK-013 -- 06 §2.2.1 grants {len(grant)} (apiGroup, resource, verb) triples and "
        f"06 §2.2 adds per tier ({tiers}); {copies} VAP copies compile exactly the {len(grant)}, "
        f"every actor RBAC object grants only what its tier is owed and only what admission would "
        f"admit, and per tier the union is exactly that. Tier-stamped actor objects present: "
        f"{', '.join(stamped) if stamped else 'none (06 §2.2 has no renderer -- P9-T9b-5b-0-ii)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
