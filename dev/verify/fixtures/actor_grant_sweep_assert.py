#!/usr/bin/env python3
"""The assertion block of `dev/verify/actor-grant-sweep-l2.sh`, over one sweep transcript.

WHY THE BLOCK IS A FILE AND NOT A BASH FUNCTION. The suite's `--negative-control` replays exactly
these arms against transcripts no cluster produced, and the arms have to be the SAME code in both
modes or the control is measuring a simplified copy that a real defect could slip past. In bash that
means either a very large function or a second implementation; in python it is one file both modes
call. It also makes the arms testable on their own, which is what the control does.

INPUT: a TSV transcript on a path, one row per `kubectl auth can-i` question actually asked:

    ns  sa  tier  role  kind  group  resource  subresource  verb  where  expected  answer

`expected` is what 06 says the answer must be (derived by actor_grant_expectations.py, never
written down here); `answer` is what the cluster's authorizer said -- `yes`, `no`, or empty for a
question whose command failed. A second file lists the identities that were swept, one
`ns/sa/tier/role` per line, so the coverage arm can tell "this identity answered every question
correctly" from "this identity was never asked anything".

THE ARMS. Each prints exactly one PASS: or FAIL: line, and the suite counts them.

  A-1 COVERAGE. Every discovered identity contributed rows, every kind the table defines is
      represented, and all three tiers are present. Without this the whole file is satisfied by an
      empty transcript, which is what a sweep looks like when its query loop silently failed
      ([[LSN-035]]). Floors, not exact counts: 06 §2.2 is expected to grow.
  A-2 NO UNANSWERED QUESTIONS. A row with an empty `answer` is a question whose `kubectl` invocation
      failed -- unknown resource, bad flag, unreachable API server. It is neither a yes nor a no,
      and scoring it as either invents a result. Any such row fails this arm by name.
  A-3 THE POSITIVE HALF. Every `expected=yes` row answered yes. This is the half that makes the
      sweep falsifiable: a fleet whose RBAC never applied answers `no` to everything and would pass
      a denial-only sweep perfectly. `reader-read` is excluded here and scored by A-6, because it is
      an any-of rather than an all-of.
  A-4 THE NEGATIVE HALF. Every `expected=no` row answered no. Accept (e) itself.
  A-5 APPEND-ONLY, scored separately from A-4 although its rows are a subset. V-BRK-013 names this
      property on its own -- "cannot `update`/`delete` a record" -- and it is the one whose failure
      means the journal is a mutable log rather than an audit trail. An arm that reports "37 of 474
      negative rows failed" does not say that; this one does.
  A-6 THE FREEZE READ, per tier. V-BRK-013 says `fleetfreezes` is readable by EVERY tier "including
      developer-team", and 06 §4.4 is why: a tier that cannot read the freeze object fails closed
      permanently. The missing-grant direction bricks a tier rather than failing safe, so it is
      asserted per tier and not once over the fleet.
  A-7 READER NON-VACUITY, the any-of. Each reader identity must answer yes to at least one read
      probe. Every `reader-no-write` row is satisfied by an identity bound to nothing at all, and on
      a cluster where the reader RoleBinding failed to apply that is precisely the state.

Exit: 0 = every arm green · 1 = at least one arm red · 2 = the transcript could not be read.
"""

from __future__ import annotations

import collections
import pathlib
import sys

# Floors for the coverage arm. Deliberately well under today's derivation (702 rows over 3 tiers) --
# the arm exists to catch a transcript that is empty or nearly so, not to pin a count that 06 is
# allowed to change. The per-kind floor is 1: a kind deriving zero rows is caught upstream by
# actor_grant_expectations.py --self-test, and re-pinning it here would make two files fail for one
# reason.
MIN_ROWS = 120
MIN_TIERS = 3
KINDS = (
    "journal-write",
    "journal-read",
    "freeze-read",
    "tier-read",
    "append-only",
    "dark-write",
    "elsewhere-write",
    "reader-no-write",
    "reader-read",
)
FIELDS = (
    "ns",
    "sa",
    "tier",
    "role",
    "kind",
    "group",
    "resource",
    "subresource",
    "verb",
    "where",
    "expected",
    "answer",
)


def q(row: dict[str, str]) -> str:
    """A row, printed the way a human would re-ask it."""
    res = row["resource"]
    if row["group"]:
        res = f"{res}.{row['group']}"
    if row["subresource"]:
        res = f"{res} --subresource={row['subresource']}"
    return f"{row['verb']} {res} (in {row['ns'] if row['where'] == 'own' else 'elsewhere'})"


def read_rows(path: pathlib.Path) -> list[dict[str, str]]:
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != len(FIELDS):
            sys.exit(
                f"FAIL: {path}:{n} has {len(parts)} field(s), not {len(FIELDS)}. The transcript is "
                f"malformed, and a malformed transcript parsed leniently is a sweep that scores "
                f"rows it misread."
            )
        row = dict(zip(FIELDS, parts))
        # THE ARITY CHECK ABOVE IS NOT ENOUGH, and the run that proved it is worth writing down.
        # The sweep's row builder split the query line on `IFS=$'\t'`, which collapses runs of tabs,
        # so every row with an empty `subresource` came through shifted by one field: the right
        # NUMBER of columns, each holding its neighbour's value. `expected` then held a namespace
        # rather than yes/no, and the arms below simply found fewer rows to score — A-3 reported
        # "all 12 grants held" out of a transcript of 647 and called itself green. A transcript is
        # not well-formed because it has twelve columns; it is well-formed because each column holds
        # a value from that column's alphabet, and every one of these is closed.
        for field, allowed in (
            ("kind", KINDS),
            ("role", ("actor", "reader")),
            ("where", ("own", "elsewhere")),
            ("expected", ("yes", "no")),
            ("answer", ("yes", "no", "")),
        ):
            if row[field] not in allowed:
                sys.exit(
                    f"FAIL: {path}:{n} has {field}='{row[field]}', which is not one of "
                    f"{', '.join(repr(a) for a in allowed)}. The transcript has the right number of "
                    f"columns and the wrong values in them — the shape a field-shifted parse takes. "
                    f"Scoring it would report on rows it misread, which is worse than not scoring "
                    f"it at all."
                )
        rows.append(row)
    return rows


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(f"usage: {sys.argv[0]} <transcript.tsv> <identities.txt>")
    transcript = pathlib.Path(sys.argv[1])
    identities_path = pathlib.Path(sys.argv[2])
    if not transcript.is_file():
        print(
            f"FAIL: no sweep transcript at {transcript}. Nothing was asked of the authorizer, so "
            f"there is nothing to judge.",
            file=sys.stderr,
        )
        return 2
    rows = read_rows(transcript)
    identities = [
        ln.strip() for ln in identities_path.read_text().splitlines() if ln.strip()
    ] if identities_path.is_file() else []

    failed = 0

    def ok(msg: str) -> None:
        print(f"PASS: {msg}")

    def bad(msg: str) -> None:
        nonlocal failed
        failed += 1
        print(f"FAIL: {msg}")

    # --- A-1 coverage ---------------------------------------------------------------------------
    by_ident = collections.Counter(f"{r['ns']}/{r['sa']}/{r['tier']}/{r['role']}" for r in rows)
    kinds = collections.Counter(r["kind"] for r in rows)
    tiers = {r["tier"] for r in rows}
    gaps = []
    if len(rows) < MIN_ROWS:
        gaps.append(f"only {len(rows)} question(s) were asked, below the floor of {MIN_ROWS}")
    if len(tiers) < MIN_TIERS:
        gaps.append(f"only {len(tiers)} tier(s) swept ({', '.join(sorted(tiers)) or 'none'}); 06 §2.2 has {MIN_TIERS}")
    missing_kinds = [k for k in KINDS if kinds.get(k, 0) == 0]
    if missing_kinds:
        gaps.append(f"no rows of kind(s) {', '.join(missing_kinds)} — those clauses went unasked")
    unasked = [i for i in identities if by_ident.get(i, 0) == 0]
    if unasked:
        gaps.append(
            f"{len(unasked)} discovered identity/identities were asked nothing: {', '.join(unasked)}"
        )
    if gaps:
        bad(
            "A-1 coverage: the sweep did not sweep. "
            + "; ".join(gaps)
            + ". Every arm below passes on an empty transcript, so this one runs first"
        )
    else:
        ok(
            f"A-1 coverage: {len(rows)} questions over {len(by_ident)} identity/identities and "
            f"{len(tiers)} tiers, every kind represented"
        )

    # --- A-2 no unanswered questions --------------------------------------------------------------
    blank = [r for r in rows if r["answer"] not in ("yes", "no")]
    if blank:
        bad(
            f"A-2 unanswered: {len(blank)} question(s) produced no yes/no from the authorizer, e.g. "
            f"{q(blank[0])} -> '{blank[0]['answer']}'. A question that errored is not a denial; "
            f"scoring it as one is how a sweep passes on a cluster it could not reach"
        )
    else:
        ok(f"A-2 unanswered: all {len(rows)} questions got a yes/no from the live authorizer")

    # --- A-3 the positive half ---------------------------------------------------------------------
    pos = [r for r in rows if r["expected"] == "yes" and r["kind"] != "reader-read"]
    pos_bad = [r for r in pos if r["answer"] != "yes"]
    if pos_bad:
        detail = "; ".join(f"{r['sa']}: {q(r)} -> {r['answer'] or '<none>'}" for r in pos_bad[:6])
        bad(
            f"A-3 positive half: {len(pos_bad)} of {len(pos)} grants 06 REQUIRES are not held. "
            f"{detail}"
            + (f" (+{len(pos_bad) - 6} more)" if len(pos_bad) > 6 else "")
            + ". The missing-grant direction does not fail safe — 06 §2.2.1's own note is that a "
            "tier which loses a grant is bricked, and every negative arm below is satisfied by an "
            "identity that holds nothing at all"
        )
    else:
        ok(
            f"A-3 positive half: all {len(pos)} grants 06 requires are held by the identity that "
            f"is owed them"
        )

    # --- A-4 the negative half ----------------------------------------------------------------------
    neg = [r for r in rows if r["expected"] == "no"]
    neg_bad = [r for r in neg if r["answer"] == "yes"]
    if neg_bad:
        detail = "; ".join(f"{r['sa']} ({r['kind']}): {q(r)}" for r in neg_bad[:6])
        bad(
            f"A-4 negative half: {len(neg_bad)} of {len(neg)} verbs 06 withholds ARE HELD. {detail}"
            + (f" (+{len(neg_bad) - 6} more)" if len(neg_bad) > 6 else "")
            + ". Accept (e) is that no agent identity in the fleet holds a write verb, and 07 §2 "
            "makes Phase 9's whole claim that the machinery runs with no write authority anywhere"
        )
    else:
        ok(
            f"A-4 negative half: all {len(neg)} verbs 06 withholds are refused by the live "
            f"authorizer, for every identity in the fleet"
        )

    # --- A-5 append-only, by name -------------------------------------------------------------------
    ao = [r for r in rows if r["kind"] == "append-only"]
    ao_bad = [r for r in ao if r["answer"] == "yes"]
    if not ao:
        bad(
            "A-5 append-only: no identity was asked whether it can rewrite a record. V-BRK-013's "
            "first named property is unasked"
        )
    elif ao_bad:
        bad(
            f"A-5 append-only: {len(ao_bad)} identity/verb pair(s) CAN rewrite or remove an "
            f"ActionRecord: "
            + "; ".join(f"{r['sa']}: {r['verb']}" for r in ao_bad[:6])
            + ". An actor that can update a record can rewrite the evidence of what it did, which "
            "turns invariant 3 — every action is journalled — into a claim about a mutable log"
        )
    else:
        ok(
            f"A-5 append-only: {len(ao)} rewrite/remove question(s) all refused; the journal is "
            f"append-only for every actor identity swept"
        )

    # --- A-6 the freeze read, per tier ----------------------------------------------------------------
    freeze_tiers = {r["tier"] for r in rows if r["kind"] == "freeze-read"}
    freeze_bad = sorted(
        {r["tier"] for r in rows if r["kind"] == "freeze-read" and r["answer"] != "yes"}
    )
    if not freeze_tiers:
        bad(
            "A-6 freeze read: no tier was asked whether it can read fleetfreezes. 06 §4.4 makes an "
            "unreadable freeze object a permanent fail-closed, and V-BRK-013 names this clause "
            "separately for that reason"
        )
    elif freeze_bad:
        bad(
            f"A-6 freeze read: tier(s) {', '.join(freeze_bad)} cannot read fleetfreezes. That tier "
            f"fails closed permanently (06 §4.4) — dropping this grant does not fail safe, it "
            f"bricks the tier"
        )
    else:
        ok(
            f"A-6 freeze read: all {len(freeze_tiers)} tier(s) ({', '.join(sorted(freeze_tiers))}) "
            f"can read fleetfreezes, developer-team included"
        )

    # --- A-7 reader non-vacuity, the any-of ------------------------------------------------------------
    readers = {f"{r['ns']}/{r['sa']}" for r in rows if r["role"] == "reader"}
    mute = sorted(
        ident
        for ident in readers
        if not any(
            r["kind"] == "reader-read" and r["answer"] == "yes"
            for r in rows
            if f"{r['ns']}/{r['sa']}" == ident
        )
    )
    if not readers:
        bad(
            "A-7 reader non-vacuity: no reader identity was swept at all. The reader SA is the one "
            "the agent PROCESS runs as — the identity an LLM's output can actually reach — and an "
            "actor-only sweep leaves it unasked"
        )
    elif mute:
        bad(
            f"A-7 reader non-vacuity: reader identity/identities {', '.join(mute)} answered no to "
            f"every read probe, so they are bound to nothing and every reader-no-write row above "
            f"passed for the wrong reason"
        )
    else:
        ok(
            f"A-7 reader non-vacuity: all {len(readers)} reader identity/identities hold at least "
            f"one read, so their write denials are denials and not absence"
        )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
