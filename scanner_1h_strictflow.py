#!/usr/bin/env python3
"""
1H DS1_StrictFlow Production Scanner (Short-Only | 24 USDT-M Pairs)
- Signal window: Closed 1H bars opening 13:00-19:00 UTC (runs at :01 of 14:00-20:00 UTC)
- Data sources: Binance Futures -> Binance Vision spot fallback; Binance/OKX/Bybit funding fallback
- Risk rules: 8% weekly margin | 5x-10x dynamic leverage | SL>=1.5% (clamped 6.0%) | 2.3R TP | 36h max hold
"""

import math
import os
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

FAPI_URLS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]
VISION_URL = "https://data-api.binance.vision"
OKX_FUND_URL = "https://www.okx.com/api/v5/public/funding-rate"
BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"

ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "100.0"))
MARGIN_PCT = float(os.getenv("MARGIN_PCT", "0.08"))
MAX_SLOTS = int(os.getenv("MAX_SLOTS", "4"))
TP_R = 2.3
MAX_HOLD_HOURS = 36
MIN_SL_PCT = 0.015
MAX_SL_PCT = 0.060

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID", "")
FORCE_RUN = os.getenv("FORCE_RUN", "0") == "1"


def fetch_1h_klines(symbol: str, limit: int = 100) -> pd.DataFrame:
    raw = None
    for base in FAPI_URLS:
        try:
            r = requests.get(
                f"{base}/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1h", "limit": limit},
                timeout=8,
            )
            if r.status_code == 200 and isinstance(r.json(), list):
                raw = r.json()
                break
        except Exception:
            continue

    if raw is None:
        r = requests.get(
            f"{VISION_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()

    cols = ["ot", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tqv", "ig"]
    df = pd.DataFrame(raw, columns=cols)
    for col in ["o", "h", "l", "c", "v", "tbv"]:
        df[col] = df[col].astype(float)
    df["ot_dt"] = pd.to_datetime(df["ot"].astype(np.int64), unit="ms", utc=True)
    df["ct_ms"] = df["ct"].astype(np.int64)
    return df


def fetch_funding_rate(symbol: str) -> float:
    # 1. Binance Futures live premiumIndex
    for base in FAPI_URLS:
        try:
            r = requests.get(
                f"{base}/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                timeout=5,
            )
            if r.status_code == 200:
                val = r.json().get("lastFundingRate")
                if val is not None:
                    return float(val)
        except Exception:
            continue

    # 2. OKX Perpetual swap funding rate fallback
    coin = symbol.replace("USDT", "")
    try:
        r = requests.get(
            OKX_FUND_URL,
            params={"instId": f"{coin}-USDT-SWAP"},
            timeout=6,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data and data[0].get("fundingRate") is not None:
                return float(data[0]["fundingRate"])
    except Exception:
        pass

    # 3. Bybit Linear ticker funding rate fallback
    try:
        r = requests.get(
            BYBIT_TICKER_URL,
            params={"category": "linear", "symbol": symbol},
            timeout=6,
        )
        if r.status_code == 200:
            lst = r.json().get("result", {}).get("list", [])
            if lst and lst[0].get("fundingRate") is not None:
                return float(lst[0]["fundingRate"])
    except Exception:
        pass

    return 0.0001  # Neutral positive fallback if all funding endpoints time out


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    r.raise_for_status()


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    now_ms = int(now_utc.timestamp() * 1000)

    btc_df = fetch_1h_klines("BTCUSDT", limit=100)
    btc_closed = btc_df[btc_df["ct_ms"] < now_ms].reset_index(drop=True)
    if len(btc_closed) < 30:
        raise RuntimeError("Insufficient closed BTCUSDT 1H bars.")

    sig_time = btc_closed.iloc[-1]["ot_dt"]
    sig_hour = int(sig_time.hour)
    if not FORCE_RUN and not (13 <= sig_hour < 20):
        print(f"[{now_utc:%Y-%m-%d %H:%M} UTC] Bar {sig_time:%H:%M} outside 13:00-19:00 UTC window. Exiting.")
        return

    btc_h24 = float(btc_closed["h"].iloc[-25:-1].max())
    btc_hi_last = float(btc_closed["h"].iloc[-1])
    if btc_hi_last > btc_h24:
        print(f"[{sig_time:%Y-%m-%d %H:%M} UTC] BTC made new 24H high ({btc_hi_last:.2f} > {btc_h24:.2f}). No alt divergence.")
        return

    candidates = []
    for sym in SYMBOLS:
        try:
            df = fetch_1h_klines(sym, limit=100)
            open_bars = df[df["ct_ms"] >= now_ms]
            closed = df[df["ct_ms"] < now_ms].reset_index(drop=True)
            if len(closed) < 30:
                continue

            # Wilder ATR14 on closed 1H bars
            pc = closed["c"].shift(1)
            tr = np.maximum(
                closed["h"] - closed["l"],
                np.maximum((closed["h"] - pc).abs(), (closed["l"] - pc).abs()),
            )
            atr14 = float(tr.ewm(alpha=1.0 / 14.0, adjust=False).mean().iloc[-1])

            bar = closed.iloc[-1]
            h24_alt = float(closed["h"].iloc[-25:-1].max())
            o_px, h_px, l_px, c_px = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
            vol, tbv = float(bar["v"]), float(bar["tbv"])
            rng = h_px - l_px
            if rng <= 0 or vol <= 0 or atr14 <= 0:
                continue

            uw = h_px - max(o_px, c_px)
            tbr = tbv / vol

            # DS1_StrictFlow core signal conditions
            if not (h_px > h24_alt and c_px < h24_alt):
                continue
            if uw <= 0.55 * rng or tbr < 0.55:
                continue

            fund_rate = fetch_funding_rate(sym)
            if fund_rate <= 0.0:
                continue

            entry_px = float(open_bars.iloc[0]["o"]) if len(open_bars) > 0 else c_px
            raw_sl = h_px + 0.3 * atr14
            d_raw = (raw_sl - entry_px) / entry_px
            if d_raw < MIN_SL_PCT:
                continue

            d_val = float(np.clip(d_raw, MIN_SL_PCT, MAX_SL_PCT))
            sl_px = entry_px * (1.0 + d_val)
            tp_px = entry_px * (1.0 - TP_R * d_val)
            lev = int(np.clip(math.floor(0.28 / d_val), 5, 10))
            margin_usd = round(MARGIN_PCT * ACCOUNT_EQUITY, 2)
            notional_usd = round(margin_usd * lev, 2)
            if notional_usd < 5.0:
                continue

            score = ((h_px - h24_alt) / atr14) * (tbr - 0.5) * (uw / atr14)
            candidates.append(
                {
                    "sym": sym,
                    "score": score,
                    "entry": entry_px,
                    "sl": sl_px,
                    "tp": tp_px,
                    "sl_pct": d_val * 100.0,
                    "lev": lev,
                    "margin": margin_usd,
                    "notional": notional_usd,
                    "tbr": tbr,
                    "fund": fund_rate * 100.0,
                }
            )
        except Exception as exc:
            print(f"[WARN] {sym}: {exc}")

    candidates.sort(key=lambda x: (-x["score"], x["sym"]))
    selected = candidates[:MAX_SLOTS]

    if not selected:
        print(f"[{sig_time:%Y-%m-%d %H:%M} UTC] Scan complete — 0 StrictFlow setups.")
        return

    lines = [
        f"<b>1H STRICTFLOW SHORT ALERT</b> ({sig_time:%Y-%m-%d %H:%M} UTC)",
        f"Equity: <b>${ACCOUNT_EQUITY:.2f}</b> | Mode: <b>ISOLATED SHORT</b> | Max Hold: <b>{MAX_HOLD_HOURS}h</b>",
        "",
    ]
    for c in selected:
        lines.extend(
            [
                f"<b>{c['sym']}</b> | Lev: <b>{c['lev']}x</b> | Margin: <b>${c['margin']:.2f}</b> (${c['notional']:.0f} size)",
                f"• Entry: <code>{c['entry']:.6g}</code>",
                f"• Stop-Loss: <code>{c['sl']:.6g}</code> (+{c['sl_pct']:.2f}%)",
                f"• Take-Profit: <code>{c['tp']:.6g}</code> (2.3R)",
                f"• Flow: TBR {c['tbr']:.2f} | Fund {c['fund']:+.4f}% | Score {c['score']:.2f}",
                "",
            ]
        )
    lines.append("<i>Rule: Set SL & TP immediately. Never move SL to breakeven early.</i>")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
