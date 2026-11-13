#!/usr/bin/env python3
"""Delete the lab VMs (power off first if needed).

Usage (from the project root):
    python scripts/teardown_lab.py [--lab vms.yaml] [--only NAME ...]
                                   [--remove-folders] [--remove-pools]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config, teardown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete the lab VMs.")
    parser.add_argument("--lab", default="vms.yaml")
    parser.add_argument("--only", nargs="+", help="delete only these VMs")
    parser.add_argument("--remove-folders", action="store_true")
    parser.add_argument("--remove-pools", action="store_true")
    args = parser.parse_args()

    try:
        lab = config.load_lab(args.lab)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        log = teardown.teardown_lab(
            config.VCenterConfig.from_env(),
            lab,
            only=args.only,
            remove_folders=args.remove_folders,
            remove_pools=args.remove_pools,
        )
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"teardown failed: {exc}", file=sys.stderr)
        return 1
    for line in log:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
