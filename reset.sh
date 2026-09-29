#!/usr/bin/env bash
# Wipe all incident memory: stops the bots and the local Recall server, then
# deletes data/. Start the bots again afterwards with: python bots.py
#
# For a quick reset during a demo, prefer "@recall-bot reset" in the channel,
# which starts a fresh incident without deleting anything.
set -euo pipefail
cd "$(dirname "$0")"

pkill -f "[Pp]ython bots.py" && echo "stopped the bots" || echo "bots were not running"

if [ -f data/recall/runtime.json ]; then
  pid=$(python3 -c 'import json; print(json.load(open("data/recall/runtime.json"))["pid"])')
  if kill "$pid" 2>/dev/null; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    echo "stopped the Recall server (pid $pid)"
  fi
fi

rm -rf data
echo "deleted data/; start fresh with: python bots.py"
