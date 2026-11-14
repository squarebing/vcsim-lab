#!/usr/bin/env bash
# Container entrypoint: start vcsim, build the lab, serve the API.
#
# 1. vcsim starts in the background on an INTERNAL port
#    (127.0.0.1:8989) with the same inventory flags as
#    docker-compose.yml / tools/start-vcsim-binary.sh.
# 2. Once the SDK endpoint answers, scripts/build_lab.py creates the
#    22 VMs from vms.yaml.
# 3. scripts/serve_api.py (uvicorn) serves the inventory API on
#    0.0.0.0:${PORT:-8000} (Render injects $PORT). The API is the
#    front door: it serves /api/* itself and reverse-proxies every
#    other path (/sdk, /about, ...) to the internal vcsim — so the
#    public SDK endpoint keeps working unchanged.
#
# Credentials come from VCSIM_USERNAME / VCSIM_PASSWORD (defaults
# user/pass, matching the lab defaults). Set strong values in Render —
# the service is public.
set -euo pipefail
cd /app

PORT="${PORT:-8000}"
VCSIM_PORT=8989
VCSIM_USERNAME="${VCSIM_USERNAME:-user}"
VCSIM_PASSWORD="${VCSIM_PASSWORD:-pass}"

# Run vcsim from a scratch working directory, as
# tools/start-vcsim-binary.sh does: vcsim creates datastore backing
# directories (DC0-LocalDS_*) in its cwd and refuses to start if they
# already exist there.
WORK_DIR="$(mktemp -d /tmp/vcsim-work-XXXXXX)"
VCSIM_PID=""
API_PID=""

cleanup() {
  # Kill uvicorn first (it is the public port), then vcsim. kill -0
  # guards against pids that already exited on their own.
  for pid in "$API_PID" "$VCSIM_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

(
  cd "$WORK_DIR" || exit 1
  # -tls=false: Render terminates TLS at its edge and forwards plain
  # HTTP to the container, so vcsim must serve plain HTTP here — a
  # TLS-serving vcsim answers Render's forwarded requests with
  # "client sent an HTTP request to an HTTPS server". Local runs
  # (docker-compose, start-vcsim-binary.sh) keep the TLS default.
  # -l binds the loopback interface only: vcsim is internal, reached
  # solely through the API server's reverse proxy.
  exec vcsim \
    -l "127.0.0.1:${VCSIM_PORT}" \
    -tls=false \
    -api-version 8.0.3 \
    -dc 1 -cluster 1 -host 3 -standalone-host 1 \
    -ds 3 -pod 1 -pg 2 -nsx 1 -pool 1 -vm 0 \
    -username "$VCSIM_USERNAME" \
    -password "$VCSIM_PASSWORD"
) &
VCSIM_PID=$!
echo "vcsim starting (pid $VCSIM_PID) on 127.0.0.1:${VCSIM_PORT} (internal) ..."

# Wait until the SDK endpoint answers (up to 60s); fail fast if vcsim
# dies in the meantime. Any HTTP response counts. vcsim serves plain
# HTTP here (-tls=false), so the probe is plain http:// with no -k.
READY=0
for i in $(seq 1 60); do
  if ! kill -0 "$VCSIM_PID" 2>/dev/null; then
    echo "vcsim exited before becoming ready" >&2
    exit 1
  fi
  if curl -s -o /dev/null "http://127.0.0.1:${VCSIM_PORT}/"; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "timed out waiting for vcsim to answer on port ${VCSIM_PORT}" >&2
  exit 1
fi
echo "vcsim is up; building the lab ..."

# The builder connects over the same credentials vcsim was started with,
# and over plain HTTP (VC_SCHEME=http) to match -tls=false.
VC_HOST=127.0.0.1 VC_PORT="$VCSIM_PORT" VC_SCHEME=http \
  VC_USER="$VCSIM_USERNAME" VC_PASSWORD="$VCSIM_PASSWORD" VC_INSECURE=1 \
  python scripts/build_lab.py

echo "lab built; serving the inventory API on 0.0.0.0:${PORT} ..."
# uvicorn in the background, waited on (not exec'd, not foreground):
# `wait` returns immediately when a trapped signal arrives, so the
# TERM trap above fires promptly and cleanup() stops both processes.
# If uvicorn exits on its own, `wait` returns its status and set -e
# ends the script, which stops vcsim through the EXIT trap.
VC_HOST=127.0.0.1 VC_PORT="$VCSIM_PORT" VC_SCHEME=http \
  VC_USER="$VCSIM_USERNAME" VC_PASSWORD="$VCSIM_PASSWORD" VC_INSECURE=1 \
  python scripts/serve_api.py --host 0.0.0.0 --port "$PORT" &
API_PID=$!
wait "$API_PID"
