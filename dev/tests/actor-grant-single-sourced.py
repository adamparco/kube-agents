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

None of the three survives being restated. The grant currently appears in SIX places -- the spec, the
canonical VAP and its two materialized copies, the tier overlays, the per-cluster applied instances,
and the cascade templates the platform and cluster-admin agents render for clusters and namespaces
that do not exist yet. Six copies of an allow-list is five chances for one of them to gain a verb,
and the one that gains it is the one enforced in the cluster nobody is looking at. So the copies are
not asked to agree with each other -- two copies agreeing is not the property that matters -- they
are each checked against the spec.

WHY THIS CANNOT BE LEFT TO REVIEW. A wrong triple in an RBAC file is not a compile error, is not a
runtime error, and is not visible from either side: the Role looks right, the VAP that is supposed to
bound it looks right, and if they drifted together they agree with each other. The API server never
complains -- RBAC is a union, so an extra rule silently grants, and an allow-list entry for a triple
nobody requests silently does nothing. The only artifact that disagrees is a design document, and
design documents do not run.

Five properties:

  1. THE SPEC PARSES, AND IS THE ONLY DEFINITION SITE. 06 §2.2.1's fenced YAML block expands to a set
     of (apiGroup, resource, verb) triples. Everything else is compared against it.
  2. EVERY VAP COPY'S ACTOR ALLOW-LIST EQUALS THE GRANT, IN BOTH DIRECTIONS. A missing triple is a
     policy that denies a legitimate actor identity; an extra one is a hole. Copies are DISCOVERED BY
     GLOB, not by a list of paths ([[LSN-036]], [[LSN-038]]) -- the cascade template renders a fourth
     copy into every cluster the fleet grows, and a hardcoded list would not know about it.
  3. NO ACTOR RBAC OBJECT GRANTS ANYTHING OUTSIDE THE GRANT. Every Role/ClusterRole labelled
     `kube-agents/role: actor` is checked rule by rule. This is the property that fails if someone
     lands 06 §2.2's tenant template early by editing an RBAC file instead of the policy.
  4. THE GRANT IS FULLY REALISED. The union of every actor object's rules must EQUAL the grant, not
     merely be contained in it. Containment alone is satisfied by an empty tree, and by the far more
     likely accident of a rule silently dropped in one of six copies -- 06 §2.2.1's point about
     `fleetfreezes` is that the missing-grant direction is the one that bricks a tier.
  5. NON-VACUITY. The spec block must parse to a non-trivial grant, and the globs must actually find
     VAP copies and actor objects. A check that found nothing to compare would print PASS forever
     after a refactor moved the tree it reads ([[LSN-035]]).

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
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

SPEC = "docs/design/06-api-and-data-contracts.md"
# The fence is located by the heading it follows, not by an index into the file's code blocks. A
# document gains and loses fences constantly; it gains a second `#### 2.2.1` never.
SPEC_HEADING = "#### 2.2.1 Broker operations grant"

# The label that makes a Role/ClusterRole an ACTOR identity. Same discriminator the admission policy
# uses (`variables.isActor`), so the check and the runtime enforcement agree by construction rather
# than by two independent guesses.
ACTOR_LABEL = "kube-agents/role: actor"

# Admission fixtures. See FIXTURES in the module docstring.
FIXTURE_PREFIX = "vaptest-"
FIXTURE_DIR = "examples/gitops-repo/policy/tests/"

# Where a copy of the policy can legitimately live. Globs, not paths.
VAP_GLOBS = ("**/vap-agent-readonly.yaml", "**/vap-agent-readonly.yaml.tmpl")

# Non-vacuity floors. Low enough not to be a maintenance burden, high enough that a glob which
# stopped matching cannot pass.
MIN_SPEC_TRIPLES = 10
MIN_VAP_COPIES = 3
MIN_ACTOR_OBJECTS = 4

# A flow sequence on one line: `[a, b]` or `["a", "b"]`. The only form either the spec block or the
# RBAC files use, and the only form accepted -- see parse_rules.
FLOW_SEQ = re.compile(r"^\[(.*)\]$")

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
            f"{where}: expected a one-line flow sequence like [a, b], got {value.strip()!r}. "
            f"Block sequences are not read by this check and would be silently dropped."
        )
    body = m.group(1).strip()
    if not body:
        return []
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


def parse_rules(block: str, where: str) -> list[dict[str, list[str]]]:
    """Read an RBAC `rules:` body into a list of {key: [values]}.

    A narrow reader rather than a YAML library because this runs in the L0 chain and L0 installs no
    dependencies -- the same call dev/tests/yamlsubset.py documents at length. The accepted shape is
    the one every rules block in this repo is written in:

        - apiGroups: ["a", "b"]     # or [a, b]
          resources: ["x"]
          verbs: ["get"]

    Anything else raises rather than parsing partially.
    """
    rules: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    for raw in block.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            current = {}
            rules.append(current)
            stripped = stripped[2:]
        elif current is None:
            raise GrantSyntaxError(f"{where}: content before the first `- ` rule item: {raw!r}")
        if ":" not in stripped:
            raise GrantSyntaxError(f"{where}: not a `key: value` line: {raw!r}")
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


def split_documents(text: str) -> list[str]:
    """YAML documents, split on a `---` at column zero. Sufficient for these files and no more."""
    return re.split(r"^---\s*$", text, flags=re.MULTILINE)


def rbac_documents(text: str) -> list[tuple[str, str, str]]:
    """(kind, name, rules-block) for each Role/ClusterRole document carrying the actor label."""
    out: list[tuple[str, str, str]] = []
    for doc in split_documents(text):
        m = re.search(r"^kind:\s*(Role|ClusterRole)\s*$", doc, re.MULTILINE)
        if not m:
            continue
        if ACTOR_LABEL not in doc:
            continue
        name_m = re.search(r"^  name:\s*(\S+)\s*$", doc, re.MULTILINE)
        rules_m = re.search(r"^rules:\n(.*)\Z", doc, re.DOTALL | re.MULTILINE)
        out.append((m.group(1), name_m.group(1) if name_m else "<unnamed>", rules_m.group(1) if rules_m else ""))
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
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    sources: dict[str, str] = {}
    for rel in listing.split("\0"):
        if not rel:
            continue
        if rel != SPEC and not (rel.endswith(".yaml") or rel.endswith(".yaml.tmpl")):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            sources[rel] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return sources


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

    # --- property 2: every VAP copy's allow-list equals the grant --------------------------------
    vap_copies = sorted(rel for rel in sources if _is_vap_copy(rel))
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

    # --- properties 3 and 4: actor RBAC objects --------------------------------------------------
    realised: set[str] = set()
    actor_objects = 0
    for rel in sorted(sources):
        if not (rel.endswith(".yaml") or rel.endswith(".yaml.tmpl")):
            continue
        if ACTOR_LABEL not in sources[rel]:
            continue
        for kind, name, block in rbac_documents(sources[rel]):
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
            realised |= got
            for extra in sorted(got - grant):
                failures.append(
                    f"{rel}: {kind}/{name} grants {extra!r}, which is outside 06 §2.2.1's "
                    f"broker-operations grant. The per-tier tenant template of 06 §2.2 is P10-T1; "
                    f"until it lands an actor identity may hold nothing else."
                )

    if actor_objects < MIN_ACTOR_OBJECTS:
        failures.append(
            f"VACUOUS: found {actor_objects} actor RBAC object(s), below the floor of "
            f"{MIN_ACTOR_OBJECTS}. Either the tree lost its `{ACTOR_LABEL}` labels or the document "
            f"splitter stopped seeing them; both make a PASS here meaningless."
        )
    elif not failures or realised:
        for missing in sorted(grant - realised):
            failures.append(
                f"no actor RBAC object in the tree grants {missing!r}, which 06 §2.2.1 requires. A "
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
        ),
        (
            "a VAP copy's allow-list gains a triple the spec does not grant",
            lambda s: edit(
                s,
                boot_vap,
                "'kubeagents.x-k8s.io/actionrecords:create',",
                "'kubeagents.x-k8s.io/actionrecords:create',\n                  'kubeagents.x-k8s.io/actionrecords:update',",
            ),
        ),
        (
            # The copy that drifts is the one enforced in the cluster nobody is looking at, so the
            # mutation is applied to the bootstrap copy rather than the canonical one.
            "a VAP copy's allow-list loses a triple the spec grants",
            lambda s: edit(s, boot_vap, "'kubeagents.x-k8s.io/fleetfreezes:watch',", ""),
        ),
        (
            # Not a narrowing and not a widening: the bound disappears. Every negative fixture aimed
            # at validation 3 would be admitted, and the suite would still be green without this.
            "a VAP copy loses its actor validation entirely",
            lambda s: edit(s, canon_vap, "(g + '/' + res + ':' + v)", "(g + '/' + res)"),
        ),
        (
            "the spec's own grant is widened",
            lambda s: edit(
                s,
                SPEC,
                "  resources: [actionrecords]\n  verbs: [get, list, watch, create]",
                "  resources: [actionrecords]\n  verbs: [get, list, watch, create, delete]",
            ),
        ),
        (
            # The fixture escape, used from the other side: a real grant wearing a fixture's name.
            "a real actor object hides behind the fixture prefix",
            lambda s: edit(s, canon_rbac, "name: kubeagents-broker-operations", "name: vaptest-broker-operations"),
        ),
    ]

    clean = check(sources)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors: list[str] = []
    for label, mutate in mutations:
        mutated = mutate(dict(sources))
        if mutated == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        if not check(mutated):
            survivors.append(label)

    if survivors:
        print("FAIL: the negative control found regressions this check does not detect:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(f"PASS: negative control -- all {len(mutations)} injected regressions were detected")
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
    copies = sum(1 for rel in sources if _is_vap_copy(rel))
    print(
        f"PASS: V-BRK-013 -- 06 §2.2.1 grants {len(grant)} (apiGroup, resource, verb) triples; "
        f"{copies} VAP copies compile exactly those, and every actor RBAC object in the tree grants "
        f"a subset whose union is exactly those"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
