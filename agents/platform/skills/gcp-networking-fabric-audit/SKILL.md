---
name: gcp-networking-fabric-audit
description: Audits VPC subnet IPAM capacity, Cloud NAT ephemeral port exhaustion, Private Service Connect routing, and Cloud Armor WAF policies.
---

# Task

Audit Google Cloud VPC subnet IPAM allocation headroom, Cloud NAT ephemeral port capacity, Private Service Connect (PSC) reachability, and Cloud Armor WAF policies, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Networking Inspection

Follow the authoritative SOP at `governance/gcp_networking_fabric_sop.md` to execute the five diagnostic checks across target GCP projects:

- `subnet-ip-exhaustion`
- `cloud-nat-exhaustion`
- `psc-routing-deadlock`
- `mtu-packet-fragmentation`
- `cloud-armor-false-positive`

Run the collector before evaluating any check by hand — see the SOP's §2 for the manifest-reading rules:

```bash
python3 ./skills/gcp-networking-fabric-audit/scripts/networking_audit.py > /opt/data/scratch/manifest_gcp-networking-fabric-audit.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`), passing `--manifest-file` to `finish` as the SOP's §5 directs.
