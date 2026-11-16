"""
portfolio_guard.py — Unfiltered Data-Centric R&D Logger
Approves 100% of signals across all models (no slot caps, no dollar limits, no cooldown blocks)
so maximum trade data (thousands of entries) is collected for statistical evaluation.
"""
import os, json, glob
from datetime import datetime, timezone

DATA_COLLECTION_MODE = True
MAX_STOP_PCT = 0.25
MIN_STOP_PCT = 0.005

def get_guard_params(equity_usd: float = 100.0) -> dict:
    return {
        "stage": "UNFILTERED_RND_DATA_MODE",
        "max_slots": 999,
        "base_risk_usd": 1.0,
        "max_heat_usd": 999999.0,
        "high_vote_mult": 1.0,
        "max_hold_days": 8,
        "mr_tp_mult": 0.80,
        "max_same_dir": 999,
    }

def load_open_positions(state_dir: str = ".") -> list:
    open_trades = []
    first_idx = next(iter(range(1)))
    for path in sorted(glob.glob(os.path.join(state_dir, "*state*.json"))):
        if os.path.basename(path) == "guard_state.json":
            continue
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                continue
            bot_tag = os.path.basename(path).split("_state")[first_idx]
            for sym, pos in data.items():
                if isinstance(pos, dict) and pos.get("status") == "OPEN":
                    item = dict(pos)
                    item["symbol"] = sym
                    item["bot"] = bot_tag
                    open_trades.append(item)
        except Exception:
            continue
    return open_trades

def evaluate_entry(symbol: str, side: str, entry_price: float, stop_pct: float,
                   abs_votes: int = 1, setup_type: str = "MR",
                   equity_usd: float = 100.0, state_dir: str = ".") -> dict:
    sp = min(max(float(stop_pct), MIN_STOP_PCT), MAX_STOP_PCT)
    open_trades = load_open_positions(state_dir)
    return {
        "approved": True,
        "reason": "APPROVED_RND_DATA_MODE",
        "stage": "UNFILTERED_RND_DATA_MODE",
        "risk_usd": 1.0,
        "notional_usd": round(1.0 / sp, 2),
        "eff_leverage": 1.0,
        "slots_after": f"{len(open_trades) + 1}/999",
        "heat_after_usd": float(len(open_trades) + 1),
        "max_hold_days": 8,
    }

def check_stale_exit(entry_iso: str, equity_usd: float = 100.0) -> tuple:
    max_days = 8
    try:
        dt_in = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
        if dt_in.tzinfo is None:
            dt_in = dt_in.replace(tzinfo=timezone.utc)
        days_held = (datetime.now(timezone.utc) - dt_in).total_seconds() / 86400.0
        return (days_held >= max_days, round(days_held, 2), max_days)
    except Exception:
        return (False, 0.0, max_days)
        
