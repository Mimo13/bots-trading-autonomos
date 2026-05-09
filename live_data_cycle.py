#!/usr/bin/env python3
"""
Live data pipeline: fetch from Binance → run all bots → collector.
Ejecutar cada ~15 min desde cron o launchd.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/mimo13/bots-trading-autonomos-runtime')
sys.path.insert(0, str(ROOT))

from data_fetcher import update_feed_file, fetch_binance_ticker


def live_data_cycle():
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"[{ts}] 🔄 Live data cycle starting")
    
    # 1. Fetch live data for tracked assets
    assets = ["SOLUSDT", "ADAUSDT", "DOGEUSDT", "BTCUSDT", "XRPUSDT"]
    feed_dir = ROOT / "runtime" / "live"
    feed_dir.mkdir(parents=True, exist_ok=True)
    
    for symbol in assets:
        try:
            path = update_feed_file(symbol, "5m", feed_dir)
            if path:
                ticker = fetch_binance_ticker(symbol)
                price = float(ticker["lastPrice"])
                change = float(ticker["priceChangePercent"])
                print(f"  ✅ {symbol}: ${price:.4f} ({change:+.2f}%) → {path.name}")
            else:
                print(f"  ⚠️ {symbol}: no data")
        except Exception as e:
            print(f"  ❌ {symbol}: {e}")
    
    # 2. Copy best feed to standard input location for bots
    # Use SOL as default (most volatile, good for testing)
    sol_feed = feed_dir / "SOLUSDT_5m.csv"
    if sol_feed.exists():
        poly_input = ROOT / "runtime" / "polymarket" / "polymarket_base_input.csv"
        sol_feed.rename(poly_input) if not poly_input.exists() else None
        # Also update enriched
        import shutil
        enriched = ROOT / "runtime" / "polymarket" / "polymarket_input_enriched.csv"
        shutil.copy2(sol_feed, enriched)
        print(f"  📥 Feed copiado a polymarket input")
    
    # 3. Run the unified runner
    print(f"\n  🏃 Ejecutando bots...")
    cp = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "final_paper_runner.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    if cp.returncode == 0:
        print(f"  ✅ Bots ejecutados OK")
        try:
            result = json.loads(cp.stdout)
            ps = result.get("polymarket_summary", {})
            if ps:
                print(f"     PolyKronos: {ps.get('total_trades',0)} trades, PnL=${ps.get('total_pnl',0)}")
            port = result.get("portfolio_summary", {})
            if port:
                print(f"     Portfolio: {port.get('total_buys',0)}B/{port.get('total_sells',0)}S, PnL=${port.get('total_pnl',0)}")
        except Exception:
            pass
    else:
        print(f"  ❌ Error en bots: {cp.stderr[:200]}")
    
    # 4. Sync dashboard
    print(f"  📊 Sincronizando dashboard...")
    cp2 = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/collector.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    if cp2.returncode == 0:
        print(f"  ✅ Dashboard sincronizado")
    else:
        print(f"  ❌ Error collector: {cp2.stderr[:200]}")
    
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] ✅ Ciclo completado")


if __name__ == "__main__":
    live_data_cycle()
