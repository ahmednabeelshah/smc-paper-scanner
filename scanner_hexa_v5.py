import os, json, time, requests
import numpy as np
import pandas as pd

BOT_TOKEN, CHAT_ID, SL = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", ""), chr(47)
HOST = ".".join(["www", "okx", "cab"])
BASE = "https:" + SL + SL + HOST + SL + "api" + SL + "v5" + SL + "market" + SL + "candles"
COINS = "BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT UNI ATOM LTC BCH ETC FIL APT ARB OP NEAR INJ SUI TIA SEI WLD ORDI ENA ZEC".split()

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
                df["ret"] = df["c"].pct_change()
                df["vrel"] = df["vq"]/df["vq"].rolling(20).mean().replace(0, np.nan)
                df["clv"] = ((2.0*df["c"]-df["h"]-df["l"])/(df["h"]-df["l"]).replace(0, np.nan)).fillna(0.0)
                df["macd"] = df["c"].ewm(span=12, adjust=False).mean() - df["c"].ewm(span=26, adjust=False).mean()
                df["hist"] = df["macd"] - df["macd"].ewm(span=9, adjust=False).mean()
                return df
        except Exception: time.sleep(0.5*(att+1))
    return pd.DataFrame()

SF, TAG = "active_trades_hexa_v5.json", "[HEXA-V5 CHAMP]"
def main():
    st, btc = load_st(SF), fetch_1d("BTC", 130)
    if len(btc) < 70: return
    lts, bc = int(btc.index[-1]), float(btc["c"].iloc[-1])
    b_reg = 1 if bc > float(btc["e50"].iloc[-1]) else -1
    still = list()
    for p in st["active"]:
        df = fetch_1d(p["sym"], 20)
        if len(df) == 0 or int(df.index[-1]) <= p.get("last_ts", p["entry_ts"]): still.append(p); continue
        p["last_ts"], p["days"] = int(df.index[-1]), p.get("days", 0) + 1
        hi, lo, cp = float(df["h"].iloc[-1]), float(df["l"].iloc[-1]), float(df["c"].iloc[-1])
        sgn, w = (1.0 if p["side"] == "LONG" else -1.0), float(p.get("mult", 1.0))
        if p.get("mode") == "MR":
            sp = sgn*(cp-p["entry"])/p["entry"] - sgn*p["beta"]*(bc-p["b_entry"])/p["b_entry"]
            p["cum_pnl"] = round(sp, 5)
            if sp <= -p["stop_pct"] or sp >= 0.80*p["stop_pct"] or p["days"] >= p["max_d"]:
                nr = round(((-1.05) if sp <= -p["stop_pct"] else (sp/p["stop_pct"] - 0.05)) * w, 2)
                p.update({"exit_ts": lts, "exit_price": cp, "net_r": nr}); st["closed"].append(p)
                send_tg(f"{TAG} MR-EXIT {p['side']} {p['inst']} @ {cp:.4f} | {nr:+.2f}R"); continue
        else:
            p["cum_pnl"] = round(sgn*(cp-p["entry"])/p["entry"], 5)
            if (sgn > 0 and lo <= p["sl"]) or (sgn < 0 and hi >= p["sl"]):
                nr = round(((sgn*(p["sl"]-p["entry"])/(p["entry"]*p["stop_pct"])) - 0.05) * w, 2)
                p.update({"exit_ts": lts, "exit_price": p["sl"], "net_r": nr}); st["closed"].append(p)
                send_tg(f"{TAG} TREND-EXIT {p['side']} {p['inst']} @ {p['sl']:.4f} | {nr:+.2f}R"); continue
            p["best"] = max(p["best"], hi) if sgn > 0 else min(p["best"], lo)
            p["sl"] = round(max(p["sl"], p["best"]*(1.0-p["stop_pct"])) if sgn > 0 else min(p["sl"], p["best"]*(1.0+p["stop_pct"])), 6)
        still.append(p)
    st["active"] = still
    t_open = [x for x in st["active"] if x.get("mode") == "TREND"]
    m_open = [x for x in st["active"] if x.get("mode") == "MR"]
    t_cands, m_cands = list(), list()
    for sym in COINS[1:]:
        if any(x["sym"] == sym for x in st["active"]): continue
        df = fetch_1d(sym, 130)
        if len(df) < 70 or int(df.index[-1]) != lts: continue
        br = btc["ret"].reindex(df.index)
        bt = float((df["ret"].rolling(60).cov(br)/br.rolling(60).var()).clip(0.2, 3.0).iloc[-1])
        res = df["ret"] - bt*br; rv = float(res.rolling(20).std().iloc[-1])
        z1, z2, z4 = float(res.iloc[-1]/(rv+1e-12)), float(res.rolling(2).sum().iloc[-1]/(rv*1.414+1e-12)), float(res.rolling(4).sum().iloc[-1]/(rv*2.0+1e-12))
        cp, atr, acc, vr, clv = float(df["c"].iloc[-1]), float(df["atr"].iloc[-1]), float(df["acc"].iloc[-1]), float(df["vrel"].iloc[-1]), float(df["clv"].iloc[-1])
        mc, mc1 = float(df["macd"].iloc[-1]), float(df["macd"].iloc[-2])
        hs, hs1, hs2 = float(df["hist"].iloc[-1]), float(df["hist"].iloc[-2]), float(df["hist"].iloc[-3])
        v1 = 1 if (mc>0 and mc1<=0 and vr>1.35 and b_reg==1) else (-1 if (mc<0 and mc1>=0 and vr>1.35 and b_reg==-1) else 0)
        v2 = 1 if (z2>2.25 and vr>1.50 and b_reg==1) else (-1 if (z2<-2.25 and vr>1.50 and b_reg==-1) else 0)
        v3 = 1 if (hs>hs1 and hs1<hs2 and hs<0 and vr>1.20 and b_reg==1) else (-1 if (hs<hs1 and hs1>hs2 and hs>0 and vr>1.20 and b_reg==-1) else 0)
        v4 = 1 if (acc>1.25 and vr>1.35 and clv>0.70 and b_reg==1) else (-1 if (acc>1.25 and vr>1.35 and clv<-0.70 and b_reg==-1) else 0)
        tv = v1 + v2 + v3 + v4
        if tv != 0:
            sp = max(1.8*atr/cp, 0.015)
            t_cands.append((abs(tv)*10 + vr, "LONG" if tv>0 else "SHORT", sym, cp, sp, 1.5 if abs(tv)>=2 else 1.0, abs(tv)))
        strk4 = (np.sign(res.iloc[-1])==np.sign(res.iloc[-2])==np.sign(res.iloc[-3])==np.sign(res.iloc[-4]))
        m1 = -1 if (acc<1.20 and z1>2.00) else 0
        m2 = -1 if (acc<1.20 and strk4 and z4>2.25) else 0
        mv = m1 + m2
        if mv < 0:
            sp = max(1.25*rv, 0.015)
            m_cands.append((abs(z1)+0.5*abs(z4), sym, cp, bt, sp, 1.5 if abs(mv)>=2 else 1.0, 5 if m1 else 4, round(z1, 2)))
    for _, side, sym, cp, sp, mult, votes in sorted(t_cands, reverse=True)[:3-len(t_open)]:
        sl = round(cp*(1.0-sp) if side=="LONG" else cp*(1.0+sp), 6)
        st["active"].append({"mode": "TREND", "sym": sym, "inst": sym+"-USDT-SWAP", "side": side, "entry": cp, "sl": sl, "best": cp, "stop_pct": sp, "mult": mult, "votes": votes, "days": 0, "cum_pnl": 0.0, "entry_ts": lts, "last_ts": lts})
        send_tg(f"{TAG} NEW TREND {side} {sym}-USDT-SWAP @ {cp:.4f} | SL: {sl:.4f} ({sp*100:.1f}%) | Votes={votes} ({mult}x)")
    for _, sym, cp, bv, sp, mult, maxd, z_val in sorted(m_cands, reverse=True)[:3-len(m_open)]:
        st["active"].append({"mode": "MR", "sym": sym, "inst": sym+"-USDT-SWAP", "side": "SHORT", "entry": cp, "b_entry": bc, "beta": bv, "stop_pct": sp, "mult": mult, "max_d": maxd, "z1": z_val, "days": 0, "cum_pnl": 0.0, "entry_ts": lts, "last_ts": lts})
        send_tg(f"{TAG} NEW SHORT-MR {sym}-USDT-SWAP @ {cp:.4f} | Stop: {sp*100:.1f}% | Z1={z_val} ({mult}x)")
    save_st(SF, st)
if __name__ == "__main__": main()
    
