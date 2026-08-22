---
name: self-investigation
description: One hour's audit of kube-agents itself — read the deployed revision's logs, traces, metrics and cluster state, find what is broken or wasteful in the harness rather than in the clusters it manages, and write graded findings to findings.json.
---

# Self-Investigation

The procedure for one run. Work the phases in order; each narrows what the next has to read.

Everything here is read-only. You have no kubectl, no gcloud, no cluster write path. `EVIDENCE`
below means `python3 /opt/selfimprove/scripts/selfimprove_evidence.py`.

## 0. Orient (2 minutes, do not skip)

- Read the brief. Note the deployed revision, the source path, the namespace, the signals in scope.
- `EVIDENCE k8s deployments` and `EVIDENCE k8s pods` — what is actually running, and is any of it
  unhealthy right now.
- Read the ledger summary in the brief. Known findings get re-reported with the same title and
  location, not renamed.
- If the brief says the image is unstamped, every finding you write says so too.
- Budget: a few hundred tool calls for the whole run, and no warning before they are gone. Spend
  them on the phases below in order, and start writing findings.json as soon as phase 1 is done
  (§6) rather than saving it for the end.

## 1. Cast a wide net (cheap counts before expensive reads)

- `EVIDENCE logs-count --hours 24 --severity ERROR` — the shape of the last day in one call.
- `EVIDENCE logs-count --hours 24 --severity ERROR --group-by container` — which component owns it.
- `EVIDENCE logs-count --hours 168 --severity ERROR` — is today unusual or is this the baseline. A
  spike is a finding; a flat line at a high number is a different and often better finding.
- `EVIDENCE k8s events --hours 24` — restarts, evictions, failed mounts, image pull failures.
- `EVIDENCE metrics --filter 'metric.type="kubernetes.io/container/restart_count"'` — restarts the
  events have already aged out of.

Pick the two or three largest buckets. Do not read every log line; you will run out of turns
reading and have nothing left for analysis.

## 2. Read the agent's own files

The pod's own logs are the richest source and they are not on stdout — fluent-bit tails
`/opt/data/logs/*.log` and stamps them `log_source: agent-file`.

- `EVIDENCE logs --agent-files --hours 24 --limit 100`
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"Traceback"'`
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"Permission denied" OR jsonPayload.message:"403"'` —
  the highest-yield query for the `inefficiency` class.
- `EVIDENCE logs --agent-files --query 'jsonPayload.message:"No such file"'` — a tool reaching for a
  path the image does not have.

## 3. Follow one thread to the bottom

Choose the largest bucket and reconstruct what happened, in order, end to end:

- Widen the window around the first occurrence: `EVIDENCE logs --since <ts> --until <ts>`.
- Get the trace for the same window: `EVIDENCE traces --hours N --limit 50`. A span that is 80% of
  a request is a `latency` finding on its own.
- Open the source at the deployed revision and read the code that emitted the line. **This is the
  step that separates a finding from a log excerpt.** A traceback names a file and a line; go read
  it. Confirm the code path can actually be reached the way the evidence says it was.
- State the mechanism in one sentence before you write anything down. If you cannot, you have a
  symptom and not yet a finding — say so, grade it `low`, and record where the next run should look.

## 4. Sweep the classes the errors will not show you

Errors announce themselves. These do not:

- **inefficiency** — count repeats. The same tool call failing the same way forty times is one
  missing permission, not forty errors. A retry loop against a call that can never succeed is the
  canonical instance.
- **latency** — compare traces against what the code intends. A 120s connect timeout that is being
  hit is a different finding from one that is merely configured.
- **responses** — a turn that ended with no message, a reply that is a raw tool schema or a stack
  trace, a session that hit `max_turns` mid-answer.
- **delivery** — `EVIDENCE logs --query 'jsonPayload.message:"home channel" OR jsonPayload.message:"chat.spaces"'`.
  Read the surrounding turn: a delivery failure is usually silent to the user, which is what makes
  it worth finding.
- **forge** — `EVIDENCE logs --query 'jsonPayload.message:"github" AND severity>=WARNING'`. A pull
  request that was created but is wrong counts, and looks like success in the logs.

## 5. Test the hypothesis without touching anything

- Re-read the source path you blamed and check the surrounding conditions, not just the line.
- Look for the negative case in the evidence: if your explanation is right, some other input should
  have produced the opposite outcome. Query for it. Not finding it weakens the finding — say so in
  `confidence`.
- Check `EVIDENCE k8s configmaps` and the Deployment env for the value you assume is set. An
  assumption about configuration is checkable here and is wrong about a third of the time.
- Never construct a test that changes state. There is no state you are permitted to change, but the
  instinct to "just try it" is what the read-only grants exist to stop.

## 6. Write findings.json — early, and again after every finding

The file is the only channel out of the run. Nothing you print is read, and nothing you are still
holding when the turn ends survives it.

- **Write it before you think you are ready.** Write `[]` at the end of phase 1, and rewrite the
  whole array each time you confirm a finding. Your iteration budget is finite, you get no warning
  as it runs out, and a turn cut off part-way is reported as a clean run that found nothing. Two
  confirmed findings on disk beat a better list you never reached.
- One object per distinct problem. Two symptoms of one cause are one finding.
- Titles and locations are stable identity — see SOUL.md §4. Get these right or the counts never
  accumulate and nothing is ever promoted.
- `evidence` is an array of verbatim strings with timestamps, plus the query that produced each.
  Paraphrased evidence is not evidence.
- Grade against the SOUL.md §3 rubric, not against how much work the finding took to find.
- When the investigation is done, confirm the file holds what you mean to hand back, then stop.

## What not to report

- Anything in a cluster under management, or in a user's GitOps repository. That is the Platform
  Agent's work.
- Your own pod's logs and traces. They are filtered by default; do not go looking.
- A `Warning` event that is a normal part of an operation that then succeeded.
- The Slack connect timeout at pod boot — expected, and the relay handles it.
- A style preference in the source with no evidence attached to it.
