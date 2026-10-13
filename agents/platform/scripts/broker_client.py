#!/usr/bin/env python3
"""Action Broker client for Kubernetes Platform Agents.

Constructs RFC 8785 JCS-canonicalized ActionEnvelopes with deterministic
idempotency keys and submits them to the Action Broker over either:
  1. HTTPS mTLS + projected ServiceAccount token (`KUBEAGENTS_BROKER_ENDPOINT`), or
  2. NATS JetStream (`a2a propose --file -` when `A2A_NATS_URL` / `NATS_URL` is set).
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

APIVERSION = "kubeagents.x-k8s.io/v1alpha1"
KIND = "ActionEnvelope"
KEY_PREFIX = "sha256:"

DEFAULT_TOKEN_PATH = "/var/run/secrets/kubeagents.io/broker-token/token"
DEFAULT_CA_PATH = "/var/run/secrets/kubeagents.io/broker-tls/ca.crt"
DEFAULT_CERT_PATH = "/var/run/secrets/kubeagents.io/broker-client-tls/tls.crt"
DEFAULT_KEY_PATH = "/var/run/secrets/kubeagents.io/broker-client-tls/tls.key"
K8S_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_SA_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


def _jcs_escape_string(s: str) -> str:
  out = ['"']
  for ch in s:
    code = ord(ch)
    if ch == '"':
      out.append('\\"')
    elif ch == "\\":
      out.append("\\\\")
    elif code == 0x08:
      out.append("\\b")
    elif code == 0x0C:
      out.append("\\f")
    elif code == 0x0A:
      out.append("\\n")
    elif code == 0x0D:
      out.append("\\r")
    elif code == 0x09:
      out.append("\\t")
    elif code < 0x20:
      out.append(f"\\u{code:04x}")
    else:
      out.append(ch)
  out.append('"')
  return "".join(out)


def _utf16_sort_key(s: str) -> bytes:
  return s.encode("utf-16-be")


def _jcs_format_number(val: int | float) -> str:
  if isinstance(val, int) and not isinstance(val, bool):
    if val == 0:
      return "0"
    if abs(val) < 10**21:
      return str(val)
    val = float(val)

  f = float(val)
  if math.isnan(f) or math.isinf(f):
    raise ValueError("JCS does not permit NaN or Infinity")
  if f == 0.0:
    return "0"

  abs_f = abs(f)
  if 1e-6 <= abs_f < 1e21:
    if f.is_integer():
      return str(int(f))
    s = repr(f)
    if "e" not in s and "E" not in s:
      return s
    # Convert scientific repr inside [1e-6, 1e21) to fixed-point without trailing zeros.
    mantissa, exp_str = s.lower().split("e")
    exp = int(exp_str)
    sign = "-" if mantissa.startswith("-") else ""
    if sign:
      mantissa = mantissa[1:]
    if "." in mantissa:
      int_part, frac_part = mantissa.split(".")
    else:
      int_part, frac_part = mantissa, ""
    digits = int_part + frac_part
    dec_pos = len(int_part) + exp
    if dec_pos <= 0:
      return f"{sign}0.{'0' * (-dec_pos)}{digits}".rstrip("0")
    if dec_pos >= len(digits):
      return f"{sign}{digits}{'0' * (dec_pos - len(digits))}"
    return f"{sign}{digits[:dec_pos]}.{digits[dec_pos:]}".rstrip("0").rstrip(".")

  s = repr(f).lower()
  if "e" not in s:
    s = f"{f:e}"
  mantissa, exp_str = s.split("e")
  if "." in mantissa:
    mantissa = mantissa.rstrip("0").rstrip(".")
  exp_val = int(exp_str)
  exp_sign = "+" if exp_val >= 0 else "-"
  return f"{mantissa}e{exp_sign}{abs(exp_val)}"


def jcs_canonicalize(value: Any) -> bytes:
  """Serializes `value` according to RFC 8785 (JCS) as UTF-8 bytes."""

  def _serialize(v: Any) -> str:
    if v is None:
      return "null"
    if v is True:
      return "true"
    if v is False:
      return "false"
    if isinstance(v, (int, float)):
      return _jcs_format_number(v)
    if isinstance(v, str):
      return _jcs_escape_string(v)
    if isinstance(v, (list, tuple)):
      return "[" + ",".join(_serialize(item) for item in v) + "]"
    if isinstance(v, dict):
      sorted_keys = sorted(v.keys(), key=_utf16_sort_key)
      pairs = [f"{_jcs_escape_string(k)}:{_serialize(v[k])}" for k in sorted_keys]
      return "{" + ",".join(pairs) + "}"
    raise TypeError(f"Unsupported type for JCS canonicalization: {type(v)!r}")

  return _serialize(value).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def _sanitize_object_for_key(obj: dict[str, Any], is_secret: bool) -> dict[str, Any]:
  out = json.loads(json.dumps(obj))
  meta = out.get("metadata")
  if isinstance(meta, dict):
    meta.pop("managedFields", None)
    annotations = meta.get("annotations")
    if isinstance(annotations, dict):
      annotations.pop(LAST_APPLIED_ANNOTATION, None)

  if is_secret:
    for field in ("data", "stringData"):
      raw_map = out.get(field)
      if isinstance(raw_map, dict):
        digested: dict[str, Any] = {}
        for k, v in raw_map.items():
          if isinstance(v, str):
            digested[k] = KEY_PREFIX + _sha256_hex(v.encode("utf-8"))
          else:
            encoded = json.dumps(v, separators=(",", ":")).encode("utf-8")
            digested[k] = KEY_PREFIX + _sha256_hex(encoded)
        out[field] = digested
  return out


def _sanitize_patch_for_key(patch: dict[str, Any], is_secret: bool) -> dict[str, Any]:
  body = patch.get("body")
  if not is_secret:
    if isinstance(body, dict):
      return {
          "type": patch.get("type", ""),
          "body": _sanitize_object_for_key(body, False),
      }
    return {"type": patch.get("type", ""), "body": body}

  if isinstance(body, dict):
    return {
        "type": patch.get("type", ""),
        "body": _sanitize_object_for_key(body, True),
    }
  if isinstance(body, list):
    sanitized_ops = []
    for entry in body:
      if not isinstance(entry, dict):
        sanitized_ops.append(entry)
        continue
      path = entry.get("path", "")
      if (
          isinstance(path, str)
          and (path.startswith("/data/") or path.startswith("/stringData/"))
          and "value" in entry
      ):
        copied = dict(entry)
        encoded = json.dumps(copied["value"], separators=(",", ":")).encode("utf-8")
        copied["value"] = KEY_PREFIX + _sha256_hex(encoded)
        sanitized_ops.append(copied)
      else:
        sanitized_ops.append(entry)
    return {"type": patch.get("type", ""), "body": sanitized_ops}
  return {"type": patch.get("type", ""), "body": body}


def _normalize_target(target: dict[str, Any]) -> dict[str, Any]:
  norm: dict[str, Any] = {}
  group = target.get("group", "")
  version = target.get("version", "")
  if not version and "apiVersion" in target:
    api_ver = target["apiVersion"]
    if "/" in api_ver:
      group, version = api_ver.split("/", 1)
    else:
      group, version = "", api_ver
  if group:
    norm["group"] = group
  norm["version"] = version
  norm["kind"] = target.get("kind", "")
  if target.get("namespace"):
    norm["namespace"] = target["namespace"]
  norm["name"] = target.get("name", "")
  return norm


def _reduce_operation_for_key(op: dict[str, Any]) -> dict[str, Any]:
  reduced: dict[str, Any] = {"op": op["op"]}
  is_secret = False

  if op.get("target") is not None:
    t = _normalize_target(op["target"])
    reduced["target"] = t
    is_secret = t.get("kind") == "Secret"
  elif op.get("targetSelector") is not None:
    s = dict(op["targetSelector"])
    reduced["targetSelector"] = s
    is_secret = s.get("kind") == "Secret"
  elif op.get("cloudTarget") is not None:
    reduced["cloudTarget"] = dict(op["cloudTarget"])

  if op.get("desiredState"):
    reduced["desiredState"] = _sanitize_object_for_key(op["desiredState"], is_secret)
  if op.get("patch") is not None:
    reduced["patch"] = _sanitize_patch_for_key(op["patch"], is_secret)
  if op.get("delete") is not None:
    reduced["delete"] = dict(op["delete"])
  if op.get("scale") is not None:
    reduced["scale"] = {"replicas": int(op["scale"]["replicas"])}
  return reduced


def _operation_sort_key(op: dict[str, Any]) -> str:
  group = version = kind = namespace = name = ""
  if "target" in op and op["target"] is not None:
    t = op["target"]
    group = t.get("group", "")
    version = t.get("version", "")
    kind = t.get("kind", "")
    namespace = t.get("namespace", "")
    name = t.get("name", "")
  elif "targetSelector" in op and op["targetSelector"] is not None:
    s = op["targetSelector"]
    group = s.get("group", "")
    version = s.get("version", "")
    kind = s.get("kind", "")
    namespace = s.get("namespace", "")
    name = s.get("labelSelector", "")
  elif "cloudTarget" in op and op["cloudTarget"] is not None:
    c = op["cloudTarget"]
    group = c.get("provider", "")
    version = ""
    kind = c.get("service", "")
    namespace = ""
    name = f"{c.get('resource', '')}#{c.get('method', '')}"
  return f"{op['op']}\x1f{group}/{version}/{kind}/{namespace}/{name}"


def compute_idempotency_key(
    agent_identity: str,
    operations: list[dict[str, Any]],
    dry_run: bool = False,
) -> str:
  """Computes the exact `sha256:<hex>` idempotency key matching Go's `broker.ComputeIdempotencyKey`."""
  if not agent_identity:
    raise ValueError("agent_identity is required to compute idempotencyKey")
  reduced_ops = [_reduce_operation_for_key(o) for o in operations]
  reduced_ops.sort(key=_operation_sort_key)
  key_input = {
      "agentIdentity": agent_identity,
      "dryRun": bool(dry_run),
      "operations": reduced_ops,
  }
  canonical = jcs_canonicalize(key_input)
  return KEY_PREFIX + _sha256_hex(canonical)


def resolve_agent_identity(
    explicit_identity: str | None = None,
    token_path: str | None = None,
    namespace: str | None = None,
    agent_name: str | None = None,
) -> str:
  if explicit_identity:
    return explicit_identity
  env_id = os.environ.get("KUBEAGENTS_AGENT_IDENTITY", "").strip()
  if env_id:
    return env_id

  for candidate_path in (
      token_path or os.environ.get("KUBEAGENTS_BROKER_TOKEN_PATH", DEFAULT_TOKEN_PATH),
      K8S_SA_TOKEN_PATH,
  ):
    if candidate_path and os.path.isfile(candidate_path):
      try:
        with open(candidate_path, "r", encoding="utf-8") as f:
          token = f.read().strip()
        parts = token.split(".")
        if len(parts) >= 2:
          padded = parts[1] + "=" * (-len(parts[1]) % 4)
          claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
          sub = claims.get("sub", "")
          if isinstance(sub, str) and sub.startswith("system:serviceaccount:"):
            return sub
      except Exception:
        pass

  ns = (
      namespace
      or os.environ.get("KUBEAGENTS_AGENT_NAMESPACE")
      or os.environ.get("A2A_NAMESPACE")
      or os.environ.get("POD_NAMESPACE")
      or _read_file_if_exists(K8S_SA_NS_PATH)
      or "default"
  )
  sa = os.environ.get("KUBEAGENTS_AGENT_SA", "").strip()
  if not sa:
    ag = (
        agent_name
        or os.environ.get("KUBEAGENTS_AGENT_NAME")
        or os.environ.get("A2A_AGENT_NAME")
        or "platform"
    )
    sa = ag if ag.endswith("-agent-reader") else f"{ag}-agent-reader"
  return f"system:serviceaccount:{ns}:{sa}"


def _read_file_if_exists(path: str) -> str:
  try:
    if os.path.isfile(path):
      with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()
  except OSError:
    pass
  return ""


def _build_ssl_context(
    ca_path: str | None,
    cert_path: str | None,
    key_path: str | None,
) -> ssl.SSLContext:
  ca = ca_path or os.environ.get("KUBEAGENTS_BROKER_CA_PATH", DEFAULT_CA_PATH)
  cert = cert_path or os.environ.get("KUBEAGENTS_BROKER_CERT_PATH", DEFAULT_CERT_PATH)
  key = key_path or os.environ.get("KUBEAGENTS_BROKER_KEY_PATH", DEFAULT_KEY_PATH)

  if ca and os.path.isfile(ca):
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
  else:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
  if cert and key and os.path.isfile(cert) and os.path.isfile(key):
    ctx.load_cert_chain(certfile=cert, keyfile=key)
  return ctx


def fetch_nonce_http(
    endpoint: str,
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    timeout: float = 10.0,
) -> str:
  url = endpoint.rstrip("/") + "/v1alpha1/nonce"
  headers = {"Accept": "application/json"}
  if token:
    headers["Authorization"] = f"Bearer {token}"

  for method in ("GET", "POST"):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
      with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        nonce = data.get("nonce", "")
        if not nonce:
          raise RuntimeError(f"Broker nonce response missing 'nonce': {data}")
        return nonce
    except urllib.error.HTTPError as e:
      if e.code == 405 and method == "GET":
        continue
      body = e.read().decode("utf-8", errors="replace")
      raise RuntimeError(f"Nonce request failed (HTTP {e.code}): {body}") from e
  raise RuntimeError("Failed to obtain nonce from broker")


def submit_envelope_http(
    endpoint: str,
    envelope: dict[str, Any],
    token: str,
    ssl_ctx: ssl.SSLContext | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
  url = endpoint.rstrip("/") + "/v1alpha1/actions"
  payload = jcs_canonicalize(envelope)
  headers = {
      "Content-Type": "application/json",
      "Accept": "application/json",
  }
  if token:
    headers["Authorization"] = f"Bearer {token}"

  req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
  try:
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
      raw = resp.read().decode("utf-8")
      return resp.status, json.loads(raw) if raw else {}
  except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8", errors="replace")
    try:
      parsed = json.loads(raw) if raw else {"reason": f"http-{e.code}", "message": str(e)}
    except json.JSONDecodeError:
      parsed = {"reason": f"http-{e.code}", "message": raw}
    return e.code, parsed


def submit_envelope_nats(
    envelope: dict[str, Any],
    namespace: str | None = None,
    agent_name: str | None = None,
    a2a_bin: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
  binary = a2a_bin or os.environ.get("A2A_BIN") or shutil.which("a2a") or "/usr/local/bin/a2a"
  cmd = [binary, "propose", "--file", "-", f"--timeout={int(timeout)}s"]
  if namespace:
    cmd.extend(["--namespace", namespace])
  if agent_name:
    cmd.extend(["--agent", agent_name])

  payload = jcs_canonicalize(envelope)
  proc = subprocess.run(
      cmd,
      input=payload,
      capture_output=True,
      check=False,
  )
  stdout_str = proc.stdout.decode("utf-8", errors="replace").strip()
  stderr_str = proc.stderr.decode("utf-8", errors="replace").strip()
  if stdout_str:
    try:
      data = json.loads(stdout_str.splitlines()[-1])
      status = 200 if proc.returncode == 0 else 403
      return status, data
    except json.JSONDecodeError:
      pass
  if proc.returncode != 0:
    return 500, {
        "decision": "refused",
        "reason": "nats-propose-failed",
        "message": stderr_str or stdout_str or f"a2a propose exited with {proc.returncode}",
    }
  return 200, {"decision": "accepted", "message": stdout_str}


def build_envelope(
    operations: list[dict[str, Any]],
    intent: str,
    agent_identity: str,
    nonce: str,
    issued_at: str | None = None,
    rationale: str | None = None,
    dry_run: bool = False,
    require_approval: bool = False,
    max_objects: int | None = None,
    deadline_seconds: int | None = None,
    requester: dict[str, Any] | None = None,
    trigger: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
  norm_ops = []
  for op in operations:
    copied = dict(op)
    if "target" in copied and copied["target"] is not None:
      copied["target"] = _normalize_target(copied["target"])
    norm_ops.append(copied)

  if requester is None:
    req_id = (
        os.environ.get("A2A_REQUESTER_ID")
        or os.environ.get("KUBEAGENTS_REQUESTER_ID")
        or f"agent:{os.environ.get('KUBEAGENTS_AGENT_NAME') or os.environ.get('A2A_AGENT_NAME') or 'platform'}"
    )
    req_kind = os.environ.get("A2A_REQUESTER_KIND")
    if not req_kind:
      if req_id.startswith(("slack:", "googlechat:", "kubectl:")):
        req_kind = "human"
      elif req_id.startswith(("agent:", "session:")):
        req_kind = "agent"
      else:
        req_kind = "system"
    req_platform = os.environ.get("A2A_REQUESTER_PLATFORM", "")
    if not req_platform:
      for p in ("slack", "googlechat", "kubectl"):
        if req_id.startswith(p + ":"):
          req_platform = p
          break
      if not req_platform and req_kind == "agent":
        req_platform = "mesh"
    requester = {"kind": req_kind, "id": req_id}
    if req_platform:
      requester["platform"] = req_platform
    cap_key = os.environ.get("A2A_CAPABILITY_KEY", "").strip()
    cap_rev = os.environ.get("A2A_CAPABILITY_REVISION", "").strip()
    if cap_key and cap_rev and "assertion" not in requester:
      requester["assertion"] = f"{cap_key} @{cap_rev}"

  if trigger is None:
    trigger_src = os.environ.get("A2A_TRIGGER_SOURCE", "chat")
    trigger = {"source": trigger_src}
    if os.environ.get("A2A_TRIGGER_REF"):
      trigger["ref"] = os.environ["A2A_TRIGGER_REF"]

  if trace is None:
    raw_trace = os.environ.get("A2A_TRACE_ID", "").strip().lower()
    if len(raw_trace) == 32 and all(c in "0123456789abcdef" for c in raw_trace):
      trace_id = raw_trace
    else:
      trace_id = os.urandom(16).hex()
    trace = {"traceId": trace_id}
    if os.environ.get("A2A_SESSION"):
      trace["sessionId"] = os.environ["A2A_SESSION"]
    if os.environ.get("A2A_THREAD_ID"):
      trace["threadId"] = os.environ["A2A_THREAD_ID"]

  if not issued_at:
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

  idem_key = compute_idempotency_key(agent_identity, norm_ops, dry_run=dry_run)

  env: dict[str, Any] = {
      "apiVersion": APIVERSION,
      "kind": KIND,
      "intent": intent,
      "operations": norm_ops,
      "requester": requester,
      "trigger": trigger,
      "trace": trace,
      "issuedAt": issued_at,
      "nonce": nonce,
      "idempotencyKey": idem_key,
  }
  if rationale:
    env["rationale"] = rationale
  if dry_run:
    env["dryRun"] = True
  if require_approval:
    env["requireApproval"] = True
  if max_objects is not None:
    env["maxObjects"] = int(max_objects)
  if deadline_seconds is not None:
    env["deadlineSeconds"] = int(deadline_seconds)
  return env


def _build_operation_from_args(args: argparse.Namespace) -> dict[str, Any]:
  api_ver = args.api_version or "v1"
  if "/" in api_ver:
    group, version = api_ver.split("/", 1)
  else:
    group, version = "", api_ver

  target: dict[str, Any] = {
      "version": version,
      "kind": args.kind,
      "name": args.name or "<name>",
  }
  if group:
    target["group"] = group
  if args.namespace:
    target["namespace"] = args.namespace

  op_dict: dict[str, Any] = {
      "op": args.op,
      "target": target,
  }

  if args.op == "scale":
    replicas = args.replicas if args.replicas is not None else 1
    op_dict["scale"] = {"replicas": int(replicas)}
  elif args.op in ("create", "apply"):
    if args.desired_file:
      raw = (
          sys.stdin.read()
          if args.desired_file == "-"
          else open(args.desired_file, "r", encoding="utf-8").read()
      )
      op_dict["desiredState"] = json.loads(raw)
    elif args.desired:
      op_dict["desiredState"] = json.loads(args.desired)
    else:
      op_dict["desiredState"] = {
          "apiVersion": api_ver,
          "kind": args.kind,
          "metadata": {"name": target["name"], "namespace": args.namespace or "default"},
      }
  elif args.op == "patch":
    body = json.loads(args.patch) if args.patch else {}
    op_dict["patch"] = {
        "type": args.patch_type or "application/merge-patch+json",
        "body": body,
    }
  return op_dict


def _resolve_kubectl_bin() -> str:
  for candidate in (
      shutil.which("kubectl"),
      "/opt/credential-proxy/bin/kubectl",
      "/usr/local/bin/kubectl",
      "/usr/bin/kubectl",
  ):
    if candidate and os.path.isfile(candidate):
      return candidate
  return "kubectl"


def _summarize_action_record(
    ar: dict[str, Any],
    undo_items: list[dict[str, Any]],
) -> dict[str, Any]:
  meta = ar.get("metadata") or {}
  spec = ar.get("spec") or {}
  status = ar.get("status") or {}
  env = spec.get("envelope") or {}
  if not env and isinstance(spec.get("envelopeJson"), str) and spec["envelopeJson"]:
    try:
      env = json.loads(spec["envelopeJson"])
    except Exception:
      env = {}
  cls_info = spec.get("classification") or {}

  action_id = spec.get("actionId") or meta.get("name", "")
  record_name = meta.get("name", "")
  ns = meta.get("namespace", "")

  targets: list[str] = []
  ops_source = env.get("operations") or spec.get("operations") or []
  for op in ops_source:
    verb = op.get("op", "")
    t = op.get("target") or {}
    if t:
      t_ns = t.get("namespace")
      if t_ns:
        t_str = f"{t.get('kind', '')}/{t_ns}/{t.get('name', '')}"
      else:
        t_str = f"{t.get('kind', '')}/{t.get('name', '')}"
      if verb == "scale" and isinstance(op.get("scale"), dict):
        t_str += f" (scale replicas={op['scale'].get('replicas')})"
      elif verb:
        t_str += f" (op={verb})"
      targets.append(t_str)
  if not targets:
    for t in spec.get("targets") or []:
      t_ns = t.get("namespace")
      if t_ns:
        targets.append(f"{t.get('kind', '')}/{t_ns}/{t.get('name', '')}")
      else:
        targets.append(f"{t.get('kind', '')}/{t.get('name', '')}")

  phase = status.get("phase") or "Unknown"
  contested = bool(status.get("contested", False))
  undone_by = status.get("undoneBy") or ""
  timestamps = status.get("timestamps") or {}

  matching_undos: list[dict[str, Any]] = []
  for u in undo_items:
    u_spec = u.get("spec") or {}
    u_meta = u.get("metadata") or {}
    u_status = u.get("status") or {}
    action_ref_name = ((u_spec.get("actionRef") or {}).get("name") or "").strip()
    target_id = (u_spec.get("targetActionId") or action_ref_name).strip()
    if (
        target_id.upper() == action_id.upper()
        or (record_name and target_id.lower() == record_name.lower())
    ):
      matching_undos.append({
          "name": u_meta.get("name", ""),
          "createdAt": u_meta.get("creationTimestamp", ""),
          "requestedBy": u_spec.get("requestedBy", ""),
          "reason": u_spec.get("reason", ""),
          "phase": u_status.get("phase", "Pending"),
          "undoActionId": u_status.get("undoActionId") or u_status.get("inverseActionId", ""),
          "completionTime": u_status.get("completionTime") or u_status.get("completedAt", ""),
          "message": u_status.get("message", ""),
      })

  chatops_cmds: list[str] = []
  if phase == "PendingApproval":
    chatops_cmds.append(f"/approve {action_id}")
    chatops_cmds.append(f"/reject {action_id} <reason>")
  elif phase == "Verified" and not undone_by:
    chatops_cmds.append(f"/undo {action_id} [optional reason]")
  if contested:
    chatops_cmds.append(f"/uncontest {action_id}")

  summary: dict[str, Any] = {
      "actionId": action_id,
      "recordName": record_name,
      "namespace": ns,
      "createdAt": meta.get("creationTimestamp", ""),
      "intent": spec.get("intent") or env.get("intent", ""),
      "rationale": spec.get("rationale") or env.get("rationale", ""),
      "dryRun": bool(spec.get("dryRun", env.get("dryRun", False))),
      "targets": targets,
      "riskClass": cls_info.get("class", ""),
      "phase": phase,
      "message": status.get("message", ""),
      "contested": contested,
      "timestamps": timestamps,
      "undoneBy": undone_by,
      "approvals": status.get("approvals") or {},
      "report": status.get("report") or {},
      "undoRequests": matching_undos,
      "availableChatOpsCommands": chatops_cmds,
  }
  return summary


def inspect_broker_journal(
    namespace: str,
    action_id: str | None = None,
    limit: int = 10,
) -> tuple[int, dict[str, Any]]:
  kubectl_bin = _resolve_kubectl_bin()
  ar_cmd = [kubectl_bin, "-n", namespace, "get", "actionrecords.kubeagents.x-k8s.io", "-o", "json"]
  ur_cmd = [kubectl_bin, "-n", namespace, "get", "undorequests.kubeagents.x-k8s.io", "-o", "json"]

  ar_proc = subprocess.run(ar_cmd, capture_output=True, check=False)
  if ar_proc.returncode != 0:
    err_msg = ar_proc.stderr.decode("utf-8", errors="replace").strip()
    return 1, {"error": "kubectl-get-actionrecords-failed", "message": err_msg}

  ur_proc = subprocess.run(ur_cmd, capture_output=True, check=False)
  ar_list = json.loads(ar_proc.stdout.decode("utf-8", errors="replace") or "{}").get("items") or []
  ur_list = []
  if ur_proc.returncode == 0 and ur_proc.stdout:
    ur_list = json.loads(ur_proc.stdout.decode("utf-8", errors="replace") or "{}").get("items") or []

  if action_id:
    clean = action_id.strip()
    if clean.lower().startswith("ar-"):
      clean = clean[3:]
    target_ar_name = "ar-" + clean.lower()
    target_id_upper = clean.upper()
    for ar in ar_list:
      meta = ar.get("metadata") or {}
      spec = ar.get("spec") or {}
      if meta.get("name", "").lower() == target_ar_name or (spec.get("actionId") or "").upper() == target_id_upper:
        return 0, _summarize_action_record(ar, ur_list)
    return 1, {
        "error": "not-found",
        "actionId": target_id_upper,
        "recordName": target_ar_name,
        "namespace": namespace,
        "message": f"ActionRecord {target_ar_name} ({target_id_upper}) not found in namespace {namespace}",
    }

  ar_list.sort(
      key=lambda item: (item.get("metadata") or {}).get("creationTimestamp", ""),
      reverse=True,
  )
  selected = ar_list[: max(1, limit)]
  summaries = [_summarize_action_record(ar, ur_list) for ar in selected]
  return 0, {
      "namespace": namespace,
      "totalCount": len(ar_list),
      "returnedCount": len(summaries),
      "actions": summaries,
  }


def _parse_args(argv: list[str]) -> argparse.Namespace:
  cleaned = list(argv)
  positional_op: str | None = None
  positional_action_id: str | None = None
  if cleaned and cleaned[0] in ("propose", "status", "list"):
    sub = cleaned.pop(0)
    if sub in ("status", "list"):
      positional_op = sub
      if cleaned and not cleaned[0].startswith("-"):
        positional_action_id = cleaned.pop(0)

  parser = argparse.ArgumentParser(
      prog="broker_client.py",
      description="Submit an ActionEnvelope to or inspect ActionRecords in the kube-agents Action Broker.",
  )
  parser.add_argument("--file", "-f", help="Path to an ActionEnvelope JSON file (or - for stdin)")
  parser.add_argument(
      "--op",
      choices=["create", "apply", "patch", "delete", "scale", "status", "list"],
      help="Operation verb (create/apply/patch/delete/scale to propose a mutation; status/list to inspect live ActionRecord & UndoRequest state)",
  )
  parser.add_argument("--action-id", help="Action ID (ULID or ar-<ulid>) for --op status")
  parser.add_argument("--limit", type=int, default=10, help="Max ActionRecords to return for --op list (default: 10)")
  parser.add_argument("--api-version", default="v1", help="Target Kubernetes apiVersion (e.g. apps/v1 or v1)")
  parser.add_argument("--kind", help="Target Kubernetes Kind (e.g. Deployment)")
  parser.add_argument("--namespace", "-n", default=os.environ.get("POD_NAMESPACE", "default"), help="Target namespace")
  parser.add_argument("--name", help="Target resource name (or Action ID when used with --op status)")
  parser.add_argument("--replicas", type=int, help="Desired replicas for --op scale")
  parser.add_argument("--desired-file", help="Path to JSON desiredState for create/apply")
  parser.add_argument("--desired", help="Inline JSON desiredState for create/apply")
  parser.add_argument("--patch-type", default="application/merge-patch+json", help="Patch content type")
  parser.add_argument("--patch", help="Inline JSON patch body for --op patch")
  parser.add_argument("--intent", help="Human-readable intent summary")
  parser.add_argument("--rationale", help="Detailed rationale for the mutation")
  parser.add_argument("--dry-run", action="store_true", help="Request broker dry-run classification/planning only")
  parser.add_argument("--require-approval", action="store_true", help="Force human approval gate")
  parser.add_argument("--max-objects", type=int, help="Optional caller maxObjects cap")
  parser.add_argument("--deadline-seconds", type=int, help="Optional deadlineSeconds cap")
  parser.add_argument("--endpoint", default=os.environ.get("KUBEAGENTS_BROKER_ENDPOINT", ""), help="Broker HTTPS endpoint")
  parser.add_argument("--token-path", default=os.environ.get("KUBEAGENTS_BROKER_TOKEN_PATH", DEFAULT_TOKEN_PATH), help="Projected SA token path")
  parser.add_argument("--ca-path", default=os.environ.get("KUBEAGENTS_BROKER_CA_PATH", DEFAULT_CA_PATH), help="Broker CA bundle path")
  parser.add_argument("--cert-path", default=os.environ.get("KUBEAGENTS_BROKER_CERT_PATH", DEFAULT_CERT_PATH), help="Client TLS cert path")
  parser.add_argument("--key-path", default=os.environ.get("KUBEAGENTS_BROKER_KEY_PATH", DEFAULT_KEY_PATH), help="Client TLS key path")
  parser.add_argument("--agent-identity", default=os.environ.get("KUBEAGENTS_AGENT_IDENTITY", ""), help="Explicit caller identity for idempotencyKey")
  parser.add_argument("--agent", default=os.environ.get("A2A_AGENT_NAME") or os.environ.get("KUBEAGENTS_AGENT_NAME", ""), help="Target agent name for NATS transport")
  parser.add_argument("--print-only", action="store_true", help="Print the canonical ActionEnvelope JSON without submitting")
  parsed = parser.parse_args(cleaned)
  if positional_op and not parsed.op:
    parsed.op = positional_op
  if positional_action_id and not parsed.action_id:
    parsed.action_id = positional_action_id
  return parsed


def _load_broker_env_defaults() -> None:
  for candidate in ("/etc/kage/broker-env.json", "/opt/data/.broker-env.json"):
    if os.path.isfile(candidate):
      try:
        with open(candidate, "r", encoding="utf-8") as f:
          data = json.load(f)
        if isinstance(data, dict):
          for k, v in data.items():
            if isinstance(k, str) and v is not None and not os.environ.get(k):
              os.environ[k] = str(v)
      except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
  _load_broker_env_defaults()
  args = _parse_args(argv if argv is not None else sys.argv[1:])

  if args.op in ("status", "list"):
    target_action_id = (args.action_id or args.name) if args.op == "status" else None
    rc, payload = inspect_broker_journal(
        namespace=args.namespace,
        action_id=target_action_id,
        limit=args.limit,
    )
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return rc

  agent_id = resolve_agent_identity(
      explicit_identity=args.agent_identity,
      token_path=args.token_path,
      namespace=args.namespace,
      agent_name=args.agent or None,
  )

  endpoint = args.endpoint.strip()
  token = _read_file_if_exists(args.token_path) if endpoint else ""
  ssl_ctx = _build_ssl_context(args.ca_path, args.cert_path, args.key_path) if endpoint.startswith("https://") else None

  if endpoint and not args.print_only:
    nonce = fetch_nonce_http(endpoint, token, ssl_ctx=ssl_ctx)
  else:
    nonce = os.urandom(16).hex()

  if args.file:
    raw = sys.stdin.read() if args.file == "-" else open(args.file, "r", encoding="utf-8").read()
    loaded = json.loads(raw)
    ops = loaded.get("operations", [])
    dry_run = bool(loaded.get("dryRun", False)) or args.dry_run
    req_approval = bool(loaded.get("requireApproval", False)) or args.require_approval
    intent = args.intent or loaded.get("intent") or "execute proposed cluster mutation"
    rationale = args.rationale or loaded.get("rationale")
    max_objs = args.max_objects if args.max_objects is not None else loaded.get("maxObjects")
    deadline_secs = args.deadline_seconds if args.deadline_seconds is not None else loaded.get("deadlineSeconds")
    envelope = build_envelope(
        operations=ops,
        intent=intent,
        agent_identity=agent_id,
        nonce=loaded.get("nonce") if args.print_only and loaded.get("nonce") else nonce,
        issued_at=loaded.get("issuedAt") if args.print_only and loaded.get("issuedAt") else None,
        rationale=rationale,
        dry_run=dry_run,
        require_approval=req_approval,
        max_objects=max_objs,
        deadline_seconds=deadline_secs,
        requester=loaded.get("requester"),
        trigger=loaded.get("trigger"),
        trace=loaded.get("trace"),
    )
  elif args.op:
    if not args.kind:
      print("Error: --kind is required when --op is specified", file=sys.stderr)
      return 2
    op_dict = _build_operation_from_args(args)
    intent = args.intent or f"{args.op} {args.kind}/{args.name or '<name>'} in namespace {args.namespace}"
    envelope = build_envelope(
        operations=[op_dict],
        intent=intent,
        agent_identity=agent_id,
        nonce=nonce,
        rationale=args.rationale,
        dry_run=args.dry_run,
        require_approval=args.require_approval,
        max_objects=args.max_objects,
        deadline_seconds=args.deadline_seconds,
    )
  else:
    print("Error: either --file or --op must be specified", file=sys.stderr)
    return 2

  if args.print_only:
    sys.stdout.write(jcs_canonicalize(envelope).decode("utf-8") + "\n")
    return 0

  if endpoint:
    status, result = submit_envelope_http(endpoint, envelope, token, ssl_ctx=ssl_ctx)
  elif os.environ.get("A2A_NATS_URL") or os.environ.get("NATS_URL"):
    status, result = submit_envelope_nats(envelope, namespace=args.namespace, agent_name=args.agent or None)
  else:
    print(
        "Error: neither KUBEAGENTS_BROKER_ENDPOINT nor A2A_NATS_URL/NATS_URL is configured",
        file=sys.stderr,
    )
    return 1

  sys.stdout.write(json.dumps(result) + "\n")
  decision = result.get("decision", "")
  if status >= 400 or decision in ("refused", "rejected"):
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
