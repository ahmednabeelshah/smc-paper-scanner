import json
import os
import time
import pandas as pd
import requests

TOKEN = os.getenv("TG_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
FAPI_BASES = ["https://fapi.binance.com", "https://fapi1.binance.com"]
VISION_MIRROR = "https://data-api.binance.vision"
STATE_FILE = "active_trades_hybrid_609.json"
TAG = "[HYBRID_609 1H]"

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
MAX_SLOTS = 3
MAX_HOLD_HOURS = 24


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


def get_klines(symbol, interval="1h", limit=260, srv_ms=None):
    if srv_ms is None:
        srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    raw = get_json("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    closed = [r for r in raw if int(r[6]) < srv_ms]
    df = pd.DataFrame(closed, columns=["ts", "Open", "High", "Low", "Close", "Volume", "ct", "qav", "nt", "tbv", "tqv", "ig"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = df["ts"].astype(int)
    return df.set_index("ts")


def run_hybrid_609():
    srv_ms = int(get_json("/fapi/v1/time")["serverTime"])
    st = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {"active": [], "closed": [], "last_ts": 0}
    btc_df = get_klines("BTCUSDT", "1h", 260, srv_ms)
    if len(btc_df) < 210:
        return

    lts = int(btc_df.index[-1])
    still = []

    for tr in st.get("active", []):
        df = get_klines(tr["sym"], "1h", 30, srv_ms)
        if len(df) == 0 or int(df.index[-1]) <= tr.get("last_ts", tr["entry_ts"]):
            still.append(tr)
            continue

        tr["last_ts"] = int(df.index[-1])
        tr["hours"] = tr.get("hours", 0) + 1
        r = df.iloc[-1]
        ep, sl, tp, sp = tr["entry"], tr["sl"], tr["tp"], tr["stop_pct"]
        sgn = 1.0 if tr["side"] == "LONG" else -1.0
        exit_px, reason = None, None

        if tr["side"] == "LONG":
            if r["Low"] <= sl:
                exit_px, reason = sl, ("BE_STOP" if tr.get("be") else "STOP_LOSS")
            elif r["High"] >= tp:
                exit_px, reason = tp, "TAKE_PROFIT_1.8R"
            elif not tr.get("be") and (r["High"] - ep) / ep >= sp:
                tr["sl"], tr["be"] = ep, True
        else:
            if r["High"] >= sl:
                exit_px, reason = sl, ("BE_STOP" if tr.get("be") else "STOP_LOSS")
            elif r["Low"] <= tp:
                exit_px, reason = tp, "TAKE_PROFIT_1.8R"
            elif not tr.get("be") and (ep - r["Low"]) / ep >= sp:
                tr["sl"], tr["be"] = ep, True

        if exit_px is None and tr["hours"] >= MAX_HOLD_HOURS:
            exit_px, reason = float(r["Close"]), "TIME_EXIT_24H"

        if exit_px is not None:
            nr = round(((sgn * (exit_px - ep) / ep) - 0.0015) / sp, 3)
            tr.update({"exit_ts": lts, "exit_price": exit_px, "net_r": nr, "exit_reason": reason})
            st.setdefault("closed", []).append(tr)
            send_tg(f"CLOSED [{reason}]: {tr['sym']} {tr['side']} @ {exit_px:.6g} | Net: {nr:+.2f}R")
        else:
            still.append(tr)

    st["active"] = still

    if lts > st.get("last_ts", 0) and len(st["active"]) < MAX_SLOTS:
        btc_e200 = float(btc_df["Close"].ewm(span=200, adjust=False).mean().iloc[-1])
        btc_up = float(btc_df["Close"].iloc[-1]) > btc_e200

        for sym in MAJORS:
            if len(st["active"]) >= MAX_SLOTS or any(x["sym"] == sym for x in st["active"]):
                continue
            df = get_klines(sym, "1h", 260, srv_ms)
            if len(df) < 210 or int(df.index[-1]) != lts:
                continue

            cl, hi, lo, op, vo = df["Close"], df["High"], df["Low"], df["Open"], df["Volume"]
            rng = (hi - lo).replace(0, float("nan"))
            atr = (hi - lo).rolling(14).mean().iloc[-1]
            vrel = (vo / vo.rolling(20).mean()).iloc[-1]
            e200 = cl.ewm(span=200, adjust=False).mean().iloc[-1]

            d = cl.diff()
            rsi6 = 100.0 - (100.0 / (1.0 + d.clip(lower=0).ewm(alpha=1/6, adjust=False).mean() / (-d.clip(upper=0)).ewm(alpha=1/6, adjust=False).mean()))
            lo_w = ((df[["Open", "Close"]].min(axis=1) - lo) / rng).iloc[-1]
            up_w = ((hi - df[["Open", "Close"]].max(axis=1)) / rng).iloc[-1]

            c_l = btc_up and (cl.iloc[-1] > e200) and (rsi6.iloc[-2] < 25) and (lo_w >= 0.45) and (cl.iloc[-1] > op.iloc[-1]) and (vrel > 1.3)
            c_s = (not btc_up) and (cl.iloc[-1] < e200) and (rsi6.iloc[-2] > 75) and (up_w >= 0.45) and (cl.iloc[-1] < op.iloc[-1]) and (vrel > 1.3)

            if c_l or c_s:
                side = "LONG" if c_l else "SHORT"
                ep = float(cl.iloc[-1])
                sp = (2.0 * float(atr)) / ep
                sl = round(ep * (1.0 - sp) if side == "LONG" else ep * (1.0 + sp), 6)
                tp = round(ep * (1.0 + 1.80 * sp) if side == "LONG" else ep * (1.0 - 1.80 * sp), 6)
                st["active"].append({
                    "sym": sym, "inst": sym, "side": side, "entry": ep,
                    "sl": sl, "tp": tp, "stop_pct": round(sp, 5),
                    "hours": 0, "be": False, "entry_ts": lts, "last_ts": lts,
                })
                send_tg(f"NEW [{side}]: {sym} @ {ep:.6g} | SL={sl} | TP={tp}")

    st["last_ts"] = lts
    json.dump(st, open(STATE_FILE, "w"), indent=2)
    print(f"{TAG} Done. Active={len(st['active'])}/{MAX_SLOTS}")


if __name__ == "__main__":
    run_hybrid_609()
    
