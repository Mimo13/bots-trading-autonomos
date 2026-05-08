#!/usr/bin/env python3
"""
TurtleBot — Estrategia Turtle de Richard Dennis.
- Donchian Channel breakout (N días)
- ATR position sizing
- Piramidación (añadir a ganadores)
- Trailing stop basado en ATR
- Múltiples activos
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, asdict
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
    symbol: str = ""


@dataclass
class TurtleConfig:
    initial_balance: float = 100.0
    risk_per_trade: float = 0.01       # 1% risk per unit
    donchian_period: int = 20           # N-day breakout
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0    # Exit at 2x ATR from entry/highest
    pyramid_levels: int = 3             # Max pyramid levels
    pyramid_increment_pct: float = 0.5  # Add at 0.5 ATR increments
    pyramid_unit_size_pct: float = 0.5  # Each add is 0.5x initial unit
    max_risk_per_position: float = 0.03 # Max 3% risk per position
    max_positions: int = 3
    max_daily_loss_pct: float = 10.0
    max_trades_per_day: int = 10
    enable_pyramiding: bool = True
    enable_trailing: bool = True
    trailing_start_atr: float = 1.0     # Start trailing after 1x ATR profit
    exit_on_signal_reversal: bool = False


@dataclass
class Position:
    symbol: str
    side: str  # LONG or SHORT
    entry_price: float
    entry_idx: int
    qty: float
    atr_at_entry: float
    highest_price: float  # for trailing
    lowest_price: float
    stop_price: float
    pyramid_level: int = 0
    pnl: float = 0.0


def compute_atr(candles: List[Candle], period: int) -> List[float]:
    n = len(candles)
    atr = [0.0] * n
    for i in range(1, n):
        tr = max(candles[i].high - candles[i].low,
                 abs(candles[i].high - candles[i - 1].close),
                 abs(candles[i].low - candles[i - 1].close))
        atr[i] = (atr[i - 1] * (period - 1) + tr) / period if i >= period else (atr[i - 1] * (i - 1) + tr) / i if i > 0 else tr
    return atr


def donchian_channel(candles: List[Candle], period: int, idx: int) -> Tuple[float, float, float]:
    """Return (upper, lower, middle) of Donchian channel."""
    start = max(0, idx - period + 1)
    highs = [c.high for c in candles[start:idx + 1]]
    lows = [c.low for c in candles[start:idx + 1]]
    upper = max(highs) if highs else candles[idx].close
    lower = min(lows) if lows else candles[idx].close
    middle = (upper + lower) / 2
    return upper, lower, middle


def run_turtle(candles_by_symbol: Dict[str, List[Candle]], cfg: TurtleConfig, out_dir: Path) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    portfolio_path = out_dir / "portfolio_snapshots.csv"
    summary_path = out_dir / "summary.json"

    dec_fields = ["ts", "symbol", "action", "reason", "price", "entry", "stop", "atr", "qty", "balance"]
    tr_fields = ["ts", "symbol", "side", "entry", "exit", "qty", "pnl", "reason"]
    pf_fields = ["ts", "balance", "positions", "total_equity"]

    balance = cfg.initial_balance
    peak_balance = balance
    max_dd = 0.0
    wins = losses = total_trades = 0
    realized_pnl = 0.0
    trades_today = 0
    daily_start_balance = balance
    daily_realized_pnl = 0.0
    positions: List[Position] = []
    last_day = list(candles_by_symbol.values())[0][0].ts.date() if candles_by_symbol else datetime.now().date()

    # Precompute indicators per symbol
    atr_map = {}
    for sym, candles in candles_by_symbol.items():
        atr_map[sym] = compute_atr(candles, cfg.atr_period)

    # Find max length
    max_len = max(len(c) for c in candles_by_symbol.values())

    with (decisions_path.open("w", newline="") as dfile,
          trades_path.open("w", newline="") as tfile,
          portfolio_path.open("w", newline="") as pfile):
        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        pwr = csv.DictWriter(pfile, fieldnames=pf_fields)
        dwr.writeheader(); twr.writeheader(); pwr.writeheader()

        def equity():
            pos_val = sum(p.qty * sym_candles[i].close for p, sym_candles in 
                         [(p, candles_by_symbol[p.symbol]) for p in positions]
                         if i < len(sym_candles))
            return balance + pos_val

        for i in range(cfg.donchian_period + cfg.atr_period, max_len):
            # Daily reset
            any_candle = None
            for sym, candles in candles_by_symbol.items():
                if i < len(candles):
                    any_candle = candles[i]
                    break
            if not any_candle:
                continue

            if any_candle.ts.date() != last_day:
                last_day = any_candle.ts.date()
                trades_today = 0
                daily_start_balance = balance
                daily_realized_pnl = 0.0

            # Check daily loss limit
            daily_loss_pct = 0
            if daily_start_balance > 0:
                daily_loss_pct = max(0, -daily_realized_pnl / daily_start_balance * 100)
            if daily_loss_pct >= cfg.max_daily_loss_pct:
                for sym, candles in candles_by_symbol.items():
                    if i < len(candles):
                        dwr.writerow({"ts": candles[i].ts.isoformat(), "symbol": sym,
                            "action": "NO_TRADE", "reason": "DAILY_LOSS_LIMIT",
                            "price": round(candles[i].close, 4), "entry": 0, "stop": 0,
                            "atr": 0, "qty": 0, "balance": round(balance, 2)})
                continue

            # Check exits for existing positions
            for pos in list(positions):
                sym_candles = candles_by_symbol[pos.symbol]
                if i >= len(sym_candles):
                    continue
                c = sym_candles[i]
                atr = atr_map[pos.symbol][i]

                if pos.side == "LONG":
                    # Update highest price for trailing
                    pos.highest_price = max(pos.highest_price, c.high)
                    
                    # Trailing stop
                    if cfg.enable_trailing and (pos.highest_price - pos.entry_price) >= atr * cfg.trailing_start_atr:
                        new_stop = pos.highest_price - atr * cfg.atr_stop_multiplier
                        pos.stop_price = max(pos.stop_price, new_stop)
                    
                    # Check stop / exit
                    if c.low <= pos.stop_price:
                        exit_p = min(pos.stop_price, c.close)
                        pnl = pos.qty * (exit_p - pos.entry_price)
                        pos.pnl = pnl
                        realized_pnl += pnl
                        daily_realized_pnl += pnl
                        balance += pos.qty * exit_p
                        if pnl > 0: wins += 1
                        else: losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "symbol": pos.symbol,
                            "side": "SELL", "entry": round(pos.entry_price, 4),
                            "exit": round(exit_p, 4), "qty": round(pos.qty, 6),
                            "pnl": round(pnl, 4), "reason": "STOP_LOSS" if pnl <= 0 else "TRAILING_STOP"})
                        positions.remove(pos)

                elif pos.side == "SHORT":
                    pos.lowest_price = min(pos.lowest_price, c.low)
                    
                    if cfg.enable_trailing and (pos.entry_price - pos.lowest_price) >= atr * cfg.trailing_start_atr:
                        new_stop = pos.lowest_price + atr * cfg.atr_stop_multiplier
                        pos.stop_price = min(pos.stop_price, new_stop)
                    
                    if c.high >= pos.stop_price:
                        exit_p = max(pos.stop_price, c.close)
                        pnl = pos.qty * (pos.entry_price - exit_p)
                        pos.pnl = pnl
                        realized_pnl += pnl
                        daily_realized_pnl += pnl
                        balance -= pos.qty * exit_p
                        if pnl > 0: wins += 1
                        else: losses += 1
                        total_trades += 1
                        twr.writerow({"ts": c.ts.isoformat(), "symbol": pos.symbol,
                            "side": "COVER", "entry": round(pos.entry_price, 4),
                            "exit": round(exit_p, 4), "qty": round(pos.qty, 6),
                            "pnl": round(pnl, 4), "reason": "STOP_LOSS" if pnl <= 0 else "TRAILING_STOP"})
                        positions.remove(pos)

            # Check entries for each symbol
            if trades_today >= cfg.max_trades_per_day:
                continue
            if len(positions) >= cfg.max_positions:
                continue

            for sym, candles in candles_by_symbol.items():
                if i >= len(candles):
                    continue
                c = candles[i]
                atr = atr_map[sym][i]
                upper, lower, _ = donchian_channel(candles, cfg.donchian_period, i)
                
                # Count existing positions for this symbol
                sym_long = [p for p in positions if p.symbol == sym and p.side == "LONG"]
                sym_short = [p for p in positions if p.symbol == sym and p.side == "SHORT"]

                # LONG signal: breakout above Donchian high
                if c.close > upper and not sym_long:
                    # Initial entry
                    risk_amount = balance * cfg.risk_per_trade
                    qty = risk_amount / (atr * 1.0) if atr > 0 else 0
                    cost = qty * c.close
                    if cost > balance:
                        qty = balance / c.close * 0.95
                    if qty > 0:
                        stop = c.close - atr * cfg.atr_stop_multiplier
                        pos = Position(symbol=sym, side="LONG", entry_price=c.close, entry_idx=i,
                            qty=qty, atr_at_entry=atr, highest_price=c.high, lowest_price=c.low,
                            stop_price=stop, pyramid_level=0)
                        positions.append(pos)
                        balance -= cost
                        trades_today += 1
                        action = f"LONG_ENTRY"
                        dwr.writerow({"ts": c.ts.isoformat(), "symbol": sym, "action": action,
                            "reason": f"DONCHIAN_BREAK_{cfg.donchian_period}D",
                            "price": round(c.close, 4), "entry": round(c.close, 4),
                            "stop": round(stop, 4), "atr": round(atr, 4),
                            "qty": round(qty, 6), "balance": round(balance, 2)})

                # SHORT signal: breakdown below Donchian low
                if c.close < lower and not sym_short:
                    risk_amount = balance * cfg.risk_per_trade
                    qty = risk_amount / (atr * 1.0) if atr > 0 else 0
                    # For short, we reserve margin
                    if qty > 0:
                        stop = c.close + atr * cfg.atr_stop_multiplier
                        margin = qty * c.close * 0.1  # 10% margin for short
                        if margin <= balance:
                            pos = Position(symbol=sym, side="SHORT", entry_price=c.close, entry_idx=i,
                                qty=qty, atr_at_entry=atr, highest_price=c.high, lowest_price=c.low,
                                stop_price=stop, pyramid_level=0)
                            positions.append(pos)
                            balance -= margin
                            trades_today += 1
                            action = f"SHORT_ENTRY"
                            dwr.writerow({"ts": c.ts.isoformat(), "symbol": sym, "action": action,
                                "reason": f"DONCHIAN_BREAK_{cfg.donchian_period}D",
                                "price": round(c.close, 4), "entry": round(c.close, 4),
                                "stop": round(stop, 4), "atr": round(atr, 4),
                                "qty": round(qty, 6), "balance": round(balance, 2)})

                # Pyramiding (adding to winning positions)
                if cfg.enable_pyramiding and c.close > upper:
                    for pos in sym_long:
                        if pos.pyramid_level >= cfg.pyramid_levels:
                            continue
                        profit_atr = (c.close - pos.entry_price) / atr if atr > 0 else 0
                        if profit_atr >= cfg.pyramid_increment_pct * (pos.pyramid_level + 1):
                            # Add to position
                            add_qty = pos.qty * cfg.pyramid_unit_size_pct
                            cost = add_qty * c.close
                            if cost <= balance:
                                # Rebalance
                                total_qty = pos.qty + add_qty
                                total_cost = pos.qty * pos.entry_price + add_qty * c.close
                                pos.entry_price = total_cost / total_qty
                                pos.qty = total_qty
                                pos.pyramid_level += 1
                                balance -= cost
                                dwr.writerow({"ts": c.ts.isoformat(), "symbol": sym,
                                    "action": f"PYRAMID_L+{pos.pyramid_level}",
                                    "reason": f"PROFIT_{profit_atr:.1f}xATR",
                                    "price": round(c.close, 4), "entry": round(c.close, 4),
                                    "stop": round(pos.stop_price, 4), "atr": round(atr, 4),
                                    "qty": round(add_qty, 6), "balance": round(balance, 2)})

            # Portfolio snapshot
            if i % 10 == 0:
                eq = equity()
                pwr.writerow({"ts": any_candle.ts.isoformat(), "balance": round(balance, 2),
                    "positions": len(positions), "total_equity": round(eq, 2)})
                peak_balance = max(peak_balance, eq)
                dd = (peak_balance - eq) / peak_balance * 100 if peak_balance > 0 else 0
                max_dd = max(max_dd, dd)

        # Close remaining positions at end of data
        for pos in list(positions):
            sym_candles = candles_by_symbol[pos.symbol]
            last_c = sym_candles[-1]
            if pos.side == "LONG":
                pnl = pos.qty * (last_c.close - pos.entry_price)
                balance += pos.qty * last_c.close
            else:
                pnl = pos.qty * (pos.entry_price - last_c.close)
                balance -= pos.qty * last_c.close
            realized_pnl += pnl
            total_trades += 1
            if pnl > 0: wins += 1
            else: losses += 1
            twr.writerow({"ts": last_c.ts.isoformat(), "symbol": pos.symbol,
                "side": "CLOSE", "entry": round(pos.entry_price, 4),
                "exit": round(last_c.close, 4), "qty": round(pos.qty, 6),
                "pnl": round(pnl, 4), "reason": "END_OF_DATA"})
            positions.remove(pos)

    total_pnl = balance - cfg.initial_balance
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    summary = {"initial_balance": cfg.initial_balance, "final_balance": round(balance, 2),
        "total_pnl": round(total_pnl, 2), "total_trades": total_trades,
        "wins": wins, "losses": losses, "win_rate_percent": round(win_rate, 2),
        "max_drawdown_percent": round(max_dd, 2),
        "config": asdict(cfg),
        "outputs": {"decisions_log": str(decisions_path), "trades_log": str(trades_path),
            "portfolio_snapshots": str(portfolio_path), "summary_json": str(summary_path)}}
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def load_candles(path: Path, symbol: str = "") -> List[Candle]:
    candles = []
    with path.open() as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row.get("timestamp_utc","").replace("Z","+00:00"))
            candles.append(Candle(ts=ts, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume", 0)), symbol=symbol or row.get("instrument","")))
    return candles


def load_config(path: Optional[Path]) -> TurtleConfig:
    if path is None: return TurtleConfig()
    data = json.loads(path.read_text())
    c = TurtleConfig()
    for k, v in data.items():
        if hasattr(c, k): setattr(c, k, v)
    return c


def main():
    p = argparse.ArgumentParser(description="TurtleBot — Donchian breakout")
    p.add_argument("--input", nargs="+", required=True, help="CSV file(s)")
    p.add_argument("--symbols", nargs="+", default=None, help="Symbol names (matches --input order)")
    p.add_argument("--config")
    p.add_argument("--output-dir", default="turtle_runs/latest")
    args = p.parse_args()
    cfg = load_config(Path(args.config) if args.config else None)
    
    symbols = args.symbols or [Path(f).stem.split("_")[1] if "_" in Path(f).stem else Path(f).stem for f in args.input]
    candles_by_symbol = {}
    for i, f in enumerate(args.input):
        sym = symbols[i] if i < len(symbols) else f"SYM{i}"
        c = load_candles(Path(f), sym)
        if len(c) >= cfg.donchian_period + cfg.atr_period + 2:
            candles_by_symbol[sym] = c
            print(f"  {sym}: {len(c)} candles")
    
    if not candles_by_symbol:
        raise ValueError("No valid data files")
    
    print(json.dumps(run_turtle(candles_by_symbol, cfg, Path(args.output_dir)), indent=2))


if __name__ == "__main__":
    main()
