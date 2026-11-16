"""
portfolio_guard.py — Live $58 Micro-Seed & Compounding Risk Guard
Implements #149 Safety Mode ($58+: 2 Slots, $8.25 Heat, 8D Exit).
Stage 2 (#143) is hard-blocked (stage2_enabled=False) until OKX perp Audit #3 passes.
"""
import os, json, glob
from datetime import datetime, timezone

SEED_CAPITAL_USD = 58.0
GROWTH_THRESHOLD_USD = 65.0
MAX_STOP_PCT = 0.16
MIN_STOP_PCT = 0.015
MAX_SAFE_LEVERAGE = 6.95
ROUND_TRIP_FEE = 0.0012
EXTRA_SLIPPAGE = 0.0005
EXPECTED_RUN_RATE = 5.40  # Conservative 300D R run-rate (#149 solo)

STAGE = {
    "current_stage": 1,
    "stage2_enabled": False,
    "stage2_enable_gate": "audit3_okx_perp_reverify_passed",
}

KILL_SWITCH = {
    "rolling_5d_net_R_threshold": -6.0,
    "peak_to_trough_DD_R_threshold": 8.0,
    "auto_resume": False,
    "force_close_positions": False,
    "status": "operational_backstop, first 30 days live is real calibration",
}

def get_guard_params(equity_usd: float = SEED_CAPITAL_USD) -> dict:
    eq = max(float(equity_usd), 10.0)
    if eq > GROWTH_THRESHOLD_USD and not STAGE["stage2_enabled"]:
        print(f"STAGE2_BLOCKED_EQUITY_CROSSED | equity=${eq:.2f} > ${GROWTH_THRESHOLD_USD:.2f} | holding Stage 1 (#149)")
    if STAGE["stage2_enabled"] and eq > GROWTH_THRESHOLD_USD:
        unit_risk = round(eq * 0.095, 2)
        return {
            "stage": "STAGE_2_COMPOUND_65PLUS",
            "max_slots": 3,
            "base_risk_usd": unit_risk,
            "max_heat_usd": round(unit_risk * 2.5, 2),
            "high_vote_mult": 1.25,
            "max_hold_days": 10,
            "mr_tp_mult": 0.80,
            "max_same_dir": 2,
        }
    return {
        "stage": "STAGE_1_MICRO_58",
        "max_slots": 2,
        "base_risk_usd": 4.12,
        "max_heat_usd": 8.25,
        "high_vote_mult": 1.35,
        "max_hold_days": 8,
        "mr_tp_mult": 0.80,
        "max_same_dir": 2,
    }

def _scan_state_files(state_dir: str = ".") -> tuple:
    open_trades, closed_trades = [], []
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
                if not isinstance(pos, dict):
                    continue
                item = dict(pos)
                item["symbol"] = sym
                item["bot"] = bot_tag
                if pos.get("status") == "OPEN":
                    open_trades.append(item)
                elif pos.get("status") == "CLOSED":
                    closed_trades.append(item)
        except Exception:
            continue
    return open_trades, closed_trades

def load_open_positions(state_dir: str = ".") -> list:
    open_trades, _ = _scan_state_files(state_dir)
    return open_trades

def check_kill_switch(state_dir: str = ".") -> tuple:
    guard_path = os.path.join(state_dir, "guard_state.json")
    gstate = {}
    if os.path.exists(guard_path):
        try:
            with open(guard_path, "r", encoding="utf-8") as fp:
                gstate = json.load(fp)
        except Exception:
            gstate = {}
    if gstate.get("paused", False) and not gstate.get("resumed_by"):
        return True, f"KILL_SWITCH_PAUSED ({gstate.get('trigger', 'MANUAL_LOCK')})"

    _, closed_trades = _scan_state_files(state_dir)
    now_dt = datetime.now(timezone.utc)
    dated_r = []
    roll_5d_r = 0.0
    for tr in closed_trades:
        r_val = float(tr.get("net_r", tr.get("pnl_r", 0.0)))
        ex_iso = tr.get("exit_time") or tr.get("closed_at") or tr.get("updated_at")
        if ex_iso:
            try:
                dt_ex = datetime.fromisoformat(str(ex_iso).replace("Z", "+00:00"))
                if dt_ex.tzinfo is None:
                    dt_ex = dt_ex.replace(tzinfo=timezone.utc)
                dated_r.append((dt_ex, r_val))
                if (now_dt - dt_ex).total_seconds() <= 5 * 86400.0:
                    roll_5d_r += r_val
            except Exception:
                dated_r.append((now_dt, r_val))

    dated_r.sort(key=lambda x: x[next(iter(range(1)))])
    cum_r, peak_r, max_dd_r = 0.0, 0.0, 0.0
    for _, rv in dated_r:
        cum_r += rv
        peak_r = max(peak_r, cum_r)
        max_dd_r = max(max_dd_r, peak_r - cum_r)

    p1 = roll_5d_r <= KILL_SWITCH["rolling_5d_net_R_threshold"]
    p2 = max_dd_r >= KILL_SWITCH["peak_to_trough_DD_R_threshold"]
    if p1 or p2:
        trig = f"P1_5D_NET_R={roll_5d_r:.2f}R" if p1 else f"P2_MAX_DD_R={max_dd_r:.2f}R"
        gstate.update({
            "paused": True,
            "triggered_at": now_dt.isoformat(),
            "trigger": trig,
            "expected_run_rate_300d": EXPECTED_RUN_RATE,
            "resumed_by": None,
        })
        try:
            with open(guard_path, "w", encoding="utf-8") as fp:
                json.dump(gstate, fp, indent=2)
        except Exception:
            pass
        print(f"KILL_SWITCH_TRIGGERED | {trig} | auto_resume=False | force_close=False")
        return True, f"KILL_SWITCH_TRIGGERED ({trig})"
    return False, "OK"

def _in_24h_lockout(symbol: str, closed_trades: list) -> bool:
    now_dt = datetime.now(timezone.utc)
    for tr in closed_trades:
        if tr.get("symbol") != symbol:
            continue
        ex_iso = tr.get("exit_time") or tr.get("closed_at") or tr.get("updated_at")
        if not ex_iso:
            continue
        try:
            dt_ex = datetime.fromisoformat(str(ex_iso).replace("Z", "+00:00"))
            if dt_ex.tzinfo is None:
                dt_ex = dt_ex.replace(tzinfo=timezone.utc)
            if (now_dt - dt_ex).total_seconds() < 86400.0:
                return True
        except Exception:
            continue
    return False

def evaluate_entry(symbol: str, side: str, entry_price: float, stop_pct: float,
                   abs_votes: int = 1, setup_type: str = "MR",
                   equity_usd: float = SEED_CAPITAL_USD, state_dir: str = ".") -> dict:
    params = get_guard_params(equity_usd)
    paused, ks_reason = check_kill_switch(state_dir)
    if paused:
        return {"approved": False, "reason": ks_reason, "stage": params["stage"]}

    sp = max(float(stop_pct), MIN_STOP_PCT)
    if sp > MAX_STOP_PCT:
        return {"approved": False, "reason": f"SKIP_WIDE_STOP ({sp*100:.1f}% > 16.0%)", "stage": params["stage"]}

    open_trades, closed_trades = _scan_state_files(state_dir)
    if any(t.get("symbol") == symbol for t in open_trades):
        print(f"SLOT_COLLISION_DROPPED | symbol={symbol} | reason=ALREADY_OPEN")
        return {"approved": False, "reason": f"SLOT_COLLISION_DROPPED ({symbol})", "stage": params["stage"]}

    if _in_24h_lockout(symbol, closed_trades):
        return {"approved": False, "reason": f"SKIP_24H_LOCKOUT ({symbol})", "stage": params["stage"]}

    if len(open_trades) >= min(params["max_slots"], 3):
        return {"approved": False, "reason": f"SKIP_SLOTS ({len(open_trades)}/{params['max_slots']})", "stage": params["stage"]}

    norm_side = str(side).upper()
    same_dir_cnt = sum(1 for t in open_trades if str(t.get("side", "")).upper() == norm_side)
    if same_dir_cnt >= params["max_same_dir"]:
        return {"approved": False, "reason": f"SKIP_SAME_DIR ({norm_side}={same_dir_cnt}/2)", "stage": params["stage"]}

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
        
