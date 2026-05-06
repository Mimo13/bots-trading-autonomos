#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path
import psycopg

ROOT=Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')
POLY_INPUT=ROOT/'runtime/polymarket/polymarket_input_enriched.csv'
BASE_CFG=ROOT/'polymarket_paper_config.example.json'


def run_sim(config: dict, label: str):
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_dir=ROOT/f'runtime/polymarket/runs/ab_{label}_{run_id}'
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as tf:
        json.dump(config, tf)
        cfg_path=tf.name
    cmd=[
      'python3', str(ROOT/'polymarket_paper_bot.py'),
      '--input', str(POLY_INPUT),
      '--config', cfg_path,
      '--output-dir', str(out_dir)
    ]
    cp=subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode!=0:
        raise RuntimeError(cp.stderr[-700:])
    return json.loads(cp.stdout)


def main():
    if not POLY_INPUT.exists():
        raise SystemExit('missing polymarket_input_enriched.csv for A/B test')
    base=json.loads(BASE_CFG.read_text())
    candidate=dict(base)
    candidate['edge_min']=round(float(base.get('edge_min',0.03))+0.005, 6)
    candidate['max_risk_per_trade']=round(float(base.get('max_risk_per_trade',0.02))*0.9, 6)

    b=run_sim(base,'baseline')
    c=run_sim(candidate,'candidate')

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute((ROOT/'sql/schema.sql').read_text())
        conn.execute('''insert into strategy_ab_tests(bot_name,baseline_pnl,candidate_pnl,baseline_win_rate,candidate_win_rate,delta_pnl,config_patch,notes)
                        values('poly',%s,%s,%s,%s,%s,%s::jsonb,%s)''', [
            b.get('total_pnl',0), c.get('total_pnl',0), b.get('win_rate_percent',0), c.get('win_rate_percent',0),
            float(c.get('total_pnl',0))-float(b.get('total_pnl',0)),
            json.dumps({'edge_min':candidate['edge_min'],'max_risk_per_trade':candidate['max_risk_per_trade']}),
            'A/B paper simulation (baseline vs candidate)'
        ])

    print(json.dumps({'ok':True,'baseline':b,'candidate':c}, indent=2))

if __name__=='__main__':
    main()
