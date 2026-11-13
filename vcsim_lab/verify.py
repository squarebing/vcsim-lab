"""Verify the live lab against vms.yaml.

Core checks (existence, CPU, memory, guest, firmware, disks, NICs, boot
order, power, video 3D, template flag, vAppConfig) must match. TPM, GPU
and Secure Boot are best-effort: vcsim accepts the vTPM and GPU devices
but serves some of their detail back degraded, and it drops the
efiSecureBootEnabled flag entirely even though the flag is sent (verified
against govmomi v0.56.2), so a mismatch there is a warning, not a
failure — the report says which. Templates and vAppConfig are core:
vcsim serves config.template and the full vAppConfig back reliably.

The per-VM attributes are checked against the resolved assignment
(seed-resolved, see config.resolve_random_assignments): CBT, power
state and boot delay are core (vcsim serves them reliably); tools
status is best-effort — it is guest-reported with no setter API and
vcsim always reports toolsNotInstalled, so a mismatch is a warning.
"""

from __future__ import annotations

import json

from .config import (
    POWER_STATES,
    TOOLS_STATUSES,
    LabConfig,
    ResolvedAssignment,
    VCenterConfig,
    VmSpec,
    resolve_random_assignments,
)
from .query import summarize_lab
from .vcenter import connect, disconnect


def _check(label: str, expected, actual, failures: list, warnings: list, best_effort: bool = False):
    if expected == actual:
        return
    msg = f"{label}: expected {expected!r}, live is {actual!r}"
    (warnings if best_effort else failures).append(msg)


def _default_assignment(spec: VmSpec) -> ResolvedAssignment:
    """Fallback when no resolved assignment is given (offline tests):
    explicit values pass through; "random" fields take the neutral
    default (CBT off, standard boot delay, tools not installed)."""
    if spec.template:
        power_state = "poweredOff"
    elif spec.power_state in POWER_STATES:
        power_state = spec.power_state
    else:
        power_state = "poweredOn" if spec.power_on else "poweredOff"
    return ResolvedAssignment(
        cbt=spec.cbt is True,
        power_state=power_state,
        fast_boot=spec.fast_boot is True,
        tools_status=(
            spec.tools_status if spec.tools_status in TOOLS_STATUSES else "toolsNotInstalled"
        ),
    )


def verify_vm(
    spec: VmSpec,
    live: dict | None,
    assigned: ResolvedAssignment | None = None,
) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    if live is None:
        return [f"VM {spec.name!r} does not exist"], []
    assigned = assigned or _default_assignment(spec)
    _check("guest_id", spec.guest_id, live["guest_id"], failures, warnings)
    _check("cpus", spec.cpus, live["cpus"], failures, warnings)
    _check("memory_mb", spec.memory_mb, live["memory_mb"], failures, warnings)
    _check("firmware", spec.firmware, live["firmware"], failures, warnings)
    _check("hardware_version", spec.hardware_version, live["hardware_version"], failures, warnings)
    _check("power", assigned.power_state, live["power"], failures, warnings)
    _check("boot_order", spec.boot_order, live["boot_order"], failures, warnings)
    _check("template", spec.template, live.get("template", False), failures, warnings)
    # CBT and boot delay are core: vcsim serves both reliably. Checks
    # skip when the live dict has no value (offline test fixtures).
    if live.get("cbt") is not None:
        _check("cbt", assigned.cbt, live["cbt"], failures, warnings)
    if live.get("boot_delay_ms") is not None:
        _check(
            "boot_delay_ms",
            assigned.expected_boot_delay_ms(),
            live["boot_delay_ms"],
            failures,
            warnings,
        )
    if spec.vapp is not None:
        live_vapp = live.get("vapp") or {}
        product = live_vapp.get("product") or {}
        _check(
            "vapp product",
            (spec.vapp.name, spec.vapp.vendor, spec.vapp.version),
            (product.get("name"), product.get("vendor"), product.get("version")),
            failures,
            warnings,
        )
        _check(
            "vapp properties",
            {p.id: p.value for p in spec.vapp.properties},
            {p["id"]: p["value"] for p in live_vapp.get("properties", [])},
            failures,
            warnings,
        )

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
    # Tools status is guest-reported with no setter API; vcsim always
    # reports toolsNotInstalled, so a mismatch is a warning, not a
    # failure — the declared value is what the lab *expects*, not what
    # the simulator can serve.
    if live.get("tools_status") is not None:
        _check(
            "tools_status",
            assigned.tools_status,
            live["tools_status"],
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
    assignments = resolve_random_assignments(lab)
    result = {"vms": {}, "failures": 0, "warnings": 0}
    for spec in lab.vms:
        failures, warnings = verify_vm(spec, live_by_name.get(spec.name), assignments[spec.name])
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
