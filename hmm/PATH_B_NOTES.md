# Path B — Crypto Asset Adaptation (Design Notes)

**Status**: Helper modules implemented, still no production code changes by default.

Path A implemented a generic HMM regime analysis tool for any daily OHLCV
asset (default EURUSD). Path B would adapt this analysis to the repo's
actual crypto assets and orchestrator, *without modifying any existing
production bot code*.

## Target assets (from orchestrator_config.json)

| Symbol | Style | Allowed regimes |
|--------|-------|-----------------|
| SOLUSDT | Pullback/Trend | bull, sideways |
| BNBUSDC | Structure/Trend | bull, sideways |
| XRPUSDT | Grid | sideways, bear |
| ... | (orchestrator auto-assigns) | |

## HMM integration approach

Instead of training HMM on one asset, Path B would:

1. **Multi-asset HMM** — Train one GaussianHMM per symbol (or group related
   assets under one model), using the same features (volatility, momentum,
   cumulative returns) but on crypto rather than forex data.

2. **Regime mapping** — Map HMM states to the four existing regime labels
   used by the orchestrator (`bull`, `bear`, `sideways`, `risk_off`):
   - State 0 (lowest vol) → `bull`
   - State 1 (low-medium vol) → `sideways`
   - State 2 (high vol) → `bear`
   - State 3+ (extreme vol) → `risk_off`

3. **Feeding the orchestrator** — The regime signal would be exposed as a
   lightweight module (e.g. `hmm_regime_provider.py`) that the orchestrator
   queries at each cycle. No changes to existing bot files.

## Implemented helper modules

### `hmm_regime_provider.py`

Implemented as an isolated helper that:
- reads `runtime/live/*_5m.csv`
- resamples to `1h` / `4h` / `1d`
- trains one GaussianHMM per symbol
- caches models under `hmm/models/`
- maps ordered states to `bull/bear/sideways/risk_off`
- writes a current snapshot under `hmm/output/hmm_regime_snapshot.json`

### `hmm_regime_compare.py`

Implemented as a side-by-side comparison tool that:
- runs the current heuristic detector logic on the same feeds
- runs the HMM provider on the same symbols
- reports per-symbol agreement/disagreement
- writes output under `hmm/output/hmm_vs_heuristic.json`

## How the orchestrator would use it

```
Current (no changes):
    orchestrator reads config → runs bots in their allowed regimes

Path B (additive change only):
    orchestrator reads config → queries HmmRegimeProvider for current regime
      → filters bots by regime match → runs only matching bots
```

The `orchestrator_config.json` already has `regimes` per bot — the provider
just adds a dynamic regime source. This means **zero changes** to any bot's
Python file.

## Data pipeline

- Use `data_fetcher.py` (already in the repo) to get daily OHLCV for each
  symbol via Binance API.
- Train the HMM on the same 20-day rolling features.
- Store trained models as pickle (`.pkl`) in `hmm/models/` — cached and
  re-trained weekly.

## Non-goals (for Path B)

- ❌ No parameter optimisation per regime (see Path A warning).
- ❌ No changes to bot signal logic.
- ❌ No live re-training every bar (daily is sufficient).
- ❌ No forex / Polymarket assets (crypto only).

## Blockers / Risks

| Risk | Mitigation |
|------|------------|
| HMM online update complexity | Retrain weekly, cache model, don't update intraday |
| Regime mapping too coarse | Keep existing orchestrator logic as fallback |
| Feature scaling across assets | Use log-returns, not absolute prices |
| Library dependency (hmmlearn) | Already isolated in `hmm/requirements.txt` |

## Next steps

1. Decide if regimes should be shared across all symbols or per-symbol.
2. Train HMM on SOLUSDT/BTC/USDT 3y daily data and validate state maps.
3. Write `hmm_regime_provider.py` as a non-invasive helper module.
4. Patch orchestrator cycle script (not bot files) to call the provider.
5. Paper-test regime transitions over last 3 months vs strategy returns.

*Path B should remain in feature/hmm-regime-analysis until validated in
paper mode against live data.*
