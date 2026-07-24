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

package router

import (
	"context"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Reconciler keeps the routing Index in sync with the Agent CRs via a controller-runtime informer. It is
// STRICTLY READ-ONLY on the cluster: it only Gets Agents and mutates the in-memory Index — it never
// writes any object. The router's RBAC (config/rbac) therefore grants get/list/watch on agents and
// nothing else, which is what makes the ChatOps gateway itself a read-only participant (05 C15).
type Reconciler struct {
	client.Client
	Index *Index
}

// Reconcile upserts the agent into the routing table, or removes it when the CR is gone or terminating.
// A NotFound (the object was deleted before we observed it) and a set deletionTimestamp both evict the
// route, so a deleted agent stops being addressable immediately rather than lingering as a phantom.
func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (reconcile.Result, error) {
	a := &agentv1alpha1.Agent{}
	if err := r.Get(ctx, req.NamespacedName, a); err != nil {
		if apierrors.IsNotFound(err) {
			r.Index.Remove(req.NamespacedName)
			return reconcile.Result{}, nil
		}
		return reconcile.Result{}, err
	}
	if !a.DeletionTimestamp.IsZero() {
		r.Index.Remove(req.NamespacedName)
		return reconcile.Result{}, nil
	}
	r.Index.Upsert(a)
	return reconcile.Result{}, nil
}

// SetupWithManager wires the reconciler to watch Agent CRs. The manager's cache backs the informer, so
// the watch is get/list/watch only.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.Agent{}).
		Named("router-index").
		Complete(r)
}
