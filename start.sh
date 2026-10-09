#!/usr/bin/env bash
set -Eeuo pipefail

: "${VNC_PASSWORD:?Set VNC_PASSWORD in Railway before starting the hosted MT5 desktop}"
PORT="${PORT:-8080}"
DATA_DIR="${DATA_DIR:-/data}"
WINEPREFIX="${WINEPREFIX:-$DATA_DIR/wineprefix}"
export DATA_DIR WINEPREFIX WINEARCH=win64 DISPLAY=:0 HOME=/root
MT5_DIR="$DATA_DIR/mt5"
INSTALLER="$DATA_DIR/mt5setup.exe"
PASSFILE="$DATA_DIR/.vnc-pass"

mkdir -p "$DATA_DIR" "$MT5_DIR" /tmp/.X11-unix
chmod 700 "$DATA_DIR"

# Virtual desktop for the MT5 Windows terminal.
Xvfb :0 -screen 0 1366x768x24 -ac -nolisten tcp >/data/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 2
openbox >/data/openbox.log 2>&1 &
OPENBOX_PID=$!

# VNC authentication is required; classic VNC uses only the first 8 characters.
x11vnc -storepasswd "$VNC_PASSWORD" "$PASSFILE" >/dev/null
chmod 600 "$PASSFILE"
x11vnc -display :0 -rfbauth "$PASSFILE" -rfbport 5900 -localhost -forever -shared -noxdamage -repeat >/data/x11vnc.log 2>&1 &
VNC_PID=$!
websockify --web=/usr/share/novnc "0.0.0.0:$PORT" 127.0.0.1:5900 >/data/websockify.log 2>&1 &
WEB_PID=$!

cleanup() {
  kill "$WEB_PID" "$VNC_PID" "$OPENBOX_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Create a persistent Wine prefix on the Railway volume.
if [ ! -d "$WINEPREFIX/drive_c" ]; then
  wineboot --init
fi

TERMINAL="$MT5_DIR/terminal64.exe"
if [ ! -f "$TERMINAL" ]; then
  echo "MT5 terminal not installed yet; downloading official MetaTrader 5 installer."
  wget -q --show-progress -O "$INSTALLER" "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
  # Install to the persistent Railway volume so restarts keep terminal files.
  wine "$INSTALLER" /auto "/path:Z:\\data\\mt5" >/data/mt5-installer.log 2>&1 || true
  for i in $(seq 1 180); do
    [ -f "$TERMINAL" ] && break
    sleep 5
  done
fi

if [ ! -f "$TERMINAL" ]; then
  echo "ERROR: MT5 installer did not create $TERMINAL. See /data/mt5-installer.log via the persistent volume."
  tail -n 100 /data/mt5-installer.log 2>/dev/null || true
  exit 1
fi

echo "MT5 terminal found at $TERMINAL"
echo "Browser desktop is listening on port $PORT; open /vnc.html and connect with the configured VNC password."
# Keep MT5 alive for the demo. Do not configure a trading account until a user installs/verifies the EAs.
while true; do
  wine "$TERMINAL" /portable
  echo "MT5 terminal exited; retrying in 10 seconds."
  sleep 10
done
