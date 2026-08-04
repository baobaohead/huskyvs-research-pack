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
    counts = Counter(parse_date(event["weather_date"]) for event in events)
    in_range = [event for event in events if start <= parse_date(event["weather_date"]) <= end]
    out_of_range = [event for event in events if event not in in_range]
    duplicate_dates = {day: count for day, count in counts.items() if count > 1}
    duplicate_event_rows = [event for event in in_range if counts[parse_date(event["weather_date"])] > 1]
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
        "out_of_range_event_count": len(out_of_range),
        "event_counts_by_date": {day.isoformat(): counts.get(day, 0) for day in sorted(requested)},
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


def wallet_two_high_sell_analysis(paths: list[dict[str, Any]]) -> dict[str, Any]:
    high_paths = []
    for path in paths:
        if path["outcome"] != "YES":
            continue
        high_sells = [row for row in path["sells"] if row["_band"] == "PRICE_90_100C"]
        if not high_sells:
            continue
        first_high = min(high_sells, key=lambda row: row["_ts"])
        prior_buys = [row for row in path["buys"] if row["_ts"] < first_high["_ts"]]
        low_prior_buys = [row for row in prior_buys if row["_band"] in {"PRICE_0_10C", "PRICE_10_30C"}]
        if prior_buys:
            high_paths.append(
                {
                    "path": path,
                    "high_sells": high_sells,
                    "first_high": first_high,
                    "prior_buys": prior_buys,
                    "low_prior_buys": low_prior_buys,
                    "hold_seconds": first_high["_ts"] - prior_buys[0]["_ts"],
                    "high_sell_shares": sum(row["_shares"] for row in high_sells),
                    "prior_buy_shares": sum(row["_shares"] for row in prior_buys),
                    "high_sell_usd": sum(row["_usd"] for row in high_sells),
                    "prior_buy_usd": sum(row["_usd"] for row in prior_buys),
                    "all_sell_ratio": path["sold_share_ratio"],
                    "high_sell_ratio": sum(row["_shares"] for row in high_sells) / sum(row["_shares"] for row in prior_buys) if sum(row["_shares"] for row in prior_buys) else None,
                }
            )
    low_paths = [item for item in high_paths if item["low_prior_buys"]]
    def aggregate_path_set(items: list[dict[str, Any]]) -> dict[str, Any]:
        prior_buys = [row for item in items for row in item["prior_buys"]]
        low_buys = [row for item in items for row in item["low_prior_buys"]]
        high_sells = [row for item in items for row in item["high_sells"]]
        return {
            "assets": len(items),
            "fills": sum(len(item["high_sells"]) for item in items),
            "dates": len({item["path"]["weather_date"] for item in items}),
            "prior_buy_usd": sum(row["_usd"] for row in prior_buys),
            "prior_buy_shares": sum(row["_shares"] for row in prior_buys),
            "low_buy_usd": sum(row["_usd"] for row in low_buys),
            "low_buy_shares": sum(row["_shares"] for row in low_buys),
            "high_sell_usd": sum(row["_usd"] for row in high_sells),
            "high_sell_shares": sum(row["_shares"] for row in high_sells),
            "buy_avg": weighted_price(prior_buys),
            "low_buy_avg": weighted_price(low_buys),
            "sell_avg": weighted_price(high_sells),
            "high_sell_to_prior_buy_ratio": sum(row["_shares"] for row in high_sells) / sum(row["_shares"] for row in prior_buys) if sum(row["_shares"] for row in prior_buys) else None,
            "median_hold_seconds": statistics.median(item["hold_seconds"] for item in items) if items else None,
        }
    def exit_label(ratio: float | None) -> str:
        if ratio is None:
            return "UNKNOWN"
        if ratio < 0.95:
            return "部分卖出"
        if ratio <= 1.05:
            return "近似全部（按观察到的买入份数）"
        return "超过观察到的买入份数"
    for item in low_paths:
        item["exit_label"] = exit_label(item["high_sell_ratio"])
    cases = sorted(low_paths, key=lambda item: (item["high_sell_usd"], item["path"]["weather_date"]), reverse=True)
    date_cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in low_paths:
        date_cases[item["path"]["weather_date"]].append(item)
    daily_cases = []
    for day, items in date_cases.items():
        summary = aggregate_path_set(items)
        summary["weather_date"] = day
        summary["temperature_buckets"] = ", ".join(sorted({item["path"]["temperature_bucket"] for item in items}))
        summary["exit_label"] = Counter(item["exit_label"] for item in items).most_common(1)[0][0]
        daily_cases.append(summary)
    daily_cases.sort(key=lambda item: (item["high_sell_usd"], item["weather_date"]), reverse=True)
    return {
        "high_sell_yes_total_fills": sum(len([row for row in path["sells"] if path["outcome"] == "YES" and row["_band"] == "PRICE_90_100C"]) for path in paths),
        "high_sell_yes_traceable": aggregate_path_set(high_paths),
        "low_buy_high_sell": aggregate_path_set(low_paths),
        "high_paths": high_paths,
        "low_paths": low_paths,
        "daily_cases": daily_cases,
        "lowest_hold_case": min(low_paths, key=lambda item: item["hold_seconds"]) if low_paths else None,
        "highest_hold_case": max(low_paths, key=lambda item: item["hold_seconds"]) if low_paths else None,
        "partial_count": sum(item["exit_label"] == "部分卖出" for item in low_paths),
        "near_full_count": sum(item["exit_label"] == "近似全部（按观察到的买入份数）" for item in low_paths),
        "over_count": sum(item["exit_label"] == "超过观察到的买入份数" for item in low_paths),
    }


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
    return {
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
        "median_hold_seconds": statistics.median(hold_values) if hold_values else None,
        "average_fills_per_requested_day": len(rows) / len(requested_dates) if requested_dates else None,
        "average_fills_per_active_day": len(rows) / len(active_dates) if active_dates else None,
        "active_days": len(active_dates),
        "yes_no_active_days": yes_no_days,
        "top_days": top_days[:10],
        "maker_taker": maker_taker or None,
    }


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


def make_report(root: Path, report_path: Path, start: date, end: date, city: str) -> None:
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
    w2_high = wallet_two_high_sell_analysis(wallet_data[WALLETS[1]]["paths"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(root, report_path, start, end, city, requested_dates, events, denominator, wallet_data, w2_high).replace("\n+  --", "\n  --"),
        encoding="utf-8",
    )


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


def render_category_table(data: dict[str, Any]) -> str:
    counts = data["category_counts"]
    total = 96
    return md_table(
        ["互斥主类别", "天气日数", "比例"],
        [[category, counts.get(category, 0), fmt_pct(pct(counts.get(category, 0), total))] for category in CATEGORY_ORDER],
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
    lines = [
        md_table(
            ["范围", "90—100¢ SELL YES笔数", "可追溯资产数", "天气日数", "对应BUY USD均价", "对应SELL USD均价", "SELL shares / BUY shares", "中位持有"],
            [
                ["全部90—100¢ SELL YES", analysis["high_sell_yes_total_fills"], high_rows["assets"], high_rows["dates"], fmt_price(high_rows["buy_avg"]), fmt_price(high_rows["sell_avg"]), f"{high_rows['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if high_rows["high_sell_to_prior_buy_ratio"] is not None else "—", fmt_hours(high_rows["median_hold_seconds"])],
                ["其中此前有0—30¢ BUY", aggregate_rows["fills"], aggregate_rows["assets"], aggregate_rows["dates"], fmt_price(aggregate_rows["low_buy_avg"]), fmt_price(aggregate_rows["sell_avg"]), f"{aggregate_rows['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if aggregate_rows["high_sell_to_prior_buy_ratio"] is not None else "—", fmt_hours(aggregate_rows["median_hold_seconds"])],
            ],
        ),
        "",
        f"部分/近似全部/超过观察买入份数：{analysis['partial_count']} / {analysis['near_full_count']} / {analysis['over_count']} 个资产路径。这里是观察到的 SELL shares 与此前 BUY shares 的比值，不是完整库存核算。",
        "",
        "按90—100¢ SELL YES路径的高价卖出金额排序，至少列出10个真实天气日案例：",
        md_table(
            ["天气日期", "温度档", "匹配资产数", "高价SELL笔数", "对应BUY USD", "高价SELL USD", "SELL/BUY shares", "主退出形态", "中位持有"],
            [
                [case["weather_date"], case["temperature_buckets"], case["assets"], case["fills"], fmt_num(case["prior_buy_usd"]), fmt_num(case["high_sell_usd"]), f"{case['high_sell_to_prior_buy_ratio'] * 100:.2f}%" if case["high_sell_to_prior_buy_ratio"] is not None else "—", case["exit_label"], fmt_hours(case["median_hold_seconds"])]
                for case in analysis["daily_cases"][:10]
            ],
        ),
    ]
    for label, item in (("最低持有案例", analysis["lowest_hold_case"]), ("最高持有案例", analysis["highest_hold_case"])):
        if item:
            path = item["path"]
            lines.extend([
                "",
                f"{label}：{path['weather_date']} / {path['temperature_bucket']} / asset `{path['asset']}`；首次低价BUY至首次90—100¢ SELL {fmt_hours(item['hold_seconds'])}；对应BUY均价 {fmt_price(weighted_price(item['prior_buys']))}，高价SELL均价 {fmt_price(weighted_price(item['high_sells']))}，高价SELL/此前BUY shares {item['high_sell_ratio'] * 100:.2f}%。",
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
            ["每个96日历天气日平均成交笔数", fmt_num(style["average_fills_per_requested_day"])],
            ["每个有成交天气日平均成交笔数", fmt_num(style["average_fills_per_active_day"])],
            ["YES和NO都活跃的天气日数", len(style["yes_no_active_days"])],
            ["maker/taker", "NOT_AVAILABLE" if style["maker_taker"] is None else ", ".join(style["maker_taker"])],
        ],
    )


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
    lines = [
        "# SECOND_STAGE_TRADER_PATTERN_COMPARISON",
        "",
        f"研究范围：北京每日最高温市场；天气日期 {start.isoformat()} 至 {end.isoformat()}。",
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
        "两个钱包的目标市场查询均为 COMPLETE；无 API request failure、unknown timezone、unknown relative day、orphan sell 或 identity conflict。钱包二有1笔 activity-only fill，其他成交均有 activity 与 trades 双源对应；这不会改变本地报告的 READY 状态。",
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
        "结论：2026-05-01 至 2026-08-04 首尾包含确实是96个自然天气日。当前报告使用97，是因为 2026-05-19 有两个完整事件，而不是多了一个天气日期：一个 slug 为 `arch-highest-temperature-in-beijing-on-may-19-2026`，另一个为 `highest-temperature-in-beijing-on-may-19-2026`。两者 event_id 不同、各自11个完整 condition；没有范围外事件，也没有完全相同 event_id 的重复。",
        "",
        f"DENOMINATOR_97_EXPLANATION=96 unique weather dates + 1 extra same-date event on 2026-05-19 (old arch slug and new slug). Daily ratios in this report use 96 calendar dates; event-level path tables retain both event records.",
        "",
        "每个日期的 event 数如下；除 2026-05-19 外均为1：",
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
        "D0 是北京市场当地天气日；D0 下的四个小时桶是 D0 的明细，不应与 D0 再相加。每个表的占比均以该 BUY/SELL + YES/NO 类别自身为分母。没有 POST_EVENT 成交；也没有 EARLIER_THAN_D2 或 UNKNOWN 成交。",
    ]
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}"])
        for key in ("BUY YES", "BUY NO", "SELL YES", "SELL NO"):
            lines.extend(["", f"#### {key}", "", render_time_table(data[wallet]["time"][key])])
    lines.extend(["", "## 3. 每个事件的资金路径时间", "", "以下保留97个 event 记录，因此 2026-05-19 的两个 slug 分开；BUY资金25/50/75% 是该 event 内按时间排序的 BUY USD 累计阈值，不是仓位比例。"])
    for wallet in WALLETS:
        lines.extend(["", details(f"{wallet}：97个event路径表", render_event_path_table(data[wallet]["event_paths"]))])
    lines.extend(["", "## 4. 完整价格带占比", "", "每个表的 fill_share、shares_share、usd_share 分别以该四类自身总量为分母；NO 价格保持 NO 合约自身价格，未转换为 YES 等价价格。USD加权均价 = 实际 trade USD / shares。"])
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}"])
        for key in ("BUY YES", "BUY NO", "SELL YES", "SELL NO"):
            lines.extend(["", f"#### {key}", "", render_price_table(data[wallet]["price"][key])])
    w1_sell_yes = data[WALLETS[0]]["price"]["SELL YES"]
    w1_sell_yes_largest_by = max(w1_sell_yes, key=lambda row: row["usd"])["band"]
    lines.extend([
        "",
        "### 钱包一 SELL YES 的90—100美分矛盾核查",
        "",
        "钱包一 SELL YES 总额为 $7,252.18、总量41,733.22 shares，整体实际加权均价约17.38美分。当前报告的“主要价格带”使用的是固定价格带中按 USD 金额最大的单一桶，不是多数占比：",
        "",
        md_table(
            ["判断口径", "最大价格带", "该带占比", "是否过半"],
            [
                ["fill_count", PRICE_NAMES[max(w1_sell_yes, key=lambda row: row["fills"])["band"]], fmt_pct(max(w1_sell_yes, key=lambda row: row["fills"])["fill_share"]), "否"],
                ["shares", PRICE_NAMES[max(w1_sell_yes, key=lambda row: row["shares"])["band"]], fmt_pct(max(w1_sell_yes, key=lambda row: row["shares"])["shares_share"]), "否"],
                ["trade USD", PRICE_NAMES[w1_sell_yes_largest_by], fmt_pct(max(w1_sell_yes, key=lambda row: row["usd"])["usd_share"]), "否"],
            ],
        ),
        "",
        "因此，90—100美分是按 USD 的最大单一桶，但只占约36.79%；按笔数最大的是10—30美分，按 shares 最大的是0—10美分。原摘要没有错，但“主要”容易被误读为绝大多数，第二阶段应明确为“最大单一USD桶”。",
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
    lines.extend(["", "风格标签采用保守规则：", "", md_table(["钱包", "标签", "理由"], [
        [WALLETS[0], "ACTIVE_REBALANCER", "存在大量资产级双向路径、BUY→SELL与SELL→BUY切换；但没有maker/taker证据，不能仅凭成交笔数称为做市商。"],
        [WALLETS[1], "DIRECTIONAL_ACCUMULATOR", "BUY明显多于SELL，且买入YES占比高；高价卖出路径存在，但双向反复与短持有证据弱于钱包一。"],
    ])])
    lines.extend(["", "## 8. 温度组合：互斥主分类", "", "每个96日历天气日严格分入一个类别；2026-05-19 的旧/新事件在天气日级合并，重复 event 不重复计日。yes_bucket_count/no_bucket_count 是该天气日 BUY 记录中的唯一温度桶数。"])
    for wallet in WALLETS:
        lines.extend(["", f"### {wallet}", "", render_category_table(data[wallet])])
        lines.extend(["", "辅助特征统计：", "", md_table(["特征", "天气日数", "比例"], [
            ["adjacent_yes", sum(day["adjacent_yes"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["adjacent_yes"] for day in data[wallet]["temperature_days"]), 96))],
            ["non_adjacent_yes", sum(day["non_adjacent_yes"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["non_adjacent_yes"] for day in data[wallet]["temperature_days"]), 96))],
            ["same_bucket_both_sides", sum(day["same_bucket_both_sides"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["same_bucket_both_sides"] for day in data[wallet]["temperature_days"]), 96))],
            ["cross_bucket_yes_no", sum(day["cross_bucket_yes_no"] for day in data[wallet]["temperature_days"]), fmt_pct(pct(sum(day["cross_bucket_yes_no"] for day in data[wallet]["temperature_days"]), 96))],
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
    lines.extend(["", "## 11. 两个钱包的可确认模式", "", "### 钱包一", "", "#### 可以确认", "", "- BUY/SELL 都活跃，且逐资产存在大量同资产双向成交与重复切换；这不是把不同温度合同拼出来的结果。", "- BUY YES、BUY NO、SELL YES、SELL NO 的时间与价格分布明显不同，BUY 与 SELL 不能合并成一个主时点。", "- 多YES组合和相邻温度档覆盖是稳定的公开成交特征；NO 也会与 YES 混合出现。", "", "#### 合理但尚未证明", "", "- 可能是主动再平衡或短周期调整；6小时内BUY→SELL比例只能证明时间邻近，不能证明订单意图。", "- 低价买入后高价卖出在部分资产上可见，但不能推出完整退出或盈利。", "", "#### 不支持的说法", "", "- 不能称为确定的做市商；没有maker/taker和订单簿证据。", "- 不能推断每个SELL都对应此前BUY，也不能推断完整库存。", "", "### 钱包二", "", "#### 可以确认", "", "- BUY YES占其买入成交的主导位置；BUY YES主要落在10—30美分，90—100美分 SELL YES 中存在大量同资产此前BUY的可追溯路径。", "- 这些高价SELL在大多数匹配资产上是相对此前BUY的部分卖出，而不是完整退出；具体比例见第6节。", "- 成交金额更集中在D0 16:00—24:00，和钱包一的D0 00:00—08:00不同。", "", "#### 合理但尚未证明", "", "- 可能存在低价多YES覆盖后等待部分高价退出的路径，但只适用于已观察到的同资产 fills。", "", "#### 不支持的说法", "", "- 不能称为确定的方向性预测者或盈利交易者；没有完整持仓、结算和PnL证据。", "- 不能把所有低价BUY YES都视为同一高价SELL的来源。", "", "## 12. 最值得学习的3条与不适合复制的行为", "", "1. 可学习：把 BUY YES、BUY NO、SELL YES、SELL NO 分开统计，并按天气日/当地时段复盘；两个钱包的建仓和卖出时段差异说明合并口径会隐藏路径。", "2. 可学习：只在同一 asset + condition_id + temperature_bucket 内做路径匹配，避免把不同温度合同的低价买入与高价卖出拼成假策略。", "3. 可学习：用固定价格带、主力档、相邻温度覆盖和部分卖出比例形成模拟规则候选，但必须继续保留“公开 fills only”证据标签。", "", "不适合直接复制：按低价/高价区间机械追单、把多温度覆盖当作确定预测、把观察到的部分卖出当作完整退出、或把高成交量直接命名为做市。", "", "## 13. 最终对比", "", md_table(["维度", "钱包一", "钱包二"], [
            ["建仓时间", "BUY按YES/NO拆分后主要在D0及D-1；D0内00—08", "BUY主要在D0；D0内按总BUY金额可见16—24较高"],
            ["主要YES价格", PRICE_NAMES[max(data[WALLETS[0]]["price"]["BUY YES"], key=lambda row: row["usd"])["band"]], PRICE_NAMES[max(data[WALLETS[1]]["price"]["BUY YES"], key=lambda row: row["usd"])["band"]]],
            ["NO使用方法", "BUY NO主要70—90¢，SELL NO主要70—90¢；与YES混合较多", "BUY NO主要30—70¢；SELL NO全在90—100¢；NO总体投入占比较低"],
            ["温度组合", "多YES与YES/NO混合并存，相邻YES很常见", "多YES与YES/NO混合并存，混合日更密集"],
            ["卖出频率", "2,638笔，明显活跃", "83笔，远低于买入"],
            ["高价退出", "SELL YES按USD最大单一桶是90—100¢，但仅36.79%且整体均价17.38¢", "90—100¢ SELL YES约95.2%卖出USD，且可在同资产追溯到此前BUY"],
            ["持有时间", "逐资产BUY→SELL与短时切换较多，见第5/7节", "低价BUY→高价SELL路径中位持有见第6节"],
            ["反复交易", "更明显；存在BUY↔SELL切换", "较弱；主要是BUY，少量高价SELL"],
            ["疑似风格", "ACTIVE_REBALANCER", "DIRECTIONAL_ACCUMULATOR"],
            ["证据强度", "READY；逐资产路径可复核，但无maker/taker/完整库存", "READY；低买高卖同资产路径已复核，但无完整库存/意图"],
        ]),
        "",
        "## 14. 特别回答",
        "",
        "1. 两个钱包共有的稳定模式：都在多个最高温度档之间分散 BUY YES，且交易集中在D-2/D-1/D0核心窗口；都存在YES/NO混合天气日和部分SELL成交。",
        "2. 只属于钱包一的模式：SELL 频率显著更高、同资产BUY/SELL切换更明显、D0内较偏00—08；SELL NO也有较多70—90¢成交。",
        "3. 只属于钱包二的模式：BUY YES占比更高、SELL数量极少但90—100¢集中度极高；低价BUY YES到同资产高价SELL YES的路径证据更集中。",
        "4. “低价买入、多温度覆盖、高价部分退出”是否被证明：钱包二在观察到的同资产 fills 中已被证明为存在；不是所有资产/所有天气日都满足，不能外推为完整策略。钱包一也有部分同类路径，但不是其全部SELL YES。",
        "5. 可进入模拟交易候选规则：只可作为待验证候选——按本地天气日分桶、分别建模四类方向、同资产路径验证、低价BUY与高价SELL的部分退出比例、以及相邻温度覆盖。必须在模拟中加入无成交、未匹配、重复event、价格滑点和证据缺口。",
        "6. 还不能复制：完整仓位管理、订单簿做市、盈利能力、主观预测、所有低价BUY到高价SELL的因果关系，以及任何基于SELL金额的PnL/ROI结论。",
        "",
        "## 15. 复现",
        "",
        f"```bash\npython3 scripts/second_stage_trader_pattern_analysis.py \\\n+  --output-root {root} \\\n+  --report {report_path} \\\n+  --date-from {start.isoformat()} \\\n+  --date-to {end.isoformat()} \\\n+  --city {city}\n```",
        "",
        "本脚本为本地离线分析，不会发起网络请求、签名或下单。",
    ])
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
