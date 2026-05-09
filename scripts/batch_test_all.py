#!/usr/bin/env python3
"""
Batch test: runs portfolio bot on all asset/config combinations.
Creates configs optimized for each asset's volatility.
"""
from __future__ import annotations

import csv, json, subprocess, sys
from pathlib import Path

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
RUNS_DIR = ROOT / 'runtime/polymarket/runs'
BOT = ROOT / 'polymarket_portfolio_bot.py'
PYTHON = str(ROOT / '.venv/bin/python')

BASE_CFG = json.loads((ROOT / 'polymarket_portfolio_config.json').read_text())

# Asset data files
assets = {
    'sol_5m': {'file': 'polymarket_sol_5m.csv', 'vol': 0.46},
    'doge_5m': {'file': 'polymarket_doge_5m.csv', 'vol': 1.53},
    'link_5m': {'file': 'polymarket_link_5m.csv', 'vol': 0.61},
    'ada_5m': {'file': 'polymarket_ada_5m.csv', 'vol': 0.31},
    'btc_1h': {'file': 'polymarket_btc_1h.csv', 'vol': 0.08},
    'eth_1h': {'file': 'polymarket_eth_1h.csv', 'vol': 0.10},
}

# Strategy configurations
# (label, edge_min, buy_thresh, sell_thresh, risk, tp_pct, sl_pct, hold_candles, sell_on_rev)
strategies = [
    ('conservative', 0.06, 0.58, 0.42, 0.15, None, None, None, True),
    ('moderate',     0.04, 0.55, 0.45, 0.25, None, None, None, True),
    ('aggressive',   0.02, 0.52, 0.48, 0.40, None, None, None, True),
    ('tp_sl',        0.03, 0.55, 0.45, 0.25, None, None, None, True),
]


def make_config(asset_name: str, asset_vol: float, strat: tuple) -> dict:
    """Create config tuned for asset volatility + strategy."""
    label, edge, buy, sell, risk, tp_pct, sl_pct, hold, rev = strat
    cfg = dict(BASE_CFG)
    
    # Scale TP/SL to asset volatility: TP = 2x avg candle move, SL = 1.5x avg move
    vol_pct = asset_vol / 100.0  # Convert to decimal
    tp = vol_pct * 2.0
    sl = vol_pct * 1.5
    hold_candles = max(3, int(1.0 / vol_pct)) if vol_pct > 0 else 20
    hold_candles = min(hold_candles, 30)
    
    cfg.update({
        'edge_min': edge,
        'buy_threshold': buy,
        'sell_threshold': sell,
        'risk_per_trade': risk,
        'take_profit_pct': round(tp, 6),
        'stop_loss_pct': round(sl, 6),
        'max_hold_candles': hold_candles,
        'sell_on_reversal': rev,
        'sell_fraction_on_signal': 1.0,
    })
    return cfg


def run_test(asset_name: str, strat_label: str, cfg: dict) -> dict:
    """Run portfolio bot and return summary."""
    input_path = ROOT / 'runtime/polymarket' / assets[asset_name]['file']
    run_id = f'batch_{asset_name}_{strat_label}'
    out_dir = RUNS_DIR / run_id
    
    # Write temp config
    tmp_cfg = RUNS_DIR / f'_cfg_{run_id}.json'
    tmp_cfg.write_text(json.dumps(cfg, indent=2))
    
    cmd = [PYTHON, str(BOT), '--input', str(input_path),
           '--config', str(tmp_cfg), '--output-dir', str(out_dir)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if tmp_cfg.exists(): tmp_cfg.unlink()
    
    if cp.returncode != 0:
        return {'error': cp.stderr[:300]}
    
    try:
        return json.loads(cp.stdout)
    except Exception:
        return {'error': f'JSON parse: {cp.stdout[:200]}'}


def main():
    results = []
    
    for asset_name, info in assets.items():
        vol = info['vol']
        print(f"\n{'='*55}")
        print(f"📊 {asset_name.upper()} (vol: {vol}%/5m)")
        print(f"{'='*55}")
        
        for strat in strategies:
            strat_label = strat[0]
            cfg = make_config(asset_name, vol, strat)
            print(f"  ▶ {strat_label}: TP={cfg['take_profit_pct']:.4f} SL={cfg['stop_loss_pct']:.4f} hold={cfg['max_hold_candles']} risk={cfg['risk_per_trade']:.0%}", end='')
            
            summary = run_test(asset_name, strat_label, cfg)
            if 'error' in summary:
                print(f" ❌ {summary['error'][:80]}")
                continue
            
            buys = summary.get('total_buys', 0)
            sells = summary.get('total_sells', 0)
            pnl = summary.get('total_pnl', 0)
            eq = summary.get('final_equity', 0)
            wr = summary.get('sell_win_rate_percent', 0)
            dd = summary.get('max_drawdown_percent', 0)
            
            print(f" → {buys}B/{sells}S | PnL=\${pnl} | Eq=\${eq} | WR={wr}% | DD={dd}%")
            
            results.append((asset_name, strat_label, vol, buys, sells, pnl, eq, wr, dd))
    
    # Final comparison
    print(f"\n\n{'='*70}")
    print("📊 COMPARATIVA FINAL — TODOS LOS ACTIVOS Y ESTRATEGIAS")
    print(f"{'='*70}")
    print(f"{'Activo':<12} {'Estrategia':<15} {'Trades':>8} {'PnL':>10} {'Eq':>10} {'WR%':>6} {'DD%':>6}")
    print("-"*70)
    
    # Sort by PnL descending
    for r in sorted(results, key=lambda x: x[5], reverse=True):
        asset, strat, vol, buys, sells, pnl, eq, wr, dd = r
        trades = f"{buys}B/{sells}S"
        label = f"{asset}_{strat}"[:26]
        print(f"{asset:<12} {strat:<15} {trades:>8} {pnl:>10.2f} {eq:>10.2f} {wr:>6.1f} {dd:>6.2f}")
    
    # Best performer
    best = max(results, key=lambda x: x[5])  # highest PnL
    print(f"\n🏆 Mejor resultado: {best[0]}_{best[1]} → PnL=\${best[5]:.2f} WR={best[7]:.1f}%")
    
    # Show best config
    best_asset, best_strat = best[0], best[1]
    for s in strategies:
        if s[0] == best_strat:
            cfg = make_config(best_asset, [a[2] for a in assets.items() if a[0]==best_asset][0], s)
            print(f"   Config: edge={cfg['edge_min']} risk={cfg['risk_per_trade']} TP={cfg['take_profit_pct']} SL={cfg['stop_loss_pct']} hold={cfg['max_hold_candles']}")
            break


if __name__ == '__main__':
    main()
