import json, os, time, requests
import numpy as np
import pandas as pd
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = "https://api." + "telegram.org/bot" + TOKEN + "/sendMessage"
STATE_FILE = Path("active_trades_streak4.json")
TAG = "[STREAK4 1D]"

COINS = [
    "BTC-USDT-SWAP","ETH-USDT-SWAP","SOL-USDT-SWAP","XRP-USDT-SWAP","DOGE-USDT-SWAP",
    "ADA-USDT-SWAP","AVAX-USDT-SWAP","LINK-USDT-SWAP","UNI-USDT-SWAP","AAVE-USDT-SWAP",
    "BNB-USDT-SWAP","DOT-USDT-SWAP","BCH-USDT-SWAP","LTC-USDT-SWAP","ATOM-USDT-SWAP",
    "NEAR-USDT-SWAP","APT-USDT-SWAP","ARB-USDT-SWAP","OP-USDT-SWAP","SUI-USDT-SWAP",
    "INJ-USDT-SWAP","TIA-USDT-SWAP","FIL-USDT-SWAP","ETC-USDT-SWAP","TRX-USDT-SWAP",
    "TON-USDT-SWAP","WLD-USDT-SWAP","RENDER-USDT-SWAP"
]
Z_THRESH, STOP_MULT, MAX_DAYS, MAX_SLOTS = 2.50, 2.5, 4, 3

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
            rows.append((int(ts), float(o), float(h), float(l), float(c)))
    df = pd.DataFrame(rows, columns=["ts","o","h","l","c"]).sort_values("ts").drop_duplicates("ts").set_index("ts")
    df["ret"] = df["c"].pct_change().astype(float)
    df["ret4"] = (df["c"] / df["c"].shift(4) - 1.0).astype(float)
    return df

def run_streak4():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_ts": 0, "trades": []}
    btc = closed_daily_df("BTC-USDT-SWAP", 100)
    if len(btc) < 65:
        return
    latest_ts = int(btc.index[-1])
    if latest_ts <= st.get("last_ts", 0):
        print("[STREAK4] Daily bar already processed.")
        return

    b_c = float(btc["c"].iloc[-1])
    btc_ret = btc["ret"]
    b_ret4_today = float(btc["ret4"].iloc[-1])

    still = []
    for tr in st["trades"]:
        df = closed_daily_df(tr["inst"], 20)
        if latest_ts not in df.index:
            still.append(tr)
            continue
        c_now = float(df.loc[latest_ts, "c"])
        tr["days"] += 1
        sp = tr["side_num"] * (c_now - tr["entry"]) / tr["entry"] - tr["side_num"] * tr["beta"] * (b_c - tr["b_entry"]) / tr["b_entry"]
        fee_r = 0.0015 * (1.0 + tr["beta"]) / tr["stop_pct"]
        if sp <= -tr["stop_pct"]:
            net_r = -1.0 - fee_r
            send_tg(f"CLOSED [STOP]: {tr['sym']} {tr['side']} + BTC Hedge | Net: {net_r:+.2f}R")
            continue
        if tr["days"] >= MAX_DAYS:
            net_r = (sp / tr["stop_pct"]) - fee_r
            send_tg(f"CLOSED [{'WIN' if net_r > 0 else 'LOSS'} 4D]: {tr['sym']} {tr['side']} | Net: {net_r:+.2f}R")
            continue
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
            aligned = df["ret"].reindex(btc.index).astype(float)
            cov = aligned.rolling(60).cov(btc_ret).iloc[-2]
            var = btc_ret.rolling(60).var().iloc[-2]
            if pd.isna(cov) or pd.isna(var) or var == 0:
                continue
            beta = float(np.clip(cov / var, 0.2, 3.0))
            res1 = (aligned - beta * btc_ret).astype(float)
            rvol1 = float(res1.rolling(60).std().iloc[-2])
            if pd.isna(rvol1) or rvol1 == 0 or pd.isna(df.loc[latest_ts, "ret4"]):
                continue
            stop_pct = STOP_MULT * rvol1
            if stop_pct < 0.015:
                continue

            s_sign = pd.Series(np.sign(res1.to_numpy(dtype=float)), index=res1.index)
            st4 = bool((s_sign.iloc[-1] != 0) and (s_sign.iloc[-1] == s_sign.iloc[-2] == s_sign.iloc[-3] == s_sign.iloc[-4]))
            z4 = (float(df.loc[latest_ts, "ret4"]) - beta * b_ret4_today) / (rvol1 * 2.0)

            if st4 and abs(z4) > Z_THRESH:
                side = "SHORT" if z4 > 0 else "LONG"
                side_num = -1 if z4 > 0 else 1
                cands.append((abs(z4), side, side_num, inst, float(df.loc[latest_ts, "c"]), beta, stop_pct))
            time.sleep(0.05)

        cands.sort(reverse=True)
        for z_abs, side, side_num, inst, px, beta, stop_pct in cands[:MAX_SLOTS - len(st["trades"])]:
            sym = inst.replace("-USDT-SWAP", "USDT")
            st["trades"].append({
                "inst": inst, "sym": sym, "side": side, "side_num": side_num,
                "entry": px, "b_entry": b_c, "beta": round(beta, 3),
                "stop_pct": round(stop_pct, 4), "days": 0
            })
            hedge_side = "LONG" if side == "SHORT" else "SHORT"
            send_tg(f"NEW STREAK4 [{side}]: {sym} @ {px:.6g}\nHedge: {hedge_side} BTC ({beta:.2f}x beta) @ {b_c:.6g}\nZ4: {z_abs:.2f} | Spread Stop: {stop_pct*100:.2f}% | Max 4D")

    st["last_ts"] = latest_ts
    STATE_FILE.write_text(json.dumps(st, indent=2))
    print(f"[STREAK4] Daily scan finished. Active slots: {len(st['trades'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    run_streak4()
          
