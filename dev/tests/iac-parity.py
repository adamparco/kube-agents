#!/usr/bin/env python3
"""IaC parity validator (kube-agents Phase 7, P7-T1; 06 §1.1, §4; 07 §"Phase 7" Accept (a)).

The `Agent` CRD exposes `spec.iac.format` (enum `kcc | terraform`, default `kcc`) — the seam that lets
a customer provision with their IaC of choice. This validator proves the seam is REAL, not just a
field: it checks the two committed provisioning exemplars are a matched, equivalent pair and that the
reference actuation pipeline routes each format correctly.

Checks (all must pass for exit 0):

  1. HCL structural validity — the Terraform exemplar has balanced blocks and the required
     `terraform{}` provider pin + `resource "google_container_cluster"` +
     `resource "google_container_node_pool"` blocks with their load-bearing attributes.
  2. KCC↔HCL semantic equivalence — the KCC YAML (cluster-a) and Terraform HCL (cluster-b) describe
     the SAME cluster shape: location, release channel, networking mode, Workload Identity, private
     nodes, remove-default-pool, node machine type / disk / autoscaling min-max, Shielded node config,
     node auto-repair/upgrade.
  3. `iac.format` enum is real — the CRD Go types declare both `kcc` and `terraform`, so each exemplar
     corresponds to a validated field value (not an invented format).
  4. Pipeline dispatch — `apply.yml` `apply_path()` routes `*.tf` → terraform and `*.y*ml` → kubectl,
     and the on-disk dirs match: cluster-b/provisioning is *.tf-only (→ terraform), cluster-a is
     *.yaml-only (→ kubectl). The two formats never collide in one dir.
  5. Negative control — a deliberately-mangled copy of the HCL (unbalanced braces) MUST fail the
     structural check; if it passes, the validator itself is broken and we exit non-zero.

Hermetic: stdlib only (no PyYAML / python-hcl2 — absent on the build host), no network, no CLIs.
`terraform validate`/`fmt`/`apply` and a live second-cloud apply are the production checks and are
deferred-not-faked (no `terraform` binary; no billable cloud) — same pattern as Calico standing in for
kindnet's missing NetworkPolicy enforcement in earlier phases. See docs/build/phase-7.md D1/D2.

Usage:
    python3 dev/tests/iac-parity.py [REPO_ROOT]

Exit code 0 = parity holds; 1 = one or more violations (prints them). No third-party deps.
"""
from __future__ import annotations

import os
import re
import sys

# ---------------------------------------------------------------------------
# Paths (relative to repo root; script lives at dev/tests/)
# ---------------------------------------------------------------------------
KCC_REL = "examples/gitops-repo/clusters/cluster-a/provisioning/cluster-a.yaml"
TF_REL = "examples/gitops-repo/clusters/cluster-b/provisioning/cluster.tf"
TFVARS_REL = "examples/gitops-repo/clusters/cluster-b/provisioning/variables.tf"
APPLY_REL = "examples/gitops-repo/.github/workflows/apply.yml"
CRD_TYPES_REL = "k8s-operator/api/v1alpha1/common_types.go"
KCC_DIR_REL = "examples/gitops-repo/clusters/cluster-a/provisioning"
TF_DIR_REL = "examples/gitops-repo/clusters/cluster-b/provisioning"


def read(root: str, rel: str) -> str:
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# HCL helpers — strip comments + string literals so brace counting and block
# detection are not fooled by `#` comments or braces inside strings.
# ---------------------------------------------------------------------------
def _strip_comments(text: str) -> str:
    # remove `#` and `//` line comments (keep string literals for pattern matching)
    return re.sub(r"(#|//).*", "", text)


def _strip_hcl_noise(text: str) -> str:
    # remove comments AND double-quoted strings (they may contain { } or #) — for brace counting only
    return re.sub(r'"(\\.|[^"\\])*"', '""', _strip_comments(text))


def hcl_structural_errors(text: str) -> list[str]:
    """Return a list of structural problems in the HCL (empty ⇒ structurally valid)."""
    errs: list[str] = []
    # Braces: count on the noise-stripped view so `{`/`}` inside strings/comments don't skew it.
    braces = _strip_hcl_noise(text)
    # Blocks/attrs: match on the comment-stripped view so quoted labels/values survive.
    clean = _strip_comments(text)

    opens, closes = braces.count("{"), braces.count("}")
    if opens != closes:
        errs.append(f"unbalanced braces: {opens} '{{' vs {closes} '}}'")

    # Required top-level blocks.
    if not re.search(r"\bterraform\s*\{", clean):
        errs.append("missing `terraform {` block")
    if not re.search(r"\brequired_providers\s*\{", clean):
        errs.append("missing `required_providers {` (provider pin)")
    if not re.search(r'\bresource\s+"google_container_cluster"\s+"\w+"\s*\{', clean):
        errs.append('missing `resource "google_container_cluster"` block')
    if not re.search(r'\bresource\s+"google_container_node_pool"\s+"\w+"\s*\{', clean):
        errs.append('missing `resource "google_container_node_pool"` block')

    # Required attributes / sub-blocks (load-bearing security + shape).
    required_attrs = {
        "remove_default_node_pool": r"\bremove_default_node_pool\s*=",
        "networking_mode": r"\bnetworking_mode\s*=",
        "release_channel block": r"\brelease_channel\s*\{",
        "workload_identity_config block": r"\bworkload_identity_config\s*\{",
        "private_cluster_config block": r"\bprivate_cluster_config\s*\{",
        "autoscaling block": r"\bautoscaling\s*\{",
        "node_config block": r"\bnode_config\s*\{",
        "machine_type": r"\bmachine_type\s*=",
        "shielded_instance_config block": r"\bshielded_instance_config\s*\{",
    }
    for label, pat in required_attrs.items():
        if not re.search(pat, clean):
            errs.append(f"missing required `{label}`")
    return errs


# ---------------------------------------------------------------------------
# Fact extraction — a normalized dict of cluster facts from each format.
# Both parsers are regex/line based (stdlib only); the exemplars are authored
# to a known shape so targeted patterns are robust and readable.
# ---------------------------------------------------------------------------
def _find(pattern: str, text: str, flags: int = 0):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


def kcc_facts(text: str) -> dict:
    return {
        "location": _find(r"location:\s*([A-Za-z0-9-]+)", text),
        "channel": _find(r"channel:\s*([A-Z]+)", text),
        "networking_mode": _find(r"networkingMode:\s*([A-Z_]+)", text),
        "machine_type": _find(r"machineType:\s*([A-Za-z0-9-]+)", text),
        "disk_size_gb": _find(r"diskSizeGb:\s*(\d+)", text),
        "min_nodes": _find(r"minNodeCount:\s*(\d+)", text),
        "max_nodes": _find(r"maxNodeCount:\s*(\d+)", text),
        "remove_default_pool": _find(r"removeDefaultNodePool:\s*(true|false)", text),
        "private_nodes": _find(r"enablePrivateNodes:\s*(true|false)", text),
        "secure_boot": _find(r"enableSecureBoot:\s*(true|false)", text),
        "integrity_monitoring": _find(r"enableIntegrityMonitoring:\s*(true|false)", text),
        "auto_repair": _find(r"autoRepair:\s*(true|false)", text),
        "auto_upgrade": _find(r"autoUpgrade:\s*(true|false)", text),
        "workload_identity": "true" if re.search(r"workloadIdentityConfig:", text) else "false",
    }


def _tfvar_defaults(tfvars_text: str) -> dict:
    """Map `variable "x" { ... default = "y" }` → {"x": "y"} (only vars with a literal default)."""
    out: dict = {}
    for m in re.finditer(r'variable\s+"(\w+)"\s*\{(.*?)\}', tfvars_text, re.DOTALL):
        name, body = m.group(1), m.group(2)
        dm = re.search(r'default\s*=\s*"([^"]*)"', body)
        if dm:
            out[name] = dm.group(1)
    return out


def _resolve(value: str | None, tfvars: dict) -> str | None:
    """Resolve a bare `var.NAME` reference to its default (else return value unchanged)."""
    if value is None:
        return None
    m = re.fullmatch(r"var\.(\w+)", value.strip())
    if m:
        return tfvars.get(m.group(1))
    return value


def hcl_facts(text: str, tfvars: dict) -> dict:
    clean = _strip_comments(text)
    loc_raw = _find(r"location\s*=\s*(var\.\w+|\"[A-Za-z0-9-]+\")", clean)
    if loc_raw:
        loc_raw = loc_raw.strip('"')
    return {
        "location": _resolve(loc_raw, tfvars),
        "channel": _find(r'channel\s*=\s*"([A-Z]+)"', clean),
        "networking_mode": _find(r'networking_mode\s*=\s*"([A-Z_]+)"', clean),
        "machine_type": _find(r'machine_type\s*=\s*"([A-Za-z0-9-]+)"', clean),
        "disk_size_gb": _find(r"disk_size_gb\s*=\s*(\d+)", clean),
        "min_nodes": _find(r"min_node_count\s*=\s*(\d+)", clean),
        "max_nodes": _find(r"max_node_count\s*=\s*(\d+)", clean),
        "remove_default_pool": _find(r"remove_default_node_pool\s*=\s*(true|false)", clean),
        "private_nodes": _find(r"enable_private_nodes\s*=\s*(true|false)", clean),
        "secure_boot": _find(r"enable_secure_boot\s*=\s*(true|false)", clean),
        "integrity_monitoring": _find(r"enable_integrity_monitoring\s*=\s*(true|false)", clean),
        "auto_repair": _find(r"auto_repair\s*=\s*(true|false)", clean),
        "auto_upgrade": _find(r"auto_upgrade\s*=\s*(true|false)", clean),
        "workload_identity": "true" if re.search(r"workload_identity_config\s*\{", clean) else "false",
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check(root: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    passes: list[str] = []

    kcc_text = read(root, KCC_REL)
    tf_text = read(root, TF_REL)
    tfvars = _tfvar_defaults(read(root, TFVARS_REL))

    # --- Check 1: HCL structural validity ---------------------------------
    struct = hcl_structural_errors(tf_text)
    if struct:
        for e in struct:
            errors.append(f"[structure] {TF_REL}: {e}")
    else:
        passes.append("HCL structurally valid (balanced blocks; terraform+cluster+node_pool + attrs)")

    # --- Check 2: KCC↔HCL semantic equivalence ----------------------------
    kf, hf = kcc_facts(kcc_text), hcl_facts(tf_text, tfvars)
    for key in sorted(kf):
        kv, hv = kf[key], hf.get(key)
        if kv is None:
            errors.append(f"[parity] KCC missing fact `{key}`")
        elif hv is None:
            errors.append(f"[parity] HCL missing fact `{key}`")
        elif kv != hv:
            errors.append(f"[parity] `{key}` differs: KCC={kv!r} vs HCL={hv!r}")
    if not any(e.startswith("[parity]") for e in errors):
        passes.append(f"KCC↔HCL semantically equivalent across {len(kf)} facts ({', '.join(sorted(kf))})")

    # --- Check 3: iac.format enum is real ---------------------------------
    crd = read(root, CRD_TYPES_REL)
    if "Enum=kcc;terraform" not in crd:
        errors.append(f"[enum] {CRD_TYPES_REL}: missing `+kubebuilder:validation:Enum=kcc;terraform`")
    elif 'IACFormat = "kcc"' not in crd or 'IACFormat = "terraform"' not in crd:
        errors.append(f"[enum] {CRD_TYPES_REL}: enum constants for kcc/terraform not both present")
    else:
        passes.append("iac.format enum declares both `kcc` and `terraform` (each exemplar is a real value)")

    # --- Check 4: pipeline dispatch ---------------------------------------
    apply = read(root, APPLY_REL)
    tf_branch = re.search(r'ls\s+"?\$dir"?/\*\.tf.*?terraform', apply, re.DOTALL)
    yaml_branch = re.search(r'ls\s+"?\$dir"?/\*\.y\*ml.*?kubectl apply', apply, re.DOTALL)
    if not tf_branch:
        errors.append(f"[dispatch] {APPLY_REL}: no `*.tf → terraform` branch in apply_path()")
    if not yaml_branch:
        errors.append(f"[dispatch] {APPLY_REL}: no `*.y*ml → kubectl apply` branch in apply_path()")

    kcc_files = os.listdir(os.path.join(root, KCC_DIR_REL))
    tf_files = os.listdir(os.path.join(root, TF_DIR_REL))
    kcc_has_yaml = any(f.endswith((".yaml", ".yml")) for f in kcc_files)
    kcc_has_tf = any(f.endswith(".tf") for f in kcc_files)
    tf_has_tf = any(f.endswith(".tf") for f in tf_files)
    tf_has_yaml = any(f.endswith((".yaml", ".yml")) for f in tf_files)
    if not (kcc_has_yaml and not kcc_has_tf):
        errors.append(f"[dispatch] {KCC_DIR_REL} must be *.yaml-only (→kubectl); found tf={kcc_has_tf}")
    if not (tf_has_tf and not tf_has_yaml):
        errors.append(f"[dispatch] {TF_DIR_REL} must be *.tf-only (→terraform); found yaml={tf_has_yaml}")
    if not any(e.startswith("[dispatch]") for e in errors):
        passes.append("apply.yml routes cluster-a/*.yaml→kubectl, cluster-b/*.tf→terraform (no collision)")

    # --- Check 5: negative control ----------------------------------------
    # A mangled copy (drop the final closing brace) MUST fail structural validity.
    mangled = tf_text.rstrip()
    if mangled.endswith("}"):
        mangled = mangled[:-1]  # unbalance the braces
    if hcl_structural_errors(mangled):
        passes.append("negative control: mangled HCL correctly rejected by structural check")
    else:
        errors.append("[selftest] mangled HCL passed structural check — validator is broken")

    return errors, passes


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    print(f"iac-parity: KCC↔Terraform `spec.iac.format` seam (root={root})")
    try:
        errors, passes = check(root)
    except FileNotFoundError as e:
        print(f"FAIL: required exemplar/artifact missing: {e}", file=sys.stderr)
        return 1

    for p in passes:
        print(f"  PASS  {p}")
    if errors:
        print()
        for e in errors:
            print(f"  FAIL  {e}", file=sys.stderr)
        print(f"\niac-parity: {len(errors)} violation(s).", file=sys.stderr)
        return 1
    print("\niac-parity: OK — iac.format seam is real, equivalent, and correctly dispatched.")
    print("  (deferred-not-faked: terraform validate/fmt/apply + live second-cloud apply — D1/D2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
