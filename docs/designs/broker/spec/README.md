# The frozen spec series

## What this directory is

This directory is the verbatim Phase 0–9 design series for the kube-agents action broker, preserved unchanged from the pre-reset history; [`00-series-readme.md`](00-series-readme.md) is the original series map and self-declares "Design complete". The series designed the broker as part of the original three-tier fleet design (never merged); the docs one level up — [`../README.md`](../README.md), [`../architecture.md`](../architecture.md), [`../chat-approval.md`](../chat-approval.md), [`../integration.md`](../integration.md) — are the live design and cite into this directory by section.

## Why it is frozen

Code comments throughout `k8s-operator/internal/broker/**` cite these files by section number, and one test parses a spec table as data: `TestTheReservedKeyListIsTheOneTheSpecPublishes` (`k8s-operator/internal/broker/envelope_roundtrip_test.go:311`) joins `broker.ReservedKeys` to the refusal table in [06 §4.1](06-api-and-data-contracts.md). Editing these files breaks the build's meaning even where it does not break the build. Do not edit them; corrections and deltas go in the live docs one level up.

## How to read it from the module's point of view

Normative for the broker module:

- [06 §4](06-api-and-data-contracts.md) — the action envelope, classification, ActionRecord, and brake/approval contracts.
- [03 §§3–6](03-security-model.md) — the forbidden set, the pipeline, the admission backstop, the brake.
- [04 §§3–5](04-workflow-model.md) — the gated lifecycle, anti-thrash, the recovery ladder, verification predicates.
- [08 §§2.2–2.3](08-agent-runtime-and-identity.md) — the two-identity split, mTLS plus TokenReview.
- [09](09-verification-and-validation.md) — the verification discipline: stable check IDs, levels, gate classes.
- [07 §5](07-implementation-roadmap.md) — the sequencing rule: machinery before authority.

Describing the dead fleet world — read for rationale, not contract:

- The tier system and cascade provisioning ([06 §1](06-api-and-data-contracts.md), [02 §§3–5](02-agent-personas.md)).
- The agent mesh (06 §7, [05 §1.4](05-system-architecture.md)).
- The per-tier identity matrices (06 §2, 03 §3.2).
- The C15 ChatOps gateway as specified (05 §1.8) — superseded by [`../chat-approval.md`](../chat-approval.md).
- The pair-rendering topology (08 §§2.1, 2.4–2.6) — transitional in the module.

Where a live doc and this directory disagree, the live doc governs the module — and must say explicitly that it diverges.
