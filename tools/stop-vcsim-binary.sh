#!/usr/bin/env bash
# Stop the vcsim started by tools/start-vcsim-binary.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE="bin/vcsim.pid"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "stopped vcsim (pid $PID)"
  else
    echo "vcsim pid $PID is not running"
  fi
  rm -f "$PIDFILE"
else
  echo "no pid file at $PIDFILE — nothing to stop"
fi
