import json, os, time, requests
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
BASE = "https://okx.com"
TG_URL = "https://api." + "telegram.org/bot" + TOKEN + "/sendMessage"
STATE_FILE = Path("active_trades_macd.json")
TAG = "[MACD-TURN 1D]"

COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
    "UNI-USDT-SWAP", "AAVE-USDT-SWAP", "BNB-USDT-SWAP", "DOT-USDT-SWAP",
    "BCH-USDT-SWAP", "LTC-USDT-SWAP", "ATOM-USDT-SWAP"
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

def closed_daily(inst_id, limit=80):
    raw = get_json("/api/v5/market/candles", {"instId": inst_id, "bar": "1Dutc", "limit": limit}).get("data", [])
    closed = []
    for r in raw:
        ts, o, h, l, c, v, vc, vq, cfm = r
        if cfm == "1":
            closed.append((int(ts), float(o), float(h), float(l), float(c)))
    closed.reverse()
    return closed

def ema_list(vals, p):
    a = 2.0 / (p + 1.0)
    out = []
    v = vals
    for x in vals:
        v = a * x + (1.0 - a) * v
        out.append(v)
    return out

def calc_macd_metrics(kd):
    hi = [h for _, _, h, _, _ in kd]
    lo = [l for _, _, _, l, _ in kd]
    cl = [c for _, _, _, _, c in kd]
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(cl))]
    atr14 = sum(trs[-14:]) / 14.0
    e50 = ema_list(cl, 50)[-1]
    e12 = ema_list(cl, 12)
    e26 = ema_list(cl, 26)
    macd = [e12[i] - e26[i] for i in range(len(cl))]
    sig = ema_list(macd, 9)
    hist_now = macd[-1] - sig[-1]
    hist_prev = macd[-2] - sig[-2]
    return cl[-1], atr14, e50, hist_prev, hist_now

def run_macd():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_ts": 0, "trades": []}
    btc_d = closed_daily("BTC-USDT-SWAP", 80)
    if len(btc_d) < 60:
        return
    latest_ts, _, _, _, _ = btc_d[-1]
    if latest_ts <= st.get("last_ts", 0):
        print("[MACD-TURN] Daily bar already processed.")
        return

    c_btc = [c for _, _, _, _, c in btc_d]
    btc_bull = c_btc[-1] > ema_list(c_btc, 50)[-1]

    still = []
    for tr in st["trades"]:
        kd = closed_daily(tr["inst"], 35)
        _, _, b_hi, b_lo, _ = kd[-1]
        _, atr14, _, _, _ = calc_macd_metrics(kd)
        if tr["side"] == "LONG":
            if b_lo <= tr["stop"]:
                r = (((tr["stop"] - tr["entry"]) / tr["entry"]) - 0.0015) / (tr["risk"] / tr["entry"])
                send_tg(f"CLOSED [{'WIN' if r > 0 else 'LOSS'}]: {tr['sym']} LONG @ {tr['stop']:.6g} | Net: {r:+.2f}R")
                continue
            tr["best"] = max(tr["best"], b_hi)
            tr["stop"] = max(tr["stop"], tr["best"] - SL_ATR * atr14)
        else:
            if b_hi >= tr["stop"]:
                r = (((tr["entry"] - tr["stop"]) / tr["entry"]) - 0.0015) / (tr["risk"] / tr["entry"])
                send_tg(f"CLOSED [{'WIN' if r > 0 else 'LOSS'}]: {tr['sym']} SHORT @ {tr['stop']:.6g} | Net: {r:+.2f}R")
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
            kd = closed_daily(inst, 80)
            if len(kd) < 60:
                continue
            px, atr14, e50, h_prev, h_now = calc_macd_metrics(kd)
            if (SL_ATR * atr14 / px) < 0.015:
                continue
            if btc_bull and px > e50 and h_prev < 0 and h_now > h_prev:
                cands.append((abs(h_now - h_prev) / atr14, "LONG", inst, px, atr14))
            elif not btc_bull and px < e50 and h_prev > 0 and h_now < h_prev:
                cands.append((abs(h_now - h_prev) / atr14, "SHORT", inst, px, atr14))
        cands.sort(reverse=True)
        for _, side, inst, px, atr14 in cands[:MAX_SLOTS - len(st["trades"])]:
            risk = SL_ATR * atr14
            sl = px - risk if side == "LONG" else px + risk
            sym = inst.replace("-USDT-SWAP", "USDT")
            st["trades"].append({"inst": inst, "sym": sym, "side": side, "entry": px, "stop": sl, "risk": risk, "best": px})
            send_tg(f"NEW MACD TURN [{side}]: {sym}\nEntry: {px:.6g} | 2.5x ATR SL: {sl:.6g} ({(risk/px)*100:.2f}%)")

    st["last_ts"] = latest_ts
    STATE_FILE.write_text(json.dumps(st, indent=2))
    print(f"[MACD-TURN] Daily scan finished. Active slots: {len(st['trades'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    run_macd()
