"""Offline tests for vcenter.connect() scheme handling (vcenter.py).

Regression test for the Render failure where the builder spoke TLS to a
plaintext vcsim: SmartConnect defaults to https, so the configured
scheme must be passed through as its `protocol` argument.
"""

from unittest import mock

from vcsim_lab import vcenter
from vcsim_lab.config import VCenterConfig


def test_connect_passes_http_protocol(monkeypatch):
    cfg = VCenterConfig(host="127.0.0.1", port=10000, scheme="http")
    with mock.patch.object(vcenter, "SmartConnect", return_value="si") as sc:
        assert vcenter.connect(cfg) == "si"
    assert sc.call_args.kwargs["protocol"] == "http"
    assert sc.call_args.kwargs["host"] == "127.0.0.1"
    assert sc.call_args.kwargs["port"] == 10000


def test_connect_defaults_to_https_protocol():
    cfg = VCenterConfig()
    with mock.patch.object(vcenter, "SmartConnect", return_value="si") as sc:
        vcenter.connect(cfg)
    assert sc.call_args.kwargs["protocol"] == "https"
