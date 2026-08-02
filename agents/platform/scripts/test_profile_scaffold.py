"""Unit tests for profile_scaffold.overlay_template (the image -> PVC force-sync).

Run: python3 -m unittest agents.platform.scripts.test_profile_scaffold

This is the mechanism the entrypoint uses to keep an EXISTING platform profile
tracking the image. It replaced a `for f in ...; do [ -f ... ] && cp -f; done`
loop, which could only ever copy files: `[ -f ]` is false for a directory, so
cron/, skills/, and governance/ were silently skipped on every upgrade and the
agent shipped a CAPABILITIES.md describing machinery that was not there. The
directory cases below are that regression, not a formality.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_scaffold as ps  # noqa: E402

ITEMS = ("config.yaml", "SOUL.md", "AGENTS.md", "CAPABILITIES.md", "cron", "skills", "governance")


class OverlayTemplateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.template = root / "template"
        self.home = root / "home"
        for path in (self.template, self.home):
            path.mkdir()
        write(self.template / "SOUL.md", "image persona")
        write(self.template / "CAPABILITIES.md", "image capabilities")
        write(self.template / "cron" / "jobs.json", "{}")
        write(self.template / "skills" / "fleet-audit" / "SKILL.md", "image skill")
        write(self.template / "governance" / "compliance_audit_sop.md", "image sop")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_named_directories_are_overlaid_not_skipped(self):
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("{}", (self.home / "cron" / "jobs.json").read_text())
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())
        self.assertEqual("image sop", (self.home / "governance" / "compliance_audit_sop.md").read_text())

    def test_the_image_copy_wins_over_what_is_already_on_the_volume(self):
        write(self.home / "SOUL.md", "stale persona")
        write(self.home / "skills" / "fleet-audit" / "SKILL.md", "stale skill")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("image persona", (self.home / "SOUL.md").read_text())
        self.assertEqual("image skill", (self.home / "skills" / "fleet-audit" / "SKILL.md").read_text())

    def test_runtime_state_and_removed_entries_survive(self):
        # The overlay adds and overwrites; it never prunes. Per-profile runtime
        # state has to survive an upgrade, and the price of that guarantee is
        # that a skill withdrawn from the image also survives — a known limit
        # recorded at the sync site in deploy/shared/docker-entrypoint.sh.
        write(self.home / "USER.md", "operator notes")
        write(self.home / "skills" / "withdrawn" / "SKILL.md", "no longer in the image")
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertEqual("operator notes", (self.home / "USER.md").read_text())
        self.assertTrue((self.home / "skills" / "withdrawn" / "SKILL.md").exists())

    def test_an_item_the_template_does_not_ship_is_not_an_error(self):
        # config.yaml and AGENTS.md are named by the entrypoint but a template
        # need not carry every one of them.
        ps.overlay_template(self.home, self.template, None, ITEMS)
        self.assertFalse((self.home / "config.yaml").exists())
        self.assertFalse((self.home / "AGENTS.md").exists())

    def test_no_item_list_overlays_the_whole_template(self):
        ps.overlay_template(self.home, self.template)
        self.assertTrue((self.home / "cron" / "jobs.json").exists())
        self.assertTrue((self.home / "CAPABILITIES.md").exists())

    def test_a_missing_template_is_a_hard_error(self):
        with self.assertRaises(SystemExit):
            ps.overlay_template(self.home, self.template.parent / "absent", None, ITEMS)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
