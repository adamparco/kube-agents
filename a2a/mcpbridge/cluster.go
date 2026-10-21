package mcpbridge

import (
	"context"
	"errors"
)

// ClusterAccess is the Kubernetes-facing part of the bridge: kubeconfig
// discovery, credential lookup, and the port-forward to the a2a-gateway MCP
// listener. It is implemented by package kube (k8s.io/client-go) and injected
// through ServerOptions and RunCLI so this package carries no Kubernetes
// dependency and distributions can supply their own implementation.
type ClusterAccess interface {
	// DiscoverDeployments scans the kubeconfig contexts (or the single filtered
	// context) for kube-agents PlatformAgents and returns their profiles.
	DiscoverDeployments(ctx context.Context, kubeContextFilter string, includeCredentials bool) ([]DeploymentProfile, error)
	// PopulateDeploymentCredentials fills the bus and gateway credentials of prof
	// from the deployment's credentials Secret when they are not set yet.
	PopulateDeploymentCredentials(ctx context.Context, prof *DeploymentProfile) error
	// StartGatewayPortForward opens a supervised local port-forward to the
	// deployment's a2a-gateway MCP listener.
	StartGatewayPortForward(ctx context.Context, prof DeploymentProfile) (Tunnel, error)
}

// Tunnel is a supervised local port-forward to the gateway MCP listener.
type Tunnel interface {
	// MCPURL returns the local http:// base URL of the forwarded listener.
	MCPURL() string
	// Restart tears down and re-establishes the forward on the same local port.
	Restart()
	// Close stops the forward and its supervisor.
	Close()
}

// ErrNoClusterAccess is returned when a Kubernetes-backed operation is requested
// but the bridge was started without a ClusterAccess implementation.
var ErrNoClusterAccess = errors.New("kube-agents-mcp: no Kubernetes cluster access configured")

// noClusterAccess is the ClusterAccess used when none is injected: every
// Kubernetes-backed operation fails with ErrNoClusterAccess, while direct
// gateway URLs and all host-local state keep working.
type noClusterAccess struct{}

func (noClusterAccess) DiscoverDeployments(context.Context, string, bool) ([]DeploymentProfile, error) {
	return nil, ErrNoClusterAccess
}

func (noClusterAccess) PopulateDeploymentCredentials(context.Context, *DeploymentProfile) error {
	return ErrNoClusterAccess
}

func (noClusterAccess) StartGatewayPortForward(context.Context, DeploymentProfile) (Tunnel, error) {
	return nil, ErrNoClusterAccess
}
