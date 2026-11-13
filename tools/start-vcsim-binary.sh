#!/usr/bin/env bash
# Start vcsim without Docker: downloads the govmomi vcsim binary for the
# host OS/arch (once, into ./bin) and runs it with the same inventory
# flags as docker-compose.yml.
#
#   tools/start-vcsim-binary.sh
#   tools/stop-vcsim-binary.sh
#
# Override the release with VCSIM_VERSION (default v0.56.2) and the
# listen address with VCSIM_LISTEN (default 127.0.0.1:8989).
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VCSIM_VERSION:-v0.56.2}"
LISTEN="${VCSIM_LISTEN:-127.0.0.1:8989}"
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

if [ -f "$BIN_DIR/vcsim.pid" ] && kill -0 "$(cat "$BIN_DIR/vcsim.pid")" 2>/dev/null; then
  echo "vcsim already running (pid $(cat "$BIN_DIR/vcsim.pid")) on $LISTEN"
  exit 0
fi

# -load: if ./model exists and is non-empty, start from the saved model
# (see tools/save-model.sh) instead of generating a fresh inventory.
LOAD_ARGS=()
if [ -d "model" ] && [ -n "$(ls -A model 2>/dev/null)" ]; then
  echo "saved model found in ./model — starting vcsim with -load"
  LOAD_ARGS=(-load "$PWD/model")
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
LOG="$PWD/$BIN_DIR/vcsim.log"
PID_FILE="$PWD/$BIN_DIR/vcsim.pid"

echo "starting vcsim $VERSION on $LISTEN (work dir: $WORK_DIR) ..."
(
  cd "$WORK_DIR" || exit 1
  exec nohup "$BIN_ABS" "${LOAD_ARGS[@]}" \
    -l "$LISTEN" \
    -api-version 8.0.3 \
    -dc 1 -cluster 1 -host 3 -standalone-host 1 \
    -ds 3 -pod 1 -pg 2 -nsx 1 -pool 1 -vm 0 \
    -username "${VCSIM_USERNAME:-user}" \
    -password "${VCSIM_PASSWORD:-pass}" \
    > "$LOG" 2>&1
) &
echo $! > "$PID_FILE"
echo "pid $(cat "$PID_FILE"), log: $BIN_DIR/vcsim.log"
echo "SDK endpoint: https://${LISTEN}/sdk"
