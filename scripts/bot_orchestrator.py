#!/usr/bin/env python3
"""Paper-only bot fleet orchestrator — v2 with multi-symbol regime + auto-pause.

This module is intentionally isolated: it reads market/feed + bot metrics,
produces recommendations and an audit trail, and can auto-pause unhealthy bots
(apply_actions=true). Set orchestrator_config.json:apply_actions=true only after
paper validation.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
except Exception:  # pragma: no cover - lets local smoke tests run without db deps
    psycopg = None

ROOT = Path(os.getenv("BTA_ROOT", "/Users/mimo13/bots-trading-autonomos-runtime"))
DB_URL = os.getenv("DATABASE_URL", "postgresql:///bots_dashboard")
CONFIG = ROOT / "orchestrator_config.json"
STATE_DIR = ROOT / "runtime" / "orchestrator"
RUNS_DIR = ROOT / "runtime" / "polymarket" / "runs"
LIVE_DIR = ROOT / "runtime" / "live"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def read_closes(symbol: str, limit: int = 180) -> list[float]:
    path = LIVE_DIR / f"{symbol}_5m.csv"
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


REGIME_PRIORITY = {"risk_off": 0, "bear": 1, "sideways": 2, "bull": 3, "unknown": 4}


def load_hmm_provider_class():
    hmm_path = ROOT / "hmm" / "hmm_regime_provider.py"
    if not hmm_path.exists():
        return None
    try:
        skill_dir = str(hmm_path.parent)
        if skill_dir not in sys.path:
            sys.path.insert(0, skill_dir)
        spec = importlib.util.spec_from_file_location("hmm_regime_provider", hmm_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod.HmmRegimeProvider, mod.ProviderConfig
    except Exception:
        return None


def detect_regime(symbol: str) -> dict[str, Any]:
    closes = read_closes(symbol)
    if len(closes) < 60:
        return {
            "symbol": symbol,
            "regime": "unknown",
            "confidence": 0.0,
            "reason": "insufficient_feed_data",
            "metrics": {"samples": len(closes)},
        }

    last = closes[-1]
    sma20 = statistics.fmean(closes[-20:])
    sma50 = statistics.fmean(closes[-50:])
    trend20 = pct_change(sma20, last)
    trend50 = pct_change(sma50, last)
    returns = [pct_change(closes[i - 1], closes[i]) for i in range(1, len(closes))]
    vol = statistics.pstdev(returns[-60:]) if len(returns) >= 60 else 0.0
    peak = max(closes[-80:])
    drawdown = (last / peak - 1.0) if peak else 0.0

    regime = "sideways"
    reasons: list[str] = []
    if drawdown <= -0.055 or vol >= 0.012:
        regime = "risk_off"
        reasons.append("drawdown_or_volatility_guard")
    elif trend50 > 0.012 and last > sma20 > sma50:
        regime = "bull"
        reasons.append("price_above_sma20_sma50")
    elif trend50 < -0.012 and last < sma20 < sma50:
        regime = "bear"
        reasons.append("price_below_sma20_sma50")
    else:
        regime = "sideways"
        reasons.append("no_clear_trend")

    confidence = min(1.0, abs(trend50) * 18 + min(vol * 25, 0.35) + (0.15 if regime != "sideways" else 0.12))
    return {
        "symbol": symbol,
        "regime": regime,
        "confidence": round(confidence, 3),
        "reason": "+".join(reasons),
        "metrics": {
            "samples": len(closes),
            "last": round(last, 6),
            "sma20": round(sma20, 6),
            "sma50": round(sma50, 6),
            "trend20_pct": round(trend20 * 100, 3),
            "trend50_pct": round(trend50 * 100, 3),
            "vol_5m_pct": round(vol * 100, 3),
            "drawdown_from_80bar_peak_pct": round(drawdown * 100, 3),
        },
    }


def merge_regimes(regimes: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple symbol regimes — most conservative wins."""
    if not regimes:
        return {"regime": "unknown", "confidence": 0.0, "reason": "no_feeds", "symbols": []}

    # Most conservative regime wins
    merged = min(regimes, key=lambda r: REGIME_PRIORITY.get(r.get("regime", "unknown"), 99))
    # Average confidence
    avg_conf = statistics.fmean([r.get("confidence", 0.0) for r in regimes]) if len(regimes) > 1 else merged["confidence"]

    return {
        "regime": merged["regime"],
        "confidence": round(avg_conf, 3),
        "reason": f"multi_symbol: most_conservative_is_{merged['regime']}",
        "symbols": [
            {
                "symbol": r["symbol"],
                "regime": r["regime"],
                "confidence": r["confidence"],
                "reason": r.get("reason", ""),
            }
            for r in regimes
        ],
        "source": "heuristic",
    }


def resolve_regime(cfg: dict[str, Any]) -> dict[str, Any]:
    symbols = cfg.get("symbols", [cfg.get("base_symbol", "SOLUSDT")])
    hmm_cfg = cfg.get("hmm_regime", {}) or {}
    if hmm_cfg.get("enabled"):
        loaded = load_hmm_provider_class()
        if loaded is not None:
            HmmRegimeProvider, ProviderConfig = loaded
            try:
                provider = HmmRegimeProvider(
                    live_dir=LIVE_DIR,
                    cfg=ProviderConfig(
                        timeframe=hmm_cfg.get("timeframe", "4h"),
                        default_symbols=tuple(hmm_cfg.get("symbols_override") or symbols),
                    ),
                )
                merged = provider.get_snapshot(
                    symbols=list(hmm_cfg.get("symbols_override") or symbols),
                    force_retrain=bool(hmm_cfg.get("force_retrain", False)),
                )
                out = {
                    "regime": merged.regime,
                    "confidence": merged.confidence,
                    "reason": merged.reason,
                    "symbols": merged.symbols,
                    "source": "hmm",
                    "timeframe": hmm_cfg.get("timeframe", "4h"),
                }
                if out["regime"] != "unknown":
                    return out
                out["fallback_note"] = "hmm_returned_unknown; falling back to heuristic"
            except Exception as e:
                out = {"fallback_note": f"hmm_error: {e}"}
        else:
            out = {"fallback_note": "hmm_provider_unavailable"}
    else:
        out = {}

    regimes = [detect_regime(sym) for sym in symbols]
    merged = merge_regimes(regimes)
    if out.get("fallback_note"):
        merged["fallback_note"] = out["fallback_note"]
    return merged


def db_fetch(sql: str, params: list[Any] | None = None) -> list[tuple]:
    if psycopg is None:
        return []
    try:
        with psycopg.connect(DB_URL) as c:
            with c.cursor() as cur:
                cur.execute(sql, params or [])
                return cur.fetchall()
    except Exception:
        return []


def db_consecutive_losses(bot: str, limit: int = 20) -> int:
    """Count consecutive LOSS rows from most recent trade backwards."""
    rows = db_fetch(
        "select result from trades where bot_name=%s and result in ('WIN','LOSS') order by ts desc limit %s",
        [bot, limit],
    )
    streak = 0
    for r in rows:
        if r[0] == "LOSS":
            streak += 1
        else:
            break
    return streak


def db_metrics(bot: str) -> dict[str, Any]:
    status_rows = db_fetch(
        """select is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at
           from bot_status where bot_name=%s""",
        [bot],
    )
    perf_rows = db_fetch(
        """select coalesce(sum(pnl_usd),0),
                  count(*) filter (where result in ('WIN','LOSS')),
                  count(*) filter (where result='WIN'),
                  count(*) filter (where result='LOSS'),
                  coalesce(avg(case when result='WIN' then 1.0 when result='LOSS' then 0.0 end),0),
                  coalesce(sum(pnl_usd) filter (where ts >= now() - interval '24 hour'),0),
                  coalesce(sum(pnl_usd) filter (where ts >= now() - interval '7 day'),0)
           from trades where bot_name=%s""",
        [bot],
    )
    status = {}
    if status_rows:
        r = status_rows[0]
        status = {
            "is_running": bool(r[0]),
            "mode": r[1],
            "balance_usd": safe_float(r[2]),
            "pnl_day_usd": safe_float(r[3]),
            "pnl_week_usd": safe_float(r[4]),
            "tokens_value_usd": safe_float(r[5]),
            "updated_at": r[6].isoformat() if hasattr(r[6], "isoformat") else r[6],
        }
    perf = {"pnl_total": 0.0, "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl_24h": 0.0, "pnl_7d": 0.0}
    if perf_rows:
        r = perf_rows[0]
        perf = {
            "pnl_total": safe_float(r[0]),
            "closed_trades": int(r[1] or 0),
            "wins": int(r[2] or 0),
            "losses": int(r[3] or 0),
            "win_rate": safe_float(r[4]),
            "pnl_24h": safe_float(r[5]),
            "pnl_7d": safe_float(r[6]),
        }
    return {"status": status, "performance": perf}


def latest_summary(prefix: str) -> dict[str, Any]:
    candidates = sorted(RUNS_DIR.glob(f"{prefix}_*/summary.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    if not candidates:
        return {}
    data = load_json(candidates[-1], {})
    return {
        "path": str(candidates[-1]),
        "initial_balance": safe_float(data.get("initial_balance"), 100.0),
        "final_balance": safe_float(data.get("final_balance", data.get("final_equity", data.get("total_equity"))), 100.0),
        "total_pnl": safe_float(data.get("total_pnl", data.get("pnl_total"))),
        "total_trades": int(safe_float(data.get("total_trades", data.get("closed_trades")), 0)),
        "wins": int(safe_float(data.get("wins"), 0)),
        "losses": int(safe_float(data.get("losses"), 0)),
        "win_rate": safe_float(data.get("win_rate_percent"), 0.0) / 100.0,
        "max_drawdown_pct": safe_float(data.get("max_drawdown_percent"), 0.0),
        "updated_at": datetime.fromtimestamp(candidates[-1].stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def score_bot(
    bot_cfg: dict[str, Any], regime: str, metrics: dict[str, Any], risk_cfg: dict[str, Any], auto_pause: dict[str, Any]
) -> dict[str, Any]:
    perf = metrics.get("performance", {})
    status = metrics.get("status", {})
    summary = metrics.get("latest_summary", {})

    db_closed = int(perf.get("closed_trades") or 0)
    summary_trades = int(summary.get("total_trades") or 0)
    use_db_perf = db_closed > 0
    closed = db_closed if use_db_perf else summary_trades
    wr = safe_float(perf.get("win_rate"), 0.0) if use_db_perf else safe_float(summary.get("win_rate"), 0.0)
    pnl7 = safe_float(perf.get("pnl_7d"), 0.0) if use_db_perf else safe_float(summary.get("total_pnl"), 0.0)
    pnl24 = safe_float(perf.get("pnl_24h"), 0.0) if use_db_perf else safe_float(summary.get("total_pnl"), 0.0)
    dd = safe_float(summary.get("max_drawdown_pct"), 0.0)
    regime_match = regime in bot_cfg.get("regimes", [])
    test_candidate = bool(bot_cfg.get("test_candidate"))

    # Consecutive losses
    consecutive_losses = metrics.get("_consecutive_losses", 0)

    score = 0.0
    score += 42 if regime_match else -30
    score += min(28, max(-18, (wr - 0.45) * 80))
    score += min(18, max(-18, pnl7 * 3))
    score += min(8, max(-8, pnl24 * 4))
    score -= min(18, dd * 1.2)
    score -= min(20, consecutive_losses * 3)  # big penalty for loss streak
    if closed < int(risk_cfg.get("min_closed_trades_for_confidence", 5)):
        score -= 8
    if test_candidate:
        score -= 5

    reason_codes: list[str] = []
    if regime_match:
        reason_codes.append("REGIME_MATCH")
    else:
        reason_codes.append("REGIME_MISMATCH")
    if wr >= safe_float(risk_cfg.get("min_win_rate_for_auto_run"), 0.45):
        reason_codes.append("WR_OK")
    else:
        reason_codes.append("WR_LOW")
    if pnl7 >= 0:
        reason_codes.append("PNL7_OK")
    else:
        reason_codes.append("PNL7_NEGATIVE")
    if dd >= safe_float(risk_cfg.get("max_portfolio_drawdown_pct"), 8.0):
        reason_codes.append("DRAWDOWN_HIGH")
    if closed < int(risk_cfg.get("min_closed_trades_for_confidence", 5)):
        reason_codes.append("LOW_SAMPLE")
    if consecutive_losses > 0:
        reason_codes.append(f"LOSS_STREAK_{consecutive_losses}")
    if consecutive_losses >= auto_pause.get("max_consecutive_losses", 5):
        reason_codes.append("CONSECUTIVE_LOSS_LIMIT")
    # Check win rate vs min threshold
    min_wr = auto_pause.get("min_win_rate", 0.30)
    min_trades_wr = auto_pause.get("min_trades_for_wr_check", 10)
    if closed >= min_trades_wr and wr < min_wr:
        reason_codes.append(f"WR_BELOW_THRESHOLD_{min_wr}")

    action = "MONITOR"
    if regime == "risk_off" or "DRAWDOWN_HIGH" in reason_codes:
        action = "PAUSE"
    elif "CONSECUTIVE_LOSS_LIMIT" in reason_codes or "WR_BELOW_THRESHOLD" in reason_codes:
        action = "PAUSE"
    elif score >= 48 and bot_cfg.get("can_auto_start"):
        action = "RUN"
    elif score < 8 and bot_cfg.get("can_auto_pause"):
        action = "PAUSE"
    elif test_candidate:
        action = "SANDBOX_OBSERVE"

    return {
        "name": bot_cfg["name"],
        "label": bot_cfg.get("label", bot_cfg["name"]),
        "style": bot_cfg.get("style"),
        "target_regimes": bot_cfg.get("regimes", []),
        "test_candidate": test_candidate,
        "is_running": bool(status.get("is_running", False)),
        "score": round(score, 2),
        "recommended_action": action,
        "reason_codes": reason_codes,
        "metrics": {
            "balance_usd": safe_float(status.get("balance_usd"), safe_float(summary.get("final_balance"), 100.0)),
            "tokens_value_usd": safe_float(status.get("tokens_value_usd"), 0.0),
            "pnl_24h": pnl24,
            "pnl_7d": pnl7,
            "closed_trades": closed,
            "wins": int(perf.get("wins") or 0) if use_db_perf else int(summary.get("wins") or 0),
            "losses": int(perf.get("losses") or 0) if use_db_perf else int(summary.get("losses") or 0),
            "win_rate": round(wr, 4),
            "max_drawdown_pct": dd,
            "consecutive_losses": consecutive_losses,
            "latest_summary_at": summary.get("updated_at"),
        },
    }


def portfolio_guardrails(items: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    total_day = sum(safe_float(i["metrics"].get("pnl_24h")) for i in items)
    total_7d = sum(safe_float(i["metrics"].get("pnl_7d")) for i in items)
    max_dd = max([safe_float(i["metrics"].get("max_drawdown_pct")) for i in items] or [0.0])
    risk = cfg.get("risk", {})
    violations: list[str] = []
    if total_day <= -abs(safe_float(risk.get("max_daily_loss_usd"), 10.0)):
        violations.append("PORTFOLIO_DAILY_LOSS_LIMIT")
    if max_dd >= safe_float(risk.get("max_portfolio_drawdown_pct"), 8.0):
        violations.append("PORTFOLIO_DRAWDOWN_LIMIT")
    return {
        "pnl_24h": round(total_day, 4),
        "pnl_7d": round(total_7d, 4),
        "max_bot_drawdown_pct": round(max_dd, 3),
        "violations": violations,
        "risk_mode": bool(violations),
    }


PAUSED_BOTS = STATE_DIR / "paused_bots.json"


def _read_paused() -> set[str]:
    try:
        if PAUSED_BOTS.exists():
            data = json.loads(PAUSED_BOTS.read_text())
            return set(data.get("paused", []))
    except Exception:
        pass
    return set()


def _write_paused(paused: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PAUSED_BOTS.write_text(json.dumps({"paused": sorted(paused), "updated_at": utc_now()}, indent=2, ensure_ascii=False))


def is_paused_by_orchestrator(bot_name: str) -> bool:
    return bot_name in _read_paused()


def apply_logical_actions(items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply actions to bot_status in DB and maintain paused_bots.json for collector."""
    applied: list[dict[str, Any]] = []
    if not cfg.get("apply_actions", False) or psycopg is None:
        return applied
    allowed = {b["name"]: b for b in cfg.get("bots", []) if not b.get("test_candidate")}
    paused = _read_paused()
    try:
        with psycopg.connect(DB_URL) as c:
            with c.cursor() as cur:
                for item in items:
                    bcfg = allowed.get(item["name"])
                    if not bcfg:
                        continue
                    action = item["recommended_action"]
                    current_running = item.get("is_running", False)
                    if action == "RUN" and bcfg.get("can_auto_start") and not current_running:
                        cur.execute("update bot_status set is_running=true, updated_at=now() where bot_name=%s", [item["name"]])
                        paused.discard(item["name"])
                        applied.append({"bot": item["name"], "action": "set_running_true"})
                    elif action == "PAUSE" and bcfg.get("can_auto_pause") and current_running:
                        cur.execute("update bot_status set is_running=false, updated_at=now() where bot_name=%s", [item["name"]])
                        paused.add(item["name"])
                        applied.append({"bot": item["name"], "action": "set_running_false"})
        _write_paused(paused)
    except Exception as e:
        applied.append({"error": str(e)[:180]})
    return applied


def run() -> dict[str, Any]:
    cfg = load_json(CONFIG, {"enabled": False, "bots": []})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = utc_now()
    if not cfg.get("enabled", False):
        state = {"enabled": False, "ts": ts, "note": "orchestrator disabled"}
        (STATE_DIR / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return state

    merged = resolve_regime(cfg)
    overall_regime = merged["regime"]

    auto_pause = cfg.get("auto_pause", {})

    items: list[dict[str, Any]] = []
    for bot_cfg in cfg.get("bots", []):
        m = db_metrics(bot_cfg["name"])
        m["latest_summary"] = latest_summary(bot_cfg.get("prefix", bot_cfg["name"]))
        m["_consecutive_losses"] = db_consecutive_losses(bot_cfg["name"], limit=20)
        items.append(score_bot(bot_cfg, overall_regime, m, cfg.get("risk", {}), auto_pause))

    guard = portfolio_guardrails(items, cfg)
    if guard["risk_mode"] or overall_regime == "risk_off":
        for item in items:
            if not item.get("test_candidate") and item["recommended_action"] != "PAUSE":
                item["recommended_action"] = "PAUSE"
                if "PORTFOLIO_GUARD" not in item["reason_codes"]:
                    item["reason_codes"].append("PORTFOLIO_GUARD")

    items.sort(key=lambda x: x["score"], reverse=True)
    applied = apply_logical_actions(items, cfg)
    best = items[0] if items else None
    state = {
        "enabled": True,
        "paper_only": bool(cfg.get("paper_only", True)),
        "apply_actions": bool(cfg.get("apply_actions", False)),
        "ts": ts,
        "regime": merged,
        "guardrails": guard,
        "best_candidate": best,
        "bots": items,
        "applied_actions": applied,
        "summary": (
            f"Régimen {overall_regime} ({merged['confidence']}) via {merged.get('source', 'heuristic')}. "
            f"Mejor candidato: {best['label']} -> {best['recommended_action']}" if best else "Sin candidatos"
        ),
    }
    (STATE_DIR / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False))
    with (STATE_DIR / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "regime": merged, "guardrails": guard, "best_candidate": best, "bots": items[:8]}, ensure_ascii=False) + "\n")
    with (STATE_DIR / "orchestrator.log").open("a", encoding="utf-8") as f:
        f.write(f"{ts} {state['summary']} guardrails={guard['violations']} apply={state['apply_actions']}\n")
    return state


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
