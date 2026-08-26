/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

const (
	// defaultPlatformAgentImage is the fallback image for the platform tier when an Agent CR omits
	// spec.deployment.image. Pinned to an immutable version tag (not :latest); shipped CRs
	// pin explicitly and production should repin to a digest (platform-agent@sha256:...).
	defaultPlatformAgentImage = "ghcr.io/gke-labs/kube-agents/platform-agent:v0.1.0"

	// defaultClusterAdminAgentImage is the fallback image for the cluster-admin tier. Each tier
	// ships its own persona baked into its own image (07 §2(a)); same pinning rules as above.
	defaultClusterAdminAgentImage = "ghcr.io/gke-labs/kube-agents/cluster-admin-agent:v0.1.0"

	// defaultDeveloperTeamAgentImage is the fallback image for the developer-team tier (the
	// namespace-scoped leaf tier; 1 per namespace). Ships a read-only, namespace-scoped persona
	// with no propose-child cascade; same pinning rules as above.
	defaultDeveloperTeamAgentImage = "ghcr.io/gke-labs/kube-agents/developer-team-agent:v0.1.0"

	// managedOTelEndpoint is the OTLP/HTTP endpoint of the GKE Managed OpenTelemetry
	// collector. The same endpoint is already used by the LiteLLM integration, so agent
	// traces and LLM-call telemetry land in the same place (Cloud Trace/Logging).
	managedOTelEndpoint = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318"
)

// otelTelemetryEnvVars returns the OpenTelemetry configuration for an agent container: the
// service name, the GKE Managed OpenTelemetry collector endpoint, and resource attributes
// carrying the agent's identity. These defaults can be overridden per-agent via Deployment.Env
// (see mergeEnvVars).
func otelTelemetryEnvVars(agentType, name, namespace string) []corev1.EnvVar {
	return []corev1.EnvVar{
		{
			Name:  "OTEL_SERVICE_NAME",
			Value: name + "-gateway",
		},
		{
			Name:  "OTEL_EXPORTER_OTLP_ENDPOINT",
			Value: managedOTelEndpoint,
		},
		{
			Name:  "OTEL_EXPORTER_OTLP_PROTOCOL",
			Value: "http/protobuf",
		},
		{
			Name: "OTEL_RESOURCE_ATTRIBUTES",
			Value: fmt.Sprintf(
				"service.namespace=%s,k8s.namespace.name=%s,kubeagents.agent_type=%s,kubeagents.agent_name=%s",
				namespace, namespace, agentType, name,
			),
		},
	}
}

// telemetryEnvNames are the variables a container needs to report to the same OTel backend as the
// agent. Selected by name out of the already-merged env rather than recomputed, so a per-agent
// override in spec.deployment.env reaches every container that reports telemetry, not just the
// first one. See selectEnvVars for why they must not diverge.
var telemetryEnvNames = []string{
	"OTEL_SERVICE_NAME",
	"OTEL_EXPORTER_OTLP_ENDPOINT",
	"OTEL_EXPORTER_OTLP_PROTOCOL",
	"OTEL_RESOURCE_ATTRIBUTES",
}

// selectEnvVars returns the entries of src whose names appear in want, in src order.
//
// Sidecars that share the agent's PVC must be given the SAME telemetry values, not their own
// copy: docker-entrypoint.sh step 4 writes $OTEL_SERVICE_NAME into
// $PLATFORM_AGENT_HOME/plugins/hermes_otel/config.yaml, and that path is on the shared volume.
// Two containers computing the endpoint independently would race on one file and the winner
// would depend on start order. Selecting from one list makes the race idempotent.
func selectEnvVars(src []corev1.EnvVar, want []string) []corev1.EnvVar {
	wanted := make(map[string]struct{}, len(want))
	for _, n := range want {
		wanted[n] = struct{}{}
	}
	out := make([]corev1.EnvVar, 0, len(want))
	for _, e := range src {
		if _, ok := wanted[e.Name]; ok {
			out = append(out, e)
		}
	}
	return out
}

// defaultImageForTier returns the baked default image for a tier, used when an Agent CR omits
// spec.deployment.image (07 §2(a)). Each tier ships its own read-only persona baked into its own
// image; spec.deployment.image still overrides. An unknown/empty tier falls back to the platform
// image (EffectiveTier already maps empty→platform, so the default arm is defense-in-depth).
func defaultImageForTier(tier agentv1alpha1.AgentTier) string {
	switch tier {
	case agentv1alpha1.TierClusterAdmin:
		return defaultClusterAdminAgentImage
	case agentv1alpha1.TierDeveloperTeam:
		return defaultDeveloperTeamAgentImage
	default:
		return defaultPlatformAgentImage
	}
}

// resolveAgentImage determines the full image reference using the optional deployment spec and a fallback default.
func resolveAgentImage(deployment *agentv1alpha1.DeploymentSpec, defaultImage string) string {
	image := defaultImage
	if deployment != nil && deployment.Image != "" {
		image = deployment.Image
		hasTagOrDigest := false
		lastSlash := strings.LastIndex(image, "/")
		refPart := image
		if lastSlash != -1 {
			refPart = image[lastSlash+1:]
		}
		if strings.Contains(refPart, ":") || strings.Contains(refPart, "@") {
			hasTagOrDigest = true
		}

		if !hasTagOrDigest {
			tag := "latest"
			if deployment.Tag != nil && *deployment.Tag != "" {
				tag = *deployment.Tag
			}
			image = fmt.Sprintf("%s:%s", image, tag)
		}
	}
	return image
}

// mergeEnvVars merges custom env vars into defaults. Custom env vars override defaults with the same name.
func mergeEnvVars(defaults []corev1.EnvVar, custom []corev1.EnvVar) []corev1.EnvVar {
	if len(custom) == 0 {
		return defaults
	}
	if len(defaults) == 0 {
		return custom
	}

	customMap := make(map[string]corev1.EnvVar, len(custom))
	for _, env := range custom {
		customMap[env.Name] = env
	}

	merged := make([]corev1.EnvVar, 0, len(defaults)+len(custom))
	for _, env := range defaults {
		if customEnv, exists := customMap[env.Name]; exists {
			merged = append(merged, customEnv)
			delete(customMap, env.Name)
		} else {
			merged = append(merged, env)
		}
	}

	// Append remaining custom env vars in their original order
	for _, env := range custom {
		if customEnv, exists := customMap[env.Name]; exists {
			merged = append(merged, customEnv)
			delete(customMap, env.Name)
		}
	}

	return merged
}

// mergeAnnotations merges custom annotations into defaults. Custom annotations override defaults with the same key.
// mergeLabels is mergeAnnotations under the name that reads correctly at a label call site. It is a
// delegation rather than a copy so the two cannot drift: precedence is the same (custom wins on a
// key collision), and so is the empty-in-empty-out behaviour.
func mergeLabels(defaults map[string]string, custom map[string]string) map[string]string {
	return mergeAnnotations(defaults, custom)
}

func mergeAnnotations(defaults map[string]string, custom map[string]string) map[string]string {
	if len(defaults) == 0 && len(custom) == 0 {
		return nil
	}
	merged := make(map[string]string, len(defaults)+len(custom))
	for k, v := range defaults {
		merged[k] = v
	}
	for k, v := range custom {
		merged[k] = v
	}
	return merged
}

// resolveDeploymentReplicasAndStrategy determines the replica count and deployment strategy
// based on ScaleToZero settings in the DeploymentSpec.
func resolveDeploymentReplicasAndStrategy(deployment *agentv1alpha1.DeploymentSpec) (int32, appsv1.DeploymentStrategy) {
	replicas := int32(1)
	strategy := appsv1.DeploymentStrategy{
		Type: appsv1.RecreateDeploymentStrategyType,
	}

	if deployment != nil {
		if deployment.ScaleToZero != nil && *deployment.ScaleToZero {
			replicas = int32(0)
		}
	}
	return replicas, strategy
}

// NOTE: the ReconcileServiceAccount helper was removed in P1-T5. The controller no longer mints or
// annotates the agent KSA at runtime; the ServiceAccount (with its Workload Identity annotation) is
// pre-created via GitOps (examples/gitops-repo/policy/rbac-overlay/) and only referenced by name.

// joinNonBlank renders an allowlist for the pod environment, dropping entries
// that are empty or pure whitespace.
//
// A blank entry names no principal, so carrying it into the pod can only make
// the in-pod allowlist look non-empty when it is not. Admission already rejects
// an all-blank list (06 §1.2 V-7); this keeps a partially-blank one from
// smuggling a meaningless member past the authorizer. There is deliberately no
// "allow all" branch here — an allowlist that names nobody is a configuration
// error, and the permissive *_ALLOW_ALL_USERS backstop was removed in P8-T1.
func joinNonBlank(users []string) string {
	kept := make([]string, 0, len(users))
	for _, u := range users {
		if trimmed := strings.TrimSpace(u); trimmed != "" {
			kept = append(kept, trimmed)
		}
	}
	return strings.Join(kept, ",")
}

// defaultSecretRef returns ref if provided, otherwise defaults to secretName with defaultKey.
func defaultSecretRef(ref *corev1.SecretKeySelector, secretName, defaultKey string) *corev1.SecretKeySelector {
	if ref != nil {
		return ref
	}
	return &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
		Key:                  defaultKey,
		Optional:             ptr.To(true),
	}
}
