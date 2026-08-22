#!/usr/bin/env python3
"""The read-only evidence surface the self-improvement agent queries.

Every source in docs/designs/self-improvement.md sec. 3 reaches the investigation
through one of the subcommands here, and nothing else does. That is the point of
the file: an agent handed `gcloud` and told to be careful is relying on its own
good behaviour, while an agent whose only tools are `logs`, `traces`, `metrics`
and `k8s get` cannot mutate anything even if it decides it should.

Two identities, neither of which can write:

* **Google Cloud** -- the runner's Workload Identity service account, holding
  `roles/logging.viewer`, `roles/cloudtrace.user` and `roles/monitoring.viewer`
  and no GKE roles at all, so `container.clusters.get` fails for every cluster in
  the project including this one. The access token comes from the GKE metadata
  server directly rather than through a client library: the three REST APIs used
  here are a GET and two POSTs, and google-cloud-logging is not in the image.

* **Kubernetes** -- the pod's own service account, bound to `view` on the release
  namespace by a RoleBinding. `view` excludes Secrets; the Role adds nothing that
  writes, and pods/exec, pods/attach and pods/portforward are excluded on top of
  it (sec. 3.3).

The loop's own records are filtered out of every log and trace query by service
name, because a loop that finds itself slow and files a pull request about
itself is a closed circuit (sec. 10). `--include-self` exists for the one case
that wants them -- debugging the loop -- and says so in its help.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)

#: The OTEL service name the runner stamps on its own telemetry, and the
#: Kubernetes object-name prefix its own pods carry. Both are excluded from
#: queries by default.
SELF_SERVICE_NAME = "kube-agents-selfimprove"

_TOKEN_CACHE: Dict[str, Any] = {}


def _fail(message: str) -> None:
    print("error: %s" % message, file=sys.stderr)
    raise SystemExit(2)


def access_token() -> str:
    """A Google access token for the runner's Workload Identity service account.

    Straight from the metadata server. google.auth would also work and is in the
    image, but it is a heavier import for a value this file needs once, and the
    metadata endpoint is the same thing the library ends up calling under
    Workload Identity.
    """
    if "token" in _TOKEN_CACHE:
        return _TOKEN_CACHE["token"]
    request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        _fail(
            "no Google access token from the metadata server (%s). Workload Identity is "
            "how this pod authenticates; check the KSA's iam.gke.io/gcp-service-account "
            "annotation and the GSA's workloadIdentityUser binding." % exc
        )
    token = payload.get("access_token")
    if not token:
        _fail("the metadata server returned no access_token")
    _TOKEN_CACHE["token"] = token
    return token


def _google_api(url: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer %s" % access_token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        _fail("%s %s: %s" % (exc.code, exc.reason, detail))
    except urllib.error.URLError as exc:
        _fail("could not reach %s: %s" % (url, exc))
    return {}


def _project() -> str:
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GKE_PROJECT_ID")
    if not project:
        _fail("GCP_PROJECT_ID is not set; the chart sets it on the runner container")
    return project


def _namespace() -> str:
    return os.environ.get("KUBE_DEFAULT_NAMESPACE") or os.environ.get("POD_NAMESPACE") or "kubeagents-system"


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def cmd_logs(args: argparse.Namespace) -> int:
    """Cloud Logging entries for the install's namespace.

    The agent writes to files on its data volume rather than to stdout, so
    `kubectl logs` on the agent container shows almost nothing. The fluent-bit
    sidecar the operator adds to every agent pod tails /opt/data/logs/*.log,
    stamps each record `log_source: agent-file` and prints it as JSON to stdout,
    from where GKE ships it here. That is the whole log-access story for a
    runner that never mounts the data volume: query Cloud Logging.
    """
    # Shared with logs-count deliberately. The count is the number the gate
    # reads and the number the pull request quotes, so a reader who runs `logs`
    # to see the entries behind it must be looking at the same set. Two copies
    # of this clause list would let one grow a filter the other does not have,
    # and the symptom -- a count that does not match the sample -- would show up
    # in a pull request rather than here.
    body = {
        "resourceNames": ["projects/%s" % _project()],
        "filter": _logs_filter(args),
        "orderBy": "timestamp desc",
        "pageSize": min(args.limit, 1000),
    }
    payload = _google_api("https://logging.googleapis.com/v2/entries:list", body)
    entries = payload.get("entries", [])[: args.limit]
    if args.raw:
        print(json.dumps(entries, indent=1))
        return 0
    for entry in entries:
        payload_text = (
            entry.get("textPayload")
            or json.dumps(entry.get("jsonPayload", {}), sort_keys=True)
            or json.dumps(entry.get("protoPayload", {}), sort_keys=True)
        )
        print(
            "%s [%s] %s/%s %s"
            % (
                entry.get("timestamp", "?"),
                entry.get("severity", "DEFAULT"),
                entry.get("resource", {}).get("labels", {}).get("pod_name", "?"),
                entry.get("resource", {}).get("labels", {}).get("container_name", "?"),
                payload_text[: args.width].replace("\n", "\\n"),
            )
        )
    if not entries:
        print("(no entries matched)")
    return 0


def cmd_logs_count(args: argparse.Namespace) -> int:
    """How many entries match, bucketed by the field the gate cares about.

    The occurrence count is the strongest sentence in a pull request, and it is
    also the number `minOccurrencesPerDay` reads, so it has to come from a
    counting query rather than from `len()` of a page of results that stopped at
    the limit.
    """
    buckets: Dict[str, int] = {}
    total = 0
    body = {
        "resourceNames": ["projects/%s" % _project()],
        "filter": _logs_filter(args),
        "orderBy": "timestamp desc",
        "pageSize": 1000,
    }
    page_token = None
    pages = 0
    while pages < args.max_pages:
        if page_token:
            body["pageToken"] = page_token
        payload = _google_api("https://logging.googleapis.com/v2/entries:list", body)
        entries = payload.get("entries", [])
        total += len(entries)
        for entry in entries:
            key = "%s/%s" % (
                entry.get("resource", {}).get("labels", {}).get("container_name", "?"),
                entry.get("severity", "DEFAULT"),
            )
            buckets[key] = buckets.get(key, 0) + 1
        page_token = payload.get("nextPageToken")
        pages += 1
        if not page_token:
            break
    print(json.dumps({"total": total, "truncated": bool(page_token), "by": buckets}, indent=1))
    return 0


def _logs_filter(args: argparse.Namespace) -> str:
    clauses = [
        'resource.type="k8s_container"',
        'resource.labels.namespace_name="%s"' % _namespace(),
        'timestamp>="%s"' % _rfc3339_hours_ago(args.hours),
    ]
    if getattr(args, "severity", None):
        clauses.append('severity>="%s"' % args.severity.upper())
    if getattr(args, "container", None):
        clauses.append('resource.labels.container_name="%s"' % args.container)
    if getattr(args, "agent_files", False):
        clauses.append('jsonPayload.log_source="agent-file"')
    if getattr(args, "query", None):
        clauses.append("(%s)" % args.query)
    if not getattr(args, "include_self", False):
        clauses.append('NOT resource.labels.pod_name:"%s"' % SELF_SERVICE_NAME)
    return " AND ".join(clauses)


def _rfc3339_hours_ago(hours: float) -> str:
    import datetime as dt

    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------


def cmd_traces(args: argparse.Namespace) -> int:
    """Cloud Trace spans, which is where a latency finding has to come from.

    A log line records that a turn happened; a span tree records which tool call
    inside it consumed the wall clock. Signal 3 in sec. 4 is not answerable from
    timestamps in a log, which is why this subcommand exists rather than being
    folded into `logs`.
    """
    params = {
        "startTime": _rfc3339_hours_ago(args.hours),
        "pageSize": str(min(args.limit, 1000)),
        "view": "ROOTSPAN" if not args.full else "COMPLETE",
    }
    filters = []
    if args.span:
        filters.append("span:%s" % args.span)
    if not args.include_self:
        # Cloud Trace has no NOT operator, so the loop's own traces are dropped
        # after the fact rather than in the query. The page size is spent on
        # them either way; on an install where the loop dominates the trace
        # volume, pass --service to narrow it at the source instead.
        pass
    if args.service:
        filters.append("+root:%s" % args.service)
    if filters:
        params["filter"] = " ".join(filters)
    url = "https://cloudtrace.googleapis.com/v1/projects/%s/traces?%s" % (
        _project(),
        urllib.parse.urlencode(params),
    )
    payload = _google_api(url, method="GET")
    traces = payload.get("traces", [])
    rows = []
    for trace in traces:
        spans = trace.get("spans", [])
        if not spans:
            continue
        root = spans[0]
        name = root.get("name", "?")
        if not args.include_self and SELF_SERVICE_NAME in name:
            continue
        rows.append(
            {
                "traceId": trace.get("traceId"),
                "root": name,
                "start": root.get("startTime"),
                "end": root.get("endTime"),
                "spans": len(spans),
            }
        )
    print(json.dumps(rows, indent=1))
    if not rows:
        print("(no traces matched)", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def cmd_metrics(args: argparse.Namespace) -> int:
    """One Cloud Monitoring time series, for the numbers a span cannot give.

    Container restarts, memory working set, CPU throttling: fleet-level shapes
    that show a problem is systemic rather than one bad session.
    """
    params = {
        "filter": args.filter,
        "interval.startTime": _rfc3339_hours_ago(args.hours),
        "interval.endTime": _rfc3339_hours_ago(0),
        "view": "FULL",
        "pageSize": "200",
    }
    url = "https://monitoring.googleapis.com/v3/projects/%s/timeSeries?%s" % (
        _project(),
        urllib.parse.urlencode(params),
    )
    payload = _google_api(url, method="GET")
    series = payload.get("timeSeries", [])
    out = []
    for entry in series:
        points = entry.get("points", [])
        out.append(
            {
                "metric": entry.get("metric", {}),
                "resource": entry.get("resource", {}).get("labels", {}),
                "points": len(points),
                "latest": points[0].get("value") if points else None,
            }
        )
    print(json.dumps(out, indent=1))
    return 0


# --------------------------------------------------------------------------
# Kubernetes
# --------------------------------------------------------------------------


def _kube():
    from kubernetes import client, config as kube_config  # noqa: PLC0415

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover - only outside a pod
        kube_config.load_kube_config()
    return client


def cmd_k8s(args: argparse.Namespace) -> int:
    """Read the release namespace through the pod's own `view` binding.

    Deliberately not a kubectl passthrough. There is no kubectl in this image
    outside the credential-proxy shims, and adding a general "run this argv"
    door would make the read-only posture a matter of the argv policy rather
    than of the grant. These five reads are what sec. 3.3 says the loop needs.
    """
    client = _kube()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    custom = client.CustomObjectsApi()
    ns = _namespace()
    out: Any

    if args.what == "pods":
        out = [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "startTime": str(p.status.start_time),
                "containers": [
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restarts": cs.restart_count,
                        "image": cs.image,
                        "state": list((cs.state.to_dict() if cs.state else {}).keys()),
                        "lastTerminated": (
                            cs.last_state.terminated.to_dict() if cs.last_state and cs.last_state.terminated else None
                        ),
                    }
                    for cs in (p.status.container_statuses or [])
                ],
            }
            for p in core.list_namespaced_pod(ns).items
        ]
    elif args.what == "deployments":
        out = [
            {
                "name": d.metadata.name,
                "replicas": d.status.replicas,
                "ready": d.status.ready_replicas,
                "images": [c.image for c in d.spec.template.spec.containers],
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                    for c in (d.status.conditions or [])
                ],
            }
            for d in apps.list_namespaced_deployment(ns).items
        ]
    elif args.what == "events":
        out = [
            {
                "type": e.type,
                "reason": e.reason,
                "object": "%s/%s" % (e.involved_object.kind, e.involved_object.name),
                "count": e.count,
                "message": e.message,
                "last": str(e.last_timestamp),
            }
            for e in core.list_namespaced_event(ns).items
            if args.include_self or SELF_SERVICE_NAME not in (e.involved_object.name or "")
        ]
    elif args.what == "configmaps":
        # Names and keys only. The values can be large and can carry install
        # identifiers, and a finding needs to know a key exists far more often
        # than it needs its contents.
        out = [
            {"name": c.metadata.name, "keys": sorted((c.data or {}).keys())}
            for c in core.list_namespaced_config_map(ns).items
        ]
    elif args.what == "platformagents":
        out = custom.list_namespaced_custom_object(
            group="kubeagents.x-k8s.io", version="v1alpha1", namespace=ns, plural="platformagents"
        ).get("items", [])
    elif args.what == "agentplugins":
        out = custom.list_namespaced_custom_object(
            group="kubeagents.x-k8s.io", version="v1alpha1", namespace=ns, plural="agentplugins"
        ).get("items", [])
    else:  # pragma: no cover - argparse constrains this
        _fail("unknown subject %r" % args.what)
        return 2

    print(json.dumps(out, indent=1, default=str))
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selfimprove-evidence",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_log_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--hours", type=float, default=24, help="how far back to look (default 24)")
        p.add_argument("--severity", default="", help="minimum severity, e.g. ERROR or WARNING")
        p.add_argument("--container", default="", help="restrict to one container name")
        p.add_argument(
            "--agent-files",
            action="store_true",
            help="only records fluent-bit lifted off the agent's /opt/data/logs files",
        )
        p.add_argument("--query", default="", help="extra Cloud Logging filter, ANDed with the rest")
        p.add_argument(
            "--include-self",
            action="store_true",
            help="include the self-improvement loop's own records (for debugging the loop only)",
        )

    p_logs = sub.add_parser("logs", help="Cloud Logging entries for the install's namespace")
    add_log_args(p_logs)
    p_logs.add_argument("--limit", type=int, default=50)
    p_logs.add_argument("--width", type=int, default=400, help="truncate each payload to this many characters")
    p_logs.add_argument("--raw", action="store_true", help="print the raw API entries as JSON")
    p_logs.set_defaults(func=cmd_logs)

    p_count = sub.add_parser("logs-count", help="count matching log entries, bucketed by container and severity")
    add_log_args(p_count)
    p_count.add_argument("--max-pages", type=int, default=10, help="stop after this many 1000-entry pages")
    p_count.set_defaults(func=cmd_logs_count)

    p_traces = sub.add_parser("traces", help="Cloud Trace root spans")
    p_traces.add_argument("--hours", type=float, default=24)
    p_traces.add_argument("--limit", type=int, default=100)
    p_traces.add_argument("--span", default="", help="substring match on a span name")
    p_traces.add_argument("--service", default="", help="restrict to one root span name prefix")
    p_traces.add_argument("--full", action="store_true", help="COMPLETE view: every span, not just roots")
    p_traces.add_argument("--include-self", action="store_true")
    p_traces.set_defaults(func=cmd_traces)

    p_metrics = sub.add_parser("metrics", help="one Cloud Monitoring time series")
    p_metrics.add_argument(
        "--filter",
        required=True,
        help='a Monitoring filter, e.g. metric.type="kubernetes.io/container/restart_count"',
    )
    p_metrics.add_argument("--hours", type=float, default=24)
    p_metrics.set_defaults(func=cmd_metrics)

    p_k8s = sub.add_parser("k8s", help="read the release namespace through the pod's `view` binding")
    p_k8s.add_argument(
        "what",
        choices=["pods", "deployments", "events", "configmaps", "platformagents", "agentplugins"],
    )
    p_k8s.add_argument("--include-self", action="store_true")
    p_k8s.set_defaults(func=cmd_k8s)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
