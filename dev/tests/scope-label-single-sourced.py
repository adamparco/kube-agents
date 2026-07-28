#!/usr/bin/env python3
"""V-RUN-011 (L0): the five 08 §2.5 label keys are spelled once, and one function renders a scope.

09 §6 states the stake plainly: "a collision is an authority bug, not a cosmetic one -- it makes the
pod<->SA pinning selector ambiguous". 03 §4.2 pins a pod to its ServiceAccount by asserting the
pod's `kube-agents/tier`, `kube-agents/scope` and `kube-agents/role` labels match that SA's, and
08 §2.5 keys the mesh NetworkPolicy and the per-scope quota on the same values. So two distinct
scopes rendering to one label is not a display bug; it is a selector that stops distinguishing two
credentials it exists to distinguish.

The L1 half of V-RUN-011 (`internal/agentlabels/labels_test.go`) proves the renderer is injective
over an adversarial corpus. It cannot prove the renderer is the ONLY renderer, and that is the
failure this file exists for -- because the second renderer is always cheap and always local:

    Labels: map[string]string{"kube-agents/scope": agent.Spec.Scope.Namespace}

Three lines, obviously correct at the call site, and it does not truncate, does not sanitize, does
not hash, and does not agree with the pod that the SA is being compared against. Nothing fails. The
selector just quietly matches the wrong set. An injectivity proof over a function nobody is obliged
to call proves nothing about the cluster.

Four properties:

  1. THE KEYS HAVE ONE DEFINITION SITE. Every `kube-agents/{tier,scope,parent,role,agent}` string
     literal in Go lives in internal/agentlabels/labels.go or in a declared exemption. A key spelled
     a second time is how the renderer and the admission policy come to disagree by one character,
     and a policy whose selector matches nothing reports success, not failure.
  2. NOTHING ELSE BUILDS A SCOPE VALUE. Every Go site that writes the `kube-agents/scope` key must
     take its value from agentlabels (`For` or `RenderScope`), or be a declared exemption.
  3. THE RENDERER STILL HASHES, AND HASHES THE RIGHT THING. `RenderScope` must digest the canonical
     length-prefixed encoding, not the readable join and not the truncation. This is shape, not
     behaviour -- but it is the shape whose loss the L1 corpus would not necessarily catch, since a
     renderer that hashed the join passed 15 of 16 corpus entries.
  4. NON-VACUITY. The definition site must exist, must define all five keys, and the scan must
     actually have read Go files naming them. A check that silently found nothing to compare would
     print PASS forever after a refactor moved the package (LSN-035).

Exemptions are NAMED AND REASONED, in the table below, never a `# noqa` -- the same extension point
`pause-is-not-scale-to-zero.py` uses, for the same reason: a check with no legitimate way to say
"this one is fine" gets weakened by the first person who needs one, and a check weakened under
deadline is a check deleted slowly.

Self-test (the `¬` of 09 §6): `--negative-control` applies each of five plausible regressions to a
copy of the sources in memory and confirms this check reports every one.

Run:  python3 dev/tests/scope-label-single-sourced.py
      python3 dev/tests/scope-label-single-sourced.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from golex import strip_go_comments  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO / "k8s-operator" / "internal" / "agentlabels"
DEFINITION_SITE = PKG / "labels.go"
GO_ROOT = REPO / "k8s-operator"

# The five keys of 08 §2.5. Each must be defined exactly here and nowhere else.
KEYS = (
    "kube-agents/tier",
    "kube-agents/scope",
    "kube-agents/parent",
    "kube-agents/role",
    "kube-agents/agent",
)

# A Go string literal naming one of the five. `kube-agents/agent-id` must NOT match
# `kube-agents/agent`, so the closing quote is part of the pattern.
KEY_LITERAL_RE = re.compile(r'"(' + "|".join(re.escape(k) for k in KEYS) + r')"')

# Property 1 applies to production Go only. A `_test.go` file SHOULD spell the literal: a test that
# compared agentlabels.Tier against agentlabels.Tier would pass after a rename that broke every
# ValidatingAdmissionPolicy in the fleet, so asserting the literal string is the whole assertion.
# The asymmetry is deliberate and is in the safe direction -- a test that misspells a key fails
# immediately and loudly, because its assertion finds nothing; production code that misspells a key
# fails silently forever, because a selector matching nothing is indistinguishable from a selector
# matching a set that is legitimately empty. Property 2 (the scope VALUE) still applies to tests,
# with its own exemption table.
def is_test_file(name: str) -> bool:
    return name.endswith("_test.go")


# Non-test files that may spell a key without going through the package. Each entry is a reason, not
# a waiver: it says what the file is doing and why the alternative is worse.
EXEMPT_SPELLING: dict[str, str] = {
    # The definition site itself.
    "labels.go": "IS the definition site",
}

# Files that may produce a `kube-agents/scope` VALUE without calling the renderer.
EXEMPT_SCOPE_VALUE: dict[str, str] = {
    "labels.go": "IS the renderer",
    "labels_test.go": "exercises the renderer",
    # See the long comment on journal.ScopeLabel. Same key, different value semantics: the journal
    # indexes on the scope LEAF (06 §4.3, 06 §5.1) where a workload carries the whole rendered scope
    # key (08 §2.5). Switching the journal to RenderScope would silently invalidate every existing
    # ActionRecord selector, so the divergence is recorded and left alone rather than closed inside
    # an implementation unit.
    "store.go": "the journal indexes on the scope LEAF, not the 08 §2.5 rendering -- the key is "
    "shared with agentlabels, the value deliberately is not; see the comment on journal.ScopeLabel "
    "and the open item in docs/build/phase-9.md",
    "store_test.go": "pins store.go's leaf semantics; it must be exempt for the same reason "
    "store.go is, and it is the thing that would fail first if the leaf rendering changed",
}

# Property 3: the renderer's shape. `canonical` exists to be the thing hashed.
CANONICAL_HASH_RE = re.compile(r"sha256\.Sum256\(\[\]byte\(canonical\(s\)\)\)")
CANONICAL_IS_LENGTH_PREFIXED_RE = re.compile(r"len\(s\.ProjectID\).*len\(s\.ClusterName\).*len\(s\.Namespace\)", re.DOTALL)

# Property 4's real teeth. Counting files that NAME a key would let this check pass on a tree where
# the package exists, is internally consistent, and is called by nobody -- which is precisely the
# state a botched refactor leaves behind and precisely the state that makes properties 1-3 vacuous.
# So the floor is on files that IMPORT the package. Two is the current count (the manifest renderer
# and the journal) and is low enough not to be a maintenance tax.
IMPORT_PATH = "github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
MIN_FILES_IMPORTING = 2

# Property 2's trigger. Deliberately BOTH spellings: a caller that imports the constant for the key
# and then hand-builds the value is the harder version of the same bug -- it looks single-sourced in
# review, because the import is right there -- and matching only the string literal would miss it
# entirely.
SCOPE_KEY_REFERENCE = ('"kube-agents/scope"', "agentlabels.Scope", "ScopeLabel")

# Property 2's positive side: a file that references the scope key must also call the renderer.
RENDERER_CALLS = ("agentlabels.For(", "agentlabels.RenderScope(", "RenderScope(", "For(agent,")


def tracked_go_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "k8s-operator"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / p for p in out.split("\0") if p.endswith(".go")]


def check(sources: dict[str, str]) -> list[str]:
    """All four properties over already-read sources, so the negative control can mutate them."""
    failures: list[str] = []
    definition = sources.get(DEFINITION_SITE.name, "")

    # 4a. The definition site exists and defines all five.
    if not definition:
        return [
            f"{DEFINITION_SITE.relative_to(REPO)} is missing or empty. There is no definition site "
            "to compare against, so this check is asserting nothing"
        ]
    for key in KEYS:
        if f'"{key}"' not in definition:
            failures.append(
                f"{DEFINITION_SITE.name}: does not define {key!r}. 08 §2.5 requires all five on "
                "both halves of the pair; a key that is not defined here is a key each caller "
                "spells for itself"
            )

    files_importing = 0

    for name, text in sorted(sources.items()):
        if name == DEFINITION_SITE.name:
            continue
        code = strip_go_comments(text)
        if IMPORT_PATH in code:
            files_importing += 1

        # 1. One definition site for the spellings. Production Go only -- see is_test_file.
        if KEY_LITERAL_RE.search(code) and name not in EXEMPT_SPELLING and not is_test_file(name):
            for lineno, line in enumerate(code.splitlines(), start=1):
                for key in KEY_LITERAL_RE.findall(line):
                    failures.append(
                        f"{name}:{lineno} spells {key!r} as a literal. Import "
                        "internal/agentlabels and use the constant -- or add a named, reasoned "
                        "entry to EXEMPT_SPELLING in this file. A key spelled twice is a key that "
                        "can disagree by one character, and a ValidatingAdmissionPolicy whose "
                        "selector matches nothing reports success (08 §2.5)"
                    )

        # 2. One renderer for the scope value.
        if any(ref in code for ref in SCOPE_KEY_REFERENCE) and name not in EXEMPT_SCOPE_VALUE:
            if not any(call in code for call in RENDERER_CALLS):
                failures.append(
                    f"{name}: references the {'kube-agents/scope'!r} key but never calls "
                    "agentlabels.For or agentlabels.RenderScope. A hand-built scope value does not "
                    "truncate, sanitize or hash the way the renderer does, so it will not match the "
                    "value on the ServiceAccount that 03 §4.2 compares it against -- and nothing "
                    "reports the mismatch (V-RUN-011)"
                )

    # 3. The renderer still hashes the canonical encoding.
    if not CANONICAL_HASH_RE.search(definition):
        failures.append(
            f"{DEFINITION_SITE.name}: the digest is no longer taken over `canonical(s)`. Hashing "
            "the readable join reintroduces the join's ambiguity ({acme, prod.eu, payments} and "
            "{acme, prod, eu.payments} join to one string); hashing the TRUNCATION defeats the "
            "point entirely. Both read in a diff as 'adds a hash suffix'"
        )
    if not CANONICAL_IS_LENGTH_PREFIXED_RE.search(definition):
        failures.append(
            f"{DEFINITION_SITE.name}: `canonical` no longer length-prefixes all three levels. The "
            "length prefix is the whole reason the encoding is injective for levels that may "
            "themselves contain the separator"
        )

    # 4b. Non-vacuity: somebody actually calls the package.
    if files_importing < MIN_FILES_IMPORTING:
        failures.append(
            f"VACUOUS: only {files_importing} Go file(s) import {IMPORT_PATH}, below the floor of "
            f"{MIN_FILES_IMPORTING}. Either the scan stopped reading the tree or the callers "
            "stopped calling the renderer. A single-sourcing check over a source nobody reads from "
            "asserts nothing about what the cluster is actually labelled with"
        )

    return failures


def read_sources() -> dict[str, str]:
    """Every tracked Go file, plus the definition package read straight off disk.

    The package is read from disk rather than from `git ls-files` so this check is usable on the
    working tree that is introducing it -- a check that only becomes runnable after the commit it
    is meant to gate is a check that gates nothing.
    """
    sources: dict[str, str] = {}
    paths = list(tracked_go_files())
    if PKG.is_dir():
        paths.extend(PKG.glob("*.go"))
    for path in paths:
        try:
            sources[path.name] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return sources


def negative_control() -> int:
    """Break each property in memory and confirm this check notices."""
    sources = read_sources()
    mutations = [
        (
            "a caller respells a key instead of importing the constant",
            lambda s: {**s, "some_renderer.go": 'package controller\n\nvar l = map[string]string{"kube-agents/tier": t}\n'},
        ),
        (
            # The harder version: the key comes from the package, so the import is right there in
            # the diff and it reads as single-sourced. Only the VALUE is hand-built.
            "a caller imports the key constant but hand-builds the value",
            lambda s: {
                **s,
                "some_renderer.go": "package controller\n\nvar l = map[string]string{agentlabels.Scope: agent.Spec.Scope.Namespace}\n",
            },
        ),
        (
            # The same respelling, hidden behind a URL on the same line. This is the mutation that
            # makes the shared golex scanner load-bearing rather than a tidy-up: the line-oriented
            # `line.split("//", 1)[0]` this check used until LSN-038's pass truncates at the `//`
            # in `https://`, drops the rest of the line, and reports nothing. A false negative here
            # is permanent and silent -- the selector just stops distinguishing two credentials.
            "a caller respells a key on a line that also contains a URL",
            lambda s: {
                **s,
                "some_renderer.go": 'package controller\n\nvar u, l = "https://kubernetes.default.svc", map[string]string{"kube-agents/tier": t}\n',
            },
        ),
        (
            "the renderer hashes the readable join instead of the canonical encoding",
            lambda s: {
                **s,
                DEFINITION_SITE.name: s[DEFINITION_SITE.name].replace(
                    "sha256.Sum256([]byte(canonical(s)))", "sha256.Sum256([]byte(key))", 1
                ),
            },
        ),
        (
            "canonical stops length-prefixing",
            lambda s: {
                **s,
                DEFINITION_SITE.name: s[DEFINITION_SITE.name].replace(
                    "len(s.ProjectID), s.ProjectID", "s.ProjectID", 1
                ),
            },
        ),
        (
            "a key is dropped from the definition site",
            lambda s: {
                **s,
                DEFINITION_SITE.name: s[DEFINITION_SITE.name].replace('"kube-agents/parent"', '"kube-agents/owner"'),
            },
        ),
    ]

    clean = check(sources)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors: list[str] = []
    for label, mutate in mutations:
        mutated = mutate(dict(sources))
        if mutated == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        if not check(mutated):
            survivors.append(label)

    if survivors:
        print("FAIL: the negative control found regressions this check does not detect:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(f"PASS: negative control -- all {len(mutations)} injected regressions were detected")
    return 0


def main() -> int:
    if "--negative-control" in sys.argv[1:]:
        return negative_control()

    failures = check(read_sources())
    if failures:
        print("FAIL: V-RUN-011 -- the scope label is not single-sourced", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: V-RUN-011 (L0) -- the {len(KEYS)} keys of 08 §2.5 are defined once in "
        f"{DEFINITION_SITE.relative_to(REPO)}, every scope value comes from the renderer, and the "
        "renderer still digests the canonical length-prefixed encoding"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
