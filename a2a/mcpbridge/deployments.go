package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Deployment naming and defaults shared by the bridge core and the Kubernetes
// adapter in package kube.
const (
	// DefaultNATSPort is the in-cluster NATS client port of a kube-agents deployment.
	DefaultNATSPort = 4222
	// DefaultMCPPort is the a2a-gateway MCP HTTP listener port.
	DefaultMCPPort = 8089
	// DefaultBusUser is the NATS user the workstation bridge authenticates as.
	DefaultBusUser = "console"
	// NATSServiceSuffix is appended to the PlatformAgent name to form its NATS Service.
	NATSServiceSuffix = "-a2a-nats"
	// GatewayServiceSuffix is appended to the PlatformAgent name to form its gateway Service.
	GatewayServiceSuffix = "-a2a-gateway"
	// CredsObjectSuffix is appended to the PlatformAgent name to form its credentials Secret.
	CredsObjectSuffix = "-a2a-nats-creds"

	// ChatPlatformGChat and friends name the chat integration a deployment is wired to.
	ChatPlatformGChat       = "gchat"
	ChatPlatformSlack       = "slack"
	ChatPlatformDiscord     = "discord"
	ChatPlatformConsoleOnly = "console"

	badgeActive   = "🟢 **Active**"
	badgeStandby  = "⚪ Standby"
	badgeReady    = "✅ Ready"
	badgeNotReady = "⚠️ Not Ready"
)

// FormatDeploymentsMarkdown renders discovered/saved DeploymentProfiles as a rich Markdown table.
func FormatDeploymentsMarkdown(profiles []DeploymentProfile, activeName string) string {
	if len(profiles) == 0 {
		return "No `kube-agents` deployments found in `~/.kube/config` or saved configuration.\n"
	}
	var sb strings.Builder
	sb.WriteString("## 🛰️ Kube-Agents Deployments\n\n")
	sb.WriteString("| Status | Name | Ready | Mode | Cluster / Location | Namespace | GitHub Repo | Chat | Profiles | Kube Context |\n")
	sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
	for _, p := range profiles {
		activeCol := badgeStandby
		if p.Name == activeName {
			activeCol = badgeActive
		}
		readyCol := badgeNotReady
		if p.Ready {
			readyCol = badgeReady
		}
		clusterLoc := p.ClusterName
		if p.Location != "" {
			clusterLoc = fmt.Sprintf("%s (`%s`)", firstNonEmptyStr(p.ClusterName, p.PlatformAgentName), p.Location)
		}
		profs := "—"
		if len(p.AgentProfiles) > 0 {
			profs = "`" + strings.Join(p.AgentProfiles, "`, `") + "`"
		}
		fmt.Fprintf(&sb, "| %s | **`%s`** | %s | `%s` | %s | `%s` | `%s` | `%s` | %s | `%s` |\n",
			activeCol,
			p.Name,
			readyCol,
			firstNonEmptyStr(p.Mode, "next"),
			firstNonEmptyStr(clusterLoc, "—"),
			p.Namespace,
			firstNonEmptyStr(p.GitHubRepo, "—"),
			firstNonEmptyStr(p.ChatPlatform, "console"),
			profs,
			firstNonEmptyStr(p.KubeContext, p.DirectNATSURL),
		)
	}
	return sb.String()
}

type cachedReleaseInfo struct {
	tag          string
	commit       string
	htmlURL      string
	publishedAt  string
	fetchedAt    time.Time
	compareCache map[string]int
}

var (
	releaseCacheMu sync.Mutex
	releaseCache   cachedReleaseInfo
)

const (
	githubLatestReleaseAPI = "https://api.github.com/repos/gke-labs/kube-agents/releases/latest"
	githubCompareAPIFormat = "https://api.github.com/repos/gke-labs/kube-agents/compare/%s...%s"
	releaseCacheTTL        = 10 * time.Minute
	githubAPITimeout       = 3500 * time.Millisecond
)

// EnrichDeploymentVersionStatus queries the upstream gke-labs/kube-agents GitHub release API
// (with a 10-minute in-memory cache) and compares the cluster's installed version/tag against
// the latest upstream release tag to populate UpgradeAvailable and CommitsBehind.
func EnrichDeploymentVersionStatus(ctx context.Context, prof *DeploymentProfile) {
	if prof == nil {
		return
	}
	now := time.Now().UTC()
	installed := strings.TrimSpace(prof.Version)
	if installed != "" && prof.LatestReleaseTag != "" && !prof.VersionCheckedAt.IsZero() && now.Sub(prof.VersionCheckedAt) < releaseCacheTTL {
		return
	}

	tag, commit, htmlURL, publishedAt := fetchLatestUpstreamRelease(ctx)
	if tag == "" {
		if prof.LatestReleaseTag == "" {
			prof.LatestReleaseTag = defaultFallbackReleaseTag
			prof.LatestReleaseURL = defaultUpstreamReleasesURL
		}
		if installed != "" && installed != prof.LatestReleaseTag && strings.TrimPrefix(installed, "v") != strings.TrimPrefix(prof.LatestReleaseTag, "v") {
			prof.UpgradeAvailable = true
		}
		return
	}

	prof.LatestReleaseTag = tag
	prof.LatestReleaseCommit = commit
	prof.LatestReleaseURL = firstNonEmptyStr(htmlURL, defaultUpstreamReleasesURL)
	prof.LatestReleasePublishedAt = publishedAt
	prof.VersionCheckedAt = now

	cleanInst := strings.TrimPrefix(installed, "v")
	cleanTag := strings.TrimPrefix(tag, "v")
	if installed == "" {
		prof.UpgradeAvailable = false
		prof.CommitsBehind = 0
		return
	}
	if cleanInst == cleanTag || (commit != "" && strings.HasPrefix(commit, installed)) || (commit != "" && strings.HasPrefix(installed, commit)) {
		prof.UpgradeAvailable = false
		prof.CommitsBehind = 0
		return
	}
	prof.UpgradeAvailable = true
	if ahead := fetchCommitsBehind(ctx, installed, tag); ahead > 0 {
		prof.CommitsBehind = ahead
	}
}

func fetchLatestUpstreamRelease(ctx context.Context) (tag, commit, htmlURL, publishedAt string) {
	releaseCacheMu.Lock()
	if releaseCache.tag != "" && time.Since(releaseCache.fetchedAt) < releaseCacheTTL {
		t, c, u, p := releaseCache.tag, releaseCache.commit, releaseCache.htmlURL, releaseCache.publishedAt
		releaseCacheMu.Unlock()
		return t, c, u, p
	}
	releaseCacheMu.Unlock()

	reqCtx, cancel := context.WithTimeout(ctx, githubAPITimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, githubLatestReleaseAPI, nil)
	if err != nil {
		return "", "", "", ""
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "kube-agents-mcp/1.0")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", "", "", ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", "", "", ""
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return "", "", "", ""
	}
	var payload struct {
		TagName         string `json:"tag_name"`
		TargetCommitish string `json:"target_commitish"`
		HTMLURL         string `json:"html_url"`
		PublishedAt     string `json:"published_at"`
	}
	if json.Unmarshal(body, &payload) != nil || strings.TrimSpace(payload.TagName) == "" {
		return "", "", "", ""
	}
	tag = strings.TrimSpace(payload.TagName)
	commit = strings.TrimSpace(payload.TargetCommitish)
	htmlURL = strings.TrimSpace(payload.HTMLURL)
	publishedAt = strings.TrimSpace(payload.PublishedAt)

	releaseCacheMu.Lock()
	releaseCache.tag = tag
	releaseCache.commit = commit
	releaseCache.htmlURL = htmlURL
	releaseCache.publishedAt = publishedAt
	releaseCache.fetchedAt = time.Now()
	if releaseCache.compareCache == nil {
		releaseCache.compareCache = make(map[string]int)
	}
	releaseCacheMu.Unlock()
	return tag, commit, htmlURL, publishedAt
}

func fetchCommitsBehind(ctx context.Context, installedRef, latestTag string) int {
	key := installedRef + "..." + latestTag
	releaseCacheMu.Lock()
	if releaseCache.compareCache != nil {
		if v, ok := releaseCache.compareCache[key]; ok {
			releaseCacheMu.Unlock()
			return v
		}
	}
	releaseCacheMu.Unlock()

	reqCtx, cancel := context.WithTimeout(ctx, githubAPITimeout)
	defer cancel()
	compareURL := fmt.Sprintf(githubCompareAPIFormat, url.PathEscape(installedRef), url.PathEscape(latestTag))
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, compareURL, nil)
	if err != nil {
		return 0
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "kube-agents-mcp/1.0")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return 0
	}
	var payload struct {
		AheadBy int `json:"ahead_by"`
	}
	if json.Unmarshal(body, &payload) != nil || payload.AheadBy <= 0 {
		return 0
	}
	releaseCacheMu.Lock()
	if releaseCache.compareCache == nil {
		releaseCache.compareCache = make(map[string]int)
	}
	releaseCache.compareCache[key] = payload.AheadBy
	releaseCacheMu.Unlock()
	return payload.AheadBy
}
