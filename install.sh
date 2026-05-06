#!/bin/zsh
set -euo pipefail
ROOT="/Volumes/Almacen/Desarrollo/bots-trading-autonomos"
cd "$ROOT"
chmod +x *.py *.sh scripts/*.py || true
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip >/dev/null
pip install -r requirements.txt >/dev/null
cp -f launchd/com.bta.*.plist ~/Library/LaunchAgents/
for j in com.bta.bridge-5m com.bta.supervisor-2h com.bta.watchdog-1m com.bta.collector-1m com.bta.dashboard-api; do
  launchctl unload ~/Library/LaunchAgents/$j.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/$j.plist
done
echo "INSTALADO. Dashboard: http://localhost:8787"
