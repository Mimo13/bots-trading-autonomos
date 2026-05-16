#!/usr/bin/env python3
"""
PolyPortfolioPaper v2 — Bot de cartera para SOL/USDT con señales TA reales.

Reescrito 2026-05-10 para sustituir el modelo de señales fake (p_model_up basado
en momentum heuristic) por indicadores técnicos reales calculados desde OHLCV.

Estrategia:
- RSI(14): comprar cuando RSI < 35 (sobrevendido), vender cuando RSI > 65 (sobrecomprado)
- Scale-in: compras más agresivas cuanto más bajo el RSI
- ATR(14): position sizing dinámico
- Take Profit: +4% desde avg entry | Stop Loss: -2.5%
- Timeout: 30 velas 5m (2.5h) máximo por posición
- Máximo 6 trades/día, pérdida diaria max 10%
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class PortfolioConfig:
    initial_balance: float = 100.0
    symbol: str = "SOLUSDT"

    # RSI thresholds
    rsi_period: int = 14
    rsi_buy_threshold: float = 35.0       # comprar cuando RSI < esto
    rsi_sell_threshold: float = 65.0       # vender cuando RSI > esto
    rsi_strong_buy: float = 25.0           # compra más agresiva si RSI < esto

    # Position sizing
    risk_per_trade: float = 0.08           # % del balance por compra normal
    risk_strong: float = 0.15              # % del balance en compra fuerte (RSI muy bajo)
    max_position_pct: float = 0.40         # max % del balance en SOL

    # ATR
    atr_period: int = 14
    atr_stop_mult: float = 2.0

    # Take Profit / Stop Loss
    take_profit_pct: float = 0.04          # +4%
    stop_loss_pct: float = 0.025           # -2.5%

    # Risk
    max_hold_candles: int = 30             # 30 * 5m = 2.5h
    max_trades_per_day: int = 6
    max_daily_loss_pct: float = 10.0


def compute_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


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


def load_candles(path: Path) -> List[dict]:
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'ts': r.get('timestamp_utc') or r.get('ts', ''),
                    'symbol': r.get('instrument') or r.get('symbol', 'SOLUSDT'),
                    'open': float(r.get('open', 0)),
                    'high': float(r.get('high', 0)),
                    'low': float(r.get('low', 0)),
                    'close': float(r.get('close', 0)),
                    'volume': float(r.get('volume', 0)),
                })
            except Exception:
                continue
    return rows


def run_simulation(candles: List[dict], cfg: PortfolioConfig, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tlog = out_dir / 'trades_log.csv'
    dlog = out_dir / 'decisions_log.csv'
    sump = out_dir / 'summary.json'

    balance = cfg.initial_balance
    position_qty = 0.0
    cost_basis = 0.0
    avg_entry = 0.0
    realized_pnl = 0.0
    candles_held = 0
    day_trades = 0
    last_day = ''
    daily_pnl = 0.0
    peak_equity = cfg.initial_balance
    max_dd = 0.0
    wins = 0
    losses = 0

    with open(tlog, 'w', newline='') as tf, open(dlog, 'w', newline='') as df:
        tw = csv.DictWriter(tf, fieldnames=[
            'ts', 'symbol', 'side', 'qty', 'price', 'usd_amount',
            'realized_pnl', 'reason'
        ])
        dw = csv.DictWriter(df, fieldnames=[
            'timestamp_utc', 'instrument', 'action', 'reason_code',
            'rsi', 'atr', 'balance', 'position_qty', 'position_value',
            'total_equity', 'price'
        ])
        tw.writeheader()
        dw.writeheader()

        closes = [c['close'] for c in candles]
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]

        warmup = cfg.rsi_period + cfg.atr_period + 5

        for i, c in enumerate(candles):
            price = c['close']
            if price <= 0:
                continue

            # Daily reset
            day_key = c['ts'][:10] if c['ts'] else ''
            if day_key and day_key != last_day:
                last_day = day_key
                day_trades = 0
                daily_pnl = 0.0

            action = "HOLD"
            reason = ""
            trade_side = ""
            qty_traded = 0.0
            usd_amount = 0.0
            trade_pnl = 0.0
            trade_reason = ""

            if i < warmup:
                reason = "WARMUP"
            else:
                # Compute TA
                rsi = compute_rsi(closes[:i + 1], cfg.rsi_period)
                atr = compute_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], cfg.atr_period)

                # Hard gates
                if day_trades >= cfg.max_trades_per_day:
                    reason = "MAX_TRADES_DAY"
                elif daily_pnl < -(cfg.max_daily_loss_pct / 100.0) * cfg.initial_balance:
                    reason = "DAILY_LOSS_LIMIT"

                if not reason:
                    # BUY signal: RSI oversold
                    if rsi < cfg.rsi_buy_threshold and position_qty < 0.0001:
                        action = "BUY"
                        # Scale position size based on RSI depth
                        if rsi < cfg.rsi_strong_buy:
                            risk = cfg.risk_strong
                        else:
                            risk = cfg.risk_per_trade
                        spend = balance * risk
                        max_spend = balance * cfg.max_position_pct
                        spend = min(spend, max_spend)
                        if spend > 0.5 and price > 0:
                            qty = spend / price
                            cost_basis += spend
                            position_qty += qty
                            avg_entry = cost_basis / position_qty
                            balance -= spend
                            qty_traded = qty
                            usd_amount = spend
                            trade_side = "BUY"
                            trade_reason = f"RSI_BUY_{rsi:.0f}"
                            day_trades += 1

                    # SELL signal: RSI overbought
                    elif rsi > cfg.rsi_sell_threshold and position_qty > 0.0001:
                        action = "SELL"
                        proceeds = position_qty * price
                        trade_pnl = proceeds - cost_basis
                        realized_pnl += trade_pnl
                        daily_pnl += trade_pnl
                        balance += proceeds
                        qty_traded = position_qty
                        usd_amount = proceeds
                        trade_side = "SELL"
                        trade_reason = f"RSI_SELL_{rsi:.0f}"
                        day_trades += 1
                        if trade_pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        position_qty = 0.0
                        cost_basis = 0.0
                        avg_entry = 0.0
                        candles_held = 0

                # Track hold duration
                if position_qty > 0.0001:
                    candles_held += 1
                else:
                    candles_held = 0

                # Timeout: force sell if held too long
                if position_qty > 0.0001 and candles_held >= cfg.max_hold_candles and action != "SELL":
                    action = "TIMEOUT_SELL"
                    trade_pnl = (price - avg_entry) * position_qty
                    proceeds = position_qty * price
                    realized_pnl += trade_pnl
                    daily_pnl += trade_pnl
                    balance += proceeds
                    trade_side = "SELL"
                    trade_reason = "MAX_HOLD"
                    qty_traded = position_qty
                    usd_amount = proceeds
                    if trade_pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    position_qty = 0.0
                    cost_basis = 0.0
                    avg_entry = 0.0
                    candles_held = 0

                # Take Profit / Stop Loss
                if position_qty > 0.0001 and avg_entry > 0 and action != "SELL":
                    pnl_pct = (price - avg_entry) / avg_entry
                    if pnl_pct >= cfg.take_profit_pct:
                        action = "TP_SELL"
                        trade_pnl = (price - avg_entry) * position_qty
                        proceeds = position_qty * price
                        realized_pnl += trade_pnl
                        daily_pnl += trade_pnl
                        balance += proceeds
                        trade_side = "SELL"
                        trade_reason = "TAKE_PROFIT"
                        qty_traded = position_qty
                        usd_amount = proceeds
                        wins += 1
                        position_qty = 0.0
                        cost_basis = 0.0
                        avg_entry = 0.0
                        candles_held = 0
                    elif pnl_pct <= -cfg.stop_loss_pct:
                        action = "SL_SELL"
                        trade_pnl = (price - avg_entry) * position_qty
                        proceeds = position_qty * price
                        realized_pnl += trade_pnl
                        daily_pnl += trade_pnl
                        balance += proceeds
                        trade_side = "SELL"
                        trade_reason = "STOP_LOSS"
                        qty_traded = position_qty
                        usd_amount = proceeds
                        losses += 1
                        position_qty = 0.0
                        cost_basis = 0.0
                        avg_entry = 0.0
                        candles_held = 0

            # Log trade
            if trade_side and qty_traded > 0:
                tw.writerow({
                    'ts': c['ts'],
                    'symbol': cfg.symbol,
                    'side': trade_side,
                    'qty': round(qty_traded, 8),
                    'price': round(price, 6),
                    'usd_amount': round(usd_amount, 2),
                    'realized_pnl': round(trade_pnl, 4),
                    'reason': trade_reason,
                })

            # Equity tracking
            pos_value = position_qty * price
            total_equity = balance + pos_value
            peak_equity = max(peak_equity, total_equity)
            dd = (peak_equity - total_equity) / peak_equity if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)

            # Compute RSI/ATR for logging
            rsi_val = compute_rsi(closes[:i + 1], cfg.rsi_period) if i >= warmup else 50
            atr_val = compute_atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], cfg.atr_period) if i >= warmup else 0
            reason_code = reason or (action + ("_EXECUTED" if qty_traded > 0 else ""))

            dw.writerow({
                'timestamp_utc': c['ts'],
                'instrument': cfg.symbol,
                'action': action,
                'reason_code': reason_code,
                'rsi': round(rsi_val, 1),
                'atr': round(atr_val, 6),
                'balance': round(balance, 2),
                'position_qty': round(position_qty, 6),
                'position_value': round(pos_value, 2),
                'total_equity': round(total_equity, 2),
                'price': round(price, 6),
            })

    # Final: close any remaining position at mark-to-market
    final_price = candles[-1]['close']
    pos_value = position_qty * final_price
    unrealized = (final_price - avg_entry) * position_qty if avg_entry > 0 and position_qty > 0 else 0
    total_equity = balance + pos_value
    total_pnl = total_equity - cfg.initial_balance

    summary = {
        'initial_balance': cfg.initial_balance,
        'final_balance': round(balance, 2),
        'final_position_qty': round(position_qty, 6),
        'position_value': round(pos_value, 2),
        'total_equity': round(total_equity, 2),
        'total_pnl': round(total_pnl, 2),
        'realized_pnl': round(realized_pnl, 4),
        'unrealized_pnl': round(unrealized, 4),
        'total_trades': wins + losses,
        'wins': wins,
        'losses': losses,
        'win_rate_pct': round(wins / max(1, wins + losses) * 100, 1),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'config': {k: v for k, v in asdict(cfg).items()},
        'outputs': {
            'decisions_log': str(dlog),
            'trades_log': str(tlog),
            'summary_json': str(sump),
        },
    }
    sump.write_text(json.dumps(summary, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser(description="PolyPortfolioPaper v2 — RSI-based portfolio bot")
    p.add_argument('--input', required=True)
    p.add_argument('--config')
    p.add_argument('--output-dir', default='portfolio_runs/latest')
    args = p.parse_args()

    cfg = PortfolioConfig()
    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            d = json.loads(cfg_path.read_text())
            for k, v in d.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)

    candles = load_candles(Path(args.input))
    if len(candles) < 50:
        raise ValueError(f'Need at least 50 candles, got {len(candles)}')

    summary = run_simulation(candles, cfg, Path(args.output_dir))
    print(f'PolyPortfolioPaper v2 — {cfg.symbol} (RSI-based)')
    print(f'  Period: {candles[0]["ts"]} → {candles[-1]["ts"]}')
    print(f'  Result: ${summary["initial_balance"]:.2f} → ${summary["total_equity"]:.2f}')
    print(f'  PnL: ${summary["total_pnl"]:.2f} (realized: ${summary["realized_pnl"]:.4f})')
    print(f'  Trades: {summary["total_trades"]} ({summary["wins"]}W/{summary["losses"]}L) WR: {summary["win_rate_pct"]}%')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
