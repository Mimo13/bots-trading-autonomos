#!/usr/bin/env python3
"""LunarCrush data feed — social sentiment, engagement, galxy score.
Requiere API key gratuita de https://lunarcrush.com/register
"""
from __future__ import annotations
import json, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'


def _load_key() -> str:
    if not ENV_PATH.exists():
        return ''
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith('LUNARCRUSH_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def fetch_social_sentiment(symbol: str = 'XRP') -> dict | None:
    """Social volume, sentiment, galaxy score for a coin."""
    key = _load_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f'https://lunarcrush.com/api/v2/coins?key={key}&symbol={symbol}&data_points=1',
            headers={'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            rows = d.get('data', [])
            if not rows:
                return None
            r = rows[0]
            return {
                'symbol': r.get('symbol'),
                'name': r.get('name'),
                'price': r.get('price'),
                'galaxy_score': r.get('galaxy_score'),
                'social_volume': r.get('social_volume'),
                'social_score': r.get('social_score'),
                'social_contributors': r.get('social_contributors'),
                'sentiment': r.get('sentiment'),
                'avg_sentiment': r.get('avg_sentiment'),
                'bull_bear_pct': r.get('bull_bear_pct'),
                'price_volatility': r.get('price_volatility'),
                'source': 'lunarcrush',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'LunarCrush fetch failed: {e}')
        return None


def fetch_trending() -> list | None:
    """Top trending coins by social volume (gratis)."""
    key = _load_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f'https://lunarcrush.com/api/v2/coins?key={key}&limit=10&sort=social_volume',
            headers={'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return d.get('data', [])
    except Exception:
        return None


if __name__ == '__main__':
    import json
    d = fetch_social_sentiment()
    print(json.dumps(d, indent=2, default=str) if d else 'No key or error')
