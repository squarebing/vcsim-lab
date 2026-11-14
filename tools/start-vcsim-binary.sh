#!/usr/bin/env bash
# Start vcsim without Docker: downloads the govmomi vcsim binary for the
# host OS/arch (once, into ./bin) and runs it with the inventory flags
# from an environment's env.yaml.
#
#   tools/start-vcsim-binary.sh [--env default]
#   tools/start-vcsim-binary.sh --env dev
#   tools/stop-vcsim-binary.sh [--env default | --all]
#
# Each environment gets its own vcsim process (own port, pid file and
# log: bin/vcsim-<env>.pid / bin/vcsim-<env>.log), so several can run
# side by side. Override the release with VCSIM_VERSION (default
# v0.56.2) and the listen address with VCSIM_LISTEN (default
# 127.0.0.1:<env port>).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV="${1:-}"
if [ "$ENV" = "--env" ]; then
  ENV="${2:-}"
  if [ -z "$ENV" ]; then echo "usage: $0 [--env NAME]" >&2; exit 2; fi
elif [ -n "$ENV" ]; then
  echo "usage: $0 [--env NAME]" >&2
  exit 2
else
  ENV="default"
fi

# Environment details from env.yaml (port, vcsim flags, model dir) —
# scripts/env_info.py prints shell assignments; eval them.
if ! eval "$(python3 scripts/env_info.py --env "$ENV")"; then
  echo "cannot load environment '$ENV' (is PyYAML installed? pip install -r requirements.txt)" >&2
  exit 2
fi

VERSION="${VCSIM_VERSION:-v0.56.2}"
LISTEN="${VCSIM_LISTEN:-127.0.0.1:${ENV_PORT}}"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac

BIN_DIR="bin"
BIN="$BIN_DIR/vcsim"
if [ ! -x "$BIN" ]; then
  echo "downloading vcsim $VERSION for $OS/$ARCH ..."
  mkdir -p "$BIN_DIR"
  URL="https://github.com/vmware/govmomi/releases/download/${VERSION}/vcsim_${OS}_${ARCH}.tar.gz"
  curl -L -o "$BIN_DIR/vcsim.tar.gz" "$URL"
  tar --no-same-owner -C "$BIN_DIR" -xzf "$BIN_DIR/vcsim.tar.gz" vcsim
  rm "$BIN_DIR/vcsim.tar.gz"
  chmod +x "$BIN"
fi

PID_FILE="$PWD/$BIN_DIR/vcsim-${ENV}.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "vcsim ($ENV) already running (pid $(cat "$PID_FILE")) on $LISTEN"
  exit 0
fi

# -load: if model/<env> exists and is non-empty, start from the saved
# model (see tools/save-model.sh) instead of generating a fresh inventory.
LOAD_ARGS=()
if [ -d "$ENV_MODEL_DIR" ] && [ -n "$(ls -A "$ENV_MODEL_DIR" 2>/dev/null)" ]; then
  echo "saved model found in ./$ENV_MODEL_DIR — starting vcsim with -load"
  LOAD_ARGS=(-load "$PWD/$ENV_MODEL_DIR")
fi

# Run vcsim from a scratch working directory: on the -load path vcsim
# never sets up its temp dir, so it creates the datastore backing
# directories (DC0-LocalDS_*) in its cwd — and refuses to start if they
# already exist there. A fresh scratch dir per start avoids both the
# clutter and the collision. (Fresh-create runs use /tmp/govcsim-*
# either way.)
WORK_DIR="${VCSIM_WORK_DIR:-$(mktemp -d /tmp/vcsim-work-XXXXXX)}"
mkdir -p "$WORK_DIR"
BIN_ABS="$PWD/$BIN"
LOG="$PWD/$BIN_DIR/vcsim-${ENV}.log"

echo "starting vcsim $VERSION for environment '$ENV' on $LISTEN (work dir: $WORK_DIR) ..."
(
  cd "$WORK_DIR" || exit 1
  # shellcheck disable=SC2086 — $ENV_VCSIM_FLAGS is a space-separated
  # flag list from env.yaml (validated by vcsim_lab.config).
  exec nohup "$BIN_ABS" "${LOAD_ARGS[@]}" \
    -l "$LISTEN" \
    $ENV_VCSIM_FLAGS \
    -vm 0 \
    -username "${VCSIM_USERNAME:-user}" \
    -password "${VCSIM_PASSWORD:-pass}" \
    > "$LOG" 2>&1
) &
echo $! > "$PID_FILE"
echo "pid $(cat "$PID_FILE"), log: $BIN_DIR/vcsim-${ENV}.log"
echo "SDK endpoint: https://${LISTEN}/sdk"
