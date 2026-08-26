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

package probe_test

// Fixtures for probe_envtest_test.go. Everything here builds real API objects against the running
// envtest API server; nothing here fakes an answer, because a fake answer is the thing under test.

import (
	"context"
	"fmt"
	"sync/atomic"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func ptr[T any](v T) *T { return &v }

var nsCounter atomic.Int64

// newNamespace creates a uniquely named namespace. Every test gets its own, because several of the
// probes list across a whole namespace and a shared one would make one test's fixtures another
// test's false positive.
func newNamespace(t *testing.T, ctx context.Context, labels map[string]string) string {
	t.Helper()
	name := fmt.Sprintf("probe-%d", nsCounter.Add(1))
	mustCreate(t, ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels}})
	return name
}

func mustCreate(t *testing.T, ctx context.Context, obj client.Object) {
	t.Helper()
	if err := k8s.Create(ctx, obj); err != nil {
		t.Fatalf("create %T %s: %v", obj, obj.GetName(), err)
	}
}

func podTemplate(labels map[string]string) corev1.PodTemplateSpec {
	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: labels},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{Name: "app", Image: "registry.k8s.io/pause:3.9"}},
		},
	}
}

// makePod creates a pod and writes the restart counts onto its status. envtest runs no kubelet, so
// status is whatever the test says it is -- which is the only way to construct "this rollout
// completed and then started crashlooping" deterministically.
func makePod(t *testing.T, ctx context.Context, ns, name string, labels map[string]string, appRestarts, initRestarts int32) {
	t.Helper()
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name, Labels: labels},
		Spec: corev1.PodSpec{
			InitContainers: []corev1.Container{{Name: "init", Image: "registry.k8s.io/pause:3.9"}},
			Containers:     []corev1.Container{{Name: "app", Image: "registry.k8s.io/pause:3.9"}},
		},
	}
	mustCreate(t, ctx, pod)

	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{Name: "app", RestartCount: appRestarts}}
	pod.Status.InitContainerStatuses = []corev1.ContainerStatus{{Name: "init", RestartCount: initRestarts}}
	if err := k8s.Status().Update(ctx, pod); err != nil {
		t.Fatalf("status update pod %s/%s: %v", ns, name, err)
	}
}

func clusterIPService(ns, name string) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": name},
			Ports:    []corev1.ServicePort{{Port: 80, TargetPort: intstr.FromInt32(8080)}},
		},
	}
}

// ingress builds an Ingress routing to the named Services. With no names it builds one whose only
// path points at a RESOURCE backend -- a legal Ingress that names no Service at all, which is the
// negative control for EndpointCount and the only way to construct it: networking/v1 validation
// rejects an Ingress with neither rules nor a defaultBackend.
func ingress(ns, name string, services ...string) *networkingv1.Ingress {
	ing := &networkingv1.Ingress{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name}}
	paths := []networkingv1.HTTPIngressPath{}
	if len(services) == 0 {
		paths = append(paths, networkingv1.HTTPIngressPath{
			Path:     "/static",
			PathType: ptr(networkingv1.PathTypePrefix),
			Backend: networkingv1.IngressBackend{Resource: &corev1.TypedLocalObjectReference{
				APIGroup: ptr("k8s.example.com"), Kind: "StorageBucket", Name: "assets",
			}},
		})
	}
	for _, svc := range services {
		paths = append(paths, networkingv1.HTTPIngressPath{
			Path:     "/" + svc,
			PathType: ptr(networkingv1.PathTypePrefix),
			Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{
				Name: svc, Port: networkingv1.ServiceBackendPort{Number: 80},
			}},
		})
	}
	ing.Spec.Rules = []networkingv1.IngressRule{{
		IngressRuleValue: networkingv1.IngressRuleValue{
			HTTP: &networkingv1.HTTPIngressRuleValue{Paths: paths},
		},
	}}
	return ing
}

func limitRange(ns, name string, max, defaults corev1.ResourceList) *corev1.LimitRange {
	item := corev1.LimitRangeItem{Type: corev1.LimitTypeContainer}
	if max != nil {
		item.Max = max
	}
	if defaults != nil {
		item.Default = defaults
		item.DefaultRequest = defaults
	}
	return &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec:       corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{item}},
	}
}

func lrRef(ns, name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "LimitRange", Namespace: ns, Name: name}
}

// setQuotaStatus writes what the quota controller would have written. envtest runs no
// kube-controller-manager, which is convenient here: the "object present, controller has not
// processed it" state is the default rather than a race to catch.
func setQuotaStatus(t *testing.T, ctx context.Context, q *corev1.ResourceQuota, hard corev1.ResourceList) {
	t.Helper()
	live := &corev1.ResourceQuota{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(q), live); err != nil {
		t.Fatalf("get quota: %v", err)
	}
	used := corev1.ResourceList{}
	for name := range hard {
		used[name] = resource.MustParse("0")
	}
	live.Status = corev1.ResourceQuotaStatus{Hard: hard, Used: used}
	if err := k8s.Status().Update(ctx, live); err != nil {
		t.Fatalf("status update quota: %v", err)
	}
}

// --- the Config Connector node pool -----------------------------------------------------------

const (
	providerGroup   = "container.cnrm.cloud.google.com"
	providerVersion = "v1beta1"
)

// nodePoolCRD is a minimal stand-in for Config Connector's ContainerNodePool: the real schema is
// hundreds of fields and this prober reads five of them. `x-kubernetes-preserve-unknown-fields`
// keeps whatever a test writes, and the status subresource is what makes `metadata.generation`
// advance on a spec change while a status write leaves it alone -- which is the entire mechanism
// the stale-Ready check depends on.
func nodePoolCRD() *apiextensionsv1.CustomResourceDefinition {
	return &apiextensionsv1.CustomResourceDefinition{
		ObjectMeta: metav1.ObjectMeta{Name: "containernodepools." + providerGroup},
		Spec: apiextensionsv1.CustomResourceDefinitionSpec{
			Group: providerGroup,
			Names: apiextensionsv1.CustomResourceDefinitionNames{
				Plural:   "containernodepools",
				Singular: "containernodepool",
				Kind:     "ContainerNodePool",
				ListKind: "ContainerNodePoolList",
			},
			Scope: apiextensionsv1.NamespaceScoped,
			Versions: []apiextensionsv1.CustomResourceDefinitionVersion{{
				Name:    providerVersion,
				Served:  true,
				Storage: true,
				Subresources: &apiextensionsv1.CustomResourceSubresources{
					Status: &apiextensionsv1.CustomResourceSubresourceStatus{},
				},
				Schema: &apiextensionsv1.CustomResourceValidation{
					OpenAPIV3Schema: &apiextensionsv1.JSONSchemaProps{
						Type:                   "object",
						XPreserveUnknownFields: ptr(true),
					},
				},
			}},
		},
	}
}

func nodePoolObject(ns, name string) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetAPIVersion(providerGroup + "/" + providerVersion)
	obj.SetKind("ContainerNodePool")
	obj.SetNamespace(ns)
	obj.SetName(name)
	return obj
}

func newNodePool(t *testing.T, ctx context.Context, ns, name string, spec map[string]any) string {
	t.Helper()
	obj := nodePoolObject(ns, name)
	if err := unstructured.SetNestedMap(obj.Object, spec, "spec"); err != nil {
		t.Fatalf("set spec: %v", err)
	}
	mustCreate(t, ctx, obj)
	return name
}

func getNodePool(t *testing.T, ctx context.Context, ns, name string) *unstructured.Unstructured {
	t.Helper()
	obj := nodePoolObject(ns, name)
	if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, obj); err != nil {
		t.Fatalf("get node pool %s/%s: %v", ns, name, err)
	}
	return obj
}

func currentGeneration(t *testing.T, ctx context.Context, ns, name string) int64 {
	t.Helper()
	return getNodePool(t, ctx, ns, name).GetGeneration()
}

// setNodePoolReady writes the Ready condition Config Connector would write, stamped with the
// generation it was reconciling.
func setNodePoolReady(t *testing.T, ctx context.Context, ns, name, reason string, observedGeneration int64) {
	t.Helper()
	obj := getNodePool(t, ctx, ns, name)
	status := map[string]any{
		"observedGeneration": observedGeneration,
		"conditions": []any{map[string]any{
			"type": "Ready", "status": "True", "reason": reason,
		}},
	}
	if err := unstructured.SetNestedMap(obj.Object, status, "status"); err != nil {
		t.Fatalf("set status: %v", err)
	}
	if err := k8s.Status().Update(ctx, obj); err != nil {
		t.Fatalf("status update node pool: %v", err)
	}
}

// touchNodePoolSpec changes the spec, which advances metadata.generation and leaves the status
// where it was -- the shape of a resize the provider has not caught up with.
func touchNodePoolSpec(t *testing.T, ctx context.Context, ns, name string, nodeCount int64) {
	t.Helper()
	obj := getNodePool(t, ctx, ns, name)
	if err := unstructured.SetNestedField(obj.Object, nodeCount, "spec", "nodeCount"); err != nil {
		t.Fatalf("set nodeCount: %v", err)
	}
	if err := k8s.Update(ctx, obj); err != nil {
		t.Fatalf("update node pool: %v", err)
	}
}

// makeNode registers a Node carrying the GKE node-pool label, which is how nodePoolCounts finds a
// pool's nodes without a cloud API call.
func makeNode(t *testing.T, ctx context.Context, pool, suffix string, ready bool) {
	t.Helper()
	name := fmt.Sprintf("%s-%s", pool, suffix)
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{
		Name:   name,
		Labels: map[string]string{"cloud.google.com/gke-nodepool": pool},
	}}
	mustCreate(t, ctx, node)
	t.Cleanup(func() { _ = k8s.Delete(context.Background(), node) })

	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	node.Status.Conditions = []corev1.NodeCondition{{Type: corev1.NodeReady, Status: status}}
	if err := k8s.Status().Update(ctx, node); err != nil {
		t.Fatalf("status update node %s: %v", name, err)
	}
}
