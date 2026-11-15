import os, glob, json, csv
from datetime import datetime, timezone

CSV_FILE = "master_trade_log.csv"
JSON_FILE = "master_trade_log.json"

CORE_7 = {
    "active_trades_vol_macd.json": "VOL-MACD 1D (CORE)",
    "active_trades_hvol_res.json": "HVOL-RES 1D (CORE)",
    "active_trades_macd.json": "MACD-TURN 1D (CORE)",
    "active_trades_atr_clv.json": "ATR-CLV 1D (CORE)",
    "active_trades_res_ch30.json": "RES-CH30 1D (CORE)",
    "active_trades_calm_res.json": "CALM-RES 1D (CORE)",
    "active_trades_calm_streak4.json": "CALM-STREAK4 1D (CORE)",
}

def fmt_side(s):
    if s in (1, "1", "LONG", "long"): return "LONG"
    if s in (-1, "-1", "SHORT", "short"): return "SHORT"
    return str(s)

def fmt_ts(val):
    if not val: return ""
    if isinstance(val, (int, float)) and val > 1e11:
        return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(val)

def extract_traits(t):
    keys = ["beta", "entry_z", "z1", "z2", "z4", "streak", "vrel", "acc", "clv", "stop_dist", "stop_pct"]
    parts = [f"{k}={round(t[k], 3) if isinstance(t[k], float) else t[k]}" for k in keys if k in t and t[k] is not None]
    return "; ".join(parts)

def main():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows, summary = [], {}

    for path in sorted(glob.glob("active_trades*.json")):
        try:
            data = json.load(open(path, "r"))
        except Exception:
            continue

        model = CORE_7.get(path, path.replace("active_trades_", "").replace("active_trades", "SMC-5M").replace(".json", "").upper() + " (CTRL)")
        act_list = data.get("active", data if isinstance(data, list) else [])
        cls_list = data.get("closed", []) if isinstance(data, dict) else []

        realized_r = 0.0
        unrealized_r = 0.0

        for t in cls_list:
            nr = float(t.get("net_r", 0.0))
            realized_r += nr
            rows.append({
                "Updated_UTC": now_utc,
                "Model": model,
                "Status": "CLOSED",
                "Symbol": t.get("inst") or t.get("sym", ""),
                "Side": fmt_side(t.get("side", "")),
                "Entry_Date": fmt_ts(t.get("entry_date") or t.get("entry_ts")),
                "Exit_Date": fmt_ts(t.get("exit_date") or t.get("exit_ts")),
                "Entry_Price": t.get("alt_entry") or t.get("entry", ""),
                "Exit_Or_SL": t.get("exit_price") or t.get("sl", ""),
                "Net_Or_Open_R": round(nr, 3),
                "Days_Held": t.get("days", ""),
                "Traits": extract_traits(t),
            })

        for t in act_list:
            sd = float(t.get("stop_dist") or t.get("stop_pct") or 0.0)
            cpnl = float(t.get("cum_pnl", 0.0))
            open_r = round(cpnl / sd, 3) if sd > 0 else 0.0
            unrealized_r += open_r
            rows.append({
                "Updated_UTC": now_utc,
                "Model": model,
                "Status": "ACTIVE",
                "Symbol": t.get("inst") or t.get("sym", ""),
                "Side": fmt_side(t.get("side", "")),
                "Entry_Date": fmt_ts(t.get("entry_date") or t.get("entry_ts")),
                "Exit_Date": "",
                "Entry_Price": t.get("alt_entry") or t.get("entry", ""),
                "Exit_Or_SL": t.get("sl") or f"StopDist:{round(sd*100, 2)}%",
                "Net_Or_Open_R": open_r,
                "Days_Held": t.get("days", 0),
                "Traits": extract_traits(t),
            })

        wins = sum(1 for x in cls_list if float(x.get("net_r", 0.0)) > 0)
        summary[model] = {
            "active_count": len(act_list),
            "closed_count": len(cls_list),
            "win_rate_pct": round(100.0 * wins / len(cls_list), 1) if cls_list else 0.0,
            "realized_r": round(realized_r, 2),
            "unrealized_r": round(unrealized_r, 2),
        }

    cols = ["Updated_UTC", "Model", "Status", "Symbol", "Side", "Entry_Date", "Exit_Date", "Entry_Price", "Exit_Or_SL", "Net_Or_Open_R", "Days_Held", "Traits"]
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    json.dump({"updated_utc": now_utc, "total_active": sum(v["active_count"] for v in summary.values()), "total_closed": sum(v["closed_count"] for v in summary.values()), "models": summary, "trades": rows}, open(JSON_FILE, "w"), indent=2)
    print(f"Master log updated: {len(rows)} total rows saved to {CSV_FILE} & {JSON_FILE}")

if __name__ == "__main__":
    main()
