---
name: fleet-audit-reports
description: Answer a question about a past autonomous fleet audit from the on-pod report store — what a stream last found, how many criticals are open, what changed between two runs, which clusters were skipped or only partly covered, and when each stream last ran.
---

# fleet-audit-reports — Reading What the Audits Found

Every audit run keeps what it published, on the volume, beside the agent. This skill answers
questions off those files. It never runs an audit and never publishes one — the `fleet-audit` skill
owns both ends of that lifecycle.

## The store

`/opt/data/fleet-audit/reports/<audit-id>/`, one directory per stream:

- `latest.json` — the newest run's envelope.
- `runs/<YYYYMMDDThhmmss.ffffffZ>.json` — one envelope per run, newest 14 kept. The only
  run-over-run history that exists anywhere: the ledger issue rewrites itself in place.
- `started.json` — present only while a run is in flight. It is the run lock, so its presence and
  its age are how a stream reads as running, or as dead past a two-hour ceiling.

An envelope carries `audit_id`, `finished_at`, `status`, `issue_number`, `issue_url`, `partial`,
`coverage_gaps`, `collect_s`, `inspect_s`, `publish_s`, `new_ids`, `resolved_ids`, `current_ids`,
`id_scheme`, and `document`.

- `document` is the whole validated findings document — post-degradation, un-clipped, so it holds
  findings the issue body had no room to print.
- `current_ids` is the **rendered** id set, exactly what the body's hidden block published. Derive
  the full set from `document`.

## Query it; do not read it

**Never open `latest.json` with a file tool.** It embeds `document`, which passes 60,000 characters
on a finding-heavy stream — times eight streams, times fourteen runs. Answering "how many criticals
are open?" that way spends tens of thousands of tokens on an integer.

`python3 ./skills/fleet-audit-reports/scripts/report_query.py <subcommand>` prints one small JSON
object per call. Naming the interpreter is load-bearing here for the reason
[`fleet-audit/SKILL.md`](../fleet-audit/SKILL.md#step-1--start) gives: run by path, the gateway's
lifecycle guard reads the script's own text and fails closed on any path-shaped token in it that
names a real directory. This script happens to carry none today, so do not read a working bare-path
invocation as licence to drop the `python3` — one `sys.path.append("/opt/…")` is all it would take.

| Subcommand                          | Answers                                                   |
| ----------------------------------- | --------------------------------------------------------- |
| `streams`                           | one row per stream: last run, status, counts, liveness    |
| `show <stream>`                     | one run's envelope **without** `document`                 |
| `findings <stream>`                 | finding id, severity, title, cluster, check — filterable  |
| `finding <stream> <id>`             | one finding in full; the only call that returns prose     |
| `checks <stream>`                   | the command behind each check that ran, plus exclusions   |
| `diff <stream> [--from S] [--to S]` | ids and titles added and resolved between two runs        |
| `runs <stream>`                     | the stamps the ring holds, so a `diff` can name real ones |

- Exit 0 answered the question. Exit 2 could not, and stdout still holds one JSON object whose
  `error` says why — absent store, absent stream, absent stamp, a file that would not parse. Every
  answer carries an `error` key, null on success.
- `--run` takes a stamp from `runs`, with or without the `.json`. Default is the newest run.
- `--severity`, `--cluster` and `--check` on `findings`, and `--cluster` and `--check` on `checks`,
  are exact matches, case-insensitive.
- `findings`, `checks` and `diff` cap at 100 rows, raisable with `--limit`. `findings` and `checks`
  report `matched`, `returned` and `truncated`; `diff` caps `added` and `resolved` independently
  and reports `added_total`, `resolved_total`, `unchanged` and `truncated` instead — so a `diff`
  whose `added` holds 100 rows may have had more, and `added_total` is the number to quote.
  Findings sort severity-first, so a cap only ever drops the least severe; `checks` keeps the
  document's order, so a capped answer lines up with the issue's table rather than a re-sort of it.
- `--root` overrides the store root. In the pod, leave it alone.

## The four questions this gets asked

**"What did last night's compliance audit find?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py show compliance-audit
python3 ./skills/fleet-audit-reports/scripts/report_query.py findings compliance-audit --severity critical
```

`show` gives `status`, `findings`, `critical`, `partial`, the delta counts and `issue_url`; name the
criticals from `findings`. Always hand back the issue URL — the store is where you read, the ledger
is where a human acts.

**"What changed since the last run?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py diff compliance-audit
```

Defaults to the newest two runs and returns ids and titles under `added` and `resolved`. For a wider
span, list the ring first and name two stamps:

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py runs compliance-audit
python3 ./skills/fleet-audit-reports/scripts/report_query.py diff compliance-audit --from <stamp> --to <stamp>
```

**"Tell me about that finding."**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py finding compliance-audit netpol-missing-payments
```

The evidence command and excerpt, the impact, and all three `recommendation` fields. This is the
expensive call, so make it for the finding that was asked about and not for the list.

**"The issue says the commands were omitted for space — what were they?"**

```bash
python3 ./skills/fleet-audit-reports/scripts/report_query.py checks obtainability-audit --cluster prod-us-east
```

The ledger's evidence table is last in line for the body budget, so on a finding-heavy run it is
dropped whole and a notice points the reader here. `checks` is what that notice promises: one row
per check the run says it performed, with the command that performed it, plus every
`checks_not_applicable` exclusion and its reason. Filter by cluster or check rather than asking for
all of them — a 16-cluster stream carries upwards of 150.

Fleet-wide, `streams` is the whole answer: one row per stream with its liveness, so "when did each
last run" and "is anything stuck" come back in one call. Coverage questions read `show`'s
`coverage_gaps`, which names each cluster that was skipped or short of its checks; `streams` carries
only the count, because eight streams of gap prose is the unbounded shape these subcommands exist to
avoid.

## Two things the store is not

- **Not the live issue.** It is the last _published_ state. A finding a human closed by hand since
  that run, or a `/remediate` posted since, is not in it until the next run rewrites the store. When
  the question is about the issue, read the issue.
- **Not written for every invocation.** Only the exit-0 publish path writes — never `--dry-run`,
  never a run that exited 2, never `remediate`. The write is best-effort, and a failed write deletes
  `latest.json` rather than leave a superseded envelope reading as current. **A missing `latest.json`
  means unknown, not clean** — say the store has no record, and read the ledger issue.

## Red lines

- **Never report an absent, unreadable or `never` stream as a clean fleet.** "I could not look" and
  "nothing is wrong" are different answers. The `error` key and the `liveness` value exist to keep
  them apart; pass them through to the user.
- **Never read `document` whole** to answer a question one subcommand already answers.
- **Never run, publish, or remediate from here.** Dispatching a stream, rewriting a ledger and
  opening remediation pull requests all belong to `fleet-audit`
  ([Running a stream on demand](../fleet-audit/SKILL.md#running-a-stream-on-demand)).
- **Never quote a count the store did not give you.** The counts are keys; re-deriving one by hand
  from prose is how a stale number reaches a user with the authority of a file read.
