"""Offline tests for the host storage model: no vCenter needed.

Run from the project root:

    python -m pytest tests/ -q
    # or without pytest:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import storage  # noqa: E402

STORAGE_YAML = os.path.join(os.path.dirname(__file__), "..", "storage.yaml")


def _base_model() -> dict:
    """A minimal valid model, for validation tests."""
    return {
        "arrays": [
            {
                "id": "arr-fc",
                "vendor": "Vendor A",
                "model": "Model X",
                "protocol": "fc",
                "controllers": ["C0"],
                "ports": [{"name": "C0.FC0", "wwpn": "50:0a:09:80:00:01:02:03"}],
                "luns": [
                    {
                        "id": "lun-0",
                        "label": "vol-0",
                        "naa": "naa.624a937001000001",
                        "capacity_gb": 100,
                        "datastore": "LocalDS_0",
                    }
                ],
            }
        ],
        "hosts": {
            "host-1": {
                "hbas": [
                    {
                        "device": "vmhba2",
                        "type": "fc",
                        "model": "HBA",
                        "driver": "lpfc",
                        "wwpn": "10:00:00:10:9b:01:02:11",
                    }
                ]
            }
        },
    }


def _load(raw: dict) -> storage.StorageModel:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(raw, fh)
        path = fh.name
    try:
        return storage.load_storage(path)
    finally:
        os.unlink(path)


def _fake_live() -> dict:
    """Fake live inventory shaped like storage.live_storage() output."""
    return {
        "hosts": [
            {
                "name": "host-1",
                "software_iscsi_enabled": False,
                "hbas": [
                    {
                        "device": "vmhba0",
                        "type": "ParallelScsiHba",
                        "model": "PVSCSI SCSI Controller",
                        "driver": "pvscsi",
                        "pci": "0000:03:00.0",
                        "bus": 3,
                        "status": "unknown",
                    }
                ],
                "luns": [
                    {
                        "canonical_name": "mpx.vmhba0:C0:T0:L0",
                        "device_name": "/vmfs/devices/disks/mpx.vmhba0:C0:T0:L0",
                        "type": "disk",
                        "vendor": "VMware,",
                        "model": "VMware Virtual S",
                        "revision": "1.0",
                        "uuid": "0000000000766d686261303a303a30",
                        "display_name": "Local VMware, Disk",
                        "descriptors": ["mpx.vmhba0:C0:T0:L0"],
                        "operational_state": ["ok"],
                        "capacity_gb": 32.0,
                        "ssd": True,
                        "local_disk": True,
                    }
                ],
                "multipath": [],
            },
            {
                "name": "host-live-only",
                "software_iscsi_enabled": False,
                "hbas": [],
                "luns": [],
                "multipath": [],
            },
        ],
        "datastores": [
            {
                "name": "LocalDS_0",
                "type": "OTHER",
                "info_type": "LocalDatastoreInfo",
                "capacity_gb": 4096.0,
                "free_gb": 4096.0,
                "url": "/tmp/govcsim-x/DC0-LocalDS_0",
                "mounts": [
                    {
                        "host": "host-1",
                        "access_mode": "readWrite",
                        "mounted": True,
                        "accessible": True,
                    }
                ],
                "extents": None,
            }
        ],
    }


class TestStorageModel(unittest.TestCase):
    def test_shipped_model_loads(self):
        model = storage.load_storage(STORAGE_YAML)
        self.assertEqual(len(model.arrays), 2)
        self.assertEqual(len(model.all_luns()), 3)
        self.assertEqual(len(model.hosts), 4)
        self.assertEqual(model.warnings, [])

    def test_shipped_model_protocols(self):
        model = storage.load_storage(STORAGE_YAML)
        protocols = {a.protocol for a in model.arrays}
        self.assertEqual(protocols, {"fc", "iscsi"})
        fc = [a for a in model.arrays if a.protocol == "fc"][0]
        self.assertEqual(len(fc.luns), 2)
        iscsi = [a for a in model.arrays if a.protocol == "iscsi"][0]
        self.assertIsNotNone(iscsi.iqn)

    def test_lun_datastore_mapping(self):
        model = storage.load_storage(STORAGE_YAML)
        self.assertEqual(model.lun_for_datastore("LocalDS_0").id, "fa-lun-00")
        self.assertEqual(model.lun_for_datastore("LocalDS_1").id, "fa-lun-01")
        self.assertEqual(model.lun_for_datastore("LocalDS_2").id, "ps-lun-00")
        self.assertEqual(model.array_for_datastore("LocalDS_0").id, "flash-array-01")
        self.assertEqual(model.array_for_datastore("LocalDS_2").id, "iscsi-array-01")
        self.assertIsNone(model.lun_for_datastore("NoSuchDS"))

    def test_luns_visible_to(self):
        model = storage.load_storage(STORAGE_YAML)
        for host_name in ("DC0_C0_H0", "DC0_C0_H1", "DC0_C0_H2", "DC0_H0"):
            visible = model.luns_visible_to(host_name)
            self.assertEqual(len(visible), 3)
            self.assertEqual(
                {lun.id for _, lun in visible}, {"fa-lun-00", "fa-lun-01", "ps-lun-00"}
            )
        self.assertEqual(model.luns_visible_to("NoSuchHost"), [])

    def test_host_with_only_fc_hba_sees_only_fc_luns(self):
        raw = _base_model()
        raw["hosts"]["host-1"]["hbas"] = [
            {
                "device": "vmhba2",
                "type": "fc",
                "wwpn": "10:00:00:10:9b:01:02:11",
            }
        ]
        raw["arrays"].append(
            {
                "id": "arr-iscsi",
                "vendor": "Vendor B",
                "model": "Model Y",
                "protocol": "iscsi",
                "controllers": ["N0"],
                "iqn": "iqn.2015-10.com.vendor:array",
                "ports": [{"name": "N0.eth0", "portal": "10.0.0.1:3260"}],
                "luns": [
                    {
                        "id": "lun-1",
                        "naa": "naa.6000097000000001",
                        "capacity_gb": 50,
                        "datastore": "LocalDS_1",
                    }
                ],
            }
        )
        model = _load(raw)
        visible = model.luns_visible_to("host-1")
        self.assertEqual([lun.id for _, lun in visible], ["lun-0"])


class TestStorageValidation(unittest.TestCase):
    def test_bad_naa(self):
        raw = _base_model()
        raw["arrays"][0]["luns"][0]["naa"] = "naa.123"
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_bad_wwpn(self):
        raw = _base_model()
        raw["hosts"]["host-1"]["hbas"][0]["wwpn"] = "10:00:00:10:9b:01:02"
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_bad_iqn(self):
        raw = _base_model()
        raw["hosts"]["host-1"]["hbas"][0] = {
            "device": "vmhba33",
            "type": "iscsi",
            "iqn": "not-an-iqn",
        }
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_duplicate_naa(self):
        raw = _base_model()
        raw["arrays"][0]["luns"].append(
            {
                "id": "lun-1",
                "naa": "naa.624a937001000001",
                "capacity_gb": 10,
                "datastore": "LocalDS_1",
            }
        )
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_duplicate_datastore_mapping(self):
        raw = _base_model()
        raw["arrays"][0]["luns"].append(
            {
                "id": "lun-1",
                "naa": "naa.624a937001000002",
                "capacity_gb": 10,
                "datastore": "LocalDS_0",
            }
        )
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_duplicate_hba_device(self):
        raw = _base_model()
        raw["hosts"]["host-1"]["hbas"].append(
            {
                "device": "vmhba2",
                "type": "fc",
                "wwpn": "10:00:00:10:9b:01:02:12",
            }
        )
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_fc_hba_requires_wwpn(self):
        raw = _base_model()
        del raw["hosts"]["host-1"]["hbas"][0]["wwpn"]
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_iscsi_hba_requires_iqn(self):
        raw = _base_model()
        raw["hosts"]["host-1"]["hbas"][0] = {"device": "vmhba33", "type": "iscsi"}
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_bad_protocol(self):
        raw = _base_model()
        raw["arrays"][0]["protocol"] = "nvmeof"
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_duplicate_array_id(self):
        raw = _base_model()
        raw["arrays"].append(copy.deepcopy(raw["arrays"][0]))
        with self.assertRaises(storage.StorageError):
            _load(raw)

    def test_missing_file(self):
        with self.assertRaises(storage.StorageError):
            storage.load_storage("/nonexistent/storage.yaml")


class TestReconcile(unittest.TestCase):
    def test_mounts_get_backing_lun(self):
        model = _load(_base_model())
        report = storage.reconcile(model, _fake_live())
        host = [h for h in report["hosts"] if h["name"] == "host-1"][0]
        self.assertEqual(len(host["mounts"]), 1)
        mount = host["mounts"][0]
        self.assertEqual(mount["datastore"], "LocalDS_0")
        self.assertEqual(mount["backing_lun"], "lun-0")
        self.assertEqual(mount["backing_naa"], "naa.624a937001000001")
        self.assertEqual(mount["array"], "arr-fc")
        # No VMFS extents live, so the backing is declared, not verified.
        self.assertFalse(mount["backing_verified_live"])

    def test_declared_hba_not_live_is_noted(self):
        model = _load(_base_model())
        report = storage.reconcile(model, _fake_live())
        host = [h for h in report["hosts"] if h["name"] == "host-1"][0]
        self.assertTrue(any("vmhba2" in n and "not present live" in n for n in host["notes"]))
        self.assertEqual(host["declared_luns"][0]["lun"], "lun-0")
        self.assertFalse(host["declared_luns"][0]["visible_live"])

    def test_live_local_luns_flagged(self):
        model = _load(_base_model())
        report = storage.reconcile(model, _fake_live())
        host = [h for h in report["hosts"] if h["name"] == "host-1"][0]
        lun = host["live_luns"][0]
        self.assertEqual(lun["canonical_name"], "mpx.vmhba0:C0:T0:L0")
        self.assertFalse(lun["array_lun"])
        self.assertIn("local device", lun["note"])

    def test_host_only_on_one_side_is_noted(self):
        model = _load(_base_model())
        report = storage.reconcile(model, _fake_live())
        live_only = [h for h in report["hosts"] if h["name"] == "host-live-only"][0]
        self.assertTrue(any("not in storage.yaml" in n for n in live_only["notes"]))
        self.assertEqual(live_only["declared_hbas"], [])
        self.assertEqual(live_only["declared_luns"], [])

    def test_global_notes_cover_vcsim_limits(self):
        model = _load(_base_model())
        report = storage.reconcile(model, _fake_live())
        joined = " ".join(report["notes"])
        self.assertIn("no Fibre Channel HBAs served live", joined)
        self.assertIn("no iSCSI HBAs served live", joined)
        self.assertIn("no VMFS extents live", joined)

    def test_vmfs_extents_verify_backing(self):
        model = _load(_base_model())
        live = _fake_live()
        live["datastores"][0]["type"] = "VMFS"
        live["datastores"][0]["info_type"] = "VmfsDatastoreInfo"
        live["datastores"][0]["extents"] = ["naa.624a937001000001"]
        report = storage.reconcile(model, live)
        host = [h for h in report["hosts"] if h["name"] == "host-1"][0]
        self.assertTrue(host["mounts"][0]["backing_verified_live"])
        self.assertNotIn("no VMFS extents live", " ".join(report["notes"]))

    def test_datastore_missing_live_is_noted(self):
        model = _load(_base_model())
        live = _fake_live()
        live["datastores"] = []
        report = storage.reconcile(model, live)
        self.assertTrue(
            any("LocalDS_0" in n and "does not exist live" in n for n in report["notes"])
        )


if __name__ == "__main__":
    unittest.main()
