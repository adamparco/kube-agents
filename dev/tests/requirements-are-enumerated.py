#!/usr/bin/env python3
"""V-MET-009 (L0) — the uncovered requirement list is PUBLISHED, not merely counted.

09 §8 makes "comprehensive" a claim the harness can prove: *"Every normative requirement in 01-08
maps to at least one check ID."* That sentence needs a denominator, and until this file existed the
repo did not have one. `verification/traceability.yaml` is often mistaken for it and is not: that
file maps the 177 bullets of the six spec **Verification** sections, which is V-MET-011's property
and a much smaller, differently-shaped population. Nothing enumerated the normative statements
themselves, so "45% uncovered" (09 §8.1) was a number with no list behind it — exactly the failure
V-MET-009 exists to prevent.

This file is both the enumerator and the lint over its own output. That is deliberate: the property
worth having is *the committed enumeration equals what the specs say today*, and a generator that
lives somewhere else drifts from the checker that reads it.


WHAT COUNTS AS A NORMATIVE REQUIREMENT
--------------------------------------
09 §8 defines it: *"any statement using must / never / always / is rejected / is a defect / may
not, and every row of every mandated-behaviour table."* Mechanised as two rules:

1. **A sentence** — in a paragraph or a list item — carrying one of those keywords. Sentences are
   split after protecting the things a naive `.` split shreds: inline code, markdown links, `§4.4`
   cross-references, decimal numbers, and the usual abbreviations.

2. **Every data row of every mandated-behaviour table.** Header and separator rows are not
   requirements; the header is how a table *declares* its subject, not an obligation itself.

Rule 2 keeps §8's "mandated-behaviour" qualifier rather than quietly reading it as "every table".
Tables in **01** and **07** are excluded, and they are the only exclusions: 01's are an audience
map and a current-vs-intent delta, 07's are the phase task schedules and the standing-deferral
register. Those are planning artifacts — they describe what the project will do, not what the
system must do, and no check will ever "cover" a row of a delivery schedule. The exclusion is
published on the pass line and counted, because an escape hatch nobody can see is how a check
stops checking without ever going red (the lesson V-MET-001 paid for with its own line marker).
Sentences in 01 and 07 still count: 07 §5's "tests are replaced, never deleted" is a real
obligation and V-MET-003 is its mechanised form (09 §8.1 says so).

The six **Verification** sections (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7) are excluded
wholesale. They are declarations *about checks*, already enumerated bullet-by-bullet in
`verification/traceability.yaml` under V-MET-011. Enumerating them again here would put two ID
spaces over the same sentences and give V-MET-011's green a second thing to mean. The section list
is imported from `dev/tests/spec-ids.py` rather than restated, so there is one definition site.

Calibration against 09 §8.1: that table records **148** normative requirements for 02. Applying
both rules to 02, counting every table row including its Verification section, yields **147**. The
audit is reproducible for 02 and is *not* uniformly reproducible elsewhere — §8.1 records 78 for 05
where a mechanical count gives ~204, and says "96 (groups)" for 06 against ~374 rows-plus-sentences
— so §8.1 grouped some documents and enumerated others. That is why the baseline is recorded in
`verification/coverage.yaml` as *both* numbers: §8.1's published audit, verbatim and asserted
against the spec text, alongside this enumeration's own counts. A ratchet needs a baseline in the
same units as its measurement; pinning V-MET-008 to §8.1's numbers against a different denominator
would not be stricter or weaker, it would be incoherent.


REQUIREMENT IDS
---------------
`R-<doc>.<section>-<n>`, the scheme 09 §8 names, with `n` positional within the section. Positional
keys are the convention this repo already chose for `verification/traceability.yaml`, for the
reason stated there: they are stable under rewording — caught separately, by text — and unstable
under REORDERING, which is the edit that would silently re-point a mapping. So the committed file
carries each requirement's text next to its ID, and property 1 compares text, not counts. Insert a
sentence mid-section and every later row in that section reports as changed. That is loud on
purpose: renumbering a mapped requirement is not a formatting change.


WHAT THIS FILE DOES NOT DO
--------------------------
It does not decide whether a requirement is *covered*. `checks:` on each entry is curated — a check
declaring the requirement IDs it satisfies, per 09 §8 — and is populated by the unit that builds
V-MET-002 and V-MET-008. `--emit` MERGES: it rewrites `text:` from the specs and never clobbers a
curated `checks:` list. Today every list is empty, so the published uncovered list is the whole
enumeration. That overstates what is uncovered, and it is the safe direction to be wrong in: it
cannot manufacture a green.

Properties asserted, each a distinct finding prefix:

  1. enumeration is current   — a fresh extraction equals the committed id -> text exactly
  2. ids are well-formed      — `R-<doc>.<section>-<n>`, unique, contiguous 1..n within a section
  3. coverage is current      — coverage.yaml's totals agree with requirements.yaml
  4. uncovered is published   — an explicit ID sequence, not a count (V-MET-009 proper)
  5. uncovered is complete    — len(list) == enumerated - covered, and every ID resolves
  6. baseline matches 09 §8.1 — the recorded audit equals the spec's table, cell for cell

Usage:
    python3 dev/tests/requirements-are-enumerated.py                    # the check
    python3 dev/tests/requirements-are-enumerated.py --emit             # regenerate both artifacts
    python3 dev/tests/requirements-are-enumerated.py --negative-control # prove it is not vacuous
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESIGN = REPO / "docs" / "design"
SPEC_IDS = REPO / "dev" / "tests" / "spec-ids.py"
CONFORMANCE = DESIGN / "09-verification-and-validation.md"
REQUIREMENTS = Path("verification") / "requirements.yaml"
COVERAGE = Path("verification") / "coverage.yaml"

# Tables in these documents are planning artifacts, not mandated behaviour. See the module
# docstring; this is the whole exclusion list and its size is reported on the pass line.
NO_BEHAVIOUR_TABLES = {
    "01": "an audience map (§3) and a current-vs-intent delta (§6)",
    "07": "phase task schedules (§2), the current-state delta (§1), standing deferrals (§6)",
}

HEADING = re.compile(r"^#{2,6}\s+(\d+[a-z]?(?:\.\d+)*)\.?\s+(.*)$")
NORMATIVE = re.compile(
    r"\b(?:must|never|always|may not|is rejected|are rejected|is a defect)\b", re.I
)
ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|cf|vs|approx|Fig|Dr|Mr|Ms)\.", re.I)
TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$")
LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*)$")
REQ_ID = re.compile(r"^R-(0[1-8])\.(\d+[a-z]?(?:\.\d+)*)-(\d+)$")
# `. ` ends a sentence only before something that can start one.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*\[\"(])")
# Accepts both YAML quoting styles: prettier owns these files and rewrites a double-quoted scalar
# containing a `"` into a single-quoted one, so accepting only the emitted style would let
# `prettier --write` — which CI runs over the whole branch diff — turn a green check red.
SCALAR = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'"
# A row of the 09 §8.1 baseline table: `| 02 | 148 | 54 | 47 | 47 |`
BASELINE_ROW = re.compile(r"^\|\s*([0-9/ ]+?)\s*\|\s*([~\d()a-z ]+?)\s*\|\s*([~\d]+)\s*\|\s*([~\d]+)\s*\|\s*([~\d]+)\s*\|$")


class ArtifactError(Exception):
    """The committed YAML is not in the schema — a hard stop, never a reinterpretation."""


def _spec_ids():
    """Import `dev/tests/spec-ids.py` for its spec inventory and Verification-section map."""
    spec = importlib.util.spec_from_file_location("spec_ids", SPEC_IDS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scalar(raw: str) -> str:
    return json.loads(raw) if raw.startswith('"') else raw[1:-1].replace("''", "'")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# ------------------------------------------------------------------------------------ extraction
def _protect(text: str) -> tuple[str, list[str]]:
    """Hide the constructs a `.`-based sentence split would shred."""
    store: list[str] = []

    def keep(match: re.Match) -> str:
        store.append(match.group(0))
        return f"\x00{len(store) - 1}\x00"

    for pattern in (
        r"`[^`]*`",  # inline code: `spec.dryRun`
        r"\[[^\]]*\]\([^)]*\)",  # links: [06](06-api-and-data-contracts.md)
        r"§\s*\d+(?:\.\d+[a-z]?)*",  # cross-references: §2.2.1
        r"\b\d+\.\d+\b",  # decimals: 0.5
    ):
        text = re.sub(pattern, keep, text)
    return ABBREV.sub(keep, text), store


def sentences(text: str) -> list[str]:
    protected, store = _protect(text)
    restore = lambda s: re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], s)
    return [restore(part).strip() for part in SENTENCE_END.split(protected) if part.strip()]


def statement_units(lines: list[str], tables: bool) -> list[tuple[str, str]]:
    """(kind, text) for every candidate statement carrier in a section body.

    Fenced code is skipped entirely — a YAML example is not an obligation, and 06 alone has 31
    keyword-bearing lines inside fences. `tables` is False for the documents whose tables are
    planning artifacts.
    """
    out: list[tuple[str, str]] = []
    paragraph: list[str] = []
    fenced = False
    i = 0

    def flush() -> None:
        if paragraph:
            out.append(("paragraph", re.sub(r"\s+", " ", " ".join(paragraph)).strip()))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            fenced = not fenced
            i += 1
            continue
        if fenced or HEADING.match(line):
            flush()
            i += 1
            continue

        if stripped.startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            if not tables:
                continue
            # A well-formed table is header, separator, rows. Anything else: treat every
            # non-separator line as a row rather than silently dropping the block.
            rows = block[2:] if len(block) >= 2 and TABLE_SEP.match(block[1]) else block
            for row in rows:
                if not TABLE_SEP.match(row):
                    out.append(("table row", re.sub(r"\s+", " ", row)))
            continue

        if not stripped:
            flush()
            i += 1
            continue

        if match := LIST_ITEM.match(line):
            flush()
            buffer = [match.group(1)]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if (
                    nxt.strip()
                    and nxt.startswith((" ", "\t"))
                    and not LIST_ITEM.match(nxt)
                    and not nxt.strip().startswith(("|", "```"))
                ):
                    buffer.append(nxt.strip())
                    i += 1
                else:
                    break
            out.append(("list item", re.sub(r"\s+", " ", " ".join(buffer)).strip()))
            continue

        paragraph.append(stripped)
        i += 1

    flush()
    return out


def extract(read=None) -> dict[str, str]:
    """Requirement ID -> text, over 01-08. The denominator 09 §8 asks for."""
    mod = _spec_ids()
    if read is None:
        read = lambda name: (DESIGN / name).read_text(encoding="utf-8")

    out: dict[str, str] = {}
    for doc in sorted(mod.SPECS):
        lines = read(mod.SPECS[doc]).splitlines()
        verification = mod.VERIFICATION_SECTIONS.get(doc)
        tables = doc not in NO_BEHAVIOUR_TABLES

        heads = [(i, m.group(1)) for i, l in enumerate(lines) if (m := HEADING.match(l))]
        heads.append((len(lines), None))
        for (start, sec), (end, _) in zip(heads, heads[1:]):
            if sec is None:
                continue
            if verification and (sec == verification or sec.startswith(verification + ".")):
                continue
            n = 0
            for kind, text in statement_units(lines[start + 1 : end], tables):
                for candidate in [text] if kind == "table row" else sentences(text):
                    if kind == "table row" or NORMATIVE.search(candidate):
                        n += 1
                        out[f"R-{doc}.{sec}-{n}"] = candidate
    return out


# ------------------------------------------------------------------------------------- artifacts
def load_requirements(text: str) -> dict[str, dict]:
    """Strict parser for `verification/requirements.yaml`.

        "R-<doc>.<section>-<n>":
          text: "<the statement, whitespace-normalised>"
          checks:
            - V-XXX-000

    Hand-written rather than PyYAML because this runs in the L0 chain and L0 installs nothing.
    """
    entries: dict[str, dict] = {}
    key = None
    in_checks = False
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if m := re.fullmatch(rf"({SCALAR}):", line):
            key = _scalar(m.group(1))
            if key in entries:
                raise ArtifactError(f"{REQUIREMENTS}:{n}: duplicate entry {key!r}")
            entries[key] = {"text": None, "checks": [], "line": n}
            in_checks = False
        elif key is None:
            raise ArtifactError(f"{REQUIREMENTS}:{n}: content before the first entry: {line!r}")
        elif m := re.fullmatch(rf"  text: ({SCALAR})", line):
            entries[key]["text"] = _scalar(m.group(1))
            in_checks = False
        elif line in ("  checks:", "  checks: []"):
            in_checks = line == "  checks:"
        elif in_checks and (m := re.fullmatch(r"    - (V-[A-Z]{3}-\d{3})", line)):
            entries[key]["checks"].append(m.group(1))
        else:
            raise ArtifactError(f"{REQUIREMENTS}:{n}: not in the schema: {line!r}")
    for key, entry in entries.items():
        if entry["text"] is None:
            raise ArtifactError(f"{REQUIREMENTS}:{entry['line']}: {key} has no `text:`")
    return entries


def dump_requirements(entries: dict[str, dict]) -> str:
    head = f"""\
# verification/requirements.yaml -- the normative-requirement enumeration of docs/design/01-08.
#
# GENERATED, THEN CURATED. `text:` is extracted from the specs by
# `dev/tests/requirements-are-enumerated.py --emit` and must never be hand-edited: the lint
# re-extracts and compares, so an edit here reads as spec drift and fails the build. `checks:` is
# the opposite -- it is curated, it is how a check declares the requirement IDs it satisfies
# (09 section 8), and `--emit` merges rather than overwrites so regenerating never discards it.
#
# THIS IS NOT verification/traceability.yaml. That file maps the 177 bullets of the six spec
# Verification sections and belongs to V-MET-011. This one enumerates the normative statements
# themselves -- a different, larger population -- and is the denominator for V-MET-002, V-MET-008
# and V-MET-009. The two ID spaces are deliberately kept in separate files; merging them would
# give V-MET-011's green a second thing to mean.
#
# Key is `R-<doc>.<section>-<n>`, positional within the section. Positional keys are stable under
# rewording and unstable under REORDERING -- see the module docstring of the lint for why that is
# the trade this repo wants, and what to do when a section is renumbered.
#
# AN EMPTY `checks:` IS A FINDING, NOT A WAIVER. It means no check claims the requirement, and it
# is published in verification/coverage.yaml's `uncovered:` list on every run.
#
#
# HOW THE CURATION IS ARGUED
# --------------------------
# Grouped, on verification/traceability.yaml's precedent: the arguments live here, in one header,
# and no entry carries a per-entry rationale. A rationale beside every mapping is a second thing to
# keep in step with the mapping itself, and the entries are generated territory.
#
# SECTION CITATION IS NOT COVERAGE. A catalog row whose SRC column names the section is a candidate
# for its requirements, not a mapping for them: 09 section 6's SRC records where a check came from,
# and a section can hold obligations its own check never reaches. Every mapping below was decided by
# reading the requirement against the check's assertion text. Where a check carries only part of a
# conjunction, every check carrying part of the load is listed -- the normal case, as in
# traceability.yaml.
#
# A REQUIREMENT WITH NO HONEST CHECK IS LEFT UNMAPPED, and the gap is published by ID rather than
# closed with the nearest-looking catalog row. A mapping nobody established would make V-MET-002
# green over a requirement nothing asserts, which is the failure V-MET-014 names.
#
# The mapping is NOT restricted to the owning suite. V-MET-002's property is "maps to at least one
# check", any ID in the 09 section 6 catalog; coverage-ratchet.yaml's per-section suite ownership is
# a different question (which suites are load-bearing for a section) and is not a filter on this
# file. Several of 06's rows are covered by V-CMP-010 or V-MET-005, neither of which owns a 06
# section.
#
# Document 06, section by section. 06 section 1.1's budget-field table resolves as a unit under
# V-PRO-029 (one definition site per default and ceiling, an over-ceiling leaf clamping rather than
# winning). Section 1.2's V-1..V-10 rows are V-CTR-002, which requires a negative test per rule with
# the field path in the message. Section 4.2's code-floor rule rows take V-MET-005 and V-GAT-001
# together and neither alone: V-MET-005 guarantees the rule set and the corpus stay in step,
# V-GAT-001 asserts the corpus passes in full, and a rule row is covered only when both hold.
# Section 4.3's ten ActionRecord phases are V-CTR-006 (every legal transition succeeds, every
# illegal one is rejected) plus, per phase, the check that owns the phase's own behaviour. Section
# 4.4's nine fail-closed rules are V-CTR-015, which asserts them as one decision function and so
# covers exactly R-06.4.4-9..17. Schema field tables throughout take V-CMP-010, the field-level diff
# between 06's schema block and the generated OpenAPI/type.
#
# Documents 01-05, 07 and 08 -- the V-MET-002 worklist, section by section. This pass took the 135
# requirements coverage-ratchet.yaml lists as load-bearing-and-uncovered, not the whole document:
# those are the ones an unmapped entry fails V-MET-002 over. Grid and table sections resolve as a
# unit where a single check asserts the whole grid -- 02 section 7's 39 capability cells are
# V-CTN-021, which asserts every cell with its own expected outcome, and 03 section 3.2's three tier
# rows are the same grid seen per tier, so they carry V-CTN-021 plus the containment checks for the
# "never" column. 03 section 4.3 splits along its own argument: the VAP-affordable obligations take
# V-CTN-004/012/003/005/007/008/025, the cross-object ones V-CTN-013 and V-CTN-014, the journal
# table's `create`/`update`/`patch` row V-BRK-004 (rejected at admission) and its four detect-only
# rows V-BRK-003 (journal reconciliation), and the compromised-controller argument V-CTN-032, which
# runs exactly that adversary -- as the controller SA -- and asserts rules 2 and 3 still hold. 03
# section 9's summary table is one row per control family and is mapped to the family's checks
# rather than to a single row-level check. 05 section 1.2's four TTL rows and the reversibility
# horizon are V-REV-008, which asserts the class-based TTL and that a record is not GC'd before
# export confirms. 05 section 7's "the router is parked at zero replicas" is V-CMP-004, whose
# property is literally that parked-at-zero is not wired. 08 section 2.5's label table is V-RUN-004
# (stamped and selectable) plus, per row, the admission policy that consumes the label.
#
# 04 section 5.1 holds TWO tables and they take two different checks. The per-kind VERIFICATION
# PREDICATES are V-PRO-013. The per-kind SETTLE WINDOWS -- R-04.5.1-9..16, a published gap until
# 2026-07-31 -- are V-REV-012, an L0 doc-drift lint that reads the durations out of the table and
# out of the broker's window map and compares them cell for cell. They were left unmapped for as
# long as the only candidate was V-PRO-013, whose property is satisfied by waiting SOME window: the
# section states the numbers precisely so that they are falsifiable, and a check that never reads
# 5m/10m/90s/30s/15s/20m/2m cannot fail when a constant drifts from the table.
#
# Published gaps -- three requirements deliberately left unmapped, because no catalog row
# asserts them. One in 06:
#   R-06.2.3-6   "developer-team actor: none in v1". Nothing asserts the ABSENCE of a
#                developer-team actor GSA; every containment check asserts what a principal
#                cannot do, not that a principal does not exist.
# R-06.4.2-30 is NOT one of them, and was wrongly filed as one until 2026-07-31: it is a row of the
# same code-floor rule table as `secret-write` and `blast-radius-cap`, `RuleSecretMaterialEgress` is
# a member of `classify.AllFloorRuleIDs`, and the corpus carries nine cases for it -- so V-MET-005
# and V-GAT-001 reach it by exactly the argument that maps every one of its siblings, and singling
# it out was a curation error, not a stricter reading.
# And two elsewhere:
#   R-03.4.3-8   no write to an `Agent` CR whose identity is an ANCESTOR of the writer's.
#                V-CTN-007 covers the writer's OWN CR and V-CTN-025 the brake field on a child's;
#                nothing walks `parentRef` upward on a write.
#   R-03.4.3-9   actor writes stay inside the LIVE tier template. Both template checks
#                (V-CTN-012, V-CTR-004) are the inlined-literal form, which is the point of 03
#                section 4.2 -- the live-object form is a deferred capability with no check.
#
# Five of the eight closed on 2026-07-31, and every one of them closed as a CATALOG row over
# machinery that had been in the tree for units -- unmapped only because no check ID claimed it.
# That is the shape of most of what is left, and it is worth naming: an obligation with no `checks:`
# entry is not evidence that the property is unbuilt, and reading it that way is how a build
# re-implements what it already has.
#   R-07.5-4  -> V-CTN-038. "Authority never precedes machinery" is invariant 7 of
#               `dev/tests/invariants-gate.py`, on `dev/L0-CHAIN.txt` since P8. What it lacked was
#               a control. Eleven agent-RBAC documents in this tree DO carry a non-read verb, so
#               the green comes entirely from the other conjunct -- `missing_machinery()` has
#               returned `[]` since P8 closed the undo path -- and the enforcing branch has never
#               once executed against the real corpus. The row and the control landed together,
#               because a catalog ID over an unexercised branch is V-MET-014's failure wearing
#               someone else's name.
#   R-06.4.2-17 -> V-CTR-021. Asserted at three layers already (`ValidateDottedPath`,
#               `ValidateChangeRule`, and the webhook's per-index loop) plus the matcher's own
#               refusal, and the seven-mutant sweep is what shows no single layer is load-bearing
#               alone. The gap entry above read "no check asserts ChangePolicy dialect admission at
#               all", which was true of the CATALOG and false of the tree.
#   R-06.4.2-44 and R-06.4.2-45 -> V-GAT-024. The comparison method and the containment claim,
#               closed together because they are one property read from two ends. The formula half
#               recomputes `sha256(ns || 0x1f || v)` and `sha256(v)` over all three forms
#               independently of `digestForms`; the containment half is a closed allowlist over
#               `*DigestSet`'s exported surface, which is what turns "never journaled and never
#               logged" from a statement about today's call sites into one about the type system.
#   R-05.1.2-2 -> V-OBS-008. The journal side of the same property, and the one place the tree was
#               genuinely thin: `journal.Sanitize` WRITES the `sha256:` marker and
#               `undo.RedactedSecretKeys` REFUSES on it, across a package boundary neither side's
#               suite crossed. Each doc comment stated the contract from its own end and neither was
#               a test. A drift there does not fail -- it writes the hex of each value's own digest
#               into the live Secret and reports a completed undo.
#
# {len(entries)} requirements.

"""
    body = []
    for key in sorted(entries, key=_sort_key):
        entry = entries[key]
        body.append(f"{_quote(key)}:")
        body.append(f"  text: {_quote(entry['text'])}")
        if entry["checks"]:
            body.append("  checks:")
            body.extend(f"    - {c}" for c in entry["checks"])
        else:
            body.append("  checks: []")
    return head + "\n".join(body) + "\n"


def _sort_key(rid: str):
    m = REQ_ID.match(rid)
    if not m:
        return (rid, (), 0)
    doc, sec, n = m.groups()
    return (doc, tuple(int(p) if p.isdigit() else 0 for p in sec.split(".")), int(n))


def baseline_from_spec(conformance: str) -> list[dict[str, str]]:
    """The 09 §8.1 coverage-audit table, read out of the spec rather than restated."""
    body = conformance.split("### 8.1", 1)
    if len(body) == 1:
        raise ArtifactError("09 has no §8.1 section — cannot read the coverage baseline")
    rows: list[dict[str, str]] = []
    for line in body[1].split("\n## ", 1)[0].splitlines():
        line = line.strip()
        if not line.startswith("|") or TABLE_SEP.match(line):
            continue
        if m := BASELINE_ROW.match(line):
            document, total, full, partial, uncovered = (g.strip() for g in m.groups())
            if document.lower().startswith("document"):
                continue
            rows.append(
                {
                    "document": document,
                    "normative_requirements": total,
                    "fully_covered": full,
                    "partial": partial,
                    "uncovered": uncovered,
                }
            )
    if not rows:
        raise ArtifactError("09 §8.1's baseline table parsed to zero rows")
    return rows


def load_coverage(text: str) -> dict:
    """Strict parser for `verification/coverage.yaml`. Block style only — no flow mappings."""
    out: dict = {"totals": {}, "per_document": {}, "baseline_09_8_1": [], "uncovered": []}
    section = None
    doc = None
    row = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if m := re.fullmatch(r"(totals|per_document|baseline_09_8_1|uncovered):", line):
            section = m.group(1)
            doc = row = None
            continue
        if section is None:
            raise ArtifactError(f"{COVERAGE}:{n}: content before the first section: {line!r}")
        if section == "totals" and (m := re.fullmatch(r"  (\w+): (\d+)", line)):
            out["totals"][m.group(1)] = int(m.group(2))
        elif section == "per_document" and (m := re.fullmatch(rf"  ({SCALAR}):", line)):
            doc = _scalar(m.group(1))
            out["per_document"][doc] = {}
        elif section == "per_document" and doc and (m := re.fullmatch(r"    (\w+): (\d+)", line)):
            out["per_document"][doc][m.group(1)] = int(m.group(2))
        elif section == "baseline_09_8_1" and (m := re.fullmatch(rf"  - document: ({SCALAR})", line)):
            row = {"document": _scalar(m.group(1))}
            out["baseline_09_8_1"].append(row)
        elif section == "baseline_09_8_1" and row is not None and (
            m := re.fullmatch(rf"    (\w+): ({SCALAR})", line)
        ):
            row[m.group(1)] = _scalar(m.group(2))
        elif section == "uncovered" and (m := re.fullmatch(r"  - (R-[^\s]+)", line)):
            out["uncovered"].append(m.group(1))
        else:
            raise ArtifactError(f"{COVERAGE}:{n}: not in the schema: {line!r}")
    return out


def dump_coverage(entries: dict[str, dict], baseline: list[dict[str, str]]) -> str:
    per_doc: dict[str, dict[str, int]] = {}
    uncovered: list[str] = []
    for key in sorted(entries, key=_sort_key):
        doc = key[2:4]
        stats = per_doc.setdefault(doc, {"enumerated": 0, "covered": 0, "uncovered": 0})
        stats["enumerated"] += 1
        if entries[key]["checks"]:
            stats["covered"] += 1
        else:
            stats["uncovered"] += 1
            uncovered.append(key)

    total = len(entries)
    covered = sum(d["covered"] for d in per_doc.values())
    head = f"""\
# verification/coverage.yaml -- 09 section 8.1's coverage record, and the uncovered list itself.
#
# GENERATED by `dev/tests/requirements-are-enumerated.py --emit` from verification/requirements.yaml.
# Do not hand-edit: the lint recomputes and compares. To change a number here, map a requirement to
# a check there.
#
# `uncovered:` IS THE POINT. V-MET-009 requires the uncovered list to be published on every run and
# not merely counted, because a coverage percentage with no visible remainder is how this work
# stops silently. Every ID below is a normative statement in docs/design/01-08 that no check claims.
#
# TWO BASELINES, AND WHY. `baseline_09_8_1` is the audit published in the spec, asserted here cell
# for cell against 09 section 8.1 so it cannot drift. `totals` is this enumeration's own count. They
# are different populations and are NOT reconcilable into one number: section 8.1 records 148 for 02
# where these rules give 147 -- near-exact -- but 78 for 05 against ~204, and "96 (groups)" for 06
# against ~374, so that audit grouped some documents and enumerated others. V-MET-008's ratchet
# needs a baseline in the same units as its measurement, so it runs against `totals`; the published
# audit is kept beside it because the spec's number is the one a reader will look for.
#
# COVERED IS ZERO TODAY. The curated `checks:` mapping in requirements.yaml has not been populated
# yet -- that is the unit that builds V-MET-002 and V-MET-008. So every requirement below reads as
# uncovered, which overstates the gap. That is the safe direction: it cannot manufacture a green.

totals:
  enumerated: {total}
  covered: {covered}
  uncovered: {total - covered}

per_document:
"""
    body = []
    for doc in sorted(per_doc):
        stats = per_doc[doc]
        body.append(f'  "{doc}":')
        for field in ("enumerated", "covered", "uncovered"):
            body.append(f"    {field}: {stats[field]}")
    body.append("")
    body.append("baseline_09_8_1:")
    for row in baseline:
        body.append(f"  - document: {_quote(row['document'])}")
        for field in ("normative_requirements", "fully_covered", "partial", "uncovered"):
            body.append(f"    {field}: {_quote(row[field])}")
    body.append("")
    body.append("uncovered:")
    body.extend(f"  - {rid}" for rid in uncovered)
    return head + "\n".join(body) + "\n"


# ----------------------------------------------------------------------------------------- check
def check(requirements_text, coverage_text, conformance_text, extracted, stats=None) -> list[str]:
    """Six properties. Every return is a finding; an empty list is the pass."""
    findings: list[str] = []

    try:
        entries = load_requirements(requirements_text)
    except ArtifactError as exc:
        return [f"schema: {exc}"]
    try:
        coverage = load_coverage(coverage_text)
    except ArtifactError as exc:
        return [f"schema: {exc}"]

    # 1. the enumeration is current
    missing = sorted(set(extracted) - set(entries), key=_sort_key)
    extra = sorted(set(entries) - set(extracted), key=_sort_key)
    for rid in missing[:10]:
        findings.append(
            f"enumeration is stale: {rid} is a normative statement in the specs today and is not "
            f"in {REQUIREMENTS} — run `--emit`. ({extracted[rid][:70]!r})"
        )
    if len(missing) > 10:
        findings.append(f"enumeration is stale: and {len(missing) - 10} further unenumerated statements")
    for rid in extra[:10]:
        findings.append(
            f"enumeration is stale: {rid} is in {REQUIREMENTS} but the specs no longer yield it — "
            f"run `--emit`, and re-point any check that claimed it"
        )
    if len(extra) > 10:
        findings.append(f"enumeration is stale: and {len(extra) - 10} further vanished requirements")
    reworded = [
        rid for rid in sorted(set(entries) & set(extracted), key=_sort_key)
        if entries[rid]["text"] != extracted[rid]
    ]
    for rid in reworded[:10]:
        findings.append(
            f"enumeration is stale: {rid}'s text has changed in the spec — the recorded statement "
            f"is no longer the one the document makes, so any check mapped to it is mapped to "
            f"something that is gone. Recorded: {entries[rid]['text'][:60]!r}"
        )
    if len(reworded) > 10:
        findings.append(f"enumeration is stale: and {len(reworded) - 10} further reworded requirements")

    # 2. ids are well-formed and contiguous
    per_section: dict[tuple[str, str], list[int]] = {}
    for rid in entries:
        m = REQ_ID.match(rid)
        if not m:
            findings.append(f"ids are malformed: {rid!r} is not `R-<doc>.<section>-<n>`")
            continue
        per_section.setdefault((m.group(1), m.group(2)), []).append(int(m.group(3)))
    for (doc, sec), ordinals in sorted(per_section.items()):
        if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
            findings.append(
                f"ids are malformed: R-{doc}.{sec} numbers {sorted(ordinals)} — ordinals must run "
                f"1..n with no gaps, or a mapping points at a requirement that is not there"
            )

    # 3. coverage.yaml agrees with requirements.yaml
    covered = {rid for rid, e in entries.items() if e["checks"]}
    if coverage["totals"].get("enumerated") != len(entries):
        findings.append(
            f"coverage is stale: {COVERAGE} records {coverage['totals'].get('enumerated')} "
            f"enumerated, {REQUIREMENTS} holds {len(entries)} — run `--emit`"
        )
    if coverage["totals"].get("covered") != len(covered):
        findings.append(
            f"coverage is stale: {COVERAGE} records {coverage['totals'].get('covered')} covered, "
            f"{len(covered)} entries carry a check — run `--emit`"
        )

    # 4. the uncovered list is published, not counted  (V-MET-009 proper)
    declared = coverage["totals"].get("uncovered")
    published = coverage["uncovered"]
    if declared and not published:
        findings.append(
            f"uncovered is not published: {COVERAGE} counts {declared} uncovered requirements and "
            f"lists none of them. V-MET-009 requires the list itself — a percentage with no "
            f"visible remainder is how the audit stops silently (09 §8.1)"
        )

    # 5. the published list is complete and resolves
    expected = len(entries) - len(covered)
    if declared != expected:
        findings.append(
            f"uncovered is incomplete: {COVERAGE} declares {declared} uncovered against "
            f"{len(entries)} enumerated minus {len(covered)} covered = {expected}"
        )
    if published and len(published) != expected:
        findings.append(
            f"uncovered is incomplete: {len(published)} IDs published against {expected} uncovered "
            f"— a truncated list reads exactly like a complete one"
        )
    unknown = [rid for rid in published if rid not in entries]
    for rid in unknown[:5]:
        findings.append(f"uncovered is incomplete: published ID {rid} is not in {REQUIREMENTS}")
    wrongly = [rid for rid in published if rid in covered]
    for rid in wrongly[:5]:
        findings.append(f"uncovered is incomplete: {rid} is published as uncovered but claims a check")

    # 6. the recorded 09 §8.1 baseline matches the spec
    try:
        spec_baseline = baseline_from_spec(conformance_text)
    except ArtifactError as exc:
        findings.append(f"baseline is wrong: {exc}")
        spec_baseline = []
    if spec_baseline and coverage["baseline_09_8_1"] != spec_baseline:
        findings.append(
            f"baseline is wrong: {COVERAGE}'s `baseline_09_8_1` is not 09 §8.1's table. Recorded "
            f"{len(coverage['baseline_09_8_1'])} rows, the spec publishes {len(spec_baseline)}; "
            f"first divergence "
            f"{next((f'{a} != {b}' for a, b in zip(coverage['baseline_09_8_1'] + [None] * 9, spec_baseline) if a != b), 'in row count')}"
        )

    if stats is not None:
        stats.update(
            enumerated=len(entries),
            covered=len(covered),
            uncovered=expected,
            documents=len({k[2:4] for k in entries}),
            excluded=len(NO_BEHAVIOUR_TABLES),
            baseline_rows=len(spec_baseline),
        )
    return findings


def _inputs():
    return (
        (REPO / REQUIREMENTS).read_text(encoding="utf-8") if (REPO / REQUIREMENTS).is_file() else "",
        (REPO / COVERAGE).read_text(encoding="utf-8") if (REPO / COVERAGE).is_file() else "",
        CONFORMANCE.read_text(encoding="utf-8"),
        extract(),
    )


def run() -> int:
    for path in (REQUIREMENTS, COVERAGE):
        if not (REPO / path).is_file():
            print(f"FAIL: V-MET-009 (L0) -- {path} does not exist; run `--emit`")
            return 1
    stats: dict = {}
    findings = check(*_inputs(), stats=stats)
    if findings:
        print(f"FAIL: V-MET-009 (L0) -- {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    excluded = ", ".join(f"{d} ({why})" for d, why in sorted(NO_BEHAVIOUR_TABLES.items()))
    print(
        f"PASS: V-MET-009 (L0) -- {stats['enumerated']} normative requirements enumerated across "
        f"{stats['documents']} specs and current with the tree; {stats['covered']} covered, "
        f"{stats['uncovered']} uncovered and every one of them published by ID in {COVERAGE}, not "
        f"merely counted; 09 §8.1's {stats['baseline_rows']}-row baseline recorded and matching the "
        f"spec cell for cell; tables excluded as planning artifacts in {stats['excluded']} document(s): {excluded}"
    )
    return 0


def emit() -> int:
    extracted = extract()
    existing: dict[str, dict] = {}
    if (REPO / REQUIREMENTS).is_file():
        existing = load_requirements((REPO / REQUIREMENTS).read_text(encoding="utf-8"))
    entries = {
        rid: {"text": text, "checks": existing.get(rid, {}).get("checks", [])}
        for rid, text in extracted.items()
    }
    kept = sum(1 for e in entries.values() if e["checks"])
    dropped = sorted(set(existing) - set(entries), key=_sort_key)

    (REPO / REQUIREMENTS).write_text(dump_requirements(entries), encoding="utf-8")
    baseline = baseline_from_spec(CONFORMANCE.read_text(encoding="utf-8"))
    (REPO / COVERAGE).write_text(dump_coverage(entries, baseline), encoding="utf-8")

    print(f"wrote {REQUIREMENTS}: {len(entries)} requirements ({kept} carrying a curated check)")
    print(f"wrote {COVERAGE}: {len(entries) - kept} uncovered, published by ID")
    if dropped:
        print(f"NOTE: {len(dropped)} entries vanished from the specs: {', '.join(dropped[:8])}")
        for rid in dropped:
            if existing[rid]["checks"]:
                print(f"  !! {rid} carried checks {existing[rid]['checks']} — re-point them")
    return 0


# ---------------------------------------------------------------------------- negative control
def _mutate(args: tuple, index: int, fn) -> tuple:
    out = list(args)
    out[index] = fn(out[index])
    return tuple(out)


def negative_control() -> int:
    """Each mutation is a way this check could go quiet. Each names the signal it must produce."""
    base = _inputs()
    if check(*base):
        print("BROKEN: the tree is not clean, so the control cannot distinguish a caught mutation")
        return 1

    a_req = sorted(base[3], key=_sort_key)[0]
    mutations = [
        (
            "a normative statement in the spec is not enumerated",
            _mutate(base, 3, lambda e: {k: v for k, v in e.items() if k != a_req}),
            "is in verification/requirements.yaml but the specs no longer yield it",
        ),
        (
            "an enumerated requirement is deleted from the artifact",
            _mutate(base, 0, lambda t: re.sub(rf'"{re.escape(a_req)}":\n  text: .*\n  checks:.*\n', "", t)),
            "is a normative statement in the specs today and is not in",
        ),
        (
            "a requirement is silently reworded",
            _mutate(base, 0, lambda t: t.replace(f'"{a_req}":\n  text: "', f'"{a_req}":\n  text: "REWORDED ', 1)),
            "text has changed in the spec",
        ),
        (
            "the uncovered list is replaced by its count",
            _mutate(base, 1, lambda t: t.split("uncovered:\n  - ")[0] + "uncovered:\n"),
            "V-MET-009 requires the list itself",
        ),
        (
            "the uncovered list is truncated",
            _mutate(base, 1, lambda t: "\n".join(t.splitlines()[:-40]) + "\n"),
            "a truncated list reads exactly like a complete one",
        ),
        (
            "a requirement is marked covered without a check",
            _mutate(base, 1, lambda t: re.sub(r"^(  covered: )(\d+)$", lambda m: f"{m.group(1)}{int(m.group(2)) + 1}", t, count=1, flags=re.M)),
            "coverage is stale",
        ),
        (
            "the recorded 09 §8.1 baseline is edited away from the spec",
            _mutate(base, 1, lambda t: t.replace('normative_requirements: "148"', 'normative_requirements: "8"', 1)),
            "is not 09 §8.1's table",
        ),
        (
            "a section's ordinals are made non-contiguous",
            _mutate(base, 0, lambda t: t.replace(f'"{a_req}":', '"R-01.99-7":', 1)),
            "ordinals must run 1..n with no gaps",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        # A mutation that did not change its input cannot be evaluated. Without this arm the
        # unmutated tree is re-checked, comes back clean, and the row prints MISS -- which reads as
        # "the check let the defect through" when what happened is that the defect was never
        # applied. A no-op is how a mutation keyed to a literal from the tree ("covered: 0") dies
        # the day the tree moves past it.
        if args == base:
            failures += 1
            print(f"  BROKEN  {name}")
            print("           the mutation did not change its input; nothing was evaluated")
            continue
        findings = check(*args)
        hit = any(needle in f for f in findings)
        print(f"  {'caught ' if hit else 'MISS   '} {name}")
        if not hit:
            failures += 1
            print(f"           expected a finding containing {needle!r}; got {findings[:2] or 'none'}")
    print(
        f"{'PASS' if not failures else 'FAIL'}: V-MET-009 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--emit" in argv:
        return emit()
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
