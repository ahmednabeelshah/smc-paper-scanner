import os, json, csv, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone

BOT_ID = "530"
STATE_F, CSV_F = f"state_{BOT_ID}.json", f"trades_{BOT_ID}.csv"
TG_TOKEN, TG_CHAT = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
SECTORS = {"L1":"ETH SOL AVAX NEAR SUI APT", "AI":"RENDER FET TAO WLD ARKM", "DEFI":"UNI AAVE LDO LINK CRV INJ", "MEME":"DOGE SHIB PEPE WIF BONK FLOKI", "INFRA":"XRP LTC BCH ATOM ARB"}
C2S = {c: s for s, cs in SECTORS.items() for c in cs.split()}

def send_tg(msg):
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://telegram.org{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        except Exception: pass

def fetch_1h(sym):
    for url, fn in (
        (f"https://bybit.com{sym}USDT&interval=60&limit=80", lambda d: list(reversed(d.get("result", {}).get("list", [])))),
        (f"https://okx.com{sym}-USDT-SWAP&bar=1H&limit=80", lambda d: list(reversed(d.get("data", []))))
    ):
        try:
            rows = fn(requests.get(url, timeout=10).json())
            if len(rows) >= 60:
                df = pd.DataFrame(rows).iloc[:, :6].astype(float)
                df.columns = ("ts", "o", "h", "l", "c", "v")
                return df.iloc[:-1].reset_index(drop=True)
        except Exception: continue
    return pd.DataFrame()

st = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {"open": {}, "cd": {}}
now_ts = int(time.time())

for c, sec in C2S.items():
    df = fetch_1h(c)
    if df.empty or len(df) < 55: continue
    last = df.iloc[-1]
    if c in st.get("open"):
        tr = st.get("open").get(c); d_t = tr.get("dir", -1)
        hit_sl = (float(last.l) <= tr.get("sl")) if d_t == 1 else (float(last.h) >= tr.get("sl"))
        hit_tp = (float(last.h) >= tr.get("tp")) if d_t == 1 else (float(last.l) <= tr.get("tp"))
        if hit_sl or hit_tp:
            r_net = (-1.0 if hit_sl else 1.6) - (0.0015 * float(last.c) / max(tr.get("risk"), 1e-9))
            st.get("open").pop(c); st.get("cd").update({c: now_ts})
            with open(CSV_F, "a", newline="") as f: csv.writer(f).writerow((datetime.now(timezone.utc).isoformat(), BOT_ID, c, sec, "EXIT", float(last.c), round(r_net, 3)))
            send_tg(f"#{BOT_ID} EXIT {c} ({sec}) | {'TP' if hit_tp else 'SL'} | NetR: {r_net:+.2f}R")
        continue
    if len(st.get("open")) >= 5 or any(C2S.get(oc) == sec for oc in st.get("open")) or (now_ts - st.get("cd").get(c, 0)) < 86400: continue
    sig_v = float(np.log(df.c / df.c.shift(1)).fillna(0).rolling(24).std().iloc[-1])
    r3 = float((df.c.iloc[-1] - df.c.iloc[-4]) / df.c.iloc[-4])
    vr = float(last.v / max(df.v.iloc[-49:-1].mean(), 1e-9))
    rng = max(float(last.h - last.l), 1e-9)
    uw, lw = float(last.h - max(last.o, last.c)) / rng, float(min(last.o, last.c) - last.l) / rng
    d_t = 1 if (r3 < -0.055 and vr > 2.8 and lw > 0.52 and float(last.c) > float(last.o)) else (-1 if (r3 > 0.05 and vr > 2.5 and uw > 0.50 and float(last.c) < float(last.o)) else 0)
    if d_t != 0:
        ep = float(last.c); risk = max(ep * 0.015, ep * 2.2 * sig_v)
        sl, tp = ep - d_t * risk, ep + d_t * (risk * 1.6)
        st.get("open").update({c: {"dir": d_t, "sec": sec, "ep": ep, "sl": sl, "tp": tp, "risk": risk}})
        with open(CSV_F, "a", newline="") as f: csv.writer(f).writerow((datetime.now(timezone.utc).isoformat(), BOT_ID, c, sec, "LONG" if d_t == 1 else "SHORT", ep, 0.0))
        send_tg(f"#{BOT_ID} {'LONG' if d_t == 1 else 'SHORT'} {c} ({sec}) | Entry: {ep:.5g} | SL: {sl:.5g} | TP: {tp:.5g}")

json.dump(st, open(STATE_F, "w"))
