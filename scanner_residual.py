import os, json, time, requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://okx.com"
STATE_FILE = Path("active_trades_residual.json")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

ALTS = [
    "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP",
    "ADA-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "NEAR-USDT-SWAP",
    "UNI-USDT-SWAP", "AAVE-USDT-SWAP", "BNB-USDT-SWAP", "DOT-USDT-SWAP",
    "BCH-USDT-SWAP", "LTC-USDT-SWAP", "ATOM-USDT-SWAP", "ETC-USDT-SWAP",
    "FIL-USDT-SWAP", "HBAR-USDT-SWAP", "ALGO-USDT-SWAP"
]

Z_ENTRY = 2.50
SL_MULT = 2.50
MAX_HOLD = 5
MAX_SLOTS = 3

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[TG DRY-RUN]\n" + msg)
        return
    try:
        requests.post(
            f"https://telegram.org{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram send error: {e}")

def fetch_1d_closed(inst_id: str, limit: int = 140) -> pd.DataFrame:
    for a in range(3):
        try:
            r = requests.get(
                f"{BASE}/api/v5/market/candles",
                params={"instId": inst_id, "bar": "1Dutc", "limit": str(limit)},
                timeout=12
            )
            r.raise_for_status()
            rows = r.json().get("data", [])
            closed = [x for x in rows if len(x) >= 9 and x[8] == "1"]
            df = pd.DataFrame(closed, columns=["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"])
            df["ts"] = df["ts"].astype(int)
            df["c"] = df["c"].astype(float)
            df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
            df["ret"] = df["c"].pct_change()
            return df
        except Exception:
            if a == 2:
                raise
            time.sleep(2 ** a)

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_processed_ts": 0, "active": [], "closed": []}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def run_residual_scanner():
    state = load_state()
    btc_df = fetch_1d_closed("BTC-USDT-SWAP", 140)
    if len(btc_df) < 125:
        print("Insufficient BTC 1D history.")
        return

    latest_ts = int(btc_df["ts"].iloc[-1])
    day_str = datetime.fromtimestamp(latest_ts / 1000, timezone.utc).strftime("%Y-%m-%d")

    if latest_ts <= state.get("last_processed_ts", 0):
        print(f"[{day_str}] Daily residual scan already completed for this 1D close.")
        return

    btc_s = btc_df.set_index("ts")
    metrics = {}
    for inst in ALTS:
        try:
            df = fetch_1d_closed(inst, 140).set_index("ts")
            aligned = pd.DataFrame({"alt": df["ret"], "btc": btc_s["ret"], "px": df["c"]}).dropna()
            if len(aligned) < 122:
                continue

            # Rolling 60D beta & residual series (strictly prior 60 days for each bar)
            cov = aligned["alt"].rolling(60).cov(aligned["btc"])
            var = aligned["btc"].rolling(60).var()
            beta = (cov / var).shift(1).clip(0.2, 3.0)
            resid_1d = aligned["alt"] - beta * aligned["btc"]
            resid_3d = resid_1d.rolling(3).sum()
            z = (resid_3d - resid_3d.rolling(60).mean().shift(1)) / resid_3d.rolling(60).std().shift(1)
            res_vol = resid_1d.rolling(60).std().shift(1)

            last = aligned.iloc[-1]
            metrics[inst] = {
                "sym": inst.replace("-USDT-SWAP", "USDT"),
                "px": float(last["px"]),
                "alt_r": float(last["alt"]),
                "btc_r": float(last["btc"]),
                "z": float(z.iloc[-1]),
                "beta": float(beta.iloc[-1]),
                "vol": float(res_vol.iloc[-1])
            }
        except Exception as e:
            print(f"Skip {inst}: {e}")

    btc_px = float(btc_s["c"].iloc[-1])
    still_open = []

    # 1. Evaluate Active Pairs on Newly Closed Daily Bar
    for tr in state.get("active", []):
        inst = tr["inst"]
        m = metrics.get(inst)
        if not m:
            still_open.append(tr)
            continue

        pair_r = tr["side"] * (m["alt_r"] - tr["beta"] * m["btc_r"])
        tr["cum_pnl"] += pair_r
        tr["days"] += 1

        hit_sl = tr["cum_pnl"] <= -tr["stop_dist"]
        hit_tp = (tr["side"] == 1 and m["z"] >= 0.0) or (tr["side"] == -1 and m["z"] <= 0.0)
        hit_time = tr["days"] >= MAX_HOLD

        if hit_sl or hit_tp or hit_time:
            raw_pnl = max(tr["cum_pnl"], -tr["stop_dist"]) if hit_sl else tr["cum_pnl"]
            friction = 0.0028 + (0.0003 * tr["days"])
            net_r = (raw_pnl - friction) / tr["stop_dist"]
            reason = "STOP LOSS (-1R)" if hit_sl else ("MEAN REVERT (Z->0)" if hit_tp else "TIME EXIT (5D)")

            tr["exit_date"] = day_str
            tr["exit_reason"] = reason
            tr["net_r"] = round(net_r, 3)
            state.setdefault("closed", []).append(tr)

            dir_lbl = f"LONG {tr['sym']} / SHORT BTC" if tr["side"] == 1 else f"SHORT {tr['sym']} / LONG BTC"
            send_tg(
                f"📐 *[RESIDUAL 1D] CLOSED [{reason}]*\n"
                f"Pair: `{dir_lbl}`\n"
                f"Hold: `{tr['days']}d` | Exit Z: `{m['z']:+.2f}`\n"
                f"Net R: `{net_r:+.2f}R`"
            )
        else:
            still_open.append(tr)

    state["active"] = still_open

    # 2. Scan New Entries (Max 3 Concurrent Pairs)
    if len(state["active"]) < MAX_SLOTS:
        open_insts = {t["inst"] for t in state["active"]}
        cands = []
        for inst, m in metrics.items():
            if inst in open_insts or np.isnan(m["z"]) or np.isnan(m["vol"]) or m["vol"] <= 0.005:
                continue
            if abs(m["z"]) > Z_ENTRY:
                side = 1 if m["z"] < -Z_ENTRY else -1
                cands.append((abs(m["z"]), inst, side, m))

        cands.sort(key=lambda x: x[0], reverse=True)
        for _, inst, side, m in cands[:MAX_SLOTS - len(state["active"])]:
            stop_dist = m["vol"] * SL_MULT
            new_tr = {
                "inst": inst,
                "sym": m["sym"],
                "side": side,
                "entry_date": day_str,
                "alt_entry": m["px"],
                "btc_entry": btc_px,
                "beta": round(m["beta"], 3),
                "entry_z": round(m["z"], 2),
                "stop_dist": round(stop_dist, 5),
                "cum_pnl": 0.0,
                "days": 0
            }
            state["active"].append(new_tr)

            dir_lbl = f"LONG {m['sym']} + SHORT BTC ({m['beta']:.2f}x)" if side == 1 else f"SHORT {m['sym']} + LONG BTC ({m['beta']:.2f}x)"
            send_tg(
                f"📐 *[RESIDUAL 1D] NEW PAIR SIGNAL*\n"
                f"Legs: `{dir_lbl}`\n"
                f"Entry: `{m['sym']} @ {m['px']}` | `BTC @ {btc_px}`\n"
                f"3D Residual Z: `{m['z']:+.2f}` | 1R Pair Stop: `{stop_dist*100:.2f}%`\n"
                f"Exit: `Z -> 0.0` or `5D Max Hold` | Slots: `{len(state['active'])}/{MAX_SLOTS}`"
            )

    state["last_processed_ts"] = latest_ts
    save_state(state)
    print(f"[{day_str}] Residual 1D scan finished. Active pairs: {len(state['active'])}/{MAX_SLOTS}")

if __name__ == "__main__":
    run_residual_scanner()
          
