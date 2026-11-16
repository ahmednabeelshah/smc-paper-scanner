import os
import json
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone

BOT_ID = "12H_REG_440"
STATE_FILE = "state_12h_reg_440.json"
LOG_FILE = "trades_12h_reg_440.csv"

SECTORS = {
    "L1": ["ETHUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT"],
    "AI": ["RENDERUSDT", "FETUSDT", "TAOUSDT", "WLDUSDT", "ARKMUSDT"],
    "DeFi": ["UNIUSDT", "AAVEUSDT", "LDOUSDT", "LINKUSDT", "CRVUSDT", "INJUSDT"],
    "Meme": ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT"],
    "Infra": ["XRPUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT", "ARBUSDT"],
}

MAX_SLOTS = 5
COOLDOWN_SEC = 24 * 3600
MAX_HOLD_SEC = 72 * 3600
FEE_RT = 0.0015

def send_tg(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[TG-DRY] {msg}")
        return
    try:
        requests.post(
            f"https://telegram.org{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG-ERR] {e}")

def fetch_12h_klines(symbol, limit=180):
    urls = [
        f"https://binance.com{symbol}&interval=12h&limit={limit}",
        f"https://binance.vision{symbol}&interval=12h&limit={limit}",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=10)
            if r.status_code == 200:
                data = r.json()
                df = pd.DataFrame(data, columns=[
                    "open_time","open","high","low","close","volume",
                    "close_time","qav","trades","tbbav","tbqav","ignore"
                ])
                for col in ["open","high","low","close","volume"]:
                    df[col] = df[col].astype(float)
                df["open_time"] = df["open_time"].astype(np.int64)
                df["close_time"] = df["close_time"].astype(np.int64)
                return df
        except Exception:
            continue
    return None

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"active": [], "cooldowns": {}, "last_bar_ts": 0}

def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

def log_trade(row):
    df = pd.DataFrame([row])
    header = not os.path.exists(LOG_FILE)
    df.to_csv(LOG_FILE, mode="a", header=header, index=False)

def main():
    now_ms = int(time.time() * 1000)
    now_sec = int(time.time())
    st = load_state()

    btc_df = fetch_12h_klines("BTCUSDT", limit=180)
    if btc_df is None or len(btc_df) < 130:
        print("Failed to fetch BTCUSDT 12H data.")
        return

    # Strictly use closed bars only (exclude active forming candle)
    btc_closed = btc_df[btc_df["close_time"] <= now_ms].copy()
    if len(btc_closed) < 125:
        print("Not enough closed BTC bars.")
        return

    last_closed_ts = int(btc_closed["open_time"].iloc[-1])
    b_close = btc_closed["close"].values
    rb = np.log(b_close[1:] / b_close[:-1])

    sma100 = np.mean(b_close[-100:])
    btc_bear = b_close[-1] <= sma100
    btc_calm = abs(b_close[-1] / b_close[-3] - 1.0) < 0.065

    # 1. Manage Active Positions on Latest Price
    still_active = []
    for tr in st["active"]:
        sym = tr["symbol"]
        kdf = fetch_12h_klines(sym, limit=5)
        if kdf is None or len(kdf) == 0:
            still_active.append(tr)
            continue
        cur_px = float(kdf["close"].iloc[-1])
        hi_px = float(kdf["high"].iloc[-1])
        lo_px = float(kdf["low"].iloc[-1])
        d = tr["dir"]
        en = tr["entry"]
        sld = tr["sl_dist"]
        tpd = tr["tp_dist"]
        csl = tr["cur_sl"]
        be = tr["be_armed"]
        held = now_sec - tr["entry_sec"]

        exit_px = None
        reason = ""
        if d == 1:
            if lo_px <= csl:
                exit_px, reason = csl, "SL" if not be else "BE"
            elif hi_px >= en + tpd:
                exit_px, reason = en + tpd, "TP"
            else:
                if not be and hi_px >= en + 0.75 * sld:
                    tr["be_armed"] = True
                    tr["cur_sl"] = en
                if held >= MAX_HOLD_SEC:
                    exit_px, reason = cur_px, "TIME_72H"
        else:
            if hi_px >= csl:
                exit_px, reason = csl, "SL" if not be else "BE"
            elif lo_px <= en - tpd:
                exit_px, reason = en - tpd, "TP"
            else:
                if not be and lo_px <= en - 0.75 * sld:
                    tr["be_armed"] = True
                    tr["cur_sl"] = en
                if held >= MAX_HOLD_SEC:
                    exit_px, reason = cur_px, "TIME_72H"

        if exit_px is not None:
            net_r = (d * (exit_px - en) / en - FEE_RT) / tr["sl_pct"]
            st["cooldowns"][sym] = now_sec + COOLDOWN_SEC
            log_trade({
                "bot": BOT_ID,
                "symbol": sym,
                "sector": tr["sector"],
                "side": "LONG" if d == 1 else "SHORT",
                "entry": round(en, 6),
                "exit": round(exit_px, 6),
                "net_r": round(net_r, 3),
                "reason": reason,
                "closed_utc": datetime.now(timezone.utc).isoformat(),
            })
            send_tg(
                f"*{BOT_ID} EXIT ({reason})*\n"
                f"Pair: `{sym}` ({tr['sector']}) | Side: `{'LONG' if d==1 else 'SHORT'}`\n"
                f"Entry: `{en:.5f}` -> Exit: `{exit_px:.5f}` | Net: `{net_r:+.2f}R`"
            )
        else:
            still_active.append(tr)

    st["active"] = still_active

    # 2. Evaluate New Signals Only Once Per New 12H Closed Bar
    if last_closed_ts > st.get("last_bar_ts", 0) and btc_calm and len(st["active"]) < MAX_SLOTS:
        cands = []
        used_sectors = {tr["sector"] for tr in st["active"]}
        used_syms = {tr["symbol"] for tr in st["active"]}

        for sec, sym_list in SECTORS.items():
            if sec in used_sectors:
                continue
            for sym in sym_list:
                if sym in used_syms or now_sec < st["cooldowns"].get(sym, 0):
                    continue
                kdf = fetch_12h_klines(sym, limit=180)
                if kdf is None:
                    continue
                closed = kdf[kdf["close_time"] <= now_ms]
                if len(closed) < 125:
                    continue

                c_arr = closed["close"].values[-125:]
                v_arr = closed["volume"].values[-125:]
                rc = np.log(c_arr[1:] / c_arr[:-1])
                rb_sub = rb[-len(rc):]

                # Strict lagged 120-bar beta (ends at t-2, applied to t-1 return)
                cov = np.cov(rc[:-1], rb_sub[:-1])[0, 1]
                var = np.var(rb_sub[:-1])
                beta = np.clip(cov / max(var, 1e-12), 0.2, 3.0)
                res = rc - beta * rb_sub

                sig_r = np.std(res[-60:], ddof=1)
                if sig_r <= 1e-8:
                    continue
                z2 = np.sum(res[-2:]) / (sig_r * np.sqrt(2.0))
                z4 = np.sum(res[-4:]) / (sig_r * np.sqrt(4.0))
                vrel = v_arr[-1] / max(np.median(v_arr[-20:]), 1e-8)

                if vrel > 1.45:
                    continue

                sl_pct = float(np.clip(1.55 * sig_r * np.sqrt(3.0), 0.015, 0.040))
                tp_pct = 1.50 * sl_pct
                if tp_pct < 0.0225:
                    continue

                entry_px = float(kdf["open"].iloc[-1])  # Open[t] of current bar
                if btc_bear and z2 >= 1.8 and z4 >= 2.0:
                    cands.append((abs(z4), -1, sym, sec, entry_px, sl_pct, tp_pct, z2, z4))
                elif (not btc_bear) and z2 <= -2.2 and z4 <= -2.4:
                    cands.append((abs(z4), 1, sym, sec, entry_px, sl_pct, tp_pct, z2, z4))

        cands.sort(key=lambda x: -x[0])
        for score, d, sym, sec, en, sl_pct, tp_pct, z2, z4 in cands:
            if len(st["active"]) >= MAX_SLOTS:
                break
            if sec in used_sectors or sym in used_syms:
                continue
            sl_dist = en * sl_pct
            tp_dist = en * tp_pct
            cur_sl = en - d * sl_dist
            tp_px = en + d * tp_dist

            st["active"].append({
                "symbol": sym,
                "sector": sec,
                "dir": d,
                "entry": en,
                "sl_pct": sl_pct,
                "sl_dist": sl_dist,
                "tp_dist": tp_dist,
                "cur_sl": cur_sl,
                "be_armed": False,
                "entry_sec": now_sec,
            })
            used_sectors.add(sec)
            used_syms.add(sym)
            send_tg(
                f"*{BOT_ID} NEW SIGNAL*\n"
                f"Pair: `{sym}` ({sec}) | Side: `{'LONG' if d==1 else 'SHORT'}`\n"
                f"Entry: `{en:.5f}` | SL: `{cur_sl:.5f}` ({sl_pct*100:.2f}%) | TP: `{tp_px:.5f}`\n"
                f"Z2: `{z2:+.2f}` | Z4: `{z4:+.2f}` | Regime: `{'BEAR' if btc_bear else 'BULL'}`"
            )

        st["last_bar_ts"] = last_closed_ts

    save_state(st)
    print(f"[{BOT_ID}] Done. Active slots: {len(st['active'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    main()
