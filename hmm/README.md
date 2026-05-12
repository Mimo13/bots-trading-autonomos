# HMM Regime Analysis — Path A PoC

**Path A** of the HMM Regime Analysis roadmap: a self-contained Python tool
for market regime detection using Hidden Markov Models (GaussianHMM).

## Quick start

```bash
# Create an isolated environment
cd <repo-root>
python3 -m venv .venv_hmm
source .venv_hmm/bin/activate   # or .venv_hmm/Scripts/activate on Windows

# Install dependencies
pip install -r hmm/requirements.txt

# Run with defaults (EURUSD, 10 years, MA crossover sweep)
python hmm/hmm_regime_analysis.py

# Save plots as HTML files instead of opening browser
python hmm/hmm_regime_analysis.py --html ./hmm/output/

# Analyse a different asset
python hmm/hmm_regime_analysis.py --asset BTC-USD --years 5

# Use a local CSV file
python hmm/hmm_regime_analysis.py --csv data/my_data.csv

# Custom parameter grid
python hmm/hmm_regime_analysis.py --fast 5,10,20 --slow 30,50,100,200

# Console-only (no plots)
python hmm/hmm_regime_analysis.py --no-plots
```

## What it does

1. **Data**: Downloads daily OHLCV via `yfinance` (or reads a local CSV).
2. **Features**: Computes rolling volatility, cumulative return, and momentum
   (20-day window).
3. **HMM**: Trains `GaussianHMM` for 2–5 states, selects best by BIC, orders
   by ascending volatility.
4. **Sweep**: Tests every combination of SMA fast (5,10,15,20), SMA slow
   (30,50,100,200), and ATR SL multiplier (1.5,2.0,2.5,3.0).
5. **Metrics**: Per-regime and global Sharpe, Net Profit, Max DD,
   Profit Factor, Win Rate.
6. **Plots**: Regime timeline, best-params table, equity curves,
   Sharpe heatmaps.

## Files

| File | Purpose |
|------|---------|
| `hmm_regime_analysis.py` | Main standalone analysis script |
| `requirements.txt` | Isolated dependency list |
| `README.md` | This file |
| `output/` | Generated HTML plots (when `--html` is used) |

## ⚠️ Warning

This is an **in-sample** exploration tool. The parameter sweep is performed
on the entire dataset. Results ARE overfitted and should NOT be used directly
for live trading. For correct usage, see the disclaimer printed at the end
of every run.

## Path B (future)

Path B will adapt this HMM analysis to the repo's crypto assets and
orchestrator, without changing any production code — see `PATH_B_NOTES.md`.
