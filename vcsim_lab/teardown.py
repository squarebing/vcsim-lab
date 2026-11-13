"""Tear down the lab: power off and delete the VMs from vms.yaml.

Folders and resource pools are left in place by default; pass
--remove-folders / --remove-pools to remove them too (only when empty).
"""

from __future__ import annotations

from pyVmomi import vim

from .config import LabConfig, VCenterConfig
from .vcenter import (
    connect,
    disconnect,
    find_cluster,
    find_datacenter,
    find_resource_pool,
    find_vm_by_name,
    find_vm_folder,
    vm_power_state,
    wait_for_task,
)


def teardown_lab(
    cfg: VCenterConfig,
    lab: LabConfig,
    only: list[str] | None = None,
    remove_folders: bool = False,
    remove_pools: bool = False,
) -> list[str]:
    log: list[str] = []
    si = connect(cfg)
    try:
        selected = [v for v in lab.vms if only is None or v.name in only]
        for spec in selected:
            vm = find_vm_by_name(si, spec.name)
            if vm is None:
                log.append(f"{spec.name}: not present, skipped")
                continue
            if vm_power_state(si, vm) == vim.VirtualMachinePowerState.poweredOn:
                task = vm.PowerOffVM_Task()
                wait_for_task(si, task, label=f"power off {spec.name}")
            task = vm.Destroy_Task()
            wait_for_task(si, task, label=f"delete {spec.name}")
            log.append(f"{spec.name}: deleted")
        if remove_folders:
            dc = find_datacenter(si, lab.datacenter)
            for name in lab.folders:
                folder = find_vm_folder(dc, name)
                if folder is None:
                    continue
                if folder.childEntity:
                    log.append(f"folder {name}: not empty, kept")
                    continue
                task = folder.Destroy_Task()
                wait_for_task(si, task, label=f"delete folder {name}")
                log.append(f"folder {name}: deleted")
        if remove_pools:
            dc = find_datacenter(si, lab.datacenter)
            cluster = find_cluster(dc, lab.cluster)
            for name in lab.resource_pools:
                pool = find_resource_pool(cluster, name)
                if pool is None:
                    continue
                if pool.vm or pool.resourcePool:
                    log.append(f"resource pool {name}: not empty, kept")
                    continue
                task = pool.Destroy_Task()
                wait_for_task(si, task, label=f"delete resource pool {name}")
                log.append(f"resource pool {name}: deleted")
    finally:
        disconnect(si)
    return log
