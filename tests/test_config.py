"""Offline tests for VCenterConfig URL scheme handling (config.py)."""

import pytest

from vcsim_lab.config import ConfigError, VCenterConfig

VC_VARS = ("VC_HOST", "VC_PORT", "VC_USER", "VC_PASSWORD", "VC_INSECURE", "VC_SCHEME")


def _from_env(monkeypatch, **env):
    for key in VC_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return VCenterConfig.from_env()


def test_default_scheme_is_https(monkeypatch):
    assert _from_env(monkeypatch).url == "https://user@127.0.0.1:8989/sdk"


def test_http_scheme_from_env(monkeypatch):
    cfg = _from_env(monkeypatch, VC_SCHEME="http")
    assert cfg.scheme == "http"
    assert cfg.url == "http://user@127.0.0.1:8989/sdk"


def test_scheme_is_case_insensitive(monkeypatch):
    assert _from_env(monkeypatch, VC_SCHEME="HTTP").scheme == "http"


def test_invalid_scheme_raises(monkeypatch):
    with pytest.raises(ConfigError):
        _from_env(monkeypatch, VC_SCHEME="ftp")
