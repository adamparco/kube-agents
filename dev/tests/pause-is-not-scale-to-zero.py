#!/usr/bin/env python3
"""V-RUN-012 (the L0 half): `pause` is structurally not a scale-to-zero.

09 lists V-RUN-012 at `L0, L2`. The L2 half runs a real pause/resume cycle against a cluster and
asserts the observable clause -- the Deployment's replicas, the pod UID, and its start time are all
unchanged, and the queue survives. That is the half that matters, and it needs a cluster.

This half asserts the SHAPE, and it is the half that is true before anything is deployed: the code
that decides an agent's replica count is not given the agent's brake state, so it cannot consult it.
A type-level argument rather than a behavioural one, which is why it survives refactors that a
golden or a table test would not notice.

Why this is worth a check at all. `resolveDeploymentReplicasAndStrategy` in manifest_helpers.go
ALREADY contains a scale-to-zero branch: `spec.deployment.scaleToZero`, an unrelated idling feature
that legitimately renders `replicas: 0`. So the change V-RUN-012 forbids is not an exotic one. It is
three lines away from code that already does exactly that, it is a single `||`, and it reads in a
diff as tidy reuse:

    if deployment.ScaleToZero != nil && *deployment.ScaleToZero { replicas = 0 }
    if operations.Paused != nil && *operations.Paused { replicas = 0 }   # <-- this

It would pass every existing test that does not specifically render a paused agent. It would look
like a performance win. And it would break the one thing 08 §2.4 and 06 §4.4 both promise about
pause: the pod keeps running, keeps its work queue, and keeps being able to say why it is refusing
-- so `resume` is a released brake rather than a cold start, and a human who paused for thirty
seconds during a deploy does not get an agent that came back having forgotten what it was doing.

Five properties:

  1. THE REPLICA DECIDER CANNOT SEE THE BRAKE. `resolveDeploymentReplicasAndStrategy` takes exactly
     one parameter, of type `*agentv1alpha1.DeploymentSpec`. `OperationsSpec` is not reachable from
     `DeploymentSpec`, so the function is structurally incapable of consulting `paused`. Widening
     the signature to take the whole Agent is the change this catches.
  2. NOTHING ELSE DECIDES REPLICAS. `Replicas:` is assigned in exactly one place, from that
     function's result. A second assignment site is a second decision, and the second one is where
     the brake gets wired in. This one property holds over EVERY controller source, exempt or not,
     because it is the concrete damage rather than a proxy for it.
  3. NO BRAKE FIELD IS READ IN THE RENDERING PATH. `Paused`, `PauseReason`, `DryRunOnly` and
     `FrozenBy` do not appear in the three files that turn an Agent into a workload. The brake
     belongs in the broker's refusal path; a renderer that reads it is a renderer that can act on it.
  4. THE PROPERTY TEST EXISTS. `TestPauseDoesNotChangeTheRenderedDeployment` and its negative
     control `TestScaleToZeroStillWorks` are present. This check asserts shape and cannot observe
     behaviour; deleting the test that observes behaviour must not be silent.
  5. EVERY CONTROLLER SOURCE IS CLASSIFIED. Each non-test file in `internal/controller` is either a
     rendering source (property 3 applies) or carries a written exemption saying why it may read the
     brake. Without this the check rots in the safe-looking direction: a new renderer added under a
     new filename would simply not be scanned, and the check would keep printing PASS.

Why property 5 rather than "scan everything". Some controllers MUST read the brake -- the reconciler
that mirrors `spec.operations` into `status.operations` is the whole point of the field, and the undo
controller reads it to refuse. A check that failed on those would be weakened by the very next unit
that needs one, and a check weakened under deadline is a check deleted slowly. So the exemption is a
first-class, named, reasoned entry rather than a `# noqa`, and adding one is a visible diff.

Self-test (the `¬` of 09 §6): run with `--negative-control` to apply each of five failures to a copy
of the source in memory and confirm this check reports it. A check nobody has watched fail is a check
nobody knows works -- and property 1 in particular is the kind that passes vacuously if the regex
stops matching, which is exactly how a lint dies quietly (LSN-035).

Run:  python3 dev/tests/pause-is-not-scale-to-zero.py
      python3 dev/tests/pause-is-not-scale-to-zero.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTROLLER = REPO / "k8s-operator" / "internal" / "controller"
HELPERS = CONTROLLER / "manifest_helpers.go"
MANIFESTS = CONTROLLER / "agent_manifests.go"
PROPERTY_TEST = CONTROLLER / "pause_not_scale_to_zero_test.go"

# The function that decides the replica count, and the only parameter list it may have.
DECIDER = "resolveDeploymentReplicasAndStrategy"
DECIDER_SIG = re.compile(
    r"func\s+" + DECIDER + r"\s*\(([^)]*)\)",
)
ALLOWED_PARAM_TYPE = "*agentv1alpha1.DeploymentSpec"

# Every assignment of a Deployment's replica count in the rendering path.
REPLICAS_ASSIGN = re.compile(r"^\s*Replicas:\s*(.+?),?\s*$", re.MULTILINE)
ALLOWED_REPLICAS_RHS = {"&replicas"}

# Brake fields. Reading any of them while rendering a workload is the mistake, whatever it is then
# used for -- a renderer that can see the brake is one `if` away from acting on it.
BRAKE_FIELDS = {
    "Paused": "the brake itself (03 §6): a paused agent keeps its pod (08 §2.4)",
    "PauseReason": "brake state; the reason is surfaced by the broker, not baked into a workload",
    "DryRunOnly": "shadow mode is a broker-side refusal, not a different pod (06 §1.1)",
    "FrozenBy": "a FleetFreeze stops execution without modifying any agent (06 §4.4)",
}
# `.Operations` reached from the renderer at all. Kept separate from the field names because it
# catches the wiring one step before the read.
OPERATIONS_ACCESS = re.compile(r"\.Spec\.Operations\b")

REQUIRED_TESTS = (
    "TestPauseDoesNotChangeTheRenderedDeployment",
    "TestScaleToZeroStillWorks",
)

# The files that turn an Agent into a workload. The brake must not appear in any of them.
RENDERING = {
    "manifest_helpers.go": "decides replicas, strategy, resources -- the exact surface V-RUN-012 is about",
    "agent_manifests.go": "renders the Deployment, Service and ConfigMap the agent runs as",
    "pod_launcher.go": "creates agent pods directly; a brake read here rolls the pod just as surely",
}

# Controllers that may legitimately read `spec.operations`, each with the reason. Property 2 still
# applies to these: reading the brake is allowed, turning it into a replica count is not.
EXEMPT = {
    "agent_controller.go": "mirrors spec.operations into status.operations (06 §1.1) -- reading the "
    "brake to REPORT it is the field's purpose",
    "journal_reconciler.go": "reconciles the append-only journal; brake state is journalled context",
    "retention_controller.go": "prunes journal records; unrelated to the brake but must stay classified",
    "undo_controller.go": "reverses an executed action (05 §1.3) -- it reads the brake's own objects "
    "(UndoRequest, ActionRecord) and writes only their status plus an advisory annotation on the "
    "target; it renders no workload, so property 3 has nothing to bite on here",
}


def controller_sources() -> list[pathlib.Path]:
    return sorted(p for p in CONTROLLER.glob("*.go") if not p.name.endswith("_test.go"))


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments.

    Load-bearing: every one of these files EXPLAINS the rule in prose, and the prose necessarily
    contains the words the check forbids. Without this, the documentation that makes the rule
    survivable would be what fails it -- and the fix a hurried person reaches for is deleting the
    comment.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", src)


def check(sources: dict[str, str], test_src: str) -> list[str]:
    """Run all four properties over already-read sources, so the negative control can mutate them."""
    failures: list[str] = []
    helpers = sources.get(HELPERS.name, "")

    # 1. The replica decider cannot see the brake.
    sig = DECIDER_SIG.search(helpers)
    if not sig:
        failures.append(
            f"{HELPERS.name}: cannot find `func {DECIDER}(`. Either it was renamed or the replica "
            "decision moved; this check is asserting nothing until it is repointed"
        )
    else:
        params = [p.strip() for p in sig.group(1).split(",") if p.strip()]
        if len(params) != 1:
            failures.append(
                f"{HELPERS.name}: {DECIDER} takes {len(params)} parameters ({sig.group(1).strip()!r}), "
                "want exactly 1. Widening the signature is how the replica decision gains access to "
                "the brake (V-RUN-012)"
            )
        else:
            ptype = params[0].split(maxsplit=1)[-1]
            if ptype != ALLOWED_PARAM_TYPE:
                failures.append(
                    f"{HELPERS.name}: {DECIDER} takes a {ptype}, want {ALLOWED_PARAM_TYPE}. "
                    "OperationsSpec is not reachable from DeploymentSpec, and that unreachability is "
                    "the whole guarantee: the function cannot consult `paused` because it is not "
                    "given it (V-RUN-012)"
                )

    # 2. Nothing else decides replicas -- over EVERY controller source, exempt or not.
    for name, src in sources.items():
        for rhs in REPLICAS_ASSIGN.findall(strip_comments(src)):
            if rhs.strip() not in ALLOWED_REPLICAS_RHS:
                failures.append(
                    f"{name}: `Replicas: {rhs.strip()}` is a second replica decision. There is one, "
                    f"and it comes from {DECIDER}; a second site is where the brake gets wired into "
                    "the workload (V-RUN-012)"
                )

    # 3. No brake field is read in the rendering path.
    for name in sorted(RENDERING):
        body = strip_comments(sources.get(name, ""))
        if OPERATIONS_ACCESS.search(body):
            failures.append(
                f"{name}: reads `.Spec.Operations` while rendering the agent workload. The brake is "
                "enforced by the broker refusing envelopes, not by changing the pod -- a renderer "
                "that can see the brake is one `if` away from acting on it (V-RUN-012, 08 §2.4)"
            )
        for fld, why in BRAKE_FIELDS.items():
            if re.search(r"\.\s*" + fld + r"\b", body):
                failures.append(
                    f"{name}: reads the brake field `.{fld}` while rendering the agent workload: {why}"
                )

    # 5. Every controller source is classified.
    for name in sorted(sources):
        if name not in RENDERING and name not in EXEMPT:
            failures.append(
                f"{name}: is neither a declared rendering source nor a declared exemption. Add it to "
                "RENDERING in this file if it turns an Agent into a workload -- or to EXEMPT with the "
                "reason it may read the brake. An unclassified file is simply not scanned, which is "
                "how this check would keep printing PASS while the renderer it was written to guard "
                "moved somewhere else (V-RUN-012)"
            )
    for name in sorted(set(RENDERING) | set(EXEMPT)):
        if name not in sources:
            failures.append(
                f"{name}: is declared in this check but no longer exists in internal/controller. A "
                "stale entry silently narrows what gets scanned"
            )

    # 4. The property test exists.
    for want in REQUIRED_TESTS:
        if f"func {want}(" not in test_src:
            failures.append(
                f"{PROPERTY_TEST.name}: `{want}` is missing. This check asserts shape and cannot "
                "observe behaviour; the L1 property test is the half that renders a paused agent and "
                "compares it, and its negative control is what proves the renderer can produce a "
                "difference at all"
            )

    return failures


def read_sources() -> tuple[dict[str, str], str]:
    sources = {p.name: p.read_text() for p in controller_sources()}
    test_src = PROPERTY_TEST.read_text() if PROPERTY_TEST.exists() else ""
    return sources, test_src


def negative_control() -> int:
    """Break each property in memory and confirm this check notices."""
    sources, test_src = read_sources()
    mutations = [
        (
            "the replica decider is handed the whole Agent",
            lambda s, t: (
                {
                    **s,
                    HELPERS.name: s[HELPERS.name].replace(
                        f"func {DECIDER}(deployment *agentv1alpha1.DeploymentSpec)",
                        f"func {DECIDER}(agent *agentv1alpha1.Agent, deployment *agentv1alpha1.DeploymentSpec)",
                        1,
                    ),
                },
                t,
            ),
        ),
        (
            "a second replica decision appears in the renderer",
            lambda s, t: (
                {
                    **s,
                    MANIFESTS.name: s[MANIFESTS.name].replace(
                        "Replicas: &replicas,", "Replicas: &pausedReplicas,", 1
                    ),
                },
                t,
            ),
        ),
        (
            "the renderer reads the brake",
            lambda s, t: (
                {
                    **s,
                    MANIFESTS.name: s[MANIFESTS.name].replace(
                        "replicas, strategy := ",
                        "_ = agent.Spec.Operations.Paused\n\treplicas, strategy := ",
                        1,
                    ),
                },
                t,
            ),
        ),
        (
            "the L1 property test is deleted",
            lambda s, t: (s, t.replace(f"func {REQUIRED_TESTS[0]}(", "func disabled(", 1)),
        ),
        (
            "the renderer moves to a file this check does not know about",
            lambda s, t: ({**s, "workload_renderer.go": "package controller\n"}, t),
        ),
    ]

    clean = check(sources, test_src)
    if clean:
        print(
            "FAIL: the negative control cannot run -- the check is already failing on the real tree:",
            file=sys.stderr,
        )
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    survivors = []
    for label, mutate in mutations:
        ms, mt = mutate(dict(sources), test_src)
        if ms == sources and mt == test_src:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        if not check(ms, mt):
            survivors.append(label)

    if survivors:
        print("FAIL: V-RUN-012 negative control -- these breakages were NOT caught:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(f"PASS: V-RUN-012 negative control -- all {len(mutations)} breakages caught")
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    for required in (HELPERS, MANIFESTS, PROPERTY_TEST):
        if not required.exists():
            print(f"FAIL: V-RUN-012 -- {required.relative_to(REPO)} does not exist", file=sys.stderr)
            return 1

    sources, test_src = read_sources()
    failures = check(sources, test_src)

    if failures:
        print("FAIL: V-RUN-012 -- `pause` is not structurally distinct from scale-to-zero", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS: V-RUN-012 (L0) -- {len(RENDERING)} rendering sources carry no brake field, "
        f"{DECIDER} is not given one, {len(sources)} controller sources hold a single replica "
        "decision, and the L1 property test and its negative control are present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
