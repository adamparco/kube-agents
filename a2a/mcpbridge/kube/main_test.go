package kube

import (
	"os"
	"testing"
)

// Shared fixtures for the package tests (mirrors the core test fixtures so the
// discovery and port-forward tests read the same way in both packages).
const (
	testClusterName    = "prod-us-central1"
	testKubeContext    = "gke_example-project_us-central1_prod-us-central1"
	testUnreachableCtx = "gke_example-project_us-central1_unreachable"
	testNamespace      = "kubeagents-system"
	testPAName         = "platform-agent"
	testProjectID      = "example-project"
	testLocation       = "us-central1"
	testGitHubRepo     = "example-org/example-infra"
	testConsoleAuthVal = "console-secret-token"
	testGatewayAuthVal = "gateway-secret-token"
)

// TestMain keeps the suite hermetic when it runs inside an Antigravity
// conversation (the host exports ANTIGRAVITY_CONVERSATION_ID to tool processes).
func TestMain(m *testing.M) {
	os.Unsetenv("ANTIGRAVITY_CONVERSATION_ID")
	os.Exit(m.Run())
}
