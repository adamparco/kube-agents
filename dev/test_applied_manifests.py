#!/usr/bin/env python3
"""No manifest the install path applies may carry a fake value (V-CMP-003, L0).

The rule is narrow on purpose: **a placeholder token is forbidden in a manifest
something applies, and fine everywhere else.** Examples, docs and templates are
supposed to say `REPLACE_WITH_PROJECT_ID` — that is the token doing its job. The
defect is a placeholder in `k8s-operator/config/`, which `make deploy` kustomizes
straight onto a cluster.

Why it earns its own check rather than a code review. P8-T4 found
`config/router/deployment.yaml` shipping `KAGE_PROJECT_ID: "REPLACE_WITH_PROJECT_ID"`
with `replicas: 1`, and the local dev cluster's router had crash-looped **414 times**
against it. Nobody had missed a review; the provisioning script explicitly repairs
this env at step 5. The manifest was only wrong in the window between `make deploy`
and that step — and permanently wrong for anyone who runs `make deploy` alone, which
is what the developer docs tell you to run.

What made it expensive was not the outage but the ERROR MESSAGE. A placeholder is
syntactically valid, so it does not fail at admission or at parse; it flows into the
client library and fails as `credentials: could not find default credentials`. Every
reader of that log goes looking for a missing service-account key. The value was
right there in the pod spec and the failure pointed somewhere else entirely.

So the fix was to make the field empty rather than fake, and this is the check that
keeps it that way.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Directories whose contents get applied to a cluster as-is. `config/` is kustomize
# input for `make deploy`; `deploy/` holds the rendered shared defaults.
APPLIED_ROOTS = [
    REPO / "k8s-operator" / "config",
    REPO / "deploy",
]

# Deliberately NOT scanned — a placeholder here is the feature:
#   examples/gitops-repo/  exemplars a human fills in before committing
#   k8s-operator/scripts/*.template  rendered with envsubst before apply
#   agents/*/skills/**/assets/  skill templates, covered by test_skill_templates.py
PLACEHOLDER = re.compile(r"REPLACE_WITH_[A-Z0-9_]*|<PLACEHOLDER>|PLACEHOLDER_[A-Z0-9_]+")

# A value the manifest actually sets, vs. a token named in a comment. `# REPLACE both
# values` is prose telling an operator what to do; `value: "REPLACE_WITH_X"` is a fake
# datum that reaches a running process. Only the latter is the defect.
COMMENT = re.compile(r"^\s*#")


def applied_manifests() -> list[Path]:
    found: list[Path] = []
    for root in APPLIED_ROOTS:
        if root.is_dir():
            found += [p for p in root.rglob("*.yaml") if p.is_file()]
            found += [p for p in root.rglob("*.yml") if p.is_file()]
    return sorted(found)


class TestNoPlaceholderReachesACluster(unittest.TestCase):
    def test_applied_manifests_carry_no_placeholder_value(self):
        manifests = applied_manifests()
        self.assertTrue(manifests, "no applied manifests found — this check covers nothing")

        offenders = []
        for path in manifests:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if COMMENT.match(line):
                    continue
                m = PLACEHOLDER.search(line)
                if m:
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {m.group(0)} — {line.strip()[:80]}")

        self.assertEqual(
            offenders,
            [],
            "a placeholder value is in a manifest the install path APPLIES (V-CMP-003).\n"
            "Leave the field empty instead: the process fails naming the variable, rather than\n"
            "failing somewhere downstream with the placeholder disguised as real data.\n"
            + "\n".join(offenders),
        )

    def test_the_scanner_distinguishes_a_value_from_a_comment(self):
        """A check that cannot fail is not evidence (09 §6, V-MET-014).

        Skipping comments is the exemption that could quietly hollow this out, so
        the two shapes are pinned: a set value is caught, the prose that tells an
        operator to set it is not.
        """
        self.assertTrue(PLACEHOLDER.search('              value: "REPLACE_WITH_PROJECT_ID"'))
        self.assertTrue(PLACEHOLDER.search("  projectId: PLACEHOLDER_PROJECT"))
        self.assertTrue(COMMENT.match("            # REPLACE both values for the target environment."))
        self.assertFalse(COMMENT.match('            - name: KAGE_PROJECT_ID'))

    def test_the_router_env_is_empty_not_fake(self):
        """The specific regression, pinned to the specific file.

        The general scan above would also catch this, but only while the token keeps
        its current spelling. This asserts the shape the fix chose — empty string,
        both vars, still present so `kubectl set env` has something to overwrite.
        """
        router = REPO / "k8s-operator/config/router/deployment.yaml"
        lines = router.read_text().splitlines()
        text = "\n".join(lines)
        for var in ("KAGE_PROJECT_ID", "KAGE_INBOUND_SUBSCRIPTION"):
            self.assertIn(f"- name: {var}", text, f"{var} was removed from the router Deployment")
            i = lines.index(f"            - name: {var}")
            self.assertEqual(
                lines[i + 1].strip(),
                'value: ""',
                f"{var} no longer renders an empty value",
            )
        # Comments excluded for the same reason the general scan excludes them: the note
        # explaining why this field is empty has to name the token it replaced, and a check
        # that forbids naming it would force the explanation out — losing the one thing that
        # stops someone helpfully filling the blank back in.
        code = [ln for ln in lines if not COMMENT.match(ln)]
        self.assertEqual(
            [ln for ln in code if "REPLACE_WITH" in ln], [], "the router placeholder is back"
        )


if __name__ == "__main__":
    unittest.main()
