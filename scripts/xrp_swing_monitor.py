#!/usr/bin/env python3
"""
XRP Swing Monitor — vigilancia y trading automático de XRP en Binance.

Estrategia:
- HOLDING_XRP: espera a que XRP llegue a $2.20+ con señal bajista para vender a USDC
  - Venta por tramos (50% en primera señal, 50% en confirmación)
  - Trailing take-profit: si cae 5% desde el pico, vende todo
- HOLDING_USDC: espera a que XRP baje para recomprar
  - Recompra escalonada: 25% en cada nivel ($2.10, $2.05, $2.00, $1.95)

Estado persistido en runtime/runtime/xrp_swing/state.json
"""

import json
import time
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_DOWN

# ── Configuración ───────────────────────────────────────────────────────────

TARGET_SELL = 2.20          # Precio mínimo para considerar venta
TARGET_SELL_MIN = 2.15      # Precio mínimo si hay señal bajista fuerte
BUY_LEVELS = [2.10, 2.05, 2.00, 1.95]  # Precios de recompra escalonada
TRAILING_STOP_PCT = 5.0     # Si cae X% desde el pico, vender todo
FIRST_TRANCHE_PCT = 50      # % a vender en primera señal
TRAILING_CHECK_MIN = 1.0    # % mínimo desde pico para activar trailing

RSI_OVERBOUGHT = 70         # RSI > 70 = sobrecompra → posible bajada
RSI_OVERSOLD = 30           # RSI < 30 = sobreventa → posible subida
VOLUME_SPIKE = 1.5          # Volumen > 1.5x media = spike

SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SCRIPT_DIR.parent / 'runtime' / 'xrp_swing'
STATE_FILE = RUNTIME_DIR / 'state.json'
LOG_FILE = RUNTIME_DIR / 'decisions.log'


# ── Helpers ─────────────────────────────────────────────────────────────────

def _ensure_dirs():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _log(entry: dict):
    _ensure_dirs()
    entry['ts'] = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        'mode': 'testnet',           # 'testnet' | 'mainnet'
        'state': 'HOLDING_XRP',      # HOLDING_XRP | HOLDING_USDC
        'xrp_qty': 76.37,            # XRP en spot
        'usdc_qty': 0.0,             # USDC en spot
        'peak_price': 0.0,           # Precio más alto desde que estamos en HOLDING_XRP
        'sell_tranches': 0,          # 0, 1, 2 (tramos vendidos)
        'buy_tranches': [],          # niveles de recompra ya ejecutados
        'target_sell': TARGET_SELL,
        'buy_levels': BUY_LEVELS,
        'trailing_stop_pct': TRAILING_STOP_PCT,
        'last_action': None,
        'last_check': None,
    }


def _save_state(state: dict):
    _ensure_dirs()
    state['last_check'] = datetime.now(timezone.utc).isoformat()
    with STATE_FILE.open('w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _get_client(testnet: bool = True):
    """Importar y cargar cliente Binance."""
    sys.path.insert(0, str(SCRIPT_DIR.parent))
    from binance_client import load_client
    return load_client(testnet=testnet)


def _calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0
    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def _validate_quantity(client, symbol: str, qty: float) -> float:
    """Validar cantidad contra LOT_SIZE de Binance."""
    filters = client.get_filters(symbol)
    lot = filters.get("LOT_SIZE", {})
    step = float(lot.get("stepSize", "0.1"))
    min_qty = float(lot.get("minQty", "0.1"))
    qty = round(qty / step) * step
    if qty < min_qty:
        qty = min_qty
    return qty


# ── Lógica de trading ──────────────────────────────────────────────────────

def check_and_trade(dry_run: bool = False) -> dict:
    """Ejecutar una ronda de monitorización. Devuelve resumen de decisiones."""
    state = _load_state()
    is_testnet = state['mode'] == 'testnet'

    # Conectar a Binance
    client = _get_client(testnet=is_testnet)

    # Obtener precio actual
    ticker = client.get_ticker('XRPUSDT')
    price = float(ticker['lastPrice'])
    change_24h = float(ticker.get('priceChangePercent', 0))

    # Obtener velas para análisis técnico
    klines = client.get_klines('XRPUSDT', interval='5m', limit=50)
    closes = [k['close'] for k in klines]
    highs = [k['high'] for k in klines]
    volumes = [k['volume'] for k in klines]

    # Calcular indicadores
    ema20 = _calc_ema(closes, 20)
    ema50 = _calc_ema(closes, 50)
    rsi14 = _calc_rsi(closes, 14)
    avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    last_volume = volumes[-2] if len(volumes) >= 2 else 0  # penúltima vela (completa)
    volume_ratio = last_volume / avg_volume if avg_volume > 0 else 1.0
    recent_high = max(highs[-6:])  # máximos de últimas 6 velas (30 min)
    recent_low = min(k['low'] for k in klines[-6:])

    # Detectar señales
    signals = {}
    signals['above_ema20'] = price > ema20
    signals['above_ema50'] = price > ema50
    signals['rsi_overbought'] = rsi14 > RSI_OVERBOUGHT
    signals['rsi_oversold'] = rsi14 < RSI_OVERSOLD
    signals['volume_spike'] = volume_ratio > VOLUME_SPIKE
    signals['price_rejection'] = recent_high > price and price < closes[-3] * 1.01  # precio cayendo desde máximo
    signals['bullish_cross'] = ema20 > ema50  # golden cross

    # Determinar señal de venta (bearish)
    bearish_signals = 0
    if signals['rsi_overbought']:
        bearish_signals += 1
    if not signals['above_ema20']:
        bearish_signals += 1
    if signals['price_rejection'] and signals['volume_spike']:
        bearish_signals += 2  # señal fuerte
    elif signals['price_rejection']:
        bearish_signals += 1

    # Determinar señal de compra (bullish)
    bullish_signals = 0
    if signals['rsi_oversold']:
        bullish_signals += 1
    if signals['above_ema20']:
        bullish_signals += 1
    if signals['bullish_cross'] and price > ema20:
        bullish_signals += 1

    decisions = []
    orders = []
    new_state = dict(state)

    if state['state'] == 'HOLDING_XRP':
        # Actualizar pico de precio para trailing stop
        if price > state.get('peak_price', 0):
            new_state['peak_price'] = price

        # Trailing stop: si cae TRAILING_STOP_PCT desde el pico
        peak = state.get('peak_price', price)
        if peak > TARGET_SELL_MIN and peak > 0:
            drop_pct = (peak - price) / peak * 100
            if drop_pct >= TRAILING_STOP_PCT:
                decisions.append(f"Trailing stop: +{drop_pct:.1f}% desde pico ${peak:.4f}")
                qty = state.get('xrp_qty', 0)
                if qty > 0.1 and not dry_run:
                    valid_qty = _validate_quantity(client, 'XRPUSDT', qty)
                    order = client.place_market_order('XRPUSDC', 'SELL', valid_qty)
                    orders.append(order)
                    new_state['xrp_qty'] = 0
                    new_state['usdc_qty'] += float(order.get('cummulativeQuoteQty', 0))
                    new_state['sell_tranches'] = 2
                    new_state['state'] = 'HOLDING_USDC'
                    decisions.append(f"VENDIDO TODO: {valid_qty} XRP @ ${price:.4f}")
                elif dry_run:
                    new_state['state'] = 'HOLDING_USDC'
                    decisions.append(f"[DRY] Vender {qty} XRP @ ${price:.4f}")

        # Señal de venta por precio objetivo
        if price >= state.get('target_sell', TARGET_SELL) or \
           (price >= TARGET_SELL_MIN and bearish_signals >= 2):
            qty = state.get('xrp_qty', 0)
            sold_already = state.get('sell_tranches', 0)

            if sold_already < 1 and qty > 0.1:
                # Vender primer tramo (50%)
                sell_qty = qty * FIRST_TRANCHE_PCT / 100
                valid_qty = _validate_quantity(client, 'XRPUSDT', sell_qty)
                if not dry_run:
                    order = client.place_market_order('XRPUSDC', 'SELL', valid_qty)
                    orders.append(order)
                    new_state['xrp_qty'] = round(qty - valid_qty, 4)
                    new_state['usdc_qty'] += float(order.get('cummulativeQuoteQty', 0))
                    new_state['sell_tranches'] = 1
                    decisions.append(f"Tramo 1 vendido: {valid_qty} XRP @ ${price:.4f}")
                else:
                    new_state['xrp_qty'] = round(qty - sell_qty, 4)
                    new_state['sell_tranches'] = 1
                    decisions.append(f"[DRY] Tramo 1: vender {sell_qty} XRP @ ${price:.4f}")

            elif sold_already >= 1 and qty > 0.1 and bearish_signals >= 2:
                # Vender segundo tramo (restante)
                valid_qty = _validate_quantity(client, 'XRPUSDT', qty)
                if not dry_run:
                    order = client.place_market_order('XRPUSDC', 'SELL', valid_qty)
                    orders.append(order)
                    new_state['xrp_qty'] = 0
                    new_state['usdc_qty'] += float(order.get('cummulativeQuoteQty', 0))
                    new_state['sell_tranches'] = 2
                    new_state['state'] = 'HOLDING_USDC'
                    decisions.append(f"Tramo 2 vendido (restante): {valid_qty} XRP @ ${price:.4f}")
                else:
                    new_state['xrp_qty'] = 0
                    new_state['sell_tranches'] = 2
                    new_state['state'] = 'HOLDING_USDC'
                    decisions.append(f"[DRY] Tramo 2: vender {qty} XRP @ ${price:.4f}")

        if not decisions:
            decisions.append(f"Esperando... ${price:.4f} | pico: ${state.get('peak_price', 0):.4f} | señal bajista: {bearish_signals}/3")

    elif state['state'] == 'HOLDING_USDC':
        # Recompra escalonada
        buy_levels = state.get('buy_levels', BUY_LEVELS)
        executed = state.get('buy_tranches', [])
        usdc = state.get('usdc_qty', 0)

        # Precio actual dentro de rango de compra
        for level in buy_levels:
            if price <= level and level not in executed:
                # Cuánto comprar en este nivel
                remaining_levels = [l for l in buy_levels if l not in executed]
                if not remaining_levels:
                    break
                qty_per_level = usdc / len(remaining_levels) if usdc > 0 else 0
                buy_usdc = qty_per_level * 0.98  # dejar margen para comisiones
                buy_qty = buy_usdc / price if price > 0 else 0

                if buy_qty > 0.1 and not dry_run:
                    valid_qty = _validate_quantity(client, 'XRPUSDT', buy_qty)
                    order = client.place_market_order('XRPUSDC', 'BUY', valid_qty)
                    orders.append(order)
                    new_state['xrp_qty'] = state.get('xrp_qty', 0) + valid_qty
                    new_state['usdc_qty'] = usdc - float(order.get('cummulativeQuoteQty', 0))
                    executed = executed + [level]
                    new_state['buy_tranches'] = executed
                    decisions.append(f"Compra nivel ${level:.2f}: {valid_qty} XRP @ ${price:.4f}")
                elif dry_run:
                    executed = executed + [level]
                    new_state['buy_tranches'] = executed
                    decisions.append(f"[DRY] Comprar ${buy_usdc:.2f} de XRP @ ${price:.4f} (nivel ${level:.2f})")

        # Si ya ejecutamos todos los niveles o XRP subió de vuelta
        if len(executed) >= len(buy_levels) or (price > buy_levels[0] * 1.05 and executed):
            # Volver a HOLDING_XRP
            new_state['state'] = 'HOLDING_XRP'
            new_state['peak_price'] = price
            new_state['sell_tranches'] = 0
            new_state['buy_tranches'] = []
            decisions.append(f"Vuelta a HOLDING_XRP (${price:.4f})")

        if not decisions:
            decisions.append(f"Esperando recompra... ${price:.4f} | niveles: {[l for l in buy_levels if l not in executed]}")

    # Ensamblar resultado

    result = {
        'ok': True,
        'mode': state['mode'],
        'state': new_state['state'],
        'price': round(price, 6),
        'change_24h': round(change_24h, 2),
        'indicators': {
            'ema20': round(ema20, 4),
            'ema50': round(ema50, 4),
            'rsi14': round(rsi14, 1),
            'volume_ratio': round(volume_ratio, 2),
        },
        'signals': signals,
        'bearish_signals': bearish_signals,
        'bullish_signals': bullish_signals,
        'xrp_qty': round(new_state.get('xrp_qty', 0), 4),
        'usdc_qty': round(new_state.get('usdc_qty', 0), 4),
        'peak_price': round(new_state.get('peak_price', 0), 4),
        'sell_tranches': new_state.get('sell_tranches', 0),
        'buy_tranches': new_state.get('buy_tranches', []),
        'decisions': decisions,
        'orders': len(orders),
        'dry_run': dry_run,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    # Guardar estado y log
    new_state['price'] = price
    new_state['change_24h'] = round(change_24h, 2)
    new_state['indicators'] = result['indicators']
    new_state['signals'] = signals
    new_state['bearish_signals'] = bearish_signals
    new_state['bullish_signals'] = bullish_signals
    _save_state(new_state)

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='XRP Swing Monitor')
    parser.add_argument('--mode', choices=['testnet', 'mainnet', 'dry'], default='testnet',
                        help='Modo de operación')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simular sin órdenes reales')
    parser.add_argument('--set-mode', choices=['testnet', 'mainnet'],
                        help='Cambiar modo persistente')
    parser.add_argument('--status', action='store_true',
                        help='Mostrar estado actual')
    args = parser.parse_args()

    state = _load_state()

    if args.set_mode:
        state['mode'] = args.set_mode
        _save_state(state)
        print(f"Modo cambiado a: {args.set_mode}")
        sys.exit(0)

    if args.status:
        state = _load_state()
        print(json.dumps(state, indent=2, ensure_ascii=False))
        sys.exit(0)

    if args.mode:
        state['mode'] = args.mode
        _save_state(state)

    dry = args.dry_run or state['mode'] == 'dry'
    result = check_and_trade(dry_run=dry)
    print(json.dumps(result, indent=2, ensure_ascii=False))
