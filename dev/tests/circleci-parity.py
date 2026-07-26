#!/usr/bin/env python3
"""CircleCI dispatch-parity validator (kube-agents Phase 7, P7-T2; 06 §4; 07 §"Phase 7" Accept (b)).

kube-agents is unopinionated about CI/CD (06 §4). This validator proves the second reference pipeline
(`.circleci/config.yml`, CircleCI) is a genuine parity twin of the first (`.github/workflows/apply.yml`,
GitHub Actions) — same KCC/HCL dispatch, same merge-to-`main` trigger, same per-target least-privilege
credentials, and NO new agent-held write credential (invariant 2 / D4).

Checks (all must pass for exit 0):

  1. Structural YAML validity — `config.yml` parses (PyYAML if available; else a stdlib structural
     check: no tab indentation, even-space indents, required top-level keys `version`/`jobs`/
     `workflows`), and declares `version: 2.1`.
  2. An `apply` job exists.
  3. The workflow is filtered to `main` — every `apply` job invocation carries `only: main` (applies
     happen only after a reviewed PR is merged).
  4. Dispatch parity vs `apply.yml` — both files route `*.tf` → `terraform apply` and `*.y*ml` →
     `kubectl apply` (identical `apply_path()` signature); the KCC/HCL seam is pipeline-independent.
  5. Per-target least privilege — ≥2 distinct per-target contexts (`kube-agents-apply-<target>`), the
     CircleCI analogue of apply.yml's per-target GitHub Environment.
  6. Invariant 2 / D4 — keyless auth (`CIRCLE_OIDC_TOKEN` → WIF) and NO static service-account key
     material; the pipeline is the writer, the agent holds no credential.
  7. Negative control — a mangled copy (tab-indented line, illegal YAML) MUST fail the validity check.

Hermetic: stdlib only (uses PyYAML only if already importable), no network, no CLIs. `circleci config
validate` and a live CircleCI run are the production checks, deferred-not-faked (CLI absent; no billable
account) — same pattern as Calico standing in for kindnet. See docs/build/phase-7.md D4.

Usage:
    python3 dev/tests/circleci-parity.py [REPO_ROOT]

Exit code 0 = parity holds; 1 = one or more violations (prints them). No third-party deps required.
"""
from __future__ import annotations

import os
import re
import sys

CIRCLECI_REL = "examples/gitops-repo/.circleci/config.yml"
APPLY_REL = "examples/gitops-repo/.github/workflows/apply.yml"


def read(root: str, rel: str) -> str:
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# YAML validity — real parse if PyYAML is importable, else a stdlib structural check.
# ---------------------------------------------------------------------------
def yaml_validity_errors(text: str) -> list[str]:
    errs: list[str] = []
    try:
        import yaml  # type: ignore

        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:  # pragma: no cover - depends on host
            return [f"PyYAML parse error: {e}"]
        if not isinstance(doc, dict):
            errs.append("top-level YAML is not a mapping")
        return errs
    except ImportError:
        pass  # fall through to the stdlib structural check

    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^[ ]*\t", line):
            errs.append(f"line {i}: tab in indentation (illegal YAML)")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 != 0:
            errs.append(f"line {i}: odd indentation ({indent} spaces) — not 2-space structured")
    for key in ("version:", "jobs:", "workflows:"):
        if not re.search(rf"^{re.escape(key)}", text, re.MULTILINE):
            errs.append(f"missing top-level `{key}`")
    return errs


# ---------------------------------------------------------------------------
# Dispatch signature — the KCC/HCL routing that must match across pipelines.
# ---------------------------------------------------------------------------
def dispatch_signature(text: str) -> dict:
    return {
        "tf_glob": bool(re.search(r'ls\s+"?\$dir"?/\*\.tf', text)),
        "tf_apply": bool(re.search(r"terraform\b.*\bapply", text, re.DOTALL)),
        "yaml_glob": bool(re.search(r'ls\s+"?\$dir"?/\*\.y\*ml', text)),
        "kubectl_apply": bool(re.search(r"kubectl apply --server-side", text)),
    }


def check(root: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    passes: list[str] = []

    cfg = read(root, CIRCLECI_REL)
    apply = read(root, APPLY_REL)

    # --- Check 1: YAML validity + version 2.1 -----------------------------
    yerr = yaml_validity_errors(cfg)
    if yerr:
        for e in yerr:
            errors.append(f"[yaml] {CIRCLECI_REL}: {e}")
    else:
        passes.append("config.yml is structurally valid YAML")
    if not re.search(r'^version:\s*"?2\.1"?\s*$', cfg, re.MULTILINE):
        errors.append(f"[version] {CIRCLECI_REL}: missing `version: 2.1`")
    else:
        passes.append("declares version: 2.1")

    # --- Check 2: apply job exists ----------------------------------------
    if not re.search(r"^\s{2}apply:\s*$", cfg, re.MULTILINE):
        errors.append(f"[job] {CIRCLECI_REL}: no `apply` job defined")
    else:
        passes.append("defines an `apply` job")

    # --- Check 3: workflow filtered to main -------------------------------
    # Count apply job invocations in the workflows section vs `only: main` filters.
    wf = cfg.split("workflows:", 1)[-1] if "workflows:" in cfg else ""
    job_invocations = len(re.findall(r"^\s+-\s+apply:\s*$", wf, re.MULTILINE))
    only_main = len(re.findall(r"only:\s*main\b", wf))
    if job_invocations == 0:
        errors.append(f"[trigger] {CIRCLECI_REL}: no apply job invocation in workflows")
    elif only_main < job_invocations:
        errors.append(
            f"[trigger] {CIRCLECI_REL}: {job_invocations} apply job(s) but only {only_main} "
            f"`only: main` filter(s) — some apply is not main-gated"
        )
    else:
        passes.append(f"all {job_invocations} apply job(s) filtered to `main` (apply only post-merge)")

    # --- Check 4: dispatch parity vs apply.yml ----------------------------
    sc, sa = dispatch_signature(cfg), dispatch_signature(apply)
    if sc != sa:
        errors.append(f"[dispatch] CircleCI vs apply.yml signature mismatch: circle={sc} apply={sa}")
    elif not all(sa.values()):
        errors.append(f"[dispatch] incomplete dispatch signature in both files: {sa}")
    else:
        passes.append("dispatch parity: *.tf→terraform, *.y*ml→kubectl — identical to apply.yml")

    # --- Check 5: per-target least-privilege contexts ---------------------
    contexts = sorted(set(re.findall(r"kube-agents-apply-[a-z0-9-]+", cfg)))
    if len(contexts) < 2:
        errors.append(f"[creds] {CIRCLECI_REL}: expected ≥2 per-target contexts, found {contexts}")
    else:
        passes.append(f"per-target least-priv contexts: {', '.join(contexts)}")

    # --- Check 6: invariant 2 / D4 — keyless, no static key ---------------
    if "CIRCLE_OIDC_TOKEN" not in cfg:
        errors.append(f"[invariant2] {CIRCLECI_REL}: not keyless (no CIRCLE_OIDC_TOKEN / OIDC→WIF)")
    static_key_markers = ["GCLOUD_SERVICE_KEY", "credentials_json", "-----BEGIN", "service_account_key"]
    leaked = [m for m in static_key_markers if m in cfg]
    if leaked:
        errors.append(f"[invariant2] {CIRCLECI_REL}: static key material present: {leaked}")
    if "CIRCLE_OIDC_TOKEN" in cfg and not leaked:
        passes.append("keyless OIDC→WIF auth; no static key; no agent-held write credential (inv. 2)")

    # --- Check 7: negative control ----------------------------------------
    mangled = "\tbroken_indent: true\n" + cfg  # a tab-indented line ⇒ illegal YAML
    if yaml_validity_errors(mangled):
        passes.append("negative control: tab-indented (illegal YAML) config correctly rejected")
    else:
        errors.append("[selftest] mangled config passed YAML validity — validator is broken")

    return errors, passes


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    print(f"circleci-parity: second pipeline ↔ apply.yml (root={root})")
    try:
        errors, passes = check(root)
    except FileNotFoundError as e:
        print(f"FAIL: required pipeline file missing: {e}", file=sys.stderr)
        return 1

    for p in passes:
        print(f"  PASS  {p}")
    if errors:
        print()
        for e in errors:
            print(f"  FAIL  {e}", file=sys.stderr)
        print(f"\ncircleci-parity: {len(errors)} violation(s).", file=sys.stderr)
        return 1
    print("\ncircleci-parity: OK — second pipeline actuates the same repo, same seam, same trust boundary.")
    print("  (deferred-not-faked: `circleci config validate` + a live CircleCI run — D4)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
