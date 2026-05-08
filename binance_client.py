#!/usr/bin/env python3
"""
Binance Client — conexión autenticada a Binance (testnet o mainnet).
Usa las claves de .env.local.

Métodos principales:
- get_klines(symbol, interval, limit) → List[Candle]
- get_account() → dict
- place_market_order(symbol, side, quantity) → dict
- place_stop_order(symbol, side, quantity, stopPrice, sl?, tp?) → dict
- get_open_orders(symbol) → list
- cancel_order(symbol, orderId) → bool
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError


class BinanceClient:
    """Cliente Binance con firma HMAC-SHA256."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.binance.com"):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.base = base_url.rstrip("/")
        self._headers = {"X-MBX-APIKEY": self.api_key, "User-Agent": "BTA/1.0"}

    # ── Público (sin firma) ──────────────────────────────────────────────────

    def ping(self) -> bool:
        return self._get("/api/v3/ping") == {}

    def get_server_time(self) -> int:
        return self._get("/api/v3/time")["serverTime"]

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> List[dict]:
        """Fetch OHLCV candles. Devuelve lista de dicts con timestamp_utc, open, high, low, close, volume."""
        data = self._get(f"/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}")
        rows = []
        for k in data:
            ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
            rows.append({
                "timestamp_utc": ts.isoformat().replace("+00:00", "Z"),
                "instrument": symbol.upper(),
                "timeframe": interval,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return rows

    def get_ticker(self, symbol: str) -> dict:
        return self._get(f"/api/v3/ticker/24hr?symbol={symbol.upper()}")

    def get_exchange_info(self, symbol: str = None) -> dict:
        path = "/api/v3/exchangeInfo"
        if symbol:
            path += f"?symbol={symbol.upper()}"
        return self._get(path)

    def get_filters(self, symbol: str) -> dict:
        """Devuelve los filtros de trading para un símbolo (LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER, etc)."""
        info = self.get_exchange_info(symbol)
        if not info.get("symbols"):
            return {}
        filters = {f["filterType"]: f for f in info["symbols"][0].get("filters", [])}
        return filters

    # ── Privado (firmado) ────────────────────────────────────────────────────

    def get_account(self) -> dict:
        return self._signed_get("/api/v3/account")

    def get_balance(self, asset: str = None) -> List[dict]:
        """Devuelve lista de balances no-cero. Si asset se especifica, devuelve ese balance."""
        acct = self.get_account()
        balances = [b for b in acct.get("balances", []) if float(b["free"]) > 0 or float(b["locked"]) > 0]
        if asset:
            return [b for b in balances if b["asset"] == asset]
        return balances

    def get_open_orders(self, symbol: str = None) -> list:
        path = "/api/v3/openOrders"
        if symbol:
            path += f"?symbol={symbol.upper()}"
        return self._signed_get(path)

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """side = 'BUY' o 'SELL'. quantity validado contra LOT_SIZE."""
        qty = self._validate_quantity(symbol, quantity)
        params = f"symbol={symbol.upper()}&side={side}&type=MARKET&quantity={qty}"
        return self._signed_post("/api/v3/order", params)

    def place_limit_order(self, symbol: str, side: str, quantity: float, price: float,
                          time_in_force: str = "GTC") -> dict:
        qty = self._validate_quantity(symbol, quantity)
        pr = self._validate_price(symbol, price)
        params = f"symbol={symbol.upper()}&side={side}&type=LIMIT&timeInForce={time_in_force}&quantity={qty}&price={pr}"
        return self._signed_post("/api/v3/order", params)

    def place_stop_order(self, symbol: str, side: str, quantity: float, stop_price: float,
                         sl_price: float = None, tp_price: float = None) -> dict:
        """Stop-loss order. Si sl_price o tp_price se especifican, se añaden como trailing SL/TP."""
        qty = self._validate_quantity(symbol, quantity)
        sp = self._validate_price(symbol, stop_price)
        params = f"symbol={symbol.upper()}&side={side}&type=STOP_LOSS_LIMIT&quantity={qty}&stopPrice={sp}&price={sp}&timeInForce=GTC"
        if sl_price:
            params += f"&stopLimitPrice={self._validate_price(symbol, sl_price)}"
        if tp_price:
            params += f"&takeProfitLimitPrice={self._validate_price(symbol, tp_price)}"
        return self._signed_post("/api/v3/order", params)

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        params = f"symbol={symbol.upper()}&orderId={order_id}"
        return self._signed_delete("/api/v3/order", params)

    def get_order_status(self, symbol: str, order_id: int) -> dict:
        params = f"symbol={symbol.upper()}&orderId={order_id}"
        return self._signed_get(f"/api/v3/order?{params}")

    def get_my_trades(self, symbol: str, limit: int = 50) -> list:
        return self._signed_get(f"/api/v3/myTrades?symbol={symbol.upper()}&limit={limit}")

    def get_account_snapshot(self, snapshot_type: str = "SPOT") -> dict:
        return self._signed_get(f"/sapi/v1/accountSnapshot?type={snapshot_type}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _validate_quantity(self, symbol: str, quantity: float) -> float:
        filters = self.get_filters(symbol)
        lot = filters.get("LOT_SIZE", {})
        step = float(lot.get("stepSize", "0.001"))
        min_qty = float(lot.get("minQty", "0.001"))
        qty = round(quantity / step) * step
        if qty < min_qty:
            qty = min_qty
        return qty

    def _validate_price(self, symbol: str, price: float) -> float:
        filters = self.get_filters(symbol)
        pf = filters.get("PRICE_FILTER", {})
        tick = float(pf.get("tickSize", "0.0001"))
        return round(price / tick) * tick

    def _signed_get(self, path: str) -> dict:
        ts = int(time.time() * 1000)
        sep = "&" if "?" in path else "?"
        url = f"{self.base}{path}{sep}timestamp={ts}"
        sig = self._sign(url.split("?", 1)[1])
        url += f"&signature={sig}"
        return self._get(url, use_headers=True)

    def _signed_post(self, path: str, params: str = "") -> dict:
        ts = int(time.time() * 1000)
        full_params = f"{params}&timestamp={ts}" if params else f"timestamp={ts}"
        sig = self._sign(full_params)
        url = f"{self.base}{path}"
        body = f"{full_params}&signature={sig}"
        return self._post(url, body)

    def _signed_delete(self, path: str, params: str = "") -> dict:
        ts = int(time.time() * 1000)
        full_params = f"{params}&timestamp={ts}" if params else f"timestamp={ts}"
        sig = self._sign(full_params)
        url = f"{self.base}{path}?{full_params}&signature={sig}"
        return self._delete(url)

    def _sign(self, params: str) -> str:
        return hmac.new(self.api_secret.encode("utf-8"), params.encode("utf-8"), hashlib.sha256).hexdigest()

    def _get(self, url: str, use_headers: bool = False) -> dict:
        if url.startswith("/"):
            url = self.base + url
        req = Request(url, headers=self._headers if use_headers else {"User-Agent": "BTA/1.0"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, url: str, body: str) -> dict:
        if url.startswith("/"):
            url = self.base + url
        data = body.encode("utf-8")
        req = Request(url, data=data, headers={**self._headers, "Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _delete(self, url: str) -> dict:
        if url.startswith("/"):
            url = self.base + url
        req = Request(url, method="DELETE", headers=self._headers)
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ── Factory ──────────────────────────────────────────────────────────────────

def load_client(env_path: Path = None, testnet: bool = True) -> BinanceClient:
    """Carga claves desde .env.local y devuelve un cliente Binance (testnet por defecto)."""
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env.local"
    if not env_path.exists():
        # Try sibling dirs
        for p in [Path(".env.local"), Path("../.env.local")]:
            if p.exists():
                env_path = p
                break

    # Simple .env parser
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

    if testnet:
        api_key = env.get("BINANCE_TESTNET_API", env.get("BINANCE_API_KEY", ""))
        api_secret = env.get("BINANCE_TESTNET_API_SECRET", env.get("BINANCE_API_SECRET", ""))
        base_url = env.get("BINANCE_TESTNET_BASE_URL", "https://testnet.binance.vision")
    else:
        api_key = env.get("BINANCE_API_KEY", "")
        api_secret = env.get("BINANCE_API_SECRET", "")
        base_url = env.get("BINANCE_BASE_URL", "https://api.binance.com")

    if not api_key or not api_secret:
        raise ValueError("API key/secret no encontradas en .env.local")

    return BinanceClient(api_key, api_secret, base_url)


# ── Test rápido ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    client = load_client(testnet=True)
    print(f"Ping: {client.ping()}")
    print(f"Time: {client.get_server_time()}")
    print(f"SOLUSDT: ${client.get_ticker('SOLUSDT')['lastPrice']}")
    bal = client.get_balance()
    print(f"Balances ({len(bal)}):")
    for b in bal[:5]:
        print(f"  {b['asset']}: {b['free']}")
