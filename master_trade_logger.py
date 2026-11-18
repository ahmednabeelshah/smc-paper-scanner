import os, glob, json, csv
from datetime import datetime, timezone

CSV_FILE = "master_trade_log.csv"
JSON_FILE = "master_trade_log.json"

CORE_4 = {
    "active_trades_calm_res.json": "CALM-RES 1D (CORE)",
    "active_trades_tsmom.json": "TSMOM 1D UPG (CORE)",
    "active_trades_4h_res_414.json": "4H-RES-414 SNIPER (CORE)",
    "active_trades_hybrid_609.json": "HYBRID-609 1H (CORE)",
}

BASE_KEYS = {
    "inst", "sym", "side", "entry_date", "entry_ts", "exit_date", "exit_ts",
    "last_checked_ts", "last_ts", "alt_entry", "entry", "exit_price", "sl",
    "risk", "best", "b_entry", "btc_entry", "net_r", "cum_pnl", "days",
    "bars", "hours", "stop_pct", "stop_dist",
}


def fmt_side(s):
    if s in (1, "1", "LONG", "long"):
        return "LONG"
    if s in (-1, "-1", "SHORT", "short"):
        return "SHORT"
    return str(s)


def fmt_ts(val, intraday=False):
    if not val:
        return ""
    if isinstance(val, (int, float)) and val > 1e11:
        fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
        return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc).strftime(fmt)
    return str(val)


def fmt_duration(t):
    if "days" in t and t["days"] != "":
        return f"{t['days']}d"
    if "bars" in t and t["bars"] != "":
        return f"{t['bars'] * 4}h"
    if "hours" in t and t["hours"] != "":
        return f"{t['hours']}h"
    return ""


def extract_traits(t):
    parts = []
    for k, v in sorted(t.items()):
        if k not in BASE_KEYS and v is not None:
            parts.append(f"{k}={round(v, 3) if isinstance(v, float) else v}")
    return "; ".join(parts)


def main():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows, summary = [], {}

    for path in sorted(glob.glob("active_trades*.json")):
        fname = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception:
            continue

        default_name = fname.replace("active_trades_", "").replace("active_trades", "LEGACY").replace(".json", "").upper() + " (AUTO)"
        model = CORE_4.get(fname, default_name)
        is_intraday = any(tag in fname for tag in ("4h", "1h", "609"))

        act_list = data.get("active", data if isinstance(data, list) else [])
        cls_list = data.get("closed", []) if isinstance(data, dict) else []

        realized_r, unrealized_r = 0.0, 0.0

        for t in cls_list:
            nr = float(t.get("net_r", 0.0))
            realized_r += nr
            rows.append({
                "Updated_UTC": now_utc,
                "Model": model,
                "Status": "CLOSED",
                "Symbol": t.get("inst") or t.get("sym", ""),
                "Side": fmt_side(t.get("side", "")),
                "Entry_Date": fmt_ts(t.get("entry_date") or t.get("entry_ts"), intraday=is_intraday),
                "Exit_Date": fmt_ts(t.get("exit_date") or t.get("exit_ts"), intraday=is_intraday),
                "Entry_Price": t.get("alt_entry") or t.get("entry", ""),
                "Exit_Or_SL": t.get("exit_price") or t.get("sl", ""),
                "Net_Or_Open_R": round(nr, 3),
                "Days_Held": fmt_duration(t),
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
                "Entry_Date": fmt_ts(t.get("entry_date") or t.get("entry_ts"), intraday=is_intraday),
                "Exit_Date": "",
                "Entry_Price": t.get("alt_entry") or t.get("entry", ""),
                "Exit_Or_SL": t.get("sl") or f"StopDist:{round(sd * 100, 2)}%",
                "Net_Or_Open_R": open_r,
                "Days_Held": fmt_duration(t),
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

    cols = [
        "Updated_UTC", "Model", "Status", "Symbol", "Side",
        "Entry_Date", "Exit_Date", "Entry_Price", "Exit_Or_SL",
        "Net_Or_Open_R", "Days_Held", "Traits",
    ]
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_utc": now_utc,
            "total_active": sum(v["active_count"] for v in summary.values()),
            "total_closed": sum(v["closed_count"] for v in summary.values()),
            "models": summary,
            "trades": rows,
        }, f, indent=2)

    print(f"Master log updated: {len(rows)} rows across {len(summary)} models.")


if __name__ == "__main__":
    main()
    
