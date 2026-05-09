#!/usr/bin/env python3
"""Unified runner — cada bot con sus propios $100 iniciales."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tradingview_bridge import write_ctrader_signal, enrich_polymarket_csv

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
CTRADER_SIGNAL = ROOT / 'runtime/tradingview/ctrader_signal.csv'
LIVE_FEED = ROOT / 'runtime/live/SOLUSDT_5m.csv'  # Live data from Binance
POLY_BASE = ROOT / 'runtime/polymarket/polymarket_base_input.csv'
POLY_ENRICHED = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'
POLY_CONFIG = ROOT / 'polymarket_paper_config.example.json'
PORTFOLIO_CONFIG = ROOT / 'polymarket_portfolio_config.json'
FABIAN_CONFIG = ROOT / 'fabian_config_crypto.json'
FABIANPRO_CONFIG = ROOT / 'fabian_pro_config.json'
TURTLE_CONFIG = ROOT / 'turtle_bot_config.json'
POLY_RUNS_DIR = ROOT / 'runtime/polymarket/runs'
XRP_GRID_CONFIG = ROOT / 'xrp_grid_config.json'
PYTHON = str(ROOT / '.venv/bin/python')

INITIAL_BALANCE = 100.0


def run_bot(cmd: list, name: str, balance_key: str, status: dict) -> dict:
    """Run a bot with the given command, return its summary dict or {}."""
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if cp.returncode == 0:
        status[f'{name}_simulated'] = True
        status['notes'].append(f'{name} run ok')
        try:
            return json.loads(cp.stdout)
        except Exception:
            status['notes'].append(f'{name} parse failed')
    else:
        status['notes'].append(f'{name} failed: {cp.stderr.strip()[:150]}')
    return {}


def run() -> dict:
    status = {
        'ts_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'ctrader_signal_written': False,
        'notes': [],
    }

    # cTrader signal (always runs, independent)
    try:
        write_ctrader_signal(CTRADER_SIGNAL, 'EURUSD', 'OANDA:EURUSD')
        status['ctrader_signal_written'] = True
    except Exception as e:
        status['notes'].append(f'ctrader signal failed: {str(e)[:80]}')

    # Use live data feed first, then fall back to polymarket input
    poly_input = LIVE_FEED if LIVE_FEED.exists() else (POLY_ENRICHED if POLY_ENRICHED.exists() else POLY_BASE)
    if not poly_input.exists():
        status['notes'].append(f'no input data: {poly_input}')
        return status

    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    # 1. PolyKronosPaper — apuestas binarias
    cfg = json.loads(POLY_CONFIG.read_text())
    cfg['initial_equity'] = INITIAL_BALANCE
    tmp = POLY_RUNS_DIR / f'_cfg_poly_{ts}.json'
    tmp.write_text(json.dumps(cfg))
    out = POLY_RUNS_DIR / ts
    cmd = [PYTHON, str(ROOT / 'polymarket_paper_bot.py'),
           '--input', str(poly_input), '--config', str(tmp), '--output-dir', str(out)]
    s = run_bot(cmd, 'poly', 'final_equity', status)
    if s: status['poly_summary'] = s
    if tmp.exists(): tmp.unlink()

    # 2. PolyPortfolioPaper — cartera
    cfg2 = json.loads(PORTFOLIO_CONFIG.read_text())
    cfg2['initial_balance'] = INITIAL_BALANCE
    tmp2 = POLY_RUNS_DIR / f'_cfg_port_{ts}.json'
    tmp2.write_text(json.dumps(cfg2))
    out2 = POLY_RUNS_DIR / f'portfolio_{ts}'
    cmd2 = [PYTHON, str(ROOT / 'polymarket_portfolio_bot.py'),
            '--input', str(poly_input), '--config', str(tmp2), '--output-dir', str(out2)]
    s2 = run_bot(cmd2, 'portfolio', 'final_balance', status)
    if s2: status['portfolio_summary'] = s2
    if tmp2.exists(): tmp2.unlink()

    # 3. FabiánPullback Python — estructura/ruptura
    cfg3 = json.loads(FABIAN_CONFIG.read_text())
    cfg3['initial_balance'] = INITIAL_BALANCE
    tmp3 = POLY_RUNS_DIR / f'_cfg_fabian_{ts}.json'
    tmp3.write_text(json.dumps(cfg3))
    out3 = POLY_RUNS_DIR / f'fabian_{ts}'
    cmd3 = [PYTHON, str(ROOT / 'fabian_pullback_bot.py'),
            '--input', str(poly_input), '--config', str(tmp3), '--output-dir', str(out3)]
    s3 = run_bot(cmd3, 'fabian', 'final_balance', status)
    if s3: status['fabian_summary'] = s3
    if tmp3.exists(): tmp3.unlink()

    # 4. FabianPro — estructura mejorada + ADX/ATR + cartera
    cfg4 = json.loads(FABIANPRO_CONFIG.read_text())
    cfg4['initial_balance'] = INITIAL_BALANCE
    tmp4 = POLY_RUNS_DIR / f'_cfg_fabianpro_{ts}.json'
    tmp4.write_text(json.dumps(cfg4))
    out4 = POLY_RUNS_DIR / f'fabianpro_{ts}'
    cmd4 = [PYTHON, str(ROOT / 'fabian_pro_bot.py'),
            '--input', str(poly_input), '--config', str(tmp4), '--output-dir', str(out4)]
    s4 = run_bot(cmd4, 'fabianpro', 'final_balance', status)
    if s4: status['fabianpro_summary'] = s4
    if tmp4.exists(): tmp4.unlink()

    # 5. TurtleBot — Donchian breakout + piramidación + múltiples activos
    turtle_inputs = []
    turtle_symbols = []
    for sym in ['SOL', 'ADA']:
        f = ROOT / f'runtime/live/{sym}USDT_5m.csv'
        if f.exists():
            turtle_inputs.append(str(f))
            turtle_symbols.append(sym)
    if turtle_inputs:
        out5 = POLY_RUNS_DIR / f'turtle_{ts}'
        cmd5 = [PYTHON, str(ROOT / 'turtle_bot.py'), '--input'] + turtle_inputs + \
               ['--symbols'] + turtle_symbols + \
               ['--config', str(TURTLE_CONFIG), '--output-dir', str(out5)]
        cp5 = subprocess.run(cmd5, capture_output=True, text=True, timeout=90)
        if cp5.returncode == 0:
            status['turtle_simulated'] = True
            status['notes'].append('turtle run ok')
            try:
                s5 = json.loads(cp5.stdout)
                status['turtle_summary'] = s5
            except Exception:
                status['notes'].append('turtle parse failed')
        else:
            status['notes'].append(f'turtle failed: {cp5.stderr.strip()[:150]}')

    # 6. XRP Grid Bot — cuadrícula dinámica asistida por IA
    xrp_feed = ROOT / 'runtime/live/XRPUSDT_5m.csv'
    if xrp_feed.exists():
        out6 = POLY_RUNS_DIR / f'xrp_grid_{ts}'
        cfg6 = json.loads(XRP_GRID_CONFIG.read_text())
        cfg6['initial_balance'] = INITIAL_BALANCE
        tmp6 = POLY_RUNS_DIR / f'_cfg_xrp_{ts}.json'
        tmp6.write_text(json.dumps(cfg6))
        cmd6 = [PYTHON, str(ROOT / 'xrp_grid_bot.py'),
                '--input', str(xrp_feed), '--config', str(tmp6), '--output-dir', str(out6)]
        s6 = run_bot(cmd6, 'xrp_grid', 'final_balance', status)
        if s6: status['xrp_grid_summary'] = s6
        if tmp6.exists(): tmp6.unlink()

    # 7. AI Grid Advisor (recalcular cuadrícula)
    try:
        subprocess.Popen([PYTHON, str(ROOT / 'ai_grid_advisor.py')], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status['notes'].append('grid advisor started')
    except Exception as e:
        status['notes'].append(f'grid advisor error: {str(e)[:80]}')

    # 8. MTF Regime Bot — confluencia régimen + pullback
    tmp8 = POLY_RUNS_DIR / f'_cfg_mtfreg_{ts}.json'
    cfg8 = json.loads((ROOT / 'mtf_regime_config.json').read_text())
    cfg8['initial_balance'] = INITIAL_BALANCE
    tmp8.write_text(json.dumps(cfg8))
    out8 = POLY_RUNS_DIR / f'mtfreg_{ts}'
    cmd8 = [PYTHON, str(ROOT / 'mtf_regime_bot.py'),
            '--input', str(poly_input), '--config', str(tmp8), '--output-dir', str(out8)]
    s8 = run_bot(cmd8, 'mtfreg', 'final_balance', status)
    if s8: status['mtfreg_summary'] = s8
    if tmp8.exists(): tmp8.unlink()

    # 9. Box Breakout Bot — ruptura de caja
    tmp9 = POLY_RUNS_DIR / f'_cfg_boxbr_{ts}.json'
    cfg9 = json.loads((ROOT / 'box_breakout_config.json').read_text())
    cfg9['initial_balance'] = INITIAL_BALANCE
    tmp9.write_text(json.dumps(cfg9))
    out9 = POLY_RUNS_DIR / f'boxbr_{ts}'
    cmd9 = [PYTHON, str(ROOT / 'box_breakout_bot.py'),
            '--input', str(poly_input), '--config', str(tmp9), '--output-dir', str(out9)]
    s9 = run_bot(cmd9, 'boxbr', 'final_balance', status)
    if s9: status['boxbr_summary'] = s9
    if tmp9.exists(): tmp9.unlink()

    # 10. Scalping 5m Bot — momentum + estructura + hard kills
    tmp10 = POLY_RUNS_DIR / f'_cfg_scalp_{ts}.json'
    cfg10 = json.loads((ROOT / 'scalping_5m_config.json').read_text())
    cfg10['initial_balance'] = INITIAL_BALANCE
    tmp10.write_text(json.dumps(cfg10))
    out10 = POLY_RUNS_DIR / f'scalp_{ts}'
    cmd10 = [PYTHON, str(ROOT / 'scalping_5m_bot.py'),
            '--input', str(poly_input), '--config', str(tmp10), '--output-dir', str(out10)]
    s10 = run_bot(cmd10, 'scalp', 'final_balance', status)
    if s10: status['scalp_summary'] = s10
    if tmp10.exists(): tmp10.unlink()

    # Obsidian log (optional)
    subprocess.run(['python3', str(ROOT / 'update_obsidian_trading_log.py')],
                   capture_output=True, timeout=10)

    status_path = ROOT / 'runtime/polymarket/last_runner_status.json'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2))
    return status


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
