# Overview 03: User Interactions

**Summarizes:** [`docs/design/02` §2.4](../design/02-agent-personas.md) ·
[`04` §2–§3](../design/04-workflow-model.md) ·
[`06` §2b](../design/06-api-and-data-contracts.md)

---

## 1. Who interacts with the system

| Audience                   | Their agent              | What they do with it                                                                                                                  |
| -------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| **Platform teams**         | Platform Agent           | Provision and upgrade clusters, set cross-cluster governance and global policy, manage cost and capacity, drive fleet-wide compliance |
| **Cluster administrators** | Cluster Admin Agent      | Node pools, add-ons, namespace and tenant provisioning, cluster-scoped policy and quotas, cluster health                              |
| **Developer teams**        | Developer Team Agent     | Onboard and scale workloads, run rollouts, troubleshoot and repair, wire observability                                                |
| **Approvers** (a roster)   | Any agent they are on    | Approve or reject the gated class, release the brake, thaw a freeze                                                                   |
| **Anyone authorized**      | Any agent they can reach | Hit the brake: `pause`, `undo`, `status`                                                                                              |

**SRE is not a separate audience.** Reliability work appears as critical user journeys at every
layer, served by whichever persona owns the scope it applies to.

---

## 2. The two authorization tiers

This distinction governs every interaction in the system, so it is worth stating before the
surfaces:

| Tier                | Who is on it                              | What it lets you do                                                                             |
| ------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------- |
| **`allowedUsers`**  | Per agent, an explicit allowlist          | Talk to the agent, direct its work, **and stop it** — `pause`, `undo`, `status`, `actions`      |
| **Approval roster** | Per agent, defaults to the scope's owners | **Let it proceed** — `approve`, `reject` — and **relax a stop** — `resume`, `thaw`, `uncontest` |

> Anyone trusted enough to use an agent is trusted enough to hit its brake; braking is always the
> safe direction. **`pause` and `undo` are deliberately the most widely available commands in the
> system.**

Three rules keep approval from becoming a laundering path:

- Roster membership is **separate** from `allowedUsers`. Being able to chat with an agent confers no
  approval rights.
- **No agent may approve anything** — not its own action, not a sibling's, not a child's. A parent
  may not approve a child's, or a parent could launder a gated action through a child and the gate
  would bound nothing.
- Approval is recorded by an **authenticated human** against the `ActionRecord` and verified by the
  broker. The agent reporting "they said yes" is model output, and model output is never an
  authorization signal.

Identity is the chat platform's, qualified and immutable — `slack:U0123ABCD`, never a display name
and never an email, both of which a user can change and an attacker can imitate.

---

## 3. Surface 1: chat (the primary human entrypoint)

**Slack is the reference platform.** Google Chat is fully supported and opt-in, and behaves
identically — both are normalized into the same internal message before anything is resolved.

There is **one** `@kage` app for the whole fleet, held by the ChatOps router, **not** by the agent
pods. The pods hold no chat credential at all.

### 3.1 Five ways to address an agent, in strict precedence

| #   | Mode                    | Example                                                          | Inference? |
| --- | ----------------------- | ---------------------------------------------------------------- | ---------- |
| 1   | **Slash command**       | `/kage ask devteam-charlie why is checkout erroring?`            | No         |
| 2   | **Mention with handle** | `@kage cluster-bravo drain node-7`                               | No         |
| 3   | **Thread affinity**     | a bare reply in an existing `@kage` thread                       | No         |
| 4   | **Channel binding**     | a bare message in `#kage-charlie`, bound to `devteam-charlie`    | No         |
| 5   | **Natural language**    | `@kage why is my app crashing on the bravo cluster, charlie ns?` | Yes        |

**Deterministic over inference.** Modes 1–4 always win and spend **zero** inference calls; mode 5 is
the convenience fallback and, on low confidence, **asks rather than guesses**.

**Handles are derived, not a registry.** An agent's handle is its `<tier>-<scope-leaf>` name —
`platform-<project>`, `cluster-admin-<cluster>` (alias `cluster-<cluster>`),
`developer-team-<namespace>` (alias `devteam-<namespace>`) — mapping deterministically to the unique
`Agent` CR the controller already keys on. **No routing table can drift.** The same name keys the
mesh endpoint, so humans and agents address an agent identically.

**Channel binding is the per-audience front door.** `#kage-platform` for the platform team,
`#kage-cluster-bravo` for that cluster's admins, `#kage-charlie` for the `charlie` namespace's
developers. Inside a bound channel a bare message routes deterministically, with no handle and no
inference.

**One slash command, not one per agent — and this is forced, not stylistic.** Slack registers slash
commands statically, per app. A per-agent command would mean editing the app manifest every time a
team gets a namespace. So the fleet's growth lives in the command's _arguments_.

**Routing is not an authorization signal.** The gateway enforces the resolved agent's `allowedUsers`
**before** dispatch, identically in all five modes. Being in a bound channel decides _which_ agent,
never _whether_ this human may use it. A mis-route can only land on an agent the human is already
allowed to reach, still bounded by that agent's scope ceiling, the forbidden set, and the gated
class.

### 3.2 Control-plane commands

These are **not agent conversation.** They are executed by the gateway and the controller against
Kubernetes objects, and the gateway never forwards them to the agent for interpretation.

| Command                               | Effect                                                                             | Authorized by            |
| ------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------ |
| `/kage pause <handle> [reason]`       | Broker refuses new envelopes immediately; in-flight action completes or rolls back | `allowedUsers`           |
| `/kage resume <handle>`               | Clears the brake — **not** `contested` markers, **not** a freeze                   | **approval roster**      |
| `/kage freeze <scope> [reason] [ttl]` | Nothing executes anywhere in scope. Undo and rollback still work                   | **approval roster**      |
| `/kage thaw <freeze-name>`            | Deletes the freeze                                                                 | **approval roster**      |
| `/kage undo <action-id> [reason]`     | Replays the recorded undo plan as a new, classified, journaled action              | `allowedUsers`           |
| `/kage approve <action-id> [note]`    | Releases a parked action to execute                                                | **approval roster only** |
| `/kage reject <action-id> [note]`     | Terminates a parked action                                                         | **approval roster only** |
| `/kage uncontest <action-id>`         | Clears a `contested` marker so the agent may act on that target again              | **approval roster only** |
| `/kage status [handle]`               | Paused/frozen state, budget, pending approvals, last action, counters              | `allowedUsers`           |
| `/kage actions [--since] [--class]`   | Recent `ActionRecord`s for the scope, **with their undo handles**                  | `allowedUsers`           |
| `/kage help`                          | The grammar, and **only** the handles the caller may reach                         | anyone in the workspace  |

---

## 4. Surface 2: `kubectl` and the API (the surface that must never be down)

**Every command in the table above has an equivalent `kubectl` path, and that path is the contract —
the chat form is the convenience.**

This is not a stylistic preference. Slack is the **most likely** thing to be unavailable at the
moment someone needs the brake: a workspace outage, a revoked app token, a dropped connection, or a
router pod that is itself the incident. So `pause`, `freeze`, `thaw`, `undo`, `approve`, `reject`,
`uncontest`, and `status` are Kubernetes-object operations first and chat commands second.

```bash
# pause — Slack down, agent still stoppable
kubectl patch agent developer-team-team-x -n team-x --type=merge \
  -p '{"spec":{"operations":{"paused":true,"pauseReason":"suspect rollout loop"}}}'
```

```bash
# the journal is queryable with the tools already in the room
kubectl get actionrecords -A
```

> A build in which any brake control is reachable only through chat has the dependency backwards.
> A human entrypoint that is down is an inconvenience; a brake that is down would be a defect.

**The brake must also work with the inference stack down** — no dependency on the model, the router,
or the agent pod.

---

## 5. Surface 3: machine triggers (no human in the loop)

Most of what the system does is never asked for by anybody:

| Trigger                    | Source                                                        |
| -------------------------- | ------------------------------------------------------------- |
| Kubernetes warning events  | The API server, watched per-agent, filtered and deduplicated  |
| Alerts                     | Cloud Monitoring / Alertmanager, over the event ingress relay |
| Webhooks                   | GitHub and other external systems                             |
| Mesh calls                 | A parent delegating down, or a child escalating up            |
| Scheduled heartbeats       | Per-tier governance SOPs as the backstop                      |
| The agent's own work queue | Improvements it noticed while doing something else            |

---

## 6. What an interaction actually looks like

### 6.1 The routine case — most interactions

1. A human asks, or a trigger fires.
2. The agent reads its scope, diagnoses, and decides on a concrete change.
3. The agent submits an envelope; the broker classifies it `routine`, generates the undo plan,
   snapshots, executes, verifies, and journals.
4. The agent reports: **what it did, what it observed, and the undo handle.**

No confirmation question. No pre-announcement. No proposal. **Asking permission it does not need is
a defect**, on the same footing as a failed action.

### 6.2 The gated case

1. The broker classifies the action `gated` and writes an `ActionRecord` in `PendingApproval`
   carrying the full plan: intent, targets, the diff it would apply, the classification and the
   **specific rule that gated it**, and the undo plan if one exists. **Nothing executes and nothing
   is partially applied.**
2. The roster is notified in chat with the action ID and a one-line statement of the consequence,
   plus the security review's findings attached to the record.
3. A roster member approves or rejects — from chat buttons, from a typed command, or from `kubectl`.
4. On approval the broker **re-runs its pipeline from the top** — scope, classification, brake, and a
   freshness check against the snapshot — before executing. **An approval is permission, not a
   bypass:** if the world changed while it waited, it re-gates.
5. Unapproved actions expire after a TTL (default 24 h, configurable **shorter** per roster) and are
   recorded as `Expired`. Expiry is not a rejection; the agent may re-raise, which re-classifies from
   the top.

**The agent does not block.** While an action is parked it continues with everything else, including
the ungated parts of the same task. **An agent idling on a pending approval is a defect.**

**On the buttons:** a click proves that a Slack user clicked — nothing more. It is not a credential
and carries no authority the same human typing the command would not have. The broker resolves the
clicker's canonical principal from the verified payload (never from the button's own value),
re-checks the roster, re-checks the TTL, and re-classifies against current state.

### 6.3 The refused case

When the broker returns `forbidden`, the agent **states the refusal plainly**, names which rule and
why, and either escalates to its parent or names the human path. **Reformulating a refused action
into a different shape is a security event, not persistence** — and it is watched by an SLI.

---

## 7. Reference journeys

Drawn from the product-level definition of done. Each must hold **on a live install**.

| #   | Journey                                                                                                                                                                                                               | Delivered in  |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| 1   | A platform operator states an intent and the Platform Agent **completes it** — provisioning a cluster or onboarding a tenant end-to-end, with no manual `kubectl` step and no human approval for the reversible parts | Phase 11      |
| 2   | A Cluster Admin Agent **creates and configures** a namespace and its Developer Team Agent directly, within the Platform Agent's guardrails                                                                            | Phase 11      |
| 3   | A Developer Team Agent **fixes** a workload problem in its namespace unprompted, and is provably unable to affect another namespace                                                                                   | Phase 10 / 13 |
| 4   | The Platform Agent detects an injected drift and **remediates it unprompted**, then reports the change and its undo handle — no PR anywhere in the path                                                               | Phase 13      |
| 5   | A human runs `undo <action-id>` on any executed action and the prior state is restored                                                                                                                                | Phase 10      |
| 6   | A human runs `pause` on a misbehaving agent and it stops acting **immediately, mid-queue** — with inference down                                                                                                      | Phase 9 / 10  |
| 7   | A gated action (delete a PVC) parks and does **not** execute — including when a chat message or injected content insists it is safe and urgent                                                                        | Phase 10      |
| 8   | An attempted action outside an agent's scope is **rejected** — by the broker, and again by admission if the broker is bypassed                                                                                        | Phase 10      |
| 9   | A child escalates a need beyond its scope, the parent acts on it, and the child's authority is **not** widened by having asked                                                                                        | Phase 12      |

---

## 8. Reporting: what the agent owes the human

The agent is accountable for the **decision** and the **report** — the diagnosis, the choice of
change, the verification, and the honesty of the outcome. It is structurally prevented from acting
outside scope, unjournaled, or irreversibly without a human, so the report is where the remaining
trust lives.

Every action report carries: **what was done, why, what was observed on verification, and the undo
handle.** A gated action's report says plainly what will happen on approval and **who was asked**. A
refusal's report quotes the reason verbatim.
