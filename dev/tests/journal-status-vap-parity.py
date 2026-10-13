#!/usr/bin/env python3
"""L0 parity check between ActionRecordStatus JSON fields and vap-agent-scope-journal.yaml.

Enforces the invariant cited in k8s-operator/config/policy/vap-agent-scope-journal.yaml
(lines 94 and 178):
  1. Every JSON field <f> of ActionRecordStatus in actionrecord_types.go must have a
     corresponding <f>Changed variable in kube-agents-agent-scope-journal's spec.variables.
  2. Every <f>Changed variable in spec.variables (excluding nothingChanged) must correspond
     to a real JSON field on ActionRecordStatus (no phantom variables).
  3. Every <f>Changed variable must appear as !variables.<f>Changed in the nothingChanged
     conjunction so no status field can fail open at admission.
  4. A built-in negative control injects a synthetic 'unprotectedField' into a copy of the
     struct fields set and verifies that parity detection fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
TYPES_PATH = REPO_ROOT / "k8s-operator" / "api" / "broker" / "v1alpha1" / "actionrecord_types.go"
VAP_PATH = REPO_ROOT / "k8s-operator" / "config" / "policy" / "vap-agent-scope-journal.yaml"
POLICY_NAME = "kube-agents-agent-scope-journal"


def extract_status_json_fields(go_source: str) -> set[str]:
    """Extract JSON field names from `type ActionRecordStatus struct { ... }`."""
    match = re.search(
        r"type\s+ActionRecordStatus\s+struct\s*\{(?P<body>.*?)\n\}",
        go_source,
        re.DOTALL,
    )
    if not match:
        raise ValueError("could not locate `type ActionRecordStatus struct` in actionrecord_types.go")

    body = match.group("body")
    fields: set[str] = set()
    for tag_match in re.finditer(r'`json:"([^"]+)"`', body):
        tag_value = tag_match.group(1)
        name = tag_value.split(",", 1)[0].strip()
        if name and name != "-":
            fields.add(name)

    if not fields:
        raise ValueError("extracted zero JSON fields from ActionRecordStatus")
    return fields


def extract_vap_sets(vap_yaml: str) -> tuple[set[str], set[str]]:
    """Return (<field>Changed variable roots, nothingChanged variable roots) from the VAP."""
    target_doc = None
    for doc in yaml.safe_load_all(vap_yaml):
        if not isinstance(doc, dict):
            continue
        if (
            doc.get("kind") == "ValidatingAdmissionPolicy"
            and doc.get("metadata", {}).get("name") == POLICY_NAME
        ):
            target_doc = doc
            break

    if target_doc is None:
        raise ValueError(f"could not find ValidatingAdmissionPolicy {POLICY_NAME!r} in {VAP_PATH}")

    variables = target_doc.get("spec", {}).get("variables", [])
    changed_vars: set[str] = set()
    nothing_changed_expr: str | None = None

    for var in variables:
        name = var.get("name", "")
        expr = var.get("expression", "")
        if name == "nothingChanged":
            nothing_changed_expr = expr
        elif name.endswith("Changed"):
            changed_vars.add(name[: -len("Changed")])

    if nothing_changed_expr is None:
        raise ValueError(f"missing 'nothingChanged' variable in {POLICY_NAME}")

    nothing_changed_fields: set[str] = set()
    clauses = [c.strip() for c in nothing_changed_expr.split("&&") if c.strip()]
    for clause in clauses:
        m = re.fullmatch(r"!variables\.([A-Za-z0-9_]+)Changed", clause)
        if not m:
            raise ValueError(
                f"unexpected clause {clause!r} in nothingChanged expression "
                "(expected '!variables.<field>Changed')"
            )
        nothing_changed_fields.add(m.group(1))

    return changed_vars, nothing_changed_fields


def check_parity(
    struct_fields: set[str],
    changed_vars: set[str],
    nothing_changed_fields: set[str],
) -> list[str]:
    """Compare the three sets and return a list of human-readable parity errors."""
    errors: list[str] = []

    missing_vars = sorted(struct_fields - changed_vars)
    if missing_vars:
        errors.append(
            f"ActionRecordStatus fields missing <field>Changed variable in VAP: {missing_vars}"
        )

    phantom_vars = sorted(changed_vars - struct_fields)
    if phantom_vars:
        errors.append(
            f"VAP <field>Changed variables with no matching ActionRecordStatus JSON field: {phantom_vars}"
        )

    missing_in_nothing_changed = sorted(struct_fields - nothing_changed_fields)
    if missing_in_nothing_changed:
        errors.append(
            f"ActionRecordStatus fields missing !variables.<field>Changed in nothingChanged: {missing_in_nothing_changed}"
        )

    extra_in_nothing_changed = sorted(nothing_changed_fields - struct_fields)
    if extra_in_nothing_changed:
        errors.append(
            f"nothingChanged references unknown ActionRecordStatus fields: {extra_in_nothing_changed}"
        )

    var_vs_nothing_diff = sorted(changed_vars ^ nothing_changed_fields)
    if var_vs_nothing_diff:
        errors.append(
            f"<field>Changed variables and nothingChanged clauses differ on: {var_vs_nothing_diff}"
        )

    return errors


def main() -> int:
    go_source = TYPES_PATH.read_text(encoding="utf-8")
    vap_yaml = VAP_PATH.read_text(encoding="utf-8")

    struct_fields = extract_status_json_fields(go_source)
    changed_vars, nothing_changed_fields = extract_vap_sets(vap_yaml)

    # 1. Positive parity check.
    errors = check_parity(struct_fields, changed_vars, nothing_changed_fields)
    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    # 2. Negative control: inject a synthetic unprotected field and confirm detection fails.
    mutated_fields = set(struct_fields) | {"unprotectedField"}
    neg_errors = check_parity(mutated_fields, changed_vars, nothing_changed_fields)
    if not neg_errors:
        print(
            "FAIL: negative control did not detect synthetic 'unprotectedField' in ActionRecordStatus",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: ActionRecordStatus ({len(struct_fields)} fields) is in 1:1 parity with "
        f"{POLICY_NAME} (<field>Changed variables and nothingChanged conjunction); "
        "negative control passed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
