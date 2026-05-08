#!/usr/bin/env python3
"""
Fabian Live Runner — ejecuta la estrategia FabiánPullback en Binance Testnet.
Usa datos OHLCV reales de la API y ejecuta órdenes reales en testnet.

Uso:
  python3 fabian_live_runner.py [--mode pullback|pro] [--symbol SOLUSDT] [--interval 5m]

Requiere .env.local con BINANCE_TESTNET_API y BINANCE_TESTNET_API_SECRET.
"""
from __future__ import annotations

import csv
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from binance_client import load_client, BinanceClient

# Reutilizamos la lógica de estrategia del bot original
from fabian_pullback_bot import (
    Candle, FabianConfig, TradePlan,
    find_swing_highs, find_swing_lows,
    detect_market_structure, detect_strong_breakout,
    find_entry_zone, build_trade_plan,
    get_session, load_config,
)

# ── Config ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "runtime" / "testnet"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADES_LOG = OUT_DIR / "trades_log.csv"
DECISIONS_LOG = OUT_DIR / "decisions_log.csv"
SUMMARY_PATH = OUT_DIR / "summary.json"
STATUS_PATH = OUT_DIR / "status.json"

# ── Estado global ─────────────────────────────────────────────────────────────

class LiveState:
    def __init__(self, initial_balance: float = 100.0):
        self.balance = initial_balance
        self.peak_balance = initial_balance
        self.daily_start_balance = initial_balance
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.trades_london = 0
        self.trades_ny = 0
        self.consecutive_losses = 0
        self.pause_until_ts = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_trades = 0
        self.last_reset_day = datetime.now(timezone.utc).date()
        self.running = True
        self.last_bar_close = 0.0
        self.last_bar_ts = ""

    def reset_daily(self):
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset_day:
            self.trades_today = 0
            self.trades_london = 0
            self.trades_ny = 0
            self.daily_start_balance = self.balance
            self.daily_pnl = 0.0
            self.last_reset_day = today

    def can_trade(self, session: str, cfg: FabianConfig) -> bool:
        if self.trades_today >= cfg.max_trades_per_day:
            return False
        if session == "NONE" and not cfg.crypto_mode:
            return False
        if session == "LONDON" and self.trades_london >= cfg.max_trades_per_session:
            return False
        if session == "NY" and self.trades_ny >= cfg.max_trades_per_session:
            return False
        if self.daily_start_balance > 0:
            loss_pct = (self.daily_pnl / self.daily_start_balance) * 100
            if loss_pct <= -cfg.max_daily_loss_pct:
                return False
        return True


# ── Live Runner ───────────────────────────────────────────────────────────────

class FabianLiveRunner:
    def __init__(self, client: BinanceClient, config: FabianConfig,
                 symbol: str = "SOLUSDT", interval: str = "5m",
                 mode: str = "pullback", initial_balance: float = 100.0):
        self.client = client
        self.cfg = config
        self.symbol = symbol.upper()
        self.interval = interval
        self.mode = mode
        self.initial_balance = initial_balance
        self.state = LiveState(initial_balance)
        self._candles: List[Candle] = []
        self._body_avgs: List[float] = []
        self._last_processed_bar = 0

        # CSV writers
        self._dec_writer = None
        self._tr_writer = None
        self._init_csv()

    def _init_csv(self):
        """Abrir ficheros CSV de log."""
        dec_f = open(DECISIONS_LOG, "a", newline="")
        tr_f = open(TRADES_LOG, "a", newline="")
        self._dec_writer = csv.DictWriter(dec_f, fieldnames=[
            "ts", "session", "structure", "action", "reason",
            "open", "high", "low", "close", "entry", "sl", "tp", "rr", "balance"])
        self._tr_writer = csv.DictWriter(tr_f, fieldnames=[
            "ts", "action", "entry", "exit", "sl", "tp", "pnl", "rr", "reason"])
        if DECISIONS_LOG.stat().st_size == 0:
            self._dec_writer.writeheader()
        if TRADES_LOG.stat().st_size == 0:
            self._tr_writer.writeheader()

    def fetch_candles(self) -> List[Candle]:
        """Obtener velas desde Binance testnet."""
        rows = self.client.get_klines(self.symbol, self.interval, 200)
        candles = []
        for r in rows:
            ts = datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00"))
            candles.append(Candle(
                ts=ts,
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            ))
        return candles

    def update_balance(self):
        """Actualizar balance simulado (no lee del exchange real).
        Usa realized PnL de testnet para actualizar el saldo de $100."""
        try:
            my_trades = self.client.get_my_trades(self.symbol, limit=10)
            total_pnl = 0.0
            for t in my_trades:
                realized = float(t.get('realizedPnl', 0))
                if realized != 0:
                    total_pnl += realized
            sim = self.initial_balance + total_pnl
            if sim > 0:
                self.state.balance = sim
        except Exception as e:
            pass

    def log_decision(self, row: dict):
        if self._dec_writer:
            self._dec_writer.writerow(row)

    def log_trade(self, row: dict):
        if self._tr_writer:
            self._tr_writer.writerow(row)
        # También registrar en trades_log compatible con collector
        self._write_trade_to_csv(row)

    def _write_trade_to_csv(self, row: dict):
        """Formato compatible con trades_log.csv del bot paper."""
        action = row.get("action", "")
        ts = row.get("ts", "")
        f = OUT_DIR / f"fabian_live_{datetime.now().strftime('%Y%m%d')}.csv"
        exists = f.exists()
        with open(f, "a", newline="") as fh:
            w = csv.writer(fh)
            if not exists:
                w.writerow(["ts", "action", "entry", "exit", "sl", "tp", "pnl", "rr", "reason"])
            w.writerow([
                ts, action,
                row.get("entry", ""),
                row.get("exit", ""),
                row.get("sl", ""),
                row.get("tp", ""),
                row.get("pnl", ""),
                row.get("rr", ""),
                row.get("reason", "")
            ])

    def execute_trade(self, plan: TradePlan) -> Optional[dict]:
        """Ejecutar una orden en testnet según el plan."""
        try:
            qty = plan.volume
            side = "BUY" if plan.action == "BUY_STOP" else "SELL"
            stop_price = plan.entry

            # Orden stop-limit
            result = self.client.place_stop_order(
                symbol=self.symbol,
                side=side,
                quantity=qty,
                stop_price=stop_price,
                sl_price=plan.sl,
                tp_price=plan.tp,
            )
            if result.get("orderId"):
                print(f"[LIVE] Orden {side} {qty} {self.symbol} @ {stop_price} — ORDER ID: {result['orderId']}")
                return result
            else:
                print(f"[WARN] Orden no aceptada: {result}")
                return None
        except Exception as e:
            print(f"[ERROR] No se pudo colocar orden: {e}")
            return None

    def cancel_pending_orders(self):
        """Cancelar órdenes pendientes para el símbolo."""
        try:
            orders = self.client.get_open_orders(self.symbol)
            for o in orders:
                if o.get("status") == "NEW":
                    self.client.cancel_order(self.symbol, o["orderId"])
                    print(f"[LIVE] Cancelada orden {o['orderId']}")
        except Exception as e:
            print(f"[WARN] No se pudieron cancelar órdenes: {e}")

    def run_once(self) -> bool:
        """Un ciclo de trading. Devuelve True si sigue corriendo."""
        try:
            # 1. Obtener velas
            candles = self.fetch_candles()
            if len(candles) < 100:
                print(f"[WARN] Pocas velas: {len(candles)}")
                return True

            idx = len(candles) - 1
            if idx == self._last_processed_bar:
                return True  # misma vela, esperar
            self._last_processed_bar = idx

            # 2. Actualizar estado
            self.update_balance()
            self.state.reset_daily()
            self.state.peak_balance = max(self.state.peak_balance, self.state.balance)

            # 3. Body average
            self._candles = candles
            self._compute_body_average(idx)

            # 4. Pausa por racha
            if self.state.pause_until_ts > time.time():
                return True

            # 5. Estructura
            bar = candles[idx]
            if bar.ts == self.state.last_bar_ts:
                return True
            self.state.last_bar_ts = bar.ts.isoformat() if bar.ts else ""

            swing_highs = find_swing_highs(
                [c.close for c in candles], [c.high for c in candles],
                [c.low for c in candles], self.cfg.swing_lookback, self.cfg.structure_bars)
            swing_lows = find_swing_lows(
                [c.close for c in candles], [c.high for c in candles],
                [c.low for c in candles], self.cfg.swing_lookback, self.cfg.structure_bars)

            if len(swing_highs) < 2 or len(swing_lows) < 2:
                return True

            structure, struct_high, struct_low = detect_market_structure(swing_highs, swing_lows, idx)
            session = get_session(bar.ts, self.cfg)

            if structure == "RANGE":
                self.log_decision({
                    "ts": bar.ts.isoformat(), "session": session,
                    "structure": structure, "action": "WAIT", "reason": "RANGO",
                    "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                    "entry": 0, "sl": 0, "tp": 0, "rr": 0, "balance": round(self.state.balance, 2),
                })
                return True

            if not self.state.can_trade(session, self.cfg):
                return True

            # 6. Detectar ruptura
            body = abs(bar.close - bar.open)
            wick = bar.high - bar.low
            wick_body = wick / body if body > 0 else 99
            body_avg = self._body_avgs[idx] if idx < len(self._body_avgs) else 0

            if body < body_avg * self.cfg.force_body_multiplier or wick_body > self.cfg.max_wick_to_body_ratio:
                return True

            action = "WAIT"
            plan = None

            if structure == "BULLISH" and bar.high > struct_high and bar.close > struct_high:
                zone = find_entry_zone("BULLISH", idx, candles)
                if zone:
                    plan = build_trade_plan("BULLISH", zone, struct_high, body_avg, self.cfg, bar, session, idx + 5)
                    action = "BUY_STOP" if plan else "NO_PLAN"

            elif structure == "BEARISH" and bar.low < struct_low and bar.close < struct_low:
                zone = find_entry_zone("BEARISH", idx, candles)
                if zone:
                    plan = build_trade_plan("BEARISH", zone, struct_low, body_avg, self.cfg, bar, session, idx + 5)
                    action = "SELL_STOP" if plan else "NO_PLAN"

            # 7. Decisión log
            self.log_decision({
                "ts": bar.ts.isoformat(), "session": session,
                "structure": structure, "action": action, "reason": plan.reason if plan else action,
                "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                "entry": plan.entry if plan else 0,
                "sl": plan.sl if plan else 0,
                "tp": plan.tp if plan else 0,
                "rr": round(plan.rr, 2) if plan else 0,
                "balance": round(self.state.balance, 2),
            })

            # 8. Ejecutar
            if plan:
                result = self.execute_trade(plan)
                if result:
                    self.state.trades_today += 1
                    if session == "LONDON":
                        self.state.trades_london += 1
                    if session == "NY":
                        self.state.trades_ny += 1
                    self.log_trade({
                        "ts": bar.ts.isoformat(), "action": plan.action,
                        "entry": plan.entry, "exit": "",
                        "sl": plan.sl, "tp": plan.tp,
                        "pnl": 0, "rr": round(plan.rr, 2),
                        "reason": plan.reason,
                    })

            return True

        except Exception as e:
            print(f"[ERROR] run_once: {e}")
            import traceback
            traceback.print_exc()
            return True

    def _compute_body_average(self, idx: int):
        period = self.cfg.body_avg_period
        if idx < period:
            return
        total = 0.0
        for i in range(idx - period, idx):
            total += abs(self._candles[i].close - self._candles[i].open)
        avg = total / period
        if len(self._body_avgs) <= idx:
            self._body_avgs.append(avg)
        else:
            self._body_avgs[idx] = avg

    def run(self):
        """Bucle principal."""
        print(f"=== Fabian Live Runner ({self.mode}) ===")
        print(f"Symbol: {self.symbol} | Interval: {self.interval}")
        print(f"Risk: {self.cfg.risk_percent}% | MinRR: {self.cfg.min_rr}")
        print(f"Output: {OUT_DIR}")
        print(f"Starting...")

        while True:
            try:
                self.run_once()
                self._save_status()
                time.sleep(5)
            except (KeyboardInterrupt, SystemExit):
                print("\n[STOP] Fin")
                break
            except Exception as e:
                print(f"[LOOP] {e}")
                time.sleep(10)

        self._save_summary()
        print("=== Stoged ===" if True else "")  # evitar print literal simple
        print("=== Stopped ===")

    def _save_status(self):
        """Guardar estado actual en JSON y PostgreSQL."""
        status = {
            "bot_name": f"fabian_live_{self.mode}",
            "label": f"Fabian Live {self.mode} (testnet)",
            "is_running": True,
            "balance_usd": round(self.state.balance, 2),
            "pnl_day_usd": round(self.state.daily_pnl, 2),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "testnet",
            "symbol": self.symbol,
            "interval": self.interval,
        }
        STATUS_PATH.write_text(json.dumps(status, indent=2))

        # También escribir a PostgreSQL para el dashboard
        try:
            import psycopg
            with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql:///bots_dashboard")) as conn:
                conn.execute('''
                insert into bot_status(bot_name,is_running,mode,balance_usd,pnl_day_usd,pnl_week_usd,tokens_value_usd,updated_at)
                values(%s,%s,'testnet',%s,%s,0,0,now())
                on conflict(bot_name) do update set
                  is_running=excluded.is_running, balance_usd=excluded.balance_usd,
                  pnl_day_usd=excluded.pnl_day_usd, updated_at=now()
                ''', [f"fabian_live_{self.mode}", True,
                      round(self.state.balance, 2), round(self.state.daily_pnl, 2)])
                conn.commit()
        except Exception as e:
            print(f"[DB] upsert_status: {e}")

    def _save_summary(self):
        """Guardar resumen final."""
        total_pnl = self.state.balance - self.initial_balance
        win_rate = (self.state.total_wins / self.state.total_trades * 100) if self.state.total_trades > 0 else 0
        summary = {
            "initial_balance": self.initial_balance,
            "final_balance": round(self.state.balance, 2),
            "total_pnl": round(total_pnl, 2),
            "total_trades": self.state.total_trades,
            "wins": self.state.total_wins,
            "losses": self.state.total_losses,
            "win_rate_percent": round(win_rate, 2),
        }
        SUMMARY_PATH.write_text(json.dumps(summary, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Fabian Live Runner (testnet)")
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--interval", default="5m")
    p.add_argument("--mode", choices=["pullback", "pro"], default="pullback")
    p.add_argument("--config", help="Ruta a config JSON opcional")
    p.add_argument("--initial-balance", type=float, default=100.0)
    p.add_argument("--risk", type=float, default=2.0)
    p.add_argument("--min-rr", type=float, default=1.2)
    args = p.parse_args()

    cfg = FabianConfig(initial_balance=args.initial_balance,
                       risk_percent=args.risk, min_rr=args.min_rr)
    if args.config:
        cfg_loaded = load_config(Path(args.config))
        for k, v in cfg_loaded.__dict__.items():
            if hasattr(cfg, k) and v != FabianConfig().__dict__.get(k):
                setattr(cfg, k, v)

    if args.mode == "pro":
        cfg.risk_percent = 2.0
        cfg.min_rr = 1.0

    client = load_client(testnet=True)

    runner = FabianLiveRunner(client, cfg, symbol=args.symbol,
                              interval=args.interval, mode=args.mode,
                              initial_balance=args.initial_balance)
    runner.run()


if __name__ == "__main__":
    main()
