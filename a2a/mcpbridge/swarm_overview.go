package mcpbridge

import (
	"fmt"
	"strings"
	"time"
)

const (
	defaultUpstreamRepoURL         = "https://github.com/gke-labs/kube-agents"
	defaultUpstreamDocsURL         = "https://gke-labs.github.io/kube-agents/"
	defaultUpstreamInstallGuideURL = "https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md"
	defaultUpstreamUpgradeGuideURL = "https://github.com/gke-labs/kube-agents/blob/main/docs/site/src/content/docs/install/upgrade.md"
	defaultUpstreamReleasesURL     = "https://github.com/gke-labs/kube-agents/releases"
	defaultFallbackReleaseTag      = "0.16.0"
	defaultDisconnectedSwarmID     = "_kube_agents_default"
)

// KubeAgentsClusterCandidate summarizes a discovered kube-agents cluster that can be
// connected with a single click from the Not-Connected Overview tab.
type KubeAgentsClusterCandidate struct {
	Name              string `json:"name"`
	ProjectID         string `json:"project_id,omitempty"`
	Location          string `json:"location,omitempty"`
	ClusterName       string `json:"cluster_name,omitempty"`
	KubeContext       string `json:"kube_context,omitempty"`
	Namespace         string `json:"namespace,omitempty"`
	PlatformAgentName string `json:"platform_agent_name,omitempty"`
	Mode              string `json:"mode,omitempty"`
	Version           string `json:"version,omitempty"`
	GitHubRepo        string `json:"github_repo,omitempty"`
	Ready             bool   `json:"ready"`
}

// KubeAgentsOverviewState provides rich structured metadata for the Swarm UI Overview tab
// in both Connected and Not-Connected modes.
type KubeAgentsOverviewState struct {
	Connected                bool                         `json:"connected"`
	UpdatedAt                string                       `json:"updated_at"`
	RootConversationID       string                       `json:"root_conversation_id,omitempty"`
	ProjectID                string                       `json:"project_id,omitempty"`
	Location                 string                       `json:"location,omitempty"`
	ClusterName              string                       `json:"cluster_name,omitempty"`
	KubeContext              string                       `json:"kube_context,omitempty"`
	Namespace                string                       `json:"namespace,omitempty"`
	PlatformAgentName        string                       `json:"platform_agent_name,omitempty"`
	Mode                     string                       `json:"mode,omitempty"`
	Ready                    bool                         `json:"ready"`
	StatusMessage            string                       `json:"status_message,omitempty"`
	GitHubRepo               string                       `json:"github_repo,omitempty"`
	GitHubRepoURL            string                       `json:"github_repo_url,omitempty"`
	ChatPlatform             string                       `json:"chat_platform,omitempty"`
	NATSService              string                       `json:"nats_service,omitempty"`
	NATSPort                 int                          `json:"nats_port,omitempty"`
	BusUser                  string                       `json:"bus_user,omitempty"`
	BusEndpoint              string                       `json:"bus_endpoint,omitempty"`
	ActiveConversationAlias  string                       `json:"active_conversation_alias,omitempty"`
	ActiveConsoleToken       string                       `json:"active_console_token,omitempty"`
	PushArmed                bool                         `json:"push_armed"`
	SwarmTitle               string                       `json:"swarm_title,omitempty"`
	SwarmMemberNames         []string                     `json:"swarm_member_names,omitempty"`
	AgentProfiles            []string                     `json:"agent_profiles,omitempty"`
	ActiveSessionPodCount    int                          `json:"active_session_pod_count"`
	ClustersMonitored        int                          `json:"clusters_monitored,omitempty"`
	ToolExecutionsTotal      int                          `json:"tool_executions_total,omitempty"`
	RemediationsProposed     int                          `json:"remediations_proposed,omitempty"`
	RemediationsMerged       int                          `json:"remediations_merged,omitempty"`
	InstalledVersion         string                       `json:"installed_version,omitempty"`
	InstalledShortVersion    string                       `json:"installed_short_version,omitempty"`
	HelmChartVersion         string                       `json:"helm_chart_version,omitempty"`
	LatestReleaseTag         string                       `json:"latest_release_tag,omitempty"`
	LatestReleaseCommit      string                       `json:"latest_release_commit,omitempty"`
	LatestReleaseShortCommit string                       `json:"latest_release_short_commit,omitempty"`
	LatestReleaseURL         string                       `json:"latest_release_url,omitempty"`
	LatestReleasePublishedAt string                       `json:"latest_release_published_at,omitempty"`
	UpgradeAvailable         bool                         `json:"upgrade_available"`
	CommitsBehind            int                          `json:"commits_behind,omitempty"`
	UpgradeCommand           string                       `json:"upgrade_command,omitempty"`
	InstallCommand           string                       `json:"install_command,omitempty"`
	UpstreamRepoURL          string                       `json:"upstream_repo_url"`
	UpstreamDocsURL          string                       `json:"upstream_docs_url"`
	UpstreamInstallGuideURL  string                       `json:"upstream_install_guide_url"`
	UpstreamUpgradeGuideURL  string                       `json:"upstream_upgrade_guide_url"`
	UpstreamReleasesURL      string                       `json:"upstream_releases_url"`
	DiscoveredClusters       []KubeAgentsClusterCandidate `json:"discovered_clusters,omitempty"`
}

func shortenVersionOrSHA(v string) string {
	s := strings.TrimSpace(v)
	if len(s) >= 16 && isHexHash(s) {
		return s[:7]
	}
	return s
}

func isHexHash(s string) bool {
	if len(s) < 7 {
		return false
	}
	for _, r := range s {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f') || (r >= 'A' && r <= 'F')) {
			return false
		}
	}
	return true
}

func normalizeGitHubRepoURL(repo string) string {
	r := strings.TrimSpace(repo)
	if r == "" {
		return ""
	}
	if strings.HasPrefix(r, "http://") || strings.HasPrefix(r, "https://") {
		return r
	}
	return "https://github.com/" + strings.TrimPrefix(r, "/")
}

func buildLocalKubeAgentsOverviewState(
	input SwarmProjectionInput,
	members map[string]SwarmMember,
	nowISO string,
) *KubeAgentsOverviewState {
	dep := input.Deployment
	depName := firstNonEmptyStr(dep.Name, dep.ClusterName, dep.PlatformAgentName, "kube-agents")
	title := fmt.Sprintf("kube-agents · %s", depName)
	if !input.Connected {
		title = "kube-agents · Not Connected"
	}

	var memberNames []string
	for _, name := range []string{swarmMemberCoordinator, swarmMemberNarrator, swarmMemberPlatform} {
		if _, ok := members[name]; ok {
			memberNames = append(memberNames, name)
		}
	}
	for name := range members {
		if name != swarmMemberCoordinator && name != swarmMemberNarrator && name != swarmMemberPlatform {
			memberNames = append(memberNames, name)
		}
	}

	activeAlias := strings.TrimSpace(input.ActiveConversation)
	activeToken := ""
	rootID := strings.TrimSpace(input.RootConversationID)
	if rootID != "" {
		for alias, c := range input.Conversations {
			if strings.TrimSpace(c.AGYConversationID) == rootID && strings.TrimSpace(c.ParentAGYConversationID) == "" {
				activeAlias = alias
				activeToken = c.Token
				break
			}
		}
	}
	if activeToken == "" && activeAlias != "" {
		if c, ok := input.Conversations[activeAlias]; ok {
			activeToken = c.Token
		}
	}
	if activeAlias == "" {
		activeAlias = defaultConvAlias
		if c, ok := input.Conversations[defaultConvAlias]; ok {
			activeToken = c.Token
		}
	}
	if activeToken != "" && !strings.HasPrefix(activeToken, consoleConvPrefix) {
		activeToken = consoleConvPrefix + activeToken
	}

	natsPort := dep.NATSPort
	if natsPort <= 0 {
		natsPort = DefaultNATSPort
	}
	natsSvc := firstNonEmptyStr(dep.NATSService, firstNonEmptyStr(dep.PlatformAgentName, "platform-agent")+NATSServiceSuffix)
	ns := firstNonEmptyStr(dep.Namespace, "kubeagents-system")
	busUser := firstNonEmptyStr(dep.BusUser, "console")
	busEndpoint := fmt.Sprintf("nats://%s@%s/%s:%d", busUser, ns, natsSvc, natsPort)

	installedVer := strings.TrimSpace(dep.Version)
	latestTag := firstNonEmptyStr(strings.TrimSpace(dep.LatestReleaseTag), defaultFallbackReleaseTag)
	latestURL := firstNonEmptyStr(strings.TrimSpace(dep.LatestReleaseURL), defaultUpstreamReleasesURL)
	latestCommit := strings.TrimSpace(dep.LatestReleaseCommit)
	upgradeAvail := dep.UpgradeAvailable
	if !upgradeAvail && installedVer != "" && latestTag != "" && installedVer != latestTag && strings.TrimPrefix(installedVer, "v") != strings.TrimPrefix(latestTag, "v") {
		if latestCommit == "" || (!strings.HasPrefix(latestCommit, installedVer) && !strings.HasPrefix(installedVer, latestCommit)) {
			upgradeAvail = true
		}
	}

	projFlag := firstNonEmptyStr(dep.ProjectID, "<PROJECT_ID>")
	locFlag := firstNonEmptyStr(dep.Location, "us-central1")
	clusterFlag := firstNonEmptyStr(dep.ClusterName, "platform-agent-host")
	cleanRef := strings.TrimPrefix(latestTag, "v")
	upgradeCmd := "curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/" + cleanRef + "/upgrade.sh -o /tmp/kube-agents-upgrade.sh && bash /tmp/kube-agents-upgrade.sh --plan --non-interactive --gcp-project-id=" + projFlag + " --gcp-region=" + locFlag + " --gke-cluster-name=" + clusterFlag
	installCmd := "curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/" + cleanRef + "/install.sh -o /tmp/kube-agents-install.sh && bash /tmp/kube-agents-install.sh --dry-run --non-interactive --mode=next"

	var candidates []KubeAgentsClusterCandidate
	seenCand := make(map[string]bool)
	addCandidate := func(p DeploymentProfile) {
		cName := firstNonEmptyStr(p.Name, p.ClusterName, p.PlatformAgentName)
		if cName == "" || seenCand[cName] {
			return
		}
		seenCand[cName] = true
		candidates = append(candidates, KubeAgentsClusterCandidate{
			Name:              cName,
			ProjectID:         p.ProjectID,
			Location:          p.Location,
			ClusterName:       firstNonEmptyStr(p.ClusterName, cName),
			KubeContext:       p.KubeContext,
			Namespace:         firstNonEmptyStr(p.Namespace, "kubeagents-system"),
			PlatformAgentName: firstNonEmptyStr(p.PlatformAgentName, "platform-agent"),
			Mode:              firstNonEmptyStr(p.Mode, "next"),
			Version:           shortenVersionOrSHA(p.Version),
			GitHubRepo:        p.GitHubRepo,
			Ready:             p.Ready,
		})
	}
	for _, p := range input.DiscoveredDeployments {
		addCandidate(p)
	}
	if dep.Name != "" || dep.ClusterName != "" || dep.KubeContext != "" {
		addCandidate(dep)
	}

	return &KubeAgentsOverviewState{
		Connected:                input.Connected,
		UpdatedAt:                nowISO,
		RootConversationID:       rootID,
		ProjectID:                dep.ProjectID,
		Location:                 dep.Location,
		ClusterName:              firstNonEmptyStr(dep.ClusterName, dep.Name),
		KubeContext:              dep.KubeContext,
		Namespace:                ns,
		PlatformAgentName:        firstNonEmptyStr(dep.PlatformAgentName, "platform-agent"),
		Mode:                     firstNonEmptyStr(dep.Mode, "next"),
		Ready:                    dep.Ready,
		StatusMessage:            dep.StatusMessage,
		GitHubRepo:               dep.GitHubRepo,
		GitHubRepoURL:            normalizeGitHubRepoURL(dep.GitHubRepo),
		ChatPlatform:             firstNonEmptyStr(dep.ChatPlatform, "console"),
		NATSService:              natsSvc,
		NATSPort:                 natsPort,
		BusUser:                  busUser,
		BusEndpoint:              busEndpoint,
		ActiveConversationAlias:  activeAlias,
		ActiveConsoleToken:       activeToken,
		PushArmed:                input.PushArmed || rootID != "",
		SwarmTitle:               title,
		SwarmMemberNames:         memberNames,
		AgentProfiles:            append([]string(nil), dep.AgentProfiles...),
		ActiveSessionPodCount:    len(dep.ActiveSessionPods),
		ClustersMonitored:        dep.ClustersMonitored,
		ToolExecutionsTotal:      dep.ToolExecutionsTotal,
		RemediationsProposed:     dep.RemediationsProposed,
		RemediationsMerged:       dep.RemediationsMerged,
		InstalledVersion:         installedVer,
		InstalledShortVersion:    shortenVersionOrSHA(installedVer),
		HelmChartVersion:         dep.HelmChartVersion,
		LatestReleaseTag:         latestTag,
		LatestReleaseCommit:      latestCommit,
		LatestReleaseShortCommit: shortenVersionOrSHA(latestCommit),
		LatestReleaseURL:         latestURL,
		LatestReleasePublishedAt: dep.LatestReleasePublishedAt,
		UpgradeAvailable:         upgradeAvail,
		CommitsBehind:            dep.CommitsBehind,
		UpgradeCommand:           upgradeCmd,
		InstallCommand:           installCmd,
		UpstreamRepoURL:          defaultUpstreamRepoURL,
		UpstreamDocsURL:          defaultUpstreamDocsURL,
		UpstreamInstallGuideURL:  defaultUpstreamInstallGuideURL,
		UpstreamUpgradeGuideURL:  defaultUpstreamUpgradeGuideURL,
		UpstreamReleasesURL:      defaultUpstreamReleasesURL,
		DiscoveredClusters:       candidates,
	}
}

func buildLocalSwarmOverview(
	input SwarmProjectionInput,
	members map[string]SwarmMember,
	headSeq int,
	now time.Time,
) SwarmView {
	nowISO := now.UTC().Format(time.RFC3339)
	kaState := buildLocalKubeAgentsOverviewState(input, members, nowISO)

	if !input.Connected {
		var attention []SwarmAttentionItem
		var workstreams []SwarmWorkstream
		var milestones []SwarmTextItem

		if len(kaState.DiscoveredClusters) > 0 {
			for idx, cand := range kaState.DiscoveredClusters {
				connectActions := []SwarmAction{
					{
						Type:   "ka_action",
						Target: "connect:" + cand.Name,
						Label:  fmt.Sprintf("⚡ Connect to %s", cand.Name),
					},
					{
						Type:   "prompt",
						Target: fmt.Sprintf("Connect to kube-agents deployment `%s` and give me a live status overview.", cand.Name),
						Label:  "Ask Agent to Connect",
					},
				}
				attention = append(attention, SwarmAttentionItem{
					ID:       fmt.Sprintf("attn-discovered-%d", idx+1),
					Kind:     "decision",
					Severity: "high",
					Title:    fmt.Sprintf("Discovered kube-agents cluster: %s (%s / %s)", cand.Name, firstNonEmptyStr(cand.ProjectID, "GCP"), firstNonEmptyStr(cand.Location, "GKE")),
					Why:      fmt.Sprintf("PlatformAgent %s/%s is ready in kubeconfig (%s). Click Connect to attach the A2A v2 JetStream bus.", cand.Namespace, cand.PlatformAgentName, firstNonEmptyStr(cand.KubeContext, cand.ClusterName)),
					Refs:     []SwarmRef{{Member: swarmMemberCoordinator}},
					Actions:  connectActions,
				})
			}
		}

		installActions := []SwarmAction{
			{
				Type:   "prompt",
				Target: "Please help me install kube-agents in vNext mode (--mode=next) onto my GKE cluster following the install-kube-agents skill and the latest instructions at https://github.com/gke-labs/kube-agents/, then connect over MCP/A2A and open the Swarm UI.",
				Label:  "🚀 Install kube-agents on GKE",
			},
			{
				Type:   "url",
				Target: defaultUpstreamInstallGuideURL,
				Label:  "INSTALL.md Guide",
			},
			{
				Type:   "url",
				Target: defaultUpstreamDocsURL,
				Label:  "Documentation",
			},
		}
		attention = append(attention, SwarmAttentionItem{
			ID:       "attn-install-kube-agents",
			Kind:     "decision",
			Severity: "medium",
			Title:    "Install kube-agents on a new or existing GKE cluster",
			Why:      "Deploy the Kubernetes Agentic Harness (PlatformAgent CRD, Hermes coordinator, A2A v2 JetStream bus, and GitOps controller) with guided setup.",
			Refs:     []SwarmRef{{Member: swarmMemberCoordinator}},
			Actions:  installActions,
		})

		workstreams = append(workstreams,
			SwarmWorkstream{
				ID:     "ws-install",
				Title:  "Guided GKE Installation & Bootstrap",
				Owners: []string{swarmMemberCoordinator},
				State:  "paused",
				Note:   fmt.Sprintf("Latest release v%s · Runs release-pinned install.sh locally, deploys operator CRDs & PlatformAgent, and connects MCP/A2A.", kaState.LatestReleaseTag),
				Refs:   []SwarmRef{{Member: swarmMemberCoordinator}},
				Actions: []SwarmAction{
					{
						Type:   "prompt",
						Target: fmt.Sprintf("Follow the install-kube-agents skill to download the v%s release `install.sh` locally via `run_command` and run `bash install.sh --dry-run --non-interactive --mode=next` to preview the vNext installation plan.", kaState.LatestReleaseTag),
						Label:  "Preview Install Plan (--dry-run)",
					},
					{
						Type:   "url",
						Target: defaultUpstreamRepoURL,
						Label:  "GitHub Repo",
					},
				},
			},
			SwarmWorkstream{
				ID:     "ws-docs",
				Title:  "Product Architecture & A2A v2 JetStream Fleet",
				Owners: []string{swarmMemberCoordinator, swarmMemberNarrator},
				State:  "active",
				Note:   "Autonomous SRE & Platform Agent on GKE with ephemeral Claude Code /session workers, GitOps PR remediation, and live blackboard topics.",
				Refs:   []SwarmRef{{Member: swarmMemberCoordinator}},
				Actions: []SwarmAction{
					{
						Type:   "url",
						Target: defaultUpstreamDocsURL,
						Label:  "Documentation Site",
					},
					{
						Type:   "url",
						Target: defaultUpstreamReleasesURL,
						Label:  "Release Notes",
					},
				},
			},
		)

		milestones = append(milestones,
			SwarmTextItem{
				ID:   "ms-disconnected-ready",
				Text: "Swarm UI initialized in Not-Connected mode — select a discovered cluster or click Install kube-agents to begin.",
				Refs: []SwarmRef{{Member: swarmMemberCoordinator}},
			},
		)

		headline := "Not connected to a kube-agents deployment — connect to a discovered GKE cluster or launch a guided installation"
		if len(kaState.DiscoveredClusters) > 0 {
			headline = fmt.Sprintf("Not connected — %d kube-agents cluster(s) discovered in ~/.kube/config ready for 1-click connection", len(kaState.DiscoveredClusters))
		}

		return SwarmView{
			Version:     1,
			AsOfSeq:     headSeq,
			UpdatedAt:   nowISO,
			Headline:    headline,
			Phase:       &SwarmPhase{Label: "Not Connected · Ready to Connect or Install", Index: 1, Total: 2},
			Attention:   attention,
			Workstreams: workstreams,
			Milestones:  milestones,
			Risks:       []SwarmTextItem{},
			KubeAgents:  kaState,
		}
	}

	depName := firstNonEmptyStr(kaState.ClusterName, kaState.PlatformAgentName, "kube-agents")
	var attention []SwarmAttentionItem
	if kaState.UpgradeAvailable {
		behindNote := ""
		if kaState.CommitsBehind > 0 {
			behindNote = fmt.Sprintf(" (%d commits behind v%s)", kaState.CommitsBehind, kaState.LatestReleaseTag)
		}
		attention = append(attention, SwarmAttentionItem{
			ID:       "attn-upgrade-available",
			Kind:     "decision",
			Severity: "medium",
			Title:    fmt.Sprintf("Upgrade available: kube-agents v%s (installed: %s%s)", kaState.LatestReleaseTag, firstNonEmptyStr(kaState.InstalledShortVersion, "dev"), behindNote),
			Why:      fmt.Sprintf("Cluster %s can be upgraded to release v%s.", depName, kaState.LatestReleaseTag),
			Refs:     []SwarmRef{{Member: swarmMemberCoordinator}, {Member: swarmMemberPlatform}},
			Actions: []SwarmAction{
				{
					Type:   "prompt",
					Target: fmt.Sprintf("Please upgrade kube-agents on cluster `%s` (project `%s`, location `%s`) to the latest release (`v%s`) following the upgrade-kube-agents skill: run the v%s `upgrade.sh --plan` script locally via `run_command` first, apply the upgrade locally, and reconnect over MCP/A2A.", kaState.ClusterName, kaState.ProjectID, kaState.Location, kaState.LatestReleaseTag, kaState.LatestReleaseTag),
					Label:  fmt.Sprintf("Upgrade to v%s", kaState.LatestReleaseTag),
				},
				{
					Type:   "url",
					Target: kaState.LatestReleaseURL,
					Label:  fmt.Sprintf("v%s Release Notes", kaState.LatestReleaseTag),
				},
			},
		})
	}

	workstreams := []SwarmWorkstream{
		{
			ID:     "ws-platform",
			Title:  fmt.Sprintf("PlatformAgent (%s/%s)", kaState.Namespace, kaState.PlatformAgentName),
			Owners: []string{swarmMemberPlatform, swarmMemberCoordinator},
			State:  "active",
			Note:   fmt.Sprintf("Connected via %s · Mode: %s · Version: %s", kaState.BusEndpoint, kaState.Mode, firstNonEmptyStr(kaState.InstalledShortVersion, "latest")),
			Refs:   []SwarmRef{{Member: swarmMemberPlatform}},
		},
	}

	milestones := []SwarmTextItem{
		{
			ID:   "ms-bus-connected",
			Text: fmt.Sprintf("Connected to %s (%s/%s) over A2A v2 JetStream (%s)", depName, kaState.ProjectID, kaState.Location, kaState.BusEndpoint),
			Refs: []SwarmRef{{Member: swarmMemberNarrator}, {Member: swarmMemberPlatform}},
		},
	}

	return SwarmView{
		Version:     1,
		AsOfSeq:     headSeq,
		UpdatedAt:   nowISO,
		Headline:    fmt.Sprintf("Connected to %s (%s) · %d swarm members", depName, kaState.Mode, len(members)),
		Phase:       &SwarmPhase{Label: "Connected · A2A v2 JetStream Active", Index: 1, Total: 3},
		Attention:   attention,
		Workstreams: workstreams,
		Milestones:  milestones,
		Risks:       []SwarmTextItem{},
		KubeAgents:  kaState,
	}
}

func enrichRemoteOverviewState(
	resp *RemoteSwarmResponse,
	rootID string,
	localDep DeploymentProfile,
	discovered []DeploymentProfile,
	activeConv string,
	pushArmed bool,
) {
	if resp == nil {
		return
	}
	mergedDep := resp.Deployment
	if mergedDep.Name == "" {
		mergedDep = localDep
	} else {
		if mergedDep.HelmChartVersion == "" {
			mergedDep.HelmChartVersion = localDep.HelmChartVersion
		}
		if mergedDep.LatestReleaseTag == "" {
			mergedDep.LatestReleaseTag = localDep.LatestReleaseTag
			mergedDep.LatestReleaseCommit = localDep.LatestReleaseCommit
			mergedDep.LatestReleaseURL = localDep.LatestReleaseURL
			mergedDep.LatestReleasePublishedAt = localDep.LatestReleasePublishedAt
			mergedDep.UpgradeAvailable = localDep.UpgradeAvailable
			mergedDep.CommitsBehind = localDep.CommitsBehind
			mergedDep.VersionCheckedAt = localDep.VersionCheckedAt
		}
		if mergedDep.ClustersMonitored == 0 {
			mergedDep.ClustersMonitored = localDep.ClustersMonitored
			mergedDep.ToolExecutionsTotal = localDep.ToolExecutionsTotal
			mergedDep.RemediationsProposed = localDep.RemediationsProposed
			mergedDep.RemediationsMerged = localDep.RemediationsMerged
		}
		if mergedDep.ProjectID == "" {
			mergedDep.ProjectID = localDep.ProjectID
		}
		if mergedDep.Location == "" {
			mergedDep.Location = localDep.Location
		}
		if mergedDep.ClusterName == "" {
			mergedDep.ClusterName = localDep.ClusterName
		}
		if mergedDep.KubeContext == "" {
			mergedDep.KubeContext = localDep.KubeContext
		}
		if mergedDep.GitHubRepo == "" {
			mergedDep.GitHubRepo = localDep.GitHubRepo
		}
	}
	resp.Deployment = mergedDep

	nowISO := firstNonEmptyStr(resp.Snapshot.View.UpdatedAt, time.Now().UTC().Format(time.RFC3339))
	input := SwarmProjectionInput{
		RootConversationID:    rootID,
		Deployment:            mergedDep,
		DiscoveredDeployments: discovered,
		Connected:             resp.Connected,
		ActiveConversation:    firstNonEmptyStr(resp.ActiveConversation, activeConv),
		PushArmed:             pushArmed,
		Conversations:         resp.Conversations,
	}
	kaState := buildLocalKubeAgentsOverviewState(input, resp.Snapshot.Members, nowISO)
	resp.Snapshot.View.KubeAgents = kaState

	if resp.Connected && kaState.UpgradeAvailable {
		hasUpgradeAttn := false
		for _, a := range resp.Snapshot.View.Attention {
			if a.ID == "attn-upgrade-available" {
				hasUpgradeAttn = true
				break
			}
		}
		if !hasUpgradeAttn {
			depName := firstNonEmptyStr(kaState.ClusterName, kaState.PlatformAgentName, "kube-agents")
			behindNote := ""
			if kaState.CommitsBehind > 0 {
				behindNote = fmt.Sprintf(" (%d commits behind v%s)", kaState.CommitsBehind, kaState.LatestReleaseTag)
			}
			resp.Snapshot.View.Attention = append(resp.Snapshot.View.Attention, SwarmAttentionItem{
				ID:       "attn-upgrade-available",
				Kind:     "decision",
				Severity: "medium",
				Title:    fmt.Sprintf("Upgrade available: kube-agents v%s (installed: %s%s)", kaState.LatestReleaseTag, firstNonEmptyStr(kaState.InstalledShortVersion, "dev"), behindNote),
				Why:      fmt.Sprintf("Run `%s` to upgrade the operator, CRDs, and PlatformAgent on %s.", kaState.UpgradeCommand, depName),
				Refs:     []SwarmRef{{Member: swarmMemberCoordinator}, {Member: swarmMemberPlatform}},
				Actions: []SwarmAction{
					{
						Type:   "prompt",
						Target: fmt.Sprintf("Please upgrade kube-agents on cluster `%s` (project `%s`, location `%s`) to the latest release (`v%s`) following the upgrade-kube-agents skill: run the v%s `upgrade.sh --plan` script locally via `run_command` first, apply the upgrade locally, and reconnect over MCP/A2A.", kaState.ClusterName, kaState.ProjectID, kaState.Location, kaState.LatestReleaseTag, kaState.LatestReleaseTag),
						Label:  fmt.Sprintf("⬆️ Upgrade to v%s", kaState.LatestReleaseTag),
					},
					{
						Type:   "url",
						Target: kaState.LatestReleaseURL,
						Label:  fmt.Sprintf("v%s Release Notes", kaState.LatestReleaseTag),
					},
				},
			})
		}
	}

	resp.Snapshot.ViewHTML = buildSwarmOverviewHTML(kaState)
}

func buildSwarmOverviewHTML(ka *KubeAgentsOverviewState) string {
	if ka == nil {
		return ""
	}
	var sb strings.Builder
	sb.WriteString(`<style>
.ka-ov {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 14px;
  font-family: var(--font-sans, system-ui, -apple-system, sans-serif);
}
.ka-hero {
  position: relative;
  padding: 14px 16px;
  border-radius: 10px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--primary, #326CE5) 14%, var(--card, #161922)), var(--card, #161922) 70%);
  border: 1px solid color-mix(in srgb, var(--primary, #326CE5) 32%, var(--border, #262936));
  overflow: hidden;
}
.ka-hero.disconnected {
  background: linear-gradient(135deg, color-mix(in srgb, #38bdf8 12%, var(--card, #161922)), var(--card, #161922) 72%);
  border-color: color-mix(in srgb, #38bdf8 30%, var(--border, #262936));
}
.ka-hero-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.ka-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ka-wheel {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, #326CE5, #06b6d4);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-size: 16px;
  box-shadow: 0 2px 8px rgba(50, 108, 229, 0.35);
  flex-shrink: 0;
}
.ka-brand-text h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--foreground, #e8eaf0);
  letter-spacing: -0.01em;
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}
.ka-brand-text p {
  margin: 2px 0 0;
  font-size: 11.5px;
  color: var(--muted-foreground, #8b90a0);
  line-height: 1.4;
}
.ka-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 8px;
  border-radius: 999px;
}
.ka-pill.ok {
  background: rgba(52, 211, 153, 0.14);
  color: #34d399;
  border: 1px solid rgba(52, 211, 153, 0.32);
}
.ka-pill.warn {
  background: rgba(251, 191, 36, 0.14);
  color: #fbbf24;
  border: 1px solid rgba(251, 191, 36, 0.32);
}
.ka-pill.info {
  background: rgba(56, 189, 248, 0.14);
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.32);
}
.ka-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 6px currentColor;
}
.ka-card {
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--card, #161922);
  border: 1px solid var(--border, #262936);
}
.ka-card-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.ka-card-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted-foreground, #8b90a0);
  display: flex;
  align-items: center;
  gap: 6px;
}
.ka-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}
.ka-table tr + tr td {
  border-top: 1px solid color-mix(in srgb, var(--border, #262936) 65%, transparent);
}
.ka-table td {
  padding: 6px 4px;
  vertical-align: top;
  line-height: 1.4;
}
.ka-table td.ka-k {
  width: 36%;
  font-weight: 600;
  color: var(--muted-foreground, #8b90a0);
  padding-right: 10px;
  white-space: nowrap;
}
.ka-table td.ka-v {
  color: var(--foreground, #e8eaf0);
  word-break: break-word;
}
.ka-code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 10.5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--foreground, #e8eaf0) 8%, transparent);
  color: var(--foreground, #e8eaf0);
}
.ka-cmd-box {
  margin-top: 8px;
  padding: 7px 9px;
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid var(--border, #262936);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 10.5px;
  color: #93c5fd;
  overflow-x: auto;
  white-space: nowrap;
}
.ka-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.ka-ver-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid color-mix(in srgb, var(--border, #262936) 75%, transparent);
}
.ka-btn-group {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}
.ka-btn-links {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.ka-btn {
  appearance: none;
  -webkit-appearance: none;
  outline: none;
  font-family: inherit;
  font-size: 11.5px;
  font-weight: 500;
  letter-spacing: -0.005em;
  line-height: 1.2;
  padding: 6px 11px;
  border-radius: 7px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.04);
  color: var(--foreground, #e8eaf0);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
  user-select: none;
  transition: background 140ms ease, border-color 140ms ease, color 140ms ease, box-shadow 140ms ease, transform 80ms ease;
}
.ka-btn svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  stroke: currentColor;
  stroke-width: 1.8;
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: 0.9;
}
.ka-btn:hover {
  background: rgba(255, 255, 255, 0.085);
  border-color: rgba(255, 255, 255, 0.2);
}
.ka-btn:active {
  transform: translateY(0.5px);
}
.ka-btn:focus-visible {
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.45);
}
.ka-btn.primary {
  background: linear-gradient(180deg, #3b82f6 0%, #2563eb 100%);
  border-color: rgba(96, 165, 250, 0.45);
  color: #ffffff;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.18);
}
.ka-btn.primary:hover {
  background: linear-gradient(180deg, #60a5fa 0%, #3b82f6 100%);
  border-color: rgba(147, 197, 253, 0.65);
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.38), inset 0 1px 0 rgba(255, 255, 255, 0.24);
}
.ka-btn.ghost {
  background: transparent;
  border-color: transparent;
  color: var(--muted-foreground, #94a3b8);
  padding: 5px 8px;
  font-size: 11px;
}
.ka-btn.ghost:hover {
  background: rgba(255, 255, 255, 0.055);
  border-color: rgba(255, 255, 255, 0.08);
  color: var(--foreground, #f1f5f9);
}
.ka-btn.danger-subtle:hover {
  background: rgba(239, 68, 68, 0.12);
  border-color: rgba(239, 68, 68, 0.35);
  color: #fca5a5;
}
.ka-btn.is-busy {
  opacity: 0.78;
  pointer-events: none;
}
.ka-ver-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
  gap: 8px;
  margin-bottom: 4px;
}
.ka-ver-box {
  padding: 8px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--foreground, #e8eaf0) 3%, transparent);
  border: 1px solid var(--border, #262936);
}
.ka-ver-lbl {
  font-size: 9.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted-foreground, #8b90a0);
}
.ka-ver-val {
  font-size: 13px;
  font-weight: 700;
  color: var(--foreground, #e8eaf0);
  margin-top: 2px;
  font-family: var(--font-mono, ui-monospace, monospace);
}
.ka-ver-sub {
  font-size: 10px;
  color: var(--muted-foreground, #8b90a0);
  margin-top: 2px;
}
.ka-feat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
  gap: 8px;
  margin-top: 8px;
}
.ka-feat {
  padding: 9px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--foreground, #e8eaf0) 3%, transparent);
  border: 1px solid var(--border, #262936);
}
.ka-feat-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--foreground, #e8eaf0);
  margin-bottom: 3px;
}
.ka-feat-desc {
  font-size: 10.5px;
  color: var(--muted-foreground, #8b90a0);
  line-height: 1.4;
}
.ka-cluster-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--primary, #326CE5) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--primary, #326CE5) 26%, var(--border, #262936));
  margin-top: 6px;
  flex-wrap: wrap;
}
.ka-cluster-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ka-cluster-name {
  font-size: 12px;
  font-weight: 700;
  color: var(--foreground, #e8eaf0);
}
.ka-cluster-sub {
  font-size: 10.5px;
  color: var(--muted-foreground, #8b90a0);
}
</style>`)

	if ka.Connected {
		writeConnectedOverviewHTML(&sb, ka)
	} else {
		writeDisconnectedOverviewHTML(&sb, ka)
	}
	sb.WriteString(`<script>
(function(){
  document.addEventListener('click', function(ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest('[data-action]') : null;
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    var act = btn.getAttribute('data-action');
    var target = btn.getAttribute('data-target') || '';
    if (!act || !target) return;
    if (act === 'prompt' || act === 'ka_action') {
      var orig = btn.innerHTML;
      btn.classList.add('is-busy');
      var busyTxt = act === 'prompt' ? 'Sending to chat…' :
                    target === 'disconnect' ? 'Disconnecting…' :
                    target.indexOf('connect:') === 0 ? 'Connecting…' : 'Refreshing…';
      var lbl = btn.querySelector('.ka-btn-lbl');
      if (lbl) lbl.textContent = busyTxt;
      setTimeout(function() {
        btn.classList.remove('is-busy');
        btn.innerHTML = orig;
      }, 4000);
    }
    try {
      window.parent.postMessage({ type: 'NAVIGATE', action: { type: act, target: target } }, '*');
    } catch (_) {}
  }, true);
})();
</script>`)
	return sb.String()
}

const (
	svgRefresh      = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.4-3.65"/><path d="M13.5 2.5v2.2h-2.2"/></svg>`
	svgDisconnect   = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2.5H4a1.5 1.5 0 0 0-1.5 1.5v8A1.5 1.5 0 0 0 4 13.5h2"/><path d="M10.5 5.25 13.25 8l-2.75 2.75"/><path d="M6.5 8h6.5"/></svg>`
	svgUpgrade      = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 12.5v-9"/><path d="M4.5 7 8 3.5 11.5 7"/><path d="M3.5 13.5h9"/></svg>`
	svgTerminalPlan = `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.75" width="12" height="10.5" rx="1.75"/><path d="m5 6.25 2 1.75-2 1.75"/><path d="M8.5 9.75H11"/></svg>`
	svgExternal     = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.75 3.5H4.25A1.25 1.25 0 0 0 3 4.75v7A1.25 1.25 0 0 0 4.25 13h7a1.25 1.25 0 0 0 1.25-1.25v-2.5"/><path d="M9.5 3h3.5v3.5"/><path d="M13 3 7.5 8.5"/></svg>`
	svgBook         = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 3.75A1.25 1.25 0 0 1 3.75 2.5H7.5v10.75H3.75A1.25 1.25 0 0 1 2.5 12z"/><path d="M13.5 3.75a1.25 1.25 0 0 0-1.25-1.25H8.5v10.75h3.75a1.25 1.25 0 0 0 1.25-1.25z"/></svg>`
	svgCode         = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m5.25 5-3 3 3 3"/><path d="m10.75 5 3 3-3 3"/><path d="m9 3.25-2 9.5"/></svg>`
	svgBolt         = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8.75 2 3.5 9h4l-.75 5L12.5 7h-4z"/></svg>`
	svgChat         = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3.5h10a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H6l-3 2.5v-9.5a1 1 0 0 1 1-1z"/></svg>`
)

func writeConnectedOverviewHTML(sb *strings.Builder, ka *KubeAgentsOverviewState) {
	depName := firstNonEmptyStr(ka.ClusterName, ka.PlatformAgentName, "kube-agents")
	readyLabel := "Ready"
	readyClass := "ok"
	if !ka.Ready {
		readyLabel = "Reconciling"
		readyClass = "warn"
	}

	sb.WriteString(`<div class="ka-ov">`)

	// 1. Hero Header
	sb.WriteString(`<div class="ka-hero"><div class="ka-hero-top">`)
	sb.WriteString(`<div class="ka-brand"><div class="ka-wheel">☸</div><div class="ka-brand-text">`)
	fmt.Fprintf(sb, `<h2>kube-agents · %s <span class="ka-pill %s"><span class="ka-dot"></span>%s</span></h2>`,
		htmlEsc(depName), readyClass, htmlEsc(readyLabel))
	fmt.Fprintf(sb, `<p>Connected over A2A v2 JetStream (<span class="ka-code">%s</span>) · Live bi-directional Swarm bridge</p>`,
		htmlEsc(ka.BusEndpoint))
	sb.WriteString(`</div></div>`)
	sb.WriteString(`<div class="ka-btn-group">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="ka_action" data-target="refresh">%s<span class="ka-btn-lbl">Refresh</span></button>`, svgRefresh)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn danger-subtle" data-action="ka_action" data-target="disconnect">%s<span class="ka-btn-lbl">Disconnect</span></button>`, svgDisconnect)
	sb.WriteString(`</div></div></div>`)

	// 2. Version & Upgrade Status Card (Middle position in Overview tab)
	sb.WriteString(`<div class="ka-card">`)
	sb.WriteString(`<div class="ka-card-hdr">`)
	sb.WriteString(`<div class="ka-card-title">📦 Version &amp; Upgrade Overview</div>`)
	if ka.UpgradeAvailable {
		behindTxt := ""
		if ka.CommitsBehind > 0 {
			behindTxt = fmt.Sprintf(" · %d commits behind", ka.CommitsBehind)
		}
		fmt.Fprintf(sb, `<span class="ka-pill warn"><span class="ka-dot"></span>Upgrade Available (v%s%s)</span>`,
			htmlEsc(ka.LatestReleaseTag), htmlEsc(behindTxt))
	} else {
		fmt.Fprintf(sb, `<span class="ka-pill ok"><span class="ka-dot"></span>Up to Date (v%s)</span>`,
			htmlEsc(ka.LatestReleaseTag))
	}
	sb.WriteString(`</div>`)

	sb.WriteString(`<div class="ka-ver-grid">`)
	sb.WriteString(`<div class="ka-ver-box"><div class="ka-ver-lbl">Installed Image Tag</div>`)
	fmt.Fprintf(sb, `<div class="ka-ver-val">%s</div>`, htmlEsc(firstNonEmptyStr(ka.InstalledShortVersion, "latest")))
	if ka.HelmChartVersion != "" {
		fmt.Fprintf(sb, `<div class="ka-ver-sub">Chart: %s</div>`, htmlEsc(ka.HelmChartVersion))
	} else if ka.InstalledVersion != "" && ka.InstalledVersion != ka.InstalledShortVersion {
		fmt.Fprintf(sb, `<div class="ka-ver-sub" title="%s">SHA: %s…</div>`, htmlEsc(ka.InstalledVersion), htmlEsc(ka.InstalledShortVersion))
	}
	sb.WriteString(`</div>`)

	sb.WriteString(`<div class="ka-ver-box"><div class="ka-ver-lbl">Latest Release</div>`)
	fmt.Fprintf(sb, `<div class="ka-ver-val">v%s</div>`, htmlEsc(ka.LatestReleaseTag))
	if ka.LatestReleaseShortCommit != "" {
		fmt.Fprintf(sb, `<div class="ka-ver-sub">Commit: %s</div>`, htmlEsc(ka.LatestReleaseShortCommit))
	} else {
		sb.WriteString(`<div class="ka-ver-sub">gke-labs/kube-agents</div>`)
	}
	sb.WriteString(`</div>`)

	sb.WriteString(`<div class="ka-ver-box"><div class="ka-ver-lbl">Fleet Activity</div>`)
	if ka.ClustersMonitored > 0 || ka.ToolExecutionsTotal > 0 {
		fmt.Fprintf(sb, `<div class="ka-ver-val">%d clusters</div>`, ka.ClustersMonitored)
		fmt.Fprintf(sb, `<div class="ka-ver-sub">%d tool runs · %d PRs</div>`, ka.ToolExecutionsTotal, ka.RemediationsProposed)
	} else {
		fmt.Fprintf(sb, `<div class="ka-ver-val">%d profiles</div>`, len(ka.AgentProfiles))
		fmt.Fprintf(sb, `<div class="ka-ver-sub">%d active /session pods</div>`, ka.ActiveSessionPodCount)
	}
	sb.WriteString(`</div></div>`)

	sb.WriteString(`<div class="ka-ver-footer">`)
	sb.WriteString(`<div class="ka-btn-group">`)
	cleanRef := strings.TrimPrefix(ka.LatestReleaseTag, "v")
	projVal := firstNonEmptyStr(ka.ProjectID, "<PROJECT_ID>")
	locVal := firstNonEmptyStr(ka.Location, "us-central1")
	clusterVal := firstNonEmptyStr(ka.ClusterName, depName)
	upgradePrompt := "Please upgrade kube-agents on cluster `" + ka.ClusterName + "` (project `" + ka.ProjectID + "`, location `" + ka.Location + "`) to the latest release (`" + cleanRef + "`) following the upgrade-kube-agents skill: download `https://raw.githubusercontent.com/gke-labs/kube-agents/" + cleanRef + "/upgrade.sh` locally via `run_command` (never send to @platform), run `bash /tmp/kube-agents-upgrade.sh --plan --non-interactive --gcp-project-id=" + projVal + " --gcp-region=" + locVal + " --gke-cluster-name=" + clusterVal + "` first, apply the upgrade locally, patch `platform-agent-a2a-gateway` with the MCP gateway image if needed, and reconnect over MCP/A2A."
	planPrompt := "Follow the upgrade-kube-agents skill to preview the upgrade of `" + depName + "` (project `" + projVal + "`, region `" + locVal + "`) to `" + cleanRef + "` locally via `run_command` (`curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/" + cleanRef + "/upgrade.sh -o /tmp/kube-agents-upgrade.sh && bash /tmp/kube-agents-upgrade.sh --plan --non-interactive --gcp-project-id=" + projVal + " --gcp-region=" + locVal + " --gke-cluster-name=" + clusterVal + "`) — do not send this to @platform — and summarize what will change."
	if ka.UpgradeAvailable {
		fmt.Fprintf(sb, `<button type="button" class="ka-btn primary" data-action="prompt" data-target="%s">%s<span class="ka-btn-lbl">Upgrade to v%s</span></button>`,
			htmlEsc(upgradePrompt), svgUpgrade, htmlEsc(ka.LatestReleaseTag))
		fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="prompt" data-target="%s">%s<span class="ka-btn-lbl">Preview Plan</span></button>`,
			htmlEsc(planPrompt), svgTerminalPlan)
	} else {
		fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="prompt" data-target="%s">%s<span class="ka-btn-lbl">Verify v%s</span></button>`,
			htmlEsc(planPrompt), svgRefresh, htmlEsc(ka.LatestReleaseTag))
	}
	sb.WriteString(`</div>`)
	sb.WriteString(`<div class="ka-btn-links">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>Release Notes</span></button>`,
		htmlEsc(ka.LatestReleaseURL), svgExternal)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>Upgrade Guide</span></button>`,
		htmlEsc(ka.UpstreamUpgradeGuideURL), svgBook)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>GitHub</span></button>`,
		htmlEsc(ka.UpstreamRepoURL), svgCode)
	sb.WriteString(`</div></div></div>`)

	// 3. Deployment & A2A JetStream Status Card (matches initial connection summary)
	sb.WriteString(`<div class="ka-card">`)
	sb.WriteString(`<div class="ka-card-hdr">`)
	sb.WriteString(`<div class="ka-card-title">🛰️ Deployment &amp; A2A JetStream Status</div>`)
	fmt.Fprintf(sb, `<span class="ka-pill info">Mode: %s</span>`, htmlEsc(firstNonEmptyStr(ka.Mode, "next")))
	sb.WriteString(`</div>`)

	sb.WriteString(`<table class="ka-table"><tbody>`)
	fmt.Fprintf(sb, `<tr><td class="ka-k">Project / Region</td><td class="ka-v"><span class="ka-code">%s</span> (<span class="ka-code">%s</span>)</td></tr>`,
		htmlEsc(firstNonEmptyStr(ka.ProjectID, "—")), htmlEsc(firstNonEmptyStr(ka.Location, "—")))
	if ka.KubeContext != "" {
		fmt.Fprintf(sb, `<tr><td class="ka-k">GKE Cluster</td><td class="ka-v"><span class="ka-code">%s</span> (context: <span class="ka-code">%s</span>)</td></tr>`,
			htmlEsc(depName), htmlEsc(ka.KubeContext))
	} else {
		fmt.Fprintf(sb, `<tr><td class="ka-k">GKE Cluster</td><td class="ka-v"><span class="ka-code">%s</span></td></tr>`, htmlEsc(depName))
	}

	verBadge := firstNonEmptyStr(ka.InstalledShortVersion, "latest")
	fmt.Fprintf(sb, `<tr><td class="ka-k">Deployment / Mode</td><td class="ka-v"><span class="ka-code">%s/%s</span> · Mode: <span class="ka-code">%s</span> · Version: <span class="ka-code">%s</span> · Status: <strong>%s</strong></td></tr>`,
		htmlEsc(ka.Namespace), htmlEsc(ka.PlatformAgentName), htmlEsc(firstNonEmptyStr(ka.Mode, "next")), htmlEsc(verBadge), htmlEsc(readyLabel))

	if ka.GitHubRepo != "" {
		fmt.Fprintf(sb, `<tr><td class="ka-k">GitOps Repository</td><td class="ka-v"><span class="ka-code">%s</span></td></tr>`, htmlEsc(ka.GitHubRepo))
	}
	fmt.Fprintf(sb, `<tr><td class="ka-k">Bus Endpoint</td><td class="ka-v"><span class="ka-code">%s</span> (in-cluster <span class="ka-code">%s%s</span> MCP HTTP)</td></tr>`,
		htmlEsc(ka.BusEndpoint), htmlEsc(ka.PlatformAgentName), htmlEsc(GatewayServiceSuffix))
	fmt.Fprintf(sb, `<tr><td class="ka-k">Active Conversation</td><td class="ka-v"><span class="ka-code">%s</span> (token: <span class="ka-code">%s</span>)</td></tr>`,
		htmlEsc(ka.ActiveConversationAlias), htmlEsc(firstNonEmptyStr(ka.ActiveConsoleToken, "console:default")))

	pushStatus := "✅ Armed"
	if !ka.PushArmed {
		pushStatus = "⚪ Standby"
	}
	if ka.RootConversationID != "" {
		fmt.Fprintf(sb, `<tr><td class="ka-k">Bi-Directional Push</td><td class="ka-v">%s → Antigravity conversation <span class="ka-code">%s</span></td></tr>`,
			pushStatus, htmlEsc(ka.RootConversationID))
	} else {
		fmt.Fprintf(sb, `<tr><td class="ka-k">Bi-Directional Push</td><td class="ka-v">%s</td></tr>`, pushStatus)
	}

	var memberBadges []string
	for _, m := range ka.SwarmMemberNames {
		memberBadges = append(memberBadges, fmt.Sprintf(`<span class="ka-code">%s</span>`, htmlEsc(m)))
	}
	fmt.Fprintf(sb, `<tr><td class="ka-k">Swarm Fleet</td><td class="ka-v"><strong>%s</strong> (%s)</td></tr>`,
		htmlEsc(ka.SwarmTitle), strings.Join(memberBadges, ", "))
	sb.WriteString(`</tbody></table></div>`)

	sb.WriteString(`</div>`)
}

func writeDisconnectedOverviewHTML(sb *strings.Builder, ka *KubeAgentsOverviewState) {
	sb.WriteString(`<div class="ka-ov">`)

	// 1. Hero Header (Not Connected)
	sb.WriteString(`<div class="ka-hero disconnected"><div class="ka-hero-top">`)
	sb.WriteString(`<div class="ka-brand"><div class="ka-wheel">☸</div><div class="ka-brand-text">`)
	sb.WriteString(`<h2>kube-agents · Kubernetes Agentic Harness <span class="ka-pill info"><span class="ka-dot"></span>Not Connected</span></h2>`)
	sb.WriteString(`<p>Autonomous SRE &amp; Platform Engineering agent fleet on GKE powered by the A2A v2 JetStream message bus</p>`)
	sb.WriteString(`</div></div>`)
	sb.WriteString(`<div class="ka-btn-group">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="ka_action" data-target="refresh">%s<span class="ka-btn-lbl">Scan ~/.kube/config</span></button>`, svgRefresh)
	sb.WriteString(`</div></div></div>`)

	// 2. Discovered Clusters (if any exist in kubeconfig / saved config)
	if len(ka.DiscoveredClusters) > 0 {
		sb.WriteString(`<div class="ka-card">`)
		sb.WriteString(`<div class="ka-card-hdr">`)
		sb.WriteString(`<div class="ka-card-title">🛰️ Discovered kube-agents Clusters in ~/.kube/config</div>`)
		fmt.Fprintf(sb, `<span class="ka-pill ok"><span class="ka-dot"></span>%d Available</span>`, len(ka.DiscoveredClusters))
		sb.WriteString(`</div>`)
		sb.WriteString(`<div style="font-size:11.5px;color:var(--muted-foreground, #8b90a0);margin-bottom:6px;">Connect immediately to an existing <span class="ka-code">PlatformAgent</span> deployment discovered in your Kubernetes contexts:</div>`)
		for _, c := range ka.DiscoveredClusters {
			sb.WriteString(`<div class="ka-cluster-row">`)
			sb.WriteString(`<div class="ka-cluster-meta">`)
			fmt.Fprintf(sb, `<div class="ka-cluster-name">☸ %s <span class="ka-code">%s/%s</span></div>`,
				htmlEsc(c.Name), htmlEsc(c.Namespace), htmlEsc(c.PlatformAgentName))
			fmt.Fprintf(sb, `<div class="ka-cluster-sub">Project: <span class="ka-code">%s</span> · Location: <span class="ka-code">%s</span> · Mode: <span class="ka-code">%s</span> · Version: <span class="ka-code">%s</span></div>`,
				htmlEsc(firstNonEmptyStr(c.ProjectID, "—")),
				htmlEsc(firstNonEmptyStr(c.Location, "—")),
				htmlEsc(firstNonEmptyStr(c.Mode, "next")),
				htmlEsc(firstNonEmptyStr(c.Version, "latest")))
			sb.WriteString(`</div>`)
			sb.WriteString(`<div class="ka-btn-group">`)
			fmt.Fprintf(sb, `<button type="button" class="ka-btn primary" data-action="ka_action" data-target="connect:%s">%s<span class="ka-btn-lbl">Connect to %s</span></button>`,
				htmlEsc(c.Name), svgBolt, htmlEsc(c.Name))
			fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="prompt" data-target="Connect to kube-agents deployment %s and show me the live cluster and A2A bus status.">%s<span class="ka-btn-lbl">Ask Agent</span></button>`,
				htmlEsc(c.Name), svgChat)
			sb.WriteString(`</div></div>`)
		}
		sb.WriteString(`</div>`)
	}

	// 3. Install kube-agents Overview & Interactive Action Card
	sb.WriteString(`<div class="ka-card">`)
	sb.WriteString(`<div class="ka-card-hdr">`)
	sb.WriteString(`<div class="ka-card-title">🚀 Install kube-agents on GKE</div>`)
	fmt.Fprintf(sb, `<span class="ka-pill info">Latest Release: v%s</span>`, htmlEsc(ka.LatestReleaseTag))
	sb.WriteString(`</div>`)
	sb.WriteString(`<div style="font-size:11.5px;color:var(--foreground, #e8eaf0);line-height:1.5;">Deploy <strong>kube-agents</strong> onto a new or existing GKE cluster in your GCP project. The guided installer sets up the <span class="ka-code">kube-agents-operator</span>, <span class="ka-code">PlatformAgent</span> CRD, A2A v2 JetStream message bus, and automatically connects this Swarm UI once ready.</div>`)
	fmt.Fprintf(sb, `<div class="ka-cmd-box">%s</div>`, htmlEsc(ka.InstallCommand))
	sb.WriteString(`<div class="ka-ver-footer">`)
	sb.WriteString(`<div class="ka-btn-group">`)
	cleanInstallRef := strings.TrimPrefix(ka.LatestReleaseTag, "v")
	fmt.Fprintf(sb, `<button type="button" class="ka-btn primary" data-action="prompt" data-target="Please help me install kube-agents in vNext mode (--mode=next) onto a GKE cluster following the install-kube-agents skill and the latest instructions at https://github.com/gke-labs/kube-agents/, then connect over MCP/A2A and open the Swarm UI.">%s<span class="ka-btn-lbl">Install kube-agents</span></button>`, svgBolt)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="prompt" data-target="Follow the install-kube-agents skill to download https://raw.githubusercontent.com/gke-labs/kube-agents/%s/install.sh locally via run_command, run bash /tmp/kube-agents-install.sh --dry-run --non-interactive --mode=next, and walk me through the vNext installation plan and prerequisites.">%s<span class="ka-btn-lbl">Preview Plan</span></button>`, htmlEsc(cleanInstallRef), svgTerminalPlan)
	sb.WriteString(`</div>`)
	sb.WriteString(`<div class="ka-btn-links">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>INSTALL.md</span></button>`, htmlEsc(ka.UpstreamInstallGuideURL), svgBook)
	sb.WriteString(`</div></div></div>`)

	// 4. kube-agents Product Details, Architecture & Documentation Links
	sb.WriteString(`<div class="ka-card">`)
	sb.WriteString(`<div class="ka-card-hdr">`)
	sb.WriteString(`<div class="ka-card-title">☸ About kube-agents · Product Overview &amp; Documentation</div>`)
	sb.WriteString(`<span class="ka-pill ok">Open Source · Apache-2.0</span>`)
	sb.WriteString(`</div>`)
	sb.WriteString(`<div style="font-size:11.5px;color:var(--muted-foreground, #8b90a0);line-height:1.5;"><strong>kube-agents</strong> (<span class="ka-code">gke-labs/kube-agents</span>) is an open-source Kubernetes Agentic Harness that runs an autonomous multi-agent SRE &amp; platform engineering fleet directly inside your GKE cluster.</div>`)

	sb.WriteString(`<div class="ka-feat-grid">`)
	sb.WriteString(`<div class="ka-feat"><div class="ka-feat-title">🧠 Hermes Platform Coordinator</div><div class="ka-feat-desc">Continuously monitors cluster state, investigates anomalies, and coordinates specialist sub-agents over the A2A v2 NATS JetStream bus.</div></div>`)
	sb.WriteString(`<div class="ka-feat"><div class="ka-feat-title">🐚 Ephemeral /session Workers</div><div class="ka-feat-desc">Spawns isolated on-demand Claude Code worker pods (<span class="ka-code">s-&lt;id&gt;</span>) for deep 1-on-1 debugging and interactive pair-programming in Antigravity.</div></div>`)
	sb.WriteString(`<div class="ka-feat"><div class="ka-feat-title">🔧 GitOps PR Remediation</div><div class="ka-feat-desc">Drafts, validates, and opens GitHub Pull Requests for Kubernetes manifest fixes with native side-by-side diff inspection in Antigravity.</div></div>`)
	sb.WriteString(`<div class="ka-feat"><div class="ka-feat-title">📡 Shared Blackboard Topics</div><div class="ka-feat-desc">Publishes live cluster health snapshots (<span class="ka-code">TOPICS-STATE</span>) and incident journals (<span class="ka-code">TOPICS-JOURNAL</span>) with proactive push alerts.</div></div>`)
	sb.WriteString(`</div>`)

	sb.WriteString(`<div class="ka-ver-footer">`)
	sb.WriteString(`<div class="ka-btn-group">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn primary" data-action="url" data-target="%s">%s<span>GitHub Repository</span></button>`, htmlEsc(ka.UpstreamRepoURL), svgCode)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn" data-action="url" data-target="%s">%s<span>Documentation</span></button>`, htmlEsc(ka.UpstreamDocsURL), svgBook)
	sb.WriteString(`</div>`)
	sb.WriteString(`<div class="ka-btn-links">`)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>Install Guide</span></button>`, htmlEsc(ka.UpstreamInstallGuideURL), svgExternal)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>Upgrade Guide</span></button>`, htmlEsc(ka.UpstreamUpgradeGuideURL), svgExternal)
	fmt.Fprintf(sb, `<button type="button" class="ka-btn ghost" data-action="url" data-target="%s">%s<span>Releases (v%s)</span></button>`, htmlEsc(ka.UpstreamReleasesURL), svgExternal, htmlEsc(ka.LatestReleaseTag))
	sb.WriteString(`</div></div></div>`)

	sb.WriteString(`</div>`)
}

func htmlEsc(s string) string {
	r := strings.NewReplacer(
		"&", "&amp;",
		"<", "&lt;",
		">", "&gt;",
		`"`, "&quot;",
		"'", "&#39;",
	)
	return r.Replace(s)
}
