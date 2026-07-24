#!/usr/bin/env python3
"""Forward-only weather market simulation ledger v5.

This module never connects to a wallet, never asks for private keys, and never
submits real orders. It records forward signals, public orderbook snapshots, and
simulated fills using append-only ledgers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "forward_simulation_v5.0.0"
ORDERBOOK_DOCS_URL = "https://docs.polymarket.com/api-reference/market-data/get-order-book"
DEFAULT_CLOB_BASE_URL = "https://clob.polymarket.com"
FORMAL_MODE = "formal"
DEMO_MODE = "demo"
STRATEGY_IDS = [
    "hold_to_settlement",
    "tp_2x_sell_50pct",
    "tp_2x_sell_75pct",
    "tp_5x_sell_25pct",
]
STRATEGIES = {
    "hold_to_settlement": {"multiple": None, "sell_fraction": 0.0, "description": "完全持有到结算"},
    "tp_2x_sell_50pct": {"multiple": 2.0, "sell_fraction": 0.50, "description": "2x卖出剩余仓位50%"},
    "tp_2x_sell_75pct": {"multiple": 2.0, "sell_fraction": 0.75, "description": "2x卖出剩余仓位75%"},
    "tp_5x_sell_25pct": {"multiple": 5.0, "sell_fraction": 0.25, "description": "5x卖出剩余仓位25%"},
}
EPS = 1e-9


SIGNAL_FIELDS = [
    "signal_id",
    "created_at_utc",
    "city",
    "weather_date_local",
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
    "source",
    "notes",
    "mode",
    "registered_at_utc",
]

EVENT_FIELDS = [
    "event_id",
    "signal_id",
    "city",
    "weather_date_local",
    "market_slug",
    "condition_id",
    "token_id",
    "outcome",
    "created_at_utc",
    "mode",
]

ENTRY_FILL_FIELDS = [
    "entry_fill_id",
    "signal_id",
    "event_id",
    "snapshot_id",
    "token_id",
    "filled_at_utc",
    "intended_usd",
    "max_entry_price",
    "filled_shares",
    "spent_usd",
    "entry_vwap",
    "best_bid",
    "best_ask",
    "spread",
    "complete_fill",
    "unfilled_usd",
    "depth_levels_json",
    "fee_scenario_bps",
    "simulated_fee_usd",
    "mode",
]

POSITION_FIELDS = [
    "position_event_id",
    "strategy_id",
    "signal_id",
    "event_id",
    "token_id",
    "updated_at_utc",
    "event_type",
    "delta_buy_shares",
    "delta_buy_cost",
    "delta_sell_shares",
    "sell_proceeds",
    "remaining_shares",
    "remaining_cost_basis",
    "rolling_avg_cost",
    "realized_gross_proceeds",
    "realized_cost_basis_removed",
    "source_fill_id",
    "mode",
    "notes",
]

EXIT_FILL_FIELDS = [
    "exit_fill_id",
    "strategy_id",
    "signal_id",
    "event_id",
    "snapshot_id",
    "token_id",
    "filled_at_utc",
    "trigger_multiple",
    "planned_sell_fraction",
    "planned_sell_shares",
    "filled_shares",
    "exit_vwap",
    "gross_proceeds",
    "best_bid",
    "best_ask",
    "spread",
    "complete_fill",
    "unfilled_shares",
    "rolling_avg_cost_before",
    "threshold_price",
    "depth_levels_json",
    "fee_scenario_bps",
    "simulated_fee_usd",
    "mode",
]

SETTLEMENT_FIELDS = [
    "settlement_id",
    "strategy_id",
    "signal_id",
    "event_id",
    "token_id",
    "settled_at_utc",
    "settlement_price",
    "remaining_shares",
    "settlement_value",
    "realized_gross_proceeds",
    "total_buy_cost",
    "gross_pnl",
    "fee_scenario_bps",
    "simulated_fee_usd",
    "net_pnl",
    "mode",
    "notes",
]

EVENT_RESULT_FIELDS = [
    "event_id",
    "strategy_id",
    "mode",
    "city",
    "weather_date_local",
    "market_slug",
    "position_count",
    "simulated_buy_usd",
    "simulated_entry_spent_usd",
    "unfilled_entry_usd",
    "avg_entry_vwap",
    "avg_exit_vwap",
    "gross_pnl",
    "net_pnl",
    "triggered_take_profit",
    "incomplete_take_profit",
    "settled",
]

CSV_SCHEMAS = {
    "signals.csv": SIGNAL_FIELDS,
    "events.csv": EVENT_FIELDS,
    "entry_fills.csv": ENTRY_FILL_FIELDS,
    "strategy_positions.csv": POSITION_FIELDS,
    "exit_fills.csv": EXIT_FILL_FIELDS,
    "settlements.csv": SETTLEMENT_FIELDS,
    "event_results.csv": EVENT_RESULT_FIELDS,
}


@dataclass
class Orderbook:
    token_id: str
    bids: list[dict[str, float]]
    asks: list[dict[str, float]]
    raw: dict[str, Any]


@dataclass
class PositionState:
    strategy_id: str
    signal_id: str
    event_id: str
    token_id: str
    remaining_shares: float = 0.0
    remaining_cost_basis: float = 0.0
    realized_gross_proceeds: float = 0.0
    realized_cost_basis_removed: float = 0.0

    @property
    def rolling_avg_cost(self) -> float:
        return self.remaining_cost_basis / self.remaining_shares if self.remaining_shares > EPS else math.nan


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def csv_path(data_dir: Path, name: str) -> Path:
    return data_dir / name


def ensure_ledger(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, fields in CSV_SCHEMAS.items():
        path = csv_path(data_dir, name)
        if not path.exists():
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fields)
                writer.writeheader()
    for name in ["orderbook_snapshots.jsonl", "audit_log.jsonl"]:
        path = data_dir / name
        if not path.exists():
            path.touch()
    state_path = data_dir / "system_state.json"
    if not state_path.exists():
        write_state(
            data_dir,
            {
                "version": VERSION,
                "mode": FORMAL_MODE if data_dir.name != DEMO_MODE else DEMO_MODE,
                "formal_started_at_utc": None,
                "last_run_at_utc": None,
                "notes": "State file may be updated; raw ledgers remain append-only.",
            },
        )


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(stable_json(payload) + "\n")


def read_state(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "system_state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_state(data_dir: Path, state: dict[str, Any]) -> None:
    (data_dir / "system_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def audit(data_dir: Path, event_type: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        data_dir / "audit_log.jsonl",
        {
            "audit_id": sha256_text(stable_json({"event_type": event_type, "payload": payload, "time": now_utc()})),
            "created_at_utc": now_utc(),
            "event_type": event_type,
            "payload": payload,
        },
    )


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """Small YAML reader for this project's simple config shape."""
    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, out)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
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
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


def data_dir_for(root: Path, mode: str) -> Path:
    if mode == DEMO_MODE:
        return root / "data/forward_v5/demo"
    return root / "data/forward_v5"


def event_id_for(signal: dict[str, Any]) -> str:
    raw = "|".join(
        [
            signal.get("city", ""),
            signal.get("weather_date_local", ""),
            signal.get("market_slug", ""),
            signal.get("condition_id", ""),
        ]
    )
    return "evt_" + sha256_text(raw)[:16]


def normalize_level(level: dict[str, Any]) -> dict[str, float] | None:
    price = fnum(level.get("price"), math.nan)
    size = fnum(level.get("size"), math.nan)
    if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
        return None
    return {"price": price, "size": size}


def normalize_orderbook(raw: dict[str, Any], token_id: str) -> Orderbook:
    bids = [x for x in (normalize_level(v) for v in raw.get("bids", [])) if x]
    asks = [x for x in (normalize_level(v) for v in raw.get("asks", [])) if x]
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return Orderbook(token_id=token_id, bids=bids, asks=asks, raw=raw)


def best_bid_ask(book: Orderbook) -> tuple[float, float, float]:
    best_bid = book.bids[0]["price"] if book.bids else math.nan
    best_ask = book.asks[0]["price"] if book.asks else math.nan
    spread = best_ask - best_bid if math.isfinite(best_bid) and math.isfinite(best_ask) else math.nan
    return best_bid, best_ask, spread


def simulate_buy_from_asks(book: Orderbook, intended_usd: float, max_entry_price: float) -> dict[str, Any]:
    remaining_usd = intended_usd
    spent = 0.0
    shares = 0.0
    levels: list[dict[str, float]] = []
    for level in book.asks:
        if level["price"] > max_entry_price + EPS or remaining_usd <= EPS:
            break
        qty = min(level["size"], remaining_usd / level["price"])
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        spent += usd
        shares += qty
        remaining_usd -= usd
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
    best_bid, best_ask, spread = best_bid_ask(book)
    return {
        "filled_shares": shares,
        "spent_usd": spent,
        "vwap": spent / shares if shares > EPS else math.nan,
        "complete_fill": remaining_usd <= 1e-6,
        "unfilled_usd": max(remaining_usd, 0.0),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "levels": levels,
    }


def simulate_sell_to_bids(book: Orderbook, planned_shares: float) -> dict[str, Any]:
    remaining = planned_shares
    proceeds = 0.0
    shares = 0.0
    levels: list[dict[str, float]] = []
    for level in book.bids:
        if remaining <= EPS:
            break
        qty = min(level["size"], remaining)
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        proceeds += usd
        shares += qty
        remaining -= qty
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
    best_bid, best_ask, spread = best_bid_ask(book)
    return {
        "filled_shares": shares,
        "gross_proceeds": proceeds,
        "vwap": proceeds / shares if shares > EPS else math.nan,
        "complete_fill": remaining <= 1e-6,
        "unfilled_shares": max(remaining, 0.0),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "levels": levels,
    }


def snapshot_id_for(mode: str, token_id: str, purpose: str, raw_orderbook: dict[str, Any]) -> str:
    # Excludes fetch time by design: the same raw snapshot should not be reused
    # to account for another fill after restart.
    return "ob_" + sha256_text(stable_json({"mode": mode, "token_id": token_id, "purpose": purpose, "raw": raw_orderbook}))[:24]


def record_orderbook_snapshot(data_dir: Path, mode: str, token_id: str, purpose: str, raw_orderbook: dict[str, Any], source: str) -> str:
    snapshot_id = snapshot_id_for(mode, token_id, purpose, raw_orderbook)
    existing = {json.loads(line).get("snapshot_id") for line in (data_dir / "orderbook_snapshots.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
    if snapshot_id not in existing:
        append_jsonl(
            data_dir / "orderbook_snapshots.jsonl",
            {
                "snapshot_id": snapshot_id,
                "captured_at_utc": now_utc(),
                "mode": mode,
                "token_id": token_id,
                "purpose": purpose,
                "source": source,
                "raw_orderbook": raw_orderbook,
            },
        )
    return snapshot_id


def fetch_orderbook(token_id: str, base_url: str = DEFAULT_CLOB_BASE_URL) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/book?" + urllib.parse.urlencode({"token_id": token_id})
    req = urllib.request.Request(url, headers={"User-Agent": "huskyvs-forward-sim-v5/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def read_orderbook_file(path: Path, token_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "bids" in payload and "asks" in payload:
        return payload
    if token_id in payload:
        return payload[token_id]
    raise KeyError(f"orderbook for token_id={token_id} not found in {path}")


def existing_ids(data_dir: Path, filename: str, field: str) -> set[str]:
    return {row.get(field, "") for row in read_csv_rows(csv_path(data_dir, filename))}


def validate_signal(signal: dict[str, Any], data_dir: Path, mode: str) -> None:
    required = ["signal_id", "created_at_utc", "city", "weather_date_local", "market_slug", "condition_id", "token_id", "outcome", "side", "intended_usd", "max_entry_price", "source"]
    missing = [field for field in required if not signal.get(field)]
    if missing:
        raise ValueError(f"signal missing required fields: {missing}")
    if signal.get("side", "").upper() != "BUY":
        raise ValueError("v5 currently accepts BUY entry signals only")
    if fnum(signal.get("intended_usd")) <= 0:
        raise ValueError("intended_usd must be positive")
    if not (0 < fnum(signal.get("max_entry_price")) <= 1):
        raise ValueError("max_entry_price must be in (0, 1]")
    if mode == FORMAL_MODE:
        state = read_state(data_dir)
        formal_started = state.get("formal_started_at_utc")
        if not formal_started:
            raise ValueError("formal sample is not started; run start-formal first")
        if parse_utc(signal["created_at_utc"]) < parse_utc(formal_started):
            raise ValueError("historical signal cannot enter formal forward sample")


def register_signals(data_dir: Path, signals_file: Path, mode: str) -> list[dict[str, Any]]:
    ensure_ledger(data_dir)
    registered_at = now_utc()
    existing = existing_ids(data_dir, "signals.csv", "signal_id")
    rows = read_csv_rows(signals_file)
    out: list[dict[str, Any]] = []
    for raw in rows:
        if not raw.get("signal_id") or raw["signal_id"] in existing:
            continue
        signal = {field: raw.get(field, "") for field in SIGNAL_FIELDS}
        signal["mode"] = mode
        signal["registered_at_utc"] = registered_at
        validate_signal(signal, data_dir, mode)
        event_id = event_id_for(signal)
        append_csv(csv_path(data_dir, "signals.csv"), SIGNAL_FIELDS, [signal])
        append_csv(
            csv_path(data_dir, "events.csv"),
            EVENT_FIELDS,
            [
                {
                    "event_id": event_id,
                    "signal_id": signal["signal_id"],
                    "city": signal["city"],
                    "weather_date_local": signal["weather_date_local"],
                    "market_slug": signal["market_slug"],
                    "condition_id": signal["condition_id"],
                    "token_id": signal["token_id"],
                    "outcome": signal["outcome"],
                    "created_at_utc": signal["created_at_utc"],
                    "mode": mode,
                }
            ],
        )
        audit(data_dir, "signal_registered", {"signal_id": signal["signal_id"], "event_id": event_id, "mode": mode})
        out.append(signal)
    return out


def latest_position_states(data_dir: Path) -> dict[tuple[str, str], PositionState]:
    states: dict[tuple[str, str], PositionState] = {}
    for row in read_csv_rows(csv_path(data_dir, "strategy_positions.csv")):
        key = (row["strategy_id"], row["token_id"])
        states[key] = PositionState(
            strategy_id=row["strategy_id"],
            signal_id=row["signal_id"],
            event_id=row["event_id"],
            token_id=row["token_id"],
            remaining_shares=fnum(row.get("remaining_shares")),
            remaining_cost_basis=fnum(row.get("remaining_cost_basis")),
            realized_gross_proceeds=fnum(row.get("realized_gross_proceeds")),
            realized_cost_basis_removed=fnum(row.get("realized_cost_basis_removed")),
        )
    return states


def process_entry(
    data_dir: Path,
    signal: dict[str, Any],
    raw_orderbook: dict[str, Any],
    mode: str,
    source: str = "public_orderbook",
    fee_bps: float = 0.0,
) -> dict[str, Any]:
    ensure_ledger(data_dir)
    if signal["signal_id"] in existing_ids(data_dir, "entry_fills.csv", "signal_id"):
        return {"status": "skipped_existing_entry", "signal_id": signal["signal_id"]}
    event_id = event_id_for(signal)
    book = normalize_orderbook(raw_orderbook, signal["token_id"])
    snapshot_id = record_orderbook_snapshot(data_dir, mode, signal["token_id"], "entry", raw_orderbook, source)
    entry = simulate_buy_from_asks(book, fnum(signal["intended_usd"]), fnum(signal["max_entry_price"]))
    if entry["filled_shares"] <= EPS:
        audit(data_dir, "entry_not_filled", {"signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "reason": "no executable ask depth under max_entry_price"})
        return {"status": "not_filled", "signal_id": signal["signal_id"], "snapshot_id": snapshot_id}
    fill_id = "entry_" + sha256_text(stable_json({"signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "spent": entry["spent_usd"]}))[:20]
    fee = entry["spent_usd"] * fee_bps / 10000.0
    fill_row = {
        "entry_fill_id": fill_id,
        "signal_id": signal["signal_id"],
        "event_id": event_id,
        "snapshot_id": snapshot_id,
        "token_id": signal["token_id"],
        "filled_at_utc": now_utc(),
        "intended_usd": signal["intended_usd"],
        "max_entry_price": signal["max_entry_price"],
        "filled_shares": entry["filled_shares"],
        "spent_usd": entry["spent_usd"],
        "entry_vwap": entry["vwap"],
        "best_bid": entry["best_bid"],
        "best_ask": entry["best_ask"],
        "spread": entry["spread"],
        "complete_fill": entry["complete_fill"],
        "unfilled_usd": entry["unfilled_usd"],
        "depth_levels_json": stable_json(entry["levels"]),
        "fee_scenario_bps": fee_bps,
        "simulated_fee_usd": fee,
        "mode": mode,
    }
    append_csv(csv_path(data_dir, "entry_fills.csv"), ENTRY_FILL_FIELDS, [fill_row])
    position_rows = []
    prior_states = latest_position_states(data_dir)
    for strategy_id in STRATEGY_IDS:
        prior = prior_states.get((strategy_id, signal["token_id"]))
        prior_shares = prior.remaining_shares if prior else 0.0
        prior_cost = prior.remaining_cost_basis if prior else 0.0
        prior_proceeds = prior.realized_gross_proceeds if prior else 0.0
        prior_removed = prior.realized_cost_basis_removed if prior else 0.0
        remaining_shares = prior_shares + entry["filled_shares"]
        remaining_cost = prior_cost + entry["spent_usd"]
        position_rows.append(
            {
                "position_event_id": "pos_" + sha256_text(stable_json({"strategy": strategy_id, "fill": fill_id}))[:20],
                "strategy_id": strategy_id,
                "signal_id": signal["signal_id"],
                "event_id": event_id,
                "token_id": signal["token_id"],
                "updated_at_utc": now_utc(),
                "event_type": "entry_buy",
                "delta_buy_shares": entry["filled_shares"],
                "delta_buy_cost": entry["spent_usd"],
                "delta_sell_shares": 0,
                "sell_proceeds": 0,
                "remaining_shares": remaining_shares,
                "remaining_cost_basis": remaining_cost,
                "rolling_avg_cost": remaining_cost / remaining_shares if remaining_shares > EPS else math.nan,
                "realized_gross_proceeds": prior_proceeds,
                "realized_cost_basis_removed": prior_removed,
                "source_fill_id": fill_id,
                "mode": mode,
                "notes": "same entry fill copied to all frozen strategy branches; add-ons roll into token-level strategy inventory",
            }
        )
    append_csv(csv_path(data_dir, "strategy_positions.csv"), POSITION_FIELDS, position_rows)
    audit(data_dir, "entry_filled", {"signal_id": signal["signal_id"], "entry_fill_id": fill_id, "snapshot_id": snapshot_id, "filled_shares": entry["filled_shares"], "spent_usd": entry["spent_usd"]})
    return {"status": "filled", "signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "entry_fill_id": fill_id, "filled_shares": entry["filled_shares"], "spent_usd": entry["spent_usd"]}


def active_signals(data_dir: Path) -> list[dict[str, str]]:
    signals = {row["signal_id"]: row for row in read_csv_rows(csv_path(data_dir, "signals.csv"))}
    signals_by_token: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(csv_path(data_dir, "signals.csv")):
        signals_by_token[row["token_id"]] = row
    states = latest_position_states(data_dir)
    active_tokens = {token_id for _, token_id in states if states[(_, token_id)].remaining_shares > EPS}
    return [signals_by_token[token] for token in sorted(active_tokens) if token in signals_by_token]


def process_exits_for_signal(
    data_dir: Path,
    signal: dict[str, Any],
    raw_orderbook: dict[str, Any],
    mode: str,
    source: str = "public_orderbook",
    fee_bps: float = 0.0,
) -> list[dict[str, Any]]:
    ensure_ledger(data_dir)
    book = normalize_orderbook(raw_orderbook, signal["token_id"])
    snapshot_id = record_orderbook_snapshot(data_dir, mode, signal["token_id"], "exit", raw_orderbook, source)
    prior_exit_keys = {(row["strategy_id"], row["token_id"], row["snapshot_id"]) for row in read_csv_rows(csv_path(data_dir, "exit_fills.csv"))}
    states = latest_position_states(data_dir)
    results: list[dict[str, Any]] = []
    exit_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    for strategy_id in STRATEGY_IDS:
        strategy = STRATEGIES[strategy_id]
        if strategy["multiple"] is None:
            continue
        key = (strategy_id, signal["token_id"])
        state = states.get(key)
        if not state or state.remaining_shares <= EPS:
            continue
        if (strategy_id, signal["token_id"], snapshot_id) in prior_exit_keys:
            results.append({"status": "skipped_existing_snapshot", "strategy_id": strategy_id, "signal_id": signal["signal_id"], "snapshot_id": snapshot_id})
            continue
        planned_shares = state.remaining_shares * float(strategy["sell_fraction"])
        sell = simulate_sell_to_bids(book, planned_shares)
        threshold = state.rolling_avg_cost * float(strategy["multiple"])
        if sell["filled_shares"] <= EPS or not math.isfinite(sell["vwap"]) or sell["vwap"] + EPS < threshold:
            results.append({"status": "not_triggered", "strategy_id": strategy_id, "signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "executable_vwap": sell["vwap"], "threshold": threshold})
            continue
        avg_cost_before = state.rolling_avg_cost
        sold_shares = min(sell["filled_shares"], state.remaining_shares)
        removed_cost = avg_cost_before * sold_shares
        remaining_shares = state.remaining_shares - sold_shares
        remaining_cost = max(0.0, state.remaining_cost_basis - removed_cost)
        realized_proceeds = state.realized_gross_proceeds + sell["gross_proceeds"]
        realized_cost_removed = state.realized_cost_basis_removed + removed_cost
        exit_fill_id = "exit_" + sha256_text(stable_json({"strategy": strategy_id, "signal_id": signal["signal_id"], "snapshot": snapshot_id, "shares": sold_shares}))[:20]
        fee = sell["gross_proceeds"] * fee_bps / 10000.0
        exit_rows.append(
            {
                "exit_fill_id": exit_fill_id,
                "strategy_id": strategy_id,
                "signal_id": signal["signal_id"],
                "event_id": event_id_for(signal),
                "snapshot_id": snapshot_id,
                "token_id": signal["token_id"],
                "filled_at_utc": now_utc(),
                "trigger_multiple": strategy["multiple"],
                "planned_sell_fraction": strategy["sell_fraction"],
                "planned_sell_shares": planned_shares,
                "filled_shares": sold_shares,
                "exit_vwap": sell["vwap"],
                "gross_proceeds": sell["gross_proceeds"],
                "best_bid": sell["best_bid"],
                "best_ask": sell["best_ask"],
                "spread": sell["spread"],
                "complete_fill": sell["complete_fill"],
                "unfilled_shares": max(planned_shares - sold_shares, 0.0),
                "rolling_avg_cost_before": avg_cost_before,
                "threshold_price": threshold,
                "depth_levels_json": stable_json(sell["levels"]),
                "fee_scenario_bps": fee_bps,
                "simulated_fee_usd": fee,
                "mode": mode,
            }
        )
        position_rows.append(
            {
                "position_event_id": "pos_" + sha256_text(stable_json({"exit": exit_fill_id, "remaining": remaining_shares}))[:20],
                "strategy_id": strategy_id,
                "signal_id": signal["signal_id"],
                "event_id": event_id_for(signal),
                "token_id": signal["token_id"],
                "updated_at_utc": now_utc(),
                "event_type": "exit_sell",
                "delta_buy_shares": 0,
                "delta_buy_cost": 0,
                "delta_sell_shares": sold_shares,
                "sell_proceeds": sell["gross_proceeds"],
                "remaining_shares": remaining_shares,
                "remaining_cost_basis": remaining_cost,
                "rolling_avg_cost": remaining_cost / remaining_shares if remaining_shares > EPS else math.nan,
                "realized_gross_proceeds": realized_proceeds,
                "realized_cost_basis_removed": realized_cost_removed,
                "source_fill_id": exit_fill_id,
                "mode": mode,
                "notes": "take-profit trigger used executable bid-depth VWAP",
            }
        )
        results.append({"status": "exit_filled", "strategy_id": strategy_id, "signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "exit_fill_id": exit_fill_id, "filled_shares": sold_shares, "exit_vwap": sell["vwap"], "complete_fill": sell["complete_fill"]})
    if exit_rows:
        append_csv(csv_path(data_dir, "exit_fills.csv"), EXIT_FILL_FIELDS, exit_rows)
        append_csv(csv_path(data_dir, "strategy_positions.csv"), POSITION_FIELDS, position_rows)
        audit(data_dir, "exit_fills_recorded", {"signal_id": signal["signal_id"], "snapshot_id": snapshot_id, "count": len(exit_rows)})
    return results


def settle_positions(data_dir: Path, settlements_file: Path, mode: str, fee_bps: float = 0.0) -> list[dict[str, Any]]:
    ensure_ledger(data_dir)
    rows = read_csv_rows(settlements_file)
    states = latest_position_states(data_dir)
    existing = {(row["strategy_id"], row["token_id"]) for row in read_csv_rows(csv_path(data_dir, "settlements.csv"))}
    out: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    for row in rows:
        signal_id = row["signal_id"]
        token_id = row.get("token_id") or ""
        if not token_id and signal_id:
            signal_lookup = {r["signal_id"]: r for r in read_csv_rows(csv_path(data_dir, "signals.csv"))}
            token_id = signal_lookup.get(signal_id, {}).get("token_id", "")
        settlement_price = fnum(row.get("settlement_price"), math.nan)
        if not math.isfinite(settlement_price):
            raise ValueError(f"invalid settlement_price for signal {signal_id}")
        for strategy_id in STRATEGY_IDS:
            if (strategy_id, token_id) in existing:
                continue
            state = states.get((strategy_id, token_id))
            if not state:
                continue
            value = state.remaining_shares * settlement_price
            total_buy_cost = state.remaining_cost_basis + state.realized_cost_basis_removed
            gross_pnl = state.realized_gross_proceeds + value - total_buy_cost
            fee = (state.realized_gross_proceeds + value) * fee_bps / 10000.0
            settlement_id = "set_" + sha256_text(stable_json({"strategy": strategy_id, "signal": signal_id, "price": settlement_price}))[:20]
            out.append(
                {
                    "settlement_id": settlement_id,
                    "strategy_id": strategy_id,
                    "signal_id": signal_id,
                    "event_id": state.event_id,
                    "token_id": state.token_id,
                    "settled_at_utc": row.get("settled_at_utc") or now_utc(),
                    "settlement_price": settlement_price,
                    "remaining_shares": state.remaining_shares,
                    "settlement_value": value,
                    "realized_gross_proceeds": state.realized_gross_proceeds,
                    "total_buy_cost": total_buy_cost,
                    "gross_pnl": gross_pnl,
                    "fee_scenario_bps": fee_bps,
                    "simulated_fee_usd": fee,
                    "net_pnl": gross_pnl - fee,
                    "mode": mode,
                    "notes": row.get("notes", ""),
                }
            )
            position_rows.append(
                {
                    "position_event_id": "pos_" + sha256_text(stable_json({"settlement": settlement_id}))[:20],
                    "strategy_id": strategy_id,
                    "signal_id": signal_id,
                    "event_id": state.event_id,
                    "token_id": state.token_id,
                    "updated_at_utc": now_utc(),
                    "event_type": "settlement",
                    "delta_buy_shares": 0,
                    "delta_buy_cost": 0,
                    "delta_sell_shares": state.remaining_shares,
                    "sell_proceeds": value,
                    "remaining_shares": 0,
                    "remaining_cost_basis": 0,
                    "rolling_avg_cost": math.nan,
                    "realized_gross_proceeds": state.realized_gross_proceeds + value,
                    "realized_cost_basis_removed": total_buy_cost,
                    "source_fill_id": settlement_id,
                    "mode": mode,
                    "notes": "settlement closes remaining simulated inventory",
                }
            )
    if out:
        append_csv(csv_path(data_dir, "settlements.csv"), SETTLEMENT_FIELDS, out)
        append_csv(csv_path(data_dir, "strategy_positions.csv"), POSITION_FIELDS, position_rows)
        audit(data_dir, "settlements_recorded", {"count": len(out), "mode": mode})
    return out


def start_formal(root: Path, config_path: Path, confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise ValueError("start-formal requires --confirm")
    data_dir = data_dir_for(root, FORMAL_MODE)
    ensure_ledger(data_dir)
    state = read_state(data_dir)
    if state.get("formal_started_at_utc"):
        return {"status": "already_started", "formal_started_at_utc": state["formal_started_at_utc"]}
    code_path = Path(__file__)
    state.update(
        {
            "version": VERSION,
            "mode": FORMAL_MODE,
            "formal_started_at_utc": now_utc(),
            "config_path": str(config_path),
            "config_sha256": file_sha256(config_path),
            "code_path": str(code_path),
            "code_sha256": file_sha256(code_path),
            "last_run_at_utc": now_utc(),
            "long_running_monitor_started": False,
        }
    )
    write_state(data_dir, state)
    audit(data_dir, "formal_started", state)
    return {"status": "started", "formal_started_at_utc": state["formal_started_at_utc"], "config_sha256": state["config_sha256"], "code_sha256": state["code_sha256"]}


def integrity_check(data_dir: Path, mode: str) -> dict[str, Any]:
    ensure_ledger(data_dir)
    signals = read_csv_rows(csv_path(data_dir, "signals.csv"))
    entries = read_csv_rows(csv_path(data_dir, "entry_fills.csv"))
    positions = read_csv_rows(csv_path(data_dir, "strategy_positions.csv"))
    exits = read_csv_rows(csv_path(data_dir, "exit_fills.csv"))
    snapshots = [json.loads(line) for line in (data_dir / "orderbook_snapshots.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    signal_ids = {r["signal_id"] for r in signals}
    entry_signal_counts: dict[str, int] = {}
    for row in entries:
        entry_signal_counts[row["signal_id"]] = entry_signal_counts.get(row["signal_id"], 0) + 1
    entry_branch_counts: dict[str, set[str]] = {}
    for row in positions:
        if row["event_type"] == "entry_buy":
            entry_branch_counts.setdefault(row["signal_id"], set()).add(row["strategy_id"])
    duplicate_exit_snapshot = len({(r["strategy_id"], r["token_id"], r["snapshot_id"]) for r in exits}) != len(exits)
    states = latest_position_states(data_dir)
    negative_inventory = [key for key, state in states.items() if state.remaining_shares < -EPS]
    formal_historical = []
    if mode == FORMAL_MODE:
        started = read_state(data_dir).get("formal_started_at_utc")
        if started:
            formal_historical = [r["signal_id"] for r in signals if parse_utc(r["created_at_utc"]) < parse_utc(started)]
    result = {
        "mode": mode,
        "signals": len(signals),
        "entry_fills": len(entries),
        "strategy_position_events": len(positions),
        "exit_fills": len(exits),
        "orderbook_snapshots": len(snapshots),
        "signals_without_duplicate_entry": all(v <= 1 for v in entry_signal_counts.values()),
        "all_entry_fills_have_four_strategy_branches": all(len(entry_branch_counts.get(row["signal_id"], set())) == 4 for row in entries),
        "duplicate_exit_snapshot_accounting": duplicate_exit_snapshot,
        "negative_inventory_count": len(negative_inventory),
        "formal_historical_signal_count": len(formal_historical),
        "demo_data_isolated": mode == DEMO_MODE or all(row.get("mode") != DEMO_MODE for row in signals + entries + positions + exits),
        "ok": all(v <= 1 for v in entry_signal_counts.values())
        and all(len(entry_branch_counts.get(row["signal_id"], set())) == 4 for row in entries)
        and not duplicate_exit_snapshot
        and not negative_inventory
        and not formal_historical,
    }
    return result


def demo(root: Path) -> dict[str, Any]:
    data_dir = data_dir_for(root, DEMO_MODE)
    ensure_ledger(data_dir)
    created = now_utc()
    signal = {
        "signal_id": "demo_weather_signal_v5_001",
        "created_at_utc": created,
        "city": "Demo City",
        "weather_date_local": "2099-01-01",
        "market_slug": "demo-weather-market-v5",
        "condition_id": "demo_condition_v5",
        "token_id": "demo_token_yes_v5",
        "outcome": "YES",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.62",
        "market_probability_at_signal": "0.11",
        "intended_usd": "100",
        "max_entry_price": "0.12",
        "source": "demo_fixture_not_formal",
        "notes": "fixture signal; must never enter formal stats",
        "mode": DEMO_MODE,
        "registered_at_utc": "",
    }
    tmp = data_dir / "demo_signal_input.csv"
    if not tmp.exists():
        append_csv(tmp, SIGNAL_FIELDS[:-2], [{k: signal.get(k, "") for k in SIGNAL_FIELDS[:-2]}])
    registered = register_signals(data_dir, tmp, DEMO_MODE)
    signal = (registered[0] if registered else read_csv_rows(csv_path(data_dir, "signals.csv"))[0])
    entry_book = {
        "market": "demo_token_yes_v5",
        "asset_id": "demo_token_yes_v5",
        "bids": [{"price": "0.10", "size": "100"}],
        "asks": [{"price": "0.10", "size": "500"}, {"price": "0.11", "size": "1000"}],
    }
    entry_result = process_entry(data_dir, signal, entry_book, DEMO_MODE, "demo_fixture_orderbook", 0.0)
    exit_book = {
        "market": "demo_token_yes_v5",
        "asset_id": "demo_token_yes_v5",
        "bids": [{"price": "0.52", "size": "200"}, {"price": "0.49", "size": "1000"}],
        "asks": [{"price": "0.55", "size": "500"}],
    }
    exit_results = process_exits_for_signal(data_dir, signal, exit_book, DEMO_MODE, "demo_fixture_orderbook", 0.0)
    settlement_file = data_dir / "demo_settlement_input.csv"
    if not settlement_file.exists():
        append_csv(settlement_file, ["signal_id", "settled_at_utc", "settlement_price", "notes"], [{"signal_id": signal["signal_id"], "settled_at_utc": now_utc(), "settlement_price": "0", "notes": "demo settlement to zero"}])
    settlements = settle_positions(data_dir, settlement_file, DEMO_MODE, 0.0)
    return {
        "data_dir": str(data_dir),
        "entry_result": entry_result,
        "exit_results": exit_results,
        "settlement_rows": len(settlements),
        "integrity": integrity_check(data_dir, DEMO_MODE),
    }


def command_init(args: argparse.Namespace) -> None:
    root = Path(args.root)
    ensure_ledger(data_dir_for(root, FORMAL_MODE))
    ensure_ledger(data_dir_for(root, DEMO_MODE))
    print(json.dumps({"status": "initialized", "formal_data_dir": str(data_dir_for(root, FORMAL_MODE)), "demo_data_dir": str(data_dir_for(root, DEMO_MODE))}, ensure_ascii=False, indent=2))


def command_register(args: argparse.Namespace) -> None:
    root = Path(args.root)
    mode = args.mode
    data_dir = data_dir_for(root, mode)
    rows = register_signals(data_dir, Path(args.signals_file), mode)
    print(json.dumps({"registered": len(rows), "mode": mode}, ensure_ascii=False, indent=2))


def command_process_entry(args: argparse.Namespace) -> None:
    root = Path(args.root)
    config = load_simple_yaml(Path(args.config))
    data_dir = data_dir_for(root, args.mode)
    ensure_ledger(data_dir)
    signals = {row["signal_id"]: row for row in read_csv_rows(csv_path(data_dir, "signals.csv"))}
    signal = signals[args.signal_id]
    if args.orderbook_file:
        raw = read_orderbook_file(Path(args.orderbook_file), signal["token_id"])
        source = "orderbook_file"
    else:
        raw = fetch_orderbook(signal["token_id"], config.get("api", {}).get("clob_base_url", DEFAULT_CLOB_BASE_URL))
        source = "public_orderbook"
    print(json.dumps(process_entry(data_dir, signal, raw, args.mode, source, fnum(config.get("fees", {}).get("entry_fee_bps", 0.0))), ensure_ascii=False, indent=2))


def command_monitor_once(args: argparse.Namespace) -> None:
    root = Path(args.root)
    config = load_simple_yaml(Path(args.config))
    data_dir = data_dir_for(root, args.mode)
    ensure_ledger(data_dir)
    results = []
    for signal in active_signals(data_dir):
        if args.orderbook_file:
            raw = read_orderbook_file(Path(args.orderbook_file), signal["token_id"])
            source = "orderbook_file"
        else:
            raw = fetch_orderbook(signal["token_id"], config.get("api", {}).get("clob_base_url", DEFAULT_CLOB_BASE_URL))
            source = "public_orderbook"
        results.extend(process_exits_for_signal(data_dir, signal, raw, args.mode, source, fnum(config.get("fees", {}).get("exit_fee_bps", 0.0))))
    print(json.dumps({"mode": args.mode, "results": results}, ensure_ascii=False, indent=2))


def command_settle(args: argparse.Namespace) -> None:
    root = Path(args.root)
    data_dir = data_dir_for(root, args.mode)
    config = load_simple_yaml(Path(args.config))
    rows = settle_positions(data_dir, Path(args.settlements_file), args.mode, fnum(config.get("fees", {}).get("settlement_fee_bps", 0.0)))
    print(json.dumps({"settlement_rows": len(rows), "mode": args.mode}, ensure_ascii=False, indent=2))


def command_start_formal(args: argparse.Namespace) -> None:
    print(json.dumps(start_formal(Path(args.root), Path(args.config), args.confirm), ensure_ascii=False, indent=2))


def command_demo(args: argparse.Namespace) -> None:
    print(json.dumps(demo(Path(args.root)), ensure_ascii=False, indent=2, sort_keys=True))


def command_integrity(args: argparse.Namespace) -> None:
    data_dir = data_dir_for(Path(args.root), args.mode)
    print(json.dumps(integrity_check(data_dir, args.mode), ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward-only weather simulation v5")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config/forward_simulation_v5.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    p.set_defaults(func=command_init)

    p = sub.add_parser("start-formal")
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=command_start_formal)

    p = sub.add_parser("register")
    p.add_argument("--signals-file", required=True)
    p.add_argument("--mode", choices=[FORMAL_MODE, DEMO_MODE], default=FORMAL_MODE)
    p.set_defaults(func=command_register)

    p = sub.add_parser("process-entry")
    p.add_argument("--signal-id", required=True)
    p.add_argument("--mode", choices=[FORMAL_MODE, DEMO_MODE], default=FORMAL_MODE)
    p.add_argument("--orderbook-file")
    p.set_defaults(func=command_process_entry)

    p = sub.add_parser("monitor-once")
    p.add_argument("--mode", choices=[FORMAL_MODE, DEMO_MODE], default=FORMAL_MODE)
    p.add_argument("--orderbook-file")
    p.set_defaults(func=command_monitor_once)

    p = sub.add_parser("settle")
    p.add_argument("--settlements-file", required=True)
    p.add_argument("--mode", choices=[FORMAL_MODE, DEMO_MODE], default=FORMAL_MODE)
    p.set_defaults(func=command_settle)

    p = sub.add_parser("demo")
    p.set_defaults(func=command_demo)

    p = sub.add_parser("integrity")
    p.add_argument("--mode", choices=[FORMAL_MODE, DEMO_MODE], default=FORMAL_MODE)
    p.set_defaults(func=command_integrity)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
