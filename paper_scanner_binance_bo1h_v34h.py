#!/usr/bin/env python3
"""
Binance Paper-Trading Scanner (NO LIVE ORDERS)
Combined Portfolio: HIGH-R-BO 1H TRAIL-2.8R + SWEEP-ABSORB V3 4H (MAX_SLOTS = 4)
- Fetches 1H klines concurrently via ThreadPoolExecutor (8 workers)
- Tries data-api.binance.vision first, falling back to fapi.binance.com
- Supports single-pass execution (--once) for GitHub Actions / cron workflows
- Evaluates strictly closed bars (t-1) and enters at current bar Open (t)
- Persists active state in active_trades_bo1h_v34h.json & history in closed_trades_bo1h_v34h.csv
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

STATE_FILE = Path("active_trades_bo1h_v34h.json")
HISTORY_FILE = Path("closed_trades_bo1h_v34h.csv")

MAX_SLOTS = 4
MIN_STOP_PCT = 0.015
BASE_FRICTION = 0.0015
POLL_INTERVAL_SEC = 30
MAX_WORKERS = 8
MAX_ALERTED_KEYS = 1000

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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Bo1hV34hScanner/1.1)"}


def send_telegram_raw(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}")
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    for attempt in range(2):
        try:
            r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=8)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1.0 * (attempt + 1))


def send_telegram_once(state: dict, alert_key: str, msg: str) -> None:
    alerted = state.get("alerted_keys")
    if not isinstance(alerted, list):
        alerted = []
    if alert_key in alerted:
        return
    send_telegram_raw(msg)
    alerted.append(alert_key)
    state["alerted_keys"] = alerted[-MAX_ALERTED_KEYS:]


def fetch_klines_1h(symbol: str, limit: int = 260) -> Optional[pd.DataFrame]:
    params = {"symbol": symbol, "interval": "1h", "limit": limit}
    with requests.Session() as session:
        for base_url in KLINE_ENDPOINTS:
            for attempt in range(2):
                try:
                    resp = session.get(base_url, params=params, headers=HEADERS, timeout=6)
                    if resp.status_code != 200:
                        continue
                    raw = resp.json()
                    if not isinstance(raw, list) or len(raw) < 120:
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
                time.sleep(0.5 * (attempt + 1))
    return None


def fetch_all_klines_1h(symbols: List[str], limit: int = 260) -> Dict[str, pd.DataFrame]:
    fetched: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_klines_1h, sym, limit): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = fut.result()
                if df is not None and len(df) >= 120:
                    fetched[sym] = df
            except Exception:
                continue
    return fetched


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def resample_4h_closed(df_1h_closed: pd.DataFrame) -> pd.DataFrame:
    df_4h = df_1h_closed.resample("4h", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "taker_buy_volume": "sum",
    }).dropna()
    last_closed_ts = df_1h_closed.index[-1]
    if (last_closed_ts.hour + 1) % 4 != 0:
        df_4h = df_4h.iloc[:-1]
    return df_4h


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                active = data.get("active")
                alerted = data.get("alerted_keys")
                return {
                    "last_processed_1h": data.get("last_processed_1h"),
                    "active": active if isinstance(active, dict) else {},
                    "alerted_keys": alerted if isinstance(alerted, list) else [],
                }
        except Exception:
            pass
    return {"last_processed_1h": None, "active": {}, "alerted_keys": []}


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


def evaluate_signals(
    closed_1h: Dict[str, pd.DataFrame],
    eval_ts: pd.Timestamp,
) -> List[dict]:
    btc_1h = closed_1h["BTCUSDT"]
    btc_ema20 = btc_1h["close"].ewm(span=20, adjust=False).mean()
    btc_ema50 = btc_1h["close"].ewm(span=50, adjust=False).mean()
    btc_up_1h = bool((btc_1h["close"].iloc[-1] > btc_ema50.iloc[-1]) and (btc_ema20.iloc[-1] > btc_ema50.iloc[-1]))
    btc_dn_1h = bool((btc_1h["close"].iloc[-1] < btc_ema50.iloc[-1]) and (btc_ema20.iloc[-1] < btc_ema50.iloc[-1]))

    is_4h_boundary = ((eval_ts.hour + 1) % 4 == 0)
    btc_4h = resample_4h_closed(btc_1h) if is_4h_boundary else None
    btc_up_4h = btc_dn_4h = False
    if btc_4h is not None and len(btc_4h) >= 55:
        b4_ema50 = btc_4h["close"].ewm(span=50, adjust=False).mean()
        btc_up_4h = bool(btc_4h["close"].iloc[-1] > b4_ema50.iloc[-1])
        btc_dn_4h = bool(btc_4h["close"].iloc[-1] < b4_ema50.iloc[-1])

    signals: List[dict] = []

    for sym, df in closed_1h.items():
        if sym == "BTCUSDT" or eval_ts not in df.index or len(df) < 60:
            continue

        # 1) HIGH-R-BO 1H TRAIL-2.8R
        atr14 = calc_atr(df, 14)
        ema20 = df["close"].ewm(span=20, adjust=False).mean()
        ema50 = df["close"].ewm(span=50, adjust=False).mean()
        prior_hi = df["high"].rolling(24).max().shift(1)
        prior_lo = df["low"].rolling(24).min().shift(1)
        vol_rel = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)
        tbr = df["taker_buy_volume"] / df["volume"].replace(0, np.nan)

        row = df.iloc[-1]
        c_px, o_px = float(row["close"]), float(row["open"])
        a_px = float(atr14.iloc[-1])
        v_rel = float(vol_rel.iloc[-1])
        tbr_v = float(tbr.iloc[-1])

        if np.isfinite(a_px) and c_px > 0 and np.isfinite(v_rel) and np.isfinite(tbr_v):
            long_bo = (
                btc_up_1h and (ema20.iloc[-1] > ema50.iloc[-1])
                and (c_px > float(prior_hi.iloc[-1])) and (c_px > o_px)
                and (v_rel >= 1.60) and (tbr_v >= 0.58)
            )
            short_bo = (
                btc_dn_1h and (ema20.iloc[-1] < ema50.iloc[-1])
                and (c_px < float(prior_lo.iloc[-1])) and (c_px < o_px)
                and (v_rel >= 1.60) and (tbr_v <= 0.42)
            )
            if long_bo or short_bo:
                stop_pct = max(MIN_STOP_PCT, min(0.055, (1.8 * a_px) / c_px))
                signals.append({
                    "sleeve": "BO1H",
                    "symbol": sym,
                    "direction": 1 if long_bo else -1,
                    "score": v_rel,
                    "stop_pct": stop_pct,
                    "tp_r": 2.8,
                    "max_hours": 16,
                    "trail_step": True,
                })

        # 2) SWEEP-ABSORB V3 4H (evaluated on 4H close boundaries)
        if is_4h_boundary:
            df4 = resample_4h_closed(df)
            if len(df4) >= 30:
                atr4 = calc_atr(df4, 14)
                p4_hi = df4["high"].rolling(20).max().shift(1)
                p4_lo = df4["low"].rolling(20).min().shift(1)
                rng4 = (df4["high"] - df4["low"]).replace(0, np.nan)
                lw4 = (np.minimum(df4["open"], df4["close"]) - df4["low"]) / rng4
                uw4 = (df4["high"] - np.maximum(df4["open"], df4["close"])) / rng4
                tbr4 = df4["taker_buy_volume"] / df4["volume"].replace(0, np.nan)
                vrel4 = df4["volume"] / df4["volume"].rolling(20).mean().replace(0, np.nan)

                r4 = df4.iloc[-1]
                c4, a4 = float(r4["close"]), float(atr4.iloc[-1])
                vr4, tb4 = float(vrel4.iloc[-1]), float(tbr4.iloc[-1])
                if np.isfinite(a4) and c4 > 0 and np.isfinite(vr4) and np.isfinite(tb4):
                    long_v3 = (
                        btc_up_4h and (float(r4["low"]) < float(p4_lo.iloc[-1]))
                        and (c4 > float(p4_lo.iloc[-1])) and (float(lw4.iloc[-1]) >= 0.40)
                        and (tb4 <= 0.45) and (vr4 >= 1.30)
                    )
                    short_v3 = (
                        btc_dn_4h and (float(r4["high"]) > float(p4_hi.iloc[-1]))
                        and (c4 < float(p4_hi.iloc[-1])) and (float(uw4.iloc[-1]) >= 0.40)
                        and (tb4 >= 0.55) and (vr4 >= 1.30)
                    )
                    if long_v3 or short_v3:
                        stop_pct = max(MIN_STOP_PCT, min(0.055, (1.8 * a4) / c4))
                        signals.append({
                            "sleeve": "V3_4H",
                            "symbol": sym,
                            "direction": 1 if long_v3 else -1,
                            "score": vr4 + 10.0,
                            "stop_pct": stop_pct,
                            "tp_r": 2.2,
                            "max_hours": 36,
                            "trail_step": False,
                        })

    return signals


def process_bar_cycle(state: dict) -> bool:
    raw = fetch_all_klines_1h(UNIVERSE, limit=260)
    if "BTCUSDT" not in raw:
        print("[WARN] Failed to fetch BTCUSDT klines.")
        return False

    now_utc = datetime.now(timezone.utc)
    btc_df = raw["BTCUSDT"]
    closed_mask = btc_df["close_dt"] <= now_utc
    if closed_mask.sum() < 100:
        return False

    closed_1h = {s: df[df["close_dt"] <= now_utc] for s, df in raw.items()}
    eval_ts = closed_1h["BTCUSDT"].index[-1]
    eval_iso = eval_ts.isoformat()

    if state.get("last_processed_1h") == eval_iso:
        print(f"[INFO] Bar {eval_iso} already processed. Active slots: {len(state.get('active', {}))}/{MAX_SLOTS}")
        return False

    active: Dict[str, dict] = state.get("active", {})

    # 1. Manage open paper positions against the newly closed 1H bar (eval_ts)
    for key in list(active.keys()):
        p = active[key]
        sym = p["sym"]
        if sym not in raw or eval_ts not in closed_1h[sym].index:
            continue
        bar = closed_1h[sym].loc[eval_ts]
        op, hi, lo, cl = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

        hit = False
        exit_reason = ""
        gross_r = 0.0
        exit_px = cl

        if p["dir"] == 1:
            if lo <= p["sl"]:
                hit, exit_reason = True, "SL"
                exit_px = min(op, p["sl"]) if op < p["sl"] else p["sl"]
                gross_r = ((exit_px / p["entry"]) - 1.0) / p["stop_pct"]
            elif hi >= p["tp"]:
                hit, exit_reason, exit_px, gross_r = True, "TP", p["tp"], p["tp_r"]
        else:
            if hi >= p["sl"]:
                hit, exit_reason = True, "SL"
                exit_px = max(op, p["sl"]) if op > p["sl"] else p["sl"]
                gross_r = -((exit_px / p["entry"]) - 1.0) / p["stop_pct"]
            elif lo <= p["tp"]:
                hit, exit_reason, exit_px, gross_r = True, "TP", p["tp"], p["tp_r"]

        if not hit:
            p["hours"] += 1
            if p["hours"] >= p["max_hours"]:
                next_bars = raw[sym][raw[sym].index > eval_ts]
                exit_px = float(next_bars.iloc[0]["open"]) if not next_bars.empty else cl
                gross_r = (p["dir"] * ((exit_px / p["entry"]) - 1.0)) / p["stop_pct"]
                hit, exit_reason = True, "TIME"

        if hit:
            net_r = gross_r - (BASE_FRICTION / p["stop_pct"])
            active.pop(key)
            side = "LONG" if p["dir"] == 1 else "SHORT"
            append_history({
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "sleeve": p["sleeve"],
                "symbol": sym,
                "side": side,
                "entry": round(p["entry"], 6),
                "exit": round(exit_px, 6),
                "reason": exit_reason,
                "net_r": round(net_r, 4),
            })
            alert_key = f"BO1H_V34H:{p['sleeve']}:{sym}:{side}:EXIT:{eval_iso}:{round(exit_px, 6)}"
            send_telegram_once(
                state,
                alert_key,
                f"PAPER EXIT [{p['sleeve']}] {sym} {side} | Reason: {exit_reason} | "
                f"Entry: {p['entry']:.4f} -> Exit: {exit_px:.4f} | Net: {net_r:+.2f}R",
            )
            continue

        close_r = (p["dir"] * ((cl / p["entry"]) - 1.0)) / p["stop_pct"]
        if not p["trail_step"]:
            if p["stage"] == 0 and close_r >= 1.0:
                p["stage"] = 1
                p["sl"] = p["entry"] * (1.0 + p["dir"] * p["stop_pct"] * 0.15)
        else:
            if p["stage"] < 2 and close_r >= 2.0:
                p["stage"] = 2
                p["sl"] = p["entry"] * (1.0 + p["dir"] * p["stop_pct"] * 1.25)
            elif p["stage"] < 1 and close_r >= 1.2:
                p["stage"] = 1
                p["sl"] = p["entry"] * (1.0 + p["dir"] * p["stop_pct"] * 0.50)

    # 2. Generate new signals on closed bar (eval_ts) and enter at current open bar (t Open)
    signals = evaluate_signals(closed_1h, eval_ts)
    if signals and len(active) < MAX_SLOTS:
        signals.sort(key=lambda s: s["score"], reverse=True)
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
            spct = max(MIN_STOP_PCT, sig["stop_pct"])
            sl_px = opx * (1.0 - sig["direction"] * spct)
            tp_px = opx * (1.0 + sig["direction"] * spct * sig["tp_r"])
            key = f"{sig['sleeve']}:{sig['symbol']}"
            active[key] = {
                "sleeve": sig["sleeve"],
                "sym": sig["symbol"],
                "dir": sig["direction"],
                "entry": opx,
                "stop_pct": spct,
                "sl": sl_px,
                "tp": tp_px,
                "tp_r": sig["tp_r"],
                "stage": 0,
                "hours": 0,
                "max_hours": sig["max_hours"],
                "trail_step": sig["trail_step"],
                "opened_at": next_bars.index[0].isoformat(),
            }
            active_syms.add(sig["symbol"])
            side = "LONG" if sig["direction"] == 1 else "SHORT"
            alert_key = f"BO1H_V34H:{sig['sleeve']}:{sig['symbol']}:{side}:ENTRY:{eval_iso}:{round(opx, 6)}"
            send_telegram_once(
                state,
                alert_key,
                f"PAPER ENTRY [{sig['sleeve']}] {sig['symbol']} {side} @ {opx:.4f} | "
                f"SL: {sl_px:.4f} ({spct*100:.2f}%) | TP: {tp_px:.4f} ({sig['tp_r']}R) | "
                f"Slots: {len(active)}/{MAX_SLOTS}",
            )

    state["last_processed_1h"] = eval_iso
    state["active"] = active
    save_state(state)
    print(f"[OK] Processed bar {eval_iso}. Active slots: {len(active)}/{MAX_SLOTS}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance Paper Scanner (BO-1H-TRAIL2.8R + V3-4H)")
    parser.add_argument("--once", action="store_true", help="Run a single bar check and exit (for YAML/cron workflows)")
    args = parser.parse_args()

    state = load_state()
    if args.once:
        process_bar_cycle(state)
        sys.exit(0)

    send_telegram_raw(
        f"Started Binance Paper Scanner (BO-1H-TRAIL2.8R + V3-4H | MAX_SLOTS={MAX_SLOTS} | "
        f"Active: {len(state.get('active', {}))})"
    )
    while True:
        try:
            process_bar_cycle(state)
        except Exception as exc:
            print(f"[WARN] Cycle error: {exc}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
    
