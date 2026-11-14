"""Offline tests for environment discovery and loading
(vcsim_lab.config list/load_environment, Environment).

Environments are built in tmp directories so the tests never touch the
real environments/ tree; a few tests assert on the real tree (default
and dev must both load and their labs must parse).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _write_env(root, name, **overrides):
    """Write environments/<name>/env.yaml under root (tmp_path)."""
    env_dir = os.path.join(root, name)
    os.makedirs(env_dir, exist_ok=True)
    data = {"name": name, "port": 8989 + len(os.listdir(root))}
    data.update(overrides)
    with open(os.path.join(env_dir, "env.yaml"), "w") as fh:
        yaml.safe_dump(data, fh)
    return env_dir


def test_discovery_sorts_and_applies_defaults(tmp_path):
    _write_env(tmp_path, "zeta", port=8999)
    _write_env(tmp_path, "alpha", port=8989, description="first")
    envs = config.list_environments(tmp_path)
    assert [e.name for e in envs] == ["alpha", "zeta"]
    alpha = envs[0]
    assert alpha.description == "first"
    assert alpha.port == 8989
    # Omitted vcsim flags default to the original lab's shape.
    assert alpha.vcsim["hosts"] == 3
    assert alpha.vcsim["datastores"] == 3
    assert alpha.vcsim["api_version"] == "8.0.3"
    assert alpha.lab_path == tmp_path / "alpha" / "vms.yaml"
    assert alpha.storage_path == tmp_path / "alpha" / "storage.yaml"
    assert alpha.model_dir == tmp_path.parent / "model" / "alpha"


def test_discovery_ignores_dirs_without_env_yaml(tmp_path):
    os.makedirs(tmp_path / "not-an-env")
    _write_env(tmp_path, "real", port=8989)
    assert [e.name for e in config.list_environments(tmp_path)] == ["real"]


def test_discovery_missing_dir_is_empty():
    assert config.list_environments("/nonexistent/environments") == []


def test_duplicate_port_rejected(tmp_path):
    _write_env(tmp_path, "one", port=8989)
    _write_env(tmp_path, "two", port=8989)
    with pytest.raises(config.ConfigError, match="both use vcsim port 8989"):
        config.list_environments(tmp_path)


def test_load_environment_unknown_names_available(tmp_path):
    _write_env(tmp_path, "alpha", port=8989)
    with pytest.raises(config.ConfigError, match="unknown environment 'nope'; available: alpha"):
        config.load_environment("nope", tmp_path)


def test_env_name_must_match_directory(tmp_path):
    env_dir = tmp_path / "alpha"
    env_dir.mkdir()
    with open(env_dir / "env.yaml", "w") as fh:
        yaml.safe_dump({"name": "beta", "port": 8989}, fh)
    with pytest.raises(config.ConfigError, match="does not match its directory"):
        config.list_environments(tmp_path)


@pytest.mark.parametrize(
    ("bad_port", "pattern"),
    [(0, "port must be an integer"), (65536, "port must be an integer"),
     (-1, "port must be an integer"), ("8989", "port must be an integer"),
     (True, "port must be an integer"), (None, "missing required key 'port'")],
)
def test_env_port_must_be_valid(tmp_path, bad_port, pattern):
    _write_env(tmp_path, "alpha", port=bad_port)
    with pytest.raises(config.ConfigError, match=pattern):
        config.list_environments(tmp_path)


def test_unknown_vcsim_flag_rejected(tmp_path):
    _write_env(tmp_path, "alpha", port=8989, vcsim={"vm": 0})
    with pytest.raises(config.ConfigError, match="unknown vcsim flag"):
        config.list_environments(tmp_path)


def test_negative_vcsim_flag_rejected(tmp_path):
    _write_env(tmp_path, "alpha", port=8989, vcsim={"hosts": -1})
    with pytest.raises(config.ConfigError, match="non-negative integer"):
        config.list_environments(tmp_path)


def test_vcsim_args_uses_flag_map(tmp_path):
    _write_env(
        tmp_path, "alpha", port=8989,
        vcsim={"hosts": 2, "datastores": 1, "api_version": "8.0.2"},
    )
    env = config.load_environment("alpha", tmp_path)
    args = env.vcsim_args()
    assert args[:2] == ["-api-version", "8.0.2"]
    assert args[args.index("-host") + 1] == "2"
    assert args[args.index("-ds") + 1] == "1"
    assert "-vm" not in args  # always added by the caller, never from env.yaml


def test_vcenter_config_takes_env_port(tmp_path):
    _write_env(tmp_path, "alpha", port=8990)
    env = config.load_environment("alpha", tmp_path)
    base = config.VCenterConfig(host="vc.example", port=443, scheme="https",
                                user="admin", password="s3cret", insecure=False)
    cfg = env.vcenter_config(base)
    assert cfg.port == 8990  # the environment's port, not the base's
    assert cfg.host == "vc.example"
    assert cfg.scheme == "https"
    assert cfg.user == "admin"
    assert cfg.insecure is False


def test_vcenter_config_for_env_vc_port_override(tmp_path, monkeypatch):
    _write_env(tmp_path, "alpha", port=8990)
    env = config.load_environment("alpha", tmp_path)
    monkeypatch.delenv("VC_PORT", raising=False)
    assert config.vcenter_config_for_env(env).port == 8990
    monkeypatch.setenv("VC_PORT", "8000")
    assert config.vcenter_config_for_env(env).port == 8000


def test_real_environments_load():
    envs = config.list_environments(os.path.join(ROOT, "environments"))
    assert [e.name for e in envs] == ["default", "dev"]
    default, dev = envs
    assert default.port == 8989
    assert dev.port == 8990
    assert dev.vcsim["hosts"] == 2
    assert dev.vcsim["datastores"] == 2
    # Both labs parse and keep their expected sizes.
    assert len(config.load_lab(default.lab_path).vms) == 22
    assert len(config.load_lab(dev.lab_path).vms) == 3
    assert [vm.name for vm in config.load_lab(dev.lab_path).vms] == [
        "dev-win10", "dev-ubuntu", "dev-ubuntu-template",
    ]


def test_env_info_script_lists_and_describes():
    """scripts/env_info.py is the shell scripts' only view of env.yaml,
    so test it end to end (subprocess, project root as cwd)."""
    out = subprocess.run(
        [sys.executable, "scripts/env_info.py", "--list"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert out == ["default", "dev"]
    out = subprocess.run(
        [sys.executable, "scripts/env_info.py", "--env", "dev"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "ENV_NAME=dev" in out
    assert "ENV_PORT=8990" in out
    assert "-host 2" in out
    assert "ENV_LAB=environments/dev/vms.yaml" in out
    assert "ENV_MODEL_DIR=model/dev" in out
    # The output must eval safely in bash: the flag list contains
    # spaces, and an unquoted assignment would run its second word as
    # a command (regression test — this broke the start script once).
    evaled = subprocess.run(
        ["bash", "-c",
         'eval "$(python3 scripts/env_info.py --env dev)"; echo "$ENV_PORT|$ENV_VCSIM_FLAGS"'],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert evaled.returncode == 0, evaled.stderr
    assert evaled.stdout.strip() == (
        "8990|-api-version 8.0.3 -dc 1 -cluster 1 -host 2 "
        "-standalone-host 1 -ds 2 -pod 1 -pg 2 -nsx 1 -pool 1"
    )
    missing = subprocess.run(
        [sys.executable, "scripts/env_info.py", "--env", "nope"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert missing.returncode == 2
    assert "unknown environment 'nope'" in missing.stderr
