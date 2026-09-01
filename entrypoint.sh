#!/bin/sh
set -e
# Ensure a virtual X server is running on :99 for headful chromium.
# Check the X socket (reliable) rather than $DISPLAY, which the image sets
# as a fallback even when no server is running.
if [ ! -e /tmp/.X11-unix/X99 ]; then
  Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
  # Wait for the X socket to appear (up to ~5s).
  i=0
  while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do
    sleep 0.1
    i=$((i + 1))
  done
fi
export DISPLAY=:99
exec "$@"
