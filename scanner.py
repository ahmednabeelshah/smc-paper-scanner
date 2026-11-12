import json, time, requests
from datetime import datetime, timezone
from pathlib import Path

TOKEN = "9218350197:AAHad9f12msIV8kor8YV6NbwWTLBDFMTe80"
CHAT_ID = "5300804129"
BASE = "https://fapi." + "binance.com"
TG_URL = "https://api." + "telegram.org/bot" + TOKEN + "/sendMessage"
STATE_FILE = Path("active_trades.json")

def get_json(path, params=None):
    for a in range(4):
        try:
            r = requests.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == 3: raise
            time.sleep(2 ** a)

def send_tg(msg):
    print("\n" + "="*50 + "\n" + msg + "\n" + "="*50)
    try: requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": msg}, timeout=15)
    except Exception as e: print("TG warn:", e)

def ema_last(vals, p):
    a = 2.0 / (p + 1.0)
    it = iter(vals)
    v = next(it)
    for x in it: v = a * x + (1.0 - a) * v
    return v

def closed_klines(sym, iv, limit, srv_ms):
    raw = get_json("/fapi/v1/klines", {"symbol": sym, "interval": iv, "limit": limit})
    return [r for r in raw if int(r) < srv_ms]

def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {"seen": [], "trades": []}

def save_state(st):
    st["seen"] = st["seen"][-300:]
    STATE_FILE.write_text(json.dumps(st, indent=2))

def track_trades(st, srv_ms):
    for tr in st["trades"]:
        if tr["status"] not in ("PENDING", "ACTIVE"): continue
        sym, side = tr["symbol"], tr["side"]
        kl = get_json("/fapi/v1/klines", {"symbol": sym, "interval": "5m", "startTime": tr["last_chk"], "limit": 400})
        if not isinstance(kl, list) or not kl: continue
        for bar in kl:
            b_open, b_hi, b_lo, b_cl, b_close_t = float(bar), float(bar), float(bar), float(bar), int(bar)
            if b_close_t > srv_ms: break
            tr["last_chk"] = b_close_t + 1
            if tr["status"] == "PENDING":
                if (side == "LONG" and b_lo <= tr["stop"]) or (side == "SHORT" and b_hi >= tr["stop"]):
                    tr["status"] = "EXPIRED_SL"
                    send_tg(f"EXPIRED (Hit SL before fill): {sym} {side}")
                    break
                if (side == "LONG" and b_lo <= tr["confirm"]) or (side == "SHORT" and b_hi >= tr["confirm"]):
                    tr["status"] = "ACTIVE"
                    tr["fill_ms"] = b_close_t
                    send_tg(f"FILLED PAPER TRADE: {sym} {side} @ {tr['entry']:.6g}\nNow tracking SL {tr['stop']:.6g} | TP1 {tr['tp1']:.6g} | TP2 {tr['tp2']:.6g}")
                elif b_close_t >= tr["exp_ms"]:
                    tr["status"] = "EXPIRED_2H"
                    send_tg(f"EXPIRED (No fill in 2h): {sym} {side}")
                    break
            if tr["status"] == "ACTIVE":
                entry, sl, tp1, tp2, be = tr["entry"], tr["stop"], tr["tp1"], tr["tp2"], tr["be"]
                if side == "LONG":
                    if b_lo <= sl:
                        rem = 0.5 if tr["hit_tp1"] else 1.0
                        pnl = tr["locked_pct"] + rem * ((min(sl, b_open) - entry) / entry * 100.0) - 0.14
                        tr["status"] = "CLOSED"
                        res = "WIN (TP1+BE)" if tr["hit_tp1"] else "LOSS (SL)"
                        send_tg(f"CLOSED [{res}]: {sym} LONG | Net: {pnl:+.2f}%")
                        break
                    if not tr["hit_tp1"] and b_hi >= tp1:
                        tr["hit_tp1"] = True
                        tr["locked_pct"] = 0.5 * ((tp1 - entry) / entry * 100.0)
                        tr["stop"] = max(sl, be)
                        send_tg(f"TP1 HIT (50% locked): {sym} LONG | Stop moved to BE {tr['stop']:.6g}")
                    if b_hi >= tp2:
                        rem = 0.5 if tr["hit_tp1"] else 1.0
                        pnl = tr["locked_pct"] + rem * ((tp2 - entry) / entry * 100.0) - 0.14
                        tr["status"] = "CLOSED"
                        send_tg(f"CLOSED [BIG WIN TP2]: {sym} LONG | Net: {pnl:+.2f}%")
                        break
                else:
                    if b_hi >= sl:
                        rem = 0.5 if tr["hit_tp1"] else 1.0
                        pnl = tr["locked_pct"] + rem * ((entry - max(sl, b_open)) / entry * 100.0) - 0.14
                        tr["status"] = "CLOSED"
                        res = "WIN (TP1+BE)" if tr["hit_tp1"] else "LOSS (SL)"
                        send_tg(f"CLOSED [{res}]: {sym} SHORT | Net: {pnl:+.2f}%")
                        break
                    if not tr["hit_tp1"] and b_lo <= tp1:
                        tr["hit_tp1"] = True
                        tr["locked_pct"] = 0.5 * ((entry - tp1) / entry * 100.0)
                        tr["stop"] = min(sl, be)
                        send_tg(f"TP1 HIT (50% locked): {sym} SHORT | Stop moved to BE {tr['stop']:.6g}")
                    if b_lo <= tp2:
                        rem = 0.5 if tr["hit_tp1"] else 1.0
                        pnl = tr["locked_pct"] + rem * ((entry - tp2) / entry * 100.0) - 0.14
                        tr["status"] = "CLOSED"
                        send_tg(f"CLOSED [BIG WIN TP2]: {sym} SHORT | Net: {pnl:+.2f}%")
                        break
                if b_close_t - tr["fill_ms"] >= 72 * 3600 * 1000:
                    rem = 0.5 if tr["hit_tp1"] else 1.0
                    g = ((b_cl - entry) / entry * 100.0) * (1.0 if side == "LONG" else -1.0)
                    pnl = tr["locked_pct"] + rem * g - 0.16
                    tr["status"] = "CLOSED"
                    send_tg(f"CLOSED [72H TIME EXIT]: {sym} {side} | Net: {pnl:+.2f}%")
                    break

def run_once():
    st = load_state()
    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    track_trades(st, srv_ms)
    now_str = datetime.fromtimestamp(srv_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    btc4 = closed_klines("BTCUSDT", "4h", 70, srv_ms)
    btc1 = closed_klines("BTCUSDT", "1h", 20, srv_ms)
    c4_b = [float(x) for x in btc4]
    e50_b = ema_last(c4_b, 50)
    b_bull, b_bear = c4_b[-1] > e50_b, c4_b[-1] < e50_b
    b_ret5 = (float(btc1[-1]) / float(btc1[-6])) - 1.0

    info = get_json("/fapi/v1/exchangeInfo")
    tickers = get_json("/fapi/v1/ticker/24hr")
    books = {b["symbol"]: b for b in get_json("/fapi/v1/ticker/bookTicker")}
    active = {s["symbol"] for s in info["symbols"] if s["status"]=="TRADING" and s["contractType"]=="PERPETUAL" and s["quoteAsset"]=="USDT"}
    top30 = sorted([t for t in tickers if t["symbol"] in active and float(t["quoteVolume"]) >= 15_000_000], key=lambda x: float(x["quoteVolume"]), reverse=True)[:30]
    print(f"[{now_str}] GitHub Cloud Scan 30 pairs | BTC 4h={'BULL' if b_bull else 'BEAR'} | Active/Pending={sum(1 for x in st['trades'] if x['status'] in ('PENDING','ACTIVE'))}")

    for t in top30:
        sym = t["symbol"]
        if sym == "BTCUSDT": continue
        try:
            bk = books.get(sym, {})
            bid, ask = float(bk.get("bidPrice", 0)), float(bk.get("askPrice", 0))
            if bid <= 0 or ((ask - bid) / bid * 100.0) >= 0.08: continue
            fund = float(get_json("/fapi/v1/premiumIndex", {"symbol": sym}).get("lastFundingRate", 0))
            if abs(fund) >= 0.0005: continue
            k4 = closed_klines(sym, "4h", 70, srv_ms)
            k1 = closed_klines(sym, "1h", 35, srv_ms)
            if len(k4) < 55 or len(k1) < 25: continue
            c4 = [float(x) for x in k4]
            e50 = ema_last(c4, 50)
            op, hi, lo, cl, vol = [float(x) for x in k1], [float(x) for x in k1], [float(x) for x in k1], [float(x) for x in k1], [float(x) for x in k1]
            avg_v = sum(vol[-22:-2]) / 20.0
            if avg_v == 0 or (vol[-2] / avg_v) < 1.5: continue
            v_mult = vol[-2] / avg_v
            c_ret5 = (cl[-1] / cl[-6]) - 1.0
            side = None
            if b_bear and c4[-1] < e50 and c_ret5 < b_ret5:
                gap = ((lo[-3] - hi[-1]) / cl[-1]) * 100.0
                if gap >= 0.35 and cl[-2] < op[-2]:
                    side, entry, sl = "SHORT", hi[-1], hi[-3] * 1.002
                    f_conf, risk, tp1, tp2, be = entry * 1.0005, sl - entry, entry - (sl - entry)*1.5, entry - (sl - entry)*3.0, entry * 0.9985
            elif b_bull and c4[-1] > e50 and c_ret5 > b_ret5:
                gap = ((lo[-1] - hi[-3]) / cl[-1]) * 100.0
                if gap >= 0.35 and cl[-2] > op[-2]:
                    side, entry, sl = "LONG", lo[-1], lo[-3] * 0.998
                    f_conf, risk, tp1, tp2, be = entry * 0.9995, entry - sl, entry + (entry - sl)*1.5, entry + (entry - sl)*3.0, entry * 1.0015
            if side is None or risk <= 0 or (risk / entry) > 0.04: continue
            c_ms = int(k1[-1])
            uid = f"{sym}_{side}_{c_ms}"
            if uid in st["seen"]: continue
            st["seen"].append(uid)
            st["trades"].append({"symbol": sym, "side": side, "entry": entry, "confirm": f_conf, "stop": sl, "tp1": tp1, "tp2": tp2, "be": be, "status": "PENDING", "last_chk": c_ms + 1, "exp_ms": c_ms + 7200000, "fill_ms": 0, "hit_tp1": False, "locked_pct": 0.0})
            send_tg(f"NEW SIGNAL [{side}]: {sym}\nEntry: {entry:.6g} | SL: {sl:.6g}\nTP1: {tp1:.6g} | TP2: {tp2:.6g}\nFVG: {gap:.2f}% | Vol: {v_mult:.2f}x")
        except Exception as e: print(f"Skip {sym}: {e}")
    save_state(st)

if __name__ == "__main__":
    run_once()
              
