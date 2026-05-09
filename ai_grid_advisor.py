#!/usr/bin/env python3
"""AI Grid Advisor — calcula el grid óptimo para XRP usando LLM + datos disponibles.
Usa las fuentes que tengan API key configurada; las que falten se saltan.
Ejecución recomendada: cada 6-12h.
"""
from __future__ import annotations
import json, logging, os, subprocess, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('ai_grid')

ROOT = Path(__file__).resolve().parent


def _get_gh_token() -> str:
    try:
        r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        return ''
    return ''


def _call_llm(system: str, user: str, timeout: int = 15) -> Optional[dict]:
    token = _get_gh_token()
    if not token:
        logger.warning("No GitHub token")
        return None
    try:
        payload = json.dumps({
            'model': 'gpt-4.1',
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'temperature': 0.2,
            'max_tokens': 500,
            'response_format': {'type': 'json_object'},
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.githubcopilot.com/chat/completions',
            data=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            content = data['choices'][0]['message']['content']
            return json.loads(content)
    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        return None


def gather_market_data() -> dict:
    """Recolectar datos de todas las fuentes disponibles."""
    data = {'sources_available': {}, 'sources_missing': []}
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Free sources (siempre disponibles, sin API key)
    try:
        from data_sources.free_sources import fetch_all_free
        free_data = fetch_all_free()
        if free_data and free_data.get('items'):
            data['free_sources'] = free_data
            data['sources_available']['binance_futures'] = True
            data['sources_available']['coingecko'] = True
    except Exception as e:
        logger.warning(f'Free sources failed: {e}')
        data['sources_available']['free_sources'] = False

    # CoinMarketCap
    try:
        from data_sources.coinmarketcap_feed import fetch_global_metrics, fetch_xrp_price
        g = fetch_global_metrics()
        if g:
            data['global_metrics'] = g
            data['sources_available']['coinmarketcap'] = True
        xrp_p = fetch_xrp_price()
        if xrp_p:
            data['xrp_price'] = xrp_p
    except Exception as e:
        data['sources_available']['coinmarketcap'] = False
        data['sources_missing'].append('coinmarketcap')

    # Coinglass
    try:
        from data_sources.coinglass_feed import fetch_open_interest, fetch_funding_rate
        oi = fetch_open_interest()
        if oi:
            data['open_interest'] = oi
            data['sources_available']['coinglass'] = True
        fr = fetch_funding_rate()
        if fr:
            data['funding_rate'] = fr
    except Exception as e:
        data['sources_available']['coinglass'] = False
        data['sources_missing'].append('coinglass')

    # XRPScan
    try:
        from data_sources.xrpscan_feed import fetch_market_overview
        xrp = fetch_market_overview()
        if xrp:
            data['xrpscan'] = xrp
            data['sources_available']['xrpscan'] = True
    except Exception as e:
        data['sources_available']['xrpscan'] = False
        data['sources_missing'].append('xrpscan')

    # TradingView signal (del bridge existente)
    sig_path = ROOT / 'runtime/tradingview/ctrader_signal.csv'
    if sig_path.exists():
        try:
            import csv
            with sig_path.open() as f:
                for r in csv.DictReader(f):
                    data['tradingview_signal'] = r
                    data['sources_available']['tradingview'] = True
                    break
        except Exception:
            data['sources_available']['tradingview'] = False
            data['sources_missing'].append('tradingview')
    else:
        data['sources_available']['tradingview'] = False
        data['sources_missing'].append('tradingview')

    data['ts'] = ts
    return data


SYSTEM_PROMPT = """Eres un asesor de trading especializado en grids para XRP. 
Tu función es analizar datos de mercado y proponer un grid óptimo.

Responde ÚNICAMENTE con JSON:
{
  "grid_min_price": float,   // precio mínimo del grid
  "grid_max_price": float,   // precio máximo del grid
  "grid_levels": int,        // número de niveles (entre 6 y 20)
  "confidence": float,       // 0-1 confianza en la recomendación
  "reason": string,          // explicación breve
  "market_regime": "bullish" | "bearish" | "neutral",
  "next_rebalance_hours": int // en cuántas horas recalcular (6 o 12)
}

REGLAS:
- grid_min_price debe ser soporte razonable (no un precio irreal)
- grid_max_price debe ser un nivel de resistencia o toma de ganancias
- Si hay datos insuficientes, baja confidence
- XRP es un activo volátil, deja márgenes amplios
- Precios actuales de XRP ~$2-3 (2025-2026)"""


def calculate_grid(market_data: dict) -> dict:
    """Usar LLM + datos de mercado para calcular grid óptimo."""
    user_msg = json.dumps(market_data, indent=2, default=str)
    result = _call_llm(SYSTEM_PROMPT, user_msg)

    if result is None:
        # Fallback: grid conservador
        current_price = market_data.get('xrp_price', 2.50)
        return {
            'grid_min_price': round(current_price * 0.6, 2),
            'grid_max_price': round(current_price * 1.6, 2),
            'grid_levels': 10,
            'confidence': 0.3,
            'reason': 'LLM no disponible, grid conservador por defecto',
            'market_regime': 'neutral',
            'next_rebalance_hours': 6,
        }

    # Validar campos mínimos
    for field in ['grid_min_price', 'grid_max_price', 'grid_levels']:
        if field not in result:
            current_price = market_data.get('xrp_price', 2.50)
            result['grid_min_price'] = round(current_price * 0.6, 2)
            result['grid_max_price'] = round(current_price * 1.6, 2)
            result['grid_levels'] = 10
            break

    result.setdefault('confidence', 0.5)
    result.setdefault('reason', 'Cálculo IA')
    result.setdefault('market_regime', 'neutral')
    result.setdefault('next_rebalance_hours', 6)

    return result


def run_advisor() -> dict:
    """Ejecutar recolección de datos + cálculo de grid."""
    logger.info("Recolectando datos de mercado...")
    market_data = gather_market_data()
    logger.info(f"Fuentes disponibles: {list(market_data.get('sources_available', {}).keys())}")
    logger.info(f"Fuentes faltantes: {market_data.get('sources_missing', [])}")

    logger.info("Calculando grid óptimo con IA...")
    grid = calculate_grid(market_data)

    # Guardar resultado
    out_dir = ROOT / 'runtime' / 'grid_advisor'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'last_grid_recommendation.json'

    output = {
        'ts': market_data.get('ts', datetime.now(timezone.utc).isoformat()),
        'market_data': {k: v for k, v in market_data.items() if k in ('sources_available', 'sources_missing', 'xrp_price')},
        'grid': grid,
    }
    out_path.write_text(json.dumps(output, indent=2, default=str))
    logger.info(f"Grid recomendado: ${grid['grid_min_price']:.2f} - ${grid['grid_max_price']:.2f} | {grid['grid_levels']} niveles | confianza {grid['confidence']:.2f}")
    return output


if __name__ == '__main__':
    r = run_advisor()
    print(json.dumps(r, indent=2, default=str))
