#!/usr/bin/env python3
"""
HMM Regime Analysis — Hidden Markov Model for Market Regime Detection
======================================================================

Self-contained analysis tool that:
  1. Downloads/loads OHLCV data (EURUSD via yfinance by default)
  2. Engineers features (volatility, momentum, cumulative returns)
  3. Trains GaussianHMM (2–5 states), selects best via BIC
  4. Runs a parameter sweep over a Moving Average Crossover strategy
  5. Reports per-regime and global metrics (Sharpe, DD, Profit Factor)
  6. Produces interactive Plotly visualisations

⚠️  WARNING — In-sample overfitting hazard (see disclaimer at bottom).

Usage:
  python hmm_regime_analysis.py                  # default: EURUSD, 10y
  python hmm_regime_analysis.py --csv path.csv   # load local CSV
  python hmm_regime_analysis.py --asset BTC-USD --years 5
  python hmm_regime_analysis.py --html out.html  # save HTML instead of open
  python hmm_regime_analysis.py --help

CSV format (when --csv is used):
  timestamp_utc or Date or ts, open, high, low, close, volume
"""

from __future__ import annotations

import argparse
import itertools
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Plotly ──────────────────────────────────────────────────────────────
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

# ── HMM ─────────────────────────────────────────────────────────────────
from hmmlearn.hmm import GaussianHMM

# ── Data source ─────────────────────────────────────────────────────────
try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False

# ── Silent mode for negligible warnings ─────────────────────────────────
warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")
warnings.filterwarnings("ignore", category=UserWarning, module="hmmlearn")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pandas")

pio.templates.default = "plotly_dark"
_REGIME_COLORS = ["#2ecc71", "#3498db", "#e67e22", "#e74c3c", "#9b59b6"]

# ══════════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class HmmConfig:
    """HMM detection parameters."""
    feature_window: int = 20          # rolling window for feature engineering
    min_states: int = 2
    max_states: int = 5
    n_iter: int = 1000
    random_state: int = 42


@dataclass
class StrategyConfig:
    """MA Crossover strategy parameter grid."""
    fast_sma: List[int] = field(default_factory=lambda: [5, 10, 15, 20])
    slow_sma: List[int] = field(default_factory=lambda: [30, 50, 100, 200])
    atr_sl_mult: List[float] = field(default_factory=lambda: [1.5, 2.0, 2.5, 3.0])
    min_trades: int = 30              # minimum trades to accept a param combo


# ══════════════════════════════════════════════════════════════════════════
#  1.  Data loading
# ══════════════════════════════════════════════════════════════════════════

def fetch_data(asset: str = "EURUSD=X", years: int = 10,
               csv_path: Optional[str] = None) -> pd.DataFrame:
    """
    Load daily OHLCV data from yfinance (default) or a local CSV file.
    Returns a DataFrame with columns: open, high, low, close, volume
    and a DatetimeIndex named 'date'.
    """
    if csv_path:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        df = pd.read_csv(path)
        # Try common timestamp column names
        ts_cols = [c for c in ["timestamp_utc", "Date", "date", "ts", "time"]
                   if c in df.columns]
        if ts_cols:
            df = df.rename(columns={ts_cols[0]: "date"})
        else:
            df = df.rename(columns={df.columns[0]: "date"})
        df["date"] = pd.to_datetime(df["date"], utc=True)
        df = df.set_index("date").sort_index()
        # Keep only OHLCV
        keep = {c.lower() for c in ["open", "high", "low", "close", "volume"]}
        cols = [c for c in df.columns if c.lower() in keep]
        df = df[cols]
        df.columns = [c.lower() for c in df.columns]
        # Ensure numeric
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna()
        print(f"  Loaded {len(df)} rows from {csv_path}")
        return df

    if not _HAS_YFINANCE:
        raise ImportError(
            "yfinance is required. Install with: pip install yfinance"
        )

    print(f"  Downloading {asset} — last {years} years (daily)...")
    df = yf.download(asset, period=f"{years}y", interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"No data returned for {asset}")

    # yfinance returns MultiIndex columns; flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0).str.lower()
    # If OHLCV is in (Open, High, Low, Close, Volume) flatten case
    rename = {"open": "open", "high": "high", "low": "low",
              "close": "close", "volume": "volume"}
    df = df.rename(columns=str.lower)
    keep = {c: c for c in ["open", "high", "low", "close", "volume"]
            if c in df.columns}
    df = df[list(keep.keys())]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    print(f"  Downloaded {len(df)} rows")
    return df


# ══════════════════════════════════════════════════════════════════════════
#  2.  Feature engineering
# ══════════════════════════════════════════════════════════════════════════

def build_features(close: pd.Series, window: int = 20) -> pd.DataFrame:
    """
    Build HMM features from daily close prices.
      - log_returns
      - volatility (rolling std of log-returns, annualised)
      - cumulative_return (rolling sum of log-returns)
      - momentum (pct change over window)
    Returns a DataFrame indexed by date with one row per valid observation.
    """
    log_ret = np.log(close / close.shift(1))
    features = pd.DataFrame(index=close.index)
    features["volatility"] = (
        log_ret.rolling(window).std() * np.sqrt(252)
    )
    features["cumulative_return"] = log_ret.rolling(window).sum()
    features["momentum"] = close.pct_change(window)
    # Drop leading NaNs
    features = features.dropna()
    return features


# ══════════════════════════════════════════════════════════════════════════
#  3.  HMM training & state ordering
# ══════════════════════════════════════════════════════════════════════════

def train_hmm(features: pd.DataFrame,
              cfg: HmmConfig) -> Tuple[GaussianHMM, int, np.ndarray]:
    """
    Train GaussianHMM for 2..max_states, select best by BIC,
    and order states by ascending volatility.
    Returns (model, n_states, state_sequence).
    """
    X = features.values
    best_model: Optional[GaussianHMM] = None
    best_bic = np.inf
    best_n = cfg.min_states

    for n in range(cfg.min_states, cfg.max_states + 1):
        model = GaussianHMM(
            n_components=n,
            covariance_type="diag",
            n_iter=cfg.n_iter,
            random_state=cfg.random_state,
            tol=1e-5,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(X)
        # BIC: -2*log_likelihood + n_params * log(n_obs)
        log_lik = model.score(X)
        n_params = n * (n - 1)  # transition probs
        n_params += n * X.shape[1] * 2  # means + covs per state
        bic = -2 * log_lik + n_params * np.log(len(X))
        print(f"    States={n:2d}  logL={log_lik:>10.1f}  params={n_params:3d}  "
              f"BIC={bic:>10.1f}")
        if bic < best_bic:
            best_bic = bic
            best_model = model
            best_n = n

    # State sequence
    states = best_model.predict(X)

    # Order states by mean volatility (feature column 0)
    state_vol = pd.Series(states).groupby(states).apply(
        lambda idx: X[idx, 0].mean()
    )
    # Map: old_state → new_state (0 = lowest vol)
    ordered_map = {old: new for new, old in enumerate(state_vol.argsort())}
    ordered_states = np.array([ordered_map[s] for s in states])
    return best_model, best_n, ordered_states


# ══════════════════════════════════════════════════════════════════════════
#  4.  Backtest engine (MA Crossover with ATR stop-loss)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    side: str                     # "long" | "short"
    regime: int                   # HMM state at entry
    pnl_pct: float
    atr_at_entry: float


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (simple Wilder's method)."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def backtest_ma_crossover(
    df: pd.DataFrame,
    fast: int,
    slow: int,
    atr_mult: float,
    regimes: pd.Series,          # aligned to df.index
) -> Tuple[List[Trade], pd.Series]:
    """
    Run a MA crossover backtest.

    Rules:
      - Long entry when SMA(fast) > SMA(slow) and was below previous bar.
      - Short entry when SMA(fast) < SMA(slow) and was above previous bar.
      - SL placed at entry ± ATR * atr_mult.
      - Exit when SL hit or when the opposite signal occurs.
      - Positions sized at 100% capital (no kelly) — we measure pnl pct.

    Returns (trades, equity_curve).
    """
    close = df["close"]
    sma_f = close.rolling(fast).mean()
    sma_s = close.rolling(slow).mean()
    atr = compute_atr(df)
    regime_align = regimes.reindex(df.index, method="ffill").fillna(0).astype(int)

    in_position = False
    trades: List[Trade] = []
    entry_price = 0.0
    entry_date = pd.NaT
    side = ""
    atr_entry = 0.0
    regime = 0
    sl_price = 0.0

    equity = pd.Series(1.0, index=df.index)
    position_count = 0  # -1 short, 0 flat, +1 long

    for i in range(1, len(df)):
        if not in_position:
            # Check for entry signal
            if ~np.isnan(sma_f.iloc[i]) and ~np.isnan(sma_s.iloc[i]):
                cross_above = (
                    sma_f.iloc[i - 1] <= sma_s.iloc[i - 1] and
                    sma_f.iloc[i] > sma_s.iloc[i]
                )
                cross_below = (
                    sma_f.iloc[i - 1] >= sma_s.iloc[i - 1] and
                    sma_f.iloc[i] < sma_s.iloc[i]
                )
                if cross_above:
                    in_position = True
                    entry_price = close.iloc[i]
                    entry_date = df.index[i]
                    side = "long"
                    atr_entry = atr.iloc[i]
                    regime = int(regime_align.iloc[i])
                    sl_price = entry_price - atr_entry * atr_mult
                    position_count = 1
                elif cross_below:
                    in_position = True
                    entry_price = close.iloc[i]
                    entry_date = df.index[i]
                    side = "short"
                    atr_entry = atr.iloc[i]
                    regime = int(regime_align.iloc[i])
                    sl_price = entry_price + atr_entry * atr_mult
                    position_count = -1
        else:
            # Check exit conditions
            exit_now = False
            exit_price = close.iloc[i]
            exit_date = df.index[i]

            if side == "long":
                # SL hit (intraday approximation: use low)
                if df["low"].iloc[i] <= sl_price:
                    exit_price = sl_price
                    exit_now = True
                # Opposite signal
                elif (~np.isnan(sma_f.iloc[i]) and ~np.isnan(sma_s.iloc[i])
                      and sma_f.iloc[i] < sma_s.iloc[i]
                      and sma_f.iloc[i - 1] >= sma_s.iloc[i - 1]):
                    exit_now = True
                # Time exit on period end
                elif i == len(df) - 1:
                    exit_now = True
            else:  # short
                if df["high"].iloc[i] >= sl_price:
                    exit_price = sl_price
                    exit_now = True
                elif (~np.isnan(sma_f.iloc[i]) and ~np.isnan(sma_s.iloc[i])
                      and sma_f.iloc[i] > sma_s.iloc[i]
                      and sma_f.iloc[i - 1] <= sma_s.iloc[i - 1]):
                    exit_now = True
                elif i == len(df) - 1:
                    exit_now = True

            if exit_now:
                pnl = (
                    (exit_price / entry_price - 1.0)
                    if side == "long"
                    else (entry_price / exit_price - 1.0)
                )
                trades.append(Trade(
                    entry_date=entry_date,
                    exit_date=exit_date,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    side=side,
                    regime=regime,
                    pnl_pct=pnl,
                    atr_at_entry=atr_entry,
                ))
                in_position = False
                position_count = 0

        # Track equity (cumulative pnl)
        if i > 0:
            ret = close.iloc[i] / close.iloc[i - 1] - 1
            equity.iloc[i] = equity.iloc[i - 1] * (1 + ret * position_count)
        else:
            equity.iloc[i] = 1.0

    # Safety — if still in position at end, close at last close
    if in_position:
        pnl = (
            (close.iloc[-1] / entry_price - 1.0)
            if side == "long"
            else (entry_price / close.iloc[-1] - 1.0)
        )
        trades.append(Trade(
            entry_date=entry_date,
            exit_date=df.index[-1],
            entry_price=entry_price,
            exit_price=close.iloc[-1],
            side=side,
            regime=regime,
            pnl_pct=pnl,
            atr_at_entry=atr_entry,
        ))

    return trades, equity


# ══════════════════════════════════════════════════════════════════════════
#  5.  Metrics
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class RegimeMetrics:
    n_trades: int = 0
    sharpe: float = 0.0
    net_profit_pct: float = 0.0
    max_dd_pct: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0


def compute_metrics(trades: List[Trade], equity: pd.Series) -> RegimeMetrics:
    """Compute performance metrics from a list of trades and equity curve."""
    m = RegimeMetrics()
    m.n_trades = len(trades)
    if m.n_trades == 0:
        return m

    pnl_series = pd.Series([t.pnl_pct for t in trades])
    m.net_profit_pct = pnl_series.sum() * 100
    m.win_rate = (pnl_series > 0).mean() * 100

    winners = pnl_series[pnl_series > 0].sum()
    losers = pnl_series[pnl_series < 0].sum()
    m.profit_factor = (
        (winners / abs(losers)) if losers < 0 else float("inf")
    )

    # Sharpe (daily returns from equity curve)
    daily_ret = equity.pct_change().dropna()
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        m.sharpe = np.sqrt(252) * daily_ret.mean() / daily_ret.std()
    else:
        m.sharpe = 0.0

    # Max drawdown
    cum = equity / equity.iloc[0]
    running_max = cum.expanding().max()
    dd = (cum - running_max) / running_max
    m.max_dd_pct = dd.min() * 100

    return m


def compute_trade_metrics(trades: List[Trade]) -> RegimeMetrics:
    """Compute metrics based only on flat trade PnL (no equity curve)."""
    m = RegimeMetrics()
    m.n_trades = len(trades)
    if m.n_trades == 0:
        return m
    pnl = np.array([t.pnl_pct for t in trades])
    m.net_profit_pct = pnl.sum() * 100
    m.win_rate = (pnl > 0).mean() * 100
    winners = pnl[pnl > 0].sum()
    losers = pnl[pnl < 0].sum()
    m.profit_factor = (winners / abs(losers)) if losers < 0 else float("inf")
    # Simple Sharpe from trade returns (not annualised)
    if pnl.std() > 0:
        m.sharpe = pnl.mean() / pnl.std() * np.sqrt(252)
    m.max_dd_pct = 0.0  # Requires equity curve → skipped here
    return m


# ══════════════════════════════════════════════════════════════════════════
#  6.  Parameter sweep
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class SweepResult:
    """One row in the parameter-sweep results table."""
    fast: int
    slow: int
    atr_mult: float
    global_: RegimeMetrics = field(default_factory=RegimeMetrics)
    per_regime: Dict[int, RegimeMetrics] = field(default_factory=dict)
    trade_list: List[Trade] = field(default_factory=list)
    equity: Optional[pd.Series] = None


def run_parameter_sweep(
    df: pd.DataFrame,
    regimes: pd.Series,
    strategy_cfg: StrategyConfig,
) -> List[SweepResult]:
    """Test every combination of strategy parameters."""
    param_grid = list(itertools.product(
        strategy_cfg.fast_sma,
        strategy_cfg.slow_sma,
        strategy_cfg.atr_sl_mult,
    ))
    # Filter out fast >= slow
    param_grid = [(f, s, m) for f, s, m in param_grid if f < s]

    print(f"\n  Parameter sweep: {len(param_grid)} combinations")
    results: List[SweepResult] = []

    for idx, (fast, slow, mult) in enumerate(param_grid):
        sys.stdout.write(
            f"\r    [{idx+1}/{len(param_grid)}] SMA({fast},{slow}) "
            f"ATRx{mult:.1f}  "
        )
        sys.stdout.flush()

        trades, equity = backtest_ma_crossover(df, fast, slow, mult, regimes)
        if len(trades) == 0:
            continue

        r = SweepResult(fast=fast, slow=slow, atr_mult=mult,
                        trade_list=trades, equity=equity)
        r.global_ = compute_trade_metrics(trades)

        # Per-regime breakdown
        regimes_set = sorted({t.regime for t in trades})
        for reg in regimes_set:
            reg_trades = [t for t in trades if t.regime == reg]
            r.per_regime[reg] = compute_trade_metrics(reg_trades)

        results.append(r)

    print()  # newline after progress
    return results


def filter_results(results: List[SweepResult],
                   min_trades: int) -> List[SweepResult]:
    """Remove param combos with fewer than min_trades total trades."""
    return [r for r in results if r.global_.n_trades >= min_trades]


def best_by_regime(results: List[SweepResult],
                   regime: int) -> Optional[SweepResult]:
    """Return the result with highest Sharpe for a given regime."""
    valid = [r for r in results if regime in r.per_regime]
    if not valid:
        return None
    return max(valid, key=lambda r: r.per_regime[regime].sharpe)


# ══════════════════════════════════════════════════════════════════════════
#  7.  Plots
# ══════════════════════════════════════════════════════════════════════════

def plot_regime_timeline(
    df: pd.DataFrame,
    states: np.ndarray,
    n_states: int,
    features: pd.DataFrame,
    model: GaussianHMM,
    save_html: Optional[str] = None,
):
    """
    Timeline with price, regime coloured backdrop, and distribution pie.
    """
    fig = make_subplots(
        rows=2, cols=2,
        row_heights=[0.75, 0.25],
        column_widths=[0.7, 0.3],
        specs=[[{"colspan": 2}, None],
               [{"colspan": 1}, {"colspan": 1}]],
        subplot_titles=("Price & Regime Timeline", "Regime Distribution"),
        vertical_spacing=0.12,
    )

    # Find indices matching features (aligned dates)
    feat_dates = features.index
    # Create regime bands
    for s in range(n_states):
        mask = states == s
        seg_starts = []
        seg_ends = []
        in_seg = False
        for i, m in enumerate(mask):
            if m and not in_seg:
                seg_starts.append(feat_dates[i])
                in_seg = True
            elif not m and in_seg:
                # Find previous date
                if i > 0:
                    seg_ends.append(feat_dates[i - 1])
                else:
                    seg_ends.append(feat_dates[i])
                in_seg = False
        if in_seg:
            seg_ends.append(feat_dates[-1])

        for start, end in zip(seg_starts, seg_ends):
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor=_REGIME_COLORS[s % len(_REGIME_COLORS)],
                opacity=0.25,
                layer="below",
                line_width=0,
                row=1, col=1,
            )

    # Price line
    fig.add_trace(
        go.Scatter(
            x=df.index, y=df["close"],
            line=dict(color="#ffffff", width=1.2),
            name="Close Price",
        ),
        row=1, col=1,
    )

    # Pie chart
    state_counts = pd.Series(states).value_counts().sort_index()
    labels = [f"Regime {s}" for s in range(n_states)]
    fig.add_trace(
        go.Pie(
            labels=labels,
            values=state_counts.values,
            marker=dict(
                colors=[_REGIME_COLORS[s % len(_REGIME_COLORS)]
                        for s in range(n_states)]
            ),
            textinfo="label+percent",
            hole=0.3,
        ),
        row=2, col=2,
    )

    # Transition matrix text
    trans_mat = model.transmat_
    trans_text = "<b>Transition Matrix</b><br>"
    trans_text += "<table style='font-size:11px'>"
    trans_text += "<tr><th></th>"
    for s in range(n_states):
        trans_text += f"<th>R{s}</th>"
    trans_text += "</tr>"
    for s in range(n_states):
        trans_text += f"<tr><td><b>R{s}</b></td>"
        for s2 in range(n_states):
            val = trans_mat[s, s2]
            trans_text += f"<td>{val:.3f}</td>"
        trans_text += "</tr>"
    trans_text += "</table>"

    fig.add_annotation(
        x=0.5, y=0.35,
        xref="paper", yref="paper",
        text=trans_text,
        showarrow=False,
        align="left",
        bordercolor="#555",
        borderwidth=1,
        bgcolor="rgba(0,0,0,0.6)",
    )

    fig.update_layout(
        title=f"<b>HMM Regime Detection — {n_states} States</b>",
        template="plotly_dark",
        height=700,
        showlegend=False,
    )

    _show_or_save(fig, save_html, "regime_timeline.html")
    print(f"  ✓ Regime timeline {'saved' if save_html else 'opened in browser'}")


def _best_params_table(results: List[SweepResult],
                       all_regimes: List[int]) -> go.Figure:
    """Build a Plotly table showing the best param combo per regime."""
    headers = ["Regime"] + [
        "SMA Fast", "SMA Slow", "ATR Mult",
        "Trades", "Sharpe", "Net Profit %", "Max DD %", "Profit Factor",
    ]
    rows = []

    # Global best
    if results:
        best_global = max(results, key=lambda r: r.global_.sharpe)
        g = best_global.global_
        rows.append([
            f"<b>Global</b>",
            str(best_global.fast), str(best_global.slow),
            f"{best_global.atr_mult:.1f}",
            str(g.n_trades),
            f"{g.sharpe:.2f}",
            f"{g.net_profit_pct:.1f}",
            f"{g.max_dd_pct:.1f}",
            f"{g.profit_factor:.2f}",
        ])

    for reg in sorted(all_regimes):
        best = best_by_regime(results, reg)
        if best is None:
            continue
        m = best.per_regime[reg]
        rows.append([
            f"Regime {reg}",
            str(best.fast), str(best.slow),
            f"{best.atr_mult:.1f}",
            str(m.n_trades),
            f"{m.sharpe:.2f}",
            f"{m.net_profit_pct:.1f}",
            f"{m.max_dd_pct:.1f}",
            f"{m.profit_factor:.2f}",
        ])

    fig = go.Figure(data=[
        go.Table(
            header=dict(values=headers, align="center",
                        fill_color="#1a1a2e", font=dict(color="white")),
            cells=dict(values=list(zip(*rows)), align="center",
                       fill_color="#16213e",
                       font=dict(color=["white"], size=12),
                       height=28),
        )
    ])
    fig.update_layout(
        title="<b>Best Parameter Combination per Regime</b>",
        template="plotly_dark",
        height=200 + len(rows) * 35,
    )
    return fig


def plot_equity_curves(
    results: List[SweepResult],
    df: pd.DataFrame,
    states: np.ndarray,
    all_regimes: List[int],
    save_html: Optional[str] = None,
):
    """Equity curves: global best, per-regime best, combined regime equity."""
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.25, 0.20],
        vertical_spacing=0.06,
    )

    # ── Row 1: Price + Global best equity ──
    best_global = max(results, key=lambda r: r.global_.sharpe)
    eq = best_global.equity
    if eq is not None:
        eq_norm = eq / eq.iloc[0]
        fig.add_trace(
            go.Scatter(x=eq.index, y=eq_norm,
                       line=dict(color="#00ff88", width=1.5),
                       name="Equity (Global Best)"),
            row=1, col=1,
        )
    fig.add_trace(
        go.Scatter(x=df.index, y=df["close"] / df["close"].iloc[0],
                   line=dict(color="#888", width=1, dash="dot"),
                   name="Price (normalised)"),
        row=1, col=1,
    )

    # ── Row 2: Regime backdrop + regime equity curves ──
    # Regime background bands
    feat_dates = pd.Series(df.index).pipe(
        lambda s: s[s.isin(states)] if len(states) != len(df) else s
    )
    # Use feature-aligned dates
    align_states = states
    feat_dates = df.index[-len(states):]  # states aligned to last N rows of df

    # Actually, states are predicted on feature rows. Let me re-align.
    # The number of states is same as len(features), which is len(df) - feature_window
    # I need to match this to the df index properly.
    feature_start = len(df.index) - len(states)
    feat_dates = df.index[feature_start:]

    for s in range(len({s for s in states})):
        mask = states == s
        seg_starts = []
        seg_ends = []
        in_seg = False
        for i, m in enumerate(mask):
            if m and not in_seg:
                seg_starts.append(feat_dates[i])
                in_seg = True
            elif not m and in_seg:
                seg_ends.append(feat_dates[i - 1] if i > 0 else feat_dates[i])
                in_seg = False
        if in_seg:
            seg_ends.append(feat_dates[-1])
        for start, end in zip(seg_starts, seg_ends):
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor=_REGIME_COLORS[s % len(_REGIME_COLORS)],
                opacity=0.2, layer="below", line_width=0,
                row=2, col=1,
            )

    # Per-regime best equity curves
    for reg in sorted(all_regimes):
        best = best_by_regime(results, reg)
        if best is None or best.equity is None:
            continue
        eq_reg = best.equity / best.equity.iloc[0]
        fig.add_trace(
            go.Scatter(x=eq_reg.index, y=eq_reg,
                       line=dict(color=_REGIME_COLORS[reg % len(_REGIME_COLORS)],
                                 width=1.2),
                       name=f"Best for Regime {reg}"),
            row=2, col=1,
        )

    # Row 2 background: price
    fig.add_trace(
        go.Scatter(x=df.index, y=df["close"] / df["close"].iloc[0],
                   line=dict(color="#555", width=0.8, dash="dot"),
                   showlegend=False,
                   name="Price"),
        row=2, col=1,
    )

    # ── Row 3: Regime bar chart ──
    # Create a discrete regime bar
    step = max(1, len(feat_dates) // 300)  # sample for performance
    regime_bar = pd.Series(states[::step], index=feat_dates[::step])
    fig.add_trace(
        go.Bar(
            x=regime_bar.index,
            y=regime_bar.values,
            marker=dict(
                color=[_REGIME_COLORS[s % len(_REGIME_COLORS)]
                       for s in regime_bar.values]
            ),
            width=86400000 * 3,  # ~3 days width
            name="Regime",
            showlegend=False,
        ),
        row=3, col=1,
    )

    fig.update_layout(
        title="<b>Equity Curves — Global & Per-Regime</b>",
        template="plotly_dark",
        height=900,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Equity (normalised)", row=1, col=1)
    fig.update_yaxes(title_text="Equity (norm)", row=2, col=1)
    fig.update_yaxes(title_text="Regime", row=3, col=1,
                     tickvals=list(range(5)),
                     ticktext=[f"R{r}" for r in range(5)])

    _show_or_save(fig, save_html, "equity_curves.html")
    print(f"  ✓ Equity curves {'saved' if save_html else 'opened in browser'}")


def plot_sharpe_heatmap(
    results: List[SweepResult],
    regimes_to_show: List[int],
    save_html: Optional[str] = None,
):
    """Heatmap of Sharpe ratio for the two most important params: fast SMA & slow SMA."""
    for reg in regimes_to_show:
        # Build pivot table: fast × slow → Sharpe (average over ATR values)
        data = []
        for r in results:
            if reg not in r.per_regime:
                continue
            data.append({
                "fast": r.fast,
                "slow": r.slow,
                "sharpe": r.per_regime[reg].sharpe,
            })
        if not data:
            continue
        pivot = pd.DataFrame(data).groupby(
            ["fast", "slow"], as_index=False
        )["sharpe"].mean()  # average over ATR multiplier
        pivot_tbl = pivot.pivot(index="fast", columns="slow", values="sharpe")
        pivot_tbl = pivot_tbl.sort_index(ascending=False)

        fig = go.Figure(data=go.Heatmap(
            z=pivot_tbl.values,
            x=list(pivot_tbl.columns),
            y=list(pivot_tbl.index),
            colorscale="RdYlGn",
            zmid=0,
            text=np.round(pivot_tbl.values, 2),
            texttemplate="%{text}",
            hovertemplate="Fast SMA: %{y}<br>Slow SMA: %{x}<br>Sharpe: %{z:.2f}<extra></extra>",
        ))
        fig.update_layout(
            title=f"<b>Sharpe Heatmap — Regime {reg}</b>",
            template="plotly_dark",
            xaxis_title="Slow SMA",
            yaxis_title="Fast SMA",
            height=500,
        )
        suffix = f"_regime_{reg}"
        _show_or_save(fig, save_html, f"sharpe_heatmap{suffix}.html")
        print(f"  ✓ Sharpe heatmap (Regime {reg}) "
              f"{'saved' if save_html else 'opened in browser'}")


def _show_or_save(fig: go.Figure, save_dir: Optional[str], filename: str):
    """Open in browser or save to HTML file."""
    if save_dir:
        path = Path(save_dir)
        path.mkdir(parents=True, exist_ok=True)
        fig.write_html(str(path / filename), include_plotlyjs="cdn")
    else:
        fig.show()


# ══════════════════════════════════════════════════════════════════════════
#  8.  Disclaimer
# ══════════════════════════════════════════════════════════════════════════

DISCLAIMER = """
╔══════════════════════════════════════════════════════════════════════════╗
║  ⚠️  IN-SAMPLE OVERFITTING WARNING  ⚠️                                   ║
║                                                                          ║
║  The parameter sweep in this script is performed on the ENTIRE           ║
║  historical dataset (in-sample). Results ARE severely overfitted         ║
║  and should NEVER be used as-is for live trading decisions.              ║
║                                                                          ║
║  How HMM regime analysis SHOULD be used:                                 ║
║                                                                          ║
║  1. UNIVERSALIST vs SPECIALIST concept:                                  ║
║     Train HMM ONCE to classify which regime the market is in today.      ║
║     Use this classification to SELECT which strategies to ACTIVATE       ║
║     (and at what position sizing), NOT to change signal parameters.      ║
║                                                                          ║
║  2. At the CODE level:                                                   ║
║     - Strategy A runs only in Regime 0–1 (low vol / trending)            ║
║     - Strategy B runs only in Regime 2–3 (high vol / ranging)            ║
║     - Position size is adjusted by regime (e.g. 50% in Regime 3)        ║
║                                                                          ║
║  3. What NOT to do:                                                      ║
║     ❌ Cross-validating strategy parameters per regime on past data       ║
║     ❌ Selecting the "best" SMA lengths per regime from this sweep        ║
║     ❌ Assuming past regime transitions predict future transitions        ║
║                                                                          ║
║  Use this tool for EXPLORATION and EDUCATION only.                       ║
╚══════════════════════════════════════════════════════════════════════════╝
"""


# ══════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="HMM Regime Analysis for MA Crossover Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Example:
  python hmm_regime_analysis.py                       # EURUSD, 10y
  python hmm_regime_analysis.py --asset BTC-USD --years 5
  python hmm_regime_analysis.py --csv data.csv --html ./output/
  python hmm_regime_analysis.py --fast 5,10,20 --slow 30,50,100
""",
    )
    parser.add_argument("--asset", default="EURUSD=X",
                        help="Yahoo Finance ticker (default: EURUSD=X)")
    parser.add_argument("--years", type=int, default=10,
                        help="Years of history to fetch (default: 10)")
    parser.add_argument("--csv", default=None,
                        help="Path to local CSV file (overrides --asset)")
    parser.add_argument("--html", default=None,
                        help="Output directory for saved HTML files "
                             "(default: open browser windows)")
    parser.add_argument("--fast", default=None,
                        help="Comma-separated fast SMA values "
                             "(default: 5,10,15,20)")
    parser.add_argument("--slow", default=None,
                        help="Comma-separated slow SMA values "
                             "(default: 30,50,100,200)")
    parser.add_argument("--atr-mult", default=None,
                        help="Comma-separated ATR SL multipliers "
                             "(default: 1.5,2.0,2.5,3.0)")
    parser.add_argument("--no-plots", action="store_true",
                        help="Skip plot generation (console only)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    args = parser.parse_args()

    # ── Parse parameter grids ──
    def parse_int_list(val: str) -> List[int]:
        return [int(x.strip()) for x in val.split(",")]

    def parse_float_list(val: str) -> List[float]:
        return [float(x.strip()) for x in val.split(",")]

    strat_cfg = StrategyConfig()
    if args.fast:
        strat_cfg.fast_sma = parse_int_list(args.fast)
    if args.slow:
        strat_cfg.slow_sma = parse_int_list(args.slow)
    if args.atr_mult:
        strat_cfg.atr_sl_mult = parse_float_list(args.atr_mult)

    hmm_cfg = HmmConfig(random_state=args.seed)

    # ── Step 0: Header ──
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║            HMM Regime Analysis — MA Crossover               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # ── Step 1: Load data ──
    print("─" * 50)
    print("  1. Loading data...")
    print("─" * 50)
    try:
        df = fetch_data(asset=args.asset, years=args.years, csv_path=args.csv)
    except (FileNotFoundError, ValueError, ImportError) as e:
        print(f"  ❌ {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Range: {df.index[0].date()} → {df.index[-1].date()}")
    print()

    # ── Step 2: Build features ──
    print("─" * 50)
    print("  2. Engineering features...")
    print("─" * 50)
    features = build_features(df["close"], window=hmm_cfg.feature_window)
    print(f"  Features computed: {len(features)} rows "
          f"({hmm_cfg.feature_window}-day rolling window)")
    print(f"    • Volatility (rolling std × √252)")
    print(f"    • Cumulative return (rolling sum of log-returns)")
    print(f"    • Momentum ({hmm_cfg.feature_window}-day pct change)")
    print()

    # ── Step 3: HMM training ──
    print("─" * 50)
    print("  3. Training GaussianHMM...")
    print("─" * 50)
    model, n_states, states = train_hmm(features, hmm_cfg)
    trans = model.transmat_
    print(f"\n  ✓ Best model: {n_states} states (BIC-selected)")
    print(f"  Transition matrix:")
    for s in range(n_states):
        print(f"    R{s}: " + "  ".join(f"{trans[s, s2]:.3f}" for s2 in range(n_states)))
    print()

    # Distribution
    state_counts = pd.Series(states).value_counts().sort_index()
    for s in range(n_states):
        pct = state_counts.get(s, 0) / len(states) * 100
        print(f"    Regime {s}: {pct:.1f}% of observations")

    # Descriptive stats per regime
    feat_df = features.copy()
    feat_df["regime"] = states
    print(f"\n  Feature means by regime:")
    for s in range(n_states):
        subset = feat_df[feat_df["regime"] == s]
        print(f"    R{s}: vol={subset['volatility'].mean():.4f}  "
              f"cum_ret={subset['cumulative_return'].mean():.4f}  "
              f"mom={subset['momentum'].mean():.4f}")
    print()

    # ── Step 4: Parameter sweep ──
    print("─" * 50)
    print("  4. Running parameter sweep...")
    print("─" * 50)
    # Align regime labels to df: states were computed on feature matrix which
    # corresponds to the last N = len(states) rows of df.
    feature_start = len(df.index) - len(states)
    regime_series = pd.Series(
        states, index=df.index[feature_start:], dtype=int
    )
    # Fill regime for earliest rows (before feature_start) with regime 0
    regime_series_full = (
        pd.Series(0, index=df.index).combine_first(
            regime_series.to_frame("r")["r"]
        )
        .fillna(0).astype(int)
    )

    # To run backtest on all data, we need the full series. Let me mask the
    # early part as "unknown". We'll still run the sweep on the full df but
    # regime = 0 for early rows (it won't matter much as those pre-feature
    # dates won't have many trades anyway due to SMA startup lag).
    raw_results = run_parameter_sweep(df, regime_series_full, strat_cfg)

    print(f"\n  Raw param combos: {len(raw_results)} "
          f"(with ≥1 trade)")
    results = filter_results(raw_results, strat_cfg.min_trades)
    print(f"  After filter (≥{strat_cfg.min_trades} trades): {len(results)}")

    if not results:
        print("  ❌ No param combinations passed the minimum trade filter.")
        print("     Try reducing --fast / --slow minimums or "
              "lower min_trades threshold.")
        sys.exit(0)

    # ── Report best by regime ──
    all_regimes = sorted({k for r in results for k in r.per_regime})

    print(f"\n  {'Regime':<10} {'SMA-F':<6} {'SMA-S':<6} {'ATRx':<5} "
          f"{'Trades':<7} {'Sharpe':<7} {'Net%':<8} {'WF':<5}")
    print("  " + "-" * 55)

    # Global best
    best_global = max(results, key=lambda r: r.global_.sharpe)
    g = best_global.global_
    print(f"  {'Global':<10} {best_global.fast:<6} {best_global.slow:<6} "
          f"{best_global.atr_mult:<5.1f} {g.n_trades:<7} {g.sharpe:<7.2f} "
          f"{g.net_profit_pct:<8.1f} {g.profit_factor:<5.2f}")

    for reg in all_regimes:
        best = best_by_regime(results, reg)
        if best is None:
            continue
        m = best.per_regime[reg]
        print(f"  {'Regime ' + str(reg):<10} {best.fast:<6} {best.slow:<6} "
              f"{best.atr_mult:<5.1f} {m.n_trades:<7} {m.sharpe:<7.2f} "
              f"{m.net_profit_pct:<8.1f} {m.profit_factor:<5.2f}")
    print()

    # Print disclaimer
    print(DISCLAIMER)

    # ── Step 5: Plots ──
    if not args.no_plots:
        print("─" * 50)
        print("  5. Generating plots...")
        print("─" * 50)
        try:
            plot_regime_timeline(df, states, n_states, features, model,
                                save_html=args.html)
            _tb = _best_params_table(results, all_regimes)
            _show_or_save(_tb, args.html, "best_params_table.html")
            print(f"  ✓ Best params table {'saved' if args.html else 'opened in browser'}")
            plot_equity_curves(results, df, states, all_regimes,
                              save_html=args.html)
            plot_sharpe_heatmap(results, all_regimes, save_html=args.html)
            print("\n  ✓ All plots generated successfully.")
        except Exception as e:
            print(f"\n  ⚠️  Plot generation error: {e}")
            print("  (Results are still valid; rerun with --no-plots for "
                  "console-only mode)")
    else:
        print("  (Plots skipped via --no-plots)")

    print("\n✅  Analysis complete.")


if __name__ == "__main__":
    main()
