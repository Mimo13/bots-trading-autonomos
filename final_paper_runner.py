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
FABIAN_SPOT_LONG_CONFIG = ROOT / 'fabian_spot_long_config.json'
FABIANPRO_CONFIG = ROOT / 'fabian_pro_config.json'
TURTLE_CONFIG = ROOT / 'turtle_bot_config.json'
POLY_RUNS_DIR = ROOT / 'runtime/polymarket/runs'
XRP_GRID_CONFIG = ROOT / 'xrp_grid_config.json'
BNB_SPOT_LONG_CONFIG = ROOT / 'bnb_spot_long_config.json'
BNB_GRID_CONFIG = ROOT / 'bnb_grid_config.json'
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
        'archived_skipped': ['fabian_py (→fabian_spot_long)', 'fabianpro', 'mtfreg', 'boxbr', 'scalp'],
    }

    # cTrader signal (always runs, independent)
    try:
        write_ctrader_signal(CTRADER_SIGNAL, 'EURUSD', 'OANDA:EURUSD')
        status['ctrader_signal_written'] = True
    except Exception as e:
        status['notes'].append(f'ctrader signal failed: {str(e)[:80]}')

    # Fetch live data for all bot symbols from Binance
    try:
        from data_fetcher import update_feed_file
        feed_symbols = ['SOLUSDT', 'XRPUSDT', 'BNBUSDC', 'ADAUSDT', 'DOGEUSDT', 'BTCUSDT']
        feed_dir = ROOT / 'runtime' / 'live'
        for sym in feed_symbols:
            try:
                p = update_feed_file(sym, '5m', feed_dir)
                if p:
                    status['notes'].append(f'feed {sym}: ok')
                else:
                    status['notes'].append(f'feed {sym}: no data')
            except Exception as fe:
                status['notes'].append(f'feed {sym}: {str(fe)[:60]}')
    except Exception as e:
        status['notes'].append(f'live data fetch: {str(e)[:80]}')

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

    # 2. PolyPortfolioPaper v2 — RSI-based (reactivado 2026-05-10)
    PFOLIO_CONFIG = ROOT / 'pfolio_config.json'
    tmp2 = POLY_RUNS_DIR / f'_cfg_pfolio_{ts}.json'
    if PFOLIO_CONFIG.exists():
        import shutil
        shutil.copy2(str(PFOLIO_CONFIG), str(tmp2))
    out2 = POLY_RUNS_DIR / f'pfolio_{ts}'
    cmd2 = [PYTHON, str(ROOT / 'polymarket_portfolio_bot.py'),
            '--input', str(poly_input), '--config', str(tmp2), '--output-dir', str(out2)]
    s2 = run_bot(cmd2, 'pfolio', 'final_balance', status)
    if s2: status['pfolio_summary'] = s2
    if tmp2.exists(): tmp2.unlink()

    # [ARCHIVADO 2026-05-11] FabiánPullback Python — estructura/ruptura con shorts
    # Reemplazado por FabianSpotLong (3b). Los shorts no son replicables en spot.
    # cfg3 = json.loads(FABIAN_CONFIG.read_text())
    # ...

    # 3b. FabianSpotLong — Fabián adaptado a spot long-only para comparar con SolPullback
    cfg3b = json.loads(FABIAN_SPOT_LONG_CONFIG.read_text())
    cfg3b['initial_balance'] = INITIAL_BALANCE
    cfg3b['spot_long_only'] = True
    tmp3b = POLY_RUNS_DIR / f'_cfg_fabian_spot_long_{ts}.json'
    tmp3b.write_text(json.dumps(cfg3b))
    out3b = POLY_RUNS_DIR / f'fabian_spot_long_{ts}'
    cmd3b = [PYTHON, str(ROOT / 'fabian_pullback_bot.py'),
             '--input', str(poly_input), '--config', str(tmp3b), '--output-dir', str(out3b)]
    s3b = run_bot(cmd3b, 'fabian_spot_long', 'final_balance', status)
    if s3b: status['fabian_spot_long_summary'] = s3b
    if tmp3b.exists(): tmp3b.unlink()

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
#     turtle_inputs = []
#     turtle_symbols = []
#     for sym in ['SOL', 'ADA']:
#         f = ROOT / f'runtime/live/{sym}USDT_5m.csv'
#         if f.exists():
#             turtle_inputs.append(str(f))
#             turtle_symbols.append(sym)
#     if turtle_inputs:
#         out5 = POLY_RUNS_DIR / f'turtle_{ts}'
#         cmd5 = [PYTHON, str(ROOT / 'turtle_bot.py'), '--input'] + turtle_inputs + \
#                ['--symbols'] + turtle_symbols + \
#                ['--config', str(TURTLE_CONFIG), '--output-dir', str(out5)]
#         cp5 = subprocess.run(cmd5, capture_output=True, text=True, timeout=90)
#         if cp5.returncode == 0:
#             status['turtle_simulated'] = True
#             status['notes'].append('turtle run ok')
#             try:
#                 s5 = json.loads(cp5.stdout)
#                 status['turtle_summary'] = s5
#             except Exception:
#                 status['notes'].append('turtle parse failed')
#         else:
#             status['notes'].append(f'turtle failed: {cp5.stderr.strip()[:150]}')

    # 5b. SolPullbackBot — pullback en SOL/USDT con RSI/ATR/EMA (4h, datos Binance)
    out5b = POLY_RUNS_DIR / f'sol_pb_{ts}'
    cmd5b = [PYTHON, str(ROOT / 'sol_pullback_bot.py'),
             '--output-dir', str(out5b), '--limit', '200']
    s5b = run_bot(cmd5b, 'sol_pb', 'final_balance', status)
    if s5b: status['sol_pb_summary'] = s5b

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

    # 6b. BnbSpotLongBot — Fabián spot long-only en BNB/USDC
    bnb_feed = ROOT / 'runtime/live/BNBUSDC_5m.csv'
    if bnb_feed.exists():
        cfg6b = json.loads(BNB_SPOT_LONG_CONFIG.read_text())
        cfg6b['initial_balance'] = INITIAL_BALANCE
        cfg6b['spot_long_only'] = True
        cfg6b['symbol'] = 'BNBUSDC'
        tmp6b = POLY_RUNS_DIR / f'_cfg_bnb_spot_long_{ts}.json'
        tmp6b.write_text(json.dumps(cfg6b))
        out6b = POLY_RUNS_DIR / f'bnb_spot_long_{ts}'
        cmd6b = [PYTHON, str(ROOT / 'fabian_pullback_bot.py'),
                 '--input', str(bnb_feed), '--config', str(tmp6b), '--output-dir', str(out6b)]
        s6b = run_bot(cmd6b, 'bnb_spot_long', 'final_balance', status)
        if s6b: status['bnb_spot_long_summary'] = s6b
        if tmp6b.exists(): tmp6b.unlink()

    # 6c. BNB Grid Bot — misma lógica grid dinámica que XRP Grid, en BNB/USDC
    if bnb_feed.exists():
        out6c = POLY_RUNS_DIR / f'bnb_grid_{ts}'
        cfg6c = json.loads(BNB_GRID_CONFIG.read_text())
        cfg6c['initial_balance'] = INITIAL_BALANCE
        cfg6c['symbol'] = 'BNBUSDC'
        tmp6c = POLY_RUNS_DIR / f'_cfg_bnb_grid_{ts}.json'
        tmp6c.write_text(json.dumps(cfg6c))
        cmd6c = [PYTHON, str(ROOT / 'xrp_grid_bot.py'),
                 '--input', str(bnb_feed), '--config', str(tmp6c), '--output-dir', str(out6c)]
        s6c = run_bot(cmd6c, 'bnb_grid', 'final_balance', status)
        if s6c: status['bnb_grid_summary'] = s6c
        if tmp6c.exists(): tmp6c.unlink()

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

    # Paper Fleet Orchestrator — update regime before Portfolio Mirror reads targets.
    try:
        cp_orch = subprocess.run([PYTHON, str(ROOT / 'scripts/bot_orchestrator.py')],
                                 cwd=ROOT, capture_output=True, text=True, timeout=30)
        status['orchestrator_ran'] = (cp_orch.returncode == 0)
        if cp_orch.returncode != 0:
            status['notes'].append(f'orchestrator error: {cp_orch.stderr.strip()[:150]}')
    except Exception as e:
        status['orchestrator_ran'] = False
        status['notes'].append(f'orchestrator exception: {str(e)[:120]}')

    # Portfolio Mirror Bot — rebalanceo sistemático sobre el portfolio real de Binance
    portfolio_runs = ROOT / 'runtime/portfolio/runs'
    portfolio_runs.mkdir(parents=True, exist_ok=True)
    out_pmb = portfolio_runs / f'portfolio_mirror_{ts}'
    cfg_pmb = ROOT / 'portfolio_paper_config.json'
    # Tomar régimen más reciente del orquestador si existe
    regime = 'sideways'
    state_path = ROOT / 'runtime/orchestrator/state.json'
    if state_path.exists():
        try:
            sd = json.loads(state_path.read_text())
            regime = sd.get('regime', {}).get('regime', 'sideways')
        except Exception:
            pass
    cmd_pmb = [PYTHON, str(ROOT / 'portfolio_paper_bot.py'),
               '--config', str(cfg_pmb),
               '--output-dir', str(out_pmb),
               '--regime', regime,
               '--snapshot', str(ROOT / 'runtime/portfolio/snapshot.json')]
    s_pmb = run_bot(cmd_pmb, 'portfolio_paper_bot', 'final_balance', status)
    if s_pmb: status['portfolio_mirror_summary'] = s_pmb

    # Obsidian log (optional)
    subprocess.run(['python3', str(ROOT / 'update_obsidian_trading_log.py')],
                   capture_output=True, timeout=10)

    status_path = ROOT / 'runtime/polymarket/last_runner_status.json'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2))
    return status


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
