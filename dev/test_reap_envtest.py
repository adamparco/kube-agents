#!/usr/bin/env python3
"""Tests for the LSN-059 reaper, `dev/reap-envtest.sh`.

Real processes, not a parsed dry run. The reaper's two safety properties are both statements about
what it does to a live process table -- "only orphans" and "only under this root" -- and a test that
stubs `ps` proves them about the stub. So each case here spawns a decoy whose argv[0] is an absolute
path of the reaper's choosing, lets the script make its own decision, and then asks the kernel who
is still alive.

`bash -c 'exec -a "$0" sleep ...' <path>` is how a decoy gets an arbitrary argv[0] while being a
real, harmless `sleep`. It also means no file has to exist at that path, which matters: the reaper
must not be able to pass a case by stat-ing something the test conveniently created.

The reaper is only ever pointed at a temp root here. It is a script that kills processes and is
wired into `make test`; a test of it that swept the developer's real asset root would be a worse
bug than the one it closes.
"""

import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REAPER = _HERE / "reap-envtest.sh"


def _run(*args, cwd=None):
    return subprocess.run(
        ["bash", str(_REAPER), *args],
        capture_output=True,
        text=True,
        cwd=cwd or str(_HERE.parent),
    )


def _alive(pid: int) -> bool:
    """Live, and not a zombie.

    `kill(pid, 0)` is the obvious implementation and it is wrong here. A decoy whose parent is this
    test process stays in the table as a zombie until Python waits on it, and `kill -0` succeeds on
    a zombie -- so a correctly reaped process reads as still running. The state field is what
    distinguishes "the reaper failed" from "the test has not called wait() yet".
    """
    state = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _ppid(pid: int) -> int:
    out = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return int(out) if out else -1


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class _Decoys(unittest.TestCase):
    """Spawns fake control planes and guarantees they die with the test, reaped or not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name) / "bin" / "k8s"
        (self.root / "1.31.0-test").mkdir(parents=True)
        self._pids: list[int] = []
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap_decoys)
        self.addCleanup(self._tmp.cleanup)

    def _reap_decoys(self):
        # Belt and braces. A decoy that outlives a failing assertion is a `sleep 600` with an
        # argv[0] that looks like etcd, sitting on the developer's machine until they notice.
        for pid in self._pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for proc in self._procs:
            proc.wait()  # collect the zombie; an unwaited child is a ResourceWarning per test

    def _spawn(self, argv0: str, orphan: bool = False) -> int:
        """A `sleep` wearing `argv0`. `orphan=True` double-forks so init adopts it."""
        inner = 'exec -a "$0" sleep 600'
        if orphan:
            # `>/dev/null` on the BACKGROUND job, not on the outer shell: a decoy that inherits the
            # capture pipe holds it open for its full lifetime, and `subprocess.run` waits for EOF
            # rather than for exit. The first draft of this file deadlocked exactly there.
            proc = subprocess.run(
                ["bash", "-c", f"bash -c '{inner}' \"$1\" >/dev/null 2>&1 & echo $!", "_", argv0],
                capture_output=True,
                text=True,
                check=True,
            )
            pid = int(proc.stdout.strip())
        else:
            child = subprocess.Popen(
                ["bash", "-c", inner, argv0],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._procs.append(child)
            pid = child.pid
        self._pids.append(pid)
        # The process must be visible in `ps` with the argv we asked for before the reaper looks;
        # otherwise a pass means "the reaper found nothing", which is what every failure looks like.
        deadline = time.time() + 10
        while time.time() < deadline:
            out = subprocess.run(
                ["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True
            ).stdout
            if out.startswith(argv0):
                if not orphan or _ppid(pid) == 1:
                    return pid
            time.sleep(0.05)
        self.fail(f"decoy {pid} never appeared in ps as {argv0!r} (orphan={orphan})")

    def asset(self, name: str) -> str:
        return str(self.root / "1.31.0-test" / name)


class Refusals(_Decoys):
    """A prefix match is only as safe as its prefix, so a bad prefix is refused, not interpreted."""

    def test_filesystem_root_is_refused(self):
        r = _run("--dir", "/")
        self.assertEqual(2, r.returncode, r.stderr)
        self.assertIn("REFUSING", r.stderr)

    def test_a_two_component_path_is_refused(self):
        # `/usr/bin` would put every system daemon in scope of a script whose job is to kill what
        # is in scope.
        r = _run("--dir", "/usr/bin")
        self.assertEqual(2, r.returncode, r.stderr)
        self.assertIn("too few path components", r.stderr)

    def test_a_relative_root_is_refused(self):
        # A relative prefix means something different in every recipe that runs this, and the
        # difference is which processes die.
        r = _run("--dir", "k8s-operator/bin/k8s")
        self.assertEqual(2, r.returncode, r.stderr)
        self.assertIn("not absolute", r.stderr)

    def test_an_unknown_flag_is_refused_rather_than_ignored(self):
        # An ignored flag is the dangerous direction: `--dry-run` silently becomes a real reap.
        r = _run("--dry-run", "--dir", str(self.root))
        self.assertEqual(2, r.returncode, r.stderr)
        self.assertIn("unknown argument", r.stderr)

    def test_dir_without_a_value_is_refused(self):
        r = _run("--dir")
        self.assertEqual(2, r.returncode, r.stderr)


class EmptyScope(_Decoys):
    def test_an_empty_root_reaps_nothing_and_says_so(self):
        r = _run("--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("nothing to reap", r.stdout)

    def test_list_on_an_empty_root_exits_zero(self):
        # `--list` is the probe form: exit 1 means orphans exist. An empty root must not report one.
        r = _run("--list", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)

    def test_a_missing_root_is_not_an_error(self):
        # A fresh clone has no `bin/k8s` until `setup-envtest` runs, and the reaper is a
        # prerequisite of `test`. Failing here would fail the build for having nothing to do.
        r = _run("--dir", str(self.root / "does-not-exist" / "either"))
        self.assertEqual(0, r.returncode, r.stderr)


class OnlyOrphans(_Decoys):
    """The predicate that makes this safe to wire into `test`: a live parent is a live test run."""

    def test_a_parented_control_plane_is_left_alone(self):
        # This is the concurrent-`make test` case. Without it, wiring the sweep into `test` makes
        # two simultaneous runs kill each other -- a fix whose failure mode is worse than the leak.
        pid = self._spawn(self.asset("kube-apiserver"))
        self.assertNotEqual(1, _ppid(pid))
        r = _run("--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("nothing to reap", r.stdout)
        self.assertIn("1 in scope", r.stdout)
        self.assertTrue(_alive(pid), "the reaper killed a control plane whose parent is alive")

    def test_list_does_not_report_a_parented_control_plane(self):
        pid = self._spawn(self.asset("etcd"))
        r = _run("--list", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertTrue(_alive(pid))

    def test_all_reaps_a_parented_control_plane(self):
        # `--all` exists for "I know nothing is running, clear the machine", and is deliberately
        # not what the Makefile passes. Asserting it works is also how the arm above is proved to
        # be the predicate talking rather than the anchor.
        pid = self._spawn(self.asset("etcd"))
        r = _run("--all", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(_wait_gone(pid), r.stdout)

    def test_an_orphan_is_reaped(self):
        pid = self._spawn(self.asset("etcd"), orphan=True)
        r = _run("--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("reaped 1 envtest process", r.stdout)
        self.assertTrue(_wait_gone(pid), r.stdout)

    def test_list_reports_an_orphan_and_exits_one_without_killing_it(self):
        pid = self._spawn(self.asset("kube-apiserver"), orphan=True)
        r = _run("--list", "--dir", str(self.root))
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("orphaned envtest process", r.stdout)
        self.assertTrue(_alive(pid), "`--list` is the probe form and must never kill anything")

    def test_an_orphan_is_reaped_while_a_live_run_continues(self):
        # The two properties together, which is the only configuration the Makefile ever produces:
        # a previous run's leftovers and this run's control plane, in the process table at once.
        orphan = self._spawn(self.asset("etcd"), orphan=True)
        live = self._spawn(self.asset("etcd"))
        r = _run("--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(_wait_gone(orphan), r.stdout)
        self.assertTrue(_alive(live), "the live run's control plane was reaped")


class OnlyUnderTheRoot(_Decoys):
    """[[LSN-005]]'s rule -- anchored at the left edge -- applied to a process, not a cluster."""

    def test_a_process_outside_the_root_is_not_in_scope(self):
        # `pgrep etcd` would find this. It is somebody's real etcd, and the reaper runs from a
        # Makefile on every test run.
        outside = self._spawn(str(pathlib.Path(self._tmp.name) / "usr" / "local" / "bin" / "etcd"),
                              orphan=True)
        r = _run("--all", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("nothing to reap", r.stdout)
        self.assertTrue(_alive(outside))

    def test_a_sibling_directory_sharing_a_prefix_is_not_in_scope(self):
        # `<...>/bin/k8s-old` starts with `<...>/bin/k8s`. The trailing slash in the match is the
        # only thing standing between the two, and it is one character somebody could drop.
        sibling = self.root.parent / "k8s-old" / "1.31.0-test"
        sibling.mkdir(parents=True)
        pid = self._spawn(str(sibling / "etcd"), orphan=True)
        r = _run("--all", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(_alive(pid), "a sibling root sharing a name prefix was swept")

    def test_the_match_is_not_a_substring_match(self):
        # The root appears in this argv, but not at the left edge. A `grep`-shaped implementation
        # reaps it; an `index(...) == 1` one does not. The distinction is not academic: a shell
        # whose command line mentions the path is exactly this shape, which is how a hand-rolled
        # detector killed a live `make` earlier in this same unit.
        pid = self._spawn(f"/bin/echo {self.root}/1.31.0-test/etcd", orphan=True)
        r = _run("--all", "--dir", str(self.root))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(_alive(pid), "a process merely MENTIONING the asset root was reaped")


class DefaultRoot(unittest.TestCase):
    """The default scope is this repo's asset root, resolved from the script, not from `$PWD`."""

    def test_the_default_root_is_the_repo_asset_dir_regardless_of_cwd(self):
        # A reaper whose scope depends on where it was invoked from sweeps nothing when a Makefile
        # recipe runs it elsewhere -- and "swept nothing" and "nothing to sweep" print the same
        # line. Run from `/`, where a `$PWD`-relative implementation would resolve to `/k8s-...`.
        expected = _HERE.parent / "k8s-operator" / "bin" / "k8s"
        r = _run("--list", cwd="/")
        self.assertIn(str(expected), r.stdout + r.stderr)
        self.assertIn(r.returncode, (0, 1), r.stderr)


if __name__ == "__main__":
    unittest.main()
