#!/bin/sh
set -eu

if [ -f /app/config-source/passion-admin.json ]; then
  install -o monitor -g monitor -m 600 /app/config-source/passion-admin.json /app/config/passion-admin.json
fi

exec runuser -u monitor -- "$@"
