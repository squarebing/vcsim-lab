# vcsim-lab

Build a varied vCenter inventory in [vcsim](https://github.com/vmware/govmomi)
(the govmomi vSphere simulator) with plain Python + pyVmomi — 12 VMs spanning
Windows, Linux and BSD, BIOS and EFI, NVMe/PVSCSI/LSI storage, standard /
distributed / NSX networks, TPM, Secure Boot, 3D video, and three GPU modes —
then query it back the way you would query a real vCenter.

Everything is declarative: edit `vms.yaml`, run one command, get the lab.
The builder is idempotent (re-runs skip existing VMs) and reports, per VM,
exactly which steps vcsim accepted.

Validated end to end against govmomi **v0.56.2** on 2026-11-13:
full build 12 VMs / 40 steps / 0 failed, verify 0 failures
(4 Secure Boot warnings — a vcsim limitation, see below), and a
save/load round-trip that restores all 12 VMs.

## Quickstart

```bash
pip install -r requirements.txt        # pyvmomi, pyyaml
cp .env.example .env                   # optional; defaults already match vcsim

# 1. Start vcsim (pick one)
tools/start-vcsim-binary.sh            # no Docker: downloads the vcsim binary once into ./bin
# or: docker compose up -d

# 2. Build the lab
python scripts/build_lab.py

# 3. Look at it
python scripts/query_lab.py            # table
python scripts/query_lab.py --json     # machine-readable
python scripts/query_lab.py --pci      # host PCI devices (GPU inventory)
python scripts/query_storage.py        # host storage: HBAs, LUNs, mounts
python scripts/verify_lab.py           # live lab vs vms.yaml
```

Tear down the lab VMs (leaves vcsim running):

```bash
python scripts/teardown_lab.py
```

Stop vcsim: `tools/stop-vcsim-binary.sh` (binary) or `docker compose down`.

## Layout

```
vms.yaml                 the lab: 12 VMs, folders, pools, networks (single source of truth)
docker-compose.yml       vcsim container (vmware/vcsim:v0.56.2)
requirements.txt         pyvmomi>=8.0, pyyaml>=6.0
.env.example             VC_HOST/PORT/USER/PASSWORD, VC_INSECURE, VCSIM_VERSION
PROMPTS.md               log of the prompts that built this project
storage.yaml             host storage model: arrays, LUNs, fabric HBAs, LUN -> datastore
Dockerfile               vcsim + lab image (python:3.12-slim, vcsim binary)
docker-entrypoint.sh     start vcsim, build the lab, stay in foreground
render.yaml              Render Blueprint: web service from the Docker Hub image
.dockerignore            keep bin/, model/, caches out of the image
.github/workflows/docker-publish.yml   push to main -> build -> Docker Hub -> Render

vcsim_lab/
  config.py              YAML -> dataclasses + validation (guest IDs, hex PCI IDs)
  specs.py               pure device planner + CreateVM spec assembly (offline-testable)
  devices.py             device builders: controllers, disks, NICs, CD-ROM, TPM, GPU, boot
  vcenter.py             connect, leaf property retrieval, task waiting, lookups
  build.py               idempotent builder + per-VM step report
  query.py               reference read path: summarize one/all VMs
  storage.py             storage model (YAML -> dataclasses) + live reconciliation
  verify.py              live lab vs vms.yaml (core checks fail, best-effort warn)
  teardown.py            delete the lab VMs

scripts/
  build_lab.py           [--only NAME ...] [--recreate] [--report FILE] [--plan-only]
  query_lab.py           [--json] [--inventory] [--pci] [--guest-ids]
  query_storage.py       [--json] [--only HOST ...] [--storage-yaml FILE]
  verify_lab.py          exit code 0 when all core checks pass
  teardown_lab.py        delete the lab VMs

tools/
  start-vcsim-binary.sh  download + run the vcsim binary (no Docker)
  stop-vcsim-binary.sh
  save-model.sh          govc object.save -> ./model for fast restarts (vcsim -load)

tests/test_specs.py      20 offline tests (VM planner/builder)
tests/test_storage.py    23 offline tests (storage model + reconcile)
                         run all: pytest tests/ -q (no vCenter needed)
```

## The lab

One datacenter (`DC0`), one cluster (`DC0_C0`, 3 hosts) + 1 standalone host,
3 datastores (`LocalDS_0/1/2`), networks `VM Network` (standard),
`DC0_DVPG0` / `DC0_DVPG1` (distributed portgroups), `DC0_NSX0` (NSX opaque).
Folders `lab-windows`, `lab-linux`, `lab-gpu`; resource pools
`lab-general`, `lab-gpu`.

| VM | OS | vCPU / RAM | Firmware | Storage | Network | Special |
|---|---|---|---|---|---|---|
| win11-secure | Windows 11 | 4 / 8 GB | EFI + Secure Boot + TPM | NVMe 100G thin | DVPG0 vmxnet3 | CD-ROM, boot disk > cdrom |
| win2022-sql | Windows Server 2022 | 8 / 16 GB | EFI + TPM | PVSCSI 100G thin + 500G eager | DVPG0 + DVPG1 | |
| win10-vdi | Windows 10 | 4 / 8 GB | EFI + Secure Boot + TPM | NVMe 80G thin | DVPG0 vmxnet3 | 3D video, 128 MB VRAM |
| ubuntu-gpu-llm | Ubuntu | 8 / 32 GB | EFI | NVMe 200G thin | DVPG0 vmxnet3 | GPU passthrough (T4) |
| ubuntu-vgpu-vdi | Ubuntu | 4 / 16 GB | EFI | NVMe 100G thin | DVPG0 vmxnet3 | vGPU `nvidia_a100-40c` |
| rhel9-ai-train | RHEL 9 | 16 / 64 GB | EFI + Secure Boot + TPM | NVMe 500G thin | DVPG0 + NSX opaque | GPU dynamic (A100) |
| debian12-web | Debian 12 | 2 / 4 GB | BIOS, vmx-19 | LSI SAS 40G thin | VM Network e1000 | PXE first: network > disk |
| rhel9-db | RHEL 9 | 8 / 32 GB | EFI | PVSCSI 80G + 500G eager + 200G | DVPG0 vmxnet3 | CD-ROM, 3 datastores |
| sles15-sap | SLES 15 | 8 / 32 GB | EFI + Secure Boot + TPM | NVMe 150G thin | DVPG0 vmxnet3 | CD-ROM first: cdrom > disk |
| freebsd13-nas | FreeBSD 13 | 2 / 4 GB | BIOS, vmx-19 | LSI Logic 60G + 2× LSI SAS 200G | VM Network e1000e | |
| centos8-legacy | CentOS 8 | 2 / 2 GB | BIOS, vmx-19 | LSI Logic 60G thick lazy | VM Network e1000 | boot disk > network |
| ubuntu-k8s-node | Ubuntu | 4 / 8 GB | EFI, hot-add | NVMe 100G thin | DVPG0 + DVPG1 | boot disk > network |

GPU VMs also get a full memory reservation and the Broadcom KB 334594
extraConfig (`pciPassthru.use64bitMMIO=TRUE`, `pciPassthru.64bitMMIOSizeGB`).

## Host storage

`storage.yaml` declares the shared storage the lab is *meant* to have —
two arrays, their LUNs, fabric HBAs per host, and the LUN → datastore
mapping — and `scripts/query_storage.py` reconciles it against what
vcsim actually serves:

```bash
python scripts/query_storage.py            # per-host table
python scripts/query_storage.py --json     # full reconciled report
python scripts/query_storage.py --only DC0_C0_H0
```

Declared model: `flash-array-01` (Pure Storage FlashArray//X70, Fibre
Channel, 2 LUNs → LocalDS_0/1) and `iscsi-array-01` (Dell EMC PowerStore
1000T, iSCSI, 1 LUN → LocalDS_2). Every host has two 32 Gbps FC HBAs
(vmhba2/vmhba3, with WWPNs) and one software-iSCSI HBA (vmhba33, with an
initiator IQN); a host "sees" an array's LUNs when it has an HBA of the
array's protocol. NAA names, WWPNs and IQNs are validated on load
(`storage.load_storage`); each datastore maps to at most one LUN.

What vcsim serves vs. what is declared — the query says this plainly,
and so does the report's `notes`:

| | Live (vcsim) | Declared (storage.yaml) |
|---|---|---|
| HBAs | vmhba0 (PVSCSI), vmhba1 + vmhba64 (IDE) — local only | vmhba2/3 FC + vmhba33 iSCSI |
| SCSI LUNs | 32 GB local SSD + CD-ROM per host (`mpx.*`, no NAA names) | 3 array LUNs with NAA names |
| Multipath / topology | served: 1 active path per LUN, policy FIXED | — |
| Software iSCSI | disabled | iSCSI HBA declared per host |
| Datastore mounts | served: all 3 datastores on all 4 hosts, readWrite | — |
| Datastore → LUN backing | not served: datastores are type OTHER, no VMFS extents | LUN per datastore, declared only |

There is no API to create HBAs or LUNs in vSphere — they are discovered
hardware — so the declared half can never be "built" into vcsim; it is a
model to query against. Against a real vCenter the same code reads the
real FC/iSCSI HBAs and, for VMFS datastores, verifies the backing LUN
from the extent disk names (`backing_verified_live`).

## How it works

**Plan → build split.** `specs.plan_vm()` is pure: it assigns controller
keys, disk unit numbers (SCSI skips 7), NIC keys and the CD-ROM's AHCI
controller without touching vCenter, so it is unit-tested offline.
`build_create_spec()` turns a plan + resolved inventory references into a
`vim.vm.ConfigSpec`. New devices in a create spec use temporary negative
keys (controllers −100…, disks −200…, CD-ROM −300, NICs −400…); vCenter
assigns the real keys.

**Create carries core devices only** (controllers, disks, NICs, CD-ROM).
Boot order, TPM, GPU and video are applied afterwards as separate
best-effort `ReconfigVM_Task` steps, each recorded in the build report, so
one device the simulator refuses cannot fail the whole VM. This split is
also forced by vcsim behaviour (see below): the video card *must* be a
post-create edit, and Secure Boot is sent with the boot-order reconfig.

**Idempotent.** Existing VMs are skipped unless `--recreate` is given;
folders and resource pools are ensured; datastores and networks are
resolved up front so a typo in `vms.yaml` fails fast instead of
halfway through the build. `--plan-only` prints the device plan without
connecting.

**Verify semantics.** Core mismatches (CPU, memory, guest, firmware,
disks, NICs, boot order, power, video 3D) are failures. TPM, GPU and
Secure Boot mismatches are warnings: vcsim accepts the devices but serves
some of their detail back degraded, and drops the Secure Boot flag
entirely (see below).

## Save / load the model

```bash
tools/save-model.sh            # govc object.save -> ./model (downloads govc once)
tools/stop-vcsim-binary.sh && tools/start-vcsim-binary.sh   # auto-loads ./model
```

The start script picks up `./model` automatically when present; for Docker,
uncomment the volume + `-load` command in `docker-compose.yml`.
Saving must capture the *whole* ServiceContent (no `-dc`, no path) — a
subtree save omits the root folder and a later `-load` serves an empty
inventory even though the objects load. The start script runs vcsim from a
scratch working directory because the `-load` path creates the datastore
backing directories (`DC0-LocalDS_*`) in its cwd and refuses to start if
they already exist there.

## vcsim vs real vCenter

Everything in this section was observed live against govmomi v0.56.2 and
is handled in the code (or in `verify.py`'s best-effort warnings). If you
point this project at a real vCenter, most of these disappear — but the
code paths are written to be correct against both.

**Standard-network NICs must set `deviceName`.** vcsim's `configureDevice`
resolves a `NetworkBackingInfo` NIC by `FindByName(b.DeviceName,
dc.Network).Reference()` — an empty `deviceName` nil-dereferences and
**kills the simulator process** (`simulator/virtual_machine.go:1537`),
in both `CreateVM` and `ReconfigVM`, for vmxnet3 and e1000 alike.
`devices._nic_backing` always sets `backing.deviceName` alongside
`backing.network`. (Real vCenter accepts the same fields.)

**Whole-object property reads fail; leaf reads work.** `vm.runtime` and
`vm.summary` fail to deserialize in pyVmomi because vcsim returns an
empty string for the `FaultToleranceState` enum inside them; whole
`task.info` fails for FileManager tasks because vcsim sets
`TaskInfo.entity` to the FileManager, which is not a ManagedEntity.
`vcenter.retrieve_props` / `retrieve_props_multi` fetch leaf paths
(`runtime.powerState`, `info.state`, …) instead, and `wait_for_task`
polls `info.state` / `info.error` that way. Never read `vm.summary`,
`vm.runtime` or `task.info` as whole objects against vcsim.

**Some properties are not served at all.** `HostSystem.config` raises
`AttributeError` (no graphics info, shared-GPU capabilities, or
`pciPassthruInfo`); `EnvironmentBrowser.guestOsDescriptor` is not served
and `QueryConfigOption` fails deserializing an empty `SharesInfo.Level`
enum. `--guest-ids` therefore prints an explanation instead of a list.
GPU host discovery uses `HostSystem.configManager` / PCI device reads
that *are* served. But leaf paths under `config.storageDevice` *are*
served: `config.storageDevice.hostBusAdapter` / `scsiLun` return through
the PropertyCollector even though whole `host.config` raises —
`storage.live_host_storage` uses them as a fallback.

**Host storage is served, but only local.** `HostStorageSystem.storageDeviceInfo`
(HBAs, SCSI LUNs, topology, multipath, software-iSCSI state) *is*
served, via leaf retrieves — each host has vmhba0 (PVSCSI), vmhba1 +
vmhba64 (PIIX4 IDE), a 32 GB local SSD (`mpx.vmhba0:C0:T0:L0`) and a
CD-ROM, one active multipath path per LUN (policy FIXED), and software
iSCSI disabled. But there are no FC/iSCSI HBAs, no NAA-named LUNs and
no arrays, and the datastores are type OTHER with `LocalDatastoreInfo`
(not VMFS), so there are no extents linking a datastore to a LUN. The
storage model in `storage.yaml` declares that half instead, and
`query_storage.py` reconciles the two. The StoragePod (`DC0_POD0`) is
empty (capacity 0, no member datastores).

**Cross-datastore disks need their directories pre-created.**
`CreateVM` fails with `FileNotFound` unless the VM's directory already
exists on each *non-home* datastore (`FileManager.MakeDirectory`);
pre-creating the *home* directory instead causes `FileAlreadyExists`,
because vcsim creates it itself. `build.ensure_vm_dirs` skips the home
datastore, and `build_one_vm` removes stale home directories before
creating — a leftover directory from a failed create makes the next
create fail, and v0.56.2 can panic the simulator while handling the
conflict, so the builder never creates into leftovers and never retries
a conflicted create.

**`Datacenter.network` omits opaque networks** (and lists some DVPGs
twice). `find_network` also walks `networkFolder.childEntity`, which
lists every network type including `OpaqueNetwork`.

**TPM is served back degraded.** The `ReconfigVM` to add a vTPM
succeeds, but vcsim serves the device back as the base `VirtualDevice`
type with label `Virtual TPM`, not as `VirtualTPM`. `devices.has_tpm`
matches both shapes (used by the query tool and by the builder's
already-has-TPM check). On real vCenter a vTPM also needs a Key
Provider configured — vcsim has none, which is why the step is
best-effort.

**Secure Boot is dropped.** `efiSecureBootEnabled=true` is sent
(verified by serializing the pyVmomi request) and vcsim's `apply()`
copies `BootOptions` wholesale, yet the live value reads back `None`
— the flag is lost in vcsim's decode path. The builder still sends it
(correct for real vCenter); `verify.py` treats it as best-effort.

**Video cards are replaced at create time.** vcsim's `addDefaultDevices`
swaps any create-spec video card for the ESXi default (4 MB VRAM, no
3D). The builder therefore edits the existing card (key 500) post-create
via `ReconfigVM`, which vcsim *and* real vCenter honour.

**Dynamic GPU backing is served back degraded.** A dynamic (DirectPath)
GPU device is present after the build, but vcsim serves its backing as
the base `DeviceBackingInfo` — the `allowedDevice` list is lost, so the
query tool reports it as `gpu=unknown`. Passthrough and vGPU backings
keep their full detail.

**Passthrough binds the SVGA adapter.** Each vcsim host reports 43 PCI
devices, of which exactly one is display-class: the VMware SVGA II
Adapter (`0000:00:0f.0`, `0x15ad:0x0405`). There is no NVIDIA device, so
passthrough mode falls back to the first GPU-class device and the build
report says so. On real hardware with a T4/A100 it binds the requested
card.

**Synchronous creates.** `Folder.CreateFolder` and
`ResourcePool.CreateResourcePool` return the object directly, not a
Task. `ResourceConfigSpec` requires `cpuAllocation`/`memoryAllocation`
(the builder supplies normal-share defaults).

**Miscellaneous.** vcsim's datastore browser shows VM folders as empty
(no real `.vmx`/`.vmdk` files listed). `BootableCdromDevice` and
`BootableFloppyDevice` have no `deviceKey` property — only disk and
network boot entries carry one (handled in `devices.resolve_boot_devices`).

## Guest IDs

A few IDs are not what you would guess; these were checked against
Broadcom's guest-OS documentation and are validated (warn-only) in
`config.py`:

- Windows Server 2022 → `windows2019srvNext_64Guest` (there is no
  `windows2022srv_64Guest`); Server 2025 → `windows2022srvNext_64Guest`
- Windows 10 → `windows9_64Guest` (not `windows10_64Guest`)
- `centos8_64Guest`, `debian12_64Guest`, `rhel9_64Guest`, `sles15_64Guest`,
  `freebsd13_64Guest` are valid as written

## Pointing at a real vCenter

Set `VC_HOST`, `VC_USER`, `VC_PASSWORD` in `.env` and `VC_INSECURE=0` if
the certificate is trusted. The inventory names in `vms.yaml`
(datacenter, cluster, datastores, networks) must match the real
inventory — the builder fails fast on the first missing reference.
Notes: a vTPM needs a Key Provider; GPU passthrough needs a host with
the requested PCI device (or the build reports the fallback); Secure
Boot and video 3D are honoured for real, so `verify.py`'s warnings
should clear.

## CI/CD pipeline

Every push to `main` rebuilds the container image and redeploys it:

```
push to main
  -> GitHub Actions (.github/workflows/docker-publish.yml)
  -> docker build + push binghuspectrocloud/vcsim-lab:latest (and :<sha>)
  -> POST the Render deploy hook
  -> Render pulls :latest and runs it
```

The image (`Dockerfile`, base `python:3.12-slim`) contains the vcsim
binary plus the lab code. On start, `docker-entrypoint.sh` launches
vcsim with the same inventory flags as `docker-compose.yml` on
`0.0.0.0:${PORT:-8989}` (Render injects `$PORT`), waits for the SDK
endpoint to answer, runs `scripts/build_lab.py` so the 12 VMs exist,
then keeps vcsim in the foreground. Run it locally the same way:

```bash
docker build -t vcsim-lab .
docker run -p 8989:8989 vcsim-lab   # SDK: https://localhost:8989/sdk
```

### One-time setup

1. **Docker Hub secrets** — in the GitHub repo, Settings -> Secrets and
   variables -> Actions, add:
   - `DOCKERHUB_USERNAME` — `binghuspectrocloud`
   - `DOCKERHUB_TOKEN` — a Docker Hub access token (Docker Hub ->
     Account Settings -> Personal Access Tokens). Without these the
     workflow fails loudly with setup instructions; it never silently
     skips the publish.
2. **Render service** — push once so Actions publishes the image, then
   in the Render Dashboard: New -> Blueprint -> connect
   `squarebing/vcsim-lab`. Render reads `render.yaml` (free plan, web
   service from the prebuilt image) and prompts for `VCSIM_USERNAME`
   and `VCSIM_PASSWORD`. Set strong credentials — the service is
   public, and the defaults are `user` / `pass`.
3. **Deploy hook** — in the Render service, Settings -> Deploy Hook,
   copy the URL and add it as the `RENDER_DEPLOY_HOOK` GitHub repo
   secret. Until then the workflow still publishes the image and skips
   the Render step with a notice. (Deploys come only from the hook:
   `autoDeploy` is off in `render.yaml`, so a repo push can't race the
   image publish.)

### Free-plan caveats

- Free web services **spin down after 15 minutes without traffic**
  and take about a minute to wake on the next request. The simulator
  is in-memory, so every wake rebuilds the lab from scratch (the
  entrypoint runs `build_lab.py` on each start) — anything created
  interactively in the sim is lost on spin-down.
- Free instances are capped at 750 instance-hours per workspace per
  month, so one always-on service nearly exhausts the budget.
- There is no health-check endpoint: Render's default port check is
  what marks the service live, and vcsim listens as soon as it starts.
