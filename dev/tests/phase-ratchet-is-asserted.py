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
     09 §10's ratchet table row for the phase (expanding each suite name against the 09 §6 catalog,
     honouring a `(Lk)` qualifier) UNION the phase file's own acceptance table. Neither source may
     parse to nothing. A hand-written list in this file would be the same artifact that failed --
     one more place to forget -- and a parse that silently matches nothing scores every unrun check
     as satisfied ([[LSN-048]]).
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

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "design" / "09-verification-and-validation.md"
RESULTS = REPO / "verification" / "results.csv"

# 09 §5's gate-class table. A member of one of these suites may never be deferred (09 §9.6), so
# "required and not green" splits into two populations rather than one count.
BLOCKING_ALWAYS = ("V-CTN", "V-BRK", "V-REV", "V-ISO", "V-ADV", "V-MET")

# A row of the 09 §6 catalog: `| V-XXX-nnn | text | ... | L0, L2 | weight |`. The level cell is
# located by shape, not by column index, because the catalog's sub-tables do not all carry the same
# columns (some have a spec-reference cell, some do not).
CATALOG_ROW = re.compile(r"^\|\s*(V-[A-Z]{3}-\d{3})\s*\|(.*)$", re.M)
LEVEL_CELL = re.compile(r"^L\d(\s*,\s*L\d)*$")

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


def parse_catalog(spec_text: str) -> dict[str, set[str]]:
    """Every check ID in 09 §6, mapped to the levels its row declares."""
    catalog: dict[str, set[str]] = {}
    for check_id, rest in CATALOG_ROW.findall(spec_text):
        cells = [c.strip() for c in rest.split("|")]
        levels = next((c for c in cells if LEVEL_CELL.match(c)), "")
        catalog[check_id] = set(re.findall(r"L\d", levels))
    if not catalog:
        raise ParseError("the 09 §6 catalog parsed to 0 check IDs -- the row shape has changed")
    return catalog


def parse_ratchet(spec_text: str, phase: int, catalog: dict[str, set[str]]) -> set[str]:
    """The 09 §10 'Newly required' cell for a phase, expanded against the catalog."""
    cell = None
    for row_phase, newly_required in RATCHET_ROW.findall(spec_text):
        if int(row_phase) == phase:
            cell = newly_required
            break
    if cell is None:
        raise ParseError(f"09 §10 has no ratchet row for phase {phase}")

    required: set[str] = set()
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
            required.update(explicit)
            # `V-ISO-001/002/006`: the trailing numbers carry the suite of the first ID.
            head = explicit[0]
            for tail in re.findall(r"/(\d{3})", entry):
                required.add(f"{head[:5]}-{tail}")
            continue

        suite_match = SUITE.search(entry)
        if not suite_match:
            continue  # prose like "(read-side)" or "core" -- carries no ID of its own
        suite = suite_match.group(0)
        members = {k for k in catalog if k.startswith(suite + "-")}
        if level:
            members = {k for k in members if level in catalog[k]}
        if not members:
            raise ParseError(
                f"09 §10 phase {phase} names {entry!r}, which expands to 0 check IDs against the "
                f"09 §6 catalog -- a required set that matches nothing scores every unrun check as "
                f"satisfied ([[LSN-048]])"
            )
        required.update(members)

    if not required:
        raise ParseError(f"09 §10 phase {phase} expanded to 0 check IDs")
    return required


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

    required = sorted(ratchet | table)
    green = [c for c in required if is_green(results.get(c, []))]
    not_green = [c for c in required if c not in set(green)]
    undeferrable = [c for c in not_green if c[:5] in BLOCKING_ALWAYS]
    # Property 4: 09 §10 requires it and the phase file's ACCEPTANCE TABLE does not name it. The
    # table, not the whole file -- this line first read `CHECK_ID.findall(phase_text)`, and the
    # control caught it the moment T11a's own write-up mentioned the four missing IDs in prose while
    # describing the gap. Naming an ID in a paragraph about how it is unasserted is not binding it to
    # an acceptance bullet; counting it would be [[LSN-019]] inside the check written to end
    # [[LSN-019]]'s last recurrence.
    unnamed_by_phase = sorted(ratchet - table)

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
        "ratchet": sorted(ratchet),
        "table": sorted(table),
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


def negative_control(phase: int | None = None) -> int:
    phase = latest_phase() if phase is None else phase
    spec_text = SPEC.read_text()
    phase_text = (REPO / "docs" / "build" / f"phase-{phase}.md").read_text()
    results_text = RESULTS.read_text()

    catalog = parse_catalog(spec_text)
    required = sorted(parse_ratchet(spec_text, phase, catalog) | parse_acceptance_table(phase_text, phase))
    future = _synthesise_green(required)
    # The future tree also has to satisfy property 4, which is about the phase file's acceptance
    # table and not about results at all. The IDs go INSIDE that section -- appending them to the end
    # of the file would satisfy a whole-file grep and not the property, which is the distinction
    # property 4 exists to draw.
    future_phase_text = phase_text.replace(
        ACCEPTANCE_HEADING,
        ACCEPTANCE_HEADING + "\n\n| " + " | ".join(required) + " |\n",
        1,
    )

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
            "the future tree, with the phase file back to under-naming its own ratchet",
            spec_text,
            phase_text,
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
    ]

    caught = 0
    for label, s_text, p_text, r_text, needle in cases:
        try:
            failures, _ = scan_text(s_text, p_text, r_text, phase)
            message = " | ".join(failures)
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

    total = len(cases)
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
