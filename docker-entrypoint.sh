#!/usr/bin/env bash
# Container entrypoint: start one vcsim per environment, build each lab,
# serve the API.
#
# 1. For each environment in environments/ (or VCSIM_ENVS, a
#    comma-separated subset): vcsim starts in the background on its
#    INTERNAL port (127.0.0.1:<env port>, -tls=false) with the
#    inventory flags from environments/<env>/env.yaml.
# 2. Once each SDK endpoint answers, scripts/build_lab.py --env <name>
#    creates that environment's VMs.
# 3. scripts/serve_api.py (uvicorn) serves the inventory API on
#    0.0.0.0:${PORT:-8000} (Render injects $PORT). The API is the
#    front door: it serves /api/* itself and reverse-proxies every
#    other path (/sdk, /about, ...) to the active environment's vcsim.
#
# Credentials come from VCSIM_USERNAME / VCSIM_PASSWORD (defaults
# user/pass, matching the lab defaults). Set strong values in Render —
# the service is public.
set -euo pipefail
cd /app

PORT="${PORT:-8000}"
VCSIM_USERNAME="${VCSIM_USERNAME:-user}"
VCSIM_PASSWORD="${VCSIM_PASSWORD:-pass}"

# Which environments to start: VCSIM_ENVS (comma-separated names,
# e.g. VCSIM_ENVS=default), default all environments in environments/.
if [ -n "${VCSIM_ENVS:-}" ]; then
  ENVS="${VCSIM_ENVS//,/ }"
else
  ENVS="$(python3 scripts/env_info.py --list)"
fi
if [ -z "$ENVS" ]; then
  echo "no environments found in environments/ (and VCSIM_ENVS is empty)" >&2
  exit 1
fi

VCSIM_PIDS=""
API_PID=""
FIRST_ENV=""

cleanup() {
  # Kill uvicorn first (it is the public port), then every vcsim.
  # kill -0 guards against pids that already exited on their own.
  for pid in $API_PID $VCSIM_PIDS; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Shared connection settings for the builders and the API: plain HTTP
# (VC_SCHEME=http) to match vcsim's -tls=false — Render terminates TLS
# at its edge and forwards plain HTTP to the container. VC_PORT is
# deliberately NOT set: each environment takes its own port from
# env.yaml (an explicit VC_PORT would override it — see
# vcsim_lab.config.vcenter_config_for_env).
export VC_HOST=127.0.0.1 VC_SCHEME=http
export VC_USER="$VCSIM_USERNAME" VC_PASSWORD="$VCSIM_PASSWORD" VC_INSECURE=1

for ENV in $ENVS; do
  # First environment started — captured here, not derived from $ENVS
  # afterwards: $ENVS is newline-separated (env_info.py --list) or
  # space-separated (VCSIM_ENVS), and a space-only suffix strip would
  # pass the whole "default\ndev" list as one name to --active-env
  # (that broke the Render deploy once). The loop variable is always
  # a single name.
  if [ -z "$FIRST_ENV" ]; then
    FIRST_ENV="$ENV"
  fi
  # Environment details from env.yaml (port, vcsim flags) — fails fast
  # on an unknown name in VCSIM_ENVS.
  ENV_INFO="$(python3 scripts/env_info.py --env "$ENV")" || {
    echo "unknown environment '$ENV' (check VCSIM_ENVS)" >&2
    exit 2
  }
  eval "$ENV_INFO"

  # Run vcsim from a scratch working directory, as
  # tools/start-vcsim-binary.sh does: vcsim creates datastore backing
  # directories (DC0-LocalDS_*) in its cwd and refuses to start if they
  # already exist there. One scratch dir per environment keeps them
  # from colliding with each other.
  WORK_DIR="$(mktemp -d /tmp/vcsim-work-XXXXXX)"
  (
    cd "$WORK_DIR" || exit 1
    # -tls=false: Render terminates TLS at its edge and forwards plain
    # HTTP to the container, so vcsim must serve plain HTTP here — a
    # TLS-serving vcsim answers Render's forwarded requests with
    # "client sent an HTTP request to an HTTPS server". Local runs
    # (docker-compose, start-vcsim-binary.sh) keep the TLS default.
    # -l binds the loopback interface only: vcsim is internal, reached
    # solely through the API server's reverse proxy.
    # shellcheck disable=SC2086 — $ENV_VCSIM_FLAGS is a space-separated
    # flag list from env.yaml (validated by vcsim_lab.config).
    exec vcsim \
      -l "127.0.0.1:${ENV_PORT}" \
      -tls=false \
      $ENV_VCSIM_FLAGS \
      -vm 0 \
      -username "$VCSIM_USERNAME" \
      -password "$VCSIM_PASSWORD"
  ) &
  VCSIM_PID=$!
  VCSIM_PIDS="$VCSIM_PIDS $VCSIM_PID"
  echo "vcsim ($ENV) starting (pid $VCSIM_PID) on 127.0.0.1:${ENV_PORT} (internal) ..."

  # Wait until the SDK endpoint answers (up to 60s); fail fast if
  # vcsim dies in the meantime. Any HTTP response counts. vcsim serves
  # plain HTTP here (-tls=false), so the probe is plain http:// with no -k.
  READY=0
  for i in $(seq 1 60); do
    if ! kill -0 "$VCSIM_PID" 2>/dev/null; then
      echo "vcsim ($ENV) exited before becoming ready" >&2
      exit 1
    fi
    if curl -s -o /dev/null "http://127.0.0.1:${ENV_PORT}/"; then
      READY=1
      break
    fi
    sleep 1
  done
  if [ "$READY" -ne 1 ]; then
    echo "timed out waiting for vcsim ($ENV) to answer on port ${ENV_PORT}" >&2
    exit 1
  fi
  echo "vcsim ($ENV) is up; building its lab ..."
  python scripts/build_lab.py --env "$ENV"
done

echo "labs built; serving the inventory API on 0.0.0.0:${PORT} ..."
# uvicorn in the background, waited on (not exec'd, not foreground):
# `wait` returns immediately when a trapped signal arrives, so the
# TERM trap above fires promptly and cleanup() stops every process.
# If uvicorn exits on its own, `wait` returns its status and set -e
# ends the script, which stops all vcsim instances through the EXIT trap.
# The API's active environment defaults to the first environment
# started (captured in the loop above) — "default" when all start
# (the list is sorted), or the first VCSIM_ENVS entry when a subset
# starts, so the default endpoints serve a running environment
# instead of a dead one.
python scripts/serve_api.py --host 0.0.0.0 --port "$PORT" --active-env "$FIRST_ENV" &
API_PID=$!
wait "$API_PID"
