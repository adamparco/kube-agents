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

package probe

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// nodePoolLabel is the label GKE stamps on every node, naming the pool it belongs to. It is how the
// Kubernetes half of the node-pool row is answered without a cloud API call.
const nodePoolLabel = "cloud.google.com/gke-nodepool"

// ProviderState is the cloud provider's own answer for a node pool or cluster.
//
// # There is no cloud API call here, and that is deliberate
//
// 04 §5.1's row reads "Provider reports the target state AND nodes register Ready", and the obvious
// implementation calls container.googleapis.com. This one does not, because the targets the broker
// acts on are Config Connector custom resources -- `ContainerNodePool` and `ContainerCluster` --
// and Config Connector's whole job is to reconcile the cloud resource and write what it found back
// into the CR's status. Reading the CR is reading the provider's answer, one hop later.
//
// Three things follow, and the third is the one that matters:
//
//   - The broker needs no cloud credential to verify a cloud change. Its Workload Identity binding
//     stays scoped to the cluster, which keeps 03's blast radius argument intact.
//   - The answer is as fresh as Config Connector's reconcile loop, not as fresh as GCP. A settle
//     window of 20 minutes for a node pool absorbs that; a shorter one would not.
//   - A stale Ready is the failure mode, so `observedGeneration` is checked. A `ContainerNodePool`
//     whose Ready condition was written BEFORE this action still says True, and reporting it would
//     verify every node-pool change instantly and correctly-looking. That check is the difference
//     between this method and a status echo.
func (s *Source) ProviderState(ctx context.Context, ref agentv1alpha1.TargetRef) (verify.ProviderStatus, error) {
	if s.Client == nil {
		return verify.ProviderStatus{}, s.noClient("the provider state of " + describe(ref))
	}
	if ref.Group != "container.cnrm.cloud.google.com" {
		return verify.ProviderStatus{}, fmt.Errorf("%s is not a provider resource this prober can "+
			"read; the 04 §5.1 provider row covers ContainerNodePool and ContainerCluster in "+
			"container.cnrm.cloud.google.com: %w", describe(ref), verify.ErrProbeUnsupported)
	}

	obj, err := s.Get(ctx, ref)
	if err != nil {
		return verify.ProviderStatus{}, fmt.Errorf("reading %s for its provider status: %w",
			describe(ref), err)
	}

	state, atTarget := readyState(obj)
	out := verify.ProviderStatus{State: state, AtTargetState: atTarget}

	switch ref.Kind {
	case "ContainerNodePool":
		out.NodesReady, out.NodesExpected, err = s.nodePoolCounts(ctx, obj, ref.Name)
	case "ContainerCluster":
		out.NodesReady, out.NodesExpected, err = s.clusterNodeCounts(ctx)
	default:
		return verify.ProviderStatus{}, fmt.Errorf("%s is in the provider group but is not a node "+
			"pool or a cluster: %w", describe(ref), verify.ErrProbeUnsupported)
	}
	if err != nil {
		return verify.ProviderStatus{}, err
	}
	return out, nil
}

// readyState reads the Config Connector Ready condition and decides whether it describes the
// current generation.
//
// The condition's `reason` is used as the State string rather than its `status`, because "True" is
// not something to put in front of an operator: Config Connector's reasons are `UpToDate`,
// `Updating`, `UpdateFailed`, `DependencyNotReady`, and those are the provider states 04 §5.1 means.
//
// `AtTargetState` requires BOTH Ready=True and status.observedGeneration == metadata.generation. A
// missing observedGeneration is treated as not-caught-up rather than as caught-up: the field is
// absent exactly while the controller has not yet written status for this object, which is when a
// stale Ready is most likely.
func readyState(obj *unstructured.Unstructured) (string, bool) {
	conds, _, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	status, reason := "", ""
	for _, raw := range conds {
		c, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if typ, _ := c["type"].(string); typ != "Ready" {
			continue
		}
		status, _ = c["status"].(string)
		reason, _ = c["reason"].(string)
		break
	}
	if status == "" {
		return "no Ready condition yet", false
	}
	state := reason
	if state == "" {
		state = "Ready=" + status
	}
	if status != "True" {
		return state, false
	}

	gen, _, _ := unstructured.NestedInt64(obj.Object, "metadata", "generation")
	observed, found, _ := unstructured.NestedInt64(obj.Object, "status", "observedGeneration")
	if !found || observed < gen {
		return fmt.Sprintf("%s (from generation %d, not the current %d)", state, observed, gen), false
	}
	return state, true
}

// nodePoolCounts is the Kubernetes half of the node-pool row: how many of this pool's nodes have
// registered, and how many of those are Ready.
//
// # The denominator
//
// `NodesExpected` is what the pool ASKED for, not what turned up, because the failure this row
// exists to catch is "the provider says RUNNING and no node has registered". A denominator taken
// from the registered set would make that case read 0/0, which providerPredicate treats as an
// unknown rather than a failure -- honest, but it never fails.
//
// So it is `spec.nodeCount` multiplied by the number of zones the pool spans, and for an autoscaled
// pool it is `spec.autoscaling.minNodeCount` -- the smallest count the pool is allowed to sit at,
// which is the strongest claim that is true regardless of load. A pool with neither yields 0, and
// providerPredicate says Indeterminate, which is the correct answer for "the CR does not say how
// many nodes it wants".
func (s *Source) nodePoolCounts(ctx context.Context, obj *unstructured.Unstructured, pool string) (int, int, error) {
	var nodes corev1.NodeList
	if err := s.Client.List(ctx, &nodes,
		client.MatchingLabelsSelector{Selector: oneLabel(nodePoolLabel, pool)},
	); err != nil {
		return 0, 0, fmt.Errorf("listing the nodes of pool %s: %w", pool, err)
	}
	return countReady(nodes.Items), expectedNodes(obj), nil
}

// clusterNodeCounts is the ContainerCluster arm.
//
// A cluster CR declares no node count of its own -- the counts live in its node pools -- so the
// denominator here is the registered node set. That is a weaker claim than the node-pool arm's and
// the weakness is specific: this arm CANNOT catch a node that never registered, only one that
// registered and is not Ready. Verifying a cluster-level change by way of its pools would be the
// stronger form, and it needs the CR-to-pool relationship, which is a lookup this method does not
// do. Recorded rather than papered over.
func (s *Source) clusterNodeCounts(ctx context.Context) (int, int, error) {
	var nodes corev1.NodeList
	if err := s.Client.List(ctx, &nodes); err != nil {
		return 0, 0, fmt.Errorf("listing the cluster's nodes: %w", err)
	}
	return countReady(nodes.Items), len(nodes.Items), nil
}

// countReady counts nodes whose Ready condition is True.
func countReady(nodes []corev1.Node) int {
	n := 0
	for i := range nodes {
		for _, c := range nodes[i].Status.Conditions {
			if c.Type == corev1.NodeReady && c.Status == corev1.ConditionTrue {
				n++
				break
			}
		}
	}
	return n
}

// expectedNodes reads the declared size of a node pool. Zero means the CR does not say.
func expectedNodes(obj *unstructured.Unstructured) int {
	zones := 1
	if locs, found, _ := unstructured.NestedStringSlice(obj.Object, "spec", "nodeLocations"); found && len(locs) > 0 {
		zones = len(locs)
	}
	if enabled, found, _ := unstructured.NestedBool(obj.Object, "spec", "autoscaling", "enabled"); found && enabled {
		if minimum, found, _ := unstructured.NestedInt64(obj.Object, "spec", "autoscaling", "minNodeCount"); found {
			return int(minimum) * zones
		}
		return 0
	}
	// An autoscaling block with no `enabled` field is still autoscaling in the Config Connector
	// schema, where the field defaults to true when the block is present at all.
	if minimum, found, _ := unstructured.NestedInt64(obj.Object, "spec", "autoscaling", "minNodeCount"); found {
		return int(minimum) * zones
	}
	if count, found, _ := unstructured.NestedInt64(obj.Object, "spec", "nodeCount"); found {
		return int(count) * zones
	}
	return 0
}
