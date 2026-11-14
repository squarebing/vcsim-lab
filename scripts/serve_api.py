#!/usr/bin/env python3
"""Serve the vcsim-lab inventory API (FastAPI front door).

Usage (from the project root, with vcsim already running):
    python scripts/serve_api.py                  # http://127.0.0.1:8000
    python scripts/serve_api.py --host 0.0.0.0 --port 9000
    API_PORT=8080 python scripts/serve_api.py

The API serves /api/* itself (inventory as JSON, from the same pyVmomi
read paths as scripts/query_lab.py) and reverse-proxies every other
path to vcsim — so /sdk and /about keep working on the same port.
It serves every environment in environments/ (see README "Multiple
environments"): /api/* serves the active environment by default
(--active-env, default default), POST /api/environment switches it,
and ?env=<name> selects one environment for a single request.

Connection settings come from the usual VC_* environment variables
(VC_HOST/VC_SCHEME/VC_USER/VC_PASSWORD — each environment takes its
own port from its env.yaml); the listen address from API_HOST/API_PORT
(API_PORT falls back to $PORT, which Render injects, then 8000).

See README "Inventory API" for the endpoint list and the front-door
design. No authentication: the API is read-only inventory, and the
service is public by design (see PROMPTS.md entry 15).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config  # noqa: E402
from vcsim_lab.api import create_app  # noqa: E402


def _default_port() -> int:
    """API_PORT, else $PORT (Render injects it), else 8000."""
    for var in ("API_PORT", "PORT"):
        value = os.environ.get(var)
        if value:
            return int(value)
    return 8000


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the vcsim-lab inventory API.")
    parser.add_argument(
        "--host",
        default=os.environ.get("API_HOST", "127.0.0.1"),
        help="listen address (default: $API_HOST or 127.0.0.1; use 0.0.0.0 in a container)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_default_port(),
        help="listen port (default: $API_PORT, $PORT, or 8000)",
    )
    parser.add_argument(
        "--env-root",
        default=config.ENVIRONMENTS_DIR,
        help="environments directory (default: environments)",
    )
    parser.add_argument(
        "--active-env",
        default=config.DEFAULT_ENVIRONMENT,
        help="environment served by default (default: default; switch at runtime with POST /api/environment)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn log level (default: info)",
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed; run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    try:
        app = create_app(env_root=args.env_root, active_env=args.active_env)
    except (ValueError, config.ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    registry = app.state.registry
    print(
        f"inventory API on http://{args.host}:{args.port}  (/api, /docs; /sdk proxied to vcsim)\n"
        f"environments: {', '.join(registry.names())} (active: {registry.active})"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
