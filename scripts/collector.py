#!/usr/bin/env python3
from __future__ import annotations
import csv, json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import psycopg

ROOT=Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')


def parse_ts(s:str):
    s=(s or '').strip()
    if s.endswith('Z'): s=s[:-1]+'+00:00'
    return datetime.fromisoformat(s)


def ensure_schema(conn):
    conn.execute((ROOT/'sql/schema.sql').read_text())


def upsert_status(conn, bot, **vals):
    conn.execute('''
    insert into bot_status(bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at)
    values(%(bot)s,%(is_running)s,'paper',%(balance)s,%(pnl_day)s,%(pnl_week)s,%(tokens)s,now())
    on conflict(bot_name) do update set
      is_running=excluded.is_running, mode=excluded.mode, balance_usd=excluded.balance_usd,
      pnl_day_usd=excluded.pnl_day_usd,pnl_week_usd=excluded.pnl_week_usd,tokens_value_usd=excluded.tokens_value_usd,
      updated_at=now()
    ''', {'bot':bot,'is_running':vals.get('is_running',False),'balance':vals.get('balance',0),'pnl_day':vals.get('pnl_day',0),'pnl_week':vals.get('pnl_week',0),'tokens':vals.get('tokens',0)})


def load_wallet_positions(conn, bot:str):
    wallet_csv = ROOT / f'runtime/ops/{bot}_wallet_tokens.csv'
    pos_csv = ROOT / f'runtime/ops/{bot}_open_positions.csv'
    if wallet_csv.exists():
        with wallet_csv.open() as f:
            r=csv.DictReader(f)
            for x in r:
                conn.execute('''insert into wallet_tokens(bot_name,token,amount,usd_value,updated_at)
                                values(%s,%s,%s,%s,now())
                                on conflict(bot_name,token) do update set amount=excluded.amount, usd_value=excluded.usd_value, updated_at=now()''',
                             [bot, x.get('token','UNK'), float(x.get('amount') or 0), float(x.get('usd_value') or 0)])
    if pos_csv.exists():
        with pos_csv.open() as f:
            r=csv.DictReader(f)
            for x in r:
                conn.execute('''insert into positions_open(bot_name,symbol,side,qty,entry_price,mark_price,unrealized_pnl_usd,updated_at)
                                values(%s,%s,%s,%s,%s,%s,%s,now())
                                on conflict(bot_name,symbol,side) do update set qty=excluded.qty,entry_price=excluded.entry_price,mark_price=excluded.mark_price,unrealized_pnl_usd=excluded.unrealized_pnl_usd,updated_at=now()''',
                             [bot, x.get('symbol','UNK'), x.get('side','BUY'), float(x.get('qty') or 0), float(x.get('entry_price') or 0), float(x.get('mark_price') or 0), float(x.get('unrealized_pnl_usd') or 0)])


INITIAL_BALANCE = 100.0

def load_poly(conn):
    runs=ROOT/'runtime/polymarket/runs'
    files=sorted(runs.glob('*/trades_log.csv')) if runs.exists() else []
    now=datetime.now(timezone.utc); day0=now.date(); week0=now-timedelta(days=7)
    pnl_day=0.0; pnl_week=0.0; balance=INITIAL_BALANCE; trades_n=0
    for f in files[-40:]:
        with f.open() as h:
            r=csv.DictReader(h)
            for x in r:
                trades_n += 1
                ts=parse_ts(x['entry_timestamp_utc']); pnl=float(x.get('pnl') or 0); stake=float(x.get('stake') or 0)
                balance += pnl
                if ts.date()==day0: pnl_day += pnl
                if ts>=week0: pnl_week += pnl
                side='BUY' if x.get('side')=='UP' else 'SELL'
                conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                values('poly',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                [ts, side, stake, stake, pnl, x.get('result'), json.dumps(x)])
    load_wallet_positions(conn,'poly')
    tokens=conn.execute("select coalesce(sum(usd_value),0) from wallet_tokens where bot_name='poly'").fetchone()[0]
    running=(ROOT/'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn,'poly',is_running=running,balance=balance,pnl_day=pnl_day,pnl_week=pnl_week,tokens=float(tokens or 0))


def load_fabian(conn):
    ops=ROOT/'runtime/ops/ctrader_operations.csv'; signal=ROOT/'runtime/tradingview/ctrader_signal.csv'
    running=signal.exists() and (datetime.now(timezone.utc).timestamp()-signal.stat().st_mtime)<600
    now=datetime.now(timezone.utc); day0=now.date(); week0=now-timedelta(days=7)
    pnl_day=0.0; pnl_week=0.0; balance=INITIAL_BALANCE
    if ops.exists():
        with ops.open() as h:
            r=csv.DictReader(h)
            for x in r:
                ts=parse_ts(x['timestamp_utc']); pnl=float(x.get('pnl') or 0); usd=float(x.get('usd_amount') or 0); qty=float(x.get('token_qty') or 0)
                balance += pnl
                if ts.date()==day0: pnl_day += pnl
                if ts>=week0: pnl_week += pnl
                conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                values('fabian',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                [ts, x.get('operation',''), qty, usd, pnl, 'WIN' if pnl>0 else 'LOSS' if pnl<0 else 'FLAT', json.dumps(x)])
    load_wallet_positions(conn,'fabian')
    tokens=conn.execute("select coalesce(sum(usd_value),0) from wallet_tokens where bot_name='fabian'").fetchone()[0]
    upsert_status(conn,'fabian',is_running=running,balance=balance,pnl_day=pnl_day,pnl_week=pnl_week,tokens=float(tokens or 0))


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        ensure_schema(conn)
        load_poly(conn)
        load_fabian(conn)

if __name__=='__main__':
    main()
