package kube

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"

	// Register client-go auth plugins (gcp, oidc, exec) for kubeconfig authentication.
	_ "k8s.io/client-go/plugin/pkg/client/auth"
)

const (
	contextProbeTimeout      = 10 * time.Second
	credsFetchTimeout        = 10 * time.Second
	consoleAuthField         = "console-password"
	gatewayAuthField         = "gateway-password"
	k8sConditionReady        = "Ready"
	k8sStatusTrue            = "True"
	maxDiscoveryWorkers      = 8
	crdAbsPathPlatformAgents = "/apis/kubeagents.x-k8s.io/v1alpha1/platformagents"
	crdAbsPathAgentProfiles  = "/apis/kubeagents.x-k8s.io/v1alpha1/agentprofiles"
)

type k8sPlatformAgentList struct {
	Items []k8sPlatformAgentItem `json:"items"`
}

type k8sPlatformAgentItem struct {
	Metadata struct {
		Name      string            `json:"name"`
		Namespace string            `json:"namespace"`
		Labels    map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		Mode       string `json:"mode"`
		Version    string `json:"version"`
		Deployment struct {
			Tag string `json:"tag"`
		} `json:"deployment"`
		Harness struct {
			ProjectID   string `json:"projectId"`
			ClusterName string `json:"clusterName"`
			Location    string `json:"location"`
		} `json:"harness"`
		Integration struct {
			GitHub struct {
				GitRepo string `json:"gitRepo"`
			} `json:"github"`
			GoogleChat map[string]any `json:"googleChat"`
			GChat      map[string]any `json:"gchat"`
			Slack      map[string]any `json:"slack"`
			Discord    map[string]any `json:"discord"`
		} `json:"integration"`
	} `json:"spec"`
	Status struct {
		Conditions []struct {
			Type    string `json:"type"`
			Status  string `json:"status"`
			Reason  string `json:"reason"`
			Message string `json:"message"`
		} `json:"conditions"`
		Usage struct {
			ClustersMonitored         int `json:"clustersMonitored"`
			ToolExecutionsTotal       int `json:"toolExecutionsTotal"`
			RemediationsProposedTotal int `json:"remediationsProposedTotal"`
			RemediationsMergedTotal   int `json:"remediationsMergedTotal"`
		} `json:"usage"`
	} `json:"status"`
}

type k8sAgentProfileList struct {
	Items []struct {
		Metadata struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		Spec struct {
			// ClusterRef is set on cluster-scoped specialist profiles only (per the
			// AgentProfile CRD); interactive session profiles leave it empty.
			ClusterRef *struct {
				Cluster   string `json:"cluster"`
				Location  string `json:"location"`
				ProjectID string `json:"projectId"`
			} `json:"clusterRef,omitempty"`
		} `json:"spec"`
	} `json:"items"`
}

func restConfigForKubeContext(kubeCtx string) (*rest.Config, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	overrides := &clientcmd.ConfigOverrides{CurrentContext: strings.TrimSpace(kubeCtx)}
	clientCfg := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loadingRules, overrides)
	return clientCfg.ClientConfig()
}

func listKubeContextsClientGo(trimmedFilter string) ([]string, error) {
	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	rawCfg, err := loadingRules.Load()
	if err != nil {
		if trimmedFilter != "" {
			return []string{trimmedFilter}, nil
		}
		return nil, fmt.Errorf("loading kubeconfig contexts: %w", err)
	}
	lowerFilter := strings.ToLower(trimmedFilter)
	var contexts []string
	for name := range rawCfg.Contexts {
		c := strings.TrimSpace(name)
		if c == "" {
			continue
		}
		if lowerFilter == "" || strings.Contains(strings.ToLower(c), lowerFilter) {
			contexts = append(contexts, c)
		}
	}
	sort.Strings(contexts)
	if len(contexts) == 0 && trimmedFilter != "" {
		contexts = []string{trimmedFilter}
	}
	return contexts, nil
}

// DiscoverDeployments scans Kubernetes contexts (or a single filtered context) in parallel
// for PlatformAgent resources and returns discovered DeploymentProfiles.
func DiscoverDeployments(ctx context.Context, kubeContextFilter string, includeCredentials bool) ([]mcpbridge.DeploymentProfile, error) {
	trimmedFilter := strings.TrimSpace(kubeContextFilter)
	contexts, err := listKubeContextsClientGo(trimmedFilter)
	if err != nil {
		return nil, err
	}
	if len(contexts) == 0 {
		return nil, nil
	}

	type result struct {
		profiles []mcpbridge.DeploymentProfile
	}
	results := make([]result, len(contexts))
	sem := make(chan struct{}, maxDiscoveryWorkers)
	var wg sync.WaitGroup

	for i, kctx := range contexts {
		wg.Add(1)
		go func(idx int, kubeCtx string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			profiles := probeKubeContext(ctx, kubeCtx, includeCredentials)
			results[idx] = result{profiles: profiles}
		}(i, kctx)
	}
	wg.Wait()

	var all []mcpbridge.DeploymentProfile
	nameCounts := make(map[string]int)
	for _, r := range results {
		for _, p := range r.profiles {
			nameCounts[p.Name]++
			all = append(all, p)
		}
	}
	for i := range all {
		if nameCounts[all[i].Name] > 1 && all[i].KubeContext != "" {
			all[i].Name = all[i].KubeContext + "/" + all[i].Name
		}
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].Ready != all[j].Ready {
			return all[i].Ready
		}
		return all[i].Name < all[j].Name
	})
	return all, nil
}

func probeKubeContext(ctx context.Context, kubeCtx string, includeCredentials bool) []mcpbridge.DeploymentProfile {
	probeCtx, cancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer cancel()

	restCfg, err := restConfigForKubeContext(kubeCtx)
	if err != nil {
		return nil
	}
	restCfg.Timeout = contextProbeTimeout
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil
	}
	out, err := clientset.Discovery().RESTClient().Get().AbsPath(crdAbsPathPlatformAgents).DoRaw(probeCtx)
	if err != nil {
		return nil
	}
	profOut, profErr := clientset.Discovery().RESTClient().Get().AbsPath(crdAbsPathAgentProfiles).DoRaw(probeCtx)

	var paList k8sPlatformAgentList
	if err := json.Unmarshal(out, &paList); err != nil || len(paList.Items) == 0 {
		return nil
	}

	profilesByNS := make(map[string][]string)
	if profErr == nil && len(profOut) > 0 {
		var apList k8sAgentProfileList
		if json.Unmarshal(profOut, &apList) == nil {
			for _, item := range apList.Items {
				// Only interactive (non cluster-scoped) profiles are projected as fleet agents;
				// the CRD marks cluster specialists structurally through spec.clusterRef.
				if item.Spec.ClusterRef != nil {
					continue
				}
				profilesByNS[item.Metadata.Namespace] = append(profilesByNS[item.Metadata.Namespace], item.Metadata.Name)
			}
		}
	}

	now := time.Now().UTC()
	var discovered []mcpbridge.DeploymentProfile
	for _, item := range paList.Items {
		ns := item.Metadata.Namespace
		paName := item.Metadata.Name
		alias := item.Spec.Harness.ClusterName
		if alias == "" {
			alias = paName
		}
		ready := false
		statusMsg := ""
		for _, cond := range item.Status.Conditions {
			if cond.Type == k8sConditionReady {
				ready = (cond.Status == k8sStatusTrue)
				statusMsg = cond.Message
				break
			}
		}
		chatPlat := mcpbridge.ChatPlatformConsoleOnly
		switch {
		case len(item.Spec.Integration.GoogleChat) > 0 || len(item.Spec.Integration.GChat) > 0:
			chatPlat = mcpbridge.ChatPlatformGChat
		case len(item.Spec.Integration.Slack) > 0:
			chatPlat = mcpbridge.ChatPlatformSlack
		case len(item.Spec.Integration.Discord) > 0:
			chatPlat = mcpbridge.ChatPlatformDiscord
		}
		version := item.Spec.Version
		if version == "" {
			version = item.Spec.Deployment.Tag
		}
		helmChart := ""
		if item.Metadata.Labels != nil {
			helmChart = strings.TrimSpace(item.Metadata.Labels["helm.sh/chart"])
		}

		prof := mcpbridge.DeploymentProfile{
			Name:                 alias,
			KubeContext:          kubeCtx,
			Namespace:            ns,
			PlatformAgentName:    paName,
			Mode:                 item.Spec.Mode,
			Version:              version,
			HelmChartVersion:     helmChart,
			ProjectID:            item.Spec.Harness.ProjectID,
			ClusterName:          item.Spec.Harness.ClusterName,
			Location:             item.Spec.Harness.Location,
			GitHubRepo:           item.Spec.Integration.GitHub.GitRepo,
			ChatPlatform:         chatPlat,
			NATSService:          paName + mcpbridge.NATSServiceSuffix,
			NATSPort:             mcpbridge.DefaultNATSPort,
			CredsObjectName:      paName + mcpbridge.CredsObjectSuffix,
			BusUser:              mcpbridge.DefaultBusUser,
			Ready:                ready,
			StatusMessage:        statusMsg,
			AgentProfiles:        profilesByNS[ns],
			ClustersMonitored:    item.Status.Usage.ClustersMonitored,
			ToolExecutionsTotal:  item.Status.Usage.ToolExecutionsTotal,
			RemediationsProposed: item.Status.Usage.RemediationsProposedTotal,
			RemediationsMerged:   item.Status.Usage.RemediationsMergedTotal,
			LastDiscoveredAt:     now,
		}
		mcpbridge.EnrichDeploymentVersionStatus(ctx, &prof)
		if includeCredentials {
			_ = PopulateDeploymentCredentials(ctx, &prof)
		}
		discovered = append(discovered, prof)
	}
	return discovered
}

// PopulateDeploymentCredentials fetches the NATS console and gateway credentials
// from the deployment's Kubernetes Secret if not already populated.
func PopulateDeploymentCredentials(ctx context.Context, prof *mcpbridge.DeploymentProfile) error {
	if prof == nil || prof.KubeContext == "" || prof.Namespace == "" {
		return nil
	}
	credsName := prof.CredsObjectName
	if credsName == "" && prof.PlatformAgentName != "" {
		credsName = prof.PlatformAgentName + mcpbridge.CredsObjectSuffix
	}
	if credsName == "" {
		return nil
	}
	fetchCtx, cancel := context.WithTimeout(ctx, credsFetchTimeout)
	defer cancel()

	restCfg, err := restConfigForKubeContext(prof.KubeContext)
	if err != nil {
		return fmt.Errorf("loading kubeconfig for %s: %w", prof.KubeContext, err)
	}
	restCfg.Timeout = credsFetchTimeout
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return fmt.Errorf("creating k8s client for %s: %w", prof.KubeContext, err)
	}
	secret, err := clientset.CoreV1().Secrets(prof.Namespace).Get(fetchCtx, credsName, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("fetching credentials object %s/%s: %w", prof.Namespace, credsName, err)
	}
	if raw, ok := secret.Data[consoleAuthField]; ok {
		prof.BusAuthValue = strings.TrimSpace(string(raw))
	}
	if raw, ok := secret.Data[gatewayAuthField]; ok {
		prof.ObserverAuthValue = strings.TrimSpace(string(raw))
	}
	if prof.BusUser == "" {
		prof.BusUser = mcpbridge.DefaultBusUser
	}
	return nil
}
