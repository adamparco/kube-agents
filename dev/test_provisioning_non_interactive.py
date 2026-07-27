#!/usr/bin/env python3
"""Every provisioning prompt is reachable only when an operator can answer it (L0).

`make live-refresh ARGS="--yes"` and `provision.sh --no-confirm` both promise an unattended run.
On 2026-07-26 they did not deliver one: `--no-confirm` set NO_CONFIRM, and NO_CONFIRM was read by
exactly one function (`confirm_action`). Every *configuration* prompt -- the Slack token questions,
the API-key questions, the project-number fallback -- tested `DRY_RUN` and `CI` and not NO_CONFIRM,
so they stayed live during a run that had already been declared non-interactive.

The failure is not a hang. Under `set -e` a `read` at EOF returns non-zero and the script exits
mid-pipeline: the live refresh aborted at step 06 of 13 with the operator already rolled onto the
new build and the three agent tiers still on the old one. A half-refreshed live install, caused by
the flag whose only job was to make the run safe to leave alone.

This asserts the property structurally rather than by running the scripts, which need gcloud, a
cluster and real credentials: every `read` that consumes stdin must be lexically inside a branch
guarded by `is_non_interactive` (or the `DRY_RUN`/`CI` spellings that predate it). A new prompt
added outside such a branch fails here, on a PR, instead of at step 06 of a live refresh.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "k8s-operator/scripts"

# The provisioner and everything it sources. teardown_* is out of scope: it is destructive by
# definition and its confirmations are the point.
FILES = sorted(
    [SCRIPTS / "common.sh"]
    + [p for p in SCRIPTS.glob("provision_*.sh")]
    + [SCRIPTS / "live_refresh.sh"]
)

# `read` forms that do NOT touch stdin: a here-string splits a value already in hand, and a
# `while ... read` loop is fed by a redirect or a pipe from the command that produced it.
NON_STDIN = re.compile(r"<<<|^\s*(while|for|until)\b|done\s*<")

# `read` as a COMMAND: at the start of a statement, followed by a flag, a variable, or nothing.
# Not `read-only|*)`, a case pattern in provision_04's role selector -- `read\b` matches that,
# because `\b` sits happily between "read" and "-".
READ = re.compile(r"(?:^|;|\bthen\b|\bdo\b|\|\||&&)\s*read(?:\s+-\w+)*(?:\s+\w+)*\s*(?:$|;|<|#)")

# Any spelling of "there is nobody at the keyboard".
GUARD = re.compile(r"is_non_interactive|is_ci_pipeline|DRY_RUN")


FUNC_START = re.compile(r"^(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")
BAILS_OUT = re.compile(r"^\s*(return|exit)\b")


def guarded_read_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with a stdin-consuming `read` that no guard covers.

    Two guard shapes are recognised, because both are in use and both are correct:

    * ENCLOSING -- the read sits inside an `if is_non_interactive; then ... else <read> fi` chain.
    * EARLY EXIT -- the function opens with `if is_non_interactive; then ...; return/exit; fi`, so
      everything after it is unreachable without a keyboard. This is the better shape for a
      function that is nothing but prompts (`loop_add_tokens`, `init_var_required`): one guard at
      the top rather than one per read.

    Bash is not parseable with a regex in general. It is parseable enough for this, because these
    guards are written in one consistent shape across the codebase -- and `test_the_scanner_
    actually_finds_reads` fails if that stops being true and the pattern quietly matches nothing.
    """
    unguarded: list[tuple[int, str]] = []
    if_stack: list[bool] = []  # per open `if` chain: does its condition test a guard?
    chain_bails: list[bool] = []  # ...and does that guarded arm return/exit?
    func_sealed = False  # an early-exit guard has fired in the current function

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = "" if raw.lstrip().startswith("#") else raw.split("#", 1)[0]
        stripped = line.strip()
        if not stripped:
            continue

        if FUNC_START.match(stripped):
            func_sealed = False
        elif raw.startswith("}"):
            # End of a function body (closing brace in column 0). Without this the seal LEAKED:
            # one early-exit guard anywhere in the file marked every later read as protected,
            # including reads at top level after the last function. Caught by appending an
            # unguarded `read` to provision_06 and watching this suite stay green.
            func_sealed = False

        if re.match(r"^if\b", stripped):
            if_stack.append(bool(GUARD.search(stripped)))
            chain_bails.append(False)
        elif re.match(r"^elif\b", stripped) and if_stack:
            if_stack[-1] = if_stack[-1] or bool(GUARD.search(stripped))
        elif re.match(r"^fi\b", stripped) and if_stack:
            was_guarded = if_stack.pop()
            bailed = chain_bails.pop()
            if was_guarded and bailed:
                func_sealed = True
        elif BAILS_OUT.match(stripped) and chain_bails:
            chain_bails[-1] = True

        if READ.search(stripped) and not NON_STDIN.search(stripped):
            if not any(if_stack) and not func_sealed:
                unguarded.append((lineno, stripped))

    return unguarded


class ProvisioningIsNonInteractive(unittest.TestCase):
    def test_every_prompt_is_behind_a_non_interactive_guard(self):
        offenders: list[str] = []
        for path in FILES:
            for lineno, text in guarded_read_lines(path):
                rel = path.relative_to(REPO)
                offenders.append(f"  {rel}:{lineno}: {text}")

        self.assertEqual(
            [],
            offenders,
            "\nThese `read` calls consume stdin with no is_non_interactive guard above them:\n"
            + "\n".join(offenders)
            + "\n\nUnder `--no-confirm`/`--yes`/CI there is no operator to answer, and `set -e`"
            "\nturns the resulting EOF into an abort partway through the pipeline. Wrap the prompt"
            "\nin `if is_non_interactive; then <keep the existing value, or fail loudly>; else ...`.",
        )

    def test_the_shared_predicate_covers_all_three_conditions(self):
        text = (SCRIPTS / "common.sh").read_text()
        m = re.search(r"^is_non_interactive\(\)\s*\{(.*?)^\}", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(m, "common.sh no longer defines is_non_interactive()")
        body = m.group(1)
        for condition in ("NO_CONFIRM", "DRY_RUN", "is_ci_pipeline"):
            self.assertIn(
                condition,
                body,
                f"is_non_interactive() no longer tests {condition}; a run that declares itself"
                " unattended would prompt again",
            )

    def test_provision_sh_forwards_the_non_interactive_flag_to_every_step(self):
        """A guard nothing can reach is not a guard.

        provision.sh parses --no-confirm into NO_CONFIRM for itself, and each of the 13 step
        scripts re-parses its own argv and resets NO_CONFIRM to 0 first. So the flag only crosses
        the process boundary if it is forwarded on the command line. It was not: only --dry-run
        was, and `--no-confirm` therefore suppressed exactly one confirmation -- provision.sh's own
        -- while every child ran fully interactive. Every prompt guard added downstream was dead
        code until this was fixed.
        """
        text = (SCRIPTS / "provision.sh").read_text()

        self.assertRegex(
            text,
            r"STEP_ARGS\+=\(\s*\"--no-confirm\"\s*\)",
            "provision.sh no longer adds --no-confirm to the args it forwards to the step scripts",
        )

        steps = re.findall(r'^"\$\{SCRIPT_DIR\}/(provision_\d+_\w+\.sh)"(.*)$', text, re.MULTILINE)
        self.assertGreaterEqual(
            len(steps), 13, f"parsed only {len(steps)} step invocations out of provision.sh"
        )
        for name, tail in steps:
            self.assertIn(
                '"${STEP_ARGS[@]}"',
                tail,
                f"{name} is invoked without the forwarded flags, so a --no-confirm run would"
                " still prompt there",
            )

    def test_the_scanner_actually_finds_reads(self):
        # Two empty lists compare equal. If the `read` pattern stops matching -- a refactor, a
        # rename, a file moved -- this suite would pass against a pipeline of nothing but prompts.
        total = 0
        for path in FILES:
            for raw in path.read_text().splitlines():
                stripped = raw.strip()
                if stripped.startswith("#"):
                    continue
                if READ.search(stripped) and not NON_STDIN.search(stripped):
                    total += 1
        self.assertGreaterEqual(
            total,
            8,
            f"the scanner found only {total} stdin reads across {len(FILES)} scripts; the pattern"
            " has stopped matching and this suite no longer asserts anything",
        )


if __name__ == "__main__":
    unittest.main()
