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
	"errors"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	authzv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// errStoreDown is what an unreachable journal store looks like from the caller's side: the List
// simply does not come back with an answer. 06 §4.4 row 3 says "cannot reach", not "was denied", so
// the failure injected here is transport-shaped rather than RBAC-shaped -- the RBAC leg has its own
// observation and its own test below.
var errStoreDown = errors.New("Get \"https://10.0.0.1:443/apis/kubeagents.x-k8s.io/v1alpha1/actionrecords\": dial tcp: i/o timeout")

// failingReader is an APIReader whose List never succeeds.
type failingReader struct {
	client.Reader
	err error
}

func (f failingReader) List(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error {
	return f.err
}

func (f failingReader) Get(_ context.Context, _ client.ObjectKey, _ client.Object, _ ...client.GetOption) error {
	return f.err
}

// recordingAuthorizer answers a SubjectAccessReview the way the API server would, and keeps the
// reviews it was asked so a test can assert the QUESTION and not only the answer. A probe that
// asks the wrong question and gets "yes" is indistinguishable from a working one.
type recordingAuthorizer struct {
	allow    bool
	err      error
	reviewed []authzv1.SubjectAccessReviewSpec
}

func (a *recordingAuthorizer) Create(_ context.Context, obj client.Object, _ ...client.CreateOption) error {
	sar, ok := obj.(*authzv1.SubjectAccessReview)
	if !ok {
		return errors.New("recordingAuthorizer: not a SubjectAccessReview")
	}
	a.reviewed = append(a.reviewed, sar.Spec)
	if a.err != nil {
		return a.err
	}
	sar.Status.Allowed = a.allow
	return nil
}

func journalProbeAgent() *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "team-x-agent", Namespace: "team-x"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  "developer-team",
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: "adamparco-kage", ClusterName: "cluster-a", Namespace: "team-x"},
		},
	}
}

// TestJournalReachableRequiresAllThreeObservations is the table of 06 §4.4 row 3's writer: the
// value is a conjunction and every leg can veto it alone.
func TestJournalReachableRequiresAllThreeObservations(t *testing.T) {
	scheme := setupScheme()
	agent := journalProbeAgent()

	okReader := fake.NewClientBuilder().WithScheme(scheme).Build()

	cases := []struct {
		name        string
		brokerReady bool
		reader      client.Reader
		authz       *recordingAuthorizer
		want        bool
		wantReason  string
	}{
		{
			name:        "all three hold",
			brokerReady: true,
			reader:      okReader,
			authz:       &recordingAuthorizer{allow: true},
			want:        true,
		},
		{
			name:        "the broker is not running",
			brokerReady: false,
			reader:      okReader,
			authz:       &recordingAuthorizer{allow: true},
			want:        false,
			wantReason:  "not ready",
		},
		{
			name:        "the store does not answer",
			brokerReady: true,
			reader:      failingReader{err: errStoreDown},
			authz:       &recordingAuthorizer{allow: true},
			want:        false,
			wantReason:  "listing ActionRecord",
		},
		{
			name:        "the broker's identity may no longer write the journal",
			brokerReady: true,
			reader:      okReader,
			authz:       &recordingAuthorizer{allow: false},
			want:        false,
			wantReason:  "not authorized",
		},
		{
			name:        "the review itself could not be answered",
			brokerReady: true,
			reader:      okReader,
			authz:       &recordingAuthorizer{err: errStoreDown},
			want:        false,
			wantReason:  "failed",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := &AgentReconciler{APIReader: tc.reader, Authorizer: tc.authz}
			got, reason := r.journalReachable(context.Background(), agent, tc.brokerReady)
			if got != tc.want {
				t.Errorf("journalReachable = %v (%s), want %v", got, reason, tc.want)
			}
			if tc.want {
				if reason != "" {
					t.Errorf("a reachable journal carried a reason %q; the reason exists to explain a false", reason)
				}
				return
			}
			if reason == "" {
				t.Fatal("journalReachable reported false with no reason; an operator cannot tell which of the three legs failed")
			}
			if !strings.Contains(reason, tc.wantReason) {
				t.Errorf("reason = %q, want it to name %q -- a reason that does not discriminate sends the operator to the wrong incident",
					reason, tc.wantReason)
			}
		})
	}
}

// TestJournalProbeShortCircuitsOnAnUnreadyBroker pins the order. The two API calls exist to observe
// a RUNNING broker's journal; making them for a broker that is not running spends two requests per
// agent per minute, for every agent in a fleet that is still installing, to learn nothing.
func TestJournalProbeShortCircuitsOnAnUnreadyBroker(t *testing.T) {
	authz := &recordingAuthorizer{allow: true}
	r := &AgentReconciler{
		// A reader that would panic if touched: the assertion is that it is not touched.
		APIReader:  failingReader{err: errors.New("the probe listed ActionRecord for a broker that is not running")},
		Authorizer: authz,
	}
	got, reason := r.journalReachable(context.Background(), journalProbeAgent(), false)
	if got {
		t.Fatalf("journalReachable = true for a broker that is not ready (%s)", reason)
	}
	if len(authz.reviewed) != 0 {
		t.Errorf("the probe issued %d SubjectAccessReviews for a broker that is not running, want 0", len(authz.reviewed))
	}
}

// TestJournalProbeAsksAboutTheBrokersIdentity is the test that keeps this from being a proxy.
//
// Every other leg measures something about the OPERATOR -- its connectivity, its view of a
// Deployment. This one is the only question that is about the broker, and it is only about the
// broker if the review names the actor ServiceAccount, the agent's namespace, and the verb the
// broker actually needs. Assert the question, not the answer.
func TestJournalProbeAsksAboutTheBrokersIdentity(t *testing.T) {
	scheme := setupScheme()
	agent := journalProbeAgent()
	authz := &recordingAuthorizer{allow: true}
	r := &AgentReconciler{
		APIReader:  fake.NewClientBuilder().WithScheme(scheme).Build(),
		Authorizer: authz,
	}

	if ok, reason := r.journalReachable(context.Background(), agent, true); !ok {
		t.Fatalf("journalReachable = false on a healthy fixture: %s", reason)
	}
	if len(authz.reviewed) != 1 {
		t.Fatalf("reviews issued = %d, want exactly 1", len(authz.reviewed))
	}
	spec := authz.reviewed[0]

	// The expected principal is written out in full rather than rebuilt from
	// `actorServiceAccountName`. A value compared against the function that produced it agrees with
	// every wrong answer that function could give ([[LSN-034]]) -- and the identity the broker runs
	// as is derived from tier + scope in exactly one place, so a literal here is the second opinion.
	const wantUser = "system:serviceaccount:team-x:developer-team-team-x-actor"
	if spec.User != wantUser {
		t.Errorf("review user = %q, want %q: the reader SA, the operator, or a literal name would all answer a question about the wrong principal",
			spec.User, wantUser)
	}
	if got := actorServiceAccountName(agent); "system:serviceaccount:team-x:"+got != wantUser {
		t.Errorf("the fixture's actor SA is %q, so this test's literal no longer names the identity the broker pod runs as", got)
	}
	// The groups the API server attaches to a real ServiceAccount token. Without them the review
	// ignores every RoleBinding written against `system:serviceaccounts:<ns>` and reports a
	// working broker as unauthorized.
	for _, want := range []string{"system:serviceaccounts", "system:serviceaccounts:team-x", "system:authenticated"} {
		found := false
		for _, g := range spec.Groups {
			if g == want {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("review groups %v are missing %q", spec.Groups, want)
		}
	}
	if spec.ResourceAttributes == nil {
		t.Fatal("the review carried no ResourceAttributes; a review with none asks about nothing and is allowed by default")
	}
	ra := spec.ResourceAttributes
	if ra.Verb != "create" {
		t.Errorf("review verb = %q, want create: 06 §2.2.1 grants the broker get/list/watch/create, and it is `create` whose loss makes the journal unwritable", ra.Verb)
	}
	if ra.Resource != "actionrecords" || ra.Group != agentv1alpha1.GroupVersion.Group {
		t.Errorf("review resource = %s/%s, want %s/actionrecords", ra.Group, ra.Resource, agentv1alpha1.GroupVersion.Group)
	}
	if ra.Subresource != "" {
		t.Errorf("review subresource = %q; asking about actionrecords/status is a different, later failure and would report a healthy journal as unreachable", ra.Subresource)
	}
	if ra.Namespace != "team-x" {
		t.Errorf("review namespace = %q, want the agent's own namespace team-x", ra.Namespace)
	}
}

// TestJournalReachableReachesAgentStatus is the wiring test, and it is the one that would have
// caught the five-phase gap this unit closes: `BrokerStatus.JournalReachable` was a declared CRD
// field with readers and no writer, and nothing went red, because a probe that is never consulted
// looks exactly like a probe that answers false.
func TestJournalReachableReachesAgentStatus(t *testing.T) {
	scheme := setupScheme()

	for _, tc := range []struct {
		name  string
		allow bool
		want  bool
	}{
		{"the broker may write the journal", true, true},
		{"the broker's journal grant is gone", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := journalProbeAgent()
			brokerDep := &appsv1.Deployment{
				ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-broker", Namespace: agent.Namespace},
				Status:     appsv1.DeploymentStatus{ReadyReplicas: 1},
			}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithStatusSubresource(&agentv1alpha1.Agent{}).
				WithObjects(agent, brokerDep).
				Build()

			r := &AgentReconciler{
				Client:     cl,
				Scheme:     scheme,
				APIReader:  cl,
				Authorizer: &recordingAuthorizer{allow: tc.allow},
			}
			if err := r.updateStatusReady(context.Background(), agent); err != nil {
				t.Fatalf("updateStatusReady: %v", err)
			}

			var got agentv1alpha1.Agent
			if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), &got); err != nil {
				t.Fatalf("reading the Agent back: %v", err)
			}
			if got.Status.Broker == nil {
				t.Fatal("status.broker was not written at all")
			}
			if !got.Status.Broker.Ready {
				t.Fatal("the fixture's broker Deployment has a ready replica but status.broker.ready is false; the rest of this test would be measuring the wrong leg")
			}
			if got.Status.Broker.JournalReachable != tc.want {
				t.Errorf("status.broker.journalReachable = %v, want %v -- 06 §4.4 row 3 requires this field to follow the observation, not to sit at its zero",
					got.Status.Broker.JournalReachable, tc.want)
			}
		})
	}
}

// TestJournalProbeWithNoDependenciesWiredIsUnreachable holds the fail-closed direction at the one
// place a future edit is most likely to reach for a convenient default: a nil APIReader is a
// wiring bug, and the tempting repair -- fall back to r.Client -- answers the probe out of an
// informer cache, which is the false green `brake.MaxFreezeStaleness` exists to argue against.
func TestJournalProbeWithNoDependenciesWiredIsUnreachable(t *testing.T) {
	scheme := setupScheme()
	agent := journalProbeAgent()

	for _, tc := range []struct {
		name string
		r    *AgentReconciler
	}{
		{"no reader", &AgentReconciler{Authorizer: &recordingAuthorizer{allow: true}}},
		{"no authorizer", &AgentReconciler{APIReader: fake.NewClientBuilder().WithScheme(scheme).Build()}},
		{"neither", &AgentReconciler{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, reason := tc.r.journalReachable(context.Background(), agent, true)
			if got {
				t.Fatal("journalReachable = true with a dependency unwired; unknown must read as unreachable")
			}
			if !strings.Contains(reason, "not observed") {
				t.Errorf("reason = %q, want it to say the value was not observed rather than describing a cluster condition", reason)
			}
		})
	}
}
