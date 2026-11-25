ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip bluez

# prepare: copy the repository's host/ folder next to this file first (see README.md)
COPY host /opt/spark-host
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /opt/spark-host

COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
