#!/usr/bin/env python3
"""V-MET-008 -- the coverage ratchet, and the ownership derivation both coverage gates stand on.

09 section 8 splits requirement coverage into two tiers. **Full coverage, no exceptions, in the
load-bearing suites** (V-CTN, V-BRK, V-REV, V-ISO, V-ADV) is V-MET-002. **A ratchet everywhere
else** is this check: coverage may not fall below the recorded baseline, and *"a new normative
statement arrives with a check or a named deferral"*.

Both sentences begin with a word neither of them defines mechanically: **owned**. V-MET-002 is
scoped to the requirements *owned by* the load-bearing suites, and V-MET-008 to *the remaining
suites*. The two are complements of one partition, so whoever gets to draw it decides how much each
gate asks for -- and the direction of the incentive is not symmetric. Declaring a section unowned
shrinks V-MET-002's obligation, which is the BLOCKING-ALWAYS one, and grows V-MET-008's, which is
the lenient one. A partition asserted by hand would be a dial with a comfortable end.

So the partition is DERIVED, from 09 section 6's own `Source` column. Every catalog row already
names the spec section that owns its rationale -- that is what the column is for -- and a
requirement is owned by a suite when it lives in a section that suite's checks cite. Nothing new is
minted here; the ownership falls out of an index that has been reviewed for four phases, and it
moves when the catalog moves.

Three things about that derivation are worth stating, because each of them is a way it could have
been quietly wrong, and V-ISO managed to be two of them at once.

  1. THE V-ISO TABLE HAS NO `Source` COLUMN. Section 6.4's table is `| ID | Scenario | Lvl |
     Phase |`; the source is in the section's prose -- "CH1-CH9 as defined in 05 section 8". A
     derivation that reads only cells therefore gives a BLOCKING-ALWAYS suite ZERO owned sections,
     silently, and V-MET-002 gets easier by nine checks' worth. The fallback reads the prose
     between the subsection heading and its table -- and only for a table with no `Source` column,
     because a table that HAS the column and leaves a cell empty has an unstated source, which is a
     finding and must not be papered over by its preamble.

  2. OBLIGATIONS LIVE IN TWO ID SPACES, AND V-ISO's LIVE IN THE OTHER ONE. `05 section 8` is 05's
     Verification section, and `verification/requirements.yaml` excludes all six Verification
     sections on purpose: a spec's list of what to verify is indexed separately, as
     `verification/traceability.yaml`'s `<doc>§<sec>#<n>` bullets under V-MET-011, and enumerating
     it twice would give one obligation two IDs. So V-ISO's one derived section resolves perfectly
     and yields zero `R-` requirements. Section-level non-emptiness does not catch that -- one is
     not zero -- and the pass line would have read "V-ISO 1 section" while V-MET-002 asked a
     BLOCKING-ALWAYS suite for nothing at all. Both spaces are therefore read, a section is
     resolved in whichever space its obligations live in, and non-emptiness is asserted at BOTH
     granularities: **a load-bearing suite deriving to zero sections, or to zero obligations from
     the sections it did derive, is a hard failure**. V-ISO's eighteen `05§8#n` bullets are real,
     curated and already gated; what a single-space reading would have destroyed is this check's
     ability to say so, and its ability to tell that case apart from a suite that owns nothing
     anywhere.

  3. OWNERSHIP IS PREFIX-INCLUSIVE. `03 section 4` owns `03 section 4.1` and everything below it: a
     section includes its subsections, and the rationale a check cites at section level covers the
     refinements underneath. Today the two readings coincide exactly -- 420 either way -- and the
     pass line says so, so the day they diverge is visible.

THE ARRIVAL CLAUSE IS KEYED ON TEXT, NOT ON IDs. Requirement IDs are positional
(`R-<doc>.<section>-<n>`, n counting from the top of the section), so inserting one `must` at the
top of a section renumbers every statement below it. An arrival gate keyed on IDs would fire on all
of them and name the wrong sentence in every case. It is keyed instead on a content digest of the
statement, recorded in `baseline.digests`: reordering changes nothing, an insertion adds exactly one
digest, and a rewording swaps one for one. The rewording case reads as an arrival, which is the
honest reading -- a changed obligation is a new obligation -- and it has the same three exits the
spec gives any arrival: map it to a check, name a deferral, or re-baseline deliberately with
`--rebaseline`, which is a one-line diff a reviewer can see.

WHY `--rebaseline` IS NOT A BYPASS, AND WHERE IT STILL IS ONE. It rewrites the digest set, so it can
always be used to make an arrival disappear. That is the same bargain `dev/assertion-baseline.json`
strikes and it is defensible for the same reason: the escape is a committed diff, not a silent
runtime decision. What it may NOT do is lower the floor. `--rebaseline` refuses to write a
per-document `covered` count below the one already recorded, so re-baselining can retire an
obligation to *cover* a statement but never retire coverage already achieved. Property 5 asserts the
floor from the other side on every run.

WHAT THIS CHECK DOES NOT DO. It does not decide whether a requirement is *adequately* covered -- one
check named against a statement satisfies it, exactly as 09 section 8 says ("maps to at least one
check ID"). It does not populate the mapping; `verification/requirements.yaml`'s `checks:` is
curated, and the load-bearing draw-down is V-MET-002's unit. And it does not read
`coverage.yaml`'s `baseline_09_8_1` as its floor: that audit counted a different population -- 78
for 05 where these rules give ~204, "96 (groups)" for 06 against ~374 -- so it is asserted cell for
cell where it lives and is not used as a ratchet in units it was never measured in.

Run:  python3 dev/tests/coverage-ratchet-holds.py
      python3 dev/tests/coverage-ratchet-holds.py --emit         # regenerate, preserving baseline
      python3 dev/tests/coverage-ratchet-holds.py --rebaseline   # + re-cut the digest set
      python3 dev/tests/coverage-ratchet-holds.py --negative-control
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESIGN = REPO / "docs" / "design"
CONFORMANCE = DESIGN / "09-verification-and-validation.md"
ENUMERATOR = REPO / "dev" / "tests" / "requirements-are-enumerated.py"
RATCHET = Path("verification") / "coverage-ratchet.yaml"
REQUIREMENTS = Path("verification") / "requirements.yaml"
TRACEABILITY = Path("verification") / "traceability.yaml"

# 09 section 8.1 names these five as the tier that must reach zero uncovered. Sorted, because the
# order they appear in the spec's sentence is not a fact about them.
LOAD_BEARING = ("V-ADV", "V-BRK", "V-CTN", "V-ISO", "V-REV")

# Non-vacuity floors. Low enough never to need maintenance, high enough that a parser that stopped
# reading cannot report a clean run: zero arrivals against zero statements is the shape of every
# coverage gate that quietly stopped working.
MIN_STATEMENTS = 700
MIN_CATALOG_ROWS = 200

CATALOG_ROW = re.compile(r"^\|\s*(V-[A-Z]{3}-\d{3})\s*\|(.*)$")
TABLE_HEADER = re.compile(r"^\|\s*ID\s*\|(.*)$")
SECTION = re.compile(r"^##\s+(\d+)\.")
SUBSECTION = re.compile(r"^###\s+(6(?:\.\d+)*)\s+(.*)$")
# `03 §4.2`, `[05](05-system-architecture.md) §8`, and a bare `§4.3` that inherits the last document
# named to its left. `this doc §6` resolves to 09, which is not in 01-08 and so owns nothing.
SOURCE_REF = re.compile(
    r"\[(?P<linked>0[1-8])\]\([^)]*\)\s*§\s*(?P<lsec>\d+(?:\.\d+)*)"
    r"|(?P<doc>\b0[1-8])\s*§\s*(?P<dsec>\d+(?:\.\d+)*)"
    r"|§\s*(?P<bare>\d+(?:\.\d+)*)"
)
REQ_ID = re.compile(r"^R-(0[1-8])\.(\d+[a-z]?(?:\.\d+)*)-(\d+)$")
BULLET_ID = re.compile(r"^(0[1-8])§(\d+)#(\d+)$")
BULLET_KEY = re.compile(r"^\"((0[1-8])§(\d+)#\d+)\":\s*$")
SCALAR = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'"


class ArtifactError(Exception):
    """An input this derivation depends on did not yield what it must."""


def _enumerator():
    """`requirements-are-enumerated.py` by path -- its name is not an identifier."""
    spec = importlib.util.spec_from_file_location("requirements_are_enumerated", ENUMERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scalar(raw: str) -> str:
    if raw.startswith('"'):
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    return raw[1:-1].replace("''", "'")


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def digest(text: str) -> str:
    """A content digest of one normative statement.

    Whitespace-normalised first: the specs are prettier-wrapped, so a paragraph rewrap changes where
    the newlines fall without changing a word, and a digest that moved on a rewrap would report the
    whole document as newly arrived.
    """
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------------- derivation
def parse_sources(conformance: str) -> tuple[dict[str, set[tuple[str, str]]], dict[str, str]]:
    """Every 09 section 6 check ID -> the (document, section) pairs its `Source` resolves to.

    Returns the map and, beside it, the raw source text per check, so a finding can quote the cell
    it failed to resolve rather than asserting that it did not resolve.

    The `Source` column is located by NAME from each table's header row, not by index. Section 6.14
    and section 6.15 carry the same column in a different position, and section 6.4 does not carry
    it at all -- a fixed index would read a level or a phase into the ownership map and the result
    would be a section number that happens to look plausible.
    """
    sources: dict[str, set[tuple[str, str]]] = {}
    raw: dict[str, str] = {}
    columns: list[str] = []
    prose: list[str] = []
    section_fallback = ""
    in_catalog = False

    for line in conformance.splitlines():
        if m := SECTION.match(line):
            # Only section 6 is the catalog. Section 8's V-MET table and section 10's ratchet also
            # start rows with a check ID, and reading them here would overwrite a real Source with
            # whatever their columns happen to hold.
            in_catalog = m.group(1) == "6"
            columns, prose, section_fallback = [], [], ""
            continue
        if not in_catalog:
            continue
        if SUBSECTION.match(line):
            columns, prose, section_fallback = [], [], ""
            continue
        if m := TABLE_HEADER.match(line):
            columns = ["ID"] + [c.strip() for c in m.group(1).split("|")]
            # Everything read since the heading is this table's preamble. That is the ONLY place
            # section 6.4 states V-ISO's source, and it is consulted only when the table carries no
            # `Source` column at all -- a table that has the column and leaves a cell empty has an
            # unstated source, which is a finding and must not be papered over by its preamble.
            section_fallback = "" if "Source" in columns else " ".join(prose)
            continue
        if m := CATALOG_ROW.match(line):
            check_id = m.group(1)
            cells = ["", *(c.strip() for c in m.group(2).split("|"))]
            if "Source" in columns:
                idx = columns.index("Source")
                cell = cells[idx] if idx < len(cells) else ""
            else:
                cell = section_fallback
            raw[check_id] = cell
            sources.setdefault(check_id, set()).update(resolve(cell))
            continue
        if line.strip() and not line.startswith("|"):
            prose.append(line.strip())

    if len(sources) < MIN_CATALOG_ROWS:
        raise ArtifactError(
            f"09 section 6 parsed to {len(sources)} catalog rows, below the floor of "
            f"{MIN_CATALOG_ROWS}. The row or header shape changed and this derivation is reading a "
            f"fraction of the catalog; every ownership number below it would be too small."
        )
    return sources, raw


def resolve(cell: str) -> set[tuple[str, str]]:
    """`03 §3.3, §4.3` -> {(03, 3.3), (03, 4.3)}. A bare § inherits the last document named."""
    out: set[tuple[str, str]] = set()
    doc = None
    for m in SOURCE_REF.finditer(cell):
        if m.group("linked"):
            doc = m.group("linked")
            out.add((doc, m.group("lsec")))
        elif m.group("doc"):
            doc = m.group("doc")
            out.add((doc, m.group("dsec")))
        elif doc:
            out.add((doc, m.group("bare")))
    return out


def load_bullets(text: str) -> dict[str, list[str]]:
    """`verification/traceability.yaml`'s keys and their checks -- the SECOND obligation space.

    01-08's Verification sections (02 section 10, 03 section 11, 04 section 9, 05 section 8, 06
    section 10, 08 section 7) are deliberately excluded from `requirements.yaml`: a spec's list of
    what to verify is indexed by V-MET-011 in its own positional space, `<doc>§<sec>#<n>`, and
    enumerating it twice would give one obligation two IDs.

    This check has to read both, because a 09 section 6 `Source` may name a Verification section --
    and one load-bearing suite's source is nothing else. See `derive()`.
    """
    out: dict[str, list[str]] = {}
    key = None
    for line in text.splitlines():
        if m := BULLET_KEY.match(line):
            key = m.group(1)
            out[key] = []
        elif key and (m := re.fullmatch(r"    - (V-[A-Z]{3}-\d{3})", line)):
            out[key].append(m.group(1))
        elif key and re.fullmatch(r"  checks: \[\]", line):
            out[key] = []
    return out


def owns(owned: dict[tuple[str, str], set[str]], rid: str) -> set[str]:
    """The load-bearing checks that own a requirement, prefix-inclusive on the section number."""
    m = REQ_ID.match(rid) or BULLET_ID.match(rid)
    if not m:
        return set()
    doc, sec = m.group(1), m.group(2)
    hit: set[str] = set()
    for (odoc, osec), checks in owned.items():
        if odoc == doc and (sec == osec or sec.startswith(osec + ".")):
            hit |= checks
    return hit


def derive(conformance: str, extracted: dict[str, str], bullets: dict[str, list[str]]) -> dict:
    """The partition, and everything a finding needs to explain it.

    Obligations live in TWO spaces and ownership has to span both. `extracted` is
    `requirements.yaml`'s `R-<doc>.<sec>-<n>`; `bullets` is `traceability.yaml`'s
    `<doc>§<sec>#<n>`, which is where the six Verification sections' obligations went. A 09 section
    6 `Source` may name either kind of section, and treating a Verification source as owning
    nothing is not a rounding error: **V-ISO's only source is `05 §8`**, a Verification section, so
    a single-space reading gives a BLOCKING-ALWAYS suite zero obligations while every number on the
    pass line still looks plausible. Section-level non-emptiness does not catch it either -- V-ISO
    derives exactly one section, which is not zero. The suite's eighteen obligations are real, they
    are curated in `traceability.yaml`, and V-MET-011 already gates them; what would have been lost
    is this check's ability to *say* that, and its ability to tell that case apart from a suite
    that owns nothing anywhere.
    """
    sources, raw = parse_sources(conformance)

    owned: dict[tuple[str, str], set[str]] = {}
    unresolved: list[str] = []
    for check_id, refs in sorted(sources.items()):
        if check_id[:5] not in LOAD_BEARING:
            continue
        if not refs:
            unresolved.append(f"{check_id} (Source cell reads {raw[check_id]!r})")
            continue
        for ref in refs:
            owned.setdefault(ref, set()).add(check_id)

    per_suite: dict[str, set[tuple[str, str]]] = {s: set() for s in LOAD_BEARING}
    for ref, checks in owned.items():
        for check_id in checks:
            per_suite[check_id[:5]].add(ref)

    obligations = {**{rid: "requirements" for rid in extracted},
                   **{key: "traceability" for key in bullets}}
    load_bearing = {oid: sorted(owns(owned, oid)) for oid in obligations if owns(owned, oid)}
    exact = sum(
        1
        for oid in obligations
        if (m := (REQ_ID.match(oid) or BULLET_ID.match(oid))) and (m.group(1), m.group(2)) in owned
    )

    def yielded(doc: str, sec: str) -> list[str]:
        return [
            oid
            for oid in load_bearing
            if (m := (REQ_ID.match(oid) or BULLET_ID.match(oid)))
            and m.group(1) == doc
            and (m.group(2) == sec or m.group(2).startswith(sec + "."))
        ]

    sections = []
    for (doc, sec), checks in sorted(owned.items()):
        hit = yielded(doc, sec)
        space = obligations[hit[0]] if hit else "none"
        for suite in sorted({c[:5] for c in checks}):
            sections.append(
                {
                    "suite": suite,
                    "document": doc,
                    "section": sec,
                    "space": space,
                    "enumerated": len(hit),
                    "checks": " ".join(sorted(c for c in checks if c[:5] == suite)),
                }
            )

    return {
        "sources": sources,
        "owned": owned,
        "obligations": obligations,
        "per_suite": per_suite,
        "per_suite_obligations": {
            s: sorted(
                oid
                for oid in load_bearing
                if any(c[:5] == s for c in load_bearing[oid])
            )
            for s in LOAD_BEARING
        },
        "unresolved": unresolved,
        "load_bearing": load_bearing,
        "sections": sections,
        "exact": exact,
        "empty_sources": sorted(ref for ref in owned if not yielded(*ref)),
    }


# --------------------------------------------------------------------------------------- artifact
def load_ratchet(text: str) -> dict:
    """Strict parser for `verification/coverage-ratchet.yaml`. Block style only."""
    out: dict = {
        "ownership": {},
        "ownership_sections": [],
        "baseline": {},
        "floor": {},
        "deferrals": [],
        "digests": [],
        "load_bearing_uncovered": [],
    }
    section = None
    row: dict | None = None
    doc = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if m := re.fullmatch(
            r"(ownership|ownership_sections|baseline|floor|deferrals|digests"
            r"|load_bearing_uncovered):(\s*\[\])?",
            line,
        ):
            section = m.group(1)
            row = None
            doc = None
            continue
        if section is None:
            raise ArtifactError(f"{RATCHET}:{n}: content before the first section: {line!r}")
        if section in ("ownership", "baseline") and (m := re.fullmatch(r"  (\w+): (\d+)", line)):
            out[section][m.group(1)] = int(m.group(2))
        elif section == "baseline" and (m := re.fullmatch(rf"  (\w+): ({SCALAR})", line)):
            out[section][m.group(1)] = _scalar(m.group(2))
        elif section == "ownership_sections" and (
            m := re.fullmatch(rf"  - suite: ({SCALAR})", line)
        ):
            row = {"suite": _scalar(m.group(1))}
            out["ownership_sections"].append(row)
        elif section == "deferrals" and (m := re.fullmatch(rf"  - requirement: ({SCALAR})", line)):
            row = {"requirement": _scalar(m.group(1))}
            out["deferrals"].append(row)
        elif section in ("ownership_sections", "deferrals") and row is not None and (
            m := re.fullmatch(rf"    (\w+): ({SCALAR}|\d+)", line)
        ):
            value = m.group(2)
            row[m.group(1)] = int(value) if value.isdigit() else _scalar(value)
        elif section == "floor" and (m := re.fullmatch(rf"  ({SCALAR}):", line)):
            doc = _scalar(m.group(1))
            out["floor"][doc] = {}
        elif section == "floor" and doc and (m := re.fullmatch(r"    (\w+): (\d+)", line)):
            out["floor"][doc][m.group(1)] = int(m.group(2))
        elif section == "digests" and (m := re.fullmatch(r"  - ([0-9a-f]+)", line)):
            out["digests"].append(m.group(1))
        elif section == "load_bearing_uncovered" and (m := re.fullmatch(rf"  - ({SCALAR})", line)):
            out["load_bearing_uncovered"].append(_scalar(m.group(1)))
        else:
            raise ArtifactError(f"{RATCHET}:{n}: not in the schema: {line!r}")
    return out


def worklist(derived: dict, covered: dict[str, list[str]]) -> list[str]:
    """V-MET-002's remainder: every load-bearing-owned obligation that maps to no check yet."""
    return sorted(
        (oid for oid in derived["load_bearing"] if not covered.get(oid)), key=_sort_key
    )


def dump_ratchet(derived: dict, covered: dict[str, list[str]], baseline: dict) -> str:
    """Regenerate the artifact. `baseline` (recorded date, digests, floor, deferrals) is carried
    through untouched unless the caller re-cut it -- the same merge rule `requirements.yaml`'s
    curated `checks:` gets, and for the same reason: a generator that clobbers the curated half
    produces a file byte-identical to a correct first run."""
    load_bearing = derived["load_bearing"]
    elsewhere = len(derived["obligations"]) - len(load_bearing)
    lb_uncovered = worklist(derived, covered)

    head = f"""\
# verification/coverage-ratchet.yaml -- who owns which requirement, and the floor coverage may not
# fall below.
#
# GENERATED by `dev/tests/coverage-ratchet-holds.py --emit` (V-MET-008). The ownership half is
# DERIVED from 09 section 6's `Source` column and is recomputed and compared on every L0 run --
# hand-editing it only produces a finding. The `baseline` half is CARRIED THROUGH and re-cut only
# by an explicit `--rebaseline`.
#
# WHY OWNERSHIP IS DERIVED AND NOT DECLARED. 09 section 8's two coverage gates are complements of
# one partition: V-MET-002 demands full coverage of the requirements owned by V-CTN / V-BRK /
# V-REV / V-ISO / V-ADV, and V-MET-008 ratchets the rest. Declaring a section unowned would shrink
# the BLOCKING-ALWAYS obligation and grow the lenient one, so the partition is read out of the
# catalog rather than asserted here.
#
# `digests` IS THE ARRIVAL BASELINE, and it is keyed on statement TEXT rather than on requirement
# ID. IDs are positional, so one insertion renumbers everything below it in the section; a digest
# set is stable under reordering and moves by exactly one entry per real change. A statement whose
# digest is not in this list is NEW, and 09 section 8.1 requires a new normative statement to
# arrive with a check or a named deferral.
#
# `floor` MAY NOT BE LOWERED, including by `--rebaseline`, which refuses to write a smaller number
# than the one already here. Re-baselining can retire an obligation to cover a statement; it can
# never retire coverage already achieved.
#
# `load_bearing_uncovered` is V-MET-002's worklist -- the requirements that gate must see mapped
# before it can be green. It is published by ID for the same reason coverage.yaml publishes the
# uncovered list by ID: a count with no visible remainder is how this work stops silently.

ownership:
  suites: {len(LOAD_BEARING)}
  sections: {len(derived["owned"])}
  load_bearing: {len(load_bearing)}
  elsewhere: {elsewhere}

ownership_sections:
"""
    body: list[str] = []
    for row in derived["sections"]:
        body.append(f"  - suite: {_quote(row['suite'])}")
        body.append(f"    document: {_quote(row['document'])}")
        body.append(f"    section: {_quote(row['section'])}")
        body.append(f"    space: {_quote(row['space'])}")
        body.append(f"    enumerated: {row['enumerated']}")
        body.append(f"    checks: {_quote(row['checks'])}")

    body.append("")
    body.append("baseline:")
    body.append(f"  recorded: {_quote(baseline['recorded'])}")
    body.append(f"  statements: {len(baseline['digests'])}")

    body.append("")
    body.append("floor:")
    for doc in sorted(baseline["floor"]):
        body.append(f'  "{doc}":')
        body.append(f"    covered: {baseline['floor'][doc]['covered']}")

    body.append("")
    if baseline["deferrals"]:
        body.append("deferrals:")
        for row in baseline["deferrals"]:
            body.append(f"  - requirement: {_quote(row['requirement'])}")
            for field in ("blocker", "owner", "promotion"):
                body.append(f"    {field}: {_quote(row.get(field, ''))}")
    else:
        body.append("deferrals: []")

    body.append("")
    body.append("digests:")
    body.extend(f"  - {d}" for d in sorted(baseline["digests"]))

    body.append("")
    body.append("load_bearing_uncovered:")
    body.extend(f"  - {_quote(oid)}" for oid in lb_uncovered)
    return head + "\n".join(body) + "\n"


def _sort_key(oid: str):
    m = REQ_ID.match(oid) or BULLET_ID.match(oid)
    if not m:
        return ("", (), 0, oid)
    doc, sec, n = m.groups()
    return (doc, tuple(int(p) if p.isdigit() else 0 for p in sec.split(".")), int(n), oid)


# ------------------------------------------------------------------------------------------ check
def check(
    ratchet_text, requirements_text, traceability_text, conformance_text, extracted, stats=None
) -> list[str]:
    """Seven properties. Every return is a finding; an empty list is the pass."""
    findings: list[str] = []
    enumerator = _enumerator()

    try:
        artifact = load_ratchet(ratchet_text)
        entries = enumerator.load_requirements(requirements_text)
        bullets = load_bullets(traceability_text)
        derived = derive(conformance_text, extracted, bullets)
    except (ArtifactError, enumerator.ArtifactError) as exc:
        return [f"an input did not parse: {exc}"]

    # One map from obligation ID to the checks that claim it, across both spaces. The floor and the
    # arrival clause below stay scoped to `requirements.yaml` -- see their comments -- but the
    # worklist spans both, because V-MET-002's sentence is about requirements, not about files.
    covered: dict[str, list[str]] = {
        **{rid: entry["checks"] for rid, entry in entries.items()},
        **bullets,
    }

    # ---- property 0: non-vacuity. Every number below is a count of things that were read, and a
    # reader that stopped reading reports zero violations against zero inputs.
    if len(extracted) < MIN_STATEMENTS:
        findings.append(
            f"VACUOUS: the enumeration yielded {len(extracted)} normative statements, below the "
            f"floor of {MIN_STATEMENTS}. Zero arrivals against a corpus that failed to load is "
            f"indistinguishable from a clean run."
        )
    if len(artifact["digests"]) < MIN_STATEMENTS:
        findings.append(
            f"VACUOUS: the arrival baseline holds {len(artifact['digests'])} digests, below the "
            f"floor of {MIN_STATEMENTS}. An empty baseline makes every statement an arrival, and a "
            f"truncated one silently exempts whatever it dropped."
        )

    # ---- property 1: a load-bearing suite owns nothing. Asserted at BOTH granularities, because
    # they fail differently: V-ISO derives one section and would derive zero obligations from it
    # under a requirements.yaml-only reading, and one section is not zero sections.
    for suite in LOAD_BEARING:
        if not derived["per_suite"][suite]:
            findings.append(
                f"a load-bearing suite owns nothing: {suite} derived zero spec sections from 09 "
                f"section 6. Every requirement its checks are about has just moved out of "
                f"V-MET-002's obligation and into V-MET-008's, which is the lenient tier. Section "
                f"6.4 is the known case -- V-ISO's table has no `Source` column and its source is "
                f"in the subsection prose."
            )
        elif not derived["per_suite_obligations"][suite]:
            findings.append(
                f"a load-bearing suite owns nothing: {suite} derived "
                f"{len(derived['per_suite'][suite])} spec section(s) and zero obligations from "
                f"them. The sections resolved and then yielded nothing, which is the same false "
                f"green one level down -- V-MET-002 asks nothing of a BLOCKING-ALWAYS suite while "
                f"every count on the pass line still looks plausible. A Verification section's "
                f"obligations are traceability.yaml bullets, not requirements.yaml IDs; a source "
                f"naming one is read in that space."
            )
    for unresolved in derived["unresolved"]:
        findings.append(
            f"a load-bearing check's Source resolves to nothing: {unresolved}. 09 section 8 says a "
            f"check whose Source cell resolves to nothing has no stated rationale anywhere; here it "
            f"also owns no requirements, so it silently costs V-MET-002 whatever it was about."
        )

    # ---- property 2: the recorded ownership is current with the catalog.
    fields = ("suite", "document", "section", "space", "enumerated", "checks")
    recorded = [tuple(r.get(f) for f in fields) for r in artifact["ownership_sections"]]
    expected = [tuple(r.get(f) for f in fields) for r in derived["sections"]]
    if recorded != expected:
        only_recorded = [r for r in recorded if r not in expected]
        only_expected = [r for r in expected if r not in recorded]
        findings.append(
            f"ownership is stale: {len(only_recorded)} recorded row(s) the catalog no longer "
            f"yields and {len(only_expected)} the catalog yields and the artifact does not. "
            f"First divergence: recorded {only_recorded[:1] or 'none'} vs derived "
            f"{only_expected[:1] or 'none'}. Re-run with --emit."
        )

    # ---- property 3: the partition is exhaustive and its published half is complete.
    load_bearing = derived["load_bearing"]
    obligations = derived["obligations"]
    counts = {
        "suites": len(LOAD_BEARING),
        "sections": len(derived["owned"]),
        "load_bearing": len(load_bearing),
        "elsewhere": len(obligations) - len(load_bearing),
    }
    for field, value in counts.items():
        if artifact["ownership"].get(field) != value:
            findings.append(
                f"the partition is wrong: ownership.{field} records "
                f"{artifact['ownership'].get(field)}, the derivation gives {value}. "
                f"load_bearing + elsewhere must equal the {len(obligations)} obligations "
                f"enumerated across both spaces."
            )
    published = worklist(derived, covered)
    if artifact["load_bearing_uncovered"] != published:
        missing = [r for r in published if r not in set(artifact["load_bearing_uncovered"])]
        extra = [r for r in artifact["load_bearing_uncovered"] if r not in set(published)]
        findings.append(
            f"the partition is wrong: load_bearing_uncovered is not V-MET-002's worklist -- "
            f"{len(missing)} owned-and-uncovered requirement(s) are absent from it "
            f"({' '.join(missing[:4])}) and {len(extra)} listed are not "
            f"({' '.join(extra[:4])}). A truncated worklist reads exactly like a shorter one."
        )

    # ---- property 4: the arrival clause. 09 section 8.1, verbatim: a new normative statement
    # arrives with a check or a named deferral. Scoped to the `requirements.yaml` space on purpose:
    # a new Verification bullet is already an arrival gate, because V-MET-011 fails the build on any
    # bullet that resolves to no check. A second gate over the same 177 keys would not add a
    # property, only a second place to re-baseline.
    known = set(artifact["digests"])
    deferred = {row["requirement"] for row in artifact["deferrals"]}
    arrivals = [rid for rid, text in extracted.items() if digest(text) not in known]
    uncovered_arrivals = sorted(
        (rid for rid in arrivals if not covered.get(rid) and rid not in deferred),
        key=_sort_key,
    )
    for rid in uncovered_arrivals[:12]:
        findings.append(
            f"a new normative statement arrived uncovered: {rid} is not in the arrival baseline "
            f"recorded {artifact['baseline'].get('recorded', '?')}, and it names no check and no "
            f"deferral. 09 section 8.1: a new normative statement arrives with a check or a named "
            f"deferral. Text: {extracted[rid][:110]!r}"
        )
    if len(uncovered_arrivals) > 12:
        findings.append(
            f"a new normative statement arrived uncovered: and {len(uncovered_arrivals) - 12} "
            f"more, not listed. If a spec was reworded rather than extended, re-cut the baseline "
            f"deliberately with --rebaseline; the floor is asserted separately and cannot fall."
        )

    # ---- property 5: the floor. Per-document coverage may not fall below what was recorded. Over
    # `requirements.yaml` only, for the same reason as property 4: traceability.yaml's mapping is
    # V-MET-011's, asserted in both directions and already unable to fall.
    per_doc: dict[str, int] = {}
    for rid, entry in entries.items():
        doc = rid[2:4]
        per_doc[doc] = per_doc.get(doc, 0) + (1 if entry["checks"] else 0)
    for doc in sorted(artifact["floor"]):
        floor = artifact["floor"][doc]["covered"]
        now = per_doc.get(doc, 0)
        if now < floor:
            findings.append(
                f"the floor fell: document {doc} covers {now} requirement(s) and the recorded "
                f"floor is {floor}. Coverage may rise but never fall (09 section 8.1). A check "
                f"removed from a requirement's `checks:` list is a retirement and must name its "
                f"replacement first."
            )

    # ---- property 6: a deferral is a named deferral, in V-MET-006's shape.
    for row in artifact["deferrals"]:
        rid = row["requirement"]
        if rid not in extracted:
            findings.append(
                f"a deferral is unnamed: {rid} is deferred and is not a requirement the specs "
                f"yield today. A deferral outliving its statement is an exemption nobody can see."
            )
        for field in ("blocker", "owner", "promotion"):
            if not str(row.get(field, "")).strip():
                findings.append(
                    f"a deferral is unnamed: {rid} has no {field}. V-MET-006's shape is a named "
                    f"blocker, an owner and a promotion condition; a deferral missing one is a "
                    f"waiver wearing a deferral's name."
                )
        if covered.get(rid):
            findings.append(
                f"a deferral is unnamed: {rid} is deferred and also maps to "
                f"{' '.join(covered[rid])}. It is covered; delete the deferral rather "
                f"than leaving a standing exemption behind a satisfied obligation."
            )

    if stats is not None:
        orphans = sorted(known - {digest(t) for t in extracted.values()})
        stats.update(
            {
                "statements": len(extracted),
                "bullets": len(bullets),
                "load_bearing": len(load_bearing),
                "elsewhere": len(obligations) - len(load_bearing),
                "sections": len(derived["owned"]),
                "exact": derived["exact"],
                "empty_sources": derived["empty_sources"],
                "arrivals": len(arrivals),
                "deferrals": len(artifact["deferrals"]),
                "orphans": len(orphans),
                "recorded": artifact["baseline"].get("recorded", "?"),
                "floor": sum(d["covered"] for d in artifact["floor"].values()),
                "covered": sum(per_doc.values()),
                "worklist": len(published),
                "per_suite": {
                    s: (len(derived["per_suite"][s]), len(derived["per_suite_obligations"][s]))
                    for s in LOAD_BEARING
                },
            }
        )
    return findings


# ------------------------------------------------------------------------------------------ entry
def _inputs():
    enumerator = _enumerator()
    return (
        (REPO / RATCHET).read_text(encoding="utf-8"),
        (REPO / REQUIREMENTS).read_text(encoding="utf-8"),
        (REPO / TRACEABILITY).read_text(encoding="utf-8"),
        CONFORMANCE.read_text(encoding="utf-8"),
        enumerator.extract(),
    )


def run() -> int:
    stats: dict = {}
    findings = check(*_inputs(), stats=stats)
    if findings:
        print("FAIL: V-MET-008 (L0) -- the coverage ratchet does not hold", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1

    suites = ", ".join(
        f"{s} {stats['per_suite'][s][0]}/{stats['per_suite'][s][1]}" for s in LOAD_BEARING
    )
    print(
        f"PASS: V-MET-008 (L0) -- ownership derived from 09 section 6's Source column over "
        f"{stats['sections']} spec section(s), none of the five load-bearing suites owning zero "
        f"sections or zero obligations ({suites} sections/obligations); {stats['load_bearing']} of "
        f"{stats['statements'] + stats['bullets']} obligations "
        f"({stats['statements']} normative statements + {stats['bullets']} Verification bullets) "
        f"are owned by the load-bearing suites and {stats['elsewhere']} are elsewhere; "
        f"prefix-inclusive and exact section matching agree at {stats['exact']}; "
        f"{len(stats['empty_sources'])} cited section(s) yield nothing in either space; "
        f"{stats['arrivals']} statement(s) have arrived since the baseline recorded "
        f"{stats['recorded']} and {stats['deferrals']} deferral(s) are named; {stats['orphans']} "
        f"baseline digest(s) no longer match any statement; coverage {stats['covered']} against a "
        f"floor of {stats['floor']}; {stats['worklist']} owned obligation(s) published by ID as "
        f"V-MET-002's worklist"
    )
    return 0


def emit(rebaseline: str | None = None) -> int:
    enumerator = _enumerator()
    extracted = enumerator.extract()
    entries = enumerator.load_requirements((REPO / REQUIREMENTS).read_text(encoding="utf-8"))
    bullets = load_bullets((REPO / TRACEABILITY).read_text(encoding="utf-8"))
    covered = {**{rid: e["checks"] for rid, e in entries.items()}, **bullets}
    derived = derive(CONFORMANCE.read_text(encoding="utf-8"), extracted, bullets)

    path = REPO / RATCHET
    if path.exists():
        prior = load_ratchet(path.read_text(encoding="utf-8"))
        baseline = {
            "recorded": prior["baseline"].get("recorded", "unrecorded"),
            "digests": list(prior["digests"]),
            "floor": {d: dict(v) for d, v in prior["floor"].items()},
            "deferrals": list(prior["deferrals"]),
        }
    else:
        baseline = {"recorded": "unrecorded", "digests": [], "floor": {}, "deferrals": []}

    if rebaseline:
        baseline["recorded"] = rebaseline
        added = {digest(t) for t in extracted.values()} - set(baseline["digests"])
        dropped = set(baseline["digests"]) - {digest(t) for t in extracted.values()}
        baseline["digests"] = sorted({digest(t) for t in extracted.values()})
        per_doc: dict[str, int] = {}
        for rid, entry in entries.items():
            doc = rid[2:4]
            per_doc[doc] = per_doc.get(doc, 0) + (1 if entry["checks"] else 0)
        for doc in sorted(set(per_doc) | set(baseline["floor"])):
            was = baseline["floor"].get(doc, {}).get("covered", 0)
            now = per_doc.get(doc, 0)
            if now < was:
                print(
                    f"--rebaseline refuses to lower the floor: document {doc} is recorded at {was} "
                    f"covered and the tree gives {now}. The floor may rise; it may never fall.",
                    file=sys.stderr,
                )
                return 1
            baseline["floor"][doc] = {"covered": now}
        print(f"rebaselined: +{len(added)} digest(s), -{len(dropped)}")

    path.write_text(dump_ratchet(derived, covered, baseline), encoding="utf-8")
    print(f"wrote {RATCHET}")
    return 0


def _mutate(args: tuple, index: int, fn) -> tuple:
    out = list(args)
    out[index] = fn(out[index])
    return tuple(out)


def negative_control() -> int:
    """Each mutation is a way this check could go quiet. Each names the signal it must produce.

    Two of the seven properties are unexercised by today's tree -- there are no arrivals and the
    floor is at zero, because the curated mapping is V-MET-002's unit and has not been written. A
    clause with nothing to bite on is exactly the shape [[LSN-035]] is about, so the mutations
    below manufacture the inputs the tree does not have: an arrival, a deferral missing its owner,
    and a floor raised above the coverage under it.
    """
    base = _inputs()
    if check(*base):
        print("BROKEN: the tree is not clean, so the control cannot distinguish a caught mutation")
        return 1

    arrived = _mutate(base, 4, lambda e: {**e, "R-01.1-99": "A new thing that must never happen."})
    mutations = [
        (
            "a normative statement arrives with no check and no deferral",
            arrived,
            "a new normative statement arrived uncovered",
        ),
        (
            "an arrival is deferred, and the deferral names no owner",
            _mutate(
                arrived,
                0,
                lambda t: t.replace(
                    "deferrals: []",
                    'deferrals:\n  - requirement: "R-01.1-99"\n'
                    '    blocker: "the broker does not exist yet"\n'
                    '    owner: ""\n    promotion: "phase 10"',
                    1,
                ),
            ),
            "has no owner",
        ),
        (
            "V-ISO's prose source is removed, so a BLOCKING-ALWAYS suite owns nothing",
            _mutate(base, 3, lambda t: t.replace(
                "CH1–CH9 as defined in [05](05-system-architecture.md) §8.", "CH1–CH9.", 1
            )),
            "a load-bearing suite owns nothing: V-ISO derived zero spec sections",
        ),
        (
            "V-ISO's sections resolve, and the space holding their obligations is not read",
            _mutate(base, 2, lambda t: re.sub(
                r"^\"05§8#\d+\":\n(?: +.*\n)*\n?", "", t, flags=re.M
            )),
            "V-ISO derived 1 spec section(s) and zero obligations",
        ),
        (
            "a load-bearing check's Source cell is blanked",
            _mutate(base, 3, lambda t: re.sub(
                r"^(\|\s*V-CTN-\d{3}\s*\|[^|]*\|)[^|]*\|", r"\1  |", t, count=1, flags=re.M
            )),
            "Source resolves to nothing",
        ),
        (
            "a recorded ownership row is deleted from the artifact",
            _mutate(base, 0, lambda t: t.replace(
                '  - suite: "V-ADV"\n    document: "03"\n    section: "1"\n', "", 1
            )),
            "ownership is stale",
        ),
        (
            "the load-bearing count is edited down",
            _mutate(base, 0, lambda t: re.sub(r"  load_bearing: \d+", "  load_bearing: 12", t, 1)),
            "the partition is wrong: ownership.load_bearing",
        ),
        (
            "V-MET-002's worklist is truncated",
            _mutate(base, 0, lambda t: "\n".join(t.splitlines()[:-30]) + "\n"),
            "is not V-MET-002's worklist",
        ),
        (
            "the floor is raised above the coverage under it",
            _mutate(base, 0, lambda t: t.replace('  "02":\n    covered: 0', '  "02":\n    covered: 9', 1)),
            "the floor fell: document 02",
        ),
        (
            "the enumerator returns almost nothing",
            _mutate(base, 4, lambda e: dict(sorted(e.items())[:5])),
            "VACUOUS: the enumeration yielded",
        ),
        (
            "the arrival baseline is emptied",
            _mutate(base, 0, lambda t: re.sub(r"digests:\n(  - [0-9a-f]{16}\n)+", "digests:\n", t)),
            "VACUOUS: the arrival baseline holds",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        findings = check(*args)
        hit = any(needle in f for f in findings)
        print(f"  {'caught ' if hit else 'MISS   '} {name}")
        if not hit:
            failures += 1
            print(f"           expected a finding containing {needle!r}; got {findings[:2] or 'none'}")
    print(
        f"{'PASS' if not failures else 'FAIL'}: V-MET-008 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--rebaseline" in argv:
        rest = argv[argv.index("--rebaseline") + 1 :]
        if not rest or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rest[0]):
            print(
                "--rebaseline takes the date it is being cut on: --rebaseline YYYY-MM-DD. It is "
                "recorded in the artifact so every later arrival can name what it arrived after.",
                file=sys.stderr,
            )
            return 2
        return emit(rebaseline=rest[0])
    if "--emit" in argv:
        return emit()
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
