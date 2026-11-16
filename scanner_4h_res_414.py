import os
import json
import time
import urllib.request
from datetime import datetime, timezone
import numpy as np
import pandas as pd

try:
    import telegram_notify
except ImportError:
    telegram_notify = None

import master_trade_logger

STRATEGY_NAME   = "4H_RES_414"
STATE_FILE      = "active_trades_4h_res_414.json"
MAX_SLOTS       = 5
MAX_PER_CLUSTER = 1
MIN_STOP_FLOOR  = 0.015
MAX_HOLD_HOURS  = 60.0
BE_TRIGGER_R    = 0.80
BE_LOCK_PCT     = 0.002

CLUSTERS = {
    "L1":    ["ETH", "SOL", "AVAX", "NEAR", "SUI", "APT"],
    "AI":    ["RENDER", "FET", "TAO", "WLD", "ARKM"],
    "DeFi":  ["UNI", "AAVE", "LDO", "LINK", "CRV", "INJ"],
    "Meme":  ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
    "Infra": ["XRP", "LTC", "BCH", "ATOM", "ARB"]
}
COIN_CLUSTER = {c: cl for cl, coins in CLUSTERS.items() for c in coins}
UNIVERSE     = ["BTC"] + list(COIN_CLUSTER.keys())


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_okx_1h(sym, pages=14):
    inst = f"{sym}-USDT-SWAP"
    rows, after = [], ""
    for _ in range(pages):
        url = f"https://okx.com{inst}&bar=1H&limit=100"
        if after:
            url += f"&after={after}"
        try:
            data = fetch_json(url).get("data", [])
        except Exception:
            data = []
        if not data:
            break
        rows.extend(data)
        last_item = data.pop()
        after = last_item.pop(0)
        time.sleep(0.08)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v", "vc", "vq", "conf"])
    df = df[df["conf"] == "1"].copy()
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)
    return df.drop_duplicates("ts").sort_values("ts").set_index("ts")[["o", "h", "l", "c", "v"]]


def fetch_kucoin_1h(sym, days=65):
    pair = f"{sym}-USDT"
    end_s = int(time.time())
    start_s = end_s - days * 86400
    url = f"https://kucoin.com{pair}&startAt={start_s}&endAt={end_s}"
    try:
        data = fetch_json(url).get("data", [])
    except Exception:
        data = []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data, columns=["ts", "o", "c", "h", "l", "v", "turnover"])
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="s", utc=True)
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)
    return df.drop_duplicates("ts").sort_values("ts").set_index("ts")[["o", "h", "l", "c", "v"]]


def load_4h_universe():
    now_utc = datetime.now(timezone.utc)
    curr_4h_open = now_utc.replace(hour=(now_utc.hour // 4) * 4, minute=0, second=0, microsecond=0)
    ohlc_4h = {}

    for sym in UNIVERSE:
        df1h = fetch_okx_1h(sym)
        if len(df1h) < 1200:
            df1h = fetch_kucoin_1h(sym)
        if len(df1h) < 1200:
            continue
        df4 = df1h.resample("4h", label="left", closed="left").agg({
            "o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"
        }).dropna()
        closed_4h = df4[df4.index < pd.Timestamp(curr_4h_open)]
        if len(closed_4h) >= 310:
            ohlc_4h[sym] = (
                closed_4h,
                float(df1h["c"].iloc[-1]),
                float(df1h["h"].iloc[-4:].max()),
                float(df1h["l"].iloc[-4:].min())
            )
    return ohlc_4h


def send_tg(msg):
    if telegram_notify and hasattr(telegram_notify, "send_alert"):
        try:
            telegram_notify.send_alert(msg)
        except Exception:
            pass
    print(msg)


def run_scanner():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] Running {STRATEGY_NAME} Scanner...")

    data = load_4h_universe()
    if "BTC" not in data:
        print("BTC data unavailable - aborting scan.")
        return

    btc_df, btc_px, _, _ = data["BTC"]
    idx = btc_df.index
    btc_c = btc_df["c"]
    btc_sma300 = btc_c.rolling(300, min_periods=150).mean().iloc[-1]
    btc_trend = 1 if btc_c.iloc[-1] > btc_sma300 else -1
    ret_b = np.log(btc_c / btc_c.shift(1))
    var_b = ret_b.rolling(180, min_periods=120).var().replace(0, np.nan)

    active = master_trade_logger.load_active_trades(STATE_FILE)
    still_active = []

    for tr in active:
        sym = tr["symbol"].replace("-USDT", "")
        if sym not in data:
            still_active.append(tr)
            continue

        _, curr_px, hi_4h, lo_4h = data[sym]
        ep = float(tr["entry_price"])
        sl = float(tr["sl"])
        tp = float(tr["tp"])
        sl_pct = float(tr["sl_pct"]) / 100.0
        direction = tr["direction"]
        d_sign = -1 if direction == "SHORT" else 1

        open_dt = datetime.strptime(tr["open_time"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        hrs_held = (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600.0

        best_px = lo_4h if direction == "SHORT" else hi_4h
        mfe_r = (d_sign * (best_px - ep) / ep) / max(sl_pct, 1e-6)
        tr["max_r"] = round(max(float(tr.get("max_r", 0.0)), mfe_r), 2)

        if not tr.get("be_moved", False) and mfe_r >= BE_TRIGGER_R:
            new_sl = ep * (1.0 - BE_LOCK_PCT) if direction == "SHORT" else ep * (1.0 + BE_LOCK_PCT)
            if (direction == "SHORT" and new_sl < sl) or (direction == "LONG" and new_sl > sl):
                sl = round(new_sl, 6)
                tr["sl"] = sl
                tr["be_moved"] = True
                send_tg(f"[{STRATEGY_NAME}] BE LOCK (+0.2%): {tr['symbol']} {direction} | New SL: {sl}")

        exit_px, close_reason = None, None
        if direction == "SHORT":
            if hi_4h >= sl:
                exit_px, close_reason = sl, ("BE_STOP" if tr.get("be_moved") else "STOP_LOSS")
            elif lo_4h <= tp:
                exit_px, close_reason = tp, "TAKE_PROFIT"
        else:
            if lo_4h <= sl:
                exit_px, close_reason = sl, ("BE_STOP" if tr.get("be_moved") else "STOP_LOSS")
            elif hi_4h >= tp:
                exit_px, close_reason = tp, "TAKE_PROFIT"

        if exit_px is None and hrs_held >= MAX_HOLD_HOURS:
            exit_px, close_reason = curr_px, "TIME_EXIT_60H"

        if exit_px is not None:
            master_trade_logger.log_trade_close(tr, exit_px, close_reason)
            send_tg(f"[{STRATEGY_NAME}] CLOSED {tr['symbol']} {direction} | {close_reason} @ {exit_px:.4f}")
        else:
            still_active.append(tr)

    if len(still_active) < MAX_SLOTS:
        open_syms = {t["symbol"].replace("-USDT", "") for t in still_active}
        cl_counts = {}
        for t in still_active:
            s = t["symbol"].replace("-USDT", "")
            cl = COIN_CLUSTER.get(s, "Other")
            cl_counts[cl] = cl_counts.get(cl, 0) + 1

        candidates = []
        for sym in COIN_CLUSTER:
            if sym in open_syms or sym not in data:
                continue
            df4, curr_px, _, _ = data[sym]
            df4 = df4.reindex(idx).ffill(limit=2)
            if df4["c"].isna().tail(200).any():
                continue

            ret_c = np.log(df4["c"] / df4["c"].shift(1))
            beta = (ret_c.rolling(180, min_periods=120).cov(ret_b) / var_b).clip(-3.0, 3.0)
            res = ret_c - beta * ret_b
            s4h = res.rolling(180, min_periods=120).std().iloc[-1]
            if not np.isfinite(s4h) or s4h <= 0:
                continue

            z3 = res.iloc[-3:].sum() / (s4h * np.sqrt(3))
            z6 = res.iloc[-6:].sum() / (s4h * np.sqrt(6))
            zv = z3 if abs(z3) >= abs(z6) else z6

            v_mean = df4["v"].iloc[-60:].mean()
            vrel = (df4["v"].iloc[-1] / v_mean) if v_mean > 0 else 99.0

            if np.isfinite(zv) and abs(zv) >= 2.2 and vrel <= 1.50:
                d_sign = -1 if zv > 0 else 1
                if d_sign != btc_trend:
                    continue
                sig_24h = s4h * np.sqrt(6)
                candidates.append((abs(zv), sym, COIN_CLUSTER[sym], d_sign, sig_24h, curr_px, zv, vrel))

        candidates.sort(key=lambda x: x, reverse=True)

        for _, sym, cl, d_sign, sig_24h, pe, zv, vrel in candidates:
            if len(still_active) >= MAX_SLOTS:
                break
            if cl_counts.get(cl, 0) >= MAX_PER_CLUSTER:
                continue

            dsl = max(MIN_STOP_FLOOR, 1.55 * sig_24h)
            dtp = 1.50 * sig_24h
            direction = "SHORT" if d_sign == -1 else "LONG"
            sl = round(pe * (1.0 + dsl) if d_sign == -1 else pe * (1.0 - dsl), 6)
            tp = round(pe * (1.0 - dtp) if d_sign == -1 else pe * (1.0 + dtp), 6)
            reason = f"Z={zv:+.2f}|VRel={vrel:.2f}|CL={cl}|BTC50={'UP' if btc_trend==1 else 'DN'}"

            new_tr = master_trade_logger.log_trade_open(
                strategy_name=STRATEGY_NAME,
                symbol=f"{sym}-USDT",
                direction=direction,
                entry_price=round(pe, 6),
                sl_price=sl,
                tp_price=tp,
                reason=reason
            )
            still_active.append(new_tr)
            cl_counts[cl] = cl_counts.get(cl, 0) + 1
            send_tg(f"[{STRATEGY_NAME}] NEW {direction} {sym}-USDT ({cl}) @ {pe:.4f} | SL: {sl} | TP: {tp} | {reason}")

    master_trade_logger.save_active_trades(STATE_FILE, still_active)
    print(f"[{STRATEGY_NAME}] Scan Complete. Active Slots: {len(still_active)}/{MAX_SLOTS}")


if __name__ == "__main__":
    run_scanner()
      
