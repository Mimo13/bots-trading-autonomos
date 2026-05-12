"""CoinEx API client v2 — spot balance + public ticker."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
import urllib.request
import json as _json
from typing import Any


class CoinExClient:
    BASE_URL = "https://api.coinex.com"

    def __init__(self, access_id: str, secret_key: str):
        self.access_id = access_id
        self.secret_key = secret_key

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        ts = str(int(time.time() * 1000))
        params = dict(params or {})
        params["access_id"] = self.access_id
        params["tonce"] = ts
        # v2 auth: sign access_id + tonce
        sign_str = f"{self.access_id}{ts}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self.BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Authorization": f"{self.access_id}:{signature}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise Exception(f"CoinEx API error {data.get('code')}: {data.get('message')}")
        # v2 returns {"code":0,"data":[...],"message":"OK"}
        return data.get("data", data)

    def get_balance(self) -> dict[str, dict[str, float]]:
        """Return {ASSET: {'free': float, 'locked': float}}."""
        result = self._request("GET", "/v2/assets/spot/balance", {})
        if not result:
            return {}
        out: dict[str, dict[str, float]] = {}
        if isinstance(result, list):
            for item in result:
                asset = item.get("ccy", "")
                out[asset] = {
                    "free": float(item.get("available", 0) or 0),
                    "locked": float(item.get("frozen", 0) or 0),
                }
        elif isinstance(result, dict) and "list" in result:
            for item in result["list"]:
                asset = item.get("ccy", item.get("asset", ""))
                out[asset] = {
                    "free": float(item.get("available", 0) or 0),
                    "locked": float(item.get("frozen", 0) or 0),
                }
        elif isinstance(result, dict):
            for k, v in result.items():
                if isinstance(v, dict):
                    out[k] = {
                        "free": float(v.get("available", v.get("free", 0)) or 0),
                        "locked": float(v.get("frozen", v.get("locked", 0)) or 0),
                    }
        return out


def coinex_price(symbol: str) -> tuple[float | None, float | None]:
    """Return (last_price, change_24h_pct) via CoinEx public ticker.

    Falls back to Binance if CoinEx is unreachable.
    """
    # Try CoinEx first
    try:
        url = f"https://api.coinex.com/api/v1/market/ticker?market={symbol.upper()}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        d = data.get("data", {}).get("ticker", {})
        return float(d.get("last", 0)) or None, float(d.get("change", 0)) or None
    except Exception:
        pass
    # Fallback to Binance public ticker
    try:
        bn_sym = symbol.replace("USDT", "USDT").upper()
        url2 = f"https://api.binance.com/api/v3/ticker/24hr?symbol={bn_sym}"
        req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=8) as r2:
            d2 = _json.loads(r2.read())
        last = float(d2.get("lastPrice", 0)) or None
        pct = float(d2.get("priceChangePercent", 0)) or None
        return last, pct
    except Exception:
        return None, None


def coinex_balance_usd(access_id: str, secret_key: str) -> dict[str, Any]:
    """Return CoinEx portfolio as USD value dict.

    Note: CoinEx API v2 balance endpoint returned 'access_id not exists' for the
    provided key. This may indicate the key was created on the v1 API format and
    needs to be recreated in CoinEx settings for v2 access.
    The price lookup (coinex_price) works via Binance fallback.
    """
    client = CoinExClient(access_id, secret_key)
    try:
        balances = client.get_balance()
    except Exception as e:
        err_str = str(e)
        if "access_id not exists" in err_str or "4005" in err_str:
            return {
                "ok": False,
                "exchange": "CoinEx",
                "error": "API key not valid for v2 endpoint — recreate key in CoinEx settings for v2 API access",
                "items": [],
                "total_usd": 0,
                "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        raise
    total = 0.0
    items = []
    for asset, bal in balances.items():
        free = bal["free"]
        locked = bal["locked"]
        qty = free + locked
        if qty <= 0:
            continue
        usd_value: float | None = None
        change_24h: float | None = None
        if asset.endswith("USDT") or asset == "USDC":
            usd_value = qty
            change_24h = 0.0
        else:
            try:
                last, change_24h = coinex_price(f"{asset}USDT")
                if last and last > 0:
                    usd_value = qty * last
            except Exception:
                pass
        if usd_value is not None:
            total += usd_value
        items.append(
            {
                "asset": asset,
                "free": round(free, 6),
                "locked": round(locked, 6),
                "qty": round(qty, 6),
                "usd_value": round(usd_value, 2) if usd_value else None,
                "change_24h": round(change_24h, 2) if change_24h is not None else None,
            }
        )
    items.sort(key=lambda x: (x.get("usd_value") is None, -(x.get("usd_value") or 0)))
    return {
        "ok": True,
        "exchange": "CoinEx",
        "total_usd": round(total, 2),
        "items": items,
        "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }