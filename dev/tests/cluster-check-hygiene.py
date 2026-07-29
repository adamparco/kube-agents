#!/usr/bin/env python3
"""Two ways a cluster-facing check script is silently wrong about the cluster.

Both properties below come from defects found in `dev/verify/brake-fanout-l2.sh` while it was being
written (P9-T7c-3c-ii-b-2-b). Neither was a defect in the product; both were defects in the check,
and both are the kind that a passing run cannot distinguish from a correct one. They share a
scanner because they share the part that must not go stale: the *discovery* of which scripts talk
to a cluster. A property asserted over a set that quietly stopped growing is LSN-036.

--------------------------------------------------------------------------------------------------
PROPERTY 1 (LSN-044) -- `auth can-i` takes a resource, optionally a NAME, and never a subresource
--------------------------------------------------------------------------------------------------

`kubectl auth can-i <verb> <TYPE>/<NAME>` parses the slash as naming an OBJECT. So

    kubectl auth can-i patch actionrecords.kubeagents.x-k8s.io/status --as=$SA

asks whether the subject may patch an ActionRecord literally *named* `status`. The subresource form
is `--subresource=status`. The command is well-formed, exits 0, and prints a plausible answer.

The reason this is worth a gate rather than a code review note: `actionrecords/status` is exactly
how the ClusterRole, the ValidatingAdmissionPolicy and every comment in the tree spell that
subresource, so transcribing it into `auth can-i` is the natural motion. In P9-T7c-3c-ii-b-2-b it
happened to land on a positive assertion, where the wrong answer is a red. **The direction that
matters is the negative one.** A `want_no` written that way draws its `no` from a resource name
nobody was ever granted rather than from the policy under test -- it would pass against a
ClusterRole with `verbs: ["*"]` on the subresource, and it is how an entire authority boundary gets
"proven" by a check that never asked the question.

Two halves, because a resource is either statically resolvable or it is not:

  1a. RESOLVABLE. No positional word of an `auth can-i` invocation may contain `/`. A blanket ban
      rather than a "known subresource names" list on purpose: the failure is silent, the legitimate
      `TYPE/NAME` form has never once been used in this repository, and a list of subresource names
      is a headcount (LSN-036). If a genuine `TYPE/NAME` query is ever needed, add it to
      `LITERAL_EXEMPT` below with the reason -- a visible diff, which is the point.

      Resolvable includes `for`-lists. `for pair in "impersonate users" "escalate roles.rbac..."`
      feeding `auth can-i $pair` is an established idiom in `verify-phase2.sh` and
      `verify-phase3.sh`; those lists are enumerated and each expansion checked, so those sites get
      the strict treatment rather than the weaker runtime-guard treatment below.

  1b. COMPUTED. A script that passes a resource this file cannot resolve must carry the guard at
      runtime instead: a `*/*)` case arm that refuses the malformed shape. This is the `can()`
      helper in `brake-fanout-l2.sh`, and it is strictly stronger than 1a where it applies, since it
      also catches a slash that only appears at runtime. Without it, 1a is trivially evaded by
      assigning the string to a variable first -- not maliciously, just by refactoring a repeated
      query into a helper, which is exactly what happened here.

--------------------------------------------------------------------------------------------------
PROPERTY 2 (LSN-045) -- a script that writes to an append-only journal cannot delete its namespace
--------------------------------------------------------------------------------------------------

`kube-agents-journal-retention` denies DELETE of an `ActionRecord` to every principal except the
retention controller and the operator, **and denies it even to them unless
`status.exported.confirmed` is true**. The namespace controller is not on that list. On a cluster
with no audit sink nothing ever confirms an export -- `journal_reconciler.go` logs that the record
"will be retained indefinitely because the export is the durable record (05 §1.2)" -- so a namespace
holding one can never finish terminating, and its name can never be reused.

The suite that found this passed on run 1 and could not create its namespace on run 2. That is the
worst available failure mode for a check: correct-looking evidence, produced once, from a fixture
path that cannot be repeated. A re-run is how a green result is distinguished from a lucky one.

The tempting fix is a one-liner -- patch `status.exported.confirmed: true` onto the suite's own
records and the namespace frees itself. It is also writing an export confirmation for an export that
never happened, in shipped tooling, where it becomes the idiom the next suite copies. Declined; see
the ledger's Decisions table for 2026-07-29. The supported shape is: reuse the namespace, delete
only the objects inside it, mint identifiers per run, and select Events by `involvedObject.uid` so
residue from a previous run cannot satisfy an assertion.

**The protected set is derived, not listed.** It comes from the ValidatingAdmissionPolicies
themselves -- every resource any policy matches for `DELETE` -- and the Kind is then read out of the
CRD that declares that plural. Add a second retention policy over a second resource and this check
starts enforcing it the same day, with no edit here. That derivation is the whole difference between
this and a check that knows about ActionRecords.

Self-test (the `¬`): `--negative-control` applies each breakage to a copy of the sources in memory
and confirms this check reports it.

Run:  python3 dev/tests/cluster-check-hygiene.py
      python3 dev/tests/cluster-check-hygiene.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
POLICY_DIR = REPO / "k8s-operator" / "config" / "policy"
CRD_DIR = REPO / "k8s-operator" / "config" / "crd" / "bases"
SCRIPT_ROOTS = (REPO / "dev", REPO / "k8s-operator" / "scripts")

# Non-vacuity floors. Each one is a count that only ever grows in normal work, so a floor that trips
# means the discovery stopped finding things -- which is the failure this file is most exposed to,
# since every property below is asserted over a set this file computes.
MIN_SCRIPTS = 25
MIN_CANI_SITES = 10
MIN_PROTECTED_RESOURCES = 1
MIN_WRITER_SCRIPTS = 1

# Genuine `TYPE/NAME` queries, if one is ever needed. Empty, and it has always been empty.
LITERAL_EXEMPT: dict[str, str] = {}

# --------------------------------------------------------------------------------------------
# Shell source handling
# --------------------------------------------------------------------------------------------

COMMENT_LINE = re.compile(r"^\s*#")


def strip_comments(src: str) -> str:
    """Drop whole-line shell comments, keeping line numbers intact.

    Load-bearing, and for the same reason `pause-is-not-scale-to-zero.py` strips Go comments: the
    scripts that got these rules right are the ones that EXPLAIN them, and the explanation
    necessarily contains the forbidden text. `brake-fanout-l2.sh` documents both traps in its header
    -- including the literal phrase `kubectl delete ns` -- and a check that failed on the
    documentation would be teaching people to delete the documentation.

    Whole-line only. A trailing `# ...` after code is not stripped, because deciding whether a `#`
    is a comment or part of a string needs a shell parser, and guessing wrong in the permissive
    direction is how a scanner develops a blind spot someone can park a defect in. The cost is a
    false positive on `foo   # see: kubectl delete ns`, which is loud, fixable, and has never
    occurred.
    """
    return "\n".join("" if COMMENT_LINE.match(ln) else ln for ln in src.split("\n"))


def shell_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for root in SCRIPT_ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.sh")):
            out[str(p.relative_to(REPO))] = p.read_text()
    return out


# --------------------------------------------------------------------------------------------
# Property 1 -- auth can-i
# --------------------------------------------------------------------------------------------

CANI = re.compile(r"auth\s+can-i\s+(?P<rest>[^\n]*)")
FOR_LIST = re.compile(r"^\s*for\s+(?P<var>\w+)\s+in\s+(?P<list>.+?)(?:;|\s*$)", re.MULTILINE)
QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")
VAR_REF = re.compile(r"\$\{?(\w+)\}?")
# The `*/*)` case arm that refuses a malformed resource. Written loosely enough to survive
# reformatting and tightly enough that a bare `*)` default arm does not satisfy it.
SLASH_GUARD = re.compile(r"\*\s*/\s*\*\s*\)")

# Flags of `kubectl auth can-i` that take their value as the NEXT token. Without this the value is
# read as a positional -- `-n $NSX` looked like a computed resource on the first draft of this file,
# which would have demanded a runtime guard from three scripts that never compute a resource at all.
# An unlisted separate-form flag degrades to a spurious positional: a false positive, which is loud.
VALUE_FLAGS = {
    "-n", "--namespace", "--as", "--as-group", "--as-uid", "--subresource", "--context",
    "--cluster", "--user", "--kubeconfig", "--server", "-o", "--output", "--request-timeout",
}


def _tokens(rest: str) -> list[str]:
    """Split the tail of an `auth can-i` command into shell-ish tokens, minus redirections."""
    rest = rest.split("2>")[0].split("|")[0]
    rest = rest.rstrip(")").rstrip('"').rstrip()
    return [t for t in re.split(r"\s+", rest) if t]


def _positionals(toks: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("-"):
            i += 2 if t in VALUE_FLAGS else 1
            continue
        out.append(t)
        i += 1
    return out


def for_lists(src: str) -> dict[str, list[str]]:
    """`for VAR in ...` -> the literal items, when the list is statically enumerable.

    `for pair in "impersonate users" "escalate roles.rbac..."` feeding `auth can-i $pair` is an
    established idiom here (verify-phase2, verify-phase3), and a line-scoped scan cannot see into
    it. Resolving the list turns those sites back into literal ones, which is both stricter than
    demanding a runtime guard from them and closer to what is actually being asserted.
    """
    out: dict[str, list[str]] = {}
    for fm in FOR_LIST.finditer(src):
        raw = fm.group("list").strip()
        items = [a or b for a, b in QUOTED.findall(raw)]
        if not items and not any(c in raw for c in "$`*"):
            items = raw.split()  # `for v in get list watch; do`
        if items:
            out.setdefault(fm.group("var"), []).extend(items)
    return out


def _expand(positionals: list[str], lists: dict[str, list[str]]) -> list[list[str]] | None:
    """Every concrete word-list this invocation can produce, or None if it cannot be resolved."""
    results: list[list[str]] = [[]]
    for tok in positionals:
        bare = tok.strip("\"'")
        v = VAR_REF.fullmatch(bare)
        if v and v.group(1) in lists:
            results = [r + item.split() for item in lists[v.group(1)] for r in results]
        elif "$" in bare or "`" in bare:
            return None
        else:
            results = [r + [bare] for r in results]
    return results


def check_can_i(sources: dict[str, str]) -> tuple[list[str], int]:
    failures: list[str] = []
    sites = 0

    for name, raw in sorted(sources.items()):
        src = strip_comments(raw)
        matches = list(CANI.finditer(src))
        if not matches:
            continue
        lists = for_lists(src)
        unresolved: list[str] = []

        for m in matches:
            sites += 1
            call = f"auth can-i {m.group('rest').strip()}"
            expansions = _expand(_positionals(_tokens(m.group("rest"))), lists)

            # 1b: a resource this file cannot resolve must be refused by the script at runtime.
            if expansions is None:
                unresolved.append(call)
                continue

            # 1a: no positional word may contain a slash, however it got there.
            for words in expansions:
                for w in words:
                    if "/" not in w or w in LITERAL_EXEMPT:
                        continue
                    sub = w.split("/", 1)[1]
                    via = "" if call.find(w) >= 0 else " (reached via a `for`-list)"
                    failures.append(
                        f"{name}: `{call}`{via} puts `{w}` in a positional slot. kubectl parses a "
                        f"positional `TYPE/NAME`, not `TYPE/SUBRESOURCE`, so this asks about an "
                        f"object NAMED `{sub}`. Pass `--subresource={sub}` instead. On a negative "
                        f"assertion the form is VACUOUSLY GREEN: the `no` comes from a resource "
                        f"name nobody was granted, not from the policy under test (LSN-044)"
                    )

        if unresolved and not SLASH_GUARD.search(src):
            failures.append(
                f"{name}: {len(unresolved)} `auth can-i` site(s) name a resource this check cannot "
                f"resolve (e.g. `{unresolved[0]}`), and the script carries no `*/*)` guard. A "
                f"computed resource cannot be checked statically, so the script must refuse the "
                f"malformed shape itself -- see `can()` in dev/verify/brake-fanout-l2.sh. Without "
                f"one, property 1a is evaded by the ordinary refactor of hoisting a repeated query "
                f"into a helper, which is exactly how it was evaded here (LSN-044)"
            )

    return failures, sites


# --------------------------------------------------------------------------------------------
# Property 2 -- protected resources vs namespace deletion
# --------------------------------------------------------------------------------------------

VAP_DOC = re.compile(r"^kind:\s*ValidatingAdmissionPolicy\s*$", re.MULTILINE)
RULE_CHUNK = re.compile(r"-\s*apiGroups:")
OPERATIONS = re.compile(r"operations:\s*\[([^\]]*)\]")
RESOURCES = re.compile(r"resources:\s*\[([^\]]*)\]")
CRD_KIND = re.compile(r"^    kind:\s*(\w+)\s*$", re.MULTILINE)
CRD_PLURAL = re.compile(r"^    plural:\s*(\w+)\s*$", re.MULTILINE)

# How a script says "delete a namespace". `--all` sweeps and label-selector deletes are not here:
# they do not remove the namespace, so the object survives in a namespace that still terminates.
NS_DELETE = re.compile(r"delete\s+(?:-[\w-]+(?:=\S+)?\s+)*(?:ns|namespace|namespaces)\b")


def protected_resources(policy_text: dict[str, str]) -> set[str]:
    """Every resource any ValidatingAdmissionPolicy matches for DELETE.

    Derived rather than listed: this is the single definition site for "the API server will refuse
    to delete one of these", and a second retention policy over a second resource should start
    being enforced here on the day it lands, not on the day someone remembers to edit this file.
    """
    found: set[str] = set()
    for text in policy_text.values():
        if not VAP_DOC.search(text):
            continue
        chunks = RULE_CHUNK.split(text)[1:]
        for chunk in chunks:
            ops = OPERATIONS.search(chunk)
            res = RESOURCES.search(chunk)
            if not ops or not res:
                continue
            if "DELETE" not in ops.group(1).upper():
                continue
            for r in re.findall(r"[\w.\-]+", res.group(1)):
                if "/" in r:  # a subresource is not separately deletable
                    continue
                found.add(r)
    return found


def kinds_for(plurals: set[str], crds: dict[str, str]) -> dict[str, str]:
    """plural -> Kind, read out of the CRD that declares the plural."""
    out: dict[str, str] = {}
    for text in crds.values():
        k = CRD_KIND.search(text)
        p = CRD_PLURAL.search(text)
        if k and p and p.group(1) in plurals:
            out[p.group(1)] = k.group(1)
    return out


def check_namespace_lifecycle(
    sources: dict[str, str], policies: dict[str, str], crds: dict[str, str]
) -> tuple[list[str], set[str], list[str]]:
    failures: list[str] = []
    plurals = protected_resources(policies)
    kinds = kinds_for(plurals, crds)

    unresolved = sorted(plurals - set(kinds))
    if unresolved:
        failures.append(
            f"a policy denies DELETE on {unresolved} and no CRD in config/crd/bases declares that "
            f"plural. Either the policy names a resource that does not exist -- in which case it "
            f"protects nothing -- or `make manifests` has not run. Both make the derivation below "
            f"silently narrower than the policy set it is supposed to mirror"
        )

    writers: list[str] = []
    for name, raw in sorted(sources.items()):
        src = strip_comments(raw)
        created = sorted({k for k in kinds.values() if re.search(rf"kind:\s*{k}\b", src)})
        if not created:
            continue
        writers.append(name)
        m = NS_DELETE.search(src)
        if m:
            failures.append(
                f"{name}: creates {', '.join(created)} and also runs `{m.group(0)}`. A "
                f"ValidatingAdmissionPolicy denies DELETE of those objects to the namespace "
                f"controller, so the namespace will terminate forever and the script cannot be "
                f"re-run. Reuse the namespace and delete only the objects inside it; mint "
                f"identifiers per run; select Events by `involvedObject.uid` so residue cannot "
                f"satisfy an assertion. Do NOT patch `status.exported.confirmed` to free it -- "
                f"that forges the field 05 §1.2 makes the durable record (LSN-045)"
            )

    return failures, plurals, writers


# --------------------------------------------------------------------------------------------

def check(
    sources: dict[str, str], policies: dict[str, str], crds: dict[str, str]
) -> tuple[list[str], dict[str, object]]:
    failures: list[str] = []

    cani_failures, sites = check_can_i(sources)
    failures.extend(cani_failures)

    ns_failures, plurals, writers = check_namespace_lifecycle(sources, policies, crds)
    failures.extend(ns_failures)

    # Non-vacuity. Every property above is asserted over a set this file discovers, so a discovery
    # that finds nothing reports success -- the exact shape LSN-036 is about.
    if len(sources) < MIN_SCRIPTS:
        failures.append(
            f"VACUOUS: found {len(sources)} shell scripts under {[str(r.relative_to(REPO)) for r in SCRIPT_ROOTS]}, "
            f"expected at least {MIN_SCRIPTS}. The discovery is broken, not the tree"
        )
    if sites < MIN_CANI_SITES:
        failures.append(
            f"VACUOUS: found {sites} `auth can-i` sites, expected at least {MIN_CANI_SITES}. "
            f"Property 1 is asserting over almost nothing; fix the scanner, not the floor"
        )
    if len(plurals) < MIN_PROTECTED_RESOURCES:
        failures.append(
            f"VACUOUS: derived {len(plurals)} DELETE-protected resources from "
            f"config/policy/*.yaml, expected at least {MIN_PROTECTED_RESOURCES}. Property 2 is "
            f"asserting nothing -- either the retention policy was removed (a finding in its own "
            f"right) or the parser stopped matching"
        )
    if len(writers) < MIN_WRITER_SCRIPTS:
        failures.append(
            f"VACUOUS: no script creates a DELETE-protected object, so property 2 has no subject. "
            f"It had one when this check was written (dev/verify/brake-fanout-l2.sh). A property "
            f"with no subject passes forever"
        )

    return failures, {"scripts": len(sources), "sites": sites, "plurals": sorted(plurals), "writers": writers}


def read_all() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    sources = shell_sources()
    policies = {p.name: p.read_text() for p in sorted(POLICY_DIR.glob("*.yaml"))} if POLICY_DIR.exists() else {}
    crds = {p.name: p.read_text() for p in sorted(CRD_DIR.glob("*.yaml"))} if CRD_DIR.exists() else {}
    return sources, policies, crds


BRAKE = "dev/verify/brake-fanout-l2.sh"
PH3 = "dev/verify/verify-phase3.sh"


def negative_control() -> int:
    sources, policies, crds = read_all()

    def with_script(s: dict[str, str], name: str, text: str) -> dict[str, str]:
        return {**s, name: text}

    # Each mutation carries the SIGNAL its finding must contain -- the property it targets, not
    # merely "something failed". A control that only asks whether the failure list is non-empty
    # cannot tell a mutation caught by its own property from one intercepted by a broader one, and
    # the narrow property underneath then accumulates controls that never execute it (LSN-035).
    # `dev/tests/negative-controls-name-their-rule.py` enforces this shape across the corpus.
    mutations = [
        (
            "a subresource is passed positionally to auth can-i",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace("auth can-i get nodes --as=$SA", "auth can-i get nodes/status --as=$SA", 1),
                ),
                p,
                c,
            ),
            "`nodes/status` in a positional slot",
        ),
        (
            "the same defect hidden one indirection out, in a for-list",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        'for pair in "impersonate users"',
                        'for pair in "patch actionrecords/status" "impersonate users"',
                        1,
                    ),
                ),
                p,
                c,
            ),
            "`actionrecords/status` in a positional slot",
        ),
        (
            "a slash reaches the resource slot only after a flag whose value was misread",
            # Guards the tokenizer itself: if VALUE_FLAGS ever swallows a token it should not, the
            # resource moves out of the slot this check inspects and 1a goes quiet.
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        "auth can-i get pods --as=$SA -n kube-system",
                        "auth can-i get pods --as=$SA -n kube-system pods/status",
                        1,
                    ),
                ),
                p,
                c,
            ),
            "`pods/status` in a positional slot",
        ),
        (
            "the runtime guard on the computed resource is removed",
            lambda s, p, c: (
                with_script(s, BRAKE, re.sub(r"\*/\*\)", "__removed__)", s[BRAKE], count=1)),
                p,
                c,
            ),
            "brake-fanout-l2.sh: 1 `auth can-i` site(s) name a resource this check cannot resolve",
        ),
        (
            "a resolvable resource is hoisted into a variable, evading 1a, with no guard added",
            lambda s, p, c: (
                with_script(
                    s,
                    PH3,
                    s[PH3].replace(
                        "auth can-i get nodes --as=$SA",
                        'auth can-i get "$RES" --as=$SA',
                        1,
                    ),
                ),
                p,
                c,
            ),
            "verify-phase3.sh: 1 `auth can-i` site(s) name a resource this check cannot resolve",
        ),
        (
            "an L2 suite that creates ActionRecords deletes its namespace",
            lambda s, p, c: (
                with_script(s, BRAKE, s[BRAKE] + '\n$K delete ns "$NS" --ignore-not-found\n'),
                p,
                c,
            ),
            "creates ActionRecord and also runs `delete ns`",
        ),
        (
            "the same, spelled `delete namespace` with a flag in between",
            lambda s, p, c: (
                with_script(s, BRAKE, s[BRAKE] + '\n$K delete --wait=false namespace "$NS"\n'),
                p,
                c,
            ),
            "runs `delete --wait=false namespace`",
        ),
        (
            "a NEW script creates the protected kind and cleans up the old way",
            lambda s, p, c: (
                with_script(
                    s,
                    "dev/verify/some-future-suite.sh",
                    "#!/usr/bin/env bash\n$K apply -f - <<EOF\nkind: ActionRecord\nEOF\n"
                    '$K delete ns "$NS"\n',
                ),
                p,
                c,
            ),
            "some-future-suite.sh: creates ActionRecord",
        ),
        (
            "the retention policy stops matching DELETE, so the protected set empties",
            lambda s, p, c: (
                s,
                {k: v.replace('operations: ["DELETE"]', 'operations: ["UPDATE"]') for k, v in p.items()},
                c,
            ),
            "VACUOUS: derived 0 DELETE-protected resources",
        ),
        (
            "the policy protects a resource no CRD declares",
            lambda s, p, c: (
                s,
                {k: v.replace('resources: ["actionrecords"]', 'resources: ["ghostrecords"]') for k, v in p.items()},
                c,
            ),
            "denies DELETE on ['ghostrecords'] and no CRD",
        ),
        (
            "the script discovery finds nothing",
            lambda s, p, c: ({BRAKE: s[BRAKE]}, p, c),
            "VACUOUS: found 1 shell scripts",
        ),
        (
            "comment stripping is disabled, so the documentation fails the check",
            # The inverse control: it proves strip_comments is load-bearing rather than decorative.
            # Asserted by construction below rather than by mutating the sources.
            #
            # It lands on property 2, not property 1, and the signal is how that was discovered. The
            # prose spelling of the can-i trap is `<verb> <type>/<thing>` in backticks, and a
            # backtick means command substitution, so `_expand` correctly declines to resolve it.
            # Only the `kubectl delete ns` prose in the LSN-045 header actually trips a property.
            # A control that asked only "did something fail" would have recorded this as covering
            # both, which is the whole of LSN-035 in one line.
            None,
            "creates ActionRecord and also runs `delete ns`",
        ),
    ]

    clean, _ = check(sources, policies, crds)
    if clean:
        print("FAIL: the negative control cannot run -- the check is already failing on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors = []
    for label, mutate, signal in mutations:
        if mutate is None:
            # The comment-stripping control, run directly: the real tree must fail if comments are
            # NOT stripped, which is what makes stripping them a decision rather than a habit.
            found = check({k: v.replace("#", " ") for k, v in sources.items()}, policies, crds)[0]
        else:
            ms, mp, mc = mutate(dict(sources), dict(policies), dict(crds))
            if (ms, mp, mc) == (sources, policies, crds):
                survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
                continue
            found = check(ms, mp, mc)[0]
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:100]}...)"
            )

    if survivors:
        print("FAIL: cluster-check-hygiene negative control -- these breakages were NOT caught:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(f"PASS: cluster-check-hygiene negative control -- all {len(mutations)} breakages caught")
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    sources, policies, crds = read_all()
    failures, stats = check(sources, policies, crds)

    if failures:
        print("FAIL: cluster-check-hygiene (LSN-044, LSN-045)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: cluster-check-hygiene (L0) -- {stats['sites']} `auth can-i` sites across "
        f"{stats['scripts']} shell scripts name a resource and never a subresource, every computed "
        f"resource is guarded, and the {len(stats['writers'])} script(s) that create "
        f"DELETE-protected objects ({', '.join(stats['plurals'])}) delete no namespace"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
