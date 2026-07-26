#!/usr/bin/env python3
"""Render a full developer-team agent bundle from the skill's asset templates.

This is the mechanical half of the F4 provisioning cascade ONE LAYER DOWN (05 §5): the Cluster Admin
Agent, having decided a namespace needs its own read-only Developer Team Agent, renders the complete
GitOps bundle for that tenant namespace and then hands the tree off to `submit-suggestion` (which
opens the PR). A human reviews and merges; the customer's CI/CD applies it in the `namespaces/` wave.

The bundle is EVERYTHING a fresh tenant namespace needs for its leaf-tier agent, and nothing that
mints privilege at runtime — and, unlike the cluster-admin cascade, NO bootstrap/ or VAP waves (the
control plane + the agent-read-only ValidatingAdmissionPolicy already exist in the cluster, installed
by the cluster-admin's own F4 bootstrap):

  clusters/<cluster>/namespaces/<namespace>/
    00-namespace.yaml                       # the tenant Namespace (blast-radius boundary)
    10-resourcequota.yaml                   # aggregate compute/object cap (03 §3)
    20-netpol-default-deny.yaml             # zero-trust ingress+egress baseline (03 §10)
    30-netpol-developer-team-egress.yaml    # per-tier egress allowlist (DNS + A4 hop + external CIDRs)
    40-service-aliases.yaml                 # ExternalName aliases (A4 graft: litellm, minter)
    50-developer-team-identity.yaml         # pre-created read-only KSA + namespaced Role/Binding + WI
    60-developer-team-agent.yaml            # the developer-team Agent CR (references the KSA by name)
    README.md                               # human-facing description of the bundle (not applied)

These seven manifests are the "seven 06 §3 paths" the bundle emits; the README is documentation.

This script ONLY writes local files (token substitution over assets/) — it holds no credentials and
makes no cluster/cloud mutation. All actuation is the reviewed PR + the CI/CD pipeline. The controller
references the pre-created ServiceAccount by name and mints no RBAC at runtime (08 §4).

Usage:
  render_developer_team.py --cluster cluster-a --namespace team-x --project-id my-proj \
    --location us-central1 --team-lead-chat-id users/123456789 \
    [--workload-identity [--gke-dataplane auto|v1|v2]] \
    [--hub-inference-cidr 10.10.0.0/28] [--hub-minty-cidr 10.10.0.16/28] \
    [--mcp-cidrs 10.10.0.32/28] [--parent cluster-admin-<cluster>] [--repo-root .]

An unset chat ID is written as a REPLACE_WITH_* placeholder for the human to fill at review time: it
is a valid string that applies but fails CLOSED, so the bundle is reviewable and the agent is
reachable by nobody until it is set. The egress widenings are NOT placeholders — each is simply
absent unless asked for, because a REPLACE_WITH_* in a `cidr:` field is rejected by the API server
and made the entire bundle un-appliable (V-CMP-003).

The four isolation manifests (10/20/30/40) are the SAME BYTES the installer applies from
k8s-operator/scripts/*.template — that is asserted, not aspired to: dev/test_skill_templates.py
renders both and compares them. Edit the installer template, then regenerate the asset here.
"""
from __future__ import annotations

import argparse
import os
import sys

# Token -> the placeholder written when the corresponding CLI arg is omitted. A partially-specified
# render still produces a reviewable (if not yet appliable) diff.
#
# Only ONE token still has a placeholder default, and the difference matters. A chat ID is a plain
# string: `users/REPLACE_WITH_TEAM_LEAD_ID` applies cleanly and matches nobody, so the bundle is
# reviewable and the agent is unreachable until a human fills it in — loud, and fail-closed. A CIDR
# is not: `REPLACE_WITH_HUB_INFERENCE_CIDR` in a `cidr:` field is rejected by the API server, so the
# whole bundle became un-appliable (V-CMP-003) and the reviewer's only options were to fill in a
# number they may not have or to delete the rule. The optional egress rules are therefore composed
# below and are ABSENT UNLESS CONFIGURED, matching what the install path renders.
PLACEHOLDER_DEFAULTS = {
    "@@TEAM_LEAD_CHAT_ID@@": "users/REPLACE_WITH_TEAM_LEAD_ID",
}

# assets/<relative template path> -> repo-relative output path (with @@CLUSTER@@/@@NAMESPACE@@
# substituted). A trailing ".tmpl" on the asset is stripped in the output name. The first seven are
# the appliable manifests (the "seven 06 §3 paths"); README.md is documentation.
ASSET_MAP = {
    "00-namespace.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/00-namespace.yaml",
    "10-resourcequota.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/10-resourcequota.yaml",
    "20-netpol-default-deny.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/20-netpol-default-deny.yaml",
    "30-netpol-developer-team-egress.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/30-netpol-developer-team-egress.yaml",
    "40-service-aliases.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/40-service-aliases.yaml",
    "50-developer-team-identity.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/50-developer-team-identity.yaml",
    "60-developer-team-agent.yaml.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/60-developer-team-agent.yaml",
    "README.md.tmpl": "clusters/@@CLUSTER@@/namespaces/@@NAMESPACE@@/README.md",
}

# The seven appliable manifests (used to report the "seven 06 §3 paths" distinctly from the README).
MANIFEST_ASSETS = [k for k in ASSET_MAP if k.endswith(".yaml.tmpl")]


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render a developer-team agent GitOps bundle.")
    p.add_argument("--cluster", required=True, help="Target cluster name (e.g. cluster-a).")
    p.add_argument("--namespace", required=True, help="Target tenant namespace (e.g. team-x).")
    p.add_argument("--project-id", required=True, help="GCP project ID the cluster lives in.")
    p.add_argument("--location", required=True, help="Cluster location/region (e.g. us-central1).")
    p.add_argument(
        "--parent",
        default=None,
        help="parentRef Agent name (default: cluster-admin-<cluster>, this Cluster Admin Agent).",
    )
    p.add_argument("--team-lead-chat-id", help="Google Chat user ID allowed to reach the agent (users/NNN).")
    # The three remote-hub egress widenings. Omitted => the rule is absent, not stubbed. There is no
    # --github-cidrs: GitHub's four published IPv4 blocks are fixed in the egress template itself
    # (rule 4), because they are the same for every tenant and a per-tenant copy is a per-tenant way
    # to get them wrong.
    p.add_argument("--hub-inference-cidr", help="Hub LiteLLM inference private-endpoint CIDR (comma-separated).")
    p.add_argument("--hub-minty-cidr", help="Hub Minty (token broker) private-endpoint CIDR (comma-separated).")
    p.add_argument("--mcp-cidrs", help="MCP grounding endpoint CIDRs (comma-separated).")
    p.add_argument(
        "--workload-identity",
        action="store_true",
        help=(
            "Target cluster uses GKE Workload Identity: add the narrow, port-bound metadata rules. "
            "Without this the tenant agent has no cloud identity at all on a WI cluster; with it on "
            "a non-WI cluster the raw node service account becomes reachable. Match the cluster."
        ),
    )
    p.add_argument(
        "--gke-dataplane",
        choices=("auto", "v1", "v2"),
        default="auto",
        help="Which metadata IP<->port pairing to emit with --workload-identity (default: both).",
    )
    p.add_argument("--repo-root", default=".", help="GitOps repo root to write into (default cwd).")
    p.add_argument("--assets-dir", default=None, help="Override the assets dir (default: ../assets next to this script).")
    return p.parse_args(argv)


def _cidr_rule(comment: str, csv: str | None, port: int) -> list[str]:
    """One egress rule over a comma-separated CIDR list, or nothing at all.

    Byte-for-byte the same emission as `_emit_cidr_rule` in
    k8s-operator/scripts/common.sh. The two must not diverge: the manifests this
    script proposes and the ones the installer applies are the same manifests,
    and dev/test_skill_templates.py compares them exactly.
    """
    cidrs = [c.strip() for c in (csv or "").split(",") if c.strip()]
    if not cidrs:
        return []
    out = [f"    # {comment}", "    - to:"]
    for c in cidrs:
        out += ["        - ipBlock:", f"            cidr: {c}"]
    out += ["      ports:", "        - protocol: TCP", f"          port: {port}"]
    return out


def build_optional_egress_block(args: argparse.Namespace) -> str:
    """The rules appended to the tenant egress allowlist, in install-path order.

    Mirrors `render_wi_metadata_block` + `render_remote_hub_block`. Every one of
    these is a WIDENING of a pure allowlist, so each is emitted only when the
    caller asked for it by name — the default bundle reaches DNS, the in-cluster
    alias hop, and GitHub, and nothing else.
    """
    lines: list[str] = []
    if args.workload_identity:
        lines += [
            "    # 5) GKE metadata server — Workload Identity ONLY, bound to the metadata ports. This is the",
            "    #    single widening in this policy; it is narrow on purpose. WI's metadata concealment keeps",
            "    #    the node service account unreachable, so what this opens is the pod's own viewer-only",
            "    #    GSA. Rendered only because WORKLOAD_IDENTITY_ENABLED=true (common.sh:render_wi_metadata_block).",
        ]
        if args.gke_dataplane in ("auto", "v1"):
            lines += [
                "    #    Dataplane V1 / Calico (GKE >= 1.21.0-gke.1000).",
                "    - to:",
                "        - ipBlock:",
                "            cidr: 169.254.169.252/32",
                "      ports:",
                "        - protocol: TCP",
                "          port: 988",
                "        - protocol: TCP",
                "          port: 987",
            ]
        if args.gke_dataplane in ("auto", "v2"):
            lines += [
                "    #    Dataplane V2.",
                "    - to:",
                "        - ipBlock:",
                "            cidr: 169.254.169.254/32",
                "      ports:",
                "        - protocol: TCP",
                "          port: 80",
                "        - protocol: TCP",
                "          port: 8080",
            ]
    lines += _cidr_rule(
        "6) Hub Inference (LiteLLM) over the hub's VPC-internal private endpoint (05 §5).",
        args.hub_inference_cidr,
        443,
    )
    lines += _cidr_rule(
        "7) Hub Minty — the GitHub/Workload-Identity token broker, VPC-internal (05 §5).",
        args.hub_minty_cidr,
        443,
    )
    lines += _cidr_rule(
        "8) MCP grounding endpoints the agent reads live docs from (03 §10).",
        args.mcp_cidrs,
        443,
    )
    return "\n".join(lines)


def build_substitutions(args: argparse.Namespace) -> dict[str, str]:
    # parentRef defaults to this cluster's Cluster Admin Agent (the tier one level up that proposes
    # the developer-team agent) — the F4 cascade parent for the leaf tier.
    parent = args.parent if args.parent else f"cluster-admin-{args.cluster}"
    subs = {
        "@@CLUSTER@@": args.cluster,
        "@@NAMESPACE@@": args.namespace,
        "@@PROJECT_ID@@": args.project_id,
        "@@LOCATION@@": args.location,
        "@@PARENT@@": parent,
        "@@EGRESS_OPTIONAL_BLOCKS@@": build_optional_egress_block(args),
    }
    optional = {
        "@@TEAM_LEAD_CHAT_ID@@": args.team_lead_chat_id,
    }
    for token, value in optional.items():
        subs[token] = value if value else PLACEHOLDER_DEFAULTS[token]
    return subs


def substitute_tokens(text: str, subs: dict[str, str]) -> str:
    """Token replacement with no trailing-newline policy — for output paths."""
    for token, value in subs.items():
        text = text.replace(token, value)
    return text


def substitute(text: str, subs: dict[str, str]) -> str:
    text = substitute_tokens(text, subs)
    # Exactly one trailing newline, however the optional blocks came out. The installer does the
    # same thing (`printf '%s\n' "$(...)"` in render_egress_policy) and for the same reason: with no
    # optional rules the slot renders empty and leaves a stray blank line, which Prettier strips
    # from the committed copies — putting the formatter and the byte-for-byte drift check in direct
    # conflict. Normalising here keeps both gates true at once.
    return text.rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    subs = build_substitutions(args)

    assets_dir = args.assets_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
    assets_dir = os.path.abspath(assets_dir)
    if not os.path.isdir(assets_dir):
        print(f"error: assets dir not found: {assets_dir}", file=sys.stderr)
        return 2

    written: list[str] = []
    manifests: list[str] = []
    for asset_rel, out_tmpl in ASSET_MAP.items():
        src = os.path.join(assets_dir, asset_rel)
        if not os.path.isfile(src):
            print(f"error: missing asset template: {src}", file=sys.stderr)
            return 2
        with open(src, "r", encoding="utf-8") as fh:
            rendered = substitute(fh.read(), subs)

        out_rel = substitute_tokens(out_tmpl, subs)
        out_path = os.path.join(os.path.abspath(args.repo_root), out_rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        written.append(out_rel)
        if asset_rel in MANIFEST_ASSETS:
            manifests.append(out_rel)

    print(f"Rendered developer-team bundle for {args.namespace} in {args.cluster}:")
    print("  seven appliable manifests (06 §3):")
    for rel in manifests:
        print(f"    {rel}")
    for rel in written:
        if rel not in manifests:
            print(f"  documentation: {rel}")
    print(
        "\nThis bundle mints NO privilege at runtime and ships NO bootstrap/VAP waves (the cluster "
        "already has the control plane + agent-read-only VAP).\n"
        "Next: stage exactly these files and hand off to submit-suggestion on branch "
        f"'cluster-admin-agent/provision-developer-team-{args.namespace}'.\n"
        "Fill the REPLACE_WITH_TEAM_LEAD_ID placeholder before the pipeline applies: until it names "
        "a real user the agent is reachable by nobody."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
