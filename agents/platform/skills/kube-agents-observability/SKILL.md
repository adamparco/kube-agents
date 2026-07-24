---
name: kube-agents-observability
description: Audit, monitor, and debug the logging, tracing, metrics, and API/dashboard observability of the Platform Agent.
---

# Task

Audit, verify, and troubleshoot the logging, metrics, and distributed tracing observability of the Platform Agent.

> [!TIP]
> The provided Python scripts in the `scripts/` subdirectory are parameterized reference implementations. When troubleshooting, you can run them directly, customize their parameters, or write custom just-in-time scripts/commands to query more specific metrics, endpoints, or time ranges as required by the task context.

# Backend selection (provider-neutral seam)

Google Cloud is the **default** backend, but the endpoints are **not hardcoded** — they resolve from
the environment via [`scripts/obs_backend.py`](scripts/obs_backend.py) (Phase 7, 01 §6). This keeps
kube-agents from being wired to GCP while leaving existing GKE installs unchanged when the knobs are
unset.

- **`KUBEAGENTS_OBS_BACKEND`** — backend profile. `gcp` (**default**) resolves metrics/trace/logging to
  Cloud Monitoring / Cloud Trace / Cloud Logging (the historical behavior; unset ⇒ no change). Any
  other value is a non-GCP backend and **requires** the explicit base URLs below.
- **`OBS_MONITORING_BASE_URL` / `OBS_TRACE_BASE_URL` / `OBS_LOGGING_BASE_URL`** — explicit per-signal
  base-URL overrides. When set they win (they work even with the `gcp` profile), so a single signal can
  point at, e.g., an in-cluster Prometheus (`http://prometheus-server.monitoring.svc.cluster.local`)
  without switching the whole profile.
- **`OTEL_EXPORTER_OTLP_ENDPOINT`** — the OTLP _export_ endpoint the agent ships spans to. Baked to the
  GKE managed-OTel collector by default; set this standard OTEL env to target any OTLP collector on a
  non-GKE cluster (resolved by `docker-entrypoint.sh`).

**Non-GCP path (documented):** point the base URLs at your metrics/trace/logging stack (Prometheus /
Tempo / Loki or a vendor OTLP endpoint). Query **translation** for a non-GCP backend (Cloud Monitoring
MQL → PromQL, Cloud Trace → Tempo, GCP-managed-Prometheus metric names → native names) is
backend-specific and is deferred (D3) — only the _endpoint_ is provider-neutral today.

# Workflow

## Logging

### 1. Audit Agent Main Logs

- Verify that the main agent container is writing logs to `/opt/data/logs/*.log`.
- View the internal agent log files directly:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n kubeagents-system -- tail -n 100 /opt/data/logs/agent.log
  ```

### 2. Inspect Sidecar Log Aggregator (Fluent-bit)

- Verify the `fluent-bit` sidecar container tails the log directory and streams to standard output:
  ```bash
  kubectl logs <pod-name> -c fluent-bit -n kubeagents-system --tail=100
  ```
- Retrieve and verify the configuration of the Fluent-bit sidecar:
  ```bash
  kubectl get configmap <agent-name>-fluent-bit-config -n kubeagents-system -o yaml
  ```
- Ensure the shared `/opt/data` volume is mounted to both the agent and Fluent-bit containers:
  ```bash
  kubectl get pod <pod-name> -n kubeagents-system -o jsonpath='{.spec.containers[*].volumeMounts}'
  ```

### 3. Identify Active Chat Users (Auditing Interactions)

To determine which users have interacted with the system via Google Chat in the last 24 hours (or a custom window):

- Run the packaged Python helper script to automatically query and parse the GKE container logs from Google Cloud Logging:

  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/get_chat_users.py --project-id <PROJECT_ID> [--hours <HOURS>]

  ```

- Alternatively, search Cloud Logging manually (via console or gcloud CLI) for the custom GChat event format emitted by the hermes session store:
  ```bash
  gcloud logging read 'resource.type="k8s_container" "Logging incoming GChat event"' --project=<PROJECT_ID> --limit=1000 --format="json"
  ```
  Look for log lines containing the format: `Logging incoming GChat event: User=<email>, Session=<session_id>`.

## Metrics

> [!NOTE]
> LLM token and operational metrics are conditional on the LLM proxy or inference server used.
>
> - **LiteLLM**: The scripts below query custom LiteLLM metrics. See the [LiteLLM Prometheus Documentation](https://docs.litellm.ai/docs/proxy/prometheus) for a complete list of metrics.

> - **vLLM**: Exposes different Prometheus metrics (e.g., `vllm:num_requests_waiting`). See the [vLLM Metrics Documentation](https://docs.vllm.ai/en/stable/usage/metrics/) for details.
> - **Other providers**: Query names will vary based on the specific provider's exporter.

### 1. Verify Cloud Monitoring & Prometheus State

- Check that Google Cloud Managed Service for Prometheus (GMP) is running in the cluster:
  ```bash
  kubectl get pods -n gmp-system
  ```
- Verify the agent deployment has correct annotations for Prometheus scraping:
  ```bash
  kubectl get deployment <agent-deployment-name> -n kubeagents-system -o yaml
  ```

### 2. Inspect CPU and Memory Metrics

- Query Kubernetes metrics API to verify resource usage of the agent pods:
  ```bash
  kubectl top pod -l app=<agent-name> -n kubeagents-system
  ```

### 3. Check Token Usage (Last 24h)

- Run the python script to fetch LiteLLM total token metrics from Cloud Monitoring:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/check_token_usage.py --project-id <project-id>
  ```

### 4. List LiteLLM Metric Descriptors

- Run the python script to list all available metric descriptors for LiteLLM:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/get_metric_descriptors.py --project-id <project-id>
  ```

## Traces

> [!NOTE]
> The system relies on GKE Managed OpenTelemetry for distributed tracing.
>
> - **Harness Agents**: Emit traces natively via the `hermes_otel` plugin.
> - **LiteLLM**: Emits trace spans via its OTLP callback system.
> - **Visualization**: Exported traces are stored in Google Cloud Trace and can be searched/analyzed in the **Trace Explorer** console.

### 1. Verify OpenTelemetry (OTel) Configuration

- Ensure the `hermes_otel` plugin is enabled in `/opt/data/config.yaml` or `/opt/defaults/config.yaml`.
- Verify the exporter backend is configured to use the GKE managed collector endpoint: `http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces`

### 2. Diagnose Trace Collector Connectivity

- Test network reachability from the agent container to the OpenTelemetry collector:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n kubeagents-system -- curl -i -s -o /dev/null -w "%{http_code}" -X POST http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces
  ```
- Check the agent logs for OTLP connection warnings or trace export failures:
  ```bash
  kubectl logs <pod-name> -c <agent-container-name> -n kubeagents-system --tail=500 | grep -iE "(otel|trace|exporter|export)"
  ```

### 3. Fetch and Analyze Traces (Locating Performance Bottlenecks)

To list recent traces or analyze span latency distributions to locate performance bottlenecks (such as slow tool executions or model calls):

- Run the trace latency analyzer script:

  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/analyze_trace_latency.py --project-id <project-id> [--hours <hours>] [--limit <limit>]
  ```

  **Example Output:**

  ```text
  Retrieving the last 3 traces...
  ======================================================================
  Trace ID: 0006344377aac15d1baede1a41e88a2c
  Total Duration: 0.647 seconds | Total Spans: 3
  Breakdown of spans:
    - POST /v1/chat/completions                          :  0.646s (99.9%)
    - chat model-default                                 :  0.627s (97.0%)
    - auth /v1/chat/completions                          :  0.001s ( 0.1%)
  ```

- Alternatively, run the raw trace list script:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/fetch_traces.py --project-id <project-id> --hours 24
  ```

## Agent Status and Health

### 1. Diagnose Agent API and Dashboard Exposure

- Verify pod running status and details:
  ```bash
  kubectl get pods -n kubeagents-system -l app=<agent-name> -o wide
  ```
- Inspect Service configurations for the API port (`8642`) and Dashboard port (`9119`):
  ```bash
  kubectl get service platform-agent -n kubeagents-system -o yaml
  ```
- Forward agent ports locally to test web UI or API access:
  ```bash
  kubectl port-forward svc/<agent-service-name> -n kubeagents-system 9119:9119
  ```

### 2. Inspect Persistent Internal State & Memory

- Inspect the agent's active memory files and settings:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n kubeagents-system -- ls -la /opt/data/memory/
  kubectl exec <pod-name> -c <agent-container-name> -n kubeagents-system -- cat /opt/data/memory/heartbeat-state.json
  ```
