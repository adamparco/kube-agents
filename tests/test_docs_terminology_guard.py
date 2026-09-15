"""What the cron-prompt scan decides to check, and what it leaves alone.

`hack/check-docs-terminology.sh` verifies that every cron prompt quoted in the
documentation is a verbatim copy of a prompt in the roster. Deciding *which*
`"prompt"` keys are claiming to be quotations is the whole difficulty, and it
has failed in both directions: too wide and an unrelated pull request that
renders a LiteLLM request body goes red with an error about cron jobs; too
narrow and a drifted quotation sits unchecked while the guard reports PASS.

Neither direction fails visibly in this repository. The tree happens to contain
three quotations and no near-misses, so the guard prints "Terminology check
passed" whichever way the classifier is wrong. The cases below are the
near-misses the tree does not have.

`hack/scan-cron-prompts.awk` is driven directly rather than through the shell
script, because the script reads the repository it ships in — `git ls-files`,
the real rosters, `audit_report.py` — and none of that can be fixtured. The one
test that does run the script runs it against that real repository, and asks
only whether it started.

Run:
  python3 -m unittest discover -s tests -p 'test_docs_terminology_guard.py' -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_AWK = REPO_ROOT / "hack" / "scan-cron-prompts.awk"
GUARD = REPO_ROOT / "hack" / "check-docs-terminology.sh"

# One real roster id, so the fixtures read like the documents they stand in for.
KNOWN_ID = "fleet-consistency-drift"


def scan(document: str, awk: str = "awk") -> list[tuple[str, int]]:
    """Run the scan over one document; return its (verdict, line) decisions."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ids = root / "ids.txt"
        ids.write_text(KNOWN_ID + "\n", encoding="utf-8")
        doc = root / "doc.md"
        doc.write_text(textwrap.dedent(document).lstrip("\n"), encoding="utf-8")
        proc = subprocess.run(
            [awk, "-v", f"idfile={ids}", "-f", str(SCAN_AWK), str(doc)],
            capture_output=True,
            text=True,
            check=True,
        )
    out = []
    for line in proc.stdout.splitlines():
        kind, _path, lineno, _text = line.split(":", 3)
        out.append((kind, int(lineno)))
    return out


class ScanScopeTest(unittest.TestCase):
    """Which `"prompt"` keys the scan claims, and which it declines."""

    def test_prose_naming_json_keys_is_not_a_roster_entry(self):
        # `concepts/governance-sops.md` has no fence anywhere and writes
        # `"skills": [...]` in a sentence. Treating a fence-free document as one
        # block made every such page one malformed roster entry, so any prose
        # sentence added to it that spelled `"prompt": "` failed CI for the
        # whole repository.
        self.assertEqual(
            scan(
                """
                # Governance SOPs

                Each SOP is reachable from an entry whose "skills": ["fleet-audit"]
                line names the skill that loads it.

                A roster entry's "prompt": "text the operator wrote" field carries
                the instruction the agent wakes up to.
                """
            ),
            [],
        )

    def test_prose_after_a_closed_fence_is_prose_again(self):
        hits = scan(
            f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```

            Prose mentioning "prompt": "something made up" is not a quotation.
            """
        )
        self.assertEqual(hits, [("R", 2)])

    def test_an_unrelated_request_body_is_left_alone(self):
        # A LiteLLM or Vertex payload has a `"prompt"` too. No roster id in the
        # document, so nothing here is claiming to quote the roster.
        self.assertEqual(
            scan(
                """
                ```json
                { "model": "gpt-4", "prompt": "Summarise the ticket." }
                ```
                """
            ),
            [],
        )

    def test_a_request_body_beside_a_roster_entry_is_still_left_alone(self):
        # The document-level fallback must not reach a block that carries keys
        # of its own -- otherwise a page documenting both a cron job and an LLM
        # call fails on the LLM call.
        hits = scan(
            f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```

            ```json
            {{ "model": "gpt-4", "prompt": "Summarise the ticket." }}
            ```
            """
        )
        self.assertEqual(hits, [("R", 2)])

    def test_a_quotation_trimmed_to_the_prompt_alone_is_still_graded(self):
        # No id, no sibling key, nothing for either structural rule to catch.
        # Deciding purely per block left this graded by nothing at all, which is
        # the silent pass the guard exists to prevent.
        hits = scan(
            f"""
            ```json
            {{
              "id": "{KNOWN_ID}",
              "schedule": "20 8 * * 1",
              "prompt": "Compare every cluster."
            }}
            ```

            Quoted again on its own, drifted:

            ```json
            "prompt": "Compare every cluster, twice."
            ```
            """
        )
        self.assertEqual(hits, [("R", 5), ("R", 12)])


class BlockquotedFenceTest(unittest.TestCase):
    """A fence inside a blockquote opens and closes a block like any other."""

    def test_two_blockquoted_manifests_are_two_blocks(self):
        # The fence regex used to reject `> ```json`, so these merged into one
        # block and the id in the first graded the prompt in the second.
        hits = scan(
            f"""
            > ```json
            > {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            > ```

            > ```json
            > {{ "schedule": "0 8 * * *", "prompt": "Compare every cluster." }}
            > ```
            """
        )
        self.assertEqual(hits, [("R", 2), ("O", 6)])


class OrphanEntryTest(unittest.TestCase):
    """A rendered entry naming no known job is reported, never skipped."""

    def test_an_entry_with_no_id_is_reported(self):
        hits = scan(
            """
            ```json
            {
              "schedule": "0 8 * * *",
              "prompt": "Compare every cluster."
            }
            ```
            """
        )
        self.assertEqual(hits, [("O", 4)])

    def test_an_entry_whose_id_the_roster_does_not_know_is_reported(self):
        # A renamed or mistyped job. The error message already offers "correct
        # it if the job was renamed"; before this it was silently unchecked,
        # because no sibling key survived the trim to mark the block cron-shaped.
        hits = scan(
            """
            ```json
            {
              "id": "fleet-consistency-drft",
              "prompt": "Compare every cluster."
            }
            ```
            """
        )
        self.assertEqual(hits, [("O", 4)])


class AwkPortabilityTest(unittest.TestCase):
    """CI runs Ubuntu, where /usr/bin/awk is mawk, not the BSD awk on a Mac."""

    def test_every_awk_on_this_machine_agrees(self):
        document = f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "a", "schedule": "0 8 * * *" }}
            ```

            ```json
            "prompt": "b"
            ```

            > ```json
            > {{ "id": "nope", "prompt": "c" }}
            > ```
            """
        available = [a for a in ("awk", "mawk", "gawk") if shutil.which(a)]
        self.assertIn("awk", available)
        results = {a: scan(document, awk=a) for a in available}
        self.assertEqual(
            len(set(map(tuple, results.values()))),
            1,
            f"awk implementations disagree: {results}",
        )


class GuardWiringTest(unittest.TestCase):
    """The shell script and the awk program have to stay attached."""

    def test_the_guard_invokes_the_scan_file(self):
        source = GUARD.read_text(encoding="utf-8")
        self.assertIn("scan-cron-prompts.awk", source)
        self.assertRegex(source, r'-f "\$SCAN_AWK"')

    def test_unrunnable_checks_are_reported_before_the_scan_can_exit(self):
        # The scan exits 1 outright on an unreadable roster or a failed awk. The
        # "checks that could not run" report has to be printed before that
        # point, or an early exit takes it with it and the run blames the roster
        # for a failure a broken grep pattern had already caused.
        source = GUARD.read_text(encoding="utf-8")
        report = source.index("A terminology check could not run")
        scan_call = source.index('-f "$SCAN_AWK"')
        self.assertLess(report, scan_call)

    def test_the_guard_runs_from_a_directory_that_is_not_the_repository_root(self):
        # Every other test in this class reads the source; this one executes it,
        # because the defect it covers is invisible to a source read. The script
        # cd's to the repository root on line 16 and then resolved the awk
        # program against `$0`, which is still relative to the *caller's*
        # directory -- so `cd hack && ./check-docs-terminology.sh` exited 1 on
        # "not found" before checking a single prompt, while CI, which runs it
        # from the root, stayed green.
        # Invoked by a *relative* path, which is how a person types it and the
        # only spelling that reproduces the break: `dirname` on an absolute
        # `$0` lands on the right directory from any cwd, so a test that runs
        # the script by its full path passes either way.
        for cwd in (REPO_ROOT / "hack", REPO_ROOT / "docs"):
            with self.subTest(cwd=cwd.name):
                proc = subprocess.run(
                    ["./" + os.path.relpath(GUARD, cwd)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn("cannot run", proc.stdout + proc.stderr)
                self.assertEqual(
                    proc.returncode, 0, (proc.stdout + proc.stderr)[-2000:]
                )

    def test_the_scan_refuses_to_grade_a_document_without_a_roster(self):
        # Without `-v idfile`, BSD awk and gawk abort, but mawk reads the empty
        # string as a missing file and calls every rendered entry an orphan --
        # a page of failures that look like findings. Both are wrong answers to
        # "the caller forgot the roster", so the program says so itself.
        document = f'```json\n{{ "id": "{KNOWN_ID}", "prompt": "a" }}\n```\n'
        for awk in [a for a in ("awk", "mawk", "gawk") if shutil.which(a)]:
            with self.subTest(awk=awk):
                proc = subprocess.run(
                    [awk, "-f", str(SCAN_AWK)],
                    input=textwrap.dedent(document),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("idfile", proc.stderr)

    def test_no_floor_fires_on_a_search_that_did_not_run(self):
        # A "no document states this any more" error is a wrong diagnosis when
        # the search itself failed: `search` returns 2 and prints nothing, and
        # every floor below then announces its own cap is undocumented. Both
        # floors gate on the search having run.
        source = GUARD.read_text(encoding="utf-8")
        for guarded in ('if [ "$probe_ok" -eq 0 ]', 'if [ "$ID_SEARCH_OK" -eq 0 ]'):
            self.assertIn(guarded, source)
        floors = re.findall(r"^.*-lt 1 \]; then$", source, re.MULTILINE)
        self.assertEqual(len(floors), 2, f"unguarded floor added? {floors}")


if __name__ == "__main__":
    unittest.main()
