#!/bin/bash
set -e

export WINEPREFIX=/opt/mt5
export WINEDEBUG=-all
export DISPLAY=:99

MT5DIR="/opt/mt5/drive_c/Program Files/MetaTrader 5"
EXPERT_DIR="$MT5DIR/MQL5/Experts"
mkdir -p "$EXPERT_DIR" "$MT5DIR/Config"
cp -f /opt/app/MFP_GOLD_OANDA_MT5_BRIDGE.mq5 "$EXPERT_DIR/MFP_GOLD_OANDA_MT5_BRIDGE.mq5"
wine "$MT5DIR/metaeditor64.exe" '/compile:C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\MFP_GOLD_OANDA_MT5_BRIDGE.mq5' /log || true
cat > "$EXPERT_DIR/MFP_GOLD_OANDA_MT5_BRIDGE.set" <<EOF
SignalUrl1=$${SIGNAL_URL_1:-}
SignalUrl2=$${SIGNAL_URL_2:-}
PollSeconds=$${POLL_SECONDS:-2}
VolumeLots=$${VOLUME_LOTS:-0.15}
MagicNumber=$${MAGIC_NUMBER:-58247654}
DeviationPoints=$${DEVIATION_POINTS:-50}
EnableRealDemoExecution=$${ENABLE_REAL_DEMO_EXECUTION:-true}
CloseOnOppositeSignal=$${CLOSE_ON_OPPOSITE_SIGNAL:-true}
UseChartSymbol=true
FixedSymbol=
LogFileName=MFP_OANDA_GOLD_CROSSCHECK.csv
EOF
cat > "$MT5DIR/Config/railway.ini" <<EOF
[Common]
Login=$${OANDA_LOGIN:-}
Password=$${OANDA_PASSWORD:-}
Server=$${OANDA_SERVER:-OANDA_UK-Demo-1}
KeepPrivate=1
NewsEnable=0
CertInstall=1

[Experts]
AllowLiveTrading=1
AllowDllImport=0
Enabled=1
Account=1
Profile=0
WebRequest=1
WebRequestUrl=$${SIGNAL_URL_1:-}
WebRequestUrl2=$${SIGNAL_URL_2:-}

[Charts]
MaxBars=10000
ProfileLast=Default

[StartUp]
Expert=MFP_GOLD_OANDA_MT5_BRIDGE
ExpertParameters=MFP_GOLD_OANDA_MT5_BRIDGE.set
Symbol=$${OANDA_SYMBOL:-XAUUSD}
Period=M1
EOF
Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
sleep 2
echo "[MT5 CLOUD] starting MetaTrader 5"
echo "[MT5 CLOUD] server=$${OANDA_SERVER:-OANDA_UK-Demo-1} symbol=$${OANDA_SYMBOL:-XAUUSD}"
echo "[MT5 CLOUD] EA=MFP_GOLD_OANDA_MT5_BRIDGE"
echo "[MT5 CLOUD] execution=$${ENABLE_REAL_DEMO_EXECUTION:-true}"
exec wine "$MT5DIR/terminal64.exe" /portable '/config:C:\\Program Files\\MetaTrader 5\\Config\\railway.ini'