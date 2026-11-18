#!/usr/bin/env python3
"""
HTTP 451-Proof Production Scanner for 4H-SWEEP-ABSORB V3 (Paper Mode Only)
- Fetches 1H candles from data.binance.vision + recent live 1H klines with fallback
- Resamples to 4H and checks the most recently closed 4H bar (zero lookahead)
- Logs new signals to master_trade_log.csv and master_trade_log.json
"""
import csv
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import numpy as np
import pandas as pd
import requests

MODEL_NAME = "4H-SWEEP-ABSORB V3"
LIVE_ORDERS_ENABLED = False  # Strictly paper/research logging unless explicitly authorized

UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "UNIUSDT", "AAVEUSDT", "BNBUSDT", "DOTUSDT",
    "BCHUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "INJUSDT", "TIAUSDT", "FILUSDT", "ETCUSDT",
]

CACHE_DIR = "./binance_vision_cache_1h"
LOG_CSV = "master_trade_log.csv"
LOG_JSON = "master_trade_log.json"

LOOKBACK = 20
WICK_MIN = 0.40
TBR_BAND = 0.05
VOL_MIN = 1.30
MIN_STOP_PCT = 0.020
MAX_STOP_PCT = 0.055
TP_R = 2.2
BE_TRIGGER_R = 1.0
BE_LOCK_R = 0.15
MAX_BARS_4H = 9

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count", "taker_buy_volume",
    "taker_buy_quote_volume", "ignore",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SweepAbsorbScanner/1.0)"}


def month_keys_for_lookback(days_back: int = 25) -> List[str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    keys = set()
    cur = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    end_m = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while cur <= end_m:
        keys.add(f"{cur.year:04d}-{cur.month:02d}")
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
    return sorted(keys)


def fetch_vision_1h(symbol: str, months: List[str], session: requests.Session) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    cur_ym = f"{now.year:04d}-{now.month:02d}"
    frames = []

    for ym in months:
        cache_path = os.path.join(CACHE_DIR, f"{symbol}-1h-{ym}.csv.gz")
        if ym != cur_ym and os.path.exists(cache_path):
            try:
                frames.append(pd.read_csv(cache_path))
                continue
            except Exception:
                os.remove(cache_path)

        url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{ym}.zip"
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    raw = pd.read_csv(f, header=None)
                    if isinstance(raw.iloc[0, 0], str) and not str(raw.iloc[0, 0]).isdigit():
                        raw = raw.iloc[1:].reset_index(drop=True)
                    raw.columns = KLINE_COLS[: raw.shape[1]]
                    df_m = raw[["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]].copy()
                    if ym != cur_ym:
                        df_m.to_csv(cache_path, index=False, compression="gzip")
                    frames.append(df_m)
        except Exception:
            continue

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "volume", "taker_buy_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
    df.loc[df["open_time"] > 10**14, "open_time"] //= 1000
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.drop_duplicates("dt").sort_values("dt").set_index("dt")[
        ["open", "high", "low", "close", "volume", "taker_buy_volume"]
    ]


def fetch_recent_rest_1h(symbol: str, session: requests.Session) -> pd.DataFrame:
    """Fallback/tail fetcher using data-api.binance.vision (avoids HTTP 451 where reachable)."""
    endpoints = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit=120",
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=120",
    ]
    for url in endpoints:
        try:
            resp = session.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                continue
            df = pd.DataFrame(rows, columns=KLINE_COLS[: len(rows[0])])
            for col in ["open", "high", "low", "close", "volume", "taker_buy_volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
            df.loc[df["open_time"] > 10**14, "open_time"] //= 1000
            df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            return df.drop_duplicates("dt").sort_values("dt").set_index("dt")[
                ["open", "high", "low", "close", "volume", "taker_buy_volume"]
            ]
        except Exception:
            continue
    return pd.DataFrame()


def load_aligned_1h(symbols: List[str], months: List[str]) -> Dict[str, pd.DataFrame]:
    session = requests.Session()
    merged: Dict[str, pd.DataFrame] = {}

    for sym in symbols:
        df_vis = fetch_vision_1h(sym, months, session)
        df_api = fetch_recent_rest_1h(sym, session)
        parts = [p for p in [df_vis, df_api] if not p.empty]
        if not parts:
            continue
        comb = pd.concat(parts)
        comb = comb[~comb.index.duplicated(keep="last")].sort_index()
        merged[sym] = comb

    if "BTCUSDT" not in merged or merged["BTCUSDT"].empty:
        raise RuntimeError("Failed to load BTCUSDT 1H candles from Binance sources.")

    idx = merged["BTCUSDT"].index
    aligned = {}
    for sym, df in merged.items():
        aligned[sym] = df.reindex(idx).ffill().dropna()
    return aligned


def resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    return df_1h.resample("4h", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "taker_buy_volume": "sum",
    }).dropna()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def scan_closed_4h_signals(bars_4h: Dict[str, pd.DataFrame]) -> List[Dict]:
    """Evaluates only the most recently completed 4H bar (index -2) to prevent lookahead."""
    btc = bars_4h["BTCUSDT"]
    if len(btc) < max(LOOKBACK + 5, 60):
        return []

    closed_idx = btc.index[-2]
    btc_ema50 = btc["close"].ewm(span=50, adjust=False).mean()
    btc_c = float(btc.loc[closed_idx, "close"])
    btc_e50 = float(btc_ema50.loc[closed_idx])

    btc_long_ok = btc_c > btc_e50
    btc_short_ok = btc_c < btc_e50

    found: List[Dict] = []
    for sym, df in bars_4h.items():
        if sym == "BTCUSDT" or closed_idx not in df.index:
            continue
        pos = df.index.get_loc(closed_idx)
        if pos < LOOKBACK + 2:
            continue

        atr14 = calc_atr(df, 14)
        prior_hi = df["high"].rolling(LOOKBACK).max().shift(1)
        prior_lo = df["low"].rolling(LOOKBACK).min().shift(1)
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        lower_wick = (np.minimum(df["open"], df["close"]) - df["low"]) / rng
        upper_wick = (df["high"] - np.maximum(df["open"], df["close"])) / rng
        tbr = df["taker_buy_volume"] / df["volume"].replace(0, np.nan)
        vol_rel = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)

        lo = float(df.loc[closed_idx, "low"])
        hi = float(df.loc[closed_idx, "high"])
        cl = float(df.loc[closed_idx, "close"])
        plo = float(prior_lo.loc[closed_idx])
        phi = float(prior_hi.loc[closed_idx])
        lw = float(lower_wick.loc[closed_idx]) if np.isfinite(lower_wick.loc[closed_idx]) else 0.0
        uw = float(upper_wick.loc[closed_idx]) if np.isfinite(upper_wick.loc[closed_idx]) else 0.0
        tb = float(tbr.loc[closed_idx]) if np.isfinite(tbr.loc[closed_idx]) else 0.5
        vr = float(vol_rel.loc[closed_idx]) if np.isfinite(vol_rel.loc[closed_idx]) else 0.0
        av = float(atr14.loc[closed_idx]) if np.isfinite(atr14.loc[closed_idx]) else 0.0

        is_long = (lo < plo) and (cl > plo) and (lw >= WICK_MIN) and (tb <= (0.50 - TBR_BAND)) and (vr >= VOL_MIN) and btc_long_ok
        is_short = (hi > phi) and (cl < phi) and (uw >= WICK_MIN) and (tb >= (0.50 + TBR_BAND)) and (vr >= VOL_MIN) and btc_short_ok

        if not (is_long or is_short) or cl <= 0 or av <= 0:
            continue

        side = "LONG" if is_long else "SHORT"
        direction = 1 if is_long else -1
        stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, (1.8 * av) / cl))
        sl_price = cl * (1.0 - direction * stop_pct)
        tp1_price = cl * (1.0 + direction * stop_pct * BE_TRIGGER_R)
        tp2_price = cl * (1.0 + direction * stop_pct * TP_R)

        found.append({
            "timestamp_scan_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "closed_4h_bar_utc": closed_idx.strftime("%Y-%m-%d %H:%M:%S"),
            "model": MODEL_NAME,
            "symbol": sym,
            "side": side,
            "ref_entry_price": round(cl, 6),
            "stop_loss_price": round(sl_price, 6),
            "tp1_be_price": round(tp1_price, 6),
            "tp2_target_price": round(tp2_price, 6),
            "stop_pct": round(stop_pct * 100.0, 2),
            "tp_r": TP_R,
            "be_trigger_r": BE_TRIGGER_R,
            "be_lock_r": BE_LOCK_R,
            "max_bars_4h": MAX_BARS_4H,
            "vol_rel": round(vr, 2),
            "tbr": round(tb, 3),
            "mode": "PAPER",
        })

    found.sort(key=lambda r: r["vol_rel"], reverse=True)
    return found


def persist_signals(new_signals: List[Dict]) -> List[Dict]:
    existing: List[Dict] = []
    if os.path.exists(LOG_JSON):
        try:
            with open(LOG_JSON, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                existing = [r for r in loaded if isinstance(r, dict)]
            elif isinstance(loaded, dict):
                for k in ("signals", "trades", "records", "data"):
                    if isinstance(loaded.get(k), list):
                        existing = [r for r in loaded[k] if isinstance(r, dict)]
                        break
        except Exception:
            existing = []

    seen = {
        (r.get("model"), r.get("symbol"), r.get("closed_4h_bar_utc"), r.get("side"))
        for r in existing
    }
    truly_new: List[Dict] = []

    for sig in new_signals:
        key = (sig["model"], sig["symbol"], sig["closed_4h_bar_utc"], sig["side"])
        if key not in seen:
            seen.add(key)
            existing.append(sig)
            truly_new.append(sig)

    with open(LOG_JSON, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    if existing:
        fieldnames: List[str] = []
        for row in existing:
            for col in row.keys():
                if col not in fieldnames:
                    fieldnames.append(col)
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing)

    return truly_new


def main() -> None:
    if LIVE_ORDERS_ENABLED:
        raise RuntimeError("Live orders are disabled. Explicit authorization is required.")

    months = month_keys_for_lookback(days_back=25)
    data_1h = load_aligned_1h(UNIVERSE, months)
    bars_4h = {s: resample_4h(df) for s, df in data_1h.items()}
    signals = scan_closed_4h_signals(bars_4h)
    added = persist_signals(signals)

    print(f"[{MODEL_NAME}] Scan complete | Closed 4H Bar: {bars_4h['BTCUSDT'].index[-2]} | New Signals Logged: {len(added)}")
    for s in added:
        print(
            f"  -> {s['symbol']} {s['side']} | Ref: {s['ref_entry_price']} | SL: {s['stop_loss_price']} "
            f"({s['stop_pct']}%) | TP2: {s['tp2_target_price']} ({s['tp_r']}R) | VolRel: {s['vol_rel']}x | TBR: {s['tbr']}"
        )


if __name__ == "__main__":
    main()
