#!/usr/bin/env bash
set -Eeuo pipefail

: "${VNC_PASSWORD:?Set VNC_PASSWORD in Railway before starting the hosted MT5 desktop}"
: "${COPIER_TOKEN:?Existing Railway COPIER_TOKEN must remain configured}"
PORT="${PORT:-8080}"
DATA_DIR="${DATA_DIR:-/data}"
WINEPREFIX="${WINEPREFIX:-/tmp/wineprefix}"
export DATA_DIR WINEPREFIX WINEARCH=win64 DISPLAY=:0 HOME=/root WINEDEBUG=-all

INSTALL_DIR="/tmp/mt5-install"
MASTER_DIR="/tmp/mt5-master"
SLAVE_DIR="/tmp/mt5-slave"
INSTALLER="/tmp/mt5setup.exe"
PASSFILE="$DATA_DIR/.vnc-pass"

mkdir -p "$DATA_DIR" "$INSTALL_DIR" /tmp/.X11-unix
chmod 700 "$DATA_DIR"

# Copier API stays running on the private internal port.
PORT=3000 DATA_DIR="$DATA_DIR" node /app/mfp-reverse-copier/server.js >/tmp/copier.log 2>&1 &
COPIER_PID=$!

# Browser-accessible virtual desktop.
Xvfb :0 -screen 0 1600x900x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 2
openbox >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!
x11vnc -storepasswd "$VNC_PASSWORD" "$PASSFILE" >/dev/null
chmod 600 "$PASSFILE"
x11vnc -display :0 -rfbauth "$PASSFILE" -rfbport 5900 -localhost -forever -shared -noxdamage -repeat >/tmp/x11vnc.log 2>&1 &
VNC_PID=$!
websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
WEB_PID=$!
nginx -g 'daemon off;' >/tmp/nginx.log 2>&1 &
NGINX_PID=$!

cleanup() {
  kill "$NGINX_PID" "$WEB_PID" "$VNC_PID" "$OPENBOX_PID" "$XVFB_PID" "$COPIER_PID" 2>/dev/null || true
  [ -n "${MASTER_PID:-}" ] && kill "$MASTER_PID" 2>/dev/null || true
  [ -n "${SLAVE_PID:-}" ] && kill "$SLAVE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ ! -d "$WINEPREFIX/drive_c" ]; then
  wineboot --init
fi

# Install MT5 once, then keep two isolated portable copies so each can log into
# a different demo account (Master and Reverse Slave).
if [ ! -f "$INSTALL_DIR/terminal64.exe" ]; then
  echo "Downloading official MetaTrader 5 installer."
  wget -q --show-progress -O "$INSTALLER" "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
  wine "$INSTALLER" /auto "/path:Z:\\tmp\\mt5-install" >/tmp/mt5-installer.log 2>&1 || true
  for i in $(seq 1 180); do
    [ -f "$INSTALL_DIR/terminal64.exe" ] && break
    sleep 5
  done
fi

if [ ! -f "$INSTALL_DIR/terminal64.exe" ]; then
  echo "ERROR: MT5 installer did not create $INSTALL_DIR/terminal64.exe."
  tail -n 100 /tmp/mt5-installer.log 2>/dev/null || true
  exit 1
fi

if [ ! -f "$MASTER_DIR/terminal64.exe" ]; then
  mkdir -p "$MASTER_DIR"
  cp -a "$INSTALL_DIR/." "$MASTER_DIR/"
fi
if [ ! -f "$SLAVE_DIR/terminal64.exe" ]; then
  mkdir -p "$SLAVE_DIR"
  cp -a "$INSTALL_DIR/." "$SLAVE_DIR/"
fi

echo "Starting MT5 MASTER terminal (separate portable folder)."
wine "$MASTER_DIR/terminal64.exe" /portable >/tmp/mt5-master.log 2>&1 &
MASTER_PID=$!
sleep 8
echo "Starting MT5 REVERSE SLAVE terminal (separate portable folder)."
wine "$SLAVE_DIR/terminal64.exe" /portable >/tmp/mt5-slave.log 2>&1 &
SLAVE_PID=$!

echo "MT5 Master and Slave terminals launched. Open /vnc.html?autoconnect=1&resize=remote&path=websockify"
# Keep the hosted environment alive; restart either terminal if it exits.
while true; do
  if ! kill -0 "$MASTER_PID" 2>/dev/null; then
    echo "Master terminal exited; restarting."
    wine "$MASTER_DIR/terminal64.exe" /portable >/data/mt5-master.log 2>&1 &
    MASTER_PID=$!
  fi
  if ! kill -0 "$SLAVE_PID" 2>/dev/null; then
    echo "Slave terminal exited; restarting."
    wine "$SLAVE_DIR/terminal64.exe" /portable >/data/mt5-slave.log 2>&1 &
    SLAVE_PID=$!
  fi
  sleep 10
done
