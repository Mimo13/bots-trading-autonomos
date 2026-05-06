#!/bin/zsh
set -euo pipefail
ROOT="/Volumes/Almacen/Desarrollo/bots-trading-autonomos"
mkdir -p "$ROOT/runtime/logs" "$ROOT/runtime/polymarket/runs" "$ROOT/runtime/ops"
cd "$ROOT"
/usr/bin/python3 "$ROOT/paper_bot_supervisor.py" >> "$ROOT/runtime/logs/supervisor_cycle.log" 2>&1
