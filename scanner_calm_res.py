import os
import json
import time
import requests
import numpy as np
import pandas as pd

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID", "")

FAPI_BASES = ["https://fapi.binance.com", "https://fapi1.binance.com"]
VISION_MIRROR = "https://data-api.binance.vision"

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT",
    "BCHUSDT", "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "NEARUSDT", "INJUSDT", "TIAUSDT", "ETCUSDT", "BNBUSDT", "AAVEUSDT",
]

SF = "active_trades_calm_res.json"
TAG = "[CALM-RES 1D]"
MAX_SLOTS = 3
MAX_HOLD_DAYS = 5


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
    for att in range(3):
        try:
            r = requests.get(VISION_MIRROR + v_path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if att == 2:
                raise
            time.sleep(2 ** att)


def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print("TG ERR:", e)


def load_st(fn):
    return json.load(open(fn)) if os.path.exists(fn) else {"active": [], "closed": []}


def save_st(fn, st):
    json.dump(st, open(fn, "w"), indent=2)


def fetch_1d(sym, limit=120, srv_ms=None):
    if srv_ms is None:
        srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    raw = get_json("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": limit})
    rows = [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        for r in raw
        if int(r[6]) < srv_ms
    ]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"]).sort_values("ts").drop_duplicates("ts").set_index("ts")
    tr = np.maximum(df["h"] - df["l"], np.maximum(abs(df["h"] - df["c"].shift(1)), abs(df["l"] - df["c"].shift(1))))
    df["atr"] = tr.rolling(14).mean()
    df["atr50"] = tr.rolling(50).mean()
    df["acc"] = (df["atr"] / df["atr50"].replace(0, np.nan)).shift(1)
    df["ret"] = df["c"].pct_change()
    return df


def main():
    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    st = load_st(SF)
    btc = fetch_1d("BTCUSDT", 120, srv_ms)
    if len(btc) < 70:
        return

    lts = int(btc.index[-1])
    bc = float(btc["c"].iloc[-1])
    still = []

    # 1. Manage Active Beta-Hedged Pairs
    for p in st["active"]:
        df = fetch_1d(p["sym"], 20, srv_ms)
        if len(df) == 0 or int(df.index[-1]) <= p.get("last_ts", p["entry_ts"]):
            still.append(p)
            continue

        p["last_ts"] = int(df.index[-1])
        p["days"] = p.get("days", 0) + 1
        cp = float(df["c"].iloc[-1])
        sgn = 1.0 if p["side"] == "LONG" else -1.0
        sp = sgn * (cp - p["entry"]) / p["entry"] - sgn * p["beta"] * (bc - p["b_entry"]) / p["b_entry"]
        p["cum_pnl"] = round(sp, 5)

        if sp <= -p["stop_pct"] or sp >= 0.80 * p["stop_pct"] or p["days"] >= MAX_HOLD_DAYS:
            nr = round((-1.0 - 0.0015 * (1.0 + p["beta"]) / p["stop_pct"]) if sp <= -p["stop_pct"] else (sp / p["stop_pct"] - 0.05), 2)
            p.update({"exit_ts": lts, "exit_price": cp, "net_r": nr})
            st["closed"].append(p)
            send_tg(f"{TAG} EXIT {p['side']} {p['sym']} @ {cp:.4f} | {nr:+.2f}R")
            continue
        still.append(p)

    st["active"] = still

    # 2. Scan New Calm-Regime Residual Extremes (Strict .shift(1) OOS Parameters)
    if len(st["active"]) < MAX_SLOTS:
        cands = []
        for sym in COINS[1:]:
            if any(x["sym"] == sym for x in st["active"]):
                continue
            df = fetch_1d(sym, 120, srv_ms)
            if len(df) < 70 or int(df.index[-1]) != lts:
                continue

            br = btc["ret"].reindex(df.index)
            bt = (df["ret"].rolling(60).cov(br) / br.rolling(60).var()).shift(1).clip(0.2, 3.0)
            res1 = df["ret"] - bt * br
            rv1 = res1.rolling(20).std().shift(1)

            z1 = float((res1 / (rv1 + 1e-12)).iloc[-1])
            rv = float(rv1.iloc[-1])
            bv = float(bt.iloc[-1])
            acc = float(df["acc"].iloc[-1])

            if np.isfinite(z1) and np.isfinite(rv) and np.isfinite(bv) and np.isfinite(acc):
                if abs(z1) > 2.00 and acc < 1.20:
                    sp = max(1.25 * rv, 0.015)
                    cands.append((abs(z1), "SHORT" if z1 > 0 else "LONG", sym, float(df["c"].iloc[-1]), bv, sp))

        for za, side, sym, cp, bv, sp in sorted(cands, reverse=True)[:MAX_SLOTS - len(st["active"])]:
            st["active"].append({
                "sym": sym,
                "inst": sym,
                "side": side,
                "entry": cp,
                "b_entry": bc,
                "beta": round(bv, 4),
                "stop_pct": round(sp, 5),
                "days": 0,
                "cum_pnl": 0.0,
                "entry_ts": lts,
                "last_ts": lts,
            })
            send_tg(f"{TAG} NEW {side} {sym} @ {cp:.4f} | Stop: {sp*100:.2f}% | Beta={bv:.2f} | |Z1|={za:.2f}")

    save_st(SF, st)
    print(f"{TAG} Scan complete. Active={len(st['active'])}/{MAX_SLOTS}")


if __name__ == "__main__":
    main()
