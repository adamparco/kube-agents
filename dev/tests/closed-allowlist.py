#!/usr/bin/env python3
"""Closed-allowlist conformance validator (kube-agents Phase 8, P8-T1).

Implements the **L0 half of V-CTR-014** — _the permissive escape hatch does not
exist_ — plus the static half of **V-CTR-002 / 06 §1.2 V-7**, the rule that an
empty *or all-blank* allowlist is not an allowlist.

Why this check exists. The bypass it guards was not a missing validation; it was
four correct-looking validations that all agreed on the wrong predicate. The
provisioning template rendered:

    allowedUsers:
      - "${ALLOWED_USERS}"

from an `envsubst` variable that defaulted to the empty string. That is a list of
**size 1**, so `size(self.allowedUsers) > 0` passed, `len(x) == 0` passed, and the
controller then read the lone blank entry and emitted `SLACK_ALLOW_ALL_USERS=true`
into the pod. Every layer was individually defensible and the boundary was open.

So the check is deliberately not "does a validation exist" — it is "does the
forbidden *shape* appear anywhere", which is the thing a future refactor can
actually reintroduce by accident.

Checks (all must pass for exit 0):

  1. **No permissive identifier anywhere.** The substring `ALLOW_ALL_USERS` does
     not appear in any tracked source file — renderer, template, provisioning
     script, config default, manifest or golden. Prose that names the retired
     hatch is allowed only in the documentation paths listed in DOC_ALLOWLIST,
     because a design document that cannot mention what it removed cannot explain
     the removal.
  2. **No size-only allowlist guard.** No Go source or CEL rule guards an
     allowlist with a bare `len(...) == 0` / `size(...) > 0` predicate. The guard
     must inspect entry content — `hasNonBlankEntry` in Go, `exists(u, u.trim()
     != '')` in CEL.
  3. **Both CEL rules are content-aware**, in the type source and in the
     generated CRD, which are separate artifacts that drift independently.
  3b. **No kubebuilder marker has been mangled by gofmt.** The formatter rewrites
     an ASCII quote pair inside a comment into a typographic close-quote, which
     turns a CEL empty-string literal into uncompilable garbage — silently, and
     one generation before it fails.
  4. **No template renders a literal single-element allowlist** from a bare
     variable expansion — the exact shape that started this.
  5. **Provisioning never offers "leave it empty to allow everyone."** No prompt
     string may advertise the permissive default.

Negative control (`--self-test`): each check is re-run against an in-memory
fixture that reintroduces the defect it guards, and must fail. A check that
cannot fail is not evidence (09 §6, V-MET-014).

Usage:
    python3 dev/tests/closed-allowlist.py [REPO_ROOT]
    python3 dev/tests/closed-allowlist.py --self-test

Exit code 0 = the allowlist is closed on every static layer; 1 = one or more
violations (prints them). Stdlib only, no network, no cluster.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitcorpus import repo_files  # noqa: E402

# The retired escape hatch. Matching on the bare identifier rather than on the
# two fully-qualified names is deliberate: a third integration would otherwise
# be free to invent GITHUB_ALLOW_ALL_USERS and pass.
FORBIDDEN_IDENT = "ALLOW_ALL_USERS"

# Paths where the retired hatch may be *named* but never *implemented*: the
# specs, the build ledger and the phase breakdowns have to be able to describe
# what was removed and why. This validator itself is excluded for the same
# reason. Everything else is source.
DOC_ALLOWLIST = (
    "docs/design/",
    "docs/build/",
    # The harness's own prose — LESSONS.md exists to record that this hatch was
    # removed and why, which it cannot do without naming it. Same rationale as
    # docs/build/; it was missing only because nothing under .claude/harness/ had
    # ever mentioned the identifier until LSN-017 was written.
    ".claude/harness/",
    # V-CTR-014's own evidence rows. These bytes were inside docs/build/LEDGER.md
    # and exempt under the prefix above until 2026-07-26, when the by-check-ID
    # table moved to a CSV; the exemption follows the content rather than being
    # a new one. Named as an exact file, not as `verification/`, because the
    # directory will later hold per-run manifests written by whatever produced
    # them, and a prefix here would exempt those sight-unseen.
    "verification/results.csv",
    # The requirement enumeration (V-MET-009). It is a generated, verbatim mirror of the
    # normative statements in docs/design/01-08, and 07 §2's acceptance text names the retired
    # hatch while describing its deletion — the same bytes, already exempt one line above under
    # `docs/design/`. The exemption follows the content, exactly as it did for results.csv.
    # An exact file for the same reason given there: `verification/` will hold per-run manifests
    # written by whatever produced them, and a prefix would exempt those sight-unseen.
    #
    # Note this is narrower than it looks. The file's `text:` values are asserted equal to the
    # spec's own sentences on every L0 run, so the only way to smuggle an emission through this
    # exemption is to first put it in docs/design/ — where it is already exempt, and where it
    # would be read.
    "verification/requirements.yaml",
    "dev/tests/closed-allowlist.py",
)

# Files that legitimately assert the *absence* of the identifier. These are
# security assertions and removing them would trip the V-MET-003 ratchet, so
# they are permitted to contain the string — but only inside an absence check.
ASSERTION_FILES = (
    "k8s-operator/internal/controller/agent_manifests_test.go",
    "k8s-operator/internal/testing/cluster_admin_render_test.go",
    "k8s-operator/internal/testing/testdata/cluster-admin/input.yaml",
    "k8s-operator/internal/router/resolve_test.go",
    "k8s-operator/internal/router/authorize.go",
    "k8s-operator/internal/controller/manifest_helpers.go",
    # The live half of V-CTR-014: it exists to prove no rendered Deployment carries
    # the env var, so it has to be able to name it. The emission guard below still
    # applies — it may assert the absence, never render one.
    "dev/verify/closed-allowlist-l2.sh",
    # The documentation half: docs-truth.py carries the identifier in its RETIRED
    # table so it can fail any document that still promises the hatch. Two of the
    # falsehoods it found on its first run were exactly that (P8-T7). It is an
    # absence assertion aimed at prose rather than at source, which is why it
    # belongs here and not in DOC_ALLOWLIST — the emission guard should keep
    # applying to it like any other check.
    "dev/tests/docs-truth.py",
)

# What "emitting" the hatch looks like, as opposed to naming it in an absence
# assertion. The ASSERTION_FILES exemption is only worth granting if this can
# tell the two apart, and until P8-T7 it could not: the single pattern was the Go
# struct-literal shape `Name:  "FOO"`, so of the eight exempted files only the Go
# sources were actually guarded. `export SLACK_ALLOW_ALL_USERS=true` appended to
# closed-allowlist-l2.sh passed cleanly. An exemption whose guard does not speak
# the exempted file's language is a blanket pass wearing a guard's clothes.
#
# Anchors stay narrow on purpose. The assertion files are full of lines like
# `grep -q "ALLOW_ALL_USERS"` and `carries no *_ALLOW_ALL_USERS env var`, and
# those must stay green — an emission binds the identifier to a value, a mention
# does not.
EMISSION_SHAPES = (
    # Go struct literal, YAML mapping, JSON/Python dict: a name key bound to it.
    # The `\\?` before each quote covers the same shape nested inside a string
    # literal — a JSON blob in a Go const, a heredoc that writes a manifest.
    (
        "name-keyed",
        re.compile(r"""\\?["']?[Nn]ame\\?["']?\s*:\s*\\?["']?[A-Z_]*ALLOW_ALL_USERS"""),
    ),
    # Shell/dotenv assignment, with or without `export`.
    ("assignment", re.compile(r"(?:^|[\s;({])[A-Z_]*ALLOW_ALL_USERS\s*=")),
)

# Comment markers across the languages in ASSERTION_FILES (Go, shell, YAML,
# Python). The shapes above are matched against the code half of the line only:
# four of the exempted files carry comments like `// ... renders
# GOOGLE_CHAT_ALLOW_ALL_USERS=true. P8-T1 deleted it`, which is the assignment
# shape verbatim and is also the exact prose the exemption exists to permit.
# Narrating the removed behaviour is the point; a comment cannot set an env var.
#
# This does mishandle a `#` inside a shell string (`echo "# FOO=true"` reads as
# prose). That is the right trade: the failure mode is a missed emission that
# only echoes text, and the alternative is deleting the comments that explain
# why the hatch is gone.
COMMENT_MARKERS = ("//", "#")


def code_part(line: str) -> str:
    """The line up to its first comment marker."""
    cut = min(
        (line.index(m) for m in COMMENT_MARKERS if m in line),
        default=len(line),
    )
    return line[:cut]

# A size-only guard on an allowlist: the predicate the bypass satisfied.
SIZE_ONLY_GO = re.compile(r"len\(\s*\w*\.?AllowedUsers\s*\)\s*[=!]=\s*0")
SIZE_ONLY_CEL = re.compile(r"size\(\s*self\.allowedUsers\s*\)\s*>\s*0")

# The content-aware CEL predicate that must replace it. Both spellings are
# accepted, but only the size() one is safe to WRITE in a Go marker comment —
# see check_markers_survive_gofmt.
CEL_CONTENT_AWARE = re.compile(
    r"self\.allowedUsers\.exists\(\s*\w+\s*,\s*"
    r"(?:\w+\.trim\(\)\s*!=\s*''|\w+\.trim\(\)\.size\(\)\s*>\s*0)\s*\)"
)

# Typographic quotes gofmt substitutes into line comments. A kubebuilder marker
# is a comment, so the formatter edits it like prose.
SMART_QUOTES = "‘’“”"
KUBEBUILDER_MARKER = re.compile(r"^\s*//\s*\+kubebuilder:")

# `allowedUsers:` followed by exactly one bare "${VAR}" item.
TEMPLATE_BARE_EXPANSION = re.compile(
    r"allowedUsers:\s*\n\s*-\s*\"\$\{[A-Z_]+\}\"", re.MULTILINE
)

# Prompts that advertise the permissive default.
PERMISSIVE_PROMPT = re.compile(
    r"(empty|blank).{0,40}(allow|admit).{0,20}all|allow.{0,20}all.{0,30}(if|when).{0,20}(empty|blank)",
    re.IGNORECASE,
)

CEL_SOURCE = "k8s-operator/api/v1alpha1/agent_types.go"
CEL_GENERATED = "k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agents.yaml"
TEMPLATES = (
    "k8s-operator/scripts/platform-agent.yaml.template",
    "k8s-operator/scripts/cluster-admin-agent.yaml.template",
    "k8s-operator/scripts/developer-team-agent.yaml.template",
)
PROVISION_GLOB = "k8s-operator/scripts/provision_*.sh"


# Paths that live in .git/info/exclude and are force-added on commit (the harness
# tree, the build ledger, the dev suites). `git ls-files --others
# --exclude-standard` deliberately hides them, so they are walked directly.
FORCE_ADDED_ROOTS = (".claude/harness", ".claude/skills", "docs/build", "dev")
SOURCE_SUFFIXES = (".go", ".py", ".sh", ".yaml", ".yml", ".md", ".template", ".json")


def tracked_files(root: Path) -> list[str]:
    """The corpus: every tracked file, plus every new file not yet added.

    This used to be `git ls-files` alone, and that made the check blind in the one
    window where it matters most. A unit writes a new script, runs this validator
    (green — the file is untracked, so it was never scanned), records the green in
    the ledger, and only then commits. The evidence was gathered against a tree
    that did not contain the work. P8-T1 shipped exactly that way: the violation in
    `closed-allowlist-l2.sh` surfaced one commit later, in P8-T2 (LSN-017).

    So the corpus is the working tree as it will exist after the commit, not as git
    currently indexes it: tracked files, untracked-but-not-ignored files, and a
    direct walk of the force-added roots that `--exclude-standard` would drop.
    """
    seen: dict[str, None] = {}

    for rel in repo_files(root):
        seen.setdefault(rel, None)

    for base in FORCE_ADDED_ROOTS:
        for path in (root / base).rglob("*"):
            if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                seen.setdefault(str(path.relative_to(root)), None)

    return sorted(seen)


def read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except (OSError, IsADirectoryError):
        return ""


def check_no_permissive_identifier(root: Path, files: list[str]) -> list[str]:
    """1. `*_ALLOW_ALL_USERS` appears nowhere outside documentation and absence
    assertions."""
    errors = []
    for rel in files:
        if rel.startswith(DOC_ALLOWLIST):
            continue
        text = read(root, rel)
        if FORBIDDEN_IDENT not in text:
            continue
        if rel in ASSERTION_FILES:
            # Permitted, but only as an absence assertion — never as an emission.
            for lineno, line in enumerate(text.splitlines(), 1):
                if FORBIDDEN_IDENT not in line:
                    continue
                for shape, pattern in EMISSION_SHAPES:
                    if pattern.search(code_part(line)):
                        errors.append(
                            f"{rel}:{lineno}: emits the retired {FORBIDDEN_IDENT} "
                            f"env var ({shape} form)"
                        )
                        break
            continue
        first = next(
            lineno
            for lineno, line in enumerate(text.splitlines(), 1)
            if FORBIDDEN_IDENT in line
        )
        errors.append(
            f"{rel}:{first}: the retired {FORBIDDEN_IDENT} escape hatch reappears "
            f"(V-CTR-014; it was deleted in P8-T1)"
        )
    return errors


def check_no_size_only_guard(root: Path, files: list[str]) -> list[str]:
    """2. No allowlist is guarded by a predicate that counts entries without
    looking at them."""
    errors = []
    for rel in files:
        if rel.startswith(DOC_ALLOWLIST) or not rel.endswith((".go", ".yaml")):
            continue
        # Test files may construct degenerate lists; they assert on behaviour,
        # not on the guard. The guard itself lives in non-test source.
        if rel.endswith("_test.go"):
            continue
        text = read(root, rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            if SIZE_ONLY_GO.search(line) or SIZE_ONLY_CEL.search(line):
                errors.append(
                    f"{rel}:{lineno}: allowlist guarded by entry count alone — "
                    f'a one-element list containing "" satisfies it (06 §1.2 V-7)'
                )
    return errors


def check_cel_is_content_aware(root: Path) -> list[str]:
    """3. Both the marker source and the generated CRD carry the content-aware
    rule. They are separate artifacts and `make manifests` is easy to forget."""
    errors = []
    for rel, want in ((CEL_SOURCE, 2), (CEL_GENERATED, 2)):
        text = read(root, rel)
        if not text:
            errors.append(f"{rel}: missing — cannot verify the CEL allowlist rule")
            continue
        found = len(CEL_CONTENT_AWARE.findall(text))
        if found < want:
            errors.append(
                f"{rel}: expected {want} content-aware allowedUsers CEL rules "
                f"(googleChat + slack), found {found}. Run `make manifests` if the "
                f"marker source is correct but the CRD is stale."
            )
    return errors


def check_markers_survive_gofmt(root: Path, files: list[str]) -> list[str]:
    """3b. No kubebuilder marker contains a typographic quote.

    A kubebuilder marker is a Go line comment, and `gofmt` applies its legacy
    prose-quoting substitution to comments: a pair of adjacent ASCII apostrophes
    becomes a single U+201D. The CEL empty-string literal is exactly that pair,
    so `rule="... != ''"` is rewritten into a rule the API server cannot compile.

    The failure is invisible at the point it happens. `make build` runs
    controller-gen BEFORE `go fmt`, so the CRD generated in the same command is
    correct and every unit test passes -- Go never compiles CEL. The corruption
    only reaches the CRD on the NEXT generation, and only fails at `kubectl
    apply` time, in whatever change happens to run it. Hence a static guard on
    the marker text itself rather than trust in the build order.
    """
    errors = []
    for rel in files:
        if not rel.endswith(".go"):
            continue
        for lineno, line in enumerate(read(root, rel).splitlines(), 1):
            if not KUBEBUILDER_MARKER.match(line):
                continue
            bad = sorted({ch for ch in line if ch in SMART_QUOTES})
            if bad:
                errors.append(
                    f"{rel}:{lineno}: kubebuilder marker contains typographic "
                    f"quote(s) {bad} — gofmt rewrote an ASCII quote pair in this "
                    f"comment and the marker is no longer valid CEL. Restate the "
                    f"predicate without quote characters (e.g. size() > 0)."
                )
    return errors


def check_templates(root: Path) -> list[str]:
    """4. No template renders a single-element allowlist from a bare variable."""
    errors = []
    for rel in TEMPLATES:
        text = read(root, rel)
        if not text:
            errors.append(f"{rel}: missing")
            continue
        if TEMPLATE_BARE_EXPANSION.search(text):
            errors.append(
                f"{rel}: renders a one-element allowlist from a bare ${{VAR}} expansion. "
                f"An unset variable yields [\"\"] — size 1, names nobody. Render the "
                f"block conditionally via render_allowlist_block instead."
            )
    return errors


def check_provisioning_prompts(root: Path) -> list[str]:
    """5. No provisioning prompt advertises an allow-all default."""
    errors = []
    for path in sorted(root.glob(PROVISION_GLOB)):
        rel = str(path.relative_to(root))
        for lineno, line in enumerate(read(root, rel).splitlines(), 1):
            if "ALLOWED_USERS" not in line:
                continue
            if PERMISSIVE_PROMPT.search(line):
                errors.append(
                    f"{rel}:{lineno}: prompt offers a permissive default for an "
                    f"allowlist — there is no allow-all option"
                )
    return errors


def run(root: Path) -> list[str]:
    files = tracked_files(root)
    errors: list[str] = []
    errors += check_no_permissive_identifier(root, files)
    errors += check_no_size_only_guard(root, files)
    errors += check_cel_is_content_aware(root)
    errors += check_markers_survive_gofmt(root, files)
    errors += check_templates(root)
    errors += check_provisioning_prompts(root)
    return errors


# ─── Negative controls ────────────────────────────────────────────────────────
# Each control reintroduces exactly one form of the defect and asserts the
# corresponding matcher fires. Without these, a typo'd regex reads as green.

def emits(line: str) -> bool:
    """Would check_no_permissive_identifier call this line an emission?"""
    return any(pattern.search(code_part(line)) for _, pattern in EMISSION_SHAPES)


NEGATIVE_CONTROLS = (
    # One control per emission shape, in the syntax of a file that is actually
    # exempted. The control this replaced read `FORBIDDEN_IDENT in '...'` — a
    # substring test that never touched the regex, so it stayed green through
    # the entire period the shell and YAML shapes went unguarded (V-MET-014).
    ("emitted env var (Go struct literal)", lambda: emits('Name:  "SLACK_ALLOW_ALL_USERS",')),
    (
        "emitted env var (Go struct literal nested in a string)",
        lambda: emits(r'const j = "{\"Name\": \"SLACK_ALLOW_ALL_USERS\"}"'),
    ),
    ("emitted env var (YAML mapping)", lambda: emits("        - name: SLACK_ALLOW_ALL_USERS")),
    (
        "emitted env var (Python/JSON dict)",
        lambda: emits('    {"Name": "SLACK_ALLOW_ALL_USERS", "Value": "true"},'),
    ),
    ("emitted env var (shell export)", lambda: emits("export SLACK_ALLOW_ALL_USERS=true")),
    ("emitted env var (bare shell assignment)", lambda: emits("SLACK_ALLOW_ALL_USERS=true")),
    # And the other half: the lines the assertion files really contain must NOT
    # read as emissions, or the exemption collapses into a ban and the security
    # assertions get deleted to make the lint pass.
    ("mention in a grep is not an emission", lambda: not emits('  grep -q "ALLOW_ALL_USERS"')),
    (
        "mention in prose is not an emission",
        lambda: not emits('pass "no Deployment carries a *_ALLOW_ALL_USERS env var"'),
    ),
    (
        "Go map lookup is not an emission",
        lambda: not emits('\tif _, ok := envMap["GOOGLE_CHAT_ALLOW_ALL_USERS"]; ok {'),
    ),
    (
        "Go comment narrating the removed assignment is not an emission",
        lambda: not emits(
            "// that an empty allowlist renders GOOGLE_CHAT_ALLOW_ALL_USERS=true. P8-T1"
        ),
    ),
    (
        "YAML comment narrating it is not an emission",
        lambda: not emits("#     GOOGLE_CHAT_ALLOW_ALL_USERS, which P8-T1 deleted outright"),
    ),
    (
        "code before a comment is still checked",
        lambda: emits("export SLACK_ALLOW_ALL_USERS=true  # deleted in P8-T1"),
    ),
    (
        "Go range over names is not an emission",
        lambda: not emits(
            '\tfor _, name := range []string{"GOOGLE_CHAT_ALLOW_ALL_USERS", "SLACK_ALLOW_ALL_USERS"} {'
        ),
    ),
    (
        "size-only Go guard",
        lambda: bool(SIZE_ONLY_GO.search("if len(sl.AllowedUsers) == 0 {")),
    ),
    (
        "size-only CEL guard",
        lambda: bool(
            SIZE_ONLY_CEL.search(
                "has(self.allowedUsers) && size(self.allowedUsers) > 0"
            )
        ),
    ),
    (
        "content-aware CEL recognized (quote form)",
        lambda: bool(
            CEL_CONTENT_AWARE.search("self.allowedUsers.exists(u, u.trim() != '')")
        ),
    ),
    (
        "content-aware CEL recognized (size form)",
        lambda: bool(
            CEL_CONTENT_AWARE.search(
                "self.allowedUsers.exists(u, u.trim().size() > 0)"
            )
        ),
    ),
    (
        "gofmt-mangled marker rejected",
        lambda: bool(
            KUBEBUILDER_MARKER.match(
                '// +kubebuilder:validation:XValidation:rule="x != ”"'
            )
        )
        and any(
            ch in SMART_QUOTES
            for ch in '// +kubebuilder:validation:XValidation:rule="x != ”"'
        ),
    ),
    (
        "intact marker accepted",
        lambda: not any(
            ch in SMART_QUOTES
            for ch in '// +kubebuilder:validation:XValidation:rule="u.trim().size() > 0"'
        ),
    ),
    (
        "bare template expansion",
        lambda: bool(
            TEMPLATE_BARE_EXPANSION.search(
                '      allowedUsers:\n        - "${ALLOWED_USERS}"\n'
            )
        ),
    ),
    (
        "permissive prompt",
        lambda: bool(
            PERMISSIVE_PROMPT.search(
                'init_var "ALLOWED_USERS" "" "Leaving it empty will allow all users."'
            )
        ),
    ),
)


def self_test() -> int:
    failures = []
    for name, control in NEGATIVE_CONTROLS:
        if not control():
            failures.append(name)
    if failures:
        print("NEGATIVE CONTROL FAILED — these matchers do not fire on the defect:")
        for name in failures:
            print(f"  ✗ {name}")
        return 1
    print(f"Negative controls: {len(NEGATIVE_CONTROLS)}/{len(NEGATIVE_CONTROLS)} fire on a reintroduced defect.")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()

    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[2]

    errors = run(root)
    if errors:
        print(f"CLOSED-ALLOWLIST VIOLATIONS ({len(errors)}) — V-CTR-002 / V-CTR-014:")
        for err in errors:
            print(f"  ✗ {err}")
        return 1

    if self_test() != 0:
        return 1

    print("V-CTR-014 (L0): the permissive escape hatch does not exist.")
    print("V-CTR-002 (L0, static half): every allowlist guard inspects entry content.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
