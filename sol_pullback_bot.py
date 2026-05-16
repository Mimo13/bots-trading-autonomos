#!/usr/bin/env python3
"""
SolPullbackBot — Bot oportunista para SOL/USDT basado en análisis de mercado 2026-05-10.

Estrategia:
1. Esperar que RSI(4h) baje de 70 (enfriamiento de sobrecompra) — no comprar en FOMO
2. Precio debe estar sobre SMA20 (tendencia alcista intacta)
3. Entrada: vela 4h cierra > apertura tras tocar o acercarse a EMA9/EMA21 (pullback comprado)
4. SL: -2 ATR desde entrada
5. TP: +3 ATR (ratio 1.5:1)
6. Timeout: 12 velas (48 horas) — si no se ejecuta en ese tiempo, se cancela
7. No operar si RSI vuelve a > 80 (sobrecompra extrema)
8. Max 2 operaciones/día

Diseñado como bot temporal de oportunidad mientras SOL mantenga momentum alcista.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
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
class SolPullbackConfig:
    # Risk
    risk_percent: float = 2.0
    max_daily_loss_pct: float = 6.0
    max_trades_per_day: int = 2
    min_rr: float = 1.5

    # ATR
    atr_period: int = 14
    atr_sl_mult: float = 2.0  # SL = entry - 2*ATR
    atr_tp_mult: float = 3.0  # TP = entry + 3*ATR

    # RSI
    rsi_period: int = 14
    rsi_cooled_threshold: float = 70.0  # RSI must be below this to enter
    rsi_extreme_threshold: float = 80.0  # No trade if RSI above this

    # Moving averages
    sma20_period: int = 20
    ema9_period: int = 9
    ema21_period: int = 21
    
    # Entry: how close to EMA for pullback (as fraction of ATR)
    pullback_zone_atr_mult: float = 1.0  # within this many ATR of EMA

    # Timeout
    pending_order_expiry_candles: int = 12  # 12 * 4h = 48h

    # Balance
    initial_balance: float = 100.0
    
    # Symbol
    symbol: str = "SOLUSDT"


def compute_rsi(closes: List[float], period: int = 14) -> float:
    """Compute RSI for the latest period."""
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(candles: List[Candle], period: int = 14) -> float:
    """Compute ATR for the latest period."""
    if len(candles) < period + 1:
        return (candles[-1].high - candles[-1].low)
    tr_values = []
    for i in range(1, len(candles)):
        h_l = candles[i].high - candles[i].low
        h_pc = abs(candles[i].high - candles[i - 1].close)
        l_pc = abs(candles[i].low - candles[i - 1].close)
        tr_values.append(max(h_l, h_pc, l_pc))
    return sum(tr_values[-period:]) / period


def compute_sma(closes: List[float], period: int) -> float:
    """Simple moving average."""
    if len(closes) < period:
        return sum(closes) / len(closes)
    return sum(closes[-period:]) / period


def compute_ema(closes: List[float], period: int) -> float:
    """Exponential moving average."""
    if len(closes) < period:
        return sum(closes) / len(closes)
    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def compute_all_indicators(candles: List[Candle], cfg: SolPullbackConfig) -> Dict:
    """Compute all technical indicators."""
    closes = [c.close for c in candles]
    
    rsi = compute_rsi(closes, cfg.rsi_period)
    atr = compute_atr(candles, cfg.atr_period)
    sma20 = compute_sma(closes, cfg.sma20_period)
    ema9 = compute_ema(closes, cfg.ema9_period)
    ema21 = compute_ema(closes, cfg.ema21_period)
    
    current = candles[-1]
    price = current.close
    
    # Pullback zone: ± pullback_zone_atr_mult * ATR around EMA9/EMA21
    pullback_low = min(ema9, ema21) - cfg.pullback_zone_atr_mult * atr
    pullback_high = max(ema9, ema21) + cfg.pullback_zone_atr_mult * atr
    
    return {
        'rsi': rsi,
        'atr': atr,
        'sma20': sma20,
        'ema9': ema9,
        'ema21': ema21,
        'price': price,
        'pullback_zone_low': pullback_low,
        'pullback_zone_high': pullback_high,
        'trend': 'BULLISH' if price > sma20 else 'BEARISH',
    }


def should_enter(candles: List[Candle], cfg: SolPullbackConfig, 
                 open_positions: int, daily_trades: int) -> Tuple[bool, str, dict]:
    """
    Decide if we should enter a trade now.
    Returns (should_enter, reason, indicators_dict).
    """
    ind = compute_all_indicators(candles, cfg)
    c = candles[-1]
    
    # Gate checks
    if daily_trades >= cfg.max_trades_per_day:
        return False, "MAX_TRADES_DAY", ind
    
    if open_positions > 0:
        return False, "ALREADY_IN_POSITION", ind
    
    if ind['trend'] == 'BEARISH':
        return False, "BEARISH_TREND", ind
    
    if ind['rsi'] > cfg.rsi_extreme_threshold:
        return False, f"RSI_EXTREME_{ind['rsi']:.1f}", ind
    
    # Core signal: RSI cooled + price near EMA pullback zone + bullish candle
    rsi_cooled = ind['rsi'] < cfg.rsi_cooled_threshold
    
    in_pullback_zone = (
        c.low <= ind['pullback_zone_high'] and 
        c.low >= ind['pullback_zone_low']
    )
    
    # Also check if the candle's low touched near EMA (relaxed: within 2x ATR)
    near_ema = min(
        abs(c.low - ind['ema9']),
        abs(c.low - ind['ema21'])
    ) <= cfg.pullback_zone_atr_mult * 2 * ind['atr']
    
    # Bullish candle: close > open
    bullish_candle = c.close > c.open
    
    # Build reason
    reasons = []
    if rsi_cooled:
        reasons.append(f"RSI_COOLED_{ind['rsi']:.1f}")
    if in_pullback_zone:
        reasons.append("IN_ZONE")
    elif near_ema:
        reasons.append("NEAR_EMA")
    if bullish_candle:
        reasons.append("BULLISH_CANDLE")
    
    # Entry logic: need RSI cooled + (in zone or near EMA) + bullish candle
    if rsi_cooled and (in_pullback_zone or near_ema) and bullish_candle:
        return True, "|".join(reasons), ind
    
    # Secondary entry: RSI not cooled but strong pullback to SMA20
    if in_pullback_zone and bullish_candle and ind['rsi'] < 65:
        return True, f"STRONG_PULLBACK_RSI{ind['rsi']:.1f}", ind
    
    if not rsi_cooled:
        return False, f"RSI_TOO_HIGH_{ind['rsi']:.1f}", ind
    if not (in_pullback_zone or near_ema):
        return False, "NO_PULLBACK", ind
    if not bullish_candle:
        return False, "BEARISH_CANDLE", ind
    
    return False, "NO_SIGNAL", ind


def compute_entry_exit(candles: List[Candle], cfg: SolPullbackConfig, 
                       ind: dict) -> Tuple[float, float, float]:
    """Compute entry price, stop loss, and take profit."""
    c = candles[-1]
    entry = c.close
    atr = ind['atr']
    
    # For long: SL below entry, TP above entry
    sl = entry - cfg.atr_sl_mult * atr
    tp = entry + cfg.atr_tp_mult * atr
    
    return entry, sl, tp


def run_simulation(candles: List[Candle], cfg: SolPullbackConfig, 
                   out_dir: Path) -> Dict:
    """Run the SolPullback strategy on historical candles."""
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    summary_path = out_dir / "summary.json"
    
    dec_fields = ["ts", "symbol", "rsi", "atr", "sma20", "ema9", "ema21",
                  "action", "reason", "price", "entry", "sl", "tp", "balance"]
    tr_fields = ["ts", "symbol", "action", "entry", "exit", "sl", "tp", 
                 "pnl", "rr", "reason", "qty"]
    
    balance = cfg.initial_balance
    wins = 0
    losses = 0
    total_trades = 0
    daily_trades = 0
    last_reset_day = candles[0].ts.date() if candles else datetime.now().date()
    daily_pnl = 0.0
    daily_start_balance = balance
    peak_balance = balance
    max_dd = 0.0
    
    open_position = None  # {'entry': float, 'sl': float, 'tp': float, 'fill_idx': int, 'volume': float}
    pending_order = None  # {'entry': float, 'sl': float, 'tp': float, 'expiry_idx': int, 'volume': float}
    
    with (decisions_path.open("w", newline="", encoding="utf-8") as dfile,
          trades_path.open("w", newline="", encoding="utf-8") as tfile):
        
        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        dwr.writeheader()
        twr.writeheader()
        
        warmup = cfg.atr_period + cfg.rsi_period + max(cfg.sma20_period, cfg.ema21_period) + 5
        
        for i, c in enumerate(candles):
            if i < warmup:
                continue
            
            # Daily reset
            if c.ts.date() != last_reset_day:
                last_reset_day = c.ts.date()
                daily_trades = 0
                daily_pnl = 0.0
                daily_start_balance = balance
            
            window = candles[:i + 1]
            ind = compute_all_indicators(window, cfg)
            
            action = "HOLD"
            reason = ""
            
            # Check if pending order should fill
            if pending_order and open_position is None:
                if c.high >= pending_order['entry']:
                    # Fill at entry
                    open_position = {
                        'entry': pending_order['entry'],
                        'sl': pending_order['sl'],
                        'tp': pending_order['tp'],
                        'fill_idx': i,
                        'volume': pending_order['volume'],
                    }
                    twr.writerow({
                        "ts": c.ts.isoformat(),
                        "symbol": cfg.symbol,
                        "action": "BUY",
                        "entry": round(pending_order['entry'], 6),
                        "exit": "",
                        "sl": round(pending_order['sl'], 6),
                        "tp": round(pending_order['tp'], 6),
                        "pnl": 0.0,
                        "rr": round(abs(pending_order['tp'] - pending_order['entry']) / max(0.0001, abs(pending_order['entry'] - pending_order['sl'])), 2),
                        "reason": "ENTRY_FILLED",
                        "qty": round(pending_order['volume'] / pending_order['entry'], 6),
                    })
                    pending_order = None
                    daily_trades += 1
                    action = "ENTRY_FILLED"
                    reason = "ORDER_FILLED"
                elif i >= pending_order.get('expiry_idx', i + 999):
                    pending_order = None
                    reason = "ORDER_EXPIRED"
            
            # Check open position for SL/TP hit
            if open_position:
                pos = open_position
                if c.low <= pos['sl']:
                    # Stop loss hit
                    pnl = pos['volume'] * (pos['sl'] - pos['entry'])
                    balance += pnl
                    daily_pnl += pnl
                    losses += 1
                    total_trades += 1
                    twr.writerow({
                        "ts": c.ts.isoformat(),
                        "symbol": cfg.symbol,
                        "action": "SELL",
                        "entry": round(pos['entry'], 6),
                        "exit": round(pos['sl'], 6),
                        "sl": round(pos['sl'], 6),
                        "tp": round(pos['tp'], 6),
                        "pnl": round(pnl, 4),
                        "rr": round(abs(pos['tp'] - pos['entry']) / max(0.0001, abs(pos['entry'] - pos['sl'])), 2),
                        "reason": "SL_HIT",
                        "qty": round(pos['volume'] / pos['entry'], 6),
                    })
                    open_position = None
                    action = "SL_HIT"
                    reason = f"LOSS_{pnl:.2f}"
                elif c.high >= pos['tp']:
                    # Take profit hit
                    pnl = pos['volume'] * (pos['tp'] - pos['entry'])
                    balance += pnl
                    daily_pnl += pnl
                    wins += 1
                    total_trades += 1
                    twr.writerow({
                        "ts": c.ts.isoformat(),
                        "symbol": cfg.symbol,
                        "action": "SELL",
                        "entry": round(pos['entry'], 6),
                        "exit": round(pos['tp'], 6),
                        "sl": round(pos['sl'], 6),
                        "tp": round(pos['tp'], 6),
                        "pnl": round(pnl, 4),
                        "rr": round(abs(pos['tp'] - pos['entry']) / max(0.0001, abs(pos['entry'] - pos['sl'])), 2),
                        "reason": "TP_HIT",
                        "qty": round(pos['volume'] / pos['entry'], 6),
                    })
                    open_position = None
                    action = "TP_HIT"
                    reason = f"WIN_{pnl:.2f}"
                else:
                    action = "IN_POSITION"
                    reason = f"HOLDING_ENTRY_{pos['entry']:.4f}"
            
            # Try to enter if no position and no pending order
            if open_position is None and pending_order is None:
                # Check daily loss limit
                if daily_pnl < -(cfg.max_daily_loss_pct / 100.0) * daily_start_balance:
                    reason = "DAILY_LOSS_LIMIT"
                else:
                    should, signal_reason, _ = should_enter(
                        window, cfg, 
                        open_positions=1 if open_position else 0,
                        daily_trades=daily_trades
                    )
                    
                    if should:
                        entry, sl, tp = compute_entry_exit(window, cfg, ind)
                        
                        # Position sizing
                        risk_amount = balance * (cfg.risk_percent / 100.0)
                        sl_distance = abs(entry - sl)
                        volume = risk_amount / sl_distance if sl_distance > 0 else 0
                        rr = abs(tp - entry) / max(0.0001, sl_distance)
                        
                        if rr >= cfg.min_rr and volume > 0:
                            pending_order = {
                                'entry': entry,
                                'sl': sl,
                                'tp': tp,
                                'expiry_idx': i + cfg.pending_order_expiry_candles,
                                'volume': volume,
                            }
                            action = "PLACE_BUY"
                            reason = f"ENTRY_SIGNAL_{signal_reason}_RR{rr:.1f}"
                        else:
                            reason = f"LOW_RR_{rr:.1f}"
                    else:
                        reason = signal_reason
            else:
                if not reason:
                    reason = "WAITING"
            
            # Always log decision
            dwr.writerow({
                "ts": c.ts.isoformat(),
                "symbol": cfg.symbol,
                "rsi": round(ind['rsi'], 1),
                "atr": round(ind['atr'], 4),
                "sma20": round(ind['sma20'], 4),
                "ema9": round(ind['ema9'], 4),
                "ema21": round(ind['ema21'], 4),
                "action": action,
                "reason": reason,
                "price": round(c.close, 6),
                "entry": round(pending_order['entry'], 6) if pending_order else 0,
                "sl": round(pending_order['sl'], 6) if pending_order else 0,
                "tp": round(pending_order['tp'], 6) if pending_order else 0,
                "balance": round(balance, 2),
            })
            
            # Track drawdown
            peak_balance = max(peak_balance, balance)
            dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
            max_dd = max(max_dd, dd)
    
    # Close any remaining position at last candle's close
    if open_position:
        exit_price = candles[-1].close
        pnl = open_position['volume'] * (exit_price - open_position['entry'])
        balance += pnl
        total_trades += 1
        if pnl > 0:
            wins += 1
        else:
            losses += 1
    
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
        "symbol": cfg.symbol,
        "strategy": "SolPullback_RSI_ATR_EMA",
        "config": {k: v for k, v in asdict(cfg).items() if not k.startswith('_')},
        "outputs": {
            "decisions_log": str(decisions_path),
            "trades_log": str(trades_path),
            "summary_json": str(summary_path),
        }
    }
    
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def fetch_binance_klines(symbol: str = "SOLUSDT", interval: str = "4h", 
                          limit: int = 200) -> List[Candle]:
    """Fetch OHLCV data from Binance public API."""
    import urllib.request
    
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'SolPullbackBot/1.0'
    })
    
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    
    candles = []
    for k in data:
        ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        candles.append(Candle(
            ts=ts,
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
        ))
    
    return candles


def load_config(path: Optional[Path]) -> SolPullbackConfig:
    if path is None or not path.exists():
        return SolPullbackConfig()
    data = json.loads(path.read_text())
    c = SolPullbackConfig()
    for k, v in data.items():
        if hasattr(c, k):
            setattr(c, k, v)
    return c


def main():
    p = argparse.ArgumentParser(description="SolPullbackBot — SOL pullback opportunity bot")
    p.add_argument("--input", help="CSV file with OHLCV data (optional, uses Binance API if omitted)")
    p.add_argument("--config", help="JSON config file")
    p.add_argument("--output-dir", default="sol_pullback_runs/latest")
    p.add_argument("--fetch", action="store_true", default=True, 
                   help="Fetch live data from Binance (default)")
    p.add_argument("--interval", default="4h", help="Candle interval (default: 4h)")
    p.add_argument("--limit", type=int, default=200, help="Number of candles (default: 200)")
    args = p.parse_args()
    
    cfg = load_config(Path(args.config) if args.config else None)
    
    if args.input:
        # Load from CSV (same format as other bots)
        candles = []
        with open(args.input) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = row.get("timestamp_utc", row.get("ts", "")).replace("Z", "+00:00")
                ts = datetime.fromisoformat(ts_str)
                candles.append(Candle(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                ))
    else:
        candles = fetch_binance_klines(cfg.symbol, args.interval, args.limit)
    
    if len(candles) < 50:
        raise ValueError(f"Need at least 50 candles, got {len(candles)}")
    
    print(f"Running SolPullbackBot on {cfg.symbol} with {len(candles)} candles ({args.interval})")
    print(f"  Period: {candles[0].ts.isoformat()} → {candles[-1].ts.isoformat()}")
    print(f"  Current price: ${candles[-1].close:.4f}")
    
    summary = run_simulation(candles, cfg, Path(args.output_dir))
    
    print(f"\nResults:")
    print(f"  Balance: ${summary['initial_balance']:.2f} → ${summary['final_balance']:.2f}")
    print(f"  PnL: ${summary['total_pnl']:.2f}")
    print(f"  Trades: {summary['total_trades']} ({summary['wins']}W/{summary['losses']}L)")
    print(f"  Win Rate: {summary['win_rate_percent']:.1f}%")
    print(f"  Max DD: {summary['max_drawdown_percent']:.2f}%")
    
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
