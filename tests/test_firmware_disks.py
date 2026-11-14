"""Offline tests for the firmware mix, multi-disk layout, and the
basic/dynamic disk model: no vCenter needed. Run from the project root:

    python -m pytest tests/ -q
    # or without pytest:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pyVmomi import vim  # noqa: E402

from vcsim_lab import config, specs, verify  # noqa: E402

LAB = os.path.join(os.path.dirname(__file__), "..", "environments", "default", "vms.yaml")

MINIMAL_VM = {
    "name": "x",
    "guest_id": "ubuntu64Guest",
    "firmware": "efi",
    "disks": [{"size_gb": 10}],
    "nics": [{"network": "prod"}],
}

WINDOWS_VM = dict(MINIMAL_VM, guest_id="windows2019srv_64Guest")


def _load_vm(base=None, **overrides):
    raw = dict(base or MINIMAL_VM)
    raw.update(overrides)
    return config._load_vm(raw, [])


def _vm_spec(**overrides):
    base = dict(
        name="test-vm",
        guest_id="ubuntu64Guest",
        cpus=4,
        cores_per_socket=2,
        memory_mb=8192,
        hardware_version="vmx-20",
        firmware="efi",
        disks=[config.DiskSpec(size_gb=100, controller="nvme", datastore="LocalDS_0")],
        nics=[config.NicSpec(network="prod", type="vmxnet3")],
        boot_order=["disk"],
    )
    base.update(overrides)
    return config.VmSpec(**base)


def _windows_spec(**overrides):
    base = dict(
        name="win-test",
        guest_id="windows2019srv_64Guest",
        disks=[
            config.DiskSpec(size_gb=100, controller="pvscsi", datastore="LocalDS_0"),
            config.DiskSpec(
                size_gb=500, controller="pvscsi", datastore="LocalDS_1", disk_type="dynamic"
            ),
            config.DiskSpec(
                size_gb=500, controller="pvscsi", datastore="LocalDS_2", disk_type="dynamic"
            ),
        ],
        volumes=[config.VolumeSpec(name="Data", kind="mirrored", disks=[2, 3])],
    )
    base.update(overrides)
    return _vm_spec(**base)


class TestDiskValidation(unittest.TestCase):
    def test_dynamic_disk_on_linux_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(disks=[{"size_gb": 10, "disk_type": "dynamic"}])

    def test_dynamic_disk_on_windows_ok(self):
        vm = _load_vm(WINDOWS_VM, disks=[{"size_gb": 10, "disk_type": "dynamic"}])
        self.assertEqual(vm.disks[0].disk_type, "dynamic")

    def test_bad_disk_type_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(WINDOWS_VM, disks=[{"size_gb": 10, "disk_type": "fancy"}])

    def test_default_disk_type_is_basic(self):
        vm = _load_vm()
        self.assertEqual(vm.disks[0].disk_type, "basic")
        self.assertEqual(vm.volumes, [])

    def test_secure_boot_on_bios_rejected(self):
        # TPM-on-BIOS is covered by test_specs.TestConfig.test_tpm_requires_efi.
        with self.assertRaises(config.ConfigError):
            _load_vm(firmware="bios", secure_boot=True)

    def test_windows_guest_detection(self):
        self.assertTrue(config.is_windows_guest("windows11_64Guest"))
        self.assertTrue(config.is_windows_guest("windows2019srv_64Guest"))
        self.assertTrue(config.is_windows_guest("windows9_64Guest"))
        self.assertFalse(config.is_windows_guest("ubuntu64Guest"))
        self.assertFalse(config.is_windows_guest("rhel9_64Guest"))


class TestVolumeValidation(unittest.TestCase):
    def _win_disks(self, count=3):
        disks = [{"size_gb": 100}]
        disks += [{"size_gb": 500, "disk_type": "dynamic"} for _ in range(count - 1)]
        return disks

    def test_spanned_needs_two_disks(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(2),
                volumes=[{"name": "V", "kind": "spanned", "disks": [2]}],
            )

    def test_striped_needs_two_disks(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(2),
                volumes=[{"name": "V", "kind": "striped", "disks": [2]}],
            )

    def test_mirrored_needs_two_disks(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(2),
                volumes=[{"name": "V", "kind": "mirrored", "disks": [2]}],
            )

    def test_simple_needs_exactly_one_disk(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(3),
                volumes=[{"name": "V", "kind": "simple", "disks": [2, 3]}],
            )

    def test_volume_disk_must_be_dynamic(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(3),
                volumes=[{"name": "V", "kind": "simple", "disks": [1]}],
            )

    def test_volume_disk_index_out_of_range(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(2),
                volumes=[{"name": "V", "kind": "simple", "disks": [3]}],
            )
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(2),
                volumes=[{"name": "V", "kind": "simple", "disks": [0]}],
            )

    def test_duplicate_volume_name_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(3),
                volumes=[
                    {"name": "V", "kind": "simple", "disks": [2]},
                    {"name": "V", "kind": "simple", "disks": [3]},
                ],
            )

    def test_disk_in_two_volumes_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(3),
                volumes=[
                    {"name": "A", "kind": "simple", "disks": [2]},
                    {"name": "B", "kind": "simple", "disks": [2]},
                ],
            )

    def test_bad_volume_kind_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                WINDOWS_VM,
                disks=self._win_disks(3),
                volumes=[{"name": "V", "kind": "raid5", "disks": [2, 3]}],
            )

    def test_valid_volumes_load(self):
        vm = _load_vm(
            WINDOWS_VM,
            disks=self._win_disks(3),
            volumes=[{"name": "Data", "kind": "mirrored", "disks": [2, 3]}],
        )
        self.assertEqual(len(vm.volumes), 1)
        self.assertEqual(vm.volumes[0].name, "Data")
        self.assertEqual(vm.volumes[0].kind, "mirrored")
        self.assertEqual(vm.volumes[0].disks, [2, 3])


class TestDiskLayoutModel(unittest.TestCase):
    def test_layout_summary(self):
        vm = _windows_spec()
        summary = config.disk_layout_summary(vm)
        self.assertTrue(summary["has_dynamic"])
        self.assertEqual(
            summary["disks"],
            [
                {"index": 1, "disk_type": "basic", "volume": None, "volume_kind": None},
                {"index": 2, "disk_type": "dynamic", "volume": "Data", "volume_kind": "mirrored"},
                {"index": 3, "disk_type": "dynamic", "volume": "Data", "volume_kind": "mirrored"},
            ],
        )
        self.assertEqual(
            summary["volumes"], [{"name": "Data", "kind": "mirrored", "disks": [2, 3]}]
        )

    def test_extra_config_string(self):
        vm = _windows_spec()
        self.assertEqual(
            config.disk_layout_extra_config(vm),
            "basic; dynamic:Data:mirrored; dynamic:Data:mirrored",
        )

    def test_extra_config_none_for_basic_vm(self):
        self.assertIsNone(config.disk_layout_extra_config(_vm_spec()))
        summary = config.disk_layout_summary(_vm_spec())
        self.assertFalse(summary["has_dynamic"])
        self.assertEqual(summary["volumes"], [])

    def test_dynamic_disk_without_volume(self):
        vm = _windows_spec(volumes=[])
        self.assertEqual(config.disk_layout_extra_config(vm), "basic; dynamic; dynamic")
        summary = config.disk_layout_summary(vm)
        self.assertEqual(summary["disks"][1]["volume"], None)

    def test_lab_dynamic_layouts(self):
        lab = config.load_lab(LAB)
        self.assertEqual(
            config.disk_layout_extra_config(lab.vm("win2022-sql")),
            "basic; dynamic:SQLData:mirrored; dynamic:SQLData:mirrored",
        )
        self.assertEqual(
            config.disk_layout_extra_config(lab.vm("win2019-fileserver")),
            "basic; dynamic:FileShare:spanned; dynamic:FileShare:spanned",
        )


class TestSpecBuilding(unittest.TestCase):
    def _refs(self):
        net = MagicMock(spec=vim.Network)
        net.name = "VM Network"
        return specs.ResolvedRefs(
            folder=MagicMock(), resource_pool=MagicMock(), datastores={},
            networks={"prod": net}, network_types={"prod": "standard"},
        )

    def _extra(self, cfg):
        return {opt.key: opt.value for opt in (cfg.extraConfig or [])}

    def test_create_spec_stamps_disk_layout(self):
        spec = _windows_spec()
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs())
        self.assertEqual(
            self._extra(cfg).get("lab.diskLayout"),
            "basic; dynamic:Data:mirrored; dynamic:Data:mirrored",
        )

    def test_create_spec_no_layout_for_basic_vm(self):
        spec = _vm_spec()
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs())
        self.assertNotIn("lab.diskLayout", self._extra(cfg))


class TestLabMatrix(unittest.TestCase):
    def test_lab_counts(self):
        lab = config.load_lab(LAB)
        self.assertEqual(len(lab.vms), 22)

    def test_firmware_mix_is_balanced(self):
        # A future edit must not silently unbalance the mix: both
        # firmwares well represented (10 BIOS / 12 EFI at last count).
        lab = config.load_lab(LAB)
        bios = [v for v in lab.vms if v.firmware == "bios"]
        efi = [v for v in lab.vms if v.firmware == "efi"]
        self.assertGreaterEqual(len(bios), 9)
        self.assertGreaterEqual(len(efi), 9)
        self.assertEqual(len(bios) + len(efi), len(lab.vms))

    def test_tpm_and_secure_boot_stay_efi(self):
        lab = config.load_lab(LAB)
        for vm in lab.vms:
            if vm.tpm or vm.secure_boot:
                self.assertEqual(vm.firmware, "efi", vm.name)
        # Windows 11 requires EFI too (TPM + Secure Boot are mandatory).
        for vm in lab.vms:
            if vm.guest_id == "windows11_64Guest":
                self.assertEqual(vm.firmware, "efi", vm.name)

    def test_templates_span_both_firmwares(self):
        lab = config.load_lab(LAB)
        firmwares = {v.firmware for v in lab.vms if v.template}
        self.assertEqual(firmwares, {"bios", "efi"})

    def test_multi_disk_mix(self):
        lab = config.load_lab(LAB)
        multi = [v for v in lab.vms if len(v.disks) >= 2]
        self.assertGreaterEqual(len(multi), 11)
        self.assertTrue(any(len(v.disks) >= 3 for v in multi))
        # ... but single-disk VMs still exist too.
        self.assertTrue(any(len(v.disks) == 1 for v in lab.vms))

    def test_dynamic_disk_mix(self):
        lab = config.load_lab(LAB)
        dynamic_vms = [v for v in lab.vms if any(d.disk_type == "dynamic" for d in v.disks)]
        self.assertEqual(len(dynamic_vms), 4)
        # All four volume kinds are represented.
        kinds = {vol.kind for v in dynamic_vms for vol in v.volumes}
        self.assertEqual(kinds, {"simple", "spanned", "striped", "mirrored"})
        # Dynamic disks are Windows-only, and every volume's disks are dynamic.
        for vm in dynamic_vms:
            self.assertTrue(config.is_windows_guest(vm.guest_id), vm.name)
            for vol in vm.volumes:
                for idx in vol.disks:
                    self.assertEqual(vm.disks[idx - 1].disk_type, "dynamic")


def _live(**overrides):
    base = {
        "name": "win-test",
        "guest_id": "windows2019srv_64Guest",
        "cpus": 4,
        "memory_mb": 8192,
        "firmware": "efi",
        "hardware_version": "vmx-20",
        "power": "poweredOff",
        "boot_order": ["disk"],
        "template": False,
        "vapp": None,
        "disks": [
            {"capacity_gb": 100, "datastore": "LocalDS_0", "thin": True},
            {"capacity_gb": 500, "datastore": "LocalDS_1", "thin": True},
            {"capacity_gb": 500, "datastore": "LocalDS_2", "thin": True},
        ],
        "nics": [{"label": "Network adapter 1"}],
        "cdrom": False,
        "video_3d": False,
        "tpm": False,
        "secure_boot": False,
        "gpus": [],
        "cbt": None,
        "boot_delay_ms": None,
        "tools_status": None,
        "disk_layout": "basic; dynamic:Data:mirrored; dynamic:Data:mirrored",
    }
    base.update(overrides)
    return base


class TestVerifyDiskLayout(unittest.TestCase):
    def test_matching_layout_passes(self):
        failures, warnings = verify.verify_vm(_windows_spec(), _live())
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_mismatched_layout_fails(self):
        failures, _ = verify.verify_vm(
            _windows_spec(), _live(disk_layout="basic; dynamic; dynamic")
        )
        self.assertTrue(any("disk_layout" in f for f in failures))

    def test_missing_layout_warns_not_fails(self):
        failures, warnings = verify.verify_vm(_windows_spec(), _live(disk_layout=None))
        self.assertEqual(failures, [])
        self.assertTrue(any("disk_layout" in w for w in warnings))

    def test_basic_vm_has_no_layout_check(self):
        failures, warnings = verify.verify_vm(
            _vm_spec(), _live(name="test-vm", guest_id="ubuntu64Guest", disk_layout=None,
                              disks=[{"capacity_gb": 100, "datastore": "LocalDS_0", "thin": True}])
        )
        self.assertEqual(failures, [])
        self.assertFalse(any("disk_layout" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
