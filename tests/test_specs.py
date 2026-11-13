"""Offline tests: no vCenter needed. Run from the project root:

    python -m pytest tests/ -q
    # or without pytest:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pyVmomi import vim  # noqa: E402

from vcsim_lab import config, devices, specs, vcenter  # noqa: E402

LAB = os.path.join(os.path.dirname(__file__), "..", "vms.yaml")


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


class TestConfig(unittest.TestCase):
    def test_lab_loads(self):
        lab = config.load_lab(LAB)
        self.assertEqual(len(lab.vms), 21)
        self.assertEqual(lab.datacenter, "DC0")
        self.assertEqual(lab.cluster, "DC0_C0")

    def test_lab_covers_the_matrix(self):
        lab = config.load_lab(LAB)
        guest_ids = {v.guest_id for v in lab.vms}
        self.assertGreaterEqual(len(guest_ids), 8)
        firmwares = {v.firmware for v in lab.vms}
        self.assertEqual(firmwares, {"bios", "efi"})
        self.assertTrue(any(v.tpm for v in lab.vms))
        self.assertTrue(any(v.gpu and v.gpu.mode == "passthrough" for v in lab.vms))
        self.assertTrue(any(v.gpu and v.gpu.mode == "vgpu" for v in lab.vms))
        self.assertTrue(any(v.gpu and v.gpu.mode == "dynamic" for v in lab.vms))
        gpu_vendors = {v.gpu.vendor for v in lab.vms if v.gpu}
        self.assertEqual(gpu_vendors, {"nvidia", "amd"})
        self.assertEqual(len([v for v in lab.vms if v.template]), 3)
        self.assertEqual(len([v for v in lab.vms if v.vapp is not None]), 2)
        controllers = {d.controller for v in lab.vms for d in v.disks}
        self.assertTrue({"nvme", "pvscsi", "lsi-sas", "lsi-logic"} <= controllers)
        nic_types = {n.type for v in lab.vms for n in v.nics}
        self.assertTrue({"vmxnet3", "e1000", "e1000e"} <= nic_types)
        boots = {tuple(v.boot_order) for v in lab.vms}
        self.assertIn(("network", "disk"), boots)  # PXE-first
        self.assertIn(("cdrom", "disk"), boots)  # install-media-first
        self.assertTrue(any(len(v.disks) >= 3 for v in lab.vms))
        self.assertTrue(any(len(v.nics) >= 2 for v in lab.vms))

    def test_hex_ids(self):
        self.assertEqual(config.parse_hex_id("0x10de", "vendor"), 0x10DE)
        self.assertEqual(config.parse_hex_id("0x1eb8", "device"), 0x1EB8)
        self.assertEqual(config.parse_hex_id(4318, "vendor"), 4318)
        with self.assertRaises(config.ConfigError):
            config.parse_hex_id("not-a-number", "vendor")

    def test_signed_short(self):
        self.assertEqual(config.to_signed_short(0x10DE), 4318)
        # 0x1EB8 = 7864, still positive; 0x8xxx values go negative.
        self.assertEqual(config.to_signed_short(0x8086), -32634)
        self.assertEqual(config.to_signed_short(0xFFFF), -1)

    def test_tpm_requires_efi(self):
        with self.assertRaises(config.ConfigError):
            config._load_vm(
                {"name": "x", "guest_id": "ubuntu64Guest", "firmware": "bios", "tpm": True,
                 "disks": [{"size_gb": 10}], "nics": [{"network": "prod"}]},
                [],
            )


class TestPlan(unittest.TestCase):
    def test_keys_unique_and_controllers_resolve(self):
        lab = config.load_lab(LAB)
        for vm in lab.vms:
            plan = specs.plan_vm(vm)
            keys = [c.key for c in plan.controllers]
            keys += [d.key for d in plan.disks]
            keys += [n.key for n in plan.nics]
            if plan.cdrom_key is not None:
                keys.append(plan.cdrom_key)
            self.assertEqual(len(keys), len(set(keys)), f"{vm.name}: duplicate temp keys")
            for disk in plan.disks:
                self.assertIn(disk.controller_key, [c.key for c in plan.controllers])
            if plan.cdrom_key is not None:
                self.assertIn(plan.cdrom_controller_key, [c.key for c in plan.controllers])

    def test_scsi_skips_unit_7(self):
        spec = _vm_spec(
            disks=[config.DiskSpec(size_gb=10, controller="pvscsi") for _ in range(9)],
        )
        plan = specs.plan_vm(spec)
        units = [d.unit_number for d in plan.disks]
        self.assertNotIn(7, units)
        self.assertEqual(units, [0, 1, 2, 3, 4, 5, 6, 8, 9])

    def test_cdrom_gets_ahci_controller(self):
        spec = _vm_spec(cdrom=config.CdromSpec())
        plan = specs.plan_vm(spec)
        kinds = [c.kind for c in plan.controllers]
        self.assertIn("ahci", kinds)
        self.assertIsNotNone(plan.cdrom_controller_key)

    def test_multi_disk_multi_controller(self):
        lab = config.load_lab(LAB)
        vm = lab.vm("rhel9-db")
        plan = specs.plan_vm(vm)
        self.assertEqual(len(plan.disks), 3)
        # 1 pvscsi controller for the disks + 1 ahci controller for the CD-ROM
        self.assertEqual(len(plan.controllers), 2)
        self.assertEqual([d.unit_number for d in plan.disks], [0, 1, 2])


class TestBootResolution(unittest.TestCase):
    def _fake_vm(self):
        disk0 = vim.vm.device.VirtualDisk()
        disk0.key = 2000
        disk1 = vim.vm.device.VirtualDisk()
        disk1.key = 2001
        nic0 = vim.vm.device.VirtualVmxnet3()
        nic0.key = 4000
        cdrom = vim.vm.device.VirtualCdrom()
        cdrom.key = 3000
        hw = types.SimpleNamespace(device=[disk0, disk1, nic0, cdrom])
        cfg = types.SimpleNamespace(hardware=hw)
        return types.SimpleNamespace(name="fake", config=cfg)

    def test_resolve(self):
        vm = self._fake_vm()
        order, warnings = devices.resolve_boot_devices(vm, ["network", "disk:2", "cdrom"])
        self.assertEqual(warnings, [])
        self.assertEqual([getattr(d, "deviceKey", None) for d in order], [4000, 2001, None])
        self.assertIsInstance(order[0], vim.vm.BootOptions.BootableEthernetDevice)
        self.assertIsInstance(order[1], vim.vm.BootOptions.BootableDiskDevice)
        self.assertIsInstance(order[2], vim.vm.BootOptions.BootableCdromDevice)

    def test_missing_device_warns(self):
        vm = self._fake_vm()
        order, warnings = devices.resolve_boot_devices(vm, ["disk:5", "floppy"])
        self.assertEqual(order, [])
        self.assertEqual(len(warnings), 2)

    def test_describe_round_trip(self):
        vm = self._fake_vm()
        order, _ = devices.resolve_boot_devices(vm, ["cdrom", "disk", "network"])
        boot = devices.build_boot_options(order, secure_boot=True)
        vm.config.bootOptions = boot
        self.assertEqual(devices.describe_boot_order(vm), ["cdrom", "disk", "network"])


class TestDeviceBuilders(unittest.TestCase):
    def test_disk_backing_modes(self):
        thin = devices.build_disk("vm1", 100, "LocalDS_0", -200, -100, 0, thin=True)
        self.assertTrue(thin.device.backing.thinProvisioned)
        self.assertFalse(thin.device.backing.eagerlyScrub)
        eager = devices.build_disk("vm1", 100, "LocalDS_0", -200, -100, 0, thin=False, eager=True)
        self.assertFalse(eager.device.backing.thinProvisioned)
        self.assertTrue(eager.device.backing.eagerlyScrub)
        self.assertEqual(eager.device.capacityInKB, 100 * 1024 * 1024)
        self.assertEqual(eager.fileOperation, "create")

    def test_gpu_builders(self):
        vgpu = devices.build_gpu_vgpu(-600, "nvidia_a100-40c")
        self.assertEqual(vgpu.device.backing.vgpu, "nvidia_a100-40c")
        dyn = devices.build_gpu_dynamic(-600, 0x10DE, 0x20B0)
        allowed = dyn.device.backing.allowedDevice[0]
        self.assertEqual((allowed.vendorId, allowed.deviceId), (0x10DE, 0x20B0))

    def test_tpm_builder(self):
        spec = devices.build_tpm()
        self.assertIsInstance(spec.device, vim.vm.device.VirtualTPM)

    def test_has_tpm_matches_vcsim_shape(self):
        # vcsim serves the vTPM back as a base VirtualDevice labelled
        # "Virtual TPM", not as a VirtualTPM; both shapes must match.
        plain = vim.vm.device.VirtualDevice()
        plain.deviceInfo = vim.Description(label="Virtual TPM")
        self.assertTrue(devices.has_tpm([plain]))
        real = vim.vm.device.VirtualTPM()
        real.deviceInfo = vim.Description(label="Virtual TPM")
        self.assertTrue(devices.has_tpm([real]))
        other = vim.vm.device.VirtualDevice()
        other.deviceInfo = vim.Description(label="PCI device")
        self.assertFalse(devices.has_tpm([other]))

    def test_video_edit_is_an_edit(self):
        spec = devices.build_video_edit(500, 131072, True)
        self.assertEqual(spec.operation, "edit")
        self.assertEqual(spec.device.key, 500)
        self.assertEqual(spec.device.videoRamSizeInKB, 131072)
        self.assertTrue(spec.device.enable3DSupport)
        self.assertEqual(spec.device.use3dRenderer, "automatic")

    def test_standard_nic_sets_device_name(self):
        # Regression test: vcsim resolves a standard-network NIC by
        # backing.deviceName (FindByName); an empty deviceName nil-derefs
        # and kills the simulator (govmomi v0.56.2 virtual_machine.go:1537).
        net = MagicMock(spec=vim.Network)
        net.name = "VM Network"
        spec = devices.build_nic(config.NicSpec(network="vm-network", type="vmxnet3"), -400, net, "standard")
        backing = spec.device.backing
        self.assertIsInstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo)
        self.assertEqual(backing.deviceName, "VM Network")
        self.assertIs(backing.network, net)


class TestFindNetwork(unittest.TestCase):
    def test_finds_opaque_network_in_network_folder(self):
        # vcsim omits OpaqueNetwork from Datacenter.network; find_network
        # must also walk the network folder, which lists it.
        opaque = MagicMock(spec=vim.OpaqueNetwork)
        opaque.name = "DC0_NSX0"
        folder = MagicMock(spec=vim.Folder)
        folder.childEntity = [opaque]
        dc = MagicMock(spec=vim.Datacenter)
        dc.network = []
        dc.networkFolder = folder
        self.assertIs(vcenter.find_network(dc, "DC0_NSX0"), opaque)

    def test_missing_network_returns_none(self):
        folder = MagicMock(spec=vim.Folder)
        folder.childEntity = []
        dc = MagicMock(spec=vim.Datacenter)
        dc.network = []
        dc.networkFolder = folder
        self.assertIsNone(vcenter.find_network(dc, "no-such-network"))


if __name__ == "__main__":
    unittest.main()
