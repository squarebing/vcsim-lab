"""Configuration: load and validate ``vms.yaml`` plus connection settings.

This module has no pyVmomi dependency so the YAML matrix and the device
planner can be unit-tested without a vCenter connection.

Environments: the repo holds one vCenter simulation per directory under
``environments/`` — each with ``env.yaml`` (name, description, vcsim
port, inventory flags), ``vms.yaml`` (the lab) and ``storage.yaml``
(the storage model). See ``Environment`` / ``list_environments`` /
``load_environment`` below, and README "Multiple environments".
"""

from __future__ import annotations

import os
import random
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
GPU_VENDORS = {"nvidia": 0x10DE, "amd": 0x1002}
# (vendor, model) -> PCI device id. Used to cross-check a declared model
# against device_id for passthrough/dynamic GPUs. vGPU-only models (e.g.
# H100, which the lab only uses as a vGPU profile) are not listed: a vGPU
# profile carries no PCI device id.
KNOWN_GPU_MODELS = {
    ("nvidia", "T4"): 0x1EB8,
    ("nvidia", "A100"): 0x20B0,
    ("amd", "MI210"): 0x0C34,
}
# NVIDIA vGPU profile names look like nvidia_a100-40c / grid_t4-8q
# (vendor prefix, GPU, framebuffer + license suffix). AMD has no vGPU
# profiles: MxGPU is SR-IOV virtual functions presented as PCI
# passthrough devices, so an AMD GPU must use passthrough or dynamic.
_VGPU_PROFILE_RE = re.compile(r"^(nvidia|grid)_[a-z0-9]+-\d+[cq]?$")
VAPP_PROPERTY_TYPES = ("string", "ip", "password", "boolean", "number")
BOOT_TOKENS = ("disk", "network", "cdrom", "floppy")

# Basic vs. dynamic disks — the Windows Disk Management distinction.
# There is NO vSphere API property for it: a VMDK is the same either
# way; basic/dynamic is guest-OS configuration (Disk Management /
# diskpart on Windows). The lab therefore declares it per disk and
# stamps the layout into the VM's extraConfig (lab.diskLayout) so it
# is visible in the inventory; query/verify report it as declared,
# not as a live vCenter property. Dynamic disks are Windows-only.
DISK_TYPES = ("basic", "dynamic")
# Volume kinds for dynamic disks (Windows Disk Management):
#   simple   one disk, one volume
#   spanned  one volume across 2+ disks, filled in order
#   striped  one volume across 2+ disks, data striped (RAID 0)
#   mirrored one volume across 2+ disks, data duplicated (RAID 1)
VOLUME_KINDS = ("simple", "spanned", "striped", "mirrored")

# Per-VM attributes assigned randomly (seeded) or explicitly:
#   cbt          Changed Block Tracking (ConfigSpec.changeTrackingEnabled)
#   power_state  runtime power state (poweredOn / poweredOff / suspended)
#   fast_boot    boot delay 0 ms (boot immediately) vs. the standard delay
#   tools_status declared VMware Tools status (guest-reported; vcsim never
#                reports tools, so this is a declared-vs-live comparison)
POWER_STATES = ("poweredOn", "poweredOff", "suspended")
TOOLS_STATUSES = ("toolsNotInstalled", "toolsNotRunning", "toolsOk", "toolsOld")
RANDOM = "random"
DEFAULT_RANDOM_SEED = 42
# Boot delay in milliseconds: fast-boot VMs boot immediately, the rest
# get a standard 3 s delay (enough to hit a key for the boot menu).
FAST_BOOT_DELAY_MS = 0
NORMAL_BOOT_DELAY_MS = 3000

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
    # basic | dynamic — the Windows Disk Management distinction.
    # Declared-only: vSphere has no API property for it (see
    # DISK_TYPES). Dynamic disks are Windows-only (validated).
    disk_type: str = "basic"


@dataclass
class VolumeSpec:
    """A Windows dynamic-disk volume: one volume spanning 1+ disks.

    ``disks`` are 1-based disk indexes into the VM's disks list
    (matching the "Hard disk N" labels and the boot tokens disk:2).
    Every disk in a volume must be dynamic; spanned/striped/mirrored
    volumes need 2+ disks, a simple volume exactly one. Declared-only,
    like disk_type — on a real Windows VM the volume would be created
    in Disk Management / diskpart.
    """

    name: str
    kind: str  # simple | spanned | striped | mirrored
    disks: list[int] = field(default_factory=list)


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
    vendor: str | None = None  # "nvidia" | "amd"; inferred from vendor_id if omitted
    model: str | None = None  # e.g. "A100", "MI210", "H100"


@dataclass
class VAppPropertySpec:
    """One OVF environment property of a virtual appliance."""

    id: str  # e.g. "hostname" — unique within the VM
    label: str  # e.g. "Hostname"
    type: str = "string"  # string | ip | password | boolean | number
    value: str = ""
    user_configurable: bool = True
    description: str = ""


@dataclass
class VAppSpec:
    """OVF product info + properties for a virtual appliance VM.

    Stored on the VM as vAppConfig (VmConfigSpec on write, VmConfigInfo
    on read). vcsim accepts it on CreateVM and ReconfigVM and serves
    it back intact (verified against govmomi v0.56.2).
    """

    name: str  # product name, e.g. "OPNsense"
    vendor: str  # e.g. "Deciso"
    version: str  # e.g. "24.7"
    full_version: str = ""
    properties: list[VAppPropertySpec] = field(default_factory=list)


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
    # vApp container (from lab.vapps) the VM is created in instead of a
    # resource pool. A vApp is a ResourcePool subtype, so the VM is
    # created with pool=<vApp>.
    vapp_container: str = ""
    # OVF product info + properties (vAppConfig) for appliance VMs.
    vapp: VAppSpec | None = None
    # True for template VMs: created normally, then marked as a template
    # (MarkAsTemplate) as the last build step. Templates cannot be
    # powered on, so template=True requires power_on=False.
    template: bool = False
    power_on: bool = False
    # Per-VM attributes, each an explicit value or "random" (resolved
    # from lab.random_seed at build time — see resolve_random_assignments).
    # cbt: Changed Block Tracking on/off.
    cbt: bool | str = RANDOM
    # power_state: poweredOn | poweredOff | suspended | "random" | None.
    # None (the default) derives from power_on for backward compatibility;
    # an explicit power_state wins over power_on. Templates ignore it and
    # always stay poweredOff.
    power_state: str | None = None
    # fast_boot: boot delay 0 ms (boot immediately) vs. the standard
    # 3 s delay. There is no native vSphere VM "fast boot" setting —
    # bootDelay is the lab's representation of it (see README).
    fast_boot: bool | str = RANDOM
    # tools_status: the declared VMware Tools status. Guest-reported with
    # no setter API; vcsim always reports toolsNotInstalled, so this is
    # compared declared-vs-live rather than enforced (see README).
    tools_status: str = RANDOM
    cpu_hot_add: bool = False
    memory_hot_add: bool = False
    disks: list[DiskSpec] = field(default_factory=list)
    # Dynamic-disk volumes (Windows only, declared-only — see
    # VolumeSpec). A VM with no dynamic disks has no volumes.
    volumes: list[VolumeSpec] = field(default_factory=list)
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
class ResolvedAssignment:
    """A VM's per-VM attributes after "random" has been resolved.

    Produced by resolve_random_assignments() from the lab seed: the same
    vms.yaml + seed always yields the same assignment, so builds are
    reproducible and verify can check the live lab against it.
    """

    cbt: bool
    power_state: str  # poweredOn | poweredOff | suspended
    fast_boot: bool
    tools_status: str  # one of TOOLS_STATUSES

    def expected_boot_delay_ms(self) -> int:
        return FAST_BOOT_DELAY_MS if self.fast_boot else NORMAL_BOOT_DELAY_MS

    def to_dict(self) -> dict:
        return {
            "cbt": self.cbt,
            "power_state": self.power_state,
            "fast_boot": self.fast_boot,
            "tools_status": self.tools_status,
            "boot_delay_ms": self.expected_boot_delay_ms(),
        }


@dataclass
class LabConfig:
    datacenter: str
    cluster: str
    folders: list[str] = field(default_factory=list)
    resource_pools: list[str] = field(default_factory=list)
    # vApp containers created under the cluster's root resource pool
    # (ResourcePool.CreateVApp). VMs name one via `vapp_container:`.
    vapps: list[str] = field(default_factory=list)
    # Seed for the per-VM random attributes (cbt, power_state, fast_boot,
    # tools_status). Each VM draws from its own RNG seeded with
    # "<seed>:<vm-name>", so adding a VM never reshuffles the others.
    random_seed: int = DEFAULT_RANDOM_SEED
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
    # https by default (vcsim serves TLS); http when vcsim runs with
    # -tls=false — the container on Render, where TLS is terminated
    # at the edge and plain HTTP is forwarded to the container.
    scheme: str = "https"

    @classmethod
    def from_env(cls) -> "VCenterConfig":
        scheme = os.environ.get("VC_SCHEME", "https").lower()
        if scheme not in ("http", "https"):
            raise ConfigError(f"VC_SCHEME must be 'http' or 'https', got {scheme!r}")
        return cls(
            host=os.environ.get("VC_HOST", "127.0.0.1"),
            port=int(os.environ.get("VC_PORT", "8989")),
            user=os.environ.get("VC_USER", "user"),
            password=os.environ.get("VC_PASSWORD", "pass"),
            insecure=os.environ.get("VC_INSECURE", "1") not in ("0", "false", "False"),
            scheme=scheme,
        )

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.user}@{self.host}:{self.port}/sdk"


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

# One vCenter simulation per directory under environments/:
#   environments/<name>/env.yaml      name, description, vcsim port, flags
#   environments/<name>/vms.yaml      that environment's lab
#   environments/<name>/storage.yaml  that environment's storage model
# The original lab lives in environments/default/ (port 8989, so
# existing local workflows keep working); environments/dev/ is the
# small template for new environments.
DEFAULT_ENVIRONMENT = "default"
ENVIRONMENTS_DIR = "environments"

# vcsim inventory flags (env.yaml `vcsim:` map, key -> CLI flag). All
# optional — omitted keys take these defaults, which match the original
# lab's start flags. `-vm 0` is NOT here: the lab always builds the VMs
# itself, so every environment starts with zero stock VMs.
VCSIM_FLAG_DEFAULTS: dict[str, str | int] = {
    "api_version": "8.0.3",
    "datacenters": 1,
    "clusters": 1,
    "hosts": 3,
    "standalone_hosts": 1,
    "datastores": 3,
    "pods": 1,
    "portgroups": 2,
    "nsx": 1,
    "pools": 1,
}
_VCSIM_FLAG_ARGS = {
    "api_version": "-api-version",
    "datacenters": "-dc",
    "clusters": "-cluster",
    "hosts": "-host",
    "standalone_hosts": "-standalone-host",
    "datastores": "-ds",
    "pods": "-pod",
    "portgroups": "-pg",
    "nsx": "-nsx",
    "pools": "-pool",
}


@dataclass
class Environment:
    """One vCenter simulation: its identity, port and inventory shape.

    Credentials are NOT part of an environment — they come from the
    VCSIM_USERNAME / VCSIM_PASSWORD environment variables, shared
    across all environments (see ``vcenter_config``).
    """

    name: str
    description: str = ""
    port: int = 8989
    # vcsim inventory flags (env.yaml `vcsim:` map merged over
    # VCSIM_FLAG_DEFAULTS): api_version, datacenters, clusters, hosts,
    # standalone_hosts, datastores, pods, portgroups, nsx, pools.
    vcsim: dict = field(default_factory=dict)
    # environments/<name> and the project root (parent of environments/).
    directory: Path = Path(".")
    root: Path = Path(".")

    @property
    def lab_path(self) -> Path:
        return self.directory / "vms.yaml"

    @property
    def storage_path(self) -> Path:
        return self.directory / "storage.yaml"

    @property
    def env_yaml_path(self) -> Path:
        return self.directory / "env.yaml"

    @property
    def model_dir(self) -> Path:
        """Where this environment's saved vcsim model lives (model/<name>/)."""
        return self.root / "model" / self.name

    def vcsim_args(self) -> list[str]:
        """The vcsim CLI flags for this environment's inventory shape
        (without -l / -tls / -username / -password / -vm, which the
        caller supplies). Values are validated ints/strings, so the
        list is safe to pass to a subprocess or print for a shell."""
        args: list[str] = []
        for key, flag in _VCSIM_FLAG_ARGS.items():
            args.extend([flag, str(self.vcsim[key])])
        return args

    def vcenter_config(self, base: VCenterConfig | None = None) -> VCenterConfig:
        """Connection settings for this environment's vcsim: host,
        scheme, credentials and TLS behaviour from ``base`` (default:
        the VC_* environment variables), port from this environment.
        An explicitly set VC_PORT does NOT override here — the API
        connects to several environments at once, so each one must use
        its own port. (The single-environment CLI scripts apply the
        VC_PORT override themselves; see ``vcenter_config_for_env``.)"""
        base = base or VCenterConfig.from_env()
        return VCenterConfig(
            host=base.host,
            port=self.port,
            user=base.user,
            password=base.password,
            insecure=base.insecure,
            scheme=base.scheme,
        )


def vcenter_config_for_env(env: Environment) -> VCenterConfig:
    """Connection settings for the CLI scripts talking to one
    environment: as ``Environment.vcenter_config``, except an
    explicitly set VC_PORT wins over the environment's port. That is
    how the host scripts point at the container's proxied /sdk
    (``VC_PORT=8000 VC_SCHEME=http python scripts/query_lab.py``) —
    the container's vcsim instances are internal, so their own ports
    are not reachable from the host. Normally leave VC_PORT unset and
    the environment's port is used."""
    if "VC_PORT" in os.environ:
        base = VCenterConfig.from_env()
        return base  # from_env already read the explicit VC_PORT
    return env.vcenter_config()


def _load_env_yaml(path: Path) -> Environment:
    """Parse one env.yaml; ``path`` is environments/<name>/env.yaml."""
    if not path.exists():
        raise ConfigError(f"env.yaml not found: {path}")
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    name = _require(raw, "name", f"{path}")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}: name must be a non-empty string")
    if name != path.parent.name:
        raise ConfigError(
            f"{path}: name {name!r} does not match its directory {path.parent.name!r} "
            "(an environment's name is its directory name)"
        )
    port = _require(raw, "port", f"{path}")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{path}: port must be an integer between 1 and 65535, got {port!r}")
    vcsim_raw = raw.get("vcsim") or {}
    if not isinstance(vcsim_raw, dict):
        raise ConfigError(f"{path}: vcsim must be a mapping of inventory flags")
    unknown = sorted(set(vcsim_raw) - set(VCSIM_FLAG_DEFAULTS))
    if unknown:
        raise ConfigError(
            f"{path}: unknown vcsim flag(s) {unknown}; known flags are "
            f"{sorted(VCSIM_FLAG_DEFAULTS)}"
        )
    vcsim: dict[str, str | int] = dict(VCSIM_FLAG_DEFAULTS)
    for key, value in vcsim_raw.items():
        if key == "api_version":
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{path}: vcsim.api_version must be a non-empty string")
            vcsim[key] = value
        else:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(
                    f"{path}: vcsim.{key} must be a non-negative integer, got {value!r}"
                )
            vcsim[key] = value
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{path}: description must be a string")
    return Environment(
        name=name,
        description=description,
        port=port,
        vcsim=vcsim,
        directory=path.parent,
        root=path.parent.parent.parent,
    )


def list_environments(env_root: str | Path = ENVIRONMENTS_DIR) -> list[Environment]:
    """All environments under ``env_root`` (default ``environments/``),
    sorted by name. Raises ConfigError when two environments share a
    vcsim port (each vcsim needs its own port) or when an env.yaml is
    invalid. An empty list when the directory does not exist (callers
    that need at least one environment — the API, the entrypoint —
    fail fast on that themselves)."""
    root = Path(env_root)
    if not root.is_dir():
        return []
    envs = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "env.yaml").exists():
            envs.append(_load_env_yaml(child / "env.yaml"))
    seen_ports: dict[int, str] = {}
    for env in envs:
        if env.port in seen_ports:
            raise ConfigError(
                f"environments {seen_ports[env.port]!r} and {env.name!r} both use "
                f"vcsim port {env.port} — each environment needs its own port"
            )
        seen_ports[env.port] = env.name
    return envs


def load_environment(name: str, env_root: str | Path = ENVIRONMENTS_DIR) -> Environment:
    """The environment called ``name``. Raises ConfigError naming the
    available environments when it does not exist."""
    envs = list_environments(env_root)
    for env in envs:
        if env.name == name:
            return env
    available = ", ".join(e.name for e in envs) or "(none — no environments/ directory?)"
    raise ConfigError(f"unknown environment {name!r}; available: {available}")


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


def is_windows_guest(guest_id: str) -> bool:
    """True for Windows guest OS identifiers (windows11_64Guest,
    windows2019srv_64Guest, ...). Dynamic disks are a Windows-only
    concept, so this gates the disk_type validation."""
    return guest_id.startswith("windows")


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
        vendor = g.get("vendor")
        if vendor is not None and vendor not in GPU_VENDORS:
            raise ConfigError(f"{where}: gpu.vendor must be one of {sorted(GPU_VENDORS)}")
        model = g.get("model")
        if mode in ("passthrough", "dynamic"):
            _require(g, "vendor_id", f"{where} gpu")
            _require(g, "device_id", f"{where} gpu")
            vendor_id = parse_hex_id(g["vendor_id"], "vendor_id")
            device_id = parse_hex_id(g["device_id"], "device_id")
            if vendor is not None and vendor_id != GPU_VENDORS[vendor]:
                raise ConfigError(
                    f"{where}: gpu.vendor {vendor!r} implies vendor_id "
                    f"{GPU_VENDORS[vendor]:#06x}, got {g['vendor_id']!r}"
                )
            if vendor is None:
                # Infer the vendor from the PCI id when it is a known one.
                vendor = next(
                    (v for v, vid in GPU_VENDORS.items() if vid == vendor_id), None
                )
            if model is not None and vendor is not None:
                known = KNOWN_GPU_MODELS.get((vendor, model))
                if known is not None and known != device_id:
                    raise ConfigError(
                        f"{where}: gpu.model {model!r} implies device_id "
                        f"{known:#06x}, got {g['device_id']!r}"
                    )
                if known is None:
                    warnings.append(
                        f"{where}: gpu.model {model!r} is not in the known list for "
                        f"vendor {vendor!r}; the PCI ids are used as-is."
                    )
        if mode == "vgpu":
            _require(g, "profile", f"{where} gpu")
            if vendor == "amd":
                raise ConfigError(
                    f"{where}: AMD has no vGPU profiles — AMD MxGPU is SR-IOV "
                    "virtual functions presented as PCI passthrough devices, "
                    "so use gpu.mode passthrough or dynamic with AMD PCI ids"
                )
            if not _VGPU_PROFILE_RE.match(g["profile"]):
                raise ConfigError(
                    f"{where}: gpu.profile {g['profile']!r} does not look like an "
                    "NVIDIA vGPU profile (e.g. 'nvidia_a100-40c', 'grid_t4-8q')"
                )
        gpu = GpuSpec(
            mode=mode,
            vendor_id=g.get("vendor_id"),
            device_id=g.get("device_id"),
            profile=g.get("profile"),
            label=g.get("label", ""),
            mmio_size_gb=int(g.get("mmio_size_gb", 32)),
            vendor=vendor,
            model=model,
        )

    vapp = None
    if raw.get("vapp_config"):
        a = raw["vapp_config"]
        props = []
        seen_ids = set()
        for i, p in enumerate(a.get("properties") or []):
            pid = _require(p, "id", f"{where} vapp_config property {i}")
            if pid in seen_ids:
                raise ConfigError(f"{where}: duplicate vapp_config property id {pid!r}")
            seen_ids.add(pid)
            ptype = p.get("type", "string")
            if ptype not in VAPP_PROPERTY_TYPES:
                raise ConfigError(
                    f"{where}: vapp_config property {pid!r} type must be one of "
                    f"{VAPP_PROPERTY_TYPES}"
                )
            props.append(
                VAppPropertySpec(
                    id=pid,
                    label=_require(p, "label", f"{where} vapp_config property {pid!r}"),
                    type=ptype,
                    value=str(p.get("value", "")),
                    user_configurable=bool(p.get("user_configurable", True)),
                    description=str(p.get("description", "")),
                )
            )
        vapp = VAppSpec(
            name=_require(a, "name", f"{where} vapp_config"),
            vendor=_require(a, "vendor", f"{where} vapp_config"),
            version=_require(a, "version", f"{where} vapp_config"),
            full_version=str(a.get("full_version", "")),
            properties=props,
        )

    disks = []
    for i, d in enumerate(raw.get("disks") or []):
        if d.get("controller", "pvscsi") not in CONTROLLER_TYPES:
            raise ConfigError(f"{where}: disk {i} controller must be one of {CONTROLLER_TYPES}")
        disk_type = d.get("disk_type", "basic")
        if disk_type not in DISK_TYPES:
            raise ConfigError(f"{where}: disk {i} disk_type must be one of {DISK_TYPES}")
        if disk_type == "dynamic" and not is_windows_guest(guest_id):
            raise ConfigError(
                f"{where}: disk {i} is dynamic, but dynamic disks are a "
                f"Windows-only concept (guest {guest_id!r} is not Windows)"
            )
        disks.append(
            DiskSpec(
                size_gb=int(_require(d, "size_gb", f"{where} disk {i}")),
                controller=d.get("controller", "pvscsi"),
                datastore=d.get("datastore", "LocalDS_0"),
                thin=bool(d.get("thin", True)),
                eager=bool(d.get("eager", False)),
                disk_mode=d.get("disk_mode", "persistent"),
                disk_type=disk_type,
            )
        )
    if not disks:
        raise ConfigError(f"{where}: at least one disk is required")

    # Dynamic-disk volumes: each volume names its disks by 1-based index
    # into the disks list; every disk in a volume must be dynamic.
    volumes = []
    seen_volumes = set()
    claimed_disks: dict[int, str] = {}
    for i, vol in enumerate(raw.get("volumes") or []):
        vwhere = f"{where} volume {i}"
        vname = _require(vol, "name", vwhere)
        if vname in seen_volumes:
            raise ConfigError(f"{where}: duplicate volume name {vname!r}")
        seen_volumes.add(vname)
        kind = _require(vol, "kind", vwhere)
        if kind not in VOLUME_KINDS:
            raise ConfigError(f"{vwhere}: kind must be one of {VOLUME_KINDS}")
        members = vol.get("disks") or []
        if not members:
            raise ConfigError(f"{vwhere}: at least one disk is required")
        if len(members) != len(set(members)):
            raise ConfigError(f"{vwhere}: duplicate disk index in {members}")
        if kind == "simple" and len(members) != 1:
            raise ConfigError(f"{vwhere}: a simple volume spans exactly one disk")
        if kind in ("spanned", "striped", "mirrored") and len(members) < 2:
            raise ConfigError(f"{vwhere}: a {kind} volume needs 2+ disks, got {len(members)}")
        for idx in members:
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 1 or idx > len(disks):
                raise ConfigError(
                    f"{vwhere}: disk index {idx!r} is out of range "
                    f"(VM has {len(disks)} disk(s), indexes are 1-based)"
                )
            if disks[idx - 1].disk_type != "dynamic":
                raise ConfigError(
                    f"{vwhere}: disk {idx} is not dynamic — every disk in a "
                    "volume must have disk_type: dynamic"
                )
            if idx in claimed_disks:
                raise ConfigError(
                    f"{vwhere}: disk {idx} is already in volume {claimed_disks[idx]!r}"
                )
            claimed_disks[idx] = vname
        volumes.append(VolumeSpec(name=vname, kind=kind, disks=list(members)))

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
    template = bool(raw.get("template", False))
    if template and raw.get("power_on"):
        raise ConfigError(f"{where}: template VMs cannot be powered on (power_on must be false)")

    # Per-VM attributes: explicit value or "random" (resolved from the
    # lab seed at build time). Templates are exempt from power-state
    # assignment — they always stay poweredOff.
    cbt = raw.get("cbt", RANDOM)
    if cbt is not True and cbt is not False and cbt != RANDOM:
        raise ConfigError(f"{where}: cbt must be true, false or 'random'")
    power_state = raw.get("power_state")
    if power_state is not None and power_state not in POWER_STATES and power_state != RANDOM:
        raise ConfigError(
            f"{where}: power_state must be one of {list(POWER_STATES)} or 'random'"
        )
    if template and power_state is not None:
        raise ConfigError(
            f"{where}: template VMs cannot take power_state {power_state!r} "
            "(templates always stay poweredOff)"
        )
    fast_boot = raw.get("fast_boot", RANDOM)
    if fast_boot is not True and fast_boot is not False and fast_boot != RANDOM:
        raise ConfigError(f"{where}: fast_boot must be true, false or 'random'")
    tools_status = raw.get("tools_status", RANDOM)
    if tools_status not in TOOLS_STATUSES and tools_status != RANDOM:
        raise ConfigError(
            f"{where}: tools_status must be one of {list(TOOLS_STATUSES)} or 'random'"
        )

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
        vapp_container=raw.get("vapp_container", ""),
        vapp=vapp,
        template=template,
        power_on=bool(raw.get("power_on", False)),
        cbt=cbt,
        power_state=power_state,
        fast_boot=fast_boot,
        tools_status=tools_status,
        cpu_hot_add=bool(raw.get("cpu_hot_add", False)),
        memory_hot_add=bool(raw.get("memory_hot_add", False)),
        disks=disks,
        volumes=volumes,
        nics=nics,
        cdrom=cdrom,
        boot_order=list(boot_order),
        extra_config={str(k): str(v) for k, v in (raw.get("extra_config") or {}).items()},
    )


def load_lab(path: str | Path = "environments/default/vms.yaml") -> LabConfig:
    """Load and validate the lab matrix. Raises ConfigError on bad input.
    The default is the `default` environment's lab; use
    ``load_environment(name).lab_path`` for other environments."""
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
        if vm.vapp_container and vm.vapp_container not in (lab.get("vapps") or []):
            raise ConfigError(
                f"VM {vm.name!r}: vapp_container {vm.vapp_container!r} is not in lab.vapps:"
            )

    seed = lab.get("random_seed", DEFAULT_RANDOM_SEED)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ConfigError(f"lab.random_seed must be an integer, got {seed!r}")

    return LabConfig(
        datacenter=_require(lab, "datacenter", "lab:"),
        cluster=_require(lab, "cluster", "lab:"),
        folders=list(lab.get("folders") or []),
        resource_pools=list(lab.get("resource_pools") or []),
        vapps=list(lab.get("vapps") or []),
        random_seed=seed,
        networks=networks,
        vms=vms,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Seeded random assignment
# ---------------------------------------------------------------------------


def resolve_random_assignments(lab: LabConfig) -> dict[str, ResolvedAssignment]:
    """Resolve every VM's per-VM attributes to concrete values.

    Explicit values pass through unchanged. Fields set to "random" draw
    from a per-VM RNG seeded with "<lab seed>:<vm name>", so the
    assignment is deterministic for a given vms.yaml + seed, and adding
    or removing a VM never reshuffles the others. Draws happen in a
    fixed order (cbt, power_state, fast_boot, tools_status) and only for
    fields actually set to "random".

    Templates are exempt from power-state assignment: they always resolve
    to poweredOff and consume no power-state draw. A VM with no
    power_state derives it from the legacy power_on flag.
    """
    resolved = {}
    for vm in lab.vms:
        rng = random.Random(f"{lab.random_seed}:{vm.name}")
        cbt = rng.choice([True, False]) if vm.cbt == RANDOM else bool(vm.cbt)
        if vm.template:
            power_state = "poweredOff"
        elif vm.power_state == RANDOM:
            power_state = rng.choice(POWER_STATES)
        elif vm.power_state is not None:
            power_state = vm.power_state
        else:
            power_state = "poweredOn" if vm.power_on else "poweredOff"
        fast_boot = rng.choice([True, False]) if vm.fast_boot == RANDOM else bool(vm.fast_boot)
        tools_status = (
            rng.choice(TOOLS_STATUSES) if vm.tools_status == RANDOM else vm.tools_status
        )
        resolved[vm.name] = ResolvedAssignment(
            cbt=cbt,
            power_state=power_state,
            fast_boot=fast_boot,
            tools_status=tools_status,
        )
    return resolved


def load_declared_assignments(path: str | Path = "environments/default/vms.yaml") -> dict | None:
    """The lab's per-VM declared values (name -> dict), or None when the
    lab file cannot be loaded — callers then show live values only.

    Each entry carries the seed-resolved attributes (cbt, power_state,
    fast_boot, tools_status, boot_delay_ms) plus the declared disk
    layout (basic/dynamic types and volumes). Shared by the query tool
    and the inventory API so both show the same declared-vs-live view.
    """
    try:
        lab = load_lab(path)
    except (ConfigError, OSError):
        return None
    assigned = resolve_random_assignments(lab)
    declared = {}
    for vm in lab.vms:
        entry = assigned[vm.name].to_dict()
        entry["disk_layout"] = disk_layout_summary(vm)
        declared[vm.name] = entry
    return declared


# ---------------------------------------------------------------------------
# Disk layout (basic/dynamic disks, declared-only)
# ---------------------------------------------------------------------------

# extraConfig key carrying the declared disk layout on VMs with dynamic
# disks, so the layout is visible in the inventory (vcsim serves
# extraConfig back intact). Basic-only VMs carry no key: basic is the
# default and needs no marker.
DISK_LAYOUT_EXTRA_CONFIG_KEY = "lab.diskLayout"


def disk_layout_summary(vm: VmSpec) -> dict:
    """The VM's declared disk layout as plain JSON-able data.

    Per disk: its 1-based index, basic/dynamic type, and the volume it
    belongs to (if any) with that volume's kind. Plus the volume list
    itself and a has_dynamic flag. Used by the query tool to show the
    declared layout alongside the live disks.
    """
    volume_of: dict[int, VolumeSpec] = {}
    for volume in vm.volumes:
        for idx in volume.disks:
            volume_of[idx] = volume
    disks = []
    for i, disk in enumerate(vm.disks, start=1):
        volume = volume_of.get(i)
        disks.append(
            {
                "index": i,
                "disk_type": disk.disk_type,
                "volume": volume.name if volume else None,
                "volume_kind": volume.kind if volume else None,
            }
        )
    return {
        "disks": disks,
        "volumes": [
            {"name": v.name, "kind": v.kind, "disks": list(v.disks)} for v in vm.volumes
        ],
        "has_dynamic": any(d.disk_type == "dynamic" for d in vm.disks),
    }


def disk_layout_extra_config(vm: VmSpec) -> str | None:
    """The compact disk-layout string stamped into extraConfig, or None
    for VMs with no dynamic disks (basic is the default; no marker
    needed). One entry per disk, in disk order, separated by "; ":

        basic; dynamic:SQLData:mirrored; dynamic:SQLData:mirrored

    A dynamic disk in no volume is just "dynamic". The builder writes
    this under DISK_LAYOUT_EXTRA_CONFIG_KEY at create time; verify
    checks the live value against it.
    """
    summary = disk_layout_summary(vm)
    if not summary["has_dynamic"]:
        return None
    parts = []
    for entry in summary["disks"]:
        if entry["disk_type"] == "basic":
            parts.append("basic")
        elif entry["volume"] is not None:
            parts.append(f"dynamic:{entry['volume']}:{entry['volume_kind']}")
        else:
            parts.append("dynamic")
    return "; ".join(parts)
