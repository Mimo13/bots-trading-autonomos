#!/usr/bin/env python3
from __future__ import annotations
import os, subprocess, csv, json, urllib.request, urllib.error
from datetime import datetime, timezone
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
    'sol_pb': {'label': 'SolPullbackBot', 'short': 'SolPullback', 'order': 10, 'family': 'crypto'},
    'fabian_py': {'label': 'Fabian Python', 'short': 'FabianPy', 'order': 20, 'family': 'crypto'},
    'fabianpro': {'label': 'FabianPro', 'short': 'FabianPro', 'order': 30, 'family': 'crypto'},
    # ARCHIVADO: 'fabian_live_pullback': {'label': 'Fabian Live (testnet)', 'short': 'FabianLive', 'order': 35, 'family': 'testnet'},
    # ARCHIVADO: 'fabian_live_pro': {'label': 'Fabian Live Pro (testnet)', 'short': 'FabianLivePro', 'order': 36, 'family': 'testnet'},
    'tv_sol': {'label': 'TV Signal SOL', 'short': 'TV-SOL', 'order': 41, 'family': 'crypto'},
    'mtfreg': {'label': 'MTF Regime Bot', 'short': 'MTFReg', 'order': 45, 'family': 'crypto'},
    'boxbr': {'label': 'Box Breakout Bot', 'short': 'BoxBr', 'order': 46, 'family': 'crypto'},
    'scalp': {'label': 'Scalping 5m Bot', 'short': 'Scalp5m', 'order': 47, 'family': 'crypto'},
    'xrp_grid': {'label': 'XRP Grid Bot', 'short': 'XRPGrid', 'order': 48, 'family': 'crypto'},
    # ARCHIVADO: 'pfolio': {'label': 'PolyPortfolioPaper', 'short': 'PolyPortfolio', 'order': 60, 'family': 'polymarket'},
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
    if bot=='poly':
        runs=sorted((ROOT/'runtime/polymarket/runs').glob('*/decisions_log.csv'))[-90:]
        for f in runs:
            with f.open() as h:
                for row in csv.DictReader(h):
                    ts=row.get('timestamp_utc','')
                    try:
                        dt=datetime.fromisoformat(ts.replace('Z','+00:00'))
                    except Exception:
                        continue
                    if dt.timestamp()>=cutoff:
                        rows.append((dt,row.get('reason_code','UNKNOWN')))
    else:
        ops=ROOT/'runtime/ops/ctrader_operations.csv'
        if ops.exists():
            with ops.open() as h:
                for row in csv.DictReader(h):
                    rc=row.get('reason_code') or row.get('operation') or 'UNKNOWN'
                    dt=datetime.now(timezone.utc)
                    rows.append((dt,rc))
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
        and bot_name not in ('poly','fabian','pfolio','fabian_live_pullback','fabian_live_pro')
      group by bot_name
    ''')

    agg={}
    for r in rows:
        agg[r[0]]={'pnl_week':_j(r[1]),'trades':int(r[2] or 0),'win_rate':_j(r[3])}

    # Include all bots from BOT_META except poly, and those in bot_status
    bots_rows=q("select bot_name from bot_status where bot_name not in ('poly','fabian','pfolio','fabian_live_pullback','fabian_live_pro') order by bot_name")
    bot_names=[r[0] for r in bots_rows]
    for b in BOT_META.keys():
        if b != 'poly' and b not in bot_names:
            bot_names.append(b)

    items=[]
    for b in bot_names:
        m=_meta(b)
        a=agg.get(b, {'pnl_week':0,'trades':0,'win_rate':0})
        items.append({'bot_name':b, 'label':m['label'], 'short':m['short'], 'pnl_week':a['pnl_week'],'trades':a['trades'],'win_rate':a['win_rate']})

    items.sort(key=lambda x: (x.get('order', _meta(x['bot_name']).get('order',999))))
    return {'items':items}

def _run_pfolio(cfg_path: str = ''):
    """Run portfolio bot with enriched CSV, output to portfolio_ timestamp dir."""
    poly_input = ROOT / 'runtime/polymarket/polymarket_input_enriched.csv'
    if not poly_input.exists():
        poly_input = ROOT / 'runtime/polymarket/polymarket_base_input.csv'
    if not poly_input.exists():
        return
    run_id = datetime.now(timezone.utc).strftime('portfolio_%Y%m%dT%H%M%SZ')
    out_dir = ROOT / 'runtime/polymarket/runs' / run_id
    cfg = ROOT / 'polymarket_portfolio_config.json'
    cmd = [str(ROOT / '.venv/bin/python'), str(ROOT / 'polymarket_portfolio_bot.py'),
           '--input', str(poly_input), '--config', str(cfg), '--output-dir', str(out_dir)]
    subprocess.Popen(cmd, cwd=ROOT)


@app.post('/api/bots/{bot}/start')
def start(bot:str):
    db_name = {'sol_pb':'sol_pb','fabian_py':'fabian_py','fabianpro':'fabianpro','poly':'poly','tv_sol':'tv_sol'}.get(bot)
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
    db_name = {'sol_pb':'sol_pb','fabian_py':'fabian_py','fabianpro':'fabianpro',
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
    rows = q("select bot_name, is_running, mode from bot_status where bot_name not in ('poly','fabian','pfolio','fabian_live_pullback','fabian_live_pro') order by bot_name")
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
