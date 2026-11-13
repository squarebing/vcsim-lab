"""Build the lab: folders, resource pools, VMs, then best-effort extras.

Idempotent: VMs that already exist are left alone (use --recreate to
delete and rebuild them), but their per-VM attributes (CBT, power
state, boot delay) are reconciled against the resolved assignment on
every run. Every step is recorded in the returned report, including
which best-effort steps (TPM, GPU, boot order, power-state) the
simulator accepted — run with --report to write it as JSON.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field

from pyVmomi import vim

from . import devices, specs
from .config import (
    LabConfig,
    ResolvedAssignment,
    VCenterConfig,
    VmSpec,
    resolve_random_assignments,
)
from .vcenter import (
    VCenterError,
    connect,
    disconnect,
    find_cluster,
    find_datacenter,
    find_datastore,
    find_network,
    find_resource_pool,
    find_vm_by_name,
    find_vm_folder,
    retrieve_props,
    vm_power_state,
    wait_for_task,
)


@dataclass
class StepResult:
    step: str
    status: str  # ok | skipped | failed
    detail: str = ""


@dataclass
class VmReport:
    name: str
    created: bool
    steps: list[StepResult] = field(default_factory=list)
    # The VM's resolved per-VM attributes (cbt, power_state, fast_boot,
    # tools_status, boot_delay_ms) — what the build aimed for.
    assigned: dict | None = None

    def add(self, step: str, status: str, detail: str = "") -> None:
        self.steps.append(StepResult(step, status, detail))

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == "failed"]


@dataclass
class BuildReport:
    started: str
    vms: list[VmReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started": self.started,
            "notes": self.notes,
            "vms": [
                {
                    "name": vm.name,
                    "created": vm.created,
                    "assigned": vm.assigned,
                    "steps": [
                        {"step": s.step, "status": s.status, "detail": s.detail} for s in vm.steps
                    ],
                }
                for vm in self.vms
            ],
        }


# ---------------------------------------------------------------------------
# Inventory setup
# ---------------------------------------------------------------------------


def ensure_folder(dc: vim.Datacenter, name: str) -> vim.Folder:
    existing = find_vm_folder(dc, name)
    if existing is not None:
        return existing
    # CreateFolder is synchronous: it returns the new Folder, not a Task.
    return dc.vmFolder.CreateFolder(name)


def ensure_resource_pool(cluster: vim.ClusterComputeResource, name: str) -> vim.ResourcePool:
    existing = find_resource_pool(cluster, name)
    if existing is not None:
        return existing
    # CreateResourcePool is synchronous: it returns the new pool.
    # cpuAllocation/memoryAllocation are required fields of the spec.
    def _alloc():
        return vim.ResourceAllocationInfo(
            reservation=0,
            expandableReservation=True,
            limit=-1,
            shares=vim.SharesInfo(level=vim.SharesInfo.Level.normal),
        )

    pool_spec = vim.ResourceConfigSpec(cpuAllocation=_alloc(), memoryAllocation=_alloc())
    return cluster.resourcePool.CreateResourcePool(name, pool_spec)


def ensure_vapp(cluster: vim.ClusterComputeResource, name: str) -> vim.VirtualApp:
    """Create (or find) a vApp container under the cluster's root pool.

    CreateVApp is a ResourcePool method (not a Folder method) and is
    synchronous: it returns the new VirtualApp. A vApp is a ResourcePool
    subtype, so VMs are created inside it by passing it as the pool.
    """
    existing = find_resource_pool(cluster, name)
    if existing is not None:
        return existing

    def _alloc():
        return vim.ResourceAllocationInfo(
            reservation=0,
            expandableReservation=True,
            limit=-1,
            shares=vim.SharesInfo(level=vim.SharesInfo.Level.normal),
        )

    res_spec = vim.ResourceConfigSpec(cpuAllocation=_alloc(), memoryAllocation=_alloc())
    product = vim.vApp.ProductInfo()
    product.key = 0
    product.name = name
    product.vendor = "vcsim-lab"
    product.version = "1.0"
    product.fullVersion = "1.0"
    product_spec = vim.vApp.ProductSpec()
    product_spec.operation = "add"
    product_spec.info = product
    vapp_spec = vim.vApp.VAppConfigSpec()
    vapp_spec.product = [product_spec]
    return cluster.resourcePool.CreateVApp(name, res_spec, vapp_spec)


def resolve_refs(si, lab: LabConfig, dc, cluster) -> dict:
    """Resolve datastores, networks, folders and pools once, up front, so
    a typo fails fast before any VM is created."""
    datastores = {}
    for vm in lab.vms:
        for disk in vm.disks:
            if disk.datastore not in datastores:
                datastores[disk.datastore] = find_datastore(dc, disk.datastore)

    networks, network_types, opaque, notes = {}, {}, {}, []
    for logical, net in lab.networks.items():
        obj = find_network(dc, net.name)
        if obj is None and net.fallback:
            fallback = lab.networks[net.fallback]
            obj = find_network(dc, fallback.name)
            if obj is not None:
                notes.append(
                    f"network {net.name!r} ({logical}) not in inventory; "
                    f"NICs on it use {fallback.name!r} ({net.fallback}) instead"
                )
                networks[logical] = obj
                network_types[logical] = fallback.type
                continue
        if obj is None:
            available = sorted(n.name for n in dc.network)
            raise VCenterError(
                f"network {net.name!r} ({logical}) not found; available: {available}. "
                "Check the vcsim flags in docker-compose.yml (-pg / -nsx) "
                "or the networks: map in vms.yaml."
            )
        networks[logical] = obj
        network_types[logical] = net.type
        if net.type == "opaque":
            props = retrieve_props(
                si, obj, vim.OpaqueNetwork, ["summary.opaqueNetworkId", "summary.opaqueNetworkType"]
            )
            opaque[logical] = (
                props.get("summary.opaqueNetworkId"),
                props.get("summary.opaqueNetworkType"),
            )

    folders = {name: ensure_folder(dc, name) for name in lab.folders}
    pools = {name: ensure_resource_pool(cluster, name) for name in lab.resource_pools}
    vapps = {name: ensure_vapp(cluster, name) for name in lab.vapps}
    hosts = list(cluster.host)
    return {
        "datastores": datastores,
        "networks": networks,
        "network_types": network_types,
        "opaque": opaque,
        "folders": folders,
        "pools": pools,
        "vapps": vapps,
        "hosts": hosts,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Per-VM steps
# ---------------------------------------------------------------------------


def ensure_vm_dirs(si, dc: vim.Datacenter, spec: VmSpec) -> None:
    """Pre-create the VM's home directory on each datastore it uses.

    Real vCenter creates these on demand, but vcsim's CreateVM fails with
    FileNotFound for a disk on a datastore whose VM directory does not
    exist yet (verified against vcsim v0.56.2). Pre-creating is a no-op
    on real vCenter and makes multi-datastore VMs work in the simulator.
    """
    file_manager = si.RetrieveContent().fileManager
    home_datastore = spec.disks[0].datastore
    for ds_name in {d.datastore for d in spec.disks}:
        if ds_name == home_datastore:
            # vcsim creates the home directory itself during CreateVM and
            # fails with FileAlreadyExists if it is already there — so
            # only pre-create directories on the *other* datastores.
            continue
        try:
            file_manager.MakeDirectory(f"[{ds_name}] {spec.name}", dc, True)
        except Exception:
            pass  # already exists, or CreateVM will surface a real problem


def _cleanup_vm_dirs(si, dc: vim.Datacenter, spec: VmSpec) -> None:
    """Best-effort removal of a VM's home directories on all its
    datastores. Used after a failed CreateVM so a retry does not trip
    over the previous attempt's leftover directory (vcsim fails the
    create with FileAlreadyExists when the home directory is present)."""
    file_manager = si.RetrieveContent().fileManager
    for ds_name in {d.datastore for d in spec.disks}:
        try:
            task = file_manager.DeleteDatastoreFile_Task(f"[{ds_name}] {spec.name}", dc)
            wait_for_task(si, task, label=f"remove [{ds_name}] {spec.name}")
        except Exception:
            pass


def _reconfig(si, vm: vim.VirtualMachine, spec: vim.vm.ConfigSpec, label: str) -> None:
    task = vm.ReconfigVM_Task(spec)
    wait_for_task(si, task, label=label)


def apply_boot_order(
    si, vm: vim.VirtualMachine, spec: VmSpec, boot_delay_ms: int, report: VmReport
) -> None:
    boot_devices, warnings = devices.resolve_boot_devices(vm, spec.boot_order)
    for warning in warnings:
        report.add("boot-order", "failed", warning)
    if not boot_devices:
        report.add("boot-order", "failed", "no bootable devices resolved; boot order left as default")
        return
    boot = devices.build_boot_options(
        boot_devices, secure_boot=spec.secure_boot, boot_delay_ms=boot_delay_ms
    )
    cfg = vim.vm.ConfigSpec(bootOptions=boot)
    _reconfig(si, vm, cfg, f"set boot order on {spec.name}")
    actual = devices.describe_boot_order(vm)
    report.add(
        "boot-order", "ok", f"{' > '.join(actual) or '(empty)'} (bootDelay {boot_delay_ms}ms)"
    )


def apply_video(si, vm: vim.VirtualMachine, spec: VmSpec, report: VmReport) -> None:
    """Set the VM's video card VRAM / 3D by editing the default card.

    A video card in the CreateVM spec is silently replaced by vcsim's
    ESXi default (4 MB, no 3D); editing the existing card post-create is
    honoured by vcsim and by real vCenter.
    """
    cards = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualVideoCard)]
    if not cards:
        report.add("video", "failed", "VM has no video card to edit")
        return
    cfg = vim.vm.ConfigSpec(
        deviceChange=[devices.build_video_edit(cards[0].key, spec.video_ram_kb, spec.video_3d)]
    )
    _reconfig(si, vm, cfg, f"set video card on {spec.name}")
    detail = f"{spec.video_ram_kb // 1024}MB VRAM" + (" + 3D" if spec.video_3d else "")
    report.add("video", "ok", detail)


def add_tpm(si, vm: vim.VirtualMachine, spec: VmSpec, report: VmReport) -> None:
    if devices.has_tpm(vm.config.hardware.device):
        report.add("tpm", "skipped", "VM already has a vTPM")
        return
    cfg = vim.vm.ConfigSpec(deviceChange=[devices.build_tpm()])
    _reconfig(si, vm, cfg, f"add vTPM to {spec.name}")
    report.add("tpm", "ok", "VirtualTPM device added")


def add_gpu(si, vm: vim.VirtualMachine, spec: VmSpec, hosts, report: VmReport) -> None:
    if any(isinstance(d, vim.vm.device.VirtualPCIPassthrough) for d in vm.config.hardware.device):
        report.add("gpu", "skipped", "VM already has a PCI passthrough device")
        return
    gpu_spec, note = devices.gpu_device_spec(spec.gpu, hosts)
    cfg = vim.vm.ConfigSpec(deviceChange=[gpu_spec])
    _reconfig(si, vm, cfg, f"add GPU to {spec.name}")
    report.add("gpu", "ok", note)


def apply_cbt(
    si, vm: vim.VirtualMachine, spec: VmSpec, assigned: ResolvedAssignment, report: VmReport
) -> None:
    """Reconcile changeTrackingEnabled with the assigned value.

    Fresh builds set CBT in the create spec, so this step skips; it
    repairs a VM whose CBT was changed, or whose assignment changed
    (e.g. a new random seed). Changing CBT requires the VM to be
    powered off on real vCenter, so a powered-on or suspended VM is
    powered off first — the power-state step afterwards restores its
    target state.
    """
    props = retrieve_props(si, vm, vim.VirtualMachine, ["config.changeTrackingEnabled"])
    live = props.get("config.changeTrackingEnabled")
    if live == assigned.cbt:
        report.add("cbt", "skipped", f"changeTrackingEnabled already {live}")
        return
    if vm_power_state(si, vm) != "poweredOff":
        task = vm.PowerOffVM_Task()
        wait_for_task(si, task, label=f"power off {spec.name} to change CBT")
    _reconfig(
        si,
        vm,
        vim.vm.ConfigSpec(changeTrackingEnabled=assigned.cbt),
        f"set changeTrackingEnabled={assigned.cbt} on {spec.name}",
    )
    report.add("cbt", "ok", f"changeTrackingEnabled {live} -> {assigned.cbt}")


def apply_power_state(
    si, vm: vim.VirtualMachine, spec: VmSpec, assigned: ResolvedAssignment, report: VmReport
) -> None:
    """Bring the VM to its assigned power state.

    poweredOn:  PowerOnVM_Task (also resumes a suspended VM).
    suspended:  power on first if off, then SuspendVM_Task.
    poweredOff: PowerOffVM_Task from on or suspended.
    """
    target = assigned.power_state
    current = vm_power_state(si, vm)
    if current == target:
        report.add("power-state", "skipped", f"already {target}")
        return
    if target == "poweredOn":
        task = vm.PowerOnVM_Task()
        wait_for_task(si, task, label=f"power on {spec.name}")
    elif target == "suspended":
        if current == "poweredOff":
            task = vm.PowerOnVM_Task()
            wait_for_task(si, task, label=f"power on {spec.name} before suspend")
        task = vm.SuspendVM_Task()
        wait_for_task(si, task, label=f"suspend {spec.name}")
    else:  # poweredOff
        task = vm.PowerOffVM_Task()
        wait_for_task(si, task, label=f"power off {spec.name}")
    report.add("power-state", "ok", f"{current} -> {target}")


def apply_vapp_config(si, vm: vim.VirtualMachine, spec: VmSpec, report: VmReport) -> None:
    """Reconcile the VM's vAppConfig (OVF product info + properties).

    The create spec already sets it, so a fresh build skips here; this
    step repairs a VM whose vAppConfig was changed or dropped. vcsim
    accepts vAppConfig on ReconfigVM and serves it back intact.
    """
    if devices.vapp_config_matches(vm, spec.vapp):
        report.add("vapp-config", "skipped", "vAppConfig already matches the spec")
        return
    cfg = vim.vm.ConfigSpec(vAppConfig=devices.build_vapp_config(spec.vapp))
    _reconfig(si, vm, cfg, f"set vAppConfig on {spec.name}")
    live = devices.describe_vapp_config(vm)
    product = (live or {}).get("product") or {}
    report.add(
        "vapp-config",
        "ok",
        f"{product.get('name')} {product.get('version')} by {product.get('vendor')}",
    )


def mark_as_template(si, vm: vim.VirtualMachine, spec: VmSpec, report: VmReport) -> None:
    """Mark the VM as a template (config.template = true).

    Runs LAST: device reconfigs on a template fail with NotSupported in
    vcsim (and on real vCenter a template is not reconfigurable), so
    every device-adding extra must run before this step. MarkAsTemplate
    is synchronous and requires the VM to be powered off.
    """
    if devices.is_template(vm):
        report.add("template", "skipped", "VM is already a template")
        return
    if vm_power_state(si, vm) != "poweredOff":
        task = vm.PowerOffVM_Task()
        wait_for_task(si, task, label=f"power off {spec.name} before marking as template")
    vm.MarkAsTemplate()
    report.add("template", "ok", "marked as template (config.template = true)")


def build_one_vm(
    si,
    lab: LabConfig,
    spec: VmSpec,
    refs: dict,
    recreate: bool,
    assigned: ResolvedAssignment,
) -> VmReport:
    report = VmReport(name=spec.name, created=False, assigned=assigned.to_dict())
    dc = find_datacenter(si, lab.datacenter)

    existing = find_vm_by_name(si, spec.name)
    if existing is not None:
        if not recreate:
            report.add("create", "skipped", "VM already exists (use --recreate to rebuild)")
            vm = existing
        else:
            if vm_power_state(si, existing) != "poweredOff":
                task = existing.PowerOffVM_Task()
                wait_for_task(si, task, label=f"power off {spec.name}")
            task = existing.Destroy_Task()
            wait_for_task(si, task, label=f"delete {spec.name}")
            existing = None
    if existing is None:
        folder = refs["folders"].get(spec.folder) or dc.vmFolder
        # A vApp is a ResourcePool, so a VM with vapp_container is
        # created with pool=<the vApp> instead of a plain pool.
        pool = refs["vapps"].get(spec.vapp_container) or refs["pools"].get(spec.resource_pool)
        if pool is None:
            cluster = find_cluster(dc, lab.cluster)
            pool = cluster.resourcePool
        plan = specs.plan_vm(spec)
        resolved = specs.ResolvedRefs(
            folder=folder,
            resource_pool=pool,
            datastores=refs["datastores"],
            networks=refs["networks"],
            network_types=refs["network_types"],
            opaque=refs["opaque"],
        )
        config = specs.build_create_spec(spec, plan, resolved, cbt=assigned.cbt)
        # Remove any stale home directories from an earlier attempt first:
        # vcsim fails the create with FileAlreadyExists when the home
        # directory is present, and (v0.56.2) can even crash the simulator
        # while handling the conflict — so never create into leftovers.
        _cleanup_vm_dirs(si, dc, spec)
        ensure_vm_dirs(si, dc, spec)
        try:
            task = folder.CreateVM_Task(config=config, pool=pool)
            wait_for_task(si, task, label=f"create VM {spec.name}")
        except VCenterError:
            _cleanup_vm_dirs(si, dc, spec)
            raise
        report.add("create", "ok", f"{spec.cpus} vCPU, {spec.memory_mb} MB, {spec.guest_id}")
        report.created = True
        vm = find_vm_by_name(si, spec.name)

    # Best-effort extras: each runs even if a previous one failed, and a
    # failure is recorded instead of aborting the build.
    # Video is customised only when the spec asks for 3D or a non-default
    # VRAM size (VmSpec default is 16384 KB); other VMs keep the default
    # card vCenter/vcsim adds at create time.
    #
    # A VM that already exists as a template skips all device extras:
    # device reconfigs on a template fail with NotSupported, and a
    # template cannot be powered on. It was fully configured before it
    # was marked, so there is nothing to redo.
    if devices.is_template(vm):
        report.add("template", "skipped", "VM is already a template; extras skipped")
        return report
    video_step = (
        ("video", lambda: apply_video(si, vm, spec, report))
        if spec.video_3d or spec.video_ram_kb != 16384
        else (None, None)
    )
    for step, fn in (
        ("boot-order", lambda: apply_boot_order(si, vm, spec, assigned.expected_boot_delay_ms(), report)),
        video_step,
        ("tpm", lambda: add_tpm(si, vm, spec, report)) if spec.tpm else (None, None),
        ("gpu", lambda: add_gpu(si, vm, spec, refs["hosts"], report)) if spec.gpu else (None, None),
        ("vapp-config", lambda: apply_vapp_config(si, vm, spec, report))
        if spec.vapp
        else (None, None),
        # CBT runs before the power-state step: the VM is still off, and
        # changing CBT requires a powered-off VM on real vCenter.
        ("cbt", lambda: apply_cbt(si, vm, spec, assigned, report)),
        # The power-state step always runs (it skips when the VM is
        # already in its assigned state), so a re-run reconciles a VM
        # whose assignment changed.
        ("power-state", lambda: apply_power_state(si, vm, spec, assigned, report)),
        # Template marking runs LAST, after every device extra.
        ("template", lambda: mark_as_template(si, vm, spec, report))
        if spec.template
        else (None, None),
    ):
        if step is None:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — recorded in the report
            report.add(step, "failed", str(exc))
    return report


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


def build_lab(
    cfg: VCenterConfig,
    lab: LabConfig,
    only: list[str] | None = None,
    recreate: bool = False,
) -> BuildReport:
    report = BuildReport(started=time.strftime("%Y-%m-%dT%H:%M:%S"))
    report.notes.extend(lab.warnings)
    si = connect(cfg)
    try:
        about = si.RetrieveContent().about
        report.notes.append(f"connected to {about.fullName} (API {about.apiVersion})")
        dc = find_datacenter(si, lab.datacenter)
        cluster = find_cluster(dc, lab.cluster)
        refs = resolve_refs(si, lab, dc, cluster)
        report.notes.extend(refs["notes"])
        report.notes.append(
            f"cluster {cluster.name}: {len(refs['hosts'])} hosts, "
            f"{len(refs['datastores'])} datastores, {len(refs['networks'])} networks"
        )
        selected = [v for v in lab.vms if only is None or v.name in only]
        if only:
            missing = sorted(set(only) - {v.name for v in lab.vms})
            if missing:
                raise VCenterError(f"unknown VM name(s) in --only: {missing}")
        # Resolve the per-VM attributes once: the same seed always gives
        # the same assignment, so builds are reproducible.
        assignments = resolve_random_assignments(lab)
        power = Counter(a.power_state for a in assignments.values())
        report.notes.append(
            f"random attributes resolved with seed {lab.random_seed}: "
            f"{power['poweredOn']} poweredOn / {power['suspended']} suspended / "
            f"{power['poweredOff']} poweredOff, CBT on "
            f"{sum(1 for a in assignments.values() if a.cbt)}/{len(assignments)}, "
            f"fast-boot {sum(1 for a in assignments.values() if a.fast_boot)}/{len(assignments)}"
        )
        for spec in selected:
            report.vms.append(build_one_vm(si, lab, spec, refs, recreate, assignments[spec.name]))
    finally:
        disconnect(si)
    return report


def print_report(report: BuildReport) -> None:
    for note in report.notes:
        print(f"note: {note}")
    print()
    n_ok = n_fail = 0
    for vm in report.vms:
        print(f"{vm.name} {'(created)' if vm.created else '(existed)'}")
        if vm.assigned:
            a = vm.assigned
            print(
                f"  assigned: cbt={a['cbt']} power={a['power_state']} "
                f"fast_boot={a['fast_boot']} (bootDelay {a['boot_delay_ms']}ms) "
                f"tools={a['tools_status']}"
            )
        for step in vm.steps:
            mark = {"ok": "ok  ", "skipped": "skip", "failed": "FAIL"}[step.status]
            print(f"  [{mark}] {step.step:<11} {step.detail}")
            if step.status == "failed":
                n_fail += 1
            elif step.status == "ok":
                n_ok += 1
    print()
    print(f"{len(report.vms)} VMs, {n_ok} successful steps, {n_fail} failed steps")


def write_report(report: BuildReport, path: str) -> None:
    with open(path, "w") as fh:
        json.dump(report.to_dict(), fh, indent=2)
    print(f"report written to {path}")
