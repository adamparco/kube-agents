#!/usr/bin/env bash
# broker-driver.sh — put a REAL caller at a deployed broker's door.
#
# WHY THIS EXISTS
#   Every broker claim recorded before Phase 9's T8b-4b was a claim about a broker that nothing had
#   ever spoken to. `broker-per-agent-l2.sh` says so in its own words: it proves each broker pod runs
#   and states as a non-claim that it does not prove the broker *serves*, "because no client here
#   holds a certificate". Holding one is not a small thing — the broker demands mTLS with a
#   certificate the `kubeagents-mesh-ca` issued, a projected ServiceAccount token whose audience is
#   `kubeagents-broker`, and the SPIFFE URI in the first to name the ServiceAccount in the second.
#   Nothing in `dev/` could assemble that, so five BLOCKING-ALWAYS rows of 09 §6.2 had no evidence.
#
#   This library assembles it, once, so that any suite can ask a deployed broker a question and get
#   the broker's own answer back.
#
# WHY IT IS A POD AND NOT A PORT-FORWARD
#   `broker_client.py`'s module docstring says the server certificate is verified against
#   `KUBEAGENTS_BROKER_SAN` "not against the host in the URL", which would have licensed a tunnel
#   from the developer's machine. `cfg.san` is read, required by `require()`, and then never used:
#   `build_ssl_context` sets `check_hostname = True` and `urllib` takes `server_hostname` from the
#   URL. Nothing is broken today — the two strings are equal — but a `https://127.0.0.1:PORT`
#   endpoint would fail hostname verification, and there is no client-side override that is not also
#   an override an agent could reach for. (Filed as a finding against the shipped client. A fixture
#   does not fix shipped code.)
#
#   In-cluster is the better answer regardless. The request crosses the real `<agent>-broker`
#   Service, the real `<agent>-broker-ingress` NetworkPolicy and the real `<agent>-to-broker` egress
#   hop. A tunnel touches none of those, and two of them are the subject of other checks.
#
# WHY THE POD RUNS python:3.12-slim AND MOUNTS THE CODE
#   The agent images in Artifact Registry build `FROM nousresearch/hermes-agent` and are days stale;
#   an agent image is also the wrong thing to assert against, because the question is what the
#   *shipped* `agents/platform/scripts/` transport does, and that is in the working tree. So the pod
#   is a stock interpreter and the code arrives in a ConfigMap generated from the tree at run time.
#   The consequence is worth stating plainly: this proves the shipped SOURCE speaks to the deployed
#   broker. It does not prove the published agent IMAGE carries that source — that is P1's job on
#   the agent image and a different row.
#
# NAME RESOLUTION IS SHORT-CIRCUITED, DELIBERATELY, AND IT IS A NON-CLAIM
#   `<agent>-to-broker` makes every reader-labelled pod default-deny on egress with exactly one hole
#   — TCP 8443 to the actor half — and there is no DNS rule in it anywhere. A real agent pod gets
#   DNS from the per-tier install-time egress policy, which `provision_13` applies and a scratch
#   cluster does not carry. Rather than widen a policy under test, the driver pod carries a
#   `hostAliases` entry mapping the broker's SAN to the broker Service's ClusterIP. TLS still
#   verifies that exact name against the certificate, which is the property being tested. What is
#   NOT demonstrated is that cluster DNS publishes the name — that is `broker-per-agent-l2.sh`'s
#   L2-3, which reads the Endpoints the API server computed, and it is already green.
#
# CREDENTIALS THIS MINTS, AND WHY THAT IS ACCEPTABLE ON A SCRATCH CLUSTER ONLY
#   A short-lived token for ANOTHER agent's reader ServiceAccount, passed to the pod in an env var
#   so the broker can be asked to refuse it. It is `kubectl create token --duration=15m` against a
#   ServiceAccount that exists only on the scratch cluster, it grants nothing outside that cluster,
#   and it is the entire subject of V-BRK-010. Callers are guarded to `gke-scratch-*`; this file
#   does not relax that and must never be sourced by anything that does.
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/broker-driver.sh"
#   broker_driver_apply_code      "$K" <ns> <configmap>
#   broker_driver_untrusted_keypair "$K" <ns> <secret>
#   broker_driver_run             "$K" <ns> <agent> <foreign-agent> <pod> <configmap> <secret>
#   broker_driver_delete          "$K" <ns> <pod> <configmap> <secret>
#
#   `broker_driver_run` also counts what the broker minted and refuses to hand back a transcript it
#   cannot account for — see "WHAT THE BROKER MINTED, COUNTED" below. No caller has to ask for it.

# Where each thing lands inside the driver pod. The agent's OWN TLS dir is not here: it is read off
# the rendered Deployment (P6) so the pod mounts the keypair exactly where the shipped client will
# look for it, even if that path changes.
BROKER_DRIVER_CODE_MOUNT=/opt/probe
BROKER_DRIVER_FOREIGN_TLS_DIR=/etc/kage/foreign
BROKER_DRIVER_UNTRUSTED_TLS_DIR=/etc/kage/untrusted
# The pod must reach a terminal phase within this. The probe's own per-request timeout is 30s and it
# makes ten requests, most of which fail fast; a run that exceeds this is stuck, not slow.
BROKER_DRIVER_TIMEOUT="${BROKER_DRIVER_TIMEOUT:-300}"

# WHICH PROBE THE POD RUNS (P9-T9b-5b-i)
#   Everything above is about putting a real caller at the door; what that caller then ASKS is a
#   separate question, and there is now more than one answer. `broker_probe.py` presents ten
#   credentials and asserts on refusals; `broker_execute_probe.py` presents one and asserts on what
#   the journal recorded. Both need the same certificate, the same audience-bound token and the same
#   hostAliases short-circuit, and none of that is worth a second copy.
#
#   Set through `broker_driver_use_probe`, never by assigning these two directly: they must agree
#   (the mounted key is what the command executes) and a suite that set one would get a pod that
#   runs a file which is not there. The default is the original probe, so every existing caller is
#   unchanged by this.
BROKER_DRIVER_PROBE="dev/verify/fixtures/broker_probe.py"
BROKER_DRIVER_PROBE_NAME="broker_probe.py"

# The tenant namespace the probe should aim a write at, when it aims one. Empty for probes that do
# not — `broker_probe.py` targets its own namespace and ignores this.
BROKER_DRIVER_TENANT_NS="${BROKER_DRIVER_TENANT_NS:-}"

# The name the probe should give the object it aims at. Empty means "use the probe's own constant",
# which is what every single-submission caller wants. A caller that runs the same probe repeatedly
# against one cluster MUST vary it per submission: 06 §9's idempotency key is a hash over identity +
# operations + dryRun, so an unvaried target makes every submission after the first the SAME action,
# answered `200 decision=deduplicated` carrying the FIRST submission's actionId ([[LSN-067]]).
BROKER_DRIVER_TARGET_NAME="${BROKER_DRIVER_TARGET_NAME:-}"

# WHAT ELSE THE PROBE IS TOLD (P9-T9b-5b-ii-a)
#   Newline-separated `NAME=VALUE`, appended to the pod's env and empty by default. It exists
#   because `broker_refuse_probe.py` runs the SAME probe twice against two different cluster states
#   and has to be told which run it is on — a knob that is genuinely the caller's, not the driver's.
#
#   Additive rather than a replacement for the fixed list above, and that is the point: every
#   variable a probe needs to reach the broker at all is derived from the rendered Deployment (P6),
#   and a caller able to override those could hand the pod an endpoint the controller never
#   rendered and get a green run against a broker nobody deployed. This list is read AFTER them and
#   cannot displace them — a duplicate key in a Kubernetes env list is a validation error, so an
#   attempt to shadow one fails loudly at apply time instead of quietly at the door.
#
#   `BROKER_DRIVER_TENANT_NS` stays a named variable rather than folding into this. It has three
#   callers and a documented meaning; demoting it to a string in a list would make it unfindable.
BROKER_DRIVER_EXTRA_ENV="${BROKER_DRIVER_EXTRA_ENV:-}"

# WHAT THE BROKER MINTED, COUNTED — distinct(actionId) == submissions ([[LSN-067]])
#   The property in one line: N submissions to a broker must produce N DISTINCT actions. The count of
#   REPLIES is not the count of ACTIONS, and the gap between the two is precisely the interesting
#   failure — a driver that submits N times and hands back a transcript of N answers is satisfied
#   today by a broker that collapsed several submissions onto one action, or that replayed one
#   action's id.
#
#   Measured, not theorised. `brake-l2.sh` submitted eleven envelopes across a fault injection and
#   got eleven replies — one `202 accepted`, ten `200 deduplicated` — all naming ONE actionId. 06 §9's
#   idempotency key is a sha256 over identity + operations + dryRun, so an unvaried target made every
#   submission the SAME action and the broker, correctly, answered the second and later ones with the
#   FIRST one's record. Nine arms were then answered by a record minted before the fault they claimed
#   to measure and passed; two happened to disagree with it and failed. A suite reporting a mix of red
#   and green is the last shape anyone re-reads for "did the instrument observe anything at all".
#
#   It lives HERE and not in a suite, which is the whole of the lesson: every present and future
#   caller gets it without re-authoring it, and a new suite cannot forget to ask.
#
#   WHAT COUNTS AS A SUBMISSION. A transcript line that is a broker reply to an envelope: a non-empty
#   `decision` other than `rejected`, OR a non-empty `actionId`. The union of the two, because the
#   probes do not all spell both — `broker_probe.py` emits neither key, its subject being what the
#   door refuses rather than what the pipeline does. `rejected` is 06 §4.1's refusal shape and
#   `server.go`'s `write()` renders it with NO actionId on purpose (`broker-refuse-l2.sh`'s A-2
#   asserts exactly that), so a refusal is not a submission that produced an action and is not
#   counted as one.
#
#   WHAT COUNTS AS AN ACTION. A distinct, non-empty `actionId` on a reply whose decision is NOT
#   `deduplicated`. A dedup mints nothing: by the broker's own word it is handing back an earlier
#   submission's record. That one rule catches both halves of the lesson — the eleven-into-one
#   collapse above (11 submissions, 1 action), and the case a bare count would miss, a suite RE-RUN
#   inside `antireplay.go`'s 24-hour `ReplayWindow`, where each submission dedups onto the PREVIOUS
#   run's id and `distinct == submissions` would be satisfied by records nobody minted today.
#
#   AN UNREADABLE actionId IS A FAILURE, NOT A SKIPPED ROW. A counted submission whose reply carries
#   no actionId adds to the denominator and to nothing else, so the arithmetic goes red on it and the
#   row is printed by name: a response the driver cannot read is exactly a response it cannot count.
#   (A line that is not a JSON object is not a reply at all — `kubectl logs` carries the pod's stderr
#   in the same stream, and a traceback counted as a submission would be a red nobody could read.)
#
#   ZERO SUBMISSIONS IS NOT A PASS AND CANNOT BE REPORTED AS ONE ([[LSN-035]]/[[LSN-038]]). The empty
#   set is handled structurally rather than by a verdict: there is no path through
#   `broker_driver_assert_distinct_actions` that prints `OK` over an empty ledger — it prints
#   `NOT-EVALUATED`, which is a named absence and never evidence. It is not made fatal, and that is
#   reasoned rather than lenient: two callers reach it legitimately. `broker-auth-l2.sh` runs
#   `broker_probe.py`, whose transcript carries no `actionId` key at all; and `broker-refuse-l2.sh`'s
#   first two runs are refusals by design, its only accepting submission being the third. The ledger
#   is CUMULATIVE across every run in one suite process for that second reason — a zero that is
#   transient in run 1 is settled by run 3 — and for the first: the eleven-into-one collapse is
#   invisible inside any single run, where one submission and one action always agree.
#
#   ONCE BROKEN, IT STAYS BROKEN. The ledger is never rewound, so the run after a mismatch fails on
#   the same arithmetic. A suite that has already measured one action twice does not recover by
#   submitting a third time.
#
#   WHY A FILE AND NOT A SHELL COUNTER. Every caller invokes `broker_driver_run` inside a command
#   substitution — `out="$(broker_driver_run ...)"` — which is a SUBSHELL, so a variable incremented
#   in the function is discarded the moment it returns; [[LSN-064]] and [[LSN-065]] are the same shape
#   one pipeline over. The path is minted ONCE, here, at source time, in the caller's own shell, so
#   every subshell inherits the same path and appends to the same file. An inherited value is honoured
#   (an L2 fixture can point this at somewhere it can read afterwards) and truncated before use, so a
#   stale file is never counted as this run's.
#
#   A path that cannot be written is emptied here rather than diagnosed here, and the complaint is
#   made at run time by `broker_driver_record_actions` — sourcing a library is not where a suite
#   expects to be told anything, and an empty value is a state that function already has to handle.
BROKER_DRIVER_LEDGER="${BROKER_DRIVER_LEDGER:-}"
if [ -z "$BROKER_DRIVER_LEDGER" ]; then
  BROKER_DRIVER_LEDGER="$(mktemp "${TMPDIR:-/tmp}/broker-driver-actions.XXXXXX" 2>/dev/null)" ||
    BROKER_DRIVER_LEDGER=""
else
  # `2>/dev/null` FIRST: redirections are applied left to right, and a `>` that fails after it would
  # print the shell's own "No such file or directory" onto the suite's transcript.
  : 2>/dev/null >"$BROKER_DRIVER_LEDGER" || BROKER_DRIVER_LEDGER=""
fi

# broker_driver_use_probe <path-relative-to-repo-root>
#   Both halves at once. rc 1 if the file is not there, which is the whole reason this is a function
#   and not two assignments: a typo'd path would otherwise surface as a pod that exits 2 with
#   "can't open file", four minutes after the suite started.
broker_driver_use_probe() {
  local rel="$1" root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  if [ ! -f "$root/$rel" ]; then
    echo "broker_driver_use_probe: no probe at $rel (relative to $root)" >&2
    return 1
  fi
  BROKER_DRIVER_PROBE="$rel"
  BROKER_DRIVER_PROBE_NAME="${rel##*/}"
}

# broker_driver_env <kubectl-cmd> <namespace> <agent> <ENV_NAME>
#   One value from the RENDERED agent Deployment (P6), never reconstructed from the naming
#   functions. `brokerEndpoint`, `brokerSAN` and `agentIdentity` all exist in Go and would each be a
#   second implementation here; worse, a driver that computed its own endpoint would still pass on a
#   cluster whose controller rendered a different one, which is the failure this whole unit exists
#   to make visible.
#
#   The container is selected by which one CARRIES the variable rather than by name: the agent
#   container's name is a literal in `agent_manifests.go` and reproducing it here would be one more
#   thing to keep in step.
broker_driver_env() {
  local K="$1" ns="$2" agent="$3" name="$4"
  $K -n "$ns" get "deploy/${agent}-gateway" \
    -o jsonpath="{.spec.template.spec.containers[*].env[?(@.name==\"${name}\")].value}" 2>/dev/null
}

# broker_driver_reader_sa <kubectl-cmd> <namespace> <agent>
#   The identity the broker expects, read off the CR the same way `readerServiceAccountName` reads
#   it: `spec.security.serviceAccountName`, else the CR's own name.
broker_driver_reader_sa() {
  local K="$1" ns="$2" agent="$3" sa
  sa="$($K -n "$ns" get "agent/$agent" -o jsonpath='{.spec.security.serviceAccountName}' 2>/dev/null)"
  [ -n "$sa" ] || sa="$agent"
  printf '%s' "$sa"
}

# broker_driver_apply_code <kubectl-cmd> <namespace> <configmap>
#   The two SHIPPED transport modules plus the probe, from the working tree. `agents/platform` is the
#   canonical copy: the three tiers' `scripts/` directories are byte-identical for these files today
#   and `dev/test_agent_script_parity.py` is what holds them that way — this reads one of them rather
#   than picking a tier, because a driver that read the tier under test would agree with it by
#   construction.
broker_driver_apply_code() {
  local K="$1" ns="$2" cm="$3" root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  $K -n "$ns" create configmap "$cm" \
    --from-file=broker_client.py="$root/agents/platform/scripts/broker_client.py" \
    --from-file=action_envelope.py="$root/agents/platform/scripts/action_envelope.py" \
    --from-file="$BROKER_DRIVER_PROBE_NAME=$root/$BROKER_DRIVER_PROBE" \
    --dry-run=client -o yaml | $K apply -f - >/dev/null || return 1
}

# broker_driver_untrusted_keypair <kubectl-cmd> <namespace> <secret>
#   A self-signed client certificate the mesh CA never saw, for the "wrong-CA client" arm of
#   V-BRK-007. Generated locally with openssl and thrown away with the Secret; it authenticates
#   nothing, anywhere, and its only purpose is to be rejected during a handshake.
#
#   The subject deliberately spells a plausible SPIFFE URI. A certificate that was refused for having
#   no SAN at all would be refused one layer earlier than the one being measured, and the check would
#   pass without ever exercising chain verification.
broker_driver_untrusted_keypair() {
  local K="$1" ns="$2" secret="$3" tmp rc=0
  tmp="$(mktemp -d)" || return 1
  cat >"$tmp/openssl.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = ext
[dn]
CN = untrusted-client
[ext]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = URI:spiffe://cluster.local/ns/kubeagents-system/sa/not-a-real-agent
CNF
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 \
    -keyout "$tmp/tls.key" -out "$tmp/tls.crt" -config "$tmp/openssl.cnf" >/dev/null 2>&1 || rc=1
  if [ "$rc" -eq 0 ]; then
    $K -n "$ns" create secret generic "$secret" \
      --from-file=tls.crt="$tmp/tls.crt" --from-file=tls.key="$tmp/tls.key" \
      --dry-run=client -o yaml | $K apply -f - >/dev/null || rc=1
  fi
  rm -rf "$tmp"
  return "$rc"
}

# broker_driver_record_actions <transcript>
#   One ledger row per submission in this transcript — `<decision>\t<actionId>\t<scenario>` — appended
#   to the cumulative ledger. The classification is the one written down above.
#   rc 0 = recorded, which includes recording nothing when the transcript holds no submission ·
#   1 = could not record, and that is fatal to the run: a driver that cannot account for what it
#   submitted must not hand back a transcript that looks accounted for.
broker_driver_record_actions() {
  local transcript="${1:-}" rows=""
  if [ -z "$BROKER_DRIVER_LEDGER" ]; then
    echo "broker_driver_record_actions: there is no writable ledger — mktemp failed when this library" >&2
    echo "  was sourced, or an inherited BROKER_DRIVER_LEDGER path could not be written. Nothing can" >&2
    echo "  count distinct(actionId) == submissions, so no run can be accounted for. [[LSN-067]]" >&2
    return 1
  fi
  # python3 is LAST in the pipeline, so the rc tested here is python's and not printf's ([[LSN-064]]),
  # and the capture is a command substitution rather than a pipeline component, so the value survives
  # ([[LSN-065]]). python3 is what every caller already uses to read this same transcript — no new
  # dependency, and there is no `jq` anywhere under dev/.
  rows="$(printf '%s\n' "$transcript" | python3 -c '
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue  # kubectl logs carries the pod stderr too; a traceback is not a broker reply
    try:
        reply = json.loads(line)
    except ValueError:
        continue
    if not isinstance(reply, dict):
        continue
    decision = str(reply.get("decision") or "").strip()
    action = str(reply.get("actionId") or "").strip()
    if not action and decision in ("", "rejected"):
        continue  # a probe diagnostic, or the 06 section 4.1 refusal shape: no action was minted
    scenario = str(reply.get("scenario") or "")
    print("\t".join(v.replace("\t", " ") for v in (decision, action, scenario)))
')" || {
    echo "broker_driver_record_actions: could not read the transcript (python3 rc $?); the run" >&2
    echo "  cannot be counted, so it is reported as could-not-run rather than as evidence." >&2
    return 1
  }
  [ -n "$rows" ] || return 0
  printf '%s\n' "$rows" >>"$BROKER_DRIVER_LEDGER" || {
    echo "broker_driver_record_actions: could not append to the ledger $BROKER_DRIVER_LEDGER." >&2
    return 1
  }
}

# broker_driver_assert_distinct_actions
#   The property, over every submission this suite process has made so far. Exposed by name so a
#   caller may re-assert it at teardown; `broker_driver_run` already runs it after every run, which is
#   what makes it unforgettable.
#   rc 0 = it holds over a NON-EMPTY set, or there was nothing to evaluate (said so, never a pass) ·
#   1 = fewer distinct actions than submissions, or the ledger could not be read.
broker_driver_assert_distinct_actions() {
  local counts="" n=0 distinct=0 dedups=0 blanks=0
  if [ -z "$BROKER_DRIVER_LEDGER" ] || [ ! -f "$BROKER_DRIVER_LEDGER" ]; then
    echo "broker-driver LSN-067 action-count: NOT-EVALUATED — no ledger at" >&2
    echo "  '${BROKER_DRIVER_LEDGER:-<unset>}', so nothing could be counted. This is not a pass." >&2
    return 1
  fi
  # awk holds the id set: bash 3.2 on macOS has no associative arrays and no `mapfile`, and the whole
  # question is "how many DISTINCT ids", which is one awk idiom and no shell loop.
  counts="$(awk -F'\t' '
    { n++ }
    $1 == "deduplicated" { dd++; next }
    $2 == "" { blank++; next }
    !seen[$2]++ { d++ }
    END { printf "%d %d %d %d\n", n + 0, d + 0, dd + 0, blank + 0 }
  ' "$BROKER_DRIVER_LEDGER")" || {
    echo "broker-driver LSN-067 action-count: NOT-EVALUATED — the ledger $BROKER_DRIVER_LEDGER could" >&2
    echo "  not be read. This is not a pass." >&2
    return 1
  }
  IFS=' ' read -r n distinct dedups blanks <<EOF
$counts
EOF

  if [ "${n:-0}" -eq 0 ]; then
    # THE EMPTY SET IS NEVER THE PASS BRANCH. It cannot reach one: this arm returns before the
    # comparison exists. See the header for the two callers that reach it legitimately.
    echo "broker-driver LSN-067 action-count: NOT-EVALUATED — 0 submissions so far in this suite;" >&2
    echo "  no transcript line carried an actionId or a decision other than 'rejected'. NOT a pass:" >&2
    echo "  an assertion satisfied by an empty set is not evidence." >&2
    return 0
  fi

  if [ "$distinct" -eq "$n" ]; then
    echo "broker-driver LSN-067 action-count: OK — $n submission(s), $distinct distinct actionId(s)." >&2
    return 0
  fi

  echo "broker-driver LSN-067 action-count: MISMATCH — $n submission(s) produced only $distinct" >&2
  echo "  distinct actionId(s) ($dedups answered 'deduplicated', $blanks carried no actionId at all)." >&2
  echo "  N submissions must mint N distinct actions. They did not, so some arm below is about to be" >&2
  echo "  answered by a record minted by a DIFFERENT submission — the failure is the instrument's, not" >&2
  echo "  the broker's, and every verdict measured against it is void ([[LSN-067]])." >&2
  echo "  Three things produce this, in the order they are worth checking:" >&2
  echo "    1. the same envelope submitted twice. 06 §9's idempotency key is a hash over identity +" >&2
  echo "       operations + dryRun, so vary the operations per submission — BROKER_DRIVER_TARGET_NAME" >&2
  echo "       is the knob, and brake-l2.sh's submit() is the worked example." >&2
  echo "    2. a RE-RUN of this suite inside antireplay.go's 24h ReplayWindow, dedup'ing onto the ids" >&2
  echo "       the previous run minted. Restart the broker pod, or vary the target names per run." >&2
  echo "    3. a reply the broker sent with no actionId on a decision that should carry one." >&2
  echo "  Every submission recorded so far:" >&2
  awk -F'\t' '{
    printf "    #%d decision=%s actionId=%s scenario=%s\n", NR,
      ($1 == "" ? "<none>" : $1), ($2 == "" ? "<MISSING>" : $2), ($3 == "" ? "<none>" : $3)
  }' "$BROKER_DRIVER_LEDGER" >&2
  return 1
}

# broker_driver_run <kubectl-cmd> <ns> <agent> <foreign-agent> <pod> <configmap> <untrusted-secret>
#   Render the pod, wait for it to finish, and print its stdout. One JSON object per line, which is
#   the probe's output contract; the caller asserts on those lines.
#   rc 0 = the pod ran to completion, its logs are on stdout, and every submission in them minted its
#   own action · 1 = it could not be run, or it could not be accounted for.
broker_driver_run() {
  local K="$1" ns="$2" agent="$3" foreign="$4" pod="$5" cm="$6" untrusted="$7"

  local endpoint san identity token_file tls_dir
  endpoint="$(broker_driver_env "$K" "$ns" "$agent" KUBEAGENTS_BROKER_ENDPOINT)"
  san="$(broker_driver_env "$K" "$ns" "$agent" KUBEAGENTS_BROKER_SAN)"
  identity="$(broker_driver_env "$K" "$ns" "$agent" KUBEAGENTS_AGENT_IDENTITY)"
  token_file="$(broker_driver_env "$K" "$ns" "$agent" KUBEAGENTS_BROKER_TOKEN_FILE)"
  tls_dir="$(broker_driver_env "$K" "$ns" "$agent" KUBEAGENTS_BROKER_TLS_DIR)"
  if [ -z "$endpoint" ] || [ -z "$san" ] || [ -z "$identity" ] || [ -z "$token_file" ] || [ -z "$tls_dir" ]; then
    echo "broker_driver_run: ${agent}-gateway carries no broker wiring (endpoint='$endpoint' san='$san'" >&2
    echo "  identity='$identity' token='$token_file' tls='$tls_dir'). This controller did not render a" >&2
    echo "  broker-aware agent Deployment; there is nothing to drive." >&2
    return 1
  fi
  local token_dir="${token_file%/*}"

  # The address the SAN is pinned to. Not DNS: see the header.
  local broker_ip
  broker_ip="$($K -n "$ns" get "svc/${agent}-broker" -o jsonpath='{.spec.clusterIP}' 2>/dev/null)"
  if [ -z "$broker_ip" ] || [ "$broker_ip" = "None" ]; then
    echo "broker_driver_run: Service ${agent}-broker has no ClusterIP; the broker is not deployed." >&2
    return 1
  fi

  local reader_sa foreign_sa foreign_token
  reader_sa="$(broker_driver_reader_sa "$K" "$ns" "$agent")"
  foreign_sa="$(broker_driver_reader_sa "$K" "$ns" "$foreign")"
  # 15 minutes: long enough that a slow image pull does not turn V-BRK-010 into an expiry test,
  # short enough that a forgotten scratch pod is not carrying a usable token tomorrow.
  foreign_token="$($K -n "$ns" create token "$foreign_sa" --audience=kubeagents-broker --duration=15m 2>/dev/null)"
  if [ -z "$foreign_token" ]; then
    echo "broker_driver_run: could not mint a token for $foreign_sa; the V-BRK-010 arm has no subject." >&2
    return 1
  fi

  # `BROKER_DRIVER_EXTRA_ENV`, rendered. Validated rather than interpolated blind: the heredoc below
  # is unquoted, so a value carrying a `"` would close the YAML string and a value carrying a `$`
  # would be expanded by the shell before the API server ever saw it. Both produce a pod that is
  # wrong in a way the logs do not explain, so both are refused here with the offending line named.
  local extra_yaml="" kv
  while IFS= read -r kv; do
    case "$kv" in '' | \#*) continue ;; esac
    case "$kv" in
      [A-Za-z_]*=*) ;;
      *)
        echo "broker_driver_run: BROKER_DRIVER_EXTRA_ENV line is not NAME=VALUE: $kv" >&2
        return 1
        ;;
    esac
    case "${kv%%=*}" in
      *[!A-Za-z0-9_]*)
        echo "broker_driver_run: BROKER_DRIVER_EXTRA_ENV name is not an env identifier: ${kv%%=*}" >&2
        return 1
        ;;
    esac
    case "${kv#*=}" in
      *['"$\`']*)
        echo "broker_driver_run: BROKER_DRIVER_EXTRA_ENV value for ${kv%%=*} carries a quote, dollar or backslash;" >&2
        echo "  the pod manifest is a shell heredoc and cannot carry one safely." >&2
        return 1
        ;;
    esac
    extra_yaml="$extra_yaml
        - name: ${kv%%=*}
          value: \"${kv#*=}\""
  done <<EOF
$BROKER_DRIVER_EXTRA_ENV
EOF

  $K -n "$ns" delete pod "$pod" --ignore-not-found --wait=true >/dev/null 2>&1

  # `restartPolicy: Never` and no retries: a probe that ran twice would present the same nonce
  # twice, and a replay refusal on the second run is not the answer any of these rows is asking for.
  #
  # The labels are the pair selector and nothing else. No `kube-agents/tier`: this pod is not an
  # agent, the tier policies are not written for it, and claiming a tier it does not have would put
  # it under admission rules whose subject is a different thing.
  $K apply -f - >/dev/null <<YAML || { echo "broker_driver_run: could not create pod $pod" >&2; return 1; }
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: $ns
  labels:
    kube-agents/agent: $agent
    kube-agents/role: reader
spec:
  restartPolicy: Never
  serviceAccountName: $reader_sa
  automountServiceAccountToken: true
  hostAliases:
    - ip: "$broker_ip"
      hostnames: ["$san"]
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: python:3.12-slim
      command: ["python3", "$BROKER_DRIVER_CODE_MOUNT/$BROKER_DRIVER_PROBE_NAME"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      env:
        - name: PYTHONPATH
          value: "$BROKER_DRIVER_CODE_MOUNT"
        - name: PYTHONDONTWRITEBYTECODE
          value: "1"
        - name: HOME
          value: /tmp
        - name: KUBEAGENTS_BROKER_ENDPOINT
          value: "$endpoint"
        - name: KUBEAGENTS_BROKER_SAN
          value: "$san"
        - name: KUBEAGENTS_AGENT_IDENTITY
          value: "$identity"
        - name: KUBEAGENTS_BROKER_TOKEN_FILE
          value: "$token_file"
        - name: KUBEAGENTS_BROKER_TLS_DIR
          value: "$tls_dir"
        - name: PROBE_NAMESPACE
          value: "$ns"
        - name: PROBE_TENANT_NAMESPACE
          value: "$BROKER_DRIVER_TENANT_NS"
        # Empty for every caller that submits once, which leaves the probe's historical constant in
        # place. A caller that submits the SAME probe more than once per cluster must vary this, or
        # 06 §9's idempotency key is identical and the broker answers the second submission with the
        # first one's record ([[LSN-067]]).
        - name: PROBE_TARGET_NAME
          value: "$BROKER_DRIVER_TARGET_NAME"
        - name: PROBE_FOREIGN_TLS_DIR
          value: "$BROKER_DRIVER_FOREIGN_TLS_DIR"
        - name: PROBE_UNTRUSTED_TLS_DIR
          value: "$BROKER_DRIVER_UNTRUSTED_TLS_DIR"
        - name: PROBE_DEFAULT_AUDIENCE_TOKEN_FILE
          value: /var/run/secrets/kubernetes.io/serviceaccount/token
        - name: PROBE_FOREIGN_TOKEN
          value: "$foreign_token"$extra_yaml
      volumeMounts:
        - name: code
          mountPath: $BROKER_DRIVER_CODE_MOUNT
          readOnly: true
        - name: mesh-tls
          mountPath: $tls_dir
          readOnly: true
        - name: foreign-tls
          mountPath: $BROKER_DRIVER_FOREIGN_TLS_DIR
          readOnly: true
        - name: untrusted-tls
          mountPath: $BROKER_DRIVER_UNTRUSTED_TLS_DIR
          readOnly: true
        - name: broker-token
          mountPath: $token_dir
          readOnly: true
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: code
      configMap:
        name: $cm
    - name: mesh-tls
      secret:
        secretName: ${agent}-mesh-tls
    - name: foreign-tls
      secret:
        secretName: ${foreign}-mesh-tls
    - name: untrusted-tls
      secret:
        secretName: $untrusted
    - name: broker-token
      projected:
        sources:
          - serviceAccountToken:
              path: ${token_file##*/}
              audience: kubeagents-broker
              expirationSeconds: 3600
    - name: tmp
      emptyDir: {}
YAML

  # POLLED, never slept on (P10's sibling precondition P9). `.status.phase` on a pod is kubelet-
  # written state, and a fixed sleep followed by one read is the shape that produces a flake nobody
  # can reproduce.
  local deadline=$((SECONDS + BROKER_DRIVER_TIMEOUT)) phase=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    phase="$($K -n "$ns" get "pod/$pod" -o jsonpath='{.status.phase}' 2>/dev/null)"
    case "$phase" in
      Succeeded | Failed) break ;;
    esac
    sleep 3
  done
  case "$phase" in
    Succeeded | Failed) ;;
    *)
      echo "broker_driver_run: pod $pod did not finish within ${BROKER_DRIVER_TIMEOUT}s (phase='${phase:-unknown}')." >&2
      $K -n "$ns" describe "pod/$pod" 2>&1 | tail -20 >&2
      return 1
      ;;
  esac

  # Captured rather than streamed, because the transcript is now read twice: once by the caller, and
  # once here, to count what the broker minted. The caller's copy is written FIRST and unchanged —
  # stdout is its parse input and everything the accounting says goes to stderr, like every other
  # message in this file.
  local transcript="" logs_rc=0
  transcript="$($K -n "$ns" logs "pod/$pod" 2>/dev/null)" || logs_rc=$?
  printf '%s\n' "$transcript"
  if [ "$logs_rc" -ne 0 ]; then
    echo "broker_driver_run: could not read the logs of pod $pod (kubectl rc $logs_rc)." >&2
    return 1
  fi

  # distinct(actionId) == submissions, over every run this suite process has made. [[LSN-067]].
  broker_driver_record_actions "$transcript" || return 1
  broker_driver_assert_distinct_actions || return 1
}

# broker_driver_delete <kubectl-cmd> <namespace> <pod> <configmap> <untrusted-secret>
#   Everything this library created, and nothing it did not. The agent's own mesh Secret, the reader
#   ServiceAccount and the Agent CRs belong to whoever seeded them.
#
#   The submission ledger goes too — every caller calls this from a cleanup trap, so it is the one
#   place a temp file this library minted is certain to be reached. Its contents have already been
#   printed by whichever assertion cared: nothing is lost by deleting it, and a /tmp accumulating one
#   file per L2 run is a thing nobody would notice until it mattered.
broker_driver_delete() {
  local K="$1" ns="$2" pod="$3" cm="$4" secret="$5"
  $K -n "$ns" delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1
  $K -n "$ns" delete configmap "$cm" --ignore-not-found --wait=false >/dev/null 2>&1
  $K -n "$ns" delete secret "$secret" --ignore-not-found --wait=false >/dev/null 2>&1
  [ -n "$BROKER_DRIVER_LEDGER" ] && rm -f "$BROKER_DRIVER_LEDGER" 2>/dev/null
  return 0
}
