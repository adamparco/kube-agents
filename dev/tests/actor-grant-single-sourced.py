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

  * A **tier-neutral** actor object (no `kube-agents/tier`) is the shared broker-operations
    ClusterRole, which exists because a namespace-scoped tier cannot own a cluster-scoped grant. It
    is owed §2.2.1 and nothing else.
  * A **tier-stamped** actor object is owed its tier's §2.2 template PLUS §2.2.1's grant. Since
    P9-T9b-5b-0-ii-b there are three, `k8s-operator/scripts/actor-grant-<tier>.yaml.template`, and
    the tier arm runs against the real tree rather than against a synthetic.

The tier arm was written one unit BEFORE that render existed, because a check may not be changed in
the same unit as the implementation whose failure motivated the change, and it was exercised in the
meantime by a negative-control row that synthesised the object the render would emit. That is the
two-trees discipline of [[LSN-053]], and this is what the far side of it looks like: the rows that
stood in for the future tree are now rows that perturb the real one.

A CEILING AND A PROFILE, WHICH ARE NOT THE SAME SET. 06 §2.2's templates carry
`create`/`update`/`patch`/`delete`; the platform one carries them on `secrets`, cluster-wide. Phase
9's acceptance (07 §2) is that the whole safety machinery runs end to end **with no write authority
anywhere**, so rendering a template WHOLE inside Phase 9 would hand every platform actor cluster-wide
`delete secrets` in the phase whose entire point is that nothing can write. The install path
therefore renders the READ half, and the write half arrives with P10-T1 -- which 03 §4.2 defines as
this very policy inverted: "the same policy object as the read-only generation's
`vap-agent-readonly`, **inverted**: reader SAs keep the read-verb allow-list; actor SAs get a
scope-and-template allow-list instead of a blanket write denial".

Only the completeness direction moves. Property 3's CEILING is the whole template -- an actor object
may never exceed 06 §2.2, dark or not -- while property 4's PROFILE is the read-verb half, which is
what the tree is required to have actually rendered. `DARK_PROFILE` below is the one place that
flips, and property 6 is what keeps the distinction from being decorative: under a read-widened
policy a write triple is admitted by neither the allow-list nor the read disjunct, so ADMISSION is
what holds Phase 9 dark, and this check asserts admission's shape rather than trusting it.

Seven properties:

  1. THE SPEC PARSES, AND IS THE ONLY DEFINITION SITE. 06 §2.2.1's fenced YAML block expands to a set
     of (apiGroup, resource, verb) triples, and each of 06 §2.2's three fenced templates expands to
     one more, keyed by the tier its own labels declare. Everything else is compared against those.
  2. EVERY VAP COPY'S ACTOR ALLOW-LIST EQUALS THE BROKER-OPERATIONS GRANT, IN BOTH DIRECTIONS, AND
     THE EXPRESSION AROUND IT HAS ONE OF EXACTLY TWO SHAPES. A missing triple is a policy that denies
     a legitimate actor identity; an extra one is a hole. Copies are DISCOVERED BY GLOB, not by a list
     of paths ([[LSN-036]], [[LSN-038]]) -- the cascade template renders a fourth copy into every
     cluster the fleet grows, and a hardcoded list would not know about it. The literal list is only
     half of what validation 3 says: the CEL around it either bounds an actor to the list ALONE
     (`bare`) or to the list plus any read verb (`read-widened`, the form that admits a rendered tier
     profile). Both are pinned exactly, and a third shape fails -- an allow-list is only a bound if
     the expression consuming it is one you have read.
  3. NO ACTOR RBAC OBJECT GRANTS ANYTHING OUTSIDE WHAT ITS TIER IS OWED. Every Role/ClusterRole
     labelled `kube-agents/role: actor` is checked rule by rule against the set above. This is the
     property that fails if someone lands a tenant grant by editing an RBAC file rather than by
     rendering the template 06 §2.2 actually specifies -- and, for a tier-stamped object, if the
     rules drift from that template by one resource.
  4. WHAT A TIER IS OWED **AT THIS PHASE** IS FULLY REALISED. Per tier, the UNION of that tier's
     actor objects must EQUAL that tier's profile -- §2.2.1's grant plus the READ verbs of its §2.2
     template while `DARK_PROFILE` holds -- not merely be contained in it. Note the asymmetry with
     property 3, which is deliberate: the ceiling is the whole template and does not move, so a write
     triple is never admitted by being unasserted here; it is caught by property 6 instead.
     Containment alone is satisfied by an empty tree, and by the far more likely accident of a rule
     silently dropped in one of six copies
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
     every actor object under `failurePolicy: Fail`, to the allow-list alone or to the allow-list plus
     any read verb, depending on which of property 2's two shapes every copy carries -- the bound is
     the INTERSECTION across copies, because a rule one cluster's policy admits and another's refuses
     is refused. All three copies are `read-widened` today, so the rendered read profile is admitted
     and the WRITE half is not -- which is the mechanism, not merely the intention, that keeps Phase
     9 dark. Revert any one of them to `bare` and the intersection is bare again: the three shipped
     templates become objects the spec requires and the installed policy refuses, which is how a
     render lands that no cluster will ever accept. 06 §2.2 names `vap-agent-scope` as the validator
     for the tier templates and it does not exist (P10-T1); until then this property is the standing
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

Self-test (the `¬` of 09 §6): `--negative-control` applies each of twenty plausible regressions to a
copy of the sources in memory and confirms this check reports every one -- each by the property it
targets, not merely by turning the suite red ([[LSN-035]]).

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

# The read verbs, in the one place both this check and `vap-agent-readonly` validation 1 spell them.
# Property 2's `read-widened` shape is BUILT from this tuple rather than compared to a literal, so
# the policy's disjunct and this check's profile cannot come to disagree about what "read" means.
READ_VERBS = ("get", "list", "watch")

# Phase 9 renders the READ half of each 06 §2.2 template and nothing else -- see the module
# docstring. Flip this at P10-T1, in the same unit that widens the allow-list from `read-widened` to
# the per-tier template allow-list 03 §4.2 requires; the two are one change wearing two hats, and
# flipping either alone turns this check red, which is the intended coupling.
DARK_PROFILE = True
# A tier's profile must be a STRICT subset of its ceiling and still substantial. Both directions
# matter: a profile equal to the ceiling means the read filter did nothing (Phase 9 is not dark), and
# an empty one means it ate everything (property 4 stops asserting anything).
MIN_PROFILE_TRIPLES = 10

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

# Everything CEL evaluates between `r.verbs.all(v,` and the allow-list membership test. Empty in the
# `bare` shape; exactly one read-verb disjunct in the `read-widened` one. Anything else and this
# check no longer knows what validation 3 permits, which is a failure and not a default.
#
# The head is TEMPERED against `r.verbs.all(` rather than being a plain `.*?`, because validation 1
# opens a `r.verbs.all(v, v in ['get','list','watch'])` of its own several hundred characters
# earlier and a lazy dot happily spans from there to validation 3's allow-list -- reporting the two
# validations and the comment between them as validation 3's verb test, which is a third shape, so
# the check fails loudly on the tree it is supposed to pass on.
VAP_VERB_HEAD = re.compile(
    r"r\.verbs\.all\(\s*v\s*,(?P<head>(?:(?!r\.verbs\.all\().)*?)\(g \+ '/' \+ res \+ ':' \+ v\)",
    re.DOTALL,
)
VAP_READ_DISJUNCT = "v in [" + ", ".join(f"'{v}'" for v in READ_VERBS) + "] ||"


# Validation 2, the wrong-scope rule: a namespace-scoped tier may not be granted a ClusterRole. The
# tier is parsed OUT of the policy rather than named here, for the same reason the allow-list is --
# "developer-team is namespace-scoped" already has definition sites in 03 §4 and in the policy, and
# a third one in a check that exists to forbid third copies would be its own joke.
VAP_WRONG_SCOPE = re.compile(
    r"!\(object\.kind == 'ClusterRole' &&\s*"
    r"'kube-agents/tier' in object\.metadata\.labels &&\s*"
    r"object\.metadata\.labels\['kube-agents/tier'\] == '(?P<tier>[^']+)'\)"
)


def vap_verb_shape(text: str) -> str | None:
    """Which of property 2's two shapes this copy's validation 3 has, or None for a third thing.

    `bare` bounds an actor to the allow-list alone; `read-widened` bounds it to the allow-list plus
    any read verb, which is what admits a rendered tier profile without admitting a tier's writes.
    None is not "unknown, assume the strict one" -- it is a failure, because the shapes this returns
    are the only two whose consequences property 6 knows how to compute.
    """
    m = VAP_VERB_HEAD.search(text)
    if not m:
        return None
    squashed = "".join(m.group("head").split())
    if squashed == "":
        return "bare"
    if squashed == "".join(VAP_READ_DISJUNCT.split()):
        return "read-widened"
    return None


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


# The install-path copy of the policy: the one a provisioning skill renders onto a new cluster,
# as opposed to the two exemplar copies under examples/gitops-repo. Located by PROPERTY rather
# than by literal path, because the literal is exactly what the P13-T5 persona rename broke --
# `propose-cluster-admin` became `provision-cluster-admin`, the anchor assertion in `narrow` raised
# KeyError, and `--negative-control` could not run at all. An unevaluable control is a failure, not
# a pass, so the locator is the check's own VAP glob narrowed to the templated copy under a skill's
# asset tree; a rename that keeps the file where it belongs no longer breaks it, and a rename that
# MOVES it out of the skill tree still fails loudly here.
SKILL_ASSET_TREE = re.compile(r"^agents/[^/]+/skills/[^/]+/assets/")


def _skill_vap_tmpl(sources: dict[str, str]) -> str:
    """The sole vap-agent-readonly template under a skill's assets, keyed as `sources` keys it."""
    found = sorted(
        rel
        for rel in sources
        if _is_vap_copy(rel) and rel.endswith(".tmpl") and SKILL_ASSET_TREE.match(rel)
    )
    assert len(found) == 1, (
        f"expected exactly one vap-agent-readonly template under a skill's assets, found "
        f"{found!r}. The mutations below narrow every installed copy of the policy at once; with "
        f"this one missing they would narrow only the exemplars, and property 6's bound -- the "
        f"intersection over copies -- would move for the wrong reason."
    )
    return found[0]


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

    def ceiling(tier: str) -> set[str]:
        """What an actor object stamped with `tier` may hold. 06 §2.2 ∪ §2.2.1. Property 3's bound.

        06 §2.2.1: the three templates "cover what an agent acts on. They do not cover what the
        broker needs to run its own pipeline"; every actor identity "additionally receives exactly
        this rule set". Additionally -- so the union, and for a tier-neutral object the grant alone.

        This does NOT move with the phase. Dark mode is a statement about what is rendered, never a
        licence to render something the spec does not describe.
        """
        return grant | templates.get(tier, set())

    def profile(tier: str) -> set[str]:
        """What a tier's objects must actually realise at this phase. Property 4's bound.

        The whole grant, plus the READ verbs of the tier template while Phase 9 is dark. A
        tier-neutral object has no template, so its profile is its ceiling and nothing about it
        changes at P10.
        """
        tmpl = templates.get(tier, set())
        if DARK_PROFILE:
            tmpl = {t for t in tmpl if t.rsplit(":", 1)[-1] in READ_VERBS}
        return grant | tmpl

    # Non-vacuity for the split itself. A profile that equals its ceiling means the read filter is a
    # no-op and property 4 is quietly demanding write authority in a phase that forbids it; an empty
    # one means the filter ate the template and property 4 is quietly demanding nothing.
    if DARK_PROFILE:
        for tier in sorted(templates):
            prof, ceil = profile(tier), ceiling(tier)
            if prof >= ceil:
                failures.append(
                    f"VACUOUS: the {tier} profile is not a strict subset of its ceiling, so the "
                    f"read-verb filter selected nothing away. Either 06 §2.2's {tier} template has "
                    f"stopped carrying write verbs -- in which case dark mode is over and "
                    f"DARK_PROFILE should say so -- or the filter is broken."
                )
            elif len(prof) < MIN_PROFILE_TRIPLES:
                failures.append(
                    f"VACUOUS: the {tier} profile is {len(prof)} triple(s), below the floor of "
                    f"{MIN_PROFILE_TRIPLES}. The read-verb filter ate the template, so property 4 "
                    f"would accept an actor object holding almost nothing."
                )

    # --- property 2: every VAP copy's allow-list equals the grant --------------------------------
    vap_copies = sorted(rel for rel in sources if _is_vap_copy(rel))
    admissible: set[str] | None = None  # intersection over copies; property 6's bound
    admits_reads: bool | None = None  # conjunction over copies, for the same reason
    # Denials, unlike admissions, UNION across copies: one copy refusing a shape is enough to refuse
    # it, which is the same conjunction seen from the other side.
    no_clusterrole: set[str] = set()
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

        # --- property 2, second half: the shape of the expression around the list -----------------
        # The list is only a bound in the company of the CEL that consumes it. Two shapes are
        # admissible and a third is a failure, because a check that reads the list and shrugs at the
        # expression would score a `v != 'delete' ||` prefix -- everything but one verb, admitted --
        # as an unchanged twenty-triple policy.
        shape = vap_verb_shape(sources[rel])
        if shape is None:
            head = VAP_VERB_HEAD.search(sources[rel])
            got = repr(head.group("head").strip()[:200]) if head else "no `r.verbs.all(v, ...)` at all"
            failures.append(
                f"{rel}: validation 3 bounds an actor by neither the allow-list alone nor the "
                f"allow-list plus {list(READ_VERBS)}, but by a third thing this check cannot score: "
                f"{got}. An allow-list is only a bound in the company of the expression that "
                f"consumes it, and a disjunct admitting one WRITE verb reads exactly like the read "
                f"one while leaving Phase 9 not dark. Teach this check the new shape in its own "
                f"unit -- Guardrail 9 -- or revert."
            )
        widened = shape == "read-widened"
        admits_reads = widened if admits_reads is None else (admits_reads and widened)

        # --- property 6's other half: validation 2, the wrong-scope rule --------------------------
        # A tier the policy declares namespace-scoped cannot be handed a ClusterRole, and 06 §2.2
        # gives that tier a template like any other -- so "render each tier's template" reads as a
        # ClusterRole three times and is refused once. The check has to know that before the render
        # does, or it certifies a tree the API server will not take.
        scope = VAP_WRONG_SCOPE.search(sources[rel])
        if not scope:
            failures.append(
                f"{rel}: no wrong-scope validation. A namespace-scoped tier could be granted a "
                f"cluster-scoped ClusterRole and this copy would admit it, and property 6 would go "
                f"on reporting that admission accepts the tree."
            )
        else:
            no_clusterrole.add(scope.group("tier"))

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

            # --- property 3: nothing outside the tier's CEILING ---------------------------------
            # The ceiling is the whole of 06 §2.2, write verbs and all, and does not move with the
            # phase: dark mode says what the tree renders, never what the spec permits.
            for extra in sorted(got - ceiling(tier)):
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
            # First the shape of the object, which validation 2 judges before validation 3 gets to
            # look at a single rule.
            if kind == "ClusterRole" and tier in no_clusterrole:
                failures.append(
                    f"{rel}: {kind}/{name} is a ClusterRole stamped `kube-agents/tier: {tier}`, "
                    f"which vap-agent-readonly's wrong-scope validation denies outright -- the tier "
                    f"is namespace-scoped and gets a Role in its own namespace. 06 §2.2 gives every "
                    f"tier a template and says nothing about the KIND that carries it, so rendering "
                    f"all three the same way is the natural mistake and this is where it stops."
                )
            # Then the rules, against whichever of property 2's two shapes every copy carries: the
            # allow-list alone, or the allow-list plus any read verb. Both halves conjoin across
            # copies -- a triple one cluster's policy admits and another's refuses is refused.
            if admissible is not None:
                for rejected in sorted(got - admissible):
                    if admits_reads and rejected.rsplit(":", 1)[-1] in READ_VERBS:
                        continue  # reached by the read disjunct, not by the allow-list
                    why = (
                        "it is outside the allow-list and is not a read verb, so neither disjunct "
                        "of validation 3 reaches it"
                        if admits_reads
                        else "it is outside the allow-list, which is validation 3's whole bound"
                    )
                    failures.append(
                        f"{rel}: {kind}/{name} grants {rejected!r}, which no installed "
                        f"vap-agent-readonly admits -- {why}. `isActor` selects on the actor label "
                        f"alone, so validation 3 governs this object under failurePolicy: Fail and "
                        f"the API server will REFUSE the apply. If this is a WRITE triple from 06 "
                        f"§2.2, that refusal is the mechanism holding Phase 9 dark (07 §2: the "
                        f"machinery runs end to end with no write authority anywhere) and the fix "
                        f"is to stop rendering it, never to widen the policy -- `vap-agent-scope` "
                        f"is where a tier template becomes admissible, and it arrives at P10-T1."
                    )

    if actor_objects < MIN_ACTOR_OBJECTS:
        failures.append(
            f"VACUOUS: found {actor_objects} actor RBAC object(s), below the floor of "
            f"{MIN_ACTOR_OBJECTS}. Either the tree lost its `{ACTOR_LABEL}` labels or the document "
            f"splitter stopped seeing them; both make a PASS here meaningless."
        )
    elif not failures or realised:
        # --- property 4: per tier, the union EQUALS that tier's PROFILE ---------------------------
        # Only for a tier that has objects. A tier with none has not been rendered yet, and demanding
        # it here would make V-BRK-013 fail until an unrelated unit lands -- which for a
        # BLOCKING-ALWAYS check is not a deferral that 09 §9.6 permits, it is a red gate.
        #
        # The bound is the PROFILE, not property 3's ceiling. The asymmetry is deliberate and is not
        # a hole: a write triple left unasserted here is still refused by property 3's ceiling if it
        # is outside the template, and by property 6 if it is inside one, since a rendered write
        # triple is admitted by neither disjunct of a read-widened policy.
        for tier in sorted(realised):
            for missing in sorted(profile(tier) - realised[tier]):
                where = (
                    f"the READ half of 06 §2.2's {tier} actor template, or §2.2.1"
                    if tier and DARK_PROFILE
                    else f"06 §2.2's {tier} actor template or §2.2.1" if tier else "06 §2.2.1"
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
    tmpl_vap = _skill_vap_tmpl(sources)
    overlay = "examples/gitops-repo/policy/rbac-overlay/platform.yaml"
    # The install-path render of 06 §2.2's platform template, landed by P9-T9b-5b-0-ii-b. Property
    # 4 measures the UNION of a tier's objects, so the two missing-rule rows below have to perturb
    # THIS file rather than inject a lamed synthetic beside it: a synthetic missing a rule the
    # shipped template still grants is a union that is still complete, and the row scores nothing.
    shipped_platform = "k8s-operator/scripts/actor-grant-platform.yaml.template"

    # --- the tier arm's subject: an object the real tree deliberately does not contain ------------
    # The three shipped templates render the READ half of 06 §2.2 and no more, so the rows that need
    # a WRONGLY rendered object -- the whole template with its write verbs, a rule outside the
    # template, a mis-stamped tier, a namespace-scoped tier carried by a ClusterRole -- have to
    # synthesise one. It is built FROM the spec rather than from a literal copied out of it: a
    # literal would be a fourth definition site, and the one thing this check exists to forbid.
    # Rows about a MISSING rule perturb the shipped template instead, for the union reason above.

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

    def rules_from_triples(triples: set[str]) -> str:
        """Re-emit a set of triples as a `rules:` block, one rule each.

        Used for the dark-mode object, whose rules are a SUBSET of a §2.2 template's and so cannot
        be lifted as fence text. Still built from the spec -- parsed out of it and re-emitted, never
        transcribed -- so the tier profile this control renders moves when 06 §2.2 moves.
        """
        return "".join(
            f'  - apiGroups: ["{t.rsplit(":", 1)[0].split("/", 1)[0]}"]\n'
            f'    resources: ["{t.rsplit(":", 1)[0].split("/", 1)[1]}"]\n'
            f'    verbs: ["{t.rsplit(":", 1)[1]}"]\n'
            for t in sorted(triples)
        )

    def read_half(tier: str) -> set[str]:
        """The READ verbs of 06 §2.2's template for `tier` -- what 5b-0-ii-b renders under dark mode."""
        tmpl, _ = spec_tier_templates(sources[SPEC])
        assert tier in tmpl, f"06 §2.2 has no actor template for {tier!r} to build the control from"
        got = {t for t in tmpl[tier] if t.rsplit(":", 1)[-1] in READ_VERBS}
        assert got, f"the {tier} template has no read verbs left, so the dark-mode control is empty"
        return got

    def rendered_actor(
        tier: str,
        *,
        extra: str = "",
        stamp: str = "",
        dark: bool = False,
    ) -> str:
        """A per-tier actor object the render could plausibly produce and must not.

        `dark=False` renders 06 §2.2 ∪ §2.2.1 whole -- the object the spec describes and Phase 9
        may not have. `dark=True` renders the read half plus §2.2.1, which is the phase profile
        the shipped templates already carry, and is used for the rows about the wrong KIND or the
        wrong tier stamp rather than the wrong triples.
        """
        body = rules_from_triples(read_half(tier)) if dark else fence_rules(tier)
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

    # --- the other half of 5b-0-ii-b, now landed: perturbing the allow-list that admits it --------
    # Until P9-T9b-5b-0-ii-b these helpers SYNTHESISED the widened policy, because the tree was bare
    # and the rows below were this check's committed record of the tree the next unit would build --
    # the two-trees discipline of [[LSN-053]]. That unit landed, so the direction inverts: the
    # widened form is what the three copies now hold, and the mutations perturb it. `narrow` puts a
    # copy back to the bare shape; `rewiden` swaps the disjunct for a different one. The two rows
    # that existed only to describe the future tree -- the read half and the whole template rendered
    # against a bare policy -- become one reachable regression each: the disjunct reverted, and the
    # disjunct reverted in ONE copy.
    #
    # `narrow` defaults to every copy for the reason `widen` did: property 2's shape test and
    # property 6's bound are both conjunctions over the three, so touching one and forgetting the
    # others leaves the bound where it was, which is the correct answer and not the one the mutation
    # is trying to demonstrate. The one row that passes `only=` is asserting exactly that.
    BARE_ANCHOR = "r.verbs.all(v,\n                (g + '/' + res + ':' + v)"
    WIDE_ANCHOR = (
        f"r.verbs.all(v,\n                {VAP_READ_DISJUNCT}\n                "
        "(g + '/' + res + ':' + v)"
    )

    def narrow(s: dict[str, str], only: list[str] | None = None) -> dict[str, str]:
        out = dict(s)
        for rel in only or [canon_vap, boot_vap, tmpl_vap]:
            assert WIDE_ANCHOR in out[rel], f"the verb-test anchor has moved in {rel}"
            out[rel] = out[rel].replace(WIDE_ANCHOR, BARE_ANCHOR, 1)
        return out

    def rewiden(s: dict[str, str], disjunct: str) -> dict[str, str]:
        out = dict(s)
        for rel in [canon_vap, boot_vap, tmpl_vap]:
            assert WIDE_ANCHOR in out[rel], f"the verb-test anchor has moved in {rel}"
            out[rel] = out[rel].replace(
                WIDE_ANCHOR,
                f"r.verbs.all(v,\n                {disjunct}\n                "
                "(g + '/' + res + ':' + v)",
                1,
            )
        return out

    # 06 §2.2's platform block names apiextensions.k8s.io in its NOT GRANTED footer by name.
    OUTSIDE_TEMPLATE = (
        "  - apiGroups: [apiextensions.k8s.io]\n"
        "    resources: [customresourcedefinitions]\n"
        "    verbs: [create]\n"
    )
    # A platform-only rule as the SHIPPED template spells it, so dropping it is visible in `owed`
    # -- unlike anything §2.2.1 re-grants to every tier, and unlike anything a sibling tier's
    # template also carries.
    SHIPPED_PLATFORM_ONLY = (
        '  - apiGroups: ["constraints.gatekeeper.sh", "templates.gatekeeper.sh", "kyverno.io"]\n'
        '    resources: ["*"]\n'
        '    verbs: ["get", "list", "watch"]\n'
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
            # Property 6 against the REAL tree, and the coupling 5b-0-ii-b created. Three templates
            # now render a read profile that only a read-widened policy admits, so reverting the
            # disjunct strands them; no synthetic object is injected here, because the shipped
            # templates are the subject. This is the row that fails if a later unit reads the
            # disjunct as a relaxation and "simplifies" validation 3 back to the allow-list alone.
            # `secrets:list` is named in the signal because it is the specific authority the
            # material-egress scan of 06 §4.2 fails closed without, on a real cluster, today.
            "the read disjunct is reverted in every copy, stranding the rendered read profiles",
            lambda s: narrow(s),
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
            # that silently under-provisions a tier instead of over-provisioning it. Applied to the
            # shipped template, because that is now where the authority comes from: a platform actor
            # that cannot read Gatekeeper constraints reports a clean fleet policy from a 403.
            "the shipped platform template silently drops a rule 06 §2.2 requires",
            lambda s: edit(s, shipped_platform, SHIPPED_PLATFORM_ONLY, ""),
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
        (
            # The same revert, in ONE copy, which is the shape drift actually takes. The copy that
            # goes stale is the one enforced in the cluster nobody is reading, and a reviewer
            # comparing it against the canonical file would see a policy that is stricter -- the
            # safe-looking direction. It strands the same three profiles, because the bound this
            # check computes is the conjunction over all three copies and not the best of them.
            "one VAP copy reverts to the bare disjunct while the other two stay widened",
            lambda s: narrow(s, only=[boot_vap]),
            "grants '/secrets:list', which no installed vap-agent-readonly admits",
        ),
        (
            # The other half alone, and the one that would go unnoticed: widening a policy nothing
            # yet exercises. Under `bare` this is caught by property 6 the moment a tier object
            # lands; under a WRITE-widened policy it would never be caught at all, because every
            # rendered write triple would be admissible. So property 2 pins the shape itself.
            "the read disjunct is widened by one write verb",
            lambda s: rewiden(s, "v in ['get', 'list', 'watch', 'patch'] ||"),
            "bounds an actor by neither the allow-list alone nor the allow-list plus",
        ),
        (
            # Property 4 against the profile rather than the ceiling, and against one RESOURCE
            # rather than a whole rule -- the diff is a single word inside a flow sequence, which is
            # what a hand-listed read profile actually loses. At L2 it surfaces as a refusal the
            # operator reads as policy working, which is why the profile is checked and not trusted.
            "the shipped platform template drops one resource from a read rule",
            lambda s: edit(
                s,
                shipped_platform,
                '    resources: ["namespaces", "serviceaccounts", "configmaps", "secrets"]',
                '    resources: ["namespaces", "serviceaccounts", "configmaps"]',
            ),
            "no tier-platform actor RBAC object in the tree grants '/secrets:list'",
        ),
        (
            # THE MECHANISM THAT HOLDS PHASE 9 DARK. The policy is the one 5b-0-ii-b landed, and
            # the render reaches for the whole template instead of its read half. Everything the
            # spec describes, admitted by nothing: the write triples fall outside the allow-list and
            # are not read verbs, so neither disjunct reaches them. Without this row, `DARK_PROFILE`
            # would be a comment -- property 4 alone permits a superset.
            "the whole template is rendered under the shipped policy, write verbs and all",
            lambda s: with_actor(s, rendered_actor("platform")),
            "is not a read verb, so neither disjunct of validation 3 reaches it",
        ),
        (
            # Validation 2. 06 §2.2 gives all three tiers a template and is silent on the KIND that
            # carries it, so "render each tier's template" reads as a ClusterRole three times --
            # perfectly conformant to §2.2, admissible under validation 3, and refused by the
            # policy anyway. Found by probing this check against the tree 5b-0-ii-b then built --
            # and it is why that tier's shipped template is a Role.
            "the namespace-scoped tier's profile is rendered as a ClusterRole",
            lambda s: with_actor(s, rendered_actor("developer-team", dark=True)),
            "which vap-agent-readonly's wrong-scope validation denies outright",
        ),
        (
            "a VAP copy loses its wrong-scope validation",
            lambda s: edit(s, boot_vap, "object.kind == 'ClusterRole' &&", "object.kind == '' &&"),
            "no wrong-scope validation",
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
    copies = sorted(rel for rel in sources if _is_vap_copy(rel))
    shapes = sorted({vap_verb_shape(sources[rel]) or "unreadable" for rel in copies})
    tiers = ", ".join(
        f"{t}: ceiling {len(grant | templates[t])}, profile "
        f"{len(grant | {x for x in templates[t] if x.rsplit(':', 1)[-1] in READ_VERBS})}"
        if DARK_PROFILE
        else f"{t}: ceiling {len(grant | templates[t])} (= profile)"
        for t in sorted(templates)
    )
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
        f"PASS: V-BRK-013 -- 06 §2.2.1 grants {len(grant)} (apiGroup, resource, verb) triples, and "
        f"joined with 06 §2.2 per tier ({tiers}); {len(copies)} VAP copies compile exactly the "
        f"{len(grant)} and bound actors {'/'.join(shapes)}; every actor RBAC object stays under its "
        f"tier's ceiling and inside what admission admits, and per tier the union is exactly the "
        f"profile. Tier-stamped actor objects present: "
        f"{', '.join(stamped) if stamped else 'none (06 §2.2 has no renderer -- P9-T9b-5b-0-ii-b)'}"
        + (
            f" . DARK_PROFILE is on: the write half of 06 §2.2 is specified, unrendered, and "
            f"inadmissible, which is how 07 §2's 'no write authority anywhere' is enforced rather "
            f"than intended."
            if DARK_PROFILE
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
