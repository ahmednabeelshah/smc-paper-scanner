import os
import json
import csv
import warnings
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
import urllib.request
import urllib.parse

warnings.filterwarnings("ignore")

BOT_ID = "4H_BEAR_424"
STATE_FILE = "active_trades_4h_bear_424.json"
LOG_FILE = "master_trade_log.csv"
FEE_RT = 0.0015
RISK_PCT = 0.025
MAX_SLOTS = 5
COOLDOWN_HOURS = 24
MAX_HOLD_HOURS = 60

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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        url = f"https://telegram.org{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode("utf-8")
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"active": [], "cooldowns": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_log(row):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow([
                "bot_id", "pair", "sector", "side", "entry_time", "exit_time",
                "entry_price", "exit_price", "sl_pct", "net_r", "exit_reason"
            ])
        writer.writerow(row)

def fetch_4h_data():
    raw = yf.download(["BTC-USD"] + PAIRS, period="120d", interval="1h", progress=False, auto_adjust=True)
    O_df = raw.xs("Open", level=0, axis=1).resample("4h").first().dropna(how="all").ffill()
    H_df = raw.xs("High", level=0, axis=1).resample("4h").max().reindex(O_df.index).ffill()
    L_df = raw.xs("Low", level=0, axis=1).resample("4h").min().reindex(O_df.index).ffill()
    C_df = raw.xs("Close", level=0, axis=1).resample("4h").last().reindex(O_df.index).ffill()
    V_df = raw.xs("Volume", level=0, axis=1).resample("4h").sum().reindex(O_df.index).fillna(0.0)

    # Drop currently forming incomplete 4H bar so index -1 is strictly closed bar t-1
    now_utc = datetime.now(timezone.utc)
    bar_start = now_utc.replace(hour=(now_utc.hour // 4) * 4, minute=0, second=0, microsecond=0)
    if C_df.index[-1] >= bar_start:
        O_df, H_df, L_df, C_df, V_df = O_df.iloc[:-1], H_df.iloc[:-1], L_df.iloc[:-1], C_df.iloc[:-1], V_df.iloc[:-1]
    return O_df, H_df, L_df, C_df, V_df

def main():
    state = load_state()
    active = state.get("active", [])
    cooldowns = state.get("cooldowns", {})
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    O_df, H_df, L_df, C_df, V_df = fetch_4h_data()
    if len(C_df) < 365:
        print(f"[{BOT_ID}] Insufficient bars: {len(C_df)}")
        return

    # Live intrabar price check via 1h latest close/high/low
    live_1h = yf.download(PAIRS, period="2d", interval="1h", progress=False, auto_adjust=True)
    live_c = live_1h.xs("Close", level=0, axis=1).ffill().iloc[-1]
    live_h = live_1h.xs("High", level=0, axis=1).ffill().iloc[-1]
    live_l = live_1h.xs("Low", level=0, axis=1).ffill().iloc[-1]

    # 1. Manage Open Trades
    still_open = []
    for pos in active:
        pair = pos["pair"]
        side = pos["side"]
        pe = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        rp = pos["sl_pct"]
        open_dt = datetime.strptime(pos["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        elapsed_h = (now_utc - open_dt).total_seconds() / 3600.0

        cur_c = float(live_c.get(pair, pe))
        cur_h = float(live_h.get(pair, cur_c))
        cur_l = float(live_l.get(pair, cur_c))

        exit_p, reason = None, None
        if side == "LONG":
            if cur_l <= sl:
                exit_p, reason = sl, ("BE" if pos.get("be_done") else "SL")
            elif cur_h >= tp:
                exit_p, reason = tp, "TP"
            elif not pos.get("be_done") and (cur_h - pe) / pe >= 0.8 * rp:
                pos["sl_price"] = pe
                pos["be_done"] = True
                send_tg(f"[{BOT_ID}] BE LOCKED {pair} LONG @ {pe:.6g}")
        else:
            if cur_h >= sl:
                exit_p, reason = sl, ("BE" if pos.get("be_done") else "SL")
            elif cur_l <= tp:
                exit_p, reason = tp, "TP"
            elif not pos.get("be_done") and (pe - cur_l) / pe >= 0.8 * rp:
                pos["sl_price"] = pe
                pos["be_done"] = True
                send_tg(f"[{BOT_ID}] BE LOCKED {pair} SHORT @ {pe:.6g}")

        if exit_p is None and elapsed_h >= MAX_HOLD_HOURS:
            exit_p, reason = cur_c, "TIME"

        if exit_p is not None:
            raw_ret = (exit_p - pe) / pe if side == "LONG" else (pe - exit_p) / pe
            net_r = round((raw_ret - FEE_RT) / rp, 4)
            cooldowns[pair] = (now_utc + timedelta(hours=COOLDOWN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            append_log([
                BOT_ID, pair, pos["sector"], side, pos["entry_time"], now_iso,
                round(pe, 8), round(exit_p, 8), round(rp, 5), net_r, reason
            ])
            send_tg(f"[{BOT_ID}] EXIT {pair} {side} ({reason}) | Net: {net_r:+.2f}R")
        else:
            still_open.append(pos)

    active = still_open

    # 2. Check Bear Regime Gate on strictly closed 4H bar t-1
    btc_c = C_df["BTC-USD"].to_numpy(dtype=np.float64)
    sma300_btc = float(np.mean(btc_c[-300:]))
    if btc_c[-1] <= sma300_btc and len(active) < MAX_SLOTS:
        used_secs = {p["sector"] for p in active}
        used_coins = {p["pair"] for p in active}
        lr_b = np.log(np.maximum(btc_c[-361:] / np.maximum(btc_c[-362:-1], 1e-12), 1e-12))
        vb = float(np.var(lr_b))
        rb_dm = lr_b - np.mean(lr_b)

        cands = []
        for pair in PAIRS:
            if pair not in C_df.columns or pair in used_coins or PAIR_TO_SEC[pair] in used_secs:
                continue
            cd_str = cooldowns.get(pair)
            if cd_str and now_utc < datetime.strptime(cd_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc):
                continue

            c_arr = C_df[pair].to_numpy(dtype=np.float64)
            v_arr = V_df[pair].to_numpy(dtype=np.float64)
            if len(c_arr) < 362 or np.isnan(c_arr[-362:]).any():
                continue

            lr_i = np.log(np.maximum(c_arr[-361:] / np.maximum(c_arr[-362:-1], 1e-12), 1e-12))
            ri_dm = lr_i - np.mean(lr_i)
            beta = float(np.sum(ri_dm * rb_dm) / (360.0 * vb)) if vb > 1e-12 else 1.0

            res_180 = lr_i[-180:] - beta * lr_b[-180:]
            cum6 = float(np.sum(res_180[-6:]))
            sig = float(np.std(res_180, ddof=1))
            if sig <= 1e-12:
                continue
            z6 = cum6 / (sig * np.sqrt(6.0))

            vmed = float(np.median(v_arr[-180:]))
            vrel = v_arr[-1] / vmed if vmed > 1e-12 else 99.0
            if vrel > 1.50:
                continue

            side = None
            if z6 >= 2.0:
                side = "SHORT"
            elif z6 <= -3.2:
                side = "LONG"

            if side is not None:
                pe = float(live_c.get(pair, c_arr[-1]))
                sl_dist = float(np.clip(1.55 * sig * np.sqrt(6.0), 0.015, 0.035))
                tp_dist = (1.50 / 1.55) * sl_dist
                sl_p = pe * (1.0 + sl_dist) if side == "SHORT" else pe * (1.0 - sl_dist)
                tp_p = pe * (1.0 - tp_dist) if side == "SHORT" else pe * (1.0 + tp_dist)
                cands.append((abs(z6), pair, PAIR_TO_SEC[pair], side, pe, sl_p, tp_p, sl_dist, z6))

        cands.sort(reverse=True)
        for _, pair, sec, side, pe, sl_p, tp_p, sl_dist, z6 in cands:
            if len(active) >= MAX_SLOTS:
                break
            if sec not in used_secs and pair not in used_coins:
                active.append({
                    "bot_id": BOT_ID,
                    "pair": pair,
                    "sector": sec,
                    "side": side,
                    "entry_time": now_iso,
                    "entry_price": round(pe, 8),
                    "sl_price": round(sl_p, 8),
                    "tp_price": round(tp_p, 8),
                    "sl_pct": round(sl_dist, 5),
                    "be_done": False
                })
                used_secs.add(sec)
                used_coins.add(pair)
                send_tg(
                    f"[{BOT_ID}] ENTRY {pair} ({sec}) {side} | Z6={z6:+.2f} | "
                    f"Entry={pe:.6g} | SL={sl_p:.6g} ({sl_dist*100:.2f}%) | TP={tp_p:.6g}"
                )

    state["active"] = active
    state["cooldowns"] = cooldowns
    save_state(state)
    print(f"[{BOT_ID}] Run complete @ {now_iso} UTC | Active: {len(active)}/{MAX_SLOTS} | BTC<=SMA300: {btc_c[-1] <= sma300_btc}")

if __name__ == "__main__":
    main()
          
