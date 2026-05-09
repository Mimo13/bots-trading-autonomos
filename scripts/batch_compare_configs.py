#!/usr/bin/env python3
"""
Batch runner: prueba múltiples configs de portfolio bot y PolyKronos,
compara resultados y reporta cuál funciona mejor.
"""
from __future__ import annotations

import csv, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
POLY_INPUT = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'
RUNS_DIR = ROOT / 'runtime/polymarket/runs'
POLY_BOT = ROOT / 'polymarket_paper_bot.py'
PORTFOLIO_BOT = ROOT / 'polymarket_portfolio_bot.py'
PYTHON = str(ROOT / '.venv/bin/python')

configs = {
    # === PolyKronos variants ===
    'poly_high_edge': {
        'type': 'poly',
        'file': ROOT / 'polymarket_paper_config.example.json',
        'patch': {'edge_min': 0.06},
        'desc': 'PolyKronos: edge_min 0.06 (señales muy fuertes)'
    },
    'poly_low_edge': {
        'type': 'poly',
        'file': ROOT / 'polymarket_paper_config.example.json',
        'patch': {'edge_min': 0.001, 'atr_min_ratio': 0.00001, 'kelly_fraction': 0.3},
        'desc': 'PolyKronos: edge_min 0.001, riesgo reducido'
    },
    # === Portfolio variants ===
    'port_aggressive': {
        'type': 'portfolio',
        'file': ROOT / 'polymarket_portfolio_config.json',
        'patch': {'risk_per_trade': 0.50, 'take_profit_pct': 0.03, 'stop_loss_pct': 0.02, 'edge_min': 0.05},
        'desc': 'Portfolio Agresivo: 50% por trade, TP 3%, SL 2%'
    },
    'port_accumulator': {
        'type': 'portfolio',
        'file': ROOT / 'polymarket_portfolio_config.json',
        'patch': {'buy_threshold': 0.50, 'sell_threshold': 0.40, 'sell_on_reversal': False, 'take_profit_pct': 0.04, 'stop_loss_pct': 0.025},
        'desc': 'Portfolio Acumulador: compra en señales suaves, vende solo en fuertes'
    },
    'port_conservative': {
        'type': 'portfolio',
        'file': ROOT / 'polymarket_portfolio_config.json',
        'patch': {'risk_per_trade': 0.10, 'edge_min': 0.08, 'take_profit_pct': 0.05, 'stop_loss_pct': 0.02},
        'desc': 'Portfolio Conservador: 10% por trade, edge 0.08'
    },
    'port_high_volume': {
        'type': 'portfolio',
        'file': ROOT / 'polymarket_portfolio_config.json',
        'patch': {'risk_per_trade': 0.75, 'buy_threshold': 0.52, 'sell_threshold': 0.48, 'take_profit_pct': 0.015, 'stop_loss_pct': 0.01, 'edge_min': 0.01},
        'desc': 'Portfolio High Volume: 75% por trade, SL/TP ajustados'
    },
}


def run_poly(config_file: Path, patch: dict, run_id: str) -> dict:
    """Run poly bot with patched config."""
    cfg = json.loads(config_file.read_text())
    cfg.update(patch)
    tmp_cfg = RUNS_DIR / f'_tmp_{run_id}.json'
    tmp_cfg.write_text(json.dumps(cfg, indent=2))
    out_dir = RUNS_DIR / f'batch_{run_id}'
    cmd = [PYTHON, str(POLY_BOT), '--input', str(POLY_INPUT), '--config', str(tmp_cfg), '--output-dir', str(out_dir)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if tmp_cfg.exists(): tmp_cfg.unlink()
    if cp.returncode != 0:
        return {'error': cp.stderr[:300]}
    return json.loads(cp.stdout)


def run_portfolio(config_file: Path, patch: dict, run_id: str) -> dict:
    """Run portfolio bot with patched config."""
    cfg = json.loads(config_file.read_text())
    cfg.update(patch)
    tmp_cfg = RUNS_DIR / f'_tmp_{run_id}.json'
    tmp_cfg.write_text(json.dumps(cfg, indent=2))
    out_dir = RUNS_DIR / f'batch_{run_id}'
    cmd = [PYTHON, str(PORTFOLIO_BOT), '--input', str(POLY_INPUT), '--config', str(tmp_cfg), '--output-dir', str(out_dir)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if tmp_cfg.exists(): tmp_cfg.unlink()
    if cp.returncode != 0:
        return {'error': cp.stderr[:300]}
    return json.loads(cp.stdout)


def main():
    results = []
    for name, cfg in configs.items():
        print(f"\n{'='*50}")
        print(f"▶ Ejecutando: {cfg['desc']}")
        print(f"{'='*50}")
        try:
            if cfg['type'] == 'poly':
                summary = run_poly(cfg['file'], cfg['patch'], name)
            else:
                summary = run_portfolio(cfg['file'], cfg['patch'], name)
            
            if 'error' in summary:
                print(f"  ❌ Error: {summary['error']}")
                results.append((name, cfg['desc'], 'ERROR', 0, 0, 0, 0))
            else:
                if cfg['type'] == 'poly':
                    tr = summary.get('total_trades', 0)
                    wr = summary.get('win_rate_percent', 0)
                    pnl = summary.get('total_pnl', 0)
                    dd = summary.get('max_drawdown_percent', 0)
                    print(f"  Trades: {tr} | Win rate: {wr}% | PnL: ${pnl} | DD: {dd}%")
                    results.append((name, cfg['desc'], 'poly', tr, wr, pnl, dd))
                else:
                    tr = summary.get('total_buys', 0) + summary.get('total_sells', 0)
                    wr = summary.get('sell_win_rate_percent', 0)
                    pnl = summary.get('total_pnl', 0)
                    dd = summary.get('max_drawdown_percent', 0)
                    print(f"  Trades: {summary.get('total_buys',0)}B/{summary.get('total_sells',0)}S | Sell WR: {wr}% | PnL: ${pnl} | DD: {dd}%")
                    results.append((name, cfg['desc'], 'port', tr, wr, pnl, dd))
        except Exception as e:
            print(f"  ❌ Excepción: {e}")
            results.append((name, cfg['desc'], 'ERROR', 0, 0, 0, 0))

    # Final comparison table
    print(f"\n\n{'='*60}")
    print("📊 COMPARATIVA FINAL")
    print(f"{'='*60}")
    print(f"{'Variante':<25} {'Trades':>8} {'WR%':>6} {'PnL$':>10} {'DD%':>6}")
    print("-"*60)
    for name, desc, typ, tr, wr, pnl, dd in sorted(results, key=lambda x: abs(float(x[4])), reverse=True):
        label = desc[:24]
        print(f"{label:<25} {tr:>8} {wr:>6} {pnl:>10} {dd:>6}")


if __name__ == '__main__':
    main()
