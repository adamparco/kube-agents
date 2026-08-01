#!/usr/bin/env python3
"""V-CMP-020: each tier's `skills/` tree is its 02 §2.1 row, and only its 02 §2.1 row.

DELIBERATELY NOT ON `dev/L0-CHAIN.txt`, AND NOT IN `verification/implementations.yaml`.
=======================================================================================
This check is RED on the tree, with 37 findings, and every one of them is the persona
conversion that 02 §2.1's own "Renames the conversion must perform" table describes and
that `07` schedules at **P13-T5**: `submit-suggestion` -> `apply-change`,
`raise-escalation` -> `escalate`, `propose-*` -> `provision-*`, the new `delegate`, and
the four workload skills the Developer Team does not yet hold. 02 §2.1 says so in as many
words -- *"The old skills exist today under `agents/*/skills/`; 07 sequences the swap."*

So this is not a defect this check found. It is a conversion this check will verify, and
the honest state until then is `deferred` with a named blocker, not `pass` and not a
chain line that turns CI red for four phases. V-CMP is BLOCKING-PHASE, not
BLOCKING-ALWAYS, so 09 §9.6 permits the deferral.

  Blocker:              07 P13-T5 (persona conversion)
  Promotion condition:  `python3 dev/tests/tier-skills-match-the-allocation.py` exits 0.
                        On that day, add it and its `--negative-control` to
                        `dev/L0-CHAIN.txt` and add its `implementations.yaml` row.

The file is committed ahead of that day on purpose: it makes the promotion condition a
command rather than a paragraph, and `implementations.yaml`'s own rule -- *"a check with
no green row is absent, and that is correct"* -- is about the REGISTRY, which is why
there is no row for it there.

A CATALOGUING FINDING, recorded and not acted on here.
------------------------------------------------------
09 §6 dates this row at phase **8**. It dates `V-CMP-021` -- the structurally identical
row over jobs and SOPs, blocked by the same conversion, cited to the same §5.3 -- at
phase **13**. One of the two datings is wrong, and it is almost certainly this one.
Correcting it is a spec edit that would remove this row from the phase-8/9 required set,
which is exactly the change Guardrail 9 forbids inside the unit whose check it greens.
It belongs to `harness-improve`.

02 §2.1 is the only place the roster's capabilities are allocated. It is a twelve-row table with one
column per tier, and it is prose-adjacent in the worst way: a ✅ moved one column left is a valid
Markdown edit, a skill directory copied one tier up is a valid `cp -r`, and neither leaves a mark
anywhere else in the tree. `make validate` asserts that skills live under `agents/*/skills/` and not
under `agents/*/defaults/skills/`, and that each one carries a `SKILL.md`. Both are rules about
SHAPE. Nothing in the tree has ever asserted the ALLOCATION -- which tier holds which skill -- so
the table and the directories have been free to disagree, and they do.

The failure this catches is not a typo. A skill is an authority: `gke-cluster-creator` under
`agents/developer-team/skills/` is a namespace-scoped agent holding the instructions for creating a
cluster, and the containment argument in 03 §4.2 has two layers -- "cannot express" and "cannot
cause". The identity pair holds the second. 02 §2.1 IS the first, and a skill set that quietly
becomes a superset dissolves it without touching a single RBAC rule. The reverse is quieter still: a
tier missing a skill its row promises is an agent that answers "I can't do that" to work the design
says is its own, and the only artifact that would have noticed is the table nobody diffs.

Five properties.

  1. NON-VACUITY. 02 §2.1's allocation table is found under its own heading, its four column
     headings are the ones this check binds, every row's cells are readable, no skill is named on
     two rows, all three tier columns come out with a non-empty skill set, the two skills in
     `PER_SKILL_COLUMNS` were actually found, and all three `agents/<tier>/skills/` directories
     exist and are non-empty. Properties 2-4 are set comparisons, and a set comparison against a
     table that did not parse is the greenest thing in this repo.
  2. NOTHING MISSING. Every skill 02 §2.1 gives to a tier exists as a directory under that tier.
  3. NOTHING UNPUBLISHED. Every directory under `agents/<tier>/skills/` whose name appears on NO row
     of 02 §2.1 is drift in the direction the spec cannot see -- a capability shipped into an agent
     pod that the allocation never granted.
  4. NOTHING BORROWED. The cross-tier property, stated separately from 3 because it is a different
     accident with a different fix: the directory's name IS in 02 §2.1, and 02 §2.1 gives it to
     other tiers. 02 §2.1 legitimately shares -- `apply-change`, `detect-drift`, `gke-observability`
     and `read-knowledge` go to all three (the section says so in the sentence under the table),
     `gke-multi-tenancy` to two, `delegate` to two, `escalate` to two -- so the property is NOT "no
     skill appears under more than one tier". It is: for every skill, the set of tiers holding it on
     disk equals the set of tiers 02 §2.1 names. Property 2 holds one direction of that equality and
     this one holds the other, which is why a shared skill cannot be quietly widened to a fourth
     column's worth of tiers.
  5. THE PER-SKILL BINDING IS TOTAL OVER ITS ROW. See `PER_SKILL_COLUMNS` below: one row of 02 §2.1
     names two skills whose ✅s are not interchangeable, and the binding that resolves it is stated
     here. Every tier that row checks must be claimed by exactly one bound skill, and every tier a
     bound skill claims must be checked on that row. Without the second direction the binding rots
     in silence: 02 §2.1 gains a third ✅ on that row, the binding assigns it to neither skill,
     property 2 never asks the tier for anything, and the new grant is unasserted.

ON SPELLING, since it is the first thing to check in a two-artifact comparison: 02 §2.1's skill names
and the directory names are spelled IDENTICALLY -- every name in the table is already
`lowercase-hyphenated` and is used verbatim as a path in the same section's rename table
(`agents/platform/skills/provision-cluster-admin/`). So there is no rename map in this file and
there must not be one; the binding is the identity function, and property 3 is what keeps it that
way -- a spec name that stops matching a directory name shows up as a missing skill AND an
unpublished directory, one finding from each direction, rather than as a silent pass.

TWO THINGS ARE HARDCODED, both because the table cannot state them and neither is a number that
drifts. `TIER_COLUMNS` binds the column headings to directory names (`Cluster Admin` ->
`cluster-admin`); property 1 asserts the headings so the binding cannot outlive them.
`PER_SKILL_COLUMNS` resolves the `provision-cluster-admin` / `provision-developer-team` row, whose
two cells read `✅ | ✅ | —` and cannot say which ✅ belongs to which of the two skills -- every
other row assigns all of its skills to all of its checked columns, and this one does not. 02 §2.1's
own rename table settles it (`agents/platform/skills/provision-cluster-admin/` and
`agents/cluster-admin/skills/provision-developer-team/`), and property 5 makes the binding total in
both directions so it cannot silently stop describing the row.

WHAT THIS DOES NOT ASSERT, deliberately: that a `SKILL.md` exists, that skills are absent from
`agents/*/defaults/skills/`, or that any skill does what its name says. The first two are
`make validate`'s rules and duplicating them here would give two checks one property and neither a
clear owner. The consequence worth knowing: a skill MOVED to `defaults/skills/` reads here as
missing from its tier, and `make validate` is the check that names where it went.

Run:  python3 dev/tests/tier-skills-match-the-allocation.py
      python3 dev/tests/tier-skills-match-the-allocation.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "design" / "02-agent-personas.md"
AGENTS = REPO / "agents"

# The allocation table's column headings, in order, bound to the directory under `agents/`. The
# mapping is mechanical (lowercase, space -> hyphen) but it is written out rather than computed:
# a computed mapping would happily follow a renamed column into a directory that does not exist,
# and property 1 asserts the headings so this binding cannot outlive the table it reads.
TIER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Platform", "platform"),
    ("Cluster Admin", "cluster-admin"),
    ("Developer Team", "developer-team"),
)
TIERS = tuple(directory for _, directory in TIER_COLUMNS)

# The one row 02 §2.1 cannot resolve on its own. `**provision-cluster-admin** / **provision-
# developer-team** (§6)` carries `✅ | ✅ | —`, and the general rule -- every skill on a row goes to
# every column the row checks -- would hand BOTH skills to BOTH tiers, which is not what §6 or the
# section's own rename table says. The rename table states the pairing as two paths:
# `agents/platform/skills/provision-cluster-admin/` and
# `agents/cluster-admin/skills/provision-developer-team/`. That pairing is the binding, hardcoded
# once here, and property 5 asserts it is total over its row in both directions.
PER_SKILL_COLUMNS: dict[str, frozenset[str]] = {
    "provision-cluster-admin": frozenset({"platform"}),
    "provision-developer-team": frozenset({"cluster-admin"}),
}

SECTION = re.compile(r"^### 2\.1 Skill allocation\s*$", re.M)
NEXT_SECTION = re.compile(r"^#{1,3} ", re.M)
CODE_SPAN = re.compile(r"`([^`]+)`")
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHECKED = "✅"
UNCHECKED = {"", "—", "-"}

Allocation = dict[str, set[str]]  # tier directory -> skill names
Disk = dict[str, list[str]]  # tier directory -> directory names under skills/


def split_row(line: str) -> list[str] | None:
    """A Markdown table row as its cells, or None if the line is not one."""
    if not line.startswith("|") or not line.rstrip().endswith("|"):
        return None
    return [cell.strip() for cell in line.rstrip()[1:-1].split("|")]


def parse_spec(text: str) -> tuple[list[tuple[list[str], set[str]]], list[str]]:
    """02 §2.1's allocation table as [(skills on the row, tiers the row checks)], plus parse gripes.

    Commentary after an em dash is dropped before the code spans are read, and that is load-bearing
    rather than tidy: the `escalate` row reads ``**`escalate`** — one-hop mesh call to `parentRef`
    (§2.3)``, so a reader that takes every code span in the cell comes away believing 02 §2.1
    allocates a skill called `parentRef`. The gripes list is returned instead of raised because an
    unreadable cell has to be a finding -- a table that half-parses is how a set comparison goes
    quiet.
    """
    rows: list[tuple[list[str], set[str]]] = []
    gripes: list[str] = []

    start = SECTION.search(text)
    if not start:
        return rows, ["VACUOUS: 02 has no `### 2.1 Skill allocation` heading to read a table under."]
    tail = text[start.end() :]
    end = NEXT_SECTION.search(tail)
    section = tail[: end.start()] if end else tail

    headings = [heading for heading, _ in TIER_COLUMNS]
    lines = section.splitlines()
    header_at = None
    for i, line in enumerate(lines):
        cells = split_row(line)
        if cells and len(cells) == 4 and cells[0] == "Skill(s)":
            header_at = i
            if cells[1:] != headings:
                gripes.append(
                    f"VACUOUS: 02 §2.1's allocation table is headed {cells[1:]}, and this check "
                    f"binds the columns {headings} to {list(TIERS)}. A renamed column would be read "
                    f"as a renamed tier and every skill under it would compare against nothing."
                )
            break
    if header_at is None:
        return rows, [
            "VACUOUS: 02 §2.1 has no `| Skill(s) | Platform | Cluster Admin | Developer Team |` "
            "table header, so the allocation this check compares the tree against was never read."
        ]
    if gripes:
        return rows, gripes

    seen: dict[str, int] = {}
    for line in lines[header_at + 1 :]:
        cells = split_row(line)
        if cells is None:
            break
        if len(cells) != 4:
            gripes.append(
                f"02 §2.1's allocation table has a {len(cells)}-cell row where 4 are expected: "
                f"{line.strip()[:80]!r}. The columns are read positionally."
            )
            break
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue  # the |---|:--:| separator

        label = re.split(r"—|--", cells[0])[0]
        skills: list[str] = []
        for span in CODE_SPAN.findall(label):
            if not SKILL_NAME.match(span):
                gripes.append(
                    f"02 §2.1 row {cells[0][:60]!r} names `{span}` in its Skill(s) cell and that is "
                    f"not a directory-shaped skill name. Either the row allocates something this "
                    f"check cannot look for on disk, or prose crossed to the left of the em dash "
                    f"and is being read as an allocation."
                )
                continue
            if span in seen:
                gripes.append(
                    f"02 §2.1 names `{span}` on two rows (rows {seen[span]} and {len(rows) + 1}). "
                    f"Each row is read as the complete statement of which tiers hold the skills it "
                    f"names; two rows for one skill means neither is."
                )
                continue
            seen[span] = len(rows) + 1
            skills.append(span)

        checked: set[str] = set()
        for (heading, directory), cell in zip(TIER_COLUMNS, cells[1:]):
            if CHECKED in cell:
                checked.add(directory)
            elif cell not in UNCHECKED:
                gripes.append(
                    f"02 §2.1's {heading} cell on row {cells[0][:60]!r} reads {cell!r}, which is "
                    f"neither a ✅ nor an empty/— cell. This check reads a cell as granted only on "
                    f"the ✅, so an unrecognised spelling of 'yes' silently revokes the grant."
                )
        if skills:
            rows.append((skills, checked))
    return rows, gripes


def allocate(rows: list[tuple[list[str], set[str]]]) -> tuple[Allocation, dict[str, set[str]]]:
    """Expand the rows into tier -> skills and skill -> tiers, applying `PER_SKILL_COLUMNS`."""
    by_tier: Allocation = {tier: set() for tier in TIERS}
    by_skill: dict[str, set[str]] = {}
    for skills, checked in rows:
        for skill in skills:
            granted = checked & PER_SKILL_COLUMNS.get(skill, frozenset(TIERS))
            by_skill[skill] = set(granted)
            for tier in granted:
                by_tier[tier].add(skill)
    return by_tier, by_skill


def ordered(tiers: set[str]) -> list[str]:
    """Tiers in 02 §2.1's column order, so a finding reads the way the table does."""
    return [tier for tier in TIERS if tier in tiers]


def check(spec_text: str, disk: Disk) -> list[str]:
    rows, gripes = parse_spec(spec_text)
    by_tier, by_skill = allocate(rows)

    # --- 1. non-vacuity -----------------------------------------------------------------------
    vacuous: list[str] = list(gripes)
    for tier in TIERS:
        if not by_tier.get(tier):
            vacuous.append(
                f"VACUOUS: 02 §2.1's allocation table yielded no skill at all for {tier}. Every "
                f"property below is a set comparison, and 'nothing missing' over an empty spec set "
                f"is a pass that asserts nothing."
            )
    for skill, bound in PER_SKILL_COLUMNS.items():
        if skill not in by_skill:
            vacuous.append(
                f"VACUOUS: this check binds `{skill}` to {ordered(set(bound))} because 02 §2.1's "
                f"row cannot say which ✅ is which, and 02 §2.1 no longer names `{skill}` on any "
                f"row. The binding now describes nothing, and the row it came from is unasserted."
            )
    for tier in TIERS:
        if not disk.get(tier):
            vacuous.append(
                f"VACUOUS: `agents/{tier}/skills/` does not exist or holds no skill directory, so "
                f"the tier this check is comparing against 02 §2.1 is the empty set."
            )
    if vacuous:
        return vacuous

    findings: list[str] = []

    # --- 2. nothing missing -------------------------------------------------------------------
    for tier in TIERS:
        present = set(disk[tier])
        for skill in sorted(by_tier[tier]):
            if skill not in present:
                findings.append(
                    f"02 §2.1 gives `{skill}` to {tier} and `agents/{tier}/skills/{skill}/` does "
                    f"not exist. The tier ships without a capability its own row promises, and the "
                    f"agent answers 'I can't do that' to work the design calls its own."
                )

    # --- 3. nothing unpublished / 4. nothing borrowed -------------------------------------------
    for tier in TIERS:
        for skill in sorted(disk[tier]):
            if skill in by_tier[tier]:
                continue
            if skill not in by_skill:
                findings.append(
                    f"`agents/{tier}/skills/{skill}/` exists and 02 §2.1 names no such skill on any "
                    f"row. A capability shipped into an agent pod that the allocation never granted "
                    f"is the half of the drift the spec cannot see."
                )
            elif not by_skill[skill]:
                findings.append(
                    f"`agents/{tier}/skills/{skill}/` exists, and 02 §2.1 names `{skill}` but "
                    f"gives it to no tier -- its row carries no ✅ in any column. A skill nobody is "
                    f"allocated is still installed on {tier}."
                )
            else:
                owners = ordered(by_skill[skill])
                findings.append(
                    f"`agents/{tier}/skills/{skill}/` exists, and 02 §2.1 gives `{skill}` to "
                    f"{', '.join(owners)} -- not to {tier}. 02 §2.1 is the 'cannot express' half of "
                    f"03 §4.2's containment argument; a tier holding another tier's skill dissolves "
                    f"it without touching one RBAC rule."
                )

    # --- 5. the per-skill binding is total over its row ------------------------------------------
    for skills, checked in rows:
        bound_here = [s for s in skills if s in PER_SKILL_COLUMNS]
        if not bound_here:
            continue
        # Only the two directions an INPUT can break are asserted. A third arm -- two bound skills
        # claiming the same tier -- was written and removed: PER_SKILL_COLUMNS is source, not input,
        # so no mutation can reach it, and it is caught anyway from the other side (the tier the
        # second skill stopped claiming then reads as an unclaimed ✅ below). A branch no control can
        # exercise reads as protection and is not.
        claimed: set[str] = set()
        for skill in bound_here:
            for tier in PER_SKILL_COLUMNS[skill]:
                if tier not in checked:
                    findings.append(
                        f"This check binds `{skill}` to {tier}, and 02 §2.1's row for it no longer "
                        f"checks the {tier} column. The binding is asserting a grant the table has "
                        f"withdrawn."
                    )
                claimed.add(tier)
        for tier in ordered(checked - claimed):
            findings.append(
                f"02 §2.1's `{skills[0]}` row checks {tier} and no skill this check binds to that "
                f"column claims it, so the ✅ grants nothing and nothing on disk has to match it. "
                f"Add the tier to PER_SKILL_COLUMNS -- an unclaimed ✅ on this row is invisible to "
                f"every property above."
            )

    return findings


def read_disk() -> Disk:
    """The directory names under each tier's `skills/`. Dot-directories are not skills."""
    disk: Disk = {}
    for tier in TIERS:
        root = AGENTS / tier / "skills"
        if not root.is_dir():
            disk[tier] = []
            continue
        disk[tier] = sorted(
            child.name for child in root.iterdir() if child.is_dir() and not child.name.startswith(".")
        )
    return disk


def _inputs() -> tuple[str, Disk]:
    if not SPEC.exists():
        raise SystemExit(f"FAIL: {SPEC.relative_to(REPO)} does not exist")
    return SPEC.read_text(encoding="utf-8"), read_disk()


def run() -> int:
    spec_text, disk = _inputs()
    findings = check(spec_text, disk)
    if findings:
        print("FAIL: V-CMP-020 -- the tiers' skills/ trees are not 02 §2.1's allocation", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1
    rows, _ = parse_spec(spec_text)
    by_tier, by_skill = allocate(rows)
    shared = sum(1 for tiers in by_skill.values() if len(tiers) > 1)
    counts = ", ".join(f"{tier} {len(by_tier[tier])}" for tier in TIERS)
    print(
        f"PASS: V-CMP-020 (L0) -- 02 §2.1 allocates {len(by_skill)} skill(s) over "
        f"{len(rows)} row(s) ({counts}); every one exists under every tier its row names, every "
        f"skills/ directory on disk is on its own tier's row, and the {shared} skill(s) 02 §2.1 "
        f"shares are held on disk by exactly the tiers it shares them with"
    )
    return 0


def _mutate(base: tuple[str, Disk], index: int, fn) -> tuple[str, Disk]:
    out: list = [base[0], {tier: list(skills) for tier, skills in base[1].items()}]
    out[index] = fn(out[index])
    return (out[0], out[1])


def _without(tier: str, skill: str):
    return lambda d: {**d, tier: [s for s in d[tier] if s != skill]}


def _with(tier: str, skill: str):
    return lambda d: {**d, tier: sorted(set(d[tier]) | {skill})}


def _control_base() -> tuple[str, Disk]:
    """A green pair to mutate: 02 §2.1's text, and the tree 02 §2.1 describes.

    This control CANNOT use the working tree, and the reason is a scheduled one rather than a bug.
    07 P13-T5 is the unit that converts the personas, and it says in as many words that the
    allocation is wrong today -- the Developer Team holds none of the seven workload skills 02 §2.1
    assigns it and the Platform Agent carries the whole superset -- so `run()` is red on purpose and
    will stay red until phase 13. A control based on the live tree would print BROKEN for five
    phases, and a control that cannot run is indistinguishable from one that passes ([[LSN-038]]),
    which is the failure this whole file is written against.

    So the disk side is PROJECTED from the spec side through this check's own parser. That makes the
    base green by construction, which is exactly what a mutation harness needs and nothing more: the
    base proves no property here, and every row below is a defect injected into a pair that is known
    to agree. The projection is not a free pass either -- it runs the real parser, so a parser that
    stops reading 02 §2.1 yields empty tiers, property 1 fires on the base, and the control reports
    BROKEN instead of quietly scoring twelve mutations against nothing.
    """
    spec_text = SPEC.read_text(encoding="utf-8")
    rows, _ = parse_spec(spec_text)
    by_tier, _ = allocate(rows)
    return spec_text, {tier: sorted(by_tier[tier]) for tier in TIERS}


def negative_control() -> int:
    """Each mutation is a way the allocation could drift, and each names the signal it must produce.

    The drift is symmetric -- 02 §2.1 and the tree are two artifacts with no compiler between them
    -- so both sides are mutated. A skill moved on disk and the same skill moved in the table have
    to be caught by two different messages, or the check is only asserting that the two differ and
    not which one moved.

    Every branch in `check` and `parse_spec` that can append a finding is reached by exactly one row
    below. That is the reason for eighteen rows rather than the eight the property list would
    suggest: a parse guard with no mutation behind it is a guard nobody has ever seen fire, and the
    ones here -- the truncated row, the code span left of the em dash, the retitled heading -- are
    the arms that would turn a real disagreement into a silent short read.
    """
    base = _control_base()
    live = check(*_inputs())
    findings = check(*base)
    if findings:
        print("  BROKEN   the projected base is not green, so no row below can be attributed")
        for f in findings[:4]:
            print(f"           {f}")
        print("FAIL: V-CMP-020 negative control -- 0 mutations evaluated")
        return 1
    print(
        f"  (base: 02 §2.1 projected onto the tree it describes; the WORKING tree is "
        f"{len(live)} finding(s) from 02 §2.1 today -- 07 P13-T5 -- which is why the live tree "
        f"cannot serve as a control base)"
    )

    mutations = [
        (
            "a tier loses a skill its own row promises",
            _mutate(base, 1, _without("platform", "gke-cluster-creator")),
            "gives `gke-cluster-creator` to platform and `agents/platform/skills/",
        ),
        (
            "a tier loses a SHARED skill, which the other two tiers still hold",
            _mutate(base, 1, _without("cluster-admin", "gke-multi-tenancy")),
            "gives `gke-multi-tenancy` to cluster-admin and",
        ),
        (
            "a tier borrows a single-tier skill from the tier above it",
            _mutate(base, 1, _with("developer-team", "gke-cost-analysis")),
            "gives `gke-cost-analysis` to platform -- not to developer-team",
        ),
        (
            "the parent borrows a skill 02 §2.1 shares between the two tiers BELOW it",
            _mutate(base, 1, _with("platform", "escalate")),
            "gives `escalate` to cluster-admin, developer-team -- not to platform",
        ),
        (
            "a skill directory appears that 02 §2.1 has never heard of",
            _mutate(base, 1, _with("cluster-admin", "gke-node-surgery")),
            "gke-node-surgery/` exists and 02 §2.1 names no such skill on any row",
        ),
        (
            "02 §2.1 revokes a row's only ✅ while the directories stay put",
            _mutate(base, 0, lambda t: t.replace(
                "| `gke-cluster-creator`, `gke-cluster-lifecycle`, `gke-cost-analysis`                              |         ✅         |",
                "| `gke-cluster-creator`, `gke-cluster-lifecycle`, `gke-cost-analysis`                              |                    |",
            )),
            "names `gke-cluster-creator` but gives it to no tier",
        ),
        (
            # The cell is emptied to a bare em dash rather than defaced, and that is not cosmetic:
            # the first draft wrote `- fleet view`, which is neither a ✅ nor an UNCHECKED token, so
            # the unreadable-cell arm of property 1 fired first and this row scored MISS against a
            # property it never reached. That is [[LSN-035]]'s shape exactly -- the broad rule
            # swallowing the narrow one -- and the per-row needle is what surfaced it.
            "02 §2.1 withdraws a shared skill from one tier and the tree does not follow",
            _mutate(base, 0, lambda t: t.replace(
                "| `gke-observability`, `detect-drift` (detect **and remediate**)                                   |   ✅ fleet view    |",
                "| `gke-observability`, `detect-drift` (detect **and remediate**)                                   |         —         |",
            )),
            "gives `detect-drift` to cluster-admin, developer-team -- not to platform",
        ),
        (
            "a ✅ is respelled as a word, which would silently revoke the grant",
            _mutate(base, 0, lambda t: t.replace(
                "| `read-knowledge` (OKF)                                                                           |         ✅         |",
                "| `read-knowledge` (OKF)                                                                           |        yes         |",
            )),
            "cell on row '`read-knowledge` (OKF)' reads 'yes'",
        ),
        (
            "the same skill is named on two rows, so neither row is the whole statement",
            _mutate(base, 0, lambda t: t.replace(
                "| `read-knowledge` (OKF) ", "| `read-knowledge` (OKF), `detect-drift` "
            )),
            "02 §2.1 names `detect-drift` on two rows",
        ),
        (
            "the provision row gains a third ✅ that the per-skill binding claims for nobody",
            _mutate(base, 0, lambda t: t.replace(
                "| **`provision-cluster-admin`** / **`provision-developer-team`** (§6)                              |         ✅         |         ✅          |         —          |",
                "| **`provision-cluster-admin`** / **`provision-developer-team`** (§6)                              |         ✅         |         ✅          |         ✅         |",
            )),
            "row checks developer-team and no skill this check binds to that column claims it",
        ),
        (
            "the provision row loses a ✅ the per-skill binding still claims",
            _mutate(base, 0, lambda t: t.replace(
                "| **`provision-cluster-admin`** / **`provision-developer-team`** (§6)                              |         ✅         |",
                "| **`provision-cluster-admin`** / **`provision-developer-team`** (§6)                              |         —         |",
            )),
            "binds `provision-cluster-admin` to platform, and 02 §2.1's row for it no longer checks",
        ),
        (
            "a bound skill is renamed in 02 §2.1 and PER_SKILL_COLUMNS is not touched",
            _mutate(base, 0, lambda t: t.replace(
                "**`provision-developer-team`** (§6)", "**`provision-dev-team`** (§6)"
            )),
            "VACUOUS: this check binds `provision-developer-team` to ['cluster-admin']",
        ),
        (
            "a tier column is renamed in the header, so its skills would compare against nothing",
            _mutate(base, 0, lambda t: t.replace("|    Cluster Admin    |", "|    Cluster Ops      |")),
            "VACUOUS: 02 §2.1's allocation table is headed",
        ),
        (
            "the allocation table's header row is rewritten out of recognition",
            _mutate(base, 0, lambda t: t.replace("| Skill(s)  ", "| Capability ")),
            "VACUOUS: 02 §2.1 has no `| Skill(s) | Platform | Cluster Admin | Developer Team |`",
        ),
        (
            "a row loses a column separator, where a positional read would silently truncate",
            _mutate(base, 0, lambda t: t.replace(
                "| `read-knowledge` (OKF)                                                                           |         ✅         |         ✅          |         ✅         |",
                "| `read-knowledge` (OKF)                                                                           |         ✅                   ✅          |         ✅         |",
            )),
            "allocation table has a 3-cell row where 4 are expected",
        ),
        (
            "prose crosses to the left of the em dash and reads as an allocated skill",
            _mutate(base, 0, lambda t: t.replace(
                "| **`apply-change`** — build and submit an Action Envelope",
                "| **`apply-change`**, an `ActionEnvelope` — build and submit an Action Envelope",
            )),
            "names `ActionEnvelope` in its Skill(s) cell",
        ),
        (
            "§2.1's heading is retitled, so the table is looked for in the wrong section",
            _mutate(base, 0, lambda t: t.replace(
                "### 2.1 Skill allocation", "### 2.1 Skills by persona"
            )),
            "VACUOUS: 02 has no `### 2.1 Skill allocation` heading",
        ),
        (
            "a tier's skills/ tree is emptied, which no comparison would notice on its own",
            _mutate(base, 1, lambda d: {**d, "developer-team": []}),
            "VACUOUS: `agents/developer-team/skills/` does not exist or holds no skill directory",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        # A mutation that did not change its input cannot be evaluated: the unmutated base is
        # re-checked, comes back clean, and the row prints MISS -- the verdict for "the check let
        # the defect through" -- over a defect that was never applied ([[LSN-063]]). The spec-side
        # mutations here are whitespace-exact replacements against an aligned Markdown table, which
        # is precisely the edit that stops applying when someone reflows the file.
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
        f"{'PASS' if not failures else 'FAIL'}: V-CMP-020 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught, from both ends of the drift"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
