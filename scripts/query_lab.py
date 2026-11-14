#!/usr/bin/env python3
"""Query the vcsim lab with pyVmomi.

Usage (from the project root):
    python scripts/query_lab.py [--env default] [--json] [--only NAME ...]
    python scripts/query_lab.py --inventory
    python scripts/query_lab.py --pci
    python scripts/query_lab.py --guest-ids

--env selects the environment (default: default); the vcsim port comes
from environments/<env>/env.yaml unless VC_PORT is set. The VM table /
JSON includes the per-VM attributes (CBT, power state, fast boot /
boot delay, tools status). With --lab (default: the environment's
vms.yaml) each VM also shows its declared (seed-resolved) values, and
a tools status mismatch is flagged — vcsim never reports guest tools,
so the live value is always toolsNotInstalled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config, query  # noqa: E402
from vcsim_lab.vcenter import connect, disconnect  # noqa: E402


def _load_declared(path: str) -> dict | None:
    """Resolve the lab's per-VM assignments (name -> dict), or None if
    the lab file cannot be loaded — querying stays live-only then.
    Each entry carries the seed-resolved attributes plus the declared
    disk layout (basic/dynamic types and volumes)."""
    declared = config.load_declared_assignments(path)
    if declared is None:
        print(f"note: could not load {path}; showing live values only", file=sys.stderr)
    return declared


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the vcsim lab.")
    parser.add_argument("--env", default=config.DEFAULT_ENVIRONMENT,
                        help="environment to query (default: default)")
    parser.add_argument("--json", action="store_true", help="print VMs as JSON")
    parser.add_argument("--only", nargs="+", help="show only these VMs")
    parser.add_argument("--lab", default=None,
                        help="lab file for declared per-VM values (default: the environment's vms.yaml)")
    parser.add_argument("--inventory", action="store_true", help="list datacenters/hosts/datastores/networks/VMs")
    parser.add_argument("--pci", action="store_true", help="list host PCI devices and GPU capability")
    parser.add_argument("--guest-ids", action="store_true", help="list guest OS ids the simulator advertises")
    args = parser.parse_args()

    try:
        env = config.load_environment(args.env)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    lab_path = args.lab or env.lab_path
    cfg = config.vcenter_config_for_env(env)
    try:
        si = connect(cfg)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1
    try:
        if args.inventory:
            print(json.dumps(query.list_inventory(si), indent=2))
        elif args.pci:
            print(json.dumps(query.list_host_pci(si), indent=2))
        elif args.guest_ids:
            entries = query.list_guest_ids(si)
            if not entries:
                print("the simulator does not publish guest OS descriptors "
                      "(EnvironmentBrowser.guestOsDescriptor is not served by vcsim); "
                      "guest_id values in vms.yaml are still accepted as-is.")
            for entry in entries:
                print(f"{entry['id']:<28} {entry['family']:<12} {entry['full_name']}")
        elif args.json:
            declared = _load_declared(str(lab_path))
            print(json.dumps(query.summarize_lab(si, args.only, declared=declared), indent=2))
        else:
            declared = _load_declared(str(lab_path))
            summaries = query.summarize_lab(si, args.only, declared=declared)
            if not summaries:
                print("no VMs found")
            query.print_table(summaries)
    finally:
        disconnect(si)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
