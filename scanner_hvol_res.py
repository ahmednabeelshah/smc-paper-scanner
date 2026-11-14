import json, os, time, requests
import numpy as np
import pandas as pd
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = "https://api." + "telegram.org/bot" + TOKEN + "/sendMessage"
STATE_FILE = Path("active_trades_hvol_res.json")
TAG = "[HVOL-RES 1D]"

COINS = [
    "BTC-USDT-SWAP","ETH-USDT-SWAP","SOL-USDT-SWAP","XRP-USDT-SWAP","DOGE-USDT-SWAP",
    "ADA-USDT-SWAP","AVAX-USDT-SWAP","LINK-USDT-SWAP","UNI-USDT-SWAP","AAVE-USDT-SWAP",
    "BNB-USDT-SWAP","DOT-USDT-SWAP","BCH-USDT-SWAP","LTC-USDT-SWAP","ATOM-USDT-SWAP",
    "NEAR-USDT-SWAP","APT-USDT-SWAP","ARB-USDT-SWAP","OP-USDT-SWAP","SUI-USDT-SWAP",
    "INJ-USDT-SWAP","TIA-USDT-SWAP","FIL-USDT-SWAP","ETC-USDT-SWAP","TRX-USDT-SWAP",
    "TON-USDT-SWAP","WLD-USDT-SWAP","RENDER-USDT-SWAP"
]
SL_ATR, MAX_SLOTS = 2.5, 3

def get_json(path, params=None):
    for a in range(4):
        try:
            r = requests.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == 3:
                raise
            time.sleep(2 ** a)

def send_tg(msg):
    full = f"{TAG}\n{msg}"
    print("\n" + "=" * 50 + "\n" + full + "\n" + "=" * 50)
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": full}, timeout=15)
    except Exception as e:
        print("TG warn:", e)

def closed_daily_df(inst_id, limit=100):
    raw = get_json("/api/v5/market/candles", {"instId": inst_id, "bar": "1Dutc", "limit": limit}).get("data", [])
    rows = []
    for r in raw:
        ts, o, h, l, c, v, vc, vq, cfm = r
        if cfm == "1":
            rows.append((int(ts), float(o), float(h), float(l), float(c), float(vq)))
    df = pd.DataFrame(rows, columns=["ts","o","h","l","c","vq"]).sort_values("ts").drop_duplicates("ts").set_index("ts")
    tr = np.maximum(df["h"] - df["l"], np.maximum(abs(df["h"] - df["c"].shift(1)), abs(df["l"] - df["c"].shift(1))))
    df["atr"] = tr.rolling(14).mean()
    df["e50"] = df["c"].ewm(span=50, adjust=False).mean()
    df["ret"] = df["c"].pct_change()
    df["ret2"] = df["c"] / df["c"].shift(2) - 1.0
    df["vol_rel1"] = df["vq"] / df["vq"].rolling(20).mean().replace(0, np.nan)
    return df

def run_hvol_res():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_ts": 0, "trades": []}
    btc = closed_daily_df("BTC-USDT-SWAP", 100)
    if len(btc) < 65:
        return
    latest_ts = int(btc.index[-1])
    if latest_ts <= st.get("last_ts", 0):
        print("[HVOL-RES] Daily bar already processed.")
        return

    btc_bull = bool(btc["c"].iloc[-1] > btc["e50"].iloc[-1])
    btc_ret = btc["ret"]
    btc_ret2 = float(btc["ret2"].iloc[-1])

    still = []
    for tr in st["trades"]:
        df = closed_daily_df(tr["inst"], 40)
        if latest_ts not in df.index:
            still.append(tr)
            continue
        r = df.loc[latest_ts]
        b_hi, b_lo, atr14 = float(r["h"]), float(r["l"]), float(r["atr"])
        if tr["side"] == "LONG":
            if b_lo <= tr["stop"]:
                net_r = (((tr["stop"] - tr["entry"]) / tr["entry"]) - 0.0015) / (tr["risk"] / tr["entry"])
                send_tg(f"CLOSED [{'WIN' if net_r > 0 else 'LOSS'}]: {tr['sym']} LONG @ {tr['stop']:.6g} | Net: {net_r:+.2f}R")
                continue
            tr["best"] = max(tr["best"], b_hi)
            tr["stop"] = max(tr["stop"], tr["best"] - SL_ATR * atr14)
        else:
            if b_hi >= tr["stop"]:
                net_r = (((tr["entry"] - tr["stop"]) / tr["entry"]) - 0.0015) / (tr["risk"] / tr["entry"])
                send_tg(f"CLOSED [{'WIN' if net_r > 0 else 'LOSS'}]: {tr['sym']} SHORT @ {tr['stop']:.6g} | Net: {net_r:+.2f}R")
                continue
            tr["best"] = min(tr["best"], b_lo)
            tr["stop"] = min(tr["stop"], tr["best"] + SL_ATR * atr14)
        still.append(tr)
    st["trades"] = still

    if len(st["trades"]) < MAX_SLOTS:
        cands = []
        for inst in COINS[1:]:
            if any(x["inst"] == inst for x in st["trades"]):
                continue
            df = closed_daily_df(inst, 100)
            if len(df) < 65 or latest_ts not in df.index:
                continue
            r = df.loc[latest_ts]
            px, atr14 = float(r["c"]), float(r["atr"])
            if pd.isna(atr14) or (SL_ATR * atr14 / px) < 0.015:
                continue

            aligned_ret = df["ret"].reindex(btc.index)
            cov = aligned_ret.rolling(60).cov(btc_ret).iloc[-2]
            var = btc_ret.rolling(60).var().iloc[-2]
            if pd.isna(cov) or pd.isna(var) or var == 0:
                continue
            beta = float(np.clip(cov / var, 0.2, 3.0))
            rvol1 = float((aligned_ret - beta * btc_ret).rolling(60).std().iloc[-2])
            if pd.isna(rvol1) or rvol1 == 0 or pd.isna(r["ret2"]) or pd.isna(r["vol_rel1"]):
                continue

            z2 = (float(r["ret2"]) - beta * btc_ret2) / (rvol1 * np.sqrt(2))
            if btc_bull and z2 > 2.25 and float(r["vol_rel1"]) > 1.50:
                cands.append((z2, "LONG", inst, px, atr14, float(r["vol_rel1"])))
            elif not btc_bull and z2 < -2.25 and float(r["vol_rel1"]) > 1.50:
                cands.append((abs(z2), "SHORT", inst, px, atr14, float(r["vol_rel1"])))
            time.sleep(0.05)

        cands.sort(reverse=True)
        for z_abs, side, inst, px, atr14, vrel in cands[:MAX_SLOTS - len(st["trades"])]:
            risk = SL_ATR * atr14
            sl = px - risk if side == "LONG" else px + risk
            sym = inst.replace("-USDT-SWAP", "USDT")
            st["trades"].append({"inst": inst, "sym": sym, "side": side, "entry": px, "stop": sl, "risk": risk, "best": px})
            send_tg(f"NEW HVOL-RES [{side}]: {sym}\nEntry: {px:.6g} | 2.5x ATR SL: {sl:.6g} ({(risk/px)*100:.2f}%)\nZ2: {z_abs:.2f} | VolRel: {vrel:.2f}x")

    st["last_ts"] = latest_ts
    STATE_FILE.write_text(json.dumps(st, indent=2))
    print(f"[HVOL-RES] Daily scan finished. Active slots: {len(st['trades'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    run_hvol_res()
