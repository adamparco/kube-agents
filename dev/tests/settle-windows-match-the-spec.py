#!/usr/bin/env python3
"""V-REV-012: the broker's settle windows are 04 §5.1's table, cell for cell.

04 §5.1 states the windows in the spec rather than leaving them to the implementation, and it says
why in the sentence above the table: *"'Bounded' on its own is unfalsifiable -- any number satisfies
it -- so the windows are stated here rather than left to the implementation."* That sentence is a
verification instruction. A check that asserts only "the window is bounded" is the check the section
was written to rule out; the numbers are the property.

Nothing else in the tree reads them. V-PRO-013 exercises §5.1's OTHER table -- the per-kind verified-
when predicates -- and it is deferred against a 09 §12 row besides; `predicate_test.go` asserts that
`SettleWindow` returns what `settleWindows` holds, which is a tautology over the map rather than a
comparison against the spec. So the eight window rows are asserted by nobody, and the failure mode
is the quiet one: somebody widens `NetworkPolicy` from 30s to 5m to stop a flaky test, every Go test
still passes because they all read the same map, and the spec now describes a system that does not
exist. The drift is invisible in both directions -- editing the doc is equally silent.

Five properties.

  1. NON-VACUITY. The spec table parses to exactly the eight labels this file binds, the code map
     parses to at least eleven entries, and all three constants are found. Every other property is a
     comparison, and a comparison over an empty set is the greenest thing in this repo.
  2. EVERY SPEC ROW MATCHES ITS CODE ENTRIES. A row naming N kinds carries either N durations,
     positionally, or one that applies to all N -- `Service / Ingress / Gateway | 90s / 5m / 5m` is
     the first shape and `ResourceQuota / LimitRange | 15s` is the second.
  3. NO CODE ENTRY IS UNACCOUNTED FOR. A kind given a window in Go with no row in 04 §5.1 is drift
     in the direction the spec cannot see: the section is meant to be the published list, and an
     unpublished window is exactly the "left to the implementation" the sentence forbids.
  4. THE CEILING IS THE SECTION'S NUMBER, AND IT BINDS. `MaxSettleWindow` equals the "No target
     waits longer than 30 minutes" sentence, and no table value exceeds it -- a table row above the
     ceiling is clamped at runtime, so the published number would be a lie the code silently
     corrects.
  5. THE DEFAULT IS THE SECTION'S NUMBER. `DefaultSettleWindow` equals the `Custom resource` row,
     which 04 §5.1 names as *"the default for any kind with no row of its own"*.

WHAT THIS CHECK DOES NOT ASSERT, deliberately: that the numbers are RIGHT. 09 §12 row T-9 is open
and the code comment beside the table says so in as many words -- *"these are the harness's working
values, not a resolution of T-9"*. Two artifacts agreeing is the whole property; if T-9 resolves to
different numbers, both move together and this check is what makes that a single edit rather than a
silent divergence.

Run:  python3 dev/tests/settle-windows-match-the-spec.py
      python3 dev/tests/settle-windows-match-the-spec.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "design" / "04-workflow-model.md"
CODE = REPO / "k8s-operator" / "internal" / "broker" / "verify" / "predicate.go"

# The `Kind` cell of 04 §5.1's window table, bound to what each label means in code. The BINDING is
# hardcoded; the NUMBERS are not, and the numbers are what drifts. "Node pool / cluster (cloud)"
# does not appear anywhere in the Go source, so something has to say it means the two Config
# Connector kinds -- and a binding stated once here, asserted total in both directions by properties
# 1 and 3, is a smaller surface than a third artifact holding the mapping.
#
# Two labels bind to a constant rather than to map entries: `RBAC` is keyed by kind alone in
# `SettleWindow`'s fallback (any group), and `Custom resource` IS the default.
RBAC = "@rbacSettleWindow"
DEFAULT = "@DefaultSettleWindow"

SPEC_ROWS: dict[str, list[object]] = {
    "Deployment / StatefulSet": [("apps", "Deployment"), ("apps", "StatefulSet")],
    "DaemonSet": [("apps", "DaemonSet")],
    "Service / Ingress / Gateway": [
        ("", "Service"),
        ("networking.k8s.io", "Ingress"),
        ("gateway.networking.k8s.io", "Gateway"),
    ],
    "NetworkPolicy": [("networking.k8s.io", "NetworkPolicy")],
    "ResourceQuota / LimitRange": [("", "ResourceQuota"), ("", "LimitRange")],
    "Node pool / cluster (cloud)": [
        ("container.cnrm.cloud.google.com", "ContainerNodePool"),
        ("container.cnrm.cloud.google.com", "ContainerCluster"),
    ],
    "RBAC": [RBAC],
    "Custom resource": [DEFAULT],
}

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
GO_UNIT_SECONDS = {"Second": 1, "Minute": 60, "Hour": 3600}

WINDOW_TABLE_HEADER = re.compile(r"^\|\s*Kind\s*\|\s*Settle window\s*\|\s*$", re.M)
TABLE_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
DURATION = re.compile(r"\b(\d+)\s*([smh])\b")
CEILING = re.compile(r"No target waits longer than (\d+) minutes")

GO_CONST = re.compile(r"^const (\w+) = (\d+) \* time\.(Second|Minute|Hour)\s*$", re.M)
GO_MAP = re.compile(
    r"^var settleWindows = map\[classify\.KindRef\]time\.Duration\{\n(.*?)^\}", re.S | re.M
)
GO_ENTRY = re.compile(
    r'^\s*\{Group:\s*"([^"]*)",\s*Kind:\s*"([^"]+)"\}:\s*(\d+)\s*\*\s*time\.(Second|Minute|Hour),',
    re.M,
)


def human(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def parse_spec(text: str) -> tuple[dict[str, list[int]], int | None]:
    """The window table's rows as label -> [seconds], plus the ceiling sentence's number.

    The commentary after an em dash is dropped before durations are read: `5m / 10m -- a StatefulSet
    rolls one pod at a time` must yield two numbers, and `90s / 5m / 5m -- the two 5m rows are LB
    programming` must yield three rather than five.
    """
    rows: dict[str, list[int]] = {}
    m = WINDOW_TABLE_HEADER.search(text)
    if m:
        for line in text[m.end() :].lstrip("\n").splitlines():
            if not line.startswith("|"):
                break
            cells = TABLE_ROW.match(line)
            if not cells:
                break
            label, value = cells.group(1).strip(), cells.group(2)
            if set(label) <= {"-", ":"}:
                continue  # the |---|---| separator
            value = re.split(r"—|--", value)[0]
            rows[label] = [int(n) * UNIT_SECONDS[u] for n, u in DURATION.findall(value)]
    ceiling = CEILING.search(text)
    return rows, int(ceiling.group(1)) * 60 if ceiling else None


def parse_code(text: str) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """The `settleWindows` map as (group, kind) -> seconds, plus the named constants."""
    consts = {name: int(n) * GO_UNIT_SECONDS[u] for name, n, u in GO_CONST.findall(text)}
    entries: dict[tuple[str, str], int] = {}
    body = GO_MAP.search(text)
    if body:
        for group, kind, n, unit in GO_ENTRY.findall(body.group(1)):
            entries[(group, kind)] = int(n) * GO_UNIT_SECONDS[unit]
    return entries, consts


def check(spec_text: str, code_text: str) -> list[str]:
    rows, ceiling = parse_spec(spec_text)
    entries, consts = parse_code(code_text)

    # --- 1. non-vacuity ---------------------------------------------------------------------
    vacuous: list[str] = []
    missing_labels = [k for k in SPEC_ROWS if k not in rows]
    if missing_labels:
        vacuous.append(
            f"VACUOUS: 04 §5.1's settle-window table did not yield {missing_labels} -- "
            f"parsed {sorted(rows)}. Every property below is a comparison, and a comparison over a "
            f"row that was not read passes silently."
        )
    extra_labels = [k for k in rows if k not in SPEC_ROWS]
    if extra_labels:
        vacuous.append(
            f"04 §5.1's window table has row(s) {extra_labels} that this check does not bind to any "
            f"kind, so their numbers are asserted by nothing. Add the binding to SPEC_ROWS -- a new "
            f"published window that no code entry has to match is the same hole as an unpublished one."
        )
    # There is deliberately NO floor on len(entries). It would guard nothing here and would cost a
    # property: a map that stops parsing yields zero entries, and property 2 then reports every one
    # of the eleven published kinds as having no entry -- eleven findings naming the kinds, which is
    # louder and better attributed than a count. Set at the map's real size, a floor would also make
    # property 2's `falls through to the default` branch unreachable, since deleting any single
    # entry would trip the floor first. A whole branch that no control can exercise reads as
    # protection and is not.
    for const in ("MaxSettleWindow", "DefaultSettleWindow", "rbacSettleWindow"):
        if const not in consts:
            vacuous.append(
                f"VACUOUS: {CODE.relative_to(REPO)} declares no `{const}` this check can read, so "
                f"the value 04 §5.1 publishes for it is compared against nothing."
            )
    if ceiling is None:
        vacuous.append(
            "VACUOUS: 04 §5.1's 'No target waits longer than N minutes' sentence did not parse, so "
            "property 4 has no number to hold the code to."
        )
    if vacuous:
        return vacuous

    findings: list[str] = []

    # --- 2. every spec row matches its code entries -----------------------------------------
    bound: set[tuple[str, str]] = set()
    for label, targets in SPEC_ROWS.items():
        values = rows[label]
        if len(values) == 1 and len(targets) > 1:
            values = values * len(targets)
        if len(values) != len(targets):
            findings.append(
                f"04 §5.1's `{label}` row names {len(targets)} kind(s) but carries "
                f"{len(values)} duration(s) ({[human(v) for v in values]}). A row is read "
                f"positionally, or as one value for all of them; neither reading fits."
            )
            continue
        for target, want in zip(targets, values):
            if target == RBAC:
                got, where = consts["rbacSettleWindow"], "rbacSettleWindow"
            elif target == DEFAULT:
                got, where = consts["DefaultSettleWindow"], "DefaultSettleWindow"
            else:
                bound.add(target)
                if target not in entries:
                    findings.append(
                        f"04 §5.1 publishes {human(want)} for `{label}` "
                        f"(group={target[0]!r} kind={target[1]!r}), and settleWindows has no entry "
                        f"for it -- so that target falls through to the "
                        f"{human(consts['DefaultSettleWindow'])} default and the published number "
                        f"is not the one the broker waits."
                    )
                    continue
                got, where = entries[target], f"settleWindows[{target[0] or '(core)'}/{target[1]}]"
            if got != want:
                findings.append(
                    f"04 §5.1 publishes {human(want)} for `{label}` but {where} is {human(got)}. "
                    f"The section states these numbers precisely so they are falsifiable; the two "
                    f"artifacts have to move together or the spec describes a system that is not "
                    f"running."
                )

    # --- 3. no code entry is unaccounted for -------------------------------------------------
    for target in sorted(entries):
        if target not in bound:
            findings.append(
                f"settleWindows gives group={target[0]!r} kind={target[1]!r} a window of "
                f"{human(entries[target])} and 04 §5.1 publishes no row for it. An unpublished "
                f"window is the 'left to the implementation' the section's own sentence rules out."
            )

    # --- 4. the ceiling -----------------------------------------------------------------------
    if consts["MaxSettleWindow"] != ceiling:
        findings.append(
            f"04 §5.1 says no target waits longer than {human(ceiling)}; MaxSettleWindow is "
            f"{human(consts['MaxSettleWindow'])}."
        )
    for target, seconds in sorted(entries.items()):
        if seconds > consts["MaxSettleWindow"]:
            findings.append(
                f"settleWindows[{target[0] or '(core)'}/{target[1]}] is {human(seconds)}, above the "
                f"{human(consts['MaxSettleWindow'])} ceiling. clampWindow silently corrects it at "
                f"runtime, so the published table would state a window the broker never waits."
            )

    return findings


def _inputs() -> tuple[str, str]:
    for path in (SPEC, CODE):
        if not path.exists():
            raise SystemExit(f"FAIL: {path.relative_to(REPO)} does not exist")
    return SPEC.read_text(encoding="utf-8"), CODE.read_text(encoding="utf-8")


def run() -> int:
    findings = check(*_inputs())
    if findings:
        print("FAIL: V-REV-012 -- the settle windows do not match 04 §5.1", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1
    rows, ceiling = parse_spec(SPEC.read_text(encoding="utf-8"))
    entries, consts = parse_code(CODE.read_text(encoding="utf-8"))
    print(
        f"PASS: V-REV-012 (L0) -- {len(rows)} published settle-window row(s) of 04 §5.1 agree with "
        f"{len(entries)} settleWindows entr(ies) plus the RBAC and default constants, cell for "
        f"cell; nothing in the map is unpublished; the ceiling is {human(consts['MaxSettleWindow'])} "
        f"and no row exceeds it"
    )
    return 0


def _mutate(args: tuple[str, str], index: int, fn) -> tuple[str, str]:
    out = list(args)
    out[index] = fn(out[index])
    return (out[0], out[1])


def negative_control() -> int:
    """Each mutation is a way this check could go quiet, and each names the signal it must produce.

    Drift is symmetric here in a way most doc-drift lints are not -- there is no "source of truth"
    side -- so the table is mutated from both ends: the same number changed in the spec and in the
    code has to be caught twice, by two different messages.
    """
    base = _inputs()
    findings = check(*base)
    if findings:
        print("  BROKEN   the tree is not green, so no row below can be attributed")
        for f in findings[:4]:
            print(f"           {f}")
        print("FAIL: V-REV-012 negative control -- 0 mutations evaluated")
        return 1

    mutations = [
        (
            "the code widens NetworkPolicy and the spec is not touched",
            _mutate(base, 1, lambda t: t.replace(
                '{Group: "networking.k8s.io", Kind: "NetworkPolicy"}: 30 * time.Second',
                '{Group: "networking.k8s.io", Kind: "NetworkPolicy"}: 5 * time.Minute',
            )),
            "publishes 30s for `NetworkPolicy`",
        ),
        (
            "the spec widens NetworkPolicy and the code is not touched",
            _mutate(base, 0, lambda t: t.replace("| NetworkPolicy               | 30s", "| NetworkPolicy               | 5m ")),
            "publishes 5m for `NetworkPolicy`",
        ),
        (
            "the StatefulSet half of a two-kind row is dropped, so the row reads as one value",
            _mutate(base, 0, lambda t: t.replace(
                "| 5m / 10m — a StatefulSet rolls one pod at a time", "| 5m — a StatefulSet rolls one pod at a time"
            )),
            "settleWindows[apps/StatefulSet] is 10m",
        ),
        (
            "a kind is given a window in Go that 04 §5.1 does not publish",
            _mutate(base, 1, lambda t: t.replace(
                '\t{Group: "networking.k8s.io", Kind: "NetworkPolicy"}: 30 * time.Second,',
                '\t{Group: "networking.k8s.io", Kind: "NetworkPolicy"}: 30 * time.Second,\n'
                '\t{Group: "batch", Kind: "CronJob"}:                   45 * time.Second,',
            )),
            "04 §5.1 publishes no row for it",
        ),
        (
            "a published kind loses its map entry and falls through to the default",
            _mutate(base, 1, lambda t: t.replace(
                '\t{Group: "", Kind: "LimitRange"}:    15 * time.Second,\n', ""
            )),
            "kind='LimitRange'",
        ),
        (
            "the ceiling drifts in code",
            _mutate(base, 1, lambda t: t.replace(
                "const MaxSettleWindow = 30 * time.Minute", "const MaxSettleWindow = 45 * time.Minute"
            )),
            "no target waits longer than 30m; MaxSettleWindow is 45m",
        ),
        (
            "a table row is edited above the ceiling, where clampWindow would silently correct it",
            _mutate(base, 1, lambda t: t.replace(
                '{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"}:  30 * time.Minute',
                '{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"}:  300 * time.Minute',
            )),
            "above the 30m ceiling",
        ),
        (
            "the default constant drifts from the `Custom resource` row",
            _mutate(base, 1, lambda t: t.replace(
                "const DefaultSettleWindow = 2 * time.Minute", "const DefaultSettleWindow = 4 * time.Minute"
            )),
            "publishes 2m for `Custom resource`",
        ),
        (
            "the RBAC constant drifts from its row",
            _mutate(base, 1, lambda t: t.replace(
                "const rbacSettleWindow = 15 * time.Second", "const rbacSettleWindow = 90 * time.Second"
            )),
            "publishes 15s for `RBAC`",
        ),
        (
            "the spec's window table is deleted outright",
            _mutate(base, 0, lambda t: t.replace("| Kind                        | Settle window", "| Kind | Verified when")),
            "VACUOUS: 04 §5.1's settle-window table did not yield",
        ),
        (
            "a new published row arrives that no code entry has to match",
            _mutate(base, 0, lambda t: re.sub(
                # Anchored on `15s` because 04 §5.1 has TWO tables and both carry an `RBAC` row --
                # inserting into the verified-when table above would change a file this check does
                # not read, score MISS, and send the reader looking for a hole that is not there.
                r"^(\| RBAC +\| 15s.*\|)$",
                lambda m: "| Job / CronJob               | 12m"
                + " " * (len(m.group(1)) - len("| Job / CronJob               | 12m") - 1)
                + "|\n"
                + m.group(1),
                t,
                count=1,
                flags=re.M,
            )),
            "that this check does not bind to any kind",
        ),
        (
            "the Go map stops parsing, so every published kind reports as absent by name",
            _mutate(base, 1, lambda t: t.replace(
                "var settleWindows = map[classify.KindRef]time.Duration{",
                "var settleWindows = map[classify.KindRef]time.Duration{ // nolint\n",
            )),
            "kind='Deployment'",
        ),
    ]

    failures = 0
    for name, args, needle in mutations:
        # A mutation that did not change its input cannot be evaluated: the unmutated base is
        # re-checked, comes back clean, and the row prints MISS -- the verdict for "the check let
        # the defect through" -- over a defect that was never applied ([[LSN-063]]).
        if args == base:
            failures += 1
            print(f"  BROKEN  {name}")
            print("           the mutation did not change its input; nothing was evaluated")
            continue
        found = check(*args)
        hit = any(needle in f for f in found)
        print(f"  {'caught ' if hit else 'MISS   '} {name}")
        if not hit:
            failures += 1
            print(f"           expected a finding containing {needle!r}; got {found[:2] or 'none'}")
    print(
        f"{'PASS' if not failures else 'FAIL'}: V-REV-012 negative control -- "
        f"{len(mutations) - failures}/{len(mutations)} mutations caught, from both ends of the drift"
    )
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
