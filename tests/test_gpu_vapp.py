"""Offline tests for GPU vendor/model, templates and vApp appliances:
no vCenter needed. Run from the project root:

    python -m pytest tests/ -q
    # or without pytest:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pyVmomi import vim  # noqa: E402

from vcsim_lab import config, devices, specs, verify  # noqa: E402

LAB = os.path.join(os.path.dirname(__file__), "..", "environments", "default", "vms.yaml")

MINIMAL_VM = {
    "name": "x",
    "guest_id": "ubuntu64Guest",
    "firmware": "efi",
    "disks": [{"size_gb": 10}],
    "nics": [{"network": "prod"}],
}


def _load_vm(**overrides):
    raw = dict(MINIMAL_VM)
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


def _live(**overrides):
    base = {
        "name": "test-vm",
        "guest_id": "ubuntu64Guest",
        "cpus": 4,
        "memory_mb": 8192,
        "firmware": "efi",
        "hardware_version": "vmx-20",
        "power": "poweredOff",
        "boot_order": ["disk"],
        "template": False,
        "vapp": None,
        "disks": [{"capacity_gb": 100, "datastore": "LocalDS_0", "thin": True}],
        "nics": [{"label": "Network adapter 1"}],
        "cdrom": False,
        "video_3d": False,
        "tpm": False,
        "secure_boot": False,
        "gpus": [],
    }
    base.update(overrides)
    return base


class TestGpuVendorModel(unittest.TestCase):
    def test_vendor_model_load(self):
        vm = _load_vm(
            gpu={"mode": "passthrough", "vendor": "nvidia", "model": "A100",
                 "vendor_id": "0x10de", "device_id": "0x20b0"}
        )
        self.assertEqual(vm.gpu.vendor, "nvidia")
        self.assertEqual(vm.gpu.model, "A100")

    def test_vendor_inferred_from_vendor_id(self):
        vm = _load_vm(
            gpu={"mode": "passthrough", "model": "MI210",
                 "vendor_id": "0x1002", "device_id": "0x0c34"}
        )
        self.assertEqual(vm.gpu.vendor, "amd")

    def test_unknown_vendor_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                gpu={"mode": "passthrough", "vendor": "intel",
                     "vendor_id": "0x8086", "device_id": "0x1234"}
            )

    def test_vendor_vendor_id_mismatch_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                gpu={"mode": "passthrough", "vendor": "amd", "model": "MI210",
                     "vendor_id": "0x10de", "device_id": "0x0c34"}
            )

    def test_model_device_id_mismatch_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                gpu={"mode": "passthrough", "vendor": "nvidia", "model": "A100",
                     "vendor_id": "0x10de", "device_id": "0x1eb8"}
            )

    def test_unknown_model_warns_but_loads(self):
        warnings = []
        vm = config._load_vm(
            dict(MINIMAL_VM, gpu={"mode": "passthrough", "vendor": "nvidia",
                                  "model": "L40S", "vendor_id": "0x10de",
                                  "device_id": "0x26b5"}),
            warnings,
        )
        self.assertEqual(vm.gpu.model, "L40S")
        self.assertEqual(len(warnings), 1)
        self.assertIn("L40S", warnings[0])

    def test_amd_vgpu_rejected(self):
        # AMD MxGPU is SR-IOV VF passthrough, not a vGPU profile.
        with self.assertRaises(config.ConfigError):
            _load_vm(
                gpu={"mode": "vgpu", "vendor": "amd", "model": "MI210",
                     "profile": "amd_mxgpu"}
            )

    def test_vgpu_profile_names_validated(self):
        for profile in ("nvidia_a100-40c", "grid_t4-8q", "nvidia_h100-80c"):
            vm = _load_vm(gpu={"mode": "vgpu", "vendor": "nvidia", "profile": profile})
            self.assertEqual(vm.gpu.profile, profile)
        for bad in ("a100-40c", "nvidia_a100", "amd_mxgpu", "nvidia_a100-40x"):
            with self.assertRaises(config.ConfigError, msg=bad):
                _load_vm(gpu={"mode": "vgpu", "vendor": "nvidia", "profile": bad})


class TestTemplates(unittest.TestCase):
    def test_template_flag_loads(self):
        vm = _load_vm(template=True)
        self.assertTrue(vm.template)
        self.assertFalse(vm.power_on)

    def test_template_with_power_on_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(template=True, power_on=True)

    def test_is_template(self):
        templ = types.SimpleNamespace(config=types.SimpleNamespace(template=True))
        plain = types.SimpleNamespace(config=types.SimpleNamespace(template=False))
        unset = types.SimpleNamespace(config=types.SimpleNamespace(template=None))
        self.assertTrue(devices.is_template(templ))
        self.assertFalse(devices.is_template(plain))
        self.assertFalse(devices.is_template(unset))


class TestVAppConfig(unittest.TestCase):
    def test_vapp_loads(self):
        vm = _load_vm(
            vapp_config={
                "name": "OPNsense", "vendor": "Deciso", "version": "24.7",
                "properties": [
                    {"id": "hostname", "label": "Hostname", "value": "opnsense-fw"},
                    {"id": "wan_ip", "label": "WAN IP", "type": "ip", "value": "192.168.1.1"},
                ],
            }
        )
        self.assertEqual(vm.vapp.name, "OPNsense")
        self.assertEqual(vm.vapp.vendor, "Deciso")
        self.assertEqual([p.id for p in vm.vapp.properties], ["hostname", "wan_ip"])
        self.assertEqual(vm.vapp.properties[1].type, "ip")

    def test_vapp_requires_product_fields(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(vapp_config={"vendor": "Deciso", "version": "24.7"})

    def test_vapp_duplicate_property_id_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                vapp_config={
                    "name": "X", "vendor": "Y", "version": "1.0",
                    "properties": [
                        {"id": "hostname", "label": "Hostname"},
                        {"id": "hostname", "label": "Hostname again"},
                    ],
                }
            )

    def test_vapp_bad_property_type_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(
                vapp_config={
                    "name": "X", "vendor": "Y", "version": "1.0",
                    "properties": [{"id": "x", "label": "X", "type": "url"}],
                }
            )

    def test_vapp_container_must_be_declared(self):
        yaml_text = """
lab:
  datacenter: DC0
  cluster: DC0_C0
  vapps: [lab-appliances]
networks:
  prod: {name: DC0_DVPG0, type: dvpg}
vms:
  - name: app1
    guest_id: ubuntu64Guest
    disks: [{size_gb: 10}]
    nics: [{network: prod}]
    vapp_container: no-such-vapp
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(yaml_text)
            path = fh.name
        try:
            with self.assertRaises(config.ConfigError):
                config.load_lab(path)
        finally:
            os.unlink(path)

    def test_build_vapp_config_types(self):
        # The write type is VmConfigSpec with ProductSpec/PropertySpec
        # entries (operation "add"); VmConfigInfo is the read-back type.
        vapp = config.VAppSpec(
            name="OPNsense", vendor="Deciso", version="24.7",
            properties=[config.VAppPropertySpec(id="hostname", label="Hostname",
                                                value="opnsense-fw")],
        )
        spec = devices.build_vapp_config(vapp)
        self.assertIsInstance(spec, vim.vApp.VmConfigSpec)
        product = spec.product[0]
        self.assertIsInstance(product, vim.vApp.ProductSpec)
        self.assertEqual(product.operation, "add")
        self.assertEqual((product.info.name, product.info.vendor, product.info.version),
                         ("OPNsense", "Deciso", "24.7"))
        prop = spec.property[0]
        self.assertIsInstance(prop, vim.vApp.PropertySpec)
        self.assertEqual(prop.operation, "add")
        self.assertEqual((prop.info.id, prop.info.label, prop.info.value),
                         ("hostname", "Hostname", "opnsense-fw"))

    def test_describe_and_match_round_trip(self):
        vapp = config.VAppSpec(
            name="TrueNAS", vendor="iXsystems", version="24.10",
            properties=[config.VAppPropertySpec(id="hostname", label="Hostname",
                                                value="truenas-storage")],
        )
        product = types.SimpleNamespace(
            name="TrueNAS", vendor="iXsystems", version="24.10", fullVersion="24.10.2")
        prop = types.SimpleNamespace(id="hostname", label="Hostname", type="string",
                                     value="truenas-storage")
        vapp_info = types.SimpleNamespace(product=[product], property=[prop])
        vm = types.SimpleNamespace(config=types.SimpleNamespace(vAppConfig=vapp_info))
        described = devices.describe_vapp_config(vm)
        self.assertEqual(described["product"]["name"], "TrueNAS")
        self.assertEqual(described["properties"][0]["value"], "truenas-storage")
        self.assertTrue(devices.vapp_config_matches(vm, vapp))
        vapp2 = config.VAppSpec(name="TrueNAS", vendor="iXsystems", version="25.04")
        self.assertFalse(devices.vapp_config_matches(vm, vapp2))
        no_vapp = types.SimpleNamespace(config=types.SimpleNamespace(vAppConfig=None))
        self.assertIsNone(devices.describe_vapp_config(no_vapp))
        self.assertFalse(devices.vapp_config_matches(no_vapp, vapp))


class TestCreateSpecVApp(unittest.TestCase):
    def _refs(self):
        # A standard network: its NIC backing only needs the network's
        # name, which keeps this fixture about vAppConfig, not NICs.
        net = MagicMock(spec=vim.Network)
        net.name = "VM Network"
        return specs.ResolvedRefs(
            folder=MagicMock(), resource_pool=MagicMock(), datastores={},
            networks={"prod": net}, network_types={"prod": "standard"},
        )

    def test_vapp_config_in_create_spec(self):
        vapp = config.VAppSpec(name="OPNsense", vendor="Deciso", version="24.7")
        spec = _vm_spec(vapp=vapp)
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs())
        self.assertIsNotNone(cfg.vAppConfig)
        self.assertEqual(cfg.vAppConfig.product[0].info.name, "OPNsense")

    def test_no_vapp_config_without_spec(self):
        spec = _vm_spec()
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs())
        self.assertIsNone(cfg.vAppConfig)


class TestVerifyTemplateVApp(unittest.TestCase):
    def test_template_mismatch_is_a_failure(self):
        spec = _vm_spec(template=True)
        failures, warnings = verify.verify_vm(spec, _live(template=False))
        self.assertTrue(any("template" in f for f in failures))
        failures, warnings = verify.verify_vm(spec, _live(template=True))
        self.assertEqual(failures, [])

    def test_vapp_mismatch_is_a_failure(self):
        vapp = config.VAppSpec(
            name="OPNsense", vendor="Deciso", version="24.7",
            properties=[config.VAppPropertySpec(id="hostname", label="Hostname",
                                                value="opnsense-fw")],
        )
        spec = _vm_spec(vapp=vapp)
        live_vapp = {
            "product": {"name": "OPNsense", "vendor": "Deciso", "version": "24.7",
                        "full_version": "24.7.1"},
            "properties": [{"id": "hostname", "label": "Hostname", "type": "string",
                            "value": "opnsense-fw"}],
        }
        failures, warnings = verify.verify_vm(spec, _live(vapp=live_vapp))
        self.assertEqual(failures, [])
        failures, _ = verify.verify_vm(spec, _live(vapp=None))
        self.assertTrue(any("vapp" in f for f in failures))
        wrong = dict(live_vapp)
        wrong["properties"] = [{"id": "hostname", "label": "Hostname", "type": "string",
                                "value": "other"}]
        failures, _ = verify.verify_vm(spec, _live(vapp=wrong))
        self.assertTrue(any("vapp properties" in f for f in failures))


class TestLabMatrix(unittest.TestCase):
    def test_lab_has_gpu_vendors_templates_appliances(self):
        lab = config.load_lab(LAB)
        self.assertEqual(len(lab.vms), 22)
        vendors = {v.gpu.vendor for v in lab.vms if v.gpu}
        self.assertEqual(vendors, {"nvidia", "amd"})
        models = {(v.gpu.vendor, v.gpu.model) for v in lab.vms if v.gpu}
        self.assertIn(("nvidia", "A100"), models)
        self.assertIn(("nvidia", "H100"), models)
        self.assertIn(("amd", "MI210"), models)
        templates = [v for v in lab.vms if v.template]
        self.assertEqual(len(templates), 3)
        self.assertTrue(all(not v.power_on for v in templates))
        appliances = [v for v in lab.vms if v.vapp is not None]
        self.assertEqual(len(appliances), 2)
        self.assertEqual({v.vapp_container for v in appliances}, {"lab-appliances"})
        self.assertEqual(lab.vapps, ["lab-appliances"])
        self.assertIn("lab-templates", lab.folders)
        self.assertIn("lab-appliances", lab.folders)


if __name__ == "__main__":
    unittest.main()
