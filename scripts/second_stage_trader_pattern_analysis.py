#!/usr/bin/env python3
"""Second-stage, local-only path analysis for Beijing highest-temperature fills.

This script deliberately consumes the existing first-stage ``all_fills.csv`` and
``_public_evidence`` files. It never calls a network endpoint. Price buckets
come from the fixed first-stage implementation so the registered buckets are
not recreated or changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.polymarket_highest_temperature_trader_pattern_v1 import price_band


WALLETS = (
    "0x7c63520c2ca9b336af0c205b9ccf68217bb393d4",
    "0x8fbd7cf5f806f563080864694415829f7229a959",
)
PRICE_BANDS = (
    "PRICE_0_10C",
    "PRICE_10_30C",
    "PRICE_30_70C",
    "PRICE_70_90C",
    "PRICE_90_100C",
)
PRICE_NAMES = {
    "PRICE_0_10C": "0—10美分",
    "PRICE_10_30C": "10—30美分",
    "PRICE_30_70C": "30—70美分",
    "PRICE_70_90C": "70—90美分",
    "PRICE_90_100C": "90—100美分",
}
TIME_ROWS = (
    "D-2",
    "D-1",
    "D0",
    "D0_00_08",
    "D0_08_12",
    "D0_12_16",
    "D0_16_24",
    "POST_EVENT",
)
TIME_NAMES = {
    "D-2": "D-2",
    "D-1": "D-1",
    "D0": "D0",
    "D0_00_08": "D0 00—08",
    "D0_08_12": "D0 08—12",
    "D0_12_16": "D0 12—16",
    "D0_16_24": "D0 16—24",
    "POST_EVENT": "POST_EVENT",
}
PATH_KEY_FIELDS = (
    "weather_date_local",
    "condition_id",
    "asset",
    "outcome",
    "temperature_bucket",
)
CATEGORY_ORDER = (
    "SINGLE_YES_ONLY",
    "MULTI_YES_ONLY",
    "SINGLE_NO_ONLY",
    "MULTI_NO_ONLY",
    "SINGLE_YES_PLUS_NO",
    "MULTI_YES_PLUS_NO",
    "NO_BUY",
)
SHORT_HOLD_HOURS = 6.0
SECOND_STAGE_NETWORK_CALL_COUNT = 0

# Second-stage thresholds are explicit so the style labels and observed-exit
# classifications are reproducible rather than wallet-specific judgments.
LOW_BUY_BANDS = {"PRICE_0_10C", "PRICE_10_30C"}
HIGH_SELL_BAND = "PRICE_90_100C"
OBSERVED_EXIT_PARTIAL_MAX = 0.95
OBSERVED_EXIT_NEAR_FULL_MAX = 1.05
STYLE_BUY_DOMINANT_MIN_BUY_FILL_SHARE = 0.80
STYLE_BUY_DOMINANT_MAX_SELL_TO_BUY_FILL_RATIO = 0.20
STYLE_ACTIVE_MIN_REPEATED_ASSET_SHARE = 0.25
STYLE_ACTIVE_MIN_SELL_REBUY_RATIO = 0.20
STYLE_ACTIVE_MIN_SAME_HOUR_TWO_WAY = 10
STYLE_MARKET_MIN_SAME_HOUR_TWO_WAY = 20
STYLE_MARKET_MIN_SHORT_HOLD_RATIO = 0.50
STYLE_MARKET_MIN_SELL_REBUY_RATIO = 0.30


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def as_float(value: Any) -> float:
    return float(value or 0.0)


def pct(value: float, denominator: float) -> float:
    return value / denominator * 100.0 if denominator else 0.0


def fmt_num(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    number = float(value)
    if math.isnan(number):
        return "—"
    return f"{number:,.{digits}f}"


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def fmt_price(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}¢"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def fmt_hours(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds / 3600:.2f}h"


def fmt_duration_signed(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    return f"{sign}{abs(seconds) / 3600:.2f}h"


def esc(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(esc(value) for value in row) + " |")
    return "\n".join(lines)


def details(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"


def parse_local_datetime(row: dict[str, Any]) -> datetime:
    value = str(row["trade_time_market_local"])
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_fills(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        normalized = dict(row)
        normalized["_ts"] = int(float(row["timestamp_epoch"]))
        normalized["_utc_dt"] = datetime.fromtimestamp(normalized["_ts"], tz=timezone.utc)
        normalized["_local_dt"] = parse_local_datetime(row)
        normalized["_price"] = as_float(row["price"])
        normalized["_shares"] = as_float(row["shares"])
        normalized["_usd"] = as_float(row["trade_usd"])
        normalized["_side"] = row["side"].upper()
        normalized["_outcome"] = row["outcome"].upper()
        normalized["_band"] = price_band(row["price"])
        normalized["_date"] = parse_date(row["weather_date_local"])
        normalized["_event_slug"] = row.get("event_slug") or row.get("slug", "")
        normalized["_path_key"] = tuple(row[field] for field in PATH_KEY_FIELDS)
        result.append(normalized)
    return sorted(result, key=lambda item: (item["_ts"], item.get("transaction_hash", "")))


def aggregate(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    materialized = list(rows)
    return {
        "fills": len(materialized),
        "shares": sum(row["_shares"] for row in materialized),
        "usd": sum(row["_usd"] for row in materialized),
    }


def weighted_price(rows: Iterable[dict[str, Any]]) -> float | None:
    materialized = list(rows)
    shares = sum(row["_shares"] for row in materialized)
    if not shares:
        return None
    return sum(row["_usd"] for row in materialized) / shares


def pctl(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def summarize_amounts(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    result = aggregate(rows)
    result["fill_share"] = 0.0
    result["shares_share"] = 0.0
    result["usd_share"] = 0.0
    return result


def group_by_date(rows: list[dict[str, Any]]) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["_date"]].append(row)
    return grouped


def group_by_event_slug(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["_event_slug"]].append(row)
    return grouped


def classify_observed_exit(observed_net_inventory_before: float, sell_shares: float) -> tuple[str, float | None]:
    """Classify one high-price SELL against the chronological observed inventory."""
    if observed_net_inventory_before <= 0:
        return "UNKNOWN_INVENTORY", None
    ratio = sell_shares / observed_net_inventory_before
    if ratio < OBSERVED_EXIT_PARTIAL_MAX:
        return "PARTIAL_OBSERVED_EXIT", ratio
    if ratio <= OBSERVED_EXIT_NEAR_FULL_MAX:
        return "NEAR_FULL_OBSERVED_EXIT", ratio
    return "EXCEEDS_OBSERVED_INVENTORY", ratio


def side_outcome_time_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = aggregate(rows)
    result = []
    for bucket in TIME_ROWS:
        if bucket == "D0":
            scoped = [row for row in rows if row["relative_weather_day"] == "D0"]
        elif bucket.startswith("D0_"):
            scoped = [row for row in rows if row["report_time_bucket"] == bucket]
        elif bucket == "POST_EVENT":
            scoped = [row for row in rows if row["relative_weather_day"] == "POST_EVENT"]
        else:
            scoped = [row for row in rows if row["relative_weather_day"] == bucket]
        item = aggregate(scoped)
        item["bucket"] = bucket
        item["fill_share"] = pct(item["fills"], total["fills"])
        item["shares_share"] = pct(item["shares"], total["shares"])
        item["usd_share"] = pct(item["usd"], total["usd"])
        result.append(item)
    extras = sorted(
        {
            row["relative_weather_day"]
            for row in rows
            if row["relative_weather_day"] not in {"D-2", "D-1", "D0", "POST_EVENT"}
        }
        - {"EARLIER_THAN_D2", "UNKNOWN"}
    )
    for bucket in ("EARLIER_THAN_D2", "UNKNOWN"):
        scoped = [row for row in rows if row["relative_weather_day"] == bucket]
        if scoped:
            item = aggregate(scoped)
            item["bucket"] = bucket
            item["fill_share"] = pct(item["fills"], total["fills"])
            item["shares_share"] = pct(item["shares"], total["shares"])
            item["usd_share"] = pct(item["usd"], total["usd"])
            result.append(item)
    if extras:
        raise ValueError(f"unexpected relative day labels: {extras}")
    return result


def price_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = aggregate(rows)
    result = []
    for band in PRICE_BANDS:
        scoped = [row for row in rows if row["_band"] == band]
        item = aggregate(scoped)
        item["band"] = band
        item["fill_share"] = pct(item["fills"], total["fills"])
        item["shares_share"] = pct(item["shares"], total["shares"])
        item["usd_share"] = pct(item["usd"], total["usd"])
        item["weighted_price"] = weighted_price(scoped)
        result.append(item)
    return result


def event_records(root: Path) -> list[dict[str, Any]]:
    audit_path = root / "_public_evidence" / WALLETS[0] / "event_audit.json"
    target_path = root / "_public_evidence" / WALLETS[0] / "target_markets.json"
    audit = read_json(audit_path)
    target = read_json(target_path)
    slug_to_event_id = {}
    slug_to_date = {}
    for market in target:
        slug = market.get("event_slug", "")
        slug_to_event_id[slug] = market.get("event_id", "")
        slug_to_date[slug] = market.get("weather_date_local", "")
    result = []
    for item in audit:
        slug = item["event_slug"]
        result.append(
            {
                "event_id": item["event_id"] or slug_to_event_id.get(slug, ""),
                "event_slug": slug,
                "weather_date": item["weather_date_local"] or slug_to_date.get(slug, ""),
                "condition_count": item.get("condition_count", 0),
                "completeness_status": item.get("completeness_status", ""),
            }
        )
    return sorted(result, key=lambda item: (item["weather_date"], item["event_id"], item["event_slug"]))


def date_denominator(root: Path, start: date, end: date, events: list[dict[str, Any]]) -> dict[str, Any]:
    requested = set(date_range(start, end))
    in_range = [event for event in events if start <= parse_date(event["weather_date"]) <= end]
    out_of_range = [event for event in events if event not in in_range]
    in_range_counts = Counter(parse_date(event["weather_date"]) for event in in_range)
    duplicate_dates = {day: count for day, count in in_range_counts.items() if count > 1}
    duplicate_event_rows = [event for event in in_range if in_range_counts[parse_date(event["weather_date"])] > 1]
    event_id_counts = Counter(event.get("event_id", "") for event in in_range if event.get("event_id", ""))
    duplicate_event_ids = {event_id: count for event_id, count in event_id_counts.items() if count > 1}
    duplicate_slug_rows = []
    for day in sorted(duplicate_dates):
        duplicate_slug_rows.extend(event for event in in_range if parse_date(event["weather_date"]) == day)
    old_new_duplicate = []
    for day in sorted(duplicate_dates):
        day_events = [event for event in in_range if parse_date(event["weather_date"]) == day]
        slugs = [event["event_slug"] for event in day_events]
        if any(slug.startswith("arch-") for slug in slugs) and any(not slug.startswith("arch-") for slug in slugs):
            old_new_duplicate.append(day.isoformat())
    return {
        "requested_days": len(requested),
        "unique_weather_dates": len({parse_date(event["weather_date"]) for event in in_range}),
        "event_count": len(in_range),
        "duplicate_event_date_count": len(duplicate_dates),
        "duplicate_event_count": len(duplicate_event_rows),
        "duplicate_event_id_count": len(duplicate_event_ids),
        "duplicate_event_ids": duplicate_event_ids,
        "out_of_range_event_count": len(out_of_range),
        "event_counts_by_date": {day.isoformat(): in_range_counts.get(day, 0) for day in sorted(requested)},
        "duplicate_events": duplicate_slug_rows,
        "old_new_duplicate_dates": old_new_duplicate,
        "out_of_range_events": out_of_range,
    }


def asset_paths(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["_path_key"]].append(row)
    result = []
    for key, group in sorted(grouped.items(), key=lambda item: item[0]):
        ordered = sorted(group, key=lambda row: (row["_ts"], row.get("transaction_hash", "")))
        cumulative_buy_shares = 0.0
        cumulative_sell_shares = 0.0
        high_sell_ledger_rows = []
        for row in ordered:
            row["cumulative_buy_shares_before"] = cumulative_buy_shares
            row["cumulative_sell_shares_before"] = cumulative_sell_shares
            row["observed_net_inventory_before"] = cumulative_buy_shares - cumulative_sell_shares
            row["sell_to_observed_inventory_ratio"] = None
            row["observed_exit_classification"] = None
            if row["_side"] == "SELL" and row["_outcome"] == "YES" and row["_band"] == HIGH_SELL_BAND:
                classification, ratio = classify_observed_exit(
                    row["observed_net_inventory_before"], row["_shares"]
                )
                row["sell_to_observed_inventory_ratio"] = ratio
                row["observed_exit_classification"] = classification
                high_sell_ledger_rows.append(row)
            if row["_side"] == "BUY":
                cumulative_buy_shares += row["_shares"]
            elif row["_side"] == "SELL":
                cumulative_sell_shares += row["_shares"]
        buys = [row for row in ordered if row["_side"] == "BUY"]
        sells = [row for row in ordered if row["_side"] == "SELL"]
        first_buy = buys[0] if buys else None
        last_buy = buys[-1] if buys else None
        first_sell = sells[0] if sells else None
        last_sell = sells[-1] if sells else None
        sell_after_buy = [sell for sell in sells if any(buy["_ts"] < sell["_ts"] for buy in buys)]
        first_sell_after = min(sell_after_buy, key=lambda row: row["_ts"]) if sell_after_buy else None
        actions = [row["_side"] for row in ordered]
        transitions = sum(left != right for left, right in zip(actions, actions[1:]))
        buy_to_sell_transitions = sum(left == "BUY" and right == "SELL" for left, right in zip(actions, actions[1:]))
        sell_to_buy_transitions = sum(left == "SELL" and right == "BUY" for left, right in zip(actions, actions[1:]))
        buy_after_sell = bool(
            sells and any(buy["_ts"] > sell["_ts"] for sell in sells for buy in buys)
        )
        local_hours = defaultdict(set)
        for row in ordered:
            hour_key = row["_local_dt"].strftime("%Y-%m-%d %H")
            local_hours[hour_key].add(row["_side"])
        same_hour_two_way = sum("BUY" in sides and "SELL" in sides for sides in local_hours.values())
        result.append(
            {
                "key": key,
                "weather_date": key[0],
                "condition_id": key[1],
                "asset": key[2],
                "outcome": key[3],
                "temperature_bucket": key[4],
                "event_slug": ordered[0]["_event_slug"],
                "rows": ordered,
                "buys": buys,
                "sells": sells,
                "high_sell_ledger_rows": high_sell_ledger_rows,
                "buy_count": len(buys),
                "sell_count": len(sells),
                "buy_shares": sum(row["_shares"] for row in buys),
                "sell_shares": sum(row["_shares"] for row in sells),
                "buy_usd": sum(row["_usd"] for row in buys),
                "sell_usd": sum(row["_usd"] for row in sells),
                "sold_share_ratio": (
                    sum(row["_shares"] for row in sells) / sum(row["_shares"] for row in buys)
                    if buys and sum(row["_shares"] for row in buys)
                    else None
                ),
                "buy_avg": weighted_price(buys),
                "sell_avg": weighted_price(sells),
                "price_difference": (
                    weighted_price(sells) - weighted_price(buys)
                    if buys and sells
                    else None
                ),
                "first_buy": first_buy,
                "last_buy": last_buy,
                "first_sell": first_sell,
                "last_sell": last_sell,
                "first_sell_after": first_sell_after,
                "first_buy_to_first_sell_seconds": (
                    first_sell_after["_ts"] - first_buy["_ts"]
                    if first_buy and first_sell_after
                    else None
                ),
                "last_buy_to_first_sell_seconds": (
                    first_sell_after["_ts"] - last_buy["_ts"]
                    if last_buy and first_sell_after
                    else None
                ),
                "sell_after_buy": bool(sell_after_buy),
                "buy_after_sell": buy_after_sell,
                "transitions": transitions,
                "buy_to_sell_transitions": buy_to_sell_transitions,
                "sell_to_buy_transitions": sell_to_buy_transitions,
                "same_hour_two_way": same_hour_two_way,
            }
        )
    return result


def event_path_rows(events: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_slug = group_by_event_slug(rows)
    result = []
    for event in events:
        scoped = by_slug.get(event["event_slug"], [])
        buys = sorted((row for row in scoped if row["_side"] == "BUY"), key=lambda row: row["_ts"])
        sells = sorted((row for row in scoped if row["_side"] == "SELL"), key=lambda row: row["_ts"])
        total_buy_usd = sum(row["_usd"] for row in buys)
        cumulative = 0.0
        funding_times: dict[str, datetime | None] = {"25%": None, "50%": None, "75%": None}
        for row in buys:
            cumulative += row["_usd"]
            if total_buy_usd:
                for label, threshold in (("25%", 0.25), ("50%", 0.50), ("75%", 0.75)):
                    if funding_times[label] is None and cumulative >= total_buy_usd * threshold:
                        funding_times[label] = row["_local_dt"]
        result.append(
            {
                "event_id": event["event_id"],
                "event_slug": event["event_slug"],
                "weather_date": event["weather_date"],
                "first_buy_yes": min((row["_local_dt"] for row in buys if row["_outcome"] == "YES"), default=None),
                "first_buy_no": min((row["_local_dt"] for row in buys if row["_outcome"] == "NO"), default=None),
                "buy_25": funding_times["25%"],
                "buy_50": funding_times["50%"],
                "buy_75": funding_times["75%"],
                "last_buy": max((row["_local_dt"] for row in buys), default=None),
                "first_sell": min((row["_local_dt"] for row in sells), default=None),
                "last_sell": max((row["_local_dt"] for row in sells), default=None),
                "buy_fills": len(buys),
                "buy_usd": total_buy_usd,
                "sell_fills": len(sells),
                "sell_usd": sum(row["_usd"] for row in sells),
            }
        )
    return result


def numeric_bucket_position(row: dict[str, Any]) -> tuple[float | None, float | None]:
    kind = row.get("bucket_kind")
    low = row.get("bucket_low")
    high = row.get("bucket_high")
    low_value = as_float(low) if low not in (None, "", "null") else None
    high_value = as_float(high) if high not in (None, "", "null") else None
    if kind == "exact":
        return low_value, high_value
    if kind == "below":
        return None, high_value
    if kind == "above":
        return low_value, None
    return low_value, high_value


def exact_numeric_bucket(row: dict[str, Any]) -> float | None:
    low, high = numeric_bucket_position(row)
    return low if low is not None and high is not None and low == high else None


def temperature_day_summary(rows: list[dict[str, Any]], requested_dates: list[date]) -> list[dict[str, Any]]:
    by_date = group_by_date(rows)
    result = []
    for day in requested_dates:
        scoped = [row for row in by_date.get(day, []) if row["_side"] == "BUY"]
        yes_rows = [row for row in scoped if row["_outcome"] == "YES"]
        no_rows = [row for row in scoped if row["_outcome"] == "NO"]
        yes_keys = sorted({(row["temperature_bucket"], row["bucket_kind"]) for row in yes_rows})
        no_keys = sorted({(row["temperature_bucket"], row["bucket_kind"]) for row in no_rows})
        yes_count = len(yes_keys)
        no_count = len(no_keys)
        if yes_count == 0 and no_count == 0:
            category = "NO_BUY"
        elif yes_count == 1 and no_count == 0:
            category = "SINGLE_YES_ONLY"
        elif yes_count > 1 and no_count == 0:
            category = "MULTI_YES_ONLY"
        elif yes_count == 0 and no_count == 1:
            category = "SINGLE_NO_ONLY"
        elif yes_count == 0 and no_count > 1:
            category = "MULTI_NO_ONLY"
        elif yes_count == 1:
            category = "SINGLE_YES_PLUS_NO"
        else:
            category = "MULTI_YES_PLUS_NO"
        exact_yes = sorted(
            {
                value
                for row in yes_rows
                if (value := exact_numeric_bucket(row)) is not None
            }
        )
        adjacent = any(abs(left - right) == 1 for index, left in enumerate(exact_yes) for right in exact_yes[index + 1:])
        non_adjacent = False
        yes_representatives = {}
        no_representatives = {}
        for row in yes_rows:
            yes_representatives.setdefault((row["temperature_bucket"], row["bucket_kind"]), row)
        for row in no_rows:
            no_representatives.setdefault((row["temperature_bucket"], row["bucket_kind"]), row)
        yes_values = [exact_numeric_bucket(row) for row in yes_representatives.values()]
        yes_values = [value for value in yes_values if value is not None]
        for index, left in enumerate(yes_values):
            for right in yes_values[index + 1:]:
                if abs(left - right) != 1:
                    non_adjacent = True
        yes_names = {key[0] for key in yes_keys}
        no_names = {key[0] for key in no_keys}
        same_bucket = bool(yes_names & no_names)
        cross_bucket = any(yes_name != no_name for yes_name in yes_names for no_name in no_names)
        yes_usd_by_key = defaultdict(float)
        yes_shares_by_key = defaultdict(float)
        for row in yes_rows:
            key = (row["temperature_bucket"], row["bucket_kind"])
            yes_usd_by_key[key] += row["_usd"]
            yes_shares_by_key[key] += row["_shares"]
        main_key = max(yes_usd_by_key, key=yes_usd_by_key.get) if yes_usd_by_key else None
        main_usd_share = (
            yes_usd_by_key[main_key] / sum(yes_usd_by_key.values()) * 100
            if main_key is not None and sum(yes_usd_by_key.values())
            else None
        )
        bucket_average_prices = {}
        for key in yes_usd_by_key:
            bucket_average_prices[key] = weighted_price(
                [row for row in yes_rows if (row["temperature_bucket"], row["bucket_kind"]) == key]
            )
        main_price = bucket_average_prices.get(main_key) if main_key else None
        most_expensive = bool(main_key and main_price is not None and math.isclose(main_price, max(bucket_average_prices.values()), rel_tol=1e-9, abs_tol=1e-9))
        cheapest = bool(main_key and main_price is not None and math.isclose(main_price, min(bucket_average_prices.values()), rel_tol=1e-9, abs_tol=1e-9))
        cheap_rows = [row for row in yes_rows if row["_band"] == "PRICE_0_10C"]
        adjacent_price_gaps = []
        for key, price in bucket_average_prices.items():
            row = yes_representatives[key]
            value = exact_numeric_bucket(row)
            if value is None:
                continue
            for other_key, other_price in bucket_average_prices.items():
                if other_key == key or other_price is None:
                    continue
                other_value = exact_numeric_bucket(yes_representatives[other_key])
                if other_value is None or abs(value - other_value) != 1:
                    continue
                if key == main_key or other_key == main_key:
                    adjacent_price_gaps.append(abs(main_price - other_price) * 100 if key == main_key else abs(main_price - price) * 100)
        result.append(
            {
                "weather_date": day.isoformat(),
                "rows": scoped,
                "yes_rows": yes_rows,
                "no_rows": no_rows,
                "yes_keys": yes_keys,
                "no_keys": no_keys,
                "yes_bucket_count": yes_count,
                "no_bucket_count": no_count,
                "category": category,
                "adjacent_yes": adjacent,
                "non_adjacent_yes": non_adjacent,
                "same_bucket_both_sides": same_bucket,
                "cross_bucket_yes_no": cross_bucket,
                "yes_usd_by_key": dict(yes_usd_by_key),
                "yes_shares_by_key": dict(yes_shares_by_key),
                "bucket_average_prices": bucket_average_prices,
                "main_key": main_key,
                "main_usd_share": main_usd_share,
                "main_is_most_expensive": most_expensive,
                "main_is_cheapest": cheapest,
                "cheap_tail_usd": sum(row["_usd"] for row in cheap_rows),
                "cheap_tail_shares": sum(row["_shares"] for row in cheap_rows),
                "adjacent_price_gaps_cents": adjacent_price_gaps,
                "all_yes_usd": sum(row["_usd"] for row in yes_rows),
                "all_yes_shares": sum(row["_shares"] for row in yes_rows),
                "all_no_usd": sum(row["_usd"] for row in no_rows),
                "all_buy_usd": sum(row["_usd"] for row in scoped),
            }
        )
    return result


def no_position(day: dict[str, Any]) -> str:
    yes_rows = day["yes_rows"]
    no_rows = day["no_rows"]
    if not yes_rows or not no_rows:
        return "无法判断"
    yes_names = {row["temperature_bucket"] for row in yes_rows}
    yes_intervals = [numeric_bucket_position(row) for row in yes_rows]
    finite_yes_lows = [low for low, _ in yes_intervals if low is not None]
    finite_yes_highs = [high for _, high in yes_intervals if high is not None]
    min_yes = min(finite_yes_lows) if finite_yes_lows else None
    max_yes = max(finite_yes_highs) if finite_yes_highs else None
    positions = []
    for row in no_rows:
        if row["temperature_bucket"] in yes_names:
            positions.append("same")
            continue
        low, high = numeric_bucket_position(row)
        if max_yes is not None and low is not None and low > max_yes:
            positions.append("above")
        elif min_yes is not None and high is not None and high < min_yes:
            positions.append("below")
        elif low is not None and high is not None and min_yes is not None and max_yes is not None and low >= min_yes and high <= max_yes:
            positions.append("inside")
        else:
            positions.append("unknown")
    if "same" in positions:
        return "与YES相同温度"
    if "below" in positions and "above" in positions:
        return "多个NO分布在两侧"
    if positions and all(position == "below" for position in positions):
        return "NO位于全部YES下方"
    if positions and all(position == "above" for position in positions):
        return "NO位于全部YES上方"
    if "inside" in positions:
        return "NO位于YES范围内部"
    return "无法判断"


def asset_exit_classification(high_sell_rows: list[dict[str, Any]]) -> str:
    classes = {row["observed_exit_classification"] for row in high_sell_rows}
    if not classes:
        return "UNKNOWN"
    if len(classes) > 1:
        return "MIXED_EXIT_PATTERN"
    only = next(iter(classes))
    return {
        "PARTIAL_OBSERVED_EXIT": "ALL_PARTIAL",
        "NEAR_FULL_OBSERVED_EXIT": "HAS_NEAR_FULL",
        "EXCEEDS_OBSERVED_INVENTORY": "HAS_EXCESS",
        "UNKNOWN_INVENTORY": "UNKNOWN",
    }.get(only, "UNKNOWN")


def exit_label(classification: str) -> str:
    return {
        "ALL_PARTIAL": "部分退出",
        "HAS_NEAR_FULL": "接近全部观察库存退出",
        "HAS_EXCESS": "超过观察库存",
        "UNKNOWN": "观察库存未知",
        "MIXED_EXIT_PATTERN": "混合退出模式",
    }.get(classification, classification)


def wallet_two_high_sell_analysis(paths: list[dict[str, Any]]) -> dict[str, Any]:
    high_paths = []
    for path in paths:
        if path["outcome"] != "YES" or not path["high_sell_ledger_rows"]:
            continue
        first_high = min(path["high_sell_ledger_rows"], key=lambda row: row["_ts"])
        prior_buys = [row for row in path["buys"] if row["_ts"] < first_high["_ts"]]
        low_prior_buys = [row for row in prior_buys if row["_band"] in LOW_BUY_BANDS]
        if not prior_buys:
            continue
        item = {
            "path": path,
            "high_sells": path["high_sell_ledger_rows"],
            "first_high": first_high,
            "prior_buys": prior_buys,
            "low_prior_buys": low_prior_buys,
            "first_any_buy_to_first_high_sell_seconds": first_high["_ts"] - prior_buys[0]["_ts"],
            "first_low_buy_to_first_high_sell_seconds": (
                first_high["_ts"] - low_prior_buys[0]["_ts"] if low_prior_buys else None
            ),
            "high_sell_shares": sum(row["_shares"] for row in path["high_sell_ledger_rows"]),
            "prior_buy_shares": sum(row["_shares"] for row in prior_buys),
            "high_sell_usd": sum(row["_usd"] for row in path["high_sell_ledger_rows"]),
            "prior_buy_usd": sum(row["_usd"] for row in prior_buys),
            "observed_exit_classes": sorted({row["observed_exit_classification"] for row in path["high_sell_ledger_rows"]}),
            "asset_exit_classification": asset_exit_classification(path["high_sell_ledger_rows"]),
        }
        item["legacy_high_sell_ratio"] = item["high_sell_shares"] / item["prior_buy_shares"] if item["prior_buy_shares"] else None
        high_paths.append(item)
    low_paths = [item for item in high_paths if item["low_prior_buys"]]

    def aggregate_path_set(items: list[dict[str, Any]]) -> dict[str, Any]:
        prior_buys = [row for item in items for row in item["prior_buys"]]
        low_buys = [row for item in items for row in item["low_prior_buys"]]
        high_sells = [row for item in items for row in item["high_sells"]]
        class_counts = Counter(row["observed_exit_classification"] for row in high_sells)
        asset_counts = Counter(item["asset_exit_classification"] for item in items)
        any_holds = [item["first_any_buy_to_first_high_sell_seconds"] for item in items]
        low_holds = [item["first_low_buy_to_first_high_sell_seconds"] for item in items if item["first_low_buy_to_first_high_sell_seconds"] is not None]
        return {
            "assets": len(items),
            "high_sell_asset_count": len(items),
            "fills": len(high_sells),
            "high_sell_fill_count": len(high_sells),
            "dates": len({item["path"]["weather_date"] for item in items}),
            "prior_buy_usd": sum(row["_usd"] for row in prior_buys),
            "prior_buy_shares": sum(row["_shares"] for row in prior_buys),
            "low_buy_usd": sum(row["_usd"] for row in low_buys),
            "low_buy_shares": sum(row["_shares"] for row in low_buys),
            "high_sell_usd": sum(row["_usd"] for row in high_sells),
            "high_sell_shares": sum(row["_shares"] for row in high_sells),
            "all_prior_buy_weighted_price": weighted_price(prior_buys),
            "low_0_30_buy_weighted_price": weighted_price(low_buys),
            "high_90_100_sell_weighted_price": weighted_price(high_sells),
            "high_sell_to_prior_buy_ratio": sum(row["_shares"] for row in high_sells) / sum(row["_shares"] for row in prior_buys) if sum(row["_shares"] for row in prior_buys) else None,
            "median_any_hold_seconds": statistics.median(any_holds) if any_holds else None,
            "median_low_hold_seconds": statistics.median(low_holds) if low_holds else None,
            "partial_fill_count": class_counts["PARTIAL_OBSERVED_EXIT"],
            "near_full_fill_count": class_counts["NEAR_FULL_OBSERVED_EXIT"],
            "exceeds_fill_count": class_counts["EXCEEDS_OBSERVED_INVENTORY"],
            "unknown_fill_count": class_counts["UNKNOWN_INVENTORY"],
            "all_partial_asset_count": asset_counts["ALL_PARTIAL"],
            "near_full_asset_count": asset_counts["HAS_NEAR_FULL"],
            "mixed_asset_count": asset_counts["MIXED_EXIT_PATTERN"],
            "exceeds_asset_count": asset_counts["HAS_EXCESS"],
            "unknown_asset_count": asset_counts["UNKNOWN"],
            "exit_class_counts": dict(class_counts),
            "asset_exit_class_counts": dict(asset_counts),
        }

    # These are the old static path numbers retained only as a before/after audit.
    legacy_partial = sum(item["legacy_high_sell_ratio"] is not None and item["legacy_high_sell_ratio"] < OBSERVED_EXIT_PARTIAL_MAX for item in low_paths)
    legacy_near_full = sum(item["legacy_high_sell_ratio"] is not None and OBSERVED_EXIT_PARTIAL_MAX <= item["legacy_high_sell_ratio"] <= OBSERVED_EXIT_NEAR_FULL_MAX for item in low_paths)
    legacy_any_holds = [item["first_any_buy_to_first_high_sell_seconds"] for item in low_paths]
    date_cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in low_paths:
        date_cases[item["path"]["weather_date"]].append(item)
    daily_cases = []
    for day, items in date_cases.items():
        summary = aggregate_path_set(items)
        summary["weather_date"] = day
        summary["temperature_buckets"] = ", ".join(sorted({item["path"]["temperature_bucket"] for item in items}))
        summary["exit_label"] = exit_label(Counter(item["asset_exit_classification"] for item in items).most_common(1)[0][0])
        daily_cases.append(summary)
    daily_cases.sort(key=lambda item: (item["high_sell_usd"], item["weather_date"]), reverse=True)
    high_traceable = aggregate_path_set(high_paths)
    low_traceable = aggregate_path_set(low_paths)
    low_traceable["legacy_partial_asset_count"] = legacy_partial
    low_traceable["legacy_near_full_asset_count"] = legacy_near_full
    low_traceable["legacy_dates"] = len({item["path"]["weather_date"] for item in low_paths})
    low_traceable["legacy_median_low_hold_seconds"] = statistics.median(legacy_any_holds) if legacy_any_holds else None
    return {
        "high_sell_yes_total_fills": sum(len(path["high_sell_ledger_rows"]) for path in paths),
        "high_sell_yes_traceable": high_traceable,
        "low_buy_high_sell": low_traceable,
        "high_paths": high_paths,
        "low_paths": low_paths,
        "daily_cases": daily_cases,
        "lowest_hold_case": min(low_paths, key=lambda item: item["first_low_buy_to_first_high_sell_seconds"]) if low_paths else None,
        "highest_hold_case": max(low_paths, key=lambda item: item["first_low_buy_to_first_high_sell_seconds"]) if low_paths else None,
        "legacy_low_buy_high_sell_dates": low_traceable["legacy_dates"],
        "new_low_buy_high_sell_dates": low_traceable["dates"],
    }


def classify_trader_style(metrics: dict[str, Any]) -> str:
    """Return a parameterized style label; wallet identity is never consulted."""
    if (
        metrics["buy_fill_share"] >= STYLE_BUY_DOMINANT_MIN_BUY_FILL_SHARE
        and metrics["sell_to_buy_fill_ratio"] <= STYLE_BUY_DOMINANT_MAX_SELL_TO_BUY_FILL_RATIO
    ):
        return "BUY_DOMINANT_ACCUMULATOR"
    if (
        metrics["repeated_asset_share"] >= STYLE_ACTIVE_MIN_REPEATED_ASSET_SHARE
        and metrics["sell_then_rebuy_ratio_decimal"] >= STYLE_ACTIVE_MIN_SELL_REBUY_RATIO
        and metrics["same_hour_two_way"] >= STYLE_ACTIVE_MIN_SAME_HOUR_TWO_WAY
    ):
        return "ACTIVE_REBALANCER"
    if (
        metrics["same_hour_two_way"] >= STYLE_MARKET_MIN_SAME_HOUR_TWO_WAY
        and metrics["short_hold_ratio_decimal"] >= STYLE_MARKET_MIN_SHORT_HOLD_RATIO
        and metrics["sell_then_rebuy_ratio_decimal"] >= STYLE_MARKET_MIN_SELL_REBUY_RATIO
    ):
        return "POSSIBLE_MARKET_MAKER"
    return "MIXED_OR_UNCLEAR"


def wallet_style(paths: list[dict[str, Any]], rows: list[dict[str, Any]], requested_dates: list[date]) -> dict[str, Any]:
    both = [path for path in paths if path["buy_count"] and path["sell_count"]]
    matched = [path for path in both if path["sell_after_buy"]]
    repeat = [path for path in paths if path["buy_count"] >= 2 and path["sell_count"] >= 2]
    active_dates = sorted({row["_date"] for row in rows})
    daily = group_by_date(rows)
    top_days = []
    for day in requested_dates:
        scoped = daily.get(day, [])
        if not scoped:
            continue
        top_days.append({
            "weather_date": day.isoformat(),
            "fills": len(scoped),
            "buy_fills": sum(row["_side"] == "BUY" for row in scoped),
            "sell_fills": sum(row["_side"] == "SELL" for row in scoped),
            "usd": sum(row["_usd"] for row in scoped),
        })
    top_days.sort(key=lambda item: (item["fills"], item["usd"], item["weather_date"]), reverse=True)
    yes_no_days = []
    for day in requested_dates:
        scoped = daily.get(day, [])
        if {row["_outcome"] for row in scoped} >= {"YES", "NO"}:
            yes_no_days.append(day.isoformat())
    hold_values = [path["first_buy_to_first_sell_seconds"] for path in matched if path["first_buy_to_first_sell_seconds"] is not None and path["first_buy_to_first_sell_seconds"] >= 0]
    short_assets = [path for path in matched if path["first_buy_to_first_sell_seconds"] is not None and 0 <= path["first_buy_to_first_sell_seconds"] <= SHORT_HOLD_HOURS * 3600]
    sell_then_buy = [path for path in both if path["buy_after_sell"]]
    two_way_hour_count = sum(path["same_hour_two_way"] for path in paths)
    maker_fields = {"maker", "taker", "maker_taker", "liquidity_side", "role"}
    available_fields = set(rows[0]) if rows else set()
    maker_taker = sorted(available_fields & maker_fields)
    metrics = {
        "buy_fill_count": sum(row["_side"] == "BUY" for row in rows),
        "sell_fill_count": sum(row["_side"] == "SELL" for row in rows),
        "asset_count": len(paths),
        "both_buy_sell_assets": len(both),
        "matched_buy_then_sell_assets": len(matched),
        "repeated_buy_sell_assets": len(repeat),
        "buy_sell_transitions": sum(path["buy_to_sell_transitions"] for path in paths),
        "sell_buy_transitions": sum(path["sell_to_buy_transitions"] for path in paths),
        "same_hour_two_way_asset_hours": two_way_hour_count,
        "short_hold_assets": len(short_assets),
        "short_hold_ratio": pct(len(short_assets), len(matched)),
        "sell_then_rebuy_assets": len(sell_then_buy),
        "sell_then_rebuy_ratio": pct(len(sell_then_buy), len(both)),
        "sell_then_rebuy_ratio_decimal": len(sell_then_buy) / len(both) if both else 0.0,
        "median_hold_seconds": statistics.median(hold_values) if hold_values else None,
        "average_fills_per_requested_day": len(rows) / len(requested_dates) if requested_dates else None,
        "average_fills_per_active_day": len(rows) / len(active_dates) if active_dates else None,
        "active_days": len(active_dates),
        "yes_no_active_days": yes_no_days,
        "top_days": top_days[:10],
        "maker_taker": maker_taker or None,
    }
    metrics["buy_fill_share"] = pct(metrics["buy_fill_count"], len(rows)) / 100 if rows else 0.0
    metrics["sell_to_buy_fill_ratio"] = metrics["sell_fill_count"] / metrics["buy_fill_count"] if metrics["buy_fill_count"] else float("inf")
    metrics["repeated_asset_share"] = metrics["repeated_buy_sell_assets"] / metrics["asset_count"] if metrics["asset_count"] else 0.0
    metrics["same_hour_two_way"] = metrics["same_hour_two_way_asset_hours"]
    metrics["short_hold_ratio_decimal"] = metrics["short_hold_ratio"] / 100 if metrics["short_hold_ratio"] is not None else 0.0
    metrics["style_label"] = classify_trader_style(metrics)
    return metrics


def percentile_summary(values: list[float]) -> dict[str, float | None]:
    return {label: pctl(values, quantile) for label, quantile in (("P25", 0.25), ("P50", 0.50), ("P75", 0.75), ("P90", 0.90))}


def multi_yes_analysis(days: list[dict[str, Any]]) -> dict[str, Any]:
    yes_active = [day for day in days if day["yes_bucket_count"] > 0]
    multi = [day for day in days if day["yes_bucket_count"] > 1]
    yes_counts = [day["yes_bucket_count"] for day in yes_active]
    main_shares = [day["main_usd_share"] for day in yes_active if day["main_usd_share"] is not None]
    all_yes_usd = sum(day["all_yes_usd"] for day in yes_active)
    cheap_usd = sum(day["cheap_tail_usd"] for day in yes_active)
    all_yes_shares = sum(day["all_yes_shares"] for day in yes_active)
    cheap_shares = sum(day["cheap_tail_shares"] for day in yes_active)
    gaps = [gap for day in multi for gap in day["adjacent_price_gaps_cents"]]
    counts_distribution = Counter(day["yes_bucket_count"] for day in multi)
    return {
        "yes_active_days": len(yes_active),
        "yes_bucket_count_percentiles": percentile_summary([float(value) for value in yes_counts]),
        "main_usd_share_percentiles": percentile_summary([float(value) for value in main_shares]),
        "main_most_expensive_count": sum(day["main_is_most_expensive"] for day in yes_active),
        "main_cheapest_count": sum(day["main_is_cheapest"] for day in yes_active),
        "multi_yes_days": len(multi),
        "multi_yes_distribution": dict(sorted(counts_distribution.items())),
        "adjacent_rate": pct(sum(day["adjacent_yes"] for day in multi), len(multi)),
        "non_adjacent_pair_rate": pct(sum(day["non_adjacent_yes"] for day in multi), len(multi)),
        "cheap_tail_usd_share": pct(cheap_usd, all_yes_usd),
        "cheap_tail_shares_share": pct(cheap_shares, all_yes_shares),
        "adjacent_price_gap_percentiles_cents": percentile_summary(gaps),
        "adjacent_price_gap_count": len(gaps),
    }


def no_analysis(days: list[dict[str, Any]]) -> dict[str, Any]:
    mixed = [day for day in days if day["category"] in {"SINGLE_YES_PLUS_NO", "MULTI_YES_PLUS_NO"}]
    position_counts = Counter(no_position(day) for day in mixed)
    first_counts = Counter()
    late_counts = Counter()
    mixed_no_rows = []
    daily_no_shares = []
    for day in mixed:
        yes_times = [row["_ts"] for row in day["yes_rows"]]
        no_times = [row["_ts"] for row in day["no_rows"]]
        if min(yes_times) < min(no_times):
            first_counts["YES先买"] += 1
        elif min(no_times) < min(yes_times):
            first_counts["NO先买"] += 1
        else:
            first_counts["同秒"] += 1
        buy_rows = sorted(day["rows"], key=lambda row: row["_ts"])
        total_buy_usd = sum(row["_usd"] for row in buy_rows)
        cumulative = 0.0
        buy50_ts = None
        for row in buy_rows:
            cumulative += row["_usd"]
            if total_buy_usd and buy50_ts is None and cumulative >= total_buy_usd * 0.50:
                buy50_ts = row["_ts"]
        first_no = min(no_times)
        if min(yes_times) < first_no:
            late_counts["NO晚于YES"] += 1
        if buy50_ts is not None and first_no > buy50_ts:
            late_counts["NO晚于BUY资金50%"] += 1
        mixed_no_rows.extend(day["no_rows"])
        daily_no_shares.append(pct(sum(row["_usd"] for row in day["no_rows"]), total_buy_usd))
    price_rows = price_table(mixed_no_rows)
    mixed_no_usd = sum(row["_usd"] for row in mixed_no_rows)
    mixed_buy_usd = sum(sum(row["_usd"] for row in day["rows"]) for day in mixed)
    return {
        "mixed_days": len(mixed),
        "position_counts": dict(position_counts),
        "first_buy_counts": dict(first_counts),
        "late_counts": dict(late_counts),
        "price_rows": price_rows,
        "overall_no_usd_share": pct(mixed_no_usd, mixed_buy_usd),
        "daily_no_usd_share_percentiles": percentile_summary(daily_no_shares),
    }


def quality_summary(root: Path, wallet: str) -> dict[str, str]:
    rows = read_csv(root / wallet / "data_quality.csv")
    return {row["metric"]: row["value"] for row in rows}


def aggregate_path_metrics(paths: list[dict[str, Any]]) -> dict[str, Any]:
    both = [path for path in paths if path["buy_count"] and path["sell_count"]]
    matched = [path for path in both if path["sell_after_buy"]]
    first_hold = [path["first_buy_to_first_sell_seconds"] for path in matched if path["first_buy_to_first_sell_seconds"] is not None]
    last_to_first = [path["last_buy_to_first_sell_seconds"] for path in matched if path["last_buy_to_first_sell_seconds"] is not None]
    return {
        "asset_count": len(paths),
        "buy_and_sell_assets": len(both),
        "buy_only_assets": sum(path["buy_count"] and not path["sell_count"] for path in paths),
        "sell_only_assets": sum(path["sell_count"] and not path["buy_count"] for path in paths),
        "buy_then_sell_assets": len(matched),
        "buy_then_sell_weather_days": len({path["weather_date"] for path in matched}),
        "first_buy_to_first_sell_percentiles": percentile_summary([seconds / 3600 for seconds in first_hold]),
        "last_buy_to_first_sell_percentiles": percentile_summary([seconds / 3600 for seconds in last_to_first]),
        "total_buy_shares": sum(path["buy_shares"] for path in paths),
        "total_sell_shares": sum(path["sell_shares"] for path in paths),
        "total_buy_usd": sum(path["buy_usd"] for path in paths),
        "total_sell_usd": sum(path["sell_usd"] for path in paths),
        "weighted_buy_price": weighted_price([row for path in paths for row in path["buys"]]),
        "weighted_sell_price": weighted_price([row for path in paths for row in path["sells"]]),
        "sold_share_ratio": sum(path["sell_shares"] for path in paths) / sum(path["buy_shares"] for path in paths) if sum(path["buy_shares"] for path in paths) else None,
        "weighted_price_difference": (
            weighted_price([row for path in paths for row in path["sells"]])
            - weighted_price([row for path in paths for row in path["buys"]])
            if paths and weighted_price([row for path in paths for row in path["buys"]]) is not None and weighted_price([row for path in paths for row in path["sells"]]) is not None
            else None
        ),
    }


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: jsonable(row.get(field, "")) for field in fieldnames})


def build_machine_outputs(
    report_path: Path,
    start: date,
    end: date,
    city: str,
    requested_dates: list[date],
    events: list[dict[str, Any]],
    denominator: dict[str, Any],
    data: dict[str, dict[str, Any]],
    high_analyses: dict[str, dict[str, Any]],
) -> None:
    output_dir = report_path.parent
    asset_rows = []
    high_fill_rows = []
    high_asset_rows = []
    daily_rows = []
    style_rows = []
    for wallet in WALLETS:
        item = data[wallet]
        for path in item["paths"]:
            asset_rows.append({
                "wallet": wallet,
                "weather_date": path["weather_date"],
                "condition_id": path["condition_id"],
                "asset": path["asset"],
                "outcome": path["outcome"],
                "temperature_bucket": path["temperature_bucket"],
                "event_slug": path["event_slug"],
                "buy_count": path["buy_count"],
                "sell_count": path["sell_count"],
                "buy_shares": path["buy_shares"],
                "sell_shares": path["sell_shares"],
                "sold_share_ratio": path["sold_share_ratio"],
                "buy_usd_weighted_price": path["buy_avg"],
                "sell_usd_weighted_price": path["sell_avg"],
                "sell_minus_buy_price": path["price_difference"],
                "first_buy_to_first_sell_seconds": path["first_buy_to_first_sell_seconds"],
                "last_buy_to_first_sell_seconds": path["last_buy_to_first_sell_seconds"],
                "buy_after_sell": path["buy_after_sell"],
                "same_hour_two_way": path["same_hour_two_way"],
            })
            if path["high_sell_ledger_rows"]:
                high_asset_rows.append({
                    "wallet": wallet,
                    "weather_date": path["weather_date"],
                    "condition_id": path["condition_id"],
                    "asset": path["asset"],
                    "outcome": path["outcome"],
                    "temperature_bucket": path["temperature_bucket"],
                    "event_slug": path["event_slug"],
                    "high_sell_fill_count": len(path["high_sell_ledger_rows"]),
                    "asset_exit_classification": asset_exit_classification(path["high_sell_ledger_rows"]),
                    "all_buy_shares": path["buy_shares"],
                    "all_sell_shares": path["sell_shares"],
                    "sold_share_ratio": path["sold_share_ratio"],
                    "buy_usd_weighted_price": path["buy_avg"],
                    "sell_usd_weighted_price": path["sell_avg"],
                })
                for fill in path["high_sell_ledger_rows"]:
                    prior_buys = [row for row in path["buys"] if row["_ts"] < fill["_ts"]]
                    low_buys = [row for row in prior_buys if row["_band"] in LOW_BUY_BANDS]
                    first_any = prior_buys[0] if prior_buys else None
                    first_low = low_buys[0] if low_buys else None
                    high_fill_rows.append({
                        "wallet": wallet,
                        "weather_date": path["weather_date"],
                        "condition_id": path["condition_id"],
                        "asset": path["asset"],
                        "outcome": path["outcome"],
                        "temperature_bucket": path["temperature_bucket"],
                        "event_slug": path["event_slug"],
                        "sell_timestamp": fill["_local_dt"],
                        "sell_price": fill["_price"],
                        "sell_shares": fill["_shares"],
                        "cumulative_buy_shares_before": fill["cumulative_buy_shares_before"],
                        "cumulative_sell_shares_before": fill["cumulative_sell_shares_before"],
                        "observed_net_inventory_before": fill["observed_net_inventory_before"],
                        "sell_to_observed_inventory_ratio": fill["sell_to_observed_inventory_ratio"],
                        "observed_exit_classification": fill["observed_exit_classification"],
                        "has_prior_any_buy": bool(prior_buys),
                        "has_prior_low_0_30_buy": bool(low_buys),
                        "first_any_buy_to_this_high_sell_seconds": fill["_ts"] - first_any["_ts"] if first_any else None,
                        "first_low_buy_to_this_high_sell_seconds": fill["_ts"] - first_low["_ts"] if first_low else None,
                    })
        for day in item["temperature_days"]:
            daily_rows.append({
                "wallet": wallet,
                "weather_date": day["weather_date"],
                "category": day["category"],
                "yes_bucket_count": day["yes_bucket_count"],
                "no_bucket_count": day["no_bucket_count"],
                "adjacent_yes": day["adjacent_yes"],
                "non_adjacent_yes": day["non_adjacent_yes"],
                "same_bucket_both_sides": day["same_bucket_both_sides"],
                "cross_bucket_yes_no": day["cross_bucket_yes_no"],
                "main_yes_bucket": day["main_key"][0] if day["main_key"] else "",
                "main_yes_usd_share": day["main_usd_share"],
                "yes_buy_usd": day["all_yes_usd"],
                "no_buy_usd": day["all_no_usd"],
                "all_buy_usd": day["all_buy_usd"],
            })
        style = item["style"]
        style_rows.append({key: value for key, value in style.items() if key != "top_days" and key != "yes_no_active_days"})

    def compact_case(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        path = item["path"]
        return {
            "weather_date": path["weather_date"],
            "condition_id": path["condition_id"],
            "asset": path["asset"],
            "outcome": path["outcome"],
            "temperature_bucket": path["temperature_bucket"],
            "first_any_buy_to_first_high_sell_seconds": item["first_any_buy_to_first_high_sell_seconds"],
            "first_low_buy_to_first_high_sell_seconds": item["first_low_buy_to_first_high_sell_seconds"],
            "low_0_30_buy_weighted_price": weighted_price(item["low_prior_buys"]),
            "high_90_100_sell_weighted_price": weighted_price(item["high_sells"]),
            "asset_exit_classification": item["asset_exit_classification"],
        }

    def compact_high_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
        excluded = {"high_paths", "low_paths", "lowest_hold_case", "highest_hold_case"}
        result = {key: value for key, value in analysis.items() if key not in excluded}
        result["lowest_hold_case"] = compact_case(analysis.get("lowest_hold_case"))
        result["highest_hold_case"] = compact_case(analysis.get("highest_hold_case"))
        return result

    json_payload = {
        "schema_version": "second_stage_trader_pattern_comparison_v2",
        "public_fills_only": True,
        "network_accessed": False,
        "network_call_count": SECOND_STAGE_NETWORK_CALL_COUNT,
        "scope": {
            "weather_date_from": start.isoformat(),
            "weather_date_to": end.isoformat(),
            "city": city,
            "requested_calendar_day_count": len(requested_dates),
        },
        "denominator": denominator,
        "denominator_97_explanation": (
            f"{denominator['unique_weather_dates']} unique weather dates + "
            f"{denominator['event_count'] - denominator['unique_weather_dates']} extra same-date event records"
        ),
        "thresholds": {
            "low_buy_bands": sorted(LOW_BUY_BANDS),
            "high_sell_band": HIGH_SELL_BAND,
            "observed_exit_partial_max": OBSERVED_EXIT_PARTIAL_MAX,
            "observed_exit_near_full_max": OBSERVED_EXIT_NEAR_FULL_MAX,
            "short_hold_hours": SHORT_HOLD_HOURS,
            "style": {
                "buy_dominant_min_buy_fill_share": STYLE_BUY_DOMINANT_MIN_BUY_FILL_SHARE,
                "buy_dominant_max_sell_to_buy_fill_ratio": STYLE_BUY_DOMINANT_MAX_SELL_TO_BUY_FILL_RATIO,
                "active_min_repeated_asset_share": STYLE_ACTIVE_MIN_REPEATED_ASSET_SHARE,
                "active_min_sell_rebuy_ratio": STYLE_ACTIVE_MIN_SELL_REBUY_RATIO,
                "active_min_same_hour_two_way": STYLE_ACTIVE_MIN_SAME_HOUR_TWO_WAY,
            },
        },
        "events": events,
        "wallets": {
            wallet: {
                "quality": data[wallet]["quality"],
                "path_metrics": data[wallet]["path_metrics"],
                "category_counts": dict(data[wallet]["category_counts"]),
                "multi_yes": data[wallet]["multi_yes"],
                "no": data[wallet]["no"],
                "style": data[wallet]["style"],
                "high_sell_summary": compact_high_analysis(high_analyses[wallet]),
            }
            for wallet in WALLETS
        },
    }
    (output_dir / "SECOND_STAGE_TRADER_PATTERN_COMPARISON.json").write_text(
        json.dumps(jsonable(json_payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_csv_rows(output_dir / "asset_path_summary.csv", asset_rows, list(asset_rows[0]) if asset_rows else ["wallet"])
    write_csv_rows(output_dir / "high_sell_path_fills.csv", high_fill_rows, list(high_fill_rows[0]) if high_fill_rows else ["wallet"])
    write_csv_rows(output_dir / "high_sell_path_assets.csv", high_asset_rows, list(high_asset_rows[0]) if high_asset_rows else ["wallet"])
    write_csv_rows(output_dir / "daily_temperature_structure.csv", daily_rows, list(daily_rows[0]) if daily_rows else ["wallet"])
    write_csv_rows(output_dir / "trader_style_metrics.csv", style_rows, list(style_rows[0]) if style_rows else ["wallet"])


def make_report(root: Path, report_path: Path, start: date, end: date, city: str) -> dict[str, Any]:
    requested_dates = date_range(start, end)
    events = event_records(root)
    denominator = date_denominator(root, start, end, events)
    wallet_data = {}
    for wallet in WALLETS:
        rows = normalize_fills(read_csv(root / wallet / "all_fills.csv"))
        quality = quality_summary(root, wallet)
        paths = asset_paths(rows)
        daily_temperature = temperature_day_summary(rows, requested_dates)
        wallet_data[wallet] = {
            "rows": rows,
            "quality": quality,
            "paths": paths,
            "event_paths": event_path_rows(events, rows),
            "time": {},
            "price": {},
            "path_metrics": aggregate_path_metrics(paths),
            "temperature_days": daily_temperature,
            "category_counts": Counter(day["category"] for day in daily_temperature),
            "multi_yes": multi_yes_analysis(daily_temperature),
            "no": no_analysis(daily_temperature),
            "style": wallet_style(paths, rows, requested_dates),
        }
        for side, outcome in (("BUY", "YES"), ("BUY", "NO"), ("SELL", "YES"), ("SELL", "NO")):
            scoped = [row for row in rows if row["_side"] == side and row["_outcome"] == outcome]
            key = f"{side} {outcome}"
            wallet_data[wallet]["time"][key] = side_outcome_time_table(scoped)
            wallet_data[wallet]["price"][key] = price_table(scoped)
    high_analyses = {wallet: wallet_two_high_sell_analysis(wallet_data[wallet]["paths"]) for wallet in WALLETS}
    w2_high = high_analyses[WALLETS[1]]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(root, report_path, start, end, city, requested_dates, events, denominator, wallet_data, w2_high).replace("\n+  --", "\n  --"),
        encoding="utf-8",
    )
    build_machine_outputs(report_path, start, end, city, requested_dates, events, denominator, wallet_data, high_analyses)
    return {"denominator": denominator, "wallet_data": wallet_data, "high_analyses": high_analyses}


def render_time_table(rows: list[dict[str, Any]]) -> str:
    return md_table(
        ["时间桶", "fills", "fill占比", "shares", "shares占比", "trade USD", "USD占比"],
        [
            [TIME_NAMES[row["bucket"]], row["fills"], fmt_pct(row["fill_share"]), fmt_num(row["shares"]), fmt_pct(row["shares_share"]), fmt_num(row["usd"]), fmt_pct(row["usd_share"])]
            for row in rows
        ],
    )


def render_price_table(rows: list[dict[str, Any]]) -> str:
    return md_table(
        ["价格带", "fill_count", "fill_share", "shares", "shares_share", "trade_usd", "usd_share", "USD加权均价"],
        [
            [PRICE_NAMES[row["band"]], row["fills"], fmt_pct(row["fill_share"]), fmt_num(row["shares"]), fmt_pct(row["shares_share"]), fmt_num(row["usd"]), fmt_pct(row["usd_share"]), fmt_price(row["weighted_price"])]
            for row in rows
        ],
    )


def render_event_path_table(rows: list[dict[str, Any]]) -> str:
    return md_table(
        ["event_id", "weather_date", "event_slug", "first BUY YES", "first BUY NO", "BUY资金25%", "BUY资金50%", "BUY资金75%", "last BUY", "first SELL", "last SELL", "BUY USD", "SELL USD"],
        [
            [
                row["event_id"], row["weather_date"], row["event_slug"], fmt_dt(row["first_buy_yes"]), fmt_dt(row["first_buy_no"]),
                fmt_dt(row["buy_25"]), fmt_dt(row["buy_50"]), fmt_dt(row["buy_75"]), fmt_dt(row["last_buy"]), fmt_dt(row["first_sell"]), fmt_dt(row["last_sell"]), fmt_num(row["buy_usd"]), fmt_num(row["sell_usd"]),
            ]
            for row in rows
        ],
    )


def render_category_table(data: dict[str, Any], total_days: int) -> str:
    counts = data["category_counts"]
    return md_table(
        ["互斥主类别", "天气日数", "比例"],
        [[category, counts.get(category, 0), fmt_pct(pct(counts.get(category, 0), total_days))] for category in CATEGORY_ORDER],
    )


def render_path_metrics(metrics: dict[str, Any]) -> str:
    return md_table(
        ["指标", "结果"],
        [
            ["资产组数", metrics["asset_count"]],
            ["有BUY也有SELL的资产数", metrics["buy_and_sell_assets"]],
            ["只有BUY的资产数", metrics["buy_only_assets"]],
            ["只有SELL的资产数", metrics["sell_only_assets"]],
            ["BUY后发生SELL的资产数", metrics["buy_then_sell_assets"]],
            ["BUY后发生SELL的天气日数", metrics["buy_then_sell_weather_days"]],
            ["首次BUY至首次SELL P25/P50/P75/P90", "/".join("—" if v is None else f"{v:.2f}h" for v in metrics["first_buy_to_first_sell_percentiles"].values())],
            ["最后BUY至首次SELL P25/P50/P75/P90", "/".join("—" if v is None else f"{v:.2f}h" for v in metrics["last_buy_to_first_sell_percentiles"].values())],
            ["总买入shares", fmt_num(metrics["total_buy_shares"])],
            ["总卖出shares", fmt_num(metrics["total_sell_shares"])],
            ["sold_share_ratio", "—" if metrics["sold_share_ratio"] is None else f"{metrics['sold_share_ratio'] * 100:.2f}%"],
            ["USD加权平均买价", fmt_price(metrics["weighted_buy_price"])],
            ["USD加权平均卖价", fmt_price(metrics["weighted_sell_price"])],
            ["卖价减买价", fmt_price(metrics["weighted_price_difference"])],
        ],
    )


def render_high_cases(analysis: dict[str, Any]) -> str:
    aggregate_rows = analysis["low_buy_high_sell"]
    high_rows = analysis["high_sell_yes_traceable"]
    partial = aggregate_rows["all_partial_asset_count"]
    near_full = aggregate_rows["near_full_asset_count"]
    if near_full > partial:
        dominance_sentence = "在可解释的公开观察库存路径中，接近全部观察库存退出的资产更多。"
    elif partial > near_full:
        dominance_sentence = "在可解释的公开观察库存路径中，部分退出的资产更多。"
    else:
        dominance_sentence = "在可解释的公开观察库存路径中，部分退出和接近全部退出均存在，没有单一主导模式。"
    lines = [
        md_table(
            ["范围", "90—100¢ SELL YES笔数", "可追溯资产数", "天气日数", "全部此前BUY均价", "0—30¢ BUY均价", "90—100¢ SELL均价", "SELL shares / BUY shares", "任意BUY→高价SELL中位", "低价BUY→高价SELL中位"],
            [
                ["全部90—100¢ SELL YES", analysis["high_sell_yes_total_fills"], high_rows["assets"], high_rows["dates"], fmt_price(high_rows["all_prior_buy_weighted_price"]), fmt_price(high_rows["low_0_30_buy_weighted_price"]), fmt_price(high_rows["high_90_100_sell_weighted_price"]), f"{high_rows['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if high_rows["high_sell_to_prior_buy_ratio"] is not None else "—", fmt_hours(high_rows["median_any_hold_seconds"]), fmt_hours(high_rows["median_low_hold_seconds"])],
                ["其中此前有0—30¢ BUY", aggregate_rows["fills"], aggregate_rows["assets"], aggregate_rows["dates"], fmt_price(aggregate_rows["all_prior_buy_weighted_price"]), fmt_price(aggregate_rows["low_0_30_buy_weighted_price"]), fmt_price(aggregate_rows["high_90_100_sell_weighted_price"]), f"{aggregate_rows['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if aggregate_rows["high_sell_to_prior_buy_ratio"] is not None else "—", fmt_hours(aggregate_rows["median_any_hold_seconds"]), fmt_hours(aggregate_rows["median_low_hold_seconds"])],
            ],
        ),
        "",
        f"{dominance_sentence}",
        f"逐笔高价SELL分类（fills）：PARTIAL_OBSERVED_EXIT={aggregate_rows['partial_fill_count']}，NEAR_FULL_OBSERVED_EXIT={aggregate_rows['near_full_fill_count']}，EXCEEDS_OBSERVED_INVENTORY={aggregate_rows['exceeds_fill_count']}，UNKNOWN_INVENTORY={aggregate_rows['unknown_fill_count']}。资产级分类：ALL_PARTIAL={aggregate_rows['all_partial_asset_count']}，HAS_NEAR_FULL={aggregate_rows['near_full_asset_count']}，MIXED_EXIT_PATTERN={aggregate_rows['mixed_asset_count']}，HAS_EXCESS={aggregate_rows['exceeds_asset_count']}，UNKNOWN={aggregate_rows['unknown_asset_count']}。",
        "观察库存只基于公开 fills，不等于真实完整账户库存；每笔高价SELL都按时间顺序扣减，后续BUY会重新增加观察库存。",
        f"修复前/修复后低买高卖天气日数：{aggregate_rows['legacy_dates']} / {aggregate_rows['dates']}；修复前/修复后资产级部分退出数：{aggregate_rows['legacy_partial_asset_count']} / {aggregate_rows['all_partial_asset_count']}；修复前/修复后接近全部退出数：{aggregate_rows['legacy_near_full_asset_count']} / {aggregate_rows['near_full_asset_count']}。",
        f"修复前中位‘低价BUY’持有时间（实际为任意BUY→高价SELL）：{fmt_hours(aggregate_rows['legacy_median_low_hold_seconds'])}；修复后任意BUY→高价SELL：{fmt_hours(aggregate_rows['median_any_hold_seconds'])}；修复后0—30¢ BUY→高价SELL：{fmt_hours(aggregate_rows['median_low_hold_seconds'])}。",
        "",
        "按90—100¢ SELL YES路径的高价卖出金额排序，至少列出10个真实天气日案例：",
        md_table(
            ["天气日期", "温度档", "匹配资产数", "高价SELL笔数", "0—30¢ BUY USD", "高价SELL USD", "SELL/观察库存", "主退出形态", "任意BUY→高价SELL", "低价BUY→高价SELL"],
            [
                [case["weather_date"], case["temperature_buckets"], case["assets"], case["fills"], fmt_num(case["low_buy_usd"]), fmt_num(case["high_sell_usd"]), f"{case['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if case["high_sell_to_prior_buy_ratio"] is not None else "—", case["exit_label"], fmt_hours(case["median_any_hold_seconds"]), fmt_hours(case["median_low_hold_seconds"])]
                for case in analysis["daily_cases"][:10]
            ],
        ),
    ]
    for label, item in (("最低持有案例", analysis["lowest_hold_case"]), ("最高持有案例", analysis["highest_hold_case"])):
        if item:
            path = item["path"]
            lines.extend([
                "",
                f"{label}：{path['weather_date']} / {path['temperature_bucket']} / asset `{path['asset']}`；任意BUY至首次90—100¢ SELL {fmt_hours(item['first_any_buy_to_first_high_sell_seconds'])}，0—30¢ BUY至首次90—100¢ SELL {fmt_hours(item['first_low_buy_to_first_high_sell_seconds'])}；低价BUY均价 {fmt_price(weighted_price(item['low_prior_buys']))}，高价SELL均价 {fmt_price(weighted_price(item['high_sells']))}，资产级退出分类 {item['asset_exit_classification']}。",
            ])
    return "\n".join(lines)


def render_style(style: dict[str, Any]) -> str:
    return md_table(
        ["指标", "结果"],
        [
            ["资产组数", style["asset_count"]],
            ["同一资产反复BUY/SELL（BUY≥2且SELL≥2）", style["repeated_buy_sell_assets"]],
            ["BUY→SELL相邻切换次数", style["buy_sell_transitions"]],
            ["SELL→BUY重新买入切换次数", style["sell_buy_transitions"]],
            ["同一资产同一北京时间小时双向成交组数", style["same_hour_two_way_asset_hours"]],
            [f"BUY后{SHORT_HOLD_HOURS:.0f}小时内SELL的资产比例", f"{style['short_hold_ratio']:.2f}% ({style['short_hold_assets']}/{style['matched_buy_then_sell_assets']})"],
            ["SELL后重新BUY的资产比例", f"{style['sell_then_rebuy_ratio']:.2f}% ({style['sell_then_rebuy_assets']}/{style['both_buy_sell_assets']})"],
            ["首次BUY至首次SELL中位持有", fmt_hours(style["median_hold_seconds"])],
            ["每个请求日历天气日平均成交笔数", fmt_num(style["average_fills_per_requested_day"])],
            ["每个有成交天气日平均成交笔数", fmt_num(style["average_fills_per_active_day"])],
            ["YES和NO都活跃的天气日数", len(style["yes_no_active_days"])],
            ["maker/taker", "NOT_AVAILABLE" if style["maker_taker"] is None else ", ".join(style["maker_taker"])],
        ],
    )


def render_dynamic_conclusion_sections(
    root: Path,
    report_path: Path,
    city: str,
    requested_dates: list[date],
    data: dict[str, dict[str, Any]],
    w2_high: dict[str, Any],
) -> list[str]:
    calendar_day_count = len(requested_dates)
    w1, w2 = (data[wallet] for wallet in WALLETS)
    w1_style, w2_style = w1["style"], w2["style"]
    w2_low = w2_high["low_buy_high_sell"]
    w2_label = w2_style["style_label"]
    w1_label = w1_style["style_label"]
    if w2_low["near_full_asset_count"] > w2_low["all_partial_asset_count"]:
        w2_exit_sentence = "接近全部观察库存退出的资产更多"
    elif w2_low["all_partial_asset_count"] > w2_low["near_full_asset_count"]:
        w2_exit_sentence = "部分退出的资产更多"
    else:
        w2_exit_sentence = "部分退出和接近全部退出没有单一主导模式"
    common_multi = min(w1["multi_yes"]["multi_yes_days"], w2["multi_yes"]["multi_yes_days"])
    if w2_low["assets"]:
        low_high_proof = (
            f"已在{w2_low['fills']}笔高价SELL、{w2_low['assets']}个资产、{w2_low['dates']}个天气日中观察到此前0—30¢ BUY YES；"
            f"低价BUY均价{fmt_price(w2_low['low_0_30_buy_weighted_price'])}，高价SELL均价{fmt_price(w2_low['high_90_100_sell_weighted_price'])}。"
        )
    else:
        low_high_proof = "当前没有可追溯的此前0—30¢ BUY YES→90—100¢ SELL YES资产路径。"
    def time_summary(wallet_data: dict[str, Any]) -> str:
        return f"BUY YES D0主时段{main_time_for_wallet(wallet_data, 'BUY YES')}；BUY NO D0主时段{main_time_for_wallet(wallet_data, 'BUY NO')}"
    def main_time_for_wallet(wallet_data: dict[str, Any], key: str) -> str:
        rows = [row for row in wallet_data["time"][key] if row["bucket"].startswith("D0_")]
        return TIME_NAMES[max(rows, key=lambda row: row["usd"])["bucket"]] if rows else "—"
    def price_summary(wallet_data: dict[str, Any], key: str) -> str:
        row = max(wallet_data["price"][key], key=lambda item: item["usd"])
        return f"{PRICE_NAMES[row['band']]}（USD占比{fmt_pct(row['usd_share'])}）"
    def composition_summary(wallet_data: dict[str, Any]) -> str:
        counts = wallet_data["category_counts"]
        return f"MULTI_YES_ONLY={counts.get('MULTI_YES_ONLY', 0)}、MULTI_YES_PLUS_NO={counts.get('MULTI_YES_PLUS_NO', 0)}、NO_BUY={counts.get('NO_BUY', 0)}（/{calendar_day_count}日）"
    def sell_exit_summary(wallet_data: dict[str, Any]) -> str:
        sell_yes = wallet_data["price"]["SELL YES"]
        max_row = max(sell_yes, key=lambda item: item["usd"])
        return f"SELL YES最大USD桶为{PRICE_NAMES[max_row['band']]}（{fmt_pct(max_row['usd_share'])}），SELL fills={sum(row['fills'] for row in sell_yes)}"
    style_table = md_table(
        ["钱包", "标签", "证据驱动理由"],
        [
            [wallet, data[wallet]["style"]["style_label"], f"BUY fills={data[wallet]['style']['buy_fill_count']}、SELL fills={data[wallet]['style']['sell_fill_count']}、重复双向资产占比{data[wallet]['style']['repeated_asset_share']:.1%}、同小时双向组数{data[wallet]['style']['same_hour_two_way']}。"]
            for wallet in WALLETS
        ],
    )
    return [
        "",
        "## 11. 两个钱包的可确认模式",
        "",
        f"### {WALLETS[0]}",
        "",
        "#### 可以确认",
        "",
        f"- BUY fills={w1_style['buy_fill_count']}、SELL fills={w1_style['sell_fill_count']}；{time_summary(w1)}。",
        f"- 逐资产有BUY也有SELL的资产数为{w1['path_metrics']['buy_and_sell_assets']}，BUY后发生SELL的天气日数为{w1['path_metrics']['buy_then_sell_weather_days']}；不是把不同温度合同拼接出来的结果。",
        f"- 温度组合中多YES相关天气日为{w1['multi_yes']['multi_yes_days']}，相邻YES组合率为{w1['multi_yes']['adjacent_rate']:.2f}%。",
        "",
        "#### 合理但尚未证明",
        "",
        f"- {SHORT_HOLD_HOURS:.0f}小时内BUY→SELL比例为{w1_style['short_hold_ratio']:.2f}%，可支持时间邻近，但不能证明订单意图。",
        f"- {sell_exit_summary(w1)}；高价卖出不等于完整退出或盈利。",
        "",
        "#### 不支持的说法",
        "",
        "- 没有 maker/taker 或订单簿证据，不能确定称为做市商。",
        "- 不能把公开卖出成交解释为完整账户库存或完整PnL。",
        "",
        f"### {WALLETS[1]}",
        "",
        "#### 可以确认",
        "",
        f"- BUY fills={w2_style['buy_fill_count']}、SELL fills={w2_style['sell_fill_count']}；{time_summary(w2)}。",
        f"- BUY YES主要价格带为{price_summary(w2, 'BUY YES')}；{low_high_proof}",
        f"- 逐笔观察库存退出中，{w2_low['partial_fill_count']}笔部分、{w2_low['near_full_fill_count']}笔接近全部、{w2_low['exceeds_fill_count']}笔超过观察库存、{w2_low['unknown_fill_count']}笔库存未知；资产级结论由这些逐笔结果动态生成。",
        "",
        "#### 合理但尚未证明",
        "",
        f"- 多YES覆盖后等待高价部分退出可以作为候选路径，但只适用于已匹配的{w2_low['assets']}个资产，不代表所有BUY。",
        "",
        "#### 不支持的说法",
        "",
        "- 不能据此确定方向性预测、盈利或完整仓位管理。",
        "- 不能把 SELL 金额、观察库存比例或价格差直接解释为PnL/ROI。",
        "",
        "## 12. 最值得学习的3条与不适合复制的行为",
        "",
        "1. 可学习：严格拆分 BUY YES、BUY NO、SELL YES、SELL NO，并按天气日和当地时段复盘。",
        "2. 可学习：只在同一 wallet + weather_date + condition_id + asset + outcome + temperature_bucket 内做路径匹配。",
        "3. 可学习：把0—30¢低价买入、90—100¢高价卖出、逐笔观察库存和温度覆盖作为待验证模拟规则，而不是直接复制结论。",
        "",
        "不适合直接复制：机械追逐低价/高价、把多温度覆盖当作确定预测、把观察库存当作真实库存、或把高成交量直接命名为做市。",
        "",
        "## 13. 最终对比",
        "",
        md_table(
            ["维度", WALLETS[0], WALLETS[1]],
            [
                ["建仓时间", time_summary(w1), time_summary(w2)],
                ["主要YES价格", price_summary(w1, "BUY YES"), price_summary(w2, "BUY YES")],
                ["NO使用方法", f"BUY NO {price_summary(w1, 'BUY NO')}；混合日NO总体USD占比{w1['no']['overall_no_usd_share']:.2f}%", f"BUY NO {price_summary(w2, 'BUY NO')}；混合日NO总体USD占比{w2['no']['overall_no_usd_share']:.2f}%"],
                ["温度组合", composition_summary(w1), composition_summary(w2)],
                ["卖出频率", f"{w1_style['sell_fill_count']} fills", f"{w2_style['sell_fill_count']} fills"],
                ["高价退出", sell_exit_summary(w1), sell_exit_summary(w2)],
                ["持有时间", f"首次BUY→首次SELL中位{fmt_hours(w1['path_metrics']['first_buy_to_first_sell_percentiles']['P50'] * 3600 if w1['path_metrics']['first_buy_to_first_sell_percentiles']['P50'] is not None else None)}", f"低价BUY→高价SELL中位{fmt_hours(w2_low['median_low_hold_seconds'])}"],
                ["反复交易", f"BUY→SELL切换{w1_style['buy_sell_transitions']}、SELL→BUY切换{w1_style['sell_buy_transitions']}", f"BUY→SELL切换{w2_style['buy_sell_transitions']}、SELL→BUY切换{w2_style['sell_buy_transitions']}"],
                ["疑似风格", w1_label, w2_label],
                ["证据强度", "READY；逐资产路径和逐笔观察库存可复核", "READY；低买高卖路径和逐笔观察库存可复核"],
            ],
        ),
        "",
        "## 14. 特别回答",
        "",
        f"1. 两个钱包共有的稳定模式：两者均有多YES组合（共同至少{common_multi}个天气日），且路径分析只在同一资产合同内成立；公开证据都不能替代完整库存。",
        f"2. 只属于钱包一的模式：SELL fills={w1_style['sell_fill_count']}、SELL→BUY切换={w1_style['sell_buy_transitions']}、同小时双向组数={w1_style['same_hour_two_way']}，主动再平衡特征更强。",
        f"3. 只属于钱包二的模式：BUY fills={w2_style['buy_fill_count']}、SELL fills={w2_style['sell_fill_count']}，低价BUY→高价SELL路径覆盖{w2_low['dates']}个天气日；标签由通用分类器生成：{w2_label}。",
        f"4. “低价买入、多温度覆盖、高价部分退出”是否被逐资产证据证明：{low_high_proof}逐笔观察库存的资产级主导结果为：{w2_low['all_partial_asset_count']}个ALL_PARTIAL、{w2_low['near_full_asset_count']}个HAS_NEAR_FULL、{w2_low['mixed_asset_count']}个MIXED、{w2_low['exceeds_asset_count']}个HAS_EXCESS、{w2_low['unknown_asset_count']}个UNKNOWN；这说明{w2_exit_sentence}，不代表完整策略。",
        "5. 可进入模拟交易候选规则：本地天气日分桶、四类方向分开、同资产匹配、逐笔库存更新、0—30¢与90—100¢路径、部分退出比例和相邻温度覆盖；必须继续加入滑点、未成交和证据缺口。",
        "6. 还不能复制：真实库存、完整订单、maker/taker、主观预测、盈利能力、PnL/ROI/胜率，以及把任何单一钱包路径外推为通用策略。",
        "",
        "## 15. 机器可读输出",
        "",
        "同一离线分析函数同时生成 `SECOND_STAGE_TRADER_PATTERN_COMPARISON.json`、`asset_path_summary.csv`、`high_sell_path_fills.csv`、`high_sell_path_assets.csv`、`daily_temperature_structure.csv`、`trader_style_metrics.csv`。",
        "",
        "## 16. 复现",
        "",
        f"```bash\npython3 scripts/second_stage_trader_pattern_analysis.py \\\n+  --output-root {root} \\\n+  --report {report_path} \\\n+  --date-from {min(requested_dates).isoformat()} \\\n+  --date-to {max(requested_dates).isoformat()} \\\n+  --city {city}\n```".replace("\n+  --", "\n  --"),
        "",
        "本脚本为本地离线分析，不会发起网络请求、签名或下单。",
    ]


def render_report(
    root: Path,
    report_path: Path,
    start: date,
    end: date,
    city: str,
    requested_dates: list[date],
    events: list[dict[str, Any]],
    denominator: dict[str, Any],
    data: dict[str, dict[str, Any]],
    w2_high: dict[str, Any],
) -> str:
    calendar_day_count = len(requested_dates)
    event_count = len(events)
    duplicate_dates = sorted(denominator.get("old_new_duplicate_dates", []))
    duplicate_date_text = ", ".join(duplicate_dates) if duplicate_dates else "无"
    duplicate_event_text = "; ".join(
        f"{event['weather_date']} / {event['event_slug']} / event_id={event['event_id']}"
        for event in denominator.get("duplicate_events", [])
    ) or "无"
    duplicate_condition_text = "; ".join(
        f"{event['event_slug']}={event['condition_count']} conditions"
        for event in denominator.get("duplicate_events", [])
    ) or "无"
    duplicate_event_id_text = "; ".join(
        f"{event_id}×{count}" for event_id, count in sorted(denominator.get("duplicate_event_ids", {}).items())
    ) or "无"
    quality_failures = []
    source_only_total = 0
    for wallet in WALLETS:
        quality = data[wallet]["quality"]
        source_only_total += int(quality.get("activity_only_fill_count", 0)) + int(quality.get("trades_only_fill_count", 0))
        for metric in ("api_request_failure_count", "unknown_timezone_fill_count", "unknown_relative_day_count", "orphan_sell_asset_count", "market_identity_conflict_count"):
            if int(quality.get(metric, 0)):
                quality_failures.append(f"{wallet}:{metric}={quality[metric]}")
    quality_sentence = "未发现 API failure、unknown timezone/relative day、orphan sell 或 market identity conflict。" if not quality_failures else "质量告警：" + ", ".join(quality_failures) + "。"
    w1 = data[WALLETS[0]]
    w2 = data[WALLETS[1]]
    w1_rows = w1["rows"]
    w2_rows = w2["rows"]
    def class_price(wallet_data: dict[str, Any], key: str) -> str:
        rows = wallet_data["price"][key]
        row = max(rows, key=lambda item: item["usd"])
        return PRICE_NAMES[row["band"]]
    def main_d0_time(wallet_data: dict[str, Any], key: str = "BUY YES") -> str:
        rows = [row for row in wallet_data["time"][key] if row["bucket"].startswith("D0_")]
        return TIME_NAMES[max(rows, key=lambda item: item["usd"])["bucket"]] if rows else "—"
    def observed_exit_sentence(summary: dict[str, Any]) -> str:
        partial = summary["all_partial_asset_count"]
        near = summary["near_full_asset_count"]
        if near > partial:
            return "接近全部观察库存退出的资产更多"
        if partial > near:
            return "部分退出的资产更多"
        return "部分退出和接近全部退出没有单一主导模式"
    w2_low = w2_high["low_buy_high_sell"]
    w2_exit_sentence = observed_exit_sentence(w2_low)
    style_threshold_sentence = (
        f"风格阈值：BUY_DOMINANT 要求 BUY fill 占比≥{STYLE_BUY_DOMINANT_MIN_BUY_FILL_SHARE:.0%} 且 SELL/BUY fill≤{STYLE_BUY_DOMINANT_MAX_SELL_TO_BUY_FILL_RATIO:.0%}；"
        f"ACTIVE_REBALANCER 要求重复双向资产占比≥{STYLE_ACTIVE_MIN_REPEATED_ASSET_SHARE:.0%}、SELL→BUY比例≥{STYLE_ACTIVE_MIN_SELL_REBUY_RATIO:.0%}、同小时双向组数≥{STYLE_ACTIVE_MIN_SAME_HOUR_TWO_WAY}。"
    )
    lines = [
        "# SECOND_STAGE_TRADER_PATTERN_COMPARISON",
        "",
        f"研究范围：{city}每日最高温市场；天气日期 {start.isoformat()} 至 {end.isoformat()}。",
        "",
        "本阶段只读取既有 `all_fills.csv`、`summary.json`、`data_quality.csv` 和 `_public_evidence`，没有重新抓取。价格带沿用第一阶段固定注册桶；本报告只描述公开 fills，不推断未成交订单、完整库存、PnL、ROI、胜率或主观意图。",
        "",
        "## 0. 证据状态",
        "",
        md_table(
            ["钱包", "pattern_report_status", "target events", "target conditions", "market completeness", "pagination", "source-only fills"],
            [
                [wallet, data[wallet]["quality"].get("pattern_report_status"), data[wallet]["quality"].get("target_event_count"), data[wallet]["quality"].get("target_condition_count"), data[wallet]["quality"].get("market_complete_count"), data[wallet]["quality"].get("pagination_saturation_status"), data[wallet]["quality"].get("activity_only_fill_count", "0")]
                for wallet in WALLETS
            ],
        ),
        "",
        f"两个钱包的目标市场查询均为 COMPLETE；{quality_sentence}本轮 source-only fill 总数为{source_only_total}，其余成交有 activity 与 trades 双源对应；这不会改变本地报告的 READY 状态。",
        f"SECOND_STAGE_NETWORK_CALL_COUNT={SECOND_STAGE_NETWORK_CALL_COUNT}。底层既有 evidence 的历史抓取计数保留在 run_manifest，不属于本轮第二阶段分析。",
        "",
        "## 1. 日期分母核查",
        "",
        "```text",
        f"REQUESTED_CALENDAR_DAY_COUNT={denominator['requested_days']}",
        f"UNIQUE_WEATHER_DATE_COUNT={denominator['unique_weather_dates']}",
        f"EVENT_COUNT={denominator['event_count']}",
        f"DUPLICATE_EVENT_DATE_COUNT={denominator['duplicate_event_date_count']}",
        f"OUT_OF_RANGE_EVENT_COUNT={denominator['out_of_range_event_count']}",
        "```",
        "",
        f"结论：请求范围首尾包含确实是{calendar_day_count}个自然天气日。当前 event 数为{event_count}，因为重复天气日期为{duplicate_date_text}；重复事件为：{duplicate_event_text}。这些事件分别为{duplicate_condition_text}；范围外事件数为{denominator['out_of_range_event_count']}；同一 event_id 重复记录为{duplicate_event_id_text}。",
        "",
        f"DENOMINATOR_EVENT_EXPLANATION={denominator['unique_weather_dates']} unique weather dates + {event_count - denominator['unique_weather_dates']} extra same-date event records on {duplicate_date_text}. Daily ratios use {calendar_day_count} calendar dates; event-level path tables retain all {event_count} event records.",
        f"DENOMINATOR_97_EXPLANATION={denominator['unique_weather_dates']} unique weather dates + {event_count - denominator['unique_weather_dates']} extra same-date event records on {duplicate_date_text}; 97 is an event-record denominator, not a natural-day denominator.",
        "",
        f"每个日期的 event 数如下；重复日期为{duplicate_date_text}：",
        md_table(
            ["日期", "events", "日期", "events"],
            [[left, denominator["event_counts_by_date"][left], right, denominator["event_counts_by_date"][right]] for left, right in zip(list(denominator["event_counts_by_date"])[::2], list(denominator["event_counts_by_date"])[1::2])],
        ),
        "",
        "重复事件明细：",
        md_table(
            ["weather_date", "event_id", "event_slug", "condition_count", "completeness"],
            [[event["weather_date"], event["event_id"], event["event_slug"], event["condition_count"], event["completeness_status"]] for event in denominator["duplicate_events"]],
        ),
        "",
        "## 2. BUY / SELL 时间拆分",
        "",
        f"D0 是{city}市场当地天气日；D0 下的四个小时桶是 D0 的明细，不应与 D0 再相加。每个表的占比均以该 BUY/SELL + YES/NO 类别自身为分母。POST_EVENT、EARLIER_THAN_D2、UNKNOWN 的额外成交行由本地数据动态检查；当前总计为{sum(1 for wallet in WALLETS for row in data[wallet]['rows'] if row['relative_weather_day'] == 'POST_EVENT')}、{sum(1 for wallet in WALLETS for row in data[wallet]['rows'] if row['relative_weather_day'] == 'EARLIER_THAN_D2')}、{sum(1 for wallet in WALLETS for row in data[wallet]['rows'] if row['relative_weather_day'] == 'UNKNOWN')}。",
    ]
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}"])
        for key in ("BUY YES", "BUY NO", "SELL YES", "SELL NO"):
            lines.extend(["", f"#### {key}", "", render_time_table(data[wallet]["time"][key])])
    lines.extend(["", "## 3. 每个事件的资金路径时间", "", f"以下保留{event_count}个 event 记录，因此重复天气日期的多个 slug 分开；BUY资金25/50/75% 是该 event 内按时间排序的 BUY USD 累计阈值，不是仓位比例。"])
    for wallet in WALLETS:
        lines.extend(["", details(f"{wallet}：{event_count}个event路径表", render_event_path_table(data[wallet]["event_paths"]))])
    lines.extend(["", "## 4. 完整价格带占比", "", "每个表的 fill_share、shares_share、usd_share 分别以该四类自身总量为分母；NO 价格保持 NO 合约自身价格，未转换为 YES 等价价格。USD加权均价 = 实际 trade USD / shares。"])
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}"])
        for key in ("BUY YES", "BUY NO", "SELL YES", "SELL NO"):
            lines.extend(["", f"#### {key}", "", render_price_table(data[wallet]["price"][key])])
    w1_sell_yes = data[WALLETS[0]]["price"]["SELL YES"]
    w1_sell_yes_largest_by = max(w1_sell_yes, key=lambda row: row["usd"])["band"]
    w1_sell_yes_total_usd = sum(row["usd"] for row in w1_sell_yes)
    w1_sell_yes_total_shares = sum(row["shares"] for row in w1_sell_yes)
    w1_sell_yes_weighted = w1_sell_yes_total_usd / w1_sell_yes_total_shares if w1_sell_yes_total_shares else None
    w1_sell_yes_by_fill = max(w1_sell_yes, key=lambda row: row["fills"])
    w1_sell_yes_by_shares = max(w1_sell_yes, key=lambda row: row["shares"])
    w1_sell_yes_by_usd = max(w1_sell_yes, key=lambda row: row["usd"])
    lines.extend([
        "",
        "### 钱包一 SELL YES 的90—100美分矛盾核查",
        "",
        f"{WALLETS[0]} SELL YES 总额为${w1_sell_yes_total_usd:,.2f}、总量{w1_sell_yes_total_shares:,.2f} shares，整体实际加权均价约{fmt_price(w1_sell_yes_weighted)}。当前报告的“主要价格带”使用的是固定价格带中按 USD 金额最大的单一桶，不是多数占比：",
        "",
        md_table(
            ["判断口径", "最大价格带", "该带占比", "是否过半"],
            [
                ["fill_count", PRICE_NAMES[w1_sell_yes_by_fill["band"]], fmt_pct(w1_sell_yes_by_fill["fill_share"]), "是" if w1_sell_yes_by_fill["fill_share"] > 50 else "否"],
                ["shares", PRICE_NAMES[w1_sell_yes_by_shares["band"]], fmt_pct(w1_sell_yes_by_shares["shares_share"]), "是" if w1_sell_yes_by_shares["shares_share"] > 50 else "否"],
                ["trade USD", PRICE_NAMES[w1_sell_yes_by_usd["band"]], fmt_pct(w1_sell_yes_by_usd["usd_share"]), "是" if w1_sell_yes_by_usd["usd_share"] > 50 else "否"],
            ],
        ),
        "",
        f"因此，{PRICE_NAMES[w1_sell_yes_by_usd['band']]}是按 USD 的最大单一桶，占{fmt_pct(w1_sell_yes_by_usd['usd_share'])}；按笔数最大的是{PRICE_NAMES[w1_sell_yes_by_fill['band']]}，按 shares 最大的是{PRICE_NAMES[w1_sell_yes_by_shares['band']]}。报告中的“主要”应明确为“最大单一USD桶”，不等于绝大多数。",
        "",
        "## 5. 逐资产 BUY / SELL 路径匹配",
        "",
        "匹配键严格使用 wallet + weather_date + condition_id + asset + outcome + temperature_bucket。不同温度合同不会被拼接。sold_share_ratio 仅为该资产观察到的 SELL shares / BUY shares，不是完整库存或完整 PnL。",
    ])
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}", "", render_path_metrics(data[wallet]["path_metrics"])])
    lines.extend(["", "## 6. 钱包二：低价 BUY YES → 90—100美分 SELL YES 验证", "", "假设定义：同一资产先出现 BUY YES，且此前至少有一笔 BUY YES 落在0—30美分固定价格带，之后出现同一资产的90—100美分 SELL YES。只按同一资产追溯，不跨温度合同。", "", render_high_cases(w2_high)])
    low = w2_high["low_buy_high_sell"]
    label = "PROVEN_IN_OBSERVED_FILLS" if low["assets"] and low["dates"] >= 10 else ("PROVEN_BUT_FEW_DATES" if low["assets"] else "NOT_PROVEN")
    lines.extend(["", f"LOW_BUY_HIGH_SELL_PATTERN={label}", "", "结论：若上述路径存在，它只证明公开成交中出现了同资产低价买入后高价卖出的可观察路径；不能证明完整仓位、全部卖出、盈利或主观策略。"])
    lines.extend(["", "## 7. 钱包一：主动交易 / 做市型判断", "", "“短时间”固定定义为同一资产首次BUY后6小时内出现首次SELL；比例按有BUY后SELL的资产组计算。该阈值用于可复核，不代表原交易员的规则。"])
    for wallet in WALLETS:
        style = data[wallet]["style"]
        lines.extend(["", f"### {wallet}", "", render_style(style), "", "成交最密集的10个天气日：", md_table(["天气日期", "fills", "BUY fills", "SELL fills", "trade USD"], [[item["weather_date"], item["fills"], item["buy_fills"], item["sell_fills"], fmt_num(item["usd"])] for item in style["top_days"]])])
        lines.extend(["", f"YES和NO同时有公开成交的天气日数：{len(style['yes_no_active_days'])}。maker/taker字段：{'NOT_AVAILABLE' if style['maker_taker'] is None else ', '.join(style['maker_taker'])}。", ""])
    def style_reason(style: dict[str, Any]) -> str:
        label = style["style_label"]
        if label == "BUY_DOMINANT_ACCUMULATOR":
            return f"BUY fill占比{style['buy_fill_share']:.1%}、SELL/BUY fill比{style['sell_to_buy_fill_ratio']:.1%}，满足买入主导阈值；不代表完整库存或方向意图。"
        if label == "ACTIVE_REBALANCER":
            return f"重复双向资产占比{style['repeated_asset_share']:.1%}、SELL→BUY比例{style['sell_then_rebuy_ratio_decimal']:.1%}、同小时双向组数{style['same_hour_two_way']}，满足主动再平衡阈值；没有maker/taker证据。"
        if label == "POSSIBLE_MARKET_MAKER":
            return f"同小时双向组数{style['same_hour_two_way']}、短持有比例{style['short_hold_ratio_decimal']:.1%}、SELL→BUY比例{style['sell_then_rebuy_ratio_decimal']:.1%}达到可能做市候选阈值，但maker/taker不可用。"
        return "当前指标没有满足预设的主动再平衡、买入主导或可能做市候选阈值。"
    lines.extend(["", "风格标签采用参数化规则：", style_threshold_sentence, "", md_table(["钱包", "标签", "理由"], [
        [wallet, data[wallet]["style"]["style_label"], style_reason(data[wallet]["style"])] for wallet in WALLETS
    ])])
    lines.extend(["", "## 8. 温度组合：互斥主分类", "", f"每个{calendar_day_count}日历天气日严格分入一个类别；重复天气日期的多个 event 在天气日级合并，重复 event 不重复计日。yes_bucket_count/no_bucket_count 是该天气日 BUY 记录中的唯一温度桶数。"])
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}", "", render_category_table(data[wallet], calendar_day_count)])
        lines.extend(["", "辅助特征统计：", "", md_table(["特征", "天气日数", "比例"], [
            ["adjacent_yes", sum(day["adjacent_yes"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["adjacent_yes"] for day in data[wallet]["temperature_days"]), calendar_day_count))],
            ["non_adjacent_yes", sum(day["non_adjacent_yes"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["non_adjacent_yes"] for day in data[wallet]["temperature_days"]), calendar_day_count))],
            ["same_bucket_both_sides", sum(day["same_bucket_both_sides"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["same_bucket_both_sides"] for day in data[wallet]["temperature_days"]), calendar_day_count))],
            ["cross_bucket_yes_no", sum(day["cross_bucket_yes_no"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["cross_bucket_yes_no"] for day in data[wallet]["temperature_days"]), calendar_day_count))],
        ])])
    lines.extend(["", "## 9. 多YES组合结构", "", "‘便宜尾部YES’固定定义为 BUY YES 0—10美分价格带；相邻YES只按精确温度相差1°C识别。主力YES档是该天气日 BUY YES USD 最大的温度桶。"])
    for wallet in WALLETS:
        item = data[wallet]["multi_yes"]
        distribution = ", ".join(f"{count}档:{n}天" for count, n in sorted(item["multi_yes_distribution"].items())) or "无多YES天气日"
        lines.extend(["", f"### {wallet}", "", md_table(["指标", "结果"], [
            ["有YES BUY天气日", item["yes_active_days"]],
            ["YES档数量 P25/P50/P75/P90", "/".join("—" if value is None else f"{value:.2f}" for value in item["yes_bucket_count_percentiles"].values())],
            ["主力YES档USD占全部YES BUY USD P25/P50/P75/P90", "/".join("—" if value is None else f"{value:.2f}%" for value in item["main_usd_share_percentiles"].values())],
            ["主力档为最贵YES档", f"{item['main_most_expensive_count']}/{item['yes_active_days']}天"],
            ["主力档为最便宜YES档", f"{item['main_cheapest_count']}/{item['yes_active_days']}天"],
            ["多YES天气日", item["multi_yes_days"]],
            ["多YES档数分布", distribution],
            ["相邻YES组合率", fmt_pct(item["adjacent_rate"])],
            ["存在非相邻YES对比例", fmt_pct(item["non_adjacent_pair_rate"])],
            ["便宜尾部YES USD占比", fmt_pct(item["cheap_tail_usd_share"])],
            ["便宜尾部YES shares占比", fmt_pct(item["cheap_tail_shares_share"])],
            ["主力档与相邻档价格差 P25/P50/P75/P90", "/".join("—" if value is None else f"{value:.2f}¢" for value in item["adjacent_price_gap_percentiles_cents"].values()) + f"（n={item['adjacent_price_gap_count']}）"],
        ])])
    lines.extend(["", "## 10. NO 在组合中的位置", "", "NO 位置先按价格合同的 bucket_kind、bucket_low、bucket_high 与 YES 温度范围判断；NO 只表示该 NO 合约的公开成交，不解释为押该温度发生。位置分类优先级为：同温度、两侧、全部下方、全部上方、范围内部、无法判断。"])
    for wallet in WALLETS:
        item = data[wallet]["no"]
        lines.extend(["", f"### {wallet}", "", md_table(["指标", "结果"], [
            ["MIXED YES/NO天气日", item["mixed_days"]],
            ["NO位置分类", "; ".join(f"{key}:{value}天" for key, value in sorted(item["position_counts"].items())) or "无"],
            ["首次买入顺序", "; ".join(f"{key}:{value}天" for key, value in sorted(item["first_buy_counts"].items())) or "无"],
            ["NO晚于YES", item["late_counts"].get("NO晚于YES", 0)],
            ["NO晚于BUY资金50%", item["late_counts"].get("NO晚于BUY资金50%", 0)],
            ["混合天气日NO投入USD占当天BUY USD P25/P50/P75/P90", "/".join("—" if value is None else f"{value:.2f}%" for value in item["daily_no_usd_share_percentiles"].values())],
            ["混合天气日NO总体USD占比", fmt_pct(item["overall_no_usd_share"])],
        ]), "", "混合天气日 BUY NO 价格带：", render_price_table(item["price_rows"])])
    # Sections 11 onward are generated below from the current analysis data.
    conclusion_start = next((index for index, line in enumerate(lines) if line == "## 11. 两个钱包的可确认模式"), len(lines))
    lines = lines[:conclusion_start]
    lines.extend(render_dynamic_conclusion_sections(root, report_path, city, requested_dates, data, w2_high))
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--city", default="beijing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start = parse_date(args.date_from)
    end = parse_date(args.date_to)
    if args.city != "beijing":
        raise SystemExit("This second-stage script is scoped to the requested Beijing evidence.")
    make_report(args.output_root.resolve(), args.report.resolve(), start, end, args.city)
    print(args.report.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
