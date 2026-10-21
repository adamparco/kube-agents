// Package mcpbridge is the workstation-side kube-agents Model Context Protocol
// bridge: a stdio JSON-RPC server, plus an Antigravity lifecycle hook handler
// (--hook), that lets a local coding agent (Antigravity) drive a kube-agents
// deployment through its A2A JetStream bus - discover and connect to
// deployments, send a message and stream the turn's steps back as it runs,
// inspect and steer tasks, read blackboard topics, open the PRs an agent
// filed, and receive push notifications when work finishes out of turn.
//
// It is a thin client of the in-cluster MCP server hosted by a2a-gateway: it
// forwards tool calls over HTTP/NDJSON and keeps the host-local state the
// in-cluster side cannot reach: it projects the fleet into Antigravity's native
// Swarm UI directory (.system_generated/swarm/) via SwarmSink and
// LineageResolver, re-parents subagent conversations through the Antigravity
// language server, saves PR .diff/.patch files for the native viewer, and wakes
// the owning conversation through agentapi push notifications. Everything that
// touches Kubernetes - kubeconfig discovery, the credentials Secret, and the
// in-process SPDY port-forward to the gateway - sits behind the ClusterAccess
// interface and is implemented by the sibling package kube (k8s.io/client-go),
// so this package itself has no Kubernetes dependency. Linux and macOS are
// supported; process supervision relies on POSIX signals.
//
// Host integration is configured through the environment; every variable is
// optional and the defaults match a stock Antigravity installation:
//
//	ANTIGRAVITY_CONVERSATION_ID    conversation to bind for push notifications
//	                               (exported by Antigravity for hooks and tools)
//	ANTIGRAVITY_CSRF_TOKEN         language-server CSRF token; when unset the
//	                               bridge reads it from the running server
//	KUBE_AGENTS_AGY_BRAIN_DIR      Antigravity brain directory
//	                               (default ~/.gemini/antigravity/brain)
//	KUBE_AGENTS_AGY_AGENTAPI_PATH  agentapi binary used for push wakeups
//	                               (default ~/.gemini/antigravity/bin/agentapi)
//	KUBE_AGENTS_AGY_LS_URL         Antigravity language server base URL
//	                               (default http://127.0.0.1:5387)
//	KUBE_AGENTS_MCP_CONFIG         bridge state file
//	                               (default ~/.config/kube-agents/mcp-config.json)
//	GH_TOKEN / GITHUB_TOKEN        GitHub token for inspect_pr; when neither is
//	                               set the bridge falls back to the github.com
//	                               entry in ~/.git-credentials, if present
//
// RunCLI is the entry point: besides --hook it accepts --version, --config,
// --ui-action {connect,disconnect,refresh,init} with --deployment and
// --conversation-id, and otherwise serves MCP over stdio. Callers pass the
// ClusterAccess implementation (normally kube.New()); the binary lives in
// a2a/cmd/kube-agents-mcp.
package mcpbridge
