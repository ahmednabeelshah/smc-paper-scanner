import json, os, time, requests
from datetime import datetime, timezone
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = f"https://telegram.org{TOKEN}/sendMessage"
STATE_FILE = Path("active_trades_tsmom.json")
TAG = "📊 [TSMOM 5-YR SWING]"

COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
    "UNI-USDT-SWAP", "AAVE-USDT-SWAP"
]

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
    print("\n" + "=" * 50 + "\n" + msg + "\n" + "=" * 50)
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("TG warn:", e)

def ema_series(vals, p):
    a = 2.0 / (p + 1.0)
    out = []
    v = vals.__getitem__(0)
    for x in vals:
        v = a * x + (1.0 - a) * v
        out.append(v)
    return out

def atr_last(hi, lo, cl, p=14):
    trs = []
    for i in range(1, len(cl)):
        tr = max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
        trs.append(tr)
    return sum(trs[-p:]) / float(p)

def closed_daily(inst_id, limit=80):
    raw = get_json("/api/v5/market/candles", {"instId": inst_id, "bar": "1Dutc", "limit": limit}).get("data", [])
    closed = [r for r in raw if r.__getitem__(8) == "1"]
    closed.reverse()
    return closed

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen": [], "trades": []}

def save_state(st):
    st["seen"] = st["seen"][-200:]
    STATE_FILE.write_text(json.dumps(st, indent=2))
def manage_open_tsmom(st, srv_ms):
    for tr in st["trades"]:
        if tr["status"] != "ACTIVE":
            continue
        inst = tr["inst"]
        sym = tr["symbol"]
        kd = closed_daily(inst, 30)
        if len(kd) < 20:
            continue
        hi = [float(x.__getitem__(2)) for x in kd]
        lo = [float(x.__getitem__(3)) for x in kd]
        cl = [float(x.__getitem__(4)) for x in kd]
        atr14 = atr_last(hi, lo, cl, 14)

        # Check intraday 1H highs/lows since last_chk for exact Chandelier trailing stop
        k1 = get_json("/api/v5/market/candles", {"instId": inst, "bar": "1H", "limit": 48}).get("data", [])
        k1.reverse()
        for bar in k1:
            b_hi = float(bar.__getitem__(2))
            b_lo = float(bar.__getitem__(3))
            b_close_t = int(bar.__getitem__(0)) + 3600000
            if bar.__getitem__(8) != "1" or b_close_t <= tr["last_chk"]:
                continue
            tr["last_chk"] = b_close_t
            entry, sl, risk = tr["entry"], tr["stop"], tr["risk"]

            if tr["side"] == "LONG":
                if b_lo <= sl:
                    pnl_pct = ((sl - entry) / entry * 100.0) - 0.14
                    r_mult = pnl_pct / ((risk / entry) * 100.0)
                    tr["status"] = "CLOSED"
                    res = "WIN" if r_mult > 0 else "LOSS"
                    send_tg(f"{TAG}\nCLOSED [{res}]: {sym} LONG @ {sl:.6g}\nNet: {pnl_pct:+.2f}% ({r_mult:+.2f}R)")
                    break
                if b_hi > tr["best"]:
                    tr["best"] = b_hi
                    new_sl = max(sl, tr["best"] - 2.5 * atr14)
                    if new_sl > sl * 1.002:
                        tr["stop"] = new_sl
                        send_tg(f"{TAG}\nATR TRAIL UPDATED: {sym} LONG\nPeak: {tr['best']:.6g} -> New SL: {new_sl:.6g}")
            else:
                if b_hi >= sl:
                    pnl_pct = ((entry - sl) / entry * 100.0) - 0.14
                    r_mult = pnl_pct / ((risk / entry) * 100.0)
                    tr["status"] = "CLOSED"
                    res = "WIN" if r_mult > 0 else "LOSS"
                    send_tg(f"{TAG}\nCLOSED [{res}]: {sym} SHORT @ {sl:.6g}\nNet: {pnl_pct:+.2f}% ({r_mult:+.2f}R)")
                    break
                if b_lo < tr["best"]:
                    tr["best"] = b_lo
                    new_sl = min(sl, tr["best"] + 2.5 * atr14)
                    if new_sl < sl * 0.998:
                        tr["stop"] = new_sl
                        send_tg(f"{TAG}\nATR TRAIL UPDATED: {sym} SHORT\nLow: {tr['best']:.6g} -> New SL: {new_sl:.6g}")

def run_once():
    st = load_state()
    srv_ms = int(get_json("/api/v5/public/time")["data"].__getitem__(0)["ts"])
    manage_open_tsmom(st, srv_ms)

    dt_utc = datetime.fromtimestamp(srv_ms / 1000, timezone.utc)
    now_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    open_slots = sum(1 for x in st["trades"] if x["status"] == "ACTIVE")
    if open_slots >= 3:
        print(f"[{now_str}] [TSMOM] Max 3 swing slots full ({open_slots}/3).")
        save_state(st)
        return

    btc_d = closed_daily("BTC-USDT-SWAP", 70)
    if len(btc_d) < 55:
        save_state(st)
        return
    c_btc = [float(x.__getitem__(4)) for x in btc_d]
    e50_btc = ema_series(c_btc, 50)[-1]
    btc_bull = c_btc[-1] > e50_btc
    btc_bear = c_btc[-1] < e50_btc
    d_close_ms = int(btc_d[-1].__getitem__(0)) + 86400000
    print(f"[{now_str}] [TSMOM] Daily Regime: BTC={'BULL' if btc_bull else 'BEAR'} | Slots={open_slots}/3")

    for inst in COINS:
        if inst == "BTC-USDT-SWAP":
            continue
        if sum(1 for x in st["trades"] if x["status"] == "ACTIVE") >= 3:
            break
        sym = inst.replace("-USDT-SWAP", "USDT")
        if any(x["symbol"] == sym and x["status"] == "ACTIVE" for x in st["trades"]):
            continue
        uid = f"{sym}_TSMOM_{d_close_ms}"
        if uid in st["seen"]:
            continue

        try:
            kd = closed_daily(inst, 70)
            if len(kd) < 55:
                continue
            hi = [float(x.__getitem__(2)) for x in kd]
            lo = [float(x.__getitem__(3)) for x in kd]
            cl = [float(x.__getitem__(4)) for x in kd]
            e20 = ema_series(cl, 20)
            e50 = ema_series(cl, 50)
            atr14 = atr_last(hi, lo, cl, 14)

            side = None
            if btc_bull and cl[-1] > e20[-1] and e20[-1] > e50[-1] and cl[-2] <= e20[-2]:
                side = "LONG"
                entry = cl[-1]
                risk = 2.5 * atr14
                sl = entry - risk
            elif btc_bear and cl[-1] < e20[-1] and e20[-1] < e50[-1] and cl[-2] >= e20[-2]:
                side = "SHORT"
                entry = cl[-1]
                risk = 2.5 * atr14
                sl = entry + risk

            if side is None or risk <= 0:
                continue

            st["seen"].append(uid)
            st["trades"].append({
                "symbol": sym, "inst": inst, "side": side,
                "entry": entry, "stop": sl, "risk": risk, "best": entry,
                "status": "ACTIVE", "last_chk": d_close_ms
            })
            sl_pct = (risk / entry) * 100.0
            send_tg(
                f"{TAG}\nNEW SWING SIGNAL [{side}]: {sym}\n"
                f"Entry: {entry:.6g} | 2.5x ATR SL: {sl:.6g} ({sl_pct:.2f}%)\n"
                f"Exit: Dynamic 2.5x ATR Trailing Stop (Max 3 Slots)"
            )
        except Exception as e:
            print(f"[TSMOM] Skip {sym}: {e}")

    save_state(st)

if __name__ == "__main__":
    run_once()
