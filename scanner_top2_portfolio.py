import json
import math
import os
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

FAPI_BASES = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]
VISION_BASE = "https://data-api.binance.vision"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate-history"

BTC_SYMBOL = "BTCUSDT"
SYMBOLS = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "TIAUSDT", "SEIUSDT", "RENDERUSDT", "FETUSDT", "WIFUSDT",
]
TOP10_MAJORS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
}

STATE_FILE = "alert_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PortfolioSwingScanner/2.0)"}


def _req_json(url: str, params: dict, timeout: int = 12):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def fetch_klines_df(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    raw = None
    for base in FAPI_BASES:
        raw = _req_json(f"{base}/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if isinstance(raw, list) and len(raw) > 20:
            break
        raw = None
    if raw is None:
        raw = _req_json(f"{VISION_BASE}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(raw, list) or len(raw) <= 20:
        raise RuntimeError(f"Failed klines for {symbol} ({interval})")

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_vol", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    df["dt"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume", "quote_vol", "taker_buy_vol"]:
        df[c] = df[c].astype(float)
    df["tbr"] = np.clip(df["taker_buy_vol"] / df["volume"].replace(0.0, np.nan), 0.0, 1.0).fillna(0.5)
    return df[["dt", "open", "high", "low", "close", "volume", "quote_vol", "tbr"]].sort_values("dt").reset_index(drop=True)


def fetch_7d_funding(symbol: str) -> Optional[float]:
    for base in FAPI_BASES:
        data = _req_json(f"{base}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 21})
        if isinstance(data, list) and len(data) >= 15:
            return float(sum(float(x["fundingRate"]) for x in data[-21:]))
    coin = symbol.replace("USDT", "")
    okx = _req_json(OKX_FUNDING_URL, {"instId": f"{coin}-USDT-SWAP", "limit": 21})
    if isinstance(okx, dict) and isinstance(okx.get("data"), list) and len(okx["data"]) >= 15:
        return float(sum(float(x["fundingRate"]) for x in okx["data"][:21]))
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
    state["sent_keys"] = list(state.get("sent_keys", []))[-600:]
    state["closed"] = list(state.get("closed", []))[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or "").strip()
    if not token or not chat_id or os.getenv("DRY_RUN", "").lower() == "true":
        print("\n--- TELEGRAM PREVIEW ---\n" + text + "\n------------------------")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    r.raise_for_status()
    return True
def eval_m01_1d(sym: str, d1: pd.DataFrame, btc_1d: pd.DataFrame, is_top20: bool) -> Optional[dict]:
    if not is_top20 or len(d1) < 110 or len(btc_1d) < 110:
        return None
    btc_c = float(btc_1d["close"].iloc[-1])
    btc_ema100 = float(btc_1d["close"].ewm(span=100, adjust=False).mean().iloc[-1])
    if not (btc_c > btc_ema100):
        return None

    tr = pd.concat([
        d1["high"] - d1["low"],
        (d1["high"] - d1["close"].shift(1)).abs(),
        (d1["low"] - d1["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr20 = float(tr.rolling(20).mean().iloc[-1])
    ema100 = float(d1["close"].ewm(span=100, adjust=False).mean().iloc[-1])
    hh55 = float(d1["high"].shift(1).rolling(55).max().iloc[-1])
    c = float(d1["close"].iloc[-1])

    if np.isfinite(atr20) and atr20 > 0 and c > hh55 and c > ema100:
        sl_px = c - 2.5 * atr20
        sl_pct = (c - sl_px) / c
        if sl_pct > 0.01:
            tp_px = c + 3.0 * (c - sl_px)
            return {
                "model": "M01_1D_Trend", "symbol": sym, "side": "LONG",
                "entry": c, "sl_px": sl_px, "tp_px": tp_px,
                "sl_pct": sl_pct, "tp_pct": (tp_px - c) / c,
                "note": f"3.0R Ref | Trail 3.0x ATR20 ({3.0 * atr20:.5g})",
                "trail": 3.0 * atr20,
                "bar_time": str(d1["dt"].iloc[-1]),
            }
    return None


def eval_m04_4h(sym: str, df4: pd.DataFrame) -> Optional[dict]:
    if sym not in TOP10_MAJORS or len(df4) < 550:
        return None
    d = df4.copy()
    d["day"] = d["dt"].dt.floor("D")
    d1 = d.groupby("day")["close"].last().to_frame("d1_c")
    d1["d1_ema50"] = d1["d1_c"].ewm(span=50, adjust=False).mean()
    d = d.merge(d1.shift(1), left_on="day", right_index=True, how="left")

    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - d["close"].shift(1)).abs(),
        (d["low"] - d["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = float(tr.rolling(14).mean().iloc[-1])
    bb_mid = d["close"].rolling(20).mean()
    bb_bw = (4.0 * d["close"].rolling(20).std(ddof=0)) / bb_mid
    bw_pct = bb_bw.rolling(540, min_periods=180).rank(pct=True)
    sq6 = bool((bw_pct <= 0.10).rolling(6).min().iloc[-1] == 1.0)

    c = float(d["close"].iloc[-1])
    tbr = float(d["tbr"].iloc[-1])
    d1_c = float(d["d1_c"].iloc[-1]) if pd.notna(d["d1_c"].iloc[-1]) else np.nan
    d1_ema = float(d["d1_ema50"].iloc[-1]) if pd.notna(d["d1_ema50"].iloc[-1]) else np.nan
    hi20 = float(d["high"].shift(1).rolling(20).max().iloc[-1])
    lo20 = float(d["low"].shift(1).rolling(20).min().iloc[-1])

    if not (sq6 and np.isfinite(atr14) and atr14 > 0 and np.isfinite(d1_ema)):
        return None
    s_dist = 1.5 * atr14
    s_pct = s_dist / c
    if s_pct <= 0.005:
        return None

    if c > hi20 and d1_c > d1_ema and tbr > 0.52:
        return {
            "model": "M04_4H_Squeeze", "symbol": sym, "side": "LONG",
            "entry": c, "sl_px": c - s_dist, "tp_px": c + 2.5 * s_dist,
            "sl_pct": s_pct, "tp_pct": 2.5 * s_pct,
            "note": "2.5R Target | BE at +2.0R | Max Hold 48h",
            "bar_time": str(d["dt"].iloc[-1]),
        }
    if c < lo20 and d1_c < d1_ema and tbr < 0.48:
        return {
            "model": "M04_4H_Squeeze", "symbol": sym, "side": "SHORT",
            "entry": c, "sl_px": c + s_dist, "tp_px": c - 2.5 * s_dist,
            "sl_pct": s_pct, "tp_pct": 2.5 * s_pct,
            "note": "2.5R Target | BE at +2.0R | Max Hold 48h",
            "bar_time": str(d["dt"].iloc[-1]),
        }
    return None


def eval_m03_weekly(daily_map: Dict[str, pd.DataFrame], med_vols: Dict[str, float]) -> List[dict]:
    if not daily_map:
        return []
    sample_dt = next(iter(daily_map.values()))["dt"].iloc[-1]
    if os.getenv("FORCE_M03", "").lower() != "true" and (sample_dt + pd.Timedelta(days=1)).weekday() != 0:
        return []

    eligible = [s for s, df in daily_map.items() if len(df) >= 120 and s in med_vols]
    top_syms = sorted(eligible, key=lambda s: med_vols[s], reverse=True)[:30]
    f7_map = {}
    for sym in top_syms:
        f7 = fetch_7d_funding(sym)
        if f7 is not None:
            f7_map[sym] = f7
        time.sleep(0.03)
    if len(f7_map) < 10:
        return []

    ranked = sorted(f7_map.items(), key=lambda kv: kv[1])
    lo4, hi4 = {k for k, _ in ranked[:4]}, {k for k, _ in ranked[-4:]}
    s_cands, l_cands = [], []
    for sym in hi4:
        df = daily_map[sym]
        c = float(df["close"].iloc[-1])
        ema10 = float(df["close"].ewm(span=10, adjust=False).mean().iloc[-1])
        if f7_map[sym] >= 0.00015 and c < ema10:
            s_cands.append(sym)
    for sym in lo4:
        if sym in s_cands:
            continue
        df = daily_map[sym]
        c = float(df["close"].iloc[-1])
        ema10 = float(df["close"].ewm(span=10, adjust=False).mean().iloc[-1])
        if f7_map[sym] <= 0.00005 and c > ema10:
            l_cands.append(sym)

    out = []
    for side, cands, sl_m, tp_m in [("SHORT", s_cands, 1.08, 0.84), ("LONG", l_cands, 0.92, 1.16)]:
        if len(cands) >= 3:
            for sym in cands:
                c = float(daily_map[sym]["close"].iloc[-1])
                out.append({
                    "model": "M03_Wkly_Fund", "symbol": sym, "side": side,
                    "entry": c, "sl_px": c * sl_m, "tp_px": c * tp_m,
                    "sl_pct": 0.08, "tp_pct": 0.16,
                    "note": f"2.0R Bracket (8% SL / 16% TP) | 7d Fund={f7_map[sym]*100:+.3f}%",
                    "bar_time": str(daily_map[sym]["dt"].iloc[-1]),
                })
    return out


COST_RT = 0.0020  # 0.20% round trip (fees + slippage)

# One-time seed: M03 trades opened before exit tracking existed (Nov 24 2026, 1:41 AM IST)
SEED_OPEN = [
    {"model": "M03_Wkly_Fund", "symbol": "NEARUSDT", "side": "SHORT", "ts": 1795464660, "entry": 2.986, "sl": 3.22488, "sl0": 3.22488, "tp": 2.50824, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "WIFUSDT", "side": "SHORT", "ts": 1795464660, "entry": 0.1206, "sl": 0.130248, "sl0": 0.130248, "tp": 0.101304, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "RENDERUSDT", "side": "SHORT", "ts": 1795464660, "entry": 1.339, "sl": 1.44612, "sl0": 1.44612, "tp": 1.12476, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "ARBUSDT", "side": "SHORT", "ts": 1795464660, "entry": 0.1143, "sl": 0.123444, "sl0": 0.123444, "tp": 0.096012, "max_h": 168, "be_r": None, "trail": None},
]


def manage_open(state: dict, now_ts: float):
    """Paper exit tracking on closed 1h bars: SL first, then TP, then max hold; BE and trail move the stop after each bar."""
    still = []
    for p in state.get("open", []):
        try:
            hrs = int((now_ts - p["ts"]) / 3600) + 3
            k = fetch_klines_df(p["symbol"], "1h", min(1000, max(hrs, 25))).iloc[:-1]
            k = k[k["dt"] >= pd.to_datetime(p["ts"], unit="s", utc=True)]
            long_ = p["side"] == "LONG"
            e, sl, tp = p["entry"], p["sl"], p["tp"]
            risk = abs(e - p["sl0"])
            exit_px, why = None, None
            for _, b in k.iterrows():
                hit_sl = b["low"] <= sl if long_ else b["high"] >= sl
                hit_tp = b["high"] >= tp if long_ else b["low"] <= tp
                if hit_sl:
                    exit_px, why = sl, "SL"
                    break
                if hit_tp:
                    exit_px, why = tp, "TP"
                    break
                age_h = (b["dt"].timestamp() + 3600 - p["ts"]) / 3600
                if p.get("max_h") and age_h >= p["max_h"]:
                    exit_px, why = float(b["close"]), "TIME"
                    break
                fav = (b["high"] - e) if long_ else (e - b["low"])
                if p.get("be_r") and fav >= p["be_r"] * risk:
                    sl = max(sl, e) if long_ else min(sl, e)
                if p.get("trail"):
                    sl = max(sl, b["high"] - p["trail"]) if long_ else min(sl, b["low"] + p["trail"])
            p["sl"] = sl
            if exit_px is None:
                still.append(p)
                continue
            r = ((exit_px - e) if long_ else (e - exit_px)) / risk
            net = r - COST_RT / (risk / e)
            send_telegram(
                f"<b>PAPER EXIT [{p['model']}] {p['symbol']} {p['side']}</b>\n"
                f"<b>Reason:</b> {why} | <b>Entry:</b> {e:.6g} | <b>Exit:</b> {exit_px:.6g}\n"
                f"<b>Gross:</b> {r:+.2f}R | <b>Net:</b> {net:+.2f}R"
            )
            state.setdefault("closed", []).append({**p, "exit": exit_px, "why": why, "net_r": round(net, 3), "exit_ts": now_ts})
        except Exception as ex:
            print(f"[{p.get('symbol')}] manage error: {ex}")
            still.append(p)
    state["open"] = still


def main():
    equity = float(os.getenv("ACCOUNT_EQUITY", "100.0"))
    margin_base = float(os.getenv("MARGIN_BASE_USD", "8.0"))
    compound = os.getenv("COMPOUND", "true").lower() == "true"
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"

    state = load_state()
    sent_keys = set(state.get("sent_keys", []))
    last_coin_ts = state.get("last_coin_ts", {})
    now_ts = time.time()

    if not state.get("seeded_v1"):
        state.setdefault("open", []).extend(SEED_OPEN)
        state["seeded_v1"] = True
    if not dry_run:
        manage_open(state, now_ts)

    btc_1d = fetch_klines_df(BTC_SYMBOL, "1d", 150).iloc[:-1].reset_index(drop=True)
    btc_4h = fetch_klines_df(BTC_SYMBOL, "4h", 600).iloc[:-1].reset_index(drop=True)
    daily_map, h4_map, med_vols = {}, {BTC_SYMBOL: btc_4h}, {}

    for sym in SYMBOLS:
        try:
            d1 = fetch_klines_df(sym, "1d", 150).iloc[:-1].reset_index(drop=True)
            daily_map[sym] = d1
            if len(d1) >= 120:
                med_vols[sym] = float(d1["quote_vol"].tail(30).median())
            if sym in TOP10_MAJORS:
                h4_map[sym] = fetch_klines_df(sym, "4h", 600).iloc[:-1].reset_index(drop=True)
        except Exception as e:
            print(f"[{sym}] Skip fetch error: {e}")
        time.sleep(0.04)

    top20_set = set(sorted(med_vols.keys(), key=lambda s: med_vols[s], reverse=True)[:20])
    signals: List[dict] = []
    for sym in [BTC_SYMBOL] + SYMBOLS:
        if sym in h4_map:
            s4 = eval_m04_4h(sym, h4_map[sym])
            if s4:
                signals.append(s4)
        if sym != BTC_SYMBOL and sym in daily_map:
            s1 = eval_m01_1d(sym, daily_map[sym], btc_1d, sym in top20_set)
            if s1:
                signals.append(s1)
    signals.extend(eval_m03_weekly(daily_map, med_vols))
    print(f"Scan complete | BTC 1D={btc_1d['close'].iloc[-1]:.2f} | Raw signals={len(signals)}")

    fired = 0
    for sig in signals:
        sym, model, side = sig["symbol"], sig["model"], sig["side"]
        dedup_key, cd_key = f"{model}_{sym}_{side}_{sig['bar_time']}", f"{model}_{sym}"
        if not dry_run and (dedup_key in sent_keys or (now_ts - float(last_coin_ts.get(cd_key, 0.0))) < 16 * 3600):
            continue
        d_val = max(float(sig["sl_pct"]), 0.005)
        lev = int(np.clip(math.floor(0.28 / d_val), 3, 10))
        margin_usd = min(round(margin_base * (equity / 100.0), 2) if compound else round(margin_base, 2), round(0.15 * equity, 2))
        notional = round(margin_usd * lev, 2)
        if notional < 5.0:
            continue
        sl_sign, tp_sign = ("-" if side == "LONG" else "+"), ("+" if side == "LONG" else "-")
        msg = (
            f"<b>SWING PORTFOLIO — {side} SIGNAL</b>\n"
            f"<b>Coin:</b> {sym} | <b>Engine:</b> {model}\n"
            f"<b>Mode:</b> Isolated | <b>Leverage:</b> {lev}x | <b>Margin:</b> ${margin_usd:.2f} (Notional ${notional:.2f})\n"
            f"<b>Entry (Ref):</b> {sig['entry']:.6g}\n"
            f"<b>Stop-Loss:</b> {sig['sl_px']:.6g} ({sl_sign}{sig['sl_pct']*100:.2f}%)\n"
            f"<b>Take-Profit:</b> {sig['tp_px']:.6g} ({tp_sign}{sig['tp_pct']*100:.2f}%)\n"
            f"<b>Rule:</b> {sig['note']}"
        )
        send_telegram(msg)
        if not dry_run:
            state.setdefault("open", []).append({
                "model": model, "symbol": sym, "side": side, "ts": now_ts,
                "entry": sig["entry"], "sl": sig["sl_px"], "sl0": sig["sl_px"], "tp": sig["tp_px"],
                "max_h": {"M04_4H_Squeeze": 48, "M03_Wkly_Fund": 168}.get(model),
                "be_r": 2.0 if model == "M04_4H_Squeeze" else None,
                "trail": sig.get("trail"),
            })
            sent_keys.add(dedup_key)
            last_coin_ts[cd_key] = now_ts
        fired += 1

    if not dry_run:
        state["sent_keys"], state["last_coin_ts"] = list(sent_keys), last_coin_ts
        save_state(state)
    print(f"Alerts processed: {fired} | Open paper trades: {len(state.get('open', []))}")


if __name__ == "__main__":
    main()
    "NEARUSDT", "side": "SHORT", "ts": 1795464660, "entry": 2.986, "sl": 3.22488, "sl0": 3.22488, "tp": 2.50824, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "WIFUSDT", "side": "SHORT", "ts": 1795464660, "entry": 0.1206, "sl": 0.130248, "sl0": 0.130248, "tp": 0.101304, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "RENDERUSDT", "side": "SHORT", "ts": 1795464660, "entry": 1.339, "sl": 1.44612, "sl0": 1.44612, "tp": 1.12476, "max_h": 168, "be_r": None, "trail": None},
    {"model": "M03_Wkly_Fund", "symbol": "ARBUSDT", "side": "SHORT", "ts": 1795464660, "entry": 0.1143, "sl": 0.123444, "sl0": 0.123444, "tp": 0.096012, "max_h": 168, "be_r": None, "trail": None},
]


def manage_open(state: dict, now_ts: float):
    """Paper exit tracking on closed 1h bars: SL first, then TP, then max hold; BE and trail move the stop after each bar."""
    still = []
    for p in state.get("open", []):
        try:
            hrs = int((now_ts - p["ts"]) / 3600) + 3
            k = fetch_klines_df(p["symbol"], "1h", min(1000, max(hrs, 25))).iloc[:-1]
            k = k[k["dt"] >= pd.to_datetime(p["ts"], unit="s", utc=True)]
            long_ = p["side"] == "LONG"
            e, sl, tp = p["entry"], p["sl"], p["tp"]
            risk = abs(e - p["sl0"])
            exit_px, why = None, None
            for _, b in k.iterrows():
                hit_sl = b["low"] <= sl if long_ else b["high"] >= sl
                hit_tp = b["high"] >= tp if long_ else b["low"] <= tp
                if hit_sl:
                    exit_px, why = sl, "SL"
                    break
                if hit_tp:
                    exit_px, why = tp, "TP"
                    break
                age_h = (b["dt"].timestamp() + 3600 - p["ts"]) / 3600
                if p.get("max_h") and age_h >= p["max_h"]:
                    exit_px, why = float(b["close"]), "TIME"
                    break
                fav = (b["high"] - e) if long_ else (e - b["low"])
                if p.get("be_r") and fav >= p["be_r"] * risk:
                    sl = max(sl, e) if long_ else min(sl, e)
                if p.get("trail"):
                    sl = max(sl, b["high"] - p["trail"]) if long_ else min(sl, b["low"] + p["trail"])
            p["sl"] = sl
            if exit_px is None:
                still.append(p)
                continue
            r = ((exit_px - e) if long_ else (e - exit_px)) / risk
            net = r - COST_RT / (risk / e)
            send_telegram(
                f"<b>PAPER EXIT [{p['model']}] {p['symbol']} {p['side']}</b>\n"
                f"<b>Reason:</b> {why} | <b>Entry:</b> {e:.6g} | <b>Exit:</b> {exit_px:.6g}\n"
                f"<b>Gross:</b> {r:+.2f}R | <b>Net:</b> {net:+.2f}R"
            )
            state.setdefault("closed", []).append({**p, "exit": exit_px, "why": why, "net_r": round(net, 3), "exit_ts": now_ts})
        except Exception as ex:
            print(f"[{p.get('symbol')}] manage error: {ex}")
            still.append(p)
    state["open"] = still


def main():
    equity = float(os.getenv("ACCOUNT_EQUITY", "100.0"))
    margin_base = float(os.getenv("MARGIN_BASE_USD", "8.0"))
    compound = os.getenv("COMPOUND", "true").lower() == "true"
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"

    state = load_state()
    sent_keys = set(state.get("sent_keys", []))
    last_coin_ts = state.get("last_coin_ts", {})
    now_ts = time.time()

    if not state.get("seeded_v1"):
        state.setdefault("open", []).extend(SEED_OPEN)
        state["seeded_v1"] = True
    if not dry_run:
        manage_open(state, now_ts)

    btc_1d = fetch_klines_df(BTC_SYMBOL, "1d", 150).iloc[:-1].reset_index(drop=True)
    btc_4h = fetch_klines_df(BTC_SYMBOL, "4h", 600).iloc[:-1].reset_index(drop=True)
    daily_map, h4_map, med_vols = {}, {BTC_SYMBOL: btc_4h}, {}

    for sym in SYMBOLS:
        try:
            d1 = fetch_klines_df(sym, "1d", 150).iloc[:-1].reset_index(drop=True)
            daily_map[sym] = d1
            if len(d1) >= 120:
                med_vols[sym] = float(d1["quote_vol"].tail(30).median())
            if sym in TOP10_MAJORS:
                h4_map[sym] = fetch_klines_df(sym, "4h", 600).iloc[:-1].reset_index(drop=True)
        except Exception as e:
            print(f"[{sym}] Skip fetch error: {e}")
        time.sleep(0.04)

    top20_set = set(sorted(med_vols.keys(), key=lambda s: med_vols[s], reverse=True)[:20])
    signals: List[dict] = []
    for sym in [BTC_SYMBOL] + SYMBOLS:
        if sym in h4_map:
            s4 = eval_m04_4h(sym, h4_map[sym])
            if s4:
                signals.append(s4)
        if sym != BTC_SYMBOL and sym in daily_map:
            s1 = eval_m01_1d(sym, daily_map[sym], btc_1d, sym in top20_set)
            if s1:
                signals.append(s1)
    signals.extend(eval_m03_weekly(daily_map, med_vols))
    print(f"Scan complete | BTC 1D={btc_1d['close'].iloc[-1]:.2f} | Raw signals={len(signals)}")

    fired = 0
    for sig in signals:
        sym, model, side = sig["symbol"], sig["model"], sig["side"]
        dedup_key, cd_key = f"{model}_{sym}_{side}_{sig['bar_time']}", f"{model}_{sym}"
        if not dry_run and (dedup_key in sent_keys or (now_ts - float(last_coin_ts.get(cd_key, 0.0))) < 16 * 3600):
            continue
        d_val = max(float(sig["sl_pct"]), 0.005)
        lev = int(np.clip(math.floor(0.28 / d_val), 3, 10))
        margin_usd = min(round(margin_base * (equity / 100.0), 2) if compound else round(margin_base, 2), round(0.15 * equity, 2))
        notional = round(margin_usd * lev, 2)
        if notional < 5.0:
            continue
        sl_sign, tp_sign = ("-" if side == "LONG" else "+"), ("+" if side == "LONG" else "-")
        msg = (
            f"<b>SWING PORTFOLIO — {side} SIGNAL</b>\n"
            f"<b>Coin:</b> {sym} | <b>Engine:</b> {model}\n"
            f"<b>Mode:</b> Isolated | <b>Leverage:</b> {lev}x | <b>Margin:</b> ${margin_usd:.2f} (Notional ${notional:.2f})\n"
            f"<b>Entry (Ref):</b> {sig['entry']:.6g}\n"
            f"<b>Stop-Loss:</b> {sig['sl_px']:.6g} ({sl_sign}{sig['sl_pct']*100:.2f}%)\n"
            f"<b>Take-Profit:</b> {sig['tp_px']:.6g} ({tp_sign}{sig['tp_pct']*100:.2f}%)\n"
            f"<b>Rule:</b> {sig['note']}"
        )
        send_telegram(msg)
        if not dry_run:
            state.setdefault("open", []).append({
                "model": model, "symbol": sym, "side": side, "ts": now_ts,
                "entry": sig["entry"], "sl": sig["sl_px"], "sl0": sig["sl_px"], "tp": sig["tp_px"],
                "max_h": {"M04_4H_Squeeze": 48, "M03_Wkly_Fund": 168}.get(model),
                "be_r": 2.0 if model == "M04_4H_Squeeze" else None,
                "trail": sig.get("trail"),
            })
            sent_keys.add(dedup_key)
            last_coin_ts[cd_key] = now_ts
        fired += 1

    if not dry_run:
        state["sent_keys"], state["last_coin_ts"] = list(sent_keys), last_coin_ts
        save_state(state)
    print(f"Alerts processed: {fired} | Open paper trades: {len(state.get('open', []))}")


if __name__ == "__main__":
    main()ate.get("sent_keys", []))
    last_coin_ts = state.get("last_coin_ts", {})
    now_ts = time.time()

    btc_1d = fetch_klines_df(BTC_SYMBOL, "1d", 150).iloc[:-1].reset_index(drop=True)
    btc_4h = fetch_klines_df(BTC_SYMBOL, "4h", 600).iloc[:-1].reset_index(drop=True)
    daily_map, h4_map, med_vols = {}, {BTC_SYMBOL: btc_4h}, {}

    for sym in SYMBOLS:
        try:
            d1 = fetch_klines_df(sym, "1d", 150).iloc[:-1].reset_index(drop=True)
            daily_map[sym] = d1
            if len(d1) >= 120:
                med_vols[sym] = float(d1["quote_vol"].tail(30).median())
            if sym in TOP10_MAJORS:
                h4_map[sym] = fetch_klines_df(sym, "4h", 600).iloc[:-1].reset_index(drop=True)
        except Exception as e:
            print(f"[{sym}] Skip fetch error: {e}")
        time.sleep(0.04)

    top20_set = set(sorted(med_vols.keys(), key=lambda s: med_vols[s], reverse=True)[:20])
    signals: List[dict] = []
    for sym in [BTC_SYMBOL] + SYMBOLS:
        if sym in h4_map:
            s4 = eval_m04_4h(sym, h4_map[sym])
            if s4:
                signals.append(s4)
        if sym != BTC_SYMBOL and sym in daily_map:
            s1 = eval_m01_1d(sym, daily_map[sym], btc_1d, sym in top20_set)
            if s1:
                signals.append(s1)
    signals.extend(eval_m03_weekly(daily_map, med_vols))
    print(f"Scan complete | BTC 1D={btc_1d['close'].iloc[-1]:.2f} | Raw signals={len(signals)}")

    fired = 0
    for sig in signals:
        sym, model, side = sig["symbol"], sig["model"], sig["side"]
        dedup_key, cd_key = f"{model}_{sym}_{side}_{sig['bar_time']}", f"{model}_{sym}"
        if not dry_run and (dedup_key in sent_keys or (now_ts - float(last_coin_ts.get(cd_key, 0.0))) < 16 * 3600):
            continue
        d_val = max(float(sig["sl_pct"]), 0.005)
        lev = int(np.clip(math.floor(0.28 / d_val), 3, 10))
        margin_usd = min(round(margin_base * (equity / 100.0), 2) if compound else round(margin_base, 2), round(0.15 * equity, 2))
        notional = round(margin_usd * lev, 2)
        if notional < 5.0:
            continue
        sl_sign, tp_sign = ("-" if side == "LONG" else "+"), ("+" if side == "LONG" else "-")
        msg = (
            f"<b>SWING PORTFOLIO — {side} SIGNAL</b>\n"
            f"<b>Coin:</b> {sym} | <b>Engine:</b> {model}\n"
            f"<b>Mode:</b> Isolated | <b>Leverage:</b> {lev}x | <b>Margin:</b> ${margin_usd:.2f} (Notional ${notional:.2f})\n"
            f"<b>Entry (Ref):</b> {sig['entry']:.6g}\n"
            f"<b>Stop-Loss:</b> {sig['sl_px']:.6g} ({sl_sign}{sig['sl_pct']*100:.2f}%)\n"
            f"<b>Take-Profit:</b> {sig['tp_px']:.6g} ({tp_sign}{sig['tp_pct']*100:.2f}%)\n"
            f"<b>Rule:</b> {sig['note']}"
        )
        send_telegram(msg)
        if not dry_run:
            sent_keys.add(dedup_key)
            last_coin_ts[cd_key] = now_ts
        fired += 1

    if not dry_run:
        state["sent_keys"], state["last_coin_ts"] = list(sent_keys), last_coin_ts
        save_state(state)
    print(f"Alerts processed: {fired}")


if __name__ == "__main__":
    main()
