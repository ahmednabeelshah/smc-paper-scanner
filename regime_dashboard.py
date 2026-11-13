import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

CORE_12 = [
    "ETH-USDT-SWAP", "SOL-USDT-SWAP", "LINK-USDT-SWAP", "AVAX-USDT-SWAP",
    "SUI-USDT-SWAP", "NEAR-USDT-SWAP", "UNI-USDT-SWAP", "AAVE-USDT-SWAP",
    "BNB-USDT-SWAP", "DOT-USDT-SWAP", "LTC-USDT-SWAP", "BCH-USDT-SWAP"
]

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"

def fetch_4h_closed(inst_id: str, limit: int = 80) -> pd.DataFrame:
    resp = requests.get(
        OKX_CANDLES_URL,
        params={"instId": inst_id, "bar": "4H", "limit": str(limit)},
        timeout=10
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    # OKX returns newest first; filter strictly for confirmed/closed candles (confirm == "1")
    closed = [r for r in rows if len(r) >= 9 and r[8] == "1"]
    if len(closed) < 55:
        raise ValueError(f"Insufficient closed 4H candles for {inst_id}: {len(closed)}")

    df = pd.DataFrame(closed, columns=["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"])
    df["ts"] = df["ts"].astype(int)
    df["c"] = df["c"].astype(float)
    df = df.sort_values("ts").reset_index(drop=True)
    df["ema20"] = df["c"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["c"].ewm(span=50, adjust=False).mean()
    return df

def run_regime_dashboard():
    now_utc = datetime.now(timezone.utc)
    hr_utc = now_utc.hour
    session_active = 8 <= hr_utc <= 18

    print("=" * 72)
    print(f"MARKET REGIME & BREADTH MONITOR | {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 72)

    # 1. BTC 4H Macro Regime
    try:
        btc = fetch_4h_closed("BTC-USDT-SWAP")
        last = btc.iloc[-1]
        btc_close, btc_e20, btc_e50 = last["c"], last["ema20"], last["ema50"]
        btc_bear = btc_close < btc_e50

        if btc_close < btc_e50 and btc_e20 < btc_e50:
            regime = "BEAR TREND ([v5.4] Short Gate OPEN)"
        elif btc_close > btc_e50 and btc_e20 > btc_e50:
            regime = "BULL TREND ([v5.4] Short Gate LOCKED)"
        else:
            regime = "SIDEWAYS / TRANSITION (Chop Risk)"

        print(f"BTC 4H Close   : ${btc_close:,.2f} | EMA20: ${btc_e20:,.2f} | EMA50: ${btc_e50:,.2f}")
        print(f"Macro Regime   : {regime}")
        print(f"London/NY Gate : {'OPEN (08:00-18:00 UTC)' if session_active else 'CLOSED (Outside 08:00-18:00 UTC)'}")
    except Exception as e:
        print(f"BTC Regime Check Failed: {e}")
        return

    # 2. Core-12 Altcoin 4H Trend Breadth
    print("\n--- Core-12 Universe 4H Trend Breadth ---")
    bear_aligned = []
    bull_aligned = []
    mixed = []
    failed = []

    for inst in CORE_12:
        sym = inst.replace("-USDT-SWAP", "USDT")
        try:
            df = fetch_4h_closed(inst)
            row = df.iloc[-1]
            c, e20, e50 = row["c"], row["ema20"], row["ema50"]
            dist_e50 = ((c - e50) / e50) * 100.0
            if c < e50 and e20 < e50:
                bear_aligned.append((sym, dist_e50))
            elif c > e50 and e20 > e50:
                bull_aligned.append((sym, dist_e50))
            else:
                mixed.append((sym, dist_e50))
        except Exception as err:
            failed.append(f"{sym} ({err})")

    print(f"Bearish 4H Alignment (C < EMA50 & EMA20 < EMA50) : {len(bear_aligned)}/12")
    if bear_aligned:
        bear_aligned.sort(key=lambda x: x[1])
        formatted = ", ".join([f"{s} ({d:+.2f}%)" for s, d in bear_aligned])
        print(f"  Eligible Short Candidates : {formatted}")

    print(f"Bullish 4H Alignment (C > EMA50 & EMA20 > EMA50) : {len(bull_aligned)}/12")
    print(f"Mixed / Choppy 4H Alignment                      : {len(mixed)}/12")
    if failed:
        print(f"Fetch Errors                                     : {', '.join(failed)}")

    # 3. Proprietary OI & Funding Telemetry Check (if csv exists)
    csv_path = Path("oi_funding_log.csv")
    if csv_path.exists():
        try:
            oi_df = pd.read_csv(csv_path)
            unique_hours = oi_df["timestamp_utc"].nunique()
            latest_ts = oi_df["timestamp_utc"].iloc[-1]
            btc_rows = oi_df[oi_df["symbol"] == "BTCUSDT"]
            btc_fr = f"{btc_rows['funding_rate'].iloc[-1]*100:+.4f}%" if not btc_rows.empty else "N/A"
            print("\n--- Proprietary OI & Funding Log Status ---")
            print(f"Logged Hours : {unique_hours} | Latest Snapshot: {latest_ts} | BTC Funding: {btc_fr}")
        except Exception as e:
            print(f"\nOI Log Read Warning: {e}")

    print("=" * 72)

if __name__ == "__main__":
    run_regime_dashboard()
