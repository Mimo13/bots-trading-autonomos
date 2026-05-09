#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
RUNTIME = ROOT / 'runtime'
STATUS_MD = ROOT / 'PAPER_BOT_SUPERVISION_LOG.md'


def run_cmd(cmd: list[str]):
    cp = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def append(lines: list[str]):
    with STATUS_MD.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n\n')


def main():
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    lines = [f'## Supervisión {ts}','']
    rc,out,err = run_cmd(['python3', str(ROOT/'final_paper_runner.py')])
    if rc==0:
        lines.append('- Runner: ✅ OK')
        try:
            j = json.loads(out)
            for k in ['ctrader_signal_written','polymarket_enriched','polymarket_simulated','obsidian_updated']:
                lines.append(f'  - {k}: {j.get(k)}')
            if j.get('notes'):
                lines.append(f"  - notes: {' | '.join(j['notes'])}")
        except Exception:
            lines.append('- warning: salida no JSON')
    else:
        lines.append('- Runner: ❌ ERROR')
        lines.append(f'  - stderr: {err[:700]}')

    for p,label in [
        (RUNTIME/'tradingview/ctrader_signal.csv','cTrader signal CSV'),
        (Path('/Users/mimo13/Documents/Obsidian Vault/Trading/Paper_Trading_Operaciones.md'),'Obsidian trading note'),
    ]:
        if p.exists():
            age=int(datetime.now(timezone.utc).timestamp()-p.stat().st_mtime)
            lines.append(f'- {label}: ✅ exists (age_s={age})')
        else:
            lines.append(f'- {label}: ❌ missing ({p})')
    append(lines)
    print('\n'.join(lines))
    return 0 if rc==0 else 1

if __name__=='__main__':
    raise SystemExit(main())
