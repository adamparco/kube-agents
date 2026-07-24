#!/usr/bin/env python3
"""Authentication for the local session-inject seam (Phase 4 / P4-T1, S1).

The session daemon (``session_kv_server``) binds ``127.0.0.1:8699`` so only same-pod
callers can reach it. This module is the defense-in-depth bearer/owner check layered on
top of that loopback bind: even a compromised co-container cannot wake an agent turn
without presenting the per-pod API key that the operator injects into the sidecar.

It is deliberately dependency-free (stdlib only) so the security decision is unit-testable
without FastAPI, and so the eventingress relay (D1) can reuse the exact same predicate for
its cloud-push legs.
"""

from __future__ import annotations

import os
import secrets
from typing import List, Optional, Tuple

# Env var holding the per-pod inject key. The operator sets this on both the sidecar
# (which presents it) and the main container (which validates it) from the same Secret,
# so the two halves always agree. Empty => auth disabled (dev/test only).
ENV_API_KEY = "API_SERVER_KEY"

# Optional comma-separated allow-list of accepted ``X-Asserted-Caller`` values — the
# server-side half of the watcher's ``--owner=<tier>`` claim. Empty => owner not checked.
ENV_ALLOWED_OWNERS = "SESSION_KV_ALLOWED_OWNERS"

_BEARER_PREFIX = "Bearer "


def expected_api_key() -> str:
    """Return the configured per-pod inject key ('' means auth is disabled)."""
    return os.environ.get(ENV_API_KEY, "")


def allowed_owners() -> List[str]:
    """Return the configured owner allow-list (empty list means any owner is accepted)."""
    raw = os.environ.get(ENV_ALLOWED_OWNERS, "")
    return [owner.strip() for owner in raw.split(",") if owner.strip()]


def check_inject_auth(
    authorization: Optional[str],
    x_asserted_caller: Optional[str],
    expected_key: str,
    allowed: Optional[List[str]] = None,
) -> Optional[Tuple[int, str]]:
    """Pure authorization decision for the session-inject seam.

    Returns ``None`` when the caller is authorized, otherwise ``(status_code, detail)``
    describing the rejection. When ``expected_key`` is empty the check is skipped
    (returns ``None``); the caller is responsible for logging that auth is disabled.

    The bearer comparison is constant-time to avoid leaking the key via timing.
    """
    if not expected_key:
        return None

    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        return (401, "missing or malformed Authorization bearer token")

    presented = authorization[len(_BEARER_PREFIX):]
    if not secrets.compare_digest(presented, expected_key):
        return (401, "invalid bearer token")

    if allowed and (x_asserted_caller or "") not in allowed:
        return (403, "asserted caller not permitted for this seam")

    return None
