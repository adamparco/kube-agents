#!/usr/bin/env python3
"""Render a full cluster-admin agent bundle from the skill's asset templates.

This is the mechanical half of the F4 provisioning cascade (05 §5): the Platform Agent, having
decided a cluster needs its own read-only Cluster Admin Agent, renders the complete GitOps bundle
for that cluster and then hands the tree off to `submit-suggestion` (which opens the PR). A human
reviews and merges; the customer's CI/CD applies it in bootstrap order.

The bundle is EVERYTHING a fresh spoke needs, and nothing that mints privilege at runtime:

  clusters/<cluster>/
    bootstrap/                      # ordered control-plane waves (05 §7) — resolves chicken-and-egg
      00-cert-manager/              # cert-manager (webhook serving cert)
      10-controller/                # CRD + controller + webhooks + kage-router (config/default)
      20-policy/                    # the agent-read-only VAP, enforcing BEFORE the identity applies
    agents/
      identity/cluster-admin-identity.yaml   # pre-created read-only KSA + ClusterRole/Binding + WI
      agent.yaml                             # the cluster-admin Agent CR (references the KSA by name)
      netpol-cluster-admin-egress.yaml       # per-tier default-deny egress (cross-cluster contract)

This script ONLY writes local files (token substitution over assets/) — it holds no credentials and
makes no cluster/cloud mutation. All actuation is the reviewed PR + the CI/CD pipeline.

Usage:
  render_cluster_admin.py --cluster cluster-b --project-id my-proj --location us-central1 \
    --admin-chat-id users/123456789 \
    [--hub-inference-cidr 10.10.0.0/28] [--hub-minty-cidr 10.10.0.16/28] \
    [--github-cidrs 140.82.112.0/20] [--mcp-cidrs 10.10.0.32/28] \
    [--parent platform-agent] [--repo-root .]

Any CIDR/chat-id left unset is written as a REPLACE_WITH_* placeholder for the human to fill at
review time (same convention as the reference clusters/cluster-a exemplar).
"""
from __future__ import annotations

import argparse
import os
import sys

# Token -> the CLI arg that fills it. Tokens absent from args fall back to the placeholder default
# so a partially-specified render still produces a reviewable (if not yet appliable) diff.
PLACEHOLDER_DEFAULTS = {
    "@@ADMIN_CHAT_ID@@": "users/REPLACE_WITH_CLUSTER_ADMIN_ID",
    "@@HUB_INFERENCE_CIDR@@": "REPLACE_WITH_HUB_INFERENCE_CIDR",
    "@@HUB_MINTY_CIDR@@": "REPLACE_WITH_HUB_MINTY_CIDR",
    "@@GITHUB_CIDRS@@": "REPLACE_WITH_GITHUB_CIDRS",
    "@@MCP_GROUNDING_CIDRS@@": "REPLACE_WITH_MCP_GROUNDING_CIDRS",
}

# assets/<relative template path> -> repo-relative output path (with @@CLUSTER@@ substituted). A
# trailing ".tmpl" on the asset is stripped in the output name.
ASSET_MAP = {
    "agent.yaml.tmpl": "clusters/@@CLUSTER@@/agents/agent.yaml",
    "identity/cluster-admin-identity.yaml.tmpl": "clusters/@@CLUSTER@@/agents/identity/cluster-admin-identity.yaml",
    "netpol-cluster-admin-egress.yaml.tmpl": "clusters/@@CLUSTER@@/agents/netpol-cluster-admin-egress.yaml",
    "bootstrap/README.md.tmpl": "clusters/@@CLUSTER@@/bootstrap/README.md",
    "bootstrap/00-cert-manager/kustomization.yaml.tmpl": "clusters/@@CLUSTER@@/bootstrap/00-cert-manager/kustomization.yaml",
    "bootstrap/10-controller/kustomization.yaml.tmpl": "clusters/@@CLUSTER@@/bootstrap/10-controller/kustomization.yaml",
    "bootstrap/20-policy/vap-agent-readonly.yaml.tmpl": "clusters/@@CLUSTER@@/bootstrap/20-policy/vap-agent-readonly.yaml",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render a cluster-admin agent GitOps bundle.")
    p.add_argument("--cluster", required=True, help="Target cluster name (e.g. cluster-b).")
    p.add_argument("--project-id", required=True, help="GCP project ID the cluster lives in.")
    p.add_argument("--location", required=True, help="Cluster location/region (e.g. us-central1).")
    p.add_argument("--parent", default="platform-agent", help="parentRef Agent name (default platform-agent).")
    p.add_argument("--admin-chat-id", help="Google Chat user ID allowed to reach the agent (users/NNN).")
    p.add_argument("--hub-inference-cidr", help="Hub LiteLLM inference private-endpoint CIDR.")
    p.add_argument("--hub-minty-cidr", help="Hub Minty (token broker) private-endpoint CIDR.")
    p.add_argument("--github-cidrs", help="GitHub egress CIDR (see api.github.com/meta).")
    p.add_argument("--mcp-cidrs", help="MCP grounding endpoint CIDR.")
    p.add_argument("--repo-root", default=".", help="GitOps repo root to write into (default cwd).")
    p.add_argument("--assets-dir", default=None, help="Override the assets dir (default: ../assets next to this script).")
    return p.parse_args(argv)


def build_substitutions(args: argparse.Namespace) -> dict[str, str]:
    subs = {
        "@@CLUSTER@@": args.cluster,
        "@@PROJECT_ID@@": args.project_id,
        "@@LOCATION@@": args.location,
        "@@PARENT@@": args.parent,
    }
    # Optional tokens: use the provided value, else the REPLACE_WITH_* placeholder.
    optional = {
        "@@ADMIN_CHAT_ID@@": args.admin_chat_id,
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

    print("Rendered cluster-admin bundle:")
    for rel in written:
        print(f"  {rel}")
    print(
        "\nNext: stage exactly these files and hand off to submit-suggestion on branch "
        f"'platform-agent/provision-cluster-admin-{args.cluster}'.\n"
        "Fill any REPLACE_WITH_* placeholders (chat ID, hub/GitHub/MCP CIDRs) before the pipeline applies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
