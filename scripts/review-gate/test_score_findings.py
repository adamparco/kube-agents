#!/usr/bin/env python3
"""Unit tests for the review-gate scorer (Phase 5 / P5-T3) — the hermetic Accept-(a) proof.

Deliberately dependency-free (stdlib unittest only), matching the Phase-4 idiom, so the block
decision is provable offline / in-pod / in CI. `today` is injected so expiry tests are deterministic
(never touches the real clock).
"""

import datetime
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import score_findings as sf

TODAY = datetime.date(2026, 7, 24)


def _flat(*findings):
    """Wrap raw finding dicts in the aggregated skill shape and flatten (exercises flatten_findings)."""
    return sf.flatten_findings([{"agent": "review-security-k8s-pod", "findings": list(findings)}])


class TestNormalizeAndFingerprint(unittest.TestCase):
    def test_line_numbers_stripped_for_stability(self):
        # Same finding reported at different lines => same fingerprint.
        a = sf.fingerprint("rbac", "role.yaml", "verb '*' granted on line 42")
        b = sf.fingerprint("rbac", "role.yaml", "verb '*' granted on line 87")
        self.assertEqual(a, b)

    def test_colon_and_bare_number_forms_stripped(self):
        a = sf.fingerprint("rbac", "role.yaml", "wildcard verb at role.yaml:42")
        b = sf.fingerprint("rbac", "role.yaml", "wildcard verb at role.yaml:99")
        self.assertEqual(a, b)

    def test_different_file_differs(self):
        a = sf.fingerprint("rbac", "role-a.yaml", "verb '*' granted")
        b = sf.fingerprint("rbac", "role-b.yaml", "verb '*' granted")
        self.assertNotEqual(a, b)

    def test_different_agent_differs(self):
        a = sf.fingerprint("rbac", "x.yaml", "same message")
        b = sf.fingerprint("network", "x.yaml", "same message")
        self.assertNotEqual(a, b)

    def test_fingerprint_is_16_hex(self):
        fp = sf.fingerprint("a", "b", "c")
        self.assertEqual(len(fp), 16)
        int(fp, 16)  # raises if not hex


class TestSeverity(unittest.TestCase):
    def test_known_severities_pass_through(self):
        for s in ("critical", "high", "medium", "low"):
            self.assertEqual(sf.severity_of({"severity": s}), s)

    def test_case_insensitive(self):
        self.assertEqual(sf.severity_of({"severity": "HIGH"}), "high")

    def test_missing_treated_as_high(self):
        self.assertEqual(sf.severity_of({}), "high")

    def test_unknown_treated_as_high(self):
        self.assertEqual(sf.severity_of({"severity": "spicy"}), "high")


class TestScoreBlockRule(unittest.TestCase):
    def test_clean_passes(self):
        report = sf.score([], [], TODAY)
        self.assertFalse(report["blocked"])

    def test_unmitigated_high_blocks(self):
        findings = _flat({"message": "missing default-deny netpol", "file": "ns.yaml", "severity": "high"})
        report = sf.score(findings, [], TODAY)
        self.assertTrue(report["blocked"])
        self.assertEqual(len(report["blockers"]), 1)

    def test_critical_blocks(self):
        findings = _flat({"message": "cluster-admin binding", "file": "rb.yaml", "severity": "critical"})
        self.assertTrue(sf.score(findings, [], TODAY)["blocked"])

    def test_medium_low_advisory_only(self):
        findings = _flat(
            {"message": "missing readOnlyRootFilesystem", "file": "d.yaml", "severity": "medium"},
            {"message": "label nit", "file": "d.yaml", "severity": "low"},
        )
        report = sf.score(findings, [], TODAY)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(report["advisory"]), 2)

    def test_missing_severity_treated_as_high_and_blocks(self):
        findings = _flat({"message": "no severity here", "file": "x.yaml"})
        self.assertTrue(sf.score(findings, [], TODAY)["blocked"])


class TestWaivers(unittest.TestCase):
    def _high(self):
        return _flat({"message": "hostPath mount exposes node", "file": "ds.yaml", "severity": "high"})

    def test_valid_waiver_mitigates(self):
        findings = self._high()
        fp = sf.fingerprint("review-security-k8s-pod", "ds.yaml", "hostPath mount exposes node")
        waivers = [
            {
                "fingerprint": fp,
                "justification": "CSI daemonset requires it; read-only.",
                "approved_by": "adamparco",
                "expires": datetime.date(2026, 12, 31),
            }
        ]
        report = sf.score(findings, waivers, TODAY)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(report["waived"]), 1)

    def test_expired_waiver_blocks(self):
        findings = self._high()
        fp = sf.fingerprint("review-security-k8s-pod", "ds.yaml", "hostPath mount exposes node")
        waivers = [
            {
                "fingerprint": fp,
                "justification": "was fine last year",
                "approved_by": "adamparco",
                "expires": datetime.date(2026, 1, 1),  # < TODAY
            }
        ]
        self.assertTrue(sf.score(findings, waivers, TODAY)["blocked"])

    def test_waiver_expiring_today_still_active(self):
        findings = self._high()
        fp = sf.fingerprint("review-security-k8s-pod", "ds.yaml", "hostPath mount exposes node")
        waivers = [
            {"fingerprint": fp, "justification": "j", "approved_by": "a", "expires": TODAY}
        ]
        self.assertFalse(sf.score(findings, waivers, TODAY)["blocked"])

    def test_non_matching_waiver_does_not_mitigate(self):
        findings = self._high()
        waivers = [
            {"fingerprint": "deadbeefdeadbeef", "justification": "j", "approved_by": "a", "expires": TODAY}
        ]
        self.assertTrue(sf.score(findings, waivers, TODAY)["blocked"])


class TestParseWaivers(unittest.TestCase):
    def test_empty_inline_list(self):
        waivers, warnings = sf.parse_waivers("waivers: []\n")
        self.assertEqual(waivers, [])
        self.assertEqual(warnings, [])

    def test_full_entry_parses(self):
        text = (
            "# comment line\n"
            "waivers:\n"
            "  - fingerprint: 0123456789abcdef\n"
            "    justification: hostPath needed by CSI; scoped read-only.\n"
            "    approved_by: adamparco\n"
            "    expires: 2026-12-31\n"
        )
        waivers, warnings = sf.parse_waivers(text)
        self.assertEqual(warnings, [])
        self.assertEqual(len(waivers), 1)
        self.assertEqual(waivers[0]["fingerprint"], "0123456789abcdef")
        self.assertEqual(waivers[0]["expires"], datetime.date(2026, 12, 31))

    def test_two_entries(self):
        text = (
            "waivers:\n"
            "  - fingerprint: aaaaaaaaaaaaaaaa\n"
            "    justification: one\n"
            "    approved_by: a\n"
            "    expires: 2026-12-31\n"
            "  - fingerprint: bbbbbbbbbbbbbbbb\n"
            "    justification: two\n"
            "    approved_by: b\n"
            "    expires: 2027-01-01\n"
        )
        waivers, warnings = sf.parse_waivers(text)
        self.assertEqual(len(waivers), 2)
        self.assertEqual(warnings, [])

    def test_missing_field_ignored_with_warning(self):
        text = (
            "waivers:\n"
            "  - fingerprint: aaaaaaaaaaaaaaaa\n"
            "    justification: no approver or expiry\n"
        )
        waivers, warnings = sf.parse_waivers(text)
        self.assertEqual(waivers, [])
        self.assertEqual(len(warnings), 1)

    def test_bad_date_ignored_with_warning(self):
        text = (
            "waivers:\n"
            "  - fingerprint: aaaaaaaaaaaaaaaa\n"
            "    justification: bad date\n"
            "    approved_by: a\n"
            "    expires: not-a-date\n"
        )
        waivers, warnings = sf.parse_waivers(text)
        self.assertEqual(waivers, [])
        self.assertEqual(len(warnings), 1)

    def test_quoted_values_stripped(self):
        text = (
            "waivers:\n"
            '  - fingerprint: "aaaaaaaaaaaaaaaa"\n'
            "    justification: 'has: a colon inside'\n"
            "    approved_by: a\n"
            "    expires: 2026-12-31\n"
        )
        waivers, _ = sf.parse_waivers(text)
        self.assertEqual(waivers[0]["fingerprint"], "aaaaaaaaaaaaaaaa")
        self.assertEqual(waivers[0]["justification"], "has: a colon inside")

    def test_missing_file_text_is_empty(self):
        waivers, warnings = sf.parse_waivers("")
        self.assertEqual(waivers, [])


class TestFlattenShapes(unittest.TestCase):
    def test_nested_shape(self):
        raw = [{"agent": "rbac", "findings": [{"message": "m", "file": "f", "severity": "high"}]}]
        out = sf.flatten_findings(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["agent"], "rbac")

    def test_flat_shape(self):
        raw = [{"agent": "rbac", "message": "m", "file": "f", "severity": "high"}]
        out = sf.flatten_findings(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["message"], "m")

    def test_single_object(self):
        raw = {"agent": "rbac", "findings": [{"message": "m", "file": "f", "severity": "low"}]}
        self.assertEqual(len(sf.flatten_findings(raw)), 1)

    def test_empty(self):
        self.assertEqual(sf.flatten_findings([]), [])
        self.assertEqual(sf.flatten_findings(None), [])


class TestMainExitCodes(unittest.TestCase):
    def _run(self, findings_obj, waiver_text, tmp_path, today="2026-07-24"):
        fpath = tmp_path / "findings.json"
        wpath = tmp_path / "waivers.yaml"
        fpath.write_text(json.dumps(findings_obj), encoding="utf-8")
        wpath.write_text(waiver_text, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = sf.main([str(fpath), "--waivers", str(wpath), "--today", today])
        return code, buf.getvalue()

    def test_exit_1_on_unmitigated_high(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            code, out = self._run(
                [{"agent": "rbac", "findings": [{"message": "wildcard verb", "file": "r.yaml", "severity": "high"}]}],
                "waivers: []\n",
                Path(d),
            )
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)

    def test_exit_0_on_clean(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            code, out = self._run([], "waivers: []\n", Path(d))
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_exit_0_on_waived_high(self):
        import tempfile

        fp = sf.fingerprint("rbac", "r.yaml", "wildcard verb")
        waiver = (
            "waivers:\n"
            "  - fingerprint: {}\n"
            "    justification: mitigated by VAP\n"
            "    approved_by: adamparco\n"
            "    expires: 2026-12-31\n"
        ).format(fp)
        with tempfile.TemporaryDirectory() as d:
            code, out = self._run(
                [{"agent": "rbac", "findings": [{"message": "wildcard verb", "file": "r.yaml", "severity": "high"}]}],
                waiver,
                Path(d),
            )
        self.assertEqual(code, 0)
        self.assertIn("WAIVED", out)

    def test_fingerprint_mode_exit_0(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fpath = Path(d) / "f.json"
            fpath.write_text(
                json.dumps([{"agent": "rbac", "findings": [{"message": "m", "file": "f", "severity": "high"}]}]),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = sf.main([str(fpath), "--fingerprint"])
        self.assertEqual(code, 0)
        self.assertIn("high", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
