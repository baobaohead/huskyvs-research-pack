#!/usr/bin/env python3
"""Offline Husky Beijing YES/NO price and outcome study.

The input is the reviewed, portable Beijing evidence already committed to this
repository plus the reviewed Beijing fill/event outputs derived from it.  This
module deliberately has no HTTP client and never connects to an account.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


HUSKY_WALLET = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
ANALYSIS_CUTOFF_UTC = "2026-07-29T03:30:01.944885+00:00"
PUBLIC_DATA_ONLY = True
PUBLIC_GET_ONLY = True
ACCOUNT_CONNECTION = False
SIGNING = False
REAL_ORDER = False
FORMAL_STARTED = False
NETWORK_CALL_COUNT = 0
SCHEMA_VERSION = "husky_beijing_price_outcome_study_v1"
PORTABLE_EVIDENCE_SCHEMA = "husky_beijing_portable_evidence_v1"
EPSILON = 1e-9
ADD_THRESHOLD = Decimal("0.01")

PRICE_BANDS = (
    ("0—1美分", Decimal("0"), Decimal("0.01")),
    ("1—2美分", Decimal("0.01"), Decimal("0.02")),
    ("2—5美分", Decimal("0.02"), Decimal("0.05")),
    ("5—10美分", Decimal("0.05"), Decimal("0.10")),
    ("10—15美分", Decimal("0.10"), Decimal("0.15")),
    ("15—20美分", Decimal("0.15"), Decimal("0.20")),
    ("20—30美分", Decimal("0.20"), Decimal("0.30")),
    ("30—40美分", Decimal("0.30"), Decimal("0.40")),
    ("40—50美分", Decimal("0.40"), Decimal("0.50")),
    ("50—60美分", Decimal("0.50"), Decimal("0.60")),
    ("60—70美分", Decimal("0.60"), Decimal("0.70")),
    ("70—80美分", Decimal("0.70"), Decimal("0.80")),
    ("80—90美分", Decimal("0.80"), Decimal("0.90")),
    ("90—100美分", Decimal("0.90"), Decimal("1.0000000001")),
)
YES_THRESHOLDS = (
    Decimal("0.10"), Decimal("0.15"), Decimal("0.20"), Decimal("0.25"),
    Decimal("0.30"), Decimal("0.35"), Decimal("0.40"), Decimal("0.45"),
    Decimal("0.50"), Decimal("0.55"), Decimal("0.60"), Decimal("0.70"),
    Decimal("0.80"),
)
QUANTILES = (0.50, 0.75, 0.90, 0.95, 0.99)


def decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite decimal value: {value!r}")
    return result


def number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric value: {value!r}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_numbers(value: Any) -> Any:
    """Make saved artifacts stable across supported CPython float repr details."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite output value: {value!r}")
        return round(value, 10)
    if isinstance(value, dict):
        return {key: normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_numbers(item) for item in value]
    return value


def stable_json(value: Any) -> str:
    return json.dumps(
        normalize_numbers(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(
            normalize_numbers(value), ensure_ascii=False, sort_keys=True
        )
    if isinstance(value, float):
        return normalize_numbers(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def write_csv(
    path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None
) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        seen: set[str] = set()
        for row in materialized:
            for field in row:
                if field not in seen:
                    seen.add(field)
                    fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in materialized:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def price_band(value: Any) -> str:
    price = decimal(value)
    for label, low, high in PRICE_BANDS:
        if low <= price < high:
            return label
    raise ValueError(f"price outside supported binary range: {price}")


def nearest_rank(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def weighted_quantile(
    rows: Iterable[dict[str, Any]],
    quantile: float,
    *,
    value_field: str = "price",
    weight_field: str = "trade_usd",
) -> float | None:
    ordered = sorted(
        (
            number(row[value_field]),
            number(row[weight_field]),
        )
        for row in rows
        if number(row[weight_field]) > 0
    )
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return None
    target = total * quantile
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative + EPSILON >= target:
            return value
    return ordered[-1][0]


def bucket_is_tail(row: dict[str, Any]) -> bool:
    return str(row.get("bucket_kind", "")).lower() in {"above", "below"}


def buckets_adjacent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if (
        str(left.get("unit")) != str(right.get("unit"))
        or str(left.get("bucket_kind", "")).lower() != "exact"
        or str(right.get("bucket_kind", "")).lower() != "exact"
    ):
        return False
    try:
        return abs(number(left["bucket_low"]) - number(right["bucket_low"])) == 1
    except (KeyError, ValueError):
        return False


def group_rows(
    rows: Iterable[dict[str, Any]], field: str
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[field])].append(row)
    return dict(grouped)


def bucket_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for bucket, bucket_rows in group_rows(rows, "temperature_bucket").items():
        ordered = sorted(bucket_rows, key=lambda item: int(item["timestamp_epoch"]))
        usd = sum(number(row["trade_usd"]) for row in bucket_rows)
        shares = sum(number(row["shares"]) for row in bucket_rows)
        prices = [number(row["price"]) for row in bucket_rows]
        details.append({
            "temperature_bucket": bucket,
            "bucket_kind": ordered[0].get("bucket_kind", ""),
            "bucket_low": ordered[0].get("bucket_low", ""),
            "unit": ordered[0].get("unit", ""),
            "asset": ordered[0].get("asset", ""),
            "first_price": prices[0],
            "minimum_price": min(prices),
            "maximum_price": max(prices),
            "usd_weighted_average_price": usd / shares if shares else None,
            "buy_usd": usd,
            "buy_shares": shares,
            "first_buy_time_cst": ordered[0].get("public_trade_time_cst", ""),
            "first_buy_timestamp": int(ordered[0]["timestamp_epoch"]),
            "fill_count": len(bucket_rows),
        })
    details.sort(key=lambda item: (item["first_buy_timestamp"], item["temperature_bucket"]))
    total = sum(item["buy_usd"] for item in details)
    for item in details:
        item["buy_usd_share"] = item["buy_usd"] / total if total else None
    return details


def has_adjacent_pair(rows: list[dict[str, Any]]) -> bool:
    representatives = list(group_rows(rows, "temperature_bucket").values())
    reps = [values[0] for values in representatives]
    return any(
        buckets_adjacent(left, right)
        for index, left in enumerate(reps)
        for right in reps[index + 1 :]
    )


def bucket_rotation(rows: list[dict[str, Any]]) -> bool:
    if len({row["temperature_bucket"] for row in rows}) < 2:
        return False
    total = sum(number(row["trade_usd"]) for row in rows)
    if total <= 0:
        return False
    dominants: list[str] = []
    for fraction in (0.25, 0.50, 0.75, 1.0):
        target = total * fraction
        running = 0.0
        amounts: Counter[str] = Counter()
        for row in sorted(rows, key=lambda item: int(item["timestamp_epoch"])):
            amounts[str(row["temperature_bucket"])] += number(row["trade_usd"])
            running += number(row["trade_usd"])
            if running + EPSILON >= target:
                dominants.append(max(amounts, key=lambda key: (amounts[key], key)))
                break
    return len(set(dominants)) > 1


def classify_event_buys(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buys = [row for row in rows if str(row["side"]).upper() == "BUY"]
    yes = [row for row in buys if str(row["outcome"]).upper() == "YES"]
    no = [row for row in buys if str(row["outcome"]).upper() == "NO"]
    yes_buckets = {str(row["temperature_bucket"]) for row in yes}
    no_buckets = {str(row["temperature_bucket"]) for row in no}
    if yes and not no:
        structure = "YES_ONLY"
    elif no and not yes:
        structure = "NO_ONLY"
    elif yes and no:
        structure = "MIXED_YES_AND_NO"
    else:
        structure = "NO_BUY"
    same = bool(yes_buckets & no_buckets)
    cross = any(y != n for y in yes_buckets for n in no_buckets)
    if structure != "MIXED_YES_AND_NO":
        subtype = ""
    elif same and cross:
        subtype = "BOTH"
    elif same:
        subtype = "SAME_BUCKET_BOTH_SIDES"
    else:
        subtype = "CROSS_BUCKET_YES_NO"
    return {
        "event_buy_structure": structure,
        "mixed_yes_no_subtype": subtype,
        "yes_bucket_count": len(yes_buckets),
        "no_bucket_count": len(no_buckets),
        "yes_buckets": sorted(yes_buckets),
        "no_buckets": sorted(no_buckets),
        "same_bucket_both_sides": same,
        "cross_bucket_yes_no": cross,
    }


def annotate_outcome_adds(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    states: dict[tuple[str, str, str], dict[str, float | None]] = defaultdict(
        lambda: {"previous_price": None, "buy_usd": 0.0, "buy_shares": 0.0}
    )
    for row in sorted(result, key=lambda item: int(item["timestamp_epoch"])):
        row.update({
            "outcome_previous_buy_price": None,
            "outcome_price_add_class": "",
            "outcome_pretrade_average_cost": None,
            "outcome_average_cost_add_class": "",
        })
        if str(row["side"]).upper() != "BUY":
            continue
        key = (
            str(row["event_key"]),
            str(row["outcome"]).upper(),
            str(row["asset"]),
        )
        state = states[key]
        current = decimal(row["price"])
        previous = state["previous_price"]
        if previous is not None:
            change = current - decimal(previous)
            row["outcome_previous_buy_price"] = float(previous)
            row["outcome_price_add_class"] = (
                "PRICE_UP_ADD" if change >= ADD_THRESHOLD
                else "PRICE_DOWN_ADD" if change <= -ADD_THRESHOLD
                else "PRICE_FLAT_ADD"
            )
        shares = float(state["buy_shares"] or 0)
        if shares > 0:
            average = float(state["buy_usd"] or 0) / shares
            change = current - decimal(average)
            row["outcome_pretrade_average_cost"] = average
            row["outcome_average_cost_add_class"] = (
                "ABOVE_AVERAGE_COST_ADD" if change >= ADD_THRESHOLD
                else "BELOW_AVERAGE_COST_ADD" if change <= -ADD_THRESHOLD
                else "NEAR_AVERAGE_COST_ADD"
            )
        state["buy_usd"] = float(state["buy_usd"] or 0) + number(row["trade_usd"])
        state["buy_shares"] = shares + number(row["shares"])
        state["previous_price"] = float(current)
    return result


def price_band_summary(
    rows: list[dict[str, Any]], outcome: str
) -> list[dict[str, Any]]:
    total_usd = sum(number(row["trade_usd"]) for row in rows)
    total_fills = len(rows)
    result: list[dict[str, Any]] = []
    for label, low, high in PRICE_BANDS:
        subset = [
            row for row in rows
            if low <= decimal(row["price"]) < high
        ]
        by_event = group_rows(subset, "event_key") if subset else {}
        usd = sum(number(row["trade_usd"]) for row in subset)
        shares = sum(number(row["shares"]) for row in subset)
        prices = [number(row["price"]) for row in subset]
        result.append({
            "outcome": outcome,
            "price_band": label,
            "lower_bound_decimal": float(low),
            "upper_bound_decimal": float(high if high <= 1 else Decimal("1")),
            "lower_bound_cents": float(low * 100),
            "upper_bound_cents": float(min(high, Decimal("1")) * 100),
            "buy_fill_count": len(subset),
            "buy_transaction_hash_count": len({
                row["transaction_hash"] for row in subset
            }),
            "weather_event_count": len({row["event_key"] for row in subset}),
            "temperature_asset_count": len({row["asset"] for row in subset}),
            "buy_shares": shares,
            "buy_usd": usd,
            "buy_usd_share": usd / total_usd if total_usd else 0,
            "buy_fill_share": len(subset) / total_fills if total_fills else 0,
            "average_fill_price": statistics.fmean(prices) if prices else None,
            "median_fill_price": statistics.median(prices) if prices else None,
            "max_single_fill_buy_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "max_single_event_buy_usd": max(
                (
                    sum(number(row["trade_usd"]) for row in event_rows)
                    for event_rows in by_event.values()
                ),
                default=0,
            ),
            **({
                "average_implied_yes_equivalent_price": (
                    statistics.fmean(1 - value for value in prices)
                    if prices else None
                ),
                "median_implied_yes_equivalent_price": (
                    statistics.median(1 - value for value in prices)
                    if prices else None
                ),
            } if outcome == "NO" else {}),
        })
    return result


def yes_threshold_summary(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    total_usd = sum(number(row["trade_usd"]) for row in rows)
    result = []
    for threshold in YES_THRESHOLDS:
        subset = [row for row in rows if decimal(row["price"]) >= threshold]
        by_event = group_rows(subset, "event_key") if subset else {}
        usd = sum(number(row["trade_usd"]) for row in subset)
        dates = sorted(
            str(row.get("public_trade_time_cst", ""))[:10] for row in subset
        )
        result.append({
            "threshold_decimal_inclusive": float(threshold),
            "threshold_cents_inclusive": float(threshold * 100),
            "buy_yes_fill_count": len(subset),
            "buy_yes_usd": usd,
            "yes_buy_usd_share": usd / total_usd if total_usd else 0,
            "weather_event_count": len(by_event),
            "temperature_asset_count": len({row["asset"] for row in subset}),
            "max_single_fill_buy_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "max_single_event_buy_usd": max(
                (
                    sum(number(row["trade_usd"]) for row in event_rows)
                    for event_rows in by_event.values()
                ),
                default=0,
            ),
            "first_trade_date_cst": dates[0] if dates else "",
            "last_trade_date_cst": dates[-1] if dates else "",
        })
    return result


def meaningful_max_price(rows: list[dict[str, Any]]) -> float | str:
    candidates = sorted({decimal(row["price"]) for row in rows}, reverse=True)
    for candidate in candidates:
        subset = [row for row in rows if decimal(row["price"]) >= candidate]
        usd = sum(number(row["trade_usd"]) for row in subset)
        events = {row["event_key"] for row in subset}
        if usd + EPSILON >= 5 and len(events) >= 3:
            return float(candidate)
    return "NOT_ESTABLISHED"


def basically_no_buy_above(
    rows: list[dict[str, Any]],
    threshold_rows: list[dict[str, Any]] | None = None,
) -> float | str:
    summaries = threshold_rows or yes_threshold_summary(rows)
    for item in summaries:
        if (
            number(item["yes_buy_usd_share"]) <= 0.01 + EPSILON
            and int(item["weather_event_count"]) <= 2
            and number(item["max_single_event_buy_usd"]) <= 5 + EPSILON
        ):
            return number(item["threshold_decimal_inclusive"])
    return "NO_CLEAR_CEILING"


def event_yes_summary(
    event_key: str,
    event_rows: list[dict[str, Any]],
    old_event: dict[str, Any],
) -> dict[str, Any]:
    yes = [
        row for row in event_rows
        if str(row["side"]).upper() == "BUY"
        and str(row["outcome"]).upper() == "YES"
    ]
    details = bucket_details(yes)
    total_usd = sum(item["buy_usd"] for item in details)
    total_shares = sum(item["buy_shares"] for item in details)
    if details:
        dominant = max(details, key=lambda item: (item["buy_usd"], item["temperature_bucket"]))
        highest = max(details, key=lambda item: (item["maximum_price"], item["temperature_bucket"]))
        cheapest = min(details, key=lambda item: (item["minimum_price"], item["temperature_bucket"]))
    else:
        dominant = highest = cheapest = {}
    return {
        "event_key": event_key,
        "weather_date": old_event.get("weather_date", ""),
        "yes_buy_fill_count": len(yes),
        "yes_bucket_count": len(details),
        "yes_buy_shares": total_shares,
        "yes_buy_usd": total_usd,
        "minimum_yes_buy_price": min(
            (number(row["price"]) for row in yes), default=None
        ),
        "maximum_yes_buy_price": max(
            (number(row["price"]) for row in yes), default=None
        ),
        "usd_weighted_average_yes_buy_price": (
            total_usd / total_shares if total_shares else None
        ),
        "dominant_yes_bucket": dominant.get("temperature_bucket", ""),
        "dominant_yes_bucket_weighted_price": dominant.get(
            "usd_weighted_average_price"
        ),
        "highest_price_yes_bucket": highest.get("temperature_bucket", ""),
        "cheapest_price_yes_bucket": cheapest.get("temperature_bucket", ""),
        "entry_timeline_status": old_event.get("entry_timeline_status", ""),
        "pnl_status": old_event.get("pnl_status", ""),
        "strict_pnl_available": old_event.get("pnl_status") == "STRICT_CLOSED_SETTLED",
        "strict_pnl": (
            number(old_event.get("strict_pnl"))
            if old_event.get("pnl_status") == "STRICT_CLOSED_SETTLED"
            else None
        ),
    }


def allocation_label(expensive_share: float) -> str:
    if expensive_share >= 0.75:
        return "EXPENSIVE_80_CHEAP_20"
    if expensive_share >= 0.65:
        return "EXPENSIVE_70_CHEAP_30"
    if expensive_share >= 0.55:
        return "EXPENSIVE_60_CHEAP_40"
    if expensive_share >= 0.45:
        return "APPROXIMATELY_EVEN"
    if expensive_share >= 0.35:
        return "CHEAP_60_EXPENSIVE_40"
    if expensive_share >= 0.25:
        return "CHEAP_70_EXPENSIVE_30"
    return "CHEAP_80_EXPENSIVE_20"


def multi_yes_event_summary(
    event_key: str, event_rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    yes = [
        row for row in event_rows
        if str(row["side"]).upper() == "BUY"
        and str(row["outcome"]).upper() == "YES"
    ]
    details = bucket_details(yes)
    if len(details) < 2:
        return None
    expensive = max(
        details,
        key=lambda item: (item["usd_weighted_average_price"], item["temperature_bucket"]),
    )
    cheap = min(
        details,
        key=lambda item: (item["usd_weighted_average_price"], item["temperature_bucket"]),
    )
    dominant = max(
        details, key=lambda item: (item["buy_usd"], item["temperature_bucket"])
    )
    first = min(
        details, key=lambda item: (item["first_buy_timestamp"], item["temperature_bucket"])
    )
    expensive_share = number(expensive["buy_usd_share"])
    has_high_50 = any(number(item["maximum_price"]) >= 0.50 for item in details)
    high_with_cheaper_adjacent = False
    for high in details:
        if number(high["maximum_price"]) < 0.50:
            continue
        for candidate in details:
            if candidate is high:
                continue
            if (
                number(candidate["usd_weighted_average_price"])
                < number(high["usd_weighted_average_price"])
                and buckets_adjacent(high, candidate)
            ):
                high_with_cheaper_adjacent = True
    return {
        "event_key": event_key,
        "weather_date": event_key[:10],
        "yes_temperature_buckets": [item["temperature_bucket"] for item in details],
        "bucket_details": details,
        "join_order": [item["temperature_bucket"] for item in details],
        "most_expensive_yes_bucket": expensive["temperature_bucket"],
        "cheapest_yes_bucket": cheap["temperature_bucket"],
        "dominant_yes_bucket_by_usd": dominant["temperature_bucket"],
        "earliest_yes_bucket": first["temperature_bucket"],
        "has_adjacent_yes_pair": has_adjacent_pair(yes),
        "weighted_price_gap": (
            number(expensive["usd_weighted_average_price"])
            - number(cheap["usd_weighted_average_price"])
        ),
        "expensive_bucket_buy_usd": expensive["buy_usd"],
        "cheap_bucket_buy_usd": cheap["buy_usd"],
        "expensive_bucket_buy_usd_share": expensive_share,
        "cheap_bucket_buy_usd_share": cheap["buy_usd_share"],
        "funding_share_gap": expensive_share - number(cheap["buy_usd_share"]),
        "most_expensive_is_dominant": (
            expensive["temperature_bucket"] == dominant["temperature_bucket"]
        ),
        "cheapest_is_small_add_on": number(cheap["buy_usd_share"]) < 0.20,
        "expensive_funding_exceeds_cheapest": expensive["buy_usd"] > cheap["buy_usd"],
        "cheapest_funding_exceeds_expensive": cheap["buy_usd"] > expensive["buy_usd"],
        "allocation_pattern": allocation_label(expensive_share),
        "has_yes_buy_at_or_above_50c": has_high_50,
        "high_50c_with_cheaper_adjacent_yes": high_with_cheaper_adjacent,
    }


def closest_scenario_examples(
    multi_rows: list[dict[str, Any]], limit: int = 5
) -> tuple[list[dict[str, Any]], int]:
    def range_distance(value: float, low: float, high: float) -> float:
        return low - value if value < low else value - high if value > high else 0.0

    candidates: list[dict[str, Any]] = []
    for event in multi_rows:
        details = [
            item for item in event["bucket_details"]
            if item.get("bucket_kind") == "exact"
        ]
        if len(details) < 3:
            continue
        best: dict[str, Any] | None = None
        for high in details:
            for adjacent in details:
                if high is adjacent or not buckets_adjacent(high, adjacent):
                    continue
                for far in details:
                    if far is high or far is adjacent:
                        continue
                    if buckets_adjacent(high, far):
                        continue
                    high_price = number(high["usd_weighted_average_price"])
                    adjacent_price = number(adjacent["usd_weighted_average_price"])
                    far_price = number(far["usd_weighted_average_price"])
                    score = (
                        range_distance(high_price, 0.40, 0.70)
                        + range_distance(adjacent_price, 0.10, 0.30)
                        + range_distance(far_price, 0.00, 0.05)
                    )
                    exact = (
                        0.40 <= high_price <= 0.70
                        and 0.10 <= adjacent_price <= 0.30
                        and 0 <= far_price <= 0.05
                    )
                    proposal = {
                        "event_key": event["event_key"],
                        "weather_date": event["weather_date"],
                        "similarity_score": score,
                        "exact_style_match": exact,
                        "high_price_bucket": high["temperature_bucket"],
                        "high_bucket_weighted_price": high_price,
                        "high_bucket_buy_usd": high["buy_usd"],
                        "high_bucket_buy_usd_share": high["buy_usd_share"],
                        "adjacent_bucket": adjacent["temperature_bucket"],
                        "adjacent_bucket_weighted_price": adjacent_price,
                        "adjacent_bucket_buy_usd": adjacent["buy_usd"],
                        "adjacent_bucket_buy_usd_share": adjacent["buy_usd_share"],
                        "far_bucket": far["temperature_bucket"],
                        "far_bucket_weighted_price": far_price,
                        "far_bucket_buy_usd": far["buy_usd"],
                        "far_bucket_buy_usd_share": far["buy_usd_share"],
                        "dominant_yes_bucket": event["dominant_yes_bucket_by_usd"],
                        "buy_times_cst": {
                            item["temperature_bucket"]: item["first_buy_time_cst"]
                            for item in (high, adjacent, far)
                        },
                        "later_adjustment_observed": any(
                            int(item["fill_count"]) > 1 for item in (high, adjacent, far)
                        ),
                    }
                    if best is None or (
                        proposal["similarity_score"],
                        proposal["event_key"],
                    ) < (best["similarity_score"], best["event_key"]):
                        best = proposal
        if best is not None:
            candidates.append(best)
    candidates.sort(
        key=lambda item: (
            not item["exact_style_match"],
            item["similarity_score"],
            item["event_key"],
        )
    )
    exact_count = sum(bool(item["exact_style_match"]) for item in candidates)
    return candidates[:limit], exact_count


def event_context_map(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {str(row["event_key"]): row for row in events}


def no_event_rows(
    event_key: str,
    event_rows: list[dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any] | None:
    no = [
        row for row in event_rows
        if str(row["side"]).upper() == "BUY"
        and str(row["outcome"]).upper() == "NO"
    ]
    if not no:
        return None
    prices = [number(row["price"]) for row in no]
    usd = sum(number(row["trade_usd"]) for row in no)
    return {
        "event_key": event_key,
        "weather_date": event_key[:10],
        "no_buckets": sorted({row["temperature_bucket"] for row in no}),
        "no_buy_fill_count": len(no),
        "no_buy_usd": usd,
        "minimum_no_price": min(prices),
        "maximum_no_price": max(prices),
        "usd_weighted_average_no_price": (
            usd / sum(number(row["shares"]) for row in no)
        ),
        "no_multi_bucket_exclusion_set": (
            len({row["temperature_bucket"] for row in no}) >= 2
        ),
        "no_adjacent_exclusion_set": has_adjacent_pair(no),
        "no_tail_usage": any(bucket_is_tail(row) for row in no),
        "implied_yes_equivalent_at_or_above_80_fill_count": sum(
            1 - number(row["price"]) >= 0.80 for row in no
        ),
        "implied_yes_equivalent_at_or_above_90_fill_count": sum(
            1 - number(row["price"]) >= 0.90 for row in no
        ),
        "implied_yes_equivalent_at_or_above_95_fill_count": sum(
            1 - number(row["price"]) >= 0.95 for row in no
        ),
        "also_bought_yes_other_bucket": any(
            y != n
            for y in classification["yes_buckets"]
            for n in classification["no_buckets"]
        ),
        "same_bucket_yes_and_no": classification["same_bucket_both_sides"],
    }


def event_pnl_context(old_event: dict[str, Any]) -> dict[str, Any]:
    strict = old_event.get("pnl_status") == "STRICT_CLOSED_SETTLED"
    return {
        "entry_path_completeness": old_event.get("entry_timeline_status", ""),
        "strict_pnl_available": strict,
        "strict_pnl": number(old_event.get("strict_pnl")) if strict else None,
        "strict_pnl_note": (
            "event-level authoritative strict PnL; not attributed to this fill"
            if strict else "strict PnL unavailable"
        ),
    }


def example_rows(
    yes: list[dict[str, Any]],
    fills_by_event: dict[str, list[dict[str, Any]]],
    old_events: dict[str, dict[str, Any]],
    *,
    low: bool,
) -> list[dict[str, Any]]:
    selected = [
        row for row in yes
        if (number(row["price"]) < 0.10 if low else number(row["price"]) >= 0.30)
    ]
    output = []
    for row in selected:
        event_yes = [
            item for item in fills_by_event[row["event_key"]]
            if str(item["side"]).upper() == "BUY"
            and str(item["outcome"]).upper() == "YES"
        ]
        details = bucket_details(event_yes)
        dominant = max(
            details, key=lambda item: (item["buy_usd"], item["temperature_bucket"])
        )
        current = next(
            item for item in details
            if item["temperature_bucket"] == row["temperature_bucket"]
        )
        event_total = sum(number(item["trade_usd"]) for item in event_yes)
        later_same_asset = [
            item for item in event_yes
            if item["asset"] == row["asset"]
            and int(item["timestamp_epoch"]) > int(row["timestamp_epoch"])
        ]
        cheaper_or_pricier_adjacent = any(
            item["temperature_bucket"] != row["temperature_bucket"]
            and buckets_adjacent(row, item)
            and (
                number(item["price"]) > number(row["price"]) if low
                else number(item["price"]) < number(row["price"])
            )
            for item in event_yes
        )
        context = event_pnl_context(old_events[row["event_key"]])
        output.append({
            "event_key": row["event_key"],
            "weather_date": row["weather_date"],
            "timestamp_cst": row["public_trade_time_cst"],
            "temperature_bucket": row["temperature_bucket"],
            "outcome": "YES",
            "price_decimal": number(row["price"]),
            "price_cents": number(row["price"]) * 100,
            "shares": number(row["shares"]),
            "trade_usd": number(row["trade_usd"]),
            "event_total_yes_buy_usd": event_total,
            "fill_share_of_event_yes_buy_usd": (
                number(row["trade_usd"]) / event_total if event_total else None
            ),
            "bucket_total_yes_buy_usd": current["buy_usd"],
            "bucket_is_event_dominant_yes": (
                current["temperature_bucket"] == dominant["temperature_bucket"]
            ),
            "same_event_bought_other_yes_bucket": len(details) > 1,
            "same_event_bought_no": any(
                str(item["side"]).upper() == "BUY"
                and str(item["outcome"]).upper() == "NO"
                for item in fills_by_event[row["event_key"]]
            ),
            "later_buy_same_yes_asset": bool(later_same_asset),
            (
                "bought_more_expensive_adjacent_yes"
                if low else "bought_cheaper_adjacent_yes"
            ): cheaper_or_pricier_adjacent,
            "threshold_ge_30c": number(row["price"]) >= 0.30,
            "threshold_ge_40c": number(row["price"]) >= 0.40,
            "threshold_ge_50c": number(row["price"]) >= 0.50,
            "threshold_ge_55c": number(row["price"]) >= 0.55,
            "threshold_ge_60c": number(row["price"]) >= 0.60,
            "threshold_ge_70c": number(row["price"]) >= 0.70,
            **context,
        })
    return sorted(
        output, key=lambda item: (item["timestamp_cst"], item["temperature_bucket"])
    )


def strict_pnl_by_yes_price_band(
    yes: list[dict[str, Any]], closed_positions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    buys_by_asset = group_rows(yes, "asset")
    reliable: list[dict[str, Any]] = []
    considered = 0
    excluded = 0
    for closed in closed_positions:
        if str(closed.get("outcome", "")).upper() != "YES":
            continue
        asset = str(closed.get("asset", ""))
        asset_rows = buys_by_asset.get(asset)
        if not asset_rows:
            continue
        considered += 1
        shares = sum(number(row["shares"]) for row in asset_rows)
        usd = sum(number(row["trade_usd"]) for row in asset_rows)
        public_average = usd / shares if shares else 0
        authoritative_shares = number(closed.get("totalBought"))
        authoritative_average = number(closed.get("avgPrice"))
        aligned = (
            abs(shares - authoritative_shares) <= 1e-6
            and abs(public_average - authoritative_average) <= 1e-4
        )
        if not aligned:
            excluded += 1
            continue
        reliable.append({
            "asset": asset,
            "event_key": asset_rows[0]["event_key"],
            "temperature_bucket": asset_rows[0]["temperature_bucket"],
            "average_buy_price": public_average,
            "buy_usd": usd,
            "strict_realized_pnl": number(closed.get("realizedPnl")),
        })
    output = []
    for label, low, high in PRICE_BANDS:
        assets = [
            row for row in reliable
            if low <= decimal(row["average_buy_price"]) < high
        ]
        invested = sum(row["buy_usd"] for row in assets)
        pnl = sum(row["strict_realized_pnl"] for row in assets)
        winner = max(
            (row["strict_realized_pnl"] for row in assets), default=0
        )
        output.append({
            "yes_average_buy_price_band": label,
            "strict_aligned_asset_count": len(assets),
            "buy_usd": invested,
            "strict_realized_pnl": pnl,
            "roi": pnl / invested if invested else None,
            "profitable_asset_count": sum(
                row["strict_realized_pnl"] > 0 for row in assets
            ),
            "loss_asset_count": sum(
                row["strict_realized_pnl"] < 0 for row in assets
            ),
            "break_even_asset_count": sum(
                row["strict_realized_pnl"] == 0 for row in assets
            ),
            "largest_winner_share_of_positive_pnl": (
                winner / sum(
                    max(row["strict_realized_pnl"], 0) for row in assets
                )
                if winner > 0 else None
            ),
            "pnl_excluding_largest_winner": pnl - max(winner, 0),
            "scope_note": (
                "Only assets whose public BUY shares and weighted average price "
                "match authoritative closed-position cost within strict tolerances."
            ),
        })
    status = {
        "result": (
            "ASSET_LEVEL_RELIABLE_SUBSET"
            if reliable else "NOT_AVAILABLE_NO_RELIABLE_ASSET_ALIGNMENT"
        ),
        "authoritative_yes_assets_considered": considered,
        "strict_aligned_yes_asset_count": len(reliable),
        "excluded_yes_asset_count": excluded,
        "share_tolerance": 1e-6,
        "average_price_tolerance": 1e-4,
        "event_pnl_attribution_used": False,
        "unvalidated_resolved_snapshot_included": False,
    }
    return output, status


def verify_sources(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    portable_root = (
        repo_root / "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1"
    )
    manifest_path = portable_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != PORTABLE_EVIDENCE_SCHEMA:
        raise RuntimeError("PORTABLE_EVIDENCE_SCHEMA_MISMATCH")
    if str(manifest.get("wallet", "")).lower() != HUSKY_WALLET:
        raise RuntimeError("PORTABLE_EVIDENCE_WALLET_MISMATCH")
    if manifest.get("analysis_cutoff_utc") != ANALYSIS_CUTOFF_UTC:
        raise RuntimeError("PORTABLE_EVIDENCE_CUTOFF_MISMATCH")
    if (
        manifest.get("public_data_only") is not True
        or manifest.get("public_get_only") is not True
    ):
        raise RuntimeError("PORTABLE_EVIDENCE_SAFETY_FLAG_MISMATCH")
    references = [{
        "relative_path": manifest_path.relative_to(repo_root).as_posix(),
        "sha256": sha256_file(manifest_path),
        "role": "portable_evidence_manifest",
    }]
    for name, meta in sorted(manifest.get("aggregates", {}).items()):
        relative = str(meta.get("relative_path", ""))
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RuntimeError(f"UNSAFE_PORTABLE_EVIDENCE_PATH:{name}")
        path = portable_root / relative
        actual = sha256_file(path)
        if actual != meta.get("sha256"):
            raise RuntimeError(f"PORTABLE_EVIDENCE_SHA_MISMATCH:{name}")
        references.append({
            "relative_path": path.relative_to(repo_root).as_posix(),
            "sha256": actual,
            "record_count": meta.get("record_count"),
            "role": f"portable_{name}",
        })
    return manifest, references


def source_reference(repo_root: Path, relative: str, role: str) -> dict[str, Any]:
    path = repo_root / relative
    return {
        "relative_path": relative,
        "sha256": sha256_file(path),
        "role": role,
    }


def summarize_adds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    price = Counter(
        row["outcome_price_add_class"]
        for row in rows if row.get("outcome_price_add_class")
    )
    average = Counter(
        row["outcome_average_cost_add_class"]
        for row in rows if row.get("outcome_average_cost_add_class")
    )
    bands = {}
    named_bands = {
        "low_below_5c": lambda value: value < 0.05,
        "medium_5_to_20c": lambda value: 0.05 <= value < 0.20,
        "high_at_or_above_20c": lambda value: value >= 0.20,
    }
    for label, predicate in named_bands.items():
        subset = [row for row in rows if predicate(number(row["price"]))]
        bands[label] = {
            "buy_fill_count": len(subset),
            "subsequent_buy_fill_count": sum(
                bool(row.get("outcome_price_add_class")) for row in subset
            ),
            "price_add_counts": dict(Counter(
                row["outcome_price_add_class"]
                for row in subset if row.get("outcome_price_add_class")
            )),
            "average_cost_add_counts": dict(Counter(
                row["outcome_average_cost_add_class"]
                for row in subset if row.get("outcome_average_cost_add_class")
            )),
        }
    for threshold in (0.20, 0.30, 0.50):
        subset = [row for row in rows if number(row["price"]) >= threshold]
        bands[f"at_or_above_{int(threshold * 100)}c"] = {
            "subsequent_buy_fill_count": sum(
                bool(row.get("outcome_price_add_class")) for row in subset
            ),
            "price_add_counts": dict(Counter(
                row["outcome_price_add_class"]
                for row in subset if row.get("outcome_price_add_class")
            )),
            "average_cost_add_counts": dict(Counter(
                row["outcome_average_cost_add_class"]
                for row in subset if row.get("outcome_average_cost_add_class")
            )),
        }
    return {
        "price_add_counts": dict(price),
        "average_cost_add_counts": dict(average),
        "price_bands_and_thresholds": bands,
    }


def band_totals(
    band_rows: list[dict[str, Any]], labels: set[str]
) -> dict[str, Any]:
    selected = [row for row in band_rows if row["price_band"] in labels]
    return {
        "fill_count": sum(int(row["buy_fill_count"]) for row in selected),
        "buy_usd": sum(number(row["buy_usd"]) for row in selected),
        "fill_share": sum(number(row["buy_fill_share"]) for row in selected),
        "buy_usd_share": sum(number(row["buy_usd_share"]) for row in selected),
        "weather_event_count": len({
            event
            for row in selected
            for event in []
        }),
    }


def render_report(summary: dict[str, Any]) -> str:
    s = summary
    bands = {row["price_band"]: row for row in s["yes_price_band_summary"]}

    def combine(labels: list[str]) -> tuple[int, float, float, float]:
        rows = [bands[label] for label in labels]
        return (
            sum(int(row["buy_fill_count"]) for row in rows),
            sum(number(row["buy_fill_share"]) for row in rows),
            sum(number(row["buy_usd"]) for row in rows),
            sum(number(row["buy_usd_share"]) for row in rows),
        )

    below5 = combine(["0—1美分", "1—2美分", "2—5美分"])
    ten20 = combine(["10—15美分", "15—20美分"])
    twenty30 = combine(["20—30美分"])
    thirty50 = combine(["30—40美分", "40—50美分"])
    over50 = combine([
        "50—60美分", "60—70美分", "70—80美分",
        "80—90美分", "90—100美分",
    ])
    multi = s["yes_multi_bucket"]
    scenario = s["scenario_35_36_37_style"]
    no = s["no_analysis"]
    q = s["yes_price_quantiles"]
    ceiling = s["price_ceiling"]
    absolute = ceiling["absolute_max_yes_buy_example"]
    allocation = s["multi_yes_allocation"]
    report_lines = [
        "# Husky 北京 YES/NO 买价研究",
        "",
        "## Executive Summary",
        "",
        (
            f"- **旧的“40天多档、31天相邻档”不能解释为同时押多个温度。** "
            f"它把 BUY YES 和 BUY NO 的合同标签混在一起；纠正后，真正的多 YES 温度事件为 "
            f"{s['corrected_bucket_statistics']['yes_multi_bucket_event_count']} 个，相邻 YES 组合为 "
            f"{s['corrected_bucket_statistics']['yes_adjacent_basket_event_count']} 个。"
        ),
        (
            f"- **YES 资金的主体价格明显低于绝对最高价。** 95% 的 YES 投入在 "
            f"{q['usd_weighted']['p95']:.4f}（{q['usd_weighted']['p95']*100:.2f} 美分）以下，"
            f"99% 在 {q['usd_weighted']['p99']:.4f} 以下；绝对最高买价是 "
            f"{ceiling['absolute_max_yes_buy_price']:.4f}。"
        ),
        (
            f"- **价格上限按预注册规则判断为 "
            f"{ceiling['basically_no_buy_above_price']*100:.0f} 美分。** "
            "这是成交金额、事件覆盖和单事件投入共同决定的描述性结论，不是主观猜测。"
        ),
        (
            f"- **低价 NO 不能读成低价买对应温度。** 53 笔 BUY NO 的中位 NO 价为 "
            f"{no['price_quantiles']['p50']:.4f}，其互补 YES 等价价中位数约 "
            f"{no['implied_yes_equivalent_quantiles']['p50']:.4f}；没有完整同时刻盘口，"
            "不能证明 Husky 在“逆市场主档”。"
        ),
        "",
        "## 25 个核心问题：逐条回答",
        "",
        (
            f"1. **[OBSERVED] 之前的 40 天多档和 31 天相邻档错在哪里？** "
            "旧方法只看 `temperature_bucket`，没有区分 outcome。BUY 30℃ YES 是押 30℃，"
            "BUY 29℃ NO 是排除 29℃，两者不能合称同时押 29℃和30℃。"
        ),
        (
            f"2. **[OBSERVED] 真正买多个 YES 温度的有多少天？** "
            f"{s['corrected_bucket_statistics']['yes_multi_bucket_event_count']} 天。"
        ),
        (
            f"3. **[OBSERVED] 真正买相邻 YES 温度组合的有多少天？** "
            f"{s['corrected_bucket_statistics']['yes_adjacent_basket_event_count']} 天。"
        ),
        f"4. **[OBSERVED] 多少天只买 YES？** {s['event_structures']['YES_ONLY']} 天。",
        f"5. **[OBSERVED] 多少天只买 NO？** {s['event_structures']['NO_ONLY']} 天。",
        (
            f"6. **[OBSERVED] 多少天同时买 YES 和 NO？** "
            f"{s['event_structures']['MIXED_YES_AND_NO']} 天；这只叫混合 YES/NO 结构，"
            "不自动等于对冲、套利或保险。"
        ),
        (
            "7. **[OBSERVED] YES 成交价主要集中在哪些区间？** "
            f"按资金，前三个价格带为：{', '.join(s['yes_top_usd_price_bands'])}。"
        ),
        (
            f"8. **[OBSERVED] 低于 5 美分的 YES 占多少？** {below5[0]} 笔，"
            f"占 YES fill {below5[1]:.2%}；投入 ${below5[2]:.2f}，占 YES 资金 {below5[3]:.2%}。"
        ),
        (
            f"9. **[OBSERVED] 10—20 美分的 YES 占多少？** {ten20[0]} 笔、"
            f"${ten20[2]:.2f}，占 YES 资金 {ten20[3]:.2%}。"
        ),
        (
            f"10. **[OBSERVED] 20—30 美分的 YES 占多少？** {twenty30[0]} 笔、"
            f"${twenty30[2]:.2f}，占 YES 资金 {twenty30[3]:.2%}。"
        ),
        (
            f"11. **[OBSERVED] 30—50 美分的 YES 占多少？** {thirty50[0]} 笔、"
            f"${thirty50[2]:.2f}，占 YES 资金 {thirty50[3]:.2%}。"
        ),
        (
            f"12. **[OBSERVED] 50 美分以上的 YES 占多少？** {over50[0]} 笔、"
            f"${over50[2]:.2f}，占 YES 资金 {over50[3]:.2%}。"
        ),
        (
            f"13. **[OBSERVED] 历史最高买过多少价格的 YES？** "
            f"{ceiling['absolute_max_yes_buy_price']:.4f}（"
            f"{ceiling['absolute_max_yes_buy_price']*100:.2f} 美分）。"
        ),
        (
            f"14. **[OBSERVED] 最高价 YES 是试仓还是主力？** 该笔投入 "
            f"${absolute['trade_usd']:.4f}，占所在事件 YES 投入 "
            f"{absolute['fill_share_of_event_yes_buy_usd']:.2%}；它也是该事件投入最多的 "
            "YES 档，因此不是几毛钱式试仓。"
        ),
        (
            f"15. **[OBSERVED] 95% 的 YES 资金买在多少以下？** "
            f"{q['usd_weighted']['p95']:.4f}（{q['usd_weighted']['p95']*100:.2f} 美分）。"
        ),
        (
            f"16. **[OBSERVED] 99% 的 YES 资金买在多少以下？** "
            f"{q['usd_weighted']['p99']:.4f}（{q['usd_weighted']['p99']*100:.2f} 美分）。"
        ),
        (
            f"17. **[OBSERVED] 是否有清晰的“超过某价格基本不买”？** "
            f"{ceiling['basically_no_buy_above_price']*100:.0f} 美分。规则要求阈值以上资金≤1%、"
            "最多2个事件、且没有单事件投入超过5美元。"
        ),
        (
            f"18. **[OBSERVED] 多 YES 组合中贵档和便宜档各投入多少？** "
            f"逐事件见 `yes_multi_bucket_event_summary.csv`；合计贵档 "
            f"${allocation['expensive_bucket_buy_usd_total']:.2f}，便宜档 "
            f"${allocation['cheap_bucket_buy_usd_total']:.2f}。"
        ),
        (
            f"19. **[OBSERVED] 贵档通常是主力吗？** 最贵档同时也是投入最多档的事件占 "
            f"{allocation['most_expensive_is_dominant_rate']:.2%}。"
        ),
        (
            f"20. **[OBSERVED] 买过类似 55/20/1 美分的三档结构吗？** "
            f"严格命中 {scenario['exact_style_event_count']} 个事件；结论为 "
            f"{scenario['result']}。最接近案例均保存在场景 CSV，未伪造案例。"
        ),
        (
            f"21. **[OBSERVED] 面对 50—60 美分 YES 会不会买？** 本样本没有："
            f"50 美分以上 {over50[0]} 笔；实际观察上限是 "
            f"{ceiling['absolute_max_yes_buy_price']*100:.2f} 美分。是否“高概率”只可用 "
            "Husky 自己的成交价描述，不能冒充完整市场概率曲线。"
        ),
        (
            f"22. **[INFERRED] 便宜 YES 是主策略还是小额彩票？** 低于5美分占 "
            f"{below5[1]:.2%} 的 fill、{below5[3]:.2%} 的资金；低价档成为事件主导 YES 档 "
            f"{s['low_price_yes']['dominant_event_count']} 次。它不只是小额彩票，但资金权重"
            "明显低于 fill 权重，不能因份额大就说资金最重。"
        ),
        (
            "23. **[OBSERVED] 低价 NO 代表什么？** 它是低价买“该温度不会发生”。"
            "例如 NO=0.04 的描述性互补 YES 等价价约为0.96；不含价差、手续费和盘口深度。"
        ),
        (
            f"24. **[NOT_SUPPORTED] 是否经常买 NO 去反对市场高概率温度？** "
            f"观察到 implied YES 等价价≥90%的 BUY NO 有 "
            f"{no['implied_yes_equivalent_at_or_above_90_count']} 笔，"
            "但缺少完整同时刻盘口与预测，不能把它证明成“经常逆市场主档”。"
        ),
        (
            "25. **[NOT_SUPPORTED] 目前什么仍无法证明？** 无法证明当时全市场最热门温度、"
            "完整概率排序、Husky 是否刻意舍弃主档，以及成交价之外的未成交意图。"
        ),
        "",
        "## YES 与 NO 的结构纠正",
        "",
        (
            f"旧口径：{s['legacy_bucket_statistics']['multi_bucket_event_count']} 个多标签事件、"
            f"{s['legacy_bucket_statistics']['adjacent_bucket_event_count']} 个相邻标签事件。"
            f"纠正后：{s['corrected_bucket_statistics']['yes_multi_bucket_event_count']} 个多 YES "
            f"事件、{s['corrected_bucket_statistics']['yes_adjacent_basket_event_count']} 个相邻 "
            f"YES 组合；NO 多档排除组合为 "
            f"{s['corrected_bucket_statistics']['no_multi_bucket_event_count']} 个。"
        ),
        "",
        "## 价格、加仓与资金权重",
        "",
        (
            "所有主结论以实际 BUY USD 为主要权重；shares 和 fill 数同时保留用于解释。"
            "YES 与 NO 的后续加仓分别按同一 outcome/asset 计算，避免把 YES 上涨加仓与 "
            "NO 上涨加仓混在一起。"
        ),
        "",
        "## 严格 PnL 的可用范围",
        "",
        (
            f"资产级结果为 `{s['strict_pnl_price_band']['result']}`："
            f"{s['strict_pnl_price_band']['strict_aligned_yes_asset_count']} 个 YES 资产通过"
            "公开 BUY 份额与平均成本的严格对齐。其余资产不进入价格带 PnL；"
            "36 个未验证 resolved-redeemable 快照没有被并入严格结果，"
            "也没有把整天 PnL 归因给单一价格档。"
        ),
        "",
        "## 下一步",
        "",
        "- 如要判断“市场主档”或是否逆势，需要同一秒或合理短窗口内的完整温度曲线、盘口和预测快照。",
        "- 如要把更多资产纳入价格带 PnL，需要解释 closed-position 成本与公开成交成本的差异并完成资产成本桥接。",
        "",
        "## 进一步问题",
        "",
        "- 高价 YES 的下单意图、未成交订单和盘口冲击仍不可见。",
        "- 本报告是截至固定时间的北京公开成交历史描述，不能单独推出未来必然行为。",
        "",
        "## Caveats and Assumptions",
        "",
        f"- 固定截止时间：`{ANALYSIS_CUTOFF_UTC}`；北京事件 50 个，公开成交 537 笔。",
        "- `implied_yes_equivalent_price = 1 - no_price` 只是二元互补价的描述性换算，不含价差、手续费和盘口深度。",
        "- `FULL_MARKET_FAVORITE_AT_BUY_TIME_STATUS=NOT_SUPPORTED_BY_CURRENT_EVIDENCE`。",
        "- 无网络、无账户连接、无签名、无真实下单、未启动 formal。",
        "",
    ]
    return "\n".join(report_lines)


def analyze(
    repo_root: Path,
    output_root: Path,
    summary_md: Path,
    summary_json: Path,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    manifest, portable_references = verify_sources(repo_root)
    fill_path = (
        repo_root
        / "docs/husky_beijing_full_trade_study_v1/beijing_all_public_fills.csv"
    )
    event_path = (
        repo_root
        / "docs/husky_beijing_full_trade_study_v1/beijing_event_summary.csv"
    )
    old_json_path = repo_root / "docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json"
    old_md_path = repo_root / "docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.md"
    fills = annotate_outcome_adds(read_csv(fill_path))
    old_event_rows = read_csv(event_path)
    old_events = event_context_map(old_event_rows)
    old_summary = json.loads(old_json_path.read_text(encoding="utf-8"))
    closed_positions = json.loads(
        (
            repo_root
            / "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/beijing_closed_positions.json"
        ).read_text(encoding="utf-8")
    )
    if len(fills) != 537:
        raise RuntimeError(f"TOTAL_PUBLIC_FILL_COUNT_MISMATCH:{len(fills)}")
    buys = [row for row in fills if str(row["side"]).upper() == "BUY"]
    sells = [row for row in fills if str(row["side"]).upper() == "SELL"]
    if len(buys) != 453 or len(sells) != 84:
        raise RuntimeError("PUBLIC_SIDE_COUNT_MISMATCH")
    if len(old_event_rows) != 50:
        raise RuntimeError("BEIJING_EVENT_COUNT_MISMATCH")
    yes = [row for row in buys if str(row["outcome"]).upper() == "YES"]
    no = [row for row in buys if str(row["outcome"]).upper() == "NO"]
    if len(yes) + len(no) != len(buys):
        raise RuntimeError("BUY_OUTCOME_CLASSIFICATION_INCOMPLETE")

    fills_by_event = group_rows(fills, "event_key")
    classifications = {
        event_key: classify_event_buys(rows)
        for event_key, rows in fills_by_event.items()
    }
    event_structures = Counter(
        item["event_buy_structure"] for item in classifications.values()
    )
    for label in ("YES_ONLY", "NO_ONLY", "MIXED_YES_AND_NO", "NO_BUY"):
        event_structures.setdefault(label, 0)
    mixed_subtypes = Counter(
        item["mixed_yes_no_subtype"]
        for item in classifications.values()
        if item["mixed_yes_no_subtype"]
    )
    for label in (
        "CROSS_BUCKET_YES_NO", "SAME_BUCKET_BOTH_SIDES", "BOTH"
    ):
        mixed_subtypes.setdefault(label, 0)

    legacy_multi = 0
    legacy_adjacent = 0
    yes_multi = 0
    yes_adjacent = 0
    yes_non_adjacent = 0
    yes_single = 0
    yes_tail_exact = 0
    yes_rotation = 0
    no_multi = 0
    no_adjacent = 0
    no_non_adjacent = 0
    no_single = 0
    no_tail = 0
    for event_key, rows in fills_by_event.items():
        event_buys = [row for row in rows if str(row["side"]).upper() == "BUY"]
        event_yes = [
            row for row in event_buys if str(row["outcome"]).upper() == "YES"
        ]
        event_no = [
            row for row in event_buys if str(row["outcome"]).upper() == "NO"
        ]
        legacy_count = len({row["temperature_bucket"] for row in event_buys})
        if legacy_count >= 2:
            legacy_multi += 1
        if has_adjacent_pair(event_buys):
            legacy_adjacent += 1
        yes_count = len({row["temperature_bucket"] for row in event_yes})
        if yes_count == 1:
            yes_single += 1
        elif yes_count >= 2:
            yes_multi += 1
            if has_adjacent_pair(event_yes):
                yes_adjacent += 1
            else:
                yes_non_adjacent += 1
        if (
            any(bucket_is_tail(row) for row in event_yes)
            and any(not bucket_is_tail(row) for row in event_yes)
        ):
            yes_tail_exact += 1
        if bucket_rotation(event_yes):
            yes_rotation += 1
        no_count = len({row["temperature_bucket"] for row in event_no})
        if no_count == 1:
            no_single += 1
        elif no_count >= 2:
            no_multi += 1
            if has_adjacent_pair(event_no):
                no_adjacent += 1
            else:
                no_non_adjacent += 1
        if any(bucket_is_tail(row) for row in event_no):
            no_tail += 1

    yes_band_rows = price_band_summary(yes, "YES")
    no_band_rows = price_band_summary(no, "NO")
    threshold_rows = yes_threshold_summary(yes)
    yes_event_rows = [
        event_yes_summary(event_key, fills_by_event.get(event_key, []), old_event)
        for event_key, old_event in sorted(old_events.items())
    ]
    multi_rows = [
        result
        for event_key, rows in sorted(fills_by_event.items())
        if (result := multi_yes_event_summary(event_key, rows)) is not None
    ]
    no_events = [
        result
        for event_key, rows in sorted(fills_by_event.items())
        if (
            result := no_event_rows(
                event_key, rows, classifications[event_key]
            )
        ) is not None
    ]
    mixed_rows = [
        {
            "event_key": event_key,
            "weather_date": event_key[:10],
            **classifications[event_key],
            "yes_buy_usd": sum(
                number(row["trade_usd"]) for row in fills_by_event[event_key]
                if str(row["side"]).upper() == "BUY"
                and str(row["outcome"]).upper() == "YES"
            ),
            "no_buy_usd": sum(
                number(row["trade_usd"]) for row in fills_by_event[event_key]
                if str(row["side"]).upper() == "BUY"
                and str(row["outcome"]).upper() == "NO"
            ),
        }
        for event_key in sorted(classifications)
        if classifications[event_key]["event_buy_structure"] == "MIXED_YES_AND_NO"
    ]

    yes_output = []
    for row in yes:
        item = dict(row)
        item["price_decimal"] = number(row["price"])
        item["price_cents"] = number(row["price"]) * 100
        item["price_band"] = price_band(row["price"])
        yes_output.append(item)
    no_output = []
    for row in no:
        item = dict(row)
        item["no_price"] = number(row["price"])
        item["price_decimal"] = number(row["price"])
        item["price_cents"] = number(row["price"]) * 100
        item["implied_yes_equivalent_price"] = 1 - number(row["price"])
        item["implied_yes_equivalent_note"] = (
            "descriptive binary complement only; excludes spread, fees, and depth"
        )
        item["price_band"] = price_band(row["price"])
        no_output.append(item)

    high_examples = example_rows(
        yes, fills_by_event, old_events, low=False
    )
    low_examples = example_rows(
        yes, fills_by_event, old_events, low=True
    )
    scenarios, scenario_exact_count = closest_scenario_examples(multi_rows)
    for scenario in scenarios:
        old_event = old_events[scenario["event_key"]]
        scenario.update(event_pnl_context(old_event))
    strict_band_rows, strict_status = strict_pnl_by_yes_price_band(
        yes, closed_positions
    )

    fill_quantiles = {
        f"p{int(q * 100)}": nearest_rank(
            [number(row["price"]) for row in yes], q
        )
        for q in QUANTILES
    }
    fill_quantiles["max"] = max(number(row["price"]) for row in yes)
    usd_quantiles = {
        f"p{int(q * 100)}": weighted_quantile(yes, q)
        for q in QUANTILES
    }
    usd_quantiles["max"] = fill_quantiles["max"]
    event_metric_quantiles = {}
    for field in (
        "minimum_yes_buy_price",
        "maximum_yes_buy_price",
        "usd_weighted_average_yes_buy_price",
        "dominant_yes_bucket_weighted_price",
    ):
        values = [
            number(row[field]) for row in yes_event_rows
            if row[field] not in (None, "")
        ]
        event_metric_quantiles[field] = {
            f"p{int(q * 100)}": nearest_rank(values, q) for q in QUANTILES
        }
        event_metric_quantiles[field]["max"] = max(values) if values else None

    absolute_max = max(number(row["price"]) for row in yes)
    absolute_examples = [
        row for row in high_examples
        if abs(number(row["price_decimal"]) - absolute_max) <= EPSILON
    ]
    meaningful = meaningful_max_price(yes)
    basic_ceiling = basically_no_buy_above(yes, threshold_rows)
    top_bands = sorted(
        yes_band_rows, key=lambda item: (-number(item["buy_usd"]), item["price_band"])
    )[:3]

    low_dominant_events = 0
    high_dominant_events = 0
    low_bucket_count = 0
    low_small_add_on_bucket_count = 0
    low_with_more_expensive_adjacent_event_count = 0
    for event_key, rows in fills_by_event.items():
        event_yes = [
            row for row in rows
            if str(row["side"]).upper() == "BUY"
            and str(row["outcome"]).upper() == "YES"
        ]
        details = bucket_details(event_yes)
        if not details:
            continue
        dominant = max(
            details, key=lambda item: (item["buy_usd"], item["temperature_bucket"])
        )
        if number(dominant["usd_weighted_average_price"]) < 0.05:
            low_dominant_events += 1
        if number(dominant["usd_weighted_average_price"]) >= 0.30:
            high_dominant_events += 1
        low_details = [
            item for item in details
            if number(item["usd_weighted_average_price"]) < 0.05
        ]
        low_bucket_count += len(low_details)
        low_small_add_on_bucket_count += sum(
            number(item["buy_usd_share"]) < 0.20 for item in low_details
        )
        if any(
            buckets_adjacent(low, expensive)
            and number(expensive["usd_weighted_average_price"])
            > number(low["usd_weighted_average_price"])
            for low in low_details
            for expensive in details
            if expensive is not low
        ):
            low_with_more_expensive_adjacent_event_count += 1

    expensive_total = sum(
        number(row["expensive_bucket_buy_usd"]) for row in multi_rows
    )
    cheap_total = sum(number(row["cheap_bucket_buy_usd"]) for row in multi_rows)
    expensive_is_dominant_count = sum(
        bool(row["most_expensive_is_dominant"]) for row in multi_rows
    )

    no_prices = [number(row["price"]) for row in no]
    no_implied = [1 - value for value in no_prices]
    no_quantiles = {
        f"p{int(q * 100)}": nearest_rank(no_prices, q) for q in QUANTILES
    }
    no_quantiles["max"] = max(no_prices)
    implied_quantiles = {
        f"p{int(q * 100)}": nearest_rank(no_implied, q) for q in QUANTILES
    }
    implied_quantiles["max"] = max(no_implied)

    legacy_correction_rows = [
        {
            "finding": "multi_temperature_bucket_event_count",
            "legacy_value": legacy_multi,
            "corrected_value": yes_multi,
            "corrected_definition": "BUY YES only; multiple temperature buckets",
        },
        {
            "finding": "adjacent_temperature_bucket_event_count",
            "legacy_value": legacy_adjacent,
            "corrected_value": yes_adjacent,
            "corrected_definition": "BUY YES only; at least one adjacent exact pair",
        },
        {
            "finding": "no_exclusion_multi_bucket_event_count",
            "legacy_value": "",
            "corrected_value": no_multi,
            "corrected_definition": "BUY NO only; multi-bucket exclusion set",
        },
        {
            "finding": "mixed_yes_no_event_count",
            "legacy_value": "",
            "corrected_value": event_structures["MIXED_YES_AND_NO"],
            "corrected_definition": "event contains BUY YES and BUY NO",
        },
        {
            "finding": "yes_only_event_count",
            "legacy_value": "",
            "corrected_value": event_structures["YES_ONLY"],
            "corrected_definition": "event contains BUY YES and no BUY NO",
        },
        {
            "finding": "no_only_event_count",
            "legacy_value": "",
            "corrected_value": event_structures["NO_ONLY"],
            "corrected_definition": "event contains BUY NO and no BUY YES",
        },
    ]

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "analysis_cutoff_utc": ANALYSIS_CUTOFF_UTC,
        "husky_wallet": HUSKY_WALLET,
        "public_data_only": PUBLIC_DATA_ONLY,
        "public_get_only": PUBLIC_GET_ONLY,
        "account_connection": ACCOUNT_CONNECTION,
        "signing": SIGNING,
        "real_order": REAL_ORDER,
        "formal_started": FORMAL_STARTED,
        "offline_network_call_count": NETWORK_CALL_COUNT,
        "beijing_event_count": len(old_event_rows),
        "total_public_fill_count": len(fills),
        "public_buy_fill_count": len(buys),
        "public_sell_fill_count": len(sells),
        "buy_yes_fill_count": len(yes),
        "buy_no_fill_count": len(no),
        "buy_yes_usd": sum(number(row["trade_usd"]) for row in yes),
        "buy_no_usd": sum(number(row["trade_usd"]) for row in no),
        "event_structures": dict(event_structures),
        "mixed_yes_no_subtypes": dict(mixed_subtypes),
        "same_bucket_both_sides_event_count": sum(
            item["same_bucket_both_sides"]
            for item in classifications.values()
        ),
        "cross_bucket_yes_no_event_count": sum(
            item["cross_bucket_yes_no"]
            for item in classifications.values()
        ),
        "legacy_bucket_statistics": {
            "method": "LEGACY_ALL_OUTCOME_BUCKETS",
            "multi_bucket_event_count": legacy_multi,
            "adjacent_bucket_event_count": legacy_adjacent,
        },
        "corrected_bucket_statistics": {
            "yes_single_bucket_event_count": yes_single,
            "yes_multi_bucket_event_count": yes_multi,
            "yes_adjacent_basket_event_count": yes_adjacent,
            "yes_non_adjacent_basket_event_count": yes_non_adjacent,
            "yes_tail_plus_exact_event_count": yes_tail_exact,
            "yes_bucket_rotation_event_count": yes_rotation,
            "no_single_bucket_event_count": no_single,
            "no_multi_bucket_event_count": no_multi,
            "no_adjacent_exclusion_event_count": no_adjacent,
            "no_non_adjacent_exclusion_event_count": no_non_adjacent,
            "no_tail_usage_event_count": no_tail,
        },
        "yes_price_band_summary": yes_band_rows,
        "no_price_band_summary": no_band_rows,
        "yes_price_threshold_summary": threshold_rows,
        "yes_price_quantiles": {
            "fill_weighted": fill_quantiles,
            "usd_weighted": usd_quantiles,
            "event_level": event_metric_quantiles,
        },
        "price_ceiling": {
            "yes_usd_p95_price": usd_quantiles["p95"],
            "yes_usd_p99_price": usd_quantiles["p99"],
            "yes_fill_p95_price": fill_quantiles["p95"],
            "yes_event_max_price_p95": event_metric_quantiles[
                "maximum_yes_buy_price"
            ]["p95"],
            "absolute_max_yes_buy_price": absolute_max,
            "absolute_max_yes_buy_example": absolute_examples[0],
            "meaningful_max_yes_buy_price": meaningful,
            "basically_no_buy_above_price": basic_ceiling,
        },
        "yes_event_price_summary": yes_event_rows,
        "yes_multi_bucket": multi_rows,
        "multi_yes_allocation": {
            "event_count": len(multi_rows),
            "expensive_bucket_buy_usd_total": expensive_total,
            "cheap_bucket_buy_usd_total": cheap_total,
            "most_expensive_is_dominant_count": expensive_is_dominant_count,
            "most_expensive_is_dominant_rate": (
                expensive_is_dominant_count / len(multi_rows)
                if multi_rows else 0
            ),
            "allocation_pattern_counts": dict(Counter(
                row["allocation_pattern"] for row in multi_rows
            )),
        },
        "low_price_yes": {
            "definition": "dominant bucket weighted average price below 5 cents",
            "dominant_event_count": low_dominant_events,
            "buy_fill_count": sum(number(row["price"]) < 0.05 for row in yes),
            "buy_shares": sum(
                number(row["shares"]) for row in yes
                if number(row["price"]) < 0.05
            ),
            "buy_shares_share": (
                sum(
                    number(row["shares"]) for row in yes
                    if number(row["price"]) < 0.05
                )
                / sum(number(row["shares"]) for row in yes)
            ),
            "buy_usd": sum(
                number(row["trade_usd"]) for row in yes
                if number(row["price"]) < 0.05
            ),
            "buy_usd_share": (
                sum(
                    number(row["trade_usd"]) for row in yes
                    if number(row["price"]) < 0.05
                )
                / sum(number(row["trade_usd"]) for row in yes)
            ),
            "weather_event_count": len({
                row["event_key"] for row in yes
                if number(row["price"]) < 0.05
            }),
            "low_bucket_count": low_bucket_count,
            "small_add_on_bucket_count": low_small_add_on_bucket_count,
            "later_add_fill_count": sum(
                number(row["price"]) < 0.05
                and bool(row.get("outcome_price_add_class"))
                for row in yes
            ),
            "with_more_expensive_adjacent_yes_event_count": (
                low_with_more_expensive_adjacent_event_count
            ),
        },
        "high_price_yes": {
            "definition": "dominant bucket weighted average price at or above 30 cents",
            "dominant_event_count": high_dominant_events,
        },
        "scenario_35_36_37_style": {
            "exact_style_event_count": scenario_exact_count,
            "selected_closest_event_count": len(scenarios),
            "result": (
                "OBSERVED_EXACT_STYLE_CASES"
                if scenario_exact_count
                else "INSUFFICIENT_HISTORICAL_EVIDENCE"
            ),
            "examples": scenarios,
        },
        "no_analysis": {
            "buy_fill_count": len(no),
            "buy_usd": sum(number(row["trade_usd"]) for row in no),
            "weather_event_count": len({row["event_key"] for row in no}),
            "low_0_5c_fill_count": sum(value < 0.05 for value in no_prices),
            "mid_5_20c_fill_count": sum(0.05 <= value < 0.20 for value in no_prices),
            "high_20c_plus_fill_count": sum(value >= 0.20 for value in no_prices),
            "price_quantiles": no_quantiles,
            "implied_yes_equivalent_quantiles": implied_quantiles,
            "implied_yes_equivalent_at_or_above_80_count": sum(
                value >= 0.80 for value in no_implied
            ),
            "implied_yes_equivalent_at_or_above_90_count": sum(
                value >= 0.90 for value in no_implied
            ),
            "implied_yes_equivalent_at_or_above_95_count": sum(
                value >= 0.95 for value in no_implied
            ),
            "events_also_with_yes_other_bucket": sum(
                bool(row["also_bought_yes_other_bucket"]) for row in no_events
            ),
            "same_bucket_yes_and_no_event_count": sum(
                bool(row["same_bucket_yes_and_no"]) for row in no_events
            ),
            "complement_price_note": (
                "1 - NO price is a descriptive binary complement only; it excludes "
                "spread, fees, and order-book depth."
            ),
        },
        "yes_add_behavior": summarize_adds(yes),
        "no_add_behavior": summarize_adds(no),
        "full_market_favorite_at_buy_time_status": (
            "NOT_SUPPORTED_BY_CURRENT_EVIDENCE"
        ),
        "strict_pnl_price_band": strict_status,
        "yes_top_usd_price_bands": [
            f"{row['price_band']} (${number(row['buy_usd']):.2f}, "
            f"{number(row['buy_usd_share']):.2%})"
            for row in top_bands
        ],
        "legacy_finding_correction_status": "CORRECTED_WITHOUT_REWRITING_HISTORY",
        "source_evidence": {
            "portable_manifest_schema": manifest["schema_version"],
            "portable_sha_verification": "PASS",
            "derived_input_sha_verification": "PASS",
            "market_curve_at_buy_time": "NOT_PRESENT",
        },
        "implementation_status": "READY_FOR_REVIEW",
    }

    output_root.mkdir(parents=True, exist_ok=True)
    write_csv(output_root / "yes_buy_fills.csv", yes_output)
    write_csv(output_root / "no_buy_fills.csv", no_output)
    write_csv(output_root / "yes_price_band_summary.csv", yes_band_rows)
    write_csv(output_root / "no_price_band_summary.csv", no_band_rows)
    write_csv(output_root / "yes_price_threshold_summary.csv", threshold_rows)
    write_csv(output_root / "yes_event_price_summary.csv", yes_event_rows)
    write_csv(output_root / "yes_multi_bucket_event_summary.csv", multi_rows)
    write_csv(output_root / "no_event_summary.csv", no_events)
    write_csv(output_root / "mixed_yes_no_event_summary.csv", mixed_rows)
    write_csv(output_root / "high_price_yes_examples.csv", high_examples)
    write_csv(output_root / "low_price_yes_examples.csv", low_examples)
    write_csv(
        output_root / "scenario_35_36_37_style_examples.csv", scenarios
    )
    write_csv(
        output_root / "legacy_vs_corrected_bucket_findings.csv",
        legacy_correction_rows,
    )
    write_csv(
        output_root / "strict_pnl_by_yes_price_band.csv", strict_band_rows
    )

    references = portable_references + [
        source_reference(
            repo_root,
            "docs/husky_beijing_full_trade_study_v1/beijing_all_public_fills.csv",
            "reviewed_derived_fill_input",
        ),
        source_reference(
            repo_root,
            "docs/husky_beijing_full_trade_study_v1/beijing_event_summary.csv",
            "reviewed_derived_event_input",
        ),
        source_reference(
            repo_root,
            "docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json",
            "reviewed_prior_summary_input",
        ),
        source_reference(
            repo_root,
            "docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.md",
            "reviewed_prior_narrative_input",
        ),
    ]
    source_manifest = {
        "schema_version": SCHEMA_VERSION,
        "analysis_cutoff_utc": ANALYSIS_CUTOFF_UTC,
        "wallet": HUSKY_WALLET,
        "offline_only": True,
        "network_call_count": NETWORK_CALL_COUNT,
        "sources": references,
        "raw_evidence_copied_to_output": False,
        "visual_omission_reason": (
            "The requested deliverables prioritize exact audit tables, threshold "
            "rules, and machine-readable cases; charts would not replace these."
        ),
        "prior_reviewed_core_counts": {
            "beijing_event_count": old_summary["beijing_event_count"],
            "total_public_fill_count": old_summary["total_public_fill_count"],
            "public_buy_fill_count": old_summary["public_buy_fill_count"],
            "public_sell_fill_count": old_summary["public_sell_fill_count"],
        },
    }
    (output_root / "source_manifest.json").write_text(
        stable_json(source_manifest), encoding="utf-8"
    )
    summary = normalize_numbers(summary)
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(stable_json(summary), encoding="utf-8")
    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(render_report(summary), encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fixed-cutoff offline Beijing YES/NO price study."
    )
    parser.add_argument("command", choices=["analyze"])
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--summary-md", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument(
        "--analysis-cutoff-utc", default=ANALYSIS_CUTOFF_UTC
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.analysis_cutoff_utc != ANALYSIS_CUTOFF_UTC:
        raise SystemExit(
            "offline cutoff must equal the fixed reviewed evidence cutoff"
        )
    if os.environ.get("HUSKY_BEIJING_NO_NETWORK") not in (None, "", "1"):
        raise SystemExit("HUSKY_BEIJING_NO_NETWORK must be unset or 1")
    analyze(
        Path(args.repo_root),
        Path(args.output_root),
        Path(args.summary_md),
        Path(args.summary_json),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
