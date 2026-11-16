"""
portfolio_guard.py — Live $58 Micro-Seed & Compounding Risk Guard
Implements #149 Safety Mode ($58-$65: 2 Slots, $8.25 Heat, 8D Exit)
and #143 Growth Mode (>$65: 3 Slots, 9.5% Compounding, 10D Exit).
"""
import os, json, glob
from datetime import datetime, timezone

SEED_CAPITAL_USD = 58.0
GROWTH_THRESHOLD_USD = 65.0
MAX_STOP_PCT = 0.16
MIN_STOP_PCT = 0.015
MAX_SAFE_LEVERAGE = 6.95
ROUND_TRIP_FEE = 0.0012

def get_guard_params(equity_usd: float = SEED_CAPITAL_USD) -> dict:
    eq = max(float(equity_usd), 10.0)
    if eq <= GROWTH_THRESHOLD_USD:
        return {
            "stage": "STAGE_1_MICRO_58",
            "max_slots": 2,
            "base_risk_usd": 4.12,
            "max_heat_usd": 8.25,
            "high_vote_mult": 1.35,
            "max_hold_days": 8,
            "mr_tp_mult": 0.80,
        }
    unit_risk = round(eq * 0.095, 2)
    return {
        "stage": "STAGE_2_COMPOUND_65PLUS",
        "max_slots": 3,
        "base_risk_usd": unit_risk,
        "max_heat_usd": round(unit_risk * 2.5, 2),
        "high_vote_mult": 1.25,
        "max_hold_days": 10,
        "mr_tp_mult": 0.80,
    }

def load_open_positions(state_dir: str = ".") -> list:
    open_trades = []
    first_idx = next(iter(range(1)))
    for path in sorted(glob.glob(os.path.join(state_dir, "*state*.json"))):
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
                   equity_usd: float = SEED_CAPITAL_USD, state_dir: str = ".") -> dict:
    params = get_guard_params(equity_usd)
    sp = max(float(stop_pct), MIN_STOP_PCT)
    if sp > MAX_STOP_PCT:
        return {"approved": False, "reason": f"SKIP_WIDE_STOP ({sp*100:.1f}% > 16.0%)", "stage": params["stage"]}
    open_trades = load_open_positions(state_dir)
    if any(t.get("symbol") == symbol for t in open_trades):
        return {"approved": False, "reason": f"SKIP_DUP ({symbol})", "stage": params["stage"]}
    if len(open_trades) >= params["max_slots"]:
        return {"approved": False, "reason": f"SKIP_SLOTS ({len(open_trades)}/{params['max_slots']})", "stage": params["stage"]}
    used_heat = sum(float(t.get("risk_usd", params["base_risk_usd"])) for t in open_trades)
    rem_heat = round(params["max_heat_usd"] - used_heat, 2)
    if rem_heat < 2.00:
        return {"approved": False, "reason": f"SKIP_HEAT (${used_heat:.2f}/${params['max_heat_usd']:.2f})", "stage": params["stage"]}
    mult = params["high_vote_mult"] if int(abs_votes) >= 2 else 1.0
    risk_usd = min(round(params["base_risk_usd"] * mult, 2), rem_heat)
    notional_usd = round(risk_usd / sp, 2)
    eff_lev = round(notional_usd / max(float(equity_usd), 1.0), 2)
    if eff_lev > MAX_SAFE_LEVERAGE:
        notional_usd = round(float(equity_usd) * MAX_SAFE_LEVERAGE, 2)
        risk_usd = round(notional_usd * sp, 2)
        eff_lev = MAX_SAFE_LEVERAGE
    return {
        "approved": True,
        "reason": "APPROVED",
        "stage": params["stage"],
        "risk_usd": risk_usd,
        "notional_usd": notional_usd,
        "eff_leverage": eff_lev,
        "slots_after": f"{len(open_trades) + 1}/{params['max_slots']}",
        "heat_after_usd": round(used_heat + risk_usd, 2),
        "max_hold_days": params["max_hold_days"],
    }

def check_stale_exit(entry_iso: str, equity_usd: float = SEED_CAPITAL_USD) -> tuple:
    params = get_guard_params(equity_usd)
    max_days = params["max_hold_days"]
    try:
        dt_in = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
        if dt_in.tzinfo is None:
            dt_in = dt_in.replace(tzinfo=timezone.utc)
        days_held = (datetime.now(timezone.utc) - dt_in).total_seconds() / 86400.0
        return (days_held >= max_days, round(days_held, 2), max_days)
    except Exception:
        return (False, 0.0, max_days)
