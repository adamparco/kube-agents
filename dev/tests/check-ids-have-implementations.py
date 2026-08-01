#!/usr/bin/env python3
"""V-MET-001 — the catalog and the code agree about what is implemented.

09 §8's row reads: "Every check ID in §6 exists in the implemented suite, and every implemented test
declares a known check ID." Two directions between two sets — the IDs the specification defines, and
the IDs the executable tree asserts — and until `verification/implementations.yaml` existed there
was no artifact holding the second one, so neither direction could be checked.

WHAT WENT WRONG WITHOUT IT. `dev/tests/phase-ratchet-is-asserted.py` prints a `hint: named by ...`
column built from `git grep <check-id>`, under a footer that disclaims it in as many words: "The
`hint` column is UNWEIGHTED and no property reads it. A file naming a check ID may be disclaiming
it." On 2026-07-31 the column was read as coverage anyway and `P9-T11g` was scheduled promising
"eleven runs to record and two builds". The tree held one run and twelve builds. Nine of the eleven
were named by a parser fixture, by `.claude/harness/binding.md` and the skills that REQUIRE the
check, or by `examples/gitops-repo/policy/tests/vap_actor_*.yaml` — corpora that are real, correct,
and executed by nothing on either chain. A grep cannot tell an assertion from a citation, and a
second grep will not either. The registry is a human's answer, written once, and this file is what
stops it drifting from the tree it describes.

THE PROPERTIES, in the order they run:

  1. THE REGISTRY PARSES. Strict, no PyYAML, because L0 installs no dependencies. A row is a quoted
     check ID holding either `runs:` + `asserts_in:` or `unimplemented:`. Anything else raises.

  2. EVERY REGISTRY KEY IS A 09 §6 ID. The reverse direction on the catalog side: a renamed or
     retired ID leaves a row here pointing at nothing, and a row pointing at nothing reads exactly
     like a row pointing at something.

  3. EVERY `asserts_in` EXISTS AND NAMES ITS CHECK ID. This is the half that makes a later grep
     honest. It is also what caught the seven checks whose implementation was real and anonymous:
     V-BRK-023, V-CTR-016, V-CTR-017, V-CTR-018, V-GAT-001, V-MET-014 and V-REV-008 were all green,
     all genuinely asserted, and named their own ID nowhere in the tree.

  4. EVERY `runs` IS REACHABLE AND REACHES ITS FILE. The command must appear VERBATIM on
     `dev/L0-CHAIN.txt` or `dev/L2-CHAIN.txt`, or be one of the two entry points no chain line
     carries: `make -C k8s-operator test` (the Go entry point `.claude/harness/binding.md` §Build
     names, absent from L0 because it needs envtest) and `python3 -m unittest discover dev`. Then
     the command must actually reach the file it claims. An implementation nothing runs is not an
     implementation — that is the other half of the VAP-corpus lesson above.

  5. FORWARD, PHASE-SCOPED: every check that is required at the current phase AND has a green row in
     `verification/results.csv` has an entry here. A check with no green row is deliberately absent:
     it is not in the implemented suite, and `phase-ratchet-is-asserted.py` already owns the
     "required but not green" population. Duplicating that population would give one gap two gates
     and two chances to be answered differently.

  6. EVERY CHECK-ID TOKEN IN THE TEST CORPUS IS DEFINED IN 09 §6. 09 §8's second clause, literally:
     every implemented test declares a KNOWN check ID. A typo, a renumbering, or a reference to a
     retired ID fails here. Two exits, both loud. A negative control needs IDs that will never be
     real, so the suite codes in `SYNTHETIC_SUITES` are reserved and property 0 fails if 09 ever
     defines one. And a test may legitimately name a non-existent ID in prose — `invariants-gate.py`
     records that there is no V-CTR-021 (not-a-check-id), so a false pass on V-CTN-021 was not a
     one-letter slip, which is the point of the sentence — so a line carrying that marker is exempt.
     Both
     exits are COUNTED and printed on the pass line: an escape hatch nobody can see is how a check
     stops checking without ever going red.

  7. THE `unimplemented:` COUNT MAY NOT RISE. `unimplemented:` records a check with a green results
     row whose evidence attests to something other than the check's stated property — a false green,
     published rather than hidden. Today there is exactly one, V-CTR-001. The ceiling is the shape
     09 §8.1 chose for coverage and V-MET-003 for assertions: a gate that always fails is a gate
     someone disables, so the remainder is named, counted and ratcheted instead of tolerated.

WHY IT IMPORTS THE RATCHET MODULE rather than re-deriving the catalog, the phase filter and the
results reader: V-MET-013 forbids a second definition site, and three of them would be three chances
for this check and the phase gate to disagree about what "required" or "green" means.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shlex
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = REPO / "verification" / "implementations.yaml"
SPEC = REPO / "docs" / "design" / "09-verification-and-validation.md"
RESULTS = REPO / "verification" / "results.csv"
L0_CHAIN = REPO / "dev" / "L0-CHAIN.txt"
L2_CHAIN = REPO / "dev" / "L2-CHAIN.txt"
RATCHET_SRC = REPO / "dev" / "tests" / "phase-ratchet-is-asserted.py"

CHECK_ID = re.compile(r"V-[A-Z]{3}-\d{3}")

# The two entry points no chain line carries, and why. `make -C k8s-operator test` is
# `binding.md` §Build's Go entry point and needs KUBEBUILDER_ASSETS, so L0 cannot run it;
# `python3 -m unittest discover dev` IS on L0, and is named here so a `dev/test_*.py` row does not
# have to restate the discover invocation per module.
ENTRY_POINTS = ("make -C k8s-operator test", "python3 -m unittest discover dev")

# Property 7. Raising this number is a decision, not a fix.
UNIMPLEMENTED_CEILING = 1

# Property 6's two exits. `SYNTHETIC_SUITES` are the suite codes a negative control may invent;
# property 0 fails if 09 §6 ever defines one, so the reservation cannot rot into a blind spot.
# `NOT_A_CHECK_ID` exempts one line, for the case where naming a non-existent ID IS the sentence.
SYNTHETIC_SUITES = ("XXX", "QQQ", "ZZZ")
NOT_A_CHECK_ID = "not-a-check-id"

# Property 6's negative control needs the harder case: a REAL suite code carrying a number 09 §6
# does not define -- the renumbering, not the typo. A `SYNTHETIC_SUITES` code would be let through
# by design and the control would pass on a mutation the check never saw.
RENUMBERED_FIXTURE = "V-CTR-099"  # not-a-check-id -- deliberately undefined, see above

# Where a check ID may legitimately appear in executable form. Anything outside this set is prose,
# a spec, or a fixture, and property 6 does not read it.
CORPUS_GLOBS = (
    ("dev/tests", "*.py"),
    ("dev", "test_*.py"),
    ("dev/verify", "*.sh"),
)
GO_CORPUS = ("k8s-operator", "*_test.go")


class RegistryError(Exception):
    """The registry did not parse. Never a finding — a finding needs a parsed file."""


# --------------------------------------------------------------------------------------------
# The strict reader. Same shape as `load_traceability` in dev/tests/spec-ids.py, and for the same
# reason: a lenient reader turns a malformed row into a silently absent property.
# --------------------------------------------------------------------------------------------

KEY_LINE = re.compile(r'^"(?P<id>V-[A-Z]{3}-\d{3})":$')
FIELD_LINE = re.compile(r"^  (?P<key>runs|asserts_in|unimplemented): (?P<val>.*)$")


def load_registry(text: str) -> dict[str, dict[str, str]]:
    """`verification/implementations.yaml` -> {check_id: {field: value}}."""
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = KEY_LINE.match(raw)
        if m:
            if m.group("id") in out:
                raise RegistryError(f"line {n}: {m.group('id')} appears twice")
            current = m.group("id")
            out[current] = {}
            continue
        m = FIELD_LINE.match(raw)
        if not m:
            raise RegistryError(f"line {n}: not a check-ID key and not a known field: {raw!r}")
        if current is None:
            raise RegistryError(f"line {n}: a field before any check ID")
        try:
            value = json.loads(m.group("val"))
        except json.JSONDecodeError as exc:
            raise RegistryError(f"line {n}: {m.group('key')} is not a JSON scalar ({exc})") from exc
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(f"line {n}: {m.group('key')} is not a non-empty string")
        out[current][m.group("key")] = value
    if not out:
        raise RegistryError("the registry parsed to 0 entries")
    for check_id, fields in out.items():
        keys = set(fields)
        if keys == {"runs", "asserts_in"} or keys == {"unimplemented"}:
            continue
        raise RegistryError(
            f"{check_id}: an entry is `runs` + `asserts_in` or `unimplemented` alone, "
            f"not {sorted(keys)}"
        )
    return out


def ratchet_module():
    """The catalog parser, the phase filter and the results reader, from their one definition site."""
    spec = importlib.util.spec_from_file_location("_phase_ratchet", RATCHET_SRC)
    if spec is None or spec.loader is None:
        raise RegistryError(f"cannot import {RATCHET_SRC}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chain_commands(text: str) -> set[str]:
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def reaches(command: str, target: str) -> bool:
    """Does running `command` execute the assertions in `target`?"""
    if command == "make -C k8s-operator test":
        return target.startswith("k8s-operator/") and target.endswith("_test.go")
    if command == "python3 -m unittest discover dev":
        p = pathlib.PurePosixPath(target)
        return str(p.parent) == "dev" and p.name.startswith("test_") and p.suffix == ".py"
    return target in shlex.split(command)


def corpus_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for sub, pattern in CORPUS_GLOBS:
        out.extend(sorted((REPO / sub).glob(pattern)))
    out.extend(sorted((REPO / GO_CORPUS[0]).rglob(GO_CORPUS[1])))
    return out


# --------------------------------------------------------------------------------------------
# The properties.
# --------------------------------------------------------------------------------------------


def check(
    registry_text: str,
    spec_text: str,
    phase_text: str,
    results_text: str,
    l0_text: str,
    l2_text: str,
    corpus: dict[str, str],
    phase: int,
    file_exists=None,
    file_text=None,
    stats: dict | None = None,
) -> list[str]:
    """Returns findings. An empty list is a pass. `stats` is filled with property 6's exit counts."""
    if stats is None:
        stats = {}
    stats.setdefault("exempt_lines", 0)
    stats.setdefault("synthetic", 0)
    # `corpus` is the authority for any path it holds -- property 3 and property 6 must read the
    # same bytes, or a negative control can mutate one and watch the other stay green.
    if file_exists is None:
        file_exists = lambda p: p in corpus or (REPO / p).is_file()  # noqa: E731
    if file_text is None:
        file_text = lambda p: (  # noqa: E731
            corpus[p] if p in corpus else (REPO / p).read_text(errors="ignore")
        )

    registry = load_registry(registry_text)
    ratchet = ratchet_module()
    catalog = parse_catalog_or_raise(ratchet, spec_text)
    findings: list[str] = []

    # Property 2 — every registry key is a 09 §6 ID.
    for check_id in sorted(set(registry) - set(catalog)):
        findings.append(
            f"property 2: {check_id} has a registry row and no 09 §6 catalog row -- the ID was "
            f"renamed, retired or mistyped, and the row now points at nothing"
        )

    # Property 3 — every `asserts_in` exists and names its check ID.
    for check_id, fields in sorted(registry.items()):
        target = fields.get("asserts_in")
        if target is None:
            continue
        if not file_exists(target):
            findings.append(f"property 3: {check_id} names `{target}`, which does not exist")
            continue
        if check_id not in file_text(target):
            findings.append(
                f"property 3: {check_id} names `{target}`, which never mentions {check_id} -- "
                f"declare the ID there, or the registry is the only place the pairing exists"
            )

    # Property 4 — every `runs` is reachable and reaches its file.
    chains = chain_commands(l0_text) | chain_commands(l2_text)
    for check_id, fields in sorted(registry.items()):
        command = fields.get("runs")
        if command is None:
            continue
        if command not in chains and command not in ENTRY_POINTS:
            findings.append(
                f"property 4: {check_id} runs `{command}`, which is on neither chain and is not an "
                f"entry point -- an implementation nothing runs is not an implementation"
            )
            continue
        if not reaches(command, fields["asserts_in"]):
            findings.append(
                f"property 4: {check_id} runs `{command}`, which does not reach "
                f"`{fields['asserts_in']}`"
            )

    # Property 5 — forward, phase-scoped.
    required = required_set(ratchet, spec_text, phase_text, phase)
    results = ratchet.parse_results(results_text)
    green = {c for c in required if ratchet.is_green(results.get(c, []))}
    for check_id in sorted(green - set(registry)):
        findings.append(
            f"property 5: {check_id} is required at phase {phase} and green in "
            f"verification/results.csv, and no registry row says where its assertion lives"
        )

    # Property 0 — the synthetic namespaces are still synthetic. Checked here rather than at import
    # so it fails as a finding on the mutated catalog too.
    for suite in SYNTHETIC_SUITES:
        real = sorted(c for c in catalog if c.startswith(f"V-{suite}-"))
        if real:
            findings.append(
                f"property 0: 09 §6 now defines {' '.join(real)}, and V-{suite}-* is reserved for "
                f"negative-control fixtures -- property 6 would stop seeing a real ID's typos"
            )

    # Property 6 — every check-ID token in the corpus is defined in 09 §6.
    for path in sorted(corpus):
        for lineno, line in enumerate(corpus[path].splitlines(), 1):
            if NOT_A_CHECK_ID in line:
                stats["exempt_lines"] += 1
                continue
            for token in sorted(set(CHECK_ID.findall(line))):
                if token in catalog:
                    continue
                if token[2:5] in SYNTHETIC_SUITES:
                    stats["synthetic"] += 1
                    continue
                findings.append(
                    f"property 6: {path}:{lineno} names {token}, which 09 §6 does not define -- "
                    f"09 §8's second clause is that every implemented test declares a KNOWN check "
                    f"ID. If naming a non-existent ID is the point of the line, say so on it with "
                    f"the marker `{NOT_A_CHECK_ID}`"
                )

    # Property 7 — the `unimplemented:` count may not rise.
    unimplemented = sorted(c for c, f in registry.items() if "unimplemented" in f)
    if len(unimplemented) > UNIMPLEMENTED_CEILING:
        findings.append(
            f"property 7: {len(unimplemented)} `unimplemented:` rows against a ceiling of "
            f"{UNIMPLEMENTED_CEILING} -- {' '.join(unimplemented)}. Each one is a check with a "
            f"green row whose evidence attests to something else. The ceiling only moves by "
            f"argument, in its own unit"
        )
    return findings


def parse_catalog_or_raise(ratchet, spec_text: str) -> dict:
    try:
        return ratchet.parse_catalog(spec_text)
    except Exception as exc:  # ParseError, and anything the row shape can throw
        raise RegistryError(f"09 §6 did not parse: {exc}") from exc


def required_set(ratchet, spec_text: str, phase_text: str, phase: int) -> set[str]:
    """09 §10's ratchet for this phase, union the phase file's acceptance table -- the phase gate's
    own definition of `required`, read from the phase gate."""
    catalog = ratchet.parse_catalog(spec_text)
    return set(ratchet.parse_ratchet(spec_text, phase, catalog).required) | set(
        ratchet.parse_acceptance_table(phase_text, phase)
    )


# --------------------------------------------------------------------------------------------
# Runner and negative control.
# --------------------------------------------------------------------------------------------


def _inputs(phase: int) -> tuple:
    corpus = {}
    for path in corpus_files():
        corpus[str(path.relative_to(REPO))] = path.read_text(errors="ignore")
    phase_file = REPO / "docs" / "build" / f"phase-{phase}.md"
    return (
        REGISTRY.read_text(),
        SPEC.read_text(),
        phase_file.read_text(),
        RESULTS.read_text(),
        L0_CHAIN.read_text(),
        L2_CHAIN.read_text(),
        corpus,
        phase,
    )


def run(phase: int | None = None) -> int:
    ratchet = ratchet_module()
    phase = ratchet.latest_phase() if phase is None else phase
    args = _inputs(phase)
    stats: dict[str, int] = {}
    findings = check(*args, stats=stats)
    registry = load_registry(args[0])
    if findings:
        print(f"FAIL: V-MET-001 (L0) -- {len(findings)} findings", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1
    implemented = sum(1 for f in registry.values() if "runs" in f)
    unimplemented = sorted(c for c, f in registry.items() if "unimplemented" in f)
    corpus_ids = set()
    for text in args[6].values():
        corpus_ids |= set(CHECK_ID.findall(text))
    print(
        f"PASS: V-MET-001 (L0) -- {implemented} check IDs map to an implementation that exists, "
        f"names its own ID and runs on a chain; every ID required at phase {phase} and green in "
        f"verification/results.csv has a row; the {len(corpus_ids)} distinct check IDs the test "
        f"corpus names are all defined in 09 §6, past {stats['synthetic']} reserved "
        f"V-{{{'|'.join(SYNTHETIC_SUITES)}}}-* fixture token(s) and {stats['exempt_lines']} line(s) "
        f"marked `{NOT_A_CHECK_ID}`; {len(unimplemented)} published `unimplemented:` row(s) against "
        f"a ceiling of {UNIMPLEMENTED_CEILING}"
        + (f" ({' '.join(unimplemented)})" if unimplemented else "")
    )
    return 0


def _mutate(args: tuple, index: int, fn) -> tuple:
    out = list(args)
    out[index] = fn(out[index])
    return tuple(out)


def negative_control(phase: int | None = None) -> int:
    """Break each property in turn and require the finding that property, and only that, produces.

    Per-mutation signal, not merely non-emptiness ([[LSN-035]]): a control that asserts "some
    finding appeared" passes when property 4 fires for property 2's mutation, which is the check
    reporting the wrong thing while reading green.
    """
    ratchet = ratchet_module()
    phase = ratchet.latest_phase() if phase is None else phase
    base = _inputs(phase)
    if check(*base):
        print("BROKEN: the tree is not green, so the control cannot attribute anything", file=sys.stderr)
        return 1

    # A row whose `asserts_in` the corpus holds, so mutating the corpus actually reaches property 3.
    real_id = next(
        c
        for c, f in load_registry(base[0]).items()
        if "runs" in f and f["asserts_in"] in base[6]
    )
    real_row = re.search(
        rf'^"{real_id}":\n  runs: (?P<runs>.*)\n  asserts_in: (?P<in>.*)$', base[0], re.M
    )
    if real_row is None:
        print(f"BROKEN: could not locate {real_id}'s row to mutate", file=sys.stderr)
        return 1

    mutations: list[tuple[str, tuple, str]] = [
        (
            "an ID with no 09 §6 row keeps a registry entry",
            _mutate(base, 0, lambda t: t.replace(f'"{real_id}":', '"V-ZZZ-999":', 1)),
            "property 2: V-ZZZ-999 has a registry row and no 09 §6 catalog row",
        ),
        (
            "asserts_in points at a file that does not exist",
            _mutate(
                base,
                0,
                lambda t: t.replace(
                    f'  asserts_in: {real_row.group("in")}',
                    '  asserts_in: "dev/tests/there-is-no-such-file.py"',
                    1,
                ),
            ),
            f"property 3: {real_id} names `dev/tests/there-is-no-such-file.py`, which does not exist",
        ),
        (
            "the implementation stops naming its own check ID",
            _mutate(
                base,
                6,
                lambda c: {
                    k: (v.replace(real_id, "V-XXX-000") if k == json.loads(real_row.group("in")) else v)
                    for k, v in c.items()
                },
            ),
            f"property 3: {real_id} names `{json.loads(real_row.group('in'))}`, which never mentions",
        ),
        (
            "the command is on no chain and is not an entry point",
            _mutate(
                base,
                0,
                lambda t: t.replace(
                    f'  runs: {real_row.group("runs")}',
                    '  runs: "python3 dev/tests/not-on-any-chain.py"',
                    1,
                ),
            ),
            f"property 4: {real_id} runs `python3 dev/tests/not-on-any-chain.py`, which is on "
            f"neither chain",
        ),
        (
            "a chain command that does not reach the file it claims",
            _mutate(
                base,
                0,
                lambda t: t.replace(
                    f'  runs: {real_row.group("runs")}',
                    '  runs: "make -C k8s-operator test"\n  asserts_in: "dev/tests/spec-ids.py"',
                    1,
                ).replace(f'  asserts_in: {real_row.group("in")}\n', "", 1),
            ),
            f"property 4: {real_id} runs `make -C k8s-operator test`, which does not reach",
        ),
        (
            "a green, required check loses its registry row",
            _mutate(
                base,
                0,
                lambda t: re.sub(
                    rf'^"{re.escape(_a_required_green(base, phase))}":\n(?:  .*\n)+', "", t, count=1, flags=re.M
                ),
            ),
            f"property 5: {_a_required_green(base, phase)} is required at phase {phase} and green",
        ),
        (
            "a test declares a check ID 09 §6 does not define",
            _mutate(
                base,
                6,
                lambda c: {
                    **c,
                    "dev/tests/spec-ids.py": c["dev/tests/spec-ids.py"]
                    + f"\n# {RENUMBERED_FIXTURE}\n",
                },
            ),
            f"names {RENUMBERED_FIXTURE}, which 09 §6 does not define",
        ),
        (
            "a second `unimplemented:` row appears without the ceiling moving",
            _mutate(
                base,
                0,
                lambda t: t
                + '\n"V-CTR-004":\n  unimplemented: "a second false green, smuggled in"\n',
            ),
            f"property 7: 2 `unimplemented:` rows against a ceiling of {UNIMPLEMENTED_CEILING}",
        ),
    ]

    caught = 0
    for name, args, needle in mutations:
        try:
            findings = check(*args)
        except RegistryError as exc:
            findings = [f"registry did not parse: {exc}"]
        hit = [f for f in findings if needle in f]
        if hit:
            caught += 1
            print(f"  [ok] {name}")
        elif findings:
            print(
                f"  [MISS] {name} -- the check went red, but for the wrong reason:\n"
                f"         wanted: {needle}\n         got:    {findings[0]}",
                file=sys.stderr,
            )
        else:
            print(f"  [MISS] {name} -- the check stayed green", file=sys.stderr)

    total = len(mutations)
    if caught == total:
        print(f"PASS: V-MET-001 negative control -- all {total} breakages caught, each by the "
              f"property it targets")
        return 0
    print(f"FAIL: V-MET-001 negative control -- {caught}/{total}", file=sys.stderr)
    return 1


def _a_required_green(base: tuple, phase: int) -> str:
    """One check that property 5 covers, chosen from the tree so the control cannot go stale."""
    ratchet = ratchet_module()
    required = required_set(ratchet, base[1], base[2], phase)
    results = ratchet.parse_results(base[3])
    registry = load_registry(base[0])
    for check_id in sorted(required):
        if check_id in registry and ratchet.is_green(results.get(check_id, [])):
            return check_id
    raise RegistryError("no required-and-green check has a registry row -- property 5 is vacuous")


def main(argv: list[str]) -> int:
    phase = None
    if "--phase" in argv:
        phase = int(argv[argv.index("--phase") + 1])
    try:
        if "--negative-control" in argv:
            return negative_control(phase)
        return run(phase)
    except RegistryError as exc:
        print(f"FAIL: V-MET-001 (L0) -- {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
