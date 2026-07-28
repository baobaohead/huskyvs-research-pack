#!/usr/bin/env python3
"""Fast, read-only ZBAA shadow strategy lab.

This module deliberately has no formal mode, account integration, signing, or
order-writing code.  It consumes a manual D-1 probability distribution and
public/saved market evidence, then writes an isolated DEMO ledger and reports.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.polymarket_public_adapter_v5_1_8 import (
    AdapterError,
    PublicAdapter,
    consume_buy_depth,
    consume_sell_depth,
    dstr,
    gamma_token_pairs,
    json_safe,
    normalize_orderbook,
    parse_weather_market,
)


VERSION = "husky_zbaa_fast_lab_v1"
SHADOW_MODE = "SHADOW_MANUAL"
STATION = "ZBAA"
CITY = "Beijing"
WEATHER_METRIC = "highest_temperature"
MARKET_METRIC = "high"
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
EDGE_THRESHOLDS = {"EDGE_05": Decimal("0.05"), "EDGE_10": Decimal("0.10"), "EDGE_15": Decimal("0.15")}
PORTFOLIO_RULES = ("MAIN_ONLY", "TOP2_ADJACENT")
EXIT_RULES = ("HOLD", "DOUBLE_SELL_50", "DOUBLE_SELL_75")
PUBLIC_GET_ONLY = True
ACCOUNT_CONNECTION = False
SIGNING = False
REAL_ORDER = False
FORMAL_ZERO_STATUS = {
    "formal_started_at_utc": None,
    "formal_signal_count": 0,
    "formal_snapshot_count": 0,
    "formal_entry_fill_count": 0,
    "formal_exit_fill_count": 0,
    "formal_settlement_count": 0,
    "formal_event_result_count": 0,
}
CITY_ALIASES = {"beijing", "beijing city", "beijing capital", "北京", "北京市", "北京首都机场"}
HISTORY_SOURCE_FILES = (
    "data/raw/trades.csv",
    "data/raw/activity.csv",
    "data/raw/closed_positions.csv",
    "data/raw/current_positions.csv",
    "data/processed/weather_trades_normalized.csv",
    "data/processed/weather_position_lifecycle.csv",
    "data/processed/weather_city_day_baskets.csv",
    "data/processed/city_day_pnl.csv",
    "data/processed/profit_concentration.csv",
    "data/exit_rule_grid_v4.csv",
    "data/exit_rule_position_detail_v4.csv",
)
HISTORY_FIELD_MAP = {
    "event_identity": ["city", "weather_date", "weather_metric"],
    "position_identity": ["asset", "conditionId", "eventSlug", "slug"],
    "temperature_bucket": ["bucket_label", "bucket_kind", "bucket_low", "bucket_high", "outcome"],
    "entry_price": ["first BUY price from weather_trades_normalized.csv", "weighted_avg_buy_price", "avgPrice"],
    "exit_price": ["weighted_avg_sell_price", "SELL price from weather_trades_normalized.csv"],
    "stake": ["buy_usd", "initialValue", "totalBought", "usdcSize"],
    "sold_fraction": ["sell_shares / buy_shares"],
    "remaining_fraction": ["max(buy_shares - sell_shares, 0) / buy_shares"],
    "realized_pnl": ["authoritative_realized_pnl", "realizedPnl"],
    "settlement_pnl": ["hold_to_settlement_pnl in exit-rule detail when available"],
    "trade_side": ["side"],
    "timestamps": ["timestamp", "timestamp_utc", "first_buy_utc", "last_trade_utc"],
}


class ValidationError(ValueError):
    """Fail-closed validation error for shadow inputs/evidence."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}: invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field}: timezone is required")
    return parsed


def decimal(value: Any, field: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValidationError(f"{field}: invalid decimal") from exc
    if not result.is_finite():
        raise ValidationError(f"{field}: must be finite")
    return result


def canonical_city(value: Any) -> str:
    raw = " ".join(str(value or "").strip().split())
    if raw.lower() not in CITY_ALIASES:
        raise ValidationError("city: only Beijing/北京 aliases are allowed")
    return CITY


def probability_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("integer_temperature_probabilities")
    if not isinstance(rows, list) or not rows:
        raise ValidationError("integer_temperature_probabilities: non-empty array required")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValidationError(f"integer_temperature_probabilities[{index}]: object required")
        raw_temp = row.get("temperature_c")
        if isinstance(raw_temp, bool):
            raise ValidationError(f"integer_temperature_probabilities[{index}].temperature_c: integer required")
        try:
            temp = int(raw_temp)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"integer_temperature_probabilities[{index}].temperature_c: integer required") from exc
        if str(raw_temp).strip() not in {str(temp), f"{temp}.0"}:
            raise ValidationError(f"integer_temperature_probabilities[{index}].temperature_c: integer required")
        if temp in seen:
            raise ValidationError(f"integer_temperature_probabilities: duplicate temperature {temp}")
        seen.add(temp)
        probability = decimal(row.get("probability"), f"probability[{temp}]")
        if probability < 0 or probability > 1:
            raise ValidationError(f"probability[{temp}]: must be between 0 and 1")
        normalized.append({"temperature_c": temp, "probability": probability})
    normalized.sort(key=lambda item: item["temperature_c"])
    temperatures = [row["temperature_c"] for row in normalized]
    if temperatures != list(range(temperatures[0], temperatures[-1] + 1)):
        raise ValidationError("integer_temperature_probabilities: temperatures must be a complete contiguous integer range")
    total = sum((row["probability"] for row in normalized), Decimal("0"))
    if abs(total - Decimal("1")) > Decimal("0.000000001"):
        raise ValidationError(f"integer_temperature_probabilities: probabilities sum to {total}, not 1")
    return normalized


def validate_probability_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a manual ZBAA D-1 15:00 CST distribution."""
    if not isinstance(payload, dict):
        raise ValidationError("input root must be an object")
    if str(payload.get("station") or "").upper() != STATION:
        raise ValidationError("station: only ZBAA is allowed; ZSPD and other stations are rejected")
    city = canonical_city(payload.get("city"))
    if str(payload.get("weather_metric") or "") != WEATHER_METRIC:
        raise ValidationError("weather_metric: must be highest_temperature")
    if str(payload.get("mode") or "").upper() != SHADOW_MODE:
        raise ValidationError("mode: only SHADOW_MANUAL is allowed; FORMAL is rejected")
    if "FORMAL" in str(payload.get("data_status") or "").upper():
        raise ValidationError("data_status: FORMAL is rejected")
    for field in ("forecast_run_id", "data_status", "confidence", "explanation"):
        if not str(payload.get(field) or "").strip():
            raise ValidationError(f"{field}: required")
    as_of_cst = parse_datetime(payload.get("as_of_time_cst"), "as_of_time_cst")
    as_of_utc = parse_datetime(payload.get("as_of_time_utc"), "as_of_time_utc")
    generated = parse_datetime(payload.get("generated_at_utc"), "generated_at_utc")
    if as_of_cst.utcoffset() != timedelta(hours=8) or as_of_cst.timetz().replace(tzinfo=None) != time(15, 0, 0):
        raise ValidationError("as_of_time_cst: must be D-1 15:00:00+08:00")
    expected_utc = as_of_cst.astimezone(UTC)
    if as_of_utc.astimezone(UTC) != expected_utc or expected_utc.time().replace(tzinfo=None) != time(7, 0, 0):
        raise ValidationError("as_of_time_utc: must be the corresponding D-1 07:00:00Z")
    try:
        weather_date = date.fromisoformat(str(payload.get("weather_date_local") or ""))
    except ValueError as exc:
        raise ValidationError("weather_date_local: invalid date") from exc
    if weather_date != as_of_cst.date() + timedelta(days=1):
        raise ValidationError("weather_date_local: must be the next local day after as_of_time_cst")
    if generated.astimezone(UTC) > expected_utc:
        raise ValidationError("generated_at_utc: information generated after the 15:00 CST cutoff is not allowed")
    probabilities = probability_rows(payload)
    return {
        **payload,
        "station": STATION,
        "city": city,
        "weather_metric": WEATHER_METRIC,
        "mode": SHADOW_MODE,
        "weather_date_local": weather_date.isoformat(),
        "as_of_time_cst": as_of_cst.isoformat(),
        "as_of_time_utc": as_of_utc.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "generated_at_utc": generated.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "integer_temperature_probabilities": probabilities,
    }


def load_probability_input(path: Path) -> dict[str, Any]:
    return validate_probability_input(json.loads(path.read_text(encoding="utf-8")))


def stable_payload_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(stable_payload_json(value).encode("utf-8")).hexdigest()


def build_run_identity(probability_input: dict[str, Any], intended_usd: Decimal | None = None) -> dict[str, str]:
    identity_fields = {
        "forecast_run_id": probability_input["forecast_run_id"],
        "station": probability_input["station"],
        "weather_date_local": probability_input["weather_date_local"],
        "as_of_time_utc": probability_input["as_of_time_utc"],
    }
    probability_sha256 = sha256_payload(probability_input)
    identity_key = sha256_payload(identity_fields)
    run_content = {
        "probability_input_sha256": probability_sha256,
        "intended_usd": None if intended_usd is None else dstr(intended_usd),
    }
    run_content_sha256 = sha256_payload(run_content)
    run_binding = {**identity_fields, **run_content}
    run_hash = sha256_payload(run_binding)
    return {
        **identity_fields,
        "probability_input_sha256": probability_sha256,
        "intended_usd": run_content["intended_usd"],
        "run_content_sha256": run_content_sha256,
        "identity_key": identity_key,
        "run_id": f"zbaa-{probability_input['weather_date_local'].replace('-', '')}-{run_hash[:20]}",
    }


def build_signal_id(run_id: str, edge_rule: str, portfolio_rule: str, temperature_bucket: str) -> str:
    binding = {
        "run_id": run_id,
        "edge_rule": edge_rule,
        "portfolio_rule": portfolio_rule,
        "temperature_bucket": temperature_bucket,
    }
    return f"sig-{sha256_payload(binding)[:24]}"


def inspect_existing_run(output_dir: Path, identity: dict[str, str]) -> dict[str, Any] | None:
    """Return the existing completed run for an exact rerun, otherwise fail closed."""
    if not output_dir.exists():
        return None
    manifest_path = output_dir / "run_manifest.json"
    if not manifest_path.exists():
        if any(output_dir.iterdir()):
            raise ValidationError("output_dir exists without a run_manifest; refusing to mix or overwrite runs")
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing_identity = manifest.get("run_identity") or {}
    if existing_identity.get("identity_key") == identity["identity_key"]:
        if existing_identity.get("run_content_sha256") != identity["run_content_sha256"]:
            raise ValidationError("conflicting rerun: same run identity has different run content")
    if existing_identity.get("run_id") != identity["run_id"]:
        raise ValidationError(
            f"output_dir already belongs to another run_id: {existing_identity.get('run_id') or 'UNKNOWN'}"
        )
    required = (
        output_dir / "decision_report.json",
        output_dir / "shadow_signals.csv",
        output_dir / "demo_ledger.sqlite3",
    )
    if not all(path.exists() for path in required):
        raise ValidationError("existing run is incomplete; refusing a silent rerun")
    report = json.loads((output_dir / "decision_report.json").read_text(encoding="utf-8"))
    report["run_status"] = "IDEMPOTENT_NOOP"
    return report


def iter_csv_chunks(path: Path, required_fields: Iterable[str] | None = None, chunk_size: int = 5000) -> Iterator[list[dict[str, str]]]:
    """Stream a CSV in bounded chunks and optionally retain only needed fields."""
    wanted = set(required_fields or [])
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        chunk: list[dict[str, str]] = []
        for row in reader:
            chunk.append({key: row.get(key, "") for key in wanted} if wanted else dict(row))
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def csv_header(path: Path) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(next(csv.reader(handle), []))


def fnum(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def event_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("city") or ""), str(row.get("weather_date") or row.get("weather_date_local") or ""), str(row.get("weather_metric") or ""))


def percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    index = (len(clean) - 1) * pct
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return clean[low]
    return clean[low] * (high - index) + clean[high] * (index - low)


def bootstrap_mean_ci(values: list[float], iterations: int = 2000, seed: int = 518) -> list[float] | None:
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return None
    rng = random.Random(seed)
    means = [statistics.fmean(rng.choice(clean) for _ in clean) for _ in range(iterations)]
    return [round(percentile(means, 0.025) or 0.0, 6), round(percentile(means, 0.975) or 0.0, 6)]


def max_consecutive_loss(events: list[dict[str, Any]]) -> dict[str, Any]:
    longest = current = 0
    current_loss = worst_loss = 0.0
    end_key: tuple[str, str, str] | None = None
    for item in sorted(events, key=lambda row: (row["event_key"][1], row["event_key"][0], row["event_key"][2])):
        if item["pnl"] < 0:
            current += 1
            current_loss += item["pnl"]
            if current > longest:
                longest, worst_loss, end_key = current, current_loss, item["event_key"]
        else:
            current, current_loss = 0, 0.0
    return {"events": longest, "cumulative_pnl": round(worst_loss, 6), "ending_event": list(end_key) if end_key else None}


def history_inventory(repo_root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for relative in HISTORY_SOURCE_FILES:
        path = repo_root / relative
        inventory.append(
            {
                "path": relative,
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "columns": csv_header(path),
                "read_policy": "STREAM_ONLY_NOT_USED" if relative.endswith("exit_rule_position_detail_v4.csv") else "HEADER_ONLY_OR_STREAMED",
            }
        )
    return inventory


def _load_history_positions(repo_root: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    lifecycle_path = repo_root / "data/processed/weather_position_lifecycle.csv"
    positions: list[dict[str, Any]] = []
    fields = {
        "asset", "city", "weather_date", "weather_metric", "bucket_label", "bucket_kind",
        "bucket_low", "bucket_high", "buy_count", "buy_shares", "buy_usd", "sell_count",
        "sell_shares", "sell_usd", "weighted_avg_buy_price", "weighted_avg_sell_price",
        "authoritative_realized_pnl", "pnl_status", "exit_mode", "first_buy_utc",
    }
    for chunk in iter_csv_chunks(lifecycle_path, fields, 5000):
        for raw in chunk:
            buy_shares = fnum(raw["buy_shares"], 0.0)
            sell_shares = fnum(raw["sell_shares"], 0.0)
            position = dict(raw)
            for field in (
                "bucket_low", "bucket_high", "buy_shares", "buy_usd", "sell_shares",
                "sell_usd", "weighted_avg_buy_price", "weighted_avg_sell_price",
                "authoritative_realized_pnl",
            ):
                position[field] = fnum(raw.get(field))
            position["buy_count"] = int(fnum(raw.get("buy_count"), 0))
            position["sell_count"] = int(fnum(raw.get("sell_count"), 0))
            position["sold_fraction"] = min(max(sell_shares / buy_shares, 0.0), 1.0) if buy_shares > 0 else math.nan
            position["remaining_fraction"] = max(1.0 - position["sold_fraction"], 0.0) if math.isfinite(position["sold_fraction"]) else math.nan
            position["event_key"] = event_key(raw)
            positions.append(position)
    first_buy: dict[str, tuple[int, float]] = {}
    trade_path = repo_root / "data/processed/weather_trades_normalized.csv"
    for chunk in iter_csv_chunks(trade_path, {"asset", "side", "timestamp", "price"}, 10000):
        for row in chunk:
            if str(row["side"]).upper() != "BUY":
                continue
            timestamp = int(fnum(row["timestamp"], 0))
            price = fnum(row["price"])
            asset = row["asset"]
            if asset and math.isfinite(price) and (asset not in first_buy or timestamp < first_buy[asset][0]):
                first_buy[asset] = (timestamp, price)
    return positions, {asset: value[1] for asset, value in first_buy.items()}


def _historical_exit_rule_comparison(repo_root: Path) -> dict[str, Any]:
    """Read the existing v4 event-aware 70/30 rule grid without re-fitting it."""
    path = repo_root / "data/exit_rule_grid_v4.csv"
    if not path.exists():
        return {"status": "NOT_SUPPORTED_BY_AVAILABLE_DATA", "rules": {}}
    wanted = {
        "rule_id", "split", "price_scenario", "group_type", "positions", "events",
        "net_pnl", "roi_on_buy_usd", "delta_vs_hold_pnl",
        "leave_top1_out_net_pnl", "leave_top5_out_net_pnl",
    }
    rule_names = {
        "hold_to_settlement": "HOLD",
        "tp_2_0x_sell_50pct": "DOUBLE_SELL_50",
        "tp_2_0x_sell_75pct": "DOUBLE_SELL_75",
    }
    rules: dict[str, dict[str, Any]] = defaultdict(dict)
    for chunk in iter_csv_chunks(path, wanted, 1000):
        for row in chunk:
            public_name = rule_names.get(row["rule_id"])
            if not public_name or row["group_type"] != "all_yes":
                continue
            scenario = row["price_scenario"]
            split = row["split"]
            if scenario not in {"sampled_1_0", "haircut_0_8"}:
                continue
            rules[public_name][f"{split}_{scenario}"] = {
                "positions": int(fnum(row["positions"], 0)),
                "events": int(fnum(row["events"], 0)),
                "net_pnl": fnum(row["net_pnl"]),
                "roi_on_buy_usd": fnum(row["roi_on_buy_usd"]),
                "delta_vs_hold_pnl": fnum(row["delta_vs_hold_pnl"]),
                "leave_top1_out_net_pnl": fnum(row["leave_top1_out_net_pnl"]),
                "leave_top5_out_net_pnl": fnum(row["leave_top5_out_net_pnl"]),
            }
    for name, result in rules.items():
        validation = result.get("validation_sampled_1_0", {})
        haircut = result.get("validation_haircut_0_8", {})
        if name == "HOLD":
            result["out_of_sample_result"] = "HOLD_COMPARATOR_BASELINE"
        elif validation.get("delta_vs_hold_pnl", 0) > 0 and haircut.get("delta_vs_hold_pnl", 0) > 0:
            result["out_of_sample_result"] = "VALIDATED_OUT_OF_SAMPLE_WITH_0_8_HAIRCUT"
        else:
            result["out_of_sample_result"] = "NOT_VALIDATED_OUT_OF_SAMPLE"
    return {
        "status": "EXISTING_V4_GRID_READ_ONLY",
        "split": "chronological train/validation already fixed in v4; no parameters changed here",
        "rules": dict(rules),
    }


def analyze_history(repo_root: Path) -> dict[str, Any]:
    inventory = history_inventory(repo_root)
    positions, first_buy_prices = _load_history_positions(repo_root)
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        by_event[position["event_key"]].append(position)
    events: list[dict[str, Any]] = []
    for key, rows in by_event.items():
        pnls = [row["authoritative_realized_pnl"] for row in rows if math.isfinite(row["authoritative_realized_pnl"])]
        events.append({"event_key": key, "positions": len(rows), "pnl": sum(pnls), "pnl_supported": bool(pnls)})
    events.sort(key=lambda row: (row["event_key"][1], row["event_key"][0], row["event_key"][2]))
    split_index = math.floor(len(events) * 0.7)
    train_keys = {row["event_key"] for row in events[:split_index]}
    validation_keys = {row["event_key"] for row in events[split_index:]}

    first_prices = [first_buy_prices[row["asset"]] for row in positions if row["asset"] in first_buy_prices]
    stake_ratios: list[float] = []
    positions_per_event = [len(rows) for rows in by_event.values()]
    for rows in by_event.values():
        total = sum(row["buy_usd"] for row in rows if math.isfinite(row["buy_usd"]))
        if total > 0:
            stake_ratios.extend(row["buy_usd"] / total for row in rows if math.isfinite(row["buy_usd"]))
    sold = [row for row in positions if row["sell_count"] > 0 and math.isfinite(row["sold_fraction"])]
    exit_multiples = [
        row["weighted_avg_sell_price"] / row["weighted_avg_buy_price"]
        for row in sold
        if math.isfinite(row["weighted_avg_sell_price"]) and math.isfinite(row["weighted_avg_buy_price"]) and row["weighted_avg_buy_price"] > 0
    ]
    partial_sold = [row for row in sold if row["sold_fraction"] < 0.999999]
    added = [row for row in positions if row["buy_count"] > 1]
    no_sell = [row for row in positions if row["sell_count"] == 0]
    event_pnls = [row["pnl"] for row in events if row["pnl_supported"]]
    winners = sorted((row for row in events if row["pnl"] > 0), key=lambda row: row["pnl"], reverse=True)
    gross_positive = sum(row["pnl"] for row in winners)
    total_pnl = sum(event_pnls)
    remove_top_1 = total_pnl - sum(row["pnl"] for row in winners[:1])
    remove_top_5 = total_pnl - sum(row["pnl"] for row in winners[:5])
    top_share = winners[0]["pnl"] / gross_positive if winners and gross_positive else None

    def adjacent(rows: list[dict[str, Any]]) -> bool:
        intervals = sorted(
            (
                -math.inf if not math.isfinite(row["bucket_low"]) else row["bucket_low"],
                math.inf if not math.isfinite(row["bucket_high"]) else row["bucket_high"],
            )
            for row in rows
        )
        return len(intervals) > 1 and all(right[0] - left[1] <= 1.000001 for left, right in zip(intervals, intervals[1:]))

    adjacent_events = {key for key, rows in by_event.items() if adjacent(rows)}
    multi_events = {key for key, rows in by_event.items() if len(rows) > 1}
    main_with_adjacent_events: set[tuple[str, str, str]] = set()
    for key, rows in by_event.items():
        if len(rows) < 2:
            continue
        main = max(rows, key=lambda row: row["buy_usd"] if math.isfinite(row["buy_usd"]) else -math.inf)
        if any(row is not main and adjacent([main, row]) for row in rows):
            main_with_adjacent_events.add(key)
    partial_events = {row["event_key"] for row in partial_sold}
    doubled_exit_events = {
        row["event_key"]
        for row in sold
        if math.isfinite(row["weighted_avg_sell_price"])
        and math.isfinite(row["weighted_avg_buy_price"])
        and row["weighted_avg_buy_price"] > 0
        and row["weighted_avg_sell_price"] / row["weighted_avg_buy_price"] >= 2
    }
    hold_events = {key for key, rows in by_event.items() if all(row["sell_count"] == 0 for row in rows)}

    def rate(keys: set[tuple[str, str, str]], base: set[tuple[str, str, str]]) -> float | None:
        return len(keys & base) / len(base) if base else None

    hypotheses = [
        {
            "id": "H1_ADJACENT_BASKET",
            "classification": "INFERRED",
            "statement": "Husky often combines adjacent temperature buckets within one weather event.",
            "train_rate": rate(adjacent_events, train_keys),
            "validation_rate": rate(adjacent_events, validation_keys),
        },
        {
            "id": "H2_PARTIAL_EXIT_AT_2X",
            "classification": "INFERRED",
            "statement": "Partial exits and exits around/above 2x are candidates for forward testing, not proven rules.",
            "train_rate": rate(partial_events & doubled_exit_events, train_keys),
            "validation_rate": rate(partial_events & doubled_exit_events, validation_keys),
        },
        {
            "id": "H3_HOLD_TO_SETTLEMENT",
            "classification": "INFERRED",
            "statement": "Holding all recorded shares without a pre-resolution sell is a recurring behavior.",
            "train_rate": rate(hold_events, train_keys),
            "validation_rate": rate(hold_events, validation_keys),
        },
    ]
    for item in hypotheses:
        vrate = item["validation_rate"]
        item["out_of_sample_result"] = "VALIDATED_AS_RECURRING_BEHAVIOR" if vrate is not None and vrate >= 0.10 else "NOT_VALIDATED_OUT_OF_SAMPLE"
        item["profitability_claim"] = "NOT_SUPPORTED_BY_AVAILABLE_DATA"
    exit_comparison = _historical_exit_rule_comparison(repo_root)
    double_results = [
        exit_comparison.get("rules", {}).get(name, {}).get("out_of_sample_result")
        for name in ("DOUBLE_SELL_50", "DOUBLE_SELL_75")
    ]
    if any(result == "NOT_VALIDATED_OUT_OF_SAMPLE" for result in double_results):
        hypotheses[1]["out_of_sample_result"] = "NOT_VALIDATED_OUT_OF_SAMPLE"
    hypotheses[2]["out_of_sample_result"] = "BASELINE_BEHAVIOR_ONLY_NOT_PROFITABILITY_VALIDATION"

    slippage = {}
    total_shares = sum(row["buy_shares"] for row in positions if math.isfinite(row["buy_shares"]))
    for cents in (0, 1, 2):
        slippage[f"plus_{cents}c"] = round(total_pnl - total_shares * cents / 100, 6)
    beijing_events = {key for key in by_event if key[0].strip().lower() == "beijing"}
    observed = [
        f"{len(events)} weather events and {len(positions)} YES positions are directly recorded after normalization.",
        f"{len(multi_events)} events contain more than one purchased temperature bucket; {len(adjacent_events)} are adjacent by normalized interval.",
        f"{len(added)} positions have more than one BUY record; {len(partial_sold)} positions have a recorded partial sell.",
        f"{len(no_sell)} positions have no recorded SELL in the available trade window.",
    ]
    not_supported = [
        "Husky's private intent, psychology, unpublished bankroll, or fixed decision rule.",
        "A historical weather-probability edge at entry because no contemporaneous Husky weather probability series is available.",
        "ZBAA station identity for Beijing history when the historical records identify only the city/market.",
        "A causal or profitable strategy rule from descriptive historical behavior.",
        "Whether every position with no recorded SELL was actually held through final settlement.",
        "Husky's intended stake when only actual fills/cash flows were recorded.",
    ]
    return {
        "version": VERSION,
        "inventory": inventory,
        "field_mapping": HISTORY_FIELD_MAP,
        "sample": {
            "weather_event_count": len(events),
            "position_count": len(positions),
            "beijing_event_count": len(beijing_events),
            "zbaa_station_confirmed_event_count": 0,
            "time_split": {"train_event_count": len(train_keys), "validation_event_count": len(validation_keys), "rule": "chronological 70/30 by weather event"},
        },
        "statistics": {
            "positions_per_event": {
                "min": min(positions_per_event) if positions_per_event else 0,
                "median": statistics.median(positions_per_event) if positions_per_event else None,
                "max": max(positions_per_event) if positions_per_event else 0,
                "distribution": dict(sorted(Counter(positions_per_event).items())),
            },
            "multi_bucket_event_count": len(multi_events),
            "adjacent_bucket_event_count": len(adjacent_events),
            "main_bucket_with_adjacent_event_count": len(main_with_adjacent_events),
            "first_buy_price": {
                "supported_positions": len(first_prices),
                "min": min(first_prices) if first_prices else None,
                "median": statistics.median(first_prices) if first_prices else None,
                "p90": percentile(first_prices, 0.90),
                "max": max(first_prices) if first_prices else None,
            },
            "stake_fraction_per_bucket": {
                "supported_positions": len(stake_ratios),
                "median": statistics.median(stake_ratios) if stake_ratios else None,
                "p10": percentile(stake_ratios, 0.10),
                "p90": percentile(stake_ratios, 0.90),
            },
            "added_buy_position_count": len(added),
            "no_recorded_sell_position_count": len(no_sell),
            "partial_sell_position_count": len(partial_sold),
            "sold_fraction": {
                "median": statistics.median(row["sold_fraction"] for row in sold) if sold else None,
                "p10": percentile([row["sold_fraction"] for row in sold], 0.10),
                "p90": percentile([row["sold_fraction"] for row in sold], 0.90),
            },
            "sell_to_buy_price_multiple": {
                "supported_positions": len(exit_multiples),
                "median": statistics.median(exit_multiples) if exit_multiples else None,
                "p90": percentile(exit_multiples, 0.90),
                "at_or_above_2x_count": sum(value >= 2 for value in exit_multiples),
            },
            "event_pnl": {
                "supported_events": len(event_pnls),
                "total": round(total_pnl, 6),
                "bootstrap_mean_95pct_ci": bootstrap_mean_ci(event_pnls),
                "top_event_share_of_gross_positive": top_share,
                "remove_top_1": round(remove_top_1, 6),
                "remove_top_5": round(remove_top_5, 6),
                "maximum_consecutive_loss": max_consecutive_loss(events),
                "slippage_sensitivity_assumption": slippage,
            },
        },
        "observed": observed,
        "hypotheses": hypotheses,
        "historical_exit_rule_comparison": exit_comparison,
        "not_supported": not_supported,
        "out_of_sample_result": [item["out_of_sample_result"] for item in hypotheses],
        "general_husky_hypothesis_only": True,
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "NOT_SUPPORTED_BY_AVAILABLE_DATA"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_history_report(analysis: dict[str, Any]) -> str:
    sample = analysis["sample"]
    stats = analysis["statistics"]
    lines = [
        "# HUSKY STRATEGY EVIDENCE FAST v1",
        "",
        "## Scope and safeguards",
        "",
        "This is a descriptive study of recorded behavior. It does not use Husky history to create ZBAA weather probabilities and does not claim a strategy is profitable.",
        "",
        "Weather-event identity is always `city + weather_date_local + weather_metric`; temperature buckets inside one event are not treated as independent experiments.",
        "",
        "## Data directory and field mapping",
        "",
        "| File | Bytes | Columns inspected | Read policy |",
        "|---|---:|---|---|",
    ]
    for item in analysis["inventory"]:
        columns = ", ".join(item["columns"]) if item["columns"] else "MISSING"
        lines.append(f"| `{item['path']}` | {item['size_bytes']} | {columns} | {item['read_policy']} |")
    lines.extend(["", "Logical field mapping:"])
    for logical, physical in analysis["field_mapping"].items():
        lines.append(f"- `{logical}` ← {', '.join(physical)}")
    lines.extend(
        [
            "",
            "## Sample size",
            "",
            f"- Weather events: {sample['weather_event_count']}",
            f"- Positions: {sample['position_count']}",
            f"- Beijing events: {sample['beijing_event_count']}",
            "- Station-confirmed ZBAA historical events: 0 (`NOT_SUPPORTED_BY_AVAILABLE_DATA`; old records do not prove station identity)",
            f"- Chronological split: first {sample['time_split']['train_event_count']} events (70%) for observation; final {sample['time_split']['validation_event_count']} events (30%) for independent checking.",
            "",
            "## OBSERVED",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in analysis["observed"])
    lines.extend(
        [
            f"- Positions per event: median {_fmt(stats['positions_per_event']['median'])}, range {stats['positions_per_event']['min']}–{stats['positions_per_event']['max']}.",
            f"- Main bucket (largest recorded event stake) has at least one adjacent purchased bucket in {stats['main_bucket_with_adjacent_event_count']} events.",
            f"- First BUY price: n={stats['first_buy_price']['supported_positions']}, median {_fmt(stats['first_buy_price']['median'])}, p90 {_fmt(stats['first_buy_price']['p90'])}.",
            f"- Per-bucket event stake fraction: median {_fmt(stats['stake_fraction_per_bucket']['median'])}, p10–p90 {_fmt(stats['stake_fraction_per_bucket']['p10'])}–{_fmt(stats['stake_fraction_per_bucket']['p90'])}.",
            f"- Recorded sold fraction: median {_fmt(stats['sold_fraction']['median'])}; weighted sell/buy multiple median {_fmt(stats['sell_to_buy_price_multiple']['median'])}.",
            f"- Event PnL total: {_fmt(stats['event_pnl']['total'], 2)}; bootstrap 95% interval for mean event PnL: {stats['event_pnl']['bootstrap_mean_95pct_ci']}.",
            f"- Largest winner share of gross positive PnL: {_fmt(stats['event_pnl']['top_event_share_of_gross_positive'])}.",
            f"- PnL after removing top 1 / top 5 positive events: {_fmt(stats['event_pnl']['remove_top_1'], 2)} / {_fmt(stats['event_pnl']['remove_top_5'], 2)}.",
            f"- Maximum consecutive losing events: {stats['event_pnl']['maximum_consecutive_loss']}.",
            "",
            "## INFERRED — at most three forward hypotheses",
            "",
        ]
    )
    for item in analysis["hypotheses"]:
        lines.extend(
            [
                f"### {item['id']}",
                "",
                item["statement"],
                "",
                f"Train rate: {_fmt(item['train_rate'])}; held-out rate: {_fmt(item['validation_rate'])}. Result: `{item['out_of_sample_result']}`. Profitability: `NOT_SUPPORTED_BY_AVAILABLE_DATA`.",
                "",
            ]
        )
    lines.extend(
        [
            "## Fixed exit-rule held-out comparison",
            "",
            "The existing v4 grid is read-only evidence. Its chronological train/validation split and fixed 2x rules are reused without tuning. Positive sampled PnL alone is not treated as validation when the 0.8 executable-price haircut fails versus HOLD.",
            "",
            "| Rule | Validation sampled net PnL | Delta vs HOLD | Validation 0.8-haircut net PnL | 0.8 delta vs HOLD | Result |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for name in ("HOLD", "DOUBLE_SELL_50", "DOUBLE_SELL_75"):
        rule = analysis["historical_exit_rule_comparison"].get("rules", {}).get(name, {})
        sampled = rule.get("validation_sampled_1_0", {})
        haircut = rule.get("validation_haircut_0_8", {})
        lines.append(
            f"| {name} | {_fmt(sampled.get('net_pnl'), 2)} | {_fmt(sampled.get('delta_vs_hold_pnl'), 2)} | "
            f"{_fmt(haircut.get('net_pnl'), 2)} | {_fmt(haircut.get('delta_vs_hold_pnl'), 2)} | "
            f"`{rule.get('out_of_sample_result', 'NOT_SUPPORTED_BY_AVAILABLE_DATA')}` |"
        )
    lines.append("")
    lines.extend(["## NOT_SUPPORTED", ""])
    lines.extend(f"- {item}: `NOT_SUPPORTED_BY_AVAILABLE_DATA`" for item in analysis["not_supported"])
    lines.extend(
        [
            "",
            "## Slippage sensitivity",
            "",
            "No entry-time historical order books are available. The figures below are mechanical sensitivity assumptions, not observed fills:",
            "",
            f"`{stats['event_pnl']['slippage_sensitivity_assumption']}`",
            "",
            "## Overfitting risk",
            "",
            "The same account produced all observations, outcomes are clustered within weather events, station identity is often absent, and exploratory history cannot prove future profitability. Parameters for the shadow phase are therefore fixed before results: EDGE_05/10/15, MAIN_ONLY, TOP2_ADJACENT 70/30, HOLD, DOUBLE_SELL_50, and DOUBLE_SELL_75.",
            "",
        ]
    )
    if analysis["general_husky_hypothesis_only"]:
        lines.extend(["`GENERAL_HUSKY_HYPOTHESIS_ONLY`: ZBAA-specific history is insufficient; only future ZBAA shadow samples can validate these candidates.", ""])
    return "\n".join(lines)


def write_history_outputs(repo_root: Path, output_path: Path) -> dict[str, Any]:
    analysis = analyze_history(repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_history_report(analysis), encoding="utf-8")
    summary_path = output_path.with_suffix(".json")
    summary_path.write_text(json.dumps(json_safe(analysis), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return analysis


def probability_for_bucket(probabilities: list[dict[str, Any]], bucket_type: str, threshold: Decimal) -> Decimal:
    value = int(threshold)
    if threshold != Decimal(value):
        raise ValidationError("market bucket threshold must be an integer Celsius value")
    mapping = {row["temperature_c"]: row["probability"] for row in probabilities}
    if value not in mapping:
        raise ValidationError(f"manual probability range does not explicitly cover market threshold {value}C")
    if bucket_type == "exact":
        return mapping[value]
    if bucket_type == "or_below":
        return sum((probability for temp, probability in mapping.items() if temp <= value), Decimal("0"))
    if bucket_type == "or_higher":
        return sum((probability for temp, probability in mapping.items() if temp >= value), Decimal("0"))
    raise ValidationError(f"unsupported temperature bucket type: {bucket_type}")


def _yes_token(market: dict[str, Any], clob: dict[str, Any]) -> str:
    for pair in gamma_token_pairs(market):
        if pair["outcome"].strip().lower() == "yes":
            return pair["token_id"]
    tokens = clob.get("t") if isinstance(clob.get("t"), list) else clob.get("tokens")
    for item in tokens or []:
        outcome = str(item.get("o") or item.get("outcome") or "").lower()
        if outcome == "yes":
            return str(item.get("t") or item.get("token_id") or "")
    return ""


def normalize_evidence_record(record: dict[str, Any], probability_input: dict[str, Any]) -> dict[str, Any]:
    gamma = record.get("gamma") if isinstance(record.get("gamma"), dict) else record.get("market")
    clob = record.get("clob") if isinstance(record.get("clob"), dict) else {}
    raw_book = record.get("orderbook")
    if isinstance(raw_book, dict) and isinstance(raw_book.get("http"), dict):
        raw_book = raw_book["http"].get("payload")
    if not isinstance(gamma, dict) or not isinstance(raw_book, dict):
        raise ValidationError("saved evidence record requires gamma market and orderbook objects")
    parsed = parse_weather_market(gamma, str(gamma.get("title") or ""))
    if parsed["parsing_status"] != "ok":
        raise ValidationError(f"market parsing failed: {parsed['parsing_errors']}")
    if parsed["city"] != CITY:
        raise ValidationError(f"non-Beijing market rejected: {parsed['city']}")
    if parsed["weather_date_local"] != probability_input["weather_date_local"]:
        raise ValidationError(f"market date mismatch: {parsed['weather_date_local']}")
    if parsed["weather_metric"] != MARKET_METRIC:
        raise ValidationError(f"market metric mismatch: {parsed['weather_metric']}")
    if parsed["unit"] != "C":
        raise ValidationError("only Celsius markets are supported")
    condition_id = str(gamma.get("conditionId") or gamma.get("condition_id") or clob.get("c") or clob.get("condition_id") or "")
    token_id = _yes_token(gamma, clob)
    if not condition_id or not token_id:
        raise ValidationError("condition_id and YES token binding are required")
    normalized_book = normalize_orderbook(raw_book, token_id, condition_id, gamma)
    forecast_probability = probability_for_bucket(
        probability_input["integer_temperature_probabilities"],
        parsed["bucket_type"],
        decimal(parsed["threshold_value"], "threshold_value"),
    )
    return {
        "gamma": gamma,
        "clob": clob,
        "raw_orderbook": raw_book,
        "market_slug": str(gamma.get("slug") or ""),
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": "Yes",
        "bucket_type": parsed["bucket_type"],
        "threshold_value": decimal(parsed["threshold_value"]),
        "temperature_bucket": parsed["canonical_label"],
        "forecast_probability": forecast_probability,
        "book": normalized_book,
        "captured_at_utc": str(record.get("captured_at_utc") or raw_book.get("timestamp") or ""),
    }


def _walk_market_objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if (value.get("conditionId") or value.get("condition_id")) and (value.get("question") or value.get("slug")):
            yield value
        for child in value.values():
            yield from _walk_market_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_market_objects(child)


def discover_live_evidence(probability_input: dict[str, Any], adapter: PublicAdapter | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    adapter = adapter or PublicAdapter(timeout_seconds=10, max_retries=1)
    query = f"highest temperature in Beijing on {probability_input['weather_date_local']}"
    search = adapter.search(query, limit_per_type=50, events_status="active", keep_closed_markets=0)
    candidates: dict[str, dict[str, Any]] = {}
    for market in _walk_market_objects(search.payload):
        condition = str(market.get("conditionId") or market.get("condition_id") or "")
        if condition:
            candidates[condition] = market
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for condition_id, market in candidates.items():
        parsed = parse_weather_market(market, str(market.get("title") or ""))
        if parsed["city"] != CITY or parsed["weather_date_local"] != probability_input["weather_date_local"] or parsed["weather_metric"] != MARKET_METRIC:
            continue
        try:
            clob_result = adapter.clob_market_info(condition_id)
            token_id = _yes_token(market, clob_result.payload)
            if not token_id:
                raise ValidationError("YES token missing")
            book_result = adapter.orderbook(token_id)
            records.append(
                {
                    "gamma": market,
                    "clob": clob_result.payload,
                    "orderbook": book_result.payload,
                    "captured_at_utc": book_result.received_at_utc,
                    "source_urls": [search.url, clob_result.url, book_result.url],
                }
            )
        except (AdapterError, ValidationError) as exc:
            errors.append({"condition_id": condition_id, "error": str(exc)})
    return records, {
        "query": query,
        "search_url": search.url,
        "search_received_at_utc": search.received_at_utc,
        "candidate_count": len(candidates),
        "matched_count": len(records),
        "errors": errors,
        "visited_endpoints": adapter.visited_endpoints,
        "public_get_only": all(item["method"] == "GET" for item in adapter.visited_endpoints),
    }


def load_saved_evidence(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValidationError("saved public evidence must contain a markets array")
    return records, {
        "evidence_path": str(path),
        "evidence_label": str(payload.get("evidence_label") or "saved-public-evidence"),
        "captured_at_utc": payload.get("captured_at_utc"),
        "fixture": bool(payload.get("fixture", False)),
        "public_get_only": True,
    }


def evaluate_market(market: dict[str, Any], intended_usd: Decimal) -> dict[str, Any]:
    book = market["book"]
    best_ask = book["best_ask"]
    if best_ask is None:
        fill = {
            "status": "no_ask",
            "filled_shares": Decimal("0"),
            "filled_usd": Decimal("0"),
            "remaining_usd": intended_usd,
            "vwap": None,
            "levels": [],
        }
    else:
        fill = consume_buy_depth(book, intended_usd, Decimal("1"))
    vwap = fill["vwap"]
    edge = market["forecast_probability"] - vwap if vwap is not None else None
    return {
        "market_slug": market["market_slug"],
        "condition_id": market["condition_id"],
        "token_id": market["token_id"],
        "temperature_bucket": market["temperature_bucket"],
        "bucket_type": market["bucket_type"],
        "threshold_value": market["threshold_value"],
        "forecast_probability": market["forecast_probability"],
        "best_ask": best_ask,
        "intended_usd": intended_usd,
        "executable_usd": fill["filled_usd"],
        "executable_shares": fill["filled_shares"],
        "executable_average_price": vwap,
        "slippage": vwap - best_ask if vwap is not None and best_ask is not None else None,
        "unfilled_usd": fill["remaining_usd"],
        "executable_edge": edge,
        "orderbook_status": fill["status"],
        "thin_orderbook": fill["status"] != "filled",
        "consumed_levels": fill["levels"],
        "captured_at_utc": market["captured_at_utc"],
    }


def buckets_adjacent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = sorted((left, right), key=lambda item: item["threshold_value"])
    if a["bucket_type"] == "or_below":
        return b["bucket_type"] == "exact" and b["threshold_value"] == a["threshold_value"] + 1
    if b["bucket_type"] == "or_higher":
        return a["bucket_type"] == "exact" and b["threshold_value"] == a["threshold_value"] + 1
    return a["bucket_type"] == b["bucket_type"] == "exact" and b["threshold_value"] == a["threshold_value"] + 1


def build_portfolios(evaluations: list[dict[str, Any]], intended_usd: Decimal) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for edge_name, threshold in EDGE_THRESHOLDS.items():
        eligible = [
            item for item in evaluations
            if item["executable_edge"] is not None and item["executable_edge"] >= threshold and item["executable_usd"] > 0
        ]
        eligible.sort(key=lambda item: (item["executable_edge"], item["forecast_probability"]), reverse=True)
        edge_result: dict[str, Any] = {
            "threshold": threshold,
            "eligible_buckets": [item["temperature_bucket"] for item in eligible],
        }
        if eligible:
            edge_result["MAIN_ONLY"] = {
                "status": "TRADE",
                "allocations": [{"temperature_bucket": eligible[0]["temperature_bucket"], "fraction": Decimal("1"), "intended_usd": intended_usd}],
            }
        else:
            edge_result["MAIN_ONLY"] = {"status": "NO_TRADE", "allocations": []}
        if len(eligible) >= 2 and buckets_adjacent(eligible[0], eligible[1]):
            edge_result["TOP2_ADJACENT"] = {
                "status": "TRADE",
                "allocations": [
                    {"temperature_bucket": eligible[0]["temperature_bucket"], "fraction": Decimal("0.70"), "intended_usd": intended_usd * Decimal("0.70")},
                    {"temperature_bucket": eligible[1]["temperature_bucket"], "fraction": Decimal("0.30"), "intended_usd": intended_usd * Decimal("0.30")},
                ],
            }
        else:
            edge_result["TOP2_ADJACENT"] = {
                "status": "NO_TRADE",
                "reason": "top two eligible buckets are not adjacent" if len(eligible) >= 2 else "fewer than two eligible buckets",
                "allocations": [],
            }
        results[edge_name] = edge_result
    return results


def simulate_portfolio_fills(
    run_id: str,
    portfolios: dict[str, dict[str, Any]],
    market_by_bucket: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signals: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    for edge_name, edge_result in portfolios.items():
        for portfolio_name in PORTFOLIO_RULES:
            portfolio = edge_result[portfolio_name]
            for allocation in portfolio["allocations"]:
                market = market_by_bucket[allocation["temperature_bucket"]]
                fill = evaluate_market(market, allocation["intended_usd"])
                signal_id = build_signal_id(run_id, edge_name, portfolio_name, market["temperature_bucket"])
                signals.append(
                    {
                        "signal_id": signal_id,
                        "run_id": run_id,
                        "mode": "DEMO",
                        "edge_rule": edge_name,
                        "portfolio_rule": portfolio_name,
                        "weather_date_local": parse_weather_market(market["gamma"])["weather_date_local"],
                        "city": CITY,
                        "station": STATION,
                        "weather_metric": WEATHER_METRIC,
                        "temperature_bucket": market["temperature_bucket"],
                        "forecast_probability": market["forecast_probability"],
                        "condition_id": market["condition_id"],
                        "token_id": market["token_id"],
                        "side": "BUY",
                        "intended_usd": allocation["intended_usd"],
                        "created_at_utc": iso_utc(),
                    }
                )
                fills.append(
                    {
                        "signal_id": signal_id,
                        "run_id": run_id,
                        "edge_rule": edge_name,
                        "portfolio_rule": portfolio_name,
                        "temperature_bucket": market["temperature_bucket"],
                        "bucket_type": market["bucket_type"],
                        "threshold_value": market["threshold_value"],
                        "condition_id": market["condition_id"],
                        "token_id": market["token_id"],
                        "status": fill["orderbook_status"],
                        "filled_usd": fill["executable_usd"],
                        "filled_shares": fill["executable_shares"],
                        "entry_vwap": fill["executable_average_price"],
                        "unfilled_usd": fill["unfilled_usd"],
                        "best_ask": fill["best_ask"],
                        "executable_edge": fill["executable_edge"],
                    }
                )
    return signals, fills


def write_demo_ledger(path: Path, run_identity: dict[str, str], signals: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    if "formal" in {part.lower() for part in path.parts}:
        raise ValidationError("formal paths are rejected")
    if path.exists():
        raise ValidationError("demo ledger already exists; silent overwrite is forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE run_metadata(
              run_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
              probability_input_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_signals(
              signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES run_metadata(run_id)
            );
            CREATE TABLE IF NOT EXISTS demo_entry_fills(
              signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              token_id TEXT NOT NULL, payload_json TEXT NOT NULL,
              FOREIGN KEY(signal_id) REFERENCES demo_signals(signal_id)
            );
            CREATE TABLE IF NOT EXISTS demo_exit_experiments(
              run_id TEXT NOT NULL, signal_id TEXT NOT NULL,
              edge_rule TEXT NOT NULL, portfolio_rule TEXT NOT NULL,
              temperature_bucket TEXT NOT NULL, bucket_type TEXT NOT NULL,
              threshold_value TEXT NOT NULL, token_id TEXT NOT NULL,
              exit_rule TEXT NOT NULL, status TEXT NOT NULL,
              trigger_multiple TEXT, sell_fraction TEXT,
              invested_usd TEXT NOT NULL, entry_shares TEXT NOT NULL,
              entry_vwap TEXT, trigger_time_utc TEXT,
              target_sell_shares TEXT NOT NULL DEFAULT '0',
              filled_sell_shares TEXT NOT NULL DEFAULT '0',
              unfilled_sell_shares TEXT NOT NULL DEFAULT '0',
              executable_sell_vwap TEXT,
              simulated_proceeds TEXT NOT NULL DEFAULT '0',
              remaining_shares TEXT NOT NULL,
              realized_pnl_so_far TEXT NOT NULL DEFAULT '0',
              last_update_id TEXT,
              PRIMARY KEY(signal_id, exit_rule),
              FOREIGN KEY(signal_id) REFERENCES demo_signals(signal_id)
            );
            CREATE TABLE demo_update_snapshots(
              update_id TEXT NOT NULL, run_id TEXT NOT NULL,
              signal_id TEXT NOT NULL, exit_rule TEXT NOT NULL,
              captured_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(update_id, signal_id, exit_rule)
            );
            CREATE TABLE demo_settlements(
              run_id TEXT NOT NULL, signal_id TEXT NOT NULL,
              edge_rule TEXT NOT NULL, portfolio_rule TEXT NOT NULL,
              exit_rule TEXT NOT NULL, observed_max_temp_c INTEGER NOT NULL,
              invested_usd TEXT NOT NULL, realized_exit_proceeds TEXT NOT NULL,
              settlement_proceeds TEXT NOT NULL, total_proceeds TEXT NOT NULL,
              net_pnl TEXT NOT NULL, roi TEXT, settled_at_utc TEXT NOT NULL,
              PRIMARY KEY(signal_id, exit_rule)
            );
            CREATE TABLE demo_run_settlement(
              run_id TEXT PRIMARY KEY, observed_max_temp_c INTEGER NOT NULL,
              settled_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS safety_state(
              key TEXT PRIMARY KEY, value TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO run_metadata VALUES(?,?,?,?)",
            (
                run_identity["run_id"],
                run_identity["identity_key"],
                run_identity["probability_input_sha256"],
                stable_payload_json(run_identity),
            ),
        )
        for signal in signals:
            connection.execute(
                "INSERT INTO demo_signals VALUES(?,?,?)",
                (signal["signal_id"], run_identity["run_id"], stable_payload_json(signal)),
            )
        for fill in fills:
            connection.execute(
                "INSERT INTO demo_entry_fills VALUES(?,?,?,?)",
                (fill["signal_id"], run_identity["run_id"], fill["token_id"], stable_payload_json(fill)),
            )
            for exit_rule, trigger, fraction in (
                ("HOLD", None, None),
                ("DOUBLE_SELL_50", "2", "0.50"),
                ("DOUBLE_SELL_75", "2", "0.75"),
            ):
                connection.execute(
                    """
                    INSERT INTO demo_exit_experiments(
                      run_id,signal_id,edge_rule,portfolio_rule,temperature_bucket,
                      bucket_type,threshold_value,token_id,exit_rule,status,
                      trigger_multiple,sell_fraction,invested_usd,entry_shares,
                      entry_vwap,remaining_shares
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_identity["run_id"], fill["signal_id"], fill["edge_rule"],
                        fill["portfolio_rule"], fill["temperature_bucket"], fill["bucket_type"],
                        dstr(fill["threshold_value"]), fill["token_id"], exit_rule, "OPEN",
                        trigger, fraction, dstr(fill["filled_usd"]), dstr(fill["filled_shares"]),
                        None if fill["entry_vwap"] is None else dstr(fill["entry_vwap"]),
                        dstr(fill["filled_shares"]),
                    ),
                )
        for key, value in FORMAL_ZERO_STATUS.items():
            connection.execute("INSERT INTO safety_state VALUES(?,?)", (key, json.dumps(value)))
        connection.commit()
        demo_signal_count = connection.execute("SELECT COUNT(*) FROM demo_signals").fetchone()[0]
        demo_fill_count = connection.execute("SELECT COUNT(*) FROM demo_entry_fills").fetchone()[0]
        demo_exit_experiment_count = connection.execute("SELECT COUNT(*) FROM demo_exit_experiments").fetchone()[0]
    return {
        "path": str(path),
        "run_id": run_identity["run_id"],
        "demo_signal_count": demo_signal_count,
        "demo_entry_fill_count": demo_fill_count,
        "demo_exit_experiment_count": demo_exit_experiment_count,
        **FORMAL_ZERO_STATUS,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([{key: dstr(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows])


def _market_report_rows(evaluations: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for item in evaluations:
        avg = "无可成交卖单" if item["executable_average_price"] is None else f"{float(item['executable_average_price']):.4f}"
        edge = "无法计算" if item["executable_edge"] is None else f"{float(item['executable_edge']):+.4f}"
        rows.extend(
            [
                f"- 温度档：{item['temperature_bucket']}",
                f"  天气概率：{float(item['forecast_probability']):.1%}",
                f"  市场 best ask：{'无' if item['best_ask'] is None else f'{float(item['best_ask']):.4f}'}",
                f"  实际可成交均价：{avg}",
                f"  价值差：{edge}",
                f"  盘口是否太薄：{'是' if item['thin_orderbook'] else '否'}（未成交 ${float(item['unfilled_usd']):.2f}）",
            ]
        )
    return rows


def render_daily_report(
    probability_input: dict[str, Any],
    evaluations: list[dict[str, Any]],
    portfolios: dict[str, Any],
    fills: list[dict[str, Any]],
    source_meta: dict[str, Any],
) -> str:
    main_probability = max(probability_input["integer_temperature_probabilities"], key=lambda row: row["probability"])
    trade_count = sum(1 for fill in fills if fill["filled_usd"] > 0)
    intended = sum((fill["filled_usd"] + fill["unfilled_usd"] for fill in fills), Decimal("0"))
    filled = sum((fill["filled_usd"] for fill in fills), Decimal("0"))
    unfilled = sum((fill["unfilled_usd"] for fill in fills), Decimal("0"))
    source_label = source_meta.get("evidence_label") or ("LIVE_READONLY" if source_meta.get("search_url") else "saved-public-evidence")
    return "\n".join(
        [
            "# ZBAA 每日影子模拟报告",
            "",
            f"证据来源：`{source_label}`。抓取/保存时间：`{source_meta.get('captured_at_utc') or source_meta.get('search_received_at_utc') or '见逐市场快照'}`。",
            "",
            "## 一、天气判断",
            "",
            f"明天北京首都机场最高温主档：{main_probability['temperature_c']}°C",
            f"程序概率：{float(main_probability['probability']):.1%}（该概率由用户手工输入，不是本程序生成）",
            f"置信度：{probability_input['confidence']}",
            f"主要原因：{probability_input['explanation']}",
            "",
            "## 二、市场判断",
            "",
            *_market_report_rows(evaluations),
            "",
            "“可能被低估”只表示：手工天气概率减去当前订单簿在指定金额下的实际模拟成交均价达到预设门槛。",
            "",
            "## 三、模拟动作",
            "",
            f"买或不买：共生成 {trade_count} 笔规则组合下的 DEMO 成交；没有符合条件的组合明确为 NO_TRADE。",
            f"买哪档：详见 `shadow_signals.csv` 及下方固定规则矩阵：`{json.dumps(json_safe(portfolios), ensure_ascii=False, sort_keys=True)}`",
            f"模拟投入（各实验组合合计，组合之间互为对照而非一个组合仓位）：${float(intended):.2f}",
            f"实际成交：${float(filled):.2f}",
            f"未成交：${float(unfilled):.2f}",
            "为什么：仅当 executable edge 达到 EDGE_05/10/15，并满足 MAIN_ONLY 或 TOP2_ADJACENT 的预注册结构时才模拟买入。",
            "",
            "## 四、退出实验",
            "",
            "HOLD：持有到最终结算；当前尚未结算，继续观察。",
            "DOUBLE_SELL_50：可成交卖价达到实际买入均价 2 倍时卖出 50%；当前为入场快照，继续观察。",
            "DOUBLE_SELL_75：可成交卖价达到实际买入均价 2 倍时卖出 75%；当前为入场快照，继续观察。",
            "",
            "## 五、风险提醒",
            "",
            "- 该结果为影子模拟，不是真实交易建议。",
            "- 天气概率由用户手工输入。",
            "- Husky 历史只用于提出策略假设，不用于生成天气概率或判断某温度档低估。",
            "- 规则是否有效必须依靠按天气事件统计的前向样本验证。",
            "- 本工具只执行公开 GET；不连接账户或钱包，不签名，不真实下单。",
            "",
        ]
    )


def run_shadow(
    probability_path: Path,
    intended_usd: Decimal,
    output_dir: Path,
    saved_public_evidence: Path | None = None,
    live_readonly: bool = False,
    adapter: PublicAdapter | None = None,
) -> dict[str, Any]:
    if intended_usd <= 0:
        raise ValidationError("intended_usd must be positive")
    if saved_public_evidence is None and not live_readonly:
        raise ValidationError("one of --saved-public-evidence or --live-readonly is required")
    if saved_public_evidence is not None and live_readonly:
        raise ValidationError("--saved-public-evidence and --live-readonly are mutually exclusive")
    if "formal" in {part.lower() for part in output_dir.parts}:
        raise ValidationError("formal output paths are rejected")
    probability_input = load_probability_input(probability_path)
    run_identity = build_run_identity(probability_input, intended_usd)
    existing = inspect_existing_run(output_dir, run_identity)
    if existing is not None:
        return existing
    output_dir.mkdir(parents=True, exist_ok=True)
    if live_readonly:
        records, source_meta = discover_live_evidence(probability_input, adapter)
        source_meta["fixture"] = False
        source_meta["evidence_label"] = "LIVE_READONLY"
    else:
        assert saved_public_evidence is not None
        records, source_meta = load_saved_evidence(saved_public_evidence)
    if not records:
        raise ValidationError("no matching Beijing highest-temperature markets found")
    markets = [normalize_evidence_record(record, probability_input) for record in records]
    bucket_names = [market["temperature_bucket"] for market in markets]
    if len(bucket_names) != len(set(bucket_names)):
        raise ValidationError("duplicate temperature bucket markets are rejected")
    token_ids = [market["token_id"] for market in markets]
    if len(token_ids) != len(set(token_ids)):
        raise ValidationError("duplicate YES token binding across markets is rejected")
    evaluations = [evaluate_market(market, intended_usd) for market in markets]
    portfolios = build_portfolios(evaluations, intended_usd)
    signals, fills = simulate_portfolio_fills(
        run_identity["run_id"],
        portfolios,
        {market["temperature_bucket"]: market for market in markets},
    )
    ledger = write_demo_ledger(output_dir / "demo_ledger.sqlite3", run_identity, signals, fills)

    orderbook_dir = output_dir / "orderbook_snapshots"
    orderbook_dir.mkdir(parents=True, exist_ok=True)
    for market in markets:
        safe_name = market["temperature_bucket"].replace(":", "_")
        (orderbook_dir / f"{safe_name}.json").write_text(
            json.dumps(json_safe({"raw": market["raw_orderbook"], "normalized": market["book"]}), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    market_snapshot = {
        "source": source_meta,
        "captured_at_utc": iso_utc(),
        "markets": evaluations,
    }
    decision_report = {
        "version": VERSION,
        "run_status": "CREATED",
        "run_identity": run_identity,
        "mode": "DEMO",
        "probability_input": probability_input,
        "markets": evaluations,
        "entry_rules": portfolios,
        "baseline": {"NO_TRADE": "No position, zero PnL baseline for every forward event"},
        "exit_rules": {rule: "继续观察" for rule in EXIT_RULES},
        "demo_ledger": ledger,
        "safety": {
            **FORMAL_ZERO_STATUS,
            "PUBLIC_GET_ONLY": PUBLIC_GET_ONLY,
            "ACCOUNT_CONNECTION": ACCOUNT_CONNECTION,
            "SIGNING": SIGNING,
            "REAL_ORDER": REAL_ORDER,
        },
    }
    manifest = {
        "version": VERSION,
        "run_identity": run_identity,
        "run_environment": "DEMO",
        "created_at_utc": iso_utc(),
        "probability_input_path": str(probability_path),
        "saved_public_evidence_path": str(saved_public_evidence) if saved_public_evidence else None,
        "live_readonly": live_readonly,
        "source": source_meta,
        "output_files": [
            "decision_report.json", "decision_report.md", "shadow_signals.csv",
            "market_snapshot.json", "orderbook_snapshots/", "run_manifest.json", "demo_ledger.sqlite3",
        ],
        "safety": decision_report["safety"],
    }
    (output_dir / "decision_report.json").write_text(json.dumps(json_safe(decision_report), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "market_snapshot.json").write_text(json.dumps(json_safe(market_snapshot), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "run_manifest.json").write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "decision_report.md").write_text(
        render_daily_report(probability_input, evaluations, portfolios, fills, source_meta),
        encoding="utf-8",
    )
    write_csv(
        output_dir / "shadow_signals.csv",
        signals,
        [
            "signal_id", "run_id", "mode", "edge_rule", "portfolio_rule", "weather_date_local",
            "city", "station", "weather_metric", "temperature_bucket",
            "forecast_probability", "condition_id", "token_id", "side",
            "intended_usd", "created_at_utc",
        ],
    )
    return decision_report


def load_run_context(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    if "formal" in {part.lower() for part in run_dir.parts}:
        raise ValidationError("formal run paths are rejected")
    manifest_path = run_dir / "run_manifest.json"
    report_path = run_dir / "decision_report.json"
    ledger_path = run_dir / "demo_ledger.sqlite3"
    if not manifest_path.exists() or not report_path.exists() or not ledger_path.exists():
        raise ValidationError("run directory is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    identity = manifest.get("run_identity") or {}
    if not identity.get("run_id") or report.get("run_identity", {}).get("run_id") != identity["run_id"]:
        raise ValidationError("run identity binding is missing or inconsistent")
    if manifest.get("run_environment") != "DEMO":
        raise ValidationError("only DEMO runs can be updated or settled")
    with sqlite3.connect(ledger_path) as connection:
        row = connection.execute(
            "SELECT identity_key,probability_input_sha256,payload_json FROM run_metadata WHERE run_id=?",
            (identity["run_id"],),
        ).fetchone()
    ledger_identity = json.loads(row[2]) if row else {}
    if (
        not row
        or row[0] != identity["identity_key"]
        or row[1] != identity["probability_input_sha256"]
        or ledger_identity.get("run_content_sha256") != identity.get("run_content_sha256")
    ):
        raise ValidationError("ledger run identity does not match manifest")
    return manifest, report, ledger_path


def _update_id() -> str:
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    return f"upd-{stamp}-{uuid4().hex[:8]}"


def update_shadow(
    run_dir: Path,
    saved_public_evidence: Path | None = None,
    live_readonly: bool = False,
    adapter: PublicAdapter | None = None,
) -> dict[str, Any]:
    if saved_public_evidence is None and not live_readonly:
        raise ValidationError("one of --saved-public-evidence or --live-readonly is required")
    if saved_public_evidence is not None and live_readonly:
        raise ValidationError("--saved-public-evidence and --live-readonly are mutually exclusive")
    manifest, report, ledger_path = load_run_context(run_dir)
    with sqlite3.connect(ledger_path) as state_connection:
        if state_connection.execute(
            "SELECT 1 FROM demo_run_settlement WHERE run_id=?",
            (manifest["run_identity"]["run_id"],),
        ).fetchone():
            raise ValidationError("settled run cannot be updated")
    probability_input = validate_probability_input(report["probability_input"])
    if live_readonly:
        records, source_meta = discover_live_evidence(probability_input, adapter)
        source_meta.update({"fixture": False, "evidence_label": "LIVE_READONLY"})
    else:
        assert saved_public_evidence is not None
        records, source_meta = load_saved_evidence(saved_public_evidence)
    markets = [normalize_evidence_record(record, probability_input) for record in records]
    if len({market["token_id"] for market in markets}) != len(markets):
        raise ValidationError("duplicate token records in update evidence are rejected")
    market_by_token = {market["token_id"]: market for market in markets}
    update_id = _update_id()
    captured_at = iso_utc()
    results: list[dict[str, Any]] = []
    with sqlite3.connect(ledger_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM demo_exit_experiments WHERE run_id=? ORDER BY signal_id,exit_rule",
            (manifest["run_identity"]["run_id"],),
        ).fetchall()
        if not rows:
            raise ValidationError("run has no DEMO exit experiments")
        run_tokens = {str(row["token_id"]) for row in rows}
        missing = sorted(run_tokens - set(market_by_token))
        if missing:
            raise ValidationError(f"update evidence is missing run token(s): {','.join(missing)}")
        for row in rows:
            market = market_by_token[str(row["token_id"])]
            book = market["book"]
            base = {
                "update_id": update_id,
                "run_id": row["run_id"],
                "signal_id": row["signal_id"],
                "edge_rule": row["edge_rule"],
                "portfolio_rule": row["portfolio_rule"],
                "temperature_bucket": row["temperature_bucket"],
                "exit_rule": row["exit_rule"],
                "captured_at_utc": captured_at,
                "best_bid": book["best_bid"],
                "best_ask": book["best_ask"],
                "orderbook_snapshot": book,
            }
            if row["exit_rule"] == "HOLD":
                result = {
                    **base,
                    "status": "OPEN",
                    "trigger_time_utc": None,
                    "target_sell_shares": Decimal("0"),
                    "filled_sell_shares": Decimal("0"),
                    "unfilled_sell_shares": Decimal("0"),
                    "executable_sell_vwap": None,
                    "simulated_proceeds": Decimal("0"),
                    "remaining_shares": decimal(row["remaining_shares"]),
                    "realized_pnl_so_far": decimal(row["realized_pnl_so_far"]),
                }
            elif row["status"] != "OPEN":
                result = {
                    **base,
                    "status": "REPEATED_EXIT_REJECTED",
                    "prior_status": row["status"],
                    "trigger_time_utc": row["trigger_time_utc"],
                    "target_sell_shares": decimal(row["target_sell_shares"]),
                    "filled_sell_shares": decimal(row["filled_sell_shares"]),
                    "unfilled_sell_shares": decimal(row["unfilled_sell_shares"]),
                    "executable_sell_vwap": None if row["executable_sell_vwap"] is None else decimal(row["executable_sell_vwap"]),
                    "simulated_proceeds": decimal(row["simulated_proceeds"]),
                    "remaining_shares": decimal(row["remaining_shares"]),
                    "realized_pnl_so_far": decimal(row["realized_pnl_so_far"]),
                }
            else:
                entry_shares = decimal(row["entry_shares"])
                entry_vwap = decimal(row["entry_vwap"])
                target = entry_shares * decimal(row["sell_fraction"])
                executable = consume_sell_depth(book, target)
                sell_vwap = executable["vwap"]
                threshold = entry_vwap * decimal(row["trigger_multiple"])
                full_target = executable["remaining_shares"] <= Decimal("0.00000001")
                triggered = full_target and sell_vwap is not None and sell_vwap >= threshold
                if triggered:
                    proceeds = executable["filled_usd"]
                    remaining = entry_shares - executable["filled_shares"]
                    realized_pnl = proceeds - executable["filled_shares"] * entry_vwap
                    connection.execute(
                        """
                        UPDATE demo_exit_experiments SET
                          status='TRIGGERED',trigger_time_utc=?,target_sell_shares=?,
                          filled_sell_shares=?,unfilled_sell_shares=?,
                          executable_sell_vwap=?,simulated_proceeds=?,
                          remaining_shares=?,realized_pnl_so_far=?,last_update_id=?
                        WHERE signal_id=? AND exit_rule=? AND status='OPEN'
                        """,
                        (
                            captured_at, dstr(target), dstr(executable["filled_shares"]),
                            dstr(executable["remaining_shares"]), dstr(sell_vwap), dstr(proceeds),
                            dstr(remaining), dstr(realized_pnl), update_id,
                            row["signal_id"], row["exit_rule"],
                        ),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise ValidationError("exit state changed concurrently; refusing duplicate sale")
                    status = "TRIGGERED"
                    filled_sell_shares = executable["filled_shares"]
                    simulated_proceeds = proceeds
                    remaining_shares = remaining
                    realized = realized_pnl
                    trigger_time = captured_at
                else:
                    status = "OPEN_NO_TRIGGER"
                    filled_sell_shares = Decimal("0")
                    simulated_proceeds = Decimal("0")
                    remaining_shares = entry_shares
                    realized = Decimal("0")
                    trigger_time = None
                    connection.execute(
                        """
                        UPDATE demo_exit_experiments SET
                          target_sell_shares=?,unfilled_sell_shares=?,
                          executable_sell_vwap=?,last_update_id=?
                        WHERE signal_id=? AND exit_rule=? AND status='OPEN'
                        """,
                        (
                            dstr(target), dstr(target),
                            None if sell_vwap is None else dstr(sell_vwap), update_id,
                            row["signal_id"], row["exit_rule"],
                        ),
                    )
                result = {
                    **base,
                    "status": status,
                    "trigger_threshold": threshold,
                    "trigger_time_utc": trigger_time,
                    "target_sell_shares": target,
                    "filled_sell_shares": filled_sell_shares,
                    "unfilled_sell_shares": target - filled_sell_shares,
                    "executable_depth_shares": executable["filled_shares"],
                    "executable_sell_vwap": sell_vwap,
                    "simulated_proceeds": simulated_proceeds,
                    "remaining_shares": remaining_shares,
                    "realized_pnl_so_far": realized,
                    "best_bid_only_would_trigger": book["best_bid"] is not None and book["best_bid"] >= threshold,
                }
            connection.execute(
                "INSERT INTO demo_update_snapshots VALUES(?,?,?,?,?,?)",
                (
                    update_id, row["run_id"], row["signal_id"], row["exit_rule"],
                    captured_at, stable_payload_json(result),
                ),
            )
            results.append(result)
        connection.commit()
    snapshot_dir = run_dir / "update_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{update_id}.json"
    if snapshot_path.exists():
        raise ValidationError("update snapshot collision; refusing overwrite")
    payload = {
        "version": VERSION,
        "update_id": update_id,
        "run_id": manifest["run_identity"]["run_id"],
        "captured_at_utc": captured_at,
        "source": source_meta,
        "ignored_non_run_tokens": sorted(set(market_by_token) - run_tokens),
        "results": results,
    }
    snapshot_path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def bucket_wins(bucket_type: str, threshold: Decimal, observed_max_temp_c: int) -> bool:
    observed = Decimal(observed_max_temp_c)
    if bucket_type == "exact":
        return observed == threshold
    if bucket_type == "or_below":
        return observed <= threshold
    if bucket_type == "or_higher":
        return observed >= threshold
    raise ValidationError(f"unsupported settlement bucket type: {bucket_type}")


def settle_shadow(run_dir: Path, observed_max_temp_c: int) -> dict[str, Any]:
    if isinstance(observed_max_temp_c, bool) or int(observed_max_temp_c) != observed_max_temp_c:
        raise ValidationError("observed-max-temp-c must be an integer")
    manifest, _report, ledger_path = load_run_context(run_dir)
    run_id = manifest["run_identity"]["run_id"]
    report_path = run_dir / "settlement_report.json"
    with sqlite3.connect(ledger_path) as connection:
        connection.row_factory = sqlite3.Row
        existing = connection.execute(
            "SELECT observed_max_temp_c FROM demo_run_settlement WHERE run_id=?",
            (run_id,),
        ).fetchall()
        if existing:
            temperatures = {int(row[0]) for row in existing}
            if temperatures == {int(observed_max_temp_c)}:
                if not report_path.exists():
                    raise ValidationError("settlement rows exist but settlement report is missing")
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                payload["settlement_status"] = "IDEMPOTENT_NOOP"
                return payload
            raise ValidationError(f"conflicting settlement: run already settled at {sorted(temperatures)}C")
        rows = connection.execute(
            "SELECT * FROM demo_exit_experiments WHERE run_id=? ORDER BY signal_id,exit_rule",
            (run_id,),
        ).fetchall()
        settled_at = iso_utc()
        positions: list[dict[str, Any]] = []
        for row in rows:
            invested = decimal(row["invested_usd"])
            realized_exit = decimal(row["simulated_proceeds"])
            remaining = decimal(row["remaining_shares"])
            won = bucket_wins(row["bucket_type"], decimal(row["threshold_value"]), int(observed_max_temp_c))
            settlement_proceeds = remaining if won else Decimal("0")
            total_proceeds = realized_exit + settlement_proceeds
            net_pnl = total_proceeds - invested
            roi = net_pnl / invested if invested > 0 else None
            position = {
                "run_id": run_id,
                "signal_id": row["signal_id"],
                "edge_rule": row["edge_rule"],
                "portfolio_rule": row["portfolio_rule"],
                "temperature_bucket": row["temperature_bucket"],
                "exit_rule": row["exit_rule"],
                "observed_max_temp_c": int(observed_max_temp_c),
                "bucket_won": won,
                "invested_usd": invested,
                "realized_exit_proceeds": realized_exit,
                "settlement_proceeds": settlement_proceeds,
                "total_proceeds": total_proceeds,
                "net_pnl": net_pnl,
                "roi": roi,
                "settled_at_utc": settled_at,
            }
            connection.execute(
                """
                INSERT INTO demo_settlements(
                  run_id,signal_id,edge_rule,portfolio_rule,exit_rule,
                  observed_max_temp_c,invested_usd,realized_exit_proceeds,
                  settlement_proceeds,total_proceeds,net_pnl,roi,settled_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, row["signal_id"], row["edge_rule"], row["portfolio_rule"],
                    row["exit_rule"], int(observed_max_temp_c), dstr(invested),
                    dstr(realized_exit), dstr(settlement_proceeds), dstr(total_proceeds),
                    dstr(net_pnl), None if roi is None else dstr(roi), settled_at,
                ),
            )
            connection.execute(
                "UPDATE demo_exit_experiments SET status='SETTLED' WHERE signal_id=? AND exit_rule=?",
                (row["signal_id"], row["exit_rule"]),
            )
            positions.append(position)
        connection.execute(
            "INSERT INTO demo_run_settlement VALUES(?,?,?)",
            (run_id, int(observed_max_temp_c), settled_at),
        )
        connection.commit()
    payload = {
        "version": VERSION,
        "settlement_status": "SETTLED",
        "run_id": run_id,
        "weather_date_local": manifest["run_identity"]["weather_date_local"],
        "observed_max_temp_c": int(observed_max_temp_c),
        "settled_at_utc": settled_at,
        "positions": positions,
        "safety": {
            **FORMAL_ZERO_STATUS,
            "PUBLIC_GET_ONLY": PUBLIC_GET_ONLY,
            "ACCOUNT_CONNECTION": ACCOUNT_CONNECTION,
            "SIGNING": SIGNING,
            "REAL_ORDER": REAL_ORDER,
        },
    }
    if report_path.exists():
        raise ValidationError("settlement report already exists without ledger settlement; refusing overwrite")
    report_path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _strategy_metrics(events: list[dict[str, Any]], settled_event_count: int) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: item["weather_date_local"])
    total_invested = sum((item["invested_usd"] for item in ordered), Decimal("0"))
    total_pnl = sum((item["net_pnl"] for item in ordered), Decimal("0"))
    winning = [item for item in ordered if item["net_pnl"] > 0]
    losing = [item for item in ordered if item["net_pnl"] < 0]
    streak = longest = 0
    for item in ordered:
        if item["net_pnl"] < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    positive = sorted((item["net_pnl"] for item in winning), reverse=True)
    gross_positive = sum(positive, Decimal("0"))
    return {
        "sample_status": "INSUFFICIENT_FORWARD_SAMPLE" if settled_event_count < 30 else "SUFFICIENT_FORWARD_SAMPLE",
        "settled_event_count": settled_event_count,
        "traded_event_count": sum(item["invested_usd"] > 0 for item in ordered),
        "total_invested": total_invested,
        "total_pnl": total_pnl,
        "roi": total_pnl / total_invested if total_invested > 0 else None,
        "winning_events": len(winning),
        "losing_events": len(losing),
        "maximum_consecutive_losses": longest,
        "largest_event_gain": max((item["net_pnl"] for item in winning), default=Decimal("0")),
        "largest_event_loss": min((item["net_pnl"] for item in losing), default=Decimal("0")),
        "top_winner_share": positive[0] / gross_positive if positive and gross_positive > 0 else None,
        "pnl_without_top_1": total_pnl - sum(positive[:1], Decimal("0")),
        "pnl_without_top_5": total_pnl - sum(positive[:5], Decimal("0")),
        "events": ordered,
    }


def summarize_shadow(runs_root: Path) -> dict[str, Any]:
    if "formal" in {part.lower() for part in runs_root.parts}:
        raise ValidationError("formal run roots are rejected")
    settled_runs: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    seen_events: set[tuple[str, str]] = set()
    for manifest_path in sorted(runs_root.rglob("run_manifest.json")):
        run_dir = manifest_path.parent
        manifest, _report, ledger_path = load_run_context(run_dir)
        run_id = manifest["run_identity"]["run_id"]
        with sqlite3.connect(ledger_path) as connection:
            connection.row_factory = sqlite3.Row
            run_settlement = connection.execute(
                "SELECT observed_max_temp_c,settled_at_utc FROM demo_run_settlement WHERE run_id=?",
                (run_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM demo_settlements WHERE run_id=? ORDER BY signal_id,exit_rule",
                (run_id,),
            ).fetchall()
        if not run_settlement:
            continue
        if run_id in seen_run_ids:
            raise ValidationError(f"duplicate settled run_id found: {run_id}")
        event_key_value = (
            manifest["run_identity"]["station"],
            manifest["run_identity"]["weather_date_local"],
        )
        if event_key_value in seen_events:
            raise ValidationError(f"multiple settled runs found for one weather event: {event_key_value}")
        seen_run_ids.add(run_id)
        seen_events.add(event_key_value)
        settled_runs.append({"manifest": manifest, "rows": rows, "run_dir": str(run_dir)})
    event_count = len(settled_runs)
    strategy_events: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    strategy_keys = [
        (edge, portfolio, exit_rule)
        for edge in EDGE_THRESHOLDS
        for portfolio in PORTFOLIO_RULES
        for exit_rule in EXIT_RULES
    ]
    for run in settled_runs:
        manifest = run["manifest"]
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in run["rows"]:
            grouped[(row["edge_rule"], row["portfolio_rule"], row["exit_rule"])].append(row)
        for key in strategy_keys:
            rows = grouped.get(key, [])
            strategy_events[key].append(
                {
                    "run_id": manifest["run_identity"]["run_id"],
                    "weather_date_local": manifest["run_identity"]["weather_date_local"],
                    "invested_usd": sum((decimal(row["invested_usd"]) for row in rows), Decimal("0")),
                    "net_pnl": sum((decimal(row["net_pnl"]) for row in rows), Decimal("0")),
                }
            )
    strategies: list[dict[str, Any]] = []
    for edge, portfolio, exit_rule in strategy_keys:
        strategies.append(
            {
                "edge_rule": edge,
                "portfolio_rule": portfolio,
                "exit_rule": exit_rule,
                **_strategy_metrics(strategy_events[(edge, portfolio, exit_rule)], event_count),
            }
        )
    no_trade_events = [
        {
            "run_id": run["manifest"]["run_identity"]["run_id"],
            "weather_date_local": run["manifest"]["run_identity"]["weather_date_local"],
            "invested_usd": Decimal("0"),
            "net_pnl": Decimal("0"),
        }
        for run in settled_runs
    ]
    strategies.append(
        {
            "edge_rule": "NO_TRADE",
            "portfolio_rule": "NO_TRADE",
            "exit_rule": "NO_TRADE",
            **_strategy_metrics(no_trade_events, event_count),
        }
    )
    payload = {
        "version": VERSION,
        "created_at_utc": iso_utc(),
        "settled_event_count": event_count,
        "aggregation_unit": "station + weather_date_local (one weather day = one event)",
        "strategies": strategies,
    }
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "forward_strategy_summary.json").write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md = [
        "# ZBAA Forward Strategy Summary",
        "",
        f"Settled weather events: {event_count}. One station/date is counted once.",
        "",
        "| Edge | Portfolio | Exit | Events | Traded | Invested | PnL | ROI | Sample |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in strategies:
        roi = "n/a" if item["roi"] is None else f"{float(item['roi']):.2%}"
        md.append(
            f"| {item['edge_rule']} | {item['portfolio_rule']} | {item['exit_rule']} | "
            f"{item['settled_event_count']} | {item['traded_event_count']} | "
            f"{float(item['total_invested']):.2f} | {float(item['total_pnl']):.2f} | "
            f"{roi} | {item['sample_status']} |"
        )
    md.extend(["", "All results are DEMO shadow outcomes, not trading advice.", ""])
    (runs_root / "forward_strategy_summary.md").write_text("\n".join(md), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Husky ZBAA fast shadow strategy lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    history = subparsers.add_parser("analyze-history")
    history.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    history.add_argument("--output", default="docs/HUSKY_STRATEGY_EVIDENCE_FAST_v1.md")
    shadow = subparsers.add_parser("run-shadow")
    shadow.add_argument("--probability-input", required=True)
    shadow.add_argument("--intended-usd", required=True)
    shadow.add_argument("--output-dir", required=True)
    source = shadow.add_mutually_exclusive_group(required=True)
    source.add_argument("--saved-public-evidence")
    source.add_argument("--live-readonly", action="store_true")
    shadow.add_argument("--mode", default="DEMO")
    update = subparsers.add_parser("update-shadow")
    update.add_argument("--run-dir", required=True)
    update_source = update.add_mutually_exclusive_group(required=True)
    update_source.add_argument("--saved-public-evidence")
    update_source.add_argument("--live-readonly", action="store_true")
    update.add_argument("--mode", default="DEMO")
    settle = subparsers.add_parser("settle-shadow")
    settle.add_argument("--run-dir", required=True)
    settle.add_argument("--observed-max-temp-c", required=True, type=int)
    settle.add_argument("--mode", default="DEMO")
    summary = subparsers.add_parser("summarize-shadow")
    summary.add_argument("--runs-root", required=True)
    summary.add_argument("--mode", default="DEMO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze-history":
            repo_root = Path(args.repo_root).resolve()
            output = Path(args.output)
            if not output.is_absolute():
                output = repo_root / output
            inventory = history_inventory(repo_root)
            print("DATA_DIRECTORY_AND_FIELD_MAPPING")
            print(json.dumps({"inventory": inventory, "field_mapping": HISTORY_FIELD_MAP}, ensure_ascii=False, indent=2))
            analysis = write_history_outputs(repo_root, output)
            print(json.dumps(json_safe({"report": str(output), "sample": analysis["sample"]}), ensure_ascii=False, indent=2))
            return 0
        if str(getattr(args, "mode", "DEMO")).upper() != "DEMO":
            raise ValidationError("formal mode is rejected; only DEMO is available")
        if args.command == "run-shadow":
            result = run_shadow(
                Path(args.probability_input),
                decimal(args.intended_usd, "intended_usd"),
                Path(args.output_dir),
                Path(args.saved_public_evidence) if args.saved_public_evidence else None,
                args.live_readonly,
            )
            output = {
                "output_dir": args.output_dir,
                "run_status": result["run_status"],
                "run_id": result["run_identity"]["run_id"],
                "demo_ledger": result["demo_ledger"],
                "safety": result["safety"],
            }
        elif args.command == "update-shadow":
            result = update_shadow(
                Path(args.run_dir),
                Path(args.saved_public_evidence) if args.saved_public_evidence else None,
                args.live_readonly,
            )
            output = {
                "run_dir": args.run_dir,
                "update_id": result["update_id"],
                "run_id": result["run_id"],
                "result_counts": dict(Counter(item["status"] for item in result["results"])),
            }
        elif args.command == "settle-shadow":
            result = settle_shadow(Path(args.run_dir), args.observed_max_temp_c)
            output = {
                "run_dir": args.run_dir,
                "run_id": result["run_id"],
                "settlement_status": result["settlement_status"],
                "observed_max_temp_c": result["observed_max_temp_c"],
            }
        else:
            result = summarize_shadow(Path(args.runs_root))
            output = {
                "runs_root": args.runs_root,
                "settled_event_count": result["settled_event_count"],
                "strategy_count": len(result["strategies"]),
            }
        print(json.dumps(json_safe(output), ensure_ascii=False, indent=2))
        return 0
    except (ValidationError, AdapterError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
