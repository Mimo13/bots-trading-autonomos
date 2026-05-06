#!/usr/bin/env python3
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
OBS_PATH = Path('/Users/mimo13/Documents/Obsidian Vault/Trading/Paper_Trading_Operaciones.md')
MADRID = ZoneInfo('Europe/Madrid')

POLY_RUNS = ROOT / 'runtime/polymarket/runs'
POLY_OPS_CSV = ROOT / 'runtime/ops/poly_operations.csv'
CTRADER_OPS_CSV = ROOT / 'runtime/ops/ctrader_operations.csv'


def parse_iso_utc(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    return datetime.fromisoformat(ts)


def load_poly_rows():
    rows = []
    if not POLY_RUNS.exists():
        return rows
    for run in sorted(POLY_RUNS.glob('*/trades_log.csv')):
        with run.open('r', encoding='utf-8', newline='') as f:
            r = csv.DictReader(f)
            for x in r:
                ts = parse_iso_utc(x['entry_timestamp_utc']).astimezone(MADRID)
                side = 'Compra' if x.get('side') == 'UP' else 'Venta'
                stake = float(x.get('stake') or 0)
                pnl = float(x.get('pnl') or 0)
                rows.append((ts, side, stake, stake, pnl))
    rows.sort(key=lambda t: t[0])
    return rows


def load_generic_ops(path: Path):
    # Optional external feed for cTrader/Poly with columns:
    # timestamp_utc,operation,token_qty,usd_amount,pnl
    out = []
    if not path.exists():
        return out
    with path.open('r', encoding='utf-8', newline='') as f:
        r = csv.DictReader(f)
        for x in r:
            ts = parse_iso_utc(x['timestamp_utc']).astimezone(MADRID)
            out.append((
                ts,
                x.get('operation', ''),
                float(x.get('token_qty') or 0),
                float(x.get('usd_amount') or 0),
                float(x.get('pnl') or 0),
            ))
    out.sort(key=lambda t: t[0])
    return out


def table_rows_md(rows):
    if not rows:
        return "| _sin operaciones_ |  |  |  |  |\n"
    lines = []
    for ts, op, token_qty, usd_amt, pnl in rows[-300:]:
        lines.append(f"| {ts.strftime('%Y-%m-%d %H:%M')} | {op} | {token_qty:.6f} | {usd_amt:.2f} | {pnl:.2f} |")
    return "\n".join(lines) + "\n"


def daily_weekly_md(rows):
    dsum = defaultdict(float)
    wsum = defaultdict(float)
    for ts, _, _, _, pnl in rows:
        dsum[ts.strftime('%Y-%m-%d')] += pnl
        y, w, _ = ts.isocalendar()
        wsum[f"{y}-W{w:02d}"] += pnl

    if dsum:
        d_lines = "\n".join([f"| {d} | {v:.2f} |" for d, v in sorted(dsum.items())[-60:]]) + "\n"
    else:
        d_lines = "| _sin datos_ | 0 |\n"

    if wsum:
        w_lines = "\n".join([f"| {w} | {v:.2f} |" for w, v in sorted(wsum.items())[-24:]]) + "\n"
    else:
        w_lines = "| _sin datos_ | 0 |\n"

    return d_lines, w_lines


def build_md(ctrader_rows, poly_rows):
    now = datetime.now(MADRID).strftime('%Y-%m-%d %H:%M')
    c_day, c_week = daily_weekly_md(ctrader_rows)
    p_day, p_week = daily_weekly_md(poly_rows)

    return f"""# Paper Trading — Registro de Operaciones

Actualizado: {now}
Zona horaria objetivo: Europe/Madrid

---

## Bot: FabiánPullback

| Fecha/Hora (Madrid) | Operación | Cantidad Token | Cantidad $ | Ganancia/Pérdida $ |
|---|---|---:|---:|---:|
{table_rows_md(ctrader_rows)}
### Sumatorio diario (FabiánPullback)
| Día | PnL total $ |
|---|---:|
{c_day}
### Sumatorio semanal (FabiánPullback)
| Semana ISO | PnL total $ |
|---|---:|
{c_week}
---

## Bot: PolyKronosPaper

| Fecha/Hora (Madrid) | Operación | Cantidad Token | Cantidad $ | Ganancia/Pérdida $ |
|---|---|---:|---:|---:|
{table_rows_md(poly_rows)}
### Sumatorio diario (PolyKronosPaper)
| Día | PnL total $ |
|---|---:|
{p_day}
### Sumatorio semanal (PolyKronosPaper)
| Semana ISO | PnL total $ |
|---|---:|
{p_week}

---

## Notas
- FabiánPullback se alimenta de `runtime/ops/ctrader_operations.csv` cuando esté disponible.
- PolyKronosPaper se actualiza automáticamente desde `runtime/polymarket/runs/*/trades_log.csv`.
"""


def main():
    ctrader_rows = load_generic_ops(CTRADER_OPS_CSV)
    poly_rows = load_poly_rows()
    # optional manual poly ops override/append
    poly_rows.extend(load_generic_ops(POLY_OPS_CSV))
    poly_rows.sort(key=lambda t: t[0])

    OBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OBS_PATH.write_text(build_md(ctrader_rows, poly_rows), encoding='utf-8')
    print(f"Updated Obsidian log: {OBS_PATH}")


if __name__ == '__main__':
    main()
