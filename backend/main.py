#!/usr/bin/env python3
from __future__ import annotations
import os, subprocess, csv, json, urllib.request, urllib.error
from datetime import datetime, timezone, timezone
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import psycopg

ROOT=Path('/Users/mimo13/bots-trading-autonomos-runtime')
DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')
app=FastAPI(title='Bots Trading Dashboard')
app.mount('/static', StaticFiles(directory=str(ROOT/'frontend')), name='static')

BOT_META = {
    'sol_pb': {'label': 'SolPullbackBot', 'short': 'SolPullback', 'order': 10, 'family': 'crypto', 'exchange': 'Binance (USDC)'},
    'fabian_spot_long': {'label': 'FabianSpotLong', 'short': 'FabianSpot', 'order': 25, 'family': 'crypto', 'compare': True, 'exchange': 'Binance (USDC)'},
    'xrp_grid': {'label': 'XRP Grid Bot', 'short': 'XRPGrid', 'order': 48, 'family': 'crypto', 'exchange': 'Binance (USDC)'},
    'bnb_spot_long': {'label': 'BnbSpotLongBot', 'short': 'BnbSpot', 'order': 49, 'family': 'crypto', 'exchange': 'Binance (USDC)'},
    'bnb_grid': {'label': 'BNB Grid Bot', 'short': 'BNBGrid', 'order': 49.5, 'family': 'crypto', 'exchange': 'Binance (USDC)'},
    # ARCHIVADO 2026-05-13: PolyKronosPaper — 39% WR, -$53.44 PnL 7d, 10 pérdidas seguidas, sin edge en binary options
    # ARCHIVO DEFINITIVO 2026-05-13: excluido de API, DB, collector y frontend
    # ARCHIVADO 2026-05-14: SOL Portfolio Spot (pfolio) — RSI 5m sobre SOL, -$0.54 7d, 53% WR, edge negativo.
    # ARCHIVADO 2026-05-11:
    # 'fabian_py': {'label': 'Fabian Python', 'short': 'FabianPy', ...} — shorts irrealistas; reemplazado por fabian_spot_long
    # 'fabianpro': {'label': 'FabianPro', ...} — shorts + rendimiento mediocre (+$16 en 42 trades)
    # 'mtfreg': {'label': 'MTF Regime Bot', ...} — plano (+$0.21), 5 trades, shorts
    # 'boxbr': {'label': 'Box Breakout Bot', ...} — plano (+$0.07), 5 trades, shorts
    # 'scalp': {'label': 'Scalping 5m Bot', ...} — plano (+$0.03), 6 trades, shorts
}

def _meta(bot: str):
    base = {'label': bot, 'short': bot, 'order': 999, 'family': 'paper', 'ai': False}
    base.update(BOT_META.get(bot, {}))
    return base


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

def _perf_summary(bot: str):
    rows=q('''
      select
        coalesce(sum(pnl_usd),0) as pnl_total,
        count(*) filter (where result in ('WIN','LOSS')) as closed_trades,
        count(*) filter (where result='WIN') as wins,
        count(*) filter (where result='LOSS') as losses,
        coalesce(avg(case when result='WIN' then 1.0 when result='LOSS' then 0.0 end),0) as win_rate,
        coalesce(sum(pnl_usd) filter (where ts >= now() - interval '24 hour'),0) as pnl_24h,
        coalesce(sum(pnl_usd) filter (where ts >= now() - interval '7 day'),0) as pnl_7d
      from trades
      where bot_name=%s
    ''',[bot])
    if not rows:
        return {'pnl_total':0,'closed_trades':0,'wins':0,'losses':0,'win_rate':0,'pnl_24h':0,'pnl_7d':0}
    r=rows[0]
    return {
      'pnl_total':_j(r[0]), 'closed_trades':int(r[1] or 0), 'wins':int(r[2] or 0), 'losses':int(r[3] or 0),
      'win_rate':_j(r[4]), 'pnl_24h':_j(r[5]), 'pnl_7d':_j(r[6])
    }

@app.get('/')
def home():
    return FileResponse(ROOT/'frontend/index.html')

@app.get('/api/bots/{bot}')
def bot(bot:str):
    s=q('select bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at from bot_status where bot_name=%s',[bot])
    t=q('select ts,side,token_qty,usd_amount,pnl_usd,result from trades where bot_name=%s order by ts desc limit 120',[bot])
    p=q('select symbol,side,qty,entry_price,mark_price,unrealized_pnl_usd,updated_at from positions_open where bot_name=%s order by symbol',[bot])
    # Open operations are real DB rows from trades whose result is still empty.
    # This keeps the dashboard consistent with the "Sin cerrar" donut: if a trade is counted
    # as unclosed there, it must also be visible in the open-operations table.
    open_t=q('''
      select ts,side,token_qty,usd_amount,pnl_usd,result,raw
      from trades
      where bot_name=%s and coalesce(result,'')=''
      order by ts desc
      limit 300
    ''',[bot])
    w=q('select token,amount,usd_value,updated_at from wallet_tokens where bot_name=%s order by usd_value desc',[bot])

    if s:
        ks=['bot_name','is_running','mode','balance_usd','pnl_day_usd','pnl_week_usd','tokens_value_usd','updated_at']
        status={k:_j(v) for k,v in zip(ks,s[0])}
    else:
        status={'bot_name':bot,'is_running':False,'mode':'paper','balance_usd':100,'pnl_day_usd':0,'pnl_week_usd':0,'tokens_value_usd':0,'updated_at':None}

    trades=[{k:_j(v) for k,v in zip(['ts','side','token_qty','usd_amount','pnl_usd','result'],r)} for r in t]
    positions=[{k:_j(v) for k,v in zip(['symbol','side','qty','entry_price','mark_price','unrealized_pnl_usd','updated_at'],r)} for r in p]
    for ts,side,token_qty,usd_amount,pnl_usd,result,raw in open_t:
        raw = raw or {}
        try:
            entry = raw.get('entry') or raw.get('price')
        except AttributeError:
            entry = None
        try:
            symbol = raw.get('symbol')
            sl = raw.get('sl')
            tp = raw.get('tp')
            reason = raw.get('reason')
        except AttributeError:
            symbol = sl = tp = reason = None
        positions.append({
            'symbol': symbol,
            'side': side,
            'qty': _j(token_qty),
            'entry_price': _j(entry),
            'mark_price': None,
            'unrealized_pnl_usd': None,
            'updated_at': _j(ts),
            'usd_amount': _j(usd_amount),
            'sl': _j(sl),
            'tp': _j(tp),
            'reason': reason,
            'source': 'open_trade'
        })
    wallet=[{k:_j(v) for k,v in zip(['token','amount','usd_value','updated_at'],r)} for r in w]
    status.update(_meta(bot))
    return {'status':status,'trades':trades,'positions':positions,'wallet':wallet,'performance':_perf_summary(bot)}

@app.get('/api/equity/{bot}')
def equity(bot:str, days:int=7):
    rows=q('''
      select date_trunc('day', ts) as day, coalesce(sum(pnl_usd),0) as pnl
      from trades
      where bot_name=%s and ts >= now() - (%s::text || ' day')::interval
      group by 1 order by 1
    ''',[bot, max(1,min(days,60))])
    cumulative=0.0
    items=[]
    for day,pnl in rows:
        cumulative += float(pnl or 0)
        items.append({'day':_j(day),'pnl':_j(pnl),'cumulative_pnl':cumulative})
    return {'bot':bot,'days':days,'items':items}

@app.get('/api/pnl-hourly/{bot}')
def pnl_hourly(bot:str, hours:int=24):
    rows=q('''
      select date_trunc('hour', ts) as hour, coalesce(sum(pnl_usd),0) as pnl
      from trades
      where bot_name=%s and ts >= now() - (%s::text || ' hour')::interval
      group by 1 order by 1
    ''',[bot, max(1,min(hours,168))])
    return {'bot':bot,'hours':hours,'items':[{'hour':_j(r[0]),'pnl':_j(r[1])} for r in rows]}

def _latency_checks():
    return [
      ('cTrader signal', [ROOT/'runtime/tradingview/ctrader_signal.csv'], 6*60),
      ('Runner status', [ROOT/'runtime/polymarket/last_runner_status.json'], 130*60),
      ('watchdog log', [ROOT/'runtime/logs/watchdog.log', Path('/Users/mimo13/.bta-run/logs/watchdog_runtime.log')], 5*60),
      ('supervisor log', [ROOT/'runtime/logs/supervisor_cycle.log', Path('/Users/mimo13/.bta-run/logs/supervisor_runtime.log')], 130*60),
    ]

@app.get('/api/latency')
def latency():
    now=datetime.now(timezone.utc).timestamp()
    items=[]
    for name,paths,limit in _latency_checks():
        p=None
        for cand in paths:
            if cand.exists():
                p=cand
                break
        if p is None:
            items.append({'name':name,'age_s':None,'limit_s':limit,'severity':'critical'})
            continue
        age=int(now-p.stat().st_mtime)
        sev='ok' if age<=limit else 'warning' if age<=limit*2 else 'critical'
        items.append({'name':name,'age_s':age,'limit_s':limit,'severity':sev})

    # collector freshness from DB status timestamps
    try:
        rows=q("select extract(epoch from (now()-max(updated_at))) from bot_status")
        age=int(float(rows[0][0])) if rows and rows[0][0] is not None else None
    except Exception:
        age=None
    if age is None:
        items.append({'name':'collector freshness','age_s':None,'limit_s':180,'severity':'critical'})
    else:
        sev='ok' if age<=180 else 'warning' if age<=360 else 'critical'
        items.append({'name':'collector freshness','age_s':age,'limit_s':180,'severity':sev})

    return {'items':items}

@app.get('/api/alerts')
def alerts():
    out=[]
    for x in latency()['items']:
        msg='archivo ausente' if x['age_s'] is None else f"age={x['age_s']}s"
        out.append({'severity':x['severity'],'name':x['name'],'message':msg})
    return {'alerts':out}

def _collect_reason_rows(bot:str, days:int=1):
    rows=[]
    cutoff=datetime.now(timezone.utc).timestamp() - max(1,days)*86400
    # poly ARCHIVADO 2026-05-13 — no reasons collection needed
    return rows

@app.get('/api/reasons/{bot}')
def reasons(bot:str, days:int=1):
    from collections import Counter
    c=Counter([rc for _,rc in _collect_reason_rows(bot,days)])
    top=[{'reason_code':k,'count':v} for k,v in c.most_common(12)]
    return {'bot':bot,'days':days,'items':top}

@app.get('/api/reasons-hourly/{bot}')
def reasons_hourly(bot:str, days:int=1):
    from collections import Counter
    hourly=Counter()
    for dt, rc in _collect_reason_rows(bot, days):
        key=f"{dt.hour:02d}:00"
        hourly[(key, rc)] += 1
    items=[{'hour':h,'reason_code':rc,'count':n} for (h,rc),n in sorted(hourly.items(), key=lambda x:(x[0][0],-x[1]))]
    return {'bot':bot,'days':days,'items':items}

def _orchestrator_state():
    p = ROOT / 'runtime/orchestrator/state.json'
    if not p.exists():
        return {'enabled': False, 'note': 'state not generated yet', 'bots': []}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        return {'enabled': False, 'error': str(e), 'bots': []}

@app.get('/api/orchestrator/status')
def orchestrator_status():
    return _orchestrator_state()

@app.get('/api/orchestrator/decisions')
def orchestrator_decisions(limit:int=40):
    p = ROOT / 'runtime/orchestrator/decisions.jsonl'
    if not p.exists():
        return {'items': []}
    lines = p.read_text().splitlines()[-max(1,min(limit,200)):]
    items=[]
    for line in reversed(lines):
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return {'items': items}

@app.post('/api/orchestrator/run')
def orchestrator_run():
    py=str(ROOT/'.venv/bin/python')
    cp=subprocess.run([py, str(ROOT/'scripts/bot_orchestrator.py')], capture_output=True, text=True)
    return {'ok':cp.returncode==0,'stdout':cp.stdout[-2000:], 'stderr':cp.stderr[-1200:], 'state': _orchestrator_state()}

@app.get('/api/strategy/recommendations')
def strategy_recommendations():
    rows=q('''select bot_name,ts,summary,recommendations,confidence from strategy_recommendations order by ts desc limit 10''')
    return {'items':[{'bot_name':r[0],'ts':_j(r[1]),'summary':r[2],'recommendations':r[3],'confidence':_j(r[4])} for r in rows]}

@app.post('/api/strategy/run')
def strategy_run():
    py=str(ROOT/'.venv/bin/python')
    cp=subprocess.run([py, str(ROOT/'scripts/strategy_advisor.py')], capture_output=True, text=True)
    return {'ok':cp.returncode==0,'stdout':cp.stdout[-500:], 'stderr':cp.stderr[-500:]}

@app.post('/api/strategy/ab-run')
def strategy_ab_run():
    py=str(ROOT/'.venv/bin/python')
    cp=subprocess.run([py, str(ROOT/'scripts/strategy_ab_sim.py')], capture_output=True, text=True)
    return {'ok':cp.returncode==0,'stdout':cp.stdout[-1200:], 'stderr':cp.stderr[-1200:]}

@app.get('/api/strategy/ab-tests')
def strategy_ab_tests():
    rows=q('''select id,bot_name,ts,baseline_pnl,candidate_pnl,delta_pnl,baseline_win_rate,candidate_win_rate,config_patch,notes
              from strategy_ab_tests order by ts desc limit 10''')
    items=[]
    for r in rows:
        items.append({
          'id':r[0],'bot_name':r[1],'ts':_j(r[2]),'baseline_pnl':_j(r[3]),'candidate_pnl':_j(r[4]),'delta_pnl':_j(r[5]),
          'baseline_win_rate':_j(r[6]),'candidate_win_rate':_j(r[7]),'config_patch':r[8],'notes':r[9]
        })
    return {'items':items}

@app.post('/api/strategy/promote')
def strategy_promote():
    py=str(ROOT/'.venv/bin/python')
    cp=subprocess.run([py, str(ROOT/'scripts/strategy_promote.py')], capture_output=True, text=True)
    return {'ok':cp.returncode==0,'stdout':cp.stdout[-1200:], 'stderr':cp.stderr[-1200:]}

@app.get('/api/strategy/promotions')
def strategy_promotions():
    rows=q('''select bot_name,ts,decision,reason,ab_test_id,proposed_patch,applied
              from strategy_promotions order by ts desc limit 20''')
    items=[]
    for r in rows:
        items.append({'bot_name':r[0],'ts':_j(r[1]),'decision':r[2],'reason':r[3],'ab_test_id':r[4],'proposed_patch':r[5],'applied':r[6]})
    return {'items':items}

@app.get('/api/weekly-compare')
def weekly_compare():
    rows=q('''
      select bot_name,
             coalesce(sum(pnl_usd),0) as pnl_week,
             count(*) filter (where result in ('WIN','LOSS')) as trades,
             coalesce(avg(case when result='WIN' then 1.0 when result='LOSS' then 0.0 end),0) as win_rate
      from trades
      where ts >= now() - interval '7 day'
        and bot_name not in ('poly','fabian','turtle','tv_sol','fabian_live_pullback','fabian_live_pro','fabian_py','fabianpro','mtfreg','boxbr','scalp','pfolio')
      group by bot_name
    ''')

    agg={}
    for r in rows:
        agg[r[0]]={'pnl_week':_j(r[1]),'trades':int(r[2] or 0),'win_rate':_j(r[3])}

    # Include all bots from BOT_META, excluding archived ones
    bots_rows=q("select bot_name from bot_status where bot_name not in ('poly','fabian','turtle','tv_sol','fabian_live_pullback','fabian_live_pro','fabian_py','fabianpro','mtfreg','boxbr','scalp','pfolio') order by bot_name")
    bot_names=[r[0] for r in bots_rows]
    for b in BOT_META.keys():
        if b not in bot_names:
            bot_names.append(b)

    items=[]
    for b in bot_names:
        m=_meta(b)
        a=agg.get(b, {'pnl_week':0,'trades':0,'win_rate':0})
        items.append({'bot_name':b, 'label':m['label'], 'short':m['short'], 'pnl_week':a['pnl_week'],'trades':a['trades'],'win_rate':a['win_rate']})

    items.sort(key=lambda x: (x.get('order', _meta(x['bot_name']).get('order',999))))
    return {'items':items}


@app.get('/api/weekly-compare-candidates')
def weekly_compare_candidates():
    candidate_bots = ['bnb_spot_long', 'fabian_spot_long']
    rows=q('''
      select bot_name,
             coalesce(sum(pnl_usd),0) as pnl_week,
             count(*) filter (where result in ('WIN','LOSS')) as trades,
             coalesce(avg(case when result='WIN' then 1.0 when result='LOSS' then 0.0 end),0) as win_rate
      from trades
      where ts >= now() - interval '7 day'
        and bot_name = any(%s)
      group by bot_name
    ''',[candidate_bots])
    agg={r[0]:{'pnl_week':_j(r[1]),'trades':int(r[2] or 0),'win_rate':_j(r[3])} for r in rows}
    items=[]
    for b in candidate_bots:
        m=_meta(b)
        a=agg.get(b, {'pnl_week':0,'trades':0,'win_rate':0})
        items.append({'bot_name':b, 'label':m['label'], 'short':m['short'], **a})
    return {'items':items}

def _run_pfolio(cfg_path: str = ''):
    """Run portfolio bot with enriched CSV, output to portfolio_ timestamp dir."""
    # poly ARCHIVADO 2026-05-13 — polymarket input no longer used
    # polymarket_portfolio_bot.py is also effectively archived (no live data pipeline)
    pass


@app.post('/api/bots/{bot}/start')
def start(bot:str):
    # poly removed from db_name — ARCHIVADO 2026-05-13
    db_name = {'sol_pb':'sol_pb','fabian_py':'fabian_py','fabian_spot_long':'fabian_spot_long','fabianpro':'fabianpro','tv_sol':'tv_sol'}.get(bot)
    # pfolio archivado — ver polymarket_portfolio_bot.py
    if bot=='sol_pb':
        # SolPullbackBot: arranca con datos de Binance
        subprocess.Popen([str(ROOT/'.venv/bin/python'), str(ROOT/'sol_pullback_bot.py'),
                         '--output-dir', str(ROOT/'runtime/polymarket/runs/sol_pb_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))],
                         cwd=ROOT)
    # pfolio archivado
    elif bot=='pfolio':
        pass  # _run_pfolio() — archivado 2026-05-10
    # Mark as running in DB immediately
    if db_name:
        try:
            with psycopg.connect(DB_URL) as c:
                with c.cursor() as cur:
                    cur.execute('''
                        insert into bot_status(bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at)
                        values(%s,true,'paper',0,0,0,0,now())
                        on conflict(bot_name) do update set is_running=true, updated_at=now()
                    ''', [db_name])
        except Exception as e:
            return {'ok':False,'error':str(e)}
    return {'ok':True}

@app.post('/api/bots/{bot}/stop')
def stop(bot:str):
    if bot=='sol_pb':
        subprocess.run(['pkill','-f','sol_pullback_bot'])
    # pfolio archivado
    elif bot=='pfolio':
        pass
    db_name = {'sol_pb':'sol_pb','fabian_py':'fabian_py','fabian_spot_long':'fabian_spot_long','fabianpro':'fabianpro',
               'poly':'poly','tv_sol':'tv_sol',
               'fabian_live_pullback':'fabian_live_pullback','fabian_live_pro':'fabian_live_pro','tv_sol':'tv_sol'}.get(bot)
    if db_name:
        try:
            with psycopg.connect(DB_URL) as c:
                with c.cursor() as cur:
                    cur.execute('update bot_status set is_running=false, updated_at=now() where bot_name=%s', [db_name])
        except Exception:
            pass
    return {'ok':True,'note':'detenido'}

@app.get('/api/bots')
def bots_list():
    """List all registered bots dynamically."""
    rows = q("select bot_name, is_running, mode from bot_status where bot_name not in ('poly','fabian','turtle','tv_sol','fabian_live_pullback','fabian_live_pro','fabian_py','fabianpro','mtfreg','boxbr','scalp','pfolio') order by bot_name")
    bots=[]
    for r in rows:
        m=_meta(r[0])
        bots.append({'name': r[0], 'is_running': r[1], 'mode': r[2], **m})
    bots.sort(key=lambda x:(x.get('order',999), x['name']))
    return {'bots': bots}

@app.get('/api/ai-advisor/stats')
def ai_advisor_stats():
    log_path = ROOT / 'runtime/logs/ai_advisor_validations.csv'
    if not log_path.exists():
        return {'enabled': False, 'total': 0, 'validated': 0, 'rejected': 0, 'last_10': []}
    import csv
    rows = []
    with log_path.open() as f:
        for r in csv.DictReader(f):
            rows.append(r)
    total = len(rows)
    validated = sum(1 for r in rows if r.get('decision') == 'EXECUTE')
    rejected = max(0, total - validated)
    last_10 = rows[-10:] if rows else []
    return {
        'enabled': bool(rows),
        'total': total,
        'validated': validated,
        'rejected': rejected,
        'reject_rate': round(rejected / max(1, total) * 100, 1),
        'last_10': last_10[::-1],
    }

@app.get('/api/binance/personal-portfolio')
def binance_personal_portfolio():
    try:
        from binance_client import load_client
        client = load_client(testnet=False)
        balances = client.get_balance()

        items=[]
        total=0.0
        for b in balances:
            asset=b.get('asset')
            free=float(b.get('free') or 0)
            locked=float(b.get('locked') or 0)
            qty=free+locked
            if qty<=0:
                continue
            usd_value=None
            quote_asset = asset[2:] if asset.startswith('LD') and len(asset) > 2 else asset
            if quote_asset == 'USDT':
                usd_value=qty
                change_24h=0.0
            else:
                try:
                    t=client.get_ticker(f"{quote_asset}USDT")
                    last_price=float(t.get('lastPrice') or 0)
                    usd_value=qty*last_price
                    change_24h=float(t.get('priceChangePercent') or 0)
                except Exception:
                    usd_value=None
                    change_24h=None
            if usd_value is not None:
                total += usd_value
            items.append({'asset':asset,'free':free,'locked':locked,'qty':qty,'usd_value':usd_value,'change_24h':change_24h})

        items.sort(key=lambda x: (x.get('usd_value') is None, -(x.get('usd_value') or 0)))
        return {'ok':True,'total_usd':total,'items':items,'updated_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}
    except Exception as e:
        return {'ok':False,'error':str(e),'total_usd':0,'items':[]}

@app.get('/api/personal-portfolio')
def personal_portfolio():
    cfg_path = ROOT / 'personal_portfolio_config.json'
    if not cfg_path.exists():
        return {'ok':False,'error':'config not found','accounts':[],'cold_wallet':{'items':[],'total':0}}
    cfg = json.loads(cfg_path.read_text())
    accounts = cfg.get('accounts', [])
    cold = cfg.get('cold_wallet', [])
    total_feb = sum(a.get('febrero', 0) for a in accounts)
    total_abr = sum(a.get('abril', 0) for a in accounts)
    cold_items, cold_total = [], 0.0
    for c in cold:
        qty = float(c.get('qty', 0))
        asset = c.get('asset', '')
        name = c.get('name', asset)
        price, change_24h = None, None
        # Try Binance first
        try:
            from binance_client import load_client
            t = load_client(testnet=False).get_ticker(f"{asset}USDT")
            price = float(t.get('lastPrice', 0))
            change_24h = float(t.get('priceChangePercent', 0))
        except Exception:
            pass
        # Fallback to MEXC public API
        if price is None:
            try:
                req = urllib.request.Request(f'https://api.mexc.com/api/v3/ticker/24hr?symbol={asset}USDT',
                    headers={'Accept': 'application/json','User-Agent':'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    t = json.loads(resp.read().decode('utf-8'))
                    price = float(t.get('lastPrice', 0))
                    change_24h = float(t.get('priceChangePercent', 0))
            except Exception:
                pass
        usd_value = qty * price if price else None
        if usd_value:
            cold_total += usd_value
        cold_items.append({'asset':asset,'name':name,'qty':round(qty,4),'price':price,'usd_value':round(usd_value,2) if usd_value else None,'change_24h':change_24h})
    return {'ok':True,'accounts':accounts,'totals':{'febrero':round(total_feb,2),'abril':round(total_abr,2),'change':round(total_abr-total_feb,2),'change_pct':round((total_abr/max(1,total_feb)-1)*100,1)},'cold_wallet':{'items':cold_items,'total':round(cold_total,2)},'updated_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}

@app.post('/api/personal-portfolio/save')
def personal_portfolio_save(data: dict):
    cfg_path = ROOT / 'personal_portfolio_config.json'
    if not cfg_path.exists():
        return {'ok':False}
    current = json.loads(cfg_path.read_text())
    if 'accounts' in data:
        current['accounts'] = data['accounts']
    if 'cold_wallet' in data:
        current['cold_wallet'] = data['cold_wallet']
    cfg_path.write_text(json.dumps(current, indent=2, ensure_ascii=False))
    return {'ok':True}

@app.get('/api/health')
def health():
    return {'ok':True}

# ──────────────────────────────────────────────
# CoinEx portfolio endpoint
# ──────────────────────────────────────────────
@app.get("/api/coinex/portfolio")
def coinex_portfolio():
    try:
        access_id = os.getenv("COINEX_ACCESS_ID", "")
        secret_key = os.getenv("COINEX_SECRET_KEY", "")
        if not access_id or not secret_key:
            return {"ok": False, "error": "COINEX_ACCESS_ID or COINEX_SECRET_KEY not set", "items": [], "total_usd": 0}
        from coinex_client import coinex_balance_usd
        return coinex_balance_usd(access_id, secret_key)
    except Exception as e:
        return {"ok": False, "error": str(e), "items": [], "total_usd": 0}


# ──────────────────────────────────────────────
# Portfolio Mirror — snapshot + rebalance system
# ──────────────────────────────────────────────
PORTFOLIO_SNAPSHOT = ROOT / 'runtime/portfolio/snapshot.json'
PORTFOLIO_RUNS = ROOT / 'runtime/portfolio/runs'

@app.get('/api/portfolio-mirror/status')
def portfolio_mirror_status():
    """Estado actual del Portfolio Mirror Bot."""
    # Leer snapshot
    snapshot = {'total_usd': 0, 'assets': [], 'captured_at': None}
    if PORTFOLIO_SNAPSHOT.exists():
        try:
            snapshot = json.loads(PORTFOLIO_SNAPSHOT.read_text())
        except Exception:
            pass
    # Leer último run
    latest_run = None
    latest_mtime = 0
    if PORTFOLIO_RUNS.exists():
        for entry in sorted(PORTFOLIO_RUNS.iterdir()):
            if entry.name.startswith('portfolio_mirror_'):
                s = entry / 'summary.json'
                if s.exists() and s.stat().st_mtime > latest_mtime:
                    latest_run = entry
                    latest_mtime = s.stat().st_mtime
    summary = None
    if latest_run:
        try:
            summary = json.loads((latest_run / 'summary.json').read_text())
        except Exception:
            pass
    # Performance desde BD
    perf = _perf_summary('portfolio_mirror')
    # Target allocation por régimen
    from scripts.bot_orchestrator import detect_regime, merge_regimes
    try:
        regs = [detect_regime(s) for s in ['SOLUSDT','XRPUSDT','BNBUSDC']]
        current_regime = merge_regimes(regs).get('regime', 'sideways')
    except Exception:
        current_regime = 'sideways'
    targets = {'sideways': {'XRP':35,'PEPE':10,'USDC':30,'SOL':15,'HBAR':10},
               'bull': {'XRP':40,'PEPE':15,'USDC':15,'SOL':20,'HBAR':10},
               'bear': {'XRP':20,'PEPE':5,'USDC':55,'SOL':10,'HBAR':10}}
    active_target = targets.get(current_regime, targets['sideways'])
    return {'ok': True,
            'snapshot': snapshot,
            'current_regime': current_regime,
            'targets': targets,
            'active_target': active_target,
            'latest_summary': summary,
            'performance': perf}

@app.get('/api/portfolio-mirror/history')
def portfolio_mirror_history(limit: int = 30):
    """Histórico de runs del Portfolio Mirror Bot."""
    items = []
    if PORTFOLIO_RUNS.exists():
        for entry in sorted(PORTFOLIO_RUNS.iterdir(), reverse=True):
            if entry.name.startswith('portfolio_mirror_'):
                s = entry / 'summary.json'
                if s.exists():
                    try:
                        items.append(json.loads(s.read_text()))
                    except Exception:
                        pass
                if len(items) >= limit:
                    break
    return {'ok': True, 'items': items}

PORTFOLIO_DECISIONS = ROOT / 'runtime/portfolio/decisions.jsonl'

@app.get('/api/portfolio-mirror/decisions')
def portfolio_mirror_decisions(limit: int = 50):
    """Decisiones del Portfolio Mirror Bot (qué investigó, por qué tradeó o no)."""
    if not PORTFOLIO_DECISIONS.exists():
        return {'ok': True, 'items': []}
    lines = PORTFOLIO_DECISIONS.read_text().strip().split('\n')
    items = []
    for line in lines[-max(1, min(limit, 200)):]:
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return {'ok': True, 'items': items}

# ──────────────────────────────────────────────
# Shadow Mode — HMM vs Heuristic comparison
# ──────────────────────────────────────────────
@app.get("/api/shadow/status")
def shadow_status():
    """Current regime: orchestrator heuristic vs HMM snapshot."""
    import json as _j
    from pathlib import Path
    from scripts.bot_orchestrator import detect_regime, merge_regimes, load_json, utc_now
    CONFIG = ROOT / "orchestrator_config.json"
    cfg = load_json(CONFIG, {})
    # Heuristic result
    symbols = cfg.get("symbols", ["SOLUSDT"])
    heur_regimes = [detect_regime(s) for s in symbols]
    heur_merged = merge_regimes(heur_regimes)
    # HMM snapshot
    snapshot_path = ROOT / "hmm" / "output" / "hmm_regime_snapshot.json"
    hmm_data = {"regime": "unknown", "confidence": 0.0, "reason": "", "symbols": [], "source": "none"}
    if snapshot_path.exists():
        try:
            hmm_data = _j.loads(snapshot_path.read_text())
        except Exception:
            pass
    heuristic_regime = heur_merged.get("regime", "unknown")
    hmm_regime = hmm_data.get("regime", "unknown")
    agree = heuristic_regime == hmm_regime
    return {
        "ok": True,
        "timestamp": utc_now(),
        "shadow_enabled": bool(cfg.get("hmm_regime", {}).get("enabled")),
        "heuristic": {
            "regime": heuristic_regime,
            "confidence": heur_merged.get("confidence", 0),
            "reason": heur_merged.get("reason", ""),
            "source": "orchestrator_heuristic",
        },
        "hmm": {
            "regime": hmm_regime,
            "confidence": hmm_data.get("confidence", 0),
            "reason": hmm_data.get("reason", ""),
            "symbols": hmm_data.get("symbols", []),
            "source": hmm_data.get("source", "unknown"),
            "generated_at": hmm_data.get("generated_at", ""),
        },
        "comparison": {
            "agree": agree,
            "heuristic_regime": heuristic_regime,
            "hmm_regime": hmm_regime,
            "disagreement_severity": "high" if (heuristic_regime in ("bull", "sideways") and hmm_regime in ("bear", "risk_off")) or (heuristic_regime == "risk_off" and hmm_regime in ("bull", "sideways")) else "medium" if heuristic_regime != hmm_regime else "none",
        },
    }


SHADOW_LOG = ROOT / "runtime" / "orchestrator" / "shadow_log.jsonl"



ANALYSIS_DIR = ROOT / "analisis"


def _parse_analysis_frontmatter(text: str):
    meta = {}
    body = text
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end != -1:
            fm = text[4:end]
            body = text[end + 5:]
            for line in fm.splitlines():
                if ':' not in line:
                    continue
                k, v = line.split(':', 1)
                k = k.strip().lower()
                v = v.strip()
                if k == 'tags':
                    if v.startswith('[') and v.endswith(']'):
                        vals = [x.strip().strip('"\'') for x in v[1:-1].split(',') if x.strip()]
                        meta[k] = vals
                    else:
                        meta[k] = [x.strip() for x in v.split(',') if x.strip()]
                else:
                    meta[k] = v.strip('"\'')
    return meta, body


def _fallback_title(body: str, file_name: str) -> str:
    for ln in body.splitlines():
        ln = ln.strip()
        if ln.startswith('#'):
            return ln.lstrip('#').strip()
    return file_name.rsplit('.',1)[0].replace('_', ' ').replace('-', ' ').strip()


def _fallback_summary(body: str, max_len: int = 180) -> str:
    for ln in body.splitlines():
        t = ln.strip()
        if not t or t.startswith('#'):
            continue
        if len(t) > max_len:
            return t[:max_len-1].rstrip() + '\u2026'
        return t
    return 'Sin resumen todav\u00eda.'


@app.get('/api/analisis')
def list_analisis():
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(ANALYSIS_DIR.glob('*.md'), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            text = p.read_text(encoding='utf-8')
        except Exception:
            continue
        meta, body = _parse_analysis_frontmatter(text)
        st = p.stat()
        title = meta.get('title') or _fallback_title(body, p.name)
        summary = meta.get('summary') or _fallback_summary(body)
        tags = meta.get('tags') or ['analisis']
        if '__' in p.name:
            prefix = p.name.split('__', 1)[0].lower()
            if prefix and prefix not in tags:
                tags = [prefix] + tags
        date = meta.get('date') or datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        items.append({
            'id': p.name, 'file': p.name, 'title': title,
            'summary': summary, 'tags': tags,
            'date': date,
            'updated_at': datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    return {'items': items}


@app.get('/api/analisis/read-status')
def analisis_read_status():
    return {"read_ids": []}


@app.get('/api/analisis/{item_id}')
def get_analisis(item_id: str):
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    if '/' in item_id or '\\' in item_id or item_id.startswith('.'):
        return {'ok': False, 'error': 'invalid id'}
    p = (ANALYSIS_DIR / item_id).resolve()
    if not str(p).startswith(str(ANALYSIS_DIR.resolve())) or not p.exists() or p.suffix.lower() != '.md':
        return {'ok': False, 'error': 'not found'}
    text = p.read_text(encoding='utf-8')
    meta, body = _parse_analysis_frontmatter(text)
    title = meta.get('title') or _fallback_title(body, p.name)
    summary = meta.get('summary') or _fallback_summary(body)
    tags = meta.get('tags') or ['analisis']
    if '__' in p.name:
        prefix = p.name.split('__', 1)[0].lower()
        if prefix and prefix not in tags:
            tags = [prefix] + tags
    return {
        'ok': True, 'id': p.name, 'file': p.name,
        'title': title, 'summary': summary,
        'tags': tags, 'date': meta.get('date'),
        'markdown': body,
    }


@app.post('/api/sherlock/request')
def create_sherlock_request(data: dict):
    topic = (data.get('topic') or '').strip()
    if not topic:
        return {'ok': False, 'error': 'topic required'}
    req_dir = ANALYSIS_DIR / 'requests'
    req_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    existing = list(req_dir.glob(f'REQ__{date_str}_*.md'))
    n = len(existing) + 1
    safe_topic = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in topic)[:50].strip().replace(' ', '_').lower()
    filename = f'REQ__{date_str}_{n:02d}_{safe_topic}.md'
    filepath = req_dir / filename
    content = f'''---
title: "Petición: {topic}"
summary: "Pendiente de procesar por Sherlock"
tags: [request, sherlock]
date: {date_str}
---

# 📬 Petición de Análisis — {topic}

> Fecha: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
> Estado: Pendiente

'''
    filepath.write_text(content)
    return {'ok': True, 'file': filename, 'path': str(filepath)}


@app.get('/api/sherlock/requests')
def list_sherlock_requests():
    req_dir = ANALYSIS_DIR / 'requests'
    req_dir.mkdir(parents=True, exist_ok=True)
    done_dir = req_dir / 'done'
    items = []
    for p in sorted(req_dir.glob('REQ__*.md'), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.parent.name == 'done':
            continue
        text = p.read_text(encoding='utf-8')
        meta, body = _parse_analysis_frontmatter(text)
        title = meta.get('title') or p.name
        # Check if body has more than just the template (has content added by Sherlock)
        has_content = len(body.strip()) > 250
        items.append({'id': p.name, 'title': title, 'status': 'hecho' if has_content else 'pendiente',
                      'date': meta.get('date', ''), 'file': p.name, 'body': body.strip() if has_content else ''})
    if done_dir.exists():
        for p in sorted(done_dir.glob('REQ__*.md'), key=lambda x: x.stat().st_mtime, reverse=True):
            text = p.read_text(encoding='utf-8')
            meta, body = _parse_analysis_frontmatter(text)
            title = meta.get('title') or p.name
            items.append({'id': p.name, 'title': title, 'status': 'hecho',
                          'date': meta.get('date', ''), 'file': p.name, 'body': ''})
    return {'items': items}

@app.get("/api/shadow/history")
def shadow_history(limit: int = 100):
    """Historical shadow mode comparisons."""
    if not SHADOW_LOG.exists():
        return {"ok": True, "items": []}
    lines = SHADOW_LOG.read_text().strip().split("\n")
    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    items.reverse()
    return {"ok": True, "items": items}


@app.post("/api/shadow/log")
def shadow_log():
    """Write current shadow comparison to history log."""
    status = shadow_status()
    from scripts.bot_orchestrator import utc_now
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        entry = {
            "ts": status.get("timestamp", utc_now()),
            "heuristic_regime": status.get("heuristic", {}).get("regime"),
            "hmm_regime": status.get("hmm", {}).get("regime"),
            "agree": status.get("comparison", {}).get("agree"),
            "heuristic_confidence": status.get("heuristic", {}).get("confidence"),
            "hmm_confidence": status.get("hmm", {}).get("confidence"),
            "disagreement_severity": status.get("comparison", {}).get("disagreement_severity"),
        }
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True}
