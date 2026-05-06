#!/usr/bin/env python3
"""
TradingView bridge for both bots.

Capabilities:
1) write-ctrader-signal:
   Generate CSV signal file consumed by FabianStructurePullbackBot.
2) enrich-polymarket-csv:
   Read base polymarket CSV and output CSV with tv_recommendation/tv_confidence.

Requires: uv tool install tradingview-mcp-server
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


def _load_tv_modules():
    candidates = glob.glob(os.path.expanduser("~/.local/share/uv/tools/tradingview-mcp-server/lib/python*/site-packages"))
    if not candidates:
        raise RuntimeError("tradingview-mcp-server site-packages not found. Run: uv tool install tradingview-mcp-server")
    sys.path.insert(0, candidates[0])

    from tradingview_mcp.core.services.screener_service import get_multiple_analysis  # type: ignore

    return get_multiple_analysis


@dataclass
class TvSignal:
    recommendation: str
    confidence: float


def _recommendation_to_confidence(summary: Dict) -> float:
    # TradingView gives vote counts in summary: BUY/SELL/NEUTRAL
    buy = float(summary.get("BUY", 0) or 0)
    sell = float(summary.get("SELL", 0) or 0)
    neutral = float(summary.get("NEUTRAL", 0) or 0)
    total = buy + sell + neutral
    if total <= 0:
        return 0.0

    rec = (summary.get("RECOMMENDATION") or "").upper()
    if rec in ("BUY", "STRONG_BUY"):
        return max(0.0, min(1.0, buy / total))
    if rec in ("SELL", "STRONG_SELL"):
        return max(0.0, min(1.0, sell / total))
    return max(0.0, min(1.0, neutral / total))


def _normalize_recommendation(rec: str) -> str:
    r = (rec or "").upper()
    if "BUY" in r:
        return "BUY"
    if "SELL" in r:
        return "SELL"
    return "NEUTRAL"


def fetch_tv_signals(exchange_symbols: List[str], screener: str, interval: str) -> Dict[str, TvSignal]:
    get_multiple_analysis = _load_tv_modules()
    data = get_multiple_analysis(screener, interval, exchange_symbols)

    out: Dict[str, TvSignal] = {}
    for sym in exchange_symbols:
        analysis = data.get(sym)
        if analysis is None:
            out[sym] = TvSignal("NEUTRAL", 0.0)
            continue

        summary = getattr(analysis, "summary", {}) or {}
        rec = _normalize_recommendation(summary.get("RECOMMENDATION", "NEUTRAL"))
        conf = _recommendation_to_confidence(summary)
        out[sym] = TvSignal(rec, conf)

    return out


def write_ctrader_signal(output_csv: Path, ctrader_symbol: str, tv_exchange_symbol: str, interval: str = "5m") -> None:
    screener = "forex"
    signals = fetch_tv_signals([tv_exchange_symbol], screener=screener, interval=interval)
    sig = signals[tv_exchange_symbol]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "symbol", "recommendation", "confidence"])
        w.writerow([ts, ctrader_symbol, sig.recommendation, f"{sig.confidence:.4f}"])


def _default_tv_symbol_for_instrument(instr: str) -> Tuple[str, str]:
    i = (instr or "").upper()
    # returns (screener, exchange_symbol)
    if i in ("BTC", "BTCUSD", "BTC-USD"):
        return "crypto", "BINANCE:BTCUSDT"
    if i in ("ETH", "ETHUSD", "ETH-USD"):
        return "crypto", "BINANCE:ETHUSDT"
    if i in ("EURUSD", "EUR/USD"):
        return "forex", "OANDA:EURUSD"
    return "crypto", "BINANCE:BTCUSDT"


def enrich_polymarket_csv(input_csv: Path, output_csv: Path, interval: str = "5m") -> None:
    with input_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if not reader.fieldnames:
            raise RuntimeError("Input CSV has no header")
        fieldnames = list(reader.fieldnames)

    if "instrument" not in fieldnames:
        raise RuntimeError("Input CSV must contain 'instrument' column")

    unique_instr = sorted({(r.get("instrument") or "").strip().upper() for r in rows if (r.get("instrument") or "").strip()})
    symbol_map: Dict[str, Tuple[str, str]] = {ins: _default_tv_symbol_for_instrument(ins) for ins in unique_instr}

    by_screener: Dict[str, List[str]] = {}
    for _, (scr, exsym) in symbol_map.items():
        by_screener.setdefault(scr, [])
        if exsym not in by_screener[scr]:
            by_screener[scr].append(exsym)

    tv_lookup: Dict[str, TvSignal] = {}
    for screener, syms in by_screener.items():
        tv_lookup.update(fetch_tv_signals(syms, screener=screener, interval=interval))

    if "tv_recommendation" not in fieldnames:
        fieldnames.append("tv_recommendation")
    if "tv_confidence" not in fieldnames:
        fieldnames.append("tv_confidence")

    for r in rows:
        ins = (r.get("instrument") or "").strip().upper()
        _, exsym = symbol_map.get(ins, ("crypto", "BINANCE:BTCUSDT"))
        sig = tv_lookup.get(exsym, TvSignal("NEUTRAL", 0.0))
        r["tv_recommendation"] = sig.recommendation
        r["tv_confidence"] = f"{sig.confidence:.4f}"

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView bridge for FabiánPullback and PolyKronosPaper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("write-ctrader-signal")
    p1.add_argument("--output", required=True)
    p1.add_argument("--ctrader-symbol", default="EURUSD")
    p1.add_argument("--tv-symbol", default="OANDA:EURUSD")
    p1.add_argument("--interval", default="5m")

    p2 = sub.add_parser("enrich-polymarket-csv")
    p2.add_argument("--input", required=True)
    p2.add_argument("--output", required=True)
    p2.add_argument("--interval", default="5m")

    args = parser.parse_args()

    if args.cmd == "write-ctrader-signal":
        write_ctrader_signal(
            output_csv=Path(args.output),
            ctrader_symbol=args.ctrader_symbol,
            tv_exchange_symbol=args.tv_symbol,
            interval=args.interval,
        )
        print(f"Wrote cTrader signal to {args.output}")

    elif args.cmd == "enrich-polymarket-csv":
        enrich_polymarket_csv(
            input_csv=Path(args.input),
            output_csv=Path(args.output),
            interval=args.interval,
        )
        print(f"Wrote enriched Polymarket CSV to {args.output}")


if __name__ == "__main__":
    main()
