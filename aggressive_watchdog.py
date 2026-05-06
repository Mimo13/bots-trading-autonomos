#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT=Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
LOG=ROOT/'runtime/logs/watchdog.log'
CTRADER_SIGNAL=ROOT/'runtime/tradingview/ctrader_signal.csv'
LAST_STATUS=ROOT/'runtime/polymarket/last_runner_status.json'
MAX_SIGNAL_AGE_S=360
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
    cdp=cdp_ok(); sa=age(CTRADER_SIGNAL); ta=age(LAST_STATUS)
    w(f"ages: ctrader_signal={sa}s last_runner_status={ta}s")
    if sa>MAX_SIGNAL_AGE_S or not cdp:
        run([str(ROOT/'run_bridge_cycle.sh')],3,15,'bridge_cycle')
    if ta>MAX_STATUS_AGE_S:
        run([str(ROOT/'run_supervisor_cycle.sh')],2,30,'supervisor_cycle')
    sa2=age(CTRADER_SIGNAL); ta2=age(LAST_STATUS)
    healthy=sa2<=MAX_SIGNAL_AGE_S and ta2<=MAX_STATUS_AGE_S
    w(f"health: {'OK' if healthy else 'DEGRADED'} signal_age={sa2}s status_age={ta2}s")
    return 0 if healthy else 1

if __name__=='__main__': raise SystemExit(main())
