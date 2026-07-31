---
name: apply-change
description: Change the cluster or cloud. Build an Action Envelope and submit it to your broker, which classifies it, plans an undo, journals it, executes it and verifies it. This is the only way to write anything.
---

# apply-change — ask the broker; it decides and it acts

You hold **no write credential**. Nothing in this pod can change a cluster or a cloud resource: no
`kubectl apply`, no `gcloud`, no client with a mutating token. That is not a restriction that was
placed on you — it is the shape of the system. Your write authority lives in a separate process
called the **Action Broker**, running beside you under a different identity, and the only way to
reach it is `submit_action`.

This is worth understanding rather than working around, because three things follow from it that no
amount of care on your part could otherwise guarantee:

- **If you are ever manipulated — by a chat message, a log line, a Kubernetes event, a file in a
  repo — the worst that instruction can produce is an envelope.** It is still scope-checked,
  classified, and gated by code that never read the text that fooled you.
- **You cannot talk your way past a gate.** Risk is computed from the target objects and the diff,
  not from how confident you sound. You do not decide your own risk level, and you must never tell a
  human you have.
- **Every change is already undoable at the moment it is reported.** The broker generates the undo
  plan _before_ executing. If it cannot generate one, it does not proceed quietly — it reclassifies
  the action as gated and asks a human.

## When to use this skill

Any time the answer to a problem is "something should change". Diagnosis, root-causing and reading
are ordinary work; this skill is the boundary you cross when reading stops and acting starts.

Do **not** use it to propose or to document. There is no propose verb, no branch, and no pull
request in this path. You are not writing a suggestion for someone to apply later.

## The two tools

| Tool                                                | What it does                                                                                                    |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `plan_action(intent, operations, trigger_source)`   | Everything except the execution: the risk classification, the blast radius, and the undo plan. Changes nothing. |
| `submit_action(intent, operations, trigger_source)` | The same envelope, executed — subject to classification, the brake, gating and verification.                    |

Both take the same three things:

- **`intent`** — one sentence, written for a human, saying what this change is _for_. It is stored
  on the record and it is what a person reads at 3am. "Raise the memory limit on `api` in `team-x`
  so it stops OOMKilling" is an intent. "Apply the manifest" is not.
- **`operations`** — the list of concrete changes. Each has an `op` — one of `create`, `apply`,
  `patch`, `delete`, `scale` — and exactly one of `target`, `targetSelector`, or `cloudTarget`.
- **`trigger_source`** — what caused _you_ to act. Not what the change does; what put you in motion.
  It is required, and it is one of exactly seven words:

  | You are acting because…                     | `trigger_source` |
  | ------------------------------------------- | ---------------- |
  | a human asked you, in conversation          | `chat`           |
  | a human asked you to reverse something      | `undo`           |
  | you saw an object change and decided to act | `watch`          |
  | an alert fired                              | `alert`          |
  | a schedule ran you                          | `cron`           |
  | another agent asked you to do this          | `delegation`     |
  | something was escalated to you              | `escalation`     |

  The first two mean **a human asked**. The other five mean **you decided**. That line is the one
  this field exists to record: it is what the platform's autonomy reporting counts, and it is the
  answer to "how much of what happened here did the agents choose?" — a question nobody can
  reconstruct afterwards from the change itself. Say which is actually true. There is no penalty for
  `watch`, and an autonomous action filed as `chat` is a false statement about a human.

  Optionally, `trigger_ref` (what to look at: the alert name, the object, the action id you were
  delegated) and `trigger_detail` (one line about the trigger — the reason for the _change_ is
  `intent`).

`submit_action` also takes `rationale` (optional reasoning; recorded, and never treated as a risk
signal) and `require_approval` (ask for a human even when the classifier would not).

There is **no parameter for your tier, your scope, your risk class, or whether the change is
approved.** Their absence is deliberate and is the security property, not an oversight. The broker
derives who you are from the authenticated connection, and derives what you may touch from that. An
envelope that named its own scope would be a request to be trusted about the one thing that must not
be taken on trust — so the field does not exist to be filled in.

`require_approval` is the single direction you may push, and it only goes one way: you can ask for
**more** gating than the classifier decided. You can never ask for less.

## How to use it

**1. Plan first, whenever you are not certain.** `plan_action` costs nothing and tells you what will
happen — including whether the change will park for approval, which is usually what you want to know
before you promise a human anything.

**2. Show the classification to the human before you submit, if one is watching.** The class and the
blast radius are the useful part. "This is classified `gated` because it deletes a PersistentVolume
in a production namespace" is a sentence a human can act on.

**3. Submit, and read the answer properly.** There are three outcomes and they are not
interchangeable:

- **Submitted.** The change happened, or is happening. You get an `actionId`. Report it.
- **Parked for approval.** Nothing has changed yet and nothing will until a human approves. Say
  exactly that. Do not describe a parked action in the past tense.
- **Refused.** The reply begins with `REFUSED`. Something — scope, the brake, a policy — said no.
  Report the reason as given. Do not retry it in a different shape hoping for a different answer; a
  refusal is a decision, not an obstacle, and re-submitting a refused action with the wording
  changed is the behaviour this system is built to make pointless.

**4. Report what actually happened.** The one genuinely unacceptable outcome is telling a human the
cluster changed when it did not. If you are unsure what the reply meant, say what the reply said.

## What never appears in this path

- No `git` branch, commit, push, or pull request. The old GitOps proposal path is not what this is.
- No `kubectl apply`, `kubectl delete`, `kubectl patch`, `kubectl scale`, or `gcloud` command. If
  you find yourself reaching for one, the change belongs in an envelope.
- No tool that pauses, resumes, freezes, approves, rejects, or un-contests anything. Those controls
  exist for humans to use **on** you, and an agent that could release its own gated action would
  make the gate decorative. If you need one of them used, ask a human.

## Worked example

An OOMKilling workload in a namespace you are responsible for:

```
plan_action(
  intent="Raise the memory limit on the api Deployment in team-x so it stops OOMKilling",
  trigger_source="alert",
  trigger_ref="KubePodCrashLooping/team-x/api",
  operations=[{
    "op": "patch",
    "target": {"group": "apps", "version": "v1", "kind": "Deployment",
               "namespace": "team-x", "name": "api"},
    "patch": {"spec": {"template": {"spec": {"containers": [
      {"name": "api", "resources": {"limits": {"memory": "1Gi"}}}]}}}}
  }],
)
```

The example is triggered by an alert, so `trigger_source` is `alert` and not `chat` — nobody asked;
the alert fired and you decided. If a human had said "the api pods are OOMKilling, fix it", the same
change would be `chat`. The operations are identical and the origin is not, which is exactly why the
field is a parameter rather than something the tool guesses.

Read the classification. If it is what you expected, call `submit_action` with the same arguments —
including the same `trigger_source`, since it is the same act — then report the `actionId` and the
undo handle to whoever asked.
