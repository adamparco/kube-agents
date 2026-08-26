package classify

import "strings"

// Direction analysis: is this change LOOSENING a control or TIGHTENING one?
//
// 03 §5.2 -- "Agents are trusted to make things safer without asking, never to make them less
// safe." The asymmetry is the point of the whole design, and V-GAT-010 and V-GAT-021 exist to prove
// it holds across every control: adding a NetworkPolicy is routine, deleting one gates; narrowing
// an RBAC rule is routine, widening it gates; enabling a PodSecurity standard is routine, dropping
// it gates.
//
// This is not a general-purpose "is this change safe" analysis and cannot be. It is a fixed list of
// controls whose direction is well-defined, and everything not on the list is DirectionAny -- which
// means a direction-scoped rule does not fire, and the class comes from the other inputs. Guessing
// at the direction of an arbitrary field change would be the same mistake as entropy-scanning for
// secrets: confident, frequently wrong, and wrong in the direction that lets things through.

// SecurityControl names a control whose direction the classifier understands.
type SecurityControl string

const (
	ControlNetworkPolicy   SecurityControl = "network-policy"
	ControlRBAC            SecurityControl = "rbac"
	ControlPodSecurity     SecurityControl = "pod-security"
	ControlSecurityContext SecurityControl = "security-context"
	ControlAdmission       SecurityControl = "admission-webhook"
	ControlServiceExposure SecurityControl = "service-exposure"
	ControlAuthn           SecurityControl = "authentication"
	ControlResourceLimits  SecurityControl = "resource-limits"
)

// SecurityControls is the set of controls the direction analysis covers, and the set V-GAT-021's
// loosen/tighten matrix iterates. Eight controls, each with a loosening fixture, a tightening
// fixture, and -- the one that catches a lazy implementation -- a MIXED fixture that does both in
// one operation, which must gate: a change that tightens one control and loosens another is not
// neutral, and an implementation that nets them out would let any loosening be laundered by
// bundling a tightening alongside it.
var SecurityControls = []SecurityControl{
	ControlNetworkPolicy, ControlRBAC, ControlPodSecurity, ControlSecurityContext,
	ControlAdmission, ControlServiceExposure, ControlAuthn, ControlResourceLimits,
}

// ControlChange is one control moving in one direction within an operation.
type ControlChange struct {
	Control   SecurityControl
	Direction Direction
	// Detail is the human-readable "what moved", e.g. "removed the default-deny ingress rule".
	Detail string
	// Where is the JSON Pointer, for the reason.
	Where string
}

// CombineDirection reduces per-control changes to the operation's direction.
//
// ANY loosening makes the whole operation a loosening, regardless of how much tightening
// accompanies it. This is the netting-out refusal described above, and it is why the return is not
// a score.
func CombineDirection(changes []ControlChange) Direction {
	found := DirectionAny
	for _, c := range changes {
		if c.Direction == DirectionLoosen {
			return DirectionLoosen
		}
		if c.Direction == DirectionTighten {
			found = DirectionTighten
		}
	}
	return found
}

// LooseningChanges filters to the changes that gate, for the reason strings. A gated action must
// tell the approver which control moved and where; "this loosens security" is not reviewable.
func LooseningChanges(changes []ControlChange) []ControlChange {
	var out []ControlChange
	for _, c := range changes {
		if c.Direction == DirectionLoosen {
			out = append(out, c)
		}
	}
	return out
}

// Polarity is whether an object's EXISTENCE restricts or opens.
//
// This distinction is the object-level twin of boolFieldLoosensWhenTrue, and it exists for the same
// reason: the polarity cannot be inferred, so it is a table. A NetworkPolicy is a restriction --
// creating one tightens, deleting one loosens. An Ingress is an opening -- creating one LOOSENS and
// deleting one tightens, the exact reverse. An implementation with one rule for both ("delete
// loosens") is right about half the list and confidently backwards about the other half, and the
// half it is backwards about is the half that puts things on the internet.
type Polarity int

const (
	// PolarityRestriction: the object's existence constrains something. create tightens.
	PolarityRestriction Polarity = iota
	// PolarityOpening: the object's existence grants reachability. create loosens.
	PolarityOpening
)

// ControlOfKind maps a target kind to the control it embodies and that kind's polarity, for
// whole-object verbs where the direction follows from create-vs-delete rather than from a field
// diff.
func ControlOfKind(k KindRef) (SecurityControl, Polarity, bool) {
	switch k.Group + "/" + k.Kind {
	case "networking.k8s.io/NetworkPolicy":
		return ControlNetworkPolicy, PolarityRestriction, true
	case "rbac.authorization.k8s.io/Role", "rbac.authorization.k8s.io/ClusterRole",
		"rbac.authorization.k8s.io/RoleBinding", "rbac.authorization.k8s.io/ClusterRoleBinding":
		// RBAC objects are openings that happen to be spelled as grants: creating a ClusterRoleBinding
		// gives somebody a permission they did not have. `identity-change` gates the bindings
		// regardless of direction, but the direction still has to be right for the reason string.
		return ControlRBAC, PolarityOpening, true
	case "admissionregistration.k8s.io/ValidatingWebhookConfiguration",
		"admissionregistration.k8s.io/MutatingWebhookConfiguration",
		"admissionregistration.k8s.io/ValidatingAdmissionPolicy",
		"admissionregistration.k8s.io/ValidatingAdmissionPolicyBinding":
		return ControlAdmission, PolarityRestriction, true
	case "policy/PodDisruptionBudget":
		return ControlResourceLimits, PolarityRestriction, true
	case "/ResourceQuota", "/LimitRange":
		return ControlResourceLimits, PolarityRestriction, true
	case "networking.k8s.io/Ingress", "gateway.networking.k8s.io/Gateway",
		"gateway.networking.k8s.io/HTTPRoute",
		"compute.cnrm.cloud.google.com/ComputeForwardingRule",
		"compute.cnrm.cloud.google.com/ComputeFirewall":
		// The openings. Without these entries the direction analysis returns `any` for every
		// Ingress and firewall write, `public-exposure` -- which requires `loosen` -- never fires, and
		// the rule that exists to catch "you just put this on the internet" catches nothing. The
		// corpus fixture that found this is gat-050.
		return ControlServiceExposure, PolarityOpening, true
	}
	return "", PolarityRestriction, false
}

// DirectionOfWholeObject gives the direction of a create/delete on a control-bearing kind.
//
// Polarity decides which way round it goes; see Polarity above. This is where the asymmetry is most
// visible: `kubectl delete networkpolicy default-deny` is one word away from `kubectl apply` and one
// of them should wake someone up.
func DirectionOfWholeObject(verb string, k KindRef) (ControlChange, bool) {
	ctrl, pol, ok := ControlOfKind(k)
	if !ok {
		return ControlChange{}, false
	}
	creating := verb == "create"
	if verb != "create" && verb != "delete" {
		// `apply` and `patch` on a control object need the field diff, not the verb.
		return ControlChange{}, false
	}
	loosens := creating == (pol == PolarityOpening)
	if loosens {
		return ControlChange{Control: ctrl, Direction: DirectionLoosen,
			Detail: verbedControl(creating, ctrl, "widens what can reach it", "removes a control")}, true
	}
	return ControlChange{Control: ctrl, Direction: DirectionTighten,
		Detail: verbedControl(creating, ctrl, "adds a control", "narrows what can reach it")}, true
}

func verbedControl(creating bool, ctrl SecurityControl, whenCreating, whenDeleting string) string {
	if creating {
		return "creates a " + string(ctrl) + " object, which " + whenCreating
	}
	return "deletes a " + string(ctrl) + " object, which " + whenDeleting
}

// looseningFieldPaths are dotted paths whose modification loosens, by control. Matched by prefix,
// like every other fieldPaths list, so `spec.template.spec.securityContext` covers everything under
// it.
//
// This list is intentionally about REMOVAL and WIDENING of the field, which the caller determines
// from the patch op: a `remove` of a hardening field loosens; an `add` of one tightens. A `replace`
// needs the values, which is why replaceLoosens exists below.
var looseningFieldPaths = map[SecurityControl][]string{
	ControlSecurityContext: {
		"spec.securityContext",
		"spec.template.spec.securityContext",
		"spec.containers[*].securityContext",
		"spec.template.spec.containers[*].securityContext",
		"spec.template.spec.initContainers[*].securityContext",
	},
	ControlPodSecurity: {
		"metadata.labels['pod-security.kubernetes.io/enforce']",
		"metadata.labels['pod-security.kubernetes.io/audit']",
		"metadata.labels['pod-security.kubernetes.io/warn']",
	},
	ControlRBAC: {
		"rules",
		"roleRef",
		"subjects",
	},
	ControlNetworkPolicy: {
		"spec.ingress",
		"spec.egress",
		"spec.policyTypes",
		"spec.podSelector",
	},
	ControlServiceExposure: {
		"spec.type",
		"spec.externalIPs",
		"spec.loadBalancerSourceRanges",
	},
	ControlAuthn: {
		"spec.template.spec.serviceAccountName",
		"spec.template.spec.automountServiceAccountToken",
		"automountServiceAccountToken",
	},
	ControlResourceLimits: {
		"spec.hard",
		"spec.limits",
		"spec.template.spec.containers[*].resources.limits",
	},
	ControlAdmission: {
		"webhooks",
		"webhooks[*].failurePolicy",
		"webhooks[*].rules",
	},
}

// DirectionOfPatch classifies one JSON Patch operation against the control list.
//
// Iteration is over SecurityControls, NOT over looseningFieldPaths, and that is not a style
// preference. Some paths belong to more than one control -- `spec.template.spec.containers[*]
// .resources.limits` is a resource-limit and, on a cluster using limits as a security boundary, a
// security-context concern -- so ranging over the map would pick a different control on different
// runs and the reason string would not be stable. V-GAT-017 requires 100 permuted inputs to produce
// byte-identical output; a map range is the easiest way to fail it and the hardest to notice,
// because it passes locally almost every time.
func DirectionOfPatch(op PatchOp) (ControlChange, bool) {
	for _, ctrl := range SecurityControls {
		paths, ok := looseningFieldPaths[ctrl]
		if !ok {
			continue
		}
		for _, p := range paths {
			ok, err := PointerPrefixMatch(p, op.Path)
			if err != nil || !ok {
				continue
			}
			switch op.Op {
			case "remove":
				return ControlChange{Control: ctrl, Direction: DirectionLoosen,
					Detail: "removes " + p, Where: op.Path}, true
			case "add":
				return ControlChange{Control: ctrl, Direction: DirectionTighten,
					Detail: "adds " + p, Where: op.Path}, true
			case "replace":
				d := replaceDirection(ctrl, op)
				return ControlChange{Control: ctrl, Direction: d,
					Detail: "changes " + p, Where: op.Path}, true
			}
		}
	}
	return ControlChange{}, false
}

// replaceDirection decides the direction of a `replace` from the new value.
//
// Where the value is a known enum this is exact. Where it is not -- an arbitrary rewrite of an RBAC
// `rules` array, a new set of NetworkPolicy ingress rules -- it returns LOOSEN, and that default is
// the deliberate one. An unknown rewrite of a security control is a change nobody has proven safe,
// and the cost of the two errors is not symmetric: guessing tighten on a real loosening is a
// control that silently did not fire; guessing loosen on a real tightening is an approval request
// that a human waves through in ten seconds.
func replaceDirection(ctrl SecurityControl, op PatchOp) Direction {
	s, isString := op.Value.(string)
	if !isString {
		return DirectionLoosen
	}
	v := strings.ToLower(strings.TrimSpace(s))
	switch ctrl {
	case ControlPodSecurity:
		// restricted > baseline > privileged.
		switch v {
		case "restricted":
			return DirectionTighten
		case "privileged", "baseline":
			return DirectionLoosen
		}
	case ControlServiceExposure:
		// ClusterIP is the closed one.
		switch v {
		case "clusterip":
			return DirectionTighten
		case "loadbalancer", "nodeport", "externalname":
			return DirectionLoosen
		}
	case ControlAdmission:
		switch v {
		case "fail":
			return DirectionTighten
		case "ignore":
			return DirectionLoosen
		}
	}
	return DirectionLoosen
}

// DirectionOfBool handles the boolean hardening fields, where `false` is not always the loose one.
//
// `runAsNonRoot: false` loosens, but `allowPrivilegeEscalation: false` TIGHTENS, and
// `privileged: true` loosens. Getting this backwards is the single most likely bug in this file, so
// the polarity is a table rather than an inference from the value.
var boolFieldLoosensWhenTrue = map[string]bool{
	"privileged":                   true,
	"allowPrivilegeEscalation":     true,
	"hostNetwork":                  true,
	"hostPID":                      true,
	"hostIPC":                      true,
	"automountServiceAccountToken": true,

	"runAsNonRoot":           false,
	"readOnlyRootFilesystem": false,
}

// DirectionOfBoolField returns the direction of setting a known boolean hardening field.
func DirectionOfBoolField(field string, value bool) (Direction, bool) {
	loosensWhenTrue, known := boolFieldLoosensWhenTrue[field]
	if !known {
		return DirectionAny, false
	}
	if value == loosensWhenTrue {
		return DirectionLoosen, true
	}
	return DirectionTighten, true
}
