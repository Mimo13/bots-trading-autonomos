#!/usr/bin/env python3
from __future__ import annotations
import json, os, shutil, subprocess
from datetime import datetime, timezone
from pathlib import Path
import psycopg

ROOT = Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
DB_URL = os.getenv('DATABASE_URL', 'postgresql:///bots_dashboard')
CFG = ROOT / 'polymarket_paper_config.example.json'
LOG = ROOT / 'runtime/logs/strategy_cycle_2h.log'


def run_py(script: str):
    py = ROOT / '.venv/bin/python'
    cp = subprocess.run([str(py), str(ROOT / script)], capture_output=True, text=True)
    return cp.returncode, cp.stdout, cp.stderr


def log(msg: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f"{ts} | {msg}\n")


def apply_patch_if_promoted():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        row = conn.execute('''
            select decision, proposed_patch
            from strategy_promotions
            order by ts desc
            limit 1
        ''').fetchone()
        if not row:
            log('no promotion decision found')
            return
        decision, patch = row
        if decision != 'promote_candidate_paper':
            log(f'promotion decision={decision} (no apply)')
            return

        current = json.loads(CFG.read_text(encoding='utf-8'))
        backup = CFG.with_suffix('.json.bak')
        shutil.copy2(CFG, backup)

        # patch puede venir como JSONB dict
        for k, v in (patch or {}).items():
            current[k] = v

        current['_last_autotune_utc'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        CFG.write_text(json.dumps(current, indent=2), encoding='utf-8')
        log(f'auto-applied paper patch: {json.dumps(patch)}')


def main():
    for s in ['scripts/strategy_advisor.py', 'scripts/strategy_ab_sim.py', 'scripts/strategy_promote.py']:
        rc, out, err = run_py(s)
        if rc != 0:
            log(f'{s} FAILED: {err[-400:]}')
            return 1
        log(f'{s} OK')
    apply_patch_if_promoted()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
