#!/bin/sh
set -eu
# Private app storage; this container does not mount Home Assistant config.
chown -R news:news /data
chmod 0700 /data
exec su-exec news:news /opt/venv/bin/python /opt/news/app.py
