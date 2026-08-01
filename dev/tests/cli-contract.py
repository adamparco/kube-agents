#!/usr/bin/env python3
"""CLI contract check — every flag a caller passes is a flag the CLI accepts.

Written 2026-07-25 in response to an escape, not to a hypothesis.

P8-T4 removed `--github-cidrs` from `render_developer_team.py` for a good reason:
GitHub's four published IPv4 blocks are the same for every tenant, so they belong
in the egress template rather than in a per-tenant flag that is a per-tenant way to
get them wrong. The unit added a test that the renderer's substitution tokens and
the assets' tokens are the same set — the check that would have caught a *dead*
flag — and shipped green.

`dev/verify/verify-phase3.sh` was still passing `--github-cidrs`. From that
merge until this file was written, P3-K7 ("cascade render -> VAP dry-run") did not
verify anything: argparse exited 2, the invocation's stderr went to /dev/null, the
bundle was never written, and the check reported "render produced no identity
file". It failed loudly — but only when somebody ran an L2 suite by hand, and the
message named the symptom (no file) rather than the cause (the caller is holding a
flag that no longer exists). No L0 check looked at the *callers* of a CLI at all.

So the property is the one nobody was asserting:

    for every invocation of a repo CLI anywhere in the tree,
    every `--flag` in that invocation is a flag the CLI's parser defines.

It is deliberately a whole-tree sweep and not a list of known call sites. A check
that knows about verify-phase3.sh would have to be edited to notice the next
caller, and an edit is something I could quietly skip (LSN-007's shape).

What it reads
-------------
* **CLIs**: every tracked `.py` that constructs an `ArgumentParser`. The accepted
  flag set comes from running `python3 <cli> --help`, which is the parser itself
  answering. Four CLIs cannot be imported in a bare interpreter (`yaml`,
  `github_token_refresh`); for those it falls back to reading `add_argument(...)`
  string literals out of the source with `ast`, and says so in the output. A CLI
  whose flags are built from non-literal expressions is reported as unanalyzable
  rather than skipped silently.
* **Callers**: tracked `.sh`, `.md`, `.yml`, `.yaml`, `.py`, `.txt` and Dockerfiles.
  Line continuations are folded first, so a nine-line backslash-continued invocation
  is one logical command. Shell comment lines are skipped. Each invocation is cut at
  the first `|`, `;`, `&&`, `>` or backtick so a following command's flags are not
  attributed to this one.

Deliberately NOT read: `docs/build/**`. Those are the harness's own build records —
a ledger row describing a 2026-07-19 invocation that used a flag removed on
2026-07-24 is an accurate historical record, and rewriting history to satisfy a
lint is the opposite of what the ledger is for. The exclusion is printed on every
run; it is not a silent cap.

Deliberately NOT checked: required flags a caller omits, or flag *values*. argparse
already fails loudly on a missing required flag, and values need a semantics this
check does not have. Nor is the flag matched to the *subcommand* it was written
under — a CLI's subparser flags are unioned, so `resolver.py claim --report x` (a
`transition` flag) passes here and argparse rejects it at runtime. The property is
the cheapest one that would have caught the escape, and it was false.

Exit 0 = every invocation's flags exist. Exit 1 = at least one does not.
Exit 2 = the check could not run (no `git ls-files`, no CLIs found). Never conflate
2 with 0: a sweep that found nothing to sweep is not a pass (V-MET-014).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitcorpus import repo_files  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()

# Directories whose text is a historical record, not an instruction to a machine.
EXCLUDED_DIRS = ("docs/build/",)

CALLER_SUFFIXES = (".sh", ".md", ".yml", ".yaml", ".py", ".txt")
CALLER_NAMES = ("Dockerfile",)

# A long option as argparse spells it: two dashes, then a letter/digit, then word chars or dashes.
# The lookbehind keeps `--foo` out of `----foo` and out of `x--foo`.
FLAG = re.compile(r"(?<![\w-])--([A-Za-z0-9][A-Za-z0-9-]*)")

# Where an invocation stops. `>` is here for redirections; a flag after one belongs to nothing.
TERMINATORS = ("|", ";", "&&", "||", ">", "`", "\n")

HELP_TIMEOUT = 30

# Non-vacuity floor on the SUBJECT. 18 distinct CLI basenames on 2026-08-01 (27 files; the
# per-tier copies of `submit_suggestion.py` and friends union under one basename). Set at two
# thirds so ordinary churn does not trip it and a collapse does.
MIN_CLIS = 12

# argparse prints a subparser's choices as `{poll,claim,transition}` in the positional section.
# Their flags are not in the top-level help, and a caller writes `resolver.py claim --issue 7`.
SUBCOMMANDS = re.compile(r"\{([a-z0-9][a-z0-9_,-]*)\}")


# --------------------------------------------------------------------------------------------
# what a CLI accepts
# --------------------------------------------------------------------------------------------
def _help_text(cli: Path, argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), *argv, "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _flags_from_help(cli: Path) -> set[str] | None:
    """Ask the parser, including each subparser. None if it will not run in a bare interpreter."""
    top = _help_text(cli, [])
    if top is None:
        return None
    flags = {m.group(1) for m in FLAG.finditer(top)}
    # One level of subcommands. Nested subparsers exist in argparse but not in this tree; a caller
    # of one would show up as an unknown flag rather than as a silent pass.
    subs: set[str] = set()
    for m in SUBCOMMANDS.finditer(top):
        subs |= {s for s in m.group(1).split(",") if s}
    for sub in sorted(subs):
        sub_help = _help_text(cli, [sub])
        if sub_help is not None:
            flags |= {m.group(1) for m in FLAG.finditer(sub_help)}
    return flags


def _flags_from_source(cli: Path) -> tuple[set[str], list[str]]:
    """Read `add_argument("--x", ...)` literals. Second element: reasons it may be incomplete."""
    flags: set[str] = {"help"}
    notes: list[str] = []
    try:
        tree = ast.parse(cli.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return flags, [f"unparsable: {exc}"]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        if not node.args:
            notes.append("an add_argument() call takes no positional name")
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("--"):
                    flags.add(arg.value[2:])
            else:
                notes.append("an add_argument() name is a non-literal expression")
    return flags, notes


def builds_a_parser(text: str) -> tuple[bool, str | None]:
    """Does this module construct an `ArgumentParser`? Answered by AST, never by substring.

    B-011's property, applied to the corpus gate rather than to a control mode: a string that
    mentions a construct is not the construct. `dev/tests/negative-controls-name-their-rule.py`
    carries a set of synthetic Python fixtures as string literals -- among them one whose whole
    point is to be an argparse dispatch -- and a `"ArgumentParser" in text` test therefore admitted
    it as a CLI. It is not one: it accepts `--negative-control` through a plain `sys.argv` check and
    has no `--help`, so `_flags_from_help` ran it, got its ordinary PASS output back with rc 0, read
    zero flags out of it, and reported `dev/L0-CHAIN.txt:283` as passing a flag the CLI does not
    accept. A false finding against a line that works.

    A `Call` inside a string constant is not in the tree at all, so this is the whole fix. The
    second element is a reason string when the answer had to fall back to the substring test, which
    happens only for a file that will not parse -- excluding it silently would shrink the corpus
    invisibly, which is the failure [[LSN-038]] names.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return "ArgumentParser" in text, f"unparsable, fell back to a substring test: {exc}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "ArgumentParser":
            return True, None
    return False, None


def discover_clis(repo: Path, tracked: list[str]) -> dict[str, dict]:
    """basename -> {path, flags, mode, notes}. Basename, because that is how callers name them."""
    clis: dict[str, dict] = {}
    for rel in tracked:
        if not rel.endswith(".py"):
            continue
        path = repo / rel
        # This checker is itself an argparse CLI in the tracked tree. Probing it would run
        # `cli-contract.py --help`, which scans again and probes itself again -- unbounded
        # recursive spawn, each generation orphaning the next to init. Read its flags from
        # source instead of executing it.
        if path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        is_cli, fallback_note = builds_a_parser(text)
        if not is_cli:
            continue
        flags = _flags_from_help(path)
        if flags is not None:
            mode, notes = "--help", []
        else:
            mode = "source"
            flags, notes = _flags_from_source(path)
        if fallback_note:
            notes = [*notes, fallback_note]
        name = path.name
        if name in clis:
            # Three identical `submit_suggestion.py` copies exist, one per tier. A caller names a
            # basename; union the parsers so a flag accepted by the copy actually invoked is not a
            # finding, and note the ambiguity.
            clis[name]["flags"] |= flags
            clis[name]["paths"].append(rel)
            clis[name]["notes"] += notes
        else:
            clis[name] = {"paths": [rel], "flags": flags, "mode": mode, "notes": list(notes)}
    return clis


# --------------------------------------------------------------------------------------------
# what the callers pass
# --------------------------------------------------------------------------------------------
def logical_lines(text: str) -> list[tuple[int, str]]:
    """Fold `\\`-continued lines. Returns (1-based line number of the first line, joined text)."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for n, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        out.append((start, " ".join(buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _cut(segment: str) -> str:
    """Everything up to the first shell terminator — the flags of THIS command only."""
    end = len(segment)
    for t in TERMINATORS:
        i = segment.find(t)
        if i != -1:
            end = min(end, i)
    return segment[:end]


def invocations(text: str, names: set[str]) -> list[tuple[int, str, str, set[str]]]:
    """(line, cli basename, the invocation text, flags passed)."""
    found: list[tuple[int, str, str, set[str]]] = []
    for lineno, line in logical_lines(text):
        if line.lstrip().startswith("#"):
            continue
        for name in names:
            start = 0
            while True:
                i = line.find(name, start)
                if i == -1:
                    break
                start = i + len(name)
                # Must be a whole path component: `xrender_developer_team.py` is a different file.
                before = line[i - 1] if i else " "
                if before.isalnum() or before in "_-":
                    continue
                segment = _cut(line[start:])
                flags = {m.group(1) for m in FLAG.finditer(segment)}
                if flags:
                    found.append((lineno, name, (name + segment).strip(), flags))
    return found


def caller_files(repo: Path, tracked: list[str], cli_paths: set[str]) -> list[str]:
    out = []
    for rel in tracked:
        if rel in cli_paths:
            continue
        # Not a caller either. This file's docstring spells out example invocations
        # (`resolver.py claim --report x`) to explain what the check does not cover; parsing
        # them as real calls turns its own prose into findings. It was skipped here for free
        # while it was in cli_paths -- keep that now that discovery excludes it.
        if (repo / rel).resolve() == SELF:
            continue
        if any(rel.startswith(d) for d in EXCLUDED_DIRS):
            continue
        if rel.endswith(CALLER_SUFFIXES) or Path(rel).name in CALLER_NAMES:
            out.append(rel)
    return out


# --------------------------------------------------------------------------------------------
def run(repo: Path) -> tuple[list[str], list[str], int]:
    """(findings, notes, scanned invocation count)."""
    # Tracked AND new-but-not-ignored -- a CLI added by the current unit is the one whose contract
    # has never been checked by anything. See `gitcorpus` and [[LSN-050]].
    try:
        tracked = repo_files(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"could not run: git ls-files failed ({exc})")

    clis = discover_clis(repo, tracked)
    if not clis:
        raise SystemExit("could not run: no argparse CLI found in the tracked tree")
    # A floor above zero, because the interesting collapse is partial. On 2026-08-01 the corpus
    # gate moved from a substring test to an AST one and correctly lost two members; a gate that
    # only refuses an EMPTY corpus would have said nothing had that change lost twenty. The floor
    # sits on the subject, not on the findings -- a tree with no contract violations is supposed to
    # report none, and is byte-identical to a scanner that stopped discovering CLIs.
    if len(clis) < MIN_CLIS:
        raise SystemExit(
            f"VACUOUS: {len(clis)} CLI(s) discovered, floor is {MIN_CLIS}. Either the corpus gate "
            f"in `builds_a_parser` stopped recognising a parser shape this tree uses, or a lot of "
            f"CLIs left the tree. Neither is something this check may pass through quietly."
        )

    cli_paths = {p for c in clis.values() for p in c["paths"]}
    names = set(clis)

    findings: list[str] = []
    notes: list[str] = []
    scanned = 0

    for name, cli in sorted(clis.items()):
        if cli["mode"] == "source":
            notes.append(
                f"{name}: flags read from source, not from `--help` (the script does not import "
                f"in a bare interpreter). Accepted set may be incomplete."
            )
        for note in dict.fromkeys(cli["notes"]):
            notes.append(f"{name}: {note}")
        if len(cli["paths"]) > 1:
            notes.append(f"{name}: {len(cli['paths'])} copies; accepted flags are their union")

    for rel in caller_files(repo, tracked, cli_paths):
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(n in text for n in names):
            continue
        for lineno, name, invocation, flags in invocations(text, names):
            scanned += 1
            unknown = sorted(flags - clis[name]["flags"])
            for flag in unknown:
                findings.append(
                    f"{rel}:{lineno}: `{name}` does not accept `--{flag}`\n"
                    f"      {invocation[:160]}\n"
                    f"      parser: {clis[name]['paths'][0]} (via {clis[name]['mode']})"
                )
    return findings, notes, scanned


# --------------------------------------------------------------------------------------------
def self_test() -> int:
    """The extractor must fire on the escape it was written for, and stay quiet otherwise."""
    accepted = {"cluster", "namespace", "repo-root", "hub-inference-cidr", "help"}
    cases = [
        (
            "the real escape: a caller passes a flag the parser dropped",
            "render_x.py --cluster a --github-cidrs 1.2.3.0/24 --repo-root $TMP",
            ["github-cidrs"],
        ),
        (
            "folded continuation lines are one invocation",
            "render_x.py --cluster a \\\n  --namespace n \\\n  --dead-flag v\n",
            ["dead-flag"],
        ),
        (
            "a following command's flags are not attributed to this one",
            "render_x.py --cluster a | grep --color=never x",
            [],
        ),
        (
            "a redirection ends the invocation",
            "render_x.py --cluster a >/dev/null 2>&1",
            [],
        ),
        ("a shell comment is not an invocation", "# render_x.py --github-cidrs is gone", []),
        ("a clean invocation is silent", "render_x.py --cluster a --repo-root .", []),
        (
            "--flag=value is the same flag",
            "render_x.py --repo-root=. --nope=1",
            ["nope"],
        ),
        (
            "a different file with a matching tail is not this CLI",
            "myrender_x.py --github-cidrs 1.2.3.0/24",
            [],
        ),
    ]
    failures = 0
    for label, text, expected in cases:
        got = sorted(
            f
            for _, _, _, flags in invocations(text, {"render_x.py"})
            for f in sorted(flags - accepted)
        )
        if got == expected:
            print(f"  control OK   {label}")
        else:
            print(f"  control DEAD {label}: expected {expected}, got {got}")
            failures += 1
    print(f"\n{len(cases) - failures}/{len(cases)} controls behave.")
    return 1 if failures else 0


def main() -> int:
    # This script takes no flags, so it never built a parser -- which meant `--help` fell
    # through to a full scan rather than printing and exiting. Anything probing this tree
    # with `--help` (including this checker) would start a recursive spawn. Answer it here.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "usage: cli-contract.py [--help] [--self-test]\n\n"
            "Checks that every flag a caller passes to a repo CLI is one that CLI defines.\n"
            "Takes no other arguments; scans the tracked tree from the repo root.\n\n"
            "  --help       show this message and exit\n"
            "  --self-test  run the built-in controls instead of the scan\n"
        )
        return 0
    if "--self-test" in sys.argv:
        return self_test()
    findings, notes, scanned = run(REPO)
    for note in notes:
        print(f"  note: {note}")
    print(f"  scope: {scanned} invocation(s) scanned; excluded {', '.join(EXCLUDED_DIRS)} "
          f"(historical build records)")
    if findings:
        print("\nCLI contract violations — a caller passes a flag the CLI does not accept:\n")
        for f in findings:
            print(f"  - {f}")
        print(
            "\nargparse exits 2 on an unknown flag. If the caller redirects stderr, the only\n"
            "symptom is the work not happening."
        )
        return 1
    print("CLI contract: OK — every flag passed to a repo CLI is one that CLI defines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
