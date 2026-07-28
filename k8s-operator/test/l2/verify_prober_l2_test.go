//go:build l2

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

// V-PRO-027 at L2. Driven by dev/verify/verify-prober-l2.sh, which owns the destructive-test guard;
// connect() in live_state_l2_test.go duplicates it, because a probe that can only be aimed safely
// by its wrapper is one `go test` away from being aimed at the live install.
package l2

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/probe"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// The two images. pause never exits; busybox is given a command that runs, succeeds at being Ready,
// and THEN dies -- which is the shape 04 §5.1's "no new restarts" clause exists for. A container
// that fails immediately would never make its Deployment Available, so the predicate would stop at
// the replica count and the restart clause would never be reached.
const (
	pauseImage   = "registry.k8s.io/pause:3.9"
	busyboxImage = "registry.k8s.io/busybox:1.27.2"
)

// proPtr is the address-of-a-literal helper the typed API objects below need. The package has no
// shared one; naming it with this file's prefix keeps it out of the way of any future addition.
func proPtr[T any](v T) *T { return &v }

// proNamespace is this file's own probe namespace, with its own GenerateName prefix so a leaked
// namespace can be attributed to the run that leaked it.
func proNamespace(t *testing.T, ctx context.Context, k8s client.Client, labels map[string]string) *corev1.Namespace {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "kage-pro027-", Labels: labels}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create probe namespace: %v", err)
	}
	t.Cleanup(func() {
		_ = k8s.Delete(context.Background(), ns, client.PropagationPolicy(metav1.DeletePropagationBackground))
	})
	return ns
}

// proWait polls until cond is true. It reports the last observation on timeout rather than just
// "timed out", because on a real cluster the interesting failures are the ones where the thing
// nearly happened.
func proWait(t *testing.T, timeout time.Duration, what string, cond func() (bool, string)) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	last := "(never evaluated)"
	for time.Now().Before(deadline) {
		ok, obs := cond()
		last = obs
		if ok {
			t.Logf("  %s: %s", what, obs)
			return
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("timed out after %s waiting for %s; last observation: %s", timeout, what, last)
}

func proDeployment(ns, name string, replicas int32, image string, command []string) *appsv1.Deployment {
	c := corev1.Container{Name: "app", Image: image, Command: command}
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{{
				Key: "app", Operator: metav1.LabelSelectorOpIn, Values: []string{name},
			}}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": name}},
				Spec: corev1.PodSpec{
					Containers:                    []corev1.Container{c},
					TerminationGracePeriodSeconds: new(int64),
				},
			},
		},
	}
}

func proRef(group, version, kind, ns, name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Group: group, Version: version, Kind: kind, Namespace: ns, Name: name}
}

// V-PRO-027, first half: the evidence comes from the cluster.
//
// # Why this needs a real cluster and the envtest suite is not enough
//
// internal/broker/probe/probe_envtest_test.go asserts every one of these properties against a real
// API server, and that is a genuine L1 result. It is not this one. envtest runs an API SERVER and
// nothing else -- no kubelet, no deployment controller, no endpoint-slice controller, no quota
// controller. Every status the L1 suite reads is one the L1 suite wrote, which means the L1 suite
// proves the prober reads the right FIELDS and cannot prove that anything ever writes them.
//
// That gap is exactly the one V-PRO-027 is about. "Verification evidence is read from the cluster"
// is not a claim about field paths; it is a claim that the numbers came from something that was
// watching. Four things here are unavailable below L2:
//
//   - A REAL KUBELET restarting a real container. The restart count in step 3 is produced by a
//     container that actually died, on a node, after actually having been Ready.
//   - A REAL DEPLOYMENT CONTROLLER. Step 1 is the whole of 04 §5.1's opening sentence in one
//     assertion: the write returned success and the verdict is not Satisfied, because nothing has
//     converged yet. There is no way to stage that at L1 without writing the un-converged status by
//     hand, which begs the question.
//   - A REAL ENDPOINT-SLICE CONTROLLER. Step 4's count is published by the controller from live pod
//     readiness, not by this test.
//   - GKE'S REAL AUTHORIZER CHAIN. Step 5's SubjectAccessReview goes through RBAC *and* GKE's IAM
//     webhook authorizer. envtest has RBAC alone.
func TestPRO027EvidenceIsReadFromTheClusterNotFromTheWrite(t *testing.T) {
	k8s, _, kubeContext := connect(t)
	ctx := context.Background()
	s := &probe.Source{Client: k8s}
	ns := proNamespace(t, ctx, k8s, nil)
	t.Logf("context %s, probe namespace %s", kubeContext, ns.Name)

	steady := proDeployment(ns.Name, "steady", 2, pauseImage, nil)
	// Ready almost at once, dead 20 seconds later, restarted by the kubelet: a rollout that
	// completed and then fell over.
	flapper := proDeployment(ns.Name, "flapper", 1, busyboxImage,
		[]string{"/bin/sh", "-c", "sleep 20; exit 7"})
	for _, d := range []*appsv1.Deployment{steady, flapper} {
		if err := k8s.Create(ctx, d); err != nil {
			t.Fatalf("create Deployment %s: %v", d.Name, err)
		}
	}

	steadyRef := proRef("apps", "v1", "Deployment", ns.Name, "steady")
	flapperRef := proRef("apps", "v1", "Deployment", ns.Name, "flapper")
	baseline := int64(0)
	pred := verify.PredicateFor(steadyRef)

	// --- step 1 -----------------------------------------------------------------------------
	// 04 §5.1's opening sentence, as an assertion. Both Creates above returned success.
	t.Log("step 1: the write returned success and the verdict is not satisfied")
	ev := pred(ctx, s, verify.Target{Ref: steadyRef, BaselineRestarts: &baseline})
	if ev.Verdict == verify.VerdictSatisfied {
		t.Fatalf("the verdict was Satisfied immediately after the write, before any pod existed: "+
			"%+v -- that is verification answered from the write's own return value", ev)
	}
	t.Logf("  verdict %s: %s", ev.Verdict, ev.Detail)

	// --- step 2 -----------------------------------------------------------------------------
	proWait(t, 4*time.Minute, "the steady rollout to converge", func() (bool, string) {
		ev := pred(ctx, s, verify.Target{Ref: steadyRef, BaselineRestarts: &baseline})
		return ev.Verdict == verify.VerdictSatisfied, fmt.Sprintf("%s: %s", ev.Verdict, ev.Detail)
	})
	t.Log("step 2: the rollout converged and the prober saw it happen")

	// --- step 3 -----------------------------------------------------------------------------
	// The pair that carries this test. A real kubelet restarts flapper's container; steady's count
	// must stay at zero throughout. A prober that answered from the namespace rather than from the
	// workload's selector, or that reported a number it had not counted, fails one or the other.
	proWait(t, 5*time.Minute, "the kubelet to restart flapper's container", func() (bool, string) {
		n, err := s.RestartCount(ctx, flapperRef)
		if err != nil {
			return false, "restart count errored: " + err.Error()
		}
		return n > 0, fmt.Sprintf("flapper restarts = %d", n)
	})
	if n, err := s.RestartCount(ctx, steadyRef); err != nil || n != 0 {
		t.Fatalf("steady restarts = %d (err %v), want 0: the crashlooping workload next door was "+
			"counted as this one's, which is what an unresolved selector does", n, err)
	}
	t.Log("step 3: restarts are counted live, per workload, and the healthy neighbour is unaffected")

	// And the consequence: the flapper's own row does not verify, even though its Deployment write
	// succeeded and its replicas have been Available.
	flapEv := verify.PredicateFor(flapperRef)(ctx, s, verify.Target{Ref: flapperRef, BaselineRestarts: &baseline})
	if flapEv.Verdict == verify.VerdictSatisfied {
		t.Fatalf("a workload that is restarting verified as Satisfied: %+v", flapEv)
	}
	t.Logf("  flapper verdict %s: %s", flapEv.Verdict, flapEv.Detail)

	// --- step 4 -----------------------------------------------------------------------------
	// Endpoints published by the real endpoint-slice controller, from live pod readiness.
	front := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "front"},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": "steady"},
			Ports:    []corev1.ServicePort{{Port: 80, TargetPort: intstr.FromInt32(80)}},
		},
	}
	quiet := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "quiet"},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{"app": "nothing-has-this-label"},
			Ports:    []corev1.ServicePort{{Port: 80, TargetPort: intstr.FromInt32(80)}},
		},
	}
	for _, svc := range []*corev1.Service{front, quiet} {
		if err := k8s.Create(ctx, svc); err != nil {
			t.Fatalf("create Service %s: %v", svc.Name, err)
		}
	}
	frontRef := proRef("", "v1", "Service", ns.Name, "front")
	proWait(t, 3*time.Minute, "the endpointslice controller to publish steady's endpoints", func() (bool, string) {
		n, err := s.EndpointCount(ctx, frontRef)
		if err != nil {
			return false, "endpoint count errored: " + err.Error()
		}
		return n == 2, fmt.Sprintf("front endpoints = %d", n)
	})
	// A Service that selects nothing is the legitimate zero, and it must not be an error: the
	// predicate reads zero as Pending, which is the right answer for pods that are not up yet.
	if n, err := s.EndpointCount(ctx, proRef("", "v1", "Service", ns.Name, "quiet")); err != nil || n != 0 {
		t.Fatalf("a Service selecting nothing gave (%d, %v); want (0, nil) -- zero endpoints is an "+
			"observation, and turning it into an error would make every not-yet-ready Service a "+
			"probe failure instead of a Pending", n, err)
	}
	t.Log("step 4: endpoints come from the endpointslice controller, and an empty one is zero not an error")

	// The programmed address is the cluster IP the API server really assigned.
	live := &corev1.Service{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(front), live); err != nil {
		t.Fatalf("get Service front: %v", err)
	}
	addr, err := s.ProgrammedAddress(ctx, frontRef)
	if err != nil {
		t.Fatalf("programmed address: %v", err)
	}
	if addr == "" || addr != live.Spec.ClusterIP {
		t.Fatalf("programmed address %q, want the assigned cluster IP %q", addr, live.Spec.ClusterIP)
	}

	// --- step 5 -----------------------------------------------------------------------------
	// The RBAC row under GKE's real authorizer chain: RBAC plus the IAM webhook authorizer. The
	// bound subject must be allowed and the unbound one denied -- and, critically, the denial must
	// arrive as a clean answer rather than as an evaluation error, which is the case this prober
	// refuses to read as a deny.
	const bound = "kage-pro027-bound@example.com"
	const unbound = "kage-pro027-unbound@example.com"
	if err := k8s.Create(ctx, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "reader"},
		Rules:      []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"configmaps"}, Verbs: []string{"get"}}},
	}); err != nil {
		t.Fatalf("create Role: %v", err)
	}
	if err := k8s.Create(ctx, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "reader"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "reader"},
		Subjects:   []rbacv1.Subject{{Kind: rbacv1.UserKind, APIGroup: rbacv1.GroupName, Name: bound}},
	}); err != nil {
		t.Fatalf("create RoleBinding: %v", err)
	}
	q := verify.AccessQuery{Verb: "get", Resource: "configmaps", Namespace: ns.Name}
	// RBAC bindings propagate to the authorizer's cache; on GKE that is fast but not instant.
	proWait(t, 2*time.Minute, "the binding to reach the authorizer", func() (bool, string) {
		bq := q
		bq.User = bound
		ok, err := s.AccessReview(ctx, bq)
		if err != nil {
			return false, "review errored: " + err.Error()
		}
		return ok, fmt.Sprintf("bound subject allowed = %v", ok)
	})
	uq := q
	uq.User = unbound
	allowed, err := s.AccessReview(ctx, uq)
	if err != nil {
		t.Fatalf("the review for the unbound subject did not complete: %v -- on this cluster that is "+
			"a real evaluation error, and it is correctly not being reported as a deny", err)
	}
	if allowed {
		t.Fatal("a subject with no binding was reported as allowed by the real authorizer chain")
	}
	t.Log("step 5: subjectaccessreview answered by the cluster's real authorizer chain, both directions")
}

// V-PRO-027, second half: the mandatory negative control (09 §6 line 271).
//
// Every clause of the check is of the form "returns X rather than Y", and Y is in each case the
// benign, plausible, passing answer. This test asserts the Y's do not happen. The headline is the
// admission probe: on GKE the admission chain has Pod Security Admission, GKE's own webhooks, and
// whatever else the cluster carries, so "submit something illegal and watch it be rejected" has
// several sources other than the LimitRange under test. That is a fact about a real cluster which
// envtest can only simulate.
func TestPRO027NoProbeSubstitutesABenignDefault(t *testing.T) {
	k8s, _, kubeContext := connect(t)
	ctx := context.Background()
	s := &probe.Source{Client: k8s}
	t.Logf("context %s", kubeContext)
	t.Log("step 6: negative control — every clause asserted in the direction that would pass silently")

	plain := proNamespace(t, ctx, k8s, nil)

	// (a) A restart count that cannot be counted is an error, never zero. A PodDisruptionBudget with
	// an empty selector is the legal object that makes this constructible: policy/v1 defines the
	// empty selector as "every pod in the namespace".
	if err := k8s.Create(ctx, &policyv1.PodDisruptionBudget{
		ObjectMeta: metav1.ObjectMeta{Namespace: plain.Name, Name: "all-pods"},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MinAvailable: proPtr(intstr.FromInt32(1)),
			Selector:     &metav1.LabelSelector{},
		},
	}); err != nil {
		t.Fatalf("create PDB: %v", err)
	}
	if n, err := s.RestartCount(ctx, proRef("policy", "v1", "PodDisruptionBudget", plain.Name, "all-pods")); err == nil {
		t.Fatalf("an empty selector returned %d restarts rather than refusing; zero restarts is the "+
			"passing answer for the Deployment row, so a count that was not counted passes it", n)
	}

	// (b) A routing object that names no Service is an error, never zero. ingressClassName points at
	// a class nothing implements, so no controller adopts this object and no load balancer is
	// provisioned -- the check is about what the prober reads, not about GKE's ingress controller.
	unclaimed := "kage-pro027-no-such-class"
	if err := k8s.Create(ctx, &networkingv1.Ingress{
		ObjectMeta: metav1.ObjectMeta{Namespace: plain.Name, Name: "routes-nowhere"},
		Spec: networkingv1.IngressSpec{
			IngressClassName: &unclaimed,
			Rules: []networkingv1.IngressRule{{IngressRuleValue: networkingv1.IngressRuleValue{
				HTTP: &networkingv1.HTTPIngressRuleValue{Paths: []networkingv1.HTTPIngressPath{{
					Path:     "/static",
					PathType: proPtr(networkingv1.PathTypePrefix),
					Backend: networkingv1.IngressBackend{Resource: &corev1.TypedLocalObjectReference{
						APIGroup: proPtr("k8s.example.com"), Kind: "StorageBucket", Name: "assets",
					}},
				}}},
			}}},
		},
	}); err != nil {
		t.Fatalf("create Ingress: %v", err)
	}
	if n, err := s.EndpointCount(ctx, proRef("networking.k8s.io", "v1", "Ingress", plain.Name, "routes-nowhere")); err == nil {
		t.Fatalf("an Ingress with no Service backend returned %d rather than refusing; zero would "+
			"say its backends are unready when the fact is that it has none", n)
	}

	// (c) The two-leg admission probe, positive leg: a real LimitRanger refusing a real pod.
	max := corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("500m")}
	if err := k8s.Create(ctx, proLimitRange(plain.Name, "caps", max)); err != nil {
		t.Fatalf("create LimitRange: %v", err)
	}
	lrRef := proRef("", "v1", "LimitRange", plain.Name, "caps")
	enforcing, err := s.AdmissionEnforcing(ctx, lrRef)
	if err != nil {
		t.Fatalf("admission enforcing: %v", err)
	}
	if !enforcing {
		t.Fatal("the real LimitRanger did not refuse a pod over the container max, or refused the " +
			"compliant one too; if the compliant leg is being refused by something else on this " +
			"cluster, that is a finding about the cluster and it belongs in the evidence")
	}

	// (d) THE ONE THAT MATTERS. A namespace where Pod Security Admission refuses everything. Both
	// legs are refused, so the probe learned nothing about the LimitRange and must say so. A
	// one-leg implementation -- "the violating pod was rejected, therefore it is enforcing" --
	// reports true here, and would go on reporting true for a LimitRange that had been deleted.
	locked := proNamespace(t, ctx, k8s, map[string]string{
		"pod-security.kubernetes.io/enforce": "restricted",
	})
	if err := k8s.Create(ctx, proLimitRange(locked.Name, "caps", max)); err != nil {
		t.Fatalf("create LimitRange in the locked namespace: %v", err)
	}
	lockedEnforcing, err := s.AdmissionEnforcing(ctx, proRef("", "v1", "LimitRange", locked.Name, "caps"))
	if err != nil {
		t.Fatalf("admission enforcing in the locked namespace: %v", err)
	}
	if lockedEnforcing {
		t.Fatal("both dry-run legs were refused by Pod Security Admission and the probe still " +
			"claimed the LimitRange was enforcing; the refusal had nothing to do with it")
	}
	t.Log("  the two-leg admission probe reported not-observed where another admitter refuses both legs")

	// (e) The probe is non-mutating on a real cluster, where a leftover pod would be scheduled.
	var pods corev1.PodList
	if err := k8s.List(ctx, &pods, client.InNamespace(plain.Name)); err != nil {
		t.Fatalf("list pods: %v", err)
	}
	if len(pods.Items) != 0 {
		t.Fatalf("the dry-run admission probe left %d pod(s) behind on a real cluster", len(pods.Items))
	}
	t.Log("  the admission probe persisted nothing")

	// (f) An absent capability is ErrProbeUnsupported, never a satisfied answer. Indeterminate is
	// expensive -- it becomes a rollback at the settle deadline -- and that is the correct
	// direction; reporting "reachable" for a probe that was never run is not.
	if reachable, err := s.Connectivity(ctx, verify.ConnectivityProbe{
		From: "a", To: "b", Port: 80, WantReachable: true,
	}); !errors.Is(err, verify.ErrProbeUnsupported) || reachable {
		t.Fatalf("Connectivity with no dataplane prober gave (%v, %v); want (false, ErrProbeUnsupported)",
			reachable, err)
	}
	if _, err := s.AdmissionEnforcing(ctx, proRef("", "v1", "ConfigMap", plain.Name, "nope")); !errors.Is(err, verify.ErrProbeUnsupported) {
		t.Fatalf("AdmissionEnforcing on an out-of-row kind gave %v; want ErrProbeUnsupported", err)
	}
	if _, err := s.ProviderState(ctx, proRef("", "v1", "ConfigMap", plain.Name, "nope")); !errors.Is(err, verify.ErrProbeUnsupported) {
		t.Fatalf("ProviderState on an out-of-row kind gave %v; want ErrProbeUnsupported", err)
	}
	t.Log("  an absent capability is ErrProbeUnsupported, which the driver turns into a rollback, not a pass")
}

func proLimitRange(ns, name string, max corev1.ResourceList) *corev1.LimitRange {
	return &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type: corev1.LimitTypeContainer, Max: max,
		}}},
	}
}

// V-PRO-027's fourth clause: a target replaced during the settle window is not evidence about the
// one the action touched.
//
// A settle window is minutes long. Inside it, an object can be deleted and recreated at the same
// name by a human, by a reconciler, or by the garbage collector -- and the replacement is usually
// healthy, so every other probe in the package would report the action as verified. Only the uid
// distinguishes them, and only a real cluster deletes an object for real: envtest has no finalizers
// worth the name and no controller racing to put it back.
func TestPRO027AReplacedTargetIsNotTheOneTheActionTouched(t *testing.T) {
	k8s, _, kubeContext := connect(t)
	ctx := context.Background()
	s := &probe.Source{Client: k8s}
	ns := proNamespace(t, ctx, k8s, nil)
	t.Logf("context %s, probe namespace %s", kubeContext, ns.Name)
	t.Log("step 7: the settle-window replacement")

	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "pinned"},
		Data:       map[string]string{"key": "before"},
	}
	if err := k8s.Create(ctx, cm); err != nil {
		t.Fatalf("create: %v", err)
	}
	original := string(cm.UID)
	ref := proRef("", "v1", "ConfigMap", ns.Name, "pinned")
	pinned := ref
	pinned.UID = original

	if _, err := s.Get(ctx, pinned); err != nil {
		t.Fatalf("reading the object the action actually touched: %v", err)
	}

	if err := k8s.Delete(ctx, cm); err != nil {
		t.Fatalf("delete: %v", err)
	}
	proWait(t, 2*time.Minute, "the object to actually be gone", func() (bool, string) {
		_, err := s.Get(ctx, ref)
		return err != nil, fmt.Sprintf("get after delete: %v", err)
	})

	replacement := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns.Name, Name: "pinned"},
		Data:       map[string]string{"key": "after"},
	}
	if err := k8s.Create(ctx, replacement); err != nil {
		t.Fatalf("recreate: %v", err)
	}
	if string(replacement.UID) == original {
		t.Fatalf("the API server reused uid %s, so this test proves nothing", original)
	}

	// The replacement is present, healthy, and at the right name. Only the uid says it is a
	// stranger.
	if _, err := s.Get(ctx, ref); err != nil {
		t.Fatalf("the replacement is readable without a pin, so the object really is back: %v", err)
	}
	if _, err := s.Get(ctx, pinned); err == nil {
		t.Fatal("a different object at the same name was accepted as evidence about the action; " +
			"the replacement is healthy, so every other probe in the package would have verified it")
	} else {
		t.Logf("  refused, correctly: %v", err)
	}
}
