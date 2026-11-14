#!/usr/bin/env python3
"""Verify the live lab matches an environment's vms.yaml. Exit code 0
when all core checks pass (TPM/GPU mismatches are warnings, not failures).

Usage (from the project root):
    python scripts/verify_lab.py [--env default] [--lab FILE] [--json]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config, verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the live lab against an environment's vms.yaml.")
    parser.add_argument("--env", default=config.DEFAULT_ENVIRONMENT,
                        help="environment to verify (default: default)")
    parser.add_argument("--lab", default=None,
                        help="lab file (default: the environment's vms.yaml)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        env = config.load_environment(args.env)
        lab = config.load_lab(args.lab or env.lab_path)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        result = verify.verify_lab(config.vcenter_config_for_env(env), lab)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"verify failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(verify.result_json(result))
    ok = verify.print_result(result)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
