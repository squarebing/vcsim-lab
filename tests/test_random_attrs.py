"""Offline tests for the seeded per-VM random attributes
(CBT, power state, fast boot, tools status): no vCenter needed.
Run from the project root:

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

LAB = os.path.join(os.path.dirname(__file__), "..", "vms.yaml")

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


def _lab(vms, seed=42):
    return config.LabConfig(datacenter="DC0", cluster="DC0_C0", vms=vms, random_seed=seed)


class TestResolution(unittest.TestCase):
    def test_same_seed_same_assignment(self):
        lab = _lab([_vm_spec(name="a"), _vm_spec(name="b"), _vm_spec(name="c")])
        first = config.resolve_random_assignments(lab)
        second = config.resolve_random_assignments(lab)
        self.assertEqual(first, second)

    def test_different_seed_different_assignment(self):
        vms = [_vm_spec(name=f"vm-{i}") for i in range(10)]
        a = config.resolve_random_assignments(_lab(vms, seed=42))
        b = config.resolve_random_assignments(_lab(vms, seed=7))
        self.assertNotEqual(a, b)

    def test_adding_a_vm_does_not_reshuffle_others(self):
        before = config.resolve_random_assignments(
            _lab([_vm_spec(name="a"), _vm_spec(name="b")])
        )
        after = config.resolve_random_assignments(
            _lab([_vm_spec(name="a"), _vm_spec(name="b"), _vm_spec(name="c")])
        )
        self.assertEqual(before["a"], after["a"])
        self.assertEqual(before["b"], after["b"])

    def test_explicit_values_pass_through(self):
        vm = _vm_spec(
            cbt=True, power_state="suspended", fast_boot=False, tools_status="toolsOk"
        )
        assigned = config.resolve_random_assignments(_lab([vm]))["test-vm"]
        self.assertTrue(assigned.cbt)
        self.assertEqual(assigned.power_state, "suspended")
        self.assertFalse(assigned.fast_boot)
        self.assertEqual(assigned.tools_status, "toolsOk")

    def test_power_state_derived_from_power_on(self):
        on = config.resolve_random_assignments(_lab([_vm_spec(power_on=True)]))["test-vm"]
        off = config.resolve_random_assignments(_lab([_vm_spec(power_on=False)]))["test-vm"]
        self.assertEqual(on.power_state, "poweredOn")
        self.assertEqual(off.power_state, "poweredOff")

    def test_explicit_power_state_wins_over_power_on(self):
        vm = _vm_spec(power_on=True, power_state="poweredOff")
        assigned = config.resolve_random_assignments(_lab([vm]))["test-vm"]
        self.assertEqual(assigned.power_state, "poweredOff")

    def test_template_power_forced_off(self):
        vm = _vm_spec(template=True)
        assigned = config.resolve_random_assignments(_lab([vm]))["test-vm"]
        self.assertEqual(assigned.power_state, "poweredOff")

    def test_random_draws_stay_in_range(self):
        vms = [_vm_spec(name=f"vm-{i}") for i in range(20)]
        for assigned in config.resolve_random_assignments(_lab(vms)).values():
            self.assertIsInstance(assigned.cbt, bool)
            self.assertIn(assigned.power_state, config.POWER_STATES)
            self.assertIsInstance(assigned.fast_boot, bool)
            self.assertIn(assigned.tools_status, config.TOOLS_STATUSES)


class TestValidation(unittest.TestCase):
    def test_bad_power_state_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(power_state="running")

    def test_bad_tools_status_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(tools_status="toolsGreat")

    def test_bad_cbt_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(cbt="yes")

    def test_bad_fast_boot_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(fast_boot="maybe")

    def test_template_with_power_state_rejected(self):
        with self.assertRaises(config.ConfigError):
            _load_vm(template=True, power_state="poweredOn")
        with self.assertRaises(config.ConfigError):
            _load_vm(template=True, power_state="random")

    def test_random_seed_must_be_int(self):
        import tempfile

        import yaml

        lab = {
            "lab": {"datacenter": "DC0", "cluster": "DC0_C0", "random_seed": "forty-two"},
            "networks": {},
            "vms": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(lab, fh)
            path = fh.name
        try:
            with self.assertRaises(config.ConfigError):
                config.load_lab(path)
        finally:
            os.unlink(path)

    def test_defaults_are_random(self):
        vm = _load_vm()
        self.assertEqual(vm.cbt, "random")
        self.assertIsNone(vm.power_state)
        self.assertEqual(vm.fast_boot, "random")
        self.assertEqual(vm.tools_status, "random")


class TestSpecBuilding(unittest.TestCase):
    def _refs(self):
        net = MagicMock(spec=vim.Network)
        net.name = "VM Network"
        return specs.ResolvedRefs(
            folder=MagicMock(), resource_pool=MagicMock(), datastores={},
            networks={"prod": net}, network_types={"prod": "standard"},
        )

    def test_create_spec_sets_cbt(self):
        spec = _vm_spec()
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs(), cbt=True)
        self.assertTrue(cfg.changeTrackingEnabled)
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs(), cbt=False)
        self.assertFalse(cfg.changeTrackingEnabled)

    def test_create_spec_cbt_defaults_off(self):
        spec = _vm_spec()
        cfg = specs.build_create_spec(spec, specs.plan_vm(spec), self._refs())
        self.assertFalse(cfg.changeTrackingEnabled)

    def test_boot_delay_values(self):
        fast = config.ResolvedAssignment(
            cbt=True, power_state="poweredOn", fast_boot=True, tools_status="toolsOk"
        )
        slow = config.ResolvedAssignment(
            cbt=False, power_state="poweredOff", fast_boot=False, tools_status="toolsOld"
        )
        self.assertEqual(fast.expected_boot_delay_ms(), 0)
        self.assertEqual(slow.expected_boot_delay_ms(), 3000)
        self.assertEqual(config.FAST_BOOT_DELAY_MS, 0)
        self.assertEqual(config.NORMAL_BOOT_DELAY_MS, 3000)


class TestLabMatrix(unittest.TestCase):
    def test_lab_distribution_covers_all_values(self):
        lab = config.load_lab(LAB)
        self.assertEqual(len(lab.vms), 22)
        assigned = config.resolve_random_assignments(lab)
        self.assertEqual(set(a.power_state for a in assigned.values()), set(config.POWER_STATES))
        self.assertEqual({a.cbt for a in assigned.values()}, {True, False})
        self.assertEqual({a.fast_boot for a in assigned.values()}, {True, False})
        self.assertGreaterEqual(len({a.tools_status for a in assigned.values()}), 3)

    def test_templates_all_powered_off_appliances_on(self):
        lab = config.load_lab(LAB)
        assigned = config.resolve_random_assignments(lab)
        for vm in lab.vms:
            if vm.template:
                self.assertEqual(assigned[vm.name].power_state, "poweredOff")
        for vm in lab.vms:
            if vm.vapp_container:
                self.assertEqual(assigned[vm.name].power_state, "poweredOn")


def _live(**overrides):
    base = {
        "name": "test-vm",
        "guest_id": "ubuntu64Guest",
        "cpus": 4,
        "memory_mb": 8192,
        "firmware": "efi",
        "hardware_version": "vmx-20",
        "power": "poweredOn",
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
        "cbt": True,
        "boot_delay_ms": 0,
        "tools_status": "toolsOk",
    }
    base.update(overrides)
    return base


class TestVerifyRandomAttrs(unittest.TestCase):
    def _assigned(self, **overrides):
        base = dict(cbt=True, power_state="poweredOn", fast_boot=True, tools_status="toolsOk")
        base.update(overrides)
        return config.ResolvedAssignment(**base)

    def test_matching_assignment_passes(self):
        spec = _vm_spec()
        failures, warnings = verify.verify_vm(spec, _live(), self._assigned())
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_power_mismatch_is_a_failure(self):
        spec = _vm_spec()
        failures, _ = verify.verify_vm(spec, _live(power="poweredOff"), self._assigned())
        self.assertTrue(any("power" in f for f in failures))

    def test_cbt_mismatch_is_a_failure(self):
        spec = _vm_spec()
        failures, _ = verify.verify_vm(spec, _live(cbt=False), self._assigned())
        self.assertTrue(any("cbt" in f for f in failures))

    def test_boot_delay_mismatch_is_a_failure(self):
        spec = _vm_spec()
        failures, _ = verify.verify_vm(spec, _live(boot_delay_ms=3000), self._assigned())
        self.assertTrue(any("boot_delay" in f for f in failures))

    def test_tools_mismatch_is_a_warning_not_a_failure(self):
        spec = _vm_spec()
        failures, warnings = verify.verify_vm(
            spec, _live(tools_status="toolsNotInstalled"), self._assigned()
        )
        self.assertEqual(failures, [])
        self.assertTrue(any("tools_status" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
