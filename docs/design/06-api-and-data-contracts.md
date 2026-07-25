# Design 06: API & Data Contracts

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** 01–05 · **Tier:** Buildable (bridging)

---

## TL;DR

The exact interfaces a builder implements against, for a system whose agents **act**:

- the **`Agent` CRD** (`kubeagents.x-k8s.io/v1alpha1`) — tier / scope / parentRef / harness /
  integration / deployment, **plus** the imperative additions: `spec.operations.paused` (the
  brake), an **approval-roster** reference, stricter-only **`ChangePolicy`** references, and action
  status (§1);
- the **identity contract** — the **reader/actor split** ([03](03-security-model.md) §3.1): two
  ServiceAccounts per agent, with literal per-tier RBAC templates for all six identities and their
  cloud IAM mapping (§2);
- the **ChatOps addressing & routing** contract — **Slack-first**, one fleet-level Slack app held
  by the router over Socket Mode, five-step deterministic resolution including **channel bindings**,
  the single `/kage` verb/target grammar and its brake commands, with Google Chat as the supported
  secondary platform (§2b);
- the **journal & IaC-mirror repo layout** — the repo is a **mirror, not a control path** (§3);
- **the action contracts** — the **Action Envelope** an agent submits (with its **anti-replay**
  freshness/nonce/key rules), the deterministic **risk classifier** and `ChangePolicy`, the
  **`ActionRecord`** with its **undo plan**, its **retention _and_ guaranteed-undo-window** clocks,
  and the **pause / freeze / undo / contested** brake objects (§4 — the centrepiece);
- the **OKF** knowledge schema (§5), **session** state keys (§6), the **agent mesh** RPC (§7), the
  **audit & attribution** chain (§8), and the **MCP tool surface**, now write-capable **only as
  envelope builders** (§9).

Namespace convention `kubeagents-system`; all agent labels/annotations use the `kube-agents/`
prefix; API group `kubeagents.x-k8s.io`, version `v1alpha1`.

> **Reading order for an implementer.** §1 → §2 gives you the objects and identities to provision;
> §4 gives you the write path. Build §4 before granting anything in §2's actor column
> ([01](01-vision-scope.md) §6, ordering constraint).

---

## 1. Agent definition — the `Agent` CRD (per persona)

Each agent is defined by one instance of a single, tier-discriminated **`Agent` custom resource**
(`kubeagents.x-k8s.io/v1alpha1`), reconciled by the **kube-agents controller** into an isolated
agent pod **and its Action Broker** ([05](05-system-architecture.md) C1/C-AB,
[08](08-agent-runtime-and-identity.md) §2). The CR selects the **Hermes** harness with the persona's
profile/skills, carries the tier/scope/parent metadata, names the pod's **reader** identity, and
declares the **operational envelope** — pause state, approval roster, change policies, initiative
budget — under which its broker executes.

**The CRD is not an authority-granting surface** ([03](03-security-model.md) §3.4). It carries no
field that can grant RBAC, widen scope, or loosen risk classification. Every field that touches
safety is **stricter-only**: it can tighten the code floor and can never move below it.

### 1.1 CR shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: developer-team-team-x # <tier>-<scope-leaf> by convention
  namespace: team-x # developer-team tier MUST be created in its scoped namespace (§1.2)
spec:
  # ---- identity of the agent (immutable discriminators) --------------------------------------
  tier: developer-team # platform | cluster-admin | developer-team. Immutable (CEL + webhook).
  scope:
    projectId: my-project # all tiers
    clusterName: cluster-a # cluster-admin + developer-team
    namespace: team-x # developer-team only
  parentRef:
    name: cluster-admin-cluster-a # required for non-platform tiers

  # ---- runtime -------------------------------------------------------------------------------
  harness: # struct, not a string (k8s-operator/api/v1alpha1/common_types.go)
    clusterName: cluster-a
    location: us-central1
    projectId: my-project
    hermes: { dashboardEnabled: true, agentHome: /opt/data }
    memory: { memoryEnabled: true, provider: multiuser_memory }
  deployment:
    image: us-docker.pkg.dev/my-project/kube-agents/developer-team-agent
    tag: v1.4.0
    runtimeClassName: null # gVisor sandbox — deferred (08 §5.1)
  security:
    serviceAccountName: developer-team-agent # the READER SA (§2). Read-only. On the agent pod.

  # ---- operational envelope (NEW — the imperative model) -------------------------------------
  operations:
    paused: false # THE BRAKE (03 §6). Broker refuses new envelopes when true.
    pauseReason: "" # free text, set with paused; surfaced in chat and status
    dryRunOnly: false # shadow mode: classify+journal, never execute. Stricter-only.
    approvalRosterRef:
      name: team-x-approvers # ApprovalRoster consulted for `gated` actions (§4.4)
    changePolicyRefs: # stricter-only classification overlays (§4.2). Ordered, all applied.
      - name: baseline-conservative
      - name: team-x-pci
    initiativeBudget: # PER CLASS and per origin. Caps only; never above the code ceiling
      # ^ see "Initiative budget, per class" below for the ceilings and the accounting rules
      selfInitiated: # trigger.source ∈ {watch, alert, cron, delegation, escalation}
        routinePerHour: 30 # code ceiling 50
        elevatedPerHour: 6 # code ceiling 10
        gatedPerHour: 3 # code ceiling 5 — counts submissions, not approvals
        actionsPerDay: 200 # code ceiling 500 — all classes together
      humanRequested: # trigger.source ∈ {chat, undo}
        routinePerHour: 120 # code ceiling 200
        elevatedPerHour: 40 # code ceiling 60
        gatedPerHour: 20 # code ceiling 30
        actionsPerDay: 800 # code ceiling 2000
      maxObjectsPerAction: 25 # code ceiling 50 (the §4.2 per-action gate threshold)
      flapWindow: 30m # code floor 5m (a shorter window is rejected)
      flapThreshold: 3 # code ceiling 5
    notifyOn: elevated # routine | elevated | gated — minimum class that pings humans at once

  # ---- integrations --------------------------------------------------------------------------
  integration:
    github:
      gitRepo: https://github.com/acme/gitops
      mirror: # write-behind IaC/journal mirror (§3). NOT a control path.
        enabled: true
        mode: both # state | log | both
        branch: main

    # SLACK — the reference chat platform (§2b). Enabled by default on generated CRs.
    # Only PER-AGENT facts live here. The Slack *app* (tokens, Socket Mode, the one registered
    # slash command) is FLEET-LEVEL and lives on `ChatOpsConfig` — see below.
    slack:
      enabled: true
      allowedUsers: # closed allowlist — required when enabled (V-7)
        - slack:U02ABCDEF # platform-qualified, immutable member ID (V-11).
        - slack:U07GHIJKL # NEVER @handle, display name, or email — those are reassignable.
      channelBindings: # OPTIONAL. A bound channel routes bare messages here (§2b, step 4).
        - channelId: C01TEAMXOPS # Slack channel ID, never "#team-x-ops" — names are mutable.
          channelName: team-x-ops # advisory, for humans and status; never matched on.
          requireMention: false # true ⇒ only @kage-prefixed messages in this channel bind.
      # Reports, gate prompts, and Block Kit approvals for this agent. Defaults to
      # channelBindings[0].channelId, then the roster's notify.slack.channel (§4.4).
      notifyChannel: C01TEAMXOPS

    # GOOGLE CHAT — secondary, opt-in. Same routing semantics, different ingress (§2b parity).
    googleChat:
      enabled: false
      projectId: my-project # project owning the Pub/Sub push subscription
      topicName: kage-chat
      subscriptionName: kage-chat-sub
      allowedUsers: [] # e.g. ["googlechat:users/1234567890"] — required when enabled (V-7)
      spaceBindings: # the Chat parity of channelBindings
        - spaceId: spaces/AAAA
          requireMention: true # Chat DMs deliver unprefixed; spaces normally require @kage
      notifySpace: spaces/AAAA
status:
  phase: Ready # Pending | Provisioning | Ready | Degraded | Paused | Failed
  address: developer-team-team-x.team-x.svc.cluster.local
  lastReconcileTime: "2026-07-24T18:02:11Z"
  conditions: [] # Ready, BrokerReady, JournalReachable, BudgetExhausted, Frozen, Paused
  deploymentStatus: { name: developer-team-team-x, readyReplicas: 1 }
  serviceStatus: { endpoint: https://developer-team-team-x.team-x.svc:8444 }
  storageStatus: { bound: true }
  chatStatus: # what the router actually resolved for this agent (§2b)
    primaryPlatform: slack # slack | googlechat | none
    boundChannels: ["slack:C01TEAMXOPS"] # platform-qualified; conflicts are reported, not merged
    notifyTarget: slack:C01TEAMXOPS
    reachable: true # false ⇒ chat ingress is down; the kubectl/API brake is unaffected (§4.4)

  # ---- action-pipeline status (NEW) ----------------------------------------------------------
  operations:
    paused: false
    pausedSince: null
    pausedBy: "" # chat user id or K8s username that set the brake
    reason: ""
    dryRunOnly: false
    frozenBy: "" # name of the FleetFreeze covering this scope, if any (§4.4)
  broker:
    endpoint: https://developer-team-team-x-broker.team-x.svc:8443
    actorServiceAccount: developer-team-team-x-actor
    ready: true
    journalReachable: true # false ⇒ the broker is fail-closed and executing nothing
  lastAction:
    actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
    name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6
    riskClass: routine
    status: Verified
    intent: restart crash-looping deployment api-gateway
    target: apps/v1/Deployment team-x/api-gateway
    completionTime: "2026-07-24T17:58:44Z"
  pendingApprovals: 1 # count of ActionRecords in PendingApproval for this agent
  pendingApprovalRefs: # capped at 8 most recent; the journal is authoritative
    - ar-01j8z3a1b2c3d4e5f6g7h8j9k0
  budget: # mirrors the spec shape one-for-one, so "which bucket is empty" is readable
    windowStart: "2026-07-24T17:00:00Z" # start of the current rolling hour
    dayWindowStart: "2026-07-24T00:00:00Z"
    selfInitiated:
      { routineUsed: 7, elevatedUsed: 1, gatedUsed: 0, dayUsed: 23 }
    humanRequested:
      { routineUsed: 12, elevatedUsed: 0, gatedUsed: 1, dayUsed: 31 }
    exhaustedBuckets: [] # e.g. ["selfInitiated.elevatedPerHour"] — names the empty bucket
    exhausted: false # true iff exhaustedBuckets is non-empty
    cooldownUntil: null
  counters:
    actions24h: 41
    undone24h: 1
    contestedTargets: 1
    forbiddenAttempts24h: 0
    verificationFailures24h: 0
```

**No field names the actor.** `spec.security.serviceAccountName` names the **reader** SA only, and
V-10 constrains it to the tier template's name. The **actor** SA is _derived_ from `tier` + `scope`
(§2) and looked up by name — the CRD has, and must never gain, a `brokerServiceAccountName` /
`actorServiceAccountName` / equivalent. The ability to name the actor identity is the ability to
point a broker at a more privileged one, which is exactly the self-escalation
[03](03-security-model.md) §3.3 and §3.4 exist to make unrepresentable
([08](08-agent-runtime-and-identity.md) §2.1). The controller publishes the resolved name in
`status.broker.actorServiceAccount` — **status, not spec**: observable, not settable.

**Field provenance.** `tier` / `scope` / `parentRef` / `harness` / `deployment` / `security` /
`integration` and the base `status` fields exist today in
`k8s-operator/api/v1alpha1/{agent_types.go,common_types.go}` — keep their names and nesting.
`spec.operations` and `status.{operations,broker,lastAction,pendingApprovals,budget,counters}` are
**new** and are what the imperative model adds. `spec.iac.format` (`kcc` | `terraform`) survives but
its meaning narrows: it now selects the **mirror** artifact format (§3), not the actuation artifact.

**Retired.** Nothing in the CRD refers to proposals, suggestions, branches, or PRs. The
`submit-suggestion` propose path is gone (§9).

#### Chat app configuration is fleet-level — `ChatOpsConfig`

**Slack app credentials and command registration are not per-agent facts, and the CRD must not
pretend they are.** A Slack **app token permits exactly one Socket Mode connection**, and Slack
registers **slash commands statically, per app** — so a per-agent Slack block carrying its own
tokens is either unusable (the second agent's connection is refused) or a fleet-wide setting typed
`n` times and free to disagree. This is the concrete reason the previous generation shipped
`slack.enabled: false` on every child tier: the per-pod Slack relay could not serve more than one
tier, so only one could have it. **That relay is retired.** The single connection is held by the
**ChatOps router** ([05](05-system-architecture.md) C15), which turns a hard platform constraint
into the reason the router exists — and, because Socket Mode is an outbound WebSocket, needs **no
public ingress**, which is what makes it usable on a private cluster.

The fleet-level configuration therefore lives in **one cluster-scoped singleton**, read **only** by
the router's ServiceAccount, in `kubeagents-system`:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ChatOpsConfig
metadata: { name: default } # singleton. A second object is rejected (V-13)
spec:
  defaultPlatform: slack # slack | googlechat. Governs generated CRs and `kubectl kage` output.
  slack:
    enabled: true
    commandName: kage # THE one slash command: `/kage`. Registered once, in the Slack app manifest.
    botUserId: U09KAGEBOT # the bot's own member ID — so its own messages are never re-ingested
    teamId: T01ACME # the workspace this app is installed in; a foreign team_id is dropped
    socketMode:
      enabled: true # DEFAULT and reference path. Outbound WSS; no request URL, no ingress, no TLS.
      appTokenSecretRef: # `xapp-…`, scope connections:write
        { name: kage-slack, key: app-token }
      maxConnections: 1 # CODE CONSTANT. Slack permits one per app token; >1 is rejected, not clamped.
    botTokenSecretRef: { name: kage-slack, key: bot-token } # `xoxb-…` — chat.postMessage, views.*
    signingSecretRef: { name: kage-slack, key: signing-secret } # only consulted when httpMode is on
    httpMode:
      enabled: false # Events API alternative. Requires public ingress; opt-in, never the default.
      publicURL: ""
    interactivity:
      blockKit: true # approve/reject rendered as Block Kit buttons (§2b.1). Convenience, not authz.
  googleChat: # secondary; unset when googleChat is disabled fleet-wide
    enabled: false
    appName: kage
    projectId: my-project # Pub/Sub project for the push subscription
    audience: "" # the Chat app's OIDC audience, verified on every inbound push
status:
  slack:
    connected: true
    connectionSince: "2026-07-24T17:12:03Z"
    teamId: T01ACME
    registeredCommands: ["/kage"] # reconciled against the app manifest; drift is a condition
  googleChat: { connected: false }
  boundChannels: 7 # total across the fleet
  conditions: [] # SlackConnected, CommandsRegistered, ChannelBindingConflict, CredentialsValid
```

Secret refs resolve in `kubeagents-system` only — a cross-namespace `namespace:` field is
deliberately absent, so no tenant can point the router at a Secret it controls.
`ChatOpsConfig` **grants nothing**: it names no humans, carries no allowlist, and is absent from
every actor template (§2.2), exactly like `FleetFreeze` and `ApprovalRoster` (§4.4). Who may talk to
an agent stays on that agent's CR, where the scope that owns it can see it.

**One connection ⇒ one active router.** The router Deployment runs a single active replica per
Socket Mode connection; additional replicas stand by under lease-based leader election and hold no
connection. Scaling chat ingress is therefore an availability problem, not a throughput one —
which is acceptable because **dispatch is not chat-bound** (§2b) and the brake never is (§4.4).

**Initiative budget, per class.** The budget is **not** class-agnostic. A single
`actionsPerHour` number cannot express the agreed default — **50 `routine` + 10 `elevated` per agent
per hour** ([05](05-system-architecture.md) §6) — because those two are different allowances that
must be exhausted independently: burning the routine bucket on log-noise cleanup must not consume
the headroom an incident needs for its one `elevated` scale-up. The budget is therefore a
**two-dimensional** cap: **origin** (self-initiated vs human-requested,
[04](04-workflow-model.md) §4.2) × **risk class**.

| Field (`spec.operations.initiativeBudget.…`) | Type     | Default | Code ceiling / floor | Meaning                                                                                                                                                        |
| -------------------------------------------- | -------- | ------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `selfInitiated.routinePerHour`               | int      | `30`    | ceiling **50**       | `routine` actions the agent started itself, per rolling hour                                                                                                   |
| `selfInitiated.elevatedPerHour`              | int      | `6`     | ceiling **10**       | `elevated` sub-cap — deliberately an order of magnitude tighter                                                                                                |
| `selfInitiated.gatedPerHour`                 | int      | `3`     | ceiling **5**        | `gated` **submissions**; approval consumes nothing (a human is already in the loop)                                                                            |
| `selfInitiated.actionsPerDay`                | int      | `200`   | ceiling **500**      | All classes together, per rolling 24 h                                                                                                                         |
| `humanRequested.routinePerHour`              | int      | `120`   | ceiling **200**      | Separate, larger allowance for `trigger.source ∈ {chat, undo}`                                                                                                 |
| `humanRequested.elevatedPerHour`             | int      | `40`    | ceiling **60**       | —                                                                                                                                                              |
| `humanRequested.gatedPerHour`                | int      | `20`    | ceiling **30**       | —                                                                                                                                                              |
| `humanRequested.actionsPerDay`               | int      | `800`   | ceiling **2000**     | —                                                                                                                                                              |
| `maxObjectsPerAction`                        | int      | `25`    | ceiling **50**       | Per-envelope object cap. 50 is where the code floor gates regardless (§4.2), so a higher value is meaningless and is rejected rather than accepted-and-ignored |
| `flapWindow`                                 | duration | `30m`   | **floor 5m**         | A shorter window is rejected                                                                                                                                   |
| `flapThreshold`                              | int      | `3`     | ceiling **5**        | Repeats of the same `(target, intent)` in `flapWindow`                                                                                                         |

**Accounting rules, stated so two implementations agree.**

- **Origin is derived from `trigger.source` in the envelope, never asserted separately.**
  `chat` and `undo` draw on `humanRequested`; `watch`, `alert`, `cron`, `delegation`, and
  `escalation` draw on `selfInitiated`. A **`delegation` from a parent spends the callee's
  self-initiated bucket** (§7 rule 8) — a chatty parent must not be able to spend a child's human
  allowance.
- **One action decrements exactly one class bucket** — the class the classifier finally assigned —
  plus the matching `actionsPerDay` counter. A `+1` escalation moves the charge to the higher bucket.
- **`Rejected`, `forbidden`, and deduplicated envelopes decrement nothing** (they never executed);
  **`DryRun` decrements nothing**; a `RolledBack` action **does** decrement, because it ran.
- **Undo is exempt from every hourly bucket**: `trigger.source: undo` is never refused for budget
  reasons, for the same reason a `FleetFreeze` still permits undo (§4.4). It still increments
  `humanRequested.dayUsed` for observability.
- **Exhaustion escalates; it does not pause.** The broker refuses with `429 budget-exhausted` and
  a `retryAfterSeconds` to the next window boundary, and names the empty bucket in
  `status.budget.exhaustedBuckets`. Auto-pause is reserved for repeated `forbidden` attempts, a
  failed rollback, and anomaly detection ([04](04-workflow-model.md) §4.2, and 09 §12.1 X-12).

### 1.2 Per-tier field usage, cardinality & validation

| `tier`           | Required `spec.scope`                   | `parentRef`             | `metadata.namespace`    | Cardinality     | Reader SA              | Actor SA                           |
| ---------------- | --------------------------------------- | ----------------------- | ----------------------- | --------------- | ---------------------- | ---------------------------------- |
| `platform`       | `projectId`                             | — (root)                | `kubeagents-system`     | 1 per project   | `platform-agent`       | `platform-<project>-actor`         |
| `cluster-admin`  | `projectId`, `clusterName`              | parent = platform agent | `kubeagents-system`     | 1 per cluster   | `cluster-admin-agent`  | `cluster-admin-<cluster>-actor`    |
| `developer-team` | `projectId`, `clusterName`, `namespace` | parent = cluster-admin  | **= `scope.namespace`** | 1 per namespace | `developer-team-agent` | `developer-team-<namespace>-actor` |

**Validation rules (all v1, all enforced — not conventions).** V-1…V-5 exist today in
`k8s-operator/internal/webhook/agent_webhook.go`; **V-6 is new and required by the inversion**
([03](03-security-model.md) §4.2).

| #        | Rule                                                                                                                                                                                                                         | Where                                                                                     | Failure                               |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------- |
| **V-1**  | `spec.tier` ∈ {`platform`,`cluster-admin`,`developer-team`}; **immutable** after create                                                                                                                                      | CRD enum + CEL `self == oldSelf` + webhook                                                | `Invalid`                             |
| **V-2**  | Per-tier required `scope` fields present (table above)                                                                                                                                                                       | validating webhook                                                                        | `Required`                            |
| **V-3**  | `parentRef.name` present for non-platform tiers                                                                                                                                                                              | validating webhook                                                                        | `Required`                            |
| **V-4**  | **Developer-team placement:** `metadata.namespace == spec.scope.namespace`                                                                                                                                                   | validating webhook                                                                        | `Invalid`                             |
| **V-5**  | **`(tier, scope)` cardinality:** exactly one non-terminating `Agent` per identity key                                                                                                                                        | validating webhook (cluster-wide `List`)                                                  | `Duplicate`                           |
| **V-6**  | **Cross-object ceiling — NEW, v1:** the child's scope must be a **strict subset** of `parentRef`'s scope, and the parent's tier must be the tier immediately above the child's                                               | validating webhook (reads the parent CR)                                                  | `Invalid`                             |
| **V-7**  | **Closed allowlist:** an enabled chat integration (`slack` _or_ `googleChat`) must carry a non-empty `allowedUsers`, and an all-blank/whitespace list is **not** an allowlist — it is empty                                  | CRD CEL + validating webhook                                                              | `Required`                            |
| **V-8**  | **Budget clamp, per class:** any `initiativeBudget` leaf above its code ceiling — or `flapWindow` below the 5m floor — is **rejected**, not silently clamped. Checked leaf-by-leaf against the §1.1 table                    | validating webhook                                                                        | `Invalid`                             |
| **V-9**  | **No authority fields:** the schema is closed; an unknown field under `spec` — in particular anything named `rbac`, `rules`, `riskClass`, `allow`, `bypass`, `scopeOverride` — is pruned/refused                             | CRD structural schema (`x-kubernetes-preserve-unknown-fields` is **never** set on `spec`) | field pruned; CI test asserts absence |
| **V-10** | **Reader-only SA override:** `spec.security.serviceAccountName` may name only the **reader** SA and must match the tier template pattern `^<tier>-agent$`. There is **no** field anywhere in the CRD that names the actor SA | validating webhook                                                                        | `Invalid`                             |
| **V-11** | **Platform-qualified principals — NEW:** every `allowedUsers` entry matches `^(slack\|googlechat):\S+$` with a platform-native **immutable user ID**; a bare ID, `@handle`, display name, or email is refused                | CRD CEL pattern + validating webhook                                                      | `Invalid`                             |
| **V-12** | **Channel binding is exclusive — NEW:** a `(platform, channelId)` / `(platform, spaceId)` pair may be bound by **at most one** `Agent` across the fleet; a second binding is a conflict, exactly like V-5's `(tier, scope)`  | validating webhook (cluster-wide `List` over the binding index)                           | `Duplicate`                           |
| **V-13** | **`ChatOpsConfig` singleton — NEW:** at most one object, named `default`; `slack.socketMode.maxConnections` may only be `1`; a chat platform enabled on any `Agent` must be enabled in `ChatOpsConfig`                       | CRD CEL (`self.metadata.name == 'default'`) + validating webhook                          | `Invalid` / `Duplicate`               |

**V-11 in detail — why the principal format is a hard schema rule, not a style guide.** An
allowlist entry must be the platform's **immutable** identifier: a Slack member ID (`U…`, or `W…`
on Enterprise Grid) or a Google Chat resource name (`users/<numeric-id>`). Display names, `@`
handles, and email addresses are all **mutable and reassignable** — a departed employee's handle
can be taken by a new hire, at which point an allowlist built on it silently transfers authority to
a person nobody granted it to. Refusing the mutable forms at admission is the only place that
failure mode is cheap to prevent.

```text
principal := <platform> ":" <id>
  platform ∈ { slack, googlechat }
  slack      → ^U[A-Z0-9]{6,}$ | ^W[A-Z0-9]{6,}$       e.g. slack:U02ABCDEF
  googlechat → ^users/[0-9]{6,}$                        e.g. googlechat:users/1234567890

REFUSED: "U02ABCDEF" (unqualified) · "slack:@aparco" · "slack:A. Parco"
         "aparco@acme.com" · "" · "   " · "slack:" · "discord:1234"
```

The same canonical string is what §4.1's `requester` (`{platform, id}`) canonicalizes to, what
§4.4's `ApprovalRoster.spec.approvers[]` entries canonicalize to (`platform` + `id` → `<platform>:<id>`),
what `UndoRequest.spec.requestedBy` / `FleetFreeze.spec.requestedBy` carry, and what §8 records as
`kubeagents.requester` — one comparison function, `internal/principal.Canonical`, used everywhere a
human is matched against a list. A principal from one platform **never** matches a list entry from
another: `slack:U02ABCDEF` and `googlechat:users/1234567890` are different principals even when
they are the same human, because nothing in either platform proves that.

**V-12 in detail — the channel is an address, never an authority.** A bound channel makes a bare
message resolvable (§2b step 4); it does **not** admit the people in it. Every message from a bound
channel is still checked against the target agent's `allowedUsers`, per turn, per sender. A channel
bound to two agents would make a bare message ambiguous, and "ambiguous" is the one thing
deterministic routing may not be — so the second binding is rejected at admission rather than
resolved by a rule nobody can predict. Rejection is `Duplicate` on
`spec.integration.slack.channelBindings[i].channelId`, naming the `Agent` that already holds it.
Unbinding is deleting the entry; the router's binding index is **derived** from the CRs, so there
is no separate table to drift (the same property §2b's handle map has).

**V-6 in detail** (the difference between "a parent cannot express an over-grant" and "a parent
cannot cause one"). For a candidate child `C` with parent `P`:

```text
tier(P) must be the immediate parent tier of tier(C)
  platform → cluster-admin → developer-team

scope(C) ⊂ scope(P), evaluated field-wise:
  C.projectId   == P.projectId                                   (always)
  C.clusterName == P.clusterName    when P.tier == cluster-admin
  C.namespace   != ""               when C.tier == developer-team
and scope(C) != scope(P)                                          (strict subset)

P must not be terminating, and P.spec.operations.paused must be false
  (a paused parent may not provision — the brake covers provisioning too)
```

Rejection is `Invalid` on `spec.parentRef.name` with the offending comparison in the message. The
same predicate is reused by the broker when classifying a child-provisioning envelope (§4.2) and by
`vap-agent-scope` when the child's RBAC objects are submitted ([03](03-security-model.md) §4.2), so
the ceiling is checked three times by three mechanisms that fail independently.

**Cardinality key.** `identity = tier + "/" + projectId [+ "/" + clusterName [+ "/" + namespace]]`,
computed by `internal/agentindex.ScopeIdentity`. The same function keys the ChatOps routing index
(§2b) and the actor SA name derivation (§2), so routing, identity, and cardinality can never drift.

---

## 2. Identity contract — the reader / actor split

Every `Agent` CR is served by **two** ServiceAccounts ([03](03-security-model.md) §3.1). This is
the single most load-bearing contract in the document: it is what makes "the LLM holds no write
credential" a structural fact rather than a policy.

| Identity   | SA name                | Namespace                                | Held by        | Authority                                                   | Labels                                                                             |
| ---------- | ---------------------- | ---------------------------------------- | -------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Reader** | `<tier>-agent`         | `kubeagents-system` (dev-team: its `ns`) | The agent pod  | `get`/`list`/`watch` within scope. **No write verb, ever.** | `kube-agents/tier: <tier>`, `kube-agents/role: reader`                             |
| **Actor**  | `<tier>-<scope>-actor` | same as the reader                       | The broker pod | Scoped read-write minus the forbidden set                   | `kube-agents/tier: <tier>`, `kube-agents/role: actor`, `kube-agents/scope: <leaf>` |

`<scope>` is the tier's scope **leaf**: project (platform), cluster (cluster-admin), namespace
(developer-team). If `<tier>-<scope>-actor` exceeds 253 characters the leaf is truncated to 40
characters and suffixed with the first 8 hex digits of `sha256(identity)`; the controller records
the resolved name in `status.broker.actorServiceAccount`.

**Provisioning rules (unchanged in shape from the read-only generation).**

1. Identity derives from `tier` + `scope` **alone**. The CR requests nothing.
2. The controller **references** these SAs by name (pod `serviceAccountName`); it **mints no RBAC**
   at runtime. The reader SA comes from `spec.security.serviceAccountName`; the actor SA name is
   **derived**, not configurable — a CR cannot point its broker at a different identity.
3. RBAC objects are rendered from a **constant per-tier template** by the render overlay
   (`policy/rbac-overlay/<tier>.yaml`) and applied out-of-band, or by a **parent** provisioning a
   child (§4.2 of [03](03-security-model.md)). The parent supplies only `(tier, scope, parent)`.
4. `kube-agents/role` is the label `vap-agent-scope` selects on. A reader-labelled SA bound to any
   rule containing a write verb is **denied at admission**, independently of who submits it.

### 2.1 Reader templates (3 tiers)

Read-only, identical in shape to the shipped `examples/gitops-repo/policy/rbac-overlay/*.yaml`;
the only change is the added `kube-agents/role: reader` label.

```yaml
# platform reader — cluster-wide read, plus CRD and provisioning-CR read.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-agent-explorer
  labels: { kube-agents/tier: platform, kube-agents/role: reader }
rules:
  - apiGroups:
      [
        "",
        apps,
        batch,
        networking.k8s.io,
        rbac.authorization.k8s.io,
        autoscaling,
        policy,
      ]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [apiextensions.k8s.io]
    resources: [customresourcedefinitions]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources:
      [agents, actionrecords, changepolicies, approvalrosters, fleetfreezes]
    verbs: [get, list, watch]
  - apiGroups: ["*.cnrm.cloud.google.com"] # KCC provisioning CRs, where the customer runs them
    resources: ["*"]
    verbs: [get, list, watch]
---
# cluster-admin reader — cluster-wide read within its one cluster (the cluster IS the boundary).
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-admin-agent-explorer
  labels: { kube-agents/tier: cluster-admin, kube-agents/role: reader }
rules:
  - apiGroups:
      [
        "",
        apps,
        batch,
        networking.k8s.io,
        rbac.authorization.k8s.io,
        autoscaling,
        policy,
        storage.k8s.io,
      ]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources:
      [agents, actionrecords, changepolicies, approvalrosters, fleetfreezes]
    verbs: [get, list, watch]
---
# developer-team reader — NAMESPACED Role. A ClusterRole labelled tier=developer-team is a
# wrong-scope grant and is denied by vap-agent-scope.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer-team-agent-explorer
  namespace: team-x
  labels: { kube-agents/tier: developer-team, kube-agents/role: reader }
rules:
  - apiGroups: ["", apps, batch, networking.k8s.io, autoscaling, policy]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
```

**Universal reader prohibitions** (asserted by test, not just by omission): no verb outside
`get`/`list`/`watch`; no `escalate`/`bind`/`impersonate`; no `create` on `subjectaccessreviews`
(that belongs to the deferred §2a); no access to `secrets` **write**; no `pods/exec`,
`pods/attach`, `pods/portforward`.

### 2.2 Actor templates (3 tiers)

These are the literal rule bodies the render overlay emits and `vap-agent-scope` validates against.
A rule not present here is not grantable to an actor identity.

```yaml
# ---------------------------------------------------------------------------------------------
# developer-team ACTOR — namespaced Role in its one namespace. The narrowest identity in the system.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer-team-team-x-actor
  namespace: team-x
  labels:
    {
      kube-agents/tier: developer-team,
      kube-agents/role: actor,
      kube-agents/scope: team-x,
    }
rules:
  - apiGroups: [""]
    resources:
      [
        pods,
        services,
        configmaps,
        secrets,
        serviceaccounts,
        persistentvolumeclaims,
        endpoints,
      ]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [pods/log, pods/status, events]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [pods/eviction]
    verbs: [create]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [apps]
    resources: [deployments/scale, statefulsets/scale, replicasets/scale]
    verbs: [get, update, patch]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [autoscaling]
    resources: [horizontalpodautoscalers]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [policy]
    resources: [poddisruptionbudgets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, networkpolicies]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [gateway.networking.k8s.io]
    resources: [httproutes, grpcroutes]
    verbs: [get, list, watch, create, update, patch, delete]
# NOT GRANTED, deliberately: rbac.authorization.k8s.io (any verb) — a namespace agent may not author
# RoleBindings, because a RoleBinding is the one namespaced object that can name an agent identity
# (03 §3.3 rule 1). Also not granted: resourcequotas/limitranges writes (the cluster-admin tier owns
# them — a tenant cannot raise its own quota), pods/exec, pods/attach, pods/portforward, and any
# cluster-scoped resource whatsoever.
```

```yaml
# ---------------------------------------------------------------------------------------------
# cluster-admin ACTOR — ClusterRole, bounded to its one cluster by *being installed only there*.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-admin-cluster-a-actor
  labels:
    {
      kube-agents/tier: cluster-admin,
      kube-agents/role: actor,
      kube-agents/scope: cluster-a,
    }
rules:
  # Tenancy: namespaces and their guardrails.
  - apiGroups: [""]
    resources:
      [
        namespaces,
        resourcequotas,
        limitranges,
        serviceaccounts,
        configmaps,
        secrets,
      ]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies, ingresses, ingressclasses]
    verbs: [get, list, watch, create, update, patch, delete]
  # Tenant RBAC + child (developer-team) identity provisioning. NAMESPACED RBAC ONLY.
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [roles, rolebindings]
    verbs: [get, list, watch, create, update, patch, delete]
  # Workloads across the cluster (add-ons and tenant remediation).
  - apiGroups: ["", apps, batch, autoscaling, policy]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Nodes: cordon/label/drain. No node CREATE (that is a node-pool operation, below).
  - apiGroups: [""]
    resources: [nodes]
    verbs: [get, list, watch, update, patch, delete]
  - apiGroups: [""]
    resources: [pods/eviction]
    verbs: [create]
  - apiGroups: [storage.k8s.io]
    resources: [storageclasses, csidrivers, volumeattachments]
    verbs: [get, list, watch, create, update, patch, delete]
  # Node pools for its own cluster, via KCC.
  - apiGroups: [container.cnrm.cloud.google.com]
    resources: [containernodepools]
    verbs: [get, list, watch, create, update, patch, delete]
  # Provisioning its children (developer-team Agent CRs). The child ⊆ parent ceiling is enforced by
  # the webhook (V-6), NOT by RBAC — RBAC cannot express "children only".
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [agents]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
# NOT GRANTED: clusterroles, clusterrolebindings (a developer-team child needs neither — its
# template is namespaced); admissionregistration.k8s.io; apiextensions.k8s.io; container-cluster
# lifecycle (containerclusters — that is the platform tier); anything in the platform's cloud scope.
```

**The `kube-system` add-on allowlist** ([03](03-security-model.md) §3.3 rule 6). Protected
namespaces are `kube-system` and `kubeagents-system`; the forbidden set refuses every write into
them **except** the objects below, and only for the **cluster-admin** tier. This list is the "narrow,
explicitly declared allowlist" 03 promises and does not otherwise exist anywhere.

It is enumerated as **named objects, never as kinds**. A kind-level allowlist (`ConfigMaps in
kube-system`) would readmit `kube-root-ca.crt`, `extension-apiserver-authentication`, and every
add-on's authz config in one line — which is the whole namespace back again. Object-level naming
makes the reviewable unit "may this agent edit _this_ object" rather than "does this agent operate
add-ons".

```go
// Code constant. Not a ConfigMap, not a ChangePolicy field, not a CRD field: like the forbidden
// set itself (03 §3.3), changing it is a code change with a review, not a runtime grant.
// internal/broker/classify/protected.go
var KubeSystemAddonAllowlist = []AllowlistEntry{
  // {Group, Kind, Namespace, Name, Verbs}          purpose
  {"apps", "DaemonSet",  "kube-system", "kube-dns",                     WriteVerbs},   // CoreDNS/kube-dns tuning
  {"apps", "Deployment", "kube-system", "kube-dns",                     WriteVerbs},
  {"apps", "Deployment", "kube-system", "kube-dns-autoscaler",          WriteVerbs},
  {"",     "ConfigMap",  "kube-system", "kube-dns",                     WriteVerbs},   // stub/forward zones
  {"",     "ConfigMap",  "kube-system", "coredns",                      WriteVerbs},
  {"",     "ConfigMap",  "kube-system", "kube-dns-autoscaler",          WriteVerbs},
  {"",     "ConfigMap",  "kube-system", "cluster-autoscaler-status",    ReadVerbs},    // read-only: a signal, not a knob
  {"apps", "DaemonSet",  "kube-system", "metrics-server",               WriteVerbs},
  {"apps", "Deployment", "kube-system", "metrics-server",               WriteVerbs},
  {"apps", "DaemonSet",  "kube-system", "fluentbit-gke",                WriteVerbs},   // GKE logging agent resources
  {"apps", "DaemonSet",  "kube-system", "gke-metrics-agent",            WriteVerbs},
  {"apps", "DaemonSet",  "kube-system", "netd",                         WriteVerbs},
  {"",     "ConfigMap",  "kube-system", "ingress-nginx-controller",     WriteVerbs},   // where the customer runs it
  {"apps", "Deployment", "kube-system", "ingress-nginx-controller",     WriteVerbs},
  {"policy","PodDisruptionBudget","kube-system","*-pdb",                WriteVerbs},   // ONLY suffix wildcard allowed
}
```

| Property           | Rule                                                                                                                                                                                                                |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tier               | **`cluster-admin` only.** A `platform` or `developer-team` envelope touching `kube-system` is `forbidden` with no allowlist consultation at all                                                                     |
| Namespace          | `kube-system` only. **`kubeagents-system` has no allowlist and never gains one** — it holds the control plane the agent is not permitted to tamper with ([03](03-security-model.md) §3.3 rule 3)                    |
| Matching           | Exact `(group, kind, namespace, name)`. The **only** permitted wildcard is a trailing `*` in `name`, and only where listed above; a leading or interior `*` is a compile-time error in the table's validator        |
| Verbs              | `WriteVerbs = {create, update, patch}`. **`delete` is never on the allowlist** — deleting a cluster add-on is not a repair, and the entry that would permit it does not exist                                       |
| Class              | An allowlisted write is **never `routine`**: it classifies at **`elevated`** minimum, and `+1` to `gated` if the cluster carries the production label (§4.2)                                                        |
| Enforcement points | The broker (allowlist check inside `forbidden-set` evaluation) **and** `vap-agent-scope`, which carries the identical list rendered into CEL. A drift test asserts the two lists are byte-equal after normalization |

```yaml
# ---------------------------------------------------------------------------------------------
# platform ACTOR — ClusterRole on the hub/management cluster + the project cloud identity.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-my-project-actor
  labels:
    {
      kube-agents/tier: platform,
      kube-agents/role: actor,
      kube-agents/scope: my-project,
    }
rules:
  # Cluster + node-pool lifecycle, and project cloud resources, via KCC.
  - apiGroups: [container.cnrm.cloud.google.com]
    resources: [containerclusters, containernodepools]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups:
      [
        compute.cnrm.cloud.google.com,
        iam.cnrm.cloud.google.com,
        monitoring.cnrm.cloud.google.com,
      ]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Fleet policy for TENANTS (not for agents, and not the kube-agents VAPs).
  - apiGroups: [constraints.gatekeeper.sh, templates.gatekeeper.sh, kyverno.io]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Provisioning its children (cluster-admin Agent CRs) and their identities.
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [agents]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [roles, rolebindings, clusterroles, clusterrolebindings]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [namespaces, serviceaccounts, configmaps, secrets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
# NOT GRANTED: admissionregistration.k8s.io (validatingadmissionpolicies / -bindings —
# vap-agent-scope is control plane, 03 §3.3 rule 3); apiextensions.k8s.io (the Agent CRD itself);
# resourcemanager IAM roles that could bind a principal at project level to an agent GSA.
```

**Three properties the platform actor's RBAC rules depend on, spelled out because they are easy to
lose:**

1. **Kubernetes' built-in escalation prevention does the heavy lifting on the RBAC grants.** An
   actor SA may `create` a `ClusterRole`, but the API server refuses to let it create one carrying
   permissions the actor does not itself hold — unless it holds `escalate`, which is in the
   forbidden set and appears in **no** template. Likewise `RoleBinding` creation is refused without
   `bind` unless the creator holds every permission being bound. Attenuation is therefore enforced
   by the API server, by `vap-agent-scope`, and by webhook V-6 — three independent mechanisms.
2. **RBAC cannot express "not its own object".** Nothing in `agents` or `roles` grants above
   prevents an agent from patching **its own** `Agent` CR or a **parent's**. That exclusion is
   enforced by the broker (forbidden-set match, §4.2) and by `vap-agent-scope` (which denies any
   write to an `Agent` CR whose identity key equals or is an ancestor of the writer's).
3. **RBAC cannot exclude a namespace.** The cluster-admin actor's cluster-wide workload grant would
   otherwise reach `kube-system` and `kubeagents-system`. The protected-namespace carve-out
   ([03](03-security-model.md) §3.3 rule 6) is enforced by the broker and by `vap-agent-scope`,
   which denies writes into protected namespaces except a named add-on allowlist.

#### 2.2.1 Broker operations grant (all tiers, identical)

The three actor templates above cover what an agent **acts on**. They do not cover what the broker
needs to **run its own pipeline** — and without this block the system cannot satisfy invariant 3,
because the broker would have no permission to write the journal it is required to write. Every
actor identity additionally receives exactly this rule set, byte-identical across tiers:

```yaml
# Broker operations — appended verbatim to every actor Role/ClusterRole.
- apiGroups: [authentication.k8s.io] # step 1: authenticate the calling agent
  resources: [tokenreviews]
  verbs: [create]
- apiGroups: [kubeagents.x-k8s.io] # step 11: journal — the broker owns its own records
  resources: [actionrecords]
  verbs: [get, list, watch, create]
- apiGroups: [kubeagents.x-k8s.io]
  resources: [actionrecords/status]
  verbs: [get, update, patch]
- apiGroups: [kubeagents.x-k8s.io] # step 5: brake — MUST be readable by every tier
  resources: [fleetfreezes]
  verbs: [get, list, watch]
- apiGroups: [kubeagents.x-k8s.io] # step 5: its own pause state
  resources: [agents]
  verbs: [get, list, watch]
- apiGroups: [kubeagents.x-k8s.io] # steps 4 and 7: classification and approval inputs
  resources: [changepolicies, approvalrosters]
  verbs: [get, list, watch]
```

Three properties of this grant are load-bearing and are asserted separately (09 §6.14,
`V-BRK-013`):

- **`create` but never `update`/`delete` on `actionrecords`.** The broker appends to the journal and
  advances `status`; it can never rewrite or remove a record, including its own. Tampering with the
  journal stays in the forbidden set (§3.3 rule 4) for every identity without exception.
- **`fleetfreezes` is readable by _every_ tier.** A tier that cannot read the freeze object fails
  closed permanently (§4.4), so omitting this grant does not fail safe — it bricks the tier.
- **The grant is identical across tiers and is not scoped.** It confers no authority over tenant
  resources, so widening it does not widen an agent's reach; keeping it uniform means one rule set
  to review rather than three.

### 2.3 Cloud IAM mapping (Workload Identity)

One Google service account per identity, bound to the KSA by the standard
`iam.gke.io/gcp-service-account` annotation. Actor GSAs carry **IAM Conditions** pinning them to
their own scope — the cloud equivalent of the RBAC scope ceiling, and the only enforcement available
outside Kubernetes admission.

| Identity                  | GSA                                        | Roles                                                                                                                            | IAM condition                                                                                                 |
| ------------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| platform **reader**       | `kubeagents-platform-gsa`                  | `roles/viewer`, `roles/container.viewer`, `roles/monitoring.viewer`                                                              | project-scoped                                                                                                |
| platform **actor**        | `kubeagents-platform-actor-gsa`            | `roles/container.admin`, `roles/compute.networkAdmin`, `roles/monitoring.editor`, a custom role for the KCC resources it manages | `resource.project == "<project>"`                                                                             |
| cluster-admin **reader**  | `kubeagents-cluster-admin-<cluster>-gsa`   | `roles/container.viewer`, `roles/monitoring.viewer`                                                                              | `resource.name.startsWith("projects/P/locations/L/clusters/C")`                                               |
| cluster-admin **actor**   | `kubeagents-cluster-admin-<cluster>-actor` | custom role: `container.clusters.update`, `container.nodePools.*`, `container.operations.get`                                    | same `startsWith` condition — **one GSA per cluster, never one per project**                                  |
| developer-team **reader** | `kubeagents-devteam-<ns>-gsa`              | `roles/monitoring.viewer` (logs/metrics for its workloads)                                                                       | log-filter / label condition on the namespace                                                                 |
| developer-team **actor**  | **none in v1**                             | —                                                                                                                                | a namespace tier has no cloud write surface; add a narrowly-conditioned GSA only when a concrete need appears |

**Never granted to any actor GSA, at any tier:** `roles/owner`, `roles/editor`,
`roles/iam.securityAdmin`, `roles/resourcemanager.projectIamAdmin`,
`roles/iam.serviceAccountTokenCreator` on an agent GSA, or any role permitting
`iam.serviceAccounts.setIamPolicy` / `resourcemanager.projects.setIamPolicy`. These are the cloud
expression of [03](03-security-model.md) §3.3 rule 1, and their absence is a checked property, not a
convention (§10).

---

## 2a. User-authorization contract — DEFERRED hardening (down-scope to the requester)

Implements [03](03-security-model.md) §4a: a human request's effective authority becomes
**agent scope ∩ the requester's own permissions**, eliminating the confused deputy.

> **Deferred — not in v1.** v1 secures the human→agent boundary with trusted-human access
> (`allowedUsers`, checked before dispatch), the scope ceiling, and the gated class.

**What changed: the broker is now its natural host.** In the read-only generation this check had no
obvious home; the broker already (a) sits outside the LLM loop, (b) authenticates the caller, and
(c) resolves every target's scope per action — which is exactly the machinery a per-request
down-scope needs. The contract when it lands:

- the router issues a **signed requester assertion** (§4.1 `requester.assertion`) carrying the
  authenticated principal and groups; the broker verifies the signature rather than trusting the
  envelope's `requester` block;
- for each Kubernetes target the broker issues a `SubjectAccessReview` for the **requester** with
  the envelope's own verb/resource/namespace, and proceeds only on `status.allowed == true`;
- for each cloud target it calls `iam.testIamPermissions` for the requester's principal;
- a denial refuses the whole envelope (no partial application, matching §4.1's atomicity rule) and
  is journaled as `Rejected` with `reason: requester-unauthorized`;
- autonomous actions (watch / alert / cron triggers) have no requester and continue to run under
  the agent's own scope.

The broker needs `create` on `subjectaccessreviews`. That grant is **absent from every v1 template**
in §2.2 and is added only with this hardening.

---

## 2b. ChatOps addressing & routing contract

How a human names the agent they want ([02](02-agent-personas.md) §2.4). The **ChatOps gateway**
([05](05-system-architecture.md) C15) resolves every inbound message to exactly one `(tier, scope)`
`Agent` CR, checks that agent's allowlist, and dispatches. **Slack is the reference platform**;
Google Chat is fully supported and secondary, with its deltas stated explicitly (parity table
below) rather than assumed identical.

**Ingress and dispatch are independent, and this is the load-bearing separation.** The router
normalizes a Slack event (or a Chat push) into one internal `InboundMessage` and then dispatches
over the **existing per-agent transport, unchanged**. Adding, changing, or losing a chat platform
touches the normalizer and nothing else; a spoke agent is reached identically regardless of which
platform the human typed into. This is why Chat stays supported without a second dispatch path.

```yaml
# The normalized message. Produced by exactly one adapter per platform; consumed by Resolve().
InboundMessage:
  platform: slack # slack | googlechat
  principal: slack:U02ABCDEF # canonical, platform-qualified (V-11). The ONLY identity considered.
  channel: C01TEAMXOPS # Slack channel ID | Chat space name
  threadKey: slack:C01TEAMXOPS:1721840283.001900 # see "Thread affinity", below
  text: "why is checkout erroring?"
  command: kage # set only for a slash command; the verb/target grammar is parsed from `text`
  mentionsBot: true
  interaction: null # set for a Block Kit button / Chat card click (§2b.1)
  receivedAt: "2026-07-24T17:58:01Z"
```

**Handle grammar (platform-neutral, unchanged).** An agent's handle is `<tier>-<scope-leaf>`:

| Tier             | Canonical handle           | Short alias          | Resolves to `(tier, scope)` |
| ---------------- | -------------------------- | -------------------- | --------------------------- |
| `platform`       | `@platform-<project>`      | —                    | `(platform, project)`       |
| `cluster-admin`  | `@cluster-admin-<cluster>` | `@cluster-<cluster>` | `(cluster-admin, cluster)`  |
| `developer-team` | `@developer-team-<ns>`     | `@devteam-<ns>`      | `(developer-team, ns)`      |

Prefix matching is longest-first (`cluster-admin-` before `cluster-`); leaves are lower-cased and
must be RFC-1123 labels, refused rather than coerced
(`k8s-operator/internal/router/grammar.go`). The map is **derived** from the same `(tier, scope)`
key the cardinality webhook enforces (§1.2) — there is no separate routing registry to drift. A
handle is **text inside the message**, not a Slack `@`-mention of a real Slack user, so it survives
both platforms and neither platform can rename it out from under the fleet.

**Resolution order — five steps, deterministic first, inference last.** `Resolve()` returns on the
first step that yields exactly one agent; a step that yields two or more is a **clarify**, never a
pick.

| #   | Mode      | Slack (reference)                                | Google Chat (parity)                          | Inference? |
| --- | --------- | ------------------------------------------------ | --------------------------------------------- | ---------- |
| 1   | `slash`   | `/kage devteam-team-x why is checkout erroring?` | `/kage devteam-team-x …` (Chat slash command) | No         |
| 2   | `handle`  | `@kage devteam-team-x drain node-7`              | `@kage devteam-team-x drain node-7`           | No         |
| 3   | `thread`  | any reply in a thread already routed to an agent | same, keyed on the Chat thread name           | No         |
| 4   | `channel` | a bare message in bound `#team-x-ops`            | a bare message in a bound space               | No         |
| 5   | `nl`      | `@kage why is my app crashing in team-x?`        | same                                          | **Yes**    |

Only mode 5 spends an inference call, and on low confidence it **asks one question** rather than
guessing. The mode is recorded on every turn (§8) as `routingMode`, alongside the platform.

**Why `channel` sits below `thread` and above `nl`.** A bound channel is the Slack-idiomatic form of
[02](02-agent-personas.md) §2.4's per-audience entrypoint: `#team-x-ops` is already the place the
team-x humans talk about team-x, so a bare message there needs neither a handle nor a guess. It sits
_below_ thread affinity because an explicit routing decision already made in this thread is more
specific than the room it happens in, and _above_ `nl` because it is deterministic and free. A
channel binds to at most one agent (V-12), so step 4 can never be ambiguous by construction.

**Thread affinity — `threadKey`.** Normalized, platform-qualified, and stable for the life of the
thread:

```text
threadKey := "slack:" <channelId> ":" <thread_ts>          # Slack: thread_ts of the ROOT message
           | "googlechat:" <spaceId> ":" <threadName>      # Chat:  the thread resource name
```

Slack's `thread_ts` is the root message's timestamp, so a top-level message uses its own `ts` and
every reply carries the parent's — the key is identical for every turn in a thread without the
router storing anything but the mapping. Semantics are **unchanged and deliberately narrow**:
affinity names a _target_, never a _permission_. **Every turn re-authorizes**, and a **different**
user posting in an already-routed thread is checked against the target agent's `allowedUsers` from
scratch — they inherit nothing from the human who opened it. Affinity is dropped when the message
re-addresses another agent explicitly (steps 1–2 always win over step 3).

**Routing is never an authz signal.** `Resolve()` only names a target; `Authorize()` independently
reads the **target** CR's `allowedUsers` and compares the canonical principal (V-11). The gateway is
**fail-closed**: an empty, absent, or all-blank allowlist refuses everyone, and a principal that
does not parse is refused rather than compared loosely. A mis-route can therefore only ever land on
an agent the human was already allowed to reach ([03](03-security-model.md) §4a).

**Slack ↔ Google Chat parity — where they genuinely differ.**

| Concern              | Slack (reference)                                                               | Google Chat (secondary)                                                     |
| -------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Transport            | **Socket Mode** — outbound WSS from the router; no public ingress               | HTTPS **Pub/Sub push** to the router; OIDC audience verified per message    |
| Connection limit     | **One** Socket Mode connection per app token ⇒ one active router replica (§1.1) | None; the subscription fans out normally                                    |
| Command registration | **Static, per app** — one `/kage` command for the whole fleet (§2b.1)           | Static, per app — one `/kage`; Chat also matches the bot mention            |
| Interactivity        | **Block Kit** buttons for approve/reject/undo, delivered over the same socket   | Card `onclick` actions; same payload contract, same re-verification (§2b.1) |
| Threading            | `thread_ts` (root message timestamp)                                            | thread resource name under the space                                        |
| Principal format     | `slack:U02ABCDEF` (member ID; `W…` on Enterprise Grid)                          | `googlechat:users/1234567890`                                               |
| Binding unit         | channel ID (`channelBindings`)                                                  | space name (`spaceBindings`)                                                |
| Enabled by default   | **Yes** — generated CRs ship `slack.enabled: true`                              | No — opt-in per agent **and** fleet-wide in `ChatOpsConfig`                 |

Both platforms may be enabled at once; they are independent ingresses onto the same dispatch path,
and an agent reachable on both is reachable under **two** allowlists that are checked separately.

### 2b.1 Operational commands (new — the imperative model)

These are **control-plane commands, not agent conversation**. They are executed by the gateway and
the controller against Kubernetes objects and **must work with the LLM, the agent pod, and the
inference stack all unavailable** ([03](03-security-model.md) §6). The gateway never forwards them
to the agent for interpretation.

**One command, a verb/target grammar.** Slack registers slash commands **statically, per app**
(§1.1), so `/<handle> pause` — a command per agent — is unregisterable for a fleet that grows by
one command per namespace. The grammar therefore puts the variable part in the **arguments**, where
it costs nothing:

```text
/kage <handle> <intent…>          address an agent in natural language
/kage <verb> [args…]              a control-plane command (table below)
@kage <handle> <intent…>          bot mention; the handle is the FIRST token after the mention
<bare message in a bound channel> address that channel's agent (§2b step 4)
```

`<handle>` is the §2b grammar (`devteam-team-x`, `cluster-bravo`, `platform-my-project`) — written
without a leading `@` after `/kage`, and with or without one after `@kage`; both parse. The first
token is a **verb** if it is in the closed set below, otherwise it is a handle; a token that is
neither is refused with the two lists, never inferred. Google Chat registers the identical single
`/kage` command, so the grammar is one parser (`internal/router/grammar.go`) for both platforms.

| Command                               | Effect                                                                                                                                                                    | Object touched                                      | Authorized by                                                                |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------- |
| `/kage pause <handle> [reason]`       | Broker refuses new envelopes immediately; in-flight action completes or rolls back                                                                                        | `Agent.spec.operations.{paused,pauseReason}`        | the target agent's `allowedUsers`                                            |
| `/kage resume <handle>`               | Clears the brake. Does **not** clear `contested` markers or a `FleetFreeze`                                                                                               | `Agent.spec.operations.paused`                      | the target agent's **approval roster** (stricter than pause, deliberately)   |
| `/kage freeze <scope> [reason] [ttl]` | Nothing executes anywhere in scope. Undo and rollback still work                                                                                                          | creates a `FleetFreeze` (§4.4)                      | approval roster of the agent owning the scope, or its parent's roster        |
| `/kage thaw <freeze-name>`            | Deletes the `FleetFreeze`                                                                                                                                                 | `FleetFreeze`                                       | the roster that created it, or a parent's roster                             |
| `/kage undo <action-id> [reason]`     | Replays the recorded undo plan as a new, classified, journaled action                                                                                                     | creates an `UndoRequest` (§4.4)                     | the owning agent's `allowedUsers`                                            |
| `/kage approve <action-id> [note]`    | Releases a `PendingApproval` action to execute                                                                                                                            | `ActionRecord.status` via the approvals subresource | **approval roster only**; never the requester of the action (§4.4 four-eyes) |
| `/kage reject <action-id> [note]`     | Terminates a `PendingApproval` action as `Rejected`                                                                                                                       | `ActionRecord.status`                               | approval roster only                                                         |
| `/kage uncontest <action-id>`         | Clears a `contested` marker so the agent may act on that target again (§4.4)                                                                                              | `ActionRecord.status.contested` + target annotation | **approval roster only** — never the agent, and never cleared by `resume`    |
| `/kage status [handle]`               | Renders `Agent.status` — paused/frozen, budget, pending approvals, last action, counters. With no handle: the agent bound to this channel or thread, else a fleet summary | read-only                                           | the target agent's `allowedUsers`                                            |
| `/kage actions [--since] [--class]`   | Lists recent `ActionRecord`s for the scope with their undo handles                                                                                                        | read-only                                           | the target agent's `allowedUsers`                                            |
| `/kage help`                          | Prints this grammar and the handles the caller may reach                                                                                                                  | read-only                                           | anyone in the workspace; lists **only** agents whose allowlist admits them   |

`freeze` and `thaw` take a scope or freeze name rather than a handle because they are fleet
controls, not agent controls; `/kage freeze` with no argument freezes the scope of the agent bound
to the current channel and **says which scope it froze** before doing it.

**Block Kit approvals — a convenience, never an authorization.** When an action parks as
`PendingApproval`, the router posts an approval message to the roster's `notify.slack.channel`
(§4.4) with the classification, blast radius, undo plan summary, and two Block Kit buttons:

```json
{
  "type": "button",
  "action_id": "kage_approve",
  "style": "primary",
  "text": { "type": "plain_text", "text": "Approve" },
  "value": "{\"actionId\":\"01J8Z3A1B2C3D4E5F6G7H8J9K0\",\"agent\":\"developer-team/my-project/cluster-a/team-x\"}"
}
```

A click arrives over the same Socket Mode connection as an `interaction` on the normalized message
(§2b). **What the payload proves is that a Slack user clicked — nothing more.** It is not a
credential, it is not an approval, and it carries no authority the same human typing
`/kage approve <id>` would not have. On receipt the broker **re-runs the approval pipeline from the
top** ([04](04-workflow-model.md) §3.1 step 4): it resolves the clicker's canonical principal from
the verified Slack payload (never from the button `value`), re-checks it against the target's
`ApprovalRoster`, re-applies `allowSelfApproval` and `minApprovals`, re-checks the TTL, and
**re-classifies the action against current cluster state** — refusing if the class rose or the undo
plan's `preconditions.uid` no longer matches. The `actionId` in `value` is a _lookup key_ that the
broker independently validates against the record it renders; a tampered or replayed payload
resolves to an action the clicker may not approve and is refused, journaled, and reported in-thread.
Buttons are idempotent: a second click on an already-decided action re-renders the outcome.

`/kage approve <action-id>` is the **fallback and the contract**; the button is sugar over it. Chat
parity: the identical payload is delivered as a card `onclick` action and takes the identical path.
When `ChatOpsConfig.spec.slack.interactivity.blockKit: false`, approvals are typed-command only and
nothing else changes.

**Two-tier authorization, stated plainly.** `allowedUsers` gates _talking to an agent and stopping
it_; the **approval roster** gates _letting it proceed_ and _relaxing a stop_. Anyone trusted enough
to use an agent is trusted enough to hit its brake — braking is always the safe direction. Releasing
the brake, approving a gated action, or thawing a freeze requires roster membership. `pause` and
`undo` are therefore deliberately the **most** widely available commands in the system.

**The brake never depends on Slack.** Every command in the table above has an equivalent
`kubectl` / API path, and Slack is the **most likely** thing to be unavailable at the moment someone
needs the brake — a workspace outage, a revoked app token, a dropped Socket Mode connection, or a
router pod that is itself the incident. `pause`, `freeze`, `thaw`, `undo`, `approve`, `reject`,
`uncontest`, and `status` are therefore Kubernetes-object operations first and chat commands second;
the router is a **convenience front end that holds no state the objects do not**. A build in which
any brake control is reachable only through chat has the dependency backwards.

```bash
# pause — Slack down, agent still stoppable
kubectl patch agent developer-team-team-x -n team-x --type=merge \
  -p '{"spec":{"operations":{"paused":true,"pauseReason":"suspect rollout loop"}}}'

kubectl apply -f - <<'EOF'
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata: { name: incident-4471 }
spec:
  scope: { projectId: my-project }        # everything in the project
  reason: "INC-4471 — payments degraded"
  requestedBy: slack:U02ABCDEF            # canonical principal (V-11), even off-platform
  expiresAt: "2026-07-24T22:00:00Z"
EOF

kubectl kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6 --reason "wrong diagnosis"
kubectl kage approve 01J8Z3A1B2C3D4E5F6G7H8J9K0 --note "INC-4471 change window"
kubectl kage status --agent developer-team-team-x
```

`kubectl kage` is a thin plugin over the same API surface the router calls
(`POST /v1alpha1/actions/{actionId}/approve`, the approvals subresource, `UndoRequest`,
`FleetFreeze`), authorized by the caller's **Kubernetes** identity mapped to a canonical principal —
so roster membership, four-eyes, and TTL are enforced by the same code whichever door was used.

**Attribution (extends §8).** Every chat turn's audit record carries the resolved agent
(`tier`, `scope`), the **platform** (`slack` | `googlechat`), the **routing mode**
(`slash` | `handle` | `thread` | `channel` | `nl`), the canonical requester, the `threadKey`, the
trace/session IDs, and — for the commands above — the **object mutated**, whether the input was
typed or a Block Kit interaction, and, for `approve`, the `action-id` released.

---

## 3. Journal & IaC-mirror repository layout

**The repository is no longer a control path.** In the read-only generation the customer's GitOps
repo was the mutation mechanism: an agent pushed a branch, a human merged, CI applied. The Action
Broker replaces all three steps ([03](03-security-model.md) §4). What remains is a **write-behind
mirror** — valuable, optional, and deliberately powerless: **compromising the repo cannot cause a
cluster change** ([03](03-security-model.md) §2, [04](04-workflow-model.md) §6).

**What is retired** (delete these paths and their machinery, do not repurpose them):

| Retired                                                 | Why                                                     | Replaced by                             |
| ------------------------------------------------------- | ------------------------------------------------------- | --------------------------------------- |
| `submit-suggestion` skill and the propose branch naming | Agents execute; they do not propose                     | §4.1 Action Envelope                    |
| `.github/workflows/apply.yml` as the **applier**        | The broker applies, synchronously, in-cluster           | §4 broker pipeline                      |
| Branch protection + CODEOWNERS as the **approval gate** | Approval is a risk-class decision, not a merge decision | §4.2 classifier + §4.4 `ApprovalRoster` |
| `knowledge/escalation/` as the cross-tier message bus   | Agents call each other directly                         | §7 agent mesh                           |

**What survives, and what it is now for:**

```text
<gitops-repo>/
├── clusters/<cluster>/            # MIRROR of executed desired state — for IaC continuity, not apply
│   ├── provisioning/              # KCC YAML or Terraform HCL, written back after execution
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs + per-agent reader/actor identity manifests
├── fleet/                         # project-level policy; platform-tier Agent CR + identities
├── knowledge/                     # OKF base (§5) — unchanged, still not applied to any cluster
├── policy/                        # vap-agent-scope, vap-agent-pod-hardening, ChangePolicies,
│                                  #   rbac-overlay/<tier>.yaml templates (§2)
├── journal/                       # EXPORTED action log (new)
│   └── <YYYY>/<MM>/<DD>.ndjson    # one ActionRecord summary per line, append-only
└── .github/workflows/             # optional: drift-detect + policy CI. NOT an applier.
```

**Bootstrap remains a human path.** `policy/` and the initial `agents/` + identity manifests are
applied by a human or the install pipeline **before** any agent exists — an agent cannot bootstrap
its own authority. After bootstrap, the parent-provisions-child flow (§4.2) creates children
in-cluster and the mirror records them.

### 3.1 Mirror contract

Configured per agent by `spec.integration.github.mirror` (§1.1). Executed by the broker **after**
step 11 of the pipeline — the `ActionRecord` is durable first, always.

| Field         | Type                       | Default            | Meaning                                                                                                                               |
| ------------- | -------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`     | bool                       | `false`            | Mirroring off by default; the journal is the system of record                                                                         |
| `mode`        | `state` \| `log` \| `both` | `both`             | `state` writes desired-state files; `log` appends `journal/…ndjson`                                                                   |
| `branch`      | string                     | `main`             | Target branch. Commits are **direct** — no PR, because there is no review to perform                                                  |
| `paths`       | []string                   | derived from scope | Restricts what this agent may mirror; defaults to its own scope's subtree                                                             |
| `batchWindow` | duration                   | `5m`               | **`log` mode only.** Coalesce journal appends into one commit to avoid a commit per pod restart. Ignored for `state` mode — see below |

**`state` commits are synchronous; `log` commits may batch.** This distinction is load-bearing, not
an optimisation detail. [04](04-workflow-model.md) §6 rests the `mirror`-mode race mitigation on the
commit being "part of the action rather than a later batch" — a five-minute coalescing window would
widen exactly the window in which a GitOps engine reverts the agent. So:

- **`state`** — the desired-state write is performed **within the action**, immediately after step
  11, before the action is reported complete. `batchWindow` does not apply. Its latency is measured
  and bounded (09 §12, `V-PRO-014`).
- **`log`** — the journal append is an audit record with no reconciliation semantics, so coalescing
  it is safe and `batchWindow` applies.
- **`both`** — the state write is synchronous; the log append batches.

A mirror failure never blocks, delays, or reverts the action itself; it is retried and surfaced.

**Commit shape.** Conventional Commit, subject `chore(mirror): <intent>`, with trailers:

```text
chore(mirror): scale api-gateway to 6 replicas

kube-agents-action-id: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
kube-agents-agent: developer-team/my-project/cluster-a/team-x
kube-agents-risk-class: elevated
kube-agents-requester: slack:U02ABCDEF
kube-agents-trace-id: 4bf92f3577b34da6a3ce929d0e0e4736
[skip ci]
```

**Two hazards to design against, called out because both have bitten GitOps mirrors before:**

1. **The mirror must not trigger an applier.** If the customer still runs Argo/Flux/Actions against
   these paths, a mirror commit re-applies what the broker just applied — at best a no-op, at worst
   a fight. Mitigations, in order of preference: mirror to a dedicated branch; or have the broker
   set the same field manager (`kube-agents/<tier>/<scope>`) so server-side apply is idempotent; or
   `[skip ci]` as shown. Where a GitOps engine is authoritative for a path, the broker treats a
   drift it did not cause as a **`contested` target** (§4.4) rather than re-fixing it.
2. **Credentials.** Mirror pushes use a **Minty-brokered short-lived GitHub token**, scoped to the
   one repo, held by the **broker** — never by the agent pod. A mirror failure is logged and
   retried; it **never** blocks, reverts, or delays an action, because the mirror is not the record.

**Format.** `spec.iac.format` (`kcc` | `terraform`, default `kcc`) selects the artifact written under
`provisioning/`. Kubernetes objects are always mirrored as YAML manifests, normalized identically to
the undo-plan sanitizer (§4.3) so a mirror diff shows intent, not server noise.

---

## 4. Action contracts

This is the centre of the system. Everything else in this document exists to serve the sequence
below: an agent composes an **Action Envelope**, its **broker** classifies and executes it, and an
**`ActionRecord`** carrying an **undo plan** is the durable result.

```text
agent pod (reader SA, LLM)                broker pod (actor SA, deterministic code)
        │                                          │
        │  POST /v1alpha1/actions   ─────────────► │ 1  authenticate caller (mTLS + TokenReview)
        │      Action Envelope (§4.1)              │ 2  validate schema · freshness · nonce · key
        │                                          │ 3  resolve scope from the CALLER, not the body
        │                                          │ 4  classify risk (§4.2)
        │                                          │ 5  check the brake (§4.4)
        │                                          │ 6  generate the undo plan (§4.3)
        │                                          │ 7  gate if required → PendingApproval
        │                                          │ 8  snapshot pre-state
        │                                          │ 9  execute (server-side apply, actor identity)
        │                                          │ 10 verify, roll back on failure
        │  ◄─────────────  ActionResponse          │ 11 journal the ActionRecord  ← before reporting
```

Steps 1, 3, 4, 5, 6 and 11 are not skippable by any caller ([03](03-security-model.md) §4.1).

### 4.1 Action Envelope

The request an agent submits to **its own** broker. Not a CRD — a request body, `POST`ed as JSON to
`https://<agent-name>-broker.<namespace>.svc:8443/v1alpha1/actions` over mTLS. A NetworkPolicy
admits **only** the agent pod of the same `Agent` CR to that endpoint.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionEnvelope

# ---- what and why -----------------------------------------------------------------------------
intent: "restart crash-looping deployment api-gateway (OOMKilled x7 in 10m) and raise its memory limit"
rationale:
  | # optional, ≤4096 chars — model reasoning, recorded, NEVER a classification input
  Container `api-gateway` exceeded its 256Mi limit under the 17:40 traffic step.
  Prior 24h p99 RSS is 340Mi. Raising to 512Mi and restarting.

# ---- the operations (applied atomically: all targets in scope, or the envelope is rejected) ----
operations:
  - op: patch # create | apply | patch | delete | scale
    target:
      group: apps
      version: v1
      kind: Deployment
      namespace: team-x
      name: api-gateway
    patch:
      type: application/merge-patch+json
      body:
        spec:
          template:
            spec:
              containers:
                - name: api-gateway
                  resources: { limits: { memory: 512Mi } }
  - op: patch
    target:
      {
        group: apps,
        version: v1,
        kind: Deployment,
        namespace: team-x,
        name: api-gateway,
      }
    patch:
      type: application/merge-patch+json
      body:
        spec:
          template:
            metadata:
              annotations: { kube-agents/restarted-at: "2026-07-24T17:58:02Z" }

# ---- provenance -------------------------------------------------------------------------------
requester:
  kind: human # human | agent | system
  id: slack:U02ABCDEF # canonical principal (§1.2 V-11), agent identity key, or "" for system
  platform: slack # slack | googlechat | kubectl | mesh | ""
  displayName: "A. Parco"
  assertion: "" # router-signed JWT. Empty ⇒ ActionRecord marks attributionUnverified (§2a)
trigger:
  source: watch # chat | watch | alert | cron | delegation | escalation | undo
  ref: "pod/api-gateway-7d9c-4kk2" # the object/alert/thread that caused this
  detail: "CrashLoopBackOff, 7 restarts in 10m"
trace:
  traceId: 4bf92f3577b34da6a3ce929d0e0e4736 # W3C trace-id, 32 hex
  spanId: 00f067aa0ba902b7
  sessionId: hermes-9f21c4 # Hermes session
  threadId: slack:C01TEAMXOPS:1721840283.001900 # normalized threadKey (§2b), for the reply

# ---- freshness & anti-replay (see "Anti-replay", below) ----------------------------------------
issuedAt: "2026-07-24T17:58:01Z" # RFC-3339, UTC, from the agent pod. Freshness window applies
nonce: 8f14e45fceea167a5a36dedd4bea2543 # broker-issued, single-use, 128-bit, TTL 120s

# ---- execution controls (stricter-only) --------------------------------------------------------
idempotencyKey: sha256:9f2b…c41a # caller-computed AND broker-recomputed; must match (see below)
dryRun: false # true ⇒ classify, plan, verify-plan, journal as DryRun; never execute
requireApproval: false # true ⇒ force this action to `gated` even if it classifies lower
maxObjects: 5 # caller's own cap; effective cap = min(this, budget, ChangePolicy, code)
deadlineSeconds: 120 # broker aborts and rolls back past this
```

**Field reference.**

| Field                         | Type                                                     | Req | Default      | Notes                                                                                                  |
| ----------------------------- | -------------------------------------------------------- | --- | ------------ | ------------------------------------------------------------------------------------------------------ |
| `intent`                      | string, 1–512                                            | ✓   | —            | Human-readable, imperative, one line. Rendered in chat, the digest, and `ActionRecord`                 |
| `rationale`                   | string, ≤4096                                            |     | `""`         | Recorded for review. **Never** read by the classifier                                                  |
| `operations[]`                | array, 1–50                                              | ✓   | —            | Applied in order, atomically w.r.t. scope/classification (see atomicity, below)                        |
| `operations[].op`             | enum                                                     | ✓   | —            | `create` \| `apply` \| `patch` \| `delete` \| `scale`                                                  |
| `operations[].target`         | object                                                   | (b) | —            | `{group, version, kind, namespace, name}`. `group: ""` for core. Cloud variant below                   |
| `operations[].targetSelector` | `{group, version, kind, namespace, labelSelector}`       | (b) | —            | Fan-out form. **Expanded against live state at step 3**, before classification (§4.2 blast radius)     |
| `operations[].desiredState`   | object                                                   | (a) | —            | Full object for `create`/`apply`. Mutually exclusive with `patch`                                      |
| `operations[].patch`          | `{type, body}`                                           | (a) | —            | `type` ∈ `application/merge-patch+json`, `application/json-patch+json`, `application/apply-patch+yaml` |
| `operations[].delete`         | `{propagationPolicy, gracePeriodSeconds, preconditions}` |     | `Foreground` | Only with `op: delete`                                                                                 |
| `operations[].scale`          | `{replicas}`                                             | (a) | —            | Only with `op: scale`                                                                                  |
| `requester`                   | object                                                   | ✓   | —            | Attribution. **Not** an authorization input in v1 (§2a)                                                |
| `trigger`                     | object                                                   | ✓   | —            | `source` is a closed enum; drives autonomy metrics ([01](01-vision-scope.md) §7)                       |
| `trace`                       | object                                                   | ✓   | —            | `traceId` required; the chain in §8 depends on it                                                      |
| `issuedAt`                    | RFC-3339 UTC                                             | ✓   | —            | Freshness. Outside the acceptance window ⇒ `403 envelope-expired`                                      |
| `nonce`                       | string, 32 hex                                           | ✓   | —            | Single-use, broker-issued. Reuse ⇒ `403 replayed-envelope`                                             |
| `idempotencyKey`              | string, ≤128                                             | ✓   | —            | `sha256:<hex>`. Broker recomputes and compares — see below                                             |
| `dryRun`                      | bool                                                     |     | `false`      | Forced `true` when `spec.operations.dryRunOnly` is set                                                 |
| `requireApproval`             | bool                                                     |     | `false`      | Stricter-only: `true` raises to `gated`; `false` never lowers anything                                 |
| `maxObjects`                  | int                                                      |     | `1`          | Guards fan-out for selector-shaped operations                                                          |
| `deadlineSeconds`             | int, 1–900                                               |     | `120`        | Clamped to the code ceiling                                                                            |

(a) exactly one of `desiredState` / `patch` / `scale` per operation, matching its `op`.
(b) exactly one of `target` / `targetSelector` / `cloudTarget` per operation. `targetSelector` is
refused for `op: create` (there is nothing to select) and never crosses a namespace boundary.

**Cloud target variant.** For non-Kubernetes resources, `target` takes the cloud shape; everything
else in the envelope is identical:

```yaml
- op: apply
  cloudTarget:
    provider: gcp
    service: container.googleapis.com
    resource: projects/my-project/locations/us-central1/clusters/cluster-a/nodePools/default
    method: setSize
  desiredState: { nodeCount: 6 }
```

**Idempotency — the key, computed exactly.** "A digest over the normalized desired state" is not a
specification: two callers that normalize differently produce different keys, dedupe silently stops
working, and the failure is invisible because nothing errors. The key is therefore defined
byte-precisely, and the **broker recomputes it and compares**:

```text
idempotencyKey = "sha256:" + lowerhex( SHA-256( JCS( K ) ) )

K = {
  "agentIdentity": "<tier>/<projectId>[/<clusterName>[/<namespace>]]",   // §1.2 identity key
  "dryRun":        <bool>,
  "operations": [                    // SORTED, see below
    { "op":      "<create|apply|patch|delete|scale>",
      "target":  {"group","version","kind","namespace","name"},          // cloudTarget variant: the
                                                                          //   {provider,service,resource,method} object
      "payload": <sanitized desiredState | patch.body | scale | delete options> }
  ]
}
```

| Step               | Rule                                                                                                                                                                                                                                                     |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Canonical JSON** | **RFC 8785 (JCS)** — UTF-8, lexicographic key ordering by UTF-16 code unit, no insignificant whitespace, RFC-8785 number serialization. Not "`json.Marshal` with sorted keys", which differs on numbers and escapes                                      |
| **Normalization**  | `payload` is passed through the **§4.3.1 sanitizer** — the same function, not a parallel one. Its `DROP` list removes `resourceVersion`, `uid`, `managedFields`, `creationTimestamp`, `status`, and the reassigned `spec.clusterIP*` / `nodePort` fields |
| **Secrets**        | The sanitizer's `REDACT` rule applies, so a `Secret` payload is hashed as per-key digests. **The key never embeds Secret material**, and two writes of the same value dedupe correctly anyway                                                            |
| **Sort order**     | `operations` sorted by the string `op + "\x1f" + group + "/" + version + "/" + kind + "/" + namespace + "/" + name`, byte-wise ascending. Ties (a genuinely repeated operation) keep envelope order and are **not** de-duplicated within the envelope    |
| **Excluded**       | `intent`, `rationale`, `requester`, `trigger`, `trace`, `nonce`, `issuedAt`, `requireApproval`, `maxObjects`, `deadlineSeconds`. Prose and provenance must not change the key, or retries with a reworded intent would double-execute                    |
| **Verification**   | The broker recomputes `K` from the accepted envelope. A mismatch is `400 idempotency-key-mismatch`, journaled `Rejected` — a caller **cannot** choose its own key                                                                                        |

The broker keeps computed keys for **24 h**. A repeat within the window returns the **original**
`ActionRecord` reference with `decision: deduplicated` and executes nothing — this is what makes an
agent retry after a timeout safe, and it is also the first line of defence against a flapping loop.

**Anti-replay.** Dedupe alone does not stop a **replay**: a captured envelope whose credentials are
still valid re-executes, and an attacker who flips one byte of `idempotencyKey` walks straight past
the dedupe cache ([03](03-security-model.md) §8, [08](08-agent-runtime-and-identity.md) §4). Three
independent mechanisms close it, checked at pipeline **step 2**, before classification:

| #     | Mechanism                                                 | Rule                                                                                                                                                                                                                                                                                          | Rejection               |
| ----- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| **1** | **Freshness window** on `issuedAt`                        | Accepted only when `now - 120s ≤ issuedAt ≤ now + 30s`. The asymmetry is deliberate: 120 s covers a slow inference turn plus retry, 30 s covers clock skew only. This bounds the replay window to ≤150 s even if 2 and 3 both failed                                                          | `403 envelope-expired`  |
| **2** | **Single-use broker-issued `nonce`**                      | The agent calls `GET /v1alpha1/nonce` (same mTLS listener) and receives `{nonce, expiresAt}` — 128 bits from a CSPRNG, TTL **120 s**, at most **32** outstanding per caller. The broker marks it consumed on receipt, before any other processing, and keeps consumed nonces for the full TTL | `403 replayed-envelope` |
| **3** | **`(agentIdentity, traceId, idempotencyKey)` uniqueness** | This tuple may execute **once**, ever, within the 24 h key window. A genuine retry necessarily carries a **fresh nonce and a fresh `spanId`** but may reuse the tuple, and gets `decision: deduplicated`; a **re-POST of the identical bytes** is caught by 2 first                           | `403 replayed-envelope` |

Mechanism 3 is what makes the "flip a byte of the key" bypass useless: the key is recomputed from
content, so mutating it produces `400 idempotency-key-mismatch`, and leaving it alone produces a
dedupe hit. There is no third option.

**Nonce state is broker-local and survives restart the way the brake does — by failing closed.** The
consumed-nonce set lives in memory with a periodic checkpoint; a broker that has just restarted and
cannot prove a nonce is unused **refuses envelopes issued before its start time** rather than
accepting them, for the same reason an unreadable `FleetFreeze` freezes the scope (§4.4). The cost
is one retry after a broker restart; the alternative is a replay window across every restart.

Every rejection above is journaled as an `ActionRecord` in status `Rejected` with
`reason: replayed-envelope` / `envelope-expired` / `idempotency-key-mismatch`, **plus a security
event** — a replay attempt must leave evidence, not a silent 403.

**Atomicity.** Scope resolution, classification, and the brake check apply to the envelope **as a
whole**: one out-of-scope or forbidden target rejects the entire envelope, with nothing applied
([03](03-security-model.md) §4.1 step 3). Execution itself is best-effort sequential: if operation
_k_ fails, the broker rolls back operations _1..k-1_ using the already-generated undo plan and
records `Failed` or `RolledBack`. An envelope should therefore group operations that belong to one
logical change (as above: limit + restart) and **not** batch unrelated work.

#### What the broker ignores — and what it refuses

This is the security-load-bearing half of the schema.

| The envelope claims…                                                    | Broker behaviour                                                                                                                             |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| a `tier`, `scope`, `namespace` **authority**, or `actor`                | **Refused.** These are reserved top-level keys. Scope is derived from the authenticated caller's SA ([03](03-security-model.md) §4.1 step 1) |
| a `riskClass`, `class`, `severity`, or `approved: true`                 | **Refused.** Classification is computed, never asserted                                                                                      |
| a `bypass`, `force`, `skipJournal`, `skipVerify`, `emergency`           | **Refused**, and emits a security event — these names exist only to be rejected loudly                                                       |
| an `undoPlan`                                                           | **Refused.** The broker generates the plan; a caller-supplied one is an undo-poisoning vector                                                |
| a reused `nonce`, a stale `issuedAt`, or a self-chosen `idempotencyKey` | **Refused** at step 2, before classification, with a security event — see "Anti-replay" below                                                |
| any other unknown field                                                 | **Refused** (`400 unknown field`). The schema is closed; nothing is silently dropped                                                         |
| `rationale` arguing an action is safe / urgent / approved               | **Recorded and ignored.** Model output is never a risk signal ([03](03-security-model.md) §8)                                                |
| `requester.id` (unsigned)                                               | **Recorded, not trusted.** Without a valid `assertion` the record carries `attributionUnverified: true`                                      |

"Refused" means HTTP `400`/`403`, no execution, and — for the reserved-key and `bypass` families —
an `ActionRecord` in status `Rejected` plus a security event, so an injected agent trying to talk
its way past the broker leaves evidence rather than a gap.

**Response.**

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionResponse
actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6 # ULID; also the ActionRecord name suffix
actionRecordRef: { name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6, namespace: team-x }
status: Verified # §4.3 lifecycle value
riskClass: elevated
decision: executed # executed | pending-approval | rejected | deduplicated | dry-run
undoAvailable: true
undoCommand: "kubectl kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6"
verification:
  { passed: true, checks: 2, detail: "rollout complete; 0 restarts in 5m" }
message: "raised memory limit to 512Mi and restarted api-gateway"
retryAfterSeconds: 0 # non-zero when paused/frozen/budget-exhausted
```

### 4.2 Risk classification & `ChangePolicy`

The classifier is **deterministic code in the broker**, evaluated on every envelope before
execution ([03](03-security-model.md) §5). Its inputs are the envelope's targets and the live
cluster state — never `intent`, never `rationale`, never anything a model wrote in prose.

**Output contract.**

```yaml
classification:
  class: gated # routine | elevated | gated | forbidden
  reasons: # ordered, every rule that fired — this is the explanation shown to humans
    - {
        rule: destructive-stateful-delete,
        class: gated,
        detail: "PersistentVolumeClaim team-x/pg-data",
      }
    - {
        rule: production-environment,
        class: "+1",
        detail: "namespace label kube-agents/environment=production",
      }
  blastRadius: { objects: 1, fractionOfScope: 0.02, cap: 25 }
  undoable: false
  undoReason: "delete of a bound PVC is not reconstructable: PV data is not snapshotted"
  policySources: [code-floor, changepolicy/baseline-conservative]
```

**Evaluation order** (short-circuit at the first two):

1. **Scope** — every target inside the caller's derived scope? Any miss ⇒ `forbidden`, stop.
2. **Forbidden set** ([03](03-security-model.md) §3.3) ⇒ `forbidden`, stop.
3. Compute a class from each remaining input; the result is the **maximum** over all of them.
4. Apply the `+1` escalations (environment, novelty), capped at `gated`.
5. Apply every matching `ChangePolicy`, taking the **maximum** again (stricter-only, by construction).
6. If no valid undo plan can be generated (§4.3) ⇒ **raise to at least `gated`**.

**Rule table shape.** The code floor is a list of rules of exactly this form; a `ChangePolicy`
contributes additional rules in the same form, and nothing else can.

```yaml
- id: destructive-stateful-delete
  when:
    verbs: [delete]
    kinds:
      - { group: "", kind: PersistentVolumeClaim }
      - { group: "", kind: PersistentVolume }
      - { group: "", kind: Namespace }
      - { group: apps, kind: StatefulSet }
      - { group: container.cnrm.cloud.google.com, kind: ContainerCluster }
      - { group: container.cnrm.cloud.google.com, kind: ContainerNodePool }
  class: gated
  reason: "deletes data or capacity that cannot be reconstructed from a manifest"
```

| Field                                        | Type                                              | Meaning                                                                         |
| -------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------- |
| `id`                                         | string, unique                                    | Appears in `classification.reasons[].rule` and in the audit event               |
| `when.verbs`                                 | []enum                                            | Envelope `op` values this rule matches. Empty = any                             |
| `when.kinds`                                 | []`{group, kind}`                                 | Target kinds. Empty = any                                                       |
| `when.excludeKinds`                          | []`{group, kind}`                                 | Target kinds this rule never matches. Applied after `when.kinds`                |
| `when.ownedByLowerTier`                      | bool                                              | Code floor only. True ⇒ matches when a lower tier's agent owns the target       |
| `when.namespaces` / `when.namespaceSelector` | []string / labelSelector                          | Target namespaces                                                               |
| `when.labelSelector`                         | labelSelector                                     | Matched against the **live** target object, not the desired state               |
| `when.fieldPaths`                            | []string (**dotted path**, see below)             | Fires when the change touches these paths (e.g. `spec.type`, `spec.ingress`)    |
| `when.direction`                             | `loosen` \| `tighten` \| `any`                    | Security direction; `loosen` is what gates                                      |
| `class`                                      | `routine`\|`elevated`\|`gated`\|`forbidden`\|`+1` | The class this rule contributes                                                 |
| `maxObjects`                                 | int                                               | Blast-radius cap; exceeding it raises to `gated`, exceeding the hard cap aborts |
| `reason`                                     | string                                            | Shown verbatim to the human                                                     |

**Two path dialects, and which is which.** This document uses both, and a reader who assumes one
will write a rule that never fires:

| Where                                   | Dialect                                                                                                                                     | Example                                                    |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `when.fieldPaths` (rule authoring)      | **Dotted relaxed JSONPath** — the Kubernetes `kubectl`/`client-go` dialect, no leading `$`, `[i]` for list indices, `[*]` for "any element" | `spec.template.spec.containers[*].image`                   |
| `status.applied[].diff[].path` (output) | **RFC 6901 JSON Pointer** — because the diff is an RFC 6902 JSON Patch and Patch mandates Pointer                                           | `/spec/template/spec/containers/0/resources/limits/memory` |

They are never interchangeable and neither is ever accepted where the other is expected: a
`fieldPaths` entry beginning with `/` is rejected at `ChangePolicy` admission with
`Invalid: expected a dotted field path, not a JSON Pointer`.

**Escaping, one example each.** The annotation key `kube-agents/restarted-at` contains a `/`, which
is a segment separator in JSON Pointer and an ordinary character in a dotted path:

```text
dotted path (fieldPaths):  metadata.annotations['kube-agents/restarted-at']
                           ^ a segment containing '.' or '/' MUST be bracket-quoted
JSON Pointer (diff.path):  /metadata/annotations/kube-agents~1restarted-at
                           ^ '/' escapes to ~1 and '~' escapes to ~0 (RFC 6901 §3)
```

The broker converts internally: it computes the diff as JSON Patch, then evaluates `fieldPaths` by
translating each dotted path to the equivalent Pointer prefix set and testing for **prefix
containment**, so `spec.template` matches a change at
`/spec/template/spec/containers/0/image`. Matching is on the **touched** path set — the union of
`path` and `from` across the diff — so a `remove` counts as touching the path it removed.

**The code floor, abridged** — the rules that must exist, mapping 1:1 onto
[03](03-security-model.md) §5.2:

| `id`                          | Fires on                                                                                             | Class       |
| ----------------------------- | ---------------------------------------------------------------------------------------------------- | ----------- |
| `out-of-scope`                | any target outside the caller's derived scope                                                        | `forbidden` |
| `forbidden-set`               | agent RBAC/IAM, escalation verbs, control plane, journal, protected namespaces                       | `forbidden` |
| `no-undo-plan`                | broker cannot generate a validated undo plan                                                         | `gated`     |
| `destructive-stateful-delete` | delete of PVC/PV/Namespace/StatefulSet/cluster/node pool/bucket/disk/snapshot/backup                 | `gated`     |
| `security-loosen`             | delete or weaken NetworkPolicy/PSA label/policy; widen an RBAC or IAM grant to a non-agent principal | `gated`     |
| `public-exposure`             | Service→`LoadBalancer`/`NodePort`, Ingress/Gateway added, `0.0.0.0/0` in an allow rule               | `gated`     |
| `traffic-shift-production`    | Service/Ingress/Gateway/HTTPRoute change on a production-labelled target                             | `gated`     |
| `identity-change`             | any write to ServiceAccount, Secret of type `*token*`, IAM binding                                   | `gated`     |
| `blast-radius-cap`            | `objects > min(50, maxObjects)`, counted **after** selector expansion                                | `gated`     |
| `blast-radius-hard-cap`       | `objects > 100` or `fractionOfScope > 0.5`                                                           | abort       |
| `secret-write`                | create/update of a `Secret`                                                                          | `elevated`  |
| `secret-material-egress`      | a **non-Secret** payload carries a value equal to live `Secret` material in the caller's scope       | `gated`     |
| `cross-tier-direct-operation` | direct operation on a resource owned by a **lower tier that has its own agent**                      | `gated`     |
| `production-environment`      | target namespace/object carries `kube-agents/environment: production` (or the `env` alias)           | `+1`        |
| `novel-action`                | first occurrence of `(op, kind)` for this agent in the trust-building window                         | `+1`        |
| `object-override`             | `kube-agents/change-policy: gated\|forbidden` annotation on the object or its namespace              | as stated   |
| _default_                     | anything else, in scope, reversible                                                                  | `routine`   |

**The production label — one canonical key, one documented alias.** The classifier looks for
**`kube-agents/environment: production`** on the target object, and failing that on its namespace.
`env=production` is accepted **only as a documented alias**, for the very common case of an existing
estate that already labels this way. Precedence is exact and is not a merge:

```text
1. object    kube-agents/environment    ← canonical; wins outright
2. object    env                        ← alias, consulted only if 1 is absent
3. namespace kube-agents/environment    ← canonical
4. namespace env                        ← alias, consulted only if 3 is absent
⇒ first match wins; no match ⇒ not production.
```

The canonical key wins **even when it disagrees** with the alias: an object carrying
`kube-agents/environment: staging` **and** `env=production` is **not** production, because the
canonical key is present and says so. The reverse — `kube-agents/environment: production` plus
`env=dev` — **is** production. Silence on the canonical key is the only condition under which the
alias is read at all. Values are compared case-insensitively after trimming; `prod` is **not** an
accepted value of either key, because accepting near-misses makes the negative test unwritable.
New estates should set only the canonical key; the alias is compatibility, not a second contract.

**Blast radius, in numbers.** Three limits, at three different layers, and they are not the same
number:

| Limit                              | Value                                 | Layer                   | Effect                                                                   |
| ---------------------------------- | ------------------------------------- | ----------------------- | ------------------------------------------------------------------------ |
| Literal operations in one envelope | **≤ 50**                              | schema (§4.1)           | `400` at validation — the envelope is malformed, nothing is classified   |
| Objects touched, per action        | **> 50 ⇒ `gated`**                    | `blast-radius-cap`      | Human approval; `maxObjects` / `ChangePolicy` may lower it, never raise  |
| Objects touched, per action        | **> 100**, or `fractionOfScope > 0.5` | `blast-radius-hard-cap` | **Abort.** No approval path — this action does not exist in a gated form |

The gate at 50 and the hard abort at 100 look redundant against a 50-operation envelope ceiling, and
would be, if operations were always literal. They are not: an operation may carry a
`targetSelector` (a label selector in place of `name`), and **fan-out is counted after expansion
against live state** at step 3, before classification. So the 100-object hard cap is reachable
**only** via selector fan-out — which is exactly the shape of the accident it exists to stop (one
mistyped selector matching an entire namespace), and never via an agent laboriously enumerating 101
targets it could not have fitted in the envelope anyway. Expansion is evaluated once, and the
resolved object list is what gets snapshotted, executed, and journaled in `spec.targets`; a selector
whose match set changes between expansion and execution fails the `preconditions.uid` check on the
affected object and rolls the envelope back.

**`fractionOfScope`, denominator named.** The denominator is the count of **workload objects in the
agent's scope** — not "everything the agent can see", which would include Events and Endpoints and
make the fraction meaningless.

| Tier             | Denominator = live objects in scope, of kinds…                                                                                                                                                                                                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `developer-team` | In its namespace: `apps/{Deployment,StatefulSet,DaemonSet}`, `batch/{Job,CronJob}`, `v1/{Service,ConfigMap,Secret,PersistentVolumeClaim,ServiceAccount}`, `autoscaling/HorizontalPodAutoscaler`, `policy/PodDisruptionBudget`, `networking.k8s.io/{Ingress,NetworkPolicy}`, `gateway.networking.k8s.io/{HTTPRoute,GRPCRoute}` |
| `cluster-admin`  | The same kind set across **every non-protected namespace** in its cluster, plus `v1/Namespace` and `v1/Node`                                                                                                                                                                                                                  |
| `platform`       | `container.cnrm…/{ContainerCluster,ContainerNodePool}` plus every other KCC object in the project. Kubernetes workloads are **not** counted — the platform tier does not own them (see `cross-tier-direct-operation`)                                                                                                         |

**Excluded from the denominator, deliberately:** `Pod`, `ReplicaSet`, `ControllerRevision`,
`EndpointSlice`, `Event`, and anything with an `ownerReference` to a counted object — they are
controller output, they churn, and counting them would let a scope inflate its own denominator by
scaling up. The count comes from the broker's reader informer cache, is at most **60 s** stale, and
is floored at **20**: in a scope with fewer than 20 workload objects the denominator is 20, so a
two-object namespace does not report `fractionOfScope: 1.0` for a routine two-object patch. If the
count is unavailable, `fractionOfScope` is recorded as `null` and only the absolute caps apply —
the broker does not guess a denominator.

**`secret-material-egress`, and why it is not an entropy rule.** [03](03-security-model.md) §8 lists
"copying Secret material into a non-Secret object" as a gated exfiltration path, but `secret-write`
covers writes **to** a Secret, not copies **out of** one — so as written the rule protects the
container and not the contents. The missing rule:

```yaml
- id: secret-material-egress
  when:
    verbs: [create, apply, patch]
    excludeKinds: [{ group: "", kind: Secret }] # writing a Secret INTO a Secret is `secret-write`
  class: gated
  reason: "the payload contains material that matches a live Secret in this scope"
```

**Comparison method — a match against live Secret values, never a heuristic.** At classification
the broker builds a set of digests of every value in every `Secret` **readable in the caller's
scope**: for each key, `sha256(secretNamespace || 0x1f || value)` and `sha256(value)`, plus the
base64 and URL-encoded forms of the value. It then walks every string leaf of the outgoing payload
(and, for a string longer than 64 bytes, every whitespace/quote/comma-delimited token within it) and
digests each candidate the same way. Any intersection fires the rule. Minimum candidate length is
**8 bytes** — shorter values collide with ordinary config and would gate every `replicas: 3`.

The rule is deliberately **not** an entropy test. "High-entropy string in a ConfigMap" fires on image
digests, ULIDs, git SHAs, JWT-shaped feature flags, and base64 TLS bundles — every one of them a
routine config change — so an entropy rule would gate approximately all config work within a day and
be switched off within two. A value-match rule has the opposite failure mode: it can only fire when
the material genuinely left a Secret, so a false positive is a real (if benign) copy. Digests are
computed in-broker, held in memory, never journaled and never logged; `classification.reasons[]`
names the **source Secret and key** and never the value.

Two limits stated honestly, because the rule's value depends on knowing them: it detects **verbatim
and simply-encoded** copies, not material that was transformed (encrypted, hashed, split, or
re-encoded in a form not listed above); and the digest set costs one full `Secret` list per
classification, cached for 60 s per scope, which is why the rule is scoped to the caller's own
namespace / cluster rather than the fleet.

**`cross-tier-direct-operation` — making a stated prohibition into a rule.**
[03](03-security-model.md) §3.2 says the Platform Agent "may not operate tenant workloads directly —
delegate to the tier that owns them". Today that sentence appears in no actor template and no
forbidden-set entry, so it is an aspiration: RBAC cannot express it (the platform actor legitimately
holds broad grants for provisioning), and nothing else was asked to. It becomes a classification
rule:

```yaml
- id: cross-tier-direct-operation
  when:
    verbs: [create, apply, patch, delete, scale]
    ownedByLowerTier: true # computed, see below
  class: gated
  reason: "this object is owned by a lower tier that has its own agent — delegate instead (§7)"
```

**Ownership is computed, not declared.** A target is _owned by a lower tier_ when a **non-terminating
`Agent` CR exists whose scope strictly contains the target and is strictly contained by the
caller's** — the same subset predicate V-6 uses (§1.2), reused so the two cannot drift. Concretely:

| Caller          | Target                                                 | Owner exists?                                                           | Outcome                                                         |
| --------------- | ------------------------------------------------------ | ----------------------------------------------------------------------- | --------------------------------------------------------------- |
| `platform`      | `apps/v1 Deployment team-x/api` in `cluster-a`         | `developer-team` agent for `team-x` ✓                                   | **`gated`** — delegate down the chain instead                   |
| `platform`      | same Deployment, in a namespace with **no** agent      | ✗                                                                       | Rule does not fire; classified normally                         |
| `cluster-admin` | `apps/v1 Deployment team-x/api`, `team-x` has an agent | ✓                                                                       | **`gated`** — same rule, all tiers, not a platform special case |
| `cluster-admin` | `v1/ResourceQuota team-x/compute`                      | agent exists, but quota is **not** in the child's actor template (§2.2) | Rule does not fire — the child cannot own what it cannot write  |
| `platform`      | `container.cnrm…/ContainerNodePool` in `cluster-a`     | cluster-admin agent exists **and** holds `containernodepools`           | **`gated`**                                                     |

Two refinements that keep the rule from being merely annoying:

- **Delegation is the routine path, and stays routine.** A parent that delegates (§7) causes the
  **child** to compose its own envelope in its own scope; the child's action classifies on its own
  merits and is typically `routine`. The rule therefore does not make cross-tier work expensive —
  it makes the _direct_ form expensive and the _delegated_ form free, which is the intended
  gradient.
- **It gates; it does not forbid.** A child that is paused, frozen, `Failed`, or absent is a real
  operational state, and a parent reaching past a broken child during an incident is legitimate —
  with a human saying so. That is precisely what `gated` means, and it is why this is a
  classification rule rather than a forbidden-set entry. The reason string names the child agent so
  the approver can see whose scope is being entered, and the resulting `ActionRecord` carries the
  label `kube-agents/cross-tier: <child-identity>` so the pattern is queryable ("how often does the
  platform tier reach into namespaces?" is an answerable question, and a rising answer is a signal).

**`ChangePolicy`** — cluster-scoped, stricter-only by construction:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ChangePolicy
metadata:
  name: baseline-conservative
spec:
  # Which agents this applies to. Omitted ⇒ every agent. A policy set by a parent's operator binds
  # its children too; agents cannot select themselves out.
  agentSelector:
    tiers: [developer-team]
    scopes: [{ projectId: my-project, clusterName: cluster-a }]
  rules: # same shape as the code-floor rule table above
    - id: gate-all-deletes-while-ramping
      when: { verbs: [delete] }
      class: gated
      reason: "trust-building period: all deletes are reviewed"
    - id: tighten-fanout
      when: {}
      maxObjects: 10
      reason: "cap blast radius below the code ceiling"
status:
  agentsMatched: 4
  conditions: []
```

**Why loosening is unrepresentable rather than merely forbidden.** There is no `allow`, no
`maxClass`, no `exempt`, no `class: routine` **downgrade** path: `class` on a `ChangePolicy` rule is
validated to be ≥ the class the code floor would assign for the same match, and the broker takes the
**maximum** of all sources regardless. A policy that tried to lower a class would be rejected at
admission; even if it were somehow admitted it would have no effect. The forbidden set is a code
constant and is not addressable by `ChangePolicy` at all.

**`ChangePolicy` objects are control-plane objects** ([03](03-security-model.md) §3.3 rule 3): no
actor template in §2.2 grants write on `changepolicies`, and `vap-agent-scope` denies it
independently. A human tightens policy; an agent cannot touch it in either direction.

#### Worked examples

**(1) Restart a crash-looping Deployment — `routine`.**
Envelope: `patch apps/v1 Deployment team-x/api-gateway`, adding a restart annotation.
Scope: `team-x` ⊆ developer-team scope `team-x` ✓. Forbidden set: no match. Undo plan: `restore` the
prior Deployment object (sanitized snapshot) — generated and validated ✓. Destructiveness: none.
Direction: neither. Blast radius: 1 object of ~50 → 0.02, cap 25 ✓. Environment: `team-x` carries no
production label. Novelty: this agent has patched Deployments 31 times this week.
⇒ **`routine`.** Executes immediately; appears in the periodic digest, no ping.

**(2) Scale a production Deployment 3 → 10 — `elevated`.**
Envelope: `scale apps/v1 Deployment payments-prod/checkout replicas: 10`.
Scope ✓, forbidden ✗, undo plan = `restore replicas: 3` ✓, destructive ✗, direction neither, blast
radius 1 ✓. Base class from the default rule: `routine`. Then `production-environment` fires (`+1`)
because the namespace carries `kube-agents/environment: production` (§4.2 precedence).
⇒ **`elevated`.** Executes immediately, pings the owning humans at once with the undo handle, and
gets the 90-day retention. Note what did **not** happen: no human blocked a reversible capacity fix
during a traffic spike — which is the entire point of the class existing between `routine` and
`gated`.

**(3) Delete a bound PVC to reclaim quota — `gated`.**
Envelope: `delete v1 PersistentVolumeClaim team-x/pg-data`.
Scope ✓ (in namespace). Forbidden set ✗. Undo-plan generation **fails**: recreating the PVC yields a
new volume, not the data — `no-undo-plan` fires (`gated`). Independently
`destructive-stateful-delete` fires (`gated`). Blast radius 1.
⇒ **`gated`, for two independent reasons.** Parks as `PendingApproval`, notifies
`team-x-approvers`, expires after the roster's TTL, executes nothing meanwhile. A chat message
insisting the volume is empty and the deletion urgent changes nothing — the classifier never reads
it ([03](03-security-model.md) §8.1).

**(4, contrast) Bind the agent's own reader SA to `cluster-admin` — `forbidden`.**
Rule `forbidden-set` fires at step 2 (RBAC naming an agent identity). Rejected outright, security
event emitted, no approval path offered anywhere; repeated attempts trip the SLI and auto-pause the
agent ([03](03-security-model.md) §6).

### 4.3 `ActionRecord`

The durable journal entry — one per envelope, created **before** the action is reported complete
([03](03-security-model.md) §4.1 step 11). An `ActionRecord` is a namespaced CRD
(`kubeagents.x-k8s.io/v1alpha1`), created in the agent's namespace, so `kubectl get actionrecords`
works, admission can protect it, and the undo controller can watch it. It is mirrored to the
durable log sink ([05](05-system-architecture.md)) and, optionally, exported to `journal/` (§3).

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionRecord
metadata:
  name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6 # "ar-" + lower-cased ULID
  namespace: team-x
  labels:
    kube-agents/tier: developer-team
    kube-agents/scope: team-x
    kube-agents/risk-class: elevated
    kube-agents/status: Verified
    kube-agents/trigger: watch
    kube-agents/chain-id: 01j8z2k9q7v3x5m6n8p0r2t4w0 # delegation chain — see "chainId", below
    kube-agents/undo-of: 01j8z1a0b1c2d3e4f5g6h7j8k9 # present ONLY on an undo record (§4.3)
spec: # IMMUTABLE after creation (enforced by CEL + vap-agent-scope)
  actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
  agentRef: { name: developer-team-team-x, namespace: team-x }
  agentIdentity: developer-team/my-project/cluster-a/team-x # the (tier, scope) key
  actorServiceAccount: developer-team-team-x-actor # who actually wrote
  requester:
    {
      kind: human,
      id: slack:U02ABCDEF,
      platform: slack,
      displayName: "A. Parco",
    }
  attributionUnverified: false # true when no signed requester assertion was present
  trigger:
    source: watch # chat | watch | alert | cron | delegation | escalation | undo
    ref: pod/api-gateway-7d9c-4kk2
    detail: "CrashLoopBackOff x7/10m"
    undoOf: "" # REQUIRED and non-empty iff source == undo (see "Undo linkage", below)
    chainId: 01J8Z2K9Q7V3X5M6N8P0R2T4W0 # from MeshRequest.chain; own actionId when chain-originating
  trace:
    {
      traceId: 4bf92f3577b34da6a3ce929d0e0e4736,
      spanId: 00f067aa0ba902b7,
      sessionId: hermes-9f21c4,
    }
  intent: "raise api-gateway memory limit to 512Mi and restart"
  rationale: "…" # recorded, never a classification input
  idempotencyKey: sha256:9f2b…c41a
  dryRun: false

  classification: # verbatim output of §4.2
    class: elevated
    reasons:
      - {
          rule: production-environment,
          class: "+1",
          detail: "namespace label kube-agents/environment=production",
        }
    blastRadius: { objects: 1, fractionOfScope: 0.02, cap: 25 }
    undoable: true
    policySources: [code-floor, changepolicy/baseline-conservative]

  targets:
    - {
        group: apps,
        version: v1,
        kind: Deployment,
        namespace: team-x,
        name: api-gateway,
        uid: 2f1c…,
        resourceVersion: "81422",
      }

  preState: # snapshot of every target, taken inside the broker at step 8
    - targetIndex: 0
      capturedAt: "2026-07-24T17:58:02Z"
      object: {
          apiVersion: apps/v1,
          kind: Deployment,
          metadata: { … },
          spec: { … },
        } # sanitized
      # object OR objectRef — see "large snapshots" below
      objectRef: null
      sha256: 3d1a…

  undo: # generated at step 6, BEFORE execution (§4.3.1)
    strategy: restore
    generatedAt: "2026-07-24T17:58:02Z"
    validated: true
    steps:
      - op: apply
        target:
          {
            group: apps,
            version: v1,
            kind: Deployment,
            namespace: team-x,
            name: api-gateway,
          }
        object: { … } # the sanitized preState object
        preconditions: { uid: 2f1c… } # refuse the undo if the object was replaced meanwhile
    caveats:
      - "restores spec only; pods created since will be replaced by the rollout"

  retention: # TWO independent clocks — see "Retention vs the undo window", below
    class: elevated
    ttl: 2160h # 90d — how long the RECORD lives
    expiresAt: "2026-10-22T17:58:02Z" # record deletion time
    undoWindow: 720h # 30d — how long UNDO IS PROMISED
    undoWindowExpiresAt: "2026-08-23T17:58:02Z" # always ≤ expiresAt

status:
  phase: Verified
  observedGeneration: 1
  applied:
    - targetIndex: 0
      diff: # normalized JSON-patch of what actually changed on the server
        - {
            op: replace,
            path: /spec/template/spec/containers/0/resources/limits/memory,
            from: 256Mi,
            value: 512Mi,
          }
        - {
            op: add,
            path: /spec/template/metadata/annotations/kube-agents~1restarted-at,
            value: "2026-07-24T17:58:02Z",
          }
      resourceVersionAfter: "81430"
  verification:
    passed: true
    completedAt: "2026-07-24T18:03:11Z"
    checks:
      - {
          name: rollout-complete,
          passed: true,
          detail: "1/1 updated replicas available",
        }
      - {
          name: no-restarts-5m,
          passed: true,
          detail: "0 container restarts since apply",
        }
  recovery: # the recovery ladder (04 §5), recorded so it is observable
    rung: 1 # 1 retry · 2 alternative · 3 rollback · 4 escalate · 5 page
    transitions: # append-only; a skipped rung MUST carry a reason
      - { at: "2026-07-24T18:02:55Z", from: 0, to: 1, reason: conflict-retry }
  report: # the four beats (02 §2.5.4) as STRUCTURED fields, not prose
    noticed: "checkout OOMKilled every ~40s against a 256Mi limit"
    did: "raised limits.memory to 512Mi (elevated)"
    verified: "3/3 pods Ready, restart count flat for 6m"
    undo: "kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6"
  approvals: # present only for gated actions
    required: 1
    granted: []
    rejected: []
    expiresAt: null
  contested: false # set true when a human undoes or manually reverts this change (§4.4)
  undoneBy: "" # actionId of the undo action, once executed
  timestamps:
    submitted: "2026-07-24T17:58:01Z"
    classified: "2026-07-24T17:58:01Z"
    approved: null
    executionStarted: "2026-07-24T17:58:02Z"
    executionEnded: "2026-07-24T17:58:04Z"
    verified: "2026-07-24T18:03:11Z"
  message: "raised memory limit to 512Mi and restarted api-gateway"
```

**`status.report` is structured, and the chat text is rendered from it — never the reverse.** The
four beats of [02](02-agent-personas.md) §2.5.4 are fields, not prose the harness has to parse back
out of a chat message. This is what makes the character and honesty requirements _mechanically_
checkable rather than a matter for an LLM judge: a report claiming a fix can be compared directly
against `status` and `verification.passed`, and a missing beat is a schema failure. An
implementation that emits chat prose and derives the fields afterwards is non-conforming, because
the two can then disagree.

**`status.recovery` makes the ladder observable.** [04](04-workflow-model.md) §5 requires that the
agent never skips a rung silently and never restarts at the bottom for the same target after a
rollback. Neither is checkable unless the rung is recorded, so it is: `transitions` is append-only,
non-decreasing in `rung`, and any skip carries a `reason`.

**Status lifecycle.**

```text
                          ┌──────────► Rejected            (forbidden, out of scope, brake, refused schema)
                          │
Pending ──► PendingApproval ──► Executing ──► Verified ──► Undone
   │              │  │              │              │
   │              │  └► Expired     └► Failed ──► RolledBack
   │              └► Rejected                (verification failed; broker restored pre-state)
   └────────────────────────────► Executing        (routine / elevated: no gate)

DryRun is a terminal state reached from Pending when dryRun=true.
```

| Phase             | Meaning                                                                                | Terminal |
| ----------------- | -------------------------------------------------------------------------------------- | -------- |
| `Pending`         | Accepted, classified, undo plan generated; not yet executing                           |          |
| `PendingApproval` | `gated`; awaiting the roster. Nothing has been written                                 |          |
| `Executing`       | Snapshot taken; server-side apply in progress                                          |          |
| `Verified`        | Executed **and** the intended outcome confirmed                                        | ✓        |
| `Failed`          | Execution errored; partial work rolled back where possible                             | ✓        |
| `RolledBack`      | Executed, verification failed, pre-state automatically restored                        | ✓        |
| `Undone`          | A human ran `undo`; the plan replayed successfully                                     | ✓        |
| `Rejected`        | Refused before execution (forbidden / out of scope / braked / rejected by an approver) | ✓        |
| `Expired`         | `gated` action whose approval TTL elapsed                                              | ✓        |
| `DryRun`          | Classified, planned, journaled; deliberately not executed                              | ✓        |

**Retention vs the undo window — two clocks, not one.** These are different promises and they are
routinely conflated: **retention** is how long the `ActionRecord` object exists; the **guaranteed
undo window** is how long `kubectl kage undo` is promised to work. The undo window is the shorter of
the two on purpose — the snapshot a `restore` replays goes stale (the object drifts, its
`ownerReferences` change, the workload it belongs to is redeployed), so a 90-day-old routine undo is
a plausible-looking action that quietly restores the wrong world. Keeping the record while
withdrawing the promise is the honest arrangement. Both are fields on the record, per class:

| Class                  | `retention.ttl` (record lives) | `retention.undoWindow` (undo promised) | Rationale                                                                  |
| ---------------------- | ------------------------------ | -------------------------------------- | -------------------------------------------------------------------------- |
| `routine`              | **30 d**                       | **7 d**                                | Enough to notice and undo; keeps etcd small                                |
| `elevated`             | **90 d**                       | **30 d**                               | Consequential changes stay undoable across a month, readable for a quarter |
| `gated`                | **365 d**                      | **90 d**                               | Approval evidence; also the audit-review horizon                           |
| `Rejected` (forbidden) | **365 d**                      | n/a (nothing executed)                 | Security evidence — never short-lived                                      |

| Field                                | Type     | Set by              | Rule                                                                                                                                                                                      |
| ------------------------------------ | -------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `spec.retention.class`               | enum     | broker, at creation | The final risk class. Immutable with the rest of `spec`                                                                                                                                   |
| `spec.retention.ttl`                 | duration | broker, at creation | From the table. A `ChangePolicy` may **lengthen** it (stricter-only in the audit direction), never shorten it                                                                             |
| `spec.retention.expiresAt`           | RFC-3339 | broker, at creation | `timestamps.submitted + ttl`. The cleanup controller deletes on this, and **only after** the exporter confirms the record landed in the audit sink ([05](05-system-architecture.md) §1.2) |
| `spec.retention.undoWindow`          | duration | broker, at creation | From the table. Must satisfy `undoWindow ≤ ttl`, validated by CEL                                                                                                                         |
| `spec.retention.undoWindowExpiresAt` | RFC-3339 | broker, at creation | `timestamps.executionEnded + undoWindow` (falls back to `submitted` for records that never executed)                                                                                      |

**What the undo window actually gates.** The undo controller (`C-UC`,
[05](05-system-architecture.md) §1.3 step 2) refuses an `UndoRequest` past
`undoWindowExpiresAt` with `status.phase: Refused` and
`message: undo window expired at <ts>; recover from the audit sink`. This is a **refusal, not an
error**: the record is still there, the plan is still readable, and a human may reconstruct the
change by hand — but the system stops claiming one command will do it correctly. The window is
**not** extendable by a CR field; extending a promise the snapshot cannot keep is the failure mode
the split exists to prevent.

**Large snapshots.** A `preState.object` above **1 MiB** (or an envelope whose total snapshot
exceeds 1 MiB) is written to the journal store instead and referenced by
`preState[].objectRef: {store, key, sha256}`; the CR keeps the digest only. The broker verifies the
digest on undo and refuses to replay a snapshot that does not match. **If the snapshot cannot be
persisted, the action does not execute** — fail-closed, same rule as journaling
([03](03-security-model.md) §6).

**Immutability, and exactly who may write `status`.** `spec` is immutable after creation (CEL
`self == oldSelf` on every field). `status` is **not** freely writable, and "writable only by the
broker and the undo controller" is too loose to conformance-test — the check needs a principal list
and a field list. This is that list; `vap-agent-scope-journal` enforces it on
`actionrecords/status`, and any principal or field pair not in the table is **denied**:

| Principal                                                                      | Subresource            | May write                                                                                                                                             | Constraint                                                                                                                             |
| ------------------------------------------------------------------------------ | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `system:serviceaccount:<ns>:<tier>-<scope>-actor` — **the owning broker**      | `actionrecords/status` | `phase`, `observedGeneration`, `applied`, `verification`, `recovery`, `report`, `timestamps`, `message`                                               | Only on records whose `spec.agentIdentity` equals the broker's own derived identity. **Never** `approvals`, `contested`, or `undoneBy` |
| `system:serviceaccount:kubeagents-system:kube-agents-undo-controller` (`C-UC`) | `actionrecords/status` | `phase` (→ `Undone` only), `undoneBy`, `contested`, `message`                                                                                         | Any record in any namespace — undo must work for an agent that no longer exists ([05](05-system-architecture.md) §1.3)                 |
| `system:serviceaccount:kubeagents-system:kube-agents-chatops-gateway`          | `actionrecords/status` | `approvals` (`granted`, `rejected`, `expiresAt`), `phase` (`PendingApproval` → `Pending`/`Rejected`), `contested` (clear only, for `/kage uncontest`) | Enforces the roster, four-eyes, and `minApprovals` before writing (§4.4). Cannot touch `applied` or `verification`                     |
| `system:serviceaccount:kubeagents-system:kube-agents-retention-controller`     | (main resource)        | nothing — `delete` only                                                                                                                               | May `delete` only when `now > spec.retention.expiresAt` **and** the exporter has confirmed the record landed in the audit sink         |
| **Every agent reader SA**                                                      | —                      | nothing                                                                                                                                               | `get`/`list`/`watch` only (§2.1)                                                                                                       |
| **A human `cluster-admin`**                                                    | —                      | **nothing.** Explicitly denied                                                                                                                        | See below                                                                                                                              |

**A human cluster-admin may not write `ActionRecord.status`, and this is deliberate.** The
`vap-agent-scope-journal` policy matches on **all** principals, not just agent identities, so a
`kubectl patch actionrecord … --subresource=status` by a human is rejected the same way an agent's
is. Approving, rejecting, and un-contesting are done through `/kage approve`, `/kage reject`, and
`/kage uncontest` (§2b.1) or by creating the equivalent objects — paths that check the roster, record the human, and
leave an audit trail — never by hand-editing the outcome of an action. Without this the four-eyes
rule is decorative: any cluster-admin could mark their own gated action `granted` and execute it.

Two honest limits: a principal holding `delete` on `validatingadmissionpolicybindings` can remove
the policy, so this is a **tamper-evident** control, not a tamper-proof one — removal is itself an
audited cluster-scoped write and is alarmed (SLI 3, [01](01-vision-scope.md) §7); and the
`kube-agents-retention-controller` SA can delete records, which is why its deletion predicate is
narrow, why deletion is only permitted post-export, and why the exported journal (§3) — not the CR
— is the system of record.

`vap-agent-scope` separately denies `delete` and `update` on the **main** `actionrecords` resource
to every agent identity ([03](03-security-model.md) §3.3 rule 4), including the actor SA that
created the record — the append-only property asserted in §2.2.1.

**Undo linkage is bidirectional, and both directions are required.** An undo action produces its
**own** `ActionRecord` (§4.3.1: "undo is itself an action"), which leaves two objects that must find
each other. One link is not enough, because the two queries are different:

| Direction                 | Field                              | On                  | Answers                                                                                                                   |
| ------------------------- | ---------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| forward (undo → original) | `spec.trigger.undoOf: <action-id>` | the **undo** record | "what did this undo action revert?" — and it is in `spec`, so it is **immutable** and survives the original's deletion    |
| reverse (original → undo) | `status.undoneBy: <action-id>`     | the **original**    | "was this action ever undone?" — the question a human asks before re-attempting a fix, and the one `contested` depends on |

Both are **mandatory**, not alternatives: `spec.trigger.undoOf` is required and non-empty exactly
when `spec.trigger.source == undo` (CEL, validated at admission), and `C-UC` sets
`status.undoneBy` on the original in the same reconcile that moves it to `Undone`. Either alone
leaves a query that has to scan the whole journal, and the reverse link alone is also **mutable
status on an object the undo may outlive** — the original can reach its retention TTL while the undo
record persists, and after that only `undoOf` still names the relationship. The `kube-agents/undo-of`
label mirrors `spec.trigger.undoOf` so `kubectl get actionrecords -l kube-agents/undo-of=<id>`
resolves without a field-selector index.

**`chainId` makes a delegation a single object graph.** A fleet-wide rollout is one intent that
fans out into a `MeshRequest` per tier and an `ActionRecord` per agent
([05](05-system-architecture.md) §1.4). Without a shared key, reconstructing it means walking
`chain.visited` lists across N brokers. So:

| Rule                | Detail                                                                                                                                                                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Format              | ULID, uppercase in `spec`, **lower-cased in the label** (label values are case-sensitive and RFC-1123-constrained)                                                                                                                                                                                                   |
| Origination         | The agent that starts a chain sets `chainId = its own actionId`, so the chain is named after the action that caused it and needs no allocator                                                                                                                                                                        |
| Propagation         | Carried in `MeshRequest.chain.chainId` (§7) and copied verbatim by every callee into `spec.trigger.chainId` of **every** `ActionRecord` it produces for that request                                                                                                                                                 |
| Label               | `kube-agents/chain-id: <lowercased>` on every record in the chain, including the originator's. `kubectl get actionrecords -A -l kube-agents/chain-id=<id>` is the whole rollout                                                                                                                                      |
| Non-chained actions | Set `chainId` to their own `actionId`. Every record has one; there is no empty case to handle                                                                                                                                                                                                                        |
| Trust               | Like `from`, it is **not** an authority input — a forged `chainId` mislabels a query result and grants nothing. The `traceId` (§8) remains the correlation key for **latency**; `chainId` is the key for **causation**, and they are deliberately separate: a retried mesh call gets a new trace and keeps its chain |

#### 4.3.1 The undo plan

Generated at step 6, **before** execution, and validated by dry-running each step against the API
server. If generation or validation fails, the action is raised to `gated` (§4.2 `no-undo-plan`).

| Original op                    | `strategy` | Steps                                                                               | Fidelity                                                                                 |
| ------------------------------ | ---------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `create`                       | `delete`   | `delete` the created object, with `preconditions.uid` = the UID the create returned | **Exact.** Removing something that did not exist restores the prior state precisely      |
| `apply` (object existed)       | `restore`  | `apply` the sanitized `preState` object with `preconditions.uid`                    | **Exact for spec/metadata.** Server-defaulted and controller-owned fields reconverge     |
| `apply` (object did not exist) | `delete`   | as `create`                                                                         | Exact                                                                                    |
| `patch`                        | `restore`  | `apply` the sanitized `preState` object                                             | Exact for spec/metadata                                                                  |
| `scale`                        | `restore`  | `scale` back to the recorded `replicas`                                             | Exact for the field; pod identities are not preserved (and for a Deployment need not be) |
| `delete`                       | `recreate` | `create` from the sanitized snapshot                                                | **Structural only — see below.** This is why most deletes gate                           |
| cloud `apply`/`setSize`        | `inverse`  | the provider's inverse call with the recorded prior value                           | Exact where the provider exposes a true inverse; otherwise `none`                        |
| anything else                  | `none`     | —                                                                                   | ⇒ `gated`                                                                                |

**Sanitizer** (applied to every snapshot before it becomes an undo step — this normalization is what
makes `restore` idempotent and mirror diffs readable):

```text
DROP   metadata.{uid, resourceVersion, generation, creationTimestamp, managedFields,
                 deletionTimestamp, deletionGracePeriodSeconds, selfLink}
DROP   metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]
DROP   status                       (unless the target IS a status subresource)
DROP   spec.clusterIP, spec.clusterIPs, spec.healthCheckNodePort, .nodePort   (immutable, reassigned)
KEEP   metadata.{name, namespace, labels, annotations, ownerReferences, finalizers}
KEEP   spec (in full), and data/stringData for ConfigMap/Secret
REDACT Secret.data values in the CR copy → replaced with sha256 digests; the restorable ciphertext
       is written to the journal store under objectRef (never to the mirror repo, never to a log)
```

**What is NOT undoable — and is therefore `gated` by definition.** The list is short, explicit, and
the same list that appears in [03](03-security-model.md) §5.2 as `destructiveness`:

- **Data.** Deleting a bound `PersistentVolumeClaim`, a `PersistentVolume`, a cloud disk, bucket,
  database, snapshot, or backup. Recreation yields a new empty volume: structurally identical,
  materially different.
- **Namespace and cluster deletion.** Cascading, non-atomic, and not reconstructable from one
  snapshot; recreating the container does not recreate its contents.
- **Node pool deletion / shrink below in-use capacity.** Local state and in-flight work are lost.
- **Identity and credential operations.** Rotating or deleting a credential, revoking a key,
  releasing a static IP or DNS name — the old value is gone even if the object comes back.
- **Anything whose effect left the API.** A `Job` that sent mail, charged a card, or called a
  webhook; a traffic shift that already served requests. The object is restorable; the effect is not.
- **Objects with a new identity on recreation.** A recreated object gets a new UID, so every
  `ownerReference`, PVC binding, and external reference pointing at the old one is dangling. The
  broker detects inbound references during plan generation and downgrades `recreate` to `none`.

**Undo is itself an action.** Replaying a plan submits a new envelope with `trigger.source: undo`,
which is classified, snapshotted, verified, and journaled like any other — so an undo whose own
effect is destructive gates in turn ([03](03-security-model.md) §6).

### 4.4 Brake contract — `pause` / `resume` / `freeze` / `undo` / `contested`

All five live in the controller and the broker, never in a skill, a prompt, or the model. All five
must work with the LLM, the router, and the inference stack unavailable
([03](03-security-model.md) §6).

| Control     | Object / field                                                     | Scope       | Who may invoke                                            | Semantics                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------ | ----------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pause`     | `Agent.spec.operations.paused: true` (+ `pauseReason`)             | one agent   | the agent's `allowedUsers`; anyone with `patch` on the CR | Broker refuses **new** envelopes immediately (`403 agent-paused`, `retryAfterSeconds`). The in-flight action completes or rolls back — never half-applied. The work queue is preserved |
| `resume`    | `Agent.spec.operations.paused: false`                              | one agent   | the agent's **approval roster**                           | Clears only the pause. `contested` markers, freezes, and budget cooldowns survive                                                                                                      |
| `freeze`    | `FleetFreeze` (cluster-scoped)                                     | scope/fleet | approval roster of the owning agent or a parent's         | Nothing executes in the frozen scope. Consulted on **every** envelope. **Fail-closed**                                                                                                 |
| `undo`      | `UndoRequest` (namespaced) → new envelope                          | one action  | the owning agent's `allowedUsers`                         | Replays the recorded plan as a first-class classified, journaled action                                                                                                                |
| `contested` | `ActionRecord.status.contested: true` + advisory target annotation | one target  | set **automatically**; cleared by the roster              | The agent must not redo that change to that target without explicit human instruction                                                                                                  |

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata: { name: incident-4471 }
spec:
  scope: # omit narrower fields to widen; {} means THE ENTIRE FLEET
    projectId: my-project
    clusterName: cluster-a # optional
    namespace: "" # optional
  reason: "INC-4471 — payments degraded, no automated changes"
  requestedBy: slack:U02ABCDEF
  expiresAt: "2026-07-24T22:00:00Z" # optional; a freeze with no expiry never self-clears
  allowUndo: true # default true — undo and rollback keep working during a freeze
  allowClasses: [] # default empty = nothing executes. May list ONLY `routine`; never `gated`
status:
  agentsFrozen: 12
  activeSince: "2026-07-24T18:41:00Z"
```

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: UndoRequest
metadata: { name: undo-01j8z2k9q7v3x5m6n8p0r2t4w6, namespace: team-x }
spec:
  actionRef: { name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6 }
  reason: "wrong diagnosis — the OOM was upstream"
  requestedBy: slack:U02ABCDEF
  markContested: true # default true: also mark the target contested
status:
  phase: Executed # Pending | Executing | Executed | Failed | Refused
  undoActionId: 01J8Z4M2P8Q0R1S2T3U4V5W6X7
  message: "restored Deployment team-x/api-gateway to resourceVersion 81422"
```

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ApprovalRoster
metadata: { name: team-x-approvers, namespace: team-x }
spec:
  approvers:
    # `platform` + `id` canonicalize to the §1.2 V-11 principal `<platform>:<id>`.
    - { platform: slack, id: "U02ABCDEF", displayName: "A. Parco" }
    - { platform: slack, id: "U07GHIJKL", displayName: "R. Ops" }
    - { platform: googlechat, id: "users/1234567890", displayName: "J. Chat" }
  minApprovals: 1 # default 1
  allowSelfApproval: false # default false — the human who requested an action may not approve it
  ttl: 24h # DEFAULT 24h (canonical; 04 §3.1 references this field). Ceiling 72h, floor 1h.
  # A gated action past its TTL becomes `Expired` and is never executed afterwards.
  notify: # where approval requests land (Block Kit buttons on Slack — §2b.1)
    slack: { channel: "C01ABCDEF" }
    googleChat: { space: "spaces/AAAA" }
  escalateTo: { name: cluster-a-approvers, namespace: kubeagents-system } # optional, on TTL
```

**`ApprovalRoster.spec.ttl` is the one approval-TTL default in the set.** It is **24 h**, and
[04](04-workflow-model.md) §3.1 references this field rather than restating a number. 24 h rather
than a few hours because a gated action is by definition irreversible or high-blast-radius, and the
approver population is a small roster that spans time zones and sleep: a 4-hour TTL expires most
overnight requests, which trains agents (and their operators) to re-submit rather than wait, and
re-submission is exactly the behaviour a gate exists to prevent. The ceiling is **72 h** — past
that the cluster state the classification was computed against is no longer the state the approver
is approving, so the broker **re-classifies at approval time** and refuses to execute if the class
rose or a target's `resourceVersion` moved in a way the undo plan's `preconditions.uid` no longer
matches. Expiry is never an approval: an `Expired` action is terminal (§4.3).

**Fail-closed rules — the whole point of the brake.**

| Condition                                                               | Broker behaviour                                                           |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Cannot read the `FleetFreeze` list (API error, cache stale beyond 30 s) | **Treat the scope as frozen.** Refuse everything except `undo`             |
| Cannot read its own `Agent` CR                                          | Treat as paused                                                            |
| Cannot reach the journal store                                          | Refuse to execute; set `status.broker.journalReachable: false`; auto-pause |
| Cannot persist a pre-state snapshot                                     | Refuse that envelope                                                       |
| Cannot generate or validate an undo plan                                | Raise to `gated` (never execute on a hope)                                 |
| Approval roster missing / empty while a `gated` action waits            | Action stays `PendingApproval` and expires; it is **never** auto-approved  |
| Initiative budget exhausted, or flap threshold breached                 | Refuse and escalate to a human ([04](04-workflow-model.md) §4.2)           |
| Target carries a `contested` marker                                     | Refuse; report; require explicit human instruction to proceed              |
| Broker cannot verify an executed action **and** cannot roll it back     | Auto-pause the agent and page ([03](03-security-model.md) §6)              |

**`contested`, precisely.** When a human runs `undo`, or the broker observes a target reverted by a
non-agent field manager within the flap window, the broker records `contested: true` on the
originating `ActionRecord` and indexes the target reference. A later envelope whose target matches a
contested entry is refused with `403 target-contested` and the originating `actionId`. The index is
authoritative because a deleted object cannot hold an annotation; where the object exists the broker
**also** stamps `kube-agents/contested: <action-id>` on it as an advisory signal for humans. A
contested marker is cleared only by an approval-roster member (`/kage uncontest <action-id>` or removing
the annotation and patching the record's status) — never by the agent, and never by `resume`.

**Agents cannot touch any of it.** `Agent` CRs, `FleetFreeze`, `ApprovalRoster`, `ChangePolicy`, and
`ActionRecord` status are control-plane objects: absent from every actor template in §2.2, and
denied to agent identities by `vap-agent-scope` independently ([03](03-security-model.md) §3.3
rule 3). An agent can be stopped; it cannot stop being stoppable.

---

## 5. OKF knowledge contract

OKF = markdown + YAML frontmatter in the repo's **`knowledge/` root**. It lives outside the mirrored
state paths, so it is never applied to a cluster. Required frontmatter field: `type`.

| `type`              | Purpose                                   | Key frontmatter                                |
| ------------------- | ----------------------------------------- | ---------------------------------------------- |
| `cluster-blueprint` | Standard cluster config baseline          | `title, tags, resource, timestamp`             |
| `tenancy-model`     | Namespace isolation standard              | `title, tags`                                  |
| `runbook`           | Operational procedure (SRE CUJ)           | `title, tags, timestamp`                       |
| `metric-definition` | Named metric/KPI definition               | `title, tags, resource`                        |
| `escalation`        | A cross-tier request **not** yet acted on | `title, tags, timestamp, resource`             |
| `observation`       | A durable finding worth sharing           | `title, tags, timestamp`, **`actionRefs: []`** |

**Two changes from the read-only generation:**

1. **Observations record actions taken, not proposals made.** An `observation` now carries
   `actionRefs: [<action-id>, …]` linking the finding to the `ActionRecord`s that resolved it. The
   durable knowledge is "this failure mode recurs and _this fix worked_", not "here is a suggestion
   someone should apply".
2. **`escalation` is no longer a message bus.** Cross-tier requests go over the agent mesh (§7),
   synchronously. The `escalation` type survives only for requests a human must resolve — a budget
   approval, a vendor ticket, a decision outside every agent's scope.

Agents **read** OKF for context and **write** curated updates through the mirror (§3), attributed to
the `ActionRecord` that produced them. Layout mirrors OKF: `knowledge/{index.md, <type>/…}`;
markdown links form the graph; optional `log.md` for history. OKF holds durable knowledge only —
**not** session state.

**`type` is a closed set that is extended by declaration — not an "open convention".** "Open
convention, not a hard enum" makes the validator unfalsifiable: every value passes, so the required
frontmatter keys per type cannot be checked either, and the check degenerates to "the file has a
`type` field". The rule instead:

> A `knowledge/` file is **valid** iff its `type` is (a) one of the **six canonical types** above,
> **or** (b) declared in the `typeRegistry` of `knowledge/index.md` — **and**, either way, every
> frontmatter key that type requires is present and non-empty.

```yaml
# knowledge/index.md — frontmatter
---
title: Knowledge index
type: index # reserved; index.md is the only file that may carry it
typeRegistry: # locally declared types. Empty/absent ⇒ only the six canonical types are valid.
  - type: incident-postmortem
    requiredKeys: [title, tags, timestamp, actionRefs]
    description: "One incident, its diagnosis, and the actions that resolved it"
  - type: cost-baseline
    requiredKeys: [title, tags, resource, timestamp]
    description: "Expected spend for a workload, used to judge a cost anomaly"
---
```

| Rule              | Detail                                                                                                                                                                                    |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Registry location | `knowledge/index.md` frontmatter only. One registry per repo — a per-directory registry would let a subtree legalize its own types                                                        |
| `type` syntax     | RFC-1123 label (lower-case, `[a-z0-9-]`, ≤63 chars). It is also the directory name under `knowledge/`                                                                                     |
| Required keys     | `title` and `tags` are required for **every** type, canonical or declared, and need not be repeated in `requiredKeys`                                                                     |
| Redefinition      | A registry entry naming a canonical type is rejected — the six are fixed, and their required keys are the table above                                                                     |
| Who may extend it | `index.md` is a `knowledge/` file, so an agent may propose a registry entry through the mirror (§3) like any other knowledge write. It is a repo commit a human reviews, not a live grant |
| Failure mode      | An unregistered `type`, or a missing required key, fails the OKF lint (§10) — the file is a **lint failure**, not silently-ignored knowledge                                              |
| Unchanged         | `observation` still requires `actionRefs`; nothing about the six canonical types moves                                                                                                    |

This keeps the extensibility the "open convention" wording was reaching for — a deployment with a
`cost-baseline` habit can have one — while leaving the validator with something to fail on.

## 6. Session-state contract (mem0 deferred post-v1)

**Semantic recall (mem0/Qdrant) is deferred post-v1**; v1 ships no vector store. If introduced
later, scope every insert/query by the composite key `{tier}:{scope-id}` (e.g.
`developer-team:cluster-a/team-x`) with **server-side** isolation — one collection or
access-controlled key per scope, never a client-supplied filter, because a cross-scope read is an
isolation escape ([03](03-security-model.md) §3) — with TTL entries (30–90 d) that graduate durable
observations into OKF.

**Session state (existing, `multiuser_memory`):** `session_db.sqlite` keyed by
platform/space/thread; per-user memory in `memories/users/<safe_user_id>.md`; shared SOPs in
`memories/MEMORY.md`. Per-user isolation by runtime `user_id`. This stays as-is.

Two consumers beyond the agent itself: the gateway keys **routing thread affinity** on the
normalized `threadKey` (§2b, mode `thread` — `slack:<channel>:<thread_ts>` |
`googlechat:<space>:<thread>`), and the broker uses `trace.threadId` to deliver the **action report
and its undo handle back into the thread that triggered it** — including asynchronously, when a
`gated` action is approved minutes later. Session state is never an authorization input.

## 7. Agent mesh contract

Replaces the review-gate contract that occupied this slot. Agents call each other **directly**: a
parent **delegates** down, a child **escalates** up ([02](02-agent-personas.md) §2.3, README
invariant 5). The mesh carries **requests, never authority**.

**Transport.** HTTPS + JSON, mTLS, served by the **agent pod** (not the broker) at
`https://<agent-name>.<namespace>.svc:8444/v1alpha1/mesh/{delegate,escalate}`. Discovery is the
`(tier, scope)` index over `Agent` CRs (§1.2) — the same key routing and cardinality use — resolved
to the CR's `status.serviceStatus.endpoint`. There is no registry and no broadcast.

```yaml
# ---- request -----------------------------------------------------------------------------------
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: MeshRequest
meshKind: delegate # delegate (parent→child) | escalate (child→parent)
from:
  agentIdentity: cluster-admin/my-project/cluster-a # derived from the caller's SA, not trusted from here
to:
  tier: developer-team
  scope: { projectId: my-project, clusterName: cluster-a, namespace: team-x }
intent: "apply the new egress NetworkPolicy baseline to your namespace"
context: # untrusted DATA for the callee's model, never instructions to its broker
  policyName: egress-baseline-v3
  deadline: "2026-07-25T00:00:00Z"
  reference: "knowledge/tenancy-model/egress-baseline.md"
trace: { traceId: 4bf92f…, spanId: 00f067aa0ba902b7, sessionId: hermes-9f21c4 }
requester: { kind: agent, id: cluster-admin/my-project/cluster-a } # attribution only
chain: # LOOP PREVENTION + causation key — see below
  chainId: 01J8Z2K9Q7V3X5M6N8P0R2T4W0 # the originating actionId; copied into every ActionRecord
  depth: 1 # code constant MaxMeshDepth = 3; depth >= 4 is refused
  visited: ["platform/my-project", "cluster-admin/my-project/cluster-a"]
idempotencyKey: sha256:1b7e…90fa
deadlineSeconds: 60
```

**`meshKind`, not `kind_`.** The discriminator between a delegation and an escalation is
`meshKind` — a plain field beside the Kubernetes-style `apiVersion` / `kind` envelope header. The
earlier spelling `kind_` was a documentation artifact for "the other kind"; no OpenAPI generator,
Go struct tag, or JSON schema can round-trip a trailing underscore cleanly, and a reviewer cannot
tell it from a typo. `meshKind` is a closed enum (`delegate` | `escalate`), required, and validated
against the lineage check in rule 3 — a `delegate` from a non-parent is refused
`not-in-lineage`, not silently re-interpreted as an escalation.

```yaml
# ---- response ----------------------------------------------------------------------------------
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: MeshResponse
outcome: accepted # accepted | completed | refused | deferred | paused | frozen | over-budget | loop-detected
taskId: hermes-task-77c1 # present when accepted (async work)
actionIds: [] # ActionRecord ids produced by the callee, filled in on completion
message: "accepted; will apply egress-baseline-v3 to team-x and report in this trace"
refusalReason: "" # required when outcome == refused
retryAfterSeconds: 0 # set for paused | frozen | over-budget
```

**The rules that make this safe.**

1. **Authentication.** mTLS plus a `TokenReview` of the caller's **reader** SA. The callee derives
   `from.agentIdentity` from the authenticated identity and **overwrites** whatever the body said —
   the `from` field is a convenience for logs, never an input to a decision.
2. **The callee re-authorizes in its own scope, under its own gates.** A mesh request is exactly as
   powerful as the same words typed by a human into the callee's chat: the callee composes its
   **own** Action Envelope, submits it to **its own** broker with **its own** actor identity, and
   gets its own classification, brake check, budget, and gates. Authority is never inherited, never
   forwarded, never pooled. A `gated` action requested by a parent still waits for the **child's**
   approval roster.
3. **Topology.** `delegate` is accepted only from the caller named in `spec.parentRef`; `escalate`
   only from an agent whose `parentRef` names the callee. Sibling and cross-tree calls are refused
   with `refused / not-in-lineage`. Verified against the CR graph, not the request body.
4. **Refusal is a first-class outcome.** A callee may refuse — out of scope for it, contradicts a
   local `ChangePolicy`, or it simply disagrees. `refused` with a `refusalReason` is a normal
   response, not an error; the caller must handle it and must not retry it as a different shape.
5. **Paused / frozen callee.** Returns `paused` or `frozen` with `retryAfterSeconds`. The caller
   **must not** route around it — no doing the work itself in the callee's scope (it has no
   authority there), and no asking a sibling. It reports the blockage to a human.
6. **Timeouts.** `deadlineSeconds` default 60, ceiling 300. Work that outlives the deadline returns
   `accepted` + `taskId` and reports asynchronously into the same `traceId`. A timed-out caller must
   treat the outcome as **unknown**, not failed, and reconcile by reading the callee's
   `ActionRecord`s for its `idempotencyKey`.
7. **Loop prevention.** `chain.visited` is the ordered list of agent identity keys already in the
   call chain, and `chain.depth` its length. On receipt the callee: refuses with `loop-detected` if
   its own identity is already in `visited`; refuses if `depth >= 4`; otherwise appends itself and
   increments `depth` before making any onward call. A request arriving with an absent or malformed
   `chain` — including an absent `chainId`, a `depth` that disagrees with `len(visited)`, or a
   duplicate entry in `visited` — is refused, not defaulted, because a missing chain is exactly what
   a loop looks like after one bad hop.

   **The depth limit is a code constant, not a budget.** `MaxMeshDepth = 3` in
   `internal/mesh/limits.go`; the rejection predicate is `depth >= MaxMeshDepth + 1`, i.e.
   `depth >= 4`. There is **no** CRD field, `ChangePolicy` rule, or envelope field that sets it, and
   the earlier description of a "default budget 3" is withdrawn — a default implies a configurable,
   and a configurable that no schema exposes is a field a builder will invent. Three is the whole
   hierarchy (`platform → cluster-admin → developer-team`), so a fourth hop is by construction
   either a loop or a tier that does not exist; there is nothing legitimate to configure.

   **`chain.chainId`** is set once by the originator (its own `actionId`), never rewritten, and
   copied into `spec.trigger.chainId` and the `kube-agents/chain-id` label of every `ActionRecord`
   any callee produces for the request (§4.3). It is attribution, not authority.

8. **Rate.** Mesh requests consume the **callee's** initiative budget, not the caller's, so a
   chatty parent cannot spend a child's autonomy. Inbound mesh rate is separately capped per caller.

## 8. Audit & attribution contract

Extends `docs/designs/audit-logging-user-attribution.md`. The requirement is a **single unbroken
chain** from the human's message to the row in the cloud audit log:

```text
chat message            → platform + routingMode + requester
                          + threadKey + traceId                    (router, §2b)
  → Action Envelope     → same traceId, requester, trigger        (§4.1)
    → ActionRecord      → actionId + actorServiceAccount + traceId (§4.3)
      → Kubernetes write→ annotation kube-agents/action-id + field manager
        → audit log     → actor SA + annotation + user-agent
```

**Every write carries `kube-agents/action-id`.** The broker stamps
`metadata.annotations["kube-agents/action-id"]` on every object it creates or updates, plus
`kube-agents/agent` (the identity key) and `kube-agents/risk-class`. Server-side apply uses field
manager `kube-agents/<tier>/<scope>`. `vap-agent-scope` **rejects** a write by an actor SA that
lacks the annotation, so an unjournaled write is impossible rather than merely detectable
([03](03-security-model.md) §4.3) — this is SLI 2 ([01](01-vision-scope.md) §7) enforced at
admission.

**Where an annotation cannot go** — deletes, subresource writes, and cloud API calls — correlation
is carried instead by the HTTP user-agent
`kube-agents-broker/<version> (agent=<identity>; action=<actionId>)`, which lands in both the
Kubernetes and the Cloud Audit Log, plus the `ActionRecord` itself. Every audit query in §10 must
accept either correlation path.

**OTel resource/span attributes** emitted by the broker on every action:

| Attribute                    | Example                                                      |
| ---------------------------- | ------------------------------------------------------------ |
| `kubeagents.action_id`       | `01J8Z2K9Q7V3X5M6N8P0R2T4W6`                                 |
| `kubeagents.agent_identity`  | `developer-team/my-project/cluster-a/team-x`                 |
| `kubeagents.tier` / `.scope` | `developer-team` / `team-x`                                  |
| `kubeagents.actor_sa`        | `developer-team-team-x-actor`                                |
| `kubeagents.risk_class`      | `elevated`                                                   |
| `kubeagents.trigger_source`  | `watch`                                                      |
| `kubeagents.requester`       | `slack:U02ABCDEF` (canonical, V-11)                          |
| `kubeagents.chat_platform`   | `slack` \| `googlechat` \| `""` (non-chat)                   |
| `kubeagents.routing_mode`    | `slash` \| `handle` \| `thread` \| `channel` \| `nl` \| `""` |
| `kubeagents.thread_key`      | `slack:C01TEAMXOPS:1721840283.001900`                        |
| `kubeagents.interaction`     | `typed` \| `blockkit` \| `card` \| `kubectl`                 |
| `kubeagents.undo_available`  | `true`                                                       |
| `kubeagents.approved_by`     | `slack:U07GHIJKL` (gated actions only)                       |

**Platform and routing mode are recorded as a pair, and both are required.** `routingMode` alone is
ambiguous once two platforms are live — `channel` means a Slack channel binding or a Chat space
binding, and the two have different administrators, different membership, and different failure
modes. The attribution tuple is therefore
`(platform, routingMode) ∈ {slack, googlechat} × {slash, handle, thread, channel, nl}`, recorded on
**every** chat turn alongside the canonical requester, the `threadKey`, and the trace ID, and
carried unchanged into the Action Envelope's `requester.platform` (§4.1) and the `ActionRecord`
(§4.3). For a non-chat origin — `kubectl`, the API, the mesh, a watch — `chat_platform` and
`routing_mode` are the empty string and `interaction` is `kubectl` or unset; they are **never**
defaulted to `slack`, because "we do not know how this arrived" and "it arrived over Slack" must not
render identically in an audit query.

These are attribution fields only. Nothing in the tuple is read by the classifier, the roster check,
or `Authorize()` — routing is never an authz signal (§2b), and an action's risk class is
byte-identical whether it came from a slash command, a bound channel, or a Block Kit click.

Chat turns additionally record the **resolved agent**, and — where the turn is a `gated` decision —
whether the approval was typed or clicked (§2b.1), so a roster review can tell a deliberate
`/kage approve` from a button press without changing what either was allowed to do. The durable
attribution for a mutation is the **`ActionRecord`** — not a merge commit, not a PR URL. Where the
mirror is enabled, the commit trailers (§3.1) provide a secondary, human-browsable index into it.

## 9. MCP tool surface — write-capable, as envelope builders only

The read-only generation's job here was **removal**: retire `create_cluster`, make the remote `gke`
MCP describe/list only, delete the `apply_manifest` helpers. That is now **inverted** — but not by
putting the deleted tools back.

**The distinction the whole section turns on:** a tool may **compose an Action Envelope and submit
it to the broker**. A tool may **never call a mutating API itself**. The first is an agent asking
for something to happen, under classification, gating, snapshotting, verification, and journaling.
The second is an unjournaled write by a process holding an LLM — the exact thing
[03](03-security-model.md) §4 exists to make impossible. A "write tool" that talks to
`container.googleapis.com` is not a faster version of the broker; it is a hole in it.

Concretely: the agent pod holds no write credential (§2), so a mutating tool inside it has nothing
to authenticate with. The tools below are therefore not privileged — they are **request builders**,
and their entire safety story is that there is nothing to escalate.

| Tool / server                                   | Read-only generation                                                       | Imperative end state                                                                                                                                                                                                                                      |
| ----------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`submit_action`** (new, core)                 | —                                                                          | **The one mutation tool.** Takes `intent` + `operations` + `trigger`, fills `trace`/`requester` from the session, computes `idempotencyKey`, `POST`s to the broker, returns the `ActionResponse` (§4.1). Every other write-shaped tool is sugar over this |
| **`plan_action`** (new)                         | —                                                                          | Same, with `dryRun: true`. Returns the classification, blast radius, and undo plan **without executing** — how an agent checks whether something will gate                                                                                                |
| `apply_manifest` / `delete_cluster_manifest`    | Undecorated `kubectl` helpers; deleted                                     | **Return as envelope builders**: `apply_manifest(obj)` → `submit_action(op=apply, …)`. The `kubectl` shell-out never returns — the broker uses a typed client                                                                                             |
| `create_cluster` (remote `gke` MCP)             | Retired; provisioning became "author KCC YAML + open a PR"                 | **Returns as `provision_cluster`** — a builder emitting a `ContainerCluster` (or Terraform) desired state into an envelope. Classified `gated` (irreversible), so it lands in the platform roster's approval queue, then the **broker** applies it        |
| `gke` MCP wiring (`renderConfigYAML()`)         | Fronted read-only / dropped from `platform_toolsets`; ConfigMap mounted RO | **Stays describe/list/get.** A remote MCP writes with a credential the broker does not control and cannot journal — so the remote proxy remains read-only _permanently_. Cloud writes go envelope → broker → cloud API with the **actor GSA**             |
| `gke-cluster-creator` skill                     | Retired/adjusted to author YAML + open a PR                                | **Restored**: gathers requirements, grounds on the `cluster-blueprint` OKF, calls `plan_action`, shows the human the classification, calls `submit_action`                                                                                                |
| `submit-suggestion` skill                       | The sole mutation path                                                     | **Deleted.** No branch, no PR, no propose verb anywhere in the agent surface                                                                                                                                                                              |
| `raise-escalation` skill                        | Wrote a `knowledge/escalation/` file; the parent polled for it             | **Replaced** by the mesh call (§7). The OKF type survives only for human-resolved requests (§5)                                                                                                                                                           |
| Brake tools (`pause_self`, `freeze`, `approve`) | —                                                                          | **Never exist.** An agent may not pause itself (it would also be able to resume itself), may not freeze, and may not approve. `undo` is human-invoked (§4.4)                                                                                              |
| `get_action_status` / `list_actions` (new)      | —                                                                          | Read-only over the agent's own `ActionRecord`s — how an agent reports what it did and offers the undo handle                                                                                                                                              |

**Invariants a reviewer can check mechanically** (and §10 does):

- exactly one code path in the agent image performs mutation, and it is an HTTP call to
  `https://<agent>-broker…:8443/v1alpha1/actions`;
- no `kubectl apply|create|patch|delete|scale`, no `gcloud … create|update|delete`, and no mutating
  client-go verb appears anywhere in the agent image or its skills;
- the rendered runtime config (`renderConfigYAML()` → the mounted ConfigMap, mounted
  `readOnly: true`) exposes no mutating remote MCP tool — checked against the **rendered** config,
  never only the baked `agents/<tier>/config.yaml`.

## 10. Verification

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-CTR, V-CMP**. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

Contract-level checks. Security behaviour is verified in [03](03-security-model.md) §11; these
verify that the **shapes** in this document are real.

**Schema round-trips and rejects (§1)**

- Every `Agent` CR in `examples/` and `deploy/` validates against the generated CRD; a round-trip
  (`kubectl apply` → `get -o yaml` → re-apply) is a no-op diff.
- **V-1…V-13 each have a negative test**: wrong tier enum; tier mutation; missing per-tier scope
  field; missing `parentRef`; a developer-team `Agent` in the wrong `metadata.namespace`; a second
  CR for the same `(tier, scope)`; **a child whose scope is not a strict subset of its parent's, and
  a child whose parent is the wrong tier (V-6)**; an enabled chat integration with an empty
  `allowedUsers`; an `initiativeBudget` above the code ceiling; a
  `spec.security.serviceAccountName` that is not the tier's reader SA; an unqualified or mutable
  principal (V-11); a channel bound twice (V-12); a second `ChatOpsConfig` (V-13). Each is rejected
  at apply time with the field path in the message.
- **Per-class budget (§1.1):** each of the ten `initiativeBudget` leaves is rejected one above its
  ceiling and accepted at it; `flapWindow: 1m` is rejected; a CR carrying the retired flat
  `actionsPerHour` / `actionsPerDay` under `initiativeBudget` is **pruned or refused**, so the
  class-agnostic shape cannot survive an upgrade. Behaviourally: 50 `routine` self-initiated actions
  in an hour do **not** consume the `elevated` sub-cap (assert the 51st `routine` is refused
  `429 budget-exhausted` while an `elevated` action still executes), a `chat`-triggered action does
  not draw on `selfInitiated`, a `delegation` from a parent **does**, and `trigger.source: undo` is
  never refused for budget. Exhaustion escalates and **does not pause** the agent.
- **No authority fields:** a CR carrying `spec.rbac`, `spec.rules`, `spec.riskClass`,
  `spec.scopeOverride`, `spec.brokerServiceAccountName`, or `spec.actorServiceAccountName` is
  pruned/rejected; a test greps the generated CRD schema to assert none of those property names
  exists and that `spec` sets no `x-kubernetes-preserve-unknown-fields`.

**Chat integration, addressing & routing (§1.1, §1.2, §2b, §2b.1)**

- **Slack is the default, Chat is opt-in:** every `Agent` in `examples/` and `deploy/` ships
  `integration.slack.enabled: true` with a non-empty, platform-qualified `allowedUsers`; a generated
  CR sets `googleChat.enabled: false`. A grep test asserts no `slack: { enabled: false }` remains as
  a shipped default and that the retired **per-agent Slack relay** is absent from the agent image —
  the only Slack client in the build is the router's.
- **Fleet-level config is fleet-level:** the `Agent` CRD schema contains **no** Slack token, app
  token, signing secret, or command-name property (grep the generated CRD); `ChatOpsConfig` is
  cluster-scoped, rejects a name other than `default`, rejects a second object, rejects
  `socketMode.maxConnections: 2` rather than clamping it, and resolves Secret refs only in
  `kubeagents-system` (a ref naming another namespace fails to compile — the field does not exist).
  No actor SA can `get` a `ChatOpsConfig` or its Secrets.
- **One connection:** with two router replicas running, exactly one holds the Socket Mode
  connection and the standby holds none (assert on `status.slack.connected` and the Slack
  `apps.connections.open` count); killing the leader re-establishes exactly one connection, and
  never two.
- **Principal format (V-11):** a table test over accepted forms (`slack:U02ABCDEF`, `slack:W…`,
  `googlechat:users/1234567890`) and rejected forms (`U02ABCDEF`, `slack:@aparco`,
  `aparco@acme.com`, `slack:A. Parco`, `""`, `"   "`, `slack:`, `discord:1234`). An all-blank list
  is treated as **empty** and refused by V-7, not as an allowlist. `internal/principal.Canonical`
  is the single comparison function — a grep asserts `allowedUsers`, `ApprovalRoster.approvers`,
  `requestedBy`, and the envelope `requester` all route through it, and a cross-platform pair
  (`slack:U02ABCDEF` vs `googlechat:users/1234567890`) never compares equal.
- **Channel binding (V-12):** binding `C01TEAMXOPS` to a second `Agent` is rejected `Duplicate`
  naming the incumbent; a bare message in a bound channel resolves with `routingMode: channel` and
  **zero inference calls** (assert the inference client was not invoked); the same message in an
  unbound channel falls through to `nl`; deleting the binding makes the channel fall through again
  with no router restart. A user **not** in the target's `allowedUsers` posting in the bound channel
  is refused — the binding addresses, it does not admit.
- **Precedence (§2b):** a five-case table over one message text proves the order
  slash → handle → thread → channel → nl, including the conflict cases: an explicit handle inside a
  bound channel wins over the binding; a slash command inside an already-routed thread wins over
  affinity and re-points the thread; and only the `nl` case spends an inference call.
- **Thread affinity (§2b, §6):** `threadKey` is `slack:<channel>:<thread_ts>` where `thread_ts` is
  the **root** message's timestamp for both the root and every reply; a Chat thread produces
  `googlechat:<space>:<thread>`; and — the decisive one — a **different** user replying in a routed
  thread is re-authorized against the target's `allowedUsers` and refused if absent, inheriting
  nothing from the human who opened the thread.
- **Grammar (§2b.1):** `/kage` is the only command in the Slack app manifest, and a test asserts the
  manifest's command list equals `ChatOpsConfig.status.registeredCommands`; each verb in the table
  parses with and without a leading `@` on the handle; an unknown first token is refused with the
  verb and handle lists and is **never** treated as intent; `/kage status` with no handle resolves
  via channel binding, then thread, then a fleet summary; `/kage help` lists only agents whose
  allowlist admits the caller.
- **Block Kit is not authorization (§2b.1):** a button click by a **non-roster** Slack user is
  refused even though the payload is validly signed; a click whose `value` names a **different**
  `actionId` than the message rendered is refused and journaled; a replayed payload is refused; a
  second click on a decided action re-renders the outcome rather than approving twice; a click by
  the action's own requester is refused under `allowSelfApproval: false`; and an action whose class
  rose since it parked is refused at click time. For every case, the identical outcome is produced
  by the typed `/kage approve <id>` — asserted as a paired table test, because "the button and the
  command must not diverge" is the property.
- **The brake survives Slack (§2b.1, §4.4):** with the router scaled to zero **and** the Slack app
  token revoked, `pause`, `resume`, `freeze`, `thaw`, `undo`, `approve`, `reject`, `uncontest`, and
  `status` all succeed via `kubectl` / the API with unchanged authorization semantics; a chaos run
  that drops the Socket Mode connection mid-approval leaves no action auto-approved and no action
  stuck outside its TTL.
- **Chat parity (§2b):** the parity table is executed as a matrix — for each of the five routing
  modes, the same logical message on Slack and on Google Chat resolves to the same agent, produces
  the same dispatch over the per-agent transport, and differs only in `platform`, `threadKey`, and
  principal format. Disabling Slack fleet-wide leaves Google Chat fully functional, and vice versa.

**Identity templates match what admission enforces (§2)**

- For each of the six identities, the rendered manifest exists, carries
  `kube-agents/{tier,role[,scope]}`, and is referenced/derived correctly (reader by
  `spec.security.serviceAccountName`; actor by the derivation, surfaced in
  `status.broker.actorServiceAccount`).
- **Templates ↔ policy agreement:** for every rule in every §2.2 actor template,
  `vap-agent-scope` **admits** it; for a mutated copy of each template with one extra
  apiGroup/resource/verb, `vap-agent-scope` **denies** it. This is the check that keeps the document
  and the policy from drifting — run it as a table test over all three tiers.
- `kubectl auth can-i --as=system:serviceaccount:…`: every reader returns **no** for
  `create|update|patch|delete` on everything, universally; every actor returns **yes** in scope for
  its templated resources and **no** out of scope, for `escalate`/`bind`/`impersonate`, and for
  writes to `changepolicies`/`fleetfreezes`/`approvalrosters`. On `actionrecords` an actor returns
  **yes** for `create` and for `update` on `actionrecords/status`, and **no** for `update`/`delete`
  of the record itself (§2.2.1) — the append-only property, asserted in both directions. On
  `agents` a non-platform actor returns **yes** only for creating/patching a **child** CR within
  its scope, and **no** for its own CR, a parent's, and for `spec.operations.paused` on any CR
  ([03](03-security-model.md) §3.3 rule 3).
- **`kube-system` add-on allowlist (§2.2):** the broker's list and the `vap-agent-scope` CEL list
  are byte-equal after normalization (a table test asserts it, so the two cannot drift). For each
  entry, a cluster-admin `patch` is admitted and classifies **at least `elevated`**; a `delete` of
  the same object is refused; the same write from a `platform` or `developer-team` broker is
  `forbidden`; a `kube-system` object **not** on the list (e.g. `ConfigMap kube-root-ca.crt`,
  `DaemonSet konnectivity-agent`) is `forbidden`; and any write into `kubeagents-system` is
  `forbidden` for every tier, with no allowlist consulted.
- Cloud: no actor GSA holds `roles/owner`, `roles/editor`, `roles/iam.securityAdmin`,
  `roles/resourcemanager.projectIamAdmin`, or `iam.serviceAccounts.setIamPolicy`; every actor GSA
  binding carries the scope IAM condition from §2.3.

**Envelope (§4.1)**

- A valid envelope round-trips to an `ActionResponse` with a resolvable `actionRecordRef`.
- **Scope spoofing is rejected:** an envelope carrying a top-level `scope`, `tier`, `actor`,
  `riskClass`, `approved`, `bypass`, `force`, `skipJournal`, or `undoPlan` is refused `400`/`403`,
  executes nothing, and (for the reserved and bypass families) produces a `Rejected`
  `ActionRecord` plus a security event. A developer-team agent's envelope naming a target in
  another namespace is refused, whatever the body claims.
- **Atomicity:** a two-operation envelope with one out-of-scope target applies **neither**.
- **Idempotency:** the same `idempotencyKey` submitted twice within the window yields one
  `ActionRecord` and `decision: deduplicated`. **The key is recomputed:** an envelope whose
  `idempotencyKey` does not match the broker's own JCS/SHA-256 computation is refused
  `400 idempotency-key-mismatch`; a golden corpus of envelope→key pairs (including one with a
  Secret payload, one with reordered operations, and one differing only in `rationale`) pins the
  algorithm so two implementations cannot disagree — reordering operations must **not** change the
  key, and changing `dryRun` must.
- **Anti-replay (§4.1):** a byte-identical re-POST of a successful envelope is refused
  `403 replayed-envelope` and executes nothing; an envelope with `issuedAt` 5 minutes old is refused
  `403 envelope-expired`; an envelope with `issuedAt` 5 minutes in the future is likewise refused; a
  captured envelope with one byte of `idempotencyKey` flipped is refused
  `400 idempotency-key-mismatch` (not executed as new work); a nonce reused after its 120 s TTL is
  still refused; and a broker restarted mid-test refuses envelopes issued before its start time. All
  four rejections produce a `Rejected` `ActionRecord` **and** a security event.
- **Unknown fields** are refused, not dropped (assert the error names the field).

**Classification (§4.2)**

- The three worked examples classify exactly as documented (`routine`, `elevated`, `gated`), with
  the named rules in `classification.reasons`.
- **Prose cannot move a class:** the same envelope with an `intent`/`rationale` asserting the action
  is safe, pre-approved, and urgent classifies identically — byte-for-byte the same
  `classification` block.
- **Stricter-only:** a `ChangePolicy` raising a class takes effect; one attempting to lower a class
  is rejected at admission; a hand-crafted policy object that somehow lowers a value has no effect
  because the broker takes the maximum (test both).
- No actor SA can create, update, or delete a `ChangePolicy`.
- **Production label precedence (§4.2):** all four cells of the matrix — object canonical, object
  alias, namespace canonical, namespace alias — resolve as documented, and the two disagreement
  cases are asserted explicitly: `kube-agents/environment: staging` + `env=production` is **not**
  production; `kube-agents/environment: production` + `env=dev` **is**. `env=prod` matches nothing.
- **Blast radius (§4.2):** an envelope with 51 literal operations is refused at schema validation; a
  `targetSelector` expanding to 51 objects classifies `gated` with `blast-radius-cap` in
  `reasons[]`; one expanding to 101 **aborts** with no approval path offered; `fractionOfScope` is
  computed against the documented workload-kind denominator, is floored at 20 (a 2-object namespace
  does not report `1.0`), and is `null` — with the absolute caps still applied — when the count is
  unavailable.
- **`secret-material-egress` (§4.2):** a ConfigMap whose value equals a live `Secret` value in the
  same namespace classifies `gated`, and so do its base64 and URL-encoded forms and the case where
  the value appears as one token inside a longer connection string. A ConfigMap containing a
  high-entropy string that is **not** a live Secret value (an image digest, a ULID, a git SHA, a
  base64 CA bundle) classifies normally — the anti-entropy assertion, run as a table test, because
  a heuristic implementation passes the positive cases and fails only here. `reasons[]` names the
  source Secret and key and **never** the value; no test artifact, log line, or `ActionRecord`
  contains the material.
- **`cross-tier-direct-operation` (§4.2):** a platform-tier envelope patching a Deployment in a
  namespace that **has** a developer-team agent classifies `gated` with the child named in the
  reason and `kube-agents/cross-tier` on the record; the same envelope against a namespace with
  **no** agent classifies normally; the same rule fires for cluster-admin → developer-team; and the
  **delegated** path (§7) for the same work classifies `routine` in the child — the gradient, not
  just the gate.
- **Path dialects (§4.2):** a `ChangePolicy` whose `fieldPaths` entry starts with `/` is rejected at
  admission; a dotted `fieldPaths` prefix matches a deeper JSON Pointer change; and the
  bracket-quoted annotation example matches the `~1`-escaped diff path for the same change.

**`ActionRecord` and undo (§4.3)**

- **Undo-plan generation per verb:** `create` → `delete`; `apply`/`patch` on an existing object →
  `restore`; `apply` on a new object → `delete`; `scale` → `restore`; `delete` of a reconstructable
  object → `recreate`; cloud `setSize` → `inverse`. Each generated plan is dry-run-validated and
  then **actually replayed**, and the resulting object diffed against the recorded snapshot.
- **Unrevertible ⇒ gated:** deleting a bound PVC, deleting a namespace, and rotating a credential
  each produce `undoable: false` and park as `PendingApproval` — never execute.
- **Sanitizer:** a restore step contains no `resourceVersion`, `uid`, `managedFields`, or `status`,
  and a `Secret`'s values are digested in the CR while remaining restorable from the journal store.
- **Immutability:** `spec` updates and `delete` on an `ActionRecord` are rejected for every agent
  identity, including the actor that created it.
- Lifecycle: every phase in the §4.3 table is reachable in a test, and a `gated` action left past
  its roster TTL (**24 h**, §4.4) becomes `Expired`, not approved.
- **Retention vs undo window (§4.3):** every executed record carries both `expiresAt` and
  `undoWindowExpiresAt`, with `undoWindow ≤ ttl` (CEL rejects a record violating it); the per-class
  pairs are 30/7, 90/30, 365/90 days; an `UndoRequest` filed **inside** the window executes; one
  filed after it is `Refused` with the expiry in the message **while the record still exists** —
  proving the two clocks are independent rather than one field read twice.
- **Undo linkage is bidirectional (§4.3):** after an undo, the undo record carries
  `spec.trigger.undoOf` **and** the label `kube-agents/undo-of`, and the original carries
  `status.undoneBy`; each direction is resolvable by a label or field query without scanning; a
  record with `trigger.source: undo` and an empty `undoOf` is **rejected at admission**; and after
  the original is deleted at its TTL the undo record still names what it reverted.
- **`chainId` (§4.3, §7):** a delegated fleet rollout across three tiers yields
  `kubectl get actionrecords -A -l kube-agents/chain-id=<id>` returning every record in the chain
  and nothing else; a non-chained action's `chainId` equals its own `actionId`; a forged `chainId`
  in a `MeshRequest` changes attribution only and grants nothing.
- **`status` writability (§4.3):** for each principal in the table, the permitted field set is
  accepted and one field outside it is denied — the owning broker cannot write `approvals`,
  `contested`, or `undoneBy`; a **different** agent's broker cannot write the record at all; the
  gateway cannot write `applied` or `verification`; and a **human cluster-admin**
  `kubectl patch --subresource=status` is **denied**, so a self-approval by hand is impossible. The
  retention controller's `delete` is refused before `expiresAt` and before export confirmation.

**Brake (§4.4)**

- `pause` stops the agent mid-queue; the in-flight action lands or rolls back, never half-applied.
- `resume` requires roster membership; `pause` and `undo` require only `allowedUsers`.
- A `FleetFreeze` blocks the scope; `allowUndo: true` still permits undo; **making the freeze object
  unreadable freezes the scope** rather than opening it.
- Approvals: `allowSelfApproval: false` refuses the requester's own approval; `minApprovals: 2`
  requires two distinct approvers; an empty roster never auto-approves.
- `contested`: an undone change is not re-applied; only a roster member can clear the marker, and
  `/kage uncontest <action-id>` (§2b.1) is the supported path — invoked by a non-roster member it is
  refused, and `resume` never clears it.
- **Approval TTL:** the default resolves to **24 h** from `ApprovalRoster.spec.ttl` with no number
  hard-coded in the broker; a roster `ttl: 96h` is rejected against the 72 h ceiling; an action
  approved after its targets moved is **re-classified at approval time** and refused if the class
  rose or `preconditions.uid` no longer matches.
- All of pause / freeze / undo / status work with the inference endpoint and the agent pod down.

**Mesh (§7)**

- A parent→child `delegate` succeeds; the callee's `ActionRecord` names the **callee's** actor SA,
  not the caller's.
- **Re-authorization:** a request that would be `gated` for the callee waits for the **callee's**
  roster even though the caller is a parent holding broader authority.
- **Lineage:** sibling and cross-tree calls are refused `not-in-lineage`; `from` is overwritten from
  the authenticated identity (a forged `from` changes nothing).
- **Loop prevention:** a request whose `visited` already contains the callee is refused
  `loop-detected`; `depth >= 4` is refused; an absent or malformed `chain` — missing `chainId`,
  `depth != len(visited)`, or a duplicate in `visited` — is refused, not defaulted; a deliberately
  constructed A→B→C→A cycle terminates at the first repeat. A **grep test** asserts no CRD field,
  `ChangePolicy` field, or envelope field can set the depth: `MaxMeshDepth` appears once, as a
  constant.
- **Schema hygiene:** the discriminator is `meshKind` and a request carrying `kind_` is refused as
  an unknown field; a generated Go struct / OpenAPI schema round-trips the request and response
  shapes with no field requiring a rename (the check that would have caught `kind_`).
- A paused or frozen callee returns `paused`/`frozen` + `retryAfterSeconds`, and the caller does not
  route around it.

**Audit, repo, OKF, MCP (§3, §5, §8, §9)**

- **Trace continuity:** for a sampled chat-initiated action, one `traceId` links the chat audit
  record, the envelope, the `ActionRecord`, and the Kubernetes/Cloud audit entry.
- **Platform × routing mode (§8):** every chat-originated action carries a non-empty
  `kubeagents.chat_platform` **and** `kubeagents.routing_mode`, and the pair matches how the message
  actually arrived — asserted across all ten `{slack, googlechat} × {slash, handle, thread, channel,
nl}` cells. `kubeagents.requester` is the canonical `<platform>:<id>` form in every case, and
  `kubeagents.thread_key` matches the router's `threadKey`. A `kubectl`-, mesh-, watch-, or
  cron-originated action carries **empty** `chat_platform` and `routing_mode` — never `slack` by
  default — and `interaction: kubectl` or unset; an audit query filtering `chat_platform=slack`
  returns no non-chat actions.
- **Attribution is not authority:** the same envelope classified with each of the ten
  `(platform, routingMode)` pairs produces a byte-identical `classification` block, and a forged
  `routingMode` or `chat_platform` changes the audit row and nothing else. For a `gated` action,
  `interaction` distinguishes `blockkit` from `typed` while `approved_by` and the roster outcome are
  identical.
- Every object written by an actor SA carries `kube-agents/action-id`; a write with the annotation
  stripped is **rejected at admission**; deletes and cloud calls are correlated by the broker
  user-agent.
- **Repo layout** matches §3, `journal/<Y>/<M>/<D>.ndjson` parses, and — the retirement check —
  no `submit-suggestion` skill, propose branch prefix, or applier workflow remains.
- **Mirror is not a control path:** with the mirror enabled, a hand-authored commit to the mirrored
  paths changes nothing in the cluster; a mirror-push failure does not fail, delay, or revert the
  action.
- **OKF:** every `knowledge/` file carries a valid `type` and resolving links; `observation` files
  written after an action carry `actionRefs` that resolve to real `ActionRecord`s. **The type check
  can fail:** a file with `type: made-up-thing` absent from `knowledge/index.md`'s `typeRegistry`
  fails the lint; the same file passes once the type is registered; a file of a **registered** type
  missing one of that type's `requiredKeys` fails; a registry entry redefining a canonical type is
  rejected; and `type: index` on any file other than `index.md` fails.
- **MCP:** exactly one mutation path exists in the agent image (the broker HTTP call); no mutating
  `kubectl`/`gcloud`/client-go verb appears in the image or its skills; the **rendered** runtime
  config exposes no mutating remote MCP tool and is mounted `readOnly: true`; `plan_action` with
  `dryRun: true` returns a classification and undo plan and writes nothing.
