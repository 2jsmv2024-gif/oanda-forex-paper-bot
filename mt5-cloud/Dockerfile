FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/opt/mt5
ENV WINEDEBUG=-all
ENV DISPLAY=:99

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends wine64 wine32 xvfb wget curl ca-certificates cabextract unzip winbind \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/mt5 /opt/app
WORKDIR /opt/app
RUN wget -q -O /tmp/mt5setup.exe https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe \
 && wineboot --init \
 && wine /tmp/mt5setup.exe /auto \
 && rm -f /tmp/mt5setup.exe

COPY MFP_GOLD_OANDA_MT5_BRIDGE.mq5 /opt/app/
COPY start.sh /opt/app/start.sh
RUN chmod +x /opt/app/start.sh

CMD ["/opt/app/start.sh"]