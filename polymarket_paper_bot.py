#!/usr/bin/env python3
"""
Polymarket Paper Bot (Kronos-style) - offline simulator

Inputs: CSV with OHLC + model and market probabilities.
Strategy:
- Direction from p_model_up
- Edge filter vs p_market_up
- ATR/ADX gates
- Quarter-Kelly sizing (capped)
- Daily loss cap, trade cap, loss-streak pause
- Full decision/trade logging
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Candle:
    ts: datetime
    instrument: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    p_model_up: float
    p_market_up: float
    tv_recommendation: str = ""
    tv_confidence: float = 0.0


@dataclass
class Config:
    initial_equity: float = 1000.0
    edge_min: float = 0.03
    tv_filter_enabled: bool = False
    tv_min_confidence: float = 0.55
    tv_alignment_required: bool = True
    atr_period: int = 14
    atr_min_ratio: float = 0.0015
    adx_period: int = 14
    adx_min: float = 18.0
    payout_multiplier: float = 0.95
    kelly_fraction: float = 0.25
    max_risk_per_trade: float = 0.02
    max_daily_loss_percent: float = 3.0
    max_trades_per_day: int = 20
    loss_streak_pause: int = 3
    pause_hours: int = 24


def parse_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_candles(path: Path) -> List[Candle]:
    rows: List[Candle] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        required = {
            "timestamp_utc",
            "instrument",
            "timeframe",
            "open",
            "high",
            "low",
            "close",
            "p_model_up",
            "p_market_up",
        }
        has_tv_cols = ("tv_recommendation" in (r.fieldnames or [])) and ("tv_confidence" in (r.fieldnames or []))
        missing = required - set(r.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns in input CSV: {sorted(missing)}")

        for row in r:
            rows.append(
                Candle(
                    ts=parse_ts(row["timestamp_utc"]),
                    instrument=row["instrument"].strip(),
                    timeframe=row["timeframe"].strip(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    p_model_up=float(row["p_model_up"]),
                    p_market_up=float(row["p_market_up"]),
                    tv_recommendation=(row.get("tv_recommendation", "") if has_tv_cols else "").strip().upper(),
                    tv_confidence=float(row.get("tv_confidence", 0.0) or 0.0) if has_tv_cols else 0.0,
                )
            )
    rows.sort(key=lambda x: x.ts)
    return rows


def true_range(curr: Candle, prev_close: float) -> float:
    return max(curr.high - curr.low, abs(curr.high - prev_close), abs(curr.low - prev_close))


def compute_atr(candles: List[Candle], period: int) -> List[Optional[float]]:
    atr: List[Optional[float]] = [None] * len(candles)
    trs: List[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c.high - c.low
        else:
            tr = true_range(c, candles[i - 1].close)
        trs.append(tr)

        if i + 1 < period:
            continue
        if i + 1 == period:
            atr[i] = sum(trs[:period]) / period
        else:
            prev_atr = atr[i - 1]
            if prev_atr is None:
                continue
            atr[i] = ((prev_atr * (period - 1)) + tr) / period
    return atr


def compute_adx(candles: List[Candle], period: int) -> List[Optional[float]]:
    n = len(candles)
    adx: List[Optional[float]] = [None] * n
    if n < period + 2:
        return adx

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr[i] = true_range(candles[i], candles[i - 1].close)

    tr_s = [None] * n
    pdm_s = [None] * n
    mdm_s = [None] * n

    tr_s[period] = sum(tr[1 : period + 1])
    pdm_s[period] = sum(plus_dm[1 : period + 1])
    mdm_s[period] = sum(minus_dm[1 : period + 1])

    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n

    def safe_div(a: float, b: float) -> float:
        return 0.0 if b == 0 else a / b

    plus_di[period] = 100 * safe_div(pdm_s[period], tr_s[period])
    minus_di[period] = 100 * safe_div(mdm_s[period], tr_s[period])
    denom = (plus_di[period] or 0) + (minus_di[period] or 0)
    dx[period] = 0.0 if denom == 0 else 100 * abs((plus_di[period] or 0) - (minus_di[period] or 0)) / denom

    for i in range(period + 1, n):
        tr_s[i] = (tr_s[i - 1] - (tr_s[i - 1] / period) + tr[i]) if tr_s[i - 1] is not None else None
        pdm_s[i] = (pdm_s[i - 1] - (pdm_s[i - 1] / period) + plus_dm[i]) if pdm_s[i - 1] is not None else None
        mdm_s[i] = (mdm_s[i - 1] - (mdm_s[i - 1] / period) + minus_dm[i]) if mdm_s[i - 1] is not None else None

        if tr_s[i] is None:
            continue
        plus_di[i] = 100 * safe_div(pdm_s[i] or 0.0, tr_s[i])
        minus_di[i] = 100 * safe_div(mdm_s[i] or 0.0, tr_s[i])
        denom2 = (plus_di[i] or 0) + (minus_di[i] or 0)
        dx[i] = 0.0 if denom2 == 0 else 100 * abs((plus_di[i] or 0) - (minus_di[i] or 0)) / denom2

    # ADX init
    start = period * 2
    if start >= n:
        return adx

    seed_vals = [v for v in dx[period : start + 1] if v is not None]
    if len(seed_vals) < period:
        return adx

    adx[start] = sum(seed_vals[:period]) / period
    for i in range(start + 1, n):
        if dx[i] is None or adx[i - 1] is None:
            continue
        adx[i] = ((adx[i - 1] * (period - 1)) + dx[i]) / period

    return adx


def kelly_fraction_for_trade(p: float, b: float) -> float:
    q = 1.0 - p
    if b <= 0:
        return 0.0
    f = (b * p - q) / b
    return max(0.0, f)


def side_and_edge(p_model_up: float, p_market_up: float) -> (str, float, float):
    if p_model_up >= 0.5:
        side = "UP"
        p_side_model = p_model_up
        p_side_market = p_market_up
    else:
        side = "DOWN"
        p_side_model = 1.0 - p_model_up
        p_side_market = 1.0 - p_market_up
    edge = p_side_model - p_side_market
    return side, p_side_model, edge


def run_sim(candles: List[Candle], cfg: Config, out_dir: Path) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    summary_path = out_dir / "summary.json"

    dec_fields = [
        "timestamp_utc",
        "instrument",
        "timeframe",
        "action",
        "reason_code",
        "p_model_up",
        "p_market_up",
        "tv_recommendation",
        "tv_confidence",
        "edge",
        "atr",
        "adx",
        "stake",
        "equity",
    ]
    tr_fields = [
        "entry_timestamp_utc",
        "exit_timestamp_utc",
        "instrument",
        "timeframe",
        "side",
        "stake",
        "p_side_model",
        "edge",
        "entry_price",
        "exit_price",
        "result",
        "pnl",
        "equity_after",
    ]

    atr = compute_atr(candles, cfg.atr_period)
    adx = compute_adx(candles, cfg.adx_period)

    equity = cfg.initial_equity
    peak_equity = equity
    max_dd = 0.0

    daily_state: Dict[str, Dict] = {}
    loss_streak = 0
    pause_until: Optional[datetime] = None

    wins = 0
    losses = 0
    trades = 0

    with decisions_path.open("w", newline="", encoding="utf-8") as dfile, trades_path.open(
        "w", newline="", encoding="utf-8"
    ) as tfile:
        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        dwr.writeheader()
        twr.writeheader()

        # one-step horizon: entry at close[i], exit at close[i+1]
        for i in range(max(cfg.atr_period, cfg.adx_period * 2), len(candles) - 1):
            c = candles[i]
            nx = candles[i + 1]

            day = c.ts.date().isoformat()
            if day not in daily_state:
                daily_state[day] = {
                    "start_equity": equity,
                    "realized_pnl": 0.0,
                    "trades": 0,
                }

            ds = daily_state[day]

            action = "NO_TRADE"
            reason = ""
            stake = 0.0
            atr_i = atr[i]
            adx_i = adx[i]

            # hard gates
            if pause_until and c.ts < pause_until:
                reason = "STREAK_PAUSE"
            elif ds["trades"] >= cfg.max_trades_per_day:
                reason = "MAX_TRADES_DAY"
            else:
                daily_loss_pct = 0.0
                if ds["start_equity"] > 0:
                    daily_loss_pct = max(0.0, (-ds["realized_pnl"] / ds["start_equity"]) * 100.0)
                if daily_loss_pct >= cfg.max_daily_loss_percent:
                    reason = "DAILY_LOSS_LIMIT"

            if not reason:
                if atr_i is None or adx_i is None:
                    reason = "INDICATORS_NOT_READY"
                elif (atr_i / c.close) < cfg.atr_min_ratio:
                    reason = "ATR_TOO_LOW"
                elif adx_i < cfg.adx_min:
                    reason = "ADX_TOO_LOW"

            side = ""
            p_side_model = 0.0
            edge = 0.0
            if not reason:
                side, p_side_model, edge = side_and_edge(c.p_model_up, c.p_market_up)
                if edge < cfg.edge_min:
                    reason = "EDGE_TOO_LOW"

            if not reason and cfg.tv_filter_enabled:
                if c.tv_confidence < cfg.tv_min_confidence:
                    reason = "TV_CONFIDENCE_TOO_LOW"
                elif cfg.tv_alignment_required:
                    tv_side = c.tv_recommendation
                    if tv_side not in ("BUY", "SELL"):
                        reason = "TV_SIGNAL_INVALID"
                    else:
                        expected = "BUY" if side == "UP" else "SELL"
                        if tv_side != expected:
                            reason = "TV_DIRECTION_MISMATCH"

            if reason:
                dwr.writerow(
                    {
                        "timestamp_utc": c.ts.isoformat(),
                        "instrument": c.instrument,
                        "timeframe": c.timeframe,
                        "action": action,
                        "reason_code": reason,
                        "p_model_up": round(c.p_model_up, 6),
                        "p_market_up": round(c.p_market_up, 6),
                        "tv_recommendation": c.tv_recommendation,
                        "tv_confidence": round(c.tv_confidence, 6),
                        "edge": round(edge, 6),
                        "atr": "" if atr_i is None else round(atr_i, 8),
                        "adx": "" if adx_i is None else round(adx_i, 6),
                        "stake": stake,
                        "equity": round(equity, 2),
                    }
                )
                continue

            # position sizing
            kelly = kelly_fraction_for_trade(p_side_model, cfg.payout_multiplier)
            raw_fraction = cfg.kelly_fraction * kelly
            fraction = min(cfg.max_risk_per_trade, max(0.0, raw_fraction))
            stake = equity * fraction

            if stake <= 0:
                dwr.writerow(
                    {
                        "timestamp_utc": c.ts.isoformat(),
                        "instrument": c.instrument,
                        "timeframe": c.timeframe,
                        "action": "NO_TRADE",
                        "reason_code": "STAKE_ZERO",
                        "p_model_up": round(c.p_model_up, 6),
                        "p_market_up": round(c.p_market_up, 6),
                        "tv_recommendation": c.tv_recommendation,
                        "tv_confidence": round(c.tv_confidence, 6),
                        "edge": round(edge, 6),
                        "atr": round(atr_i, 8),
                        "adx": round(adx_i, 6),
                        "stake": 0.0,
                        "equity": round(equity, 2),
                    }
                )
                continue

            # execute paper trade, settle on next candle close
            up_realized = nx.close > c.close
            won = (side == "UP" and up_realized) or (side == "DOWN" and not up_realized)
            pnl = stake * cfg.payout_multiplier if won else -stake
            equity += pnl

            trades += 1
            ds["trades"] += 1
            ds["realized_pnl"] += pnl

            if won:
                wins += 1
                loss_streak = 0
                result = "WIN"
            else:
                losses += 1
                loss_streak += 1
                result = "LOSS"
                if loss_streak >= cfg.loss_streak_pause:
                    pause_until = c.ts + timedelta(hours=cfg.pause_hours)

            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            max_dd = max(max_dd, dd)

            dwr.writerow(
                {
                    "timestamp_utc": c.ts.isoformat(),
                    "instrument": c.instrument,
                    "timeframe": c.timeframe,
                    "action": "TRADE_EXECUTED",
                    "reason_code": "EDGE_AND_FILTERS_OK",
                    "p_model_up": round(c.p_model_up, 6),
                    "p_market_up": round(c.p_market_up, 6),
                    "tv_recommendation": c.tv_recommendation,
                    "tv_confidence": round(c.tv_confidence, 6),
                    "edge": round(edge, 6),
                    "atr": round(atr_i, 8),
                    "adx": round(adx_i, 6),
                    "stake": round(stake, 6),
                    "equity": round(equity, 2),
                }
            )

            twr.writerow(
                {
                    "entry_timestamp_utc": c.ts.isoformat(),
                    "exit_timestamp_utc": nx.ts.isoformat(),
                    "instrument": c.instrument,
                    "timeframe": c.timeframe,
                    "side": side,
                    "stake": round(stake, 6),
                    "p_side_model": round(p_side_model, 6),
                    "edge": round(edge, 6),
                    "entry_price": round(c.close, 8),
                    "exit_price": round(nx.close, 8),
                    "result": result,
                    "pnl": round(pnl, 6),
                    "equity_after": round(equity, 6),
                }
            )

    win_rate = (wins / trades) * 100.0 if trades else 0.0
    total_pnl = equity - cfg.initial_equity

    summary = {
        "initial_equity": cfg.initial_equity,
        "final_equity": round(equity, 6),
        "total_pnl": round(total_pnl, 6),
        "total_trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(win_rate, 4),
        "max_drawdown_percent": round(max_dd * 100.0, 4),
        "config": cfg.__dict__,
        "outputs": {
            "decisions_log": str(decisions_path),
            "trades_log": str(trades_path),
            "summary_json": str(summary_path),
        },
    }

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_config(path: Optional[Path]) -> Config:
    if path is None:
        return Config()
    data = json.loads(path.read_text(encoding="utf-8"))
    c = Config()
    for k, v in data.items():
        if not hasattr(c, k):
            raise ValueError(f"Unknown config key: {k}")
        setattr(c, k, v)
    return c


def main() -> None:
    p = argparse.ArgumentParser(description="Run Polymarket Kronos-style paper simulation")
    p.add_argument("--input", required=True, help="CSV input path")
    p.add_argument("--output-dir", default="paper_runs/latest", help="Output directory")
    p.add_argument("--config", default=None, help="Optional JSON config")
    args = p.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output_dir)
    cfg_path = Path(args.config) if args.config else None

    cfg = load_config(cfg_path)
    candles = load_candles(input_path)
    if len(candles) < 100:
        raise ValueError("Need at least 100 rows for stable ATR/ADX warmup.")

    summary = run_sim(candles, cfg, out_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
