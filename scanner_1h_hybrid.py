#!/usr/bin/env python3
"""
scanner_1h_hybrid.py — Live Production 1H Short Scanner & Trailing Position Manager
====================================================================================
Locked Architecture (Validated Across 15 Frontier AI Models + Institutional Risk Audits):
  - Signal A: DS1_StrictFlow (1H 24H-high failed sweep + TBR >= 0.55 + Funding > 0 + Wick > 55% + BTC <= 24H high)
  - Signal B: CGPT_M1 (Astra 6.2 48H OLS Beta-Residual Trap, score >= 0.62)
  - Bucket Risk Tiers (Base r = 2.0%):
      * Consensus (A & B agree): 1.75x (3.5% risk)
      * DS1_StrictFlow Only:     1.00x (2.0% risk)
      * CGPT_M1 Only:            0.35x (0.7% risk)
  - Guards & Sizing:
      * Fee-adjusted notional: Notional = (Equity * r_eff) / (SL% + c_sl), Leverage 5x-10x, Margin <= 15% Eq
      * Skip SL < 1.5%, Clamp SL at 6.0%, Max 5 open slots, Max 6.0% aggregate open stop risk
      * Correlation Cluster Cap: Max 2 open shorts in coins with 168h return correlation >= 0.75
      * BTC Volatility & Squeeze Throttle: 0.50x-1.00x (caps at 0.60x if BTC 1H > +1% & median funding < 0)
      * 12h per-coin cooldown + 12h global pause if 2 stop-losses hit within 6 hours
  - Full Trailing Exit Lifecycle (Hourly Alerts):
      * 12H Stalled-Fade Exit: Close 100% if MFE < 0.5R at 12 hours
      * TP1 @ 2.3R: Close 50%, lock stop-loss at +1.5R profit, trail remaining 50% by 1.0 * ATR(14)
      * 36H Max Hold: Close any remaining position at 36 hours
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import time
import warnings
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# =============================================================================
# 1. LOCKED CONFIGURATION
# =============================================================================
SYMBOLS = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "UNIUSDT",
    "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "SUIUSDT", "TIAUSDT", "SEIUSDT", "RENDERUSDT", "FETUSDT", "WIFUSDT",
]
ALL_SYMBOLS = SYMBOLS + ["BTCUSDT"]

STATE_FILE = os.getenv("STATE_FILE", "state_1h_hybrid.json")
INITIAL_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "100.0"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FORCE_REPORT = os.getenv("FORCE_REPORT", "0").strip() == "1"

R_BASE = 0.020                      # 2.0% base risk per trade
TIER_MULT = {1: 1.75, 2: 1.00, 3: 0.00}  # 1=Consensus, 2=DS1_SF Only, 3=CGPT_M1 Only
TIER_LABEL = {
    1: "CONSENSUS (DS1_SF + CGPT_M1) [1.75x]",
    2: "DS1_StrictFlow Only [1.00x]",
    3: "CGPT_M1 Beta-Trap Only [0.35x]",
}
D_MIN, D_MAX = 0.015, 0.060         # Skip < 1.5% SL, clamp > 6.0% SL
MAX_SLOTS = 5
MAX_AGG_RISK = 0.060                # 6.0% max open portfolio stop risk
MAX_NOTIONAL_MULT = 2.0             # Max 2.0x total gross leverage
MAX_MARGIN_PCT = 0.15               # Max 15% equity margin per single trade
CORR_THRESHOLD = 0.75               # Max 2 open shorts in >= 0.75 correlation cluster
COOLDOWN_HOURS = 12
PAUSE_HOURS = 12
TP1_R = 2.3
LOCK_R = 1.5
MAX_HOLD_HOURS = 36
EPS = 1e-12


# =============================================================================
# 2. TELEGRAM & STATE PERSISTENCE HELPERS
# =============================================================================
def send_telegram(text: str) -> None:
    print("\n" + text + "\n")
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=12,
        )
    except Exception as exc:
        print(f"[Telegram Warning] {exc}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("equity", INITIAL_EQUITY)
            st.setdefault("week_id", "")
            st.setdefault("week_eq", st["equity"])
            st.setdefault("positions", {})
            st.setdefault("cooldowns", {})
            st.setdefault("stop_timestamps", [])
            st.setdefault("last_signal_bar", "")
            return st
        except Exception as exc:
            print(f"[State Warning] Resetting corrupted state: {exc}")
    return {
        "equity": INITIAL_EQUITY,
        "week_id": "",
        "week_eq": INITIAL_EQUITY,
        "positions": {},
        "cooldowns": {},
        "stop_timestamps": [],
        "last_signal_bar": "",
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# =============================================================================
# 3. BINANCE DATA FETCHER (FAPI WITH VISION + BYBIT FALLBACK)
# =============================================================================
def check_fapi() -> bool:
    try:
        return requests.get("https://fapi.binance.com/fapi/v1/ping", timeout=4).status_code == 200
    except Exception:
        return False


def fetch_symbol_1h(sym: str, n_bars: int, fapi_ok: bool) -> tuple[str, pd.DataFrame | None, pd.Series | None]:
    sess = requests.Session()
    k_url = "https://fapi.binance.com/fapi/v1/klines" if fapi_ok else "https://data-api.binance.vision/api/v3/klines"
    end_ms = int(pd.Timestamp.now(tz="UTC").floor("h").timestamp() * 1000)
    start_ms = end_ms - n_bars * 3600_000

    rows, cur = [], start_ms
    while cur < end_ms:
        try:
            r = sess.get(
                k_url,
                params={"symbol": sym, "interval": "1h", "startTime": cur, "endTime": end_ms, "limit": 1000},
                timeout=12,
            )
            data = r.json() if r.status_code == 200 else None
        except Exception:
            data = None
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        last_ot = int(data[-1][0])
        if last_ot <= cur:
            break
        cur = last_ot + 3600_000

    if not rows:
        return sym, None, None

    df = pd.DataFrame(rows, columns=["ot", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tqv", "ig"])
    df = df.drop_duplicates("ot").sort_values("ot")
    idx = pd.to_datetime(df["ot"].astype(np.int64), unit="ms", utc=True)
    kd = pd.DataFrame(df[["o", "h", "l", "c", "v", "qv", "tbv"]].astype(float).to_numpy(), index=idx)

    # Funding rate (only needed for 24 alts, trailing ~200h)
    f_ser = pd.Series(0.0001, index=idx)
    if sym != "BTCUSDT":
        f_start = end_ms - 240 * 3600_000
        if fapi_ok:
            try:
                r = sess.get(
                    "https://fapi.binance.com/fapi/v1/fundingRate",
                    params={"symbol": sym, "startTime": f_start, "endTime": end_ms, "limit": 500},
                    timeout=10,
                )
                fd = r.json() if r.status_code == 200 else None
                if isinstance(fd, list) and fd:
                    fdf = pd.DataFrame(fd).drop_duplicates("fundingTime").sort_values("fundingTime")
                    f_ser = pd.Series(
                        fdf["fundingRate"].astype(float).to_numpy(),
                        index=pd.to_datetime(fdf["fundingTime"].astype(np.int64), unit="ms", utc=True).dt.floor("h"),
                    )
                    f_ser = f_ser[~f_ser.index.duplicated(keep="last")]
            except Exception:
                pass
        else:
            try:
                r = sess.get(
                    "https://api.bybit.com/v5/market/funding/history",
                    params={"category": "linear", "symbol": sym, "endTime": end_ms, "limit": 200},
                    timeout=10,
                )
                lst = r.json().get("result", {}).get("list", []) if r.status_code == 200 else []
                if lst:
                    fdf = pd.DataFrame(lst).drop_duplicates("fundingRateTimestamp").sort_values("fundingRateTimestamp")
                    f_ser = pd.Series(
                        fdf["fundingRate"].astype(float).to_numpy(),
                        index=pd.to_datetime(fdf["fundingRateTimestamp"].astype(np.int64), unit="ms", utc=True).dt.floor("h"),
                    )
                    f_ser = f_ser[~f_ser.index.duplicated(keep="last")]
            except Exception:
                pass

    return sym, kd, f_ser


def fetch_live_prices(fapi_ok: bool) -> dict[str, float]:
    url = "https://fapi.binance.com/fapi/v1/ticker/price" if fapi_ok else "https://data-api.binance.vision/api/v3/ticker/price"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            return {x["symbol"]: float(x["price"]) for x in r.json() if x["symbol"] in ALL_SYMBOLS}
    except Exception:
        pass
    return {}


# =============================================================================
# 4. CAUSAL VECTORIZED INDICATORS & HYBRID SIGNAL ENGINE
# =============================================================================
def lag(a: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full_like(a, np.nan, dtype=float)
    if k == 0:
        out[:] = a
    elif k > 0:
        out[k:] = a[:-k]
    return out


def roll(a: np.ndarray, w: int, op: str, minp: int | None = None) -> np.ndarray:
    mp = w if minp is None else minp
    return getattr(pd.DataFrame(a.reshape(len(a), -1)).rolling(w, min_periods=mp), op)().to_numpy().reshape(a.shape)


def atr_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.where(np.isfinite(lag(c, 1)), lag(c, 1), c)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.DataFrame(tr.reshape(len(tr), -1)).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy().reshape(h.shape)


def atr_sma(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = lag(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return roll(tr, n, "mean")


def rz(x: np.ndarray, w: int, minp: int | None = None, floor: float = 1e-5, excl: bool = False) -> np.ndarray:
    b = lag(x, 1) if excl else x
    return np.clip((x - roll(b, w, "mean", minp)) / np.maximum(roll(b, w, "std", minp), floor), -5.0, 5.0)


def ramp(x: np.ndarray, a: float, b: float) -> np.ndarray:
    return np.nan_to_num(np.clip((x - a) / (b - a), 0.0, 1.0), nan=0.0)


def compute_signals_and_regime(H1: np.ndarray, PAY: np.ndarray, GRID: pd.DatetimeIndex) -> dict:
    hr1 = GRID.hour.to_numpy()
    o1, h1, l1, c1, v1, b1 = H1[:, :24, 0], H1[:, :24, 1], H1[:, :24, 2], H1[:, :24, 3], H1[:, :24, 4], H1[:, :24, 6]
    bh1, bl1, bc1 = H1[:, 24, 1], H1[:, 24, 2], H1[:, 24, 3]
    f_now = pd.DataFrame(PAY[:, :24]).ffill().fillna(0.0001).to_numpy()

    rng1 = np.maximum(h1 - l1, EPS)
    uw1 = (h1 - np.maximum(o1, c1)) / rng1
    tbr1 = np.clip(b1 / np.maximum(v1, EPS), 0.0, 1.0)
    a14 = atr_wilder(h1, l1, c1, 14)
    sa14 = atr_sma(h1, l1, c1, 14)
    zf168_lag = rz(f_now, 168, 168, 1e-5, excl=True)
    ph24 = lag(roll(h1, 24, "max"), 1)
    bph24 = lag(roll(bh1, 24, "max"), 1)[:, None]

    # Model A: DS1_StrictFlow (1H Short)
    m_sf = (
        np.isfinite(ph24)
        & (h1 > ph24)
        & (c1 < ph24)
        & (bh1[:, None] <= bph24)
        & (tbr1 >= 0.55)
        & (f_now > 0.0)
        & (uw1 > 0.55)
        & ((hr1 >= 13) & (hr1 < 20))[:, None]
    )
    sl_sf = np.where(m_sf, h1 + 0.30 * a14, np.nan)

    # Model B: CGPT_M1 (Astra 6.2 Beta-Residual Trap, thr=0.62)
    ra = np.log(c1 / np.maximum(lag(c1, 1), EPS))
    rb = np.log(bc1 / np.maximum(lag(bc1, 1), EPS))[:, None]
    mx48, my48 = roll(rb, 48, "mean"), roll(ra, 48, "mean")
    vx48 = roll(rb * rb, 48, "mean") - mx48 ** 2
    vy48 = roll(ra * ra, 48, "mean") - my48 ** 2
    cov48 = roll(ra * rb, 48, "mean") - mx48 * my48
    b_ols = cov48 / np.where(vx48 > EPS, vx48, np.nan)
    se_ols = np.sqrt(np.maximum((vy48 - b_ols * cov48) * 48.0 / 46.0, 0.0))
    z_ols = (ra - my48 - b_ols * (rb - mx48)) / np.where(se_ols > 1e-10, se_ols, np.nan)

    F_cg = np.clip(0.5 + zf168_lag / 4.0, 0.0, 1.0)
    clk_cg = np.clip(np.minimum(hr1 - 11.0, 21.0 - hr1) / 2.0, 0.0, 1.0)[:, None]
    S_cg1 = (
        0.35 * np.nan_to_num(np.clip(0.5 + z_ols / 4.0, 0.0, 1.0))
        + 0.25 * ramp(tbr1, 0.50, 0.65)
        + 0.20 * F_cg
        + 0.20 * uw1 * clk_cg
    )
    m_cg = (h1 > ph24) & (c1 < ph24) & ((hr1 >= 12) & (hr1 <= 20))[:, None] & (S_cg1 >= 0.62)
    sl_cg = np.where(m_cg, np.maximum(h1, ph24) + 0.30 * sa14, np.nan)

    # BTC Volatility Throttle + DeepSeek Squeeze Guard
    btc_ret = pd.Series(np.log(bc1 / np.maximum(lag(bc1, 1), EPS)))
    vol72 = btc_ret.rolling(72, min_periods=36).std()
    vol180d_med = vol72.rolling(180 * 24, min_periods=720).median()
    throttle_arr = np.nan_to_num(np.clip((vol180d_med / vol72.replace(0, np.nan)).to_numpy(), 0.50, 1.00), nan=1.00)

    r_btc1h = np.zeros_like(bc1)
    r_btc1h[1:] = bc1[1:] / bc1[:-1] - 1.0
    squeeze = (r_btc1h > 0.010) & (np.nanmedian(f_now, axis=1) < 0.0)
    throttle_arr = np.where(squeeze, np.minimum(throttle_arr, 0.60), throttle_arr)

    # Trailing 168h return correlation matrix for the latest closed bar
    corr_mat = pd.DataFrame(ra[-168:]).corr().to_numpy()

    s = len(GRID) - 1  # Latest closed 1H bar
    both = m_sf[s] & m_cg[s]
    a_only = m_sf[s] & ~m_cg[s]
    b_only = m_cg[s] & ~m_sf[s]
    bkt = np.where(both, 1, np.where(a_only, 2, np.where(b_only, 3, 0)))
    raw_sl = np.where(both, np.maximum(sl_sf[s], sl_cg[s]), np.where(a_only, sl_sf[s], np.where(b_only, sl_cg[s], np.nan)))

    return {
        "bar_ts": GRID[s],
        "bkt": bkt,
        "raw_sl": raw_sl,
        "score_cg": S_cg1[s],
        "tbr": tbr1[s],
        "funding": f_now[s],
        "atr14": a14[s],
        "last_hi": h1[s],
        "last_lo": l1[s],
        "last_cl": c1[s],
        "throttle": float(throttle_arr[s]),
        "squeeze_active": bool(squeeze[s]),
        "corr_mat": corr_mat,
    }


# =============================================================================
# 5. MAIN EXECUTION LOOP (MANAGE TRAILING EXITS + SCAN NEW ENTRIES)
# =============================================================================
def run_scanner(H1: np.ndarray | None = None, PAY: np.ndarray | None = None, GRID: pd.DatetimeIndex | None = None, live_px: dict[str, float] | None = None) -> None:
    now_utc = pd.Timestamp.now(tz="UTC")
    state = load_state()

    # Weekly Monday 00:00 UTC equity snapshot
    wk_id = str(now_utc.tz_localize(None).to_period("W-SUN"))
    if state["week_id"] != wk_id:
        state["week_id"] = wk_id
        state["week_eq"] = float(state["equity"])

    fapi_ok = check_fapi() if H1 is None else True
    if H1 is None or PAY is None or GRID is None:
        end_dt = now_utc.floor("h")
        # Fetch 4500 bars (~187 days) so BTC 180d volatility median has full history
        GRID = pd.date_range(end_dt - pd.Timedelta(hours=4500), end_dt - pd.Timedelta(hours=1), freq="1h", tz="UTC")
        H1 = np.full((len(GRID), 25, 7), np.nan)
        PAY = np.full((len(GRID), 25), np.nan)
        smap = {s: j for j, s in enumerate(ALL_SYMBOLS)}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_symbol_1h, s, 4500 if s == "BTCUSDT" else 420, fapi_ok) for s in ALL_SYMBOLS]
            for fut in as_completed(futs):
                sym, kd, fs = fut.result()
                if kd is not None:
                    H1[:, smap[sym], :] = kd.reindex(GRID).to_numpy()
                    if fs is not None:
                        PAY[:, smap[sym]] = fs.reindex(GRID).to_numpy()

    if live_px is None:
        live_px = fetch_live_prices(fapi_ok)

    sig = compute_signals_and_regime(H1, PAY, GRID)
    bar_ts = sig["bar_ts"]
    bar_str = bar_ts.strftime("%Y-%m-%d %H:%M UTC")
    alerts: list[str] = []

    # -------------------------------------------------------------------------
    # STEP A: MANAGE OPEN POSITIONS (SL HIT, TP1 2.3R, TRAILING SL, 12H/36H EXIT)
    # -------------------------------------------------------------------------
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        j = SYMBOLS.index(sym)
        hi_bar = float(sig["last_hi"][j])
        lo_bar = float(sig["last_lo"][j])
        cl_bar = float(sig["last_cl"][j])
        atr_now = float(sig["atr14"][j])
        cur_px = float(live_px.get(sym, cl_bar))

        if not np.isfinite(cur_px) or cur_px <= 0:
            continue

        entry_ts = pd.Timestamp(pos["entry_ts"])
        hours_held = int(round((now_utc.floor("h") - entry_ts.floor("h")).total_seconds() / 3600.0))
        p_in = float(pos["entry"])
        sl_cur = float(pos["sl"])
        tp1 = float(pos["tp1"])
        r_dist = float(pos["r_dist"])
        pos["min_lo"] = min(float(pos.get("min_lo", p_in)), lo_bar if np.isfinite(lo_bar) else cur_px, cur_px)
        pos["min_cl"] = min(float(pos.get("min_cl", p_in)), cl_bar if np.isfinite(cl_bar) else cur_px)

        worst_px = max(hi_bar if np.isfinite(hi_bar) else cur_px, cur_px)
        best_px = min(lo_bar if np.isfinite(lo_bar) else cur_px, cur_px)

        # 1. Check Stop-Loss Hit
        if worst_px >= sl_cur:
            rem_frac = 0.50 if pos.get("runner_on", False) else 1.00
            pnl_r = (p_in - sl_cur) / max(r_dist, EPS)
            pnl_usd = pos["risk_usd"] * rem_frac * pnl_r
            state["equity"] = max(10.0, float(state["equity"]) + pnl_usd)
            state["cooldowns"][sym] = now_utc.isoformat()
            if pnl_r < 0:
                state["stop_timestamps"].append(now_utc.isoformat())
            state["positions"].pop(sym)
            tag = "TRAILING STOP HIT (Remaining 50%)" if pos.get("runner_on") else "STOP-LOSS HIT (100%)"
            alerts.append(
                f"🛑 <b>{tag} — {sym} SHORT</b>\n"
                f"• Exit: <code>{sl_cur:.6g}</code> (Entry: <code>{p_in:.6g}</code>)\n"
                f"• Realized: <b>{pnl_r:+.2f}R</b> ({pnl_usd:+.2f} USDT) | 12h Cooldown Active"
            )
            continue

        # 2. Check TP1 (2.3R) Partial Close + Runner Activation
        if not pos.get("runner_on", False) and best_px <= tp1:
            pos["runner_on"] = True
            pnl_usd_half = pos["risk_usd"] * 0.50 * TP1_R
            state["equity"] = float(state["equity"]) + pnl_usd_half
            lock_sl = p_in - LOCK_R * r_dist
            atr_sl = (pos["min_cl"] + 1.0 * atr_now) if np.isfinite(atr_now) else lock_sl
            new_sl = min(sl_cur, lock_sl, atr_sl)
            pos["sl"] = new_sl
            alerts.append(
                f"🎯 <b>TP1 HIT (+{TP1_R}R) — {sym} SHORT</b>\n"
                f"• <b>ACTION 1:</b> Close <b>50%</b> at <code>{tp1:.6g}</code> (+{pnl_usd_half:.2f} USDT)\n"
                f"• <b>ACTION 2:</b> Move SL on remaining 50% to <code>{new_sl:.6g}</code> (locks +{LOCK_R}R min)\n"
                f"• Trailing active at <code>1.0 × ATR14</code> up to 36h"
            )
            continue

        # 3. Update Trailing SL if Runner is Active
        if pos.get("runner_on", False) and np.isfinite(atr_now):
            cand_sl = pos["min_cl"] + 1.0 * atr_now
            if cand_sl < sl_cur * 0.9985:  # Only alert when SL tightens meaningfully (>0.15%)
                pos["sl"] = cand_sl
                locked_r = (p_in - cand_sl) / max(r_dist, EPS)
                alerts.append(
                    f"📉 <b>TRAILING SL UPDATE — {sym} SHORT</b>\n"
                    f"• Move SL: <code>{sl_cur:.6g}</code> → <b><code>{cand_sl:.6g}</code></b> (Locks <b>{locked_r:+.2f}R</b>)\n"
                    f"• Live Price: <code>{cur_px:.6g}</code> | Held: {hours_held}h / {MAX_HOLD_HOURS}h"
                )

        # 4. Check 12H Stalled-Fade Early Exit (if TP1 not reached & MFE < 0.5R)
        if not pos.get("runner_on", False) and hours_held == 12:
            mfe_r = (p_in - pos["min_lo"]) / max(r_dist, EPS)
            if mfe_r < 0.50:
                cur_r = (p_in - cur_px) / max(r_dist, EPS)
                pnl_usd = pos["risk_usd"] * cur_r
                state["equity"] = max(10.0, float(state["equity"]) + pnl_usd)
                state["cooldowns"][sym] = now_utc.isoformat()
                state["positions"].pop(sym)
                alerts.append(
                    f"⏱ <b>12H EARLY EXIT (Stalled Fade) — {sym} SHORT</b>\n"
                    f"• <b>ACTION:</b> Close <b>100% at Market</b> (~<code>{cur_px:.6g}</code>)\n"
                    f"• Reason: 12h MFE was only <b>{mfe_r:.2f}R</b> (&lt; 0.50R) | Est. P&L: <b>{cur_r:+.2f}R</b>"
                )
                continue

        # 5. Check 36H Max Hold Time Exit
        if hours_held >= MAX_HOLD_HOURS:
            rem_frac = 0.50 if pos.get("runner_on", False) else 1.00
            cur_r = (p_in - cur_px) / max(r_dist, EPS)
            pnl_usd = pos["risk_usd"] * rem_frac * cur_r
            state["equity"] = max(10.0, float(state["equity"]) + pnl_usd)
            state["cooldowns"][sym] = now_utc.isoformat()
            state["positions"].pop(sym)
            alerts.append(
                f"⌛ <b>36H MAX HOLD EXIT — {sym} SHORT</b>\n"
                f"• <b>ACTION:</b> Close remaining position at Market (~<code>{cur_px:.6g}</code>)\n"
                f"• Entry: <code>{p_in:.6g}</code> | Est. Final Leg: <b>{cur_r:+.2f}R</b>"
            )

    # -------------------------------------------------------------------------
    # STEP B: EVALUATE NEW HYBRID SHORT ENTRIES (PREVENT DUPLICATE BAR RUNS)
    # -------------------------------------------------------------------------
    if state.get("last_signal_bar") != bar_str:
        state["last_signal_bar"] = bar_str

        # Prune old stop timestamps > 12h ago
        recent_stops = []
        for ts_str in state.get("stop_timestamps", []):
            dt_s = pd.Timestamp(ts_str)
            if (now_utc - dt_s).total_seconds() <= 12 * 3600:
                recent_stops.append(ts_str)
        state["stop_timestamps"] = recent_stops

        # Check 6h Double-SL Circuit Breaker
        stops_6h = [s_t for s_t in recent_stops if (now_utc - pd.Timestamp(s_t)).total_seconds() <= 6 * 3600]
        paused = len(stops_6h) >= 2

        if paused:
            alerts.append("🚨 <b>CIRCUIT BREAKER ACTIVE:</b> 2 stop-losses hit within 6h — new shorts paused for 12h.")
        else:
            raw_cands = []
            for j in np.flatnonzero(sig["bkt"] > 0):
                sym = SYMBOLS[int(j)]
                pi = float(live_px.get(sym, sig["last_cl"][j]))
                sr = float(sig["raw_sl"][j])
                bkt = int(sig["bkt"][j])
                if bkt == 3 or TIER_MULT.get(bkt, 0.0) <= 0:
                    continue
                if not (np.isfinite(pi) and np.isfinite(sr)) or pi <= 0:
                    continue
                dr = (sr - pi) / pi
                if dr < D_MIN:
                    continue
                prio = (0 if bkt == 1 else (1 if bkt == 2 else 2), dr, int(j))
                raw_cands.append((prio, int(j), sym, bkt, pi, dr))

            raw_cands.sort(key=lambda x: x[0])
            eq_now = float(state["equity"])
            weq = float(state["week_eq"])
            corr_mat = sig["corr_mat"]

            for _, j, sym, bkt, pi, dr in raw_cands:
                if len(state["positions"]) >= MAX_SLOTS:
                    break
                if sym in state["positions"]:
                    continue

                # 12h per-symbol cooldown
                cd_str = state["cooldowns"].get(sym)
                if cd_str and (now_utc - pd.Timestamp(cd_str)).total_seconds() < COOLDOWN_HOURS * 3600:
                    continue

                # Correlation Cluster Cap (max 2 open shorts in >= 0.75 cluster)
                if len(state["positions"]) >= 2:
                    open_idxs = [SYMBOLS.index(osym) for osym in state["positions"]]
                    high_corr_cnt = sum(1 for oj in open_idxs if np.isfinite(corr_mat[j, oj]) and corr_mat[j, oj] >= CORR_THRESHOLD)
                    if high_corr_cnt >= 2:
                        continue

                dv = float(np.clip(dr, D_MIN, D_MAX))
                c_sl = 0.00045 + 0.00165 * (1.0 + dv)
                r_eff = R_BASE * TIER_MULT[bkt] * sig["throttle"]

                open_risk_pct = sum(float(v["risk_pct"]) for v in state["positions"].values())
                rem_risk = max(0.0, MAX_AGG_RISK - open_risk_pct)
                r_use = min(r_eff, rem_risk)
                if r_use < 0.25 * r_eff:
                    continue

                notional = (eq_now * r_use) / (dv + c_sl)
                open_notional = sum(float(v["notional"]) for v in state["positions"].values())
                rem_notional = max(0.0, MAX_NOTIONAL_MULT * eq_now - open_notional)
                notional = min(notional, rem_notional)

                lv = int(np.clip(round(notional / max(0.12 * weq, 1e-6)), 5, 10))
                margin_usd = notional / lv
                if margin_usd > MAX_MARGIN_PCT * eq_now:
                    margin_usd = MAX_MARGIN_PCT * eq_now
                    notional = margin_usd * 10
                    lv = 10

                open_margin = sum(float(v["margin"]) for v in state["positions"].values())
                if notional < 5.0 or (margin_usd + open_margin) > 0.70 * eq_now:
                    continue

                sl_px = pi * (1.0 + dv)
                tp1_px = pi * (1.0 - TP1_R * dv)
                r_dist = pi * dv
                actual_risk_usd = notional * (dv + c_sl)
                actual_risk_pct = actual_risk_usd / max(eq_now, 1e-6)

                state["positions"][sym] = {
                    "side": "SHORT",
                    "bucket": bkt,
                    "entry": pi,
                    "sl": sl_px,
                    "tp1": tp1_px,
                    "r_dist": r_dist,
                    "sl_pct": dv,
                    "notional": notional,
                    "margin": margin_usd,
                    "leverage": lv,
                    "risk_usd": actual_risk_usd,
                    "risk_pct": actual_risk_pct,
                    "entry_ts": now_utc.isoformat(),
                    "min_lo": pi,
                    "min_cl": pi,
                    "runner_on": False,
                }

                sq_note = " (Squeeze Guard 0.6x)" if sig["squeeze_active"] else ""
                alerts.append(
                    f"🔻 <b>NEW 1H HYBRID SHORT — {sym}</b>\n"
                    f"• Tier: <b>{TIER_LABEL[bkt]}</b>{sq_note}\n"
                    f"• Entry: <code>{pi:.6g}</code> | Leverage: <b>{lv}x</b> | Margin: <b>${margin_usd:.2f}</b> (Notional: ${notional:.2f})\n"
                    f"• Stop-Loss: <code>{sl_px:.6g}</code> (<b>{dv*100:.2f}%</b> | Risk: ${actual_risk_usd:.2f})\n"
                    f"• TP1 (Close 50%): <code>{tp1_px:.6g}</code> (<b>+{TP1_R}R</b>) | Remaining 50%: Trail 1.0×ATR14\n"
                    f"• TBR: {sig['tbr'][j]:.2f} | Funding: {sig['funding'][j]*100:+.4f}% | CGPT Score: {sig['score_cg'][j]:.2f}"
                )

    save_state(state)

    if alerts:
        header = f"📊 <b>1H HYBRID SCANNER ({bar_str})</b> | Open: {len(state['positions'])}/{MAX_SLOTS} | Eq: ${state['equity']:.2f}\n" + "—" * 28 + "\n"
        send_telegram(header + "\n\n".join(alerts))
    elif FORCE_REPORT:
        send_telegram(
            f"✅ <b>1H HYBRID HEARTBEAT ({bar_str})</b>\n"
            f"• No new entries or trailing triggers | Open Positions: {len(state['positions'])}/{MAX_SLOTS}\n"
            f"• BTC Throttle: {sig['throttle']:.2f}x | Ref Equity: ${state['equity']:.2f}"
        )
    else:
        print(f"[{bar_str}] Scan complete. No alerts triggered. Open positions: {len(state['positions'])}/{MAX_SLOTS}.")


if __name__ == "__main__":
    run_scanner()
