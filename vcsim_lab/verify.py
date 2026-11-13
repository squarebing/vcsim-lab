"""Verify the live lab against vms.yaml.

Core checks (existence, CPU, memory, guest, firmware, disks, NICs, boot
order, power, video 3D) must match. TPM, GPU and Secure Boot are
best-effort: vcsim accepts the vTPM and GPU devices but serves some of
their detail back degraded, and it drops the efiSecureBootEnabled flag
entirely even though the flag is sent (verified against govmomi v0.56.2),
so a mismatch there is a warning, not a failure — the report says which.
"""

from __future__ import annotations

import json

from .config import LabConfig, VCenterConfig, VmSpec
from .query import summarize_lab
from .vcenter import connect, disconnect


def _check(label: str, expected, actual, failures: list, warnings: list, best_effort: bool = False):
    if expected == actual:
        return
    msg = f"{label}: expected {expected!r}, live is {actual!r}"
    (warnings if best_effort else failures).append(msg)


def verify_vm(spec: VmSpec, live: dict | None) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    if live is None:
        return [f"VM {spec.name!r} does not exist"], []
    _check("guest_id", spec.guest_id, live["guest_id"], failures, warnings)
    _check("cpus", spec.cpus, live["cpus"], failures, warnings)
    _check("memory_mb", spec.memory_mb, live["memory_mb"], failures, warnings)
    _check("firmware", spec.firmware, live["firmware"], failures, warnings)
    _check("hardware_version", spec.hardware_version, live["hardware_version"], failures, warnings)
    _check("power", "poweredOn" if spec.power_on else "poweredOff", live["power"], failures, warnings)
    _check("boot_order", spec.boot_order, live["boot_order"], failures, warnings)

    expected_disks = sorted((d.size_gb, d.datastore, d.thin) for d in spec.disks)
    actual_disks = sorted(
        (d["capacity_gb"], d["datastore"], d["thin"]) for d in live["disks"]
    )
    _check("disks (size_gb, datastore, thin)", expected_disks, actual_disks, failures, warnings)
    _check("nic count", len(spec.nics), len(live["nics"]), failures, warnings)
    _check("cdrom", spec.cdrom is not None, live["cdrom"], failures, warnings)
    _check("video_3d", spec.video_3d, live["video_3d"], failures, warnings)

    # Best-effort: the simulator may legitimately refuse these.
    _check("tpm", spec.tpm, live["tpm"], failures, warnings, best_effort=True)
    _check("secure_boot", spec.secure_boot, live["secure_boot"], failures, warnings, best_effort=True)
    _check(
        "gpu present",
        spec.gpu is not None,
        len(live["gpus"]) > 0,
        failures,
        warnings,
        best_effort=True,
    )
    return failures, warnings


def verify_lab(cfg: VCenterConfig, lab: LabConfig) -> dict:
    si = connect(cfg)
    try:
        live_by_name = {s["name"]: s for s in summarize_lab(si)}
    finally:
        disconnect(si)
    result = {"vms": {}, "failures": 0, "warnings": 0}
    for spec in lab.vms:
        failures, warnings = verify_vm(spec, live_by_name.get(spec.name))
        result["vms"][spec.name] = {"failures": failures, "warnings": warnings}
        result["failures"] += len(failures)
        result["warnings"] += len(warnings)
    return result


def print_result(result: dict) -> bool:
    ok = result["failures"] == 0
    for name, checks in result["vms"].items():
        failures, warnings = checks["failures"], checks["warnings"]
        if not failures and not warnings:
            print(f"{name}: OK")
            continue
        print(f"{name}:")
        for msg in failures:
            print(f"  FAIL {msg}")
        for msg in warnings:
            print(f"  WARN {msg} (best-effort: vcsim may not support it)")
    print()
    print(f"{len(result['vms'])} VMs checked, {result['failures']} failures, {result['warnings']} warnings")
    return ok


def result_json(result: dict) -> str:
    return json.dumps(result, indent=2)
