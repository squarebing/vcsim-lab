"""Inventory API: a FastAPI front door for the vcsim environments.

Two jobs, on one port:

* ``/api/*`` — the inventory as JSON, backed by the same pyVmomi read
  paths as ``scripts/query_lab.py`` and ``scripts/query_storage.py``
  (``query.summarize_lab`` / ``list_inventory`` / ``list_host_pci``,
  ``storage.summarize_storage``). No new inventory logic lives here.
* Everything else — reverse-proxied to vcsim, so ``/sdk`` (the vSphere
  SOAP endpoint), ``/about`` (vcsim's methods/types page) and anything
  else vcsim serves keep working unchanged on the same port.

Multiple environments: the repo holds one vCenter simulation per
directory under ``environments/`` (see ``vcsim_lab.config``), each
with its own vcsim process on its own port. The API serves all of
them from the same endpoints, and the caller picks which one two ways:

* Globally: ``POST /api/environment {"name": "<env>"}`` switches the
  active environment (in-memory; restarts default to ``default``).
  All ``/api/*`` endpoints and the proxy then serve that environment.
* Per request: ``?env=<name>`` (or the ``X-Environment`` header; the
  query param wins) serves one request from that environment without
  changing the active one. The proxy honours it too — ``/sdk?env=dev``
  pins one SOAP session to the dev environment.

SOAP-session caveat: a pyVmomi session (cookie) belongs to one vcsim,
so do not switch the active environment mid-session — reconnect after
switching, or pin the session with ``/sdk?env=<name>``.

The intended deployment (see ``docker-entrypoint.sh``) runs one vcsim
per environment on internal ports (127.0.0.1:8989, 8990, ...) and this
API on the public port (Render injects ``$PORT``); the API connects to
each vcsim as a client and proxies the rest through. Locally,
``scripts/serve_api.py`` runs the same app against vcsim instances
started by ``tools/start-vcsim-binary.sh --env <name>``.

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
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, query, storage
from .config import VCenterConfig
from .vcenter import VCenterError, connect, disconnect

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
        "Read-only inventory of the vcsim environments, served from the "
        "same pyVmomi read paths as scripts/query_lab.py. Everything "
        "outside /api is reverse-proxied to the active environment's "
        "vcsim (/sdk, /about). Switch environments with "
        "POST /api/environment or per request with ?env=<name>."
    ),
    "docs": "/docs",
    "endpoints": {
        "/api/environments": "all environments: name, description, port, status, VM counts",
        "/api/environment": "GET the active environment; POST {\"name\": ...} to switch it",
        "/api/inventory": "full inventory in one response (VMs, hosts, datastores, networks, pools, vApps)",
        "/api/vms": "all VMs with full summaries (?template=true/false filters templates)",
        "/api/vms/{name}": "one VM by name",
        "/api/hosts": "hosts with PCI devices, GPU capability and their VMs",
        "/api/datastores": "datastores with capacity and mounts",
        "/api/networks": "networks (standard, distributed, NSX opaque)",
        "/api/vapps": "vApp containers with their member VMs",
        "/api/storage": "host storage: HBAs, LUNs, mounts, reconciled with the environment's storage.yaml",
        "/api/health": "API health + per-environment vcsim reachability",
    },
}


# ---------------------------------------------------------------------------
# Environment registry
# ---------------------------------------------------------------------------


@dataclass
class EnvironmentTarget:
    """One environment as the API sees it: its identity, connection
    settings, and its declared lab/storage models (loaded at startup,
    best-effort — None when the files cannot be loaded, so the API
    still works live-only against that environment)."""

    env: config.Environment
    vc_config: VCenterConfig
    declared: dict | None = None
    storage_model: storage.StorageModel | None = None

    @property
    def name(self) -> str:
        return self.env.name

    @property
    def description(self) -> str:
        return self.env.description

    @property
    def port(self) -> int:
        return self.env.port

    @property
    def sdk_url(self) -> str:
        return f"{self.vc_config.scheme}://{self.vc_config.host}:{self.vc_config.port}/sdk"

    def info(self) -> dict:
        """The environment as /api/environments and /api/environment
        describe it (before the live status is added)."""
        return {
            "name": self.name,
            "description": self.description,
            "port": self.port,
            "sdk": self.sdk_url,
            "declared_vm_count": len(self.declared) if self.declared is not None else None,
        }


class EnvironmentRegistry:
    """The environments the API serves, plus the active one.

    The active environment is in-memory state: POST /api/environment
    changes it for the whole process, and a restart defaults it back
    to ``default``. Per-request selection (?env= / X-Environment)
    never touches it.
    """

    def __init__(
        self,
        targets: dict[str, EnvironmentTarget] | list[EnvironmentTarget],
        active: str = config.DEFAULT_ENVIRONMENT,
    ):
        self.targets: dict[str, EnvironmentTarget] = (
            dict(targets)
            if isinstance(targets, dict)
            else {t.name: t for t in targets}
        )
        if not self.targets:
            raise ValueError("no environments to serve")
        if active not in self.targets:
            raise ValueError(
                f"unknown active environment {active!r}; available: "
                f"{', '.join(sorted(self.targets))}"
            )
        self.active = active

    @classmethod
    def discover(
        cls,
        env_root: str | Path = config.ENVIRONMENTS_DIR,
        base_vc_config: VCenterConfig | None = None,
        active: str = config.DEFAULT_ENVIRONMENT,
    ) -> "EnvironmentRegistry":
        """Build the registry from ``environments/*/env.yaml``: one
        target per environment, each with its own vcsim port and its
        declared lab/storage models loaded best-effort. With no
        environments directory at all, fall back to a single synthetic
        ``default`` target using the base connection settings (live
        only) — so the API still works against a bare vCenter."""
        base = base_vc_config or VCenterConfig.from_env()
        envs = config.list_environments(env_root)
        if not envs:
            synthetic = config.Environment(
                name=config.DEFAULT_ENVIRONMENT,
                description="live vCenter (no environments/ directory)",
                port=base.port,
                vcsim=dict(config.VCSIM_FLAG_DEFAULTS),
                directory=Path("."),
                root=Path("."),
            )
            return cls([EnvironmentTarget(env=synthetic, vc_config=base)], active="default")
        targets = []
        for env in envs:
            declared = config.load_declared_assignments(env.lab_path)
            try:
                model = storage.load_storage(env.storage_path)
            except (storage.StorageError, OSError):
                model = None
            targets.append(
                EnvironmentTarget(
                    env=env,
                    vc_config=env.vcenter_config(base),
                    declared=declared,
                    storage_model=model,
                )
            )
        return cls(targets, active=active)

    def names(self) -> list[str]:
        return sorted(self.targets)

    def get(self, name: str) -> EnvironmentTarget:
        """The target called ``name``; raises KeyError when unknown."""
        return self.targets[name]

    def active_target(self) -> EnvironmentTarget:
        return self.targets[self.active]

    def set_active(self, name: str) -> EnvironmentTarget:
        """Switch the active environment; raises KeyError when unknown."""
        target = self.targets[name]
        self.active = name
        return target

    def resolve(self, name: str | None) -> EnvironmentTarget:
        """The target for one request: ``name`` when given (the ?env=
        query param or the X-Environment header), else the active
        environment. Raises KeyError for an unknown name."""
        if name:
            return self.targets[name]
        return self.active_target()


class SwitchEnvironmentRequest(BaseModel):
    """POST /api/environment body: the environment to switch to."""

    name: str


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


def _probe(target: EnvironmentTarget) -> dict:
    """Best-effort live status for one environment: reachable or not,
    and the live VM count when reachable. Used by /api/environments
    and /api/health; never raises."""
    info = target.info()
    try:
        si = connect(target.vc_config)
    except VCenterError as exc:
        info["status"] = "unreachable"
        info["vm_count"] = None
        info["error"] = str(exc)
        return info
    try:
        inventory = query.list_inventory(si)
        info["status"] = "reachable"
        info["vm_count"] = len(inventory["vms"])
    except Exception as exc:  # noqa: BLE001 — status probe must not raise
        info["status"] = "unreachable"
        info["vm_count"] = None
        info["error"] = str(exc)
    finally:
        disconnect(si)
    return info


def create_app(
    vc_config: VCenterConfig | None = None,
    env_root: str | Path = config.ENVIRONMENTS_DIR,
    registry: EnvironmentRegistry | None = None,
    active_env: str = config.DEFAULT_ENVIRONMENT,
) -> FastAPI:
    """Build the inventory API app.

    ``registry`` is the environment set to serve; when not given it is
    discovered from ``env_root`` (``environments/*/env.yaml``) with
    connection settings from ``vc_config`` (default: the VC_*
    environment variables, the same vars the scripts use — each
    environment takes its own port from env.yaml). ``active_env`` is
    the environment served by default until POST /api/environment
    switches it.
    """
    if registry is None:
        registry = EnvironmentRegistry.discover(
            env_root, vc_config or VCenterConfig.from_env(), active=active_env
        )
    elif active_env != config.DEFAULT_ENVIRONMENT:
        # An explicitly passed registry with an explicit active env:
        # honour the active env (unknown name -> clear startup failure).
        try:
            registry.set_active(active_env)
        except KeyError:
            raise ValueError(
                f"unknown active environment {active_env!r}; available: "
                f"{', '.join(registry.names())}"
            ) from None

    app = FastAPI(
        title="vcsim-lab inventory API",
        description=(
            "Read-only inventory of the vcsim environments. /api/* is "
            "served here; everything else is reverse-proxied to the "
            "active environment's vcsim (/sdk, /about). Switch "
            "environments with POST /api/environment or per request "
            "with ?env=<name>. No authentication."
        ),
        version="1.0",
    )
    # The proxy and the endpoints read the registry from app.state.
    app.state.registry = registry

    def _resolve_target(
        request: Request,
        response: Response,
        env: str | None = Query(
            default=None,
            description="serve this request from another environment without changing the active one",
        ),
    ) -> EnvironmentTarget:
        """The environment for one /api request: the ?env= query param
        wins, then the X-Environment header, then the active
        environment. Unknown names are a JSON 404. The resolved
        environment is also stamped on the X-Environment response
        header so clients can see which environment answered."""
        name = env or request.headers.get("X-Environment")
        try:
            target = registry.resolve(name)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown environment {name!r}; available: {', '.join(registry.names())}",
            ) from None
        response.headers["X-Environment"] = target.name
        return target

    # ------------------------------------------------------------------
    # /api endpoints (sync: FastAPI runs them in a thread pool, which is
    # what the blocking pyVmomi calls need)
    # ------------------------------------------------------------------

    @app.get("/api", summary="Index of the inventory endpoints")
    def api_index() -> dict:
        index = dict(API_INDEX)
        index["active_environment"] = registry.active
        index["environments"] = registry.names()
        return index

    @app.get("/api/environments", summary="All environments with live status")
    def api_environments() -> list[dict]:
        return [
            {**_probe(target), "active": target.name == registry.active}
            for target in (registry.targets[name] for name in registry.names())
        ]

    @app.get("/api/environment", summary="The active environment")
    def api_get_environment() -> dict:
        target = registry.active_target()
        return {**target.info(), "active": True}

    @app.post("/api/environment", summary="Switch the active environment")
    def api_set_environment(body: SwitchEnvironmentRequest) -> dict:
        try:
            target = registry.get(body.name)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown environment {body.name!r}; available: {', '.join(registry.names())}",
            ) from None
        # Fail fast: do not leave the API pointing at a dead
        # environment. A connect here only checks reachability; the
        # endpoints open their own connections per request.
        try:
            si = connect(target.vc_config)
        except VCenterError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"environment {body.name!r} is unreachable: {exc}",
            ) from exc
        disconnect(si)
        registry.set_active(body.name)
        return {**target.info(), "active": True}

    @app.get("/api/inventory", summary="Full inventory in one response")
    def api_inventory(target: EnvironmentTarget = Depends(_resolve_target)) -> dict:
        with _session(target.vc_config) as si:
            inventory = query.list_inventory(si)
        inventory["environment"] = target.name
        inventory["counts"] = {k: len(v) for k, v in inventory.items() if isinstance(v, list)}
        return inventory

    @app.get("/api/vms", summary="All VMs with full summaries")
    def api_vms(
        target: EnvironmentTarget = Depends(_resolve_target),
        template: bool | None = Query(default=None, description="filter to templates (true) or non-templates (false)"),
    ) -> list[dict]:
        with _session(target.vc_config) as si:
            summaries = query.summarize_lab(si, declared=target.declared)
        if template is not None:
            summaries = [s for s in summaries if bool(s.get("template")) is template]
        return summaries

    @app.get("/api/vms/{name}", summary="One VM by name")
    def api_vm(name: str, target: EnvironmentTarget = Depends(_resolve_target)) -> dict:
        with _session(target.vc_config) as si:
            summaries = query.summarize_lab(si, names=[name], declared=target.declared)
        if not summaries:
            raise HTTPException(status_code=404, detail=f"no VM named {name!r} in environment {target.name!r}")
        return summaries[0]

    @app.get("/api/hosts", summary="Hosts with PCI devices and their VMs")
    def api_hosts(target: EnvironmentTarget = Depends(_resolve_target)) -> list[dict]:
        with _session(target.vc_config) as si:
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
    def api_datastores(target: EnvironmentTarget = Depends(_resolve_target)) -> list[dict]:
        with _session(target.vc_config) as si:
            return storage.live_datastore_mounts(si)

    @app.get("/api/networks", summary="Networks (standard, distributed, NSX opaque)")
    def api_networks(target: EnvironmentTarget = Depends(_resolve_target)) -> dict:
        with _session(target.vc_config) as si:
            inventory = query.list_inventory(si)
        return {
            "environment": target.name,
            "networks": inventory["networks"],
            "dvportgroups": inventory["dvportgroups"],
            "count": len(inventory["networks"]),
        }

    @app.get("/api/vapps", summary="vApp containers with their member VMs")
    def api_vapps(target: EnvironmentTarget = Depends(_resolve_target)) -> list[dict]:
        with _session(target.vc_config) as si:
            return query.list_vapps(si)

    @app.get("/api/storage", summary="Host storage, reconciled with the environment's storage.yaml")
    def api_storage(target: EnvironmentTarget = Depends(_resolve_target)) -> dict:
        with _session(target.vc_config) as si:
            live = storage.live_storage(si)
        # An empty model reconciles cleanly (live storage only, with
        # notes saying nothing is declared) — so the response shape is
        # the same whether or not the environment's storage.yaml loaded.
        model = target.storage_model or storage.StorageModel()
        report = storage.reconcile(model, live)
        report["environment"] = target.name
        if target.storage_model is None:
            report["notes"] = [
                f"storage model for environment {target.name!r} not loaded "
                f"({target.env.storage_path}); live storage only, nothing declared to reconcile against"
            ] + report["notes"]
        return report

    @app.get("/api/health", summary="API health + per-environment vcsim reachability")
    def api_health() -> Response:
        statuses = {
            name: _probe(registry.targets[name]) for name in registry.names()
        }
        active = statuses[registry.active]
        ok = active["status"] == "reachable"
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "degraded",
                "active_environment": registry.active,
                "vcsim": active["status"],
                "sdk": active["sdk"],
                "environments": statuses,
            },
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
    # to the selected environment's vcsim — ?env=<name> (or the
    # X-Environment header) pins the request to one environment,
    # otherwise the active environment serves it. Registered LAST so
    # the /api routes and FastAPI's own /docs, /redoc and /openapi.json
    # win. The proxy is async (httpx); the /api endpoints above are
    # sync (pyVmomi) — FastAPI runs both.
    #
    # SOAP note: pyVmomi POSTs XML to /sdk with Content-Type: text/xml
    # and keeps a session cookie (vmware_soap_session) after login, so
    # the body is forwarded byte-identical and Cookie/Authorization in
    # and Set-Cookie out pass through untouched. The ?env= pin is
    # stripped from the forwarded query string (vcsim knows nothing
    # about it); all other query params pass through. A session cookie
    # belongs to one vcsim — do not switch the active environment
    # mid-session; reconnect, or pin with /sdk?env=<name> (or, for
    # pyVmomi, SmartConnect(customHeaders={"X-Environment": ...})).
    # ------------------------------------------------------------------
    @app.api_route("/{full_path:path}", methods=_PROXY_METHODS, include_in_schema=False)
    async def proxy_to_vcsim(request: Request, full_path: str) -> Response:
        registry_: EnvironmentRegistry = request.app.state.registry
        name = request.query_params.get("env") or request.headers.get("X-Environment")
        # pyVmomi's SmartConnect probes {path}/vimServiceVersions.xml to
        # pick the API version — with path='/sdk?env=dev' that mangles
        # into /sdk?env=dev/vimServiceVersions.xml (the probe suffix
        # lands inside the query value). Environment names never
        # contain '/', so split it back out: the env is the part before
        # the suffix, and the suffix rejoins the forwarded path.
        probe_suffix = ""
        if name and name.endswith("/vimServiceVersions.xml"):
            name = name[: -len("/vimServiceVersions.xml")]
            probe_suffix = "/vimServiceVersions.xml"
        try:
            target = registry_.resolve(name)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"unknown environment {name!r}; available: {', '.join(registry_.names())}"
                },
            )
        cfg = target.vc_config
        backend = _vcsim_base(cfg)
        url = f"{backend}/{full_path}{probe_suffix}"
        # Forward every query param except the env pin.
        rest = [(k, v) for k, v in request.query_params.multi_items() if k != "env"]
        if rest:
            url = f"{url}?{'&'.join(f'{k}={v}' for k, v in rest)}"
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
        response_headers["X-Environment"] = target.name
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
