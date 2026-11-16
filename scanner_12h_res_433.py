import os, json, csv, time, warnings
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
warnings.filterwarnings("ignore")

BOT_ID = "12H_RES_433"
STATE_FILE = "active_trades_12h_res_433.json"
LOG_FILE = "master_trade_log.csv"
FEE_RT, MAX_SLOTS, COOLDOWN_HOURS, MAX_HOLD_HOURS = 0.0015, 5, 24, 72

SECTORS = {
    "L1": ["ETH-USD", "SOL-USD", "AVAX-USD", "NEAR-USD", "SUI20947-USD", "APT21794-USD"],
    "AI": ["RENDER-USD", "FET-USD", "TAO22974-USD", "WLD-USD", "ARKM-USD"],
    "DeFi": ["UNI7083-USD", "AAVE-USD", "LDO-USD", "LINK-USD", "CRV-USD", "INJ-USD"],
    "Meme": ["DOGE-USD", "SHIB-USD", "PEPE24478-USD", "WIF-USD", "BONK-USD", "FLOKI-USD"],
    "Infra": ["XRP-USD", "LTC-USD", "BCH-USD", "ATOM-USD", "ARB-USD"]
}
PAIRS = [p for plist in SECTORS.values() for p in plist]
PAIR_TO_SEC = {p: s for s, plist in SECTORS.items() for p in plist}

def send_tg(msg):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "").strip()
    if token and chat_id:
        try:
            requests.post(f"https://telegram.org{token}/sendMessage", data={"chat_id": chat_id, "text": msg}, timeout=10)
        except Exception:
            pass

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"active": [], "cooldowns": {}, "last_entry_bar": ""}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_log(row):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["bot_id", "pair", "sector", "side", "entry_time", "exit_time", "entry_price", "exit_price", "sl_pct", "net_r", "exit_reason"])
        w.writerow(row)

def fetch_1h(ticker, rng="120d"):
    url = f"https://yahoo.com{ticker}"
    for _ in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, params={"interval": "1h", "range": rng}, timeout=15)
            if r.status_code == 200:
                res = r.json().get("chart", {}).get("result", [])
                if not res: return None
                r0 = res[0]
                q = r0.get("indicators", {}).get("quote", [{}])[0]
                df = pd.DataFrame({"High": q.get("high", []), "Low": q.get("low", []), "Close": q.get("close", []), "Volume": q.get("volume", [])}, index=pd.to_datetime(r0.get("timestamp", []), unit="s", utc=True))
                return df.dropna(subset=["Close"])
        except Exception:
            time.sleep(0.5)
    return None

def main():
    state = load_state()
    active, cooldowns, last_bar = state.get("active", []), state.get("cooldowns", {}), state.get("last_entry_bar", "")
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    bar_start = now_utc.replace(hour=(now_utc.hour // 12) * 12, minute=0, second=0, microsecond=0)
    bar_key = bar_start.strftime("%Y-%m-%d %H:%M")
    in_entry = (now_utc.hour % 12 == 0) and (now_utc.minute < 15) and (last_bar != bar_key)

    if not active and not in_entry:
        print(f"[{BOT_ID}] Idle @ {now_iso} UTC")
        return

    rng = "120d" if in_entry else "5d"
    data_1h, live_bar = {}, {}
    active_set = {p["pair"] for p in active}
    for pair in PAIRS:
        if not in_entry and pair not in active_set: continue
        df = fetch_1h(pair, rng)
        if df is None or len(df) < 5: continue
        live_bar[pair] = (float(df["Close"].iloc[-1]), float(df["High"].iloc[-1]), float(df["Low"].iloc[-1]))
        if in_entry: data_1h[pair] = df

    still_open = []
    for pos in active:
        pair, side, pe, sl, tp, rp = pos["pair"], pos["side"], pos["entry_price"], pos["sl_price"], pos["tp_price"], pos["sl_pct"]
        elapsed_h = (now_utc - datetime.strptime(pos["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
        cur_c, cur_h, cur_l = live_bar.get(pair, (pe, pe, pe))
        exit_p, reason = None, None
        if side == "LONG":
            if cur_l <= sl: exit_p, reason = sl, ("BE" if pos.get("be_done") else "SL")
            elif cur_h >= tp: exit_p, reason = tp, "TP"
            elif not pos.get("be_done") and (cur_h - pe) / pe >= 0.8 * rp:
                pos["sl_price"], pos["be_done"] = pe, True
                send_tg(f"[{BOT_ID}] BE LOCKED {pair} LONG @ {pe:.6g}")
        else:
            if cur_h >= sl: exit_p, reason = sl, ("BE" if pos.get("be_done") else "SL")
            elif cur_l <= tp: exit_p, reason = tp, "TP"
            elif not pos.get("be_done") and (pe - cur_l) / pe >= 0.8 * rp:
                pos["sl_price"], pos["be_done"] = pe, True
                send_tg(f"[{BOT_ID}] BE LOCKED {pair} SHORT @ {pe:.6g}")
        if exit_p is None and elapsed_h >= MAX_HOLD_HOURS: exit_p, reason = cur_c, "TIME"
        if exit_p is not None:
            raw_ret = (exit_p - pe) / pe if side == "LONG" else (pe - exit_p) / pe
            net_r = round((raw_ret - FEE_RT) / rp, 4)
            cooldowns[pair] = (now_utc + timedelta(hours=COOLDOWN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            append_log([BOT_ID, pair, pos["sector"], side, pos["entry_time"], now_iso, round(pe, 8), round(exit_p, 8), round(rp, 5), net_r, reason])
            send_tg(f"[{BOT_ID}] EXIT {pair} {side} ({reason}) | Net: {net_r:+.2f}R")
        else:
            still_open.append(pos)
    active = still_open

    if in_entry:
        state["last_entry_bar"] = bar_key
        btc_1h = fetch_1h("BTC-USD", "120d")
        if btc_1h is not None and len(btc_1h) >= 1500:
            btc_12h = btc_1h["Close"].resample("12h").last().ffill()
            if btc_12h.index[-1] >= bar_start: btc_12h = btc_12h.iloc[:-1]
            btc_c = btc_12h.to_numpy(dtype=np.float64)
            if len(btc_c) >= 125 and abs(btc_c[-1] / max(btc_c[-3], 1e-12) - 1.0) < 0.060 and len(active) < MAX_SLOTS:
                used_secs, used_coins = {p["sector"] for p in active}, {p["pair"] for p in active}
                lr_b = np.log(np.maximum(btc_c[-121:] / np.maximum(btc_c[-122:-1], 1e-12), 1e-12))
                vb, rb_dm = float(np.var(lr_b)), lr_b - np.mean(lr_b)
                cands = []
                for pair, df in data_1h.items():
                    if pair in used_coins or PAIR_TO_SEC[pair] in used_secs: continue
                    cd_str = cooldowns.get(pair)
                    if cd_str and now_utc < datetime.strptime(cd_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc): continue
                    c_arr = df["Close"].resample("12h").last().reindex(btc_12h.index).ffill().to_numpy(dtype=np.float64)
                    v_arr = df["Volume"].resample("12h").sum().reindex(btc_12h.index).fillna(0.0).to_numpy(dtype=np.float64)
                    if len(c_arr) < 122 or np.isnan(c_arr[-122:]).any(): continue
                    lr_i = np.log(np.maximum(c_arr[-121:] / np.maximum(c_arr[-122:-1], 1e-12), 1e-12))
                    beta = float(np.sum((lr_i - np.mean(lr_i)) * rb_dm) / (120.0 * vb)) if vb > 1e-12 else 1.0
                    res_60 = lr_i[-60:] - beta * lr_b[-60:]
                    sig = float(np.std(res_60, ddof=1))
                    if sig <= 1e-12: continue
                    z2 = float(np.sum(res_60[-2:])) / (sig * np.sqrt(2.0))
                    z4 = float(np.sum(res_60[-4:])) / (sig * np.sqrt(4.0))
                    vmed = float(np.median(v_arr[-60:]))
                    if (v_arr[-1] / vmed if vmed > 1e-12 else 99.0) > 1.50: continue
                    side = "SHORT" if (z2 >= 2.2 and z4 >= 2.2) else ("LONG" if (z2 <= -2.8 and z4 <= -2.8) else None)
                    if side:
                        pe = live_bar.get(pair, (c_arr[-1], 0, 0))[0]
                        sl_dist = float(np.clip(1.55 * sig * np.sqrt(2.0), 0.015, 0.040))
                        tp_dist = 1.50 * sl_dist
                        if tp_dist >= 0.020:
                            sl_p = pe * (1.0 + sl_dist) if side == "SHORT" else pe * (1.0 - sl_dist)
                            tp_p = pe * (1.0 - tp_dist) if side == "SHORT" else pe * (1.0 + tp_dist)
                            cands.append((abs(z4), pair, PAIR_TO_SEC[pair], side, pe, sl_p, tp_p, sl_dist, z2, z4))
                cands.sort(reverse=True)
                for _, pair, sec, side, pe, sl_p, tp_p, sl_dist, z2, z4 in cands:
                    if len(active) >= MAX_SLOTS: break
                    if sec not in used_secs and pair not in used_coins:
                        active.append({"bot_id": BOT_ID, "pair": pair, "sector": sec, "side": side, "entry_time": now_iso, "entry_price": round(pe, 8), "sl_price": round(sl_p, 8), "tp_price": round(tp_p, 8), "sl_pct": round(sl_dist, 5), "be_done": False})
                        used_secs.add(sec); used_coins.add(pair)
                        send_tg(f"[{BOT_ID}] ENTRY {pair} ({sec}) {side} | Z2={z2:+.2f}, Z4={z4:+.2f} | Entry={pe:.6g} | SL={sl_p:.6g} ({sl_dist*100:.2f}%) | TP={tp_p:.6g}")

    state["active"], state["cooldowns"] = active, cooldowns
    save_state(state)
    print(f"[{BOT_ID}] Run complete @ {now_iso} UTC | Active: {len(active)}/{MAX_SLOTS}")

if __name__ == "__main__":
    main()
