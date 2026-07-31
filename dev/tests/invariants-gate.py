#!/usr/bin/env python3
"""The mechanical half of `.claude/harness/invariants.md` (P8-T6).

The gate was thirteen numbered paragraphs and a promise to read them. A checklist enforces nothing:
it is re-read by whoever is about to violate it, at the moment they are most motivated to interpret
it favourably. This script makes four of the thirteen — and two of the unnumbered repo-mechanics
rules under them — fail a build.

    7    authority never precedes machinery   -> check_write_verbs_have_machinery
    8    tests are replaced, never deleted    -> check_assertion_ratchet   (V-MET-003)
    12   deferrals name an external blocker   -> check_deferrals_name_blockers (V-MET-006, LSN-008)
    13   every failure leaves a lesson        -> check_closed_lessons_are_executable (LSN-019)
    §DTG destructive-test guard stays anchored -> check_destructive_guards_are_anchored (LSN-005)
    §RM  build targets name their cluster    -> check_make_targets_are_context_explicit (LSN-018)

Two more checks guard the gate itself rather than an invariant: retired IDs keep a replacement
pointer (V-MET-004) and the L0 chain stays runnable and wired to CI — a chain nothing runs is the
same as no chain, and this script is IN that chain.

The other nine invariants are judgements about intent that a script cannot make. They stay in
invariants.md and are answered per PR. Mechanizing four is not "the gate is done" — it is four fewer
places where being tired is the same as being dishonest.

Exit 0 = every check passed. Exit 1 = at least one failed. Exit 2 = the gate could not run (a
corpus file vanished, the baseline is unreadable). Exit 2 is deliberately distinct: "the gate broke"
must never be silently equivalent to "the gate passed", which is the failure LSN-020 was written for.

Run: python3 dev/tests/invariants-gate.py [--update-baseline]
"""

from __future__ import annotations

import ast
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitcorpus import read_repo_files, repo_files  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LESSONS = REPO / ".claude/harness/LESSONS.md"
LEDGER = REPO / "docs/build/LEDGER.md"
# The verification evidence corpus. It was one markdown table inside LEDGER.md until 2026-07-26,
# when the by-check-ID rows moved to a CSV and the Phases 0-7 suite-level log moved to the archive.
# All three are listed because the question the gate asks of them -- "is this check green anywhere?"
# -- is about the corpus, not about the file it currently sits in. Splitting the corpus without
# telling the gate is how a check goes quietly VACUOUS; see _verification_evidence_rows.
LEDGER_ARCHIVE = REPO / "docs/build/archive/LEDGER-phases-0-7.md"
RESULTS_CSV = REPO / "verification/results.csv"
L0_CHAIN = REPO / "dev/L0-CHAIN.txt"
L2_CHAIN = REPO / "dev/L2-CHAIN.txt"
WORKFLOWS = REPO / ".github/workflows"
BASELINE = REPO / "dev/assertion-baseline.json"
# The conformance spec. Check IDs, their levels, and the phase each is required by all live here.
SPEC = REPO / "docs/design/09-verification-and-validation.md"

# ---------------------------------------------------------------------------------------------
# Invariant 7 — authority never precedes machinery
# ---------------------------------------------------------------------------------------------

# The verbs an agent identity may hold. This is an ALLOW-LIST, copied deliberately from the CEL in
# examples/gitops-repo/clusters/cluster-a/bootstrap/20-policy/vap-agent-readonly.yaml rather than
# re-derived: a deny-list of "write verbs" is how LSN-004 happened -- `escalate`, `bind` and
# `impersonate` are not writes and are worse than most writes.
READ_VERBS = {"get", "list", "watch"}

# What makes a Role an AGENT identity rather than the operator's own. Same predicate the cluster's
# admission policy uses (`is-agent-rbac`: 'kube-agents/tier' in object.metadata.labels), so the gate
# and the runtime enforcement agree by construction instead of by two independent guesses. The
# operator's own role legitimately writes -- it creates Deployments -- and carries no tier label.
TIER_LABEL = "kube-agents/tier"

# The machinery that must exist before any agent identity may hold a write verb (07 §5, 03 §6).
# Probed against the tree rather than described in prose so that this check STOPS failing on its own
# the moment the machinery actually lands -- a gate that needs hand-editing to notice progress gets
# hand-edited to pass.
#
# DISCOVERED BY GLOB, NOT BY A LIST OF PATHS, and that is [[LSN-038]]. The first draft named two
# candidate directories per item. Both guesses for the classifier (`internal/classifier`,
# `internal/risk`) and both for undo (`internal/undo`, `internal/broker/undo.go`) were wrong by the
# time the code existed: P9-T3a put the classifier at `internal/broker/classify/` and P9-T5b put undo
# at `internal/broker/undo/`. The probe therefore reported two of four absent for six merged units,
# and NOTHING NOTICED -- because a false "absent" only makes this check stricter, and no agent
# identity had a write verb yet to be strict about. The bill arrives at the unit that first adds one:
# a gate that has been lying in the safe direction goes red on correct code, and the one-line green
# is to edit the list, which is [[LSN-036]] exactly.
#
# Each entry: (human name, glob patterns, what proves it is real rather than an empty directory).
MACHINERY = [
    (
        "Action Broker",
        ["k8s-operator/internal/**/broker", "k8s-operator/internal/**/actionbroker"],
        None,
    ),
    (
        # Package directories, not `classif*` — a bare stem also matches
        # `internal/router/classify.go`, which classifies chat intents and has nothing to do with
        # action risk. A probe that resolves against the wrong subsystem is worse than one that
        # resolves against nothing: it reports the machinery present and would let a write verb
        # through on the strength of the router.
        "risk classifier",
        [
            "k8s-operator/internal/**/classify",
            "k8s-operator/internal/**/classifier",
            "k8s-operator/internal/**/risk",
        ],
        None,
    ),
    (
        "ActionRecord journal",
        [
            "k8s-operator/api/**/actionrecord_types.go",
            "k8s-operator/internal/journal",
        ],
        None,
    ),
    (
        "undo path",
        ["k8s-operator/internal/**/undo", "k8s-operator/internal/**/undo.go"],
        None,
    ),
]

# Machinery that has NOT been built yet must be declared here, `name -> the phase that builds it`,
# and the declaration is checked against the ledger's current phase. Absence is then a stated fact
# with an expiry rather than an inference from a path that may simply be wrong, which is the half of
# [[LSN-038]] a better glob does not fix: the four probes were wrong in the direction that produces
# no output at all. Empty today because 07 §5 orders all four before Phase 10 and all four exist.
UNBUILT_UNTIL_PHASE: dict[str, int] = {}

RULES_BLOCK = re.compile(r"^\s*rules:\s*$")
VERBS_LINE = re.compile(r"^\s*(?:-\s*)?verbs:\s*(\[.*\]|)\s*$")
LIST_ITEM = re.compile(r"^\s*-\s*[\"']?([a-zA-Z*]+)[\"']?\s*$")


def _yaml_docs(text: str) -> list[str]:
    """Split on `---` at column 0. Good enough: we only need per-document label+verb locality."""
    return re.split(r"^---\s*$", text, flags=re.MULTILINE)


def _verbs_in(doc: str) -> list[tuple[int, str]]:
    """Every verb granted in a document, with its 1-based line number within that document."""
    out: list[tuple[int, str]] = []
    lines = doc.splitlines()
    for i, line in enumerate(lines):
        m = VERBS_LINE.match(line)
        if not m:
            continue
        inline = m.group(1)
        if inline:
            for v in re.findall(r"[\"']?([a-zA-Z*]+)[\"']?", inline):
                if v:
                    out.append((i + 1, v))
            continue
        # Block form: consume the following `- verb` items.
        for nxt in lines[i + 1 :]:
            item = LIST_ITEM.match(nxt)
            if not item:
                break
            out.append((i + 1, item.group(1)))
    return out


def agent_rbac_documents() -> list[tuple[Path, int, str]]:
    """(file, doc index, document text) for every Role/ClusterRole that is an agent identity.

    Scans templates too. A verb that only appears after envsubst is still a granted verb, and the
    templates are what provisioning actually applies.
    """
    found = []
    for path in sorted(REPO.rglob("*.yaml")) + sorted(REPO.rglob("*.yaml.template")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith((".git/", "node_modules/", "docs/site/node_modules/")):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "kind: Role" not in text and "kind: ClusterRole" not in text:
            continue
        for n, doc in enumerate(_yaml_docs(text)):
            if not re.search(r"^kind:\s*(Cluster)?Role\s*$", doc, re.MULTILINE):
                continue
            if TIER_LABEL not in doc:
                continue
            found.append((path, n, doc))
    return found


def _go_files(root: Path) -> tuple[list[Path], list[Path]]:
    """(non-test .go files, _test.go files) under a path that may be a file or a directory."""
    if root.is_file():
        siblings = sorted(root.parent.glob("*.go"))
    else:
        siblings = sorted(root.rglob("*.go"))
    return (
        [p for p in siblings if not p.name.endswith("_test.go")],
        [p for p in siblings if p.name.endswith("_test.go")],
    )


def discover_machinery(name: str) -> tuple[list[Path], str | None]:
    """Find one machinery item in the tree. Returns (paths that qualify, why nothing did).

    Invariant 7 asks for machinery that "exist**s** and **is tested**", so both halves are probed:
    a match must carry at least one non-test `.go` file declaring a function and at least one
    `_test.go` declaring a `func Test`. A directory holding a doc.go and a TODO is not the undo
    path, and a package with no test is not machinery this invariant will accept -- an agent with
    write RBAC and an untested undo path is the situation the invariant names in its own sentence.
    """
    entry = next((m for m in MACHINERY if m[0] == name), None)
    if entry is None:
        return [], f"{name!r} is not a declared machinery item"
    _, globs, _ = entry
    candidates = sorted({p for g in globs for p in REPO.glob(g)})
    if not candidates:
        return [], f"no path in the tree matches any of {globs}"
    qualified, why = [], []
    for path in candidates:
        src, tests = _go_files(path)
        rel = path.relative_to(REPO).as_posix()
        if not any(re.search(r"^func ", p.read_text(), re.MULTILINE) for p in src):
            why.append(f"{rel} has no non-test Go function")
            continue
        if not any(re.search(r"^func Test", p.read_text(), re.MULTILINE) for p in tests):
            why.append(f"{rel} has Go code but no `func Test` beside it")
            continue
        qualified.append(path)
    return qualified, None if qualified else "; ".join(why)


def missing_machinery() -> list[str]:
    return [name for name, _, _ in MACHINERY if not discover_machinery(name)[0]]


def check_machinery_probes_resolve() -> list[str]:
    """[[LSN-038]]. The probe's own answer is a claim, and it is checked before it is used.

    `check_write_verbs_have_machinery` consults `missing_machinery()` only when a write verb turns
    up, so a probe that cannot find code which is merged and tested reads as "stricter than needed"
    and produces no output at all -- for as long as it takes someone to add the first write verb.
    That is six units in this repo's case. Here the same answer is asked for on every run, and an
    unresolvable probe must be DECLARED in UNBUILT_UNTIL_PHASE with the phase that ends it.

    Deliberately not "the machinery must exist": during Phases 0-7 it correctly did not. What may
    not happen is machinery being absent *silently*.
    """
    failures = []
    current = _current_phase()
    for name, globs, _ in MACHINERY:
        found, why = discover_machinery(name)
        if found:
            if name in UNBUILT_UNTIL_PHASE:
                failures.append(
                    f"machinery {name!r} resolves to {[p.relative_to(REPO).as_posix() for p in found]} "
                    f"but is still declared unbuilt until phase {UNBUILT_UNTIL_PHASE[name]}. Drop "
                    f"the declaration; a stale one hides the next probe that goes wrong."
                )
            continue
        if name not in UNBUILT_UNTIL_PHASE:
            failures.append(
                f"machinery {name!r} is not discoverable ({why}), and is not declared in "
                f"UNBUILT_UNTIL_PHASE. Either it moved and the globs {globs} no longer find it — "
                f"which makes invariant 7 fail on correct code the day someone adds a write verb — "
                f"or it does not exist yet and the declaration is missing."
            )
        elif current is not None and current >= UNBUILT_UNTIL_PHASE[name]:
            failures.append(
                f"machinery {name!r} is declared unbuilt until phase {UNBUILT_UNTIL_PHASE[name]} "
                f"and the ledger says the build is in phase {current}, but nothing in the tree "
                f"matches {globs} ({why}). Either that phase closed without building it, or it was "
                f"built somewhere the probe cannot see."
            )
    return failures


CURRENT_PHASE = re.compile(r"^\|\s*Current phase\s*\|.*?Phase\s+(\d+)", re.MULTILINE)


def _current_phase() -> int | None:
    """The phase number from the ledger's Status table, or None if it cannot be read.

    None is not a pass: it only suppresses the phase-expiry arm, and the "declared or discoverable"
    arm above runs regardless.
    """
    if not LEDGER.exists():
        return None
    m = CURRENT_PHASE.search(LEDGER.read_text())
    return int(m.group(1)) if m else None


def check_write_verbs_have_machinery() -> list[str]:
    """Invariant 7. An agent with write RBAC and no journal is worse than either system.

    Whole-tree, not diff-vs-base. A diff check answers "did THIS PR add one", which reads green for
    a write verb that reached main while the gate was not wired up -- and this gate was not wired up
    until today, so that is not hypothetical. The state check has no base-ref dependency and cannot
    be satisfied by merging in two steps.
    """
    failures = []
    docs = agent_rbac_documents()
    if not docs:
        return [
            "VACUOUS: no agent-identity RBAC found at all. Either the tree lost its tier labels or "
            f"the '{TIER_LABEL}' discriminator no longer matches the VAP. Check "
            "examples/gitops-repo/clusters/cluster-a/bootstrap/20-policy/vap-agent-readonly.yaml."
        ]

    absent = missing_machinery()
    for path, n, doc in docs:
        rel = path.relative_to(REPO).as_posix()
        for _line, verb in _verbs_in(doc):
            if verb in READ_VERBS:
                continue
            if not absent:
                # Machinery exists; invariant 7 is satisfied and the verb is a Phase 10+ decision
                # that other checks (V-BRK, V-REV) own. Not this gate's call.
                continue
            failures.append(
                f"{rel} (doc {n}) grants agent identity the verb '{verb}', but the machinery that "
                f"makes a write survivable does not exist: {', '.join(absent)}. "
                f"07 §5 — authority never precedes machinery."
            )
    return failures


# ---------------------------------------------------------------------------------------------
# Invariant 8 / V-MET-003 — the assertion ratchet
# ---------------------------------------------------------------------------------------------
#
# The unit of the ratchet is the NAMED TEST, not the `assert` statement. "Tests are replaced, never
# deleted" is a statement about tests; counting raw asserts would fail on an honest refactor that
# folds three into a loop, and a gate that fires on honest work is a gate that gets disabled. A
# named test disappearing is unambiguous.
#
# Scope note. 09 §8 scopes V-MET-003 to V-CTN/V-BRK/V-REV/V-ADV. Only one of those suites
# (V-CTN-020) has any implementation today, so a ratchet honouring that scope literally would guard
# one check and read green forever -- V-MET-014, "a check that cannot fail is not evidence". This
# ratchets the WHOLE test corpus instead. That is strictly stronger and never weaker, which is the
# only direction a check may be changed without human review (invariant 10).

PY_TEST = re.compile(r"^\s*def (test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)
GO_TEST = re.compile(r"^func (Test[A-Za-z0-9_]+)\s*\(", re.MULTILINE)
# Shell suites print their named checks. Both spellings in the corpus.
SH_CHECK = re.compile(r"^\s*(?:check|assert)_[a-z_]*\s+[\"']([^\"']+)[\"']", re.MULTILINE)


def corpus_files() -> list[Path]:
    out = []
    out += sorted((REPO / "dev").glob("test_*.py"))
    out += sorted((REPO / "dev/tests").glob("*.py"))
    # Two entries, not one: what used to be the flat `kind/` directory is now split into the scripts
    # that judge (verify/) and the scripts that provision (cluster/). A single glob over either one
    # would silently halve the shell corpus, and the ratchet only fires on names that were already
    # baselined -- a file that stops being globbed at all is exactly the shape this misses.
    out += sorted((REPO / "dev/verify").glob("*.sh"))
    out += sorted((REPO / "dev/cluster").glob("*.sh"))
    out += sorted((REPO / "dev/tests").glob("*.sh"))
    for d in ("k8s-operator/internal", "k8s-operator/api"):
        out += sorted((REPO / d).rglob("*_test.go"))
    return [p for p in out if p.exists()]


def inventory() -> dict[str, list[str]]:
    """{repo-relative file: [sorted named tests]} — the thing the ratchet compares."""
    inv: dict[str, list[str]] = {}
    for path in corpus_files():
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text()
        names = set()
        if path.suffix == ".py":
            names |= set(PY_TEST.findall(text))
        elif path.suffix == ".go":
            names |= set(GO_TEST.findall(text))
        elif path.suffix == ".sh":
            names |= set(SH_CHECK.findall(text))
        if names:
            inv[rel] = sorted(names)
    return inv


def load_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text())


def check_assertion_ratchet() -> list[str]:
    base = load_baseline()
    if not base:
        return [
            f"no baseline at {BASELINE.relative_to(REPO)} — run with --update-baseline to record "
            "the current inventory. The ratchet cannot ratchet against nothing."
        ]

    retired = base.get("retired", {})
    old = base.get("inventory", {})
    new = inventory()

    failures = []
    total_old = sum(len(v) for v in old.values())
    total_new = sum(len(v) for v in new.values())

    for rel, names in old.items():
        if rel not in new:
            if rel in retired:
                continue
            failures.append(
                f"the whole corpus file {rel} is gone ({len(names)} named tests). If that is "
                f"deliberate, add it to \"retired\" in {BASELINE.relative_to(REPO)} with the path "
                f"that replaces it (V-MET-004: a retirement names its replacement)."
            )
            continue
        lost = sorted(set(names) - set(new[rel]))
        for name in lost:
            key = f"{rel}::{name}"
            if key in retired:
                continue
            failures.append(
                f"{key} existed at baseline and is gone. Tests are replaced, never deleted "
                f"(invariant 8). Add \"{key}\" to \"retired\" in "
                f"{BASELINE.relative_to(REPO)} naming its replacement, or restore it."
            )

    if total_new < total_old and not failures:
        failures.append(
            f"the corpus shrank from {total_old} to {total_new} named tests and no individual "
            f"loss was identified — the inventory extractor probably stopped matching a file "
            f"format. Investigate before updating the baseline."
        )
    return failures


def check_the_ratchet_baseline_covers_the_corpus() -> list[str]:
    """LSN-056: a ratchet that was last wound in April protects April's tests and prints today's tick.

    `check_assertion_ratchet` above compares the tree against `dev/assertion-baseline.json` and
    fails when a baselined test disappears. It says nothing about tests the baseline never knew, so
    a baseline that is never regenerated silently narrows: on 2026-07-30 it held **194** names
    across **34** files and the tree had **1290** across **137**. The ratchet had been guarding 15%
    of the suite and reporting the same green as if it guarded all of it, for months.

    The instruction to regenerate has been sitting in the baseline's own `_comment` the whole time
    ("Regenerate ... ONLY when adding tests"). Prose on the artifact is not a mechanization — that
    is [[LSN-019]] — so this is the arm that makes it one.

    Deliberately strict about names, not just files. Covering only files would let a file grow from
    two tests to fifty with forty-eight outside the ratchet, which is the same escape at a smaller
    scale and would be harder to see the second time. The cost is one command in any unit that adds
    a test, and the failure message is that command.
    """
    base = load_baseline()
    if not base:
        return []  # check_assertion_ratchet owns the missing-baseline case
    old, retired = base.get("inventory", {}), base.get("retired", {})
    new = inventory()
    if not new:
        return [
            "VACUOUS: the inventory extractor found no named tests at all, so this check compared "
            "nothing. The tree has thousands; the extractor stopped matching a file format."
        ]

    unratcheted = sorted(
        f"{rel}::{name}"
        for rel, names in new.items()
        for name in set(names) - set(old.get(rel, []))
        if f"{rel}::{name}" not in retired
    )
    if not unratcheted:
        return []

    files = sorted({k.split("::")[0] for k in unratcheted})
    shown = unratcheted[:8]
    more = f" (+{len(unratcheted) - len(shown)} more)" if len(unratcheted) > len(shown) else ""
    return [
        f"{len(unratcheted)} named test(s) across {len(files)} file(s) exist in the tree and not "
        f"in {BASELINE.relative_to(REPO)}, so the ratchet does not protect them: {shown}{more}. "
        f"Wind it: `python3 dev/tests/invariants-gate.py --update-baseline`. A test outside the "
        f"baseline can be deleted with every gate green, which is what the ratchet exists to stop."
    ]


def check_retirements_name_replacements() -> list[str]:
    """V-MET-004. A retirement with an empty replacement is a deletion with extra steps."""
    base = load_baseline()
    if not base:
        return []
    failures = []
    for key, replacement in base.get("retired", {}).items():
        if not isinstance(replacement, str) or not replacement.strip():
            failures.append(f"retired entry {key!r} names no replacement (V-MET-004)")
            continue
        # The replacement must be something that exists, not a promise.
        target = replacement.split("::")[0]
        if target.startswith(("V-", "09 §", "docs/")):
            continue  # a spec pointer, legitimate for a check retired as out-of-model
        if not (REPO / target).exists():
            failures.append(
                f"retired entry {key!r} points at {target!r}, which does not exist on disk. "
                f"A replacement pointer to a missing file is how LSN-019 happens."
            )
    return failures


# ---------------------------------------------------------------------------------------------
# Invariant 13 / LSN-019 — a `closed` lesson names something that runs
# ---------------------------------------------------------------------------------------------

LESSON_ROW = re.compile(
    r"^\|\s*\*\*(LSN-\d+)\*\*\s*\|([^|]*)\|([^|]*)\|\s*(?:\*\*)?(\w+)(?:\*\*)?\s*\|([^|]*)\|",
    re.MULTILINE,
)
PATHISH = re.compile(r"`([^`]+\.(?:py|sh|go|yaml|yml))`")


def l0_chain_text() -> str:
    if not L0_CHAIN.exists():
        raise FileNotFoundError(L0_CHAIN)
    return L0_CHAIN.read_text()


def _uncommented(text: str) -> str:
    """Drop whole-line `#` comments. Comments are the largest false-green risk in this check.

    l0-checks.yml's own header names install-path-wired.py, closed-allowlist.py and
    reference-render.py in prose. Matching against the raw file would let a lesson close against a
    script that is *mentioned* by an automation rather than *run* by one -- which is precisely the
    distinction (LSN-019) this check exists to draw. Inline trailing `#` is left alone: it can be
    inside a quoted string, and a wrong strip fails open.
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def regress_chain_text() -> str:
    """Everything that invokes an artifact automatically: both chains plus the CI workflows.

    The first draft of this check accepted only L0-CHAIN.txt, and it was wrong in a way worth
    recording. A lesson closed by a Kind script (LSN-006: "well-formed is not enforced") can never
    appear in an L0 chain -- L2 needs a cluster, by definition -- so the draft demanded that the
    only lessons allowed to be `closed` are the ones provable without the thing that proves
    enforcement. It conflated "runs in the pre-merge gate" with "runs at all".

    Widening it is not invariant 9 (no weakening to pass): this check has never passed, has never
    been recorded as evidence anywhere, and is still being authored. It is also not a way out of
    the finding -- twelve lessons still reopen after the correction, which is the whole reason to
    trust the widened form.
    """
    parts = [L0_CHAIN.read_text() if L0_CHAIN.exists() else ""]
    parts.append(L2_CHAIN.read_text() if L2_CHAIN.exists() else "")
    if WORKFLOWS.is_dir():
        for wf in sorted(WORKFLOWS.glob("*.y*ml")):
            parts.append(wf.read_text())
    return _uncommented("\n".join(parts))


def _invoked_by(artifact: str, chain: str) -> bool:
    """Does anything automatic actually run this file?"""
    base = artifact.split("/")[-1]
    if base in chain:
        return True
    # A workflow is not invoked BY the chain -- it IS the automation, provided something triggers
    # it. A `workflow_dispatch`-only file is not: nothing runs it unless a human presses a button,
    # which is the same standing as a script nobody types.
    wf = WORKFLOWS / base
    if wf.is_file():
        head = wf.read_text().split("jobs:")[0]
        return "pull_request" in head or "push" in head or "schedule" in head
    # `python3 -m unittest discover dev` runs every dev/test_*.py without naming one.
    # Requiring the name to appear literally would reopen lessons closed by a real, running test.
    if (
        base.startswith("test_")
        and base.endswith(".py")
        and (REPO / "dev" / base).exists()
        and "unittest discover dev" in chain
    ):
        return True
    # Same argument for Go, and it was missing until LSN-038's pass. `make -C k8s-operator test`
    # (the required `Run Controller Tests` check) runs `go test $(go list ./... | grep -v /e2e)`,
    # which names no file either -- so a lesson mechanized as a Go test read as "run by nothing"
    # and had to be co-signed by a Python script to close. That pushed three lessons (LSN-032/033/
    # 034) into citing whichever `.py` was nearby, which is a citation that does not describe the
    # mechanization. The `/e2e` exclusion is copied from the Makefile line, not assumed: a test
    # under `test/e2e/` is genuinely run by nothing automatic.
    if base.endswith("_test.go"):
        for hit in REPO.rglob(base):
            rel = hit.relative_to(REPO).as_posix()
            if "/e2e/" in rel or not rel.startswith("k8s-operator/"):
                continue
            if not re.search(r"^func Test", hit.read_text(), re.MULTILINE):
                continue
            if "k8s-operator test" in chain or "go test" in chain:
                return True
    return False


def check_closed_lessons_are_executable() -> list[str]:
    """LSN-019: closed means a command exits non-zero when the defect returns.

    Every lesson whose index row says `closed` must name at least one artifact that (a) exists on
    disk and (b) is invoked by one of the declared chains or a CI workflow. A check ID is not a
    mechanization -- 09 is a specification and specifications do not execute. That distinction is
    the whole lesson: LSN-007 was closed against "V-CMP-001", no script implemented it, and the
    defect came back twice.

    Expect this to reopen lessons. That is the point, and the open count crossing its threshold is
    the correct outcome rather than a reason to weaken the rule.
    """
    if not LESSONS.exists():
        return [f"{LESSONS.relative_to(REPO)} not found"]

    chain = regress_chain_text()
    if "unittest discover dev" not in chain:
        return [
            "VACUOUS: the assembled chain text does not contain the unittest discover command, so "
            "the chain files were not read and every lesson would reopen for the wrong reason."
        ]
    rows = LESSON_ROW.findall(LESSONS.read_text())
    if len(rows) < 15:
        return [
            f"VACUOUS: parsed only {len(rows)} lesson rows from the index table; the format "
            f"changed and this check stopped checking. Fix the parser, not the table."
        ]

    failures = []
    for lid, _tag, _symptom, status, closed_by in rows:
        if status.strip().lower() != "closed":
            continue
        artifacts = PATHISH.findall(closed_by)
        if not artifacts:
            failures.append(
                f"{lid} is `closed` and its 'Closed by' column names no runnable artifact "
                f"(only: {closed_by.strip()}). A check ID, a binding.md clause and a spec section "
                f"all describe who should enforce; none of them execute."
            )
            continue
        # EVERY named artifact must exist, not merely one of them. "One of them" is what this
        # check asked for until 2026-07-26, and it let LSN-027 name `lib/substrate-capacity.sh`
        # -- a path that has never existed, the file is at `dev/lib/` -- for as long as the row
        # also named `invariants-gate.py`, which resolves. A row that half-resolves reads as a
        # working citation and is checked as one, which is the [[lsn-023]] shape one level up:
        # the text that describes the mechanization satisfied the check instead of the
        # mechanization. Tightening, not relaxing (invariant 10 permits it).
        missing = []
        for a in artifacts:
            hits = list(REPO.rglob(a)) if "/" not in a else ([REPO / a] if (REPO / a).exists() else [])
            if not [h for h in hits if h.exists()]:
                missing.append(a)
        if len(missing) == len(artifacts):
            failures.append(
                f"{lid} is `closed` naming {artifacts}, none of which exists on disk."
            )
            continue
        if missing:
            failures.append(
                f"{lid} is `closed` and names {missing}, which do not exist on disk. The row's "
                f"other citations resolve, so this reads as a working reference and is not one — "
                f"fix the path or drop the claim."
            )
            continue
        if not any(_invoked_by(a, chain) for a in artifacts):
            failures.append(
                f"{lid} is `closed` naming {artifacts}, which exist but are run by nothing: no "
                f"line of L0-CHAIN.txt or L2-CHAIN.txt, no step of any workflow. An artifact "
                f"nothing runs is not a mechanization; wire it into a chain or reopen the lesson."
            )
    return failures


LESSON_BODY = re.compile(r"^##\s+(LSN-\d+)\b", re.MULTILINE)


def check_every_lesson_has_an_index_row() -> list[str]:
    """LSN-019, one level up: a lesson the gate cannot see is a lesson the gate does not enforce.

    `check_closed_lessons_are_executable` iterates the INDEX TABLE. A lesson body appended without
    its index row is therefore not checked at all — not reported as open, not reported as
    unmechanized, not reported. It reads as closed to a human (the body says `closed`) and does not
    exist to the gate.

    That is not hypothetical. LSN-031 was written in P9-T3a with a full body, a `closed` status and
    two named mechanizations, and no index row. It went unenforced until LSN-032 was being written
    against the same table and the row was missing from the count.

    The table is also the only place the tag lives, and the tag is how ORIENT decides which lessons
    to read. A lesson with no row is invisible to the reader who needed it, too.
    """
    if not LESSONS.exists():
        return [f"{LESSONS.relative_to(REPO)} not found"]

    text = LESSONS.read_text()
    bodies = LESSON_BODY.findall(text)
    rows = {lid for lid, *_ in LESSON_ROW.findall(text)}

    if len(bodies) < 15:
        return [
            f"VACUOUS: parsed only {len(bodies)} lesson bodies from "
            f"{LESSONS.relative_to(REPO)}; the heading format changed and this check stopped "
            f"checking. Fix the parser, not the file."
        ]

    failures = []
    for lid in bodies:
        if lid not in rows:
            failures.append(
                f"{lid} has a body but no row in the index table. Every other check in this file "
                f"reads the table, so the lesson is unenforced and its tag is unreadable during "
                f"ORIENT — it looks closed and is not checked."
            )
    for lid in sorted(rows - set(bodies)):
        failures.append(
            f"{lid} has an index row and no body. The row's citations are checked and its "
            f"substance is nowhere; a reader following the tag arrives at nothing."
        )
    return failures


# ---------------------------------------------------------------------------------------------
# The human backlog is drained, not accumulated
# ---------------------------------------------------------------------------------------------

BACKLOG = REPO / "docs/build/BACKLOG.md"

BACKLOG_SECTIONS = ("Inbox", "Scheduled", "Refused", "Done")
LAST_DRAINED = re.compile(r"^\*\*Last drained:\*\*\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)
INBOX_ITEM = re.compile(r"^### (?P<title>.+?)\s*$", re.M)
ADDED = re.compile(r"^-\s+\*\*Added:\*\*\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)
BACKLOG_ID = re.compile(r"^\|\s*(B-\d{3})\s*\|", re.M)


def _backlog_section(text: str, name: str) -> str | None:
    """The body of one `## <name>` section, up to the next `## ` heading."""
    m = re.search(rf"^## {name}\s*$\n(?P<body>(?:(?!^## ).*\n)*)", text, re.M)
    return m.group("body") if m else None


HARNESS_RUN_SKILL = REPO / ".claude/skills/harness-run/SKILL.md"


def _drain_is_committed(text: str, inbox: str, drained_on: str) -> list[str]:
    """A drain that exists only in the working tree has not happened yet (LSN-043).

    ORIENT is required to WRITE exactly one artifact — the drain — and everything that follows it
    moves `HEAD`: the phase branch gets created, a stash gets popped, a PR gets merged. One of those
    reverted a completed drain, and `check_backlog_is_drained` above passed on the reverted file
    exactly as it passed on the drained one: an empty inbox stamped with today's date is what a
    correct drain looks like. The two backlog items in it were recovered only because an unrelated
    `cp` to `/tmp` happened to still be there.

    So the property is not "the inbox is empty" — that one is already asserted and it is the wrong
    question here. It is that the drain is DURABLE: whatever ORIENT removed from the inbox is in a
    commit, not in the editor.

    The distinction that makes this safe to enforce is the one thing `BACKLOG.md` exists for. A human
    may append to the inbox at any time, including mid-unit — that is the whole affordance, and it
    leaves the file dirty on purpose. So an uncommitted change is only a finding when it *removes*
    items or *advances* `Last drained`, which no human append does and every drain does.

    Also asserts that `harness-run` §1 step 6 still tells the harness to commit before SELECT. The
    procedural instruction and the gate that enforces it have to move together, or the next person
    to read the skill learns a workflow the gate rejects.
    """
    failures = []

    skill = HARNESS_RUN_SKILL.read_text() if HARNESS_RUN_SKILL.exists() else ""
    if not re.search(r"commit the drain,?\s*before SELECT", skill, re.I):
        failures.append(
            f"{HARNESS_RUN_SKILL.relative_to(REPO)} §1 step 6 no longer tells ORIENT to commit the "
            f"drain before SELECT. That sentence is the procedure this check enforces; without it "
            f"the gate below reads as an unexplained rule, and LSN-043's window reopens."
        )

    try:
        head = subprocess.run(
            ["git", "show", f"HEAD:{BACKLOG.relative_to(REPO)}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        # Reported, never skipped. "git was unavailable" is how this half of the check would go
        # quiet, and a quiet half reads identically to a passing one (LSN-035, LSN-038).
        return failures + [
            f"VACUOUS: could not read {BACKLOG.relative_to(REPO)} from HEAD ({exc}), so the "
            f"drain-is-committed half of this check did not run. The file is tracked and git is "
            f"present in every environment that runs the L0 chain; if that changed, this check "
            f"needs a new way to see the last committed state, not a pass."
        ]

    if head == text:
        return failures

    head_inbox = _backlog_section(head, "Inbox") or ""
    head_drained = LAST_DRAINED.search(head_inbox)
    if head_drained and drained_on > head_drained.group(1):
        failures.append(
            f"{BACKLOG.relative_to(REPO)}: `Last drained` advanced to {drained_on} (HEAD says "
            f"{head_drained.group(1)}) and the change is not committed. Commit the drain now, "
            f"before SELECT — a branch switch, a stash pop or a `gh pr merge` reverts it silently, "
            f"and the reverted file passes every gate in this file (LSN-043)."
        )

    gone = {t.strip() for t in INBOX_ITEM.findall(head_inbox)} - {
        t.strip() for t in INBOX_ITEM.findall(inbox)
    }
    if gone:
        failures.append(
            f"{BACKLOG.relative_to(REPO)}: inbox item(s) {sorted(gone)} were resolved out of the "
            f"inbox and the change is not committed. A resolved item that exists only in the "
            f"working tree is one `git checkout` from never having existed, with every gate green "
            f"(LSN-043). Commit the drain as its own commit before SELECT."
        )

    return failures


def check_backlog_is_drained() -> list[str]:
    """A human backlog the harness reads and walks past is worse than no backlog at all.

    `docs/build/BACKLOG.md` is the one file a human may append to while the harness is mid-unit. The
    protocol that makes it safe is that it is drained at ORIENT and only at ORIENT, and that EVERY
    item is resolved in the ORIENT that reads it — scheduled, refused, or escalated. That protocol
    is prose, and prose is what the harness is most likely to skip when the inbox holds something
    inconvenient and there is a unit waiting.

    So the property is mechanized here, and it is a single comparison: an item whose `Added` date is
    strictly before `Last drained` was in the inbox when the harness last looked, and is still in the
    inbox. There is no reading of that which is not "read and ignored".

    The rest of the check is anti-vacuity. It fails if a section heading vanished (the drain would
    have nowhere to move things to), if `Last drained` is gone (the comparison above silently stops
    happening), if an inbox item has no `Added` (same — an undated item can never be found stale),
    or if a `B-nnn` id appears twice across the tables (a reused id makes the trail ambiguous about
    which item actually landed).
    """
    if not BACKLOG.exists():
        return [
            f"{BACKLOG.relative_to(REPO)} not found. It is the only place a human can add work "
            f"mid-run without racing the ledger; without it there is no such place."
        ]

    text = BACKLOG.read_text()
    failures = []

    bodies = {}
    for name in BACKLOG_SECTIONS:
        body = _backlog_section(text, name)
        if body is None:
            failures.append(
                f"the `## {name}` section is gone from {BACKLOG.relative_to(REPO)}. The drain "
                f"protocol moves items between these four sections; a missing one is a drain step "
                f"with nowhere to put its result."
            )
        else:
            bodies[name] = body
    if failures:
        return failures

    drained = LAST_DRAINED.search(bodies["Inbox"])
    if not drained:
        return [
            f"{BACKLOG.relative_to(REPO)}: the `**Last drained:** YYYY-MM-DD` line is gone from "
            f"`## Inbox`. It is the left-hand side of the only comparison that can tell a stale "
            f"item from a fresh one, so removing it does not relax this check — it disables it."
        ]
    drained_on = drained.group(1)

    # Split the inbox into per-item blocks so an `Added` line is attributed to its own item.
    starts = [m.start() for m in INBOX_ITEM.finditer(bodies["Inbox"])]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(bodies["Inbox"])
        block = bodies["Inbox"][start:end]
        title = INBOX_ITEM.match(block).group("title")
        added = ADDED.search(block)
        if not added:
            failures.append(
                f'inbox item "{title}" has no `**Added:** YYYY-MM-DD`. An undated item can never '
                f"be found stale by this check, so it can sit in the inbox forever."
            )
        elif added.group(1) < drained_on:
            failures.append(
                f'inbox item "{title}" was added {added.group(1)} and the inbox was drained '
                f"{drained_on} — so ORIENT read it and left it. Every item is resolved in the "
                f"ORIENT that reads it: schedule it, refuse it with an argument, or escalate it."
            )

    failures.extend(_drain_is_committed(text, bodies["Inbox"], drained_on))

    seen = {}
    for name in ("Scheduled", "Refused", "Done"):
        for bid in BACKLOG_ID.findall(bodies[name]):
            if bid in seen:
                failures.append(
                    f"{bid} appears in both `## {seen[bid]}` and `## {name}`. Backlog ids are "
                    f"never reused; two rows with one id make it unanswerable which item landed."
                )
            seen[bid] = name

    return failures


# ---------------------------------------------------------------------------------------------
# The chain itself must be real
# ---------------------------------------------------------------------------------------------


def check_l0_chain_is_runnable() -> list[str]:
    """Every command in the chain names a file that exists, and CI runs the chain.

    Without this, L0-CHAIN.txt becomes the next thing that is authoritative and wrong.
    """
    failures = []
    try:
        chain = l0_chain_text()
    except FileNotFoundError:
        return [f"{L0_CHAIN.relative_to(REPO)} is missing — it is the single definition site"]

    commands = [
        ln.strip()
        for ln in chain.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(commands) < 5:
        return [f"VACUOUS: the L0 chain has only {len(commands)} commands"]

    for cmd in commands:
        for token in cmd.split():
            if token.endswith((".py", ".sh")) and not (REPO / token).exists():
                failures.append(f"L0 chain runs {token!r}, which does not exist")

    wf = REPO / ".github/workflows/l0-checks.yml"
    if not wf.exists():
        failures.append(
            "no .github/workflows/l0-checks.yml — the chain is defined and nothing runs it on a "
            "PR, which is the exact defect this file was created to fix"
        )
    elif "L0-CHAIN.txt" not in wf.read_text():
        failures.append(
            "l0-checks.yml does not read L0-CHAIN.txt — it has its own copy of the list, so the "
            "two will drift (V-MET-013: single definition site)"
        )
    return failures


# ---------------------------------------------------------------------------------------------
# LSN-005 — the destructive-test guard stays anchored
# ---------------------------------------------------------------------------------------------

# A script whose context comes from the caller: `CTX="${1:-gke-scratch-...}"`. Those are the ones
# that can be pointed anywhere. up.sh derives its context from the cluster it just created and is
# not in scope -- there is no argument to aim at the live cluster.
CALLER_CTX = re.compile(r'^\s*CTX="?\$\{\d+:-', re.MULTILINE)
CASE_ON_CTX = re.compile(r'case\s+"?\$(?:\{)?CTX(?:\})?"?\s+in(.*?)^\s*esac', re.MULTILINE | re.DOTALL)
# One anchor, since 2026-07-26. `kind-` was the second accepted prefix until the inner loop moved
# off the host; dropping it NARROWS what these scripts will run against, which invariant 10 permits
# in the strengthening direction only -- and this is that direction. A stale `kind-*` arm left here
# would be worse than cosmetic: it is an accepted prefix with nothing behind it, so the first script
# to acquire one would be accepted by a guard nobody is maintaining.
ANCHORED_ARM = re.compile(r"^gke-scratch-[A-Za-z0-9*_.-]*\*?$")


def _case_arms(body: str) -> tuple[list[str], str | None]:
    """Split a `case` body into (accepting patterns, the text of the `*)` arm).

    Parsed by splitting on `;;` rather than by matching `^...)` per line, because the default arm
    is usually several lines long and its `echo "... (destructive-test guard)."` contains a closing
    paren. A line-oriented parser reads that message AS a pattern, reports it as unanchored, and
    the check fails on the four scripts that are correct — which is how a real gate gets disabled.
    """
    arms: list[str] = []
    default: str | None = None
    for seg in body.split(";;"):
        seg = seg.strip().lstrip("\\").strip()
        if not seg:
            continue
        head, sep, rest = seg.partition(")")
        if not sep or "\n" in head:
            continue  # not an arm head: trailing text after the last `;;`
        patterns = [p.strip().strip("\"'") for p in head.split("|")]
        if patterns == ["*"]:
            default = rest
            continue
        arms += patterns
    return arms, default


def check_destructive_guards_are_anchored() -> list[str]:
    """LSN-005. Every script that takes a --context from the caller refuses a non-scratch one.

    These scripts apply deliberately-bad RBAC, delete pods and exercise denial paths. `binding.md`
    §Targets classifies the live cluster `platform-agent-host` as install verification only, and
    `kubectl config current-context` may well be pointing at it right now.

    The assertion is specifically about ANCHORING, not about the guard existing. `*scratch*)` and
    `[[ $CTX == *scratch* ]]` both look like guards and both accept `my-gke-scratch-of-prod`; an
    anchored `gke-scratch-*` cannot. LSN-005 is the substring-match lesson, so a check that only
    asserted "there is a case statement" would pass the exact code the lesson is about.
    """
    scripts = sorted((REPO / "dev").rglob("*.sh"))
    scoped = [p for p in scripts if CALLER_CTX.search(p.read_text())]
    if len(scoped) < 10:
        return [
            f"VACUOUS: found only {len(scoped)} caller-context scripts under dev/; there "
            f"were 14 when this check was written. The CTX idiom changed and the guard is now "
            f"unchecked on every script the pattern stopped matching."
        ]

    failures = []
    for p in scoped:
        rel = p.relative_to(REPO)
        text = p.read_text()
        m = CASE_ON_CTX.search(text)
        if not m:
            failures.append(
                f"{rel} takes a context from the caller and has no `case \"$CTX\" in` guard — "
                f"it will run against whatever the caller passes, including the live cluster."
            )
            continue
        arms, default = _case_arms(m.group(1))
        if not arms:
            failures.append(f"{rel}: the $CTX case has no accepting arm; the guard parses as empty")
            continue
        for pat in arms:
            if not ANCHORED_ARM.match(pat):
                failures.append(
                    f"{rel}: guard arm {pat!r} is not anchored to `gke-scratch-`. "
                    f"A leading or interior `*` makes this a substring match, which is LSN-005 "
                    f"verbatim — the live cluster is one character away from matching."
                )
        if default is None:
            failures.append(
                f"{rel}: the $CTX case has no `*)` arm, so an unmatched context falls through "
                f"and the script proceeds. Refusing must be the default, not the exception."
            )
        elif not re.search(r"exit\s+[1-9]", default):
            failures.append(
                f"{rel}: the `*)` arm does not exit non-zero. A guard that warns and continues "
                f"is a comment."
            )
    return failures


# ---------------------------------------------------------------------------------------------
# LSN-018 — a build target names its cluster
# ---------------------------------------------------------------------------------------------

OPERATOR_MAKEFILE = REPO / "k8s-operator/Makefile"
MAKE_TARGET = re.compile(r"^([a-z][a-z0-9-]*):([^=\n]*?)(?:##.*)?$", re.MULTILINE)
# The guard lives in a recipe, so its shell variable is `$$ctx` after make's expansion pass.
CASE_ON_MAKE_CTX = re.compile(r'case\s+"\$\$ctx"\s+in(.*?)esac', re.DOTALL)


def check_make_targets_are_context_explicit() -> list[str]:
    """LSN-018. `make install` must not be addressed to ambient state.

    `make -C k8s-operator install KUBECTL="kubectl --context kind-kube-agents-egress"` was run,
    accepted, and silently ignored: the recipes piped into a bare `kubectl`, so the CRD went to
    whatever `kubectl config current-context` was. It was the right cluster by luck. `make` accepts
    any variable assignment whether or not the Makefile reads it, so a no-op override looks exactly
    like a working one -- worse than no override, because it manufactures a feeling of having been
    explicit.

    `binding.md` §Targets requires an explicit `--context` on every cluster command, because
    current-context on the build host may be the live cluster `platform-agent-host`. A rule that
    shell scripts obey and build targets ignore is not a rule; this makes the Makefile obey it too.
    """
    if not OPERATOR_MAKEFILE.exists():
        return [f"{OPERATOR_MAKEFILE.relative_to(REPO)} not found"]
    text = OPERATOR_MAKEFILE.read_text()
    failures = []

    if "KUBE_CONTEXT" not in text:
        failures.append(
            "k8s-operator/Makefile has no KUBE_CONTEXT parameter — the deployment targets are "
            "addressed to whatever `kubectl config current-context` happens to be (LSN-018)."
        )
        return failures
    if "$(origin KUBECTL), command line" not in text:
        failures.append(
            "the Makefile does not reject a command-line KUBECTL= override. That exact no-op "
            "override is LSN-018's trigger; accepting and ignoring it is the defect."
        )

    body = text.partition("##@ Deployment")[2]
    if not body:
        return failures + ["no `##@ Deployment` section found; this check cannot locate the targets"]

    # Every recipe line (tab-indented) that reaches a cluster must go through $(KUBECTL).
    for n, line in enumerate(body.splitlines(), 1):
        if not line.startswith("\t"):
            continue
        if re.search(r"(?<![-\w$)])kubectl\s", line):
            failures.append(
                f"k8s-operator/Makefile deployment recipe line {n} invokes a bare `kubectl`: "
                f"{line.strip()[:70]!r}. It must use $(KUBECTL), which carries --context."
            )

    # ...and every target that owns such a recipe must depend on the guard.
    guarded, unguarded = 0, []
    for m in MAKE_TARGET.finditer(body):
        name, prereqs = m.group(1), m.group(2)
        recipe = body[m.end() :].split("\n.PHONY")[0]
        if not re.search(r"^\t.*\$\(KUBECTL\)", recipe, re.MULTILINE):
            continue
        if "ctx-guard" in prereqs:
            guarded += 1
        else:
            unguarded.append(name)
    for name in unguarded:
        failures.append(
            f"make target {name!r} writes to a cluster and does not depend on `ctx-guard`, so it "
            f"runs against the ambient context without saying which one."
        )
    if guarded < 8 and not unguarded:
        failures.append(
            f"VACUOUS: only {guarded} guarded cluster-addressing targets were recognised; there "
            f"were 10. The recipe-detection regex stopped matching and this check went quiet."
        )

    # The guard itself has to be anchored, for the same reason the shell scripts' guards are.
    m = CASE_ON_MAKE_CTX.search(text)
    if not m:
        failures.append('ctx-guard has no `case "$$ctx" in` guard on the ambient context')
    else:
        arms, default = _case_arms(m.group(1))
        if not arms:
            failures.append("ctx-guard's case has no accepting arm; the guard parses as empty")
        for pat in arms:
            if not ANCHORED_ARM.match(pat):
                failures.append(
                    f"ctx-guard arm {pat!r} is not anchored to `kind-`/`gke-scratch-` (LSN-005)"
                )
        if default is None or not re.search(r"exit\s+[1-9]", default):
            failures.append(
                "ctx-guard's `*)` arm is missing or does not exit non-zero — an unrecognised "
                "ambient context would be accepted, which is the whole of LSN-018"
            )
    return failures


# ---------------------------------------------------------------------------------------------
# V-MET-006 / LSN-008 — a deferral names a blocker, an owner and a way out
# ---------------------------------------------------------------------------------------------

# 09 §5.1. Suites whose gate class is BLOCKING-ALWAYS: these may not be deferred outright.
BLOCKING_ALWAYS = ("V-CTN", "V-BRK", "V-REV", "V-ISO", "V-ADV", "V-MET")
CHECK_ID = re.compile(r"\bV-[A-Z]{3}-\d{3}\b")


# A deferral row declares itself closed with `**CLOSED <date> by ...**` in the blocker cell.
# Leading markdown (bold, italic, strikethrough) is skipped; the word itself must be first.
CLOSED_MARKER = re.compile(r"[\s*~_]*closed(?![A-Za-z0-9])", re.IGNORECASE)


def _deferral_rows() -> list[list[str]]:
    """Every data row of the Deferrals table, PADDED to five cells rather than filtered to five.

    The first draft dropped any row with fewer than five cells. That is the same bug the check
    exists to catch, one level up: a deferral that omits the owner column *entirely* -- rather than
    leaving it blank -- was silently removed from the corpus, so the row least likely to name an
    owner was the one row guaranteed not to be asked. The tree contained a live instance
    (V-CMP-004 CLAIM 2), and the gate reported eight green.

    A short row is now padded with empty strings, which makes it fail on the missing cell instead of
    disappearing. Blanking a cell and deleting a cell must not have different consequences.
    """
    text = LEDGER.read_text()
    start = text.find("\n## Deferrals")
    if start < 0:
        return []
    end = text.find("\n## ", start + 1)
    section = text[start : end if end > 0 else len(text)]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= set("| -"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0].lower() == "date":
            continue
        rows.append((cells + ["", "", "", "", ""])[:5])
    return rows


# The corpus was 39 rows when this floor was written -- 37 from results.csv and 2 markdown rows
# naming a check ID. Set just under the CSV's own row count, because the record is append-only: it
# grows on every run and never shrinks, so any real drop means the gate stopped reading it.
#
# The floor exists because the failure mode of a MOVED corpus is not the failure mode of a missing
# one. Missing is safe: `green` goes False and the BLOCKING-ALWAYS arm fails loudly. But it fails
# saying "V-CTN-020 is deferred and green nowhere", which sends whoever reads it to look at
# V-CTN-020, and the actual defect is three directories away in the gate's own file list. A wrong
# diagnosis delivered confidently is worse than a red build. Say which failure this is.
EVIDENCE_ROW_FLOOR = 35


def _verification_evidence_rows() -> list[str]:
    """Every row of the verification record, wherever it currently lives, as searchable text.

    Three sources, one question. The markdown sources are matched as whole lines exactly as they
    were when the record was a single table in the ledger -- `cid in line and "**pass**" in line`
    -- because that is the behaviour the BLOCKING-ALWAYS arm was mutation-tested against and a
    corpus move is not the moment to also change the predicate.

    The CSV is parsed with `csv.reader` rather than grepped, which makes it STRICTER than the
    markdown path: `pass` has to be in the `result` field and the ID has to be in the `check_id`
    field, so the word "passes" inside some other check's evidence paragraph cannot vouch for a
    check that never ran. The rows are re-emitted in the pipe-delimited shape the caller expects,
    with everything but those two fields dropped, so nothing else in the row can match either.
    Stricter again since 2026-07-29: one line **per check ID**, and a pass a later `**correction**`
    row retracts is not emitted at all. See the comment on that loop for the two live defects.

    Rows are filtered to those that actually name a check. That is behaviour-preserving for the
    caller, which only ever matched a row containing the ID it was asking about, and it is what
    makes the floor below mean something: without it the count is dominated by the Status, Index,
    Deferrals and Decisions tables, and the whole evidence record could vanish while the corpus
    still measured two hundred rows.
    """
    rows: list[str] = []
    for path in (LEDGER, LEDGER_ARCHIVE):
        if path.exists():
            rows += [
                ln
                for ln in path.read_text().splitlines()
                if ln.startswith("|") and CHECK_ID.search(ln)
            ]
    records = _results_rows()
    # The record is APPEND-ONLY, so a pass that was wrong is never removed -- it is retracted by a
    # later `**correction**` row naming the same check (the convention V-MET-014 rows 18-19 set).
    # A reader that greps for "pass" therefore keeps vouching for a claim the record itself has
    # withdrawn, permanently and by construction. That is not hypothetical: V-CTN-021 was falsely
    # passed on row 47, and appending the correction did not stop the BLOCKING-ALWAYS arm reading
    # it as green, because the retracted row is still sitting there with the word in it. A check
    # that cannot see a retraction cannot see a correction, and the whole convention is decorative.
    #
    # So passes are emitted PER CHECK ID and dropped when a later correction names that ID. Per-ID
    # matters as much as the supersession: row 47 is `"V-CTR-002, V-CTN-021"`, one cell naming two
    # checks, of which exactly one was really run. Emitting the row whole makes the honest half
    # vouch for the other half forever.
    retracted_at: dict[str, int] = {}
    for i, rec in enumerate(records):
        if "correction" in (rec.get("result") or "").lower():
            for cid in CHECK_ID.findall(rec.get("check_id") or ""):
                retracted_at[cid] = i  # last correction wins; a later re-pass still counts
    for i, rec in enumerate(records):
        if "pass" not in (rec.get("result") or "").lower():
            continue
        for cid in CHECK_ID.findall(rec.get("check_id") or ""):
            if retracted_at.get(cid, -1) > i:
                continue
            rows.append(f"| {cid} | **pass** |")
    return rows


def _closed_deferral_failures(
    subject: str, blocker: str, promote: str, chain: str
) -> list[str]:
    """The LSN-019 bar, applied to a deferral that says it is over.

    Deliberately the same three questions LSN-019 asks a closed lesson, in the same order, because
    the two closures make the same claim: that a thing which used to be unverifiable is now
    verified by something that runs. A closure backed by a paragraph is the shape LSN-007 had, and
    that defect returned twice.
    """
    if not re.search(r"\d{4}-\d{2}-\d{2}", blocker):
        return [
            f"deferral {subject!r} is CLOSED without a closure date. When it stopped being true "
            f"is half of what the register is for."
        ]
    artifacts = PATHISH.findall(promote)
    if not artifacts:
        return [
            f"deferral {subject!r} is CLOSED and its promote-when column names no runnable "
            f"artifact (only: {promote.strip() or '—'}). The column asks what would end the "
            f"deferral; a closed row answers with the thing that did, and prose does not run."
        ]
    resolved = [a for a in artifacts if (REPO / a).exists()]
    if not resolved:
        return [
            f"deferral {subject!r} is CLOSED naming {artifacts}, none of which exists on disk."
        ]
    if not any(_invoked_by(a, chain) for a in resolved):
        return [
            f"deferral {subject!r} is CLOSED naming {resolved}, which exist but are run by "
            f"nothing: no line of L0-CHAIN.txt or L2-CHAIN.txt, no step of any workflow. The "
            f"deferral was that nothing checked this; an artifact nothing invokes has not "
            f"changed that."
        ]
    return []


# ---------------------------------------------------------------------------------------------
# The † set — checks 09 §6.14 marks as blocked on an unresolved §12 tightening
# ---------------------------------------------------------------------------------------------

CATALOG_ID = re.compile(r"^\*{0,2}(V-[A-Z]{3}-\d{3})\*{0,2}$")


def _catalog_rows() -> list[list[str]]:
    """Every row of every §6 catalog table, as cells, keyed by a check ID in column 1."""
    rows = []
    for line in SPEC.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and CATALOG_ID.match(cells[0]):
            rows.append(cells)
    return rows


def _catalog_phases() -> dict[str, int]:
    """{check id: the phase 09 §6 requires it by}, for every catalogued check.

    Derived from the tables, never listed here (LSN-036). Every §6 catalog table ends in a Phase
    column, so the phase is the last cell whenever it is a bare integer. A row whose last cell is
    not a bare integer is simply ABSENT from this map rather than defaulted -- callers must treat
    a missing phase as unknown, never as "not yet due" (LSN-038).
    """
    phases = {}
    for cells in _catalog_rows():
        if cells[-1].isdigit():
            phases[CATALOG_ID.match(cells[0]).group(1)] = int(cells[-1])
    return phases


def _dagger_checks() -> dict[str, int | None]:
    """{check id: required phase, or None if unknown} for every check marked **†**."""
    phases = _catalog_phases()
    out: dict[str, int | None] = {}
    for cells in _catalog_rows():
        if "†" not in " ".join(cells):
            continue
        cid = CATALOG_ID.match(cells[0]).group(1)
        out[cid] = phases.get(cid)
    return out


def check_dagger_checks_are_deferred_by_id() -> list[str]:
    """09 §12: a check blocked on an unresolved tightening must be **recorded as deferred**.

    By ID, and that is the whole of this check. Until 2026-07-29 the ledger deferred the whole set
    as a category -- one row reading "09 §6.14 checks marked †" -- which is accurate prose and
    invisible to every check that works on IDs. `check_deferrals_name_blockers` below asks whether
    a deferred check is BLOCKING-ALWAYS by running CHECK_ID over the row's subject cell; against a
    category name it matches nothing and reports clean. **V-CTN-021 is a V-CTN check** and sat
    inside that row, unasked, from the day the row was written to the day this check was.

    The failure is not that someone hid it. The row is honest and a human reading it would find
    V-CTN-021 in a minute. The failure is that the gate's one arm for "is a BLOCKING-ALWAYS check
    being deferred" could be satisfied by naming a set instead of its members, which makes the
    arm's silence mean nothing -- the LSN-035 shape, one level up from the checks it was written
    about: an assertion that passes for a reason unrelated to the property.

    So: the † set is derived from 09 §6.14, and every member must appear literally in the
    Deferrals table. An empty derived set is a FAIL, not a clean run -- if the daggers are ever
    restyled or the section renamed, this check must say so rather than congratulate the tree.
    """
    if not SPEC.exists():
        return [f"{SPEC.relative_to(REPO)} not found"]
    if not LEDGER.exists():
        return [f"{LEDGER.relative_to(REPO)} not found"]
    dagger = _dagger_checks()
    if not dagger:
        return [
            "VACUOUS: no check in 09 §6.14 parses as marked †; there were 4 when this was "
            "written (V-CTN-021, V-PRO-022, V-PRO-026, V-CHR-014). Either every tightening was "
            "resolved — in which case delete this check and the deferral row together — or the "
            "marker changed and the derivation broke. An empty † set must never read as clean."
        ]
    deferred = "\n".join(c[1] for c in _deferral_rows())
    failures = []
    for cid in sorted(dagger):
        if cid not in deferred:
            failures.append(
                f"{cid} is marked † in 09 §6.14 — blocked on an unresolved §12 tightening — but "
                f"no row of the ledger's Deferrals table names it. 09 §12: such a check 'must be "
                f"recorded as deferred with this row as the blocker', never quietly skipped. "
                f"Naming the category it belongs to is not naming it: the BLOCKING-ALWAYS arm "
                f"reads check IDs out of the subject cell and a category name matches none."
            )
    failures += _dagger_pass_failures(sorted(dagger))
    return failures


def _results_rows() -> list[dict[str, str]]:
    """`verification/results.csv` as dicts, in append order. Index order IS chronological order."""
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _dagger_pass_failures(dagger: list[str]) -> list[str]:
    """The other half: a † check may not be recorded as **passing**.

    "Blocked on a §12 tightening" and "passed" cannot both be true -- 09 §6.14 says a † check "is
    not runnable until the named ambiguity is resolved", so a pass row naming one is a false green
    by construction, provable from two text files with no cluster and no judgement call.

    This arm exists because the tree had one. `results.csv` row 47 (2026-07-27, P8-T9) recorded
    `"V-CTR-002, V-CTN-021"` as **pass** at L2, on evidence -- `webhook-negatives-l2.sh` -- that
    proves V-CTR-002's property exactly and says nothing whatever about V-CTN-021's, which is
    conformance of all 39 cells of the 02 §7 boundary matrix. There is no V-CTR-021, so it is not
    a one-letter slip from the neighbouring ID; the ID that got recorded exists, is BLOCKING-ALWAYS,
    and was not run. It went unnoticed for two days and it was **masking a second defect**: the
    BLOCKING-ALWAYS arm of `check_deferrals_name_blockers` short-circuits on "green somewhere", so
    the phase-expiry clause written in the same sitting as this one could never fire, and testing
    that clause is what surfaced the false green. A false pass does not sit still -- it silences
    the checks downstream of it.

    **Corrections are honoured, history is not rewritten.** `results.csv` is append-only, so the
    fix for a bad pass row is a later `**correction**` row naming the same check, which is the
    convention the record already follows (see V-MET-014, rows 18-19). A pass superseded that way
    is satisfied here; an uncorrected one is not.
    """
    rows = _results_rows()
    if not rows:
        return [
            f"VACUOUS: {RESULTS_CSV.relative_to(REPO)} parsed to zero rows. This arm asks whether "
            f"any † check was recorded green, and against an empty corpus the answer is always no."
        ]
    failures = []
    for cid in dagger:
        hits = [
            (i, r["result"]) for i, r in enumerate(rows) if cid in (r.get("check_id") or "")
        ]
        first_pass = next((i for i, res in hits if "pass" in res), None)
        if first_pass is None:
            continue
        if any(i > first_pass and "correction" in res for i, res in hits):
            continue
        failures.append(
            f"{cid} is marked † in 09 §6.14 — not runnable until its §12 tightening is resolved — "
            f"yet {RESULTS_CSV.relative_to(REPO)} row {first_pass + 2} records it **pass** with no "
            f"later **correction** row retracting it. A blocked check cannot have passed; the "
            f"evidence on that row belongs to some other check sharing the cell. Append a "
            f"correction row (the record is append-only), do not edit the pass away."
        )
    return failures


def check_deferrals_name_blockers() -> list[str]:
    """V-MET-006. "Deferred" is only honest when it names what it is waiting for.

    Without the blocker, the owner and the promotion condition, a deferral is a failure with a
    softer word for it, and nothing ever revisits it -- which is LSN-008. The second half is 09
    §9.6: a BLOCKING-ALWAYS check may not be deferred at all. A *level* of one may (V-CTN-020 has
    no Dataplane V2 at L3), but only when the check is green at some other level; otherwise the
    suite is unverified and the build is not shippable, whatever the row says.

    A row that declares **CLOSED** is asked a different question, not excused from being asked.
    The tempting shortcut -- skip struck-through rows -- rebuilds the exact bug `_deferral_rows`
    was written to kill: it hands every deferral an exit that consists of typing a word. So a
    closed row must instead clear the LSN-019 bar, which is *higher* than the one it leaves: name
    a runnable artifact, on disk, invoked by a chain or a workflow. "Deferred" means nothing can
    check this yet; "closed" therefore means something now does, and it has to be nameable. The
    promote-when column is where that name goes -- the column asks what would end the deferral,
    and for a closed row the answer is what did.
    """
    if not LEDGER.exists():
        return [f"{LEDGER.relative_to(REPO)} not found"]
    rows = _deferral_rows()
    if len(rows) < 5:
        return [
            f"VACUOUS: parsed {len(rows)} deferral rows; the table had 11 when this was written. "
            f"Either the section moved or the parser broke — a deferrals check that sees no "
            f"deferrals reports the cleanest possible build."
        ]

    evidence = _verification_evidence_rows()
    if len(evidence) < EVIDENCE_ROW_FLOOR:
        return [
            f"VACUOUS: the verification evidence corpus is {len(evidence)} rows; it was "
            f"{EVIDENCE_ROW_FLOOR}+ when this floor was written. The BLOCKING-ALWAYS arm below "
            f"asks whether a deferred check is green somewhere, and against an empty corpus the "
            f"answer is always no — every deferral would fail, for a reason that has nothing to "
            f"do with any deferral. Check whether the record moved again: "
            f"{LEDGER.name}, {LEDGER_ARCHIVE.relative_to(REPO)}, {RESULTS_CSV.relative_to(REPO)}."
        ]
    chain = regress_chain_text()
    failures = []
    for cells in rows:
        _date, subject, blocker, owner, promote = cells[:5]
        # Anchored to the START of the cell, not searched anywhere inside it. `"CLOSED" in
        # blocker.upper()` is satisfied by a blocker that merely uses the word -- "the L3
        # maintenance window closed", "closed beta" -- and the consequence is silent and
        # backwards: the row stops being asked for a promote-when condition, which is the
        # one question a still-open deferral exists to answer. The convention every closed
        # row in the ledger already follows is `**CLOSED <date> by ...**` as the cell's
        # first token, so requiring that costs nothing and closes the false positive.
        closed = bool(CLOSED_MARKER.match(blocker))
        # Only the promote-when question changes shape when a row closes. The first draft skipped
        # the whole loop for a closed row, and a mutation that blanked the owner of a CLOSED row
        # went straight through: typing one word had quietly dropped two of the three questions,
        # in a check whose own docstring promised the opposite. Who carried a deferral does not
        # stop being a fact when it ends -- that is the half of the register history lives in.
        asked = (("blocker", blocker), ("owner", owner))
        if not closed:
            asked += (("promote-when", promote),)
        for label, value in asked:
            if not value or value in {"-", "—", "TBD", "tbd", "?"}:
                failures.append(
                    f"deferral {subject!r} names no {label}. V-MET-006: a deferral without one "
                    f"is a failure that has been renamed rather than recorded."
                )
        if closed:
            failures += _closed_deferral_failures(subject, blocker, promote, chain)
        for cid in CHECK_ID.findall(subject):
            if not cid.startswith(BLOCKING_ALWAYS):
                continue
            # A level of a BLOCKING-ALWAYS check may be deferred only if the check is green
            # somewhere. Search the verification record for a pass row naming the same ID.
            green = any(cid in ln and "**pass**" in ln for ln in evidence)
            if green:
                continue
            # ...unless the check is not required yet. 09 §10 ratchets suites in by phase, and a
            # check whose phase has not arrived is not "deferred" in the §9.6 sense -- it is
            # unstarted, which is the normal state of all future work and not a defect. V-CTN-021
            # is required at phase 11; refusing it at phase 9 would make every build red for as
            # long as it takes to get there, and a check that is red for two phases straight is
            # one somebody routes around.
            #
            # The carve-out is narrow and it EXPIRES BY ITSELF. Both numbers must be known -- an
            # unparseable phase on either side is not a grant, it is the unknown case, and the
            # unknown case fails (LSN-038: a guard that cannot run must not score as a pass). And
            # it lapses the moment the phase arrives: at phase 11 this row starts failing with no
            # edit to anything, which is the property that makes it safe to grant at all. The
            # alternative -- a human remembering, at the phase 11 milestone, that a row written in
            # phase 9 was conditional -- is not a mechanism.
            required, current = _catalog_phases().get(cid), _current_phase()
            if required is not None and current is not None and current < required:
                continue
            due = (
                f"is required at phase {required} and the build is at phase {current}"
                if required is not None and current is not None
                else f"has no parseable phase in 09 §6 (required={required}, current={current}), "
                f"so it cannot be excused as not-yet-due"
            )
            failures.append(
                f"deferral {subject!r} defers {cid}, a BLOCKING-ALWAYS check; no row of the "
                f"verification log records it passing at any level, and it {due}. 09 §9.6: if it "
                f"cannot run, the build is not verifiable and that is a halt, not a row."
            )
    return failures


# ---------------------------------------------------------------------------------------------
# LSN-001 / LSN-002 / LSN-003 — an L2 script declares its preconditions, and the declaration is
# backed by code
# ---------------------------------------------------------------------------------------------
#
# All three lessons are the same shape: a run was green, the green was about the wrong artifact, and
# nobody could tell from the output. LSN-001 (stale image) recurred THREE times, LSN-002
# (grandfathered object) once, LSN-003 (the image-baked config.yaml shadowed by the operator-rendered
# ConfigMap) once. Each was closed against a precondition ID in binding.md, and binding.md does not
# execute.
#
# Declaring is the cheap half and it is not nothing: the recurrences all happened to authors who
# believed the precondition held and had never been asked to write down why. But a declaration alone
# is a comment, so each non-`none` answer must be BACKED by something in the script — a P1 that names
# a workload must be followed by a real digest assertion, a P3 that names an object must be followed
# by a delete or a server-dry-run. And `none` must carry a reason, because "none" with no argument is
# how a precondition gets waived by whoever is in the biggest hurry.

PRECOND_LINE = re.compile(
    r"^#\s+(P1|P3|P6) (image-under-test|admission-recreate|runtime-authoritative):\s*(.*)$"
)
PRECOND_CONT = re.compile(r"^#\s{5,}(\S.*)$")
PRECOND_FIELDS = {
    "P1": "image-under-test",
    "P3": "admission-recreate",
    "P6": "runtime-authoritative",
}
# How much argument a waiver owes. Short enough that an honest one-liner passes ("none — no
# first-party image participates"), long enough that a bare "none" or "n/a" cannot.
MIN_WAIVER_REASON = 30
# What counts as actually re-running admission on a fresh object: the helper, an explicit delete, or
# a server-side dry run (which admits in full and persists nothing, so nothing can be grandfathered).
P3_BACKING = ("p3_force_recreate", " delete ", "dry-run=server")
# The two members of P3_BACKING that are CODE IDENTIFIERS rather than English. A declaration can only
# contain one of these on purpose, so when it does, that specific mechanism must be in the code — see
# check_l2_scripts_declare_preconditions for why the disjunction above is not enough on its own.
# " delete " is deliberately excluded: "deletes the Deployment" is ordinary prose, and treating the
# word as a reference to the token would make the check fire on how a sentence is phrased.
P3_NAMED_MECHANISMS = ("p3_force_recreate", "dry-run=server")
# ConfigMap data keys the operator emits at runtime. A file of the same basename in the image is
# SHADOWED by this at runtime, so reading the file is reading an input, not the artifact.
CM_DATA_KEY = re.compile(r'"([A-Za-z0-9._-]+\.(?:ya?ml|json|toml))":\s*\w')
# How many scripts L2-CHAIN.txt named when this check was written. A ratchet, not a constant: it is
# here because the first version's floor was 5 against a 6-line chain, so deleting a line from the
# chain left the check green over the remaining five. A floor below the real count is a check that
# tolerates exactly the change it exists to notice.
# Raised 6 -> 16 on 2026-07-30 (P9-T9b-4), in the same commit that grew the chain, because the
# docstring above is an instruction and it had not been followed since the check was written: the
# floor sat at 6 against a 14-line chain, so eight lines could have left without a word. Phase 9's
# milestone will collapse its seven lines into verify-phase9.sh and this number must come down in
# THAT commit, deliberately, which is the whole point of it being here.
# Raised 16 -> 17 on 2026-07-30 (P9-T9b-5a): actor-overlay-admission-l2.sh, the line that executes
# the phase's admission ruling. Moved in the same commit as the line it counts, which is the only
# way this ratchet is ever allowed to move upward.
# Raised 17 -> 18 on 2026-07-30 (P9-T9b-5b-i): broker-execute-l2.sh, acceptance bullet (a). Same
# commit as the line, same rule.
# Raised 18 -> 19 on 2026-07-31 (P9-T9b-5b-ii-a): broker-refuse-l2.sh, V-BRK-018 and the journal half
# of acceptance bullet (d). Same commit as the line, same rule.
# Raised 19 -> 20 on 2026-07-31 (P9-T9b-5b-ii-b-1): broker-gate-l2.sh, V-REV-003 — the gated outcome,
# which neither the accepting line nor the refusing one can reach. Same commit as the line, same rule.
# Raised 20 -> 21 on 2026-07-31 (P9-T9b-5c): actor-grant-sweep-l2.sh, V-BRK-013's L2 half and Phase 9
# acceptance (e). The first line in the chain whose subject is the API server's AUTHORIZER rather
# than a workload — everything above asks what some pod did with the authority it holds, and this
# one asks what authority exists. Same commit as the line, same rule.
# Raised 21 -> 22 on 2026-07-31 (P9-T8b-4b-ii-2b-ii-b): undo-coverage-l2.sh, V-REV-001 at population
# scale. The line above it already scores V-REV-001 and is green; this one is the same check with a
# denominator of 35 instead of 1, which is the difference between "the undo planner worked once" and
# "the undo planner covers the corpus". Same commit as the line, same rule.
L2_CHAIN_FLOOR = 22
# How many scripts the TRANSITIVE scope held when it was widened (2026-07-25, P8-T8). A separate
# ratchet from the one above because the two guard different things: L2_CHAIN_FLOOR notices a line
# leaving L2-CHAIN.txt, this one notices a claim-making script leaving the closure — including one
# that leaves by being un-called, or by renaming its verdict functions, neither of which touches the
# chain file at all.
# Raised 16 -> 25 on 2026-07-30 (P9-T9b-4), same argument as the line above. The two scripts that
# joined the closure that day are verify-phase8.sh and verify-phase9.sh — the Phase 8 gate had been
# outside it for three phases because the standing chain line was a phase behind, so the one script
# that renders Phase 8's verdict was never asked which artifact it was judging.
# Raised 25 -> 26 on 2026-07-30 (P9-T9b-5a): actor-overlay-admission-l2.sh. It joins by being named
# on a chain line rather than by being reached from one, so both floors move together this time —
# they will not always, and the day they diverge is the day one of the two is doing work.
# Raised 26 -> 27 on 2026-07-30 (P9-T9b-5b-i): broker-execute-l2.sh, also named on a chain line, so
# both floors move together a second time.
# Raised 27 -> 28 on 2026-07-31 (P9-T9b-5b-ii-a): broker-refuse-l2.sh, named on a chain line; a third
# time together, and still not a rule that they must be.
# Raised 28 -> 29 on 2026-07-31 (P9-T9b-5b-ii-b-1): broker-gate-l2.sh, named on a chain line; a fourth
# time together.
# Raised 29 -> 30 on 2026-07-31 (P9-T9b-5c): actor-grant-sweep-l2.sh. It joins the closure twice over
# — named on an L2 chain line AND reached from verify-phase9.sh section F, which has been detecting
# it by artifact and failing while it was absent. A fifth time together.
# Raised 30 -> 31 on 2026-07-31 (P9-T8b-4b-ii-2b-ii-b): undo-coverage-l2.sh, named on an L2 chain
# line. A sixth time together.
L2_SCOPE_FLOOR = 31
# A script whose output is read as a verdict defines both of these. Derived rather than listed,
# because a curated roster of "the L2 scripts" is a roster someone must remember to extend, and the
# gap this widening closed existed for five phases precisely because nobody did. Both are required:
# up.sh provisions the cluster, lib/preconditions.sh and lib/substrate-capacity.sh are sourced for their
# helpers, and none of the three renders a verdict a stale image or grandfathered object could
# falsify.
VERDICT_FUNCS = (
    re.compile(r"^\s*(?:function\s+)?pass\s*\(\)", re.MULTILINE),
    re.compile(r"^\s*(?:function\s+)?bad\s*\(\)", re.MULTILINE),
)
# A reference to another shell script as it appears in code: bare, $REPO_ROOT-prefixed, or assembled
# from a loop variable — verify-phase5.sh reaches phases 2-4 as `verify-phase$p.sh`, and a resolver
# that only understood literals would have missed verify-phase4.sh entirely. Variables are resolved by
# GLOB, which over-approximates; that is the safe direction for a check that fails on an ABSENT
# declaration, since the worst case is asking a script to explain itself that did not have to.
SCRIPT_REF = re.compile(r"(dev/[A-Za-z0-9_./${}-]*\.sh)")


def _declared_preconditions(text: str) -> dict[str, str]:
    """{'P1': 'value with continuations folded in', ...} from a script's header block."""
    out: dict[str, str] = {}
    cur: str | None = None
    for line in text.splitlines():
        m = PRECOND_LINE.match(line)
        if m:
            cur = m.group(1)
            out[cur] = m.group(3).strip()
            continue
        if cur is None:
            continue
        c = PRECOND_CONT.match(line)
        if c:
            out[cur] += " " + c.group(1).strip()
        else:
            cur = None
    return out


def _l2_chain_scripts() -> list[Path]:
    """Every .sh named by a live line of L2-CHAIN.txt, INCLUDING ones that do not exist.

    Silently dropping a missing path would make a typo'd chain line look like a shorter chain, and
    a shorter chain is exactly what the floor check below is trying to notice.
    """
    if not L2_CHAIN.exists():
        return []
    out = []
    for line in L2_CHAIN.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in line.split():
            if token.endswith(".sh"):
                out.append(REPO / token)
    return out


def _script_refs(text: str) -> list[Path]:
    """Every dev/*.sh path this script's CODE names, with shell variables glob-expanded.

    Comments are stripped first for the same reason the P1/P3 backing checks strip them: a header
    that MENTIONS chaos-suite.sh in prose is not a script that runs it, and treating the two alike
    would pull every script named in a "NOT covered here" paragraph into scope.
    """
    out: list[Path] = []
    for ref in SCRIPT_REF.findall(_code_lines(text)):
        if "$" in ref:
            out.extend(sorted(REPO.glob(re.sub(r"\$\{?\w+\}?", "*", ref))))
        else:
            out.append(REPO / ref)
    return out


def _l2_scripts_in_scope() -> list[Path]:
    """L2-CHAIN.txt's own lines, plus every claim-making script an L2 run transitively reaches.

    The chain's six lines reach nine more through delegation — verify-phase7.sh runs phases 2, 3 and
    6; phase 6 runs the chaos suite and phase 5; phase 5 runs phases 2-4; four of them run
    negative-attenuation.sh. Those nine made L2 claims for five phases without declaring what artifact
    they were judging, which is how verify-phase2.sh could read a pod that predated the build under
    test and report it as evidence (LSN-002, found by this widening).

    Chain lines are kept even when they do not exist, so a typo'd chain line still reaches the
    floor and the existence check below. Transitive entries are traversed through regardless of
    whether they render verdicts — up.sh renders none but reaches reload-images.sh, and
    verify-phase7.sh reaches five scripts that do — and filtered out of the result at the end.
    """
    chain = _l2_chain_scripts()
    chain_set = set(chain)
    seen: set[Path] = set()
    order: list[Path] = []
    frontier = list(chain)
    while frontier:
        path = frontier.pop(0)
        if path in seen:
            continue
        seen.add(path)
        order.append(path)
        if path.exists():
            frontier.extend(_script_refs(path.read_text()))
    return [
        p
        for p in order
        if p in chain_set
        or (p.exists() and all(r.search(p.read_text()) for r in VERDICT_FUNCS))
    ]


def _shadowed_basenames() -> set[str]:
    """Basenames the operator writes into a ConfigMap, derived rather than listed.

    Hardcoding {'config.yaml'} would make this check a memorial to LSN-003 instead of a guard: the
    next renderer to emit a second key would not be covered, and nothing would say so.
    """
    keys: set[str] = set()
    src = REPO / "k8s-operator/internal"
    if not src.is_dir():
        return keys
    for go in src.rglob("*.go"):
        if go.name.endswith("_test.go"):
            continue
        keys |= set(CM_DATA_KEY.findall(go.read_text()))
    return keys


def _code_lines(text: str) -> str:
    """The script with its comments removed — what it DOES, not what it says about itself.

    Shell has no block comments, so dropping `#`-leading lines and everything after an unquoted
    ` #` is the whole job. Quote tracking is deliberately naive (it counts unescaped `'` and `"`
    before the `#`): the only cost of getting it wrong is keeping a line that is really a comment,
    which is the safe direction for a check that fails when the string is ABSENT.
    """
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        cut = -1
        sq = dq = 0
        for i, ch in enumerate(line):
            if ch == "'" and dq % 2 == 0:
                sq += 1
            elif ch == '"' and sq % 2 == 0:
                dq += 1
            elif ch == "#" and sq % 2 == 0 and dq % 2 == 0 and i > 0 and line[i - 1] in " \t":
                cut = i
                break
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def check_l2_scripts_declare_preconditions() -> list[str]:
    """LSN-001/002/003. Every L2 script says which artifact it is judging, and proves it.

    Scoped to the TRANSITIVE closure of L2-CHAIN.txt, not just its lines. It was chain-only until
    2026-07-25, on the argument that a declaration written on a script's behalf by its caller is a
    guess wearing the costume of a fact — which is true, and was an argument for reading the nine
    delegated scripts, not for leaving them unasked. P8-T8 read them. Two turned out to be reporting
    a pod that could predate the build under test as though it were evidence about it.
    """
    chain = _l2_chain_scripts()
    scripts = _l2_scripts_in_scope()
    if len(chain) < L2_CHAIN_FLOOR:
        return [
            f"VACUOUS: resolved {len(chain)} scripts from {L2_CHAIN.relative_to(REPO)}; there "
            f"were {L2_CHAIN_FLOOR} when this was written. A line left the chain, or the chain "
            f"format changed and this check went quiet over the scripts it stopped seeing. "
            f"Retiring an L2 check is a deliberate act (V-MET-004) — raise or lower this floor "
            f"in the same commit that changes the chain, never afterwards."
        ]
    if len(scripts) < L2_SCOPE_FLOOR:
        return [
            f"VACUOUS: the L2 closure resolved to {len(scripts)} claim-making scripts; there were "
            f"{L2_SCOPE_FLOOR} when the scope was widened. Something stopped being reachable, "
            f"stopped existing, or stopped looking like a verdict-renderer (pass()/bad()) — and "
            f"each of those is a script whose preconditions this check quietly stopped asking "
            f"about. Move this floor in the same commit that removes the script, never afterwards."
        ]

    shadowed = _shadowed_basenames()
    failures = []
    for path in scripts:
        rel = path.relative_to(REPO).as_posix()
        if not path.exists():
            failures.append(
                f"{L2_CHAIN.relative_to(REPO)} runs {rel!r}, which does not exist. The chain is "
                f"the definition site for what L2 evidence means; a line that cannot run is a "
                f"suite silently missing from every 'full L2 run' claim."
            )
            continue
        text = path.read_text()
        declared = _declared_preconditions(text)

        missing = [f"{k} {v}" for k, v in PRECOND_FIELDS.items() if k not in declared]
        if missing:
            failures.append(
                f"{rel} declares no {', '.join(missing)}. An L2 script that does not say which "
                f"artifact it is judging cannot be read as evidence about any particular one "
                f"(LSN-001/002/003)."
            )
            continue

        for key, value in sorted(declared.items()):
            low = value.lower()
            if low.startswith("none"):
                reason = value[4:].lstrip(" —-:").strip()
                if len(reason) < MIN_WAIVER_REASON:
                    failures.append(
                        f"{rel} waives {key} with {value!r} and no argument. A waiver is a claim "
                        f"about the script — say why the precondition cannot bite here."
                    )
                continue
            if len(value) < 12:
                failures.append(
                    f"{rel} declares {key} as {value!r}, which names nothing specific enough to "
                    f"check against."
                )

        # CODE, not prose. Every backing test below asks "does the script DO this", and the
        # PRECONDITIONS block it just read is a paragraph that SAYS this — "Asserted via
        # p1_assert_build_under_test" in verify-phase7.sh, in a comment, four lines above. A
        # whole-text substring search cannot tell the claim from the act, so deleting the call
        # and keeping the sentence left this check green (found by mutation, 2026-07-25; LSN-023).
        code = _code_lines(text)

        p1 = declared["P1"]
        if not p1.lower().startswith("none") and "p1_assert_build_under_test" not in code:
            failures.append(
                f"{rel} declares P1 against {p1.split('—')[0].strip()!r} but never calls "
                f"p1_assert_build_under_test. The declaration is ahead of the code: the script "
                f"asserts nothing about the digest it just said it depends on, which is LSN-001 "
                f"with a paragraph in front of it."
            )

        p3 = declared["P3"]
        if not p3.lower().startswith("none") and not any(b in code for b in P3_BACKING):
            failures.append(
                f"{rel} declares P3 against a live object but contains no delete, no "
                f"p3_force_recreate and no --dry-run=server. Admission does not evict what already "
                f"exists, so the claim would be about the rules in force when the object was "
                f"created (LSN-002)."
            )
        # LSN-023, one level deeper than the P1 rule above. The disjunction is satisfied by ANY
        # delete in the file, including a cleanup that runs only after an assertion has already
        # failed — measured 2026-07-25 by deleting verify-phase3.sh's p3_force_recreate call, which
        # the gate did not notice because line 85 tears down a rejected fixture. So: whatever
        # mechanism the declaration NAMES, the code must contain that one. A block saying
        # "p3_force_recreate deletes the Deployment" is a claim about a specific call, and a check
        # that reads the claim should be able to tell whether the call is there.
        for mech in P3_NAMED_MECHANISMS:
            if mech in p3 and mech not in code:
                failures.append(
                    f"{rel} declares P3 by naming {mech!r}, which appears nowhere in its code. "
                    f"Some other delete in the file satisfies the general backing test, so this "
                    f"would otherwise read as green while the mechanism the declaration promised "
                    f"is absent (LSN-002/LSN-023)."
                )

        p6 = declared["P6"]
        for base in shadowed:
            # A P6 that points at a FILE PATH whose basename the operator also renders is naming the
            # input, not the runtime artifact -- unless it says so and names the rendered one too.
            if re.search(rf"[\w./-]+/{re.escape(base)}\b", p6) and "configmap" not in p6.lower():
                failures.append(
                    f"{rel} declares P6 as a path ending in {base!r}, which the operator renders "
                    f"into a ConfigMap and mounts OVER the image-baked copy. The file is an input; "
                    f"the ConfigMap is the artifact that runs (LSN-003). Name the rendered one."
                )
    return failures


# ---------------------------------------------------------------------------------------------
# P9 — an assertion on controller-written state is reached by polling, never by waiting a while
# ---------------------------------------------------------------------------------------------

# A `.status` subtree is written by a controller, asynchronously, after the object is accepted. Any
# read of one is therefore a race unless something synchronises it, and the two primitives that do
# are a bounded poll and `kubectl wait --for=`. A `sleep` is neither: it encodes a guess about a
# controller's latency, and the guess is re-made every time the cluster is slower than the day the
# number was chosen.
#
# Written after two live instances on 2026-07-25, both in tenant-isolation-l2.sh. `.status.hard`
# was read with no wait at all, so on the Calico cluster -- where the quota controller took 21s and
# five polls -- the check reported the quota as capping NOTHING on all five axes while the very
# next section of the same run proved it binds. Five lines below, `.status.used` was read after a
# flat `sleep 3` against that same 21s controller, and had been passing on luck.
#
# The rule is deliberately about the READ and not about `sleep`. A ban on bare sleeps would flag
# the seven "let Calico program the policy" waits in this corpus, where no readiness field exists
# to poll and the sleep is the honest primitive -- a lint whose findings are mostly legitimate is
# one that teaches people to write exemptions.
STATUS_READ = re.compile(r"jsonpath=.\{\.status\.")
LOOP_OPEN = re.compile(r"(^|\s)(for|while|until)\b.*;\s*do\s*$|(^|\s)do\s*$")
LOOP_CLOSE = re.compile(r"^\s*done\b")
KUBECTL_WAIT = re.compile(r"\bwait\s+--for=")
FUNC_OPEN = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")
# Below this the regex has stopped matching and the check has stopped checking. A floor rather than
# an exact ratchet, unlike the assertion count: how MANY status reads the corpus makes is not
# itself a property worth defending -- that none of them is unsynchronised is.
L2_STATUS_READ_FLOOR = 6


def _lines_inside_loops(lines: list[str]) -> tuple[list[bool], dict[str, bool]]:
    """For each line: is it inside a loop body? And: which functions are CALLED from inside one?

    The second half exists for `chaos-suite.sh`'s `is_ready()`, which reads a pod's Ready condition
    and is correct precisely because both of its call sites are poll loops. Judging the read by the
    function it sits in would flag it; judging it by how the function is used is the real question.
    """
    in_loop, depth = [], 0
    for line in lines:
        code = "" if line.strip().startswith("#") else line.split(" #")[0]
        if LOOP_CLOSE.search(code):
            depth = max(0, depth - 1)
        in_loop.append(depth > 0)
        if LOOP_OPEN.search(code):
            depth += 1
    called_in_loop: dict[str, bool] = {}
    for i, line in enumerate(lines):
        code = "" if line.strip().startswith("#") else line.split(" #")[0]
        m = FUNC_OPEN.match(code)
        if m:
            called_in_loop.setdefault(m.group(1), False)
    for name in called_in_loop:
        for i, line in enumerate(lines):
            code = "" if line.strip().startswith("#") else line.split(" #")[0]
            if FUNC_OPEN.match(code):
                continue
            if re.search(rf"\b{re.escape(name)}\b", code) and in_loop[i]:
                called_in_loop[name] = True
                break
    return in_loop, called_in_loop


def check_l2_status_reads_are_polled() -> list[str]:
    """Precondition P9. Controller-written state is polled for, not slept on."""
    scripts = [p for p in _l2_scripts_in_scope() if p.exists()]
    failures, total = [], 0
    for p in scripts:
        lines = p.read_text().splitlines()
        in_loop, called_in_loop = _lines_inside_loops(lines)
        enclosing = None
        for i, line in enumerate(lines):
            code = "" if line.strip().startswith("#") else line.split(" #")[0]
            m = FUNC_OPEN.match(code)
            if m:
                enclosing = m.group(1)
            elif code.strip() == "}":
                enclosing = None
            if not STATUS_READ.search(code):
                continue
            total += 1
            if in_loop[i] or called_in_loop.get(enclosing or "", False):
                continue
            if KUBECTL_WAIT.search("\n".join(lines[max(0, i - 20) : i])):
                continue
            failures.append(
                f"{p.relative_to(REPO)}:{i + 1} reads a `.status` field with nothing "
                f"synchronising it — no enclosing poll, no `kubectl wait --for=` above it. A "
                f"controller writes that subtree after admission, so an unsynchronised read "
                f"returns whatever has landed so far, and an empty one is indistinguishable from "
                f"the property genuinely being absent."
            )
    if total < L2_STATUS_READ_FLOOR:
        return [
            f"VACUOUS: found {total} `.status` reads across {len(scripts)} L2 scripts; there were "
            f"10 when this was written. The pattern stopped matching, so every read now looks "
            f"synchronised by virtue of being invisible."
        ]
    return failures


# ---------------------------------------------------------------------------------------------
# P3, second half — a pod under an admission assertion is reached by ownership, not by selector
# ---------------------------------------------------------------------------------------------

# `p3_force_recreate` returns the moment the DEPLOYMENT's uid changes. Its pods are garbage-collected
# after that, asynchronously, so the pod belonging to the generation P3 has just deleted is still
# listed -- and still carries no deletionTimestamp to filter on -- at the instant the caller starts
# looking. A selector poll therefore hands back the OLD pod, which is the exact object P3 exists to
# keep the assertion away from, and an `.items[N]` read re-resolves that changing list once per field.
#
# Written after verify-phase3.sh failed 2 runs in 3 on 2026-07-25. Every failure was an EMPTY read
# (`pod SA is ''`, `pod image ''`), never a wrong value; one run read the SA successfully and then got
# '' for the image and the tier one kubectl call later, as GC removed the pod mid-sequence.
# verify-phase2.sh carried a byte-identical block and was green purely because GC happened to be
# quicker there -- the defect and the luck are indistinguishable from inside a passing run. That was
# the third instance in one day of `.items[0]`-after-a-selector being a guess, the first having been
# in P1's own pod read, which is the point at which this harness stops fixing instances.
#
# The rule is scoped to scripts that call `p3_force_recreate`, and deliberately not to every
# `.items[` in the corpus. Reading `.items[0]` off `get nodes` on a single-node Kind is a different
# and much weaker claim, and a lint whose findings are mostly legitimate is one that teaches people to
# write exemptions. Where the script has just deleted one generation of a workload and is about to
# assert on the next, the index is never defensible.
P3_RECREATE_CALL = re.compile(r"(?<![\w-])p3_force_recreate\s+\S")
ITEMS_INDEX_READ = re.compile(r"jsonpath=.\{\.items\[")
# Two callers today. A floor rather than a ratchet: how many suites force-recreate is not itself a
# property worth defending, but a rule that applies to nothing has stopped being evidence (V-MET-014).
P3_RECREATE_CALLER_FLOOR = 2


def check_p3_pods_resolved_by_ownership() -> list[str]:
    """Precondition P3. A script that force-recreates resolves pods by ownership, not by list index."""
    callers, failures = [], []
    for p in sorted(_l2_scripts_in_scope()):
        if not p.exists():
            continue
        lines = p.read_text().splitlines()
        code = ["" if ln.strip().startswith("#") else ln.split(" #")[0] for ln in lines]
        if not any(P3_RECREATE_CALL.search(c) for c in code):
            continue
        callers.append(p)
        for i, c in enumerate(code):
            if ITEMS_INDEX_READ.search(c):
                failures.append(
                    f"{p.relative_to(REPO)}:{i + 1} indexes into `.items[]` in a script that calls "
                    f"`p3_force_recreate`. The recreate returns when the Deployment's uid changes, "
                    f"not when its old pods are gone, so that list still contains the generation P3 "
                    f"just deleted — and re-resolving it per field lets GC empty it mid-sequence. "
                    f"Resolve the pod once with `p3_pod_of_deploy` and read every field from that "
                    f"name (LSN-024)."
                )
    if len(callers) < P3_RECREATE_CALLER_FLOOR:
        return [
            f"VACUOUS: only {len(callers)} script(s) in the L2 scope call `p3_force_recreate`; there "
            f"were {P3_RECREATE_CALLER_FLOOR} when this was written. Either the precondition stopped "
            f"being used or the call pattern stopped matching, and in both cases this check is now "
            f"reporting green about nothing."
        ]
    return failures


# ---------------------------------------------------------------------------------------------
# P10 — a script that reads a cluster asserts the cluster can still run the experiment
# ---------------------------------------------------------------------------------------------

# LSN-026. verify-phase8.sh's first end-to-end run reported that tenant isolation did not hold, that
# the egress default-deny did not hold, and that chaos C2 failed to replace a deleted pod. All three
# were false: kube-scheduler and kube-controller-manager were in CrashLoopBackOff, so fixture pods
# stayed Pending and new namespaces never got a `default` ServiceAccount. Every enforcement check in
# this corpus has the same shape -- create a fixture, attempt the thing that must be denied, observe
# that it was denied -- and when the fixture never runs, the attempt never happens, which is
# indistinguishable from a policy working right up to the moment the script concludes it is ABSENT.
#
# P10 was written that day and wired into verify-phase8.sh alone. That covered the phase gate and
# nothing else: every other script in the L2 closure is independently runnable -- which is how
# LSN-025 was found -- and on a sick cluster each still produces its own false failure. A
# precondition installed at one caller on everyone else's behalf is the shape L2_SCOPE_FLOOR exists
# to reject, so this makes it a rule.
#
# SCOPED BY WHAT THE SCRIPT DOES, not by a roster. A closure script that never invokes kubectl makes
# no claim about a cluster and cannot assert one is healthy -- otel-endpoint.sh reads a Dockerfile
# and an entrypoint. That is a derived predicate, not an exemption: adding a kubectl call to it puts
# it in scope automatically, which is the opposite of a list someone must remember to extend.
# CODE lines only (LSN-023): a PRECONDITIONS paragraph saying "P10 is asserted" is not the call.
P10_CALL = re.compile(r"(?<![\w-])p10_assert_control_plane_healthy\s+\S")
# Fifteen of the sixteen closure scripts touch a cluster today. A floor, not a ratchet: how many L2
# scripts exist is not itself a property worth defending, but a rule that applies to nothing has
# stopped being evidence (V-MET-014), and the way this one would silently empty out is a change to
# _l2_scripts_in_scope or to the kubectl predicate rather than a deletion anyone would notice.
P10_CALLER_FLOOR = 15


def check_l2_scripts_assert_cluster_health() -> list[str]:
    """Precondition P10 / LSN-026. Every L2 script that reads a cluster first proves it converges."""
    callers, failures = [], []
    for p in sorted(_l2_scripts_in_scope()):
        if not p.exists():
            continue
        code = _code_lines(p.read_text())
        if "kubectl" not in code:
            continue
        callers.append(p)
        if not P10_CALL.search(code):
            failures.append(
                f"{p.relative_to(REPO)} reads a cluster and never calls "
                f"p10_assert_control_plane_healthy. Its verdict cannot distinguish a security "
                f"property that is missing from a control plane that has stopped converging, and "
                f"it reports the first (LSN-026). Call P10 before the first claim; rc 2 is "
                f"could-not-run, never a failure."
            )
    if len(callers) < P10_CALLER_FLOOR:
        return [
            f"VACUOUS: only {len(callers)} script(s) in the L2 scope read a cluster; there were "
            f"{P10_CALLER_FLOOR} when this was written. Either the closure shrank or the kubectl "
            f"predicate stopped matching, and in both cases this check is now reporting green "
            f"about nothing. Move this floor in the same commit that removes the script."
        ]
    return failures


# ---------------------------------------------------------------------------------------------

CAPACITY_LIB = REPO / "dev" / "lib" / "substrate-capacity.sh"
# `# @covers: <command>` on the line above `assert_<name>_capacity() {`. Parsed, not hardcoded --
# see the docstring for why the map has to live in the library and not here.
COVERS = re.compile(
    r"^#\s*@covers:\s*(?P<cmd>.+?)\s*$\n(?P<fn>assert_\w*capacity)\s*\(\)\s*\{",
    re.MULTILINE,
)


def check_cluster_creating_scripts_assert_capacity() -> list[str]:
    """LSN-027. Anything that creates a cluster measures its substrate first, from one definition site.

    Every resource `lib/substrate-capacity.sh` measures was found the hard way, and each was then
    written into whichever script happened to be in hand -- which is how the memory floor briefly
    existed in two files with two different values. The deeper problem is that a preflight grown one
    incident at a time only ever measures the PREVIOUS incident: the memory check was written after
    LSN-026, and the very next new cluster died on inotify while that check printed 5.7Gi of
    headroom. A green preflight is not neutral there; it actively points at the wrong resource.

    So the rule is not "have a preflight", it is "call THE preflight" -- one place to fix a floor,
    one place to add the next resource, and a new `up-*.sh` cannot be written without it.

    SUBSTRATE-NEUTRAL BY CONSTRUCTION. The lesson is about preflights, not about Colima, and the
    check has to outlive the host that taught it -- otherwise deleting Kind silently retires it.
    So the command -> preflight map is PARSED out of the library's `@covers:` lines rather than
    written here. Adding a substrate is one function plus one comment in one file; retiring one is
    a deletion, after which no script matches that command and the map shrinks on its own. A table
    here would instead go quietly green about a pattern nothing uses any more.

    Scanned on code lines only (LSN-023): a comment mentioning the assertion is not a call. An
    `echo` containing a create command DOES trip this, and deliberately -- it caught one on its
    first run. A remediation string telling the reader to hand-roll a cluster is itself the defect,
    because a hand-rolled cluster is missing whatever up.sh does after the create; up.sh is the
    only path that produces a working cluster, so it is the only thing a message may name.
    """
    if not CAPACITY_LIB.exists():
        return [
            f"VACUOUS: {CAPACITY_LIB.relative_to(REPO)} does not exist, so there is no preflight to "
            f"require. If the library moved, move this constant with it -- in the same commit."
        ]
    covers = {m.group("cmd"): m.group("fn") for m in COVERS.finditer(CAPACITY_LIB.read_text())}
    if not covers:
        return [
            f"VACUOUS: {CAPACITY_LIB.relative_to(REPO)} declares no `# @covers: <command>` line "
            f"above any assert_*_capacity function, so this check knows of no cluster-creating "
            f"command to police and passes everything. Either the annotation was dropped or the "
            f"function naming changed -- fix the parser, not the finding."
        ]

    creators: dict[Path, list[str]] = {}
    for p in sorted((REPO / "dev").rglob("*.sh")):
        code = _code_lines(p.read_text())
        matched = [cmd for cmd in covers if cmd in code]
        if matched:
            creators[p] = matched
    if not creators:
        return [
            f"VACUOUS: no script under dev/ runs any of the cluster-creating commands the library "
            f"claims to cover ({', '.join(sorted(covers))}), so this check has nothing to enforce. "
            f"Either clusters are now created somewhere this does not look, or a substrate was "
            f"retired without retiring its @covers line -- fix the search, not the finding."
        ]

    failures = []
    for p, cmds in creators.items():
        code = _code_lines(p.read_text())
        rel = p.relative_to(REPO)
        for cmd in cmds:
            fn = covers[cmd]
            if fn not in code:
                failures.append(
                    f"{rel} runs `{cmd}` without calling {fn} (lib/substrate-capacity.sh). Source "
                    f"it and call it before the create, or the next capacity outage is diagnosed "
                    f"from scratch (LSN-026, LSN-027)."
                )
            elif "lib/substrate-capacity.sh" not in code:
                failures.append(
                    f"{rel} calls {fn} but never sources lib/substrate-capacity.sh, so it is "
                    f"either relying on a caller's environment or has its own copy. One definition "
                    f"site (V-MET-013) is the whole point of the check."
                )
    return failures


def check_platform_idioms_are_gnu_first() -> list[str]:
    """LSN-029. Where BSD and GNU spell a flag differently, the GNU arm is tried FIRST.

    Every shell script in this tree was written on a Mac and now runs on Linux -- Cloud Build
    containers, the ubuntu-latest CI runner, cluster nodes. For most idioms the difference is
    cosmetic. For a few, the same letter means two unrelated things, and those do not degrade, they
    LIE:

        stat -f   BSD: a format string (`%m` = mtime).   GNU: FILESYSTEM status; `%m` is invalid.
        date -r   BSD: render this epoch.                GNU: render this FILE's mtime.
        date -j   BSD: do not set the clock.             GNU: no such flag.

    `stat -f` is the dangerous one and it is worth spelling out why, because the failure survives
    the guard people reach for. GNU `stat -f` writes the filesystem block to STDOUT and only then
    exits 1. So in the natural-looking

        stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null

    the `2>/dev/null` catches nothing (the noise came out of stdout), `||` fires, and the epoch is
    appended to six lines of block-size trivia. The caller's `[ "$m" -ge N ]` then exits 2 -- not
    true, not false, and silent. P1 answered STALE for every build on Linux for as long as that
    line existed, and nothing noticed while the inner loop was Mac-only.

    WHY A LINT AND NOT "CI RUNS ON LINUX NOW". CI running on Linux is what FOUND this, on its first
    green-to-red transition after the move. It is not what closes it: it only catches an idiom on a
    path some test happens to execute, and the three `date -r` calls it did not catch sat inside a
    P1 failure message -- a branch no green run enters, on the platform where P1 now runs. LSN-019
    is explicit that a lesson closed against "somebody would notice" is not closed. This reads the
    text instead, so an unexecuted branch is judged the same as an executed one.

    THE RULE IS ORDER, NOT ABSENCE. Both arms are wanted -- the tree still has to work on the Mac
    it is developed on. So a BSD form is fine exactly when its GNU counterpart appears within the
    preceding few code lines, which covers both shapes in use here: the one-line `A || B` chain,
    and a multi-line `case` that validates A's output before falling through to B. Scanned on code
    lines only (LSN-023), or this docstring and the ones in preconditions.sh would trip it.
    """
    # (gnu, bsd, label). `date -u -d` is the same GNU arm with a timezone flag wedged in.
    PAIRS = (
        (re.compile(r"\bstat\s+-c\b"), re.compile(r"\bstat\s+-f\b"), "stat -c before stat -f"),
        (
            re.compile(r"\bdate\s+(?:-u\s+)?-d\b"),
            re.compile(r"\bdate\s+(?:-u\s+)?-r\b"),
            "date -d before date -r",
        ),
        (
            re.compile(r"\bdate\s+(?:-u\s+)?-d\b"),
            re.compile(r"\bdate\s+-j\b"),
            "date -d before date -j",
        ),
    )
    WINDOW = 12  # code lines; wide enough for a case/esac fallthrough, narrow enough to mean pairing

    # Every TRACKED `*.sh`, not a directory list. A directory list was the first draft and it left
    # eight scripts unscanned, `deploy/shared/*.sh` among them -- and those run ONLY on Linux,
    # inside the containers, where a BSD-first idiom gets no second chance. A lint whose scope is
    # enumerated goes quietly partial the first time a script lands somewhere new.
    #
    # git rather than rglob, for one specific reason: `k8s-operator/scripts/vars.sh` is gitignored
    # and holds live secrets in plaintext. This check prints the offending LINE in its failure
    # message, so a bare rglob would put a lint one bad line away from printing a token into CI
    # logs. `gitcorpus` adds the new-but-not-ignored half ([[LSN-050]]): a script written by the
    # current unit is the one whose BSD-vs-GNU idioms nothing has ever run.
    try:
        scripts = [REPO / p for p in repo_files(REPO, "*.sh")]
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"VACUOUS: could not enumerate shell scripts ({exc}); nothing was scanned."]
    if len(scripts) < 60:
        return [
            f"VACUOUS: found {len(scripts)} tracked shell scripts to scan; this tree had 73 when "
            f"the check was written. Either the corpus moved or the enumeration broke."
        ]

    failures = []
    for path in scripts:
        raw = path.read_text()
        lines = _code_lines(raw).splitlines()
        # _code_lines drops comment lines, so its indices are not file line numbers. Recover the
        # real one by matching the text back -- a reviewer needs a number they can jump to, and a
        # wrong-but-plausible number is the same species of defect this check is about.
        original = raw.splitlines()
        for gnu, bsd, label in PAIRS:
            for i, line in enumerate(lines):
                if not bsd.search(line):
                    continue
                if any(gnu.search(w) for w in lines[max(0, i - WINDOW) : i + 1]):
                    continue
                real = next(
                    (n for n, r in enumerate(original, 1) if r.strip() == line.strip()), None
                )
                where = f"{path.relative_to(REPO)}:{real}" if real else path.relative_to(REPO)
                failures.append(
                    f"{where} uses a BSD-only idiom with no GNU arm within {WINDOW} code lines "
                    f"above it — wanted {label}. On Linux the BSD form does not fail cleanly, so "
                    f"the caller gets a plausible wrong answer rather than an error:\n"
                    f"        {line.strip()}"
                )
    return failures


# ---------------------------------------------------------------------------------------------
# LSN-050 — a pre-commit check may not enumerate its corpus from the index alone
# ---------------------------------------------------------------------------------------------

# Every `.py` and `.sh` under `dev/`, not just the ones currently wired into the chain. The lesson
# is that "the eighth script written next month copies the seventh": a rule that only binds once a
# script reaches L0-CHAIN.txt lets the defect be written, reviewed and merged first, and catches it
# at the moment it is least convenient to fix. Scanning the whole tree is strictly stronger and
# costs nothing, because `dev/` is exactly the L0 check tree.
LS_FILES_ROOT = "dev"


def _ls_files_lists(src: str) -> list[list[str]]:
    """Every list/tuple literal in a Python source that spells `ls-files`, as its string elements.

    Parsed with `ast` rather than grepped, for [[LSN-023]]: this file, `gitcorpus.py` and every
    docstring in the seven converted checks discuss `git ls-files --others` at length in prose, and
    a grep cannot tell the sentence from the argv. An AST sees only real string literals, so a
    comment explaining the rule can never satisfy or break it.

    Non-constant elements (`*pathspecs`, `str(root)`) are dropped rather than failing the parse:
    they cannot be `--others`, and their presence says nothing either way.
    """
    out: list[list[str]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        strings = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if any("ls-files" in s for s in strings):
            out.append(strings)
    return out


def check_l0_corpus_is_not_index_only() -> list[str]:
    """LSN-050. No check under `dev/` may enumerate its inputs with `git ls-files` and no `--others`.

    `ls-files` without `--others` lists the INDEX. A file created by the current unit is not in the
    index until it is staged, so a check written that way is blind to exactly the code nothing has
    ever reviewed — and silent about it, because a pass over a corpus missing the file under test
    prints the same thing as a pass over one containing it. `api-group-single-sourced.py` scanned
    115 files and passed while the defect it exists to catch sat in an untracked file two
    directories away.

    The sanctioned form is `gitcorpus.repo_files`, which is also the only place in the tree allowed
    to spell the flags. This check does not require that helper by name — requiring a call site to
    use one function is a shape a refactor breaks for good reasons — it requires the property the
    helper has.
    """
    failures: list[str] = []
    root = REPO / LS_FILES_ROOT
    scanned = 0
    seen_calls = 0

    for path in sorted(root.rglob("*")):
        if path.suffix not in (".py", ".sh") or not path.is_file():
            continue
        rel = path.relative_to(REPO)
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "ls-files" not in src:
            continue
        scanned += 1

        if path.suffix == ".py":
            try:
                lists = _ls_files_lists(src)
            except SyntaxError as exc:
                failures.append(f"{rel}: could not parse to check its corpus enumeration ({exc})")
                continue
            for argv in lists:
                seen_calls += 1
                if "--others" not in argv:
                    failures.append(
                        f"{rel} builds `git {' '.join(a for a in argv if a != 'git')}` — "
                        f"`ls-files` with no `--others` lists the index, so this check is blind to "
                        f"any file the current unit has written and not yet staged (LSN-050). Use "
                        f"`gitcorpus.repo_files`, or add `--others --exclude-standard`."
                    )
        else:
            for n, line in enumerate(src.splitlines(), 1):
                if "ls-files" not in line or line.lstrip().startswith("#"):
                    continue
                seen_calls += 1
                if "--others" not in line:
                    failures.append(
                        f"{rel}:{n} runs `ls-files` with no `--others`, which lists the index and "
                        f"not the working tree (LSN-050):\n        {line.strip()}"
                    )

    if scanned == 0 or seen_calls == 0:
        return [
            f"VACUOUS: found {scanned} file(s) under {LS_FILES_ROOT}/ mentioning ls-files and "
            f"{seen_calls} enumeration(s) in them; this tree had 10 when the rule was written, so "
            f"the parser has stopped seeing the call sites rather than the tree having lost them"
        ]
    return failures


# ---------------------------------------------------------------------------------------------
# The open count has three copies, and only one of them is read by SELECT
# ---------------------------------------------------------------------------------------------

# `**Status:** closed` and `**Status: closed**` are both in use in the bodies; the bold falls in a
# different place and the word is the same. Anchored to the literal `**Status:` so a sentence about
# status cannot match.
BODY_STATUS = re.compile(r"\*\*Status:(?:\*\*)?\s*([A-Za-z]+)")
TALLY = re.compile(r"^\*\*Open:\s*(\d+)\s+of\s+(\d+)\*\*(.*)$", re.MULTILINE)


def _lesson_bodies(text: str) -> list[tuple[str, str]]:
    """Each `## LSN-nnn` heading paired with the text up to the next heading."""
    marks = [(m.group(1), m.start()) for m in LESSON_BODY.finditer(text)]
    out = []
    for i, (lid, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out.append((lid, text[start:end]))
    return out


def check_lesson_status_matches_its_index_row() -> list[str]:
    """LSN-044's own tail: the open count lives in three places and two of them go stale.

    `harness-run` §2 sends the next invocation to `harness-improve` and nothing else when open
    lessons exceed the `binding.md` threshold, so the count is a control-flow input, not
    bookkeeping. It is written down three times in this one file — the index table's Status column,
    each body's `**Status:**` header, and the `**Open: N of M**` tally — and the three are edited by
    hand at different moments. On 2026-07-29 they disagreed: LSN-044's index row said `closed` and
    its body header still said `open`, and the improvement pass that read the bodies counted 6 open
    against a real 5, tripped a threshold that had not been crossed, and had to record the miscount
    in the ledger.

    The index table is the definition site — every other check in this file reads it. This check
    makes the other two copies agree with it:

    - a body that restates its status must restate the SAME status;
    - the tally's N must equal the number of `open` rows, its M the number of rows, and the IDs it
      names in parentheses must be exactly the open ones.

    A body with no `**Status:**` header at all passes. That is deliberate and it is the honest
    scope: 32 of the 50 bodies have never carried one, the index is where a reader is sent, and a
    body that says nothing cannot say something false. Requiring the header everywhere would be a
    different change, and it would add 32 more hand-maintained copies of the value this check
    exists to stop duplicating.
    """
    if not LESSONS.exists():
        return [f"{LESSONS.relative_to(REPO)} not found"]

    text = LESSONS.read_text()
    rows = {lid: status.strip().lower() for lid, _t, _s, status, _c in LESSON_ROW.findall(text)}
    bodies = _lesson_bodies(text)
    if len(rows) < 15 or len(bodies) < 15:
        return [
            f"VACUOUS: parsed {len(rows)} index row(s) and {len(bodies)} body/bodies; this file "
            f"had 50 of each when the rule was written, so the parser stopped seeing the table "
            f"rather than the table having emptied. Fix the parser, not the file."
        ]

    failures = []
    restated = 0
    for lid, body in bodies:
        m = BODY_STATUS.search(body)
        if not m:
            continue
        restated += 1
        said = m.group(1).strip().lower()
        want = rows.get(lid)
        if want is None:
            continue  # check_every_lesson_has_an_index_row owns this one
        if said != want:
            failures.append(
                f"{lid}: the index row says `{want}` and the body header says `{said}`. The index "
                f"is the definition site and the count derived from it decides whether the next "
                f"invocation is an improvement pass — a stale body header has already produced one "
                f"miscounted threshold trip."
            )

    if restated == 0:
        return failures + [
            "VACUOUS: not one lesson body carries a `**Status:**` header, so the body arm of this "
            "check compared nothing. 18 did when the rule was written; the header format changed."
        ]

    open_ids = sorted(lid for lid, st in rows.items() if st == "open")
    tally = TALLY.search(text)
    if not tally:
        failures.append(
            "no `**Open: N of M**` tally line found. It is what an orientation reads before "
            "deciding whether the threshold is crossed; without it the count is recomputed by "
            "hand, which is how it went wrong."
        )
        return failures

    n, m, rest = int(tally.group(1)), int(tally.group(2)), tally.group(3)
    if n != len(open_ids) or m != len(rows):
        failures.append(
            f"the tally says `Open: {n} of {m}` and the index table holds {len(open_ids)} open of "
            f"{len(rows)}. Open rows: {open_ids or '(none)'}."
        )
    named = set(re.findall(r"LSN-\d+", rest))
    if named and named != set(open_ids):
        failures.append(
            f"the tally names {sorted(named)} as the open lessons and the index table's open rows "
            f"are {open_ids or '(none)'}."
        )
    return failures


METRICS_HEADER = "| Date | Phase | Escape rate |"

# A `\|` inside a cell is a literal pipe, not a column break — the rows quote shell pipelines. This
# is how the P9-T7d-5 row lost a column for a day: `envsubst | kubectl apply -f -` inside a code
# span, which markdown splits and a code span does not protect.
CELL_BREAK = re.compile(r"(?<!\\)\|")


def check_metrics_rows_are_complete() -> list[str]:
    """A metrics row short of columns drops a value silently, and the pass reads the gap as absence.

    `SELF-IMPROVEMENT` §6 names the metrics table as the improvement pass's input set, and open
    lessons is one of its columns. The rows are long — several run past 2 000 characters — and a
    checkpoint interrupted partway through writes a row that renders perfectly in markdown with its
    last six cells simply gone. That is what happened to the P9-T7c-4a row: escape rate present,
    rework / halts / open lessons / deferrals / coverage / cycle time all missing, and nothing said
    so, because a short markdown row is legal markdown.

    Every data row must carry exactly as many cells as the header. Nothing here reads the values;
    the property is that they exist to be read.
    """
    if not LEDGER.exists():
        return [f"{LEDGER.relative_to(REPO)} not found"]

    lines = LEDGER.read_text().splitlines()
    header = next((n for n, l in enumerate(lines) if l.startswith(METRICS_HEADER)), None)
    if header is None:
        return [
            f"VACUOUS: no metrics table header starting {METRICS_HEADER!r} in "
            f"{LEDGER.relative_to(REPO)}; the table moved or was renamed and this check stopped "
            f"checking."
        ]

    want = len(CELL_BREAK.findall(lines[header]))
    failures, rows = [], 0
    for n in range(header + 2, len(lines)):  # +2 skips the |---| separator
        line = lines[n]
        if not line.startswith("|"):
            if line.strip():
                break  # the table ended at a non-blank, non-row line
            continue  # a blank line inside the table; the ledger has one
        rows += 1
        got = len(CELL_BREAK.findall(line))
        if got != want:
            failures.append(
                f"{LEDGER.relative_to(REPO)}:{n + 1} is a metrics row with {got - 1} cell(s) and "
                f"the header declares {want - 1}. The missing cells render as empty and read as "
                f"'not measured': {line[:90]}…"
            )
    if rows < 5:
        return [
            f"VACUOUS: found {rows} metrics row(s) under the header; there were 22 when the rule "
            f"was written, so the row scan stopped seeing the table."
        ]
    return failures


# ---------------------------------------------------------------------------------------------
# V-CTN-037 — a test-only RBAC grant never leaves dev/
# ---------------------------------------------------------------------------------------------

# The label that says "this grant exists so a suite can observe something, and for no other
# reason". Discovery is BY THIS MARKER, not by a path or a filename: a rule keyed to
# `dev/verify/fixtures/actor-tenant-grant.yaml` is a headcount of one ([[LSN-036]]) and says
# nothing about the second fixture, which is the one nobody reviews.
TEST_ONLY_MARKER = "kube-agents/test-only-grant"
RBAC_KIND = re.compile(
    r"^kind:\s*(Role|ClusterRole|RoleBinding|ClusterRoleBinding)\s*$", re.MULTILINE
)
# Cluster-scoped RBAC cannot be confined to a test namespace by construction, so a test-only grant
# may not be either of these — there is no teardown that makes a ClusterRole safe to have existed.
CLUSTER_SCOPED_RBAC = {"ClusterRole", "ClusterRoleBinding"}
# [[LSN-004]]'s list, and the reason invariant 7 uses an allow-list. None of these is a "write" and
# every one of them is worse than most writes: `escalate` lifts the RBAC escalation-prevention
# check, `bind` grants any Role the holder can name, `impersonate` is every identity at once, and
# `*` is all three plus whatever the next API group adds.
ESCALATION_VERBS = {"escalate", "bind", "impersonate", "*"}
RBAC_API_GROUP = "rbac.authorization.k8s.io"


# What a `kubectl apply -f` will take as a manifest. `.template`/`.tmpl` are included for the same
# reason `agent_rbac_documents` includes them: a verb that only appears after envsubst is still a
# granted verb, and the templates are what provisioning actually applies.
YAML_SUFFIXES = (".yaml", ".yml", ".yaml.template", ".yaml.tmpl")


def _rbac_documents(corpus: dict[str, str]) -> list[tuple[str, int, str, str]]:
    """(repo-relative path, doc index, kind, document text) for every RBAC document in a manifest.

    Subset by suffix rather than by a second `git ls-files` pass: the corpus is already the whole
    worktree, and two enumerations of the same tree are two chances for them to disagree.
    """
    out = []
    for rel, text in corpus.items():
        if not rel.endswith(YAML_SUFFIXES):
            continue
        for n, doc in enumerate(_yaml_docs(text)):
            m = RBAC_KIND.search(doc)
            if m:
                out.append((rel, n, m.group(1), doc))
    return out


def check_test_only_grants_are_confined() -> list[str]:
    """V-CTN-037. A grant that exists only for a test may exist only where tests live.

    P9-T8b-4b-ii-2a had to give the deployed actor identity real authority over real objects on a
    real cluster, because the shipped 06 §2.2.1 grant deliberately gives it none and every envelope
    therefore died at step 3 with a 403 — which meant steps 4 through 9 of the broker pipeline had
    never once run against a live API server, and V-REV-001's "shadow mode never mutates" had L1
    evidence and nothing above it. Proving a system does not mutate requires first letting it get
    far enough to try.

    So the fixture is legitimate and the risk is entirely about WHERE IT ENDS UP. A test-only grant
    that drifts into an install path is the single worst outcome available here: it is a real
    over-grant, on every cluster the provisioning scripts touch, wearing a filename that says it is
    only for tests. Nobody would review it again, because it was reviewed once, in a test.

    Five properties, all derived from the marker rather than from a path:

      P1  the marker appears only under `dev/`, or in prose (`.md`)
      P2  every RBAC document in a YAML under `dev/` carries it — so P1 cannot be dodged by
          simply leaving the next fixture unmarked
      P3  nothing outside `dev/` (prose aside) so much as names a file containing one
      P4  no marked document is cluster-scoped; a ClusterRole is not confinable to a test namespace
          and no teardown makes one safe to have existed
      P5  no marked Role reaches `escalate`/`bind`/`impersonate`/`*`, or the RBAC API group itself

    P4 and P5 are the ones that matter if P1 through P3 ever fail: they bound the blast radius of a
    fixture that HAS escaped to the authority a namespaced read-mostly Role can hold.

    WHAT THIS DOES NOT COVER, AND WHY IT IS A FILE-SHAPED RULE. RBAC written as a heredoc inside a
    `dev/**.sh` is out of scope, and that is a stated non-claim rather than an oversight. A
    heredoc's disposition is not statically derivable: `dev/verify/brake-fanout-l2.sh` applies one
    and keeps it, and `dev/tests/negative-attenuation.sh` applies four of which three are SUPPOSED
    to be denied — one of them a ClusterRole granting `impersonate`, which is an adversarial input
    proving the VAP rejects it, not a grant. Marking that would be a lie and exempting it by helper
    name would be an enumeration. The consequence is worth naming in the other direction too: the
    confinement rule is enforceable on files and not on heredocs, so the fixture was made a FILE in
    order to be inside it. Closing the heredoc half needs a way to tell an applied grant from an
    adversarial input, which is an improvement pass, not this unit.
    """
    try:
        corpus = read_repo_files(REPO)
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"VACUOUS: could not enumerate the worktree ({exc}); nothing was scanned."]

    rbac = _rbac_documents(corpus)
    marked = [d for d in rbac if TEST_ONLY_MARKER in d[3]]
    if not marked:
        return [
            f"VACUOUS: no YAML in the tree carries a {TEST_ONLY_MARKER!r} RBAC document. Either "
            f"the marker was renamed — in which case every property below silently stopped being "
            f"checked, which is the whole of [[LSN-038]] — or the last test-only grant was "
            f"removed, in which case delete this check rather than leaving it green over nothing."
        ]

    failures: list[str] = []
    marked_files = {rel for rel, _, _, _ in marked}

    # P1 — the marker itself is confined. Prose may discuss it: 09 §6, invariants.md and the phase
    # breakdown all name it, and a rule that forbade that would be a rule against documenting it.
    for rel, text in corpus.items():
        if TEST_ONLY_MARKER not in text or rel.startswith("dev/") or rel.endswith(".md"):
            continue
        failures.append(
            f"{rel} carries the {TEST_ONLY_MARKER!r} marker and is not under dev/. A test-only "
            f"grant outside dev/ is an over-grant on every cluster this path is applied to."
        )

    # P2 — no unmarked RBAC under dev/. Without this, P1 is dodged by writing the next fixture
    # without the marker, and the check reports green over the file it exists to find.
    for rel, n, kind, doc in rbac:
        if rel.startswith("dev/") and TEST_ONLY_MARKER not in doc:
            failures.append(
                f"{rel} document {n} is a {kind} under dev/ and carries no "
                f"{TEST_ONLY_MARKER!r} label. Every RBAC document in dev/ is a test-only grant "
                f"by definition; an unmarked one is invisible to every property above."
            )

    # P3 — nothing outside dev/ references a file that holds one. The marker travelling is one way
    # a fixture escapes; an install script pointing at where it already lives is the other, and it
    # leaves the fixture itself looking untouched.
    basenames = {rel.rsplit("/", 1)[-1]: rel for rel in marked_files}
    for rel, text in corpus.items():
        if rel.startswith("dev/") or rel.endswith(".md") or rel in marked_files:
            continue
        for base, src in basenames.items():
            if base in text:
                failures.append(
                    f"{rel} references {base!r} ({src}), a file holding a test-only grant. "
                    f"Reachable only from dev/ means reachable only from dev/."
                )

    # P4/P5 — the blast radius of a fixture that did escape.
    for rel, n, kind, doc in marked:
        if kind in CLUSTER_SCOPED_RBAC:
            failures.append(
                f"{rel} document {n} is a {kind}. A test-only grant must be namespaced: a "
                f"cluster-scoped one cannot be confined to a test namespace, and its teardown "
                f"cannot undo the window in which it existed."
            )
        if kind not in ("Role", "ClusterRole"):
            continue  # bindings carry no rules; their roleRef.apiGroup is legitimately RBAC's
        for line, verb in _verbs_in(doc):
            if verb in ESCALATION_VERBS:
                failures.append(
                    f"{rel} document {n} line {line} grants {verb!r}. [[LSN-004]]: this is not a "
                    f"write verb and it is worse than one — a test-only grant may never hold it."
                )
        rules = doc.split("rules:", 1)
        if len(rules) == 2 and RBAC_API_GROUP in rules[1]:
            failures.append(
                f"{rel} document {n} grants a resource in {RBAC_API_GROUP!r}. A grant over RBAC "
                f"itself is authority to widen every other grant, whatever verbs it names."
            )

    return failures


# ---------------------------------------------------------------------------------------------
# LSN-052 / LSN-054 — a green produced by not asking
# ---------------------------------------------------------------------------------------------

BINDING = REPO / ".claude/harness/binding.md"
PROTOCOL = REPO / ".claude/harness/PROTOCOL.md"
OPERATOR_TEST_CMD = "make -C k8s-operator test"
ENVTEST_ENV = "KUBEBUILDER_ASSETS"
OPERATOR_MAKEFILE = REPO / "k8s-operator/Makefile"
REAPER = REPO / "dev/reap-envtest.sh"


def _envtest_gated_packages() -> set[Path]:
    """Directories holding a Go test that skips itself when `KUBEBUILDER_ASSETS` is unset."""
    return {
        p.parent
        for p in (REPO / "k8s-operator").rglob("*_test.go")
        if ENVTEST_ENV in p.read_text(encoding="utf-8", errors="replace")
    }


def check_envtest_is_run_by_the_command_checkpoint_names() -> list[str]:
    """LSN-054: a skipped envtest is reported as a passing package, and `go test ./...` agrees.

    `requireEnv(t)` calls `t.Skip` when `KUBEBUILDER_ASSETS` is unset. A skipped test is not a
    failing test, so the package prints `ok` and the bare command is green over a suite that never
    ran. That is how a stale premise inside a BLOCKING-ALWAYS check (V-BRK-023) survived every local
    run of a 25-commit phase branch and surfaced on its final CI run.

    The sharp part is that LSN-052 -- the lesson that a unit runs no Go tests at all -- proposed
    `cd k8s-operator && go test ./...` as its mechanization, and that command walks straight past
    this. So the two close together or neither does, and the property here is not "tests are run"
    but "the command that is named is the one that resolves the assets".

    Four things have to hold at once, because each is load-bearing for a different reader:

      1. The make target really does resolve the assets. If it stops, everything below is a rule
         about a command that no longer does the thing, which is worse than no rule.
      2. `binding.md` §Build names it as the entry point, and says why the bare command is not one.
      3. `harness-run` §6 CHECKPOINT names it too. §Build is a table a reader consults; CHECKPOINT
         is the list they walk. A rule in only the first is a rule at the wrong moment.
      4. PROTOCOL §3's done-condition 1 mentions tests at all. It said "build/format/lint" and a
         unit that satisfied it exactly had still never run a test.
    """
    gated = _envtest_gated_packages()
    if not gated:
        return [
            f"VACUOUS: no `*_test.go` under k8s-operator/ mentions {ENVTEST_ENV}, so this check has "
            f"nothing to protect and cannot fail. Either envtest was removed -- in which case "
            f"retire this check and the binding rows it pins -- or the skip guard was renamed and "
            f"this check went quiet, which reads exactly like a pass (LSN-035, LSN-038)."
        ]

    failures = []
    makefile = REPO / "k8s-operator/Makefile"
    mk = makefile.read_text(encoding="utf-8") if makefile.exists() else ""
    target = re.search(r"^test:.*\n(?:\t.*\n)+", mk, re.M)
    if not target or ENVTEST_ENV not in target.group(0):
        failures.append(
            f"{makefile.relative_to(REPO)}'s `test` target no longer sets {ENVTEST_ENV}. That is "
            f"the premise every rule below rests on: without it `{OPERATOR_TEST_CMD}` skips the "
            f"same {len(gated)} packages the bare command does, and the harness is following a "
            f"rule that buys nothing."
        )

    binding = BINDING.read_text(encoding="utf-8") if BINDING.exists() else ""
    build = re.search(r"^## §Build\s*$\n(?P<body>(?:(?!^## ).*\n)*)", binding, re.M)
    if not build:
        failures.append(f"{BINDING.relative_to(REPO)} has no §Build section to read")
    else:
        body = build.group("body")
        if OPERATOR_TEST_CMD not in body:
            failures.append(
                f"{BINDING.relative_to(REPO)} §Build no longer names `{OPERATOR_TEST_CMD}`. It is "
                f"the only command that runs envtest; naming anything else names a green that was "
                f"produced by not asking (LSN-054)."
            )
        if ENVTEST_ENV not in body:
            failures.append(
                f"{BINDING.relative_to(REPO)} §Build names the command but no longer explains that "
                f"{ENVTEST_ENV} is what makes it different from `go test ./...`. The next reader "
                f"sees two rows that look interchangeable and picks the faster one."
            )

    skill = HARNESS_RUN_SKILL.read_text(encoding="utf-8") if HARNESS_RUN_SKILL.exists() else ""
    checkpoint = re.search(r"^## 6\. CHECKPOINT\s*$\n(?P<body>(?:(?!^## ).*\n)*)", skill, re.M)
    if not checkpoint:
        failures.append(f"{HARNESS_RUN_SKILL.relative_to(REPO)} has no §6 CHECKPOINT to read")
    elif OPERATOR_TEST_CMD not in checkpoint.group("body"):
        failures.append(
            f"{HARNESS_RUN_SKILL.relative_to(REPO)} §6 CHECKPOINT no longer names "
            f"`{OPERATOR_TEST_CMD}`. §Build is a table a reader consults; CHECKPOINT is the list "
            f"they walk before calling a unit done. The rule has to be in the list."
        )

    protocol = PROTOCOL.read_text(encoding="utf-8") if PROTOCOL.exists() else ""
    unit = re.search(r"^## 3\. The unit of work\s*$\n(?P<body>(?:(?!^## ).*\n)*)", protocol, re.M)
    if not unit:
        failures.append(f"{PROTOCOL.relative_to(REPO)} has no §3 to read")
    elif not re.search(r"\btests?\b", unit.group("body"), re.I):
        failures.append(
            f"{PROTOCOL.relative_to(REPO)} §3's done-conditions no longer mention tests. They read "
            f"'build/format/lint' until 2026-07-30, and a unit that satisfied them exactly had "
            f"still never run one (LSN-052)."
        )

    return failures


def check_mutation_specs_declare_required_env() -> list[str]:
    """LSN-054, one level down: a mutation sweep over a skipping suite reports holes that are not.

    `dev/mutate.py` rule 5 checks every catcher against `go test -list` so a misremembered name is a
    refusal rather than a survivor. That guard is blind here: `-list` COMPILES rather than runs, so
    a catcher inside a file that `t.Skip`s itself is listed exactly as a running one is. The suite
    then stays green under every mutation and each such mutant scores ESCAPED -- six of nineteen, in
    the sweep that produced this lesson, every one of them against a test that catches it cleanly.

    Rule 7 gives a spec `suite.requires_env`, and this asserts the committed specs use it: any
    `go` spec whose package patterns cover an envtest-gated directory must declare the variable.
    Derived from the tree rather than from a list, so a new spec over `./internal/controller/...`
    is caught the day it lands and not the day its report is believed.
    """
    specs = sorted((REPO / "verification/mutants").glob("*.json"))
    if not specs:
        return ["VACUOUS: no mutation specs found under verification/mutants/"]
    gated = _envtest_gated_packages()
    if not gated:
        return [f"VACUOUS: nothing under k8s-operator/ gates on {ENVTEST_ENV}"]

    def covers(root: Path, pattern: str, pkgdir: Path) -> bool:
        if not pattern.startswith("./"):
            return False
        recursive = pattern.endswith("/...")
        base = (root / pattern[2:].removesuffix("/...").rstrip("/")).resolve()
        return pkgdir == base or (recursive and base in pkgdir.parents)

    failures = []
    for path in specs:
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{path.relative_to(REPO)}: unreadable ({exc})")
            continue
        suite = spec.get("suite") or {}
        if suite.get("kind") != "go":
            continue
        root = (REPO / suite.get("dir", ".")).resolve()
        hit = sorted(
            d.relative_to(REPO).as_posix()
            for d in gated
            if any(covers(root, p, d) for p in suite.get("packages") or [])
        )
        if hit and ENVTEST_ENV not in (suite.get("requires_env") or []):
            failures.append(
                f"{path.relative_to(REPO)} sweeps {hit}, whose tests skip themselves when "
                f"{ENVTEST_ENV} is unset, and does not declare `\"requires_env\": "
                f"[\"{ENVTEST_ENV}\"]`. Run without it and every mutant those packages would catch "
                f"scores ESCAPED against a suite that catches them cleanly (LSN-054)."
            )
    return failures


VERIFY_DIR = REPO / "dev/verify"


def check_negative_controls_exercise_the_statement_under_test() -> list[str]:
    """LSN-060: a ¬ form that synthesises its input measures nothing about how the input is obtained.

    `broker-execute-l2.sh` looked its ActionRecord up by the RAW action id. Object names are
    `journal.RecordName(actionID)` = `"ar-" + lower(actionID)` (06 §4.3, lowercased because a name
    must be a DNS subdomain and a ULID is uppercase), so that lookup could not have found a record
    against any commit. The suite never said so: the only thing that had ever exercised the arm was
    `--negative-control`, which synthesises thirteen record documents and feeds them straight to the
    assertion block, never touching the lookup. 13/13 green, for a statement that had not once run.

    Two properties, because the general one is a write-it-down and the specific one is a diff.

    1. Every suite with a `--negative-control` mode carries a `NEGATIVE CONTROL DOES NOT EXERCISE:`
       block naming the live statements its synthesised path bypasses. Forced, not inferred -- the
       same move [[LSN-051]] makes for a §8.5 halt: you cannot enumerate a bypass you never looked
       for, and being made to write the list down is where the omission becomes visible.

    2. No script fetches an `actionrecord` by interpolating a bare action id. That is the exact
       defect, it is a one-line grep, and it is the half a reviewer cannot be relied on to catch
       because the correct and incorrect forms differ by four characters.
    """
    scripts = sorted(VERIFY_DIR.glob("*.sh"))
    if not scripts:
        return ["VACUOUS: no suites under dev/verify/"]

    marker = "NEGATIVE CONTROL DOES NOT EXERCISE:"
    # `get actionrecord "$action_id"` and friends: a lookup whose name argument is an id variable.
    by_raw_id = re.compile(
        # The trailing boundary is load-bearing: without it `$REC_QUIET_IDLE` matches on its `ID`
        # and `brake-fanout-l2.sh` -- which derives its names correctly via `rec_name` -- is
        # reported as the defect. A check whose first finding is a false positive teaches the next
        # reader to skim its output.
        r"""(?:get|delete|patch)\s+actionrecords?(?:\.agents\.gke\.io)?\s+"?\$\{?"""
        r"""(\w*(?:action_?)?[iI][dD])\}?"?(?![\w-])""",
    )

    failures, saw_control = [], False
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO).as_posix()

        for m in by_raw_id.finditer(text):
            line = text[: m.start()].count("\n") + 1
            failures.append(
                f"{rel}:{line} looks an ActionRecord up by `${m.group(1)}`. The object name is "
                f'`journal.RecordName` = `"ar-" + lower(actionID)` (06 §4.3), so this cannot find a '
                f"record. Derive it: `record_name=\"ar-$(printf '%s' \"$action_id\" | "
                f'tr \'[:upper:]\' \'[:lower:]\')\"` (LSN-060).'
            )

        if "--negative-control" not in text:
            continue
        saw_control = True
        if marker not in text:
            failures.append(
                f"{rel} has a `--negative-control` mode and no `{marker}` block. A control that "
                f"synthesises its input proves the ASSERTIONS are not always-green and proves "
                f"nothing about the statements it bypassed to inject that input -- which are the "
                f"API call, the parse and the lookup, i.e. where an L2 suite actually fails. List "
                f"them in a comment so the next reader can see what the 13/13 does not cover "
                f"(LSN-060)."
            )
            continue
        after = text.split(marker, 1)[1]
        listed = [
            ln.strip().lstrip("#").strip()
            for ln in after.splitlines()[1:]
            if ln.lstrip().startswith("#")
        ]
        if not any(item.startswith(("-", "*")) and len(item) > 3 for item in listed):
            failures.append(
                f"{rel} carries `{marker}` with no entries under it. An empty list is the claim "
                f"that the control exercises everything, which is the claim LSN-060 was about."
            )

    if not saw_control:
        failures.append(
            "VACUOUS: no suite under dev/verify/ declares a `--negative-control` mode, so the "
            "declaration half of this check measured nothing."
        )
    return failures


def check_checkpoint_commits_reach_ci() -> list[str]:
    """LSN-055: one push per branch is one CI run for every commit on it.

    The phase-9 branch took 25 CHECKPOINT commits and a single `git push`, so
    `k8s-operator-test.yml` ran once -- on the PR, against all 25 at once -- and the red it found
    belonged to a commit twenty back. CHECKPOINT exists to attribute a verdict to one unit; a
    once-per-branch CI run destroys exactly that.

    The fix has two halves and each is useless alone, which is the only reason this check exists
    rather than a note. A push cadence with a `main`-only trigger pushes into silence. A wide
    trigger with no cadence is still triggered once. So the pair is asserted together, and neither
    can be dropped on the grounds that the other one covers it.
    """
    failures = []

    binding = BINDING.read_text(encoding="utf-8") if BINDING.exists() else ""
    branching = re.search(r"^## §Branching\s*$\n(?P<body>(?:(?!^## ).*\n)*)", binding, re.M)
    if not branching:
        failures.append(f"{BINDING.relative_to(REPO)} has no §Branching section to read")
    else:
        # A TABLE ROW, not the section body. The prose below the table explains the rule and
        # necessarily contains both words ("25 CHECKPOINT commits and one push"), so a body-wide
        # search stays green after the rule itself is deleted -- which is how a check ends up
        # pinning its own rationale instead of its rule.
        rows = [r for r in branching.group("body").splitlines() if r.startswith("|")]
        if not any("push" in r.lower() and "every CHECKPOINT" in r for r in rows):
            failures.append(
                f"{BINDING.relative_to(REPO)} §Branching has no row requiring a push at every "
                f"CHECKPOINT. Without the cadence the trigger below fires once per phase and CI is "
                f"an end-of-phase audit against commits nobody can still attribute (LSN-055)."
            )

    skill = HARNESS_RUN_SKILL.read_text(encoding="utf-8") if HARNESS_RUN_SKILL.exists() else ""
    checkpoint = re.search(r"^## 6\. CHECKPOINT\s*$\n(?P<body>(?:(?!^## ).*\n)*)", skill, re.M)
    if checkpoint and "git push" not in checkpoint.group("body"):
        failures.append(
            f"{HARNESS_RUN_SKILL.relative_to(REPO)} §6 CHECKPOINT no longer tells the unit to push. "
            f"The cadence lives in binding.md, but CHECKPOINT is the list that gets walked."
        )

    wf = WORKFLOWS / "k8s-operator-test.yml"
    if not wf.exists():
        return failures + [
            f"VACUOUS: {wf.relative_to(REPO)} is gone, so the trigger half of this check did not "
            f"run. If operator tests moved, repoint this check at their workflow; a missing file "
            f"is not a pass."
        ]
    text = wf.read_text(encoding="utf-8")
    on = re.search(r"^on:\s*$\n(?P<body>(?:^[ \t].*\n|^\s*\n)*)", text, re.M)
    if not on:
        failures.append(f"{wf.relative_to(REPO)}: could not parse the `on:` block")
    else:
        push = re.search(r"^  push:\s*$\n(?P<body>(?:^ {4,}.*\n)*)", on.group("body"), re.M)
        if not push:
            failures.append(
                f"{wf.relative_to(REPO)} has no `push:` trigger, so a CHECKPOINT push runs nothing "
                f"and the operator suite is only ever exercised by the phase's PR (LSN-055)."
            )
        elif re.search(r"branches(-ignore)?:", push.group("body")):
            failures.append(
                f"{wf.relative_to(REPO)}'s `push:` trigger is branch-filtered "
                f"({push.group('body').strip()!r}). Phase branches are where CHECKPOINT commits "
                f"live; a filter that excludes them means the cadence pushes into silence and the "
                f"suite still runs once, on the PR (LSN-055)."
            )

    return failures


def check_spec_contradiction_halts_cite_both_sides() -> list[str]:
    """LSN-051: a contradiction is a relation between two sentences, and one halt carried one.

    A PROTOCOL §8.5 halt was declared against 06 §2.2.1 -- the *broker-operations* grant -- when the
    authority in question is 06 §2.2, one level up, which grants every read the halt called
    ungranted. A subsection number reads like a refinement of its section, so having read §2.2.1
    felt like having read §2.2. A session was spent, and the contradiction did not exist.

    Being made to write the second citation down is where the absence becomes visible: you cannot
    quote a sentence you never found. So the rule is procedural -- it belongs in the skill -- and
    this asserts both that the skill still carries it and that the ledger's §8.5 rows obey it. The
    ledger half is what makes this more than a lint on prose; the skill half is what stops the
    ledger half reading as an unexplained rule the next time someone meets it (the shape
    `_drain_is_committed` uses).
    """
    failures = []

    skill = HARNESS_RUN_SKILL.read_text(encoding="utf-8") if HARNESS_RUN_SKILL.exists() else ""
    halts = re.search(r"^## 7\. Halt conditions.*$\n(?P<body>(?:(?!^## ).*\n)*)", skill, re.M)
    if not halts:
        failures.append(f"{HARNESS_RUN_SKILL.relative_to(REPO)} has no §7 halt-conditions section")
    elif not re.search(r"§8\.5.{0,400}both", halts.group("body"), re.I | re.S):
        failures.append(
            f"{HARNESS_RUN_SKILL.relative_to(REPO)} §7 no longer requires a §8.5 halt to quote "
            f"BOTH conflicting statements by document and section. That sentence is the procedure "
            f"the ledger arm below enforces; without it the arm reads as an arbitrary citation "
            f"count (LSN-051)."
        )

    if not LEDGER.exists():
        return failures + [f"VACUOUS: {LEDGER.relative_to(REPO)} not found"]

    # Only rows that DECLARE a halt, not rows that discuss one. A withdrawn row is struck through
    # with `~~` and is deliberately left in place as a record; re-litigating it every run would
    # make the check noisy in exactly the way that gets a check deleted.
    citation = re.compile(r"\b0[1-9]\s*§\s*\d+(?:\.\d+)*")
    rows = 0
    for n, line in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|") or "§8.5" not in line:
            continue
        if not re.search(r"\bHALTED\b|\bHALT\b", line) or "~~" in line:
            continue
        rows += 1
        cites = {re.sub(r"\s+", "", c) for c in citation.findall(line)}
        if len(cites) < 2:
            failures.append(
                f"{LEDGER.relative_to(REPO)}:{n} declares a PROTOCOL §8.5 halt and cites "
                f"{sorted(cites) or 'no spec section'}. A contradiction is a relation between two "
                f"statements; a row carrying fewer than two citations is not describing one. Quote "
                f"both, each by document and section — that is where LSN-051's missing half would "
                f"have become visible before the session was spent."
            )
    if rows == 0 and not failures:
        # Not a failure. Recorded so a reader of the output knows this arm found nothing to judge
        # rather than judging and approving.
        pass
    return failures


def check_a_check_only_unit_exhibits_both_trees() -> list[str]:
    """LSN-053: a check split off from its implementation has two trees to be green on.

    Guardrail 9 forbids changing a check in the same unit as the implementation whose failure
    motivated it, so checks get reshaped one unit AHEAD of what they will judge. Such a unit is
    green on today's tree by construction. If nobody establishes it is also green on the tree the
    next unit will build, the next unit's first run reads as "my implementation broke the check" and
    the cheapest diff to green is to edit the check -- Guardrail 9's own pressure, arriving one unit
    later in disguise.

    That is not a hypothetical risk: probing the future tree is what caught a false negative on
    2026-07-30, where a whole tier returned zero findings because the property only knew about one
    of the two admission validations that tree would hit.

    Mechanizing "did you check the other tree" directly would need a check that knows what the next
    unit will build. What CAN be asserted is the artifact the rule demands: the future-tree evidence
    is a committed `--negative-control` row rather than a `/tmp` probe, so it re-runs on every chain
    run instead of once, in one session, for one reader.
    """
    failures = []

    skill = HARNESS_RUN_SKILL.read_text(encoding="utf-8") if HARNESS_RUN_SKILL.exists() else ""
    impl = re.search(r"^## 4\. IMPLEMENT\s*$\n(?P<body>(?:(?!^## ).*\n)*)", skill, re.M)
    if not impl:
        failures.append(f"{HARNESS_RUN_SKILL.relative_to(REPO)} has no §4 IMPLEMENT section")
    else:
        body = impl.group("body")
        if not re.search(r"both\b.{0,60}\btrees?\b", body, re.I | re.S):
            failures.append(
                f"{HARNESS_RUN_SKILL.relative_to(REPO)} §4 no longer requires a check-only unit to "
                f"exhibit BOTH trees — today's and the one the next unit will build (LSN-053)."
            )
        if "negative-control" not in body:
            failures.append(
                f"{HARNESS_RUN_SKILL.relative_to(REPO)} §4 no longer says the future-tree evidence "
                f"is a committed `--negative-control` row. A `/tmp` probe proves it once, to one "
                f"session; the point of the rule is that it re-runs (LSN-053)."
            )

    # The rule is only worth stating if the repo actually has the affordance it names.
    chain = regress_chain_text()
    if "--negative-control" not in chain:
        failures.append(
            "no line of the declared chains runs a check with `--negative-control`, so §4's rule "
            "names an affordance this repo does not have. Either the controls stopped being wired "
            "into a chain — in which case the future-tree evidence is not re-running either — or "
            "the flag was renamed and this check went quiet."
        )
    return failures


def check_envtest_control_planes_are_reaped() -> list[str]:
    """LSN-059: a hard-killed `go test` abandons one etcd and one kube-apiserver per package.

    envtest starts a real control plane per test BINARY and stops it in `TestMain` after `m.Run()`
    returns. A SIGKILL never reaches that line, launchd/init adopts the children, and nothing on the
    machine ever reaps them. Measured on the dev laptop on 2026-07-30 with no test run in flight: 32
    processes, 30 of them at `ppid=1`, holding 1375 MB, the oldest ~31 hours old.

    It is a HARNESS defect and not only a test defect because the harness is the killer. A
    time-bounded caller kills `go test`, a cohort is abandoned, the machine gets slower, a slower
    machine is likelier to hit the same bound. LSN-058 is the standing proof that this class of
    interference does not stay quiet.

    You cannot trap a SIGKILL, so the fix is a SWEEP at the START of the next run — the one moment
    guaranteed to happen after an abandoned cohort exists. Four things are asserted, and each covers
    a different way the fix could be present but inert:

      1. The reaper exists. Everything below is a rule about a file.
      2. It is a PREREQUISITE of `test`, not only a trap. The prerequisite is the load-bearing half:
         it bounds accumulation at one run's worth however the PREVIOUS run died. A trap alone
         covers every death except the only one that causes the leak.
      3. The trap is there too, on EXIT/INT/TERM — the tidy half, which keeps an ordinary Ctrl-C
         from leaving a cohort behind until the next run.
      4. `binding.md` §Build still carries the timeout the caller needs. The sweep bounds the leak
         at one run's worth however the previous run died; it does not stop that run's worth being
         MADE, and what makes it is a default two-minute bound around a 2m09s command. No check in
         this tree can read the caller's timeout, so the number being written down is the most that
         can be held — and it is worth holding, because it is the difference between a leak that is
         cleaned up and a leak that is not created.
      5. The two safety properties inside the reaper are intact: selection anchored at the LEFT EDGE
         of the asset root (LSN-005's rule applied to a process — a name match would reap somebody's
         real etcd, silently, from a Makefile) and the `ppid == 1` predicate (without it, wiring the
         sweep into `test` makes two concurrent `make test` runs kill each other, a fix whose
         failure mode is worse than the leak). Deleting either one leaves a script that still runs,
         still prints, and still exits 0.
    """
    gated = _envtest_gated_packages()
    if not gated:
        return [
            f"VACUOUS: nothing under k8s-operator/ mentions {ENVTEST_ENV}, so no test starts a "
            f"control plane and there is nothing to leak. Either envtest was removed — retire this "
            f"check and the Makefile wiring with it — or the assets are resolved some other way now "
            f"and this check went quiet, which reads exactly like a pass (LSN-035, LSN-038)."
        ]

    failures = []

    reaper = REAPER
    if not reaper.exists():
        return failures + [
            f"{reaper.name} is gone. It is the only thing that reaps a control plane whose "
            f"`go test` was killed before TestMain could stop it, and the {len(gated)} envtest "
            f"packages each abandon two processes per killed run (LSN-059)."
        ]

    makefile = OPERATOR_MAKEFILE
    mk = makefile.read_text(encoding="utf-8") if makefile.exists() else ""
    target = re.search(r"^test:(?P<prereqs>.*)\n(?P<recipe>(?:\t.*\n)+)", mk, re.M)
    if not target:
        failures.append(f"{makefile.relative_to(REPO)} has no `test` target to read")
    else:
        if "reap-envtest" not in target.group("prereqs"):
            failures.append(
                f"{makefile.relative_to(REPO)}'s `test` target no longer takes `reap-envtest` as a "
                f"prerequisite. That is the load-bearing half: it runs BEFORE the tests, which is "
                f"the only moment guaranteed to happen after the previous run was SIGKILLed. A "
                f"trap cannot cover the death that causes the leak (LSN-059)."
            )
        recipe = target.group("recipe")
        if not re.search(r"trap\s+.*reap-envtest.*EXIT", recipe):
            failures.append(
                f"{makefile.relative_to(REPO)}'s `test` recipe no longer traps the reaper on EXIT. "
                f"The before-sweep bounds the leak at one run's worth; the trap is what keeps an "
                f"ordinary Ctrl-C from producing that run's worth in the first place (LSN-059)."
            )

    if not re.search(r"^reap-envtest:.*\n(?:\t.*\n)*\t.*reap-envtest\.sh", mk, re.M):
        failures.append(
            f"{makefile.relative_to(REPO)} has no `reap-envtest` target that runs "
            f"{reaper.relative_to(REPO)}. A prerequisite naming a target that does something else "
            f"is a green produced by not asking (LSN-059)."
        )

    body = reaper.read_text(encoding="utf-8")
    if "$2 == 1" not in body:
        failures.append(
            f"{reaper.relative_to(REPO)} no longer selects on `ppid == 1`. That predicate is "
            f"precisely and only the leak: a control plane with a live parent is somebody's test "
            f"run in flight, including a CONCURRENT `make test`. Without it, the sweep the "
            f"Makefile runs on every `test` kills the other terminal's suite (LSN-059)."
        )
    if not re.search(r"index\(\s*argv0\s*,\s*root\s*\)\s*==\s*1", body):
        failures.append(
            f"{reaper.relative_to(REPO)} no longer anchors its match at the LEFT EDGE of the asset "
            f"root. A substring or name match (`pgrep etcd`) reaps the etcd somebody is running for "
            f"real work — from a Makefile, silently, on every test run. This is LSN-005's rule "
            f"applied to a process instead of a cluster."
        )
    if "REFUSING" not in body:
        failures.append(
            f"{reaper.relative_to(REPO)} no longer refuses any asset root. A prefix match is only "
            f"as safe as the prefix, and `--dir /` puts every process on the machine in scope of a "
            f"script whose job is to kill what is in scope (LSN-059)."
        )

    # The caller half. The sweep bounds the leak at one run's worth however the previous run died;
    # it does not stop that run's worth being MADE. What makes it is a caller whose default time
    # bound is two minutes running a command measured at 2m09s, and no check in this tree can see
    # the caller's timeout — so what is asserted is that the number a caller needs is still written
    # down where the caller's operator will read it.
    binding = BINDING.read_text(encoding="utf-8") if BINDING.exists() else ""
    build = re.search(r"^## §Build\s*$\n(?P<body>(?:(?!^## ).*\n)*)", binding, re.M)
    if not build:
        failures.append(f"{BINDING.relative_to(REPO)} has no §Build section to read")
    elif not re.search(r"\btimeout\b", build.group("body"), re.I):
        failures.append(
            f"{BINDING.relative_to(REPO)} §Build no longer warns that "
            f"`{OPERATOR_TEST_CMD}` must be given an explicit timeout. It was measured at 2m09s "
            f"against a two-minute default, so a caller that does not raise the bound kills it a "
            f"few seconds from the end and abandons a control plane per envtest package — every "
            f"run, forever (LSN-059)."
        )

    return failures


# Repointed by dev/test_invariants_gate.py's controls, which is why they are constants.
VERIFY_DIR = REPO / "dev" / "verify"
RATCHET_RUNNER = REPO / "dev" / "tests" / "phase-ratchet-is-asserted.py"


def check_phase_gate_runs_its_own_ratchet() -> list[str]:
    """Planning defect 4 / LSN-019: every `verify-phase<N>.sh` runs the 09 §10 ratchet for ITS phase.

    On 2026-07-31 `harness-milestone` was invoked for Phase 9 with all 70 in-phase leaf units done,
    and stopped at §1: **23 of the 75 check IDs the phase requires had never been run**, 8 of them
    from BLOCKING-ALWAYS suites. Every section of `verify-phase9.sh` was passing or failing for
    reasons unrelated to them, because the script names 18 check IDs and has no V-ISO section at
    all. A gate that never names an ID cannot go red for it.

    The gap had been PREDICTED. `docs/build/phase-9.md` § "Planning defect 4", written 2026-07-27,
    counts seventeen unrun ratchet checks and declares the resolution: *"verify-phase9.sh runs the
    ratchet, not the Accept list."* The acceptance table was amended, which is half of it. The
    script was not, which is the half that runs. [[LSN-019]] again -- prose on the artifact is not a
    mechanization, and here the prose was a correct description of a live defect.

    So the mechanization has to be about the SCRIPT, and it has to be about every phase gate rather
    than the one that got caught, because the next phase's gate is written by copying this one.
    Three things, each covering a different way the arm could be present and inert:

      1. Every `dev/verify/verify-phase<N>.sh` invokes `phase-ratchet-is-asserted.py`.
      2. It passes `--phase <N>` matching the script's OWN number. A gate that audits phase 8's
         ratchet is a gate that passes while its own phase is unproven, and the two lines differ by
         one character.
      3. The invocation's failure is a `bad`/failure, not a bare informational echo. An arm whose
         red does not reach the exit code is a comment.

    Phases before the arm existed are exempt by an explicit list rather than by a floor: a floor
    silently exempts every phase added below it, and the whole point is that a new gate inherits
    the obligation. Adding a phase to that list is a conversation, which is what it should be.
    """
    failures: list[str] = []
    runner = RATCHET_RUNNER
    if not runner.exists():
        return [
            f"{runner.relative_to(REPO)} does not exist. It is the derived form of the 09 §10 "
            f"ratchet, and without it every phase gate is back to a hand-written check list "
            f"(planning defect 4)."
        ]

    # Gates written before 2026-07-31, when this obligation did not exist. Not a floor: a floor
    # would exempt phase 16 as readily as phase 2.
    grandfathered = {"2", "3", "4", "5", "6", "7", "8"}

    gates = sorted(VERIFY_DIR.glob("verify-phase*.sh"))
    if not gates:
        return ["dev/verify/ has no verify-phase*.sh at all -- this check matched nothing"]

    checked = 0
    for gate in gates:
        m = re.match(r"verify-phase(\d+)\.sh$", gate.name)
        if not m or m.group(1) in grandfathered:
            continue
        checked += 1
        phase = m.group(1)
        text = gate.read_text(encoding="utf-8")
        invocations = [
            line
            for line in text.splitlines()
            if "phase-ratchet-is-asserted.py" in line and not line.lstrip().startswith("#")
        ]
        if not invocations:
            failures.append(
                f"{gate.relative_to(REPO)} never invokes dev/tests/phase-ratchet-is-asserted.py, so "
                f"nothing in it checks phase {phase}'s OWN 09 §10 ratchet. Sections that run the "
                f"Accept list and the PRIOR ratchet both stay green while required check IDs go "
                f"unrun -- that is exactly how planning defect 4 survived to the milestone."
            )
            continue
        if not any(re.search(rf"--phase\s+{phase}(\s|$)", line) for line in invocations):
            failures.append(
                f"{gate.relative_to(REPO)} invokes phase-ratchet-is-asserted.py but not with "
                f"`--phase {phase}`. A gate that audits another phase's ratchet passes while its "
                f"own is unproven, and the two lines differ by one character."
            )
        # 3. The red has to reach the exit code. The gate scripts report failure through `bad`.
        window = "\n".join(
            text.splitlines()[
                max(0, text.splitlines().index(invocations[0]) - 2) : text.splitlines().index(
                    invocations[0]
                )
                + 12
            ]
        )
        if "bad " not in window:
            failures.append(
                f"{gate.relative_to(REPO)}'s ratchet invocation does not reach `bad` within twelve "
                f"lines, so a failed ratchet audit prints and does not fail the gate. An arm whose "
                f"red does not reach the exit code is a comment."
            )

    if not checked:
        failures.append(
            "every verify-phase*.sh in the tree is grandfathered out of the ratchet obligation, so "
            "this check evaluated nothing. Remove a phase from `grandfathered` or delete the check "
            "-- a check with an empty corpus reports the same green as one that passed."
        )
    return failures


CHECKS = [
    ("invariant 7 — authority never precedes machinery", check_write_verbs_have_machinery),
    ("LSN-038 — the machinery probes resolve against the tree", check_machinery_probes_resolve),
    ("invariant 8 / V-MET-003 — assertion ratchet", check_assertion_ratchet),
    (
        "LSN-056 — the ratchet baseline covers the whole corpus",
        check_the_ratchet_baseline_covers_the_corpus,
    ),
    ("V-MET-004 — retirements name replacements", check_retirements_name_replacements),
    ("LSN-005 — destructive-test guards stay anchored", check_destructive_guards_are_anchored),
    ("LSN-018 — build targets name their cluster", check_make_targets_are_context_explicit),
    ("V-MET-006 / LSN-008 — deferrals name a blocker", check_deferrals_name_blockers),
    ("09 §12 / LSN-046 — † checks are deferred by ID", check_dagger_checks_are_deferred_by_id),
    (
        "LSN-001/002/003 — L2 scripts declare and back their preconditions",
        check_l2_scripts_declare_preconditions,
    ),
    ("invariant 13 / LSN-019 — closed lessons are executable", check_closed_lessons_are_executable),
    ("LSN-019 — every lesson body has an index row", check_every_lesson_has_an_index_row),
    ("P9 — `.status` reads are polled, not slept on", check_l2_status_reads_are_polled),
    ("P3 — recreated pods are resolved by ownership", check_p3_pods_resolved_by_ownership),
    (
        "LSN-026 / P10 — L2 scripts assert the cluster can run the experiment",
        check_l2_scripts_assert_cluster_health,
    ),
    (
        "LSN-027 — cluster-creating scripts measure their substrate first",
        check_cluster_creating_scripts_assert_capacity,
    ),
    (
        "LSN-029 — BSD/GNU flag collisions are written GNU-first",
        check_platform_idioms_are_gnu_first,
    ),
    ("the human backlog is drained, not accumulated", check_backlog_is_drained),
    ("L0 chain is runnable and wired to CI", check_l0_chain_is_runnable),
    (
        "LSN-050 — L0 checks enumerate the working tree, not the index",
        check_l0_corpus_is_not_index_only,
    ),
    (
        "LSN-044 — a lesson's status agrees with its index row and the tally",
        check_lesson_status_matches_its_index_row,
    ),
    ("the metrics table's rows carry every column", check_metrics_rows_are_complete),
    ("V-CTN-037 — a test-only RBAC grant never leaves dev/", check_test_only_grants_are_confined),
    (
        "LSN-052/054 — the test entry point CHECKPOINT names is the one that runs envtest",
        check_envtest_is_run_by_the_command_checkpoint_names,
    ),
    (
        "LSN-054 — a mutation spec over an envtest package declares the env it needs",
        check_mutation_specs_declare_required_env,
    ),
    ("LSN-055 — a CHECKPOINT commit reaches CI", check_checkpoint_commits_reach_ci),
    (
        "LSN-060 — a negative control exercises the statement under test",
        check_negative_controls_exercise_the_statement_under_test,
    ),
    (
        "LSN-051 — a §8.5 halt quotes both sides of the contradiction",
        check_spec_contradiction_halts_cite_both_sides,
    ),
    (
        "LSN-053 — a check split from its implementation is green on both trees",
        check_a_check_only_unit_exhibits_both_trees,
    ),
    (
        "LSN-059 — a killed test run's control planes are reaped by the next one",
        check_envtest_control_planes_are_reaped,
    ),
    (
        "planning defect 4 — every phase gate runs its own 09 §10 ratchet",
        check_phase_gate_runs_its_own_ratchet,
    ),
]


def update_baseline() -> None:
    existing = load_baseline()
    payload = {
        "_comment": (
            "Baseline for the assertion ratchet (V-MET-003, invariants.md 8). Regenerate with "
            "`python3 dev/tests/invariants-gate.py --update-baseline` ONLY when adding "
            "tests. Removing a name here to make the gate pass is invariant 9 (no weakening to "
            "pass) and invariant 10 if the check is load-bearing."
        ),
        "retired": existing.get("retired", {}),
        "inventory": inventory(),
    }
    BASELINE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    total = sum(len(v) for v in payload["inventory"].values())
    print(f"wrote {BASELINE.relative_to(REPO)}: {len(payload['inventory'])} files, {total} named tests")


def main() -> int:
    if "--update-baseline" in sys.argv:
        update_baseline()
        return 0

    failed = 0
    for title, fn in CHECKS:
        try:
            problems = fn()
        except Exception as exc:  # the gate broke; never report that as a pass
            print(f"✗ {title}\n    GATE ERROR: {type(exc).__name__}: {exc}")
            return 2
        if problems:
            failed += 1
            print(f"✗ {title}")
            for p in problems:
                print(f"    - {p}")
        else:
            print(f"✓ {title}")

    print()
    if failed:
        print(f"{failed} of {len(CHECKS)} invariant checks FAILED")
        return 1
    print(f"all {len(CHECKS)} mechanized invariant checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
