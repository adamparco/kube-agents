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

package execute

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The API-server-backed Reader and Applier.
//
// Everything above this file is testable without a cluster, which is the point of the seams; this
// is the one place that talks to a real API server, and it is deliberately thin. The rules live in
// apply.go and integrity.go, not here -- a rule implemented inside the client is a rule that only
// an envtest can see.

// ClientReader reads live objects through a controller-runtime client.
type ClientReader struct {
	Client client.Client
}

var _ Reader = (*ClientReader)(nil)

// Get returns the live object. A NotFound propagates unwrapped so CaptureAll can narrow it.
func (r *ClientReader) Get(ctx context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	obj := objectFor(ref)
	key := client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}
	if err := r.Client.Get(ctx, key, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

// ClientApplier is the production Applier.
type ClientApplier struct {
	Client client.Client

	// DryRunUnsupported reports targets whose API does not honour dry-run. Optional; nil means the
	// server honours it everywhere, which is true of the built-in groups and of CRDs.
	//
	// It is a hook rather than a discovery call because the answer is deployment-specific
	// (an aggregated API server the operator does not know about) and because getting it WRONG in
	// the permissive direction is the dangerous one: claiming dry-run support that does not exist
	// means the "executed diff" is whatever the server returned from a request it actually
	// performed. An operator that knows of such an API says so here.
	DryRunUnsupported func(agentv1alpha1.TargetRef) bool
}

var _ Applier = (*ClientApplier)(nil)

// Apply performs a server-side apply.
//
// Ownership is NOT forced. A conflict means another manager owns a field this apply would change,
// and that is exactly the `contested` signal of 03 §6 -- forcing would resolve it by taking the
// field, silently, which is the opposite of what a broker whose job is attribution should do. The
// conflict surfaces as an error and goes to the recovery ladder.
func (a *ClientApplier) Apply(ctx context.Context, obj *unstructured.Unstructured, fieldManager string, dryRun bool) (*unstructured.Unstructured, error) {
	out := obj.DeepCopy()
	opts := []client.PatchOption{client.FieldOwner(fieldManager)}
	if dryRun {
		opts = append(opts, client.DryRunAll)
	}
	if err := a.Client.Patch(ctx, out, client.Apply, opts...); err != nil {
		return nil, err
	}
	return out, nil
}

// Create makes an object that does not exist, and FAILS if the name is taken.
//
// Deliberately not on the Applier interface, and no execution path calls it: an agent's `create`
// verb goes through Apply like every other write, because server-side apply is what carries the
// field-manager attribution the whole broker is built around. The one caller is the rollback
// replayer, and it needs this method for the property Apply does not have. A recreate step reverses
// a delete, so it carries no uid precondition -- the uid died with the object -- and its ONLY
// protection against restoring a snapshot on top of a stranger who has since taken the name is that
// a create returns AlreadyExists. Apply at that same name merges the snapshot into the stranger's
// object and reports success where their fields do not collide, and raises a field-ownership
// conflict where they do; neither answer tells the operator that the object is gone.
//
// It lives here rather than in the rollback package because this file is the single place in the
// broker that talks to an API server, and a second client opened next to it would be a second set
// of answers to the same questions. LSN-040.
func (a *ClientApplier) Create(ctx context.Context, obj *unstructured.Unstructured, fieldManager string) (*unstructured.Unstructured, error) {
	out := obj.DeepCopy()
	if err := a.Client.Create(ctx, out, client.FieldOwner(fieldManager)); err != nil {
		return nil, err
	}
	return out, nil
}

// Patch applies a raw patch of the given media type.
func (a *ClientApplier) Patch(ctx context.Context, ref agentv1alpha1.TargetRef, patchType string, body []byte, fieldManager string, dryRun bool) (*unstructured.Unstructured, error) {
	pt, err := patchTypeOf(patchType)
	if err != nil {
		return nil, err
	}
	out := objectFor(ref)
	out.SetName(ref.Name)
	out.SetNamespace(ref.Namespace)

	opts := []client.PatchOption{client.FieldOwner(fieldManager)}
	if dryRun {
		opts = append(opts, client.DryRunAll)
	}
	if err := a.Client.Patch(ctx, out, client.RawPatch(pt, body), opts...); err != nil {
		return nil, err
	}
	return out, nil
}

// Scale sets replicas through the scale subresource and returns the PARENT object.
//
// For a dry run the parent is re-read and the requested replica count composed onto it. That is a
// computation the broker performs, which everywhere else in this package is the thing not to do --
// it is admissible here and only here because the scale subresource has exactly one writable field.
// A patch's merge can touch anything; a scale cannot.
func (a *ClientApplier) Scale(ctx context.Context, ref agentv1alpha1.TargetRef, replicas int32, fieldManager string, dryRun bool) (*unstructured.Unstructured, error) {
	parent := objectFor(ref)
	key := client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}
	if err := a.Client.Get(ctx, key, parent); err != nil {
		return nil, err
	}

	scale := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "autoscaling/v1",
		"kind":       "Scale",
		"metadata": map[string]any{
			"name":      ref.Name,
			"namespace": ref.Namespace,
		},
		"spec": map[string]any{"replicas": int64(replicas)},
	}}

	opts := []client.SubResourceUpdateOption{client.FieldOwner(fieldManager), client.WithSubResourceBody(scale)}
	if dryRun {
		opts = append(opts, client.DryRunAll)
	}
	if err := a.Client.SubResource("scale").Update(ctx, parent, opts...); err != nil {
		return nil, err
	}

	out := parent.DeepCopy()
	if dryRun {
		if err := unstructured.SetNestedField(out.Object, int64(replicas), "spec", "replicas"); err != nil {
			return nil, err
		}
		return out, nil
	}
	if err := a.Client.Get(ctx, key, out); err != nil {
		return nil, err
	}
	return out, nil
}

// Delete removes the object, with the UID precondition when one is pinned.
func (a *ClientApplier) Delete(ctx context.Context, ref agentv1alpha1.TargetRef, opts DeleteOpts, dryRun bool) error {
	obj := objectFor(ref)
	obj.SetName(ref.Name)
	obj.SetNamespace(ref.Namespace)

	var dopts []client.DeleteOption
	if dryRun {
		dopts = append(dopts, client.DryRunAll)
	}
	if opts.UID != "" {
		uid := types.UID(opts.UID)
		dopts = append(dopts, client.Preconditions{UID: &uid})
	}
	if opts.GracePeriodSeconds != nil {
		dopts = append(dopts, client.GracePeriodSeconds(*opts.GracePeriodSeconds))
	}
	if opts.PropagationPolicy != "" {
		p, err := propagationOf(opts.PropagationPolicy)
		if err != nil {
			return err
		}
		dopts = append(dopts, p)
	}
	return a.Client.Delete(ctx, obj, dopts...)
}

// SupportsDryRun defaults to true and defers to the configured hook.
func (a *ClientApplier) SupportsDryRun(_ context.Context, ref agentv1alpha1.TargetRef) bool {
	if a.DryRunUnsupported == nil {
		return true
	}
	return !a.DryRunUnsupported(ref)
}

// objectFor builds an empty typed-by-GVK unstructured for a target.
func objectFor(ref agentv1alpha1.TargetRef) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	apiVersion := ref.Version
	if ref.Group != "" {
		apiVersion = ref.Group + "/" + ref.Version
	}
	obj.SetAPIVersion(apiVersion)
	obj.SetKind(ref.Kind)
	return obj
}

// patchTypeOf maps the three accepted media types of 06 §4.1. Unknown types are rejected rather
// than defaulted: defaulting a media type decides how a payload merges, which is the one decision
// the classifier's answer depends on.
func patchTypeOf(mediaType string) (types.PatchType, error) {
	switch types.PatchType(mediaType) {
	case types.JSONPatchType:
		return types.JSONPatchType, nil
	case types.MergePatchType:
		return types.MergePatchType, nil
	case types.StrategicMergePatchType:
		return types.StrategicMergePatchType, nil
	default:
		return "", fmt.Errorf("unsupported patch media type %q", mediaType)
	}
}

func propagationOf(policy string) (client.PropagationPolicy, error) {
	switch policy {
	case "Orphan", "Background", "Foreground":
		return client.PropagationPolicy(policy), nil
	default:
		return "", fmt.Errorf("unsupported propagation policy %q", policy)
	}
}
