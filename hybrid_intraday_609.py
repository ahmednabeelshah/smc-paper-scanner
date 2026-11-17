import time
import requests
import pandas as pd

# Configuration
BASE_URL = "https://fapi.binance.com"
MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
RISK_PCT = 0.015  # 1.5% equity risk per trade

def get_klines(symbol, interval="1h", limit=300):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    res = requests.get(url, params=params)
    df = pd.DataFrame(res.json(), columns=["ts", "Open", "High", "Low", "Close", "Volume", "close_time", "qav", "num_trades", "tbv", "tqv", "ignore"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def scan_hybrid_609():
    signals = []
    
    btc_df = get_klines("BTCUSDT", "1h", 300)
    if btc_df.empty: return signals
    btc_ema200 = btc_df["Close"].ewm(span=200, adjust=False).mean().iloc[-2]
    btc_macro_up = btc_df["Close"].iloc[-2] > btc_ema200
    btc_macro_dn = btc_df["Close"].iloc[-2] < btc_ema200
    
    for sym in MAJORS:
        df = get_klines(sym, "1h", 300)
        if len(df) < 250: continue
        
        closed_df = df.iloc[:-1].copy()
        cl, hi, lo, op, vo = closed_df["Close"], closed_df["High"], closed_df["Low"], closed_df["Open"], closed_df["Volume"]
        
        rng = hi - lo
        atr = rng.rolling(14).mean()
        v_rel = vo / vo.rolling(20).mean()
        ema200 = cl.ewm(span=200, adjust=False).mean()
        
        d = cl.diff()
        up = d.clip(lower=0).ewm(alpha=1/6, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1/6, adjust=False).mean()
        rsi6 = 100 - (100 / (1 + up / dn))
        
        lo_w = (closed_df[["Open", "Close"]].min(axis=1) - lo) / rng
        up_w = (hi - closed_df[["Open", "Close"]].max(axis=1)) / rng
        
        curr_cl, curr_op, curr_v_rel, curr_ema200 = cl.iloc[-1], op.iloc[-1], v_rel.iloc[-1], ema200.iloc[-1]
        curr_lo_w, curr_up_w, curr_atr = lo_w.iloc[-1], up_w.iloc[-1], atr.iloc[-1]
        prev_rsi6 = rsi6.iloc[-2]
        
        c_l = (btc_macro_up and curr_cl > curr_ema200 and prev_rsi6 < 25 and curr_lo_w >= 0.45 and curr_cl > curr_op and curr_v_rel > 1.3)
        c_s = (btc_macro_dn and curr_cl < curr_ema200 and prev_rsi6 > 75 and curr_up_w >= 0.45 and curr_cl < curr_op and curr_v_rel > 1.3)
               
        if c_l or c_s:
            side = 1 if c_l else -1
            entry_px = float(df["Open"].iloc[-1]) 
            stop_pct = (2.0 * curr_atr) / entry_px
            
            signals.append({
                "symbol": sym,
                "side": "LONG" if side == 1 else "SHORT",
                "entry": entry_px,
                "sl_price": entry_px * (1 - stop_pct) if side == 1 else entry_px * (1 + stop_pct),
                "tp1_price": entry_px * (1 + stop_pct) if side == 1 else entry_px * (1 - stop_pct),
                "trail_dist": 1.5 * curr_atr,
                "timestamp": df.index[-1].strftime("%Y-%m-%d %H:%M:%S UTC")
            })
            
    return signals

if __name__ == '__main__':
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC')}] Scanning SMC Paper Trading Hybrid #609...")
    setups = scan_hybrid_609()
    
    if not setups:
        print("No setups found. Waiting for next hourly close.")
    else:
        for s in setups:
            print(f"\n[+] VALID SETUP: {s['symbol']} | {s['side']}")
            print(f"    Entry: {s['entry']:.4f}")
            print(f"    SL:    {s['sl_price']:.4f} (Dynamic 2.0x ATR)")
            print(f"    TP1:   {s['tp1_price']:.4f} (50% Position Base-Hit)")
            print(f"    Trail: {s['trail_dist']:.4f} (1.5x ATR trailing on remaining 50%)")
          
