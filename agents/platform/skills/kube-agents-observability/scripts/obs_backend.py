"""Provider-neutral observability backend selector (kube-agents Phase 7, P7-T3; 01 §6; D3).

The observability skill scripts talk to a metrics/trace/logging backend. To keep kube-agents from
being hard-wired to GCP, the backend base URLs are resolved here from the environment instead of being
hardcoded in each script:

  - KUBEAGENTS_OBS_BACKEND selects a profile. `gcp` (the DEFAULT) resolves to the Google Cloud
    Monitoring / Cloud Trace / Cloud Logging endpoints — so an unset environment behaves EXACTLY as
    before (no regression on GKE). Any other value is a non-GCP backend and MUST supply explicit base
    URLs via the per-signal overrides below (Prometheus/Tempo/Loki/OTLP topologies vary — there is no
    single correct default, so we require it rather than invent one).

  - OBS_MONITORING_BASE_URL / OBS_TRACE_BASE_URL / OBS_LOGGING_BASE_URL are explicit per-signal
    overrides. When set they win over the profile (they work even with the `gcp` profile), so a single
    signal can be pointed at, e.g., an in-cluster Prometheus without switching the whole profile.

This is a SEAM, not a rip-out: GCP remains the zero-config default. Query translation for a non-GCP
backend (e.g. Cloud Monitoring MQL → PromQL, Cloud Trace → Tempo) is backend-specific and deferred
(D3) — this module only makes the ENDPOINT provider-neutral.

Functions read the environment on every call so a caller (or a test) can vary it between calls.
"""
from __future__ import annotations

import os

# The Google Cloud defaults — the historical hardcoded hosts. Unset env ⇒ these ⇒ no regression.
_GCP_DEFAULTS = {
    "monitoring": "https://monitoring.googleapis.com",
    "trace": "https://cloudtrace.googleapis.com",
    "logging": "https://logging.googleapis.com",
}

_OVERRIDE_ENV = {
    "monitoring": "OBS_MONITORING_BASE_URL",
    "trace": "OBS_TRACE_BASE_URL",
    "logging": "OBS_LOGGING_BASE_URL",
}


def backend() -> str:
    """The selected backend profile (lower-cased); defaults to `gcp`."""
    return (os.getenv("KUBEAGENTS_OBS_BACKEND") or "gcp").strip().lower()


def _resolve(signal: str) -> str:
    # 1) An explicit per-signal override always wins (works with any profile).
    override = os.getenv(_OVERRIDE_ENV[signal])
    if override:
        return override.rstrip("/")
    # 2) Otherwise the profile decides. `gcp` (default) → the Google Cloud endpoints.
    if backend() == "gcp":
        return _GCP_DEFAULTS[signal]
    # 3) A non-GCP profile with no explicit URL is a configuration error — fail loudly, don't
    #    silently fall back to Google (which would leak queries to the wrong backend).
    raise SystemExit(
        f"KUBEAGENTS_OBS_BACKEND={backend()!r}: set {_OVERRIDE_ENV[signal]} to the {signal} "
        f"endpoint for a non-GCP backend (e.g. an in-cluster Prometheus/Tempo/Loki base URL)."
    )


def monitoring_base_url() -> str:
    """Base URL for the metrics/monitoring API (Cloud Monitoring by default)."""
    return _resolve("monitoring")


def trace_base_url() -> str:
    """Base URL for the tracing API (Cloud Trace by default)."""
    return _resolve("trace")


def logging_base_url() -> str:
    """Base URL for the logging API (Cloud Logging by default)."""
    return _resolve("logging")
