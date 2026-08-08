#!/usr/bin/env python3
"""Cron entry point for the daily AI workload security audit — see platform_cron_dispatch.py."""

from platform_cron_dispatch import main

raise SystemExit(main("ai-security-audit"))
