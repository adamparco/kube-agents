# Spike: wiring the controller to Scion's launch primitive (P1-T12)

**Status:** spike complete — seam landed, native remains the v1 default with a mandatory fallback.
**Spec:** [08 §2](../../design/08-agent-runtime-and-identity.md) items 1–2, §6 (approach & non-goals),
[07](../../design/07-implementation-roadmap.md) Phase 1. **Risk:** low.
**Code:** `k8s-operator/internal/controller/pod_launcher.go` (+ `pod_launcher_test.go`); call site
`platformagent_controller.go` `reconcileDeployment`.

## Question

08 §2 says the controller reconciles **one isolated pod per agent** using the exact per-pod,
hardened-runtime shape **verified in Scion** (`serviceAccountName` for Workload Identity, `namespace`,
`runtimeClassName`, hardened pod-security context). v1 builds that pod **natively**; the roadmap calls
for a Phase-1 spike to see whether the controller can instead **call Scion's launch primitive** for pod
construction — **with a fallback to the native build if Scion's K8s mode is absent**.

## What Scion provides (and doesn't, yet)

- Scion (`GoogleCloudPlatform/scion`) verifies the per-pod-identity model kube-agents reuses:
  `pkg/api/types.go` describes the launch/pod contract; `pkg/runtime/k8s_runtime.go` is its Kubernetes
  runtime.
- **Its K8s runtime is early** (08 §2 "traded away", §6 non-goals): it cannot yet supervise long-lived
  agent pods. Deploying Scion as a standalone per-cluster orchestrator is an explicit **non-goal** for
  v1 — the kube-agents controller owns lifecycle, cardinality, relaunch, and label-stamping.
- Consequence: we want Scion's **launch primitive** (pod construction) as a callable, **not** Scion's
  orchestrator. Since the primitive's K8s mode isn't dependable yet, the integration must degrade to the
  native builder automatically.

## The seam

`pod_launcher.go` introduces a small, framework-neutral seam:

- `LaunchSpec` — the minimal per-pod contract (name, namespace, `serviceAccountName`,
  `runtimeClassName`, hardened-posture assertions) mirroring the fields Scion's launch primitive
  verifies. `launchSpecFor(agent)` extracts it from the `Agent` CR — one source of truth both launchers
  read.
- `PodLauncher` interface — `BuildDeployment(...) *appsv1.Deployment`.
  - `nativePodLauncher` — wraps the existing verified `buildDeployment`; **the fallback and v1 default**.
    Output is byte-identical to today's operator (golden test unchanged).
  - `scionPodLauncher` — the spike target. It derives `LaunchSpec` (proving the extraction path), then,
    until Scion K8s mode is dependable, delegates to the native fallback. A real integration would
    submit the `LaunchSpec` to Scion's primitive and reconcile the returned pod.
- `selectPodLauncher(log)` — returns Scion **only** when the gate is on **and** the probe reports Scion
  K8s mode available; otherwise native. Never nil, so pod construction always has a working path.

Selection inputs:

- **Feature gate** `KUBEAGENTS_SCION_LAUNCH` (env, default off) — opt-in only.
- **Availability probe** `scionK8sModeAvailable()` — returns `false` in v1 (Scion K8s mode absent). This
  is the single place a future Scion sidecar/endpoint readiness check goes.

The reconcile path (`reconcileDeployment`) now calls `selectPodLauncher(logf.FromContext(ctx))` and
builds through it. With the gate off (default) this is the native builder — **no behavioural change**.

## Result

- **Wired with a mandatory native fallback** — the T12 acceptance signal. Verified by
  `pod_launcher_test.go`:
  - default (gate unset) → native launcher;
  - gate **on** but Scion K8s mode absent → **falls back to native** (the load-bearing property);
  - `scionPodLauncher` output is at **parity** with native (identity, placement, sandbox, hardened
    securityContext all equal) — wiring the seam never silently changes the deployed pod.
- `go build/vet/test ./...` green; golden fixture unchanged.

## What a real integration needs next (deferred, out of Phase 1 scope)

1. A dependency/transport to Scion's launch primitive (vendored Go package, or a Scion sidecar exposing
   the primitive) — kept out of v1 to avoid taking on Scion's early K8s runtime.
2. A translation from `LaunchSpec` (+ the container/volume graph the native builder still owns) into a
   Scion launch request, and back into a reconciled pod.
3. A real `scionK8sModeAvailable()` readiness probe.
4. Parity/e2e coverage asserting a Scion-launched pod matches the native pod's identity and posture on a
   live cluster.

Until then, native is the default and the fallback, and the controller — not Scion — owns lifecycle
(08 §6).
