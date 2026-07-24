#!/usr/bin/env python3
"""v5.1.2 read-only live integration harness."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

try:
    from src.polymarket_public_adapter_v5_1_2 import (
        ADAPTER_VERSION,
        CLOB_BASE,
        GAMMA_BASE,
        AdapterError,
        PublicAdapter,
        content_hash,
        fnum,
        is_weather_market,
        normalize_orderbook,
        official_fee,
        parse_market_status,
        parse_weather_market,
        simulate_buy_vwap,
        simulate_sell_vwap,
        stable_json,
        token_mapping_from_market,
        write_json,
    )
except ModuleNotFoundError:
    from polymarket_public_adapter_v5_1_2 import (
        ADAPTER_VERSION,
        CLOB_BASE,
        GAMMA_BASE,
        AdapterError,
        PublicAdapter,
        content_hash,
        fnum,
        is_weather_market,
        normalize_orderbook,
        official_fee,
        parse_market_status,
        parse_weather_market,
        simulate_buy_vwap,
        simulate_sell_vwap,
        stable_json,
        token_mapping_from_market,
        write_json,
    )


VERSION = "forward_simulation_v5.1.2-live-integration"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE_SUBDIR = "data/forward_v5_1_2/live_integration"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, out)]
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if text.startswith("- "):
            parent = stack[-1][1]
            key = "__list__"
            parent.setdefault(key, []).append(parse_scalar(text[2:]))
            continue
        key, _, value = text.partition(":")
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value.strip())
    return normalize_lists(out)


def normalize_lists(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"__list__"}:
            return value["__list__"]
        return {k: normalize_lists(v) for k, v in value.items()}
    return value


def parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


def live_dir(root: Path, config: dict[str, Any]) -> Path:
    rel = config.get("paths", {}).get("live_integration_dir", LIVE_SUBDIR)
    return root / str(rel)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(stable_json(payload) + "\n")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_temperature(text: str) -> tuple[str, str]:
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(°?\s*[FC]|degrees?\s*[FC])", text, re.I)
    if not m:
        return "", ""
    return m.group(1), ("F" if "f" in m.group(2).lower() else "C")


def event_key(city: str, date: str, metric: str) -> str:
    return "|".join([" ".join(city.lower().split()), date, " ".join(metric.lower().split())])


def market_prices(market: dict[str, Any]) -> list[float]:
    raw = market.get("outcomePrices")
    if isinstance(raw, list):
        return [fnum(x, math.nan) for x in raw]
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
            if isinstance(payload, list):
                return [fnum(x, math.nan) for x in payload]
        except json.JSONDecodeError:
            return []
    return []


def market_score(market: dict[str, Any]) -> float:
    return max(fnum(market.get("liquidityNum"), 0), fnum(market.get("volume24hr"), 0), fnum(market.get("volumeNum"), 0))


def markets_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        copied = dict(market)
        copied["_event_title"] = event.get("title") or ""
        copied["_event_slug"] = event.get("slug") or ""
        copied["_event_id"] = event.get("id") or ""
        copied["_event_end_date"] = event.get("endDate") or event.get("endDateIso") or ""
        rows.append(copied)
    return rows


def market_is_live_tradable(market: dict[str, Any]) -> bool:
    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    resolved = bool(market.get("resolved") or market.get("automaticallyResolved"))
    accepting = market.get("acceptingOrders")
    return active and not closed and not resolved and accepting is not False


def search_terms_for_weather() -> list[str]:
    tomorrow = utcnow().date() + timedelta(days=1)
    month_day = tomorrow.strftime("%B %-d") if hasattr(tomorrow, "strftime") else ""
    return [
        f"{month_day} temperature",
        f"highest temperature {month_day}",
        f"temperature {month_day}",
        f"weather {month_day}",
        "highest temperature in",
        "temperature",
        "weather",
    ]


def discover_weather_markets(adapter: PublicAdapter, max_pages: int = 2, page_size: int = 50) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_responses = []
    markets: list[dict[str, Any]] = []
    seen = set()
    for term in search_terms_for_weather():
        try:
            res = adapter.search(term, limit_per_type=10)
            raw_responses.append({"endpoint": res.url, "status_code": res.status_code, "payload": res.payload})
            payload = res.payload if isinstance(res.payload, dict) else {}
            for event in payload.get("events") or []:
                event_title = str(event.get("title") or "")
                for market in markets_from_event(event):
                    slug = market.get("slug")
                    if slug and slug not in seen and market_is_live_tradable(market) and is_weather_market(market, event_title):
                        markets.append(market)
                        seen.add(slug)
        except AdapterError as exc:
            raw_responses.append({"endpoint": exc.endpoint, "error": exc.category, "message": str(exc), "search_term": term})
        if markets:
            break
    if markets:
        return markets, raw_responses

    try:
        res = adapter.list_events(limit=100, offset=0)
        raw_responses.append({"endpoint": res.url, "status_code": res.status_code, "payload": res.payload})
        payload = res.payload if isinstance(res.payload, list) else res.payload.get("events", [])
        for event in payload:
            event_title = str(event.get("title") or "")
            for market in markets_from_event(event):
                slug = market.get("slug")
                if slug and slug not in seen and market_is_live_tradable(market) and is_weather_market(market, event_title):
                    markets.append(market)
                    seen.add(slug)
    except AdapterError as exc:
        raw_responses.append({"endpoint": exc.endpoint, "error": exc.category, "message": str(exc), "fallback": "events"})
    if markets:
        return markets, raw_responses

    for offset in range(0, max_pages * page_size, page_size):
        try:
            res = adapter.list_markets(limit=page_size, offset=offset)
            raw_responses.append({"endpoint": res.url, "status_code": res.status_code, "payload": res.payload})
            payload = res.payload if isinstance(res.payload, list) else res.payload.get("markets", [])
            if not payload:
                break
            for market in payload:
                slug = market.get("slug")
                if slug and slug not in seen and market_is_live_tradable(market) and is_weather_market(market):
                    markets.append(market)
                    seen.add(slug)
        except AdapterError as exc:
            raw_responses.append({"endpoint": exc.endpoint, "error": exc.category, "message": str(exc)})
            break
    return markets, raw_responses


def choose_markets(adapter: PublicAdapter, root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    out_dir = live_dir(root, config)
    markets, raw = discover_weather_markets(adapter)
    write_json(out_dir / "raw_market_discovery.json", raw)
    beijing = [m for m in markets if "beijing" in " ".join(str(m.get(k, "")) for k in ["question", "slug"]).lower() and "july-22" in str(m.get("slug", "")).lower()]
    selected_markets: list[dict[str, Any]] = []
    if beijing and bool(beijing[0].get("active")) and not bool(beijing[0].get("closed")):
        selected_markets.extend(beijing[:1])
    if len(selected_markets) < 2:
        by_event: dict[str, list[dict[str, Any]]] = {}
        for market in markets:
            key = str(market.get("_event_slug") or market.get("eventSlug") or market.get("slug") or "")
            by_event.setdefault(key, []).append(market)
        candidate_groups = sorted(by_event.values(), key=lambda group: max(market_score(m) for m in group), reverse=True)
        for group in candidate_groups:
            if len(group) < 2:
                continue
            prices_by_market = [(m, market_prices(m)) for m in group]
            high = max(group, key=market_score)
            low_candidates = [
                m for m, prices in prices_by_market
                if any(math.isfinite(p) and p <= 0.10 for p in prices) and m.get("slug") != high.get("slug")
            ]
            low = max(low_candidates, key=market_score) if low_candidates else next((m for m in group if m.get("slug") != high.get("slug")), None)
            for market in [high, low]:
                if market and market.get("slug") not in {m.get("slug") for m in selected_markets}:
                    selected_markets.append(market)
            if len(selected_markets) >= 2:
                break
    if len(selected_markets) < 2:
        ranked = sorted(markets, key=market_score, reverse=True)
        for market in ranked:
            if market.get("slug") not in {m.get("slug") for m in selected_markets}:
                selected_markets.append(market)
            if len(selected_markets) >= 2:
                break
    if len(selected_markets) < 2:
        raise RuntimeError("fewer than two active weather markets found")

    rows: list[dict[str, Any]] = []
    for market in selected_markets[:3]:
        slug = str(market.get("slug", ""))
        by_slug = adapter.market_by_slug(slug).payload if slug else market
        condition_id = str(by_slug.get("conditionId") or by_slug.get("condition_id") or market.get("conditionId") or "")
        clob_info = {}
        try:
            clob_info = adapter.clob_market_info(condition_id).payload if condition_id else {}
        except AdapterError as exc:
            clob_info = {"error": exc.category, "message": str(exc)}
        mapping = token_mapping_from_market(by_slug, clob_info)
        prices = market_prices(by_slug)
        chosen_idx = 0
        if prices:
            low = [(i, p) for i, p in enumerate(prices) if math.isfinite(p) and p <= 0.10]
            chosen_idx = low[0][0] if low else max(range(len(prices)), key=lambda i: prices[i] if math.isfinite(prices[i]) else -1)
        event_title = str(market.get("_event_title") or by_slug.get("title") or by_slug.get("eventTitle") or "")
        info = parse_weather_market(by_slug, event_title)
        status = parse_market_status(by_slug)
        if chosen_idx >= len(mapping):
            chosen_idx = 0
        token = mapping[chosen_idx] if mapping else {"outcome": "", "token_id": ""}
        rows.append({
            "selected_at_utc": iso(),
            "event_title": event_title or by_slug.get("title") or by_slug.get("question") or "",
            "city": info["city"],
            "weather_date_local": info["weather_date_local"],
            "weather_metric": info["weather_metric"],
            "market_slug": slug,
            "condition_id": condition_id,
            "outcome_label": token.get("outcome", ""),
            "token_id": token.get("token_id", ""),
            "active": status["active"],
            "closed": status["closed"],
            "resolved": status["resolved"],
            "fees_enabled": by_slug.get("feesEnabled"),
            "selection_reason": "beijing_july_22_priority" if market in beijing else "active_weather_liquidity_or_low_price",
            "raw_market": by_slug,
            "clob_info": clob_info,
        })
    write_json(out_dir / "selected_markets.json", rows)
    return rows


def validate_token_mapping(markets: list[dict[str, Any]], out_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for item in markets:
        raw = item.get("raw_market", {})
        clob_info = item.get("clob_info", {})
        mapping = token_mapping_from_market(raw, clob_info if isinstance(clob_info, dict) else {})
        question = str(raw.get("question") or item.get("event_title") or "")
        temp, unit = parse_temperature(" ".join([question, str(raw.get("groupItemTitle") or ""), item.get("outcome_label", "")]))
        info = parse_weather_market(raw, item.get("event_title", ""))
        ek = event_key(info["city"], info["weather_date_local"], info["weather_metric"])
        for m in mapping:
            valid = bool(m.get("token_id")) and bool(item.get("condition_id")) and m.get("outcome") in [x.get("outcome") for x in mapping]
            rows.append({
                "market_slug": item["market_slug"],
                "condition_id": item["condition_id"],
                "outcome_label": m.get("outcome", ""),
                "side": "YES" if str(m.get("outcome", "")).lower() == "yes" else ("NO" if str(m.get("outcome", "")).lower() == "no" else ""),
                "token_id": m.get("token_id", ""),
                "parsed_temperature": temp,
                "parsed_unit": unit,
                "event_key": ek,
                "mapping_valid": str(valid).lower(),
                "error_message": "" if valid else "missing token_id or condition_id",
            })
    write_csv(out_dir / "token_mapping_validation.csv", ["market_slug", "condition_id", "outcome_label", "side", "token_id", "parsed_temperature", "parsed_unit", "event_key", "mapping_valid", "error_message"], rows)
    return rows


def fee_rate_from_market(item: dict[str, Any]) -> tuple[bool | None, float | None, float | None, str]:
    market = item.get("raw_market", {})
    clob = item.get("clob_info", {})
    enabled = market.get("feesEnabled")
    schedule = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    rate = fnum(schedule.get("rate"), math.nan)
    exp = fnum(schedule.get("exponent"), math.nan)
    source = "gamma.feeSchedule"
    if math.isfinite(rate) and rate > 1:
        rate = rate / 10000.0
    if not math.isfinite(rate) and isinstance(clob, dict):
        fd = clob.get("fd") if isinstance(clob.get("fd"), dict) else {}
        rate = fnum(fd.get("r"), math.nan)
        exp = fnum(fd.get("e"), math.nan)
        source = "clob.fd"
        if math.isfinite(rate) and rate > 1:
            rate = rate / 10000.0
    if not math.isfinite(rate) and isinstance(clob, dict):
        tbf = fnum(clob.get("tbf"), math.nan)
        if math.isfinite(tbf):
            rate = tbf / 10000.0 if tbf > 1 else tbf
            source = "clob.tbf"
    if not math.isfinite(exp):
        exp = 1.0
    return (enabled if isinstance(enabled, bool) else None, rate if math.isfinite(rate) else None, exp, source)


def save_orderbook_snapshot(out_dir: Path, item: dict[str, Any], result: Any, raw_index: int) -> dict[str, Any]:
    raw_dir = out_dir / "raw_orderbooks"
    token_id = item["token_id"]
    raw_path = raw_dir / f"{raw_index:05d}_{token_id}.json"
    write_json(raw_path, {"http": {"url": result.url, "status_code": result.status_code, "latency_ms": result.latency_ms, "request_started_at_utc": result.started_at_utc, "response_received_at_utc": result.received_at_utc}, "raw": result.payload})
    normalized = normalize_orderbook(result.payload)
    snapshot_id = "live_" + content_hash({"token_id": token_id, "hash": normalized.get("hash"), "content": normalized["content_hash"]})[:24]
    row = {
        "request_started_at_utc": result.started_at_utc,
        "response_received_at_utc": result.received_at_utc,
        "server_timestamp": normalized.get("timestamp"),
        "market_slug": item["market_slug"],
        "condition_id": item["condition_id"],
        "token_id": token_id,
        "bids": normalized["bids"],
        "asks": normalized["asks"],
        "best_bid": normalized["best_bid"],
        "best_ask": normalized["best_ask"],
        "spread": normalized["spread"],
        "bid_depth_levels": normalized["bid_depth_levels"],
        "ask_depth_levels": normalized["ask_depth_levels"],
        "total_bid_shares": normalized["total_bid_shares"],
        "total_ask_shares": normalized["total_ask_shares"],
        "normalized_content_hash": normalized["content_hash"],
        "snapshot_id": snapshot_id,
        "http_status": result.status_code,
        "request_latency_ms": result.latency_ms,
        "adapter_version": ADAPTER_VERSION,
        "empty": normalized["empty"],
        "raw_file": str(raw_path),
    }
    append_jsonl(out_dir / "orderbook_snapshots.jsonl", row)
    return row


def collect_orderbook_round(adapter: PublicAdapter, out_dir: Path, markets: list[dict[str, Any]], raw_index: int) -> tuple[list[dict[str, Any]], int]:
    snapshots = []
    for item in markets:
        try:
            result = adapter.orderbook(item["token_id"])
            raw_index += 1
            snapshots.append(save_orderbook_snapshot(out_dir, item, result, raw_index))
        except AdapterError as exc:
            append_jsonl(out_dir / "adapter_audit_log.jsonl", {"created_at_utc": iso(), "event_type": "orderbook_error", "token_id": item["token_id"], "category": exc.category, "message": str(exc), "status_code": exc.status_code})
    return snapshots, raw_index


def write_vwap_rows(out_dir: Path, snapshot_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    buy_sizes = config.get("live_rehearsal", {}).get("virtual_buy_usd", [1, 5, 10, 25, 50])
    sell_sizes = config.get("live_rehearsal", {}).get("virtual_sell_shares", [10, 50, 100])
    unique_snapshots = []
    seen_ids = set()
    for snap in snapshot_rows:
        if snap["snapshot_id"] in seen_ids:
            continue
        seen_ids.add(snap["snapshot_id"])
        unique_snapshots.append(snap)
    manual_snapshots = {snap["snapshot_id"] for snap in unique_snapshots[:5]}
    for snap in unique_snapshots:
        book = {"asks": snap["asks"], "bids": snap["bids"], "best_ask": snap["best_ask"], "best_bid": snap["best_bid"]}
        for size in buy_sizes:
            calc = simulate_buy_vwap(book, float(size))
            rows.append({"snapshot_id": snap["snapshot_id"], "token_id": snap["token_id"], "action": "buy", "intended_usd_or_shares": size, "filled_usd": calc["filled_usd"], "filled_shares": calc["filled_shares"], "vwap": calc["vwap"], "best_price": calc["best_price"], "slippage_vs_best": calc["slippage_vs_best"], "depth_levels_consumed": calc["depth_levels_consumed"], "fully_filled": str(calc["fully_filled"]).lower(), "unfilled_amount": calc["unfilled_amount"], "calculation_valid": str(bool(calc["filled_shares"] or calc["unfilled_amount"])).lower(), "manual_check": "yes" if snap["snapshot_id"] in manual_snapshots else ""})
        for size in sell_sizes:
            calc = simulate_sell_vwap(book, float(size))
            rows.append({"snapshot_id": snap["snapshot_id"], "token_id": snap["token_id"], "action": "sell", "intended_usd_or_shares": size, "filled_usd": calc["filled_usd"], "filled_shares": calc["filled_shares"], "vwap": calc["vwap"], "best_price": calc["best_price"], "slippage_vs_best": calc["slippage_vs_best"], "depth_levels_consumed": calc["depth_levels_consumed"], "fully_filled": str(calc["fully_filled"]).lower(), "unfilled_amount": calc["unfilled_amount"], "calculation_valid": str(bool(calc["filled_shares"] or calc["unfilled_amount"])).lower(), "manual_check": "yes" if snap["snapshot_id"] in manual_snapshots else ""})
    fields = ["snapshot_id", "token_id", "action", "intended_usd_or_shares", "filled_usd", "filled_shares", "vwap", "best_price", "slippage_vs_best", "depth_levels_consumed", "fully_filled", "unfilled_amount", "calculation_valid", "manual_check"]
    write_csv(out_dir / "live_vwap_validation.csv", fields, rows)
    return rows


def write_fee_rows(out_dir: Path, markets: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    by_token = {m["token_id"]: m for m in markets}
    unique_snapshots = []
    seen_ids = set()
    for snap in snapshots:
        if snap["snapshot_id"] in seen_ids:
            continue
        seen_ids.add(snap["snapshot_id"])
        unique_snapshots.append(snap)
    for snap in unique_snapshots[:20]:
        item = by_token.get(snap["token_id"])
        if not item:
            continue
        enabled, rate, exp, source = fee_rate_from_market(item)
        price = snap["best_ask"] if snap["best_ask"] is not None else (snap["best_bid"] or 0)
        shares = 10.0
        calc = official_fee(shares, price, enabled, rate, exp)
        rows.append({"token_id": snap["token_id"], "market_slug": snap["market_slug"], "fees_enabled": enabled, "fee_parameter_source": source, "fee_rate_parameter": rate, "fee_exponent_observed": calc["fee_exponent_observed"], "fee_formula_version": "fee=shares*fee_rate*price*(1-price)", "gross_notional": calc["gross_notional"], "official_fee": calc["official_fee"], "fallback_fee": calc["fallback_fee"], "fee_status": calc["fee_status"], "net_proceeds_or_cost": calc["net_proceeds_or_cost"]})
    if not rows:
        for item in markets:
            enabled, rate, exp, source = fee_rate_from_market(item)
            calc = official_fee(10.0, 0.5, enabled, rate, exp)
            rows.append({"token_id": item["token_id"], "market_slug": item["market_slug"], "fees_enabled": enabled, "fee_parameter_source": source, "fee_rate_parameter": rate, "fee_exponent_observed": calc["fee_exponent_observed"], "fee_formula_version": "fee=shares*fee_rate*price*(1-price)", "gross_notional": calc["gross_notional"], "official_fee": calc["official_fee"], "fallback_fee": calc["fallback_fee"], "fee_status": calc["fee_status"], "net_proceeds_or_cost": calc["net_proceeds_or_cost"]})
    fields = ["token_id", "market_slug", "fees_enabled", "fee_parameter_source", "fee_rate_parameter", "fee_exponent_observed", "fee_formula_version", "gross_notional", "official_fee", "fallback_fee", "fee_status", "net_proceeds_or_cost"]
    write_csv(out_dir / "fee_validation.csv", fields, rows)
    return rows


def write_status_rows(out_dir: Path, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in markets:
        raw = item.get("raw_market", {})
        st = parse_market_status(raw)
        evidence = {
            "condition_id": item["condition_id"],
            "market_slug": item["market_slug"],
            "source_type": "official_public_gamma",
            "source_endpoint": "GET /markets/slug/{slug}",
            "source_reference": item["market_slug"],
            "raw_response_hash": sha256(stable_json(raw).encode("utf-8")).hexdigest(),
            "observed_at_utc": iso(),
            "market_status": st["market_status"],
            "resolution_status": st["raw_status"],
            "winning_outcome": st["winning_outcome"],
            "token_settlement_value": "" if not st["resolved"] else ("1" if st["winning_outcome"] == item["outcome_label"] else "0"),
            "evidence_valid": str(bool(item["condition_id"] and item["market_slug"])).lower(),
        }
        append_jsonl(out_dir / "market_status_snapshots.jsonl", evidence)
        rows.append(evidence)
    return rows


def formal_isolation(root: Path, out_dir: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {"generated_at_utc": iso(), "ok": True, "formal_dirs": {}}
    for rel in ["data/forward_v5_1/formal", "data/forward_v5_1_1/formal"]:
        path = root / rel
        info = {"exists": path.exists(), "row_counts": {}, "formal_started_at_utc": None}
        if path.exists():
            state_json = path / "system_state.json"
            if state_json.exists():
                try:
                    info["formal_started_at_utc"] = json.loads(state_json.read_text(encoding="utf-8")).get("formal_started_at_utc")
                except Exception:
                    info["formal_started_at_utc"] = "unreadable"
            sqlite_path = path / "ledger.sqlite3"
            if sqlite_path.exists():
                info["sqlite_present"] = True
                checks["ok"] = False
            for csv_path in path.glob("*.csv"):
                with csv_path.open(encoding="utf-8", newline="") as f:
                    count = sum(1 for _ in csv.DictReader(f))
                info["row_counts"][csv_path.name] = count
                if count:
                    checks["ok"] = False
            for jsonl_path in path.glob("*.jsonl"):
                count = len([line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()])
                info["row_counts"][jsonl_path.name] = count
                if count:
                    checks["ok"] = False
            if info["formal_started_at_utc"] not in (None, "", "null"):
                checks["ok"] = False
        checks["formal_dirs"][rel] = info
    write_json(out_dir / "formal_isolation_proof.json", checks)
    return checks


def read_only_scan(root: Path, out_dir: Path) -> dict[str, Any]:
    patterns = [
        "private" + "_key",
        "seed" + " phrase",
        "mnemo" + "nic",
        "wallet" + " connect",
        "sign" + "ing",
        "create" + "_order",
        "post" + "_order",
        "submit" + "_order",
        "cancel" + "_order",
        "app" + "rove",
        "allow" + "ance",
        "trans" + "fer",
        "Web" + "3",
        "CLOB" + " trading",
    ]
    code_files = [root / "src/polymarket_public_adapter_v5_1_2.py", root / "src/forward_simulation_v5_1_2.py", root / "src/forward_reporting_v5_1_2.py"]
    findings = []
    for path in code_files:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        for p in patterns:
            if p.lower() in text.lower():
                findings.append({"path": str(path), "pattern": p})
    result = {"generated_at_utc": iso(), "code_files_scanned": [str(p) for p in code_files], "forbidden_findings": findings, "ok": not findings, "actual_endpoints": []}
    write_json(out_dir / "read_only_security_scan.json", result)
    return result


def run_failure_probe(out_dir: Path) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []

    def flaky_transport(url: str, method: str, timeout: float) -> tuple[int, str]:
        attempts.append({"url": url, "method": method, "timeout": timeout, "attempt_number": len(attempts) + 1})
        if len(attempts) == 1:
            raise AdapterError("network_error", "injected transient read failure", endpoint=url)
        return 200, "{\"server_time\":1234567890}"

    adapter = PublicAdapter(max_retries=1, backoff_seconds=0, transport=flaky_transport)
    try:
        result = adapter.get_json(CLOB_BASE, "/time")
        probe = {
            "generated_at_utc": iso(),
            "ok": result.status_code == 200 and len(attempts) == 2,
            "failure_category": "network_error",
            "attempts": attempts,
            "recovered": True,
            "false_fill_created": False,
        }
    except AdapterError as exc:
        probe = {
            "generated_at_utc": iso(),
            "ok": False,
            "failure_category": exc.category,
            "attempts": attempts,
            "recovered": False,
            "false_fill_created": False,
            "message": str(exc),
        }
    write_json(out_dir / "network_failure_probe.json", probe)
    append_jsonl(out_dir / "adapter_audit_log.jsonl", {"created_at_utc": iso(), "event_type": "network_failure_probe", **probe})
    return probe


def run_live(root: Path, config_path: Path, iterations: int, interval: float) -> dict[str, Any]:
    config = load_config(config_path)
    out_dir = live_dir(root, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    adapter = PublicAdapter(config["public_api"].get("gamma_base", GAMMA_BASE), config["public_api"].get("clob_base", CLOB_BASE), fnum(config["public_api"].get("timeout_seconds"), 10), int(config["public_api"].get("max_retries", 2)), fnum(config["public_api"].get("backoff_seconds"), 0.5))
    markets = choose_markets(adapter, root, config)
    token_rows = validate_token_mapping(markets, out_dir)
    snapshots = []
    start = utcnow()
    raw_index = len(list((out_dir / "raw_orderbooks").glob("*.json")))
    for i in range(iterations):
        round_snapshots, raw_index = collect_orderbook_round(adapter, out_dir, markets, raw_index)
        snapshots.extend(round_snapshots)
        if i < iterations - 1 and interval > 0:
            time.sleep(interval)
    duration = (utcnow() - start).total_seconds()
    vwap_rows = write_vwap_rows(out_dir, snapshots, config)
    fee_rows = write_fee_rows(out_dir, markets, snapshots)
    status_rows = write_status_rows(out_dir, markets)
    failure_probe = run_failure_probe(out_dir)
    isolation = formal_isolation(root, out_dir)
    scan = read_only_scan(root, out_dir)
    scan["actual_endpoints"] = adapter.visited_endpoints
    write_json(out_dir / "read_only_security_scan.json", scan)
    summary = {"started_at_utc": iso(start), "completed_at_utc": iso(), "duration_seconds": duration, "iterations": iterations, "market_count": len(markets), "token_count": len({m["token_id"] for m in markets}), "snapshot_count": len(snapshots), "token_mapping_rows": len(token_rows), "vwap_rows": len(vwap_rows), "fee_rows": len(fee_rows), "status_rows": len(status_rows), "network_failure_probe_ok": failure_probe["ok"], "formal_isolation_ok": isolation["ok"], "read_only_scan_ok": scan["ok"]}
    write_json(out_dir / "live_integration_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=".")
    p.add_argument("--config", default="config/forward_simulation_v5_1_2.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sp = sub.add_parser("sample")
    sp.add_argument("--iterations", type=int, default=None)
    sp.add_argument("--interval-seconds", type=float, default=None)
    sub.add_parser("audit")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    config_path = Path(args.config)
    config = load_config(config_path)
    if args.command == "discover":
        adapter = PublicAdapter()
        rows = choose_markets(adapter, root, config)
        print(json.dumps({"selected": len(rows)}, indent=2, ensure_ascii=False))
    elif args.command == "sample":
        default_iterations = int(config.get("live_rehearsal", {}).get("default_iterations", 15))
        max_iterations = int(config.get("live_rehearsal", {}).get("max_iterations", 30))
        iterations = args.iterations if args.iterations is not None else default_iterations
        if iterations > max_iterations:
            raise RuntimeError("iterations exceeds configured max")
        interval = args.interval_seconds if args.interval_seconds is not None else fnum(config.get("live_rehearsal", {}).get("default_interval_seconds"), 60)
        print(json.dumps(run_live(root, config_path, iterations, interval), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "audit":
        out_dir = live_dir(root, config)
        print(json.dumps({"formal_isolation": formal_isolation(root, out_dir), "scan": read_only_scan(root, out_dir)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
