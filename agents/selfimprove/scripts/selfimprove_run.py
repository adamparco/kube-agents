#!/usr/bin/env python3
"""One self-improvement run: establish what is deployed, investigate it, grade, file.

This is the CronJob's entrypoint. It is deliberately not the agent entrypoint --
`docker-entrypoint.sh` scaffolds profiles onto a PVC, starts a gateway and waits,
which is the shape of the thing being observed rather than of the observer. The
runner does the opposite: it builds a private Hermes home on an emptyDir, takes
one headless agent turn, writes what it learned to the ledger and exits, so the
Job completes and `concurrencyPolicy: Forbid` can do its job.

The order is fixed and each step can refuse:

1. **Identity.** Which commit is the pod under observation running? Everything
   downstream is unfalsifiable without this -- a finding written against `main`
   about a pod running a three-week-old image describes code that is not there.
   Answered by build-info.json, stamped into the image at build time, and
   cross-checked against the live Deployment. A mismatch aborts: it means the
   agent was rolled and the CronJob was not.
2. **Source.** The repository at that revision, into the emptyDir.
3. **Investigate.** One `hermes -z` turn, handed the brief below and the
   read-only evidence tools of selfimprove_evidence.py.
4. **Grade and gate.** The agent's findings are merged into the ledger, which
   owns the occurrence counts; the gate (sec. 7.3) decides which are promoted.
5. **File.** In fork/upstream mode, one further agent turn per promoted finding
   opens the pull request. In report-only -- the default -- nothing leaves the
   cluster and the ledger is the whole output.

See docs/designs/self-improvement.md for why each of those is shaped this way.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selfimprove_ledger as ledger_mod  # noqa: E402

BUILD_INFO_PATH = "/opt/build-info.json"
TEMPLATE_DIR = "/opt/selfimprove"
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
HERMES_TREE = "/opt/hermes"

DEFAULT_UPSTREAM = "gke-labs/kube-agents"

#: Wall clock at import, which is within a second of the container starting and
#: therefore of the clock `activeDeadlineSeconds` is measured against.
RUN_STARTED = time.time()

#: Seconds held back from the deadline for the ledger write and the final log.
#: The ledger is the run's entire output in report-only mode, so being killed
#: while holding it is the one failure that makes the whole hour worthless --
#: the findings were computed, the counts were incremented in memory, and none
#: of it reached the ConfigMap.
DEADLINE_RESERVE_SECONDS = 90

#: Below this there is no point starting another agent turn: it cannot get
#: through a tool call and a reply, and a turn killed halfway still costs the
#: tokens it spent.
MIN_TURN_SECONDS = 120


def log(message: str) -> None:
    print("[selfimprove] %s" % message, flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def seconds_left(deadline: int) -> Optional[int]:
    """How much of `activeDeadlineSeconds` is left, minus the ledger reserve.

    `None` when no deadline was supplied, meaning "unbounded" -- the caller then
    uses its configured timeout unmodified.

    This exists because the two budgets are configured independently and their
    defaults already conflict: investigateTimeoutSeconds 3000 plus
    fileTimeoutSeconds 900 for each of up to maxPullRequestsPerDay findings is
    4800 seconds against an activeDeadlineSeconds of 3600. The kubelet wins that
    argument, and it wins it by SIGKILLing the pod at a moment nothing chose --
    most expensively, after the investigation has been paid for and before the
    ledger has been written. Rather than making the chart do arithmetic over a
    finding count it cannot know at render time, the runner measures.
    """
    if deadline <= 0:
        return None
    return int(deadline - (time.time() - RUN_STARTED) - DEADLINE_RESERVE_SECONDS)


def budgeted(configured: int, deadline: int) -> int:
    """`configured`, clamped to what is actually left before the deadline."""
    remaining = seconds_left(deadline)
    if remaining is None:
        return configured
    return max(0, min(configured, remaining))


# --------------------------------------------------------------------------
# 1. Identity: what is actually deployed
# --------------------------------------------------------------------------


def read_build_info() -> Dict[str, Any]:
    """The revision stamp the image carries.

    Written by deploy/docker/Dockerfile from the GIT_SHA build argument. A build
    that did not pass one -- a bare `docker build`, or the dev-rebuild path
    before it was taught to -- leaves `revision` empty, which is a refusal
    rather than a guess (sec. 11).
    """
    try:
        with open(BUILD_INFO_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _kube_client():
    from kubernetes import client, config as kube_config  # noqa: PLC0415

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover - only outside a pod
        kube_config.load_kube_config()
    return client


def observed_images(namespace: str, deployment: str) -> Tuple[Optional[str], List[str]]:
    """The agent container image the live Deployment is running, and every image in it."""
    try:
        client = _kube_client()
    except Exception as exc:  # no client at all: no in-cluster config, no kubeconfig
        log("no Kubernetes client (%s); skipping the image cross-check" % exc)
        return None, []
    apps = client.AppsV1Api()
    try:
        dep = apps.read_namespaced_deployment(name=deployment, namespace=namespace)
    except client.exceptions.ApiException as exc:
        log("cannot read Deployment %s/%s (%s); skipping the image cross-check" % (namespace, deployment, exc.status))
        return None, []
    containers = dep.spec.template.spec.containers
    images = [c.image for c in containers]
    primary = None
    for container in containers:
        if container.name in ("platform-agent", "agent"):
            primary = container.image
            break
    return primary or (images[0] if images else None), images


def own_image(namespace: str) -> Optional[str]:
    """This pod's own runner-container image, read from the API rather than assumed.

    The operator answers the same question the same way -- it reads its own Pod
    to set OPERATOR_IMAGE -- so this is a pattern the codebase already has. The
    downward API cannot supply an image, which is why this is an API read and
    not an env var: an env var would say what the chart *intended* to schedule,
    and the whole point of the check is to catch the case where that is no
    longer what is running.
    """
    pod_name = env("POD_NAME")
    if not pod_name:
        return None
    try:
        client = _kube_client()
    except Exception:
        return None
    core = client.CoreV1Api()
    try:
        pod = core.read_namespaced_pod(name=pod_name, namespace=namespace)
    except client.exceptions.ApiException:
        return None
    for container in pod.spec.containers:
        if container.name == "runner":
            return container.image
    return pod.spec.containers[0].image if pod.spec.containers else None


def resolve_revision(namespace: str, deployment: str, allow_fallback: bool) -> Dict[str, Any]:
    info = read_build_info()
    revision = str(info.get("revision") or "").strip()
    runner_image = own_image(namespace)
    agent_image, all_images = observed_images(namespace, deployment)

    # `git describe --dirty` appends `-dirty` when the tree had uncommitted
    # changes at build time. That suffix is not a ref -- codeload would 404 on
    # it -- so the fetch uses the base commit, but the base commit is by
    # definition NOT what is running. Recorded rather than quietly stripped: the
    # investigation has to be told, because on a dirty build the source it reads
    # and the code the pod executes are known to differ, and a finding that
    # cites a line number is then citing the wrong file.
    dirty = revision.endswith("-dirty")
    result = {
        "revision": revision,
        "fetch_ref": revision[: -len("-dirty")] if dirty else revision,
        "dirty": dirty,
        "build_info": info,
        "runner_image": runner_image,
        "agent_image": agent_image,
        "deployment_images": all_images,
        "stamped": bool(revision),
        "image_match": None,
        "refuse": None,
    }

    if runner_image and agent_image:
        result["image_match"] = runner_image == agent_image
        if not result["image_match"]:
            result["refuse"] = (
                "the runner is on %s and the agent Deployment is on %s. The CronJob and the "
                "agent have diverged, so anything found here would be attributed to the wrong "
                "code. Re-render the chart at the deployed image, or roll the agent."
                % (runner_image, agent_image)
            )
            return result

    if not revision:
        if allow_fallback:
            result["revision"] = env("SELFIMPROVE_FALLBACK_REF", "main")
            result["fetch_ref"] = result["revision"]
            result["stamped"] = False
        else:
            result["refuse"] = (
                "the image carries no revision stamp (%s has no `revision`), so the loop cannot "
                "establish which commit is running. Rebuild with --build-arg GIT_SHA=<sha>, or "
                "set selfImprovement.allowUnstampedImage=true to investigate against a named ref "
                "and accept that every finding may cite code the pod is not running."
                % BUILD_INFO_PATH
            )
    return result


# --------------------------------------------------------------------------
# 2. Source at that revision
# --------------------------------------------------------------------------


def fetch_source(repo: str, ref: str, dest: str, timeout: int = 180) -> Optional[str]:
    """Unpack a repository at a ref into dest, over anonymous HTTPS.

    A tarball rather than `git clone`, and the reason is the image: there is no
    git in the agent image outside the credential-proxy shims, so a clone in
    report-only mode -- which renders no proxy -- would need a credential path
    the mode exists to not have. The tarball is byte-identical to a checkout at
    that commit, which is what the investigation reads; what it gives up is
    history, so a finding that needs `git log` or blame belongs to a run in a
    mode that has the proxy.
    """
    url = "https://codeload.github.com/%s/tar.gz/%s" % (repo, ref)
    log("fetching %s" % url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        log("could not fetch %s: %s" % (url, exc))
        return None
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        # The archive is one top-level directory, <repo>-<ref>.
        members = tar.getmembers()
        top = members[0].name.split("/")[0] if members else ""
        _safe_extract(tar, dest)
    root = os.path.join(dest, top)
    return root if os.path.isdir(root) else None


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    """Extract, refusing any member that would land outside dest.

    The archive comes from GitHub over TLS, so this is not the threat it would
    be for an arbitrary upload -- but a path-traversal guard on an extract the
    runner performs as root-adjacent is cheap, and its absence is the kind of
    thing this loop is supposed to find in other people's code.
    """
    base = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (target == base or target.startswith(base + os.sep)):
            raise RuntimeError("refusing tar member outside the destination: %r" % member.name)
        if member.issym() or member.islnk():
            link_target = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
            if not (link_target == base or link_target.startswith(base + os.sep)):
                raise RuntimeError("refusing link member outside the destination: %r" % member.name)
    # `data` is the stricter of the two stdlib filters -- it rejects absolute
    # paths, links escaping the destination, device nodes and setuid bits -- so
    # it subsumes the loop above rather than replacing it. Both run: the loop
    # gives a message naming the offending member, and the filter covers the
    # cases it does not think of. Passed explicitly because it becomes the
    # default in Python 3.14 and is a DeprecationWarning until then; relying on
    # the version would make the hardening depend on a base-image bump.
    try:
        tar.extractall(dest, filter="data")  # noqa: S202 - every member was checked above
    except TypeError:  # pragma: no cover - Python without the filter argument
        tar.extractall(dest)  # noqa: S202 - every member was checked above


def hermes_pin(source_root: Optional[str]) -> str:
    """The Hermes base-image tag this build was made from, out of tags.env."""
    if not source_root:
        return ""
    path = os.path.join(source_root, "tags.env")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("HERMES_AGENT_TAG="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------
# 3. The Hermes home and the brief
# --------------------------------------------------------------------------


def scaffold_home(home: str) -> None:
    """Build the runner's private profile on the emptyDir.

    Copied from the image rather than merged onto a volume, because there is no
    volume: every run starts from the template and nothing it writes survives
    except the ledger. That is the property that makes the loop safe to leave on
    -- a run cannot accumulate state that changes how the next one behaves.
    """
    os.makedirs(home, exist_ok=True)
    for name in ("SOUL.md", "AGENTS.md", "config.yaml"):
        src = os.path.join(TEMPLATE_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(home, name))
    skills_src = os.path.join(TEMPLATE_DIR, "skills")
    if os.path.isdir(skills_src):
        shutil.copytree(skills_src, os.path.join(home, "skills"), dirs_exist_ok=True)
    for sub in ("logs", "sessions", "memories", "cache"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)


def build_brief(
    identity: Dict[str, Any],
    source_root: Optional[str],
    harness_pin: str,
    signals: List[str],
    ledger: Dict[str, Any],
    findings_path: str,
    namespace: str,
    mode: str,
) -> str:
    revision = identity["revision"]
    if not identity["stamped"]:
        stamp_note = (
            "WARNING: the image carries no revision stamp. The source below is %s, which may not be "
            "the code the pod is running. Say so in every finding you record." % revision
        )
    elif identity.get("dirty"):
        stamp_note = (
            "WARNING: this image was built from a MODIFIED working tree. The source below is the "
            "base commit %s, and the pod is running that plus uncommitted changes you cannot see. "
            "Line numbers and file contents may not match. Treat anything you find as provisional "
            "and say in the finding that it was observed against a dirty build."
            % identity["fetch_ref"]
        )
    else:
        stamp_note = "The image is revision-stamped, so this is the commit the observed pod is running."
    # No upstream Hermes checkout is fetched. nousresearch/hermes-agent is not
    # reachable anonymously the way this repository is, and adding a credential
    # for it would put a second GitHub identity into report-only mode -- the one
    # mode whose whole claim is that it has none. The attribution the design
    # wants is still available without it, because the executing tree and the
    # complete list of local changes are both already in the image.
    harness_note = (
        "%s is the executing harness with this repository's patches already applied. To tell "
        "which behaviour is upstream Hermes and which is ours, read it against "
        "%s/deploy/docker/patches/ -- that directory is the complete list of what this "
        "repository changes, so anything you see in the tree and not in the patches is "
        "upstream's.%s" % (
            HERMES_TREE,
            source_root or "the source tree",
            (" The pinned upstream tag is %s." % harness_pin) if harness_pin else "",
        )
    )
    tools = os.path.join(TEMPLATE_DIR, "scripts", "selfimprove_evidence.py")
    return textwrap.dedent(
        """\
        Investigate this kube-agents installation for self-improvement findings, then write them
        to %(findings_path)s and stop. Follow the `self-investigation` skill in your skills
        directory; it holds the procedure, the evidence bar and the output schema.

        WHAT YOU ARE LOOKING AT
        - Deployed revision: %(revision)s. %(stamp_note)s
        - Source at that revision: %(source_root)s
        - Executing harness: %(harness_root_note)s
        - Namespace under observation: %(namespace)s
        - Mode: %(mode)s
        - Signal classes in scope this run: %(signals)s

        YOUR ONLY EVIDENCE TOOLS
        Run these with the shell. They are read-only by grant, not by convention: this pod's
        Google service account holds logging/trace/monitoring viewer and no GKE roles, and its
        Kubernetes service account is bound to `view` on one namespace.

          python3 %(tools)s logs --hours 24 --severity ERROR --limit 50
          python3 %(tools)s logs --agent-files --query 'jsonPayload.message:"Traceback"'
          python3 %(tools)s logs-count --hours 24 --severity ERROR
          python3 %(tools)s traces --hours 24 --limit 50
          python3 %(tools)s metrics --filter 'metric.type="kubernetes.io/container/restart_count"'
          python3 %(tools)s k8s pods|deployments|events|configmaps|platformagents|agentplugins

        Run each with --help before guessing at flags. You have no kubectl, no gcloud and no
        cluster write path of any kind; do not try to acquire one.

        WHAT THE PREVIOUS RUNS ALREADY KNOW
        Re-report a finding that is already here -- with this run's fresh evidence and the SAME
        fingerprint -- rather than inventing a new one for it. That is how occurrence counts
        accumulate, and the count is what the gate reads.

        %(ledger_summary)s

        WHEN YOU ARE DONE
        Write a JSON array to %(findings_path)s. Nothing else you print is read. An empty array
        is a valid and common answer -- a run that finds nothing is worth more than a run that
        promotes a guess to fill the file.
        """
    ) % {
        "findings_path": findings_path,
        "revision": revision,
        "stamp_note": stamp_note,
        "source_root": source_root or "(unavailable: the fetch failed; work from the harness and the cluster only)",
        "harness_root_note": harness_note,
        "namespace": namespace,
        "mode": mode,
        "signals": ", ".join(signals),
        "tools": tools,
        "ledger_summary": ledger_mod.summarise_for_prompt(ledger),
    }


# --------------------------------------------------------------------------
# 4. The agent turn
# --------------------------------------------------------------------------


def run_agent(prompt: str, home: str, timeout: int, label: str) -> Tuple[int, str]:
    """One headless Hermes turn against the private home.

    `hermes -z PROMPT --cli` rather than `hermes cron tick`: the tick path needs
    a cron store with a job in it that is always due, which is three moving
    parts to arrange the Kubernetes schedule has already arranged. `-z` is the
    same agent loop with the prompt supplied directly, and it was verified
    against this image on a fresh HERMES_HOME before the runner was written to
    depend on it.
    """
    environment = dict(os.environ)
    environment["HERMES_HOME"] = home
    environment["HOME"] = os.path.join(home, "home")
    os.makedirs(environment["HOME"], exist_ok=True)
    environment.setdefault("PYTHONPATH", os.path.join(TEMPLATE_DIR, "scripts"))
    started = time.time()
    log("agent turn (%s) starting, budget %ds" % (label, timeout))
    try:
        completed = subprocess.run(
            [HERMES_BIN, "-z", prompt, "--cli"],
            env=environment,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        log("agent turn (%s) hit its %ds budget" % (label, timeout))
        return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    elapsed = time.time() - started
    log("agent turn (%s) exited %d after %.0fs" % (label, completed.returncode, elapsed))
    if completed.stderr.strip():
        log("agent stderr tail: %s" % completed.stderr.strip()[-2000:])
    return completed.returncode, completed.stdout


def read_findings(path: str, stdout: str) -> List[Dict[str, Any]]:
    """The agent's findings, from the file it was told to write.

    The stdout fallback exists because the failure it covers is common and
    silent: a turn that ran the whole investigation, printed the JSON, and never
    called the write tool. Recovering it costs a few lines here and saves a
    wasted run.
    """
    raw = ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        log("no findings file at %s; falling back to the turn's stdout" % path)
        raw = _fenced_json(stdout)
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
        recovered = _fenced_json(raw)
        if recovered:
            try:
                parsed = json.loads(recovered)
            except ValueError:
                parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        parsed = parsed["findings"]
    if not isinstance(parsed, list):
        log("findings were not a JSON array; treating the run as having found nothing")
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _fenced_json(text: str) -> str:
    if not text:
        return ""
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
        if start == -1:
            return ""
        body = text[start + 3 :]
    else:
        body = text[start + 7 :]
    end = body.find("```")
    return body[:end] if end != -1 else ""


# --------------------------------------------------------------------------
# 5. Filing
# --------------------------------------------------------------------------


def file_pull_request(
    entry: Dict[str, Any],
    identity: Dict[str, Any],
    source_root: Optional[str],
    home: str,
    mode: str,
    upstream: str,
    fork: str,
    timeout: int,
) -> Optional[str]:
    """One further agent turn that turns a promoted finding into a pull request.

    A separate turn from the investigation on purpose. The investigation's job
    is to be sceptical about whether something is wrong; this one's is to write
    a change. Running both in one context means the turn that wrote the patch is
    the turn that decided the finding was real, and it will not go back.
    """
    prompt = textwrap.dedent(
        """\
        Open one pull request for the finding below, following the `file-pull-request` skill in
        your skills directory. One finding, one pull request.

        FINDING (fingerprint %(fingerprint)s, graded %(severity)s, signal %(signal)s)
        Title: %(title)s
        Seen: %(occurrences)d time(s) in the last 24 hours; first seen %(first_seen)s
        At revision: %(revision)s
        Location: %(location)s

        Summary
        %(summary)s

        Evidence
        %(evidence)s

        Proposed fix (the investigation's suggestion, not a decision)
        %(fix)s

        WHERE
        - Source checkout: %(source_root)s
        - Upstream: %(upstream)s
        - Push branches to: %(fork)s
        - Mode: %(mode)s

        Print the pull request URL on the last line of your reply, alone, and nothing else after it.
        """
    ) % {
        "fingerprint": entry.get("fingerprint", "?"),
        "severity": entry.get("severity", "?"),
        "signal": entry.get("signal", "?"),
        "title": entry.get("title", "?"),
        "occurrences": ledger_mod.occurrences_in_window(entry, ledger_mod.utcnow()),
        "first_seen": entry.get("first_seen", "?"),
        "revision": identity["revision"],
        "location": entry.get("location", "(not localised)"),
        "summary": entry.get("summary", ""),
        "evidence": json.dumps(entry.get("evidence"), indent=1)[:6000],
        "fix": entry.get("proposed_fix", "(none proposed)"),
        "source_root": source_root or "(unavailable)",
        "upstream": upstream,
        "fork": fork or "(none configured: upstream mode requires a fork)",
        "mode": mode,
    }
    code, stdout = run_agent(prompt, home, timeout, "file:%s" % entry.get("fingerprint", "?"))
    if code != 0:
        return None
    for line in reversed([l.strip() for l in stdout.splitlines() if l.strip()]):
        if line.startswith("https://github.com/"):
            return line
    return None


# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except run the agent and write the ledger; prints the brief it would have used",
    )
    args = parser.parse_args(argv)

    namespace = env("KUBE_DEFAULT_NAMESPACE") or env("POD_NAMESPACE") or "kubeagents-system"
    mode = env("SELFIMPROVE_MODE", "report-only")
    deployment = env("SELFIMPROVE_AGENT_DEPLOYMENT", "platform-agent-gateway")
    ledger_name = env("SELFIMPROVE_LEDGER_CONFIGMAP", "kube-agents-selfimprove-ledger")
    upstream = env("SELFIMPROVE_UPSTREAM_REPO", DEFAULT_UPSTREAM)
    fork = env("SELFIMPROVE_FORK_REPO")
    allow_fallback = env("SELFIMPROVE_ALLOW_UNSTAMPED_IMAGE", "false").lower() in ("1", "true", "yes")
    signals = [s.strip() for s in env("SELFIMPROVE_SIGNALS", ",".join(ledger_mod.SIGNALS)).split(",") if s.strip()]
    investigate_timeout = env_int("SELFIMPROVE_INVESTIGATE_TIMEOUT", 1800)
    file_timeout = env_int("SELFIMPROVE_FILE_TIMEOUT", 900)
    deadline = env_int("SELFIMPROVE_DEADLINE", 0)
    home = env("SELFIMPROVE_HOME", "/home/selfimprove")
    try:
        gate = json.loads(env("SELFIMPROVE_GATE", "{}") or "{}")
    except ValueError:
        log("SELFIMPROVE_GATE is not valid JSON; treating the gate as promoting nothing")
        gate = {}

    log("mode=%s namespace=%s ledger=%s signals=%s" % (mode, namespace, ledger_name, ",".join(signals)))

    identity = resolve_revision(namespace, deployment, allow_fallback)
    log("runner image: %s" % identity["runner_image"])
    log("agent image:  %s" % identity["agent_image"])
    log("revision:     %s (stamped=%s)" % (identity["revision"], identity["stamped"]))
    if identity.get("dirty"):
        log(
            "the image was built from a modified tree; fetching base commit %s, which is NOT "
            "everything the pod is running" % identity["fetch_ref"]
        )

    ledger = ledger_mod.load(namespace, ledger_name) if not args.dry_run else ledger_mod.empty_ledger()
    ledger_mod.prune(ledger, ledger_mod.utcnow())

    if identity["refuse"]:
        log("REFUSING TO RUN: %s" % identity["refuse"])
        if not args.dry_run:
            ledger_mod.record_run(ledger, identity["revision"] or "unknown", "refused", 0, 0, identity["refuse"])
            ledger_mod.save(namespace, ledger_name, ledger)
        return 1

    workspace = os.path.join(home, "src")
    source_root = fetch_source(upstream, identity["fetch_ref"], workspace)
    if source_root:
        log("source at %s" % source_root)
    else:
        log("source fetch failed; the investigation runs against the harness and the cluster only")

    pin = hermes_pin(source_root)
    if pin:
        log("hermes pin from tags.env: %s" % pin)

    scaffold_home(home)
    findings_path = os.path.join(home, "findings.json")
    if os.path.exists(findings_path):
        os.remove(findings_path)

    brief = build_brief(identity, source_root, pin, signals, ledger, findings_path, namespace, mode)
    if args.dry_run:
        print(brief)
        return 0

    investigate_budget = budgeted(investigate_timeout, deadline)
    if investigate_budget < investigate_timeout:
        log(
            "clamping the investigation to %ds: SELFIMPROVE_INVESTIGATE_TIMEOUT is %ds but only "
            "that much of activeDeadlineSeconds=%ds is left" % (investigate_budget, investigate_timeout, deadline)
        )
    code, stdout = run_agent(brief, home, investigate_budget, "investigate")
    outcome = "ok" if code == 0 else ("deadline" if code == 124 else "error")
    findings = read_findings(findings_path, stdout)
    log("the investigation reported %d finding(s)" % len(findings))

    fingerprints = []
    for finding in findings:
        fp, _ = ledger_mod.record_finding(ledger, finding, identity["revision"])
        fingerprints.append(fp)

    promoted, reasons = ledger_mod.evaluate_gate(ledger, gate, fingerprints)
    for fp in fingerprints:
        log("  %s -> %s" % (fp, reasons.get(fp, "held: not considered")))

    filed = 0
    if mode == "report-only":
        if promoted:
            log("%d finding(s) cleared the gate; mode is report-only, so they stay in the ledger" % len(promoted))
    else:
        for fp in promoted:
            turn_budget = budgeted(file_timeout, deadline)
            if turn_budget < MIN_TURN_SECONDS:
                log(
                    "out of time: %s and any findings after it stay in the ledger, unfiled. They "
                    "keep their occurrence counts and their gate eligibility, so the next run "
                    "files them first." % fp
                )
                break
            if turn_budget < file_timeout:
                log("filing %s on a reduced %ds budget; the deadline is closer than the timeout" % (fp, turn_budget))
            entry = ledger["findings"][fp]
            url = file_pull_request(entry, identity, source_root, home, mode, upstream, fork, turn_budget)
            if url:
                ledger_mod.record_promotion(ledger, fp, url, identity["revision"])
                filed += 1
                log("filed %s for %s" % (url, fp))
            else:
                log("could not file a pull request for %s; it stays in the ledger" % fp)

    ledger_mod.record_run(ledger, identity["revision"], outcome, len(findings), filed)
    ledger_mod.save(namespace, ledger_name, ledger)
    log("ledger written to configmap/%s in %s" % (ledger_name, namespace))
    log(
        "run complete: outcome=%s findings=%d promoted=%d filed=%d"
        % (outcome, len(findings), len(promoted), filed)
    )
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
