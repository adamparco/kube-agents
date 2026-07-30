#!/usr/bin/env python3
"""Tests for `dev/mutate.py`, the sanctioned mutation-sweep runner.

Every test here is one of the three failures the runner exists to make impossible. They are written
as reproductions rather than as API tests: a test named `test_snapshot_keys_by_position` that builds
one file proves nothing about [[LSN-047]], whose whole content is that two files shared a basename.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "mutate_py", pathlib.Path(__file__).resolve().parent / "mutate.py"
)
mutate = importlib.util.module_from_spec(_SPEC)
sys.modules["mutate_py"] = mutate
_SPEC.loader.exec_module(mutate)


class FakeSuite:
    """A suite whose verdict is dictated by the current contents of the files under test.

    `reddens` maps a substring to the name of the test that "fails" when it is present. That keeps
    the end-to-end tests honest about the one thing that matters -- the runner must observe that
    the mutation LANDED before it scores anything -- without a Go toolchain in the loop.
    """

    kind = "fake"

    def __init__(self, files, reddens, breaks_build=()):
        self.files = [pathlib.Path(f) for f in files]
        self.reddens = reddens
        self.breaks_build = breaks_build
        self.runs = 0

    def _text(self):
        return "".join(f.read_text() for f in self.files)

    def run(self):
        self.runs += 1
        text = self._text()
        if any(b in text for b in self.breaks_build):
            return 2, "# example.com/pkg\n./x.go:3:2: undefined: nope [build failed]\n", False
        red = [t for needle, t in self.reddens.items() if needle in text]
        if not red:
            return 0, "ok\tpkg\t0.1s\n", True
        return 1, "".join(f"--- FAIL: {t} (0.00s)\n" for t in red), True

    def failing(self, out):
        return set(mutate.GO_FAIL.findall(out))

    def known_tests(self):
        return set(self.reddens.values())


def _write(d: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class SnapshotKeysByPosition(unittest.TestCase):
    """LSN-047. Two files, one basename, and the restore put the wrong one over both."""

    def test_two_files_with_the_same_basename_are_both_restored(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            a = _write(d, "cooldown/cooldown.go", "package cooldown // A\n")
            b = _write(d, "verify/cooldown.go", "package verify // B\n")

            snap = mutate.Snapshot([a, b])
            a.write_text("mutated A\n")
            b.write_text("mutated B\n")
            snap.restore()

            self.assertEqual(a.read_text(), "package cooldown // A\n")
            self.assertEqual(b.read_text(), "package verify // B\n")

    def test_restore_is_idempotent_so_the_signal_path_cannot_double_restore(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            snap = mutate.Snapshot([f])
            f.write_text("mutated\n")
            snap.restore()
            f.write_text("written after the sweep finished\n")
            snap.restore()  # the EXIT arm firing after the signal arm already ran
            self.assertEqual(f.read_text(), "written after the sweep finished\n")

    def test_a_missing_file_is_refused_rather_than_snapshotted_as_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "gone.go"
            with self.assertRaises(mutate.Broken):
                mutate.Snapshot([missing])


class TheCatcherMustExist(unittest.TestCase):
    """LSN-048. `-run TestBrakeFailClosed` matched nothing, exited 0, and scored three survivors."""

    def test_an_unknown_catcher_is_refused_before_the_first_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            suite = FakeSuite([f], {"mutated": "TestTheRealName"})
            mutants = [
                {
                    "id": "M1",
                    "why": "w",
                    "catcher": "TestBrakeFailClosed",
                    "edits": [{"file": str(f), "find": "original", "replace": "mutated"}],
                }
            ]
            with self.assertRaises(mutate.Broken) as cm:
                mutate.sweep(suite, mutants, verbose=False)
            self.assertIn("TestBrakeFailClosed", str(cm.exception))
            self.assertEqual(suite.runs, 0, "refused before the baseline, let alone a mutation")
            self.assertEqual(f.read_text(), "original\n")

    def test_a_spec_may_not_narrow_the_run(self):
        with tempfile.TemporaryDirectory() as td:
            spec = pathlib.Path(td) / "s.json"
            spec.write_text(
                '{"suite": {"kind": "go", "dir": ".", "packages": ["./p", "-run", "TestX"]},'
                ' "mutants": [{"id": "M1", "why": "w", "catcher": "TestX",'
                ' "edits": [{"file": "a", "find": "x", "replace": "y"}]}]}'
            )
            with self.assertRaises(mutate.Broken) as cm:
                mutate.load_spec(spec)
            self.assertIn("-run", str(cm.exception))

    def test_red_by_a_different_test_than_the_one_claimed_is_an_escape(self):
        """`rc != 0` is not a catch. The row's claim is about WHICH test holds the property."""
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            suite = FakeSuite([f], {"mutated": "TestSomethingElse", "never": "TestTheClaim"})
            mutants = [
                {
                    "id": "M1",
                    "why": "w",
                    "catcher": "TestTheClaim",
                    "edits": [{"file": str(f), "find": "original", "replace": "mutated"}],
                }
            ]
            self.assertEqual(mutate.sweep(suite, mutants, verbose=False), 1)


class MutationTextNeverCrossesAShellParse(unittest.TestCase):
    """LSN-049. A needle containing `""` closed a `bash -c` string and the sweep scored the 0."""

    def test_a_needle_containing_go_quotes_applies_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", '\treturn false, false, ""\n')
            mutate.apply_edit(
                {"file": str(f), "find": 'return false, false, ""', "replace": 'return true, false, ""'}
            )
            self.assertEqual(f.read_text(), '\treturn true, false, ""\n')

    def test_a_needle_with_backslashes_and_newlines_applies_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", 'a := "c:\\\\tmp"\nb := `$HOME`\n')
            mutate.apply_edit(
                {"file": str(f), "find": 'a := "c:\\\\tmp"\nb := `$HOME`', "replace": "// gone"}
            )
            self.assertEqual(f.read_text(), "// gone\n")

    def test_a_stale_needle_is_BROKEN_and_not_a_survivor(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            suite = FakeSuite([f], {"mutated": "TestX"})
            mutants = [
                {
                    "id": "M1",
                    "why": "w",
                    "catcher": "TestX",
                    "edits": [{"file": str(f), "find": "a needle nobody updated", "replace": "z"}],
                }
            ]
            self.assertEqual(mutate.sweep(suite, mutants, verbose=False), 2)
            self.assertEqual(f.read_text(), "original\n")

    def test_an_ambiguous_needle_is_BROKEN_rather_than_landing_twice(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "dup\ndup\n")
            with self.assertRaises(mutate.Broken) as cm:
                mutate.apply_edit({"file": str(f), "find": "dup", "replace": "z"})
            self.assertIn("occurs 2 times", str(cm.exception))


class VerdictsAndExitCodes(unittest.TestCase):
    def _run(self, reddens, mutants, breaks_build=(), files=None):
        suite = FakeSuite(files, reddens, breaks_build)
        return mutate.sweep(suite, mutants, verbose=False)

    def test_a_caught_mutant_exits_zero_and_the_file_comes_back(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            rc = self._run(
                {"mutated": "TestX"},
                [
                    {
                        "id": "M1",
                        "why": "w",
                        "catcher": "TestX",
                        "edits": [{"file": str(f), "find": "original", "replace": "mutated"}],
                    }
                ],
                files=[f],
            )
            self.assertEqual(rc, 0)
            self.assertEqual(f.read_text(), "original\n")

    def test_a_green_suite_under_mutation_is_an_escape(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            rc = self._run(
                {"nothing-here": "TestX"},
                [
                    {
                        "id": "M1",
                        "why": "w",
                        "catcher": "TestX",
                        "edits": [{"file": str(f), "find": "original", "replace": "mutated"}],
                    }
                ],
                files=[f],
            )
            self.assertEqual(rc, 1)

    def test_a_mutant_that_does_not_compile_is_BROKEN_not_an_escape(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            rc = self._run(
                {"never": "TestX"},
                [
                    {
                        "id": "M1",
                        "why": "w",
                        "catcher": "TestX",
                        "edits": [{"file": str(f), "find": "original", "replace": "syntax-error"}],
                    }
                ],
                breaks_build=("syntax-error",),
                files=[f],
            )
            self.assertEqual(rc, 2)

    def test_a_red_baseline_measures_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(pathlib.Path(td), "x.go", "original\n")
            suite = FakeSuite([f], {"original": "TestX"})
            with self.assertRaises(mutate.Broken) as cm:
                mutate.sweep(
                    suite,
                    [
                        {
                            "id": "M1",
                            "why": "w",
                            "catcher": "TestX",
                            "edits": [{"file": str(f), "find": "original", "replace": "mutated"}],
                        }
                    ],
                    verbose=False,
                )
            self.assertIn("baseline is RED", str(cm.exception))


class SpecShape(unittest.TestCase):
    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as td:
            spec = pathlib.Path(td) / "s.json"
            spec.write_text(body)
            return mutate.load_spec(spec)

    def test_a_bare_list_of_mutants_is_refused(self):
        with self.assertRaises(mutate.Broken) as cm:
            self._load('[{"id": "M1"}]')
        self.assertIn("LSN-047", str(cm.exception))

    def test_a_mutant_missing_its_catcher_is_refused(self):
        with self.assertRaises(mutate.Broken) as cm:
            self._load(
                '{"suite": {"kind": "go", "dir": ".", "packages": ["./p"]},'
                ' "mutants": [{"id": "M1", "why": "w",'
                ' "edits": [{"file": "a", "find": "x", "replace": "y"}]}]}'
            )
        self.assertIn("catcher", str(cm.exception))

    def test_a_mutant_missing_its_why_is_refused(self):
        """A row nobody can read is a row nobody re-derives when it starts escaping."""
        with self.assertRaises(mutate.Broken) as cm:
            self._load(
                '{"suite": {"kind": "go", "dir": ".", "packages": ["./p"]},'
                ' "mutants": [{"id": "M1", "catcher": "TestX",'
                ' "edits": [{"file": "a", "find": "x", "replace": "y"}]}]}'
            )
        self.assertIn("why", str(cm.exception))

    def test_an_edit_that_changes_nothing_is_refused(self):
        with self.assertRaises(mutate.Broken) as cm:
            self._load(
                '{"suite": {"kind": "go", "dir": ".", "packages": ["./p"]},'
                ' "mutants": [{"id": "M1", "why": "w", "catcher": "TestX",'
                ' "edits": [{"file": "a", "find": "x", "replace": "x"}]}]}'
            )
        self.assertIn("nothing is mutated", str(cm.exception))

    def test_duplicate_ids_are_refused(self):
        row = '{"id": "M1", "why": "w", "catcher": "TestX", "edits": [{"file": "a", "find": "x", "replace": "y"}]}'
        with self.assertRaises(mutate.Broken) as cm:
            self._load(
                '{"suite": {"kind": "go", "dir": ".", "packages": ["./p"]},'
                f' "mutants": [{row}, {row}]}}'
            )
        self.assertIn("duplicate", str(cm.exception))

    def test_an_empty_mutant_list_is_refused(self):
        with self.assertRaises(mutate.Broken) as cm:
            self._load('{"suite": {"kind": "go", "dir": ".", "packages": ["./p"]}, "mutants": []}')
        self.assertIn("0/0", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
