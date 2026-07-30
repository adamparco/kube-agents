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

// Assembly for the broker's steps 3-11. Everything below already existed, unit-tested, and
// unreachable from a running process: `broker.Config.Pipeline` had exactly one implementation in
// this binary and it was `UnavailablePipeline`, the 503. This file is where the adapters written by
// P9-T7c-3d meet the pipeline they were written for.
//
// It is deliberately its own file rather than another eighty lines inside `run`. The reason is
// [[LSN-007]] -- "built, tested, and unreachable" -- and the only durable defence against it is a
// check that can see the wiring. A `pipeline.Config` assembled inline in `run` alongside a listener
// and a TLS handshake is not reachable from a test; one built by a function whose whole output is
// the config is (see wiring_test.go and V-BRK-027).
//
// # No policy lives here
//
// Every number this file could have chosen -- refresh intervals, cache TTLs, the undo planner --
// is left unset so the owning package supplies it. A staleness bound restated here would be a
// second definition site for a value whose whole purpose is to be compared against a limit
// declared next to it, and the two copies drift in the direction nobody notices: the one that
// still says "fresh".
package main

import (
	"context"
	"errors"
	"fmt"

	"k8s.io/client-go/discovery"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/brake"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/budget"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/cooldown"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/escalate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/history"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/livestate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/policy"
	// Aliased: `probe` is already a function in waitforbroker.go, and the init-container's /healthz
	// GET has nothing to do with the verification prober.
	brokerprobe "github.com/gke-labs/kube-agents/k8s-operator/internal/broker/probe"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/refindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/writeahead"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// brokerDeps is everything `run` has already dialled, handed to the assembly so that nothing here
// builds a client of its own.
//
// One client, not several. The broker's reads are mostly its own writes read back, and `run`
// already chose a direct (uncached) client for the reason stated there; a second client built in
// here would be a second answer to "is the cache allowed to be stale", settled by whichever call
// site a reader happened to open.
type brokerDeps struct {
	// RESTConfig is used for exactly one thing: the discovery client. See newDiscovery.
	RESTConfig *rest.Config

	// Client reads and writes objects. Direct, not cached.
	Client client.Client

	// Records is the ActionRecord store. The SAME store the rejection journal writes through --
	// `run` builds it once and passes it here, because a broker with two stores has two answers to
	// "which namespace do records live in".
	Records *journal.Store

	// AgentName, Namespace and ActorServiceAccount identify the agent this broker serves. From the
	// broker's own deployment, never from an envelope (03 §4.1 step 1).
	AgentName           string
	Namespace           string
	ActorServiceAccount string
}

func (d brokerDeps) validate() error {
	switch {
	case d.RESTConfig == nil:
		return errors.New("no REST config; the discovery client cannot be built, and without discovery the blast-radius denominator and the inbound-reference scan both go blind")
	case d.Client == nil:
		return errors.New("no API client")
	case d.Records == nil:
		return errors.New("no ActionRecord store; nothing executes unjournaled (03 §4.1 step 11)")
	case d.AgentName == "":
		return errors.New("no agent name")
	case d.Namespace == "":
		return errors.New("no namespace")
	case d.ActorServiceAccount == "":
		return errors.New("no actor service account; the record's actor field would not say who COULD have written")
	}
	return nil
}

// startable is one background source, in the order it must be brought up.
//
// Two fields rather than one interface because the sources genuinely differ in shape and pretending
// otherwise would hide the difference: `brake` and `cooldown` refresh lazily inside the call that
// needs them and have no loop, while `policy`, `history` and `budget` poll. What they share is
// exactly the part that matters at startup -- a first synchronous read whose failure must stop the
// process.
type startable struct {
	// name appears in the startup error. It is the package name, so an operator reading
	// "startup read for the policy source failed" knows which RBAC rule to look at.
	name string

	// refresh is the first, synchronous read. A returned error is FATAL: see startSources.
	refresh func(context.Context) error

	// run is the polling loop, or nil for a source that refreshes lazily.
	run func(context.Context)
}

// newDiscovery builds the discovery client the two enumerating adapters need.
//
// # Why discovery is not in the broker's RBAC grant, and why that is fine
//
// 06 §2.2.1 lists the broker operations grant exactly, and it contains no `nonResourceURLs` -- the
// VAP that enforces the grant refuses any actor Role that adds one. Discovery reads `/api` and
// `/apis`, which ARE non-resource URLs, so on the face of it this client should be Forbidden.
// It is not: Kubernetes binds the built-in `system:discovery` ClusterRole to the
// `system:authenticated` group out of the box, on GKE as everywhere else, so every authenticated
// ServiceAccount can enumerate API groups without any grant naming it. The broker therefore gets
// discovery without the grant having to widen, which is why the grant can stay byte-identical
// across tiers and still be the whole story for tenant authority.
//
// # Both consumers treat a nil client as an error, not as an empty answer
//
// livestate.Source's denominator and refindex.Source's referrer scan are the two reads that ask
// "what kinds exist", and for both of them an empty kind list is the LOOSENING answer: a scan over
// no kinds finds no references and reports the object free to delete, and a count over no kinds
// yields a denominator of zero. Failing to construct this is therefore a startup error rather than
// a degraded mode.
func newDiscovery(cfg *rest.Config) (discovery.ServerResourcesInterface, error) {
	dc, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("build discovery client: %w", err)
	}
	return dc, nil
}

// pipelineConfig assembles steps 3-11 and returns the sources that must be started.
//
// ctx is the PROCESS lifetime, not a submission's. It is captured by the identity closure below,
// which `policy.Source` calls from its own polling loop -- a loop that has no submission in scope
// and whose reads should end when the broker does. A per-submission context would be the wrong one
// twice over: the poll would be cancelled by whichever request happened to finish first, and the
// identity would be resolved against a deadline chosen by a caller.
//
// The returned slice is ORDERED and the order is load-bearing; see the brake and policy entries.
func pipelineConfig(ctx context.Context, d brokerDeps) (pipeline.Config, []startable, error) {
	if err := d.validate(); err != nil {
		return pipeline.Config{}, nil, fmt.Errorf("pipeline wiring: %w", err)
	}

	disc, err := newDiscovery(d.RESTConfig)
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	// --- the two clients the executor and the replayer share -----------------------------------
	//
	// One applier, used by both. The replayer's doc is explicit that a rollback must run with the
	// SAME credential as the action it reverses -- "a rollback that needed more authority than the
	// action it reverses would be a privilege escalation wearing a safety label" -- and sharing the
	// object is how that is made true by construction rather than by two matching literals.
	reader := &execute.ClientReader{Client: d.Client}
	applier := &execute.ClientApplier{Client: d.Client}

	// --- step 5: the brake ----------------------------------------------------------------------
	//
	// Built first because the policy source's identity closure reads through it.
	brakeSrc, err := brake.NewSource(brake.SourceConfig{
		Reader:    d.Client,
		Journal:   d.Client,
		AgentName: d.AgentName,
		Namespace: d.Namespace,
	})
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	// --- step 4: the classifier's policy set ----------------------------------------------------
	//
	// Identity reads through the brake rather than through a second watcher of the same Agent CR.
	// Two reasons, and the second is the one that matters. The cheap one: the brake already holds a
	// TTL'd read of exactly this object, so a second informer would be a second cache with its own
	// expiry, and the two would disagree for a window nobody bounded. The load-bearing one: the
	// brake's cache is the read the REFUSAL is computed from, so an identity resolved from the same
	// bytes cannot classify against an agent the brake never saw.
	//
	// A nil Agent is BrakeView's "could not read" convention and becomes an error here rather than
	// a zero policy.Agent, because the zero Agent is the fleet-wide identity: it binds only the
	// unscoped, untiered policies, and a ChangePolicy can only tighten, so a policy that fails to
	// bind is a classification LOWER than the operator wrote. That distinction is V-BRK-026's and
	// the reason SourceConfig.Identity returns an error at all.
	identity := func() (policy.Agent, error) {
		v := brakeSrc.Observe(ctx)
		if v.Agent == nil {
			return policy.Agent{}, fmt.Errorf(
				"the Agent CR %s/%s could not be read, so this broker does not know its own (tier, scope); "+
					"classifying against the zero Agent would bind only the fleet-wide policies, which is a classification lower than the operator wrote",
				d.Namespace, d.AgentName)
		}
		return policy.Agent{Tier: v.Agent.Spec.Tier, Scope: scope.Of(v.Agent)}, nil
	}

	// --- the journal-derived sources ------------------------------------------------------------
	historySrc, err := history.NewSource(history.SourceConfig{
		Journal:   d.Client,
		Namespace: d.Namespace,
	})
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	policySrc, err := policy.NewSource(policy.SourceConfig{
		Reader:   d.Client,
		Identity: identity,
		History:  historySrc,
	})
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	cooldownSrc, err := cooldown.NewSource(cooldown.SourceConfig{
		Journal:   d.Client,
		Namespace: d.Namespace,
	})
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	budgetSrc, err := budget.NewSource(budget.SourceConfig{
		Journal:   d.Client,
		Namespace: d.Namespace,
		AgentName: d.AgentName,
	})
	if err != nil {
		return pipeline.Config{}, nil, err
	}

	cfg := pipeline.Config{
		AgentName:           d.AgentName,
		Namespace:           d.Namespace,
		ActorServiceAccount: d.ActorServiceAccount,

		Classifier: policySrc,
		Live:       &livestate.Source{Client: d.Client, Discovery: disc},
		Refs:       &refindex.Source{Client: d.Client, Discovery: disc},
		Reader:     reader,

		// BodyStore is DELIBERATELY nil, and it is the one seam in this file that is a gap rather
		// than a choice. `bodystore.Journal` is the production execute.BodyStore and it needs a
		// `journal.BlobSink` behind it; no production BlobSink exists -- the package ships the
		// interface plus WriterSink and MemorySink, which implement the DIFFERENT AuditSink. So the
		// >1 MiB `objectRef` path of 06 §4.3 has no implementation to point at, and a bodystore
		// constructed over a nil sink would be strictly worse than nil: `execute.capture` already
		// refuses an over-limit body when no store is configured, and a store that exists but
		// cannot store would turn that refusal into a nil-sink error one layer deeper. Nil refuses
		// by name. Allowlisted in wiring_test.go, with this reason, so the allowlist entry
		// disappears the day a sink lands.
		BodyStore: nil,

		Executor: &execute.Executor{
			Applier: applier,
			// The write-ahead confirmer, not the store: V-BRK-006 is that the record is DURABLE
			// before the mutation, and only a read-back can establish that.
			Journal: &writeahead.Confirmer{Records: d.Records, Namespace: d.Namespace},
		},
		Verifier: &verify.Driver{
			Prober: &brokerprobe.Source{Client: d.Client},
			// Same applier as the executor: see above.
			Rollback: &rollback.Replayer{
				Writer: applier,
				Reader: reader,
				// Nil for the same reason BodyStore is nil, and legal by the field's own doc: "a
				// broker with no store configured can still replay every inline step, and a step
				// that needs it then refuses by name rather than by nil dereference".
				Sink: nil,
			},
			Pager:    &escalate.Recorder{Client: d.Client, Namespace: d.Namespace},
			Pauser:   &escalate.Recorder{Client: d.Client, Namespace: d.Namespace},
			Cooldown: cooldownSrc,
		},
		Records: d.Records,
		Brake:   brakeSrc,

		Accountant: budgetSrc,
		// Empty, and known to be. `ContestedIndex` is in-memory and therefore per-broker and lost on
		// restart; rebuilding it from `ActionRecord.status.contested` is P9-T6c's job. An empty
		// index answers "not contested" for everything, which is the loosening direction -- but the
		// alternative available today is nil, and nil makes the brake refuse EVERY action, which is
		// not a safer broker, it is no broker. Recorded here so the gap is read as a gap.
		Contested: broker.NewContestedIndex(),

		// Approvals is nil: no notifier exists yet (04 §3). Step 7 leaves a gated action parked and
		// says so, which is the same outcome a notifier that could not reach anyone produces.
		// Allowlisted in wiring_test.go with this reason.
		Approvals: nil,
	}

	// Ordered. `brake` precedes `policy` because the policy source's first Refresh calls the
	// identity closure, which reads the brake's cache -- an unwarmed brake would make the policy
	// source's startup read fail for a reason that has nothing to do with ChangePolicy, and send
	// whoever reads the error to the wrong RBAC rule.
	sources := []startable{
		{name: "brake", refresh: brakeSrc.Refresh},
		{name: "policy", refresh: policySrc.Refresh, run: policySrc.Run},
		{name: "history", refresh: historySrc.Refresh, run: historySrc.Run},
		{name: "cooldown", refresh: cooldownSrc.Refresh},
		{name: "budget", refresh: budgetSrc.Refresh, run: budgetSrc.Run},
	}
	return cfg, sources, nil
}

// startSources performs every first read synchronously and then starts the pollers.
//
// # A failed first read stops the process
//
// This is what `brake.Source.Refresh` and `budget.Source.Refresh` say in their own doc comments,
// and the argument generalises to all five: every one of them reads either the Agent CR, the
// ChangePolicy list or the ActionRecord list, and every one of those is covered by the 06 §2.2.1
// grant. A failure on the very first read is therefore an RBAC gap, a missing CRD or a broken
// network path -- a deployment fault, not a blip -- and the alternative to exiting is a broker that
// comes up healthy, answers /healthz, and refuses every submission with a different message per
// source. Both are fail-closed. Only one of them says why in a place an operator is already looking.
//
// The pollers are started only after every first read succeeded, so a goroutine is never left
// running behind a process that is about to exit non-zero.
func startSources(ctx context.Context, sources []startable) error {
	for _, s := range sources {
		if err := s.refresh(ctx); err != nil {
			return fmt.Errorf("startup read for the %s source: %w", s.name, err)
		}
	}
	for _, s := range sources {
		if s.run == nil {
			continue
		}
		go s.run(ctx)
	}
	return nil
}
