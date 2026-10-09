FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    WINEPREFIX=/tmp/wineprefix \
    WINEARCH=win64 \
    DISPLAY=:0

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates wget curl nodejs nginx wine wine64 wine32:i386 xvfb x11vnc novnc websockify openbox winbind cabextract fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

COPY start.sh /usr/local/bin/start.sh
COPY nginx.conf /etc/nginx/nginx.conf
COPY mfp-reverse-copier /app/mfp-reverse-copier
RUN chmod 0755 /usr/local/bin/start.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/start.sh"]
