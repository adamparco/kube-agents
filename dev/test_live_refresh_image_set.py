#!/usr/bin/env python3
"""`live_refresh.sh` verifies exactly the image set `cloud-build-push` builds (L0).

`make live-refresh` is a join between two lists that live in different files and are maintained by
different edits:

  * the root Makefile's `cloud-build-push` recipe, which SUBMITS the builds -- three tiers derived
    from `agents/*/`, plus credential-proxy, k8s-operator, kage-router and replay-proxy;
  * `live_refresh.sh`'s `IMAGES` array, which is what it RESOLVES against Artifact Registry before
    pinning anything, and what it matches running containers against afterwards.

Drift between them is silent in the direction that matters. An image added to the Makefile and not
to the script is built and pushed and then never verified: it can be absent from the registry, or
the cluster can keep running the previous one, and the refresh still reports success -- the script
would confirm six of seven and print a green. That is LSN-007 (built, tested, unreachable) aimed at
the verification step itself, and `cloud-build-push` has already been wrong in exactly this
direction once: its own help text read "every first-party image" while omitting replay-proxy, the
one image with no local build path at all.

The script does assert this at runtime, before it builds. That check only fires when somebody runs
a refresh against the live install, which is the worst moment to discover it and the least likely
to be reached -- so the same property is asserted here, on every PR, with no cluster and no network.

What this does NOT assert: that either list is *correct*, i.e. that it names every first-party image
the repo produces. `test_image_provenance.py` owns that question against the publish workflows. This
one owns only the join.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MAKEFILE = REPO / "Makefile"
SCRIPT = REPO / "k8s-operator/scripts/live_refresh.sh"

# The tiers are submitted by `for target in $(AGENTS)`, where AGENTS is the basename of every
# agents/*/ directory, and the recipe appends "-agent" to form the image name. Deriving the set the
# same way the Makefile does is the point: hardcoding three names here would keep passing on the day
# a fourth tier is added and neither list is updated.
AGENT_DIRS = sorted(p.name for p in (REPO / "agents").iterdir() if p.is_dir())


def makefile_image_set() -> set[str]:
    """The images `cloud-build-push` submits, read from the recipe rather than assumed."""
    text = MAKEFILE.read_text()
    body = text.split("cloud-build-push:", 1)
    if len(body) != 2:
        raise AssertionError("no cloud-build-push target in the root Makefile")
    recipe = body[1].split("\nstatus:", 1)[0]

    images = set()
    # The loop over $(AGENTS): `submit "$$target-agent" ...`
    if re.search(r'submit\s+"\$\$target-agent"', recipe):
        images.update(f"{d}-agent" for d in AGENT_DIRS)
    # The explicitly-named submissions: `submit credential-proxy ...`
    for name in re.findall(r"^\s*submit\s+([a-z0-9][a-z0-9-]*)\s", recipe, re.MULTILINE):
        images.add(name)
    return images


def script_image_set() -> set[str]:
    """The contents of live_refresh.sh's IMAGES=( ... ) array."""
    text = SCRIPT.read_text()
    m = re.search(r"^IMAGES=\(\s*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise AssertionError("no IMAGES=( ... ) array in live_refresh.sh")
    return {
        line.strip()
        for line in m.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


class LiveRefreshImageSet(unittest.TestCase):
    def test_the_two_lists_are_the_same_set(self):
        built = makefile_image_set()
        verified = script_image_set()

        self.assertEqual(
            built,
            verified,
            "\n`make cloud-build-push` and live_refresh.sh disagree about the image set."
            f"\n  built but NOT verified: {sorted(built - verified) or 'none'}"
            f"\n  verified but NOT built: {sorted(verified - built) or 'none'}"
            "\nAn image in the first group is pushed and then never checked, so a refresh"
            "\nreports success while the cluster keeps running the previous one. An image in"
            "\nthe second group makes every refresh fail on a tag nothing produces.",
        )

    def test_the_set_is_not_vacuously_empty(self):
        # Both parsers key off text that a refactor could rename, and two empty sets compare equal.
        # A passing test that read nothing is the failure mode this file exists to prevent.
        built = makefile_image_set()
        self.assertGreaterEqual(
            len(built),
            len(AGENT_DIRS) + 1,
            f"parsed only {sorted(built)} out of the cloud-build-push recipe; the parser has"
            " stopped matching and this suite would pass against anything",
        )

    def test_every_tier_directory_is_covered(self):
        # provision_12 derives the child tiers from AGENT_IMAGE's registry and AGENT_TAG, so all
        # three move together on one tag. A tier whose image is not in the refresh set gets pinned
        # to a tag that was never built for it.
        verified = script_image_set()
        for tier in AGENT_DIRS:
            self.assertIn(
                f"{tier}-agent",
                verified,
                f"agents/{tier}/ exists but live_refresh.sh never verifies {tier}-agent;"
                " provision_12 will still pin it to the new tag",
            )


if __name__ == "__main__":
    unittest.main()
