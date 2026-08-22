FROM python:3.12-alpine

WORKDIR /app
COPY bridge.py panel.user.js ./

ENV STATE_DIR=/state \
    LISTEN_PORT=8687
VOLUME ["/state"]
EXPOSE 8687

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD wget -qO- http://127.0.0.1:8687/status >/dev/null || exit 1

CMD ["python", "-u", "bridge.py"]
