package classify

import "testing"

// V-GAT-010 and V-GAT-021: the loosen/tighten asymmetry, across every control.
//
// The matrix below is the shape 09 §7.1 asks for -- each control with a loosening case, a
// tightening case, and a mixed case. The mixed case is the one that catches a lazy implementation:
// an operation that tightens one control and loosens another must GATE, because netting them out
// would let any loosening be laundered by bundling a tightening alongside it. That is not a
// hypothetical attack; it is what a model does naturally when it "improves" a manifest.

func TestWholeObjectDirection(t *testing.T) {
	cases := []struct {
		name  string
		verb  string
		kind  KindRef
		want  Direction
		found bool
	}{
		{"deleting a NetworkPolicy loosens", "delete", KindRef{Group: "networking.k8s.io", Kind: "NetworkPolicy"}, DirectionLoosen, true},
		{"creating a NetworkPolicy tightens", "create", KindRef{Group: "networking.k8s.io", Kind: "NetworkPolicy"}, DirectionTighten, true},
		{"deleting a webhook config loosens", "delete", KindRef{Group: "admissionregistration.k8s.io", Kind: "ValidatingWebhookConfiguration"}, DirectionLoosen, true},
		{"deleting a ResourceQuota loosens", "delete", KindRef{Group: "", Kind: "ResourceQuota"}, DirectionLoosen, true},

		// POLARITY. The three above are restrictions: their existence constrains, so deleting them
		// loosens. The three below are openings: their existence GRANTS, so it runs the other way, and
		// an implementation with one uniform "delete loosens" rule is confidently backwards on
		// exactly the objects that put things on the internet. See Polarity in direction.go.
		//
		// Deleting a ClusterRole revokes a permission -- disruptive, certainly, but not a security
		// loosening, and calling it one sends an approval request to a human whose correct answer is
		// always yes. `identity-change` gates the bindings regardless of direction, so nothing is lost
		// by getting this right.
		{"creating a ClusterRole loosens", "create", KindRef{Group: "rbac.authorization.k8s.io", Kind: "ClusterRole"}, DirectionLoosen, true},
		{"deleting a ClusterRole tightens", "delete", KindRef{Group: "rbac.authorization.k8s.io", Kind: "ClusterRole"}, DirectionTighten, true},
		{"creating an Ingress loosens", "create", KindRef{Group: "networking.k8s.io", Kind: "Ingress"}, DirectionLoosen, true},
		{"deleting an Ingress tightens", "delete", KindRef{Group: "networking.k8s.io", Kind: "Ingress"}, DirectionTighten, true},
		{"opening a ComputeFirewall loosens", "create", KindRef{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeFirewall"}, DirectionLoosen, true},
		{"deleting a Gateway tightens", "delete", KindRef{Group: "gateway.networking.k8s.io", Kind: "Gateway"}, DirectionTighten, true},

		// A Deployment is not a control object, so its direction is not knowable from the verb.
		{"deleting a Deployment has no direction", "delete", KindRef{Group: "apps", Kind: "Deployment"}, DirectionAny, false},
		// patch/apply on a control object need the field diff, not the verb.
		{"patching a NetworkPolicy needs the diff", "patch", KindRef{Group: "networking.k8s.io", Kind: "NetworkPolicy"}, DirectionAny, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, ok := DirectionOfWholeObject(tc.verb, tc.kind)
			if ok != tc.found {
				t.Fatalf("DirectionOfWholeObject(%q, %v) found = %v, want %v", tc.verb, tc.kind, ok, tc.found)
			}
			if ok && c.Direction != tc.want {
				t.Fatalf("direction = %q, want %q", c.Direction, tc.want)
			}
		})
	}
}

func TestPatchDirection(t *testing.T) {
	cases := []struct {
		name string
		op   PatchOp
		want Direction
	}{
		{"removing a securityContext loosens", PatchOp{Op: "remove", Path: "/spec/template/spec/securityContext"}, DirectionLoosen},
		{"adding a securityContext tightens", PatchOp{Op: "add", Path: "/spec/template/spec/securityContext"}, DirectionTighten},
		{"removing ingress rules loosens", PatchOp{Op: "remove", Path: "/spec/ingress"}, DirectionLoosen},
		{"adding ingress rules tightens", PatchOp{Op: "add", Path: "/spec/ingress"}, DirectionTighten},
		{"removing RBAC rules loosens", PatchOp{Op: "remove", Path: "/rules"}, DirectionLoosen},

		// Enum-valued replaces, where the direction is exact.
		{"pod-security restricted tightens", PatchOp{Op: "replace", Path: "/metadata/labels/pod-security.kubernetes.io~1enforce", Value: "restricted"}, DirectionTighten},
		{"pod-security privileged loosens", PatchOp{Op: "replace", Path: "/metadata/labels/pod-security.kubernetes.io~1enforce", Value: "privileged"}, DirectionLoosen},
		{"Service to ClusterIP tightens", PatchOp{Op: "replace", Path: "/spec/type", Value: "ClusterIP"}, DirectionTighten},
		{"Service to LoadBalancer loosens", PatchOp{Op: "replace", Path: "/spec/type", Value: "LoadBalancer"}, DirectionLoosen},
		{"failurePolicy Fail tightens", PatchOp{Op: "replace", Path: "/webhooks/0/failurePolicy", Value: "Fail"}, DirectionTighten},
		{"failurePolicy Ignore loosens", PatchOp{Op: "replace", Path: "/webhooks/0/failurePolicy", Value: "Ignore"}, DirectionLoosen},

		// The default for an unknown rewrite of a control. Loosen, deliberately: guessing tighten on
		// a real loosening is a control that silently did not fire.
		{"an opaque RBAC rewrite defaults to loosen", PatchOp{Op: "replace", Path: "/rules", Value: []any{}}, DirectionLoosen},
		{"an unknown Service type defaults to loosen", PatchOp{Op: "replace", Path: "/spec/type", Value: "Weird"}, DirectionLoosen},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, ok := DirectionOfPatch(tc.op)
			if !ok {
				t.Fatalf("DirectionOfPatch(%+v) did not match any control", tc.op)
			}
			if c.Direction != tc.want {
				t.Fatalf("direction = %q, want %q", c.Direction, tc.want)
			}
		})
	}
}

// TestMixedChangeIsALoosening is the negative control of V-GAT-021.
func TestMixedChangeIsALoosening(t *testing.T) {
	changes := []ControlChange{
		{Control: ControlNetworkPolicy, Direction: DirectionTighten},
		{Control: ControlRBAC, Direction: DirectionLoosen},
		{Control: ControlPodSecurity, Direction: DirectionTighten},
	}
	if got := CombineDirection(changes); got != DirectionLoosen {
		t.Fatalf("a mixed change combined to %q; any loosening must dominate, or a loosening can be "+
			"laundered by bundling a tightening alongside it", got)
	}
}

func TestCombineDirection(t *testing.T) {
	if got := CombineDirection(nil); got != DirectionAny {
		t.Fatalf("no changes = %q, want %q", got, DirectionAny)
	}
	if got := CombineDirection([]ControlChange{{Direction: DirectionTighten}}); got != DirectionTighten {
		t.Fatalf("only tightening = %q, want %q", got, DirectionTighten)
	}
}

// TestBooleanPolarity is the table that exists because inferring it is the most likely bug in the
// file: `runAsNonRoot: false` loosens but `allowPrivilegeEscalation: false` TIGHTENS.
func TestBooleanPolarity(t *testing.T) {
	cases := []struct {
		field string
		value bool
		want  Direction
	}{
		{"privileged", true, DirectionLoosen},
		{"privileged", false, DirectionTighten},
		{"allowPrivilegeEscalation", true, DirectionLoosen},
		{"allowPrivilegeEscalation", false, DirectionTighten},
		{"runAsNonRoot", false, DirectionLoosen},
		{"runAsNonRoot", true, DirectionTighten},
		{"readOnlyRootFilesystem", false, DirectionLoosen},
		{"readOnlyRootFilesystem", true, DirectionTighten},
		{"hostNetwork", true, DirectionLoosen},
		{"automountServiceAccountToken", true, DirectionLoosen},
	}
	for _, tc := range cases {
		t.Run(tc.field, func(t *testing.T) {
			got, known := DirectionOfBoolField(tc.field, tc.value)
			if !known {
				t.Fatalf("DirectionOfBoolField(%q) is not in the polarity table", tc.field)
			}
			if got != tc.want {
				t.Fatalf("DirectionOfBoolField(%q, %v) = %q, want %q", tc.field, tc.value, got, tc.want)
			}
		})
	}
	if _, known := DirectionOfBoolField("replicas", true); known {
		t.Fatal("an unknown field must not be given a direction")
	}
}

// TestSecurityLoosenGatesAndTightenDoesNot is the asymmetry at the classifier level, which is where
// it is actually observable: same kind, same verb, opposite direction, different class.
func TestSecurityLoosenGatesAndTightenDoesNot(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})

	loosen := op("patch", "networking.k8s.io", "NetworkPolicy", "team-a", "default-deny")
	loosen.Direction = DirectionLoosen
	got := classify(t, c, input(loosen))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleSecurityLoosen)

	tighten := op("patch", "networking.k8s.io", "NetworkPolicy", "team-a", "default-deny")
	tighten.Direction = DirectionTighten
	got = classify(t, c, input(tighten))
	wantClass(t, got, ClassRoutine)
	if hasReason(got, RuleSecurityLoosen) {
		t.Fatal("a tightening change fired security-loosen")
	}
}

func TestDirectionOfPatchIsDeterministic(t *testing.T) {
	// A path that could plausibly be reached by more than one control's list. Ranging over the
	// looseningFieldPaths map instead of over SecurityControls makes this flap.
	op := PatchOp{Op: "remove", Path: "/spec/template/spec/containers/0/resources/limits"}
	first, ok := DirectionOfPatch(op)
	if !ok {
		t.Skip("path no longer matches any control")
	}
	for i := 0; i < 200; i++ {
		got, _ := DirectionOfPatch(op)
		if got.Control != first.Control || got.Direction != first.Direction {
			t.Fatalf("run %d: got control %q direction %q, want %q/%q -- iteration order is not stable",
				i, got.Control, got.Direction, first.Control, first.Direction)
		}
	}
}
