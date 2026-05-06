#!/bin/zsh
set -euo pipefail
ROOT="/Volumes/Almacen/Desarrollo/bots-trading-autonomos"
mkdir -p "$ROOT/runtime/logs"
cd "$ROOT"
/usr/bin/python3 "$ROOT/aggressive_watchdog.py" >> "$ROOT/runtime/logs/watchdog_stdout.log" 2>&1
