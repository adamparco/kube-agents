#!/usr/bin/env python3
"""V-CTR-003 and V-CMP-011: the `Agent` CRD schema grants nothing, and cannot be made to.

Two check IDs, one property, one file. 09 §6.5 states V-CTR-003 as "No authority fields in the CRD
schema" citing 06 §10; 09 §6.15 states V-CMP-011 as "The CRD schema holds none of the prohibited
authority field names and sets no `x-kubernetes-preserve-unknown-fields` on `spec`". Those are the
same sentence read from the contract side and from the completeness side, and 06 §10 writes it once:

    **No authority fields:** a CR carrying `spec.rbac`, `spec.rules`, `spec.riskClass`,
    `spec.scopeOverride`, `spec.brokerServiceAccountName`, or `spec.actorServiceAccountName` is
    pruned/rejected; a test greps the generated CRD schema to assert none of those property names
    exists and that `spec` sets no `x-kubernetes-preserve-unknown-fields`.

Two IDs asserted by one artifact is deliberate rather than sloppy. V-MET-013 forbids a check ID
having two definition sites in 09; it says nothing about two IDs sharing an implementation, and
splitting this into two files that read the same YAML would give the pair two chances to drift apart
while proving one thing.

WHY THE PROPERTY MATTERS. 03 §4.2 makes an agent's authority a function of its tier and scope, minted
by the platform and never by the CR. Every field above is a way for the CR to name its own authority
instead: `spec.rbac`/`spec.rules` grant it directly, `spec.riskClass` re-labels how dangerous its
actions are, `spec.scopeOverride` moves the boundary, and the two ServiceAccount names substitute a
credential the platform did not mint. None of them is an exotic mistake -- each is the obvious field
to add the first time someone needs an exception, and each reads in review as configuration.

WHY THE SCHEMA AND NOT THE VALIDATOR. A structural schema PRUNES an unknown field rather than
rejecting it (06 §1.2 names the V-9 outcome "field pruned", not `Invalid`), so a pruned `spec.rbac`
grants nothing even if it is applied. That is the actual guarantee, and it is a property of the
schema alone -- no webhook runs, no controller reads. It also means the whole guarantee rests on the
schema staying CLOSED: one `x-kubernetes-preserve-unknown-fields: true` anywhere under `spec` and the
pruning stops, at which point property 1 is asserting that a name is absent from a schema that no
longer decides what is present.

FIVE PROPERTIES:

  1. NO PROHIBITED PROPERTY NAME under the `Agent` CRD's `spec` schema, at any depth. Depth matters:
     `spec.rbac` is the obvious spelling and `spec.security.rbac` is the one that gets merged.
  2. NO `x-kubernetes-preserve-unknown-fields` under that subtree, at any depth -- not merely on the
     `spec` node. A preserved pocket three levels down is where a pruned field stops being pruned,
     and it is legal YAML that controller-gen emits on request (`actionrecords.yaml` carries two, for
     the opaque payload and patch, which is why this check is scoped to the Agent CRD and not to
     `config/crd/bases/*`).
  3. THE WALK IS NON-VACUOUS. The `spec` node exists, is `type: object`, and the scan found a
     plausible number of property names under it. A check that greps for six absent strings passes
     just as happily against a file it failed to parse, a path prefix that moved, or an empty
     directory (LSN-035).
  4. THE GO SOURCE DECLARES NO PROHIBITED JSON TAG either. The CRD in `config/crd/bases/` is
     GENERATED, and nothing in this repository's PR CI runs `make manifests` and diffs the result. So
     between adding `RBAC []rbacv1.PolicyRule \x60json:"rbac"\x60` to a type and remembering to
     regenerate, properties 1-3 read the old file and pass. This arm closes that window by asserting
     on the source of truth as well as its output.
  5. THE L2 BEHAVIOURAL ARM STILL EXISTS. This check asserts a shape and cannot observe pruning;
     `webhook-negatives-l2.sh`'s V-9 arm applies a CR carrying `spec.rbac` against a real API server
     and asserts it comes back without it. Deleting the half that proves the mechanism must not be
     silent, because what remains then looks exactly like full coverage.

WHY THE EXEMPTION IN PROPERTY 4 IS NAMED RATHER THAN SCOPED AWAY. `changepolicy_types.go` declares
`Rules []ChangeRule \x60json:"rules"\x60`, and it is correct: a ChangePolicy is a cluster-scoped
guardrail written by a human, not authority claimed by an agent. The tempting fix is to scan only
`agent_types.go`, which would also stop scanning any file a future AgentSpec embeds a type from. So
the scan is the whole package and the one legitimate tag is an entry with its reason -- and an entry
that stops matching is itself a finding, because a stale exemption widens what is allowed while
looking like it guards something.

Self-test (the `¬` of 09 §6): `--negative-control` applies each of six breakages to a copy of the
sources in memory and confirms this check reports it BY THE PROPERTY IT TARGETS, not merely that
something went red.

Run:  python3 dev/tests/crd-has-no-authority-fields.py
      python3 dev/tests/crd-has-no-authority-fields.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CRD = REPO / "k8s-operator" / "config" / "crd" / "bases" / "kubeagents.x-k8s.io_agents.yaml"
API = REPO / "k8s-operator" / "api" / "v1alpha1"
L2_ARM = REPO / "dev" / "verify" / "webhook-negatives-l2.sh"

# 06 §10, verbatim and in its order. The value is why the field is authority, so a failure says what
# was granted rather than only which string matched.
PROHIBITED = {
    "rbac": "grants RBAC from the CR -- 03 §4.2 mints authority from (tier, scope) and never from spec",
    "rules": "the same grant one word shorter; `rules` is what a PolicyRule slice is called upstream",
    "riskClass": "re-labels how dangerous an action is, which is the input the broker gates on (06 §3)",
    "scopeOverride": "moves the boundary the credential is minted against -- invariant 2 is absolute",
    "brokerServiceAccountName": "substitutes a write credential the platform did not mint (08 §2.4)",
    "actorServiceAccountName": "the same substitution for the actor half of the pair",
}

PRESERVE_UNKNOWN = "x-kubernetes-preserve-unknown-fields"

# The path, in the CRD document, of the schema for an Agent's `spec`. Written out rather than
# searched for so that a schema moving (a second version, a renamed subresource) is a finding rather
# than a silently narrower scan -- property 3 is what turns the miss into a failure.
SPEC_PATH = ("spec", "versions", "schema", "openAPIV3Schema", "properties", "spec")

# Property 3's floor. The Agent spec embeds enough upstream pod machinery that the real count is in
# the hundreds; this is set well below it, because the number the check defends against is a
# single-digit one produced by a parse that gave up early.
MIN_PROPERTIES = 50

# Property 4. Keyed by (file, tag) with the reason. See the module docstring.
EXEMPT_TAGS = {
    ("changepolicy_types.go", "rules"): "a ChangePolicy's rules are a cluster-scoped guardrail "
    "written by a human (06 §4.2). It is not an Agent field, it is not reachable from AgentSpec -- "
    "the Agent carries `changePolicyRefs`, a list of names -- and it grants nothing to anyone",
}
JSON_TAG = re.compile(r'json:"(?P<tag>[A-Za-z_][A-Za-z0-9_]*)')

# Property 5. Both halves of the V-9 arm: the strict-client rejection and the pruning assertion. Two
# needles rather than one because the arm's whole point is that the two outcomes differ.
L2_NEEDLES = (
    'reject "V-9 (strict: the default client path)" "spec.rbac"',
    "V-9 (pruning): spec.rbac SURVIVED into the returned object",
)

# --- the reader ---------------------------------------------------------------------------------
# A dependency-free structural walk, for the reason dev/tests/spec-ids.py and dev/tests/yamlsubset.py
# each have one: this runs in the L0 chain and L0 installs nothing, so a check that needs PyYAML is
# not an L0 check. `yamlsubset` itself cannot read a CRD -- controller-gen folds Go doc comments into
# multi-line plain scalars, which is outside its accepted subset -- and widening a parser two corpus
# lints depend on, in order to read a seventh file, is a change to their blast radius, not to this
# check's.

KEY_LINE = re.compile(r"^(?P<indent> *)(?P<dash>- )?(?P<key>[A-Za-z_][A-Za-z0-9_./-]*):(?:\s(?P<val>.*))?$")
DASH_SCALAR = re.compile(r"^ *- \S.*$")
# Keys whose value is free text. Everything indented under one of these is prose and is not scanned;
# every description in the file is a Go doc comment, and several of them necessarily contain the
# words this check forbids.
TEXT_KEYS = {"description", "example", "message", "messageExpression", "reason", "rule"}


class CRDSyntaxError(Exception):
    """A line the walk could not classify. Loud, on purpose -- see walk()."""


def walk(text: str) -> list[tuple[tuple[str, ...], str | None]]:
    """Return every mapping key in the document as (path, inline value).

    Sequence indices are elided: a key inside `- name: v1alpha1` is reported under `versions`, not
    under `versions[0]`. The Agent CRD ships exactly one version and the check asserts on names
    rather than on positions, so the index carries nothing.

    A line that is neither a key, a scalar sequence item, nor prose under a TEXT_KEY raises. The
    alternative -- skipping it -- is how a walk quietly stops covering a region while still
    returning a plausible-looking list of keys.
    """
    stack: list[tuple[int, str]] = []
    prose_indent: int | None = None
    out: list[tuple[tuple[str, ...], str | None]] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if prose_indent is not None:
            if indent > prose_indent:
                continue
            prose_indent = None

        m = KEY_LINE.match(line)
        if m:
            eff = len(m.group("indent")) + (2 if m.group("dash") else 0)
            while stack and stack[-1][0] >= eff:
                stack.pop()
            key = m.group("key")
            stack.append((eff, key))
            value = m.group("val")
            out.append((tuple(k for _, k in stack), value))
            head = (value or "").strip()[:1]
            if key in TEXT_KEYS or head in ("|", ">"):
                prose_indent = eff
            continue

        if DASH_SCALAR.match(line):
            continue

        raise CRDSyntaxError(
            f"{CRD.name}:{lineno}: this walk cannot classify {stripped[:60]!r}. It is not a mapping "
            "key, not a scalar sequence item, and not prose under a free-text key -- so the region "
            "it belongs to would be scanned wrongly or not at all"
        )

    return out


def go_sources() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(API.glob("*.go")) if not p.name.endswith("_test.go")}


def strip_go_comments(src: str) -> str:
    """Every one of these types documents its own fields, and V-CTR-003's own reason for existing is
    written out in at least one doc comment. Without this, the prose explaining the rule is what
    fails it -- and the fix a hurried person reaches for is deleting the prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", src)


# --- the properties -----------------------------------------------------------------------------
def check(crd_text: str, go: dict[str, str], l2_text: str) -> list[str]:
    """All five properties over already-read sources, so the negative control can mutate them."""
    failures: list[str] = []

    try:
        rows = walk(crd_text)
    except CRDSyntaxError as exc:
        return [str(exc)]

    n = len(SPEC_PATH)
    subtree = [(path, value) for path, value in rows if path[:n] == SPEC_PATH]
    names = {path[-1] for path, _ in subtree if len(path) > 1 and path[-2] == "properties"}

    # 3. The walk is non-vacuous. First, because the other two properties are absence claims.
    if not subtree:
        failures.append(
            f"{CRD.name}: nothing found under {'.'.join(SPEC_PATH)}. The Agent's spec schema is not "
            "where this check looks for it -- a renamed version, a second version, or a moved "
            "subresource -- and until it is repointed properties 1 and 2 are asserting that six "
            "names are absent from a subtree they never read (LSN-035)"
        )
    else:
        spec_type = next(
            (v for p, v in subtree if len(p) == n + 1 and p[-1] == "type"), None
        )
        if (spec_type or "").strip() != "object":
            failures.append(
                f"{CRD.name}: the `spec` schema declares `type: {spec_type}`, want `object`. Pruning "
                "is a property of a structural object schema; anything else and the closed-schema "
                "guarantee this check rests on does not apply (06 §1.2 V-9)"
            )
        if len(names) < MIN_PROPERTIES:
            failures.append(
                f"{CRD.name}: only {len(names)} property names found under `spec`, want at least "
                f"{MIN_PROPERTIES}. The scan reached the subtree but barely read it, which produces "
                "the same green as a schema that genuinely contains none of the prohibited names"
            )

    # 1. No prohibited property name, at any depth under `spec`.
    for path, _ in subtree:
        if len(path) > 1 and path[-2] == "properties" and path[-1] in PROHIBITED:
            failures.append(
                f"{CRD.name}: the `spec` schema declares the property `{path[-1]}` at "
                f"{'.'.join(path[n:])} -- {PROHIBITED[path[-1]]} (06 §10, V-CTR-003)"
            )

    # 2. No preserved pocket, at any depth under `spec`.
    for path, value in subtree:
        if path[-1] == PRESERVE_UNKNOWN:
            where = ".".join(path[n:-1]) or "spec"
            failures.append(
                f"{CRD.name}: `{PRESERVE_UNKNOWN}: {(value or '').strip()}` is set at {where}. An "
                "unpruned pocket under `spec` is where a field the schema does not declare survives "
                "into the stored object -- and property 1's absence claim stops meaning anything the "
                "moment one exists (06 §10, V-CMP-011)"
            )

    # 4. No prohibited JSON tag in the API package.
    for name in sorted(go):
        for tag in JSON_TAG.findall(strip_go_comments(go[name])):
            if tag not in PROHIBITED:
                continue
            if (name, tag) in EXEMPT_TAGS:
                continue
            failures.append(
                f"{name}: declares `json:\"{tag}\"` -- {PROHIBITED[tag]}. The CRD in "
                "config/crd/bases is generated and no PR check regenerates it, so the schema on "
                "disk will keep passing properties 1-3 until someone runs `make manifests` "
                "(06 §10, V-CTR-003)"
            )
    for (name, tag), why in sorted(EXEMPT_TAGS.items()):
        if name not in go:
            failures.append(
                f"{name}: is exempted for `json:\"{tag}\"` but no longer exists in "
                f"{API.relative_to(REPO)}. A stale exemption widens what property 4 allows while "
                "reading as something it guards"
            )
        elif f'json:"{tag}' not in strip_go_comments(go[name]):
            failures.append(
                f"{name}: is exempted for `json:\"{tag}\"` and no longer declares it ({why}). Remove "
                "the exemption -- while it stands, that file may reintroduce the tag silently"
            )

    # 5. The L2 behavioural arm still exists.
    for needle in L2_NEEDLES:
        if needle not in l2_text:
            failures.append(
                f"{L2_ARM.name}: the V-9 arm no longer contains {needle!r}. This check asserts the "
                "schema's SHAPE and cannot observe a field being pruned; that arm is the only place "
                "the mechanism is exercised against a real API server, and without it the pair reads "
                "as covered while nothing proves pruning happens (06 §1.2 V-9)"
            )

    return failures


def read_sources() -> tuple[str, dict[str, str], str]:
    return (
        CRD.read_text(),
        go_sources(),
        L2_ARM.read_text() if L2_ARM.exists() else "",
    )


# --- the negative control -----------------------------------------------------------------------
def negative_control() -> int:
    """Break each property in memory and confirm this check notices, and notices for the right
    reason. Six mutations; the two schema arms get two each, because a single 'did anything fail'
    assertion cannot tell a top-level hit from a nested one, and nested is the arm that would rot."""
    crd, go, l2 = read_sources()

    spec_head = "            spec:\n              description: spec defines the desired state of the Agent\n"
    spec_props = "              properties:\n                deployment:\n"
    security_props = "                  properties:\n                    serviceAccountAnnotations:\n"
    mutations = [
        (
            "an authority field is added at the top of `spec`",
            lambda c, g, s: (
                c.replace(
                    spec_props,
                    "              properties:\n                rbac:\n                  type: string\n"
                    "                deployment:\n",
                    1,
                ),
                g,
                s,
            ),
            "declares the property `rbac` at properties.rbac",
        ),
        (
            "an authority field is buried three levels down instead",
            lambda c, g, s: (
                c.replace(
                    security_props,
                    "                  properties:\n                    actorServiceAccountName:\n"
                    "                      type: string\n                    serviceAccountAnnotations:\n",
                    1,
                ),
                g,
                s,
            ),
            "declares the property `actorServiceAccountName` at "
            "properties.security.properties.actorServiceAccountName",
        ),
        (
            "the `spec` schema is opened with a preserve-unknown pocket",
            lambda c, g, s: (
                c.replace(spec_head, spec_head + f"              {PRESERVE_UNKNOWN}: true\n", 1),
                g,
                s,
            ),
            f"`{PRESERVE_UNKNOWN}: true` is set at spec",
        ),
        (
            "the spec schema moves and the walk reads nothing",
            lambda c, g, s: (c.replace("openAPIV3Schema:", "openAPIV4Schema:", 1), g, s),
            "The Agent's spec schema is not where this check looks for it",
        ),
        (
            "the Go type gains the field but the CRD is not regenerated",
            lambda c, g, s: (
                c,
                {
                    **g,
                    "common_types.go": g["common_types.go"].replace(
                        "type AgentSpec struct {",
                        'type AgentSpec struct {\n\tScopeOverride string `json:"scopeOverride,omitempty"`',
                        1,
                    ),
                },
                s,
            ),
            'common_types.go: declares `json:"scopeOverride"`',
        ),
        (
            "the L2 arm that proves pruning actually happens is deleted",
            lambda c, g, s: (c, g, s.replace(L2_NEEDLES[1], "V-9: ok", 1)),
            "the V-9 arm no longer contains",
        ),
    ]

    clean = check(crd, go, l2)
    if clean:
        print(
            "FAIL: the negative control cannot run -- the check is already failing on the real tree:",
            file=sys.stderr,
        )
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors = []
    for label, mutate, signal in mutations:
        mc, mg, ms = mutate(crd, dict(go), l2)
        if (mc, mg, ms) == (crd, go, l2):
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        found = check(mc, mg, ms)
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:120]}...)"
            )

    if survivors:
        print(
            "FAIL: V-CTR-003 / V-CMP-011 negative control -- these breakages were NOT caught:",
            file=sys.stderr,
        )
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(
        f"PASS: V-CTR-003 / V-CMP-011 negative control -- all {len(mutations)} breakages caught, "
        "each by the property it targets"
    )
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    for required in (CRD, L2_ARM):
        if not required.exists():
            print(
                f"FAIL: V-CTR-003 / V-CMP-011 -- {required.relative_to(REPO)} does not exist",
                file=sys.stderr,
            )
            return 1

    crd, go, l2 = read_sources()
    failures = check(crd, go, l2)

    if failures:
        print(
            "FAIL: V-CTR-003 / V-CMP-011 -- the Agent CRD schema can carry authority", file=sys.stderr
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    rows = walk(crd)
    n = len(SPEC_PATH)
    names = {
        path[-1]
        for path, _ in rows
        if path[:n] == SPEC_PATH and len(path) > 1 and path[-2] == "properties"
    }
    print(
        f"PASS: V-CTR-003, V-CMP-011 (L0) -- none of the {len(PROHIBITED)} prohibited authority "
        f"names appears among the {len(names)} property names under the Agent CRD's `spec`, no "
        f"`{PRESERVE_UNKNOWN}` is set anywhere in that subtree, the {len(go)} sources in "
        f"{API.relative_to(REPO)} declare no prohibited JSON tag outside {len(EXEMPT_TAGS)} named "
        f"exemption, and {L2_ARM.name}'s V-9 arm still proves the pruning"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
