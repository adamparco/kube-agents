// Command mcp-server runs the Model Context Protocol (MCP) server for the
// kube-agents A2A v2 (next) JetStream message bus over stdio.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/gke-labs/kube-agents/a2a/mcp"
)

const (
	flagConfigName = "config"
	flagConfigDesc = "path to persistent MCP configuration file (default: ~/.config/kube-agents/mcp-config.json)"
	flagHookName   = "hook"
	flagHookDesc   = "run as a Jetski SessionStart/PreInvocation lifecycle hook (reads hook JSON on stdin, binds conversationId, and drains pending push events)"
	errExitCode    = 1
)

func main() {
	configPath := flag.String(flagConfigName, "", flagConfigDesc)
	hookMode := flag.Bool(flagHookName, false, flagHookDesc)
	flag.Parse()

	store, err := mcp.NewConfigStore(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(errExitCode)
	}
	if *hookMode {
		if err := mcp.HandleJetskiHook(store, os.Stdin, os.Stdout); err != nil {
			os.Exit(errExitCode)
		}
		return
	}
	rpcOut := os.Stdout
	os.Stdout = os.Stderr

	srv, err := mcp.NewServer(mcp.ServerOptions{Store: store})
	if err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(errExitCode)
	}
	defer srv.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		srv.Close()
		os.Exit(errExitCode)
	}()

	if err := srv.ServeStdio(ctx, os.Stdin, rpcOut); err != nil {
		fmt.Fprintf(os.Stderr, "kube-agents-mcp: %v\n", err)
		os.Exit(errExitCode)
	}
}
