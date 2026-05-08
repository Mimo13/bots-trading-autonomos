#!/usr/bin/env python3
"""
Shared portfolio state for all bots.
Tracks cash balance, wallet, and handles bankruptcy reset.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

SHARED_STATE_PATH = Path('/Users/mimo13/bots-trading-autonomos-runtime/runtime/shared_state.json')
INITIAL_BALANCE = 100.0
RESET_BALANCE = 100.0


def default_state() -> dict:
    return {
        "cash_balance": INITIAL_BALANCE,
        "initial_balance": INITIAL_BALANCE,
        "total_pnl": 0.0,
        "run_count": 0,
        "reset_count": 0,
        "last_reset_reason": "",
        "wallet": {},  # token -> qty held
        "updated_at": datetime.now(timezone.utc).isoformat() + "Z",
    }


def load_state() -> dict:
    if SHARED_STATE_PATH.exists():
        try:
            return json.loads(SHARED_STATE_PATH.read_text())
        except (json.JSONDecodeError, Exception):
            pass
    state = default_state()
    save_state(state)
    return state


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat() + "Z"
    SHARED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_cash_balance() -> float:
    state = load_state()
    return float(state.get("cash_balance", INITIAL_BALANCE))


def update_balance(delta: float, reason: str = "") -> float:
    """
    Add delta to cash balance. Negative = purchase/spend, Positive = sale/income.
    If balance drops to 0 or below, auto-reset to RESET_BALANCE.
    Returns new balance.
    """
    state = load_state()
    state["cash_balance"] = float(state.get("cash_balance", INITIAL_BALANCE)) + delta
    state["total_pnl"] = float(state.get("total_pnl", 0.0)) + delta
    state["run_count"] = int(state.get("run_count", 0)) + 1

    # Check for bankruptcy
    if state["cash_balance"] <= 0:
        state["reset_count"] = int(state.get("reset_count", 0)) + 1
        state["last_reset_reason"] = f"Bankruptcy at run {state['run_count']}: balance=${state['cash_balance']:.2f}. Reason: {reason}"
        state["cash_balance"] = RESET_BALANCE
        state["wallet"] = {}  # Clear wallet on reset
        print(f"🔄 BANKRUPTCY RESET #{state['reset_count']}: {state['last_reset_reason']}")

    save_state(state)
    return float(state["cash_balance"])


def update_wallet(token: str, delta_qty: float, price: float) -> Dict:
    """
    Update wallet holdings. Positive delta = bought, Negative delta = sold.
    Returns updated wallet dict.
    """
    state = load_state()
    wallet = dict(state.get("wallet", {}))
    current = float(wallet.get(token, 0.0))
    new_qty = current + delta_qty
    if new_qty <= 0:
        wallet.pop(token, None)
    else:
        wallet[token] = round(new_qty, 8)
    state["wallet"] = wallet
    save_state(state)
    return wallet


def record_trade(side: str, token: str, qty: float, price: float, pnl: float = 0.0) -> None:
    """Record a trade and update balance/wallet."""
    if side.upper() == "BUY":
        cost = qty * price
        update_balance(-cost, f"BUY {qty:.4f} {token} @ ${price:.4f}")
        update_wallet(token, qty, price)
    elif side.upper() == "SELL":
        proceeds = qty * price
        update_balance(proceeds, f"SELL {qty:.4f} {token} @ ${price:.4f}")
        update_wallet(token, -qty, price)


def summary() -> str:
    state = load_state()
    pnl = float(state["total_pnl"])
    balance = float(state["cash_balance"])
    wallet = state.get("wallet", {})
    wallet_str = ", ".join(f"{k}: {v:.4f}" for k, v in wallet.items()) if wallet else "empty"
    return (f"💰 Balance: ${balance:.2f} | PnL: ${pnl:+.2f} | "
            f"Wallet: {wallet_str} | Runs: {state['run_count']} | Resets: {state['reset_count']}")


if __name__ == "__main__":
    print(f"Estado actual: {summary()}")
    print(f"\nComandos de prueba:")
    print(f"  python3 -c 'from shared_state import *; update_balance(-10, \"test buy\"); print(summary())'")
    print(f"  python3 -c 'from shared_state import *; update_balance(15, \"test sell\"); print(summary())'")
