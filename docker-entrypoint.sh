#!/usr/bin/env bash
# Container entrypoint: start vcsim, build the lab, keep vcsim running.
#
# 1. vcsim starts in the background with the same inventory flags as
#    docker-compose.yml / tools/start-vcsim-binary.sh, listening on
#    0.0.0.0:${PORT:-8989} (Render injects $PORT).
# 2. Once the SDK endpoint answers, scripts/build_lab.py creates the
#    22 VMs from vms.yaml.
# 3. vcsim is brought to the foreground so the container stays alive.
#
# Credentials come from VCSIM_USERNAME / VCSIM_PASSWORD (defaults
# user/pass, matching the lab defaults). Set strong values in Render —
# the service is public.
set -euo pipefail
cd /app

PORT="${PORT:-8989}"
VCSIM_USERNAME="${VCSIM_USERNAME:-user}"
VCSIM_PASSWORD="${VCSIM_PASSWORD:-pass}"

# Run vcsim from a scratch working directory, as
# tools/start-vcsim-binary.sh does: vcsim creates datastore backing
# directories (DC0-LocalDS_*) in its cwd and refuses to start if they
# already exist there.
WORK_DIR="$(mktemp -d /tmp/vcsim-work-XXXXXX)"
VCSIM_PID=""

cleanup() {
  if [ -n "$VCSIM_PID" ] && kill -0 "$VCSIM_PID" 2>/dev/null; then
    kill -TERM "$VCSIM_PID" 2>/dev/null || true
    wait "$VCSIM_PID" 2>/dev/null || true
  fi
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
  exec vcsim \
    -l "0.0.0.0:${PORT}" \
    -tls=false \
    -api-version 8.0.3 \
    -dc 1 -cluster 1 -host 3 -standalone-host 1 \
    -ds 3 -pod 1 -pg 2 -nsx 1 -pool 1 -vm 0 \
    -username "$VCSIM_USERNAME" \
    -password "$VCSIM_PASSWORD"
) &
VCSIM_PID=$!
echo "vcsim starting (pid $VCSIM_PID) on 0.0.0.0:${PORT} ..."

# Wait until the SDK endpoint answers (up to 60s); fail fast if vcsim
# dies in the meantime. Any HTTP response counts. vcsim serves plain
# HTTP here (-tls=false), so the probe is plain http:// with no -k.
READY=0
for i in $(seq 1 60); do
  if ! kill -0 "$VCSIM_PID" 2>/dev/null; then
    echo "vcsim exited before becoming ready" >&2
    exit 1
  fi
  if curl -s -o /dev/null "http://127.0.0.1:${PORT}/"; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "timed out waiting for vcsim to answer on port ${PORT}" >&2
  exit 1
fi
echo "vcsim is up; building the lab ..."

# The builder connects over the same credentials vcsim was started with,
# and over plain HTTP (VC_SCHEME=http) to match -tls=false.
VC_HOST=127.0.0.1 VC_PORT="$PORT" VC_SCHEME=http \
  VC_USER="$VCSIM_USERNAME" VC_PASSWORD="$VCSIM_PASSWORD" VC_INSECURE=1 \
  python scripts/build_lab.py

echo "lab built; vcsim running in the foreground (pid $VCSIM_PID)"
wait "$VCSIM_PID"
