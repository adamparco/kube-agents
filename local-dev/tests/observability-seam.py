#!/usr/bin/env python3
"""Observability backend-seam validator (kube-agents Phase 7, P7-T3; 01 §6; D3).

Proves the observability skill's backend endpoints are provider-neutral — resolved from the
environment via obs_backend, with Google Cloud as the zero-config default (no regression) and a
documented non-GCP path. All hermetic: no network, stdlib only.

Checks (all must pass for exit 0):

  1. Default (env unset) → the exact Google Cloud endpoints (monitoring/trace/logging googleapis) —
     unchanged behavior on GKE.
  2. Explicit `KUBEAGENTS_OBS_BACKEND=gcp` → the same Google Cloud endpoints.
  3. A per-signal override (`OBS_MONITORING_BASE_URL=<prometheus url>`) wins and resolves to a
     NON-googleapis endpoint (works even under the gcp profile).
  4. `KUBEAGENTS_OBS_BACKEND=prometheus` + explicit base URLs → all three signals resolve to
     non-googleapis endpoints.
  5. A non-GCP profile with a required base URL UNSET fails loudly (SystemExit) rather than silently
     falling back to Google.
  6. Each observability script resolves its base URL via the seam (`*_base_url()`), with no hardcoded
     `https://*.googleapis.com` API host left in the URL construction.

Usage:
    python3 local-dev/tests/observability-seam.py [REPO_ROOT]

Exit code 0 = seam holds; 1 = one or more violations (prints them). No third-party deps.
"""
from __future__ import annotations

import os
import sys

SCRIPTS_REL = "agents/platform/skills/kube-agents-observability/scripts"
GCP_HOSTS = {
    "monitoring": "https://monitoring.googleapis.com",
    "trace": "https://cloudtrace.googleapis.com",
    "logging": "https://logging.googleapis.com",
}
OBS_ENV_VARS = [
    "KUBEAGENTS_OBS_BACKEND",
    "OBS_MONITORING_BASE_URL",
    "OBS_TRACE_BASE_URL",
    "OBS_LOGGING_BASE_URL",
]
# The URL construction in each script → which base_url helper it must use.
SCRIPT_SIGNALS = {
    "fetch_traces.py": "trace_base_url",
    "analyze_trace_latency.py": "trace_base_url",
    "get_chat_users.py": "logging_base_url",
    "check_token_usage.py": "monitoring_base_url",
    "get_metric_descriptors.py": "monitoring_base_url",
}


def _clear_env() -> None:
    for k in OBS_ENV_VARS:
        os.environ.pop(k, None)


def check(root: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    passes: list[str] = []

    scripts_dir = os.path.join(root, SCRIPTS_REL)
    sys.path.insert(0, scripts_dir)
    try:
        import obs_backend  # noqa: E402
    except ImportError as e:
        return [f"[import] cannot import obs_backend from {SCRIPTS_REL}: {e}"], []

    def urls() -> dict:
        return {
            "monitoring": obs_backend.monitoring_base_url(),
            "trace": obs_backend.trace_base_url(),
            "logging": obs_backend.logging_base_url(),
        }

    # --- Check 1: default (unset) → Google Cloud endpoints ----------------
    _clear_env()
    u = urls()
    if u == GCP_HOSTS:
        passes.append("default (env unset) resolves to the exact Google Cloud endpoints (no regression)")
    else:
        errors.append(f"[default] unset env resolved to {u}, expected {GCP_HOSTS}")

    # --- Check 2: explicit gcp profile → Google Cloud endpoints -----------
    _clear_env()
    os.environ["KUBEAGENTS_OBS_BACKEND"] = "gcp"
    if urls() == GCP_HOSTS:
        passes.append("explicit KUBEAGENTS_OBS_BACKEND=gcp resolves to Google Cloud endpoints")
    else:
        errors.append("[gcp] explicit gcp profile did not resolve to Google Cloud endpoints")

    # --- Check 3: per-signal override wins & is non-googleapis -------------
    _clear_env()
    prom = "http://prometheus-server.monitoring.svc.cluster.local"
    os.environ["OBS_MONITORING_BASE_URL"] = prom
    mon = obs_backend.monitoring_base_url()
    if mon == prom and "googleapis.com" not in mon:
        passes.append("per-signal override wins and resolves to a non-googleapis endpoint")
    else:
        errors.append(f"[override] OBS_MONITORING_BASE_URL override resolved to {mon!r}")

    # --- Check 4: non-gcp profile + explicit URLs → all non-googleapis ----
    _clear_env()
    os.environ["KUBEAGENTS_OBS_BACKEND"] = "prometheus"
    os.environ["OBS_MONITORING_BASE_URL"] = "http://prometheus-server.monitoring.svc:9090"
    os.environ["OBS_TRACE_BASE_URL"] = "http://tempo.monitoring.svc:3200"
    os.environ["OBS_LOGGING_BASE_URL"] = "http://loki.monitoring.svc:3100"
    u = urls()
    if all("googleapis.com" not in v for v in u.values()):
        passes.append("non-GCP profile (prometheus) + explicit URLs → all signals non-googleapis")
    else:
        errors.append(f"[nongcp] non-GCP profile still resolved a googleapis endpoint: {u}")

    # --- Check 5: non-gcp profile, required URL unset → loud failure ------
    _clear_env()
    os.environ["KUBEAGENTS_OBS_BACKEND"] = "prometheus"  # no OBS_*_BASE_URL
    try:
        obs_backend.monitoring_base_url()
        errors.append("[failloud] non-GCP profile with no base URL did NOT raise (silent fallback risk)")
    except SystemExit:
        passes.append("non-GCP profile with a required base URL unset fails loudly (no silent GCP fallback)")

    # --- Check 6: scripts use the seam, not hardcoded googleapis hosts ----
    _clear_env()
    for fname, helper in SCRIPT_SIGNALS.items():
        path = os.path.join(scripts_dir, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            errors.append(f"[script] missing observability script {fname}")
            continue
        if f"{helper}()" not in text:
            errors.append(f"[script] {fname} does not resolve its URL via {helper}()")
        for host in GCP_HOSTS.values():
            if host in text:
                errors.append(f"[script] {fname} still hardcodes API host {host}")
    if not any(e.startswith("[script]") for e in errors):
        passes.append(f"all {len(SCRIPT_SIGNALS)} obs scripts resolve base URLs via the seam (no hardcoded host)")

    _clear_env()
    return errors, passes


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    print(f"observability-seam: provider-neutral obs backend (root={root})")
    errors, passes = check(root)
    for p in passes:
        print(f"  PASS  {p}")
    if errors:
        print()
        for e in errors:
            print(f"  FAIL  {e}", file=sys.stderr)
        print(f"\nobservability-seam: {len(errors)} violation(s).", file=sys.stderr)
        return 1
    print("\nobservability-seam: OK — GCP is the default; endpoints resolve provider-neutrally.")
    print("  (deferred-not-faked: query translation + a live non-GCP backend end-to-end — D3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
