import json
import math
import os
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

SYMBOLS = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "UNIUSDT",
    "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "SUIUSDT", "TIAUSDT", "SEIUSDT", "RENDERUSDT", "FETUSDT", "WIFUSDT",
]
BTC_SYMBOL = "BTCUSDT"

FAPI_BASES = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]
VISION_BASE = "https://data-api.binance.vision"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
STATE_FILE = "alert_state.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CleanTop2Scanner/1.0)"}


def _req_json(url: str, params: dict, timeout: int = 12):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    if r.status_code == 200:
        return r.json()
    return None


def fetch_klines_raw(symbol: str, interval: str = "4h", limit: int = 120):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    # 1. Try Binance Futures endpoints first
    for base in FAPI_BASES:
        try:
            data = _req_json(f"{base}/fapi/v1/klines", params)
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            continue

    # 2. Fallback to unblocked Binance Vision market-data endpoint (works on US GitHub runners)
    try:
        data = _req_json(f"{VISION_BASE}/api/v3/klines", params)
        if isinstance(data, list) and len(data) > 0:
            return data
    except Exception:
        pass

    raise RuntimeError(f"All Binance kline endpoints failed for {symbol} ({params})")


def fetch_funding_series(symbol: str, limit: int = 40) -> pd.Series:
    # 1. Try Binance Futures fundingRate
    for base in FAPI_BASES:
        try:
            data = _req_json(f"{base}/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
            if isinstance(data, list) and len(data) > 0:
                ts = pd.to_datetime([int(x["fundingTime"]) for x in data], unit="ms", utc=True).floor("h")
                vals = [float(x["fundingRate"]) for x in data]
                s = pd.Series(vals, index=ts).sort_index()
                return s[~s.index.duplicated(keep="last")]
        except Exception:
            continue

    # 2. Fallback to unblocked OKX public perpetual funding history
    try:
        inst_id = symbol.replace("USDT", "-USDT-SWAP")
        res = _req_json(OKX_FUNDING_URL, {"instId": inst_id, "limit": limit})
        if isinstance(res, dict) and res.get("code") == "0" and res.get("data"):
            rows = res["data"]
            ts = pd.to_datetime([int(x["fundingTime"]) for x in rows], unit="ms", utc=True).floor("h")
            vals = [float(x["fundingRate"]) for x in rows]
            s = pd.Series(vals, index=ts).sort_index()
            return s[~s.index.duplicated(keep="last")]
    except Exception:
        pass

    return pd.Series(dtype=float)


def get_4h_df(symbol: str, limit: int = 120) -> pd.DataFrame:
    raw = fetch_klines_raw(symbol, interval="4h", limit=limit)
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_vol", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "taker_buy_vol"]:
        df[c] = df[c].astype(float)
    df["dt"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms", utc=True)

    # Wilder ATR(14)
    prev_c = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_c).abs(), (df["low"] - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
    df["tbr"] = np.clip(df["taker_buy_vol"] / df["volume"].replace(0.0, np.nan), 0.0, 1.0).fillna(0.5)
    return df


def compute_fractals(df: pd.DataFrame):
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    n = len(df)
    is_sh = np.zeros(n, dtype=bool)
    is_sl = np.zeros(n, dtype=bool)
    conf_hi = np.full(n, np.nan)

    for k in range(2, n - 2):
        if h[k] > h[k - 1] and h[k] > h[k - 2] and h[k] > h[k + 1] and h[k] > h[k + 2]:
            is_sh[k] = True
            conf_hi[k + 2] = h[k]
        if l[k] < l[k - 1] and l[k] < l[k - 2] and l[k] < l[k + 1] and l[k] < l[k + 2]:
            is_sl[k] = True

    ref_hi2 = pd.Series(conf_hi).ffill().to_numpy()
    return is_sh, is_sl, ref_hi2


def evaluate_symbol(symbol: str, df: pd.DataFrame, btc_close_s: float, btc_ref_hi2_s: float):
    s = len(df) - 1
    if s < 20:
        return None

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    tbr = df["tbr"].to_numpy()

    if not np.isfinite(atr[s]) or atr[s] <= 0:
        return None

    is_sh, is_sl, ref_hi2 = compute_fractals(df)

    # --- Model 1: DS_NoBE_2.5R (Priority 0) ---
    uwick = h[s] - max(o[s], c[s])
    body = abs(c[s] - o[s])
    btc_ok = (not np.isfinite(btc_ref_hi2_s)) or (btc_close_s <= btc_ref_hi2_s)

    if (
        np.isfinite(ref_hi2[s])
        and h[s] > ref_hi2[s]
        and c[s] < ref_hi2[s]
        and uwick > body
        and c[s] < o[s]
        and tbr[s] >= 0.50
        and btc_ok
    ):
        f_ser = fetch_funding_series(symbol, limit=40)
        fund_ok = True
        if len(f_ser) >= 4:
            f_mapped = f_ser.reindex(df["dt"], method="ffill").ffill().fillna(0.0)
            f_med8 = f_mapped.rolling(8, min_periods=4).median()
            if np.isfinite(f_med8.iloc[s]):
                fund_ok = bool(f_mapped.iloc[s] >= f_med8.iloc[s])

        if fund_ok:
            raw_sl = max(h[s], ref_hi2[s]) + 0.25 * atr[s]
            return {
                "symbol": symbol,
                "model": "DS_NoBE_2.5R",
                "priority": 0,
                "score": float(uwick / max(body, 1e-9)),
                "entry": float(c[s]),
                "raw_sl": float(raw_sl),
                "tp_r": 2.5,
                "max_hold_h": 72,
                "bar_time": str(df["dt"].iloc[s]),
            }

    # --- Model 2: DeepSeek_Breaker_Strict (Priority 1) ---
    sh_i = np.flatnonzero(is_sh)
    sl_i = np.flatnonzero(is_sl)
    c_sh = sh_i[sh_i <= s - 4]
    c_sl = sl_i[sl_i <= s - 4]
    if len(c_sh) >= 1 and len(c_sl) >= 1:
        r_h = int(c_sh[-1])
        r_l = int(c_sl[-1])
        if h[r_h] >= np.nanmax(h[max(0, r_h - 10) : r_h + 1]):
            raid_bars = [k for k in range(r_h + 1, s - 1) if h[k] > h[r_h]]
            if raid_bars:
                rk = raid_bars[0]
                if (
                    c[s - 1] < l[r_l]
                    and h[s] >= l[r_h]
                    and c[s] < h[r_h]
                    and (h[rk] - c[s - 1]) >= 1.5 * atr[s]
                ):
                    raw_sl = h[rk] + 0.25 * atr[s]
                    return {
                        "symbol": symbol,
                        "model": "DeepSeek_Breaker_Strict",
                        "priority": 1,
                        "score": float((h[rk] - c[s - 1]) / atr[s]),
                        "entry": float(c[s]),
                        "raw_sl": float(raw_sl),
                        "tp_r": 2.2,
                        "max_hold_h": 120,
                        "bar_time": str(df["dt"].iloc[s]),
                    }

    return None


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sent_keys": [], "last_coin_ts": {}}


def save_state(state: dict):
    state["sent_keys"] = state.get("sent_keys", [])[-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(text: str):
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TG_TOKEN")
        or os.getenv("TG_BOT_TOKEN")
    )
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        print("Telegram credentials not set; printing alert:\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=12,
    )
    print(f"Telegram status: {r.status_code}")


def main():
    equity = float(os.getenv("ACCOUNT_EQUITY", "100.0"))
    margin_base = float(os.getenv("MARGIN_BASE_USD", "8.0"))
    compound = os.getenv("COMPOUND", "true").lower() == "true"

    btc_df = get_4h_df(BTC_SYMBOL, limit=120).iloc[:-1].reset_index(drop=True)
    _, _, btc_ref_hi2 = compute_fractals(btc_df)
    btc_close_s = float(btc_df["close"].iloc[-1])
    btc_ref_hi2_s = float(btc_ref_hi2[-1])

    state = load_state()
    sent_keys = set(state.get("sent_keys", []))
    last_coin_ts = state.get("last_coin_ts", {})
    now_ts = time.time()

    signals = []
    for sym in SYMBOLS:
        try:
            df = get_4h_df(sym, limit=120).iloc[:-1].reset_index(drop=True)
            sig = evaluate_symbol(sym, df, btc_close_s, btc_ref_hi2_s)
            if sig:
                signals.append(sig)
        except Exception as e:
            print(f"[{sym}] Skipped due to fetch/eval error: {e}")
        time.sleep(0.05)

    signals.sort(key=lambda x: (x["priority"], -x["score"], x["symbol"]))
    print(f"Scan complete | BTC Close={btc_close_s:.2f} | Raw signals={len(signals)}")

    fired = 0
    for sig in signals:
        sym = sig["symbol"]
        dedup_key = f"{sym}_{sig['bar_time']}_{sig['model']}"
        if dedup_key in sent_keys:
            continue
        if (now_ts - float(last_coin_ts.get(sym, 0.0))) < 16 * 3600:
            continue

        entry = sig["entry"]
        d_raw = (sig["raw_sl"] - entry) / entry
        d_val = float(np.clip(d_raw, 0.015, 0.080))
        sl_px = entry * (1.0 + d_val)
        tp_px = entry * (1.0 - sig["tp_r"] * d_val)

        lev = int(np.clip(math.floor(0.28 / d_val), 5, 10))
        margin_usd = round(margin_base * (equity / 100.0), 2) if compound else round(margin_base, 2)
        margin_usd = min(margin_usd, round(0.15 * equity, 2))
        notional = round(margin_usd * lev, 2)
        if notional < 5.0:
            continue

        msg = (
            f"<b>4H CLEAN_TOP2 — SHORT SIGNAL</b>\n"
            f"<b>Coin:</b> {sym} | <b>Model:</b> {sig['model']}\n"
            f"<b>Mode:</b> Isolated | <b>Leverage:</b> {lev}x | <b>Margin:</b> ${margin_usd:.2f} (Notional ${notional:.2f})\n"
            f"<b>Entry (Ref):</b> {entry:.6g}\n"
            f"<b>Stop-Loss:</b> {sl_px:.6g} (+{d_val*100:.2f}%)\n"
            f"<b>Take-Profit:</b> {tp_px:.6g} (-{sig['tp_r']*d_val*100:.2f}% | {sig['tp_r']}R)\n"
            f"<b>Max Hold:</b> {sig['max_hold_h']}h (No early BE)"
        )
        send_telegram(msg)
        sent_keys.add(dedup_key)
        last_coin_ts[sym] = now_ts
        fired += 1

    state["sent_keys"] = list(sent_keys)
    state["last_coin_ts"] = last_coin_ts
    save_state(state)
    print(f"Alerts sent this run: {fired}")


if __name__ == "__main__":
    main()
    
