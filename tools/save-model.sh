#!/usr/bin/env bash
# Save the running vcsim inventory to ./model so a later vcsim can start
# from it with `-load` (fast restarts, same inventory, no rebuild).
#
#   tools/save-model.sh
#
# Requires govc (downloaded once into ./bin if missing). The vcsim
# instance must be running and the lab built.
#
# After saving, restart vcsim and it will pick the model up:
#   - binary: tools/stop-vcsim-binary.sh && tools/start-vcsim-binary.sh
#     (start script uses ./model automatically when present)
#   - docker: uncomment the volume + -load command in docker-compose.yml,
#     then: docker compose down && docker compose up -d
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VCSIM_VERSION:-v0.56.2}"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac

BIN_DIR="bin"
GOVC="$BIN_DIR/govc"
if [ ! -x "$GOVC" ]; then
  echo "downloading govc $VERSION for $OS/$ARCH ..."
  mkdir -p "$BIN_DIR"
  URL="https://github.com/vmware/govmomi/releases/download/${VERSION}/govc_${OS}_${ARCH}.tar.gz"
  curl -L -o "$BIN_DIR/govc.tar.gz" "$URL"
  tar --no-same-owner -C "$BIN_DIR" -xzf "$BIN_DIR/govc.tar.gz" govc
  rm "$BIN_DIR/govc.tar.gz"
  chmod +x "$GOVC"
fi

export GOVC_URL="https://${VC_USER:-user}:${VC_PASSWORD:-pass}@${VC_HOST:-127.0.0.1}:${VC_PORT:-8989}/sdk"
export GOVC_INSECURE=true

# object.save with no PATH saves from ServiceContent: the whole inventory
# including the root folder and the ServiceInstance object, which the
# loader needs to rebuild the folder tree. (Saving a subtree, e.g.
# `-dc DC0 /`, omits the root folder, and a later `-load` serves an
# empty inventory even though the objects load.) -f removes any previous
# save in the directory first.
"$GOVC" object.save -f -d model
echo "model saved to ./model ($(find model -type f | wc -l) files)"
