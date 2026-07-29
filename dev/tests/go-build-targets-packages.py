#!/usr/bin/env python3
"""Every `go build` in this repository builds a PACKAGE, never a file list (P9-T7b; LSN-037).

`go build ./cmd/broker` compiles the package: every .go file in that directory, which is what the
Go toolchain, `go vet`, and `go test` all mean by "the broker". `go build cmd/broker/main.go`
compiles exactly the one file named and pretends the rest of the package does not exist. The two
spellings are indistinguishable right up until a package acquires its second file, and then the
file-list form fails on the symbols defined in the file it silently dropped.

That is not hypothetical here. `Dockerfile.broker` shipped the file-list form, and it broke the
moment `waitforbroker.go` landed beside `main.go`:

    cmd/broker/main.go:128:8: undefined: waitOptions
    cmd/broker/main.go:150:13: undefined: runWaitForBroker

`go build ./...`, `go vet ./...` and `go test ./...` were all green on that branch, and six of the
seven CI checks passed. The image build was the only red one, because it was the only build in the
repository not compiling whole packages. The failure therefore arrives late, at the step furthest
from the edit, and it looks like an infrastructure problem rather than a one-token spelling
mistake -- which is exactly the profile of a defect worth mechanizing rather than remembering.

Properties (all must hold for exit 0):

  1. Every `go build` invocation in every build input -- Dockerfiles, Makefiles, shell scripts,
     workflows -- targets a package path, not one or more `.go` files.
  2. Build inputs are DISCOVERED by glob, not enumerated. A new Dockerfile or a new Makefile target
     is covered the day it lands, with no edit here. This is the LSN-036 arm: a check that lists the
     files it knows about is a headcount of today's tree, and it goes quiet exactly when the tree
     grows.
  3. Non-vacuity (LSN-035): the sweep found at least one real `go build`. A check whose subject has
     been refactored out from under it prints PASS forever, so finding nothing is a FAIL.

Usage:
    python3 dev/tests/go-build-targets-packages.py [REPO_ROOT]
    python3 dev/tests/go-build-targets-packages.py --negative-control

Exit 0 = every build compiles whole packages; 1 = one or more file-list builds. Stdlib only.
"""
from __future__ import annotations

import os
import re
import sys

# Where build inputs live. Directories that hold vendored or generated trees are skipped outright --
# we are auditing how THIS repository builds itself, not how its dependencies build themselves.
SKIP_DIRS = {".git", "node_modules", "vendor", "bin", "dist", ".venv", "__pycache__"}

# A build input is anything that can invoke a compiler. Matched on name, so `Dockerfile.broker`,
# `Makefile`, `up.sh` and `release.yml` all qualify without being named individually (property 2).
BUILD_INPUT_RE = re.compile(
    r"""^(
        Dockerfile(\..+)?      # Dockerfile, Dockerfile.broker, Dockerfile.router
      | Makefile | .+\.mk
      | .+\.sh
      | .+\.ya?ml              # workflows, Cloud Build, GoReleaser
    )$""",
    re.VERBOSE,
)

# `go build` up to the end of the logical line. `[^\n\\]*(?:\\\n[^\n\\]*)*` walks backslash
# continuations so a build split across Dockerfile lines is still read as one invocation.
GO_BUILD_RE = re.compile(r"\bgo\s+build\b[^\n\\]*(?:\\\n[^\n\\]*)*")

# A `.go` operand that is not the argument to a flag. `-o main.go` would be a bizarre output name
# but it is an output, not a target, so the flag-argument case is excluded explicitly.
GO_FILE_OPERAND_RE = re.compile(r"(?<![\w./-])(?:[\w./-]*/)?[\w.-]+\.go\b")
FLAG_WITH_ARG_RE = re.compile(r"(?:^|\s)-(?:o|ldflags|gcflags|asmflags|tags|buildmode|pkgdir)(?:=|\s+)(\S+)")


def _logical_line(text: str, start: int) -> int:
    """1-based line number of an offset, for an error message a reader can jump to."""
    return text.count("\n", 0, start) + 1


def _strip_flag_args(invocation: str) -> str:
    """Remove flag ARGUMENTS so `-o broker` and `-ldflags=...` cannot be mistaken for targets."""
    return FLAG_WITH_ARG_RE.sub(" ", invocation)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return (line, invocation, offending operand) for each file-list `go build`."""
    out: list[tuple[int, str, str]] = []
    for m in GO_BUILD_RE.finditer(text):
        invocation = m.group(0)
        # Comment lines describing the rule must not trip the rule (LSN-023): a Dockerfile comment
        # that says "not `go build cmd/broker/main.go`" is documentation, not an invocation.
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()].lstrip()
        if prefix.startswith("#") or prefix.startswith("//"):
            continue
        for operand in GO_FILE_OPERAND_RE.finditer(_strip_flag_args(invocation)):
            out.append((_logical_line(text, m.start()), " ".join(invocation.split()), operand.group(0)))
    return out


def iter_build_inputs(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if BUILD_INPUT_RE.match(name):
                yield os.path.join(dirpath, name)


def check(root: str) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    passes: list[str] = []
    total_builds = 0
    files_with_builds: list[str] = []

    for path in iter_build_inputs(root):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        if "go build" not in text:
            continue
        rel = os.path.relpath(path, root)
        found = len(GO_BUILD_RE.findall(text))
        # A comment mentioning `go build` is not a build; only count files that survive the scan as
        # having real invocations, but count invocations from the regex so the tally is honest.
        total_builds += found
        files_with_builds.append(rel)
        for line, invocation, operand in scan_text(text):
            errors.append(
                f"[{rel}:{line}] builds the FILE {operand!r}, not a package:\n"
                f"            {invocation}\n"
                f"        Use the package path (`./cmd/...`). A file-list build drops every other\n"
                f"        file in the same package, and breaks the day that package gains a second\n"
                f"        file -- which is how Dockerfile.broker broke in P9-T7b (LSN-037)."
            )

    # Property 3 -- non-vacuity (LSN-035). If the sweep matched nothing, the check is not passing,
    # it is unemployed: the build inputs moved somewhere the glob does not look.
    if total_builds == 0:
        errors.append(
            "[vacuity] found no `go build` invocation in any build input. This check cannot pass "
            "vacuously: either the builds moved out of the globbed file types (extend BUILD_INPUT_RE) "
            "or this check has outlived its subject and should be retired with an ID, not deleted."
        )
    else:
        passes.append(
            f"{total_builds} `go build` invocation(s) across {len(files_with_builds)} build input(s) "
            f"target packages: {', '.join(files_with_builds)}"
        )
        passes.append("build inputs discovered by glob, so a new Dockerfile or Makefile target is covered unedited")

    return errors, passes, total_builds


# --- negative control -------------------------------------------------------------------------
# `¬` in 09 §6. Each mutation is a spelling someone would plausibly write, and the check must reject
# every one of them. Without this, a regex that silently stopped matching would report a clean tree.
#
# (label, snippet, operands) -- the third element is every offending operand the scan must NAME, not
# merely the fact that it found something ([[LSN-035]]). It is what separates "this snippet contains
# a file build" from "the scan located the file build I planted, all of it". The two-file mutation
# is the one that makes the difference: a scan that stops at the first operand would report a hit
# and pass a non-emptiness assertion while missing `waitforbroker.go` -- the exact file whose
# omission from the Dockerfile build is what LSN-037 is about.
MUTATIONS = [
    ("the kubebuilder scaffold's default, restored",
     "RUN CGO_ENABLED=0 go build -a -o manager cmd/main.go",
     ["cmd/main.go"]),
    ("a nested main package built by file",
     "RUN go build -o broker cmd/broker/main.go",
     ["cmd/broker/main.go"]),
    ("a Makefile recipe built by file",
     "\tgo build -o bin/router cmd/router/main.go",
     ["cmd/router/main.go"]),
    ("two files listed explicitly, which looks like it fixes the problem",
     "RUN go build -o broker cmd/broker/main.go cmd/broker/waitforbroker.go",
     ["cmd/broker/main.go", "cmd/broker/waitforbroker.go"]),
    ("a file-list build with flags in front of it",
     'RUN go build -ldflags="-w -s" -tags netgo -o /workspace/x ./cmd/x/main.go',
     ["./cmd/x/main.go"]),
    ("a bare file target with no -o at all",
     "RUN go build cmd/main.go",
     ["cmd/main.go"]),
    ("a continuation-split build whose target is on the second line",
     "RUN CGO_ENABLED=0 \\\n      go build -a -o broker \\\n      cmd/broker/main.go",
     ["cmd/broker/main.go"]),
]

# Spellings that are CORRECT and must not be flagged. A check that rejects the fix as well as the
# defect is worse than no check: it teaches people to skip it.
NON_MUTATIONS = [
    ("the package form", "RUN go build -a -o broker ./cmd/broker"),
    ("the package form with a trailing slash", "RUN go build -o /workspace/eventingress ./cmd/eventingress/"),
    ("the current package", "RUN go build -o x ."),
    ("a wildcard build", "RUN go build ./..."),
    ("a comment describing the rule (LSN-023)", "# never `go build cmd/broker/main.go` -- build ./cmd/broker"),
    ("an output binary that happens to end in .go is still a flag argument", "RUN go build -o weird.go ./cmd/x"),
]


def negative_control() -> int:
    print("go-build-targets-packages: negative control")
    failures = 0

    for label, snippet, operands in MUTATIONS:
        named = [operand for _, _, operand in scan_text(snippet)]
        missed = [o for o in operands if o not in named]
        if not named:
            print(f"  FAIL  NOT rejected: {label}\n          {snippet!r}", file=sys.stderr)
            failures += 1
        elif missed:
            print(f"  FAIL  rejected, but not for the operand(s) it was about: {label}\n"
                  f"          missed {missed!r}; named {named!r}", file=sys.stderr)
            failures += 1
        else:
            print(f"  PASS  rejected, naming {', '.join(operands)}: {label}")

    for label, snippet in NON_MUTATIONS:
        hits = scan_text(snippet)
        if hits:
            print(f"  FAIL  false positive on {label}: {hits}", file=sys.stderr)
            failures += 1
        else:
            print(f"  PASS  accepted: {label}")

    if failures:
        print(f"\ngo-build-targets-packages: negative control FAILED ({failures}).", file=sys.stderr)
        return 1
    print(f"\ngo-build-targets-packages: negative control OK "
          f"({len(MUTATIONS)} rejected, {len(NON_MUTATIONS)} accepted).")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--negative-control" in args:
        return negative_control()

    root = args[0] if args else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    print(f"go-build-targets-packages: every `go build` compiles a package (root={root})")
    errors, passes, _ = check(root)
    for p in passes:
        print(f"  PASS  {p}")
    if errors:
        print()
        for e in errors:
            print(f"  FAIL  {e}", file=sys.stderr)
        print(f"\ngo-build-targets-packages: {len(errors)} file-list build(s).", file=sys.stderr)
        return 1
    print("\ngo-build-targets-packages: OK — no build in this repository can drop a file from its package.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
