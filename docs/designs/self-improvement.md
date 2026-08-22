# Self-Improvement — kube-agents as its own subject

> **STATUS — proposal; nothing here is implemented.** No script, chart template, values key or
> cron entry described below exists on `main`. Identifiers are this document's proposals, not the
> repository's vocabulary. Where the design depends on something that does exist, the file and line
> are cited; where it depends on something that does not, the gap is called out rather than assumed
> away.

**Scope:** A disabled-by-default hourly investigation of kube-agents itself — its source, its
harness, and the installation it is running in — which grades what it finds and, above a
configurable bar, opens a pull request against this repository.

**Owns:** the isolation boundary between that investigation and the agent it observes; the evidence
sources it may read; the severity and frequency gate; and the GitHub identity it mints tokens with.

---

## 1. The distinction this turns on

kube-agents already runs continuous audits. The roster in
[`agents/platform/cron/jobs.json`](../../agents/platform/cron/jobs.json) schedules a compliance
audit, an obtainability audit, a fleet-wide cost analysis and their siblings; each drives the
`fleet-audit` skill, and each produces findings about **the clusters under management**. The agent
is the observer and the customer's infrastructure is the observed.

This feature inverts that. The observer is the same harness; the observed is kube-agents — the
Python under `agents/`, the operator under `k8s-operator/`, the chart under `charts/`, the Hermes
harness the image is built on, and the behaviour all of it exhibits in the pod it is running in
right now. Nothing about a customer's cluster is in scope, and nothing the loop concludes can reach
one.

|                       | Fleet audits (shipping)                                | Self-improvement (this document)                                             |
| --------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------- |
| Subject               | the managed clusters and the customer's GitOps repo    | kube-agents' own source, harness, and running installation                   |
| Evidence              | the live GKE and Kubernetes APIs, billing, GitOps tree | the agent's own logs, traces, stores, CRs, and this repository's history     |
| Output goes to        | the customer's GitOps repository                       | `gke-labs/kube-agents`, and `nousresearch/hermes-agent` for harness findings |
| Who reviews it        | the cluster owner                                      | this repository's maintainers                                                |
| Worst case from a bug | a wrong change proposed against a customer's cluster   | a pull request nobody merges                                                 |
| Cadence               | the roster in `agents/platform/cron/jobs.json`         | hourly, off unless switched on                                               |
| Identity              | `platform-agent-scope`, the GitOps repo                | a separate GitHub App on separate repositories (§6)                          |

Two consequences follow immediately, and the rest of the design is mostly working them out.

**The reviewer is not the operator.** A fleet-audit finding lands in front of the person who runs
the cluster it is about. A self-improvement finding lands in front of kube-agents maintainers, who
may have no relationship with the install that produced it. An install therefore cannot be opted
into publishing on its behalf: the default mode files nothing anywhere, and reaching the upstream
repository takes a GitHub App the operator has to install deliberately.

**A finding is not an incident.** The loop never pages, never posts to the home channel unless
asked to, and never opens a kanban card on the board the Platform Agent works from. Its output is
a durable artifact read on someone else's schedule. Delivery paths that exist to interrupt a human
are the wrong shape for it, and reusing them would put self-referential noise into the channel
where cluster incidents arrive.

## 2. What "the code base that is currently deployed" means

The investigation begins by cloning the source the pod is actually running. Getting that wrong
makes every downstream conclusion unfalsifiable — a finding written against `main` about a pod
running a three-week-old image describes code that is not there.

**Nothing inside the image names its own commit.**
[`deploy/docker/Dockerfile`](../../deploy/docker/Dockerfile) declares build arguments
(`HERMES_AGENT_TAG`, `HERMES_AGENT_IMAGE`, `ENVOY_VERSION`, `GOLANG_VERSION`) and no `LABEL`; there
is no version `ENV` and no build-info file, and `.dockerignore` excludes `.git`, so the build
context never contained the metadata in the first place. The operator states the consequence
outright — `manifest_helpers.go:80` omits `app.kubernetes.io/version` because "there is no
build-time version to report" — and `AgentStatus` records phase, address, replicas and endpoints
but no image, tag, digest or version.

**The registry knows, though, and that is enough to work today.** Both publish workflows push
`platform-agent:${{ github.sha }}` alongside `:latest` on every push to `main`, and release images
are retagged rather than rebuilt (`docker buildx imagetools create` in `scripts/release/common.sh`),
so `:X.Y.Z` and `:<sha>` resolve to the same manifest digest and that commit carries the matching
git tag. A runner can therefore resolve its own image digest, list the tags sharing it, and take
the 40-hex one. The cost is a registry read grant the runner needs for nothing else, and one case
that still resolves to nothing: the dev-rebuild path tags from `IMAGE_TAG` in `vars.sh`, which maps
to no commit at all.

**Stamping the revision is the better fix and the only edit this feature wants outside its own
files**: an `ARG GIT_SHA`, an `org.opencontainers.image.revision` label, and a `build-info.json` in
the image. It removes the registry grant, covers the dev-rebuild case, and is worth doing on its
own merits. The design does not block on it — the digest path above is the fallback — but a runner
that has to fall back should say so in every finding it files.

**Either way the runner reads the answer off itself.** It is scheduled with the same image
reference the agent Deployment is running, so whatever identifies its own filesystem identifies the
deployed code by construction, with no privileged read of the agent's container. Before anything
else it compares its own image reference against the live Deployment's — listing Deployments is in
the `view` role — and aborts on a mismatch, which means the agent was rolled and the CronJob was
not. The operator solves the same problem the same way, reading its own Pod from the API to set
`OPERATOR_IMAGE`, so this is a pattern the codebase already has.

**The harness is not a clone, and assuming it is would be the subtler mistake.** Hermes is not
vendored and not checked out: it arrives as the prebuilt `docker.io/nousresearch/hermes-agent` base
image pinned in [`tags.env`](../../tags.env), and the build then rewrites its Python source in place
at `/opt/hermes` — `deploy/docker/patches/*.py` make anchored literal and AST edits through
`patchlib.py`, and a dozen of them are applied in a mandatory order. A clone of the upstream tag is
therefore _not_ the harness that is executing; it is the harness before this repository got to it.

The executing harness is `/opt/hermes` on the runner's own filesystem, patches already applied, and
that is what the runner reads. The upstream clone is still useful, but for a different job: diffed
against `/opt/hermes` it shows exactly which behaviour is this repository's and which is upstream's,
which is the difference between a finding that belongs in `deploy/docker/patches/` and one that
belongs to Nous Research. Several signals in §4 originate in the harness — an agentic loop taking a
turn it did not need is usually the harness's scheduling, not a kube-agents skill — so getting this
attribution right is most of the value of looking at the harness at all.

## 3. What it is allowed to look at

The rule the user set is "inspect, but do not modify". Making it mechanically true rather than a
matter of the agent's good behaviour means being specific about each source and, where a source is
unreachable without breaking the rule, saying so instead of quietly reaching for it.

### 3.1 Logs

The agent writes its logs to files on the data volume, not to stdout, which is why `kubectl logs`
on the agent container shows so little. `buildFluentBitConfigMap` in
[`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)
tails `/opt/data/logs/*.log`, stamps each record with `log_source: agent-file`, and prints it to
stdout as `json_lines` — from where GKE's node agent ships it to Cloud Logging.

That is the whole log-access story for an isolated runner: it queries Cloud Logging with
`roles/logging.viewer`, filtered to the install's namespace and `jsonPayload.log_source`. It never
mounts the data volume, never execs into the pod, and gets the operator's, the credential proxy's
and the minter's container logs from the same place. The sidecar is already deployed on every agent
pod, so no change to the observed system is needed to make its logs readable.

### 3.2 Traces and metrics

`hermes_otel` is enabled in the profile and its endpoint is pointed at the collector by the
entrypoint. Latency findings — the third signal class — come from spans, not from log timestamps:
a span tree shows which tool call in a turn consumed the wall clock, which a log line cannot.
`roles/cloudtrace.user` and `roles/monitoring.viewer` on the project are enough, and both are read
roles.

### 3.3 The Kubernetes API

`view` on the release namespace, bound with a `RoleBinding` rather than a `ClusterRoleBinding`, plus
`get`/`list`/`watch` on `platformagents.kubeagents.x-k8s.io` and `agentplugins.kubeagents.x-k8s.io`.
That covers the CR and its `.status`, the Deployment and its env, the ConfigMaps, events, and pod
state — enough to find a container that has been restarting, an env var that never made it out of
the CR, or a condition that has been `False` for a week.

`view` deliberately excludes Secrets. Three verbs are excluded on top of it and are worth naming so
a later widening has to argue against something: `pods/exec` and `pods/attach`, because exec into
the agent container is arbitrary code execution as the agent and no amount of intent makes it a
read; and `pods/portforward`, for the same reason one step removed.

Kubernetes RBAC is doing more work here than it does for the agent, and the reason is worth stating.
The agent's KSA permissions are not the binding constraint on a _managed_ cluster, because the agent
authenticates to those clusters as its Google service account via
`gcloud container clusters get-credentials` — the KSA grant governs the install's own namespace and
little else. The runner has no such escape hatch: its GSA holds logging, trace and monitoring read
roles and no GKE roles at all, so `get-credentials` fails for every cluster in the project,
including the one it is running on. There is no managed cluster it can reach by any path.

### 3.4 The SQLite stores

This is the source the loop most wants and the one it cannot have cheaply. Session history, the
kanban board and the OTel live store are SQLite files on the agent's data volume, and they hold
the evidence for the signal classes that logs answer worst — how many turns a task took, which
cards ended `blocked` and never recovered, which cron deliveries recorded a `last_delivery_error`.

Three ways to reach them, and the design takes the third:

- **Mount the volume read-only from the runner.** Not available. The operator creates the claim
  `ReadWriteOnce` (`platformagent_manifests.go:125`, and `defaultAccessModes` at `:141`), so a
  second pod on a second node cannot attach it at all, and a second pod on the same node attaching
  a live SQLite database with an active WAL is how these files get corrupted rather than read.

- **Exec into the agent and query in place.** Rejected in §3.3, and it is also a write: opening a
  WAL database read-write is a modification of the file.

- **Snapshot the volume and mount the restore.** A `VolumeSnapshot` of the claim, restored into a
  throwaway claim the Job mounts, gives a consistent point-in-time copy that can be opened with
  `immutable=1` without the live file ever being touched. It is the only one of the three that is
  genuinely non-invasive to the observed system.

It is also the one place the loop creates cluster objects, which is why it is behind its own flag,
off even when the feature is on, and documented as such. An install that leaves it off gets a
runner whose findings about session and board behaviour are inferred from logs and spans; an
install that turns it on has consented to two objects per run, in its own namespace, garbage
collected at the end of the run.

### 3.5 The repository

The clones from §2, plus the public GitHub API: recent commits, open issues, open pull requests. A
finding that duplicates open work is noise, and the check is the same one `AGENTS.md` requires of a
human contributor before starting a task. Read access here is anonymous; the credential in §6 is
for writing.

## 4. The signals

The seven classes below are the ones the loop looks for. The value of the table is the middle
column: a signal with no stated evidence source is an invitation to speculate, and a
self-improvement loop that speculates produces pull requests that waste maintainer attention faster
than it saves it.

| #   | Signal                                      | Where the evidence is                                                                | What a finding must show                                                                    |
| --- | ------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| 1   | Errors                                      | `log_source: agent-file` at ERROR, container stderr, CR `.status` conditions, events | The stack or message, the code path in the cloned revision, and how often it fired          |
| 2   | Inefficiency — missing permission or tool   | Denials from `command_policy.py`, `gcloud`/`kubectl` 403s, `command not found`       | The refused call, whether the refusal was correct, and the cost of the retry loop it caused |
| 2   | Inefficiency — unneeded loop or turn        | Span trees; turn counts per session; repeated identical tool calls in one session    | The turn that added nothing, and what in the prompt or harness produced it                  |
| 3   | Latency                                     | Span durations, p50/p95 per tool and per skill, cron run durations                   | The span that dominates, compared against the same span in earlier runs                     |
| 4   | Wrong, inaccurate or missing user responses | Sessions ending without a delivered reply; kanban cards `blocked` with `result` NULL | The session, what was asked, what was returned, and where the reply was lost                |
| 5   | Failed chat delivery                        | `last_delivery_error` on cron jobs; the delivery paths in the credential proxy       | The target that failed, the platform, and whether the target was resolvable at all          |
| 6   | Failed issue or PR creation                 | Minty refusals in the proxy's own log; `gh` exit codes; the resolver's reason codes  | The HTTP status, the scope requested, and whether the App had the permission                |
| 7   | Anything else                               | any of the above                                                                     | The same bar: evidence first, then a claim                                                  |

Two of these are worth a note because the repository already knows they are hard. Signal 5 has a
known shape where an alert is delivered on one platform and its report on another; signal 6 has a
known shape where a scopeless repository produces an HTTP 500 from the minter rather than a
recognisable refusal. A loop that rediscovers a known-open issue should say so and link it, which
is what the duplicate check in §3.5 is for.

## 5. Architecture

### 5.1 Three shapes that do not work

The isolation requirement rules out the obvious placements, and each rules itself out for a
different reason worth recording.

**A cron job in the Platform Agent's own profile.** This is how every existing audit runs, and it
would be about twenty lines: an entry in `agents/platform/cron/jobs.json` and a skill directory.
It fails the requirement completely. The job would run as the agent's service account, with the
agent's kubeconfig, the agent's GitHub token and the agent's model budget, on the agent's data
volume. An investigation into why the agent is slow that competes with the agent for its own
resources is not an investigation, and the finding "the agent's credentials are over-scoped" would
be written by something holding those credentials.

It also cannot be switched off. Nothing in the `PlatformAgent` CRD or in `values.yaml` adds, edits
or disables a cron job: the roster is baked into the image and merged onto the volume by
`profile_scaffold.py` at pod startup, and the merge rule is that the image wins every key it ships.
An entry in that file is on for every install that pulls the image, which is the opposite of the
disabled-by-default requirement, and the only runtime route to changing it is the `cronjob` tool.

**An `AgentPlugin`.** The right instinct — [`agentplugins/README.md`](../../agentplugins/README.md)
describes exactly the kind of out-of-tree capability this is, and the CR mounts an OCI image into a
profile without touching `deploy/` or `agents/`. But the isolation an AgentPlugin provides is of
_source_, not of _runtime_: the code still executes inside the agent's process, under the agent's
identity, on the agent's volume, so every objection above still holds. It also cannot schedule
anything. The operator mounts the image and symlinks it into the profile; nothing merges a
plugin-supplied `cron/jobs.json` into the profile's cron store, so an `AgentPlugin` has no way to
run hourly without a new entrypoint step and a new mode on `profile_scaffold.py` — a change to the
shared boot path, which is what this feature is trying to avoid.

**A second `PlatformAgent` custom resource.** This would get a separate pod with a separate
identity and let the operator do the work. It is blocked by admission:
[`platformagent_webhook.go:130-154`](../../k8s-operator/internal/webhook/platformagent_webhook.go)
rejects a second `PlatformAgent` in the cluster with "only one PlatformAgent is allowed per
cluster". Relaxing a validating webhook so an optional, off-by-default feature can exist is the
wrong trade; the singleton rule is protecting something real about leader election and volume
ownership.

### 5.2 What it is

A Kubernetes `CronJob`, rendered by the chart only when the feature is switched on, running the
same agent image with a private Hermes home on an `emptyDir`.

```
CronJob  kube-agents-selfimprove          schedule 0 * * * *, suspended unless enabled
  └── Job (concurrencyPolicy: Forbid, backoffLimit: 0, activeDeadlineSeconds)
        ├── initContainer credential-proxy   restartPolicy: Always  ← native sidecar
        └── container     runner             the agent image, HERMES_HOME=/home/selfimprove
```

The run is: scaffold a private profile onto the `emptyDir`; clone the two repositories at the
revisions from §2; execute one agent turn; write the ledger; exit. There is no gateway, no chat
platform, no dashboard and no PVC. `hermes cron tick` is the invocation, because it is the one
path in this repository demonstrated to run an agent turn to completion without a gateway —
[`agents/chat/scripts/profile_cron_tick.py`](../../agents/chat/scripts/profile_cron_tick.py)
spawns exactly that, and its docstring is explicit that this is the same path a manual tick takes.
The Kubernetes schedule supplies the timing; the profile's cron store holds one job, due always,
so the tick always fires.

**The credential proxy must be a native sidecar**, declared as an `initContainer` with
`restartPolicy: Always`, not as a second `containers` entry. A long-running container in an
ordinary `containers` list never exits, so the Job never completes and `concurrencyPolicy: Forbid`
blocks every subsequent run forever. This is a one-word difference in the manifest with a failure
mode that takes a day to diagnose.

`concurrencyPolicy: Forbid` is also the whole of the mutual-exclusion story. There is no lease,
because there is no shared mutable state to serialise access to — the design's ledger (§7) is the
only thing the runner writes, and `Forbid` guarantees one writer.

This would be the repository's first `CronJob`. The only `batch/v1` object the chart renders today
is a Helm pre-delete `Job`, so there is no house convention to follow for one and this design is
setting it. The convention it does follow is `githubMinter.enabled`: a chart-only standalone
workload, guarded on a values key, that the operator knows nothing about and that renders nothing
when the key is false.

### 5.3 The isolation ledger

| Resource                | Shared with the agent?  | Why                                                                                              |
| ----------------------- | ----------------------- | ------------------------------------------------------------------------------------------------ |
| Pod / process           | no                      | separate `CronJob`; the agent is unaware of it                                                   |
| Container image         | **yes**                 | deliberate: it is how the runner knows the deployed revision (§2), and it adds no image to pin   |
| Kubernetes SA           | no                      | `kubeagents-selfimprove`, `view` on one namespace                                                |
| Google SA               | no                      | `kubeagents-selfimprove-gsa`, read roles on logging, trace and monitoring                        |
| Data volume             | no                      | `emptyDir`; the agent's `ReadWriteOnce` claim is never mounted                                   |
| GitHub App and identity | no                      | a separate App, a separate minter, a separate scope (§6)                                         |
| Credential proxy        | separate instance       | the same script and image, its own process, pointed at its own minter                            |
| Minter                  | separate Deployment     | `github-token-minter-selfimprove`; `templates/github-minter.yaml` is not edited                  |
| Model endpoint          | **yes** by default      | the in-cluster LiteLLM Service; duplicating a gateway buys nothing. Overridable for budget split |
| Chat platforms          | no                      | the runner has no Slack or Google Chat credential and no home channel                            |
| Kanban board            | no                      | findings go to the ledger, not to the board the agent works from                                 |
| Telemetry               | write, own service name | traces are stamped `kube-agents-selfimprove` so the loop's own cost is separable                 |

Only two entries are `yes`, and both are argued for rather than inherited. Everything the feature
adds is rendered by one new chart template, `templates/self-improvement.yaml`, guarded on one
values key. With the flag off the template renders nothing, and the install is byte-identical to
one from a chart that never had the feature.

**One label must not be copied.** The minter's ingress policy admits pods carrying
`kubeagents.x-k8s.io/has-credential-proxy: "true"`
([`github-minter.yaml:199-206`](../../charts/kube-agents/templates/github-minter.yaml)), and the
operator stamps that label on agent pods (`platformagent_manifests.go:1930`). The runner pod runs a
credential proxy and so invites the label by analogy — and carrying it would let the runner reach
the _platform_ minter and mint tokens for the customer's GitOps repository, silently undoing §6.
The runner is labelled `kubeagents.x-k8s.io/selfimprove: "true"` instead, its own minter admits only
that, and the platform minter continues to admit only the other. Neither can reach the other's
broker.

### 5.4 Read-only, in three layers

- **RBAC.** `view` on one namespace, no Secrets, no exec. Nothing in the grant can mutate the
  agent, and nothing in it reaches another namespace or another cluster.
- **`command_policy.py`.** The runner's credential proxy runs with
  `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` left at its default, so `kubectl` and `gcloud` are held to a
  default-deny argv allowlist. The module's own docstring is worth heeding — it is the only thing
  enforcing the posture, so a false allow is the whole control, not a redundant check. The kill
  switch is already treated as such: it is in the operator's `SensitiveEnvVars`, rejected by the
  webhook and separately dropped at reconcile, because the webhook's `failurePolicy` is `Ignore`.
  The runner inherits that protection by using the same proxy, and nothing in this design should
  give it a way to set the variable that the agent does not have.
- **No mutating credential exists.** The runner holds no GitOps token, so there is no cluster it
  could push a manifest to even if the first two layers failed. `git` and `gh` are outside
  `command_policy.py` on purpose, and in the agent's case the workspace lease governs them; the
  runner's equivalent is that its token is scoped to two repositories that contain no
  infrastructure.

The exception, stated plainly rather than buried: the runner writes its ledger. That is one
`ConfigMap` in the install's own namespace, granted by `resourceNames` on a `Role` so the grant
cannot reach a second object, and it is the runner's own bookkeeping rather than any part of the
system under observation. Where §3.4's snapshot path is enabled, add two short-lived objects per
run to that list.

## 6. Minting GitHub tokens without touching the existing flow

This is the part of the requirement that looked hardest and turns out to be nearly free, because
the seam already exists.

### 6.1 Why the existing minter cannot simply be reused

[`templates/github-minter.yaml`](../../charts/kube-agents/templates/github-minter.yaml) renders one
minty config, keyed `<org>-<repo>.yaml`, holding one scope named `platform-agent-scope` whose rule
is `assertion.email in ['<platform GSA>']` and whose `repositories` list is the single GitOps repo.
A Terraform validation in `terraform/examples/full-install/main.tf` enforces the single repository.

Both ways of extending it are bad. Adding the upstream repository to the existing scope widens the
_Platform Agent's own token_ to reach `gke-labs/kube-agents` — a standing privilege increase for the
component that talks to customers, in exchange for a feature that is off by default. Adding a second
scope to the same config file means editing the shared template, so every install renders the
change whether or not it wants the feature, and the blast radius of a templating mistake is every
deployment.

There is also a plain fact that settles it: this needs a _different GitHub App_. The existing App is
installed on the customer's GitOps repository. Opening pull requests against `gke-labs/kube-agents`
requires an App installed there, with a different app ID and a different private key. Sharing a
minter between two Apps was never on the table.

### 6.2 A second minter, and no code change at all

The self-improvement template renders its own `github-token-minter-selfimprove` — Deployment,
Service, ConfigMap, KSA, GSA annotation, KMS key reference and NetworkPolicy — structurally a copy
of the existing one with different values, and rendered only when the feature is enabled. The
existing template is not touched.

Pointing the runner at it takes one environment variable.
[`github_token_refresh.py:17`](../../agents/platform/scripts/github_token_refresh.py) reads

```python
TOKEN_BROKER_URL = os.getenv("TOKEN_BROKER_URL", "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token")
```

and the credential proxy already forwards `TOKEN_BROKER_URL` into the subprocess environment
(`credential_proxy.py:890`) before running that script for `/v1/github/refresh`
(`credential_proxy.py:1640-1642`). Setting it on the runner's sidecar to
`http://github-token-minter-selfimprove.<namespace>.svc.cluster.local:8080/token` is the entire
integration. No Python changes, no proxy changes, no operator changes.

One wart, named so nobody has to rediscover it: the scope name in the request body is the literal
`"platform-agent-scope"` (`github_token_refresh.py:103`). Scope names are per-config-file, so the
self-improvement minty config uses that same name and everything works unmodified. It reads oddly
in a config that has nothing to do with the platform agent. Making it configurable is a one-line
`os.getenv` on a shared file; the trade is a misleading identifier against an edit to the credential
path, and the identifier is the cheaper cost.

**The separate proxy instance is required, not merely tidy.** A minted token is cached by
`gh auth login --with-token` into the sidecar's private `hosts.yml`, and `gh` holds one token per
host. Two identities sharing one credential proxy would share one `github.com` entry and overwrite
each other, so whichever refreshed last would own both flows — the failure being that the Platform
Agent's next GitOps push runs under the self-improvement App, or the reverse. Separate proxies mean
separate state directories and no interaction at all.

**And the proxy is not where the boundary lives.** `_handle_github_refresh` validates only that the
repository string looks like `owner/name`; there is no repo allowlist in the proxy. The boundary is
three things outside it: whether a rule file for that repository is mounted in the minter, whether
the CEL `assertion.email` matches the one permitted caller, and — decisively — which repositories
the GitHub App is installed on. All three differ between the two minters, which is why the
separation holds even though the code path is identical.

**One prerequisite is manual and cannot be Terraformed.** The App's private key is never a
Kubernetes Secret or a Secret Manager entry; it is imported once into an import-only KMS key, and
the minter fails its readiness probe until that import has happened. `install.sh` automates it for
the existing minter via `import_github_pem()`. A second App means a second import, sequenced before
the second minter can become ready, and an operator moving to `fork` or `upstream` mode should
expect that step.

### 6.3 Fork topology and the three modes

Repository policy is that branches are pushed to a fork, never to `gke-labs/kube-agents`. A
cross-fork pull request needs `contents: write` on the fork, to push the branch, and
`pull_requests: write` on the upstream, to open the PR against it — so the App is installed on both
and its scope lists both. That is the one place a scope in this design names two repositories, and
both belong to the self-improvement App.

Because that is a real amount of GitHub administration, and because the operator running an install
is usually not a kube-agents maintainer, the destination is a mode rather than an assumption:

- **`report-only` (the default when the feature is enabled).** No GitHub credential, no minter
  rendered, no network path to GitHub for writing. Findings accumulate in the ledger and are read
  with `kubectl get configmap`. Everything in §§2–5 runs; nothing leaves the cluster. Most installs
  should stay here — the loop is still worth running, because the ledger is exactly the evidence a
  bug report needs.
- **`fork`.** Branches and pull requests go to a fork the operator owns. Useful for an install that
  wants the loop's output as a reviewable artifact without publishing to a repository it does not
  control.
- **`upstream`.** Cross-fork pull requests against `gke-labs/kube-agents`. This is the mode the
  project's own dogfood installs run, and it is the one that makes the feature's stated goal — the
  harness getting less buggy over time — actually happen.

Harness findings are the exception to all three. `nousresearch/hermes-agent` is a third-party
repository; the loop never opens a pull request there. A harness finding becomes a section in the
ledger and, in `upstream` mode, an issue on this repository describing the upstream behaviour, so a
maintainer can decide whether to carry a patch in `deploy/docker/patches/` or raise it upstream
themselves.

## 7. Grading, frequency, and the gate

### 7.1 Severity

| Severity   | Meaning                                                                                                       |
| ---------- | ------------------------------------------------------------------------------------------------------------- |
| `critical` | A user gets a wrong answer, work is lost, a security control is not enforcing, or data is corrupted           |
| `high`     | A user-visible failure with a workaround; a recurring error that fails an entire job class; delivery failures |
| `medium`   | Measurable waste — extra turns, avoidable latency, a retry loop from a missing permission, a redundant call   |
| `low`      | Misleading output, documentation drift, a log line that sends a reader the wrong way                          |

Grading is the agent's judgement against that rubric, and it is recorded with the evidence so a
maintainer can disagree with the grade without re-deriving the finding.

### 7.2 Frequency, which is two knobs

The requirement asks that "the PR frequency be tracked" and gives `severity: critical` and
`frequency: 5 a day` as an example gate. That phrasing supports two readings — how often the
_finding_ recurs, and how many _pull requests_ the loop is allowed to open — and both are needed,
so the design implements both under distinct names rather than picking one:

- **`minOccurrencesPerDay`** is an evidence threshold. A finding seen once may be a fluke; a
  finding seen twenty times a day is a pattern, and the count is itself the strongest sentence in
  the pull request.
- **`maxPullRequestsPerDay`** is a noise ceiling. It bounds what the loop can do to a maintainer's
  inbox regardless of how much it finds, and it is the safety valve for the case where a genuine
  regression makes every run produce a fresh critical finding.

Counting requires identity across runs, which is the same problem the fleet audit solved. A finding
is fingerprinted over its signal class, the normalised message with identifiers and timestamps
stripped, and the code location in the cloned revision; the ledger holds the fingerprint, first and
last seen, a rolling 24-hour count, the current grade, and any pull request already opened for it.
[`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) is the precedent for the dedup and
lifecycle, and this feature should follow it rather than invent a second scheme.

### 7.3 The gate

A finding is promoted to a pull request when it matches any promotion rule, has not been promoted
inside its cooldown, and the day's budget is not spent. Everything else stays in the ledger, which
is not a discard: an unpromoted finding keeps accumulating occurrences, and a `high` at four
occurrences a day is one bad day away from crossing on its own.

## 8. The pull request

Five things, because that is what was asked for and because it is also what
[`.github/PULL_REQUEST_TEMPLATE.md`](../../.github/PULL_REQUEST_TEMPLATE.md) wants:

1. **The finding**, in detail: what is wrong, in which file at which revision, and what it costs.
2. **The evidence**: log queries with their results, span timings, the occurrence count and the
   window it was counted over, and the ledger fingerprint so the next run's PR can be recognised as
   the same finding rather than a new one.
3. **The fix and why this fix.** Alternatives considered and why they lost. A finding whose fix is
   not obvious should open an issue instead of guessing at a patch.
4. **Live validation, honestly scoped.** The runner is read-only, so it cannot exercise a fix
   against a running install, and claiming otherwise would be worse than claiming nothing. What it
   can do is run the repository's own checks against its clone — `go build` in `k8s-operator/`,
   the Python test suites, `make docs-check` — and state exactly that. `AGENTS.md` accepts "not
   live-tested" with a reason; it does not accept an empty section or an overstated one.
5. **The code changes**, scoped to the finding. One finding, one pull request. Conventional Commit
   title, and the body says it was opened by the self-improvement loop, which install produced it,
   and at which revision.

Two of the repository's rules apply awkwardly to a machine author and are worth settling here. The
**Self-Review** section must not claim a review it did not perform: the runner is the context that
wrote the change, and `AGENTS.md` is explicit that reviewing a diff in the context that produced it
is the one configuration that does not work. The honest content is the checks it ran, the angles it
considered, and a statement that no independent adversarial pass was performed — leaving that pass
to `kube-agents-bot`, which reviews every pull request on open anyway. And the **duplicate-work
scan** is not optional: §3.5's check against open issues and pull requests goes in the PR's Context
section as `Closes #<n>` or as a note on how this differs, exactly as a human contributor's would.

## 9. Configuration

One block, off by default, in `charts/kube-agents/values.yaml`:

```yaml
# The self-improvement loop: an hourly, read-only investigation of kube-agents
# itself — its source, its harness, and this installation — which grades what it
# finds and, above the gate below, opens a pull request upstream. Distinct from
# the fleet audits, which look outward at the managed clusters. Rendered only
# when enabled: with this off the chart emits nothing for it.
selfImprovement:
  enabled: false
  schedule: "0 * * * *"
  # Hard stop for one investigation. A run that hits this is a finding.
  activeDeadlineSeconds: 3600

  # report-only  ledger only, no GitHub credential, nothing leaves the cluster
  # fork         branches and PRs to a fork the operator owns
  # upstream     cross-fork PRs against gke-labs/kube-agents
  mode: report-only

  # Which of the seven signal classes to investigate. Narrowing this is the
  # cheapest way to cut the loop's cost.
  signals: [errors, inefficiency, latency, responses, delivery, forge, other]

  gate:
    # A finding is promoted when it matches any rule, is out of cooldown, and
    # the day's budget is unspent. Everything else stays in the ledger.
    rules:
      - severity: critical
        minOccurrencesPerDay: 1
      - severity: high
        minOccurrencesPerDay: 5
    maxPullRequestsPerDay: 2
    # Do not re-open the same fingerprint inside this window.
    cooldownHours: 24

  github:
    # Required when mode is fork or upstream.
    upstreamRepo: gke-labs/kube-agents
    forkRepo: ""
    appId: ""
    ksaName: kubeagents-selfimprove
    gsaName: kubeagents-selfimprove-gsa
    kms:
      keyring: selfimprove-token-minter-keyring
      key: selfimprove-token-minter-key
      keyVersion: "1"

  # Reading the agent's SQLite stores means snapshotting its volume, which is
  # the one place this feature creates cluster objects. Off even when the
  # feature is on; see the design's §3.4.
  volumeSnapshot:
    enabled: false
    snapshotClassName: ""

  # Empty uses the in-cluster LiteLLM Service the agent uses. Set to give the
  # loop its own model budget.
  model:
    endpoint: ""

  resources:
    requests: { cpu: 500m, memory: 2Gi }
    limits: { cpu: "2", memory: 4Gi }
```

## 10. Failure modes it takes a position on

**The loop investigates itself.** Its own runs produce logs, spans and errors in the same namespace,
and a loop that finds itself slow and opens a pull request about itself is a closed circuit that
generates work indefinitely. Its telemetry carries the service name
`kube-agents-selfimprove` and its log records are excluded from its own queries by that name. This
is a filter that must be written on the first day, not the day it goes wrong.

**A run outlives its schedule.** Hourly with `concurrencyPolicy: Forbid` means a run that takes
seventy minutes silently halves the cadence, and a run that hangs stops the loop entirely with no
error anywhere. `activeDeadlineSeconds` bounds it and a killed run is itself recorded in the ledger.

**The image moves and the CronJob does not.** Covered by the abort in §2, and worth repeating
because it is the failure that produces confidently wrong pull requests rather than no pull
requests. Every finding is stamped with the revision it was found at, so a maintainer can check.

**The gate is set too loose on a large fleet.** `maxPullRequestsPerDay` is per install, and fifty
installs at two a day is a hundred pull requests against one repository. This is the strongest
argument for `report-only` being the default and for `upstream` being a mode a maintainer chooses
for a small number of installs. Deduplication across installs is not solved here — see §11.

**A finding is right and the fix is wrong.** The most likely bad outcome, and the reason §8 says a
finding whose fix is not obvious becomes an issue. A pull request with correct evidence and a wrong
patch still delivers most of the value, provided the evidence is separable from the patch, which is
what the five-part structure is for.

**The ledger rolls the agent.** The operator SHA256-hashes the ConfigMaps it owns into the agent's
pod-template annotations, deliberately, because the profile merge only happens at startup and a
config change must therefore roll the pod. A ledger ConfigMap that ended up in that set would roll
the Platform Agent on every write — an hourly restart caused by the thing that is supposed to be
observing it without touching it. The ledger is a chart-owned object the agent's Deployment does not
reference, and it must stay that way.

**Evidence leaks.** Logs and spans contain customer cluster names, project IDs and user identifiers,
and an `upstream`-mode pull request publishes whatever is quoted in it. Evidence must be redacted
before it is quoted — the credential proxy's `redact_credentials` is the existing precedent for the
token case, but identifiers need their own pass, and no install should be moved to `upstream` mode
without someone having looked at what its ledger actually contains.

## 11. Limits

- **Nothing here is implemented.** The chart template, the runner, the ledger schema, the profile
  and its skill, and the Dockerfile revision stamp all have to be written.
- **Revision identification is a fallback until the image is stamped.** The registry-digest path in
  §2 works today for anything CI built, costs a registry read grant, and resolves to nothing for a
  dev-rebuild image tagged from `vars.sh`. On such an install the loop cannot establish what it is
  looking at and should refuse to run rather than guess.
- **Cross-install deduplication is out of scope.** Each install's ledger is its own, so the same bug
  found on ten installs is ten findings and, above the gate, up to ten pull requests. The mitigation
  is `report-only` as the default; a shared ledger would need a service this project does not have.
- **The SQLite stores are unreachable in the default configuration**, so the signal classes that
  depend on session and board history (§4, rows 2b and 4) run on weaker evidence unless §3.4's
  snapshot path is enabled.
- **Harness findings cannot be fixed by this loop.** A change to Hermes behaviour is either an
  upstream request to Nous Research or a new anchored patch under `deploy/docker/patches/`, and the
  patch harness demands an applier with exact-count assertions, a verifier that proves the patched
  code behaves, and a unit suite. The loop files the finding and the attribution; the patch is a
  human decision.
- **The loop cannot validate a fix against a running install**, by construction. Everything it
  proposes is reviewed and exercised by a human or by CI. That is the correct division: it is a
  detector with a strong evidence habit, not an autonomous committer.
