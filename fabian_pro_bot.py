#!/usr/bin/env python3
"""
FabianPro — Fusión de FabiánPullback + lo mejor de PolyKronos y PolyPortfolio.
- Estructura de mercado + breakout + pullback (de Fabian)
- Filtro ADX para evitar rangos (de PolyKronos)
- TP/SL dinámicos basados en ATR (adaptativo)
- Tracking de cartera (de PolyPortfolio)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str


@dataclass
class FabianProConfig:
    # Risk
    risk_percent: float = 2.0
    reduced_risk_percent: float = 1.0
    max_daily_loss_pct: float = 10.0
    max_trades_per_session: int = 3
    max_trades_per_day: int = 6
    min_rr: float = 1.0
    sl_buffer_pips: float = 0.2
    enable_trailing: bool = True
    enable_break_even: bool = True
    crypto_mode: bool = True

    # Structure
    swing_lookback: int = 2
    structure_bars: int = 60
    body_avg_period: int = 14
    force_body_multiplier: float = 1.2
    max_wick_to_body_ratio: float = 2.0
    entry_mode: int = 0
    pending_order_expiry_minutes: int = 60
    max_entries_per_structure: int = 1
    min_break_pullback_candles: int = 0
    max_break_pullback_candles: int = 10

    # ADX filter (from PolyKronos)
    use_adx_filter: bool = True
    adx_period: int = 14
    adx_min: float = 20.0

    # ATR sizing (adaptive)
    use_atr_sizing: bool = True
    atr_period: int = 14
    atr_multiplier_sl: float = 1.5
    atr_multiplier_tp: float = 2.5

    # Portfolio (from PolyPortfolio)
    partial_take_profit_pct: float = 0.0

    # General
    initial_balance: float = 100.0


@dataclass
class TradePlan:
    action: str
    entry: float
    sl: float
    tp: float
    volume: float
    risk_usd: float
    reward_usd: float
    rr: float
    session: str
    reason: str
    expiry_idx: int


def find_swing_highs(highs: List[float], lookback: int, max_bars: int) -> List[SwingPoint]:
    n = len(highs)
    end = n - lookback - 1
    start = max(lookback, end - max_bars)
    result = []
    for i in range(start, end + 1):
        h = highs[i]
        is_swing = True
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n or h <= highs[i - j] or h <= highs[i + j]:
                is_swing = False
                break
        if is_swing:
            result.append(SwingPoint(i, h, "high"))
    return result


def find_swing_lows(lows: List[float], lookback: int, max_bars: int) -> List[SwingPoint]:
    n = len(lows)
    end = n - lookback - 1
    start = max(lookback, end - max_bars)
    result = []
    for i in range(start, end + 1):
        l = lows[i]
        is_swing = True
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n or l >= lows[i - j] or l >= lows[i + j]:
                is_swing = False
                break
        if is_swing:
            result.append(SwingPoint(i, l, "low"))
    return result


def compute_adx(candles: List[Candle], period: int) -> List[float]:
    """ADX calculation (from PolyKronos)."""
    n = len(candles)
    adx = [0.0] * n
    if n < period * 2 + 2:
        return adx

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(candles[i].high - candles[i].low,
                    abs(candles[i].high - candles[i - 1].close),
                    abs(candles[i].low - candles[i - 1].close))

    tr_s = [0.0] * n
    pdm_s = [0.0] * n
    mdm_s = [0.0] * n
    tr_s[period] = sum(tr[1:period + 1])
    pdm_s[period] = sum(plus_dm[1:period + 1])
    mdm_s[period] = sum(minus_dm[1:period + 1])

    pdi, mdi, dx = [0.0] * n, [0.0] * n, [0.0] * n
    pdi[period] = 100 * pdm_s[period] / tr_s[period] if tr_s[period] > 0 else 0
    mdi[period] = 100 * mdm_s[period] / tr_s[period] if tr_s[period] > 0 else 0
    denom = pdi[period] + mdi[period]
    dx[period] = 100 * abs(pdi[period] - mdi[period]) / denom if denom > 0 else 0

    for i in range(period + 1, n):
        tr_s[i] = tr_s[i - 1] - tr_s[i - 1] / period + tr[i]
        pdm_s[i] = pdm_s[i - 1] - pdm_s[i - 1] / period + plus_dm[i]
        mdm_s[i] = mdm_s[i - 1] - mdm_s[i - 1] / period + minus_dm[i]
        pdi[i] = 100 * pdm_s[i] / tr_s[i] if tr_s[i] > 0 else 0
        mdi[i] = 100 * mdm_s[i] / tr_s[i] if tr_s[i] > 0 else 0
        d = pdi[i] + mdi[i]
        dx[i] = 100 * abs(pdi[i] - mdi[i]) / d if d > 0 else 0

    for i in range(period * 2, n):
        adx[i] = sum(dx[i - period:i]) / period
    return adx


def compute_atr(candles: List[Candle], period: int) -> List[float]:
    """ATR calculation (from PolyKronos)."""
    n = len(candles)
    atr = [0.0] * n
    for i in range(1, n):
        tr = max(candles[i].high - candles[i].low,
                 abs(candles[i].high - candles[i - 1].close),
                 abs(candles[i].low - candles[i - 1].close))
        if i < period:
            atr[i] = (atr[i - 1] * (i - 1) + tr) / i if i > 0 else tr
        else:
            atr[i] = (atr[i - 1] * (period - 1) + tr) / period
    return atr


def detect_market_structure(swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
                            idx: int) -> Tuple[str, float, float]:
    recent_h = [s for s in swing_highs if s.index <= idx]
    recent_l = [s for s in swing_lows if s.index <= idx]
    if len(recent_h) < 2 or len(recent_l) < 2:
        return "RANGE", 0.0, 0.0
    lh, ph = recent_h[-1], recent_h[-2]
    ll, pl = recent_l[-1], recent_l[-2]
    if lh.price > ph.price and ll.price > pl.price:
        return "BULLISH", lh.price, ll.price
    if lh.price < ph.price and ll.price < pl.price:
        return "BEARISH", lh.price, ll.price
    return "RANGE", lh.price, ll.price


def find_entry_zone_pro(structure: str, break_idx: int, candles: List[Candle],
                         max_lookahead: int) -> Optional[Tuple[float, float]]:
    """Improved entry zone detection with wider crypto-friendly zones."""
    end = min(break_idx + max_lookahead, len(candles))
    if end <= break_idx + 1:
        return None

    bc = candles[break_idx]
    if structure == "BULLISH":
        zone_high = min(bc.close, bc.high)
        zone_low = max(bc.open, bc.close * 0.998)  # wider zone
        for i in range(break_idx + 1, end):
            c = candles[i]
            if c.low <= zone_high and c.low >= zone_low:
                entry = (zone_high + zone_low) / 2
                return (entry, zone_low)
            if c.high > max(bc.high, bc.close * 1.01):
                break
    elif structure == "BEARISH":
        zone_low = max(bc.low, bc.close)
        zone_high = min(bc.open, bc.close * 1.002)
        for i in range(break_idx + 1, end):
            c = candles[i]
            if c.high >= zone_low and c.high <= zone_high:
                entry = (zone_high + zone_low) / 2
                return (entry, zone_high)
            if c.low < min(bc.low, bc.close * 0.99):
                break
    return None


def run_simulation(candles: List[Candle], cfg: FabianProConfig, out_dir: Path) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    portfolio_path = out_dir / "portfolio_snapshots.csv"
    summary_path = out_dir / "summary.json"

    dec_fields = ["ts", "structure", "adx", "action", "reason",
                  "open", "high", "low", "close", "entry", "sl", "tp", "rr", "balance"]
    tr_fields = ["ts", "action", "entry", "exit", "sl", "tp", "pnl", "rr", "reason"]
    pf_fields = ["ts", "balance", "position_qty", "avg_entry", "position_value", "unrealized_pnl", "realized_pnl", "total_equity", "price"]

    balance = cfg.initial_balance
    peak_balance = balance
    max_dd = 0.0
    trades_today = 0
    daily_realized_pnl = 0.0
    daily_start_balance = balance
    consecutive_losses = 0
    pause_until_idx = -1
    last_reset_day = candles[0].ts.date() if candles else datetime.now().date()
    wins = losses = total_trades = 0

    # Indicators
    adx_values = compute_adx(candles, cfg.adx_period) if cfg.use_adx_filter else [99.0] * len(candles)
    atr_values = compute_atr(candles, cfg.atr_period) if cfg.use_atr_sizing else [0] * len(candles)

    # Body averages
    body_avgs = []
    for i in range(len(candles)):
        body = abs(candles[i].close - candles[i].open)
        body_avgs.append(sum(abs(candles[j].close - candles[j].open) for j in range(max(0, i - cfg.body_avg_period), i)) / max(i, cfg.body_avg_period) if i > 0 else body)

    # Portfolio state
    position_qty = 0.0
    position_cost = 0.0
    realized_pnl = 0.0

    with (decisions_path.open("w", newline="") as dfile,
          trades_path.open("w", newline="") as tfile,
          portfolio_path.open("w", newline="") as pfile):
        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        pwr = csv.DictWriter(pfile, fieldnames=pf_fields)
        dwr.writeheader(); twr.writeheader(); pwr.writeheader()

        open_positions: List[TradePlan] = []
        filled_positions: List[dict] = []

        for i, c in enumerate(candles):
            if i < max(cfg.body_avg_period + cfg.swing_lookback * 2, cfg.adx_period * 2 + 2):
                continue

            # Daily reset
            if c.ts.date() != last_reset_day:
                last_reset_day = c.ts.date()
                trades_today = 0
                daily_start_balance = balance
                daily_realized_pnl = 0.0

            reason = ""
            action = "NO_TRADE"
            current_adx = adx_values[i] if i < len(adx_values) else 0
            current_atr = atr_values[i] if i < len(atr_values) else 0

            # Check pending order fills
            for plan in list(open_positions):
                if plan.action == "BUY_STOP" and c.high >= plan.entry:
                    filled_positions.append({"ts": c.ts.isoformat(), "action": plan.action,
                        "entry": plan.entry, "sl": plan.sl, "tp": plan.tp, "rr": plan.rr,
                        "volume": plan.volume, "risk_usd": plan.risk_usd, "reason": plan.reason})
                    open_positions.remove(plan)
                    # Update portfolio: buy tokens
                    cost = plan.entry * plan.volume
                    position_qty += plan.volume
                    position_cost += cost
                    balance -= cost
                    twr.writerow({"ts": c.ts.isoformat(), "action": "BUY",
                        "entry": round(plan.entry, 6), "exit": "",
                        "sl": round(plan.sl, 6), "tp": round(plan.tp, 6),
                        "pnl": 0.0, "rr": round(plan.rr, 2), "reason": "ENTRY_BUY"})
                elif plan.action == "SELL_STOP" and c.low <= plan.entry:
                    filled_positions.append({"ts": c.ts.isoformat(), "action": plan.action,
                        "entry": plan.entry, "sl": plan.sl, "tp": plan.tp, "rr": plan.rr,
                        "volume": plan.volume, "risk_usd": plan.risk_usd, "reason": plan.reason})
                    open_positions.remove(plan)
                    # Short: reserve risk amount as margin, don't add full notional
                    position_qty -= plan.volume  # negative = short
                    margin = min(plan.risk_usd, balance)
                    balance -= margin  # lock margin
                    twr.writerow({"ts": c.ts.isoformat(), "action": "SHORT",
                        "entry": round(plan.entry, 6), "exit": "",
                        "sl": round(plan.sl, 6), "tp": round(plan.tp, 6),
                        "pnl": 0.0, "rr": round(plan.rr, 2), "reason": "ENTRY_SHORT"})
                if plan.expiry_idx <= i:
                    open_positions.remove(plan)

            # Check filled positions (exit by SL/TP)
            for pos in list(filled_positions):
                if "exit_ts" in pos: continue
                if pos["action"] == "BUY_STOP":
                    if c.low <= pos["sl"]:
                        exit_p = pos["sl"]
                        pnl = pos["volume"] * (exit_p - pos["entry"])
                        pos["exit_ts"] = c.ts.isoformat(); pos["exit"] = exit_p; pos["pnl"] = pnl
                        balance += pos["volume"] * exit_p
                        realized_pnl += pnl; daily_realized_pnl += pnl
                        position_qty -= pos["volume"]
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "SELL",
                            "entry": round(pos["entry"], 6), "exit": round(exit_p, 6),
                            "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                            "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "SL_HIT"})
                    elif c.high >= pos["tp"]:
                        exit_p = pos["tp"]
                        pnl = pos["volume"] * (exit_p - pos["entry"])
                        pos["exit_ts"] = c.ts.isoformat(); pos["exit"] = exit_p; pos["pnl"] = pnl
                        balance += pos["volume"] * exit_p
                        realized_pnl += pnl; daily_realized_pnl += pnl
                        position_qty -= pos["volume"]
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "SELL",
                            "entry": round(pos["entry"], 6), "exit": round(exit_p, 6),
                            "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                            "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "TP_HIT"})
                else:  # SELL_STOP
                    if c.high >= pos["sl"]:
                        exit_p = pos["sl"]
                        pnl = pos["volume"] * (pos["entry"] - exit_p)
                        pos["exit_ts"] = c.ts.isoformat(); pos["exit"] = exit_p; pos["pnl"] = pnl
                        # Return margin + PnL
                        balance += pos.get("risk_usd", 0) + pnl
                        realized_pnl += pnl; daily_realized_pnl += pnl
                        position_qty += pos["volume"]
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "COVER",
                            "entry": round(pos["entry"], 6), "exit": round(exit_p, 6),
                            "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                            "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "SL_HIT"})
                    elif c.low <= pos["tp"]:
                        exit_p = pos["tp"]
                        pnl = pos["volume"] * (pos["entry"] - exit_p)
                        pos["exit_ts"] = c.ts.isoformat(); pos["exit"] = exit_p; pos["pnl"] = pnl
                        balance += pos.get("risk_usd", 0) + pnl
                        realized_pnl += pnl; daily_realized_pnl += pnl
                        position_qty += pos["volume"]
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "COVER",
                            "entry": round(pos["entry"], 6), "exit": round(exit_p, 6),
                            "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                            "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "TP_HIT"})

            # Hard gates
            if trades_today >= cfg.max_trades_per_day:
                reason = "MAX_TRADES_DAY"
            elif daily_realized_pnl < 0 and abs(daily_realized_pnl) / daily_start_balance * 100 >= cfg.max_daily_loss_pct:
                reason = "DAILY_LOSS_LIMIT"
            elif pause_until_idx > i:
                reason = "STREAK_PAUSE"
            elif cfg.use_adx_filter and current_adx < cfg.adx_min:
                reason = f"ADX_TOO_LOW({current_adx:.1f})"

            if not reason:
                sh = find_swing_highs([c.high for c in candles[:i+1]], cfg.swing_lookback, cfg.structure_bars)
                sl = find_swing_lows([c.low for c in candles[:i+1]], cfg.swing_lookback, cfg.structure_bars)
                structure, lst_h, lst_l = detect_market_structure(sh, sl, i)

                if structure == "RANGE":
                    reason = "RANGE_STRUCTURE"
                else:
                    body_avg = body_avgs[i]
                    bc = candles[i]
                    body = abs(bc.close - bc.open)
                    wick_ratio = (bc.high - bc.low) / body if body > 0 else 99

                    is_break = False
                    broken_level = 0.0
                    if structure == "BULLISH" and bc.high > lst_h and bc.close > lst_h and body >= body_avg * cfg.force_body_multiplier and wick_ratio <= cfg.max_wick_to_body_ratio:
                        is_break = True
                        broken_level = lst_h
                    elif structure == "BEARISH" and bc.low < lst_l and bc.close < lst_l and body >= body_avg * cfg.force_body_multiplier and wick_ratio <= cfg.max_wick_to_body_ratio:
                        is_break = True
                        broken_level = lst_l

                    if not is_break:
                        reason = "NO_BREAKOUT"
                    else:
                        zone = find_entry_zone_pro(structure, i, candles, cfg.max_break_pullback_candles)
                        if zone is None:
                            reason = "NO_ZONE"
                        else:
                            entry, zone_edge = zone
                            sl_distance = abs(entry - broken_level)

                            # Adaptive SL/TP using ATR
                            if cfg.use_atr_sizing and current_atr > 0:
                                sl_distance = max(sl_distance, current_atr * cfg.atr_multiplier_sl)
                                tp_distance = sl_distance * cfg.min_rr
                            else:
                                tp_distance = sl_distance * cfg.min_rr

                            if structure == "BULLISH":
                                sl = entry - sl_distance
                                tp = entry + tp_distance
                                action_plan = "BUY_STOP"
                            else:
                                sl = entry + sl_distance
                                tp = entry - tp_distance
                                action_plan = "SELL_STOP"

                            risk_usd = balance * (cfg.risk_percent / 100.0)
                            volume = risk_usd / sl_distance if sl_distance > 0 else 0
                            rr = tp_distance / sl_distance if sl_distance > 0 else 0
                            
                            # Max position = 3x balance (3:1 leverage for crypto) 
                            max_volume = (balance * 3.0) / entry if entry > 0 else 0
                            volume = min(volume, max_volume)
                            risk_usd_actual = volume * sl_distance

                            cost_buy = volume * entry if action_plan == "BUY_STOP" else 0
                            if rr >= cfg.min_rr and volume > 0 and cost_buy <= balance and risk_usd_actual <= balance:
                                plan = TradePlan(action=action_plan, entry=entry, sl=sl, tp=tp,
                                    volume=volume, risk_usd=risk_usd, reward_usd=risk_usd * rr,
                                    rr=rr, session="CRYPTO", reason=f"{structure}_BREAK",
                                    expiry_idx=i + cfg.pending_order_expiry_minutes)
                                open_positions.append(plan)
                                trades_today += 1
                                action = f"PLACE_{action_plan}"
                                reason = f"TRADE_PLACED_RR{rr:.1f}"

                                if consecutive_losses >= 3:
                                    pause_until_idx = i + 24
                                    consecutive_losses = 0

                                dwr.writerow({"ts": c.ts.isoformat(), "structure": structure,
                                    "adx": round(current_adx, 1), "action": action, "reason": reason,
                                    "open": round(c.open, 6), "high": round(c.high, 6),
                                    "low": round(c.low, 6), "close": round(c.close, 6),
                                    "entry": round(entry, 6), "sl": round(sl, 6),
                                    "tp": round(tp, 6), "rr": round(rr, 2),
                                    "balance": round(balance, 2)})
                                continue
                            else:
                                reason = f"INVALID_PLAN(rr={rr:.1f})"

            # Decision log
            dwr.writerow({"ts": c.ts.isoformat(), "structure": structure if 'structure' in dir() else "?",
                "adx": round(current_adx, 1), "action": action, "reason": reason,
                "open": round(c.open, 6), "high": round(c.high, 6), "low": round(c.low, 6),
                "close": round(c.close, 6), "entry": 0, "sl": 0, "tp": 0, "rr": 0,
                "balance": round(balance, 2)})

            peak_balance = max(peak_balance, balance)
            dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
            max_dd = max(max_dd, dd)

            # Portfolio snapshot every 5 candles
            if i % 5 == 0:
                pos_val = position_qty * c.close
                upnl = realized_pnl  # simplified
                pwr.writerow({"ts": c.ts.isoformat(), "balance": round(balance, 2),
                    "position_qty": round(position_qty, 6),
                    "avg_entry": round(position_cost / position_qty if position_qty > 0 else 0, 6),
                    "position_value": round(pos_val, 2),
                    "unrealized_pnl": round(pos_val - position_cost, 4),
                    "realized_pnl": round(realized_pnl, 4),
                    "total_equity": round(balance + pos_val, 2),
                    "price": round(c.close, 2)})

    total_pnl = balance - cfg.initial_balance
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    summary = {"initial_balance": cfg.initial_balance, "final_balance": round(balance, 2),
        "total_pnl": round(total_pnl, 2), "total_trades": total_trades,
        "wins": wins, "losses": losses, "win_rate_percent": round(win_rate, 2),
        "max_drawdown_percent": round(max_dd, 2),
        "final_position_qty": round(position_qty, 6),
        "position_value": round(position_qty * candles[-1].close if candles else 0, 2),
        "config": asdict(cfg),
        "outputs": {"decisions_log": str(decisions_path), "trades_log": str(trades_path),
            "portfolio_snapshots": str(portfolio_path), "summary_json": str(summary_path)}}
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def load_candles(path: Path) -> List[Candle]:
    candles = []
    with path.open() as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row.get("timestamp_utc","").replace("Z","+00:00"))
            candles.append(Candle(ts=ts, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume", row.get("volumefrom", 0)))))
    return candles


def load_config(path: Optional[Path]) -> FabianProConfig:
    if path is None: return FabianProConfig()
    data = json.loads(path.read_text())
    c = FabianProConfig()
    for k, v in data.items():
        if hasattr(c, k): setattr(c, k, v)
    return c


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--config")
    p.add_argument("--output-dir", default="fabian_pro_runs/latest")
    args = p.parse_args()
    cfg = load_config(Path(args.config) if args.config else None)
    candles = load_candles(Path(args.input))
    if len(candles) < 100:
        raise ValueError("Need at least 100 candles")
    print(json.dumps(run_simulation(candles, cfg, Path(args.output_dir)), indent=2))


if __name__ == "__main__":
    main()
