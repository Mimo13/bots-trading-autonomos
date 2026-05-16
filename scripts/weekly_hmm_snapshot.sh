#!/bin/zsh
# Weekly HMM snapshot — every Monday 08:00 Europe/Madrid
# Called by cron; output goes to hmm/output/hmm_regime_snapshot.json
cd /Users/mimo13/bots-trading-autonomos-runtime
BTA_ROOT=/Users/mimo13/bots-trading-autonomos-runtime \
    . .venv/bin/python hmm/hmm_regime_provider.py \
    --symbols SOLUSDT,XRPUSDT --timeframe 4h --force-retrain
