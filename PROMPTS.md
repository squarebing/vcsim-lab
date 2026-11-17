# Prompts

A chronological log of the prompts (user requests) that built this project,
kept so the reasoning behind the code stays traceable. Prompts are quoted
verbatim; each entry notes what it produced.

Times are US/Pacific. Append new entries at the end as the project evolves.

## 1. 2026-11-13 11:28 — Build the vcsim lab

> I am using python pyvmomi to query a vcenter system. And I want to use vcsim to simulate a vcenter environment, with a v ariery of VMs with different OS, memory, CPU, GPU, TPM, boot sequence, storage and networks. I need code to help me build the said vcsim env.

Outcome: the whole project — `vms.yaml` (12 VMs), the `vcsim_lab` package,
`scripts/` (build/query/verify/teardown), `tools/`, `tests/`, and the README.
Validated live against govmomi vcsim v0.56.2: full build 12 VMs / 40 steps /
0 failed, verify 0 failures, save/load round-trip restores all 12 VMs,
20 offline tests pass.

## 2. 2026-11-13 12:44 — Publish to GitHub

> can you create a project in github and put the code there as a new project

Outcome: published as the public repo https://github.com/squarebing/vcsim-lab —
23 files in one snapshot commit (`fcaff0e9`) on `main`. The GitHub connector
cannot create repositories (403 for App tokens), so the empty repo was created
on github.com first.

## 3. 2026-11-13 12:45 — Visibility: public

> Public

Outcome: repo created public, as chosen. (Answer to "public or private?")

## 4. 2026-11-13 12:47 — GitHub App installed

> it is done

Outcome: confirmed the Muse GitHub App installation; verified access as
`squarebing`. Repo creation via the connector then failed (403), so the repo
was created manually on github.com instead.

## 5. 2026-11-13 12:50 — Repo created

> it is done

Outcome: confirmed `squarebing/vcsim-lab` exists, is public, empty, and
defaults to `main`; the 23 files were ready to push.

## 6. 2026-11-13 12:51 — Confirm the push

> Yes, push it

Outcome: pushed the snapshot commit `fcaff0e9` (on top of the provider's
generated initial commit `5dfcd58`); verified the project's README is the one
on the repo page.

## 7. 2026-11-13 12:55 — Keep a prompt log

> Keep track of prompts used in the project

Outcome: this file.

## 8. 2026-11-13 13:22 — Add host storage info

> add storage info to hosts, such as storage arrays, HBA, SCSI Luns and mounts

Outcome: host storage inventory. Probed vcsim first: it serves per-host
HBAs (vmhba0 PVSCSI, vmhba1/vmhba64 IDE), two local LUNs (32 GB SSD +
CD-ROM, `mpx.*` names), SCSI topology, multipath info (1 active path per
LUN, FIXED) and datastore mounts — but no FC/iSCSI HBAs, no NAA LUNs, no
arrays, and datastores are type OTHER with no VMFS extents. Added
`storage.yaml` (declared model: FC array + iSCSI array, 3 LUNs mapped to
LocalDS_0/1/2, fabric HBAs per host), `vcsim_lab/storage.py` (model
validation + live reconciliation), `scripts/query_storage.py` (per-host
table / `--json`), and `tests/test_storage.py` (23 offline tests; 43
total). README documents what vcsim serves vs. what is declared.

## 9. 2026-11-13 13:37 — CI/CD pipeline: Docker Hub + Render

> can you setup the pipeline so that, once new code is pushed, the docker image is updated and pushed to docker hub, and render.com is connected to the new docker image and make it running

Outcome: CI/CD pipeline. Added `Dockerfile` (python:3.12-slim + vcsim
binary from the govmomi release) and `docker-entrypoint.sh` (start
vcsim on 0.0.0.0:$PORT with the lab's flags, build the 12 VMs, keep
vcsim in the foreground), `.dockerignore`, `.github/workflows/docker-publish.yml`
(push to main -> build -> push squarebing/vcsim-lab:latest and
:<sha> to Docker Hub -> POST the Render deploy hook), and `render.yaml`
(Blueprint: free web service, runtime image). Docker Hub secrets fail
loudly when unset; the Render step skips with a notice until
RENDER_DEPLOY_HOOK is set. README documents the flow, the secrets, and
the free-plan spin-down caveat. Image build itself needs the first
Actions run (no Docker daemon in the sandbox). Note: the GitHub connector
cannot create workflow files (403 — the App token lacks the Workflows
permission), so `.github/workflows/docker-publish.yml` was added via the
GitHub web UI instead of the snapshot push. Fixed 2026-11-13 14:12: the
workflow failed at parse time because GitHub does not allow the `secrets`
context in `if` conditionals ("Unrecognized named-value: 'secrets'") —
the Render-hook check now happens in shell instead of a step `if`.

## 10. 2026-11-13 14:30 — Render URL gives HTTP-to-HTTPS error

> when check Render URL:  http://vcsim-lab.onrender.com/ or https://vcsim-lab.onrender.com/, I received client sent an http request to an https server issue.

Outcome: Render terminates TLS at its edge and forwards plain HTTP to
the container, but vcsim serves HTTPS by default, so its Go server
rejected the forwarded requests. Fixed by serving plain HTTP in the
container: vcsim runs with `-tls=false` (flag verified in v0.56.2:
"Enable TLS (default true)"), the entrypoint probes `http://` and
passes `VC_SCHEME=http` to the builder, and `VCenterConfig` gained a
`scheme` field read from `VC_SCHEME` (default `https`, so local runs
are unchanged). Public clients still reach the sim over HTTPS at the
Render URL. Added `tests/test_config.py` (4 tests). Note: this fix was
incomplete — see entry 11. The scheme reached the URL property but
not the connection itself, so the Render build still failed.

## 11. 2026-11-13 14:39 — Render build fails: SSL WRONG_VERSION_NUMBER

> render failed with "build failed: could not connect to vCenter at 127.0.0.1:10000 as 'squarebing': [SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1011). Is vcsim running? See README 'Start vcsim'."

Outcome: the entry-10 fix only changed the `VCenterConfig.url`
property, which nothing in the connection path uses:
`vcenter.connect()` calls pyVmomi's `SmartConnect`, which defaults to
`protocol='https'` and silently ignores the configured scheme — so the
builder still spoke TLS to the now-plaintext vcsim. Fixed by passing
`protocol=cfg.scheme` to `SmartConnect` (verified against the
installed pyVmomi: `SmartConnect(protocol='https', ...)` — "What
protocol to use for the connection (e.g. https or http)"). Added
`tests/test_vcenter.py` (2 tests, mocking `SmartConnect` to assert the
protocol passthrough).

## 12. 2026-11-13 14:47 — NVIDIA + AMD GPU VMs, VM templates, appliance VMs

> add VMs with Nvidia and AMD GPU, and add some VM templates, and also add two VMs as appliances

Outcome: the lab grew from 12 to 21 VMs. Probing first showed vcsim
accepts all three: passthrough naming an NVIDIA/AMD PCI device binds the
hosts' only display device (VMware SVGA II) with the declared IDs kept
in the build report; vGPU profile names are stored unvalidated; dynamic
GPUs read back degraded (allowedDevice lost). `MarkAsTemplate()` works
and `config.template` reads back true, but device reconfigs on a
template fail with `NotSupported`, so marking runs last. `vAppConfig`
(VmConfigSpec with ProductSpec/PropertySpec, operation "add") is
accepted on CreateVM and ReconfigVM and reads back intact, and
`ResourcePool.CreateVApp` (a ResourcePool method, not a Folder method)
creates a working vApp container. One interpretation call: AMD MxGPU is
SR-IOV VF passthrough, not a vGPU profile (the vGPU-profile mechanism
is NVIDIA-only), so the AMD shared-GPU VM is modelled as dynamic
DirectPath with MI210 PCI IDs and the loader rejects AMD + vgpu.
Added: `ubuntu-nvidia-a100-pt` (NVIDIA A100 passthrough),
`ubuntu-nvidia-h100-vgpu` (`nvidia_h100-80c`), `rhel9-amd-mi210-pt`
(AMD MI210 passthrough), `win11-amd-mxgpu` (AMD MxGPU dynamic, +TPM,
Secure Boot, 3D); templates `win2022-template`,
`ubuntu-2404-template`, `rhel9-template` (`template: true`, marked last,
skipped on re-run); appliances `opnsense-fw` (OPNsense 24.7, WAN+LAN)
and `truenas-storage` (TrueNAS 24.10, OS + 200G data disk) with OVF
properties in the `lab-appliances` vApp. `config.py` carries GPU
vendor/model with PCI cross-checks and the vApp model; `verify.py`
checks template flag and vApp product/properties as core checks;
teardown removes the vApp under `--remove-pools`, using leaf property
retrieval for the emptiness check because reading
`VirtualApp.resourcePool` directly fails pyVmomi deserialization
(`KeyError: 'type'` — same for a template's `resourcePool`). Full build
21 VMs / 69 steps / 0 failed in ~6s; verify 0 failures, 5 Secure Boot
warnings (the known vcsim limitation); save/load round-trip restores
all 21 VMs; 72 tests (23 new in `tests/test_gpu_vapp.py`).

## 13. 2026-11-13 15:09 — Random CBT, power state, tools status, fast boot

> add random CBT, power state, tool status and fast boot to VMs

Outcome: four per-VM attributes now vary across the lab, each an
explicit value or `"random"` in `vms.yaml`, resolved from
`lab.random_seed` (42) at build time. Probing first showed what vcsim
actually serves: `changeTrackingEnabled` is accepted in create and
reconfig specs and reads back; `bootOptions.bootDelay` is stored and
served back; `SuspendVM_Task` works and `suspended` reads back as the
power state (PowerOn resumes it); but `guest.toolsStatus` is always
`toolsNotInstalled` / `guestToolsNotRunning` in every power state —
vcsim never simulates guest tools, and there is no setter API for it.
Two interpretation calls: there is no native vSphere VM "fast boot"
setting (only host-level ESXi Quick Boot), so fast boot is represented
as `bootDelay = 0` vs. the standard 3000 ms; and tools status is
declared-only — the query tool shows live + declared and flags
mismatches, and verify treats it as a best-effort warning. Resolution
is seeded per VM (`"<seed>:<vm-name>"`, fixed draw order cbt →
power_state → fast_boot → tools_status), so the same seed always gives
the same assignment and adding a VM never reshuffles the others; the
build report records each VM's assigned values. Templates are exempt
from power-state assignment (always `poweredOff`, `power_state:` on a
template is rejected); appliances keep explicit `poweredOn`. The
builder sets CBT in the create spec, reconciles it on re-runs (powering
the VM off first, since real vCenter requires that), and enforces the
power state (suspend = power on, then suspend); changing the seed
reconciles the lab to the new assignment without a rebuild. Seed 42
lands at 9 poweredOn / 6 suspended / 6 poweredOff, CBT on 9/21,
fast-boot 8/21, all four tools statuses declared. The teardown →
rebuild cycle exposed a vcsim quirk: destroying a template leaves a
stale MOR in the ContainerView (one per destroyed template, never
clearing), which crashed `find_vm_by_name`, the batched leaf retrieve
and `--inventory` — fixed with `vcenter.safe_name` (skip stale entries
in name lookups) plus a per-object fallback in `retrieve_props_multi`
and a stale-entry skip in `summarize_lab`. Teardown also now powers off
suspended VMs before destroying them. Full build 21 VMs /
75 steps / 0 failed; verify 0 failures, 22 warnings (5 Secure Boot +
17 tools status, both vcsim limitations); idempotent re-run is a
no-op on CBT/power; 97 tests (25 new in `tests/test_random_attrs.py`).

## 14. 2026-11-13 15:30 — BIOS vs UEFI, multi-disk VMs, basic vs dynamic disks

> set some VMs to BIOS, some to UEFI..  Set some VMs with multiple disks.  Set some VMs with basic disk and some with dynamic disks. Create new VMs if needed

Firmware was lopsided (4 BIOS / 17 EFI) and only 4 VMs had multiple
disks. Rebalanced to 10 BIOS / 12 EFI by converting five EFI VMs to
BIOS (rhel9-db, ubuntu-k8s-node, ubuntu-2404-template, rhel9-template,
truenas-storage) and adding one new BIOS VM (win2019-fileserver,
Windows Server 2019). TPM / Secure Boot VMs and Windows 11 VMs stay
EFI — the loader already rejected TPM or Secure Boot on BIOS, and the
templates now span both firmwares. 13 of 22 VMs have 2+ disks (40
disks total): second disks added to win11-secure, ubuntu-gpu-llm,
rhel9-ai-train, sles15-sap, centos8-legacy, ubuntu-k8s-node and
win11-amd-mxgpu, third disks to win2022-sql and win10-vdi.

Basic vs. dynamic disk is the Windows Disk Management distinction and
vSphere has no API property for it (confirmed by searching pyVmomi's
vim.vm device types — the only "dynamic" hits are the unrelated GPU
DynamicBackingInfo), so it is a declared per-disk attribute
(`disk_type: basic | dynamic`, default basic) plus a `volumes:` list
grouping dynamic disks into named volumes (simple / spanned / striped
/ mirrored, 1-based disk indexes). The loader validates it: dynamic
disks are Windows-only, every disk in a volume must be dynamic, and
spanned/striped/mirrored need 2+ disks. All four volume kinds are
represented: simple on win11-secure (Data), mirrored on win2022-sql
(SQLData), striped on win10-vdi (Scratch), spanned on
win2019-fileserver (FileShare). So the layout is visible in the
inventory, VMs with dynamic disks carry it in extraConfig
(`lab.diskLayout`, e.g. `basic; dynamic:SQLData:mirrored;
dynamic:SQLData:mirrored`) — probed live, vcsim serves extraConfig
back intact — and the query tool shows the declared type per disk
while verify checks the live extraConfig value against the declared
layout (a missing key warns rather than fails, since the layout is
declared-only by nature). Full build 22 VMs / 77 steps / 0 failed in
~8s; verify 0 failures, 23 warnings (5 Secure Boot + 18 tools status,
both vcsim limitations); idempotent re-run and --recreate clean;
save/load round-trip restores all 22 VMs; 130 tests (33 new in
`tests/test_firmware_disks.py`).

## 15. 2026-11-13 16:43 — Inventory API (FastAPI, no API key)

> ok. let' build the api (without API key)

Outcome: the inventory API — `vcsim_lab/api.py` (FastAPI app) plus
`scripts/serve_api.py` (uvicorn runner). This followed a feasibility
discussion in the same conversation: FastAPI is not required, but it
is the nicest option (free OpenAPI docs); the user asked to keep the
current endpoints and put the API at `/api`. The API is the front door
on the public port: it serves `/api/*` itself and reverse-proxies
every other path to vcsim, so `/sdk` (the vSphere SOAP endpoint — the
lab's own code connects to it) and `/about` (vcsim's methods/types
page) keep working unchanged on the same port. No authentication, by
the user's explicit choice — the API is read-only inventory; the vcsim
credentials still guard `/sdk` itself, since the proxy forwards auth
through rather than bypassing it.

Endpoints: `/api` (index), `/api/inventory` (full inventory + counts),
`/api/vms` (full summaries, `?template=true/false` filter),
`/api/vms/{name}` (404 when unknown), `/api/hosts` (PCI devices, GPU
capability, their VMs), `/api/datastores`, `/api/networks`,
`/api/vapps` (new `query.list_vapps`, members + product info),
`/api/storage` (reconciled with `storage.yaml`, live-only when the
file is missing), `/api/health` (200 when vcsim is reachable, 503 with
a JSON body when it is not), plus `/docs`. The endpoints reuse the
existing read paths (`query.summarize_lab`, `list_inventory`,
`list_host_pci`, `storage.summarize_storage`) — no new inventory logic
— and show the declared-vs-live values when `vms.yaml` is present
(new shared `config.load_declared_assignments`, also used by
`query_lab.py` now). Each request opens its own vCenter connection
(sync `def` endpoints, run in FastAPI's thread pool); when vcsim is
down the endpoints answer 503 and the proxy 502, both with JSON
bodies. The proxy forwards method, path, query, headers (including
`Authorization`/`Cookie`) and body byte-identical, and passes status,
headers (including `Set-Cookie`) and body back.

Wiring: `docker-entrypoint.sh` now starts vcsim on internal
127.0.0.1:8989, builds the lab, then serves the API on
0.0.0.0:${PORT:-8000} (uvicorn backgrounded and waited on, so the TERM
trap fires promptly and cleanup stops both processes);
`docker-compose.yml` builds the project image and publishes 8000
(the lab builds inside the container); `Dockerfile` default port is
8000; `requirements.txt` gains fastapi, uvicorn, httpx; `.env.example`
gains API_HOST/API_PORT. `render.yaml` needed no change (Render
injects $PORT).

One real bug found during live validation: the proxy crashed with
httpx `InvalidURL` at client construction because httpx's default
`trust_env=True` parses the environment's proxy vars and choked on a
bare `::1` NO_PROXY entry — fixed with `trust_env=False` (also correct
in principle: the hop to vcsim must never use an environment proxy),
with a regression test asserting it. Validated live against govmomi
v0.56.2: fresh build 22 VMs / 77 steps / 0 failed; all endpoints
return correct data (22 VMs, 4 hosts, 3 datastores, 6 networks, 1
vApp, 2 arrays); `/docs` loads; a real pyVmomi client logs in through
the proxied `/sdk` and sees all 22 VMs with the session cookie
surviving; `/about` through the proxy matches vcsim's output
directly; vcsim-down behavior verified (health 503, `/api/vms` 503,
proxy 502); the container's plain-HTTP path (`-tls=false`,
VC_SCHEME=http) validated the same way. 151 tests (21 new in
`tests/test_api.py`, mocked vCenter + fake httpx backend). Docker is
not available in the sandbox, so the image was not built — the
entrypoint was syntax-checked and its flow validated by the live
HTTP-mode simulation instead.

## 16. 2026-11-13 18:17 — Multiple vCenter environments

> reorganize this repo, so that I can create multiple vCenter environments. The current one can be named "default". For endpoints, they stay the same, but I can switch to different environments.

Outcome: the repo is reorganized around `environments/<name>/`, one
vCenter simulation per directory. Each environment has `env.yaml`
(name, description, vcsim port, inventory flags), `vms.yaml` (its lab)
and `storage.yaml` (its storage model). The current lab moved
unchanged into `environments/default/` (22 VMs, port 8989, same vcsim
flags); a small new `environments/dev/` (3 VMs, port 8990, 2 hosts, 2
datastores) proves the mechanism and is the template for new
environments — copy it, pick a name and an unused port, edit the YAML.
No code changes are needed to add an environment: the scripts, the API
and the container discover environments by scanning `environments/`.

`vcsim_lab/config.py` gains the environment model: `Environment`
(name, port, flags, paths, `vcsim_args()`, `vcenter_config()`),
`list_environments()` (sorted, duplicate-port rejection) and
`load_environment(name)`. `env.yaml` flags are all optional (defaults
match the original lab); the name must match the directory name.
Credentials stay out of `env.yaml` — VCSIM_USERNAME/VCSIM_PASSWORD
are shared across environments. All scripts take `--env` (default
`default`): build/query/query_storage/verify/teardown resolve the lab,
storage model and vcsim port from the environment; an explicitly set
VC_PORT still overrides the port (the container-proxy workflow).
`scripts/env_info.py` prints shell assignments for an environment so
the shell scripts can read env.yaml; the tools take `--env` too
(per-environment pid/log files, `model/<env>/` save dirs;
`stop-vcsim-binary.sh --all` stops every environment).

The API serves all environments from the same endpoints
(`vcsim_lab/api.py` builds an `EnvironmentRegistry` at startup, one
target per environment with its own port and declared models, loaded
best-effort). Switching works two ways: globally, `POST
/api/environment {"name": "dev"}` switches the active environment
(in-memory; restarts default to `default`) — unknown names are a 404
and switching to an unreachable environment is a 503 that leaves the
active environment unchanged; per request, `?env=<name>` (or the
`X-Environment` header, query param wins) serves one request from
another environment without changing the active one, and the resolved
environment is stamped on the `X-Environment` response header. New
endpoints: `GET /api/environments` (all environments with live status
and VM counts) and `GET /api/environment` (the active one);
`/api/health` now reports per-environment status too. The reverse
proxy routes to the selected environment as well (`/sdk?env=dev` pins
one SOAP session to dev's vcsim; the `env` pin is stripped from the
forwarded query string). SOAP-session caveat, documented in the code
and README: a session cookie belongs to one vcsim, so do not switch
the active environment mid-session — reconnect, or pin with
`/sdk?env=<name>`.

Wiring: `docker-entrypoint.sh` starts one vcsim per environment (each
on its internal port from env.yaml, `-tls=false`), builds each lab,
then serves the API; `VCSIM_ENVS` (comma-separated) limits which
environments start, default all. `docker-compose.yml` / `Dockerfile` /
`render.yaml` / `.env.example` updated for the multi-env flow (the
Dockerfile copies the whole project, so no structural change was
needed there). Validated live against govmomi v0.56.2: both
environments running concurrently (default 8989, dev 8990), both labs
built and verified, API switching + per-request override + proxied
`/sdk` (real pyVmomi client) + `/about` all proven; the real
entrypoint was run end-to-end in the sandbox via an `/app` symlink
(both vcsim instances in plain-HTTP Render mode, both lab builds,
API, TERM-trap cleanup, and a `VCSIM_ENVS=dev` subset run all
verified). Validations during live testing found and fixed three
things: (1)
`env_info.py` printed the flag list unquoted, so `eval` in the shell
scripts ran its second word as a command — values are now
shlex-quoted, with a regression test that evals the output in bash;
(2) pyVmomi's `SmartConnect(path='/sdk?env=dev')` mangles its
version probe into `/sdk?env=dev/vimServiceVersions.xml`, so the
proxy now splits that suffix back out (env names never contain `/`)
and forwards the probe to the pinned environment — both
`path='/sdk?env=dev'` and `customHeaders={"X-Environment": "dev"}`
pin a real pyVmomi session, verified live; (3) the entrypoint now
passes the first started environment as the API's active env
(`--active-env`), so with `VCSIM_ENVS=dev` the endpoints serve dev
out of the box instead of an unreachable `default`. 187 tests (19
new in `tests/test_environments.py`, 17 new/changed in
`tests/test_api.py`).
Docker is not available in the sandbox, so the image was not built —
the entrypoint was syntax-checked and run live in the sandbox
instead. Note: the old top-level `vms.yaml` / `storage.yaml`
still exist on GitHub until deleted via the web UI (the push
procedure cannot delete files).

## 17. 2026-11-13 18:49 — Fix "unknown active environment 'default\ndev'" on Render

> render is complaining about
>
> config error: unknown active environment 'default\ndev'; available: default, dev

Outcome: the Render deploy of the multi-environment reorg failed at
startup because `docker-entrypoint.sh` derived the API's `--active-env`
as `${ENVS%% *}` — a suffix strip that only splits on spaces — while
`scripts/env_info.py --list` prints one environment name per line, so
with two environments `$ENVS` was the newline-separated string
"default\ndev" and the whole list was passed as one environment name.
The API's validation correctly rejected it. Fix: the entrypoint now
captures the first environment inside its start loop (`FIRST_ENV` set
from the loop variable on the first iteration), which is a single name
by construction regardless of whether the list is newline-separated
(`--list`) or space-separated (`VCSIM_ENVS`). Regression test in
`tests/test_environments.py`: the loop-capture derivation run in bash
against the real `--list` output yields `default`, plus a tripwire
asserting the entrypoint no longer contains the space-only split.
Validated by running the real `docker-entrypoint.sh` end to end in
the sandbox via an `/app` symlink (both vcsim instances in plain-HTTP
Render mode, both labs built, API serving with active env `default`,
switching + `?env=` override + proxied `/sdk` for both environments
proven with curl and a real pyVmomi client, TERM-trap cleanup
verified). 188 tests.

## 18. 2026-11-14 18:56 — toolsOk VMs, a Solaris VM, and 0/1/2 snapshots

> Can I have some VMs with vm.guest.toolsStatus equal to toolsOk...
> Also, I want to have an OS with Solaris OS. And VMs with 0, 1, 2
> snapshots

Outcome: three features in the default environment (dev unchanged),
lab now 23 VMs. (1) Five VMs declare `tools_status: toolsOk`
explicitly — `win2022-sql` (Windows), `debian12-web` (Debian),
`solaris11-db` (Solaris), `ubuntu-k8s-node` (Ubuntu, already had it),
`truenas-storage` (FreeBSD) — plus two more that draw it from seed
42, so 7/23 declare toolsOk. Declared-only, as before: vcsim always
reports `toolsNotInstalled` live, so the query tool shows
declared-vs-live and verify warns (19 tools-status warnings, no
failures); the README says so next to the existing tools-status note.
(2) New VM `solaris11-db` (Oracle Solaris 11, `solaris11_64Guest`,
BIOS, 2 vCPU / 4096 MB, 2 disks, VM Network, `lab-linux` folder,
powered on, CBT on, toolsOk declared, 1 snapshot); the Solaris guest
IDs (`solaris10/11_64Guest`, `solaris10/11Guest`) were added to
`config.KNOWN_GUEST_IDS`. (3) New per-VM `snapshots:` list in
`vms.yaml` (name + optional description): 5 VMs with 2 snapshots, 6
with 1, 12 with none (16 total). The loader rejects snapshots on
templates (vSphere forbids it) and duplicate names; the builder
creates them after the power-state step (`CreateSnapshot`,
memory=False, quiesce=False — works in any power state) and only for
freshly created VMs, so a re-run creates no duplicates (verified
live: still 16 after a re-run). Query reads the tree via leaf
retrieve (`snapshot.rootSnapshotList`, `snapshot.currentSnapshot`)
with a best-effort fallback that reports unreadable state as unknown
rather than zero; verify checks live snapshot names as a core check
(skipped when the state is unreadable); the API serves snapshots in
the per-VM summaries. Live-validated against vcsim v0.56.2: full
build 23 VMs / 91 steps / 0 failed, verify 0 failures (5 Secure Boot
+ 19 tools-status warnings), live snapshot distribution 12×0 / 6×1 /
5×2 matches declared exactly, Solaris VM confirmed with the right
guest ID, API `/api/vms/solaris11-db` serves the snapshot fields, and
a save/load round-trip restores all 23 VMs and all 16 snapshots.
One vcsim gap found and documented: the snapshot tree's `state` field
always reads back `poweredOff` regardless of the VM's power state at
capture time, so it is reported but never verified. 219 tests (31 new
in `tests/test_snapshots.py`).

## 19. 2026-11-14 19:13 — Nested folders for the lab's VMs

> in vCenter, created nested VMs and put VMs in them

Clarified in chat (19:14): "nested folders" — not nested resource
pools or nested ESXi. The motivation is preflight: its `--folder`
flag matches by subtree, and the lab's folders were flat, so every VM
reported the same path.

Outcome: the default environment's five flat folders (`lab-windows`,
`lab-linux`, `lab-gpu`, `lab-templates`, `lab-appliances`) became a
nested hierarchy under the datacenter vm folder, three levels deep,
with VMs at every level (dev unchanged):

```
lab/                      freebsd13-nas
├── Prod/                 win11-secure, win2019-fileserver, ubuntu-k8s-node
│   ├── Web/              debian12-web, sles15-sap
│   ├── DB/               win2022-sql, rhel9-db, solaris11-db
│   └── VDI/              win10-vdi, win11-amd-mxgpu, ubuntu-vgpu-vdi
├── Lab/
│   ├── GPU/              ubuntu-gpu-llm, rhel9-ai-train,
│                         ubuntu-nvidia-a100-pt, ubuntu-nvidia-h100-vgpu,
│                         rhel9-amd-mi210-pt
│   └── Legacy/           centos8-legacy
├── Templates/            win2022-template, ubuntu-2404-template, rhel9-template
└── Appliances/           opnsense-fw, truenas-storage
```

Each VM declares `folder:` as a path relative to the datacenter vm
folder (`""` for the vm folder itself); `config.validate_folder_path`
rejects leading/trailing slashes, empty segments and `.`/`..`, and
`load_lab` rejects duplicate `lab.folders` entries. The builder
creates missing parents recursively (`Folder.CreateFolder`, idempotent
— a re-run creates nothing) and ensures every VM's folder path even
when it is not listed in `lab.folders`, so a create never silently
falls back to the vm folder root. The query tool reports each VM's
live folder path, walked from the VM's parent chain via a batched
`parent` leaf retrieve plus a shared per-folder cache
(`vcenter.folder_rel_path`; None when the chain cannot be read); the
API serves it in the per-VM summaries; `verify.py` checks the live
path against the declared one as a core check (skipped when unknown).
Teardown removes folders deepest first (`--remove-folders`), so the
whole tree comes down. The appliance VMs show the two hierarchies are
independent: they sit in the `lab/Appliances` folder *and* in the
`lab-appliances` vApp resource pool. Live-validated against vcsim
v0.56.2: full build 23 VMs / 91 steps / 0 failed, verify 0 failures
(5 Secure Boot + 19 tools-status warnings), live folder distribution
matches declared exactly, the `lab/Prod` subtree holds 11 VMs (3
direct + 8 in Web/DB/VDI), the raw tree walk shows real nested Folder
objects, the API serves `folder` per VM, a re-run leaves the folder
count at 10, and teardown `--remove-folders` deletes all 10 folders
with the vm folder left empty. 261 tests (41 new in
`tests/test_folders.py`, 1 new API folder test).

## 20. 2026-11-15 17:20 — Settings for testing preflight checks: disk.EnableUUID, RDM, shared and independent disks, SR-IOV NICs

> Can you update vcsim-lab repo with various settings so I can use
> them to test new features in preflight check
> (then, on the scope question: "Add those, plus try RDM / shared /
> SR-IOV")

Outcome: the lab now varies every setting preflight checks that vcsim
can express. First, live experiments against a scratch vcsim (one test
VM per feature, all five accepted and reading back intact): `disk.EnableUUID`
in extraConfig, `diskMode: independent-nonpersistent`, `sharing:
sharingMultiWriter`, an RDM `RawDiskMappingVer1BackingInfo` disk
(physical compatibility, fake NAA deviceName), and a
`VirtualSriovEthernetCard` (empty SriovBackingInfo — vcsim serves no
host PCI devices to bind a physical function to).

(1) New per-VM attribute `disk_enable_uuid` (true | false | random,
default random), following the cbt/fast_boot pattern end to end:
`VmSpec` + validation, `ResolvedAssignment` (defaulted False so older
constructions keep working) + `to_dict`, `resolve_random_assignments`
— drawn LAST, after tools_status, so the sequential RNG does not
reshuffle any earlier draw (pinned by a test) — `build_create_spec`
stamps `disk.EnableUUID` into extraConfig as explicit TRUE/FALSE on
every VM (a user `extra_config` entry wins via setdefault), query
reports the live value, verify checks it as a core check (skipped when
the live VM has no key — built before the feature). With seed 42 the
lab lands at 12/23 on; `win2022-sql` and `rhel9-db` set it explicitly
true, `dev-win10` too. (2) Per-disk `backing: vmdk | rdm` +
`rdm_compatibility` (physical/virtual): `freebsd13-nas`' third disk
is an RDM (physical compatibility; deviceName is a deterministic fake
NAA id from the VM name + disk index via `devices.rdm_device_name`).
RDM disks ignore thin/eager and have no diskMode. (3) Per-disk
`shared: true` (multi-writer): `sles15-sap` gained a 10 GB quorum disk
— the lab is now 43 disks. (4) Per-disk `disk_mode` is now validated
against the three real modes; `centos8-legacy`'s data disk is
`independent-nonpersistent`. (5) Per-NIC `sriov: true` builds a
VirtualSriovEthernetCard (`opnsense-fw`'s WAN NIC; `type` must not be
combined with it). Query reports backing / rdm_compatibility /
rdm_device_name / shared per disk and sriov per NIC; verify compares
the full disk tuple and the NIC device types (with a None-safe sort
key — RDM disks serve thin/diskMode as None, and a plain `sorted()`
crashes comparing None to bool, found by the live verify run).

Two pyVmomi gotchas, both fixed here and in preflight: class
`__name__`s are fully dotted (`vim.vm.device.VirtualDisk.…`), so a
short-name match never fires — RDM detection now uses `isinstance`
in both repos (preflight's `disk-rdm` check could never fire before
this; see preflight PROMPTS entry 53), and `nic_device_class_name`
returns the dotted class name to match what query reports live.

Live-validated end to end: full local build 23 VMs / 91 steps / 0
failed, verify 0 failures (24 best-effort warnings: tools-status +
Secure Boot, as before), and a preflight assess against the local lab
fires every new check — disk-enable-uuid WARN on 11 VMs (the 12th is
a template), disk-mode BLOCK on centos8-legacy, disk-rdm BLOCK on
freebsd13-nas, disk-shared BLOCK on sles15-sap, nic-sriov WARN on
opnsense-fw. Note: the Render deployment rebuilds the lab from the
repo on deploy, so the live lab picks these settings up on the next
redeploy; a re-run of build_lab.py over an existing lab does NOT
retrofit extraConfig (no reconcile step — same as diskLayout).
298 tests (37 new in `tests/test_preflight_settings.py`, plus 2
assertions added to `tests/test_random_attrs.py`).

## 21. 2026-11-16 01:18 — Three more VMs: 2008 R2, XP x64, Photon

> Add three VMs to vcsim-lab, one with Microsoft Windows 2008 R2
> Server, the other winXPPro64Guest, and VMware Photon OS

Done. Lab is now 26 VMs / 46 disks (was 23 / 43), 13 BIOS / 13
EFI (was 11 / 12), snapshots still 16. The two legacy Windows
guests join centos8-legacy in lab/Lab/Legacy (now 3 VMs); Photon
is a container host in lab/Prod/Web (now 3 VMs):

- win2008r2-legacy — windows7Server64Guest, BIOS, vmx-19, 2 vCPU /
  4 GB, 60 GB thin lsi-logic disk, e1000, poweredOff, toolsOld.
- winxp64-legacy — winXPPro64Guest, BIOS, vmx-19, 1 vCPU / 2 GB,
  40 GB thin lsi-logic disk, e1000, poweredOff, toolsOld.
- photon-container — vmwarePhoton64Guest, EFI, vmx-20, 2 vCPU /
  4 GB, 50 GB thin nvme disk, vmxnet3 on prod, random power/tools.

windows7Server64Guest and winXPPro64Guest were added to
KNOWN_GUEST_IDS (vmwarePhoton64Guest was already there). Pinned
counts updated in 8 test files (26 VMs, 46 disks, folder map +
distribution, Prod subtree list, snapshot distribution 0:15/1:6/
2:5); one new test pins the three guest IDs, folders, firmware
and power states. Suite 299 passed. Live-verified: full build
26 VMs / 98 steps / 0 failed, verify 0 failures; query confirms
all three with the right guest IDs, folders and devices.
Preflight against the enlarged lab: warm 0/4/19, cold 0/10/13
over 23 assessed VMs — the two legacy Windows guests warn on
guest-os in both contexts (in neither list) and block warm
(powered off); Photon passes guest-os cold and warns warm (not
Cirrus-qualified), warn verdict in both.

## 22. 2026-11-16 09:05 — Four coverage VMs for untested preflight checks

> review the various vcenter cases in the "preflight" project, and
> assess the VMs created in the "vcsim-lab" project and let me know
> if we need more VMs with different configurations for testing

Coverage review first: of preflight's 34 VM-scope vCenter checks,
22 fired non-pass findings live and 12 always passed. Five of the
12 were fillable — vcsim was probed live to confirm it serves each
config (nestedHVEnabled=True read back; all five legacy device
classes accepted unbacked and served back; `ZZ_Test-VM` accepted
as a name). The other six are untestable in vcsim no matter the
VM: vm-encryption (CryptoManager is a stub with no EncryptVM_Task),
fault-tolerance (no API to set ftInfo), secure-boot BLOCK (vcsim
drops the flag), vm-arch x86/arm (no Tools arch data), tools-status
PASS (always toolsNotInstalled), Windows power checks +
cirrus-endpoints (no guest operations). firmware-type WARN is
unreachable by design (only fires on unrecognized firmware).

Done. Two new builder fields: `nested_virt:` (nestedHVEnabled in
the create spec) and `extra_devices:` (usb/serial/parallel/sound/
floppy, added post-create in one reconfig, skip-if-present on
re-runs; validated against EXTRA_DEVICE_TYPES, duplicates
rejected). Both flow through the query summary and the verifier
(core checks, skipped when the live summary predates the fields).
Lab is now 30 VMs / 52 disks, 15 BIOS / 15 EFI, snapshots still
16. The four new VMs:

- sap-hana-production-database-server-primary-001-eu-central-instance
  — 67-char valid name, 48 vCPU / 512 GB RAM, 3x1 TB disks,
  lab/Prod/DB, poweredOff. Fires cpu-memory-size (vCPU + RAM),
  disk-size, and vm-name (length branch).
- Win2019_FileServer — uppercase + underscore name, lab/Lab/Legacy.
  Fires vm-name (regex branch).
- kvm-nested-host — nested_virt: true, lab/Lab. Fires nested-virt.
- win10-legacy-desk — all five extra devices, lab/Prod/VDI. Fires
  unsupported-devices with all five labels.

18 new tests in tests/test_coverage_vms.py (config, create spec,
device builders, add_extra_devices idempotency, query summary,
verify, lab matrix); pinned counts updated in 7 files. Suite 317
passed. Live-verified: full build 30 VMs / 109 steps / 0 failed,
verify 0 failures; preflight cold assess confirms all five checks
fire (7 warn findings across the four VMs), counts now 0/12/15
over 27 assessed VMs.

## 23. Clean-room VM (2026-11-16)

Prompt: "in vcsim-lab, can you create a VM that has no block, warning
at all, so when I use --ignore-host-transport, i expect this VM to
pass"

New VM `clean-rhel9` (own "Clean-room" section at the end of
environments/default/vms.yaml): RHEL 9, BIOS, 2 vCPU / 4 GB, one
40 GB thin persistent VMDK, one E1000 NIC on the standard
`vm-network`, poweredOff, no CD-ROM, no snapshots, no TPM, no
Secure Boot, no SR-IOV, no extra devices, no vApp, no hot-add, no
nested virt, no 3D video, disk.EnableUUID false, cbt false.
Guest `rhel9_64Guest` is in both preflight's KubeVirt list and the
Cirrus list, so guest-os passes under either context.

Honest limit, found by reading the checks before building: a
zero-warning VM is impossible against vcsim. `tools-status` only
passes on toolsOk, and vcsim reports toolsNotInstalled for every VM
(Tools status is guest-reported with no setter API); `vm-arch` only
passes on a recorded x86 arch, and vcsim serves no Tools data, so
arch is always unrecorded (verified: all 30 lab VMs read
toolsNotInstalled / arch null). Both warnings are vcsim artifacts —
on a real vCenter with Tools running, this VM passes clean. The VM
declares toolsOk (sixth VM to do so; header comment updated).

One VM also cannot pass both contexts: cold wants poweredOff,
warm wants a running VM with CBT on. clean-rhel9 is the cold
control VM; a warm twin would need power_state poweredOn + cbt
true.

Live-verified (local vcsim, cold assess, --ignore-host-transport):
28 pass, 1 info (vm-appliance), 1 skip (vddk-port-902), and only
the two vcsim-artifact warnings (vm-arch, tools-status) — verdict
warn on those two alone. Warm assess blocks only on cirrus-cbt and
cirrus-power-state, as designed.

Lab is now 31 VMs / 53 disks, 16 BIOS / 15 EFI, snapshots still
16. Count pins updated in 7 test files, plus a new
test_clean_room_vm_passes_every_vm_check pinning the VM's clean
attributes. Suite 318 passed. README counts refreshed (were stale
at 23 VMs). Full build 31 VMs / 111 steps / 0 failed, verify 0
failures (5 Secure Boot + 27 tools-status warnings); saved model
re-saved and restart round-trip verified (31 VMs restored).

## 24. clean-rhel9 annotation: zero warnings now (2026-11-16)

Prompt (in the preflight chat): "clean-rhel9 still has two
warnings. (tool-status and vm-arch). Can you fix it some this VM
will have no warnings?"

The fix landed in preflight (entry 86): preflight detects the
simulator from AboutInfo.fullName and reports tools-status and
vm-arch as INFO against vcsim instead of WARN. clean-rhel9 now
passes with 28 pass / 3 info / 1 skip / 0 warnings, live-verified
against the local lab. This entry updates the lab's own words,
which said the two warnings were unavoidable: the Clean-room
section comment and the VM annotation in
environments/default/vms.yaml, and the comment in
test_clean_room_vm_passes_every_vm_check. No lab attributes
changed — vcsim still reports no Tools data; only preflight's
reading of it changed. The live VM's annotation was updated in
place via ReconfigVM_Task and the model re-saved, so the running
lab matches the YAML without a rebuild.

CORRECTION to entry 24 (same day): the model re-save did NOT
work as described. Saving from a vcsim that was itself loaded
from a model made govc object.save write most objects twice
(539 files), and the reloaded vcsim died on the first duplicate
Datastore ("mkdir .../DC0-LocalDS_0: file exists"). The lab was
recovered by deduping the model (keep the lowest-numbered file
per MoRef — the first copy is the complete one; later copies
can miss properties), and the restart round-trip then verified
clean (31 VMs, 0 failures, 32 warnings, clean-rhel9 passes).
The stale vm-271/275/279 references in the container view are
unrelated: vcsim's deleted stock VMs, still listed in a
resource pool since the original build. See entry 25 for the
permanent fix.

## 25. save-model.sh dedupes the saved model (2026-11-16)

Follow-up to entry 24's broken re-save. govc object.save from a
model-loaded vcsim writes most objects twice (same MoRef,
adjacent sequence numbers — the loader registers them under two
paths and govc's traversal sees both); a model with duplicate
Datastore files cannot load (the loader mkdirs the same
datastore directory twice and aborts). save-model.sh now runs a
dedupe pass after the save: keep the lowest-numbered file per
MoRef (the first copy is the complete one — later copies can
miss properties, e.g. a network's name), delete the rest, and
report how many were removed. Verified end to end: re-saved
from the running (model-loaded) vcsim, 220 duplicates removed,
319 files, restart loads clean, verify 31 VMs / 0 failures /
32 warnings. Note govc object.save -f wipes files but not the
old per-object subdirectories in the model dir (harmless —
the loader ignores them).

## 26. Workflow tags use the Docker Hub secret; pipeline verified live (2026-11-16)

Bing updated .github/workflows/docker-publish.yml directly on
GitHub (commit 870b0d7, "Update Docker tags to use GitHub
secrets"): the image tags are now
`${{ secrets.DOCKERHUB_USERNAME }}/vcsim-lab:latest` and
`:${{ github.sha }}` instead of the hardcoded `squarebing/`
prefix. The local copy was synced down from GitHub afterwards
(the connector cannot push workflow files, so GitHub is the
source of truth for this file).

The run on that commit (44020953815) is green on every step,
and the Render lab was verified current the same evening:
31 VMs including clean-rhel9 (entry 23), and win2022-sql reads
disk_enable_uuid True — the deployed image is no longer the
pre-entry-20 build. The CI/CD pipeline (push to main -> build
-> Docker Hub -> Render deploy hook) is live end to end; the
earlier "first run failed at the Render step" caveat no longer
applies.
