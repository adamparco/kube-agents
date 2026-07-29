#!/usr/bin/env python3
"""Every overlay an install path applies is rendered by something CI runs (LSN-042, general form).

`kustomize build config/default` was broken for a month. Deterministically, on every commit since
the mesh CA landed, with all eight required checks green the whole time -- because not one of them
rendered it. `make build` and `make test` were Go targets, `l0-checks.yml` installs no dependencies
and kustomize is a downloaded binary, `docker-build.yml` builds images, `k8s-operator-test.yml` ran
envtest. Each was telling the truth about a different thing. The sanctioned install path was the one
thing nothing exercised.

That specific case is closed: `make render` builds `config/install` and is a prerequisite of both
`build` and `test`, so `k8s-operator-test.yml` reaches it. This file is the **general** property,
which is the half LSN-042 left open:

    every kustomize directory an install path APPLIES must also be one `make render` BUILDS,
    and `render` must remain a prerequisite of a target CI actually invokes.

Both halves are load-bearing and neither implies the other. Extending `render` while quietly
dropping it from `test`'s prerequisites restores the original blind spot exactly, and the diff that
does it is one word long.

WHY THE SET IS DERIVED. When this was written, `render` covered `config/install` and nothing else,
while the Makefile applied four more overlays -- LiteLLM base, the ChatGPT overlay, inference-replay
and the GitHub integration. All four rendered cleanly, so the gap was luck; the next one will not
be. Listing five directories here would make this check a headcount of today's integrations
([[LSN-036]]), and integrations are precisely the thing that gets added by someone who is not
thinking about the render target. So both sides are read out of the Makefiles and scripts
themselves, and a sixth overlay is enforced on the day its `apply` line lands.

WHAT "APPLIES" MEANS, precisely: a `kustomize build <dir>` whose command also reaches an `apply`.
That filter is what keeps the property honest in both directions. It excludes the `config/crd` build
in the CRD-drift check, which is a comparison and not an install, and it excludes `render` itself --
otherwise every directory would satisfy the property by being in the recipe that is supposed to
prove it. It includes the `undeploy` targets' directories only when the same directory is applied
somewhere too, which it always is: an overlay you can delete but never installed is a different bug.

WHAT THIS DOES NOT COVER. `envsubst < x.yaml.template | kubectl apply` is an install path with no
builder, so there is no render to demand. Reachability of those is [[LSN-039]]'s
`identity-has-install-path.py`; structural validity of the substituted output needs the variables
and therefore a cluster, so it is L2's. The failure LSN-042 is about -- a *build* silently broken
for a month -- needs a build to be silently broken, and envsubst has no merge semantics, no
selectors and no inclusion graph to be wrong about.

Self-test (the `¬`): `--negative-control` applies each breakage in memory and confirms it is caught.

Run:  python3 dev/tests/install-artifacts-are-rendered.py
      python3 dev/tests/install-artifacts-are-rendered.py --negative-control
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
OPERATOR_MAKEFILE = "k8s-operator/Makefile"

# Non-vacuity floors: every set below is discovered, and a discovery that finds nothing passes.
MIN_APPLIED = 5
MIN_MAKEFILES = 2

# `render` earns its place only by hanging off a target CI invokes. Checked by name so that
# unhooking it is a failure here rather than a silent return to the original blind spot.
CI_REACHED_TARGETS = ("build", "test")

KUSTOMIZE_BUILD = re.compile(r"(?:\$\(KUSTOMIZE\)|\bkustomize)\s+build\s+(?P<dir>[\w./${}-]+)")
TARGET = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+):(?P<prereqs>[^=\n]*)$", re.M)


def _makefile_targets(text: str) -> dict[str, tuple[list[str], str]]:
    """target -> (prerequisites, recipe body). Recipes are the tab-indented lines beneath."""
    out: dict[str, tuple[list[str], str]] = {}
    lines = text.split("\n")
    starts = [(i, m) for i, ln in enumerate(lines) if (m := TARGET.match(ln))]
    for idx, (i, m) in enumerate(starts):
        body: list[str] = []
        for ln in lines[i + 1 :]:
            if ln.startswith("\t") or not ln.strip():
                body.append(ln)
            else:
                break
        out[m.group("name")] = (m.group("prereqs").split(), "\n".join(body))
    return out


def _sources() -> dict[str, str]:
    """Every tracked Makefile and shell script -- anything that could apply an overlay.

    `git ls-files` rather than a directory walk, for the reason `invariants-gate.py` gives: the
    corpus and the publishable set should be the same set, and `k8s-operator/scripts/vars.sh` is
    gitignored and holds live secrets.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*Makefile", "*.mk", "*.sh"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    out: dict[str, str] = {}
    for rel in listing.split("\0"):
        p = REPO / rel
        if rel and p.is_file():
            out[rel] = p.read_text()
    return out


def applied_dirs(sources: dict[str, str]) -> dict[str, str]:
    """dir -> where it is applied. A build whose command line also reaches an `apply`."""
    found: dict[str, str] = {}
    for name, text in sorted(sources.items()):
        for i, line in enumerate(text.split("\n"), 1):
            if "apply" not in line:
                continue
            for m in KUSTOMIZE_BUILD.finditer(line):
                found.setdefault(m.group("dir").rstrip("/"), f"{name}:{i}")
    return found


def rendered_dirs(sources: dict[str, str]) -> set[str]:
    mk = sources.get(OPERATOR_MAKEFILE, "")
    targets = _makefile_targets(mk)
    if "render" not in targets:
        return set()
    return {m.group("dir").rstrip("/") for m in KUSTOMIZE_BUILD.finditer(targets["render"][1])}


def check(sources: dict[str, str]) -> tuple[list[str], dict[str, object]]:
    failures: list[str] = []
    applied = applied_dirs(sources)
    rendered = rendered_dirs(sources)
    mk = sources.get(OPERATOR_MAKEFILE, "")
    targets = _makefile_targets(mk)

    if "render" not in targets:
        failures.append(
            f"{OPERATOR_MAKEFILE} has no `render` target. It is the only thing in this repository "
            f"that builds what the repository installs; without it `kustomize build` is unreachable "
            f"from CI and an unrenderable overlay is green until someone deploys it (LSN-042)."
        )
    else:
        for t in CI_REACHED_TARGETS:
            if t not in targets:
                failures.append(f"{OPERATOR_MAKEFILE}: no `{t}` target, so `render`'s route into CI cannot be checked.")
            elif "render" not in targets[t][0]:
                failures.append(
                    f"{OPERATOR_MAKEFILE}: `render` is not a prerequisite of `{t}`. "
                    f"`k8s-operator-test.yml` runs `make -C k8s-operator test`, and that "
                    f"prerequisite is the entire route from CI to a rendered install. Dropping it "
                    f"restores the month-long blind spot exactly, with every check still green "
                    f"(LSN-042)."
                )

    for d, where in sorted(applied.items()):
        if d not in rendered:
            failures.append(
                f"{where} applies `{d}` and `make render` never builds it. An overlay nothing "
                f"renders is broken silently: `kustomize build config/default` failed on every "
                f"commit for a month with all eight required checks green. Add it to the `render` "
                f"recipe in {OPERATOR_MAKEFILE} (LSN-042)."
            )

    if len(applied) < MIN_APPLIED:
        failures.append(
            f"VACUOUS: found {len(applied)} applied kustomize directories, expected at least "
            f"{MIN_APPLIED}. The scan is broken, not the tree -- this check then passes by "
            f"asserting nothing, which is the shape of the bug it exists for (LSN-035)."
        )
    if sum(1 for k in sources if k.endswith("Makefile") or k.endswith(".mk")) < MIN_MAKEFILES:
        failures.append(
            f"VACUOUS: fewer than {MIN_MAKEFILES} Makefiles were read; the enumeration broke."
        )

    return failures, {"applied": applied, "rendered": sorted(rendered)}


def negative_control() -> int:
    sources = _sources()
    clean, _ = check(sources)
    if clean:
        print("FAIL: the negative control cannot run -- the check already fails on the real tree:", file=sys.stderr)
        for f in clean:
            print(f"  - {f}", file=sys.stderr)
        return 1

    MK = OPERATOR_MAKEFILE
    # (label, mutate, signal). The signal names the property, not merely the fact of a failure. Four
    # properties overlap here -- the applied set, the rendered set, `render`'s two prerequisite
    # edges, and non-vacuity -- and deleting the `render` target trips all four at once, so a
    # non-emptiness assertion would let the three narrower mutations ride on the broadest one
    # ([[LSN-035]]).
    mutations = [
        (
            "a newly applied overlay is not added to `render`",
            lambda s: {**s, MK: s[MK] + (
                "\n.PHONY: deploy-newthing\ndeploy-newthing:\n"
                "\t$(KUSTOMIZE) build config/integrations/newthing | $(KUBECTL) apply -f -\n"
            )},
            "applies `config/integrations/newthing` and `make render` never builds it",
        ),
        (
            "an overlay is dropped from the `render` recipe",
            lambda s: {**s, MK: s[MK].replace(
                "@$(KUSTOMIZE) build config/install > /dev/null", "@true", 1)},
            "applies `config/install` and `make render` never builds it",
        ),
        (
            "`render` is unhooked from `test` -- the one-word regression",
            lambda s: {**s, MK: s[MK].replace(
                "test: manifests generate fmt vet render setup-envtest",
                "test: manifests generate fmt vet setup-envtest", 1)},
            "`render` is not a prerequisite of `test`",
        ),
        (
            "`render` is unhooked from `build`",
            lambda s: {**s, MK: s[MK].replace(
                "build: manifests generate fmt vet render",
                "build: manifests generate fmt vet", 1)},
            "`render` is not a prerequisite of `build`",
        ),
        (
            "the `render` target is deleted outright",
            lambda s: {**s, MK: re.sub(
                r"^render:.*\n(?:\t.*\n)*", "", s[MK], count=1, flags=re.M)},
            "has no `render` target",
        ),
        (
            "an install script applies an unrendered overlay outside the Makefile",
            lambda s: {**s, "dev/cluster/up.sh": s["dev/cluster/up.sh"]
                       + "\nkustomize build config/integrations/sidecar | kubectl apply -f -\n"},
            "dev/cluster/up.sh:",
        ),
        (
            "the scan stops finding applied overlays",
            lambda s: {k: v.replace("apply", "APPLY") for k, v in s.items()},
            "VACUOUS: found 0 applied kustomize directories",
        ),
    ]

    survivors = []
    for label, mutate, signal in mutations:
        m = mutate(dict(sources))
        if m == sources:
            survivors.append(f"{label} (the mutation did not apply -- its anchor text has moved)")
            continue
        found = check(m)[0]
        if not found:
            survivors.append(f"{label} (not caught at all)")
        elif not any(signal in f for f in found):
            survivors.append(
                f"{label} (caught, but not by the property it targets -- no finding mentions "
                f"{signal!r}; first finding was: {found[0][:120]}...)"
            )

    if survivors:
        print("FAIL: install-artifacts-are-rendered negative control -- NOT caught:", file=sys.stderr)
        for s in survivors:
            print(f"  - {s}", file=sys.stderr)
        return 1

    print(
        f"PASS: install-artifacts-are-rendered negative control -- all {len(mutations)} breakages "
        f"caught, each by the property it targets"
    )
    return 0


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()

    failures, stats = check(_sources())
    if failures:
        print("FAIL: install-artifacts-are-rendered (LSN-042)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    applied = stats["applied"]
    print(
        f"PASS: install-artifacts-are-rendered (L0) -- all {len(applied)} kustomize "
        f"director{'y' if len(applied) == 1 else 'ies'} an install path applies "
        f"({', '.join(sorted(applied))}) are built by `make render`, which is a prerequisite of "
        f"{' and '.join(CI_REACHED_TARGETS)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
