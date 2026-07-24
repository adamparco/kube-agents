#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from contextlib import closing

import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from agent_common_server import _run_env, CONFIG_PATH, DOTENV_PATH
import inject_auth

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("session_kv_server")

try:
    import dotenv
    dotenv.load_dotenv(DOTENV_PATH)
except Exception:
    pass

app = FastAPI()

SESSION_KV_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")
CLEANUP_TTL_DAYS = int(os.getenv("SESSION_KV_CLEANUP_TTL_DAYS", "14"))


def init_db() -> None:
    db_dir = os.path.dirname(SESSION_KV_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_metadata (
                    session_id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    chat_id   TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    report    TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )






def cleanup_old_records(conn: sqlite3.Connection) -> None:
    try:
        # Delete incident reports and session metadata older than CLEANUP_TTL_DAYS
        param = f"-{CLEANUP_TTL_DAYS} days"
        conn.execute("DELETE FROM incidents WHERE created_at < datetime('now', ?)", (param,))
        conn.execute("DELETE FROM session_metadata WHERE updated_at < datetime('now', ?)", (param,))
    except Exception as exc:
        logger.error(f"Failed to clean up old DB records: {exc}")


def _require_inject_auth(authorization: Optional[str], x_asserted_caller: Optional[str]) -> None:
    """Guard the machine-push seam (POST /sessions and /sessions/{id}/inject).

    Enforces the per-pod bearer key (and optional owner allow-list) when API_SERVER_KEY is
    set — which the operator always does in production. When it is unset (local dev / unit
    tests) the seam is left open with a warning; the 127.0.0.1 bind is the unconditional
    network-level backstop in that case. See inject_auth.check_inject_auth for the decision.
    """
    expected = inject_auth.expected_api_key()
    if not expected:
        logger.warning(
            "API_SERVER_KEY unset — session-inject seam auth is DISABLED (dev/test only)"
        )
        return
    err = inject_auth.check_inject_auth(
        authorization, x_asserted_caller, expected, inject_auth.allowed_owners()
    )
    if err is not None:
        status_code, detail = err
        raise HTTPException(status_code=status_code, detail=detail)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions", status_code=201)
def create_session(
    authorization: Optional[str] = Header(default=None),
    x_asserted_caller: Optional[str] = Header(default=None),
) -> Dict[str, str]:
    """Create a new session ID for the incoming incident."""
    _require_inject_auth(authorization, x_asserted_caller)
    session_id = f"k8s-evt-{uuid.uuid4().hex[:8]}"
    
    # Save the session to the local metadata DB
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps({"platform": "k8s-watcher", "created_at": datetime.now(timezone.utc).isoformat()}))
            )
            cleanup_old_records(conn)
    return {"sessionID": session_id}


def clean_workload_name(kind: str, name: str) -> str:
    if kind.lower() == "pod":
        # Match pattern of deployment replica (e.g. -6cfdb6b98b-zwv24)
        m = re.match(r"^(.*?)-[a-f0-9]{8,10}-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
        # Match pattern of statefulset/job/pod replica (e.g. -0 or -abcde)
        m = re.match(r"^(.*?)-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
    return name


def clean_reason_label(reason: str) -> str:
    # E.g. FailedToDrainNode -> Failed to drain node
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', reason).lower()
    return s.capitalize()


def clean_event_message(message: str) -> str:
    msg = message.replace("PodDisruptionBudget", "PDB")
    # Simplify PDB eviction violation message:
    m = re.search(r"cannot be evicted:\s*(would violate PDB\s+(?:[^/]+/)?([a-zA-Z0-9_-]+))", msg)
    if m:
        clean_pdb = m.group(2)
        return f"Eviction would violate PDB {clean_pdb}"
    return msg


def get_severity_details(event_type: str, reason: str) -> tuple[str, str]:
    event_lower = event_type.lower()
    reason_lower = reason.lower()
    
    # Blocker if it blocks drain, eviction, or scheduling
    is_blocker = (
        event_lower == "warning" and 
        any(x in reason_lower for x in ("drain", "evict", "schedul", "capacity", "oomkilled", "crashloopbackoff", "failedmount"))
    )
    
    if is_blocker:
        return "🔴", "Critical"
    elif event_lower == "warning":
        return "🟡", "Warning"
    else:
        return "🔵", "Info"


# --- Inject-kind discriminator (Phase 4 / P4-T2, S2; 04 §4) -----------------------------
# The machine-push seam multiplexes several signal sources. Each carries a top-level
# "kind" so the daemon renders and frames the agent turn appropriately instead of coercing
# every payload through the Kubernetes-event path. A missing/unknown kind is rejected (400).
INJECT_KIND_K8S_EVENT = "k8s-event"
INJECT_KIND_K8S_EVENT_FOLLOWUP = "k8s-event-followup"
INJECT_KIND_ALERT = "alert"
INJECT_KIND_GITHUB = "github"
INJECT_KIND_ESCALATION = "escalation"

_KNOWN_INJECT_KINDS = {
    INJECT_KIND_K8S_EVENT,
    INJECT_KIND_K8S_EVENT_FOLLOWUP,
    INJECT_KIND_ALERT,
    INJECT_KIND_GITHUB,
    INJECT_KIND_ESCALATION,
}


def _format_k8s_event_card(payload: Dict[str, Any]) -> str:
    """Render the notification card for a Kubernetes event (unchanged legacy format)."""
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    event_type = payload.get("type") or "Warning"

    severity_emoji, severity_label = get_severity_details(event_type, event_reason)
    clean_name = clean_workload_name(object_kind, object_name)
    clean_reason = clean_reason_label(event_reason)
    clean_msg = clean_event_message(message)
    return (
        f"{severity_emoji} *{severity_label}:* {clean_reason} `{namespace}/{clean_name}` — {clean_msg}\n"
        f"🌱 _Digging down to the root cause..._"
    )


def _format_alert_card(payload: Dict[str, Any]) -> str:
    """Render the card for a monitoring/alerting webhook (Cloud Monitoring, PagerDuty, …)."""
    title = payload.get("summary") or payload.get("reason") or payload.get("message") or "Alert"
    policy = payload.get("policy") or payload.get("name") or ""
    severity = str(payload.get("severity") or payload.get("type") or "warning").lower()
    emoji = "🔴" if severity in ("critical", "error", "page", "high") else "🟡"
    scope = f" `{payload.get('namespace')}`" if payload.get("namespace") else ""
    policy_suffix = f" ({policy})" if policy else ""
    return (
        f"{emoji} *Alert:* {title}{policy_suffix}{scope}\n"
        f"🌱 _Investigating the alerting condition..._"
    )


def _format_github_card(payload: Dict[str, Any]) -> str:
    """Render the card for a GitHub webhook (PR/issue/push activity on a watched repo)."""
    action = payload.get("action") or payload.get("reason") or "event"
    repo = payload.get("repo") or payload.get("repository") or ""
    title = payload.get("title") or payload.get("message") or ""
    number = payload.get("number")
    ref = f" #{number}" if number is not None else ""
    where = f" `{repo}`" if repo else ""
    detail = f" — {title}" if title else ""
    return (
        f"🐙 *GitHub:* {action}{ref}{where}{detail}\n"
        f"🌱 _Reviewing the change..._"
    )


def _format_escalation_card(payload: Dict[str, Any]) -> str:
    """Render the card for an escalation surfaced from a lower tier (via shared knowledge state).

    Note: this only *wakes* the parent to assess the escalation file — the parent re-derives
    its own scope and acts via a GitOps PR. It is never a direct lower->parent call (invariant 4).
    """
    origin = payload.get("from") or payload.get("from_tier") or "a lower tier"
    summary = payload.get("summary") or payload.get("message") or payload.get("reason") or "escalation raised"
    scope = payload.get("namespace") or payload.get("scope") or ""
    scope_suffix = f" `{scope}`" if scope else ""
    return (
        f"⏫ *Escalation from {origin}:*{scope_suffix} {summary}\n"
        f"🌱 _Assessing whether this falls within my scope..._"
    )


def _format_inject_card(kind: str, payload: Dict[str, Any]) -> str:
    """Dispatch to the per-kind notification card renderer."""
    if kind in (INJECT_KIND_K8S_EVENT, INJECT_KIND_K8S_EVENT_FOLLOWUP):
        return _format_k8s_event_card(payload)
    if kind == INJECT_KIND_ALERT:
        return _format_alert_card(payload)
    if kind == INJECT_KIND_GITHUB:
        return _format_github_card(payload)
    if kind == INJECT_KIND_ESCALATION:
        return _format_escalation_card(payload)
    # Unreachable: inject_message validates kind before dispatch.
    raise HTTPException(status_code=400, detail=f"unknown inject kind: {kind!r}")


def get_active_platform() -> str:
    try:
        import yaml
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        platforms = cfg.get("platforms", {})
        if platforms.get("slack", {}).get("enabled"):
            return "slack"
        if platforms.get("google_chat", {}).get("enabled"):
            return "google_chat"
    except Exception as exc:
        logger.error(f"Failed to parse config.yaml for active platform: {exc}")
    if os.environ.get("SLACK_BOT_TOKEN"):
        return "slack"
    return "google_chat"


def _post_initial_alert(active_platform: str, alert_msg: str) -> str | None:
    """Send initial warning alert via hermes CLI and return the thread/message ID."""
    try:
        res = subprocess.run(
            ["hermes", "send", "--json", "--to", active_platform, alert_msg],
            check=True,
            capture_output=True,
            text=True,
            env=_run_env()
        )
        resp = json.loads(res.stdout)
        msg_id = resp.get("message_id", "")
        if msg_id:
            # Google Chat message IDs contain space and message parts; we extract the thread key.
            if active_platform == "google_chat" and "/messages/" in msg_id:
                space_part, msg_part = msg_id.split("/messages/", 1)
                thread_key = msg_part.split(".")[0]
                return f"{space_part}/threads/{thread_key}"
            return msg_id
    except subprocess.CalledProcessError as exc:
        logger.error(f"Failed to post warning alert. Stdout: {exc.stdout}. Stderr: {exc.stderr}. Exc: {exc}")
    except Exception as exc:
        logger.error(f"Failed to post warning alert or parse message_id response: {exc}")
    return None


def _register_session_routing(session_id: str, platform: str, thread_id: str) -> None:
    """Save thread configurations in session_metadata SQLite table."""
    try:
        with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
            with conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta["thread_id"] = thread_id
                    if platform == "slack":
                        meta["chat_id"] = os.environ.get("SLACK_HOME_CHANNEL", "")
                    else:
                        meta["chat_id"] = thread_id.split("/threads/")[0]
                    
                    # Update SQLite metadata table
                    conn.execute(
                        "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id)
                    )
    except Exception as exc:
        logger.error(f"Failed to update session metadata with thread_id: {exc}")


def _create_gateway_session(api_url: str, session_id: str, headers: Dict[str, str]) -> bool:
    """POST request to local gateway API to initialize the troubleshooting session ID."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions",
            data=json.dumps({"session_id": session_id, "title": f"Triage {session_id}"}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:  # 409 Conflict means it already exists, which is acceptable
            return True
        logger.error(f"Failed to create gateway API session (code {exc.code}): {exc.read().decode()}")
    except Exception as exc:
        logger.error(f"Failed to connect to gateway API server: {exc}")
    return False


def _k8s_event_query_head(session_id: str, payload: Dict[str, Any], cluster_name: str) -> str:
    """Head of the agent query for a Kubernetes event (unchanged legacy framing)."""
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    return (
        f"Analyze the following Kubernetes event warning on GKE cluster '{cluster_name}' "
        f"for the active session '{session_id}'.\n\n"
        f"**Event Details:**\n"
        f"• *Resource:* {namespace}/{object_kind}/{object_name}\n"
        f"• *Event Reason:* {event_reason}\n"
        f"• *Warning Message:* {message}\n\n"
    )


def _alert_query_head(session_id: str, payload: Dict[str, Any], cluster_name: str) -> str:
    """Head of the agent query for a monitoring/alerting webhook."""
    title = payload.get("summary") or payload.get("reason") or "Alert"
    policy = payload.get("policy") or payload.get("name") or "unknown-policy"
    severity = str(payload.get("severity") or payload.get("type") or "warning")
    namespace = payload.get("namespace") or "(cluster-wide)"
    message = payload.get("message") or ""
    return (
        f"Investigate the following monitoring alert on GKE cluster '{cluster_name}' "
        f"for the active session '{session_id}'.\n\n"
        f"**Alert Details:**\n"
        f"• *Policy:* {policy}\n"
        f"• *Severity:* {severity}\n"
        f"• *Scope:* {namespace}\n"
        f"• *Summary:* {title}\n"
        f"• *Detail:* {message}\n\n"
    )


def _github_query_head(session_id: str, payload: Dict[str, Any]) -> str:
    """Head of the agent query for a GitHub webhook."""
    action = payload.get("action") or "event"
    repo = payload.get("repo") or payload.get("repository") or "(unknown repo)"
    title = payload.get("title") or payload.get("message") or ""
    number = payload.get("number")
    ref = f"#{number}" if number is not None else ""
    return (
        f"Review the following GitHub activity for the active session '{session_id}'.\n\n"
        f"**GitHub Details:**\n"
        f"• *Repository:* {repo}\n"
        f"• *Action:* {action} {ref}\n"
        f"• *Title:* {title}\n\n"
    )


def _escalation_query_head(session_id: str, payload: Dict[str, Any]) -> str:
    """Head of the agent query for an escalation surfaced from a lower tier."""
    origin = payload.get("from") or payload.get("from_tier") or "a lower tier"
    summary = payload.get("summary") or payload.get("message") or "escalation raised"
    scope = payload.get("namespace") or payload.get("scope") or "(unspecified)"
    ref = payload.get("ref") or payload.get("path") or ""
    ref_line = f"• *Escalation record:* {ref}\n" if ref else ""
    return (
        f"An escalation was raised from {origin} for the active session '{session_id}'.\n\n"
        f"**Escalation Details:**\n"
        f"• *From:* {origin}\n"
        f"• *Reported scope:* {scope}\n"
        f"{ref_line}"
        f"• *Summary:* {summary}\n\n"
        f"Re-derive the affected scope yourself from read-only cluster state before acting; "
        f"do not trust the reported scope blindly, and never contact the lower tier directly.\n\n"
    )


# Keys the ChatOps router / event source may use to carry per-turn attribution into the inject request
# (Phase 5 T-A, 06 §8; acceptance d). The router stamps kage_sender / kage_trace_id on the dispatched
# message; an event source may instead name them requested_by / trace_id. Both spellings are accepted so
# the attribution survives whichever path fed the inject.
_ATTR_REQUESTED_BY_KEYS = ("requested_by", "kage_sender", "requester", "sender")
_ATTR_TRACE_ID_KEYS = ("trace_id", "kage_trace_id", "traceID")


def _first_present(*sources: Dict[str, Any], keys: tuple = ()) -> str:
    """Return the first non-empty value for any of `keys` across `sources` (in order), else ''."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            val = src.get(key)
            if val:
                return str(val).strip()
    return ""


def _extract_attribution(request_data: Dict[str, Any], payload: Dict[str, Any]) -> tuple[str, str]:
    """Pull (requested_by, trace_id) from the inject request, preferring the top-level envelope.

    The router-added correlation fields ride on the dispatched message; the credential proxy forwards them
    either at the top level of the inject body or inside the inner payload. Missing values stay empty here
    — submit_suggestion then falls back to the autonomous attribution, so a signal-driven turn with no
    human requester is still attributable, never silently unattributed.
    """
    requested_by = _first_present(request_data, payload, keys=_ATTR_REQUESTED_BY_KEYS)
    trace_id = _first_present(request_data, payload, keys=_ATTR_TRACE_ID_KEYS)
    return requested_by, trace_id


def _record_session_attribution(session_id: str, requested_by: str, trace_id: str) -> None:
    """Merge (requested_by, trace_id) into the session's metadata row so the turn is auditable.

    Attribution is persisted (not just passed to the agent) so an operator can, from get_metadata alone,
    tie a session to the human + turn that triggered it — the read side of acceptance d. Best-effort: a
    metadata write failure must never block the actual notification/turn.
    """
    if not (requested_by or trace_id):
        return
    try:
        with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
            with conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                meta = json.loads(row[0]) if row else {}
                if requested_by:
                    meta["requested_by"] = requested_by
                if trace_id:
                    meta["trace_id"] = trace_id
                conn.execute(
                    "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET metadata = excluded.metadata, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (session_id, json.dumps(meta)),
                )
    except Exception as exc:
        logger.error(f"Failed to record session attribution for {session_id}: {exc}")


def _attribution_instruction(requested_by: str, trace_id: str) -> str:
    """The line that tells the agent to stamp the attribution trailers on any GitOps PR it opens.

    submit_suggestion stamps Requested-by:/Trace-Id: unconditionally (falling back to autonomous), but
    passing the router-provided values explicitly is what ties a merged PR back to THIS turn's requester.
    Emitted only when at least one value is known; a purely autonomous turn omits it and lets the script's
    fallback attribute to the agent identity.
    """
    if not (requested_by or trace_id):
        return ""
    flags = []
    if requested_by:
        flags.append(f"--requested-by '{requested_by}'")
    if trace_id:
        flags.append(f"--trace-id '{trace_id}'")
    return (
        "\n4. ATTRIBUTION (required): when you run the submit-suggestion skill to open the PR, pass "
        f"{' '.join(flags)} so the change is attributable to the requester and this exact turn "
        "(they become the Requested-by:/Trace-Id: PR trailers)."
    )


def _report_and_gitops_tail(session_id: str, project_query: str, requested_by: str = "", trace_id: str = "") -> str:
    """Shared reporting-format + read-only GitOps-PR instruction tail for every kind."""
    return (
        f"When calling your send_notification tool to report findings, you MUST pass this exact session ID: '{session_id}' as the session_id argument so it routes as a threaded reply to the warning alert.\n\n"
        f"When done, post your final diagnostic report to the chat platform (using your notification tool) formatted exactly like this:\n\n"
        f"📋 *Incident Triage*\n\n"
        f"• *Issue:* <Short 1-sentence description of the problem>\n"
        f"• *Root Cause:* <Key constraint mismatch or log finding in 1-2 sentences>\n\n"
        f"🛠️ *Proposed Fixes (GitOps):*\n"
        f"*Option A (<Action Title>):* <1-sentence description of Option A GitOps fix>.\n"
        f"*Option B (<Action Title>):* <1-sentence description of Option B GitOps fix>.\n\n"
        f"🔗 <https://console.cloud.google.com/kubernetes/workload/overview{project_query}|GKE Workloads> | "
        f"<https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22{project_query}|Cloud Logs>\n\n"
        f"👉 *Reply to this thread with 'apply Option A' or 'apply Option B' to automatically open a GitOps Pull Request with the fix.*\n\n"
        f"---"
        f"\n\n**GitOps PR Instructions (For subsequent turns if the user replies):**\n"
        f"If the user replies to the thread with 'apply Option A' or 'apply Option B':\n"
        f"1. You are explicitly authorized to create a new branch, modify the resource manifests in the local checkout, commit, push, and open a GitHub Pull Request matching the selected option.\n"
        f"2. Post a threaded response confirming the PR was created and include the clickable PR link.\n"
        f"3. Do not execute any write mutations (kubectl scale, patch, or apply) directly on the live cluster."
        + _attribution_instruction(requested_by, trace_id)
    )


def _build_agent_query(session_id: str, payload: Dict[str, Any]) -> str:
    """Format a detailed Markdown diagnostic query for the agent, framed by signal kind.

    The k8s-event head is preserved verbatim; other kinds get a source-appropriate head so
    the agent is not told to "analyze a Kubernetes event" for an alert/GitHub/escalation
    signal. All kinds share the same read-only reporting + GitOps-PR tail.
    """
    cluster_name = os.environ.get("GKE_CLUSTER_NAME", "platform-agent-host")
    gcp_project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GCP_PROJECT") or ""
    project_query = f"?project={gcp_project}" if gcp_project else ""
    kind = (payload.get("kind") or "").strip()

    if kind == INJECT_KIND_ALERT:
        head = _alert_query_head(session_id, payload, cluster_name)
    elif kind == INJECT_KIND_GITHUB:
        head = _github_query_head(session_id, payload)
    elif kind == INJECT_KIND_ESCALATION:
        head = _escalation_query_head(session_id, payload)
    else:  # k8s-event / k8s-event-followup (default framing)
        head = _k8s_event_query_head(session_id, payload, cluster_name)

    # Attribution (Phase 5 T-A): the router-provided requester + per-turn trace id ride on the payload
    # (stashed by inject_message) so the GitOps tail can instruct the agent to stamp them on any PR.
    requested_by = str(payload.get("kage_requested_by") or "").strip()
    trace_id = str(payload.get("kage_trace_id") or "").strip()
    return head + _report_and_gitops_tail(session_id, project_query, requested_by, trace_id)


def _start_agent_turn(api_url: str, session_id: str, query: str, headers: Dict[str, str]) -> None:
    """Post the agent query request to execute the diagnostic reasoning loop."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions/{session_id}/chat",
            data=json.dumps({"message": query}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300.0) as resp:
            if resp.status != 200:
                logger.error(f"Gateway API chat execution failed (status {resp.status})")
    except Exception as exc:
        logger.error(f"Failed to call gateway API chat execution: {exc}")


def trigger_agent_troubleshooter(session_id: str, alert_msg: str, payload: Dict[str, Any]) -> None:
    """Post warning alert to Chat, configure thread mapping, and trigger the agent loop in background."""
    active_platform = get_active_platform()
    
    # 1. Post initial warning notification to Google Chat or Slack
    thread_id = _post_initial_alert(active_platform, alert_msg)
    
    # 2. Register thread-to-session mappings for two-way chat routing
    if thread_id:
        _register_session_routing(session_id, active_platform, thread_id)

    # 3. Configure HTTP authentication headers for Hermes REST gateway
    api_url = os.environ.get("PLATFORM_API_URL", "http://127.0.0.1:8642")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_SERVER_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # 4. Instantiate the session in Platform Gateway
    session_created = _create_gateway_session(api_url, session_id, headers)
    if not session_created:
        logger.error(f"Aborting troubleshooting trigger: session creation failed for {session_id}")
        return

    # 5. Formulate instructions query and execute the agent turn
    agent_query = _build_agent_query(session_id, payload)
    _start_agent_turn(api_url, session_id, agent_query, headers)


@app.post("/sessions/{session_id}/inject")
def inject_message(
    session_id: str,
    request_data: Dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
    x_asserted_caller: Optional[str] = Header(default=None),
) -> Dict[str, str]:
    """Receive the event payload and notify the Platform Agent via Google Chat."""
    _require_inject_auth(authorization, x_asserted_caller)
    raw_message = request_data.get("message", "")
    if not raw_message:
        raise HTTPException(status_code=400, detail="message field is required")

    try:
        payload = json.loads(raw_message)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse inner payload JSON: {exc}")

    # Route on the signal kind (04 §4). Missing/unknown kinds are rejected so a new source
    # cannot be silently coerced through the Kubernetes-event rendering path.
    kind = (payload.get("kind") or "").strip()
    if kind not in _KNOWN_INJECT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or missing inject kind: {kind!r} (expected one of {sorted(_KNOWN_INJECT_KINDS)})",
        )

    # Attribution (Phase 5 T-A, acceptance d): capture the requester + per-turn trace id the router carried
    # in, persist them to the session metadata (audit read side), and stash them on the payload so the
    # agent-query builder can instruct the agent to stamp them as PR trailers (the mutation write side).
    requested_by, trace_id = _extract_attribution(request_data, payload)
    _record_session_attribution(session_id, requested_by, trace_id)
    if requested_by:
        payload["kage_requested_by"] = requested_by
    if trace_id:
        payload["kage_trace_id"] = trace_id

    alert_msg = _format_inject_card(kind, payload)

    # Delegate the heavy REST API call to FastAPI BackgroundTasks to keep response times sub-millisecond
    background_tasks.add_task(trigger_agent_troubleshooter, session_id, alert_msg, payload)

    return {"status": "injected"}


@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str) -> Dict[str, Any]:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session metadata not found")

    try:
        return json.loads(row[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Data decoding failure: {exc}")


@app.get("/v1/sessions")
def list_sessions(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(limit, 1000))
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        rows = conn.execute(
            """
            SELECT session_id, metadata, updated_at
            FROM session_metadata
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sessions = []
    for session_id, metadata, updated_at in rows:
        try:
            parsed = json.loads(metadata)
        except Exception:
            parsed = {}
        sessions.append(
            {
                "session_id": session_id,
                "metadata": parsed,
                "updated_at": updated_at,
            }
        )
    return {"sessions": sessions}


@app.post("/v1/incidents")
def store_incident(body: Dict[str, Any]) -> Dict[str, str]:
    chat_id, thread_id, report = body.get("chat_id"), body.get("thread_id"), body.get("report")
    if not (chat_id and thread_id and report):
        raise HTTPException(status_code=400, detail="chat_id, thread_id, report required")
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            # keep the FIRST report per thread (the one carrying the options)
            conn.execute(
                "INSERT OR IGNORE INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                (chat_id, thread_id, report),
            )
            cleanup_old_records(conn)
    return {"status": "stored"}


@app.get("/v1/incidents/by-thread")
def get_incident(chat_id: str, thread_id: str) -> Dict[str, str]:
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no incident for thread")
    return {"chat_id": chat_id, "thread_id": thread_id, "report": row[0]}


init_db()
