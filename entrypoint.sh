#!/bin/sh
set -e

# Ensure a virtual X server is running on :99 for headful chromium.
# A dead Xvfb (OOM-killed or crashed) leaves stale /tmp/.X99-lock and
# /tmp/.X11-unix/X99 behind; trusting the socket file's existence would skip
# startup, and the stale lock blocks any new instance. So probe a real
# connection instead, and clean up before starting.

x_alive() {
  python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect("/tmp/.X11-unix/X99"); sys.exit(0)' 2>/dev/null
}

start_xvfb() {
  pkill -f "Xvfb :99" 2>/dev/null || true
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
  Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
  i=0
  while ! x_alive && [ "$i" -lt 50 ]; do
    sleep 0.1
    i=$((i + 1))
  done
}

if ! x_alive; then
  start_xvfb
fi

# Self-heal: if Xvfb dies at runtime (e.g. OOM), restart it within ~30s so
# headful launches stop failing and leaking orphaned chromium processes.
(
  while true; do
    sleep 30
    if ! x_alive; then
      start_xvfb || true
    fi
  done
) &

export DISPLAY=:99
exec "$@"
