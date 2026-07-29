#!/usr/bin/env python3
"""L0: every field of `ActionRecordStatus` is accounted for in vap-agent-scope-journal.

WHY THIS EXISTS. `vap-agent-scope-journal.yaml` is 06 §4.3's (principal x field) table as CEL, and it
enumerates the status fields one at a time -- `phaseChanged`, `appliedChanged`, and so on -- because
Kubernetes' CEL types an OpenAPI object as a struct and a struct cannot be indexed by a runtime
string. That enumeration FAILS OPEN, and the failure is quiet:

    validation 1 admits a write when `variables.nothingChanged`.
    `nothingChanged` is the conjunction of `!<field>Changed` over the ENUMERATED fields.
    A field nobody enumerated is therefore invisible to it.

So an UPDATE that changes only an unenumerated status field satisfies `nothingChanged`, satisfies
validation 1, and is admitted -- from any principal, including the human cluster-admin the policy
exists to stop. The other four validations are all guarded by `!variables.is<Principal>`, so they do
not save it either. Nothing in the cluster complains: the policy loads, enforces every row it knows
about, and its own tests keep passing, because they only exercise fields that are in the table.

This is not hypothetical. The file used to claim, in a comment, that a catch-all named
`unknownStatusFieldChanged` denied exactly this. No such variable ever existed. The comment was
written by somebody who correctly identified the hazard and then described the fix instead of
building it, and it survived several reviews looking like protection.

WHY IT CANNOT BE FIXED INSIDE THE POLICY. The obvious catch-all, `object.status != oldObject.status`
crossed with the enumerated set, is not expressible: CEL cannot list a struct's keys, and the
nearest thing that is expressible -- denying whenever the status objects differ at all -- would deny
the no-op re-Update that controller-runtime performs on every conflict retry. That is the case the
`nothingChanged` escape hatch was added for in the first place.

So the invariant is enforced here, before the policy ever loads:

  1. PARITY. Every json field of `ActionRecordStatus` has a `<field>Changed` variable in the policy.
  2. THE VARIABLE IS WIRED IN. Every such variable appears in the `nothingChanged` conjunction. A
     variable that exists but is not consumed there is the same hole with an alibi.
  3. THE VARIABLE READS ITS OWN FIELD. `fooChanged` must mention `status.foo`. Guards against the
     copy-paste that duplicates a neighbouring row and leaves one field unwatched while both the
     name and the wiring look right.
  4. NO PHANTOMS. A `<field>Changed` variable whose field is not in the struct means the struct was
     renamed and the policy now watches nothing under the old name.
  5. NON-VACUITY. The struct must parse to a plausible number of fields and the policy must parse to
     a plausible number of variables ([[LSN-035]]): a check that read neither file would print PASS
     forever.

Deliberately reads the GO TYPE, not the generated CRD yaml. The Go struct is the definition site --
`make manifests` renders the CRD from it -- and reading the generated artifact would let a check pass
on a tree whose generated output is stale, which is the state a working tree is in for the whole of
the edit that adds a field.

Self-test (the `¬` of 09 §6): `--negative-control` injects each way the parity can break and
confirms this check reports every one.

Run:  python3 dev/tests/journal-status-vap-parity.py
      python3 dev/tests/journal-status-vap-parity.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

TYPES = "k8s-operator/api/v1alpha1/actionrecord_types.go"
POLICY = "k8s-operator/config/policy/vap-agent-scope-journal.yaml"

STRUCT = "ActionRecordStatus"

# Non-vacuity floors. Well below the current counts, high enough that a file which stopped parsing
# cannot slip through as "zero fields, zero variables, they match".
MIN_FIELDS = 8
MIN_VARIABLES = 8

# `Foo Bar `json:"foo,omitempty"`` -- the json name is all this check needs, and the tag is the only
# place it is authoritative (the Go field name is capitalized and the CRD is generated from the tag).
JSON_TAG = re.compile(r'json:"(?P<name>[A-Za-z0-9_]+)[,"]')

# A `- name: x` entry in the policy's `variables:` list, and the expression that follows it.
VAR_ENTRY = re.compile(
    r"^    - name:\s*(?P<name>\S+)\s*$\n(?P<body>(?:^ {6}.*\n|^\s*\n)*)",
    re.MULTILINE,
)


class ParseError(Exception):
    """A source is not in the shape this check reads. Loud, on purpose.

    Silence is the failure that matters here: a parser that shrugs reports fewer fields than the
    struct has, and fewer fields is exactly the state this check exists to detect.
    """


def status_fields(go_src: str) -> list[str]:
    """The json names of `ActionRecordStatus`'s top-level fields, in declaration order.

    Brace-counted rather than regex-terminated because the struct contains nested literals in its
    doc comments and a `}` at column zero is not a reliable terminator for a type this heavily
    annotated. Only depth-1 tags are collected: an inlined sub-struct would be a different shape and
    is rejected rather than half-read.
    """
    start = go_src.find(f"type {STRUCT} struct {{")
    if start < 0:
        raise ParseError(
            f"{TYPES}: `type {STRUCT} struct` not found. Either the type was renamed or this check "
            f"is reading the wrong file; both make every comparison below vacuous."
        )
    depth = 0
    fields: list[str] = []
    for raw in go_src[start:].splitlines():
        line = raw.split("//", 1)[0]
        opens, closes = line.count("{"), line.count("}")
        if depth == 1:
            m = JSON_TAG.search(raw)
            if m and "`json:" in raw:
                fields.append(m.group("name"))
        depth += opens - closes
        if depth <= 0 and (opens or closes):
            break
    if not fields:
        raise ParseError(f"{TYPES}: {STRUCT} parsed to zero json fields.")
    return fields


def policy_variables(yaml_src: str) -> dict[str, str]:
    """`{variable name: its expression text}` for the policy's `variables:` block.

    A regex over the text rather than a YAML parse: this runs in the L0 chain, which installs no
    dependencies, and the block is machine-formatted by prettier so its indentation is stable. The
    same call dev/tests/yamlsubset.py documents at length.
    """
    out: dict[str, str] = {}
    for m in VAR_ENTRY.finditer(yaml_src):
        out[m.group("name")] = m.group("body")
    if not out:
        raise ParseError(
            f"{POLICY}: no `- name:` variables parsed. The block moved or was reindented, and a "
            f"check that reads no variables reports parity with the empty set."
        )
    return out


def nothing_changed_body(yaml_src: str) -> str:
    """The text of the `nothingChanged` expression, which is what validation 1 actually consumes."""
    m = re.search(
        r"^    - name:\s*nothingChanged\s*$\n(?P<body>(?:^ {6}.*\n)*)",
        yaml_src,
        re.MULTILINE,
    )
    if not m:
        raise ParseError(
            f"{POLICY}: the `nothingChanged` variable is gone. It is the escape hatch validation 1 "
            f"consumes; without it this check has nothing to verify wiring against, and the policy "
            f"itself would deny every no-op conflict retry."
        )
    return m.group("body")


def check(sources: dict[str, str]) -> list[str]:
    failures: list[str] = []

    try:
        fields = status_fields(sources[TYPES])
        variables = policy_variables(sources[POLICY])
        wiring = nothing_changed_body(sources[POLICY])
    except ParseError as e:
        return [str(e)]

    if len(fields) < MIN_FIELDS:
        failures.append(
            f"VACUOUS: {STRUCT} parsed to {len(fields)} field(s), below the floor of {MIN_FIELDS}. "
            f"The struct shrank or the parser stopped reading it."
        )
    if len(variables) < MIN_VARIABLES:
        failures.append(
            f"VACUOUS: {POLICY} parsed to {len(variables)} variable(s), below the floor of "
            f"{MIN_VARIABLES}."
        )

    changed_vars = {n for n in variables if n.endswith("Changed") and n != "nothingChanged"}

    # --- 1 and 2: parity, and the variable is actually consumed ---------------------------------
    for f in fields:
        var = f"{f}Changed"
        if var not in variables:
            failures.append(
                f"status.{f} has no `{var}` variable in {POLICY}. An UPDATE changing only this field "
                f"satisfies `nothingChanged`, so validation 1 admits it FROM ANY PRINCIPAL — "
                f"including the human cluster-admin the policy exists to stop. Add the variable, add "
                f"it to `nothingChanged`, and decide which principals may write it."
            )
            continue
        if f"!variables.{var}" not in re.sub(r"\s+", " ", wiring).replace("! variables.", "!variables."):
            failures.append(
                f"`{var}` exists but is not part of the `nothingChanged` conjunction. The variable "
                f"is computed and then ignored, which leaves status.{f} writable by any principal "
                f"through the validation-1 escape hatch."
            )
        # --- 3: the variable reads its own field, and ONLY its own field ------------------------
        #
        # Equality rather than membership. These expressions have four branches (`has(old)` vs
        # `has(new)` crossed with the value comparison), and a copy-paste that fixes up three of
        # them and misses the fourth passes a "mentions the right field" test while watching the
        # wrong one on the branch that matters. Restricted to KNOWN field names so a reference to
        # something outside the struct is property 4's business, not a false positive here.
        read = {x for x in re.findall(r"status\.([A-Za-z0-9_]+)", variables[var])} & set(fields)
        if read != {f}:
            failures.append(
                f"`{var}` reads status.{sorted(read) or ['nothing']}, not exactly status.{f}. It was "
                f"almost certainly copied from a neighbouring row: the name and the wiring look "
                f"right and the field is unwatched on at least one branch."
            )

    # --- 4: no phantoms --------------------------------------------------------------------------
    for var in sorted(changed_vars):
        f = var[: -len("Changed")]
        if f not in fields:
            failures.append(
                f"`{var}` watches `status.{f}`, which is not a field of {STRUCT}. The field was "
                f"renamed or removed and the policy is now guarding a name nothing writes."
            )

    return failures


def read_sources() -> dict[str, str]:
    return {rel: (REPO / rel).read_text() for rel in (TYPES, POLICY)}


def negative_control() -> int:
    sources = read_sources()

    def edit(s: dict[str, str], rel: str, old: str, new: str) -> dict[str, str]:
        return {**s, rel: s[rel].replace(old, new, 1)}

    # (label, mutate, signal). The signal names the property, not merely the fact of a failure.
    # Parity, wiring and expression-identity all read the same two files and all report on
    # `<field>Changed`, so "the check went red" is satisfied by whichever notices first and says
    # nothing about the other two ([[LSN-035]]).
    mutations = [
        (
            # The defect this check was written for: a status field lands, and nobody teaches the
            # policy about it. This is exactly what `escalation` did before the check existed.
            "a new status field is added to the Go type and not to the policy",
            lambda s: edit(
                s,
                TYPES,
                "\tEscalation *ActionEscalation `json:\"escalation,omitempty\"`",
                "\tEscalation *ActionEscalation `json:\"escalation,omitempty\"`\n"
                "\t// +optional\n\tQuarantined bool `json:\"quarantined,omitempty\"`",
            ),
            "status.quarantined has no `quarantinedChanged` variable",
        ),
        (
            "a `<field>Changed` variable is deleted from the policy",
            lambda s: edit(
                s,
                POLICY,
                "    - name: messageChanged\n      expression: >-\n",
                "    - name: unusedChanged\n      expression: >-\n",
            ),
            "status.message has no `messageChanged` variable",
        ),
        (
            # The subtlest one: the variable is still computed, so a reader scanning the variables
            # block sees full coverage. Only the conjunction knows.
            "a variable is computed but dropped from the `nothingChanged` conjunction",
            lambda s: edit(s, POLICY, "!variables.contestedChanged && ", ""),
            "`contestedChanged` exists but is not part of the `nothingChanged` conjunction",
        ),
        (
            # Copy-paste: right name, wrong field. Both the parity and the wiring checks pass.
            "a variable is renamed onto another field's expression",
            lambda s: edit(
                s,
                POLICY,
                "    - name: reportChanged\n      expression: >-\n"
                "        (variables.hasOld && has(oldObject.status.report))",
                "    - name: reportChanged\n      expression: >-\n"
                "        (variables.hasOld && has(oldObject.status.recovery))",
            ),
            # Parity and wiring both pass on this mutation by construction, so this signal is the
            # only evidence the third property -- expression identity -- runs at all.
            "`reportChanged` reads status.['recovery', 'report'], not exactly status.report",
        ),
        (
            "a status field is renamed, orphaning its variable",
            lambda s: edit(s, TYPES, 'json:"undoneBy,omitempty"', 'json:"revertedBy,omitempty"'),
            "status.revertedBy has no `revertedByChanged` variable",
        ),
        (
            # LSN-035: the check runs, prints PASS, and its subject was never in scope.
            "the policy's variables block stops parsing",
            lambda s: {**s, POLICY: s[POLICY].replace("    - name: ", "    -  name: ")},
            "no `- name:` variables parsed",
        ),
        (
            "the status struct is renamed out from under the check",
            lambda s: edit(s, TYPES, f"type {STRUCT} struct {{", f"type {STRUCT}V2 struct {{"),
            f"`type {STRUCT} struct` not found",
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
        print(
            "FAIL: ActionRecordStatus and vap-agent-scope-journal disagree about which status "
            "fields exist -- the policy's allow-list fails OPEN on the difference",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    fields = status_fields(sources[TYPES])
    print(
        f"PASS: all {len(fields)} ActionRecordStatus fields are enumerated in "
        f"vap-agent-scope-journal and wired into `nothingChanged` (06 §4.3)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
