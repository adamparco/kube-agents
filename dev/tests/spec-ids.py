#!/usr/bin/env python3
"""Spec-ID lint — V-MET-010, V-MET-011, V-MET-012, and V-MET-013 for check IDs (LSN-014).

[09](../../docs/design/09-verification-and-validation.md) §14 declares four lints on the
conformance spec itself and then does not have them. They were written as `L0` in a document
nobody could run, which is the same shape as the checks they police: an assertion that exists
and cannot fail. This file is all four.

  **V-MET-010** — every check ID cited by specs 01-08 is defined in 09, and every ID 09 defines
  is traceable to a spec section that exists.
  **V-MET-011** — every bullet of every Verification section in 01-08 resolves to at least one
  check ID, via the curated `verification/traceability.yaml`, and the file and the specs agree in
  BOTH directions.
  **V-MET-012** — 09 §5.1 lists every component ID from 05 §1, and 09 §5.2 lists every contract
  defined in 06.
  **V-MET-013**, for check IDs — an ID is defined in exactly one place. Two rows with the same
  ID are two checks with one name; the one a report cites is whichever the reader found first.

V-MET-011 was deferred here until Phase 8 T10 with the blocker "the traceability generator", on the
grounds that a lint over an absent artifact either passes vacuously or fails permanently. The
promotion condition was not a generator. It was a CURATED file: a mapping produced by text
similarity would assert coverage nobody established, which is V-MET-014 with extra steps. So
`traceability.yaml` is written by hand and this lint only polices it — it never fills it in.

What it found on its first run, which is why it is worth having:

  * 05 §1 defines `C-JR` (journal reconciler) and `C-AD` (anomaly detector) as **New (v1,
    load-bearing)**. Neither appeared anywhere in 09 §5.1. Two load-bearing components with no
    Exists / Wired / Exercised probe — not deferred, not optional, simply missing from the
    inventory that decides what "complete" means.
  * 06 defines contracts in §2a, §2b, §3.1, §5 and §6 that 09 §5.2 did not list.

Traceability, concretely: each §6 catalog row carries a `Source` cell like `03 §11` or
`03 §3.3, §4.3`. The lint resolves every one against the actual heading numbers of that document.
A renumbered section in 03 silently orphans the rationale for a containment check, and the check
keeps looking authoritative because the ID is still there.

Exit 0 = clean. Exit 1 = findings. Exit 2 = could not run (a spec file is missing).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESIGN = REPO / "docs" / "design"

SPECS = {
    "01": "01-vision-scope.md",
    "02": "02-agent-personas.md",
    "03": "03-security-model.md",
    "04": "04-workflow-model.md",
    "05": "05-system-architecture.md",
    "06": "06-api-and-data-contracts.md",
    "07": "07-implementation-roadmap.md",
    "08": "08-agent-runtime-and-identity.md",
}
CONFORMANCE = "09-verification-and-validation.md"

CHECK_ID = re.compile(r"\bV-[A-Z]{3}-\d{3}\b")
COMPONENT_ID = re.compile(r"\bC(?:-[A-Z]{2}|\d{1,2})\b")
# `03 §11`, `06 §2.3`, `05 §1.2`, and the bare continuation `§4.3` in `03 §3.3, §4.3`.
SOURCE_DOC = re.compile(r"\b(0[1-8])\s+§\s*([0-9][0-9a-z.]*)")
SOURCE_MORE = re.compile(r"§\s*([0-9][0-9a-z.]*)")
HEADING = re.compile(r"^#{2,6}\s+(\d+[a-z]?(?:\.\d+)*)\.?\s+(.*)$")
# A catalog row: `| V-CTN-001 | assertion | 03 §11 | L2, L3 | 8 |`
CATALOG_ROW = re.compile(r"^\|\s*(V-[A-Z]{3}-\d{3})\s*\|")
# A bullet definition: `- **V-CMP-010** — ...`
BULLET_DEF = re.compile(r"^\s*[-*]\s+\*\*(V-[A-Z]{3}-\d{3})\*\*")

# A 06 section is a contract if it says so in its own title. The author names contracts; the lint
# does not guess which prose block is one.
CONTRACT_TITLE = re.compile(r"contract|CRD", re.I)


def _read(name: str) -> str:
    path = DESIGN / name
    if not path.is_file():
        raise SystemExit(f"could not run: {path.relative_to(REPO)} is missing")
    return path.read_text(encoding="utf-8")


def headings(text: str) -> dict[str, str]:
    """Numbered heading -> title, e.g. {'4.3': '`ActionRecord`'}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def section(text: str, start: str, end: str) -> str:
    """The text between two headings. Raises if either is missing — no silent empty slice."""
    if start not in text or end not in text:
        raise SystemExit(f"could not run: {CONFORMANCE} has no {start!r}..{end!r} section")
    return text.split(start, 1)[1].split(end, 1)[0]


# --------------------------------------------------------------------------------------------
def definition_sites(conf: str) -> dict[str, list[int]]:
    """Check ID -> line numbers where it is DEFINED (a catalog row or a `- **ID**` bullet)."""
    sites: dict[str, list[int]] = {}
    for n, line in enumerate(conf.splitlines(), 1):
        m = CATALOG_ROW.match(line) or BULLET_DEF.match(line)
        if m:
            sites.setdefault(m.group(1), []).append(n)
    return sites


def check_ids_defined_once(conf: str) -> list[str]:
    """V-MET-013 for check IDs."""
    return [
        f"09 §6: `{cid}` is defined {len(lines)} times (lines {', '.join(map(str, lines))}) — "
        f"two checks with one name"
        for cid, lines in sorted(definition_sites(conf).items())
        if len(lines) > 1
    ]


def check_spec_citations_resolve(conf: str, defined: set[str]) -> list[str]:
    """V-MET-010, forward: a spec citing a check ID that 09 does not define."""
    findings = []
    for num, name in SPECS.items():
        text = _read(name)
        for n, line in enumerate(text.splitlines(), 1):
            for cid in sorted(set(CHECK_ID.findall(line))):
                if cid not in defined:
                    findings.append(
                        f"docs/design/{name}:{n}: cites `{cid}`, which 09 does not define"
                    )
    return findings


def _citations(text: str) -> list[tuple[str, str]]:
    """Spec references in a cell or a preamble, as (doc, target).

    Four forms occur in 09, and all four are load-bearing somewhere in the catalog:
      `03 §11`             -> ("03", "11")        a section
      `03 §3.3, §4.3`      -> two, the second inheriting the document
      `05 C15`             -> ("05", "C15")       a component, not a section
      `this doc §6`        -> ("09", "6")         self-reference (the meta suite)
    Markdown links are flattened first: `[05](05-system-architecture.md) §8` is `05 §8`.
    A bare `§7.5` with no document named before it in the same text is 09's own convention for a
    self-reference, and resolves against 09.
    """
    flat = re.sub(r"\[(\d\d)\]\([^)]*\)", r"\1", text)
    flat = re.sub(r"\bthis doc\b", "09", flat, flags=re.I)
    cites: list[tuple[str, str]] = []
    doc: str | None = None
    for token in re.split(r"[,;]", flat):
        dm = re.search(r"\b(0[1-9])\s+§\s*([0-9][0-9a-z.]*)", token)
        if dm:
            doc = dm.group(1)
            cites.append((doc, dm.group(2).rstrip(".")))
            continue
        cm = re.search(r"\b(0[1-9])\s+(C(?:-[A-Z]{2}|\d{1,2}))\b", token)
        if cm:
            doc = cm.group(1)
            cites.append((doc, cm.group(2)))
            continue
        sm = SOURCE_MORE.search(token)
        if sm:
            cites.append((doc or "09", sm.group(1).rstrip(".")))
    return cites


def check_catalog_sources_resolve(conf: str) -> list[str]:
    """V-MET-010, reverse: every catalog row traces to something that exists.

    The catalog is not one table with one shape. `V-ISO` has no Source column at all — its
    preamble says "CH1-CH9 as defined in 05 §8" and the rows inherit it; `V-CHR` swaps Source for
    Kind; the §8 meta table has neither. So the source is read from the row's `Source` column when
    the table has one and from the preceding section's preamble when it does not. A table with
    neither is the finding: those checks have no stated rationale anywhere.
    """
    section_index = {num: set(headings(_read(name))) for num, name in SPECS.items()}
    section_index["09"] = set(headings(conf))
    components = _component_ids()

    findings = []
    src_idx: int | None = None
    in_table = False
    preamble: list[tuple[str, str]] = []
    since_heading: list[str] = []

    for n, line in enumerate(conf.splitlines(), 1):
        if line.startswith("#"):
            in_table, src_idx, since_heading = False, None, []
            preamble = []
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else None
        if cells and not in_table and cells[0] == "ID":
            in_table = True
            src_idx = cells.index("Source") if "Source" in cells else None
            preamble = _citations(" ".join(since_heading))
            continue
        if not line.startswith("|"):
            in_table = False
            since_heading.append(line)
            continue

        m = CATALOG_ROW.match(line)
        if not m:
            continue
        cid = m.group(1)
        cell = cells[src_idx] if (cells and src_idx is not None and src_idx < len(cells)) else ""
        cites = _citations(cell) or preamble
        if not cites:
            findings.append(
                f"09:{n}: `{cid}` traces to no spec section — neither its row (Source cell "
                f"{cell!r}) nor its section preamble names one"
            )
            continue
        for doc, target in cites:
            if target.startswith("C"):
                if target not in components:
                    findings.append(
                        f"09:{n}: `{cid}` sources `{doc} {target}`, which is not a component in "
                        f"05 §1"
                    )
            elif target not in section_index.get(doc, ()):
                findings.append(
                    f"09:{n}: `{cid}` sources `{doc} §{target}`, which is not a heading in "
                    f"{SPECS.get(doc, CONFORMANCE)} — renumbered or deleted"
                )
    return findings


# --------------------------------------------------------------------------------------------
# V-MET-011 — the traceability matrix.
#
# Six of the eight specs carry a Verification section that states obligations. The pairs are
# DECLARED here and also DISCOVERED below, and the lint fails if the two disagree. Declaring alone
# would silently drop a section that got renumbered; discovering alone would silently absorb a
# Verification section added to a spec that had none — new obligations arriving as a green run.
VERIFICATION_SECTIONS = {"02": "10", "03": "11", "04": "9", "05": "8", "06": "10", "08": "7"}

# 01 §8 and 07 §5 are also titled "Verification" and are NOT obligation sources: 01 §8 is the index
# ("each success criterion is made concrete in 02 §10, 03 §11, …") and 07 §5 is the harness's
# iterate-until-green loop. They are excluded by name rather than by a title heuristic, because
# "this section is prose about verification, not a list of properties" is a judgement and the file
# should say who made it. The exclusion is not taken on trust either: 01 §8 publishes the list of
# the six, and the lint asserts that published list equals VERIFICATION_SECTIONS above. A spec that
# indexes the obligations is not itself indexed by them.
VERIFICATION_INDEX_SECTIONS = {"01": "8", "07": "5"}
VERIFICATION_TITLE = re.compile(r"^verification\b", re.I)
# `02 §10` inside 01 §8's enumeration. Anchored on a two-digit spec number so the `§2/§5` a few
# lines later, which points at 07's own sections, is not mistaken for one.
INDEXED_SECTION = re.compile(r"\b(0[1-8])\s+§\s*(\d+)\b")
TRACEABILITY = Path("verification") / "traceability.yaml"


def _section_body(text: str, number: str, where: str = "the spec") -> list[str]:
    """The lines under `## <number>. ...`, up to the next `## `."""
    lines = text.splitlines()
    head = re.compile(rf"^## {re.escape(number)}\.?\s")
    start = next((i for i, l in enumerate(lines) if head.match(l)), None)
    if start is None:
        raise SystemExit(f"could not run: {where} has no `## {number}.` heading")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1 : end]


def _split_bullets(body: list[str]) -> list[str]:
    """Top-level `- ` items in order, each joined with its indented continuation."""
    out: list[str] = []
    buf: list[str] | None = None
    for line in body + [""]:
        if line.startswith("- "):
            if buf:
                out.append(re.sub(r"\s+", " ", " ".join(buf)).strip())
            buf = [line[2:]]
        elif buf is not None:
            if line.startswith("  ") and line.strip():
                buf.append(line.strip())
            else:
                out.append(re.sub(r"\s+", " ", " ".join(buf)).strip())
                buf = None
    return out


def verification_bullets() -> dict[str, str]:
    """`<spec>§<section>#<n>` -> the bullet's text, whitespace-normalised.

    A bullet is a TOP-LEVEL `- ` item. Its continuation is any following indented non-empty line,
    joined with a single space; a blank line or an unindented line ends it. Nested `  - ` sub-items
    are therefore part of their parent bullet and are not numbered separately — 06 §10 in
    particular hangs sub-cases off single obligations, and splitting them would renumber every
    later key in the section the first time somebody promoted a sub-case to its own line. The key
    is positional on purpose: it is stable under rewording (the lint catches that separately, by
    text) and unstable under REORDERING, which is the edit that would silently re-point a mapping.
    """
    out: dict[str, str] = {}
    for spec_num, section_num in sorted(VERIFICATION_SECTIONS.items()):
        body = _section_body(_read(SPECS[spec_num]), section_num)
        for n, text in enumerate(_split_bullets(body), 1):
            out[f"{spec_num}§{section_num}#{n}"] = text
    if len(out) < 100:
        raise SystemExit(f"could not run: only {len(out)} Verification bullets parsed — parser broke")
    return out


def check_verification_sections_declared() -> list[str]:
    """The declared set of Verification sections still matches the specs, two ways."""
    findings = []
    for num, name in SPECS.items():
        found = {n for n, title in headings(_read(name)).items() if VERIFICATION_TITLE.match(title)}
        # Only top-level `## N.` sections are obligations; a `### N.M Verification` sub-heading is
        # part of its parent's prose.
        found = {n for n in found if "." not in n}
        want = {VERIFICATION_SECTIONS.get(num) or VERIFICATION_INDEX_SECTIONS.get(num)} - {None}
        if found != want:
            findings.append(
                f"docs/design/{name}: Verification section is §{sorted(found) or ['none']}, but "
                f"this lint declares §{sorted(want) or ['none']} — renumbered, renamed, or newly "
                f"added. Every bullet under it is an obligation; fix the declaration."
            )

    # 01 §8's own enumeration of where the obligations live, against the declaration above.
    index = _section_body(_read(SPECS["01"]), VERIFICATION_INDEX_SECTIONS["01"])
    published = dict(INDEXED_SECTION.findall(" ".join(index)))
    if published != VERIFICATION_SECTIONS:
        findings.append(
            f"01 §8 indexes the Verification sections as {published}, but this lint reads "
            f"{VERIFICATION_SECTIONS}. One of the two is wrong, and the traceability matrix is "
            f"built from the second."
        )
    return findings


# A YAML quoted scalar, either style. Double-quoted is JSON's own escaping; single-quoted escapes
# only `'`, by doubling it, and honours no backslash at all.
SCALAR = r"\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'"


def _scalar(raw: str) -> str:
    return json.loads(raw) if raw.startswith('"') else raw[1:-1].replace("''", "'")


def load_traceability(text: str) -> dict[str, dict]:
    """Parse the fixed schema of `verification/traceability.yaml`. Raises on anything else.

    A strict thirty-line parser instead of PyYAML because this runs in the L0 chain, and L0
    installs no dependencies — a check that needs a package is not L0. The schema is small enough
    that the restriction costs nothing and buys one thing worth having: a file that drifts out of
    the shape below fails loudly here instead of being reinterpreted into something plausible.

        "<spec>§<section>#<n>":
          bullet: "<the bullet, whitespace-normalised>"
          checks:
            - V-XXX-000

    BOTH YAML quoting styles are accepted for the scalars, because prettier owns the formatting of
    this file and rewrites a double-quoted scalar containing a `"` into a single-quoted one. Only
    accepting the style the first version happened to be written in would mean `prettier --write`,
    which CI runs over the whole branch diff, could turn a green matrix red.
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
                raise ValueError(f"{TRACEABILITY}:{n}: duplicate entry {key!r}")
            entries[key] = {"bullet": None, "checks": [], "line": n}
            in_checks = False
        elif key is None:
            raise ValueError(f"{TRACEABILITY}:{n}: content before the first entry: {line!r}")
        elif m := re.fullmatch(rf"  bullet: ({SCALAR})", line):
            entries[key]["bullet"] = _scalar(m.group(1))
            in_checks = False
        elif line == "  checks:":
            in_checks = True
        elif in_checks and (m := re.fullmatch(r"    - (V-[A-Z]{3}-\d{3})", line)):
            entries[key]["checks"].append(m.group(1))
        else:
            raise ValueError(f"{TRACEABILITY}:{n}: not in the schema: {line!r}")
    return entries


def check_traceability(defined: set[str]) -> list[str]:
    """V-MET-011 — bullets and the matrix agree in both directions."""
    path = REPO / TRACEABILITY
    if not path.is_file():
        return [f"{TRACEABILITY} does not exist — every Verification bullet is unmapped"]
    try:
        entries = load_traceability(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [str(exc)]

    bullets = verification_bullets()
    findings = []
    for key, text in bullets.items():
        entry = entries.get(key)
        if entry is None:
            findings.append(
                f"{TRACEABILITY}: no entry for `{key}` — a Verification bullet with no check: "
                f"{text[:90]}..."
            )
            continue
        if entry["bullet"] != text:
            findings.append(
                f"{TRACEABILITY}:{entry['line']}: `{key}` is stale — the spec now reads "
                f"{text[:70]!r}, the matrix still has {(entry['bullet'] or '')[:70]!r}. Re-read the "
                f"bullet and re-decide the checks; do not just paste the new text in."
            )
        if not entry["checks"]:
            findings.append(f"{TRACEABILITY}:{entry['line']}: `{key}` names no check ID")
        for cid in entry["checks"]:
            if cid not in defined:
                findings.append(
                    f"{TRACEABILITY}:{entry['line']}: `{key}` names `{cid}`, which 09 does not define"
                )
    for key, entry in entries.items():
        if key not in bullets:
            findings.append(
                f"{TRACEABILITY}:{entry['line']}: `{key}` maps a bullet that no longer exists — "
                f"deleted, or the section was renumbered under it"
            )
    return findings


# --------------------------------------------------------------------------------------------
def _component_ids() -> list[str]:
    """The component IDs of 05 §1, in order. The inventory is that section's FIRST table."""
    body = section(_read(SPECS["05"]), "\n## 1. Component inventory", "\n## 2.")
    ids: list[str] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            if ids:
                break
            continue
        cell = line.split("|")[1].strip().strip("*").strip()
        if COMPONENT_ID.fullmatch(cell):
            ids.append(cell)
    if not ids:
        raise SystemExit("could not run: no component IDs parsed from 05 §1")
    return ids


def check_component_inventory(conf: str) -> list[str]:
    """V-MET-012a — every 05 §1 component ID appears in 09 §5.1."""
    ids = _component_ids()
    s51 = section(conf, "### 5.1 Component inventory", "### 5.2")
    mentioned = set(COMPONENT_ID.findall(s51))
    return [
        f"09 §5.1: component `{cid}` (05 §1) has no entry — no Exists/Wired/Exercised probe, "
        f"and it is not listed as deferred or optional either"
        for cid in ids
        if cid not in mentioned
    ]


def check_contract_inventory(conf: str) -> list[str]:
    """V-MET-012b — every contract-titled section of 06 is cited in 09 §5.2."""
    six = _read(SPECS["06"])
    contracts = [
        (num, title)
        for num, title in headings(six).items()
        if CONTRACT_TITLE.search(title) and num != "10"
    ]
    if not contracts:
        raise SystemExit("could not run: no contract sections parsed from 06")
    s52 = section(conf, "### 5.2 Contract inventory", "### 5.3")
    cited = set(re.findall(r"06 §([0-9][0-9a-z.]*)", s52))
    findings = []
    for num, title in contracts:
        # A parent section is covered by its children: §4 is satisfied by §4.1-§4.4.
        if num in cited or any(c.startswith(num + ".") for c in cited):
            continue
        findings.append(f"09 §5.2: 06 §{num} ({title[:60]}) is a contract with no inventory entry")
    return findings


# --------------------------------------------------------------------------------------------
def self_test(quiet: bool = False) -> int:
    """Each parser must fire on the defect it exists for."""
    cases = []

    dup = "| V-CTN-001 | a | 03 §11 | L2 | 8 |\n| V-CTN-001 | b | 03 §11 | L2 | 8 |\n"
    cases.append(("duplicate definition detected", bool(check_ids_defined_once(dup))))
    cases.append(
        ("single definition is quiet", not check_ids_defined_once("| V-CTN-001 | a | x | y | z |\n"))
    )

    sites = definition_sites("- **V-CMP-010** — a bullet-defined ID\n  mentions V-CMP-010 again\n")
    cases.append(("a mention is not a definition", sites.get("V-CMP-010") == [1]))

    hs = headings("## 2b. ChatOps addressing & routing contract\n### 4.3 `ActionRecord`\n")
    cases.append(("letter-suffixed headings parse", hs.get("2b", "").startswith("ChatOps")))
    cases.append(("nested headings parse", "4.3" in hs))

    m = SOURCE_DOC.search("03 §3.3, §4.3")
    cases.append(("source doc+section parses", bool(m) and m.group(1, 2) == ("03", "3.3")))
    cases.append(("bare continuation parses", SOURCE_MORE.search(", §4.3").group(1) == "4.3"))

    cases.append(("component id matches C-JR", bool(COMPONENT_ID.fullmatch("C-JR"))))
    cases.append(("component id matches C17", bool(COMPONENT_ID.fullmatch("C17"))))
    cases.append(("component id rejects CIDR", not COMPONENT_ID.fullmatch("CIDR")))

    # --- V-MET-011: the bullet extractor and the matrix parser ---
    sample = (
        "## 9. Verification\n\n"
        "- **First:** one line.\n"
        "- **Second:** wraps\n  onto a continuation line.\n"
        "  - a nested sub-item belongs to its parent\n\n"
        "Prose that is not a bullet.\n\n"
        "- **Third:** after the prose.\n"
    )
    got = _split_bullets(_section_body(sample, "9"))
    cases.append(("three top-level bullets, not four", len(got) == 3))
    cases.append(("continuation lines join", got[1].endswith("to its parent") if len(got) > 1 else False))
    cases.append(("prose does not become a bullet", got[-1] == "**Third:** after the prose."))

    good = '"02§10#1":\n  bullet: "a \\"quoted\\" one"\n  checks:\n    - V-CTN-001\n'
    parsed = load_traceability(good)
    cases.append(("matrix entry parses", parsed["02§10#1"]["bullet"] == 'a "quoted" one'))
    cases.append(("matrix checks parse", parsed["02§10#1"]["checks"] == ["V-CTN-001"]))
    # What `prettier --write` turns the entry above into. If this control dies, CI's own formatter
    # can redden the matrix.
    pretty = "\"02§10#1\":\n  bullet: 'a \"quoted\" one with an '' apostrophe'\n  checks:\n    - V-CTN-001\n"
    cases.append(
        (
            "prettier's single-quoted rewrite parses",
            load_traceability(pretty)["02§10#1"]["bullet"] == "a \"quoted\" one with an ' apostrophe",
        )
    )
    for label, bad in [
        ("off-schema line rejected", good + "  notes: hi\n"),
        ("duplicate entry rejected", good + good),
        ("bare check outside checks: rejected", '"a":\n  bullet: "b"\n    - V-CTN-001\n'),
    ]:
        try:
            load_traceability(bad)
            cases.append((label, False))
        except ValueError:
            cases.append((label, True))

    failures = 0
    for label, ok in cases:
        if not quiet or not ok:
            print(f"  control {'OK  ' if ok else 'DEAD'} {label}")
        failures += 0 if ok else 1
    if not quiet:
        print(f"\n{len(cases) - failures}/{len(cases)} controls behave.")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    conf = _read(CONFORMANCE)
    defined = set(definition_sites(conf))
    if len(defined) < 100:
        raise SystemExit(f"could not run: only {len(defined)} check IDs parsed from 09 — parser broke")

    # The bullet extractor keys every entry in the matrix off a (spec, section) pair, so it can only
    # be trusted once those pairs have been confirmed. Run the declaration check first and, if it
    # has anything to say, do not run the extractor at all: it would raise `could not run: no `## 9.`
    # heading`, which is true, unhelpful, and would replace the finding that explains it.
    section_findings = check_verification_sections_declared()
    groups = [
        ("V-MET-013 (check IDs) — one definition site per ID", check_ids_defined_once(conf)),
        ("V-MET-010 forward — specs cite only defined IDs", check_spec_citations_resolve(conf, defined)),
        ("V-MET-010 reverse — every check traces to a real spec section", check_catalog_sources_resolve(conf)),
        ("V-MET-012a — 09 §5.1 covers every 05 §1 component", check_component_inventory(conf)),
        ("V-MET-012b — 09 §5.2 covers every 06 contract", check_contract_inventory(conf)),
        ("V-MET-011 — the Verification sections are where we say", section_findings),
        (
            "V-MET-011 — every Verification bullet resolves to a check",
            check_traceability(defined)
            if not section_findings
            else ["NOT RUN — fix the section declarations above first; every matrix key is derived from them"],
        ),
    ]

    total = 0
    for label, findings in groups:
        if findings:
            total += len(findings)
            print(f"FAIL  {label}")
            for f in findings:
                print(f"        - {f}")
        else:
            print(f"PASS  {label}")

    # The controls run on every invocation, not only under `--self-test` — the convention
    # `closed-allowlist.py` set. Parser controls that only fire when somebody remembers the flag
    # are LSN-007 aimed at the verification machinery: this file is six regexes over prose, and a
    # regex that has silently stopped matching reports PASS on everything.
    if self_test(quiet=True) != 0:
        total += 1
        print("FAIL  negative controls — a parser control is DEAD (re-run with --self-test)")
    else:
        print("PASS  negative controls — every parser fires on the defect it exists for")

    summary = f"\n{len(defined)} check IDs defined in 09"
    try:
        mapped = load_traceability((REPO / TRACEABILITY).read_text(encoding="utf-8"))
        summary += (
            f"; {len(verification_bullets())} Verification bullets in 01-08"
            f"; {len({c for e in mapped.values() for c in e['checks']})} distinct checks cited by the matrix."
        )
    except (OSError, ValueError, SystemExit):
        summary += "; the matrix or the bullets did not parse."  # already reported above
    print(summary)
    if total:
        print(f"{total} finding(s). The conformance spec has drifted from the specs it verifies.")
        return 1
    print("Spec IDs: OK — 09 and 01-08 agree on what exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
