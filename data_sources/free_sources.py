#!/usr/bin/env python3
"""Data sources gratuitos — alternativas a Coinglass, CryptoQuant, Santiment.
Usa APIs públicas de Binance Futures y CoinGecko.
No requiere API key.
"""
from __future__ import annotations
import json, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = 'https://fapi.binance.com'
CG_BASE = 'https://api.coingecko.com/api/v3'


def fetch_binance_open_interest(symbol: str = 'XRPUSDT') -> dict | None:
    """Open interest from Binance Futures (gratuito, sin key)."""
    try:
        req = urllib.request.Request(f'{BASE}/fapi/v1/openInterest?symbol={symbol}',
                                     headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {
                'symbol': d.get('symbol'),
                'open_interest': float(d.get('openInterest', 0)),
                'source': 'binance_futures',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'Binance OI failed: {e}')
        return None


def fetch_binance_funding_rate(symbol: str = 'XRPUSDT', limit: int = 1) -> dict | None:
    """Funding rate history from Binance Futures."""
    try:
        req = urllib.request.Request(
            f'{BASE}/fapi/v1/fundingRate?symbol={symbol}&limit={limit}',
            headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            rates = []
            for r in d:
                rates.append({
                    'rate': float(r.get('fundingRate', 0)),
                    'time': r.get('fundingTime', ''),
                })
            avg_rate = sum(r['rate'] for r in rates) / max(1, len(rates))
            return {
                'symbol': symbol,
                'avg_funding_rate': avg_rate,
                'rates': rates,
                'source': 'binance_futures',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'Binance funding rate failed: {e}')
        return None


def fetch_binance_ticker_24h(symbol: str = 'XRPUSDT') -> dict | None:
    """24h ticker stats (volume, change, high, low) from Binance spot."""
    try:
        req = urllib.request.Request(
            f'https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}',
            headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            return {
                'price_change_pct': float(d.get('priceChangePercent', 0)),
                'volume': float(d.get('volume', 0)),
                'quote_volume': float(d.get('quoteVolume', 0)),
                'high': float(d.get('highPrice', 0)),
                'low': float(d.get('lowPrice', 0)),
                'source': 'binance_spot',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception:
        return None


def fetch_coingecko_sentiment(coin_id: str = 'ripple') -> dict | None:
    """Social sentiment / community data from CoinGecko (gratuito, limitado)."""
    try:
        req = urllib.request.Request(
            f'{CG_BASE}/coins/{coin_id}?localization=false&tickers=false&community_data=true&developer_data=false&sparkline=false',
            headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
            community = d.get('community_data', {})
            market = d.get('market_data', {})
            return {
                'sentiment_votes_up': community.get('sentiment_votes_up_percentage', 0),
                'sentiment_votes_down': community.get('sentiment_votes_down_percentage', 0),
                'twitter_followers': community.get('twitter_followers', 0),
                'reddit_subscribers': community.get('reddit_subscribers', 0),
                'price_change_24h': market.get('price_change_percentage_24h_in_currency', {}).get('usd', 0),
                'market_cap_rank': d.get('market_cap_rank'),
                'total_volume': market.get('total_volume', {}).get('usd', 0),
                'source': 'coingecko',
                'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'CoinGecko sentiment failed: {e}')
        return None


def fetch_all_free() -> dict:
    """Recolectar todos los datos gratuitos disponibles."""
    result = {
        'source': 'free_aggregator',
        'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'items': {}
    }

    oi = fetch_binance_open_interest()
    if oi:
        result['items']['open_interest'] = oi

    fr = fetch_binance_funding_rate()
    if fr:
        result['items']['funding_rate'] = fr

    ticker = fetch_binance_ticker_24h()
    if ticker:
        result['items']['ticker_24h'] = ticker

    sentiment = fetch_coingecko_sentiment()
    if sentiment:
        result['items']['sentiment'] = sentiment

    return result


if __name__ == '__main__':
    import json
    print(json.dumps(fetch_all_free(), indent=2, default=str)[:2000])
