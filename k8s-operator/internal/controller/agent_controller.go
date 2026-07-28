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
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	nodev1 "k8s.io/api/node/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const agentFinalizer = "kubeagents.x-k8s.io/finalizer"

// AgentReconciler reconciles an Agent object
type AgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims;configmaps;services,verbs=get;list;watch;create;update;patch;delete
// serviceaccounts is read-only: the controller REFERENCES the pre-created agent KSA by name and never
// mints or annotates it (P1-T5, 08 §4). Identity is pre-created & GitOps-managed.
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=namespaces;nodes;pods;events;persistentvolumes,verbs=get;list;watch
// +kubebuilder:rbac:groups=node.k8s.io,resources=runtimeclasses,verbs=get;list;watch
// NOTE: the controller intentionally holds NO clusterroles/clusterrolebindings permissions. It no
// longer mints agent RBAC at runtime (P1-T4, 08 §4); the read-only agent identity is pre-created via
// GitOps (policy/rbac-overlay/) and enforced by vap-agent-readonly. Do not re-add RBAC write verbs.
// +kubebuilder:rbac:groups=apiextensions.k8s.io,resources=customresourcedefinitions,verbs=get;list
// The mesh keypairs (08 §2.3, P9-T7d). The controller writes cert-manager Certificates and holds NO
// verb on the Secrets those Certificates produce — 08 §2.7 withholds get/list/watch on Secrets from
// this controller entirely, because a list verb in a namespace hosting an agent would hand it every
// projected token in that namespace. Namespaced only: the mesh CA ClusterIssuer is install-time
// (config/mesh-ca/), not something the controller may create.
// +kubebuilder:rbac:groups=cert-manager.io,resources=certificates,verbs=get;list;watch;create;update;patch;delete

func (r *AgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	instance := &agentv1alpha1.Agent{}
	if err := r.Get(ctx, req.NamespacedName, instance); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	log.Info("Reconciling Agent", "name", instance.Name, "namespace", instance.Namespace)

	// 1. Intercept Deletion (only to strip a legacy finalizer; see handleDeletion).
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, instance)
	}

	// The agent's ServiceAccount and RBAC are pre-created and GitOps-managed (policy/rbac-overlay/,
	// fleet/); the controller only REFERENCES them by name. It no longer mints a KSA (P1-T5) or agent
	// RBAC (P1-T4), so there is nothing cluster-scoped to clean up on deletion and no finalizer is
	// added — owned workload resources are garbage-collected via OwnerReferences.

	// 4. Reconcile PVC for agent persistent data
	if err := r.reconcilePVC(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 5. Reconcile ConfigMap (config.yaml content)
	configMapHash, err := r.reconcileConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Fluent Bit ConfigMap
	fluentBitHash, err := r.reconcileFluentBitConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Settings ConfigMap
	settingsHash, err := r.reconcileSettingsConfigMap(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 6. Validate RuntimeClass if specified
	if err := r.validateRuntimeClass(ctx, instance); err != nil {
		if errors.IsNotFound(err) {
			rcName := *instance.Spec.Deployment.RuntimeClassName
			msg := fmt.Sprintf("RuntimeClass '%s' is not configured in this cluster. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool first. In GKE Autopilot, gVisor is supported automatically.", rcName)
			log.Info(msg)
			if statusErr := r.updateStatusDegraded(ctx, instance, "RuntimeClassNotFound", msg); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
		}
		return ctrl.Result{}, fmt.Errorf("failed to validate RuntimeClass: %w", err)
	}

	// 7. Reconcile the workload PAIR — broker Service, then broker Deployment, then the agent
	// (08 §2.4). Not "the Deployment": one Agent CR is two workloads, and the order matters.
	if err := r.reconcileWorkloadPair(ctx, instance, configMapHash, fluentBitHash, settingsHash); err != nil {
		return ctrl.Result{}, err
	}

	// Reconcile Service
	if err := r.reconcileService(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// 7. Update status phase to Ready
	return ctrl.Result{}, r.updateStatusReady(ctx, instance)
}

// handleDeletion runs when an Agent is being deleted. The controller no longer mints RBAC or a
// KSA (P1-T4/T5) and the pre-created identity is GitOps-managed, so there is nothing for the
// controller to delete — owned workload resources (Deployment/PVC/ConfigMap/Service) are
// garbage-collected via OwnerReferences. This only strips a legacy finalizer left by an older
// controller so such CRs are not stuck terminating.
func (r *AgentReconciler) handleDeletion(ctx context.Context, agent *agentv1alpha1.Agent) (ctrl.Result, error) {
	if controllerutil.ContainsFinalizer(agent, agentFinalizer) {
		controllerutil.RemoveFinalizer(agent, agentFinalizer)
		if err := r.Update(ctx, agent); err != nil {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

func (r *AgentReconciler) reconcilePVC(ctx context.Context, agent *agentv1alpha1.Agent) error {
	for _, pvc := range []*corev1.PersistentVolumeClaim{
		buildPVC(agent),
		buildSystemPVC(agent),
	} {
		if err := r.reconcilePersistentVolumeClaim(ctx, agent, pvc); err != nil {
			return err
		}
	}
	return nil
}

func (r *AgentReconciler) reconcilePersistentVolumeClaim(ctx context.Context, agent *agentv1alpha1.Agent, pvc *corev1.PersistentVolumeClaim) error {
	if err := ctrl.SetControllerReference(agent, pvc, r.Scheme); err != nil {
		return err
	}

	found := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, client.ObjectKey{Name: pvc.Name, Namespace: pvc.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, pvc)
		}
		return err
	}
	return nil
}

func (r *AgentReconciler) reconcileConfigMap(ctx context.Context, agent *agentv1alpha1.Agent) (string, error) {
	cm := buildConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *AgentReconciler) reconcileFluentBitConfigMap(ctx context.Context, agent *agentv1alpha1.Agent) (string, error) {
	cm := buildFluentBitConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

func (r *AgentReconciler) reconcileSettingsConfigMap(ctx context.Context, agent *agentv1alpha1.Agent) (string, error) {
	cm := buildSettingsConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return "", err
	}

	err := r.Patch(ctx, cm, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller"))
	if err != nil {
		return "", err
	}

	hash, err := getConfigMapHash(cm)
	if err != nil {
		return "", err
	}
	return hash, nil
}

// reconcileWorkloadPair applies the broker and the agent, in that order (08 §2.4).
//
// One Reconcile renders both. The broker's Service is applied before either Deployment so that its
// ClusterIP and DNS name exist by the time the agent's `wait-for-broker` init container resolves
// them — a Service created after the pod that dials it turns a warm start into a two-minute wait
// on the init container's timeout, for no reason.
//
// Then the Deployments in pair order. Applying broker-first and returning on the first error is
// what implements §2.4(c): "if the broker launch fails … the launcher must not proceed to the
// agent". There is no explicit rollback of a partially-created broker here and none is needed —
// every object carries an OwnerReference to the CR, so the failure modes that would need unwinding
// (CR deleted mid-reconcile, agent apply rejected) are collected by the garbage collector against
// the owner rather than by an error path that has to be correct.
func (r *AgentReconciler) reconcileWorkloadPair(ctx context.Context, agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsHash string) error {
	// The mesh certificates first, because both halves mount them and cert-manager needs time to
	// issue. Applying them ahead of the Deployments turns the usual first-reconcile race into a
	// short ContainerCreating instead of a CrashLoopBackOff, and costs nothing when they exist.
	// This is best-effort by design: no cert-manager means no certificates and a pair that stays
	// BrokerReady: false — see reconcileMeshCertificates.
	if err := r.reconcileMeshCertificates(ctx, agent); err != nil {
		return err
	}

	brokerSvc := buildBrokerService(agent)
	if err := ctrl.SetControllerReference(agent, brokerSvc, r.Scheme); err != nil {
		return err
	}
	if err := r.Patch(ctx, brokerSvc, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller")); err != nil {
		return fmt.Errorf("failed to apply broker Service: %w", err)
	}

	// Pod construction goes through the launcher seam (08 §2 Scion spike): native build by
	// default, Scion launch primitive when gated on and available, always with native fallback.
	// BuildPair returns both halves or neither — see WorkloadPair.
	launcher := selectPodLauncher(logf.FromContext(ctx))
	pair := launcher.BuildPair(agent, configHash, fluentBitHash, settingsHash)

	for _, dep := range pair.Ordered() {
		if err := ctrl.SetControllerReference(agent, dep, r.Scheme); err != nil {
			return err
		}
		if err := r.Patch(ctx, dep, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller")); err != nil {
			return fmt.Errorf("failed to apply Deployment %q: %w", dep.Name, err)
		}
	}
	return nil
}

func (r *AgentReconciler) reconcileService(ctx context.Context, agent *agentv1alpha1.Agent) error {
	svc := buildAgentService(agent)
	if err := ctrl.SetControllerReference(agent, svc, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, svc, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller"))
}

func (r *AgentReconciler) updateStatusReady(ctx context.Context, agent *agentv1alpha1.Agent) error {
	// Fetch actual Deployment
	dep := &appsv1.Deployment{}
	errDep := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-gateway"}, dep)
	if errDep != nil && !errors.IsNotFound(errDep) {
		return fmt.Errorf("failed to get Deployment for status update: %w", errDep)
	}
	newDeploymentStatusName := ""
	newDeploymentStatusReadyReplicas := int32(0)
	if errDep == nil {
		newDeploymentStatusName = dep.Name
		newDeploymentStatusReadyReplicas = dep.Status.ReadyReplicas
	}

	// Fetch actual PVC
	pvc := &corev1.PersistentVolumeClaim{}
	errPVC := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-data"}, pvc)
	if errPVC != nil && !errors.IsNotFound(errPVC) {
		return fmt.Errorf("failed to get PVC for status update: %w", errPVC)
	}
	newStorageStatusBound := false
	if errPVC == nil {
		newStorageStatusBound = (pvc.Status.Phase == corev1.ClaimBound)
	}

	// Fetch actual Service
	svc := &corev1.Service{}
	errSvc := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, svc)
	if errSvc != nil && !errors.IsNotFound(errSvc) {
		return fmt.Errorf("failed to get Service for status update: %w", errSvc)
	}
	newServiceStatusEndpoint := ""
	newAddress := ""
	if errSvc == nil {
		newServiceStatusEndpoint = fmt.Sprintf("http://%s.%s.svc.cluster.local:8642", svc.Name, svc.Namespace)
		newAddress = fmt.Sprintf("%s.%s.svc.cluster.local", svc.Name, svc.Namespace)
	}

	// Fetch the broker half of the pair (08 §2.4). Its readiness is reported separately from the
	// agent's, because "the agent is up" and "the agent can write" are different facts and an
	// operator who cannot tell them apart will read a degraded agent as a broken one.
	brokerDep := &appsv1.Deployment{}
	errBroker := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: brokerName(agent)}, brokerDep)
	if errBroker != nil && !errors.IsNotFound(errBroker) {
		return fmt.Errorf("failed to get broker Deployment for status update: %w", errBroker)
	}
	brokerReady := errBroker == nil && brokerDep.Status.ReadyReplicas > 0

	// Determine Phase and Condition
	newPhase := "Provisioning"
	condStatus := metav1.ConditionFalse
	condReason := "Provisioning"
	condMsg := "Waiting for deployment replicas to be ready"
	if errDep == nil && dep.Status.ReadyReplicas > 0 {
		newPhase = "Ready"
		condStatus = metav1.ConditionTrue
		condReason = "Reconciled"
		condMsg = "Agent deployment and resources are fully reconciled"
	} else if errDep == nil {
		if phaseOverride, reasonOverride, msgOverride := r.getDeploymentStatusDetails(ctx, agent, dep); reasonOverride != "Provisioning" {
			newPhase = phaseOverride
			condReason = reasonOverride
			condMsg = msgOverride
		}
	}

	// `Ready` is the CONJUNCTION of the two halves (08 §2.4), and the phase follows it. An agent
	// whose broker is down is running and answering questions, so it is not "Degraded" in the
	// sense the phase already means — but it cannot execute anything, so reporting it Ready would
	// be a lie to every dashboard and every `kubectl wait` in the provisioning path.
	agentReady := condStatus
	agentReason, agentMsg := condReason, condMsg
	brokerCondStatus := metav1.ConditionFalse
	brokerReason := "BrokerProvisioning"
	brokerMsg := "Waiting for the broker Deployment to become ready"
	switch {
	case brokerReady:
		brokerCondStatus = metav1.ConditionTrue
		brokerReason = "BrokerReconciled"
		brokerMsg = "Broker is accepting envelopes"
	case errors.IsNotFound(errBroker):
		brokerReason = "BrokerNotFound"
		brokerMsg = fmt.Sprintf("Broker Deployment %s-broker does not exist; the agent cannot execute anything", agent.Name)
	}
	if condStatus == metav1.ConditionTrue && !brokerReady {
		newPhase = "Provisioning"
		condStatus = metav1.ConditionFalse
		condReason = "BrokerNotReady"
		condMsg = "Agent is running in observe-and-report mode: " + brokerMsg
	}

	newBroker := &agentv1alpha1.BrokerStatus{
		Endpoint:            brokerEndpoint(agent),
		ActorServiceAccount: actorServiceAccountName(agent),
		Ready:               brokerReady,
		// JournalReachable stays at its fail-closed zero until the broker itself reports it
		// (06 §4.4). The controller cannot observe it — it would have to ask the broker, and the
		// broker answering "yes" to the controller proves nothing about the broker's own writes.
	}

	existingCond := meta.FindStatusCondition(agent.Status.Conditions, "Ready")
	existingAgentCond := meta.FindStatusCondition(agent.Status.Conditions, "AgentReady")
	existingBrokerCond := meta.FindStatusCondition(agent.Status.Conditions, "BrokerReady")
	// Check if anything actually changed
	if agent.Status.Phase == newPhase &&
		agent.Status.DeploymentStatus.Name == newDeploymentStatusName &&
		agent.Status.DeploymentStatus.ReadyReplicas == newDeploymentStatusReadyReplicas &&
		agent.Status.StorageStatus.Bound == newStorageStatusBound &&
		agent.Status.ServiceStatus.Endpoint == newServiceStatusEndpoint &&
		agent.Status.Address == newAddress &&
		brokerStatusEqual(agent.Status.Broker, newBroker) &&
		existingAgentCond != nil && existingAgentCond.Status == agentReady && existingAgentCond.Reason == agentReason &&
		existingBrokerCond != nil && existingBrokerCond.Status == brokerCondStatus && existingBrokerCond.Reason == brokerReason &&
		existingCond != nil && existingCond.Status == condStatus && existingCond.Reason == condReason && existingCond.Message == condMsg {
		return nil
	}

	// Apply updates
	agent.Status.Phase = newPhase
	agent.Status.DeploymentStatus.Name = newDeploymentStatusName
	agent.Status.DeploymentStatus.ReadyReplicas = newDeploymentStatusReadyReplicas
	agent.Status.StorageStatus.Bound = newStorageStatusBound
	agent.Status.ServiceStatus.Endpoint = newServiceStatusEndpoint
	agent.Status.Address = newAddress
	agent.Status.Broker = newBroker

	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	// Three conditions, in the order an operator reads them: the two halves, then the conjunction.
	// `Ready` last so that a `kubectl describe` shows the summary beneath the two facts it
	// summarizes rather than above them.
	for _, condition := range []metav1.Condition{
		{
			Type:               "AgentReady",
			Status:             agentReady,
			Reason:             agentReason,
			Message:            agentMsg,
			LastTransitionTime: now,
		},
		{
			Type:               "BrokerReady",
			Status:             brokerCondStatus,
			Reason:             brokerReason,
			Message:            brokerMsg,
			LastTransitionTime: now,
		},
		{
			Type:               "Ready",
			Status:             condStatus,
			Reason:             condReason,
			Message:            condMsg,
			LastTransitionTime: now,
		},
	} {
		meta.SetStatusCondition(&agent.Status.Conditions, condition)
	}

	return r.Status().Update(ctx, agent)
}

// brokerStatusEqual compares two broker status blocks for the no-op short-circuit above. A nil
// `have` is never equal to a non-nil `want`, which is what makes the first reconcile after an
// upgrade write the block rather than skip it as unchanged.
func brokerStatusEqual(have, want *agentv1alpha1.BrokerStatus) bool {
	if have == nil || want == nil {
		return have == want
	}
	return *have == *want
}

func (r *AgentReconciler) getDeploymentStatusDetails(ctx context.Context, agent *agentv1alpha1.Agent, dep *appsv1.Deployment) (phase string, reason string, message string) {
	phase = "Provisioning"
	reason = "Provisioning"
	message = "Waiting for deployment replicas to be ready"

	podList := &corev1.PodList{}
	err := r.List(ctx, podList, client.InNamespace(agent.Namespace), client.MatchingLabels{"app": agent.Name + "-gateway"})
	if err != nil || len(podList.Items) == 0 {
		return phase, reason, message
	}

	for _, pod := range podList.Items {
		// 1. Check container waiting states (CrashLoopBackOff, ImagePullBackOff, ErrImagePull, etc.)
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.State.Waiting != nil && cs.State.Waiting.Reason != "" && cs.State.Waiting.Reason != "ContainerCreating" {
				phase = "Degraded"
				reason = cs.State.Waiting.Reason
				message = fmt.Sprintf("Container '%s' in pod %s is waiting: %s - %s", cs.Name, pod.Name, cs.State.Waiting.Reason, cs.State.Waiting.Message)
				return phase, reason, message
			}
		}

		// 2. Check pod scheduling conditions (Unschedulable due to node selector/affinity/gVisor)
		for _, cond := range pod.Status.Conditions {
			if cond.Type == corev1.PodScheduled && cond.Status == corev1.ConditionFalse && cond.Reason == "Unschedulable" {
				phase = "Degraded"
				reason = "PodUnschedulable"
				if agent.Spec.Deployment != nil && agent.Spec.Deployment.RuntimeClassName != nil && *agent.Spec.Deployment.RuntimeClassName != "" {
					rcName := *agent.Spec.Deployment.RuntimeClassName
					message = fmt.Sprintf("Pod %s is waiting to be scheduled because no nodes in the cluster match the requested RuntimeClass '%s'. For GKE Standard, enable GKE Sandbox by provisioning a gVisor node pool.", pod.Name, rcName)
				} else {
					cleanMsg := strings.TrimSuffix(strings.TrimSpace(cond.Message), ".")
					message = fmt.Sprintf("Pod %s cannot be scheduled onto any available node: %s.", pod.Name, cleanMsg)
				}
				return phase, reason, message
			}
		}
	}

	return phase, reason, message
}

func (r *AgentReconciler) validateRuntimeClass(ctx context.Context, agent *agentv1alpha1.Agent) error {
	if agent.Spec.Deployment == nil || agent.Spec.Deployment.RuntimeClassName == nil || *agent.Spec.Deployment.RuntimeClassName == "" {
		return nil
	}

	rcName := *agent.Spec.Deployment.RuntimeClassName
	rc := &nodev1.RuntimeClass{}
	err := r.Get(ctx, types.NamespacedName{Name: rcName}, rc)
	if err != nil {
		return err
	}
	return nil
}

func (r *AgentReconciler) updateStatusDegraded(ctx context.Context, agent *agentv1alpha1.Agent, reason, message string) error {
	agent.Status.Phase = "Degraded"
	now := metav1.Now()
	agent.Status.LastReconcileTime = &now

	condition := metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            message,
		LastTransitionTime: now,
	}
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
	return r.Status().Update(ctx, agent)
}

// SetupWithManager sets up the controller with the Manager.
func (r *AgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// The controller does not own the agent ServiceAccount or any RBAC — those are pre-created and
	// GitOps-managed (P1-T4/T5). It watches only the workload resources it renders.
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.Agent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Named("agent").
		Complete(r)
}
