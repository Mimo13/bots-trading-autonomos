#!/usr/bin/env python3
"""Compare current heuristic orchestrator regime vs isolated HMM provider.

Path B helper only. Produces a side-by-side JSON/console snapshot using the same
local runtime/live feeds, without modifying orchestrator behaviour.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hmm_regime_provider import HmmRegimeProvider, ProviderConfig

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "runtime" / "live"
OUTPUT_DIR = ROOT / "hmm" / "output"
REGIME_PRIORITY = {"risk_off": 0, "bear": 1, "sideways": 2, "bull": 3, "unknown": 4}


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def read_closes(symbol: str, live_dir: Path, limit: int = 180) -> list[float]:
    path = live_dir / f"{symbol}_5m.csv"
    if not path.exists():
        return []
    closes: list[float] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            c = row.get("close") or row.get("Close") or row.get("c")
            if c is None and row:
                vals = list(row.values())
                if len(vals) >= 5:
                    c = vals[4]
            val = safe_float(c, math.nan)
            if math.isfinite(val) and val > 0:
                closes.append(val)
    return closes[-limit:]


def pct_change(a: float, b: float) -> float:
    return (b / a - 1.0) if a else 0.0


def heuristic_detect_regime(symbol: str, live_dir: Path) -> dict[str, Any]:
    closes = read_closes(symbol, live_dir)
    if len(closes) < 60:
        return {"symbol": symbol, "regime": "unknown", "confidence": 0.0, "reason": "insufficient_feed_data", "metrics": {"samples": len(closes)}}

    last = closes[-1]
    sma20 = statistics.fmean(closes[-20:])
    sma50 = statistics.fmean(closes[-50:])
    trend20 = pct_change(sma20, last)
    trend50 = pct_change(sma50, last)
    returns = [pct_change(closes[i - 1], closes[i]) for i in range(1, len(closes))]
    vol = statistics.pstdev(returns[-60:]) if len(returns) >= 60 else 0.0
    peak = max(closes[-80:])
    drawdown = (last / peak - 1.0) if peak else 0.0

    if drawdown <= -0.055 or vol >= 0.012:
        regime = "risk_off"
        reason = "drawdown_or_volatility_guard"
    elif trend50 > 0.012 and last > sma20 > sma50:
        regime = "bull"
        reason = "price_above_sma20_sma50"
    elif trend50 < -0.012 and last < sma20 < sma50:
        regime = "bear"
        reason = "price_below_sma20_sma50"
    else:
        regime = "sideways"
        reason = "no_clear_trend"

    confidence = min(1.0, abs(trend50) * 18 + min(vol * 25, 0.35) + (0.15 if regime != "sideways" else 0.12))
    return {
        "symbol": symbol,
        "regime": regime,
        "confidence": round(confidence, 4),
        "reason": reason,
        "metrics": {
            "last": round(last, 6),
            "trend50_pct": round(trend50 * 100, 3),
            "vol_5m_pct": round(vol * 100, 3),
            "drawdown_from_80bar_peak_pct": round(drawdown * 100, 3),
        },
    }


def merge_regimes(regimes: list[dict[str, Any]]) -> dict[str, Any]:
    if not regimes:
        return {"regime": "unknown", "confidence": 0.0, "reason": "no_feeds", "symbols": []}
    merged = min(regimes, key=lambda r: REGIME_PRIORITY.get(r.get("regime", "unknown"), 99))
    avg_conf = statistics.fmean([r.get("confidence", 0.0) for r in regimes]) if len(regimes) > 1 else merged["confidence"]
    return {
        "regime": merged["regime"],
        "confidence": round(avg_conf, 4),
        "reason": f"heuristic_multi_symbol: most_conservative_is_{merged['regime']}",
        "symbols": regimes,
    }


def compare(symbols: list[str], live_dir: Path, timeframe: str, force_retrain: bool = False) -> dict[str, Any]:
    heuristic_symbols = [heuristic_detect_regime(sym, live_dir) for sym in symbols]
    heuristic = merge_regimes(heuristic_symbols)

    provider = HmmRegimeProvider(live_dir=live_dir, cfg=ProviderConfig(timeframe=timeframe, default_symbols=tuple(symbols)))
    hmm = provider.get_snapshot(symbols=symbols, force_retrain=force_retrain)

    by_symbol = []
    hmm_map = {s["symbol"]: s for s in asdict(hmm)["symbols"]}
    for hs in heuristic_symbols:
        cur = hmm_map.get(hs["symbol"], {})
        by_symbol.append(
            {
                "symbol": hs["symbol"],
                "heuristic_regime": hs["regime"],
                "heuristic_confidence": hs["confidence"],
                "hmm_regime": cur.get("regime", "unknown"),
                "hmm_confidence": cur.get("confidence", 0.0),
                "agreement": hs["regime"] == cur.get("regime"),
                "heuristic_reason": hs.get("reason"),
                "hmm_reason": cur.get("reason"),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "symbols": symbols,
        "timeframe": timeframe,
        "heuristic": heuristic,
        "hmm": asdict(hmm),
        "by_symbol": by_symbol,
        "summary": {
            "heuristic_regime": heuristic["regime"],
            "hmm_regime": hmm.regime,
            "agreement_count": sum(1 for row in by_symbol if row["agreement"]),
            "total_symbols": len(by_symbol),
            "fully_aligned": all(row["agreement"] for row in by_symbol),
        },
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare heuristic regime detector vs HMM provider")
    p.add_argument("--symbols", default="SOLUSDT,XRPUSDT")
    p.add_argument("--timeframe", default="4h", choices=["1h", "4h", "1d"])
    p.add_argument("--live-dir", default=str(LIVE_DIR))
    p.add_argument("--output-json", default=str(OUTPUT_DIR / "hmm_vs_heuristic.json"))
    p.add_argument("--force-retrain", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = compare(symbols, Path(args.live_dir), args.timeframe, force_retrain=args.force_retrain)
    out = Path(args.output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Wrote {out}")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
