#!/usr/bin/env python3
"""FabianInventoryBot — paper simulation using owned SOL inventory, no borrow.

It runs Fabian signals, then translates:
- BUY/SELL into normal spot long trades using USDC.
- SHORT/COVER into inventory rotation: sell owned SOL, then buy it back later.

This is NOT a real short: it never sells more SOL than the paper wallet owns.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional


@dataclass
class Wallet:
    usdc: float
    sol_qty: float
    initial_usdc: float
    initial_sol_qty: float
    initial_price: float

    def equity(self, price: float) -> float:
        return self.usdc + self.sol_qty * price


def load_prices(input_path: Path) -> tuple[float, float]:
    first = last = None
    with input_path.open() as f:
        for row in csv.DictReader(f):
            px = float(row["close"])
            if first is None:
                first = px
            last = px
    if first is None or last is None:
        raise ValueError("input has no candles")
    return first, last


def run_fabian(input_path: Path, config_path: Path, out_dir: Path) -> Path:
    signals_dir = out_dir / "fabian_signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(Path(__file__).resolve().parent / ".venv/bin/python"),
        str(Path(__file__).resolve().parent / "fabian_pullback_bot.py"),
        "--input", str(input_path),
        "--config", str(config_path),
        "--output-dir", str(signals_dir),
    ]
    # Fallback when running from dev without runtime venv path.
    if not Path(cmd[0]).exists():
        cmd[0] = "python3"
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if cp.returncode != 0:
        raise RuntimeError(f"fabian signal run failed: {cp.stderr[-1000:]}")
    return signals_dir / "trades_log.csv"


def simulate_inventory(
    signals_csv: Path,
    input_path: Path,
    out_dir: Path,
    initial_usdc: float,
    initial_sol_usd: float,
    fee_bps: float,
) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    first_price, last_price = load_prices(input_path)
    wallet = Wallet(
        usdc=initial_usdc,
        sol_qty=initial_sol_usd / first_price if first_price > 0 else 0.0,
        initial_usdc=initial_usdc,
        initial_sol_qty=initial_sol_usd / first_price if first_price > 0 else 0.0,
        initial_price=first_price,
    )
    initial_equity = wallet.equity(first_price)
    fee_rate = fee_bps / 10000.0

    open_longs: List[Dict] = []
    open_inventory_sales: List[Dict] = []
    trades: List[Dict] = []
    skipped: List[Dict] = []
    wins = losses = 0
    realized_pnl = 0.0
    peak = initial_equity
    max_dd_pct = 0.0

    with signals_csv.open() as f:
        for row in csv.DictReader(f):
            action = (row.get("action") or "").upper()
            ts = row.get("ts")
            entry = float(row.get("entry") or 0)
            exit_px = float(row.get("exit") or 0)
            qty_signal = abs(float(row.get("qty") or 0))
            if action == "BUY" and entry > 0:
                # Spot buy using USDC. Scale down if the signal is bigger than available cash.
                max_qty = wallet.usdc / (entry * (1 + fee_rate)) if entry > 0 else 0
                qty = min(qty_signal, max_qty)
                if qty <= 0:
                    skipped.append({"ts": ts, "action": action, "reason": "NO_USDC", "price": entry})
                    continue
                cost = qty * entry
                fee = cost * fee_rate
                wallet.usdc -= cost + fee
                wallet.sol_qty += qty
                open_longs.append({"qty": qty, "entry": entry, "fee_entry": fee, "ts": ts})
                trades.append({"ts": ts, "action": "BUY", "price": entry, "qty": qty, "usd_amount": cost, "pnl": 0.0, "reason": "LONG_ENTRY"})

            elif action == "SELL" and exit_px > 0:
                if not open_longs:
                    skipped.append({"ts": ts, "action": action, "reason": "NO_OPEN_LONG", "price": exit_px})
                    continue
                pos = open_longs.pop(0)
                qty = min(pos["qty"], wallet.sol_qty)
                proceeds = qty * exit_px
                fee = proceeds * fee_rate
                wallet.sol_qty -= qty
                wallet.usdc += proceeds - fee
                pnl = proceeds - fee - qty * pos["entry"] - pos["fee_entry"]
                realized_pnl += pnl
                wins += int(pnl > 0)
                losses += int(pnl < 0)
                trades.append({"ts": ts, "action": "SELL", "price": exit_px, "qty": qty, "usd_amount": proceeds, "pnl": pnl, "reason": row.get("reason") or "LONG_EXIT"})

            elif action == "SHORT" and entry > 0:
                # Inventory bearish trade: sell owned SOL only. No borrow, no margin.
                qty = min(qty_signal, wallet.sol_qty)
                if qty <= 0:
                    skipped.append({"ts": ts, "action": action, "reason": "NO_SOL_INVENTORY", "price": entry})
                    continue
                proceeds = qty * entry
                fee = proceeds * fee_rate
                wallet.sol_qty -= qty
                wallet.usdc += proceeds - fee
                open_inventory_sales.append({"qty": qty, "entry": entry, "fee_entry": fee, "ts": ts})
                trades.append({"ts": ts, "action": "SELL_INVENTORY", "price": entry, "qty": qty, "usd_amount": proceeds, "pnl": 0.0, "reason": "BEARISH_INVENTORY_SELL"})

            elif action == "COVER" and exit_px > 0:
                if not open_inventory_sales:
                    skipped.append({"ts": ts, "action": action, "reason": "NO_INVENTORY_SALE", "price": exit_px})
                    continue
                pos = open_inventory_sales.pop(0)
                qty = pos["qty"]
                cost = qty * exit_px
                fee = cost * fee_rate
                if wallet.usdc < cost + fee:
                    skipped.append({"ts": ts, "action": action, "reason": "NO_USDC_TO_REBUY", "price": exit_px})
                    open_inventory_sales.insert(0, pos)
                    continue
                wallet.usdc -= cost + fee
                wallet.sol_qty += qty
                pnl = qty * pos["entry"] - pos["fee_entry"] - cost - fee
                realized_pnl += pnl
                wins += int(pnl > 0)
                losses += int(pnl < 0)
                trades.append({"ts": ts, "action": "BUY_BACK_INVENTORY", "price": exit_px, "qty": qty, "usd_amount": cost, "pnl": pnl, "reason": row.get("reason") or "INVENTORY_REBUY"})

            eq = wallet.equity(exit_px or entry or last_price)
            peak = max(peak, eq)
            if peak > 0:
                max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)

    final_equity = wallet.equity(last_price)
    summary = {
        "strategy": "FabianInventory_SOL_USDC_no_borrow",
        "initial_equity": round(initial_equity, 4),
        "final_equity": round(final_equity, 4),
        "total_pnl": round(final_equity - initial_equity, 4),
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(final_equity - initial_equity - realized_pnl, 4),
        "wins": wins,
        "losses": losses,
        "closed_trades": wins + losses,
        "win_rate_percent": round(wins / max(1, wins + losses) * 100, 2),
        "max_drawdown_percent": round(max_dd_pct, 2),
        "initial_usdc": round(initial_usdc, 4),
        "initial_sol_qty": round(wallet.initial_sol_qty, 8),
        "initial_sol_usd": round(initial_sol_usd, 4),
        "final_usdc": round(wallet.usdc, 4),
        "final_sol_qty": round(wallet.sol_qty, 8),
        "first_price": first_price,
        "last_price": last_price,
        "fee_bps": fee_bps,
        "open_longs": len(open_longs),
        "open_inventory_sales": len(open_inventory_sales),
        "skipped": len(skipped),
        "outputs": {
            "trades_log": str(out_dir / "trades_log.csv"),
            "skipped_log": str(out_dir / "skipped_log.csv"),
            "summary_json": str(out_dir / "summary.json"),
        },
    }

    with (out_dir / "trades_log.csv").open("w", newline="") as f:
        fields = ["ts", "action", "price", "qty", "usd_amount", "pnl", "reason"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    with (out_dir / "skipped_log.csv").open("w", newline="") as f:
        fields = ["ts", "action", "reason", "price"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(skipped)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser(description="Paper inventory bot: Fabian signals using owned SOL/USDC only")
    p.add_argument("--input", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--initial-usdc", type=float, default=50.0)
    p.add_argument("--initial-sol-usd", type=float, default=50.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    signals = run_fabian(Path(args.input), Path(args.config), out_dir)
    summary = simulate_inventory(signals, Path(args.input), out_dir, args.initial_usdc, args.initial_sol_usd, args.fee_bps)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
