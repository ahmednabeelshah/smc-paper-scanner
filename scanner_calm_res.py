import os, json, time, requests
import numpy as np
import pandas as pd

BOT_TOKEN, CHAT_ID, SL = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), chr(47)
HOST = ".".join(["www", "okx", "cab"])
BASE = "https:" + SL + SL + HOST + SL + "api" + SL + "v5" + SL + "market" + SL + "candles"
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK DOT UNI ATOM LTC BCH FIL APT ARB OP SUI NEAR INJ TIA SEI RENDER FET PEPE WIF BONK AAVE".split()

def send_tg(msg):
    if not BOT_TOKEN or not CHAT_ID: print(msg); return
    try: requests.post("https:" + SL + SL + "api.telegram.org" + SL + f"bot{BOT_TOKEN}" + SL + "sendMessage", json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e: print("TG ERR:", e)

def load_st(fn): return json.load(open(fn)) if os.path.exists(fn) else {"active": list(), "closed": list()}
def save_st(fn, st): json.dump(st, open(fn, "w"), indent=2)

def fetch_1d(sym, limit=140):
    for att in range(4):
        try:
            r = requests.get(BASE, params={"instId": sym + "-USDT-SWAP", "bar": "1Dutc", "limit": str(limit)}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()
            if r.get("code") == "0":
                rows = sorted([(int(ts), float(o), float(h), float(l), float(c), float(vq)) for ts, o, h, l, c, v, vc, vq, cfm in r.get("data", list()) if str(cfm) == "1"])
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

SF, TAG = "active_trades_calm_res.json", "[CALM-RES 1D]"
def main():
    st, btc = load_st(SF), fetch_1d("BTC", 120)
    if len(btc) < 70: return
    lts, bc, still = int(btc.index[-1]), float(btc["c"].iloc[-1]), list()
    for p in st["active"]:
        df = fetch_1d(p["sym"], 20)
        if len(df) == 0 or int(df.index[-1]) <= p.get("last_ts", p["entry_ts"]): still.append(p); continue
        p["last_ts"], p["days"] = int(df.index[-1]), p.get("days", 0) + 1
        cp, sgn = float(df["c"].iloc[-1]), 1.0 if p["side"] == "LONG" else -1.0
        sp = sgn*(cp-p["entry"])/p["entry"] - sgn*p["beta"]*(bc-p["b_entry"])/p["b_entry"]
        p["cum_pnl"] = round(sp, 5)
        if sp <= -p["stop_pct"] or sp >= 0.80*p["stop_pct"] or p["days"] >= 5:
            nr = round((-1.05) if sp <= -p["stop_pct"] else (sp/p["stop_pct"] - 0.05), 2)
            p.update({"exit_ts": lts, "exit_price": cp, "net_r": nr}); st["closed"].append(p)
            send_tg(f"{TAG} EXIT {p['side']} {p['inst']} @ {cp:.4f} | {nr:+.2f}R"); continue
        still.append(p)
    st["active"] = still
    if len(st["active"]) < 3:
        cands = list()
        for sym in COINS[1:]:
            if any(x["sym"] == sym for x in st["active"]): continue
            df = fetch_1d(sym, 120)
            if len(df) < 70 or int(df.index[-1]) != lts: continue
            br = btc["ret"].reindex(df.index)
            bt = (df["ret"].rolling(60).cov(br)/br.rolling(60).var()).clip(0.2, 3.0)
            res1 = df["ret"] - bt*br
            rv1 = res1.rolling(20).std()
            z1, rv, bv, acc = float((res1/(rv1+1e-12)).iloc[-1]), float(rv1.iloc[-1]), float(bt.iloc[-1]), float(df["acc"].iloc[-1])
            if abs(z1) > 2.00 and acc < 1.20 and not np.isnan(rv):
                sp = max(1.25*rv, 0.015)
                cands.append((abs(z1), "SHORT" if z1 > 0 else "LONG", sym, float(df["c"].iloc[-1]), bv, sp))
        for za, side, sym, cp, bv, sp in sorted(cands, reverse=True)[:3-len(st["active"])]:
            st["active"].append({"sym": sym, "inst": sym+"-USDT-SWAP", "side": side, "entry": cp, "b_entry": bc, "beta": bv, "stop_pct": sp, "days": 0, "cum_pnl": 0.0, "entry_ts": lts, "last_ts": lts})
            send_tg(f"{TAG} NEW {side} {sym}-USDT-SWAP @ {cp:.4f} | Stop: {sp*100:.2f}% | |Z1|={za:.2f}")
    save_st(SF, st)
if __name__ == "__main__": main()
