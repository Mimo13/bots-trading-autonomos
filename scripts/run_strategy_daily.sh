#!/bin/zsh
set -euo pipefail
cd /Volumes/Almacen/Desarrollo/bots-trading-autonomos
DATABASE_URL=postgresql:///bots_dashboard ./.venv/bin/python scripts/strategy_advisor.py
