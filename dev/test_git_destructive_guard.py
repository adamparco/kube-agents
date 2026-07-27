#!/usr/bin/env python3
"""Tests for the LSN-030 guard.

The first test is the literal command that caused the loss. If this file is ever refactored,
that case is the one that must survive: it is the incident, not an example of it.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_GUARD = _HERE / "git-destructive-guard.py"

# The module's filename has a dash, so it is loaded by path rather than imported by name.
_spec = importlib.util.spec_from_file_location("git_destructive_guard", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class TempRepo:
    """A throwaway repo. Real git, because the guard's whole job is to read real `git status`."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = self._tmp.name
        _git("init", "-q", "-b", "main", cwd=self.path)
        _git("config", "user.email", "t@example.com", cwd=self.path)
        _git("config", "user.name", "T", cwd=self.path)
        (pathlib.Path(self.path) / "tracked.txt").write_text("committed\n")
        _git("add", "tracked.txt", cwd=self.path)
        _git("commit", "-qm", "init", cwd=self.path)
        return self

    def dirty(self):
        (pathlib.Path(self.path) / "tracked.txt").write_text("edited, never staged\n")

    def __exit__(self, *exc):
        self._tmp.cleanup()


class TestTheIncident(unittest.TestCase):
    """LSN-030 itself."""

    # Reproduced verbatim from the session that lost the work.
    INCIDENT = (
        "git status --porcelain; git checkout main; "
        "git fetch origin -q && git reset --hard origin/main -q"
    )

    def test_incident_command_is_refused_on_a_dirty_tree(self):
        with TempRepo() as r:
            r.dirty()
            msg = guard.verdict(self.INCIDENT, r.path)
            self.assertIsNotNone(msg, "the exact command that cost seven files must be refused")
            self.assertIn("LSN-030", msg)
            self.assertIn("reset --hard", msg)
            self.assertIn("tracked.txt", msg, "the refusal must name what would be lost")

    def test_incident_command_is_allowed_on_a_clean_tree(self):
        with TempRepo() as r:
            self.assertIsNone(
                guard.verdict(self.INCIDENT, r.path),
                "a reset with nothing to lose is the common case and must stay unblocked",
            )


class TestDestructiveVerbs(unittest.TestCase):
    """Each verb that overwrites rather than aborting."""

    def _refused(self, cmd):
        with TempRepo() as r:
            r.dirty()
            return guard.verdict(cmd, r.path)

    def test_reset_hard(self):
        self.assertIsNotNone(self._refused("git reset --hard HEAD~1"))

    def test_reset_hard_with_global_flag(self):
        self.assertIsNotNone(self._refused("git -C /some/dir reset --hard origin/main"))

    def test_reset_soft_and_mixed_are_allowed(self):
        # Neither touches the working tree.
        self.assertIsNone(self._refused("git reset --soft HEAD~1"))
        self.assertIsNone(self._refused("git reset HEAD~1"))

    def test_checkout_path_form(self):
        self.assertIsNotNone(self._refused("git checkout -- docs/build/LEDGER.md"))
        self.assertIsNotNone(self._refused("git checkout origin/main -- dev/L0-CHAIN.txt"))

    def test_checkout_existing_file_without_double_dash(self):
        # `git checkout dev` restores the directory if it exists; the missing `--` does not help.
        self.assertIsNotNone(self._refused("git checkout dev"))

    def test_checkout_branch_form_is_allowed(self):
        # git aborts by itself rather than overwriting, so blocking here would be noise.
        self.assertIsNone(self._refused("git checkout some-branch-name"))
        self.assertIsNone(self._refused("git checkout -b new-branch"))
        self.assertIsNone(self._refused("git switch main"))

    def test_restore(self):
        self.assertIsNotNone(self._refused("git restore dev/L0-CHAIN.txt"))
        self.assertIsNone(self._refused("git restore"), "no path named, nothing identified")

    def test_clean_force(self):
        self.assertIsNotNone(self._refused("git clean -fd"))
        self.assertIsNotNone(self._refused("git clean --force"))
        self.assertIsNone(self._refused("git clean -n"), "dry run deletes nothing")

    def test_stash_drop_and_clear(self):
        self.assertIsNotNone(self._refused("git stash drop"))
        self.assertIsNotNone(self._refused("git stash clear"))
        self.assertIsNone(self._refused("git stash push -u -m snap"), "stashing is the remedy")

    def test_aborting_verbs_are_allowed(self):
        for cmd in ("git merge origin/main", "git rebase origin/main", "git pull --ff-only"):
            self.assertIsNone(self._refused(cmd), cmd)

    def test_non_git_commands_are_allowed(self):
        for cmd in ("rm -rf /tmp/scratch", "kubectl delete pod x", "echo git reset --hard"):
            self.assertIsNone(self._refused(cmd), cmd)


class TestCompoundLines(unittest.TestCase):
    """The verb is rarely alone on the line — that is how it got through."""

    def _refused(self, cmd):
        with TempRepo() as r:
            r.dirty()
            return guard.verdict(cmd, r.path)

    def test_each_separator_is_scanned(self):
        for sep in (";", "&&", "||", "\n"):
            self.assertIsNotNone(
                self._refused(f"make -C k8s-operator build {sep} git reset --hard origin/main"),
                f"separator {sep!r}",
            )

    def test_split_commands_keeps_every_segment(self):
        self.assertEqual(
            guard.split_commands("a && b; c || d | e"), ["a", "b", "c", "d", "e"]
        )


class TestHeredocBodiesAreData(unittest.TestCase):
    """The first command this guard ever blocked was the heredoc writing LSN-030 itself."""

    def _refused(self, cmd):
        with TempRepo() as r:
            r.dirty()
            return guard.verdict(cmd, r.path)

    # Reduced from the real one: prose that quotes the incident command inside a heredoc body.
    DOC_WRITE = (
        "cat >> LESSONS.md <<'EOF'\n"
        "The next command ended `git fetch origin -q && git reset --hard origin/main -q`,\n"
        "issued while still on the T9 branch. Seven files were destroyed.\n"
        "EOF\n"
        "python3 -m unittest dev.test_git_destructive_guard"
    )

    def test_prose_quoting_the_incident_is_allowed(self):
        self.assertIsNone(self._refused(self.DOC_WRITE), "must be able to document the incident")

    def test_a_real_command_after_the_heredoc_is_still_seen(self):
        self.assertIsNotNone(
            self._refused(self.DOC_WRITE + "; git reset --hard origin/main"),
            "skipping the body must not skip what follows the terminator",
        )

    def test_unquoted_and_dash_forms(self):
        for header in ("cat > f <<EOF", "cat > f <<-EOF", 'cat > f <<"EOF"'):
            self.assertIsNone(
                self._refused(f"{header}\ngit reset --hard\nEOF"), header
            )

    def test_two_heredocs_on_one_line_close_in_order(self):
        cmd = "diff <(cat <<A\ngit reset --hard\nA\n) <(cat <<B\ngit clean -fd\nB\n)"
        self.assertIsNone(self._refused(cmd))

    def test_unterminated_heredoc_does_not_swallow_a_later_command(self):
        # No terminator: everything after the header is body, which is what bash would do too.
        self.assertIsNone(self._refused("cat <<EOF\ngit reset --hard\n"))


class TestHookProtocol(unittest.TestCase):
    """Exit codes and stdin shapes, exercised through the real process."""

    def _run(self, payload, cwd, args=()):
        return subprocess.run(
            [sys.executable, str(_GUARD), *args],
            input=payload, cwd=cwd, capture_output=True, text=True, timeout=60,
        )

    def test_blocks_with_exit_2_and_reason_on_stderr(self):
        with TempRepo() as r:
            r.dirty()
            payload = json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard origin/main"},
                "cwd": r.path,
            })
            out = self._run(payload, cwd=r.path)
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn("LSN-030", out.stderr)

    def test_allows_with_exit_0(self):
        with TempRepo() as r:
            payload = json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard origin/main"},
                "cwd": r.path,
            })
            self.assertEqual(self._run(payload, cwd=r.path).returncode, 0)

    def test_non_bash_tool_is_ignored(self):
        with TempRepo() as r:
            r.dirty()
            payload = json.dumps({
                "tool_name": "Edit",
                "tool_input": {"command": "git reset --hard"},
                "cwd": r.path,
            })
            self.assertEqual(self._run(payload, cwd=r.path).returncode, 0)

    def test_unparseable_input_never_blocks_the_session(self):
        with TempRepo() as r:
            r.dirty()
            self.assertEqual(self._run("not json at all", cwd=r.path).returncode, 0)

    def test_command_stdin_mode(self):
        with TempRepo() as r:
            r.dirty()
            out = self._run("git reset --hard origin/main", cwd=r.path, args=("--command-stdin",))
            self.assertEqual(out.returncode, 2, out.stderr)


class TestWiring(unittest.TestCase):
    """A guard nothing invokes is a comment. Assert it is actually registered."""

    def test_registered_as_a_pretooluse_bash_hook(self):
        settings = _HERE.parent / ".claude" / "settings.json"
        self.assertTrue(settings.is_file(), f"{settings} must exist for the guard to run")
        cfg = json.loads(settings.read_text())
        entries = cfg.get("hooks", {}).get("PreToolUse", [])
        wired = [
            h
            for e in entries
            if "Bash" in (e.get("matcher") or "")
            for h in e.get("hooks", [])
            if "git-destructive-guard.py" in (h.get("command") or "")
        ]
        self.assertTrue(wired, "git-destructive-guard.py must be a PreToolUse hook on Bash")

    def test_guard_is_executable(self):
        self.assertTrue(os.access(_GUARD, os.X_OK), f"{_GUARD} must be executable")


if __name__ == "__main__":
    unittest.main()
