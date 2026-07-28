//go:build l2

/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package l2 holds probes that need a real GKE cluster and are therefore never compiled into a
// normal `go test ./...`. The `l2` build tag is what keeps them out: without it this file does not
// exist as far as the toolchain is concerned, so `go vet ./...` and the L0 chain stay hermetic and
// nobody can accidentally point a CI runner at a cluster.
//
// Driven by dev/verify/classify-live-state-l2.sh, which owns the destructive-test guard. The guard
// is duplicated below on purpose -- this file creates and deletes namespaces, and a probe that can
// only be aimed safely by its wrapper is one `go test` away from being aimed at the live install.
package l2

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/livestate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// The context to aim at, from the wrapper. No default: a probe that creates and deletes namespaces
// must never have an opinion about which cluster it lands on when nobody said.
const contextEnv = "KAGE_L2_CONTEXT"

// scratchPrefix is the destructive-test guard, anchored. `platform-agent-host` -- the live install --
// must not match, and a substring test would put it one typo away (LSN-005).
const scratchPrefix = "gke-scratch-"

// alwaysSeen makes no action novel, so the novel-action escalation cannot supply a class change this
// probe would attribute to a namespace label.
type alwaysSeen struct{}

func (alwaysSeen) Seen(string, string, classify.KindRef, string) bool { return true }

// The adapter's one side channel is a controller-runtime logger, and what it emits is evidence: it
// names every kind the denominator could not list. Left unset, controller-runtime discards those
// lines and prints a stack trace instead, so the run reports a number with no way to tell whether
// it is small because the namespace is small or small because this credential cannot see much.
func init() {
	logf.SetLogger(zap.New(zap.UseDevMode(true)))
}

func connect(t *testing.T) (client.Client, discovery.ServerResourcesInterface, string) {
	t.Helper()
	kubeContext := os.Getenv(contextEnv)
	if kubeContext == "" {
		t.Fatalf("%s is unset. Run this through dev/verify/classify-live-state-l2.sh, which sets it "+
			"and enforces the scratch-cluster guard.", contextEnv)
	}
	if !strings.HasPrefix(kubeContext, scratchPrefix) {
		t.Fatalf("REFUSING: context %q is not a scratch cluster. This probe creates and deletes "+
			"namespaces; %q is the only sanctioned prefix.", kubeContext, scratchPrefix)
	}

	cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		clientcmd.NewDefaultClientConfigLoadingRules(),
		&clientcmd.ConfigOverrides{CurrentContext: kubeContext},
	).ClientConfig()
	if err != nil {
		t.Fatalf("building a client for context %q: %v", kubeContext, err)
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}
	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	disco, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		t.Fatalf("new discovery client: %v", err)
	}
	return k8s, disco, kubeContext
}

// probeNamespace creates a namespace whose name comes from the API server, so two concurrent runs
// cannot collide, and deletes it on the way out.
func probeNamespace(t *testing.T, ctx context.Context, k8s client.Client, labels map[string]string) *corev1.Namespace {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		GenerateName: "kage-gat022-",
		Labels:       labels,
	}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create probe namespace: %v", err)
	}
	t.Cleanup(func() {
		_ = k8s.Delete(context.Background(), ns, client.PropagationPolicy(metav1.DeletePropagationBackground))
	})
	return ns
}

// V-GAT-022, at L2 (09 §6: L2, weight 10, negative control mandatory).
//
// # Why this needs a real cluster and the envtest suite is not enough
//
// internal/controller/live_state_envtest_test.go asserts the same property against an envtest API
// server, and it is a genuine L1 result. It is not this one, and 09's level column is not a
// formality. Three things differ here and each of them is a way the property could hold at L1 and
// fail in production:
//
//   - The DISCOVERY SURFACE. envtest serves a bare API server and this project's CRDs -- around
//     forty namespaced kinds. GKE serves those plus its own: the managed CRDs, the metrics
//     aggregation layer, whatever the customer installed. countableKinds runs over that list, one
//     List per kind, and this is the first place the denominator is computed over a realistic one.
//   - RBAC. envtest hands out cluster-admin. A real cluster does not, and CountWorkloadObjects'
//     whole skip-do-not-refuse design exists for the kinds a narrow tier is denied. The run below
//     reports how many kinds were skipped so that number is on the record rather than assumed zero.
//   - ADMISSION AND DEFAULTING. A namespace created here passes through PSA, GKE's own webhooks and
//     whatever else is installed; its final label set is not the one this test asked for. The
//     classifier reads what the cluster ended up with, which is the only version that matters.
//
// # The experiment
//
// One envelope, four cluster states, and the class is the only thing allowed to move:
//
//	1  unlabelled namespace, payload CLAIMS production   -> baseline
//	2  unlabelled namespace, payload claims nothing      -> MUST equal baseline   (negative control)
//	3  namespace labelled production, same envelope      -> MUST be above baseline
//	4  target object labelled staging, same envelope     -> MUST drop below (3)
//
// Step 2 is the mandatory negative control and the reason the check exists. A classifier that read
// the payload would pass step 3 perfectly -- the label is present in both places -- and would be
// worthless: the environment of a target is a property of the cluster, and an agent that can assert
// it in its own request can choose its own risk class.
func TestGAT022ClassificationReadsLiveStateNotThePayload(t *testing.T) {
	k8s, disco, kubeContext := connect(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	caller := classify.Caller{
		Name: "cluster-admin-agent",
		Tier: string(agentv1alpha1.TierClusterAdmin),
		Scope: scope.Scope{
			ProjectID:   envOr("KAGE_L2_PROJECT", "adamparco-kage"),
			ClusterName: kubeContext,
		},
	}
	cls, err := classify.New(nil, alwaysSeen{})
	if err != nil {
		t.Fatalf("classify.New: %v", err)
	}

	ns := probeNamespace(t, ctx, k8s, nil)
	t.Logf("probe namespace: %s (context %s)", ns.Name, kubeContext)

	claimed := []classify.RawOp{{
		Verb:      "apply",
		Kind:      classify.KindRef{Kind: "ConfigMap"},
		Namespace: ns.Name,
		Name:      "app-config",
		Payload: map[string]any{
			"metadata": map[string]any{
				"name":      "app-config",
				"namespace": ns.Name,
				"labels":    map[string]any{classify.LabelEnvironment: "production"},
			},
			"data": map[string]any{"log_level": "info"},
		},
	}}
	unclaimed := []classify.RawOp{{
		Verb:      claimed[0].Verb,
		Kind:      claimed[0].Kind,
		Namespace: claimed[0].Namespace,
		Name:      claimed[0].Name,
		Payload: map[string]any{
			"metadata": map[string]any{"name": "app-config", "namespace": ns.Name},
			"data":     map[string]any{"log_level": "info"},
		},
	}}

	classifyOps := func(t *testing.T, ops []classify.RawOp) classify.Class {
		t.Helper()
		// A fresh adapter each time. Its denominator and digest caches are bounded at 60s, which is
		// longer than the gap between these steps, so a reused one would answer step 3 from a
		// snapshot taken before the label was applied.
		live := &livestate.Source{Client: k8s, Discovery: disco}
		resolved, err := classify.Resolve(ctx, live, caller, ops)
		if err != nil {
			t.Fatalf("Resolve against %s: %v", kubeContext, err)
		}
		out, err := cls.Classify(&classify.Input{Caller: caller, Operations: resolved, UndoPlanPresent: true})
		if err != nil {
			t.Fatalf("Classify: %v", err)
		}
		return out.Class
	}

	// --- 1. baseline ----------------------------------------------------------------------------
	baseline := classifyOps(t, claimed)
	t.Logf("step 1: unlabelled namespace, payload claims production -> %s", baseline)
	if baseline >= classify.ClassGated {
		t.Fatalf("baseline is already %s against an unlabelled namespace; nothing below can show a rise. "+
			"Either the code floor moved or this cluster labels new namespaces production by default.", baseline)
	}

	// --- 2. NEGATIVE CONTROL: the payload's claim changes nothing --------------------------------
	if got := classifyOps(t, unclaimed); got != baseline {
		t.Fatalf("NEGATIVE CONTROL FAILED: removing the production label from the PAYLOAD changed the "+
			"class from %s to %s. The classifier is reading the caller's own assertion about the "+
			"target's environment, which the caller writes.", baseline, got)
	}
	t.Logf("step 2: negative control -- payload claim removed, class unchanged at %s", baseline)

	// --- 3. the cluster gains the label ----------------------------------------------------------
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ns), ns); err != nil {
		t.Fatalf("re-read namespace: %v", err)
	}
	if ns.Labels == nil {
		ns.Labels = map[string]string{}
	}
	ns.Labels[classify.LabelEnvironment] = "production"
	if err := k8s.Update(ctx, ns); err != nil {
		t.Fatalf("label namespace production: %v", err)
	}
	labelled := classifyOps(t, claimed)
	t.Logf("step 3: namespace labelled production -> %s", labelled)
	if labelled <= baseline {
		t.Fatalf("the class did not rise when the namespace became production: %s -> %s. "+
			"A byte-identical envelope must classify differently once the target namespace gains its "+
			"production label.", baseline, labelled)
	}

	// --- 4. the object rung overrides the namespace rung ------------------------------------------
	target := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name: "app-config", Namespace: ns.Name,
			Labels: map[string]string{classify.LabelEnvironment: "staging"},
		},
		Data: map[string]string{"log_level": "info"},
	}
	if err := k8s.Create(ctx, target); err != nil {
		t.Fatalf("create the staging-labelled target: %v", err)
	}
	carved := classifyOps(t, claimed)
	t.Logf("step 4: target object labelled staging inside the production namespace -> %s", carved)
	if carved >= labelled {
		t.Fatalf("an object labelled staging inside a production namespace classified %s, no lower "+
			"than the production answer %s: the object rung of 06 §4.2's ladder is not overriding the "+
			"namespace rung, so a staging carve-out cannot be made.", carved, labelled)
	}
}

// The denominator over a real cluster's discovery surface, reported rather than merely asserted.
//
// The assertion is deliberately weak -- a namespaced count must be establishable and must not
// silently be the whole cluster -- because the interesting output is the log line. "How many kinds
// does this cluster serve, and how many of them can this credential list" is the number
// CountWorkloadObjects' skip-do-not-refuse design is built around, and it has never been measured
// against anything but a bare envtest server.
func TestGAT022DenominatorOverARealDiscoverySurface(t *testing.T) {
	k8s, disco, kubeContext := connect(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	groups, discErr := disco.ServerPreferredNamespacedResources()
	kinds := 0
	for _, g := range groups {
		kinds += len(g.APIResources)
	}
	t.Logf("%s serves %d namespaced resource entries across %d groups (discovery error: %v)",
		kubeContext, kinds, len(groups), discErr)
	if len(groups) == 0 {
		t.Fatalf("discovery returned nothing; the denominator cannot be established: %v", discErr)
	}

	ns := probeNamespace(t, ctx, k8s, nil)
	s := scope.Scope{
		ProjectID:   envOr("KAGE_L2_PROJECT", "adamparco-kage"),
		ClusterName: kubeContext,
		Namespace:   ns.Name,
	}

	live := &livestate.Source{Client: k8s, Discovery: disco}
	before, err := live.CountWorkloadObjects(ctx, s)
	if err != nil {
		t.Fatalf("CountWorkloadObjects over %s: %v", kubeContext, err)
	}
	t.Logf("denominator for a fresh namespace on a real cluster: %d", before)

	for i := 0; i < 3; i++ {
		cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("denominator-probe-%d", i), Namespace: ns.Name,
		}}
		if err := k8s.Create(ctx, cm); err != nil {
			t.Fatalf("create probe ConfigMap %d: %v", i, err)
		}
	}
	after, err := (&livestate.Source{Client: k8s, Discovery: disco}).CountWorkloadObjects(ctx, s)
	if err != nil {
		t.Fatalf("CountWorkloadObjects after: %v", err)
	}
	t.Logf("denominator after 3 unowned ConfigMaps: %d", after)
	if after-before != 3 {
		t.Fatalf("the denominator moved by %d after creating 3 unowned ConfigMaps in %s; want 3 "+
			"(before=%d after=%d). Either the List is not scoped to the namespace or ConfigMap is "+
			"being skipped.", after-before, ns.Name, before, after)
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
