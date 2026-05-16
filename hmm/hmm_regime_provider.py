#!/usr/bin/env python3
"""Crypto HMM regime provider (Path B, isolated).

Non-invasive helper module for the bots-trading-autonomos repo.
It reads existing local live feeds (runtime/live/*_5m.csv), resamples them to a
higher timeframe, trains one GaussianHMM per symbol, maps states to the
orchestrator's regime vocabulary, and returns a current regime snapshot.

This module does NOT modify orchestrator/bot behaviour by itself.
It is meant to be queried manually or from a future optional integration layer.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE_DIR = ROOT / "runtime" / "live"
DEFAULT_MODELS_DIR = ROOT / "hmm" / "models"
DEFAULT_OUTPUT_DIR = ROOT / "hmm" / "output"

REGIME_PRIORITY = {"risk_off": 0, "bear": 1, "sideways": 2, "bull": 3, "unknown": 4}


@dataclass
class ProviderConfig:
    timeframe: str = "4h"
    feature_window: int = 20
    min_states: int = 2
    max_states: int = 5
    n_iter: int = 250
    random_state: int = 42
    min_bars_after_resample: int = 120
    bars_limit_5m: int = 5000
    default_symbols: tuple[str, ...] = ("SOLUSDT", "XRPUSDT")


@dataclass
class SymbolRegimeSnapshot:
    symbol: str
    timeframe: str
    regime: str
    confidence: float
    hmm_states: int
    hmm_state: int
    reason: str
    metrics: dict[str, Any]
    transition_probs: dict[str, float]


@dataclass
class MergedSnapshot:
    regime: str
    confidence: float
    reason: str
    symbols: list[dict[str, Any]]


class HmmRegimeProvider:
    def __init__(
        self,
        live_dir: Path | None = None,
        models_dir: Path | None = None,
        output_dir: Path | None = None,
        cfg: ProviderConfig | None = None,
    ) -> None:
        self.live_dir = Path(live_dir or DEFAULT_LIVE_DIR)
        self.models_dir = Path(models_dir or DEFAULT_MODELS_DIR)
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self.cfg = cfg or ProviderConfig()
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────
    # feed loading / resampling
    # ──────────────────────────────────────────────────────────────────
    def load_symbol_feed(self, symbol: str) -> pd.DataFrame:
        path = self.live_dir / f"{symbol}_5m.csv"
        if not path.exists():
            raise FileNotFoundError(f"feed not found: {path}")

        df = pd.read_csv(path)
        if "timestamp_utc" not in df.columns:
            raise ValueError(f"feed missing timestamp_utc: {path}")

        cols = {c.lower(): c for c in df.columns}
        need = ["open", "high", "low", "close", "volume"]
        missing = [c for c in need if c not in cols]
        if missing:
            raise ValueError(f"feed missing columns {missing}: {path}")

        out = pd.DataFrame({k: pd.to_numeric(df[cols[k]], errors="coerce") for k in need})
        ts = pd.to_datetime(df["timestamp_utc"], utc=True)
        out.index = pd.DatetimeIndex(ts.dt.tz_convert(None))
        out.index.name = "timestamp"
        out = out.dropna().sort_index()
        if self.cfg.bars_limit_5m and len(out) > self.cfg.bars_limit_5m:
            out = out.iloc[-self.cfg.bars_limit_5m :]
        return out

    def resample_ohlcv(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        rule_map = {
            "1h": "1h",
            "4h": "4h",
            "1d": "1d",
        }
        if timeframe not in rule_map:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        rule = rule_map[timeframe]
        out = pd.DataFrame(
            {
                "open": df["open"].resample(rule).first(),
                "high": df["high"].resample(rule).max(),
                "low": df["low"].resample(rule).min(),
                "close": df["close"].resample(rule).last(),
                "volume": df["volume"].resample(rule).sum(),
            }
        ).dropna()
        return out

    def fetch_remote_ohlcv(self, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={timeframe}&limit={limit}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data:
            raise ValueError(f"no remote bars for {symbol} {timeframe}")
        rows = []
        for k in data:
            ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            rows.append({"timestamp_utc": ts, "open": k[1], "high": k[2], "low": k[3], "close": k[4], "volume": k[5]})
        df = pd.DataFrame(rows)
        out = pd.DataFrame(
            {
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "volume": pd.to_numeric(df["volume"], errors="coerce"),
            }
        )
        ts = pd.to_datetime(df["timestamp_utc"], utc=True)
        out.index = pd.DatetimeIndex(ts.dt.tz_convert(None))
        out.index.name = "timestamp"
        return out.dropna().sort_index()

    # ──────────────────────────────────────────────────────────────────
    # HMM features / training
    # ──────────────────────────────────────────────────────────────────
    def build_features(self, close: pd.Series) -> pd.DataFrame:
        w = self.cfg.feature_window
        log_ret = np.log(close / close.shift(1))
        feat = pd.DataFrame(index=close.index)
        feat["volatility"] = log_ret.rolling(w).std() * np.sqrt(252)
        feat["cumulative_return"] = log_ret.rolling(w).sum()
        feat["momentum"] = close.pct_change(w)
        feat = feat.replace([np.inf, -np.inf], np.nan).dropna()
        return feat

    def _scale_features(self, features: pd.DataFrame) -> tuple[np.ndarray, StandardScaler]:
        scaler = StandardScaler()
        X = scaler.fit_transform(features.values)
        return X, scaler

    def _model_cache_path(self, symbol: str) -> Path:
        tf = self.cfg.timeframe.replace("/", "_")
        return self.models_dir / f"{symbol}_{tf}_hmm.pkl"

    def train_hmm(self, features: pd.DataFrame) -> tuple[GaussianHMM, int, np.ndarray, dict[int, int], dict[str, Any], StandardScaler]:
        X, scaler = self._scale_features(features)
        best_model: GaussianHMM | None = None
        best_bic = float("inf")
        best_n = self.cfg.min_states

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for n in range(self.cfg.min_states, self.cfg.max_states + 1):
                model = GaussianHMM(
                    n_components=n,
                    covariance_type="diag",
                    n_iter=self.cfg.n_iter,
                    random_state=self.cfg.random_state,
                    tol=1e-5,
                )
                try:
                    model.fit(X)
                    if not np.isfinite(model.startprob_).all() or not np.isfinite(model.transmat_).all():
                        continue
                    if np.any(model.transmat_.sum(axis=1) == 0):
                        continue
                    log_lik = model.score(X)
                except Exception:
                    continue
                n_params = n * (n - 1)
                n_params += n * X.shape[1] * 2
                bic = -2 * log_lik + n_params * np.log(len(X))
                if bic < best_bic:
                    best_bic = bic
                    best_model = model
                    best_n = n

        if best_model is None:
            raise ValueError("no valid HMM model converged")
        states = best_model.predict(X)
        tmp = features.copy()
        tmp["raw_state"] = states
        state_vol = tmp.groupby("raw_state")["volatility"].mean()
        ordered = list(state_vol.sort_values().index)
        ordered_map = {old: new for new, old in enumerate(ordered)}
        ordered_states = np.array([ordered_map[s] for s in states], dtype=int)
        info = {"bic": round(float(best_bic), 3), "states": best_n}
        return best_model, best_n, ordered_states, ordered_map, info, scaler

    def _save_cache(
        self,
        symbol: str,
        model: GaussianHMM,
        ordered_map: dict[int, int],
        feature_index: Iterable[pd.Timestamp],
        ordered_states: np.ndarray,
        state_stats: dict[int, dict[str, float]],
        training_meta: dict[str, Any],
    ) -> None:
        payload = {
            "_format_version": 2,
            "model": model,
            "ordered_map": ordered_map,
            "feature_index": [ts.isoformat() for ts in feature_index],
            "ordered_states": ordered_states.tolist(),
            "state_stats": state_stats,
            "training_meta": training_meta,
            "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        with self._model_cache_path(symbol).open("wb") as f:
            pickle.dump(payload, f)

    def _load_cache(self, symbol: str) -> dict[str, Any] | None:
        path = self._model_cache_path(symbol)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                cache = pickle.load(f)
            if cache.get("_format_version") != 2:
                return None
            return cache
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────
    # Regime mapping
    # ──────────────────────────────────────────────────────────────────
    def _state_stats(self, features: pd.DataFrame, ordered_states: np.ndarray) -> dict[int, dict[str, float]]:
        tmp = features.copy()
        tmp["state"] = ordered_states
        stats: dict[int, dict[str, float]] = {}
        for state, grp in tmp.groupby("state"):
            stats[int(state)] = {
                "volatility": float(grp["volatility"].mean()),
                "cumulative_return": float(grp["cumulative_return"].mean()),
                "momentum": float(grp["momentum"].mean()),
            }
        return stats

    def _label_for_state(
        self,
        state: int,
        state_stats: dict[int, dict[str, float]],
        total_states: int,
    ) -> tuple[str, str]:
        """Map ordered HMM state to regime label.

        State 0 = lowest vol = typically bull/start of uptrend.
        State max = highest vol = risk_off only if momentum/return negative,
        otherwise may be volatile bull.
        """
        s = state_stats[state]
        vol_rank = state / max(1, total_states - 1)  # 0=lowest, 1=highest
        cumret = float(s["cumulative_return"])
        mom = float(s["momentum"])
        vol = float(s["volatility"])

        # risk_off: extreme vol AND negative return AND negative momentum
        if vol_rank >= 0.8 and cumret < 0 and mom < 0:
            return "risk_off", f"high_vol_negative_trend vol={vol:.4f} cumret={cumret:.4f} mom={mom:.4f}"
        # bear: declining with neg return/momentum (any vol level)
        if cumret < 0 and mom < 0:
            return "bear", f"negative_return_momentum vol={vol:.4f} cumret={cumret:.4f} mom={mom:.4f}"
        # bull: rising with positive return and momentum (any vol)
        if cumret > 0 and mom > 0:
            return "bull", f"positive_trend vol={vol:.4f} cumret={cumret:.4f} mom={mom:.4f}"
        # sideways: everything else
        return "sideways", f"mixed_state vol={vol:.4f} cumret={cumret:.4f} mom={mom:.4f}"

    def _predict_current_state(self, model: GaussianHMM, ordered_map: dict[int, int], features: pd.DataFrame) -> tuple[int, np.ndarray]:
        X = features.values
        raw_state = int(model.predict(X)[-1])
        probs = model.predict_proba(X)[-1]
        ordered_state = int(ordered_map[raw_state])
        ordered_probs = np.zeros_like(probs)
        for raw_state, ordered_state_idx in ordered_map.items():
            ordered_probs[ordered_state_idx] = probs[raw_state]
        return ordered_state, ordered_probs

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────
    def get_symbol_snapshot(self, symbol: str, force_retrain: bool = False) -> SymbolRegimeSnapshot:
        data_source = "local_5m"
        raw = self.load_symbol_feed(symbol)
        bars = self.resample_ohlcv(raw, self.cfg.timeframe)
        if len(bars) < self.cfg.min_bars_after_resample:
            bars = self.fetch_remote_ohlcv(symbol, self.cfg.timeframe, limit=max(300, self.cfg.min_bars_after_resample * 3))
            data_source = f"binance_remote_{self.cfg.timeframe}"
        if len(bars) < self.cfg.min_bars_after_resample:
            raise ValueError(f"not enough bars after resample for {symbol}: {len(bars)}")

        features = self.build_features(bars["close"])
        if len(features) < self.cfg.min_bars_after_resample // 2:
            raise ValueError(f"not enough feature rows for {symbol}: {len(features)}")

        cache = None if force_retrain else self._load_cache(symbol)
        if cache is None:
            model, n_states, ordered_states, ordered_map, train_info, _scaler = self.train_hmm(features)
            state_stats = self._state_stats(features, ordered_states)
            self._save_cache(symbol, model, ordered_map, features.index, ordered_states, state_stats, {**train_info, "rows": len(features)})
        else:
            model = cache["model"]
            ordered_map = {int(k): int(v) for k, v in cache["ordered_map"].items()}
            state_stats = {int(k): {kk: float(vv) for kk, vv in vals.items()} for k, vals in cache["state_stats"].items()}
            n_states = int(cache["training_meta"]["states"])
            train_info = cache["training_meta"]

        state, probs = self._predict_current_state(model, ordered_map, features)
        regime, reason = self._label_for_state(state, state_stats, n_states)
        raw_state_for_current = next(raw_state for raw_state, ordered_state_idx in ordered_map.items() if ordered_state_idx == state)
        transition = model.transmat_[raw_state_for_current]
        ordered_transition = np.zeros_like(transition)
        for raw_state, ordered_state_idx in ordered_map.items():
            ordered_transition[ordered_state_idx] = transition[raw_state]

        metrics = {
            "data_source": data_source,
            "bars_5m": int(len(raw)),
            "bars_resampled": int(len(bars)),
            "feature_rows": int(len(features)),
            "last_close": float(bars["close"].iloc[-1]),
            "last_bar_at": bars.index[-1].isoformat(),
            "volatility": round(state_stats[state]["volatility"], 6),
            "cumulative_return": round(state_stats[state]["cumulative_return"], 6),
            "momentum": round(state_stats[state]["momentum"], 6),
            "bic": round(float(train_info["bic"]), 3),
            "feature_scaling": "StandardScaler",
        }
        transition_probs = {f"state_{i}": round(float(p), 4) for i, p in enumerate(ordered_transition)}

        return SymbolRegimeSnapshot(
            symbol=symbol,
            timeframe=self.cfg.timeframe,
            regime=regime,
            confidence=round(float(probs[state]), 4),
            hmm_states=n_states,
            hmm_state=state,
            reason=reason,
            metrics=metrics,
            transition_probs=transition_probs,
        )

    def get_snapshot(self, symbols: list[str] | None = None, force_retrain: bool = False) -> MergedSnapshot:
        symbols = symbols or list(self.cfg.default_symbols)
        snaps: list[SymbolRegimeSnapshot] = []
        for sym in symbols:
            try:
                snaps.append(self.get_symbol_snapshot(sym, force_retrain=force_retrain))
            except Exception as e:
                snaps.append(
                    SymbolRegimeSnapshot(
                        symbol=sym,
                        timeframe=self.cfg.timeframe,
                        regime="unknown",
                        confidence=0.0,
                        hmm_states=0,
                        hmm_state=-1,
                        reason=f"feed_unavailable_or_invalid: {e}",
                        metrics={},
                        transition_probs={},
                    )
                )
        valid_snaps = [s for s in snaps if s.regime != "unknown"]
        if not valid_snaps:
            return MergedSnapshot(
                regime="sideways",
                confidence=0.0,
                reason=f"all_hmm_regimes_unknown:{symbols}",
                symbols=[asdict(s) for s in snaps],
            )
        merged = min(valid_snaps, key=lambda s: REGIME_PRIORITY.get(s.regime, 99))
        avg_conf = float(np.mean([s.confidence for s in snaps])) if snaps else 0.0
        return MergedSnapshot(
            regime=merged.regime,
            confidence=round(avg_conf, 4),
            reason=f"hmm_multi_symbol: most_conservative_is_{merged.regime}",
            symbols=[asdict(s) for s in snaps],
        )

    def write_snapshot_json(self, snapshot: MergedSnapshot, path: Path | None = None) -> Path:
        path = path or (self.output_dir / "hmm_regime_snapshot.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), **asdict(snapshot)}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crypto HMM regime provider (isolated Path B helper)")
    p.add_argument("--symbols", default="SOLUSDT,XRPUSDT", help="comma-separated symbols")
    p.add_argument("--timeframe", default="4h", choices=["1h", "4h", "1d"])
    p.add_argument("--live-dir", default=str(DEFAULT_LIVE_DIR))
    p.add_argument("--models-dir", default=str(DEFAULT_MODELS_DIR))
    p.add_argument("--output-json", default=str(DEFAULT_OUTPUT_DIR / "hmm_regime_snapshot.json"))
    p.add_argument("--force-retrain", action="store_true")
    p.add_argument("--print-json", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = ProviderConfig(timeframe=args.timeframe, default_symbols=tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
    provider = HmmRegimeProvider(live_dir=Path(args.live_dir), models_dir=Path(args.models_dir), output_dir=Path(args.output_json).parent, cfg=cfg)
    snapshot = provider.get_snapshot(force_retrain=args.force_retrain)
    out = provider.write_snapshot_json(snapshot, Path(args.output_json))
    if args.print_json:
        print(Path(out).read_text())
    else:
        print(f"Wrote {out}")
        print(json.dumps(asdict(snapshot), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
