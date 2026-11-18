import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

TOKEN = os.getenv("TG_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
FAPI_BASES = ["https://fapi.binance.com", "https://fapi1.binance.com"]
VISION_MIRROR = "https://data-api.binance.vision"
STATE_FILE = Path("active_trades_tsmom.json")
TAG = "[TSMOM 1D UPG]"

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "UNIUSDT", "AAVEUSDT", "BNBUSDT", "DOTUSDT",
    "BCHUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "INJUSDT", "TIAUSDT", "FILUSDT", "ETCUSDT",
]

MAX_SLOTS = 3
MAX_HOLD_DAYS = 20
INIT_SL_ATR = 2.5
TRAIL_ATR = 2.0
MIN_VOL_REL = 1.1
MIN_STOP_PCT = 0.015
FEE_ROUND_TRIP = 0.0015


def get_json(path, params=None):
    for base in FAPI_BASES:
        try:
            r = requests.get(base + path, params=params, timeout=10)
            if r.status_code == 451:
                break
            r.raise_for_status()
            return r.json()
        except Exception:
            continue

    v_path = path.replace("/fapi/v1/time", "/api/v3/time").replace("/fapi/v1/klines", "/api/v3/klines")
    for a in range(3):
        try:
            r = requests.get(VISION_MIRROR + v_path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == 2:
                raise
            time.sleep(2 ** a)


def send_tg(msg):
    if not TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": f"{TAG}\n{msg}"}, timeout=10)
    except Exception:
        pass


def ema_series(vals, p):
    a = 2.0 / (p + 1.0)
    out, v = [], vals[0]
    for x in vals:
        v = a * x + (1.0 - a) * v
        out.append(v)
    return out


def atr_last(hi, lo, cl, p=14):
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(cl))]
    return sum(trs[-p:]) / float(p)


def closed_klines(sym, interval="1d", limit=80, srv_ms=None):
    if srv_ms is None:
        srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    raw = get_json("/fapi/v1/klines", {"symbol": sym, "interval": interval, "limit": limit})
    return [r for r in raw if int(r[6]) < srv_ms]


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen": [], "active": [], "closed": []}


def save_state(st):
    st["seen"] = st.get("seen", [])[-300:]
    STATE_FILE.write_text(json.dumps(st, indent=2))


def manage_open_tsmom(st, srv_ms):
    still = []
    for tr in st.get("active", []):
        sym = tr["sym"]
        kd = closed_klines(sym, "1d", 30, srv_ms)
        if len(kd) < 20:
            still.append(tr)
            continue

        latest_ts = int(kd[-1][0])
        if latest_ts <= tr.get("last_ts", tr["entry_ts"]):
            still.append(tr)
            continue

        tr["last_ts"] = latest_ts
        tr["days"] = tr.get("days", 0) + 1
        hi = [float(x[2]) for x in kd]
        lo = [float(x[3]) for x in kd]
        cl = [float(x[4]) for x in kd]
        atr14 = atr_last(hi, lo, cl, 14)

        b_hi, b_lo, b_cl = hi[-1], lo[-1], cl[-1]
        entry, sl, risk = tr["entry"], tr["stop"], tr["risk"]
        exit_px, reason = None, None

        if tr["side"] == "LONG":
            if b_lo <= sl:
                exit_px, reason = sl, "ATR_TRAIL_SL"
            else:
                tr["best"] = max(tr["best"], b_hi)
                tr["stop"] = max(sl, tr["best"] - TRAIL_ATR * atr14)
        else:
            if b_hi >= sl:
                exit_px, reason = sl, "ATR_TRAIL_SL"
            else:
                tr["best"] = min(tr["best"], b_lo)
                tr["stop"] = min(sl, tr["best"] + TRAIL_ATR * atr14)

        if exit_px is None and tr["days"] >= MAX_HOLD_DAYS:
            exit_px, reason = b_cl, "20D_TIME_EXIT"

        if exit_px is not None:
            sgn = 1.0 if tr["side"] == "LONG" else -1.0
            net_r = round(((sgn * (exit_px - entry) / entry) - FEE_ROUND_TRIP) / (risk / entry), 3)
            tr.update({"exit_ts": latest_ts, "exit_price": exit_px, "net_r": net_r, "exit_reason": reason})
            st.setdefault("closed", []).append(tr)
            send_tg(f"CLOSED [{reason}]: {sym} {tr['side']} @ {exit_px:.6g} | Net: {net_r:+.2f}R")
        else:
            still.append(tr)
    st["active"] = still


def run_once():
    st = load_state()
    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    manage_open_tsmom(st, srv_ms)

    if len(st["active"]) >= MAX_SLOTS:
        save_state(st)
        return

    btc_d = closed_klines("BTCUSDT", "1d", 70, srv_ms)
    if len(btc_d) < 55:
        save_state(st)
        return

    c_btc = [float(x[4]) for x in btc_d]
    e50_btc = ema_series(c_btc, 50)[-1]
    btc_bull = c_btc[-1] > e50_btc
    d_ts = int(btc_d[-1][0])

    cands = []
    for sym in COINS:
        if any(x["sym"] == sym for x in st["active"]):
            continue
        uid = f"{sym}_TSMOM_{d_ts}"
        if uid in st["seen"]:
            continue

        kd = closed_klines(sym, "1d", 70, srv_ms)
        if len(kd) < 55 or int(kd[-1][0]) != d_ts:
            continue

        hi = [float(x[2]) for x in kd]
        lo = [float(x[3]) for x in kd]
        cl = [float(x[4]) for x in kd]
        vo = [float(x[5]) for x in kd]

        v_avg20 = sum(vo[-20:]) / 20.0
        vrel = (vo[-1] / v_avg20) if v_avg20 > 0 else 0.0
        if vrel < MIN_VOL_REL:
            continue

        e20 = ema_series(cl, 20)
        e50 = ema_series(cl, 50)
        atr14 = atr_last(hi, lo, cl, 14)
        entry = cl[-1]
        risk = INIT_SL_ATR * atr14
        if (risk / entry) < MIN_STOP_PCT:
            continue

        side = None
        if btc_bull and cl[-1] > e20[-1] > e50[-1] and cl[-2] <= e20[-2]:
            side = "LONG"
        elif (not btc_bull) and cl[-1] < e20[-1] < e50[-1] and cl[-2] >= e20[-2]:
            side = "SHORT"

        if side:
            cands.append((vrel, uid, sym, side, entry, risk))

    for vrel, uid, sym, side, entry, risk in sorted(cands, reverse=True)[:MAX_SLOTS - len(st["active"])]:
        sl = entry - risk if side == "LONG" else entry + risk
        st["seen"].append(uid)
        st["active"].append({
            "sym": sym, "inst": sym, "side": side,
            "entry": entry, "stop": sl, "sl": sl, "risk": risk,
            "stop_pct": round(risk / entry, 5), "best": entry,
            "days": 0, "entry_ts": d_ts, "last_ts": d_ts, "vol_rel": round(vrel, 2),
        })
        send_tg(f"NEW [{side}]: {sym} @ {entry:.6g} | SL={sl:.6g} | VolRel={vrel:.2f}x")

    save_state(st)
    print(f"{TAG} Done. Active={len(st['active'])}/{MAX_SLOTS}")


if __name__ == "__main__":
    run_once()
    
