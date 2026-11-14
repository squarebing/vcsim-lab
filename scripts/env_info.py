#!/usr/bin/env python3
"""Print environment details for shell scripts.

The shell scripts (tools/start-vcsim-binary.sh, docker-entrypoint.sh)
cannot parse YAML, so they ask this helper:

    python3 scripts/env_info.py --list
        one environment name per line, sorted (for the entrypoint loop)

    python3 scripts/env_info.py --env dev
        shell assignments for one environment, for eval:
            ENV_NAME=dev
            ENV_PORT=8990
            ENV_VCSIM_FLAGS='-api-version 8.0.3 -dc 1 -cluster 1 ...'
            ENV_LAB=environments/dev/vms.yaml
            ENV_STORAGE=environments/dev/storage.yaml
            ENV_MODEL_DIR=model/dev
        Values are shlex-quoted, so eval is safe (the flag list
        contains spaces — an unquoted assignment would run its second
        word as a command instead).

Usage (from the project root):
    eval "$(python3 scripts/env_info.py --env dev)"
    echo "starting $ENV_NAME on $ENV_PORT"
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vcsim_lab import config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Print environment details for shell scripts.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="print environment names, one per line")
    group.add_argument("--env", default=None, help="print shell assignments for this environment")
    parser.add_argument("--env-root", default=config.ENVIRONMENTS_DIR, help="environments directory")
    args = parser.parse_args()

    try:
        if args.list:
            for env in config.list_environments(args.env_root):
                print(env.name)
            return 0
        env = config.load_environment(args.env, args.env_root)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # shlex.quote every value: the flag list contains spaces, and an
    # unquoted `VAR=a b c` in eval would run `b` as a command instead
    # of assigning the whole string. Quoted values eval safely.
    print(f"ENV_NAME={shlex.quote(env.name)}")
    print(f"ENV_PORT={shlex.quote(str(env.port))}")
    print(f"ENV_VCSIM_FLAGS={shlex.quote(' '.join(env.vcsim_args()))}")
    print(f"ENV_LAB={shlex.quote(str(env.lab_path))}")
    print(f"ENV_STORAGE={shlex.quote(str(env.storage_path))}")
    print(f"ENV_MODEL_DIR={shlex.quote(str(env.model_dir))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
