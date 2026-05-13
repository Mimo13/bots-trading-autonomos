#!/usr/bin/env python3
"""
PortfolioPaperBot v2 — Rebalanceo sistemático con state tracking + decisions log.

Toma un snapshot del portfolio real de Binance como estado inicial,
mantiene un state file con las posiciones paper actuales, consulta precios
en cada ciclo y solo genera trades cuando la desviación supera el umbral.

Output:
  - trades_log.csv     → trades ejecutados (formato estándar para collector)
  - summary.json       → resumen del ciclo
  - decisions.jsonl    → auditoría de cada decisión (por qué tradeó/no tradeó)
  - state.json         → estado actual de posiciones paper (para el siguiente ciclo)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "portfolio"
STATE_FILE = STATE_DIR / "state.json"
SNAPSHOT_FILE = STATE_DIR / "snapshot.json"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"

# Mapa asset_key → símbolo para consulta de precio
PRICE_SYMBOLS: dict[str, Optional[str]] = {
    "XRP": "XRPUSDT",
    "PEPE": "PEPEUSDT",
    "SOL": "SOLUSDT",
    "HBAR": "HBARUSDT",
    "TOWNS": "TOWNSUSDT",
    "ADA": "ADAUSDT",
    "LAYER": "LAYERUSDT",
}
# Asset que representa stablecoin (no consulta precio)
STABLECOINS = {"USDC", "USDT", "BUSD", "DAI"}


@dataclass
class PortfolioConfig:
    rebalance_threshold_pct: float = 5.0
    min_rebalance_usd: float = 1.0
    max_trades_per_cycle: int = 6
    initial_balance: float = 0.0
    price_fallback_pct: float = 0.0  # si no hay precio, usar este cambio % desde snapshot
    targets_sideways: dict = None
    targets_bull: dict = None
    targets_bear: dict = None

    def __post_init__(self):
        if self.targets_sideways is None:
            self.targets_sideways = {"XRP": 35, "PEPE": 10, "USDC": 30, "SOL": 15, "HBAR": 10}
        if self.targets_bull is None:
            self.targets_bull = {"XRP": 40, "PEPE": 15, "USDC": 15, "SOL": 20, "HBAR": 10}
        if self.targets_bear is None:
            self.targets_bear = {"XRP": 20, "PEPE": 5, "USDC": 55, "SOL": 10, "HBAR": 10}


# ──────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_asset_key(asset_name: str) -> str:
    n = asset_name.upper()
    return n[2:] if n.startswith("LD") else n


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def fetch_price(symbol: str) -> Optional[float]:
    """Consulta precio actual desde API pública."""
    if symbol in STABLECOINS:
        return 1.0
    symbol_map = PRICE_SYMBOLS.get(symbol)
    if not symbol_map:
        return None
    # Intentar Binance primero
    for base_url in [
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol_map}",
        f"https://api.mexc.com/api/v3/ticker/price?symbol={symbol_map}",
    ]:
        try:
            req = urllib.request.Request(base_url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                p = float(data.get("price", 0))
                if p > 0:
                    return p
        except Exception:
            continue
    return None


# ──────────────────────────────────────────────
# State management
# ──────────────────────────────────────────────

def init_state_from_snapshot(snapshot: dict) -> dict:
    """Crea estado inicial desde el snapshot del portfolio real."""
    now = utc_now()
    positions: dict[str, dict] = {}
    total_cash = 0.0
    for a in snapshot.get("assets", []):
        key = resolve_asset_key(a["asset"])
        usd = float(a.get("usd_value", 0))
        qty = float(a.get("free", 0))
        if usd < 0.01:
            total_cash += usd
            continue
        price = usd / qty if qty > 0 else 1.0
        positions[key] = {
            "qty": qty,
            "entry_price": price,
            "current_price": price,
            "usd_value": usd,
            "pct": float(a.get("pct", 0)),
            "last_updated": now,
        }
    # Cash inicial = 0 (todo está en posiciones, incluyendo stablecoins)
    return {
        "initialized_at": now,
        "last_cycle": now,
        "total_usd_snapshot": round(snapshot.get("total_usd", 100), 2),
        "total_usd_paper": round(snapshot.get("total_usd", 100), 2),
        "total_cash": 0.0,
        "cycle_count": 0,
        "positions": positions,
    }


def load_state() -> dict:
    state = load_json(STATE_FILE)
    if state and state.get("positions"):
        return state
    # Si no hay state, inicializar desde snapshot
    snapshot = load_json(SNAPSHOT_FILE, {})
    if snapshot and snapshot.get("assets"):
        state = init_state_from_snapshot(snapshot)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return state
    # Si no hay snapshot, crear estado vacío
    return {
        "initialized_at": utc_now(),
        "last_cycle": utc_now(),
        "total_usd_snapshot": 100.0,
        "total_usd_paper": 100.0,
        "total_cash": 0.0,
        "cycle_count": 0,
        "positions": {},
    }


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ──────────────────────────────────────────────
# Price update — actualiza valor paper con precios actuales
# ──────────────────────────────────────────────

def update_prices(state: dict, cfg: PortfolioConfig) -> dict:
    """Actualiza precios actuales de todas las posiciones paper. Retorna decisiones de precio."""
    now = utc_now()
    decisions: list[dict] = []
    positions = state.get("positions", {})
    total = state.get("total_cash", 0.0)

    for key, pos in positions.items():
        qty = pos.get("qty", 0)
        if qty <= 0:
            pos["usd_value"] = 0
            pos["current_price"] = pos.get("entry_price", 0)
            continue

        if key in STABLECOINS:
            pos["current_price"] = 1.0
            pos["usd_value"] = round(qty, 2)
            total += pos["usd_value"]
            continue

        price = fetch_price(key)
        if price is not None:
            old_value = pos.get("usd_value", 0)
            pos["current_price"] = price
            pos["usd_value"] = round(qty * price, 4)
            pos["last_updated"] = now
            change = pos["usd_value"] - old_value
            decisions.append({
                "ts": now, "type": "price_update", "asset": key,
                "price": round(price, 6), "qty": round(qty, 6),
                "usd_value": pos["usd_value"],
                "change_from_last": round(change, 4),
                "source": "binance",
            })
        else:
            # Fallback: mantener último valor conocido
            decisions.append({
                "ts": now, "type": "price_missing", "asset": key,
                "price": None, "qty": round(qty, 6),
                "usd_value": pos.get("usd_value", 0),
                "source": "fallback_last_value",
            })

        total += pos["usd_value"]

    state["total_usd_paper"] = round(total, 2)
    state["last_cycle"] = now
    return decisions


# ──────────────────────────────────────────────
# Target allocation
# ──────────────────────────────────────────────

def get_targets(cfg: PortfolioConfig, regime: str) -> dict[str, float]:
    if regime == "bull":
        return cfg.targets_bull
    elif regime in ("bear", "risk_off"):
        return cfg.targets_bear
    return cfg.targets_sideways


# ──────────────────────────────────────────────
# Rebalance logic + decisions
# ──────────────────────────────────────────────

def evaluate_allocation(
    state: dict, targets: dict, cfg: PortfolioConfig, regime: str, now: str
) -> tuple[list[dict], list[dict], dict]:
    """
    Evalúa la asignación actual paper vs target.
    Retorna: (trades, decisions, updated_state)
    """
    decisions: list[dict] = []
    trades: list[dict] = []
    positions = state.get("positions", {})
    total_paper = state.get("total_usd_paper", 0)
    threshold = cfg.rebalance_threshold_pct

    # Calcular % actual por asset
    current_allocation: dict[str, dict] = {}
    for key, pos in positions.items():
        usd = pos.get("usd_value", 0)
        pct = (usd / total_paper * 100) if total_paper > 0 else 0
        current_allocation[key] = {"usd": usd, "pct": pct, "qty": pos.get("qty", 0)}

    # Evaluar cada asset contra target
    deviations: list[dict] = []
    for key, target_pct in targets.items():
        curr = current_allocation.get(key, {"pct": 0, "usd": 0, "qty": 0})
        dev = curr["pct"] - target_pct
        abs_dev = abs(dev)
        entry = {
            "ts": now, "type": "allocation_check", "asset": key,
            "target_pct": target_pct, "current_pct": round(curr["pct"], 2),
            "current_usd": round(curr["usd"], 2),
            "qty": curr.get("qty", 0),
            "deviation_pct": round(dev, 2),
            "exceeds_threshold": abs_dev > threshold,
            "threshold": threshold,
            "would_action": "SELL" if dev > 0 else "BUY" if dev < 0 else "HOLD",
        }
        decisions.append(entry)
        if abs_dev > threshold:
            entry["reason"] = (
                f"{'Sobre' if dev > 0 else 'Infra'}ponderado por {abs_dev:.1f} puntos "
                f"(target {target_pct}%, actual {curr['pct']:.1f}%)"
            )
            deviations.append({**entry, "deviation": dev, "target_usd": total_paper * target_pct / 100})
        else:
            entry["reason"] = (
                f"Dentro del umbral ({abs_dev:.1f}% ≤ {threshold}%)"
                if abs_dev > 0 else "En objetivo exacto"
            )

    if not deviations:
        decisions.append({
            "ts": now, "type": "cycle_summary",
            "assets_evaluated": len(targets),
            "deviations_found": 0,
            "trades_generated": 0,
            "conclusion": "Sin trades necesarios — todos los activos dentro del umbral",
        })
        return trades, decisions, state

    # Generar trades — priorizar mayores desviaciones
    trades_made = 0
    for dev in sorted(deviations, key=lambda x: abs(x["deviation"]), reverse=True):
        if trades_made >= cfg.max_trades_per_cycle:
            break
        key = dev["asset"]
        diff = dev["current_usd"] - dev["target_usd"]
        side = "SELL" if diff > 0 else "BUY"
        abs_diff = abs(diff)
        if abs_diff < cfg.min_rebalance_usd:
            decisions.append({
                "ts": now, "type": "rebalance_skipped", "asset": key,
                "reason": f"Desviación ${abs_diff:.2f} menor que mínimo ${cfg.min_rebalance_usd}",
                "would_side": side, "would_amount": round(abs_diff, 2),
            })
            continue

        price = dev["current_usd"] / dev["qty"] if dev["qty"] > 0 else 1.0
        qty = abs_diff / price if price > 0 else 0
        if qty < 0.000001:
            continue

        trade = {
            "ts": now, "side": side, "symbol": key,
            "qty": round(qty, 10) if price < 0.001 else round(qty, 6),
            "usd_amount": round(abs_diff, 4),
            "pnl": 0.0,
            "reason": f"rebalance_{key}_{side}_dev_{dev['deviation_pct']:+.1f}pct",
        }
        if side == "SELL":
            entry_price = positions.get(key, {}).get("entry_price", price)
            pnl = round((price - entry_price) * qty, 4)
            trade["pnl"] = pnl
            # Reducir posición en state y añadir efectivo
            current_qty = positions.get(key, {}).get("qty", 0)
            new_qty = max(0, current_qty - qty)
            positions[key]["qty"] = new_qty
            positions[key]["usd_value"] = round(new_qty * price, 4)
            state["total_cash"] = round(state.get("total_cash", 0) + abs_diff, 2)
        elif side == "BUY":
            # Añadir posición en state
            current_qty = positions.get(key, {}).get("qty", 0) if key in positions else 0
            current_cost = positions.get(key, {}).get("usd_value", 0) if key in positions else 0
            new_qty = current_qty + qty
            new_cost = current_cost + abs_diff
            avg_price = new_cost / new_qty if new_qty > 0 else price
            if key not in positions:
                positions[key] = {"entry_price": avg_price, "current_price": price, "last_updated": now}
            else:
                positions[key]["entry_price"] = avg_price
                positions[key]["current_price"] = price
            positions[key]["qty"] = new_qty
            positions[key]["usd_value"] = round(new_qty * price, 4)
            # Reducir cash
            state["total_cash"] = round(state.get("total_cash", 0) - abs_diff, 2)

        trades.append(trade)
        trades_made += 1
        decisions.append({
            "ts": now, "type": "rebalance_executed", "asset": key,
            "side": side, "usd_amount": round(abs_diff, 4),
            "qty": round(qty, 6), "pnl": round(trade["pnl"], 4),
            "reason": trade["reason"],
        })

    # Recalcular total paper después de trades
    new_total = state.get("total_cash", 0)
    for key, pos in positions.items():
        new_total += pos.get("usd_value", 0)
    state["total_usd_paper"] = round(new_total, 2)
    state["positions"] = positions

    decisions.append({
        "ts": now, "type": "cycle_summary",
        "assets_evaluated": len(targets),
        "deviations_found": len(deviations),
        "trades_generated": len(trades),
        "total_paper_after": round(new_total, 2),
        "conclusion": f"{len(trades)} trades ejecutados, {len(deviations) - len(trades)} desviaciones ignoradas",
    })

    return trades, decisions, state


# ──────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────

def write_outputs(out_dir: Path, trades: list[dict], summary: dict, decisions: list[dict]):
    out_dir.mkdir(parents=True, exist_ok=True)

    # trades_log.csv
    tl = out_dir / "trades_log.csv"
    if trades:
        with tl.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "side", "symbol", "qty", "usd_amount", "pnl", "reason"])
            w.writeheader()
            for t in trades:
                w.writerow(t)
    else:
        tl.write_text("ts,side,symbol,qty,usd_amount,pnl,reason\n")

    # summary.json
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    # decisions.jsonl — append al log global
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_FILE.open("a", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"  → {len(trades)} trades → {tl}")
    print(f"  → {len(decisions)} decisiones → {DECISIONS_FILE}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PortfolioPaperBot v2")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--regime", type=str, default="sideways")
    parser.add_argument("--snapshot", type=str, default=str(SNAPSHOT_FILE))
    args = parser.parse_args()

    cfg_data = load_json(Path(args.config), {})
    cfg = PortfolioConfig(**{k: v for k, v in cfg_data.items() if k in PortfolioConfig.__dataclass_fields__})
    regime = args.regime
    now = utc_now()

    print(f"=== PortfolioPaperBot v2 ===")
    print(f"  Régimen: {regime}")
    print()

    # 1. Cargar o inicializar state
    state = load_state()
    cycle_num = state.get("cycle_count", 0) + 1
    print(f"  Ciclo #{cycle_num}")
    print(f"  Posiciones paper: {len(state.get('positions', {}))} assets")
    print(f"  Total paper antes: ${state.get('total_usd_paper', 0):.2f}")
    print(f"  Cash: ${state.get('total_cash', 0):.2f}")
    print()

    all_decisions: list[dict] = []

    # 2. Actualizar precios
    print("  [1/3] Consultando precios...")
    price_decisions = update_prices(state, cfg)
    all_decisions.extend(price_decisions)
    for d in price_decisions:
        t = d.get("type", "")
        if t == "price_update":
            print(f"    {d['asset']}: ${d['price']:.4f} → ${d['usd_value']:.2f} ({d['change_from_last']:+.4f})")
        elif t == "price_missing":
            print(f"    {d['asset']}: sin precio (usando último valor conocido)")
    print(f"  Total paper tras precios: ${state.get('total_usd_paper', 0):.2f}")
    print()

    # 3. Evaluar asignación vs target
    print("  [2/3] Evaluando asignación...")
    targets = get_targets(cfg, regime)
    trades, eval_decisions, state = evaluate_allocation(state, targets, cfg, regime, now)
    all_decisions.extend(eval_decisions)

    for d in eval_decisions:
        t = d.get("type", "")
        if t == "allocation_check":
            flag = "⚠️" if d.get("exceeds_threshold") else "✅"
            print(f"    {flag} {d['asset']:6s} target={d['target_pct']:.0f}% actual={d['current_pct']:.1f}% dev={d['deviation_pct']:+.1f}% → {d['reason']}")
        elif t == "rebalance_executed":
            print(f"    📊 {d['side']:4s} {d['asset']:6s} ${d['usd_amount']:.2f} qty={d['qty']:.6g} pnl=${d['pnl']}")
        elif t == "rebalance_skipped":
            print(f"    ⏭️  {d['asset']}: {d['reason']}")
        elif t == "cycle_summary":
            print(f"    📝 {d['conclusion']}")
    print()

    # 4. Guardar state actualizado
    state["cycle_count"] = cycle_num
    state["last_cycle"] = now
    save_state(state)
    print(f"  [3/3] State guardado en {STATE_FILE}")

    # 5. Summary
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    summary = {
        "bot_name": "portfolio_paper",
        "cycle_ts": now,
        "cycle_number": cycle_num,
        "regime": regime,
        "total_usd_snapshot": round(state.get("total_usd_snapshot", 0), 2),
        "total_usd_paper": round(state.get("total_usd_paper", 0), 2),
        "total_cash": round(state.get("total_cash", 0), 2),
        "cycle_pnl": round(total_pnl, 4),
        "trades_generated": len(trades),
        "decisions_count": len(all_decisions),
        "prices_updated": len([d for d in price_decisions if d.get("type") == "price_update"]),
        "prices_missing": len([d for d in price_decisions if d.get("type") == "price_missing"]),
        "target_allocation": targets,
        "assets_held": len(state.get("positions", {})),
        "final_balance": round(state.get("total_usd_paper", 0), 2),
        "final_equity": round(state.get("total_usd_paper", 0), 2),
    }

    out_dir = Path(args.output_dir)
    write_outputs(out_dir, trades, summary, all_decisions)

    if trades:
        print(f"\n  Trades ({len(trades)}):")
        for t in trades:
            print(f"    {t['ts']} {t['side']:4s} {t['symbol']:6s} qty={t['qty']:.6g} ${t['usd_amount']:.4f} pnl=${t['pnl']:+.4f}  [{t['reason']}]")
    else:
        print(f"  Sin trades — todos los activos dentro del umbral {cfg.rebalance_threshold_pct}%")


if __name__ == "__main__":
    main()
