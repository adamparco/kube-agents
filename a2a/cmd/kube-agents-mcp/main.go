// Command kube-agents-mcp is the workstation-side kube-agents bridge for
// Antigravity: it serves the Model Context Protocol over stdio and forwards
// tool calls to the in-cluster MCP server on a2a-gateway, which fronts the
// kube-agents A2A v2 (next) JetStream message bus.
//
// Run with --hook as an Antigravity SessionStart/PreInvocation lifecycle hook,
// with --version to print the version, or without arguments as a stdio MCP
// server. See mcpbridge.RunCLI for the remaining flags.
package main

import (
	"fmt"
	"os"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
	"github.com/gke-labs/kube-agents/a2a/mcpbridge/kube"
)

func main() {
	if err := mcpbridge.RunCLI(os.Args[1:], kube.New()); err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(1)
	}
}
