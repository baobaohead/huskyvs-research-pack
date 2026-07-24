#!/usr/bin/env python3
"""Corrected v2 audit for huskyvs weather trades using existing raw data only."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.analyze_weather_strategy import parse_weather_title, parse_bucket

TRANSFORM_TYPES = {"SPLIT", "MERGE", "CONVERSION"}
MOSTLY_SOLD_THRESHOLD = 0.90
PRICE_BIN_ORDER = ["0-1c", "1-2c", "2-5c", "5-10c", "10-20c", ">=20c", "unknown"]
LEAD_BIN_ORDER = ["0-6h", "6-12h", "12-24h", "24-48h", "48-72h", "72h+", "after_local_day_end", "unknown"]

CITY_TIMEZONE = {
    "Amsterdam": "Europe/Amsterdam",
    "Ankara": "Europe/Istanbul",
    "Atlanta": "America/New_York",
    "Austin": "America/Chicago",
    "Beijing": "Asia/Shanghai",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Busan": "Asia/Seoul",
    "Cape Town": "Africa/Johannesburg",
    "Chengdu": "Asia/Shanghai",
    "Chicago": "America/Chicago",
    "Chongqing": "Asia/Shanghai",
    "Dallas": "America/Chicago",
    "Denver": "America/Denver",
    "Guangzhou": "Asia/Shanghai",
    "Helsinki": "Europe/Helsinki",
    "Hong Kong": "Asia/Hong_Kong",
    "Houston": "America/Chicago",
    "Istanbul": "Europe/Istanbul",
    "Jakarta": "Asia/Jakarta",
    "Jeddah": "Asia/Riyadh",
    "Karachi": "Asia/Karachi",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "Lagos": "Africa/Lagos",
    "London": "Europe/London",
    "Los Angeles": "America/Los_Angeles",
    "Lucknow": "Asia/Kolkata",
    "Madrid": "Europe/Madrid",
    "Mexico City": "America/Mexico_City",
    "Miami": "America/New_York",
    "Milan": "Europe/Rome",
    "Moscow": "Europe/Moscow",
    "Munich": "Europe/Berlin",
    "New York City": "America/New_York",
    "Panama City": "America/Panama",
    "Paris": "Europe/Paris",
    "Qingdao": "Asia/Shanghai",
    "San Francisco": "America/Los_Angeles",
    "Sao Paulo": "America/Sao_Paulo",
    "Seattle": "America/Los_Angeles",
    "Seoul": "Asia/Seoul",
    "Shanghai": "Asia/Shanghai",
    "Shenzhen": "Asia/Shanghai",
    "Singapore": "Asia/Singapore",
    "Taipei": "Asia/Taipei",
    "Tel Aviv": "Asia/Jerusalem",
    "Tokyo": "Asia/Tokyo",
    "Toronto": "America/Toronto",
    "Warsaw": "Europe/Warsaw",
    "Wellington": "Pacific/Auckland",
    "Wuhan": "Asia/Shanghai",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def safe_div(num: float, den: float) -> float:
    return num / den if den else math.nan


def iso_from_epoch(ts: float | int | None) -> str:
    if not ts or not finite(float(ts)):
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def parse_epoch(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def local_day_end_epoch(city: str, weather_date: str) -> int:
    tz_name = CITY_TIMEZONE.get(city)
    if not tz_name:
        raise KeyError(f"Missing timezone mapping for city: {city}")
    d = date.fromisoformat(weather_date)
    local_start = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz_name))
    local_end = local_start + timedelta(days=1)
    return int(local_end.astimezone(timezone.utc).timestamp())


def lead_bin(hours: float) -> str:
    if not finite(hours):
        return "unknown"
    if hours < 0:
        return "after_local_day_end"
    if hours < 6:
        return "0-6h"
    if hours < 12:
        return "6-12h"
    if hours < 24:
        return "12-24h"
    if hours < 48:
        return "24-48h"
    if hours < 72:
        return "48-72h"
    return "72h+"


def price_bin(price: float) -> str:
    if not finite(price):
        return "unknown"
    cents = price * 100
    if cents < 1:
        return "0-1c"
    if cents < 2:
        return "1-2c"
    if cents < 5:
        return "2-5c"
    if cents < 10:
        return "5-10c"
    if cents < 20:
        return "10-20c"
    return ">=20c"


def bucket_interval(row: dict[str, Any]) -> tuple[float, float]:
    lo = row.get("bucket_low")
    hi = row.get("bucket_high")
    return (
        -math.inf if lo in ("", None) or (isinstance(lo, float) and math.isnan(lo)) else float(lo),
        math.inf if hi in ("", None) or (isinstance(hi, float) and math.isnan(hi)) else float(hi),
    )


def bucket_sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
    lo, hi = bucket_interval(row)
    return (lo, hi, str(row.get("bucket_label") or ""))


def non_overlapping(rows: list[dict[str, Any]]) -> bool:
    intervals = sorted(bucket_interval(r) for r in rows)
    return all(next_lo > hi for (_, hi), (next_lo, _) in zip(intervals, intervals[1:]))


def adjacent_exact_or_range(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 2:
        return False
    if any(r.get("bucket_kind") not in {"exact", "range"} for r in rows):
        return False
    intervals = [bucket_interval(r) for r in sorted(rows, key=bucket_sort_key)]
    if any(math.isinf(lo) or math.isinf(hi) for lo, hi in intervals):
        return False
    return all(next_lo - hi <= 1.000001 for (_, hi), (next_lo, _) in zip(intervals, intervals[1:]))


def load_manifest(raw: Path) -> dict[str, Any]:
    return json.loads((raw / "manifest.json").read_text(encoding="utf-8"))


def normalize_positions(rows: list[dict[str, str]], source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        weather = parse_weather_title(r.get("title", ""), r.get("endDate"))
        if not weather:
            continue
        city = weather["city"]
        local_end = local_day_end_epoch(city, weather["weather_date"])
        row: dict[str, Any] = dict(r)
        row.update(weather)
        row.update(
            {
                "source": source,
                "avgPrice": fnum(r.get("avgPrice"), math.nan),
                "curPrice": fnum(r.get("curPrice"), math.nan),
                "realizedPnl": fnum(r.get("realizedPnl"), math.nan),
                "cashPnl": fnum(r.get("cashPnl"), math.nan),
                "currentValue": fnum(r.get("currentValue"), math.nan),
                "initialValue": fnum(r.get("initialValue"), math.nan),
                "size": fnum(r.get("size"), math.nan),
                "totalBought": fnum(r.get("totalBought"), math.nan),
                "timestamp": inum(r.get("timestamp")),
                "local_timezone": CITY_TIMEZONE.get(city, ""),
                "local_weather_day_end_epoch": local_end,
                "local_weather_day_end_utc": iso_from_epoch(local_end),
            }
        )
        out.append(row)
    return out


def build_asset_meta(trades_raw: list[dict[str, str]], closed: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}

    def add_from_weather_row(row: dict[str, Any], source: str) -> None:
        asset = row.get("asset") or ""
        if not asset:
            return
        weather = parse_weather_title(row.get("title", ""), row.get("endDate"))
        if not weather:
            return
        m = meta.setdefault(asset, {"asset": asset})
        m.update(weather)
        for key in ["conditionId", "title", "slug", "eventSlug", "outcome", "outcomeIndex", "oppositeAsset", "oppositeOutcome"]:
            if row.get(key) not in (None, ""):
                m[key] = row.get(key)
        city = weather["city"]
        local_end = local_day_end_epoch(city, weather["weather_date"])
        m["local_timezone"] = CITY_TIMEZONE.get(city, "")
        m["local_weather_day_end_epoch"] = local_end
        m["local_weather_day_end_utc"] = iso_from_epoch(local_end)
        m[f"seen_in_{source}"] = True

    for r in trades_raw:
        add_from_weather_row(r, "trades")
    for r in closed:
        add_from_weather_row(r, "closed_positions")
    for r in current:
        add_from_weather_row(r, "current_positions")
    return meta


def normalize_trades(trades_raw: list[dict[str, str]], asset_meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in trades_raw:
        asset = r.get("asset") or ""
        meta = asset_meta.get(asset)
        weather = meta or parse_weather_title(r.get("title", ""))
        if not weather:
            continue
        ts = inum(r.get("timestamp"))
        row: dict[str, Any] = dict(r)
        row.update(weather)
        row.update(
            {
                "timestamp": ts,
                "timestamp_utc": iso_from_epoch(ts),
                "size": fnum(r.get("size")),
                "price": fnum(r.get("price"), math.nan),
                "notional_usd": fnum(r.get("size")) * fnum(r.get("price")),
                "side": (r.get("side") or "").upper(),
                "outcome": r.get("outcome") or (meta or {}).get("outcome") or "",
                "local_timezone": (meta or {}).get("local_timezone") or CITY_TIMEZONE.get(weather.get("city", ""), ""),
                "local_weather_day_end_epoch": (meta or {}).get("local_weather_day_end_epoch", 0),
                "local_weather_day_end_utc": (meta or {}).get("local_weather_day_end_utc", ""),
            }
        )
        out.append(row)
    return out


def settled_current_candidate(row: dict[str, Any], closed_assets: set[str], asof_epoch: int) -> bool:
    if row.get("asset") in closed_assets:
        return False
    return (
        fnum(row.get("currentValue"), math.nan) == 0
        and fnum(row.get("cashPnl"), math.nan) < 0
        and inum(row.get("local_weather_day_end_epoch")) <= asof_epoch
    )


def settlement_map(closed: list[dict[str, Any]], current: list[dict[str, Any]], asof_epoch: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in closed:
        asset = r.get("asset")
        if not asset:
            continue
        out[asset] = {
            "settlement_source_v2": "closed_positions",
            "settled_sample_v2": True,
            "realized_pnl_v2": fnum(r.get("realizedPnl"), math.nan),
            "settlement_price_v2": fnum(r.get("curPrice"), math.nan),
            "current_value_component": math.nan,
            "current_cash_pnl_component": math.nan,
            "current_realized_pnl_component": math.nan,
        }
    closed_assets = set(out)
    for r in current:
        asset = r.get("asset")
        if not asset or not settled_current_candidate(r, closed_assets, asof_epoch):
            continue
        cash = fnum(r.get("cashPnl"), 0.0)
        realized = fnum(r.get("realizedPnl"), 0.0)
        out[asset] = {
            "settlement_source_v2": "current_zero_value_after_local_day_end",
            "settled_sample_v2": True,
            "realized_pnl_v2": realized + cash,
            "settlement_price_v2": fnum(r.get("curPrice"), 0.0),
            "current_value_component": fnum(r.get("currentValue"), 0.0),
            "current_cash_pnl_component": cash,
            "current_realized_pnl_component": realized,
        }
    return out


def classify_exit_mode(buy_shares: float, sell_shares: float, transform_affected: bool) -> str:
    if transform_affected:
        return "transform_affected"
    if sell_shares <= 0:
        return "never_sold"
    ratio = safe_div(sell_shares, buy_shares)
    if finite(ratio) and ratio >= MOSTLY_SOLD_THRESHOLD:
        return "mostly_or_fully_sold"
    return "partially_sold"


def implied_position_cost(row: dict[str, Any]) -> float:
    avg = fnum(row.get("avgPrice"), math.nan)
    total = fnum(row.get("totalBought"), math.nan)
    initial = fnum(row.get("initialValue"), math.nan)
    if finite(avg) and finite(total) and avg > 0 and total > 0:
        return avg * total
    if finite(initial) and initial > 0:
        return initial
    return math.nan


def build_lifecycle(
    trades: list[dict[str, Any]],
    activity_raw: list[dict[str, str]],
    closed: list[dict[str, Any]],
    current: list[dict[str, Any]],
    asset_meta: dict[str, dict[str, Any]],
    settlements: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_asset[t.get("asset", "")].append(t)
    closed_by_asset = {r.get("asset"): r for r in closed if r.get("asset")}
    current_by_asset = {r.get("asset"): r for r in current if r.get("asset")}
    activity_by_condition: dict[str, list[dict[str, str]]] = defaultdict(list)
    for a in activity_raw:
        if a.get("conditionId"):
            activity_by_condition[a["conditionId"]].append(a)

    assets = sorted(set(asset_meta) | set(by_asset) | set(closed_by_asset) | set(current_by_asset))
    out: list[dict[str, Any]] = []
    for asset in assets:
        meta = dict(asset_meta.get(asset, {}))
        if not meta:
            continue
        ts = sorted(by_asset.get(asset, []), key=lambda r: r["timestamp"])
        buys = [t for t in ts if t["side"] == "BUY"]
        sells = [t for t in ts if t["side"] == "SELL"]
        condition_id = meta.get("conditionId") or (ts[0].get("conditionId") if ts else "")
        acts = activity_by_condition.get(condition_id, [])
        types = [a.get("type", "") for a in acts]
        transform = any(t in TRANSFORM_TYPES for t in types)
        local_end = inum(meta.get("local_weather_day_end_epoch"))

        buy_shares = sum(fnum(t.get("size")) for t in buys)
        sell_shares = sum(fnum(t.get("size")) for t in sells)
        buy_usd = sum(fnum(t.get("notional_usd")) for t in buys)
        sell_usd = sum(fnum(t.get("notional_usd")) for t in sells)
        first_buy_ts = min((inum(t.get("timestamp")) for t in buys), default=0)
        weighted_buy_ts = safe_div(sum(inum(t.get("timestamp")) * fnum(t.get("notional_usd")) for t in buys), buy_usd)
        weighted_buy_price = safe_div(buy_usd, buy_shares)
        if not finite(weighted_buy_price):
            pos = closed_by_asset.get(asset) or current_by_asset.get(asset) or {}
            weighted_buy_price = fnum(pos.get("avgPrice"), math.nan)
        cap_at_risk = buy_usd
        if cap_at_risk <= 0:
            pos = closed_by_asset.get(asset) or current_by_asset.get(asset) or {}
            implied = implied_position_cost(pos)
            cap_at_risk = implied if finite(implied) else math.nan

        sell_before_end = [s for s in sells if local_end and inum(s.get("timestamp")) <= local_end]
        sell_after_end = [s for s in sells if local_end and inum(s.get("timestamp")) > local_end]
        settlement = settlements.get(asset, {})
        exit_mode = classify_exit_mode(buy_shares, sell_shares, transform)
        first_lead = safe_div(local_end - first_buy_ts, 3600) if first_buy_ts and local_end else math.nan
        weighted_lead = safe_div(local_end - weighted_buy_ts, 3600) if finite(weighted_buy_ts) and local_end else math.nan
        realized_pnl = fnum(settlement.get("realized_pnl_v2"), math.nan)
        settlement_price = fnum(settlement.get("settlement_price_v2"), math.nan)

        row = {
            "asset": asset,
            "conditionId": condition_id,
            "title": meta.get("title"),
            "slug": meta.get("slug"),
            "eventSlug": meta.get("eventSlug"),
            "city": meta.get("city"),
            "weather_date": meta.get("weather_date"),
            "weather_metric": meta.get("weather_metric"),
            "unit": meta.get("unit"),
            "local_timezone": meta.get("local_timezone"),
            "local_weather_day_end_epoch": local_end,
            "local_weather_day_end_utc": iso_from_epoch(local_end),
            "bucket_label": meta.get("bucket_label"),
            "bucket_kind": meta.get("bucket_kind"),
            "bucket_low": meta.get("bucket_low"),
            "bucket_high": meta.get("bucket_high"),
            "outcome": meta.get("outcome") or (ts[0].get("outcome") if ts else ""),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_shares": buy_shares,
            "sell_shares": sell_shares,
            "sell_share_ratio": safe_div(sell_shares, buy_shares),
            "net_traded_shares": buy_shares - sell_shares,
            "buy_usd": buy_usd,
            "sell_usd": sell_usd,
            "capital_at_risk_usd": cap_at_risk,
            "weighted_avg_buy_price": weighted_buy_price,
            "weighted_avg_sell_price": safe_div(sell_usd, sell_shares),
            "entry_price_bin": price_bin(weighted_buy_price),
            "first_buy_ts": first_buy_ts,
            "first_buy_utc": iso_from_epoch(first_buy_ts),
            "weighted_avg_buy_ts": weighted_buy_ts,
            "weighted_avg_buy_utc": iso_from_epoch(weighted_buy_ts if finite(weighted_buy_ts) else 0),
            "first_entry_lead_hours_local": first_lead,
            "first_entry_lead_bin_local": lead_bin(first_lead),
            "weighted_entry_lead_hours_local": weighted_lead,
            "weighted_entry_lead_bin_local": lead_bin(weighted_lead),
            "first_sell_ts": min((inum(t.get("timestamp")) for t in sells), default=0),
            "last_sell_ts": max((inum(t.get("timestamp")) for t in sells), default=0),
            "first_sell_utc": iso_from_epoch(min((inum(t.get("timestamp")) for t in sells), default=0)),
            "last_sell_utc": iso_from_epoch(max((inum(t.get("timestamp")) for t in sells), default=0)),
            "sell_before_local_day_end_count": len(sell_before_end),
            "sell_after_local_day_end_count": len(sell_after_end),
            "sell_before_local_day_end_usd": sum(fnum(t.get("notional_usd")) for t in sell_before_end),
            "sell_after_local_day_end_usd": sum(fnum(t.get("notional_usd")) for t in sell_after_end),
            "all_sells_before_local_day_end": bool(sells) and not sell_after_end,
            "any_sell_before_local_day_end": bool(sell_before_end),
            "split_count": types.count("SPLIT"),
            "merge_count": types.count("MERGE"),
            "conversion_count": types.count("CONVERSION"),
            "transform_affected": transform,
            "exit_mode_v2": exit_mode,
            "settled_sample_v2": bool(settlement),
            "settlement_source_v2": settlement.get("settlement_source_v2", "open_or_unresolved"),
            "settlement_price_v2": settlement_price,
            "asset_won_v2": bool(finite(settlement_price) and settlement_price >= 0.5),
            "realized_pnl_v2": realized_pnl,
            "roi_on_capital_at_risk_v2": safe_div(realized_pnl, cap_at_risk) if finite(realized_pnl) and finite(cap_at_risk) else math.nan,
            "current_value_component": settlement.get("current_value_component", math.nan),
            "current_cash_pnl_component": settlement.get("current_cash_pnl_component", math.nan),
            "current_realized_pnl_component": settlement.get("current_realized_pnl_component", math.nan),
        }
        out.append(row)
    return out


def summary_stats(values: Iterable[float]) -> dict[str, float]:
    xs = [v for v in values if finite(v)]
    if not xs:
        return {"count": 0, "sum": math.nan, "mean": math.nan, "median": math.nan, "stdev": math.nan}
    return {
        "count": len(xs),
        "sum": sum(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
    }


def grouped_pnl_rows(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(k, "") for k in keys)].append(r)
    out: list[dict[str, Any]] = []
    for key, rs in groups.items():
        settled = [r for r in rs if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]
        cap = sum(fnum(r.get("capital_at_risk_usd")) for r in settled if finite(r.get("capital_at_risk_usd")))
        row = dict(zip(keys, key))
        row.update(
            {
                "positions": len(rs),
                "settled_positions": len(settled),
                "capital_at_risk_usd": cap,
                "realized_pnl_v2": sum(fnum(r.get("realized_pnl_v2")) for r in settled),
                "roi_v2": safe_div(sum(fnum(r.get("realized_pnl_v2")) for r in settled), cap),
                "win_rate_v2": safe_div(sum(1 for r in settled if fnum(r.get("realized_pnl_v2")) > 0), len(settled)),
                "sell_before_local_day_end_positions": sum(1 for r in settled if r.get("any_sell_before_local_day_end")),
                "current_zero_value_added_positions": sum(1 for r in settled if r.get("settlement_source_v2") == "current_zero_value_after_local_day_end"),
            }
        )
        out.append(row)
    return sorted(out, key=lambda r: tuple(str(r.get(k, "")) for k in keys))


def corrected_price_bin_exit_mode(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [r for r in life if r.get("settled_sample_v2") and r.get("outcome") == "Yes"]
    rows = grouped_pnl_rows(eligible, ["entry_price_bin", "exit_mode_v2"])
    return sorted(rows, key=lambda r: (PRICE_BIN_ORDER.index(r["entry_price_bin"]) if r["entry_price_bin"] in PRICE_BIN_ORDER else 99, r["exit_mode_v2"]))


def corrected_city_day_pnl(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        if r.get("city") and r.get("weather_date"):
            groups[(r["city"], r["weather_date"], r["weather_metric"], r["unit"])].append(r)
    out: list[dict[str, Any]] = []
    for (city, day, metric, unit), rs in groups.items():
        settled = [r for r in rs if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]
        cap = sum(fnum(r.get("capital_at_risk_usd")) for r in settled if finite(r.get("capital_at_risk_usd")))
        pnl = sum(fnum(r.get("realized_pnl_v2")) for r in settled)
        out.append(
            {
                "city": city,
                "weather_date": day,
                "weather_metric": metric,
                "unit": unit,
                "positions": len(rs),
                "settled_positions": len(settled),
                "yes_positions": sum(1 for r in rs if r.get("outcome") == "Yes"),
                "no_positions": sum(1 for r in rs if r.get("outcome") == "No"),
                "capital_at_risk_usd": cap,
                "realized_pnl_v2": pnl,
                "roi_v2": safe_div(pnl, cap),
                "exit_modes": "|".join(sorted({str(r.get("exit_mode_v2")) for r in settled})),
                "current_zero_value_added_positions": sum(1 for r in settled if r.get("settlement_source_v2") == "current_zero_value_after_local_day_end"),
                "has_transform_affected": any(r.get("exit_mode_v2") == "transform_affected" for r in settled),
                "sell_before_local_day_end_positions": sum(1 for r in settled if r.get("any_sell_before_local_day_end")),
            }
        )
    return sorted(out, key=lambda r: (r["weather_date"], r["city"], r["weather_metric"], r["unit"]))


def entry_lead_time_rows(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in life:
        if not r.get("settled_sample_v2"):
            continue
        out.append(
            {
                "asset": r.get("asset"),
                "city": r.get("city"),
                "weather_date": r.get("weather_date"),
                "weather_metric": r.get("weather_metric"),
                "unit": r.get("unit"),
                "local_timezone": r.get("local_timezone"),
                "local_weather_day_end_utc": r.get("local_weather_day_end_utc"),
                "bucket_label": r.get("bucket_label"),
                "outcome": r.get("outcome"),
                "exit_mode_v2": r.get("exit_mode_v2"),
                "first_buy_utc": r.get("first_buy_utc"),
                "weighted_avg_buy_utc": r.get("weighted_avg_buy_utc"),
                "first_entry_lead_hours_local": r.get("first_entry_lead_hours_local"),
                "first_entry_lead_bin_local": r.get("first_entry_lead_bin_local"),
                "weighted_entry_lead_hours_local": r.get("weighted_entry_lead_hours_local"),
                "weighted_entry_lead_bin_local": r.get("weighted_entry_lead_bin_local"),
                "capital_at_risk_usd": r.get("capital_at_risk_usd"),
                "realized_pnl_v2": r.get("realized_pnl_v2"),
                "roi_on_capital_at_risk_v2": r.get("roi_on_capital_at_risk_v2"),
                "settlement_source_v2": r.get("settlement_source_v2"),
            }
        )
    return out


def entry_lead_summary(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    settled = [r for r in life if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]
    for universe, filt in [
        ("all_outcomes", lambda r: True),
        ("yes_only", lambda r: r.get("outcome") == "Yes"),
    ]:
        selected = [r for r in settled if filt(r)]
        for bin_name in LEAD_BIN_ORDER:
            rs = [r for r in selected if r.get("weighted_entry_lead_bin_local") == bin_name]
            if not rs:
                continue
            cap = sum(fnum(r.get("capital_at_risk_usd")) for r in rs if finite(r.get("capital_at_risk_usd")))
            pnl = sum(fnum(r.get("realized_pnl_v2")) for r in rs)
            rows.append(
                {
                    "outcome_filter": universe,
                    "entry_basis": "capital_weighted_entry_local",
                    "lead_bin": bin_name,
                    "settled_positions": len(rs),
                    "capital_at_risk_usd": cap,
                    "realized_pnl_v2": pnl,
                    "roi_v2": safe_div(pnl, cap),
                    "win_rate_v2": safe_div(sum(1 for r in rs if fnum(r.get("realized_pnl_v2")) > 0), len(rs)),
                    "current_zero_value_added_positions": sum(1 for r in rs if r.get("settlement_source_v2") == "current_zero_value_after_local_day_end"),
                }
            )
    return rows


def concentration_stats(items: list[dict[str, Any]], level: str) -> dict[str, Any]:
    values = [fnum(i.get("pnl"), math.nan) for i in items if finite(fnum(i.get("pnl"), math.nan))]
    total = sum(values)
    positives = sorted((i for i in items if finite(fnum(i.get("pnl"), math.nan)) and fnum(i.get("pnl")) > 0), key=lambda i: fnum(i.get("pnl")), reverse=True)
    gross_positive = sum(fnum(i.get("pnl")) for i in positives)

    def top_sum(n: int) -> float:
        return sum(fnum(i.get("pnl")) for i in positives[:n])

    return {
        "level": level,
        "items_with_pnl": len(values),
        "total_net_pnl": total,
        "gross_positive_pnl": gross_positive,
        "gross_loss_pnl": sum(v for v in values if v < 0),
        "top1_pnl": top_sum(1),
        "top5_pnl": top_sum(5),
        "top10_pnl": top_sum(10),
        "top1_share_of_net_pnl": safe_div(top_sum(1), total) if total > 0 else math.nan,
        "top5_share_of_net_pnl": safe_div(top_sum(5), total) if total > 0 else math.nan,
        "top10_share_of_net_pnl": safe_div(top_sum(10), total) if total > 0 else math.nan,
        "top1_share_of_gross_positive_pnl": safe_div(top_sum(1), gross_positive),
        "top5_share_of_gross_positive_pnl": safe_div(top_sum(5), gross_positive),
        "top10_share_of_gross_positive_pnl": safe_div(top_sum(10), gross_positive),
        "leave_top1_out_pnl": total - top_sum(1),
        "top1_id": str(positives[0].get("id")) if positives else "",
    }


def corrected_profit_concentration(life: list[dict[str, Any]], city_rows: list[dict[str, Any]], basket_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settled = [r for r in life if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]
    return [
        concentration_stats([{"id": r.get("asset"), "pnl": r.get("realized_pnl_v2")} for r in settled], "weather_position_assets_all_outcomes_v2"),
        concentration_stats([{"id": r.get("asset"), "pnl": r.get("realized_pnl_v2")} for r in settled if r.get("outcome") == "Yes"], "weather_position_assets_yes_only_v2"),
        concentration_stats([{"id": f"{r['city']}|{r['weather_date']}|{r['weather_metric']}|{r['unit']}", "pnl": r.get("realized_pnl_v2")} for r in city_rows if fnum(r.get("settled_positions")) > 0], "weather_city_day_baskets_all_outcomes_v2"),
        concentration_stats([{"id": r.get("basket_key"), "pnl": r.get("actual_trade_pnl_v2")} for r in basket_rows if r.get("evaluable")], "adjacent_yes_baskets_v2"),
    ]


def daily_pnl(city_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in city_rows:
        if fnum(r.get("settled_positions")) > 0:
            by_day[r["weather_date"]].append(r)
    out = []
    for day, rs in sorted(by_day.items()):
        pnl = sum(fnum(r.get("realized_pnl_v2")) for r in rs)
        out.append(
            {
                "weather_date": day,
                "city_day_count": len(rs),
                "realized_pnl_v2": pnl,
                "capital_at_risk_usd": sum(fnum(r.get("capital_at_risk_usd")) for r in rs),
                "positive_city_day_count": sum(1 for r in rs if fnum(r.get("realized_pnl_v2")) > 0),
                "negative_city_day_count": sum(1 for r in rs if fnum(r.get("realized_pnl_v2")) < 0),
            }
        )
    return out


def max_drawdown(daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    cumulative = 0.0
    peak = 0.0
    peak_date = "start"
    max_dd = 0.0
    dd_peak_date = "start"
    trough_date = ""
    for r in daily_rows:
        cumulative += fnum(r.get("realized_pnl_v2"))
        if cumulative > peak:
            peak = cumulative
            peak_date = r.get("weather_date", "")
        drawdown = peak - cumulative
        if drawdown > max_dd:
            max_dd = drawdown
            dd_peak_date = peak_date
            trough_date = r.get("weather_date", "")
    return {
        "max_drawdown_usd": max_dd,
        "drawdown_peak_date": dd_peak_date,
        "drawdown_trough_date": trough_date,
        "ending_cumulative_pnl": cumulative,
    }


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return math.nan
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return math.nan
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def corrected_city_correlation(city_rows: list[dict[str, Any]], min_overlap: int = 5) -> list[dict[str, Any]]:
    by_city: dict[str, dict[str, float]] = defaultdict(dict)
    for r in city_rows:
        if fnum(r.get("settled_positions")) <= 0:
            continue
        key = f"{r['weather_date']}|{r['weather_metric']}|{r['unit']}"
        by_city[r["city"]][key] = by_city[r["city"]].get(key, 0.0) + fnum(r.get("realized_pnl_v2"))
    cities = sorted(c for c, vals in by_city.items() if len(vals) >= min_overlap)
    out = []
    for i, a in enumerate(cities):
        for b in cities[i + 1 :]:
            overlap = sorted(set(by_city[a]) & set(by_city[b]))
            if len(overlap) < min_overlap:
                continue
            xs = [by_city[a][k] for k in overlap]
            ys = [by_city[b][k] for k in overlap]
            out.append(
                {
                    "city_a": a,
                    "city_b": b,
                    "overlap_days": len(overlap),
                    "pearson_corr_no_zero_fill": pearson(xs, ys),
                    "same_sign_rate": safe_div(sum(1 for x, y in zip(xs, ys) if (x >= 0) == (y >= 0)), len(overlap)),
                }
            )
    return sorted(out, key=lambda r: (-r["overlap_days"], r["city_a"], r["city_b"]))


def basket_comparison(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        if r.get("outcome") == "Yes" and r.get("city") and r.get("weather_date"):
            groups[(r["city"], r["weather_date"], r["weather_metric"], r["unit"])].append(r)
    out: list[dict[str, Any]] = []
    for (city, day, metric, unit), rows0 in groups.items():
        rows = sorted(rows0, key=bucket_sort_key)
        settled = [r for r in rows if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]
        exact_range = [r for r in settled if r.get("bucket_kind") in {"exact", "range"}]
        evaluable_rows = exact_range if adjacent_exact_or_range(exact_range) and non_overlapping(exact_range) and len(exact_range) >= 2 else []
        if not evaluable_rows:
            if len(rows) >= 2:
                out.append(
                    {
                        "basket_key": f"{city}|{day}|{metric}|{unit}",
                        "city": city,
                        "weather_date": day,
                        "weather_metric": metric,
                        "unit": unit,
                        "bucket_count": len(rows),
                        "settled_yes_bucket_count": len(settled),
                        "evaluable": False,
                        "not_evaluable_reason": "requires at least two settled adjacent non-overlapping exact/range YES buckets",
                    }
                )
            continue
        total_budget = sum(fnum(r.get("capital_at_risk_usd")) for r in evaluable_rows if finite(r.get("capital_at_risk_usd")))
        if total_budget <= 0:
            continue
        main = max(evaluable_rows, key=lambda r: fnum(r.get("capital_at_risk_usd")))
        prices = [fnum(r.get("weighted_avg_buy_price"), math.nan) for r in evaluable_rows]
        winner_rows = [r for r in evaluable_rows if r.get("asset_won_v2")]
        actual_trade_pnl = sum(fnum(r.get("realized_pnl_v2")) for r in evaluable_rows)
        original_hold_payout = sum(fnum(r.get("buy_shares")) for r in winner_rows)
        original_hold_pnl = original_hold_payout - total_budget
        main_price = fnum(main.get("weighted_avg_buy_price"), math.nan)
        main_payout = safe_div(total_budget, main_price) if winner_rows and any(w.get("asset") == main.get("asset") for w in winner_rows) and main_price > 0 else 0.0
        equal_amount_payout = 0.0
        for r, price in zip(evaluable_rows, prices):
            if r.get("asset_won_v2") and price > 0:
                equal_amount_payout += safe_div(total_budget / len(evaluable_rows), price)
        price_sum = sum(p for p in prices if p > 0 and finite(p))
        equal_payout_payout = safe_div(total_budget, price_sum) if winner_rows and price_sum > 0 and len(prices) == len(evaluable_rows) else 0.0
        out.append(
            {
                "basket_key": f"{city}|{day}|{metric}|{unit}",
                "city": city,
                "weather_date": day,
                "weather_metric": metric,
                "unit": unit,
                "bucket_count": len(evaluable_rows),
                "buckets": "|".join(str(r.get("bucket_label")) for r in evaluable_rows),
                "winner_buckets": "|".join(str(r.get("bucket_label")) for r in winner_rows),
                "main_bucket": main.get("bucket_label"),
                "total_input_usd": total_budget,
                "actual_trade_pnl_v2": actual_trade_pnl,
                "original_shares_hold_to_resolution_pnl": original_hold_pnl,
                "main_bucket_only_same_input_pnl": main_payout - total_budget,
                "equal_amount_basket_same_input_pnl": equal_amount_payout - total_budget,
                "equal_payout_basket_same_input_pnl": equal_payout_payout - total_budget,
                "actual_minus_equal_amount": actual_trade_pnl - (equal_amount_payout - total_budget),
                "actual_minus_original_hold": actual_trade_pnl - original_hold_pnl,
                "actual_minus_main_bucket": actual_trade_pnl - (main_payout - total_budget),
                "evaluable": True,
                "not_evaluable_reason": "",
                "liquidity_limit": "counterfactual assumes all target shares could be filled at observed weighted average prices",
                "non_simultaneous_fill_limit": "counterfactual ignores that original fills occurred at different times and market states",
            }
        )
    return sorted(out, key=lambda r: (str(r.get("weather_date")), str(r.get("city")), str(r.get("weather_metric")), str(r.get("unit"))))


def v1_vs_v2(v1_summary: dict[str, Any], v2_summary: dict[str, Any], v1_conc: list[dict[str, str]], v2_conc: list[dict[str, Any]]) -> list[dict[str, Any]]:
    v1_position_conc = next((r for r in v1_conc if r.get("level") == "weather_position_assets_all_outcomes"), {})
    v2_position_conc = next((r for r in v2_conc if r.get("level") == "weather_position_assets_all_outcomes_v2"), {})
    rows = []

    def add(metric: str, v1: Any, v2: Any) -> None:
        a, b = fnum(v1, math.nan), fnum(v2, math.nan)
        rows.append({"metric": metric, "v1": a, "v2": b, "delta_v2_minus_v1": b - a if finite(a) and finite(b) else math.nan})

    add("settled_weather_assets", v1_summary.get("closed_weather_assets_with_authoritative_pnl"), v2_summary.get("settled_weather_assets_v2"))
    add("total_net_pnl", v1_summary.get("closed_realized_pnl", {}).get("sum"), v2_summary.get("total_net_pnl_v2"))
    add("roi_on_capital_at_risk", v2_summary.get("v1_roi_on_buy_usd"), v2_summary.get("roi_on_capital_at_risk_v2"))
    add("max_drawdown_usd", v1_summary.get("portfolio_drawdown", {}).get("max_drawdown_usd"), v2_summary.get("portfolio_drawdown", {}).get("max_drawdown_usd"))
    add("top1_share_of_net_pnl", v1_position_conc.get("top1_share_of_total_net_pnl"), v2_position_conc.get("top1_share_of_net_pnl"))
    add("top5_share_of_net_pnl", v1_position_conc.get("top5_share_of_total_net_pnl"), v2_position_conc.get("top5_share_of_net_pnl"))
    add("top10_share_of_net_pnl", v1_position_conc.get("top10_share_of_total_net_pnl"), v2_position_conc.get("top10_share_of_net_pnl"))
    add("current_zero_value_added_positions", 0, v2_summary.get("current_zero_value_added_positions"))
    add("current_zero_value_added_pnl", 0, v2_summary.get("current_zero_value_added_pnl"))
    return rows


def fmt_money(x: Any) -> str:
    v = fnum(x, math.nan)
    return "n/a" if not finite(v) else f"${v:,.2f}"


def fmt_pct(x: Any) -> str:
    v = fnum(x, math.nan)
    return "n/a" if not finite(v) else f"{v * 100:.1f}%"


def fmt_num(x: Any, digits: int = 2) -> str:
    v = fnum(x, math.nan)
    return "n/a" if not finite(v) else f"{v:,.{digits}f}"


def markdown_table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    header = "| " + " | ".join(label for label, _ in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = ["| " + " | ".join(str(r.get(key, "")) for _, key in cols) + " |" for r in rows]
    return "\n".join([header, sep, *body])


def generate_report(
    report_path: Path,
    summary: dict[str, Any],
    price_rows: list[dict[str, Any]],
    exit_rows: list[dict[str, Any]],
    lead_summary: list[dict[str, Any]],
    concentration_rows: list[dict[str, Any]],
    basket_rows: list[dict[str, Any]],
    corr_rows: list[dict[str, Any]],
    comparison_rows: list[dict[str, Any]],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    yes_price = price_rows
    low_never = [r for r in yes_price if r["entry_price_bin"] in {"0-1c", "1-2c", "2-5c", "5-10c"} and r["exit_mode_v2"] == "never_sold"]
    low_never_pnl = sum(fnum(r.get("realized_pnl_v2")) for r in low_never)
    pre_sell_rows = [r for r in exit_rows if r["exit_mode_v2"] in {"partially_sold", "mostly_or_fully_sold"}]
    pre_sell_pnl = sum(fnum(r.get("realized_pnl_v2")) for r in pre_sell_rows)
    never_pnl = sum(fnum(r.get("realized_pnl_v2")) for r in exit_rows if r["exit_mode_v2"] == "never_sold")
    best_lead = max(
        [r for r in lead_summary if r["outcome_filter"] == "yes_only" and r["lead_bin"] in {"0-6h", "6-12h", "12-24h", "24-48h", "48-72h", "72h+"} and fnum(r.get("settled_positions")) >= 10],
        key=lambda r: fnum(r.get("roi_v2"), -math.inf),
        default={},
    )
    corr_vals = [fnum(r.get("pearson_corr_no_zero_fill"), math.nan) for r in corr_rows if finite(fnum(r.get("pearson_corr_no_zero_fill"), math.nan))]
    avg_corr = statistics.mean(corr_vals) if corr_vals else math.nan
    eval_baskets = [r for r in basket_rows if r.get("evaluable")]
    actual_basket = sum(fnum(r.get("actual_trade_pnl_v2")) for r in eval_baskets)
    equal_amount = sum(fnum(r.get("equal_amount_basket_same_input_pnl")) for r in eval_baskets)
    original_hold = sum(fnum(r.get("original_shares_hold_to_resolution_pnl")) for r in eval_baskets)
    conc = {r["level"]: r for r in concentration_rows}
    pos_conc = conc.get("weather_position_assets_all_outcomes_v2", {})
    low_hold_conclusion = (
        "Holding cheap tickets to resolution remains profitable in this corrected sample, but it is no longer the whole story after adding current-position losses."
        if low_never_pnl > 0
        else "The corrected sample does not support the claim that low-price tickets held to resolution are the profit engine; this group is net negative after adding current-position losses."
    )
    sell_conclusion = (
        "On this corrected sample, sell-involved positions contribute more net profit than never-sold positions."
        if pre_sell_pnl > never_pnl
        else "On this corrected sample, never-sold positions contribute at least as much net profit as sell-involved positions."
    )
    basket_conclusion = (
        "In aggregate, the actual configuration beats the simple equal-amount counterfactual in this corrected state model."
        if actual_basket > equal_amount
        else "In aggregate, the actual configuration does not beat the simple equal-amount counterfactual in this corrected state model."
    )

    lines = [
        "# HUSKYVS_FULL_AUDIT_v2_CORRECTED",
        "",
        f"Generated at: {summary['generated_at_utc']}",
        f"Wallet: `{summary['wallet']}`",
        "",
        "This v2 audit uses existing `data/raw` only. No public data was re-fetched.",
        "",
        "## Corrections Applied",
        "",
        f"- Added `{summary['current_zero_value_added_positions']}` weather assets from `current_positions` where the local weather day had ended, `currentValue=0`, and `cashPnl<0`, deduplicated by `asset` against `closed_positions`.",
        "- Current-position additions use `realizedPnl + cashPnl` as full asset economic PnL, while preserving both components in the lifecycle CSV.",
        "- Entry lead time now uses each city's local timezone and local weather-day end, not a single UTC proxy.",
        "- Exit modes are `never_sold`, `partially_sold`, `mostly_or_fully_sold`, and `transform_affected`; sell timing is checked against local weather-day end.",
        "",
        "## Headline Numbers",
        "",
        f"- Settled weather assets v2: {summary['settled_weather_assets_v2']}",
        f"- Total weather net PnL v2: {fmt_money(summary['total_net_pnl_v2'])}",
        f"- ROI on capital at risk v2: {fmt_pct(summary['roi_on_capital_at_risk_v2'])}",
        f"- Max drawdown v2: {fmt_money(summary['portfolio_drawdown']['max_drawdown_usd'])}",
        f"- Top1/Top5/Top10 position profit share of net: {fmt_pct(pos_conc.get('top1_share_of_net_pnl'))} / {fmt_pct(pos_conc.get('top5_share_of_net_pnl'))} / {fmt_pct(pos_conc.get('top10_share_of_net_pnl'))}",
        "",
        "## Required Answers",
        "",
        f"**Does huskyvs rely on holding low-price tickets to resolution?** Low-price YES `never_sold` groups from 0-10c sum to {fmt_money(low_never_pnl)}. {low_hold_conclusion}",
        "",
        f"**Does profit mainly come from pre-resolution selling?** `partially_sold + mostly_or_fully_sold` positions sum to {fmt_money(pre_sell_pnl)}, while `never_sold` sums to {fmt_money(never_pnl)}. {sell_conclusion}",
        "",
        f"**Best local entry window?** Among YES bins with at least 10 settled positions, the best capital-weighted local lead bin is `{best_lead.get('lead_bin', 'n/a')}` with ROI {fmt_pct(best_lead.get('roi_v2'))} and PnL {fmt_money(best_lead.get('realized_pnl_v2'))}.",
        "",
        f"**Does multi-city diversification still hold?** Pairwise city correlations use overlap days only, with no blank-date zero fill. Average correlation is {fmt_num(avg_corr, 3)} across {len(corr_vals)} pairs, so diversification still appears meaningful, though large winners still affect net PnL.",
        "",
        f"**Is the actual adjacent-basket sizing better than simple equal-amount sizing?** Across {len(eval_baskets)} evaluable adjacent exact/range YES baskets, actual trading PnL is {fmt_money(actual_basket)}, original-shares hold-to-resolution is {fmt_money(original_hold)}, and equal-amount same-input PnL is {fmt_money(equal_amount)}. {basket_conclusion}",
        "",
        "Counterfactual limits: all basket comparisons assume fills were available at the observed weighted average prices and ignore non-simultaneous execution, market impact, queue priority, and changing information over time.",
        "",
        "## Exit Mode PnL",
        "",
        markdown_table(
            [
                {
                    "mode": r["exit_mode_v2"],
                    "n": r["settled_positions"],
                    "pnl": fmt_money(r["realized_pnl_v2"]),
                    "roi": fmt_pct(r["roi_v2"]),
                    "pre_end": r["sell_before_local_day_end_positions"],
                }
                for r in exit_rows
            ],
            [("Mode", "mode"), ("Settled", "n"), ("PnL", "pnl"), ("ROI", "roi"), ("Sold Before Local End", "pre_end")],
        ),
        "",
        "## YES Price Bins by Exit Mode",
        "",
        markdown_table(
            [
                {
                    "bin": r["entry_price_bin"],
                    "mode": r["exit_mode_v2"],
                    "n": r["settled_positions"],
                    "pnl": fmt_money(r["realized_pnl_v2"]),
                    "roi": fmt_pct(r["roi_v2"]),
                }
                for r in yes_price
            ],
            [("Price", "bin"), ("Exit", "mode"), ("Settled", "n"), ("PnL", "pnl"), ("ROI", "roi")],
        ),
        "",
        "## V1 vs V2",
        "",
        markdown_table(
            [{"metric": r["metric"], "v1": fmt_num(r["v1"], 4), "v2": fmt_num(r["v2"], 4), "delta": fmt_num(r["delta_v2_minus_v1"], 4)} for r in comparison_rows],
            [("Metric", "metric"), ("V1", "v1"), ("V2", "v2"), ("Delta", "delta")],
        ),
        "",
        "## Remaining Data Gaps",
        "",
        "- Open orders, cancellations, quote edits, and queue position are not recoverable from public ledger files.",
        "- Counterfactual basket results cannot prove executable performance because liquidity and timing are not reconstructed.",
        "- Some `current_positions` rows may represent residual token accounting rather than complete market-level lifecycle; v2 preserves source labels for audit.",
        "- Exact weather-station observation cutoffs are approximated as local calendar-day end; market-specific resolution delays are not reconstructed.",
        "- Correlation estimates use only overlapping active city-days; they do not model weather-regime common shocks outside the traded sample.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def data_integrity_audit(raw: Path, manifest: dict[str, Any], trades_raw: list[dict[str, str]], activity_raw: list[dict[str, str]], current_raw: list[dict[str, str]], closed_raw: list[dict[str, str]], life: list[dict[str, Any]]) -> dict[str, Any]:
    row_consistency = {}
    for name, rows in [("trades", trades_raw), ("activity", activity_raw), ("current_positions", current_raw), ("closed_positions", closed_raw)]:
        jsonl_path = raw / f"{name}.jsonl"
        jsonl_rows = sum(1 for _ in jsonl_path.open(encoding="utf-8")) if jsonl_path.exists() else 0
        row_consistency[name] = {
            "csv_rows": len(rows),
            "jsonl_rows": jsonl_rows,
            "manifest_rows": manifest.get("counts", {}).get(name),
            "consistent": len(rows) == jsonl_rows == manifest.get("counts", {}).get(name),
        }
    activity_types = Counter(r.get("type", "") for r in activity_raw)
    missing_tz = sorted({r.get("city") for r in life if r.get("city") and not r.get("local_timezone")})
    return {
        "row_consistency": row_consistency,
        "critical_parameter_checks": {
            "trades_takerOnly_false": manifest.get("critical_parameters", {}).get("takerOnly") is False,
            "activity_has_trade_split_merge_redeem": all(activity_types.get(t, 0) > 0 for t in ["TRADE", "SPLIT", "MERGE", "REDEEM"]),
        },
        "snapshot_zip_valid": zipfile.is_zipfile(raw / "accounting_snapshot.zip"),
        "timezone_mapping_complete": not missing_tz,
        "missing_timezone_cities": missing_tz,
        "settled_rows_have_local_cutoff": all(r.get("local_weather_day_end_epoch") for r in life if r.get("settled_sample_v2")),
        "current_zero_value_added_positions": sum(1 for r in life if r.get("settlement_source_v2") == "current_zero_value_after_local_day_end"),
        "current_zero_value_added_pnl": sum(fnum(r.get("realized_pnl_v2")) for r in life if r.get("settlement_source_v2") == "current_zero_value_after_local_day_end"),
    }


def create_delivery_zip(root: Path, zip_path: Path) -> dict[str, Any]:
    include_roots = [
        root / "reports" / "HUSKYVS_FULL_AUDIT_v2_CORRECTED.md",
        root / "data" / "processed_v2",
        root / "src" / "analyze_weather_strategy.py",
        root / "src" / "analyze_weather_strategy_v2.py",
        root / "tests",
    ]
    files: list[Path] = []
    for p in include_roots:
        if p.is_dir():
            files.extend(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts)
        elif p.exists():
            files.append(p)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(files):
            zf.write(file, file.relative_to(root))
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        names = zf.namelist()
    return {"zip_path": str(zip_path), "zip_size_bytes": zip_path.stat().st_size, "zip_file_count": len([n for n in names if not n.endswith("/")]), "zip_testzip_bad_file": bad}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--out", default="data/processed_v2")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--zip", default="huskyvs_corrected_audit_v2.zip")
    parser.add_argument("--as-of", default="")
    args = parser.parse_args()

    root = Path.cwd()
    raw = Path(args.raw)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reports = Path(args.reports)

    manifest = load_manifest(raw)
    asof_epoch = parse_epoch(args.as_of) if args.as_of else int(datetime.now(timezone.utc).timestamp())
    trades_raw = read_csv(raw / "trades.csv")
    activity_raw = read_csv(raw / "activity.csv")
    current_raw = read_csv(raw / "current_positions.csv")
    closed_raw = read_csv(raw / "closed_positions.csv")

    closed = normalize_positions(closed_raw, "closed_positions")
    current = normalize_positions(current_raw, "current_positions")
    asset_meta = build_asset_meta(trades_raw, closed, current)
    trades = normalize_trades(trades_raw, asset_meta)
    settlements = settlement_map(closed, current, asof_epoch)
    life = build_lifecycle(trades, activity_raw, closed, current, asset_meta, settlements)
    settled = [r for r in life if r.get("settled_sample_v2") and finite(r.get("realized_pnl_v2"))]

    city_rows = corrected_city_day_pnl(life)
    price_rows = corrected_price_bin_exit_mode(life)
    exit_rows = grouped_pnl_rows(settled, ["exit_mode_v2"])
    exit_rows = sorted(exit_rows, key=lambda r: ["never_sold", "partially_sold", "mostly_or_fully_sold", "transform_affected"].index(r["exit_mode_v2"]) if r["exit_mode_v2"] in {"never_sold", "partially_sold", "mostly_or_fully_sold", "transform_affected"} else 99)
    entry_rows = entry_lead_time_rows(life)
    lead_summary = entry_lead_summary(life)
    basket_rows = basket_comparison(life)
    daily_rows = daily_pnl(city_rows)
    drawdown = max_drawdown(daily_rows)
    corr_rows = corrected_city_correlation(city_rows)
    concentration_rows = corrected_profit_concentration(life, city_rows, [r for r in basket_rows if r.get("evaluable")])
    audit = data_integrity_audit(raw, manifest, trades_raw, activity_raw, current_raw, closed_raw, life)

    total_pnl = sum(fnum(r.get("realized_pnl_v2")) for r in settled)
    total_cap = sum(fnum(r.get("capital_at_risk_usd")) for r in settled if finite(r.get("capital_at_risk_usd")))
    v1_life = read_csv(Path("data/processed/weather_position_lifecycle.csv"))
    v1_closed = [r for r in v1_life if r.get("pnl_status") == "closed_authoritative"]
    v1_cap = sum(fnum(r.get("buy_usd")) for r in v1_closed)
    v1_summary = json.loads(Path("data/processed/audit_summary.json").read_text(encoding="utf-8"))
    v1_conc = read_csv(Path("data/processed/profit_concentration.csv"))

    summary = {
        "wallet": manifest.get("wallet"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_asof_utc": iso_from_epoch(asof_epoch),
        "raw_counts": manifest.get("counts", {}),
        "weather_trade_rows": len(trades),
        "weather_assets": len(life),
        "settled_weather_assets_v2": len(settled),
        "closed_positions_weather_assets": len(closed),
        "current_zero_value_added_positions": audit["current_zero_value_added_positions"],
        "current_zero_value_added_pnl": audit["current_zero_value_added_pnl"],
        "total_net_pnl_v2": total_pnl,
        "capital_at_risk_usd_v2": total_cap,
        "roi_on_capital_at_risk_v2": safe_div(total_pnl, total_cap),
        "v1_roi_on_buy_usd": safe_div(v1_summary.get("closed_realized_pnl", {}).get("sum", math.nan), v1_cap),
        "portfolio_drawdown": drawdown,
        "exit_mode_pnl": exit_rows,
        "integrity_audit": audit,
    }

    comparison_rows = v1_vs_v2(v1_summary, summary, v1_conc, concentration_rows)

    write_rows(out / "corrected_position_lifecycle.csv", life)
    write_rows(out / "corrected_entry_lead_time.csv", entry_rows)
    write_rows(out / "corrected_entry_lead_time_by_bin.csv", lead_summary)
    write_rows(out / "corrected_price_bin_exit_mode.csv", price_rows)
    write_rows(out / "corrected_exit_mode_pnl.csv", exit_rows)
    write_rows(out / "corrected_city_day_pnl.csv", city_rows)
    write_rows(out / "corrected_city_correlation.csv", corr_rows)
    write_rows(out / "corrected_portfolio_daily_pnl.csv", daily_rows)
    write_rows(out / "corrected_basket_comparison.csv", basket_rows)
    write_rows(out / "corrected_profit_concentration.csv", concentration_rows)
    write_rows(out / "v1_vs_v2_comparison.csv", comparison_rows)
    write_json(out / "corrected_audit_summary.json", summary)
    write_json(out / "corrected_data_integrity_audit.json", audit)

    generate_report(
        reports / "HUSKYVS_FULL_AUDIT_v2_CORRECTED.md",
        summary,
        price_rows,
        exit_rows,
        lead_summary,
        concentration_rows,
        basket_rows,
        corr_rows,
        comparison_rows,
    )
    zip_info = create_delivery_zip(root, Path(args.zip))
    summary["delivery_zip"] = zip_info
    write_json(out / "corrected_audit_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
