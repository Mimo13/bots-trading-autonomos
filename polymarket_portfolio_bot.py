#!/usr/bin/env python3
"""
PolyPortfolioPaper — Bot de cartera para Polymarket Paper Trading.

Modelo realista con compra/venta de tokens, inventario, y P&L.
- Compra tokens cuando modelo ve oportunidad (edge > mínimo)
- Acumula tokens en cartera
- Vende cuando hay señal de reversión, take profit, o stop loss
- P&L realizado al vender, no realizado marcado a mercado
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Candle:
    ts: datetime
    instrument: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    p_model_up: float
    p_market_up: float
    tv_recommendation: str = ""
    tv_confidence: float = 0.0


@dataclass
class PortfolioConfig:
    initial_balance: float = 100.0
    buy_threshold: float = 0.65        # p_model_up above this = BUY signal
    sell_threshold: float = 0.35       # p_model_up below this = SELL signal
    edge_min: float = 0.05             # min |p_model_up - p_market_up|
    risk_per_trade: float = 0.10       # % of balance to spend per buy
    max_position_pct: float = 0.30     # max % of balance in a single position
    take_profit_pct: float = 0.04      # sell if price rises X% above avg entry
    stop_loss_pct: float = 0.025       # sell if price drops X% below avg entry
    sell_on_reversal: bool = True      # sell all when signal reverses
    sell_fraction_on_signal: float = 1.0  # fraction to sell on reversal (1.0 = all)
    max_hold_candles: int = 15         # max candles to hold before forced sell
    max_trades_per_day: int = 6
    max_daily_loss_pct: float = 10.0


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_entry: float = 0.0
    cost_basis: float = 0.0  # total USD spent


@dataclass
class TradeRecord:
    entry_ts: datetime
    exit_ts: Optional[datetime]
    symbol: str
    side: str  # BUY or SELL
    qty: float
    price: float
    usd_amount: float
    realized_pnl: float
    reason: str


def parse_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_candles(path: Path) -> List[Candle]:
    rows: List[Candle] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        required = {"timestamp_utc", "instrument", "timeframe", "open", "high", "low", "close", "p_model_up", "p_market_up"}
        has_tv = ("tv_recommendation" in (r.fieldnames or [])) and ("tv_confidence" in (r.fieldnames or []))
        missing = required - set(r.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns: {sorted(missing)}")
        for row in r:
            rows.append(Candle(
                ts=parse_ts(row["timestamp_utc"]),
                instrument=row["instrument"].strip(),
                timeframe=row["timeframe"].strip(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                p_model_up=float(row["p_model_up"]),
                p_market_up=float(row["p_market_up"]),
                tv_recommendation=(row.get("tv_recommendation", "") if has_tv else "").strip().upper(),
                tv_confidence=float(row.get("tv_confidence", 0.0) or 0.0) if has_tv else 0.0,
            ))
    rows.sort(key=lambda x: x.ts)
    return rows


def load_config(path: Optional[Path]) -> PortfolioConfig:
    if path is None:
        return PortfolioConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    c = PortfolioConfig()
    for k, v in data.items():
        if not hasattr(c, k):
            raise ValueError(f"Unknown config key: {k}")
        setattr(c, k, v)
    return c


def compute_sma(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def run_portfolio_sim(candles: List[Candle], cfg: PortfolioConfig, out_dir: Path) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    decisions_path = out_dir / "decisions_log.csv"
    trades_path = out_dir / "trades_log.csv"
    portfolio_path = out_dir / "portfolio_snapshots.csv"
    summary_path = out_dir / "summary.json"

    dec_fields = ["timestamp_utc", "instrument", "action", "reason_code",
                   "p_model_up", "p_market_up", "edge", "balance", "position_qty",
                   "position_value", "total_equity", "price"]
    tr_fields = ["ts", "symbol", "side", "qty", "price", "usd_amount", "realized_pnl", "reason"]
    pf_fields = ["ts", "balance", "position_qty", "avg_entry", "position_value",
                  "unrealized_pnl", "realized_pnl", "total_equity", "price"]

    balance = cfg.initial_balance
    position = Position(symbol=candles[0].instrument if candles else "")
    realized_pnl = 0.0
    peak_equity = cfg.initial_balance
    max_dd = 0.0
    day_trades: Dict[str, int] = {}
    daily_realized_pnl: Dict[str, float] = {}
    day_start_balance: Dict[str, float] = {}
    reason_counts: Counter = Counter()

    trades_log: List[TradeRecord] = []

    def equity():
        pos_val = position.qty * last_price if last_price else 0.0
        return balance + pos_val

    def position_value():
        return position.qty * last_price if last_price else 0.0

    def unrealized():
        if position.qty <= 0 or position.avg_entry <= 0:
            return 0.0
        return position.qty * (last_price - position.avg_entry)

    def day_key(ts: datetime) -> str:
        return ts.date().isoformat()

    def check_daily_loss(dk: str) -> bool:
        if dk not in day_start_balance:
            day_start_balance[dk] = balance + position_value()
        start_eq = day_start_balance[dk]
        if start_eq <= 0:
            return True
        current_eq = equity()
        loss_pct = max(0, (start_eq - current_eq) / start_eq) * 100
        return loss_pct >= cfg.max_daily_loss_pct

    last_price = candles[0].close if candles else 0.0
    candles_held = 0

    with (decisions_path.open("w", newline="", encoding="utf-8") as dfile,
          trades_path.open("w", newline="", encoding="utf-8") as tfile,
          portfolio_path.open("w", newline="", encoding="utf-8") as pfile):

        dwr = csv.DictWriter(dfile, fieldnames=dec_fields)
        twr = csv.DictWriter(tfile, fieldnames=tr_fields)
        pwr = csv.DictWriter(pfile, fieldnames=pf_fields)
        dwr.writeheader()
        twr.writeheader()
        pwr.writeheader()

        for i, c in enumerate(candles):
            last_price = c.close
            dk = day_key(c.ts)
            day_trades.setdefault(dk, 0)
            daily_realized_pnl.setdefault(dk, 0.0)

            action = "HOLD"
            reason = ""
            qty_traded = 0.0
            usd_amount = 0.0
            trade_pnl = 0.0
            trade_side = ""
            trade_reason = ""

            p_up = c.p_model_up
            p_mkt = c.p_market_up
            edge = abs(p_up - p_mkt)

            # Skip first few candles to build history
            if i < 10:
                reason = "WARMUP"
                dwr.writerow({"timestamp_utc": c.ts.isoformat(), "instrument": c.instrument,
                              "action": action, "reason_code": reason,
                              "p_model_up": round(p_up, 6), "p_market_up": round(p_mkt, 6),
                              "edge": round(edge, 6), "balance": round(balance, 2),
                              "position_qty": round(position.qty, 6),
                              "position_value": round(position_value(), 2),
                              "total_equity": round(equity(), 2), "price": round(c.close, 2)})
                pwr.writerow({"ts": c.ts.isoformat(), "balance": round(balance, 2),
                              "position_qty": round(position.qty, 6),
                              "avg_entry": round(position.avg_entry, 6) if position.avg_entry else 0,
                              "position_value": round(position_value(), 2),
                              "unrealized_pnl": round(unrealized(), 4),
                              "realized_pnl": round(realized_pnl, 4),
                              "total_equity": round(equity(), 2), "price": round(c.close, 2)})
                continue

            # Hard gates
            if day_trades[dk] >= cfg.max_trades_per_day:
                reason = "MAX_TRADES_DAY"
            elif check_daily_loss(dk):
                reason = "DAILY_LOSS_LIMIT"

            if not reason and edge < cfg.edge_min:
                reason = "EDGE_TOO_LOW"

            # Decision logic
            if not reason:
                if p_up >= cfg.buy_threshold and p_up > p_mkt:
                    # BUY signal
                    action = "BUY"
                    # How much to spend
                    spend = balance * cfg.risk_per_trade
                    max_allowed = balance * cfg.max_position_pct
                    spend = min(spend, max_allowed)
                    if spend > 0.1 and c.close > 0:
                        qty = spend / c.close
                        # Add to position (dollar-cost averaging)
                        old_cost = position.cost_basis
                        old_qty = position.qty
                        position.cost_basis = old_cost + spend
                        position.qty = old_qty + qty
                        position.avg_entry = position.cost_basis / position.qty if position.qty > 0 else 0
                        balance -= spend
                        qty_traded = qty
                        usd_amount = spend
                        trade_side = "BUY"
                        trade_reason = "SIGNAL_BUY"
                        day_trades[dk] += 1
                        trades_log.append(TradeRecord(
                            entry_ts=c.ts, exit_ts=None, symbol=c.instrument,
                            side="BUY", qty=qty, price=c.close, usd_amount=spend,
                            realized_pnl=0.0, reason="SIGNAL_BUY"
                        ))

                elif p_up <= cfg.sell_threshold and p_up < p_mkt and position.qty > 0:
                    # SELL signal — sell holdings
                    action = "SELL"
                    sell_qty = position.qty * cfg.sell_fraction_on_signal
                    sell_qty = min(sell_qty, position.qty)
                    if sell_qty > 0 and c.close > 0:
                        proceeds = sell_qty * c.close
                        # PnL = proceeds - cost of shares sold
                        cost_of_sold = (sell_qty / position.qty) * position.cost_basis if position.qty > 0 else 0
                        trade_pnl = proceeds - cost_of_sold
                        realized_pnl += trade_pnl
                        daily_realized_pnl[dk] += trade_pnl
                        balance += proceeds
                        position.qty -= sell_qty
                        position.cost_basis -= cost_of_sold
                        if position.qty <= 0.000001:
                            position.qty = 0.0
                            position.cost_basis = 0.0
                            position.avg_entry = 0.0
                        else:
                            position.avg_entry = position.cost_basis / position.qty if position.qty > 0 else 0
                        qty_traded = sell_qty
                        usd_amount = proceeds
                        trade_side = "SELL"
                        trade_reason = "SIGNAL_SELL"
                        day_trades[dk] += 1

            # Track hold duration
            if position.qty > 0.000001:
                candles_held += 1
            else:
                candles_held = 0

            # Force sell if held too long
            if position.qty > 0.000001 and candles_held >= cfg.max_hold_candles and action != "SELL":
                action = "TIMEOUT_SELL"
                trade_pnl = (last_price - position.avg_entry) * position.qty
                proceeds = position.qty * last_price
                realized_pnl += trade_pnl
                daily_realized_pnl[dk] += trade_pnl
                balance += proceeds
                trade_side = "SELL"
                trade_reason = "MAX_HOLD"
                qty_traded = position.qty
                usd_amount = proceeds
                position.qty = 0.0
                position.cost_basis = 0.0
                position.avg_entry = 0.0
                candles_held = 0

            # Take profit / Stop loss checks (after signal-based actions)
            if position.qty > 0.000001 and position.avg_entry > 0 and last_price > 0:
                pnl_pct = (last_price - position.avg_entry) / position.avg_entry
                if pnl_pct >= cfg.take_profit_pct and action != "SELL":
                    # Take profit
                    action = "TP_SELL"
                    sell_qty = position.qty
                    proceeds = sell_qty * last_price
                    cost_of_sold = position.cost_basis
                    trade_pnl = proceeds - cost_of_sold
                    realized_pnl += trade_pnl
                    daily_realized_pnl[dk] += trade_pnl
                    balance += proceeds
                    trade_side = "SELL"
                    trade_reason = "TAKE_PROFIT"
                    qty_traded = sell_qty
                    usd_amount = proceeds
                    position.qty = 0.0
                    position.cost_basis = 0.0
                    position.avg_entry = 0.0
                    day_trades[dk] -= 1  # don't count TP as a trade for daily limit

                elif pnl_pct <= -cfg.stop_loss_pct and action != "SELL":
                    # Stop loss
                    action = "SL_SELL"
                    sell_qty = position.qty
                    proceeds = sell_qty * last_price
                    cost_of_sold = position.cost_basis
                    trade_pnl = proceeds - cost_of_sold
                    realized_pnl += trade_pnl
                    daily_realized_pnl[dk] += trade_pnl
                    balance += proceeds
                    trade_side = "SELL"
                    trade_reason = "STOP_LOSS"
                    qty_traded = sell_qty
                    usd_amount = proceeds
                    position.qty = 0.0
                    position.cost_basis = 0.0
                    position.avg_entry = 0.0

            if (trade_side == "SELL" or trade_side == "BUY") and qty_traded > 0:
                # Log the trade
                twr.writerow({"ts": c.ts.isoformat(), "symbol": c.instrument,
                              "side": trade_side, "qty": round(qty_traded, 8),
                              "price": round(last_price, 2),
                              "usd_amount": round(usd_amount, 2),
                              "realized_pnl": round(trade_pnl, 4),
                              "reason": trade_reason})
                # Also track in trades_log for counting
                trades_log.append(TradeRecord(
                    entry_ts=c.ts, exit_ts=c.ts if trade_side == "SELL" else None,
                    symbol=c.instrument, side=trade_side,
                    qty=qty_traded, price=last_price, usd_amount=usd_amount,
                    realized_pnl=trade_pnl, reason=trade_reason
                ))

            # Track peak equity and drawdown
            current_eq = equity()
            peak_equity = max(peak_equity, current_eq)
            dd = (peak_equity - current_eq) / peak_equity if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)

            # Write decision row
            reason_code = reason if reason else (action + ("_EXECUTED" if qty_traded > 0 else ""))
            if reason:
                reason_counts[reason] += 1
            else:
                reason_counts[action + ("_EXECUTED" if qty_traded > 0 else "_NO_ACTION")] += 1

            dwr.writerow({"timestamp_utc": c.ts.isoformat(), "instrument": c.instrument,
                          "action": action, "reason_code": reason_code,
                          "p_model_up": round(p_up, 6), "p_market_up": round(p_mkt, 6),
                          "edge": round(edge, 6), "balance": round(balance, 2),
                          "position_qty": round(position.qty, 6),
                          "position_value": round(position_value(), 2),
                          "total_equity": round(current_eq, 2), "price": round(last_price, 2)})

            # Portfolio snapshot every 5 candles
            if i % 5 == 0:
                pwr.writerow({"ts": c.ts.isoformat(), "balance": round(balance, 2),
                              "position_qty": round(position.qty, 6),
                              "avg_entry": round(position.avg_entry, 6) if position.avg_entry else 0,
                              "position_value": round(position_value(), 2),
                              "unrealized_pnl": round(unrealized(), 4),
                              "realized_pnl": round(realized_pnl, 4),
                              "total_equity": round(current_eq, 2), "price": round(last_price, 2)})

    # Final summary
    final_eq = equity()
    total_pnl = final_eq - cfg.initial_balance
    total_buys = sum(1 for t in trades_log if t.side == "BUY")
    total_sells = sum(1 for t in trades_log if t.side == "SELL")
    winning_sells = sum(1 for t in trades_log if t.side == "SELL" and t.realized_pnl > 0)
    losing_sells = sum(1 for t in trades_log if t.side == "SELL" and t.realized_pnl <= 0)
    sell_win_rate = winning_sells / (winning_sells + losing_sells) if (winning_sells + losing_sells) > 0 else 0

    summary = {
        "initial_balance": cfg.initial_balance,
        "final_balance": round(balance, 2),
        "final_position_qty": round(position.qty, 6),
        "position_value": round(position_value(), 2),
        "final_equity": round(final_eq, 2),
        "total_pnl": round(total_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized(), 2),
        "total_buys": total_buys,
        "total_sells": total_sells,
        "winning_sells": winning_sells,
        "losing_sells": losing_sells,
        "sell_win_rate_percent": round(sell_win_rate * 100, 2),
        "max_drawdown_percent": round(max_dd * 100, 2),
        "config": asdict(cfg),
        "outputs": {
            "decisions_log": str(decisions_path),
            "trades_log": str(trades_path),
            "portfolio_snapshots": str(portfolio_path),
            "summary_json": str(summary_path),
        },
    }

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="PolyPortfolioPaper — Portfolio-based Polymarket paper bot")
    p.add_argument("--input", required=True, help="CSV input path")
    p.add_argument("--config", default=None, help="Optional JSON config")
    p.add_argument("--output-dir", default="portfolio_runs/latest", help="Output directory")
    args = p.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output_dir)
    cfg_path = Path(args.config) if args.config else None

    cfg = load_config(cfg_path)
    candles = load_candles(input_path)
    if len(candles) < 50:
        raise ValueError("Need at least 50 rows.")

    summary = run_portfolio_sim(candles, cfg, out_dir)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
