#!/usr/bin/env python3
"""Unit tests for the session-inject seam authorization core (Phase 4 / P4-T1, S1).

These are deliberately dependency-free (stdlib unittest only) so the security decision is
provable in any environment, including the offline build inner loop where FastAPI is not
installed.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import inject_auth

KEY = "s3cret-per-pod-key"


class TestCheckInjectAuth(unittest.TestCase):
    def test_disabled_when_no_expected_key(self):
        # Empty expected key => seam open (dev/test); any/None headers accepted.
        self.assertIsNone(inject_auth.check_inject_auth(None, None, ""))
        self.assertIsNone(inject_auth.check_inject_auth("Bearer whatever", "platform", ""))

    def test_missing_authorization_rejected(self):
        err = inject_auth.check_inject_auth(None, "platform", KEY)
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 401)

    def test_malformed_authorization_rejected(self):
        # No "Bearer " prefix.
        err = inject_auth.check_inject_auth(KEY, "platform", KEY)
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 401)

    def test_wrong_token_rejected(self):
        err = inject_auth.check_inject_auth("Bearer not-the-key", "platform", KEY)
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 401)

    def test_correct_token_accepted(self):
        self.assertIsNone(inject_auth.check_inject_auth(f"Bearer {KEY}", "platform", KEY))

    def test_correct_token_accepted_no_owner_allowlist(self):
        # No allow-list configured => owner header is not checked.
        self.assertIsNone(inject_auth.check_inject_auth(f"Bearer {KEY}", None, KEY, []))

    def test_owner_allowlist_allows_member(self):
        self.assertIsNone(
            inject_auth.check_inject_auth(f"Bearer {KEY}", "cluster-admin", KEY, ["cluster-admin"])
        )

    def test_owner_allowlist_rejects_non_member(self):
        err = inject_auth.check_inject_auth(
            f"Bearer {KEY}", "developer-team", KEY, ["platform", "cluster-admin"]
        )
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 403)

    def test_owner_allowlist_rejects_missing_caller(self):
        err = inject_auth.check_inject_auth(f"Bearer {KEY}", None, KEY, ["platform"])
        self.assertIsNotNone(err)
        self.assertEqual(err[0], 403)


class TestEnvHelpers(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (inject_auth.ENV_API_KEY, inject_auth.ENV_ALLOWED_OWNERS)}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_expected_api_key_default_empty(self):
        self.assertEqual(inject_auth.expected_api_key(), "")

    def test_expected_api_key_reads_env(self):
        os.environ[inject_auth.ENV_API_KEY] = KEY
        self.assertEqual(inject_auth.expected_api_key(), KEY)

    def test_allowed_owners_default_empty(self):
        self.assertEqual(inject_auth.allowed_owners(), [])

    def test_allowed_owners_parses_and_trims(self):
        os.environ[inject_auth.ENV_ALLOWED_OWNERS] = " platform , cluster-admin ,, "
        self.assertEqual(inject_auth.allowed_owners(), ["platform", "cluster-admin"])


if __name__ == "__main__":
    unittest.main()
