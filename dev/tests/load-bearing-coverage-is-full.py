#!/usr/bin/env python3
"""V-MET-002 -- full coverage of the load-bearing suites.

09 section 8's row: *"every normative requirement owned by V-CTN, V-BRK, V-REV, V-ISO or V-ADV maps
to >=1 check. An unmapped requirement in these categories fails the build"*. Section 8.1 says the
same thing twice, once as the rule and once as the deadline: *"Full coverage, no exceptions, in the
load-bearing suites ... a gap here is not a backlog item"*, and *"those must reach zero uncovered
before Phase 10 grants the first write credential"*. This file is that gate.

IT IS THE STRICT HALF OF A PARTITION, AND THE PARTITION IS NOT DRAWN HERE. `owned` is derived from
09 section 6's `Source` column by `dev/tests/coverage-ratchet-holds.py`, which is V-MET-008 -- the
lenient half -- and that file argues the derivation at length. Both gates read the same `derive()`
because the two obligations are complements: any section moved out of this one lands in that one,
and a second implementation of `owned` would be a dial with a comfortable end. This file imports it
rather than restating it.

THREE PROPERTIES, AND WHY THE THIRD IS NOT PEDANTRY.

  1. THE POPULATION IS REAL. Enough enumerated requirements, enough derived obligations, every one
     of the five suites owning at least one section AND at least one obligation, and no
     load-bearing `Source` cell that resolves to nothing. A gate whose population went empty reports
     full coverage of nothing, and in this repo `VACUOUS:` is a failure. The per-suite clause is not
     redundant with the count: V-ISO owns exactly one section and its obligations live in the OTHER
     ID space, so a single-space reading gives a BLOCKING-ALWAYS suite zero obligations while the
     total still reads 400-odd. An unresolved `Source` cell belongs here for the same reason a blank
     one does: it silently shrinks the strict half.

     V-MET-008 asserts non-emptiness too, and that is deliberate rather than a duplicated gate. It
     asserts it of ITS population; a check that lets a sibling vouch for its own denominator is a
     check that goes quiet the day the sibling is skipped, retired, or scoped away.

     THERE IS DELIBERATELY NO FLOOR UNDER THE CATALOG, and the asymmetry is the reason. Every other
     population here fails silent -- an empty one turns a gate green. Property 3's does not: a
     catalog that stopped parsing makes every mapped ID look absent, so the failure mode is 341
     findings rather than none. Asserting a floor over it would read like vacuity protection while
     protecting against nothing. The floor that IS load-bearing sits on the shared derivation, is
     asserted by `parse_sources`, and reaches this arm as a parse finding -- which the control
     exercises, because an upstream guard nobody demonstrates is an upstream guard nobody notices
     losing.

  2. EVERY OWNED OBLIGATION MAPS TO AT LEAST ONE CHECK. The remainder is emitted ONE FINDING PER
     OBLIGATION, by ID and with the statement's own words -- never as a count. A coverage gate that
     prints a number and not a list is how the work stops silently (09 section 8.1's pairing with
     V-MET-009), and it is the difference between a worklist and a score.

  3. WHAT IT MAPS TO IS A CHECK. Every ID claimed by an owned obligation must be a row of the 09
     section 6 catalog. Nothing else in the tree asserts this: `requirements-are-enumerated.py`
     validates the enumeration against the specs and the IDs against their own grammar, and neither
     it nor V-MET-008 ever asks whether `V-CTN-999` exists (not-a-check-id). Without this clause the strictest gate
     in the repo is the easiest one to satisfy -- a typo, a renumbered ID, or a check retired out
     from under a mapping all read as coverage, and property 2 goes green on them.

WHAT THIS CHECK DOES NOT DO. It does not ask whether the mapped check is GREEN. Coverage and
greenness are different questions with different owners: `dev/tests/phase-ratchet-is-asserted.py`
owns "required and not green", and 09 section 8's sentence is about the mapping. Many load-bearing
requirements today map to checks dated phase 10 or 11, and that is the catalog working as designed
-- the draw-down 09 section 8.1 describes. It also does not decide whether one check is ADEQUATE
coverage; the spec says "at least one" and this file says exactly that. The judgement about which
check honestly covers which statement is curated in `verification/requirements.yaml`, under the two
rules stated in its header: section citation is not coverage, and a requirement with no honest check
is left unmapped rather than closed with the nearest-looking catalog row.

WHY THIS RUNS OFF THE L0 CHAIN TODAY. See `dev/L0-CHAIN.txt` -- the live arm is red by construction
until the catalog grows the rows that close the published remainder, so it runs in
`dev/verify/verify-phase9.sh` where its redness is the phase's worklist, and only the negative
control is chain-eligible. The control's base case is the FUTURE tree, in which the remainder is
closed and this arm must go GREEN.

Run:  python3 dev/tests/load-bearing-coverage-is-full.py
      python3 dev/tests/load-bearing-coverage-is-full.py --negative-control
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFORMANCE = REPO / "docs" / "design" / "09-verification-and-validation.md"
RATCHET_MODULE = REPO / "dev" / "tests" / "coverage-ratchet-holds.py"
REQUIREMENTS = Path("verification") / "requirements.yaml"
TRACEABILITY = Path("verification") / "traceability.yaml"

# Non-vacuity floors. Low enough never to need maintenance, high enough that a parser that stopped
# reading cannot report a clean run. The owned floor is the one that matters here: it is this
# check's whole denominator, and 420 today.
MIN_STATEMENTS = 700
MIN_OWNED = 300


def _ratchet():
    """`coverage-ratchet-holds.py` by path -- its name is not an identifier."""
    spec = importlib.util.spec_from_file_location("coverage_ratchet_holds", RATCHET_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def catalog_ids(conformance: str) -> set[str]:
    """Every check ID with a catalog row: section 6, the extended catalog, and section 8's V-MET.

    Read here rather than taken from `parse_sources`, which yields 239 rows to this function's 251.
    The twelve it does not see are the V-MET table, which lives in section 8 and which 09 calls a
    catalog in as many words -- and which nine of document 06's mapped rule rows name as their
    check. Borrowing the derivation's population would have rejected every one of them.
    """
    return {
        m.group(1)
        for line in conformance.splitlines()
        if (m := re.match(r"^\|\s*(V-[A-Z]{3}-\d{3})\s*\|", line))
    }


# ------------------------------------------------------------------------------------------ check
def check(
    requirements_text, traceability_text, conformance_text, extracted, stats=None
) -> list[str]:
    """Three properties. Every return is a finding; an empty list is the pass."""
    ratchet = _ratchet()
    enumerator = ratchet._enumerator()

    try:
        entries = enumerator.load_requirements(requirements_text)
        bullets = ratchet.load_bullets(traceability_text)
        derived = ratchet.derive(conformance_text, extracted, bullets)
    except (ratchet.ArtifactError, enumerator.ArtifactError) as exc:
        return [f"an input did not parse: {exc}"]

    catalog = catalog_ids(conformance_text)
    owned: dict[str, set[str]] = derived["load_bearing"]
    covered: dict[str, list[str]] = {
        **{rid: entry["checks"] for rid, entry in entries.items()},
        **bullets,
    }

    # ---- property 1: the population is real ----------------------------------------------------
    # Returned ALONE if it fires. A vacuous population also makes every obligation look uncovered,
    # and four hundred property-2 lines wrapped around the one line that explains them is a report
    # nobody reads to the end.
    vacuity: list[str] = []
    if len(owned) < MIN_OWNED:
        vacuity.append(
            f"VACUOUS: the load-bearing partition yielded {len(owned)} obligation(s), under the "
            f"floor of {MIN_OWNED}. Full coverage of nothing is not full coverage."
        )
    if len(entries) < MIN_STATEMENTS:
        vacuity.append(
            f"VACUOUS: the enumeration yielded {len(entries)} requirement(s), under the floor of "
            f"{MIN_STATEMENTS}. {REQUIREMENTS} is this gate's denominator."
        )
    for suite in ratchet.LOAD_BEARING:
        sections = len(derived["per_suite"][suite])
        obligations = len(derived["per_suite_obligations"][suite])
        if not sections or not obligations:
            vacuity.append(
                f"a load-bearing suite owns nothing: {suite} derived {sections} spec section(s) "
                f"and {obligations} obligation(s). 09 §8 names five suites and this gate must ask "
                f"something of each of them."
            )
    for unresolved in derived["unresolved"]:
        vacuity.append(
            f"Source resolves to nothing: {unresolved}. An unreadable Source cell moves its "
            f"section out of the BLOCKING-ALWAYS half of the partition, silently."
        )
    if vacuity:
        return vacuity

    findings: list[str] = []

    # ---- property 2: every owned obligation maps to at least one check -------------------------
    remainder = sorted(
        (oid for oid in owned if not covered.get(oid)), key=ratchet._sort_key
    )
    for oid in remainder:
        suites = " ".join(sorted({c[:5] for c in owned[oid]}))
        text = " ".join(entries.get(oid, {}).get("text", "").split())[:110]
        findings.append(
            f"{oid} maps to no check and is owned by {suites}: {text!r} "
            f"(09 §8 -- an unmapped requirement in these categories fails the build)"
        )

    # ---- property 3: what it maps to is a check ------------------------------------------------
    for oid in sorted(owned, key=ratchet._sort_key):
        for claimed in covered.get(oid, []):
            if claimed not in catalog:
                findings.append(
                    f"{oid} names {claimed}, which has no row in the 09 §6 catalog. A mapping to a "
                    f"string that is not a check ID is not coverage -- it is the cheapest way to "
                    f"turn this gate green."
                )

    if stats is not None:
        stats.update(
            owned=len(owned),
            catalog=len(catalog),
            statements=len(entries),
            bullets=len(bullets),
            remainder=remainder,
            per_suite={
                s: len(derived["per_suite_obligations"][s]) for s in ratchet.LOAD_BEARING
            },
        )
    return findings


# ------------------------------------------------------------------------------------------ entry
def _inputs():
    ratchet = _ratchet()
    return (
        (REPO / REQUIREMENTS).read_text(encoding="utf-8"),
        (REPO / TRACEABILITY).read_text(encoding="utf-8"),
        CONFORMANCE.read_text(encoding="utf-8"),
        ratchet._enumerator().extract(),
    )


def run() -> int:
    stats: dict = {}
    findings = check(*_inputs(), stats=stats)
    if findings:
        print(
            "FAIL: V-MET-002 (L0) -- the load-bearing suites are not at full coverage",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        print(
            f"\n  {len(stats.get('remainder', findings))} obligation(s) above, published by ID "
            f"rather than counted (09 §8.1's pairing with V-MET-009). Closing one is a mapping in "
            f"{REQUIREMENTS} when an honest catalog row exists, and a new row in 09 §6 when it "
            f"does not. It is not closed by citing the section.",
            file=sys.stderr,
        )
        return 1

    suites = ", ".join(f"{s} {stats['per_suite'][s]}" for s in _ratchet().LOAD_BEARING)
    print(
        f"PASS: V-MET-002 (L0) -- all {stats['owned']} obligation(s) owned by the five "
        f"load-bearing suites ({suites}) map to at least one check, drawn from "
        f"{stats['statements']} normative statements and {stats['bullets']} Verification bullets; "
        f"every claimed ID is one of {stats['catalog']} rows in the 09 §6 catalog"
    )
    return 0


# -------------------------------------------------------------------------------- negative control
def _mutate(args: tuple, index: int, fn) -> tuple:
    out = list(args)
    out[index] = fn(out[index])
    return tuple(out)


def _future_tree(base: tuple) -> tuple:
    """The tree this arm must go green on once the catalog closes the published remainder.

    A check split off from its implementation has two trees to be green on, and today's is the one
    that cannot exercise the pass ([[LSN-053]]). So the control's base is synthesised: every owned
    obligation still carrying `checks: []` is given one of the checks whose 09 §6 `Source` cites its
    section.

    THAT MAPPING IS THE ONE THE CURATION RULES FORBID. `verification/requirements.yaml`'s header
    says section citation is not coverage, and the sixteen open gaps are open precisely because the
    nearest-looking catalog row does not assert them. Inventing it here is legitimate for exactly
    one reason: nothing is written, and the property under test is "does this arm distinguish mapped
    from unmapped", not "is this mapping any good". It must never reach the tree, which is why the
    synthesis lives in the control and takes no `--emit`.

    It is written to survive the tree moving under it. When the real curation closes the remainder,
    this becomes a no-op and the base is simply today's tree -- which is correct, not broken. What
    it must never do is silently fail to close a gap it did not know how to fill: `negative_control`
    asserts the synthesised base is GREEN, and an obligation in the OTHER ID space (a
    `traceability.yaml` bullet, which this synthesiser does not touch because none is open today)
    would surface there as a named failure rather than as a quiet MISS on every row below.
    """
    ratchet = _ratchet()
    enumerator = ratchet._enumerator()
    entries = enumerator.load_requirements(base[0])
    bullets = ratchet.load_bullets(base[1])
    derived = ratchet.derive(base[2], base[3], bullets)

    text = base[0]
    for oid, owners in derived["load_bearing"].items():
        if entries.get(oid, {}).get("checks") or oid not in entries or not owners:
            continue
        stand_in = sorted(owners)[0]
        text = re.sub(
            rf'(^"{re.escape(oid)}":\n  text: .*\n)  checks: \[\]\n',
            lambda m: f"{m.group(1)}  checks:\n    - {stand_in}\n",
            text,
            count=1,
            flags=re.M,
        )
    return _mutate(base, 0, lambda _: text)


def negative_control() -> int:
    """Each mutation is a way this check could go quiet. Each names the signal it must produce.

    THE BASE IS THE FUTURE TREE, not today's. Today's tree is RED -- sixteen owned obligations have
    no catalog row that asserts them -- so a control built on it could not tell "the mutation was
    caught" from "everything is red anyway", and every row would score `caught` for the wrong
    reason. Building on the closed tree costs one synthesis step and buys the case that actually
    matters: an arm that could not go green once the work lands makes editing the arm the cheapest
    diff at exactly the wrong moment ([[LSN-053]]).
    """
    today = _inputs()
    base = _future_tree(today)
    print(
        f"  base     the future tree -- the published remainder closed by synthesis "
        f"({'no-op: the real tree is already closed' if base == today else 'stand-ins applied'})"
    )
    findings = check(*base)
    if findings:
        print("  BROKEN   the synthesised base is not green, so no row below can be trusted")
        for f in findings[:4]:
            print(f"           {f}")
        print("FAIL: V-MET-002 negative control -- 0 mutations evaluated")
        return 1

    # The first owned obligation in document order, resolved from the base rather than named: a
    # mutation keyed to a literal requirement ID dies the day that statement is reworded, and a
    # no-op mutation scores MISS -- the verdict for "the check let the defect through" -- over a
    # defect that was never applied ([[LSN-063]]).
    ratchet = _ratchet()
    victim = sorted(
        ratchet.derive(base[2], base[3], ratchet.load_bullets(base[1]))["load_bearing"],
        key=ratchet._sort_key,
    )[0]

    mutations = [
        (
            "an owned obligation loses its last check",
            _mutate(base, 0, lambda t: re.sub(
                rf'(^"{re.escape(victim)}":\n  text: .*\n)  checks:\n(?:    - V-[A-Z]{{3}}-\d{{3}}\n)+',
                lambda m: f"{m.group(1)}  checks: []\n",
                t, count=1, flags=re.M,
            )),
            f"{victim} maps to no check",
        ),
        (
            "an owned obligation maps to a check ID that does not exist",
            _mutate(base, 0, lambda t: re.sub(
                rf'(^"{re.escape(victim)}":\n  text: .*\n  checks:\n)    - V-[A-Z]{{3}}-\d{{3}}\n',
                lambda m: f"{m.group(1)}    - V-CTN-999\n",  # not-a-check-id -- deliberately undefined
                t, count=1, flags=re.M,
            )),
            "names V-CTN-999, which has no row in the 09 §6 catalog",  # not-a-check-id
        ),
        (
            "V-ISO's prose source is removed, so a BLOCKING-ALWAYS suite owns nothing",
            _mutate(base, 2, lambda t: t.replace(
                "CH1–CH9 as defined in [05](05-system-architecture.md) §8.", "CH1–CH9.", 1
            )),
            "a load-bearing suite owns nothing: V-ISO derived 0 spec section(s)",
        ),
        (
            "V-ISO's sections resolve, and the space holding their obligations is not read",
            _mutate(base, 1, lambda t: re.sub(
                r"^\"05§8#\d+\":\n(?: +.*\n)*\n?", "", t, flags=re.M
            )),
            "V-ISO derived 1 spec section(s) and 0 obligation(s)",
        ),
        (
            "a load-bearing check's Source cell is blanked",
            _mutate(base, 2, lambda t: re.sub(
                r"^(\|\s*V-CTN-\d{3}\s*\|[^|]*\|)[^|]*\|", r"\1  |", t, count=1, flags=re.M
            )),
            "Source resolves to nothing",
        ),
        (
            "the enumerator returns almost nothing, so the partition is empty",
            _mutate(base, 3, lambda e: dict(sorted(e.items())[:5])),
            "VACUOUS: the load-bearing partition yielded",
        ),
        (
            "the enumeration artifact is truncated",
            _mutate(base, 0, lambda t: "\n".join(t.splitlines()[:400]) + "\n"),
            "VACUOUS: the enumeration yielded",
        ),
        (
            "the catalog stops parsing, and the shared derivation's floor is what says so",
            _mutate(base, 2, lambda t: re.sub(
                r"^\|\s*V-[A-Z]{3}-\d{3}\s*\|.*\n", "", t, flags=re.M
            )),
            "an input did not parse: 09 section 6 parsed to 0 catalog rows",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        # A mutation that did not change its input cannot be evaluated. Without this arm the
        # unmutated base is re-checked, comes back clean, and the row prints MISS -- which reads as
        # "the check let the defect through" when what happened is that the defect was never
        # applied ([[LSN-063]]).
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
        f"{'PASS' if not failures else 'FAIL'}: V-MET-002 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught, on a base tree in which "
        f"the published remainder is closed and this arm goes green"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
