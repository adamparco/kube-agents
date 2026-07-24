package testing

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/testutil"
)

// TestClusterAdminRender_LoadBearing asserts, in prose the golden byte-compare can't, the four
// load-bearing properties of a rendered cluster-admin pod (Phase 2 P2-T11; 06 §2; 03 §4/§11;
// acceptance b/d/e). The golden file (testdata/cluster-admin/expected/agent.yaml) locks the FULL
// render against drift; this test states WHY specific fields matter so a future edit that regenerates
// the golden can't quietly weaken a security property without a named failure here.
func TestClusterAdminRender_LoadBearing(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile(filepath.Join("testdata", "cluster-admin", "input.yaml"))
	if err != nil {
		t.Fatalf("read input: %v", err)
	}
	agent := &agentv1alpha1.Agent{}
	if err := yaml.Unmarshal(data, agent); err != nil {
		t.Fatalf("unmarshal input: %v", err)
	}

	resources, err := testutil.RunOperatorReconcile(
		context.Background(), testScheme, agent,
		func(c client.Client, s *runtime.Scheme) reconcile.Reconciler {
			return &controller.AgentReconciler{Client: c, Scheme: s}
		},
	)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	var dep *appsv1.Deployment
	var cfg *corev1.ConfigMap
	for _, r := range resources {
		switch o := r.(type) {
		case *appsv1.Deployment:
			dep = o
		case *corev1.ConfigMap:
			if o.Name == agent.Name+"-config" {
				cfg = o
			}
		}
	}
	if dep == nil {
		t.Fatal("no Deployment rendered")
	}
	if cfg == nil {
		t.Fatal("no agent-config ConfigMap rendered")
	}

	// (b/e) The pod binds the PRE-CREATED read-only ServiceAccount by name — the controller references
	// it, never mints RBAC (08 §4). A drift to a mutating SA would break the read-only invariant.
	if got := dep.Spec.Template.Spec.ServiceAccountName; got != "cluster-admin-agent" {
		t.Errorf("serviceAccountName = %q, want cluster-admin-agent", got)
	}

	main := containerByName(t, dep, "platform-agent")

	// (Phase 5 H-A, 05 §5) Every agent container runs with a read-only root filesystem — a mutable
	// root fs would let a compromised process rewrite its own binaries/config. Assert it on EVERY
	// rendered container so a golden regen can't silently weaken one; a writable /tmp emptyDir backs it.
	for _, c := range dep.Spec.Template.Spec.Containers {
		if c.SecurityContext == nil || c.SecurityContext.ReadOnlyRootFilesystem == nil || !*c.SecurityContext.ReadOnlyRootFilesystem {
			t.Errorf("container %q: readOnlyRootFilesystem must be true (Phase 5 hardening)", c.Name)
		}
		hasTmp := false
		for _, m := range c.VolumeMounts {
			if m.MountPath == "/tmp" {
				hasTmp = true
			}
		}
		if !hasTmp {
			t.Errorf("container %q: a writable /tmp mount must back readOnlyRootFilesystem", c.Name)
		}
	}

	// Image is resolved BY TIER: the input omits spec.deployment.image, so the render must fall back to
	// defaultImageForTier(cluster-admin). A regression here means a cluster-admin pod runs a wrong image.
	const wantImage = "ghcr.io/gke-labs/kube-agents/cluster-admin-agent:v0.1.0"
	if main.Image != wantImage {
		t.Errorf("image = %q, want tier-default %q", main.Image, wantImage)
	}

	env := envMap(main)

	// submit-suggestion namespaces its PR branches by AGENT_TIER; cluster-admin proposals must land under
	// cluster-admin-agent/ and never masquerade as the platform tier.
	if got := env["AGENT_TIER"]; got != "cluster-admin" {
		t.Errorf("AGENT_TIER = %q, want cluster-admin", got)
	}

	// (d) The CLOSED chat allowlist renders to GOOGLE_CHAT_ALLOWED_USERS, and the permissive backstop
	// GOOGLE_CHAT_ALLOW_ALL_USERS must be ABSENT (it is emitted only for an empty/absent allowlist).
	if got := env["GOOGLE_CHAT_ALLOWED_USERS"]; got != "users/REPLACE_WITH_CLUSTER_ADMIN_ID" {
		t.Errorf("GOOGLE_CHAT_ALLOWED_USERS = %q, want the closed list", got)
	}
	if _, present := env["GOOGLE_CHAT_ALLOW_ALL_USERS"]; present {
		t.Error("GOOGLE_CHAT_ALLOW_ALL_USERS is set: a closed allowlist must never emit the allow-all backstop")
	}

	// Zero mutating tools: the runtime-authoritative config.yaml (mounted over /opt/data/config.yaml)
	// must not wire the cluster-mutating `gke` remote MCP or any create/delete/apply verb (03 §4, 06 §9).
	rendered := cfg.Data["config.yaml"]
	for _, forbidden := range []string{"mcp-gke", "create_cluster", "delete_cluster", "apply_manifest", "container.googleapis.com"} {
		if strings.Contains(rendered, forbidden) {
			t.Errorf("rendered config.yaml contains a mutating-tool reference %q; the agent must be read-only", forbidden)
		}
	}
	// Sanity: the read-only toolset is actually present (guards against an empty/broken render passing above).
	if !strings.Contains(rendered, "mcp-platform_control") {
		t.Error("rendered config.yaml missing expected read-only toolset mcp-platform_control")
	}
}

func containerByName(t *testing.T, dep *appsv1.Deployment, name string) corev1.Container {
	t.Helper()
	for _, c := range dep.Spec.Template.Spec.Containers {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("container %q not found in Deployment", name)
	return corev1.Container{}
}

func envMap(c corev1.Container) map[string]string {
	m := make(map[string]string, len(c.Env))
	for _, e := range c.Env {
		m[e.Name] = e.Value
	}
	return m
}
