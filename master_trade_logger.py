#!/usr/bin/env python3
"""
Master Trade Logger for Paper-Trading Scanners (NO LIVE ORDERS)
- Safely reads active_trades_*.json, closed_trades_*.csv, and existing master_trade_log.json
- Skips non-dict entries and string keys/items so AttributeError ('str' has no attribute 'get') never occurs
- Writes normalized summary rows to master_trade_log.csv and master_trade_log.json
"""
import csv
import glob
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


MASTER_CSV = "master_trade_log.csv"
MASTER_JSON = "master_trade_log.json"


def _extract_dict_records(obj: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                records.append(item)
    elif isinstance(obj, dict):
        # Handle nested state dicts such as {"last_processed_1h": "...", "active": {"BO1H:DOTUSDT": {...}}}
        for key in ("active", "open_positions", "positions", "trades", "signals", "records", "data"):
            sub = obj.get(key)
            if isinstance(sub, dict):
                for sub_key, val in sub.items():
                    if isinstance(val, dict):
                        row = dict(val)
                        if "key" not in row and isinstance(sub_key, str):
                            row["slot_key"] = sub_key
                        records.append(row)
            elif isinstance(sub, list):
                for item in sub:
                    if isinstance(item, dict):
                        records.append(item)
    return records


def load_active_states() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob("active_trades_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in _extract_dict_records(data):
                item["_source_file"] = path
                item["_status"] = item.get("_status") or "OPEN"
                rows.append(item)
        except Exception as exc:
            print(f"[WARN] Could not parse {path}: {exc}")
    return rows


def load_closed_csvs() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob("closed_trades_*.csv")):
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if isinstance(r, dict):
                        item = dict(r)
                        item["_source_file"] = path
                        item["_status"] = item.get("_status") or "CLOSED"
                        rows.append(item)
        except Exception as exc:
            print(f"[WARN] Could not parse {path}: {exc}")
    return rows


def load_existing_master_json() -> List[Dict[str, Any]]:
    if not os.path.exists(MASTER_JSON):
        return []
    try:
        with open(MASTER_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _extract_dict_records(data)
    except Exception:
        return []


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f if f == f and abs(f) != float("inf") else default
    except Exception:
        return default


def normalize_row(t: Dict[str, Any]) -> Dict[str, Any]:
    model = str(t.get("model") or t.get("sleeve") or t.get("strategy") or t.get("_source_file") or "UNKNOWN")
    symbol = str(t.get("symbol") or t.get("sym") or "").upper()
    side_raw = t.get("side")
    if not side_raw and "dir" in t:
        d = _safe_float(t.get("dir"), 0.0)
        side_raw = "LONG" if d > 0 else ("SHORT" if d < 0 else "")
    side = str(side_raw or "").upper()

    stop_dist = _safe_float(
        t.get("stop_dist") if t.get("stop_dist") is not None else t.get("stop_pct"),
        0.0,
    )
    entry_px = _safe_float(t.get("entry") if t.get("entry") is not None else t.get("ref_entry_price"), 0.0)
    sl_px = _safe_float(t.get("sl") if t.get("sl") is not None else t.get("stop_loss_price"), 0.0)
    tp_px = _safe_float(t.get("tp") if t.get("tp") is not None else t.get("tp2_target_price"), 0.0)
    net_r = _safe_float(t.get("net_r"), 0.0)
    status = str(t.get("_status") or t.get("status") or "OPEN")
    ts = str(
        t.get("exit_time")
        or t.get("opened_at")
        or t.get("closed_4h_bar_utc")
        or t.get("timestamp_scan_utc")
        or datetime.now(timezone.utc).isoformat()
    )

    return {
        "timestamp_utc": ts,
        "model": model,
        "symbol": symbol,
        "side": side,
        "status": status,
        "entry": round(entry_px, 6),
        "sl": round(sl_px, 6),
        "tp": round(tp_px, 6),
        "stop_dist": round(stop_dist, 4),
        "net_r": round(net_r, 4),
        "source_file": str(t.get("_source_file") or ""),
    }


def dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    return (
        row["model"],
        row["symbol"],
        row["side"],
        row["status"],
        row["timestamp_utc"],
    )


def main() -> None:
    combined_raw: List[Dict[str, Any]] = []
    combined_raw.extend(load_existing_master_json())
    combined_raw.extend(load_active_states())
    combined_raw.extend(load_closed_csvs())

    deduped: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for raw_item in combined_raw:
        if not isinstance(raw_item, dict):
            continue
        norm = normalize_row(raw_item)
        if not norm["symbol"] and norm["model"] == "UNKNOWN":
            continue
        deduped[dedupe_key(norm)] = norm

    final_rows = list(deduped.values())

    with open(MASTER_JSON, "w", encoding="utf-8") as f:
        json.dump(final_rows, f, indent=2)

    fieldnames = [
        "timestamp_utc",
        "model",
        "symbol",
        "side",
        "status",
        "entry",
        "sl",
        "tp",
        "stop_dist",
        "net_r",
        "source_file",
    ]
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(final_rows)

    unique_models = len({r["model"] for r in final_rows if r["model"]})
    print(f"Master log updated: {len(final_rows)} rows across {unique_models} models.")


if __name__ == "__main__":
    main()
    
