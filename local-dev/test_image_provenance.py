#!/usr/bin/env python3
"""No first-party image reference may name something CI does not publish (V-CMP-002, L0).

The defect this closes had two independent halves, both live when Phase 8 opened:

  1. **Three images were built by nothing.** `cluster-admin-agent` and `developer-team-agent` had
     Dockerfile targets since the tiers existed, `kage-router` had a Dockerfile and a kustomize base
     pinning it since Phase 6 -- and no workflow built any of them. The operator defaults every
     non-platform Agent CR to those two image names (manifest_helpers.go), so every cluster-admin
     and developer-team agent resolved to an image that did not exist.

  2. **The tag everything pinned was published by nothing.** Every manifest, kustomization and
     Makefile default in the tree pinned `:v0.1.0`, six of them under a comment reading "pinned to
     an immutable version tag (not :latest)". CI published `:latest` and `:${github.sha}`. It never
     once published `v0.1.0` -- for ANY image, including the three that were being built.

Both are LSN-007: built, tested, and unreachable. Neither is visible from inside the thing that is
wrong. Reading the workflow, three images publish correctly. Reading a manifest, `:v0.1.0` looks
like exactly the disciplined pin the comment claims. The defect only exists in the JOIN, which is
why it survived review for two phases and why it needs a check rather than a convention.

So this asserts the join: the set of (image, tag) a manifest can pin is a subset of the set CI
produces. It is L0 -- it reads the working tree and nothing else, no registry and no network -- so
it fails on the PR that introduces the drift rather than on the install that trips over it.

What it does NOT assert: that the image is actually present in GHCR right now. That depends on a
release having been cut upstream, is not a property of this tree, and cannot be checked offline.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The workflows that publish to GHCR -- the registry every first-party manifest reference names.
GHCR_PUBLISH = [
    REPO / ".github/workflows/docker-publish-ghcr.yml",
    REPO / ".github/workflows/docker-publish-k8s-operator.yml",
]
# The GAR mirror. No manifest references it, so it is not part of the reference join; it is checked
# only for parity, because a mirror missing three of seven images is a mirror that lies.
GAR_PUBLISH = REPO / ".github/workflows/docker-publish-gcp.yml"
# Build-only validation on PRs. An image published from main but built by no PR has no gate.
PR_BUILD = REPO / ".github/workflows/docker-build.yml"

WORKFLOW_FILES = set(GHCR_PUBLISH) | {GAR_PUBLISH, PR_BUILD}

# A reference to an image this repo is responsible for publishing. Deliberately anchored to the real
# registry+org: a personal Artifact Registry path (us-east4-docker.pkg.dev/<someone>/kube-agents/...)
# is a dev artifact whose provenance is the developer's own, not CI's.
FIRST_PARTY = re.compile(r"ghcr\.io/gke-labs/kube-agents/([a-z0-9][a-z0-9-]*)(?::([^\s\"'`,)]+))?")

# Every GHCR publish step must produce exactly this tag expression. Pinning the SCHEME rather than
# the resolved strings is what makes the check meaningful offline: KAGE_TAG is the git tag on a v*
# build and the commit sha on a main build, so "publishes ${{ env.KAGE_TAG }}" is precisely the
# statement "publishes the release tag and the sha, and nothing else".
KAGE_TAG_EXPR = "${{ env.KAGE_TAG }}"
PUBLISH_TAG_LINE = re.compile(
    r"^\s*tags:\s*ghcr\.io/\$\{\{ env\.IMAGE_REPOSITORY \}\}/([a-z0-9-]+):(.+?)\s*$"
)
COSIGN_GHCR = re.compile(r"cosign sign --yes \"ghcr\.io/\$\{\{ env\.IMAGE_REPOSITORY \}\}/([a-z0-9-]+)@")
COSIGN_GAR = re.compile(r"cosign sign --yes \"\$\{GCP_REGISTRY\}/([a-z0-9-]+)@")
PR_BUILD_TAG = re.compile(r"^\s*tags:\s*([a-z0-9-]+):ci\s*$", re.MULTILINE)

SHA40 = re.compile(r"^[0-9a-f]{40}$")
VERSION_TAG = re.compile(r"^v\d+\.\d+\.\d+")
# A tag that is a template hole, not a pin: "${AGENT_TAG}", "<YOUR-TAG>", "@@TAG@@". Nothing is
# being pinned, so there is nothing to resolve against the published set.
TEMPLATED = re.compile(r"[\$\{\}<>@]")
# The dev-loop tag `make cloud-build-push` prints. Only ever used against a personal registry, but
# it can appear alongside a ghcr.io path in prose describing the override.
LOCAL_DEV_TAG = re.compile(r"^src-")
# The BuildKit inline-cache tag. Published, deliberately mutable, never deployable.
CACHE_TAG = "buildcache"

COMMENT = re.compile(r"^\s*#")


def tags_env() -> dict[str, str]:
    values = {}
    for line in (REPO / "tags.env").read_text().splitlines():
        if COMMENT.match(line) or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


KAGE_IMAGE_VERSION = tags_env()["KAGE_IMAGE_VERSION"]


def scanned_files() -> list[Path]:
    """Every text file that could pin an image, minus the ones that define the answer.

    The workflows are excluded because they are the publisher, not a consumer -- counting their own
    output as a reference would make the check trivially self-satisfying. This file is excluded
    because it quotes the defect it forbids.

    `*_test.go` is excluded because a Go table-test's input literal is not a manifest: nothing
    applies it, and `resolveAgentImage` is being asked "given tag X, what do you join?" -- the
    answer has to be exercised with `:latest` precisely because a user may write that. Excluding
    the category would leave a hole where the REAL Go defaults live, so it does not:
    test_the_go_defaults_pin_the_release_tag pins those directly, by name.
    """
    out = []
    for p in REPO.rglob("*"):
        if not p.is_file() or p.resolve() == Path(__file__).resolve() or p in WORKFLOW_FILES:
            continue
        parts = p.parts
        if ".git" in parts or "node_modules" in parts or "bin" in parts:
            continue
        if p.name.endswith("_test.go"):
            continue
        if p.suffix not in {
            ".yaml", ".yml", ".go", ".sh", ".md", ".py", ".template", ".tmpl", ".env", ".json",
        } and p.name not in {"Makefile"}:
            continue
        out.append(p)
    return sorted(out)


def references() -> list[tuple[str, str | None, str]]:
    """Every first-party (image, tag, where) pinned anywhere in the tree.

    Handles the three shapes that occur. A joined `image: repo/name:tag`; the split form the Agent
    CRD uses, where `spec.deployment.image` and `spec.deployment.tag` are separate fields; and the
    kustomize `images:` pin, which spells the same split `newName:`/`newTag:`:

        image: "ghcr.io/gke-labs/kube-agents/cluster-admin-agent"     newName: ghcr.io/.../kage-router
        tag: "v0.1.0"                                                 newTag: v0.1.0

    Both split forms are what a naive grep for `name:tag` misses entirely, and between them they
    cover every shipped Agent CR, both skill templates, and the router's deployed tag. The kustomize
    one was found by a mutation: retagging config/router to :latest changed what `make deploy`
    actually pulls and the check said nothing.
    """
    found: list[tuple[str, str | None, str]] = []
    for path in scanned_files():
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(REPO))
        for i, line in enumerate(lines):
            for m in FIRST_PARTY.finditer(line):
                image, tag = m.group(1), m.group(2)
                if tag is None:
                    tag = _lookahead_tag(lines, i)
                found.append((image, tag, f"{rel}:{i + 1}"))
    return found


def _lookahead_tag(lines: list[str], i: int) -> str | None:
    """The `tag:`/`newTag:` sibling of a bare `image:`/`newName:` line in the same mapping.

    A `digest:` sibling returns None: a digest IS the immutable pin this check exists to encourage,
    it names a build directly rather than via a tag, so there is no tag to resolve.
    """
    if not re.match(r"^\s*(-\s+)?(image|newName):", lines[i]):
        return None
    indent = len(lines[i]) - len(lines[i].lstrip())
    for nxt in lines[i + 1 : i + 4]:
        if COMMENT.match(nxt) or not nxt.strip():
            continue
        if len(nxt) - len(nxt.lstrip()) != indent:
            return None
        m = re.match(r"^\s*(?:tag|newTag):\s*[\"']?([^\"'\s]+)[\"']?\s*$", nxt)
        return m.group(1) if m else None
    return None


def ghcr_published() -> dict[str, str]:
    """image name -> the tag expression its publish step produces."""
    published = {}
    for wf in GHCR_PUBLISH:
        for line in wf.read_text().splitlines():
            m = PUBLISH_TAG_LINE.match(line)
            if m:
                published[m.group(1)] = m.group(2)
    return published


def cosigned(pattern: re.Pattern[str], wf: Path) -> set[str]:
    return set(pattern.findall(wf.read_text()))


class TestImageProvenance(unittest.TestCase):
    def test_every_referenced_image_has_a_publish_step(self):
        """Half one: cluster-admin-agent, developer-team-agent and kage-router were built by nothing."""
        published = ghcr_published()
        self.assertTrue(published, "parsed no publish steps — the check would cover nothing")

        orphans = sorted({
            f"{image}  <- {where}"
            for image, _tag, where in references()
            if image not in published
        })
        self.assertEqual(
            orphans,
            [],
            "a manifest pins a first-party image that no workflow builds (V-CMP-002).\n"
            f"Published: {sorted(published)}\n"
            "Add a build+push step (and a cosign line, and a PR build) or stop referencing it:\n"
            + "\n".join(orphans),
        )

    def test_every_referenced_tag_is_a_tag_ci_produces(self):
        """Half two: everything pinned :v0.1.0 and CI published :latest and :${sha}."""
        published = ghcr_published()
        bad = []
        for image, tag, where in references():
            if tag is None or TEMPLATED.search(tag) or LOCAL_DEV_TAG.match(tag):
                continue
            if image not in published:
                continue  # already reported by the test above
            produced = published[image]
            if tag == "latest":
                bad.append(f"{where}: {image}:latest — no workflow publishes a :latest")
            elif tag == CACHE_TAG:
                bad.append(f"{where}: {image}:{CACHE_TAG} — that is a build cache, not a release")
            elif VERSION_TAG.match(tag):
                if tag != KAGE_IMAGE_VERSION:
                    bad.append(
                        f"{where}: {image}:{tag} — tags.env says KAGE_IMAGE_VERSION={KAGE_IMAGE_VERSION}"
                    )
                elif produced != KAGE_TAG_EXPR:
                    bad.append(f"{where}: {image}:{tag} — its publish step produces {produced}")
            elif SHA40.match(tag):
                if produced != KAGE_TAG_EXPR:
                    bad.append(f"{where}: {image}:{tag} — its publish step produces {produced}")
            else:
                bad.append(f"{where}: {image}:{tag} — unrecognised tag, cannot verify it is published")

        self.assertEqual(
            bad,
            [],
            "a first-party image is pinned to a tag no workflow produces (V-CMP-002).\n"
            "Publishing is: push to main -> :${github.sha}, push of v* -> the release tag.\n"
            + "\n".join(bad),
        )

    def test_no_publish_step_pushes_a_mutable_release_tag(self):
        """`:latest` in a shared registry, restated as a mechanism.

        Six comments across the tree promise "pinned to an immutable version tag (not :latest)".
        That promise was false in both directions at once — the version tag was not published and
        :latest was. This is the half that keeps :latest from coming back.
        """
        offenders = []
        for wf in [*GHCR_PUBLISH, GAR_PUBLISH]:
            for n, line in enumerate(wf.read_text().splitlines(), 1):
                if COMMENT.match(line):
                    continue
                if re.search(r":latest\b", line):
                    offenders.append(f"{wf.relative_to(REPO)}:{n}: {line.strip()[:90]}")
        self.assertEqual(
            offenders,
            [],
            "a publish workflow pushes :latest. Every published tag must be immutable — the sha on\n"
            "main, the release tag on a v* tag — so a deployed digest is always addressable:\n"
            + "\n".join(offenders),
        )

    def test_every_ghcr_publish_step_uses_the_release_tag_scheme(self):
        """The scheme is the thing being asserted, so the scheme itself has to be pinned."""
        published = ghcr_published()
        wrong = {i: t for i, t in published.items() if t != KAGE_TAG_EXPR}
        self.assertEqual(
            wrong, {}, f"publish steps must tag exactly '{KAGE_TAG_EXPR}', got: {wrong}"
        )

        for wf in GHCR_PUBLISH:
            text = wf.read_text()
            self.assertIn(
                'if [ "$REF_NAME" != "$KAGE_IMAGE_VERSION" ]',
                text,
                f"{wf.name} does not refuse a release tag that disagrees with tags.env — the tree\n"
                "and the registry could then name the same version for different builds",
            )

    def test_the_go_defaults_pin_the_release_tag(self):
        """The three per-tier fallbacks, pinned by name because *_test.go is exempt from the scan.

        These are what a user gets when an Agent CR omits spec.deployment.image, so they are the
        most consequential pins in the tree and the least visible -- no manifest shows them. Two of
        the three named images that no workflow built were named here.
        """
        src = (REPO / "k8s-operator/internal/controller/manifest_helpers.go").read_text()
        defaults = dict(
            re.findall(
                r'(default\w*AgentImage)\s*=\s*"ghcr\.io/gke-labs/kube-agents/[a-z-]+:([^"]+)"', src
            )
        )
        self.assertEqual(
            len(defaults), 3, f"expected 3 per-tier image defaults, found {sorted(defaults)}"
        )
        wrong = {k: v for k, v in defaults.items() if v != KAGE_IMAGE_VERSION}
        self.assertEqual(
            wrong,
            {},
            f"a tier default pins a tag other than KAGE_IMAGE_VERSION={KAGE_IMAGE_VERSION}: {wrong}",
        )

    def test_every_published_image_is_signed(self):
        built = set(ghcr_published())
        signed = set()
        for wf in GHCR_PUBLISH:
            signed |= cosigned(COSIGN_GHCR, wf)
        self.assertEqual(
            built - signed, set(), f"published but never cosigned: {sorted(built - signed)}"
        )
        self.assertEqual(
            signed - built, set(), f"cosigned but never built: {sorted(signed - built)}"
        )

    def test_the_gar_mirror_publishes_the_same_images_as_ghcr(self):
        ghcr = set(ghcr_published())
        gar = cosigned(COSIGN_GAR, GAR_PUBLISH)
        self.assertEqual(
            ghcr,
            gar,
            "the GAR mirror and GHCR publish different image sets, so 'use the GAR copy' is not a\n"
            f"safe instruction. only-GHCR={sorted(ghcr - gar)} only-GAR={sorted(gar - ghcr)}",
        )

    def test_every_published_image_is_built_on_pull_requests(self):
        """An image whose first build is the publish step has no PR that could catch it breaking.

        That is exactly how three images reached Phase 8 with targets nothing ever built.
        """
        built_on_pr = set(PR_BUILD_TAG.findall(PR_BUILD.read_text()))
        published = set(ghcr_published())
        self.assertEqual(
            published - built_on_pr,
            set(),
            f"published from main but never built on a PR: {sorted(published - built_on_pr)}",
        )

    def test_the_join_is_not_vacuous(self):
        """A check that cannot fail is not evidence (09 §6, V-MET-014).

        Three ways this could quietly stop checking anything: the reference scanner finds nothing,
        it misses the split image/tag form every Agent CR uses, or the tag classifier waves
        everything through as 'templated'. All three are pinned here.
        """
        refs = references()
        self.assertGreater(len(refs), 15, "reference scan collapsed — it should find dozens")

        images = {i for i, _t, _w in refs}
        for expected in ("platform-agent", "cluster-admin-agent", "developer-team-agent", "kage-router"):
            self.assertIn(expected, images, f"{expected} is referenced in the tree but not found")

        # The split form, from a real shipped Agent CR rather than a synthetic string.
        split = [
            (i, t, w)
            for i, t, w in refs
            if w.startswith("examples/gitops-repo/clusters/cluster-a/agents/agent.yaml")
        ]
        self.assertTrue(split, "the exemplar cluster-admin Agent CR was not scanned")
        self.assertEqual(
            split[0][1],
            KAGE_IMAGE_VERSION,
            "the split image:/tag: form did not resolve — every Agent CR would go unchecked",
        )

        # The kustomize newName:/newTag: form -- what `make deploy` actually pulls for the router.
        # A mutation (retag config/router to :latest) passed silently until this form was handled,
        # so it is pinned here rather than left to the next person to rediscover.
        kust = [
            (i, t, w)
            for i, t, w in refs
            if w.startswith("k8s-operator/config/router/kustomization.yaml")
        ]
        self.assertTrue(kust, "the router kustomization was not scanned")
        self.assertEqual(
            kust[0][1],
            KAGE_IMAGE_VERSION,
            "the kustomize newName:/newTag: form did not resolve — the router's deployed tag would "
            "go unchecked",
        )

        # And the classifier actually discriminates.
        self.assertTrue(VERSION_TAG.match("v0.1.0"))
        self.assertTrue(SHA40.match("a" * 40))
        self.assertTrue(TEMPLATED.search("${AGENT_TAG}"))
        self.assertFalse(TEMPLATED.search("v0.1.0"))
        self.assertFalse(TEMPLATED.search("latest"))


if __name__ == "__main__":
    unittest.main()
