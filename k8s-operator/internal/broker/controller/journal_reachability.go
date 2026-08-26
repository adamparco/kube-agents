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

	authzv1 "k8s.io/api/authorization/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// This file is the writer for `status.broker.journalReachable` (06 §4.4 row 3).
//
// # Why the writer is here and not in the broker
//
// Row 3 reads "Cannot reach the journal store | Refuse to execute; set
// `status.broker.journalReachable: false`; auto-pause". The broker performs the refusal
// (`internal/broker/brake.go`) and, since P9-T9c-1, the auto-pause. It cannot perform the middle
// clause, and that is not an oversight to be fixed by widening a Role:
//
//   - 06 §2.2.1's broker-operations grant gives the broker `[get, list, watch]` on `agents` and
//     nothing else. V-BRK-013 asserts that grant byte-for-byte and is BLOCKING-ALWAYS.
//   - 06 §2.2's actor templates grant `update`/`patch` on `agents` to the platform and
//     cluster-admin tiers -- for provisioning their CHILDREN -- but `status` is a subresource
//     ([[LSN-061]]) and no template anywhere grants `agents/status`. The developer-team template
//     has no verb on `agents` at all.
//
// So no broker at any tier can write this field, and the operator is the only principal that can.
//
// # Why the operator's own observation is evidence and not a proxy
//
// The obvious objection -- recorded in `agent_controller.go` for five phases, and correct as far as
// it went -- is that the controller would have to ask the broker, and a broker answering "yes"
// proves nothing about the broker's own writes. That is an argument against ONE transport, an HTTP
// self-report, not against the controller observing anything at all.
//
// What makes a controller-side observation real is 05 §1.2: the journal store is not a service, it
// is the `ActionRecord` CRD in the cluster's own etcd. "For a Cluster Admin or Developer Team Agent
// the journal lives in the same etcd as the objects it describes"; for the platform tier it lives
// in the hub cluster's etcd -- which is the cluster this operator runs in. The store the broker
// probes and the store the operator probes are the same store, always.
//
// # The three observations, and why the conjunction is the honest answer
//
// Each of the three is a distinct way row 3 fires, and each is a question the operator can put to
// the API server rather than to the broker:
//
//  1. **The broker is running** (`ready`, already computed by `updateStatusReady`). A broker that
//     is not up is not reaching anything. This also covers total loss of the broker's API path,
//     because `brake.NewSource`'s startup `Refresh` is synchronous: a broker that cannot read the
//     API server fails at boot rather than on the first envelope.
//  2. **The store answers** -- an uncached `List` of `ActionRecord` in the agent's namespace,
//     capped at one item. Deliberately the same shape as `brake.Source.probe`: the question is
//     reachability, not content.
//  3. **The broker's identity is still allowed to write it** -- a `SubjectAccessReview` for the
//     actor ServiceAccount on `create actionrecords` in that namespace. This is the one that stops
//     the whole thing from being a proxy. It is not the operator's connectivity restated; it is the
//     API server's own authoritative answer about the BROKER's authority, obtained by the one
//     principal that may ask.
//
// The value is the conjunction and every unknown resolves to false. That direction is forced, not
// chosen: `BrokerStatus.JournalReachable`'s own field comment already argues that "an agent whose
// status has never been written has not demonstrated that its journal is reachable, and the
// fail-closed reading of 'unknown' is 'unreachable'".
//
// # What this cannot see, stated so nobody has to rediscover it
//
// A broker whose POD-LEVEL NETWORK PATH to the API server breaks after boot, while its pod stays
// Ready. The broker's probes are `tcpSocket` on its own listener (`broker_manifests.go` explains
// why: the mTLS listener demands a client certificate the kubelet does not have), so a bound port
// says nothing about egress. In that state the broker knows and the operator does not.
//
// Closing that residue needs a transport from the broker to something the broker may write, and the
// grant above is exactly the list of things it may write: `actionrecords` and
// `actionrecords/status`. Both are the journal, which is the surface that is down. A heartbeat
// record would be a fourth thing in an append-only audit trail whose contents are evidence, which
// 05 §1.2 does not sanction. The residue is therefore recorded, not papered over.

// journalAuthorizer creates `SubjectAccessReview`s. An interface rather than `client.Client` so a
// unit test can answer the question -- the fake client accepts the object and never fills in
// `Status.Allowed`, so a test built on it can only ever exercise the denied leg.
type journalAuthorizer interface {
	Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error
}

// journalReachable answers observations 2 and 3 above; the caller supplies observation 1.
//
// It returns the value and the reason, and the reason is always populated on a false so the log
// line names WHICH of the three failed. "The journal is unreachable" with no discriminator sends an
// operator to read broker logs for an RBAC change the API server could have told them about.
func (r *AgentReconciler) journalReachable(ctx context.Context, agent *agentv1alpha1.Agent, brokerReady bool) (bool, string) {
	if !brokerReady {
		return false, "the broker Deployment is not ready; a broker that is not running is not reaching the journal"
	}
	if r.APIReader == nil || r.Authorizer == nil {
		// Wiring, not a cluster condition -- and it fails closed rather than defaulting to the
		// cached client, because a `List` served out of an informer cache is the exact false green
		// `brake.MaxFreezeStaleness` argues against: "a watch that silently stopped delivering is
		// not an error at all -- the informer's List succeeds, the cache answers instantly, and
		// every answer is from before the incident started."
		return false, "the controller has no uncached reader or no authorizer wired; journal reachability was not observed"
	}

	var probe agentv1alpha1.ActionRecordList
	if err := r.APIReader.List(ctx, &probe, client.InNamespace(agent.Namespace), client.Limit(1)); err != nil {
		return false, fmt.Sprintf("listing ActionRecord in %s failed: %v", agent.Namespace, err)
	}

	actor := actorServiceAccountName(agent)
	if actor == "" {
		return false, "the actor ServiceAccount name could not be derived, so the broker's authority to write the journal is unknown"
	}
	allowed, err := r.actorMayJournal(ctx, agent.Namespace, actor)
	if err != nil {
		return false, fmt.Sprintf("asking whether %s may create ActionRecord in %s failed: %v", actor, agent.Namespace, err)
	}
	if !allowed {
		return false, fmt.Sprintf("%s is not authorized to create ActionRecord in %s (06 §2.2.1); the broker can read the journal and cannot write it",
			actor, agent.Namespace)
	}
	return true, ""
}

// actorMayJournal puts observation 3 to the API server.
//
// The verb is `create` on `actionrecords`, which is the write 06 §2.2.1 grants the broker and the
// one whose loss makes the journal unwritable. It is NOT `actionrecords/status`: a broker that can
// create but not advance a phase is a different, later failure, and asking the wrong question here
// would report a healthy journal as unreachable. The subresource, when one is ever needed, goes in
// the `Subresource` FIELD -- concatenating it into `Resource` asks about a resource by that literal
// name and always answers no ([[LSN-044]]).
func (r *AgentReconciler) actorMayJournal(ctx context.Context, namespace, actor string) (bool, error) {
	user := "system:serviceaccount:" + namespace + ":" + actor
	sar := &authzv1.SubjectAccessReview{
		Spec: authzv1.SubjectAccessReviewSpec{
			User: user,
			// The groups the API server itself attaches to a ServiceAccount token. Supplying them
			// is what makes this review answer the question the broker's real requests ask; a
			// review with the user alone silently ignores every RoleBinding written against
			// `system:serviceaccounts:<ns>`, and would report a working broker as unauthorized.
			Groups: []string{
				"system:serviceaccounts",
				"system:serviceaccounts:" + namespace,
				"system:authenticated",
			},
			ResourceAttributes: &authzv1.ResourceAttributes{
				Namespace: namespace,
				Verb:      "create",
				Group:     agentv1alpha1.GroupVersion.Group,
				Resource:  "actionrecords",
			},
		},
	}
	if err := r.Authorizer.Create(ctx, sar); err != nil {
		return false, err
	}
	return sar.Status.Allowed, nil
}
