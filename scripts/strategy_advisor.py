#!/usr/bin/env python3
from __future__ import annotations
import json, os
from datetime import datetime, timezone
from pathlib import Path
import psycopg

ROOT=Path('/Volumes/Almacen/Desarrollo/bots-trading-autonomos')
DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')


def recommend_for_bot(conn, bot:str):
    rows = conn.execute("""
      select coalesce(sum(pnl_usd),0),
             coalesce(avg(case when pnl_usd>0 then 1 else 0 end),0),
             count(*)
      from trades
      where bot_name=%s and ts >= now() - interval '7 day'
    """, [bot]).fetchone()
    pnl7, win_rate, n = float(rows[0]), float(rows[1]), int(rows[2])

    recs=[]
    confidence=0.55
    if n < 20:
        recs.append({'type':'data','action':'Aumentar muestra mínima a 50 trades antes de tocar parámetros','risk':'low','applicable':False})
        confidence=0.45
    if pnl7 < 0:
        recs.append({'type':'risk','action':'Reducir max_risk_per_trade un 10-20% para mañana','risk':'medium','applicable':True,'patch':{'max_risk_per_trade':'-10%'}})
        recs.append({'type':'filter','action':'Subir edge_min +0.005 temporalmente','risk':'medium','applicable':True,'patch':{'edge_min':'+0.005'}})
    if win_rate < 0.45 and n >= 20:
        recs.append({'type':'quality','action':'Endurecer tv_min_confidence +0.03','risk':'medium','applicable':True,'patch':{'tv_min_confidence':'+0.03'}})
    if pnl7 > 0 and win_rate > 0.55 and n >= 20:
        recs.append({'type':'scale','action':'Mantener parámetros; evaluar incremento de stake +5% solo en paper','risk':'low','applicable':True,'patch':{'max_risk_per_trade':'+5% paper-only'}})
        confidence=0.7

    if not recs:
        recs.append({'type':'hold','action':'Mantener configuración actual y seguir monitorización','risk':'low','applicable':False})

    summary=f"{bot}: pnl7d={pnl7:.2f}, win_rate={win_rate*100:.1f}%, trades={n}"
    conn.execute("""
      insert into strategy_recommendations(bot_name, summary, recommendations, confidence)
      values (%s,%s,%s::jsonb,%s)
    """, [bot, summary, json.dumps(recs), confidence])


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        recommend_for_bot(conn, 'fabian')
        recommend_for_bot(conn, 'poly')
    print(json.dumps({'ok':True,'ts':datetime.now(timezone.utc).isoformat()}))

if __name__=='__main__':
    main()
