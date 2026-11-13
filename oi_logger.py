import csv, time, requests
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.okx.com"
LOG_FILE = Path("oi_funding_log.csv")

def get_json(path, params=None):
    for a in range(3):
        try:
            r = requests.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception:
            if a == 2:
                raise
            time.sleep(2 ** a)

def run_logger():
    srv_ms = int(get_json("/api/v5/public/time")["data"][0]["ts"])
    dt_utc = datetime.fromtimestamp(srv_ms / 1000, timezone.utc)
    hour_key = dt_utc.strftime("%Y-%m-%d %H:00:00Z")

    # Log once per closed hour (prevents duplicate rows on 5-minute cron ticks)
    if LOG_FILE.exists():
        with LOG_FILE.open("r", newline="") as f:
            rows = list(csv.reader(f))
            if len(rows) > 1 and rows[-1][0] == hour_key:
                print(f"[{hour_key}] OI/Funding already logged for this hour.")
                return

    tickers = get_json("/api/v5/market/tickers", {"instType": "SWAP"}).get("data", [])
    usdt_swaps = [
        t for t in tickers
        if t["instId"].endswith("-USDT-SWAP")
        and float(t.get("volCcy24h", 0)) * float(t.get("last", 0)) >= 15_000_000
    ]
    top30 = sorted(
        usdt_swaps,
        key=lambda x: float(x.get("volCcy24h", 0)) * float(x.get("last", 0)),
        reverse=True
    )[:30]

    oi_list = get_json("/api/v5/public/open-interest", {"instType": "SWAP"}).get("data", [])
    oi_map = {x["instId"]: float(x.get("oiCcy", 0)) for x in oi_list}

    file_exists = LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp_utc", "symbol", "last_price", "oi_usd", "funding_rate", "spread_pct"])

        logged = 0
        for t in top30:
            inst = t["instId"]
            sym = inst.replace("-USDT-SWAP", "USDT")
            last_px = float(t.get("last", 0))
            bid, ask = float(t.get("bidPx", 0)), float(t.get("askPx", 0))
            if last_px <= 0 or bid <= 0:
                continue
            spread_pct = (ask - bid) / bid * 100.0
            oi_usd = oi_map.get(inst, 0.0) * last_px

            f_data = get_json("/api/v5/public/funding-rate", {"instId": inst}).get("data", [])
            fund = float(f_data[0]["fundingRate"]) if f_data else 0.0

            writer.writerow([hour_key, sym, f"{last_px:.6g}", f"{oi_usd:.2f}", f"{fund:.6f}", f"{spread_pct:.4f}"])
            logged += 1

    print(f"[{hour_key}] Logged live OI, Funding & Spread for {logged} pairs -> {LOG_FILE}")

if __name__ == "__main__":
    run_logger()
