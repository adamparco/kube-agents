#!/usr/bin/env bash
# Reap orphaned envtest control planes — LSN-059.
#
# The lesson it closes: `make -C k8s-operator test` starts an etcd AND a kube-apiserver per test
# BINARY -- fourteen `*_envtest_test.go` packages, run in parallel by `go test` -- and stops them in
# TestMain after `m.Run()` returns. Any hard kill of the test process skips that line, and launchd
# (or init) adopts the children. Nothing ever reaps them. Measured on the dev laptop on 2026-07-30
# with no test run in flight: 16 etcd + 16 kube-apiserver, 30 of them at `ppid=1`, holding 1375 MB
# on a 16 GB machine, the oldest ~31 hours old. They arrived in cohorts matching test runs.
#
# Why this is a HARNESS defect and not only a test defect: the harness is the thing killing those
# processes. A time-bounded caller SIGKILLs `go test`, one control plane per package is abandoned,
# the machine gets slower, a slower machine is likelier to hit the same bound, and the next run
# abandons another cohort. The loop is self-reinforcing in the wrong direction, and [[LSN-058]] is
# the standing proof that this class of interference does not stay quiet -- it produced a red naming
# a file nobody wrote.
#
# You cannot trap a SIGKILL, so the fix cannot live in teardown. It has to be a SWEEP, and the sweep
# has to run at the START of the next run -- that is the one moment guaranteed to happen after an
# abandoned cohort exists. `k8s-operator/Makefile`'s `test` target therefore takes this as a
# prerequisite AND traps it on EXIT/INT/TERM. The prerequisite is the load-bearing half: it bounds
# accumulation at one run's worth no matter how the previous run died. The trap is the tidy half and
# covers every death except SIGKILL.
#
# TWO PROPERTIES MAKE THIS SAFE TO RUN AS A PREREQUISITE OF EVERY TEST RUN, and both are the point:
#
#   1. DISCOVERY IS ANCHORED AT THE LEFT EDGE OF AN ABSOLUTE PATH. A process is in scope iff its
#      argv[0] begins with the envtest asset root. This is LSN-005's rule -- the destructive guard
#      that matched a context name by SUBSTRING and would have accepted `prod-scratchpad` -- applied
#      to a process instead of a cluster. A name match (`pgrep etcd`) would reap the etcd somebody
#      is running for real work, and it would do it from a Makefile, silently, on every test run.
#
#   2. ONLY ORPHANS ARE REAPED. The default predicate is `ppid == 1`, which is precisely and only
#      the leak: a control plane whose parent is alive is somebody's test run in flight, including a
#      CONCURRENT `make test` in another terminal. Without this, wiring the sweep into `test` would
#      make two simultaneous runs kill each other -- a fix whose failure mode is worse than the leak.
#
# `--all` drops property 2 and is deliberately not what the Makefile uses. It exists for the "I know
# nothing is running, clear the machine" case, and it says so before it does it.
#
# Exit codes:
#   0  nothing in scope was found, or (default mode) everything found was reaped
#   1  `--list` only: orphans exist. This is the probe form; it never kills anything
#   2  refused -- bad usage, or an asset root too broad to sweep safely
#
# Usage:
#   bash dev/reap-envtest.sh                      # reap orphans under the default asset root
#   bash dev/reap-envtest.sh --list               # report only; exit 1 if any orphan exists
#   bash dev/reap-envtest.sh --all                # also reap live control planes (kills test runs)
#   bash dev/reap-envtest.sh --dir /path/to/k8s   # a different asset root (the Makefile passes one)

set -euo pipefail

# The default is resolved from THIS SCRIPT's location, never from `$PWD`. A reaper whose scope
# depends on where it was invoked from is a reaper that silently sweeps nothing when a Makefile
# recipe runs it with a different working directory -- and "swept nothing" and "nothing to sweep"
# print the same line.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ROOT="${REPO_ROOT}/k8s-operator/bin/k8s"

MODE=reap        # reap | list | all
TERM_GRACE=5     # seconds to wait for SIGTERM before SIGKILL

usage() {
	sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
	case "$1" in
	--list) MODE=list ;;
	--all) MODE=all ;;
	--dir)
		[ $# -ge 2 ] || { echo "REFUSING: --dir needs a path" >&2; exit 2; }
		ROOT="$2"
		shift
		;;
	--dir=*) ROOT="${1#--dir=}" ;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "REFUSING: unknown argument '$1'" >&2
		usage >&2
		exit 2
		;;
	esac
	shift
done

# A prefix match is only as safe as the prefix. `--dir /` or `--dir ''` would put every process on
# the machine in scope, and this script's whole job is to kill what is in scope. Refuse rather than
# interpret: an absolute path with at least three components is the narrowest rule that admits every
# real asset root (`<repo>/k8s-operator/bin/k8s`, and a single version subdirectory beneath it) and
# admits no filesystem root, no `/usr`, and no relative path whose meaning depends on the caller.
case "$ROOT" in
/*) : ;;
*)
	echo "REFUSING: asset root '$ROOT' is not absolute. A relative prefix means something" >&2
	echo "  different in every recipe that runs this, and the difference is which processes die." >&2
	exit 2
	;;
esac
depth="$(printf '%s' "${ROOT%/}" | tr -cd '/' | wc -c | tr -d ' ')"
if [ "$depth" -lt 3 ]; then
	echo "REFUSING: asset root '$ROOT' has too few path components to be an envtest asset" >&2
	echo "  directory. Sweeping a prefix that shallow would put unrelated processes in scope." >&2
	exit 2
fi
ROOT="${ROOT%/}"

# `args` and not `comm`: on Linux `ps -o comm` is the basename truncated to 15 characters, so the
# path this whole script matches on would not be there at all. argv[0] is what envtest execs -- the
# absolute path out of KUBEBUILDER_ASSETS -- on both platforms.
#
# Field order is fixed here and read positionally below, so the parse cannot drift from the request.
snapshot() {
	ps -eo pid=,ppid=,etime=,rss=,args= 2>/dev/null || true
}

# Everything under the root, orphaned or not. `$ROOT/` with the trailing slash so a sibling
# directory named `k8s-old` can never be swept by a root of `k8s`.
in_scope() {
	snapshot | awk -v root="${ROOT}/" '
		{
			argv0 = $5
			if (index(argv0, root) == 1) { print $1, $2, $3, $4, argv0 }
		}'
}

selected() {
	if [ "$MODE" = all ]; then
		in_scope
	else
		# ppid == 1: the parent is gone. A live parent is a test run in flight.
		in_scope | awk '$2 == 1'
	fi
}

if [ ! -d "$ROOT" ]; then
	echo "reap-envtest: no asset root at ${ROOT} — nothing to sweep."
	exit 0
fi

rows="$(selected)"

if [ -z "$rows" ]; then
	scope_total="$(in_scope | wc -l | tr -d ' ')"
	if [ "$MODE" = list ]; then
		echo "reap-envtest: no orphaned control planes under ${ROOT} (${scope_total} in scope, all parented)."
	else
		echo "reap-envtest: nothing to reap under ${ROOT} (${scope_total} in scope, all parented)."
	fi
	exit 0
fi

count="$(printf '%s\n' "$rows" | wc -l | tr -d ' ')"
kb="$(printf '%s\n' "$rows" | awk '{s += $4} END {print s + 0}')"
mb=$((kb / 1024))

printf '%-8s %-6s %-12s %-9s %s\n' PID PPID ELAPSED RSS_MB BINARY
printf '%s\n' "$rows" | while read -r pid ppid etime rss argv0; do
	printf '%-8s %-6s %-12s %-9s %s\n' "$pid" "$ppid" "$etime" "$((rss / 1024))" "$(basename "$argv0")"
done

if [ "$MODE" = list ]; then
	echo "reap-envtest: ${count} orphaned envtest process(es) under ${ROOT}, holding ${mb} MB."
	echo "  Each one is a control plane whose \`go test\` was killed before TestMain could stop it."
	echo "  Reap them with: bash dev/reap-envtest.sh"
	exit 1
fi

if [ "$MODE" = all ]; then
	echo "reap-envtest: --all — reaping ALL ${count} envtest process(es) under ${ROOT}, including any"
	echo "  whose parent is alive. A \`make test\` running right now will fail."
fi

pids="$(printf '%s\n' "$rows" | awk '{print $1}')"
# SIGTERM first: etcd and kube-apiserver both handle it, and a clean stop releases the data
# directory under $TMPDIR instead of stranding it.
for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done

waited=0
while [ "$waited" -lt "$TERM_GRACE" ]; do
	still=0
	for pid in $pids; do
		if kill -0 "$pid" 2>/dev/null; then still=1; fi
	done
	[ "$still" -eq 0 ] && break
	sleep 1
	waited=$((waited + 1))
done

killed=0
for pid in $pids; do
	if kill -0 "$pid" 2>/dev/null; then
		kill -KILL "$pid" 2>/dev/null || true
		killed=$((killed + 1))
	fi
done

if [ "$killed" -gt 0 ]; then
	echo "reap-envtest: reaped ${count} envtest process(es) (~${mb} MB); ${killed} needed SIGKILL after ${TERM_GRACE}s."
else
	echo "reap-envtest: reaped ${count} envtest process(es) (~${mb} MB) on SIGTERM."
fi
exit 0
