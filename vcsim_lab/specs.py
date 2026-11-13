"""Device planning (pure, offline-testable) and CreateVM spec assembly.

``plan_vm`` turns a VmSpec into a concrete device layout — which
controllers exist, which key each device gets, which controller/unit each
disk and the CD-ROM attach to — without touching pyVmomi managed objects,
so it can be unit-tested with no vCenter. ``build_create_spec`` then turns
a plan plus resolved inventory references into a vim.vm.ConfigSpec.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyVmomi import vim

from . import devices
from .config import VmSpec


@dataclass
class ControllerPlan:
    kind: str
    key: int


@dataclass
class DiskPlan:
    index: int
    controller_kind: str
    controller_key: int
    unit_number: int
    key: int


@dataclass
class NicPlan:
    index: int
    key: int


@dataclass
class VmPlan:
    controllers: list[ControllerPlan] = field(default_factory=list)
    disks: list[DiskPlan] = field(default_factory=list)
    nics: list[NicPlan] = field(default_factory=list)
    cdrom_key: int | None = None
    cdrom_controller_key: int | None = None
    cdrom_unit_number: int | None = None

    def controller_key(self, kind: str) -> int:
        for ctrl in self.controllers:
            if ctrl.kind == kind:
                return ctrl.key
        raise KeyError(f"no {kind} controller in plan")


def _scsi_unit_numbers(count: int) -> list[int]:
    """SCSI unit numbers 0-6, 8-15 (7 is the controller itself)."""
    units = [u for u in range(16) if u != 7]
    if count > len(units):
        raise ValueError(f"too many disks on one SCSI controller: {count} (max 15)")
    return units[:count]


def plan_vm(spec: VmSpec) -> VmPlan:
    """Compute the device layout for a VM spec.

    One controller per controller kind used by the VM's disks; a CD-ROM
    gets its own AHCI controller unless an AHCI disk already created one.
    """
    plan = VmPlan()
    next_controller_key = devices.KEY_CONTROLLER

    def controller_for(kind: str) -> int:
        nonlocal next_controller_key
        for ctrl in plan.controllers:
            if ctrl.kind == kind:
                return ctrl.key
        key = next_controller_key
        next_controller_key -= 1
        plan.controllers.append(ControllerPlan(kind=kind, key=key))
        return key

    # Disks, grouped by controller kind for unit-number assignment.
    by_kind: dict[str, list[int]] = {}
    for i, disk in enumerate(spec.disks):
        by_kind.setdefault(disk.controller, []).append(i)
    disk_units: dict[int, int] = {}
    for kind, indexes in by_kind.items():
        if devices.is_scsi_controller(kind):
            units = _scsi_unit_numbers(len(indexes))
        else:
            units = list(range(len(indexes)))
        for idx, unit in zip(indexes, units):
            disk_units[idx] = unit

    for i, disk in enumerate(spec.disks):
        plan.disks.append(
            DiskPlan(
                index=i,
                controller_kind=disk.controller,
                controller_key=controller_for(disk.controller),
                unit_number=disk_units[i],
                key=devices.KEY_DISK - i,
            )
        )

    if spec.cdrom is not None:
        ahci_key = controller_for("ahci")
        # CD-ROM takes the next free AHCI unit after any AHCI disks.
        used = [d.unit_number for d in plan.disks if d.controller_kind == "ahci"]
        plan.cdrom_controller_key = ahci_key
        plan.cdrom_unit_number = (max(used) + 1) if used else 0
        plan.cdrom_key = devices.KEY_CDROM

    for i in range(len(spec.nics)):
        plan.nics.append(NicPlan(index=i, key=devices.KEY_NIC - i))

    return plan


@dataclass
class ResolvedRefs:
    """Inventory objects a create needs, resolved once by the builder."""

    folder: vim.Folder
    resource_pool: vim.ResourcePool
    datastores: dict[str, vim.Datastore]
    networks: dict[str, object]  # logical name -> network managed object
    network_types: dict[str, str]  # logical name -> standard | dvpg | opaque
    opaque: dict[str, tuple[str, str]] = field(default_factory=dict)  # logical -> (id, type)


def build_create_spec(spec: VmSpec, plan: VmPlan, refs: ResolvedRefs) -> vim.vm.ConfigSpec:
    """Build the CreateVM_Task spec.

    Covers the core devices only (controllers, disks, NICs, CD-ROM).
    Boot order, TPM, GPU and video are applied afterwards by the builder
    as separate reconfig tasks — see devices.py for why.
    """
    device_change: list[vim.vm.device.VirtualDeviceSpec] = []

    for ctrl in plan.controllers:
        device_change.append(devices.build_controller(ctrl.kind, ctrl.key))

    home_datastore = spec.disks[0].datastore
    for disk_plan in plan.disks:
        disk = spec.disks[disk_plan.index]
        device_change.append(
            devices.build_disk(
                vm_name=spec.name,
                size_gb=disk.size_gb,
                datastore_name=disk.datastore,
                key=disk_plan.key,
                controller_key=disk_plan.controller_key,
                unit_number=disk_plan.unit_number,
                thin=disk.thin,
                eager=disk.eager,
                disk_mode=disk.disk_mode,
                disk_index=disk_plan.index,
            )
        )

    if plan.cdrom_key is not None:
        device_change.append(
            devices.build_cdrom(plan.cdrom_key, plan.cdrom_controller_key, plan.cdrom_unit_number)
        )

    for nic_plan in plan.nics:
        nic = spec.nics[nic_plan.index]
        device_change.append(
            devices.build_nic(
                nic,
                nic_plan.key,
                refs.networks[nic.network],
                refs.network_types[nic.network],
                label=f"Network adapter {nic_plan.index + 1}",
                opaque=refs.opaque.get(nic.network),
            )
        )

    files = vim.vm.FileInfo()
    files.vmPathName = f"[{home_datastore}] {spec.name}/{spec.name}.vmx"

    config = vim.vm.ConfigSpec(
        name=spec.name,
        guestId=spec.guest_id,
        version=spec.hardware_version,
        uuid=None,
        numCPUs=spec.cpus,
        numCoresPerSocket=spec.cores_per_socket,
        memoryMB=spec.memory_mb,
        cpuHotAddEnabled=spec.cpu_hot_add,
        memoryHotAddEnabled=spec.memory_hot_add,
        firmware=spec.firmware,
        annotation=spec.annotation or spec.os_label,
        files=files,
        deviceChange=device_change,
    )

    extra = dict(spec.extra_config)
    if spec.gpu and spec.gpu.mode in ("passthrough", "dynamic"):
        # DirectPath I/O needs full memory reservation and 64-bit MMIO
        # sized to the GPU framebuffer (Broadcom KB 334594).
        config.memoryAllocation = vim.ResourceAllocationInfo(
            reservation=spec.memory_mb,
            expandableReservation=False,
            limit=-1,
            shares=vim.SharesInfo(level=vim.SharesInfo.Level.normal),
        )
        extra.setdefault("pciPassthru.use64bitMMIO", "TRUE")
        extra.setdefault("pciPassthru.64bitMMIOSizeGB", str(spec.gpu.mmio_size_gb))
    if extra:
        config.extraConfig = [vim.option.OptionValue(key=k, value=v) for k, v in sorted(extra.items())]

    return config
