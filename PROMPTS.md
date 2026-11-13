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
