"""Query the lab: the pyVmomi read path, mirroring what the builder wrote.

``summarize_vm`` is the reference implementation for "give me everything
about a VM" — OS, CPU/memory, firmware, TPM, GPU, boot order, disks and
NICs — using plain attribute access on the objects a ContainerView
returns. For large inventories the same fields can be pulled through a
PropertyCollector (see ``summarize_vm``'s notes), but for a lab of this
size the direct read is clearer and fast enough.
"""

from __future__ import annotations

import json

from pyVmomi import vim

from . import devices
from .vcenter import connect, disconnect, list_objs, retrieve_props_multi


# ---------------------------------------------------------------------------
# VM summaries
# ---------------------------------------------------------------------------


def _gpu_info(device) -> dict:
    backing = device.backing
    info = {"type": type(device).__name__}
    if isinstance(backing, vim.vm.device.VirtualPCIPassthrough.DeviceBackingInfo):
        info.update(
            {
                "mode": "passthrough",
                "id": backing.id,
                "device_id": backing.deviceId,
                "system_id": backing.systemId,
                "vendor_id": backing.vendorId,
            }
        )
    elif isinstance(backing, vim.vm.device.VirtualPCIPassthrough.DynamicBackingInfo):
        allowed = [
            {"vendor_id": a.vendorId, "device_id": a.deviceId} for a in (backing.allowedDevice or [])
        ]
        info.update({"mode": "dynamic", "allowed_devices": allowed, "assigned_id": backing.assignedId})
    elif isinstance(backing, vim.vm.device.VirtualPCIPassthrough.VmiopBackingInfo):
        info.update({"mode": "vgpu", "profile": backing.vgpu})
    else:
        # vcsim serves some backings (notably DynamicBackingInfo) back as
        # the base DeviceBackingInfo, losing the mode detail; the PCI
        # device itself is still there, so report it as present but
        # undescribed rather than inventing a mode.
        info.update({"mode": "unknown", "detail": "backing detail not served by vcsim"})
    return info


def _gpu_short(gpu: dict) -> str:
    """One-line GPU description for the table, e.g. ``vgpu:nvidia_a100-40c``."""
    mode = gpu.get("mode") or "?"
    detail = gpu.get("profile") or gpu.get("id") or ""
    if not detail and gpu.get("allowed_devices"):
        first = gpu["allowed_devices"][0]
        detail = f"0x{first['vendor_id']:04x}:0x{first['device_id']:04x}"
    return f"{mode}:{detail}" if detail else mode


def _disk_info(device, datastore_names: dict) -> dict:
    backing = device.backing
    ds_name = None
    file_name = getattr(backing, "fileName", "") or ""
    if file_name.startswith("["):
        ds_name = file_name[1:].split("]", 1)[0]
    return {
        "label": device.deviceInfo.label,
        "capacity_gb": round(device.capacityInKB / 1024 / 1024, 1),
        "datastore": ds_name,
        "thin": getattr(backing, "thinProvisioned", None),
        "eagerly_scrubbed": getattr(backing, "eagerlyScrub", None),
        "disk_mode": getattr(backing, "diskMode", None),
        "controller_key": device.controllerKey,
        "unit_number": device.unitNumber,
        "file": file_name,
    }


def _nic_info(device) -> dict:
    backing = device.backing
    if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
        network, backing_type = backing.network.name, "standard"
    elif isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
        network = backing.port.portgroupKey
        backing_type = "dvpg"
    elif isinstance(backing, vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo):
        network = backing.opaqueNetworkId
        backing_type = "opaque"
    else:
        network, backing_type = None, type(backing).__name__ if backing else None
    return {
        "label": device.deviceInfo.label,
        "type": type(device).__name__,
        "network": network,
        "backing": backing_type,
        "mac": device.macAddress,
        "connected": bool(device.connectable.connected) if device.connectable else None,
    }


def summarize_vm(vm: vim.VirtualMachine, runtime: dict | None = None) -> dict:
    """Everything the lab tracks about one VM, as plain JSON-able data.

    ``runtime`` carries leaf properties fetched by ``summarize_lab``
    (power state, host name) — ``vm.runtime`` / ``vm.summary`` as whole
    objects cannot be deserialized against vcsim (see
    ``vcenter.retrieve_props``), so they are never touched here.
    """
    runtime = runtime or {}
    config = vm.config
    hw = config.hardware
    controllers = {}
    for dev in hw.device:
        if isinstance(dev, vim.vm.device.VirtualController):
            controllers[dev.key] = type(dev).__name__.replace("Virtual", "")

    disks = [d for d in hw.device if isinstance(d, vim.vm.device.VirtualDisk)]
    nics = [d for d in hw.device if isinstance(d, vim.vm.device.VirtualEthernetCard)]
    video = [d for d in hw.device if isinstance(d, vim.vm.device.VirtualVideoCard)]
    tpm = devices.has_tpm(hw.device)
    gpus = [d for d in hw.device if isinstance(d, vim.vm.device.VirtualPCIPassthrough)]
    cdrom = any(isinstance(d, vim.vm.device.VirtualCdrom) for d in hw.device)

    datastore_names = {}
    for ds in vm.datastore or []:
        datastore_names[ds._moId] = ds.name

    boot = getattr(config, "bootOptions", None)
    try:
        guest = vm.guest
        tools_status, ip = guest.toolsStatus, guest.ipAddress
    except Exception:
        tools_status, ip = None, None
    return {
        "name": vm.name,
        "path": config.files.vmPathName,
        "power": runtime.get("power"),
        "host": runtime.get("host"),
        "guest_id": config.guestId,
        "guest_name": config.guestFullName,
        "annotation": config.annotation,
        "hardware_version": config.version,
        "firmware": config.firmware,
        "secure_boot": bool(boot.efiSecureBootEnabled) if boot else False,
        "cpus": config.hardware.numCPU,
        "cores_per_socket": config.hardware.numCoresPerSocket,
        "memory_mb": config.hardware.memoryMB,
        "cpu_hot_add": config.cpuHotAddEnabled,
        "memory_hot_add": config.memoryHotAddEnabled,
        "tpm": tpm,
        "gpus": [_gpu_info(g) for g in gpus],
        "video_3d": bool(video and video[0].enable3DSupport),
        "video_ram_kb": video[0].videoRamSizeInKB if video else None,
        "cdrom": cdrom,
        "boot_order": devices.describe_boot_order(vm),
        "controllers": sorted(set(controllers.values())),
        "disks": [_disk_info(d, datastore_names) for d in disks],
        "nics": [_nic_info(n) for n in nics],
        "tools_status": tools_status,
        "ip": ip,
    }


def iter_vms(si) -> list[vim.VirtualMachine]:
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def summarize_lab(si, names: list[str] | None = None) -> list[dict]:
    vms = iter_vms(si)
    # One batched leaf retrieve for every VM's power state and host, plus
    # a host moId -> name map (host names come from the inventory list).
    leaf = retrieve_props_multi(si, vms, vim.VirtualMachine, ["runtime.powerState", "runtime.host"])
    host_names = {h._moId: h.name for h in list_objs(si.RetrieveContent(), vim.HostSystem)}
    summaries = []
    for vm in vms:
        props = leaf.get(vm._moId, {})
        host = props.get("runtime.host")
        summaries.append(
            summarize_vm(
                vm,
                runtime={
                    "power": props.get("runtime.powerState"),
                    "host": host_names.get(host._moId) if host is not None else None,
                },
            )
        )
    summaries.sort(key=lambda s: s["name"])
    if names:
        wanted = set(names)
        summaries = [s for s in summaries if s["name"] in wanted]
    return summaries


# ---------------------------------------------------------------------------
# Hosts / inventory
# ---------------------------------------------------------------------------


def list_host_pci(si) -> list[dict]:
    """Hosts with their PCI devices (GPU-class first) and vGPU capability.

    This is the read side of the GPU story: what passthrough devices and
    vGPU profiles does each host actually offer?
    """
    out = []
    for host in list_objs(si.RetrieveContent(), vim.HostSystem):
        hw = host.hardware
        # vcsim does not serve HostSystem.config (graphicsInfo,
        # sharedGpuCapabilities, pciPassthruInfo); real vCenter does.
        try:
            cfg = host.config
        except Exception:
            cfg = None
        pci = []
        for dev in hw.pciDevice or []:
            pci.append(
                {
                    "id": dev.id,
                    "class": f"{dev.classId:#06x}",
                    "vendor": dev.vendorName,
                    "vendor_id": dev.vendorId,
                    "device_id": dev.deviceId,
                    "name": dev.deviceName,
                    "is_display": dev.classId in (0x0300, 0x0302),
                }
            )
        pci.sort(key=lambda d: (not d["is_display"], d["id"]))
        out.append(
            {
                "host": host.name,
                "system_uuid": hw.systemInfo.uuid,
                "model": hw.systemInfo.model,
                "pci_devices": pci,
                "graphics": (
                    [{"device_name": g.deviceName, "vendor": g.vendorName} for g in (cfg.graphicsInfo or [])]
                    if cfg is not None
                    else None
                ),
                "shared_gpu_types": list(cfg.sharedPassthruGpuTypes or []) if cfg is not None else None,
                "shared_gpu_capabilities": (
                    [
                        {"vgpu": getattr(c, "vgpu", None), "capacity": getattr(c, "capacity", None)}
                        for c in (cfg.sharedGpuCapabilities or [])
                    ]
                    if cfg is not None
                    else None
                ),
                "config_available": cfg is not None,
            }
        )
    return out


def list_guest_ids(si) -> list[dict]:
    """Guest OS identifiers the (simulated) vCenter advertises, via the
    cluster's environment browser. Use it to check a guest_id before
    adding a new OS to vms.yaml."""
    content = si.RetrieveContent()
    clusters = list_objs(content, vim.ClusterComputeResource)
    if not clusters:
        return []
    browser = clusters[0].environmentBrowser
    try:
        descriptors = browser.guestOsDescriptor or []
    except Exception:
        # vcsim does not serve EnvironmentBrowser.guestOsDescriptor.
        return []
    return [
        {"id": d.id, "family": d.family, "full_name": d.fullName} for d in descriptors
    ]


def list_inventory(si) -> dict:
    content = si.RetrieveContent()
    return {
        "datacenters": [d.name for d in list_objs(content, vim.Datacenter)],
        "clusters": [c.name for c in list_objs(content, vim.ClusterComputeResource)],
        "hosts": [h.name for h in list_objs(content, vim.HostSystem)],
        "datastores": [d.name for d in list_objs(content, vim.Datastore)],
        "networks": [
            {"name": n.name, "type": type(n).__name__} for n in list_objs(content, vim.Network)
        ],
        "dvportgroups": [n.name for n in list_objs(content, vim.DistributedVirtualPortgroup)],
        "resource_pools": [p.name for p in list_objs(content, vim.ResourcePool)],
        "vms": [v.name for v in list_objs(content, vim.VirtualMachine)],
    }


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def print_table(summaries: list[dict]) -> None:
    for s in summaries:
        gpu = ", ".join(_gpu_short(g) for g in s["gpus"]) or "—"
        disks = ", ".join(
            f"{d['capacity_gb']:g}G/{d['datastore']}{' thin' if d['thin'] else ''}" for d in s["disks"]
        )
        nics = ", ".join(f"{n['type'].replace('Virtual', '')}@{n['network']}" for n in s["nics"])
        print(f"{s['name']} [{s['power']}] {s['guest_id']} {s['cpus']}vCPU {s['memory_mb']}MB {s['firmware']}")
        print(f"  hw {s['hardware_version']} tpm={s['tpm']} gpu={gpu} boot={' > '.join(s['boot_order']) or '(default)'}")
        print(f"  controllers: {', '.join(s['controllers'])}")
        print(f"  disks: {disks}")
        print(f"  nics: {nics}")
        print()


def main_json(cfg, names=None) -> str:
    si = connect(cfg)
    try:
        return json.dumps(summarize_lab(si, names), indent=2)
    finally:
        disconnect(si)
