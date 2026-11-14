"""Offline tests for the inventory API (vcsim_lab/api.py): no vCenter needed.

The vCenter layer is mocked (connect/query/storage functions), and the
reverse proxy is tested against a fake httpx client that records what
it was asked to forward — so the suite proves the routing, filtering,
error handling, environment switching and proxy forwarding without any
live server.

The app under test serves two fake environments (default on 8989, dev
on 8990) via a hand-built EnvironmentRegistry; the fake connect returns
a per-port ServiceInstance sentinel so the canned query results differ
per environment.

Run from the project root:

    python -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import api, config, storage  # noqa: E402
from vcsim_lab.api import EnvironmentRegistry, EnvironmentTarget  # noqa: E402
from vcsim_lab.config import VCenterConfig  # noqa: E402
from vcsim_lab.vcenter import VCenterError  # noqa: E402

_CANNED_VMS = [
    {"name": "win11-secure", "template": False, "host": "DC0_H0", "power": "poweredOn"},
    {"name": "win2022-template", "template": True, "host": "DC0_H0", "power": "poweredOff"},
    {"name": "ubuntu-k8s-node", "template": False, "host": "DC0_C0_H0", "power": "suspended"},
]

_CANNED_VMS_DEV = [
    {"name": "dev-win10", "template": False, "host": "DC0_H0", "power": "poweredOn"},
    {"name": "dev-ubuntu", "template": False, "host": "DC0_C0_H0", "power": "poweredOff"},
    {"name": "dev-ubuntu-template", "template": True, "host": "DC0_C0_H0", "power": "poweredOff"},
]

_CANNED_INVENTORY = {
    "datacenters": ["DC0"],
    "clusters": ["DC0_C0"],
    "hosts": ["DC0_H0", "DC0_C0_H0"],
    "datastores": ["LocalDS_0", "LocalDS_1"],
    "networks": [
        {"name": "VM Network", "type": "Network"},
        {"name": "DC0_DVPG0", "type": "DistributedVirtualPortgroup"},
    ],
    "dvportgroups": ["DC0_DVPG0"],
    "resource_pools": ["lab-general"],
    "vapps": ["lab-appliances"],
    "vms": ["win11-secure", "win2022-template", "ubuntu-k8s-node"],
}

_CANNED_INVENTORY_DEV = {
    "datacenters": ["DC0"],
    "clusters": ["DC0_C0"],
    "hosts": ["DC0_H0", "DC0_C0_H0"],
    "datastores": ["LocalDS_0", "LocalDS_1"],
    "networks": [
        {"name": "VM Network", "type": "Network"},
        {"name": "DC0_DVPG0", "type": "DistributedVirtualPortgroup"},
    ],
    "dvportgroups": ["DC0_DVPG0"],
    "resource_pools": ["lab-dev"],
    "vapps": [],
    "vms": ["dev-win10", "dev-ubuntu", "dev-ubuntu-template"],
}

_CANNED_HOSTS = [
    {"host": "DC0_H0", "system_uuid": "uuid-1", "model": "VMware7,1", "pci_devices": []},
    {"host": "DC0_C0_H0", "system_uuid": "uuid-2", "model": "VMware7,1", "pci_devices": []},
]

_CANNED_LIVE_STORAGE = {
    "hosts": [
        {
            "name": "DC0_H0",
            "hbas": [],
            "luns": [],
            "multipath": [],
            "topology": [],
            "software_iscsi_enabled": None,
        }
    ],
    "datastores": [
        {
            "name": "LocalDS_0",
            "type": "OTHER",
            "info_type": "LocalDatastoreInfo",
            "capacity_gb": 32.0,
            "free_gb": 30.0,
            "url": "ds:///vmfs/volumes/LocalDS_0",
            "mounts": [],
            "extents": None,
        }
    ],
}


# Sentinel for _target(): "not given" means "empty dict / empty model",
# while an explicit None models a target whose lab files failed to load.
_UNSET: object = object()


def _target(name, port, declared=_UNSET, storage_model=_UNSET, **vc_kwargs):
    """One fake environment target (default: https, insecure)."""
    env = config.Environment(
        name=name,
        description=f"{name} environment",
        port=port,
        vcsim=dict(config.VCSIM_FLAG_DEFAULTS),
        directory=Path("environments") / name,
        root=Path("."),
    )
    vc = VCenterConfig(host="127.0.0.1", port=port, **vc_kwargs)
    return EnvironmentTarget(
        env=env,
        vc_config=vc,
        declared={} if declared is _UNSET else declared,
        storage_model=storage.StorageModel() if storage_model is _UNSET else storage_model,
    )


def _registry(active="default", **targets):
    """Two fake environments (default 8989, dev 8990) unless overridden
    via targets={'default': target, ...}."""
    defaults = {"default": _target("default", 8989), "dev": _target("dev", 8990)}
    defaults.update(targets)
    return EnvironmentRegistry(list(defaults.values()), active=active)


def _fake_connect(cfg):
    """connect stand-in: a per-port sentinel so fakes can tell the
    environments apart. Tests that need an unreachable environment
    patch this with a port check."""
    return ("si", cfg.port)


def _fake_summarize(si, names=None, declared=None):
    """summarize_lab stand-in that honours the names filter and the
    environment (via the per-port sentinel)."""
    vms = _CANNED_VMS if si[1] == 8989 else _CANNED_VMS_DEV
    if names:
        wanted = set(names)
        return [dict(s) for s in vms if s["name"] in wanted]
    return [dict(s) for s in vms]


def _fake_inventory(si):
    return dict(_CANNED_INVENTORY if si[1] == 8989 else _CANNED_INVENTORY_DEV)


def _make_client(monkeypatch, registry=None, **app_kwargs):
    """A TestClient for an app whose vCenter connection is mocked.

    The registry defaults to two fake environments; query/storage
    functions are patched per test. list_inventory is patched by
    default because /api/environments and /api/health probe it.
    """
    monkeypatch.setattr(api, "connect", _fake_connect)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "list_inventory", _fake_inventory)
    app = api.create_app(registry=registry or _registry(), **app_kwargs)
    return TestClient(app)


class FakeAsyncClient:
    """httpx.AsyncClient stand-in: records forwarded requests, returns a
    canned response (or raises, for the unreachable-backend test)."""

    instances: list["FakeAsyncClient"] = []
    response: httpx.Response | None = None
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.requests: list[dict] = []
        FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, headers=None, content=None):
        self.requests.append(
            {
                "method": method,
                "url": str(url),
                "headers": {k.lower(): v for k, v in dict(headers or {}).items()},
                "content": content,
            }
        )
        if FakeAsyncClient.error is not None:
            raise FakeAsyncClient.error
        return FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _reset_fake_client():
    FakeAsyncClient.instances.clear()
    FakeAsyncClient.response = httpx.Response(
        200,
        headers={"content-type": "application/json", "set-cookie": "vmware_soap_session=abc"},
        content=b'{"ok": true}',
        request=httpx.Request("GET", "http://127.0.0.1:8989/about"),
    )
    FakeAsyncClient.error = None


def _patch_proxy(monkeypatch):
    """Route the API's httpx calls through the fake client."""
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


# ---------------------------------------------------------------------------
# /api endpoints
# ---------------------------------------------------------------------------


def test_api_index_lists_endpoints_and_environments(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docs"] == "/docs"
    for path in ("/api/environments", "/api/environment", "/api/inventory",
                 "/api/vms", "/api/hosts", "/api/datastores", "/api/networks",
                 "/api/vapps", "/api/storage", "/api/health"):
        assert path in body["endpoints"]
    assert body["active_environment"] == "default"
    assert body["environments"] == ["default", "dev"]


def test_vms_returns_summaries(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms")
    assert resp.status_code == 200
    assert resp.headers["X-Environment"] == "default"
    names = [vm["name"] for vm in resp.json()]
    assert names == ["win11-secure", "win2022-template", "ubuntu-k8s-node"]


def test_vms_template_filter(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    assert [v["name"] for v in client.get("/api/vms?template=true").json()] == [
        "win2022-template"
    ]
    assert [v["name"] for v in client.get("/api/vms?template=false").json()] == [
        "win11-secure",
        "ubuntu-k8s-node",
    ]


def test_vm_by_name_and_404(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms/win11-secure")
    assert resp.status_code == 200
    assert resp.json()["name"] == "win11-secure"
    resp = client.get("/api/vms/no-such-vm")
    assert resp.status_code == 404
    assert "no-such-vm" in resp.json()["detail"]


def test_inventory_includes_counts_and_environment(monkeypatch):
    monkeypatch.setattr(api.query, "list_inventory", lambda si: dict(_CANNED_INVENTORY))
    client = _make_client(monkeypatch)
    resp = client.get("/api/inventory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["environment"] == "default"
    assert body["hosts"] == ["DC0_H0", "DC0_C0_H0"]
    assert body["counts"]["hosts"] == 2
    assert body["counts"]["vms"] == 3
    assert body["counts"]["datastores"] == 2


def test_hosts_attach_their_vms(monkeypatch):
    monkeypatch.setattr(api.query, "list_host_pci", lambda si: [dict(h) for h in _CANNED_HOSTS])
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/hosts")
    assert resp.status_code == 200
    hosts = {h["host"]: h for h in resp.json()}
    assert hosts["DC0_H0"]["vms"] == ["win11-secure", "win2022-template"]
    assert hosts["DC0_H0"]["vm_count"] == 2
    assert hosts["DC0_C0_H0"]["vms"] == ["ubuntu-k8s-node"]
    assert hosts["DC0_C0_H0"]["vm_count"] == 1


def test_datastores(monkeypatch):
    canned = [{"name": "LocalDS_0", "capacity_gb": 32.0, "mounts": []}]
    monkeypatch.setattr(storage, "live_datastore_mounts", lambda si: canned)
    client = _make_client(monkeypatch)
    resp = client.get("/api/datastores")
    assert resp.status_code == 200
    assert resp.json() == canned


def test_networks(monkeypatch):
    monkeypatch.setattr(api.query, "list_inventory", lambda si: dict(_CANNED_INVENTORY))
    client = _make_client(monkeypatch)
    resp = client.get("/api/networks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["environment"] == "default"
    assert body["count"] == 2
    assert body["dvportgroups"] == ["DC0_DVPG0"]


def test_vapps(monkeypatch):
    canned = [{"name": "lab-appliances", "vms": ["opnsense-fw", "truenas-storage"], "vm_count": 2}]
    monkeypatch.setattr(api.query, "list_vapps", lambda si: canned)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vapps")
    assert resp.status_code == 200
    assert resp.json() == canned


def test_storage_reconciles_with_model(monkeypatch):
    monkeypatch.setattr(storage, "live_storage", lambda si: _CANNED_LIVE_STORAGE)
    client = _make_client(monkeypatch)
    resp = client.get("/api/storage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["environment"] == "default"
    assert body["hosts"][0]["name"] == "DC0_H0"
    assert body["datastores"][0]["name"] == "LocalDS_0"
    assert "not loaded" not in " ".join(body["notes"])


def test_storage_without_model_notes_it(monkeypatch):
    monkeypatch.setattr(storage, "live_storage", lambda si: _CANNED_LIVE_STORAGE)
    registry = _registry(default=_target("default", 8989, storage_model=None))
    client = _make_client(monkeypatch, registry=registry)
    resp = client.get("/api/storage")
    assert resp.status_code == 200
    assert any("not loaded" in n for n in resp.json()["notes"])


def test_health_ok_and_down(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["active_environment"] == "default"
    assert body["vcsim"] == "reachable"
    assert body["environments"]["default"]["status"] == "reachable"
    assert body["environments"]["dev"]["status"] == "reachable"

    def _boom(cfg):
        raise VCenterError("connection refused")

    monkeypatch.setattr(api, "connect", _boom)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["vcsim"] == "unreachable"
    assert body["environments"]["dev"]["status"] == "unreachable"


def test_health_degraded_only_when_active_down(monkeypatch):
    """One environment down does not degrade the API while the active
    environment is up — the per-env map still reports it."""
    def _down_dev(cfg):
        if cfg.port == 8990:
            raise VCenterError("connection refused")
        return ("si", cfg.port)

    monkeypatch.setattr(api, "connect", _down_dev)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "list_inventory", _fake_inventory)
    client = TestClient(api.create_app(registry=_registry()))
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["environments"]["dev"]["status"] == "unreachable"


def test_vms_down_returns_503(monkeypatch):
    def _boom(cfg):
        raise VCenterError("connection refused")

    monkeypatch.setattr(api, "connect", _boom)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    app = api.create_app(registry=_registry())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/vms")
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]


def test_unknown_api_path_is_json_404(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api/no-such-endpoint")
    assert resp.status_code == 404
    assert "unknown API path" in resp.json()["detail"]


def test_docs_loads(monkeypatch):
    """The docs routes must win over the proxy catch-all."""
    client = _make_client(monkeypatch)
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


def test_create_app_no_environments_dir_falls_back(monkeypatch):
    """No environments/ directory: a single live-only default target
    from the base connection settings (no lab files needed)."""
    monkeypatch.setattr(api, "connect", _fake_connect)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    monkeypatch.setattr(api.query, "list_inventory", _fake_inventory)
    app = api.create_app(env_root="no-such-env-root")
    client = TestClient(app)
    assert client.get("/api/vms").status_code == 200
    assert client.get("/api/environment").json()["name"] == "default"


def test_create_app_unknown_active_env_fails():
    with pytest.raises(ValueError, match="unknown active environment"):
        api.create_app(registry=_registry(), active_env="nope")


# ---------------------------------------------------------------------------
# Environment switching
# ---------------------------------------------------------------------------


def test_environments_lists_both_with_status(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api/environments")
    assert resp.status_code == 200
    envs = {e["name"]: e for e in resp.json()}
    assert sorted(envs) == ["default", "dev"]
    assert envs["default"]["active"] is True
    assert envs["default"]["port"] == 8989
    assert envs["default"]["status"] == "reachable"
    assert envs["default"]["vm_count"] == 3
    assert envs["dev"]["active"] is False
    assert envs["dev"]["port"] == 8990
    assert envs["dev"]["status"] == "reachable"
    assert envs["dev"]["vm_count"] == 3


def test_environments_reports_unreachable(monkeypatch):
    def _down_dev(cfg):
        if cfg.port == 8990:
            raise VCenterError("connection refused")
        return ("si", cfg.port)

    monkeypatch.setattr(api, "connect", _down_dev)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "list_inventory", _fake_inventory)
    client = TestClient(api.create_app(registry=_registry()))
    envs = {e["name"]: e for e in client.get("/api/environments").json()}
    assert envs["default"]["status"] == "reachable"
    assert envs["dev"]["status"] == "unreachable"
    assert envs["dev"]["vm_count"] is None
    assert "connection refused" in envs["dev"]["error"]


def test_switch_active_environment(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    assert client.get("/api/environment").json()["name"] == "default"

    resp = client.post("/api/environment", json={"name": "dev"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "dev"
    assert resp.json()["active"] is True
    assert client.get("/api/environment").json()["name"] == "dev"
    # The same endpoints now serve dev's VMs.
    names = [v["name"] for v in client.get("/api/vms").json()]
    assert names == ["dev-win10", "dev-ubuntu", "dev-ubuntu-template"]
    # And switch back.
    assert client.post("/api/environment", json={"name": "default"}).status_code == 200
    assert client.get("/api/environment").json()["name"] == "default"


def test_switch_unknown_environment_404(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.post("/api/environment", json={"name": "nope"})
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
    assert client.get("/api/environment").json()["name"] == "default"


def test_switch_unreachable_environment_503_and_stays(monkeypatch):
    def _down_dev(cfg):
        if cfg.port == 8990:
            raise VCenterError("connection refused")
        return ("si", cfg.port)

    monkeypatch.setattr(api, "connect", _down_dev)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "list_inventory", _fake_inventory)
    client = TestClient(api.create_app(registry=_registry()))
    resp = client.post("/api/environment", json={"name": "dev"})
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]
    # The active environment did not change.
    assert client.get("/api/environment").json()["name"] == "default"


def test_per_request_override_query_param(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms?env=dev")
    assert resp.status_code == 200
    assert resp.headers["X-Environment"] == "dev"
    names = [v["name"] for v in resp.json()]
    assert names == ["dev-win10", "dev-ubuntu", "dev-ubuntu-template"]
    # The active environment is untouched.
    assert client.get("/api/environment").json()["name"] == "default"
    # And the next request without the override serves default again.
    assert [v["name"] for v in client.get("/api/vms").json()][0] == "win11-secure"


def test_per_request_override_header(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms", headers={"X-Environment": "dev"})
    assert resp.status_code == 200
    assert resp.headers["X-Environment"] == "dev"
    assert [v["name"] for v in resp.json()][0] == "dev-win10"


def test_query_param_wins_over_header(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms?env=dev", headers={"X-Environment": "default"})
    assert resp.headers["X-Environment"] == "dev"
    assert [v["name"] for v in resp.json()][0] == "dev-win10"


def test_per_request_override_unknown_env_404(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms?env=nope")
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
    resp = client.get("/api/vms", headers={"X-Environment": "nope"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Reverse proxy
# ---------------------------------------------------------------------------


def test_proxy_forwards_get_and_response(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    resp = client.get("/about")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.headers["set-cookie"] == "vmware_soap_session=abc"
    assert resp.headers["X-Environment"] == "default"
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["method"] == "GET"
    assert forwarded["url"] == "https://127.0.0.1:8989/about"
    # Hop-by-hop / transport headers are not forwarded; the rest pass.
    assert "host" not in forwarded["headers"]
    assert "content-length" not in forwarded["headers"]


def test_proxy_forwards_soap_post_byte_identical(monkeypatch):
    _patch_proxy(monkeypatch)
    FakeAsyncClient.response = httpx.Response(
        200,
        headers={"content-type": "text/xml; charset=utf-8"},
        content=b"<soapenv:Envelope/>",
        request=httpx.Request("POST", "https://127.0.0.1:8989/sdk"),
    )
    client = _make_client(monkeypatch)
    body = b'<?xml version="1.0"?><soapenv:Envelope>login</soapenv:Envelope>'
    resp = client.post(
        "/sdk",
        content=body,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "Cookie": "vmware_soap_session=xyz",
            "Authorization": "Basic dXNlcjpwYXNz",
        },
    )
    assert resp.status_code == 200
    assert resp.content == b"<soapenv:Envelope/>"
    assert resp.headers["content-type"] == "text/xml; charset=utf-8"
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["method"] == "POST"
    assert forwarded["url"] == "https://127.0.0.1:8989/sdk"
    assert forwarded["content"] == body
    assert forwarded["headers"]["cookie"] == "vmware_soap_session=xyz"
    assert forwarded["headers"]["authorization"] == "Basic dXNlcjpwYXNz"
    assert forwarded["headers"]["content-type"] == "text/xml; charset=utf-8"


def test_proxy_preserves_query_string_and_status(monkeypatch):
    _patch_proxy(monkeypatch)
    FakeAsyncClient.response = httpx.Response(
        404,
        headers={"content-type": "text/html"},
        content=b"not found",
        request=httpx.Request("GET", "https://127.0.0.1:8989/nope"),
    )
    client = _make_client(monkeypatch)
    resp = client.get("/nope?x=1&y=2")
    assert resp.status_code == 404
    assert resp.content == b"not found"
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["url"] == "https://127.0.0.1:8989/nope?x=1&y=2"


def test_proxy_uses_configured_scheme_and_verify(monkeypatch):
    """Plain-HTTP vcsim (the container, -tls=false) gets http:// and
    verify=False; the default TLS vcsim gets https:// and verify=True."""
    _patch_proxy(monkeypatch)
    http_target = _target("default", 8989, scheme="http", insecure=True)
    app = api.create_app(registry=EnvironmentRegistry([http_target]))
    TestClient(app).get("/sdk")
    assert FakeAsyncClient.instances[0].kwargs["verify"] is False
    assert FakeAsyncClient.instances[0].kwargs["trust_env"] is False
    assert FakeAsyncClient.instances[0].requests[0]["url"] == "http://127.0.0.1:8989/sdk"

    FakeAsyncClient.instances.clear()
    tls_target = _target("default", 443, scheme="https", insecure=False)
    tls_target.vc_config.host = "vc.example"
    app = api.create_app(registry=EnvironmentRegistry([tls_target]))
    TestClient(app).get("/sdk")
    assert FakeAsyncClient.instances[0].kwargs["verify"] is True
    assert FakeAsyncClient.instances[0].requests[0]["url"] == "https://vc.example:443/sdk"


def test_proxy_unreachable_backend_is_502(monkeypatch):
    _patch_proxy(monkeypatch)
    FakeAsyncClient.error = httpx.ConnectError(
        "connection refused", request=httpx.Request("GET", "https://127.0.0.1:8989/about")
    )
    client = _make_client(monkeypatch)
    resp = client.get("/about")
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]


def test_proxy_env_param_pins_environment(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    resp = client.get("/sdk?env=dev")
    assert resp.status_code == 200
    assert resp.headers["X-Environment"] == "dev"
    forwarded = FakeAsyncClient.instances[0].requests[0]
    # Pinned to dev's vcsim, and the env pin is stripped from the
    # forwarded query string (vcsim knows nothing about it).
    assert forwarded["url"] == "https://127.0.0.1:8990/sdk"
    # The active environment is untouched.
    assert client.get("/api/environment").json()["name"] == "default"


def test_proxy_env_param_strips_only_env(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    client.get("/about?env=dev&x=1&y=2")
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["url"] == "https://127.0.0.1:8990/about?x=1&y=2"


def test_proxy_env_header_pins_environment(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    client.get("/sdk", headers={"X-Environment": "dev"})
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["url"] == "https://127.0.0.1:8990/sdk"


def test_proxy_unknown_env_404(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    resp = client.get("/sdk?env=nope")
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]
    assert FakeAsyncClient.instances == []  # nothing was forwarded


def test_proxy_pyvmomi_version_probe_mangling(monkeypatch):
    """pyVmomi's SmartConnect probes {path}/vimServiceVersions.xml —
    with path='/sdk?env=dev' that mangles into /sdk?env=dev/...xml
    (the probe suffix lands inside the query value). The proxy splits
    it back out: env dev, path /sdk/vimServiceVersions.xml."""
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    resp = client.get("/sdk?env=dev/vimServiceVersions.xml")
    assert resp.status_code == 200
    assert resp.headers["X-Environment"] == "dev"
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["url"] == "https://127.0.0.1:8990/sdk/vimServiceVersions.xml"


def test_proxy_follows_active_after_switch(monkeypatch):
    _patch_proxy(monkeypatch)
    client = _make_client(monkeypatch)
    assert client.post("/api/environment", json={"name": "dev"}).status_code == 200
    client.get("/sdk")
    forwarded = FakeAsyncClient.instances[0].requests[0]
    assert forwarded["url"] == "https://127.0.0.1:8990/sdk"
