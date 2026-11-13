#!/usr/bin/env python3
"""Query host storage in the vcsim lab with pyVmomi.

Combines the declared storage model (storage.yaml: arrays, fabric HBAs,
array LUNs, LUN -> datastore mapping) with what vcsim actually serves
per host (HBAs, SCSI LUNs, multipath, datastore mounts), and says
plainly where the two diverge.

Usage (from the project root):
    python scripts/query_storage.py [--json] [--only HOST ...]
    python scripts/query_storage.py --storage-yaml storage.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config, storage  # noqa: E402
from vcsim_lab.vcenter import connect, disconnect  # noqa: E402


def _hba_line(h: dict) -> str:
    extra = h.get("wwpn") or h.get("iqn") or ""
    speed = h.get("speed_gbps")
    if speed:
        extra = f"{extra} {speed}Gbps".strip()
    return f"{h['device']} [{h['type']}] {h.get('model') or ''} {extra}".rstrip()


def _lun_line(l: dict, declared: bool = False) -> str:
    if declared:
        live = "visible live" if l["visible_live"] else "not visible live"
        return (
            f"{l['lun']} {l['naa']} {l['capacity_gb']}G -> {l['datastore']} "
            f"({l['array']}, {l['protocol']}, {live})"
        )
    cap = f"{l['capacity_gb']}G" if l.get("capacity_gb") is not None else "-"
    tags = []
    if l.get("ssd"):
        tags.append("ssd")
    if l.get("local_disk"):
        tags.append("local")
    tag = f" ({', '.join(tags)})" if tags else ""
    return f"{l['canonical_name']} {l['type']} {cap} {l['vendor']} {l['model']}{tag}"


def print_report(report: dict) -> None:
    for host in report["hosts"]:
        print(f"{host['name']}")
        print("  declared HBAs (storage.yaml):")
        for h in host["declared_hbas"]:
            print(f"    {_hba_line(h)}")
        if not host["declared_hbas"]:
            print("    (none)")
        print("  live HBAs (vcsim):")
        for h in host["live_hbas"]:
            print(f"    {_hba_line(h)}")
        if not host["live_hbas"]:
            print("    (none served)")
        print("  declared LUNs visible to this host:")
        for l in host["declared_luns"]:
            print(f"    {_lun_line(l, declared=True)}")
        if not host["declared_luns"]:
            print("    (none)")
        print("  live LUNs (vcsim):")
        for l in host["live_luns"]:
            print(f"    {_lun_line(l)}")
        if not host["live_luns"]:
            print("    (none served)")
        if host["multipath"]:
            for mp in host["multipath"]:
                paths = ", ".join(f"{p['name']} [{p['state']}]" for p in mp["paths"])
                print(f"  multipath {mp['lun_uuid'][:16]}... policy={mp['policy']}: {paths}")
        print(f"  software iSCSI enabled: {host['software_iscsi_enabled']}")
        print("  datastore mounts:")
        for m in host["mounts"]:
            backing = f" <- {m['backing_lun']} ({m['array']})" if m["backing_lun"] else ""
            verified = " [backing verified live]" if m["backing_verified_live"] else ""
            print(
                f"    {m['datastore']} {m['access_mode']} "
                f"mounted={m['mounted']} accessible={m['accessible']}{backing}{verified}"
            )
        if not host["mounts"]:
            print("    (none)")
        for note in host["notes"]:
            print(f"  note: {note}")
        print()

    print("datastores:")
    for ds in report["datastores"]:
        backing = f" <- {ds['backing_lun']} ({ds['array']})" if ds["backing_lun"] else ""
        extents = f" extents={ds['extents']}" if ds["extents"] is not None else " (no extents live)"
        print(
            f"  {ds['name']} [{ds['type']}/{ds['info_type']}] "
            f"{ds['capacity_gb']}G cap, {ds['free_gb']}G free, "
            f"mounted on {len(ds['mounted_on'])} hosts{backing}{extents}"
        )
    print()
    print("arrays (declared):")
    for array in report["arrays"]:
        luns = ", ".join(f"{l['id']} ({l['capacity_gb']}G -> {l['datastore']})" for l in array["luns"])
        print(f"  {array['id']} {array['vendor']} {array['model']} [{array['protocol']}]: {luns}")
    print()
    for note in report["notes"]:
        print(f"note: {note}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Query host storage in the vcsim lab.")
    parser.add_argument("--json", action="store_true", help="print the reconciled report as JSON")
    parser.add_argument("--only", nargs="+", help="show only these hosts")
    parser.add_argument(
        "--storage-yaml", default="storage.yaml", help="storage model file (default: storage.yaml)"
    )
    args = parser.parse_args()

    try:
        model = storage.load_storage(args.storage_yaml)
    except storage.StorageError as exc:
        print(f"storage model error: {exc}", file=sys.stderr)
        return 1
    for warning in model.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    cfg = config.VCenterConfig.from_env()
    try:
        si = connect(cfg)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1
    try:
        report = storage.summarize_storage(si, model)
    finally:
        disconnect(si)
    if args.only:
        wanted = set(args.only)
        report["hosts"] = [h for h in report["hosts"] if h["name"] in wanted]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
