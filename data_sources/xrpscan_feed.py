#!/usr/bin/env python3
"""XRPScan data feed — ledger, escrows, network stats (API pública, no requiere key)."""
from __future__ import annotations
import json, urllib.request, urllib.error
from datetime import datetime, timezone
from typing import Any

XRPLEDGER_API = 'https://api.xrpscan.com/api/v1'


def fetch_network_stats() -> dict | None:
    """Network metrics (ledger index, tx count, fee, etc.)."""
    try:
        req = urllib.request.Request(f'{XRPLEDGER_API}/ledger',
                                     headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {
                'ledger_index': d.get('ledger_index'),
                'tx_count': d.get('tx_count'),
                'close_time': d.get('close_time'),
                'total_coins': d.get('total_coins'),
                'source': 'xrpscan',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'XRPScan ledger failed: {e}')
        return None


def fetch_account_info(address: str) -> dict | None:
    """Get XRP balance and info for a specific address."""
    try:
        req = urllib.request.Request(f'{XRPLEDGER_API}/account/{address}',
                                     headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {
                'address': d.get('account'),
                'balance': d.get('xrpBalance'),
                'owner_count': d.get('ownerCount'),
                'source': 'xrpscan',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'XRPScan account failed: {e}')
        return None


def fetch_exchange_volume() -> dict | None:
    """Top exchanges XRP volume (from xrpscan)."""
    try:
        req = urllib.request.Request(f'{XRPLEDGER_API}/exchange/volume',
                                     headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {'data': d, 'source': 'xrpscan',
                    'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    except Exception:
        return None


def fetch_market_overview() -> dict:
    """Consolidated view of all XRPScan data we can get for free."""
    result = {'source': 'xrpscan', 'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'items': {}}

    net = fetch_network_stats()
    if net:
        result['network'] = net

    vol = fetch_exchange_volume()
    if vol:
        result['volume'] = vol

    return result


if __name__ == '__main__':
    import json
    print(json.dumps(fetch_market_overview(), indent=2, default=str))
