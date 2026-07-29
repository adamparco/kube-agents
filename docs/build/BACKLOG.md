# BACKLOG — the harness inbox

A place a human can drop a finding, a task, an idea or a question **at any time**, including while
the harness is mid-unit, without disturbing what is running.

`LEDGER.md` is harness state: the harness reads it first and writes it last, every session, and a
human editing it mid-unit races the write. This file is the opposite — **humans write it, the
harness only drains it** — so an append here is always safe. Nothing in this file changes what the
current unit is doing. It is read at the next **ORIENT**, which is the harness's own planning
moment, and scheduled from there.

That delay is the feature. A finding that redirects work the instant it is written is a finding that
lands mid-IMPLEMENT, when the harness has the least context to place it well and the most reason to
place it badly.

---

## How to add an item

Append a block at the **bottom of `## Inbox`**. That is the whole protocol. Do not assign an ID, do
not touch any other section, do not renumber anything — appending at the end cannot conflict with a
harness write anywhere else in the file.

```markdown
### <one-line title, imperative if it is a task>

- **Kind:** finding | task | question | idea
- **Where:** <file, § or component if you know it; "unknown" is fine>
- **Why it matters:** <one or two sentences — this is what the harness schedules against>
- **Priority:** normal | now
- **Added:** YYYY-MM-DD
```

Three fields are load-bearing: the **title**, **Why it matters**, and **Added**. `Kind`, `Where` and
`Priority` may be omitted or left as `unknown` — the harness works out what it can and asks about
what it cannot.

`Added` is required because it is what makes the gate below real. `dev/tests/invariants-gate.py`
compares it against `Last drained` and fails the build if an item was in the inbox when the harness
last looked and is still sitting there. Without a date on the item, "the harness read this and
walked past it" is indistinguishable from "a human added this ten seconds ago", and the rule that
nothing survives two ORIENTs becomes unenforceable.

`Priority: now` means "make this the next unit". Use it sparingly — it displaces planned work, and
the harness will say what it displaced. Anything genuinely urgent that must interrupt a _running_
unit is not a backlog item; say it in the session.

---

## What the harness does with it

Drained at **ORIENT only** (`harness-run` §1), before SELECT, never mid-unit. Every inbox item is
resolved in the same ORIENT that reads it — scheduled, refused with an argument, or escalated. **No
item survives two ORIENTs sitting in the inbox**; an inbox that accumulates is a second, quieter
ledger nobody reads, which is the failure this file exists to avoid.

Scheduling means choosing a destination, in this order of preference:

| Destination                                    | When                                                    |
| ---------------------------------------------- | ------------------------------------------------------- |
| An existing `todo` task in the phase breakdown | It is already someone's job; the item sharpens the task |
| A new task in the **current** phase breakdown  | It belongs to work in flight                            |
| A task in a **later** phase (07 §2)            | It depends on something that phase builds               |
| A lesson in `LESSONS.md`                       | It is a mistake the harness has now made twice          |
| The next **improvement pass** queue            | It is a check, threshold, skill or spec change          |
| A **halt**                                     | It contradicts a spec, or names a security regression   |

Two rules on top of the table:

- **A finding that names a live security regression is not queued.** It becomes the next unit, or a
  halt if it cannot be fixed without a human ruling. `Priority: normal` does not override this — the
  harness classifies severity itself and says so.
- **A refusal is an argument, not a deletion.** An item the harness will not do moves to `## Refused`
  with the reason, and stays there. Refusals are re-read at every improvement pass.

Then the item **moves out of the inbox** into `## Scheduled`, with an ID, the destination, and the
date. IDs are `B-nnn`, assigned in the order drained, and are never reused — a scheduled item that
turns out to be wrong is closed with a reason, not recycled.

---

## Inbox

**Last drained:** 2026-07-28

_(empty — append new items here, below this line)_

---

## Scheduled

| ID  | Title | Kind | Scheduled into | On  |
| --- | ----- | ---- | -------------- | --- |

_(empty)_

---

## Refused

| ID  | Title | Why not | On  |
| --- | ----- | ------- | --- |

_(empty)_

---

## Done

| ID  | Title | Landed as | On  |
| --- | ----- | --------- | --- |

_(empty)_
