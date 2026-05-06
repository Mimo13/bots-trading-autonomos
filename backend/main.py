#!/usr/bin/env python3
from __future__ import annotations
import os, subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import psycopg

ROOT=Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')
app=FastAPI(title='Bots Trading Dashboard')
app.mount('/static', StaticFiles(directory=str(ROOT/'frontend')), name='static')


def _j(v):
    if isinstance(v, Decimal): return float(v)
    if hasattr(v,'isoformat'): return v.isoformat()
    return v


def q(sql, params=None):
    with psycopg.connect(DB_URL) as c:
        with c.cursor() as cur:
            cur.execute(sql, params or [])
            try: return cur.fetchall()
            except Exception: return []

@app.get('/')
def home():
    return FileResponse(ROOT/'frontend/index.html')

@app.get('/api/bots/{bot}')
def bot(bot:str):
    s=q('select bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at from bot_status where bot_name=%s',[bot])
    t=q('select ts,side,token_qty,usd_amount,pnl_usd,result from trades where bot_name=%s order by ts desc limit 120',[bot])
    p=q('select symbol,side,qty,entry_price,mark_price,unrealized_pnl_usd,updated_at from positions_open where bot_name=%s order by symbol',[bot])
    w=q('select token,amount,usd_value,updated_at from wallet_tokens where bot_name=%s order by usd_value desc',[bot])

    if s:
        ks=['bot_name','is_running','mode','balance_usd','pnl_day_usd','pnl_week_usd','tokens_value_usd','updated_at']
        status={k:_j(v) for k,v in zip(ks,s[0])}
    else:
        status={'bot_name':bot,'is_running':False,'mode':'paper','balance_usd':0,'pnl_day_usd':0,'pnl_week_usd':0,'tokens_value_usd':0,'updated_at':None}

    trades=[{k:_j(v) for k,v in zip(['ts','side','token_qty','usd_amount','pnl_usd','result'],r)} for r in t]
    positions=[{k:_j(v) for k,v in zip(['symbol','side','qty','entry_price','mark_price','unrealized_pnl_usd','updated_at'],r)} for r in p]
    wallet=[{k:_j(v) for k,v in zip(['token','amount','usd_value','updated_at'],r)} for r in w]
    return {'status':status,'trades':trades,'positions':positions,'wallet':wallet}

@app.get('/api/alerts')
def alerts():
    out=[]
    now=datetime.now(timezone.utc).timestamp()
    checks=[
      ('cTrader signal', ROOT/'runtime/tradingview/ctrader_signal.csv', 6*60),
      ('Poly status', ROOT/'runtime/polymarket/last_runner_status.json', 130*60),
      ('watchdog log', ROOT/'runtime/logs/watchdog.log', 5*60),
    ]
    for name,p,limit in checks:
        if not p.exists():
            out.append({'severity':'critical','name':name,'message':'archivo ausente'})
            continue
        age=int(now-p.stat().st_mtime)
        sev='ok' if age<=limit else 'warning' if age<=limit*2 else 'critical'
        out.append({'severity':sev,'name':name,'message':f'age={age}s'})
    return {'alerts':out}

@app.get('/api/strategy/recommendations')
def strategy_recommendations():
    rows=q('''select bot_name,ts,summary,recommendations,confidence from strategy_recommendations order by ts desc limit 10''')
    return {'items':[{'bot_name':r[0],'ts':_j(r[1]),'summary':r[2],'recommendations':r[3],'confidence':_j(r[4])} for r in rows]}

@app.post('/api/strategy/run')
def strategy_run():
    cp=subprocess.run([str(ROOT/'scripts/strategy_advisor.py')], capture_output=True, text=True)
    return {'ok':cp.returncode==0,'stdout':cp.stdout[-500:], 'stderr':cp.stderr[-500:]}

@app.post('/api/bots/{bot}/start')
def start(bot:str):
    if bot=='fabian':
        subprocess.run(['open','-ga','/Applications/cTrader.app'])
        subprocess.run([str(ROOT/'run_bridge_cycle.sh')])
    elif bot=='poly':
        subprocess.run([str(ROOT/'run_supervisor_cycle.sh')])
    return {'ok':True}

@app.post('/api/bots/{bot}/stop')
def stop(bot:str):
    if bot=='fabian': subprocess.run(['pkill','-f','cTrader'])
    return {'ok':True,'note':'modo real deshabilitado'}

@app.get('/api/health')
def health():
    return {'ok':True}
