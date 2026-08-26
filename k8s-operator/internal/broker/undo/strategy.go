package undo

import (
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// StrategyFor is the 06 §4.3.1 table, and nothing else.
//
// It answers only "what SHAPE is the inverse of this verb", and it answers it from the verb and one
// bit of live state. Whether that inverse can actually be built -- whether the object is
// recreatable, whether anything points at it, whether a snapshot exists -- is decided in plan.go.
// Keeping the two apart is what makes the table readable against the spec: a reader can diff this
// function against the six rows of §4.3.1 without holding the reference analysis in their head.
//
// `existed` distinguishes the two `apply` rows. An apply over an object that was not there is a
// create wearing an apply's name, and its inverse is a delete; an apply over one that was there is
// a restore. Getting this backwards produces a plan that deletes an object the action only
// modified, which is the single most destructive mistake this package could make -- so it is the
// first thing the round-trip fixtures pin.
func StrategyFor(verb string, existed bool) (agentv1alpha1.UndoStrategy, error) {
	switch verb {
	case "create":
		return agentv1alpha1.UndoDelete, nil
	case "apply":
		if existed {
			return agentv1alpha1.UndoRestore, nil
		}
		return agentv1alpha1.UndoDelete, nil
	case "patch":
		return agentv1alpha1.UndoRestore, nil
	case "scale":
		return agentv1alpha1.UndoRestore, nil
	case "delete":
		return agentv1alpha1.UndoRecreate, nil
	case "cloud":
		return agentv1alpha1.UndoInverse, nil
	default:
		// "anything else ⇒ none ⇒ gated". Returning an error alongside would invite a caller to
		// treat the unknown verb as a failure to be retried or logged; it is not a failure, it is
		// the table's last row, and the class it produces is the correct outcome.
		return agentv1alpha1.UndoNone, nil
	}
}

// nonRecreatableKinds are the kinds whose deletion this package cannot reverse.
//
// READ THIS LIST AGAINST classify's `statefulKinds`, NOT INSTEAD OF IT. The two answer different
// questions and their memberships genuinely differ, which is why they are two lists rather than one
// shared one:
//
//   - classify asks "does deleting this destroy data" -- and gates a Secret delete, because a
//     Secret holds data.
//   - this asks "can a recreate restore it" -- and a Secret CAN be recreated, because the snapshot
//     holds its contents. A PersistentVolumeClaim cannot: there is no field in any undo plan that
//     contains a disk.
//
// So `nonRecreatable ⊄ stateful` and `stateful ⊄ nonRecreatable` are both false statements to
// avoid, and the invariant that actually has to hold is one-directional:
//
//	anything this package cannot restore MUST be gated by the classifier
//
// A kind here that the classifier lets through as `routine` is the hole this whole unit exists to
// close -- the broker would execute an irreversible delete, having decided it was reversible in one
// file and unremarkable in another. `TestNonRecreatableKindsAreGatedByTheClassifier` asserts it in
// that direction and only that direction, so the two lists may legitimately diverge everywhere the
// invariant does not care.
var nonRecreatableKinds = []classify.KindRef{
	// --- Kubernetes data ---
	{Group: "", Kind: "PersistentVolumeClaim"},
	{Group: "", Kind: "PersistentVolume"},
	{Group: "snapshot.storage.k8s.io", Kind: "VolumeSnapshot"},
	{Group: "snapshot.storage.k8s.io", Kind: "VolumeSnapshotContent"},

	// --- Containers whose deletion is cascading and non-atomic (06 §4.3.1) ---
	// "recreating the container does not recreate its contents". A Namespace snapshot is one object;
	// what was inside it was thousands, and none of them are in the plan.
	{Group: "", Kind: "Namespace"},
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"},
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerNodePool"},

	// --- Cloud data (06 §4.3.1: "a cloud disk, bucket, database, snapshot, or backup") ---
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeDisk"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeSnapshot"},
	{Group: "storage.cnrm.cloud.google.com", Kind: "StorageBucket"},
	{Group: "sql.cnrm.cloud.google.com", Kind: "SQLInstance"},
	{Group: "sql.cnrm.cloud.google.com", Kind: "SQLDatabase"},
	{Group: "bigquery.cnrm.cloud.google.com", Kind: "BigQueryDataset"},
	{Group: "bigquery.cnrm.cloud.google.com", Kind: "BigQueryTable"},

	// --- Identity and reserved names: "the old value is gone even if the object comes back" ---
	// A released static IP is re-allocated to someone else within seconds; a recreated
	// ComputeAddress is a different address with the same object name, which is worse than a
	// missing one because everything pointing at it resolves and reaches a stranger.
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeAddress"},
	{Group: "compute.cnrm.cloud.google.com", Kind: "ComputeGlobalAddress"},
	{Group: "dns.cnrm.cloud.google.com", Kind: "DNSManagedZone"},
	{Group: "iam.cnrm.cloud.google.com", Kind: "IAMServiceAccountKey"},
}

// IsNonRecreatable reports whether deleting this kind is outside what an undo plan can reverse.
func IsNonRecreatable(k classify.KindRef) bool {
	for _, n := range nonRecreatableKinds {
		if n.Group == k.Group && n.Kind == k.Kind {
			return true
		}
	}
	return false
}

// NonRecreatableKinds returns the list, for the cross-package invariant test and for the policy
// listing a human reads. Copied, because a caller that appended to it would be editing the floor.
func NonRecreatableKinds() []classify.KindRef {
	out := make([]classify.KindRef, len(nonRecreatableKinds))
	copy(out, nonRecreatableKinds)
	return out
}

// effectfulKinds are objects whose CREATION does something the world outside the API remembers.
//
// 06 §4.3.1: "A Job that sent mail, charged a card, or called a webhook. The object is restorable;
// the effect is not." Deleting the Job undoes the record of the work, not the work -- and an undo
// plan that reports success there is worse than no plan, because it tells a human the situation is
// handled.
//
// The list is short and deliberately not clever. There is no way to inspect a Job and know whether
// its effect escaped, so the conservative reading is applied to the kind: creating one is not
// undoable, and the action gates. A gate on `create Job` is a real cost, paid knowingly, and the
// alternative is a broker that quietly promises to un-charge a card.
var effectfulKinds = []classify.KindRef{
	{Group: "batch", Kind: "Job"},
}

// IsEffectful reports whether creating this kind has effects outside the API server.
func IsEffectful(k classify.KindRef) bool {
	for _, e := range effectfulKinds {
		if e.Group == k.Group && e.Kind == k.Kind {
			return true
		}
	}
	return false
}

// cloudInverses names the cloud operations that have a true documented inverse.
//
// 06 §4.3.1 grants `inverse` only "where the provider exposes a true inverse; otherwise none". The
// map is the whole grant: a cloud op on a kind that is not here gets `none`, so the default for a
// provider call is that it cannot be undone. That default is the right way round. A cloud API's
// inverse is usually approximate -- resizing a node pool back does not return the same nodes, and
// the workloads that were evicted are not un-evicted -- and approximate is exactly the quality that
// should require a human rather than be asserted by a table.
var cloudInverses = map[classify.KindRef]string{
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerNodePool"}: "nodeCount",
}

// CloudInverseField returns the field whose prior value a cloud `inverse` restores, and whether an
// inverse exists at all.
func CloudInverseField(k classify.KindRef) (string, bool) {
	f, ok := cloudInverses[k]
	return f, ok
}

// describeKind renders a KindRef for a reason string, core group included as "core" rather than as
// an empty string that reads like a bug.
func describeKind(k classify.KindRef) string {
	if k.Group == "" {
		return fmt.Sprintf("core/%s", k.Kind)
	}
	return fmt.Sprintf("%s/%s", k.Group, k.Kind)
}
