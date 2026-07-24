#!/usr/bin/env python3
"""Full-sample v4 exit-rule backtest for huskyvs weather YES positions.

The script preserves huskyvs' real BUY fills and replaces only exits. It uses
the corrected v2 position sample and token-level public Polymarket price
history. It never re-fetches account-level trades and never places orders.
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
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analyze_weather_strategy_v2 import adjacent_exact_or_range, fnum, finite, iso_from_epoch


PRICE_HISTORY_URL = "https://clob.polymarket.com/prices-history"
BATCH_PRICE_HISTORY_URL = "https://clob.polymarket.com/batch-prices-history"
PRICE_HISTORY_DOCS_URL = "https://docs.polymarket.com/api-reference/markets/get-prices-history"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
SCENARIOS = [
    ("sampled_1_0", 1.0),
    ("haircut_0_9", 0.9),
    ("haircut_0_8", 0.8),
]
PRICE_BIN_ORDER = ["0-1c", "1-2c", "2-5c", "5-10c", "10-20c", ">=20c", "unknown"]
LEAD_BIN_ORDER = ["0-6h", "6-12h", "12-24h", "24-48h", "48-72h", "72h+", "unknown"]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    family: str
    description: str
    steps: tuple[tuple[float, float], ...] = ()
    recover_principal: bool = False
    hold_only: bool = False
    actual_huskyvs: bool = False


@dataclass
class SimResult:
    rule_id: str
    price_scenario: str
    haircut: float
    simulated_pnl: float
    simulated_roi: float
    simulated_sell_proceeds: float
    simulated_settlement_value: float
    simulated_sold_shares: float
    simulated_remaining_shares: float
    triggered_steps: str
    first_trigger_ts: int
    last_trigger_ts: int
    first_trigger_price: float
    last_trigger_price: float
    max_price_pre_end: float
    max_multiple_pre_end: float
    saved_loser_to_profit: bool
    roundtrip_2x_loss_improved: bool
    roundtrip_2x_loss_saved_to_profit: bool
    premature_correct_sell_loss: bool
    no_future_sell_violation: bool


METRIC_FIELDS = [
    "rule_id",
    "rule_family",
    "rule_description",
    "split",
    "price_scenario",
    "haircut",
    "group_type",
    "group",
    "positions",
    "events",
    "cities",
    "price_bins",
    "buy_usd",
    "net_pnl",
    "roi_on_buy_usd",
    "win_rate",
    "median_pnl",
    "median_roi",
    "max_single_loss",
    "top1_profit_share_of_net",
    "top5_profit_share_of_net",
    "top10_profit_share_of_net",
    "leave_top1_out_net_pnl",
    "leave_top5_out_net_pnl",
    "weather_date_sequence_drawdown",
    "delta_vs_hold_pnl",
    "saved_loser_to_profit_count",
    "roundtrip_2x_loss_improved_count",
    "roundtrip_2x_loss_saved_to_profit_count",
    "premature_correct_sell_loss_count",
    "premature_correct_sell_loss_usd",
    "max_city_abs_pnl_share",
    "max_price_bin_abs_pnl_share",
]

DETAIL_FIELDS = [
    "asset",
    "event_key",
    "split",
    "position_structure",
    "city",
    "weather_date",
    "weather_metric",
    "bucket_label",
    "entry_price_bin",
    "first_entry_lead_bin_local",
    "local_weather_day_end_utc",
    "rule_id",
    "rule_family",
    "price_scenario",
    "haircut",
    "history_status",
    "history_points_pre_end",
    "buy_count",
    "buy_shares",
    "buy_usd",
    "weighted_avg_buy_price",
    "settlement_price_v2",
    "asset_won_v2",
    "hold_to_settlement_pnl",
    "actual_huskyvs_pnl_v2",
    "simulated_pnl",
    "simulated_roi",
    "delta_vs_hold_pnl",
    "simulated_sell_proceeds",
    "simulated_settlement_value",
    "simulated_sold_shares",
    "simulated_remaining_shares",
    "triggered_steps",
    "first_trigger_utc",
    "last_trigger_utc",
    "first_trigger_price",
    "last_trigger_price",
    "max_price_pre_end",
    "max_multiple_pre_end",
    "saved_loser_to_profit",
    "roundtrip_2x_loss_improved",
    "roundtrip_2x_loss_saved_to_profit",
    "premature_correct_sell_loss",
    "no_future_sell_violation",
]

VALIDATION_FIELDS = [
    "rule_id",
    "rule_family",
    "rule_description",
    "train_positions",
    "validation_positions",
    "train_net_pnl",
    "train_roi",
    "validation_net_pnl",
    "validation_roi",
    "validation_delta_vs_hold",
    "validation_leave_top5_out_net_pnl",
    "validation_drawdown_by_weather_date",
    "validation_saved_loser_to_profit_count",
    "validation_roundtrip_2x_loss_saved_to_profit_count",
    "validation_premature_correct_sell_loss_count",
    "validation_0_9_net_pnl",
    "validation_0_8_net_pnl",
    "validation_0_8_delta_vs_hold",
    "validation_max_city_abs_pnl_share",
    "validation_max_price_bin_abs_pnl_share",
    "meets_candidate_filters",
    "filter_notes",
]

TOP_CANDIDATE_FIELDS = [
    "candidate_type",
    "rank",
    "rule_id",
    "rule_family",
    "rule_description",
    "validation_net_pnl",
    "validation_roi",
    "validation_0_8_net_pnl",
    "validation_delta_vs_hold",
    "validation_leave_top5_out_net_pnl",
    "validation_saved_loser_to_profit_count",
    "validation_roundtrip_2x_loss_saved_to_profit_count",
    "validation_premature_correct_sell_loss_count",
    "validation_max_city_abs_pnl_share",
    "selection_notes",
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


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


def fmt_money(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"${x:,.2f}"


def fmt_pct(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not finite(x) else f"{x * 100:.1f}%"


def event_key(row: dict[str, Any]) -> str:
    return "|".join([row.get("weather_date", ""), row.get("city", ""), row.get("weather_metric", ""), row.get("unit", "")])


def build_rules() -> list[Rule]:
    rules: list[Rule] = []
    for multiple in [1.5, 2.0, 3.0, 5.0]:
        mult_label = str(multiple).replace(".", "_")
        for pct in [0.25, 0.50, 0.75, 1.00]:
            pct_label = int(round(pct * 100))
            rules.append(
                Rule(
                    rule_id=f"tp_{mult_label}x_sell_{pct_label}pct",
                    family="single_take_profit",
                    description=f"{multiple:g}x触发后卖出{pct_label}%当前已买入且未卖出的份额，剩余持有结算",
                    steps=((multiple, pct),),
                )
            )
    rules.extend(
        [
            Rule(
                rule_id="ladder_1_5x25_2x25_3x25_hold",
                family="ladder_take_profit",
                description="1.5x卖25%，2x再卖25%，3x再卖25%，剩余持有结算",
                steps=((1.5, 0.25), (2.0, 0.25), (3.0, 0.25)),
            ),
            Rule(
                rule_id="combo_2x_sell50_hold",
                family="combo_take_profit",
                description="2x卖50%，剩余持有结算",
                steps=((2.0, 0.50),),
            ),
            Rule(
                rule_id="combo_3x_sell50_hold",
                family="combo_take_profit",
                description="3x卖50%，剩余持有结算",
                steps=((3.0, 0.50),),
            ),
            Rule(
                rule_id="recover_principal_keep_free",
                family="principal_recovery",
                description="首次可收回累计本金时卖出足够份额，剩余作为免费仓位持有结算",
                recover_principal=True,
            ),
            Rule(
                rule_id="hold_to_settlement",
                family="baseline",
                description="完全不卖，持有到结算",
                hold_only=True,
            ),
            Rule(
                rule_id="actual_huskyvs_exit",
                family="baseline",
                description="huskyvs实际退出结果",
                actual_huskyvs=True,
            ),
        ]
    )
    return rules


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
                "price": price,
                "shares": size,
                "usd": price * size,
                "transactionHash": row.get("transactionHash", ""),
            }
        )
    for rows in fills.values():
        rows.sort(key=lambda r: (r["timestamp"], r["side"], r["transactionHash"]))
    return fills


def split_events(rows: list[dict[str, Any]], train_ratio: float = 0.70) -> tuple[set[str], set[str]]:
    keys = sorted({event_key(r) for r in rows})
    cutoff = int(len(keys) * train_ratio)
    return set(keys[:cutoff]), set(keys[cutoff:])


def classify_position_structure(rows: list[dict[str, Any]]) -> dict[str, str]:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_event[event_key(row)].append(row)
    out: dict[str, str] = {}
    for event_rows in by_event.values():
        if len(event_rows) == 1:
            label = "single_position"
        elif adjacent_exact_or_range(event_rows):
            label = "adjacent_basket_position"
        else:
            label = "multi_non_adjacent_position"
        for row in event_rows:
            out[row["asset"]] = label
    return out


def cache_path(cache_dir: Path, asset: str) -> Path:
    return cache_dir / f"{asset}.json"


def clean_history_points(points: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(points, list):
        return out
    for point in points:
        ts = parse_ts(point.get("t") if isinstance(point, dict) else None)
        price = fnum(point.get("p") if isinstance(point, dict) else None, math.nan)
        if ts and finite(price):
            out.append({"t": ts, "p": price})
    out.sort(key=lambda r: r["t"])
    return out


def load_cached_history(cache_dir: Path, asset: str) -> dict[str, Any] | None:
    path = cache_path(cache_dir, asset)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    payload["history"] = clean_history_points(payload.get("history", []))
    return payload


def save_history(cache_dir: Path, asset: str, status: str, history: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "asset": asset,
        "status": status,
        "history": clean_history_points(history),
        "meta": meta,
    }
    cache_path(cache_dir, asset).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def http_json(url: str, method: str, user_agent: str, payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode("utf-8")), "ok"
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "ignore")[:300]
            if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None, f"http_{exc.code}:{body}"
        except Exception as exc:
            if attempt < 3:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None, f"{type(exc).__name__}:{exc}"
    return None, "unknown_error"


def fetch_single(asset: str, start_ts: int, end_ts: int, fidelity: int, user_agent: str) -> tuple[str, list[dict[str, Any]], str]:
    params = urllib.parse.urlencode({"market": asset, "startTs": start_ts, "endTs": end_ts, "fidelity": fidelity})
    payload, status = http_json(f"{PRICE_HISTORY_URL}?{params}", "GET", user_agent)
    if status != "ok" or not payload:
        return asset, [], f"missing_{status}"
    history = clean_history_points(payload.get("history", []))
    return asset, history, "ok" if history else "missing_empty_history"


def fetch_batch(
    assets: list[str],
    start_ts: int,
    end_ts: int,
    fidelity: int,
    user_agent: str,
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    payload = {"markets": assets, "start_ts": start_ts, "end_ts": end_ts, "fidelity": fidelity}
    response, status = http_json(BATCH_PRICE_HISTORY_URL, "POST", user_agent, payload)
    if status != "ok" or not response:
        return {asset: ([], f"missing_batch_{status}") for asset in assets}
    history_map = response.get("history", {})
    out: dict[str, tuple[list[dict[str, Any]], str]] = {}
    for asset in assets:
        history = clean_history_points(history_map.get(asset, [])) if isinstance(history_map, dict) else []
        out[asset] = (history, "ok" if history else "missing_empty_history")
    return out


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def ensure_price_histories(
    rows: list[dict[str, Any]],
    cache_dir: Path,
    fidelity: int,
    workers: int,
    user_agent: str,
    skip_fetch: bool = False,
) -> dict[str, dict[str, Any]]:
    by_asset = {r["asset"]: r for r in rows}
    cached = {asset: load_cached_history(cache_dir, asset) for asset in by_asset}
    missing_assets = [asset for asset, payload in cached.items() if payload is None]
    if missing_assets and skip_fetch:
        for asset in missing_assets:
            save_history(cache_dir, asset, "missing_fetch_disabled", [], {"source": "not_fetched"})

    if missing_assets and not skip_fetch:
        event_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for asset in missing_assets:
            event_groups[event_key(by_asset[asset])].append(by_asset[asset])

        tasks = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for event_rows in event_groups.values():
                for group_rows in chunks(event_rows, 20):
                    assets = [r["asset"] for r in group_rows]
                    start_ts = max(0, min(parse_ts(r.get("first_buy_ts")) for r in group_rows) - 3600)
                    end_ts = max(parse_ts(r.get("local_weather_day_end_epoch")) for r in group_rows)
                    tasks.append(pool.submit(fetch_batch, assets, start_ts, end_ts, fidelity, user_agent))
            for future in as_completed(tasks):
                result = future.result()
                fallback: list[tuple[str, int, int]] = []
                for asset, (history, status) in result.items():
                    row = by_asset[asset]
                    start_ts = max(0, parse_ts(row.get("first_buy_ts")) - 3600)
                    end_ts = parse_ts(row.get("local_weather_day_end_epoch"))
                    if status.startswith("missing_batch_http_400"):
                        fallback.append((asset, start_ts, end_ts))
                    else:
                        save_history(cache_dir, asset, status, history, {"source": "batch-prices-history", "fidelity": fidelity, "start_ts": start_ts, "end_ts": end_ts})
                for asset, start_ts, end_ts in fallback:
                    single_asset, history, status = fetch_single(asset, start_ts, end_ts, fidelity, user_agent)
                    save_history(cache_dir, single_asset, status, history, {"source": "prices-history", "fidelity": fidelity, "start_ts": start_ts, "end_ts": end_ts})

    return {asset: load_cached_history(cache_dir, asset) or {"status": "missing_cache", "history": []} for asset in by_asset}


def history_pre_end(row: dict[str, Any], cached: dict[str, Any]) -> list[dict[str, Any]]:
    first_buy = parse_ts(row.get("first_buy_ts"))
    local_end = parse_ts(row.get("local_weather_day_end_epoch"))
    points = []
    for point in cached.get("history", []):
        ts = parse_ts(point.get("t"))
        price = fnum(point.get("p"), math.nan)
        if first_buy <= ts <= local_end and finite(price):
            points.append({"timestamp": ts, "price": price})
    points.sort(key=lambda r: r["timestamp"])
    return points


def hold_to_settlement_pnl(row: dict[str, Any]) -> float:
    settlement_price = fnum(row.get("settlement_price_v2"), math.nan)
    if not finite(settlement_price):
        return math.nan
    return fnum(row.get("buy_shares")) * settlement_price - fnum(row.get("buy_usd"))


def simulate_rule(
    row: dict[str, Any],
    buy_fills: list[dict[str, Any]],
    price_points: list[dict[str, Any]],
    rule: Rule,
    scenario: str,
    haircut: float,
) -> SimResult:
    buy_usd = fnum(row.get("buy_usd"))
    buy_shares = fnum(row.get("buy_shares"))
    settlement_price = fnum(row.get("settlement_price_v2"), math.nan)
    hold_pnl = hold_to_settlement_pnl(row)
    actual_pnl = fnum(row.get("realized_pnl_v2"), math.nan)
    actual_sold = fnum(row.get("sell_shares"))
    asset_won = row.get("asset_won_v2") == "True"
    weighted_avg_buy = fnum(row.get("weighted_avg_buy_price"), math.nan)
    max_price = max((p["price"] * haircut for p in price_points), default=math.nan)
    max_multiple = safe_div(max_price, weighted_avg_buy)

    if rule.actual_huskyvs:
        pnl = actual_pnl
        roi = safe_div(pnl, buy_usd)
        return SimResult(rule.rule_id, scenario, haircut, pnl, roi, math.nan, math.nan, actual_sold, math.nan, "actual_huskyvs", parse_ts(row.get("first_sell_ts")), parse_ts(row.get("last_sell_ts")), fnum(row.get("weighted_avg_sell_price"), math.nan), fnum(row.get("weighted_avg_sell_price"), math.nan), max_price, max_multiple, hold_pnl < 0 < pnl, hold_pnl < 0 and max_multiple >= 2 and pnl > hold_pnl, hold_pnl < 0 and max_multiple >= 2 and pnl > 0, asset_won and actual_sold > 0 and pnl < hold_pnl, False)

    if rule.hold_only:
        pnl = hold_pnl
        roi = safe_div(pnl, buy_usd)
        return SimResult(rule.rule_id, scenario, haircut, pnl, roi, 0.0, buy_shares * settlement_price, 0.0, buy_shares, "", 0, 0, math.nan, math.nan, max_price, max_multiple, hold_pnl < 0 < pnl, False, False, False, False)

    inventory = 0.0
    cost_basis = 0.0
    cumulative_buy_usd = 0.0
    proceeds = 0.0
    triggered: list[str] = []
    triggered_steps: set[int] = set()
    first_trigger_ts = 0
    last_trigger_ts = 0
    first_trigger_price = math.nan
    last_trigger_price = math.nan
    sold_shares = 0.0
    no_future_sell_violation = False

    buy_events = [
        {"timestamp": b["timestamp"], "kind": "buy", "price": b["price"], "shares": b["shares"]}
        for b in buy_fills
        if b["timestamp"] <= parse_ts(row.get("local_weather_day_end_epoch"))
    ]
    price_events = [{"timestamp": p["timestamp"], "kind": "price", "price": p["price"] * haircut} for p in price_points]
    events = sorted(buy_events + price_events, key=lambda e: (e["timestamp"], 0 if e["kind"] == "buy" else 1))

    def execute_sell(qty: float, price: float, label: str, ts: int) -> None:
        nonlocal inventory, cost_basis, proceeds, sold_shares, first_trigger_ts, last_trigger_ts, first_trigger_price, last_trigger_price, no_future_sell_violation
        if qty <= 0 or inventory <= 0:
            return
        if qty > inventory + 1e-9:
            no_future_sell_violation = True
        qty = min(qty, inventory)
        avg_cost = safe_div(cost_basis, inventory)
        proceeds += qty * price
        cost_basis = max(0.0, cost_basis - avg_cost * qty)
        inventory -= qty
        sold_shares += qty
        triggered.append(label)
        if not first_trigger_ts:
            first_trigger_ts = ts
            first_trigger_price = price
        last_trigger_ts = ts
        last_trigger_price = price

    for event in events:
        ts = event["timestamp"]
        price = event["price"]
        if event["kind"] == "buy":
            shares = event["shares"]
            inventory += shares
            cost_basis += shares * price
            cumulative_buy_usd += shares * price
            continue
        if inventory <= 0:
            continue
        avg_cost = safe_div(cost_basis, inventory)
        if not finite(avg_cost) or avg_cost <= 0:
            continue
        if rule.recover_principal:
            unrecovered = max(cumulative_buy_usd - proceeds, 0.0)
            if unrecovered > 0 and price * inventory >= unrecovered and "recover_principal" not in triggered:
                execute_sell(unrecovered / price, price, "recover_principal", ts)
            continue
        for idx, (multiple, sell_fraction) in enumerate(rule.steps):
            if idx in triggered_steps:
                continue
            if price >= avg_cost * multiple and inventory > 0:
                qty = inventory * sell_fraction
                execute_sell(qty, price, f"{multiple:g}x_sell_{sell_fraction:.0%}", ts)
                triggered_steps.add(idx)
                avg_cost = safe_div(cost_basis, inventory) if inventory > 0 else math.nan

    settlement_value = inventory * settlement_price if finite(settlement_price) else math.nan
    pnl = proceeds + settlement_value - buy_usd
    roi = safe_div(pnl, buy_usd)
    premature = asset_won and sold_shares > 0 and pnl < hold_pnl - 1e-9
    return SimResult(
        rule.rule_id,
        scenario,
        haircut,
        pnl,
        roi,
        proceeds,
        settlement_value,
        sold_shares,
        inventory,
        ";".join(triggered),
        first_trigger_ts,
        last_trigger_ts,
        first_trigger_price,
        last_trigger_price,
        max_price,
        max_multiple,
        hold_pnl < 0 < pnl,
        hold_pnl < 0 and max_multiple >= 2 and pnl > hold_pnl,
        hold_pnl < 0 and max_multiple >= 2 and pnl > 0,
        premature,
        no_future_sell_violation,
    )


def max_drawdown_by_date(rows: list[dict[str, Any]]) -> float:
    by_date: dict[str, float] = defaultdict(float)
    for row in rows:
        by_date[row["weather_date"]] += fnum(row.get("simulated_pnl"))
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for weather_date in sorted(by_date):
        cumulative += by_date[weather_date]
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def concentration(rows: list[dict[str, Any]], k: int) -> tuple[float, float]:
    total = sum(fnum(r.get("simulated_pnl")) for r in rows)
    positives = sorted([fnum(r.get("simulated_pnl")) for r in rows if fnum(r.get("simulated_pnl")) > 0], reverse=True)
    top_sum = sum(positives[:k])
    return top_sum, safe_div(top_sum, total) if total > 0 else math.nan


def max_abs_share(rows: list[dict[str, Any]], key: str) -> float:
    grouped: dict[str, float] = defaultdict(float)
    for row in rows:
        grouped[str(row.get(key, ""))] += fnum(row.get("simulated_pnl"))
    denom = sum(abs(v) for v in grouped.values())
    return max((abs(v) for v in grouped.values()), default=0.0) / denom if denom else math.nan


def metric_row(rule: Rule, split: str, scenario: str, haircut: float, group_type: str, group: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    buy_usd = sum(fnum(r.get("buy_usd")) for r in rows)
    net_pnl = sum(fnum(r.get("simulated_pnl")) for r in rows)
    top1, top1_share = concentration(rows, 1)
    top5, top5_share = concentration(rows, 5)
    top10, top10_share = concentration(rows, 10)
    premature_loss_usd = sum(
        max(0.0, fnum(r.get("hold_to_settlement_pnl")) - fnum(r.get("simulated_pnl")))
        for r in rows
        if str(r.get("premature_correct_sell_loss")) == "True"
    )
    return {
        "rule_id": rule.rule_id,
        "rule_family": rule.family,
        "rule_description": rule.description,
        "split": split,
        "price_scenario": scenario,
        "haircut": haircut,
        "group_type": group_type,
        "group": group,
        "positions": len(rows),
        "events": len({r.get("event_key") for r in rows}),
        "cities": len({r.get("city") for r in rows}),
        "price_bins": len({r.get("entry_price_bin") for r in rows}),
        "buy_usd": buy_usd,
        "net_pnl": net_pnl,
        "roi_on_buy_usd": safe_div(net_pnl, buy_usd),
        "win_rate": safe_div(sum(1 for r in rows if fnum(r.get("simulated_pnl")) > 0), len(rows)),
        "median_pnl": median([fnum(r.get("simulated_pnl"), math.nan) for r in rows]),
        "median_roi": median([fnum(r.get("simulated_roi"), math.nan) for r in rows]),
        "max_single_loss": min((fnum(r.get("simulated_pnl")) for r in rows), default=math.nan),
        "top1_profit_share_of_net": top1_share,
        "top5_profit_share_of_net": top5_share,
        "top10_profit_share_of_net": top10_share,
        "leave_top1_out_net_pnl": net_pnl - top1,
        "leave_top5_out_net_pnl": net_pnl - top5,
        "weather_date_sequence_drawdown": max_drawdown_by_date(rows),
        "delta_vs_hold_pnl": sum(fnum(r.get("delta_vs_hold_pnl")) for r in rows),
        "saved_loser_to_profit_count": sum(1 for r in rows if str(r.get("saved_loser_to_profit")) == "True"),
        "roundtrip_2x_loss_improved_count": sum(1 for r in rows if str(r.get("roundtrip_2x_loss_improved")) == "True"),
        "roundtrip_2x_loss_saved_to_profit_count": sum(1 for r in rows if str(r.get("roundtrip_2x_loss_saved_to_profit")) == "True"),
        "premature_correct_sell_loss_count": sum(1 for r in rows if str(r.get("premature_correct_sell_loss")) == "True"),
        "premature_correct_sell_loss_usd": premature_loss_usd,
        "max_city_abs_pnl_share": max_abs_share(rows, "city"),
        "max_price_bin_abs_pnl_share": max_abs_share(rows, "entry_price_bin"),
    }


def details_for_position(
    row: dict[str, Any],
    rule: Rule,
    scenario: str,
    haircut: float,
    result: SimResult,
    history_status: str,
    history_points_count: int,
) -> dict[str, Any]:
    hold_pnl = hold_to_settlement_pnl(row)
    return {
        "asset": row["asset"],
        "event_key": row["event_key"],
        "split": row["split"],
        "position_structure": row["position_structure"],
        "city": row.get("city", ""),
        "weather_date": row.get("weather_date", ""),
        "weather_metric": row.get("weather_metric", ""),
        "bucket_label": row.get("bucket_label", ""),
        "entry_price_bin": row.get("entry_price_bin", ""),
        "first_entry_lead_bin_local": row.get("first_entry_lead_bin_local", ""),
        "local_weather_day_end_utc": row.get("local_weather_day_end_utc", ""),
        "rule_id": rule.rule_id,
        "rule_family": rule.family,
        "price_scenario": scenario,
        "haircut": haircut,
        "history_status": history_status,
        "history_points_pre_end": history_points_count,
        "buy_count": row.get("buy_count", ""),
        "buy_shares": row.get("buy_shares", ""),
        "buy_usd": row.get("buy_usd", ""),
        "weighted_avg_buy_price": row.get("weighted_avg_buy_price", ""),
        "settlement_price_v2": row.get("settlement_price_v2", ""),
        "asset_won_v2": row.get("asset_won_v2", ""),
        "hold_to_settlement_pnl": hold_pnl,
        "actual_huskyvs_pnl_v2": row.get("realized_pnl_v2", ""),
        "simulated_pnl": result.simulated_pnl,
        "simulated_roi": result.simulated_roi,
        "delta_vs_hold_pnl": result.simulated_pnl - hold_pnl,
        "simulated_sell_proceeds": result.simulated_sell_proceeds,
        "simulated_settlement_value": result.simulated_settlement_value,
        "simulated_sold_shares": result.simulated_sold_shares,
        "simulated_remaining_shares": result.simulated_remaining_shares,
        "triggered_steps": result.triggered_steps,
        "first_trigger_utc": iso_from_epoch(result.first_trigger_ts),
        "last_trigger_utc": iso_from_epoch(result.last_trigger_ts),
        "first_trigger_price": result.first_trigger_price,
        "last_trigger_price": result.last_trigger_price,
        "max_price_pre_end": result.max_price_pre_end,
        "max_multiple_pre_end": result.max_multiple_pre_end,
        "saved_loser_to_profit": result.saved_loser_to_profit,
        "roundtrip_2x_loss_improved": result.roundtrip_2x_loss_improved,
        "roundtrip_2x_loss_saved_to_profit": result.roundtrip_2x_loss_saved_to_profit,
        "premature_correct_sell_loss": result.premature_correct_sell_loss,
        "no_future_sell_violation": result.no_future_sell_violation,
    }


def build_metrics(rule_by_id: dict[str, Rule], detail_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grid_rows: list[dict[str, Any]] = []
    price_rows: list[dict[str, Any]] = []
    lead_rows: list[dict[str, Any]] = []
    haircut_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        grouped[(row["rule_id"], row["split"], row["price_scenario"], "all_yes")].append(row)

    for (rule_id, split, scenario, group), rows in grouped.items():
        rule = rule_by_id[rule_id]
        haircut = fnum(rows[0].get("haircut"))
        metric = metric_row(rule, split, scenario, haircut, "all_yes", group, rows)
        grid_rows.append(metric)
        if split == "validation":
            haircut_rows.append(metric)

    for group_field, order, out_rows, group_type in [
        ("entry_price_bin", PRICE_BIN_ORDER, price_rows, "entry_price_bin"),
        ("first_entry_lead_bin_local", LEAD_BIN_ORDER, lead_rows, "entry_lead_bin"),
    ]:
        grouped2: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in detail_rows:
            grouped2[(row["rule_id"], row["split"], row["price_scenario"], row.get(group_field) or "unknown")].append(row)
        for (rule_id, split, scenario, group), rows in grouped2.items():
            rule = rule_by_id[rule_id]
            haircut = fnum(rows[0].get("haircut"))
            out_rows.append(metric_row(rule, split, scenario, haircut, group_type, group, rows))
        out_rows.sort(key=lambda r: (r["rule_id"], r["split"], r["price_scenario"], order.index(r["group"]) if r["group"] in order else 999))

    grid_rows.sort(key=lambda r: (r["rule_id"], r["split"], r["price_scenario"]))
    haircut_rows.sort(key=lambda r: (r["rule_id"], r["price_scenario"]))
    return grid_rows, haircut_rows, price_rows, lead_rows


def validation_rows(grid_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(r["rule_id"], r["split"], r["price_scenario"]): r for r in grid_rows if r["group_type"] == "all_yes"}
    out: list[dict[str, Any]] = []
    rule_ids = sorted({r["rule_id"] for r in grid_rows})
    for rule_id in rule_ids:
        train = by_key.get((rule_id, "train", "sampled_1_0"), {})
        val = by_key.get((rule_id, "validation", "sampled_1_0"), {})
        val09 = by_key.get((rule_id, "validation", "haircut_0_9"), {})
        val08 = by_key.get((rule_id, "validation", "haircut_0_8"), {})
        if not val:
            continue
        notes = []
        if fnum(val.get("positions")) <= 25:
            notes.append("small_validation_sample")
        if fnum(val.get("net_pnl")) <= 0:
            notes.append("validation_not_positive")
        if fnum(val08.get("net_pnl")) <= 0:
            notes.append("haircut_0_8_not_positive")
        if fnum(val.get("leave_top5_out_net_pnl")) <= 0 and fnum(val.get("delta_vs_hold_pnl")) <= 0:
            notes.append("top5_removed_not_positive_and_not_better_than_hold")
        if fnum(val.get("max_city_abs_pnl_share")) > 0.35:
            notes.append("city_concentration_high")
        if fnum(val.get("max_price_bin_abs_pnl_share")) > 0.55:
            notes.append("price_bin_concentration_high")
        out.append(
            {
                "rule_id": rule_id,
                "rule_family": val.get("rule_family", ""),
                "rule_description": val.get("rule_description", ""),
                "train_positions": train.get("positions", ""),
                "validation_positions": val.get("positions", ""),
                "train_net_pnl": train.get("net_pnl", ""),
                "train_roi": train.get("roi_on_buy_usd", ""),
                "validation_net_pnl": val.get("net_pnl", ""),
                "validation_roi": val.get("roi_on_buy_usd", ""),
                "validation_delta_vs_hold": val.get("delta_vs_hold_pnl", ""),
                "validation_leave_top5_out_net_pnl": val.get("leave_top5_out_net_pnl", ""),
                "validation_drawdown_by_weather_date": val.get("weather_date_sequence_drawdown", ""),
                "validation_saved_loser_to_profit_count": val.get("saved_loser_to_profit_count", ""),
                "validation_roundtrip_2x_loss_saved_to_profit_count": val.get("roundtrip_2x_loss_saved_to_profit_count", ""),
                "validation_premature_correct_sell_loss_count": val.get("premature_correct_sell_loss_count", ""),
                "validation_0_9_net_pnl": val09.get("net_pnl", ""),
                "validation_0_8_net_pnl": val08.get("net_pnl", ""),
                "validation_0_8_delta_vs_hold": val08.get("delta_vs_hold_pnl", ""),
                "validation_max_city_abs_pnl_share": val.get("max_city_abs_pnl_share", ""),
                "validation_max_price_bin_abs_pnl_share": val.get("max_price_bin_abs_pnl_share", ""),
                "meets_candidate_filters": not notes,
                "filter_notes": ";".join(notes),
            }
        )
    out.sort(key=lambda r: fnum(r.get("validation_net_pnl"), -math.inf), reverse=True)
    return out


def top_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [r for r in rows if str(r.get("meets_candidate_filters")) == "True" and r["rule_id"] not in {"actual_huskyvs_exit", "hold_to_settlement"}]
    out: list[dict[str, Any]] = []
    for rank, row in enumerate(sorted(candidates, key=lambda r: fnum(r.get("validation_net_pnl"), -math.inf), reverse=True)[:3], 1):
        out.append(candidate_row("highest_validation_profit", rank, row, "按验证集原始采样价格净利润排序；规则仍需通过稳健性过滤"))
    robust_sorted = sorted(
        candidates,
        key=lambda r: (
            fnum(r.get("validation_0_8_net_pnl"), -math.inf)
            + 0.5 * fnum(r.get("validation_leave_top5_out_net_pnl"), -math.inf)
            + fnum(r.get("validation_delta_vs_hold"), -math.inf)
            + 25.0 * fnum(r.get("validation_roundtrip_2x_loss_saved_to_profit_count"))
            - 1000.0 * fnum(r.get("validation_max_city_abs_pnl_share")),
            fnum(r.get("validation_net_pnl"), -math.inf),
        ),
        reverse=True,
    )
    for rank, row in enumerate(robust_sorted[:3], 1):
        out.append(candidate_row("most_robust", rank, row, "综合八折净利润、剔除Top5后收益、相对完全持有增益、挽救2x后归零案例和城市集中度"))
    simple_ids = ["combo_2x_sell50_hold", "tp_2_0x_sell_50pct", "combo_3x_sell50_hold", "recover_principal_keep_free", "ladder_1_5x25_2x25_3x25_hold"]
    simple = [r for r in candidates if r["rule_id"] in simple_ids]
    simple.sort(key=lambda r: (simple_ids.index(r["rule_id"]), -fnum(r.get("validation_net_pnl"), -math.inf)))
    for rank, row in enumerate(simple[:3], 1):
        out.append(candidate_row("simplest_executable", rank, row, "优先选择触发条件少、容易执行且通过稳健性过滤的规则"))
    effective08 = [r for r in rows if fnum(r.get("validation_0_8_net_pnl")) > 0 and r["rule_id"] not in {"actual_huskyvs_exit", "hold_to_settlement"}]
    effective08.sort(key=lambda r: fnum(r.get("validation_0_8_net_pnl"), -math.inf), reverse=True)
    for rank, row in enumerate(effective08[:10], 1):
        out.append(candidate_row("haircut_0_8_still_positive", rank, row, "八折可成交价代理下验证集仍为正收益"))
    return out


def candidate_row(candidate_type: str, rank: int, row: dict[str, Any], notes: str) -> dict[str, Any]:
    return {
        "candidate_type": candidate_type,
        "rank": rank,
        "rule_id": row.get("rule_id", ""),
        "rule_family": row.get("rule_family", ""),
        "rule_description": row.get("rule_description", ""),
        "validation_net_pnl": row.get("validation_net_pnl", ""),
        "validation_roi": row.get("validation_roi", ""),
        "validation_0_8_net_pnl": row.get("validation_0_8_net_pnl", ""),
        "validation_delta_vs_hold": row.get("validation_delta_vs_hold", ""),
        "validation_leave_top5_out_net_pnl": row.get("validation_leave_top5_out_net_pnl", ""),
        "validation_saved_loser_to_profit_count": row.get("validation_saved_loser_to_profit_count", ""),
        "validation_roundtrip_2x_loss_saved_to_profit_count": row.get("validation_roundtrip_2x_loss_saved_to_profit_count", ""),
        "validation_premature_correct_sell_loss_count": row.get("validation_premature_correct_sell_loss_count", ""),
        "validation_max_city_abs_pnl_share": row.get("validation_max_city_abs_pnl_share", ""),
        "selection_notes": notes,
    }


def lookup_metric(rows: list[dict[str, Any]], rule_id: str, split: str = "validation", scenario: str = "sampled_1_0", group_type: str = "all_yes", group: str = "all_yes") -> dict[str, Any]:
    for row in rows:
        if row["rule_id"] == rule_id and row["split"] == split and row["price_scenario"] == scenario and row["group_type"] == group_type and row["group"] == group:
            return row
    return {}


def best_group(rows: list[dict[str, Any]], rule_id: str, group_type: str) -> dict[str, Any]:
    candidates = [r for r in rows if r["rule_id"] == rule_id and r["split"] == "validation" and r["price_scenario"] == "sampled_1_0" and r["group_type"] == group_type]
    return max(candidates, key=lambda r: fnum(r.get("net_pnl"), -math.inf), default={})


def generate_report(
    path: Path,
    summary: dict[str, Any],
    grid_rows: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    lead_rows: list[dict[str, Any]],
    top_rows: list[dict[str, Any]],
) -> None:
    def val(rule_id: str, scenario: str = "sampled_1_0") -> dict[str, Any]:
        return lookup_metric(grid_rows, rule_id, "validation", scenario)

    hold = val("hold_to_settlement")
    actual = val("actual_huskyvs_exit")
    two50 = val("tp_2_0x_sell_50pct")
    three50 = val("tp_3_0x_sell_50pct")
    principal = val("recover_principal_keep_free")
    premature_rule = max(
        [r for r in validation if r["rule_id"] != "actual_huskyvs_exit"],
        key=lambda r: fnum(r.get("validation_premature_correct_sell_loss_count"), -math.inf),
        default={},
    )
    roundtrip_rule = max(
        [r for r in validation if r["rule_id"] not in {"actual_huskyvs_exit", "hold_to_settlement"}],
        key=lambda r: fnum(r.get("validation_roundtrip_2x_loss_saved_to_profit_count"), -math.inf),
        default={},
    )
    profit_top = [r for r in top_rows if r["candidate_type"] == "highest_validation_profit"][:3]
    robust_top = [r for r in top_rows if r["candidate_type"] == "most_robust"][:1]
    simple_top = [r for r in top_rows if r["candidate_type"] == "simplest_executable"][:1]
    effective08 = [r for r in top_rows if r["candidate_type"] == "haircut_0_8_still_positive"]
    robust_rule_id = (robust_top[0]["rule_id"] if robust_top else (profit_top[0]["rule_id"] if profit_top else "hold_to_settlement"))
    best_price = best_group(price_rows, robust_rule_id, "entry_price_bin")
    best_lead = best_group(lead_rows, robust_rule_id, "entry_lead_bin")
    transform = summary["transform_summary"]

    lines = [
        "# HUSKYVS_EXIT_RULE_BACKTEST_v4",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Scope And Leakage Controls",
        "",
        f"- 主样本使用 v2 修正后的全部可评估天气 YES 非 transform 仓位：{summary['main_positions']} 个。",
        f"- 排除 transform 影响仓位 {summary['transform_positions']} 个，另行汇总；排除首次买入晚于当地天气日结束的不可评估仓位 {summary['excluded_after_local_end']} 个。",
        f"- 按城市-日期-指标-单位事件做时间顺序切分：训练事件 {summary['train_events']} 个、验证事件 {summary['validation_events']} 个；同一城市-日期所有温度档没有跨集合。",
        f"- 规则只使用第一笔买入后、当地天气日结束前已经出现的官方 prices-history 价格；最终结算只用于评价 PnL，不用于选择卖出时点。",
        f"- 官方价格历史来源：{PRICE_HISTORY_DOCS_URL}；本回测是采样价格与折价可成交性敏感性测试，不是历史订单簿回放。",
        "",
        "## Headline Validation Results",
        "",
        f"- 完全持有验证集 PnL：{fmt_money(hold.get('net_pnl'))}，ROI {fmt_pct(hold.get('roi_on_buy_usd'))}。",
        f"- huskyvs 实际退出验证集 PnL：{fmt_money(actual.get('net_pnl'))}，ROI {fmt_pct(actual.get('roi_on_buy_usd'))}。",
        f"- 2x卖50%验证集 PnL：{fmt_money(two50.get('net_pnl'))}，相对完全持有 {fmt_money(two50.get('delta_vs_hold_pnl'))}。",
        f"- 3x卖50%验证集 PnL：{fmt_money(three50.get('net_pnl'))}，相对完全持有 {fmt_money(three50.get('delta_vs_hold_pnl'))}。",
        f"- 收回本金保留免费仓位验证集 PnL：{fmt_money(principal.get('net_pnl'))}，八折情景 PnL {fmt_money(val('recover_principal_keep_free', 'haircut_0_8').get('net_pnl'))}。",
        "",
        "## Required Answers",
        "",
        f"**1. 2倍卖50%是否真的优于完全持有？** {'是' if fnum(two50.get('delta_vs_hold_pnl')) > 0 else '否'}。验证集相对完全持有差额为 {fmt_money(two50.get('delta_vs_hold_pnl'))}；八折情景差额为 {fmt_money(val('tp_2_0x_sell_50pct', 'haircut_0_8').get('delta_vs_hold_pnl'))}。",
        "",
        f"**2. 3倍卖出是否在全样本验证集中仍然有效？** 3x卖50%验证集 PnL 为 {fmt_money(three50.get('net_pnl'))}，八折情景为 {fmt_money(val('tp_3_0x_sell_50pct', 'haircut_0_8').get('net_pnl'))}。{'仍为正收益。' if fnum(val('tp_3_0x_sell_50pct', 'haircut_0_8').get('net_pnl')) > 0 else '折价后未能保持正收益。'}",
        "",
        f"**3. 收回本金、保留免费仓位是否更稳健？** 验证集原始/九折/八折 PnL 分别为 {fmt_money(principal.get('net_pnl'))} / {fmt_money(val('recover_principal_keep_free', 'haircut_0_9').get('net_pnl'))} / {fmt_money(val('recover_principal_keep_free', 'haircut_0_8').get('net_pnl'))}，剔除前5大赢家后为 {fmt_money(principal.get('leave_top5_out_net_pnl'))}。",
        "",
        f"**4. 哪种规则最能避免涨到2-3倍后重新归零？** `{roundtrip_rule.get('rule_id', 'n/a')}` 在验证集中把 {roundtrip_rule.get('validation_roundtrip_2x_loss_saved_to_profit_count', '0')} 个曾到2x但完全持有亏损的仓位挽救为盈利。",
        "",
        f"**5. 哪种规则最容易造成预测正确却过早卖出？** `{premature_rule.get('rule_id', 'n/a')}` 的验证集过早卖出损失计数最高，为 {premature_rule.get('validation_premature_correct_sell_loss_count', '0')} 个。",
        "",
        f"**6. 10-20美分是否仍是最强价格档？** 在稳健候选 `{robust_rule_id}` 下，验证集最强价格档为 `{best_price.get('group', 'n/a')}`，PnL {fmt_money(best_price.get('net_pnl'))}。{'因此10-20c仍成立。' if best_price.get('group') == '10-20c' else '因此10-20c不是该口径下最强档。'}",
        "",
        f"**7. 12-24小时是否仍是最稳健入场窗口？** 在稳健候选 `{robust_rule_id}` 下，验证集最强入场窗口为 `{best_lead.get('group', 'n/a')}`，PnL {fmt_money(best_lead.get('net_pnl'))}。{'因此12-24h仍成立。' if best_lead.get('group') == '12-24h' else '因此12-24h不是该口径下最强窗口。'}",
        "",
        "**8. 下一阶段最多3条候选退出规则。**",
        "",
    ]
    selected_rules = []
    if robust_top:
        selected_rules.append(robust_top[0])
    if simple_top and simple_top[0]["rule_id"] not in {r["rule_id"] for r in selected_rules}:
        selected_rules.append(simple_top[0])
    for row in profit_top:
        if row["rule_id"] not in {r["rule_id"] for r in selected_rules}:
            selected_rules.append(row)
        if len(selected_rules) >= 3:
            break
    for idx, row in enumerate(selected_rules[:3], 1):
        lines.append(f"{idx}. `{row['rule_id']}`：验证集 PnL {fmt_money(row['validation_net_pnl'])}，八折 PnL {fmt_money(row['validation_0_8_net_pnl'])}，相对完全持有 {fmt_money(row['validation_delta_vs_hold'])}。")

    lines.extend(
        [
            "",
            "## Top Validation Profit Rules",
            "",
            markdown_table(
                [
                    {
                        "rule": r["rule_id"],
                        "pnl": fmt_money(r["validation_net_pnl"]),
                        "roi": fmt_pct(r["validation_roi"]),
                        "h08": fmt_money(r["validation_0_8_net_pnl"]),
                        "delta": fmt_money(r["validation_delta_vs_hold"]),
                    }
                    for r in profit_top
                ],
                [("Rule", "rule"), ("Validation PnL", "pnl"), ("ROI", "roi"), ("0.8x PnL", "h08"), ("Delta vs Hold", "delta")],
            ),
            "",
            "## Transform-Affected Positions",
            "",
            f"- Transform YES positions excluded from main rule grid: {transform['positions']}；actual v2 PnL {fmt_money(transform['actual_pnl'])}，buy nominal {fmt_money(transform['buy_usd'])}，ROI {fmt_pct(safe_div(transform['actual_pnl'], transform['buy_usd']))}。",
            "",
            "## Data Integrity",
            "",
            f"- Main backtest positions with official pre-end history: {summary['main_positions']} / candidate non-transform YES positions {summary['candidate_nontransform_yes']}。",
            f"- Price-history missing or empty before local end: {summary['history_missing_or_empty']}。",
            f"- No-future-sell violations detected by simulator: {summary['no_future_sell_violations']}。",
            "- The drawdown metric is PnL sequence drawdown ordered by weather event date; it is not a real account maximum drawdown.",
            "",
            "## Data Gaps",
            "",
            "- prices-history is sampled midpoint/price history, not full historical order book depth; haircuts at 0.9x and 0.8x are liquidity sensitivity proxies only.",
            "- Open orders, cancellations, queue position, maker/taker intent, and available size at each sampled price remain unrecoverable.",
            "- Local weather-day end is a conservative cutoff; real market resolution and information availability may differ by market.",
            "- Transform-affected positions are excluded from the main simulator because split/merge/conversion changes token accounting.",
            "- This is still historical backtesting on one wallet; rules require forward simulation before any operational use.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def package_outputs(root: Path, zip_path: Path) -> dict[str, Any]:
    include_files = [
        "reports/HUSKYVS_EXIT_RULE_BACKTEST_v4.md",
        "data/exit_rule_grid_v4.csv",
        "data/exit_rule_validation_v4.csv",
        "data/exit_rule_price_haircut_v4.csv",
        "data/exit_rule_by_price_bin_v4.csv",
        "data/exit_rule_by_entry_time_v4.csv",
        "data/exit_rule_position_detail_v4.csv",
        "data/exit_rule_top_candidates_v4.csv",
        "data/exit_rule_integrity_v4.json",
        "src/backtest_exit_rules_v4.py",
        "tests/test_exit_rules_v4.py",
    ]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in include_files:
            path = root / rel
            if path.exists():
                zf.write(path, rel)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        count = len(zf.infolist())
    return {"zip_path": str(zip_path), "zip_size_bytes": zip_path.stat().st_size, "zip_file_count": count, "zip_testzip_bad_file": bad}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--fidelity", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--zip", type=Path, default=Path("huskyvs_exit_rule_backtest_v4.zip"))
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()

    root = args.root
    lifecycle = read_csv(root / "data/processed_v2/corrected_position_lifecycle.csv")
    trades = read_csv(root / "data/raw/trades.csv")
    fills_by_asset = normalize_fills(trades)

    candidate_yes = [
        dict(r)
        for r in lifecycle
        if r.get("settled_sample_v2") == "True"
        and r.get("outcome") == "Yes"
        and fnum(r.get("buy_usd")) > 0
        and fnum(r.get("buy_shares")) > 0
        and finite(fnum(r.get("settlement_price_v2"), math.nan))
        and r.get("asset") in fills_by_asset
    ]
    transform_rows = [r for r in candidate_yes if r.get("transform_affected") == "True"]
    nontransform_candidates = [r for r in candidate_yes if r.get("transform_affected") != "True"]
    after_local_end = [r for r in nontransform_candidates if parse_ts(r.get("first_buy_ts")) >= parse_ts(r.get("local_weather_day_end_epoch"))]
    nontransform_pre_end = [r for r in nontransform_candidates if parse_ts(r.get("first_buy_ts")) < parse_ts(r.get("local_weather_day_end_epoch"))]

    histories = ensure_price_histories(nontransform_pre_end, root / "data/price_history_cache_v4", args.fidelity, args.workers, args.user_agent, args.skip_fetch)
    for row in nontransform_pre_end:
        cached = histories.get(row["asset"], {"status": "missing_cache", "history": []})
        points = history_pre_end(row, cached)
        row["history_status"] = cached.get("status", "missing_cache")
        row["history_points_pre_end"] = len(points)
    main_rows = [r for r in nontransform_pre_end if r.get("history_status") == "ok" and int(r.get("history_points_pre_end", 0)) > 0]

    train_events, validation_events = split_events(main_rows)
    structures = classify_position_structure(main_rows)
    for row in main_rows:
        row["event_key"] = event_key(row)
        row["split"] = "train" if row["event_key"] in train_events else "validation"
        row["position_structure"] = structures.get(row["asset"], "unknown")

    rules = build_rules()
    rule_by_id = {r.rule_id: r for r in rules}
    detail_rows: list[dict[str, Any]] = []
    for row in main_rows:
        cached = histories[row["asset"]]
        points = history_pre_end(row, cached)
        buy_fills = [f for f in fills_by_asset[row["asset"]] if f["side"] == "BUY" and f["timestamp"] <= parse_ts(row.get("local_weather_day_end_epoch"))]
        for rule in rules:
            for scenario, haircut in SCENARIOS:
                result = simulate_rule(row, buy_fills, points, rule, scenario, haircut)
                detail_rows.append(details_for_position(row, rule, scenario, haircut, result, row["history_status"], len(points)))

    grid_rows, haircut_rows, price_rows, lead_rows = build_metrics(rule_by_id, detail_rows)
    validation = validation_rows(grid_rows)
    top_rows = top_candidates(validation)

    write_csv(root / "data/exit_rule_position_detail_v4.csv", detail_rows, DETAIL_FIELDS)
    write_csv(root / "data/exit_rule_grid_v4.csv", grid_rows, METRIC_FIELDS)
    write_csv(root / "data/exit_rule_validation_v4.csv", validation, VALIDATION_FIELDS)
    write_csv(root / "data/exit_rule_price_haircut_v4.csv", haircut_rows, METRIC_FIELDS)
    write_csv(root / "data/exit_rule_by_price_bin_v4.csv", price_rows, METRIC_FIELDS)
    write_csv(root / "data/exit_rule_by_entry_time_v4.csv", lead_rows, METRIC_FIELDS)
    write_csv(root / "data/exit_rule_top_candidates_v4.csv", top_rows, TOP_CANDIDATE_FIELDS)

    no_future_violations = sum(1 for r in detail_rows if str(r.get("no_future_sell_violation")) == "True")
    transform_summary = {
        "positions": len(transform_rows),
        "buy_usd": sum(fnum(r.get("buy_usd")) for r in transform_rows),
        "actual_pnl": sum(fnum(r.get("realized_pnl_v2")) for r in transform_rows),
    }
    splits_by_event: dict[str, set[str]] = defaultdict(set)
    for row in main_rows:
        splits_by_event[row["event_key"]].add(row["split"])
    integrity = {
        "candidate_yes": len(candidate_yes),
        "candidate_nontransform_yes": len(nontransform_candidates),
        "transform_positions": len(transform_rows),
        "excluded_after_local_end": len(after_local_end),
        "main_positions": len(main_rows),
        "history_missing_or_empty": len(nontransform_pre_end) - len(main_rows),
        "train_events": len(train_events),
        "validation_events": len(validation_events),
        "train_positions": sum(1 for r in main_rows if r["split"] == "train"),
        "validation_positions": sum(1 for r in main_rows if r["split"] == "validation"),
        "same_event_split_check": all(len(splits) == 1 for splits in splits_by_event.values()),
        "no_future_sell_violations": no_future_violations,
        "rules": len(rules),
        "scenarios": len(SCENARIOS),
        "detail_rows": len(detail_rows),
        "price_history_endpoint": PRICE_HISTORY_URL,
        "batch_price_history_endpoint": BATCH_PRICE_HISTORY_URL,
        "price_history_docs": PRICE_HISTORY_DOCS_URL,
        "transform_summary": transform_summary,
    }
    write_json(root / "data/exit_rule_integrity_v4.json", integrity)

    generate_report(root / "reports/HUSKYVS_EXIT_RULE_BACKTEST_v4.md", integrity, grid_rows, validation, price_rows, lead_rows, top_rows)
    write_json(root / "data/exit_rule_integrity_v4.json", integrity)
    zip_info = package_outputs(root, root / args.zip)

    print(json.dumps({**integrity, "delivery_zip": zip_info}, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
