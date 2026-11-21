import json
import os
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

# === USER CONFIGURATION ===
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "100.0"))  # Update weekly or via secret/var
MARGIN_BASE_USD = float(os.getenv("MARGIN_BASE_USD", "8.0")) # $8.0 for 8% compound, or $5.0 for fixed
COMPOUND = os.getenv("COMPOUND", "true").lower() == "true"   # True = 8% of equity, False = fixed $
MAX_SLOTS = 6
TARGET_MARGIN_LOSS = 0.28  # ~28% of margin lost at Stop-Loss
STATE_FILE = "alert_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SYMBOLS = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "UNIUSDT",
    "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "SUIUSDT", "TIAUSDT", "SEIUSDT", "RENDERUSDT", "FETUSDT", "WIFUSDT",
]
FAPI_HOSTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]


def fetch_json(path, params):
    for host in FAPI_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=12)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    raise RuntimeError(f"All Binance endpoints failed for {path} ({params})")


def get_4h_df(symbol, limit=120):
    raw = fetch_json("/fapi/v1/klines", {"symbol": symbol, "interval": "4h", "limit": limit})
    cols = ["ot", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tqv", "ig"]
    df = pd.DataFrame(raw, columns=cols)
    for col in ["o", "h", "l", "c", "v", "tbv"]:
        df[col] = df[col].astype(float)
    df["ot"] = df["ot"].astype(np.int64)
    return df


def get_funding_4h(symbol, ot_series):
    raw = fetch_json("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 60})
    if not raw:
        return np.zeros(len(ot_series), dtype=float)
    f_df = pd.DataFrame(raw)
    f_df["ft"] = f_df["fundingTime"].astype(np.int64)
    f_df["fr"] = f_df["fundingRate"].astype(float)
    f_df = f_df.sort_values("ft")
    out = np.full(len(ot_series), np.nan, dtype=float)
    for i, ot in enumerate(ot_series):
        sub = f_df[f_df["ft"] <= ot + 4 * 3600_000]
        if not sub.empty:
            out[i] = float(sub["fr"].iloc[-1])
    return pd.Series(out).ffill().fillna(0.0).to_numpy()


def wilder_atr(h, l, c, n=14):
    prev_c = np.roll(c, 1)
    prev_c[0] = np.nan
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = np.full(len(c), np.nan, dtype=float)
    if len(c) <= n:
        return atr
    cur = float(np.nanmean(tr[1 : n + 1]))
    atr[n] = cur
    for i in range(n + 1, len(c)):
        cur = (tr[i] + (n - 1) * cur) / n
        atr[i] = cur
    return atr


def fractal_highs(h):
    n = len(h)
    is_sh = np.zeros(n, dtype=bool)
    conf = np.full(n, np.nan, dtype=float)
    for k in range(2, n - 2):
        if h[k] > h[k - 1] and h[k] > h[k - 2] and h[k] > h[k + 1] and h[k] > h[k + 2]:
            is_sh[k] = True
            conf[k + 2] = h[k]
    return is_sh, pd.Series(conf).ffill().to_numpy()


def fractal_lows(l):
    n = len(l)
    is_sl = np.zeros(n, dtype=bool)
    for k in range(2, n - 2):
        if l[k] < l[k - 1] and l[k] < l[k - 2] and l[k] < l[k + 1] and l[k] < l[k + 2]:
            is_sl[k] = True
    return is_sl


def evaluate_symbol(sym, btc_closed_c, btc_ref_hi):
    df = get_4h_df(sym, limit=120)
    entry_px = float(df["o"].iloc[-1])
    bar_open_ms = int(df["ot"].iloc[-2])
    closed = df.iloc[:-1].reset_index(drop=True)

    o = closed["o"].to_numpy()
    h = closed["h"].to_numpy()
    l = closed["l"].to_numpy()
    c = closed["c"].to_numpy()
    v = closed["v"].to_numpy()
    tbv = closed["tbv"].to_numpy()
    s = len(c) - 1

    atr = wilder_atr(h, l, c, 14)
    if not np.isfinite(atr[s]) or atr[s] <= 0:
        return []

    is_sh, ref_hi = fractal_highs(h)
    is_sl = fractal_lows(l)
    tbr = np.clip(tbv / np.where(v > 0, v, np.nan), 0.0, 1.0)
    f_arr = get_funding_4h(sym, closed["ot"].to_numpy())
    f_med8 = pd.Series(f_arr).rolling(8, min_periods=4).median().to_numpy()

    signals = []

    # Model 1: DS_NoBE_2.5R
    uwick = h[s] - max(o[s], c[s])
    body = abs(c[s] - o[s])
    btc_ok = (not np.isfinite(btc_ref_hi)) or (btc_closed_c <= btc_ref_hi)
    if (
        np.isfinite(ref_hi[s])
        and h[s] > ref_hi[s]
        and c[s] < ref_hi[s]
        and uwick > body
        and c[s] < o[s]
        and tbr[s] >= 0.50
        and f_arr[s] >= f_med8[s]
        and btc_ok
    ):
        raw_sl = max(h[s], ref_hi[s]) + 0.25 * atr[s]
        signals.append({
            "model": "DS_NoBE_2.5R",
            "symbol": sym,
            "side": "SHORT",
            "bar_ms": bar_open_ms,
            "entry": entry_px,
            "raw_sl": raw_sl,
            "tp_r": 2.5,
            "max_h": 72,
            "score": uwick / max(body, 1e-9),
        })

    # Model 2: DeepSeek_Breaker_Strict
    sh_i = np.flatnonzero(is_sh)
    sl_i = np.flatnonzero(is_sl)
    c_sh = sh_i[sh_i <= s - 4]
    c_sl = sl_i[sl_i <= s - 4]
    if len(c_sh) >= 1 and len(c_sl) >= 1:
        r_h, r_l = int(c_sh[-1]), int(c_sl[-1])
        raid_bars = [k for k in range(r_h + 1, s - 1) if h[k] > h[r_h]]
        if raid_bars:
            rk = raid_bars[0]
            if (
                c[s - 1] < l[r_l]
                and h[s] >= l[r_h]
                and c[s] < h[r_h]
                and (h[rk] - c[s - 1]) >= 1.5 * atr[s]
                and h[r_h] >= np.nanmax(h[max(0, r_h - 10) : r_h + 1])
            ):
                raw_sl = h[rk] + 0.25 * atr[s]
                signals.append({
                    "model": "DeepSeek_Breaker_Strict",
                    "symbol": sym,
                    "side": "SHORT",
                    "bar_ms": bar_open_ms,
                    "entry": entry_px,
                    "raw_sl": raw_sl,
                    "tp_r": 2.2,
                    "max_h": 120,
                    "score": (h[rk] - c[s - 1]) / atr[s],
                })

    return signals


def size_trade(sig):
    p = sig["entry"]
    d_raw = (sig["raw_sl"] - p) / p
    d_val = float(np.clip(d_raw, 0.015, 0.080))
    sl_px = p * (1.0 + d_val)
    tp_px = p * (1.0 - sig["tp_r"] * d_val)

    m_use = (MARGIN_BASE_USD * (ACCOUNT_EQUITY / 100.0)) if COMPOUND else MARGIN_BASE_USD
    m_use = round(min(m_use, 0.15 * ACCOUNT_EQUITY), 2)
    lev = int(np.clip(np.floor(TARGET_MARGIN_LOSS / d_val), 5, 10))
    notional = round(m_use * lev, 2)
    if notional < 5.0:
        return None
    risk_usd = round(notional * (d_val + 0.0021), 2)
    reward_usd = round(notional * (sig["tp_r"] * d_val - 0.0009), 2)
    sig.update({
        "d_pct": round(d_val * 100.0, 2),
        "sl": sl_px,
        "tp": tp_px,
        "margin": m_use,
        "lev": lev,
        "notional": notional,
        "risk_usd": risk_usd,
        "reward_usd": reward_usd,
    })
    return sig


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"sent_keys": [], "last_coin_ms": {}}


def save_state(state):
    state["sent_keys"] = state["sent_keys"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)


def main():
    btc_df = get_4h_df("BTCUSDT", limit=120).iloc[:-1].reset_index(drop=True)
    _, btc_ref_arr = fractal_highs(btc_df["h"].to_numpy())
    btc_c = float(btc_df["c"].iloc[-1])
    btc_ref = float(btc_ref_arr[-1])

    all_sigs = []
    for sym in SYMBOLS:
        try:
            for raw_sig in evaluate_symbol(sym, btc_c, btc_ref):
                sized = size_trade(raw_sig)
                if sized:
                    all_sigs.append(sized)
        except Exception as e:
            print(f"[WARN] {sym}: {e}")
        time.sleep(0.08)

    prio = {"DS_NoBE_2.5R": 0, "DeepSeek_Breaker_Strict": 1}
    all_sigs.sort(key=lambda x: (prio[x["model"]], -x["score"], x["symbol"]))

    state = load_state()
    sent_set = set(state["sent_keys"])
    last_coin = state["last_coin_ms"]

    chosen = []
    seen_sym = set()
    for sig in all_sigs:
        key = f"{sig['model']}:{sig['symbol']}:{sig['bar_ms']}"
        if key in sent_set or sig["symbol"] in seen_sym:
            continue
        if (sig["bar_ms"] - last_coin.get(sig["symbol"], 0)) < 16 * 3600_000:
            continue
        chosen.append(sig)
        seen_sym.add(sig["symbol"])
        sent_set.add(key)
        last_coin[sig["symbol"]] = sig["bar_ms"]
        if len(chosen) >= MAX_SLOTS:
            break

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not chosen:
        print(f"[{now_str}] No new Clean_Top2 signals.")
        return

    lines = [f"*Clean_Top2 Scanner Alert* ({now_str})", f"Wallet: `${ACCOUNT_EQUITY:.2f}` | Max Slots: `{MAX_SLOTS}`\n"]
    for idx, s in enumerate(chosen, 1):
        lines.append(
            f"*{idx}. {s['symbol']} — SHORT* (`{s['model']}`)\n"
            f"• Entry: `{s['entry']:.6g}`\n"
            f"• Stop-Loss: `{s['sl']:.6g}` (`-{s['d_pct']}%`)\n"
            f"• Take-Profit: `{s['tp']:.6g}` (`{s['tp_r']}R`)\n"
            f"• Size: `${s['margin']}` margin @ `{s['lev']}x` (`${s['notional']}` pos)\n"
            f"• Risk / Reward: `-${s['risk_usd']}` / `+${s['reward_usd']}` | Max Hold: `{s['max_h']}h`\n"
        )

    send_telegram("\n".join(lines))
    state["sent_keys"] = list(sent_set)
    state["last_coin_ms"] = last_coin
    save_state(state)


if __name__ == "__main__":
    main()
