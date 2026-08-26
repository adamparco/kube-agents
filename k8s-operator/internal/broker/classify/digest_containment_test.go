// V-GAT-024 -- a secret digest is computed in-broker and cannot leave package `classify`.
//
// 06 §4.2: "At classification the broker builds a set of digests of every value in every Secret
// readable in the caller's scope: for each key, sha256(secretNamespace || 0x1f || value) and
// sha256(value), plus the base64 and URL-encoded forms of the value." And: "Digests are computed
// in-broker, held in memory, never journaled and never logged; classification.reasons[] names the
// source Secret and key and never the value."
//
// secretegress_test.go asserts the BEHAVIOUR: the right things match, the wrong things do not, and
// the rendered reason omits the value. That leaves the containment claim -- "never journaled and
// never logged" -- resting on the fact that no current call site does either, which is a property of
// today's call sites rather than of the code.
//
// This file asserts it structurally instead, in the shape `dev/tests/closed-allowlist.py` uses:
//
//   - the digest map is unexported, and `*DigestSet`'s exported method set is EXACTLY {Len, Lookup},
//     neither of which returns a digest;
//   - the only thing Lookup hands back is a SecretHit, whose field set is exactly the five
//     provenance labels;
//   - package `classify` imports no logger, so "never logged" is not something a future call site
//     can quietly stop being true.
//
// Together those make a digest UNREACHABLE outside this package. A reviewer asking "could a digest
// end up in the journal?" gets an answer from the type system rather than from a grep.
package classify

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"go/parser"
	"go/token"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// escapableSecretValue needs URL-escaping, unlike testSecretValue, so all three encoded forms are
// distinct and the set below has six entries rather than four.
//
// FINDING, recorded rather than fixed here: because `url.QueryEscape(testSecretValue)` equals
// testSecretValue, the `url` arm of TestSecretMaterialMatchesEncodedForms takes its `t.Skip` on
// every run and has never asserted anything. Changing that constant ripples through sixteen tests
// including the connection-string limitation fixture, so it is queued for an improvement pass; the
// form itself is covered from here on by the digest set below.
const escapableSecretValue = "p@ss w0rd/9f2a+xyz"

func hexOf(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// TestDigestSetEntriesAreExactlyTheSpecifiedForms pins the formula of 06 §4.2 -- both digests, all
// three forms, and the 0x1f separator -- by recomputing it here rather than by calling digestForms.
func TestDigestSetEntriesAreExactlyTheSpecifiedForms(t *testing.T) {
	const ns, name, key = "team-a", "db-creds", "password"

	raw := []byte(escapableSecretValue)
	b64 := []byte(base64.StdEncoding.EncodeToString(raw))
	esc := []byte(url.QueryEscape(escapableSecretValue))

	// Without this the test would silently measure four entries while claiming six.
	if string(esc) == string(raw) || string(b64) == string(raw) || string(esc) == string(b64) {
		t.Fatalf("the fixture's three forms are not pairwise distinct (raw=%q base64=%q url=%q); "+
			"pick a value that needs URL-escaping", raw, b64, esc)
	}

	salted := func(b []byte) string {
		buf := append([]byte(ns), 0x1f)
		return hexOf(append(buf, b...))
	}
	want := map[string]string{
		salted(raw): "raw",
		hexOf(raw):  "raw",
		salted(b64): "base64",
		hexOf(b64):  "base64",
		salted(esc): "url",
		hexOf(esc):  "url",
	}

	ds := NewDigestSet(map[string]map[string]map[string][]byte{
		ns: {name: {key: raw}},
	})
	if ds.Len() != len(want) {
		t.Fatalf("digest set holds %d entries, want %d -- 06 §4.2 specifies two digests (namespace-salted "+
			"and unsalted) for each of three forms (raw, base64, url)", ds.Len(), len(want))
	}

	got := make(map[string]string, ds.Len())
	for digest, hit := range ds.byDigest {
		got[digest] = hit.Form
		if hit.Namespace != ns || hit.Secret != name || hit.Key != key {
			t.Fatalf("entry %s names %s/%s[%s], want %s/%s[%s]", digest, hit.Namespace, hit.Secret, hit.Key, ns, name, key)
		}
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("digest set contents differ from the formula in 06 §4.2.\n got: %v\nwant: %v", got, want)
	}
}

// TestDigestSetSurfaceCannotYieldADigest is the containment argument as a closed allowlist. It
// fails when someone widens the surface, which is the only way the "never journaled, never logged"
// claim can stop being true -- a digest no other package can obtain is a digest no other package can
// write down.
func TestDigestSetSurfaceCannotYieldADigest(t *testing.T) {
	ptr := reflect.TypeOf(&DigestSet{})
	methods := make([]string, 0, ptr.NumMethod())
	for i := 0; i < ptr.NumMethod(); i++ {
		methods = append(methods, ptr.Method(i).Name)
	}
	sort.Strings(methods)
	if want := []string{"Len", "Lookup"}; !reflect.DeepEqual(methods, want) {
		t.Fatalf("*DigestSet's exported method set is %v, want %v. Adding one is how a digest gets out "+
			"of this package; 06 §4.2 says digests are held in memory, never journaled and never logged, "+
			"and that is enforceable only while nothing can ask for one.", methods, want)
	}

	// Signatures, so a same-named method cannot become a getter.
	lookup, ok := ptr.MethodByName("Lookup")
	if !ok {
		t.Fatal("Lookup is gone")
	}
	if n := lookup.Type.NumOut(); n != 2 ||
		lookup.Type.Out(0) != reflect.TypeOf(SecretHit{}) ||
		lookup.Type.Out(1).Kind() != reflect.Bool {
		t.Fatalf("Lookup returns %v, want (SecretHit, bool) -- anything else may be a digest", lookup.Type)
	}
	length, _ := ptr.MethodByName("Len")
	if length.Type.NumOut() != 1 || length.Type.Out(0).Kind() != reflect.Int {
		t.Fatalf("Len returns %v, want int", length.Type)
	}

	// The map itself is unexported, so the surface above is the whole surface.
	st := reflect.TypeOf(DigestSet{})
	if st.NumField() != 1 {
		t.Fatalf("DigestSet has %d fields, want 1 (byDigest); a new one is not covered by this argument", st.NumField())
	}
	if f := st.Field(0); f.Name != "byDigest" || f.PkgPath == "" {
		t.Fatalf("DigestSet's field is %q (exported=%v), want the unexported byDigest", f.Name, f.PkgPath == "")
	}

	// And what Lookup DOES hand back carries provenance only. The value never leaves this struct's
	// construction, and no field here can hold it.
	hit := reflect.TypeOf(SecretHit{})
	fields := make([]string, 0, hit.NumField())
	for i := 0; i < hit.NumField(); i++ {
		f := hit.Field(i)
		if f.Type.Kind() != reflect.String {
			t.Fatalf("SecretHit.%s is %v; every field here is a label a human reads, and a non-string "+
				"one is how a []byte of material gets a seat", f.Name, f.Type)
		}
		fields = append(fields, f.Name)
	}
	sort.Strings(fields)
	if want := []string{"Form", "Key", "Namespace", "Secret", "Where"}; !reflect.DeepEqual(fields, want) {
		t.Fatalf("SecretHit's fields are %v, want %v -- 06 §4.2: classification.reasons[] names the "+
			"source Secret and key and never the value", fields, want)
	}
}

// loggingImports is the deny list for TestClassifyPackageLogsNothing. Not "every import must be on
// an allowlist": the classifier legitimately grows dependencies, and a list of things it may not do
// is the part that is actually load-bearing.
var loggingImports = []string{
	"log",
	"log/slog",
	"go.uber.org/zap",
	"github.com/go-logr/logr",
	"k8s.io/klog",
	"k8s.io/klog/v2",
	"sigs.k8s.io/controller-runtime/pkg/log",
}

// TestClassifyPackageLogsNothing closes the last route out. Everything above makes a digest
// unreachable from ANOTHER package; this makes it unwritable from this one.
//
// If the classifier ever genuinely needs a logger, this test failing is the right outcome: the
// containment argument has to be re-made some other way (a logger that cannot see the DigestSet, or
// a digest type that cannot be formatted), and that is a decision, not a lint to silence.
func TestClassifyPackageLogsNothing(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}
	denied := make(map[string]bool, len(loggingImports))
	for _, p := range loggingImports {
		denied[p] = true
	}

	fset := token.NewFileSet()
	parsed := 0
	for _, e := range entries {
		n := e.Name()
		if e.IsDir() || !strings.HasSuffix(n, ".go") || strings.HasSuffix(n, "_test.go") {
			continue
		}
		src, err := os.ReadFile(n)
		if err != nil {
			t.Fatalf("read %s: %v", n, err)
		}
		f, err := parser.ParseFile(fset, n, src, parser.ImportsOnly)
		if err != nil {
			t.Fatalf("parse %s: %v", n, err)
		}
		parsed++
		for _, imp := range f.Imports {
			path := strings.Trim(imp.Path.Value, `"`)
			if denied[path] {
				t.Fatalf("%s imports %q. 06 §4.2: digests are held in memory, never journaled and "+
					"never logged -- and a logger in the package that holds them makes that a promise "+
					"about call sites instead of a property of the code.", n, path)
			}
		}
		// A `fmt.Print*` writes to stdout, which in a pod IS the log.
		for _, needle := range []string{"fmt.Print", "println("} {
			if strings.Contains(string(src), needle) {
				t.Fatalf("%s contains %q; that writes to the pod's stdout, which is the log", n, needle)
			}
		}
	}
	// Reading zero files would pass every assertion above.
	if parsed < 5 {
		t.Fatalf("only %d non-test files parsed in package classify; the scan is not seeing the package", parsed)
	}
	if _, err := os.Stat(filepath.Join(".", "secretegress.go")); err != nil {
		t.Fatalf("secretegress.go -- the file that holds the digests -- was not in the scanned directory: %v", err)
	}
}
