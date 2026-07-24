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
    [--hub-inference-cidr 10.10.0.0/28] [--hub-minty-cidr 10.10.0.16/28] \
    [--github-cidrs 140.82.112.0/20] [--mcp-cidrs 10.10.0.32/28] \
    [--parent cluster-admin-<cluster>] [--repo-root .]

Any CIDR/chat-id left unset is written as a REPLACE_WITH_* placeholder for the human to fill at
review time (same convention as the propose-cluster-admin cascade and the cluster-a exemplar). A
placeholder CIDR is an invalid value that `kubectl apply` rejects, so the pipeline cannot silently
apply a half-configured egress policy; a placeholder chat ID is a valid string that applies but fails
CLOSED (it matches no real user, so the router refuses everyone).
"""
from __future__ import annotations

import argparse
import os
import sys

# Token -> the placeholder written when the corresponding CLI arg is omitted. A partially-specified
# render still produces a reviewable (if not yet appliable) diff.
PLACEHOLDER_DEFAULTS = {
    "@@TEAM_LEAD_CHAT_ID@@": "users/REPLACE_WITH_TEAM_LEAD_ID",
    "@@HUB_INFERENCE_CIDR@@": "REPLACE_WITH_HUB_INFERENCE_CIDR",
    "@@HUB_MINTY_CIDR@@": "REPLACE_WITH_HUB_MINTY_CIDR",
    "@@GITHUB_CIDRS@@": "REPLACE_WITH_GITHUB_CIDRS",
    "@@MCP_GROUNDING_CIDRS@@": "REPLACE_WITH_MCP_GROUNDING_CIDRS",
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
    p.add_argument("--hub-inference-cidr", help="Hub LiteLLM inference private-endpoint CIDR.")
    p.add_argument("--hub-minty-cidr", help="Hub Minty (token broker) private-endpoint CIDR.")
    p.add_argument("--github-cidrs", help="GitHub egress CIDR (see api.github.com/meta).")
    p.add_argument("--mcp-cidrs", help="MCP grounding endpoint CIDR.")
    p.add_argument("--repo-root", default=".", help="GitOps repo root to write into (default cwd).")
    p.add_argument("--assets-dir", default=None, help="Override the assets dir (default: ../assets next to this script).")
    return p.parse_args(argv)


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
    }
    optional = {
        "@@TEAM_LEAD_CHAT_ID@@": args.team_lead_chat_id,
        "@@HUB_INFERENCE_CIDR@@": args.hub_inference_cidr,
        "@@HUB_MINTY_CIDR@@": args.hub_minty_cidr,
        "@@GITHUB_CIDRS@@": args.github_cidrs,
        "@@MCP_GROUNDING_CIDRS@@": args.mcp_cidrs,
    }
    for token, value in optional.items():
        subs[token] = value if value else PLACEHOLDER_DEFAULTS[token]
    return subs


def substitute(text: str, subs: dict[str, str]) -> str:
    for token, value in subs.items():
        text = text.replace(token, value)
    return text


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

        out_rel = substitute(out_tmpl, subs)
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
        "Fill any REPLACE_WITH_* placeholders (team-lead chat ID, hub/GitHub/MCP CIDRs) before the "
        "pipeline applies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
