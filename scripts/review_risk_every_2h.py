#!/usr/bin/env python3
"""
Revisión cada 2h de bots con ajuste inteligente.
Analiza win rate, PnL, bloqueos de señal, y optimiza parámetros.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
CONFIG_PATH = ROOT / 'polymarket_paper_config.example.json'
REVIEW_LOG = ROOT / 'runtime/logs/review_risk_2h.log'
LAST_RUNS = ROOT / 'runtime/polymarket/runs'
RISK_TIER_FILE = ROOT / 'runtime/logs/.risk_tier'
BEST_CONFIG_FILE = ROOT / 'runtime/logs/.best_config.json'

# Progressive tiers: each more aggressive
# (label, atr_min_ratio, edge_min, kelly_fraction, max_risk, adx_min, tv_filter, max_daily_loss_pct)
TIERS = [
    ("t0_base",       0.0001, 0.01,  0.50, 0.05, 12.0, False, 20.0),
    ("t1_aggressive", 0.00005, 0.005, 0.75, 0.08, 8.0,  False, 30.0),
    ("t2_very_agg",   0.00001, 0.001, 1.0,  0.10, 5.0,  False, 40.0),
    ("t3_max",        0.0,     0.0,   1.0,  0.15, 0.0,  False, 50.0),
]

# Win-rate based adjustments
WIN_RATE_TARGET = 0.40  # 40% target win rate (realistic for up/down binary)
MIN_TRADES_TO_EVALUATE = 10


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding='utf-8')


def read_tier() -> int:
    if RISK_TIER_FILE.exists():
        return int(RISK_TIER_FILE.read_text().strip())
    return 0


def write_tier(t: int) -> None:
    RISK_TIER_FILE.parent.mkdir(parents=True, exist_ok=True)
    RISK_TIER_FILE.write_text(str(t))


def get_recent_runs(hours: int = 12) -> List[Path]:
    """Get polymarket run directories within the last N hours, excluding A/B test runs."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    runs = []
    if not LAST_RUNS.exists():
        return runs
    for entry in sorted(LAST_RUNS.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        # Skip A/B test runs — they have different configs
        if name.startswith('ab_'):
            continue
        # Parse YYYYMMDDTHHMMSSZ from folder name
        try:
            ts_str = name[:15]  # 20260507T035412
            dt = datetime.strptime(ts_str, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                runs.append(entry)
        except (ValueError, IndexError):
            continue
    return runs


def analyze_runs(runs: List[Path]) -> dict:
    """Analyze recent runs and return performance stats."""
    total_trades = 0
    wins = 0
    losses = 0
    total_pnl = 0.0
    reason_counts: Counter = Counter()
    edges = []
    trade_pnls = []

    for run_dir in runs:
        summary = run_dir / 'summary.json'
        decisions = run_dir / 'decisions_log.csv'
        trades_log = run_dir / 'trades_log.csv'

        # Summary file
        if summary.exists():
            try:
                data = json.loads(summary.read_text())
                total_trades += data.get('total_trades', 0)
                wins += data.get('wins', 0)
                losses += data.get('losses', 0)
                total_pnl += data.get('total_pnl', 0.0)
            except Exception:
                pass

        # Decisions log - count reason codes
        if decisions.exists():
            try:
                with decisions.open() as f:
                    for row in csv.DictReader(f):
                        rc = row.get('reason_code', 'UNKNOWN')
                        reason_counts[rc] += 1
            except Exception:
                pass

        # Trades log - extract PnLs and edges
        if trades_log.exists():
            try:
                with trades_log.open() as f:
                    for row in csv.DictReader(f):
                        pnl = float(row.get('pnl', 0) or 0)
                        trade_pnls.append(pnl)
                        edge = float(row.get('edge', 0) or 0)
                        edges.append(edge)
            except Exception:
                pass

    win_rate = wins / total_trades if total_trades > 0 else 0.0
    avg_pnl = sum(trade_pnls) / len(trade_pnls) if trade_pnls else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0
    total_pnl_rounded = round(total_pnl, 2)
    max_drawdown = 0.0

    return {
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'win_rate': round(win_rate, 4),
        'total_pnl': total_pnl_rounded,
        'avg_trade_pnl': round(avg_pnl, 4),
        'avg_edge': round(avg_edge, 4),
        'trade_count': len(trade_pnls),
        'reason_codes': reason_counts.most_common(8),
    }


def run_supervisor() -> bool:
    """Run the paper bot supervisor cycle."""
    cp = subprocess.run(
        [str(ROOT / 'run_supervisor_cycle.sh')],
        cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    return cp.returncode == 0


def run_collector() -> bool:
    """Run collector to sync dashboard DB."""
    cp = subprocess.run(
        [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/collector.py')],
        cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    return cp.returncode == 0


def apply_config(cfg: dict, tier_idx: int, label: str) -> None:
    """Apply a config and save tier."""
    save_config(cfg)
    write_tier(tier_idx)
    log(f"📈 Aplicada config: tier {tier_idx} ({label})")
    for k, v in cfg.items():
        if k in ('atr_min_ratio', 'edge_min', 'kelly_fraction', 'max_risk_per_trade', 'adx_min', 'tv_filter_enabled'):
            log(f"   {k}: {v}")


def fine_tune_for_win_rate(cfg: dict, stats: dict) -> Tuple[dict, str]:
    """
    Adjust parameters based on win rate analysis.
    Returns (updated_config, description_of_changes)
    """
    wr = stats['win_rate']
    trades = stats['total_trades']
    changes = []

    if trades < MIN_TRADES_TO_EVALUATE:
        return cfg, f"solo {trades} trades, no hay suficiente data para ajustar"

    current_edge = cfg.get('edge_min', 0.01)
    current_kelly = cfg.get('kelly_fraction', 0.50)
    current_atr = cfg.get('atr_min_ratio', 0.0001)
    current_risk = cfg.get('max_risk_per_trade', 0.05)
    current_daily_loss = cfg.get('max_daily_loss_percent', 20.0)

    # Always ensure daily_loss limit allows at least 3 losses at current risk
    min_daily_loss = max(round(current_risk * 300, 1), 15.0)  # 3x risk_per_trade as %, min 15%
    if current_daily_loss < min_daily_loss:
        cfg['max_daily_loss_percent'] = min_daily_loss
        changes.append(f"daily_loss: {current_daily_loss} → {min_daily_loss}")

    if wr < 0.30:
        # Win rate too low - need better signal filtering
        log(f"⚠️ Win rate muy bajo ({wr:.1%}) - ajustando para mejorar calidad")
        # Increase edge filter to get higher quality signals
        new_edge = min(current_edge * 1.5, 0.10)
        cfg['edge_min'] = round(new_edge, 4)
        changes.append(f"edge_min: {current_edge} → {new_edge}")
        # Reduce position size to preserve capital
        new_kelly = max(current_kelly * 0.7, 0.10)
        cfg['kelly_fraction'] = round(new_kelly, 4)
        changes.append(f"kelly: {current_kelly} → {new_kelly}")

    elif 0.30 <= wr <= 0.55:
        # Win rate in acceptable range (near target) — relax filters for more trades
        log(f"✅ Win rate aceptable ({wr:.1%}) - relajando filtros para más volumen")
        if current_edge > 0.005:
            cfg['edge_min'] = round(current_edge * 0.8, 4)
            changes.append(f"edge_min: {current_edge} → {cfg['edge_min']}")
        # Increase Kelly to capture more upside
        new_kelly = min(current_kelly * 1.2, 1.0)
        cfg['kelly_fraction'] = round(new_kelly, 4)
        changes.append(f"kelly: {current_kelly} → {new_kelly}")

    elif wr > 0.55:
        # Great win rate — the strategy is working well
        log(f"🎯 Win rate excelente ({wr:.1%}) - maximizando captura")
        # Significantly relax filters and increase size
        cfg['edge_min'] = round(current_edge * 0.5, 4)
        changes.append(f"edge_min: {current_edge} → {cfg['edge_min']}")
        new_kelly = min(current_kelly * 1.5, 1.0)
        cfg['kelly_fraction'] = round(new_kelly, 4)
        changes.append(f"kelly: {current_kelly} → {new_kelly}")

    return cfg, ' | '.join(changes) if changes else 'sin cambios'


def check_market_volatility_adjustment(cfg: dict, runs: List[Path]) -> Tuple[dict, str]:
    """Check if ATR threshold needs adjustment based on actual market data."""
    # Look at recent decisions CSV to see ATR values
    recent_decisions = None
    for run_dir in sorted(runs, reverse=True):
        decisions = run_dir / 'decisions_log.csv'
        if decisions.exists():
            recent_decisions = decisions
            break

    if not recent_decisions:
        return cfg, 'no hay datos de volatilidad'

    atr_values = []
    try:
        with recent_decisions.open() as f:
            for i, row in enumerate(csv.DictReader(f)):
                if i > 50:
                    break
                atr_str = row.get('atr', '').strip()
                close_val = None
                # We don't have close in decisions, but we have p_model_up
                if atr_str and atr_str != 'NA' and atr_str != '':
                    try:
                        atr_values.append(float(atr_str))
                    except ValueError:
                        pass
    except Exception:
        return cfg, 'error leyendo ATR'

    if not atr_values:
        return cfg, 'sin valores ATR en decisiones'

    avg_atr = sum(atr_values) / len(atr_values)
    log(f"📊 ATR promedio reciente: {avg_atr:.2f}")

    # If ATR/close ratio is very low, the filter may be too strict
    # But we don't have close price in decisions. Check reason codes instead.
    return cfg, f'ATR promedio: {avg_atr:.2f}'


def main() -> int:
    log("=" * 55)
    log("📋 REVISIÓN CADA 2H — ANÁLISIS DE RENDIMIENTO")
    log("=" * 55)

    # 1. Gather recent runs
    runs = get_recent_runs(hours=12)
    log(f"📂 Ejecuciones recientes (12h): {len(runs)}")

    # 2. Analyze performance
    stats = analyze_runs(runs)
    log(f"\n📊 RENDIMIENTO PolyKronosPaper:")
    log(f"   Trades totales:  {stats['total_trades']}")
    log(f"   Wins: {stats['wins']} / Losses: {stats['losses']}")
    log(f"   Win rate:        {stats['win_rate']:.1%}")
    log(f"   PnL total:       ${stats['total_pnl']}")
    log(f"   PnL promedio:    ${stats['avg_trade_pnl']}")
    log(f"   Edge promedio:   {stats['avg_edge']:.4f}")

    # 3. Top reasons for NO_TRADE
    log(f"\n🔍 PRINCIPALES BLOQUEOS:")
    for rc, count in stats['reason_codes'][:5]:
        log(f"   {rc}: {count}")

    # 4. Load config and decide adjustments
    cfg = load_config()
    current_tier = read_tier()
    log(f"\n⚙️ Config actual — tier {current_tier}")
    for k in ('atr_min_ratio', 'edge_min', 'kelly_fraction', 'max_risk_per_trade', 'adx_min'):
        log(f"   {k}: {cfg.get(k, '?')}")

    # Decision logic
    trades_total = stats['total_trades']
    win_rate = stats['win_rate']

    if trades_total == 0:
        # No trades at all — escalate risk
        log("\n❌ 0 TRADES — escalando riesgo...")
        next_tier = current_tier + 1
        if next_tier < len(TIERS):
            label, atr, edge, kelly, max_risk, adx_min, tv, daily_loss = TIERS[next_tier]
            cfg.update({
                'atr_min_ratio': atr,
                'edge_min': edge,
                'kelly_fraction': kelly,
                'max_risk_per_trade': max_risk,
                'adx_min': adx_min,
                'tv_filter_enabled': tv,
                'max_daily_loss_percent': daily_loss,
            })
            apply_config(cfg, next_tier, label)
        else:
            log("⚠️ Ya en máximo riesgo — no hay más niveles")
    else:
        # Trades exist — optimize for win rate
        old_cfg = dict(cfg)
        cfg, edge_changes = fine_tune_for_win_rate(cfg, stats)

        # Market volatility check
        cfg, vol_msg = check_market_volatility_adjustment(cfg, runs)
        log(f"\n📈 {vol_msg}")

        # Only save if parameters actually changed
        changed = any(cfg.get(k) != old_cfg.get(k) for k in ('atr_min_ratio','edge_min','kelly_fraction','max_risk_per_trade','max_daily_loss_percent','adx_min'))
        if changed:
            apply_config(cfg, current_tier, f"t{current_tier}_optimized ({edge_changes})")
        else:
            log("\n✅ Sin ajustes necesarios")

        # If win rate is good but trades are few, consider lowering barrier
        if win_rate > 0.40 and trades_total < 20:
            log("📌 Win rate bueno pero pocos trades — siguiente revisión considerará relajar más")

    # 5. Re-run simulation if config changed
    if CONFIG_PATH.stat().st_mtime > (datetime.now().timestamp() - 5):
        log("\n🔄 Re-ejecutando simulación con nueva config...")
        if run_supervisor():
            log("✅ Simulación completada")
        else:
            log("❌ Error en simulación")

    # 6. Run collector to sync dashboard
    log("\n🔄 Sincronizando dashboard...")
    if run_collector():
        log("✅ Dashboard sincronizado")
    else:
        log("❌ Error en collector")

    # 7. FabiánPullback check
    signal = ROOT / 'runtime/tradingview/ctrader_signal.csv'
    if signal.exists():
        age = int(datetime.now(timezone.utc).timestamp() - signal.stat().st_mtime)
        log(f"\n📡 FabiánPullback: señal hace {age}s ({'✅' if age < 600 else '⚠️'})")
        with signal.open() as f:
            last_line = list(csv.DictReader(f))[-1]
            log(f"   Última: {last_line.get('recommendation','?')} conf={last_line.get('confidence','?')}")
    else:
        log("\n📡 FabiánPullback: ❌ sin archivo de señal")

    log("\n" + "=" * 55)
    log("✅ REVISIÓN COMPLETADA")
    log("=" * 55)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
