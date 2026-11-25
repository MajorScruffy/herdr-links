#!/bin/sh
# Herdr runs plugin commands without a shell and with the server's PATH.
# Prefer the system Python so the plugin works regardless of mise/pyenv state.
if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY=python3; fi
exec "$PY" "$(dirname "$0")/links.py" "$@"
