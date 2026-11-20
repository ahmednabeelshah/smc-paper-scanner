#!/usr/bin/env python3
"""
Proven Core 4H Paper Scanner (NO LIVE ORDERS)
Combines the 3 validated capital-preservation sleeves:
  1. UPSIDE_VOL_DOM  (4H Long  - Bull Market Asymmetric Candle Continuation)
  2. SHRINK_PULLBACK (4H Long  - Bull Market Decaying-Dip Breakout)
  3. FAILED_RALLY    (4H Short - Bear Market 20D SMA Resistance Rejection)

- Uses fapi.binance.com with HTTP 451 fallback to data-api.binance.vision
- Checks all active_trades_*.json files so it never double-bets on an open coin
- Includes a 1H Bitcoin 3% shock guard against sudden breaking-news spikes
- Persists state to active_trades_proven_core.json & closed_trades_proven_core.csv
  for automatic pickup by master_trade_logger.py
"""
import csv
import glob
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set

import numpy as np
import pandas as pd
import requests

LIVE_ORDERS_ENABLED = False

TOKEN = (
    os.getenv("TG_TOKEN")
    or os.getenv("TG_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or ""
).strip()
CHAT_ID = (
    os.getenv("TG_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or ""
).strip()

FAPI_BASES = ["https://fapi.binance.com", "https://fapi1.binance.com"]
VISION_MIRROR = "https://data-api.binance.vision"

STATE_FILE = Path("active_trades_proven_core.json")
HISTORY_FILE = Path("closed_trades_proven_core.csv")
TAG = "[PROVEN-CORE 4H]"

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "UNIUSDT", "AAVEUSDT", "BNBUSDT", "DOTUSDT",
    "BCHUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "INJUSDT", "TIAUSDT", "FILUSDT", "ETCUSDT",
]

MAX_BULL_SLOTS = 2
MAX_BEAR_SLOTS = 2
MIN_STOP_PCT = 0.015
MAX_STOP_PCT = 0.100
FEE_ROUND_TRIP = 0.0016  # 8 bps/side baseline


def get_json(path: str, params: dict = None):
    for base in FAPI_BASES:
        try:
            r = requests.get(base + path, params=params, timeout=10)
            if r.status_code == 451:
                break
            r.raise_for_status()
            return r.json()
        except Exception:
            continue

    v_path = path.replace("/fapi/v1/time", "/api/v3/time").replace("/fapi/v1/klines", "/api/v3/klines")
    for a in range(3):
        try:
            r = requests.get(VISION_MIRROR + v_path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == 2:
                raise
            time.sleep(2 ** a)


def send_tg(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {TAG} {msg}")
    if not TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": f"{TAG}\n{msg}"},
            timeout=10,
        )
    except Exception:
        pass


def fetch_klines_df(sym: str, interval: str, limit: int, srv_ms: int) -> pd.DataFrame:
    raw = get_json("/fapi/v1/klines", {"symbol": sym, "interval": interval, "limit": limit})
    rows = [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]))
        for r in raw
        if int(r[6]) < srv_ms
    ]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "qvol"])
    return df.sort_values("ts").drop_duplicates("ts").set_index("ts")


def calc_atr14(df: pd.DataFrame) -> pd.Series:
    pc = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=14).mean()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("active", [])
                data.setdefault("closed", [])
                data.setdefault("last_ts", 0)
                return data
        except Exception:
            pass
    return {"active": [], "closed": [], "last_ts": 0}


def save_state(st: dict) -> None:
    st["closed"] = st.get("closed", [])[-200:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_closed_csv(row: dict) -> None:
    write_header = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def get_all_open_symbols(own_active: List[dict]) -> Set[str]:
    """Reads all active_trades_*.json files in repo so no coin is double-bet."""
    busy = {str(x.get("sym") or x.get("symbol") or "").upper() for x in own_active}
    for path in glob.glob("active_trades_*.json"):
        if path == str(STATE_FILE):
            continue
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sub = data.get("active") or data.get("trades") or []
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, dict) and item.get("status", "ACTIVE") in ("ACTIVE", "PENDING", "OPEN"):
                            busy.add(str(item.get("sym") or item.get("symbol") or "").upper())
                elif isinstance(sub, dict):
                    for item in sub.values():
                        if isinstance(item, dict):
                            busy.add(str(item.get("sym") or item.get("symbol") or "").upper())
        except Exception:
            continue
    busy.discard("")
    return busy


def find_2bar_swings(df4: pd.DataFrame):
    hi = df4["h"].to_numpy()
    lo = df4["l"].to_numpy()
    n = len(df4)
    sh_idx, sl_idx = [], []
    # Strictly confirmed swings: s + 2 <= n - 1 (zero lookahead)
    for s in range(2, n - 2):
        if hi[s] > hi[s - 1] and hi[s] > hi[s - 2] and hi[s] > hi[s + 1] and hi[s] > hi[s + 2]:
            sh_idx.append(s)
        if lo[s] < lo[s - 1] and lo[s] < lo[s - 2] and lo[s] < lo[s + 1] and lo[s] < lo[s + 2]:
            sl_idx.append(s)
    return sh_idx, sl_idx


def run_scanner() -> None:
    if LIVE_ORDERS_ENABLED:
        raise RuntimeError("Live orders are disabled.")

    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    st = load_state()

    btc_4h = fetch_klines_df("BTCUSDT", "4h", 140, srv_ms)
    btc_1d = fetch_klines_df("BTCUSDT", "1d", 70, srv_ms)
    btc_1h = fetch_klines_df("BTCUSDT", "1h", 10, srv_ms)
    if len(btc_4h) < 100 or len(btc_1d) < 55 or len(btc_1h) < 3:
        print(f"{TAG} Insufficient BTC history.")
        return

    lts = int(btc_4h.index[-1])
    still = []

    # 1. Manage Open Positions on Closed 4H Bars
    for tr in st.get("active", []):
        df4 = fetch_klines_df(tr["sym"], "4h", 30, srv_ms)
        if len(df4) == 0 or int(df4.index[-1]) <= tr.get("last_ts", tr["entry_ts"]):
            still.append(tr)
            continue

        tr["last_ts"] = int(df4.index[-1])
        tr["bars"] = tr.get("bars", 0) + 1
        r = df4.iloc[-1]
        ep, sl, tp, sp = tr["entry"], tr["sl"], tr["tp"], tr["stop_pct"]
        sgn = 1.0 if tr["side"] == "LONG" else -1.0
        exit_px, reason = None, None

        if tr["side"] == "LONG":
            if r["l"] <= sl:
                exit_px, reason = sl, "STOP_LOSS"
            elif r["h"] >= tp:
                exit_px, reason = tp, "TAKE_PROFIT"
        else:
            if r["h"] >= sl:
                exit_px, reason = sl, "STOP_LOSS"
            elif r["l"] <= tp:
                exit_px, reason = tp, "TAKE_PROFIT"

        if exit_px is None and tr["bars"] >= tr["max_bars"]:
            exit_px, reason = float(r["c"]), f"TIME_EXIT_{tr['max_bars'] * 4}H"

        if exit_px is not None:
            nr = round(((sgn * (exit_px - ep) / ep) - FEE_ROUND_TRIP) / sp, 3)
            now_iso = datetime.now(timezone.utc).isoformat()
            tr.update({"exit_ts": lts, "exit_time": now_iso, "exit_price": exit_px, "net_r": nr, "exit_reason": reason})
            st["closed"].append(tr)
            append_closed_csv({
                "exit_time": now_iso,
                "model": tr["model"],
                "symbol": tr["sym"],
                "side": tr["side"],
                "entry": ep,
                "exit": round(exit_px, 6),
                "stop_pct": sp,
                "reason": reason,
                "net_r": nr,
            })
            send_tg(
                f"CLOSED [{tr['model']}] {tr['sym']} {tr['side']} | {reason}\n"
                f"Entry: {ep:.6g} -> Exit: {exit_px:.6g} | Net: {nr:+.2f}R"
            )
        else:
            still.append(tr)

    st["active"] = still

    # 2. Check Market Gates & Scan New 4H Setups
    if lts > st.get("last_ts", 0):
        # Built-in 1H Bitcoin 3% News/Macro Shock Guard
        btc_1h_move = abs(float(btc_1h["c"].iloc[-1] / btc_1h["o"].iloc[-1] - 1.0))
        if btc_1h_move >= 0.03:
            st["last_ts"] = lts
            save_state(st)
            print(f"{TAG} Paused: BTC 1H shock move {btc_1h_move*100:.2f}% >= 3.0%.")
            return

        btc_sma50 = btc_1d["c"].rolling(50).mean()
        bull_gate = bool((btc_1d["c"].iloc[-1] > btc_sma50.iloc[-1]) and (btc_sma50.iloc[-1] > btc_sma50.iloc[-11]))
        bear_gate = bool((btc_1d["c"].iloc[-1] < btc_sma50.iloc[-1]) and (btc_sma50.iloc[-1] < btc_sma50.iloc[-11]))

        busy_syms = get_all_open_symbols(st["active"])
        cands = []  # (priority, vol_rank, model, sym, side, ep, sp, rr, max_bars)

        for sym in COINS[1:]:
            if sym in busy_syms:
                continue
            df4 = fetch_klines_df(sym, "4h", 140, srv_ms)
            df1d = fetch_klines_df(sym, "1d", 45, srv_ms)
            if len(df4) < 100 or len(df1d) < 35 or int(df4.index[-1]) != lts:
                continue

            qvol_24h = float(df1d["qvol"].iloc[-1])
            if qvol_24h < 50_000_000:
                continue

            atr14 = calc_atr14(df4)
            a14 = float(atr14.iloc[-1])
            ep = float(df4["c"].iloc[-1])
            if not np.isfinite(a14) or a14 <= 0 or ep <= 0:
                continue

            c1d = float(df1d["c"].iloc[-1])
            sma20_d = float(df1d["c"].rolling(20).mean().iloc[-1])
            sma20_d_prev5 = float(df1d["c"].rolling(20).mean().iloc[-6])
            sma30_d = float(df1d["c"].rolling(30).mean().iloc[-1])

            # Sleeve 1 (Priority 1): SHRINKING PULLBACKS (Bull Long)
            if bull_gate and c1d > sma30_d:
                sh_i, sl_i = find_2bar_swings(df4.iloc[-92:])
                if len(sh_i) >= 3 and len(sl_i) >= 3:
                    sub = df4.iloc[-92:]
                    sh1, sh2, sh3 = float(sub["h"].iloc[sh_i[-3]]), float(sub["h"].iloc[sh_i[-2]]), float(sub["h"].iloc[sh_i[-1]])
                    sl1, sl2, sl3 = float(sub["l"].iloc[sl_i[-3]]), float(sub["l"].iloc[sl_i[-2]]), float(sub["l"].iloc[sl_i[-1]])
                    d1, d2, d3 = (sh1 - sl1) / sh1, (sh2 - sl2) / sh2, (sh3 - sl3) / sh3
                    v_avg20 = float(df4["qvol"].iloc[-21:-1].mean())
                    if d1 > 0 and 0 < d2 <= 0.60 * d1 and 0 < d3 <= 0.60 * d2 and d3 < 0.08:
                        if ep > sh3 and float(df4["c"].iloc[-2]) <= sh3 and float(df4["qvol"].iloc[-1]) > v_avg20:
                            sp = max(MIN_STOP_PCT, (ep - sl3) / ep)
                            if sp <= MAX_STOP_PCT:
                                cands.append((1, -qvol_24h, "SHRINK_PULLBACK", sym, "LONG", ep, sp, 2.85, 30))

            # Sleeve 2 (Priority 2): UPSIDE VOLATILITY DOMINANCE (Bull Long)
            if bull_gate:
                tail30 = df4.iloc[-30:]
                g_bodies = (tail30["c"] - tail30["o"])[tail30["c"] > tail30["o"]]
                r_bodies = (tail30["o"] - tail30["c"])[tail30["c"] < tail30["o"]]
                if len(g_bodies) >= 5 and len(r_bodies) >= 5 and float(r_bodies.mean()) > 0:
                    if float(g_bodies.mean()) >= 1.5 * float(r_bodies.mean()):
                        if float(df4["c"].iloc[-1]) > float(df4["o"].iloc[-1]) and float(df4["c"].iloc[-2]) < float(df4["o"].iloc[-2]):
                            sp = max(MIN_STOP_PCT, (2.0 * a14) / ep)
                            if sp <= 0.08:
                                cands.append((2, -qvol_24h, "UPSIDE_VOL_DOM", sym, "LONG", ep, sp, 2.00, 12))

            # Sleeve 3 (Priority 3): FAILED RALLY INTO RESISTANCE (Bear Short)
            if bear_gate and c1d < sma20_d and sma20_d < sma20_d_prev5:
                prev3 = df4.iloc[-4:-1]
                if bool((prev3["c"] > prev3["o"]).all()) and abs(float(df4["c"].iloc[-2]) - sma20_d) / sma20_d <= 0.015:
                    if float(df4["c"].iloc[-1]) < float(df4["l"].iloc[-2]):
                        rally_hi = float(df4.iloc[-4:]["h"].max())
                        sp = max(MIN_STOP_PCT, (rally_hi - ep) / ep)
                        if sp <= 0.08:
                            cands.append((3, -qvol_24h, "FAILED_RALLY", sym, "SHORT", ep, sp, 2.00, 12))

        cands.sort(key=lambda x: (x[0], x[1]))
        now_iso = datetime.now(timezone.utc).isoformat()
        for _, _, model, sym, side, ep, sp, rr, max_bars in cands:
            if sym in busy_syms:
                continue
            bull_open = sum(1 for x in st["active"] if x["side"] == "LONG")
            bear_open = sum(1 for x in st["active"] if x["side"] == "SHORT")
            if side == "LONG" and bull_open >= MAX_BULL_SLOTS:
                continue
            if side == "SHORT" and bear_open >= MAX_BEAR_SLOTS:
                continue

            sgn = 1.0 if side == "LONG" else -1.0
            sl = round(ep * (1.0 - sgn * sp), 6)
            tp = round(ep * (1.0 + sgn * rr * sp), 6)
            busy_syms.add(sym)
            st["active"].append({
                "model": model,
                "sym": sym,
                "inst": sym,
                "side": side,
                "entry": round(ep, 6),
                "sl": sl,
                "tp": tp,
                "stop_pct": round(sp, 5),
                "tp_r": rr,
                "bars": 0,
                "max_bars": max_bars,
                "opened_at": now_iso,
                "entry_ts": lts,
                "last_ts": lts,
            })
            send_tg(
                f"NEW [{model}] {side}: {sym} @ {ep:.6g}\n"
                f"SL: {sl} ({sp*100:.2f}%) | TP: {tp} ({rr:.2f}R) | Hold: {max_bars * 4}h"
            )

    st["last_ts"] = lts
    save_state(st)
    print(f"{TAG} Scan complete. Active={len(st['active'])}")


if __name__ == "__main__":
    run_scanner()
      
