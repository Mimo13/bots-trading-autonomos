#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT=Path('/Users/mimo13/bots-trading-autonomos-runtime')
LOG=ROOT/'runtime/logs/watchdog.log'
LAST_STATUS=ROOT/'runtime/polymarket/last_runner_status.json'
MAX_STATUS_AGE_S=7800

def now(): return datetime.now(timezone.utc)

def age(p:Path): return 10**9 if not p.exists() else int(now().timestamp()-p.stat().st_mtime)

def w(msg:str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a',encoding='utf-8') as f: f.write(f"{now().isoformat().replace('+00:00','Z')} | {msg}\n")

def run(cmd,tries,sleep_s,name):
    for i in range(1,tries+1):
        cp=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True)
        if cp.returncode==0:
            w(f"{name}: OK try {i}"); return True
        w(f"{name}: FAIL try {i} rc={cp.returncode} err={cp.stderr.strip()[:300]}")
        if i<tries: time.sleep(sleep_s)
    return False

def cdp_ok():
    try:
        with urlopen('http://localhost:9222/json/version', timeout=3) as r:
            d=json.loads(r.read().decode('utf-8','ignore'))
        ok=bool(d.get('Browser') and d.get('webSocketDebuggerUrl'))
        w(f"tradingview_cdp: {'OK' if ok else 'BAD'} browser={d.get('Browser','')}")
        return ok
    except Exception as e:
        w(f"tradingview_cdp: DOWN ({e})"); return False

def main():
    cdp=cdp_ok(); ta=age(LAST_STATUS)
    # DB health check — restart Postgres if needed
    dh=subprocess.run([str(ROOT/'scripts/db_health.py')],capture_output=True,text=True,timeout=90)
    w(f"db_health: {'OK' if dh.returncode==0 else f'FAIL rc={dh.returncode}'}")
    w(f"ages: cdp={'ok' if cdp else 'down'} last_runner_status={ta}s")
    if ta>MAX_STATUS_AGE_S:
        run([str(ROOT/'run_supervisor_cycle.sh')],2,30,'supervisor_cycle')
    ta2=age(LAST_STATUS)
    healthy=ta2<=MAX_STATUS_AGE_S
    w(f"health: {'OK' if healthy else 'DEGRADED'} status_age={ta2}s cdp={'ok' if cdp else 'down'}")
    return 0 if healthy else 1

if __name__=='__main__': raise SystemExit(main())
