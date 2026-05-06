#!/bin/zsh
set -euo pipefail
ROOT="/Volumes/Almacen/Desarrollo/bots-trading-autonomos"
mkdir -p "$ROOT/runtime/logs" "$ROOT/runtime/tradingview" "$ROOT/runtime/polymarket"
if ! pgrep -x "cTrader" >/dev/null 2>&1; then
  open -ga "/Applications/cTrader.app" || true
fi
cd "$ROOT"
/usr/bin/python3 "$ROOT/tradingview_bridge_cycle.py" >> "$ROOT/runtime/logs/bridge_cycle.log" 2>&1
