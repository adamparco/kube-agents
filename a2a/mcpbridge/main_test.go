package mcpbridge

import (
	"os"
	"testing"
)

// TestMain isolates the package tests from the Antigravity host environment: when the
// suite itself runs inside an Antigravity conversation, the host exports
// ANTIGRAVITY_CONVERSATION_ID, which would otherwise leak into every conversation
// resolution fallback and make binding assertions depend on the caller.
func TestMain(m *testing.M) {
	os.Unsetenv(envAGYConversationID)
	os.Unsetenv(envAGYBrainDir)
	os.Exit(m.Run())
}
