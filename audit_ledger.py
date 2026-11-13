import json
from pathlib import Path

LEDGER_FILES = {
    "v5.1 Baseline": "active_trades.json",
    "v5.4 Quant Champion": "active_trades_v54.json",
    "TSMOM Momentum": "active_trades_tsmom.json"
}

def analyze_ledger():
    print("=" * 70)
    print("SMC PAPER-TRADING AUDIT LEDGER & PERFORMANCE SCORECARD")
    print("=" * 70)

    for name, filename in LEDGER_FILES.items():
        path = Path(filename)
        print(f"\n--- Strategy Cohort: {name} ({filename}) ---")
        if not path.exists():
            print(f"Status: File not found ({filename}). No active/logged state.")
            continue

        try:
            with path.open("r") as f:
                data = json.load(f)
            
            trades = data if isinstance(data, list) else data.get("trades", data.get("active_trades", []))
            
            if not trades:
                print("Status: File exists, but 0 trades currently recorded.")
                continue

            print(f"Total Recorded Items/Trades: {len(trades)}")
            
            for idx, trade in enumerate(trades[:5], 1):
                sym = trade.get("symbol", trade.get("instId", "UNKNOWN"))
                side = trade.get("side", trade.get("direction", "SHORT"))
                entry = trade.get("entry", trade.get("entry_price", 0.0))
                print(f"  [{idx}] {sym} | {side} | Entry: {entry}")

        except Exception as e:
            print(f"Error reading {filename}: {e}")

    print("\n" + "=" * 70)

if __name__ == "__main__":
    analyze_ledger()
