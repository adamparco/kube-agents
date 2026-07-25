#!/usr/bin/env python3
"""Install-path wiring check — V-CMP-001 (Phase 8, P8-T3).

A numbered step script that no driver invokes is not part of the install. It
gets written, reviewed, tested in isolation, recorded green in the ledger, and
then does nothing forever, because the only thing that would have run it is a
line in `provision.sh` that nobody added.

This is LSN-007 ("built, tested, and unreachable"), and it recurred *inside the
unit that was fixing LSN-006*: P8-T2 shipped
`provision_13_apply_egress_policies.sh` — a real, correct, dry-run-clean step
that renders and applies the three per-tier egress policies — and never added it
to `provision.sh`. The ledger row for that unit says the policies are "applied
from an install path". They were not. Nothing caught it, because every check
pointed at the step script itself and no check pointed at the driver.

So this check does not look at any particular step. It enumerates the numbered
step scripts on disk and asserts the driver invokes each one. It is written to
catch the *next* orphan, not the one that motivated it — a check that only knows
about step 13 would have to be edited to notice step 14, and an edit is
something I could quietly skip.

Checks (all must pass for exit 0):

  1. **Every `provision_NN_*.sh` is invoked by `provision.sh`.**
  2. **Every `teardown_NN_*.sh` is invoked by `teardown.sh`.**
  3. **Every step the driver invokes exists on disk.** The mirror defect: a
     driver line naming a script that was renamed or removed. `provision.sh`
     runs under `set -e`, so this aborts the install partway, leaving a cluster
     in a state no teardown was written for.
  4. **Provision and teardown are symmetric.** Every `provision_NN` has a
     `teardown_NN` with a matching slug. An install step with no uninstall is a
     resource leak on every re-provision, and the asymmetry is invisible until
     someone tries to tear down.
  5. **Provision runs ascending, teardown runs descending.** The numbers encode
     a dependency order; teardown must unwind it. A teardown that removes the
     cluster before the workloads on it is not a teardown, it is a race.

Deliberately NOT checked: that a step does the right thing. This is reachability
only — the cheapest possible property, and the one that was false.

Negative control (`--self-test`): each check is re-run against a fixture that
reintroduces the defect it guards, and must fail. A check that cannot fail is
not evidence (09 §6, V-MET-014).

Usage:
    python3 local-dev/tests/install-path-wired.py [REPO_ROOT]
    python3 local-dev/tests/install-path-wired.py --self-test

Exit 0 = every step is reachable from a driver and the pair is symmetric;
1 = violations. Stdlib only, no cluster.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path("k8s-operator/scripts")

# provision_07_gcp_k8s_secrets.sh -> ("07", "gcp_k8s_secrets")
STEP = re.compile(r"^(provision|teardown)_(\d+)_(.+)\.sh$")


def steps(scripts: Path, kind: str) -> dict[str, str]:
    """{number: slug} for the numbered step scripts of one kind, on disk."""
    found = {}
    for path in sorted(scripts.glob(f"{kind}_*.sh")):
        m = STEP.match(path.name)
        if m and m.group(1) == kind:
            found[m.group(2)] = m.group(3)
    return found


def invoked(driver: Path, kind: str) -> list[tuple[str, str]]:
    """[(number, slug)] in the order the driver invokes them.

    Matches the script name anywhere on a line rather than parsing shell: the
    drivers wrap invocations in "${SCRIPT_DIR}/..." with trailing argument
    variables, and a real shell parser here would be a second implementation of
    bash to get wrong. A commented-out line is not an invocation, so those are
    dropped first — that is the one distinction worth making, because commenting
    a step out is precisely how a step goes dark.
    """
    order = []
    for line in driver.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(rf"{kind}_(\d+)_(.+?)\.sh", line):
            order.append((m.group(1), m.group(2)))
    return order


def check_pair(scripts: Path, driver: Path, kind: str, descending: bool) -> list[str]:
    bad = []
    if not driver.is_file():
        return [f"{driver.name}: missing — there is no install path at all"]

    on_disk = steps(scripts, kind)
    called = invoked(driver, kind)
    called_nums = [n for n, _ in called]

    if not on_disk:
        return [f"{scripts}/{kind}_NN_*.sh: none found — this check would pass vacuously"]

    # 1/2. Orphans: on disk, never invoked. The LSN-007 defect.
    for num, slug in sorted(on_disk.items()):
        if num not in called_nums:
            bad.append(
                f"{kind}_{num}_{slug}.sh exists but {driver.name} never invokes it — "
                f"the step is dead code and anything it was supposed to apply is not applied "
                f"(LSN-007)"
            )

    # 3. Dangling: invoked, not on disk.
    for num, slug in called:
        if on_disk.get(num) != slug:
            actual = on_disk.get(num)
            detail = (
                f"disk has {kind}_{num}_{actual}.sh — a rename that missed the driver"
                if actual
                else "no such file"
            )
            bad.append(
                f"{driver.name} invokes {kind}_{num}_{slug}.sh but {detail}. Under `set -e` "
                f"this aborts partway through."
            )

    # 5. Ordering. The numbers are a dependency order; teardown unwinds it.
    seen = [n for n in called_nums if n in on_disk]
    expected = sorted(seen, reverse=descending)
    if seen != expected:
        direction = "descending" if descending else "ascending"
        bad.append(
            f"{driver.name} invokes steps in the order {seen}, which is not {direction}. "
            f"The step numbers encode a dependency order and this one does not follow it."
        )
    return bad


def check_symmetry(scripts: Path) -> list[str]:
    prov, tear = steps(scripts, "provision"), steps(scripts, "teardown")
    bad = []
    for num, slug in sorted(prov.items()):
        if num not in tear:
            bad.append(
                f"provision_{num}_{slug}.sh has no teardown_{num}_*.sh — whatever it creates "
                f"survives `teardown.sh` and leaks into the next provision"
            )
        elif tear[num] != slug:
            bad.append(
                f"provision_{num}_{slug}.sh is paired with teardown_{num}_{tear[num]}.sh — "
                f"the slugs disagree, so one of the two was renamed alone"
            )
    for num, slug in sorted(tear.items()):
        if num not in prov:
            bad.append(f"teardown_{num}_{slug}.sh has no provision_{num}_*.sh to undo")
    return bad


def run_all(repo: Path) -> list[str]:
    scripts = repo / SCRIPTS
    if not scripts.is_dir():
        return [f"{SCRIPTS}: not a directory under {repo}"]
    return (
        check_pair(scripts, scripts / "provision.sh", "provision", descending=False)
        + check_pair(scripts, scripts / "teardown.sh", "teardown", descending=True)
        + check_symmetry(scripts)
    )


def _fixture(tmp: Path, prov: list[str], tear: list[str], pdrv: str, tdrv: str) -> Path:
    """A throwaway scripts/ tree, to prove each check can fail."""
    s = tmp / SCRIPTS
    s.mkdir(parents=True, exist_ok=True)
    for f in s.glob("*.sh"):
        f.unlink()
    for name in prov + tear:
        (s / name).write_text("#!/usr/bin/env bash\n")
    (s / "provision.sh").write_text(pdrv)
    (s / "teardown.sh").write_text(tdrv)
    return tmp


def self_test() -> int:
    import tempfile

    P = ["provision_01_a.sh", "provision_02_b.sh"]
    T = ["teardown_01_a.sh", "teardown_02_b.sh"]
    GOOD_P = '"${SCRIPT_DIR}/provision_01_a.sh"\n"${SCRIPT_DIR}/provision_02_b.sh"\n'
    GOOD_T = '"${SCRIPT_DIR}/teardown_02_b.sh"\n"${SCRIPT_DIR}/teardown_01_a.sh"\n'

    controls = [
        # The exact P8-T2 defect: the step exists, the driver stops one short.
        ("orphaned step rejected", P, T, '"${SCRIPT_DIR}/provision_01_a.sh"\n', GOOD_T),
        # Commenting a step out is how a live step goes dark without being deleted.
        (
            "commented-out step counts as orphaned",
            P,
            T,
            GOOD_P.replace('"${SCRIPT_DIR}/provision_02_b.sh"', '# "${SCRIPT_DIR}/provision_02_b.sh"'),
            GOOD_T,
        ),
        (
            "dangling driver reference rejected",
            P,
            T,
            GOOD_P + '"${SCRIPT_DIR}/provision_03_ghost.sh"\n',
            GOOD_T,
        ),
        (
            "rename that missed the driver rejected",
            ["provision_01_a.sh", "provision_02_renamed.sh"],
            T,
            GOOD_P,
            GOOD_T,
        ),
        (
            "provision with no teardown rejected",
            P + ["provision_03_c.sh"],
            T,
            GOOD_P + '"${SCRIPT_DIR}/provision_03_c.sh"\n',
            GOOD_T,
        ),
        (
            "teardown running in provision order rejected",
            P,
            T,
            GOOD_P,
            '"${SCRIPT_DIR}/teardown_01_a.sh"\n"${SCRIPT_DIR}/teardown_02_b.sh"\n',
        ),
        ("empty scripts tree rejected (no vacuous pass)", [], [], "", ""),
    ]

    failures = 0
    with tempfile.TemporaryDirectory() as td:
        # The fixture itself must be clean, or every control below "fires" for free.
        base = _fixture(Path(td), P, T, GOOD_P, GOOD_T)
        if run_all(base):
            print("  control DEAD: the clean fixture does not pass — controls prove nothing")
            return 1
        print("  fixture OK   (clean tree passes)")

        for name, prov, tear, pdrv, tdrv in controls:
            if run_all(_fixture(Path(td), prov, tear, pdrv, tdrv)):
                print(f"  control OK   (fires): {name}")
            else:
                print(f"  control DEAD (silent): {name}")
                failures += 1

    print(f"\n{len(controls) - failures}/{len(controls)} negative controls fire.")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    violations = run_all(repo)
    if violations:
        print("Install-path wiring violations:\n")
        for v in violations:
            print(f"  - {v}")
        print("\nA step script that no driver invokes is not installed. Wire it or retire it.")
        return 1
    print("Install path: OK — every numbered step is invoked by its driver, every driver")
    print("  reference resolves, provision/teardown are symmetric, and teardown unwinds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
