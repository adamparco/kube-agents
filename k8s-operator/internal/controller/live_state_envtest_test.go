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

package controller_test

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/livestate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// livestate.Source against a real API server (P9-T7c-3a).
//
// live_test.go covers the decisions this adapter makes -- which kinds count, which failures skip
// and which refuse, when a cache entry expires. None of that needs a cluster and none of it is
// asserted again here.
//
// What needs a cluster is the half a stub cannot lie about: that a real API server, asked the way
// this adapter asks, returns what the classifier was built to expect. Three things in particular
// only exist above envtest's line:
//
//   - PartialObjectMetadata is served by a distinct accept header. controller-runtime's fake client
//     does not model it at all, so every GetObject and every denominator List in the hermetic file
//     runs against a hand-written answer. Here they run against apiserver.
//   - Discovery is a live endpoint. countableKinds is exercised in live_test.go over a fixture of
//     four groups; here it runs over whatever this API server actually serves, which is the input
//     shape production sees and the one that grows a kind the day a CRD is installed.
//   - The RESTMapper is real, so `apps/Deploymnet` fails for the reason it fails in production
//     rather than because a stub was told to fail.
//
// The headline claim is V-GAT-022: classification reads LIVE STATE, not the payload. Its negative
// control is mandatory (09 §6) and is the second half of TestLiveStateClassificationFollowsTheCluster.

func startLiveStateEnv(t *testing.T) (client.Client, discovery.ServerResourcesInterface, context.Context) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}
	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	disco, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		t.Fatalf("new discovery client: %v", err)
	}
	return k8s, disco, context.Background()
}

// liveCaller is a cluster-admin agent: scoped to a project and a cluster, so a namespaced target
// resolves to a well-formed scope strictly inside it. A platform caller with no ClusterName would
// make every namespaced target malformed and the ownership lookup would refuse before any of these
// assertions ran.
func liveCaller() classify.Caller {
	return classify.Caller{
		Name: "cluster-admin-agent",
		Tier: string(agentv1alpha1.TierClusterAdmin),
		Scope: scope.Scope{
			ProjectID:   "adamparco-kage",
			ClusterName: "gke-scratch-kube-agents-dev",
		},
	}
}

func mustNamespace(t *testing.T, ctx context.Context, k8s client.Client, name string, labels map[string]string) *corev1.Namespace {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace %s: %v", name, err)
	}
	// Namespaces do not go away promptly without a kube-controller-manager, so nothing deletes them;
	// each test uses its own name instead.
	return ns
}

// ---------------------------------------------------------------------------------------------
// V-GAT-022 — classification reads live state, not the payload
// ---------------------------------------------------------------------------------------------

// The one envelope, classified three times against three cluster states.
//
// Byte-identical is meant literally: the same RawOp slice value is passed each time, so nothing
// about the request can be responsible for a difference in the answer. Only the cluster changes.
//
//	positive  the namespace gains kube-agents/environment=production -> the class MUST rise
//	negative  the PAYLOAD claims the same label, cluster unlabelled  -> the class MUST NOT rise
//
// The negative control is the whole point of the check. A classifier that believed the payload
// would pass the positive half perfectly and would be trivially defeatable: any agent could pin its
// own risk class by omitting a label from an object it is creating, or -- worse for the other
// direction -- an agent could not be made to gate at all, because the thing being asked about is
// the thing under its control.
func TestLiveStateClassificationFollowsTheCluster(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise livestate.Source against an API server")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	caller := liveCaller()

	cls, err := classify.New(nil, alwaysSeen{})
	if err != nil {
		t.Fatalf("classify.New: %v", err)
	}

	const nsName = "gat022-target"
	ns := mustNamespace(t, ctx, k8s, nsName, nil)

	// The envelope. Its payload asserts the production label; the cluster does not, yet. Built once
	// and never rebuilt, so the two runs below differ in the cluster alone.
	envelope := []classify.RawOp{{
		Verb:      "apply",
		Kind:      classify.KindRef{Group: "", Kind: "ConfigMap"},
		Namespace: nsName,
		Name:      "app-config",
		Payload: map[string]any{
			"metadata": map[string]any{
				"name":      "app-config",
				"namespace": nsName,
				"labels":    map[string]any{classify.LabelEnvironment: "production"},
			},
			"data": map[string]any{"log_level": "info"},
		},
	}}

	// The same envelope with the label claim removed. Nothing else differs.
	unclaimed := []classify.RawOp{{
		Verb:      envelope[0].Verb,
		Kind:      envelope[0].Kind,
		Namespace: envelope[0].Namespace,
		Name:      envelope[0].Name,
		Payload: map[string]any{
			"metadata": map[string]any{"name": "app-config", "namespace": nsName},
			"data":     map[string]any{"log_level": "info"},
		},
	}}

	classifyOps := func(t *testing.T, ops []classify.RawOp) classify.Class {
		t.Helper()
		// A fresh adapter per classification: the denominator and digest caches are keyed on scope
		// and bounded at 60s, which is longer than this test takes. Reusing one would mean the
		// second answer came out of a cache built before the cluster changed -- a real property,
		// asserted in live_test.go, but here it would only hide the thing under test.
		fresh := &livestate.Source{Client: k8s, Discovery: disco}
		resolved, err := classify.Resolve(ctx, fresh, caller, ops)
		if err != nil {
			t.Fatalf("Resolve: %v", err)
		}
		out, err := cls.Classify(&classify.Input{Caller: caller, Operations: resolved, UndoPlanPresent: true})
		if err != nil {
			t.Fatalf("Classify: %v", err)
		}
		return out.Class
	}
	classifyNow := func(t *testing.T) classify.Class { t.Helper(); return classifyOps(t, envelope) }

	// --- negative control: the payload asserts production, the cluster says nothing -------------
	baseline := classifyNow(t)
	if baseline >= classify.ClassGated {
		t.Fatalf("baseline class is already %s; this test cannot show a rise from here. "+
			"The envelope must be routine against an unlabelled namespace for the positive half to mean anything.", baseline)
	}

	// The control, isolated: against this same unlabelled cluster, an envelope that claims the
	// production label and one that does not must classify IDENTICALLY. Comparing the two directly
	// separates "the payload is ignored" from "the live state is read" -- a classifier that read
	// neither would satisfy the first assertion below and fail the positive half, and one that read
	// only the payload would do the reverse.
	if got := envelope[0].Payload.(map[string]any)["metadata"].(map[string]any)["labels"].(map[string]any)[classify.LabelEnvironment]; got != "production" {
		t.Fatalf("the negative control's payload no longer asserts the label it is controlling for (got %v)", got)
	}
	if unclaimedClass := classifyOps(t, unclaimed); unclaimedClass != baseline {
		t.Fatalf("removing the payload's production label changed the class from %s to %s: "+
			"the classifier is reading the envelope's own claim about its environment, which is the caller's to write",
			baseline, unclaimedClass)
	}

	// --- positive: the cluster gains the label, nothing else changes ----------------------------
	ns.Labels = map[string]string{classify.LabelEnvironment: "production"}
	if err := k8s.Update(ctx, ns); err != nil {
		t.Fatalf("label namespace: %v", err)
	}
	labelled := classifyNow(t)
	if labelled <= baseline {
		t.Fatalf("class did not rise when the namespace became production: %s -> %s. "+
			"The classifier is not reading the live namespace.", baseline, labelled)
	}

	// --- the ladder, live: a more specific object label overrides the namespace's ---------------
	// 06 §4.2 is first-match-wins, and the object rung is above the namespace rung. This is the
	// live-state form of it: the object exists, is labelled staging, and the production namespace
	// does not get consulted.
	staging := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name: "app-config", Namespace: nsName,
			Labels: map[string]string{classify.LabelEnvironment: "staging"},
		},
		Data: map[string]string{"log_level": "info"},
	}
	if err := k8s.Create(ctx, staging); err != nil {
		t.Fatalf("create the staging-labelled target: %v", err)
	}
	carved := classifyNow(t)
	if carved >= labelled {
		t.Fatalf("an object labelled staging inside a production namespace classified %s, "+
			"no lower than the production answer %s: the object rung is not overriding the namespace rung", carved, labelled)
	}
}

// ---------------------------------------------------------------------------------------------
// GetObject / GetNamespaceLabels against a real API server
// ---------------------------------------------------------------------------------------------

func TestLiveStateGetObjectOverARealAPIServer(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	live := &livestate.Source{Client: k8s, Discovery: disco}

	const nsName = "getobject-probe"
	mustNamespace(t, ctx, k8s, nsName, map[string]string{"env": "staging"})

	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "present", Namespace: nsName,
		Labels:      map[string]string{"app": "api"},
		Annotations: map[string]string{classify.AnnotationRiskClass: "gated"},
	}}
	if err := k8s.Create(ctx, cm); err != nil {
		t.Fatalf("create ConfigMap: %v", err)
	}

	// An object that exists: labels and annotations come back off the wire, through the
	// PartialObjectMetadata accept header rather than a full Get.
	lbls, anns, exists, err := live.GetObject(ctx, classify.KindRef{Kind: "ConfigMap"}, nsName, "present")
	if err != nil || !exists {
		t.Fatalf("GetObject on an existing object = (exists %v, err %v)", exists, err)
	}
	if lbls["app"] != "api" {
		t.Fatalf("labels = %v, want app=api", lbls)
	}
	if anns[classify.AnnotationRiskClass] != "gated" {
		t.Fatalf("annotations = %v, want the risk-class override to survive the metadata projection", anns)
	}

	// Absent is an answer, not an error: every `create` resolves through this path.
	if _, _, exists, err := live.GetObject(ctx, classify.KindRef{Kind: "ConfigMap"}, nsName, "absent"); err != nil || exists {
		t.Fatalf("GetObject on an absent object = (exists %v, err %v), want (false, nil)", exists, err)
	}

	// A kind this cluster does not serve is an ERROR, not an absent object. The distinction is
	// load-bearing: for `create`, absent is the ordinary case and a typo'd kind would sail through
	// unclassified. The RESTMapper here is the real one, so this is the failure production gets.
	_, _, _, err = live.GetObject(ctx, classify.KindRef{Group: "apps", Kind: "Deploymnet"}, nsName, "typo")
	if err == nil {
		t.Fatal("a kind the server does not serve must be an error; treating it as absent lets a misspelled kind classify as routine")
	}
	if !strings.Contains(err.Error(), "Deploymnet") {
		t.Fatalf("the error must quote the kind back so the author can see the typo: %v", err)
	}

	// A kind the server DOES serve, at a group the envelope got wrong, is the same failure with a
	// friendlier disguise. apps/ConfigMap does not exist even though both halves do.
	if _, _, _, err := live.GetObject(ctx, classify.KindRef{Group: "apps", Kind: "ConfigMap"}, nsName, "present"); err == nil {
		t.Fatal("apps/ConfigMap resolved; a wrong group must not silently find the core-group object")
	}

	// Namespace labels: present, and absent-without-error for a create into a namespace that does
	// not exist yet.
	nsLabels, nsExists, err := live.GetNamespaceLabels(ctx, nsName)
	if err != nil || !nsExists || nsLabels["env"] != "staging" {
		t.Fatalf("GetNamespaceLabels = (%v, %v, %v), want the staging label", nsLabels, nsExists, err)
	}
	if _, nsExists, err := live.GetNamespaceLabels(ctx, "never-created"); err != nil || nsExists {
		t.Fatalf("a missing namespace = (exists %v, err %v), want (false, nil)", nsExists, err)
	}
}

// ---------------------------------------------------------------------------------------------
// The denominator, over live discovery
// ---------------------------------------------------------------------------------------------

// countableKinds is exercised hermetically over a four-group fixture. This runs it over what an API
// server actually serves -- around forty kinds, including this project's own CRDs, which is the
// input shape [[LSN-036]] is about: a hardcoded list would be correct here and wrong the day a
// customer installs something.
func TestLiveStateDenominatorOverRealDiscovery(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	live := &livestate.Source{Client: k8s, Discovery: disco}

	const nsName = "denominator-probe"
	mustNamespace(t, ctx, k8s, nsName, nil)
	s := scope.Scope{ProjectID: "adamparco-kage", ClusterName: "gke-scratch-kube-agents-dev", Namespace: nsName}

	// A fresh namespace holds a default ServiceAccount and nothing else on this API server, but the
	// exact baseline is not the point and is not asserted -- it is subtracted.
	before, err := live.CountWorkloadObjects(ctx, s)
	if err != nil {
		t.Fatalf("CountWorkloadObjects over live discovery: %v", err)
	}

	owner := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: nsName}}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}
	for i := 0; i < 3; i++ {
		cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("owned-%d", i), Namespace: nsName,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID,
			}},
		}}
		if err := k8s.Create(ctx, cm); err != nil {
			t.Fatalf("create owned-%d: %v", i, err)
		}
	}
	// Excluded by kind (blast.go), so it must not move the number either.
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "excluded", Namespace: nsName},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "c", Image: "nginx"}}},
	}
	if err := k8s.Create(ctx, pod); err != nil {
		t.Fatalf("create pod: %v", err)
	}

	// A second adapter, because the first one has the pre-create count cached for 60s.
	after, err := (&livestate.Source{Client: k8s, Discovery: disco}).CountWorkloadObjects(ctx, s)
	if err != nil {
		t.Fatalf("CountWorkloadObjects after: %v", err)
	}
	if got := after - before; got != 1 {
		t.Fatalf("the denominator moved by %d after adding 1 unowned ConfigMap, 3 owned ConfigMaps and 1 Pod; want 1. "+
			"before=%d after=%d", got, before, after)
	}

	// Scope isolation over a real server: a sibling namespace's objects are not in this count.
	const otherNS = "denominator-other"
	mustNamespace(t, ctx, k8s, otherNS, nil)
	for i := 0; i < 5; i++ {
		cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("elsewhere-%d", i), Namespace: otherNS}}
		if err := k8s.Create(ctx, cm); err != nil {
			t.Fatalf("create elsewhere-%d: %v", i, err)
		}
	}
	again, err := (&livestate.Source{Client: k8s, Discovery: disco}).CountWorkloadObjects(ctx, s)
	if err != nil {
		t.Fatal(err)
	}
	if again != after {
		t.Fatalf("denominator for %s changed from %d to %d after creating objects in %s: the List is not namespaced",
			nsName, after, again, otherNS)
	}
}

// ---------------------------------------------------------------------------------------------
// The digest set, over real Secrets
// ---------------------------------------------------------------------------------------------

// The exfiltration gate's input, built from Secrets a real API server decoded. base64 round-tripping
// through etcd is exactly the kind of thing a stub gets right by construction and a real server can
// get wrong, so the assertion is that the value the operator typed is the value the digest matches.
func TestLiveStateSecretDigestsOverRealSecrets(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	live := &livestate.Source{Client: k8s, Discovery: disco}

	const nsName = "digest-probe"
	const secretValue = "s3cr3t-database-password-9f2a"
	mustNamespace(t, ctx, k8s, nsName, nil)

	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "db-creds", Namespace: nsName},
		StringData: map[string]string{"password": secretValue},
	}
	if err := k8s.Create(ctx, sec); err != nil {
		t.Fatalf("create Secret: %v", err)
	}

	s := scope.Scope{ProjectID: "adamparco-kage", ClusterName: "gke-scratch-kube-agents-dev", Namespace: nsName}
	ds, err := live.SecretDigests(ctx, s)
	if err != nil {
		t.Fatalf("SecretDigests: %v", err)
	}

	hits := classify.ScanPayload(ds, map[string]any{"data": map[string]any{"DB_PASSWORD": secretValue}}, "")
	if len(hits) != 1 {
		t.Fatalf("the digest set built from a real Secret did not match the value it was built from: %v", hits)
	}
	if hits[0].Namespace != nsName || hits[0].Secret != "db-creds" || hits[0].Key != "password" {
		t.Fatalf("hit does not name its source: %+v", hits[0])
	}

	// Negative control: a payload with no secret material produces no hits. Without this, a digest
	// set that matched everything would score a perfect result above.
	if hits := classify.ScanPayload(ds, map[string]any{"data": map[string]any{"LOG_LEVEL": "info"}}, ""); len(hits) != 0 {
		t.Fatalf("an innocuous payload matched the digest set: %v", hits)
	}
}

// ---------------------------------------------------------------------------------------------
// Ownership, over real Agent CRs
// ---------------------------------------------------------------------------------------------

func TestLiveStateLowerTierOwnerOverRealAgents(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	live := &livestate.Source{Client: k8s, Discovery: disco}
	caller := liveCaller()

	const nsName = "owner-probe"
	mustNamespace(t, ctx, k8s, nsName, nil)
	kind := classify.KindRef{Group: "apps", Kind: "Deployment"}

	// No Agents: no owner, and no error. This is the answer that SKIPS the cross-tier gate, so it
	// has to be reachable only when it is true.
	if owner, err := live.LowerTierOwner(ctx, caller, kind, nsName, "api"); err != nil || owner != "" {
		t.Fatalf("with no Agents, LowerTierOwner = (%q, %v), want (\"\", nil)", owner, err)
	}

	dev := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "team-owner", Namespace: nsName},
		Spec: agentv1alpha1.AgentSpec{
			Tier: agentv1alpha1.TierDeveloperTeam,
			Scope: &agentv1alpha1.ScopeSpec{
				ProjectID:   caller.Scope.ProjectID,
				ClusterName: caller.Scope.ClusterName,
				Namespace:   nsName,
			},
			ParentRef: &agentv1alpha1.ParentRefSpec{Name: caller.Name},
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   caller.Scope.ProjectID,
				ClusterName: caller.Scope.ClusterName,
				Location:    "us-east4-a",
			},
		},
	}
	if err := k8s.Create(ctx, dev); err != nil {
		t.Fatalf("create developer-team Agent: %v", err)
	}

	owner, err := live.LowerTierOwner(ctx, caller, kind, nsName, "api")
	if err != nil {
		t.Fatalf("LowerTierOwner: %v", err)
	}
	if owner != "team-owner" {
		t.Fatalf("owner = %q, want team-owner: the live Agent set is not reaching the lookup", owner)
	}

	// A namespace the developer-team agent does not own has no owner. Without this, an adapter that
	// returned the first Agent it listed would pass the assertion above.
	const elsewhere = "owner-probe-elsewhere"
	mustNamespace(t, ctx, k8s, elsewhere, nil)
	if owner, err := live.LowerTierOwner(ctx, caller, kind, elsewhere, "api"); err != nil || owner != "" {
		t.Fatalf("LowerTierOwner in an unowned namespace = (%q, %v), want (\"\", nil)", owner, err)
	}
}
