#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json
from dataclasses import dataclass,asdict
from pathlib import Path

@dataclass
class Cfg:
    initial_balance: float = 100.0
    risk_per_trade: float = 0.03
    lookback: int = 20
    breakout_buffer: float = 0.001
    tp_pct: float = 0.025
    sl_pct: float = 0.012
    max_trades_per_day: int = 8


def load_rows(path: Path):
    rows=[]
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append({'ts':r.get('timestamp_utc') or '', 'open':float(r.get('open') or 0), 'high':float(r.get('high') or 0), 'low':float(r.get('low') or 0), 'close':float(r.get('close') or 0), 'symbol':r.get('instrument') or 'SOLUSDT'})
            except Exception:
                continue
    return rows


def run(rows,cfg:Cfg,out_dir:Path):
    out_dir.mkdir(parents=True,exist_ok=True)
    dlog=out_dir/'decisions_log.csv'; tlog=out_dir/'trades_log.csv'; summary=out_dir/'summary.json'
    bal=cfg.initial_balance
    pos=None; wins=losses=trades=0; day_count={}
    with dlog.open('w',newline='') as df, tlog.open('w',newline='') as tf:
        dw=csv.DictWriter(df,fieldnames=['ts','symbol','action','reason','price','balance'])
        tw=csv.DictWriter(tf,fieldnames=['ts','side','qty','usd_amount','pnl','symbol'])
        dw.writeheader(); tw.writeheader()
        for i,r in enumerate(rows):
            ts=r['ts']; c=r['close']; h=r['high']; l=r['low']; sym=r['symbol']
            d=(ts[:10] if ts else 'unknown'); day_count.setdefault(d,0)
            if i<cfg.lookback: continue
            hh=max(x['high'] for x in rows[i-cfg.lookback:i])
            ll=min(x['low'] for x in rows[i-cfg.lookback:i])
            if pos is None and day_count[d] < cfg.max_trades_per_day:
                if c>hh*(1+cfg.breakout_buffer):
                    usd=max(5.0,bal*cfg.risk_per_trade); qty=usd/max(c,1e-9)
                    pos={'side':'BUY','entry':c,'qty':qty,'usd':usd,'symbol':sym}; day_count[d]+=1
                    tw.writerow({'ts':ts,'side':'BUY','qty':round(qty,6),'usd_amount':round(usd,2),'pnl':0,'symbol':sym})
                    dw.writerow({'ts':ts,'symbol':sym,'action':'BUY','reason':'BOX_BREAKOUT_UP','price':c,'balance':round(bal,2)})
                elif c<ll*(1-cfg.breakout_buffer):
                    usd=max(5.0,bal*cfg.risk_per_trade); qty=usd/max(c,1e-9)
                    pos={'side':'SHORT','entry':c,'qty':qty,'usd':usd,'symbol':sym}; day_count[d]+=1
                    tw.writerow({'ts':ts,'side':'SHORT','qty':round(qty,6),'usd_amount':round(usd,2),'pnl':0,'symbol':sym})
                    dw.writerow({'ts':ts,'symbol':sym,'action':'SHORT','reason':'BOX_BREAKOUT_DOWN','price':c,'balance':round(bal,2)})
            elif pos is not None:
                exit_side=None; pnl=0.0
                if pos['side']=='BUY':
                    ret=(c-pos['entry'])/max(pos['entry'],1e-9)
                    if ret>=cfg.tp_pct or ret<=-cfg.sl_pct or c<ll:
                        pnl=pos['usd']*ret; exit_side='SELL'
                else:
                    ret=(pos['entry']-c)/max(pos['entry'],1e-9)
                    if ret>=cfg.tp_pct or ret<=-cfg.sl_pct or c>hh:
                        pnl=pos['usd']*ret; exit_side='COVER'
                if exit_side:
                    bal+=pnl; trades+=1; wins += 1 if pnl>0 else 0; losses += 1 if pnl<0 else 0
                    tw.writerow({'ts':ts,'side':exit_side,'qty':round(pos['qty'],6),'usd_amount':round(pos['usd'],2),'pnl':round(pnl,4),'symbol':pos['symbol']})
                    dw.writerow({'ts':ts,'symbol':pos['symbol'],'action':exit_side,'reason':'TP/SL_OR_FAIL','price':c,'balance':round(bal,2)})
                    pos=None
    out={'initial_balance':cfg.initial_balance,'final_balance':round(bal,2),'total_pnl':round(bal-cfg.initial_balance,2),'total_trades':trades,'wins':wins,'losses':losses,'win_rate_percent':round((wins/max(1,trades))*100,2),'config':asdict(cfg),'outputs':{'decisions_log':str(dlog),'trades_log':str(tlog),'summary_json':str(summary)}}
    summary.write_text(json.dumps(out,indent=2))
    return out

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True); p.add_argument('--config'); p.add_argument('--output-dir',required=True); a=p.parse_args()
    cfg=Cfg()
    if a.config:
        d=json.loads(Path(a.config).read_text())
        for k,v in d.items():
            if hasattr(cfg,k): setattr(cfg,k,v)
    rows=load_rows(Path(a.input)); print(json.dumps(run(rows,cfg,Path(a.output_dir)),indent=2))

if __name__=='__main__':
    main()
