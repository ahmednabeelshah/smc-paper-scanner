import os, json, time, requests, numpy as np, pandas as pd
from datetime import datetime, timezone

BOT_ID, STATE_FILE, LOG_FILE = "12H_REG_440", "state_12h_reg_440.json", "trades_12h_reg_440.csv"
SECTORS = {
    "L1": ["ETHUSDT","SOLUSDT","AVAXUSDT","NEARUSDT","SUIUSDT","APTUSDT"],
    "AI": ["RENDERUSDT","FETUSDT","TAOUSDT","WLDUSDT","ARKMUSDT"],
    "DeFi": ["UNIUSDT","AAVEUSDT","LDOUSDT","LINKUSDT","CRVUSDT","INJUSDT"],
    "Meme": ["DOGEUSDT","SHIBUSDT","PEPEUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT"],
    "Infra": ["XRPUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","ARBUSDT"],
}

def tg(msg):
    tk, cid = os.getenv("TELEGRAM_BOT_TOKEN",""), os.getenv("TELEGRAM_CHAT_ID","")
    if not tk or not cid: print(f"[TG] {msg}"); return
    try: requests.post(f"https://telegram.org{tk}/sendMessage", json={"chat_id":cid,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except Exception as e: print(f"[TG-ERR] {e}")

def fetch_12h(sym, lim=180):
    # 1. Bybit v5 (Linear & Spot)
    for cat in ["linear", "spot"]:
        try:
            r = requests.get("https://bybit.com", params={"category":cat,"symbol":sym,"interval":"720","limit":lim}, timeout=8)
            rows = r.json().get("result", {}).get("list", [])
            if len(rows) >= 5:
                df = pd.DataFrame(list(reversed(rows)), columns=["ot","o","h","l","c","v","to"])
                for k in ["o","h","l","c","v"]: df[k] = df[k].astype(float)
                df["ot"] = df["ot"].astype(np.int64); df["ct"] = df["ot"] + 43200000
                return df
        except Exception: pass
    # 2. OKX v5
    try:
        r = requests.get("https://okx.com", params={"instId":sym.replace("USDT","-USDT"),"bar":"12H","limit":lim}, timeout=8)
        rows = r.json().get("data", [])
        if len(rows) >= 5:
            df = pd.DataFrame(list(reversed(rows)), columns=["ot","o","h","l","c","v","vc","vq","cf"])
            for k in ["o","h","l","c","v"]: df[k] = df[k].astype(float)
            df["ot"] = df["ot"].astype(np.int64); df["ct"] = df["ot"] + 43200000
            return df
    except Exception: pass
    # 3. Kraken Spot Fallback (720m = 12H)
    try:
        pair = ("XBTUSD" if sym == "BTCUSDT" else sym.replace("USDT", "USD"))
        r = requests.get("https://kraken.com", params={"pair":pair,"interval":720}, timeout=8)
        res = r.json().get("result", {})
        for k, rows in res.items():
            if k != "last" and len(rows) >= 5:
                df = pd.DataFrame(rows[-lim:], columns=["ot","o","h","l","c","vwap","v","cnt"])
                for col in ["o","h","l","c","v"]: df[col] = df[col].astype(float)
                df["ot"] = (df["ot"].astype(np.int64)) * 1000; df["ct"] = df["ot"] + 43200000
                return df
    except Exception: pass
    return None

def main():
    now_ms, now_s = int(time.time()*1000), int(time.time())
    st = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"active":[],"cd":{},"last_ts":0}
    bdf = fetch_12h("BTCUSDT", 180)
    if bdf is None: print("[12H_REG_440] BTC fetch failed (Bybit/OKX/Kraken)"); return
    bc = bdf[bdf.ct <= now_ms]
    if len(bc) < 125: return
    last_ts = int(bc.ot.iloc[-1])
    b_arr = bc.c.values
    rb = np.log(b_arr[1:] / b_arr[:-1])
    btc_bear = b_arr[-1] <= np.mean(b_arr[-100:])
    btc_calm = abs(b_arr[-1]/b_arr[-3] - 1.0) < 0.065

    nxt = []
    for tr in st["active"]:
        kdf = fetch_12h(tr["sym"], 5)
        if kdf is None: nxt.append(tr); continue
        cp, hp, lp = float(kdf.c.iloc[-1]), float(kdf.h.iloc[-1]), float(kdf.l.iloc[-1])
        d, en, sld, tpd, csl, be = tr["dir"], tr["en"], tr["sld"], tr["tpd"], tr["csl"], tr["be"]
        ex_p, rsn = None, ""
        if d == 1:
            if lp <= csl: ex_p, rsn = csl, ("BE" if be else "SL")
            elif hp >= en + tpd: ex_p, rsn = en + tpd, "TP"
            else:
                if not be and hp >= en + 0.75*sld: tr["be"], tr["csl"] = True, en
                if now_s - tr["et"] >= 259200: ex_p, rsn = cp, "TIME_72H"
        else:
            if hp >= csl: ex_p, rsn = csl, ("BE" if be else "SL")
            elif lp <= en - tpd: ex_p, rsn = en - tpd, "TP"
            else:
                if not be and lp <= en - 0.75*sld: tr["be"], tr["csl"] = True, en
                if now_s - tr["et"] >= 259200: ex_p, rsn = cp, "TIME_72H"
        if ex_p is not None:
            nr = (d*(ex_p - en)/en - 0.0015) / tr["slp"]
            st["cd"][tr["sym"]] = now_s + 86400
            row = {"bot":BOT_ID,"sym":tr["sym"],"sec":tr["sec"],"side":("LONG" if d==1 else "SHORT"),"en":en,"ex":ex_p,"r":round(nr,3),"rsn":rsn,"utc":datetime.now(timezone.utc).isoformat()}
            pd.DataFrame([row]).to_csv(LOG_FILE, mode="a", header=not os.path.exists(LOG_FILE), index=False)
            tg(f"*{BOT_ID} EXIT ({rsn})* `{tr['sym']}` {row['side']} | `{en:.4f}`->`{ex_p:.4f}` | `{nr:+.2f}R`")
        else: nxt.append(tr)
    st["active"] = nxt

    if last_ts > st["last_ts"] and btc_calm and len(st["active"]) < 5:
        cands, u_sec, u_sym = [], {x["sec"] for x in st["active"]}, {x["sym"] for x in st["active"]}
        for sec, slist in SECTORS.items():
            if sec in u_sec: continue
            for sym in slist:
                if sym in u_sym or now_s < st["cd"].get(sym, 0): continue
                kdf = fetch_12h(sym, 180)
                if kdf is None: continue
                cl = kdf[kdf.ct <= now_ms]
                if len(cl) < 125: continue
                ca, va = cl.c.values[-125:], cl.v.values[-125:]
                rc, rbs = np.log(ca[1:]/ca[:-1]), rb[-124:]
                beta = np.clip(np.cov(rc[:-1], rbs[:-1])/max(np.var(rbs[:-1]), 1e-12), 0.2, 3.0)
                res = rc - beta * rbs
                sr = np.std(res[-60:], ddof=1)
                if sr <= 1e-8: continue
                z2, z4 = np.sum(res[-2:])/(sr*np.sqrt(2)), np.sum(res[-4:])/(sr*np.sqrt(4))
                if va[-1]/max(np.median(va[-20:]), 1e-8) > 1.45: continue
                slp = float(np.clip(1.55*sr*np.sqrt(3), 0.015, 0.040))
                tpp = 1.50 * slp
                if tpp < 0.0225: continue
                en = float(kdf.o.iloc[-1])
                if btc_bear and z2 >= 1.8 and z4 >= 2.0: cands.append((abs(z4), -1, sym, sec, en, slp, tpp, z2, z4))
                elif (not btc_bear) and z2 <= -2.2 and z4 <= -2.4: cands.append((abs(z4), 1, sym, sec, en, slp, tpp, z2, z4))
        cands.sort(key=lambda x: -x)
        for sc, d, sym, sec, en, slp, tpp, z2, z4 in cands:
            if len(st["active"]) >= 5: break
            if sec in u_sec or sym in u_sym: continue
            sld, tpd = en*slp, en*tpp
            csl, tpx = en - d*sld, en + d*tpd
            st["active"].append({"sym":sym,"sec":sec,"dir":d,"en":en,"slp":slp,"sld":sld,"tpd":tpd,"csl":csl,"be":False,"et":now_s})
            u_sec.add(sec); u_sym.add(sym)
            tg(f"*{BOT_ID} ENTRY* `{sym}` ({sec}) {'LONG' if d==1 else 'SHORT'} | En:`{en:.4f}` SL:`{csl:.4f}` TP:`{tpx:.4f}` | Z4:`{z4:+.2f}`")
        st["last_ts"] = last_ts

    json.dump(st, open(STATE_FILE, "w"), indent=2)
    print(f"[{BOT_ID}] Done | Active: {len(st['active'])}/5")

if __name__ == "__main__": main()
    
