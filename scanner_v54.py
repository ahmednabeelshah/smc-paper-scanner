import json, os, time, requests
from datetime import datetime, timezone
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = f"https://telegram.org{TOKEN}/sendMessage"
STATE_FILE = Path("active_trades_v54.json")

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
    msg = f"[v5.4 SNIPER]\n{msg}"
    print("\n" + "=" * 50 + "\n" + msg + "\n" + "=" * 50)
    try:
        requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("TG warn:", e)

def ema_last(vals, p):
    a = 2.0 / (p + 1.0)
    it = iter(vals)
    v = next(it)
    for x in it:
        v = a * x + (1.0 - a) * v
    return v

def to_inst(sym):
    base = sym.replace("1000", "").replace("USDT", "")
    return f"{base}-USDT-SWAP"

def closed_klines(sym, bar_iv, limit):
    raw = get_json("/api/v5/market/candles", {"instId": to_inst(sym), "bar": bar_iv, "limit": limit}).get("data", [])
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
    st["seen"] = st["seen"][-300:]
    STATE_FILE.write_text(json.dumps(st, indent=2))

def track_trades(st, srv_ms):
    for tr in st["trades"]:
        if tr["status"] not in ("PENDING", "ACTIVE"):
            continue
        sym = tr["symbol"]
        kl = get_json("/api/v5/market/candles", {"instId": to_inst(sym), "bar": "5m", "limit": 100}).get("data", [])
        if not kl:
            continue
        kl.reverse()
        for bar in kl:
            b_open = float(bar.__getitem__(1))
            b_hi = float(bar.__getitem__(2))
            b_lo = float(bar.__getitem__(3))
            b_cl = float(bar.__getitem__(4))
            b_close_t = int(bar.__getitem__(0)) + 300000
            if bar.__getitem__(8) != "1" or b_close_t <= tr["last_chk"]:
                continue
            tr["last_chk"] = b_close_t

            if tr["status"] == "PENDING":
                if b_hi >= tr["stop"]:
                    tr["status"] = "EXPIRED_SL"
                    send_tg(f"[v5.4] EXPIRED (Hit SL before fill): {sym} SHORT")
                    break
                if b_hi >= tr["confirm"]:
                    tr["status"] = "ACTIVE"
                    tr["fill_ms"] = b_close_t
                    send_tg(f"[v5.4] FILLED PAPER TRADE: {sym} SHORT @ {tr['entry']:.6g}\nSL {tr['stop']:.6g} | TP1 {tr['tp1']:.6g} | TP2 {tr['tp2']:.6g}")
                elif b_close_t >= tr["exp_ms"]:
                    tr["status"] = "EXPIRED_2H"
                    send_tg(f"[v5.4] EXPIRED (No fill in 2h): {sym} SHORT")
                    break
            if tr["status"] == "ACTIVE":
                entry, sl, tp1, tp2, be = tr["entry"], tr["stop"], tr["tp1"], tr["tp2"], tr["be"]
                trig_22 = tr.get("trig_22", entry - (entry - tp1) * (2.2 / 1.5))
                lock_10 = tr.get("lock_10", entry - (entry - tp1) * (1.0 / 1.5))

                if b_hi >= sl:
                    rem = 0.5 if tr["hit_tp1"] else 1.0
                    pnl = tr["locked_pct"] + rem * ((entry - max(sl, b_open)) / entry * 100.0) - 0.14
                    tr["status"] = "CLOSED"
                    res = "WIN (TRAIL/BE)" if tr["hit_tp1"] else "LOSS (SL)"
                    send_tg(f"[v5.4] CLOSED [{res}]: {sym} SHORT | Net: {pnl:+.2f}%")
                    break
                if not tr["hit_tp1"] and b_lo <= tp1:
                    tr["hit_tp1"] = True
                    tr["locked_pct"] = 0.5 * ((entry - tp1) / entry * 100.0)
                    tr["stop"] = min(sl, be)
                    send_tg(f"[v5.4] TP1 HIT (50% locked): {sym} SHORT | Stop -> BE {tr['stop']:.6g}")
                if tr["hit_tp1"] and not tr.get("trailed_22", False) and b_lo <= trig_22:
                    tr["trailed_22"] = True
                    tr["stop"] = min(tr["stop"], lock_10)
                    send_tg(f"[v5.4] 2.2R TRAIL LOCKED (+1.0R): {sym} SHORT | Stop -> {tr['stop']:.6g}")
                if b_lo <= tp2:
                    rem = 0.5 if tr["hit_tp1"] else 1.0
                    pnl = tr["locked_pct"] + rem * ((entry - tp2) / entry * 100.0) - 0.14
                    tr["status"] = "CLOSED"
                    send_tg(f"[v5.4] CLOSED [BIG WIN TP2]: {sym} SHORT | Net: {pnl:+.2f}%")
                    break
                if b_close_t - tr["fill_ms"] >= 72 * 3600 * 1000:
                    rem = 0.5 if tr["hit_tp1"] else 1.0
                    g = (entry - b_cl) / entry * 100.0
                    pnl = tr["locked_pct"] + rem * g - 0.16
                    tr["status"] = "CLOSED"
                    send_tg(f"[v5.4] CLOSED [72H TIME EXIT]: {sym} SHORT | Net: {pnl:+.2f}%")
                    break

def run_once():
    st = load_state()
    srv_ms = int(get_json("/api/v5/public/time")["data"].__getitem__(0)["ts"])
    track_trades(st, srv_ms)

    dt_utc = datetime.fromtimestamp(srv_ms / 1000, timezone.utc)
    now_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not (8 <= dt_utc.hour <= 18):
        print(f"[{now_str}] [v5.4] Outside London+NY window (08-18 UTC). Tracking exits only.")
        save_state(st)
        return

    open_slots = sum(1 for x in st["trades"] if x["status"] in ("PENDING", "ACTIVE"))
    if open_slots >= 3:
        print(f"[{now_str}] [v5.4] Max 3 portfolio slots full ({open_slots}/3).")
        save_state(st)
        return

    btc4 = closed_klines("BTCUSDT", "4H", 70)
    btc1 = closed_klines("BTCUSDT", "1H", 20)
    c4_b = [float(x.__getitem__(4)) for x in btc4]
    e50_b = ema_last(c4_b, 50)
    b_bear = c4_b[-1] < e50_b
    b_ret5 = (float(btc1[-1].__getitem__(4)) / float(btc1[-6].__getitem__(4))) - 1.0

    if not b_bear:
        print(f"[{now_str}] [v5.4] BTC 4H >= EMA50 (Non-Bear). Standing down.")
        save_state(st)
        return

    tickers = get_json("/api/v5/market/tickers", {"instType": "SWAP"}).get("data", [])
    usdt_swaps = [t for t in tickers if t["instId"].endswith("-USDT-SWAP") and float(t.get("volCcy24h", 0)) * float(t.get("last", 0)) >= 15_000_000]
    top30 = sorted(usdt_swaps, key=lambda x: float(x.get("volCcy24h", 0)) * float(x.get("last", 0)), reverse=True)[:30]
    print(f"[{now_str}] [v5.4] Scan {len(top30)} pairs | BTC 4h=BEAR | Slots={open_slots}/3")

    new_candidates = []
    for t in top30:
        inst = t["instId"]
        if inst == "BTC-USDT-SWAP":
            continue
        sym = inst.replace("-USDT-SWAP", "USDT")
        if any(x["symbol"] == sym and x["status"] in ("PENDING", "ACTIVE") for x in st["trades"]):
            continue
        try:
            bid, ask = float(t.get("bidPx", 0)), float(t.get("askPx", 0))
            if bid <= 0 or ((ask - bid) / bid * 100.0) >= 0.08:
                continue
            f_data = get_json("/api/v5/public/funding-rate", {"instId": inst}).get("data", [])
            fund = float(f_data.__getitem__(0)["fundingRate"]) if f_data else 0.0
            if abs(fund) >= 0.0005:
                continue

            k4 = closed_klines(sym, "4H", 70)
            k1 = closed_klines(sym, "1H", 35)
            if len(k4) < 55 or len(k1) < 25:
                continue

            c4 = [float(x.__getitem__(4)) for x in k4]
            e20_4h = ema_last(c4, 20)
            e50_4h = ema_last(c4, 50)
            if not (c4[-1] < e50_4h and e20_4h < e50_4h):
                continue

            op = [float(x.__getitem__(1)) for x in k1]
            hi = [float(x.__getitem__(2)) for x in k1]
            lo = [float(x.__getitem__(3)) for x in k1]
            cl = [float(x.__getitem__(4)) for x in k1]
            vol = [float(x.__getitem__(7)) for x in k1]

            avg_v = sum(vol[-22:-2]) / 20.0
            if avg_v == 0 or (vol[-2] / avg_v) < 1.5:
                continue
            v_mult = vol[-2] / avg_v
            c_ret5 = (cl[-1] / cl[-6]) - 1.0
            if c_ret5 >= b_ret5:
                continue

            sw_low6 = min(lo[-7:-1])
            if cl[-1] >= sw_low6:
                continue

            gap = ((lo[-3] - hi[-1]) / cl[-1]) * 100.0
            if gap >= 0.35 and cl[-2] < op[-2]:
                entry = hi[-1]
                sl = hi[-3] * 1.002
                risk = sl - entry
                if risk <= 0 or (risk / entry) > 0.04:
                    continue
                c_ms = int(k1[-1].__getitem__(0)) + 3600000
                uid = f"{sym}_SHORT_{c_ms}"
                if uid in st["seen"]:
                    continue
                new_candidates.append((v_mult, sym, uid, c_ms, entry, sl, risk, gap))
        except Exception as e:
            print(f"[v5.4] Skip {sym}: {e}")

    new_candidates.sort(reverse=True)
    for v_mult, sym, uid, c_ms, entry, sl, risk, gap in new_candidates:
        if sum(1 for x in st["trades"] if x["status"] in ("PENDING", "ACTIVE")) >= 3:
            break
        st["seen"].append(uid)
        f_conf = entry * 1.0005
        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 3.0
        be = entry * 0.9985
        trig_22 = entry - risk * 2.2
        lock_10 = entry - risk * 1.0
        st["trades"].append({
            "symbol": sym, "side": "SHORT", "entry": entry, "confirm": f_conf,
            "stop": sl, "tp1": tp1, "tp2": tp2, "be": be,
            "trig_22": trig_22, "lock_10": lock_10, "trailed_22": False,
            "status": "PENDING", "last_chk": c_ms, "exp_ms": c_ms + 7200000,
            "fill_ms": 0, "hit_tp1": False, "locked_pct": 0.0
        })
        send_tg(f"[v5.4] NEW SIGNAL [SHORT]: {sym}\nEntry: {entry:.6g} | SL: {sl:.6g}\nTP1: {tp1:.6g} | TP2: {tp2:.6g}\nFVG: {gap:.2f}% | Vol: {v_mult:.2f}x")

    save_state(st)

if __name__ == "__main__":
    run_once()
