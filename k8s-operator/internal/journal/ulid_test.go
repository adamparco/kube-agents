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
	"strings"
	"testing"
	"time"
)

func TestNewULIDIsSortableByTime(t *testing.T) {
	// The whole reason 06 §4.3 names ULID rather than UUID is that lexical order is time order:
	// `kubectl get actionrecords` comes back as a timeline for free. If the timestamp prefix were
	// wrong, ids would still be unique and every other test here would pass.
	base := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	var prev string
	for i := 0; i < 64; i++ {
		id, err := NewULID(base.Add(time.Duration(i) * time.Millisecond))
		if err != nil {
			t.Fatalf("NewULID: %v", err)
		}
		if !ValidULID(id) {
			t.Fatalf("NewULID produced %q, which its own validator rejects", id)
		}
		if prev != "" && id <= prev {
			t.Fatalf("ULID for a later instant sorts before an earlier one: %q <= %q", id, prev)
		}
		prev = id
	}
}

func TestNewULIDIsUniqueWithinAMillisecond(t *testing.T) {
	// Two actions in the same millisecond is not a hypothetical: a fan-out delegation mints several
	// records in one loop iteration. A collision would make the second Create an AlreadyExists on
	// the first record, and the broker treats AlreadyExists as a successful idempotent retry -- so
	// the second action would execute against the first action's journal entry.
	at := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	seen := make(map[string]bool, 2048)
	for i := 0; i < 2048; i++ {
		id, err := NewULID(at)
		if err != nil {
			t.Fatalf("NewULID: %v", err)
		}
		if seen[id] {
			t.Fatalf("duplicate ULID %q within one millisecond after %d draws", id, i)
		}
		seen[id] = true
	}
}

func TestValidULID(t *testing.T) {
	good, err := NewULID(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("NewULID: %v", err)
	}
	for _, tc := range []struct {
		name string
		in   string
		want bool
	}{
		{"a freshly minted id", good, true},
		{"empty", "", false},
		{"25 characters", good[:25], false},
		{"27 characters", good + "Z", false},
		{"lowercase", strings.ToLower(good), false},
		// I, L, O and U are excluded from Crockford base32 precisely so that a human reading an
		// action id out of a Slack thread cannot turn it into a different valid id by mistyping.
		{"contains I", "0000000000000000000000000I"[:26], false},
		{"contains L", strings.Repeat("0", 25) + "L", false},
		{"contains O", strings.Repeat("0", 25) + "O", false},
		{"contains U", strings.Repeat("0", 25) + "U", false},
		{"contains a hyphen", strings.Repeat("0", 25) + "-", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := ValidULID(tc.in); got != tc.want {
				t.Fatalf("ValidULID(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestRecordNameRoundTrip(t *testing.T) {
	id, err := NewULID(time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("NewULID: %v", err)
	}
	name := RecordName(id)
	if name != "ar-"+strings.ToLower(id) {
		t.Fatalf("RecordName(%q) = %q, want the 06 §4.3 form", id, name)
	}
	// The round trip is what makes the audit-log join in V-BRK-003 a string operation rather than a
	// list-and-filter over every record in the cluster.
	if back := ActionIDFromRecordName(name); back != id {
		t.Fatalf("ActionIDFromRecordName(%q) = %q, want %q", name, back, id)
	}
}

func TestActionIDFromRecordNameRejectsForeignNames(t *testing.T) {
	// The journal reconciler uses the empty return as "not one of ours". A lenient parse here would
	// have it claim records another controller owns.
	for _, name := range []string{
		"",
		"ar-",
		"nginx",
		"ar-not-a-ulid",
		"AR-01jzq8x9k7m4n2p6r8t0v3w5yz",       // wrong-case prefix
		"actionrecord-01jzq8x9k7m4n2p6r8t0v3", // right idea, wrong prefix and length
		"ar-01jzq8x9k7m4n2p6r8t0v3w5y",        // one character short of a ULID
		"ar-01jzq8x9k7m4n2p6r8t0v3w5yzz",      // one character long
	} {
		if got := ActionIDFromRecordName(name); got != "" {
			t.Fatalf("ActionIDFromRecordName(%q) = %q, want \"\" -- a foreign name was claimed as ours", name, got)
		}
	}
}
