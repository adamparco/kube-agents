#!/usr/bin/env python3
"""V-BRK-012 (L0): one broker per Agent CR, and no fleet-wide writer anywhere.

03 §3.1 bounds the blast radius of a broker compromise at exactly one scope. That bound is a
statement about deployment topology, and it is only true while no second broker exists with a wider
one. A single shared broker serving the fleet is a strictly more convenient design -- one
Deployment, one certificate, one rollout, one place to look at logs -- and adopting it would make
the blast radius the union of every scope in the cluster while every other check in this repo stayed
green. Nothing in the envelope schema, the anti-replay window or the journal would notice.

So the property has to be checked at the shape of the renderer, not at its output.

The Go half (internal/controller/broker_manifests_test.go) renders TWO co-located Agent CRs and
proves their brokers do not collide -- distinct names, distinct actor ServiceAccounts,
non-overlapping Service selectors. That is the value-level check and it is the stronger one, but it
cannot see a renderer that has stopped depending on the CR at all in some THIRD place: a helper that
returns a constant, a Secret name pinned in a manifest, a second call site that spells
`kage-broker` where it should call brokerName(agent). This file is the source-level companion.

Four properties:

  1. EVERY BROKER NAME IS A FUNCTION OF THE CR. The five derivations (brokerName,
     brokerTLSSecretName, agentMeshTLSSecretName, brokerEndpoint, actorServiceAccountName) each take
     an *Agent and each reach agent.Name or a function that does. A derivation that stops taking the
     CR is a singleton with a parameter list.
  2. NOBODY ELSE SPELLS A BROKER OBJECT NAME. Production Go outside the definition site must not
     build a `-broker`, `-broker-tls` or `-mesh-tls` name from a literal. The second speller is
     always the cheap one -- `agent.Name + "-broker"` inline at a call site -- and it is invisible
     in review precisely because it is correct on the day it is written.
  3. THE BROKER IS SINGLE-REPLICA AND ITS SERVICE PINS THE AGENT. Two replicas would split 06 §4.1's
     single-use nonce window across two processes, and a Service selector that pins only
     `role: actor` would balance one agent's envelopes into a co-located agent's broker -- a scope
     escape that presents as load balancing.
  4. NON-VACUITY. The definition site exists, the launcher's interface returns a PAIR rather than a
     single Deployment, and the reconciler actually calls it. A check whose subject has been
     refactored out from under it prints PASS forever (LSN-035).

Exemptions are NAMED AND REASONED in the tables below, never a `# noqa`, for the reason
scope-label-single-sourced.py gives: a check with no legitimate way to say "this one is fine" gets
weakened by the first person who needs one.

Self-test (the `¬` of 09 §6): `--negative-control` applies each of six plausible regressions to a
copy of the sources in memory and confirms this check reports every one.

Run:  python3 dev/tests/one-broker-per-agent.py
      python3 dev/tests/one-broker-per-agent.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from golex import strip_go_comments  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTROLLER = REPO / "k8s-operator" / "internal" / "controller"
DEFINITION_SITE = CONTROLLER / "broker_manifests.go"
LAUNCHER_SITE = CONTROLLER / "pod_launcher.go"
RECONCILER_SITE = CONTROLLER / "agent_controller.go"

# Property 1. Each derivation must take the CR and reach its name. The value is the helper it is
# allowed to reach through instead of `agent.Name` directly -- delegation is fine, a constant is not.
DERIVATIONS: dict[str, tuple[str, ...]] = {
    "brokerName": ("agent.Name",),
    "brokerTLSSecretName": ("agent.Name",),
    "agentMeshTLSSecretName": ("agent.Name",),
    "brokerEndpoint": ("brokerName(agent)",),
    "brokerSAN": ("brokerName(agent)",),
    # The actor SA is named from (tier, scope), NOT from the CR name -- 06 §5.1 is explicit, and
    # that is what makes two agents at the same scope impossible rather than merely discouraged
    # (admission enforces the uniqueness). So its dependency is the scope, and a constant here would
    # be just as much a fleet-wide writer as a constant broker name.
    "actorServiceAccountName": ("scope.Of(agent).Leaf()",),
}

# Property 2. Fragments that, spelled outside the definition site, mean somebody rebuilt a broker
# object name by hand.
#
# The SUFFIX form only. An earlier draft also matched any full literal ending in `-broker`, which
# flagged broker.TokenAudience (`"kubeagents-broker"`) -- a token audience, not an object name. The
# suffix form is what the realistic regression actually looks like, because the name is always built
# by concatenation:
#
#     dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-broker"}}
#
# What that leaves uncovered is a fully hardcoded name (`Name: "payments-broker"`), which this file
# does not catch. That gap is deliberate and it is covered elsewhere: a hardcoded name does not vary
# with the CR, so TestTwoAgentsInOneNamespaceRenderDistinctPairs and TestNoFleetWideBroker both fail
# on it at L0. Widening the regex to close it here would cost a false positive on every legitimate
# string that happens to end in `-broker`, and a check that cries wolf gets an exemption entry
# rather than a fix.
BROKER_NAME_LITERAL_RE = re.compile(r'"-(broker-tls|mesh-tls|broker)"')

# Production Go that may spell one anyway.
EXEMPT_SPELLING: dict[str, str] = {
    "broker_manifests.go": "IS the definition site",
}

# Property 3. Two regexes rather than one literal: the count must be 1 AND it must come from a
# `const`. A literal `ptr.To(int32(1))` satisfies the first alone, and the sibling V-RUN-012 check
# cares about the second -- a const cannot be derived from an Agent, so this site cannot quietly
# become the place `paused` turns into `replicas: 0`.
SINGLE_REPLICA_RE = re.compile(r"Replicas:\s*ptr\.To\(brokerReplicas\)")
REPLICA_CONST_RE = re.compile(r"^\s*brokerReplicas\s+int32\s*=\s*1\s*$", re.MULTILINE)
SELECTOR_PINS_AGENT_RE = re.compile(r"agentlabels\.Agent:\s*agent\.Name", re.DOTALL)
SELECTOR_PINS_ROLE_RE = re.compile(r"agentlabels\.Role:\s*agentlabels\.RoleActor", re.DOTALL)

# Property 4. The launcher must not offer a way to build one half.
PAIR_INTERFACE_RE = re.compile(r"BuildPair\(agent \*agentv1alpha1\.Agent[^)]*\) WorkloadPair")
PAIR_CONSTRUCTOR_GUARD_RE = re.compile(r"if broker == nil \|\| agent == nil \{\s*\n\s*panic\(")
SINGLE_HALF_INTERFACE_RE = re.compile(r"BuildDeployment\([^)]*\) \*appsv1\.Deployment")
RECONCILER_CALLS_PAIR_RE = re.compile(r"launcher\.BuildPair\(")


def tracked_go_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "k8s-operator"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / p for p in out.split("\0") if p.endswith(".go")]


def is_test_file(name: str) -> bool:
    """Tests SHOULD spell the literal.

    A test that compared brokerName(agent) against brokerName(agent) would pass after a rename that
    broke every NetworkPolicy in the mesh. The asymmetry is in the safe direction: a test that
    misspells a name fails loudly on its own assertion, while production code that misspells one
    produces a Service selecting nothing -- and a selector matching nothing is indistinguishable
    from a selector matching a legitimately empty set.
    """
    return name.endswith("_test.go")


def function_body(code: str, name: str) -> str | None:
    """The source of `func <name>(...)`, brace-matched. None when the function is absent."""
    start = re.search(r"^func " + re.escape(name) + r"\(", code, re.MULTILINE)
    if not start:
        return None
    i = code.index("{", start.start())
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[start.start() : j + 1]
    return None


def check(sources: dict[str, str]) -> list[str]:
    """All four properties over already-read sources, so the negative control can mutate them."""
    failures: list[str] = []

    definition = sources.get(DEFINITION_SITE.name, "")
    if not definition:
        return [
            f"{DEFINITION_SITE.relative_to(REPO)} is missing or empty. There is no broker renderer "
            "to check, so this file is asserting nothing about the cluster"
        ]
    definition_code = strip_go_comments(definition)

    # 1. Every broker name is a function of the CR.
    for fn, required in DERIVATIONS.items():
        body = function_body(definition_code, fn)
        if body is None:
            failures.append(
                f"{DEFINITION_SITE.name}: {fn} is gone. Every broker object name must be derived "
                "by a named function that takes the Agent CR; a name that has moved inline is a "
                "name that can stop varying with the CR without anything noticing (V-BRK-012)"
            )
            continue
        if "agent *agentv1alpha1.Agent" not in body:
            failures.append(
                f"{DEFINITION_SITE.name}: {fn} no longer takes an *Agent. A derivation that does "
                "not read the CR names the same object for every agent in the fleet -- which is "
                "the fleet-wide writer V-BRK-012 exists to forbid"
            )
        if not any(dep in body for dep in required):
            failures.append(
                f"{DEFINITION_SITE.name}: {fn} does not reach {' or '.join(required)}. It takes "
                "the CR but does not depend on it, so two Agents would resolve to one object "
                "(V-BRK-012, 03 §3.1)"
            )

    # 2. Nobody else spells a broker object name.
    for name, text in sorted(sources.items()):
        if name in EXEMPT_SPELLING or is_test_file(name):
            continue
        code = strip_go_comments(text)
        for lineno, line in enumerate(code.splitlines(), start=1):
            if BROKER_NAME_LITERAL_RE.search(line):
                failures.append(
                    f"{name}:{lineno} builds a broker object name from a literal. Call "
                    "brokerName/brokerTLSSecretName/agentMeshTLSSecretName instead, or add a "
                    "named, reasoned entry to EXEMPT_SPELLING in this file. A second speller is "
                    "how one agent's Service comes to select another agent's broker (08 §2.3)"
                )

    # 3. Single replica, and a selector that pins BOTH labels.
    broker_dep = function_body(definition_code, "buildBrokerDeployment") or ""
    if not SINGLE_REPLICA_RE.search(broker_dep):
        failures.append(
            f"{DEFINITION_SITE.name}: buildBrokerDeployment is no longer pinned to one replica. "
            "06 §4.1's single-use nonce window is per-process state, so a second replica makes a "
            "nonce spent on one broker unspent on the other -- replay protection that holds only "
            "when the load balancer cooperates"
        )
    if not REPLICA_CONST_RE.search(definition_code):
        failures.append(
            f"{DEFINITION_SITE.name}: `brokerReplicas` is no longer a `const int32 = 1`. As a var "
            "or a function result it becomes derivable from the Agent, and the second workload in "
            "this package is the obvious place to implement `pause` as a scale-to-zero -- which "
            "V-RUN-012 forbids because a killed broker cannot tell the agent why it is refusing"
        )
    broker_svc = function_body(definition_code, "buildBrokerService") or ""
    if not SELECTOR_PINS_AGENT_RE.search(broker_svc):
        failures.append(
            f"{DEFINITION_SITE.name}: the broker Service selector does not pin the agent label. "
            "`role: actor` alone selects every co-located broker (08 §2.6 puts a platform and a "
            "cluster-admin broker in one namespace), so envelopes would round-robin into another "
            "agent's scope"
        )
    if not SELECTOR_PINS_ROLE_RE.search(broker_svc):
        failures.append(
            f"{DEFINITION_SITE.name}: the broker Service selector does not pin role=actor. "
            "`agent: <name>` alone also selects the READER pod, which has no envelope listener"
        )

    # 4. Non-vacuity: the pair seam still exists and is still called.
    launcher = strip_go_comments(sources.get(LAUNCHER_SITE.name, ""))
    if not launcher:
        failures.append(f"VACUOUS: {LAUNCHER_SITE.name} is missing; the launcher seam is unchecked")
    else:
        if not PAIR_INTERFACE_RE.search(launcher):
            failures.append(
                f"{LAUNCHER_SITE.name}: the PodLauncher interface no longer returns a WorkloadPair. "
                "08 §2.4(a) requires that 'launch an agent' not be an expressible operation; an "
                "interface that hands back one Deployment makes it expressible again"
            )
        if SINGLE_HALF_INTERFACE_RE.search(launcher):
            failures.append(
                f"{LAUNCHER_SITE.name}: a method returning a single *appsv1.Deployment is back. "
                "That is the half-pair operation 08 §2.4(a) forbids -- an agent with no broker is "
                "degraded, but a broker with no agent is an unattended write credential"
            )
        if not PAIR_CONSTRUCTOR_GUARD_RE.search(launcher):
            failures.append(
                f"{LAUNCHER_SITE.name}: newWorkloadPair no longer rejects a nil half. The guard is "
                "what makes the unpaired state unrepresentable rather than merely discouraged"
            )

    reconciler = strip_go_comments(sources.get(RECONCILER_SITE.name, ""))
    if not RECONCILER_CALLS_PAIR_RE.search(reconciler):
        failures.append(
            f"VACUOUS: {RECONCILER_SITE.name} does not call launcher.BuildPair. The renderer may be "
            "correct and unreached -- which is exactly the state a botched refactor leaves behind, "
            "and the state in which every property above is true of code the cluster never runs"
        )

    return failures


def read_sources() -> dict[str, str]:
    """Every tracked Go file, plus the controller package read straight off disk.

    The package is read from disk rather than from `git ls-files` so this check is usable on the
    working tree that is introducing it -- a check that only becomes runnable after the commit it is
    meant to gate is a check that gates nothing.
    """
    sources: dict[str, str] = {}
    paths = list(tracked_go_files())
    if CONTROLLER.is_dir():
        paths.extend(CONTROLLER.glob("*.go"))
    for path in paths:
        try:
            sources[path.name] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
    return sources


def negative_control() -> int:
    """Break each property in memory and confirm this check notices."""
    sources = read_sources()

    def edit(name: str, old: str, new: str):
        return lambda s: {**s, name: s[name].replace(old, new, 1)}

    # (label, mutate, signal). The signal names the property, not merely the fact of a failure. Six
    # properties read `broker_manifests.go` and two read `pod_launcher.go`; several would fire on
    # the same edit, so a non-emptiness assertion cannot tell which one is doing the work and a
    # property that stopped executing would go on reporting green ([[LSN-035]]).
    mutations = [
        (
            "brokerName returns a constant",
            edit(DEFINITION_SITE.name, 'return agent.Name + "-broker"', 'return "kage-broker"'),
            "brokerName does not reach agent.Name",
        ),
        (
            "the actor SA stops depending on the scope",
            edit(
                DEFINITION_SITE.name,
                "leaf := scope.Of(agent).Leaf()",
                'leaf := "shared"',
            ),
            "actorServiceAccountName does not reach scope.Of(agent).Leaf()",
        ),
        (
            "a second file spells the broker name inline",
            lambda s: {**s, "some_caller.go": 'package controller\n\nvar n = agent.Name + "-broker"\n'},
            "some_caller.go:3 builds a broker object name from a literal",
        ),
        (
            "the broker scales to two replicas",
            edit(DEFINITION_SITE.name, "Replicas: ptr.To(brokerReplicas)", "Replicas: ptr.To(int32(2))"),
            "buildBrokerDeployment is no longer pinned to one replica",
        ),
        (
            "the replica count stops being a const and becomes derivable",
            edit(DEFINITION_SITE.name, "brokerReplicas int32 = 1", "brokerReplicas = replicasFor(agent)"),
            "`brokerReplicas` is no longer a `const int32 = 1`",
        ),
        (
            "the Service selector drops the agent label",
            edit(
                DEFINITION_SITE.name,
                "Selector: map[string]string{\n\t\t\t\tagentlabels.Role:  agentlabels.RoleActor,\n\t\t\t\tagentlabels.Agent: agent.Name,\n\t\t\t}",
                "Selector: map[string]string{\n\t\t\t\tagentlabels.Role: agentlabels.RoleActor,\n\t\t\t}",
            ),
            "the broker Service selector does not pin the agent label",
        ),
        (
            "the launcher offers a single-Deployment method again",
            edit(
                LAUNCHER_SITE.name,
                "BuildPair(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) WorkloadPair",
                "BuildDeployment(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment",
            ),
            "a method returning a single *appsv1.Deployment is back",
        ),
        (
            "the pair constructor stops rejecting a nil half",
            edit(LAUNCHER_SITE.name, "if broker == nil || agent == nil {", "if false {"),
            "newWorkloadPair no longer rejects a nil half",
        ),
    ]

    clean = check(sources)
    if clean:
        print(
            "FAIL: the negative control cannot run -- the check is already failing on the real tree:",
            file=sys.stderr,
        )
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors: list[str] = []
    for label, mutate, signal in mutations:
        mutated = mutate(sources)
        if mutated == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        found = check(mutated)
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:120]}...)"
            )

    if survivors:
        print("FAIL: the check did not notice these regressions:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(
        f"PASS: negative control -- all {len(mutations)} injected regressions were caught, each by "
        f"the property it targets"
    )
    return 0


def main(argv: list[str]) -> int:
    if "--negative-control" in argv:
        return negative_control()

    failures = check(read_sources())
    if failures:
        print("FAIL: V-BRK-012 -- one broker per Agent CR, no fleet-wide writer", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: V-BRK-012 -- {len(DERIVATIONS)} broker-name derivations all depend on the Agent CR; "
        "the broker is single-replica with a two-label selector; the launcher seam returns a pair"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
