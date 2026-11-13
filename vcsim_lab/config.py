"""Configuration: load and validate ``vms.yaml`` plus connection settings.

This module has no pyVmomi dependency so the YAML matrix and the device
planner can be unit-tested without a vCenter connection.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTROLLER_TYPES = ("nvme", "pvscsi", "lsi-sas", "lsi-logic", "ahci")
NIC_TYPES = ("vmxnet3", "e1000", "e1000e")
GPU_MODES = ("passthrough", "dynamic", "vgpu")
BOOT_TOKENS = ("disk", "network", "cdrom", "floppy")

# Guest OS identifiers known to exist in vSphere 7/8. Anything else produces
# a warning (vcsim accepts arbitrary strings; real vCenter does not).
KNOWN_GUEST_IDS = {
    "windows11_64Guest",
    "windows9_64Guest",  # Windows 10 (64-bit)
    "windows9Server64Guest",  # Windows Server 2016
    "windows2019srv_64Guest",
    "windows2019srvNext_64Guest",  # Windows Server 2022
    "windows2022srvNext_64Guest",  # Windows Server 2025
    "ubuntu64Guest",
    "debian10_64Guest",
    "debian11_64Guest",
    "debian12_64Guest",
    "rhel7_64Guest",
    "rhel8_64Guest",
    "rhel9_64Guest",
    "centos7_64Guest",
    "centos8_64Guest",
    "centos9_64Guest",
    "sles15_64Guest",
    "freebsd13_64Guest",
    "freebsd14_64Guest",
    "vmwarePhoton64Guest",
    "otherLinux64Guest",
    "otherGuest",
    "otherGuest64",
}

_HW_VERSION_RE = re.compile(r"^vmx-\d+$")
_BOOT_TOKEN_RE = re.compile(r"^(disk|network|cdrom|floppy)(?::(\d+))?$")


class ConfigError(ValueError):
    """Raised when ``vms.yaml`` (or the environment) is invalid."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DiskSpec:
    size_gb: int
    controller: str = "pvscsi"
    datastore: str = "LocalDS_0"
    thin: bool = True
    eager: bool = False  # only meaningful when thin is False (thick eager-zeroed)
    disk_mode: str = "persistent"


@dataclass
class NicSpec:
    network: str  # logical name from the `networks:` map
    type: str = "vmxnet3"


@dataclass
class CdromSpec:
    backing: str = "client"  # client device (RemotePassthroughBackingInfo)


@dataclass
class GpuSpec:
    mode: str  # passthrough | dynamic | vgpu
    vendor_id: str | None = None  # hex, e.g. "0x10de" (passthrough/dynamic)
    device_id: str | None = None  # hex, e.g. "0x1eb8" (passthrough/dynamic)
    profile: str | None = None  # vGPU profile name, e.g. "nvidia_a100-40c"
    label: str = ""
    mmio_size_gb: int = 32  # pciPassthru.64bitMMIOSizeGB (passthrough/dynamic)


@dataclass
class VmSpec:
    name: str
    guest_id: str
    os_label: str = ""
    annotation: str = ""
    cpus: int = 2
    cores_per_socket: int = 1
    memory_mb: int = 4096
    hardware_version: str = "vmx-20"
    firmware: str = "bios"  # bios | efi
    secure_boot: bool = False
    tpm: bool = False
    video_3d: bool = False
    video_ram_kb: int = 16384
    gpu: GpuSpec | None = None
    folder: str = ""
    resource_pool: str = ""
    power_on: bool = False
    cpu_hot_add: bool = False
    memory_hot_add: bool = False
    disks: list[DiskSpec] = field(default_factory=list)
    nics: list[NicSpec] = field(default_factory=list)
    cdrom: CdromSpec | None = None
    boot_order: list[str] = field(default_factory=lambda: ["disk"])
    extra_config: dict[str, str] = field(default_factory=dict)


@dataclass
class NetworkSpec:
    name: str  # inventory name, e.g. "VM Network" or "DC0_DVPG0"
    type: str  # standard | dvpg | opaque
    fallback: str | None = None  # logical network to use if this one is missing


@dataclass
class LabConfig:
    datacenter: str
    cluster: str
    folders: list[str] = field(default_factory=list)
    resource_pools: list[str] = field(default_factory=list)
    networks: dict[str, NetworkSpec] = field(default_factory=dict)
    vms: list[VmSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def vm(self, name: str) -> VmSpec:
        for spec in self.vms:
            if spec.name == name:
                return spec
        raise ConfigError(f"no VM named {name!r} in vms.yaml")


@dataclass
class VCenterConfig:
    host: str = "127.0.0.1"
    port: int = 8989
    user: str = "user"
    password: str = "pass"
    insecure: bool = True  # vcsim uses a self-signed cert

    @classmethod
    def from_env(cls) -> "VCenterConfig":
        return cls(
            host=os.environ.get("VC_HOST", "127.0.0.1"),
            port=int(os.environ.get("VC_PORT", "8989")),
            user=os.environ.get("VC_USER", "user"),
            password=os.environ.get("VC_PASSWORD", "pass"),
            insecure=os.environ.get("VC_INSECURE", "1") not in ("0", "false", "False"),
        )

    @property
    def url(self) -> str:
        return f"https://{self.user}@{self.host}:{self.port}/sdk"


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------


def parse_hex_id(value: str | int, what: str) -> int:
    """Parse a PCI vendor/device id given as hex ("0x10de") or int."""
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16) if str(value).lower().startswith("0x") else int(str(value))
    except ValueError as exc:
        raise ConfigError(f"invalid {what} {value!r}: expected hex like 0x10de") from exc


def to_signed_short(value: int) -> int:
    """Convert an unsigned 16-bit PCI id to the signed short the WSDL uses.

    vSphere represents PCI vendor/device ids as signed shorts on the wire;
    values above 32767 arrive as their two's-complement negative, and the
    API docs require passing them back exactly as retrieved.
    """
    value &= 0xFFFF
    return value - 0x10000 if value > 0x7FFF else value


def _require(mapping: dict, key: str, where: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"missing required key {key!r} in {where}")
    return mapping[key]


def _load_vm(raw: dict, warnings: list[str]) -> VmSpec:
    where = f"VM {raw.get('name', '?')!r}"
    name = _require(raw, "name", where)
    guest_id = _require(raw, "guest_id", where)
    if guest_id not in KNOWN_GUEST_IDS:
        warnings.append(
            f"{where}: guest_id {guest_id!r} is not in the known-good list; "
            "vcsim will accept it but real vCenter may reject the create. "
            "Run `python scripts/query_lab.py --guest-ids` to list IDs the "
            "simulator advertises."
        )

    gpu = None
    if raw.get("gpu"):
        g = raw["gpu"]
        mode = _require(g, "mode", f"{where} gpu")
        if mode not in GPU_MODES:
            raise ConfigError(f"{where}: gpu.mode must be one of {GPU_MODES}")
        if mode in ("passthrough", "dynamic"):
            _require(g, "vendor_id", f"{where} gpu")
            _require(g, "device_id", f"{where} gpu")
            parse_hex_id(g["vendor_id"], "vendor_id")
            parse_hex_id(g["device_id"], "device_id")
        if mode == "vgpu":
            _require(g, "profile", f"{where} gpu")
        gpu = GpuSpec(
            mode=mode,
            vendor_id=g.get("vendor_id"),
            device_id=g.get("device_id"),
            profile=g.get("profile"),
            label=g.get("label", ""),
            mmio_size_gb=int(g.get("mmio_size_gb", 32)),
        )

    disks = []
    for i, d in enumerate(raw.get("disks") or []):
        if d.get("controller", "pvscsi") not in CONTROLLER_TYPES:
            raise ConfigError(f"{where}: disk {i} controller must be one of {CONTROLLER_TYPES}")
        disks.append(
            DiskSpec(
                size_gb=int(_require(d, "size_gb", f"{where} disk {i}")),
                controller=d.get("controller", "pvscsi"),
                datastore=d.get("datastore", "LocalDS_0"),
                thin=bool(d.get("thin", True)),
                eager=bool(d.get("eager", False)),
                disk_mode=d.get("disk_mode", "persistent"),
            )
        )
    if not disks:
        raise ConfigError(f"{where}: at least one disk is required")

    nics = []
    for i, n in enumerate(raw.get("nics") or []):
        if n.get("type", "vmxnet3") not in NIC_TYPES:
            raise ConfigError(f"{where}: nic {i} type must be one of {NIC_TYPES}")
        nics.append(NicSpec(network=_require(n, "network", f"{where} nic {i}"), type=n.get("type", "vmxnet3")))
    if not nics:
        raise ConfigError(f"{where}: at least one NIC is required")

    cdrom = None
    if raw.get("cdrom"):
        cdrom = CdromSpec(backing=raw["cdrom"].get("backing", "client"))

    boot_order = raw.get("boot_order") or ["disk"]
    for token in boot_order:
        if not _BOOT_TOKEN_RE.match(token):
            raise ConfigError(
                f"{where}: bad boot_order token {token!r}; use disk, disk:2, "
                "network, network:2, cdrom or floppy"
            )

    firmware = raw.get("firmware", "bios")
    if firmware not in ("bios", "efi"):
        raise ConfigError(f"{where}: firmware must be 'bios' or 'efi'")
    secure_boot = bool(raw.get("secure_boot", False))
    if secure_boot and firmware != "efi":
        raise ConfigError(f"{where}: secure_boot requires firmware 'efi'")
    if raw.get("tpm") and firmware != "efi":
        raise ConfigError(f"{where}: TPM requires firmware 'efi'")
    if gpu and firmware != "efi":
        warnings.append(f"{where}: GPU passthrough/vGPU normally needs EFI firmware")

    cpus = int(raw.get("cpus", 2))
    cores = int(raw.get("cores_per_socket", 1))
    if cpus <= 0 or cores <= 0 or cpus % cores != 0:
        raise ConfigError(f"{where}: cores_per_socket ({cores}) must divide cpus ({cpus})")
    if int(raw.get("memory_mb", 4096)) <= 0:
        raise ConfigError(f"{where}: memory_mb must be positive")
    hw = raw.get("hardware_version", "vmx-20")
    if not _HW_VERSION_RE.match(hw):
        raise ConfigError(f"{where}: hardware_version {hw!r} should look like 'vmx-20'")

    return VmSpec(
        name=name,
        guest_id=guest_id,
        os_label=raw.get("os_label", ""),
        annotation=raw.get("annotation", ""),
        cpus=cpus,
        cores_per_socket=cores,
        memory_mb=int(raw.get("memory_mb", 4096)),
        hardware_version=hw,
        firmware=firmware,
        secure_boot=secure_boot,
        tpm=bool(raw.get("tpm", False)),
        video_3d=bool(raw.get("video_3d", False)),
        video_ram_kb=int(raw.get("video_ram_kb", 16384)),
        gpu=gpu,
        folder=raw.get("folder", ""),
        resource_pool=raw.get("resource_pool", ""),
        power_on=bool(raw.get("power_on", False)),
        cpu_hot_add=bool(raw.get("cpu_hot_add", False)),
        memory_hot_add=bool(raw.get("memory_hot_add", False)),
        disks=disks,
        nics=nics,
        cdrom=cdrom,
        boot_order=list(boot_order),
        extra_config={str(k): str(v) for k, v in (raw.get("extra_config") or {}).items()},
    )


def load_lab(path: str | Path = "vms.yaml") -> LabConfig:
    """Load and validate the lab matrix. Raises ConfigError on bad input."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"lab file not found: {path} (run from the project root)")
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    warnings: list[str] = []
    lab = raw.get("lab") or {}
    networks = {}
    for logical, n in (raw.get("networks") or {}).items():
        if n.get("type") not in ("standard", "dvpg", "opaque"):
            raise ConfigError(f"network {logical!r}: type must be standard, dvpg or opaque")
        networks[logical] = NetworkSpec(
            name=_require(n, "name", f"network {logical!r}"),
            type=n["type"],
            fallback=n.get("fallback"),
        )

    vms = [_load_vm(v, warnings) for v in raw.get("vms") or []]
    names = [v.name for v in vms]
    if len(names) != len(set(names)):
        raise ConfigError(f"duplicate VM names in vms.yaml: {sorted({n for n in names if names.count(n) > 1})}")
    if not vms:
        raise ConfigError("no VMs defined in vms.yaml")

    for vm in vms:
        for nic in vm.nics:
            if nic.network not in networks:
                raise ConfigError(f"VM {vm.name!r}: nic network {nic.network!r} is not in networks:")

    return LabConfig(
        datacenter=_require(lab, "datacenter", "lab:"),
        cluster=_require(lab, "cluster", "lab:"),
        folders=list(lab.get("folders") or []),
        resource_pools=list(lab.get("resource_pools") or []),
        networks=networks,
        vms=vms,
        warnings=warnings,
    )
