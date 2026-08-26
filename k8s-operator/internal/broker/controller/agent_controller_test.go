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

package controller

import (
	"context"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	nodev1 "k8s.io/api/node/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func setupScheme() *runtime.Scheme {
	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(agentv1alpha1.AddToScheme(scheme))
	return scheme
}

func TestAgentReconciler_Reconcile(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.AgentSpec{},
	}

	// Interceptor to handle Server-Side Apply (SSA) in fake client
	interceptors := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() == types.ApplyPatchType {
				key := client.ObjectKeyFromObject(obj)
				existing := obj.DeepCopyObject().(client.Object)
				err := cl.Get(ctx, key, existing)
				if err != nil {
					if errors.IsNotFound(err) {
						return cl.Create(ctx, obj)
					}
					return err
				}
				obj.SetResourceVersion(existing.GetResourceVersion())
				return cl.Update(ctx, obj)
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}

	// Create a fake client with the Agent
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.Agent{}).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &AgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent",
			Namespace: "test-ns",
		},
	}

	ctx := context.Background()

	// Reconcile: the controller creates all owned workload resources in a single pass. After P1-T4/T5 it
	// no longer adds a finalizer or mints RBAC; the read-only identity is pre-created & GitOps-managed.
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}

	// Fetch agent to verify NO finalizer is added (nothing cluster-scoped for the controller to clean up).
	updatedAgent := &agentv1alpha1.Agent{}
	err = cl.Get(ctx, req.NamespacedName, updatedAgent)
	if err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if controllerutil.ContainsFinalizer(updatedAgent, agentFinalizer) {
		t.Errorf("expected no finalizer, but found %v", updatedAgent.Finalizers)
	}

	// Verify resources were created

	// PVC
	pvc := &corev1.PersistentVolumeClaim{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-data", Namespace: "test-ns"}, pvc); err != nil {
		t.Errorf("failed to get PVC: %v", err)
	} else if len(pvc.OwnerReferences) != 1 || pvc.OwnerReferences[0].Kind != "Agent" {
		t.Errorf("expected PVC to have OwnerReference to Agent")
	}

	// ConfigMaps
	configMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-config", Namespace: "test-ns"}, configMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-config: %v", err)
	} else if len(configMap.OwnerReferences) != 1 || configMap.OwnerReferences[0].Kind != "Agent" {
		t.Errorf("expected ConfigMap to have OwnerReference to Agent")
	}

	fluentBitConfigMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-fluent-bit-config", Namespace: "test-ns"}, fluentBitConfigMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-fluent-bit-config: %v", err)
	} else if len(fluentBitConfigMap.OwnerReferences) != 1 || fluentBitConfigMap.OwnerReferences[0].Kind != "Agent" {
		t.Errorf("expected FluentBit ConfigMap to have OwnerReference to Agent")
	}

	settingsConfigMap := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-settings", Namespace: "test-ns"}, settingsConfigMap); err != nil {
		t.Errorf("failed to get ConfigMap test-agent-settings: %v", err)
	} else if len(settingsConfigMap.OwnerReferences) != 1 || settingsConfigMap.OwnerReferences[0].Kind != "Agent" {
		t.Errorf("expected Settings ConfigMap to have OwnerReference to Agent")
	}

	// Deployment
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}, dep); err != nil {
		t.Errorf("failed to get Deployment: %v", err)
	} else {
		if len(dep.OwnerReferences) != 1 || dep.OwnerReferences[0].Kind != "Agent" {
			t.Errorf("expected Deployment to have OwnerReference to Agent")
		}
		if len(dep.Spec.Template.Spec.Containers) == 0 || dep.Spec.Template.Spec.Containers[0].Name != "platform-agent" {
			t.Errorf("expected Deployment to have container named 'platform-agent'")
		}
	}

	// Service
	svc := &corev1.Service{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}, svc); err != nil {
		t.Errorf("failed to get Service: %v", err)
	} else if len(svc.OwnerReferences) != 1 || svc.OwnerReferences[0].Kind != "Agent" {
		t.Errorf("expected Service to have OwnerReference to Agent")
	}

	// RBAC: the controller must NOT mint agent RBAC (P1-T4). Assert the ClusterRole / ClusterRoleBindings
	// the old controller created are absent — identity is pre-created via GitOps and enforced by
	// vap-agent-readonly.
	explorerRole := &rbacv1.ClusterRole{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:explorer:test-ns:test-agent"}, explorerRole); err == nil {
		t.Errorf("expected controller NOT to mint explorer ClusterRole, but it exists")
	} else if !errors.IsNotFound(err) {
		t.Errorf("unexpected error checking explorer ClusterRole: %v", err)
	}

	crbViewer := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:viewer:test-ns:test-agent"}, crbViewer); err == nil {
		t.Errorf("expected controller NOT to mint viewer ClusterRoleBinding, but it exists")
	} else if !errors.IsNotFound(err) {
		t.Errorf("unexpected error checking viewer ClusterRoleBinding: %v", err)
	}

	crbExplorer := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, types.NamespacedName{Name: "kubeagents:explorer:test-ns:test-agent"}, crbExplorer); err == nil {
		t.Errorf("expected controller NOT to mint explorer ClusterRoleBinding, but it exists")
	} else if !errors.IsNotFound(err) {
		t.Errorf("unexpected error checking explorer ClusterRoleBinding: %v", err)
	}

	// Test Deletion
	err = cl.Delete(ctx, updatedAgent)
	if err != nil {
		t.Fatalf("failed to delete agent: %v", err)
	}

	// Reconcile after deletion timestamp is set
	_, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile on delete failed: %v", err)
	}

	// Verify agent is deleted completely (no finalizer blocks removal after P1-T4/T5).
	err = cl.Get(ctx, req.NamespacedName, updatedAgent)
	if err == nil {
		t.Fatalf("expected agent to be deleted, but it still exists")
	} else if !errors.IsNotFound(err) {
		t.Fatalf("expected NotFound error, got: %v", err)
	}
}

func TestAgentReconciler_Reconcile_MissingRuntimeClass(t *testing.T) {
	scheme := setupScheme()

	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-missing-rc",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.AgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				RuntimeClassName: ptr.To("gvisor"),
			},
		},
	}

	interceptors := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() == types.ApplyPatchType {
				key := client.ObjectKeyFromObject(obj)
				existing := obj.DeepCopyObject().(client.Object)
				err := cl.Get(ctx, key, existing)
				if err != nil {
					if errors.IsNotFound(err) {
						return cl.Create(ctx, obj)
					}
					return err
				}
				obj.SetResourceVersion(existing.GetResourceVersion())
				return cl.Update(ctx, obj)
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.Agent{}).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &AgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-missing-rc",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: creates config + validates RuntimeClass (no finalizer after P1-T4/T5)
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates RuntimeClass and halts deployment creation
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("expected RequeueAfter 30s, got %v", res.RequeueAfter)
	}

	// Verify status is Degraded
	updatedAgent := &agentv1alpha1.Agent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded, got %q", updatedAgent.Status.Phase)
	}
	cond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Ready")
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != "RuntimeClassNotFound" {
		t.Errorf("expected Ready condition False with reason RuntimeClassNotFound, got %v", cond)
	}

	// Verify Deployment was NOT created
	dep := &appsv1.Deployment{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-missing-rc-gateway", Namespace: "test-ns"}, dep)
	if !errors.IsNotFound(err) {
		t.Errorf("expected Deployment to not be created when RuntimeClass is missing, got err: %v", err)
	}
}

func TestAgentReconciler_Reconcile_ExistingRuntimeClass(t *testing.T) {
	scheme := setupScheme()

	rc := &nodev1.RuntimeClass{
		ObjectMeta: metav1.ObjectMeta{
			Name: "gvisor",
		},
		Handler: "gvisor",
	}

	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-existing-rc",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.AgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				RuntimeClassName: ptr.To("gvisor"),
			},
		},
	}

	interceptors := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() == types.ApplyPatchType {
				key := client.ObjectKeyFromObject(obj)
				existing := obj.DeepCopyObject().(client.Object)
				err := cl.Get(ctx, key, existing)
				if err != nil {
					if errors.IsNotFound(err) {
						return cl.Create(ctx, obj)
					}
					return err
				}
				obj.SetResourceVersion(existing.GetResourceVersion())
				return cl.Update(ctx, obj)
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, rc).
		WithStatusSubresource(&agentv1alpha1.Agent{}).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &AgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-existing-rc",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: creates config + validates RuntimeClass (no finalizer after P1-T4/T5)
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates existing RuntimeClass and creates resources
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}
	// The RuntimeClass exists, so the 30 s degraded retry must NOT fire. What does fire is the
	// broker-health clock (brokerHealthRequeue) — a different fact with a different period, and the
	// assertion names both so that "no degraded retry" cannot be satisfied by the reconcile simply
	// having stopped coming back at all.
	if res.RequeueAfter == 30*time.Second {
		t.Error("RequeueAfter is the 30s degraded retry, but the RuntimeClass exists and nothing is degraded")
	}
	if res.RequeueAfter != brokerHealthRequeue {
		t.Errorf("RequeueAfter = %v, want the broker-health period %v: status.broker.journalReachable has no watch behind it and goes stale without the clock",
			res.RequeueAfter, brokerHealthRequeue)
	}

	// Verify Deployment was created with RuntimeClassName "gvisor"
	dep := &appsv1.Deployment{}
	err = cl.Get(ctx, types.NamespacedName{Name: "test-agent-existing-rc-gateway", Namespace: "test-ns"}, dep)
	if err != nil {
		t.Fatalf("expected Deployment to be created when RuntimeClass exists, got err: %v", err)
	}
	if dep.Spec.Template.Spec.RuntimeClassName == nil || *dep.Spec.Template.Spec.RuntimeClassName != "gvisor" {
		t.Errorf("expected Deployment RuntimeClassName 'gvisor', got %v", dep.Spec.Template.Spec.RuntimeClassName)
	}

	// Verify status is not Degraded
	updatedAgent := &agentv1alpha1.Agent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}
	if updatedAgent.Status.Phase == "Degraded" {
		t.Errorf("expected Status.Phase not Degraded when RuntimeClass exists, got %q", updatedAgent.Status.Phase)
	}
}

func TestAgentReconciler_Reconcile_PodUnschedulable(t *testing.T) {
	scheme := setupScheme()

	rc := &nodev1.RuntimeClass{
		ObjectMeta: metav1.ObjectMeta{
			Name: "gvisor",
		},
		Handler: "gvisor",
	}

	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-unschedulable",
			Namespace: "test-ns",
		},
		Spec: agentv1alpha1.AgentSpec{
			Deployment: &agentv1alpha1.DeploymentSpec{
				RuntimeClassName: ptr.To("gvisor"),
			},
		},
	}

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-agent-unschedulable-gateway-pod",
			Namespace: "test-ns",
			Labels: map[string]string{
				"app": "test-agent-unschedulable-gateway",
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			Conditions: []corev1.PodCondition{
				{
					Type:    corev1.PodScheduled,
					Status:  corev1.ConditionFalse,
					Reason:  "Unschedulable",
					Message: "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. no new claims to deallocate, preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.",
				},
			},
		},
	}

	interceptors := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if patch.Type() == types.ApplyPatchType {
				key := client.ObjectKeyFromObject(obj)
				existing := obj.DeepCopyObject().(client.Object)
				err := cl.Get(ctx, key, existing)
				if err != nil {
					if errors.IsNotFound(err) {
						return cl.Create(ctx, obj)
					}
					return err
				}
				obj.SetResourceVersion(existing.GetResourceVersion())
				return cl.Update(ctx, obj)
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}

	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, rc, pod).
		WithStatusSubresource(&agentv1alpha1.Agent{}).
		WithInterceptorFuncs(interceptors).
		Build()

	r := &AgentReconciler{
		Client: cl,
		Scheme: scheme,
	}

	req := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Name:      "test-agent-unschedulable",
			Namespace: "test-ns",
		},
	}
	ctx := context.Background()

	// 1st Reconcile: creates config + validates RuntimeClass (no finalizer after P1-T4/T5)
	_, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 1 failed: %v", err)
	}

	// 2nd Reconcile: Validates RuntimeClass, creates Deployment, and inspects unschedulable Pod
	_, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile 2 failed: %v", err)
	}

	updatedAgent := &agentv1alpha1.Agent{}
	if err := cl.Get(ctx, req.NamespacedName, updatedAgent); err != nil {
		t.Fatalf("failed to get agent: %v", err)
	}

	if updatedAgent.Status.Phase != "Degraded" {
		t.Errorf("expected Status.Phase Degraded when Pod is Unschedulable, got %q", updatedAgent.Status.Phase)
	}

	cond := meta.FindStatusCondition(updatedAgent.Status.Conditions, "Ready")
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != "PodUnschedulable" {
		t.Fatalf("expected Ready condition False with reason PodUnschedulable, got %v", cond)
	}

	expectedMsg := "Pod test-agent-unschedulable-gateway-pod is waiting to be scheduled because no nodes in the cluster match the requested RuntimeClass 'gvisor'. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool."
	if cond.Message != expectedMsg {
		t.Errorf("expected polished condition message:\n%q\ngot:\n%q", expectedMsg, cond.Message)
	}
}
