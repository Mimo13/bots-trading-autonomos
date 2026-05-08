#!/usr/bin/env python3
"""
Binance Data Fetcher — obtiene OHLCV real de Binance (API pública, sin key).
Genera el mismo formato CSV que usan los bots.
También integra señales de TradingView MCP.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Binance public API ─────────────────────────────────────────────────────────

BINANCE_BASE = "https://api.binance.com"
TIMEFRAMES = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
              "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}


def fetch_binance_klines(
    symbol: str,
    interval: str = "5m",
    limit: int = 500,
) -> List[Dict]:
    """
    Fetch OHLCV klines from Binance public API.
    No API key needed — uses public endpoint.
    
    Returns list of dicts with keys:
      timestamp_utc, instrument, timeframe, open, high, low, close, volume
    """
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urlopen(req, timeout=30)
    data = json.loads(resp.read().decode("utf-8"))
    
    rows = []
    for k in data:
        ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        rows.append({
            "timestamp_utc": ts.isoformat().replace("+00:00", "Z"),
            "instrument": symbol.upper(),
            "timeframe": interval,
            "open": str(k[1]),
            "high": str(k[2]),
            "low": str(k[3]),
            "close": str(k[4]),
            "volume": str(k[5]),
        })
    
    return rows


def fetch_binance_ticker(symbol: str) -> Dict:
    """Fetch current ticker for a symbol."""
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr?symbol={symbol.upper()}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read().decode("utf-8"))


def fetch_binance_symbols(quote_asset: str = "USDT") -> List[str]:
    """Get all USDT trading pairs from Binance."""
    url = f"{BINANCE_BASE}/api/v3/exchangeInfo"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urlopen(req, timeout=15)
    info = json.loads(resp.read().decode("utf-8"))
    
    symbols = []
    for s in info.get("symbols", []):
        if s.get("quoteAsset") == quote_asset.upper() and s.get("status") == "TRADING":
            symbols.append(s["symbol"])
    return symbols


# ── TradingView MCP client ─────────────────────────────────────────────────────

TV_MCP_HOST = os.getenv("TV_MCP_HOST", "localhost")
TV_MCP_PORT = int(os.getenv("TV_MCP_PORT", "18789"))
TV_MCP_URL = f"http://{TV_MCP_HOST}:{TV_MCP_PORT}"


def tv_get_analysis(symbol: str, screener: str = "crypto", interval: str = "5m") -> Optional[Dict]:
    """Get TradingView technical analysis for a symbol via TV MCP server."""
    try:
        from tradingview_mcp.core.services.screener_service import get_multiple_analysis
        result = get_multiple_analysis(screener, interval, [symbol])
        if symbol in result and result[symbol] is not None:
            analysis = result[symbol]
            summary = getattr(analysis, "summary", {}) or {}
            indicators = getattr(analysis, "indicators", {}) or {}
            return {
                "recommendation": (summary.get("RECOMMENDATION") or "NEUTRAL").upper(),
                "buy_pct": float(summary.get("BUY", 0) or 0),
                "sell_pct": float(summary.get("SELL", 0) or 0),
                "neutral_pct": float(summary.get("NEUTRAL", 0) or 0),
                "rsi": indicators.get("RSI"),
                "bb_upper": indicators.get("BBupper"),
                "bb_middle": indicators.get("BBmiddle"),
                "bb_lower": indicators.get("BBlower"),
                "ema50": indicators.get("EMA50"),
                "ema200": indicators.get("EMA200"),
            }
    except Exception as e:
        return {"error": str(e)}
    return None


def tv_recommendation_to_confidence(summary: Dict) -> float:
    """Convert TV votes to confidence score (0-1)."""
    buy = float(summary.get("buy_pct", 0))
    sell = float(summary.get("sell_pct", 0))
    neutral = float(summary.get("neutral_pct", 0))
    total = buy + sell + neutral
    if total <= 0:
        return 0.0
    rec = summary.get("recommendation", "NEUTRAL")
    if "BUY" in rec:
        return buy / total
    if "SELL" in rec:
        return sell / total
    return neutral / total


# ── Model probability generator ────────────────────────────────────────────────
# Since we don't have a real ML model, we use a simple heuristic:
# Combine TV analysis with price momentum

def compute_model_probs(candles: List[Dict], tv_analysis: Optional[Dict] = None) -> List[Dict]:
    """
    Add p_model_up and p_market_up probabilities to candle data.
    
    p_model_up = model's estimated probability price goes up
      - Based on recent momentum + TV indicators (if available)
    p_market_up = market-implied probability (simple normalization)
    
    This is a simplified model — in production you'd use a real ML model.
    """
    closes = [float(c["close"]) for c in candles]
    
    for i, c in enumerate(candles):
        # Market probability: normalize price position in recent range
        lookback = max(1, min(i, 20))
        recent = closes[max(0, i - lookback):i + 1]
        lo, hi = min(recent), max(recent)
        price_range = hi - lo
        if price_range > 0:
            p_market = (float(c["close"]) - lo) / price_range
        else:
            p_market = 0.5
        
        # Model probability: based on short-term momentum + TV signal
        if i >= 2:
            prev2 = closes[i - 1] - closes[i - 2] if i - 2 >= 0 else 0
            prev1 = closes[i] - closes[i - 1]
            momentum = (prev1 + prev2 * 0.5) / (abs(closes[i]) + 0.001)
            p_model = 0.5 + momentum * 50  # Scale momentum
        else:
            p_model = 0.5
        
        p_model = max(0.01, min(0.99, p_model))
        
        c["p_model_up"] = f"{p_model:.4f}"
        c["p_market_up"] = f"{p_market:.4f}"
        
        # Add TV data if available (only to first candle for simplicity)
        if tv_analysis and i == 0 and "error" not in tv_analysis:
            c["tv_recommendation"] = tv_analysis.get("recommendation", "NEUTRAL")
            c["tv_confidence"] = f"{tv_recommendation_to_confidence(tv_analysis):.4f}"
        elif "tv_recommendation" not in c:
            c["tv_recommendation"] = "NEUTRAL"
            c["tv_confidence"] = "0.0"
    
    return candles


# ── Main orchestrator ──────────────────────────────────────────────────────────

def fetch_live_data(
    symbols: List[str],
    interval: str = "5m",
    limit: int = 300,
    output_dir: Optional[Path] = None,
    use_tv: bool = True,
) -> Dict[str, Path]:
    """
    Fetch live data for multiple symbols from Binance.
    
    Returns dict of {symbol: csv_path}
    """
    if output_dir is None:
        output_dir = Path("/tmp/live_data")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    results = {}
    
    for symbol in symbols:
        print(f"📡 Fetching {symbol} {interval}...")
        try:
            candles = fetch_binance_klines(symbol, interval, limit)
            if not candles:
                print(f"  ⚠️ No data for {symbol}")
                continue
            
            # Optional TV analysis
            tv = None
            if use_tv:
                tv = tv_get_analysis(symbol, interval=interval)
                if tv and "error" in tv:
                    print(f"  ⚠️ TV analysis failed: {tv['error']}")
                    tv = None
            
            # Add model probabilities
            candles = compute_model_probs(candles, tv)
            
            # Write CSV
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"live_{symbol}_{interval}_{ts}.csv"
            csv_path = output_dir / filename
            
            fieldnames = ["timestamp_utc", "instrument", "timeframe", "open", "high", "low", "close",
                         "volume", "p_model_up", "p_market_up", "tv_recommendation", "tv_confidence"]
            
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for row in candles:
                    w.writerow({k: row.get(k, "") for k in fieldnames})
            
            results[symbol] = csv_path
            print(f"  ✅ {len(candles)} velas → {csv_path}")
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
    
    return results


def update_feed_file(symbol: str, interval: str = "5m", output_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Convenience: fetch one symbol and write to a fixed path (for cron/launchd).
    Used by the bots' data pipeline.
    """
    if output_dir is None:
        output_dir = Path(__file__).parent / "runtime" / "live"
    output_dir.mkdir(parents=True, exist_ok=True)
    feed_path = output_dir / f"{symbol}_{interval}.csv"
    
    try:
        candles = fetch_binance_klines(symbol, interval, 300)
        if not candles:
            return None
        
        tv = tv_get_analysis(symbol, interval=interval)
        candles = compute_model_probs(candles, tv)
        
        fieldnames = ["timestamp_utc", "instrument", "timeframe", "open", "high", "low", "close",
                     "volume", "p_model_up", "p_market_up", "tv_recommendation", "tv_confidence"]
        
        with feed_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in candles:
                w.writerow({k: row.get(k, "") for k in fieldnames})
        
        return feed_path
    except Exception as e:
        print(f"Error updating feed for {symbol}: {e}")
        return None


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    symbols = args if args else ["BTCUSDT", "SOLUSDT", "ADAUSDT"]
    results = fetch_live_data(symbols)
    for sym, path in results.items():
        print(f"{sym}: {path}")
