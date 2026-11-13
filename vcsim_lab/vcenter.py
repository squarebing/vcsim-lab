"""vCenter connection helpers and inventory lookups (pyVmomi).

Kept separate from the lab logic so the same helpers serve the builder, the
query tool and the verifier.
"""

from __future__ import annotations

import socket
import ssl
import time
from typing import TYPE_CHECKING

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .config import VCenterConfig

if TYPE_CHECKING:
    from pyVmomi import Vim


class VCenterError(RuntimeError):
    pass


def connect(cfg: VCenterConfig) -> "Vim.ServiceInstance":
    """Connect to vCenter/vcsim. vcsim uses a self-signed cert, so the
    default is an unverified SSL context; set VC_INSECURE=0 for a real
    vCenter with a trusted certificate.

    The scheme comes from the config (VC_SCHEME): SmartConnect defaults
    to https, so plain-http vcsim (the container on Render, -tls=false)
    needs protocol="http" passed explicitly — otherwise the client
    speaks TLS to a plaintext server and fails with
    "[SSL: WRONG_VERSION_NUMBER] wrong version number".
    """
    context = None
    if cfg.insecure:
        context = ssl._create_unverified_context()
    try:
        si = SmartConnect(
            protocol=cfg.scheme,
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            pwd=cfg.password,
            sslContext=context,
        )
    except Exception as exc:
        raise VCenterError(
            f"could not connect to vCenter at {cfg.host}:{cfg.port} as {cfg.user!r}: {exc}. "
            "Is vcsim running? See README 'Start vcsim'."
        ) from exc
    return si


def disconnect(si: "Vim.ServiceInstance") -> None:
    try:
        Disconnect(si)
    except Exception:
        pass


def wait_for_vcenter(cfg: VCenterConfig, timeout_s: int = 60) -> "Vim.ServiceInstance":
    """Wait for the SDK endpoint to accept connections, then connect.

    vcsim takes a few seconds to build its model after the port opens, so a
    bare TCP check is not enough: retry the full login until it succeeds.
    """
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((cfg.host, cfg.port), timeout=5):
                pass
        except OSError as exc:
            last_error = exc
            time.sleep(2)
            continue
        try:
            return connect(cfg)
        except VCenterError as exc:
            last_error = exc
            time.sleep(2)
    raise VCenterError(f"vCenter at {cfg.host}:{cfg.port} did not come up in {timeout_s}s: {last_error}")


def wait_for_task(si: "Vim.ServiceInstance", task: vim.Task, timeout_s: int = 300, label: str = "task") -> None:
    """Block until a task finishes; raise VCenterError with the server's
    message on failure.

    The task state is read with leaf property retrieves, not ``task.info``:
    vcsim sets TaskInfo.entity to the FileManager for file tasks, and
    pyVmomi refuses to deserialize that ("expected ManagedEntity, got
    FileManager"). Leaf paths skip the entity field entirely.
    """
    deadline = time.time() + timeout_s
    while True:
        props = retrieve_props(si, task, vim.Task, ["info.state"])
        state = props.get("info.state")
        if state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued, None):
            if time.time() > deadline:
                raise VCenterError(f"{label}: timed out after {timeout_s}s (state={state})")
            time.sleep(1)
            continue
        if state == vim.TaskInfo.State.error:
            err_props = retrieve_props(si, task, vim.Task, ["info.error"])
            err = err_props.get("info.error")
            msg = getattr(err, "msg", None) or getattr(err, "localizedMessage", None) or str(err)
            fault = type(err).__name__ if err is not None else "unknown fault"
            raise VCenterError(f"{label}: failed with {fault}: {msg}")
        if state != vim.TaskInfo.State.success:
            raise VCenterError(f"{label}: ended in unexpected state {state}")
        return


# ---------------------------------------------------------------------------
# Inventory lookups
# ---------------------------------------------------------------------------


def safe_name(obj) -> str | None:
    """An object's name, or None if the object is gone.

    vcsim's ContainerView keeps listing templates after they are
    destroyed (observed v0.56.2: every destroyed template leaves one
    stale MOR that never clears; destroying a regular VM does not),
    and any property read on one raises ManagedObjectNotFound. Name
    lookups must skip those entries instead of crashing — a real
    vCenter never serves them, so this is a no-op there.
    """
    try:
        return obj.name
    except vmodl.fault.ManagedObjectNotFound:
        return None


def get_obj(content: vim.ServiceInstanceContent, vimtype, name: str):
    """Find a managed object of `vimtype` by name (first match)."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        for obj in view.view:
            if safe_name(obj) == name:
                return obj
    finally:
        view.Destroy()
    return None


def list_objs(content: vim.ServiceInstanceContent, vimtype) -> list:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def find_datacenter(si: "Vim.ServiceInstance", name: str) -> vim.Datacenter:
    dc = get_obj(si.RetrieveContent(), vim.Datacenter, name)
    if dc is None:
        available = [d.name for d in list_objs(si.RetrieveContent(), vim.Datacenter)]
        raise VCenterError(f"datacenter {name!r} not found; available: {available}")
    return dc


def find_cluster(dc: vim.Datacenter, name: str) -> vim.ClusterComputeResource:
    for child in dc.hostFolder.childEntity:
        if isinstance(child, vim.ClusterComputeResource) and child.name == name:
            return child
    available = [c.name for c in dc.hostFolder.childEntity if isinstance(c, vim.ClusterComputeResource)]
    raise VCenterError(f"cluster {name!r} not found in datacenter {dc.name!r}; available: {available}")


def find_resource_pool(cluster: vim.ClusterComputeResource, name: str) -> vim.ResourcePool:
    """Find a child resource pool of the cluster root pool by name."""
    for pool in cluster.resourcePool.resourcePool:
        if pool.name == name:
            return pool
    return None


def find_vm_folder(dc: vim.Datacenter, name: str) -> vim.Folder:
    for child in dc.vmFolder.childEntity:
        if isinstance(child, vim.Folder) and child.name == name:
            return child
    return None


def find_datastore(dc: vim.Datacenter, name: str) -> vim.Datastore:
    for ds in dc.datastore:
        if ds.name == name:
            return ds
    available = [d.name for d in dc.datastore]
    raise VCenterError(f"datastore {name!r} not found in datacenter {dc.name!r}; available: {available}")


def find_network(dc: vim.Datacenter, name: str):
    """Find a network by name across standard networks, DV portgroups and
    opaque networks. Returns None when missing (the caller decides whether
    to fail or fall back).

    vcsim quirk: Datacenter.network omits OpaqueNetwork objects (and lists
    some DVPGs twice), so also walk the network folder, which lists every
    network type including opaque networks.
    """
    for net in dc.network:
        if net.name == name:
            return net
    return _find_network_in_folder(dc.networkFolder, name)


def _find_network_in_folder(folder: vim.Folder, name: str):
    for child in folder.childEntity:
        if isinstance(child, vim.Network) and child.name == name:
            return child
        if isinstance(child, vim.Folder):
            found = _find_network_in_folder(child, name)
            if found is not None:
                return found
    return None


def find_vm_by_name(si: "Vim.ServiceInstance", name: str) -> vim.VirtualMachine | None:
    return get_obj(si.RetrieveContent(), vim.VirtualMachine, name)


def task_error_message(exc: Exception) -> str:
    """Best-effort readable message for a task/VIM fault."""
    if isinstance(exc, vmodl.MethodFault):
        return getattr(exc, "msg", None) or str(exc)
    return str(exc)


# ---------------------------------------------------------------------------
# Leaf property retrieval
# ---------------------------------------------------------------------------


def retrieve_props(si: "Vim.ServiceInstance", obj, vimtype, paths: list[str]) -> dict:
    """Retrieve *leaf* properties (e.g. "runtime.powerState") for one
    managed object via the PropertyCollector.

    Why not just ``vm.runtime``? vcsim returns an empty string for the
    fault-tolerance state enum inside VirtualMachineRuntimeInfo, and
    pyVmomi fails to deserialize the whole ``runtime`` / ``summary``
    object ("FaultToleranceState has no attribute ''"). Leaf paths skip
    the broken field and work fine. Leaf retrieval is also the efficient
    way to read inventory at scale.
    """
    content = si.RetrieveContent()
    obj_spec = vmodl.query.PropertyCollector.ObjectSpec(obj=obj)
    prop_spec = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=paths)
    filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
    result = {}
    for obj_content in content.propertyCollector.RetrieveProperties(specSet=[filter_spec]) or []:
        for prop in obj_content.propSet:
            result[prop.name] = prop.val
    return result


def retrieve_props_multi(si: "Vim.ServiceInstance", objs, vimtype, paths: list[str]) -> dict:
    """Retrieve leaf properties for many objects in one call.

    Returns {moId: {path: value}}. If any object in the set is gone
    (vcsim serves stale ContainerView entries for destroyed templates),
    the batched call fails with ManagedObjectNotFound — fall back to
    per-object retrieves and silently drop the stale entries.
    """
    if not objs:
        return {}
    content = si.RetrieveContent()
    obj_specs = [vmodl.query.PropertyCollector.ObjectSpec(obj=o) for o in objs]
    prop_spec = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=paths)
    filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=obj_specs, propSet=[prop_spec])
    result = {}
    try:
        retrieved = content.propertyCollector.RetrieveProperties(specSet=[filter_spec]) or []
    except vmodl.fault.ManagedObjectNotFound:
        for obj in objs:
            try:
                result[obj._moId] = retrieve_props(si, obj, vimtype, paths)
            except vmodl.fault.ManagedObjectNotFound:
                continue
        return result
    for obj_content in retrieved:
        result[obj_content.obj._moId] = {p.name: p.val for p in obj_content.propSet}
    return result


def vm_power_state(si: "Vim.ServiceInstance", vm: vim.VirtualMachine) -> str:
    """A VM's power state, via a leaf retrieve (see retrieve_props)."""
    props = retrieve_props(si, vm, vim.VirtualMachine, ["runtime.powerState"])
    return props.get("runtime.powerState", "unknown")


def mo_name(si: "Vim.ServiceInstance", obj, vimtype) -> str | None:
    """A managed object's name via a leaf retrieve."""
    props = retrieve_props(si, obj, vimtype, ["name"])
    return props.get("name")
