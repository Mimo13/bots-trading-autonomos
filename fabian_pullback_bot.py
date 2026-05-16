#!/usr/bin/env python3
"""
FabiánPullback — Python port of the cTrader structure/breakout/pullback bot.
Simulates on OHLCV data from Binance.

Strategy:
1. Detect market structure (bullish/bearish via swing highs/lows)
2. Find strong breakouts beyond recent swing points
3. On breakout pullback → enter (limit order at zone)
4. SL beyond the swing point, TP at next structure level (min RR enforced)
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
    kind: str  # "high" or "low"


@dataclass
class FabianConfig:
    # Risk
    risk_percent: float = 1.0
    reduced_risk_percent: float = 0.5
    max_daily_loss_pct: float = 3.0
    max_trades_per_session: int = 1
    max_trades_per_day: int = 2
    min_rr: float = 1.5
    sl_buffer_pips: float = 0.5

    # Behavior
    enable_trailing: bool = True
    enable_break_even_at_1r: bool = True
    spot_long_only: bool = False  # Paper/live spot mode: disable SELL_STOP/SHORT plans
    min_position_usd: float = 20.0  # minimum position size to avoid micro-trades from leftovers

    # Structure
    swing_lookback: int = 3
    structure_bars: int = 100
    body_avg_period: int = 20
    force_body_multiplier: float = 1.5
    max_wick_to_body_ratio: float = 1.0
    pending_order_expiry_minutes: int = 120
    entry_mode: int = 0  # 0=midpoint, 1=conservative_edge
    
    # Session (UTC) — ignored when crypto_mode=True
    crypto_mode: bool = True
    london_start: Tuple[int, int] = (7, 0)
    london_end: Tuple[int, int] = (12, 0)
    ny_start: Tuple[int, int] = (13, 30)
    ny_end: Tuple[int, int] = (20, 0)
    avoid_first_session_minutes: int = 15

    # ATR-based risk floor — ensures meaningful PnL per trade
    atr_period: int = 14
    atr_sl_min_mult: float = 1.0      # SL distance >= ATR * this (0=disabled)
    min_sl_distance_pct: float = 0.0  # absolute minimum SL distance as % of price

    # General
    max_spread_pips: float = 2.0
    initial_balance: float = 100.0


@dataclass
class TradePlan:
    action: str  # "BUY_STOP" or "SELL_STOP"
    entry: float
    sl: float
    tp: float
    risk_pips: float
    reward_pips: float
    rr: float
    volume: float
    session: str
    reason: str
    expiry_idx: int


def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.01
    tr = []
    for i in range(1, len(closes)):
        h_l = highs[i] - lows[i]
        h_pc = abs(highs[i] - closes[i - 1])
        l_pc = abs(lows[i] - closes[i - 1])
        tr.append(max(h_l, h_pc, l_pc))
    return sum(tr[-period:]) / period


def find_swing_highs(closes: List[float], highs: List[float], lows: List[float],
                      lookback: int, max_bars: int) -> List[SwingPoint]:
    """Find swing highs: bar that is higher than `lookback` bars on each side."""
    result = []
    n = len(highs)
    end = n - lookback - 1
    start = max(lookback, end - max_bars)
    for i in range(start, end + 1):
        is_swing = True
        h = highs[i]
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n:
                is_swing = False
                break
            if h <= highs[i - j] or h <= highs[i + j]:
                is_swing = False
                break
        if is_swing:
            result.append(SwingPoint(i, h, "high"))
    return result


def find_swing_lows(closes: List[float], highs: List[float], lows: List[float],
                     lookback: int, max_bars: int) -> List[SwingPoint]:
    """Find swing lows: bar that is lower than `lookback` bars on each side."""
    result = []
    n = len(lows)
    end = n - lookback - 1
    start = max(lookback, end - max_bars)
    for i in range(start, end + 1):
        is_swing = True
        l = lows[i]
        for j in range(1, lookback + 1):
            if i - j < 0 or i + j >= n:
                is_swing = False
                break
            if l >= lows[i - j] or l >= lows[i + j]:
                is_swing = False
                break
        if is_swing:
            result.append(SwingPoint(i, l, "low"))
    return result


def detect_market_structure(swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
                            current_idx: int) -> Tuple[str, float, float]:
    """
    Determine market structure: BULLISH, BEARISH, or RANGE.
    Returns (structure, last_structural_high, last_structural_low).
    """
    recent_highs = [s for s in swing_highs if s.index <= current_idx]
    recent_lows = [s for s in swing_lows if s.index <= current_idx]
    
    if len(recent_highs) < 2 or len(recent_lows) < 2:
        return "RANGE", 0.0, 0.0
    
    last_h = recent_highs[-1]
    prev_h = recent_highs[-2]
    last_l = recent_lows[-1]
    prev_l = recent_lows[-2]
    
    # Bullish: higher highs + higher lows
    if last_h.price > prev_h.price and last_l.price > prev_l.price:
        return "BULLISH", last_h.price, last_l.price
    
    # Bearish: lower highs + lower lows
    if last_h.price < prev_h.price and last_l.price < prev_l.price:
        return "BEARISH", last_h.price, last_l.price
    
    return "RANGE", last_h.price, last_l.price


def detect_strong_breakout(structure: str, structural_high: float, structural_low: float,
                           candles: List[Candle], idx: int, body_avg: float,
                           force_body_mult: float, max_wick_body: float) -> Tuple[bool, float, int]:
    """
    Detect a strong breakout bar beyond the structural level.
    Returns (is_breakout, broken_level, breakout_bar_index).
    """
    if idx < 1 or idx >= len(candles):
        return False, 0.0, idx
    
    c = candles[idx]
    prev = candles[idx - 1]
    body = abs(c.close - c.open)
    wick_to_body = (c.high - c.low) / body if body > 0 else 99
    
    # Need strong bar (force body) with controlled wick
    if body < body_avg * force_body_mult:
        return False, 0.0, idx
    if wick_to_body > max_wick_body:
        return False, 0.0, idx
    
    if structure == "BULLISH":
        # Break above structural high
        if c.high > structural_high and c.close > structural_high:
            return True, structural_high, idx
    elif structure == "BEARISH":
        # Break below structural low
        if c.low < structural_low and c.close < structural_low:
            return True, structural_low, idx
    
    return False, 0.0, idx


def find_entry_zone(structure: str, breakout_idx: int, candles: List[Candle]) -> Optional[Tuple[float, float]]:
    """
    After breakout, find the pullback zone.
    Returns (zone_high, zone_low) or None.
    """
    lookahead = min(breakout_idx + 10, len(candles))
    if lookahead <= breakout_idx + 1:
        return None
    
    if structure == "BULLISH":
        # After bullish breakout, look for pullback to previous resistance-turned-support
        # Zone = from breakout bar's close to the structural level
        breakout_close = candles[breakout_idx].close
        # Find the highest point of the breakout bar
        zone_high = min(breakout_close, candles[breakout_idx].high)
        zone_low = max(candles[breakout_idx].open, candles[breakout_idx - 1].close) if breakout_idx > 0 else candles[breakout_idx].low
        
        # Look for a pullback candle that enters this zone
        for i in range(breakout_idx + 1, lookahead):
            c = candles[i]
            if c.low <= zone_high and c.low >= zone_low:
                entry = zone_high - (zone_high - zone_low) * 0.5  # Midpoint
                return (entry, zone_low)
            if c.high > max(candles[breakout_idx].high, breakout_close * 1.01):
                break  # Too far from breakout, stop looking
    
    elif structure == "BEARISH":
        breakout_low = candles[breakout_idx].low
        zone_low = max(breakout_low, candles[breakout_idx].low)
        zone_high = min(candles[breakout_idx].open, candles[breakout_idx - 1].close) if breakout_idx > 0 else candles[breakout_idx].high
        
        for i in range(breakout_idx + 1, lookahead):
            c = candles[i]
            if c.high >= zone_low and c.high <= zone_high:
                entry = zone_low + (zone_high - zone_low) * 0.5
                return (entry, zone_high)
            if c.low < min(candles[breakout_idx].low, breakout_low * 0.99):
                break
    
    return None


def build_trade_plan(structure: str, entry_zone: Tuple[float, float],
                     structural_level: float, body_avg: float, cfg: FabianConfig,
                     candle: Candle, session: str, expiry_idx: int,
                     current_balance: Optional[float] = None,
                     atr: float = 0.0) -> Optional[TradePlan]:
    """Build trade plan with SL, TP, and position size."""
    entry, zone_edge = entry_zone
    buffer = cfg.sl_buffer_pips * 0.0001 if candle.close > 10 else cfg.sl_buffer_pips * 0.001

    # Use the pullback zone edge as the protective stop anchor. The previous
    # implementation used the broken structure level, which can be on the wrong
    # side of entry and made the paper simulation count impossible SL hits as wins.
    if structure == "BULLISH":
        sl = zone_edge - buffer
        risk_pips = entry - sl
        if risk_pips <= 0:
            return None
        # Enforce minimum risk distance (ATR-based or % of price)
        min_risk = 0.0
        if atr > 0 and cfg.atr_sl_min_mult > 0:
            min_risk = max(min_risk, atr * cfg.atr_sl_min_mult)
        if cfg.min_sl_distance_pct > 0:
            min_risk = max(min_risk, entry * cfg.min_sl_distance_pct / 100.0)
        if risk_pips < min_risk:
            risk_pips = min_risk
            sl = entry - risk_pips  # widen SL to meet minimum
        tp = entry + risk_pips * cfg.min_rr
        reward_pips = tp - entry
        action = "BUY_STOP"
    else:
        sl = zone_edge + buffer
        risk_pips = sl - entry
        if risk_pips <= 0:
            return None
        # Enforce minimum risk distance (ATR-based or % of price)
        min_risk = 0.0
        if atr > 0 and cfg.atr_sl_min_mult > 0:
            min_risk = max(min_risk, atr * cfg.atr_sl_min_mult)
        if cfg.min_sl_distance_pct > 0:
            min_risk = max(min_risk, entry * cfg.min_sl_distance_pct / 100.0)
        if risk_pips < min_risk:
            risk_pips = min_risk
            sl = entry + risk_pips  # widen SL to meet minimum
        tp = entry - risk_pips * cfg.min_rr
        reward_pips = entry - tp
        action = "SELL_STOP"

    rr = reward_pips / risk_pips if risk_pips > 0 else 0

    if rr < cfg.min_rr:
        return None

    # Position sizing: risk X% of balance = position * |entry - SL|
    # So raw position (tokens) = (balance * risk%) / |entry - SL|
    # Then capped by available cash for spot (no leverage)
    live_balance = current_balance if current_balance is not None else cfg.initial_balance
    risk_amount = live_balance * (cfg.risk_percent / 100.0)
    raw_volume = risk_amount / risk_pips if risk_pips > 0 else 0
    
    # Spot cap: max tokens we can buy with available cash (95% to leave fee buffer)
    max_notional = live_balance * 0.98
    max_volume = max_notional / entry if entry > 0 else raw_volume
    volume = min(raw_volume, max_volume)
    
    return TradePlan(
        action=action, entry=entry, sl=sl, tp=tp,
        risk_pips=risk_pips, reward_pips=reward_pips, rr=rr,
        volume=volume, session=session, reason=f"{structure}_BREAK_PULLBACK",
        expiry_idx=expiry_idx
    )


def get_session(ts: datetime, cfg: FabianConfig) -> str:
    """Determine trading session: LONDON, NY, NONE, or CRYPTO."""
    if cfg.crypto_mode:
        return "CRYPTO"
    h, m = ts.hour, ts.minute
    mins = h * 60 + m
    l_start = cfg.london_start[0] * 60 + cfg.london_start[1]
    l_end = cfg.london_end[0] * 60 + cfg.london_end[1]
    ny_start = cfg.ny_start[0] * 60 + cfg.ny_start[1]
    ny_end = cfg.ny_end[0] * 60 + cfg.ny_end[1]
    if l_start <= mins < l_end:
        return "LONDON"
    if ny_start <= mins < ny_end:
        return "NY"
    return "NONE"


def is_session_open_lock(mins: int, session: str, cfg: FabianConfig) -> bool:
    """Check if within session opening lock period."""
    avoid = cfg.avoid_first_session_minutes
    if session == "LONDON":
        session_start = cfg.london_start[0] * 60 + cfg.london_start[1]
        return session_start <= mins < session_start + avoid
    if session == "NY":
        session_start = cfg.ny_start[0] * 60 + cfg.ny_start[1]
        return session_start <= mins < session_start + avoid
    return False


def run_simulation(candles: List[Candle], cfg: FabianConfig, out_dir: Path) -> Dict:
    """Run the FabianPullback strategy simulation."""
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    summary_path = out_dir / "summary.json"
    
    dec_fields = ["ts", "session", "structure", "action", "reason",
                  "open", "high", "low", "close", "entry", "sl", "tp", "rr", "balance"]
    tr_fields = ["ts", "action", "entry", "exit", "sl", "tp", "pnl", "rr", "reason", "qty"]
    
    balance = cfg.initial_balance
    peak_balance = balance
    max_dd = 0.0
    trades_today = 0
    trades_london = 0
    trades_ny = 0
    daily_start_balance = balance
    daily_realized_pnl = 0.0
    consecutive_losses = 0
    pause_until_idx = -1
    reduced_risk = False
    last_reset_day = candles[0].ts.date() if candles else datetime.now().date()
    
    wins = 0
    losses = 0
    total_trades = 0
    
    # Compute body average
    body_avgs = []
    for i in range(len(candles)):
        body = abs(candles[i].close - candles[i].open)
        if i >= cfg.body_avg_period:
            avg = sum(abs(candles[j].close - candles[j].open) for j in range(i - cfg.body_avg_period, i)) / cfg.body_avg_period
        else:
            avg = body
        body_avgs.append(avg)
    
    with (decisions_path.open("w", newline="", encoding="utf-8") as dfile,
          trades_path.open("w", newline="", encoding="utf-8") as tfile):
        
        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        dwr.writeheader()
        twr.writeheader()
        
        open_positions: List[TradePlan] = []  # active trades
        filled_positions: List[dict] = []  # filled trades with entry info
        
        for i, c in enumerate(candles):
            if i < cfg.body_avg_period + cfg.swing_lookback * 2:
                continue  # Warmup
            
            # Daily reset
            if c.ts.date() != last_reset_day:
                last_reset_day = c.ts.date()
                trades_today = 0
                trades_london = 0
                trades_ny = 0
                daily_start_balance = balance
                daily_realized_pnl = 0.0
            
            session = get_session(c.ts, cfg)
            mins = c.ts.hour * 60 + c.ts.minute
            
            reason = ""
            action = "NO_TRADE"
            
            # Trade management — check if pending orders should fill
            for plan in list(open_positions):
                if plan.action == "BUY_STOP":
                    if c.high >= plan.entry:
                        # Fill at entry
                        cost = plan.volume * plan.entry
                        balance -= cost
                        filled_positions.append({
                            "ts": c.ts.isoformat(), "action": plan.action,
                            "entry": plan.entry, "sl": plan.sl, "tp": plan.tp,
                            "rr": plan.rr, "volume": plan.volume,
                            "session": plan.session, "reason": plan.reason,
                            "fill_idx": i, "fill_price": plan.entry
                        })
                        open_positions.remove(plan)
                        twr.writerow({"ts": c.ts.isoformat(), "action": "BUY",
                                      "entry": round(plan.entry, 6), "exit": "",
                                      "sl": round(plan.sl, 6), "tp": round(plan.tp, 6),
                                      "pnl": 0.0, "rr": round(plan.rr, 2), "reason": "ENTRY_BUY",
                                      "qty": round(plan.volume, 6)})
                elif plan.action == "SELL_STOP":
                    if c.low <= plan.entry:
                        filled_positions.append({
                            "ts": c.ts.isoformat(), "action": plan.action,
                            "entry": plan.entry, "sl": plan.sl, "tp": plan.tp,
                            "rr": plan.rr, "volume": plan.volume,
                            "session": plan.session, "reason": plan.reason,
                            "fill_idx": i, "fill_price": plan.entry
                        })
                        open_positions.remove(plan)
                        twr.writerow({"ts": c.ts.isoformat(), "action": "SHORT",
                                      "entry": round(plan.entry, 6), "exit": "",
                                      "sl": round(plan.sl, 6), "tp": round(plan.tp, 6),
                                      "pnl": 0.0, "rr": round(plan.rr, 2), "reason": "ENTRY_SHORT",
                                      "qty": round(plan.volume, 6)})
                
                # Expiry
                if plan.expiry_idx <= i:
                    open_positions.remove(plan)
            
            # Check filled positions for SL/TP hits
            for pos in list(filled_positions):
                if "exit_ts" in pos:
                    continue
                
                if pos["action"] == "BUY_STOP":
                    vol = float(pos.get("volume", 0))
                    if c.low <= pos["sl"]:
                        pos["exit_ts"] = c.ts.isoformat()
                        pos["exit"] = pos["sl"]
                        pnl = vol * (pos["sl"] - pos["entry"])
                        pos["pnl"] = pnl
                        # Return cost basis + PnL (often a loss)
                        balance += pnl + vol * pos["entry"]
                        daily_realized_pnl += pnl
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "SELL",
                                      "entry": round(pos["entry"], 6), "exit": round(pos["sl"], 6),
                                      "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                                      "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "SL_HIT",
                                      "qty": round(vol, 6)})
                    elif c.high >= pos["tp"]:
                        pos["exit_ts"] = c.ts.isoformat()
                        pos["exit"] = pos["tp"]
                        pnl = vol * (pos["tp"] - pos["entry"])
                        pos["pnl"] = pnl
                        # Return cost basis + PnL
                        balance += pnl + vol * pos["entry"]
                        daily_realized_pnl += pnl
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "SELL",
                                      "entry": round(pos["entry"], 6), "exit": round(pos["tp"], 6),
                                      "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                                      "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "TP_HIT",
                                      "qty": round(vol, 6)})
                        
                elif pos["action"] == "SELL_STOP":
                    vol = float(pos.get("volume", 0))
                    if c.high >= pos["sl"]:
                        pos["exit_ts"] = c.ts.isoformat()
                        pos["exit"] = pos["sl"]
                        pnl = vol * (pos["entry"] - pos["sl"])
                        pos["pnl"] = pnl
                        balance += pnl
                        daily_realized_pnl += pnl
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "COVER",
                                      "entry": round(pos["entry"], 6), "exit": round(pos["sl"], 6),
                                      "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                                      "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "SL_HIT",
                                      "qty": round(vol, 6)})
                    elif c.low <= pos["tp"]:
                        pos["exit_ts"] = c.ts.isoformat()
                        pos["exit"] = pos["tp"]
                        pnl = vol * (pos["entry"] - pos["tp"])
                        pos["pnl"] = pnl
                        balance += pnl
                        daily_realized_pnl += pnl
                        if pnl > 0: wins += 1; consecutive_losses = 0
                        else: losses += 1; consecutive_losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "action": "COVER",
                                      "entry": round(pos["entry"], 6), "exit": round(pos["tp"], 6),
                                      "sl": round(pos["sl"], 6), "tp": round(pos["tp"], 6),
                                      "pnl": round(pnl, 4), "rr": round(pos["rr"], 2), "reason": "TP_HIT",
                                      "qty": round(vol, 6)})
            
            # Hard gates — can we trade?
            if session == "NONE":
                reason = "OUT_OF_SESSION"
            elif is_session_open_lock(mins, session, cfg):
                reason = "SESSION_OPEN_LOCK"
            elif trades_today >= cfg.max_trades_per_day:
                reason = "MAX_TRADES_DAY"
            elif session == "LONDON" and trades_london >= cfg.max_trades_per_session:
                reason = "MAX_TRADES_SESSION"
            elif session == "NY" and trades_ny >= cfg.max_trades_per_session:
                reason = "MAX_TRADES_SESSION"
            elif daily_realized_pnl < 0 and abs(daily_realized_pnl / daily_start_balance) * 100 >= cfg.max_daily_loss_pct:
                reason = "DAILY_LOSS_LIMIT"
            elif pause_until_idx > i:
                reason = "STREAK_PAUSE"
            
            if not reason:
                # Detect structure
                swing_highs = find_swing_highs(
                    [c.close for c in candles[:i+1]],
                    [c.high for c in candles[:i+1]],
                    [c.low for c in candles[:i+1]],
                    cfg.swing_lookback, cfg.structure_bars
                )
                swing_lows = find_swing_lows(
                    [c.close for c in candles[:i+1]],
                    [c.high for c in candles[:i+1]],
                    [c.low for c in candles[:i+1]],
                    cfg.swing_lookback, cfg.structure_bars
                )
                
                structure, last_h, last_l = detect_market_structure(swing_highs, swing_lows, i)
                
                if structure == "RANGE":
                    reason = "RANGE_STRUCTURE"
                elif cfg.spot_long_only and structure == "BEARISH":
                    reason = "SPOT_LONG_ONLY_SKIP_BEARISH"
                else:
                    # Check breakout
                    body_avg = body_avgs[i]
                    is_break, broken_level, break_idx = detect_strong_breakout(
                        structure, last_h, last_l, candles, i, body_avg,
                        cfg.force_body_multiplier, cfg.max_wick_to_body_ratio
                    )
                    
                    if not is_break:
                        reason = "NO_BREAKOUT"
                    else:
                        # Find entry zone
                        zone = find_entry_zone(structure, break_idx, candles)
                        if zone is None:
                            reason = "NO_ZONE"
                        else:
                            # Build trade plan
                            # Compute ATR for min SL floor
                            atr_val = compute_atr(
                                [c.high for c in candles[:i+1]],
                                [c.low for c in candles[:i+1]],
                                [c.close for c in candles[:i+1]],
                                cfg.atr_period
                            ) if cfg.atr_period > 0 else 0.0
                            plan = build_trade_plan(
                                structure, zone, broken_level, body_avg, cfg, c, session,
                                i + cfg.pending_order_expiry_minutes,
                                current_balance=balance,
                                atr=atr_val
                            )
                            if plan is None:
                                reason = "INVALID_PLAN"
                            elif plan.volume * plan.entry < cfg.min_position_usd:
                                reason = "POSITION_TOO_SMALL"
                            else:
                                action = f"PLACE_{plan.action}"
                                open_positions.append(plan)
                                if session == "LONDON":
                                    trades_london += 1
                                else:
                                    trades_ny += 1
                                trades_today += 1
                                reason = f"TRADE_PLACED_{plan.action}_RR{plan.rr:.1f}"
                                
                                if consecutive_losses >= 3:
                                    pause_until_idx = i + 24  # pause ~2h (24 candles at 5m)
                                    consecutive_losses = 0
                                
                                dwr.writerow({"ts": c.ts.isoformat(), "session": session,
                                              "structure": structure, "action": action,
                                              "reason": reason, "open": round(c.open, 6),
                                              "high": round(c.high, 6), "low": round(c.low, 6),
                                              "close": round(c.close, 6),
                                              "entry": round(plan.entry, 6), "sl": round(plan.sl, 6),
                                              "tp": round(plan.tp, 6), "rr": round(plan.rr, 2),
                                              "balance": round(balance, 2)})
                                continue
            
            # Decision log for non-trade bars
            dwr.writerow({"ts": c.ts.isoformat(), "session": session,
                          "structure": structure if 'structure' in dir() else "?",
                          "action": action, "reason": reason,
                          "open": round(c.open, 6), "high": round(c.high, 6),
                          "low": round(c.low, 6), "close": round(c.close, 6),
                          "entry": 0, "sl": 0, "tp": 0, "rr": 0,
                          "balance": round(balance, 2)})
            
            # Track drawdown using equity (balance + open position value)
            open_value = sum(p["volume"] * c.close for p in filled_positions if "exit_ts" not in p)
            equity = balance + open_value
            peak_balance = max(peak_balance, equity)
            dd = (peak_balance - equity) / peak_balance * 100 if peak_balance > 0 else 0
            max_dd = max(max_dd, dd)
    
    # Summary
    total_pnl = balance - cfg.initial_balance
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    summary = {
        "initial_balance": cfg.initial_balance,
        "final_balance": round(balance, 2),
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_percent": round(win_rate, 2),
        "max_drawdown_percent": round(max_dd, 2),
        "config": asdict(cfg),
        "outputs": {
            "decisions_log": str(decisions_path),
            "trades_log": str(trades_path),
            "summary_json": str(summary_path),
        }
    }
    
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def load_candles_from_csv(path: Path) -> List[Candle]:
    """Load OHLCV data from CSV (Binance format or enriched format)."""
    candles = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row.get("timestamp_utc", "").replace("Z", "+00:00")
            ts = datetime.fromisoformat(ts_str)
            candles.append(Candle(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", row.get("volumefrom", 0))),
            ))
    return candles


def load_config(path: Optional[Path]) -> FabianConfig:
    if path is None:
        return FabianConfig()
    data = json.loads(path.read_text())
    c = FabianConfig()
    for k, v in data.items():
        if hasattr(c, k):
            setattr(c, k, v)
    for key in ("london_start", "london_end", "ny_start", "ny_end"):
        if key in data:
            val = data[key]
            if isinstance(val, list) and len(val) == 2:
                setattr(c, key, (val[0], val[1]))
    return c


def main():
    p = argparse.ArgumentParser(description="FabiánPullback Python simulation")
    p.add_argument("--input", required=True)
    p.add_argument("--config")
    p.add_argument("--output-dir", default="fabian_runs/latest")
    args = p.parse_args()
    
    cfg = load_config(Path(args.config) if args.config else None)
    candles = load_candles_from_csv(Path(args.input))
    if len(candles) < 100:
        raise ValueError("Need at least 100 candles")
    
    summary = run_simulation(candles, cfg, Path(args.output_dir))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
