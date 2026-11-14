"""Inventory API: a FastAPI front door for the vcsim lab.

Two jobs, on one port:

* ``/api/*`` — the inventory as JSON, backed by the same pyVmomi read
  paths as ``scripts/query_lab.py`` and ``scripts/query_storage.py``
  (``query.summarize_lab`` / ``list_inventory`` / ``list_host_pci``,
  ``storage.summarize_storage``). No new inventory logic lives here.
* Everything else — reverse-proxied to vcsim, so ``/sdk`` (the vSphere
  SOAP endpoint), ``/about`` (vcsim's methods/types page) and anything
  else vcsim serves keep working unchanged on the same port.

The intended deployment (see ``docker-entrypoint.sh``) runs vcsim on
an internal port (127.0.0.1:8989) and this API on the public port
(Render injects ``$PORT``); the API connects to vcsim as a client and
proxies the rest through. Locally, ``scripts/serve_api.py`` runs the
same app against a vcsim started by ``tools/start-vcsim-binary.sh``.

No auth: the service is public and the API is read-only inventory.
That was the user's explicit choice (see PROMPTS.md entry 15) — do not
add an API key without being asked.

pyVmomi is blocking, so the ``/api`` endpoints are sync ``def``
functions — FastAPI runs those in a thread pool. Each request opens its
own vCenter connection (a login per request is cheap against vcsim and
avoids stale-session handling entirely); when vcsim is unreachable the
endpoints answer 503 with a JSON message instead of crashing.
"""

from __future__ import annotations

import contextlib

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import config, query, storage
from .config import VCenterConfig
from .vcenter import VCenterError, connect, disconnect

# Sentinel for create_app() arguments: "not given" means "load from the
# file", while an explicit None means "do not load".
_UNSET: object = object()

# Hop-by-hop headers are per-connection, not per-request: they must not
# be forwarded to the backend or back to the client.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_PROXY_TIMEOUT = httpx.Timeout(60.0)

API_INDEX = {
    "name": "vcsim-lab inventory API",
    "description": (
        "Read-only inventory of the vcsim environment, served from the "
        "same pyVmomi read paths as scripts/query_lab.py. Everything "
        "outside /api is reverse-proxied to vcsim itself (/sdk, /about)."
    ),
    "docs": "/docs",
    "endpoints": {
        "/api/inventory": "full inventory in one response (VMs, hosts, datastores, networks, pools, vApps)",
        "/api/vms": "all VMs with full summaries (?template=true/false filters templates)",
        "/api/vms/{name}": "one VM by name",
        "/api/hosts": "hosts with PCI devices, GPU capability and their VMs",
        "/api/datastores": "datastores with capacity and mounts",
        "/api/networks": "networks (standard, distributed, NSX opaque)",
        "/api/vapps": "vApp containers with their member VMs",
        "/api/storage": "host storage: HBAs, LUNs, mounts, reconciled with storage.yaml",
        "/api/health": "API health + vcsim reachability",
    },
}


@contextlib.contextmanager
def _session(cfg: VCenterConfig):
    """A vCenter connection for one request; 503 when vcsim is down."""
    try:
        si = connect(cfg)
    except VCenterError as exc:
        raise HTTPException(status_code=503, detail=f"vCenter unreachable: {exc}") from exc
    try:
        yield si
    finally:
        disconnect(si)


def _vcsim_base(cfg: VCenterConfig) -> str:
    return f"{cfg.scheme}://{cfg.host}:{cfg.port}"


def create_app(
    vc_config: VCenterConfig | None = None,
    lab_path: str = "vms.yaml",
    storage_path: str = "storage.yaml",
    declared=_UNSET,
    storage_model=_UNSET,
) -> FastAPI:
    """Build the inventory API app.

    ``vc_config`` defaults to the environment (``VCenterConfig.from_env``,
    the same vars the scripts use). ``declared`` is the lab's per-VM
    declared values (seed-resolved attributes + disk layout); when not
    given it is loaded from ``lab_path`` and silently omitted when the
    file cannot be loaded, so the API still works live-only against a
    vCenter without a lab file. ``storage_model`` likewise comes from
    ``storage_path``; without it ``/api/storage`` reconciles against an
    empty model (live storage only).
    """
    cfg = vc_config or VCenterConfig.from_env()
    if declared is _UNSET:
        declared = config.load_declared_assignments(lab_path)
    if storage_model is _UNSET:
        try:
            storage_model = storage.load_storage(storage_path)
        except (storage.StorageError, OSError):
            storage_model = None

    app = FastAPI(
        title="vcsim-lab inventory API",
        description=(
            "Read-only inventory of the vcsim environment. /api/* is "
            "served here; everything else is reverse-proxied to vcsim "
            "(/sdk, /about). No authentication."
        ),
        version="1.0",
    )
    # The proxy reads the config from app.state so it always uses the
    # same connection settings as the /api endpoints.
    app.state.vc_config = cfg

    # ------------------------------------------------------------------
    # /api endpoints (sync: FastAPI runs them in a thread pool, which is
    # what the blocking pyVmomi calls need)
    # ------------------------------------------------------------------

    @app.get("/api", summary="Index of the inventory endpoints")
    def api_index() -> dict:
        return API_INDEX

    @app.get("/api/inventory", summary="Full inventory in one response")
    def api_inventory() -> dict:
        with _session(cfg) as si:
            inventory = query.list_inventory(si)
        inventory["counts"] = {k: len(v) for k, v in inventory.items()}
        return inventory

    @app.get("/api/vms", summary="All VMs with full summaries")
    def api_vms(template: bool | None = Query(default=None, description="filter to templates (true) or non-templates (false)")) -> list[dict]:
        with _session(cfg) as si:
            summaries = query.summarize_lab(si, declared=declared)
        if template is not None:
            summaries = [s for s in summaries if bool(s.get("template")) is template]
        return summaries

    @app.get("/api/vms/{name}", summary="One VM by name")
    def api_vm(name: str) -> dict:
        with _session(cfg) as si:
            summaries = query.summarize_lab(si, names=[name], declared=declared)
        if not summaries:
            raise HTTPException(status_code=404, detail=f"no VM named {name!r}")
        return summaries[0]

    @app.get("/api/hosts", summary="Hosts with PCI devices and their VMs")
    def api_hosts() -> list[dict]:
        with _session(cfg) as si:
            hosts = query.list_host_pci(si)
            summaries = query.summarize_lab(si)
        by_host: dict[str, list[str]] = {}
        for summary in summaries:
            by_host.setdefault(summary.get("host"), []).append(summary["name"])
        for host in hosts:
            names = sorted(by_host.get(host["host"], []))
            host["vms"] = names
            host["vm_count"] = len(names)
        return hosts

    @app.get("/api/datastores", summary="Datastores with capacity and mounts")
    def api_datastores() -> list[dict]:
        with _session(cfg) as si:
            return storage.live_datastore_mounts(si)

    @app.get("/api/networks", summary="Networks (standard, distributed, NSX opaque)")
    def api_networks() -> dict:
        with _session(cfg) as si:
            inventory = query.list_inventory(si)
        return {
            "networks": inventory["networks"],
            "dvportgroups": inventory["dvportgroups"],
            "count": len(inventory["networks"]),
        }

    @app.get("/api/vapps", summary="vApp containers with their member VMs")
    def api_vapps() -> list[dict]:
        with _session(cfg) as si:
            return query.list_vapps(si)

    @app.get("/api/storage", summary="Host storage, reconciled with storage.yaml")
    def api_storage() -> dict:
        with _session(cfg) as si:
            live = storage.live_storage(si)
        # An empty model reconciles cleanly (live storage only, with
        # notes saying nothing is declared) — so the response shape is
        # the same whether or not storage.yaml was loaded.
        model = storage_model or storage.StorageModel()
        report = storage.reconcile(model, live)
        if storage_model is None:
            report["notes"] = [
                "storage.yaml not loaded; live storage only, nothing declared to reconcile against"
            ] + report["notes"]
        return report

    @app.get("/api/health", summary="API health + vcsim reachability")
    def api_health() -> Response:
        sdk = f"{_vcsim_base(cfg)}/sdk"
        try:
            si = connect(cfg)
        except VCenterError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "vcsim": "unreachable",
                    "sdk": sdk,
                    "error": str(exc),
                },
            )
        disconnect(si)
        return JSONResponse(
            content={"status": "ok", "vcsim": "reachable", "sdk": sdk}
        )

    # Unknown /api paths are a JSON 404, not a proxy to vcsim (whose
    # 404 is an HTML page). Registered before the proxy catch-all.
    @app.api_route("/api/{rest:path}", methods=_PROXY_METHODS, include_in_schema=False)
    def api_not_found(rest: str) -> Response:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown API path /api/{rest}"}
        )

    # ------------------------------------------------------------------
    # Reverse proxy: everything that is not /api (and not the docs) goes
    # to vcsim. Registered LAST so the /api routes and FastAPI's own
    # /docs, /redoc and /openapi.json win. The proxy is async (httpx);
    # the /api endpoints above are sync (pyVmomi) — FastAPI runs both.
    #
    # SOAP note: pyVmomi POSTs XML to /sdk with Content-Type: text/xml
    # and keeps a session cookie (vmware_soap_session) after login, so
    # the body is forwarded byte-identical and Cookie/Authorization in
    # and Set-Cookie out pass through untouched.
    # ------------------------------------------------------------------
    @app.api_route("/{full_path:path}", methods=_PROXY_METHODS, include_in_schema=False)
    async def proxy_to_vcsim(request: Request, full_path: str) -> Response:
        backend = _vcsim_base(request.app.state.vc_config)
        url = f"{backend}/{full_path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() not in ("host", "content-length")
        }
        body = await request.body()
        try:
            # trust_env=False: the backend is vcsim on a local/internal
            # address and must never go through an environment HTTP
            # proxy — and httpx's env-proxy parsing can choke on
            # unusual NO_PROXY entries (observed: a bare "::1" entry
            # raised InvalidURL at client construction time).
            async with httpx.AsyncClient(
                verify=not cfg.insecure, timeout=_PROXY_TIMEOUT, trust_env=False
            ) as client:
                upstream = await client.request(
                    request.method, url, headers=headers, content=body
                )
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": f"vcsim unreachable at {backend}: {exc}"},
            )
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    return app


def main() -> None:  # pragma: no cover - thin wrapper, see scripts/serve_api.py
    """Run the API with uvicorn (``python -m vcsim_lab.api``)."""
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
