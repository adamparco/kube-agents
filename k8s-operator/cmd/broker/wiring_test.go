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

// V-BRK-027: every seam of pipeline.Config is populated by the binary that ships, and the ones that
// are not are named with a reason.
//
// # Why reflection and not a list of assertions
//
// [[LSN-007]] is "built, tested, and unreachable", and its recurring shape is not a seam that was
// wired wrongly -- it is a seam that was never wired at all, in a struct nobody re-read after adding
// a field to it. A hand-written `if cfg.Live == nil { t.Fatal }` per field is exactly as blind to
// the thirteenth field as the wiring is. Enumerating the struct by reflection means a field ADDED to
// pipeline.Config tomorrow fails this test until someone either wires it or writes down why not.
//
// # The allowlists are two-sided
//
// `unwiredByDesign` requires the field to be zero, not merely permits it. An entry that becomes
// wired fails the test, which is what forces the allowlist to shrink when `journal.BlobSink` finally
// gets a production implementation. Without that direction the allowlist is a place things go to be
// forgotten -- [[LSN-035]]'s "a redundant guard and an unenforced guard look identical from a green
// suite".
package main

import (
	"context"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// unwiredByDesign is every pipeline.Config field the shipped binary leaves nil, with the reason.
// The reason is required and is asserted to be non-empty: "allowlisted" with no argument beside it
// is how a gap becomes permanent.
var unwiredByDesign = map[string]string{
	"BodyStore": "no production journal.BlobSink exists -- the package ships the interface plus " +
		"WriterSink/MemorySink, which implement the DIFFERENT AuditSink -- so the >1 MiB objectRef " +
		"path of 06 §4.3 has nothing behind it. execute.capture already refuses an over-limit body " +
		"when no store is configured, which is a refusal by name; a bodystore.Journal over a nil " +
		"sink would turn that into a nil-sink error one layer deeper",
	"Approvals": "no ApprovalNotifier exists yet (04 §3). Step 7 leaves a gated action parked and " +
		"says so, which is the same outcome a notifier that could not reach anyone produces",
}

// defaultedByNew is every field pipeline.New fills in itself. They are left unset here on purpose:
// restating undo.Generate or time.Now in main would be a second definition site for a value the
// pipeline package owns, and the pipeline's own doc warns that "a rule reimplemented here would be
// a second opinion about a question already answered".
//
// Unlike unwiredByDesign these are NOT asserted zero -- setting one is a legitimate choice, not a
// regression. What both maps share, and what the test actually leans on, is that every field of the
// struct is accounted for by exactly one of the three buckets.
var defaultedByNew = map[string]string{
	"Planner": "pipeline.New defaults it to undo.GenerateAndValidate, which is the production " +
		"planner. It used to default to the generate-only undo.Generate, and this entry used to " +
		"say so approvingly -- an allowlist reason is a claim about the code and is wrong as " +
		"loudly as any assertion",
	"Now": "pipeline.New defaults it to time.Now",
}

// testDeps is a brokerDeps that constructs without a cluster.
//
// Nothing in pipelineConfig dials: the adapters take a client and a discovery client and hold them.
// The fake client and an unroutable REST config are therefore enough to exercise the whole assembly,
// and using them means this check runs at L0/L1 speed on every push rather than behind envtest.
func testDeps(t *testing.T) brokerDeps {
	t.Helper()
	s := runtime.NewScheme()
	if err := agentv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	c := fake.NewClientBuilder().WithScheme(s).Build()
	return brokerDeps{
		// Host is unroutable on purpose: if a future edit makes construction dial, this test hangs
		// or fails rather than quietly reaching something.
		RESTConfig:          &rest.Config{Host: "https://127.0.0.1:1"},
		Client:              c,
		Records:             journal.NewStore(c, nil),
		AgentName:           "dev-team-a",
		Namespace:           "team-a",
		ActorServiceAccount: "kage-broker",
	}
}

func TestEveryPipelineSeamIsWiredInTheRealBinary(t *testing.T) {
	cfg, _, err := pipelineConfig(context.Background(), testDeps(t))
	if err != nil {
		t.Fatalf("pipelineConfig: %v", err)
	}

	v := reflect.ValueOf(cfg)
	typ := v.Type()

	seen := map[string]bool{}
	for i := range typ.NumField() {
		name := typ.Field(i).Name
		seen[name] = true
		f := v.Field(i)

		switch {
		case unwiredByDesign[name] != "":
			if !f.IsZero() {
				t.Errorf("%s is on the unwired-by-design allowlist but the binary now sets it. "+
					"That is good news: delete the allowlist entry rather than the assertion.", name)
			}
		case defaultedByNew[name] != "":
			// No assertion either way; see the map's comment.
		default:
			if f.IsZero() {
				t.Errorf("pipeline.Config.%s is not populated by the shipped binary, and is on no "+
					"allowlist. Either wire it, or add it to unwiredByDesign with the reason -- a "+
					"seam that reaches production nil is LSN-007 by definition.", name)
			}
		}
	}

	// A stale allowlist is its own defect: a field renamed out from under an entry leaves the entry
	// silently excusing nothing, and the renamed field is then covered by the default arm only by
	// luck.
	for _, m := range []map[string]string{unwiredByDesign, defaultedByNew} {
		for name, reason := range m {
			if !seen[name] {
				t.Errorf("allowlist names %q, which is not a field of pipeline.Config", name)
			}
			if reason == "" {
				t.Errorf("allowlist entry %q carries no reason", name)
			}
		}
	}

	// The wiring has to satisfy the pipeline's own constructor, which is a stricter statement than
	// "non-nil": New refuses eleven specific fields by name, each with a security argument.
	if _, err := pipeline.New(cfg); err != nil {
		t.Fatalf("pipeline.New rejected the config the binary builds: %v", err)
	}
}

// The seam this test exists for is the one no amount of exercising pipelineConfig can reach.
//
// `run` dials a kubeconfig, a clientset and a TLS keypair before it builds anything, so it is not
// callable from a unit test, and everything above this line would stay green if `run` went on
// handing `broker.UnavailablePipeline{}` to `broker.NewServer` -- which is precisely the state this
// unit found the binary in. So this reads the source: in the package that produces the shipped
// binary, the stub must not appear, and the `Pipeline` field of the `broker.Config` literal must
// carry a value produced by `pipeline.New`.
//
// Parsing rather than grepping because a grep for "UnavailablePipeline" matches this very comment,
// and a check that its own explanation can satisfy is not a check.
func TestTheShippedBinaryHandsTheServerARealPipeline(t *testing.T) {
	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, ".", func(fi os.FileInfo) bool {
		return !strings.HasSuffix(fi.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatalf("parse package main: %v", err)
	}
	pkg, ok := pkgs["main"]
	if !ok {
		t.Fatal("no package main in this directory")
	}

	var checked int
	for name, file := range pkg.Files {
		rel := filepath.Base(name)
		ast.Inspect(file, func(n ast.Node) bool {
			// The stub, anywhere in the binary's own sources. It stays exported and stays tested in
			// internal/broker -- it is a legitimate thing for another server to run -- but the one
			// broker whose ServiceAccount can write must not be built with it.
			if sel, isSel := n.(*ast.SelectorExpr); isSel && sel.Sel.Name == "UnavailablePipeline" {
				t.Errorf("%s:%d references broker.UnavailablePipeline; the shipped broker must be "+
					"built with the real pipeline", rel, fset.Position(sel.Pos()).Line)
			}

			lit, isLit := n.(*ast.CompositeLit)
			if !isLit {
				return true
			}
			sel, isSel := lit.Type.(*ast.SelectorExpr)
			if !isSel || sel.Sel.Name != "Config" {
				return true
			}
			if x, isIdent := sel.X.(*ast.Ident); !isIdent || x.Name != "broker" {
				return true
			}

			for _, elt := range lit.Elts {
				kv, isKV := elt.(*ast.KeyValueExpr)
				if !isKV {
					continue
				}
				key, isIdent := kv.Key.(*ast.Ident)
				if !isIdent || key.Name != "Pipeline" {
					continue
				}
				checked++
				// An identifier, not a literal. `Pipeline: broker.UnavailablePipeline{}` and
				// `Pipeline: someStub{}` are both composite literals; the real one can only come
				// from a call, because pipeline.New returns an error alongside it.
				id, isIdent := kv.Value.(*ast.Ident)
				if !isIdent {
					t.Errorf("%s:%d: broker.Config.Pipeline is built inline rather than from "+
						"pipeline.New", rel, fset.Position(kv.Pos()).Line)
					continue
				}
				if !assignedFromPipelineNew(file, id.Name) {
					t.Errorf("%s:%d: broker.Config.Pipeline is %q, which is not assigned from "+
						"pipeline.New anywhere in this file", rel, fset.Position(kv.Pos()).Line, id.Name)
				}
			}
			return true
		})
	}

	// Without this the whole test passes on a package where the literal was renamed, moved, or
	// deleted -- green because it inspected nothing.
	if checked != 1 {
		t.Fatalf("found %d broker.Config literals with a Pipeline field, want exactly 1", checked)
	}
}

// assignedFromPipelineNew reports whether name is bound by a `:=` or `=` whose right-hand side is a
// call to pipeline.New.
func assignedFromPipelineNew(file *ast.File, name string) bool {
	found := false
	ast.Inspect(file, func(n ast.Node) bool {
		as, isAssign := n.(*ast.AssignStmt)
		if !isAssign {
			return true
		}
		at := -1
		for i, lhs := range as.Lhs {
			if id, isIdent := lhs.(*ast.Ident); isIdent && id.Name == name {
				at = i
			}
		}
		if at != 0 || len(as.Rhs) != 1 {
			return true
		}
		call, isCall := as.Rhs[0].(*ast.CallExpr)
		if !isCall {
			return true
		}
		sel, isSel := call.Fun.(*ast.SelectorExpr)
		if !isSel || sel.Sel.Name != "New" {
			return true
		}
		if x, isIdent := sel.X.(*ast.Ident); isIdent && x.Name == "pipeline" {
			found = true
		}
		return true
	})
	return found
}

func TestTheBrakeIsBroughtUpBeforeThePolicySource(t *testing.T) {
	_, sources, err := pipelineConfig(context.Background(), testDeps(t))
	if err != nil {
		t.Fatalf("pipelineConfig: %v", err)
	}

	want := []string{"brake", "policy", "history", "cooldown", "budget"}
	got := make([]string, 0, len(sources))
	at := map[string]int{}
	for i, s := range sources {
		if s.refresh == nil {
			t.Errorf("source %q has no startup read; startSources would skip it silently", s.name)
		}
		if _, dup := at[s.name]; dup {
			t.Errorf("source %q appears twice", s.name)
		}
		at[s.name] = i
		got = append(got, s.name)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("sources = %v, want %v", got, want)
	}

	// The ordering that is not merely tidy. policy.Source's first Refresh calls the identity
	// closure, which reads the brake's cache; an unwarmed brake makes that read fail for a reason
	// that has nothing to do with ChangePolicy.
	if at["brake"] >= at["policy"] {
		t.Errorf("the brake is started at %d and the policy source at %d; the policy source's "+
			"identity closure reads through the brake, so the brake must be first",
			at["brake"], at["policy"])
	}
}

// The pollers are the three sources whose freshness is bounded by a staleness limit rather than by
// a lazy cache. A source that loses its loop looks identical at startup and starts refusing
// everything one staleness window later, in production, under load.
func TestThePolledSourcesHaveLoopsAndTheLazyOnesDoNot(t *testing.T) {
	_, sources, err := pipelineConfig(context.Background(), testDeps(t))
	if err != nil {
		t.Fatalf("pipelineConfig: %v", err)
	}

	wantLoop := map[string]bool{
		"policy":  true,
		"history": true,
		"budget":  true,
		// brake and cooldown refresh inside the call that needs them -- brake.Observe and
		// cooldown.Active both refresh-if-stale -- so a loop would be a second, unbounded reader of
		// the same objects.
		"brake":    false,
		"cooldown": false,
	}
	for _, s := range sources {
		want, known := wantLoop[s.name]
		if !known {
			t.Errorf("source %q is not accounted for in this test", s.name)
			continue
		}
		if got := s.run != nil; got != want {
			t.Errorf("source %q has a polling loop = %v, want %v", s.name, got, want)
		}
	}
}

// The negative control. Every field of brokerDeps is load-bearing, and the failure mode this guards
// is the one where a missing dependency yields a config that is merely *partly* built -- which
// pipeline.New might well accept, because the field it validates is not the one that went missing.
func TestAnIncompleteBrokerDepsRefusesRatherThanHalfBuilding(t *testing.T) {
	full := testDeps(t)
	for _, tc := range []struct {
		name string
		mut  func(*brokerDeps)
		want string
	}{
		{"no rest config", func(d *brokerDeps) { d.RESTConfig = nil }, "discovery"},
		{"no client", func(d *brokerDeps) { d.Client = nil }, "API client"},
		{"no record store", func(d *brokerDeps) { d.Records = nil }, "unjournaled"},
		{"no agent name", func(d *brokerDeps) { d.AgentName = "" }, "agent name"},
		{"no namespace", func(d *brokerDeps) { d.Namespace = "" }, "namespace"},
		{"no actor service account", func(d *brokerDeps) { d.ActorServiceAccount = "" }, "actor service account"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := full
			tc.mut(&d)

			cfg, sources, err := pipelineConfig(context.Background(), d)
			if err == nil {
				t.Fatal("pipelineConfig accepted an incomplete brokerDeps")
			}
			// Each case asserts on the TEXT, because a validator that returns on the first problem
			// it finds will happily let one case be satisfied by another case's check -- which is
			// how a negative suite passes for the wrong reason.
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error = %q, want it to mention %q", err, tc.want)
			}
			if !reflect.ValueOf(cfg).IsZero() {
				t.Error("a refused wiring returned a non-zero pipeline.Config; a half-built config " +
					"is the one thing a caller might pass on to pipeline.New")
			}
			if sources != nil {
				t.Error("a refused wiring returned sources to start")
			}
		})
	}
}

func TestStartSourcesStopsOnTheFirstFailedReadAndStartsNothing(t *testing.T) {
	var order []string
	boom := errors.New("forbidden")
	started := make(chan string, 4)

	sources := []startable{
		{name: "first", refresh: func(context.Context) error { order = append(order, "first"); return nil },
			run: func(context.Context) { started <- "first" }},
		{name: "second", refresh: func(context.Context) error { order = append(order, "second"); return boom },
			run: func(context.Context) { started <- "second" }},
		{name: "third", refresh: func(context.Context) error { order = append(order, "third"); return nil },
			run: func(context.Context) { started <- "third" }},
	}

	err := startSources(context.Background(), sources)
	if err == nil {
		t.Fatal("startSources returned nil after a failed startup read")
	}
	if !errors.Is(err, boom) {
		t.Errorf("error = %v, want it to wrap the underlying failure", err)
	}
	if !strings.Contains(err.Error(), "second") {
		t.Errorf("error = %q, want it to name the source that failed", err)
	}
	// The third source is never read: the process is about to exit, and a read issued on the way out
	// is a request against an API server that is already telling us something is wrong.
	if !reflect.DeepEqual(order, []string{"first", "second"}) {
		t.Errorf("refresh order = %v, want the sequence to stop at the failure", order)
	}
	// And nothing is left running behind the exit.
	//
	// A bounded WAIT, not a non-blocking receive. This assertion is about the ABSENCE of an event,
	// and `select ... default` only observes goroutines the scheduler happened to have run already
	// -- so a startSources that spawns each poller before its own read would slip past roughly
	// always. The mutation sweep for V-BRK-027 caught exactly that: mutant M9 hoisted `go s.run` above
	// the refresh and this test stayed green. The grace window is orders of magnitude longer than the
	// microseconds a `go` statement needs, so a false pass now requires a pathologically stalled
	// scheduler rather than ordinary luck.
	select {
	case name := <-started:
		t.Errorf("source %q was started despite a failed startup read; a poller must not outlive "+
			"the process that is about to exit non-zero", name)
	case <-time.After(250 * time.Millisecond):
	}
}

// V-REV-003 at the composition root: the dry-runner the binary supplies reaches a real API client.
//
// TestEveryPipelineSeamIsWiredInTheRealBinary above already refuses a nil DryRunner, and that is
// exactly the assertion that would have passed while undo was dead. The field it checks is a
// FACTORY: a factory returning a PlanDryRunner with a nil Replayer is non-zero, satisfies
// reflection, satisfies pipeline.New, and answers "no writer is configured" to every step it is
// ever asked about -- which undo.Validate turns into a downgrade, which gates every action in the
// fleet. "Populated" and "connected" are different claims and only the second one is the property.
//
// So this calls the factory and looks at what comes back.
func TestBuildPipelineWiresADryRunnerThatReachesTheClusterClient(t *testing.T) {
	cfg, _, err := pipelineConfig(context.Background(), testDeps(t))
	if err != nil {
		t.Fatalf("pipelineConfig: %v", err)
	}
	if cfg.DryRunner == nil {
		t.Fatal("the binary supplies no DryRunner factory")
	}

	const identity = "developer-team/team-a"
	dr := cfg.DryRunner(identity)
	if dr == nil {
		t.Fatal("the factory returned nil, so every step of every plan would panic or downgrade")
	}
	p, ok := dr.(*rollback.PlanDryRunner)
	if !ok {
		t.Fatalf("the factory returned %T; the production validator is *rollback.PlanDryRunner", dr)
	}
	if p.Replayer == nil || p.Replayer.Writer == nil {
		t.Fatal("the validator has no writer, so it answers `no writer is configured` to every " +
			"step -- a downgrade on every plan, which gates every action, which is undo being " +
			"dead again wearing the opposite failure mode")
	}
	// The identity is threaded, not dropped. A validator that dry-runs under a fixed manager
	// manufactures server-side-apply conflicts the real replay never hits.
	if p.AgentIdentity != identity {
		t.Errorf("AgentIdentity = %q, want %q -- the key the factory was called with", p.AgentIdentity, identity)
	}

	// The same writer the executor and the replayer use, per rollback's own package doc: "a second
	// client opened next to it would be a second set of answers to the same questions" (LSN-040).
	// Asserted through the Verifier's rollbacker, which is the only other holder of it in this
	// config.
	if cfg.Verifier == nil || cfg.Verifier.Rollback == nil {
		t.Fatal("the verifier has no rollbacker to compare the validator against")
	}
	if replayer, ok := cfg.Verifier.Rollback.(*rollback.Replayer); !ok {
		t.Fatalf("the verifier's rollbacker is %T, not *rollback.Replayer", cfg.Verifier.Rollback)
	} else if replayer != p.Replayer {
		t.Error("the plan validator and the rollback replayer are different objects; two paths to " +
			"the API server are two sets of answers, and the plan would be validated against a " +
			"client the replay does not use")
	}
}
