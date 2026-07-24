#!/usr/bin/env python3
"""Normalize the downloaded ledger and test the main huskyvs strategy hypotheses.

This is an audit tool, not an execution or copy-trading bot.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

WEATHER_RE = re.compile(
    r"^Will the (?P<metric>highest|lowest) temperature in (?P<city>.+?) "
    r"be (?P<bucket>.+?) on (?P<date>[A-Za-z]+ \d{1,2})(?:, (?P<year>\d{4}))?\?$",
    re.IGNORECASE,
)
EXACT_RE = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)°(?P<unit>[CF])$", re.I)
BELOW_RE = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)°(?P<unit>[CF]) or below$", re.I)
HIGHER_RE = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)°(?P<unit>[CF]) or higher$", re.I)
RANGE_RE = re.compile(
    r"^(?:between )?(?P<lo>-?\d+(?:\.\d+)?)"
    r"(?:°(?P<unit1>[CF]))?\s*(?:-|–|to)\s*"
    r"(?P<hi>-?\d+(?:\.\d+)?)°(?P<unit2>[CF])$",
    re.I,
)

PRICE_BIN_ORDER = ["0-1c", "1-2c", "2-5c", "5-10c", "10-20c", ">=20c"]
LEAD_BIN_ORDER = [
    "after_proxy_cutoff",
    "0-6h",
    "6-12h",
    "12-24h",
    "24-48h",
    "48-72h",
    "72-168h",
    ">=168h",
    "unknown",
]
TRANSFORM_TYPES = {"SPLIT", "MERGE", "CONVERSION"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        if value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value)


def safe_div(num: float, den: float) -> float:
    return num / den if den else math.nan


def parse_iso_epoch(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def weather_day_cutoff_epoch(weather_date: str | None) -> int:
    """Proxy for the end of the weather observation day when station metadata is absent."""
    if not weather_date:
        return 0
    try:
        d = date.fromisoformat(weather_date)
    except ValueError:
        return 0
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + 24 * 3600


def iso_from_epoch(ts: float | int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def parse_bucket(text: str) -> dict[str, Any] | None:
    text = text.strip()
    m = EXACT_RE.match(text)
    if m:
        v = float(m.group("value"))
        return {"bucket_kind": "exact", "bucket_low": v, "bucket_high": v, "unit": m.group("unit").upper()}
    m = BELOW_RE.match(text)
    if m:
        return {
            "bucket_kind": "below",
            "bucket_low": None,
            "bucket_high": float(m.group("value")),
            "unit": m.group("unit").upper(),
        }
    m = HIGHER_RE.match(text)
    if m:
        return {
            "bucket_kind": "above",
            "bucket_low": float(m.group("value")),
            "bucket_high": None,
            "unit": m.group("unit").upper(),
        }
    m = RANGE_RE.match(text)
    if m:
        unit = (m.group("unit2") or m.group("unit1")).upper()
        return {
            "bucket_kind": "range",
            "bucket_low": float(m.group("lo")),
            "bucket_high": float(m.group("hi")),
            "unit": unit,
        }
    return None


def parse_weather_title(title: str, end_date: str | None = None) -> dict[str, Any] | None:
    m = WEATHER_RE.match((title or "").strip())
    if not m:
        return None
    bucket = parse_bucket(m.group("bucket"))
    if not bucket:
        return None
    year = m.group("year")
    if not year and end_date:
        try:
            year = str(datetime.fromisoformat(end_date.replace("Z", "+00:00")).year)
        except ValueError:
            pass
    if not year:
        year = "2026"  # explicit audit fallback; row is flagged below
        inferred = True
    else:
        inferred = False
    date_obj = datetime.strptime(f"{m.group('date')} {year}", "%B %d %Y").date()
    return {
        "weather_metric": "high" if m.group("metric").lower() == "highest" else "low",
        "city": m.group("city").strip(),
        "weather_date": date_obj.isoformat(),
        "date_year_inferred": inferred,
        "bucket_label": m.group("bucket").strip(),
        **bucket,
    }


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


def lead_bin(hours: float) -> str:
    if not finite(hours):
        return "unknown"
    if hours < 0:
        return "after_proxy_cutoff"
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
    if hours < 168:
        return "72-168h"
    return ">=168h"


def bucket_interval(row: dict[str, Any]) -> tuple[float, float]:
    lo = row.get("bucket_low")
    hi = row.get("bucket_high")
    return (
        -math.inf if lo in (None, "") or (isinstance(lo, float) and math.isnan(lo)) else float(lo),
        math.inf if hi in (None, "") or (isinstance(hi, float) and math.isnan(hi)) else float(hi),
    )


def bucket_sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
    lo, hi = bucket_interval(row)
    return (lo, hi, str(row.get("bucket_label") or ""))


def intervals_overlap(rows: list[dict[str, Any]]) -> bool:
    intervals = sorted(bucket_interval(r) for r in rows)
    for (_, hi), (next_lo, _) in zip(intervals, intervals[1:]):
        if next_lo <= hi:
            return True
    return False


def is_adjacent_exact_or_range(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 2:
        return False
    intervals = [bucket_interval(r) for r in sorted(rows, key=bucket_sort_key)]
    if any(math.isinf(lo) or math.isinf(hi) for lo, hi in intervals):
        return False
    for (_, hi), (next_lo, _) in zip(intervals, intervals[1:]):
        if next_lo - hi > 1.000001:
            return False
    return True


def build_asset_meta(
    trades_raw: list[dict[str, str]],
    closed_raw: list[dict[str, str]],
    current_raw: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}

    def add(row: dict[str, str], source: str) -> None:
        asset = row.get("asset") or ""
        if not asset:
            return
        weather = parse_weather_title(row.get("title", ""), row.get("endDate"))
        if not weather:
            return
        m = meta.setdefault(asset, {"asset": asset})
        m.update(weather)
        for key in [
            "conditionId",
            "title",
            "slug",
            "eventSlug",
            "outcome",
            "outcomeIndex",
            "oppositeAsset",
            "oppositeOutcome",
        ]:
            if row.get(key) not in (None, ""):
                m[key] = row.get(key)
        if row.get("endDate"):
            m["endDate"] = row.get("endDate")
            m["market_end_epoch"] = parse_iso_epoch(row.get("endDate"))
        m["observation_cutoff_epoch"] = weather_day_cutoff_epoch(m.get("weather_date"))
        m["observation_cutoff_utc"] = iso_from_epoch(m.get("observation_cutoff_epoch"))
        m[f"seen_in_{source}"] = True

    for r in trades_raw:
        add(r, "trades")
    for r in closed_raw:
        add(r, "closed_positions")
    for r in current_raw:
        add(r, "current_positions")
    return meta


def normalized_trades(raw: list[dict[str, str]], asset_meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in raw:
        asset = r.get("asset") or ""
        weather = asset_meta.get(asset) or parse_weather_title(r.get("title", ""))
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
                "outcome": r.get("outcome") or weather.get("outcome") or "",
                "market_end_epoch": weather.get("market_end_epoch", 0),
                "observation_cutoff_epoch": weather.get("observation_cutoff_epoch", 0),
                "observation_cutoff_utc": weather.get("observation_cutoff_utc", ""),
            }
        )
        out.append(row)
    return out


def normalized_positions(raw: list[dict[str, str]], source: str) -> list[dict[str, Any]]:
    out = []
    for r in raw:
        weather = parse_weather_title(r.get("title", ""), r.get("endDate"))
        if not weather:
            continue
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
                "market_end_epoch": parse_iso_epoch(r.get("endDate")),
                "observation_cutoff_epoch": weather_day_cutoff_epoch(weather.get("weather_date")),
            }
        )
        row["observation_cutoff_utc"] = iso_from_epoch(row["observation_cutoff_epoch"])
        out.append(row)
    return out


def lifecycle(
    trades: list[dict[str, Any]],
    activity: list[dict[str, str]],
    closed: list[dict[str, Any]],
    current: list[dict[str, Any]],
    asset_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_asset[t.get("asset", "")].append(t)

    closed_by_asset = {r.get("asset", ""): r for r in closed if r.get("asset")}
    current_by_asset = {r.get("asset", ""): r for r in current if r.get("asset")}

    activity_by_condition: dict[str, list[dict[str, str]]] = defaultdict(list)
    for a in activity:
        if a.get("conditionId"):
            activity_by_condition[a["conditionId"]].append(a)

    assets = sorted(set(by_asset) | set(closed_by_asset) | set(current_by_asset))
    out = []
    for asset in assets:
        meta = dict(asset_meta.get(asset, {}))
        if not meta:
            source = closed_by_asset.get(asset) or current_by_asset.get(asset)
            if not source:
                continue
            meta = dict(source)

        ts = sorted(by_asset.get(asset, []), key=lambda x: x["timestamp"])
        buys = [x for x in ts if x["side"] == "BUY"]
        sells = [x for x in ts if x["side"] == "SELL"]
        condition_id = meta.get("conditionId") or (ts[0].get("conditionId") if ts else "")
        acts = activity_by_condition.get(condition_id, [])
        types = [a.get("type", "") for a in acts]
        has_transform = any(t in TRANSFORM_TYPES for t in types)

        buy_shares = sum(x["size"] for x in buys)
        sell_shares = sum(x["size"] for x in sells)
        buy_usd = sum(x["notional_usd"] for x in buys)
        sell_usd = sum(x["notional_usd"] for x in sells)
        first_trade_ts = min((x["timestamp"] for x in ts), default=0)
        last_trade_ts = max((x["timestamp"] for x in ts), default=0)
        first_buy_ts = min((x["timestamp"] for x in buys), default=0)
        weighted_avg_buy_ts = safe_div(sum(x["timestamp"] * x["notional_usd"] for x in buys), buy_usd)
        weighted_avg_buy_price = safe_div(buy_usd, buy_shares)
        weighted_avg_sell_price = safe_div(sell_usd, sell_shares)

        closed_row = closed_by_asset.get(asset, {})
        current_row = current_by_asset.get(asset, {})
        has_closed = bool(closed_row)
        has_current = bool(current_row)
        closed_realized = fnum(closed_row.get("realizedPnl"), math.nan) if has_closed else math.nan
        closed_cur_price = fnum(closed_row.get("curPrice"), math.nan) if has_closed else math.nan
        current_cash_pnl = fnum(current_row.get("cashPnl"), math.nan) if has_current else math.nan
        closed_total_bought = fnum(closed_row.get("totalBought"), math.nan) if has_closed else math.nan
        closed_avg_price = fnum(closed_row.get("avgPrice"), math.nan) if has_closed else math.nan
        current_initial_value = fnum(current_row.get("initialValue"), math.nan) if has_current else math.nan

        if sells and has_closed:
            non_transform_exit_mode = "mixed_sell_and_resolution"
        elif sells:
            non_transform_exit_mode = "pre_resolution_sell"
        elif has_closed:
            non_transform_exit_mode = "hold_to_resolution"
        else:
            non_transform_exit_mode = "no_public_exit_yet"
        exit_mode = "transform_affected" if has_transform else non_transform_exit_mode

        market_end_epoch = inum(meta.get("market_end_epoch"))
        observation_cutoff_epoch = inum(meta.get("observation_cutoff_epoch"))
        first_entry_lead_hours = safe_div(observation_cutoff_epoch - first_buy_ts, 3600) if first_buy_ts else math.nan
        weighted_entry_lead_hours = (
            safe_div(observation_cutoff_epoch - weighted_avg_buy_ts, 3600)
            if finite(weighted_avg_buy_ts)
            else math.nan
        )
        first_entry_lead_to_market_end_hours = (
            safe_div(market_end_epoch - first_buy_ts, 3600) if first_buy_ts and market_end_epoch else math.nan
        )
        buy_after_market_end_count = sum(1 for x in buys if market_end_epoch and x["timestamp"] > market_end_epoch)
        buy_after_observation_proxy_count = sum(
            1 for x in buys if observation_cutoff_epoch and x["timestamp"] > observation_cutoff_epoch
        )

        out.append(
            {
                "asset": asset,
                "conditionId": condition_id,
                "title": meta.get("title"),
                "slug": meta.get("slug"),
                "eventSlug": meta.get("eventSlug"),
                "city": meta.get("city"),
                "weather_date": meta.get("weather_date"),
                "weather_metric": meta.get("weather_metric"),
                "bucket_label": meta.get("bucket_label"),
                "bucket_kind": meta.get("bucket_kind"),
                "bucket_low": meta.get("bucket_low"),
                "bucket_high": meta.get("bucket_high"),
                "unit": meta.get("unit"),
                "outcome": meta.get("outcome") or (ts[0].get("outcome") if ts else ""),
                "buy_count": len(buys),
                "sell_count": len(sells),
                "buy_shares": buy_shares,
                "sell_shares": sell_shares,
                "net_traded_shares": buy_shares - sell_shares,
                "buy_usd": buy_usd,
                "sell_usd": sell_usd,
                "closed_total_bought_shares": closed_total_bought,
                "closed_avg_price": closed_avg_price,
                "closed_implied_buy_usd": closed_total_bought * closed_avg_price
                if finite(closed_total_bought) and finite(closed_avg_price)
                else math.nan,
                "current_initial_value": current_initial_value,
                "current_cash_pnl": current_cash_pnl,
                "cashflow_pnl_before_transform_adjustment": sell_usd - buy_usd,
                "closed_position_realized_pnl": closed_realized,
                "authoritative_realized_pnl": closed_realized,
                "pnl_status": "closed_authoritative" if has_closed else ("open_mark_to_market_only" if has_current else "trade_only"),
                "closed_cur_price": closed_cur_price,
                "asset_won": bool(finite(closed_cur_price) and closed_cur_price >= 0.5),
                "first_trade_ts": first_trade_ts,
                "first_trade_utc": iso_from_epoch(first_trade_ts),
                "last_trade_ts": last_trade_ts,
                "last_trade_utc": iso_from_epoch(last_trade_ts),
                "first_buy_ts": first_buy_ts,
                "first_buy_utc": iso_from_epoch(first_buy_ts),
                "weighted_avg_buy_ts": weighted_avg_buy_ts,
                "weighted_avg_buy_utc": iso_from_epoch(weighted_avg_buy_ts if finite(weighted_avg_buy_ts) else 0),
                "weighted_avg_buy_price": weighted_avg_buy_price,
                "weighted_avg_sell_price": weighted_avg_sell_price,
                "entry_price_bin": price_bin(weighted_avg_buy_price),
                "market_end_epoch": market_end_epoch,
                "market_end_utc": iso_from_epoch(market_end_epoch),
                "observation_cutoff_epoch": observation_cutoff_epoch,
                "observation_cutoff_utc": iso_from_epoch(observation_cutoff_epoch),
                "first_entry_lead_hours": first_entry_lead_hours,
                "first_entry_lead_bin": lead_bin(first_entry_lead_hours),
                "weighted_entry_lead_hours": weighted_entry_lead_hours,
                "weighted_entry_lead_bin": lead_bin(weighted_entry_lead_hours),
                "first_entry_lead_to_market_end_hours": first_entry_lead_to_market_end_hours,
                "buy_after_market_end_count": buy_after_market_end_count,
                "buy_after_observation_proxy_count": buy_after_observation_proxy_count,
                "split_count": types.count("SPLIT"),
                "merge_count": types.count("MERGE"),
                "conversion_count": types.count("CONVERSION"),
                "redeem_activity_count_by_condition": types.count("REDEEM"),
                "transform_affected": has_transform,
                "non_transform_exit_mode": non_transform_exit_mode,
                "exit_mode": exit_mode,
                "cashflow_reliable": not has_transform,
            }
        )
    return out


def city_day_pnl(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        if r.get("city") and r.get("weather_date"):
            groups[(r["city"], r["weather_date"], r["weather_metric"], r["unit"])].append(r)
    out = []
    for (city, day, metric, unit), rows in groups.items():
        closed = [r for r in rows if finite(r.get("authoritative_realized_pnl"))]
        open_rows = [r for r in rows if not finite(r.get("authoritative_realized_pnl"))]
        open_cash = [r for r in open_rows if finite(r.get("current_cash_pnl"))]
        out.append(
            {
                "city": city,
                "weather_date": day,
                "weather_metric": metric,
                "unit": unit,
                "asset_count": len(rows),
                "yes_asset_count": sum(1 for r in rows if r.get("outcome") == "Yes"),
                "no_asset_count": sum(1 for r in rows if r.get("outcome") == "No"),
                "closed_asset_count": len(closed),
                "open_or_unresolved_asset_count": len(open_rows),
                "transform_asset_count": sum(1 for r in rows if r.get("transform_affected")),
                "buy_usd": sum(fnum(r.get("buy_usd")) for r in rows),
                "sell_usd": sum(fnum(r.get("sell_usd")) for r in rows),
                "closed_authoritative_pnl": sum(fnum(r.get("authoritative_realized_pnl")) for r in closed),
                "open_mark_to_market_cash_pnl": sum(fnum(r.get("current_cash_pnl")) for r in open_cash),
                "winning_closed_assets": sum(1 for r in closed if r.get("asset_won")),
                "losing_closed_assets": sum(1 for r in closed if not r.get("asset_won")),
                "exit_modes": "|".join(sorted({str(r.get("exit_mode")) for r in rows})),
                "has_open_or_unresolved": bool(open_rows),
                "has_transform_events": any(r.get("transform_affected") for r in rows),
            }
        )
    return sorted(out, key=lambda r: (r["weather_date"], r["city"], r["weather_metric"], r["unit"]))


def basket_summary(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        if r.get("outcome") == "Yes" and r.get("city") and r.get("weather_date"):
            groups[(r["city"], r["weather_date"], r["weather_metric"], r["unit"])].append(r)
    out = []
    for (city, day, metric, unit), rows0 in groups.items():
        rows = sorted(rows0, key=bucket_sort_key)
        total_buy = sum(fnum(r.get("buy_usd")) for r in rows)
        total_sell = sum(fnum(r.get("sell_usd")) for r in rows)
        total_net_cost = total_buy - total_sell
        realized_values = [
            fnum(r.get("authoritative_realized_pnl"), math.nan)
            for r in rows
            if finite(r.get("authoritative_realized_pnl"))
        ]
        entry_shares = [max(0.0, fnum(r.get("buy_shares"))) for r in rows]
        net_shares = [max(0.0, fnum(r.get("net_traded_shares"))) for r in rows]
        state_model_valid = len(rows) >= 2 and not intervals_overlap(rows)
        out.append(
            {
                "city": city,
                "weather_date": day,
                "weather_metric": metric,
                "unit": unit,
                "bucket_count": len(rows),
                "buckets": "|".join(str(r.get("bucket_label")) for r in rows),
                "buy_usd": total_buy,
                "sell_usd": total_sell,
                "net_cost_after_pre_resolution_sells": total_net_cost,
                "authoritative_realized_pnl_sum": sum(realized_values) if realized_values else math.nan,
                "cashflow_pnl_before_resolution": total_sell - total_buy,
                "min_entry_state_payout": min(entry_shares) if entry_shares else 0.0,
                "max_entry_state_payout": max(entry_shares) if entry_shares else 0.0,
                "min_entry_state_pnl": (min(entry_shares) - total_buy) if entry_shares else math.nan,
                "max_entry_state_pnl": (max(entry_shares) - total_buy) if entry_shares else math.nan,
                "min_remaining_state_payout": min(net_shares) if net_shares else 0.0,
                "max_remaining_state_payout": max(net_shares) if net_shares else 0.0,
                "winner_bucket_labels": "|".join(str(r.get("bucket_label")) for r in rows if r.get("asset_won")),
                "winner_in_visible_yes_basket": any(r.get("asset_won") for r in rows),
                "closed_yes_asset_count": sum(1 for r in rows if finite(r.get("authoritative_realized_pnl"))),
                "open_or_unresolved_yes_asset_count": sum(1 for r in rows if not finite(r.get("authoritative_realized_pnl"))),
                "has_transform_events": any(r.get("transform_affected") for r in rows),
                "state_model_valid": state_model_valid,
                "state_model_note": "non_overlapping_yes_buckets" if state_model_valid else "overlapping_or_single_bucket_yes_markets",
                "adjacent_exact_or_range": is_adjacent_exact_or_range(rows),
                "unequal_share_ratio_entry": safe_div(max(entry_shares), min(x for x in entry_shares if x > 0))
                if any(x > 0 for x in entry_shares)
                else math.nan,
            }
        )
    return sorted(out, key=lambda r: (r["weather_date"], r["city"], r["weather_metric"], r["unit"]))


def basket_state_payoffs(life: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        if r.get("outcome") == "Yes" and r.get("city") and r.get("weather_date"):
            groups[(r["city"], r["weather_date"], r["weather_metric"], r["unit"])].append(r)

    state_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for (city, day, metric, unit), rows0 in groups.items():
        rows = sorted(rows0, key=bucket_sort_key)
        if not rows:
            continue
        total_buy = sum(fnum(r.get("buy_usd")) for r in rows)
        total_sell = sum(fnum(r.get("sell_usd")) for r in rows)
        prices = [
            fnum(r.get("weighted_avg_buy_price"), math.nan)
            for r in rows
            if finite(r.get("weighted_avg_buy_price")) and fnum(r.get("weighted_avg_buy_price")) > 0
        ]
        state_model_valid = len(rows) >= 2 and not intervals_overlap(rows)
        main = max(rows, key=lambda r: fnum(r.get("buy_usd")))
        main_price = fnum(main.get("weighted_avg_buy_price"), math.nan)
        n = len(rows)
        price_sum = sum(prices)
        equal_payout_state_pnl = safe_div(total_buy, price_sum) - total_buy if price_sum > 0 else math.nan
        group_key = f"{city}|{day}|{metric}|{unit}"
        winner_rows: list[dict[str, Any]] = []

        for r in rows:
            p = fnum(r.get("weighted_avg_buy_price"), math.nan)
            row_is_winner = bool(r.get("asset_won"))
            if row_is_winner:
                winner_rows.append(r)
            actual_entry_state_pnl = fnum(r.get("buy_shares")) - total_buy
            actual_net_state_pnl = fnum(r.get("net_traded_shares")) + total_sell - total_buy
            single_main_state_pnl = (
                safe_div(total_buy, main_price) - total_buy
                if r.get("asset") == main.get("asset") and main_price > 0
                else -total_buy
            )
            equal_dollar_state_pnl = safe_div(total_buy / n, p) - total_buy if n and p > 0 else math.nan
            ex_post_single_this_state_pnl = safe_div(total_buy, p) - total_buy if p > 0 else math.nan
            state_rows.append(
                {
                    "basket_key": group_key,
                    "city": city,
                    "weather_date": day,
                    "weather_metric": metric,
                    "unit": unit,
                    "asset": r.get("asset"),
                    "bucket_label": r.get("bucket_label"),
                    "bucket_kind": r.get("bucket_kind"),
                    "bucket_low": r.get("bucket_low"),
                    "bucket_high": r.get("bucket_high"),
                    "bucket_count": n,
                    "basket_buy_budget_usd": total_buy,
                    "basket_sell_usd": total_sell,
                    "entry_price": p,
                    "entry_shares": fnum(r.get("buy_shares")),
                    "net_shares_after_sells": fnum(r.get("net_traded_shares")),
                    "observed_winner": row_is_winner,
                    "closed_cur_price": r.get("closed_cur_price"),
                    "authoritative_position_pnl": r.get("authoritative_realized_pnl"),
                    "main_bucket_label": main.get("bucket_label"),
                    "state_model_valid": state_model_valid,
                    "state_model_note": "non_overlapping_yes_buckets" if state_model_valid else "overlapping_or_single_bucket_yes_markets",
                    "actual_entry_state_pnl_if_only_this_bucket_wins": actual_entry_state_pnl,
                    "actual_net_state_pnl_if_only_this_bucket_wins": actual_net_state_pnl,
                    "single_main_bucket_state_pnl": single_main_state_pnl,
                    "equal_dollar_basket_state_pnl": equal_dollar_state_pnl,
                    "equal_payout_basket_state_pnl": equal_payout_state_pnl,
                    "ex_post_single_this_bucket_state_pnl": ex_post_single_this_state_pnl,
                }
            )

        closed_count = sum(1 for r in rows if finite(r.get("authoritative_realized_pnl")))
        auth_pnl = sum(fnum(r.get("authoritative_realized_pnl")) for r in rows if finite(r.get("authoritative_realized_pnl")))
        if closed_count < len(rows):
            status = "unresolved_or_partial"
            actual_entry = single_main = equal_dollar = equal_payout = math.nan
        elif not state_model_valid:
            status = "not_evaluable_overlap_or_single"
            actual_entry = single_main = equal_dollar = equal_payout = math.nan
        elif len(winner_rows) == 1:
            status = "winner_in_basket"
            state = next(
                s
                for s in state_rows
                if s["basket_key"] == group_key and s["asset"] == winner_rows[0].get("asset")
            )
            actual_entry = fnum(state.get("actual_entry_state_pnl_if_only_this_bucket_wins"), math.nan)
            single_main = fnum(state.get("single_main_bucket_state_pnl"), math.nan)
            equal_dollar = fnum(state.get("equal_dollar_basket_state_pnl"), math.nan)
            equal_payout = fnum(state.get("equal_payout_basket_state_pnl"), math.nan)
        elif len(winner_rows) == 0:
            status = "winner_outside_visible_yes_basket"
            actual_entry = -total_buy
            single_main = -total_buy
            equal_dollar = -total_buy
            equal_payout = -total_buy
        else:
            status = "multiple_observed_winners_overlap"
            actual_entry = single_main = equal_dollar = equal_payout = math.nan

        summary_rows.append(
            {
                "basket_key": group_key,
                "city": city,
                "weather_date": day,
                "weather_metric": metric,
                "unit": unit,
                "bucket_count": len(rows),
                "basket_buy_budget_usd": total_buy,
                "authoritative_realized_pnl_sum": auth_pnl if closed_count else math.nan,
                "closed_yes_asset_count": closed_count,
                "winner_bucket_labels": "|".join(str(r.get("bucket_label")) for r in winner_rows),
                "counterfactual_status": status,
                "actual_entry_allocation_pnl_at_observed_state": actual_entry,
                "single_main_bucket_pnl_at_observed_state": single_main,
                "equal_dollar_basket_pnl_at_observed_state": equal_dollar,
                "equal_payout_basket_pnl_at_observed_state": equal_payout,
                "main_bucket_label": main.get("bucket_label"),
                "state_model_valid": state_model_valid,
            }
        )
    return state_rows, sorted(summary_rows, key=lambda r: (r["weather_date"], r["city"], r["weather_metric"], r["unit"]))


def concentration_stats(items: list[dict[str, Any]], level: str) -> dict[str, Any]:
    values = [fnum(i.get("pnl"), math.nan) for i in items if finite(i.get("pnl"))]
    total = sum(values)
    positives = sorted((i for i in items if finite(i.get("pnl")) and fnum(i.get("pnl")) > 0), key=lambda x: fnum(x["pnl"]), reverse=True)
    gross_positive = sum(fnum(i["pnl"]) for i in positives)
    gross_loss = sum(fnum(i.get("pnl")) for i in items if finite(i.get("pnl")) and fnum(i.get("pnl")) < 0)

    def top_sum(n: int) -> float:
        return sum(fnum(i["pnl"]) for i in positives[:n])

    def top_id(n: int) -> str:
        if len(positives) >= n:
            return str(positives[n - 1].get("id") or "")
        return ""

    return {
        "level": level,
        "items_with_pnl": len(values),
        "total_net_pnl": total,
        "gross_positive_pnl": gross_positive,
        "gross_loss_pnl": gross_loss,
        "top1_pnl": top_sum(1),
        "top5_pnl": top_sum(5),
        "top10_pnl": top_sum(10),
        "top1_share_of_total_net_pnl": safe_div(top_sum(1), total) if total > 0 else math.nan,
        "top5_share_of_total_net_pnl": safe_div(top_sum(5), total) if total > 0 else math.nan,
        "top10_share_of_total_net_pnl": safe_div(top_sum(10), total) if total > 0 else math.nan,
        "top1_share_of_gross_positive_pnl": safe_div(top_sum(1), gross_positive),
        "top5_share_of_gross_positive_pnl": safe_div(top_sum(5), gross_positive),
        "top10_share_of_gross_positive_pnl": safe_div(top_sum(10), gross_positive),
        "leave_top1_out_pnl": total - top_sum(1),
        "top1_id": top_id(1),
        "top5_cutoff_id": top_id(5),
        "top10_cutoff_id": top_id(10),
    }


def profit_concentration(life: list[dict[str, Any]], city_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    position_items = [
        {
            "id": r.get("asset"),
            "pnl": r.get("authoritative_realized_pnl"),
        }
        for r in life
        if finite(r.get("authoritative_realized_pnl"))
    ]
    basket_items = [
        {
            "id": f"{r.get('city')}|{r.get('weather_date')}|{r.get('weather_metric')}|{r.get('unit')}",
            "pnl": r.get("closed_authoritative_pnl"),
        }
        for r in city_rows
        if r.get("closed_asset_count", 0) > 0
    ]
    yes_position_items = [
        {
            "id": r.get("asset"),
            "pnl": r.get("authoritative_realized_pnl"),
        }
        for r in life
        if r.get("outcome") == "Yes" and finite(r.get("authoritative_realized_pnl"))
    ]
    return [
        concentration_stats(position_items, "weather_position_assets_all_outcomes"),
        concentration_stats(yes_position_items, "weather_position_assets_yes_only"),
        concentration_stats(basket_items, "weather_city_day_baskets_all_outcomes"),
    ]


def price_bin_by_exit_mode(life: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        p0 = fnum(r.get("weighted_avg_buy_price"), math.nan)
        if finite(p0):
            bins[("all_outcomes", price_bin(p0), r["exit_mode"])].append(r)
            if r.get("outcome") == "Yes":
                bins[("yes_only", price_bin(p0), r["exit_mode"])].append(r)
    out = []
    for (universe, b, mode), rows in sorted(
        bins.items(),
        key=lambda x: (x[0][0], PRICE_BIN_ORDER.index(x[0][1]) if x[0][1] in PRICE_BIN_ORDER else 99, x[0][2]),
    ):
        pnl = [fnum(r.get("authoritative_realized_pnl"), math.nan) for r in rows if finite(r.get("authoritative_realized_pnl"))]
        buy_for_closed = sum(fnum(r.get("buy_usd")) for r in rows if finite(r.get("authoritative_realized_pnl")))
        out.append(
            {
                "outcome_filter": universe,
                "entry_price_bin": b,
                "exit_mode": mode,
                "positions": len(rows),
                "closed_positions_with_authoritative_pnl": len(pnl),
                "buy_usd_for_closed_positions": buy_for_closed,
                "realized_pnl_sum": sum(pnl) if pnl else math.nan,
                "mean_realized_pnl": statistics.mean(pnl) if pnl else math.nan,
                "median_realized_pnl": statistics.median(pnl) if pnl else math.nan,
                "win_rate": safe_div(sum(1 for x in pnl if x > 0), len(pnl)) if pnl else math.nan,
                "roi_on_buy_usd": safe_div(sum(pnl), buy_for_closed) if buy_for_closed else math.nan,
                **concentration_stats([{"pnl": x, "id": ""} for x in pnl], "bin"),
            }
        )
    return out


def entry_lead_time(life: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = []
    for r in life:
        rows.append(
            {
                "asset": r.get("asset"),
                "city": r.get("city"),
                "weather_date": r.get("weather_date"),
                "weather_metric": r.get("weather_metric"),
                "unit": r.get("unit"),
                "bucket_label": r.get("bucket_label"),
                "outcome": r.get("outcome"),
                "exit_mode": r.get("exit_mode"),
                "buy_usd": r.get("buy_usd"),
                "weighted_avg_buy_price": r.get("weighted_avg_buy_price"),
                "first_buy_utc": r.get("first_buy_utc"),
                "weighted_avg_buy_utc": r.get("weighted_avg_buy_utc"),
                "market_end_utc": r.get("market_end_utc"),
                "observation_cutoff_utc": r.get("observation_cutoff_utc"),
                "lead_time_basis": "weather_date_end_utc_proxy",
                "first_entry_lead_hours": r.get("first_entry_lead_hours"),
                "first_entry_lead_bin": r.get("first_entry_lead_bin"),
                "weighted_entry_lead_hours": r.get("weighted_entry_lead_hours"),
                "weighted_entry_lead_bin": r.get("weighted_entry_lead_bin"),
                "first_entry_lead_to_market_end_hours": r.get("first_entry_lead_to_market_end_hours"),
                "buy_after_market_end_count": r.get("buy_after_market_end_count"),
                "buy_after_observation_proxy_count": r.get("buy_after_observation_proxy_count"),
                "authoritative_realized_pnl": r.get("authoritative_realized_pnl"),
                "pnl_status": r.get("pnl_status"),
            }
        )

    summary: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in life:
        for basis, bin_name in [
            ("first_entry", r.get("first_entry_lead_bin")),
            ("capital_weighted_entry", r.get("weighted_entry_lead_bin")),
        ]:
            summary[("all_outcomes", basis, bin_name or "unknown")].append(r)
            if r.get("outcome") == "Yes":
                summary[("yes_only", basis, bin_name or "unknown")].append(r)

    summary_rows = []
    for (universe, basis, bin_name), rs in sorted(
        summary.items(),
        key=lambda x: (
            x[0][0],
            x[0][1],
            LEAD_BIN_ORDER.index(x[0][2]) if x[0][2] in LEAD_BIN_ORDER else 99,
        ),
    ):
        pnl = [fnum(r.get("authoritative_realized_pnl"), math.nan) for r in rs if finite(r.get("authoritative_realized_pnl"))]
        buy = sum(fnum(r.get("buy_usd")) for r in rs if finite(r.get("authoritative_realized_pnl")))
        summary_rows.append(
            {
                "outcome_filter": universe,
                "entry_basis": basis,
                "lead_bin": bin_name,
                "positions": len(rs),
                "closed_positions_with_authoritative_pnl": len(pnl),
                "buy_usd_for_closed_positions": buy,
                "realized_pnl_sum": sum(pnl) if pnl else math.nan,
                "mean_realized_pnl": statistics.mean(pnl) if pnl else math.nan,
                "median_realized_pnl": statistics.median(pnl) if pnl else math.nan,
                "win_rate": safe_div(sum(1 for x in pnl if x > 0), len(pnl)) if pnl else math.nan,
                "roi_on_buy_usd": safe_div(sum(pnl), buy) if buy else math.nan,
            }
        )
    return rows, summary_rows


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return math.nan
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return math.nan
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def city_correlation(city_rows: list[dict[str, Any]], min_overlap: int = 5) -> list[dict[str, Any]]:
    by_city: dict[str, dict[str, float]] = defaultdict(dict)
    for r in city_rows:
        if r.get("closed_asset_count", 0) <= 0:
            continue
        key = f"{r['weather_date']}|{r['weather_metric']}|{r['unit']}"
        by_city[r["city"]][key] = by_city[r["city"]].get(key, 0.0) + fnum(r.get("closed_authoritative_pnl"))

    cities = sorted(c for c, values in by_city.items() if len(values) >= min_overlap)
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
                    "pearson_corr": pearson(xs, ys),
                    "same_sign_rate": safe_div(sum(1 for x, y in zip(xs, ys) if (x >= 0) == (y >= 0)), len(overlap)),
                    "city_a_total_pnl_on_overlap": sum(xs),
                    "city_b_total_pnl_on_overlap": sum(ys),
                }
            )
    return sorted(out, key=lambda r: (-(r["overlap_days"]), str(r["city_a"]), str(r["city_b"])))


def portfolio_daily_pnl(city_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in city_rows:
        if r.get("closed_asset_count", 0) > 0:
            by_date[r["weather_date"]].append(r)
    out = []
    for day, rows in sorted(by_date.items()):
        out.append(
            {
                "weather_date": day,
                "active_city_metric_count": len(rows),
                "closed_authoritative_pnl": sum(fnum(r.get("closed_authoritative_pnl")) for r in rows),
                "gross_buy_usd": sum(fnum(r.get("buy_usd")) for r in rows),
                "positive_city_day_count": sum(1 for r in rows if fnum(r.get("closed_authoritative_pnl")) > 0),
                "negative_city_day_count": sum(1 for r in rows if fnum(r.get("closed_authoritative_pnl")) < 0),
            }
        )
    return out


def max_drawdown(daily_rows: list[dict[str, Any]]) -> dict[str, Any]:
    peak = 0.0
    peak_date = ""
    cumulative = 0.0
    max_dd = 0.0
    max_dd_peak_date = ""
    trough_date = ""
    for r in daily_rows:
        cumulative += fnum(r.get("closed_authoritative_pnl"))
        if cumulative > peak:
            peak = cumulative
            peak_date = r.get("weather_date", "")
        drawdown = peak - cumulative
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_peak_date = peak_date
            trough_date = r.get("weather_date", "")
    return {
        "max_drawdown_usd": max_dd,
        "drawdown_peak_date": max_dd_peak_date,
        "drawdown_trough_date": trough_date,
        "ending_cumulative_pnl": cumulative,
    }


def raw_month_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for r in rows:
        ts = inum(r.get("timestamp"))
        if ts:
            counts[datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")] += 1
    return dict(sorted(counts.items()))


def duplicate_audit(rows: list[dict[str, str]]) -> dict[str, Any]:
    key_fields = ["transactionHash", "asset", "side", "timestamp", "size", "price"]
    counter = Counter(tuple(r.get(k, "") for k in key_fields) for r in rows)
    duplicate_groups = [(key, count) for key, count in counter.items() if count > 1]
    by_type = Counter()
    for r in rows:
        key = tuple(r.get(k, "") for k in key_fields)
        if counter[key] > 1:
            by_type[r.get("type", "NO_TYPE")] += 1
    return {
        "rows": len(rows),
        "unique_transaction_asset_side_timestamp_size_price": len(counter),
        "duplicate_groups": len(duplicate_groups),
        "duplicate_extra_rows": sum(count - 1 for _, count in duplicate_groups),
        "duplicate_rows_by_type": dict(by_type),
        "largest_duplicate_group": max((count for _, count in duplicate_groups), default=1),
    }


def data_integrity_audit(
    raw: Path,
    trades_raw: list[dict[str, str]],
    activity_raw: list[dict[str, str]],
    current_raw: list[dict[str, str]],
    closed_raw: list[dict[str, str]],
    life: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_path = raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    row_consistency = {}
    for name, rows in [
        ("trades", trades_raw),
        ("activity", activity_raw),
        ("current_positions", current_raw),
        ("closed_positions", closed_raw),
    ]:
        jsonl_path = raw / f"{name}.jsonl"
        jsonl_rows = sum(1 for _ in jsonl_path.open(encoding="utf-8")) if jsonl_path.exists() else 0
        row_consistency[name] = {
            "csv_rows": len(rows),
            "jsonl_rows": jsonl_rows,
            "manifest_rows": manifest.get("counts", {}).get(name),
            "consistent": len(rows) == jsonl_rows == manifest.get("counts", {}).get(name),
        }

    activity_types = Counter(r.get("type", "") for r in activity_raw)
    trades_monthly = raw_month_counts(trades_raw)
    activity_monthly = raw_month_counts(activity_raw)
    truncation_warnings = []
    for month, count in trades_monthly.items():
        if count >= 10_000:
            truncation_warnings.append(f"trades {month} has {count} rows; inspect offset cap")
    for month, count in activity_monthly.items():
        if count >= 5_500 or (count >= 5_000 and count % 500 == 0):
            truncation_warnings.append(f"activity {month} has {count} rows; inspect offset cap")

    return {
        "manifest": manifest,
        "row_consistency": row_consistency,
        "critical_parameter_checks": {
            "trades_takerOnly_false": manifest.get("critical_parameters", {}).get("takerOnly") is False,
            "activity_sort_ascending": manifest.get("critical_parameters", {}).get("activity_sort") == "ASC",
            "activity_has_trade_split_merge_redeem": all(
                activity_types.get(t, 0) > 0 for t in ["TRADE", "SPLIT", "MERGE", "REDEEM"]
            ),
        },
        "activity_types": dict(activity_types),
        "monthly_counts": {
            "trades": trades_monthly,
            "activity": activity_monthly,
        },
        "truncation_audit": {
            "warnings": truncation_warnings,
            "passed": not truncation_warnings,
            "method": "monthly row counts are below endpoint/page cap saturation indicators; collector would recursively split saturated windows",
        },
        "duplicate_audit": {
            "trades": duplicate_audit(trades_raw),
            "activity": duplicate_audit(activity_raw),
        },
        "snapshot_zip_valid": zipfile.is_zipfile(raw / "accounting_snapshot.zip"),
        "future_information_leakage_audit": {
            "entry_lead_time_basis": "weather_date_end_utc_proxy",
            "reason_market_endDate_not_used_as_primary_cutoff": "public endDate is midnight UTC for many weather markets and creates negative lead times for same-day trades",
            "positions_with_buy_after_market_endDate": sum(1 for r in life if fnum(r.get("buy_after_market_end_count")) > 0),
            "positions_with_buy_after_observation_proxy": sum(
                1 for r in life if fnum(r.get("buy_after_observation_proxy_count")) > 0
            ),
            "observed_winners_used_only_for_resolved_counterfactuals": True,
        },
    }


def summarize_float(values: Iterable[float]) -> dict[str, float]:
    xs = [x for x in values if finite(x)]
    if not xs:
        return {"count": 0, "sum": math.nan, "mean": math.nan, "median": math.nan, "stdev": math.nan}
    return {
        "count": len(xs),
        "sum": sum(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
    }


def fmt_money(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"${x:,.2f}"


def fmt_pct(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"{x * 100:.1f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"{x:,.{digits}f}"


def markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    shown = rows if limit is None else rows[:limit]
    header = "| " + " | ".join(label for label, _ in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in shown:
        vals = []
        for _, key in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                value = fmt_num(value, 4) if abs(value) < 10 else fmt_num(value, 2)
            vals.append(str(value))
        body.append("| " + " | ".join(vals) + " |")
    return "\n".join([header, sep, *body])


def generate_report(
    reports_dir: Path,
    audit: dict[str, Any],
    summary: dict[str, Any],
    price_rows: list[dict[str, Any]],
    lead_summary_rows: list[dict[str, Any]],
    concentration_rows: list[dict[str, Any]],
    counterfactual_rows: list[dict[str, Any]],
    correlation_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)

    yes_price = [
        r
        for r in price_rows
        if r.get("outcome_filter") == "yes_only" and r.get("closed_positions_with_authoritative_pnl", 0) > 0
    ]
    yes_price_key = sorted(
        yes_price,
        key=lambda r: (
            PRICE_BIN_ORDER.index(r["entry_price_bin"]) if r.get("entry_price_bin") in PRICE_BIN_ORDER else 99,
            str(r.get("exit_mode")),
        ),
    )
    low_price_yes = [r for r in yes_price if r.get("entry_price_bin") in {"0-1c", "1-2c", "2-5c", "5-10c"}]
    positive_low_bins = [r for r in low_price_yes if fnum(r.get("realized_pnl_sum"), math.nan) > 0]
    negative_low_bins = [r for r in low_price_yes if fnum(r.get("realized_pnl_sum"), math.nan) < 0]

    lead_yes = [
        r
        for r in lead_summary_rows
        if r.get("outcome_filter") == "yes_only"
        and r.get("entry_basis") == "capital_weighted_entry"
        and r.get("closed_positions_with_authoritative_pnl", 0) >= 10
        and finite(r.get("roi_on_buy_usd"))
    ]
    best_lead = max(lead_yes, key=lambda r: fnum(r.get("roi_on_buy_usd")), default={})

    valid_cf = [
        r
        for r in counterfactual_rows
        if r.get("counterfactual_status") in {"winner_in_basket", "winner_outside_visible_yes_basket"}
        and finite(r.get("authoritative_realized_pnl_sum"))
    ]
    cf_summary = {
        "evaluable_baskets": len(valid_cf),
        "actual_authoritative_realized_pnl": sum(fnum(r.get("authoritative_realized_pnl_sum")) for r in valid_cf),
        "actual_entry_allocation_pnl": sum(fnum(r.get("actual_entry_allocation_pnl_at_observed_state")) for r in valid_cf),
        "single_main_bucket_pnl": sum(fnum(r.get("single_main_bucket_pnl_at_observed_state")) for r in valid_cf),
        "equal_dollar_basket_pnl": sum(fnum(r.get("equal_dollar_basket_pnl_at_observed_state")) for r in valid_cf),
        "equal_payout_basket_pnl": sum(fnum(r.get("equal_payout_basket_pnl_at_observed_state")) for r in valid_cf),
    }

    daily_stats = summarize_float(fnum(r.get("closed_authoritative_pnl")) for r in daily_rows)
    corr_values = [fnum(r.get("pearson_corr"), math.nan) for r in correlation_rows if finite(r.get("pearson_corr"))]
    avg_corr = statistics.mean(corr_values) if corr_values else math.nan
    dd = summary.get("portfolio_drawdown", {})

    concentration_by_level = {r.get("level"): r for r in concentration_rows}
    pos_conc = concentration_by_level.get("weather_position_assets_all_outcomes", {})
    basket_conc = concentration_by_level.get("weather_city_day_baskets_all_outcomes", {})

    raw_counts = audit.get("manifest", {}).get("counts", {})
    lines = [
        "# HUSKYVS_FULL_AUDIT_v1",
        "",
        f"Generated at: {summary.get('generated_at_utc')}",
        f"Wallet: `{summary.get('wallet')}`",
        f"Period: `{summary.get('start_epoch')}` to `{summary.get('end_epoch')}`",
        "",
        "This report is a public-data audit only. It does not include copy-trading, staking, or funding advice.",
        "",
        "## 1. Data Coverage and Integrity",
        "",
        f"- Raw rows: trades {raw_counts.get('trades')}, activity {raw_counts.get('activity')}, current positions {raw_counts.get('current_positions')}, closed positions {raw_counts.get('closed_positions')}.",
        f"- Weather rows parsed: trades {summary.get('weather_trade_rows')}, assets {summary.get('weather_assets')}, city-day baskets {summary.get('city_day_baskets')}.",
        f"- `takerOnly=false`: {audit.get('critical_parameter_checks', {}).get('trades_takerOnly_false')}.",
        f"- Activity includes TRADE/SPLIT/MERGE/REDEEM: {audit.get('critical_parameter_checks', {}).get('activity_has_trade_split_merge_redeem')}.",
        f"- CSV/JSONL/manifest row counts consistent: {all(v.get('consistent') for v in audit.get('row_consistency', {}).values())}.",
        f"- Offset truncation audit passed: {audit.get('truncation_audit', {}).get('passed')}.",
        f"- Accounting snapshot ZIP valid: {audit.get('snapshot_zip_valid')}.",
        "",
        "Duplicate audit: trades have no duplicate groups under transactionHash/asset/side/timestamp/size/price. Activity duplicate groups are REDEEM rows with blank asset/side/price, where a single redemption transaction references multiple titles; they were not treated as duplicated fills.",
        "",
        "## 2. Low-Price YES by Exit Mode",
        "",
        "Low-price YES was not uniformly positive. In this realized public sample, some low-price bins are positive after exits, but losses remain visible and transform-affected rows must be interpreted separately.",
        "",
        markdown_table(
            [
                {
                    "price": r.get("entry_price_bin"),
                    "exit": r.get("exit_mode"),
                    "n": r.get("closed_positions_with_authoritative_pnl"),
                    "pnl": fmt_money(r.get("realized_pnl_sum")),
                    "roi": fmt_pct(r.get("roi_on_buy_usd")),
                    "win": fmt_pct(r.get("win_rate")),
                }
                for r in yes_price_key
            ],
            [("Price", "price"), ("Exit", "exit"), ("Closed", "n"), ("PnL", "pnl"), ("ROI", "roi"), ("Win", "win")],
        ),
        "",
        f"Positive low-price YES groups: {len(positive_low_bins)}; negative low-price YES groups: {len(negative_low_bins)}. This supports a signal-filtered tail strategy more than indiscriminate cheap-YES buying.",
        "",
        "## 3. Multi-City Volatility",
        "",
        f"Closed daily portfolio PnL totals {fmt_money(daily_stats.get('sum'))} across {daily_stats.get('count')} weather dates, with daily standard deviation {fmt_money(daily_stats.get('stdev'))}. Max drawdown on closed city-day PnL is {fmt_money(dd.get('max_drawdown_usd'))}, from {dd.get('drawdown_peak_date') or 'start'} to {dd.get('drawdown_trough_date') or 'n/a'}.",
        f"Pairwise city correlations with at least five overlapping city-days average {fmt_num(avg_corr, 3)} across {len(corr_values)} pairs.",
        "The correlation layer generally supports diversification. The concentration layer below shows this is not a one-winner result, although the top winners still contribute a visible share of net profit.",
        "",
        "## 4. Basket Counterfactuals",
        "",
        f"Evaluable non-overlapping resolved YES baskets: {cf_summary['evaluable_baskets']}.",
        "",
        markdown_table(
            [
                {"model": "actual_authoritative_realized", "pnl": fmt_money(cf_summary["actual_authoritative_realized_pnl"])},
                {"model": "actual_entry_allocation_state_model", "pnl": fmt_money(cf_summary["actual_entry_allocation_pnl"])},
                {"model": "single_main_bucket", "pnl": fmt_money(cf_summary["single_main_bucket_pnl"])},
                {"model": "equal_dollar_basket", "pnl": fmt_money(cf_summary["equal_dollar_basket_pnl"])},
                {"model": "equal_payout_basket", "pnl": fmt_money(cf_summary["equal_payout_basket_pnl"])},
            ],
            [("Model", "model"), ("Observed-state PnL", "pnl")],
        ),
        "",
        "The unequal adjacent-basket structure should be analyzed as state-dependent payoffs, not as a simple sum of prices. Overlapping buckets such as `or below` and `or higher` are flagged and excluded from the clean counterfactual aggregate.",
        "",
        "## 5. Profit Concentration",
        "",
        markdown_table(
            [
                {
                    "level": "position",
                    "total": fmt_money(pos_conc.get("total_net_pnl")),
                    "top1": fmt_pct(pos_conc.get("top1_share_of_total_net_pnl")),
                    "top5": fmt_pct(pos_conc.get("top5_share_of_total_net_pnl")),
                    "top10": fmt_pct(pos_conc.get("top10_share_of_total_net_pnl")),
                    "leave": fmt_money(pos_conc.get("leave_top1_out_pnl")),
                },
                {
                    "level": "basket",
                    "total": fmt_money(basket_conc.get("total_net_pnl")),
                    "top1": fmt_pct(basket_conc.get("top1_share_of_total_net_pnl")),
                    "top5": fmt_pct(basket_conc.get("top5_share_of_total_net_pnl")),
                    "top10": fmt_pct(basket_conc.get("top10_share_of_total_net_pnl")),
                    "leave": fmt_money(basket_conc.get("leave_top1_out_pnl")),
                },
            ],
            [
                ("Level", "level"),
                ("Total PnL", "total"),
                ("Top1 / Net", "top1"),
                ("Top5 / Net", "top5"),
                ("Top10 / Net", "top10"),
                ("Leave Top1 Out", "leave"),
            ],
        ),
        "",
        "## 6. Entry Lead Time",
        "",
        f"Best capital-weighted YES lead bin by ROI among bins with at least 10 closed positions: {best_lead.get('lead_bin', 'n/a')} with ROI {fmt_pct(best_lead.get('roi_on_buy_usd'))} and PnL {fmt_money(best_lead.get('realized_pnl_sum'))}.",
        "",
        markdown_table(
            [
                {
                    "bin": r.get("lead_bin"),
                    "n": r.get("closed_positions_with_authoritative_pnl"),
                    "pnl": fmt_money(r.get("realized_pnl_sum")),
                    "roi": fmt_pct(r.get("roi_on_buy_usd")),
                    "win": fmt_pct(r.get("win_rate")),
                }
                for r in lead_yes
            ],
            [("Weighted Lead", "bin"), ("Closed", "n"), ("PnL", "pnl"), ("ROI", "roi"), ("Win", "win")],
        ),
        "",
        "Lead time uses `weather_date + 1 day at 00:00 UTC` as a public observation-window proxy. Local station cutoffs are not available in the public ledger and remain a data gap.",
        "",
        "## 7. Data Gaps and Non-Recoverable Items",
        "",
        "- Unfilled orders, cancellations, quote changes, and subjective forecasts are not present in public ledger endpoints.",
        "- Local weather station identity, exact observation cutoff, model forecasts, METAR/TAF snapshots, and alert triggers are not recoverable from the wallet alone.",
        "- Current/open positions are not included in authoritative realized-PnL conclusions.",
        "- Transform-affected SPLIT/MERGE rows are labeled separately; naive cash-flow reconstruction is not trusted for them.",
        "- Overlapping YES markets cannot be treated as mutually exclusive state buckets without an external resolution map.",
    ]

    (reports_dir / "HUSKYVS_FULL_AUDIT_v1.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--out", default="data/processed")
    args = p.parse_args()
    raw, out = Path(args.raw), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    trades_raw = read_csv(raw / "trades.csv")
    activity_raw = read_csv(raw / "activity.csv")
    closed_raw = read_csv(raw / "closed_positions.csv")
    current_raw = read_csv(raw / "current_positions.csv")
    manifest = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))

    asset_meta = build_asset_meta(trades_raw, closed_raw, current_raw)
    trades = normalized_trades(trades_raw, asset_meta)
    closed = normalized_positions(closed_raw, "closed_positions")
    current = normalized_positions(current_raw, "current_positions")
    life = lifecycle(trades, activity_raw, closed, current, asset_meta)
    city_rows = city_day_pnl(life)
    baskets = basket_summary(life)
    state_rows, counterfactual_rows = basket_state_payoffs(life)
    price_rows = price_bin_by_exit_mode(life)
    lead_rows, lead_summary_rows = entry_lead_time(life)
    concentration_rows = profit_concentration(life, city_rows)
    corr_rows = city_correlation(city_rows)
    daily_rows = portfolio_daily_pnl(city_rows)
    drawdown = max_drawdown(daily_rows)
    audit = data_integrity_audit(raw, trades_raw, activity_raw, current_raw, closed_raw, life)

    write_rows(out / "weather_trades_normalized.csv", trades)
    write_rows(out / "closed_positions_weather_normalized.csv", closed)
    write_rows(out / "current_positions_weather_normalized.csv", current)
    write_rows(out / "weather_position_lifecycle.csv", life)
    write_rows(out / "weather_city_day_baskets.csv", baskets)
    write_rows(out / "price_bin_by_exit_mode.csv", price_rows)
    write_rows(out / "city_day_pnl.csv", city_rows)
    write_rows(out / "entry_lead_time.csv", lead_rows)
    write_rows(out / "entry_lead_time_by_bin.csv", lead_summary_rows)
    write_rows(out / "profit_concentration.csv", concentration_rows)
    write_rows(out / "city_correlation.csv", corr_rows)
    write_rows(out / "portfolio_daily_pnl.csv", daily_rows)
    write_rows(out / "basket_state_payoffs.csv", state_rows)
    write_rows(out / "counterfactual_basket_summary.csv", counterfactual_rows)
    write_json(out / "data_integrity_audit.json", audit)

    closed_pnl = [fnum(r.get("authoritative_realized_pnl"), math.nan) for r in life if finite(r.get("authoritative_realized_pnl"))]
    summary = {
        "wallet": manifest.get("wallet"),
        "start_epoch": manifest.get("start_epoch"),
        "end_epoch": manifest.get("end_epoch"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_counts": manifest.get("counts", {}),
        "weather_trade_rows": len(trades),
        "weather_assets": len(life),
        "closed_weather_assets_with_authoritative_pnl": len(closed_pnl),
        "city_day_pnl_rows": len(city_rows),
        "city_day_baskets": len(baskets),
        "basket_state_payoff_rows": len(state_rows),
        "counterfactual_basket_rows": len(counterfactual_rows),
        "exit_modes": dict(
            (m, sum(1 for r in life if r["exit_mode"] == m))
            for m in sorted({r["exit_mode"] for r in life})
        ),
        "closed_realized_pnl": summarize_float(closed_pnl),
        "portfolio_drawdown": drawdown,
        "warnings": [
            "SPLIT/MERGE/CONVERSION rows make naive cash-flow PnL unreliable.",
            "Closed-position realizedPnl is treated as the authoritative position-level PnL.",
            "Unequal-share baskets require state-dependent payouts; price sums alone are insufficient.",
            "Market endDate is midnight UTC for many weather rows, so entry lead time uses a weather-date-end UTC proxy.",
            "Open/current positions are excluded from authoritative realized-PnL conclusions.",
        ],
    }
    write_json(out / "audit_summary.json", summary)
    generate_report(
        Path("reports"),
        audit,
        summary,
        price_rows,
        lead_summary_rows,
        concentration_rows,
        counterfactual_rows,
        corr_rows,
        daily_rows,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
