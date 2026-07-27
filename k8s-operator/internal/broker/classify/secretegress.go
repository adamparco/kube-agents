package classify

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/url"
	"sort"
	"strings"
)

// `secret-material-egress`, 06 §4.2: gate any write whose payload contains the VALUE of a Secret
// the caller can read, wherever that value ended up -- a ConfigMap, an env var, an annotation, a
// log-shipper config, a webhook URL's query string.
//
// # Why digests and not entropy
//
// The obvious implementation is a Shannon-entropy scan: high-entropy string, probably a secret,
// gate it. 06 §4.2 rules it out and the reason is that entropy answers a different question. It
// asks "does this LOOK secret", and the answer is yes for every image digest, UID, base64 CA
// bundle, JWT audience, git SHA and generated resource name in a Kubernetes manifest -- so the rule
// fires constantly on writes that leak nothing, operators learn that the secret gate is noise, and
// they approve it without reading. A control that is always wrong is worse than no control,
// because it manufactures the habit that defeats the controls that are right.
//
// Digest matching asks the question we actually mean: "is this string EQUAL to a secret value I can
// see?" It cannot fire on an image digest, because an image digest is not the contents of a Secret.
// The cost is that it cannot catch a TRANSFORMED secret -- encrypted, truncated, reversed, or
// re-encoded in a form not in the list below -- and that is a real gap that this rule does not
// close and does not pretend to. It is not the last line of defence; the class ladder, the audit
// journal and the egress policy of 05 are.

// DigestSet is the set of secret-value digests visible to one scope, plus the provenance needed to
// name a hit without holding the value.
type DigestSet struct {
	// byDigest maps hex digest -> provenance. The values are never stored anywhere in this struct.
	byDigest map[string]SecretHit
}

// MinCandidateLength is the shortest string considered for a match, in bytes.
//
// Below 8 bytes the false-positive rate stops being theoretical: a Secret whose value is `true`,
// `8080`, `admin` or `debug` -- and those exist, because Secrets get used as generic config stores
// -- would match every unrelated occurrence of that literal in every manifest, and the gate would
// fire on writes that share a word with a Secret. Short secrets are also, definitionally, not
// secret. The tradeoff is explicit in the spec and the corpus has a fixture on each side of it.
const MinCandidateLength = 8

// DigestCacheTTLSeconds bounds how long a scope's digest set is reused. Secrets rotate; a stale set
// misses the new value and matches the old one. 60s is the spec's number and matches the
// blast-radius denominator's bound, so the two live-state caches expire together.
const DigestCacheTTLSeconds = 60

// NewDigestSet builds the digest set for a scope's Secrets.
//
// secrets maps namespace -> secret name -> key -> value. The values are consumed here and are not
// retained: everything that survives this call is a hex digest and a (namespace, secret, key)
// label. That is deliberate -- a long-lived in-memory map of plaintext secret values inside the
// broker is a much better target than the Secrets themselves, since it is pre-collected and
// cross-namespace.
func NewDigestSet(secrets map[string]map[string]map[string][]byte) *DigestSet {
	ds := &DigestSet{byDigest: map[string]SecretHit{}}
	for ns, byName := range secrets {
		for name, byKey := range byName {
			for key, value := range byKey {
				for digest, form := range digestForms(ns, value) {
					// First writer wins. Two Secrets holding the same value is a real situation (a
					// copied credential), and the map can only name one; naming the first in a
					// deterministic iteration would be nicer, but map order is not deterministic, so
					// instead the tie is broken on the provenance string to keep the reason stable
					// across runs of the same input.
					hit := SecretHit{Namespace: ns, Secret: name, Key: key, Form: form}
					if prev, ok := ds.byDigest[digest]; ok && prev.String() <= hit.String() {
						continue
					}
					ds.byDigest[digest] = hit
				}
			}
		}
	}
	return ds
}

// digestForms enumerates the encodings a secret value is hashed under.
//
// A value that reaches a ConfigMap has usually been through at least one encoding on the way, and
// the two that matter in Kubernetes are base64 (because `data:` is base64 and someone copied the
// encoded form) and URL-encoding (because the value went into a connection string or a webhook
// URL). Each form is hashed separately rather than the candidate being decoded, because decoding
// every string leaf in a payload as base64 succeeds far more often than it should -- most short
// alphanumeric strings are valid base64 of something -- and would turn the scan into an
// entropy-flavoured guess.
func digestForms(ns string, value []byte) map[string]string {
	// The minimum length applies to the SECRET VALUE, not to its encodings. Testing each encoded
	// form instead lets a 4-byte secret in through the back door: base64("true") is "dHJ1ZQ==",
	// which is exactly 8 bytes, so `debug: true` would enter the digest set and the gate would fire
	// on every manifest that base64s the word true. The spec's reason for the minimum -- "shorter
	// values collide with ordinary config" -- is a statement about the value, so the test is too.
	if len(value) < MinCandidateLength {
		return nil
	}
	forms := map[string][]byte{
		"raw":    value,
		"base64": []byte(base64.StdEncoding.EncodeToString(value)),
		"url":    []byte(url.QueryEscape(string(value))),
	}
	out := make(map[string]string, len(forms)*2)
	for name, b := range forms {
		if len(b) < MinCandidateLength {
			continue
		}
		// Two digests per form. The namespace-salted one is the primary: it means a hit can name the
		// Secret it came from without a reverse lookup, and it keeps the map from collapsing two
		// identical values in different namespaces into one entry that names the wrong owner. The
		// unsalted one catches the cross-namespace case the salted one misses -- the same value read
		// from namespace A and written into namespace B, which is the exfiltration shape, not the
		// coincidence shape.
		out[hashHex(append(append([]byte(ns), 0x1f), b...))] = name
		out[hashHex(b)] = name
	}
	return out
}

func hashHex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Lookup reports whether a candidate string matches a known secret value.
func (ds *DigestSet) Lookup(candidate string) (SecretHit, bool) {
	if ds == nil || len(candidate) < MinCandidateLength {
		return SecretHit{}, false
	}
	hit, ok := ds.byDigest[hashHex([]byte(candidate))]
	return hit, ok
}

// Len reports how many digests are in the set, for metrics and tests. Never logs the contents.
func (ds *DigestSet) Len() int {
	if ds == nil {
		return 0
	}
	return len(ds.byDigest)
}

// ScanPayload walks every string leaf of a payload and returns the secret material found.
//
// Two passes per leaf, and the second is the one that earns its keep. Matching whole leaves alone
// catches `password: <the value>` and misses `postgres://svc:<the value>@db:5432/app`, which is the
// far more common way a secret actually escapes -- nobody copies a password into a field called
// password, they build a connection string. So for leaves over 64 bytes the scan also splits on
// whitespace, quotes and commas and tests each token.
//
// The 64-byte threshold keeps the tokenising pass off the short leaves that make up almost all of a
// manifest, where a whole-leaf match is already sufficient.
const tokeniseAboveBytes = 64

func ScanPayload(ds *DigestSet, root any, basePointer string) []SecretHit {
	if ds == nil || ds.Len() == 0 {
		return nil
	}
	var hits []SecretHit
	seen := map[string]bool{}
	walkStrings(root, basePointer, func(ptr, s string) {
		for _, cand := range candidates(s) {
			hit, ok := ds.Lookup(cand)
			if !ok {
				continue
			}
			hit.Where = ptr
			key := hit.Namespace + "/" + hit.Secret + "/" + hit.Key + "@" + ptr
			if seen[key] {
				continue
			}
			seen[key] = true
			hits = append(hits, hit)
		}
	})
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Where != hits[j].Where {
			return hits[i].Where < hits[j].Where
		}
		return hits[i].String() < hits[j].String()
	})
	return hits
}

// candidates returns the strings to test for one leaf: the whole leaf, plus its tokens when long.
func candidates(s string) []string {
	if len(s) < MinCandidateLength {
		return nil
	}
	out := []string{s}
	if len(s) <= tokeniseAboveBytes {
		return out
	}
	for _, tok := range strings.FieldsFunc(s, isDelimiter) {
		if len(tok) >= MinCandidateLength && tok != s {
			out = append(out, tok)
		}
	}
	return out
}

func isDelimiter(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\r', '"', '\'', ',':
		return true
	}
	return false
}

// walkStrings visits every string leaf of a decoded JSON/YAML value, building the RFC 6901 pointer
// as it goes so a hit can say WHERE without a second traversal.
func walkStrings(v any, ptr string, visit func(pointer, s string)) {
	switch t := v.(type) {
	case string:
		visit(ptr, t)
	case map[string]any:
		// Sorted so the pointer order of hits is stable across runs -- an unstable reason list turns
		// a re-classification into a spurious diff in the journal.
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			walkStrings(t[k], ptr+"/"+escapePointerToken(k), visit)
		}
	case map[any]any:
		// YAML decoders that predate the any-keyed fix still produce these.
		keys := make([]string, 0, len(t))
		conv := make(map[string]any, len(t))
		for k, val := range t {
			ks := fmt.Sprint(k)
			keys = append(keys, ks)
			conv[ks] = val
		}
		sort.Strings(keys)
		for _, k := range keys {
			walkStrings(conv[k], ptr+"/"+escapePointerToken(k), visit)
		}
	case []any:
		for i, item := range t {
			walkStrings(item, fmt.Sprintf("%s/%d", ptr, i), visit)
		}
	case []byte:
		// A []byte leaf is a Secret's own `data` value round-tripping through the classifier. Tested
		// as a string so writing a Secret's material into another Secret is not a blind spot.
		visit(ptr, string(t))
	}
}
