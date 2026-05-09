#!/usr/bin/env python3
"""LunarCrush data feed — social sentiment, engagement, galaxy score.
Requiere API key gratuita de https://lunarcrush.com/register
"""
from __future__ import annotations
import json, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = 'https://lunarcrush.com/api4'
ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'

def _load_key() -> str:
    if not ENV_PATH.exists():
        return ''
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith('LUNARCRUSH_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def _make_request(url: str) -> dict[str, Any] | None:
    """Llamada a LunarCrush API con headers de navegador para evitar Cloudflare."""
    key = _load_key()
    if not key:
        return None
    try:
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Origin': 'https://lunarcrush.com',
            'Referer': 'https://lunarcrush.com/',
        }
        separator = '&' if '?' in url else '?'
        full_url = f'{url}{separator}key={key}'
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'LunarCrush request failed: {e}')
        return None


def fetch_social_sentiment(symbol: str = 'XRP') -> dict | None:
    """Social volume, sentiment, galaxy score for a coin."""
    data = _make_request(f'{BASE}/public/coins/list/v1?limit=50')
    if data:
        coins = data.get('data', [])
        for c in coins:
            if c.get('symbol', '').upper() == symbol.upper():
                return {
                    'symbol': c.get('symbol'),
                    'name': c.get('name'),
                    'price': c.get('price'),
                    'galaxy_score': c.get('galaxy_score'),
                    'social_volume_24h': c.get('social_volume_24h'),
                    'interactions_24h': c.get('interactions_24h'),
                    'sentiment': c.get('sentiment'),
                    'market_cap': c.get('market_cap'),
                    'market_dominance': c.get('market_dominance'),
                    'market_cap_rank': c.get('market_cap_rank'),
                    'percent_change_24h': c.get('percent_change_24h'),
                    'source': 'lunarcrush',
                    'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                }
    return None


def fetch_trending(limit: int = 10) -> list | None:
    """Top trending coins by social volume."""
    data = _make_request(f'https://lunarcrush.com/api/v2/coins?limit={limit}&sort=social_volume')
    if data:
        return data.get('data', [])
    return None


if __name__ == '__main__':
    import json
    d = fetch_social_sentiment()
    print(json.dumps(d, indent=2, default=str) if d else 'No data (check key)')
