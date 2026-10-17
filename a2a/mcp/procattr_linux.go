//go:build linux

package mcp

import "syscall"

// childProcAttr puts a supervised child (kubectl port-forward) in its own process group so the
// supervisor can kill the whole group, and asks the kernel to SIGTERM it if this process dies
// without running Close (SIGKILL, OOM, a hard Jetski restart). Soak testing found 13 orphaned
// port-forwards after a night of MCP restarts, each holding a tunnel to the cluster.
func childProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}
}
