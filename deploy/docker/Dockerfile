# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
ARG ENVOY_VERSION=v1.38.0
FROM envoyproxy/envoy:${ENVOY_VERSION} AS envoy-bin

FROM golang:1.25-alpine AS watcher-builder
WORKDIR /workspace
COPY k8s-operator/ /workspace/k8s-operator/
WORKDIR /workspace/k8s-operator
RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o /workspace/k8s-event-watcher ./cmd/k8s-event-watcher/

FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    dnsutils \
    gnupg \
    iputils-ping \
    jq \
    nano \
    patch \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. Install credential-free utility CLIs used by the sandbox.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/k9s.tar.gz "https://github.com/derailed/k9s/releases/download/v0.51.0/k9s_Linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/k9s.tar.gz -C /usr/local/bin k9s && \
    rm /tmp/k9s.tar.gz && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python \
    ddgs \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    "hermes-agent[google_chat]" \
    "hermes-agent[slack]" \
    kubernetes \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-http

# Apply upstream PR #51567 patch to google_chat adapter to fix nested inline formatting leaks (GC0, GC1, etc.) and standalone thread creation bugs
RUN sed -i 's/for key, value in placeholders.items():/for key, value in reversed(list(placeholders.items())):/' /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/plugins/platforms/google_chat/adapter.py'); c = p.read_text(); t = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"'; r = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"\n    if thread_id:\n        url += \"?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD\"'; p.write_text(c.replace(t, r)) if t in c else exit(1)" && \
    grep -q "reversed(list(placeholders.items()))" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    grep -q "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" /opt/hermes/plugins/platforms/google_chat/adapter.py

# Hotfix: Allow explicit target routing for google_chat spaces/threads in send_message_tool
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/tools/send_message_tool.py'); c = p.read_text(); t = 'def _parse_target_ref(platform_name: str, target_ref: str):\n    \"\"\"Parse a tool target into chat_id/thread_id and whether it is explicit.\"\"\"'; r = t + '\n    if platform_name == \"google_chat\":\n        parts = target_ref.split(\":\", 1)\n        if len(parts) == 2 and parts[0].startswith(\"spaces/\"):\n            return parts[0].strip(), parts[1].strip(), True\n        elif target_ref.startswith(\"spaces/\"):\n            return target_ref.strip(), None, True'; p.write_text(c.replace(t, r)) if t in c else exit(1)"

# Hotfix: stop the kanban notifier from severing URLs in a card's completion line.
# Upstream hard-slices the handoff at a fixed width, which turned a published
# ledger link into ".../is" in chat. Swap both slices for a whitespace-boundary
# clip that also stops discarding every line after the first. See the module
# docstring in deploy/docker/patches/kanban_handoff_clip.py.
COPY deploy/docker/patches/kanban_handoff_clip.py /opt/hermes/gateway/kanban_handoff_clip.py
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/gateway/kanban_watchers.py'); c = p.read_text(); t1 = 'h = lines[0][:200] if lines else payload_summary[:200]'; t2 = 'r = lines[0][:160] if lines else task.result[:160]'; exit(1) if (t1 not in c or t2 not in c) else None; c = c.replace(t1, 'h = _clip_handoff(payload_summary)').replace(t2, 'r = _clip_handoff(task.result)'); p.write_text(c + '\n\n# kube-agents patch: see gateway/kanban_handoff_clip.py\nfrom gateway.kanban_handoff_clip import clip_handoff as _clip_handoff\n')" && \
    grep -q "h = _clip_handoff(payload_summary)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "r = _clip_handoff(task.result)" /opt/hermes/gateway/kanban_watchers.py && \
    /opt/hermes/.venv/bin/python3 -c "import ast, pathlib; ast.parse(pathlib.Path('/opt/hermes/gateway/kanban_watchers.py').read_text())"

# Hotfix: make a dispatched cron run report back, and stop it closing the
# kanban card of the caller that dispatched it. Upstream discards the run's
# final response and lets the run inherit HERMES_KANBAN_TASK, which is how
# three fleet-audit streams published wrong ledgers on 2026-08-04. Nine
# anchored edits across three files, so the surgery lives in an applier script
# rather than inline; it fails the build on any anchor that no longer matches.
# verify_cron_run_scope.py then exercises the patched code. The applier only
# proves the anchors matched, and the first regression here was a runtime type
# error no anchor could have caught: save_job_output returns a pathlib.Path,
# the run action json.dumps it, and the caller got a TypeError in place of the
# report it had waited six minutes for.
# See the module docstring in deploy/docker/patches/cron_run_scope.py.
COPY deploy/docker/patches/cron_run_scope.py /opt/hermes/tools/cron_run_scope.py
COPY deploy/docker/patches/apply_cron_run_scope.py /tmp/apply_cron_run_scope.py
COPY deploy/docker/patches/verify_cron_run_scope.py /tmp/verify_cron_run_scope.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_cron_run_scope.py /opt/hermes && \
    grep -q "outcome=None) -> bool:" /opt/hermes/cron/scheduler.py && \
    grep -q "with cron_run_scope(job_id):" /opt/hermes/tools/cronjob_tools.py && \
    grep -q "cron_ownership_violation(tid)" /opt/hermes/tools/kanban_tools.py && \
    grep -q 'outcome\["output_file"\] = str(output_file)' /opt/hermes/cron/scheduler.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
from gateway.kanban_handoff_clip import clip_handoff; \
from tools.cron_run_scope import clip_cron_response, cron_run_scope; \
clipped = clip_cron_response(('word ' * 2000) + 'https://example.invalid/issues/27'); \
exit(1) if ('https://' in clipped or not 3000 < len(clipped) <= 4000) else None" && \
    /opt/hermes/.venv/bin/python3 /tmp/verify_cron_run_scope.py && \
    rm /tmp/apply_cron_run_scope.py /tmp/verify_cron_run_scope.py

# Fix: one slow cron job no longer blocks every other job on the same profile.
# `tick()` defaults to sync=True and holds <HERMES_HOME>/cron/.tick.lock across
# the whole `as_completed` wait, so on the platform profile -- ticked only by
# spawning `hermes cron tick`, which takes that default -- a fleet audit starved
# every other job for its full runtime. Measured over 17h: 3 of 35
# github-issue-resolver firings 418s / 179s / 1142s late, each unblocking within
# seconds of the blocking audit finishing. Six anchored edits, so the surgery
# lives in an applier. The lock now covers only what its own comment says it is
# for -- get_due_jobs, advance_next_runs, create_execution, pool.submit -- and a
# per-job flock replaces the cross-process in-flight guard the wide lock was
# implicitly providing. flock rather than a ledger row on purpose: the platform
# ledger is never swept (recover_interrupted() runs only under the in-process
# ticker) and already carries 6 permanent status='running' rows, so a ledger
# gate would have wedged those job ids forever.
# Must stay AFTER the cron_run_scope block: every anchor was verified against
# the post-cron_run_scope source.
# See the module docstring in deploy/docker/patches/cron_tick_lock_scope.py.
COPY deploy/docker/patches/cron_tick_lock_scope.py /opt/hermes/tools/cron_tick_lock_scope.py
COPY deploy/docker/patches/apply_cron_tick_lock_scope.py /tmp/apply_cron_tick_lock_scope.py
COPY deploy/docker/patches/verify_cron_tick_lock_scope.py /tmp/verify_cron_tick_lock_scope.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_cron_tick_lock_scope.py /opt/hermes && \
    grep -q "from tools.cron_tick_lock_scope import AdvisoryLock, JobLocks" /opt/hermes/cron/scheduler.py && \
    grep -q "_tick_lock = AdvisoryLock(lock_fd, fcntl, globals().get(\"msvcrt\"))" /opt/hermes/cron/scheduler.py && \
    grep -q "if not _job_locks.claim(job_id):" /opt/hermes/cron/scheduler.py && \
    grep -q "_job_locks.release(j\[\"id\"\])" /opt/hermes/cron/scheduler.py && \
    ! grep -q "fcntl.flock(lock_fd, fcntl.LOCK_UN)" /opt/hermes/cron/scheduler.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/verify_cron_tick_lock_scope.py && \
    rm /tmp/apply_cron_tick_lock_scope.py /tmp/verify_cron_tick_lock_scope.py

# Perf: stop the notifier waking a card's creator for a completion it has
# already delivered. On a terminal event the notifier both sends the worker's
# summary to the thread AND injects a synthetic message that costs the creator a
# full model turn — which then re-reads the card and paraphrases a message the
# user is already looking at. Measured at 5.9s and 32,460 input tokens on task
# t_c31a1f00. Upstream hardcodes the wake set with no config key; this makes it
# read kanban.wake_on_events, defaulting to the upstream tuple.
# See the module docstring in deploy/docker/patches/kanban_wake_kinds.py.
COPY deploy/docker/patches/kanban_wake_kinds.py /opt/hermes/gateway/kanban_wake_kinds.py
COPY deploy/docker/patches/apply_kanban_wake_kinds.py /tmp/apply_kanban_wake_kinds.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_wake_kinds.py /opt/hermes && \
    grep -q '_wake_kinds = _wake_kinds_for(d\["events"\])' /opt/hermes/gateway/kanban_watchers.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
from gateway.kanban_wake_kinds import DEFAULT_WAKE_KINDS, resolve_wake_kinds; \
exit(1) if resolve_wake_kinds(lambda: {}) != DEFAULT_WAKE_KINDS else None; \
exit(1) if resolve_wake_kinds(lambda: {'kanban': {'wake_on_events': ['crashed']}}) != ('crashed',) else None" && \
    rm /tmp/apply_kanban_wake_kinds.py

# Perf: hide worker-only kanban tools from orchestrator profiles. Upstream gates
# every lifecycle tool with _check_kanban_mode, which is true for any profile
# carrying the kanban toolset — so the Chat Agent front door is shipped
# kanban_complete/block/heartbeat/link/attach* on every call despite SOUL.md
# §1.5 forbidding it from calling them. Hermes has no per-tool denylist, so the
# 10,041 characters (~2,510 tokens) of schema cannot be dropped by config.
# See the module docstring in deploy/docker/patches/kanban_worker_tools.py.
COPY deploy/docker/patches/kanban_worker_tools.py /opt/hermes/tools/kanban_worker_tools.py
# Also staged next to the applier: it imports WORKER_ONLY_TOOLS, and only the
# script's own directory is on sys.path.
COPY deploy/docker/patches/kanban_worker_tools.py /tmp/kanban_worker_tools.py
COPY deploy/docker/patches/apply_kanban_worker_tools.py /tmp/apply_kanban_worker_tools.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_worker_tools.py /opt/hermes && \
    test "$(grep -c 'check_fn=_check_kanban_worker_mode' /opt/hermes/tools/kanban_tools.py)" = "7" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
import os; \
from tools.kanban_worker_tools import check_kanban_worker_mode; \
exit(1) if check_kanban_worker_mode() else None; \
os.environ['HERMES_KANBAN_TASK'] = 't_probe'; \
exit(1) if not check_kanban_worker_mode() else None" && \
    rm /tmp/apply_kanban_worker_tools.py /tmp/kanban_worker_tools.py

# Hotfix: make a card's answer reach the person who asked for it. On 2026-08-07
# a user asked which cron jobs were enabled; three cards ran, all closed `done`,
# and the list never arrived. The board shows why: `tasks.result IS NULL` on all
# three. The workers built the catalogue, printed it to their own stdout, and
# dropped it at the kanban_complete call -- because upstream's schema calls
# `result` a "Short result log line (legacy field)... Use ``summary`` instead"
# and accepts `summary or result`, while the kernel stores the chat line as
# `(summary or result).strip().splitlines()[0][:400]`. So the only field that
# can carry a report is marked legacy, the field the model is steered to holds
# one line of 400 characters, and nothing posts `result` to chat at all.
#
# Two patches, applied last so they compose with the four above rather than
# competing for the same lines. Both are deterministic -- neither tries to
# classify a card as "asked for information", because every card has an answer.
#   * kanban_result_required: rewrites the schema wording and requires `result`.
#     Refuses ONCE per task, then accepts and promotes `summary`, so the gate
#     can never wedge a card shut.
#   * kanban_result_delivery: appends `result` to the completion message the
#     notifier already builds -- one send, inheriting the existing failure
#     counter, cursor rewind, and subscription-drop machinery.
# See the module docstrings in deploy/docker/patches/kanban_result_*.py.
COPY deploy/docker/patches/kanban_result_required.py /opt/hermes/tools/kanban_result_required.py
COPY deploy/docker/patches/kanban_result_delivery.py /opt/hermes/gateway/kanban_result_delivery.py
COPY deploy/docker/patches/apply_kanban_result_required.py /tmp/apply_kanban_result_required.py
COPY deploy/docker/patches/apply_kanban_result_delivery.py /tmp/apply_kanban_result_delivery.py
COPY deploy/docker/patches/verify_kanban_result.py /tmp/verify_kanban_result.py
# The applier imports OLD_GATE/NEW_GATE from the module, and only the script's
# own directory is on sys.path.
COPY deploy/docker/patches/kanban_result_required.py /tmp/kanban_result_required.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_result_required.py /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_result_delivery.py /opt/hermes && \
    grep -q "_result_err, result = _require_result(tid, summary, result)" /opt/hermes/tools/kanban_tools.py && \
    grep -q "_apply_result_schema(KANBAN_COMPLETE_SCHEMA)" /opt/hermes/tools/kanban_tools.py && \
    grep -q "handoff += _kanban_result_block(handoff, task)" /opt/hermes/gateway/kanban_watchers.py && \
    ! grep -q "provide at least one of: summary (preferred), result" /opt/hermes/tools/kanban_tools.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/verify_kanban_result.py && \
    rm /tmp/apply_kanban_result_required.py /tmp/apply_kanban_result_delivery.py \
       /tmp/verify_kanban_result.py /tmp/kanban_result_required.py

# Hotfix: two kanban scheduler faults, both measured on the cluster 2026-08-07.
#
#   * Self-parenting deadlock. `task_links` is a predecessor edge, so a card
#     that creates work with `parents=[<itself>]` and then blocks on it can
#     never be satisfied -- claim_task refuses the children until the parent is
#     done, and the parent is waiting on them. Worse, a `dependency` block
#     routes to `todo` rather than `blocked`, which skips the block-recurrence
#     loop breaker, so the parent respawned a full agent run every few seconds.
#     One worker escaped by shelling out to sqlite3 and closing three cards
#     `done` with a fabricated 27-character result. kanban_dependency_repair
#     inverts the mis-directed edges at the block point, under two guards: the
#     block must be provably unsatisfiable (no unsettled parents of its own),
#     and inverting must actually free the child (this card is its last
#     unfinished parent). Real fan-ins and real pipelines are untouched.
#
#   * Claim tokens outlive the process that made them. `_claimer_id()` is
#     `hostname:pid`, and under Kubernetes hostname is the pod name --
#     pod-scoped, not restart-scoped. When the gateway took a SIGBUS (exit 135)
#     the replacement container kept the pod name, reset its PID namespace, and
#     adjudicated its predecessor's worker PIDs: six cards marked `crashed`.
#     `_error_fingerprint` then normalised the PID out of "pid <N> not alive",
#     so six independent crashes read as one systemic fault, which drops
#     failure_limit to 1 -- every one of them gave up on its first failure.
#     kanban_claim_fencing narrows the liveness check to this exact process
#     life, hands back cards whose owner is provably gone (closing the 900s
#     dark window a rollout otherwise costs), and keeps the PID in the
#     fingerprint.
#
# Both are emergent scheduling behaviour rather than a bad string, so the
# anchors alone prove nothing: verify_kanban_scheduling.py replays both
# incidents against a real board through the patched engine.
# See the module docstrings in deploy/docker/patches/kanban_dependency_repair.py
# and deploy/docker/patches/kanban_claim_fencing.py.
COPY deploy/docker/patches/kanban_dependency_repair.py /opt/hermes/hermes_cli/kanban_dependency_repair.py
COPY deploy/docker/patches/kanban_claim_fencing.py /opt/hermes/hermes_cli/kanban_claim_fencing.py
COPY deploy/docker/patches/apply_kanban_dependency_repair.py /tmp/apply_kanban_dependency_repair.py
COPY deploy/docker/patches/apply_kanban_claim_fencing.py /tmp/apply_kanban_claim_fencing.py
COPY deploy/docker/patches/verify_kanban_scheduling.py /tmp/verify_kanban_scheduling.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_dependency_repair.py /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_claim_fencing.py /opt/hermes && \
    grep -q "_kanban_repair_inverted_deps(conn, task_id, reason)" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "conn, _kanban_claimer, _pid_alive, _resolve_crash_grace_seconds" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "_kanban_claim_is_self(lock, _kanban_claimer)" /opt/hermes/hermes_cli/kanban_db.py && \
    test "$(grep -c 'lock.startswith(host_prefix)' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/verify_kanban_scheduling.py && \
    rm /tmp/apply_kanban_dependency_repair.py /tmp/apply_kanban_claim_fencing.py \
       /tmp/verify_kanban_scheduling.py

# Hotfix: the circuit breaker's trip does not survive the tick that wrote it.
#
# `dispatch_once` calls `detect_crashed_workers` and then, twenty lines later,
# `recompute_ready`. Both decide whether a card is over its retry budget, and
# they do not use the same threshold. `_record_task_failure` persists
# `consecutive_failures = old + 1` -- the raw attempt count -- while the two
# callers that trip early pass a threshold the dispatcher never sees: the
# clean-exit protocol-violation path adjudicates its own streak and passes
# `force_trip=True`, and the systemic same-error path passes `failure_limit=1`.
# `recompute_ready` re-derives the verdict from the column alone against the
# dispatcher's limit (2), sees 1 < 2, flips the card back to `ready` and emits
# `promoted`; the next lines of the same tick claim and respawn it. The policy
# "stop after 3 protocol violations and hand to a human" silently means 4.
#
# The fix stores the exhausted budget rather than the raw attempt count, so the
# counter -- which stays the single arbiter -- says what the trip decided. That
# matters more than it sounds: the obvious alternative, having `recompute_ready`
# read the `gave_up` event, was built and rejected because `assign_task` zeroes
# the counter and emits `assigned`, not `unblocked`, so an event-stickiness
# guard pins every reassigned card in `blocked` forever.
#
# The anchors prove only that the text landed. verify_kanban_breaker_counter.py
# replays all three trip arms plus the spawn path against a real board through
# the real `_record_task_failure` and `recompute_ready`, and -- the part that
# actually earns its runtime -- asserts that `assign_task`, `unblock_task`,
# parent-dependency promotion and worker `kanban_block` stickiness all still
# behave exactly as they did before.
#
# Two consequences operators need, both pinned by that gate rather than only
# written down. `assign_task` to the SAME profile no longer frees a tripped card
# -- it only zeroes the counter when the assignee actually changes, which was
# survivable while a systemic trip stored 1 (below the dispatcher's 2). Reassign
# elsewhere or `hermes kanban unblock`. And the floor is `DEFAULT_FAILURE_LIMIT`,
# not the configured limit, because `detect_crashed_workers` never sees the
# latter: raise `kanban.failure_limit` above 2 and systemic trips become
# revertible again, above 3 and protocol trips follow.
#
# Ordered after kanban_claim_fencing on purpose. That patch edits
# `detect_crashed_workers` and `_error_fingerprint`; this one edits only
# `_record_task_failure`, so the anchors do not overlap, but the fingerprint fix
# is what made the systemic arm rare, and reading them in this order is how the
# story makes sense.
# See the module docstring in
# deploy/docker/patches/apply_kanban_breaker_counter.py.
COPY deploy/docker/patches/apply_kanban_breaker_counter.py /tmp/apply_kanban_breaker_counter.py
COPY deploy/docker/patches/verify_kanban_breaker_counter.py /tmp/verify_kanban_breaker_counter.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_breaker_counter.py /opt/hermes && \
    grep -q "persisted_failures = max(failures, effective_limit)" /opt/hermes/hermes_cli/kanban_db.py && \
    test "$(grep -c '(persisted_failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
    test "$(grep -c '(failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/verify_kanban_breaker_counter.py && \
    rm /tmp/apply_kanban_breaker_counter.py /tmp/verify_kanban_breaker_counter.py

# Hotfix: kanban workers leaving `run_conversation` without a board write.
#
# Upstream guards this in exactly one place -- the end of the no-tool-calls
# branch, where `build_kanban_stop_nudge` gives a worker that is about to stop
# talking another turn to call `kanban_complete`/`kanban_block`. Every other way
# out of that loop is a bare `break` several hundred lines earlier and jumps
# straight over it. The worker exits rc=0 having never touched the board and the
# reaper stamps a `protocol_violation` after the fact -- a failure with no
# explanation in it, because by then the reason is gone.
#
# Card t_d3efabef burned four runs and 38 minutes this way on 2026-08-07. Each
# run halted on `loop_web_search_cap` with 173 successful searches in hand and
# was never told; the halt path breaks out of the tool-call branch without
# showing the model the block result.
#
#   * kanban_guardrail_exit's first edit makes that halt kanban-aware: reuse
#     upstream's own nudge helper (already bounded at two attempts, already a
#     no-op for non-workers) and add the one thing its text does not say -- the
#     tool is gone for this run, stop trying to use it. Clearing
#     `_tool_guardrail_halt_decision` is mandatory; `reset_for_turn` clears it
#     per turn, not per iteration.
#   * The second edit backstops the six sibling exits that leak identically, in
#     `finalize_turn` -- the single funnel every `break` passes through. An
#     unexplained protocol violation becomes a counted `timed_out` whose error
#     text names the exit reason. Goal-mode workers are excluded: they call
#     `finalize_turn` once per turn, so recording on turn 1 of N would release
#     the claim underneath the loop.
#
# The anchors prove only that the text landed. verify_kanban_guardrail_exit.py
# additionally parses the patched loop to prove the nudge is reachable, ahead of
# the break, and terminal; drives it against the real `agent.kanban_stop` (whose
# signature the patch would otherwise absorb in silence); and records a leak on a
# real board through the real `_record_task_failure`.
#
# The per-turn `max_web_searches` ceiling that triggered this is a config change
# rather than a patch -- see the /opt/platform-template/config.yaml assertion
# further down. It must not ship alone: raising the cap only moves when the bug
# fires.
# See the module docstring in deploy/docker/patches/kanban_guardrail_exit.py.
COPY deploy/docker/patches/kanban_guardrail_exit.py /opt/hermes/hermes_cli/kanban_guardrail_exit.py
COPY deploy/docker/patches/apply_kanban_guardrail_exit.py /tmp/apply_kanban_guardrail_exit.py
COPY deploy/docker/patches/verify_kanban_guardrail_exit.py /tmp/verify_kanban_guardrail_exit.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_guardrail_exit.py /opt/hermes && \
    grep -q "if _kanban_halt_nudge:" /opt/hermes/agent/conversation_loop.py && \
    grep -q "_kanban_should_record_missing(" /opt/hermes/agent/turn_finalizer.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/verify_kanban_guardrail_exit.py && \
    rm /tmp/apply_kanban_guardrail_exit.py /tmp/verify_kanban_guardrail_exit.py


# 3. Clone and build mcp-remote proxy. Git is a build-only dependency here;
# the sandbox runtime exposes only the credential-proxy Git wrapper.
#
# The source is a fork, and deliberately so: it is the only build that speaks
# Google ADC. Its commits ahead of upstream add `google-auth-library`, mint a
# fresh bearer token per call (handling expiry), and attach the
# `X-Goog-User-Project` header. Upstream geelen/mcp-remote has none of that, so
# switching the clone URL to the official repository would break every GKE and
# developer_knowledge MCP call. Do not "clean this up" without replacing the
# auth path first.
#
# The SDK is pinned explicitly because the git SHA alone does not pin it. The
# fork commits only a `pnpm-lock.yaml`, which `npm install` ignores, and
# declares `^1.23.0`; with tsup's `external: []` the SDK is bundled into the
# shipped chunk. Without this line the transport baked into the image is
# whatever was latest on build day, so two builds of this same Dockerfile can
# ship different error semantics (1.24.0 changed the thrown class).
COPY deploy/docker/patches/apply_mcp_remote_forward_errors.py /tmp/apply_mcp_remote_forward_errors.py
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm pkg set devDependencies.@modelcontextprotocol/sdk=1.30.0 && \
    npm install && \
    /opt/hermes/.venv/bin/python3 /tmp/apply_mcp_remote_forward_errors.py /opt/mcp-remote && \
    npm run build && \
    grep -q 'Failed to forward message to remote server' dist/*.js && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git /tmp/apply_mcp_remote_forward_errors.py

# 5. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults && chown hermes:hermes /opt/defaults
COPY --chown=hermes:hermes deploy/shared/defaults/ /opt/defaults/
COPY --chown=hermes:hermes agents/platform/docs/glossary.md /opt/defaults/docs/
COPY --chown=hermes:hermes agents/platform/docs/gcp-console-links.md /opt/defaults/docs/


# 5.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"

# Remove the build-only clone dependency after all plugin installation is done.
RUN apt-get purge -y git && apt-get autoremove -y && \
	rm -rf /var/lib/apt/lists/* && \
	for binary in gcloud kubectl gh git; do \
		if command -v "${binary}" >/dev/null 2>&1; then \
			echo "unexpected credential-aware CLI in sandbox image: ${binary}" >&2; \
			exit 1; \
		fi; \
	done

# 5.8. Copy custom agent startup script
COPY deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
COPY deploy/shared/credential-proxy-path.sh /etc/profile.d/credential-proxy-path.sh
COPY agents/platform/scripts/credential_proxy_client.py /usr/local/bin/credential-proxy-exec
RUN chmod 755 /usr/local/bin/agent-entrypoint /usr/local/bin/credential-proxy-exec /etc/profile.d/credential-proxy-path.sh && \
    mkdir -p /opt/credential-proxy/bin && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/kubectl && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gcloud && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gh && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/git

# Copy event-watcher binary from builder stage
COPY --from=watcher-builder /workspace/k8s-event-watcher /usr/local/bin/k8s-event-watcher
RUN chmod 755 /usr/local/bin/k8s-event-watcher

# 6. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 7. Execution lifecycle
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["hermes", "gateway", "run"]



# --- AGENT GATEWAY TARGET ---
# One image, one pod, multiple co-located Hermes profiles sharing one PVC:
#   - `default`   = Chat Agent (the front door; receives chat ingress) — synced
#                   from /opt/defaults into $HERMES_HOME at startup, except for
#                   its config.yaml, which is rebuilt from /opt/chat-template
#                   (see docker-entrypoint.sh step 2d).
#   - `platform`  = Platform Agent specialist — scaffolded at startup from
#                   /opt/platform-template (see docker-entrypoint.sh).
#   - `cluster-*` = per-cluster read-only agents — scaffolded at runtime from
#                   /opt/cluster-template (see scripts/cluster_agent_profile.py).
FROM agent-base AS platform

# --- Chat Agent (the `default` profile) ---
# Chat ingress plugins/hooks (session_store, session_otel_bridge, tool_call_audit,
# chat_message_audit) live with the front door that handles ingress.
COPY --chown=hermes:hermes agents/chat/defaults/ /opt/defaults/
COPY agents/chat/SOUL.md /opt/defaults/SOUL.md
COPY agents/chat/AGENTS.md /opt/defaults/AGENTS.md
# Self-contained Chat Agent config: it replaces (NOT merges with) the shared-defaults
# config, so the front door stays minimal — router tools only, no cloud/write tools.
#
# It goes to /opt/chat-template rather than /opt/defaults, and the shared-defaults copy
# is removed, so that NOTHING under /opt/defaults is called config.yaml. Entrypoint step
# 2 bulk-copies /opt/defaults onto the PVC with `cp -ru`, and this is the one file there
# the running agent writes to — `/sethome`'s home channel, the monitoring install id,
# saved slash-command preferences. An mtime-driven copy racing an image roll would
# discard all of it. Step 2d rebuilds config.yaml from this template plus the operator's
# overlay plus the runtime's own edits, and it is the only writer.
COPY --chown=hermes:hermes agents/chat/config.yaml /opt/chat-template/config.yaml
RUN rm -f /opt/defaults/config.yaml

# --- Shared scripts (reachable by every profile at $HERMES_HOME/scripts) ---
# Executable scripts are shared, not per-profile: the router (Chat Agent) and the
# platform/cluster tooling all resolve under $HERMES_HOME/scripts.
COPY agents/chat/scripts/ /opt/defaults/scripts/
COPY agents/platform/scripts/ /opt/defaults/scripts/
# Merges the operator's per-profile config overlays at startup (entrypoint step 2.7).
# Shipped as a file rather than inlined in the entrypoint so it can be unit tested —
# see tests/test_profile_overlay.py.
COPY deploy/shared/profile_overlay.py /opt/defaults/scripts/profile_overlay.py
# Links profile-targeted plugin image volumes into their profile homes (entrypoint step
# 2.65, and cluster_agent_profile.py for profiles scaffolded later) — see
# tests/test_profile_plugins.py.
COPY deploy/shared/profile_plugins.py /opt/defaults/scripts/profile_plugins.py
# Rebuilds the DEFAULT profile's config.yaml at startup (entrypoint step 2d) — see
# tests/test_default_profile_config.py. Imports merge() from profile_overlay.py, which is
# why the two must land in the same directory.
COPY deploy/shared/default_profile_config.py /opt/defaults/scripts/default_profile_config.py

# --- Platform Agent specialist profile TEMPLATE (scaffolded at startup) ---
COPY --chown=hermes:hermes agents/platform/SOUL.md /opt/platform-template/SOUL.md
COPY --chown=hermes:hermes agents/platform/AGENTS.md /opt/platform-template/AGENTS.md
COPY --chown=hermes:hermes agents/platform/CAPABILITIES.md /opt/platform-template/CAPABILITIES.md
COPY --chown=hermes:hermes agents/platform/skills/ /opt/platform-template/skills/
COPY --chown=hermes:hermes agents/platform/cron/ /opt/platform-template/cron/
COPY --chown=hermes:hermes agents/platform/governance/ /opt/platform-template/governance/

# Platform config is self-contained (scaffold does not merge again): build it by
# merging the shared defaults base with the platform overrides and bake it into
# the template. This reproduces the exact config the platform ran with as the
# former default profile.
#
# The two ceilings the platform overlay raises are asserted here rather than
# eyeballed, because a merge that dropped either fails silently: the agent falls
# back to the stock default and the only symptom is a run that dies early. They
# are read back the way the agent reads them -- through `load_config_readonly`,
# which deep-merges DEFAULT_CONFIG *under* the file -- so this also proves the
# overlay did not clobber its untouched siblings.
COPY deploy/shared/defaults/config.yaml /tmp/platform-base-config.yaml
COPY agents/platform/config.yaml /tmp/platform-overlay-config.yaml
COPY deploy/docker/merge_configs.py /tmp/merge_configs.py
RUN /opt/hermes/.venv/bin/python3 /tmp/merge_configs.py /tmp/platform-base-config.yaml /tmp/platform-overlay-config.yaml && \
    cp /tmp/platform-base-config.yaml /opt/platform-template/config.yaml && \
    cd /opt/hermes && HERMES_HOME=/opt/platform-template /opt/hermes/.venv/bin/python3 -c "\
from hermes_cli.config import load_config_readonly; \
from agent.tool_guardrails import ToolCallGuardrailConfig; \
cfg = load_config_readonly(); \
caps = ToolCallGuardrailConfig.from_mapping(cfg.get('tool_loop_guardrails', {})).loop_caps; \
assert caps.max_web_searches == 200, 'per-card web_search budget lost: %r' % caps.max_web_searches; \
assert caps.max_subagents == 50, 'overlay clobbered max_subagents: %r' % caps.max_subagents; \
assert cfg.get('agent', {}).get('max_turns') == 250, 'iteration budget lost: %r' % cfg.get('agent', {}).get('max_turns')" && \
    rm -f /tmp/merge_configs.py /tmp/platform-base-config.yaml /tmp/platform-overlay-config.yaml

# Copy the Cluster Agent profile TEMPLATE (persona, config, skills), scaffolded
# into per-cluster Hermes profiles at runtime by scripts/cluster_agent_profile.py.
COPY --chown=hermes:hermes agents/cluster/ /opt/cluster-template/

# Shared plugins available to all profiles (memory backend used by the Chat Agent).
COPY agents/platform/plugins/ /opt/hermes/plugins/

# NOTE: the platform overlay is no longer merged into /opt/defaults/config.yaml.
# /opt/defaults now belongs to the Chat Agent (`default` profile) and its config
# is copied verbatim above; the platform overlay is merged into
# /opt/platform-template/config.yaml instead (see the RUN block above).

# Normalize ownership and permissions at the end of build.
#
# `set -eu` rather than an `&&` chain ending in `|| true`: that trailing `|| true`
# bound to the WHOLE chain, so a failure in any early command short-circuited
# every later chmod and still exited 0 — a build that silently shipped
# unnormalized permissions. Each command must now fail the build loudly.
# Every path below is guaranteed to exist at this point: /opt/hermes/skills and
# /opt/hermes/plugins come from the base image, the rest are COPYed above.
RUN set -eu; \
    chown -R hermes:hermes /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/cluster-template /opt/platform-template /opt/chat-template; \
    for d in /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/cluster-template /opt/platform-template /opt/chat-template; do \
        find "$d" -type d -exec chmod 755 {} + ; \
        find "$d" -type f -exec chmod 644 {} + ; \
    done; \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + ; \
    for d in /opt/hermes/skills /opt/platform-template /opt/cluster-template; do \
        find "$d" -path '*/scripts/*' -type f -exec chmod 755 {} + ; \
    done

# Bake the bytecode cache for the vendored runtime.
#
# The base image sets PYTHONDONTWRITEBYTECODE=1 (an `ENV` layer in the upstream
# hermes-agent image, not anything this repo sets), and at runtime /opt/hermes
# is read-only to the uid the pod runs as. Between them the interpreter can
# never cache this tree, so the whole of it is re-parsed and re-compiled from
# source by every Python process the container starts: the gateway, each
# interpreter step 2.5 onward of the entrypoint runs, every MCP child, and every
# kanban worker. A live pod shows 9366 .py files here and 0 .pyc.
#
# Precompiling at build time sidesteps both causes. PYTHONDONTWRITEBYTECODE
# suppresses WRITING a cache, never reading one, so the variable can stay as the
# base image sets it; and a .pyc baked beside its own .py in an immutable image
# layer cannot go stale the way a runtime-written one on the shared PVC could.
# Nothing has to become writable at runtime and nothing is cached across images.
#
# Use the venv's own interpreter — the one that will import these modules. A
# .pyc is only loaded by an interpreter whose magic tag matches the writer's.
#
# No `-o`: the runtime sets neither PYTHONOPTIMIZE nor -O, so it looks for the
# plain `.cpython-NNN.pyc` that a bare compileall writes, not an `.opt-N` one.
# Setting PYTHONOPTIMIZE in the pod later without adding a matching `-o` here
# would quietly put the runtime back to compiling from source.
#
# Likewise assumes PYTHONPYCACHEPREFIX stays unset (it is), since that would
# redirect the lookup away from the in-tree __pycache__ this writes.
#
# Deliberately NOT extended to /opt/defaults, /opt/platform-template or
# /opt/cluster-template. The entrypoint copies all three onto the PVC at
# startup, and `cp` rewrites the destination mtime, which invalidates a
# timestamp-validated .pyc anyway — it would ship bytes that are guaranteed to
# be ignored.
#
# Runs after the normalization above rather than before it, so the generated
# __pycache__ directories are not swept up by the `*/scripts/*` chmod 755 there.
# They land root-owned 0644/0755 from the build umask, which is what the rest of
# /opt/hermes already is and is readable by the runtime uid.
#
# The failure branch reports rather than aborting, which is not the `|| true`
# the block above warns against: that one masked failures of unrelated commands
# in the same chain. This is one command whose only effect is a cache. A module
# that will not precompile — a py2 relic or a conditionally-invalid file, which
# large vendored trees routinely carry — simply compiles at import as it does
# today. `set -eu` still governs everything else.
#
# No speedup figure is claimed. The size of the win has not been measured on
# this image.
RUN set -eu; \
    /opt/hermes/.venv/bin/python3 -m compileall -q /opt/hermes \
      || echo "WARN: some modules under /opt/hermes did not precompile; they will compile at import, as they do without this step" >&2

# --- CREDENTIAL PROXY TARGET ---
# Only this image contains the real credential-aware CLIs. The platform target
# above contains wrappers with the same names and cannot execute locally.
FROM platform AS credential-proxy

USER root

COPY --from=envoy-bin /usr/local/bin/envoy /usr/local/bin/envoy
RUN mkdir -p /etc/envoy
COPY --chmod=0644 deploy/shared/envoy-credential-proxy.yaml /etc/envoy/envoy-credential-proxy.yaml
COPY deploy/shared/envoy-credential-sidecar.sh /usr/local/bin/envoy-credential-sidecar

RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
      git \
      google-cloud-cli \
      google-cloud-cli-gke-gcloud-auth-plugin \
      kubectl && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" && \
    dpkg -i /tmp/gh.deb && \
    rm -f /tmp/gh.deb && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 755 /etc/envoy /usr/local/bin/envoy /usr/local/bin/envoy-credential-sidecar
