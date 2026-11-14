import os, json, time, requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone

BASE = "https://okx.com"
STATE_FILE = "active_trades_hvol_res.json"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

COINS = [
    "BTC-USDT-SWAP","ETH-USDT-SWAP","SOL-USDT-SWAP","XRP-USDT-SWAP","DOGE-USDT-SWAP",
    "ADA-USDT-SWAP","AVAX-USDT-SWAP","LINK-USDT-SWAP","UNI-USDT-SWAP","AAVE-USDT-SWAP",
    "BNB-USDT-SWAP","DOT-USDT-SWAP","BCH-USDT-SWAP","LTC-USDT-SWAP","ATOM-USDT-SWAP",
    "NEAR-USDT-SWAP","APT-USDT-SWAP","ARB-USDT-SWAP","OP-USDT-SWAP","SUI-USDT-SWAP",
    "INJ-USDT-SWAP","TIA-USDT-SWAP","FIL-USDT-SWAP","ETC-USDT-SWAP","TRX-USDT-SWAP",
    "TON-USDT-SWAP","WLD-USDT-SWAP","RENDER-USDT-SWAP"
]

def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://telegram.org{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception:
        pass

def get_closed_1d(inst, limit=100):
    resp = requests.get(
        f"{BASE}/api/v5/market/candles",
        params={"instId": inst, "bar": "1Dutc", "limit": str(limit)},
        timeout=15
    ).json().get("data", [])
    rows = []
    for x in resp:
        if x[8] == "1":
            rows.append((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])))
    df = pd.DataFrame(rows, columns=["ts","o","h","l","c","vq"]).sort_values("ts").drop_duplicates("ts").set_index("ts")
    tr = np.maximum(df["h"] - df["l"], np.maximum(abs(df["h"] - df["c"].shift(1)), abs(df["l"] - df["c"].shift(1))))
    df["atr"] = tr.rolling(14).mean()
    df["e50"] = df["c"].ewm(span=50, adjust=False).mean()
    df["ret"] = df["c"].pct_change()
    df["ret2"] = df["c"] / df["c"].shift(2) - 1.0
    df["vol_rel1"] = df["vq"] / df["vq"].rolling(20).mean().replace(0, np.nan)
    return df

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"active": [], "closed": [], "last_bar_ts": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def run_scan():
    state = load_state()
    btc = get_closed_1d("BTC-USDT-SWAP", 100)
    latest_ts = int(btc.index[-1])
    if latest_ts <= state.get("last_bar_ts", 0):
        print(f"[HVOL_RES] Bar {latest_ts} already processed.")
        return

    bull = bool(btc["c"].iloc[-1] > btc["e50"].iloc[-1])
    btc_ret = btc["ret"]
    btc_ret2 = float(btc["ret2"].iloc[-1])
    still, cands, events = [], [], []

    for c in COINS[1:]:
        df = get_closed_1d(c, 100)
        if latest_ts not in df.index:
            continue
        r = df.loc[latest_ts]
        pos = next((p for p in state["active"] if p["inst"] == c), None)

        if pos:
            if pos["side"] == 1:
                if r["l"] <= pos["sl"]:
                    net_r = ((pos["sl"] - pos["entry"]) / pos["entry"] - 0.0015) / (pos["risk"] / pos["entry"])
                    state["closed"].append({**pos, "exit_ts": latest_ts, "net_r": round(float(net_r), 4)})
                    events.append(f"EXIT LONG {c} | {net_r:+.2f}R")
                    continue
                pos["best"] = max(pos["best"], float(r["h"]))
                pos["sl"] = max(pos["sl"], pos["best"] - 2.5 * float(r["atr"]))
            else:
                if r["h"] >= pos["sl"]:
                    net_r = ((pos["entry"] - pos["sl"]) / pos["entry"] - 0.0015) / (pos["risk"] / pos["entry"])
                    state["closed"].append({**pos, "exit_ts": latest_ts, "net_r": round(float(net_r), 4)})
                    events.append(f"EXIT SHORT {c} | {net_r:+.2f}R")
                    continue
                pos["best"] = min(pos["best"], float(r["l"]))
                pos["sl"] = min(pos["sl"], pos["best"] + 2.5 * float(r["atr"]))
            still.append(pos)
            continue

        aligned_ret = df["ret"].reindex(btc.index)
        cov = aligned_ret.rolling(60).cov(btc_ret).iloc[-2]
        var = btc_ret.rolling(60).var().iloc[-2]
        if pd.isna(cov) or pd.isna(var) or var == 0:
            continue
        beta = float(np.clip(cov / var, 0.2, 3.0))
        rvol1 = float((aligned_ret - beta * btc_ret).rolling(60).std().iloc[-2])
        if pd.isna(rvol1) or rvol1 == 0 or pd.isna(r["atr"]) or (2.5 * r["atr"] / r["c"]) < 0.015:
            continue

        z2 = (float(r["ret2"]) - beta * btc_ret2) / (rvol1 * np.sqrt(2))
        if bull and z2 > 2.25 and r["vol_rel1"] > 1.50:
            cands.append((z2, 1, c, r))
        elif not bull and z2 < -2.25 and r["vol_rel1"] > 1.50:
            cands.append((abs(z2), -1, c, r))
        time.sleep(0.05)

    cands.sort(reverse=True)
    for z_abs, side, c, r in cands[:3 - len(still)]:
        risk = 2.5 * float(r["atr"])
        entry = float(r["c"])
        sl = entry - risk if side == 1 else entry + risk
        still.append({
            "inst": c, "side": side, "entry": entry, "sl": sl,
            "risk": risk, "best": entry, "entry_ts": latest_ts,
            "z2": round(float(z_abs), 3), "vol_rel": round(float(r["vol_rel1"]), 2)
        })
        events.append(f"NEW {'LONG' if side == 1 else 'SHORT'} {c} @ {entry:.4f} | SL {sl:.4f} | Z2={z_abs:.2f}")

    state["active"] = still
    state["last_bar_ts"] = latest_ts
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    if events:
        send_tg("*[HVOL-RES 1D]*\n" + "\n".join(events))
    print(f"[HVOL_RES] Done | Active: {len(still)} | Events: {len(events)}")

if __name__ == "__main__":
    run_scan()
