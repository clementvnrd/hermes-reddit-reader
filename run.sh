#!/bin/bash
# Wrapper used by Hermes bots: loads .env (credentials, never committed) and runs the reader
# with the system Python in isolated mode. Usage: run.sh <status|feed|search|thread|wiki> ...
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/.env" ]; then set -a; . "$DIR/.env"; set +a; fi
unset PYTHONPATH PYTHONHOME
exec /usr/bin/python3 -E -s "$DIR/reddit_reader.py" "$@"
