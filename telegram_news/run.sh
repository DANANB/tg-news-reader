#!/bin/sh
set -eu
# Private app storage; this container does not mount Home Assistant config.
chown -R tgnews:tgnews /data
chmod 0700 /data
exec su-exec tgnews:tgnews /opt/venv/bin/python /opt/news/app.py
