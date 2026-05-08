#!/usr/bin/env python3
"""Collector — sincroniza datos de bots con PostgreSQL para el dashboard."""
from __future__ import annotations
import csv, json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import psycopg

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
DB_URL = os.getenv('DATABASE_URL', 'postgresql:///bots_dashboard')
INITIAL_BALANCE = 100.0


def parse_ts(s: str):
    s = (s or '').strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)


def ensure_schema(conn):
    conn.execute((ROOT / 'sql/schema.sql').read_text())


def upsert_status(conn, bot, **vals):
    conn.execute('''
    insert into bot_status(bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at)
    values(%(bot)s,%(is_running)s,'paper',%(balance)s,%(pnl_day)s,%(pnl_week)s,%(tokens)s,now())
    on conflict(bot_name) do update set
      is_running=excluded.is_running, mode=excluded.mode, balance_usd=excluded.balance_usd,
      pnl_day_usd=excluded.pnl_day_usd,pnl_week_usd=excluded.pnl_week_usd,tokens_value_usd=excluded.tokens_value_usd,
      updated_at=now()
    ''', {'bot': bot, 'is_running': vals.get('is_running', False),
          'balance': vals.get('balance', 0), 'pnl_day': vals.get('pnl_day', 0),
          'pnl_week': vals.get('pnl_week', 0), 'tokens': vals.get('tokens', 0)})


def last_bot_balance(prefix: str, default: float = INITIAL_BALANCE) -> float:
    """Lee el final_balance/final_equity del run más reciente del bot."""
    runs_dir = ROOT / 'runtime/polymarket/runs'
    if not runs_dir.exists():
        return default
    latest = None
    for entry in sorted(runs_dir.iterdir()):
        if entry.name.startswith(prefix):
            summary = entry / 'summary.json'
            if summary.exists():
                try:
                    data = json.loads(summary.read_text())
                    bal = float(data.get('final_balance', data.get('final_equity', default)))
                    latest = (summary.stat().st_mtime, bal)
                except Exception:
                    pass
    return latest[1] if latest else default


def load_wallet_positions(conn, bot: str):
    wallet_csv = ROOT / f'runtime/ops/{bot}_wallet_tokens.csv'
    pos_csv = ROOT / f'runtime/ops/{bot}_open_positions.csv'
    if wallet_csv.exists():
        with wallet_csv.open() as f:
            for x in csv.DictReader(f):
                conn.execute('''insert into wallet_tokens(bot_name,token,amount,usd_value,updated_at)
                values(%s,%s,%s,%s,now())
                on conflict(bot_name,token) do update set amount=excluded.amount, usd_value=excluded.usd_value, updated_at=now()''',
                             [bot, x.get('token', 'UNK'), float(x.get('amount') or 0), float(x.get('usd_value') or 0)])
    if pos_csv.exists():
        with pos_csv.open() as f:
            for x in csv.DictReader(f):
                conn.execute('''insert into positions_open(bot_name,symbol,side,qty,entry_price,mark_price,unrealized_pnl_usd,updated_at)
                values(%s,%s,%s,%s,%s,%s,%s,now())
                on conflict(bot_name,symbol,side) do update set qty=excluded.qty,entry_price=excluded.entry_price,mark_price=excluded.mark_price,unrealized_pnl_usd=excluded.unrealized_pnl_usd,updated_at=now()''',
                             [bot, x.get('symbol', 'UNK'), x.get('side', 'BUY'), float(x.get('qty') or 0),
                              float(x.get('entry_price') or 0), float(x.get('mark_price') or 0),
                              float(x.get('unrealized_pnl_usd') or 0)])


def load_poly(conn):
    runs = ROOT / 'runtime/polymarket/runs'
    files = sorted(runs.glob('[0-9]*/trades_log.csv')) if runs.exists() else []
    now = datetime.now(timezone.utc)
    day0 = now.date()
    week0 = now - timedelta(days=7)
    pnl_day = 0.0
    pnl_week = 0.0
    for f in files[-40:]:
        with f.open() as h:
            for x in csv.DictReader(h):
                ts = parse_ts(x['entry_timestamp_utc'])
                pnl = float(x.get('pnl') or 0)
                stake = float(x.get('stake') or 0)
                if ts.date() == day0:
                    pnl_day += pnl
                if ts >= week0:
                    pnl_week += pnl
                side = 'BUY' if x.get('side') == 'UP' else 'SELL'
                conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                values('poly',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                             [ts, side, stake, stake, pnl, x.get('result'), json.dumps(x)])
    balance = last_bot_balance('2', INITIAL_BALANCE)  # runs named YYYYMMDD...
    running = (ROOT / 'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn, 'poly', is_running=running, balance=balance,
                  pnl_day=pnl_day, pnl_week=pnl_week, tokens=0)


def load_fabian(conn):
    """cTrader original (C# bot) — solo señal, sin trades reales."""
    signal = ROOT / 'runtime/tradingview/ctrader_signal.csv'
    running = signal.exists() and (datetime.now(timezone.utc).timestamp() - signal.stat().st_mtime) < 600
    upsert_status(conn, 'fabian', is_running=running, balance=INITIAL_BALANCE,
                  pnl_day=0, pnl_week=0, tokens=0)


def load_pfolio(conn):
    """Portfolio bot — lee su propio resumen."""
    runs = ROOT / 'runtime/polymarket/runs'
    now = datetime.now(timezone.utc)
    day0 = now.date()
    week0 = now - timedelta(days=7)
    if runs.exists():
        for entry in runs.glob('portfolio_*'):
            tl = entry / 'trades_log.csv'
            if tl.exists():
                with tl.open() as f:
                    for x in csv.DictReader(f):
                        ts_str = x.get('ts', '')
                        try:
                            ts = parse_ts(ts_str)
                        except Exception:
                            continue
                        pnl = float(x.get('realized_pnl', 0) or 0)
                        side = x.get('side', '').upper()
                        # Solo las ventas tienen resultado (compras están abiertas)
                        if side == 'SELL':
                            result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        else:
                            result = ''  # posición abierta, sin resultado aún
                        conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                        values('pfolio',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                                     [ts, side, float(x.get('qty', 0) or 0),
                                      float(x.get('usd_amount', 0) or 0), pnl, result, json.dumps(x)])
    # Read latest portfolio summary for balance and wallet
    balance = INITIAL_BALANCE
    tokens_value = 0.0
    position_qty = 0.0
    position_price = 0.0
    
    latest_summary = None
    latest_mtime = 0
    for entry in runs.glob('portfolio_*'):
        s = entry / 'summary.json'
        if s.exists():
            mt = s.stat().st_mtime
            if mt > latest_mtime:
                latest_mtime = mt
                latest_summary = json.loads(s.read_text())
    
    if latest_summary:
        balance = float(latest_summary.get('final_balance', INITIAL_BALANCE))
        position_qty = float(latest_summary.get('final_position_qty', 0))
        tokens_value = float(latest_summary.get('position_value', 0))
        if position_qty > 0 and tokens_value > 0:
            position_price = tokens_value / position_qty
            # Write wallet tokens for dashboard "Cartera tokens"
            conn.execute('''insert into wallet_tokens(bot_name,token,amount,usd_value,updated_at)
            values('pfolio','TOKENS',%s,%s,now())
            on conflict(bot_name,token) do update set amount=excluded.amount, usd_value=excluded.usd_value, updated_at=now()''',
                         [round(position_qty, 6), round(tokens_value, 2)])
            # Write open position
            conn.execute('''insert into positions_open(bot_name,symbol,side,qty,entry_price,mark_price,unrealized_pnl_usd,updated_at)
            values('pfolio','TOKENS','BUY',%s,%s,%s,0,now())
            on conflict(bot_name,symbol,side) do update set qty=excluded.qty, entry_price=excluded.entry_price, mark_price=excluded.mark_price, updated_at=now()''',
                         [round(position_qty, 6), round(position_price, 4), round(position_price, 4)])
    
    running = (ROOT / 'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn, 'pfolio', is_running=running, balance=balance,
                  pnl_day=0, pnl_week=0, tokens=round(tokens_value, 2))


def load_fabianpro(conn):
    """FabianPro — estructura+ADX+ATR+cartera."""
    runs = ROOT / 'runtime/polymarket/runs'
    if runs.exists():
        for entry in runs.glob('fabianpro_*'):
            tl = entry / 'trades_log.csv'
            if tl.exists():
                with tl.open() as f:
                    for x in csv.DictReader(f):
                        ts_str = x.get('ts', '')
                        try:
                            ts = parse_ts(ts_str)
                        except Exception:
                            continue
                        pnl = float(x.get('pnl', 0) or 0)
                        action = x.get('action', '').upper()
                        usd = abs(pnl)
                        if action in ('BUY',):
                            side = 'BUY'; result = ''
                        elif action in ('SELL',):
                            side = 'SELL'; result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        elif action in ('SHORT',):
                            side = 'SHORT'; result = ''
                        elif action in ('COVER',):
                            side = 'COVER'; result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        else:
                            side = action; result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        # Saltar filas de entrada sin PnL (crean fantasmas con qty=0)
                        if pnl == 0 and result == '':
                            continue
                        # FabianPro siempre opera SOLUSDT; forzar símbolo si no viene en CSV
                        if 'symbol' not in x or not x.get('symbol'):
                            x['symbol'] = 'SOLUSDT'
                        conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                        values('fabianpro',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                                     [ts, side, usd, usd, pnl, result, json.dumps(x)])
    # Calcular balance desde BD: $100 + suma(PnL de todos los trades)
    r = conn.execute("select coalesce(sum(pnl_usd),0) from trades where bot_name='fabianpro'")
    total_pnl = r.fetchone()[0]
    balance = round(INITIAL_BALANCE + float(total_pnl), 2)
    running = (ROOT / 'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn, 'fabianpro', is_running=running, balance=balance,
                  pnl_day=0, pnl_week=0, tokens=0)


def load_fabian_py(conn):
    """FabiánPullback Python — lee su propio resumen."""
    runs = ROOT / 'runtime/polymarket/runs'
    now = datetime.now(timezone.utc)
    if runs.exists():
        for entry in runs.glob('fabian_*'):
            tl = entry / 'trades_log.csv'
            if tl.exists():
                with tl.open() as f:
                    for x in csv.DictReader(f):
                        ts_str = x.get('ts', '')
                        try:
                            ts = parse_ts(ts_str)
                        except Exception:
                            continue
                        pnl = float(x.get('pnl', 0) or 0)
                        action = x.get('action', '').upper()
                        usd = abs(pnl)
                        # Mapear acciones a side del dashboard
                        if action in ('BUY_STOP', 'BUY'):
                            side = 'BUY'
                            result = ''  # entrada, posición abierta
                        elif action in ('SELL_STOP', 'SHORT'):
                            side = 'SHORT'
                            result = ''  # entrada, posición abierta
                        elif action in ('SELL',):
                            side = 'SELL'  # cierre de largo
                            result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        elif action in ('COVER',):
                            side = 'COVER'  # cierre de corto
                            result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        else:
                            side = action
                            result = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        # Saltar filas de entrada sin PnL (crean fantasmas con qty=0)
                        if pnl == 0 and result == '':
                            continue
                        # Fabian Python siempre opera SOLUSDT; forzar símbolo si no viene en CSV
                        if 'symbol' not in x or not x.get('symbol'):
                            x['symbol'] = 'SOLUSDT'
                        conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                        values('fabian_py',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                                     [ts, side, usd, usd, pnl, result, json.dumps(x)])
    # Calcular balance desde BD: $100 + suma(PnL de todos los trades)
    r = conn.execute("select coalesce(sum(pnl_usd),0) from trades where bot_name='fabian_py'")
    total_pnl = r.fetchone()[0]
    balance = round(INITIAL_BALANCE + float(total_pnl), 2)
    running = (ROOT / 'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn, 'fabian_py', is_running=running, balance=balance,
                  pnl_day=0, pnl_week=0, tokens=0)


def load_turtle(conn):
    """TurtleBot — Donchian breakout."""
    runs = ROOT / 'runtime/polymarket/runs'
    if runs.exists():
        for entry in runs.glob('turtle_*'):
            tl = entry / 'trades_log.csv'
            if tl.exists():
                with tl.open() as f:
                    for x in csv.DictReader(f):
                        ts_str = x.get('ts', '')
                        try:
                            ts = parse_ts(ts_str)
                        except Exception:
                            continue
                        pnl = float(x.get('pnl', 0) or 0)
                        side = x.get('side', '').upper()
                        usd = abs(pnl)
                        if side in ('LONG_ENTRY',):
                            s = 'BUY'; r = ''
                        elif side in ('SHORT_ENTRY',):
                            s = 'SHORT'; r = ''
                        elif side in ('SELL', 'CLOSE'):
                            s = 'SELL'; r = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        elif side in ('COVER',):
                            s = 'COVER'; r = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        else:
                            s = side; r = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'FLAT'
                        conn.execute('''insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                        values('turtle',%s,%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing''',
                                     [ts, s, usd, usd, pnl, r, json.dumps(x)])
    balance = INITIAL_BALANCE
    latest_mtime = 0
    for entry in sorted((ROOT / 'runtime/polymarket/runs').iterdir()):
        if entry.name.startswith('turtle_'):
            s = entry / 'summary.json'
            if s.exists():
                mt = s.stat().st_mtime
                if mt > latest_mtime:
                    latest_mtime = mt
                    try:
                        d = json.loads(s.read_text())
                        balance = float(d.get('final_balance', INITIAL_BALANCE))
                    except Exception:
                        pass
    running = (ROOT / 'runtime/polymarket/last_runner_status.json').exists()
    upsert_status(conn, 'turtle', is_running=running, balance=balance,
                  pnl_day=0, pnl_week=0, tokens=0)


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        ensure_schema(conn)
        load_poly(conn)
        load_fabian(conn)
        load_pfolio(conn)
        load_fabian_py(conn)
        load_fabianpro(conn)


if __name__ == '__main__':
    main()
