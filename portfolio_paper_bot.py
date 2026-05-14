#!/usr/bin/env python3
"""
PortfolioPaperBot v3 — Grid Trading + Rebalanceo con state tracking.

Grid strategy: acumula tokens en dips, vende en spikes.
- Define niveles de compra/venta basados en % desde precio de referencia
- Cada nivel ejecuta un trade de tamaño fijo (order_usd)
- Recentra automáticamente cuando se llena un lado entero
- Allocation rebalancing como safety net (umbral amplio, 15%)

Output:
  - trades_log.csv     → trades ejecutados
  - summary.json       → resumen del ciclo
  - decisions.jsonl    → auditoría de cada decisión
  - state.json         → estado actual (posiciones + grid state)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "runtime" / "portfolio"
STATE_FILE = STATE_DIR / "state.json"
SNAPSHOT_FILE = STATE_DIR / "snapshot.json"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"

PRICE_SYMBOLS: dict[str, Optional[str]] = {
    "XRP": "XRPUSDT",
    "PEPE": "PEPEUSDT",
    "SOL": "SOLUSDT",
    "HBAR": "HBARUSDT",
    "TOWNS": "TOWNSUSDT",
    "ADA": "ADAUSDT",
    "LAYER": "LAYERUSDT",
}
STABLECOINS = {"USDC", "USDT", "BUSD", "DAI"}


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

@dataclass
class GridAssetConfig:
    spacing_pct: float = 3.0
    levels: int = 4
    order_usd: float = 10.0
    min_position_pct: float = 5.0
    max_position_pct: float = 50.0
    auto_recenter: bool = True


@dataclass
class PortfolioConfig:
    rebalance_threshold_pct: float = 15.0
    min_rebalance_usd: float = 3.0
    max_trades_per_cycle: int = 8
    initial_balance: float = 0.0
    price_fallback_pct: float = 0.0
    targets_sideways: dict = None
    targets_bull: dict = None
    targets_bear: dict = None
    grid_enabled: bool = True
    grid_config: dict[str, dict] = field(default_factory=dict)
    regime_confirm_cycles: int = 2
    regime_confidence_threshold: float = 0.35

    def __post_init__(self):
        if self.targets_sideways is None:
            self.targets_sideways = {"XRP": 35, "PEPE": 10, "USDC": 30, "SOL": 15, "HBAR": 10}
        if self.targets_bull is None:
            self.targets_bull = {"XRP": 40, "PEPE": 15, "USDC": 15, "SOL": 20, "HBAR": 10}
        if self.targets_bear is None:
            self.targets_bear = {"XRP": 20, "PEPE": 5, "USDC": 55, "SOL": 10, "HBAR": 10}

    def get_grid(self, asset: str) -> Optional[GridAssetConfig]:
        if not self.grid_enabled or asset not in self.grid_config:
            return None
        gc = self.grid_config[asset]
        return GridAssetConfig(**{k: v for k, v in gc.items() if k in GridAssetConfig.__dataclass_fields__})


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
    if symbol in STABLECOINS:
        return 1.0
    symbol_map = PRICE_SYMBOLS.get(symbol)
    if not symbol_map:
        return None
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


def read_orchestrator_regime_confidence() -> float:
    """Best-effort confidence read for regime smoothing."""
    try:
        sd = load_json(ROOT / "runtime" / "orchestrator" / "state.json", {})
        return float(sd.get("regime", {}).get("confidence", 0.0) or 0.0)
    except Exception:
        return 0.0


def stabilize_regime(requested_regime: str, state: dict, cfg: PortfolioConfig, now: str) -> tuple[str, dict]:
    """Avoid target churn from one noisy regime read."""
    requested = requested_regime if requested_regime in {"bull", "bear", "risk_off", "sideways"} else "sideways"
    confidence = read_orchestrator_regime_confidence()
    rs = state.setdefault("regime_state", {})
    active = rs.get("active_regime") or requested

    if requested == active:
        rs.update({
            "active_regime": active,
            "pending_regime": None,
            "pending_count": 0,
            "last_requested_regime": requested,
            "last_confidence": round(confidence, 4),
            "last_updated": now,
        })
        reason = "unchanged"
    elif requested == "risk_off" and confidence >= cfg.regime_confidence_threshold:
        active = requested
        rs.update({"active_regime": active, "pending_regime": None, "pending_count": 0})
        reason = f"risk_off_high_confidence_{confidence:.3f}"
    elif confidence >= cfg.regime_confidence_threshold:
        active = requested
        rs.update({"active_regime": active, "pending_regime": None, "pending_count": 0})
        reason = f"high_confidence_{confidence:.3f}"
    else:
        pending = rs.get("pending_regime")
        count = int(rs.get("pending_count", 0) or 0)
        count = count + 1 if pending == requested else 1
        if count >= max(1, int(cfg.regime_confirm_cycles)):
            active = requested
            rs.update({"active_regime": active, "pending_regime": None, "pending_count": 0})
            reason = f"confirmed_{count}_cycles_low_confidence_{confidence:.3f}"
        else:
            rs.update({"active_regime": active, "pending_regime": requested, "pending_count": count})
            reason = f"holding_{active}_pending_{requested}_{count}/{cfg.regime_confirm_cycles}_confidence_{confidence:.3f}"

        rs.update({
            "last_requested_regime": requested,
            "last_confidence": round(confidence, 4),
            "last_updated": now,
        })

    return active, {
        "ts": now,
        "type": "regime_check",
        "requested_regime": requested,
        "active_regime": active,
        "confidence": round(confidence, 4),
        "reason": reason,
    }

# ──────────────────────────────────────────────
# State management
# ──────────────────────────────────────────────

def init_state_from_snapshot(snapshot: dict) -> dict:
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
    return {
        "initialized_at": now,
        "last_cycle": now,
        "total_usd_snapshot": round(snapshot.get("total_usd", 100), 2),
        "total_usd_paper": round(snapshot.get("total_usd", 100), 2),
        "total_cash": 0.0,
        "cycle_count": 0,
        "positions": positions,
        "grid_state": {},
    }


def load_state() -> dict:
    state = load_json(STATE_FILE)
    if state and state.get("positions"):
        if "grid_state" not in state:
            state["grid_state"] = {}
        return state
    snapshot = load_json(SNAPSHOT_FILE, {})
    if snapshot and snapshot.get("assets"):
        state = init_state_from_snapshot(snapshot)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return state
    return {
        "initialized_at": utc_now(),
        "last_cycle": utc_now(),
        "total_usd_snapshot": 100.0,
        "total_usd_paper": 100.0,
        "total_cash": 0.0,
        "cycle_count": 0,
        "positions": {},
        "grid_state": {},
    }


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ──────────────────────────────────────────────
# Price update
# ──────────────────────────────────────────────

def update_prices(state: dict, cfg: PortfolioConfig) -> list[dict]:
    now = utc_now()
    decisions: list[dict] = []
    positions = state.get("positions", {})
    total = state.get("total_cash", 0.0)

    for key, pos in positions.items():
        qty = pos.get("qty", 0)
        if qty <= 0:
            pos["usd_value"] = 0
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


def compute_hodl_benchmark(snapshot_path: Path, state: dict) -> dict:
    """Value the original Binance snapshot with current paper prices."""
    snapshot = load_json(snapshot_path, {})
    if not snapshot or not snapshot.get("assets"):
        return {}
    prices = {k: v.get("current_price") for k, v in state.get("positions", {}).items()}
    external_cash_flows = float(state.get("external_cash_flows_usd", 0.0) or 0.0)
    total = external_cash_flows
    assets = []
    for a in snapshot.get("assets", []):
        key = resolve_asset_key(a.get("asset", ""))
        qty = float(a.get("free", 0) or 0)
        old_usd = float(a.get("usd_value", 0) or 0)
        price = prices.get(key)
        value = qty * price if price and qty > 0 else old_usd
        total += value
        assets.append({"asset": key, "qty": qty, "current_price": price, "hodl_usd": round(value, 4)})
    snapshot_total = float(snapshot.get("total_usd", total) or total)
    adjusted_cost_basis = snapshot_total + external_cash_flows
    mirror_total = float(state.get("total_usd_paper", 0) or 0)
    return {
        "snapshot_total_usd": round(snapshot_total, 2),
        "external_cash_flows_usd": round(external_cash_flows, 2),
        "adjusted_cost_basis_usd": round(adjusted_cost_basis, 2),
        "hodl_total_usd": round(total, 2),
        "hodl_pnl_usd": round(total - adjusted_cost_basis, 2),
        "hodl_pnl_pct": round(((total / adjusted_cost_basis) - 1) * 100, 2) if adjusted_cost_basis else 0,
        "mirror_vs_hodl_usd": round(mirror_total - total, 2),
        "mirror_vs_hodl_pct": round(((mirror_total / total) - 1) * 100, 2) if total else 0,
        "assets": assets,
    }

# ──────────────────────────────────────────────
# Grid Trading Engine
# ──────────────────────────────────────────────

def init_grid_state(state: dict, asset: str, price: float, grid_cfg: GridAssetConfig):
    """Inicializa el estado del grid para un asset si no existe."""
    gs = state.setdefault("grid_state", {})
    if asset not in gs:
        gs[asset] = {
            "ref_price": round(price, 8),
            "filled_buy": [],
            "filled_sell": [],
            "initialized_at": utc_now(),
        }


def _get_grid_levels(ref_price: float, grid_cfg: GridAssetConfig):
    """Retorna (buy_levels, sell_levels) como listas de (indice, precio)."""
    spacing = grid_cfg.spacing_pct / 100.0
    levels = grid_cfg.levels
    buy_levels = [(i + 1, round(ref_price * (1 - spacing * (i + 1)), 10)) for i in range(levels)]
    sell_levels = [(i + 1, round(ref_price * (1 + spacing * (i + 1)), 10)) for i in range(levels)]
    return buy_levels, sell_levels


def evaluate_grid(
    state: dict,
    cfg: PortfolioConfig,
    price_updates: list[dict],
    regime: str,
    now: str,
) -> tuple[list[dict], list[dict], dict]:
    """
    Evalúa grid levels para cada asset. Genera trades cuando el precio cruza niveles.
    Respeta min/max position allocation y cash disponible.
    """
    decisions: list[dict] = []
    trades: list[dict] = []
    positions = state.get("positions", {})
    cash = state.get("total_cash", 0.0)
    total_paper = state.get("total_usd_paper", 200.0)
    grid_state = state.get("grid_state", {})

    # Build price map from updates
    prices: dict[str, float] = {}
    for d in price_updates:
        if d.get("type") == "price_update" and d.get("price"):
            prices[d["asset"]] = d["price"]

    for asset, price in prices.items():
        grid_cfg = cfg.get_grid(asset)
        if grid_cfg is None:
            continue

        # Init grid if needed
        init_grid_state(state, asset, price, grid_cfg)
        gs = grid_state[asset]

        ref_price = gs.get("ref_price", price)
        filled_buy = set(gs.get("filled_buy", []))
        filled_sell = set(gs.get("filled_sell", []))

        buy_levels, sell_levels = _get_grid_levels(ref_price, grid_cfg)
        next_buy = next(((i, px) for i, px in buy_levels if i not in filled_buy), None)
        next_sell = next(((i, px) for i, px in sell_levels if i not in filled_sell), None)
        decisions.append({
            "ts": now, "type": "grid_check", "asset": asset,
            "price": round(price, 8), "ref_price": round(ref_price, 8),
            "spacing_pct": grid_cfg.spacing_pct,
            "next_buy_level": next_buy[0] if next_buy else None,
            "next_buy_price": round(next_buy[1], 10) if next_buy else None,
            "next_buy_distance_pct": round(((price / next_buy[1]) - 1) * 100, 2) if next_buy else None,
            "next_sell_level": next_sell[0] if next_sell else None,
            "next_sell_price": round(next_sell[1], 10) if next_sell else None,
            "next_sell_distance_pct": round(((next_sell[1] / price) - 1) * 100, 2) if next_sell else None,
            "filled_buy": sorted(filled_buy), "filled_sell": sorted(filled_sell),
            "reason": "Grid observado; sin trade salvo cruce de nivel",
        })

        # Current position
        pos = positions.get(asset, {"qty": 0, "usd_value": 0})
        qty_held = pos.get("qty", 0)
        usd_held = pos.get("usd_value", 0)
        pct = (usd_held / total_paper * 100) if total_paper > 0 else 0

        # ── BUY levels (price dropped below level) ──
        for level_idx, buy_price in buy_levels:
            if level_idx in filled_buy:
                continue
            if price <= buy_price:
                # Check allocation bounds
                target_usd = grid_cfg.order_usd
                new_pct = (usd_held + target_usd) / total_paper * 100 if total_paper > 0 else 0
                if new_pct > grid_cfg.max_position_pct:
                    decisions.append({
                        "ts": now, "type": "grid_skipped", "asset": asset,
                        "level": level_idx, "side": "BUY",
                        "reason": f"Max allocation ({grid_cfg.max_position_pct}%) would be exceeded: {new_pct:.1f}%",
                        "price": price, "buy_price": buy_price,
                    })
                    filled_buy.add(level_idx)
                    continue
                if cash < target_usd:
                    decisions.append({
                        "ts": now, "type": "grid_skipped", "asset": asset,
                        "level": level_idx, "side": "BUY",
                        "reason": f"Insufficient cash: ${cash:.2f} < ${target_usd}",
                        "price": price, "buy_price": buy_price,
                    })
                    continue

                qty = target_usd / price
                trade = {
                    "ts": now, "side": "BUY", "symbol": asset,
                    "qty": round(qty, 10) if price < 0.01 else round(qty, 6),
                    "usd_amount": round(target_usd, 4),
                    "pnl": 0.0,
                    "reason": f"grid_buy_L{level_idx}_at_{price:.6g}",
                }

                # Update position
                old_qty = qty_held
                old_cost = usd_held
                new_qty = old_qty + qty
                new_cost = old_cost + target_usd
                avg_price = new_cost / new_qty if new_qty > 0 else price
                if asset not in positions:
                    positions[asset] = {"entry_price": avg_price, "current_price": price, "last_updated": now}
                else:
                    positions[asset]["entry_price"] = avg_price
                    positions[asset]["current_price"] = price
                    positions[asset]["last_updated"] = now
                positions[asset]["qty"] = new_qty
                positions[asset]["usd_value"] = round(new_qty * price, 4)
                cash -= target_usd
                qty_held = new_qty
                usd_held = positions[asset]["usd_value"]

                filled_buy.add(level_idx)
                trades.append(trade)
                decisions.append({
                    "ts": now, "type": "grid_trade", "asset": asset,
                    "side": "BUY", "level": level_idx,
                    "price": round(price, 8), "ref_price": round(ref_price, 8),
                    "usd_amount": round(target_usd, 4),
                    "qty": round(qty, 6),
                    "reason": f"Dip → comprando en nivel {level_idx} ({grid_cfg.spacing_pct * level_idx}% abajo)",
                })

        # ── SELL levels (price rose above level) ──
        for level_idx, sell_price in sell_levels:
            if level_idx in filled_sell:
                continue
            if price >= sell_price:
                # Check we have enough tokens
                target_usd = grid_cfg.order_usd
                if usd_held < target_usd * 0.5:
                    decisions.append({
                        "ts": now, "type": "grid_skipped", "asset": asset,
                        "level": level_idx, "side": "SELL",
                        "reason": f"Insufficient position: ${usd_held:.2f} < ${target_usd * 0.5:.2f}",
                        "price": price, "sell_price": sell_price,
                    })
                    filled_sell.add(level_idx)
                    continue

                # Check min allocation bound
                new_usd = usd_held - target_usd
                new_pct = new_usd / total_paper * 100 if total_paper > 0 else 0
                if new_pct < grid_cfg.min_position_pct:
                    decisions.append({
                        "ts": now, "type": "grid_skipped", "asset": asset,
                        "level": level_idx, "side": "SELL",
                        "reason": f"Min allocation ({grid_cfg.min_position_pct}%) bound: {new_pct:.1f}%",
                        "price": price, "sell_price": sell_price,
                    })
                    filled_sell.add(level_idx)
                    continue

                qty = target_usd / price
                if qty > qty_held:
                    qty = qty_held * 0.95  # sell almost all
                    target_usd = qty * price

                entry_price = pos.get("entry_price", price)
                pnl = round((price - entry_price) * qty, 4)

                trade = {
                    "ts": now, "side": "SELL", "symbol": asset,
                    "qty": round(qty, 10) if price < 0.01 else round(qty, 6),
                    "usd_amount": round(target_usd, 4),
                    "pnl": pnl,
                    "reason": f"grid_sell_L{level_idx}_at_{price:.6g}",
                }

                # Update position
                new_qty = max(0, qty_held - qty)
                positions[asset]["qty"] = new_qty
                positions[asset]["usd_value"] = round(new_qty * price, 4)
                positions[asset]["last_updated"] = now
                cash += target_usd
                qty_held = new_qty
                usd_held = positions[asset]["usd_value"]

                filled_sell.add(level_idx)
                trades.append(trade)
                decisions.append({
                    "ts": now, "type": "grid_trade", "asset": asset,
                    "side": "SELL", "level": level_idx,
                    "price": round(price, 8), "ref_price": round(ref_price, 8),
                    "usd_amount": round(target_usd, 4),
                    "qty": round(qty, 6), "pnl": pnl,
                    "reason": f"Spike → vendiendo en nivel {level_idx} (+{grid_cfg.spacing_pct * level_idx}% arriba)",
                })

        # ── Recenter grid if all levels filled on one side ──
        if grid_cfg.auto_recenter:
            all_buy_filled = len(filled_buy) >= grid_cfg.levels
            all_sell_filled = len(filled_sell) >= grid_cfg.levels
            if all_buy_filled or all_sell_filled:
                direction = "buy" if all_buy_filled else "sell"
                decisions.append({
                    "ts": now, "type": "grid_recenter", "asset": asset,
                    "old_ref": round(ref_price, 8), "new_ref": round(price, 8),
                    "direction": direction,
                    "reason": f"Todos los niveles {direction} llenos → recentrando grid a ${price:.6g}",
                })
                gs["ref_price"] = round(price, 8)
                gs["filled_buy"] = []
                gs["filled_sell"] = []
            else:
                gs["filled_buy"] = sorted(filled_buy)
                gs["filled_sell"] = sorted(filled_sell)
        else:
            gs["filled_buy"] = sorted(filled_buy)
            gs["filled_sell"] = sorted(filled_sell)

    # Save cash and positions
    state["total_cash"] = round(cash, 2)
    state["positions"] = positions
    state["grid_state"] = grid_state

    return trades, decisions, state


# ──────────────────────────────────────────────
# Allocation rebalancing (safety net)
# ──────────────────────────────────────────────

def get_targets(cfg: PortfolioConfig, regime: str) -> dict[str, float]:
    if regime == "bull":
        return cfg.targets_bull
    elif regime in ("bear", "risk_off"):
        return cfg.targets_bear
    return cfg.targets_sideways


def evaluate_allocation(
    state: dict, targets: dict, cfg: PortfolioConfig, regime: str, now: str,
    already_traded_assets: set,
) -> tuple[list[dict], list[dict], dict]:
    """
    Evalúa asignación vs target. Solo actúa como safety net:
    - Threshold amplio (15%) para no interferir con el grid
    - No rebalancea assets que ya tuvieron grid trades este ciclo
    """
    decisions: list[dict] = []
    trades: list[dict] = []
    positions = state.get("positions", {})
    total_paper = state.get("total_usd_paper", 0)
    threshold = cfg.rebalance_threshold_pct
    cash = state.get("total_cash", 0.0)

    current_allocation: dict[str, dict] = {}
    for key, pos in positions.items():
        usd = pos.get("usd_value", 0)
        pct = (usd / total_paper * 100) if total_paper > 0 else 0
        current_allocation[key] = {"usd": usd, "pct": pct, "qty": pos.get("qty", 0)}

    deviations: list[dict] = []
    for key, target_pct in targets.items():
        if key in already_traded_assets:
            continue  # no tocar assets que ya tuvieron grid trades
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
                f"{'Sobre' if dev > 0 else 'Infra'}ponderado por {abs_dev:.1f}pp "
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
            "conclusion": "Allocation OK — sin rebalanceos necesarios",
        })
        return trades, decisions, state

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
            current_qty = positions.get(key, {}).get("qty", 0)
            new_qty = max(0, current_qty - qty)
            positions[key]["qty"] = new_qty
            positions[key]["usd_value"] = round(new_qty * price, 4)
            cash += abs_diff
        elif side == "BUY":
            if cash < abs_diff:
                decisions.append({
                    "ts": now, "type": "rebalance_skipped", "asset": key,
                    "reason": f"Cash insuficiente: ${cash:.2f} < ${abs_diff:.2f}",
                })
                continue
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
                positions[key]["last_updated"] = now
            positions[key]["qty"] = new_qty
            positions[key]["usd_value"] = round(new_qty * price, 4)
            cash -= abs_diff

        trades.append(trade)
        trades_made += 1
        decisions.append({
            "ts": now, "type": "rebalance_executed", "asset": key,
            "side": side, "usd_amount": round(abs_diff, 4),
            "qty": round(qty, 6), "pnl": round(trade["pnl"], 4),
            "reason": trade["reason"],
        })

    new_total = cash
    for key, pos in positions.items():
        new_total += pos.get("usd_value", 0)
    state["total_usd_paper"] = round(new_total, 2)
    state["total_cash"] = round(cash, 2)
    state["positions"] = positions

    decisions.append({
        "ts": now, "type": "cycle_summary",
        "assets_evaluated": len(targets),
        "deviations_found": len(deviations),
        "trades_generated": len(trades),
        "total_paper_after": round(new_total, 2),
        "conclusion": f"{len(trades)} rebalanceos ejecutados",
    })

    return trades, decisions, state


# ──────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────

def write_outputs(out_dir: Path, trades: list[dict], summary: dict, decisions: list[dict]):
    out_dir.mkdir(parents=True, exist_ok=True)

    tl = out_dir / "trades_log.csv"
    if trades:
        with tl.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "side", "symbol", "qty", "usd_amount", "pnl", "reason"])
            w.writeheader()
            for t in trades:
                w.writerow(t)
    else:
        tl.write_text("ts,side,symbol,qty,usd_amount,pnl,reason\n")

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

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
    parser = argparse.ArgumentParser(description="PortfolioPaperBot v3 — Grid + Rebalance")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--regime", type=str, default="sideways")
    parser.add_argument("--snapshot", type=str, default=str(SNAPSHOT_FILE))
    args = parser.parse_args()

    cfg_data = load_json(Path(args.config), {})
    cfg = PortfolioConfig(**{k: v for k, v in cfg_data.items() if k in PortfolioConfig.__dataclass_fields__})
    regime = args.regime
    now = utc_now()

    print(f"=== PortfolioPaperBot v3 ===  Grid: {'ON' if cfg.grid_enabled else 'OFF'}")
    print(f"  Régimen: {regime}")
    print()

    # 1. Load state
    state = load_state()
    raw_regime = regime
    regime, regime_decision = stabilize_regime(raw_regime, state, cfg, now)
    if regime != raw_regime:
        print(f"  Régimen efectivo: {regime} (pedido={raw_regime}; {regime_decision['reason']})")
    cycle_num = state.get("cycle_count", 0) + 1
    print(f"  Ciclo #{cycle_num}")
    print(f"  Posiciones: {len(state.get('positions', {}))} assets")
    print(f"  Total paper: ${state.get('total_usd_paper', 0):.2f}")
    print(f"  Cash: ${state.get('total_cash', 0):.2f}")
    print()

    all_decisions: list[dict] = [regime_decision]
    all_trades: list[dict] = []

    # 2. Update prices
    print("  [1/3] Consultando precios...")
    price_decisions = update_prices(state, cfg)
    all_decisions.extend(price_decisions)
    for d in price_decisions:
        if d.get("type") == "price_update":
            pct_sign = "🔺" if d.get("change_from_last", 0) > 0 else "🔻" if d.get("change_from_last", 0) < 0 else "➡️"
            print(f"    {pct_sign} {d['asset']:6s} ${d['price']:.6g} → ${d['usd_value']:.2f}")
        elif d.get("type") == "price_missing":
            print(f"    ⚠️  {d['asset']}: sin precio (manteniendo valor)")
    print(f"  Total paper: ${state.get('total_usd_paper', 0):.2f}")
    print()

    # 3. GRID TRADING (primary strategy)
    grid_traded_assets: set = set()
    if cfg.grid_enabled:
        print("  [2/3] Grid Trading...")
        grid_trades, grid_decisions, state = evaluate_grid(state, cfg, price_decisions, regime, now)
        all_trades.extend(grid_trades)
        all_decisions.extend(grid_decisions)
        grid_traded_assets = {t["symbol"] for t in grid_trades}

        for d in grid_decisions:
            t = d.get("type", "")
            if t == "grid_trade":
                emoji = "📉" if d["side"] == "BUY" else "📈"
                print(f"    {emoji} {d['side']:4s} {d['asset']:6s} L{d['level']} ${d['usd_amount']:.2f} @ ${d['price']:.6g} — {d['reason']}")
            elif t == "grid_skipped":
                print(f"    ⏭️  {d['asset']} {d['side']} L{d['level']}: {d['reason']}")
            elif t == "grid_recenter":
                print(f"    🔄 {d['asset']}: {d['reason']}")

        grid_assets_active = [a for a, gs in state.get("grid_state", {}).items() if gs.get("filled_buy") or gs.get("filled_sell")]
        for a in grid_assets_active:
            gs = state["grid_state"][a]
            print(f"    📊 {a} grid: ref=${gs['ref_price']:.6g} buys={gs['filled_buy']} sells={gs['filled_sell']}")
        print()

    # 4. Allocation rebalance (safety net, avoids assets that had grid trades)
    print("  [3/3] Allocation check...")
    targets = get_targets(cfg, regime)
    rebal_trades, rebal_decisions, state = evaluate_allocation(
        state, targets, cfg, regime, now, grid_traded_assets
    )
    all_trades.extend(rebal_trades)
    all_decisions.extend(rebal_decisions)

    for d in rebal_decisions:
        t = d.get("type", "")
        if t == "allocation_check":
            flag = "⚠️" if d.get("exceeds_threshold") else "✅"
            print(f"    {flag} {d['asset']:6s} target={d['target_pct']:.0f}% actual={d['current_pct']:.1f}% → {d['reason']}")
        elif t == "rebalance_executed":
            print(f"    ⚖️  {d['side']:4s} {d['asset']:6s} ${d['usd_amount']:.2f} qty={d['qty']:.6g} pnl=${d['pnl']}")
        elif t == "cycle_summary":
            print(f"    📝 {d['conclusion']}")
    print()

    # 5. Save state
    state["cycle_count"] = cycle_num
    state["last_cycle"] = now
    save_state(state)
    print(f"  ✅ State guardado ({STATE_FILE})")

    # 6. Summary
    total_pnl = sum(t.get("pnl", 0) for t in all_trades)
    hodl_benchmark = compute_hodl_benchmark(Path(args.snapshot), state)
    summary = {
        "bot_name": "portfolio_paper",
        "cycle_ts": now,
        "cycle_number": cycle_num,
        "regime": regime,
        "requested_regime": raw_regime,
        "regime_reason": regime_decision.get("reason"),
        "total_usd_snapshot": round(state.get("total_usd_snapshot", 0), 2),
        "total_usd_paper": round(state.get("total_usd_paper", 0), 2),
        "total_cash": round(state.get("total_cash", 0), 2),
        "external_cash_flows_usd": round(state.get("external_cash_flows_usd", 0.0), 2),
        "adjusted_pnl_usd": round(state.get("total_usd_paper", 0) - state.get("total_usd_snapshot", 0) - state.get("external_cash_flows_usd", 0.0), 2),
        "cycle_pnl": round(total_pnl, 4),
        "grid_trades": len([t for t in all_trades if "grid" in t.get("reason", "")]),
        "rebalance_trades": len([t for t in all_trades if "rebalance" in t.get("reason", "")]),
        "trades_generated": len(all_trades),
        "decisions_count": len(all_decisions),
        "target_allocation": targets,
        "assets_held": len(state.get("positions", {})),
        "final_balance": round(state.get("total_usd_paper", 0), 2),
        "hodl_benchmark": hodl_benchmark,
        "mirror_vs_hodl_usd": hodl_benchmark.get("mirror_vs_hodl_usd"),
        "mirror_vs_hodl_pct": hodl_benchmark.get("mirror_vs_hodl_pct"),
        "grid_state": {a: {"ref_price": gs.get("ref_price"), "filled_buy": gs.get("filled_buy", []), "filled_sell": gs.get("filled_sell", [])}
                       for a, gs in state.get("grid_state", {}).items()},
    }

    out_dir = Path(args.output_dir)
    write_outputs(out_dir, all_trades, summary, all_decisions)

    if all_trades:
        grid_count = len([t for t in all_trades if "grid" in t.get("reason", "")])
        print(f"\n  📊 Trades ({len(all_trades)} total, {grid_count} grid):")
        for t in all_trades:
            emoji = "📉" if t["side"] == "BUY" else "📈"
            print(f"    {emoji} {t['side']:4s} {t['symbol']:6s} qty={t['qty']:.6g} ${t['usd_amount']:.4f} pnl=${t['pnl']:+.4f}  [{t['reason']}]")
    else:
        print(f"\n  💤 Sin trades — precios estables, sin cruce de niveles grid")


if __name__ == "__main__":
    main()
