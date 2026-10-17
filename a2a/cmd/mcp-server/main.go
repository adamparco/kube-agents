// Command mcp-server runs the Model Context Protocol (MCP) server for the
// kube-agents A2A v2 (next) JetStream message bus over stdio.
package main

import (
	"fmt"
	"os"

	"github.com/gke-labs/kube-agents/a2a/mcp"
)

func main() {
	if err := mcp.RunCLI(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(1)
	}
}
