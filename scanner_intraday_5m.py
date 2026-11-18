#!/usr/bin/env python3
"""
Binance 5m Intraday Paper Scanner (NO LIVE ORDERS)
- Fetches 5m klines concurrently via ThreadPoolExecutor
- Evaluates strictly closed 5m bars and deduplicates via last_processed_5m
- Persists active state in active_trades_5m.json and history in closed_trades_5m.csv
"""
import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
import requests

LIVE_ORDERS_ENABLED = False

UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "UNIUSDT", "AAVEUSDT", "BNBUSDT", "DOTUSDT",
    "BCHUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "INJUSDT", "TIAUSDT", "FILUSDT", "ETCUSDT",
]

KLINE_ENDPOINTS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://fapi.binance.com/fapi/v1/klines",
]

STATE_FILE = Path("active_trades_5m.json")
HISTORY_FILE = Path("closed_trades_5m.csv")

MAX_SLOTS = 3
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.020
TP_R = 2.0
MAX_BARS_5M = 24
BASE_FRICTION = 0.0015
MAX_WORKERS = 8

TG_BOT_TOKEN = (
    os.getenv("TG_BOT_TOKEN")
    or os.getenv("TG_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or ""
).strip()
TG_CHAT_ID = (
    os.getenv("TG_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or ""
).strip()

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count", "taker_buy_volume",
    "taker_buy_quote_volume", "ignore",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Scanner5m/1.0)"}


def send_telegram(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}")
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    for attempt in range(2):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=6)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))


def fetch_klines_5m(symbol: str, limit: int = 160) -> Optional[pd.DataFrame]:
    params = {"symbol": symbol, "interval": "5m", "limit": limit}
    with requests.Session() as session:
        for base_url in KLINE_ENDPOINTS:
            for attempt in range(2):
                try:
                    resp = session.get(base_url, params=params, headers=HEADERS, timeout=5)
                    if resp.status_code != 200:
                        continue
                    raw = resp.json()
                    if not isinstance(raw, list) or len(raw) < 60:
                        continue
                    df = pd.DataFrame(raw, columns=KLINE_COLS[: len(raw[0])])
                    for col in ["open", "high", "low", "close", "volume", "taker_buy_volume"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
                    df.loc[df["open_time"] > 10**14, "open_time"] //= 1000
                    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
                    df.loc[df["close_time"] > 10**14, "close_time"] //= 1000
                    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
                    df["close_dt"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
                    return df.drop_duplicates("dt").sort_values("dt").set_index("dt")[
                        ["open", "high", "low", "close", "volume", "taker_buy_volume", "close_dt"]
                    ]
                except Exception:
                    pass
                time.sleep(0.3 * (attempt + 1))
    return None


def fetch_all_5m(symbols: List[str], limit: int = 160) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(fetch_klines_5m, s, limit): s for s in symbols}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                df = fut.result()
                if df is not None and len(df) >= 60:
                    out[s] = df
            except Exception:
                continue
    return out


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                active = data.get("active")
                return {
                    "last_processed_5m": data.get("last_processed_5m"),
                    "active": active if isinstance(active, dict) else {},
                }
        except Exception:
            pass
    return {"last_processed_5m": None, "active": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_history(row: dict) -> None:
    write_header = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def evaluate_5m_signals(closed_5m: Dict[str, pd.DataFrame], eval_ts: pd.Timestamp) -> List[dict]:
    btc = closed_5m["BTCUSDT"]
    btc_ema50 = btc["close"].ewm(span=50, adjust=False).mean()
    btc_up = bool(btc["close"].iloc[-1] > btc_ema50.iloc[-1])
    btc_dn = bool(btc["close"].iloc[-1] < btc_ema50.iloc[-1])

    signals: List[dict] = []
    for sym, df in closed_5m.items():
        if sym == "BTCUSDT" or eval_ts not in df.index or len(df) < 55:
            continue
        atr14 = calc_atr(df, 14)
        ema20 = df["close"].ewm(span=20, adjust=False).mean()
        ema50 = df["close"].ewm(span=50, adjust=False).mean()
        prior_hi = df["high"].rolling(20).max().shift(1)
        prior_lo = df["low"].rolling(20).min().shift(1)
        vol_rel = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)
        tbr = df["taker_buy_volume"] / df["volume"].replace(0, np.nan)

        row = df.iloc[-1]
        op, cl = float(row["open"]), float(row["close"])
        av = float(atr14.iloc[-1])
        vr = float(vol_rel.iloc[-1])
        tb = float(tbr.iloc[-1])
        if not (np.isfinite(av) and np.isfinite(vr) and np.isfinite(tb)) or cl <= 0 or av <= 0:
            continue

        long_sig = (
            btc_up and (ema20.iloc[-1] > ema50.iloc[-1])
            and (cl > float(prior_hi.iloc[-1])) and (cl > op)
            and (vr >= 1.60) and (tb >= 0.58)
        )
        short_sig = (
            btc_dn and (ema20.iloc[-1] < ema50.iloc[-1])
            and (cl < float(prior_lo.iloc[-1])) and (cl < op)
            and (vr >= 1.60) and (tb <= 0.42)
        )
        if long_sig or short_sig:
            stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, (1.5 * av) / cl))
            signals.append({
                "symbol": sym,
                "direction": 1 if long_sig else -1,
                "score": vr,
                "stop_pct": stop_pct,
                "tbr": tb,
            })
    return signals


def process_5m_cycle(state: dict) -> None:
    if LIVE_ORDERS_ENABLED:
        raise RuntimeError("Live orders disabled.")

    raw = fetch_all_5m(UNIVERSE, limit=160)
    if "BTCUSDT" not in raw:
        print("[WARN] Failed to fetch BTCUSDT 5m klines.")
        return

    now_utc = datetime.now(timezone.utc)
    closed_5m = {s: df[df["close_dt"] <= now_utc] for s, df in raw.items()}
    if len(closed_5m["BTCUSDT"]) < 60:
        return

    eval_ts = closed_5m["BTCUSDT"].index[-1]
    eval_iso = eval_ts.isoformat()
    if state.get("last_processed_5m") == eval_iso:
        print(f"[INFO] 5m bar {eval_iso} already processed. Active: {len(state.get('active', {}))}/{MAX_SLOTS}")
        return

    active: Dict[str, dict] = state.get("active", {})

    for key in list(active.keys()):
        p = active[key]
        sym = p["sym"]
        if sym not in closed_5m or eval_ts not in closed_5m[sym].index:
            continue
        bar = closed_5m[sym].loc[eval_ts]
        op, hi, lo, cl = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

        hit = False
        reason = ""
        gross_r = 0.0
        exit_px = cl

        if p["dir"] == 1:
            if lo <= p["sl"]:
                hit, reason = True, "SL"
                exit_px = min(op, p["sl"]) if op < p["sl"] else p["sl"]
                gross_r = ((exit_px / p["entry"]) - 1.0) / p["stop_pct"]
            elif hi >= p["tp"]:
                hit, reason, exit_px, gross_r = True, "TP", p["tp"], TP_R
        else:
            if hi >= p["sl"]:
                hit, reason = True, "SL"
                exit_px = max(op, p["sl"]) if op > p["sl"] else p["sl"]
                gross_r = -((exit_px / p["entry"]) - 1.0) / p["stop_pct"]
            elif lo <= p["tp"]:
                hit, reason, exit_px, gross_r = True, "TP", p["tp"], TP_R

        if not hit:
            p["bars"] += 1
            if p["bars"] >= MAX_BARS_5M:
                next_bars = raw[sym][raw[sym].index > eval_ts]
                exit_px = float(next_bars.iloc[0]["open"]) if not next_bars.empty else cl
                gross_r = (p["dir"] * ((exit_px / p["entry"]) - 1.0)) / p["stop_pct"]
                hit, reason = True, "TIME"

        if hit:
            net_r = gross_r - (BASE_FRICTION / p["stop_pct"])
            active.pop(key)
            side = "LONG" if p["dir"] == 1 else "SHORT"
            append_history({
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "symbol": sym,
                "side": side,
                "entry": round(p["entry"], 6),
                "exit": round(exit_px, 6),
                "reason": reason,
                "net_r": round(net_r, 4),
            })
            send_telegram(
                f"PAPER 5M EXIT {sym} {side} | {reason} | Entry: {p['entry']:.4f} -> "
                f"Exit: {exit_px:.4f} | Net: {net_r:+.2f}R"
            )
            continue

        close_r = (p["dir"] * ((cl / p["entry"]) - 1.0)) / p["stop_pct"]
        if p.get("stage", 0) == 0 and close_r >= 1.0:
            p["stage"] = 1
            p["sl"] = p["entry"] * (1.0 + p["dir"] * p["stop_pct"] * 0.15)

    signals = evaluate_5m_signals(closed_5m, eval_ts)
    if signals and len(active) < MAX_SLOTS:
        signals.sort(key=lambda x: x["score"], reverse=True)
        active_syms = {p["sym"] for p in active.values()}
        for sig in signals:
            if len(active) >= MAX_SLOTS or sig["symbol"] in active_syms:
                continue
            next_bars = raw[sig["symbol"]][raw[sig["symbol"]].index > eval_ts]
            if next_bars.empty:
                continue
            opx = float(next_bars.iloc[0]["open"])
            if not np.isfinite(opx) or opx <= 0:
                continue
            spct = sig["stop_pct"]
            sl_px = opx * (1.0 - sig["direction"] * spct)
            tp_px = opx * (1.0 + sig["direction"] * spct * TP_R)
            active[sig["symbol"]] = {
                "sym": sig["symbol"],
                "dir": sig["direction"],
                "entry": opx,
                "stop_pct": spct,
                "sl": sl_px,
                "tp": tp_px,
                "stage": 0,
                "bars": 0,
                "opened_at": next_bars.index[0].isoformat(),
            }
            active_syms.add(sig["symbol"])
            side = "LONG" if sig["direction"] == 1 else "SHORT"
            send_telegram(
                f"PAPER 5M ENTRY {sig['symbol']} {side} @ {opx:.4f} | "
                f"SL: {sl_px:.4f} ({spct*100:.2f}%) | TP: {tp_px:.4f} ({TP_R}R) | "
                f"TBR: {sig['tbr']:.2f} | Slots: {len(active)}/{MAX_SLOTS}"
            )

    state["last_processed_5m"] = eval_iso
    state["active"] = active
    save_state(state)
    print(f"[OK] 5m bar {eval_iso} processed. Active: {len(active)}/{MAX_SLOTS}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance 5m Intraday Paper Scanner")
    parser.add_argument("--once", action="store_true", help="Run a single 5m check and exit")
    args = parser.parse_args()

    state
  
