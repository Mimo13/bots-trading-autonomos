#!/usr/bin/env python3
"""Scalping 5m Bot — confirmación doble (estructura + momentum) + hard kills."""
from __future__ import annotations
import argparse,csv,json
from dataclasses import dataclass,asdict
from pathlib import Path
from math import isclose

@dataclass
class Cfg:
    initial_balance: float = 100.0
    risk_per_trade: float = 0.02
    max_daily_loss_pct: float = 5.0
    max_trades_per_day: int = 8
    max_consecutive_losses: int = 3
    cooldown_after_loss_streak_min: int = 240
    session_start_utc: str = "07:00"
    session_end_utc: str = "19:00"
    min_atr_ratio: float = 0.0008
    ema_fast: int = 8
    ema_slow: int = 21
    rsi_period: int = 7
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    rsi_entry_bullish_min: float = 55.0
    rsi_entry_bearish_max: float = 45.0
    adx_min: float = 22.0
    sl_atr_multiplier: float = 1.0
    tp_atr_multiplier: float = 1.5
    min_rr: float = 1.2
    max_spread_pct: float = 0.001
    symbols: tuple = ("SOLUSDT",)


def load_rows(path: Path):
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    'ts': r.get('timestamp_utc') or r.get('ts',''),
                    'open': float(r.get('open') or 0),
                    'high': float(r.get('high') or 0),
                    'low': float(r.get('low') or 0),
                    'close': float(r.get('close') or 0),
                    'volume': float(r.get('volume') or 0),
                    'p_model_up': float(r.get('p_model_up') or 0.5),
                    'symbol': r.get('instrument') or r.get('symbol','SOLUSDT')
                })
            except Exception:
                continue
    return rows


def ema(prev, x, n):
    a = 2 / (n + 1)
    return x if prev is None else (a * x + (1 - a) * prev)


def calc_rsi(closes, period):
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i-1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / period
    avg_l = losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def calc_atr(rows, period):
    if len(rows) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        h = rows[i]['high']
        l = rows[i]['low']
        pc = rows[i-1]['close']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def calc_adx(rows, period):
    if len(rows) < period * 2 + 1:
        return 20.0
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(-period, 0):
        h = rows[i]['high']
        l = rows[i]['low']
        pc = rows[i-1]['close']
        up = h - rows[i-1]['high']
        down = rows[i-1]['low'] - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs or sum(trs) == 0:
        return 20.0
    sum_tr = sum(trs)
    pdi = sum(plus_dm) / sum_tr * 100
    ndi = sum(minus_dm) / sum_tr * 100
    if isclose(pdi + ndi, 0):
        return 20.0
    return abs(pdi - ndi) / (pdi + ndi) * 100


def in_session(ts_str, cfg):
    if not ts_str:
        return True
    try:
        h = int(ts_str[11:13])
        m = int(ts_str[14:16])
    except Exception:
        return True
    mins = h * 60 + m
    sh, sm = map(int, cfg.session_start_utc.split(':'))
    eh, em = map(int, cfg.session_end_utc.split(':'))
    return mins >= sh * 60 + sm and mins <= eh * 60 + em


def run(rows, cfg: Cfg, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    dlog = out_dir / 'decisions_log.csv'
    tlog = out_dir / 'trades_log.csv'
    summary = out_dir / 'summary.json'

    bal = cfg.initial_balance
    pos = None
    wins = losses = trades = 0
    day_count = {}
    consec_losses = 0
    cooldown_until = ''
    ef = None
    es = None

    with dlog.open('w', newline='') as df, tlog.open('w', newline='') as tf:
        dw = csv.DictWriter(df, fieldnames=['ts', 'symbol', 'action', 'reason', 'price', 'balance'])
        tw = csv.DictWriter(tf, fieldnames=['ts', 'side', 'qty', 'usd_amount', 'pnl', 'symbol'])
        dw.writeheader()
        tw.writeheader()

        for i, r in enumerate(rows):
            ts = r['ts']
            c = r['close']
            sym = r['symbol']
            day = ts[:10] if ts else 'unknown'

            # Cooldown check
            if cooldown_until and ts < cooldown_until:
                continue

            if day not in day_count:
                day_count[day] = 0

            # Session & ATR filters
            if not in_session(ts, cfg):
                dw.writerow({'ts': ts, 'symbol': sym, 'action': 'NO_TRADE',
                             'reason': 'OUT_OF_SESSION', 'price': c, 'balance': round(bal, 2)})
                continue

            atr = calc_atr(rows[:i+1], 14)
            if atr == 0 or (atr / max(c, 1e-9)) < cfg.min_atr_ratio:
                dw.writerow({'ts': ts, 'symbol': sym, 'action': 'NO_TRADE',
                             'reason': 'LOW_VOL', 'price': c, 'balance': round(bal, 2)})
                continue

            adx = calc_adx(rows[:i+1], 14)
            if adx < cfg.adx_min:
                dw.writerow({'ts': ts, 'symbol': sym, 'action': 'NO_TRADE',
                             'reason': 'LOW_ADX', 'price': c, 'balance': round(bal, 2)})
                continue

            # Update EMAs
            ef = ema(ef, c, cfg.ema_fast)
            es = ema(es, c, cfg.ema_slow)

            if ef is None or es is None or i < cfg.ema_slow:
                continue

            opens = [x['close'] for x in rows[:i+1]]
            rsi = calc_rsi(opens, cfg.rsi_period)
            regime = 'BULL' if ef > es else 'BEAR'
            daily_loss_pct = ((bal - cfg.initial_balance) / cfg.initial_balance) * 100
            daily_exhausted = day_count[day] >= cfg.max_trades_per_day
            daily_loss_floor = daily_loss_pct <= -cfg.max_daily_loss_pct

            reason = ''
            action = 'NO_TRADE'

            # Entry logic
            if pos is None and not daily_exhausted and not daily_loss_floor:
                if regime == 'BULL' and rsi >= cfg.rsi_entry_bullish_min and rsi < cfg.rsi_overbought:
                    usd = max(3.0, bal * cfg.risk_per_trade)
                    qty = usd / max(c, 1e-9)
                    pos = {'side': 'BUY', 'entry': c, 'qty': qty, 'usd': usd, 'symbol': sym,
                           'sl': c - atr * cfg.sl_atr_multiplier,
                           'tp': c + atr * cfg.tp_atr_multiplier}
                    day_count[day] += 1
                    action = 'BUY'
                    reason = 'BULL_RSI'
                elif regime == 'BEAR' and rsi <= cfg.rsi_entry_bearish_max and rsi > cfg.rsi_oversold:
                    usd = max(3.0, bal * cfg.risk_per_trade)
                    qty = usd / max(c, 1e-9)
                    pos = {'side': 'SHORT', 'entry': c, 'qty': qty, 'usd': usd, 'symbol': sym,
                           'sl': c + atr * cfg.sl_atr_multiplier,
                           'tp': c - atr * cfg.tp_atr_multiplier}
                    day_count[day] += 1
                    action = 'SHORT'
                    reason = 'BEAR_RSI'

                if action != 'NO_TRADE':
                    tw.writerow({'ts': ts, 'side': action, 'qty': round(qty, 6),
                                 'usd_amount': round(usd, 2), 'pnl': 0, 'symbol': sym})
                    dw.writerow({'ts': ts, 'symbol': sym, 'action': action, 'reason': reason,
                                 'price': c, 'balance': round(bal, 2)})

            # Position management
            elif pos is not None:
                pnl = 0.0
                exit_side = None

                if pos['side'] == 'BUY':
                    ret = (c - pos['entry']) / max(pos['entry'], 1e-9)
                    if c >= pos['tp'] or c <= pos['sl'] or regime == 'BEAR':
                        pnl = pos['usd'] * ret
                        exit_side = 'SELL'
                        if regime == 'BEAR':
                            reason = 'REGIME_REVERSE'
                        elif c >= pos['tp']:
                            reason = 'TP_HIT'
                        else:
                            reason = 'SL_HIT'
                else:
                    ret = (pos['entry'] - c) / max(pos['entry'], 1e-9)
                    if c <= pos['tp'] or c >= pos['sl'] or regime == 'BULL':
                        pnl = pos['usd'] * ret
                        exit_side = 'COVER'
                        if regime == 'BULL':
                            reason = 'REGIME_REVERSE'
                        elif c <= pos['tp']:
                            reason = 'TP_HIT'
                        else:
                            reason = 'SL_HIT'

                if exit_side:
                    bal += pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                        consec_losses = 0
                    else:
                        losses += 1
                        consec_losses += 1

                    # Hard kill: consecutive losses
                    if consec_losses >= cfg.max_consecutive_losses:
                        # Cooldown for remainder of day + next cooldown minutes
                        cd_hours = cfg.cooldown_after_loss_streak_min // 60
                        cd_mins = cfg.cooldown_after_loss_streak_min % 60
                        if ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
                                from datetime import timedelta
                                cd_end = dt + timedelta(hours=cd_hours, minutes=cd_mins)
                                cooldown_until = cd_end.isoformat()
                            except Exception:
                                cooldown_until = ts
                        consec_losses = 0

                    tw.writerow({'ts': ts, 'side': exit_side, 'qty': round(pos['qty'], 6),
                                 'usd_amount': round(pos['usd'], 2), 'pnl': round(pnl, 4), 'symbol': pos['symbol']})
                    dw.writerow({'ts': ts, 'symbol': pos['symbol'], 'action': exit_side,
                                 'reason': reason, 'price': c, 'balance': round(bal, 2)})
                    pos = None

    out = {
        'initial_balance': cfg.initial_balance,
        'final_balance': round(bal, 2),
        'total_pnl': round(bal - cfg.initial_balance, 2),
        'total_trades': trades,
        'wins': wins,
        'losses': losses,
        'win_rate_percent': round((wins / max(1, trades)) * 100, 2) if trades else 0,
        'config': asdict(cfg),
        'outputs': {
            'decisions_log': str(dlog),
            'trades_log': str(tlog),
            'summary_json': str(summary)
        }
    }
    summary.write_text(json.dumps(out, indent=2))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--config')
    p.add_argument('--output-dir', required=True)
    a = p.parse_args()

    cfg = Cfg()
    if a.config:
        d = json.loads(Path(a.config).read_text())
        for k, v in d.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    rows = load_rows(Path(a.input))
    print(json.dumps(run(rows, cfg, Path(a.output_dir)), indent=2))


if __name__ == '__main__':
    main()
