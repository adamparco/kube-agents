#!/usr/bin/env python3
"""V-MET-007 (L0) — the security review gate is triggered by the security surface.

WHAT THIS IS FOR. 06 §7 makes the review gate the authoritative merge decision, and a gate that
does not run makes no decision at all. On 2026-07-27 that turned out to be the live state of this
repository: `.github/workflows/review-gate.yml` triggered on `**/provisioning/**`, `**/agents/**`,
`**/namespaces/**`, `**/policy/**` and `**/SOUL.md` — every one of them a manifest or a persona
path, because in Phase 5 that is what the security surface was. PR #33 then added the action
broker, its authenticator, its TLS configuration and the one image whose ServiceAccount can write,
and the gate did not run on it. Nothing was red. The PR page showed a clean set of checks with the
security gate simply absent, which is the exact failure mode V-MET-007 names: not a suite that
failed, a suite that silently skipped.

WHAT IT ASSERTS, AND IN WHICH DIRECTION. The security surface is DERIVED from the repository, not
listed here, and every derived file must be matched by at least one glob in the workflow. Three
independent sources, each of which is maintained for its own reasons and therefore keeps working
when nobody is thinking about this file:

  1. Go files carrying a `+kubebuilder:rbac` or `+kubebuilder:webhook` marker — the code that
     decides what the operator may do and what admission rejects.
  2. Go files that build a `tls.Config` or issue a `TokenReview`/`SubjectAccessReview` — the code
     that decides who is talking and whether to believe them.
  3. Manifests declaring an authority-granting kind — ClusterRole, RoleBinding, ServiceAccount,
     NetworkPolicy, ValidatingAdmissionPolicy, and the rest of the list in `AUTHORITY_KINDS`.

The check is **one-directional**. It can prove a glob is missing; it cannot prove one is
unnecessary, and it deliberately does not try. Over-triggering costs CI minutes, under-triggering
costs a review, and a rule that pushed back on breadth would be optimising the wrong one. A filter
of `- "**"` would satisfy this test, and that is the correct outcome, not a hole.

WHAT IT DOES NOT CLAIM. Nothing about whether the detector finds anything, whether the scorer's
threshold is right, or whether a waiver is justified — `scripts/review-gate/test_score_findings.py`
owns the scorer and the gate itself owns the rest. This test's whole subject is whether the gate is
invoked at all.

THE GLOB MATCHER IS CALIBRATED, NOT ASSUMED. GitHub's filter semantics have one clause that decides
the answer here: `**/agents/**` matches `agents/platform/SOUL.md` at the repository root, because a
leading `**/` matches zero directories as well as many. Read the other way, the old filter would
never have run on any agent change and the whole premise would be wrong. So the matcher is checked
against two runs that actually happened — PR #33, where the gate did not trigger, and PR #79, where
it did — replayed against the filter as it stood at the time. Those are observations of GitHub's
behaviour, not of this file's.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github/workflows/review-gate.yml"

sys.path.insert(0, str(REPO / "dev/tests"))
from gitcorpus import repo_files  # noqa: E402

# Go source that decides authority. Two constants rather than one, and the non-vacuity tests below
# use these and not copies of them: a derivation whose arms are restated in its own tests can lose
# an arm without any test noticing, which is the shape of every check that quietly stops looking.
GO_MARKERS = re.compile(r"\+kubebuilder:rbac|\+kubebuilder:webhook")  # what the operator may do
GO_AUTHN = re.compile(r"\btls\.Config\b|\bTokenReview\b|\bSubjectAccessReview\b")  # who is calling

# A manifest of one of these kinds grants, binds, restricts or admits. Anchored to the start of a
# line so a `kind:` nested inside a `rules:` block (which names kinds it permits, rather than
# declaring one) is not mistaken for a declaration.
AUTHORITY_KINDS = (
    "ClusterRole",
    "ClusterRoleBinding",
    "Role",
    "RoleBinding",
    "ServiceAccount",
    "NetworkPolicy",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration",
    "MutatingWebhookConfiguration",
    "ResourceQuota",
)
MANIFEST_AUTHORITY = re.compile(r"^kind:\s*(?:" + "|".join(AUTHORITY_KINDS) + r")\s*$", re.M)


def tracked_files() -> list[str]:
    """Every tracked-or-new, non-ignored file — `gitcorpus.repo_files`, not a bare `git ls-files`.

    LSN-050: `ls-files` without `--others` lists the INDEX, so a Go file written in the current
    unit and not yet staged is invisible. That blindness is perfectly correlated with novelty,
    which for this check means it would see every package that has already been reviewed and miss
    the one that has not — the same shape as the defect it exists to catch, one level up.
    """
    return repo_files(REPO)


def read(path: str) -> str:
    return (REPO / path).read_text(errors="ignore")


def go_files_matching(files: list[str], pattern: re.Pattern[str]) -> set[str]:
    return {f for f in files if f.endswith(".go") and pattern.search(read(f))}


def authority_manifests(files: list[str]) -> set[str]:
    return {
        f for f in files if f.endswith((".yaml", ".yml")) and MANIFEST_AUTHORITY.search(read(f))
    }


def security_surface() -> set[str]:
    files = tracked_files()
    return (
        go_files_matching(files, GO_MARKERS)
        | go_files_matching(files, GO_AUTHN)
        | authority_manifests(files)
    )


# --- GitHub Actions filter-pattern semantics -------------------------------------------------
#
# Documented at docs.github.com "Workflow syntax → onpushpull_requestpaths": `*` matches zero or
# more characters but does not match `/`; `**` matches zero or more of any character INCLUDING `/`;
# `?` matches one character other than `/`. The leading-`**/`-matches-nothing case is the one that
# matters here and the one the recorded-PR calibration below exists to pin down.


def github_glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = ["^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            # Zero or more leading directories — this is what lets `**/agents/**` match a path
            # that begins with `agents/`.
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches_any(patterns: list[str], path: str) -> bool:
    return any(github_glob_to_regex(p).match(path) for p in patterns)


# --- reading the workflow ---------------------------------------------------------------------
#
# No PyYAML: L0 installs no dependencies, by design. This is a deliberately narrow reader that
# refuses anything it was not written for rather than guessing, because a parser that silently
# returns [] on a shape it does not understand turns this whole file into a check that cannot fail.


class WorkflowShapeError(AssertionError):
    pass


def pull_request_block() -> list[str]:
    lines = WORKFLOW.read_text().splitlines()
    try:
        on_at = lines.index("on:")
    except ValueError as exc:  # pragma: no cover - shape guard
        raise WorkflowShapeError("review-gate.yml has no top-level `on:` key") from exc
    try:
        pr_at = lines.index("  pull_request:", on_at)
    except ValueError as exc:  # pragma: no cover - shape guard
        raise WorkflowShapeError("review-gate.yml has no `on.pull_request:` key") from exc
    end = pr_at + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    return lines[pr_at + 1 : end]


def _scalar(value: str) -> str:
    """Strip a trailing `# comment` and any surrounding quotes from a YAML flow scalar."""
    if '"' in value:
        return value.split('"')[1]
    return value.split("#", 1)[0].strip()


def pull_request_key(name: str) -> list[str]:
    """The list value of `on.pull_request.<name>`, in either block or flow form.

    Raises rather than returning [] when the key is absent, so a renamed key (`paths` →
    `paths-ignore`, which inverts the meaning of the whole filter) is a failure and not an
    empty result that every coverage assertion would then vacuously accept.
    """
    block = pull_request_block()
    header = f"    {name}:"
    for i, line in enumerate(block):
        if not line.startswith(header):
            continue
        inline = line[len(header) :].strip()
        if inline.startswith("["):
            return [_scalar(v) for v in inline.strip("[]").split(",") if v.strip()]
        values = []
        for item in block[i + 1 :]:
            stripped = item.strip()
            if not stripped or stripped.startswith("#"):
                continue  # interleaved comments are the norm in this block
            if not stripped.startswith("- "):
                break
            values.append(_scalar(stripped[2:]))
        return values
    raise WorkflowShapeError(f"`on.pull_request.{name}` is not present in review-gate.yml")


# --- calibration: two runs that actually happened ----------------------------------------------

FILTER_AS_OF_PR_33 = [
    "**/provisioning/**",
    "**/agents/**",
    "**/namespaces/**",
    "**/policy/**",
    "**/SOUL.md",
    ".agents/skills/review-security-k8s-**",
    "security-review-waivers.yaml",
    "scripts/review-gate/**",
    ".github/workflows/review-gate.yml",
]

# PR #33 — "the action broker". The gate did NOT run. Abridged to one file per directory: the
# question is which directories the filter reaches, and 90 envelope fixtures answer it once.
PR_33_FILES = [
    ".github/workflows/docker-build.yml",
    ".prettierignore",
    "Makefile",
    "deploy/docker/cloudbuild.yaml",
    "dev/cluster/reload-images.sh",
    "docs/build/LEDGER.md",
    "k8s-operator/Dockerfile.broker",
    "k8s-operator/Makefile",
    "k8s-operator/cmd/broker/main.go",
    "k8s-operator/internal/broker/auth.go",
    "k8s-operator/internal/broker/server.go",
    "k8s-operator/scripts/live_refresh.sh",
    "verification/fixtures/envelopes/valid/platform.scale-deployment.json",
    "verification/results.csv",
]

# PR #79 — "the apply-change skill". The gate DID run, and the only reason is `**/agents/**`
# reaching a path that starts with `agents/`. This is the whole calibration.
PR_79_FILES = [
    "agents/platform/skills/apply-change/SKILL.md",
    "dev/test_apply_change_skill.py",
    "docs/build/LEDGER.md",
    "docs/design/09-verification-and-validation.md",
    "verification/mutants/V-CTR-020.json",
    "verification/results.csv",
]


class TestTheMatcherAgreesWithGitHub(unittest.TestCase):
    """Two recorded outcomes. If the matcher's reading of `**` were wrong in either direction,
    one of these would flip — and the coverage test below would then be measuring nothing."""

    def test_pr_33_did_not_trigger_the_gate_under_the_filter_of_the_day(self):
        matched = [f for f in PR_33_FILES if matches_any(FILTER_AS_OF_PR_33, f)]
        self.assertEqual(
            matched,
            [],
            "PR #33 demonstrably did not trigger the review gate, so no file of it may match the "
            "filter as it stood; the matcher is reading `**` too generously",
        )

    def test_pr_79_did_trigger_the_gate_under_the_same_filter(self):
        matched = [f for f in PR_79_FILES if matches_any(FILTER_AS_OF_PR_33, f)]
        self.assertEqual(
            sorted(matched),
            ["agents/platform/skills/apply-change/SKILL.md"],
            "PR #79 triggered the gate through `**/agents/**` alone; if this is empty the matcher "
            "is refusing a leading `**/` the chance to match zero directories",
        )

    def test_a_single_star_stops_at_a_directory_boundary(self):
        self.assertTrue(matches_any(["k8s-operator/*.go"], "k8s-operator/main.go"))
        self.assertFalse(matches_any(["k8s-operator/*.go"], "k8s-operator/cmd/main.go"))
        self.assertTrue(matches_any(["k8s-operator/**.go"], "k8s-operator/cmd/main.go"))


class TestTheDerivationFindsSomething(unittest.TestCase):
    """LSN-021: a check that verifies nothing runs green forever. Each source is asserted to be
    non-empty separately, because a union stays healthy-looking while one arm of it dies."""

    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()

    def test_git_lists_files(self):
        self.assertGreater(len(self.files), 100, "`git ls-files` returned almost nothing")

    def test_the_kubebuilder_marker_source_finds_files(self):
        self.assertTrue(
            go_files_matching(self.files, GO_MARKERS),
            "no Go file carries an RBAC or webhook marker — that arm of the derivation is dead",
        )

    def test_the_authentication_source_finds_files(self):
        self.assertTrue(
            go_files_matching(self.files, GO_AUTHN),
            "no Go file builds a TLS config or reviews a token — that arm of the derivation is dead",
        )

    def test_the_manifest_source_finds_files(self):
        self.assertTrue(authority_manifests(self.files), "no authority-granting manifest found")

    def test_the_surface_spans_both_go_and_yaml(self):
        surface = security_surface()
        self.assertTrue(any(f.endswith(".go") for f in surface))
        self.assertTrue(any(f.endswith((".yaml", ".yml")) for f in surface))


class TestTheFilterCoversTheSurface(unittest.TestCase):
    def test_every_derived_file_is_matched_by_the_filter(self):
        patterns = pull_request_key("paths")
        uncovered = sorted(f for f in security_surface() if not matches_any(patterns, f))
        self.assertEqual(
            uncovered,
            [],
            "these files decide authority and a PR touching only them would not run the security "
            "review gate — add a glob to `on.pull_request.paths` in review-gate.yml:\n  "
            + "\n  ".join(uncovered),
        )

    def test_the_broker_is_covered(self):
        """The regression itself, stated once as an anchor: whatever else changes about the
        derivation, the package PR #33 added must never fall back out of the filter."""
        patterns = pull_request_key("paths")
        for path in (
            "k8s-operator/internal/broker/auth.go",
            "k8s-operator/internal/broker/server.go",
            "k8s-operator/internal/webhook/agent_webhook.go",
            "k8s-operator/config/rbac/role.yaml",
        ):
            with self.subTest(path=path):
                self.assertTrue(matches_any(patterns, path))


class TestTheTriggerItselfCannotSilentlySkip(unittest.TestCase):
    def test_the_filter_is_an_allow_list_not_an_ignore_list(self):
        with self.assertRaises(WorkflowShapeError):
            pull_request_key("paths-ignore")
        self.assertTrue(pull_request_key("paths"))

    def test_the_gate_reruns_on_every_push_to_the_pr(self):
        types = pull_request_key("types")
        self.assertIn(
            "synchronize",
            types,
            "without `synchronize` the gate judges the first push and nothing after it — a PR "
            "could go green and then grow the change that needed reviewing",
        )
        self.assertIn("opened", types)

    def test_the_workflow_reviews_its_own_filter(self):
        self.assertTrue(
            matches_any(pull_request_key("paths"), ".github/workflows/review-gate.yml"),
            "narrowing the filter must itself be a change the gate runs on",
        )


if __name__ == "__main__":
    unittest.main()
