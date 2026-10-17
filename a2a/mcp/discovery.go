package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os/exec"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	kubectlBin               = "kubectl"
	contextListTimeout       = 5 * time.Second
	contextProbeTimeout      = 10 * time.Second
	credsFetchTimeout        = 10 * time.Second
	portForwardReadyTimeout  = 15 * time.Second
	portForwardRetryDelay    = 1 * time.Second
	tcpDialCheckTimeout      = 500 * time.Millisecond
	defaultNATSPort          = 4222
	defaultBusUser           = "console"
	natsServiceSuffix        = "-a2a-nats"
	credsObjectSuffix        = "-a2a-nats-creds"
	consoleAuthField         = "console-password"
	gatewayAuthField         = "gateway-password"
	k8sResourcePlatformAgent = "platformagents.kubeagents.x-k8s.io"
	k8sResourceAgentProfile  = "agentprofiles.kubeagents.x-k8s.io"
	k8sResourceSecret        = "secret"
	k8sConditionReady        = "Ready"
	k8sStatusTrue            = "True"
	loopbackHost             = "127.0.0.1"
	natsSchemePrefix         = "nats://"
	maxDiscoveryWorkers      = 8
	chatPlatformGChat        = "gchat"
	chatPlatformSlack        = "slack"
	chatPlatformDiscord      = "discord"
	chatPlatformConsoleOnly  = "console"
	portForwardRegexPattern  = `Forwarding from 127\.0\.0\.1:(\d+)`
	tcpNetwork               = "tcp"
	ephemeralPortSuffix      = ":0"
	kubectlCmdConfig         = "config"
	kubectlCmdGetContexts    = "get-contexts"
	kubectlCmdGet            = "get"
	kubectlCmdPortForward    = "port-forward"
	kubectlFlagOutput        = "-o"
	kubectlOutName           = "name"
	kubectlOutJSON           = "json"
	kubectlFlagAllNamespaces = "-A"
	kubectlFlagNamespace     = "-n"
	kubectlFlagContextPrefix = "--context="
	kubectlSvcPrefix         = "svc/"
)

var (
	portForwardPortRe      = regexp.MustCompile(portForwardRegexPattern)
	errPortForwardTimedOut = errors.New("timed out waiting for kubectl port-forward to bind local port")
)

type k8sPlatformAgentList struct {
	Items []k8sPlatformAgentItem `json:"items"`
}

type k8sPlatformAgentItem struct {
	Metadata struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
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
	} `json:"status"`
}

type k8sAgentProfileList struct {
	Items []struct {
		Metadata struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
	} `json:"items"`
}

type k8sCredsResource struct {
	Data map[string]string `json:"data"`
}

// CommandRunner abstracts external command execution for testing.
type CommandRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

// DefaultCommandRunner executes a command with process-group termination on context cancellation.
func DefaultCommandRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = childProcAttr()
	cmd.Cancel = func() error {
		if cmd.Process != nil {
			return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		return nil
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		errText := strings.TrimSpace(stderr.String())
		if errText != "" {
			return nil, fmt.Errorf("%s %s: %w (%s)", name, strings.Join(args, " "), err, errText)
		}
		return nil, fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return stdout.Bytes(), nil
}

// DiscoverDeployments scans Kubernetes contexts (or a single filtered context) in parallel
// for PlatformAgent resources and returns discovered DeploymentProfiles.
func DiscoverDeployments(ctx context.Context, runner CommandRunner, kubeContextFilter string, includeCredentials bool) ([]DeploymentProfile, error) {
	if runner == nil {
		runner = DefaultCommandRunner
	}
	var contexts []string
	trimmedFilter := strings.TrimSpace(kubeContextFilter)
	listCtx, cancel := context.WithTimeout(ctx, contextListTimeout)
	out, err := runner(listCtx, kubectlBin, kubectlCmdConfig, kubectlCmdGetContexts, kubectlFlagOutput, kubectlOutName)
	cancel()
	if err != nil {
		if trimmedFilter != "" {
			contexts = []string{trimmedFilter}
		} else {
			return nil, fmt.Errorf("listing kubectl contexts: %w", err)
		}
	} else {
		lowerFilter := strings.ToLower(trimmedFilter)
		for _, line := range strings.Split(string(out), "\n") {
			c := strings.TrimSpace(line)
			if c == "" {
				continue
			}
			if lowerFilter == "" || strings.Contains(strings.ToLower(c), lowerFilter) {
				contexts = append(contexts, c)
			}
		}
		if len(contexts) == 0 && trimmedFilter != "" {
			contexts = []string{trimmedFilter}
		}
	}

	if len(contexts) == 0 {
		return nil, nil
	}

	type result struct {
		profiles []DeploymentProfile
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

			profiles := probeKubeContext(ctx, runner, kubeCtx, includeCredentials)
			results[idx] = result{profiles: profiles}
		}(i, kctx)
	}
	wg.Wait()

	var all []DeploymentProfile
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

func probeKubeContext(ctx context.Context, runner CommandRunner, kubeCtx string, includeCredentials bool) []DeploymentProfile {
	probeCtx, cancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer cancel()

	out, err := runner(probeCtx, kubectlBin, kubectlFlagContextPrefix+kubeCtx, kubectlCmdGet, k8sResourcePlatformAgent, kubectlFlagAllNamespaces, kubectlFlagOutput, kubectlOutJSON)
	if err != nil {
		return nil
	}
	var paList k8sPlatformAgentList
	if err := json.Unmarshal(out, &paList); err != nil || len(paList.Items) == 0 {
		return nil
	}

	profilesByNS := make(map[string][]string)
	if profOut, profErr := runner(probeCtx, kubectlBin, kubectlFlagContextPrefix+kubeCtx, kubectlCmdGet, k8sResourceAgentProfile, kubectlFlagAllNamespaces, kubectlFlagOutput, kubectlOutJSON); profErr == nil {
		var apList k8sAgentProfileList
		if json.Unmarshal(profOut, &apList) == nil {
			for _, item := range apList.Items {
				profilesByNS[item.Metadata.Namespace] = append(profilesByNS[item.Metadata.Namespace], item.Metadata.Name)
			}
		}
	}

	now := time.Now().UTC()
	var discovered []DeploymentProfile
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
		chatPlat := chatPlatformConsoleOnly
		switch {
		case len(item.Spec.Integration.GoogleChat) > 0 || len(item.Spec.Integration.GChat) > 0:
			chatPlat = chatPlatformGChat
		case len(item.Spec.Integration.Slack) > 0:
			chatPlat = chatPlatformSlack
		case len(item.Spec.Integration.Discord) > 0:
			chatPlat = chatPlatformDiscord
		}
		version := item.Spec.Version
		if version == "" {
			version = item.Spec.Deployment.Tag
		}

		prof := DeploymentProfile{
			Name:              alias,
			KubeContext:       kubeCtx,
			Namespace:         ns,
			PlatformAgentName: paName,
			Mode:              item.Spec.Mode,
			Version:           version,
			ProjectID:         item.Spec.Harness.ProjectID,
			ClusterName:       item.Spec.Harness.ClusterName,
			Location:          item.Spec.Harness.Location,
			GitHubRepo:        item.Spec.Integration.GitHub.GitRepo,
			ChatPlatform:      chatPlat,
			NATSService:       paName + natsServiceSuffix,
			NATSPort:          defaultNATSPort,
			CredsObjectName:   paName + credsObjectSuffix,
			BusUser:           defaultBusUser,
			Ready:             ready,
			StatusMessage:     statusMsg,
			AgentProfiles:     profilesByNS[ns],
			LastDiscoveredAt:  now,
		}
		if includeCredentials {
			_ = PopulateDeploymentCredentials(ctx, runner, &prof)
		}
		discovered = append(discovered, prof)
	}
	return discovered
}

// PopulateDeploymentCredentials fetches the NATS console and gateway credentials
// from the deployment's Kubernetes Secret if not already populated.
func PopulateDeploymentCredentials(ctx context.Context, runner CommandRunner, prof *DeploymentProfile) error {
	if prof == nil || prof.KubeContext == "" || prof.Namespace == "" {
		return nil
	}
	if runner == nil {
		runner = DefaultCommandRunner
	}
	credsName := prof.CredsObjectName
	if credsName == "" && prof.PlatformAgentName != "" {
		credsName = prof.PlatformAgentName + credsObjectSuffix
	}
	if credsName == "" {
		return nil
	}
	fetchCtx, cancel := context.WithTimeout(ctx, credsFetchTimeout)
	defer cancel()

	out, err := runner(
		fetchCtx,
		kubectlBin,
		kubectlFlagContextPrefix+prof.KubeContext,
		kubectlFlagNamespace, prof.Namespace,
		kubectlCmdGet, k8sResourceSecret, credsName,
		kubectlFlagOutput, kubectlOutJSON,
	)
	if err != nil {
		return fmt.Errorf("fetching credentials object %s/%s: %w", prof.Namespace, credsName, err)
	}
	var resource k8sCredsResource
	if err := json.Unmarshal(out, &resource); err != nil {
		return fmt.Errorf("parsing credentials object %s/%s: %w", prof.Namespace, credsName, err)
	}
	if b64, ok := resource.Data[consoleAuthField]; ok {
		if raw, err := base64.StdEncoding.DecodeString(b64); err == nil {
			prof.BusAuthValue = strings.TrimSpace(string(raw))
		}
	}
	if b64, ok := resource.Data[gatewayAuthField]; ok {
		if raw, err := base64.StdEncoding.DecodeString(b64); err == nil {
			prof.ObserverAuthValue = strings.TrimSpace(string(raw))
		}
	}
	if prof.BusUser == "" {
		prof.BusUser = defaultBusUser
	}
	return nil
}

// PortForwardTunnel manages a persistent local port-forward to the cluster's NATS service,
// automatically restarting the tunnel on the same local port if the subprocess exits.
type PortForwardTunnel struct {
	kubeContext string
	namespace   string
	service     string
	remotePort  int
	localPort   int

	mu      sync.Mutex
	cancel  context.CancelFunc
	closed  bool
	doneCh  chan struct{}
	readyCh chan struct{}
}

// StartPortForward starts a supervised `kubectl port-forward` to the deployment's NATS service
// and waits until the local TCP port is accepting connections.
func StartPortForward(ctx context.Context, prof DeploymentProfile) (*PortForwardTunnel, error) {
	remotePort := prof.NATSPort
	if remotePort <= 0 {
		remotePort = defaultNATSPort
	}
	svc := prof.NATSService
	if svc == "" {
		svc = prof.PlatformAgentName + natsServiceSuffix
	}
	localPort, err := allocateFreeLocalPort()
	if err != nil {
		return nil, fmt.Errorf("allocating local port for port-forward: %w", err)
	}

	supCtx, supCancel := context.WithCancel(context.Background())
	t := &PortForwardTunnel{
		kubeContext: prof.KubeContext,
		namespace:   prof.Namespace,
		service:     svc,
		remotePort:  remotePort,
		localPort:   localPort,
		cancel:      supCancel,
		doneCh:      make(chan struct{}),
		readyCh:     make(chan struct{}),
	}
	go t.supervise(supCtx)

	waitCtx, waitCancel := context.WithTimeout(ctx, portForwardReadyTimeout)
	defer waitCancel()
	select {
	case <-t.readyCh:
		return t, nil
	case <-waitCtx.Done():
		t.Close()
		return nil, fmt.Errorf("%w (%s/%s)", errPortForwardTimedOut, prof.Namespace, svc)
	}
}

// NATSURL returns the local nats:// URL for the port-forwarded tunnel.
func (t *PortForwardTunnel) NATSURL() string {
	return fmt.Sprintf("%s%s:%d", natsSchemePrefix, loopbackHost, t.localPort)
}

// Close stops the port-forward supervisor and terminates the child kubectl process.
func (t *PortForwardTunnel) Close() {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return
	}
	t.closed = true
	t.cancel()
	t.mu.Unlock()
	<-t.doneCh
}

func (t *PortForwardTunnel) supervise(ctx context.Context) {
	defer close(t.doneCh)
	var readyOnce sync.Once
	for ctx.Err() == nil {
		portMapping := fmt.Sprintf("%d:%d", t.localPort, t.remotePort)
		args := []string{
			kubectlFlagContextPrefix + t.kubeContext,
			kubectlFlagNamespace, t.namespace,
			kubectlCmdPortForward,
			kubectlSvcPrefix + t.service,
			portMapping,
		}
		cmd := exec.CommandContext(ctx, kubectlBin, args...)
		cmd.SysProcAttr = childProcAttr()
		cmd.Cancel = func() error {
			if cmd.Process != nil {
				return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			}
			return nil
		}
		stdoutPipe, err := cmd.StdoutPipe()
		if err != nil {
			time.Sleep(portForwardRetryDelay)
			continue
		}
		cmd.Stderr = io.Discard
		if err := cmd.Start(); err != nil {
			time.Sleep(portForwardRetryDelay)
			continue
		}

		go func(r io.Reader) {
			scanner := bufio.NewScanner(r)
			for scanner.Scan() {
				line := scanner.Text()
				if matches := portForwardPortRe.FindStringSubmatch(line); len(matches) > 1 {
					if p, err := strconv.Atoi(matches[1]); err == nil && p > 0 {
						t.localPort = p
					}
					if waitTCPReady(ctx, t.localPort) {
						readyOnce.Do(func() { close(t.readyCh) })
					}
				}
			}
		}(stdoutPipe)

		_ = cmd.Wait()
		if ctx.Err() != nil {
			return
		}
		time.Sleep(portForwardRetryDelay)
	}
}

func allocateFreeLocalPort() (int, error) {
	ln, err := net.Listen(tcpNetwork, loopbackHost+ephemeralPortSuffix)
	if err != nil {
		return 0, err
	}
	defer func() { _ = ln.Close() }()
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		return 0, errors.New("unexpected listener address type")
	}
	return addr.Port, nil
}

func waitTCPReady(ctx context.Context, port int) bool {
	addr := net.JoinHostPort(loopbackHost, strconv.Itoa(port))
	d := net.Dialer{Timeout: tcpDialCheckTimeout}
	conn, err := d.DialContext(ctx, tcpNetwork, addr)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}
