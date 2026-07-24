#!/opt/hermes/.venv/bin/python3
"""detect-drift — read-only GitOps-desired vs. live diff → corrective PR, never a direct fix (Phase 4 D3).

The Platform Agent's drift-detection SOP runs this on a schedule. It compares the GitOps-**desired**
manifest against the **live** object read with a read-only `get` (invariant 1) and, on divergence,
produces a **corrective-PR artifact** via `submit-suggestion` — unprompted, and WITHOUT ever touching
the live object (SC4, 01 §7; 04 §5.1). The correction flows only as a reviewed PR; the drifted live
object is left exactly as found.

Two load-bearing details from the design panel:

  1. DESIRED-AUTHORITATIVE, SERVER-DEFAULT-TOLERANT DIFF. Drift is computed as "does every field the
     GitOps manifest specifies still match live?". Fields that live adds but desired never specified
     (server defaults like `terminationGracePeriodSeconds`, controller-added fields) are NOT drift, so
     benign defaults don't open false-positive PRs.
  2. CANONICAL IGNORE-SET. `status`, `managedFields`, `resourceVersion`, `uid`, `creationTimestamp`,
     `generation`, `selfLink`, and the noisy `last-applied-configuration` / `revision` annotations are
     stripped from both sides before diffing.

Exit codes: 0 = no drift; 2 = drift found (and, with --emit-corrective, the artifact was produced);
1 = error. Never a nonzero-because-it-changed-something: this script only ever *reads* live.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the colocated submit-suggestion helper (same resolution as raise-escalation) so the corrective
# proposal goes out exactly the way every other change does. submit_suggestion imports
# github_token_refresh at import time, so its scripts dir must be discoverable first.
_HERE = Path(__file__).resolve()
_CANDIDATES = [
    "/opt/defaults/scripts",
    str(_HERE.parents[3] / "scripts"),
    str(_HERE.parents[2] / "submit-suggestion" / "scripts"),
]
for _p in _CANDIDATES:
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.insert(0, _p)

import submit_suggestion  # noqa: E402

# Fields dropped from BOTH sides before diffing — cluster/server bookkeeping, never GitOps-authored.
IGNORE_KEYS = {
    "managedFields",
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "selfLink",
}
IGNORE_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
}


def load_manifest(path: str) -> dict:
    """Load a manifest as a dict. JSON always; YAML if PyYAML is present (it is in the agent image)."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml  # deferred; only needed for YAML inputs

        return yaml.safe_load(text)


def strip(obj):
    """Recursively drop the ignore-set (and `status`, and noisy annotations) so the diff sees only
    GitOps-authored fields."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in IGNORE_KEYS or k == "status":
                continue
            if k == "annotations" and isinstance(v, dict):
                v = {ak: av for ak, av in v.items() if ak not in IGNORE_ANNOTATIONS}
                if not v:
                    continue
            out[k] = strip(v)
        return out
    if isinstance(obj, list):
        return [strip(x) for x in obj]
    return obj


def _stable(obj) -> str:
    return json.dumps(obj, sort_keys=True)


def find_drift(desired, live, path: str = "") -> list[dict]:
    """Return the list of drifted paths. Desired-authoritative: only fields present in `desired` are
    checked, so server-defaulted / controller-added fields in `live` never count as drift."""
    drifts: list[dict] = []
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return [{"path": path or "/", "desired": desired, "live": live, "kind": "type-mismatch"}]
        for k in sorted(desired):
            child = f"{path}.{k}" if path else k
            if k not in live:
                drifts.append({"path": child, "desired": desired[k], "live": None, "kind": "missing-in-live"})
            else:
                drifts += find_drift(desired[k], live[k], child)
    elif isinstance(desired, list):
        live_list = live if isinstance(live, list) else []
        live_canon = [_stable(x) for x in live_list]
        for i, elem in enumerate(desired):
            if _stable(elem) not in live_canon:
                drifts.append({"path": f"{path}[{i}]", "desired": elem, "live": None, "kind": "missing-list-elem"})
    else:
        if desired != live:
            drifts.append({"path": path or "/", "desired": desired, "live": live, "kind": "changed"})
    return drifts


def object_slug(desired: dict, override: str | None) -> str:
    if override:
        return override
    kind = str(desired.get("kind", "object")).lower()
    name = str((desired.get("metadata") or {}).get("name", "unknown")).lower()
    slug = f"{kind}-{name}"
    import re

    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-") or "object"


def render_observation(*, slug: str, desired: dict, drifts: list[dict], object_path: str | None, created: str) -> str:
    kind = desired.get("kind", "object")
    name = (desired.get("metadata") or {}).get("name", "unknown")
    lines = [
        "---",
        "type: observation",
        f"title: Drift detected — {kind}/{name}",
        "status: open",
        "observed-by: platform",
        f"created: {created}",
        "---",
        "",
        f"# Drift: {kind}/{name}",
        "",
        "The live object diverged from GitOps-desired state. Detected read-only — **the live object was",
        "NOT modified** (invariant 1, SC4). Reconcile by merging the re-asserted desired manifest below;",
        "the agent never patches live directly.",
        "",
        "## Drifted fields",
        "",
    ]
    for d in drifts:
        lines.append(f"- `{d['path']}` ({d['kind']}): desired `{d['desired']}` → live `{d['live']}`")
    if object_path:
        lines += ["", "## Correction", "", f"Re-asserts `{object_path}`; merging this PR reconciles the cluster via GitOps rollout."]
    lines.append("")
    return "\n".join(lines)


def _git(work: str, *args: str) -> str:
    return subprocess.run(["git", "-C", work, *args], check=True, capture_output=True, text=True).stdout


def emit_corrective(
    *,
    work: str,
    desired: dict,
    drifts: list[dict],
    slug: str,
    object_path: str | None,
    dry_run: bool,
    artifact_dir: str | None,
    created: str,
) -> str:
    """Write the drift observation (+ re-assert the desired manifest) on a platform proposal branch and
    hand off to submit-suggestion. Returns the branch name. Live is never touched here."""
    branch = f"platform-agent/drift-{slug}"
    _git(work, "checkout", "-b", branch)
    _git(work, "config", "user.email", "platform-agent@kube-agents.local")
    _git(work, "config", "user.name", "kube-agents platform agent")

    obs_rel = os.path.join("knowledge", "observation", f"drift-{slug}.md")
    os.makedirs(os.path.join(work, "knowledge", "observation"), exist_ok=True)
    with open(os.path.join(work, obs_rel), "w", encoding="utf-8") as fh:
        fh.write(render_observation(slug=slug, desired=desired, drifts=drifts, object_path=object_path, created=created))
    staged = [obs_rel]

    # Optionally re-assert the desired manifest so a merge re-applies it (idempotent).
    if object_path:
        dst = os.path.join(work, object_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if object_path.endswith((".yaml", ".yml")):
            import yaml

            with open(dst, "w", encoding="utf-8") as fh:
                yaml.safe_dump(desired, fh, sort_keys=False)
        else:
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump(desired, fh, indent=2)
        staged.append(object_path)

    _git(work, "add", *staged)  # stage only the corrective files, never `git add .`
    _git(work, "commit", "-m", f"fix(drift): correct {slug}")

    saved = os.getcwd()
    os.chdir(work)
    try:
        title = f"fix(drift): reconcile {slug}"
        body = (
            f"Automated corrective PR from the platform drift-detection SOP. The live object drifted "
            f"from GitOps-desired state; this re-asserts desired and records the finding in "
            f"`{obs_rel}`. Detection is read-only — the live object was not modified (SC4)."
        )
        if dry_run:
            submit_suggestion.dry_run(branch, "platform", title, body, artifact_dir)
        else:
            submit_suggestion.refresh_git_credentials()
            submit_suggestion.push_branch(branch, "platform")
            print(submit_suggestion.create_pull_request(None, branch, title, body))
    finally:
        os.chdir(saved)
    return branch


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only GitOps-desired vs. live drift detection.")
    p.add_argument("--desired", required=True, help="Path to the GitOps-desired manifest (JSON or YAML).")
    p.add_argument("--live", required=True, help="Path to the live object JSON (e.g. `kubectl get -o json`).")
    p.add_argument("--json", action="store_true", help="Emit the drift report as JSON.")
    p.add_argument("--emit-corrective", action="store_true", help="On drift, produce a corrective PR/artifact.")
    p.add_argument("--work-dir", help="GitOps working tree to write the corrective branch into.")
    p.add_argument("--object-path", help="Repo-relative path of the desired manifest to re-assert.")
    p.add_argument("--slug", help="Override the drift slug (default: <kind>-<name>).")
    p.add_argument("--created", help="Created date for the observation (default: today).")
    p.add_argument("--dry-run", action="store_true", help="With --emit-corrective, no push/PR (hermetic artifact).")
    p.add_argument("--artifact-dir", help="With --dry-run, also write the artifact here.")
    return p.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    desired = strip(load_manifest(args.desired))
    live = strip(load_manifest(args.live))
    drifts = find_drift(desired, live)

    if args.json:
        print(json.dumps({"drift": bool(drifts), "fields": drifts}, indent=2))
    elif not drifts:
        print("no drift: live matches every GitOps-desired field.")
    else:
        print(f"DRIFT DETECTED — {len(drifts)} field(s) diverged from GitOps-desired:")
        for d in drifts:
            print(f"  - {d['path']} ({d['kind']}): desired={d['desired']!r} live={d['live']!r}")

    if not drifts:
        return 0

    if args.emit_corrective:
        if not args.work_dir:
            raise ValueError("--emit-corrective requires --work-dir (the GitOps working tree).")
        slug = object_slug(desired, args.slug)
        created = args.created or datetime.date.today().isoformat()
        emit_corrective(
            work=args.work_dir,
            desired=load_manifest(args.desired),  # re-assert the ORIGINAL desired, not the stripped form
            drifts=drifts,
            slug=slug,
            object_path=args.object_path,
            dry_run=args.dry_run,
            artifact_dir=args.artifact_dir,
            created=created,
        )
    return 2


def main() -> int:
    try:
        return run(sys.argv[1:])
    except Exception as e:  # noqa: BLE001 — clean error, non-zero exit
        print(f"detect-drift: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
