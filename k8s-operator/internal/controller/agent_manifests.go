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
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"path"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
)

const defaultPlatformAgentSecrets = "platform-agent-secrets"
const sessionKVDBPath = "/var/lib/kube-agents/session/session_kv.db"

// buildConfigMap generates the ConfigMap manifest containing config.yaml
func buildConfigMap(agent *agentv1alpha1.Agent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-config",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"config.yaml": renderConfigYAML(agent),
		},
	}
}

// buildSettingsConfigMap generates the ConfigMap manifest containing SETTINGS.md
func buildSettingsConfigMap(agent *agentv1alpha1.Agent) *corev1.ConfigMap {
	gitRepo := ""
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		gitRepo = agent.Spec.Integration.GitHub.GitRepo
	}
	if gitRepo == "" {
		gitRepo = "None"
	}
	settingsContent := fmt.Sprintf("# GKE Scope Configuration\n- **Git Repo:** %s\n", gitRepo)
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-settings",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"SETTINGS.md": settingsContent,
		},
	}
}

// renderConfigYAML generates the YAML payload for the agent config
func renderConfigYAML(agent *agentv1alpha1.Agent) string {
	cwd := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		cwd = agent.Spec.Harness.Hermes.AgentHome
	}

	cfg := struct {
		Model struct {
			Default  string `json:"default"`
			Provider string `json:"provider"`
			Model    string `json:"model,omitempty"`
			BaseURL  string `json:"base_url,omitempty"`
			APIKey   string `json:"api_key,omitempty"`
		} `json:"model"`
		Terminal struct {
			Backend string `json:"backend"`
			Cwd     string `json:"cwd"`
		} `json:"terminal"`
		MCPServers       map[string]any      `json:"mcp_servers,omitempty"`
		PlatformToolsets map[string][]string `json:"platform_toolsets,omitempty"`
		Approvals        struct {
			CronMode string `json:"cron_mode,omitempty"`
		} `json:"approvals,omitempty"`
		Web struct {
			Backend string `json:"backend,omitempty"`
		} `json:"web,omitempty"`
		Memory struct {
			MemoryEnabled      bool   `json:"memory_enabled"`
			Provider           string `json:"provider"`
			UserProfileEnabled bool   `json:"user_profile_enabled"`
		} `json:"memory"`
		Platforms struct {
			GoogleChat struct {
				Enabled bool `json:"enabled"`
			} `json:"google_chat"`
			Slack struct {
				Enabled bool `json:"enabled"`
			} `json:"slack"`
		} `json:"platforms"`
		Plugins struct {
			Enabled []string `json:"enabled"`
		} `json:"plugins"`
		Display struct {
			Platforms map[string]map[string]any `json:"platforms,omitempty"`
		} `json:"display,omitempty"`
	}{}

	// Model & Terminal configuration
	cfg.Model.Provider = "custom"
	cfg.Model.Default = "model-default"
	cfg.Model.Model = "model-default"
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	cfg.Terminal.Backend = "local"
	cfg.Terminal.Cwd = cwd

	// MCP Servers & Toolsets configuration
	cfg.MCPServers = map[string]any{
		"platform_control": map[string]any{
			"command":         "/opt/hermes/.venv/bin/python3",
			"args":            []string{"/opt/data/scripts/platform_mcp_server.py"},
			"connect_timeout": 120,
			"timeout":         300,
			"env": map[string]string{
				"KUBERNETES_SERVICE_HOST":       "${KUBERNETES_SERVICE_HOST}",
				"KUBERNETES_SERVICE_PORT":       "${KUBERNETES_SERVICE_PORT}",
				"HERMES_HOME":                   "${HERMES_HOME}",
				"GOOGLE_CHAT_PROJECT_ID":        "${GOOGLE_CHAT_PROJECT_ID}",
				"GOOGLE_CHAT_SUBSCRIPTION_NAME": "${GOOGLE_CHAT_SUBSCRIPTION_NAME}",
				"API_SERVER_KEY":                "${API_SERVER_KEY}",
			},
		},
		// The `env:` block is load-bearing, not decoration. Hermes passes an MCP server ONLY what its
		// own config declares -- the gateway container holding API_SERVER_KEY does not put it in the
		// server's process. Declared nothing, `agent_common` read an empty key and every inter-agent
		// call died on its own fail-closed refusal, on a pod that was Ready and unit tests that were
		// green (V-CMP-006, dev/test_mcp_env_declared.py). SLACK_BOT_TOKEN is deliberately NOT here:
		// agent_common_server.load_slack_token() keys on `"SLACK_BOT_TOKEN" not in os.environ` and
		// falls back to reading the Secret directly, and a declared-but-empty value would defeat that
		// fallback rather than fix it. KUBERNETES_SERVICE_* is what makes that fallback's kubectl work.
		"agent_common": map[string]any{
			"command": "/opt/hermes/.venv/bin/python3",
			"args":    []string{"/opt/data/scripts/agent_common_server.py"},
			"env": map[string]string{
				"KUBERNETES_SERVICE_HOST": "${KUBERNETES_SERVICE_HOST}",
				"KUBERNETES_SERVICE_PORT": "${KUBERNETES_SERVICE_PORT}",
				"HERMES_HOME":             "${HERMES_HOME}",
				"SESSION_KV_DB_PATH":      "${SESSION_KV_DB_PATH}",
				"API_SERVER_KEY":          "${API_SERVER_KEY}",
			},
		},
		// Bridged with the pod's own Workload Identity token, not an OAuth flow. `mcp-remote`
		// authenticates by opening a browser and listening for a loopback redirect; a pod has
		// neither, so it hung on startup rather than failing, and a headless multi-tier install
		// could not come up (Phase 8 P8-T4). The timeouts are set here for the same reason the
		// bridge answers every request even when the remote is broken: an MCP server that never
		// answers is indistinguishable from one that is still starting.
		"developer_knowledge": map[string]any{
			"command":         "/opt/hermes/.venv/bin/python3",
			"args":            []string{"/opt/data/scripts/mcp_http_bridge.py", "https://developerknowledge.googleapis.com/mcp"},
			"connect_timeout": 30,
			"timeout":         120,
		},
		// NOTE: the remote `gke` MCP proxy (container.googleapis.com) is intentionally NOT wired here.
		// It exposes cluster-mutating tools (e.g. create_cluster), and a remote MCP's toolset cannot be
		// subset client-side, so it is dropped entirely (03 §4, 06 §9). This render is
		// runtime-authoritative (mounted over /opt/data/config.yaml), so dropping it here is what
		// actually denies the pod an in-process write path.
		//
		// That is not the same as "the agent is read-only". The agent PROCESS holds the reader identity
		// and can mutate nothing itself; mutation LEAVES the pod as an Action Envelope submitted to the
		// Action Broker, which classifies it, executes it under its own actor identity and journals it
		// with an undo handle (06 §2.2.1, §4.1). What must never appear in this config is a tool that
		// mutates from INSIDE the pod: such a call bypasses that seam, so it is unclassified,
		// unjournaled, and cannot be undone.
		//
		// developer_knowledge above is a read API and stays.
	}
	cfg.PlatformToolsets = map[string][]string{
		// mcp-gke is intentionally absent: it is the toolset for the dropped cluster-mutating `gke`
		// remote MCP server (see NOTE above). The pod keeps no in-process write path (03 §4, 06 §9).
		"cli":        {"hermes-cli", "mcp-agent_common", "mcp-platform_control", "mcp-developer_knowledge"},
		"api_server": {"hermes-api-server", "mcp-agent_common", "mcp-platform_control", "mcp-developer_knowledge"},
	}

	// Execution & Display UX configuration
	cfg.Approvals.CronMode = "approve"
	cfg.Web.Backend = "ddgs"
	// Enable incident_context plugin by default to parse and rewrite GChat/Slack threaded incident replies
	cfg.Plugins.Enabled = []string{"hermes_otel", "session_store", "session_otel_bridge", "tool_call_audit", "incident_context"}
	cfg.Display.Platforms = map[string]map[string]any{}
	cfg.Memory.MemoryEnabled = false
	cfg.Memory.Provider = "multiuser_memory"
	cfg.Memory.UserProfileEnabled = false

	if agent.Spec.Harness != nil && agent.Spec.Harness.Memory != nil {
		if agent.Spec.Harness.Memory.MemoryEnabled != nil {
			cfg.Memory.MemoryEnabled = *agent.Spec.Harness.Memory.MemoryEnabled
		}
		if agent.Spec.Harness.Memory.Provider != "" {
			cfg.Memory.Provider = agent.Spec.Harness.Memory.Provider
		}
		if agent.Spec.Harness.Memory.UserProfileEnabled != nil {
			cfg.Memory.UserProfileEnabled = *agent.Spec.Harness.Memory.UserProfileEnabled
		}
	}

	if agent.Spec.Integration != nil {
		if gchat := agent.Spec.Integration.GoogleChat; gchat != nil {
			if gchat.Enabled != nil {
				cfg.Platforms.GoogleChat.Enabled = *gchat.Enabled
			}
			cfg.Display.Platforms["google_chat"] = resolveGoogleChatDisplayConfig(gchat.Mode)
		}
		if slack := agent.Spec.Integration.Slack; slack != nil && slack.Enabled != nil {
			cfg.Platforms.Slack.Enabled = *slack.Enabled
		}
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}
	return string(data)
}

// resolveGoogleChatDisplayConfig resolves verbosity settings for Google Chat based on mode ("default" or "debug").
func resolveGoogleChatDisplayConfig(mode string) map[string]any {
	resolvedMode := "default"
	if mode != "" {
		resolvedMode = strings.ToLower(mode)
	}

	toolProgress := "off"
	memoryNotifications := "off"
	interimMessages := false

	if resolvedMode == "debug" {
		toolProgress = "all"
		memoryNotifications = "verbose"
		interimMessages = true
	}

	return map[string]any{
		"tool_progress":              toolProgress,
		"memory_notifications":       memoryNotifications,
		"interim_assistant_messages": interimMessages,
		"long_running_notifications": true,
		"busy_ack_detail":            interimMessages,
	}
}

// buildPVC generates the PVC manifest for agent data persistence
func buildPVC(agent *agentv1alpha1.Agent) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-data",
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("10Gi"),
				},
			},
		},
	}
}

// systemPVCName is the per-agent name of the system-metadata claim. Kept in one place so the PVC
// and the Deployment's volume reference can never drift apart.
func systemPVCName(agent *agentv1alpha1.Agent) string {
	return agent.Name + "-system-metadata"
}

func buildSystemPVC(agent *agentv1alpha1.Agent) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			// Per-agent, like the data PVC (agent.Name + "-data"). A namespace-shared name here
			// would be ReadWriteOnce-multi-attached the moment a second agent lands in the same
			// namespace — which is the designed topology for kubeagents-system, where the
			// cluster-admin tier sits alongside the platform tier — leaving the second pod stuck
			// in ContainerCreating forever.
			Name:      systemPVCName(agent),
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("1Gi"),
				},
			},
		},
	}
}

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	// UID/GID 10000 matches the canonical unprivileged 'hermes' runtime user created in NousResearch/hermes-agent upstream Dockerfile
	fsGroup := int64(10000)

	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	tier := agentindex.EffectiveTier(agent)
	image := resolveAgentImage(agent.Spec.Deployment, defaultImageForTier(tier))

	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}

	var initContainers []corev1.Container
	var sidecars []corev1.Container
	var sidecarVolumes []corev1.Volume
	var extraVolumes []corev1.Volume
	var extraVolumeMounts []corev1.VolumeMount
	var podAnnotations map[string]string
	if agent.Spec.Deployment != nil {
		initContainers = agent.Spec.Deployment.InitContainers
		sidecars = agent.Spec.Deployment.Sidecars
		sidecarVolumes = agent.Spec.Deployment.SidecarVolumes
		extraVolumes = agent.Spec.Deployment.ExtraVolumes
		extraVolumeMounts = agent.Spec.Deployment.ExtraVolumeMounts
		podAnnotations = agent.Spec.Deployment.PodAnnotations
	}

	homeDir := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}

	pluginsDebugVal := "0"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.PluginsDebug != nil {
		if *agent.Spec.Harness.Hermes.PluginsDebug {
			pluginsDebugVal = "1"
		}
	}

	envVars := []corev1.EnvVar{
		{
			Name:  "PLATFORM_AGENT_HOME",
			Value: homeDir,
		},
		{
			Name:  "HOME",
			Value: strings.TrimSuffix(homeDir, "/") + "/home",
		},
		{
			Name:  "PLATFORM_AGENT_PLUGINS_DEBUG",
			Value: pluginsDebugVal,
		},
		{
			Name:  "API_SERVER_ENABLED",
			Value: "true",
		},
		{
			Name:  "API_SERVER_HOST",
			Value: "0.0.0.0",
		},
		{
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
		{
			// AGENT_TIER is the persona/containment level (agentindex.EffectiveTier; empty -> platform).
			//
			// Nothing in this repository reads it. Its one consumer was submit-suggestion's resolve_tier
			// (flag > $AGENT_TIER > platform), which namespaced PR branches by tier; that skill went away
			// with the proposal path. Its replacement does not use it either — apply-change composes the
			// idempotency key from KUBEAGENTS_AGENT_IDENTITY (broker_client.py), which carries the tier
			// AND the scope leaf, because the tier alone is identical for two agents of one tier. And
			// telemetry reports the tier through OTEL_RESOURCE_ATTRIBUTES, computed from the same `tier`
			// value a few lines below rather than read back out of here.
			//
			// It stays because it is the pod's own statement of which persona it is running, in the one
			// place an operator shelling into the container would look. Do not build an identity from it.
			Name:  "AGENT_TIER",
			Value: string(tier),
		},
	}

	// Telemetry attributes the agent's real tier (agentindex.EffectiveTier; empty -> platform) so
	// traces/metrics are grouped by tier rather than all reporting as "platform".
	envVars = append(envVars, otelTelemetryEnvVars(string(tier), agent.Name, agent.Namespace)...)

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.BrowserArgs) > 0 {
		envVars = append(envVars, corev1.EnvVar{
			Name:  "AGENT_BROWSER_ARGS",
			Value: strings.Join(agent.Spec.Deployment.BrowserArgs, " "),
		})
	}

	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ClusterName != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_CLUSTER_NAME",
				Value: agent.Spec.Harness.ClusterName,
			})
		}
		if agent.Spec.Harness.Location != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_LOCATION",
				Value: agent.Spec.Harness.Location,
			})
		}
		if agent.Spec.Harness.ProjectID != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GCP_PROJECT_ID",
				Value: agent.Spec.Harness.ProjectID,
			})
		}
		var apiServerSecretRef *corev1.SecretKeySelector
		if agent.Spec.Harness.Hermes != nil {
			apiServerSecretRef = agent.Spec.Harness.Hermes.ApiServerSecretRef
		}
		envVars = append(envVars, corev1.EnvVar{
			Name:      "API_SERVER_KEY",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(apiServerSecretRef, defaultPlatformAgentSecrets, "API_SERVER_KEY")},
		})
	}

	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "GOOGLE_CHAT_PROJECT_ID",
					Value: gchat.ProjectID,
				},
				{
					Name:  "GOOGLE_CHAT_SUBSCRIPTION_NAME",
					Value: fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName),
				},
				{
					Name:  "GOOGLE_CHAT_ALLOWED_USERS",
					Value: joinNonBlank(gchat.AllowedUsers),
				},
				{
					Name:  "GOOGLE_CHAT_HOME_CHANNEL",
					Value: gchat.HomeChannel,
				},
			}...)
		}
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars,
				corev1.EnvVar{
					Name:      "SLACK_BOT_TOKEN",
					ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.BotTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_BOT_TOKEN")},
				},
				corev1.EnvVar{
					Name:      "SLACK_APP_TOKEN",
					ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.AppTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_APP_TOKEN")},
				},
			)
			envVars = append(envVars, corev1.EnvVar{
				Name:  "SLACK_ALLOWED_USERS",
				Value: joinNonBlank(slack.AllowedUsers),
			})
			if slack.HomeChannel != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL",
					Value: slack.HomeChannel,
				})
			}
			if slack.HomeChannelName != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL_NAME",
					Value: slack.HomeChannelName,
				})
			}
		}
	}

	envVars = append(envVars, corev1.EnvVar{
		Name:  "TOKEN_BROKER_URL",
		Value: fmt.Sprintf("http://github-token-minter.%s.svc.cluster.local:8080/token", agent.Namespace),
	})

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.Env) > 0 {
		envVars = mergeEnvVars(envVars, agent.Spec.Deployment.Env)
	}

	// The envelope broker (08 §2.3) — a different thing from TOKEN_BROKER_URL above, which mints
	// GitHub tokens for the suggestion path. This one is the only route by which this pod's
	// intentions become cluster writes.
	//
	// Appended AFTER the merge, and that ordering is the security property. mergeEnvVars gives
	// `spec.deployment.env` the last word, which is right for every other variable here and wrong
	// for these five: a CR author who could set KUBEAGENTS_BROKER_ENDPOINT would be choosing which
	// broker this agent's envelopes reach, and one who could set KUBEAGENTS_BROKER_SAN would be
	// choosing which certificate the agent accepts on the way — together, enough to route a signed
	// envelope and a projected token to a listener of their choosing. Applying them last, through
	// the same merge in the other direction, makes such an override lose on name rather than
	// produce a duplicate entry whose winner depends on how the kubelet folds the list.
	envVars = mergeEnvVars(envVars, agentBrokerEnvVars(agent))

	dashboardEnabled := isDashboardEnabled(agent)

	var shareProcessNamespace *bool
	if dashboardEnabled {
		shareProcessNamespace = ptr.To(true)
	}

	var runtimeClassName *string
	if agent.Spec.Deployment != nil {
		runtimeClassName = agent.Spec.Deployment.RuntimeClassName
	}

	containers := buildBaseContainers(agent, image, pullPolicy, envVars, homeDir, extraVolumeMounts, dashboardEnabled)
	defaultAnnotations := map[string]string{
		"kubeagents.x-k8s.io/config-hash":            configHash,
		"kubeagents.x-k8s.io/fluent-bit-config-hash": fluentBitHash,
		"kubeagents.x-k8s.io/settings-config-hash":   settingsConfigHash,
	}

	if len(sidecars) > 0 {
		containers = append(containers, sidecars...)
	}

	volumes := buildDefaultVolumes(agent)
	volumes = append(volumes, agentBrokerVolumes(agent)...)
	if len(sidecarVolumes) > 0 {
		volumes = append(volumes, sidecarVolumes...)
	}
	if len(extraVolumes) > 0 {
		volumes = append(volumes, extraVolumes...)
	}

	// `wait-for-broker` goes FIRST, ahead of anything the CR asked for (08 §2.4). Init containers
	// run in order, so a CR-supplied init container placed before it would run while the agent's
	// broker may not exist — and a CR author who wanted the agent to do something unobserved would
	// put it exactly there. Prepending costs a CR nothing: its own init containers still run, just
	// after the pair is known to be up or known to be absent.
	initContainers = append([]corev1.Container{buildWaitForBrokerContainer(agent)}, initContainers...)

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "Deployment",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			// The five 08 §2.5 labels come from internal/agentlabels, which is the only place they
			// are spelled and the only place a scope value is rendered (V-RUN-011). They are merged
			// UNDER `app` so a future `app` change cannot silently shadow one of them, and they are
			// deliberately NOT in the (immutable) selector below: a selector that carried them
			// could never be corrected on a live Deployment.
			Labels: mergeLabels(agentlabels.For(agent, agentlabels.RoleReader), map[string]string{
				"app": agent.Name + "-gateway",
			}),
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Strategy: strategy,
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					// Same set on the pod template. This is the copy that matters most: 03 §4.2
					// pins a pod to its ServiceAccount by comparing the POD's tier/scope/role
					// labels against the SA's, and `vap-agent-pod-hardening` selects pods, not
					// Deployments.
					Labels: mergeLabels(agentlabels.For(agent, agentlabels.RoleReader), map[string]string{
						"app": agent.Name + "-gateway",
					}),
					Annotations: mergeAnnotations(defaultAnnotations, podAnnotations),
				},
				Spec: corev1.PodSpec{
					ShareProcessNamespace: shareProcessNamespace,
					RuntimeClassName:      runtimeClassName,
					InitContainers:        initContainers,
					ServiceAccountName:    saName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup: &fsGroup,
						// UID 10000 matches canonical 'hermes' runtime user in upstream image (NousResearch/hermes-agent Dockerfile line 92)
						RunAsUser:      ptr.To(int64(10000)),
						RunAsNonRoot:   ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: containers,
					Volumes:    volumes,
				},
			},
		},
	}
}

// buildBaseContainers generates the default containers for Agent
func buildBaseContainers(agent *agentv1alpha1.Agent, image string, pullPolicy corev1.PullPolicy, envVars []corev1.EnvVar, homeDir string, extraVolumeMounts []corev1.VolumeMount, dashboardEnabled bool) []corev1.Container {
	defaultPlatformAgentVolumeMounts := []corev1.VolumeMount{
		{
			Name:      "platform-agent-data-vol",
			MountPath: homeDir,
		},
		{
			Name:      "platform-agent-config-vol",
			MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
			SubPath:   "config.yaml",
			// Runtime-authoritative config is operator-rendered and must not be mutable by the agent
			// process: a pod that can rewrite its own config.yaml can wire itself a mutating tool and
			// route around the broker seam entirely (defense in depth, 03 §4).
			ReadOnly: true,
		},
		{
			Name:      "settings-volume",
			MountPath: path.Join(homeDir, "SETTINGS.md"),
			SubPath:   "SETTINGS.md",
			ReadOnly:  true,
		},
		{
			Name:      "system-metadata",
			MountPath: path.Dir(sessionKVDBPath),
			SubPath:   "session",
		},
		{
			// Writable scratch for readOnlyRootFilesystem (H-A, 05 §5). The agent's HOME and cwd live
			// under the /opt/data PVC (writable); only /tmp needs a separate ephemeral mount. Per
			// container (not a shared emptyDir) so a compromised sidecar cannot stage files in the
			// agent's scratch space.
			Name:      "platform-agent-tmp",
			MountPath: "/tmp",
		},
	}

	var apiServerSecretRef *corev1.SecretKeySelector
	clusterName := "platform-agent-host"
	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.Hermes != nil {
			apiServerSecretRef = agent.Spec.Harness.Hermes.ApiServerSecretRef
		}
		if agent.Spec.Harness.ClusterName != "" {
			clusterName = agent.Spec.Harness.ClusterName
		}
	}

	containers := []corev1.Container{
		{
			Name:            "platform-agent",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Ports: []corev1.ContainerPort{
				{
					Name:          "api",
					ContainerPort: 8642,
				},
			},
			Env: envVars,
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("500m"),
					corev1.ResourceMemory: resource.MustParse("2Gi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("2"),
					corev1.ResourceMemory: resource.MustParse("4Gi"),
				},
			},
			// The broker mounts land on the AGENT container only, never on a sidecar or the
			// dashboard: the mesh key and the audience-scoped token are what let a process speak
			// as this agent to its broker, and every container that can read them is another
			// place a compromise becomes a write.
			VolumeMounts: append(append(defaultPlatformAgentVolumeMounts, agentBrokerVolumeMounts()...), extraVolumeMounts...),
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				ReadOnlyRootFilesystem:   ptr.To(true),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		},
	}

	if dashboardEnabled {
		dashboardEnvVars := []corev1.EnvVar{
			{
				Name:  "PLATFORM_AGENT_HOME",
				Value: homeDir,
			},
			{
				Name:  "HOME",
				Value: strings.TrimSuffix(homeDir, "/") + "/home",
			},
			{
				Name:  "SESSION_KV_DB_PATH",
				Value: sessionKVDBPath,
			},
		}

		// The dashboard used to ship with no telemetry configuration at all, so it exported
		// nothing while sitting in the same pod as a fully instrumented agent — the container a
		// human is looking at was the one component invisible in the traces. It gets the agent's
		// values verbatim (see selectEnvVars: they share a config file on the PVC), plus one
		// resource attribute so its spans are still separable from the agent's.
		dashboardEnvVars = append(dashboardEnvVars, selectEnvVars(envVars, telemetryEnvNames)...)
		for i := range dashboardEnvVars {
			if dashboardEnvVars[i].Name == "OTEL_RESOURCE_ATTRIBUTES" && dashboardEnvVars[i].ValueFrom == nil {
				dashboardEnvVars[i].Value += ",kubeagents.component=dashboard"
			}
		}

		dashboardVolumeMounts := []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: homeDir,
			},
			{
				// The operator-rendered config, on the same terms as the main container.
				// Without this mount the dashboard read the image-baked /opt/defaults copy that
				// the entrypoint seeds onto the PVC — the one renderConfigYAML exists to shadow.
				// The two disagree by construction: the render is where per-Agent settings
				// (model endpoint, enabled chat platforms, memory) live, so the dashboard
				// displayed a different agent's worth of configuration than the agent was
				// running. This is LSN-003's shape, and the fix is to read the same bytes.
				Name:      "platform-agent-config-vol",
				MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
				SubPath:   "config.yaml",
				ReadOnly:  true,
			},
			{
				Name:      "system-metadata",
				MountPath: path.Dir(sessionKVDBPath),
				SubPath:   "session",
			},
			{
				// Writable scratch for readOnlyRootFilesystem (H-A) — see the main container.
				Name:      "platform-agent-dashboard-tmp",
				MountPath: "/tmp",
			},
		}

		containers = append(containers, corev1.Container{
			Name:            "platform-agent-dashboard",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Args:            []string{"hermes", "dashboard"},
			Ports: []corev1.ContainerPort{
				{
					Name:          "dashboard",
					ContainerPort: 9119,
				},
			},
			Env: dashboardEnvVars,
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("256m"),
					corev1.ResourceMemory: resource.MustParse("512Mi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("1"),
					corev1.ResourceMemory: resource.MustParse("2Gi"),
				},
			},
			VolumeMounts: append(dashboardVolumeMounts, extraVolumeMounts...),
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				ReadOnlyRootFilesystem:   ptr.To(true),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		})
	}

	containers = append(containers, corev1.Container{
		Name:  "fluent-bit",
		Image: "fluent/fluent-bit:5.0.7",
		Args: []string{
			"-c",
			"/fluent-bit/etc/fluent-bit.conf",
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse("100m"),
				corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
				corev1.ResourceMemory:           resource.MustParse("128Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse("500m"),
				corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
				corev1.ResourceMemory:           resource.MustParse("256Mi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: "/opt/data",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-config",
				MountPath: "/fluent-bit/etc/fluent-bit.conf",
				SubPath:   "fluent-bit.conf",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-config",
				MountPath: "/fluent-bit/etc/parsers.conf",
				SubPath:   "parsers.conf",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-state",
				MountPath: "/fluent-bit/state",
			},
			{
				// Writable scratch for readOnlyRootFilesystem (H-A) — fluent-bit buffers/scratch.
				Name:      "fluent-bit-tmp",
				MountPath: "/tmp",
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	})

	// Inject the k8s-event-watcher sidecar container to capture GKE warnings and stream them to the local REST bridge.
	// The watcher's --owner is the agent's effective tier (its X-Asserted-Caller identity), and a namespace-scoped
	// developer-team agent additionally pins the watcher's informer to its own namespace (server-side scoping) so it
	// can never observe events outside its tenant.
	watcherTier := agentindex.EffectiveTier(agent)
	watcherArgs := []string{
		"--cluster-name=" + clusterName,
		"--daemon-url=http://127.0.0.1:8699",
		"--token-env=API_SERVER_KEY",
		"--owner=" + string(watcherTier),
		"--reason=FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled",
	}
	if watcherTier == agentv1alpha1.TierDeveloperTeam {
		scopeNS := ""
		if agent.Spec.Scope != nil {
			scopeNS = agent.Spec.Scope.Namespace
		}
		watcherArgs = append(watcherArgs, "--scope-namespace="+scopeNS)
	}
	containers = append(containers, corev1.Container{
		Name:            "event-watcher",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command: []string{
			"/usr/local/bin/k8s-event-watcher",
		},
		Args: watcherArgs,
		Env: []corev1.EnvVar{
			{
				Name: "API_SERVER_KEY",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: defaultSecretRef(
						apiServerSecretRef,
						defaultPlatformAgentSecrets,
						"API_SERVER_KEY",
					),
				},
			},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{
				// Writable scratch for readOnlyRootFilesystem (H-A) — the Go watcher's os.TempDir.
				Name:      "event-watcher-tmp",
				MountPath: "/tmp",
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	})

	return containers
}

// buildDefaultVolumes generates the default volumes for Agent
func buildDefaultVolumes(agent *agentv1alpha1.Agent) []corev1.Volume {
	volumes := []corev1.Volume{
		{
			Name: "platform-agent-data-vol",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: agent.Name + "-data",
				},
			},
		},
		{
			Name: "platform-agent-config-vol",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-config",
					},
					DefaultMode: ptr.To(int32(0755)),
				},
			},
		},
		{
			Name: "fluent-bit-config",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-fluent-bit-config",
					},
					DefaultMode: ptr.To(int32(420)),
				},
			},
		},
		{
			Name: "fluent-bit-state",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{},
			},
		},
		{
			Name: "system-metadata",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: systemPVCName(agent),
				},
			},
		},
		{
			Name: "settings-volume",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-settings",
					},
					DefaultMode: ptr.To(int32(0644)),
				},
			},
		},
		// Per-container writable /tmp scratch backing readOnlyRootFilesystem (H-A, 05 §5). Ephemeral
		// (emptyDir) so nothing durable escapes the read-only root; per container so a compromised
		// sidecar cannot plant files in the agent's scratch space.
		{
			Name:         "platform-agent-tmp",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
		{
			Name:         "fluent-bit-tmp",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
		{
			Name:         "event-watcher-tmp",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
	}

	// The dashboard container is only rendered when enabled, so its /tmp volume is added to match —
	// keeping the volume set free of an orphaned entry when the dashboard is off.
	if isDashboardEnabled(agent) {
		volumes = append(volumes, corev1.Volume{
			Name:         "platform-agent-dashboard-tmp",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
	}

	return volumes
}

// NOTE: buildPlatformExplorerRole / buildClusterRoleBinding were removed in P1-T4. The controller no
// longer mints agent RBAC at runtime; the agent pod's READER identity (explorer ClusterRole +
// binding, get/list/watch only) is pre-created via GitOps (examples/gitops-repo/policy/rbac-overlay/)
// and enforced by vap-agent-readonly. Write authority is not absent from the system, it is held
// elsewhere: on the separate actor ServiceAccount the broker runs as, never on the agent's. Do not
// reintroduce a runtime RBAC-minting path here (08 §4, 03 §4).

// Helper to calculate the SHA256 hash of ConfigMap Data for rolling restarts.
func getConfigMapHash(configMap *corev1.ConfigMap) (string, error) {
	if configMap == nil {
		return "", nil
	}
	dataBytes, err := json.Marshal(configMap.Data)
	if err != nil {
		return "", err
	}
	hash := sha256.Sum256(dataBytes)
	return fmt.Sprintf("%x", hash), nil
}

// buildFluentBitConfigMap generates the ConfigMap manifest containing fluent-bit.conf
func buildFluentBitConfigMap(agent *agentv1alpha1.Agent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-fluent-bit-config",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"fluent-bit.conf": `[SERVICE]
    Flush         1
    Daemon        Off
    Log_Level     info
    Parsers_File  parsers.conf

[INPUT]
    Name              tail
    Tag               agent.logs
    Path              /opt/data/logs/*.log
    DB                /fluent-bit/state/fluent-bit.db
    Refresh_Interval  5
    Rotate_Wait       30
    Mem_Buf_Limit     20MB
    Skip_Long_Lines   On
    Read_from_Head    On
    Path_Key          file_path

[FILTER]
    Name          parser
    Match         agent.logs
    Key_Name      log
    Parser        gchat_event
    Reserve_Data  On
    Preserve_Key  On

[FILTER]
    Name              record_modifier
    Match             agent.logs
    Record            app agent
    Record            log_source agent-file

[OUTPUT]
    Name              stdout
    Match             agent.logs
    Format            json_lines
`,
			"parsers.conf": `[PARSER]
    Name    gchat_event
    Format  regex
    Regex   User=(?<gchat_user>[^,\s]+),\s*Session=(?<gchat_session>[^,\s]+)
`,
		},
	}
}

// buildAgentService generates the Service manifest for Agent
func buildAgentService(agent *agentv1alpha1.Agent) *corev1.Service {
	dashboardEnabled := isDashboardEnabled(agent)

	ports := []corev1.ServicePort{
		{
			Name:       "api",
			Port:       8642,
			TargetPort: intstr.FromString("api"),
		},
	}

	if dashboardEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:       "dashboard",
			Port:       9119,
			TargetPort: intstr.FromString("dashboard"),
		})
	}

	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "Service",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name,
			Namespace: agent.Namespace,
			// 08 §2.5 stamps the five on the Service too, so "show me both halves of this agent"
			// (`-l kube-agents/agent=<name>`) returns the Services as well as the workloads. A
			// Service selector is mutable, but this one stays `app`-only: it must keep matching
			// pods from a Deployment rolled before these labels existed.
			Labels: agentlabels.For(agent, agentlabels.RoleReader),
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				"app": agent.Name + "-gateway",
			},
			Ports: ports,
		},
	}
}

func isDashboardEnabled(agent *agentv1alpha1.Agent) bool {
	if agent != nil && agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.DashboardEnabled != nil {
		return *agent.Spec.Harness.Hermes.DashboardEnabled
	}
	return true
}
