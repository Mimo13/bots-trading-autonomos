#!/usr/bin/env python3
"""CoinMarketCap data feed — precios, dominancia, miedo/avaricia."""
from __future__ import annotations
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'


def _load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch_global_metrics() -> dict | None:
    """BTC dominance, total market cap, fear & greed proxy (volume change)."""
    key = _load_env().get('COINMARKETCAP_API_KEY', '')
    if not key:
        return None

    try:
        req = urllib.request.Request(
            'https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest',
            headers={'X-CMC_PRO_API_KEY': key, 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            data = d.get('data', {})
            quote = data.get('quote', {}).get('USD', {})
            return {
                'btc_dominance': data.get('btc_dominance'),
                'total_market_cap': quote.get('total_market_cap'),
                'volume_24h': quote.get('volume_24h'),
                'market_cap_change_24h': quote.get('market_cap_change_24h'),
                'source': 'coinmarketcap',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'CMC fetch failed: {e}')
        return None


def fetch_xrp_price() -> float | None:
    """Current XRP price in USD."""
    key = _load_env().get('COINMARKETCAP_API_KEY', '')
    if not key:
        return None
    try:
        req = urllib.request.Request(
            'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?slug=xrp',
            headers={'X-CMC_PRO_API_KEY': key, 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            data = d.get('data', {})
            for k, v in data.items():
                if v.get('slug') == 'xrp':
                    return v['quote']['USD']['price']
            return None
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'CMC XRP price failed: {e}')
        return None


def fetch_listings(limit: int = 50) -> list | None:
    """Top N coins for market context."""
    key = _load_env().get('COINMARKETCAP_API_KEY', '')
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?limit={limit}',
            headers={'X-CMC_PRO_API_KEY': key, 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return d.get('data', [])
    except Exception:
        return None


if __name__ == '__main__':
    g = fetch_global_metrics()
    print('Global:', json.dumps(g, indent=2) if g else 'no key/error')
    p = fetch_xrp_price()
    print('XRP price:', p)
