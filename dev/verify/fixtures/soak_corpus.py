#!/usr/bin/env python3
"""The envelope population for the undo-coverage soak (V-REV-001 at L2).

09 §6.3 scopes V-REV-001 to "100% of executed non-gated ActionRecords". A percentage needs a
denominator, and today's denominator is **one**: `dev/verify/broker-execute-l2.sh` submits a single
hand-written envelope and its `results.csv` note says n=1 in as many words. 09 §11.11 keeps
V-REV-001 apart from V-REV-002 "precisely because the first is cheap and reassuring and the second
is the one that matters" -- and over one record the first is not even reassuring. This module builds
the population that makes it a measurement.

WHERE THE POPULATION COMES FROM, AND WHY NOT A NEW FIXTURE FILE

  `verification/fixtures/classifier-corpus.yaml` already holds 120-200 already-resolved envelopes
  (09 §7.1, floor enforced by dev/tests/classifier-corpus-lint.py), spanning the verbs, kinds and
  shapes a human thought were worth arguing about. Writing a second corpus beside it would be a
  second opinion about what an interesting action looks like, maintained by nobody, and LSN-031 is
  what two copies of one decision cost this build already.

  So the population is DERIVED. The risk of deriving is the opposite of the risk of listing: a
  listed corpus goes stale loudly, a derived one shrinks to nothing silently and presents the empty
  result as a clean run. That is what `--self-test` is for, and why its floors are checked against a
  synthesised empty selection as well as the real one -- a floor that has never fired is
  indistinguishable from a floor whose threshold is wrong.

WHAT `expect.class` IS USED FOR, AND THE ONE THING IT MUST NOT BE USED FOR

  Used for: selecting the NON-GATED classes, because that is the population 09 §6.3 scopes the check
  to. `gated` never executes and `forbidden` never reaches the planner.

  NOT used for: predicting what the live broker will decide. The corpus cases are inputs already
  resolved against a fixture world; the live broker classifies against production namespace labels,
  live object state and a real cluster's seen/novel history. An assertion that the live class equals
  `expect.class` would be a second V-MET-005 wearing V-REV-001's ID, and it would go red for reasons
  that say nothing at all about undo coverage. The soak reads back the class the broker CHOSE and
  partitions on that; this module only decides which envelopes are worth submitting.

  The same reasoning covers the namespace. Every selected case is re-addressed to the one tenant
  namespace the suite owns, which can move a case across the production ladder. That is fine for the
  same reason: the class is read, not predicted.

WHAT AUTHORIZES A CASE

  `dev/verify/fixtures/actor-tenant-write-grant.yaml`, the shipped test-only write overlay, read as
  the file it is rather than summarised into a kind list here. Subresource granularity included:
  `deployments/scale` is its own rule, so revoking it must drop the scale cases and leave the
  `patch Deployment` cases standing. A hardcoded `{ConfigMap, Deployment}` would pass every test on
  the day it was written and stop tracking the grant the first time the grant moved.

  The RBAC verbs a case needs are read off what the executor actually calls, not off the envelope
  verb's name (k8s-operator/internal/broker/execute/client.go):

    apply, create -> Applier.Apply -> a server-side-apply Patch      -> patch + create
    patch         -> Applier.Patch -> a raw Patch                    -> patch
    scale         -> SubResource("scale").Update                     -> update on <res>/scale
    delete        -> Applier.Delete                                  -> delete

  `get` is deliberately absent: every op snapshots its pre-state first, but that read is the READ
  overlay's business (`actor-tenant-grant.yaml`), and asking the write overlay for it would reject
  every case for the wrong reason.

WHAT `seed` MEANS

  Derived from the verb, because the suite has to put the cluster in the state the verb needs before
  it submits anything:

    patch, scale, delete -> present   the object must exist. A patch of a missing object is a 404,
                                      and a delete of one is a no-op the executor handles explicitly
                                      and which produces an empty diff -- so an unseeded delete
                                      reports a record with no undo plan and V-REV-001 goes red for
                                      a reason that belongs to the fixture.
    apply, create        -> absent    server-side apply works either way; leaving the object absent
                                      makes the undo plan a delete step rather than a restore, which
                                      is the arm with fewer callers.

Modes:
    soak_corpus.py --table        every selected case, TSV, for the shell suite
    soak_corpus.py --rejects      every rejected case and its one reason, TSV
    soak_corpus.py --self-test    the derivation's own non-vacuity, no cluster

Importable too: `derive()` is what dev/verify/fixtures/undo_coverage_probe.py will call, so the
suite and the probe cannot disagree about which cases are in the run.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
CORPUS = REPO / "verification" / "fixtures" / "classifier-corpus.yaml"
GRANT = REPO / "dev" / "verify" / "fixtures" / "actor-tenant-write-grant.yaml"

sys.path.insert(0, str(REPO / "dev" / "tests"))
from yamlsubset import load_corpus  # noqa: E402

# The envelope schema's executable verbs. Mirrors VALID_OPS in
# agents/platform/scripts/action_envelope.py; a verb outside it is rejected by name below rather
# than dropped, so the corpus's `cloud` cases show up in the reject table instead of vanishing.
EXECUTABLE_VERBS = ("apply", "create", "patch", "scale", "delete")

# What the executor's own client calls, per envelope verb: (rbac verbs, subresource).
RBAC_FOR_VERB: dict[str, tuple[tuple[str, ...], str]] = {
    "apply": (("patch", "create"), ""),
    "create": (("patch", "create"), ""),
    "patch": (("patch",), ""),
    "delete": (("delete",), ""),
    "scale": (("update",), "scale"),
}

SEED_FOR_VERB: dict[str, str] = {
    "apply": "absent",
    "create": "absent",
    "patch": "present",
    "scale": "present",
    "delete": "present",
}

# Kind -> resource. A naming convention, NOT a policy statement: what a case is allowed to do still
# comes entirely from the grant, and a Kind missing from this map is rejected as `not-authorized`,
# which is the conservative answer. The self-test closes the silent-shrink hole in the other
# direction -- every resource the grant names must be reachable from some Kind here, so adding
# `ingresses` to the overlay fails this file until it learns `Ingress`.
KIND_TO_RESOURCE: dict[str, str] = {
    "ConfigMap": "configmaps",
    "Deployment": "deployments",
}

NON_GATED_CLASSES = ("routine", "elevated")

# The closed set of reasons a case can be left out. Closed on purpose: the reject table has to
# account for every case the corpus holds, so a shrinking population is visible as a reason
# histogram that moved rather than as a smaller number with no explanation.
REJECT_REASONS = (
    "duplicate-id",
    "abort",
    "class-unstated",
    "class-gated",
    "class-forbidden",
    "multi-op",
    "verb-not-executable",
    "unnamed-target",
    "multi-object",
    "not-authorized",
)

# Vacuity floors. Deliberately below the measured yield rather than equal to it -- 09 §7.1 lets the
# classifier corpus grow and shrink between 120 and 200 cases, and a floor pinned to today's exact
# number is a floor that fails on somebody else's legitimate edit. Low enough not to be brittle,
# high enough that n=1 -- the thing this whole unit exists to fix -- cannot pass.
MIN_SELECTED = 20
MIN_VERBS = 3
MIN_KINDS = 2

COLUMNS = (
    "id",
    "class",
    "verb",
    "group",
    "kind",
    "resource",
    "subresource",
    "rbacVerbs",
    "target",
    "seed",
    "srcNs",
)


class CorpusDerivationError(Exception):
    """The inputs are not the shape this module reads. Loud, never a smaller population."""


# -- the grant -----------------------------------------------------------------------------------

_RULE = re.compile(
    r"^[ \t]*-[ \t]*apiGroups:[ \t]*\[(?P<groups>[^\]]*)\][ \t]*\n"
    r"^[ \t]*resources:[ \t]*\[(?P<resources>[^\]]*)\][ \t]*\n"
    r"^[ \t]*verbs:[ \t]*\[(?P<verbs>[^\]]*)\][ \t]*$",
    re.M,
)


def _flow_list(text: str) -> list[str]:
    return [w.strip().strip("\"'") for w in text.split(",") if w.strip()]


def load_grant(text: str) -> set[tuple[str, str, str]]:
    """Read the write overlay into a set of (group, resource, verb) triples.

    A narrow reader, not a YAML parser. dev/tests/yamlsubset.py cannot be used here and refusing to
    is the right call rather than a limitation: it rejects flow collections by design, and this file
    is written in them (`verbs: ["create", "update", ...]`), because it is an RBAC manifest that a
    human applies and reads, not a corpus prettier reformats.

    The one property that matters is that a rule this reader does not understand is an ERROR. A
    silently-skipped rule narrows the authorized set, which narrows the corpus, which shrinks
    V-REV-001's denominator -- the exact failure this module is built to prevent, arriving through
    its own input reader. So the count of `- apiGroups:` lines must equal the count of triples
    matched: a rule rewritten in block style halts here instead of quietly dropping its resources.
    """
    declared = len(re.findall(r"^[ \t]*-[ \t]*apiGroups:", text, re.M))
    rules = list(_RULE.finditer(text))
    if declared != len(rules):
        raise CorpusDerivationError(
            f"{GRANT.name} declares {declared} RBAC rule(s) and this reader understood "
            f"{len(rules)}. It reads the three-line flow-sequence form only; a rule written any "
            f"other way would be skipped, and a skipped rule silently shrinks the soak population"
        )
    if not rules:
        raise CorpusDerivationError(f"{GRANT.name} yielded no RBAC rules at all")

    out: set[tuple[str, str, str]] = set()
    for m in rules:
        for g in _flow_list(m.group("groups")):
            for r in _flow_list(m.group("resources")):
                for v in _flow_list(m.group("verbs")):
                    out.add((g, r, v))
    return out


def grant_resources(grant: set[tuple[str, str, str]]) -> set[str]:
    """Every resource the grant names, subresources stripped."""
    return {r.split("/", 1)[0] for _, r, _ in grant}


# -- the derivation ------------------------------------------------------------------------------


def _authorized(grant: set[tuple[str, str, str]], group: str, resource: str, sub: str, verbs: tuple[str, ...]) -> bool:
    res = f"{resource}/{sub}" if sub else resource
    return all((group, res, v) in grant for v in verbs)


def derive(
    corpus_text: str | None = None,
    grant_text: str | None = None,
) -> tuple[list[tuple[str, ...]], list[tuple[str, str]], int]:
    """Return (selected rows, [(case id, reason)], total cases seen).

    The reasons are evaluated in the order of REJECT_REASONS and the first match wins, so a case
    that is both gated and unnamed is reported as gated -- the classification reason is the one a
    reader wants, and reporting two would break the histogram's arithmetic.
    """
    doc = load_corpus(CORPUS.read_text() if corpus_text is None else corpus_text)
    grant = load_grant(GRANT.read_text() if grant_text is None else grant_text)

    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CorpusDerivationError("the classifier corpus has no `cases` list")

    selected: list[tuple[str, ...]] = []
    rejected: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    for case in cases:
        cid = str(case.get("id") or "")
        if not cid:
            raise CorpusDerivationError("a corpus case has no id; it cannot be named in a reject table")
        if cid in seen_ids:
            rejected.append((cid, "duplicate-id"))
            continue
        seen_ids.add(cid)

        expect = case.get("expect") or {}
        ops = case.get("ops") or []

        if expect.get("abort"):
            rejected.append((cid, "abort"))
            continue
        klass = expect.get("class")
        if klass is None:
            rejected.append((cid, "class-unstated"))
            continue
        if klass not in NON_GATED_CLASSES:
            rejected.append((cid, f"class-{klass}"))
            continue
        if len(ops) != 1:
            rejected.append((cid, "multi-op"))
            continue

        op = ops[0]
        verb = op.get("verb")
        if verb not in EXECUTABLE_VERBS:
            rejected.append((cid, "verb-not-executable"))
            continue
        if not op.get("name"):
            rejected.append((cid, "unnamed-target"))
            continue
        if int(op.get("objects") or 1) > 1 or op.get("fraction") is not None:
            rejected.append((cid, "multi-object"))
            continue

        group = op.get("group") or ""
        kind = op.get("kind") or ""
        resource = KIND_TO_RESOURCE.get(kind, "")
        rbac, sub = RBAC_FOR_VERB[verb]
        if not resource or not _authorized(grant, group, resource, sub, rbac):
            rejected.append((cid, "not-authorized"))
            continue

        selected.append(
            (
                cid,
                klass,
                verb,
                group,
                kind,
                resource,
                sub,
                ",".join(rbac),
                f"soak-{cid}",
                SEED_FOR_VERB[verb],
                str(op.get("namespace") or ""),
            )
        )

    return selected, rejected, len(cases)


# -- the floors ----------------------------------------------------------------------------------


def floor_problems(
    selected: list[tuple[str, ...]],
    rejected: list[tuple[str, str]],
    total: int,
    grant: set[tuple[str, str, str]],
) -> list[str]:
    """Everything wrong with a derivation, as a list. A pure function of its arguments so the
    self-test can run it over a SYNTHESISED empty selection and require it to complain -- a floor
    that has never been observed to fire is a floor whose threshold is a guess."""
    problems: list[str] = []

    if len(selected) < MIN_SELECTED:
        problems.append(
            f"the soak population is {len(selected)} case(s), below the floor of {MIN_SELECTED}. "
            f"V-REV-001 is a percentage; over a handful of records it is a rounding artifact, and "
            f"the n=1 it already has is what this corpus exists to replace"
        )

    verbs = {r[2] for r in selected}
    if len(verbs) < MIN_VERBS:
        problems.append(
            f"only {len(verbs)} distinct verb(s) selected ({sorted(verbs)}); the undo planner takes "
            f"a different path per verb, and a soak over one verb measures one of them"
        )

    kinds = {r[4] for r in selected}
    if len(kinds) < MIN_KINDS:
        problems.append(f"only {len(kinds)} distinct kind(s) selected ({sorted(kinds)}), below {MIN_KINDS}")

    classes = {r[1] for r in selected}
    for want in NON_GATED_CLASSES:
        if want not in classes:
            problems.append(
                f"no '{want}' case selected; 09 §6.3 scopes V-REV-001 to the non-gated population "
                f"and both non-gated classes execute"
            )

    seeds = {r[9] for r in selected}
    for want in ("present", "absent"):
        if want not in seeds:
            problems.append(
                f"no case seeds '{want}'; the undo plan for a verb against an existing object and "
                f"against a missing one are different plans, and only one of them is being measured"
            )

    targets = [r[8] for r in selected]
    if len(set(targets)) != len(targets):
        dupes = sorted({t for t in targets if targets.count(t) > 1})
        problems.append(
            f"target name collision: {dupes}. Two cases seeding one object means the second "
            f"overwrites the first's pre-state, and both undo plans are then written against a "
            f"world neither case set up"
        )

    # The reject table has to account for the whole corpus, and every reason has to be one this
    # module admits to. Either failure is the population shrinking without saying why.
    if len(selected) + len(rejected) != total:
        problems.append(
            f"{len(selected)} selected + {len(rejected)} rejected != {total} cases in the corpus; "
            f"some case was dropped by neither path"
        )
    unknown = sorted({r for _, r in rejected} - set(REJECT_REASONS))
    if unknown:
        problems.append(f"reject reason(s) outside the closed set: {unknown}")

    # The silent-shrink hole in the Kind map: a resource the grant authorizes and no Kind reaches is
    # authority the corpus cannot use, and nothing else in this file would notice.
    unreachable = sorted(grant_resources(grant) - set(KIND_TO_RESOURCE.values()))
    if unreachable:
        problems.append(
            f"the write overlay grants {unreachable} and no Kind in KIND_TO_RESOURCE maps to it, so "
            f"no corpus case can ever use that authority. Add the Kind or narrow the grant"
        )

    return problems


# -- self-test -----------------------------------------------------------------------------------

_MINI_GRANT = """
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["update", "patch"]
"""


def _case(cid: str, verb: str, kind: str, group: str = "", klass: str = "routine", **op_extra) -> str:
    op = [f"        verb: {verb}", f"        kind: {kind}", "        namespace: team-a", "        name: t"]
    if group:
        op.insert(1, f"        group: {group}")
    for k, v in op_extra.items():
        op.append(f"        {k}: {v}")
    body = "\n".join(op)
    return (
        f"  - id: {cid}\n"
        f"    description: synthetic\n"
        f"    ops:\n"
        f"      -{body[7:]}\n"
        f"    expect:\n"
        f"      class: {klass}\n"
    )


def _mini(*cases: str) -> str:
    return "cases:\n" + "".join(cases)


def _self_test() -> int:
    checks: list[tuple[bool, str]] = []

    def arm(ok: bool, label: str) -> None:
        checks.append((bool(ok), label))

    grant = load_grant(GRANT.read_text())
    selected, rejected, total = derive()
    sel_ids = {r[0] for r in selected}

    # 1. The real derivation clears its own floors.
    problems = floor_problems(selected, rejected, total, grant)
    arm(not problems, "the real corpus clears every floor" + ("" if not problems else f" -- {problems[0]}"))

    # 2. The floors bite. An empty selection MUST fail them; otherwise arm 1 proves nothing.
    empty = floor_problems([], [], 0, grant)
    arm(len(empty) >= 5, f"an empty selection trips the floors ({len(empty)} problem(s) raised, want >= 5)")

    # 3. A colliding target trips the uniqueness floor, which nothing in the real corpus exercises.
    clash = list(selected[:1]) * 2
    arm(
        any("collision" in p for p in floor_problems(clash, [], 2, grant)),
        "two cases sharing a target name trip the collision floor",
    )

    # 4. Revoking `deployments` drops every Deployment case that rides on that rule, and keeps the
    #    ConfigMap ones. This is what makes the filter grant-derived rather than a kind list with
    #    extra steps. The scale case is deliberately NOT expected to drop: it is authorized by
    #    `deployments/scale`, a rule this arm leaves alone, and an arm that expected it to disappear
    #    would be asserting that the reader ignores subresources.
    no_deploy = _MINI_GRANT.replace('resources: ["deployments"]', 'resources: ["replicasets"]')
    s2, _, _ = derive(grant_text=no_deploy)
    d_before = {r[0] for r in selected if r[4] == "Deployment" and r[2] != "scale"}
    d_after = {r[0] for r in s2 if r[4] == "Deployment" and r[2] != "scale"}
    arm(d_before and not d_after, f"revoking `deployments` drops all {len(d_before)} non-scale Deployment case(s)")
    arm(
        {r[0] for r in s2 if r[2] == "scale"} == {r[0] for r in selected if r[2] == "scale"},
        "and leaves the scale case, which rides on the `deployments/scale` rule",
    )
    cm_before = {r[0] for r in selected if r[4] == "ConfigMap"}
    arm(
        cm_before and cm_before == {r[0] for r in s2 if r[4] == "ConfigMap"},
        "revoking `deployments` leaves the ConfigMap cases untouched",
    )

    # 5. Subresource granularity: `deployments/scale` is its own rule.
    no_scale = _MINI_GRANT.replace('resources: ["deployments/scale"]', 'resources: ["statefulsets/scale"]')
    s3, _, _ = derive(grant_text=no_scale)
    arm(not any(r[2] == "scale" for r in s3), "revoking `deployments/scale` drops the scale case(s)")
    arm(
        any(r[2] == "patch" and r[4] == "Deployment" for r in s3),
        "revoking `deployments/scale` leaves `patch Deployment` standing",
    )

    # 6. Verb granularity: a read-only grant authorizes no writes at all.
    read_only = re.sub(r'verbs: \[[^\]]*\]', 'verbs: ["get", "list", "watch"]', _MINI_GRANT)
    s4, r4, _ = derive(grant_text=read_only)
    arm(not s4, f"a get/list/watch grant selects nothing (selected {len(s4)})")
    now = dict(r4)
    wrong = sorted(cid for cid in sel_ids if now.get(cid) != "not-authorized")
    arm(
        not wrong,
        "and every case it dropped says `not-authorized`, not something vaguer"
        + ("" if not wrong else f" -- {[(c, now.get(c)) for c in wrong[:3]]}"),
    )

    # 7. The class filter. A corpus of gated and forbidden cases is a corpus of nothing.
    s5, r5, t5 = derive(
        corpus_text=_mini(
            _case("syn-g", "patch", "ConfigMap", klass="gated"),
            _case("syn-f", "delete", "ConfigMap", klass="forbidden"),
        ),
        grant_text=_MINI_GRANT,
    )
    arm(not s5 and t5 == 2, "a gated+forbidden corpus selects nothing")
    arm(
        sorted(r for _, r in r5) == ["class-forbidden", "class-gated"],
        f"and names the class as the reason ({sorted(r for _, r in r5)})",
    )

    # 8. A verb outside the envelope schema is REJECTED BY NAME. The corpus really holds six of
    #    these (`cloud`), and a silent drop is how a derived population shrinks unnoticed.
    arm(
        any(reason == "verb-not-executable" for _, reason in rejected),
        "the corpus's non-envelope verbs land in the reject table by name",
    )

    # 9. Seed derivation, as a table. An edit to SEED_FOR_VERB that reverses a row would otherwise
    #    only show up as a 404 in the L2 suite, attributed to the broker.
    want = {"apply": "absent", "create": "absent", "patch": "present", "scale": "present", "delete": "present"}
    s6, _, _ = derive(
        corpus_text=_mini(
            _case("syn-ap", "apply", "ConfigMap"),
            _case("syn-cr", "create", "ConfigMap"),
            _case("syn-pa", "patch", "ConfigMap"),
            _case("syn-de", "delete", "ConfigMap"),
            _case("syn-sc", "scale", "Deployment", group="apps"),
        ),
        grant_text=_MINI_GRANT,
    )
    got = {r[2]: r[9] for r in s6}
    arm(got == want, f"every verb's seed state is what the executor needs ({got})")

    # 10. multi-object and unnamed cases are excluded, and the corpus has some of each.
    reasons = {r for _, r in rejected}
    arm("multi-object" in reasons or "unnamed-target" in reasons, "blast-radius and unnamed cases are excluded by name")

    # 11. The grant reader refuses what it does not understand rather than skipping it.
    block_style = _MINI_GRANT.replace('resources: ["configmaps"]', "resources:\n      - configmaps")
    try:
        load_grant(block_style)
        arm(False, "a block-style RBAC rule halts the reader")
    except CorpusDerivationError:
        arm(True, "a block-style RBAC rule halts the reader instead of being skipped")

    # 12. Duplicate ids are caught, not merged.
    _, r7, _ = derive(
        corpus_text=_mini(_case("syn-d", "patch", "ConfigMap"), _case("syn-d", "delete", "ConfigMap")),
        grant_text=_MINI_GRANT,
    )
    arm([r for _, r in r7] == ["duplicate-id"], "a duplicate case id is rejected, not silently deduplicated")

    failed = [label for ok, label in checks if not ok]
    for ok, label in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if failed:
        print(f"FAIL: {len(failed)}/{len(checks)} arms failed", file=sys.stderr)
        return 1

    hist: dict[str, int] = {}
    for _, reason in rejected:
        hist[reason] = hist.get(reason, 0) + 1
    print(
        f"PASS: {len(checks)}/{len(checks)} arms. The soak population is {len(selected)} envelope(s) "
        f"of {total} corpus cases -- "
        f"{len({r[2] for r in selected})} verb(s), {len({r[4] for r in selected})} kind(s), "
        f"classes {sorted({r[1] for r in selected})}, "
        f"seeds {sorted({r[9] for r in selected})}. "
        f"Excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(hist.items()))
    )
    print(
        "SELF-TEST DOES NOT EXERCISE: whether the live broker executes any of these, what class it "
        "assigns them, whether the seeded objects can be created, or whether an undo plan is "
        "generated. This file decides which envelopes are worth submitting and nothing else; "
        "V-REV-001 is claimed by dev/verify/undo-coverage-l2.sh (P9-T8b-4b-ii-2b-ii-b), on a "
        "cluster. (LSN-060)"
    )
    return 0


def main() -> int:
    args = sys.argv[1:]
    try:
        selected, rejected, total = derive()
    except CorpusDerivationError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    if "--self-test" in args:
        return _self_test()

    if "--rejects" in args:
        print("#id\treason")
        for cid, reason in rejected:
            print(f"{cid}\t{reason}")
        return 0

    if "--table" in args:
        print("#" + "\t".join(COLUMNS))
        print(f"#total\t{total}\t#selected\t{len(selected)}\t#rejected\t{len(rejected)}")
        for row in selected:
            print("\t".join(row))
        return 0

    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    print("usage: soak_corpus.py --table | --rejects | --self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
