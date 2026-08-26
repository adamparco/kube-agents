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

package verify

import (
	"context"
	"fmt"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// fakeProber answers every capability from fields, and returns ErrProbeUnsupported for the ones a
// test leaves nil -- which is exactly what a partially-wired broker does, and the reason
// VerdictIndeterminate exists.
type fakeProber struct {
	obj      *unstructured.Unstructured
	getErr   error
	restarts *int64
	restErr  error

	endpoints  *int
	endErr     error
	address    string
	addrErr    error
	reachable  map[string]bool
	connErr    error
	enforcing  *bool
	enfErr     error
	provider   *ProviderStatus
	provErr    error
	allowed    map[string]bool
	accessErr  error
	getCalls   int
	accessSeen []AccessQuery
}

func (f *fakeProber) Get(context.Context, agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	f.getCalls++
	if f.getErr != nil {
		return nil, f.getErr
	}
	if f.obj == nil {
		return nil, ErrProbeUnsupported
	}
	return f.obj, nil
}

func (f *fakeProber) RestartCount(context.Context, agentv1alpha1.TargetRef) (int64, error) {
	if f.restErr != nil {
		return 0, f.restErr
	}
	if f.restarts == nil {
		return 0, ErrProbeUnsupported
	}
	return *f.restarts, nil
}

func (f *fakeProber) EndpointCount(context.Context, agentv1alpha1.TargetRef) (int, error) {
	if f.endErr != nil {
		return 0, f.endErr
	}
	if f.endpoints == nil {
		return 0, ErrProbeUnsupported
	}
	return *f.endpoints, nil
}

func (f *fakeProber) ProgrammedAddress(context.Context, agentv1alpha1.TargetRef) (string, error) {
	if f.addrErr != nil {
		return "", f.addrErr
	}
	return f.address, nil
}

func (f *fakeProber) Connectivity(_ context.Context, p ConnectivityProbe) (bool, error) {
	if f.connErr != nil {
		return false, f.connErr
	}
	if f.reachable == nil {
		return false, ErrProbeUnsupported
	}
	return f.reachable[fmt.Sprintf("%s->%s:%d", p.From, p.To, p.Port)], nil
}

func (f *fakeProber) AdmissionEnforcing(context.Context, agentv1alpha1.TargetRef) (bool, error) {
	if f.enfErr != nil {
		return false, f.enfErr
	}
	if f.enforcing == nil {
		return false, ErrProbeUnsupported
	}
	return *f.enforcing, nil
}

func (f *fakeProber) ProviderState(context.Context, agentv1alpha1.TargetRef) (ProviderStatus, error) {
	if f.provErr != nil {
		return ProviderStatus{}, f.provErr
	}
	if f.provider == nil {
		return ProviderStatus{}, ErrProbeUnsupported
	}
	return *f.provider, nil
}

func (f *fakeProber) AccessReview(_ context.Context, q AccessQuery) (bool, error) {
	f.accessSeen = append(f.accessSeen, q)
	if f.accessErr != nil {
		return false, f.accessErr
	}
	if f.allowed == nil {
		return false, ErrProbeUnsupported
	}
	return f.allowed[q.User+":"+q.Verb+":"+q.Resource], nil
}

func i64(v int64) *int64 { return &v }
func ip(v int) *int      { return &v }
func bp(v bool) *bool    { return &v }

func ref(group, kind, name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Group: group, Version: "v1", Kind: kind, Namespace: "prod", Name: name}
}

func obj(group, kind, name string, spec, status map[string]any) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{"name": name, "namespace": "prod", "generation": int64(7)},
	}}
	if group == "" {
		u.Object["apiVersion"] = "v1"
	} else {
		u.Object["apiVersion"] = group + "/v1"
	}
	u.Object["kind"] = kind
	if spec != nil {
		u.Object["spec"] = spec
	}
	if status != nil {
		u.Object["status"] = status
	}
	return u
}

func evaluate(t *testing.T, p *fakeProber, target Target) Evaluation {
	t.Helper()
	return PredicateFor(target.Ref)(context.Background(), p, target)
}

// --- row 1: Deployment / StatefulSet ------------------------------------------------------------

func TestWorkloadPredicate(t *testing.T) {
	deploy := func(observed, available int64) *unstructured.Unstructured {
		return obj("apps", "Deployment", "web",
			map[string]any{"replicas": int64(3)},
			map[string]any{"observedGeneration": observed, "availableReplicas": available})
	}

	t.Run("satisfied", func(t *testing.T) {
		p := &fakeProber{obj: deploy(7, 3), restarts: i64(4)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(4)})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})

	t.Run("observedGeneration behind", func(t *testing.T) {
		p := &fakeProber{obj: deploy(6, 3), restarts: i64(4)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(4)})
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})

	t.Run("replicas short", func(t *testing.T) {
		p := &fakeProber{obj: deploy(7, 2), restarts: i64(4)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(4)})
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})

	t.Run("new restarts during the window", func(t *testing.T) {
		// The availability numbers are identical to the satisfied case. Without this half, a rollout
		// into a crashloop verifies clean for as long as the old pods keep serving.
		p := &fakeProber{obj: deploy(7, 3), restarts: i64(9)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(4)})
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending — restarts rose 4 -> 9", ev.Verdict)
		}
	})

	t.Run("no baseline is indeterminate, not satisfied", func(t *testing.T) {
		p := &fakeProber{obj: deploy(7, 3), restarts: i64(4)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web")})
		if ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})

	t.Run("absent spec.replicas defaults to one", func(t *testing.T) {
		u := obj("apps", "Deployment", "web", map[string]any{},
			map[string]any{"observedGeneration": int64(7), "availableReplicas": int64(1)})
		p := &fakeProber{obj: u, restarts: i64(0)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})

	t.Run("gone after the action", func(t *testing.T) {
		p := &fakeProber{getErr: apierrors.NewNotFound(
			schema.GroupResource{Group: "apps", Resource: "deployments"}, "web")}
		ev := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)})
		if ev.Verdict != VerdictFailed {
			t.Fatalf("verdict = %s, want Failed", ev.Verdict)
		}
	})

	t.Run("StatefulSet uses the same row", func(t *testing.T) {
		u := obj("apps", "StatefulSet", "db", map[string]any{"replicas": int64(3)},
			map[string]any{"observedGeneration": int64(7), "availableReplicas": int64(3)})
		p := &fakeProber{obj: u, restarts: i64(0)}
		ev := evaluate(t, p, Target{Ref: ref("apps", "StatefulSet", "db"), BaselineRestarts: i64(0)})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})
}

// TestWorkloadPendingReadsItsOwnConditions covers the case where waiting is pointless: the object
// already says why it will not converge.
func TestWorkloadPendingReadsItsOwnConditions(t *testing.T) {
	u := obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
		map[string]any{
			"observedGeneration": int64(7),
			"availableReplicas":  int64(0),
			"conditions": []any{map[string]any{
				"type":    "ReplicaFailure",
				"status":  "True",
				"message": `pods "web-" is forbidden: exceeded quota: compute-resources`,
			}},
		})
	p := &fakeProber{obj: u, restarts: i64(0)}
	ev := evaluate(t, p, Target{
		Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0), Capacity: CapacityExhausted,
	})
	if ev.Cause != CauseQuotaExhausted {
		t.Fatalf("cause = %s, want QuotaExhausted from the ReplicaFailure condition", ev.Cause)
	}
	if DispositionOf(ev.Cause) != Terminal {
		t.Error("a quota-rejected rollout is being waited out")
	}

	// The T-10 control: the same condition with no capacity signal must NOT read as exhausted.
	ev2 := evaluate(t, p, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)})
	if DispositionOf(ev2.Cause) != Transient {
		t.Errorf("with capacity unknown the cause is %s (%s); 09 §12 T-10 forbids assuming exhausted",
			ev2.Cause, DispositionOf(ev2.Cause))
	}
}

// --- row 2: DaemonSet ---------------------------------------------------------------------------

func TestDaemonSetPredicate(t *testing.T) {
	ds := func(desired, ready int64) *unstructured.Unstructured {
		return obj("apps", "DaemonSet", "node-agent", nil, map[string]any{
			"observedGeneration": int64(7), "desiredNumberScheduled": desired, "numberReady": ready,
		})
	}
	if ev := evaluate(t, &fakeProber{obj: ds(5, 5)}, Target{Ref: ref("apps", "DaemonSet", "node-agent")}); ev.Verdict != VerdictSatisfied {
		t.Errorf("5/5 ready is %s (%s)", ev.Verdict, ev.Detail)
	}
	if ev := evaluate(t, &fakeProber{obj: ds(5, 4)}, Target{Ref: ref("apps", "DaemonSet", "node-agent")}); ev.Verdict != VerdictPending {
		t.Errorf("4/5 ready is %s, want Pending", ev.Verdict)
	}
	stale := obj("apps", "DaemonSet", "node-agent", nil, map[string]any{
		"observedGeneration": int64(6), "desiredNumberScheduled": int64(5), "numberReady": int64(5),
	})
	if ev := evaluate(t, &fakeProber{obj: stale}, Target{Ref: ref("apps", "DaemonSet", "node-agent")}); ev.Verdict != VerdictPending {
		t.Errorf("a stale observedGeneration with 5/5 old pods ready is %s, want Pending", ev.Verdict)
	}
}

// --- row 3: Service / Ingress / Gateway ---------------------------------------------------------

func TestReachabilityPredicateRequiresBoth(t *testing.T) {
	svc := obj("", "Service", "web", nil, map[string]any{})
	target := Target{Ref: agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: "prod", Name: "web"}}

	t.Run("satisfied", func(t *testing.T) {
		p := &fakeProber{obj: svc, endpoints: ip(3), address: "10.0.0.7"}
		if ev := evaluate(t, p, target); ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})
	t.Run("address without endpoints serves errors", func(t *testing.T) {
		p := &fakeProber{obj: svc, endpoints: ip(0), address: "10.0.0.7"}
		if ev := evaluate(t, p, target); ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})
	t.Run("endpoints without an address serve nobody", func(t *testing.T) {
		p := &fakeProber{obj: svc, endpoints: ip(3), address: ""}
		if ev := evaluate(t, p, target); ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})
	t.Run("Ingress and Gateway use the same row", func(t *testing.T) {
		for _, r := range []agentv1alpha1.TargetRef{
			ref("networking.k8s.io", "Ingress", "web"),
			ref("gateway.networking.k8s.io", "Gateway", "web"),
		} {
			p := &fakeProber{obj: obj(r.Group, r.Kind, "web", nil, map[string]any{}),
				endpoints: ip(1), address: "203.0.113.4"}
			if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictSatisfied {
				t.Errorf("%s: verdict = %s (%s)", r.Kind, ev.Verdict, ev.Detail)
			}
		}
	})
}

// --- row 4: NetworkPolicy -----------------------------------------------------------------------

func TestConnectivityPredicateNeedsBothDirections(t *testing.T) {
	r := ref("networking.k8s.io", "NetworkPolicy", "allow-web")
	np := obj("networking.k8s.io", "NetworkPolicy", "allow-web", nil, nil)

	allowed := ConnectivityProbe{From: "front", To: "web", Port: 8080, WantReachable: true}
	denied := ConnectivityProbe{From: "other", To: "web", Port: 8080, WantReachable: false}

	t.Run("satisfied", func(t *testing.T) {
		p := &fakeProber{obj: np, reachable: map[string]bool{"front->web:8080": true}}
		ev := evaluate(t, p, Target{Ref: r, Probes: []ConnectivityProbe{allowed, denied}})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})

	t.Run("denial-only probes are indeterminate", func(t *testing.T) {
		// A policy that blocks everything passes a denial-only probe set. This is the failure mode of
		// every allowlist ever written, and the reason 04 §5.1 says "affirmative".
		p := &fakeProber{obj: np, reachable: map[string]bool{}}
		ev := evaluate(t, p, Target{Ref: r, Probes: []ConnectivityProbe{denied}})
		if ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})

	t.Run("allow-only probes are indeterminate", func(t *testing.T) {
		p := &fakeProber{obj: np, reachable: map[string]bool{"front->web:8080": true}}
		ev := evaluate(t, p, Target{Ref: r, Probes: []ConnectivityProbe{allowed}})
		if ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})

	t.Run("no probes at all", func(t *testing.T) {
		p := &fakeProber{obj: np}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})

	t.Run("the denied path is actually reachable", func(t *testing.T) {
		p := &fakeProber{obj: np, reachable: map[string]bool{
			"front->web:8080": true, "other->web:8080": true,
		}}
		ev := evaluate(t, p, Target{Ref: r, Probes: []ConnectivityProbe{allowed, denied}})
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending — the policy is not blocking what it claims", ev.Verdict)
		}
	})
}

// --- row 5: ResourceQuota / LimitRange ----------------------------------------------------------

func TestEnforcementPredicateRejectsMerePresence(t *testing.T) {
	r := agentv1alpha1.TargetRef{Version: "v1", Kind: "ResourceQuota", Namespace: "prod", Name: "compute"}
	rq := obj("", "ResourceQuota", "compute", nil, map[string]any{})

	if ev := evaluate(t, &fakeProber{obj: rq, enforcing: bp(true)}, Target{Ref: r}); ev.Verdict != VerdictSatisfied {
		t.Errorf("verdict = %s (%s)", ev.Verdict, ev.Detail)
	}
	// Presence alone is the "the API call returned 200" answer 04 §5.1 opens by forbidding.
	if ev := evaluate(t, &fakeProber{obj: rq, enforcing: bp(false)}, Target{Ref: r}); ev.Verdict != VerdictPending {
		t.Errorf("a stored-but-unenforced quota is %s, want Pending", ev.Verdict)
	}
	lr := agentv1alpha1.TargetRef{Version: "v1", Kind: "LimitRange", Namespace: "prod", Name: "defaults"}
	p := &fakeProber{obj: obj("", "LimitRange", "defaults", nil, map[string]any{}), enforcing: bp(true)}
	if ev := evaluate(t, p, Target{Ref: lr}); ev.Verdict != VerdictSatisfied {
		t.Errorf("LimitRange: verdict = %s (%s)", ev.Verdict, ev.Detail)
	}
}

// --- row 6: node pool / cluster -----------------------------------------------------------------

func TestProviderPredicateRequiresNodesToRegister(t *testing.T) {
	r := ref("container.cnrm.cloud.google.com", "ContainerNodePool", "pool-1")

	t.Run("satisfied", func(t *testing.T) {
		p := &fakeProber{provider: &ProviderStatus{State: "RUNNING", AtTargetState: true, NodesReady: 3, NodesExpected: 3}}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})
	t.Run("provider says RUNNING but no node registered", func(t *testing.T) {
		// The exact failure this row exists to catch.
		p := &fakeProber{provider: &ProviderStatus{State: "RUNNING", AtTargetState: true, NodesReady: 0, NodesExpected: 3}}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})
	t.Run("provider not at target state", func(t *testing.T) {
		p := &fakeProber{provider: &ProviderStatus{State: "RECONCILING", NodesReady: 3, NodesExpected: 3}}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending", ev.Verdict)
		}
	})
	t.Run("unknown expected count is indeterminate", func(t *testing.T) {
		p := &fakeProber{provider: &ProviderStatus{State: "RUNNING", AtTargetState: true}}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})
}

// --- row 7: RBAC --------------------------------------------------------------------------------

func TestAccessPredicateVerifiesTheIntendedAnswer(t *testing.T) {
	r := ref("rbac.authorization.k8s.io", "RoleBinding", "dev-edit")

	t.Run("a grant verifies by yes", func(t *testing.T) {
		p := &fakeProber{obj: obj("rbac.authorization.k8s.io", "RoleBinding", "dev-edit", nil, nil),
			allowed: map[string]bool{"alice:patch:deployments": true}}
		ev := evaluate(t, p, Target{Ref: r, Access: []AccessQuery{
			{User: "alice", Verb: "patch", Resource: "deployments", WantAllowed: true},
		}})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})

	t.Run("a revocation verifies by no", func(t *testing.T) {
		// "the intended answer", not "allowed" -- a removal of permission that still returns yes has
		// not taken effect, and the object being stored says nothing about it.
		p := &fakeProber{obj: obj("rbac.authorization.k8s.io", "RoleBinding", "dev-edit", nil, nil),
			allowed: map[string]bool{}}
		ev := evaluate(t, p, Target{Ref: r, Access: []AccessQuery{
			{User: "mallory", Verb: "delete", Resource: "secrets", WantAllowed: false},
		}})
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s)", ev.Verdict, ev.Detail)
		}
	})

	t.Run("a revocation that did not take", func(t *testing.T) {
		p := &fakeProber{obj: obj("rbac.authorization.k8s.io", "RoleBinding", "dev-edit", nil, nil),
			allowed: map[string]bool{"mallory:delete:secrets": true}}
		ev := evaluate(t, p, Target{Ref: r, Access: []AccessQuery{
			{User: "mallory", Verb: "delete", Resource: "secrets", WantAllowed: false},
		}})
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s, want Pending — the permission is still live", ev.Verdict)
		}
	})

	t.Run("no query is indeterminate", func(t *testing.T) {
		p := &fakeProber{obj: obj("rbac.authorization.k8s.io", "RoleBinding", "dev-edit", nil, nil)}
		if ev := evaluate(t, p, Target{Ref: r}); ev.Verdict != VerdictIndeterminate {
			t.Fatalf("verdict = %s, want Indeterminate", ev.Verdict)
		}
	})

	t.Run("all four RBAC kinds route here", func(t *testing.T) {
		for _, kind := range []string{"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"} {
			p := &fakeProber{obj: obj("rbac.authorization.k8s.io", kind, "x", nil, nil)}
			ev := evaluate(t, p, Target{Ref: ref("rbac.authorization.k8s.io", kind, "x")})
			if ev.Name != "access-review" {
				t.Errorf("%s routed to predicate %q, want access-review", kind, ev.Name)
			}
		}
	})
}

// --- row 8: custom resources --------------------------------------------------------------------

func TestCustomResourcePredicate(t *testing.T) {
	r := ref("example.com", "Widget", "w1")

	withReady := func(status string) *unstructured.Unstructured {
		return obj("example.com", "Widget", "w1", nil, map[string]any{
			"conditions": []any{map[string]any{"type": "Ready", "status": status}},
		})
	}
	if ev := evaluate(t, &fakeProber{obj: withReady("True")}, Target{Ref: r}); ev.Verdict != VerdictSatisfied {
		t.Errorf("Ready=True is %s", ev.Verdict)
	}
	if ev := evaluate(t, &fakeProber{obj: withReady("False")}, Target{Ref: r}); ev.Verdict != VerdictPending {
		t.Errorf("Ready=False is %s, want Pending", ev.Verdict)
	}
	if ev := evaluate(t, &fakeProber{obj: withReady("Unknown")}, Target{Ref: r}); ev.Verdict != VerdictPending {
		t.Errorf("Ready=Unknown is %s, want Pending", ev.Verdict)
	}
	// No Ready condition: presence is all 04 §5.1 claims for this row.
	bare := obj("example.com", "Widget", "w1", nil, nil)
	if ev := evaluate(t, &fakeProber{obj: bare}, Target{Ref: r}); ev.Verdict != VerdictSatisfied {
		t.Errorf("a conditionless CR is %s, want Satisfied on presence", ev.Verdict)
	}
	// ...but absence is a failure, not a pass.
	gone := &fakeProber{getErr: apierrors.NewNotFound(schema.GroupResource{Resource: "widgets"}, "w1")}
	if ev := evaluate(t, gone, Target{Ref: r}); ev.Verdict != VerdictFailed {
		t.Errorf("a missing CR is %s, want Failed", ev.Verdict)
	}
}

// --- routing and probe failures -----------------------------------------------------------------

// TestEveryKindResolvesToAPredicate is the guard on the "verified by returning 200" failure: a kind
// with no row must fall to the custom-resource row, never to nothing.
func TestEveryKindResolvesToAPredicate(t *testing.T) {
	for _, r := range []agentv1alpha1.TargetRef{
		ref("apps", "Deployment", "x"),
		ref("apps", "StatefulSet", "x"),
		ref("apps", "DaemonSet", "x"),
		{Version: "v1", Kind: "Service", Name: "x"},
		ref("networking.k8s.io", "Ingress", "x"),
		ref("gateway.networking.k8s.io", "Gateway", "x"),
		ref("networking.k8s.io", "NetworkPolicy", "x"),
		{Version: "v1", Kind: "ResourceQuota", Name: "x"},
		{Version: "v1", Kind: "LimitRange", Name: "x"},
		ref("container.cnrm.cloud.google.com", "ContainerNodePool", "x"),
		ref("rbac.authorization.k8s.io", "ClusterRole", "x"),
		ref("nobody.example.com", "SomethingNew", "x"),
		{Version: "v1", Kind: "ConfigMap", Name: "x"},
	} {
		if PredicateFor(r) == nil {
			t.Errorf("%s/%s resolves to no predicate", r.Group, r.Kind)
		}
	}
}

// TestGroupIsSignificantInRouting: a Gateway in a mesh's own group is not the gateway-api row.
func TestGroupIsSignificantInRouting(t *testing.T) {
	p := &fakeProber{obj: obj("networking.istio.io", "Gateway", "mesh", nil, nil)}
	ev := evaluate(t, p, Target{Ref: ref("networking.istio.io", "Gateway", "mesh")})
	if ev.Name != "custom-resource-ready" {
		t.Errorf("an istio Gateway routed to %q, want the custom-resource row", ev.Name)
	}
}

func TestUnsupportedProbesAreIndeterminateNotSatisfied(t *testing.T) {
	// A partially-wired broker must be visible. The alternative -- a missing capability reading as a
	// pass -- is the single most dangerous default in this package.
	cases := []struct {
		name   string
		prober *fakeProber
		target Target
	}{
		{"restart count", &fakeProber{
			obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(1)},
				map[string]any{"observedGeneration": int64(7), "availableReplicas": int64(1)}),
		}, Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)}},

		{"endpoint count", &fakeProber{obj: obj("", "Service", "web", nil, nil)},
			Target{Ref: agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: "prod", Name: "web"}}},

		{"admission enforcement", &fakeProber{obj: obj("", "ResourceQuota", "compute", nil, nil)},
			Target{Ref: agentv1alpha1.TargetRef{Version: "v1", Kind: "ResourceQuota", Namespace: "prod", Name: "compute"}}},

		{"provider state", &fakeProber{}, Target{Ref: ref("container.cnrm.cloud.google.com", "ContainerCluster", "c")}},

		{"connectivity", &fakeProber{obj: obj("networking.k8s.io", "NetworkPolicy", "p", nil, nil)},
			Target{Ref: ref("networking.k8s.io", "NetworkPolicy", "p"), Probes: []ConnectivityProbe{
				{From: "a", To: "b", Port: 80, WantReachable: true},
				{From: "c", To: "b", Port: 80, WantReachable: false},
			}}},

		{"access review", &fakeProber{obj: obj("rbac.authorization.k8s.io", "Role", "r", nil, nil)},
			Target{Ref: ref("rbac.authorization.k8s.io", "Role", "r"), Access: []AccessQuery{
				{User: "alice", Verb: "get", Resource: "pods", WantAllowed: true},
			}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := evaluate(t, tc.prober, tc.target)
			if ev.Verdict != VerdictIndeterminate {
				t.Fatalf("an unsupported %s probe gave %s (%s), want Indeterminate",
					tc.name, ev.Verdict, ev.Detail)
			}
		})
	}
}

func TestProbeErrorsAreClassified(t *testing.T) {
	svcRef := agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: "prod", Name: "web"}

	// A transient API error keeps the predicate Pending, so the settle window still governs.
	p := &fakeProber{obj: obj("", "Service", "web", nil, nil),
		endErr: apierrors.NewTooManyRequests("slow down", 1)}
	if ev := evaluate(t, p, Target{Ref: svcRef}); ev.Verdict != VerdictPending || ev.Cause != CauseThrottled {
		t.Errorf("throttled endpoint probe gave %s/%s, want Pending/Throttled", ev.Verdict, ev.Cause)
	}

	// A terminal one fails immediately rather than waiting out the window.
	p2 := &fakeProber{obj: obj("", "Service", "web", nil, nil),
		endErr: apierrors.NewBadRequest("malformed")}
	if ev := evaluate(t, p2, Target{Ref: svcRef}); ev.Verdict != VerdictFailed {
		t.Errorf("a bad-request endpoint probe gave %s, want Failed", ev.Verdict)
	}

	// A wrapped ErrProbeUnsupported is still recognized.
	p3 := &fakeProber{obj: obj("", "Service", "web", nil, nil),
		endErr: fmt.Errorf("endpoints: %w", ErrProbeUnsupported)}
	if ev := evaluate(t, p3, Target{Ref: svcRef}); ev.Verdict != VerdictIndeterminate {
		t.Errorf("a wrapped ErrProbeUnsupported gave %s, want Indeterminate", ev.Verdict)
	}
}

// nilNilProber returns neither an object nor an error, which is a bug in a prober. Reading it as
// "the object is fine" would verify an action against nothing at all.
type nilNilProber struct{ *fakeProber }

func (nilNilProber) Get(context.Context, agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	return nil, nil
}

func TestProberReturningNeitherObjectNorError(t *testing.T) {
	p := nilNilProber{&fakeProber{}}
	ev := PredicateFor(ref("apps", "Deployment", "web"))(
		context.Background(), p,
		Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)})
	if ev.Verdict != VerdictIndeterminate {
		t.Fatalf("a nil-nil Get gave %s (%s), want Indeterminate", ev.Verdict, ev.Detail)
	}
}

// --- settle windows -----------------------------------------------------------------------------

func TestSettleWindowsAreBoundedAndPerKind(t *testing.T) {
	for _, tc := range []struct {
		ref  agentv1alpha1.TargetRef
		want time.Duration
	}{
		{ref("apps", "Deployment", "x"), 5 * time.Minute},
		{ref("apps", "StatefulSet", "x"), 10 * time.Minute},
		{agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Name: "x"}, 90 * time.Second},
		{agentv1alpha1.TargetRef{Version: "v1", Kind: "ResourceQuota", Name: "x"}, 15 * time.Second},
		{ref("networking.k8s.io", "NetworkPolicy", "x"), 30 * time.Second},
		{ref("container.cnrm.cloud.google.com", "ContainerCluster", "x"), 30 * time.Minute},
		{ref("rbac.authorization.k8s.io", "RoleBinding", "x"), rbacSettleWindow},
		{ref("nobody.example.com", "SomethingNew", "x"), DefaultSettleWindow},
	} {
		if got := SettleWindow(tc.ref); got != tc.want {
			t.Errorf("SettleWindow(%s/%s) = %s, want %s", tc.ref.Group, tc.ref.Kind, got, tc.want)
		}
	}
}

// TestNoSettleWindowExceedsTheCeiling is the T-9 code ceiling. It reads the table rather than a
// hand-copied list, so a future row that types an extra zero is caught here.
func TestNoSettleWindowExceedsTheCeiling(t *testing.T) {
	for k := range settleWindows {
		r := agentv1alpha1.TargetRef{Group: k.Group, Kind: k.Kind}
		if got := SettleWindow(r); got > MaxSettleWindow {
			t.Errorf("SettleWindow(%s/%s) = %s, above the %s ceiling", k.Group, k.Kind, got, MaxSettleWindow)
		}
		if got := SettleWindow(r); got <= 0 {
			t.Errorf("SettleWindow(%s/%s) = %s; a non-positive window verifies nothing", k.Group, k.Kind, got)
		}
	}
	if got := clampWindow(72 * time.Hour); got != MaxSettleWindow {
		t.Errorf("clampWindow(72h) = %s, want the ceiling", got)
	}
	if got := clampWindow(0); got != DefaultSettleWindow {
		t.Errorf("clampWindow(0) = %s, want the default", got)
	}
	if got := clampWindow(-time.Minute); got != DefaultSettleWindow {
		t.Errorf("clampWindow(-1m) = %s, want the default", got)
	}
}
