#!/usr/bin/env python3
"""The project preflight, exercised against a project that will not answer (L0).

WHY THIS FILE EXISTS. `assert_project_capacity` is built entirely out of "could not read it —
proceed, and here is what to read INSTEAD if the create fails" branches. That design is LSN-027:
an unmeasurable resource must never send the reader to the wrong layer. On 2026-07-26, the first
real run of `dev/cluster/up.sh` proved not one of those branches was reachable.

The cause is a shell rule that is easy to state and easy to forget. Under `set -euo pipefail` —
which every script in `dev/cluster/` runs, and should — a bare

    x="$(some-command | head -1)"

whose command fails does not assign an empty string and carry on. The assignment inherits the
failure, `set -e` kills the CALLER, and the process exits with the tool's status. up.sh exited 2
having printed nothing after the APIs line: no refusal, no remediation, no clue which of four
probes died. A preflight that dies silently is strictly worse than no preflight, because now there
is a script to debug on top of whatever the reader came for.

The fix is `x="$(...)" || x=""`, which makes the assignment a compound command `set -e` does not
act on. It is one token, it is invisible in review, and deleting it restores the bug with no
symptom until the next time a probe legitimately fails — which by construction is the moment the
preflight matters most. Hence a test, and hence a test at L0: no project, no credentials, no
network, so it keeps running long after this substrate is replaced too.

The stub is the seam. Every probe shells out to `gcloud`, so a `gcloud` earlier on PATH is a whole
fake project, and the interesting scenarios are the ones no real project will produce on demand.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "dev/lib/substrate-capacity.sh"

# The library's ENTIRE external tool surface, plus the bash that runs it. `printf` and `command` are
# builtins; everything else substrate-capacity.sh invokes is here. PATH for these subprocesses is
# built from this list and nothing else.
#
# WHY A CURATED FARM AND NOT `/usr/bin:/bin`, measured 2026-07-26. Debian packages gcloud to
# /usr/bin/gcloud, and so does GitHub's ubuntu-latest runner. So the previous PATH --
# `{stub_dir}:/usr/bin:/bin`, written with a comment explaining that it keeps "a real gcloud further
# down the path" out -- kept nothing out on Linux. The `stub is None` scenario, whose entire subject
# is a host with NO gcloud on it, instead reached this harness's live project and printed its real
# CPU quota (32 used of 200, us-east4): an authenticated API call, from a file whose own docstring
# promises "no project, no credentials, no network", inside a chain whose header defines L0 as
# needing neither cluster nor network. macOS hid it for the entire life of the Mac-only inner loop
# for the dullest possible reason -- Homebrew and the SDK tarball both install gcloud OUTSIDE
# /usr/bin, so the scrub happened to work on exactly one platform, the one it was written on.
#
# The farm also makes the dependency set explicit rather than ambient: if the library grows a call
# to `sed`, these tests fail with `sed: command not found` instead of silently borrowing the host's.
LIB_TOOLS = ("bash", "awk", "cat", "grep", "head", "tr", "wc")

# The exact shape of the first-run defect: `gcloud compute regions describe` was passed `--filter`,
# which is a LIST flag, so gcloud rejected the arguments and exited 2 before printing any quota.
UNRECOGNISED_ARGS = 2


def gcloud_stub(**verdicts: object) -> str:
    """A fake gcloud. Keys are the leading subcommand words joined by '_'; values are (rc, stdout).

    Defaults to succeeding with empty output, so a scenario only has to name what it breaks.
    """
    # The subcommand words are QUOTED in the pattern. An unquoted space inside a `case` pattern is
    # a shell syntax error, and a stub that fails to parse exits 2 for every scenario -- which
    # reads, from the library's side, exactly like a project that answers nothing.
    arms = "\n".join(
        f"  '{' '.join(key.split('_'))}'*)\n"
        f'    printf \'%s\' "{out}"\n'
        f"    exit {rc} ;;"
        for key, (rc, out) in verdicts.items()  # type: ignore[misc]
    )
    # The `*)` default succeeds with empty output. That is right for a stub and wrong for a guard:
    # the anchored-arm rule (LSN-005) is about scripts that ACT on the match, not fakes that answer.
    return f'#!/bin/sh\ncase "$*" in\n{arms}\n  *) exit 0 ;;\nesac\n'


def _lib_env_knobs() -> set[str]:
    """Every environment variable the library reads, PARSED OUT OF THE LIBRARY.

    This scrub used to be `e.pop("PROJECT_ID")` and nothing else, while `substrate-capacity.sh`
    reads six: PROJECT_ID, REGION, AR_REPO, CAP_NEED_VCPU, CAP_REQUIRED_APIS, ALLOW_TIGHT_QUOTA.
    Measured before changing it, and stated as measured: the hole is LATENT, not live. Re-running
    the suite with `REGION=europe-west9` exported under the old scrub is still 9/9, because no
    scenario here asserts on the region today. It is closed now rather than when one does, because
    the failure it would produce -- a green on this Mac and a red on the runner, or worse the
    reverse -- is the one this whole file was just repaired for, one layer out.

    Parsed rather than listed for the reason the LSN-027 gate arm parses `# @covers:` instead of
    carrying a table: a hardcoded list goes stale silently the first time a knob is added, and the
    staleness looks exactly like a passing test.
    """
    knobs = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)(?::[-=]|-)", LIB.read_text()))
    if len(knobs) < 5:
        raise AssertionError(
            f"parsed only {len(knobs)} env knobs out of {LIB.name} ({sorted(knobs)}); it read six "
            f"when this was written. The parse broke, so the scrub is not scrubbing — failing "
            f"loudly rather than running a test whose inputs come partly from this shell."
        )
    return knobs


def run_preflight(stub: str | None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Source the library under `set -euo pipefail` and call it, exactly as up.sh does.

    The trailing marker is the entire point: the assertion is not only "the right message was
    printed", it is "control got past the function at all".
    """
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        if stub is not None:
            p = bindir / "gcloud"
            p.write_text(stub)
            p.chmod(0o755)
        script = (
            "set -euo pipefail\n"
            f'. "{LIB}"\n'
            "assert_project_capacity\n"
            "echo CALLER-SURVIVED\n"
        )
        # PATH is the stub directory plus a farm holding ONLY the tools in LIB_TOOLS, symlinked in
        # by name. See LIB_TOOLS for why "the system minimum" was not minimal enough.
        toolbin = Path(tmp) / "tools"
        toolbin.mkdir()
        for tool in LIB_TOOLS:
            real = shutil.which(tool)
            if real is None:
                raise AssertionError(
                    f"cannot build a sanitized PATH: {tool!r} is not on this host's PATH. "
                    f"Failing loudly rather than skipping — a skipped hermeticity test is the "
                    f"same green as a passing one (LSN-021)."
                )
            (toolbin / tool).symlink_to(real)

        e = dict(os.environ)
        for name in _lib_env_knobs():
            e.pop(name, None)
        e["PATH"] = f"{bindir}:{toolbin}"
        e.update(env or {})
        # The sanitization, asserted rather than assumed. It was an assumption for the life of this
        # file and the assumption was false on Linux; a comment describing an intended property is
        # the thing that failed here, so the property gets checked on every single call.
        reachable = shutil.which("gcloud", path=e["PATH"])
        expected = str(bindir / "gcloud") if stub is not None else None
        if reachable != expected:
            raise AssertionError(
                f"PATH leaks a gcloud this test did not put there: found {reachable!r}, "
                f"expected {expected!r}. Every scenario below would be measuring a real project."
            )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=e, timeout=60
        )


PROJECT_OK = {"config_get": (0, "adamparco-kage")}


class TestProbeFailuresDoNotAbortTheCaller(unittest.TestCase):
    """Each probe, broken on its own, must land on its own branch and let the caller continue."""

    def test_api_list_unreadable(self):
        r = run_preflight(gcloud_stub(**PROJECT_OK, services_list=(1, "")))
        self.assertEqual(r.returncode, 0, f"caller died: {r.stdout}{r.stderr}")
        self.assertIn("could not list enabled services", r.stdout)
        self.assertIn("CALLER-SURVIVED", r.stdout)

    def test_quota_probe_rejects_its_arguments(self):
        """The first-run defect itself, frozen as a regression test."""
        r = run_preflight(
            gcloud_stub(**PROJECT_OK, compute_regions_describe=(UNRECOGNISED_ARGS, ""))
        )
        self.assertEqual(r.returncode, 0, f"caller died: {r.stdout}{r.stderr}")
        self.assertIn("could not read CPUS", r.stdout)
        self.assertIn("CALLER-SURVIVED", r.stdout)

    def test_every_probe_unreadable_at_once(self):
        """A project that answers nothing but is otherwise present. Still not this script's call."""
        r = run_preflight(
            gcloud_stub(
                config_get=(0, "adamparco-kage"),
                services_list=(1, ""),
                compute_regions_describe=(1, ""),
            )
        )
        self.assertEqual(r.returncode, 0, f"caller died: {r.stdout}{r.stderr}")
        self.assertIn("CALLER-SURVIVED", r.stdout)
        # And it still says what it did not measure. A run where every probe was blind must not
        # read as a clean bill of health -- that is the LSN-027 generalization, and it is the only
        # thing this run is actually evidence about.
        self.assertIn("NOT checked:", r.stdout)


class TestRefusalsStillRefuse(unittest.TestCase):
    """The escape hatch must not have turned a refusal into a shrug."""

    def test_no_project_refuses_with_remediation(self):
        r = run_preflight(gcloud_stub(config_get=(1, "")))
        self.assertEqual(r.returncode, 2, f"expected refusal, got {r.returncode}: {r.stdout}")
        self.assertIn("no GCP project is set", r.stderr)
        self.assertIn("gcloud config set project", r.stderr)
        self.assertNotIn("CALLER-SURVIVED", r.stdout)

    def test_gcloud_absent_refuses_rather_than_dying_at_127(self):
        """No gcloud at all. The library is sourced before any tool check in some callers, so this
        path has to produce the refusal and not a bare `command not found` with rc 127."""
        r = run_preflight(None)
        self.assertEqual(r.returncode, 2, f"expected refusal, got {r.returncode}: {r.stdout}")
        self.assertIn("no GCP project is set", r.stderr)

    def test_missing_api_refuses_and_names_the_enable_command(self):
        r = run_preflight(
            gcloud_stub(**PROJECT_OK, services_list=(0, "compute.googleapis.com"))
        )
        self.assertEqual(r.returncode, 2, f"expected refusal, got {r.returncode}: {r.stdout}")
        self.assertIn("container.googleapis.com", r.stderr)
        self.assertIn("gcloud services enable", r.stderr)

    def test_quota_exhausted_refuses_and_does_not_offer_the_live_cluster(self):
        # 200 limit, 196 used -> 4 free, against a want of 8.
        r = run_preflight(
            gcloud_stub(**PROJECT_OK, compute_regions_describe=(0, "CPUS\t200\t196"))
        )
        self.assertEqual(r.returncode, 2, f"expected refusal, got {r.returncode}: {r.stdout}")
        self.assertIn("4 free vCPU", r.stderr)
        self.assertIn("pause.sh", r.stderr)
        # binding.md's ground rule, asserted rather than trusted to the prose: the remediation for
        # "the region is full" must never read as permission to shrink the live install.
        self.assertIn("Do NOT free capacity by touching platform-agent-host", r.stderr)
        self.assertIn("destructive-test target", r.stderr.replace("\n", " "))

    def test_registry_unreachable_refuses(self):
        r = run_preflight(
            gcloud_stub(
                **PROJECT_OK,
                compute_regions_describe=(0, "CPUS\t200\t0"),
                artifacts_repositories_describe=(1, ""),
            )
        )
        self.assertEqual(r.returncode, 2, f"expected refusal, got {r.returncode}: {r.stdout}")
        self.assertIn("Artifact Registry", r.stderr)


class TestTheIdiomIsStillThere(unittest.TestCase):
    """Static backstop. The behavioural tests above cover the probes that exist today; this one
    fails on a NEW probe written the old way, before it has a chance to be the thing that dies
    silently at 2am."""

    def test_no_bare_command_substitution_assignment(self):
        offenders = []
        for i, raw in enumerate(LIB.read_text().splitlines(), start=1):
            line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
            m = re.match(r'\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)="\$\(', line)
            if not m:
                continue
            # The escape may be on this line or, for a substitution spanning several, on the line
            # that closes it. Scan forward to the first line carrying the closing `)"`.
            j, text = i - 1, LIB.read_text().splitlines()
            while j < len(text) and not re.search(r'\)"', text[j]):
                j += 1
            tail = text[j] if j < len(text) else ""
            if not re.search(r'\)"\s*\|\|\s*' + re.escape(m.group(1)) + r'=', tail):
                offenders.append(f"{LIB.name}:{i}: {raw.strip()}")
        self.assertEqual(
            offenders,
            [],
            "command substitution assigned without the `|| var=` escape. Under `set -euo pipefail` "
            "a failing probe here kills the caller with no output instead of reaching its "
            '"could not read it" branch:\n  ' + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
