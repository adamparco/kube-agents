package undo

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
)

// digestOfSecretValue is the content digest of one Secret value, for the redaction placeholder.
//
// DELIBERATELY NOT the classifier's digest, and the distinction is worth stating because the two
// look interchangeable and are not. `classify`'s DigestSet salts by namespace and stores three
// encodings of every value, because its question is "does this byte sequence appear somewhere in a
// payload" -- a search over an adversarial haystack. The question here is "is the value I am about
// to restore the value I snapshotted", a plain equality check on a known coordinate. Salting it
// would make the digest meaningless to anyone verifying a restore from the journal store, and
// storing the encoded forms would put three fingerprints of a secret where one is already more than
// the CR needs.
//
// `data` values arrive base64-encoded and `stringData` values raw. They are normalized to the
// decoded bytes before hashing, so the same secret written either way produces the same digest --
// otherwise a Secret authored with `stringData` and read back as `data` would appear to have
// changed between snapshot and restore, and the verification would fail on every single one.
func digestOfSecretValue(field, value string) string {
	raw := []byte(value)
	if field == "data" {
		if decoded, err := base64.StdEncoding.DecodeString(value); err == nil {
			raw = decoded
		}
		// A `data` value that does not decode is hashed as-is. It cannot have come from the API
		// server, so it came from a payload, and refusing to hash it would turn a malformed Secret
		// into an ungeneratable undo plan -- which gates the action. Gating is right for danger and
		// wrong for a typo, and the typo will be caught by the API server a step later anyway.
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
