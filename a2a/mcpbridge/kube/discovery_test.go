package kube

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"k8s.io/client-go/kubernetes"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

// Deployment discovery and credential population through in-process client-go.

func TestDiscoverDeploymentsAndCredentials(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case crdAbsPathPlatformAgents:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "kubeagents.x-k8s.io/v1alpha1",
				"kind":       "PlatformAgentList",
				"items": []map[string]any{
					{
						"metadata": map[string]any{
							"name":      testPAName,
							"namespace": testNamespace,
						},
						"spec": map[string]any{
							"mode":    "next",
							"version": "0fe1c8ce",
							"harness": map[string]any{
								"projectId":   testProjectID,
								"clusterName": testClusterName,
								"location":    testLocation,
							},
							"integration": map[string]any{
								"github": map[string]any{
									"gitRepo": testGitHubRepo,
								},
								"gchat": map[string]any{"enabled": true},
							},
						},
						"status": map[string]any{
							"conditions": []map[string]any{
								{"type": "Ready", "status": "True", "message": "Reconciled"},
							},
						},
					},
				},
			})
		case crdAbsPathAgentProfiles:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "kubeagents.x-k8s.io/v1alpha1",
				"kind":       "AgentProfileList",
				"items": []map[string]any{
					{"metadata": map[string]any{"name": "default", "namespace": testNamespace}},
					{
						"metadata": map[string]any{"name": "cluster-upgrade", "namespace": testNamespace},
						"spec": map[string]any{
							"clusterRef": map[string]any{"cluster": testClusterName},
						},
					},
				},
			})
		case fmt.Sprintf("/api/v1/namespaces/%s/secrets/%s%s", testNamespace, testPAName, mcpbridge.CredsObjectSuffix):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "Secret",
				"metadata": map[string]any{
					"name":      testPAName + mcpbridge.CredsObjectSuffix,
					"namespace": testNamespace,
				},
				"data": map[string]string{
					consoleAuthField: base64.StdEncoding.EncodeToString([]byte(testConsoleAuthVal + "\n")),
					gatewayAuthField: base64.StdEncoding.EncodeToString([]byte(testGatewayAuthVal + "\n")),
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	kubeconfigYAML := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: reachable
  cluster:
    server: %s
- name: unreachable
  cluster:
    server: http://127.0.0.1:1
contexts:
- name: %s
  context:
    cluster: reachable
- name: %s
  context:
    cluster: unreachable
current-context: %s
`, srv.URL, testKubeContext, testUnreachableCtx, testKubeContext)

	kcPath := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(kcPath, []byte(kubeconfigYAML), 0o600); err != nil {
		t.Fatalf("WriteFile kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", kcPath)

	profiles, err := DiscoverDeployments(context.Background(), "", true)
	if err != nil {
		t.Fatalf("DiscoverDeployments: %v", err)
	}
	if len(profiles) != 1 {
		t.Fatalf("got %d profiles, want 1", len(profiles))
	}
	p := profiles[0]
	if p.Name != testClusterName || !p.Ready || p.GitHubRepo != testGitHubRepo {
		t.Errorf("unexpected profile metadata: %+v", p)
	}
	if len(p.AgentProfiles) != 1 || p.AgentProfiles[0] != "default" {
		t.Errorf("expected only interactive AgentProfile 'default', got %v", p.AgentProfiles)
	}
	if p.BusAuthValue != testConsoleAuthVal || p.ObserverAuthValue != testGatewayAuthVal {
		t.Errorf("credentials not decoded properly: bus=%q observer=%q", p.BusAuthValue, p.ObserverAuthValue)
	}
}

func TestResolveServiceTargetPodPrefersReadyPod(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case fmt.Sprintf("/api/v1/namespaces/%s/services/%s-a2a-nats", testNamespace, testPAName):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "Service",
				"metadata":   map[string]any{"name": testPAName + "-a2a-nats", "namespace": testNamespace},
				"spec": map[string]any{
					"selector": map[string]string{"app": "a2a-nats"},
				},
			})
		case fmt.Sprintf("/api/v1/namespaces/%s/pods", testNamespace):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "PodList",
				"items": []map[string]any{
					{
						"metadata": map[string]any{"name": "nats-0-not-ready", "namespace": testNamespace},
						"status": map[string]any{
							"phase": "Running",
							"conditions": []map[string]any{
								{"type": "Ready", "status": "False"},
							},
						},
					},
					{
						"metadata": map[string]any{"name": "nats-1-ready", "namespace": testNamespace},
						"status": map[string]any{
							"phase": "Running",
							"conditions": []map[string]any{
								{"type": "Ready", "status": "True"},
							},
						},
					},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	kubeconfigYAML := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: reachable
  cluster:
    server: %s
contexts:
- name: %s
  context:
    cluster: reachable
current-context: %s
`, srv.URL, testKubeContext, testKubeContext)

	kcPath := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(kcPath, []byte(kubeconfigYAML), 0o600); err != nil {
		t.Fatalf("WriteFile kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", kcPath)

	restCfg, err := restConfigForKubeContext(testKubeContext)
	if err != nil {
		t.Fatalf("restConfigForKubeContext: %v", err)
	}
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		t.Fatalf("kubernetes.NewForConfig: %v", err)
	}
	tun := &PortForwardTunnel{
		namespace:  testNamespace,
		service:    testPAName + "-a2a-nats",
		remotePort: mcpbridge.DefaultNATSPort,
	}
	podName, targetPort := tun.resolveServiceTargetPod(context.Background(), clientset)
	if podName != "nats-1-ready" || targetPort != mcpbridge.DefaultNATSPort {
		t.Fatalf("resolveServiceTargetPod = (%q, %d), want ('nats-1-ready', %d)", podName, targetPort, mcpbridge.DefaultNATSPort)
	}
}
