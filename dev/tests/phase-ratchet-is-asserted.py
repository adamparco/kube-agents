#!/usr/bin/env python3
"""The 09 §10 phase ratchet, derived rather than remembered, and asked for evidence.

WHY THIS EXISTS. On 2026-07-31 `harness-milestone` was invoked for Phase 9 with the task ladder
finished -- 70 of 70 in-phase leaf units done -- and stopped at its §1, because **23 of the 75 check
IDs the phase requires had never been run at all**, 8 of them from BLOCKING-ALWAYS suites. Nothing
was red. Nothing was deferred. The gate script `dev/verify/verify-phase9.sh` names 18 check IDs and
has no V-ISO section at all, so the missing 23 could not have turned it red: a gate that never names
an ID cannot fail for it.

That gap was PREDICTED, in writing, by the phase's own plan. `docs/build/phase-9.md` § "Planning
defect 4" says, on 2026-07-27, that seventeen ratchet checks are unrun because no Accept bullet names
them, and declares the resolution: *"`verify-phase9.sh` runs the ratchet, not the Accept list."* Half
of it was built -- the acceptance table gained four ratchet-only rows. The other half was never built
at all. [[LSN-019]]: prose on the artifact is not a mechanization, so a correct prediction of a real
gap sat in the repository for four days and changed nothing. This file is the other half.

WHAT IT ASSERTS. Four properties, for a phase named on the command line:

  1. THE REQUIRED SET IS DERIVED, AND THE DERIVATION FOUND SOMETHING. The set comes from parsing
     09 §10's ratchet table rows for EVERY phase up to and including this one (expanding each suite
     name against the 09 §6 catalog, honouring a `(Lk)` qualifier and the catalog's own Phase
     column) UNION the phase file's own acceptance table. Neither source may parse to nothing. A
     hand-written list in this file would be the same artifact that failed -- one more place to
     forget -- and a parse that silently matches nothing scores every unrun check as satisfied
     ([[LSN-048]]).
  2. EVERY REQUIRED CHECK HAS A GREEN ROW. A `pass` in `verification/results.csv` carrying a
     non-empty `evidence_ref`. 09 §9.4: a pass with no evidence reference is recorded as `skipped`.
  3. THE BLOCKING-ALWAYS MEMBERS ARE REPORTED SEPARATELY. 09 §9.6 forbids deferring them, so "not
     green" and "not green and undeferrable" are two different facts and a single count hides the
     second inside the first.
  4. THE PHASE FILE'S ACCEPTANCE TABLE DOES NOT UNDER-NAME ITS OWN RATCHET. Any ID that 09 §10
     requires and the acceptance table does not name is reported -- the TABLE, not the file: an ID
     mentioned in a paragraph is not an ID bound to an acceptance bullet, and the difference is the
     whole of [[LSN-019]]. This is the property that catches planning defect 4 *at PLAN*, in the next
     phase, instead of at the milestone. It is orthogonal to property 2: an ID can be absent from the
     table and still green (the ratchet required it, another phase's work proved it), and an ID can
     be in the table and not green.

HOW A SUITE NAME EXPANDS, AND WHY THAT TOOK TWO GOES. 09 §10's cell is one of three shapes: a bare
suite (`V-BRK`), a suite with a prose or level qualifier (`V-CTN (read-side)`, `V-GAT (L1)`), or an
explicit ID list (`V-ISO-001/002/006`). The first draft expanded a bare suite to EVERY member of it
and dropped the prose qualifier as "prose that carries no ID of its own". That over-required, and
by a lot: at phase 9 it demanded V-BRK-016 (*post-execution* journal failure -- the write lands and
the record cannot be completed) of a phase whose definition is "no write authority anywhere"
(07 §2), and V-RUN-014, whose own catalog row dates it to **phase 15**.

The column that fixes it was there all along. 09 §6's preamble says each catalog row carries "the
roadmap phase **by which it must be green**" -- a per-check due date, in the table 09 calls "the
authoritative index". So a bare suite at ratchet row N means *the members of that suite due by N*,
and three things independently confirm it:

  * §10's later rows RE-NAME individual members of suites already in the ratchet -- V-REV-008 at 14,
    V-ADV-003/005 at 13, V-CTN-010/013/018/019 at 11. Under whole-suite expansion every one of those
    is dead text, because the suite entered at 9, 10 and 8 respectively.
  * The Phase column reconstructs §10's own prose qualifier exactly. `V-CTN (read-side)` at 8 and
    `V-CTN (write-side)` at 10 partition the suite; filtering V-CTN by `phase <= 8` yields precisely
    the reader/attenuation/cardinality rows, and `phase == 10` yields precisely the actor-write and
    forbidden-rule rows. Two encodings of one partition, and only one of them is machine-readable.
  * `dev/verify/broker-execute-l2.sh`'s header already says a phase-10 member is unobservable here:
    "V-BRK-019 (the field manager string) is not observable from a shadow".

And the same reading fixes an UNDER-requirement in the other direction, which is why this is a
derivation fix and not a relaxation: §10 opens with "once a suite enters the ratchet it never
leaves", and the first draft read only the row for the phase named on the command line. Phase 9
therefore did not require V-CTN, V-CTR, V-CMP or V-MET at all, though all four entered at phase 8.
Rows now accumulate for every phase <= N. At phase 9 the two corrections are 21 IDs out and 31 in --
the ratchet goes **70 -> 80**, the required set 75 -> 98, and the checks it reports as not green
27 -> 34, of which BLOCKING-ALWAYS 11 -> 19. A change that made the gate cheaper would not have that
shape. What comes IN is the whole of V-CTN's read side, V-CTR core, and the V-MET meta-suite, none of
which phase 9 was asking for despite all three entering at phase 8.

A catalog row with NO Phase cell -- the nine V-MET rows of §11, which are meta-checks that apply at
every phase -- is required wherever its suite is named. That exemption is conservative (it can only
keep a check in) and it is COUNTED AND PRINTED rather than applied in silence, because a filter that
quietly drops what it cannot classify is the failure this whole file exists to catch.

An ID §10 names OUTRIGHT is never phase-filtered. The explicit form is how §10 pulls one member of a
suite forward or holds one back, so filtering it by the column it exists to override would make the
explicit form unable to mean anything. At phase 9 that branch is inert -- §10's only explicit IDs by
then are `V-ISO-001/002/006` and 09 §6 already dates all three to 9 -- so `--negative-control` cannot
stage a case for it and does not pretend to; the first row that names a member out of phase is what
will exercise it.

WHAT IT DELIBERATELY DOES NOT DO: infer coverage from a file naming a check ID. `git grep V-ISO-001`
finds `pair_netpol.go:68` and `pair_netpol_test.go:35`, and BOTH hits exist in order to DISCLAIM the
check -- *"V-ISO-001/002 ask whether a packet is DROPPED, which is L2 and belongs to P9-T9."* A
grep-based notion of "asserted" would have counted those two as coverage. Only a results row is
evidence, which is exactly what 09 §9.4 already says and what this check is here to enforce rather
than restate. The naming scan below is printed as an unweighted HINT, labelled as such, and no
verdict reads it.

WHERE IT RUNS, AND WHY NOT ON THE L0 CHAIN. This is a phase-gate arm, invoked by
`dev/verify/verify-phase<N>.sh` and by `harness-milestone` §2. It is RED on today's tree by
construction -- that is the point, and it is the same "detected rather than remembered" shape
section G of the phase-9 gate already uses. It is NOT on `dev/L0-CHAIN.txt`: the chain is a required
PR check, and an arm that stays red until an entire phase closes would redden every unrelated
commit's CI, which destroys the per-commit attribution CHECKPOINT exists to produce ([[LSN-055]]).
What keeps it from being forgotten is `dev/tests/invariants-gate.py`
(`check_phase_gate_runs_its_own_ratchet`), which runs at L0 and asserts that every `verify-phase<N>.sh`
in the tree invokes this file -- green today, and the half that cannot be quietly removed.

Exit codes:  0 every required check is green · 1 a property failed · 2 bad usage.

Run:  python3 dev/tests/phase-ratchet-is-asserted.py --phase 9
      python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 --negative-control
"""

from __future__ import annotations

import argparse
import csv
import io
import pathlib
import re
import subprocess
import sys
from typing import NamedTuple

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "design" / "09-verification-and-validation.md"
RESULTS = REPO / "verification" / "results.csv"

# 09 §5's gate-class table. A member of one of these suites may never be deferred (09 §9.6), so
# "required and not green" splits into two populations rather than one count.
BLOCKING_ALWAYS = ("V-CTN", "V-BRK", "V-REV", "V-ISO", "V-ADV", "V-MET")

# A row of the 09 §6 catalog: `| V-XXX-nnn | text | ... | L0, L2 | 9 |`. The level cell is located
# by shape, not by column index, because the catalog's sub-tables do not all carry the same columns
# (some have a spec-reference cell, some do not). The PHASE cell is located by position -- it is the
# last cell of the row and it is a bare integer -- because a shape match would also catch a digit
# sitting anywhere else, and the whole point of the column is that it is the one the preamble calls
# authoritative. §11's V-MET table has no phase cell at all; those rows parse to `None`.
CATALOG_ROW = re.compile(r"^\|\s*(V-[A-Z]{3}-\d{3})\s*\|(.*)$", re.M)
LEVEL_CELL = re.compile(r"^L\d(\s*,\s*L\d)*$")
PHASE_CELL = re.compile(r"^\d+$")

# A row of the 09 §10 ratchet table: `| **9** Broker, dark | V-BRK, V-REV, ... | notes |`.
RATCHET_ROW = re.compile(r"^\|\s*\*\*(\d+)\*\*[^|]*\|([^|]*)\|", re.M)

# An entry inside a ratchet cell: either a bare suite (`V-BRK`), a suite with a level qualifier
# (`V-GAT (L1)`), a single ID (`V-OBS-005`), or a slash run (`V-ISO-001/002/006`).
CHECK_ID = re.compile(r"V-[A-Z]{3}-\d{3}")
SUITE = re.compile(r"V-[A-Z]{3}")
QUALIFIER = re.compile(r"\(\s*(L\d)\s*\)")

ACCEPTANCE_HEADING = "## Acceptance → check binding"


class ParseError(Exception):
    """A source the derivation depends on did not yield what it must (property 1)."""


# --------------------------------------------------------------------------------------------
# Parsing. Every function takes text rather than a path so that `--negative-control` perturbs the
# same code the live path runs, instead of a synthesised stand-in of it ([[LSN-060]]).
# --------------------------------------------------------------------------------------------


class CatalogRow(NamedTuple):
    """One 09 §6 row: the levels it declares, and the phase by which it must be green."""

    levels: set[str]
    phase: int | None


def parse_catalog(spec_text: str) -> dict[str, CatalogRow]:
    """Every check ID in 09 §6, mapped to the levels and the due phase its row declares."""
    catalog: dict[str, CatalogRow] = {}
    for check_id, rest in CATALOG_ROW.findall(spec_text):
        cells = [c.strip() for c in rest.split("|")]
        levels = next((c for c in cells if LEVEL_CELL.match(c)), "")
        # The last non-empty cell, and only if it is a bare integer. `cells` ends with the empty
        # string a trailing `|` leaves behind.
        tail = [c for c in cells if c]
        due = int(tail[-1]) if tail and PHASE_CELL.match(tail[-1]) else None
        catalog[check_id] = CatalogRow(set(re.findall(r"L\d", levels)), due)
    if not catalog:
        raise ParseError("the 09 §6 catalog parsed to 0 check IDs -- the row shape has changed")
    return catalog


class Ratchet(NamedTuple):
    """What 09 §10 requires at a phase -- and what expanding it against 09 §6 set aside.

    `deferred` and `undated` exist so the phase filter is reportable. A filter that removes IDs and
    says nothing is a silent cap, and a silent cap on a required set reads exactly like a smaller
    spec.
    """

    required: set[str]
    deferred: set[str]  # suite members 09 §6 dates to a phase after this one
    undated: set[str]  # suite members whose 09 §6 row carries no phase cell at all
    explicit: set[str]  # IDs 09 §10 names outright, which the phase filter never touches

    def notes(self) -> list[str]:
        out = []
        if self.deferred:
            out.append(
                f"{len(self.deferred)} suite members are NOT required here -- 09 §6 dates them to a "
                f"later phase: " + " ".join(sorted(self.deferred))
            )
        if self.undated:
            out.append(
                f"{len(self.undated)} suite members carry no 09 §6 phase cell and are required at "
                f"every phase naming their suite: " + " ".join(sorted(self.undated))
            )
        return out


def parse_ratchet(spec_text: str, phase: int, catalog: dict[str, CatalogRow]) -> Ratchet:
    """09 §10's 'Newly required' cells for every phase <= this one, expanded against the catalog."""
    rows = [(int(p), cell) for p, cell in RATCHET_ROW.findall(spec_text)]
    if not any(p == phase for p, _ in rows):
        raise ParseError(f"09 §10 has no ratchet row for phase {phase}")

    required: set[str] = set()
    explicit_ids: set[str] = set()
    deferred_to_later: set[str] = set()
    undated: set[str] = set()

    # "Once a suite enters the ratchet it never leaves" (09 §10). Every row up to and including this
    # phase, not only this phase's own.
    for row_phase, cell in sorted(rows):
        if row_phase > phase:
            continue
        # Entries are comma-separated, but a slash run (`V-ISO-001/002/006`) is one entry and a level
        # qualifier binds to the entry it follows.
        for entry in cell.split(","):
            entry = entry.strip().strip("*").strip()
            if not entry:
                continue
            qual = QUALIFIER.search(entry)
            level = qual.group(1) if qual else None

            explicit = CHECK_ID.findall(entry)
            if explicit:
                # An ID §10 names OUTRIGHT is required at that row's phase, whatever the catalog
                # says. The explicit form is how §10 pulls one member of a suite forward or holds
                # one back, so filtering it by the column it exists to override would make the
                # explicit form unable to mean anything.
                explicit_ids.update(explicit)
                # `V-ISO-001/002/006`: the trailing numbers carry the suite of the first ID.
                head = explicit[0]
                for tail in re.findall(r"/(\d{3})", entry):
                    explicit_ids.add(f"{head[:5]}-{tail}")
                continue

            suite_match = SUITE.search(entry)
            if not suite_match:
                continue  # prose like "core" -- carries no ID of its own
            suite = suite_match.group(0)
            members = {k for k in catalog if k.startswith(suite + "-")}
            if level:
                members = {k for k in members if level in catalog[k].levels}
            if not members:
                raise ParseError(
                    f"09 §10 phase {row_phase} names {entry!r}, which expands to 0 check IDs "
                    f"against the 09 §6 catalog -- a required set that matches nothing scores "
                    f"every unrun check as satisfied ([[LSN-048]])"
                )
            for member in members:
                due = catalog[member].phase
                if due is None:
                    undated.add(member)
                    required.add(member)
                elif due <= phase:
                    required.add(member)
                else:
                    deferred_to_later.add(member)

    # An ID some other row names explicitly outranks a phase filter that removed it elsewhere.
    required |= explicit_ids
    deferred_to_later -= required

    if not required:
        raise ParseError(f"09 §10 phases <= {phase} expanded to 0 check IDs")

    return Ratchet(required, deferred_to_later, undated, explicit_ids)


def parse_acceptance_table(phase_text: str, phase: int) -> set[str]:
    """The check IDs the phase file's own acceptance→check binding table names."""
    if ACCEPTANCE_HEADING not in phase_text:
        raise ParseError(
            f"docs/build/phase-{phase}.md has no {ACCEPTANCE_HEADING!r} section -- the phase's own "
            f"half of the required set cannot be read"
        )
    section = phase_text.split(ACCEPTANCE_HEADING, 1)[1]
    for stop in ("\n## ",):
        if stop in section:
            section = section.split(stop, 1)[0]
    named = set(CHECK_ID.findall(section))
    # `V-RUN-001…006` is an ellipsis run, written with U+2026 in the table. Expanding it matters:
    # five of the six IDs it stands for appear nowhere else in the file, and a required set that
    # silently drops them is the failure this whole check exists to prevent.
    for suite, lo, hi in re.findall(r"V-([A-Z]{3})-(\d{3})\s*(?:…|\.\.\.)\s*(\d{3})", section):
        named.update(f"V-{suite}-{n:03d}" for n in range(int(lo), int(hi) + 1))
    if not named:
        raise ParseError(f"the acceptance table in phase-{phase}.md named 0 check IDs")
    return named


def parse_results(results_text: str) -> dict[str, list[tuple[str, str]]]:
    """check_id -> [(normalised result, evidence_ref)]. Results are written `**pass**` in the CSV.

    THE CELL IS NOT THE KEY. A `check_id` cell routinely names several IDs -- one suite run proves
    several catalog rows and gets one row citing one evidence reference -- and 36 of the 160 rows in
    `verification/results.csv` are written that way. It is the file's dominant convention for a suite
    run, not an anomaly. Keying on the raw cell filed `V-ISO-001, V-ISO-002` under that literal
    string, where it matched neither ID, and this check reported both as never asserted: **38 not
    green / 17 BLOCKING-ALWAYS** against a true 28 / 12, with 10 IDs falsely accused. A check written
    to find unrun work that invents ten pieces of it is worse than no check, because the ten are
    indistinguishable from the twenty-eight that are real.

    Splitting on the ID pattern also strips the two suffixes the file carries. `¬` is 09 §6's
    "negative control mandatory" marker copied off the catalog row -- a property of the CHECK, not a
    statement that the row records only a control run -- so `V-CTR-002 ¬` is a row about V-CTR-002.
    `(regression)` likewise. A cell naming no ID at all (`(L0 mechanization)`) contributes nothing,
    which is what it did before under a key nothing could ever look up.
    """
    rows: dict[str, list[tuple[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(results_text)):
        result = (row.get("result") or "").strip().strip("*").lower()
        evidence = (row.get("evidence_ref") or "").strip()
        # dict.fromkeys, not set(): a cell repeating an ID must not multiply its row.
        for check_id in dict.fromkeys(CHECK_ID.findall(row.get("check_id") or "")):
            rows.setdefault(check_id, []).append((result, evidence))
    return rows


# --------------------------------------------------------------------------------------------
# The verdict.
# --------------------------------------------------------------------------------------------


def is_green(rows: list[tuple[str, str]]) -> bool:
    """09 §9.4 -- a `pass` with no `evidence_ref` is recorded as `skipped`, so it is not green."""
    return any(result == "pass" and evidence for result, evidence in rows)


def scan_text(spec_text: str, phase_text: str, results_text: str, phase: int) -> tuple[list[str], dict]:
    """Returns (failures, report). Raises ParseError for a property-1 failure."""
    catalog = parse_catalog(spec_text)
    ratchet = parse_ratchet(spec_text, phase, catalog)
    table = parse_acceptance_table(phase_text, phase)
    results = parse_results(results_text)

    required = sorted(ratchet.required | table)
    green = [c for c in required if is_green(results.get(c, []))]
    not_green = [c for c in required if c not in set(green)]
    undeferrable = [c for c in not_green if c[:5] in BLOCKING_ALWAYS]
    # Property 4: 09 §10 requires it and the phase file's ACCEPTANCE TABLE does not name it. The
    # table, not the whole file -- this line first read `CHECK_ID.findall(phase_text)`, and the
    # control caught it the moment T11a's own write-up mentioned the four missing IDs in prose while
    # describing the gap. Naming an ID in a paragraph about how it is unasserted is not binding it to
    # an acceptance bullet; counting it would be [[LSN-019]] inside the check written to end
    # [[LSN-019]]'s last recurrence.
    unnamed_by_phase = sorted(ratchet.required - table)

    failures: list[str] = []
    if not_green:
        failures.append(
            f"property 2: {len(not_green)} of {len(required)} required checks have no `pass` row "
            f"with an evidence_ref in verification/results.csv (09 §9.4)"
        )
    if undeferrable:
        failures.append(
            f"property 3: {len(undeferrable)} of them are BLOCKING-ALWAYS and may not be deferred "
            f"to close the phase (09 §9.6)"
        )
    if unnamed_by_phase:
        failures.append(
            f"property 4: 09 §10 requires {len(unnamed_by_phase)} check IDs that "
            f"docs/build/phase-{phase}.md's acceptance table never names -- planning defect 4's "
            f"exact shape, and the phase file is where it is cheapest to fix"
        )

    report = {
        "required": required,
        "green": green,
        "not_green": not_green,
        "undeferrable": undeferrable,
        "unnamed_by_phase": unnamed_by_phase,
        "ratchet": sorted(ratchet.required),
        "table": sorted(table),
        "ratchet_notes": ratchet.notes(),
    }
    return failures, report


def naming_hint(check_id: str) -> str:
    """UNWEIGHTED. Which files mention the ID, for a human triaging the list. Never a verdict.

    Two of the hits this returns for V-ISO-001 exist in order to DISCLAIM the check. That is why no
    property reads this function.
    """
    out = subprocess.run(
        ["git", "grep", "-l", check_id], cwd=REPO, capture_output=True, text=True
    ).stdout.split()
    skip = (
        "docs/design/",
        "docs/build/",
        "verification/traceability",
        "verification/results.csv",
        ".claude/harness/LESSONS",
    )
    hits = [f for f in out if not f.startswith(skip)]
    return ", ".join(hits[:3]) + (f" (+{len(hits) - 3})" if len(hits) > 3 else "") if hits else "—"


def report(phase: int, with_hints: bool) -> int:
    spec_text = SPEC.read_text()
    phase_path = REPO / "docs" / "build" / f"phase-{phase}.md"
    if not phase_path.exists():
        print(f"FAIL: {phase_path.relative_to(REPO)} does not exist", file=sys.stderr)
        return 1
    try:
        failures, report = scan_text(spec_text, phase_path.read_text(), RESULTS.read_text(), phase)
    except ParseError as exc:
        print(f"FAIL: phase {phase} ratchet -- property 1: {exc}", file=sys.stderr)
        return 1

    n = len(report["required"])
    # PRINTED on both verdicts, and printed before them. The phase filter is the one part of the
    # derivation that makes the required set SMALLER, so it is the one part that can buy a green by
    # being wrong; a reader who cannot see what it removed cannot audit it. Counted and named, never
    # applied in silence.
    for note in report["ratchet_notes"]:
        print(f"  note: {note}", file=sys.stderr if failures else sys.stdout)

    if not failures:
        print(
            f"PASS: phase {phase} ratchet -- all {n} required checks are green "
            f"({len(report['ratchet'])} from 09 §10, {len(report['table'])} from the phase's "
            f"acceptance table), each with an evidence_ref"
        )
        return 0

    print(f"FAIL: phase {phase} ratchet is not asserted", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    print(
        f"\n  required {n} = {len(report['ratchet'])} (09 §10) ∪ {len(report['table'])} "
        f"(phase-{phase}.md acceptance table)\n"
        f"  green {len(report['green'])} · not green {len(report['not_green'])} · "
        f"of those BLOCKING-ALWAYS {len(report['undeferrable'])}",
        file=sys.stderr,
    )
    if report["not_green"]:
        print("\n  NOT GREEN (no `pass` row with an evidence_ref):", file=sys.stderr)
        for c in report["not_green"]:
            mark = "  ** BLOCKING-ALWAYS **" if c[:5] in BLOCKING_ALWAYS else ""
            hint = f"   hint: named by {naming_hint(c)}" if with_hints else ""
            print(f"    {c}{mark}{hint}", file=sys.stderr)
    if report["unnamed_by_phase"]:
        print(
            f"\n  REQUIRED BY 09 §10 AND ABSENT FROM phase-{phase}.md's ACCEPTANCE TABLE:\n    "
            + " ".join(report["unnamed_by_phase"]),
            file=sys.stderr,
        )
    if with_hints:
        print(
            "\n  The `hint` column is UNWEIGHTED and no property reads it. A file naming a check ID\n"
            "  may be disclaiming it: pair_netpol_test.go:35 names V-ISO-001/002 to say they are\n"
            "  L2 and belong elsewhere. Only a results row is evidence (09 §9.4).",
            file=sys.stderr,
        )
    return 1


# --------------------------------------------------------------------------------------------
# Negative control. Each case perturbs an INPUT and asserts this check notices, matched by a needle
# naming the property rather than by rc != 0 ([[LSN-035]]). The derivation itself is never
# synthesised -- the required set comes from the real 09 §10 and the real phase file in every case,
# which is the statement under test ([[LSN-060]]).
# --------------------------------------------------------------------------------------------


def _synthesise_green(required: list[str]) -> str:
    """A results.csv in which every required check is green -- the tree T11b-d will produce.

    A check split from its implementation has two trees to be green on ([[LSN-053]]). This one is RED
    on today's tree on purpose, so the tree that matters for its correctness is the FUTURE one: if
    the arm could not go green after the work lands, the cheapest diff at that moment would be to
    edit the arm.
    """
    return _write_rows([[c] for c in required])


def _synthesise_green_grouped(required: list[str], per_row: int = 4) -> str:
    """The same future tree, written the way `verification/results.csv` is ACTUALLY written.

    One row per suite run, several check IDs in the cell, one evidence reference for all of them --
    36 of the file's 160 rows. Every case above this one synthesises a row per ID, and that is
    precisely why the control was blind to the cell-is-the-key defect for the whole of T11a: the
    perturbations exercised a shape the real input does not predominantly have.
    """
    groups = [required[i : i + per_row] for i in range(0, len(required), per_row)]
    return _write_rows(groups)


def _write_rows(groups: list[list[str]]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "phase", "check_id", "level", "target", "result", "evidence_ref", "notes"])
    for ids in groups:
        w.writerow(
            [
                "2026-08-01",
                "9",
                ", ".join(ids),
                "L2",
                "gke-scratch-kube-agents-dev",
                "**pass**",
                "`synthetic`",
                "",
            ]
        )
    return out.getvalue()


def latest_phase() -> int:
    """The highest `docs/build/phase-<N>.md` carrying an acceptance table.

    The control is invoked with no arguments by `dev/tests/negative-controls-name-their-rule.py`,
    which is the LSN-035 scorer, so it needs a default -- and a hardcoded one goes stale the day
    Phase 10 opens, leaving the control auditing a closed phase forever while printing 8/8.
    """
    phases = []
    for path in (REPO / "docs" / "build").glob("phase-*.md"):
        m = re.fullmatch(r"phase-(\d+)\.md", path.name)
        if m and ACCEPTANCE_HEADING in path.read_text():
            phases.append(int(m.group(1)))
    if not phases:
        raise ParseError("no docs/build/phase-<N>.md carries an acceptance table")
    return max(phases)


def assert_hypotheticals_distinct(hypotheticals: tuple[tuple[str, set[str]], ...]) -> None:
    """The stageability guard's hypothetical acceptance tables must be DIFFERENT TABLES.

    This is what makes "derive them from 09, never from the live table" mechanically checkable
    rather than a comment. Derive either one from the document and there is a real acceptance table
    that collapses them onto each other -- the first draft's "the live table minus what §6 dates
    later" equals the complete table the moment the live table is complete -- and a guard auditing
    the same tree twice reports two greens while covering one.

    Lifted out of `stage()` so it can be exercised directly. A detector whose only test is "the
    thing it detects is absent today" is a detector that can be deleted with every gate green.
    """
    (_, first), (_, second) = hypotheticals
    if first == second:
        raise ParseError(
            "control: the two hypothetical acceptance tables are the same set, so the stageability "
            "guard audits one tree twice -- a hypothetical derived from the live table is not a "
            "hypothetical"
        )


def stage(phase: int) -> tuple[list[tuple], Ratchet, str]:
    """Build the perturbed inputs. Raises ParseError when a case cannot be staged -- see `pick`.

    Separate from `negative_control` only so the guard around it is one `try`. The loop that runs
    the cases stays there and calls `scan_text` directly: `negative-controls-name-their-rule.py`
    blinds a control by monkey-patching the function whose findings it inspects, and it reads which
    one that is off `negative_control`'s own bytecode. A control that delegates its loop is a control
    that cannot be blinded, and an unblindable control is exactly what [[LSN-035]] is about.
    """
    spec_text = SPEC.read_text()
    phase_text = (REPO / "docs" / "build" / f"phase-{phase}.md").read_text()
    results_text = RESULTS.read_text()

    catalog = parse_catalog(spec_text)
    ratchet = parse_ratchet(spec_text, phase, catalog)
    table = parse_acceptance_table(phase_text, phase)
    required = sorted(ratchet.required | table)

    # The future tree also has to satisfy property 4, which is about the phase file's acceptance
    # table and not about results at all. The IDs go INSIDE that section -- appending them to the end
    # of the file would satisfy a whole-file grep and not the property, which is the distinction
    # property 4 exists to draw.
    #
    # It REPLACES the section rather than prepending to it. Prepending leaves the real table's IDs in
    # the synthesised text, so `name_in_table(ids)` silently means "ids, plus whatever the live
    # document happens to say today" -- and every case built on it is then a claim about the tree the
    # repository is in rather than the tree the case describes. Three cases below pick a victim that
    # must be ABSENT from the staged table, and under prepending they could only ever pick from IDs
    # the live table omits: complete that table and the pools empty, `pick` refuses, and the control
    # stops being stageable at all the day the artifact it audits gets correct. That is what happened
    # on 2026-07-31 ([[LSN-053]] -- a check unit owes both trees, and this one owed the tree where
    # the acceptance table is complete).
    def name_in_table(ids: list[str]) -> str:
        head, rest = phase_text.split(ACCEPTANCE_HEADING, 1)
        tail = "\n## " + rest.split("\n## ", 1)[1] if "\n## " in rest else ""
        return head + ACCEPTANCE_HEADING + "\n\n| " + " | ".join(ids) + " |\n" + tail

    future = _synthesise_green(required)
    future_phase_text = name_in_table(required)

    victim_ba = next(c for c in required if c[:5] in BLOCKING_ALWAYS)
    victim_any = next(c for c in required if c[:5] not in BLOCKING_ALWAYS)

    # Both helpers locate their victim by "this row's cell NAMES the ID", not by "this row's cell IS
    # the ID", so the same perturbation lands on a single-ID row and on a grouped one. Matching on
    # equality would have silently no-opped against the grouped cases below -- an unperturbed input
    # scores as an escape, which reads as a hole in the check rather than a hole in the control.
    def _names(cell: str, check_id: str) -> bool:
        return check_id in CHECK_ID.findall(cell)

    def _rewrite(text: str, check_id: str, edit) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        for r in csv.reader(io.StringIO(text)):
            if len(r) > 6 and _names(r[2], check_id):
                r = edit(list(r))
            w.writerow(r)
        return out.getvalue()

    def demote(text: str, check_id: str, to: str) -> str:
        return _rewrite(text, check_id, lambda r: r[:5] + [to] + r[6:])

    def strip_evidence(text: str, check_id: str) -> str:
        return _rewrite(text, check_id, lambda r: r[:6] + [""] + r[7:])

    def drop_from_group(text: str, check_id: str) -> str:
        """Remove one ID from the grouped cell that names it, leaving the row otherwise green."""
        return _rewrite(
            text,
            check_id,
            lambda r: r[:2]
            + [", ".join(c for c in CHECK_ID.findall(r[2]) if c != check_id)]
            + r[3:],
        )

    # ---- perturbing 09 §6's Phase column, and 09 §10's row list -----------------------------
    # These five cases are the only ones that can tell a derivation which READS the Phase column from
    # one which ignores it, and the only ones that can tell "every row up to this phase" from "this
    # phase's row". Each asserts its edit LANDED against the parser rather than against a substring
    # count ([[LSN-049]]): a perturbation that silently no-ops leaves an unmutated input scoring as an
    # escape, which reads as a hole in the check instead of a hole in the control.

    def set_phase(text: str, check_id: str, new: int | None) -> str:
        """Rewrite one 09 §6 row's trailing phase cell. `new=None` removes the cell entirely."""
        row = re.compile(rf"^\|\s*{check_id}\s*\|.*$", re.M)
        cell = re.compile(r"\|\s*\d+\s*\|\s*$")
        out, n = row.subn(lambda m: cell.sub("|" if new is None else f"| {new} |", m.group(0)), text)
        if n != 1:
            raise ParseError(f"control: {check_id} matched {n} catalog rows, not 1")
        if parse_catalog(out)[check_id].phase != new:
            raise ParseError(f"control: rewriting {check_id}'s phase cell to {new} did not land")
        return out

    def only_this_phases_row(text: str) -> str:
        """Delete every 09 §10 ratchet row for a phase BEFORE this one, leaving this one's."""
        kept = [
            line
            for line in text.splitlines(keepends=True)
            if not (
                (m := RATCHET_ROW.match(line)) is not None and int(m.group(1)) < phase
            )
        ]
        out = "".join(kept)
        if len(out) >= len(text):
            raise ParseError(f"control: no 09 §10 ratchet row precedes phase {phase} to delete")
        return out

    def pick(pool, case: str) -> str:
        """One victim, or a loud failure naming the case that could not be staged.

        These three pools are read out of the derivation under test, deliberately -- synthesising
        them would make the cases measure a stand-in ([[LSN-060]]). The cost is that a defect in the
        derivation can empty a pool, and an empty pool must never quietly shrink the control to the
        cases it can still stage. Each case gets its OWN needle here so a sweep can still tell the
        defects apart ([[LSN-035]]).
        """
        for c in sorted(pool):
            return c
        raise ParseError(f"control: no victim for the {case} case -- it cannot be staged at all")

    # An ID this phase's own ratchet row does not name -- it is required only because a PRIOR row is.
    prior_only = ratchet.required - parse_ratchet(only_this_phases_row(spec_text), phase, catalog).required

    # 09 §6 does not index every ID it mentions: V-CMP-006 is named by this phase file's acceptance
    # table and has no catalog row at all, so every lookup here goes through `.get`.
    def phase_of(check_id: str) -> int | None:
        row = catalog.get(check_id)
        return None if row is None else row.phase

    def victim_pools(tbl: set[str]) -> dict[str, list[str]]:
        """The four victim pools, as a function of the acceptance table they are staged against.

        Defined ONCE and used twice -- to pick this run's victims from the real table, and to audit
        that the pools survive the tables this phase file is scheduled to become. An audit that
        re-states the pools is an audit a later edit drifts away from, and drift is how three of
        these acquired a `c not in table` term nobody noticed.

        That term was never about the property. It was compensating for `name_in_table` PREPENDING,
        which left the live table's IDs in the staged text and so could not stage a victim the live
        document happens to name. With the section replaced, the staged table is exactly the list
        handed in, and only `pull-forward` -- whose victim must not be required by anything yet --
        has any business reading `tbl` at all.
        """
        return {
            # A suite member 09 §6 dates AFTER this phase, that nothing else already requires.
            # Pulling it forward must make the future tree red; stripping its phase cell must too.
            "pull-forward": [c for c in ratchet.deferred if c not in ratchet.required | tbl],
            # A suite member 09 §6 dates at or before this phase, reachable ONLY through suite
            # expansion. Pushing it later must make a tree that omits it go green -- the arm that
            # proves the filter removes, rather than merely being consulted.
            "push-later": [
                c
                for c in ratchet.required
                if c not in ratchet.explicit and phase_of(c) is not None
            ],
            "prior-row": sorted(prior_only),
            # A BLOCKING-ALWAYS ID the DERIVATION requires. Property 4 counts what 09 §10 requires
            # and the table omits, so a victim drawn from the union `ratchet.required | table` is
            # not good enough: on an uncorrected phase-9.md the first such ID is V-BRK-001, which
            # the table names and the derivation does not, and omitting it from a staged table
            # moves property 4 by nothing at all.
            "under-naming": [c for c in sorted(ratchet.required) if c[:5] in BLOCKING_ALWAYS],
            # The sharper half of the same claim: an ID a PRIOR row's suite carries, which 09 §6
            # dates to THIS phase. Every accumulated row must be filtered against the phase under
            # test and not against its own -- filtering each row by its own number re-shrinks
            # exactly the set the accumulation widened, silently, by 11 IDs at phase 9, and
            # `prior-row` cannot see it because a phase-8 ID under a phase-8 row survives untouched.
            "carried-forward": [c for c in prior_only if phase_of(c) == phase],
        }

    # ---- the control must survive the phase file it audits getting CORRECT -------------------
    # Re-derive every pool against the acceptance tables this phase file could become and refuse to
    # stage if any of them empties. `complete` is the T11c‴ tree -- the table names every required
    # ID, which is the tree that took this control from 20/20 to unstageable on 2026-07-31 -- and
    # `partial` is any table naming a proper subset of the required set, which is the shape both
    # today's tree and T11c′'s have. A pool that survives both is a pool no acceptance-table edit
    # can empty. [[LSN-053]]: a check owes the tree the next unit builds.
    #
    # BOTH ARE DERIVED FROM 09, NOT FROM `table`. The first draft built the second one as
    # "the live table minus what §6 dates after this phase", which is the same borrow-the-artifact
    # defect this guard exists to catch, one level up: once T11c′ retargeted the real table there
    # was nothing left to subtract, the hypothetical collapsed onto the live table, and the guard
    # went from auditing two trees to auditing one. A hypothetical tree sampled off the real one is
    # not a hypothetical.
    ordered_required = sorted(required)
    hypotheticals = (
        ("complete", set(required)),
        ("partial", set(ordered_required[::2])),
    )
    # The two must be DIFFERENT TABLES, and saying so is what makes the document-independence above
    # mechanically checkable rather than a comment. Derive either one from `table` and there is a
    # real acceptance table that collapses them onto each other -- the first draft's "the live table
    # minus what §6 dates later" equals `complete` the moment the live table is complete -- and a
    # guard auditing the same tree twice reports two greens and has stopped covering one of them.
    assert_hypotheticals_distinct(hypotheticals)
    for name, tbl in hypotheticals:
        for case, pool in victim_pools(tbl).items():
            if not pool:
                raise ParseError(
                    f"control: the {case} pool is empty against a {name} acceptance table -- the "
                    f"control can only be staged while phase-{phase}.md stays wrong, which means "
                    f"it stops auditing the arm on the very tree the next unit builds"
                )

    pools = victim_pools(table)
    victim_later = pick(pools["pull-forward"], "pull-forward")
    victim_due = pick(pools["push-later"], "push-later")
    without_due = [c for c in required if c != victim_due]
    victim_prior = pick(pools["prior-row"], "prior-row")
    without_prior = [c for c in required if c != victim_prior]
    victim_carried = pick(pools["carried-forward"], "carried-forward")
    without_carried = [c for c in required if c != victim_carried]
    victim_unnamed = pick(pools["under-naming"], "under-naming")

    grouped = _synthesise_green_grouped(required)
    # Victims that SHARE a cell with other IDs -- the property the three grouped cases are about.
    # Picking `victim_ba` for them would work by accident and stop working the day the sort order
    # puts it alone in the last, short group.
    cells = [CHECK_ID.findall(r[2]) for r in list(csv.reader(io.StringIO(grouped)))[1:]]
    shared = [cell for cell in cells if len(cell) > 1]
    victim_shared = shared[0][0]
    victim_shared_ba = next((c for cell in shared for c in cell if c[:5] in BLOCKING_ALWAYS), victim_ba)

    cases = [
        (
            "09 §10 loses its row for this phase",
            spec_text.replace(f"| **{phase}** ", f"| **{phase}9** ", 1),
            phase_text,
            results_text,
            f"09 §10 has no ratchet row for phase {phase}",
        ),
        (
            "a suite named by the ratchet expands to nothing",
            spec_text.replace("| V-RUN-", "| V-RUM-"),
            phase_text,
            results_text,
            "expands to 0 check IDs",
        ),
        (
            "the phase file's acceptance heading is renamed",
            spec_text,
            phase_text.replace(ACCEPTANCE_HEADING, "## Acceptance notes", 1),
            results_text,
            "has no '## Acceptance → check binding' section",
        ),
        (
            "the 09 §6 catalog stops parsing",
            re.sub(r"^\| (V-[A-Z]{3}-\d{3}) \|", r"* \1:", spec_text, flags=re.M),
            phase_text,
            results_text,
            "the 09 §6 catalog parsed to 0 check IDs",
        ),
        (
            "THE FUTURE TREE — every required check green, phase file naming them all",
            spec_text,
            future_phase_text,
            future,
            None,  # this one must PASS
        ),
        (
            f"the future tree, with BLOCKING-ALWAYS {victim_ba} demoted to a finding",
            spec_text,
            future_phase_text,
            demote(future, victim_ba, "**finding**"),
            "BLOCKING-ALWAYS and may not be deferred",
        ),
        (
            f"the future tree, with {victim_any}'s evidence_ref emptied (09 §9.4)",
            spec_text,
            future_phase_text,
            strip_evidence(future, victim_any),
            "have no `pass` row with an evidence_ref",
        ),
        (
            f"the future tree, with the phase file under-naming its own ratchet by {victim_unnamed}",
            spec_text,
            # SYNTHESISED, not the live document. This case used to stage `phase_text` itself and
            # rely on it under-naming the ratchet -- true on 2026-07-31 by 43 IDs, and false the
            # moment the table is completed, at which point the case ESCAPES and reports a hole in
            # property 4 that is really the control describing a tree the repository has left.
            # A case about under-naming has to build the under-naming.
            name_in_table([c for c in required if c != victim_unnamed]),
            future,
            "never names",
        ),
        (
            "THE FUTURE TREE AS THE FILE ACTUALLY WRITES IT — one row per suite run, IDs grouped",
            spec_text,
            future_phase_text,
            grouped,
            None,  # this one must PASS
        ),
        (
            f"a grouped row that keeps {victim_shared}'s cellmates green and drops {victim_shared}",
            spec_text,
            future_phase_text,
            drop_from_group(grouped, victim_shared),
            "have no `pass` row with an evidence_ref",
        ),
        (
            f"a grouped row demoted to a finding, taking BLOCKING-ALWAYS {victim_shared_ba} with it",
            spec_text,
            future_phase_text,
            demote(grouped, victim_shared_ba, "**finding**"),
            "BLOCKING-ALWAYS and may not be deferred",
        ),
        (
            f"09 §6 pulls {victim_later} FORWARD to phase {phase} — the future tree must go red",
            set_phase(spec_text, victim_later, phase),
            future_phase_text,
            future,
            victim_later,
        ),
        (
            f"09 §6's phase cell for {victim_later} is deleted — undated means required, not exempt",
            set_phase(spec_text, victim_later, None),
            future_phase_text,
            future,
            victim_later,
        ),
        (
            f"a tree green for everything EXCEPT {victim_due}, which 09 §6 dates to phase "
            f"{catalog[victim_due].phase}",
            spec_text,
            name_in_table(without_due),
            _synthesise_green(without_due),
            victim_due,
        ),
        (
            f"…the same tree, with 09 §6 pushing {victim_due} out to phase 99 — must go GREEN",
            set_phase(spec_text, victim_due, 99),
            name_in_table(without_due),
            _synthesise_green(without_due),
            None,  # the filter must REMOVE, not merely be consulted
        ),
        (
            f"a tree green for this phase's own §10 row but not for {victim_prior}, which a PRIOR "
            f"row requires",
            spec_text,
            name_in_table(without_prior),
            _synthesise_green(without_prior),
            victim_prior,
        ),
        (
            f"…and not for {victim_carried}, carried by an EARLIER §10 row but dated by 09 §6 to "
            f"phase {phase} — every accumulated row filters against {phase}, not against itself",
            spec_text,
            name_in_table(without_carried),
            _synthesise_green(without_carried),
            victim_carried,
        ),
    ]
    return cases, ratchet, victim_later


def negative_control(phase: int | None = None) -> int:
    phase = latest_phase() if phase is None else phase
    try:
        cases, ratchet, victim_later = stage(phase)
    except ParseError as exc:
        print(f"FAIL: negative control could not be staged: {exc}", file=sys.stderr)
        return 1

    caught = 0
    for label, s_text, p_text, r_text, needle in cases:
        try:
            failures, rep = scan_text(s_text, p_text, r_text, phase)
            # The property-2/3/4 sentences carry counts, not IDs, so a case whose whole claim is
            # "THIS ID became required" cannot be matched against them -- and matching it on
            # "something went red" would score every perturbation as catching every property
            # ([[LSN-035]]). The named population `report()` prints is appended so those needles have
            # the one thing they are about to bind to.
            message = " | ".join(failures)
            if failures:
                message += " || not green: " + " ".join(rep["not_green"])
        except ParseError as exc:
            message = f"property 1: {exc}"
        if needle is None:
            ok = not message
            verdict = "PASS as required" if ok else f"UNEXPECTED FAIL: {message}"
        else:
            ok = needle in message
            verdict = "caught" if ok else f"ESCAPED (message was: {message or '<green>'})"
        caught += ok
        print(f"  [{'ok' if ok else 'XX'}] {label}: {verdict}")

    # The phase filter must also be REPORTABLE, and that cannot be asserted from `failures`: the
    # notes are printed on a green run too, which is the run where a silent cap does its damage.
    # Scored beside the cases because an unprinted filter is the same defect as a wrong one, arriving
    # without a symptom.
    unperturbed = scan_text(
        SPEC.read_text(),
        (REPO / "docs" / "build" / f"phase-{phase}.md").read_text(),
        RESULTS.read_text(),
        phase,
    )[1]
    notes = " ".join(unperturbed["ratchet_notes"])
    for what, want in (
        (f"names {victim_later}, which it removed", victim_later),
        ("counts what it removed", f"{len(ratchet.deferred)} suite members are NOT required here"),
        ("names the undated members it kept", f"{len(ratchet.undated)} suite members carry no"),
    ):
        ok = want in notes
        caught += ok
        print(f"  [{'ok' if ok else 'XX'}] the phase filter reports itself — it {what}")

    total = len(cases) + 3
    if caught != total:
        print(f"FAIL: negative control {caught}/{total}", file=sys.stderr)
        return 1
    print(f"PASS: negative control {caught}/{total} -- including the future tree, which must go green")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", type=int, required=True, help="roadmap phase number (07 §2)")
    ap.add_argument("--negative-control", action="store_true", help="break each property and confirm this check notices")
    ap.add_argument("--no-hints", action="store_true", help="omit the unweighted naming hints from the report")
    args = ap.parse_args(argv)
    if args.negative_control:
        return negative_control(args.phase)
    return report(args.phase, with_hints=not args.no_hints)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
