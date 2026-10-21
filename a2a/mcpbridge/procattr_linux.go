//go:build linux

package mcpbridge

import "syscall"

// childProcAttr puts a child helper process (agentapi, gh) in its own process group so the
// caller can kill the whole group, and asks the kernel to SIGTERM it if this process dies
// without running Close (SIGKILL, OOM, a hard Antigravity restart) so no helper outlives the
// bridge.
func childProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}
}
