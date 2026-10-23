FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION=1.1.1
ARG BUILD_ARCH=aarch64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip su-exec && \
    python3 -m venv /opt/venv && \
    addgroup -S tgnews && adduser -S -D -H -G tgnews -h /nonexistent -s /sbin/nologin tgnews

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app.py /opt/news/app.py
COPY run.sh /run.sh
RUN chmod 755 /run.sh && chmod 644 /opt/news/app.py
CMD ["/run.sh"]
