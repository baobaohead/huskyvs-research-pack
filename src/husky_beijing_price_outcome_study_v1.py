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

# Every band is [low, high); the final sentinel is just above 1.0 so that the
# requested 100-cent endpoint is included in the 90—100-cent band.
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
    Decimal("0.80"), Decimal("0.90"),
)
QUANTILES = (0.50, 0.75, 0.90, 0.95, 0.99)
DEFAULT_EVIDENCE_MANIFEST = (
    "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json"
)


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


def normalize_trade_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the two semantic identity fields before any aggregation."""
    normalized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["outcome"] = str(row.get("outcome", "")).strip().upper()
        row["side"] = str(row.get("side", "")).strip().upper()
        if row["outcome"] not in {"YES", "NO"}:
            raise ValueError(f"unsupported outcome: {row['outcome']!r}")
        if row["side"] not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported side: {row['side']!r}")
        normalized.append(row)
    return normalized


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
            "usd_weighted_average_price": (
                sum(
                    number(row["price"]) * number(row["trade_usd"])
                    for row in bucket_rows
                ) / usd
                if usd else None
            ),
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
            f"{outcome.lower()}_buy_fill_count": len(subset),
            "buy_transaction_hash_count": len({
                row["transaction_hash"] for row in subset
            }),
            f"{outcome.lower()}_buy_transaction_hash_count": len({
                row["transaction_hash"] for row in subset
            }),
            "weather_event_count": len({row["event_key"] for row in subset}),
            "temperature_asset_count": len({row["asset"] for row in subset}),
            "asset_count": len({row["asset"] for row in subset}),
            "buy_shares": shares,
            "buy_usd": usd,
            "buy_usd_share": usd / total_usd if total_usd else 0,
            "buy_fill_share": len(subset) / total_fills if total_fills else 0,
            "average_fill_price": statistics.fmean(prices) if prices else None,
            "mean_price": statistics.fmean(prices) if prices else None,
            "median_fill_price": statistics.median(prices) if prices else None,
            "median_price": statistics.median(prices) if prices else None,
            "minimum_price": min(prices) if prices else None,
            "maximum_price": max(prices) if prices else None,
            "max_single_fill_buy_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "maximum_single_fill_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "max_single_event_buy_usd": max(
                (
                    sum(number(row["trade_usd"]) for row in event_rows)
                    for event_rows in by_event.values()
                ),
                default=0,
            ),
            "maximum_single_event_usd": max(
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
    dominant_by_event: dict[str, str] = {}
    for event_key, event_rows in group_rows(rows, "event_key").items():
        amounts = {
            bucket: sum(number(row["trade_usd"]) for row in bucket_rows)
            for bucket, bucket_rows in group_rows(
                event_rows, "temperature_bucket"
            ).items()
        }
        dominant_by_event[event_key] = max(
            amounts, key=lambda bucket: (amounts[bucket], bucket)
        )
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
            "yes_buy_fill_count": len(subset),
            "buy_yes_usd": usd,
            "yes_buy_usd_share": usd / total_usd if total_usd else 0,
            "weather_event_count": len(by_event),
            "temperature_asset_count": len({row["asset"] for row in subset}),
            "asset_count": len({row["asset"] for row in subset}),
            "max_single_fill_buy_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "maximum_single_fill_usd": max(
                (number(row["trade_usd"]) for row in subset), default=0
            ),
            "max_single_event_buy_usd": max(
                (
                    sum(number(row["trade_usd"]) for row in event_rows)
                    for event_rows in by_event.values()
                ),
                default=0,
            ),
            "maximum_single_event_usd": max(
                (
                    sum(number(row["trade_usd"]) for row in event_rows)
                    for event_rows in by_event.values()
                ),
                default=0,
            ),
            "first_trade_date_cst": dates[0] if dates else "",
            "last_trade_date_cst": dates[-1] if dates else "",
            "first_observed_date": dates[0] if dates else "",
            "last_observed_date": dates[-1] if dates else "",
            "dominant_yes_bucket_event_count": len({
                row["event_key"] for row in subset
                if row["temperature_bucket"]
                == dominant_by_event.get(str(row["event_key"]))
            }),
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
        "yes_asset_count": len({row["asset"] for row in yes}),
        "yes_buy_shares": total_shares,
        "yes_buy_usd": total_usd,
        "minimum_yes_buy_price": min(
            (number(row["price"]) for row in yes), default=None
        ),
        "maximum_yes_buy_price": max(
            (number(row["price"]) for row in yes), default=None
        ),
        "usd_weighted_average_yes_buy_price": (
            sum(
                number(row["price"]) * number(row["trade_usd"])
                for row in yes
            ) / total_usd
            if total_usd else None
        ),
        "median_yes_buy_price": (
            statistics.median(number(row["price"]) for row in yes)
            if yes else None
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


def pair_allocation_ratio_label(left_usd: float, right_usd: float) -> str:
    total = left_usd + right_usd
    if total <= 0:
        return "OTHER"
    major = max(left_usd, right_usd) / total
    if major >= 0.85:
        return "APPROX_90_10"
    if major >= 0.75:
        return "APPROX_80_20"
    if major >= 0.65:
        return "APPROX_70_30"
    if major >= 0.55:
        return "APPROX_60_40"
    if major >= 0.45:
        return "APPROX_50_50"
    return "OTHER"


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
        "cheapest_is_dominant": (
            cheap["temperature_bucket"] == dominant["temperature_bucket"]
        ),
        "cheapest_is_small_add_on": number(cheap["buy_usd_share"]) < 0.20,
        "expensive_funding_exceeds_cheapest": expensive["buy_usd"] > cheap["buy_usd"],
        "cheapest_funding_exceeds_expensive": cheap["buy_usd"] > expensive["buy_usd"],
        "allocation_pattern": allocation_label(expensive_share),
        "expensive_vs_cheapest_pair_ratio_class": pair_allocation_ratio_label(
            number(expensive["buy_usd"]), number(cheap["buy_usd"])
        ),
        "has_yes_buy_at_or_above_50c": has_high_50,
        "high_50c_with_cheaper_adjacent_yes": high_with_cheaper_adjacent,
        "yes_bucket_rotation": bucket_rotation(yes),
        "last_dominant_yes_bucket": dominant["temperature_bucket"],
    }


def closest_scenario_examples(
    multi_rows: list[dict[str, Any]], limit: int = 5
) -> tuple[list[dict[str, Any]], int]:
    def range_distance(value: float, low: float, high: float) -> float:
        return low - value if value < low else value - high if value > high else 0.0

    candidates: list[dict[str, Any]] = []
    relaxed_candidates: list[dict[str, Any]] = []
    for event in multi_rows:
        details = [
            item for item in event["bucket_details"]
            if item.get("bucket_kind") == "exact"
        ]
        best_relaxed: dict[str, Any] | None = None
        for high in details:
            for low in details:
                if high is low or not buckets_adjacent(high, low):
                    continue
                high_price = number(high["usd_weighted_average_price"])
                low_price = number(low["usd_weighted_average_price"])
                if high_price < 0.40 or low_price > 0.10:
                    continue
                proposal = {
                    "event_key": event["event_key"],
                    "weather_date": event["weather_date"],
                    "similarity_score": (
                        abs(high_price - 0.55) + abs(low_price - 0.05)
                    ),
                    "strict_style_match": False,
                    "exact_style_match": False,
                    "relaxed_style_match": True,
                    "high_price_bucket": high["temperature_bucket"],
                    "high_bucket_weighted_price": high_price,
                    "high_bucket_buy_usd": high["buy_usd"],
                    "high_bucket_buy_usd_share": high["buy_usd_share"],
                    "adjacent_bucket": low["temperature_bucket"],
                    "adjacent_bucket_weighted_price": low_price,
                    "adjacent_bucket_buy_usd": low["buy_usd"],
                    "adjacent_bucket_buy_usd_share": low["buy_usd_share"],
                    "far_bucket": "",
                    "far_bucket_weighted_price": None,
                    "far_bucket_buy_usd": None,
                    "far_bucket_buy_usd_share": None,
                    "dominant_yes_bucket": event["dominant_yes_bucket_by_usd"],
                    "last_dominant_yes_bucket": event[
                        "last_dominant_yes_bucket"
                    ],
                    "buy_times_cst": {
                        high["temperature_bucket"]: high["first_buy_time_cst"],
                        low["temperature_bucket"]: low["first_buy_time_cst"],
                    },
                    "yes_bucket_rotation": event["yes_bucket_rotation"],
                    "later_adjustment_observed": any(
                        int(item["fill_count"]) > 1 for item in (high, low)
                    ),
                }
                if best_relaxed is None or (
                    proposal["similarity_score"],
                    proposal["event_key"],
                ) < (
                    best_relaxed["similarity_score"],
                    best_relaxed["event_key"],
                ):
                    best_relaxed = proposal
        if best_relaxed is not None:
            relaxed_candidates.append(best_relaxed)
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
                        and max(
                            number(high["bucket_low"]),
                            number(adjacent["bucket_low"]),
                            number(far["bucket_low"]),
                        )
                        - min(
                            number(high["bucket_low"]),
                            number(adjacent["bucket_low"]),
                            number(far["bucket_low"]),
                        )
                        <= 2
                    )
                    proposal = {
                        "event_key": event["event_key"],
                        "weather_date": event["weather_date"],
                        "similarity_score": score,
                        "strict_style_match": exact,
                        "exact_style_match": exact,
                        "relaxed_style_match": False,
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
                        "last_dominant_yes_bucket": event[
                            "last_dominant_yes_bucket"
                        ],
                        "buy_times_cst": {
                            item["temperature_bucket"]: item["first_buy_time_cst"]
                            for item in (high, adjacent, far)
                        },
                        "later_adjustment_observed": any(
                            int(item["fill_count"]) > 1 for item in (high, adjacent, far)
                        ),
                        "yes_bucket_rotation": event["yes_bucket_rotation"],
                    }
                    if best is None or (
                        not proposal["strict_style_match"],
                        proposal["similarity_score"],
                        proposal["event_key"],
                    ) < (
                        not best["strict_style_match"],
                        best["similarity_score"],
                        best["event_key"],
                    ):
                        best = proposal
        if best is not None:
            candidates.append(best)
    strict_candidates = [
        item for item in candidates if item["strict_style_match"]
    ]
    strict_candidates.sort(
        key=lambda item: (
            item["similarity_score"],
            item["event_key"],
        )
    )
    relaxed_candidates.sort(
        key=lambda item: (item["similarity_score"], item["event_key"])
    )
    exact_count = len(strict_candidates)
    selected = strict_candidates if strict_candidates else relaxed_candidates
    return selected[:limit], exact_count


def relaxed_scenario_event_count(
    multi_rows: list[dict[str, Any]],
) -> int:
    count = 0
    for event in multi_rows:
        details = [
            item for item in event["bucket_details"]
            if item.get("bucket_kind") == "exact"
        ]
        if any(
            high is not low
            and buckets_adjacent(high, low)
            and number(high["usd_weighted_average_price"]) >= 0.40
            and number(low["usd_weighted_average_price"]) <= 0.10
            for high in details
            for low in details
        ):
            count += 1
    return count


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
            sum(
                number(row["price"]) * number(row["trade_usd"])
                for row in no
            ) / usd
            if usd else None
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


def mixed_event_row(
    event_key: str,
    event_rows: list[dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    buys = [
        row for row in event_rows if str(row["side"]).upper() == "BUY"
    ]
    yes = [
        row for row in buys if str(row["outcome"]).upper() == "YES"
    ]
    no = [
        row for row in buys if str(row["outcome"]).upper() == "NO"
    ]
    yes_usd = sum(number(row["trade_usd"]) for row in yes)
    no_usd = sum(number(row["trade_usd"]) for row in no)
    total = yes_usd + no_usd
    ordered = sorted(buys, key=lambda row: int(row["timestamp_epoch"]))
    first_yes = min(int(row["timestamp_epoch"]) for row in yes)
    first_no = min(int(row["timestamp_epoch"]) for row in no)
    if first_yes < first_no:
        order = "YES_THEN_NO"
    elif first_no < first_yes:
        order = "NO_THEN_YES"
    else:
        order = "SAME_TIMESTAMP"
    return {
        "event_key": event_key,
        "weather_date": event_key[:10],
        **classification,
        "yes_buy_usd": yes_usd,
        "no_buy_usd": no_usd,
        "yes_buy_usd_share": yes_usd / total if total else None,
        "no_buy_usd_share": no_usd / total if total else None,
        "no_is_majority_of_event_buy_usd": no_usd > yes_usd,
        "first_outcome_order": order,
        "buy_sequence": [
            {
                "timestamp_cst": row["public_trade_time_cst"],
                "temperature_bucket": row["temperature_bucket"],
                "outcome": str(row["outcome"]).upper(),
                "price": number(row["price"]),
                "implied_yes_equivalent_price": (
                    1 - number(row["price"])
                    if str(row["outcome"]).upper() == "NO" else None
                ),
                "trade_usd": number(row["trade_usd"]),
            }
            for row in ordered
        ],
        "yes_prices": [number(row["price"]) for row in yes],
        "no_prices": [number(row["price"]) for row in no],
        "no_implied_yes_equivalent_prices": [
            1 - number(row["price"]) for row in no
        ],
    }


def no_implied_threshold_summary(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for threshold in (0.80, 0.90, 0.95, 0.99):
        subset = [
            row for row in rows
            if 1 - number(row["price"]) + EPSILON >= threshold
        ]
        result.append({
            "implied_yes_equivalent_threshold_inclusive": threshold,
            "buy_no_fill_count": len(subset),
            "buy_no_usd": sum(number(row["trade_usd"]) for row in subset),
            "weather_event_count": len({
                row["event_key"] for row in subset
            }),
        })
    return result


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
            "bucket_kind": row.get("bucket_kind", ""),
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
            "bucket_share_of_event_yes_buy_usd": (
                number(current["buy_usd"]) / event_total
                if event_total else None
            ),
            "bucket_is_event_dominant_yes": (
                current["temperature_bucket"] == dominant["temperature_bucket"]
            ),
            "is_event_dominant_yes_bucket": (
                current["temperature_bucket"] == dominant["temperature_bucket"]
            ),
            "same_event_bought_other_yes_bucket": len(details) > 1,
            "other_yes_buckets_in_event": [
                item["temperature_bucket"] for item in details
                if item["temperature_bucket"] != row["temperature_bucket"]
            ],
            "same_event_bought_no": any(
                str(item["side"]).upper() == "BUY"
                and str(item["outcome"]).upper() == "NO"
                for item in fills_by_event[row["event_key"]]
            ),
            "no_buys_present_in_event": any(
                str(item["side"]).upper() == "BUY"
                and str(item["outcome"]).upper() == "NO"
                for item in fills_by_event[row["event_key"]]
            ),
            "transaction_hash": row["transaction_hash"],
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
            "threshold_ge_80c": number(row["price"]) >= 0.80,
            "entry_timeline_status": context["entry_path_completeness"],
            "strict_pnl_status": (
                "AVAILABLE" if context["strict_pnl_available"]
                else "UNAVAILABLE"
            ),
            "strict_event_pnl": context["strict_pnl"],
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
            "condition_id": asset_rows[0]["condition_id"],
            "temperature_bucket": asset_rows[0]["temperature_bucket"],
            "outcome": "YES",
            "buy_shares": shares,
            "average_buy_price": public_average,
            "buy_usd": usd,
            "strict_realized_pnl": number(closed.get("realizedPnl")),
            "roi": (
                number(closed.get("realizedPnl")) / usd if usd else None
            ),
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
            "strict_asset_count": len(assets),
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
            if reliable
            else "INSUFFICIENT_AUTHORITATIVE_ASSET_MATCH"
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


def verify_sources(
    repo_root: Path,
    evidence_manifest: str = DEFAULT_EVIDENCE_MANIFEST,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    relative_manifest = Path(evidence_manifest)
    if relative_manifest.is_absolute() or ".." in relative_manifest.parts:
        raise RuntimeError("UNSAFE_PORTABLE_EVIDENCE_MANIFEST_PATH")
    manifest_path = repo_root / relative_manifest
    if not manifest_path.is_file():
        raise RuntimeError("PORTABLE_EVIDENCE_MANIFEST_NOT_FOUND")
    portable_root = manifest_path.parent
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
    def validate_strings(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                validate_strings(child)
        elif isinstance(value, list):
            for child in value:
                validate_strings(child)
        elif isinstance(value, str):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("UNSAFE_PATH_IN_PORTABLE_EVIDENCE_MANIFEST")

    validate_strings(manifest)
    references = [{
        "relative_path": relative_manifest.as_posix(),
        "manifest_sha256": sha256_file(manifest_path),
        "role": "portable_evidence_manifest",
    }]
    for name, meta in sorted(manifest.get("aggregates", {}).items()):
        relative = str(meta.get("relative_path", ""))
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise RuntimeError(f"UNSAFE_PORTABLE_EVIDENCE_PATH:{name}")
        path = portable_root / relative
        if not path.is_file():
            raise RuntimeError(f"PORTABLE_EVIDENCE_FILE_NOT_FOUND:{name}")
        actual = sha256_file(path)
        if actual != meta.get("sha256"):
            raise RuntimeError(f"PORTABLE_EVIDENCE_SHA_MISMATCH:{name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        actual_count = len(payload) if isinstance(payload, list) else 1
        if actual_count != int(meta.get("record_count", -1)):
            raise RuntimeError(
                f"PORTABLE_EVIDENCE_RECORD_COUNT_MISMATCH:{name}"
            )
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
        "below_5c": lambda value: value < 0.05,
        "5_to_10c": lambda value: 0.05 <= value < 0.10,
        "10_to_20c": lambda value: 0.10 <= value < 0.20,
        "20_to_30c": lambda value: 0.20 <= value < 0.30,
        "30_to_50c": lambda value: 0.30 <= value < 0.50,
        "at_or_above_50c": lambda value: value >= 0.50,
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


def add_behavior_rows(
    rows: list[dict[str, Any]], outcome: str
) -> list[dict[str, Any]]:
    definitions = (
        ("<5美分", lambda value: value < 0.05),
        ("5—10美分", lambda value: 0.05 <= value < 0.10),
        ("10—20美分", lambda value: 0.10 <= value < 0.20),
        ("20—30美分", lambda value: 0.20 <= value < 0.30),
        ("30—50美分", lambda value: 0.30 <= value < 0.50),
        (">=50美分", lambda value: value >= 0.50),
    )
    result = []
    for label, predicate in definitions:
        subset = [row for row in rows if predicate(number(row["price"]))]
        price_counts = Counter(
            row["outcome_price_add_class"] for row in subset
            if row.get("outcome_price_add_class")
        )
        average_counts = Counter(
            row["outcome_average_cost_add_class"] for row in subset
            if row.get("outcome_average_cost_add_class")
        )
        result.append({
            "outcome": outcome,
            "price_band": label,
            "buy_fill_count": len(subset),
            "subsequent_buy_fill_count": sum(price_counts.values()),
            f"{outcome.lower()}_price_up_add_count": price_counts[
                "PRICE_UP_ADD"
            ],
            f"{outcome.lower()}_price_down_add_count": price_counts[
                "PRICE_DOWN_ADD"
            ],
            f"{outcome.lower()}_price_flat_add_count": price_counts[
                "PRICE_FLAT_ADD"
            ],
            f"{outcome.lower()}_above_average_cost_add_count": average_counts[
                "ABOVE_AVERAGE_COST_ADD"
            ],
            f"{outcome.lower()}_below_average_cost_add_count": average_counts[
                "BELOW_AVERAGE_COST_ADD"
            ],
            f"{outcome.lower()}_near_average_cost_add_count": average_counts[
                "NEAR_AVERAGE_COST_ADD"
            ],
        })
    return result


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


def _render_report_legacy(summary: dict[str, Any]) -> str:
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


def render_report(summary: dict[str, Any]) -> str:
    """Render the required plain-language, 36-question review report."""
    s = summary
    bands = {row["price_band"]: row for row in s["yes_price_band_summary"]}

    def combine(labels: list[str]) -> tuple[int, float, float]:
        rows = [bands[label] for label in labels]
        return (
            sum(int(row["buy_fill_count"]) for row in rows),
            sum(number(row["buy_usd"]) for row in rows),
            sum(number(row["buy_usd_share"]) for row in rows),
        )

    below5 = combine(["0—1美分", "1—2美分", "2—5美分"])
    below10 = combine(["0—1美分", "1—2美分", "2—5美分", "5—10美分"])
    ten20 = combine(["10—15美分", "15—20美分"])
    twenty30 = combine(["20—30美分"])
    thirty50 = combine(["30—40美分", "40—50美分"])
    over50 = combine([
        "50—60美分", "60—70美分", "70—80美分",
        "80—90美分", "90—100美分",
    ])
    q = s["yes_price_quantiles"]["usd_weighted"]
    ceiling = s["price_ceiling"]
    absolute = ceiling["absolute_max_yes_buy_example"]
    allocation = s["multi_yes_allocation"]
    scenario = s["scenario_35_36_37_style"]
    no = s["no_analysis"]
    adds = s["yes_add_behavior"]["price_add_counts"]
    strict = {
        row["yes_average_buy_price_band"]: row
        for row in s["strict_pnl_by_yes_price_band"]
    }
    threshold_by_cents = {
        round(number(row["threshold_cents_inclusive"])): row
        for row in s["yes_price_threshold_summary"]
    }

    def cents(value: Any) -> str:
        return (
            str(value) if isinstance(value, str)
            else f"{number(value) * 100:.2f} 美分"
        )

    lines = [
        "# Husky 北京 YES/NO 买价与上限深度研究",
        "",
        "## 一句话结论",
        "",
        (
            f"**[OBSERVED] 固定样本里 Husky 没有买过 50 美分及以上的 YES；"
            f"绝对最高是 {cents(ceiling['absolute_max_yes_buy_price'])}，"
            f"按预注册稀疏性规则，{cents(ceiling['basically_no_buy_above_price'])}"
            "及以上可称为“历史上基本不买”。这只是历史成交上限，不是未来硬规则。**"
        ),
        "",
        "## 36 个核心问题：逐条回答",
        "",
        (
            "1. **[OBSERVED] 之前“40天多档、31天相邻档”具体错在哪里？** "
            "旧方法把 BUY YES 与 BUY NO 只按 `temperature_bucket` 合并；"
            "排除某温度的 NO 不能算成押该温度会发生。"
        ),
        (
            f"2. **[OBSERVED] 拆开后真正买多个 YES 温度多少天？** "
            f"{s['corrected_bucket_statistics']['yes_multi_bucket_event_count']} 天。"
        ),
        (
            f"3. **[OBSERVED] 真正买相邻 YES 组合多少天？** "
            f"{s['corrected_bucket_statistics']['yes_adjacent_basket_event_count']} 天。"
        ),
        f"4. **[OBSERVED] 只买 YES 多少天？** {s['event_structures']['YES_ONLY']} 天。",
        f"5. **[OBSERVED] 只买 NO 多少天？** {s['event_structures']['NO_ONLY']} 天。",
        (
            f"6. **[OBSERVED] 同时买 YES 和 NO 多少天？** "
            f"{s['event_structures']['MIXED_YES_AND_NO']} 天；仅描述成交结构，不推断动机。"
        ),
        (
            f"7. **[OBSERVED] BUY YES 有多少笔、投入多少？** "
            f"{s['buy_yes_fill_count']} 笔，${s['buy_yes_usd']:.2f}。"
        ),
        (
            f"8. **[OBSERVED] BUY NO 有多少笔、投入多少？** "
            f"{s['buy_no_fill_count']} 笔，${s['buy_no_usd']:.2f}。"
        ),
        (
            "9. **[OBSERVED] YES 买价主要在哪些区间？** 按实际 BUY USD，前三档是 "
            f"{'；'.join(s['yes_top_usd_price_bands'])}。"
        ),
        (
            f"10. **[OBSERVED] 低于5美分占多少笔和资金？** {below5[0]} 笔，"
            f"${below5[1]:.2f}，占 YES 资金 {below5[2]:.2%}。"
        ),
        (
            f"11. **[OBSERVED] 低于10美分占多少资金？** "
            f"${below10[1]:.2f}，占 {below10[2]:.2%}。"
        ),
        (
            f"12. **[OBSERVED] 10—20美分占多少？** "
            f"{ten20[0]} 笔、${ten20[1]:.2f}，占 {ten20[2]:.2%}。"
        ),
        (
            f"13. **[OBSERVED] 20—30美分占多少？** "
            f"{twenty30[0]} 笔、${twenty30[1]:.2f}，占 {twenty30[2]:.2%}。"
        ),
        (
            f"14. **[OBSERVED] 30—50美分占多少？** "
            f"{thirty50[0]} 笔、${thirty50[1]:.2f}，占 {thirty50[2]:.2%}。"
        ),
        (
            f"15. **[OBSERVED] 50美分以上占多少？** "
            f"{over50[0]} 笔、${over50[1]:.2f}，占 {over50[2]:.2%}。"
        ),
        (
            f"16. **[OBSERVED] 历史最高买过多少 YES？** "
            f"{cents(ceiling['absolute_max_yes_buy_price'])}。"
        ),
        (
            f"17. **[OBSERVED] 最高价是试仓还是主力？** 该笔 ${absolute['trade_usd']:.2f}，"
            f"占当日 YES 投入 {absolute['fill_share_of_event_yes_buy_usd']:.2%}，"
            "且是该事件主导 YES 档，不是几毛钱试仓。"
        ),
        f"18. **[OBSERVED] 50%的 YES 资金买在多少以下？** {cents(q['p50'])}。",
        f"19. **[OBSERVED] 90%的 YES 资金买在多少以下？** {cents(q['p90'])}。",
        f"20. **[OBSERVED] 95%的 YES 资金买在多少以下？** {cents(q['p95'])}。",
        f"21. **[OBSERVED] 99%的 YES 资金买在多少以下？** {cents(q['p99'])}。",
        (
            f"22. **[OBSERVED] 是否存在清晰的“超过某价基本不买”？** "
            f"有：{cents(ceiling['basically_no_buy_above_price'])}。阈值以上资金≤1%、"
            "最多2个事件、且无单事件投入超过$5。"
        ),
        (
            f"23. **[OBSERVED] 有重复性和资金意义的最高买价？** "
            f"{cents(ceiling['meaningful_max_yes_buy_price'])}；它要求累计至少$5且至少3个事件。"
        ),
        (
            f"24. **[OBSERVED] 多 YES 中贵档和便宜档分别投入多少？** "
            f"跨事件合计贵档 ${allocation['expensive_bucket_buy_usd_total']:.2f}、"
            f"便宜档 ${allocation['cheap_bucket_buy_usd_total']:.2f}；逐事件中位资金占比为 "
            f"{allocation['median_expensive_bucket_buy_usd_share']:.2%} 与 "
            f"{allocation['median_cheapest_bucket_buy_usd_share']:.2%}。"
        ),
        (
            f"25. **[OBSERVED] 最贵 YES 档通常是投入最多档吗？** "
            f"是，{allocation['most_expensive_is_dominant_rate']:.2%} 的多 YES 事件如此。"
        ),
        (
            f"26. **[OBSERVED] 最便宜档通常是主力还是附加？** "
            f"最便宜档成为主导档的比例为 {allocation['cheapest_is_dominant_rate']:.2%}；"
            "其余更多表现为非主导附加档，但逐事件明细仍有差异。"
        ),
        (
            f"27. **[OBSERVED] 买过50—60美分 YES 吗？** "
            f"{bands['50—60美分']['buy_fill_count']} 笔，"
            f"${bands['50—60美分']['buy_usd']:.2f}。"
        ),
        (
            f"28. **[OBSERVED] 买50美分以上时是否同时买便宜相邻 YES？** "
            f"50美分以上事件 {allocation['yes_50c_plus_event_count']} 个，其中带更便宜相邻档 "
            f"{allocation['yes_50c_plus_with_cheaper_adjacent_event_count']} 个。"
        ),
        (
            f"29. **[{scenario['result'] if scenario['result'] != 'NONE_OBSERVED' else 'UNKNOWN'}] "
            f"有55+20+1美分式真实结构吗？** 严格事件 "
            f"{scenario['strict_style_event_count']} 个、放宽事件 "
            f"{scenario['relaxed_style_event_count']} 个；场景结果 `{scenario['result']}`。"
        ),
        (
            f"30. **[OBSERVED] 面对高价 YES 是继续买、少量买还是基本不买？** "
            f"30美分以上仍有 {threshold_by_cents[30]['buy_yes_fill_count']} 笔、"
            f"${threshold_by_cents[30]['buy_yes_usd']:.2f}；40美分以上有 "
            f"{threshold_by_cents[40]['buy_yes_fill_count']} 笔、"
            f"${threshold_by_cents[40]['buy_yes_usd']:.2f}；50美分以上为0。"
            f"后续加仓中上涨/下跌/近乎不变分别为 "
            f"{adds.get('PRICE_UP_ADD', 0)}/{adds.get('PRICE_DOWN_ADD', 0)}/"
            f"{adds.get('PRICE_FLAT_ADD', 0)} 笔。"
        ),
        (
            f"31. **[INFERRED] 便宜 YES 是主要资金策略还是 shares 虚高？** "
            f"低于5美分占 {below5[0] / s['buy_yes_fill_count']:.2%} 的 fill、"
            f"{s['low_price_yes']['buy_shares_share']:.2%} 的 shares、"
            f"但只占 {below5[2]:.2%} 的资金，并成为主导 YES 档 "
            f"{s['low_price_yes']['dominant_event_count']} 次；不是纯彩票，但 shares 对资金权重有夸大。"
        ),
        (
            "32. **[OBSERVED] 低价 NO 代表什么？** 它是低价买“该温度不会发生”；"
            "`1-NO价` 只是同一二元市场的描述性互补价，不含价差、手续费、流动性与可成交深度。"
        ),
        (
            f"33. **[OBSERVED] 是否经常用低价 NO 排除高 YES 等价温度？** "
            f"互补 YES 等价≥90% 的 BUY NO 有 "
            f"{no['implied_yes_equivalent_at_or_above_90_count']} 笔，≥95% 有 "
            f"{no['implied_yes_equivalent_at_or_above_95_count']} 笔；"
            "这只能说明排除成交频繁，不能证明逆市场。"
        ),
        (
            "34. **[NOT_SUPPORTED] 当前能证明当时哪个温度是市场主档吗？** 不能；"
            "`FULL_MARKET_FAVORITE_AT_BUY_TIME_STATUS=NOT_SUPPORTED_BY_CURRENT_EVIDENCE`。"
        ),
        (
            f"35. **[OBSERVED] 价格带和盈亏有什么关联？** 仅有 "
            f"{s['strict_pnl_price_band']['strict_aligned_yes_asset_count']} 个 YES 资产能严格对齐；"
            f"2—5美分档 PnL 为 ${strict['2—5美分']['strict_realized_pnl']:.2f}，"
            f"但最大赢家占其正 PnL {strict['2—5美分']['largest_winner_share_of_positive_pnl']:.2%}；"
            f"30—40美分档只有 {strict['30—40美分']['strict_asset_count']} 个资产，"
            f"PnL ${strict['30—40美分']['strict_realized_pnl']:.2f}。"
            "样本稀疏且受单个赢家影响，不能推出价格导致盈亏。"
        ),
        (
            "36. **[NOT_SUPPORTED] 哪些结论仍无法证明？** 无法证明完整市场概率曲线、"
            "当时市场主档、未成交意图、套利/对冲动机、高低价与盈亏的因果关系，以及未来必然买价。"
        ),
        "",
        "## 方法、口径与可审计文件",
        "",
        "- 逻辑身份至少包含 event、condition、asset、temperature bucket、outcome 与 side；BUY YES、BUY NO、SELL YES、SELL NO 严格分开。",
        "- 价格分箱为左闭右开，最后一档含100美分；主判断使用实际 BUY USD，不以 shares 代替资金。",
        "- `high_price_yes_examples.csv` 列出所有 BUY YES ≥30美分；`yes_multi_bucket_event_summary.csv` 保存贵档、便宜档、顺序与资金比例。",
        "- `mixed_yes_no_event_summary.csv` 仅描述同事件成交顺序和资金结构，不解释主观动机。",
        "",
        "## 严格限制",
        "",
        "- 固定截止时间：`2026-07-29T03:30:01.944885+00:00`；50个事件、537笔公开成交。",
        "- 36个 resolved-redeemable 快照未进入严格 PnL；事件总 PnL 未重复分摊到价格带。",
        "- 全程离线，无账户连接、无签名、无下单、未启动 formal。",
        "",
    ]
    return "\n".join(lines)


def analyze(
    repo_root: Path,
    output_root: Path,
    summary_md: Path,
    summary_json: Path,
    evidence_manifest: str = DEFAULT_EVIDENCE_MANIFEST,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    manifest, portable_references = verify_sources(
        repo_root, evidence_manifest
    )
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
    fills = annotate_outcome_adds(
        normalize_trade_rows(read_csv(fill_path))
    )
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

    legacy_single = 0
    legacy_multi = 0
    legacy_adjacent = 0
    legacy_rotation = 0
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
        if legacy_count == 1:
            legacy_single += 1
        elif legacy_count >= 2:
            legacy_multi += 1
        if has_adjacent_pair(event_buys):
            legacy_adjacent += 1
        if bucket_rotation(event_buys):
            legacy_rotation += 1
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
        mixed_event_row(
            event_key,
            fills_by_event[event_key],
            classifications[event_key],
        )
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
    scenario_relaxed_count = relaxed_scenario_event_count(multi_rows)
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
    cheapest_is_dominant_count = sum(
        bool(row["cheapest_is_dominant"]) for row in multi_rows
    )
    low_price_band_analysis = []
    for label in ("0—1美分", "1—2美分", "2—5美分", "5—10美分"):
        band_fills = [
            row for row in yes if price_band(row["price"]) == label
        ]
        band_by_event = group_rows(band_fills, "event_key")
        dominant_events = 0
        for event_key in band_by_event:
            event_details = bucket_details([
                row for row in fills_by_event[event_key]
                if str(row["side"]).upper() == "BUY"
                and str(row["outcome"]).upper() == "YES"
            ])
            if not event_details:
                continue
            dominant = max(
                event_details,
                key=lambda item: (item["buy_usd"], item["temperature_bucket"]),
            )
            if any(
                row["temperature_bucket"] == dominant["temperature_bucket"]
                for row in band_by_event[event_key]
            ):
                dominant_events += 1
        event_usd = [
            sum(number(row["trade_usd"]) for row in event_rows)
            for event_rows in band_by_event.values()
        ]
        with_more_expensive_adjacent = sum(
            any(
                buckets_adjacent(low_row, other_row)
                and number(other_row["price"]) > number(low_row["price"])
                for low_row in band_event_rows
                for other_row in fills_by_event[event_key]
                if str(other_row["side"]).upper() == "BUY"
                and str(other_row["outcome"]).upper() == "YES"
            )
            for event_key, band_event_rows in band_by_event.items()
        )
        low_price_band_analysis.append({
            "price_band": label,
            "fill_count": len(band_fills),
            "shares": sum(number(row["shares"]) for row in band_fills),
            "buy_usd": sum(number(row["trade_usd"]) for row in band_fills),
            "buy_usd_share": (
                sum(number(row["trade_usd"]) for row in band_fills)
                / sum(number(row["trade_usd"]) for row in yes)
            ),
            "event_count": len(band_by_event),
            "asset_count": len({row["asset"] for row in band_fills}),
            "dominant_yes_bucket_event_count": dominant_events,
            "non_dominant_add_on_event_count": (
                len(band_by_event) - dominant_events
            ),
            "median_single_event_buy_usd": (
                statistics.median(event_usd) if event_usd else None
            ),
            "maximum_single_event_buy_usd": (
                max(event_usd) if event_usd else 0
            ),
            "later_add_fill_count": sum(
                bool(row.get("outcome_price_add_class"))
                for row in band_fills
            ),
            "with_more_expensive_adjacent_yes_event_count": (
                with_more_expensive_adjacent
            ),
        })

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
    no_usd_quantiles = {
        f"p{int(q * 100)}": weighted_quantile(no, q)
        for q in (0.50, 0.95)
    }
    no_implied_threshold_rows = no_implied_threshold_summary(no)
    yes_add_rows = add_behavior_rows(yes, "YES")
    no_add_rows = add_behavior_rows(no, "NO")

    legacy_correction_rows = [{
        "legacy_total_event_count": len(old_event_rows),
        "legacy_single_bucket_event_count": legacy_single,
        "legacy_multi_bucket_event_count": legacy_multi,
        "legacy_adjacent_bucket_event_count": legacy_adjacent,
        "legacy_bucket_rotation_event_count": legacy_rotation,
        "corrected_yes_buy_event_count": len({
            row["event_key"] for row in yes
        }),
        "corrected_yes_multi_bucket_event_count": yes_multi,
        "corrected_yes_adjacent_basket_event_count": yes_adjacent,
        "corrected_yes_bucket_rotation_event_count": yes_rotation,
        "no_buy_event_count": len({row["event_key"] for row in no}),
        "no_multi_bucket_exclusion_event_count": no_multi,
        "yes_only_event_count": event_structures["YES_ONLY"],
        "no_only_event_count": event_structures["NO_ONLY"],
        "mixed_yes_no_event_count": event_structures["MIXED_YES_AND_NO"],
        "same_bucket_both_sides_event_count": sum(
            item["same_bucket_both_sides"]
            for item in classifications.values()
        ),
        "cross_bucket_yes_no_event_count": sum(
            item["cross_bucket_yes_no"]
            for item in classifications.values()
        ),
        "correction_note": (
            "Legacy counts group temperature_bucket across BUY YES and BUY NO; "
            "corrected YES baskets use BUY YES only."
        ),
    }]

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
            "single_bucket_event_count": legacy_single,
            "multi_bucket_event_count": legacy_multi,
            "adjacent_bucket_event_count": legacy_adjacent,
            "bucket_rotation_event_count": legacy_rotation,
        },
        "corrected_bucket_statistics": {
            "yes_buy_event_count": len({
                row["event_key"] for row in yes
            }),
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
            "no_buy_event_count": len({
                row["event_key"] for row in no
            }),
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
            "data_completeness_gate": "PASS",
            "ceiling_result_caused_by_missing_data": False,
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
            "cheapest_is_dominant_count": cheapest_is_dominant_count,
            "cheapest_is_dominant_rate": (
                cheapest_is_dominant_count / len(multi_rows)
                if multi_rows else 0
            ),
            "median_expensive_bucket_buy_usd_share": (
                statistics.median(
                    number(row["expensive_bucket_buy_usd_share"])
                    for row in multi_rows
                ) if multi_rows else None
            ),
            "median_cheapest_bucket_buy_usd_share": (
                statistics.median(
                    number(row["cheap_bucket_buy_usd_share"])
                    for row in multi_rows
                ) if multi_rows else None
            ),
            "expensive_funding_exceeds_cheapest_event_count": sum(
                bool(row["expensive_funding_exceeds_cheapest"])
                for row in multi_rows
            ),
            "cheapest_funding_exceeds_expensive_event_count": sum(
                bool(row["cheapest_funding_exceeds_expensive"])
                for row in multi_rows
            ),
            "allocation_pattern_counts": dict(Counter(
                row["allocation_pattern"] for row in multi_rows
            )),
            "expensive_vs_cheapest_pair_ratio_counts": dict(Counter(
                row["expensive_vs_cheapest_pair_ratio_class"]
                for row in multi_rows
            )),
            "yes_50c_plus_event_count": sum(
                bool(row["has_yes_buy_at_or_above_50c"])
                for row in multi_rows
            ),
            "yes_50c_plus_with_cheaper_adjacent_event_count": sum(
                bool(row["high_50c_with_cheaper_adjacent_yes"])
                for row in multi_rows
            ),
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
        "low_price_yes_by_band": low_price_band_analysis,
        "high_price_yes": {
            "definition": "dominant bucket weighted average price at or above 30 cents",
            "dominant_event_count": high_dominant_events,
        },
        "scenario_35_36_37_style": {
            "strict_style_event_count": scenario_exact_count,
            "exact_style_event_count": scenario_exact_count,
            "relaxed_style_event_count": scenario_relaxed_count,
            "selected_closest_event_count": len(scenarios),
            "result": (
                "OBSERVED"
                if scenario_exact_count
                else "INFERRED"
                if scenario_relaxed_count
                else "NONE_OBSERVED"
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
            "usd_weighted_price_quantiles": no_usd_quantiles,
            "implied_yes_equivalent_quantiles": implied_quantiles,
            "implied_yes_equivalent_threshold_summary": (
                no_implied_threshold_rows
            ),
            "implied_yes_equivalent_at_or_above_80_count": sum(
                value >= 0.80 for value in no_implied
            ),
            "implied_yes_equivalent_at_or_above_90_count": sum(
                value >= 0.90 for value in no_implied
            ),
            "implied_yes_equivalent_at_or_above_95_count": sum(
                value >= 0.95 for value in no_implied
            ),
            "implied_yes_equivalent_at_or_above_99_count": sum(
                value >= 0.99 for value in no_implied
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
        "yes_add_behavior_by_price_band": yes_add_rows,
        "no_add_behavior_by_price_band": no_add_rows,
        "full_market_favorite_at_buy_time_status": (
            "NOT_SUPPORTED_BY_CURRENT_EVIDENCE"
        ),
        "strict_pnl_price_band": strict_status,
        "strict_pnl_by_yes_price_band": strict_band_rows,
        "yes_top_usd_price_bands": [
            f"{row['price_band']} (${number(row['buy_usd']):.2f}, "
            f"{number(row['buy_usd_share']):.2%})"
            for row in top_bands
        ],
        "legacy_finding_correction_status": "CORRECTED_WITHOUT_REWRITING_HISTORY",
        "source_evidence": {
            "portable_manifest_schema": manifest["schema_version"],
            "portable_sha_verification": "PASS",
            "derived_input_status": (
                "REVIEWED_MAIN_INPUTS_WITH_FIXED_CORE_COUNT_GATES"
            ),
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
        output_root / "scenario_35_36_37_style_examples.csv",
        scenarios,
        fields=[
            "weather_date", "event_key", "strict_style_match",
            "relaxed_style_match", "similarity_score",
            "high_price_bucket", "high_bucket_weighted_price",
            "high_bucket_buy_usd", "high_bucket_buy_usd_share",
            "adjacent_bucket", "adjacent_bucket_weighted_price",
            "adjacent_bucket_buy_usd", "adjacent_bucket_buy_usd_share",
            "far_bucket", "far_bucket_weighted_price",
            "far_bucket_buy_usd", "far_bucket_buy_usd_share",
            "buy_times_cst", "dominant_yes_bucket",
            "last_dominant_yes_bucket", "yes_bucket_rotation",
            "later_adjustment_observed", "entry_path_completeness",
            "strict_pnl_available", "strict_pnl",
        ],
    )
    write_csv(
        output_root / "legacy_vs_corrected_bucket_findings.csv",
        legacy_correction_rows,
    )
    write_csv(
        output_root / "strict_pnl_by_yes_price_band.csv", strict_band_rows
    )
    write_csv(
        output_root / "yes_add_behavior_summary.csv", yes_add_rows
    )
    write_csv(
        output_root / "no_add_behavior_summary.csv", no_add_rows
    )

    manifest_reference = portable_references[0]
    source_manifest = {
        "schema_version": SCHEMA_VERSION,
        "analysis_cutoff_utc": ANALYSIS_CUTOFF_UTC,
        "wallet": HUSKY_WALLET,
        "offline_only": True,
        "network_call_count": NETWORK_CALL_COUNT,
        "relative_path": manifest_reference["relative_path"],
        "manifest_sha256": manifest_reference["manifest_sha256"],
        "source_record_counts": {
            name: int(meta["record_count"])
            for name, meta in manifest["aggregates"].items()
        },
        "raw_evidence_copied_to_output": False,
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
        "--evidence-manifest",
        default=DEFAULT_EVIDENCE_MANIFEST,
    )
    parser.add_argument("--offline-only", action="store_true")
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
    if not args.offline_only:
        raise SystemExit("--offline-only is required")
    if os.environ.get("HUSKY_BEIJING_NO_NETWORK") != "1":
        raise SystemExit("HUSKY_BEIJING_NO_NETWORK must equal 1")
    analyze(
        Path(args.repo_root),
        Path(args.output_root),
        Path(args.summary_md),
        Path(args.summary_json),
        args.evidence_manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
