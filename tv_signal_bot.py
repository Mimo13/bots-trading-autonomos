#!/usr/bin/env python3
"""TV Signal Bot — opera siguiendo la señal de TradingView en tiempo real.
Lee el archivo ctrader_signal.csv (escrito cada 5min por el bridge)
y ejecuta trades en la dirección recomendada si la confianza > umbral.
"""
from __future__ import annotations
import csv, json, logging, time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('tv_signal_bot')

ROOT = Path(__file__).resolve().parent
SIGNAL_PATH = ROOT / 'runtime/tradingview/ctrader_signal.csv'
STATUS_DIR = ROOT / 'runtime/testnet'
INITIAL_BALANCE = 100.0

# Config
CONFIDENCE_MIN = 0.40       # Mínima confianza para operar
RISK_PER_TRADE = 0.02       # 2% del balance por trade
COOLDOWN_TRADES = 2         # Nº trades antes de pausa 1h
MAX_TRADES_PER_DAY = 8
ATR_PERIOD = 14
SL_ATR_MULT = 1.0
TP_ATR_MULT = 1.5
POSITION_EXPIRY_BARS = 12   # ~1h en 5m


def read_signal() -> dict | None:
    """Leer la última señal de TradingView del CSV."""
    if not SIGNAL_PATH.exists():
        return None
    try:
        with SIGNAL_PATH.open() as f:
            rows = list(csv.DictReader(f))
            if not rows:
                return None
            last = rows[-1]
            return {
                'ts': last.get('timestamp_utc', ''),
                'symbol': last.get('symbol', 'UNKNOWN'),
                'recommendation': last.get('recommendation', '').upper(),
                'confidence': float(last.get('confidence', 0) or 0),
            }
    except Exception as e:
        logger.warning(f'Error leyendo señal: {e}')
        return None


def fetch_live_price(symbol_key: str) -> float | None:
    """Obtener precio actual del live feed."""
    feed = ROOT / 'runtime/live' / f'{symbol_key}USDT_5m.csv'
    if feed.exists():
        try:
            with feed.open() as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return float(rows[-1].get('close', 0))
        except Exception:
            pass
    return None


def atr_from_feed(symbol_key: str) -> float:
    """Calcular ATR desde el feed de datos."""
    feed = ROOT / 'runtime/live' / f'{symbol_key}USDT_5m.csv'
    if not feed.exists():
        return 0.0
    try:
        with feed.open() as f:
            rows = list(csv.DictReader(f))
        if len(rows) < ATR_PERIOD + 1:
            return 0.0
        trs = []
        for i in range(-ATR_PERIOD, 0):
            h = float(rows[i]['high'])
            l = float(rows[i]['low'])
            pc = float(rows[i - 1]['close'])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0
    except Exception:
        return 0.0


def run_cycle(symbol_key: str, label: str = 'TV'):
    """Ciclo principal: leer señal → decidir → simular."""
    balance = INITIAL_BALANCE
    trades_today = 0
    last_reset_day = -1
    position = None
    consecutive_trades = 0
    trade_count = 0
    wins = 0
    losses = 0

    print(f"[{label}] TV Signal Bot iniciado para {symbol_key}")

    while True:
        signal = read_signal()
        price = fetch_live_price(symbol_key)

        if price is None:
            time.sleep(10)
            continue

        # Reset diario
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != last_reset_day:
            trades_today = 0
            consecutive_trades = 0
            last_reset_day = today

        # Gestión de posición existente
        if position is not None:
            atr = atr_from_feed(symbol_key)
            if position['side'] == 'BUY':
                ret = (price - position['entry']) / max(position['entry'], 1e-9)
                sl_hit = price <= position['sl']
                tp_hit = price >= position['tp']
            else:
                ret = (position['entry'] - price) / max(position['entry'], 1e-9)
                sl_hit = price >= position['sl']
                tp_hit = price <= position['tp']

            if sl_hit or tp_hit:
                pnl = position['usd'] * ret
                balance += pnl
                trade_count += 1
                consecutive_trades += 1
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                logger.info(f"[{label}] Cerrado {position['side']} PnL={pnl:.4f} Bal={balance:.2f} ({'TP' if tp_hit else 'SL'})")
                position = None

        # Buscar nueva entrada
        if position is None and trades_today < MAX_TRADES_PER_DAY:
            if signal and signal['recommendation'] in ('BUY', 'SELL') and signal['confidence'] >= CONFIDENCE_MIN:
                direction = 'BUY' if signal['recommendation'] == 'BUY' else 'SHORT'
                usd = max(3.0, balance * RISK_PER_TRADE)
                qty = usd / max(price, 1e-9)
                atr = atr_from_feed(symbol_key)
                if atr > 0:
                    sl = price - atr * SL_ATR_MULT if direction == 'BUY' else price + atr * SL_ATR_MULT
                    tp = price + atr * TP_ATR_MULT if direction == 'BUY' else price - atr * TP_ATR_MULT
                    position = {'side': direction, 'entry': price, 'qty': qty, 'usd': usd, 'sl': sl, 'tp': tp}
                    trades_today += 1
                    logger.info(f"[{label}] Entrada {direction} @ ${price:.4f} conf={signal['confidence']:.2f} sl=${sl:.4f} tp=${tp:.4f}")

        # Guardar estado
        status = {
            'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'symbol': symbol_key,
            'balance': round(balance, 2),
            'trades': trade_count,
            'wins': wins,
            'losses': losses,
            'position': position,
            'signal': signal,
        }
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        (STATUS_DIR / f'{label}_status.json').write_text(json.dumps(status, indent=2, default=str))

        time.sleep(10)


if __name__ == '__main__':
    import threading

    bot1 = threading.Thread(target=run_cycle, args=('SOL', 'TV_SOL'), daemon=True)
    bot2 = threading.Thread(target=run_cycle, args=('ADA', 'TV_ADA'), daemon=True)
    bot3 = threading.Thread(target=run_cycle, args=('SOL', 'TV_PRO'), daemon=True)

    bot1.start()
    bot2.start()
    bot3.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Deteniendo bots TV Signal...")
