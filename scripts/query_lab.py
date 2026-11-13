#!/usr/bin/env python3
"""Query the vcsim lab with pyVmomi.

Usage (from the project root):
    python scripts/query_lab.py [--json] [--only NAME ...] [--lab vms.yaml]
    python scripts/query_lab.py --inventory
    python scripts/query_lab.py --pci
    python scripts/query_lab.py --guest-ids

The VM table / JSON includes the per-VM attributes (CBT, power state,
fast boot / boot delay, tools status). With --lab (default vms.yaml)
each VM also shows its declared (seed-resolved) values, and a tools
status mismatch is flagged — vcsim never reports guest tools, so the
live value is always toolsNotInstalled.
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
    the lab file cannot be loaded — querying stays live-only then."""
    try:
        lab = config.load_lab(path)
    except (config.ConfigError, OSError) as exc:
        print(f"note: could not load {path} ({exc}); showing live values only", file=sys.stderr)
        return None
    return {name: a.to_dict() for name, a in config.resolve_random_assignments(lab).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the vcsim lab.")
    parser.add_argument("--json", action="store_true", help="print VMs as JSON")
    parser.add_argument("--only", nargs="+", help="show only these VMs")
    parser.add_argument("--lab", default="vms.yaml",
                        help="lab file for declared per-VM values (default: vms.yaml)")
    parser.add_argument("--inventory", action="store_true", help="list datacenters/hosts/datastores/networks/VMs")
    parser.add_argument("--pci", action="store_true", help="list host PCI devices and GPU capability")
    parser.add_argument("--guest-ids", action="store_true", help="list guest OS ids the simulator advertises")
    args = parser.parse_args()

    cfg = config.VCenterConfig.from_env()
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
            declared = _load_declared(args.lab)
            print(json.dumps(query.summarize_lab(si, args.only, declared=declared), indent=2))
        else:
            declared = _load_declared(args.lab)
            summaries = query.summarize_lab(si, args.only, declared=declared)
            if not summaries:
                print("no VMs found")
            query.print_table(summaries)
    finally:
        disconnect(si)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
