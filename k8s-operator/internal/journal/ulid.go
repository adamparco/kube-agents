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

package journal

import (
	"crypto/rand"
	"fmt"
	"strings"
	"time"
)

// ULIDs, in about eighty lines, rather than a dependency. The operator needs exactly two operations
// -- mint one, and check one -- and 06 §4.3 pins the encoding precisely enough that a library would
// be adding a supply-chain edge to save a lookup table.
//
// The shape matters to more than aesthetics. The first 48 bits are the mint time in milliseconds, so
// `kubectl get actionrecords` sorts by name into chronological order for free, and the 80 bits after
// it come from crypto/rand so two brokers minting in the same millisecond do not collide.

// crockford is Crockford's base32 alphabet: I, L, O and U are absent so a ULID read off a screen and
// typed into a chat command cannot become a different, valid ULID.
const crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

// ULIDLength is the fixed encoded length: 10 characters of timestamp, 16 of randomness.
const ULIDLength = 26

// decodeTable inverts crockford. -1 marks a character that is not in the alphabet.
var decodeTable = func() [256]int8 {
	var t [256]int8
	for i := range t {
		t[i] = -1
	}
	for i, c := range crockford {
		t[c] = int8(i)
	}
	return t
}()

// NewULID mints a ULID for the given instant. The caller passes the clock rather than the function
// reading one, so a test can assert the timestamp prefix and the controllers stay injectable.
func NewULID(at time.Time) (string, error) {
	var entropy [10]byte
	if _, err := rand.Read(entropy[:]); err != nil {
		// A ULID from a degraded entropy source would be a colliding action id, and a colliding
		// action id silently merges two records. Refuse instead.
		return "", fmt.Errorf("journal: read entropy for ULID: %w", err)
	}

	ms := uint64(at.UTC().UnixMilli())
	var b strings.Builder
	b.Grow(ULIDLength)

	// 48-bit timestamp, most significant character first.
	for shift := 45; shift >= 0; shift -= 5 {
		b.WriteByte(crockford[(ms>>uint(shift))&0x1f])
	}
	// 80 bits of randomness as 16 characters, five bits at a time.
	var bits uint16
	var n uint
	for _, by := range entropy {
		bits = bits<<8 | uint16(by)
		n += 8
		for n >= 5 {
			n -= 5
			b.WriteByte(crockford[(bits>>n)&0x1f])
		}
	}

	return b.String(), nil
}

// ValidULID reports whether s is a well-formed uppercase ULID. It is the Go-side twin of the CRD's
// pattern: the API server rejects a bad id at admission, and this rejects it before the broker has
// built a record around it.
func ValidULID(s string) bool {
	if len(s) != ULIDLength {
		return false
	}
	for i := 0; i < len(s); i++ {
		if decodeTable[s[i]] < 0 {
			return false
		}
	}
	return true
}

// RecordName is the ActionRecord's metadata.name for an action id: "ar-" + the lowercased ULID
// (06 §4.3). It is lowercased because a Kubernetes object name must be a DNS subdomain, and it is
// derived rather than stored so the join from an audit-log entry's `kube-agents/action-id` label
// back to the record is a string operation and not a list-and-filter.
func RecordName(actionID string) string {
	return "ar-" + strings.ToLower(actionID)
}

// ActionIDFromRecordName inverts RecordName. It returns the empty string when the name is not one
// this scheme produced, which is the case the journal reconciler treats as "not ours".
func ActionIDFromRecordName(name string) string {
	rest, ok := strings.CutPrefix(name, "ar-")
	if !ok {
		return ""
	}
	up := strings.ToUpper(rest)
	if !ValidULID(up) {
		return ""
	}
	return up
}
