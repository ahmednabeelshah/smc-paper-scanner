import json
from pathlib import Path

LEDGER_FILES = {
    "v5.1 Baseline": "active_trades.json",
    "[v5.4] Quant Sniper": "active_trades_v54.json",
    "[TSMOM] 5-Yr Swing": "active_trades_tsmom.json",
    "[RESIDUAL 1D] Champion": "active_trades_residual.json"
}

def compute_closed_metrics(closed_list):
    if not closed_list:
        return None
    r_vals = []
    for t in closed_list:
        if "net_r" in t:
            r_vals.append(float(t["net_r"]))
        elif "pnl_pct" in t and "risk_pct" in t and float(t["risk_pct"]) > 0:
            r_vals.append(float(t["pnl_pct"]) / float(t["risk_pct"]))
        elif "pnl" in t:
            r_vals.append(float(t["pnl"]))
    if not r_vals:
        return {"count": len(closed_list), "has_r": False}

    wins = [r for r in r_vals if r > 0]
    losses = [r for r in r_vals if r <= 0]
    wr = (len(wins) / len(r_vals)) * 100.0
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    net_r = sum(r_vals)

    eq, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_vals:
        eq += r
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)

    return {
        "count": len(r_vals),
        "has_r": True,
        "wr": wr,
        "pf": pf,
        "net_r": net_r,
        "max_dd": max_dd
    }

def analyze_ledger():
    print("=" * 78)
    print("SMC & RESIDUAL QUANT PAPER-TRADING AUDIT LEDGER")
    print("=" * 78)

    for name, filename in LEDGER_FILES.items():
        path = Path(filename)
        print(f"\n--- {name} ({filename}) ---")
        if not path.exists():
            print("Status: File not found yet.")
            continue

        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                active, closed = data, []
            else:
                active = data.get("active", data.get("active_trades", data.get("trades", [])))
                closed = data.get("closed", data.get("closed_trades", data.get("history", [])))

            print(f"Active Slots : {len(active)}/3")
            for idx, tr in enumerate(active, 1):
                sym = tr.get("sym", tr.get("symbol", tr.get("instId", "UNKNOWN")))
                if "beta" in tr:
                    side_lbl = f"LONG {sym} / SHORT BTC ({tr['beta']}x)" if tr.get("side") == 1 else f"SHORT {sym} / LONG BTC ({tr['beta']}x)"
                    z_ent = tr.get("entry_z", "N/A")
                    days = tr.get("days", 0)
                    print(f"  [{idx}] {side_lbl} | Entry Z: {z_ent} | Hold: {days}/5d")
                else:
                    side = tr.get("side", tr.get("direction", "SHORT"))
                    entry = tr.get("entry", tr.get("entry_price", 0.0))
                    print(f"  [{idx}] {sym} {side} | Entry: {entry}")

            stats = compute_closed_metrics(closed)
            if not stats:
                print("Closed Trades: 0 recorded yet.")
            elif not stats["has_r"]:
                print(f"Closed Trades: {stats['count']} recorded (R-multiples not mapped).")
            else:
                pf_str = f"{stats['pf']:.3f}" if stats["pf"] != float("inf") else "INF"
                print(
                    f"Closed Trades: {stats['count']} | WR: {stats['wr']:.1f}% | "
                    f"PF: {pf_str} | Net R: {stats['net_r']:+.2f}R | MaxDD: {stats['max_dd']:.2f}R"
                )

        except Exception as e:
            print(f"Read Error ({filename}): {e}")

    print("\n" + "=" * 78)

if __name__ == "__main__":
    analyze_ledger()
    
