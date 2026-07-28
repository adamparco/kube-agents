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

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/intstr"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/probe"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// Everything in this file needs a real API server, and the reason is the same one execute's envtest
// file gives: this package's whole subject is what the cluster answers. A fake client agrees with
// whatever shape the caller assumed, so a green over a fake would be a green about code that never
// ran ([[LSN-001]]'s shape, one layer in).
//
// Four of the properties here are unavailable at any lower level even in principle:
//
//   - the LimitRanger admission plugin actually refusing a pod. There is nothing in this repository
//     that could produce that refusal, and the two-leg probe in enforce.go is built entirely around
//     being unable to recognise it from the error text.
//   - a SubjectAccessReview returning a real authorization decision. envtest runs the apiserver with
//     --authorization-mode=RBAC, so a subject with no binding is genuinely denied and one with a
//     RoleBinding is genuinely allowed.
//   - a recreated object getting a NEW uid at the same name, which is what the Get pin exists for.
//   - server-side label selection over matchExpressions, which is where a naive matchLabels read
//     silently becomes the empty selector.
//
// envtest is L1 by binding.md §Targets: a real API server, process-local, no cluster. The L2
// instance of V-PRO-027 runs the same properties against gke-scratch-kube-agents-dev, where the
// discovery surface, RBAC and admission chain are the real ones.

// k8s is a WithWatch rather than a plain Client only so that TestAccessReviewRefusesAnIncompleteEvaluation
// can wrap it in an interceptor; every other test uses it as the client.Client it also is.
var (
	testEnv *envtest.Environment
	k8s     client.WithWatch
	scheme  = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		fmt.Fprintln(os.Stderr,
			"KUBEBUILDER_ASSETS unset; run via `make test` to exercise the prober against a real API server")
		os.Exit(0)
	}
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		panic(err)
	}

	testEnv = &envtest.Environment{Scheme: scheme, CRDs: []*apiextensionsv1.CustomResourceDefinition{nodePoolCRD()}}
	cfg, err := testEnv.Start()
	if err != nil {
		panic(fmt.Sprintf("start envtest: %v", err))
	}
	k8s, err = client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(fmt.Sprintf("new client: %v", err))
	}
	code := m.Run()
	_ = testEnv.Stop()
	os.Exit(code)
}

// --- Get, and the pin that makes it more than a wrapper ------------------------------------------

func TestGetRefusesAnObjectThatWasReplacedDuringTheSettleWindow(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "pinned"}}
	mustCreate(t, ctx, cm)
	ref := agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: ns, Name: "pinned"}
	original := string(cm.UID)

	t.Run("a matching uid reads normally", func(t *testing.T) {
		pinned := ref
		pinned.UID = original
		got, err := s.Get(ctx, pinned)
		if err != nil {
			t.Fatalf("get with the live uid: %v", err)
		}
		if string(got.GetUID()) != original {
			t.Fatalf("uid %s, want %s", got.GetUID(), original)
		}
	})

	t.Run("an empty uid skips the pin, because a create has none", func(t *testing.T) {
		if _, err := s.Get(ctx, ref); err != nil {
			t.Fatalf("get with no uid: %v", err)
		}
	})

	// The property. Nothing else in this package could catch it: the replacement is a healthy
	// ConfigMap at the right name, and every other probe would report it as verified.
	t.Run("a replaced object is refused by uid, not accepted by name", func(t *testing.T) {
		if err := k8s.Delete(ctx, cm); err != nil {
			t.Fatalf("delete: %v", err)
		}
		replacement := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "pinned"}}
		mustCreate(t, ctx, replacement)
		if string(replacement.UID) == original {
			t.Fatalf("the API server reused uid %s, so this test proves nothing", original)
		}

		pinned := ref
		pinned.UID = original
		_, err := s.Get(ctx, pinned)
		if err == nil {
			t.Fatal("a replaced object was accepted; the settle-window replacement is exactly the " +
				"case where the wrong object looks healthy")
		}
		if !strings.Contains(err.Error(), "replaced during the settle window") {
			t.Fatalf("error does not name the cause an operator has to act on: %v", err)
		}
	})

	t.Run("NotFound passes through unwrapped for verify.mustGet", func(t *testing.T) {
		missing := ref
		missing.Name = "no-such-configmap"
		_, err := s.Get(ctx, missing)
		if !apierrors.IsNotFound(err) {
			t.Fatalf("want an IsNotFound error so verify can say \"does not exist after the "+
				"action\"; got %v", err)
		}
	})
}

// --- RestartCount: zero is the passing answer, so it may never be a guess -------------------------

func TestRestartCountCountsThePodsOfTheWorkloadAndOnlyThose(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	// matchExpressions with an EMPTY matchLabels. A selector read as `spec.selector.matchLabels`
	// yields {} here, which compiles to the everything-selector -- so the stranger pod below is the
	// control that distinguishes "counted this workload" from "counted the namespace".
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "web"},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
				Key: "app", Operator: metav1.LabelSelectorOpIn, Values: []string{"web"},
			}}},
			Template: podTemplate(map[string]string{"app": "web"}),
		},
	}
	mustCreate(t, ctx, dep)

	makePod(t, ctx, ns, "web-1", map[string]string{"app": "web"}, 3, 0)
	makePod(t, ctx, ns, "web-2", map[string]string{"app": "web"}, 0, 4) // init-container restarts
	makePod(t, ctx, ns, "other", map[string]string{"app": "other"}, 100, 0)

	ref := agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "web"}
	got, err := s.RestartCount(ctx, ref)
	if err != nil {
		t.Fatalf("restart count: %v", err)
	}
	// 3 app-container restarts on web-1 plus 4 init-container restarts on web-2. Not 107.
	if got != 7 {
		t.Fatalf("restart count %d, want 7; 107 means the selector matched the whole namespace and "+
			"0 or 3 means init-container restarts were dropped", got)
	}
}

func TestRestartCountErrorsRatherThanReportingZero(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	mustCreate(t, ctx, &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "not-a-workload"}})

	cases := []struct {
		name string
		ref  agentv1alpha1.TargetRef
		want string
	}{{
		name: "an object with no spec.selector",
		ref:  agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: ns, Name: "not-a-workload"},
		want: "no readable spec.selector",
	}, {
		name: "a workload that does not exist",
		ref:  agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "gone"},
		want: "to find its pod selector",
	}, {
		name: "a cluster-scoped target",
		ref:  agentv1alpha1.TargetRef{Version: "v1", Kind: "Namespace", Name: ns},
		want: "has no namespace",
	}}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			n, err := s.RestartCount(ctx, tc.ref)
			if err == nil {
				t.Fatalf("got %d restarts and no error; \"I could not count\" reported as 0 is the "+
					"Deployment row passing for a crashlooping workload", n)
			}
			if n != 0 {
				t.Fatalf("an error path returned a non-zero count %d", n)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not contain %q", err, tc.want)
			}
		})
	}
}

func TestRestartCountRefusesASelectorThatMatchesEverything(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	// A PodDisruptionBudget is the shape that makes this constructible: policy/v1 explicitly permits
	// an empty selector and defines it as "every pod in the namespace", so this is a legal object an
	// agent could really be asked to create -- unlike a Deployment, whose validation rejects one.
	mustCreate(t, ctx, &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "all-pods"},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: ptr(intstr.FromInt32(1)),
			Selector:     &metav1.LabelSelector{},
		},
	})
	makePod(t, ctx, ns, "unrelated", map[string]string{"app": "other"}, 100, 0)

	n, err := s.RestartCount(ctx, agentv1alpha1.TargetRef{
		Group: "policy", Version: "v1", Kind: "PodDisruptionBudget", Namespace: ns, Name: "all-pods",
	})
	if err == nil {
		t.Fatalf("an empty selector returned %d restarts; it matches every pod in the namespace, so "+
			"the answer is some other workload's health", n)
	}
	if !strings.Contains(err.Error(), "empty selector") {
		t.Fatalf("error %q does not name the empty selector", err)
	}
}

// --- EndpointCount: zero is an observation, "no backends" is not -----------------------------------

func TestEndpointCountReadsReadyEndpointsAndWalksToBackingServices(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	mustCreate(t, ctx, clusterIPService(ns, "api"))
	mustCreate(t, ctx, clusterIPService(ns, "web"))
	mustCreate(t, ctx, clusterIPService(ns, "quiet"))

	// Two ready addresses, one explicitly unready, one with a nil Ready condition. The API's own
	// guidance is that nil means ready, so the expected total is three.
	mustCreate(t, ctx, &discoveryv1.EndpointSlice{
		ObjectMeta:  metav1.ObjectMeta{Namespace: ns, Name: "api-1", Labels: map[string]string{discoveryv1.LabelServiceName: "api"}},
		AddressType: discoveryv1.AddressTypeIPv4,
		Endpoints: []discoveryv1.Endpoint{
			{Addresses: []string{"10.0.0.1"}, Conditions: discoveryv1.EndpointConditions{Ready: ptr(true)}},
			{Addresses: []string{"10.0.0.2"}, Conditions: discoveryv1.EndpointConditions{Ready: ptr(false)}},
			{Addresses: []string{"10.0.0.3"}, Conditions: discoveryv1.EndpointConditions{Ready: ptr(true)}},
			{Addresses: []string{"10.0.0.4"}}, // nil Ready
		},
	})
	mustCreate(t, ctx, &discoveryv1.EndpointSlice{
		ObjectMeta:  metav1.ObjectMeta{Namespace: ns, Name: "web-1", Labels: map[string]string{discoveryv1.LabelServiceName: "web"}},
		AddressType: discoveryv1.AddressTypeIPv4,
		Endpoints:   []discoveryv1.Endpoint{{Addresses: []string{"10.0.1.1"}, Conditions: discoveryv1.EndpointConditions{Ready: ptr(true)}}},
	})

	svcRef := func(n string) agentv1alpha1.TargetRef {
		return agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: ns, Name: n}
	}

	t.Run("a Service counts its own slices, and nil Ready counts as ready", func(t *testing.T) {
		got, err := s.EndpointCount(ctx, svcRef("api"))
		if err != nil {
			t.Fatalf("endpoint count: %v", err)
		}
		if got != 3 {
			t.Fatalf("count %d, want 3 (two explicit ready plus one nil); 4 means the unready "+
				"endpoint was counted and 2 means nil was read as unready", got)
		}
	})

	t.Run("a Service with no slices reports zero, which is Pending and not an error", func(t *testing.T) {
		got, err := s.EndpointCount(ctx, svcRef("quiet"))
		if err != nil {
			t.Fatalf("a Service whose pods are not ready yet is not an error: %v", err)
		}
		if got != 0 {
			t.Fatalf("count %d, want 0", got)
		}
	})

	t.Run("an Ingress sums the Services it routes to", func(t *testing.T) {
		mustCreate(t, ctx, ingress(ns, "front", "api", "web"))
		got, err := s.EndpointCount(ctx, agentv1alpha1.TargetRef{
			Group: "networking.k8s.io", Version: "v1", Kind: "Ingress", Namespace: ns, Name: "front",
		})
		if err != nil {
			t.Fatalf("endpoint count: %v", err)
		}
		if got != 4 {
			t.Fatalf("count %d, want 4 (3 behind api plus 1 behind web); 0 means the walk to the "+
				"backing Services did not happen, which holds every Ingress at Pending", got)
		}
	})

	// NEGATIVE CONTROL. "Routes nowhere" and "routes to pods that are not ready" are different
	// facts with different remedies, and the predicate only sees the number.
	t.Run("a routing object with no backend errors rather than reporting zero", func(t *testing.T) {
		mustCreate(t, ctx, ingress(ns, "empty"))
		_, err := s.EndpointCount(ctx, agentv1alpha1.TargetRef{
			Group: "networking.k8s.io", Version: "v1", Kind: "Ingress", Namespace: ns, Name: "empty",
		})
		if err == nil {
			t.Fatal("an Ingress with no backend reported a count; zero here says the backends are " +
				"unready when the fact is that there are none")
		}
		if !strings.Contains(err.Error(), "names no backend Service") {
			t.Fatalf("error %q does not name the cause", err)
		}
	})
}

// --- ProgrammedAddress: every empty must be an observed empty -------------------------------------

func TestProgrammedAddressIsObservedNeverAssumed(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	clusterIP := clusterIPService(ns, "internal")
	mustCreate(t, ctx, clusterIP)

	headless := clusterIPService(ns, "headless")
	headless.Spec.ClusterIP = corev1.ClusterIPNone
	mustCreate(t, ctx, headless)

	lb := clusterIPService(ns, "public")
	lb.Spec.Type = corev1.ServiceTypeLoadBalancer
	mustCreate(t, ctx, lb)

	ref := func(n string) agentv1alpha1.TargetRef {
		return agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: ns, Name: n}
	}

	t.Run("a ClusterIP Service is addressed by its cluster IP", func(t *testing.T) {
		got, err := s.ProgrammedAddress(ctx, ref("internal"))
		if err != nil {
			t.Fatalf("programmed address: %v", err)
		}
		if got == "" || got != clusterIP.Spec.ClusterIP {
			t.Fatalf("address %q, want the assigned cluster IP %q; an empty answer here holds "+
				"every ClusterIP Service at Pending until its window expires", got, clusterIP.Spec.ClusterIP)
		}
	})

	t.Run("a headless Service is addressable the moment it exists", func(t *testing.T) {
		got, err := s.ProgrammedAddress(ctx, ref("headless"))
		if err != nil {
			t.Fatalf("programmed address: %v", err)
		}
		if !strings.Contains(got, "headless") {
			t.Fatalf("address %q, want the headless DNS form", got)
		}
	})

	t.Run("an unprovisioned LoadBalancer is empty, not its cluster IP", func(t *testing.T) {
		got, err := s.ProgrammedAddress(ctx, ref("public"))
		if err != nil {
			t.Fatalf("programmed address: %v", err)
		}
		if got != "" {
			t.Fatalf("address %q, want empty; returning the cluster IP would report an "+
				"unprovisioned load balancer as programmed", got)
		}
	})

	t.Run("a provisioned LoadBalancer reports the ingress address", func(t *testing.T) {
		live := &corev1.Service{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(lb), live); err != nil {
			t.Fatalf("get: %v", err)
		}
		live.Status.LoadBalancer.Ingress = []corev1.LoadBalancerIngress{{IP: "203.0.113.7"}}
		if err := k8s.Status().Update(ctx, live); err != nil {
			t.Fatalf("status update: %v", err)
		}
		got, err := s.ProgrammedAddress(ctx, ref("public"))
		if err != nil {
			t.Fatalf("programmed address: %v", err)
		}
		if got != "203.0.113.7" {
			t.Fatalf("address %q, want 203.0.113.7", got)
		}
	})
}

// --- AccessReview: the intended answer, and never an evaluation error --------------------------------

func TestAccessReviewReturnsRealAuthorizationDecisions(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	const allowed = "alice@example.com"
	const revoked = "mallory@example.com"

	mustCreate(t, ctx, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "reader"},
		Rules:      []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"configmaps"}, Verbs: []string{"get"}}},
	})
	mustCreate(t, ctx, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "reader"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "reader"},
		Subjects:   []rbacv1.Subject{{Kind: rbacv1.UserKind, APIGroup: rbacv1.GroupName, Name: allowed}},
	})

	q := verify.AccessQuery{Verb: "get", Resource: "configmaps", Namespace: ns}

	t.Run("a bound subject is allowed", func(t *testing.T) {
		q := q
		q.User = allowed
		got, err := s.AccessReview(ctx, q)
		if err != nil {
			t.Fatalf("access review: %v", err)
		}
		if !got {
			t.Fatal("a subject with a RoleBinding was reported as denied")
		}
	})

	// The revocation direction. 04 §5.1 says the RBAC row verifies by the review returning "the
	// intended answer", and for a revocation the intended answer is no.
	t.Run("an unbound subject is denied", func(t *testing.T) {
		q := q
		q.User = revoked
		got, err := s.AccessReview(ctx, q)
		if err != nil {
			t.Fatalf("access review: %v", err)
		}
		if got {
			t.Fatal("a subject with no binding was reported as allowed")
		}
	})

	t.Run("a query with no subject is refused rather than reviewing the anonymous user", func(t *testing.T) {
		if _, err := s.AccessReview(ctx, q); err == nil {
			t.Fatal("an empty subject was accepted")
		}
	})

	t.Run("a query with no verb or resource is refused", func(t *testing.T) {
		if _, err := s.AccessReview(ctx, verify.AccessQuery{User: allowed}); err == nil {
			t.Fatal("a query with no verb was accepted")
		}
	})
}

// The negative control for the sharpest edge in the package, and the one property here that a real
// API server will not produce on demand: an authorizer that fell over answers Allowed=false with an
// evaluationError set. Reading only Allowed makes that a deny -- and a REVOCATION verifies by a
// deny, so the broker would confirm every revocation it ever performed on exactly the days the
// authorizer was unhealthy. The interceptor is the only way to construct it, and it is confined to
// this one test rather than used as the package's client.
func TestAccessReviewRefusesAnIncompleteEvaluation(t *testing.T) {
	ctx := context.Background()

	broken := interceptor.NewClient(k8s, interceptor.Funcs{
		Create: func(_ context.Context, _ client.WithWatch, obj client.Object, _ ...client.CreateOption) error {
			sar, ok := obj.(*authorizationv1.SubjectAccessReview)
			if !ok {
				t.Fatalf("the interceptor saw a %T, not a SubjectAccessReview", obj)
			}
			sar.Status = authorizationv1.SubjectAccessReviewStatus{
				Allowed:         false,
				Denied:          true,
				EvaluationError: "webhook authorizer: Post \"https://authz\": dial tcp: i/o timeout",
			}
			return nil
		},
	})
	s := &probe.Source{Client: broken}

	allowed, err := s.AccessReview(ctx, verify.AccessQuery{
		User: "mallory@example.com", Verb: "get", Resource: "secrets", Namespace: "default",
	})
	if err == nil {
		t.Fatal("a SubjectAccessReview carrying an evaluationError was reported as a clean answer; " +
			"a partial evaluation that happened to reach a deny is still a partial evaluation, and " +
			"the RBAC row reads a deny as a verified revocation")
	}
	if allowed {
		t.Fatal("the error path also reported the request as allowed")
	}
	if !strings.Contains(err.Error(), "did not complete") {
		t.Fatalf("error %q does not say the review failed to complete", err)
	}
}

// --- AdmissionEnforcing: presence is not enforcement -----------------------------------------------

func TestQuotaEnforcementIsReadFromTheControllersStatusNotFromPresence(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	q := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "compute"},
		Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("10")}},
	}
	mustCreate(t, ctx, q)
	ref := agentv1alpha1.TargetRef{Version: "v1", Kind: "ResourceQuota", Namespace: ns, Name: "compute"}

	// envtest runs no kube-controller-manager, so the quota's status stays empty until this test
	// writes it -- which is exactly the "object present, controller has not seen it" state.
	t.Run("present but unprocessed is not enforcing", func(t *testing.T) {
		got, err := s.AdmissionEnforcing(ctx, ref)
		if err != nil {
			t.Fatalf("admission enforcing: %v", err)
		}
		if got {
			t.Fatal("a quota with an empty status was reported as enforcing; that is the " +
				"presence check 04 §5.1 says this row is not")
		}
	})

	t.Run("processed and accounting is enforcing", func(t *testing.T) {
		setQuotaStatus(t, ctx, q, corev1.ResourceList{corev1.ResourcePods: resource.MustParse("10")})
		got, err := s.AdmissionEnforcing(ctx, ref)
		if err != nil {
			t.Fatalf("admission enforcing: %v", err)
		}
		if !got {
			t.Fatal("a quota whose status matches its spec and carries usage was reported as not enforcing")
		}
	})

	// The generation half. A tightened quota is enforced at the OLD limit until the controller
	// catches up, and the window between the two is the one an operator raises a quota to close.
	t.Run("a status from the previous generation is not enforcing", func(t *testing.T) {
		live := &corev1.ResourceQuota{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(q), live); err != nil {
			t.Fatalf("get: %v", err)
		}
		live.Spec.Hard[corev1.ResourcePods] = resource.MustParse("3")
		if err := k8s.Update(ctx, live); err != nil {
			t.Fatalf("update: %v", err)
		}
		got, err := s.AdmissionEnforcing(ctx, ref)
		if err != nil {
			t.Fatalf("admission enforcing: %v", err)
		}
		if got {
			t.Fatal("a quota whose status still reports the old limit was called enforcing the new one")
		}
	})
}

func TestLimitRangeEnforcementIsObservedThroughAdmissionItself(t *testing.T) {
	ctx := context.Background()
	s := &probe.Source{Client: k8s}

	t.Run("a container max is observed by two dry runs", func(t *testing.T) {
		ns := newNamespace(t, ctx, nil)
		mustCreate(t, ctx, limitRange(ns, "caps", corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")}, nil))
		got, err := s.AdmissionEnforcing(ctx, lrRef(ns, "caps"))
		if err != nil {
			t.Fatalf("admission enforcing: %v", err)
		}
		if !got {
			t.Fatal("the LimitRanger admission plugin did not refuse a pod over the container max, " +
				"or refused the compliant one too")
		}
	})

	// NEGATIVE CONTROL, and the reason the probe has two legs. Pod Security Admission refuses BOTH
	// probe pods, so a one-leg implementation -- "submit something illegal, see it rejected" --
	// would report this LimitRange as enforcing on the strength of a rejection that has nothing to
	// do with it. The correct answer is "enforcement not observed".
	t.Run("a namespace where something else refuses everything reports not-observed", func(t *testing.T) {
		ns := newNamespace(t, ctx, map[string]string{
			"pod-security.kubernetes.io/enforce": "restricted",
		})
		mustCreate(t, ctx, limitRange(ns, "caps", corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")}, nil))
		got, err := s.AdmissionEnforcing(ctx, lrRef(ns, "caps"))
		if err != nil {
			t.Fatalf("admission enforcing: %v", err)
		}
		if got {
			t.Fatal("both dry runs were refused and the probe still claimed enforcement; the " +
				"refusal came from Pod Security Admission, not from the LimitRange")
		}
	})

	t.Run("a shape the probe cannot construct a violation for is unsupported, not false", func(t *testing.T) {
		ns := newNamespace(t, ctx, nil)
		mustCreate(t, ctx, limitRange(ns, "defaults-only", nil,
			corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m")}))
		_, err := s.AdmissionEnforcing(ctx, lrRef(ns, "defaults-only"))
		if !errors.Is(err, verify.ErrProbeUnsupported) {
			t.Fatalf("want ErrProbeUnsupported so the predicate says Indeterminate; got %v", err)
		}
	})

	t.Run("a kind outside the enforcement row is unsupported", func(t *testing.T) {
		ns := newNamespace(t, ctx, nil)
		_, err := s.AdmissionEnforcing(ctx, agentv1alpha1.TargetRef{
			Version: "v1", Kind: "ConfigMap", Namespace: ns, Name: "whatever",
		})
		if !errors.Is(err, verify.ErrProbeUnsupported) {
			t.Fatalf("want ErrProbeUnsupported; got %v", err)
		}
	})
}

func TestTheAdmissionProbeLeavesNothingBehind(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	mustCreate(t, ctx, limitRange(ns, "caps", corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")}, nil))
	if _, err := s.AdmissionEnforcing(ctx, lrRef(ns, "caps")); err != nil {
		t.Fatalf("admission enforcing: %v", err)
	}

	var pods corev1.PodList
	if err := k8s.List(ctx, &pods, client.InNamespace(ns)); err != nil {
		t.Fatalf("list pods: %v", err)
	}
	if len(pods.Items) != 0 {
		t.Fatalf("the dry-run admission probe persisted %d pod(s); a verification probe that "+
			"creates workloads in a production namespace is worse than no probe", len(pods.Items))
	}
}

// --- ProviderState: a Ready from before the action is the failure mode ------------------------------

func TestProviderStateRefusesAReadyFromThePreviousGeneration(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	// Node objects are cluster-scoped, so the pool name carries the namespace to keep this test's
	// nodes out of every other test's counts.
	name := newNodePool(t, ctx, ns, ns+"-pool", map[string]any{"nodeCount": int64(2)})
	ref := agentv1alpha1.TargetRef{
		Group: "container.cnrm.cloud.google.com", Version: "v1beta1",
		Kind: "ContainerNodePool", Namespace: ns, Name: name,
	}

	makeNode(t, ctx, name, "n1", true)
	makeNode(t, ctx, name, "n2", true)
	makeNode(t, ctx, ns+"-other-pool", "n3", true)

	t.Run("no Ready condition is not at target state", func(t *testing.T) {
		got, err := s.ProviderState(ctx, ref)
		if err != nil {
			t.Fatalf("provider state: %v", err)
		}
		if got.AtTargetState {
			t.Fatal("a node pool with no Ready condition was reported at its target state")
		}
	})

	t.Run("nodes are counted by pool label, not cluster-wide", func(t *testing.T) {
		got, err := s.ProviderState(ctx, ref)
		if err != nil {
			t.Fatalf("provider state: %v", err)
		}
		if got.NodesReady != 2 || got.NodesExpected != 2 {
			t.Fatalf("nodes %d/%d, want 2/2; 3 ready means another pool's nodes were counted",
				got.NodesReady, got.NodesExpected)
		}
	})

	// The headline property. Config Connector leaves Ready=True on the object across an update, so
	// a status echo would verify every node-pool change the instant it was submitted.
	t.Run("Ready=True from an older generation is not at target state", func(t *testing.T) {
		setNodePoolReady(t, ctx, ns, name, "UpToDate", currentGeneration(t, ctx, ns, name))
		// The action: the broker resizes the pool. Config Connector has not reconciled yet, so the
		// Ready=True it wrote for the PREVIOUS spec is still sitting on the object.
		touchNodePoolSpec(t, ctx, ns, name, 3)

		got, err := s.ProviderState(ctx, ref)
		if err != nil {
			t.Fatalf("provider state: %v", err)
		}
		if got.AtTargetState {
			t.Fatal("a Ready condition written before this action was accepted as this action's " +
				"verification")
		}
		if !strings.Contains(got.State, "not the current") {
			t.Fatalf("state %q does not tell the operator the status is stale", got.State)
		}
	})

	t.Run("Ready=True for the current generation is at target state", func(t *testing.T) {
		gen := currentGeneration(t, ctx, ns, name)
		setNodePoolReady(t, ctx, ns, name, "UpToDate", gen)

		got, err := s.ProviderState(ctx, ref)
		if err != nil {
			t.Fatalf("provider state: %v", err)
		}
		if !got.AtTargetState {
			t.Fatalf("a current Ready=True was not accepted; state %q", got.State)
		}
		if got.State != "UpToDate" {
			t.Fatalf("state %q, want the condition reason", got.State)
		}
	})

	t.Run("a kind outside the provider row is unsupported", func(t *testing.T) {
		_, err := s.ProviderState(ctx, agentv1alpha1.TargetRef{
			Version: "v1", Kind: "ConfigMap", Namespace: ns, Name: "nope",
		})
		if !errors.Is(err, verify.ErrProbeUnsupported) {
			t.Fatalf("want ErrProbeUnsupported; got %v", err)
		}
	})
}

// --- the seam: the predicates actually run against this prober --------------------------------------

// This is the LSN-040 class of test -- two packages that agree on a signature and could still
// disagree about meaning. `verify` is fully tested against its own stub prober and `probe` is fully
// tested against a real API server; nothing until here puts them in a line.
func TestTheVerifyPredicatesRunAgainstThisProber(t *testing.T) {
	ctx := context.Background()
	ns := newNamespace(t, ctx, nil)
	s := &probe.Source{Client: k8s}

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "web"},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr(int32(2)),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "web"}},
			Template: podTemplate(map[string]string{"app": "web"}),
		},
	}
	mustCreate(t, ctx, dep)
	ref := agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "web"}

	baseline := int64(0)
	target := verify.Target{Ref: ref, BaselineRestarts: &baseline}
	pred := verify.PredicateFor(ref)

	// envtest runs no deployment controller, so status is whatever this test writes -- which makes
	// both directions constructible and neither of them a race.
	t.Run("an unconverged rollout is not satisfied", func(t *testing.T) {
		ev := pred(ctx, s, target)
		if ev.Verdict == verify.VerdictSatisfied {
			t.Fatalf("a Deployment with no available replicas verified: %+v", ev)
		}
	})

	t.Run("a converged rollout with no new restarts is satisfied", func(t *testing.T) {
		live := &appsv1.Deployment{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), live); err != nil {
			t.Fatalf("get: %v", err)
		}
		live.Status = appsv1.DeploymentStatus{
			ObservedGeneration: live.Generation,
			Replicas:           2,
			AvailableReplicas:  2,
			ReadyReplicas:      2,
		}
		if err := k8s.Status().Update(ctx, live); err != nil {
			t.Fatalf("status update: %v", err)
		}
		ev := pred(ctx, s, target)
		if ev.Verdict != verify.VerdictSatisfied {
			t.Fatalf("verdict %s, want Satisfied: %s", ev.Verdict, ev.Detail)
		}
	})

	// The half that distinguishes a rollout that completed from one that completed and fell over.
	t.Run("new restarts inside the window are not satisfied", func(t *testing.T) {
		makePod(t, ctx, ns, "web-crash", map[string]string{"app": "web"}, 5, 0)
		ev := pred(ctx, s, target)
		if ev.Verdict == verify.VerdictSatisfied {
			t.Fatalf("a crashlooping workload verified: %+v", ev)
		}
	})
}

// --- the seams that must fail closed ----------------------------------------------------------------

func TestConnectivityIsUnsupportedRatherThanFalse(t *testing.T) {
	ctx := context.Background()
	s := &probe.Source{Client: k8s}

	got, err := s.Connectivity(ctx, verify.ConnectivityProbe{From: "a", To: "b", Port: 80, WantReachable: true})
	if !errors.Is(err, verify.ErrProbeUnsupported) {
		t.Fatalf("want ErrProbeUnsupported so the predicate says Indeterminate; got %v", err)
	}
	if got {
		t.Fatal("an unsupported probe reported the path as reachable")
	}

	t.Run("a wired prober is used", func(t *testing.T) {
		s := &probe.Source{Client: k8s, Dataplane: stubDataplane{reachable: true}}
		got, err := s.Connectivity(ctx, verify.ConnectivityProbe{From: "a", To: "b", Port: 80})
		if err != nil || !got {
			t.Fatalf("got %v, %v; want the stub's answer", got, err)
		}
	})
}

func TestEveryMethodRefusesANilClient(t *testing.T) {
	ctx := context.Background()
	s := &probe.Source{}
	ref := agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: "default", Name: "x"}

	calls := map[string]func() error{
		"Get":                func() error { _, err := s.Get(ctx, ref); return err },
		"RestartCount":       func() error { _, err := s.RestartCount(ctx, ref); return err },
		"EndpointCount":      func() error { _, err := s.EndpointCount(ctx, ref); return err },
		"ProgrammedAddress":  func() error { _, err := s.ProgrammedAddress(ctx, ref); return err },
		"AdmissionEnforcing": func() error { _, err := s.AdmissionEnforcing(ctx, ref); return err },
		"ProviderState":      func() error { _, err := s.ProviderState(ctx, ref); return err },
		"AccessReview": func() error {
			_, err := s.AccessReview(ctx, verify.AccessQuery{User: "u", Verb: "get", Resource: "pods"})
			return err
		},
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			if err := call(); err == nil {
				t.Fatal("a prober with no client answered; a broker assembled without one would " +
					"otherwise fail at the first settle window of the first real action")
			}
		})
	}

	// Connectivity is the exception and it is the intended one: with no client AND no dataplane
	// prober it is still ErrProbeUnsupported, because the client was never the thing that would
	// have answered it.
	if _, err := s.Connectivity(ctx, verify.ConnectivityProbe{}); !errors.Is(err, verify.ErrProbeUnsupported) {
		t.Fatalf("Connectivity: want ErrProbeUnsupported, got %v", err)
	}
}

type stubDataplane struct{ reachable bool }

func (s stubDataplane) Probe(context.Context, verify.ConnectivityProbe) (bool, error) {
	return s.reachable, nil
}
