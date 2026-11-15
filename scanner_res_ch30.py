import os, json, time, requests
import numpy as np
import pandas as pd

BOT_TOKEN, CHAT_ID, SL = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), chr(47)
BASE = "https:" + SL + SL + "://okx.com" + SL + "api" + SL + "v5" + SL + "market" + SL + "candles"
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK DOT UNI ATOM LTC BCH FIL APT ARB OP SUI NEAR INJ TIA SEI RENDER FET PEPE WIF BONK AAVE".split()

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID: print(msg); return
    try: requests.post("https:" + SL + SL + "api.telegram.org" + SL + f"bot{BOT_TOKEN}" + SL + "sendMessage", json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e: print("TG ERR:", e)

def load_st(fn): return json.load(open(fn)) if os.path.exists(fn) else {"active": [], "closed": []}
def save_st(fn, st): json.dump(st, open(fn, "w"), indent=2)

def fetch_1d(sym, limit=140):
    for att in range(4):
        try:
            r = requests.get(BASE, params={"instId": sym + "-USDT-SWAP", "bar": "1Dutc", "limit": str(limit)}, timeout=10).json()
            if r.get("code") == "0":
                rows = sorted([(int(ts), float(o), float(h), float(l), float(c), float(vq)) for ts, o, h, l, c, v, vc, vq, cfm in r.get("data", []) if str(cfm) == "1"])
                df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "vq"]).set_index("ts")
                tr = np.maximum(df["h"]-df["l"], np.maximum(abs(df["h"]-df["c"].shift(1)), abs(df["l"]-df["c"].shift(1))))
                df["atr"], df["atr50"] = tr.rolling(14).mean(), tr.rolling(50).mean()
                df["acc"], df["e50"] = df["atr"]/df["atr50"].replace(0, np.nan), df["c"].ewm(span=50, adjust=False).mean()
                df["ret"], df["ret4"] = df["c"].pct_change(), df["c"].pct_change(4)
                df["vrel"] = df["vq"]/df["vq"].rolling(20).mean().replace(0, np.nan)
                df["clv"] = ((2.0*df["c"]-df["h"]-df["l"])/(df["h"]-df["l"]).replace(0, np.nan)).fillna(0.0)
                return df
        except Exception: time.sleep(0.5*(att+1))
    return pd.DataFrame()

SF, TAG = "active_trades_res_ch30.json", "[RES-CH30 1D]"
def main():
    st, btc = load_st(SF), fetch_1d("BTC", 140)
    if len(btc) < 100: return
    lts, bull, still = int(btc.index[-1]), bool(btc["c"].iloc[-1] > btc["e50"].iloc[-1]), []
    for p in st["active"]:
        df = fetch_1d(p["sym"], 40)
        if len(df) == 0 or int(df.index[-1]) <= p.get("last_ts", p["entry_ts"]): still.append(p); continue
        r = df.iloc[-1]; p["last_ts"] = int(df.index[-1])
        if p["side"] == "LONG" and r["l"] <= p["sl"]:
            nr = round(((p["sl"]-p["entry"])/p["entry"]-0.0015)/(p["risk"]/p["entry"]), 2)
            p.update({"exit_ts": lts, "exit_price": float(p["sl"]), "net_r": nr}); st["closed"].append(p); send_tg(f"{TAG} EXIT LONG {p['inst']} @ {p['sl']:.4f} | {nr:+.2f}R"); continue
        if p["side"] == "SHORT" and r["h"] >= p["sl"]:
            nr = round(((p["entry"]-p["sl"])/p["entry"]-0.0015)/(p["risk"]/p["entry"]), 2)
            p.update({"exit_ts": lts, "exit_price": float(p["sl"]), "net_r": nr}); st["closed"].append(p); send_tg(f"{TAG} EXIT SHORT {p['inst']} @ {p['sl']:.4f} | {nr:+.2f}R"); continue
        p["best"] = max(p["best"], float(r["h"])) if p["side"] == "LONG" else min(p["best"], float(r["l"]))
        p["sl"] = max(p["sl"], p["best"]-2.5*float(r["atr"])) if p["side"] == "LONG" else min(p["sl"], p["best"]+2.5*float(r["atr"]))
        still.append(p)
    st["active"] = still
    if len(st["active"]) < 3:
        cands = []
        for sym in COINS[1:]:
            if any(x["sym"] == sym for x in st["active"]): continue
            df = fetch_1d(sym, 140)
            if len(df) < 100 or int(df.index[-1]) != lts: continue
            br = btc["ret"].reindex(df.index)
            bt = (df["ret"].rolling(60).cov(br)/br.rolling(60).var()).shift(1).clip(0.2, 3.0)
            cr = (df["ret"] - bt*br).fillna(0.0).cumsum()
            hi30, lo30 = cr.rolling(30).max().shift(1).iloc[-1], cr.rolling(30).min().shift(1).iloc[-1]
            r = df.iloc[-1]; sp = 2.5*float(r["atr"])/float(r["c"])
            if sp >= 0.015 and r["vrel"] > 1.35 and ((bull and cr.iloc[-1] > hi30) or (not bull and cr.iloc[-1] < lo30)):
                cands.append((float(r["vrel"]), "LONG" if bull else "SHORT", sym, float(r["c"]), float(r["atr"]), sp))
        for _, side, sym, cp, atr, sp in sorted(cands, reverse=True)[:3-len(st["active"])]:
            rk = 2.5*atr; sl = cp-rk if side == "LONG" else cp+rk
            st["active"].append({"sym": sym, "inst": sym+"-USDT-SWAP", "side": side, "entry": cp, "sl": sl, "risk": rk, "best": cp, "entry_ts": lts, "last_ts": lts})
            send_tg(f"{TAG} NEW {side} {sym}-USDT-SWAP @ {cp:.4f} | SL: {sl:.4f} ({sp*100:.2f}%)")
    save_st(SF, st)
if __name__ == "__main__": main()
