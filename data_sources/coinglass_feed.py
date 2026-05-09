#!/usr/bin/env python3
"""Coinglass data feed — liquidaciones, OI, funding rate (requiere API key gratis)."""
from __future__ import annotations
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'


def _load_key() -> str:
    if not ENV_PATH.exists():
        return ''
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith('COINGLASS_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def fetch_open_interest(symbol: str = 'XRP') -> dict | None:
    """Open interest and volume for XRP."""
    key = _load_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f'https://open-api.coinglass.com/public/v2/open_interest?symbol={symbol}USDT',
            headers={'accept': 'application/json', 'coinglassSecret': key}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {'data': d.get('data', []), 'source': 'coinglass',
                    'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'Coinglass OI failed: {e}')
        return None


def fetch_funding_rate(symbol: str = 'XRP') -> dict | None:
    """Funding rate across exchanges."""
    key = _load_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f'https://open-api.coinglass.com/public/v2/funding_rate?symbol={symbol}USDT',
            headers={'accept': 'application/json', 'coinglassSecret': key}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {'data': d.get('data', []), 'source': 'coinglass',
                    'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    except Exception:
        return None


if __name__ == '__main__':
    import json
    oi = fetch_open_interest()
    print('OI:', json.dumps(oi, indent=2, default=str)[:500] if oi else 'no key')
