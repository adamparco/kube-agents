#!/usr/bin/env python3
"""The `auth can-i` question table for `dev/verify/actor-grant-sweep-l2.sh`, DERIVED from 06.

WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT A LIST IN THE SUITE.
`phase-9.md` binds Accept (e) to V-BRK-013 and says, in the same paragraph, that "the sweep script
asserts the exclusion set BY NAME rather than by 'these are the ones that were there when I wrote
it'". A hand-written table in the suite is exactly the second thing: it is a snapshot of 06 §2.2
taken on the day the suite was written, and the day a verb is added to the spec is the day the sweep
stops asking about it -- silently, with every arm still green. So the table is computed from the
same definition site the L0 half reads, by importing the L0 half's own parser.

WHY IT IMPORTS `dev/tests/actor-grant-single-sourced.py` RATHER THAN RE-PARSING 06.
That file IS V-BRK-013 at L0, and its docstring's first sentence is that the grant "is written down
once". A second YAML-fence parser here would be a second reading of the same spec, free to disagree
with the first -- and the disagreement would surface as an L2 suite that fails while the L0 check
passes, which reads like a cluster defect and is not one. Importing it also means a change to how
the spec is parsed reaches both levels at once. The module name has dashes, so it is loaded through
importlib rather than `import`.

WHAT IT EMITS. One TSV row per question, on stdout:

    tier <TAB> role <TAB> kind <TAB> group <TAB> resource <TAB> subresource <TAB> verb <TAB> where <TAB> expected

  * `where` is `own` (the identity's own namespace) or `elsewhere` (a namespace it has no agent in).
  * `expected` is `yes` or `no` -- the answer `kubectl auth can-i` must give. Both directions come
    out of the same derivation, which is what makes the sweep two-sided rather than a denial test:
    a fleet whose RBAC failed to apply at all answers `no` to everything and fails the `yes` rows.

THE KINDS, and which clause of V-BRK-013 / Accept (e) each one is:

  journal-write   actor  yes  06 §2.2.1's `create actionrecords` and the `status` update/patch. The
                              append half of the journal. V-BRK-013's first named property.
  journal-read    actor  yes  §2.2.1's reads: the record itself, agents, approvalrosters,
                              changepolicies. Without these the broker cannot classify or report.
  freeze-read     actor  yes  `fleetfreezes` get/list/watch. V-BRK-013 names this one SEPARATELY and
                              says it must hold for EVERY tier "including developer-team", because a
                              tier that cannot read the freeze object fails closed permanently
                              (06 §4.4) -- the missing-grant direction bricks a tier rather than
                              failing safe, which is why it is asserted per tier and not once.
  tier-read       actor  yes  The READ half of the tier's own 06 §2.2 template -- what this tier
                              acts on, in the dark profile Phase 9 renders. This is the arm that
                              would go red if a tier's read profile were dropped from the install.
  append-only     actor  no   `update`/`patch`/`delete`/`deletecollection` on the RECORD (not its
                              status subresource). V-BRK-013's append-only property, the direction
                              that matters: an actor that can update an ActionRecord can rewrite the
                              evidence of what it did.
  dark-write      actor  no   The WRITE half of the tier's 06 §2.2 template. 07 §2 says Phase 9's
                              whole machinery runs "with no write authority anywhere", and the
                              install path renders only the read half (P10-T1 inverts the policy and
                              lands the write half). Every one of these is a `no` FOR NOW, and that
                              is the phase's central claim rather than a detail: this is the arm that
                              turns "the broker is in dry-run" from an intention into a measurement.
  elsewhere-write actor  no   A small core set asked in a namespace the identity has no agent in.
                              Catches the cluster-scoped mis-binding: a ClusterRoleBinding where the
                              spec says RoleBinding grants the same verbs everywhere, and every
                              own-namespace answer above is identical either way.
  reader-no-write reader no   The tier's write half AND the journal-append verbs, asked of the READER
                              identity. Accept (e) is "no agent identity in the fleet holds a write
                              verb", and the reader is the identity the agent PROCESS runs as -- the
                              one an LLM's output can actually reach. An actor-only sweep would leave
                              the identity that matters most unasked.
  reader-read     reader yes  A small probe set, of which AT LEAST ONE must answer yes (the suite
                              scores this kind as an any-of, not an all-of, because the explorer
                              grant's exact shape is 06 §2.1's and not this file's subject). Without
                              it every `reader-no-write` row above is satisfied by an identity that
                              was never bound to anything.

WHAT IS DELIBERATELY NOT ASKED, AND IS REPORTED RATHER THAN DROPPED SILENTLY.
06 §2.2's templates use `resources: ["*"]` for whole API groups (`apps/*`, `policy/*`,
`compute.cnrm.cloud.google.com/*`). `kubectl auth can-i <verb> '*'` is a question about the wildcard
RESOURCE, which is not the question the row is making -- RBAC's `*` in a rule matches every
resource, but `*` in a REQUEST matches nothing in particular. Those rows are counted and emitted as
a `#skipped` comment line, which the suite prints. A cap nobody prints is a cap that reads as
coverage (`harness-run` §5).

Usage:
    actor_grant_expectations.py --table            # every row, TSV
    actor_grant_expectations.py --tiers            # the tier names, one per line
    actor_grant_expectations.py --self-test        # derivation sanity, no cluster
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
L0_CHECK = REPO_ROOT / "dev" / "tests" / "actor-grant-single-sourced.py"

# The journal resource, spelled once. Both the append-only rows and the journal-write rows are about
# this one resource and its `status` subresource, and a second spelling is a second thing to keep in
# step with 06.
JOURNAL = ("kubeagents.x-k8s.io", "actionrecords")
FREEZE = ("kubeagents.x-k8s.io", "fleetfreezes")

# The verbs that constitute "a write verb" for Accept (e). `deletecollection` is in the list and is
# not pedantry: it is the verb that empties a namespace in one request, it is absent from every
# template in 06 §2.2, and a sweep that only asks about `delete` would not notice a rule that
# granted it.
MUTATING = ("create", "update", "patch", "delete", "deletecollection")

# The core resources asked in a namespace the identity has no agent in. Deliberately short and
# deliberately core: the property is whether the grant is namespace-scoped, and asking it about
# thirty resources measures the same binding thirty times.
ELSEWHERE_RESOURCES = (("", "secrets"), ("", "configmaps"), ("", "pods"), JOURNAL)

# The reader non-vacuity probes. Any one `yes` satisfies the arm.
READER_PROBES = ((("", "pods"), "list"), (("", "configmaps"), "get"), (JOURNAL, "list"))


def _load_l0():
    """The L0 check, imported as a module. It is the parser of record for 06."""
    if not L0_CHECK.is_file():
        sys.exit(
            f"FAIL: {L0_CHECK} is gone. This table is derived from the L0 half of V-BRK-013, and "
            f"without it there is no definition site to read -- which would make every question "
            f"below one this file invented."
        )
    spec = importlib.util.spec_from_file_location("actor_grant_single_sourced", L0_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _split(triple: str) -> tuple[str, str, str, str]:
    """`group/resource[/subresource]:verb` -> (group, resource, subresource, verb)."""
    path, verb = triple.rsplit(":", 1)
    group, _, rest = path.partition("/")
    resource, _, subresource = rest.partition("/")
    return group, resource, subresource, verb


def rows() -> tuple[list[tuple[str, ...]], int]:
    """Every question, plus the count of wildcard rows skipped."""
    m = _load_l0()
    sources = m.read_sources()
    grant, errs = m.spec_grant(sources[m.SPEC])
    templates, errs2 = m.spec_tier_templates(sources[m.SPEC])
    for e in errs + errs2:
        sys.exit(f"FAIL: 06 did not parse: {e}")
    if not grant or not templates:
        sys.exit(
            "FAIL: 06 parsed to an empty grant or no tier templates. Every row below would be "
            "derived from nothing, and a sweep with no questions passes (LSN-035)."
        )

    out: list[tuple[str, ...]] = []
    skipped = 0

    for tier in sorted(templates):
        tmpl = templates[tier]
        reads = {t for t in tmpl if t.rsplit(":", 1)[-1] in m.READ_VERBS}
        writes = tmpl - reads

        # --- actor: 06 §2.2.1, the grant every actor additionally receives ---------------------
        for t in sorted(grant):
            group, resource, subresource, verb = _split(t)
            if resource == "*":
                skipped += 1
                continue
            if (group, resource) == FREEZE:
                kind = "freeze-read"
            elif verb in m.READ_VERBS:
                kind = "journal-read"
            else:
                kind = "journal-write"
            out.append((tier, "actor", kind, group, resource, subresource, verb, "own", "yes"))

        # --- actor: the append-only direction, on the record itself ----------------------------
        for verb in MUTATING:
            if verb == "create":
                continue  # create IS the grant; it is a `yes` row above
            out.append(
                (tier, "actor", "append-only", JOURNAL[0], JOURNAL[1], "", verb, "own", "no")
            )

        # --- actor: the tier's own template, both halves ----------------------------------------
        for t in sorted(reads):
            group, resource, subresource, verb = _split(t)
            if resource == "*":
                skipped += 1
                continue
            out.append((tier, "actor", "tier-read", group, resource, subresource, verb, "own", "yes"))
        for t in sorted(writes):
            group, resource, subresource, verb = _split(t)
            if resource == "*":
                skipped += 1
                continue
            out.append((tier, "actor", "dark-write", group, resource, subresource, verb, "own", "no"))

        # --- actor: the same verbs, somewhere else ----------------------------------------------
        for group, resource in ELSEWHERE_RESOURCES:
            for verb in MUTATING:
                out.append(
                    (tier, "actor", "elsewhere-write", group, resource, "", verb, "elsewhere", "no")
                )

        # --- reader: no write verb anywhere, and at least one read ------------------------------
        reader_writes = set(writes)
        reader_writes |= {t for t in grant if t.rsplit(":", 1)[-1] not in m.READ_VERBS}
        for t in sorted(reader_writes):
            group, resource, subresource, verb = _split(t)
            if resource == "*":
                skipped += 1
                continue
            out.append(
                (tier, "reader", "reader-no-write", group, resource, subresource, verb, "own", "no")
            )
        for (group, resource), verb in READER_PROBES:
            out.append((tier, "reader", "reader-read", group, resource, "", verb, "own", "yes"))

    return out, skipped


def main() -> int:
    args = sys.argv[1:]
    if "--tiers" in args:
        m = _load_l0()
        templates, errs = m.spec_tier_templates(m.read_sources()[m.SPEC])
        if errs:
            sys.exit(f"FAIL: {errs[0]}")
        for tier in sorted(templates):
            print(tier)
        return 0

    table, skipped = rows()

    if "--self-test" in args:
        # Non-vacuity of the DERIVATION, which is not the same property the suite asserts. The suite
        # can only tell whether the cluster answered correctly; nothing there can tell whether the
        # table it was handed contained any questions at all, and a table of zero rows makes every
        # arm pass. Floors rather than exact counts: 06 §2.2 is expected to grow.
        kinds: dict[str, int] = {}
        tiers: set[str] = set()
        for r in table:
            kinds[r[2]] = kinds.get(r[2], 0) + 1
            tiers.add(r[0])
        problems = []
        for kind in (
            "journal-write",
            "journal-read",
            "freeze-read",
            "tier-read",
            "append-only",
            "dark-write",
            "elsewhere-write",
            "reader-no-write",
            "reader-read",
        ):
            if kinds.get(kind, 0) == 0:
                problems.append(f"kind '{kind}' derived ZERO rows; that clause is unasked")
        if len(tiers) < 3:
            problems.append(f"only {len(tiers)} tier(s) derived ({sorted(tiers)}); 06 §2.2 has three")
        yes = sum(1 for r in table if r[8] == "yes")
        no = sum(1 for r in table if r[8] == "no")
        if yes < 50 or no < 50:
            problems.append(
                f"the table is one-sided: {yes} yes-rows and {no} no-rows. A sweep is two-sided or "
                f"it is not one"
            )
        # Every tier must ask the freeze question, by name -- V-BRK-013 says "every tier, including
        # developer-team", and a table that asked it for two of three would pass on the tier the
        # spec singles out.
        for tier in sorted(tiers):
            if not any(r[0] == tier and r[2] == "freeze-read" for r in table):
                problems.append(f"tier '{tier}' is never asked whether it can read fleetfreezes")
        if problems:
            for p in problems:
                print(f"FAIL: {p}", file=sys.stderr)
            return 1
        print(
            f"PASS: the question table derives {len(table)} rows over {len(tiers)} tiers "
            f"({yes} must-answer-yes, {no} must-answer-no), {skipped} wildcard row(s) skipped; "
            f"every kind is populated and every tier is asked the fleetfreezes question"
        )
        return 0

    print(f"#skipped\t{skipped}")
    for r in table:
        print("\t".join(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
