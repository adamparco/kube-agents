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

package verify

import (
	"context"
	"fmt"
	"testing"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestCooldownBacksOffExponentially(t *testing.T) {
	for consecutive, want := range map[int]time.Duration{
		1: BaseCooldown,
		2: 2 * BaseCooldown,
		3: 4 * BaseCooldown,
		4: 8 * BaseCooldown,
	} {
		if got := CooldownFor(consecutive); got != want {
			t.Errorf("CooldownFor(%d) = %s, want %s", consecutive, got, want)
		}
	}
	// 04 §4.2 says "exponentially backed-off", so each step must be strictly longer than the last
	// until the cap. A flat curve satisfies "a quiet period" and satisfies nothing else.
	prev := time.Duration(0)
	for i := 1; i <= 7; i++ {
		d := CooldownFor(i)
		if d <= prev {
			t.Fatalf("CooldownFor(%d) = %s is not longer than CooldownFor(%d) = %s", i, d, i-1, prev)
		}
		prev = d
	}
}

func TestCooldownIsCapped(t *testing.T) {
	if got := CooldownFor(50); got != MaxCooldown {
		t.Errorf("CooldownFor(50) = %s, want the %s cap", got, MaxCooldown)
	}
	// The cap must be far enough out that the exponential is real before it bites -- see the
	// V-PRO-017 tension recorded on MaxCooldown.
	if CooldownFor(6) >= MaxCooldown {
		t.Errorf("the cap is reached by the sixth consecutive rollback (%s); the backoff has no room",
			CooldownFor(6))
	}
}

func TestCooldownForClampsNonsense(t *testing.T) {
	if got := CooldownFor(0); got != BaseCooldown {
		t.Errorf("CooldownFor(0) = %s, want BaseCooldown", got)
	}
	if got := CooldownFor(-3); got != BaseCooldown {
		t.Errorf("CooldownFor(-3) = %s, want BaseCooldown", got)
	}
}

func TestMemoryCooldownEntersAndExpires(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	key := "apps/Deployment/prod/web"

	active, _, err := m.Active(ctx, key, base)
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if active {
		t.Fatal("a target nobody rolled back is in cooldown")
	}

	until, err := m.Enter(ctx, "A1", key, base)
	if err != nil {
		t.Fatalf("Enter: %v", err)
	}
	if want := base.Add(BaseCooldown); !until.Equal(want) {
		t.Errorf("first cooldown until %s, want %s", until, want)
	}

	active, gotUntil, _ := m.Active(ctx, key, base.Add(time.Minute))
	if !active {
		t.Fatal("target is not in cooldown one minute into a five-minute quiet period")
	}
	if !gotUntil.Equal(until) {
		t.Errorf("Active reports %s, Enter returned %s", gotUntil, until)
	}

	if active, _, _ := m.Active(ctx, key, until); active {
		t.Error("cooldown is still active at its own expiry instant")
	}
	if active, _, _ := m.Active(ctx, key, until.Add(time.Second)); active {
		t.Error("cooldown outlived its expiry")
	}
}

func TestMemoryCooldownIsPerTarget(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	if _, err := m.Enter(ctx, "A1", "apps/Deployment/prod/web", base); err != nil {
		t.Fatalf("Enter: %v", err)
	}
	active, _, _ := m.Active(ctx, "apps/Deployment/prod/api", base.Add(time.Minute))
	if active {
		t.Fatal("rolling back one Deployment silenced a different one")
	}
}

func TestMemoryCooldownExtendsNeverShortens(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	key := "apps/Deployment/prod/web"

	first, _ := m.Enter(ctx, "A1", key, base)
	// A second rollback four minutes in. Its own window (10m from now) is longer, so it extends.
	second, _ := m.Enter(ctx, "A2", key, base.Add(4*time.Minute))
	if !second.After(first) {
		t.Fatalf("second cooldown ends %s, not after the first at %s", second, first)
	}

	// A third rollback one second later. The arithmetic gives 20m from then, still longer -- so
	// construct the shortening case directly: a fresh registry whose entry is already far out.
	//
	// The backward jump has to CLEAR the next step of the curve, or the test is vacuous. A second
	// failure is charged 2*BaseCooldown, so a reading only four minutes early still lands at
	// base+6m and an implementation that assigned `Until` unconditionally would pass -- which is
	// exactly what it did until a mutation sweep said so. Ten minutes early puts the naive answer
	// at base+0m, behind the base+5m already on the books.
	m2 := NewMemoryCooldown()
	long, _ := m2.Enter(ctx, "A1", key, base)                       // 5m from base
	short, _ := m2.Enter(ctx, "A2", key, base.Add(-10*time.Minute)) // an out-of-order clock reading
	if short.Before(long) {
		t.Fatalf("an out-of-order Enter shortened the cooldown from %s to %s", long, short)
	}
}

func TestMemoryCooldownDecays(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	key := "apps/Deployment/prod/web"

	if _, err := m.Enter(ctx, "A1", key, base); err != nil {
		t.Fatalf("Enter: %v", err)
	}
	if _, err := m.Enter(ctx, "A2", key, base.Add(time.Minute)); err != nil {
		t.Fatalf("Enter: %v", err)
	}
	// Two consecutive rollbacks, then a long quiet stretch. The count resets, so the next rollback
	// is charged the base rate again -- a count that never decays turns one bad week into a
	// permanently untouchable target.
	quiet := base.Add(CooldownDecay + time.Hour)
	until, _ := m.Enter(ctx, "A3", key, quiet)
	if want := quiet.Add(BaseCooldown); !until.Equal(want) {
		t.Errorf("after %s of quiet the cooldown is %s, want a reset to %s",
			CooldownDecay, until.Sub(quiet), BaseCooldown)
	}

	// Negative control: without the decay window the count keeps climbing.
	m2 := NewMemoryCooldown()
	if _, err := m2.Enter(ctx, "A1", key, base); err != nil {
		t.Fatalf("Enter: %v", err)
	}
	soon := base.Add(CooldownDecay - time.Hour)
	until2, _ := m2.Enter(ctx, "A2", key, soon)
	if got := until2.Sub(soon); got != 2*BaseCooldown {
		t.Errorf("inside the decay window the second cooldown is %s, want %s", got, 2*BaseCooldown)
	}
}

// TestMemoryCooldownIsIdempotentPerAction pins the promise CooldownRegistry.Enter makes in its own
// doc comment. It is not a defensive nicety: internal/broker/cooldown recovers failures from the
// journal AND holds an overlay of the ones it entered before the status write landed, so the same
// action reaches the fold twice by design. If entering twice were entering two failures, one
// rollback would buy a doubled quiet period on every restart.
func TestMemoryCooldownIsIdempotentPerAction(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	key := "apps/Deployment/prod/web"

	once, _ := m.Enter(ctx, "A1", key, base)
	twice, _ := m.Enter(ctx, "A1", key, base)
	if !twice.Equal(once) {
		t.Errorf("entering action A1 twice moved the expiry from %s to %s", once, twice)
	}
	// And the count did not move either: a genuinely second action must still be charged 2x, not 4x.
	third, _ := m.Enter(ctx, "A2", key, base)
	if want := base.Add(2 * BaseCooldown); !third.Equal(want) {
		t.Errorf("the second distinct action expires at %s, want %s -- the repeat was counted", third, want)
	}
}

func TestMemoryCooldownIsConcurrencySafe(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	done := make(chan struct{})
	for i := 0; i < 8; i++ {
		go func(worker int) {
			defer func() { done <- struct{}{} }()
			for j := 0; j < 50; j++ {
				// Distinct IDs per iteration, or the per-action dedup would make 399 of these 400
				// calls an early return and the test would exercise the lock and nothing under it.
				_, _ = m.Enter(ctx, fmt.Sprintf("A%d-%d", worker, j), "apps/Deployment/prod/web", base)
				_, _, _ = m.Active(ctx, "apps/Deployment/prod/web", base)
			}
		}(i)
	}
	for i := 0; i < 8; i++ {
		<-done
	}
}

// TestCooldownStopsTheNextActionRestarting is the cross-record half of "never restarts at the
// bottom for the same target after a rollback" (04 §5). The ladder's monotonicity covers one
// record; this covers the next action, which gets a fresh ladder starting legitimately at rung 0.
func TestCooldownStopsTheNextActionRestarting(t *testing.T) {
	ctx := context.Background()
	m := NewMemoryCooldown()
	ref := agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment",
		Namespace: "prod", Name: "web"}

	// Action 1 rolls back.
	if _, err := m.Enter(ctx, "A1", TargetKey(ref), base); err != nil {
		t.Fatalf("Enter: %v", err)
	}

	// Action 2 arrives a minute later with a brand-new ladder at rung 0, which is legal.
	fresh := NewLadder()
	if err := fresh.Climb(RungRetry, base.Add(time.Minute), "conflict"); err != nil {
		t.Fatalf("a fresh ladder refused rung 1: %v", err)
	}
	// The only thing standing between it and a retry loop against the same target is the registry.
	active, until, err := m.Active(ctx, TargetKey(ref), base.Add(time.Minute))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if !active {
		t.Fatal("the target is not in cooldown a minute after its remediation was rolled back")
	}
	if !until.After(base.Add(time.Minute)) {
		t.Errorf("cooldown expires at %s, which is not in the future", until)
	}
}

func TestTargetKeyIsStableAcrossRecreate(t *testing.T) {
	a := agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment",
		Namespace: "prod", Name: "web", UID: "uid-1", ResourceVersion: "100"}
	b := a
	b.UID = "uid-2"
	b.ResourceVersion = "1"
	if TargetKey(a) != TargetKey(b) {
		t.Errorf("a recreated target gets a fresh cooldown: %q vs %q", TargetKey(a), TargetKey(b))
	}

	core := agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: "prod", Name: "web"}
	if got, want := TargetKey(core), "core/Service/prod/web"; got != want {
		t.Errorf("TargetKey(core) = %q, want %q", got, want)
	}

	// Different objects must not collide, or one target's rollback quiets another.
	other := core
	other.Name = "web2"
	if TargetKey(core) == TargetKey(other) {
		t.Error("two different targets share a cooldown key")
	}
}
