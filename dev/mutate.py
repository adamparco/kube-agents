#!/usr/bin/env python3
"""Run a mutation sweep from a spec file. The sweep is CONFIGURED, not authored.

    python3 dev/mutate.py SPEC.json [--verbose]

Companion to `dev/mutate.sh`, which is the general "snapshot files, run a command, always put them
back" tool. This is the layer above it: the thing every unit kept re-authoring from scratch, and
kept re-earning a lesson from. Three of them, in four units:

  [[LSN-047]]  A throwaway driver snapshotted two files named `cooldown.go` into one directory keyed
               by BASENAME, and restored the wrong one over the other. `dev/mutate.sh` already did
               this correctly -- keyed by position -- and nothing routed the unit's sweep to it.
               A tool nobody is directed to is a tool that gets reimplemented, badly, by whoever is
               in a hurry.
  [[LSN-048]]  A sweep ran `go test -run TestBrakeFailClosed`; the test is called
               `TestBrakeEachRuleFiresInIsolation`. A `-run` pattern matching nothing prints a
               warning and EXITS 0, so three mutants scored as survivors that had never been
               evaluated. A false RED that recommends a plausible action (go strengthen the test)
               is worse than one that merely wastes time: the "fix" passes on the first run and the
               mutants stay unmeasured forever.
  [[LSN-049]]  A needle containing `""` was interpolated into a double-quoted `bash -c` argument.
               The quote closed the string early, the applier died, `&&` short-circuited, and the
               surrounding invocation still exited 0. The sweep read that 0 as "the suite passed
               with the mutation in place" and invented a hole in a property the suite catches
               cleanly.

All three reduce to one root: **the sweep scored an exit code it had not established was the test
suite's.** `rc == 0` in a mutation sweep is produced by a pipeline of things that are not the test
suite -- a `-run` filter, a shell parse, an applier, an `&&` -- and any of them can hand back a 0
meaning "I did nothing".

So this runner refuses to produce a number it cannot back:

  1. **Mutation text never crosses a shell parse.** Needles and replacements live in the spec's
     JSON and are applied by `str.replace` in this process. Nothing is interpolated into a command
     line, so a needle may contain quotes, backslashes, newlines, `$` -- anything Go source can.
  2. **The applier refuses unless the needle appears EXACTLY once.** A stale needle scores BROKEN,
     never a silent escape, and an ambiguous one never lands in two places at once.
  3. **The mutation is observed to have landed** before anything is scored. The applier's failure
     and the suite's success are different outcomes and never share an exit code.
  4. **No `-run` pattern.** The whole package's tests run for every mutant. A spec that tries to
     narrow the run is rejected: filtering is how LSN-048 happened, and running everything is also
     what makes rule 5 meaningful.
  5. **Every mutant names the test that must fail, and that test must actually exist.** The
     catchers are checked against `go test -list` BEFORE the first mutation, so a misremembered
     name is a refusal up front rather than a survivor at the end. `rc != 0` is not a catch: a
     mutant that reddens the package via a DIFFERENT test than the one it names has found
     something, but not the thing the row claims, and that scores as an escape.
  6. **Restore is a byte copy** of a snapshot taken here, keyed by position, on success, on
     failure and on signal. Never `git checkout` / `git restore` / `git stash` -- see
     [[LSN-030]] and [[LSN-022]] for the two hours that rule cost.

Three verdicts, not two:

  caught   the package went red AND the named catcher was among the failures
  ESCAPED  the mutation landed, the package built, and the suite stayed green (or went red without
           the named catcher). A real hole, or a real mis-attribution.
  BROKEN   the sweep could not evaluate the mutant -- stale needle, mutant that does not compile,
           unknown catcher. Scored as NEITHER caught nor survived so the denominator cannot
           silently shrink.

A BROKEN row is not a finding. The natural move on an apparent survivor is to go strengthen a test,
and doing that to a BROKEN row produces a test that passes immediately, looks exactly like the fix,
and leaves the mutant unmeasured. Fix the row, then re-run.

Spec format:

    {
      "suite": {
        "kind": "go",                       // or "unittest"
        "dir": "k8s-operator",              // cwd, repo-relative
        "packages": ["./internal/broker/..."]
      },
      "mutants": [
        {
          "id": "M1-apply-is-whole-object-again",
          "why": "one sentence: what a reviewer loses if this goes uncaught",
          "catcher": "TestAnApplyIsClassifiedFieldByFieldAndExecutes",
          "edits": [
            {"file": "k8s-operator/internal/broker/classify/resolve.go",
             "find":  "...exact text, exactly once in the file...",
             "replace": "...what it becomes..."}
          ]
        }
      ]
    }

For `"kind": "unittest"`, `packages` is the list of `python3 -m unittest` targets (e.g.
`["discover", "dev"]`) and `dir` is the repo root.

Exit: 0 = every mutant caught. 1 = at least one ESCAPED. 2 = at least one BROKEN, or the sweep
could not run at all (red baseline, unreadable spec, unknown catcher).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]

GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)", re.M)
GO_LIST = re.compile(r"^(Test\w+|Fuzz\w+|Example\w+|Benchmark\w+)$", re.M)
# `FAIL: test_name (module.Class.test_name)` and the `ERROR:` variant, which is also a failure.
PY_FAIL = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)", re.M)

# A build failure is not a test failure, and Go says so in two spellings depending on whether the
# package under test or one of its dependencies is the one that broke.
GO_BUILD_FAILED = ("[build failed]", "build failed", "cannot find package", "# command-line-arguments")


class Broken(Exception):
    """The sweep cannot evaluate something. Never scored as a result."""


# ------------------------------------------------------------------------------------------------
# Suites
# ------------------------------------------------------------------------------------------------


class GoSuite:
    kind = "go"

    def __init__(self, dir_: pathlib.Path, packages: list[str]) -> None:
        self.dir = dir_
        self.packages = packages

    def run(self) -> tuple[int, str, bool]:
        """(returncode, output, built). `built` is False when the compiler, not a test, said no."""
        p = subprocess.run(
            ["go", "test", "-count=1", *self.packages],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )
        out = p.stdout + p.stderr
        built = not any(m in out for m in GO_BUILD_FAILED)
        return p.returncode, out, built

    def failing(self, out: str) -> set[str]:
        return set(GO_FAIL.findall(out))

    def known_tests(self) -> set[str]:
        p = subprocess.run(
            ["go", "test", "-list", ".*", *self.packages],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            raise Broken(f"`go test -list` failed, so no catcher can be verified:\n{p.stdout}{p.stderr}")
        return set(GO_LIST.findall(p.stdout))


class UnittestSuite:
    kind = "unittest"

    def __init__(self, dir_: pathlib.Path, packages: list[str]) -> None:
        self.dir = dir_
        self.packages = packages

    def run(self) -> tuple[int, str, bool]:
        p = subprocess.run(
            [sys.executable, "-m", "unittest", *self.packages],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )
        out = p.stdout + p.stderr
        # An import error inside a discovered module is Python's build failure: the suite never ran.
        built = "Traceback (most recent call last)" not in out or "FAIL:" in out or "ERROR:" in out
        return p.returncode, out, built

    def failing(self, out: str) -> set[str]:
        return set(PY_FAIL.findall(out))

    def known_tests(self) -> set[str]:
        # `unittest` has no `-list`. Collect via the loader, which is the same discovery the run uses.
        code = (
            "import unittest,sys\n"
            "def walk(s):\n"
            "  for t in s:\n"
            "    walk(t) if isinstance(t, unittest.TestSuite) else print(t.id().rsplit('.',1)[-1])\n"
            f"walk(unittest.defaultTestLoader.discover({self.packages[-1]!r}))\n"
        )
        p = subprocess.run(
            [sys.executable, "-c", code], cwd=self.dir, capture_output=True, text=True
        )
        if p.returncode != 0:
            raise Broken(f"could not enumerate unittest names:\n{p.stdout}{p.stderr}")
        return {ln.strip() for ln in p.stdout.splitlines() if ln.strip()}


SUITES = {"go": GoSuite, "unittest": UnittestSuite}


# ------------------------------------------------------------------------------------------------
# Spec
# ------------------------------------------------------------------------------------------------


def load_spec(path: pathlib.Path) -> tuple[object, list[dict]]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Broken(f"could not read the spec {path}: {exc}")
    if not isinstance(spec, dict) or "suite" not in spec or "mutants" not in spec:
        raise Broken(
            f"{path}: a spec is an object with `suite` and `mutants`. A bare list of mutants is the "
            f"throwaway shape this runner exists to replace (LSN-047)."
        )

    s = spec["suite"]
    kind = s.get("kind")
    if kind not in SUITES:
        raise Broken(f"unknown suite kind {kind!r}; known: {sorted(SUITES)}")
    packages = s.get("packages") or []
    if not packages:
        raise Broken("suite.packages is empty; there is nothing to run")
    for arg in packages:
        if arg.startswith("-run") or arg == "-run":
            raise Broken(
                "suite.packages contains a `-run` filter. Rule 4: the whole package runs for every "
                "mutant. A `-run` pattern that matches nothing exits 0, which is how LSN-048 scored "
                "three unevaluated mutants as survivors -- and running everything is what lets a "
                "mutant that reddens a DIFFERENT test than it claims be seen at all."
            )
    suite = SUITES[kind](REPO / s.get("dir", "."), packages)

    mutants = spec["mutants"]
    if not isinstance(mutants, list) or not mutants:
        raise Broken("`mutants` is empty; a sweep of nothing reports 0/0 caught and means nothing")
    ids = set()
    for i, m in enumerate(mutants):
        for field in ("id", "why", "catcher", "edits"):
            if not m.get(field):
                raise Broken(f"mutant {i}: missing `{field}`")
        if m["id"] in ids:
            raise Broken(f"duplicate mutant id {m['id']!r}; the report would be unreadable")
        ids.add(m["id"])
        for e in m["edits"]:
            for field in ("file", "find", "replace"):
                if field not in e:
                    raise Broken(f"mutant {m['id']}: edit missing `{field}`")
            if e["find"] == e["replace"]:
                raise Broken(f"mutant {m['id']}: find == replace, so nothing is mutated")
    return suite, mutants


# ------------------------------------------------------------------------------------------------
# Apply / restore
# ------------------------------------------------------------------------------------------------


def apply_edit(edit: dict) -> None:
    """Rewrite one file. Refuses unless the needle appears exactly once.

    Exactly-once is doing three jobs. It turns a stale needle into a refusal instead of a no-op
    that scores as an escape. It stops one mutation landing in two places, where the second site
    is the one that reddens the suite and the report names the first. And it is the observation
    that the mutation LANDED, which is the thing LSN-049's `&&` chain never made.
    """
    path = REPO / edit["file"]
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Broken(f"cannot read {edit['file']}: {exc}")
    n = src.count(edit["find"])
    if n != 1:
        raise Broken(
            f"the needle occurs {n} times in {edit['file']}, want exactly 1. "
            f"A stale needle is a BROKEN row, never a survivor:\n"
            f"        {edit['find'].splitlines()[0][:100] if edit['find'] else ''}"
        )
    path.write_text(src.replace(edit["find"], edit["replace"]), encoding="utf-8")


class Snapshot:
    """Byte copies keyed by POSITION, not by basename.

    Basename keying is LSN-047 verbatim: two files called `cooldown.go`, one snapshot directory,
    the second `cp` overwrites the first, and the restore puts the wrong one over both. The damage
    surfaced on the NEXT run, as eleven "anchor not found" lines.
    """

    def __init__(self, files: list[pathlib.Path]) -> None:
        self.files = files
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="mutate-"))
        self.restored = False
        for i, f in enumerate(files):
            if not f.is_file():
                shutil.rmtree(self.dir, ignore_errors=True)
                # `relative_to` and not `os.path.relpath`, guarded: an absolute `file` in the spec
                # can point outside the repo, and a ValueError from the pretty-printer would
                # replace this refusal with a stack trace.
                try:
                    shown: object = f.relative_to(REPO)
                except ValueError:
                    shown = f
                raise Broken(f"no such file: {shown}")
            shutil.copy2(f, self.dir / str(i))

    def restore(self) -> None:
        if self.restored:
            return
        self.restored = True
        for i, f in enumerate(self.files):
            shutil.copy2(self.dir / str(i), f)
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------------------------------------
# Sweep
# ------------------------------------------------------------------------------------------------


def score(mutant: dict, rc: int, out: str, built: bool, suite) -> tuple[str, str]:
    if not built:
        return "BROKEN", "the mutant does not compile, so the check was never asked anything"
    if rc == 0:
        return "ESCAPED", "the suite stayed green with the mutation in place"
    failures = suite.failing(out)
    if mutant["catcher"] not in failures:
        return (
            "ESCAPED",
            f"the suite went red, but not via {mutant['catcher']} — {sorted(failures) or 'no named failure'}",
        )
    return "caught", mutant["catcher"]


def sweep(suite, mutants: list[dict], verbose: bool) -> int:
    known = suite.known_tests()
    unknown = sorted({m["catcher"] for m in mutants} - known)
    if unknown:
        raise Broken(
            "these mutants name a catcher that does not exist in the suite: "
            + ", ".join(unknown)
            + ".\nRefused before the first mutation, because a misremembered name is how LSN-048 "
            "turned three unevaluated mutants into three plausible-looking holes."
        )

    rc, out, built = suite.run()
    if rc != 0 or not built:
        raise Broken(
            "the baseline is RED. A sweep scores mutants by whether they turn the suite red, so a "
            "suite that is already red measures nothing:\n" + out[-4000:]
        )
    print(f"baseline: green · {len(mutants)} mutants · catchers verified against the suite\n")

    results: list[tuple[str, str, str]] = []
    for m in mutants:
        files = sorted({REPO / e["file"] for e in m["edits"]})
        try:
            snap = Snapshot(files)
        except Broken as exc:
            results.append((m["id"], "BROKEN", str(exc)))
            print(f"{'BROKEN':>7}  {m['id']:<40} {exc}")
            continue

        prev = [signal.signal(s, lambda *_: (snap.restore(), os._exit(130))) for s in (signal.SIGINT, signal.SIGTERM)]
        try:
            for e in m["edits"]:
                apply_edit(e)
        except Broken as exc:
            verdict, detail = "BROKEN", str(exc)
        else:
            rc, out, built = suite.run()
            verdict, detail = score(m, rc, out, built, suite)
            if verbose and verdict != "caught":
                detail += "\n" + "\n".join(f"          {ln}" for ln in out.splitlines()[-30:])
        finally:
            snap.restore()
            for s, h in zip((signal.SIGINT, signal.SIGTERM), prev):
                signal.signal(s, h)

        results.append((m["id"], verdict, detail))
        print(f"{verdict:>7}  {m['id']:<40} {detail}")

    caught = [r for r in results if r[1] == "caught"]
    escaped = [r for r in results if r[1] == "ESCAPED"]
    broken = [r for r in results if r[1] == "BROKEN"]

    print(f"\n{len(caught)}/{len(results)} caught", end="")
    if escaped:
        print(f" · {len(escaped)} ESCAPED", end="")
    if broken:
        print(f" · {len(broken)} BROKEN", end="")
    print()

    if broken:
        print(
            "\nBROKEN rows are not findings. Fix the row and re-run. Strengthening a test against a "
            "BROKEN row produces a test that passes on the first try, looks exactly like the fix, "
            "and leaves the mutant unmeasured (LSN-048)."
        )
        return 2
    return 1 if escaped else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a mutation sweep from a spec file.")
    ap.add_argument("spec", type=pathlib.Path)
    ap.add_argument("--verbose", action="store_true", help="print suite output for non-caught rows")
    args = ap.parse_args()

    try:
        suite, mutants = load_spec(args.spec)
        return sweep(suite, mutants, args.verbose)
    except Broken as exc:
        print(f"BROKEN SWEEP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
