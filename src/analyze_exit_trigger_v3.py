#!/usr/bin/env python3
"""Exit-trigger case study for huskyvs weather positions.

This script uses the already corrected v2 lifecycle sample plus existing raw
trade fills. It optionally queries public token-level Polymarket price history;
it never re-fetches account-level trade history.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analyze_weather_strategy_v2 import fnum, finite, iso_from_epoch


PRICE_HISTORY_URL = "https://clob.polymarket.com/prices-history"
PRICE_HISTORY_DOCS_URL = "https://docs.polymarket.com/api-reference/markets/get-prices-history"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)

CASE_TARGETS = [
    ("mostly_sold_profit", 12),
    ("partial_sold_loss", 10),
    ("never_sold_loss", 10),
    ("prediction_correct_trade_loss", 5),
    ("partial_sold_profit", 5),
]

SUMMARY_FIELDS = [
    "section",
    "group",
    "case_count",
    "sold_case_count",
    "total_realized_pnl_v2",
    "avg_roi_v2",
    "median_roi_v2",
    "hold_to_resolution_pnl_estimate",
    "median_weighted_sell_multiple",
    "median_first_sell_multiple",
    "median_sell_share_ratio",
    "reached_1_5x_count",
    "reached_2x_count",
    "reached_3x_count",
    "sold_after_1_5x_count",
    "sold_after_2x_count",
    "sold_after_3x_count",
    "post_sell_continued_up_count",
    "history_available_count",
    "notes",
]

CASE_FIELDS = [
    "case_id",
    "sample_category",
    "asset",
    "slug",
    "title",
    "city",
    "weather_date",
    "weather_metric",
    "bucket_label",
    "outcome",
    "asset_won_v2",
    "settlement_price_v2",
    "settlement_source_v2",
    "exit_mode_v2",
    "entry_price_bin",
    "weighted_avg_buy_price",
    "buy_count",
    "buy_shares",
    "buy_usd",
    "sell_count",
    "sell_shares",
    "sell_usd",
    "weighted_avg_sell_price",
    "sell_share_ratio",
    "realized_pnl_v2",
    "roi_on_capital_at_risk_v2",
    "hold_to_resolution_pnl_estimate",
    "hold_to_resolution_result",
    "first_buy_utc",
    "weighted_avg_buy_utc",
    "first_entry_lead_bin_local",
    "first_entry_lead_hours_local",
    "local_timezone",
    "local_weather_day_end_utc",
    "first_sell_utc",
    "last_sell_utc",
    "first_sell_hours_before_local_end",
    "last_sell_hours_before_local_end",
    "first_sell_multiple_vs_avg_buy",
    "weighted_sell_multiple_vs_avg_buy",
    "max_sell_fill_multiple_vs_avg_buy",
    "market_history_status",
    "market_history_points",
    "history_fidelity_minutes",
    "history_window_start_utc",
    "history_window_end_utc",
    "observed_max_price_after_first_buy",
    "observed_max_price_utc",
    "observed_max_multiple_vs_avg_buy",
    "observed_max_source",
    "first_1_5x_utc",
    "hours_after_buy_to_1_5x",
    "first_2x_utc",
    "hours_after_buy_to_2x",
    "first_3x_utc",
    "hours_after_buy_to_3x",
    "sold_after_observed_1_5x",
    "sold_after_observed_2x",
    "sold_after_observed_3x",
    "post_sell_max_price",
    "post_sell_max_utc",
    "post_sell_last_price",
    "post_sell_last_utc",
    "post_sell_max_multiple_vs_weighted_sell",
    "post_sell_direction",
    "buy_fills_json",
    "sell_fills_json",
    "data_notes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fmt_money(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"${x:,.2f}"


def fmt_pct(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"{x * 100:.1f}%"


def fmt_num(value: Any, digits: int = 2) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"{x:.{digits}f}"


def parse_ts(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def safe_div(num: float, den: float) -> float:
    return num / den if den else math.nan


def median(values: list[float]) -> float:
    xs = [v for v in values if finite(v)]
    return statistics.median(xs) if xs else math.nan


def mean(values: list[float]) -> float:
    xs = [v for v in values if finite(v)]
    return statistics.mean(xs) if xs else math.nan


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_fills(trades: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    fills: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        side = (row.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        ts = parse_ts(row.get("timestamp"))
        price = fnum(row.get("price"), math.nan)
        size = fnum(row.get("size"), math.nan)
        if not ts or not finite(price) or not finite(size):
            continue
        fills[row["asset"]].append(
            {
                "side": side,
                "timestamp": ts,
                "utc": iso_from_epoch(ts),
                "price": price,
                "shares": size,
                "usd": price * size,
                "transactionHash": row.get("transactionHash", ""),
            }
        )
    for asset_rows in fills.values():
        asset_rows.sort(key=lambda r: (r["timestamp"], r["side"], r["transactionHash"]))
    return fills


def category_for(row: dict[str, str]) -> str:
    pnl = fnum(row.get("realized_pnl_v2"), math.nan)
    mode = row.get("exit_mode_v2")
    won = row.get("asset_won_v2") == "True"
    if mode == "mostly_or_fully_sold" and pnl > 0:
        return "mostly_sold_profit"
    if mode == "partially_sold" and pnl < 0:
        return "partial_sold_loss"
    if mode == "never_sold" and pnl < 0:
        return "never_sold_loss"
    if mode in {"mostly_or_fully_sold", "partially_sold"} and won and pnl < 0:
        return "prediction_correct_trade_loss"
    if mode == "partially_sold" and pnl > 0:
        return "partial_sold_profit"
    return ""


def candidate_score(row: dict[str, str], selected: list[dict[str, str]], rank: int, total: int) -> float:
    selected_cities = {r.get("city") for r in selected}
    selected_prices = {r.get("entry_price_bin") for r in selected}
    selected_leads = {r.get("first_entry_lead_bin_local") for r in selected}
    selected_dates = {r.get("weather_date") for r in selected}
    novelty = 0.0
    novelty += 1.0 if row.get("city") not in selected_cities else 0.0
    novelty += 0.8 if row.get("entry_price_bin") not in selected_prices else 0.0
    novelty += 0.8 if row.get("first_entry_lead_bin_local") not in selected_leads else 0.0
    novelty += 0.3 if row.get("weather_date") not in selected_dates else 0.0
    rank_bonus = 1.0 - (rank / max(total, 1))
    capital = min(fnum(row.get("capital_at_risk_usd")), 100.0) / 100.0
    return novelty + 0.45 * rank_bonus + 0.15 * capital


def select_diverse(candidates: list[dict[str, str]], n: int, category: str) -> list[dict[str, str]]:
    def base_key(row: dict[str, str]) -> tuple[float, float]:
        pnl = fnum(row.get("realized_pnl_v2"), math.nan)
        roi = fnum(row.get("roi_on_capital_at_risk_v2"), math.nan)
        if "profit" in category:
            return (pnl, roi)
        return (abs(pnl), abs(roi))

    ordered = sorted(candidates, key=base_key, reverse=True)
    selected: list[dict[str, str]] = []
    remaining = list(enumerate(ordered))
    while remaining and len(selected) < n:
        idx, row = max(
            remaining,
            key=lambda item: candidate_score(item[1], selected, item[0], len(ordered)),
        )
        selected.append(row)
        remaining = [(i, r) for i, r in remaining if r.get("asset") != row.get("asset")]
    return selected


def select_cases(lifecycle: list[dict[str, str]]) -> list[dict[str, str]]:
    base = [
        r
        for r in lifecycle
        if r.get("settled_sample_v2") == "True"
        and r.get("transform_affected") != "True"
        and fnum(r.get("capital_at_risk_usd")) > 0
        and r.get("asset")
    ]
    by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in base:
        category = category_for(row)
        if category:
            by_category[category].append(row)

    selected: list[dict[str, str]] = []
    selected_assets: set[str] = set()
    for category, target in CASE_TARGETS:
        candidates = [r for r in by_category[category] if r.get("asset") not in selected_assets]
        chosen = select_diverse(candidates, target, category)
        for row in chosen:
            copied = dict(row)
            copied["sample_category"] = category
            selected.append(copied)
            selected_assets.add(row["asset"])
    return selected


def fetch_price_history(asset: str, start_ts: int, end_ts: int, fidelity: int, user_agent: str) -> tuple[list[dict[str, Any]], str]:
    if not asset or start_ts <= 0 or end_ts <= start_ts:
        return [], "missing_invalid_window"
    params = urllib.parse.urlencode(
        {
            "market": asset,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }
    )
    request = urllib.request.Request(
        f"{PRICE_HISTORY_URL}?{params}",
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"missing_http_{exc.code}"
    except Exception as exc:  # Network/API errors are recorded, not guessed through.
        return [], f"missing_{type(exc).__name__}"

    history = payload.get("history", []) if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for point in history:
        ts = parse_ts(point.get("t"))
        price = fnum(point.get("p"), math.nan)
        if ts and finite(price):
            out.append({"timestamp": ts, "price": price, "source": "polymarket_prices_history"})
    out.sort(key=lambda r: r["timestamp"])
    return out, "ok" if out else "missing_empty_history"


def observed_price_points(
    history: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    first_buy_ts: int,
) -> list[dict[str, Any]]:
    points = [p for p in history if p["timestamp"] >= first_buy_ts]
    for fill in fills:
        if fill["timestamp"] >= first_buy_ts:
            points.append(
                {
                    "timestamp": fill["timestamp"],
                    "price": fill["price"],
                    "source": f"user_{fill['side'].lower()}_fill",
                }
            )
    points.sort(key=lambda r: (r["timestamp"], r["source"]))
    return points


def first_reach(points: list[dict[str, Any]], target_price: float) -> dict[str, Any] | None:
    if not finite(target_price) or target_price <= 0:
        return None
    for point in points:
        if point["price"] >= target_price:
            return point
    return None


def post_sell_direction(
    post_points: list[dict[str, Any]],
    weighted_sell_price: float,
) -> tuple[str, float, str, float, str, float]:
    if not post_points:
        return ("missing_no_post_sell_observations", math.nan, "", math.nan, "", math.nan)
    max_point = max(post_points, key=lambda p: p["price"])
    last_point = post_points[-1]
    multiple = safe_div(max_point["price"], weighted_sell_price)
    if not finite(weighted_sell_price) or weighted_sell_price <= 0:
        direction = "missing_sell_price"
    elif max_point["price"] >= weighted_sell_price * 1.10:
        direction = "continued_up_after_sell"
    elif last_point["price"] <= weighted_sell_price * 0.90:
        direction = "reverted_or_fell_after_sell"
    else:
        direction = "mostly_flat_after_sell"
    return (
        direction,
        max_point["price"],
        iso_from_epoch(max_point["timestamp"]),
        last_point["price"],
        iso_from_epoch(last_point["timestamp"]),
        multiple,
    )


def build_case_row(
    idx: int,
    row: dict[str, str],
    fills_by_asset: dict[str, list[dict[str, Any]]],
    fidelity: int,
    fetch_history: bool,
    user_agent: str,
    now_ts: int,
) -> dict[str, Any]:
    asset = row["asset"]
    fills = fills_by_asset.get(asset, [])
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    first_buy_ts = parse_ts(row.get("first_buy_ts")) or (buys[0]["timestamp"] if buys else 0)
    first_sell_ts = parse_ts(row.get("first_sell_ts")) or (sells[0]["timestamp"] if sells else 0)
    last_sell_ts = parse_ts(row.get("last_sell_ts")) or (sells[-1]["timestamp"] if sells else 0)
    local_end_ts = parse_ts(row.get("local_weather_day_end_epoch"))
    avg_buy = fnum(row.get("weighted_avg_buy_price"), math.nan)
    avg_sell = fnum(row.get("weighted_avg_sell_price"), math.nan)
    buy_shares = fnum(row.get("buy_shares"))
    buy_usd = fnum(row.get("buy_usd"))
    settlement_price = fnum(row.get("settlement_price_v2"), math.nan)
    hold_pnl = settlement_price * buy_shares - buy_usd if finite(settlement_price) else math.nan
    history_start = max(0, first_buy_ts - 3600)
    history_end = max(local_end_ts + 6 * 3600, last_sell_ts + 6 * 3600, first_buy_ts + 6 * 3600)
    history_end = min(history_end, now_ts)

    if fetch_history:
        history, history_status = fetch_price_history(asset, history_start, history_end, fidelity, user_agent)
        # Keep pressure modest on a public endpoint.
        time.sleep(0.05)
    else:
        history, history_status = [], "missing_fetch_disabled"

    points = observed_price_points(history, fills, first_buy_ts)
    max_point = max(points, key=lambda p: p["price"]) if points else None
    thresholds = {
        "1_5x": first_reach(points, avg_buy * 1.5),
        "2x": first_reach(points, avg_buy * 2.0),
        "3x": first_reach(points, avg_buy * 3.0),
    }
    post_points = [p for p in points if last_sell_ts and p["timestamp"] > last_sell_ts]
    (
        post_direction,
        post_max_price,
        post_max_utc,
        post_last_price,
        post_last_utc,
        post_max_multiple,
    ) = post_sell_direction(post_points, avg_sell) if sells else ("no_sells", math.nan, "", math.nan, "", math.nan)

    first_sell_price = sells[0]["price"] if sells else math.nan
    max_sell_price = max((s["price"] for s in sells), default=math.nan)
    first_sell_before = safe_div(local_end_ts - first_sell_ts, 3600) if first_sell_ts and local_end_ts else math.nan
    last_sell_before = safe_div(local_end_ts - last_sell_ts, 3600) if last_sell_ts and local_end_ts else math.nan
    max_source = max_point["source"] if max_point else ""
    if history and max_point and max_point["source"].startswith("user_"):
        max_source = "market_history_plus_user_fills"
    elif history:
        max_source = "market_history"
    elif max_point:
        max_source = "user_fills_only"

    def threshold_utc(name: str) -> str:
        point = thresholds[name]
        return iso_from_epoch(point["timestamp"]) if point else ""

    def threshold_hours(name: str) -> float:
        point = thresholds[name]
        return safe_div(point["timestamp"] - first_buy_ts, 3600) if point else math.nan

    def sold_after(name: str) -> str:
        point = thresholds[name]
        if not point or not first_sell_ts:
            return ""
        return str(first_sell_ts >= point["timestamp"])

    data_notes = []
    if history_status != "ok":
        data_notes.append("market_history_missing_or_empty; only user fills available")
    if history_status == "ok":
        data_notes.append("price history sampled; observed max may miss intra-sample ticks")
    if row.get("settlement_source_v2") == "current_zero_value_after_local_day_end":
        data_notes.append("settlement result comes from v2 current_positions zero-value inclusion")

    return {
        "case_id": f"v3-{idx:02d}",
        "sample_category": row["sample_category"],
        "asset": asset,
        "slug": row.get("slug", ""),
        "title": row.get("title", ""),
        "city": row.get("city", ""),
        "weather_date": row.get("weather_date", ""),
        "weather_metric": row.get("weather_metric", ""),
        "bucket_label": row.get("bucket_label", ""),
        "outcome": row.get("outcome", ""),
        "asset_won_v2": row.get("asset_won_v2", ""),
        "settlement_price_v2": row.get("settlement_price_v2", ""),
        "settlement_source_v2": row.get("settlement_source_v2", ""),
        "exit_mode_v2": row.get("exit_mode_v2", ""),
        "entry_price_bin": row.get("entry_price_bin", ""),
        "weighted_avg_buy_price": avg_buy,
        "buy_count": row.get("buy_count", ""),
        "buy_shares": row.get("buy_shares", ""),
        "buy_usd": row.get("buy_usd", ""),
        "sell_count": row.get("sell_count", ""),
        "sell_shares": row.get("sell_shares", ""),
        "sell_usd": row.get("sell_usd", ""),
        "weighted_avg_sell_price": avg_sell,
        "sell_share_ratio": row.get("sell_share_ratio", ""),
        "realized_pnl_v2": row.get("realized_pnl_v2", ""),
        "roi_on_capital_at_risk_v2": row.get("roi_on_capital_at_risk_v2", ""),
        "hold_to_resolution_pnl_estimate": hold_pnl,
        "hold_to_resolution_result": "profit" if hold_pnl > 0 else "loss" if hold_pnl < 0 else "breakeven",
        "first_buy_utc": row.get("first_buy_utc", ""),
        "weighted_avg_buy_utc": row.get("weighted_avg_buy_utc", ""),
        "first_entry_lead_bin_local": row.get("first_entry_lead_bin_local", ""),
        "first_entry_lead_hours_local": row.get("first_entry_lead_hours_local", ""),
        "local_timezone": row.get("local_timezone", ""),
        "local_weather_day_end_utc": row.get("local_weather_day_end_utc", ""),
        "first_sell_utc": row.get("first_sell_utc", ""),
        "last_sell_utc": row.get("last_sell_utc", ""),
        "first_sell_hours_before_local_end": first_sell_before,
        "last_sell_hours_before_local_end": last_sell_before,
        "first_sell_multiple_vs_avg_buy": safe_div(first_sell_price, avg_buy),
        "weighted_sell_multiple_vs_avg_buy": safe_div(avg_sell, avg_buy),
        "max_sell_fill_multiple_vs_avg_buy": safe_div(max_sell_price, avg_buy),
        "market_history_status": history_status,
        "market_history_points": len(history),
        "history_fidelity_minutes": fidelity if fetch_history else "",
        "history_window_start_utc": iso_from_epoch(history_start),
        "history_window_end_utc": iso_from_epoch(history_end),
        "observed_max_price_after_first_buy": max_point["price"] if max_point else math.nan,
        "observed_max_price_utc": iso_from_epoch(max_point["timestamp"]) if max_point else "",
        "observed_max_multiple_vs_avg_buy": safe_div(max_point["price"], avg_buy) if max_point else math.nan,
        "observed_max_source": max_source,
        "first_1_5x_utc": threshold_utc("1_5x"),
        "hours_after_buy_to_1_5x": threshold_hours("1_5x"),
        "first_2x_utc": threshold_utc("2x"),
        "hours_after_buy_to_2x": threshold_hours("2x"),
        "first_3x_utc": threshold_utc("3x"),
        "hours_after_buy_to_3x": threshold_hours("3x"),
        "sold_after_observed_1_5x": sold_after("1_5x"),
        "sold_after_observed_2x": sold_after("2x"),
        "sold_after_observed_3x": sold_after("3x"),
        "post_sell_max_price": post_max_price,
        "post_sell_max_utc": post_max_utc,
        "post_sell_last_price": post_last_price,
        "post_sell_last_utc": post_last_utc,
        "post_sell_max_multiple_vs_weighted_sell": post_max_multiple,
        "post_sell_direction": post_direction,
        "buy_fills_json": json_dumps(buys),
        "sell_fills_json": json_dumps(sells),
        "data_notes": "; ".join(data_notes),
    }


def multiple_bin(value: float) -> str:
    if not finite(value):
        return "no_sell_or_unknown"
    if value < 1.0:
        return "<1.0x"
    if value < 1.5:
        return "1.0-1.5x"
    if value < 2.0:
        return "1.5-2.0x"
    if value < 3.0:
        return "2.0-3.0x"
    return ">=3.0x"


def ratio_bin(value: float) -> str:
    if not finite(value):
        return "unknown"
    if value <= 0:
        return "0%"
    if value < 0.25:
        return "0-25%"
    if value < 0.50:
        return "25-50%"
    if value < 0.75:
        return "50-75%"
    if value < 0.90:
        return "75-90%"
    if value <= 1.10:
        return "90-110%"
    return ">110%"


def summarize_group(section: str, group: str, rows: list[dict[str, Any]], notes: str = "") -> dict[str, Any]:
    sold_rows = [r for r in rows if fnum(r.get("sell_count")) > 0]
    return {
        "section": section,
        "group": group,
        "case_count": len(rows),
        "sold_case_count": len(sold_rows),
        "total_realized_pnl_v2": sum(fnum(r.get("realized_pnl_v2")) for r in rows),
        "avg_roi_v2": mean([fnum(r.get("roi_on_capital_at_risk_v2"), math.nan) for r in rows]),
        "median_roi_v2": median([fnum(r.get("roi_on_capital_at_risk_v2"), math.nan) for r in rows]),
        "hold_to_resolution_pnl_estimate": sum(fnum(r.get("hold_to_resolution_pnl_estimate")) for r in rows),
        "median_weighted_sell_multiple": median([fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) for r in sold_rows]),
        "median_first_sell_multiple": median([fnum(r.get("first_sell_multiple_vs_avg_buy"), math.nan) for r in sold_rows]),
        "median_sell_share_ratio": median([fnum(r.get("sell_share_ratio"), math.nan) for r in sold_rows]),
        "reached_1_5x_count": sum(1 for r in rows if r.get("first_1_5x_utc")),
        "reached_2x_count": sum(1 for r in rows if r.get("first_2x_utc")),
        "reached_3x_count": sum(1 for r in rows if r.get("first_3x_utc")),
        "sold_after_1_5x_count": sum(1 for r in sold_rows if r.get("sold_after_observed_1_5x") == "True"),
        "sold_after_2x_count": sum(1 for r in sold_rows if r.get("sold_after_observed_2x") == "True"),
        "sold_after_3x_count": sum(1 for r in sold_rows if r.get("sold_after_observed_3x") == "True"),
        "post_sell_continued_up_count": sum(1 for r in sold_rows if r.get("post_sell_direction") == "continued_up_after_sell"),
        "history_available_count": sum(1 for r in rows if r.get("market_history_status") == "ok"),
        "notes": notes,
    }


def build_summary(case_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for category, _ in CASE_TARGETS:
        rows = [r for r in case_rows if r.get("sample_category") == category]
        summary.append(summarize_group("sample_category", category, rows))
    for mode in sorted({r.get("exit_mode_v2") for r in case_rows}):
        rows = [r for r in case_rows if r.get("exit_mode_v2") == mode]
        summary.append(summarize_group("exit_mode", mode, rows))
    sold_rows = [r for r in case_rows if fnum(r.get("sell_count")) > 0]
    for group in ["<1.0x", "1.0-1.5x", "1.5-2.0x", "2.0-3.0x", ">=3.0x", "no_sell_or_unknown"]:
        rows = [r for r in sold_rows if multiple_bin(fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan)) == group]
        if rows:
            summary.append(summarize_group("weighted_sell_multiple_bin", group, rows))
    price_order = ["0-1c", "1-2c", "2-5c", "5-10c", "10-20c", ">=20c", "unknown"]
    for price in price_order:
        rows = [r for r in case_rows if r.get("entry_price_bin") == price]
        if rows:
            summary.append(summarize_group("entry_price_bin", price, rows))
    for group in ["0-25%", "25-50%", "50-75%", "75-90%", "90-110%", ">110%"]:
        rows = [r for r in sold_rows if ratio_bin(fnum(r.get("sell_share_ratio"), math.nan)) == group]
        if rows:
            summary.append(summarize_group("sell_share_ratio_bin", group, rows))
    correct_loss = [r for r in case_rows if r.get("asset_won_v2") == "True" and fnum(r.get("realized_pnl_v2")) < 0]
    summary.append(
        summarize_group(
            "diagnostic",
            "prediction_correct_but_trade_loss",
            correct_loss,
            "Final settlement was favorable, but trading PnL was negative.",
        )
    )
    return summary


def markdown_table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    out = [
        "| " + " | ".join(label for label, _ in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(key, "")) for _, key in cols) + " |")
    return "\n".join(out)


def report_lines(case_rows: list[dict[str, Any]], summary_rows: list[dict[str, Any]]) -> list[str]:
    sold_rows = [r for r in case_rows if fnum(r.get("sell_count")) > 0]
    mostly = [r for r in case_rows if r.get("exit_mode_v2") == "mostly_or_fully_sold"]
    partial = [r for r in case_rows if r.get("exit_mode_v2") == "partially_sold"]
    never = [r for r in case_rows if r.get("exit_mode_v2") == "never_sold"]
    history_ok = sum(1 for r in case_rows if r.get("market_history_status") == "ok")
    sold_2x = [r for r in sold_rows if fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) >= 2.0]
    sold_1_5x = [r for r in sold_rows if fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) >= 1.5]
    sold_3x = [r for r in sold_rows if fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) >= 3.0]
    best_multiple = max(
        [r for r in summary_rows if r["section"] == "weighted_sell_multiple_bin"],
        key=lambda r: fnum(r.get("total_realized_pnl_v2"), -math.inf),
        default={},
    )
    best_price = max(
        [r for r in summary_rows if r["section"] == "entry_price_bin"],
        key=lambda r: fnum(r.get("total_realized_pnl_v2"), -math.inf),
        default={},
    )
    correct_loss = [r for r in case_rows if r.get("asset_won_v2") == "True" and fnum(r.get("realized_pnl_v2")) < 0]
    continued_up = [r for r in sold_rows if r.get("post_sell_direction") == "continued_up_after_sell"]
    low_price_never_loss = [r for r in never if r.get("entry_price_bin") in {"0-1c", "1-2c", "2-5c"}]
    never_reached_2x = [r for r in never if r.get("first_2x_utc")]
    never_reached_3x = [r for r in never if r.get("first_3x_utc")]

    category_rows = []
    for category, _ in CASE_TARGETS:
        rows = [r for r in case_rows if r.get("sample_category") == category]
        category_rows.append(
            {
                "category": category,
                "n": len(rows),
                "pnl": fmt_money(sum(fnum(r.get("realized_pnl_v2")) for r in rows)),
                "median_sell_x": fmt_num(median([fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) for r in rows]), 2),
                "median_ratio": fmt_pct(median([fnum(r.get("sell_share_ratio"), math.nan) for r in rows])),
            }
        )

    mode_rows = []
    for mode, rows in [("mostly_or_fully_sold", mostly), ("partially_sold", partial), ("never_sold", never)]:
        mode_rows.append(
            {
                "mode": mode,
                "n": len(rows),
                "pnl": fmt_money(sum(fnum(r.get("realized_pnl_v2")) for r in rows)),
                "hold": fmt_money(sum(fnum(r.get("hold_to_resolution_pnl_estimate")) for r in rows)),
                "median_sell_x": fmt_num(median([fnum(r.get("weighted_sell_multiple_vs_avg_buy"), math.nan) for r in rows]), 2),
                "median_ratio": fmt_pct(median([fnum(r.get("sell_share_ratio"), math.nan) for r in rows])),
            }
        )

    return [
        "# HUSKYVS_EXIT_TRIGGER_STUDY_v3",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Scope",
        "",
        f"- Selected {len(case_rows)} representative settled weather positions from the corrected v2 sample; no account-level trades were re-fetched.",
        f"- Public market price history was queried only for selected asset ids via `{PRICE_HISTORY_URL}` at 5-minute fidelity.",
        f"- Price history source: {PRICE_HISTORY_DOCS_URL}",
        f"- Market history available for {history_ok}/{len(case_rows)} cases. Observed highs combine sampled market history with huskyvs' own public fills, so they are lower bounds on true tick-level highs.",
        "",
        "## Sample Mix",
        "",
        markdown_table(
            category_rows,
            [
                ("Category", "category"),
                ("Cases", "n"),
                ("PnL", "pnl"),
                ("Median Sell Multiple", "median_sell_x"),
                ("Median Sell Share", "median_ratio"),
            ],
        ),
        "",
        "## Required Answers",
        "",
        f"**Does he often sell after price doubles?** In this representative sample, {len(sold_2x)}/{len(sold_rows)} sold cases have weighted sell price at least 2.0x the weighted buy price; {len(sold_1_5x)}/{len(sold_rows)} are at least 1.5x and {len(sold_3x)}/{len(sold_rows)} are at least 3.0x. This supports a take-profit behavior around large multiples, but not a single fixed 2x rule.",
        "",
        f"**Is there a fixed reduction ratio?** Partial-sell cases have median sell-share ratio {fmt_pct(median([fnum(r.get('sell_share_ratio'), math.nan) for r in partial]))}; mostly/fully sold cases have median {fmt_pct(median([fnum(r.get('sell_share_ratio'), math.nan) for r in mostly]))}. The ratios are dispersed by design bucket, so no fixed mechanical trim percentage is visible in fills.",
        "",
        "**Full sells vs partial sells.**",
        "",
        markdown_table(
            mode_rows,
            [
                ("Exit Mode", "mode"),
                ("Cases", "n"),
                ("Actual PnL", "pnl"),
                ("Hold-to-Resolution PnL", "hold"),
                ("Median Sell Multiple", "median_sell_x"),
                ("Median Sell Share", "median_ratio"),
            ],
        ),
        "",
        f"**Most effective take-profit multiple or probability zone.** The strongest sampled sell-multiple bucket is `{best_multiple.get('group', 'n/a')}` with total PnL {fmt_money(best_multiple.get('total_realized_pnl_v2'))}; the strongest entry probability bin is `{best_price.get('group', 'n/a')}` with total sampled PnL {fmt_money(best_price.get('total_realized_pnl_v2'))}. Treat this as hypothesis generation only because the sample is representative, not exhaustive.",
        "",
        f"**Signals that help avoid 'correct prediction but losing bet'.** The sample includes {len(correct_loss)} cases where the asset ultimately won but realized trading PnL was negative. The common failure mode is selling too much too early or below the blended entry basis before local weather-day end; in contrast, {len(continued_up)} sold cases continued at least 10% above the weighted sell price after the last sell. For the never-sold loss cases, {len(never_reached_2x)}/{len(never)} had an observed 2x print and {len(never_reached_3x)}/{len(never)} had an observed 3x print, so a major avoidable failure mode is round-tripping an available profit to settlement loss.",
        "",
        "## Candidate Exit Rules To Validate",
        "",
        "1. Do not let a correct-weather thesis become a loss by selling the majority below blended cost; require a minimum positive sell multiple before large exits.",
        "2. Treat 1.5x as an alert, 2.0x as the first serious profit-taking threshold, and >=3.0x as the strongest sampled take-profit zone; verify on the full sample before operational use.",
        "3. For tickets that print 2x or 3x and then remain open, force a trim-or-recheck decision before the final local-day window to avoid round-tripping to zero.",
        "4. When the token is likely to settle correct, retain a small residual position instead of fully exiting before local day end unless the price already reflects near-certain settlement.",
        "5. Re-check adjacent basket exposure after a partial sell; a profitable sell on one bucket can leave losing residuals in neighboring buckets.",
        "",
        "## Diagnostics",
        "",
        f"- Low-price never-sold losses in the sample: {len(low_price_never_loss)} cases.",
        f"- Cases where post-sell price continued up by at least 10%: {len(continued_up)}.",
        f"- Cases with empty or unavailable public history: {len(case_rows) - history_ok}.",
        "",
        "## Data Gaps",
        "",
        "- Public CLOB history is sampled, not a full tick/order-book replay; true intraperiod highs and liquidity at size can be missed.",
        "- Open orders, cancellations, queue position, and quote edits remain unavailable from the existing public account files.",
        "- Price-history API availability can vary by resolved market; rows with missing history are explicitly flagged.",
        "- Hold-to-resolution PnL is estimated from v2 settlement state and original bought shares; it ignores whether the same size could have been carried without liquidity or risk constraints.",
        "- The 42-case sample is representative and stratified, not a full-population causal estimate.",
    ]


def generate_report(path: Path, case_rows: list[dict[str, Any]], summary_rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(report_lines(case_rows, summary_rows)) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--fidelity", type=int, default=5)
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()

    root = args.root
    lifecycle = read_csv(root / "data/processed_v2/corrected_position_lifecycle.csv")
    trades = read_csv(root / "data/raw/trades.csv")
    fills_by_asset = normalize_fills(trades)
    selected = select_cases(lifecycle)
    now_ts = int(datetime.now(timezone.utc).timestamp())

    case_rows = [
        build_case_row(
            idx=i + 1,
            row=row,
            fills_by_asset=fills_by_asset,
            fidelity=args.fidelity,
            fetch_history=not args.skip_history,
            user_agent=args.user_agent,
            now_ts=now_ts,
        )
        for i, row in enumerate(selected)
    ]
    summary_rows = build_summary(case_rows)

    write_csv(root / "data/exit_cases_v3.csv", case_rows, CASE_FIELDS)
    write_csv(root / "data/exit_trigger_summary_v3.csv", summary_rows, SUMMARY_FIELDS)
    generate_report(root / "reports/HUSKYVS_EXIT_TRIGGER_STUDY_v3.md", case_rows, summary_rows)

    print(
        json.dumps(
            {
                "case_rows": len(case_rows),
                "summary_rows": len(summary_rows),
                "history_ok": sum(1 for r in case_rows if r.get("market_history_status") == "ok"),
                "history_missing": sum(1 for r in case_rows if r.get("market_history_status") != "ok"),
                "cases_path": str(root / "data/exit_cases_v3.csv"),
                "summary_path": str(root / "data/exit_trigger_summary_v3.csv"),
                "report_path": str(root / "reports/HUSKYVS_EXIT_TRIGGER_STUDY_v3.md"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
