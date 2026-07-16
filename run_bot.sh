#!/bin/zsh
# Wrapper for launchd: activate venv + run main.py.
# Stdout/stderr go to logs/launchd-stdout.log via the plist.

set -e
cd "/Users/villerajala/Trading bot"
source .venv/bin/activate
echo "=== Bot launch at $(date) ==="
exec python3 main.py
