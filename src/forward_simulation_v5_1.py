#!/usr/bin/env python3
"""Forward-only weather market simulation v5.1.

v5.1 is a blocking-fix release. It keeps v5 files intact and writes only to
data/forward_v5_1/{formal,demo}. It never connects to a wallet and never
submits real orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import signal as signal_module
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VERSION = "forward_simulation_v5.1.0"
SCHEMA_VERSION = "forward_v5_1_schema_001"
FORMAL = "formal"
DEMO = "demo"
ORDERBOOK_URL = "https://clob.polymarket.com/book"
ORDERBOOK_DOCS_URL = "https://docs.polymarket.com/api-reference/market-data/get-order-book"
EPS = 1e-9

STRATEGIES = {
    "hold_to_settlement": {"multiple": None, "fraction": 0.0, "stage": "hold"},
    "tp_2x_sell_50pct": {"multiple": 2.0, "fraction": 0.50, "stage": "tp_2x_once"},
    "tp_2x_sell_75pct": {"multiple": 2.0, "fraction": 0.75, "stage": "tp_2x_once"},
    "tp_5x_sell_25pct": {"multiple": 5.0, "fraction": 0.25, "stage": "tp_5x_once"},
}
STRATEGY_IDS = list(STRATEGIES)

SIGNAL_FIELDS = [
    "signal_id",
    "created_at_utc",
    "registered_at_utc",
    "city",
    "city_normalized",
    "weather_date_local",
    "weather_metric",
    "event_key",
    "market_slug",
    "condition_id",
    "token_id",
    "outcome",
    "side",
    "forecast_temperature",
    "forecast_probability",
    "market_probability_at_signal",
    "intended_usd",
    "max_entry_price",
    "entry_deadline_utc",
    "source",
    "notes",
    "mode",
]
ENTRY_STATE_FIELDS = [
    "entry_state_id",
    "signal_id",
    "token_id",
    "updated_at_utc",
    "intended_usd",
    "filled_entry_usd",
    "remaining_entry_usd",
    "filled_entry_shares",
    "entry_status",
    "max_entry_price",
    "entry_deadline_utc",
    "last_entry_attempt_at",
    "last_attempt_reason",
    "mode",
]
ENTRY_FILL_FIELDS = [
    "entry_fill_id",
    "signal_id",
    "event_key",
    "token_id",
    "snapshot_id",
    "filled_at_utc",
    "gross_entry_cost",
    "entry_fee",
    "total_entry_cost",
    "filled_shares",
    "entry_vwap",
    "best_bid",
    "best_ask",
    "spread",
    "complete_fill",
    "unfilled_usd_after_fill",
    "depth_levels_json",
    "mode",
]
LOT_FIELDS = [
    "lot_id",
    "strategy_id",
    "signal_id",
    "event_key",
    "token_id",
    "entry_fill_id",
    "created_at_utc",
    "entry_shares",
    "gross_entry_cost",
    "entry_fee",
    "mode",
]
TRIGGER_FIELDS = [
    "trigger_id",
    "signal_id",
    "strategy_id",
    "trigger_stage_id",
    "event_key",
    "token_id",
    "trigger_created_at",
    "trigger_target_shares",
    "trigger_filled_shares",
    "trigger_remaining_shares",
    "trigger_status",
    "trigger_completed_at",
    "rolling_avg_cost_at_trigger",
    "threshold_price",
    "mode",
]
EXIT_FILL_FIELDS = [
    "exit_fill_id",
    "trigger_id",
    "signal_id",
    "strategy_id",
    "trigger_stage_id",
    "event_key",
    "token_id",
    "snapshot_id",
    "filled_at_utc",
    "planned_sell_shares",
    "filled_shares",
    "gross_exit_proceeds",
    "exit_fee",
    "net_exit_proceeds",
    "exit_vwap",
    "best_bid",
    "best_ask",
    "spread",
    "complete_fill",
    "unfilled_trigger_shares_after_fill",
    "depth_levels_json",
    "mode",
]
EXIT_ALLOC_FIELDS = [
    "allocation_id",
    "exit_fill_id",
    "trigger_id",
    "strategy_id",
    "signal_id",
    "event_key",
    "token_id",
    "lot_id",
    "allocated_shares",
    "gross_exit_proceeds",
    "exit_fee",
    "mode",
]
SETTLEMENT_FIELDS = [
    "settlement_id",
    "signal_id",
    "strategy_id",
    "event_key",
    "condition_id",
    "token_id",
    "settlement_outcome",
    "settlement_value",
    "source",
    "source_reference",
    "observed_at_utc",
    "recorded_at_utc",
    "operator_notes",
    "evidence_hash",
    "settlement_status",
    "remaining_shares_settled",
    "settlement_proceeds",
    "settlement_fee",
    "mode",
]
SETTLEMENT_ALLOC_FIELDS = [
    "settlement_allocation_id",
    "settlement_id",
    "strategy_id",
    "signal_id",
    "event_key",
    "token_id",
    "lot_id",
    "settled_shares",
    "settlement_proceeds",
    "settlement_fee",
    "mode",
]
EVENT_RESULT_FIELDS = [
    "event_key",
    "strategy_id",
    "mode",
    "signal_count",
    "position_count",
    "traded_event_count",
    "settled_event_count",
    "gross_entry_cost",
    "entry_fee",
    "gross_exit_proceeds",
    "exit_fee",
    "settlement_proceeds",
    "settlement_fee",
    "total_fees",
    "gross_pnl",
    "net_pnl",
    "triggered_take_profit",
    "incomplete_take_profit",
]

CSV_SCHEMAS = {
    "signals.csv": SIGNAL_FIELDS,
    "entry_order_state.csv": ENTRY_STATE_FIELDS,
    "entry_fills.csv": ENTRY_FILL_FIELDS,
    "strategy_lots.csv": LOT_FIELDS,
    "strategy_triggers.csv": TRIGGER_FIELDS,
    "exit_fills.csv": EXIT_FILL_FIELDS,
    "exit_fill_allocations.csv": EXIT_ALLOC_FIELDS,
    "settlements.csv": SETTLEMENT_FIELDS,
    "settlement_allocations.csv": SETTLEMENT_ALLOC_FIELDS,
    "event_results.csv": EVENT_RESULT_FIELDS,
}
JSONL_FILES = ["orderbook_snapshots.jsonl", "audit_log.jsonl", "heartbeat.jsonl", "errors.jsonl"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_utc() -> str:
    return utcnow().isoformat()


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_city(city: str) -> str:
    return " ".join(city.strip().lower().split())


def normalize_metric(metric: str) -> str:
    raw = " ".join((metric or "").strip().lower().split())
    aliases = {
        "high": "high",
        "highest": "high",
        "max": "high",
        "highest temperature": "high",
        "low": "low",
        "lowest": "low",
        "min": "low",
        "lowest temperature": "low",
    }
    return aliases.get(raw, raw)


def make_event_key(city: str, weather_date_local: str, weather_metric: str) -> str:
    return "|".join([normalize_city(city), weather_date_local.strip(), normalize_metric(weather_metric)])


def data_dir(root: Path, mode: str) -> Path:
    return root / "data/forward_v5_1" / mode


def ensure_ledger(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, fields in CSV_SCHEMAS.items():
        p = path / name
        if not p.exists():
            with p.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fields).writeheader()
    for name in JSONL_FILES:
        p = path / name
        if not p.exists():
            p.touch()
    state = path / "system_state.json"
    if not state.exists():
        write_state(path, {"schema_version": SCHEMA_VERSION, "mode": path.name, "formal_started_at_utc": None, "paused": False, "stopped": False})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def atomic_append_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    line_count_before = existing.count("\n")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        if existing:
            f.write(existing)
            if not existing.endswith("\n"):
                f.write("\n")
        else:
            csv.DictWriter(f, fields).writeheader()
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)
    if path.read_text(encoding="utf-8").count("\n") < line_count_before + len(rows):
        raise RuntimeError(f"append verification failed for {path}")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(stable_json(payload) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_state(path: Path) -> dict[str, Any]:
    p = path / "system_state.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / "system_state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path / "system_state.json")


def audit(path: Path, event_type: str, payload: dict[str, Any]) -> None:
    append_jsonl(path / "audit_log.jsonl", {"audit_id": "aud_" + sha256_text(stable_json({"t": now_utc(), "event": event_type, "payload": payload}))[:24], "created_at_utc": now_utc(), "event_type": event_type, "payload": payload})


def error_log(path: Path, event_type: str, payload: dict[str, Any]) -> None:
    append_jsonl(path / "errors.jsonl", {"created_at_utc": now_utc(), "event_type": event_type, "payload": payload})
    audit(path, event_type, payload)


def load_config(config_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, out)]
    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value.strip())
    return out


def parse_scalar(value: str) -> Any:
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


class LedgerLock:
    def __init__(self, path: Path):
        self.path = path / ".run_loop.lock"
        self.acquired = False

    def __enter__(self):
        try:
            self.path.mkdir()
            (self.path / "created_at_utc").write_text(now_utc(), encoding="utf-8")
            self.acquired = True
            return self
        except FileExistsError as exc:
            raise RuntimeError("another run-loop instance already holds the lock") from exc

    def __exit__(self, exc_type, exc, tb):
        if self.acquired and self.path.exists():
            shutil.rmtree(self.path)


def formal_hash_payload(root: Path, config_path: Path) -> dict[str, str]:
    return {
        "config_sha256": file_sha256(config_path),
        "core_code_sha256": file_sha256(root / "src/forward_simulation_v5_1.py"),
        "reporting_code_sha256": file_sha256(root / "src/forward_reporting_v5_1.py") if (root / "src/forward_reporting_v5_1.py").exists() else "",
        "preregistration_sha256": file_sha256(root / "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md") if (root / "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md").exists() else "",
    }


def assert_formal_hashes(path: Path, root: Path, config_path: Path, mode: str) -> None:
    if mode != FORMAL:
        return
    state = read_state(path)
    if not state.get("formal_started_at_utc"):
        raise RuntimeError("formal v5.1 sample is not started")
    current = formal_hash_payload(root, config_path)
    expected = {k: state.get(k) for k in current}
    drift = {k: {"expected": expected[k], "current": current[k]} for k in current if expected.get(k) != current[k]}
    if drift:
        audit(path, "hash_freeze_reject", {"drift": drift})
        raise RuntimeError("formal hash freeze mismatch; refusing to write formal ledger")


def assert_write_allowed(path: Path, root: Path | None, config_path: Path | None, mode: str) -> None:
    if mode != FORMAL:
        return
    if root is None or config_path is None:
        audit(path, "formal_write_without_hash_context_rejected", {"mode": mode})
        raise RuntimeError("formal writes require root and config_path for hash-freeze verification")
    assert_formal_hashes(path, root, config_path, mode)


def id_for(prefix: str, payload: dict[str, Any]) -> str:
    return prefix + "_" + sha256_text(stable_json(payload))[:24]


def normalize_level(level: dict[str, Any]) -> dict[str, float] | None:
    price = fnum(level.get("price"), math.nan)
    size = fnum(level.get("size"), math.nan)
    if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
        return {"price": price, "size": size}
    return None


def normalize_book(raw: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    bids = [x for x in (normalize_level(v) for v in raw.get("bids", [])) if x]
    asks = [x for x in (normalize_level(v) for v in raw.get("asks", [])) if x]
    return {"bids": sorted(bids, key=lambda x: x["price"], reverse=True), "asks": sorted(asks, key=lambda x: x["price"])}


def best_bid_ask(book: dict[str, list[dict[str, float]]]) -> tuple[float, float, float]:
    bid = book["bids"][0]["price"] if book["bids"] else math.nan
    ask = book["asks"][0]["price"] if book["asks"] else math.nan
    return bid, ask, ask - bid if math.isfinite(bid) and math.isfinite(ask) else math.nan


def snapshot_id(token_id: str, purpose: str, raw: dict[str, Any]) -> str:
    raw_ts = raw.get("timestamp") or raw.get("serverTime") or raw.get("updated_at") or ""
    return id_for("ob", {"token_id": token_id, "purpose": purpose, "server_timestamp": raw_ts, "book": normalize_book(raw)})


def record_snapshot(path: Path, mode: str, token_id: str, purpose: str, raw: dict[str, Any], source: str) -> str:
    sid = snapshot_id(token_id, purpose, raw)
    existing = {r["snapshot_id"] for r in read_jsonl(path / "orderbook_snapshots.jsonl")}
    if sid not in existing:
        append_jsonl(path / "orderbook_snapshots.jsonl", {"snapshot_id": sid, "captured_at_utc": now_utc(), "mode": mode, "token_id": token_id, "purpose": purpose, "source": source, "raw_orderbook": raw})
    return sid


def fetch_book(token_id: str, base_url: str = "https://clob.polymarket.com") -> dict[str, Any]:
    url = base_url.rstrip("/") + "/book?" + urllib.parse.urlencode({"token_id": token_id})
    req = urllib.request.Request(url, headers={"User-Agent": "huskyvs-forward-v5.1/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_book_file(book_file: Path, token_id: str) -> dict[str, Any]:
    payload = json.loads(book_file.read_text(encoding="utf-8"))
    if "bids" in payload and "asks" in payload:
        return payload
    if token_id in payload:
        return payload[token_id]
    raise KeyError(f"token {token_id} not found in {book_file}")


def simulate_buy(raw: dict[str, Any], intended_usd: float, max_price: float) -> dict[str, Any]:
    book = normalize_book(raw)
    remaining = intended_usd
    shares = 0.0
    gross = 0.0
    levels = []
    for level in book["asks"]:
        if remaining <= EPS or level["price"] > max_price + EPS:
            break
        qty = min(level["size"], remaining / level["price"])
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= usd
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
    bid, ask, spread = best_bid_ask(book)
    return {"shares": shares, "gross": gross, "vwap": gross / shares if shares > EPS else math.nan, "remaining_usd": max(remaining, 0.0), "complete": remaining <= 1e-6, "best_bid": bid, "best_ask": ask, "spread": spread, "levels": levels}


def simulate_sell(raw: dict[str, Any], shares_to_sell: float) -> dict[str, Any]:
    book = normalize_book(raw)
    remaining = shares_to_sell
    shares = 0.0
    gross = 0.0
    levels = []
    for level in book["bids"]:
        if remaining <= EPS:
            break
        qty = min(level["size"], remaining)
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= qty
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
    bid, ask, spread = best_bid_ask(book)
    return {"shares": shares, "gross": gross, "vwap": gross / shares if shares > EPS else math.nan, "remaining_shares": max(remaining, 0.0), "complete": remaining <= 1e-6, "best_bid": bid, "best_ask": ask, "spread": spread, "levels": levels}


def fee(gross: float, bps: float) -> float:
    return round(gross * bps / 10000.0, 8)


def latest_by(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, str]]:
    out = {}
    for row in rows:
        out[row[key]] = row
    return out


def signal_lookup(path: Path) -> dict[str, dict[str, str]]:
    return {r["signal_id"]: r for r in read_csv(path / "signals.csv")}


def latest_entry_state(path: Path) -> dict[str, dict[str, str]]:
    return latest_by(read_csv(path / "entry_order_state.csv"), "signal_id")


def entry_fill_snapshot_keys(path: Path) -> set[tuple[str, str]]:
    return {(r["signal_id"], r["snapshot_id"]) for r in read_csv(path / "entry_fills.csv")}


def exit_snapshot_keys(path: Path) -> set[tuple[str, str, str]]:
    return {(r["trigger_id"], r["snapshot_id"], r["strategy_id"]) for r in read_csv(path / "exit_fills.csv")}


def register_signals(path: Path, signals_file: Path, mode: str, config: dict[str, Any], root: Path, config_path: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    ensure_ledger(path)
    assert_formal_hashes(path, root, config_path, mode)
    now = now or utcnow()
    existing = set(signal_lookup(path))
    rows = read_csv(signals_file)
    accepted = []
    state = read_state(path)
    for row in rows:
        sid = row.get("signal_id", "")
        if not sid or sid in existing:
            continue
        try:
            created = parse_utc(row["created_at_utc"])
            if mode == FORMAL:
                started = state.get("formal_started_at_utc")
                if not started or created < parse_utc(started):
                    raise ValueError("signal before formal start")
                delay = (now - created).total_seconds()
                max_delay = fnum(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
                future_skew = fnum(config["sample_rules"].get("allowed_future_skew_seconds", 30))
                if delay > max_delay:
                    raise ValueError("signal registration delay exceeded")
                if delay < -future_skew:
                    raise ValueError("signal timestamp is too far in the future")
            valid_minutes = fnum(config["entry"].get("entry_valid_minutes", 10))
            event_key = make_event_key(row["city"], row["weather_date_local"], row.get("weather_metric", "high"))
            signal = {field: row.get(field, "") for field in SIGNAL_FIELDS}
            signal.update({
                "registered_at_utc": now.isoformat(),
                "city_normalized": normalize_city(row["city"]),
                "weather_metric": normalize_metric(row.get("weather_metric", "high")),
                "event_key": event_key,
                "entry_deadline_utc": (created + timedelta(minutes=valid_minutes)).isoformat(),
                "mode": mode,
            })
            if row.get("side", "").upper() != "BUY":
                raise ValueError("only BUY entry signals are supported")
            atomic_append_csv(path / "signals.csv", SIGNAL_FIELDS, [signal])
            atomic_append_csv(path / "entry_order_state.csv", ENTRY_STATE_FIELDS, [entry_state_row(signal, 0.0, 0.0, "pending", "", "")])
            audit(path, "signal_registered", {"signal_id": sid, "event_key": event_key})
            accepted.append(signal)
        except Exception as exc:
            audit(path, "signal_rejected", {"signal_id": sid, "reason": str(exc)})
    return accepted


def entry_state_row(signal: dict[str, Any], filled_usd: float, filled_shares: float, status: str, last_attempt: str, reason: str) -> dict[str, Any]:
    intended = fnum(signal["intended_usd"])
    return {
        "entry_state_id": id_for("entry_state", {"signal_id": signal["signal_id"], "filled": filled_usd, "status": status, "t": now_utc(), "reason": reason}),
        "signal_id": signal["signal_id"],
        "token_id": signal["token_id"],
        "updated_at_utc": now_utc(),
        "intended_usd": intended,
        "filled_entry_usd": filled_usd,
        "remaining_entry_usd": max(intended - filled_usd, 0.0),
        "filled_entry_shares": filled_shares,
        "entry_status": status,
        "max_entry_price": signal["max_entry_price"],
        "entry_deadline_utc": signal["entry_deadline_utc"],
        "last_entry_attempt_at": last_attempt,
        "last_attempt_reason": reason,
        "mode": signal["mode"],
    }


def process_entry(path: Path, signal: dict[str, Any], raw_book: dict[str, Any], mode: str, config: dict[str, Any], source: str = "fixture", now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> dict[str, Any]:
    ensure_ledger(path)
    assert_write_allowed(path, root, config_path, mode)
    now = now or utcnow()
    states = latest_entry_state(path)
    state = states.get(signal["signal_id"])
    if not state:
        atomic_append_csv(path / "entry_order_state.csv", ENTRY_STATE_FIELDS, [entry_state_row(signal, 0.0, 0.0, "pending", "", "initialized")])
        state = latest_entry_state(path)[signal["signal_id"]]
    if state["entry_status"] in {"filled", "expired", "cancelled"}:
        return {"status": "skipped_" + state["entry_status"]}
    if now > parse_utc(signal["entry_deadline_utc"]):
        atomic_append_csv(path / "entry_order_state.csv", ENTRY_STATE_FIELDS, [entry_state_row(signal, fnum(state["filled_entry_usd"]), fnum(state["filled_entry_shares"]), "expired", now.isoformat(), "entry_deadline_reached")])
        audit(path, "entry_expired", {"signal_id": signal["signal_id"]})
        return {"status": "expired"}
    sid = record_snapshot(path, mode, signal["token_id"], "entry", raw_book, source)
    if (signal["signal_id"], sid) in entry_fill_snapshot_keys(path):
        return {"status": "skipped_duplicate_snapshot", "snapshot_id": sid}
    remaining = fnum(state["remaining_entry_usd"])
    buy = simulate_buy(raw_book, remaining, fnum(signal["max_entry_price"]))
    if buy["shares"] <= EPS:
        atomic_append_csv(path / "entry_order_state.csv", ENTRY_STATE_FIELDS, [entry_state_row(signal, fnum(state["filled_entry_usd"]), fnum(state["filled_entry_shares"]), state["entry_status"], now.isoformat(), "ask_above_max_or_no_depth")])
        audit(path, "entry_not_filled", {"signal_id": signal["signal_id"], "snapshot_id": sid})
        return {"status": "not_filled", "snapshot_id": sid}
    entry_bps = fnum(config["fees"].get("entry_fee_bps", 0))
    entry_fee = fee(buy["gross"], entry_bps)
    fill_id = id_for("entry", {"signal": signal["signal_id"], "snapshot": sid, "gross": buy["gross"]})
    new_filled_usd = fnum(state["filled_entry_usd"]) + buy["gross"]
    new_filled_shares = fnum(state["filled_entry_shares"]) + buy["shares"]
    status = "filled" if new_filled_usd >= fnum(signal["intended_usd"]) - 1e-6 else "partial"
    atomic_append_csv(path / "entry_fills.csv", ENTRY_FILL_FIELDS, [{
        "entry_fill_id": fill_id, "signal_id": signal["signal_id"], "event_key": signal["event_key"], "token_id": signal["token_id"], "snapshot_id": sid, "filled_at_utc": now.isoformat(),
        "gross_entry_cost": buy["gross"], "entry_fee": entry_fee, "total_entry_cost": buy["gross"] + entry_fee, "filled_shares": buy["shares"], "entry_vwap": buy["vwap"],
        "best_bid": buy["best_bid"], "best_ask": buy["best_ask"], "spread": buy["spread"], "complete_fill": buy["complete"], "unfilled_usd_after_fill": max(fnum(signal["intended_usd"]) - new_filled_usd, 0.0),
        "depth_levels_json": stable_json(buy["levels"]), "mode": mode,
    }])
    lot_rows = []
    for strategy_id in STRATEGY_IDS:
        lot_rows.append({"lot_id": id_for("lot", {"strategy": strategy_id, "entry": fill_id}), "strategy_id": strategy_id, "signal_id": signal["signal_id"], "event_key": signal["event_key"], "token_id": signal["token_id"], "entry_fill_id": fill_id, "created_at_utc": now.isoformat(), "entry_shares": buy["shares"], "gross_entry_cost": buy["gross"], "entry_fee": entry_fee, "mode": mode})
    atomic_append_csv(path / "strategy_lots.csv", LOT_FIELDS, lot_rows)
    atomic_append_csv(path / "entry_order_state.csv", ENTRY_STATE_FIELDS, [entry_state_row(signal, new_filled_usd, new_filled_shares, status, now.isoformat(), "filled" if status == "filled" else "partial_fill")])
    audit(path, "entry_fill_recorded", {"signal_id": signal["signal_id"], "entry_fill_id": fill_id, "status": status})
    return {"status": status, "entry_fill_id": fill_id, "snapshot_id": sid, "filled_usd": buy["gross"], "filled_shares": buy["shares"]}


def lot_open_shares(path: Path, strategy_id: str, signal_id: str) -> list[dict[str, Any]]:
    lots = [r for r in read_csv(path / "strategy_lots.csv") if r["strategy_id"] == strategy_id and r["signal_id"] == signal_id]
    exit_allocs = read_csv(path / "exit_fill_allocations.csv")
    settlement_allocs = read_csv(path / "settlement_allocations.csv")
    sold_by_lot = defaultdict(float)
    for r in exit_allocs:
        if r["strategy_id"] == strategy_id and r["signal_id"] == signal_id:
            sold_by_lot[r["lot_id"]] += fnum(r["allocated_shares"])
    for r in settlement_allocs:
        if r["strategy_id"] == strategy_id and r["signal_id"] == signal_id:
            sold_by_lot[r["lot_id"]] += fnum(r["settled_shares"])
    out = []
    for lot in sorted(lots, key=lambda r: r["created_at_utc"]):
        shares = fnum(lot["entry_shares"]) - sold_by_lot[lot["lot_id"]]
        if shares > EPS:
            unit_cost = (fnum(lot["gross_entry_cost"]) + fnum(lot["entry_fee"])) / fnum(lot["entry_shares"])
            out.append({**lot, "open_shares": shares, "unit_cost": unit_cost})
    return out


def signal_position(path: Path, strategy_id: str, signal_id: str) -> dict[str, float]:
    lots = lot_open_shares(path, strategy_id, signal_id)
    shares = sum(fnum(l["open_shares"]) for l in lots)
    cost = sum(fnum(l["open_shares"]) * fnum(l["unit_cost"]) for l in lots)
    return {"shares": shares, "cost": cost, "avg_cost": cost / shares if shares > EPS else math.nan}


def trigger_latest(path: Path) -> dict[str, dict[str, str]]:
    return latest_by(read_csv(path / "strategy_triggers.csv"), "trigger_id")


def trigger_id_for(signal_id: str, strategy_id: str, stage: str) -> str:
    return id_for("trig", {"signal_id": signal_id, "strategy_id": strategy_id, "stage": stage})


def upsert_trigger(path: Path, signal: dict[str, Any], strategy_id: str, position: dict[str, float], now: datetime) -> dict[str, Any]:
    strategy = STRATEGIES[strategy_id]
    tid = trigger_id_for(signal["signal_id"], strategy_id, strategy["stage"])
    latest = trigger_latest(path).get(tid)
    if latest:
        return latest
    target = position["shares"] * float(strategy["fraction"])
    row = {
        "trigger_id": tid, "signal_id": signal["signal_id"], "strategy_id": strategy_id, "trigger_stage_id": strategy["stage"], "event_key": signal["event_key"], "token_id": signal["token_id"],
        "trigger_created_at": now.isoformat(), "trigger_target_shares": target, "trigger_filled_shares": 0.0, "trigger_remaining_shares": target, "trigger_status": "open", "trigger_completed_at": "",
        "rolling_avg_cost_at_trigger": position["avg_cost"], "threshold_price": position["avg_cost"] * float(strategy["multiple"]), "mode": signal["mode"],
    }
    atomic_append_csv(path / "strategy_triggers.csv", TRIGGER_FIELDS, [row])
    audit(path, "trigger_created", {"trigger_id": tid, "target_shares": target})
    return row


def update_trigger(path: Path, trigger: dict[str, Any], add_filled: float, now: datetime) -> dict[str, Any]:
    filled = fnum(trigger["trigger_filled_shares"]) + add_filled
    target = fnum(trigger["trigger_target_shares"])
    remaining = max(target - filled, 0.0)
    status = "completed" if remaining <= 1e-6 else "open"
    row = {**trigger, "trigger_filled_shares": filled, "trigger_remaining_shares": remaining, "trigger_status": status, "trigger_completed_at": now.isoformat() if status == "completed" else ""}
    atomic_append_csv(path / "strategy_triggers.csv", TRIGGER_FIELDS, [row])
    return row


def allocate_fifo(lots: list[dict[str, Any]], shares: float, gross: float, fee_amount: float) -> list[dict[str, Any]]:
    remaining = shares
    out = []
    for lot in lots:
        if remaining <= EPS:
            break
        qty = min(fnum(lot["open_shares"]), remaining)
        ratio = qty / shares if shares > EPS else 0
        out.append({"lot": lot, "shares": qty, "gross": gross * ratio, "fee": fee_amount * ratio})
        remaining -= qty
    if remaining > 1e-6:
        raise RuntimeError("FIFO allocation could not cover sold shares")
    return out


def signal_is_settled(path: Path, signal_id: str, strategy_id: str | None = None) -> bool:
    rows = read_csv(path / "settlements.csv")
    if strategy_id:
        return any(r["signal_id"] == signal_id and r["strategy_id"] == strategy_id for r in rows)
    return any(r["signal_id"] == signal_id for r in rows)


def process_exit(path: Path, signal: dict[str, Any], raw_book: dict[str, Any], mode: str, config: dict[str, Any], source: str = "fixture", now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> list[dict[str, Any]]:
    ensure_ledger(path)
    assert_write_allowed(path, root, config_path, mode)
    now = now or utcnow()
    sid = record_snapshot(path, mode, signal["token_id"], "exit", raw_book, source)
    prior_keys = exit_snapshot_keys(path)
    results = []
    for strategy_id, strategy in STRATEGIES.items():
        if strategy["multiple"] is None or signal_is_settled(path, signal["signal_id"], strategy_id):
            continue
        position = signal_position(path, strategy_id, signal["signal_id"])
        if position["shares"] <= EPS:
            continue
        existing = trigger_latest(path).get(trigger_id_for(signal["signal_id"], strategy_id, strategy["stage"]))
        if existing and existing["trigger_status"] == "completed":
            results.append({"strategy_id": strategy_id, "status": "trigger_completed"})
            continue
        trigger = existing
        planned = fnum(trigger["trigger_remaining_shares"]) if trigger else position["shares"] * float(strategy["fraction"])
        if planned <= EPS:
            continue
        sell_probe = simulate_sell(raw_book, planned)
        threshold = (fnum(trigger["threshold_price"]) if trigger else position["avg_cost"] * float(strategy["multiple"]))
        if sell_probe["shares"] <= EPS or not math.isfinite(sell_probe["vwap"]) or sell_probe["vwap"] + EPS < threshold:
            results.append({"strategy_id": strategy_id, "status": "not_triggered", "executable_vwap": sell_probe["vwap"], "threshold": threshold})
            continue
        trigger = trigger or upsert_trigger(path, signal, strategy_id, position, now)
        planned = min(fnum(trigger["trigger_remaining_shares"]), position["shares"])
        if (trigger["trigger_id"], sid, strategy_id) in prior_keys:
            results.append({"strategy_id": strategy_id, "status": "skipped_duplicate_snapshot"})
            continue
        sell = simulate_sell(raw_book, planned)
        shares = min(sell["shares"], fnum(trigger["trigger_remaining_shares"]))
        if shares <= EPS:
            continue
        gross = sell["gross"] * (shares / sell["shares"])
        exit_fee = fee(gross, fnum(config["fees"].get("exit_fee_bps", 0)))
        fill_id = id_for("exit", {"trigger": trigger["trigger_id"], "snapshot": sid, "shares": shares})
        atomic_append_csv(path / "exit_fills.csv", EXIT_FILL_FIELDS, [{
            "exit_fill_id": fill_id, "trigger_id": trigger["trigger_id"], "signal_id": signal["signal_id"], "strategy_id": strategy_id, "trigger_stage_id": strategy["stage"], "event_key": signal["event_key"], "token_id": signal["token_id"], "snapshot_id": sid, "filled_at_utc": now.isoformat(),
            "planned_sell_shares": planned, "filled_shares": shares, "gross_exit_proceeds": gross, "exit_fee": exit_fee, "net_exit_proceeds": gross - exit_fee, "exit_vwap": gross / shares, "best_bid": sell["best_bid"], "best_ask": sell["best_ask"], "spread": sell["spread"],
            "complete_fill": shares >= planned - 1e-6, "unfilled_trigger_shares_after_fill": max(fnum(trigger["trigger_remaining_shares"]) - shares, 0.0), "depth_levels_json": stable_json(sell["levels"]), "mode": mode,
        }])
        allocs = allocate_fifo(lot_open_shares(path, strategy_id, signal["signal_id"]), shares, gross, exit_fee)
        atomic_append_csv(path / "exit_fill_allocations.csv", EXIT_ALLOC_FIELDS, [{
            "allocation_id": id_for("alloc", {"exit": fill_id, "lot": a["lot"]["lot_id"]}), "exit_fill_id": fill_id, "trigger_id": trigger["trigger_id"], "strategy_id": strategy_id, "signal_id": signal["signal_id"], "event_key": signal["event_key"], "token_id": signal["token_id"], "lot_id": a["lot"]["lot_id"], "allocated_shares": a["shares"], "gross_exit_proceeds": a["gross"], "exit_fee": a["fee"], "mode": mode,
        } for a in allocs])
        update_trigger(path, trigger, shares, now)
        audit(path, "exit_fill_recorded", {"exit_fill_id": fill_id, "trigger_id": trigger["trigger_id"], "shares": shares})
        results.append({"strategy_id": strategy_id, "status": "exit_filled", "filled_shares": shares, "remaining_after": signal_position(path, strategy_id, signal["signal_id"])["shares"]})
    return results


def active_signals(path: Path) -> list[dict[str, str]]:
    signals = signal_lookup(path)
    states = latest_entry_state(path)
    active_ids = set()
    for sid, st in states.items():
        if st.get("entry_status") in {"pending", "partial"}:
            active_ids.add(sid)
    for sid, sig in signals.items():
        for strategy_id in STRATEGY_IDS:
            if signal_position(path, strategy_id, sid)["shares"] > EPS and not signal_is_settled(path, sid, strategy_id):
                active_ids.add(sid)
    return [signals[sid] for sid in sorted(active_ids) if sid in signals]


def process_entry_for_signal(path: Path, signal: dict[str, Any], book_provider, mode: str, config: dict[str, Any], now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> dict[str, Any] | None:
    state = latest_entry_state(path).get(signal["signal_id"])
    if state and state["entry_status"] in {"filled", "expired", "cancelled"}:
        return None
    raw = book_provider(signal["token_id"], "entry")
    return process_entry(path, signal, raw, mode, config, "run_loop", now, root, config_path)


def process_exits_for_signal(path: Path, signal: dict[str, Any], book_provider, mode: str, config: dict[str, Any], now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> list[dict[str, Any]]:
    if all(signal_position(path, strategy_id, signal["signal_id"])["shares"] <= EPS or signal_is_settled(path, signal["signal_id"], strategy_id) for strategy_id in STRATEGY_IDS):
        return []
    raw = book_provider(signal["token_id"], "exit")
    return process_exit(path, signal, raw, mode, config, "run_loop", now, root, config_path)


def run_loop(path: Path, mode: str, config: dict[str, Any], book_provider, iterations: int, sleep_seconds: float = 0.0, now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> dict[str, Any]:
    ensure_ledger(path)
    count = 0
    with LedgerLock(path):
        try:
            while iterations <= 0 or count < iterations:
                state = read_state(path)
                if state.get("stopped"):
                    break
                if state.get("paused"):
                    append_jsonl(path / "heartbeat.jsonl", {"created_at_utc": now_utc(), "status": "paused"})
                    count += 1
                    if sleep_seconds:
                        time.sleep(sleep_seconds)
                    continue
                for sig in active_signals(path):
                    try:
                        assert_write_allowed(path, root, config_path, mode)
                        process_entry_for_signal(path, sig, book_provider, mode, config, now, root, config_path)
                        process_exits_for_signal(path, sig, book_provider, mode, config, now, root, config_path)
                    except Exception as exc:
                        error_log(path, "market_loop_error", {"signal_id": sig.get("signal_id"), "error": str(exc)})
                append_jsonl(path / "heartbeat.jsonl", {"created_at_utc": now_utc(), "status": "ok", "iteration": count + 1})
                count += 1
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            state = read_state(path)
            state["last_interrupt_at_utc"] = now_utc()
            write_state(path, state)
            audit(path, "run_loop_interrupted", {"iterations_completed": count})
    return {"iterations_completed": count}


def settlement_existing(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    return {(r["strategy_id"], r["signal_id"]): r for r in read_csv(path / "settlements.csv")}


def settle(path: Path, settlements_file: Path, mode: str, config: dict[str, Any], now: datetime | None = None, root: Path | None = None, config_path: Path | None = None) -> list[dict[str, Any]]:
    ensure_ledger(path)
    assert_write_allowed(path, root, config_path, mode)
    now = now or utcnow()
    signals = signal_lookup(path)
    existing = settlement_existing(path)
    rows = read_csv(settlements_file)
    out = []
    alloc_rows = []
    for row in rows:
        sig = signals[row["signal_id"]]
        evidence_hash = row.get("evidence_hash") or sha256_text(stable_json({k: row.get(k, "") for k in ["source", "source_reference", "settlement_outcome", "settlement_value"]}))
        value = fnum(row["settlement_value"], math.nan)
        if not math.isfinite(value):
            raise ValueError("invalid settlement value")
        for strategy_id in STRATEGY_IDS:
            key = (strategy_id, sig["signal_id"])
            if key in existing:
                old = existing[key]
                if fnum(old["settlement_value"]) != value or old["settlement_outcome"] != row["settlement_outcome"]:
                    audit(path, "settlement_conflict_rejected", {"signal_id": sig["signal_id"], "strategy_id": strategy_id})
                    raise RuntimeError("conflicting settlement result")
                continue
            lots = lot_open_shares(path, strategy_id, sig["signal_id"])
            remaining = sum(fnum(l["open_shares"]) for l in lots)
            proceeds = remaining * value
            settlement_fee = fee(proceeds, fnum(config["fees"].get("settlement_fee_bps", 0)))
            sid = id_for("set", {"signal": sig["signal_id"], "strategy": strategy_id, "value": value})
            out.append({"settlement_id": sid, "signal_id": sig["signal_id"], "strategy_id": strategy_id, "event_key": sig["event_key"], "condition_id": sig["condition_id"], "token_id": sig["token_id"], "settlement_outcome": row["settlement_outcome"], "settlement_value": value, "source": row.get("source", ""), "source_reference": row.get("source_reference", ""), "observed_at_utc": row.get("observed_at_utc", ""), "recorded_at_utc": now.isoformat(), "operator_notes": row.get("operator_notes", ""), "evidence_hash": evidence_hash, "settlement_status": "final", "remaining_shares_settled": remaining, "settlement_proceeds": proceeds, "settlement_fee": settlement_fee, "mode": mode})
            for lot in lots:
                ratio = fnum(lot["open_shares"]) / remaining if remaining > EPS else 0
                alloc_rows.append({"settlement_allocation_id": id_for("set_alloc", {"settlement": sid, "lot": lot["lot_id"]}), "settlement_id": sid, "strategy_id": strategy_id, "signal_id": sig["signal_id"], "event_key": sig["event_key"], "token_id": sig["token_id"], "lot_id": lot["lot_id"], "settled_shares": lot["open_shares"], "settlement_proceeds": proceeds * ratio, "settlement_fee": settlement_fee * ratio, "mode": mode})
    if out:
        atomic_append_csv(path / "settlements.csv", SETTLEMENT_FIELDS, out)
        atomic_append_csv(path / "settlement_allocations.csv", SETTLEMENT_ALLOC_FIELDS, alloc_rows)
        audit(path, "settlements_recorded", {"count": len(out)})
    return out


def aggregate_results(path: Path) -> list[dict[str, Any]]:
    signals = list(signal_lookup(path).values())
    entry_fills = read_csv(path / "entry_fills.csv")
    exit_allocs = read_csv(path / "exit_fill_allocations.csv")
    set_allocs = read_csv(path / "settlement_allocations.csv")
    settlements = read_csv(path / "settlements.csv")
    latest_triggers = list(latest_by(read_csv(path / "strategy_triggers.csv"), "trigger_id").values())
    rows = []
    for event_key in sorted({s["event_key"] for s in signals}):
        event_signals = [s for s in signals if s["event_key"] == event_key]
        for strategy_id in STRATEGY_IDS:
            gross_entry = sum(fnum(r["gross_entry_cost"]) for r in entry_fills if any(r["signal_id"] == s["signal_id"] for s in event_signals))
            entry_fee = sum(fnum(r["entry_fee"]) for r in entry_fills if any(r["signal_id"] == s["signal_id"] for s in event_signals))
            gross_exit = sum(fnum(r["gross_exit_proceeds"]) for r in exit_allocs if r["strategy_id"] == strategy_id and r["event_key"] == event_key)
            exit_fee = sum(fnum(r["exit_fee"]) for r in exit_allocs if r["strategy_id"] == strategy_id and r["event_key"] == event_key)
            set_proceeds = sum(fnum(r["settlement_proceeds"]) for r in set_allocs if r["strategy_id"] == strategy_id and r["event_key"] == event_key)
            set_fee = sum(fnum(r["settlement_fee"]) for r in set_allocs if r["strategy_id"] == strategy_id and r["event_key"] == event_key)
            signal_count = len(event_signals)
            position_count = len({r["signal_id"] for r in entry_fills if any(r["signal_id"] == s["signal_id"] for s in event_signals)})
            settled_signals = {r["signal_id"] for r in settlements if r["strategy_id"] == strategy_id and r["event_key"] == event_key}
            settled_event = 1 if position_count > 0 and all(s["signal_id"] in settled_signals for s in event_signals if any(r["signal_id"] == s["signal_id"] for r in entry_fills)) else 0
            total_fees = entry_fee + exit_fee + set_fee
            gross_pnl = gross_exit + set_proceeds - gross_entry
            net_pnl = gross_pnl - total_fees
            rows.append({"event_key": event_key, "strategy_id": strategy_id, "mode": path.name, "signal_count": signal_count, "position_count": position_count, "traded_event_count": 1 if position_count else 0, "settled_event_count": settled_event, "gross_entry_cost": gross_entry, "entry_fee": entry_fee, "gross_exit_proceeds": gross_exit, "exit_fee": exit_fee, "settlement_proceeds": set_proceeds, "settlement_fee": set_fee, "total_fees": total_fees, "gross_pnl": gross_pnl if settled_event else math.nan, "net_pnl": net_pnl if settled_event else math.nan, "triggered_take_profit": any(r["strategy_id"] == strategy_id and r["event_key"] == event_key for r in exit_allocs), "incomplete_take_profit": any(fnum(t["trigger_remaining_shares"]) > EPS and t["strategy_id"] == strategy_id and t["event_key"] == event_key for t in latest_triggers),})
    atomic_append_csv(path / "event_results.csv", EVENT_RESULT_FIELDS, [])  # Ensure header.
    tmp = path / "event_results.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, EVENT_RESULT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path / "event_results.csv")
    return rows


def audit_integrity(path: Path, root: Path, config_path: Path, mode: str) -> dict[str, Any]:
    ensure_ledger(path)
    signals = read_csv(path / "signals.csv")
    entry_fills = read_csv(path / "entry_fills.csv")
    exits = read_csv(path / "exit_fills.csv")
    lots = read_csv(path / "strategy_lots.csv")
    triggers = latest_by(read_csv(path / "strategy_triggers.csv"), "trigger_id")
    snapshots = read_jsonl(path / "orderbook_snapshots.jsonl")
    settlements = read_csv(path / "settlements.csv")
    duplicate = lambda rows, key: len([r[key] for r in rows]) - len({r[key] for r in rows})
    signal_ids = [r["signal_id"] for r in signals]
    fill_ids = [r["entry_fill_id"] for r in entry_fills] + [r["exit_fill_id"] for r in exits]
    snapshot_ids = [r["snapshot_id"] for r in snapshots]
    trigger_overfill = sum(1 for t in triggers.values() if fnum(t["trigger_filled_shares"]) - fnum(t["trigger_target_shares"]) > 1e-6)
    negative_inventory = 0
    over_sell = 0
    for sig in signals:
        for strategy_id in STRATEGY_IDS:
            pos = signal_position(path, strategy_id, sig["signal_id"])
            if pos["shares"] < -1e-6:
                negative_inventory += 1
            bought = sum(fnum(l["entry_shares"]) for l in lots if l["signal_id"] == sig["signal_id"] and l["strategy_id"] == strategy_id)
            sold = sum(fnum(a["allocated_shares"]) for a in read_csv(path / "exit_fill_allocations.csv") if a["signal_id"] == sig["signal_id"] and a["strategy_id"] == strategy_id)
            settled = sum(fnum(a["settled_shares"]) for a in read_csv(path / "settlement_allocations.csv") if a["signal_id"] == sig["signal_id"] and a["strategy_id"] == strategy_id)
            if sold + settled - bought > 1e-6:
                over_sell += 1
    strategy_entry_inconsistent = 0
    for sig in signals:
        shares = [sum(fnum(l["entry_shares"]) for l in lots if l["signal_id"] == sig["signal_id"] and l["strategy_id"] == strategy_id) for strategy_id in STRATEGY_IDS]
        if shares and max(shares) - min(shares) > 1e-6:
            strategy_entry_inconsistent += 1
    hash_drift = False
    if mode == FORMAL and read_state(path).get("formal_started_at_utc"):
        current = formal_hash_payload(root, config_path)
        state = read_state(path)
        hash_drift = any(state.get(k) != v for k, v in current.items())
    formal_timeouts = 0
    if mode == FORMAL:
        config = load_config(config_path)
        max_delay = fnum(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
        for sig in signals:
            if (parse_utc(sig["registered_at_utc"]) - parse_utc(sig["created_at_utc"])).total_seconds() > max_delay:
                formal_timeouts += 1
    settled_after_exit = 0
    settled_keys = {(r["signal_id"], r["strategy_id"]): parse_utc(r["recorded_at_utc"]) for r in settlements}
    for ex in exits:
        t = settled_keys.get((ex["signal_id"], ex["strategy_id"]))
        if t and parse_utc(ex["filled_at_utc"]) > t:
            settled_after_exit += 1
    result = {
        "mode": mode,
        "duplicate_signal_id": duplicate(signals, "signal_id"),
        "duplicate_fill_id": len(fill_ids) - len(set(fill_ids)),
        "duplicate_snapshot_id": len(snapshot_ids) - len(set(snapshot_ids)),
        "negative_inventory": negative_inventory,
        "over_sell": over_sell,
        "trigger_overfill": trigger_overfill,
        "strategy_entry_inconsistent": strategy_entry_inconsistent,
        "demo_pollution_formal": mode == FORMAL and any(r.get("mode") == DEMO for r in signals + entry_fills + exits),
        "hash_drift": hash_drift,
        "settled_after_exit": settled_after_exit,
        "formal_signal_registration_timeout": formal_timeouts,
    }
    result["ok"] = not any(v for k, v in result.items() if k not in {"mode", "ok"})
    return result


def start_formal(root: Path, config_path: Path, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise RuntimeError("start-formal requires --confirm")
    path = data_dir(root, FORMAL)
    ensure_ledger(path)
    state = read_state(path)
    if state.get("formal_started_at_utc"):
        return {"status": "already_started"}
    state.update({"schema_version": SCHEMA_VERSION, "formal_started_at_utc": now_utc(), **formal_hash_payload(root, config_path), "paused": False, "stopped": False})
    write_state(path, state)
    audit(path, "formal_started_v5_1", state)
    return {"status": "started", "formal_started_at_utc": state["formal_started_at_utc"]}


def status(path: Path, root: Path, config_path: Path, mode: str) -> dict[str, Any]:
    state = read_state(path)
    heartbeats = read_jsonl(path / "heartbeat.jsonl")
    errors = read_jsonl(path / "errors.jsonl")
    active = len(active_signals(path))
    entry_states = latest_entry_state(path)
    positions = sum(1 for sig in signal_lookup(path).values() for strategy_id in STRATEGY_IDS if signal_position(path, strategy_id, sig["signal_id"])["shares"] > EPS)
    hash_match = True
    if mode == FORMAL and state.get("formal_started_at_utc"):
        cur = formal_hash_payload(root, config_path)
        hash_match = all(state.get(k) == v for k, v in cur.items())
    return {"mode": mode, "last_heartbeat": heartbeats[-1] if heartbeats else None, "active_signals": active, "pending_or_partial_entries": sum(1 for s in entry_states.values() if s["entry_status"] in {"pending", "partial"}), "active_strategy_positions": positions, "recent_error": errors[-1] if errors else None, "paused": state.get("paused", False), "stopped": state.get("stopped", False), "hash_match": hash_match}


def pause_resume_stop(path: Path, action: str, root: Path | None = None, config_path: Path | None = None, mode: str = DEMO) -> dict[str, Any]:
    ensure_ledger(path)
    assert_write_allowed(path, root, config_path, mode)
    state = read_state(path)
    if action == "pause":
        state["paused"] = True
    elif action == "resume":
        state["paused"] = False
        state["stopped"] = False
    elif action == "stop":
        state["stopped"] = True
    write_state(path, state)
    audit(path, "state_" + action, state)
    return {"status": action}


def demo(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = data_dir(root, DEMO)
    ensure_ledger(path)
    created = utcnow()
    sig_file = path / "demo_signal.csv"
    if not sig_file.exists():
        atomic_append_csv(sig_file, ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "market_slug", "condition_id", "token_id", "outcome", "side", "forecast_temperature", "forecast_probability", "market_probability_at_signal", "intended_usd", "max_entry_price", "source", "notes"], [{
            "signal_id": "demo_v5_1_sig_001", "created_at_utc": created.isoformat(), "city": "Demo City", "weather_date_local": "2099-01-01", "weather_metric": "high", "market_slug": "demo-market", "condition_id": "demo-cond", "token_id": "demo-token", "outcome": "YES", "side": "BUY", "forecast_temperature": "30", "forecast_probability": "0.62", "market_probability_at_signal": "0.10", "intended_usd": "100", "max_entry_price": "0.10", "source": "demo_fixture", "notes": "offline demo only"
        }])
    registered = register_signals(path, sig_file, DEMO, config, root, root / "config/forward_simulation_v5_1.yaml", created)
    sig = registered[0] if registered else signal_lookup(path)["demo_v5_1_sig_001"]
    entry_book_1 = {"timestamp": "demo-entry-1", "bids": [{"price": "0.09", "size": "100"}], "asks": [{"price": "0.10", "size": "100"}]}
    entry_book_2 = {"timestamp": "demo-entry-2", "bids": [{"price": "0.09", "size": "100"}], "asks": [{"price": "0.10", "size": "900"}]}
    process_entry(path, sig, entry_book_1, DEMO, config, "demo", created)
    process_entry(path, sig, entry_book_2, DEMO, config, "demo", created + timedelta(seconds=1))
    exit_book = {"timestamp": "demo-exit-1", "bids": [{"price": "0.25", "size": "1000"}], "asks": [{"price": "0.26", "size": "100"}]}
    exits = process_exit(path, sig, exit_book, DEMO, config, "demo", created + timedelta(seconds=2))
    set_file = path / "demo_settlement.csv"
    if not set_file.exists():
        atomic_append_csv(set_file, ["signal_id", "condition_id", "token_id", "settlement_outcome", "settlement_value", "source", "source_reference", "observed_at_utc", "operator_notes"], [{"signal_id": sig["signal_id"], "condition_id": sig["condition_id"], "token_id": sig["token_id"], "settlement_outcome": "NO", "settlement_value": "0", "source": "demo_fixture", "source_reference": "offline", "observed_at_utc": (created + timedelta(seconds=3)).isoformat(), "operator_notes": "demo settles to zero"}])
    settlements = settle(path, set_file, DEMO, config, created + timedelta(seconds=3))
    aggregate_results(path)
    return {"entry_state": latest_entry_state(path)[sig["signal_id"]], "exit_results": exits, "settlements": len(settlements), "integrity": audit_integrity(path, root, root / "config/forward_simulation_v5_1.yaml", DEMO)}


def build_provider(args, config: dict[str, Any]):
    if args.orderbook_file:
        book_file = Path(args.orderbook_file)
        return lambda token_id, purpose: read_book_file(book_file, token_id)
    return lambda token_id, purpose: fetch_book(token_id, config.get("api", {}).get("clob_base_url", "https://clob.polymarket.com"))


def command_register(args, config):
    root = Path(args.root)
    mode = args.mode
    path = data_dir(root, mode)
    rows = register_signals(path, Path(args.signals_file), mode, config, root, Path(args.config))
    print(json.dumps({"registered": len(rows)}, ensure_ascii=False, indent=2))


def command_process_entry(args, config):
    root = Path(args.root)
    mode = args.mode
    path = data_dir(root, mode)
    assert_formal_hashes(path, root, Path(args.config), mode)
    sig = signal_lookup(path)[args.signal_id]
    raw = build_provider(args, config)(sig["token_id"], "entry")
    print(json.dumps(process_entry(path, sig, raw, mode, config, "manual", root=root, config_path=Path(args.config)), ensure_ascii=False, indent=2))


def command_monitor_once(args, config):
    root = Path(args.root)
    mode = args.mode
    path = data_dir(root, mode)
    assert_formal_hashes(path, root, Path(args.config), mode)
    provider = build_provider(args, config)
    results = []
    for sig in active_signals(path):
        try:
            e = process_entry_for_signal(path, sig, provider, mode, config, root=root, config_path=Path(args.config))
            if e:
                results.append(e)
            results.extend(process_exits_for_signal(path, sig, provider, mode, config, root=root, config_path=Path(args.config)))
        except Exception as exc:
            error_log(path, "monitor_once_error", {"signal_id": sig.get("signal_id"), "error": str(exc)})
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))


def command_run_loop(args, config):
    root = Path(args.root)
    mode = args.mode
    path = data_dir(root, mode)
    assert_formal_hashes(path, root, Path(args.config), mode)
    provider = build_provider(args, config)
    interval = fnum(config.get("polling", {}).get("default_interval_seconds", 60))
    sleep_seconds = 0 if args.iterations else interval
    print(json.dumps(run_loop(path, mode, config, provider, args.iterations, sleep_seconds, root=root, config_path=Path(args.config)), ensure_ascii=False, indent=2))


def command_settle(args, config):
    root = Path(args.root)
    mode = args.mode
    path = data_dir(root, mode)
    assert_formal_hashes(path, root, Path(args.config), mode)
    print(json.dumps({"settlements": len(settle(path, Path(args.settlements_file), mode, config, root=root, config_path=Path(args.config)))}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=".")
    p.add_argument("--config", default="config/forward_simulation_v5_1.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sp = sub.add_parser("start-formal"); sp.add_argument("--confirm", action="store_true")
    sp = sub.add_parser("register"); sp.add_argument("--signals-file", required=True); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("process-entry"); sp.add_argument("--signal-id", required=True); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--orderbook-file")
    sp = sub.add_parser("monitor-once"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--orderbook-file")
    sp = sub.add_parser("run-loop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--orderbook-file"); sp.add_argument("--iterations", type=int, default=1)
    sp = sub.add_parser("settle"); sp.add_argument("--settlements-file", required=True); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("status"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("pause"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("resume"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("stop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("audit-integrity"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sub.add_parser("demo")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    config = load_config(Path(args.config))
    mode = getattr(args, "mode", FORMAL)
    path = data_dir(root, mode)
    ensure_ledger(path)
    try:
        if args.command == "init":
            ensure_ledger(data_dir(root, FORMAL)); ensure_ledger(data_dir(root, DEMO)); print(json.dumps({"status": "initialized"}, indent=2))
        elif args.command == "start-formal":
            print(json.dumps(start_formal(root, Path(args.config), args.confirm), ensure_ascii=False, indent=2))
        elif args.command == "register":
            command_register(args, config)
        elif args.command == "process-entry":
            command_process_entry(args, config)
        elif args.command == "monitor-once":
            command_monitor_once(args, config)
        elif args.command == "run-loop":
            command_run_loop(args, config)
        elif args.command == "settle":
            command_settle(args, config)
        elif args.command == "status":
            print(json.dumps(status(path, root, Path(args.config), mode), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command in {"pause", "resume", "stop"}:
            print(json.dumps(pause_resume_stop(path, args.command, root, Path(args.config), mode), ensure_ascii=False, indent=2))
        elif args.command == "audit-integrity":
            print(json.dumps(audit_integrity(path, root, Path(args.config), mode), ensure_ascii=False, indent=2, sort_keys=True))
        elif args.command == "demo":
            print(json.dumps(demo(root, config), ensure_ascii=False, indent=2, sort_keys=True))
    except Exception as exc:
        if path.exists():
            error_log(path, args.command + "_failed", {"error": str(exc)})
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
