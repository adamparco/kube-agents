#!/bin/sh
set -e

export TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
export HERMES_HOME="$TARGET_DIR"
export INSTALL_DIR="/opt/hermes"

# Pre-export AGENT_BROWSER_EXECUTABLE_PATH before running stage2-hook.sh.
# Why: Upstream stage2-hook.sh scans for Playwright's Chromium binary and
# attempts to export it to s6-overlay by creating /run/s6/container_environment/.
# In unprivileged Kubernetes Pods (RunAsNonRoot: true), /run is read-only or
# root-owned, so stage2-hook.sh crashes on `mkdir -p /run/s6/` with Permission denied.
# By pre-exporting AGENT_BROWSER_EXECUTABLE_PATH here, stage2-hook.sh detects
# [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] is false and cleanly skips writing to /run/s6/.
if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/hermes/.playwright" ]; then
    export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/hermes/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
fi

# 1. Execute upstream container initialization natively (inherits 100% of upstream updates)
if [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    /opt/hermes/docker/stage2-hook.sh
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 3. Enable OpenTelemetry plugin in active config.yaml (if writable)
if [ -f "$TARGET_DIR/config.yaml" ] && [ -w "$TARGET_DIR/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; enabled = c.setdefault('plugins', {}).setdefault('enabled', []); 'hermes_otel' not in enabled and enabled.append('hermes_otel'); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/config.yaml" 2>/dev/null || true
fi

# 4. Inject dynamic OpenTelemetry service name (if writable)
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true
fi

# 4b. Resolve the OTLP exporter endpoint from the environment (provider-neutral observability seam;
#     Phase 7 P7-T3, 01 §6, D3). The image BAKES the GKE managed-OTel collector as the default (see
#     deploy/docker/Dockerfile step 5.5), so an UNSET environment leaves that default byte-for-byte
#     (no regression on GKE). Set the standard OTEL_EXPORTER_OTLP_ENDPOINT to point the exporter at any
#     OTLP-compatible collector (a Prometheus/Tempo stack, a vendor OTLP endpoint, ...) on a non-GKE
#     target — only then do we rewrite the backend endpoint.
DEFAULT_OTLP_ENDPOINT="http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"
OTEL_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-$DEFAULT_OTLP_ENDPOINT}"
if [ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ] \
    && [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] \
    && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    echo "OTLP exporter endpoint overridden via OTEL_EXPORTER_OTLP_ENDPOINT: $OTEL_ENDPOINT"
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {}; b = c.setdefault('backends', [{}]); b[0].update({'name': b[0].get('name', 'otlp'), 'type': 'otlp', 'endpoint': sys.argv[2]}); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" "$OTEL_ENDPOINT" 2>/dev/null || true
fi

# 5. Start background microservices (FastAPI proxy)
mkdir -p "$TARGET_DIR/logs"
if [ -f "$TARGET_DIR/scripts/session_kv_server.py" ]; then
    echo "Starting Session KV server on 127.0.0.1:8699..."
    # Bind loopback only: the inject seam is a same-pod interface (sidecar/relay -> agent).
    # This closes the cross-pod network path unconditionally; inject_auth adds the bearer
    # backstop against a compromised co-container. See Phase 4 P4-T1 (S1).
    "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 127.0.0.1 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi

# 5.5. Initialize default GKE context for the container to the host cluster
if [ -n "$GKE_CLUSTER_NAME" ] && [ -n "$GKE_LOCATION" ]; then
    echo "Configuring default kubectl context to host cluster: $GKE_CLUSTER_NAME ($GKE_LOCATION)..."
    gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --location="$GKE_LOCATION" ${GOOGLE_CHAT_PROJECT_ID:+--project="$GOOGLE_CHAT_PROJECT_ID"} >/dev/null 2>&1 || true
fi

# 6. Execute primary process
exec "$@"
