"""Offline tests for the inventory API (vcsim_lab/api.py): no vCenter needed.

The vCenter layer is mocked (connect/query/storage functions), and the
reverse proxy is tested against a fake httpx client that records what
it was asked to forward — so the suite proves the routing, filtering,
error handling and proxy forwarding without any live server.

Run from the project root:

    python -m pytest tests/ -q
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import api, storage  # noqa: E402
from vcsim_lab.config import VCenterConfig  # noqa: E402
from vcsim_lab.vcenter import VCenterError  # noqa: E402

_SI = object()  # fake ServiceInstance sentinel

_CANNED_VMS = [
    {"name": "win11-secure", "template": False, "host": "DC0_H0", "power": "poweredOn"},
    {"name": "win2022-template", "template": True, "host": "DC0_H0", "power": "poweredOff"},
    {"name": "ubuntu-k8s-node", "template": False, "host": "DC0_C0_H0", "power": "suspended"},
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


def _fake_summarize(si, names=None, declared=None):
    """summarize_lab stand-in that honours the names filter."""
    if names:
        wanted = set(names)
        return [dict(s) for s in _CANNED_VMS if s["name"] in wanted]
    return [dict(s) for s in _CANNED_VMS]


def _make_client(monkeypatch, **app_kwargs):
    """A TestClient for an app whose vCenter connection is mocked.

    declared={} and an empty StorageModel keep the app hermetic (no
    vms.yaml / storage.yaml reads); individual tests patch the query /
    storage functions they exercise.
    """
    monkeypatch.setattr(api, "connect", lambda cfg: _SI)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    app_kwargs.setdefault("declared", {})
    app_kwargs.setdefault("storage_model", storage.StorageModel())
    app = api.create_app(**app_kwargs)
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


def test_api_index_lists_endpoints(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docs"] == "/docs"
    for path in ("/api/inventory", "/api/vms", "/api/hosts", "/api/datastores",
                 "/api/networks", "/api/vapps", "/api/storage", "/api/health"):
        assert path in body["endpoints"]


def test_vms_returns_summaries(monkeypatch):
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    client = _make_client(monkeypatch)
    resp = client.get("/api/vms")
    assert resp.status_code == 200
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


def test_inventory_includes_counts(monkeypatch):
    monkeypatch.setattr(api.query, "list_inventory", lambda si: dict(_CANNED_INVENTORY))
    client = _make_client(monkeypatch)
    resp = client.get("/api/inventory")
    assert resp.status_code == 200
    body = resp.json()
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
    assert body["hosts"][0]["name"] == "DC0_H0"
    assert body["datastores"][0]["name"] == "LocalDS_0"
    assert "storage.yaml not loaded" not in " ".join(body["notes"])


def test_storage_without_model_notes_it(monkeypatch):
    monkeypatch.setattr(storage, "live_storage", lambda si: _CANNED_LIVE_STORAGE)
    client = _make_client(monkeypatch, storage_model=None)
    resp = client.get("/api/storage")
    assert resp.status_code == 200
    assert any("storage.yaml not loaded" in n for n in resp.json()["notes"])


def test_health_ok_and_down(monkeypatch):
    client = _make_client(monkeypatch)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["vcsim"] == "reachable"

    def _boom(cfg):
        raise VCenterError("connection refused")

    monkeypatch.setattr(api, "connect", _boom)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
    assert resp.json()["vcsim"] == "unreachable"


def test_vms_down_returns_503(monkeypatch):
    def _boom(cfg):
        raise VCenterError("connection refused")

    monkeypatch.setattr(api, "connect", _boom)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    app = api.create_app(declared={}, storage_model=storage.StorageModel())
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


def test_create_app_missing_lab_files(monkeypatch):
    """No vms.yaml / storage.yaml: the app still builds, live-only."""
    monkeypatch.setattr(api, "connect", lambda cfg: _SI)
    monkeypatch.setattr(api, "disconnect", lambda si: None)
    monkeypatch.setattr(api.query, "summarize_lab", _fake_summarize)
    app = api.create_app(lab_path="no-such-lab.yaml", storage_path="no-such-storage.yaml")
    client = TestClient(app)
    assert client.get("/api/vms").status_code == 200


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
    cfg = VCenterConfig(host="127.0.0.1", port=8989, scheme="http", insecure=True)
    app = api.create_app(vc_config=cfg, declared={}, storage_model=storage.StorageModel())
    TestClient(app).get("/sdk")
    assert FakeAsyncClient.instances[0].kwargs["verify"] is False
    assert FakeAsyncClient.instances[0].kwargs["trust_env"] is False
    assert FakeAsyncClient.instances[0].requests[0]["url"] == "http://127.0.0.1:8989/sdk"

    FakeAsyncClient.instances.clear()
    cfg = VCenterConfig(host="vc.example", port=443, scheme="https", insecure=False)
    app = api.create_app(vc_config=cfg, declared={}, storage_model=storage.StorageModel())
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
