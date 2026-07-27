package undo

import (
	"encoding/base64"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func obj(m map[string]any) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: m}
}

// TestSanitizeDropsServerOwnedFields walks the DROP list of 06 §4.3.1 one field at a time.
//
// Field by field rather than as one object with everything set, so a failure names the field that
// survived. An all-at-once assertion tells you the sanitizer is wrong; this tells you which row of
// the table it forgot.
func TestSanitizeDropsServerOwnedFields(t *testing.T) {
	for _, f := range droppedMetadataFields {
		t.Run("metadata/"+f, func(t *testing.T) {
			in := obj(map[string]any{
				"apiVersion": "apps/v1",
				"kind":       "Deployment",
				"metadata": map[string]any{
					"name":      "api",
					"namespace": "team-x",
					f:           "value-that-must-not-survive",
				},
			})
			out, _, err := Sanitize(in, false)
			if err != nil {
				t.Fatalf("Sanitize: %v", err)
			}
			if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "metadata", f); found {
				t.Errorf("metadata.%s survived sanitization", f)
			}
			if out.GetName() != "api" || out.GetNamespace() != "team-x" {
				t.Error("the KEEP list lost name or namespace")
			}
			// The input is a snapshot the caller may still need. Mutating it in place would corrupt
			// the record the broker persists.
			if _, found, _ := unstructured.NestedFieldNoCopy(in.Object, "metadata", f); !found {
				t.Errorf("Sanitize mutated its input; metadata.%s was removed from the caller's object", f)
			}
		})
	}
}

func TestSanitizeDropsStatusUnlessItIsTheTarget(t *testing.T) {
	build := func() *unstructured.Unstructured {
		return obj(map[string]any{
			"apiVersion": "apps/v1",
			"kind":       "Deployment",
			"metadata":   map[string]any{"name": "api", "namespace": "team-x"},
			"spec":       map[string]any{"replicas": int64(3)},
			"status":     map[string]any{"readyReplicas": int64(3)},
		})
	}

	out, _, err := Sanitize(build(), false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "status"); found {
		t.Error("status survived a non-status target")
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "spec"); !found {
		t.Error("spec is on the KEEP list in full and was dropped")
	}

	// The documented exception. Dropping status here would produce a plan that restores nothing at
	// all, while reporting a strategy of `restore`.
	out, _, err = Sanitize(build(), true)
	if err != nil {
		t.Fatalf("Sanitize(status target): %v", err)
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "status"); !found {
		t.Error("status was dropped even though it was the target of the action")
	}
}

func TestSanitizeDropsAllocatedFields(t *testing.T) {
	in := obj(map[string]any{
		"apiVersion": "v1",
		"kind":       "Service",
		"metadata":   map[string]any{"name": "api", "namespace": "team-x"},
		"spec": map[string]any{
			"type":                "LoadBalancer",
			"clusterIP":           "10.4.0.17",
			"clusterIPs":          []any{"10.4.0.17"},
			"healthCheckNodePort": int64(31234),
			"ports": []any{
				map[string]any{"name": "http", "port": int64(80), "nodePort": int64(31000)},
				map[string]any{"name": "https", "port": int64(443), "nodePort": int64(31001)},
			},
		},
	})
	out, _, err := Sanitize(in, false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	for _, f := range []string{"clusterIP", "clusterIPs", "healthCheckNodePort"} {
		if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "spec", f); found {
			t.Errorf("spec.%s survived; replaying an allocated address either collides or fails validation", f)
		}
	}
	ports, _, _ := unstructured.NestedSlice(out.Object, "spec", "ports")
	if len(ports) != 2 {
		t.Fatalf("the ports themselves were dropped: %v", ports)
	}
	for i, p := range ports {
		m := p.(map[string]any)
		if _, ok := m["nodePort"]; ok {
			t.Errorf("ports[%d].nodePort survived", i)
		}
		if _, ok := m["port"]; !ok {
			t.Errorf("ports[%d].port was dropped; only the ASSIGNED half comes off", i)
		}
	}
}

// TestSanitizeLeavesLookalikeFieldsAlone is the other half of the nodePort rule: a suffix strip that
// recurses would reach into a CRD that happens to use the word and quietly corrupt an object the
// sanitizer was only asked to normalize.
func TestSanitizeLeavesLookalikeFieldsAlone(t *testing.T) {
	in := obj(map[string]any{
		"apiVersion": "networking.example.com/v1",
		"kind":       "Gateway",
		"metadata":   map[string]any{"name": "edge", "namespace": "team-x"},
		"spec": map[string]any{
			"upstream": map[string]any{"nodePort": int64(9999), "clusterIP": "unrelated"},
		},
	})
	out, _, err := Sanitize(in, false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	if v, found, _ := unstructured.NestedFieldNoCopy(out.Object, "spec", "upstream", "nodePort"); !found || v.(int64) != 9999 {
		t.Error("a nodePort nested somewhere other than spec.ports was stripped; the sanitizer is recursing")
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "spec", "upstream", "clusterIP"); !found {
		t.Error("a clusterIP nested somewhere other than spec was stripped")
	}
}

func TestSanitizeDropsLastAppliedAndEmptiesTheMap(t *testing.T) {
	in := obj(map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":      "app-config",
			"namespace": "team-x",
			"annotations": map[string]any{
				"kubectl.kubernetes.io/last-applied-configuration": `{"a":"whole second copy"}`,
			},
		},
	})
	out, _, err := Sanitize(in, false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	// Removed entirely, not left as `{}`. The two are equal to the API server and unequal to the
	// textual diff a human reads in a mirror commit.
	if _, found, _ := unstructured.NestedFieldNoCopy(out.Object, "metadata", "annotations"); found {
		t.Error("an annotations map that is empty after the drop was left in place")
	}

	// A surviving annotation keeps the map.
	in = obj(map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":      "app-config",
			"namespace": "team-x",
			"annotations": map[string]any{
				"kubectl.kubernetes.io/last-applied-configuration": "{}",
				"team.example.com/owner":                           "payments",
			},
		},
	})
	out, _, err = Sanitize(in, false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	anns := out.GetAnnotations()
	if anns["team.example.com/owner"] != "payments" {
		t.Error("a user annotation was dropped; annotations are on the KEEP list")
	}
	if _, ok := anns["kubectl.kubernetes.io/last-applied-configuration"]; ok {
		t.Error("last-applied-configuration survived")
	}
}

func TestSanitizeRedactsSecretValuesAndNotConfigMapValues(t *testing.T) {
	raw := "super-secret-value-here"
	enc := base64.StdEncoding.EncodeToString([]byte(raw))

	sec := obj(map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "creds", "namespace": "team-x"},
		"data":       map[string]any{"token": enc, "password": enc},
	})
	out, reds, err := Sanitize(sec, false)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	if len(reds) != 2 {
		t.Fatalf("got %d redactions, want 2", len(reds))
	}
	// Sorted, so two sanitizations of one object produce byte-identical output and the digest of the
	// record is stable.
	if reds[0].Key != "password" || reds[1].Key != "token" {
		t.Errorf("redactions are not sorted by key: %v", reds)
	}
	data, _, _ := unstructured.NestedStringMap(out.Object, "data")
	for k, v := range data {
		if v == enc || strings.Contains(v, raw) {
			t.Errorf("data[%q] still holds the value", k)
		}
		if !strings.HasPrefix(v, "sha256:") {
			t.Errorf("data[%q] = %q, want a sha256: digest", k, v)
		}
	}
	// The two keys hold the same value, so the two digests must match -- which is what makes the
	// digest verifiable against the journal store's copy on replay.
	if data["token"] != data["password"] {
		t.Error("equal values produced unequal digests")
	}

	cm := obj(map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": "app-config", "namespace": "team-x"},
		"data":       map[string]any{"LOG_LEVEL": "debug"},
	})
	out, reds, err = Sanitize(cm, false)
	if err != nil {
		t.Fatalf("Sanitize(ConfigMap): %v", err)
	}
	if len(reds) != 0 {
		t.Fatalf("a ConfigMap was redacted: %v", reds)
	}
	if v, _, _ := unstructured.NestedString(out.Object, "data", "LOG_LEVEL"); v != "debug" {
		t.Errorf("a ConfigMap value was replaced with a digest: %q -- keying on the `data` field instead of the kind would do exactly this", v)
	}
}

// TestSecretDigestIsEncodingIndependent is why digest.go decodes before hashing. A Secret authored
// with `stringData` and read back as `data` is the same secret, and a digest that said otherwise
// would fail every replay verification for secrets created through the write-only field.
func TestSecretDigestIsEncodingIndependent(t *testing.T) {
	raw := "super-secret-value-here"
	enc := base64.StdEncoding.EncodeToString([]byte(raw))

	fromData := digestOfSecretValue("data", enc)
	fromStringData := digestOfSecretValue("stringData", raw)
	if fromData != fromStringData {
		t.Errorf("the same secret digests differently depending on which field it arrived in:\n  data:       %s\n  stringData: %s", fromData, fromStringData)
	}
	if fromData == "" {
		t.Fatal("empty digest")
	}
	if strings.Contains(fromData, raw) {
		t.Fatal("the digest contains the plaintext")
	}
	if digestOfSecretValue("data", enc) == digestOfSecretValue("data", base64.StdEncoding.EncodeToString([]byte("different"))) {
		t.Error("different values digest identically")
	}
}

func TestSanitizeRefusesAnUnidentifiableSnapshot(t *testing.T) {
	cases := map[string]*unstructured.Unstructured{
		"nil":     nil,
		"no kind": obj(map[string]any{"apiVersion": "v1", "metadata": map[string]any{"name": "x"}}),
		"no name": obj(map[string]any{"apiVersion": "v1", "kind": "ConfigMap", "metadata": map[string]any{"namespace": "team-x"}}),
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			if _, _, err := Sanitize(in, false); err == nil {
				t.Error("Sanitize accepted a snapshot that cannot be replayed")
			}
		})
	}
}
