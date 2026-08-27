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

package gateway

import (
	"testing"
	"time"
)

func TestDedupFirstSeenIsFalse(t *testing.T) {
	d := newDedup()
	if d.SeenRecently("a", time.Minute, time.Now()) {
		t.Error("the first sighting of a key must not be reported as seen")
	}
}

func TestDedupSecondSeenWithinWindowIsTrue(t *testing.T) {
	d := newDedup()
	now := time.Now()
	d.SeenRecently("a", time.Minute, now)
	if !d.SeenRecently("a", time.Minute, now.Add(time.Second)) {
		t.Error("a repeat within the window must be reported as seen")
	}
}

func TestDedupOutsideWindowIsFalseAgain(t *testing.T) {
	d := newDedup()
	now := time.Now()
	d.SeenRecently("a", time.Minute, now)
	if d.SeenRecently("a", time.Minute, now.Add(2*time.Minute)) {
		t.Error("a repeat outside the window must not be reported as seen")
	}
}

func TestDedupDistinctKeysDoNotCollide(t *testing.T) {
	d := newDedup()
	now := time.Now()
	d.SeenRecently("a", time.Minute, now)
	if d.SeenRecently("b", time.Minute, now) {
		t.Error("a different key must not be treated as a repeat")
	}
}
