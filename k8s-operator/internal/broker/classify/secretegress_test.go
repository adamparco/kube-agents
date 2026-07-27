package classify

import (
	"encoding/base64"
	"net/url"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

const testSecretValue = "s3cr3t-database-password-9f2a"

func testDigests() *DigestSet {
	return NewDigestSet(map[string]map[string]map[string][]byte{
		"team-a": {"db-creds": {"password": []byte(testSecretValue)}},
	})
}

func TestSecretMaterialMatchesRawForm(t *testing.T) {
	ds := testDigests()
	payload := map[string]any{"data": map[string]any{"DB_PASSWORD": testSecretValue}}
	hits := ScanPayload(ds, payload, "")
	if len(hits) != 1 {
		t.Fatalf("want 1 hit, got %d: %v", len(hits), hits)
	}
	if hits[0].Namespace != "team-a" || hits[0].Secret != "db-creds" || hits[0].Key != "password" {
		t.Fatalf("hit does not name the source: %+v", hits[0])
	}
	if hits[0].Where != "/data/DB_PASSWORD" {
		t.Fatalf("hit location = %q, want /data/DB_PASSWORD", hits[0].Where)
	}
}

func TestSecretMaterialMatchesEncodedForms(t *testing.T) {
	ds := testDigests()

	b64 := base64.StdEncoding.EncodeToString([]byte(testSecretValue))
	if hits := ScanPayload(ds, map[string]any{"v": b64}, ""); len(hits) != 1 || hits[0].Form != "base64" {
		t.Fatalf("base64 form not matched: %v", hits)
	}

	esc := url.QueryEscape(testSecretValue)
	if esc == testSecretValue {
		t.Skip("this value needs no URL escaping; pick one that does")
	}
	if hits := ScanPayload(ds, map[string]any{"v": esc}, ""); len(hits) != 1 || hits[0].Form != "url" {
		t.Fatalf("url form not matched: %v", hits)
	}
}

// The tokenising pass, exercised on the delimiters 06 §4.2 actually names: whitespace, quotes and
// commas. A secret pasted into a longer field alongside other words is found.
func TestSecretMaterialInsideALongerField(t *testing.T) {
	ds := testDigests()
	long := "connect using the password " + testSecretValue + " and then run the migration job before deploying"
	if len(long) <= tokeniseAboveBytes {
		t.Fatalf("precondition: the fixture must be longer than %d bytes to exercise tokenising", tokeniseAboveBytes)
	}
	hits := ScanPayload(ds, map[string]any{"notes": long}, "")
	if len(hits) == 0 {
		t.Fatal("a whitespace-delimited secret inside a long string was not found; the tokenising pass is not working")
	}
}

// TestKnownLimitation_SecretInsideAConnectionString documents a REAL GAP, deliberately left open.
//
// 06 §4.2 specifies the token delimiters as "whitespace/quote/comma". A connection string --
// `postgres://svc:<password>@host:5432/db` -- contains no such delimiter around the secret, so the
// whole URL is one token, the digest does not match, and the copy is NOT caught. That matters
// because a connection string is the single most common way a password actually leaves a Secret;
// nobody copies a password into a field called password.
//
// Left as-is rather than quietly widened, because the delimiter set is a specified value and this
// unit implements the spec rather than negotiating with it. Widening it to URL punctuation
// (`:@/?&=;`) would be a strict improvement with no loosening risk -- digest matching has no false
// positives -- and is recorded as a finding for the spec owner. The spec's own "two limits stated
// honestly" paragraph covers transformation, not embedding, so this gap is currently undocumented
// there.
//
// If this test starts FAILING, the gap has been closed: delete it and update the spec paragraph.
func TestKnownLimitation_SecretInsideAConnectionString(t *testing.T) {
	ds := testDigests()
	conn := "postgres://svc:" + testSecretValue + "@db.team-a.svc.cluster.local:5432/app?sslmode=require&pool=10"
	if hits := ScanPayload(ds, map[string]any{"DATABASE_URL": conn}, ""); len(hits) != 0 {
		t.Fatalf("the connection-string gap appears to be closed (%v); update 06 §4.2's delimiter "+
			"list and the honest-limits paragraph, then delete this test", hits)
	}
}

// TestSecretMaterialIsNotEntropy is the decision recorded as a test: high-entropy strings that are
// NOT secret values must not match. Every one of these appears in ordinary manifests, and an
// entropy scan fires on all of them.
func TestSecretMaterialIsNotEntropy(t *testing.T) {
	ds := testDigests()
	notSecrets := []string{
		"sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
		"gcr.io/adamparco-kage/kage-operator@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		"a1b2c3d4-e5f6-7890-abcd-ef1234567890",
		"eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9",
		"kube-agents-operator-7d9f8b6c5d-x2k9p",
	}
	for _, s := range notSecrets {
		if hits := ScanPayload(ds, map[string]any{"v": s}, ""); len(hits) != 0 {
			t.Fatalf("%q matched as secret material; digest matching must not behave like an entropy scan", s)
		}
	}
}

// TestShortSecretsAreNotMatched pins the 8-byte minimum and the reason for it.
func TestShortSecretsAreNotMatched(t *testing.T) {
	ds := NewDigestSet(map[string]map[string]map[string][]byte{
		"team-a": {"flags": {"debug": []byte("true"), "port": []byte("8080")}},
	})
	// The values are below the minimum, so nothing about them is in the set at all.
	if ds.Len() != 0 {
		t.Fatalf("short values must not enter the digest set, got %d entries", ds.Len())
	}
	payload := map[string]any{"env": map[string]any{"DEBUG": "true", "PORT": "8080"}}
	if hits := ScanPayload(ds, payload, ""); len(hits) != 0 {
		t.Fatalf("a 4-byte secret value matched an unrelated literal: %v", hits)
	}
}

// TestSecretHitNeverCarriesTheValue is the property that keeps the gate from becoming the leak. The
// reason string goes into the journal, the chat notification and the audit log.
func TestSecretHitNeverCarriesTheValue(t *testing.T) {
	ds := testDigests()
	hits := ScanPayload(ds, map[string]any{"data": map[string]any{"P": testSecretValue}}, "")
	if len(hits) == 0 {
		t.Fatal("precondition: expected a hit")
	}
	rendered := hits[0].String()
	if strings.Contains(rendered, testSecretValue) {
		t.Fatalf("the reason string contains the secret value: %q", rendered)
	}
	if !strings.Contains(rendered, "db-creds") || !strings.Contains(rendered, "password") {
		t.Fatalf("the reason must name the Secret and key so a human can act on it: %q", rendered)
	}
}

func TestScanIsStablyOrdered(t *testing.T) {
	ds := NewDigestSet(map[string]map[string]map[string][]byte{
		"team-a": {"s": {"a": []byte("value-aaaaaaaaaaaa"), "b": []byte("value-bbbbbbbbbbbb")}},
	})
	payload := map[string]any{
		"z": "value-bbbbbbbbbbbb",
		"a": "value-aaaaaaaaaaaa",
		"m": "value-aaaaaaaaaaaa",
	}
	first := ScanPayload(ds, payload, "")
	for i := 0; i < 50; i++ {
		got := ScanPayload(ds, payload, "")
		if len(got) != len(first) {
			t.Fatalf("run %d: hit count changed", i)
		}
		for j := range got {
			if got[j] != first[j] {
				t.Fatalf("run %d: hit %d = %+v, want %+v -- map iteration is leaking into the output", i, j, got[j], first[j])
			}
		}
	}
}

func TestSecretEgressGatesButSecretWritesDoNot(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})

	// Writing the material into a ConfigMap gates.
	leak := op("apply", "", "ConfigMap", "team-a", "app-config")
	leak.SecretMaterial = []SecretHit{{Namespace: "team-a", Secret: "db-creds", Key: "password", Where: "/data/DB", Form: "raw"}}
	got := classify(t, c, input(leak))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleSecretMaterialEgress)

	// Writing it into a Secret does not: the rule excludes Secret targets, because moving secret
	// material between Secrets is not egress. That write is `secret-write`, which is elevated.
	move := op("apply", "", "Secret", "team-a", "db-creds-copy")
	move.SecretMaterial = []SecretHit{{Namespace: "team-a", Secret: "db-creds", Key: "password", Where: "/data/DB", Form: "raw"}}
	got = classify(t, c, input(move))
	wantClass(t, got, ClassElevated)
	if hasReason(got, RuleSecretMaterialEgress) {
		t.Fatal("a Secret-to-Secret write fired secret-material-egress")
	}
}

// The `When` on the egress rule is a pre-filter, not the test. A ConfigMap write with no material
// found must not gate.
func TestSecretEgressDoesNotFireWithoutMaterial(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("apply", "", "ConfigMap", "team-a", "app-config")))
	wantClass(t, got, ClassRoutine)
	if hasReason(got, RuleSecretMaterialEgress) {
		t.Fatal("secret-material-egress fired with no secret material in the payload")
	}
}

// --- ownership ----------------------------------------------------------------------------------

func agentCR(name, proj, cluster, ns string) agentv1alpha1.Agent {
	return agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: agentv1alpha1.AgentSpec{
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: proj, ClusterName: cluster, Namespace: ns},
		},
	}
}

func TestOwnerLookupFindsTheLowerTierOwner(t *testing.T) {
	caller := Caller{Name: "ca", Scope: scope.Scope{ProjectID: "p", ClusterName: "c"}}
	l := OwnerLookup{Agents: []agentv1alpha1.Agent{
		agentCR("dev-team-a", "p", "c", "team-a"),
		agentCR("dev-team-b", "p", "c", "team-b"),
		agentCR("ca", "p", "c", ""),
	}}

	got, err := l.Find(caller, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"})
	if err != nil {
		t.Fatal(err)
	}
	if got != "dev-team-a" {
		t.Fatalf("owner = %q, want dev-team-a", got)
	}
}

// The caller must never be its own lower-tier owner. Non-strict containment on the caller side
// would make every agent gate on every write it makes -- a broker that has stopped working, not a
// conservative failure.
func TestOwnerLookupExcludesTheCallerItself(t *testing.T) {
	caller := Caller{Name: "dev-team-a", Scope: scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"}}
	l := OwnerLookup{Agents: []agentv1alpha1.Agent{agentCR("dev-team-a", "p", "c", "team-a")}}

	got, err := l.Find(caller, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"})
	if err != nil {
		t.Fatal(err)
	}
	if got != "" {
		t.Fatalf("owner = %q, want empty: an agent writing in its own namespace is not cross-tier", got)
	}
}

func TestOwnerLookupSkipsTerminatingAgents(t *testing.T) {
	caller := Caller{Name: "ca", Scope: scope.Scope{ProjectID: "p", ClusterName: "c"}}
	dying := agentCR("dev-team-a", "p", "c", "team-a")
	now := metav1.Now()
	dying.DeletionTimestamp = &now
	dying.Finalizers = []string{"kubeagents.gke-labs.dev/cleanup"}

	l := OwnerLookup{Agents: []agentv1alpha1.Agent{dying}}
	got, err := l.Find(caller, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"})
	if err != nil {
		t.Fatal(err)
	}
	if got != "" {
		t.Fatalf("owner = %q; gating a cleanup on approval from the agent being cleaned up is a deadlock", got)
	}
}

func TestOwnerLookupPrefersTheDeepest(t *testing.T) {
	caller := Caller{Name: "platform", Scope: scope.Scope{ProjectID: "p"}}
	l := OwnerLookup{Agents: []agentv1alpha1.Agent{
		agentCR("cluster-admin-c", "p", "c", ""),
		agentCR("dev-team-a", "p", "c", "team-a"),
	}}
	got, err := l.Find(caller, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"})
	if err != nil {
		t.Fatal(err)
	}
	if got != "dev-team-a" {
		t.Fatalf("owner = %q, want the deepest owner dev-team-a: naming the intermediate agent "+
			"would send the approval request to the wrong human", got)
	}
}

// A cluster-scoped target has the caller's own scope, so it is never owned by a lower tier.
func TestClusterScopedTargetHasNoLowerTierOwner(t *testing.T) {
	caller := Caller{Name: "ca", Scope: scope.Scope{ProjectID: "p", ClusterName: "c"}}
	l := OwnerLookup{Agents: []agentv1alpha1.Agent{agentCR("dev-team-a", "p", "c", "team-a")}}
	got, err := l.Find(caller, ScopeOfTarget(caller, ""))
	if err != nil {
		t.Fatal(err)
	}
	if got != "" {
		t.Fatalf("owner = %q, want empty for a cluster-scoped target", got)
	}
}

func TestCrossTierGatesAndDoesNotForbid(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("patch", "apps", "Deployment", "team-a", "web")
	o.LowerTierOwner = "dev-team-a"
	got := classify(t, c, input(o))

	// Gated, not forbidden. The cluster admin genuinely has the authority -- V-6 admitted the child
	// precisely because its scope is inside the parent's -- so what is wrong is doing it silently.
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleCrossTierDirectOperation)

	for _, r := range got.Reasons {
		if r.Rule == RuleCrossTierDirectOperation && !strings.Contains(r.Detail, "dev-team-a") {
			t.Fatalf("the reason must name the owning agent, got %q", r.Detail)
		}
	}
}
