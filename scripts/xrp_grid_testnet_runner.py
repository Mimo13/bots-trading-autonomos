#!/usr/bin/env python3
"""XRP Testnet runner.

Runs isolated logical XRP grid bots on Binance Spot Testnet:
- xrp_grid_testnet_100: starts by buying ~100 EUR worth of USDC via EURUSDC.
- xrp_grid_testnet_portfolio: models Binance hot-wallet XRP+USDC.
- xrp_grid_testnet_hot_cold: models Binance hot-wallet XRP+USDC plus cold-wallet XRP.

The account is shared, but every order is tagged with a clientOrderId prefix per bot and state is
persisted in runtime/xrp_grid_testnet/state_*.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import psycopg

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "xrp_grid_testnet"
DB_URL = os.getenv("DATABASE_URL", "postgresql:///bots_dashboard")

import sys
sys.path.insert(0, str(ROOT))
from binance_client import load_client, BinanceClient  # noqa: E402
from xrp_grid_bot import compute_atr  # noqa: E402

SYMBOL = "XRPUSDC"
QUOTE = "USDC"
BASE = "XRP"
EUR_PAIR = "EURUSDC"
FEE_RATE = 0.001  # realistic fallback when fills don't report commission in quote/base
HOT_WALLET_XRP = float(os.getenv("XRP_TESTNET_HOT_WALLET_XRP", "76.36607565"))
HOT_WALLET_USDC = float(os.getenv("XRP_TESTNET_HOT_WALLET_USDC", "23.8258073"))
COLD_WALLET_XRP = float(os.getenv("XRP_TESTNET_COLD_WALLET_XRP", "652.938679"))

BOT_CONFIGS = {
    "xrp_grid_testnet_100": {
        "label": "XRP Testnet 100€",
        "prefix": "xgt100",
        "init_mode": "eur_to_usdc",
        "eur_amount": 100.0,
        "grid_levels_each_side": 3,
        "grid_spacing_atr": 1.0,
        "grid_atr_period": 14,
        "risk_per_trade_pct": 0.10,
        "max_xrp_hold_pct": 0.70,
        "rebalance_threshold_atr": 2.0,
        "min_edge_after_fee_pct": 0.0015,
        "max_open_orders_side": 3,
    },
    "xrp_grid_testnet_portfolio": {
        "label": "XRP Testnet Binance",
        "prefix": "xgtpf",
        "init_mode": "configured_portfolio",
        "initial_xrp": HOT_WALLET_XRP,
        "initial_usdc": HOT_WALLET_USDC,
        "portfolio_scope": "binance_hot_wallet",
        "grid_levels_each_side": 3,
        "grid_spacing_atr": 1.0,
        "grid_atr_period": 14,
        "risk_per_trade_pct": 0.10,
        "max_xrp_hold_pct": 0.90,  # Mimo is OK holding XRP inventory.
        "rebalance_threshold_atr": 2.0,
        "min_edge_after_fee_pct": 0.0015,
        "max_open_orders_side": 3,
    },
    "xrp_grid_testnet_hot_cold": {
        "label": "XRP Testnet Binance + Cold",
        "prefix": "xgtpc",
        "init_mode": "configured_portfolio",
        "initial_xrp": HOT_WALLET_XRP + COLD_WALLET_XRP,
        "initial_usdc": HOT_WALLET_USDC,
        "portfolio_scope": "binance_hot_wallet_plus_cold_xrp",
        "grid_levels_each_side": 3,
        "grid_spacing_atr": 1.0,
        "grid_atr_period": 14,
        "risk_per_trade_pct": 0.10,
        "max_xrp_hold_pct": 0.90,  # Mimo is OK holding XRP inventory.
        "rebalance_threshold_atr": 2.0,
        "min_edge_after_fee_pct": 0.0015,
        "max_open_orders_side": 3,
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def state_path(bot_name: str) -> Path:
    return RUNTIME / f"state_{bot_name}.json"


def log_path(bot_name: str) -> Path:
    return RUNTIME / f"events_{bot_name}.jsonl"


def load_state(bot_name: str) -> Optional[dict]:
    p = state_path(bot_name)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_state(bot_name: str, state: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    tmp = state_path(bot_name).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(state_path(bot_name))


def append_event(bot_name: str, event: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with log_path(bot_name).open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def dec_down(value: float, step: float) -> str:
    d = Decimal(str(value))
    s = Decimal(str(step))
    if s == 0:
        return str(value)
    return format((d / s).to_integral_value(rounding=ROUND_DOWN) * s, "f")


def get_filter_values(client: BinanceClient, symbol: str) -> dict:
    filters = client.get_filters(symbol)
    lot = filters.get("LOT_SIZE", {})
    price = filters.get("PRICE_FILTER", {})
    notional = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
    return {
        "step": float(lot.get("stepSize", "0.000001")),
        "min_qty": float(lot.get("minQty", "0.000001")),
        "tick": float(price.get("tickSize", "0.0001")),
        "min_notional": float(notional.get("minNotional", "1")),
    }


def round_qty(client: BinanceClient, symbol: str, qty: float) -> float:
    f = get_filter_values(client, symbol)
    q = float(dec_down(qty, f["step"]))
    return q if q >= f["min_qty"] else 0.0


def round_price(client: BinanceClient, symbol: str, price: float) -> float:
    f = get_filter_values(client, symbol)
    return float(dec_down(price, f["tick"]))


def signed_post_order(client: BinanceClient, params: dict) -> dict:
    ts = int(time.time() * 1000)
    params = {**params, "timestamp": ts}
    query = urlencode(params)
    sig = client._sign(query)
    body = f"{query}&signature={sig}"
    return client._post(f"{client.base}/api/v3/order", body)


def place_market_quote(client: BinanceClient, symbol: str, side: str, quote_qty: float, client_id: str) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quoteOrderQty": f"{quote_qty:.2f}",
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }
    return signed_post_order(client, params)

def place_market_qty(client: BinanceClient, symbol: str, side: str, qty: float, client_id: str) -> dict:
    q = round_qty(client, symbol, qty)
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": str(q),
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }
    return signed_post_order(client, params)


def place_limit(client: BinanceClient, symbol: str, side: str, qty: float, price: float, client_id: str) -> dict:
    q = round_qty(client, symbol, qty)
    p = round_price(client, symbol, price)
    f = get_filter_values(client, symbol)
    if q <= 0 or q * p < f["min_notional"]:
        raise ValueError(f"order too small qty={q} price={p} notional={q*p}")
    params = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": dec_down(q, f["step"]),
        "price": dec_down(p, f["tick"]),
        "newClientOrderId": client_id,
    }
    order = signed_post_order(client, params)
    order["clientOrderIdLocal"] = client_id
    return order


def balance_map(client: BinanceClient, include_locked: bool = True) -> Dict[str, float]:
    if include_locked:
        return {b["asset"]: float(b["free"]) + float(b.get("locked", 0) or 0) for b in client.get_balance()}
    return {b["asset"]: float(b["free"]) for b in client.get_balance()}


def fetch_price(client: BinanceClient, symbol: str = SYMBOL) -> float:
    return float(client.get_ticker(symbol)["lastPrice"])


def fetch_atr(client: BinanceClient) -> float:
    rows = client.get_klines(SYMBOL, "5m", 120)
    candles = [{"high": r["high"], "low": r["low"], "close": r["close"]} for r in rows]
    return compute_atr(candles, 14)


def order_prefix(bot_name: str) -> str:
    return BOT_CONFIGS[bot_name]["prefix"]


def new_client_id(bot_name: str, side: str) -> str:
    return f"{order_prefix(bot_name)}_{side.lower()}_{uuid.uuid4().hex[:10]}"


def init_state(client: BinanceClient, bot_name: str) -> dict:
    cfg = BOT_CONFIGS[bot_name]
    price = fetch_price(client)
    balances = balance_map(client)
    base_qty = 0.0
    quote_qty = 0.0
    init_order = None

    if cfg["init_mode"] == "eur_to_usdc":
        cid = new_client_id(bot_name, "eurusdc")
        # EURUSDC is base=EUR, quote=USDC. To convert 100 EUR into USDC we SELL EUR.
        init_order = place_market_qty(client, EUR_PAIR, "SELL", float(cfg["eur_amount"]), cid)
        eur_sold = float(init_order.get("executedQty") or cfg["eur_amount"])
        usdc_gross = float(init_order.get("cummulativeQuoteQty") or 0)
        commission_usdc = 0.0
        for fill in init_order.get("fills", []) or []:
            if fill.get("commissionAsset") == QUOTE:
                commission_usdc += float(fill.get("commission") or 0)
        if commission_usdc <= 0:
            commission_usdc = usdc_gross * FEE_RATE
        quote_qty = max(0.0, usdc_gross - commission_usdc)
        append_event(bot_name, {"type": "init_eur_to_usdc", "eur_sold": eur_sold, "usdc_acquired": quote_qty, "order": init_order})
    elif cfg["init_mode"] == "configured_portfolio":
        # Keep these variants isolated from Binance Testnet faucet balances. They model the
        # chosen real portfolio scope, while the testnet account only provides execution rails.
        base_qty = float(cfg.get("initial_xrp", 0.0))
        quote_qty = float(cfg.get("initial_usdc", 0.0))
        append_event(
            bot_name,
            {
                "type": "init_configured_portfolio",
                "scope": cfg.get("portfolio_scope"),
                "xrp": base_qty,
                "usdc": quote_qty,
                "basis_price": price,
            },
        )
    else:
        raise ValueError(f"unsupported init_mode={cfg['init_mode']}")

    state = {
        "bot_name": bot_name,
        "label": cfg["label"],
        "symbol": SYMBOL,
        "base": BASE,
        "quote": QUOTE,
        "status": "running",
        "created_at": utc_now(),
        "start_price": price,
        "grid_center": price,
        "last_rebalance_price": price,
        "xrp_qty": base_qty,
        "quote_qty": quote_qty,
        "xrp_qty_start": base_qty,
        "quote_qty_start": quote_qty,
        "realized_pnl": 0.0,
        "fees_quote_est": 0.0,
        "wins": 0,
        "losses": 0,
        "filled_trades": 0,
        "open_orders": [],
        "lots": ([{"qty": base_qty, "price": price, "source": "initial_portfolio", "ts": utc_now()}] if base_qty > 0 else []),
        "config": cfg,
        "init_order": init_order,
    }
    save_state(bot_name, state)
    update_db_status(bot_name, state, price)
    return state


def reserved_notional(state: dict, side: str) -> float:
    total = 0.0
    for o in state.get("open_orders", []):
        if o.get("side") == side and o.get("status") in ("NEW", "PARTIALLY_FILLED"):
            total += float(o.get("origQty", o.get("qty", 0)) or 0) * float(o.get("price", 0) or 0)
    return total


def active_orders(state: dict, side: Optional[str] = None) -> list:
    out = []
    for o in state.get("open_orders", []):
        if o.get("status") in ("NEW", "PARTIALLY_FILLED") and (side is None or o.get("side") == side):
            out.append(o)
    return out


def reconcile_orders(client: BinanceClient, bot_name: str, state: dict) -> None:
    changed = False
    current = []
    for o in state.get("open_orders", []):
        oid = o.get("orderId")
        if not oid:
            continue
        try:
            st = client.get_order_status(SYMBOL, int(oid))
        except Exception as e:
            append_event(bot_name, {"type": "order_status_error", "order": o, "error": str(e)[:180]})
            current.append(o)
            continue
        status = st.get("status")
        merged = {**o, **st, "status": status}
        if status == "FILLED" and not o.get("applied_fill"):
            apply_fill(client, bot_name, state, merged)
            merged["applied_fill"] = True
            changed = True
        elif status in ("CANCELED", "EXPIRED", "REJECTED", "FILLED"):
            changed = True
        else:
            current.append(merged)
    state["open_orders"] = current
    if changed:
        save_state(bot_name, state)


def apply_fill(client: BinanceClient, bot_name: str, state: dict, order: dict) -> None:
    side = order.get("side")
    qty = float(order.get("executedQty") or order.get("origQty") or 0)
    quote = float(order.get("cummulativeQuoteQty") or (qty * float(order.get("price") or 0)))
    avg_price = quote / qty if qty > 0 else float(order.get("price") or 0)
    fee = quote * FEE_RATE
    pnl = 0.0
    result = ""

    if side == "BUY":
        net_qty = max(0.0, qty - (qty * FEE_RATE))
        state["quote_qty"] = max(0.0, float(state.get("quote_qty", 0)) - quote)
        state["xrp_qty"] = float(state.get("xrp_qty", 0)) + net_qty
        state.setdefault("lots", []).append({"qty": net_qty, "price": avg_price, "source": "grid_buy", "ts": utc_now()})
        state["fees_quote_est"] = float(state.get("fees_quote_est", 0)) + fee
        result = ""
    elif side == "SELL":
        remaining = qty
        cost = 0.0
        lots = []
        for lot in state.get("lots", []):
            lq = float(lot.get("qty") or 0)
            if remaining <= 0:
                lots.append(lot)
                continue
            take = min(lq, remaining)
            cost += take * float(lot.get("price") or avg_price)
            lq -= take
            remaining -= take
            if lq > 1e-10:
                lot["qty"] = lq
                lots.append(lot)
        state["lots"] = lots
        net_quote = max(0.0, quote - fee)
        pnl = net_quote - cost
        state["xrp_qty"] = max(0.0, float(state.get("xrp_qty", 0)) - qty)
        state["quote_qty"] = float(state.get("quote_qty", 0)) + net_quote
        state["realized_pnl"] = float(state.get("realized_pnl", 0)) + pnl
        state["fees_quote_est"] = float(state.get("fees_quote_est", 0)) + fee
        if pnl > 0:
            state["wins"] = int(state.get("wins", 0)) + 1
            result = "WIN"
        elif pnl < 0:
            state["losses"] = int(state.get("losses", 0)) + 1
            result = "LOSS"
        else:
            result = "FLAT"

    state["filled_trades"] = int(state.get("filled_trades", 0)) + 1
    order["applied_fill"] = True
    append_event(bot_name, {"type": "filled", "side": side, "qty": qty, "avg_price": avg_price, "quote": quote, "pnl": pnl, "order": order})
    insert_trade(bot_name, side, qty, quote, pnl, result, {"avg_price": avg_price, "order": order})


def build_levels(state: dict, price: float, atr: float) -> Tuple[List[float], List[float]]:
    cfg = state["config"]
    center = float(state.get("grid_center") or price)
    if abs(price - center) > float(cfg["rebalance_threshold_atr"]) * atr:
        state["grid_center"] = price
        state["last_rebalance_price"] = price
        center = price
    spacing = max(atr * float(cfg["grid_spacing_atr"]), price * 0.0015)
    n = int(cfg["grid_levels_each_side"])
    buys = [center - spacing * i for i in range(n, 0, -1)]
    sells = [center + spacing * i for i in range(1, n + 1)]
    return buys, sells


def desired_order_exists(state: dict, side: str, price: float, tolerance: float) -> bool:
    for o in active_orders(state, side):
        op = float(o.get("price") or 0)
        if abs(op - price) <= tolerance:
            return True
    return False


def place_grid_orders(client: BinanceClient, bot_name: str, state: dict, price: float, atr: float) -> None:
    cfg = state["config"]
    filters = get_filter_values(client, SYMBOL)
    buys, sells = build_levels(state, price, atr)
    tick = filters["tick"]
    equity = float(state.get("quote_qty", 0)) + float(state.get("xrp_qty", 0)) * price
    xrp_pct = (float(state.get("xrp_qty", 0)) * price / equity) if equity > 0 else 0
    max_xrp_hold_pct = float(cfg["max_xrp_hold_pct"])
    max_side = int(cfg.get("max_open_orders_side", 3))

    # BUY grid: reserve quote virtually to avoid over-placing.
    pending_buy_notional = reserved_notional(state, "BUY")
    available_quote = max(0.0, float(state.get("quote_qty", 0)) - pending_buy_notional)
    for bp in buys:
        bp = round_price(client, SYMBOL, bp)
        if len(active_orders(state, "BUY")) >= max_side:
            break
        if xrp_pct >= max_xrp_hold_pct:
            break
        if desired_order_exists(state, "BUY", bp, tick * 2):
            continue
        notional = min(available_quote, equity * float(cfg["risk_per_trade_pct"]))
        if notional < filters["min_notional"]:
            continue
        qty = round_qty(client, SYMBOL, notional / bp)
        if qty <= 0:
            continue
        cid = new_client_id(bot_name, "buy")
        try:
            order = place_limit(client, SYMBOL, "BUY", qty, bp, cid)
            state.setdefault("open_orders", []).append(order)
            available_quote -= qty * bp
            append_event(bot_name, {"type": "place_buy", "price": bp, "qty": qty, "order": order})
        except Exception as e:
            append_event(bot_name, {"type": "place_buy_error", "price": bp, "qty": qty, "error": str(e)[:240]})

    # SELL grid: split available XRP across levels. Initial holder XRP is allowed to sell.
    pending_sell_qty = sum(float(o.get("origQty", 0) or 0) for o in active_orders(state, "SELL"))
    available_xrp = max(0.0, float(state.get("xrp_qty", 0)) - pending_sell_qty)
    # Holder-friendly: do not dump the full XRP bag across the grid. Sell gradually.
    per_level_qty = available_xrp * float(cfg.get("risk_per_trade_pct", 0.10)) if available_xrp > 0 else 0
    for sp in sells:
        sp = round_price(client, SYMBOL, sp)
        if len(active_orders(state, "SELL")) >= max_side:
            break
        if desired_order_exists(state, "SELL", sp, tick * 2):
            continue
        qty = round_qty(client, SYMBOL, min(per_level_qty, available_xrp))
        if qty <= 0 or qty * sp < filters["min_notional"]:
            continue
        cid = new_client_id(bot_name, "sell")
        try:
            order = place_limit(client, SYMBOL, "SELL", qty, sp, cid)
            state.setdefault("open_orders", []).append(order)
            available_xrp -= qty
            append_event(bot_name, {"type": "place_sell", "price": sp, "qty": qty, "order": order})
        except Exception as e:
            append_event(bot_name, {"type": "place_sell_error", "price": sp, "qty": qty, "error": str(e)[:240]})


def insert_trade(bot_name: str, side: str, qty: float, usd_amount: float, pnl: float, result: str, raw: dict) -> None:
    try:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute(
                """insert into trades(bot_name,ts,side,token_qty,usd_amount,pnl_usd,result,raw)
                   values(%s,now(),%s,%s,%s,%s,%s,%s::jsonb) on conflict do nothing""",
                [bot_name, side, qty, usd_amount, pnl, result, json.dumps(raw)],
            )
    except Exception as e:
        append_event(bot_name, {"type": "db_trade_error", "error": str(e)[:240]})


def update_db_status(bot_name: str, state: dict, price: float) -> None:
    tokens_value = float(state.get("xrp_qty", 0)) * price
    balance = float(state.get("quote_qty", 0))
    realized = float(state.get("realized_pnl", 0))
    try:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            # status
            conn.execute(
                """insert into bot_status(bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at)
                   values(%s,true,'testnet',%s,%s,%s,%s,now())
                   on conflict(bot_name) do update set is_running=true, mode='testnet', balance_usd=excluded.balance_usd,
                     pnl_day_usd=excluded.pnl_day_usd, pnl_week_usd=excluded.pnl_week_usd,
                     tokens_value_usd=excluded.tokens_value_usd, updated_at=now()""",
                [bot_name, balance, realized, realized, tokens_value],
            )
    except Exception as e:
        append_event(bot_name, {"type": "db_status_error", "error": str(e)[:240]})


def cycle_bot(client: BinanceClient, bot_name: str, init: bool = True) -> dict:
    state = load_state(bot_name)
    if not state:
        if not init:
            raise RuntimeError(f"state missing for {bot_name}; run with --init")
        state = init_state(client, bot_name)
    state["label"] = BOT_CONFIGS[bot_name]["label"]
    state["config"] = BOT_CONFIGS[bot_name]
    price = fetch_price(client)
    atr = fetch_atr(client)
    reconcile_orders(client, bot_name, state)
    place_grid_orders(client, bot_name, state, price, atr)
    equity = float(state.get("quote_qty", 0)) + float(state.get("xrp_qty", 0)) * price
    state["last_price"] = price
    state["last_atr"] = atr
    state["equity_usdc"] = equity
    state["unrealized_pnl"] = equity - (float(state.get("quote_qty_start", state.get("quote_qty", 0))) + float(state.get("xrp_qty_start", 0)) * float(state.get("start_price", price)))
    save_state(bot_name, state)
    update_db_status(bot_name, state, price)
    return state


def summary() -> dict:
    items = []
    for bot_name in BOT_CONFIGS:
        st = load_state(bot_name) or {}
        items.append(st)
    return {"ok": True, "items": items, "updated_at": utc_now()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", choices=[*BOT_CONFIGS.keys(), "all"], default="all")
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()

    if args.summary:
        print(json.dumps(summary(), indent=2))
        return

    client = load_client(env_path=ROOT / ".env.local", testnet=True)
    bots = list(BOT_CONFIGS) if args.bot == "all" else [args.bot]
    out = []
    for b in bots:
        out.append(cycle_bot(client, b, init=not args.no_init))
    print(json.dumps({"ok": True, "items": out}, indent=2, default=str))


if __name__ == "__main__":
    main()
