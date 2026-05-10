#!/usr/bin/env python3
"""
XRP Grid Bot v2 — cuadrícula dinámica centrada en precio con ATR spacing.
Rediseñado 2026-05-10 tras detectar bugs críticos en v1.

Arquitectura:
- Grid auto-centrado en el precio actual al iniciar
- Niveles espaciados por ATR (no rango fijo)
- BUY por debajo del centro, SELL por encima
- Cost basis tracking → PnL real en cada SELL
- Rebalance automático cuando el precio se sale del grid
- Gestión de riesgo: max XRP hold %, max trades/día, tamaño por ATR
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class GridConfig:
    # Balance
    initial_balance: float = 100.0
    initial_xrp: float = 0.0
    symbol: str = "XRPUSDT"

    # Grid dinámico
    grid_levels_each_side: int = 3       # niveles BUY y niveles SELL (total = 2*levels - 1 incluyendo centro)
    grid_spacing_atr: float = 1.0         # espacio entre niveles en ATRs
    grid_atr_period: int = 14

    # Riesgo
    risk_per_trade_pct: float = 0.10      # % del balance por nivel de grid
    max_xrp_hold_pct: float = 0.60        # max % del equity en XRP
    max_trades_per_day: int = 20          # limitar operaciones diarias
    min_profit_per_trade_pct: float = 0.001  # 0.1% mínimo para abrir trade

    # Rebalance
    rebalance_threshold_atr: float = 2.0  # si precio se mueve > 2 ATR del centro, rebuild grid

    # Misc
    enable_cost_tracking: bool = True


@dataclass
class GridLevel:
    price: float
    side: str          # 'BUY' o 'SELL'
    filled: bool = False
    allocated_usd: float = 0.0
    allocated_qty: float = 0.0
    paired: bool = False  # ya fue emparejado con su contraparte


def compute_atr(candles: List[dict], period: int = 14) -> float:
    """ATR desde lista de velas {open,high,low,close}."""
    if len(candles) < period + 1:
        return 0.01
    tr_values = []
    for i in range(1, len(candles)):
        h_l = candles[i]['high'] - candles[i]['low']
        h_pc = abs(candles[i]['high'] - candles[i - 1]['close'])
        l_pc = abs(candles[i]['low'] - candles[i - 1]['close'])
        tr_values.append(max(h_l, h_pc, l_pc))
    return sum(tr_values[-period:]) / period


def compute_sma(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return sum(closes) / len(closes)
    return sum(closes[-period:]) / period


class XrpGridBot:
    def __init__(self, cfg: GridConfig, out_dir: Path):
        self.cfg = cfg
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.balance_usd = cfg.initial_balance
        self.xrp_hold = cfg.initial_xrp
        self.grid: List[GridLevel] = []
        self.trades: List[dict] = []
        self.buy_queue: List[dict] = []  # {(price, qty, cost_usd)}
        
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.day_trades = 0
        self.last_reset_day = -1
        self.grid_center = 0.0
        self.last_rebalance_price = 0.0

    def _build_grid(self, center_price: float, atr: float):
        """Construir grid simétrico alrededor de center_price."""
        self.grid = []
        self.grid_center = center_price
        self.last_rebalance_price = center_price
        spacing = self.cfg.grid_spacing_atr * atr
        
        # Niveles BUY por debajo del centro
        for i in range(self.cfg.grid_levels_each_side, 0, -1):
            price = center_price - spacing * i
            self.grid.append(GridLevel(price=round(price, 6), side='BUY'))
        
        # Niveles SELL por encima del centro
        for i in range(1, self.cfg.grid_levels_each_side + 1):
            price = center_price + spacing * i
            self.grid.append(GridLevel(price=round(price, 6), side='SELL'))

    def _needs_rebalance(self, current_price: float, atr: float) -> bool:
        """Determinar si el grid necesita reconstruirse."""
        if not self.grid or self.grid_center == 0:
            return True
        distance = abs(current_price - self.grid_center)
        threshold = self.cfg.rebalance_threshold_atr * atr
        return distance > threshold

    def _allocate_trade(self, price: float) -> Tuple[float, float]:
        """Calcular cuánto USDT y XRP asignar a un nivel."""
        alloc_usd = self.balance_usd * self.cfg.risk_per_trade_pct
        alloc_qty = alloc_usd / price if price > 0 else 0
        return alloc_usd, alloc_qty

    def _can_buy(self, price: float) -> bool:
        """Verificar si podemos comprar más XRP."""
        current_xrp_value = self.xrp_hold * price
        total_equity = self.balance_usd + current_xrp_value
        if total_equity <= 0:
            return True
        current_xrp_pct = current_xrp_value / total_equity
        return current_xrp_pct < self.cfg.max_xrp_hold_pct

    def _reset_day_if_needed(self, ts_str: str):
        if not ts_str:
            return
        try:
            day = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timetuple().tm_yday
        except Exception:
            return
        if day != self.last_reset_day:
            self.day_trades = 0
            self.last_reset_day = day

    def _record_trade(self, tw, ts, side, price, qty, usd_amount, pnl, symbol, reason):
        """Escribir trade en CSV."""
        tw.writerow({
            'ts': ts,
            'symbol': symbol,
            'side': side,
            'action': side,
            'entry': round(price, 6) if side == 'BUY' else 0,
            'exit': round(price, 6) if side == 'SELL' else 0,
            'price': round(price, 6),
            'qty': round(qty, 6),
            'usd_amount': round(usd_amount, 2),
            'pnl': round(pnl, 4),
            'rr': 0,
            'reason': reason,
            'sl': 0,
            'tp': 0,
        })

    def _get_matching_buy(self, sell_price: float) -> Optional[dict]:
        """Encontrar la compra más antigua (FIFO) para emparejar con esta venta."""
        if not self.buy_queue:
            return None
        # FIFO: vender contra la compra más antigua
        buy = self.buy_queue[0]
        if buy['price'] < sell_price:
            return self.buy_queue.pop(0)
        return None

    def run_simulation(self, candle_rows: List[dict]) -> dict:
        """Ejecutar simulación sobre datos históricos con grid dinámico."""
        if len(candle_rows) < 30:
            raise ValueError("Need at least 30 candles")

        dlog_path = self.out_dir / 'decisions_log.csv'
        tlog_path = self.out_dir / 'trades_log.csv'
        summary_path = self.out_dir / 'summary.json'

        # Usar las primeras 30 velas para inicializar indicadores
        initial_candles = candle_rows[:30]
        initial_atr = compute_atr(initial_candles, self.cfg.grid_atr_period)
        
        # Precio inicial = media de las últimas velas del warmup
        center_price = sum(c['close'] for c in initial_candles[-5:]) / 5
        self._build_grid(center_price, initial_atr)

        with open(dlog_path, 'w', newline='') as df, open(tlog_path, 'w', newline='') as tf:
            dw = csv.DictWriter(df, fieldnames=[
                'ts', 'price', 'action', 'reason', 'balance_usd', 'xrp_hold',
                'xrp_value', 'total_equity', 'grid_center', 'atr', 'grid_levels'
            ])
            tw = csv.DictWriter(tf, fieldnames=[
                'ts', 'symbol', 'side', 'action', 'entry', 'exit', 'price', 'qty',
                'usd_amount', 'pnl', 'rr', 'reason', 'sl', 'tp'
            ])
            dw.writeheader()
            tw.writeheader()

            closes_window = [c['close'] for c in initial_candles]

            for i, row in enumerate(candle_rows):
                ts = row.get('ts', '')
                price = float(row.get('close', 0))
                high = float(row.get('high', price))
                low = float(row.get('low', price))
                
                if price <= 0:
                    continue

                closes_window.append(price)
                self._reset_day_if_needed(ts)

                # Recalcular ATR con ventana deslizante
                window_for_atr = candle_rows[max(0, i - 30):i + 1]
                if len(window_for_atr) >= 15:
                    atr = compute_atr(window_for_atr, self.cfg.grid_atr_period)
                else:
                    atr = initial_atr

                # Rebalance si es necesario
                if self._needs_rebalance(price, atr):
                    self._build_grid(price, atr)

                action = "HOLD"
                reason = ""

                # Verificar niveles BUY (usando low de la vela)
                for level in self.grid:
                    if level.side != 'BUY' or level.filled:
                        continue
                    if low <= level.price and self.balance_usd > 1:
                        if not self._can_buy(price):
                            reason = "MAX_XRP_HOLD"
                            continue
                        if self.day_trades >= self.cfg.max_trades_per_day:
                            reason = "MAX_TRADES_DAY"
                            continue

                        alloc_usd, alloc_qty = self._allocate_trade(level.price)
                        if alloc_qty <= 0:
                            continue

                        # Ejecutar compra al precio del nivel de grid
                        self.balance_usd -= alloc_usd
                        self.xrp_hold += alloc_qty
                        level.filled = True
                        level.allocated_usd = alloc_usd
                        level.allocated_qty = alloc_qty

                        # Registrar en FIFO para PnL tracking
                        self.buy_queue.append({
                            'price': level.price,
                            'qty': alloc_qty,
                            'cost_usd': alloc_usd,
                            'ts': ts,
                        })

                        self.day_trades += 1
                        self.total_trades += 1
                        action = "GRID_BUY"
                        reason = f"BUY@{level.price:.4f}"

                        self._record_trade(tw, ts, 'BUY', level.price, alloc_qty,
                                          alloc_usd, 0, self.cfg.symbol, reason)
                        break  # Un nivel por vela

                # Verificar niveles SELL (usando high de la vela)
                for level in self.grid:
                    if level.side != 'SELL' or level.filled:
                        continue
                    if high >= level.price and self.xrp_hold > 0:
                        # Intentar emparejar con una compra (FIFO)
                        buy = self._get_matching_buy(level.price)
                        if buy is None:
                            reason = "NO_MATCHING_BUY"
                            continue

                        sell_qty = buy['qty']
                        sell_usd = sell_qty * level.price
                        pnl = sell_usd - buy['cost_usd']

                        self.xrp_hold -= sell_qty
                        self.balance_usd += sell_usd

                        # Marcar nivel SELL como lleno
                        level.filled = True
                        level.allocated_usd = sell_usd
                        level.allocated_qty = sell_qty

                        self.day_trades += 1
                        self.total_trades += 1
                        if pnl > 0:
                            self.wins += 1
                        else:
                            self.losses += 1

                        action = "GRID_SELL"
                        reason = f"SELL@{level.price:.4f}_PNL{pnl:.4f}"

                        self._record_trade(tw, ts, 'SELL', level.price, sell_qty,
                                          sell_usd, pnl, self.cfg.symbol, reason)
                        break

                if not reason:
                    reason = "NO_TRIGGER"

                # Log de decisión
                xrp_value = self.xrp_hold * price
                total_equity = self.balance_usd + xrp_value
                dw.writerow({
                    'ts': ts,
                    'price': round(price, 6),
                    'action': action,
                    'reason': reason,
                    'balance_usd': round(self.balance_usd, 2),
                    'xrp_hold': round(self.xrp_hold, 6),
                    'xrp_value': round(xrp_value, 2),
                    'total_equity': round(total_equity, 2),
                    'grid_center': round(self.grid_center, 4),
                    'atr': round(atr, 6),
                    'grid_levels': len(self.grid),
                })

        # Cálculo final: valorar XRP a último precio
        final_price = candle_rows[-1]['close']
        xrp_value = self.xrp_hold * final_price
        total_equity = self.balance_usd + xrp_value
        total_pnl = total_equity - self.cfg.initial_balance

        # También considerar PnL de compras en buy_queue (no vendidas)
        unrealized_pnl = 0.0
        for buy in self.buy_queue:
            unrealized_pnl += buy['qty'] * (final_price - buy['price'])

        summary = {
            'initial_balance': self.cfg.initial_balance,
            'final_balance_usd': round(self.balance_usd, 2),
            'xrp_hold': round(self.xrp_hold, 6),
            'xrp_value_usd': round(xrp_value, 2),
            'total_equity': round(total_equity, 2),
            'realized_pnl': round(total_pnl - unrealized_pnl, 4),
            'unrealized_pnl': round(unrealized_pnl, 4),
            'total_pnl': round(total_pnl, 2),
            'total_trades': self.total_trades,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate_pct': round(self.wins / max(1, self.wins + self.losses) * 100, 1),
            'grid_center_final': self.grid_center,
            'config': {k: v for k, v in asdict(self.cfg).items()},
            'outputs': {
                'decisions_log': str(dlog_path),
                'trades_log': str(tlog_path),
                'summary_json': str(summary_path),
            }
        }
        summary_path.write_text(json.dumps(summary, indent=2))
        return summary


def load_candle_rows(input_path: Path) -> List[dict]:
    rows = []
    with input_path.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'ts': r.get('timestamp_utc') or r.get('ts', ''),
                    'open': float(r.get('open', 0)),
                    'high': float(r.get('high', 0)),
                    'low': float(r.get('low', 0)),
                    'close': float(r.get('close', 0)),
                    'volume': float(r.get('volume', 0)),
                })
            except Exception:
                continue
    return rows


def main():
    p = argparse.ArgumentParser(description="XRP Grid Bot v2 — grid dinámico")
    p.add_argument('--input', required=True, help="CSV con OHLCV data")
    p.add_argument('--config', help="JSON config file (opcional)")
    p.add_argument('--output-dir', required=True, help="Directorio de salida")
    args = p.parse_args()

    cfg = GridConfig()
    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.exists():
            d = json.loads(cfg_path.read_text())
            for k, v in d.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)

    rows = load_candle_rows(Path(args.input))
    if len(rows) < 30:
        raise ValueError(f"Need at least 30 candles, got {len(rows)}")

    bot = XrpGridBot(cfg, Path(args.output_dir))
    summary = bot.run_simulation(rows)

    print(f"XRP Grid Bot v2 — {cfg.symbol}")
    print(f"  Period: {rows[0]['ts']} → {rows[-1]['ts']}")
    print(f"  Grid center: ${summary['grid_center_final']:.4f}")
    print(f"  Result: ${summary['initial_balance']:.2f} → ${summary['total_equity']:.2f}")
    print(f"  PnL: ${summary['total_pnl']:.2f} (realized: ${summary['realized_pnl']:.4f})")
    print(f"  Trades: {summary['total_trades']} ({summary['wins']}W/{summary['losses']}L) WR: {summary['win_rate_pct']}%")
    print(f"  XRP hold: {summary['xrp_hold']:.4f} (${summary['xrp_value_usd']:.2f})")

    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
