"""Host storage inventory: declarative model + live reconciliation.

Two halves, kept deliberately separate:

* The *declared* model (``storage.yaml``) describes the shared storage the
  lab is meant to have — storage arrays, their LUNs, fabric HBAs per host,
  and the mapping from LUNs to datastores. vSphere has no API to create
  HBAs or LUNs (they are discovered hardware), and vcsim models only
  local storage, so this half lives in YAML and is validated offline.
* The *live* half reads what vcsim actually serves per host — HBAs,
  SCSI LUNs, SCSI topology, multipath info, software-iSCSI state, and
  datastore mounts — through the leaf property retrieval in
  ``vcenter.py`` (whole ``HostSystem.config`` is not served; see README
  "vcsim vs real vCenter").

``reconcile`` joins the two: which declared LUNs each host should see,
which live devices it actually has, and which datastore mounts back to
which array LUN — plus plain notes wherever the live inventory cannot
confirm the model (vcsim datastores are type OTHER with no VMFS
extents, so datastore-to-LUN backing is declared, never live-verified).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from pyVmomi import vim

from .vcenter import list_objs, retrieve_props

# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

_NAA_RE = re.compile(r"^naa\.[0-9a-fA-F]{16}$")
_WWPN_RE = re.compile(r"^([0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}$")
_IQN_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[A-Za-z0-9.-]+:.+$")
_HBA_DEVICE_RE = re.compile(r"^vmhba\d+$")
_PORTAL_RE = re.compile(r"^[A-Za-z0-9.-]+:\d+$")

PROTOCOLS = ("fc", "iscsi")
HBA_TYPES = ("fc", "iscsi")


class StorageError(ValueError):
    """Raised when ``storage.yaml`` is invalid."""


# ---------------------------------------------------------------------------
# Declarative model
# ---------------------------------------------------------------------------


@dataclass
class ArrayPort:
    name: str
    wwpn: str | None = None  # Fibre Channel target port
    portal: str | None = None  # iSCSI target portal, "ip:port"


@dataclass
class ArrayLun:
    id: str
    naa: str  # canonical name, e.g. naa.624a937001000001
    capacity_gb: int
    datastore: str  # vcsim datastore this LUN backs (LocalDS_0..2)
    label: str = ""  # volume name on the array


@dataclass
class StorageArray:
    id: str
    vendor: str
    model: str
    protocol: str  # fc | iscsi
    controllers: list[str] = field(default_factory=list)
    ports: list[ArrayPort] = field(default_factory=list)
    luns: list[ArrayLun] = field(default_factory=list)
    iqn: str | None = None  # iSCSI target IQN (iscsi arrays only)


@dataclass
class DeclaredHba:
    device: str  # vmhbaN
    type: str  # fc | iscsi
    model: str = ""
    driver: str = ""
    wwpn: str | None = None  # fc only
    iqn: str | None = None  # iscsi only
    speed_gbps: int | None = None


@dataclass
class HostStorage:
    name: str
    hbas: list[DeclaredHba] = field(default_factory=list)

    def has_protocol(self, protocol: str) -> bool:
        return any(h.type == protocol for h in self.hbas)


@dataclass
class StorageModel:
    arrays: list[StorageArray] = field(default_factory=list)
    hosts: dict[str, HostStorage] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def all_luns(self) -> list[tuple[StorageArray, ArrayLun]]:
        return [(a, lun) for a in self.arrays for lun in a.luns]

    def array_for_lun(self, lun_id: str) -> StorageArray | None:
        for array, lun in self.all_luns():
            if lun.id == lun_id:
                return array
        return None

    def lun_for_datastore(self, datastore: str) -> ArrayLun | None:
        for _, lun in self.all_luns():
            if lun.datastore == datastore:
                return lun
        return None

    def array_for_datastore(self, datastore: str) -> StorageArray | None:
        lun = self.lun_for_datastore(datastore)
        return self.array_for_lun(lun.id) if lun else None

    def luns_visible_to(self, host_name: str) -> list[tuple[StorageArray, ArrayLun]]:
        """Array LUNs a host should see: one per array whose protocol the
        host has an HBA for (the zoning model — FC hosts see FC LUNs,
        iSCSI hosts see iSCSI LUNs)."""
        host = self.hosts.get(host_name)
        if host is None:
            return []
        return [(a, lun) for a, lun in self.all_luns() if host.has_protocol(a.protocol)]


def _require(mapping: dict, key: str, where: str):
    if key not in mapping or mapping[key] is None:
        raise StorageError(f"missing required key {key!r} in {where}")
    return mapping[key]


def _check(regex, value: str, what: str) -> None:
    if not regex.match(value):
        raise StorageError(f"invalid {what} {value!r}")


def load_storage(path: str | Path = "environments/default/storage.yaml") -> StorageModel:
    """Load and validate the storage model. Raises StorageError on bad input."""
    path = Path(path)
    if not path.exists():
        raise StorageError(f"storage file not found: {path} (run from the project root)")
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    warnings: list[str] = []
    arrays: list[StorageArray] = []
    seen_array_ids: set[str] = set()
    seen_lun_ids: set[str] = set()
    seen_naas: set[str] = set()
    seen_datastores: dict[str, str] = {}  # datastore -> lun id

    for a in raw.get("arrays") or []:
        where = f"array {a.get('id', '?')!r}"
        array_id = _require(a, "id", where)
        if array_id in seen_array_ids:
            raise StorageError(f"duplicate array id {array_id!r}")
        seen_array_ids.add(array_id)
        protocol = _require(a, "protocol", where)
        if protocol not in PROTOCOLS:
            raise StorageError(f"{where}: protocol must be one of {PROTOCOLS}")
        ports = []
        for i, p in enumerate(a.get("ports") or []):
            port_where = f"{where} port {i}"
            name = _require(p, "name", port_where)
            if protocol == "fc":
                wwpn = _require(p, "wwpn", port_where)
                _check(_WWPN_RE, wwpn, "WWPN")
                ports.append(ArrayPort(name=name, wwpn=wwpn))
            else:
                portal = _require(p, "portal", port_where)
                _check(_PORTAL_RE, portal, "iSCSI portal (ip:port)")
                ports.append(ArrayPort(name=name, portal=portal))
        if protocol == "iscsi":
            iqn = _require(a, "iqn", where)
            _check(_IQN_RE, iqn, "iSCSI target IQN")
        else:
            iqn = a.get("iqn")
            if iqn is not None:
                _check(_IQN_RE, iqn, "iSCSI target IQN")
        luns = []
        for l in a.get("luns") or []:
            lun_where = f"{where} LUN {l.get('id', '?')!r}"
            lun_id = _require(l, "id", lun_where)
            if lun_id in seen_lun_ids:
                raise StorageError(f"duplicate LUN id {lun_id!r}")
            seen_lun_ids.add(lun_id)
            naa = _require(l, "naa", lun_where)
            _check(_NAA_RE, naa, "NAA name (naa. + 16 hex digits)")
            if naa.lower() in seen_naas:
                raise StorageError(f"duplicate NAA name {naa!r}")
            seen_naas.add(naa.lower())
            capacity = int(_require(l, "capacity_gb", lun_where))
            if capacity <= 0:
                raise StorageError(f"{lun_where}: capacity_gb must be positive")
            datastore = _require(l, "datastore", lun_where)
            if datastore in seen_datastores:
                raise StorageError(
                    f"{lun_where}: datastore {datastore!r} is already backed by "
                    f"LUN {seen_datastores[datastore]!r}; one LUN per datastore"
                )
            seen_datastores[datastore] = lun_id
            luns.append(
                ArrayLun(
                    id=lun_id,
                    naa=naa,
                    capacity_gb=capacity,
                    datastore=datastore,
                    label=l.get("label", ""),
                )
            )
        arrays.append(
            StorageArray(
                id=array_id,
                vendor=_require(a, "vendor", where),
                model=_require(a, "model", where),
                protocol=protocol,
                controllers=list(a.get("controllers") or []),
                ports=ports,
                luns=luns,
                iqn=iqn,
            )
        )
    if not arrays:
        raise StorageError("no arrays defined in storage.yaml")

    hosts: dict[str, HostStorage] = {}
    seen_wwpns: set[str] = set()
    seen_iqns: set[str] = set()
    for host_name, h in (raw.get("hosts") or {}).items():
        where = f"host {host_name!r}"
        hbas = []
        seen_devices: set[str] = set()
        for hb in h.get("hbas") or []:
            hb_where = f"{where} HBA {hb.get('device', '?')!r}"
            device = _require(hb, "device", hb_where)
            _check(_HBA_DEVICE_RE, device, "HBA device (vmhbaN)")
            if device in seen_devices:
                raise StorageError(f"{where}: duplicate HBA device {device!r}")
            seen_devices.add(device)
            hba_type = _require(hb, "type", hb_where)
            if hba_type not in HBA_TYPES:
                raise StorageError(f"{hb_where}: type must be one of {HBA_TYPES}")
            wwpn = hb.get("wwpn")
            iqn = hb.get("iqn")
            if hba_type == "fc":
                if not wwpn:
                    raise StorageError(f"{hb_where}: FC HBA requires a wwpn")
                _check(_WWPN_RE, wwpn, "WWPN")
                if wwpn.lower() in seen_wwpns:
                    raise StorageError(f"duplicate WWPN {wwpn!r}")
                seen_wwpns.add(wwpn.lower())
            else:
                if not iqn:
                    raise StorageError(f"{hb_where}: iSCSI HBA requires an iqn")
                _check(_IQN_RE, iqn, "iSCSI initiator IQN")
                if iqn in seen_iqns:
                    raise StorageError(f"duplicate initiator IQN {iqn!r}")
                seen_iqns.add(iqn)
            hbas.append(
                DeclaredHba(
                    device=device,
                    type=hba_type,
                    model=hb.get("model", ""),
                    driver=hb.get("driver", ""),
                    wwpn=wwpn,
                    iqn=iqn,
                    speed_gbps=hb.get("speed_gbps"),
                )
            )
        hosts[host_name] = HostStorage(name=host_name, hbas=hbas)

    # Cross-checks: every array LUN should be visible to at least one host,
    # and every host should see at least one LUN (warnings, not errors —
    # a host with local-only storage is a valid lab shape).
    for array, lun in [(a, l) for a in arrays for l in a.luns]:
        if not any(h.has_protocol(array.protocol) for h in hosts.values()):
            warnings.append(
                f"LUN {lun.id} (array {array.id}, {array.protocol}) is not visible "
                "to any host: no host has an HBA of that protocol"
            )
    for host in hosts.values():
        if not host.hbas:
            warnings.append(f"host {host.name!r} has no declared HBAs (local storage only)")

    return StorageModel(arrays=arrays, hosts=hosts, warnings=warnings)


def model_to_dict(model: StorageModel) -> dict:
    return {
        "arrays": [asdict(a) for a in model.arrays],
        "hosts": {name: asdict(h) for name, h in sorted(model.hosts.items())},
        "warnings": list(model.warnings),
    }


# ---------------------------------------------------------------------------
# Live reads (pyVmomi)
# ---------------------------------------------------------------------------


def _format_wwpn(wwpn: int | None) -> str | None:
    """A 64-bit WWPN as colon-separated octets, e.g. 10:00:00:10:9b:01:02:11."""
    if wwpn is None:
        return None
    hexed = f"{wwpn:016x}"
    return ":".join(hexed[i : i + 2] for i in range(0, 16, 2))


def _hba_info(hba) -> dict:
    """One live HBA as plain data. FC/iSCSI fields are read with getattr
    so this works on real vCenter too (vcsim only serves local
    ParallelScsiHba/BlockHba adapters)."""
    # pyVmomi class names carry the dotted WSDL path (vim.host.BlockHba);
    # keep the short name so the table, JSON and the FC/iSCSI checks below
    # all see the same value.
    info = {
        "device": hba.device,
        "type": type(hba).__name__.split(".")[-1],
        "model": getattr(hba, "model", None),
        "driver": getattr(hba, "driver", None),
        "pci": getattr(hba, "pci", None),
        "bus": getattr(hba, "bus", None),
        "status": str(getattr(hba, "status", None)),
    }
    if isinstance(hba, vim.host.FibreChannelHba):
        info["wwpn"] = _format_wwpn(getattr(hba, "portWorldWideName", None))
        info["node_wwpn"] = _format_wwpn(getattr(hba, "nodeWorldWideName", None))
        info["speed_gbps"] = getattr(hba, "speed", None)
    if isinstance(hba, vim.host.InternetScsiHba):
        info["iqn"] = getattr(hba, "iScsiName", None)
        info["discovery"] = getattr(hba, "discoveryProperties", None) is not None
    return info


def _lun_info(lun) -> dict:
    """One live SCSI LUN as plain data."""
    info = {
        "canonical_name": lun.canonicalName,
        "device_name": lun.deviceName,
        "type": lun.lunType,
        "vendor": (lun.vendor or "").strip(),
        "model": (lun.model or "").strip(),
        "revision": (lun.revision or "").strip(),
        "uuid": lun.uuid,
        "display_name": lun.displayName,
        "descriptors": [d.id for d in (lun.descriptor or [])],
        "operational_state": list(lun.operationalState or []),
        "capacity_gb": None,
        "ssd": None,
        "local_disk": None,
    }
    if isinstance(lun, vim.host.ScsiDisk):
        cap = lun.capacity
        info["capacity_gb"] = round(cap.block * cap.blockSize / 1024**3, 1)
        info["ssd"] = bool(lun.ssd)
        info["local_disk"] = bool(lun.localDisk)
    return info


def _multipath_info(mp) -> list[dict]:
    out = []
    for lun in mp.lun or []:
        out.append(
            {
                "lun_uuid": lun.id,
                "policy": lun.policy.policy if lun.policy else None,
                "paths": [
                    {"name": p.name, "state": str(p.state), "adapter": p.adapter}
                    for p in (lun.path or [])
                ],
            }
        )
    return out


def _topology_info(topo) -> list[dict]:
    out = []
    for adapter in topo.adapter or []:
        targets = []
        for target in adapter.target or []:
            targets.append(
                {
                    "transport": (
                        type(target.transport).__name__.split(".")[-1]
                        if target.transport
                        else None
                    ),
                    "luns": [{"number": l.lun, "key": l.scsiLun} for l in (target.lun or [])],
                }
            )
        out.append({"adapter_key": adapter.adapter, "targets": targets})
    return out


def live_host_storage(si, host: vim.HostSystem) -> dict:
    """What vcsim serves for one host's storage, as plain JSON-able data.

    Reads ``HostStorageSystem.storageDeviceInfo`` through leaf retrieves.
    Falls back to the ``config.storageDevice.*`` leaves on the host when
    the storage system is unavailable (both paths serve the same HBAs
    and LUNs against vcsim; the storage-system path additionally carries
    topology and multipath info).
    """
    result: dict = {
        "name": host.name,
        "source": "live",
        "software_iscsi_enabled": None,
        "hbas": [],
        "luns": [],
        "multipath": [],
        "topology": [],
        "error": None,
    }
    storage_system = None
    try:
        storage_system = host.configManager.storageSystem
    except Exception:
        storage_system = None
    if storage_system is not None:
        try:
            props = retrieve_props(
                si,
                storage_system,
                vim.HostStorageSystem,
                [
                    "storageDeviceInfo.hostBusAdapter",
                    "storageDeviceInfo.scsiLun",
                    "storageDeviceInfo.scsiTopology",
                    "storageDeviceInfo.multipathInfo",
                    "storageDeviceInfo.softwareInternetScsiEnabled",
                ],
            )
            result["software_iscsi_enabled"] = props.get(
                "storageDeviceInfo.softwareInternetScsiEnabled"
            )
            result["hbas"] = [
                _hba_info(h) for h in (props.get("storageDeviceInfo.hostBusAdapter") or [])
            ]
            result["luns"] = [
                _lun_info(l) for l in (props.get("storageDeviceInfo.scsiLun") or [])
            ]
            topo = props.get("storageDeviceInfo.scsiTopology")
            if topo is not None:
                result["topology"] = _topology_info(topo)
            mp = props.get("storageDeviceInfo.multipathInfo")
            if mp is not None:
                result["multipath"] = _multipath_info(mp)
            return result
        except Exception as exc:  # noqa: BLE001 — fall back to config leaves
            result["error"] = f"storageSystem read failed ({exc}); trying config leaves"
    try:
        props = retrieve_props(
            si,
            host,
            vim.HostSystem,
            ["config.storageDevice.hostBusAdapter", "config.storageDevice.scsiLun"],
        )
        result["hbas"] = [
            _hba_info(h) for h in (props.get("config.storageDevice.hostBusAdapter") or [])
        ]
        result["luns"] = [_lun_info(l) for l in (props.get("config.storageDevice.scsiLun") or [])]
    except Exception as exc:  # noqa: BLE001 — report, don't crash the query
        result["error"] = f"host storage not served: {exc}"
    return result


def live_datastore_mounts(si) -> list[dict]:
    """Datastores with their live mounts and (for VMFS) backing extents.

    vcsim datastores are type OTHER with ``LocalDatastoreInfo`` — no VMFS
    extents, so ``extents`` is None and ``info_type`` says so. Against a
    real vCenter the same code returns the VMFS extent disk names, which
    is how a datastore's backing LUN is verified live.
    """
    content = si.RetrieveContent()
    host_names = {h._moId: h.name for h in list_objs(content, vim.HostSystem)}
    out = []
    for ds in list_objs(content, vim.Datastore):
        props = retrieve_props(
            si,
            ds,
            vim.Datastore,
            [
                "host",
                "summary.type",
                "summary.capacity",
                "summary.freeSpace",
                "summary.accessible",
                "info.url",
            ],
        )
        mounts = []
        for mount in props.get("host") or []:
            mi = mount.mountInfo
            mounts.append(
                {
                    "host": host_names.get(mount.key._moId, mount.key._moId),
                    "access_mode": str(mi.accessMode),
                    "mounted": bool(mi.mounted),
                    "accessible": bool(mi.accessible),
                }
            )
        # Whole-object ds.info carries the VMFS extents on real vCenter;
        # vcsim serves LocalDatastoreInfo here (no extents). Guarded:
        # whole-object reads are the ones vcsim sometimes breaks.
        info_type, extents = None, None
        try:
            info = ds.info
            info_type = type(info).__name__.split(".")[-1]
            if isinstance(info, vim.host.VmfsDatastoreInfo):
                extents = [e.diskName for e in (info.vmfs.extent or [])]
        except Exception:
            info_type, extents = None, None
        capacity = props.get("summary.capacity") or 0
        free = props.get("summary.freeSpace") or 0
        out.append(
            {
                "name": ds.name,
                "type": props.get("summary.type"),
                "info_type": info_type,
                "url": props.get("info.url"),
                "capacity_gb": round(capacity / 1024**3, 1),
                "free_gb": round(free / 1024**3, 1),
                "accessible": props.get("summary.accessible"),
                "mounts": mounts,
                "extents": extents,
            }
        )
    out.sort(key=lambda d: d["name"])
    return out


def live_storage(si) -> dict:
    """All live host storage + datastore mounts, in one structure."""
    content = si.RetrieveContent()
    hosts = sorted(list_objs(content, vim.HostSystem), key=lambda h: h.name)
    return {
        "hosts": [live_host_storage(si, h) for h in hosts],
        "datastores": live_datastore_mounts(si),
    }


# ---------------------------------------------------------------------------
# Reconciliation (pure: offline-testable)
# ---------------------------------------------------------------------------


def reconcile(model: StorageModel, live: dict) -> dict:
    """Join the declared model with the live inventory.

    Per host: declared HBAs vs live HBAs, declared-visible array LUNs vs
    live LUNs, and datastore mounts with their declared backing LUN.
    Notes call out every place the live inventory cannot confirm the
    model — vcsim has no fabric HBAs, no array LUNs and no VMFS extents,
    so those parts of the model are declared-only by design.
    """
    live_hosts = {h["name"]: h for h in live.get("hosts", [])}
    live_ds = {d["name"]: d for d in live.get("datastores", [])}
    notes: list[str] = []

    hosts_out = []
    for name in sorted(set(model.hosts) | set(live_hosts)):
        declared = model.hosts.get(name)
        lv = live_hosts.get(name)
        declared_hbas = [asdict(h) for h in declared.hbas] if declared else []
        live_hbas = lv["hbas"] if lv else []
        live_hba_devices = {h["device"] for h in live_hbas}
        # Copy: the notes below annotate the output, not the input.
        live_luns = [dict(l) for l in (lv["luns"] if lv else [])]
        host_notes: list[str] = []
        if declared is None:
            host_notes.append("host not in storage.yaml — live storage only, no declared fabric")
        if lv is None:
            host_notes.append("host not found live — declared storage only")
        for h in declared_hbas:
            if h["device"] not in live_hba_devices:
                host_notes.append(
                    f"declared {h['device']} ({h['type']}) not present live — "
                    "vcsim does not model fabric HBAs"
                )
        for lun in live_luns:
            if lun["canonical_name"].startswith("mpx.") or lun.get("local_disk"):
                lun["array_lun"] = False
                lun["note"] = "local device, not an array LUN"
            else:
                lun["array_lun"] = None  # unknown: no NAA match to check against
        visible = []
        if declared is not None:
            for array, lun in model.luns_visible_to(name):
                visible.append(
                    {
                        "lun": lun.id,
                        "label": lun.label,
                        "naa": lun.naa,
                        "capacity_gb": lun.capacity_gb,
                        "datastore": lun.datastore,
                        "array": array.id,
                        "protocol": array.protocol,
                        "visible_live": any(
                            l["canonical_name"] == lun.naa for l in live_luns
                        ),
                    }
                )
        mounts = []
        for ds_name in sorted(live_ds):
            ds = live_ds[ds_name]
            for mount in ds["mounts"]:
                if mount["host"] != name:
                    continue
                backing = model.lun_for_datastore(ds_name)
                array = model.array_for_lun(backing.id) if backing else None
                mounts.append(
                    {
                        "datastore": ds_name,
                        "access_mode": mount["access_mode"],
                        "mounted": mount["mounted"],
                        "accessible": mount["accessible"],
                        "backing_lun": backing.id if backing else None,
                        "backing_naa": backing.naa if backing else None,
                        "array": array.id if array else None,
                        # True only when VMFS extents name the backing disk
                        # live; vcsim datastores have no extents.
                        "backing_verified_live": bool(
                            ds.get("extents")
                            and backing
                            and any(backing.naa in e for e in ds["extents"])
                        ),
                    }
                )
        hosts_out.append(
            {
                "name": name,
                "declared_hbas": declared_hbas,
                "live_hbas": live_hbas,
                "live_luns": live_luns,
                "declared_luns": visible,
                "multipath": lv.get("multipath", []) if lv else [],
                "topology": lv.get("topology", []) if lv else [],
                "software_iscsi_enabled": lv["software_iscsi_enabled"] if lv else None,
                "mounts": mounts,
                "notes": host_notes,
            }
        )

    datastores_out = []
    for ds_name in sorted(live_ds):
        ds = live_ds[ds_name]
        backing = model.lun_for_datastore(ds_name)
        array = model.array_for_lun(backing.id) if backing else None
        mounted_hosts = [m["host"] for m in ds["mounts"] if m["mounted"]]
        datastores_out.append(
            {
                "name": ds_name,
                "type": ds["type"],
                "info_type": ds["info_type"],
                "capacity_gb": ds["capacity_gb"],
                "free_gb": ds["free_gb"],
                "url": ds["url"],
                "backing_lun": backing.id if backing else None,
                "backing_naa": backing.naa if backing else None,
                "array": array.id if array else None,
                "extents": ds.get("extents"),
                "mounted_on": mounted_hosts,
                "mounts": ds["mounts"],
            }
        )
    for _, lun in model.all_luns():
        if lun.datastore not in live_ds:
            notes.append(
                f"LUN {lun.id} maps to datastore {lun.datastore!r}, which does "
                "not exist live"
            )

    # Model-level notes about what the live inventory cannot show.
    any_fc_live = any(
        h["type"] == "FibreChannelHba" for lh in live_hosts.values() for h in lh["hbas"]
    )
    any_iscsi_live = any(
        h["type"] == "InternetScsiHba" for lh in live_hosts.values() for h in lh["hbas"]
    )
    if not any_fc_live:
        notes.append("no Fibre Channel HBAs served live; declared FC HBAs are model-only")
    if not any_iscsi_live:
        notes.append("no iSCSI HBAs served live; declared iSCSI HBAs are model-only")
    if all(d.get("extents") is None for d in live_ds.values()):
        notes.append(
            "datastores have no VMFS extents live (vcsim datastores are type "
            "OTHER with LocalDatastoreInfo), so datastore-to-LUN backing is "
            "declared in storage.yaml and cannot be verified live"
        )

    return {
        "hosts": hosts_out,
        "datastores": datastores_out,
        "arrays": [asdict(a) for a in model.arrays],
        "notes": notes,
    }


def summarize_storage(si, model: StorageModel) -> dict:
    """Live inventory + declared model, reconciled. The query script's
    single entry point."""
    return reconcile(model, live_storage(si))
