import json, os, time, requests
import numpy as np
import pandas as pd
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = "https://api." + "telegram.org/bot" + TOKEN + "/sendMessage"
STATE_FILE = Path("active_trades_vol_macd.json")
TAG = "[VOL-MACD 1D]"

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
    df["vol_rel1"] = df["vq"] / df["vq"].rolling(20).mean().replace(0, np.nan)
    macd = df["c"].ewm(span=12, adjust=False).mean() - df["c"].ewm(span=26, adjust=False).mean()
    df["macd"] = macd
    df["macd_prev"] = macd.shift(1)
    return df

def run_vol_macd():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_ts": 0, "trades": []}
    btc = closed_daily_df("BTC-USDT-SWAP", 100)
    if len(btc) < 60:
        return
    latest_ts = int(btc.index[-1])
    if latest_ts <= st.get("last_ts", 0):
        print("[VOL-MACD] Daily bar already processed.")
        return

    btc_bull = bool(btc["c"].iloc[-1] > btc["e50"].iloc[-1])

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
            if len(df) < 60 or latest_ts not in df.index:
                continue
            r = df.loc[latest_ts]
            px, atr14 = float(r["c"]), float(r["atr"])
            if pd.isna(atr14) or pd.isna(r["macd_prev"]) or pd.isna(r["vol_rel1"]):
                continue
            if (SL_ATR * atr14 / px) < 0.015:
                continue

            m_now, m_prev, vrel = float(r["macd"]), float(r["macd_prev"]), float(r["vol_rel1"])
            if btc_bull and m_now > 0 and m_prev <= 0 and vrel > 1.35:
                cands.append((vrel, "LONG", inst, px, atr14))
            elif not btc_bull and m_now < 0 and m_prev >= 0 and vrel > 1.35:
                cands.append((vrel, "SHORT", inst, px, atr14))
            time.sleep(0.05)

        cands.sort(reverse=True)
        for vrel, side, inst, px, atr14 in cands[:MAX_SLOTS - len(st["trades"])]:
            risk = SL_ATR * atr14
            sl = px - risk if side == "LONG" else px + risk
            sym = inst.replace("-USDT-SWAP", "USDT")
            st["trades"].append({"inst": inst, "sym": sym, "side": side, "entry": px, "stop": sl, "risk": risk, "best": px})
            send_tg(f"NEW VOL-MACD ZERO [{side}]: {sym}\nEntry: {px:.6g} | 2.5x ATR SL: {sl:.6g} ({(risk/px)*100:.2f}%)\nVolRel: {vrel:.2f}x")

    st["last_ts"] = latest_ts
    STATE_FILE.write_text(json.dumps(st, indent=2))
    print(f"[VOL-MACD] Daily scan finished. Active slots: {len(st['trades'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    run_vol_macd()
