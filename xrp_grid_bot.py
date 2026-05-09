#!/usr/bin/env python3
"""XRP Grid Bot — estrategia de cuadrícula para XRP asistida por IA.
Sin stop loss: nunca cierra posición completa, solo ajusta niveles de grid.
La IA recalcula rangos óptimos cada N horas usando datos de mercado disponibles.
"""
from __future__ import annotations
import argparse, csv, json, logging, os, time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('xrp_grid')

ROOT = Path(__file__).resolve().parent


@dataclass
class GridConfig:
    initial_balance: float = 100.0
    initial_xrp: float = 0.0         # XRP inicial (en paper, 0)
    base_asset: str = 'XRP'
    quote_asset: str = 'USDT'
    symbol: str = 'XRPUSDT'

    # Grid dinámico — la IA puede redefinir estos rangos
    grid_min_price: float = 1.50
    grid_max_price: float = 4.00
    grid_levels: int = 10

    # Riesgo
    risk_per_trade_pct: float = 0.05  # 5% del balance por nivel
    max_xrp_hold_pct: float = 0.90    # nunca tener más del 90% del balance en XRP
    rebalance_interval_hours: int = 6  # cada cuánto la IA recalcula el grid

    # TradingView signal filter (opcional)
    tv_filter_enabled: bool = False
    tv_min_confidence: float = 0.40

    # IA grid advisor
    ai_advisor_enabled: bool = True
    ai_advisor_interval_hours: int = 6


@dataclass
class GridLevel:
    price: float
    side: str        # 'BUY' o 'SELL'
    filled: bool = False
    allocated_usd: float = 0.0
    allocated_xrp: float = 0.0


class XrpGridBot:
    def __init__(self, cfg: GridConfig, out_dir: Path):
        self.cfg = cfg
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.balance_usd = cfg.initial_balance
        self.xrp_hold = cfg.initial_xrp
        self.grid: list[GridLevel] = []
        self.trades: list[dict] = []
        self.decisions: list[dict] = []
        self.last_rebalance_ts = 0.0
        self.last_ai_run_ts = 0.0
        self.day_trades = 0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.last_reset_day = -1

    def _build_initial_grid(self):
        """Construir niveles de grid basados en configuración o IA."""
        step = (self.cfg.grid_max_price - self.cfg.grid_min_price) / max(1, self.cfg.grid_levels - 1)
        self.grid = []
        for i in range(self.cfg.grid_levels):
            price = self.cfg.grid_min_price + step * i
            if i < self.cfg.grid_levels // 2:
                self.grid.append(GridLevel(price=price, side='BUY'))
            else:
                self.grid.append(GridLevel(price=price, side='SELL'))

    def _needs_rebalance(self) -> bool:
        return (time.time() - self.last_rebalance_ts) > self.cfg.rebalance_interval_hours * 3600

    def _reset_day_if_needed(self, ts: str):
        if not ts:
            return
        try:
            d = datetime.fromisoformat(ts.replace('Z', '+00:00')).timetuple().tm_yday
        except Exception:
            return
        if d != self.last_reset_day:
            self.day_trades = 0
            self.last_reset_day = d

    def _allocate_trade(self) -> tuple[float, float]:
        """Calcular allocation para un nivel de grid."""
        alloc_usd = self.balance_usd * self.cfg.risk_per_trade_pct
        # No exceder max_xrp_hold_pct
        xrp_value = self.xrp_hold * (self.grid[0].price if self.grid else 0)
        if xrp_value > 0:
            current_xrp_pct = xrp_value / max(1, xrp_value + self.balance_usd)
            if current_xrp_pct >= self.cfg.max_xrp_hold_pct:
                return 0.0, 0.0  # No más compras
        return alloc_usd, alloc_usd

    def apply_ai_grid(self, config: dict):
        """Aplicar grid calculado por la IA."""
        if 'grid_min_price' in config:
            self.cfg.grid_min_price = config['grid_min_price']
        if 'grid_max_price' in config:
            self.cfg.grid_max_price = config['grid_max_price']
        if 'grid_levels' in config:
            self.cfg.grid_levels = config['grid_levels']
        self._build_initial_grid()
        self.last_rebalance_ts = time.time()
        logger.info(f"AI grid applied: ${self.cfg.grid_min_price:.2f} - ${self.cfg.grid_max_price:.2f} ({self.cfg.grid_levels} niveles)")

    def run_simulation(self, candle_rows: list[dict]) -> dict:
        """Ejecutar simulación sobre datos históricos."""
        from collections import defaultdict
        self._build_initial_grid()
        day_count = defaultdict(int)

        dlog_path = self.out_dir / 'decisions_log.csv'
        tlog_path = self.out_dir / 'trades_log.csv'

        with open(dlog_path, 'w', newline='') as df, open(tlog_path, 'w', newline='') as tf:
            dw = csv.DictWriter(df, fieldnames=['ts', 'price', 'action', 'reason', 'balance_usd', 'xrp_hold', 'grid_min', 'grid_max'])
            tw = csv.DictWriter(tf, fieldnames=['ts', 'side', 'price', 'qty', 'usd_amount', 'pnl', 'symbol'])
            dw.writeheader()
            tw.writeheader()

            for i, row in enumerate(candle_rows):
                ts = row.get('ts', '')
                price = float(row.get('close', 0))
                if price <= 0:
                    continue

                day = ts[:10] if ts else 'unknown'
                self._reset_day_if_needed(ts)

                # Verificar cada nivel del grid
                for level in self.grid:
                    if level.filled:
                        continue
                    if level.side == 'BUY' and price <= level.price and self.balance_usd > 1:
                        alloc_usd, _ = self._allocate_trade()
                        if alloc_usd <= 0:
                            continue
                        qty = alloc_usd / price
                        self.balance_usd -= alloc_usd
                        self.xrp_hold += qty
                        level.filled = True
                        level.allocated_usd = alloc_usd
                        level.allocated_xrp = qty
                        self.day_trades += 1
                        self.total_trades += 1
                        day_count[day] += 1
                        tw.writerow({'ts': ts, 'side': 'BUY', 'price': round(price, 6), 'qty': round(qty, 6),
                                     'usd_amount': round(alloc_usd, 2), 'pnl': 0, 'symbol': self.cfg.symbol})
                        dw.writerow({'ts': ts, 'price': round(price, 6), 'action': 'BUY',
                                     'reason': 'GRID_BUY', 'balance_usd': round(self.balance_usd, 2),
                                     'xrp_hold': round(self.xrp_hold, 6), 'grid_min': self.cfg.grid_min_price,
                                     'grid_max': self.cfg.grid_max_price})

                    elif level.side == 'SELL' and price >= level.price and self.xrp_hold > 0 and level.filled is not None:
                        # Vender solo si ese nivel fue comprado antes o si tenemos XRP
                        sell_qty = self.xrp_hold * 0.1  # vender 10% del hold por nivel
                        if sell_qty <= 0:
                            continue
                        usd_obtained = sell_qty * price
                        pnl_est = 0  # No calculamos PnL real porque no tenemos entry_price del grid
                        self.xrp_hold -= sell_qty
                        self.balance_usd += usd_obtained
                        level.filled = True
                        self.day_trades += 1
                        self.total_trades += 1
                        tw.writerow({'ts': ts, 'side': 'SELL', 'price': round(price, 6), 'qty': round(sell_qty, 6),
                                     'usd_amount': round(usd_obtained, 2), 'pnl': round(pnl_est, 4), 'symbol': self.cfg.symbol})
                        dw.writerow({'ts': ts, 'price': round(price, 6), 'action': 'SELL',
                                     'reason': 'GRID_SELL', 'balance_usd': round(self.balance_usd, 2),
                                     'xrp_hold': round(self.xrp_hold, 6), 'grid_min': self.cfg.grid_min_price,
                                     'grid_max': self.cfg.grid_max_price})

        # Valor total del portfolio
        current_price = candle_rows[-1]['close'] if candle_rows else 0
        xrp_value = self.xrp_hold * current_price
        total_equity = self.balance_usd + xrp_value

        summary = {
            'initial_balance': self.cfg.initial_balance,
            'final_balance_usd': round(self.balance_usd, 2),
            'xrp_hold': round(self.xrp_hold, 6),
            'xrp_value_usd': round(xrp_value, 2),
            'total_equity': round(total_equity, 2),
            'total_pnl': round(total_equity - self.cfg.initial_balance, 2),
            'total_trades': self.total_trades,
            'grid_min_price': self.cfg.grid_min_price,
            'grid_max_price': self.cfg.grid_max_price,
            'grid_levels': self.cfg.grid_levels,
            'config': asdict(self.cfg),
            'outputs': {
                'decisions_log': str(dlog_path),
                'trades_log': str(tlog_path),
            }
        }
        summary_path = self.out_dir / 'summary.json'
        summary_path.write_text(json.dumps(summary, indent=2))
        return summary


def load_candle_rows(input_path: Path) -> list[dict]:
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
                    'symbol': r.get('instrument') or r.get('symbol', 'XRPUSDT'),
                })
            except Exception:
                continue
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--config')
    p.add_argument('--output-dir', required=True)
    a = p.parse_args()

    cfg = GridConfig()
    if a.config:
        d = json.loads(Path(a.config).read_text())
        for k, v in d.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    rows = load_candle_rows(Path(a.input))
    bot = XrpGridBot(cfg, Path(a.output_dir))
    print(json.dumps(bot.run_simulation(rows), indent=2))


if __name__ == '__main__':
    main()
