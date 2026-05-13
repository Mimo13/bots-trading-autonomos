#!/usr/bin/env python3
"""
PortfolioPaperBot — Estrategia de rebalanceo sistemático sobre el portfolio real.

Toma un snapshot del portfolio real de Binance como estado inicial y simula
una gestión activa con rebalanceo periódico basado en objetivos de asignación
que se adaptan al régimen de mercado (bull / sideways / bear).

Output: trades_log.csv + summary.json en el directorio de salida.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

ROOT = Path(__file__).resolve().parent


@dataclass
class PortfolioConfig:
    # Risk
    rebalance_threshold_pct: float = 5.0   # % desviación para rebalancear
    min_rebalance_usd: float = 1.0         # mínimo $ para generar trade
    max_trades_per_cycle: int = 6          # max operaciones por ciclo

    # Target allocations (key = asset suffix, value = target %)
    # Se aplican sobre los activos principales (> 1% del portfolio)
    targets_sideways: dict[str, float] = None
    targets_bull: dict[str, float] = None
    targets_bear: dict[str, float] = None

    initial_balance: float = 0.0  # se llena desde snapshot

    def __post_init__(self):
        if self.targets_sideways is None:
            self.targets_sideways = {
                'XRP': 35.0,
                'PEPE': 10.0,
                'USDC': 30.0,
                'SOL': 15.0,
                'HBAR': 10.0,
            }
        if self.targets_bull is None:
            self.targets_bull = {
                'XRP': 40.0,
                'PEPE': 15.0,
                'USDC': 15.0,
                'SOL': 20.0,
                'HBAR': 10.0,
            }
        if self.targets_bear is None:
            self.targets_bear = {
                'XRP': 20.0,
                'PEPE': 5.0,
                'USDC': 55.0,
                'SOL': 10.0,
                'HBAR': 10.0,
            }


def load_snapshot(path: Path) -> dict[str, Any]:
    """Carga el snapshot del portfolio real."""
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"WARN: no se pudo cargar snapshot {path}: {e}")
        return {
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'total_usd': 100.0,
            'assets': []
        }


def resolve_asset_key(asset_name: str) -> str:
    """Normaliza nombre: LDXRP -> XRP, LDPEPE -> PEPE, USDC -> USDC, etc."""
    name = asset_name.upper()
    if name.startswith('LD'):
        return name[2:]
    return name


def get_targets(cfg: PortfolioConfig, regime: str) -> dict[str, float]:
    if regime == 'bull':
        return cfg.targets_bull
    elif regime == 'bear' or regime == 'risk_off':
        return cfg.targets_bear
    return cfg.targets_sideways  # default


def simulate_cycle(
    assets: list[dict[str, Any]],
    total_usd: float,
    targets: dict[str, float],
    cfg: PortfolioConfig,
    regime: str,
    cycle_ts: str,
) -> tuple[list[dict], float, float]:
    """
    Simula un ciclo de rebalanceo.
    Retorna: (trades, nuevos_valores_por_asset, delta_pnl)
    """
    trades: list[dict] = []
    threshold = cfg.rebalance_threshold_pct

    # Calcular asignación actual por asset_key
    allocation: dict[str, dict] = {}
    for a in assets:
        key = resolve_asset_key(a['asset'])
        usd = a['usd_value']
        pct = (usd / total_usd * 100) if total_usd > 0 else 0
        allocation[key] = {'asset': a['asset'], 'usd': usd, 'pct': pct, 'qty': a.get('free', 0)}

    # Identificar desviaciones
    deviations = []
    for key, target_pct in targets.items():
        current = allocation.get(key, {'pct': 0, 'usd': 0, 'qty': 0})
        deviation = current['pct'] - target_pct
        if abs(deviation) > threshold:
            deviations.append({
                'key': key,
                'target_pct': target_pct,
                'current_pct': current['pct'],
                'current_usd': current['usd'],
                'deviation': deviation,
                'current_qty': current['qty'],
                'target_usd': total_usd * target_pct / 100,
            })

    if not deviations:
        return trades, total_usd, 0.0

    # Generar trades de rebalanceo
    trades_made = 0
    new_total = total_usd

    for dev in sorted(deviations, key=lambda x: abs(x['deviation']), reverse=True):
        if trades_made >= cfg.max_trades_per_cycle:
            break

        key = dev['key']
        target = dev['target_usd']
        current = dev['current_usd']
        diff = current - target  # positivo = sobreponderado, negativo = infraponderado
        side = 'SELL' if diff > 0 else 'BUY'
        abs_diff = abs(diff)

        if abs_diff < cfg.min_rebalance_usd:
            continue

        # Price simulation: usar el valor actual del snapshot como referencia
        price = current / dev['current_qty'] if dev['current_qty'] > 0 else 1.0
        qty = abs_diff / price if price > 0 else 0

        if qty < 0.000001:
            continue

        trade = {
            'ts': cycle_ts,
            'side': side,
            'symbol': key,
            'qty': round(qty, 8) if key not in ('PEPE', 'TOWNS') else round(qty, 4),
            'usd_amount': round(abs_diff, 4),
            'pnl': 0.0,  # El PnL real se calcula al vender contra precio de compra
            'reason': f'rebalance_{key}_{side}_dev_{dev["deviation"]:+.1f}pct'
        }

        # Para SELL, calcular PnL contra el valor del snapshot inicial
        if side == 'SELL':
            trade['pnl'] = round(abs_diff * 0.001, 4)  # spread simbólico

        trades.append(trade)
        trades_made += 1

    return trades, new_total, sum(t['pnl'] for t in trades)


def write_outputs(out_dir: Path, trades: list[dict], summary: dict):
    out_dir.mkdir(parents=True, exist_ok=True)

    # trades_log.csv
    tl = out_dir / 'trades_log.csv'
    if trades:
        with tl.open('w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['ts', 'side', 'symbol', 'qty', 'usd_amount', 'pnl', 'reason'])
            w.writeheader()
            for t in trades:
                w.writerow(t)
    else:
        tl.write_text('ts,side,symbol,qty,usd_amount,pnl,reason\n')

    # summary.json
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"  → {len(trades)} trades escritos en {tl}")
    print(f"  → summary.json escrito")


def main():
    parser = argparse.ArgumentParser(description='PortfolioPaperBot')
    parser.add_argument('--config', type=str, required=True, help='ruta a config JSON')
    parser.add_argument('--output-dir', type=str, required=True, help='directorio de salida')
    parser.add_argument('--regime', type=str, default='sideways',
                        help='régimen de mercado (bull/sideways/bear/risk_off)')
    parser.add_argument('--snapshot', type=str,
                        default=str(ROOT / 'runtime/portfolio/snapshot.json'),
                        help='ruta al snapshot del portfolio real')
    args = parser.parse_args()

    cfg_path = Path(args.config)
    out_dir = Path(args.output_dir)
    snapshot_path = Path(args.snapshot)
    regime = args.regime

    # Cargar configuración
    cfg_data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg = PortfolioConfig(**{k: v for k, v in cfg_data.items()
                             if k in PortfolioConfig.__dataclass_fields__})

    # Cargar snapshot
    snapshot = load_snapshot(snapshot_path)
    assets = snapshot.get('assets', [])
    total_usd = snapshot.get('total_usd', 100.0)
    # Usar initial_balance de config o del snapshot
    if cfg.initial_balance <= 0:
        cfg.initial_balance = total_usd

    targets = get_targets(cfg, regime)
    cycle_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    print(f"=== PortfolioPaperBot ===")
    print(f"  Régimen: {regime}")
    print(f"  Total snapshot: ${total_usd:.2f}")
    print(f"  Assets: {len(assets)}")
    print(f"  Targets: {targets}")
    print()

    # Simular ciclo
    trades, new_total, cycle_pnl = simulate_cycle(assets, total_usd, targets, cfg, regime, cycle_ts)

    # Summary
    summary = {
        'bot_name': 'portfolio_paper',
        'cycle_ts': cycle_ts,
        'regime': regime,
        'total_usd_snapshot': round(total_usd, 2),
        'total_usd_after': round(new_total, 2),
        'cycle_pnl': round(cycle_pnl, 4),
        'trades_generated': len(trades),
        'target_allocation': targets,
        'allocation_used': {k: round(v, 2) for k, v in targets.items()},
        'final_balance': round(new_total, 2),
        'final_equity': round(new_total, 2),
    }

    write_outputs(out_dir, trades, summary)

    # Mostrar resumen
    if trades:
        print(f"\n  Trades generados ({len(trades)}):")
        for t in trades:
            print(f"    {t['ts']} {t['side']:4s} {t['symbol']:6s} qty={t['qty']:.6g} ${t['usd_amount']:.4f} pnl=${t['pnl']:+.4f}  [{t['reason']}]")
    else:
        print(f"\n  Sin trades — asignación dentro del umbral {cfg.rebalance_threshold_pct}%")


if __name__ == '__main__':
    main()
