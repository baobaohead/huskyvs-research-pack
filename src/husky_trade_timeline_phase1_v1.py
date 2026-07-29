#!/usr/bin/env python3
"""Reconstruct public Husky weather-trade timelines from repository snapshots.

This is a public-record research utility.  It does not connect an account,
submit orders, sign messages, or start any forward/formal trading process.
Public timestamps are observation timestamps, not original order timestamps.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo


CST = ZoneInfo("Asia/Shanghai")
HUSKY_WALLET = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
ADD_THRESHOLD = Decimal("0.01")
SIMULTANEOUS_BASKET_SECONDS = 5 * 60
PHASE_ORDER = [
    "D-1_EARLY",
    "D-1_AFTERNOON",
    "D-1_EVENING",
    "D0_OVERNIGHT",
    "D0_MORNING",
    "D0_WARMING_EARLY",
    "D0_WARMING_CORE",
    "D0_LATE",
    "OUTSIDE_RESEARCH_WINDOW",
]
RAW_PROFILE_FILES = [
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
]
TIMELINE_FIELDS = [
    "event_key",
    "city",
    "weather_date_local",
    "weather_metric",
    "condition_id",
    "event_slug",
    "slug",
    "token_id",
    "asset",
    "temperature_bucket",
    "bucket_kind",
    "bucket_low",
    "bucket_high",
    "unit",
    "public_record_timestamp_utc",
    "public_record_timestamp_cst",
    "relative_day",
    "time_phase",
    "side",
    "price",
    "shares",
    "trade_usd",
    "trade_usd_source",
    "transaction_hash",
    "source_file",
    "source_row_number",
    "raw_transaction_hash",
    "cumulative_buy_usd_event",
    "cumulative_buy_usd_bucket",
    "cumulative_buy_shares_bucket",
    "cumulative_sell_shares_bucket",
    "remaining_shares_bucket",
    "weighted_average_buy_price_bucket",
    "weighted_average_sell_price_bucket",
    "previous_buy_price_bucket",
    "price_change_vs_previous_buy",
    "price_change_vs_pretrade_weighted_average_cost",
    "add_action_classification",
]


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decimal_number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def canonical_decimal(value: Any) -> str:
    number = decimal_number(value)
    if number is None:
        return ""
    return format(number.normalize(), "f")


def epoch_seconds(value: Any) -> int:
    number = finite_number(value)
    if number is None:
        raise ValueError(f"Invalid Unix timestamp: {value!r}")
    absolute = abs(number)
    if absolute >= 1e17:
        number /= 1e9
    elif absolute >= 1e14:
        number /= 1e6
    elif absolute >= 1e11:
        number /= 1e3
    return int(number)


def timestamp_unit(values: Iterable[Any]) -> str:
    magnitudes = [abs(float(v)) for v in values if finite_number(v) is not None]
    if not magnitudes:
        return "NOT_AVAILABLE"
    med = statistics.median(magnitudes)
    if med >= 1e17:
        return "nanoseconds"
    if med >= 1e14:
        return "microseconds"
    if med >= 1e11:
        return "milliseconds"
    return "seconds"


def epoch_to_iso(value: Any, tz: timezone | ZoneInfo = timezone.utc) -> str:
    return datetime.fromtimestamp(epoch_seconds(value), timezone.utc).astimezone(tz).isoformat()


def relative_day_and_phase(timestamp: Any, weather_date_local: str) -> tuple[str, str]:
    local = datetime.fromtimestamp(epoch_seconds(timestamp), timezone.utc).astimezone(CST)
    weather_day = date.fromisoformat(weather_date_local)
    delta = (local.date() - weather_day).days
    relative_day = "D0" if delta == 0 else (f"D+{delta}" if delta > 0 else f"D{delta}")
    hour = local.hour + local.minute / 60 + local.second / 3600
    if delta == -1:
        if hour < 12:
            return relative_day, "D-1_EARLY"
        if hour < 18:
            return relative_day, "D-1_AFTERNOON"
        return relative_day, "D-1_EVENING"
    if delta == 0:
        if hour < 8:
            return relative_day, "D0_OVERNIGHT"
        if hour < 10:
            return relative_day, "D0_MORNING"
        if hour < 12:
            return relative_day, "D0_WARMING_EARLY"
        if hour < 14:
            return relative_day, "D0_WARMING_CORE"
        return relative_day, "D0_LATE"
    return relative_day, "OUTSIDE_RESEARCH_WINDOW"


def stable_trade_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Keep distinct tokens/fills even when they share a transaction hash."""
    return (
        str(row.get("timestamp") or row.get("timestamp_epoch") or ""),
        str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
        str(row.get("conditionId") or row.get("condition_id") or "").lower(),
        str(row.get("asset") or row.get("token_id") or ""),
        str(row.get("side") or "").upper(),
        canonical_decimal(row.get("price")),
        canonical_decimal(row.get("size") if "size" in row else row.get("shares")),
    )


def activity_join_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Join trades to activity before size.

    The public endpoints disagree on ``size`` for a minority of otherwise
    identical trade records.  The remaining six fields have full, one-to-one
    group coverage in the repository snapshot; size is retained as a
    consistency check and nearest-match tie-break, not silently discarded.
    """
    stable = stable_trade_key(row)
    return stable[:-1]


def deduplicate_trades(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[tuple[str, ...]] = set()
    kept: list[dict[str, Any]] = []
    duplicate_count = 0
    for row in rows:
        key = stable_trade_key(row)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, duplicate_count


def compute_trade_usd(row: dict[str, Any]) -> tuple[float, str]:
    usdc = finite_number(row.get("usdcSize"))
    if usdc is not None:
        return usdc, "usdcSize"
    price = finite_number(row.get("price"))
    size = finite_number(row.get("size") if "size" in row else row.get("shares"))
    if price is None or size is None:
        return math.nan, "NOT_AVAILABLE"
    return price * size, "price_x_size"


def classify_add(previous_price: Any, current_price: Any) -> tuple[str, float | None]:
    previous = decimal_number(previous_price)
    current = decimal_number(current_price)
    if previous is None or current is None:
        return "UNKNOWN", None
    change = current - previous
    if change >= ADD_THRESHOLD:
        return "PRICE_UP_ADD", float(change)
    if change <= -ADD_THRESHOLD:
        return "PRICE_DOWN_ADD", float(change)
    return "PRICE_FLAT_ADD", float(change)


def buckets_adjacent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("unit") != b.get("unit"):
        return False
    if a.get("bucket_kind") != "exact" or b.get("bucket_kind") != "exact":
        return False
    av = finite_number(a.get("bucket_low"))
    bv = finite_number(b.get("bucket_low"))
    if av is None or bv is None or not av.is_integer() or not bv.is_integer():
        return False
    return abs(int(av) - int(bv)) == 1


def threshold_time(
    rows: Iterable[dict[str, Any]],
    value_field: str,
    total: float,
    fraction: float,
) -> str | None:
    if total <= 0:
        return None
    running = 0.0
    for row in rows:
        running += float(row[value_field])
        if running + 1e-12 >= total * fraction:
            return str(row["public_record_timestamp_cst"])
    return None


def fifo_pnl(trades: Iterable[dict[str, Any]]) -> tuple[float, float, bool]:
    lots: deque[list[float]] = deque()
    pnl = 0.0
    paired = 0.0
    complete = True
    for row in trades:
        side = str(row.get("side") or "").upper()
        shares = finite_number(row.get("shares") if "shares" in row else row.get("size")) or 0.0
        recorded_price = finite_number(row.get("price")) or 0.0
        direct_usd = finite_number(row.get("trade_usd"))
        price = direct_usd / shares if direct_usd is not None and shares > 0 else recorded_price
        if side == "BUY":
            lots.append([shares, price])
        elif side == "SELL":
            remaining = shares
            while remaining > 1e-12 and lots:
                take = min(remaining, lots[0][0])
                pnl += take * (price - lots[0][1])
                paired += take
                lots[0][0] -= take
                remaining -= take
                if lots[0][0] <= 1e-12:
                    lots.popleft()
            if remaining > 1e-9:
                complete = False
    return pnl, paired, complete


def average_cost_pnl(trades: Iterable[dict[str, Any]]) -> tuple[float, float, bool]:
    shares_held = 0.0
    cost_held = 0.0
    pnl = 0.0
    paired = 0.0
    complete = True
    for row in trades:
        side = str(row.get("side") or "").upper()
        shares = finite_number(row.get("shares") if "shares" in row else row.get("size")) or 0.0
        recorded_price = finite_number(row.get("price")) or 0.0
        direct_usd = finite_number(row.get("trade_usd"))
        price = direct_usd / shares if direct_usd is not None and shares > 0 else recorded_price
        if side == "BUY":
            shares_held += shares
            cost_held += shares * price
        elif side == "SELL":
            matched = min(shares, shares_held)
            avg = cost_held / shares_held if shares_held else 0.0
            pnl += matched * (price - avg)
            paired += matched
            shares_held -= matched
            cost_held -= matched * avg
            if shares - matched > 1e-9:
                complete = False
    return pnl, paired, complete


def fifo_pnl_by_buy_phase(
    trades: Iterable[dict[str, Any]],
) -> tuple[dict[str, float], bool]:
    lots: deque[list[Any]] = deque()
    by_phase: Counter[str] = Counter()
    complete = True
    for row in trades:
        side = str(row.get("side") or "").upper()
        shares = finite_number(row.get("shares") if "shares" in row else row.get("size")) or 0.0
        recorded_price = finite_number(row.get("price")) or 0.0
        direct_usd = finite_number(row.get("trade_usd"))
        unit_price = direct_usd / shares if direct_usd is not None and shares > 0 else recorded_price
        if side == "BUY":
            lots.append([shares, unit_price, str(row.get("time_phase") or "UNKNOWN")])
        elif side == "SELL":
            remaining = shares
            while remaining > 1e-12 and lots:
                take = min(remaining, lots[0][0])
                by_phase[lots[0][2]] += take * (unit_price - lots[0][1])
                lots[0][0] -= take
                remaining -= take
                if lots[0][0] <= 1e-12:
                    lots.popleft()
            if remaining > 1e-9:
                complete = False
    return dict(by_phase), complete


def average_cost_pnl_by_buy_phase(
    trades: Iterable[dict[str, Any]],
) -> tuple[dict[str, float], bool]:
    phase_shares: Counter[str] = Counter()
    shares_held = 0.0
    cost_held = 0.0
    by_phase: Counter[str] = Counter()
    complete = True
    for row in trades:
        side = str(row.get("side") or "").upper()
        shares = finite_number(row.get("shares") if "shares" in row else row.get("size")) or 0.0
        recorded_price = finite_number(row.get("price")) or 0.0
        direct_usd = finite_number(row.get("trade_usd"))
        unit_price = direct_usd / shares if direct_usd is not None and shares > 0 else recorded_price
        if side == "BUY":
            phase = str(row.get("time_phase") or "UNKNOWN")
            phase_shares[phase] += shares
            shares_held += shares
            cost_held += shares * unit_price
        elif side == "SELL":
            matched = min(shares, shares_held)
            average_cost = cost_held / shares_held if shares_held else 0.0
            pre_sell_shares = shares_held
            if pre_sell_shares > 0:
                for phase in sorted(phase_shares):
                    allocated = matched * phase_shares[phase] / pre_sell_shares
                    by_phase[phase] += allocated * (unit_price - average_cost)
                    phase_shares[phase] -= allocated
            shares_held -= matched
            cost_held -= matched * average_cost
            if shares - matched > 1e-9:
                complete = False
    return dict(by_phase), complete


def iter_csv(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for source_row_number, row in enumerate(reader, start=2):
            yield source_row_number, row


def row_hash(row: dict[str, Any], fields: Iterable[str]) -> str:
    joined = "\x1f".join(str(row.get(field) or "") for field in fields)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def profile_csv(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = 0
        missing = Counter()
        exact_seen: set[str] = set()
        exact_duplicates = 0
        wallets: set[str] = set()
        timestamps: list[str] = []
        min_ts: int | None = None
        max_ts: int | None = None
        for row in reader:
            rows += 1
            for field in fields:
                if row.get(field) in (None, ""):
                    missing[field] += 1
            digest = row_hash(row, fields)
            if digest in exact_seen:
                exact_duplicates += 1
            else:
                exact_seen.add(digest)
            wallet = (row.get("proxyWallet") or "").lower()
            if wallet:
                wallets.add(wallet)
            raw_ts = row.get("timestamp")
            if raw_ts not in (None, ""):
                timestamps.append(raw_ts)
                try:
                    ts = epoch_seconds(raw_ts)
                except ValueError:
                    continue
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
    return {
        "path": str(path),
        "size_bytes": size,
        "rows": rows,
        "fields": fields,
        "timestamp_unit": timestamp_unit(timestamps[:10000]),
        "timestamp_timezone": "UTC epoch; display conversion uses Asia/Shanghai",
        "timestamp_range_utc": {
            "min": epoch_to_iso(min_ts) if min_ts is not None else None,
            "max": epoch_to_iso(max_ts) if max_ts is not None else None,
        },
        "missing_rate": {
            field: (missing[field] / rows if rows else None)
            for field in fields
        },
        "exact_duplicate_rows": exact_duplicates,
        "exact_duplicate_rate": exact_duplicates / rows if rows else None,
        "proxy_wallets": sorted(wallets),
    }


def load_activity_trade_index(
    path: Path,
) -> tuple[dict[tuple[str, ...], list[dict[str, str]]], dict[str, Any]]:
    index: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    trade_rows = 0
    for _, row in iter_csv(path):
        if (row.get("type") or "").upper() != "TRADE":
            continue
        trade_rows += 1
        index[activity_join_key(row)].append({
            "size": row.get("size") or "",
            "usdcSize": row.get("usdcSize") or "",
            "source_row_number": str(_),
        })
    return index, {"activity_trade_rows": trade_rows, "activity_trade_unique_keys": len(index)}


def load_raw_trade_sources(path: Path) -> tuple[dict[tuple[str, ...], list[int]], dict[str, Any]]:
    sources: dict[tuple[str, ...], list[int]] = defaultdict(list)
    rows = 0
    wallets: set[str] = set()
    hashes = 0
    for row_number, row in iter_csv(path):
        rows += 1
        sources[stable_trade_key(row)].append(row_number)
        wallet = (row.get("proxyWallet") or "").lower()
        if wallet:
            wallets.add(wallet)
        if row.get("transactionHash"):
            hashes += 1
    return sources, {
        "raw_trade_rows": rows,
        "raw_trade_unique_keys": len(sources),
        "raw_trade_duplicate_extra_rows": rows - len(sources),
        "proxy_wallets": sorted(wallets),
        "transaction_hash_coverage": hashes / rows if rows else 0.0,
    }


def event_key_for(row: dict[str, Any]) -> str:
    city = re.sub(r"[^a-z0-9]+", "-", str(row.get("city") or "unknown").lower()).strip("-")
    return f"{row.get('weather_date')}__{city}__{row.get('weather_metric') or 'unknown'}"


def load_weather_trades(
    normalized_path: Path,
    raw_sources: dict[tuple[str, ...], list[int]],
    activity_index: dict[tuple[str, ...], list[dict[str, str]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_offsets: Counter[tuple[str, ...]] = Counter()
    activity_used: dict[tuple[str, ...], set[int]] = defaultdict(set)
    normalized_rows = 0
    activity_matches = 0
    activity_size_exact_matches = 0
    raw_matches = 0
    for normalized_row_number, raw in iter_csv(normalized_path):
        normalized_rows += 1
        key = stable_trade_key(raw)
        source_numbers = raw_sources.get(key, [])
        offset = raw_offsets[key]
        source_row_number = source_numbers[offset] if offset < len(source_numbers) else None
        raw_offsets[key] += 1
        if source_row_number is not None:
            raw_matches += 1

        join_key = activity_join_key(raw)
        activity_values = activity_index.get(join_key, [])
        unused = [
            (index, value)
            for index, value in enumerate(activity_values)
            if index not in activity_used[join_key]
        ]
        raw_size = decimal_number(raw.get("size"))
        exact = [
            (index, value) for index, value in unused
            if decimal_number(value.get("size")) == raw_size
        ]
        if exact:
            activity_index_number, activity_value = exact[0]
            activity_size_exact_matches += 1
        elif unused:
            def size_distance(item: tuple[int, dict[str, str]]) -> tuple[Decimal, int]:
                candidate = decimal_number(item[1].get("size"))
                distance = abs((candidate or Decimal(0)) - (raw_size or Decimal(0)))
                return distance, item[0]
            activity_index_number, activity_value = min(unused, key=size_distance)
        else:
            activity_index_number, activity_value = -1, {}
        usdc_size = activity_value.get("usdcSize") or ""
        if activity_index_number >= 0:
            activity_used[join_key].add(activity_index_number)
            activity_matches += 1

        combined = dict(raw)
        combined["usdcSize"] = usdc_size
        trade_usd, trade_usd_source = compute_trade_usd(combined)
        ts = epoch_seconds(raw["timestamp"])
        weather_date = raw.get("weather_date") or ""
        relative_day, phase = relative_day_and_phase(ts, weather_date)
        price = finite_number(raw.get("price"))
        shares = finite_number(raw.get("size"))
        row = {
            "event_key": event_key_for(raw),
            "city": raw.get("city") or "UNKNOWN",
            "weather_date_local": weather_date or "UNKNOWN",
            "weather_metric": raw.get("weather_metric") or "UNKNOWN",
            "condition_id": raw.get("conditionId") or "NOT_AVAILABLE",
            "event_slug": raw.get("eventSlug") or "NOT_AVAILABLE",
            "slug": raw.get("slug") or "NOT_AVAILABLE",
            "token_id": raw.get("asset") or "NOT_AVAILABLE",
            "asset": raw.get("asset") or "NOT_AVAILABLE",
            "temperature_bucket": raw.get("bucket_label") or "UNKNOWN",
            "bucket_kind": raw.get("bucket_kind") or "UNKNOWN",
            "bucket_low": finite_number(raw.get("bucket_low")),
            "bucket_high": finite_number(raw.get("bucket_high")),
            "unit": raw.get("unit") or "UNKNOWN",
            "timestamp_epoch": ts,
            "public_record_timestamp_utc": epoch_to_iso(ts, timezone.utc),
            "public_record_timestamp_cst": epoch_to_iso(ts, CST),
            "relative_day": relative_day,
            "time_phase": phase,
            "side": (raw.get("side") or "UNKNOWN").upper(),
            "price": price,
            "shares": shares,
            "trade_usd": trade_usd,
            "trade_usd_source": trade_usd_source,
            "transaction_hash": raw.get("transactionHash") or "NOT_AVAILABLE",
            "source_file": "data/raw/trades.csv" if source_row_number else "data/processed/weather_trades_normalized.csv",
            "source_row_number": source_row_number or normalized_row_number,
            "raw_transaction_hash": raw.get("transactionHash") or "NOT_AVAILABLE",
            "outcome": raw.get("outcome") or "UNKNOWN",
            "proxy_wallet": (raw.get("proxyWallet") or "").lower(),
        }
        rows.append(row)
    rows, duplicate_count = deduplicate_trades(rows)
    rows.sort(key=lambda row: (
        row["timestamp_epoch"],
        str(row["transaction_hash"]),
        str(row["asset"]),
        str(row["side"]),
        float(row["price"] or 0),
        float(row["shares"] or 0),
    ))
    return rows, {
        "normalized_weather_rows": normalized_rows,
        "deduped_weather_rows": len(rows),
        "weather_duplicate_extra_rows": duplicate_count,
        "raw_source_match_coverage": raw_matches / normalized_rows if normalized_rows else 0.0,
        "activity_trade_match_coverage": activity_matches / normalized_rows if normalized_rows else 0.0,
        "activity_trade_size_exact_match_coverage": (
            activity_size_exact_matches / normalized_rows if normalized_rows else 0.0
        ),
        "weather_date_coverage": (
            sum(row["weather_date_local"] != "UNKNOWN" for row in rows) / len(rows) if rows else 0.0
        ),
    }


def load_lifecycle(path: Path) -> dict[str, dict[str, Any]]:
    lifecycle: dict[str, dict[str, Any]] = {}
    for _, row in iter_csv(path):
        asset = row.get("asset") or ""
        if asset:
            lifecycle[asset] = row
    return lifecycle


def is_authoritative_pnl(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return finite_number(row.get("authoritative_realized_pnl")) is not None


def annotate_timeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cumulative_buy_event = 0.0
    state: dict[str, dict[str, float | None]] = defaultdict(
        lambda: {
            "buy_usd": 0.0,
            "buy_shares": 0.0,
            "sell_usd": 0.0,
            "sell_shares": 0.0,
            "previous_buy_price": None,
        }
    )
    seen_buckets: set[str] = set()
    annotated: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        bucket = str(row["temperature_bucket"])
        bucket_state = state[bucket]
        side = row["side"]
        price = float(row["price"] or 0.0)
        shares = float(row["shares"] or 0.0)
        trade_usd = float(row["trade_usd"] or 0.0)
        row["previous_buy_price_bucket"] = None
        row["price_change_vs_previous_buy"] = None
        row["price_change_vs_pretrade_weighted_average_cost"] = None
        row["add_action_classification"] = None
        if side == "BUY":
            pretrade_average = (
                float(bucket_state["buy_usd"]) / float(bucket_state["buy_shares"])
                if float(bucket_state["buy_shares"]) > 0 else None
            )
            if bucket not in seen_buckets:
                row["add_action_classification"] = "NEW_BUCKET_ADD"
                seen_buckets.add(bucket)
            else:
                classification, change = classify_add(bucket_state["previous_buy_price"], price)
                row["add_action_classification"] = classification
                row["price_change_vs_previous_buy"] = change
            row["previous_buy_price_bucket"] = bucket_state["previous_buy_price"]
            if pretrade_average is not None:
                row["price_change_vs_pretrade_weighted_average_cost"] = price - pretrade_average
            bucket_state["previous_buy_price"] = price
            bucket_state["buy_usd"] = float(bucket_state["buy_usd"]) + trade_usd
            bucket_state["buy_shares"] = float(bucket_state["buy_shares"]) + shares
            cumulative_buy_event += trade_usd
        elif side == "SELL":
            bucket_state["sell_usd"] = float(bucket_state["sell_usd"]) + trade_usd
            bucket_state["sell_shares"] = float(bucket_state["sell_shares"]) + shares

        row["cumulative_buy_usd_event"] = cumulative_buy_event
        row["cumulative_buy_usd_bucket"] = float(bucket_state["buy_usd"])
        row["cumulative_buy_shares_bucket"] = float(bucket_state["buy_shares"])
        row["cumulative_sell_shares_bucket"] = float(bucket_state["sell_shares"])
        row["remaining_shares_bucket"] = float(bucket_state["buy_shares"]) - float(bucket_state["sell_shares"])
        row["weighted_average_buy_price_bucket"] = (
            float(bucket_state["buy_usd"]) / float(bucket_state["buy_shares"])
            if float(bucket_state["buy_shares"]) else None
        )
        row["weighted_average_sell_price_bucket"] = (
            float(bucket_state["sell_usd"]) / float(bucket_state["sell_shares"])
            if float(bucket_state["sell_shares"]) else None
        )
        annotated.append(row)
    return annotated


def build_scope_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buys = [row for row in rows if row["side"] == "BUY"]
    sells = [row for row in rows if row["side"] == "SELL"]
    total_buy_usd = sum(float(row["trade_usd"]) for row in buys)
    total_buy_shares = sum(float(row["shares"]) for row in buys)
    total_sell_usd = sum(float(row["trade_usd"]) for row in sells)
    total_sell_shares = sum(float(row["shares"]) for row in sells)
    build = {
        f"build_{int(fraction * 100)}pct_time": threshold_time(
            buys, "trade_usd", total_buy_usd, fraction
        )
        for fraction in (0.25, 0.50, 0.75)
    }
    sold = {}
    running = 0.0
    for fraction in (0.25, 0.50, 0.75):
        reached = None
        target = total_buy_shares * fraction
        running = 0.0
        for row in sells:
            running += float(row["shares"])
            if target > 0 and running + 1e-12 >= target:
                reached = row["public_record_timestamp_cst"]
                break
        sold[f"sold_{int(fraction * 100)}pct_time"] = reached
        sold[f"sold_{int(fraction * 100)}pct_status"] = "REACHED" if reached else "NOT_REACHED"
    first_buy_epoch = buys[0]["timestamp_epoch"] if buys else None
    last_buy_epoch = buys[-1]["timestamp_epoch"] if buys else None
    first_sell_epoch = sells[0]["timestamp_epoch"] if sells else None
    last_sell_epoch = sells[-1]["timestamp_epoch"] if sells else None
    phase_buy = {phase: 0.0 for phase in PHASE_ORDER}
    for row in buys:
        phase_buy[row["time_phase"]] += float(row["trade_usd"])
    return {
        "first_buy_time": buys[0]["public_record_timestamp_cst"] if buys else None,
        **build,
        "last_buy_time": buys[-1]["public_record_timestamp_cst"] if buys else None,
        "total_buy_usd": total_buy_usd,
        "total_buy_shares": total_buy_shares,
        "buy_trade_count": len(buys),
        "buy_duration_seconds": (
            last_buy_epoch - first_buy_epoch if first_buy_epoch is not None and last_buy_epoch is not None else None
        ),
        "phase_buy_usd": phase_buy,
        "phase_buy_usd_fraction": {
            phase: amount / total_buy_usd if total_buy_usd else None for phase, amount in phase_buy.items()
        },
        "first_sell_time": sells[0]["public_record_timestamp_cst"] if sells else None,
        **sold,
        "last_sell_time": sells[-1]["public_record_timestamp_cst"] if sells else None,
        "total_sell_shares": total_sell_shares,
        "total_sell_usd": total_sell_usd,
        "remaining_recorded_shares": total_buy_shares - total_sell_shares,
        "sell_trade_count": len(sells),
        "holding_duration_to_first_sell_seconds": (
            first_sell_epoch - first_buy_epoch
            if first_buy_epoch is not None and first_sell_epoch is not None else None
        ),
        "holding_duration_to_last_sell_seconds": (
            last_sell_epoch - first_buy_epoch
            if first_buy_epoch is not None and last_sell_epoch is not None else None
        ),
    }


def basket_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buys = [row for row in rows if row["side"] == "BUY"]
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in buys:
        by_bucket[str(row["temperature_bucket"])].append(row)
    bucket_usd = {
        bucket: sum(float(row["trade_usd"]) for row in bucket_rows)
        for bucket, bucket_rows in by_bucket.items()
    }
    join_order = sorted(
        by_bucket,
        key=lambda bucket: (
            by_bucket[bucket][0]["timestamp_epoch"],
            bucket,
        ),
    )
    dominant = (
        sorted(bucket_usd, key=lambda bucket: (-bucket_usd[bucket], bucket))[0]
        if bucket_usd else None
    )
    first_times = {
        bucket: by_bucket[bucket][0]["public_record_timestamp_cst"] for bucket in join_order
    }
    adjacent_pairs: list[list[str]] = []
    for index, left in enumerate(join_order):
        for right in join_order[index + 1:]:
            if buckets_adjacent(by_bucket[left][0], by_bucket[right][0]):
                adjacent_pairs.append([left, right])
    if len(join_order) <= 1:
        formation = "SINGLE_BUCKET_ONLY"
    else:
        spread = max(by_bucket[b][0]["timestamp_epoch"] for b in join_order) - min(
            by_bucket[b][0]["timestamp_epoch"] for b in join_order
        )
        formation = (
            "MULTI_BUCKET_WITHIN_5_MINUTES"
            if spread <= SIMULTANEOUS_BASKET_SECONDS
            else "SINGLE_THEN_BASKET"
        )
    total = sum(bucket_usd.values())
    return {
        "first_bought_bucket": join_order[0] if join_order else None,
        "dominant_bought_bucket": dominant,
        "dominant_bought_bucket_first_buy_time": first_times.get(dominant),
        "bucket_join_order": join_order,
        "bucket_first_buy_times": first_times,
        "bucket_buy_usd": bucket_usd,
        "bucket_buy_usd_fraction": {
            bucket: amount / total if total else None for bucket, amount in bucket_usd.items()
        },
        "basket_formation": formation,
        "adjacent_bucket_pairs": adjacent_pairs,
        "shifted_to_dominant_bucket": bool(dominant and join_order and dominant != join_order[0]),
        "simultaneous_window_definition_seconds": SIMULTANEOUS_BASKET_SECONDS,
    }


def pnl_metrics(rows: list[dict[str, Any]], lifecycle: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_asset[str(row["asset"])].append(row)
    fifo_total = 0.0
    average_total = 0.0
    fifo_complete = True
    average_complete = True
    authoritative_values: list[float] = []
    settlement_assets = 0
    fifo_by_phase: Counter[str] = Counter()
    average_by_phase: Counter[str] = Counter()
    for asset, asset_rows in by_asset.items():
        fifo_value, _, fifo_ok = fifo_pnl(asset_rows)
        average_value, _, average_ok = average_cost_pnl(asset_rows)
        asset_fifo_by_phase, asset_fifo_phase_ok = fifo_pnl_by_buy_phase(asset_rows)
        asset_average_by_phase, asset_average_phase_ok = average_cost_pnl_by_buy_phase(asset_rows)
        fifo_by_phase.update(asset_fifo_by_phase)
        average_by_phase.update(asset_average_by_phase)
        fifo_total += fifo_value
        average_total += average_value
        fifo_complete = fifo_complete and fifo_ok and asset_fifo_phase_ok
        average_complete = average_complete and average_ok and asset_average_phase_ok
        life = lifecycle.get(asset)
        if is_authoritative_pnl(life):
            authoritative_values.append(float(life["authoritative_realized_pnl"]))
            settlement_assets += 1
    all_assets_complete = bool(by_asset) and settlement_assets == len(by_asset)
    difference = abs(fifo_total - average_total)
    scale = max(abs(fifo_total), abs(average_total), 1.0)
    stability = "PROFIT_ATTRIBUTION_UNSTABLE" if difference > max(1.0, 0.10 * scale) else "STABLE_WITHIN_THRESHOLD"
    return {
        "realized_sell_pnl_fifo": fifo_total,
        "realized_sell_pnl_fifo_status": "COMPLETE_FOR_RECORDED_SELLS" if fifo_complete else "PARTIAL_UNMATCHED_SELLS",
        "realized_sell_pnl_average_cost": average_total,
        "realized_sell_pnl_average_cost_status": (
            "COMPLETE_FOR_RECORDED_SELLS" if average_complete else "PARTIAL_UNMATCHED_SELLS"
        ),
        "profit_attribution_stability": stability,
        "realized_sell_pnl_fifo_by_original_buy_phase": dict(fifo_by_phase),
        "realized_sell_pnl_average_cost_by_original_buy_phase": dict(average_by_phase),
        "phase_contribution_status": (
            "RECORDED_SELLS_ONLY_NO_SETTLEMENT_PNL_ALLOCATION"
        ),
        "settlement_pnl": None,
        "settlement_pnl_status": "NOT_SEPARATELY_IDENTIFIABLE_FROM_PUBLIC_POSITION_PNL",
        "total_event_pnl": sum(authoritative_values) if all_assets_complete else None,
        "total_event_pnl_status": (
            "AUTHORITATIVE_POSITION_PNL_COMPLETE"
            if all_assets_complete else "PARTIAL_CASHFLOW_ONLY"
        ),
        "authoritative_pnl_asset_coverage": settlement_assets / len(by_asset) if by_asset else None,
        "settlement_path_assets": settlement_assets,
        "event_asset_count": len(by_asset),
    }


def build_event_summary(
    event_key: str,
    rows: list[dict[str, Any]],
    lifecycle: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    timeline = annotate_timeline(rows)
    overall = build_scope_metrics(timeline)
    by_bucket_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in timeline:
        by_bucket_rows[str(row["temperature_bucket"])].append(row)
    bucket_summaries = {
        bucket: build_scope_metrics(bucket_rows)
        for bucket, bucket_rows in sorted(by_bucket_rows.items())
    }
    basket = basket_metrics(timeline)
    pnl = pnl_metrics(timeline, lifecycle)
    add_counts = Counter(
        row["add_action_classification"]
        for row in timeline
        if row["side"] == "BUY" and row["add_action_classification"]
    )
    add_usd = Counter()
    for row in timeline:
        if row["side"] == "BUY" and row["add_action_classification"]:
            add_usd[row["add_action_classification"]] += float(row["trade_usd"])
    assets = sorted({str(row["asset"]) for row in timeline})
    explicit_settlement = all(is_authoritative_pnl(lifecycle.get(asset)) for asset in assets)
    no_sell_path = (
        "HELD_TO_SETTLEMENT_OBSERVED"
        if overall["sell_trade_count"] == 0 and explicit_settlement
        else (
            "NO_RECORDED_SELL_UNKNOWN_FINAL_PATH"
            if overall["sell_trade_count"] == 0 else "RECORDED_SELL_PATH"
        )
    )
    summary = {
        "event_key": event_key,
        "city": timeline[0]["city"],
        "station_status": (
            "BEIJING_STATION_UNCONFIRMED" if timeline[0]["city"] == "Beijing" else "NOT_APPLICABLE"
        ),
        "zbaa_confirmed": False,
        "weather_date_local": timeline[0]["weather_date_local"],
        "weather_metric": timeline[0]["weather_metric"],
        "unit": timeline[0]["unit"],
        "condition_ids": sorted({row["condition_id"] for row in timeline}),
        "event_slugs": sorted({row["event_slug"] for row in timeline}),
        "overall": overall,
        "buckets": bucket_summaries,
        "basket": basket,
        "add_action_count": dict(add_counts),
        "add_action_buy_usd": dict(add_usd),
        "pnl": pnl,
        "event_data_completeness": (
            "COMPLETE"
            if (
                pnl["total_event_pnl_status"] == "AUTHORITATIVE_POSITION_PNL_COMPLETE"
                and pnl["realized_sell_pnl_fifo_status"] == "COMPLETE_FOR_RECORDED_SELLS"
                and pnl["realized_sell_pnl_average_cost_status"] == "COMPLETE_FOR_RECORDED_SELLS"
            )
            else "PARTIAL"
        ),
        "final_path_status": no_sell_path,
        "observed": [
            "逐笔 BUY/SELL、价格、份额与 public_record_timestamp 来自公开记录。",
            "温度档加入顺序、累计买入金额和记录内剩余份额由公开成交顺序直接计算。",
        ],
        "inferred": [
            "时间阶段是本研究按北京时间划分，不是 Husky 公布的规则。",
            "篮子形成类别由各温度档首次公开成交时间的间隔推断。",
        ],
        "unknown": [
            "原始挂单时间、订单提交时间和撮合引擎精确时间不可由这些公开记录确定。",
            "没有独立公开证据证明北京市场对应 ZBAA 站。",
        ],
    }
    if pnl["total_event_pnl"] is None:
        summary["unknown"].append("公开仓位记录不足以完整计算该事件最终总盈亏。")
    return timeline, summary


def event_selection_features(summary: dict[str, Any]) -> dict[str, bool]:
    overall = summary["overall"]
    total_buy = overall["total_buy_shares"]
    total_sell = overall["total_sell_shares"]
    pnl = summary["pnl"]["total_event_pnl"]
    phases = {
        phase for phase, amount in overall["phase_buy_usd"].items() if amount > 0
    }
    return {
        "beijing": summary["city"] == "Beijing",
        "multi_buy": overall["buy_trade_count"] > 1,
        "multi_bucket": len(summary["basket"]["bucket_join_order"]) > 1,
        "partial_sell": 0 < total_sell < total_buy - 1e-9,
        "has_sell": total_sell > 0,
        "profit": pnl is not None and pnl > 0,
        "loss": pnl is not None and pnl < 0,
        "long_build": (overall["buy_duration_seconds"] or 0) >= 12 * 3600,
        "d_minus_1_and_d0": bool(
            phases & {"D-1_EARLY", "D-1_AFTERNOON", "D-1_EVENING"}
        ) and bool(
            phases & {
                "D0_OVERNIGHT",
                "D0_MORNING",
                "D0_WARMING_EARLY",
                "D0_WARMING_CORE",
                "D0_LATE",
            }
        ),
    }


def select_events(
    summaries: dict[str, dict[str, Any]],
    target_count: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    features = {key: event_selection_features(summary) for key, summary in summaries.items()}

    def score(key: str) -> tuple[float, float, str]:
        feature = features[key]
        summary = summaries[key]
        weights = {
            "beijing": 8,
            "multi_buy": 3,
            "multi_bucket": 5,
            "partial_sell": 6,
            "has_sell": 2,
            "profit": 2,
            "loss": 3,
            "long_build": 2,
            "d_minus_1_and_d0": 5,
        }
        weighted = sum(weights[name] for name, present in feature.items() if present)
        return (-weighted, -summary["overall"]["total_buy_usd"], key)

    selected: list[str] = []
    reasons: dict[str, set[str]] = defaultdict(set)
    categories = [
        "beijing",
        "partial_sell",
        "multi_bucket",
        "loss",
        "profit",
        "d_minus_1_and_d0",
        "long_build",
        "multi_buy",
    ]
    ordered = sorted(summaries, key=score)
    for category in categories:
        candidate = next(
            (
                key for key in ordered
                if features[key][category] and key not in selected
            ),
            None,
        )
        if candidate is not None and len(selected) < target_count:
            selected.append(candidate)
            reasons[candidate].add(category)
    for key in ordered:
        if len(selected) >= target_count:
            break
        if key not in selected:
            selected.append(key)
            reasons[key].add("deterministic_score_fill")
    manifest_rows = [
        {
            "selection_order": index,
            "event_key": key,
            "selection_reasons": sorted(reasons[key]),
            "features": features[key],
            "score_sort_key": list(score(key)),
        }
        for index, key in enumerate(selected, start=1)
    ]
    return selected, manifest_rows


def fmt_number(value: Any, digits: int = 2) -> str:
    number = finite_number(value)
    return "NOT_AVAILABLE" if number is None else f"{number:,.{digits}f}"


def fmt_time(value: Any) -> str:
    return str(value) if value else "NOT_AVAILABLE"


def dominant_phase(summary: dict[str, Any]) -> str:
    phase_buy = summary["overall"]["phase_buy_usd"]
    if not phase_buy or sum(phase_buy.values()) <= 0:
        return "NOT_AVAILABLE"
    return sorted(phase_buy, key=lambda phase: (-phase_buy[phase], PHASE_ORDER.index(phase)))[0]


def event_opening_sentence(summary: dict[str, Any]) -> str:
    phase = dominant_phase(summary)
    share = summary["overall"]["phase_buy_usd_fraction"].get(phase)
    if share is None:
        return "该事件的公开 BUY 金额不足以判断主要建仓阶段。"
    return f"该事件公开 BUY 金额的 {share:.1%} 发生在 {phase}，这是样本内的主要投入阶段。"


def render_event_report(summary: dict[str, Any]) -> str:
    overall = summary["overall"]
    basket = summary["basket"]
    adds = summary["add_action_count"]
    pnl = summary["pnl"]
    sell_text = (
        f"首次 {fmt_time(overall['first_sell_time'])}；最后 {fmt_time(overall['last_sell_time'])}；"
        f"共 {overall['sell_trade_count']} 笔。"
        if overall["sell_trade_count"]
        else "公开成交记录中未看到 SELL；最终路径不能仅据此推定，需结合结算证据。"
    )
    return "\n".join([
        f"# {summary['event_key']} 逐笔交易还原",
        "",
        event_opening_sentence(summary),
        "",
        "## 结论先行",
        "",
        f"- 首次买入：{fmt_time(overall['first_buy_time'])}",
        f"- 完成 25% / 50% / 75% 建仓：{fmt_time(overall['build_25pct_time'])} / {fmt_time(overall['build_50pct_time'])} / {fmt_time(overall['build_75pct_time'])}",
        f"- 最后买入：{fmt_time(overall['last_buy_time'])}",
        f"- 温度档形成顺序：{' → '.join(basket['bucket_join_order']) or 'NOT_AVAILABLE'}",
        f"- 篮子类型：{basket['basket_formation']}；dominant_bought_bucket={basket['dominant_bought_bucket']}",
        f"- 补仓方向：PRICE_UP_ADD={adds.get('PRICE_UP_ADD', 0)}，PRICE_DOWN_ADD={adds.get('PRICE_DOWN_ADD', 0)}，PRICE_FLAT_ADD={adds.get('PRICE_FLAT_ADD', 0)}。",
        f"- 卖出：{sell_text}",
        f"- 记录内未卖份额：{fmt_number(overall['remaining_recorded_shares'], 6)}；路径状态：{summary['final_path_status']}。",
        f"- 数据完整性：{summary['event_data_completeness']}；盈亏路径："
        f"{pnl['total_event_pnl_status']}；总事件 PnL={fmt_number(pnl['total_event_pnl'])}。",
        "",
        "## 建仓和温度篮子",
        "",
        f"该事件共记录 {overall['buy_trade_count']} 笔 BUY，金额 ${fmt_number(overall['total_buy_usd'])}，"
        f"买入持续 {fmt_number((overall['buy_duration_seconds'] or 0) / 3600)} 小时。"
        f"第一个温度档为 {basket['first_bought_bucket']}；金额最高的档为 {basket['dominant_bought_bucket']}。"
        f"相邻整数温度档对为 {json.dumps(basket['adjacent_bucket_pairs'], ensure_ascii=False)}。",
        "",
        "## 卖出和盈亏",
        "",
        f"{sell_text} 以事件总 BUY 份额为分母，50% 卖出阈值状态为 "
        f"{overall['sold_50pct_status']}（时间：{fmt_time(overall['sold_50pct_time'])}）。",
        "",
        f"记录内 SELL 的 FIFO 实现盈亏为 ${fmt_number(pnl['realized_sell_pnl_fifo'])}；"
        f"移动平均成本实现盈亏为 ${fmt_number(pnl['realized_sell_pnl_average_cost'])}。"
        f"归因稳定性：{pnl['profit_attribution_stability']}。"
        f"两者都是本研究口径，不代表 Husky 的会计方法。",
        "",
        f"按原始买入阶段归因的 FIFO 结果："
        f"{json.dumps(pnl['realized_sell_pnl_fifo_by_original_buy_phase'], ensure_ascii=False)}；"
        f"移动平均成本结果："
        f"{json.dumps(pnl['realized_sell_pnl_average_cost_by_original_buy_phase'], ensure_ascii=False)}。"
        f"状态：{pnl['phase_contribution_status']}。",
        "",
        "## 证据等级",
        "",
        "### OBSERVED",
        "",
        *[f"- {item}" for item in summary["observed"]],
        "",
        "### INFERRED",
        "",
        *[f"- {item}" for item in summary["inferred"]],
        "",
        "### UNKNOWN",
        "",
        *[f"- {item}" for item in summary["unknown"]],
        "",
        "本案例属于 `EXPLORATORY_CASE_SELECTION`、`NOT_A_RANDOM_SAMPLE`、"
        "`NOT_A_PROFITABILITY_VALIDATION`，不得外推为 Husky 整体策略。",
        "",
    ])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [safe_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def candidate_cutoff_result(
    selected_summaries: list[dict[str, Any]],
    cutoff_label: str,
) -> dict[str, Any]:
    total = sum(summary["overall"]["total_buy_usd"] for summary in selected_summaries)
    before = 0.0
    if cutoff_label == "D1_1500_CANDIDATE":
        cutoff_delta, cutoff_hour = -1, 15
    elif cutoff_label == "D0_0800_CANDIDATE":
        cutoff_delta, cutoff_hour = 0, 8
    elif cutoff_label == "D0_1000_CANDIDATE":
        cutoff_delta, cutoff_hour = 0, 10
    elif cutoff_label == "D0_1100_CANDIDATE":
        cutoff_delta, cutoff_hour = 0, 11
    else:
        return {
            "buy_usd_before": None,
            "buy_usd_after": None,
            "buy_usd_share_before": None,
            "followability": "REQUIRES_REALTIME_NOWCAST_DEFINITION",
            "sample_sufficiency": "INSUFFICIENT_FOR_FINAL_MODEL_SELECTION",
        }
    for summary in selected_summaries:
        weather_day = date.fromisoformat(summary["weather_date_local"])
        # Re-read event timeline contribution stored for cutoff calculations.
        for row in summary["_timeline"]:
            if row["side"] != "BUY":
                continue
            local = datetime.fromtimestamp(row["timestamp_epoch"], timezone.utc).astimezone(CST)
            delta = (local.date() - weather_day).days
            if delta < cutoff_delta or (delta == cutoff_delta and local.hour < cutoff_hour):
                before += float(row["trade_usd"])
    share = before / total if total else None
    followability = (
        "MOST_SAMPLE_BUY_USD_ALREADY_ENTERED"
        if share is not None and share >= 0.75
        else (
            "MATERIAL_SAMPLE_BUY_USD_REMAINS"
            if share is not None and share >= 0.25
            else "MOST_SAMPLE_BUY_USD_REMAINS"
        )
    )
    return {
        "buy_usd_before": before,
        "buy_usd_after": total - before,
        "buy_usd_share_before": share,
        "followability": followability,
        "sample_sufficiency": "INSUFFICIENT_FOR_FINAL_MODEL_SELECTION",
    }


def aggregate_summary(
    selected_summaries: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    event_count = len(selected_summaries)
    complete = sum(
        summary["event_data_completeness"] == "COMPLETE"
        for summary in selected_summaries
    )
    explicit_settlement = sum(
        summary["pnl"]["total_event_pnl_status"] == "AUTHORITATIVE_POSITION_PNL_COMPLETE"
        for summary in selected_summaries
    )
    all_buy_usd = sum(summary["overall"]["total_buy_usd"] for summary in selected_summaries)
    phase_buy = Counter()
    adds_count = Counter()
    adds_usd = Counter()
    build_50_phases = Counter()
    build_50_epochs: list[int] = []
    build_50_relative_seconds: list[float] = []
    first_sell_phases = Counter()
    d_minus_first = 0
    d0_first = 0
    profits = 0
    losses = 0
    for summary in selected_summaries:
        for phase, amount in summary["overall"]["phase_buy_usd"].items():
            phase_buy[phase] += amount
        adds_count.update(summary["add_action_count"])
        adds_usd.update(summary["add_action_buy_usd"])
        timeline = summary["_timeline"]
        build_50 = summary["overall"]["build_50pct_time"]
        matching = next(
            (row for row in timeline if row["public_record_timestamp_cst"] == build_50 and row["side"] == "BUY"),
            None,
        )
        if matching:
            build_50_phases[matching["time_phase"]] += 1
            build_50_epochs.append(matching["timestamp_epoch"])
            weather_day = date.fromisoformat(summary["weather_date_local"])
            d0_start = datetime(
                weather_day.year, weather_day.month, weather_day.day, tzinfo=CST
            )
            build_50_relative_seconds.append(
                matching["timestamp_epoch"] - d0_start.timestamp()
            )
        first_buy = next((row for row in timeline if row["side"] == "BUY"), None)
        if first_buy and first_buy["relative_day"] == "D-1":
            d_minus_first += 1
        if first_buy and first_buy["relative_day"] == "D0":
            d0_first += 1
        first_sell = next((row for row in timeline if row["side"] == "SELL"), None)
        if first_sell:
            first_sell_phases[first_sell["time_phase"]] += 1
        pnl = summary["pnl"]["total_event_pnl"]
        profits += pnl is not None and pnl > 0
        losses += pnl is not None and pnl < 0
    d_minus_1_buy_usd = sum(
        amount for phase, amount in phase_buy.items() if phase.startswith("D-1")
    )
    d0_buy_usd = sum(
        amount for phase, amount in phase_buy.items() if phase.startswith("D0_")
    )
    cutoff_labels = [
        "D1_1500_CANDIDATE",
        "D0_0800_CANDIDATE",
        "D0_1000_CANDIDATE",
        "D0_1100_CANDIDATE",
        "D0_REALTIME_NOWCAST_CANDIDATE",
    ]
    return {
        "research_design": [
            "EXPLORATORY_CASE_SELECTION",
            "NOT_A_RANDOM_SAMPLE",
            "NOT_A_PROFITABILITY_VALIDATION",
        ],
        "sample_event_count": event_count,
        "complete_event_count": complete,
        "d_minus_1_first_entry_events": d_minus_first,
        "d0_first_entry_events": d0_first,
        "phase_buy_usd": dict(phase_buy),
        "phase_buy_usd_fraction": {
            phase: phase_buy[phase] / all_buy_usd if all_buy_usd else None
            for phase in PHASE_ORDER
        },
        "d_minus_1_buy_usd": d_minus_1_buy_usd,
        "d_minus_1_buy_usd_share": (
            d_minus_1_buy_usd / all_buy_usd if all_buy_usd else None
        ),
        "d0_buy_usd": d0_buy_usd,
        "d0_buy_usd_share": d0_buy_usd / all_buy_usd if all_buy_usd else None,
        "build_50_phase_event_count": dict(build_50_phases),
        "median_build_50_public_record_timestamp_cst": (
            epoch_to_iso(statistics.median(build_50_epochs), CST) if build_50_epochs else None
        ),
        "median_build_50_relative_seconds": (
            statistics.median(build_50_relative_seconds)
            if build_50_relative_seconds else None
        ),
        "median_build_50_relative_time": (
            relative_offset_label(statistics.median(build_50_relative_seconds))
            if build_50_relative_seconds else None
        ),
        "add_action_count": dict(adds_count),
        "add_action_buy_usd": dict(adds_usd),
        "single_then_basket_events": sum(
            summary["basket"]["basket_formation"] == "SINGLE_THEN_BASKET"
            for summary in selected_summaries
        ),
        "initial_multi_bucket_events": sum(
            summary["basket"]["basket_formation"] == "MULTI_BUCKET_WITHIN_5_MINUTES"
            for summary in selected_summaries
        ),
        "first_sell_phases": dict(first_sell_phases),
        "sold_50_reached_events": sum(
            summary["overall"]["sold_50pct_status"] == "REACHED"
            for summary in selected_summaries
        ),
        "no_recorded_sell_events": sum(
            summary["overall"]["sell_trade_count"] == 0 for summary in selected_summaries
        ),
        "explicit_settlement_path_events": explicit_settlement,
        "profit_event_count": profits,
        "loss_event_count": losses,
        "realized_sell_pnl_fifo_total": sum(
            summary["pnl"]["realized_sell_pnl_fifo"]
            for summary in selected_summaries
        ),
        "realized_sell_pnl_average_cost_total": sum(
            summary["pnl"]["realized_sell_pnl_average_cost"]
            for summary in selected_summaries
        ),
        "fifo_attribution_complete_events": sum(
            summary["pnl"]["realized_sell_pnl_fifo_status"]
            == "COMPLETE_FOR_RECORDED_SELLS"
            for summary in selected_summaries
        ),
        "average_cost_attribution_complete_events": sum(
            summary["pnl"]["realized_sell_pnl_average_cost_status"]
            == "COMPLETE_FOR_RECORDED_SELLS"
            for summary in selected_summaries
        ),
        "profit_attribution_unstable_events": sum(
            summary["pnl"]["profit_attribution_stability"]
            == "PROFIT_ATTRIBUTION_UNSTABLE"
            for summary in selected_summaries
        ),
        "profit_loss_timing_comparison": profit_loss_timing_comparison(selected_summaries),
        "candidate_research_times": {
            label: candidate_cutoff_result(selected_summaries, label) for label in cutoff_labels
        },
        "data_quality_status": data_quality["status"],
    }


def relative_offset_label(seconds: float) -> str:
    day = math.floor(seconds / 86400)
    remainder = seconds - day * 86400
    hours = int(remainder // 3600)
    minutes = int((remainder % 3600) // 60)
    prefix = "D0" if day == 0 else (f"D+{day}" if day > 0 else f"D{day}")
    return f"{prefix} {hours:02d}:{minutes:02d} CST"


def profit_loss_timing_comparison(
    selected_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, predicate in (
        ("profit_events", lambda value: value is not None and value > 0),
        ("loss_events", lambda value: value is not None and value < 0),
    ):
        group = [
            summary for summary in selected_summaries
            if predicate(summary["pnl"]["total_event_pnl"])
        ]
        total = sum(summary["overall"]["total_buy_usd"] for summary in group)
        d_minus = sum(
            amount
            for summary in group
            for phase, amount in summary["overall"]["phase_buy_usd"].items()
            if phase.startswith("D-1")
        )
        d0 = sum(
            amount
            for summary in group
            for phase, amount in summary["overall"]["phase_buy_usd"].items()
            if phase.startswith("D0_")
        )
        durations = [
            summary["overall"]["buy_duration_seconds"]
            for summary in group
            if summary["overall"]["buy_duration_seconds"] is not None
        ]
        result[label] = {
            "event_count": len(group),
            "d_minus_1_buy_usd_share": d_minus / total if total else None,
            "d0_buy_usd_share": d0 / total if total else None,
            "median_buy_duration_hours": (
                statistics.median(durations) / 3600 if durations else None
            ),
            "interpretation_limit": (
                "EXPLORATORY_SELECTED_CASES_ONLY_NOT_PROFITABILITY_VALIDATION"
            ),
        }
    return result


def choose_examples(selected_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = [
        ("A_分批建仓", lambda summary: summary["overall"]["buy_trade_count"] > 1),
        ("B_相邻档篮子", lambda summary: bool(summary["basket"]["adjacent_bucket_pairs"])),
        ("C_部分卖出或明显退出", lambda summary: summary["overall"]["sell_trade_count"] > 0),
    ]
    examples: list[dict[str, Any]] = []
    used: set[str] = set()
    for label, predicate in definitions:
        candidate = next(
            (
                summary for summary in selected_summaries
                if summary["event_key"] not in used and predicate(summary)
            ),
            None,
        )
        if candidate is None:
            candidate = next((summary for summary in selected_summaries if predicate(summary)), None)
        if candidate is None:
            continue
        used.add(candidate["event_key"])
        examples.append({
            "example_type": label,
            "event_key": candidate["event_key"],
            "first_buy": candidate["overall"]["first_buy_time"],
            "build_50": candidate["overall"]["build_50pct_time"],
            "last_buy": candidate["overall"]["last_buy_time"],
            "first_sell": candidate["overall"]["first_sell_time"],
            "last_sell": candidate["overall"]["last_sell_time"],
            "dominant_phase": dominant_phase(candidate),
            "bucket_join_order": candidate["basket"]["bucket_join_order"],
            "add_action_count": candidate["add_action_count"],
            "data_completeness": candidate["event_data_completeness"],
            "plain_language_conclusion": event_opening_sentence(candidate),
        })
    return examples


def render_summary_report(payload: dict[str, Any]) -> str:
    aggregate = payload["aggregate"]
    selected = payload["selected_events"]
    examples = payload["examples"]
    phase_lines = [
        f"- {phase}: ${fmt_number(aggregate['phase_buy_usd'].get(phase, 0))} "
        f"({aggregate['phase_buy_usd_fraction'].get(phase, 0) or 0:.1%})"
        for phase in PHASE_ORDER
        if aggregate["phase_buy_usd"].get(phase, 0) > 0
    ]
    event_lines = [
        f"- `{summary['event_key']}`：BUY ${fmt_number(summary['overall']['total_buy_usd'])}，"
        f"{summary['overall']['buy_trade_count']} 笔，{len(summary['basket']['bucket_join_order'])} 个温度档，"
        f"SELL {summary['overall']['sell_trade_count']} 笔，数据完整性 {summary['event_data_completeness']}，"
        f"PnL 状态 {summary['pnl']['total_event_pnl_status']}。"
        for summary in selected
    ]
    example_lines: list[str] = []
    for example in examples:
        example_lines.extend([
            f"### {example['example_type']}：{example['event_key']}",
            "",
            f"- 首次买入：{fmt_time(example['first_buy'])}",
            f"- 50% 建仓：{fmt_time(example['build_50'])}",
            f"- 最后买入：{fmt_time(example['last_buy'])}",
            f"- 首次 / 最后卖出：{fmt_time(example['first_sell'])} / {fmt_time(example['last_sell'])}",
            f"- 主要投入阶段：{example['dominant_phase']}",
            f"- 温度档形成顺序：{' → '.join(example['bucket_join_order'])}",
            f"- 补仓价格方向：{json.dumps(example['add_action_count'], ensure_ascii=False)}",
            f"- 数据完整性：{example['data_completeness']}",
            f"- 大白话结论：{example['plain_language_conclusion']}",
            "",
        ])
    cutoff_lines = [
        f"- {label}: 截止前 {result['buy_usd_share_before']:.1%}，"
        f"截止后 ${fmt_number(result['buy_usd_after'])}，{result['followability']}，"
        f"{result['sample_sufficiency']}。"
        if result["buy_usd_share_before"] is not None else
        f"- {label}: {result['followability']}，{result['sample_sufficiency']}。"
        for label, result in aggregate["candidate_research_times"].items()
    ]
    return "\n".join([
        "# Husky 公开天气交易逐笔时间线：一期",
        "",
        "## 技术摘要",
        "",
        f"现有公开记录被评为 `{payload['data_quality']['status']}`。本期按确定性规则选择 "
        f"{aggregate['sample_event_count']} 个事件；其中 {aggregate['complete_event_count']} 个通过严格完整性检查，"
        f"{aggregate['explicit_settlement_path_events']} 个具有明确公开仓位 PnL 路径。"
        "公开时间只称 `public_record_timestamp`，不等同于原始挂单或订单提交时间。",
        "",
        "`EXPLORATORY_CASE_SELECTION` · `NOT_A_RANDOM_SAMPLE` · `NOT_A_PROFITABILITY_VALIDATION`",
        "",
        "## 样本内建仓主要集中在哪些阶段",
        "",
        *phase_lines,
        "",
        f"样本中 D-1 首次建仓 {aggregate['d_minus_1_first_entry_events']} 个，"
        f"D0 首次建仓 {aggregate['d0_first_entry_events']} 个。"
        f"D-1 / D0 BUY 金额占比分别为 {aggregate['d_minus_1_buy_usd_share']:.1%} / "
        f"{aggregate['d0_buy_usd_share']:.1%}。"
        f"50% 建仓相对时点中位数为 {aggregate['median_build_50_relative_time']}。"
        f"PRICE_UP_ADD / PRICE_DOWN_ADD / PRICE_FLAT_ADD 次数分别为 "
        f"{aggregate['add_action_count'].get('PRICE_UP_ADD', 0)} / "
        f"{aggregate['add_action_count'].get('PRICE_DOWN_ADD', 0)} / "
        f"{aggregate['add_action_count'].get('PRICE_FLAT_ADD', 0)}。",
        "",
        "## 温度篮子和退出",
        "",
        f"先单档后篮子的事件为 {aggregate['single_then_basket_events']} 个；"
        f"5 分钟窗口内形成多档的事件为 {aggregate['initial_multi_bucket_events']} 个。"
        f"达到 50% 卖出阈值的事件为 {aggregate['sold_50_reached_events']} 个；"
        f"没有记录 SELL 的事件为 {aggregate['no_recorded_sell_events']} 个。"
        f"首次卖出阶段分布：{json.dumps(aggregate['first_sell_phases'], ensure_ascii=False)}。",
        "",
        "## 八个探索事件",
        "",
        *event_lines,
        "",
        "## 三个可读案例",
        "",
        *example_lines,
        "以上三个案例只用于展示还原方法，不得推广为 Husky 整体策略。",
        "",
        "## 盈利与亏损案例的时间结构只作描述",
        "",
        f"盈利案例（{aggregate['profit_loss_timing_comparison']['profit_events']['event_count']} 个）"
        f"D-1 / D0 BUY 金额占比分别为 "
        f"{aggregate['profit_loss_timing_comparison']['profit_events']['d_minus_1_buy_usd_share'] or 0:.1%} / "
        f"{aggregate['profit_loss_timing_comparison']['profit_events']['d0_buy_usd_share'] or 0:.1%}，"
        f"建仓持续时间中位数 "
        f"{fmt_number(aggregate['profit_loss_timing_comparison']['profit_events']['median_buy_duration_hours'])} 小时。",
        "",
        f"亏损案例（{aggregate['profit_loss_timing_comparison']['loss_events']['event_count']} 个）"
        f"D-1 / D0 BUY 金额占比分别为 "
        f"{aggregate['profit_loss_timing_comparison']['loss_events']['d_minus_1_buy_usd_share'] or 0:.1%} / "
        f"{aggregate['profit_loss_timing_comparison']['loss_events']['d0_buy_usd_share'] or 0:.1%}，"
        f"建仓持续时间中位数 "
        f"{fmt_number(aggregate['profit_loss_timing_comparison']['loss_events']['median_buy_duration_hours'])} 小时。",
        "",
        "这些差异来自非随机选择的 8 个探索案例，不能解释因果，也不能验证总体盈利率。",
        "",
        "## 候选研究时点",
        "",
        *cutoff_lines,
        "",
        "当前 8 个确定性探索样本不足以选择最终模型时点；建议扩展到 20—30 个事件后复核。",
        "",
        "## 数据、定义与方法",
        "",
        "- 事件单位：`city + weather_date_local + weather_metric`。",
        "- 所有显示时间统一为 Asia/Shanghai，并保留 UTC。",
        "- 建仓 25%/50%/75% 按事件最终总 BUY 金额累计，不按笔数，也不在单笔内插值。",
        "- 卖出阈值按事件总 BUY 份额累计。",
        "- 交易金额优先用 activity 的 `usdcSize`；缺失时用 `price × size`。",
        "- 去重键包含 timestamp、transactionHash、conditionId、asset、side、price、size；不会只按 transactionHash 去重。",
        "- 多档 5 分钟窗口是本研究的确定性分类口径，不是 Husky 公布的规则。",
        "",
        "## 数据质量审计结果",
        "",
        f"- 公开数据快照生成时间：{payload['data_quality']['data_snapshot_generated_at_utc']}；"
        f"采集范围 {payload['data_quality']['data_snapshot_start_utc']} 至 "
        f"{payload['data_quality']['data_snapshot_end_utc']}。",
        f"- 原始 trades / activity 行数：{payload['data_quality']['raw_trade_rows']} / "
        f"{payload['data_quality']['raw_activity_rows']}；复合键去重后 trades "
        f"{payload['data_quality']['deduped_trade_rows']} 行。",
        f"- 天气交易 {payload['data_quality']['weather_trade_rows']} 行；transactionHash 覆盖 "
        f"{payload['data_quality']['transaction_hash_coverage']:.2%}；weather_date 覆盖 "
        f"{payload['data_quality']['weather_date_coverage']:.2%}。",
        f"- trades 与 activity 六字段关联覆盖 "
        f"{payload['data_quality']['trades_activity_coverage']:.2%}；size 精确一致 "
        f"{payload['data_quality']['trades_activity_size_exact_coverage']:.2%}。",
        f"- 天气资产结算路径覆盖 {payload['data_quality']['settlement_path_coverage']:.2%}；"
        f"Husky proxyWallet 数量 {payload['data_quality']['husky_proxy_wallet_count']}。",
        "",
        "## 局限与可扩展结论",
        "",
        "- 值得扩展：建仓完成时点、价格上涨/下跌后的补仓比例、相邻档加入顺序、首次卖出阶段。",
        "- 目前不能成立：Husky 的主观预测档、原始挂单时间、北京市场对应 ZBAA、整体盈利率、最终可复制时点。",
        "- 没有 SELL 不自动等于持有到结算；只有仓位生命周期证据完整时才标记观察到结算路径。",
        "- FIFO 与移动平均成本仅归因公开 SELL；最终 position PnL 无法被任意按投入阶段平摊。",
        "",
        "## 下一步",
        "",
        "将相同规则扩展到 20—30 个确定性事件，并定向补齐缺失结算路径；在样本扩展前不选择最终跟随模型。",
        "",
    ])


def build_data_quality(
    repo_root: Path,
    profiles: dict[str, dict[str, Any]],
    raw_meta: dict[str, Any],
    activity_meta: dict[str, Any],
    weather_meta: dict[str, Any],
    lifecycle: dict[str, dict[str, Any]],
    weather_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_manifest = json.loads(
        (repo_root / "data/raw/manifest.json").read_text(encoding="utf-8")
    )
    condition_coverage = sum(row["condition_id"] != "NOT_AVAILABLE" for row in weather_rows) / len(weather_rows)
    asset_coverage = sum(row["asset"] != "NOT_AVAILABLE" for row in weather_rows) / len(weather_rows)
    slug_coverage = sum(row["slug"] != "NOT_AVAILABLE" for row in weather_rows) / len(weather_rows)
    event_slug_coverage = sum(row["event_slug"] != "NOT_AVAILABLE" for row in weather_rows) / len(weather_rows)
    asset_mappings: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in weather_rows:
        asset_mappings[str(row["asset"])].add((str(row["condition_id"]), str(row["temperature_bucket"])))
    mapping_conflicts = sum(len(values) > 1 for values in asset_mappings.values())
    weather_assets = set(asset_mappings)
    settlement_assets = sum(is_authoritative_pnl(lifecycle.get(asset)) for asset in weather_assets)
    has_phase1_core = (
        len({row["event_key"] for row in weather_rows if row["side"] == "BUY"}) >= 5
        and weather_meta["raw_source_match_coverage"] >= 0.99
        and weather_meta["activity_trade_match_coverage"] >= 0.99
        and weather_meta["weather_date_coverage"] >= 0.99
    )
    complete = (
        has_phase1_core
        and settlement_assets == len(weather_assets)
        and mapping_conflicts == 0
    )
    status = (
        "COMPLETE_ENOUGH_FOR_PHASE1"
        if complete else ("PARTIAL_BUT_USABLE" if has_phase1_core else "INSUFFICIENT")
    )
    return {
        "status": status,
        "data_snapshot_generated_at_utc": raw_manifest.get("generated_at"),
        "data_snapshot_start_utc": epoch_to_iso(raw_manifest["start_epoch"]),
        "data_snapshot_end_utc": epoch_to_iso(raw_manifest["end_epoch"]),
        "intended_grain": (
            "one public trade fill identified by timestamp + transactionHash + "
            "conditionId + asset + side + price + size"
        ),
        "profiles": profiles,
        "raw_trade_rows": raw_meta["raw_trade_rows"],
        "raw_activity_rows": profiles["data/raw/activity.csv"]["rows"],
        "deduped_trade_rows": raw_meta["raw_trade_unique_keys"],
        "weather_trade_rows": len(weather_rows),
        "activity_trade_rows": activity_meta["activity_trade_rows"],
        "husky_proxy_wallet_count": len(raw_meta["proxy_wallets"]),
        "husky_proxy_wallets": raw_meta["proxy_wallets"],
        "transaction_hash_coverage": raw_meta["transaction_hash_coverage"],
        "weather_date_coverage": weather_meta["weather_date_coverage"],
        "raw_source_match_coverage": weather_meta["raw_source_match_coverage"],
        "trades_activity_coverage": weather_meta["activity_trade_match_coverage"],
        "trades_activity_size_exact_coverage": (
            weather_meta["activity_trade_size_exact_match_coverage"]
        ),
        "condition_id_coverage": condition_coverage,
        "asset_coverage": asset_coverage,
        "slug_coverage": slug_coverage,
        "event_slug_coverage": event_slug_coverage,
        "asset_condition_bucket_mapping_conflicts": mapping_conflicts,
        "weather_asset_count": len(weather_assets),
        "settlement_path_asset_count": settlement_assets,
        "settlement_path_coverage": settlement_assets / len(weather_assets) if weather_assets else 0.0,
        "weather_date_source": (
            "data/processed/weather_trades_normalized.csv; derived by existing title parser "
            "and position endDate metadata. Rows with fallback year remain a known risk."
        ),
        "public_timestamp_semantics": (
            "public_record_timestamp from public trades/activity; not order_submitted_at "
            "or exact_match_engine_time"
        ),
        "quality_findings": [
            {
                "severity": "LOW",
                "finding": "Raw trades stable-key duplicate count",
                "evidence": raw_meta["raw_trade_duplicate_extra_rows"],
                "impact": "No duplicate inflation under the Phase 1 composite key.",
            },
            {
                "severity": "MEDIUM",
                "finding": "Trades and activity size fields differ for a minority of joined records.",
                "evidence": (
                    f"exact size agreement "
                    f"{weather_meta['activity_trade_size_exact_match_coverage']:.2%}; "
                    "six-field join-group coverage is complete"
                ),
                "impact": (
                    "activity usdcSize is used as the direct cash amount; trades size remains "
                    "the recorded share quantity, and the discrepancy is disclosed."
                ),
            },
            {
                "severity": "MEDIUM",
                "finding": "Settlement/final PnL is not complete for every weather asset.",
                "evidence": f"{settlement_assets}/{len(weather_assets)}",
                "impact": "Some events must remain PARTIAL_CASHFLOW_ONLY.",
            },
            {
                "severity": "MEDIUM",
                "finding": "Public timestamp is observational, not original order time.",
                "evidence": "No order-submission or match-engine timestamp field in repository snapshots.",
                "impact": "Timing conclusions describe public records only.",
            },
        ],
        "ignored_large_file": {
            "path": "data/exit_rule_position_detail_v4.csv",
            "mode": "HEADER_ONLY_OUTSIDE_ANALYZER",
            "staged": False,
        },
    }


def analyze(
    repo_root: Path,
    target_event_count: int,
    output_root: Path,
    summary_md: Path,
    summary_json: Path,
) -> dict[str, Any]:
    profiles = {
        relative: profile_csv(repo_root / relative)
        for relative in RAW_PROFILE_FILES
    }
    raw_sources, raw_meta = load_raw_trade_sources(repo_root / "data/raw/trades.csv")
    activity_index, activity_meta = load_activity_trade_index(repo_root / "data/raw/activity.csv")
    weather_rows, weather_meta = load_weather_trades(
        repo_root / "data/processed/weather_trades_normalized.csv",
        raw_sources,
        activity_index,
    )
    lifecycle = load_lifecycle(repo_root / "data/processed/weather_position_lifecycle.csv")
    data_quality = build_data_quality(
        repo_root, profiles, raw_meta, activity_meta, weather_meta, lifecycle, weather_rows
    )
    if data_quality["status"] == "INSUFFICIENT":
        payload = {
            "data_quality": data_quality,
            "implementation_status": "NEEDS_DATA",
            "unresolved_issues": [
                "Phase 1 core coverage thresholds were not met; no complex event output generated."
            ],
        }
        write_json(summary_json, safe_json(payload))
        summary_md.parent.mkdir(parents=True, exist_ok=True)
        summary_md.write_text(
            "# Husky 公开天气交易逐笔时间线：一期\n\n"
            "`INSUFFICIENT`：现有数据未达到一期还原门槛。详见 JSON 数据质量字段。\n",
            encoding="utf-8",
        )
        return payload

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in weather_rows:
        grouped[row["event_key"]].append(row)
    all_summaries: dict[str, dict[str, Any]] = {}
    all_timelines: dict[str, list[dict[str, Any]]] = {}
    for event_key, event_rows in sorted(grouped.items()):
        if not any(row["side"] == "BUY" for row in event_rows):
            continue
        timeline, summary = build_event_summary(event_key, event_rows, lifecycle)
        all_timelines[event_key] = timeline
        all_summaries[event_key] = summary

    selected_keys, selection_rows = select_events(all_summaries, target_event_count)
    output_root.mkdir(parents=True, exist_ok=True)
    events_dir = output_root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    selected_summaries: list[dict[str, Any]] = []
    for event_key in selected_keys:
        timeline = all_timelines[event_key]
        summary = all_summaries[event_key]
        summary["_timeline"] = timeline
        selected_summaries.append(summary)
        write_csv(events_dir / f"{event_key}_timeline.csv", timeline, TIMELINE_FIELDS)
        write_json(events_dir / f"{event_key}_summary.json", safe_json({
            key: value for key, value in summary.items() if key != "_timeline"
        }))
        (events_dir / f"{event_key}_report.md").write_text(
            render_event_report(summary), encoding="utf-8"
        )

    selection_manifest = {
        "selection_method": (
            "Deterministic category-first selection followed by weighted score and event_key tie-break."
        ),
        "target_event_count": target_event_count,
        "selected_event_count": len(selected_keys),
        "research_design": [
            "EXPLORATORY_CASE_SELECTION",
            "NOT_A_RANDOM_SAMPLE",
            "NOT_A_PROFITABILITY_VALIDATION",
        ],
        "selected": selection_rows,
    }
    write_json(output_root / "selection_manifest.json", selection_manifest)
    aggregate = aggregate_summary(selected_summaries, data_quality)
    examples = choose_examples(selected_summaries)
    public_summaries = [
        {key: value for key, value in summary.items() if key != "_timeline"}
        for summary in selected_summaries
    ]
    payload = {
        "data_quality": data_quality,
        "selection_manifest": selection_manifest,
        "aggregate": aggregate,
        "selected_events": public_summaries,
        "examples": examples,
        "public_data_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "formal_started": False,
        "original_data_modified": False,
        "implementation_status": "READY_FOR_REVIEW",
        "unresolved_issues": [
            "Public timestamps do not reveal original order-submission or exact match-engine time.",
            "Settlement/final PnL paths are incomplete for some events.",
            "Beijing station identity remains BEIJING_STATION_UNCONFIRMED.",
        ],
    }
    write_json(summary_json, safe_json(payload))
    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(render_summary_report(payload), encoding="utf-8")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--repo-root", type=Path, required=True)
    analyze_parser.add_argument("--target-event-count", type=int, default=8)
    analyze_parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("docs/husky_trade_timeline_phase1_v1"),
    )
    analyze_parser.add_argument(
        "--summary-md",
        type=Path,
        default=Path("docs/HUSKY_TRADE_TIMELINE_PHASE1_v1.md"),
    )
    analyze_parser.add_argument(
        "--summary-json",
        type=Path,
        default=Path("docs/HUSKY_TRADE_TIMELINE_PHASE1_v1.json"),
    )
    return parser.parse_args(argv)


def resolve_output(repo_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else repo_root / value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "analyze":
        repo_root = args.repo_root.resolve()
        payload = analyze(
            repo_root=repo_root,
            target_event_count=args.target_event_count,
            output_root=resolve_output(repo_root, args.output_root),
            summary_md=resolve_output(repo_root, args.summary_md),
            summary_json=resolve_output(repo_root, args.summary_json),
        )
        print(json.dumps({
            "data_quality_status": payload["data_quality"]["status"],
            "implementation_status": payload["implementation_status"],
            "selected_event_count": payload.get("selection_manifest", {}).get("selected_event_count", 0),
        }, ensure_ascii=False, sort_keys=True))
        return 0 if payload["implementation_status"] == "READY_FOR_REVIEW" else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
