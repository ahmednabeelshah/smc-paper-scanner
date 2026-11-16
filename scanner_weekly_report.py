import os
import glob
import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone

STATE_FILE = "state_weekly_report.json"

def send_tg(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(msg)
        return
    try:
        requests.post(
            f"https://telegram.org{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def get_r_column(df):
    for col in ["r", "net_r", "pnl_r", "R", "Net_R"]:
        if col in df.columns:
            return col
    return None

def get_simple_stats(r_series):
    rv = pd.to_numeric(r_series, errors="coerce").dropna().values
    if len(rv) == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    total_profit = float(np.sum(rv))
    win_rate = float(np.mean(rv > 0) * 100.0)
    running_total = np.cumsum(rv)
    biggest_dip = float(np.max(np.maximum.accumulate(running_total) - running_total))
    safety_score = total_profit / max(biggest_dip, 1e-6) if biggest_dip > 0 else (99.0 if total_profit > 0 else 0.0)
    return len(rv), total_profit, win_rate, biggest_dip, safety_score

def main():
    now = datetime.now(timezone.utc)
    week_id = now.strftime("%Y-W%U")
    st = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}

    # Run once every Sunday after 00:00 UTC (05:30 AM IST)
    if now.weekday() != 6 or st.get("last_week") == week_id:
        print(f"[SUNDAY-REPORT] Waiting for Sunday ({now.strftime('%a %H:%M UTC')})")
        return

    files = sorted(glob.glob("trades_*.csv"))
    if not files:
        st["last_week"] = week_id
        json.dump(st, open(STATE_FILE, "w"), indent=2)
        print("[SUNDAY-REPORT] No trade files yet.")
        return

    lines = [f"*SUNDAY PAPER-TRADE REPORT ({week_id})*\n"]
    all_trades = []

    for fpath in files:
        try:
            df = pd.read_csv(fpath)
            r_col = get_r_column(df)
            if r_col is None or len(df) == 0:
                continue
            bot_name = fpath.replace("trades_", "").replace(".csv", "").upper()
            trades, profit, win_pct, dip, safety = get_simple_stats(df[r_col])
            all_trades.extend(pd.to_numeric(df[r_col], errors="coerce").dropna().tolist())
            lines.append(
                f"• `{bot_name}`: *{profit:+.2f}R* | Wins `{win_pct:.0f}%` | Dip `{dip:.1f}R` | Safety `{safety:.2f}x` ({trades} trades)"
            )
        except Exception as e:
            lines.append(f"• `{fpath}`: could not read ({e})")

    if all_trades:
        t_cnt, t_prof, t_win, t_dip, t_safe = get_simple_stats(pd.Series(all_trades))
        lines.append("\n*ALL BOTS COMBINED*")
        lines.append(
            f"Total Profit: *{t_prof:+.2f}R* | Win Rate: `{t_win:.1f}%` | Biggest Dip: `{t_dip:.2f}R` | Safety Score: `{t_safe:.2f}x` ({t_cnt} trades)"
        )
    else:
        lines.append("No finished paper trades yet.")

    send_tg("\n".join(lines))
    st["last_week"] = week_id
    json.dump(st, open(STATE_FILE, "w"), indent=2)
    print(f"[SUNDAY-REPORT] Sent report for {week_id}.")

if __name__ == "__main__":
    main()
