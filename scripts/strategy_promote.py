#!/usr/bin/env python3
from __future__ import annotations
import json, os
import psycopg

DB_URL=os.getenv('DATABASE_URL','postgresql:///bots_dashboard')

MIN_DELTA_PNL = 5.0
MIN_CANDIDATE_WR = 52.0
MIN_BASELINE_TRADES = 20


def main():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(open('/Volumes/Almacen/Desarrollo/bots-trading-autonomos/sql/schema.sql').read())
        row = conn.execute('''
          select id, bot_name, delta_pnl, candidate_win_rate, baseline_win_rate, config_patch
          from strategy_ab_tests
          order by ts desc
          limit 1
        ''').fetchone()

        if not row:
            conn.execute('''insert into strategy_promotions(bot_name, decision, reason, applied)
                            values('poly','hold','No hay A/B test previo',false)''')
            print(json.dumps({'ok':True,'decision':'hold','reason':'no-ab-test'}))
            return

        ab_id, bot_name, delta_pnl, cwr, bwr, patch = row
        delta_pnl = float(delta_pnl or 0)
        cwr = float(cwr or 0)
        bwr = float(bwr or 0)

        # seguridad: solo paper, sin aplicación automática
        if delta_pnl >= MIN_DELTA_PNL and cwr >= MIN_CANDIDATE_WR and cwr >= bwr:
            decision='promote_candidate_paper'
            reason=f'Mejora sólida: delta_pnl={delta_pnl:.2f}, cwr={cwr:.2f}%'
        else:
            decision='hold'
            reason=f'No cumple umbrales: delta_pnl={delta_pnl:.2f}, cwr={cwr:.2f}%, bwr={bwr:.2f}%'

        conn.execute('''
          insert into strategy_promotions(bot_name, decision, reason, ab_test_id, proposed_patch, applied)
          values(%s,%s,%s,%s,%s::jsonb,false)
        ''', [bot_name, decision, reason, ab_id, json.dumps(patch or {})])

    print(json.dumps({'ok':True,'decision':decision,'reason':reason}))


if __name__=='__main__':
    main()
