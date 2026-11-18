import json
import os
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

TOKEN = os.getenv("TG_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
FAPI_BASES = ["https://fapi.binance.com", "https://fapi1.binance.com"]
VISION_MIRROR = "https://data-api.binance.vision"
STATE_FILE = "active_trades_4h_res_414.json"
TAG = "[4H_RES_414 SNIPER]"

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "UNIUSDT", "AAVEUSDT", "BNBUSDT", "DOTUSDT",
    "BCHUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "INJUSDT", "TIAUSDT", "FILUSDT", "ETCUSDT",
]

MAX_SLOTS = 3
MAX_HOLD_BARS = 12  # 48 hours
BE_TRIGGER_R = 0.80


def get_json(path, params=None):
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


def send_tg(msg):
    if not TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": f"{TAG}\n{msg}"}, timeout=10)
    except Exception:
        pass


def fetch_4h(sym, limit=220, srv_ms=None):
    if srv_ms is None:
        srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    raw = get_json("/fapi/v1/klines", {"symbol": sym, "interval": "4h", "limit": limit})
    rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw if int(r[6]) < srv_ms]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"]).sort_values("ts").drop_duplicates("ts").set_index("ts")
    df["e50"] = df["c"].ewm(span=50, adjust=False).mean()
    df["vrel60"] = df["v"] / df["v"].rolling(60).mean().replace(0, np.nan)
    return df


def run_scanner():
    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    st = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"active": [], "closed": [], "last_ts": 0}
    btc = fetch_4h("BTCUSDT", 220, srv_ms)
    if len(btc) < 190:
        return

    lts = int(btc.index[-1])
    still = []

    for tr in st.get("active", []):
        df = fetch_4h(tr["sym"], 30, srv_ms)
        if len(df) == 0 or int(df.index[-1]) <= tr.get("last_ts", tr["entry_ts"]):
            still.append(tr)
            continue

        tr["last_ts"] = int(df.index[-1])
        tr["bars"] = tr.get("bars", 0) + 1
        r = df.iloc[-1]
        ep, sl, tp, sp = tr["entry"], tr["sl"], tr["tp"], tr["stop_pct"]
        sgn = 1.0 if tr["side"] == "LONG" else -1.0
        exit_px, reason = None, None

        if tr["side"] == "LONG":
            if r["l"] <= sl:
                exit_px, reason = sl, ("BE_STOP" if tr.get("be") else "STOP_LOSS")
            elif r["h"] >= tp:
                exit_px, reason = tp, "TAKE_PROFIT"
            elif not tr.get("be") and (r["h"] - ep) / ep >= BE_TRIGGER_R * sp:
                tr["sl"], tr["be"] = ep, True
        else:
            if r["h"] >= sl:
                exit_px, reason = sl, ("BE_STOP" if tr.get("be") else "STOP_LOSS")
            elif r["l"] <= tp:
                exit_px, reason = tp, "TAKE_PROFIT"
            elif not tr.get("be") and (ep - r["l"]) / ep >= BE_TRIGGER_R * sp:
                tr["sl"], tr["be"] = ep, True

        if exit_px is None and tr["bars"] >= MAX_HOLD_BARS:
            exit_px, reason = float(r["c"]), "TIME_EXIT_48H"

        if exit_px is not None:
            nr = round(((sgn * (exit_px - ep) / ep) - 0.0015) / sp, 3)
            tr.update({"exit_ts": lts, "exit_price": exit_px, "net_r": nr, "exit_reason": reason})
            st.setdefault("closed", []).append(tr)
            send_tg(f"CLOSED [{reason}]: {tr['sym']} {tr['side']} @ {exit_px:.6g} | Net: {nr:+.2f}R")
        else:
            still.append(tr)

    st["active"] = still

    if lts > st.get("last_ts", 0) and len(st["active"]) < MAX_SLOTS:
        btc_ret = np.log(btc["c"] / btc["c"].shift(1))
        btc_var = btc_ret.rolling(180, min_periods=120).var()
        btc_bull = bool(btc["c"].iloc[-1] > btc["e50"].iloc[-1])
        btc_imp = abs(float(btc["c"].iloc[-1] / btc["c"].iloc[-7] - 1.0))

        if btc_imp <= 0.03:
            cands = []
            for sym in COINS[1:]:
                if any(x["sym"] == sym for x in st["active"]):
                    continue
                df = fetch_4h(sym, 220, srv_ms)
                if len(df) < 190 or int(df.index[-1]) != lts:
                    continue
                ret = np.log(df["c"] / df["c"].shift(1))
                bret = btc_ret.reindex(df.index).fillna(0.0)
                beta = (ret.rolling(180, min_periods=120).cov(bret) / btc_var.reindex(df.index)).clip(-3.0, 3.0)
                res = ret - beta * bret
                std = float(res.rolling(180, min_periods=120).std().iloc[-1])
                vrel = float(df["vrel60"].iloc[-1])
                if not np.isfinite(std) or std <= 0 or not np.isfinite(vrel) or vrel > 1.4:
                    continue
                z6 = float(res.iloc[-6:].sum() / (std * np.sqrt(6.0)))
                side = "LONG" if (z6 <= -3.2 and btc_bull) else ("SHORT" if (z6 >= 2.8 and not btc_bull) else None)
                if side:
                    cands.append((abs(z6), sym, side, float(df["c"].iloc[-1]), std, z6))

            for _, sym, side, ep, std, z6 in sorted(cands, reverse=True)[:MAX_SLOTS - len(st["active"])]:
                sp = max(0.015, 1.55 * std * np.sqrt(6.0))
                sl = round(ep * (1.0 - sp) if side == "LONG" else ep * (1.0 + sp), 6)
                tp = round(ep * (1.0 + 1.50 * sp) if side == "LONG" else ep * (1.0 - 1.50 * sp), 6)
                st["active"].append({
                    "sym": sym, "inst": sym, "side": side, "entry": ep,
                    "sl": sl, "tp": tp, "stop_pct": round(sp, 5),
                    "bars": 0, "be": False, "entry_ts": lts, "last_ts": lts, "z6": round(z6, 2),
                })
                send_tg(f"NEW [{side}]: {sym} @ {ep:.6g} | SL={sl} | TP={tp} | Z6={z6:+.2f}")

    st["last_ts"] = lts
    json.dump(st, open(STATE_FILE, "w"), indent=2)
    print(f"{TAG} Done. Active={len(st['active'])}/{MAX_SLOTS}")


if __name__ == "__main__":
    run_scanner()
    
