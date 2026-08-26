package classify

import (
	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The seventeen code-floor rules of 06 §4.2.
//
// "Code floor" means two things. It is the MINIMUM classification for every action, which no
// ChangePolicy can lower (03 §5.3). And it is literally in code -- not in a ConfigMap, not in a CR,
// not in a file the operator reads at start-up -- because a floor that can be edited by whoever can
// edit cluster state is not a floor, and an agent whose job is editing cluster state is exactly the
// thing it exists to constrain.
//
// Seven of the seventeen are steps in the evaluation order rather than rows in the table, because
// they need something a `When` cannot express: `out-of-scope` and `forbidden-set` short-circuit,
// `blast-radius-*` compare counts, `production-environment` and `novel-action` contribute `+1`
// rather than a class, `object-override` reads an annotation, and the default is the absence of
// every other rule. They live in classify.go. The remaining ten are here.
//
// Every rule's Reason is written to be read by the person deciding whether to approve, at the
// moment they are deciding, on a phone. Not to be read by an engineer debugging the classifier.

// Rule IDs. Referenced by the corpus fixtures, the traceability matrix and the audit journal, so
// they are constants: a fixture naming a rule that no longer exists must fail to compile the lint,
// not silently match nothing.
const (
	RuleOutOfScope                = "out-of-scope"
	RuleForbiddenSet              = "forbidden-set"
	RuleNoUndoPlan                = "no-undo-plan"
	RuleDestructiveStatefulDelete = "destructive-stateful-delete"
	RuleSecurityLoosen            = "security-loosen"
	RulePublicExposure            = "public-exposure"
	RuleTrafficShiftProduction    = "traffic-shift-production"
	RuleIdentityChange            = "identity-change"
	RuleBlastRadiusCap            = "blast-radius-cap"
	RuleBlastRadiusHardCap        = "blast-radius-hard-cap"
	RuleSecretWrite               = "secret-write"
	RuleSecretMaterialEgress      = "secret-material-egress"
	RuleCrossTierDirectOperation  = "cross-tier-direct-operation"
	RuleProductionEnvironment     = "production-environment"
	RuleNovelAction               = "novel-action"
	RuleObjectOverride            = "object-override"
	RuleDefaultRoutine            = "default-routine"
)

// AllFloorRuleIDs is every ID above, for the corpus lint (V-MET-005), which asserts the corpus
// exercises each one at least once. A floor rule with no fixture is a rule nobody has ever seen
// fire.
var AllFloorRuleIDs = []string{
	RuleOutOfScope, RuleForbiddenSet, RuleNoUndoPlan, RuleDestructiveStatefulDelete,
	RuleSecurityLoosen, RulePublicExposure, RuleTrafficShiftProduction, RuleIdentityChange,
	RuleBlastRadiusCap, RuleBlastRadiusHardCap, RuleSecretWrite, RuleSecretMaterialEgress,
	RuleCrossTierDirectOperation, RuleProductionEnvironment, RuleNovelAction, RuleObjectOverride,
	RuleDefaultRoutine,
}

// statefulKinds are the kinds whose deletion destroys data that no undo plan can restore.
//
// The distinction this list draws is not "important" -- a Deployment is important -- it is
// RECOVERABLE. Deleting a Deployment loses a spec, and the undo plan holds the spec. Deleting a
// PersistentVolumeClaim loses the volume, and there is no field in an undo plan that contains a
// disk. That is why these gate on delete while far more disruptive operations on stateless objects
// do not.
//
// THE SECOND HALF OF THIS LIST WAS MISSING UNTIL P9-T4, and the shape of the gap is worth keeping
// written down. Every kind above the divider is Kubernetes-native; every kind below it is a Config
// Connector object. The list was complete for the domain whoever wrote it had in mind -- etcd -- and
// empty for the domain where the data actually lives. `delete SQLInstance`, `delete StorageBucket`,
// `delete BigQueryDataset`, `delete ComputeDisk` and `delete ContainerCluster` all classified
// `routine`, reason "no rule matched": an agent could drop a production database without anyone
// being asked, while deleting the ConfigMap next to it required approval. Found by
// TestNonRecreatableKindsAreGatedByTheClassifier, which asks the undo generator what it cannot
// restore and then asks the classifier what it lets through.
var statefulKinds = []KindRef{
	// --- Kubernetes-native ---
	{Group: "", Kind: "PersistentVolumeClaim"},
	{Group: "", Kind: "PersistentVolume"},
	{Group: "apps", Kind: "StatefulSet"},
	{Group: "", Kind: "Secret"},
	{Group: "", Kind: "ConfigMap"},
	{Group: "", Kind: "Namespace"},
	{Group: "snapshot.storage.k8s.io", Kind: "VolumeSnapshot"},
	{Group: "snapshot.storage.k8s.io", Kind: "VolumeSnapshotContent"},

	// --- Config Connector: the cloud resources whose deletion destroys the actual data ---
	// 06 §4.3.1 names these explicitly -- "a cloud disk, bucket, database, snapshot, or backup" --
	// and 03 §5.2 repeats them under `destructiveness`. A recreate yields an empty resource with
	// the same name, which is the most dangerous possible outcome: everything that addresses it by
	// name reconnects, and finds nothing.
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeDisk"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeSnapshot"},
	{Group: "storage.cnrm.cloud.google.com", Kind: "StorageBucket"},
	{Group: "sql.cnrm.cloud.google.com", Kind: "SQLInstance"},
	{Group: "sql.cnrm.cloud.google.com", Kind: "SQLDatabase"},
	{Group: "bigquery.cnrm.cloud.google.com", Kind: "BigQueryDataset"},
	{Group: "bigquery.cnrm.cloud.google.com", Kind: "BigQueryTable"},

	// --- Config Connector: containers whose deletion is cascading and non-atomic ---
	// "recreating the container does not recreate its contents" (06 §4.3.1) applies to a GKE
	// cluster exactly as it applies to a Namespace, and a node pool takes local state and in-flight
	// work with it.
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"},
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerNodePool"},

	// --- Reserved names, which are re-allocated to somebody else on release ---
	// Not "data" in the ordinary sense, and on this list for the same reason as the rest: what is
	// lost is not restorable by recreating the object. A released static IP is handed to the next
	// caller within seconds, so a recreated ComputeAddress has the same object name and a different
	// address -- and every DNS record, firewall rule and allowlist still pointing at the old one now
	// resolves to a stranger.
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeAddress"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeGlobalAddress"},
	{Group: "dns.cnrm.cloud.google.com", Kind: "DNSManagedZone"},
}

// IsStatefulKind reports whether deleting this kind destroys data the undo plan cannot hold.
//
// Exported for the cross-package invariant in internal/broker/undo, which asserts that everything
// the undo generator cannot restore is gated here. It is a read-only predicate rather than an
// exported slice on purpose: a caller handed the slice could append to it, and appending to the
// code floor from outside the package is not a thing that should be possible.
func IsStatefulKind(k KindRef) bool {
	for _, s := range statefulKinds {
		if s.Group == k.Group && s.Kind == k.Kind {
			return true
		}
	}
	return false
}

// identityKinds are the objects that decide WHO something is, as opposed to what it may do.
var identityKinds = []KindRef{
	{Group: "", Kind: "ServiceAccount"},
	{Group: "rbac.authorization.k8s.io", Kind: "RoleBinding"},
	{Group: "rbac.authorization.k8s.io", Kind: "ClusterRoleBinding"},
	{Group: "iam.cnrm.cloud.google.com", Kind: "IAMPolicyMember"},
	{Group: "iam.cnrm.cloud.google.com", Kind: "IAMServiceAccount"},
	{Group: "iam.cnrm.cloud.google.com", Kind: "IAMPartialPolicy"},
	// The KEY, not just the account. Added in P9-T4 by the same invariant that found the cloud data
	// kinds: deleting an IAMServiceAccount gated, and deleting its key -- which is the credential
	// itself, and the thing 06 §4.3.1 names under "rotating or deleting a credential" -- classified
	// routine. Revoking a credential is the more immediate of the two: the account can be rebound,
	// the key material is gone.
	{Group: "iam.cnrm.cloud.google.com", Kind: "IAMServiceAccountKey"},
}

// There is deliberately no `securityControlKinds` list here. The set of kinds whose direction the
// classifier understands is defined exactly once, in direction.go (ControlOfKind and
// looseningFieldPaths), and `security-loosen` keys off the resulting direction rather than off a
// copy of the list. See the rule's own comment for what the copy cost.

// exposureKinds are the objects that can put something on the internet.
var exposureKinds = []KindRef{
	{Group: "", Kind: "Service"},
	{Group: "networking.k8s.io", Kind: "Ingress"},
	{Group: "gateway.networking.k8s.io", Kind: "Gateway"},
	{Group: "gateway.networking.k8s.io", Kind: "HTTPRoute"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeForwardingRule"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeFirewall"},
}

// trafficKinds are the objects that decide where live traffic goes.
var trafficKinds = []KindRef{
	{Group: "", Kind: "Service"},
	{Group: "networking.k8s.io", Kind: "Ingress"},
	{Group: "gateway.networking.k8s.io", Kind: "HTTPRoute"},
	{Group: "networking.istio.io", Kind: "VirtualService"},
	{Group: "networking.istio.io", Kind: "DestinationRule"},
}

// writeVerbs are every op that changes state. `cloud` is included: a Config Connector write is
// still a write, and the fact that it lands in GCP rather than in etcd makes it more consequential,
// not less.
var writeVerbs = []string{"create", "apply", "patch", "delete", "scale", "cloud"}

// CodeFloor returns the table-expressible half of the floor. The evaluation-order half is in
// classify.go; the two together are AllFloorRuleIDs.
//
// Order within the slice is presentation order for the reasons list, not precedence -- precedence
// is Max over classes and does not depend on order. Reasons are ordered so the most serious reads
// first, because a chat notification truncates.
func CodeFloor() RuleSet {
	return RuleSet{
		Source: "code-floor",
		Rules: []Rule{
			{
				ID:    RuleSecretMaterialEgress,
				When:  When{Verbs: writeVerbs, ExcludeKinds: []KindRef{{Group: "", Kind: "Secret"}}},
				Class: Contributes(ClassGated),
				// Matched by the classifier's secret scan rather than by When -- the When here only
				// narrows the candidate set, and floorSecretEgress does the real work. Kept as a row
				// anyway so the rule has an ID, a reason and a place in the policy listing.
				Reason: "writes the value of a Secret into an object that is not a Secret; anyone who can read that object can now read the secret",
			},
			{
				ID:     RuleDestructiveStatefulDelete,
				When:   When{Verbs: []string{"delete"}, Kinds: statefulKinds},
				Class:  Contributes(ClassGated),
				Reason: "deletes an object that holds data; the undo plan can restore the object but not its contents",
			},
			{
				ID: RuleSecurityLoosen,
				// DIRECTION ONLY -- deliberately no Kinds list.
				//
				// The first version of this rule ANDed the direction with a list of control-bearing
				// kinds, and the list was wrong in three places at once: a Namespace carries the
				// pod-security labels, a ResourceQuota carries spec.hard, and every workload kind
				// carries securityContext and serviceAccountName. All three are controls the direction
				// analysis models -- looseningFieldPaths names their exact paths -- so the analysis
				// would correctly return `loosen` and then no rule would fire, because the kind was not
				// on a second list that had to be kept in sync by hand. A silently disabled security
				// gate is the worst failure this package has, since nothing about it is visible: the
				// action succeeds and the digest says routine.
				//
				// The kind list is redundant as well as wrong. `Direction` is never `loosen` unless
				// direction.go concluded that a control on its fixed, conservative list actually moved
				// (see directionOf in resolve.go) -- so the whitelist already happened, once, in the
				// place that has the field diff. Restricting by kind here re-derives it from memory.
				When:   When{Verbs: writeVerbs, Direction: DirectionLoosen},
				Class:  Contributes(ClassGated),
				Reason: "removes or widens a security control",
			},
			{
				ID:     RulePublicExposure,
				When:   When{Verbs: writeVerbs, Kinds: exposureKinds, Direction: DirectionLoosen},
				Class:  Contributes(ClassGated),
				Reason: "exposes a workload to a wider network than it was reachable from before",
			},
			{
				ID: RuleTrafficShiftProduction,
				When: When{
					Verbs:      writeVerbs,
					Kinds:      trafficKinds,
					FieldPaths: []string{"spec.selector", "spec.rules", "spec.http", "spec.backendRefs", "spec.subsets"},
				},
				Class:  Contributes(ClassGated),
				Reason: "changes where live traffic goes",
			},
			{
				ID:     RuleIdentityChange,
				When:   When{Verbs: writeVerbs, Kinds: identityKinds},
				Class:  Contributes(ClassGated),
				Reason: "changes who a workload runs as, or what an identity is bound to",
			},
			{
				ID:     RuleCrossTierDirectOperation,
				When:   When{Verbs: writeVerbs, OwnedByLowerTier: true},
				Class:  Contributes(ClassGated),
				Reason: "writes directly to an object managed by a lower-tier agent, which will not know the state changed underneath it",
			},
			{
				ID:     RuleSecretWrite,
				When:   When{Verbs: writeVerbs, Kinds: []KindRef{{Group: "", Kind: "Secret"}}},
				Class:  Contributes(ClassElevated),
				Reason: "writes a Secret",
			},
		},
	}
}

// prefilterRules maps a rule ID to the runtime condition that decides whether it actually fires.
//
// A rule listed here has a `When` that NARROWS CANDIDATES rather than deciding: matching `When` is
// necessary and not sufficient. Two readers need to know which rules those are, and they are two
// different files, so the set is written down once here instead of as an ID comparison in each:
//
//   - classify.go skips the rule when the condition is false, or `secret-material-egress` would
//     gate every write in the product.
//   - stricter.go skips the rule when checking a ChangePolicy for containment, because "your rule
//     sits inside this rule's When" does not imply "the floor assigns this class", and treating it
//     as though it did would force every policy rule anyone could write to be `gated`.
//
// The condition is a func rather than a bool flag so that adding a rule here forces the author to
// say what the condition IS. A flag would let a rule be marked as prefiltered with nothing
// deciding it, which reads as a gate and behaves as an unconditional one.
var prefilterRules = map[string]func(*ResolvedOp) bool{
	RuleSecretMaterialEgress: func(op *ResolvedOp) bool { return len(op.SecretMaterial) > 0 },
}

// forbiddenSet is step 2 of 06 §4.2: actions with no path through an agent at all, not even with a
// human approving.
//
// The membership test for this list is narrow on purpose. "Forbidden" is not "very dangerous" --
// dangerous things gate, and gating is what a human is for. Forbidden is reserved for actions where
// a human's approval would not make the action safe, because the action destroys the ability to
// review, undo or audit it. Deleting the audit journal is forbidden; deleting a production database
// is merely gated, because a human can meaningfully say yes to that and there is a record either
// way.
// KubeAgentsGroup is this operator's own API group, READ FROM THE SCHEME rather than written out.
//
// It was written out, once, as "kubeagents.gke-labs.dev" — a group this operator has never served.
// Five of the nine forbidden-set entries below name a kube-agents kind, so five of them matched
// nothing: an agent deleting an ActionRecord, editing a ChangePolicy, lifting a FleetFreeze or
// rewriting the ApprovalRoster would have sailed past step 2 of the evaluation order. The corpus
// did not catch it because the fixtures were written from the same wrong string, which is what
// happens whenever a fixture and the code under test are copied from each other rather than from
// the definition site.
//
// This is LSN-031's shape again — a decision the codebase already made once, in
// groupversion_info.go, re-made by hand downstream — and TestForbiddenSetNamesTheLiveAPIGroup is
// the mechanization: any kube-agents kind in the floor whose group is not this constant fails.
var KubeAgentsGroup = agentv1alpha1.GroupVersion.Group

var forbiddenSet = []forbiddenEntry{
	{
		Kinds:  []KindRef{{Group: KubeAgentsGroup, Kind: "ActionRecord"}},
		Verbs:  []string{"delete", "patch", "apply"},
		Reason: "modifies or deletes the action journal, which is the record this action would itself be written to",
	},
	{
		Kinds:  []KindRef{{Group: KubeAgentsGroup, Kind: "ChangePolicy"}},
		Verbs:  []string{"delete", "patch", "apply", "create"},
		Reason: "edits the policy that decides which actions need approval",
	},
	{
		Kinds:  []KindRef{{Group: KubeAgentsGroup, Kind: "FleetFreeze"}},
		Verbs:  []string{"delete", "patch", "apply"},
		Reason: "removes the fleet-wide brake",
	},
	{
		Kinds:  []KindRef{{Group: KubeAgentsGroup, Kind: "ApprovalRoster"}},
		Verbs:  []string{"delete", "patch", "apply", "create"},
		Reason: "edits the list of humans who may approve the agent's actions",
	},
	{
		Kinds:  []KindRef{{Group: KubeAgentsGroup, Kind: "Agent"}},
		Verbs:  []string{"create", "patch", "apply"},
		Reason: "creates or modifies an Agent, which is how an agent would grant itself a wider scope",
	},
	{
		Kinds: []KindRef{
			{Group: "admissionregistration.k8s.io", Kind: "ValidatingWebhookConfiguration"},
			{Group: "admissionregistration.k8s.io", Kind: "MutatingWebhookConfiguration"},
		},
		Names:  []string{"kube-agents-validating-webhook", "kube-agents-mutating-webhook"},
		Verbs:  []string{"delete", "patch", "apply"},
		Reason: "disables the admission webhook that enforces the agent hierarchy",
	},
	{
		Kinds:  []KindRef{{Group: "apiserver.config.k8s.io", Kind: "AuditPolicy"}},
		Verbs:  writeVerbs,
		Reason: "changes what the cluster records",
	},
	{
		Kinds: []KindRef{
			{Group: "logging.cnrm.cloud.google.com", Kind: "LoggingLogSink"},
			{Group: "logging.cnrm.cloud.google.com", Kind: "LoggingLogExclusion"},
		},
		Verbs:  []string{"delete", "patch", "apply", "create", "cloud"},
		Reason: "changes where the audit log goes",
	},
	{
		Kinds:  []KindRef{{Group: "", Kind: "Namespace"}},
		Names:  []string{"kube-system", "kube-agents-system", "gke-system", "gmp-system"},
		Verbs:  []string{"delete"},
		Reason: "deletes a system namespace, taking the control plane's own workloads with it",
	},
}

type forbiddenEntry struct {
	Kinds []KindRef
	// Names narrows the entry to specific objects. Empty means every object of the kind.
	Names  []string
	Verbs  []string
	Reason string
}

// IsForbidden reports whether an operation is in the forbidden set, with the reason.
func IsForbidden(op *ResolvedOp) (bool, string) {
	for _, e := range forbiddenSet {
		if !matchesKind(e.Kinds, op.Kind) {
			continue
		}
		if len(e.Names) > 0 && !contains(e.Names, op.Name) {
			continue
		}
		if len(e.Verbs) > 0 && !contains(e.Verbs, op.Verb) {
			continue
		}
		return true, e.Reason
	}
	return false, ""
}

// ForbiddenSetSize is exported for the corpus lint, which asserts the fixture set covers every
// entry. A forbidden-set entry with no fixture is an entry nobody has proven refuses anything.
func ForbiddenSetSize() int { return len(forbiddenSet) }
