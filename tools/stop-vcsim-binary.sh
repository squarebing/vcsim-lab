#!/usr/bin/env bash
# Stop the vcsim started by tools/start-vcsim-binary.sh.
#
#   tools/stop-vcsim-binary.sh [--env default]   # stop one environment
#   tools/stop-vcsim-binary.sh --all             # stop every environment
set -euo pipefail
cd "$(dirname "$0")/.."

stop_one() {
  local pidfile="$1"
  if [ -f "$pidfile" ]; then
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "stopped vcsim (pid $pid, $pidfile)"
    else
      echo "vcsim pid $pid is not running ($pidfile)"
    fi
    rm -f "$pidfile"
  fi
}

if [ "${1:-}" = "--all" ]; then
  found=0
  for pidfile in bin/vcsim-*.pid bin/vcsim.pid; do
    [ -f "$pidfile" ] || continue
    found=1
    stop_one "$pidfile"
  done
  [ "$found" -eq 1 ] || echo "no vcsim pid files in bin/ — nothing to stop"
  exit 0
fi

ENV="default"
if [ "${1:-}" = "--env" ]; then
  ENV="${2:-}"
  if [ -z "$ENV" ]; then echo "usage: $0 [--env NAME | --all]" >&2; exit 2; fi
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [--env NAME | --all]" >&2
  exit 2
fi

PIDFILE="bin/vcsim-${ENV}.pid"
if [ -f "$PIDFILE" ]; then
  stop_one "$PIDFILE"
else
  echo "no pid file at $PIDFILE — nothing to stop"
fi
