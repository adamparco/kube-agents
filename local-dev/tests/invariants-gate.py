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

Run: python3 local-dev/tests/invariants-gate.py [--update-baseline]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LESSONS = REPO / ".claude/harness/LESSONS.md"
LEDGER = REPO / "docs/build/LEDGER.md"
L0_CHAIN = REPO / "local-dev/L0-CHAIN.txt"
L2_CHAIN = REPO / "local-dev/L2-CHAIN.txt"
WORKFLOWS = REPO / ".github/workflows"
BASELINE = REPO / "local-dev/assertion-baseline.json"

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
# Probed as paths rather than as prose so that this check STOPS failing on its own the moment the
# machinery actually lands -- a gate that needs hand-editing to notice progress gets hand-edited to
# pass. Each entry: (human name, [candidate paths], substring that must appear in one of them).
MACHINERY = [
    (
        "Action Broker",
        ["k8s-operator/internal/broker", "k8s-operator/internal/actionbroker"],
        None,
    ),
    (
        "risk classifier",
        ["k8s-operator/internal/classifier", "k8s-operator/internal/risk"],
        None,
    ),
    (
        "ActionRecord journal",
        ["k8s-operator/api/v1alpha1/actionrecord_types.go"],
        None,
    ),
    (
        "undo path",
        ["k8s-operator/internal/undo", "k8s-operator/internal/broker/undo.go"],
        None,
    ),
]

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


def missing_machinery() -> list[str]:
    absent = []
    for name, candidates, _ in MACHINERY:
        if not any((REPO / c).exists() for c in candidates):
            absent.append(name)
    return absent


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
    out += sorted((REPO / "local-dev").glob("test_*.py"))
    out += sorted((REPO / "local-dev/tests").glob("*.py"))
    out += sorted((REPO / "local-dev/kind").glob("*.sh"))
    out += sorted((REPO / "local-dev/tests").glob("*.sh"))
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
    # `python3 -m unittest discover local-dev` runs every local-dev/test_*.py without naming one.
    # Requiring the name to appear literally would reopen lessons closed by a real, running test.
    if (
        base.startswith("test_")
        and base.endswith(".py")
        and (REPO / "local-dev" / base).exists()
        and "unittest discover local-dev" in chain
    ):
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
    if "unittest discover local-dev" not in chain:
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
        resolved = []
        for a in artifacts:
            hits = list(REPO.rglob(a)) if "/" not in a else ([REPO / a] if (REPO / a).exists() else [])
            resolved += [h for h in hits if h.exists()]
        if not resolved:
            failures.append(
                f"{lid} is `closed` naming {artifacts}, none of which exists on disk."
            )
            continue
        if not any(_invoked_by(a, chain) for a in artifacts):
            failures.append(
                f"{lid} is `closed` naming {artifacts}, which exist but are run by nothing: no "
                f"line of L0-CHAIN.txt or L2-CHAIN.txt, no step of any workflow. An artifact "
                f"nothing runs is not a mechanization; wire it into a chain or reopen the lesson."
            )
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

# A script whose context comes from the caller: `CTX="${1:-kind-...}"`. Those are the ones that can
# be pointed anywhere. up.sh / up-egress.sh build `kind-$CLUSTER` themselves and are not in scope --
# there is no argument to aim at the live cluster.
CALLER_CTX = re.compile(r'^\s*CTX="?\$\{\d+:-', re.MULTILINE)
CASE_ON_CTX = re.compile(r'case\s+"?\$(?:\{)?CTX(?:\})?"?\s+in(.*?)^\s*esac', re.MULTILINE | re.DOTALL)
ANCHORED_ARM = re.compile(r"^(?:kind|gke-scratch)-[A-Za-z0-9*_.-]*\*?$")


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

    The assertion is specifically about ANCHORING, not about the guard existing. `*kind*)` and
    `[[ $CTX == *kind* ]]` both look like guards and both accept `platform-agent-host-kind-backup`;
    an anchored `kind-*` cannot. LSN-005 is the substring-match lesson, so a check that only
    asserted "there is a case statement" would pass the exact code the lesson is about.
    """
    scripts = sorted((REPO / "local-dev").rglob("*.sh"))
    scoped = [p for p in scripts if CALLER_CTX.search(p.read_text())]
    if len(scoped) < 10:
        return [
            f"VACUOUS: found only {len(scoped)} caller-context scripts under local-dev/; there "
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
                    f"{rel}: guard arm {pat!r} is not anchored to `kind-` or `gke-scratch-`. "
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

    ledger_text = LEDGER.read_text()
    chain = regress_chain_text()
    failures = []
    for cells in rows:
        _date, subject, blocker, owner, promote = cells[:5]
        closed = "CLOSED" in blocker.upper()
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
            # somewhere. Search the verification log for a pass row naming the same ID.
            green = any(
                cid in ln and "**pass**" in ln
                for ln in ledger_text.splitlines()
                if ln.startswith("|")
            )
            if not green:
                failures.append(
                    f"deferral {subject!r} defers {cid}, a BLOCKING-ALWAYS check, and no row of "
                    f"the verification log records it passing at any level. 09 §9.6: if it "
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
L2_CHAIN_FLOOR = 6
# How many scripts the TRANSITIVE scope held when it was widened (2026-07-25, P8-T8). A separate
# ratchet from the one above because the two guard different things: L2_CHAIN_FLOOR notices a line
# leaving L2-CHAIN.txt, this one notices a claim-making script leaving the closure — including one
# that leaves by being un-called, or by renaming its verdict functions, neither of which touches the
# chain file at all.
L2_SCOPE_FLOOR = 16
# A script whose output is read as a verdict defines both of these. Derived rather than listed,
# because a curated roster of "the L2 scripts" is a roster someone must remember to extend, and the
# gap this widening closed existed for five phases precisely because nobody did. Both are required:
# up.sh and up-egress.sh provision clusters, lib/preconditions.sh is sourced for its helpers, and none
# of the three renders a verdict that a stale image or a grandfathered object could falsify.
VERDICT_FUNCS = (
    re.compile(r"^\s*(?:function\s+)?pass\s*\(\)", re.MULTILINE),
    re.compile(r"^\s*(?:function\s+)?bad\s*\(\)", re.MULTILINE),
)
# A reference to another shell script as it appears in code: bare, $REPO_ROOT-prefixed, or assembled
# from a loop variable — verify-phase5.sh reaches phases 2-4 as `verify-phase$p.sh`, and a resolver
# that only understood literals would have missed verify-phase4.sh entirely. Variables are resolved by
# GLOB, which over-approximates; that is the safe direction for a check that fails on an ABSENT
# declaration, since the worst case is asking a script to explain itself that did not have to.
SCRIPT_REF = re.compile(r"(local-dev/[A-Za-z0-9_./${}-]*\.sh)")


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
    """Every local-dev/*.sh path this script's CODE names, with shell variables glob-expanded.

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
    whether they render verdicts — up-egress.sh renders none but reaches two scripts that do — and
    filtered out of the result at the end.
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

CHECKS = [
    ("invariant 7 — authority never precedes machinery", check_write_verbs_have_machinery),
    ("invariant 8 / V-MET-003 — assertion ratchet", check_assertion_ratchet),
    ("V-MET-004 — retirements name replacements", check_retirements_name_replacements),
    ("LSN-005 — destructive-test guards stay anchored", check_destructive_guards_are_anchored),
    ("LSN-018 — build targets name their cluster", check_make_targets_are_context_explicit),
    ("V-MET-006 / LSN-008 — deferrals name a blocker", check_deferrals_name_blockers),
    (
        "LSN-001/002/003 — L2 scripts declare and back their preconditions",
        check_l2_scripts_declare_preconditions,
    ),
    ("invariant 13 / LSN-019 — closed lessons are executable", check_closed_lessons_are_executable),
    ("P9 — `.status` reads are polled, not slept on", check_l2_status_reads_are_polled),
    ("P3 — recreated pods are resolved by ownership", check_p3_pods_resolved_by_ownership),
    ("L0 chain is runnable and wired to CI", check_l0_chain_is_runnable),
]


def update_baseline() -> None:
    existing = load_baseline()
    payload = {
        "_comment": (
            "Baseline for the assertion ratchet (V-MET-003, invariants.md 8). Regenerate with "
            "`python3 local-dev/tests/invariants-gate.py --update-baseline` ONLY when adding "
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
