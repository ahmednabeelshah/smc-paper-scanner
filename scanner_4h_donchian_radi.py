import os, time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import numpy as np
import pandas as pd
import requests

SYMBOLS = (
    "SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","UNIUSDT",
    "ATOMUSDT","LTCUSDT","BCHUSDT","FILUSDT","APTUSDT","ARBUSDT","OPUSDT","NEARUSDT",
    "INJUSDT","ETCUSDT","AAVEUSDT","BNBUSDT","SUIUSDT","TIAUSDT","SEIUSDT","1000PEPEUSDT",
    "1000SHIBUSDT","WIFUSDT","ORDIUSDT","WLDUSDT","TRXUSDT","TONUSDT","ICPUSDT","STXUSDT",
    "IMXUSDT","RUNEUSDT","LDOUSDT","FETUSDT","GRTUSDT","XLMUSDT","HBARUSDT","ALGOUSDT",
    "SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","CRVUSDT","SNXUSDT","DYDXUSDT",
    "1000FLOKIUSDT","1000BONKUSDT","ENAUSDT"
)
FAPI_HOSTS = (
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
)
VISION_SPOT_URL = "https://data-api.binance.vision/api/v3/klines"

RISK_PCT = float((os.getenv("RISK_PCT") or "0.035").strip())
EQUITY = float((os.getenv("ACCOUNT_EQUITY") or "100.0").strip())
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
FORCE_TEST = (os.getenv("FORCE_TEST") or "0").strip() == "1"

MAX_NOTIONAL_MULT = 4.0
MAX_STOP_PCT = Decimal("0.085")
FEE, STOP_FEE = 0.00045, 0.00165
GEO_BLOCKED = False

def fallback_filters():
    large = {"BNBUSDT", "LTCUSDT", "BCHUSDT", "AAVEUSDT"}
    fine = {"DOGEUSDT", "TRXUSDT", "XLMUSDT", "HBARUSDT", "GALAUSDT", "GRTUSDT"}
    out = {}
    for s in SYMBOLS:
        tick = (
            "0.000001" if s.startswith("1000")
            else "0.00001" if s in fine
            else "0.01" if s in large | {"SOLUSDT"}
            else "0.0001"
        )
        out[s] = (
            Decimal(tick),
            Decimal("0.1" if s in large else "1"),
            Decimal(10 if s in large else 5),
        )
    return out

def fapi_get(path, params=None):
    global GEO_BLOCKED
    if GEO_BLOCKED:
        return None
    last_err = None
    for host in FAPI_HOSTS:
        try:
            r = requests.get(f"{host}{path}", params=params, timeout=12)
            if r.status_code in (403, 418, 451) or not r.text.strip():
                last_err = f"HTTP {r.status_code} (empty/blocked)"
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
    print(f"INFO: Futures endpoints unavailable on runner IP ({last_err}). Switching to Vision fallback.")
    GEO_BLOCKED = True
    return None

def get_filters_and_funding():
    info = fapi_get("/fapi/v1/exchangeInfo")
    if info is None:
        return fallback_filters(), {}
    filters = fallback_filters()
    for item in info["symbols"]:
        s = item["symbol"]
        if s in filters and item["contractType"] == "PERPETUAL":
            f = {a["filterType"]: a for a in item["filters"]}
            filters[s] = tuple(map(Decimal, (
                f["PRICE_FILTER"]["tickSize"],
                f["LOT_SIZE"]["stepSize"],
                f["MIN_NOTIONAL"]["notional"],
            )))
    prem = fapi_get("/fapi/v1/premiumIndex") or []
    funding = {x["symbol"]: float(x["lastFundingRate"]) for x in prem if x["symbol"] in filters}
    return filters, funding

def fetch_klines(sym):
    raw = fapi_get("/fapi/v1/klines", {"symbol": sym, "interval": "4h", "limit": 300})
    if raw is not None:
        return raw
    spot_sym = sym[4:] if sym.startswith("1000") else sym
    for attempt in range(2):
        try:
            r = requests.get(VISION_SPOT_URL, params={"symbol": spot_sym, "interval": "4h", "limit": 300}, timeout=12)
            r.raise_for_status()
            rows = r.json()
            if sym.startswith("1000"):
                for row in rows:
                    for idx in (1, 2, 3, 4):
                        row[idx] = str(float(row[idx]) * 1000.0)
            return rows
        except Exception:
            if attempt == 1:
                raise
            time.sleep(1)

def wilder(s):
    v = s.dropna()
    t = v.iloc[13:].copy()
    if len(t):
        t.iloc[0] = v.iloc[:14].mean()
    return t.ewm(alpha=1/14, adjust=False).mean().reindex(s.index)

def tick_round(val, tick, mode):
    return (Decimal(str(val)) / tick).to_integral_value(rounding=mode) * tick

def scan_symbol(args):
    sym, flt, fund, now_ms = args
    raw = fetch_klines(sym)
    df = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","n","tb","tq","ig"])
    if int(df["ct"].iloc[-1]) > now_ms:
        df = df.iloc[:-1]
    o, h, l, c = (df[k].astype(float) for k in ("o","h","l","c"))
    prev = c.shift()
    atr = wilder(pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1))
    up, dn = h.diff(), -l.diff()
    plus = wilder(up.where((up>dn) & (up>0), 0).where(up.notna()))
    minus = wilder(dn.where((dn>up) & (dn>0), 0).where(dn.notna()))
    dm = plus + minus
    adx = wilder((100 * (plus - minus).abs() / dm).mask(dm == 0, 0))
    rank = ((atr / c).rolling(100).rank(method="average") - 1) * 100 / 99
    e50, e200 = (c.ewm(span=n, adjust=False, min_periods=n).mean() for n in (50, 200))
    dh, dl = h.rolling(55).max(), l.rolling(55).min()

    # Priority #1: DeepSeek_Donchian55
    don_side, score = 0, 0.0
    if adx.iloc[-1] >= 25 and 40 <= rank.iloc[-1] <= 95:
        if c.iloc[-1] > e200.iloc[-1] and e50.iloc[-1] > e200.iloc[-1] and c.iloc[-1] > dh.iloc[-2] and c.iloc[-2] <= dh.iloc[-3] and fund <= 0.0003:
            don_side, score = 1, float(adx.iloc[-1])
        elif c.iloc[-1] < e200.iloc[-1] and e50.iloc[-1] < e200.iloc[-1] and c.iloc[-1] < dl.iloc[-2] and c.iloc[-2] >= dl.iloc[-3] and fund >= -0.0003:
            don_side, score = -1, float(adx.iloc[-1])

    # Priority #2: Claude_RADI
    logc = np.log(c)
    sigp = np.sqrt((np.log(h/l)**2 / (4 * np.log(2))).rolling(60).mean()).where(lambda s: s > 0)
    sigd = sigp * np.sqrt(6)
    rvr = (logc.diff(6)**2).rolling(60).mean() / (6 * sigp**2)
    dz = logc.diff(18) / (sigp * np.sqrt(18))
    trend = logc.diff(180)
    arm_l = arm_s = True
    for z in dz.iloc[:-1].dropna():
        if z > 1.8: arm_l = False
        elif z < 0.5: arm_l = True
        if z < -1.8: arm_s = False
        elif z > -0.5: arm_s = True
    radi_side = 0
    if rvr.iloc[-1] >= 1.20:
        if arm_l and dz.iloc[-1] > 1.8 and dz.iloc[-2] <= 1.8 and trend.iloc[-1] > 0 and fund <= 0.0003:
            radi_side = 1
        elif arm_s and dz.iloc[-1] < -1.8 and dz.iloc[-2] >= -1.8 and trend.iloc[-1] < 0 and fund >= -0.0003:
            radi_side = -1

    kind, side = (0, don_side) if don_side else ((1, radi_side) if radi_side else (-1, 0))
    if side == 0:
        return None
    price, a = float(c.iloc[-1]), float(atr.iloc[-1])
    if not np.isfinite(a) or a <= 0:
        return None
    tick, step, min_notional = flt
    entry = Decimal(str(price))
    if kind == 0:
        swing = float(l.iloc[-10:].min() if side == 1 else h.iloc[-10:].max())
        d = max(side * (price - swing), 1.5 * a) + 0.25 * a
    else:
        sd = float(sigd.iloc[-1])
        if not np.isfinite(sd) or sd <= 0:
            return None
        d = price * abs(np.expm1(-side * 2 * sd))
    stop = tick_round(entry - side * Decimal(str(d)), tick, ROUND_FLOOR if side == 1 else ROUND_CEILING)
    dist = abs(entry - stop)
    if dist <= 0 or dist > MAX_STOP_PCT * entry:
        return None
    mult = Decimal("2.6" if kind == 0 else "2.5")
    tp = tick_round(entry + side * dist * mult, tick, ROUND_CEILING if side == 1 else ROUND_FLOOR)
    raw_qty = min(
        RISK_PCT * EQUITY / (float(dist) + FEE * price + STOP_FEE * float(stop)),
        MAX_NOTIONAL_MULT * EQUITY / price,
    )
    cap_qty = Decimal(str(EQUITY)) / (Decimal("0.10045") * entry)
    qty = min(Decimal(str(raw_qty)), cap_qty) // step * step
    notional = qty * entry
    if notional < min_notional:
        return None
    strat = "DeepSeek_Donchian55" if kind == 0 else "Claude_RADI"
    trail_note = f"Trail after +1.5R ({float(dist)*1.5:.4g}) by {'1.5*ATR' if kind==0 else '1.2*D'}"
    if kind == 1:
        trail_note += " | Exit if DZ flips < -1.0"
    ep_disp = tick_round(entry, tick, ROUND_HALF_UP)
    msg = (
        f"🔔 <b>{sym}</b> | <b>{'LONG' if side==1 else 'SHORT'}</b> | {strat}\n"
        f"Entry: <code>{ep_disp:f}</code>\n"
        f"Stop-Loss (SL): <code>{stop:f}</code> ({float(dist/entry)*100:.2f}%)\n"
        f"Take-Profit (TP): <code>{tp:f}</code>\n"
        f"Position Size: <code>{notional:.2f} USDT</code> / <code>{qty:f} Qty</code>\n"
        f"10x Isolated Margin: <code>{notional/10:.2f} USDT</code>\n"
        f"Rule: {trail_note}"
    )
    return (kind, -(score if kind == 0 else float(rvr.iloc[-1])), msg)

def send_telegram(text):
    print(text)
    if not BOT_TOKEN or not CHAT_ID:
        return
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()

def main():
    filters, funding = get_filters_and_funding()
    now_ms = int(time.time() * 1000)
    tasks = [(s, filters[s], funding.get(s, 0.0), now_ms) for s in SYMBOLS if s in filters]
    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = [res for res in pool.map(scan_symbol, tasks) if res is not None]
    hits.sort(key=lambda x: (x[0], x[1]))
    if hits:
        header = f"📊 <b>4H Scanner ({len(hits)} Signal{'s' if len(hits)>1 else ''})</b> — Max 3 Slots / Max 2 Same-Side\n\n"
        send_telegram(header + "\n\n".join(h[2] for h in hits[:5]))
    elif FORCE_TEST:
        mode = "Vision Fallback" if GEO_BLOCKED else "Futures Live"
        send_telegram(f"✅ <b>4H Scanner Heartbeat ({mode}):</b> Equity ${EQUITY:.2f} | 0 signals this bar.")

if __name__ == "__main__":
    main()
    
