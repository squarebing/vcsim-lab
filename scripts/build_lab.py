#!/usr/bin/env python3
"""Build the vcsim lab from an environment's vms.yaml.

Usage (from the project root):
    python scripts/build_lab.py [--env default] [--lab FILE] [--only NAME ...]
                                [--recreate] [--report build-report.json]
                                [--plan-only]

--env selects the environment (default: default); the lab comes from
environments/<env>/vms.yaml unless --lab overrides it, and the vcsim
port from environments/<env>/env.yaml unless VC_PORT is set (see
vcsim_lab.config.vcenter_config_for_env). --plan-only prints the device
plan for each VM without connecting to vCenter. Connection settings
come from VC_HOST / VC_USER / VC_PASSWORD (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import build, config, specs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the vcsim lab from an environment's vms.yaml.")
    parser.add_argument("--env", default=config.DEFAULT_ENVIRONMENT,
                        help="environment to build (default: default)")
    parser.add_argument("--lab", default=None,
                        help="lab file (default: the environment's vms.yaml)")
    parser.add_argument("--only", nargs="+", help="build only these VMs")
    parser.add_argument("--recreate", action="store_true", help="delete existing VMs first")
    parser.add_argument("--report", default=None, help="write the build report as JSON")
    parser.add_argument("--plan-only", action="store_true", help="print the device plan, do not connect")
    args = parser.parse_args()

    try:
        env = config.load_environment(args.env)
        lab = config.load_lab(args.lab or env.lab_path)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    for warning in lab.warnings:
        print(f"warning: {warning}")

    if args.plan_only:
        for vm in lab.vms:
            if args.only and vm.name not in args.only:
                continue
            plan = specs.plan_vm(vm)
            print(f"{vm.name} ({vm.guest_id}, {vm.cpus}vCPU, {vm.memory_mb}MB, {vm.firmware})")
            for ctrl in plan.controllers:
                print(f"  controller {ctrl.kind} key={ctrl.key}")
            for disk in plan.disks:
                d = vm.disks[disk.index]
                print(
                    f"  disk {disk.index}: {d.size_gb}G {d.controller} "
                    f"key={disk.key} ctl={disk.controller_key} unit={disk.unit_number} "
                    f"ds={d.datastore} thin={d.thin} eager={d.eager}"
                )
            for nic in plan.nics:
                n = vm.nics[nic.index]
                print(f"  nic {nic.index}: {n.type} on {n.network} key={nic.key}")
            if plan.cdrom_key is not None:
                print(f"  cdrom key={plan.cdrom_key} ctl={plan.cdrom_controller_key} unit={plan.cdrom_unit_number}")
            if vm.video_3d or vm.video_ram_kb != 16384:
                print(f"  video (post-create edit): {vm.video_ram_kb // 1024}MB 3d={vm.video_3d}")
            print(f"  boot: {' > '.join(vm.boot_order)}  tpm={vm.tpm} gpu={vm.gpu.mode if vm.gpu else None}")
            print()
        return 0

    cfg = config.vcenter_config_for_env(env)
    print(f"environment {env.name!r}: building {len(lab.vms)} VMs on {cfg.host}:{cfg.port}")
    try:
        report = build.build_lab(cfg, lab, only=args.only, recreate=args.recreate)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    build.print_report(report)
    if args.report:
        build.write_report(report, args.report)
    failed = sum(len(vm.failed) for vm in report.vms)
    if failed:
        print(f"\n{failed} step(s) failed — see above. Core VM creates succeeded; "
              "failures are usually best-effort steps (TPM/GPU) the simulator declined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
