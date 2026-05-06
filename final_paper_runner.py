#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tradingview_bridge import write_ctrader_signal, enrich_polymarket_csv

ROOT = Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')

CTRADER_SIGNAL = ROOT / 'runtime/tradingview/ctrader_signal.csv'
POLY_BASE = ROOT / 'runtime/polymarket/polymarket_base_input.csv'
POLY_ENRICHED = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'
POLY_CONFIG = ROOT / 'polymarket_paper_config.example.json'
POLY_RUNS_DIR = ROOT / 'runtime/polymarket/runs'


def run() -> dict:
    status = {
        'ts_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'ctrader_signal_written': False,
        'polymarket_enriched': False,
        'polymarket_simulated': False,
        'notes': [],
    }

    write_ctrader_signal(
        output_csv=CTRADER_SIGNAL,
        ctrader_symbol='EURUSD',
        tv_exchange_symbol='OANDA:EURUSD',
        interval='5m',
    )
    status['ctrader_signal_written'] = True

    if POLY_BASE.exists():
        enrich_polymarket_csv(
            input_csv=POLY_BASE,
            output_csv=POLY_ENRICHED,
            interval='5m',
        )
        status['polymarket_enriched'] = True

        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        out_dir = POLY_RUNS_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            'python3',
            str(ROOT / 'polymarket_paper_bot.py'),
            '--input',
            str(POLY_ENRICHED),
            '--config',
            str(POLY_CONFIG),
            '--output-dir',
            str(out_dir),
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode == 0:
            status['polymarket_simulated'] = True
            status['notes'].append('polymarket run ok')
            try:
                summary = json.loads(cp.stdout)
                status['polymarket_summary'] = summary
            except Exception:
                status['notes'].append('summary parse failed')
        else:
            status['notes'].append(f'polymarket run failed: {cp.stderr.strip()}')
    else:
        status['notes'].append(f'base polymarket input not found: {POLY_BASE}')

    # Update Obsidian tracking sheet (fallback logging)
    obs_cmd = ['python3', str(ROOT / 'update_obsidian_trading_log.py')]
    obs = subprocess.run(obs_cmd, capture_output=True, text=True)
    if obs.returncode == 0:
        status['obsidian_updated'] = True
    else:
        status['obsidian_updated'] = False
        status['notes'].append(f'obsidian update failed: {obs.stderr.strip()}')

    status_path = ROOT / 'runtime/polymarket/last_runner_status.json'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2), encoding='utf-8')
    return status


if __name__ == '__main__':
    result = run()
    print(json.dumps(result, indent=2))
