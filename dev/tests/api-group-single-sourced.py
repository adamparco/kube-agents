#!/usr/bin/env python3
"""LSN-032: the operator's API group is written down once, and nothing restates it wrongly.

`SchemeGroupVersion` in `k8s-operator/api/v1alpha1/groupversion_info.go` is the definition site.
It is what the scheme registers, what the API server serves, and therefore what every
`group` field in every rule, fixture, RBAC binding and manifest has to agree with.

The defect this exists for was found by READING, not by any test:

    forbiddenSet in internal/broker/classify/floor.go named `kubeagents.gke-labs.dev` -- a group
    this operator has never served -- in five of its nine entries. `delete ActionRecord`,
    `patch ChangePolicy`, `delete FleetFreeze`, `create ApprovalRoster` and `patch Agent` fell
    straight through the forbidden check and classified `routine`. The 165-case corpus agreed,
    because its fixtures had been written from the same wrong string.

That is why a grep is worth a file. A wrong group is not a compile error, is not a runtime error,
and is not visible from either side of the comparison: the deny rule looks right, the fixture that
exercises it looks right, and they match each other. The only thing that disagrees is the API
server, and it never says so -- a rule keyed on a group nobody serves simply never fires.

Two properties:

  1. NO CODE LINE NAMES A `kubeagents.*` GROUP OTHER THAN THE SERVED ONE. Comments are stripped
     first (LSN-023: the sentence describing the defect must not satisfy or fail the check that
     prevents it -- this file, floor.go and classify_test.go all discuss the old string by name).
  2. NON-VACUITY. The served group must parse out of the definition site, and must actually occur
     in code across several files. A check that silently found nothing to compare would report the
     same PASS after a refactor renamed the field it reads.

Run:  python3 dev/tests/api-group-single-sourced.py
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from golex import strip_go_comments  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
GROUPVERSION_GO = REPO / "k8s-operator" / "api" / "v1alpha1" / "groupversion_info.go"

# The definition site's own line. Matched on `schema.GroupVersion{Group: "..."}` rather than on any
# occurrence of the string, so this check reads the value from where the scheme reads it.
SERVED_GROUP_RE = re.compile(r"schema\.GroupVersion\{\s*Group:\s*\"([^\"]+)\"")

# Anything in this project's `kubeagents.` namespace. Deliberately broad on the right-hand side:
# the whole point is to catch a domain nobody serves, and the set of domains nobody serves is open.
CANDIDATE_RE = re.compile(r"\bkubeagents\.[a-z0-9][a-z0-9._-]*[a-z0-9]")

# `kubeagents.` is also the OpenTelemetry resource-attribute namespace, and an attribute key is not
# an API group. The set is CLOSED rather than pattern-matched, which is the point: a typo in an
# attribute key (`kubeagents.agnet_name`) is the same class of silent defect as a typo in a group —
# the span is emitted, the dashboard queries the spelling it expects, and the panel is empty. So a
# new attribute is a line here, which is a conversation, rather than a diff nobody read.
NON_GROUP_ATTRIBUTES = frozenset(
    {
        "kubeagents.component",
        "kubeagents.agent_type",
        "kubeagents.agent_name",
        "kubeagents.chat_platform",
        "kubeagents.routing_mode",
    }
)

# File types where a group string is a fact the cluster acts on. Markdown is excluded on purpose:
# prose about the defect is how the lesson is recorded, and a lint that failed on its own postmortem
# would be deleted within a week.
SCANNED_SUFFIXES = {".go", ".yaml", ".yml", ".json", ".sh"}

# The minimum number of distinct files that must legitimately name the served group. Low enough not
# to be a maintenance burden, high enough that a regex that stopped matching cannot pass.
MIN_FILES_NAMING_THE_GROUP = 5


def tracked_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / p for p in out.split("\0") if p]


def strip_comments(path: pathlib.Path, text: str) -> str:
    """Comment-free text, per language.

    Go goes through the shared literal-aware scanner in `golex.py`. It used to use the same
    line-oriented `line.split("//", 1)[0]` as the YAML arm below, defended by the argument that a
    truncated line is safe "because the truncation happens after" the group string. That argument
    is only true when the group string precedes the `//`, and the case that matters is the one
    where it does not:

        endpoint := "https://audit.example/kubeagents.wrong.io/agents"

    The line truncates at the `//` in `https://`, the unserved group is never seen, and the check
    prints PASS. A false negative here is exactly the defect [[LSN-032]] is about -- a group nobody
    serves, agreed with by the thing that was supposed to catch it.

    The YAML/shell arm keeps its line-oriented reading: `#` has no equivalent of `https://`, and
    its quote tracking already covers the literal case.

    Comment lines are BLANKED rather than dropped: every failure this check emits carries a line
    number, and a number that points somewhere other than what the reader's editor shows is worse
    than no number at all.
    """
    if path.suffix == ".go":
        return strip_go_comments(text)
    lines = []
    for raw in text.splitlines():
        line = raw
        if path.suffix in {".yaml", ".yml", ".sh"}:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                lines.append("")
                continue
            # An inline `#` only starts a comment after whitespace, and only outside quotes. Same
            # rule the invariants gate uses, for the same reason.
            sq = dq = 0
            for i, ch in enumerate(line):
                if ch == "'" and dq % 2 == 0:
                    sq += 1
                elif ch == '"' and sq % 2 == 0:
                    dq += 1
                elif ch == "#" and sq % 2 == 0 and dq % 2 == 0 and i > 0 and line[i - 1] in " \t":
                    line = line[:i]
                    break
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    if not GROUPVERSION_GO.exists():
        print(f"FAIL: {GROUPVERSION_GO.relative_to(REPO)} not found; there is no definition site to read", file=sys.stderr)
        return 1

    m = SERVED_GROUP_RE.search(GROUPVERSION_GO.read_text())
    if not m:
        print(
            f"FAIL: VACUOUS -- could not read the served group from "
            f"{GROUPVERSION_GO.relative_to(REPO)}. The `schema.GroupVersion{{Group: \"...\"}}` "
            f"literal moved or changed shape, so this check has nothing to compare against and "
            f"would pass on every wrong group in the tree. Fix the parser, not the source.",
            file=sys.stderr,
        )
        return 1
    served = m.group(1)

    failures: list[str] = []
    files_naming_the_group: set[pathlib.Path] = set()

    for path in tracked_files():
        if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "kubeagents." not in text:
            continue
        code = strip_comments(path, text)
        for lineno, line in enumerate(code.splitlines(), start=1):
            for found in CANDIDATE_RE.findall(line):
                if found == served:
                    files_naming_the_group.add(path)
                    continue
                # `kubeagents.x-k8s.io_changepolicies.yaml` — controller-gen names a CRD base file
                # after the group it belongs to, so the served group with a `_suffix` is the served
                # group, spelled the only way a filename can spell it.
                if found.startswith(served + "_"):
                    files_naming_the_group.add(path)
                    continue
                if found in NON_GROUP_ATTRIBUTES:
                    continue
                failures.append(
                    f"{path.relative_to(REPO)}:{lineno} names the API group {found!r}, and the "
                    f"operator serves {served!r} ({GROUPVERSION_GO.relative_to(REPO)}). Nothing "
                    f"reports this at runtime: a rule, binding or fixture keyed on a group the "
                    f"API server does not serve simply never matches."
                )

    if len(files_naming_the_group) < MIN_FILES_NAMING_THE_GROUP:
        failures.append(
            f"VACUOUS: only {len(files_naming_the_group)} file(s) were seen naming {served!r}, "
            f"below the floor of {MIN_FILES_NAMING_THE_GROUP}. Either the scan stopped reading the "
            f"tree or the group moved out of code; both make a PASS here meaningless."
        )

    if failures:
        print("FAIL: LSN-032 -- the API group is restated somewhere that disagrees with the definition site", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: LSN-032 -- served group {served!r} from "
        f"{GROUPVERSION_GO.relative_to(REPO)}; {len(files_naming_the_group)} files name it and "
        f"none names another"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
