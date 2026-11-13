"""pyVmomi device builders: controllers, disks, NICs, CD-ROM, TPM, GPU, boot.

Two rules that shape this module:

1. New devices in a CreateVM spec use *negative temporary keys*
   (e.g. -100 for the first controller). vCenter replaces them with real
   keys at create time; other devices in the same spec refer to the
   temporary key (a disk's ``controllerKey`` is its controller's temp key).
2. Boot order, TPM, GPU and video are applied *after* the VM exists, as
   separate ReconfigVM tasks using the VM's real device keys. That keeps
   one unsupported device from failing the whole create, and makes the
   build report honest about what the simulator actually accepted. Video
   must be post-create: vcsim replaces a create-time video card with its
   ESXi default (4 MB, no 3D), but honours a ReconfigVM edit of the
   existing card.
"""

from __future__ import annotations

from pyVmomi import vim

from .config import GpuSpec, NicSpec, VmSpec, parse_hex_id, to_signed_short

# Temporary key ranges for devices created in one spec. They only need to be
# unique within the spec; vCenter assigns the real keys. Video has no temp
# key: the video card is edited post-create, never added (see build_video_edit).
KEY_CONTROLLER = -100
KEY_CDROM = -300
KEY_DISK = -200
KEY_NIC = -400
KEY_TPM = -500
KEY_GPU = -600

# PCI class codes for display devices (VGA controller / 3D controller).
PCI_CLASS_DISPLAY = (0x0300, 0x0302)


def _device_spec(device, operation: str = "add") -> vim.vm.device.VirtualDeviceSpec:
    spec = vim.vm.device.VirtualDeviceSpec()
    spec.operation = operation
    spec.device = device
    return spec


def _connect_info(start_connected: bool = True) -> vim.vm.device.VirtualDevice.ConnectInfo:
    info = vim.vm.device.VirtualDevice.ConnectInfo()
    info.startConnected = start_connected
    info.allowGuestControl = True
    info.connected = True
    return info


def _description(label: str, summary: str = "") -> vim.Description:
    desc = vim.Description()
    desc.label = label
    desc.summary = summary or label
    return desc


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------

_CONTROLLER_CLASS = {
    "nvme": vim.vm.device.VirtualNVMEController,
    "pvscsi": vim.vm.device.ParaVirtualSCSIController,
    "lsi-sas": vim.vm.device.VirtualLsiLogicSASController,
    "lsi-logic": vim.vm.device.VirtualLsiLogicController,
    "ahci": vim.vm.device.VirtualAHCIController,
}

_CONTROLLER_LABEL = {
    "nvme": "NVMe Controller",
    "pvscsi": "VMware Paravirtual SCSI Controller",
    "lsi-sas": "LSI Logic SAS Controller",
    "lsi-logic": "LSI Logic Controller",
    "ahci": "SATA Controller",
}


def build_controller(kind: str, key: int) -> vim.vm.device.VirtualDeviceSpec:
    """Build a controller add-spec. SCSI controllers get bus sharing off
    and hot-add enabled; NVMe/AHCI have no such settings."""
    cls = _CONTROLLER_CLASS[kind]
    ctrl = cls()
    ctrl.key = key
    ctrl.deviceInfo = _description(_CONTROLLER_LABEL[kind])
    ctrl.busNumber = 0
    if kind in ("pvscsi", "lsi-sas", "lsi-logic"):
        ctrl.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        ctrl.hotAddRemove = True
    return _device_spec(ctrl)


def is_scsi_controller(kind: str) -> bool:
    return kind in ("pvscsi", "lsi-sas", "lsi-logic")


# ---------------------------------------------------------------------------
# Disks
# ---------------------------------------------------------------------------


def build_disk(
    vm_name: str,
    size_gb: int,
    datastore_name: str,
    key: int,
    controller_key: int,
    unit_number: int,
    thin: bool = True,
    eager: bool = False,
    disk_mode: str = "persistent",
    disk_index: int = 0,
) -> vim.vm.device.VirtualDeviceSpec:
    disk = vim.vm.device.VirtualDisk()
    disk.key = key
    disk.controllerKey = controller_key
    disk.unitNumber = unit_number
    disk.capacityInKB = size_gb * 1024 * 1024
    disk.deviceInfo = _description(f"Hard disk {disk_index + 1}", f"{size_gb} GB")

    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    backing.diskMode = disk_mode
    backing.thinProvisioned = thin
    backing.eagerlyScrub = bool(eager and not thin)
    backing.fileName = f"[{datastore_name}] {vm_name}/{vm_name}.vmdk" if disk_index == 0 else (
        f"[{datastore_name}] {vm_name}/{vm_name}_{disk_index}.vmdk"
    )
    disk.backing = backing

    spec = _device_spec(disk)
    spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
    return spec


# ---------------------------------------------------------------------------
# NICs
# ---------------------------------------------------------------------------

_NIC_CLASS = {
    "vmxnet3": vim.vm.device.VirtualVmxnet3,
    "e1000": vim.vm.device.VirtualE1000,
    "e1000e": vim.vm.device.VirtualE1000e,
}


def _nic_backing(network_obj, network_type: str, opaque: tuple[str, str] | None = None):
    """Backing for a NIC, selected by the network's inventory type."""
    if network_type == "dvpg":
        backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
        port = vim.dvs.PortConnection()
        port.portgroupKey = network_obj.key
        port.switchUuid = network_obj.config.distributedVirtualSwitch.uuid
        backing.port = port
        return backing
    if network_type == "opaque":
        # opaqueNetworkId/Type are fetched by the builder via leaf
        # property retrieval (see vcenter.retrieve_props) rather than
        # read off network.summary, which vcsim does not serve reliably.
        backing = vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo()
        backing.opaqueNetworkId, backing.opaqueNetworkType = opaque
        return backing
    # vcsim (and real vCenter) resolve a standard-network NIC by the
    # backing's deviceName: vcsim's configureDevice does
    # FindByName(b.DeviceName, dc.Network).Reference(), so an empty
    # deviceName nil-dereferences and kills the simulator process
    # (govmomi v0.56.2 simulator/virtual_machine.go:1537). Always set it.
    backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
    backing.network = network_obj
    backing.deviceName = network_obj.name
    return backing


def build_nic(
    nic: NicSpec,
    key: int,
    network_obj,
    network_type: str,
    label: str = "Network adapter 1",
    opaque: tuple[str, str] | None = None,
) -> vim.vm.device.VirtualDeviceSpec:
    card = _NIC_CLASS[nic.type]()
    card.key = key
    card.deviceInfo = _description(label, network_obj.name)
    card.addressType = "generated"  # vCenter assigns the MAC
    card.wakeOnLanEnabled = True
    card.backing = _nic_backing(network_obj, network_type, opaque)
    card.connectable = _connect_info()
    return _device_spec(card)


# ---------------------------------------------------------------------------
# CD-ROM / video
# ---------------------------------------------------------------------------


def build_cdrom(key: int, controller_key: int, unit_number: int) -> vim.vm.device.VirtualDeviceSpec:
    cdrom = vim.vm.device.VirtualCdrom()
    cdrom.key = key
    cdrom.controllerKey = controller_key
    cdrom.unitNumber = unit_number
    cdrom.deviceInfo = _description("CD/DVD drive 1")
    backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
    backing.exclusive = False
    cdrom.backing = backing
    cdrom.connectable = _connect_info(start_connected=False)
    return _device_spec(cdrom)


def build_video_edit(key: int, video_ram_kb: int, enable_3d: bool) -> vim.vm.device.VirtualDeviceSpec:
    """Edit the VM's existing (default) video card in place.

    ``key`` is the card's real key (500 on vcsim). A video card in the
    CreateVM spec is silently replaced by vcsim's default card, so 3D and
    VRAM are set here, post-create; real vCenter honours the edit too.
    """
    card = vim.vm.device.VirtualVideoCard()
    card.key = key
    card.videoRamSizeInKB = video_ram_kb
    card.numDisplays = 1
    card.useAutoDetect = True
    card.enable3DSupport = enable_3d
    if enable_3d:
        # Shared graphics memory for the 3D renderer (256 MB).
        card.graphicsMemorySizeInKB = 262144
        card.use3dRenderer = "automatic"
    return _device_spec(card, operation="edit")


# ---------------------------------------------------------------------------
# TPM
# ---------------------------------------------------------------------------


def has_tpm(hw_devices) -> bool:
    """True when the device list contains a vTPM.

    Real vCenter serves it as a VirtualTPM; vcsim serves it back as the
    base VirtualDevice type with the label "Virtual TPM", so match both.
    """
    for dev in hw_devices:
        if isinstance(dev, vim.vm.device.VirtualTPM):
            return True
        if getattr(dev.deviceInfo, "label", "") == "Virtual TPM":
            return True
    return False


def build_tpm(key: int = KEY_TPM) -> vim.vm.device.VirtualDeviceSpec:
    """A virtual TPM 2.0 device. Requires EFI firmware and hardware
    version 14+; on real vCenter it also needs a Key Provider configured,
    which vcsim does not have — so this is applied best-effort and the
    build report records whether the simulator accepted it."""
    tpm = vim.vm.device.VirtualTPM()
    tpm.key = key
    tpm.deviceInfo = _description("Virtual TPM")
    return _device_spec(tpm)


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


def find_gpu_pci_devices(hosts) -> list[tuple]:
    """Display-class PCI devices on the given hosts.

    Returns [(host, pci_device)] for devices whose class is VGA (0x0300)
    or 3D controller (0x0302). Only devices flagged passthru-capable by
    the host are returned when that info is available.
    """
    found = []
    for host in hosts:
        hardware = getattr(host, "hardware", None)
        if hardware is None:
            continue
        # vcsim does not serve HostSystem.config, so passthru eligibility
        # is unknown there; on real vCenter it filters to capable devices.
        passthru = {}
        try:
            config = host.config
        except Exception:
            config = None
        if config is not None:
            for info in config.pciPassthruInfo or []:
                passthru[info.id] = info
        for dev in hardware.pciDevice or []:
            if dev.classId not in PCI_CLASS_DISPLAY:
                continue
            info = passthru.get(dev.id)
            if info is not None and not info.passthruCapable:
                continue
            found.append((host, dev))
    return found


def build_gpu_passthrough_device(key: int, pci_device, system_uuid: str) -> vim.vm.device.VirtualDeviceSpec:
    """PCI passthrough (DirectPath I/O) bound to one concrete host device.

    The backing values come straight from HostPciDevice, per the API docs:
    deviceId is the retrieved short converted as-is to a string, vendorId
    is the retrieved short (either may be negative on the wire), id is the
    PCI name id, and systemId is the ID of the system the device is
    attached to (the host's system UUID)."""
    backing = vim.vm.device.VirtualPCIPassthrough.DeviceBackingInfo()
    backing.id = pci_device.id
    backing.deviceId = str(pci_device.deviceId)
    backing.systemId = system_uuid
    backing.vendorId = pci_device.vendorId
    device = vim.vm.device.VirtualPCIPassthrough()
    device.key = key
    device.deviceInfo = _description("PCI device", pci_device.deviceName or pci_device.id)
    device.backing = backing
    return _device_spec(device)


def build_gpu_dynamic(key: int, vendor_id: int, device_id: int) -> vim.vm.device.VirtualDeviceSpec:
    """Dynamic DirectPath I/O: vCenter picks a matching device at power-on
    from the allowed vendor/device pair. No specific host device is needed,
    which is what makes this usable against a simulator with no GPU."""
    allowed = vim.vm.device.VirtualPCIPassthrough.AllowedDevice()
    allowed.vendorId = vendor_id
    allowed.deviceId = device_id
    backing = vim.vm.device.VirtualPCIPassthrough.DynamicBackingInfo()
    backing.allowedDevice = [allowed]
    device = vim.vm.device.VirtualPCIPassthrough()
    device.key = key
    device.deviceInfo = _description("PCI device", f"Dynamic DirectPath {vendor_id:#06x}:{device_id:#06x}")
    device.backing = backing
    return _device_spec(device)


def build_gpu_vgpu(key: int, profile: str) -> vim.vm.device.VirtualDeviceSpec:
    """NVIDIA vGPU (shared GPU) via a vGPU profile name, e.g.
    "nvidia_a100-40c". The profile must exist on the host for a real
    power-on; vcsim stores the profile name without validating it."""
    backing = vim.vm.device.VirtualPCIPassthrough.VmiopBackingInfo()
    backing.vgpu = profile
    device = vim.vm.device.VirtualPCIPassthrough()
    device.key = key
    device.deviceInfo = _description("Shared PCI Device", f"vGPU profile {profile}")
    device.backing = backing
    return _device_spec(device)


def gpu_device_spec(gpu: GpuSpec, hosts, key: int = KEY_GPU) -> tuple[vim.vm.device.VirtualDeviceSpec | None, str]:
    """Build the GPU device spec for a VM, with fallback.

    Returns (spec_or_None, note). `passthrough` binds a real display-class
    host PCI device when one exists and falls back to a dynamic backing
    otherwise; `dynamic` and `vgpu` build directly."""
    if gpu.mode == "vgpu":
        return build_gpu_vgpu(key, gpu.profile), f"vGPU profile {gpu.profile}"
    vendor_id = parse_hex_id(gpu.vendor_id, "vendor_id")
    device_id = parse_hex_id(gpu.device_id, "device_id")
    if gpu.mode == "dynamic":
        return build_gpu_dynamic(key, vendor_id, device_id), (
            f"dynamic DirectPath {gpu.vendor_id}:{gpu.device_id} ({gpu.label})"
        )
    # passthrough: prefer a concrete host device, fall back to dynamic.
    candidates = find_gpu_pci_devices(hosts)
    for host, dev in candidates:
        if dev.vendorId == to_signed_short(vendor_id) and dev.deviceId == to_signed_short(device_id):
            system_uuid = host.hardware.systemInfo.uuid
            return build_gpu_passthrough_device(key, dev, system_uuid), (
                f"passthrough {dev.deviceName or dev.id} on host {host.name}"
            )
    if candidates:
        host, dev = candidates[0]
        system_uuid = host.hardware.systemInfo.uuid
        return build_gpu_passthrough_device(key, dev, system_uuid), (
            f"passthrough {dev.deviceName or dev.id} on host {host.name} "
            f"(no exact {gpu.vendor_id}:{gpu.device_id} match; used first GPU-class device)"
        )
    return build_gpu_dynamic(key, vendor_id, device_id), (
        f"no GPU-class PCI device on any host; fell back to dynamic DirectPath "
        f"{gpu.vendor_id}:{gpu.device_id} ({gpu.label})"
    )


# ---------------------------------------------------------------------------
# Boot order
# ---------------------------------------------------------------------------


def resolve_boot_devices(vm, symbolic_order: list[str]) -> list:
    """Resolve symbolic boot tokens to BootableDevice objects using the
    VM's real device keys. Tokens: disk, disk:2, network, network:2,
    cdrom, floppy. Unknown/missing devices are skipped with a warning
    returned alongside."""
    disks = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
    nics = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualEthernetCard)]
    cdroms = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualCdrom)]
    floppies = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualFloppy)]

    pools = {"disk": disks, "network": nics, "cdrom": cdroms, "floppy": floppies}
    makers = {
        "disk": vim.vm.BootOptions.BootableDiskDevice,
        "network": vim.vm.BootOptions.BootableEthernetDevice,
        "cdrom": vim.vm.BootOptions.BootableCdromDevice,
        "floppy": vim.vm.BootOptions.BootableFloppyDevice,
    }
    order, warnings = [], []
    for token in symbolic_order:
        kind, _, index = token.partition(":")
        idx = int(index) - 1 if index else 0
        pool = pools.get(kind, [])
        if idx < 0 or idx >= len(pool):
            warnings.append(f"boot token {token!r}: no such device on {vm.name}; skipped")
            continue
        device = makers[kind]()
        # Only disk and network boot entries carry a deviceKey; the CD-ROM
        # and floppy boot entries have no properties at all (the API boots
        # "the" CD-ROM / floppy drive).
        if kind in ("disk", "network"):
            device.deviceKey = pool[idx].key
        order.append(device)
    return order, warnings


def build_boot_options(
    boot_devices: list,
    secure_boot: bool = False,
    boot_delay_ms: int = 0,
    boot_retry: bool = False,
) -> vim.vm.BootOptions:
    opts = vim.vm.BootOptions()
    opts.bootOrder = boot_devices
    opts.efiSecureBootEnabled = secure_boot
    opts.bootDelay = boot_delay_ms
    opts.bootRetryEnabled = boot_retry
    if boot_retry:
        opts.bootRetryDelay = 10000
    return opts


def describe_boot_order(vm) -> list[str]:
    """Read back a VM's boot order as symbolic tokens (disk, network,
    cdrom, ...) by matching device keys to device types."""
    boot = getattr(vm.config, "bootOptions", None)
    if boot is None or not boot.bootOrder:
        return []
    by_key = {d.key: d for d in vm.config.hardware.device}
    disks = [d.key for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
    nics = [d.key for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualEthernetCard)]
    out = []
    for entry in boot.bootOrder:
        # CD-ROM / floppy entries carry no deviceKey; identify by type.
        if isinstance(entry, vim.vm.BootOptions.BootableCdromDevice):
            out.append("cdrom")
            continue
        if isinstance(entry, vim.vm.BootOptions.BootableFloppyDevice):
            out.append("floppy")
            continue
        key = getattr(entry, "deviceKey", None)
        dev = by_key.get(key)
        if isinstance(dev, vim.vm.device.VirtualDisk):
            n = disks.index(key) + 1
            out.append("disk" if n == 1 else f"disk:{n}")
        elif isinstance(dev, vim.vm.device.VirtualEthernetCard):
            n = nics.index(key) + 1
            out.append("network" if n == 1 else f"network:{n}")
        else:
            out.append(f"device:{key}")
    return out
