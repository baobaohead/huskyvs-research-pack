#!/usr/bin/env python3
"""Official hybrid-position PnL for highest-temperature markets.

This module is intentionally independent from the public-fill pattern cores.
It reconciles the two official unauthenticated position endpoints by the
unique position key ``conditionId + asset + outcome``.  A market-position
``totalPnl`` is primary; a closed-position ``realizedPnl`` is a fallback only
when the same position is absent from market-positions.  This module does
not reconstruct a ledger, ROI, Negative Risk economics, deposits,
withdrawals, gas, rebates, or strategy-level PnL.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "polymarket_highest_temperature_trader_profitability_v1_1"
LEGACY_SCHEMA_VERSION = "polymarket_highest_temperature_trader_profitability_v1"
PNL_SOURCE = "OFFICIAL_POLYMARKET_HYBRID_POSITION_PNL"
PRIMARY_PNL_SOURCE = "OFFICIAL_DATA_API_MARKET_POSITIONS_TOTAL_PNL"
CLOSED_POSITION_PNL_SOURCE = "OFFICIAL_DATA_API_CLOSED_POSITIONS_REALIZED_PNL_FALLBACK"
CLOSED_POSITIONS_ENDPOINT = "https://data-api.polymarket.com/closed-positions"
MARKET_POSITIONS_ENDPOINT = "https://data-api.polymarket.com/v1/market-positions"
GAMMA_EVENTS_ENDPOINT = "https://gamma-api.polymarket.com/events"
CLOSED_POSITION_PAGE_LIMIT = 50
CLOSED_POSITION_OFFSET_CAP = 100_000
MARKET_POSITION_PAGE_LIMIT = 500
MARKET_POSITION_OFFSET_CAP = 100_000
CLOSED_POSITION_FETCH_WORKERS = 4
PNL_ZERO_TOLERANCE_USD = Decimal("0.005")
POSITION_FIELD_TOLERANCE_USD = Decimal("0.005")
RESOLUTION_PRICE_TOLERANCE = Decimal("0.005")

# These are research-comparison thresholds, not an industry performance scale.
STABILITY_MIN_SETTLED_DAYS = 20
STABILITY_MIN_SETTLED_MONTHS = 2
STABILITY_HIGH_PROFITABLE_DAY_RATE = 0.60
STABILITY_HIGH_POSITIVE_MONTH_RATE = 0.75
STABILITY_HIGH_TOP3_PROFIT_SHARE = 0.50
STABILITY_HIGH_MAX_LOSS_TO_POSITIVE_PROFIT = 0.20
STABILITY_HIGH_MAX_LOSS_STREAK = 2
STABILITY_MEDIUM_PROFITABLE_DAY_RATE = 0.50
STABILITY_MEDIUM_POSITIVE_MONTH_RATE = 0.50
STABILITY_MEDIUM_TOP3_PROFIT_SHARE = 0.70
STABILITY_MEDIUM_MAX_LOSS_TO_POSITIVE_PROFIT = 0.35
STABILITY_MEDIUM_MAX_LOSS_STREAK = 4

DAILY_FIELDS = [
    "wallet", "canonical_city", "weather_date", "event_count",
    "position_count", "settled_position_count", "cash_pnl_usd",
    "realized_pnl_usd", "total_official_pnl_usd", "total_pnl_usd",
    "profitable_or_loss", "source",
]
MONTHLY_FIELDS = [
    "wallet", "weather_month", "market_weather_day_count", "event_count",
    "position_count", "settled_position_count", "cash_pnl_usd",
    "realized_pnl_usd", "total_official_pnl_usd", "total_pnl_usd",
    "profitable_or_loss", "source",
]
EVENT_AUDIT_FIELDS = [
    "wallet", "canonical_city", "weather_date", "event_id", "event_slug",
    "target_condition_count", "closed_target_condition_count", "resolved_target_condition_count",
    "settlement_status", "request_status", "page_count", "raw_position_count",
    "included_position_count", "market_position_page_count", "market_raw_position_count",
    "market_included_position_count", "closed_position_page_count", "closed_raw_position_count",
    "closed_included_position_count", "exact_duplicate_count", "excluded_position_count",
    "cash_pnl_usd", "realized_pnl_usd", "total_pnl_usd",
    "closed_position_realized_pnl_usd", "closed_position_crosscheck_status",
    "position_source_counts", "observed_traded_position_count",
    "pnl_covered_traded_position_count", "traded_position_pnl_coverage",
    "traded_neither_source_count", "observed_traded_position_coverage_status",
    "traded_neither_source_details", "conflict_details",
    "included_in_profitability", "issue_codes", "source",
]


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _money(value: Decimal | float | int) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.000001")))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _classify_pnl(value: Decimal) -> str:
    if value > PNL_ZERO_TOLERANCE_USD:
        return "PROFIT"
    if value < -PNL_ZERO_TOLERANCE_USD:
        return "LOSS"
    return "FLAT"


def _is_highest_temperature_slug(value: Any) -> bool:
    slug = str(value or "").lower()
    return "highest-temperature-in-" in slug


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _resolution_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Read only explicit Gamma resolution evidence; closed is not enough."""
    status = str(
        row.get("resolution_status")
        or row.get("uma_resolution_status")
        or row.get("umaResolutionStatus")
        or ""
    ).strip().upper()
    outcomes = [str(value).upper() for value in _json_list(row.get("outcomes"))]
    prices = [_decimal(value) for value in _json_list(row.get("outcome_prices", row.get("outcomePrices")))]
    final = status in {"RESOLVED", "FINAL", "RESOLUTION_CONFIRMED"}
    winner = str(row.get("resolved_outcome") or "").upper() or None
    if len(outcomes) == len(prices) and outcomes and all(price is not None for price in prices):
        winners = [
            outcome for outcome, price in zip(outcomes, prices)
            if price is not None and abs(price - Decimal(1)) <= RESOLUTION_PRICE_TOLERANCE
        ]
        zeros = all(
            price is not None and min(abs(price), abs(price - Decimal(1))) <= RESOLUTION_PRICE_TOLERANCE
            for price in prices
        )
        final = final and len(winners) == 1 and zeros
        winner = winner or (winners[0] if len(winners) == 1 else None)
    return {
        "market_id": str(row.get("market_id") or row.get("id") or ""),
        "market_closed": bool(row.get("market_closed", row.get("closed") is True)),
        "market_active": bool(row.get("market_active", row.get("active") is True)),
        "accepting_orders": row.get("accepting_orders", row.get("acceptingOrders")),
        "uma_resolution_status": status or None,
        "outcomes": outcomes,
        "outcome_prices": [None if price is None else _money(price) for price in prices],
        "resolved": final,
        "resolved_outcome": winner,
        "resolution_source": str(row.get("resolution_source") or "TARGET_MARKET_EVIDENCE"),
    }


def _resolution_from_gamma_market(market: dict[str, Any]) -> dict[str, Any]:
    fields = _resolution_fields({
        "market_id": market.get("id"),
        "market_closed": market.get("closed"),
        "market_active": market.get("active"),
        "accepting_orders": market.get("acceptingOrders"),
        "uma_resolution_status": market.get("umaResolutionStatus"),
        "outcomes": market.get("outcomes"),
        "outcome_prices": market.get("outcomePrices"),
        "resolved": market.get("resolved"),
        "resolution_source": "GAMMA_MARKET_EVIDENCE",
    })
    return fields


def _attach_gamma_resolution_evidence(
    client: Any,
    target_markets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    """Fill missing final-resolution fields with the smallest official refresh."""
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in target_markets:
        by_event[str(row.get("event_id") or "")].append(row)
    enriched: list[dict[str, Any]] = []
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    event_issues: dict[str, list[str]] = defaultdict(list)
    for event_id, rows in by_event.items():
        market_by_condition: dict[str, dict[str, Any]] = {}
        needs_refresh = any(
            not _resolution_fields(row).get("uma_resolution_status")
            and not row.get("resolution_status")
            and not row.get("outcome_prices")
            and not row.get("outcomePrices")
            and str(row.get("market_status") or "").upper() == "CLOSED"
            for row in rows
        )
        if needs_refresh:
            try:
                payload = client.get_json(f"{GAMMA_EVENTS_ENDPOINT}/{event_id}", {})
            except Exception:
                event_issues[event_id].append("RESOLUTION_REQUEST_FAILED")
                payload = {}
            for market in (payload.get("markets") or []) if isinstance(payload, dict) else []:
                condition_id = str(market.get("conditionId") or "").lower()
                if condition_id:
                    market_by_condition[condition_id] = _resolution_from_gamma_market(market)
        for row in rows:
            condition_id = str(row.get("condition_id") or "").lower()
            fields = _resolution_fields(row)
            refreshed = market_by_condition.get(condition_id)
            if refreshed:
                fields = {**fields, **refreshed, "resolution_source": refreshed["resolution_source"]}
            if (
                not fields.get("uma_resolution_status") and not fields.get("resolved")
                and fields.get("market_closed")
            ):
                event_issues[event_id].append("RESOLUTION_NOT_CONFIRMED")
            copied = {**row, **{
                "market_id": fields["market_id"],
                "market_closed": fields["market_closed"],
                "market_active": fields["market_active"],
                "accepting_orders": fields["accepting_orders"],
                "uma_resolution_status": fields["uma_resolution_status"],
                "outcomes": fields["outcomes"],
                "outcome_prices": fields["outcome_prices"],
                "resolved": fields["resolved"],
                "resolved_outcome": fields["resolved_outcome"],
                "resolution_source": fields["resolution_source"],
                "_resolution_issue_codes": sorted(set(event_issues[event_id])),
            }}
            enriched.append(copied)
            evidence[(event_id, condition_id)] = {
                "event_id": event_id,
                "condition_id": condition_id,
                **fields,
            }
    return enriched, sorted(evidence.values(), key=lambda row: (row["event_id"], row["condition_id"])), event_issues


def target_event_scopes(
    target_markets: Iterable[dict[str, Any]],
    date_from: date,
    date_to: date,
    cities: Iterable[str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create audited event scopes from already-discovered target evidence."""
    requested_cities = {str(city) for city in cities or ()}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[str] = []
    for row in target_markets:
        event_id = str(row.get("event_id") or "")
        city = str(row.get("canonical_city") or "")
        raw_day = str(row.get("weather_date_local") or "")
        event_slug = str(row.get("event_slug") or "")
        condition_id = str(row.get("condition_id") or "").lower()
        if not all((event_id, city, raw_day, event_slug, condition_id)):
            issues.append("TARGET_KEY_MISSING")
            continue
        try:
            weather_day = date.fromisoformat(raw_day)
        except ValueError:
            issues.append("TARGET_WEATHER_DATE_INVALID")
            continue
        if not date_from <= weather_day <= date_to:
            continue
        if requested_cities and city not in requested_cities:
            continue
        if not _is_highest_temperature_slug(event_slug):
            issues.append("NON_HIGHEST_TEMPERATURE_TARGET")
            continue
        grouped[event_id].append(row)

    scopes: list[dict[str, Any]] = []
    condition_owner_events: dict[str, set[str]] = defaultdict(set)
    for event_id, rows in sorted(grouped.items()):
        identities = {
            (
                str(row.get("canonical_city") or ""),
                str(row.get("weather_date_local") or ""),
                str(row.get("event_slug") or ""),
            )
            for row in rows
        }
        event_issues: list[str] = []
        if len(identities) != 1:
            event_issues.append("EVENT_MAPPING_CONFLICT")
            city, raw_day, event_slug = sorted(identities)[0]
        else:
            city, raw_day, event_slug = next(iter(identities))
        conditions: dict[str, dict[str, Any]] = {}
        for row in rows:
            condition_id = str(row.get("condition_id") or "").lower()
            meta = conditions.setdefault(condition_id, {
                "statuses": set(), "assets": {}, "slugs": set(),
                "resolutions": [], "resolved_outcomes": set(),
            })
            meta["statuses"].add(str(row.get("market_status") or "UNKNOWN").upper())
            asset = str(row.get("asset") or row.get("token_id") or "")
            outcome = str(row.get("outcome") or "").upper()
            if not asset or outcome not in {"YES", "NO"}:
                event_issues.append("TARGET_ASSET_OR_OUTCOME_MISSING")
            else:
                previous = meta["assets"].get(asset)
                if previous and previous != outcome:
                    event_issues.append("TARGET_ASSET_OUTCOME_CONFLICT")
                meta["assets"][asset] = outcome
            meta["slugs"].add(str(row.get("slug") or ""))
            resolution = _resolution_fields(row)
            meta["resolutions"].append(resolution)
            if resolution.get("resolved_outcome"):
                meta["resolved_outcomes"].add(resolution["resolved_outcome"])
            event_issues.extend(str(code) for code in row.get("_resolution_issue_codes") or [])
            condition_owner_events[condition_id].add(event_id)
        closed_conditions = {
            condition_id for condition_id, meta in conditions.items()
            if "CLOSED" in meta["statuses"]
        }
        if any(len(meta["statuses"]) > 1 for meta in conditions.values()):
            event_issues.append("MARKET_STATUS_CONFLICT")
        if any(len(meta["resolved_outcomes"]) > 1 for meta in conditions.values()):
            event_issues.append("RESOLUTION_OUTCOME_CONFLICT")
        for meta in conditions.values():
            if len({json.dumps(item, sort_keys=True) for item in meta["resolutions"]}) > 1:
                event_issues.append("RESOLUTION_EVIDENCE_CONFLICT")
        resolved_conditions = {
            condition_id for condition_id, meta in conditions.items()
            if meta["resolutions"] and all(item.get("resolved") for item in meta["resolutions"])
        }
        for meta in conditions.values():
            meta["statuses"] = sorted(meta["statuses"])
            meta["slugs"] = sorted(meta["slugs"])
            meta["resolved_outcomes"] = sorted(meta["resolved_outcomes"])
            meta["resolutions"] = meta["resolutions"][-1:]
        scopes.append({
            "event_id": event_id,
            "canonical_city": city,
            "weather_date": raw_day,
            "event_slug": event_slug,
            "conditions": conditions,
            "closed_conditions": closed_conditions,
            "resolved_conditions": resolved_conditions,
            "issues": sorted(set(event_issues)),
        })
    conflicted_conditions = {
        condition_id for condition_id, event_ids in condition_owner_events.items()
        if len(event_ids) > 1
    }
    if conflicted_conditions:
        issues.append("CONDITION_MAPPING_CONFLICT")
        for scope in scopes:
            if conflicted_conditions.intersection(scope["conditions"]):
                scope["issues"] = sorted(set(scope["issues"] + ["CONDITION_MAPPING_CONFLICT"]))
    if not scopes:
        issues.append("NO_TARGET_EVENTS_IN_SCOPE")
    return scopes, sorted(set(issues))


def _fetch_closed_positions(
    client: Any,
    wallet: str,
    scope: dict[str, Any],
    *,
    limit: int,
    offset_cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_audit = {
        "wallet": wallet,
        "canonical_city": scope["canonical_city"],
        "weather_date": scope["weather_date"],
        "event_id": scope["event_id"],
        "event_slug": scope["event_slug"],
        "target_condition_count": len(scope["conditions"]),
        "closed_target_condition_count": len(scope["closed_conditions"]),
        "resolved_target_condition_count": len(scope["resolved_conditions"]),
        "settlement_status": "RESOLVED" if scope["resolved_conditions"] == set(scope["conditions"]) else "NOT_RESOLVED",
        "page_count": 0,
        "raw_position_count": 0,
        "included_position_count": 0,
        "exact_duplicate_count": 0,
        "excluded_position_count": 0,
        "closed_position_realized_pnl_usd": 0.0,
        "closed_position_crosscheck_status": "NOT_AVAILABLE",
        "conflict_details": [],
        "included_in_profitability": False,
        "source": CLOSED_POSITION_PNL_SOURCE,
    }
    if scope["issues"]:
        return [], {
            **base_audit,
            "request_status": "MAPPING_CONFLICT",
            "issue_codes": sorted(scope["issues"]),
        }
    if scope["resolved_conditions"] != set(scope["conditions"]):
        return [], {
            **base_audit,
            "request_status": "EXCLUDED_NOT_RESOLVED",
            "issue_codes": ["RESOLUTION_NOT_CONFIRMED"],
        }

    raw_rows: list[dict[str, Any]] = []
    offset = 0
    page_count = 0
    while True:
        try:
            page = client.get_json(CLOSED_POSITIONS_ENDPOINT, {
                "user": wallet,
                "eventId": scope["event_id"],
                "limit": limit,
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
            })
        except RuntimeError:
            return [], {
                **base_audit,
                "request_status": "REQUEST_FAILED",
                "page_count": page_count,
                "raw_position_count": len(raw_rows),
                "excluded_position_count": len(raw_rows),
                "issue_codes": ["REQUEST_FAILED"],
            }
        if not isinstance(page, list):
            return [], {
                **base_audit,
                "request_status": "INVALID_RESPONSE",
                "page_count": page_count,
                "raw_position_count": len(raw_rows),
                "excluded_position_count": len(raw_rows),
                "issue_codes": ["RESPONSE_NOT_LIST"],
            }
        page_count += 1
        raw_rows.extend(page)
        if len(page) < limit:
            break
        next_offset = offset + limit
        if next_offset > offset_cap:
            return [], {
                **base_audit,
                "request_status": "PAGINATION_INCOMPLETE",
                "page_count": page_count,
                "raw_position_count": len(raw_rows),
                "excluded_position_count": len(raw_rows),
                "issue_codes": ["PAGINATION_INCOMPLETE"],
            }
        offset = next_offset

    issues: list[str] = []
    normalized: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_count = 0
    for source in raw_rows:
        proxy_wallet = str(source.get("proxyWallet") or "").lower()
        condition_id = str(source.get("conditionId") or "").lower()
        asset = str(source.get("asset") or "")
        outcome = str(source.get("outcome") or "").upper()
        pnl = _decimal(source.get("realizedPnl"))
        if proxy_wallet != wallet:
            issues.append("WALLET_MAPPING_CONFLICT")
        if not condition_id or not asset or outcome not in {"YES", "NO"} or pnl is None:
            issues.append("POSITION_KEY_OR_PNL_MISSING")
            continue
        if condition_id not in scope["conditions"]:
            issues.append("CONDITION_MAPPING_CONFLICT")
            continue
        expected_outcome = scope["conditions"][condition_id]["assets"].get(asset)
        if expected_outcome != outcome:
            issues.append("ASSET_OUTCOME_MAPPING_CONFLICT")
            continue
        event_slug = str(source.get("eventSlug") or "")
        if event_slug and event_slug != scope["event_slug"]:
            issues.append("EVENT_SLUG_MAPPING_CONFLICT")
            continue
        key = (condition_id, asset, outcome)
        candidate = {
            **source,
            "_queried_event_id": scope["event_id"],
            "_canonical_city": scope["canonical_city"],
            "_weather_date": scope["weather_date"],
            "_event_slug": scope["event_slug"],
            "_condition_id": condition_id,
            "_asset": asset,
            "_outcome": outcome,
            "_realized_pnl": str(pnl),
            "_source": CLOSED_POSITION_PNL_SOURCE,
        }
        if key in by_key:
            previous = by_key[key]
            comparable = ("_realized_pnl", "avgPrice", "totalBought", "timestamp")
            if all(str(previous.get(field) or "") == str(candidate.get(field) or "") for field in comparable):
                duplicate_count += 1
                continue
            issues.append("UNEXPLAINED_DUPLICATE_PNL")
            continue
        by_key[key] = candidate
        normalized.append(candidate)
    if issues:
        return [], {
            **base_audit,
            "request_status": "MAPPING_CONFLICT",
            "page_count": page_count,
            "raw_position_count": len(raw_rows),
            "exact_duplicate_count": duplicate_count,
            "excluded_position_count": len(raw_rows),
            "issue_codes": sorted(set(issues)),
        }
    event_pnl = sum((_decimal(row["_realized_pnl"]) or Decimal(0) for row in normalized), Decimal(0))
    return normalized, {
        **base_audit,
        "request_status": "COMPLETE",
        "page_count": page_count,
        "raw_position_count": len(raw_rows),
        "included_position_count": len(normalized),
        "exact_duplicate_count": duplicate_count,
        "closed_position_realized_pnl_usd": _money(event_pnl),
        "included_in_profitability": True,
        "issue_codes": [],
    }


def _market_page_positions(page: Any) -> tuple[list[dict[str, Any]], Any]:
    if isinstance(page, dict):
        positions = page.get("positions")
        token = page.get("token")
        rows = []
        for position in positions if isinstance(positions, list) else []:
            if isinstance(position, dict):
                rows.append({**position, "_outer_token": token})
        return rows, token
    if isinstance(page, list):
        flattened: list[dict[str, Any]] = []
        tokens: list[str] = []
        for group in page:
            if not isinstance(group, dict):
                continue
            token = group.get("token")
            if token:
                tokens.append(str(token))
            positions = group.get("positions")
            if isinstance(positions, list):
                for position in positions:
                    if isinstance(position, dict):
                        flattened.append({
                            **position,
                            "asset": position.get("asset") or token,
                            "_outer_token": token,
                        })
        return flattened, tokens[0] if len(tokens) == 1 else None
    return [], None


def _position_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return the only identity used to reconcile the two official sources."""
    return (
        str(row.get("_condition_id") or row.get("conditionId") or "").lower(),
        str(row.get("_asset") or row.get("asset") or ""),
        str(row.get("_outcome") or row.get("outcome") or "").upper(),
    )


def _sum_optional(values: Iterable[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) if present else None


def _observed_trade_facts(
    observed_fills: Iterable[dict[str, Any]] | None,
    scope: dict[str, Any],
) -> tuple[dict[tuple[str, str, str], dict[str, Any]] | None, list[str]]:
    """Build observed traded positions without using fills to calculate PnL."""
    if observed_fills is None:
        return None, []
    facts: dict[tuple[str, str, str], dict[str, Any]] = {}
    issues: list[str] = []
    for fill in observed_fills:
        if str(fill.get("market_identity_status") or "OBSERVED").upper() != "OBSERVED":
            continue
        side = str(fill.get("side") or "").upper()
        condition_id = str(fill.get("condition_id") or "").lower()
        asset = str(fill.get("asset") or "")
        outcome = str(fill.get("outcome") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        if condition_id not in scope["conditions"]:
            continue
        if not asset or outcome not in {"YES", "NO"}:
            issues.append("OBSERVED_FILL_KEY_MISSING")
            continue
        expected_outcome = scope["conditions"][condition_id]["assets"].get(asset)
        if expected_outcome != outcome:
            issues.append("OBSERVED_FILL_MAPPING_CONFLICT")
            continue
        key = (condition_id, asset, outcome)
        fact = facts.setdefault(key, {"BUY_fill_count": 0, "SELL_fill_count": 0})
        fact[f"{side}_fill_count"] += 1
    return facts, sorted(set(issues))


def _reconcile_position_sources(
    market_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    *,
    observed_fills: Iterable[dict[str, Any]] | None,
    scope: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconcile official positions once, preventing BOTH-source double count."""
    market_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    closed_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in market_positions:
        market_by_key[_position_key(row)].append(row)
    for row in closed_positions:
        closed_by_key[_position_key(row)].append(row)

    source_counts: Counter[str] = Counter()
    conflict_details: list[dict[str, Any]] = []
    issues: list[str] = []
    merged: list[dict[str, Any]] = []
    source_class_by_key: dict[tuple[str, str, str], str] = {}
    all_keys = sorted(set(market_by_key) | set(closed_by_key))
    for key in all_keys:
        market_rows = market_by_key.get(key, [])
        closed_rows = closed_by_key.get(key, [])
        market = market_rows[0] if market_rows else None
        closed_realized = _sum_optional(
            _decimal(row.get("_realized_pnl", row.get("realizedPnl")))
            for row in closed_rows
        )
        if market is not None and closed_rows:
            source_class = "BOTH_SOURCES"
            market_total = _decimal(market.get("_total_pnl", market.get("totalPnl")))
            if market_total is None or closed_realized is None:
                issues.append("BOTH_SOURCE_PNL_FIELD_MISSING")
            elif abs(market_total - closed_realized) > POSITION_FIELD_TOLERANCE_USD:
                issues.append("BOTH_SOURCE_PNL_CONFLICT")
                conflict_details.append({
                    "condition_id": key[0],
                    "asset": key[1],
                    "outcome": key[2],
                    "market_total_pnl_usd": _money(market_total),
                    "closed_realized_pnl_usd": _money(closed_realized),
                    "difference_usd": _money(market_total - closed_realized),
                })
            official = market_total
            selected_realized = _decimal(market.get("_realized_pnl", market.get("realizedPnl")))
            selected_cash = _decimal(market.get("_cash_pnl", market.get("cashPnl")))
            double_count_prevented = True
        elif market is not None:
            source_class = "MARKET_POSITION_ONLY"
            official = _decimal(market.get("_total_pnl", market.get("totalPnl")))
            selected_realized = _decimal(market.get("_realized_pnl", market.get("realizedPnl")))
            selected_cash = _decimal(market.get("_cash_pnl", market.get("cashPnl")))
            double_count_prevented = False
        else:
            source_class = "CLOSED_POSITION_ONLY"
            official = closed_realized
            selected_realized = closed_realized
            selected_cash = None
            double_count_prevented = False
        source_counts[source_class] += 1
        source_class_by_key[key] = source_class
        if official is None:
            issues.append("OFFICIAL_POSITION_PNL_MISSING")
            continue
        source_row = dict(market or closed_rows[0])
        selected = {
            **source_row,
            "pnl_source_class": source_class,
            "official_position_pnl_usd": _money(official),
            "market_cash_pnl_usd": None if market is None else _money(selected_cash or Decimal(0)),
            "market_realized_pnl_usd": None if market is None else _money(selected_realized or Decimal(0)),
            "market_total_pnl_usd": None if market is None else _money(_decimal(market.get("_total_pnl", market.get("totalPnl"))) or Decimal(0)),
            "closed_realized_pnl_usd": None if closed_realized is None else _money(closed_realized),
            "double_count_prevented": double_count_prevented,
            "_pnl_source_class": source_class,
            "_official_position_pnl_decimal": official,
            "_cash_pnl_decimal": selected_cash,
            "_realized_pnl_decimal": selected_realized,
            "_total_pnl_decimal": official,
            "_cash_pnl": None if selected_cash is None else str(selected_cash),
            "_realized_pnl": str(selected_realized or Decimal(0)),
            "_total_pnl": str(official),
            "_source": PNL_SOURCE,
        }
        if market is None:
            selected.pop("cashPnl", None)
            selected.pop("currPrice", None)
            selected.pop("_cash_pnl", None)
            selected["_cash_pnl"] = None
        merged.append(selected)

    observed_facts, observed_issues = _observed_trade_facts(observed_fills, scope)
    issues.extend(observed_issues)
    if observed_facts is None:
        observed_stats = {
            "observed_traded_position_count": None,
            "pnl_covered_traded_position_count": None,
            "traded_position_pnl_coverage": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
            "traded_neither_source_count": None,
            "traded_neither_source_details": [],
            "observed_traded_position_coverage_status": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        }
    else:
        neither_details: list[dict[str, Any]] = []
        for key, fact in sorted(observed_facts.items()):
            if key not in source_class_by_key:
                neither_details.append({
                    "weather_date": scope["weather_date"],
                    "conditionId": key[0],
                    "asset": key[1],
                    "outcome": key[2],
                    **fact,
                })
        neither_count = len(neither_details)
        if neither_count:
            issues.append("PNL_EVIDENCE_MISSING_FOR_TRADED_POSITION")
        covered = len(observed_facts) - neither_count
        observed_stats = {
            "observed_traded_position_count": len(observed_facts),
            "pnl_covered_traded_position_count": covered,
            "traded_position_pnl_coverage": f"{covered}/{len(observed_facts)}",
            "traded_neither_source_count": neither_count,
            "traded_neither_source_details": neither_details,
            "observed_traded_position_coverage_status": "FAIL" if neither_count else "PASS",
        }
    status = "BLOCKED" if any(
        code in issues for code in {"BOTH_SOURCE_PNL_CONFLICT", "PNL_EVIDENCE_MISSING_FOR_TRADED_POSITION"}
    ) else "COMPLETE"
    return merged, {
        "position_source_counts": dict(sorted(source_counts.items())),
        "closed_position_crosscheck_status": (
            "CONFLICT" if "BOTH_SOURCE_PNL_CONFLICT" in issues
            else "PASS" if source_counts.get("BOTH_SOURCES")
            else "NOT_AVAILABLE"
        ),
        "conflict_details": conflict_details,
        "issue_codes": sorted(set(issues)),
        "reconciliation_status": status,
        **observed_stats,
    }


def _fetch_market_positions(
    client: Any,
    wallet: str,
    scope: dict[str, Any],
    *,
    limit: int,
    offset_cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch official totalPnl positions for every resolved target condition."""
    base = {
        "market_position_page_count": 0,
        "market_raw_position_count": 0,
        "market_included_position_count": 0,
        "cash_pnl_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "total_pnl_usd": 0.0,
        "issue_codes": [],
        "conflict_details": [],
        "market_request_status": "COMPLETE",
    }
    if scope["resolved_conditions"] != set(scope["conditions"]):
        return [], {**base, "market_request_status": "EXCLUDED_NOT_RESOLVED", "issue_codes": ["RESOLUTION_NOT_CONFIRMED"]}
    normalized: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    issues: list[str] = []
    exact_duplicate_count = 0
    page_count = raw_count = 0
    for condition_id in sorted(scope["resolved_conditions"]):
        offset = 0
        while True:
            try:
                page = client.get_json(MARKET_POSITIONS_ENDPOINT, {
                    "market": condition_id,
                    "user": wallet,
                    "status": "ALL",
                    "limit": limit,
                    "offset": offset,
                })
            except RuntimeError:
                return [], {**base, "market_request_status": "REQUEST_FAILED", "market_position_page_count": page_count, "market_raw_position_count": raw_count, "issue_codes": ["MARKET_POSITION_REQUEST_FAILED"]}
            rows, token = _market_page_positions(page)
            page_count += 1
            raw_count += len(rows)
            for source_row in rows:
                source = dict(source_row) if isinstance(source_row, dict) else {}
                if not source.get("asset") and isinstance(token, str):
                    source["asset"] = token
                proxy_wallet = str(source.get("proxyWallet") or "").lower()
                returned_condition = str(source.get("conditionId") or condition_id).lower()
                asset = str(source.get("asset") or "")
                outer_token = str(source.get("_outer_token") or "")
                outcome = str(source.get("outcome") or "").upper()
                size = _decimal(source.get("size"))
                cash_pnl = _decimal(source.get("cashPnl"))
                realized_pnl = _decimal(source.get("realizedPnl"))
                total_pnl = _decimal(source.get("totalPnl"))
                curr_price = _decimal(source.get("currPrice"))
                required_missing = (
                    not proxy_wallet or not asset or returned_condition != condition_id
                    or outcome not in {"YES", "NO"}
                    or any(value is None for value in (size, cash_pnl, realized_pnl, total_pnl, curr_price))
                )
                if proxy_wallet != wallet:
                    issues.append("WALLET_MAPPING_CONFLICT")
                if returned_condition != condition_id or returned_condition not in scope["conditions"]:
                    issues.append("CONDITION_MAPPING_CONFLICT")
                if outer_token and asset != outer_token:
                    issues.append("TOKEN_ASSET_MAPPING_CONFLICT")
                    base["conflict_details"].append({
                        "condition_id": returned_condition,
                        "outer_token": outer_token,
                        "position_asset": asset,
                        "reason": "OUTER_TOKEN_DIFFERS_FROM_POSITION_ASSET",
                    })
                expected_outcome = scope["conditions"].get(condition_id, {}).get("assets", {}).get(asset)
                if expected_outcome != outcome:
                    issues.append("ASSET_OUTCOME_MAPPING_CONFLICT")
                if required_missing:
                    issues.append("MARKET_POSITION_REQUIRED_FIELD_MISSING")
                    continue
                if abs(total_pnl - cash_pnl - realized_pnl) > POSITION_FIELD_TOLERANCE_USD:
                    issues.append("DATA_QUALITY_CONFLICT")
                resolved_outcome = next(iter(scope["conditions"][condition_id]["resolved_outcomes"]), None)
                expected_price = Decimal(1) if outcome == resolved_outcome else Decimal(0)
                if abs(curr_price - expected_price) > RESOLUTION_PRICE_TOLERANCE:
                    issues.append("RESOLUTION_PNL_CONFLICT")
                    base["conflict_details"].append({
                        "condition_id": returned_condition,
                        "asset": asset,
                        "outcome": outcome,
                        "resolved_outcome": resolved_outcome,
                        "curr_price": _money(curr_price),
                        "expected_final_price": _money(expected_price),
                        "reason": "POSITION_CURR_PRICE_NOT_FINAL",
                    })
                key = (returned_condition, asset, outcome)
                candidate = {
                    **source,
                    "_queried_event_id": scope["event_id"],
                    "_canonical_city": scope["canonical_city"],
                    "_weather_date": scope["weather_date"],
                    "_event_slug": scope["event_slug"],
                    "_condition_id": returned_condition,
                    "_asset": asset,
                    "_outcome": outcome,
                    "_size": str(size),
                    "_cash_pnl": str(cash_pnl),
                    "_realized_pnl": str(realized_pnl),
                    "_total_pnl": str(total_pnl),
                    "_curr_price": str(curr_price),
                    "_source": PNL_SOURCE,
                }
                if key in by_key:
                    previous = by_key[key]
                    comparable = ("_size", "_cash_pnl", "_realized_pnl", "_total_pnl", "_curr_price", "avgPrice", "totalBought", "currentValue")
                    if all(str(previous.get(field) or "") == str(candidate.get(field) or "") for field in comparable):
                        exact_duplicate_count += 1
                        continue
                    issues.append("UNEXPLAINED_DUPLICATE_POSITION")
                    continue
                by_key[key] = candidate
                normalized.append(candidate)
            if len(rows) < limit:
                break
            next_offset = offset + limit
            if next_offset > offset_cap:
                return [], {**base, "market_request_status": "PAGINATION_INCOMPLETE", "market_position_page_count": page_count, "market_raw_position_count": raw_count, "issue_codes": ["MARKET_POSITION_PAGINATION_INCOMPLETE"]}
            offset = next_offset
    if issues:
        return [], {
            **base,
            "market_request_status": "MAPPING_CONFLICT",
            "market_position_page_count": page_count,
            "market_raw_position_count": raw_count,
            "market_included_position_count": len(normalized),
            "exact_duplicate_count": exact_duplicate_count,
            "issue_codes": sorted(set(issues)),
            "conflict_details": base["conflict_details"],
        }
    cash = sum((_decimal(row["_cash_pnl"]) or Decimal(0) for row in normalized), Decimal(0))
    realized = sum((_decimal(row["_realized_pnl"]) or Decimal(0) for row in normalized), Decimal(0))
    total = sum((_decimal(row["_total_pnl"]) or Decimal(0) for row in normalized), Decimal(0))
    return normalized, {
        **base,
        "market_position_page_count": page_count,
        "market_raw_position_count": raw_count,
        "market_included_position_count": len(normalized),
        "exact_duplicate_count": exact_duplicate_count,
        "cash_pnl_usd": _money(cash),
        "realized_pnl_usd": _money(realized),
        "total_pnl_usd": _money(total),
        "conflict_details": [],
    }


def _crosscheck_closed_positions(
    market_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
) -> str:
    if not market_positions:
        return "NOT_AVAILABLE"
    closed_by_key = {
        (row.get("_condition_id"), row.get("_asset"), row.get("_outcome")): row
        for row in closed_positions
    }
    small = [row for row in market_positions if abs(_decimal(row.get("_size")) or Decimal(0)) <= Decimal("0.01")]
    if not small:
        return "DIFFERENT_EXPECTED_SEMANTICS"
    compared = 0
    for row in small:
        closed = closed_by_key.get((row.get("_condition_id"), row.get("_asset"), row.get("_outcome")))
        if not closed:
            continue
        compared += 1
        if abs((_decimal(row.get("_realized_pnl")) or Decimal(0)) - (_decimal(closed.get("_realized_pnl")) or Decimal(0))) > POSITION_FIELD_TOLERANCE_USD:
            return "CONFLICT"
    return "PASS" if compared else "NOT_AVAILABLE"


def _fetch_event_evidence(
    client: Any,
    wallet: str,
    scope: dict[str, Any],
    *,
    closed_limit: int,
    closed_offset_cap: int,
    market_limit: int,
    market_offset_cap: int,
    observed_fills: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base = {
        "wallet": wallet,
        "canonical_city": scope["canonical_city"],
        "weather_date": scope["weather_date"],
        "event_id": scope["event_id"],
        "event_slug": scope["event_slug"],
        "target_condition_count": len(scope["conditions"]),
        "closed_target_condition_count": len(scope["closed_conditions"]),
        "resolved_target_condition_count": len(scope["resolved_conditions"]),
        "settlement_status": "RESOLVED" if scope["resolved_conditions"] == set(scope["conditions"]) else "NOT_RESOLVED",
        "page_count": 0,
        "raw_position_count": 0,
        "included_position_count": 0,
        "market_position_page_count": 0,
        "market_raw_position_count": 0,
        "market_included_position_count": 0,
        "closed_position_page_count": 0,
        "closed_raw_position_count": 0,
        "closed_included_position_count": 0,
        "exact_duplicate_count": 0,
        "excluded_position_count": 0,
        "cash_pnl_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "total_pnl_usd": 0.0,
        "closed_position_realized_pnl_usd": 0.0,
        "closed_position_crosscheck_status": "NOT_AVAILABLE",
        "position_source_counts": {},
        "observed_traded_position_count": None,
        "pnl_covered_traded_position_count": None,
        "traded_position_pnl_coverage": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        "traded_neither_source_count": None,
        "traded_neither_source_details": [],
        "observed_traded_position_coverage_status": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        "conflict_details": [],
        "included_in_profitability": False,
        "source": PNL_SOURCE,
    }
    if scope["issues"]:
        return [], [], {**base, "request_status": "MAPPING_CONFLICT", "issue_codes": sorted(scope["issues"])}
    if scope["resolved_conditions"] != set(scope["conditions"]):
        return [], [], {**base, "request_status": "EXCLUDED_NOT_RESOLVED", "issue_codes": ["RESOLUTION_NOT_CONFIRMED"]}
    closed_positions, closed_audit = _fetch_closed_positions(
        client, wallet, scope, limit=closed_limit, offset_cap=closed_offset_cap,
    )
    market_positions, market_audit = _fetch_market_positions(
        client, wallet, scope, limit=market_limit, offset_cap=market_offset_cap,
    )
    if market_audit["market_request_status"] != "COMPLETE":
        return [], closed_positions, {
            **base,
            **market_audit,
            "request_status": market_audit["market_request_status"],
            "closed_position_page_count": closed_audit.get("page_count", 0),
            "closed_raw_position_count": closed_audit.get("raw_position_count", 0),
            "closed_included_position_count": closed_audit.get("included_position_count", 0),
            "closed_position_realized_pnl_usd": closed_audit.get("closed_position_realized_pnl_usd", 0.0),
            "closed_position_crosscheck_status": "NOT_AVAILABLE",
            "issue_codes": sorted(set(market_audit.get("issue_codes", []))),
        }
    reconciled_positions, reconciliation = _reconcile_position_sources(
        market_positions,
        closed_positions,
        observed_fills=observed_fills,
        scope=scope,
    )
    request_status = "COMPLETE" if reconciliation["reconciliation_status"] == "COMPLETE" else "RECONCILIATION_BLOCKED"
    return (reconciled_positions if request_status == "COMPLETE" else []), closed_positions, {
        **base,
        **market_audit,
        **reconciliation,
        "request_status": request_status,
        "page_count": market_audit.get("market_position_page_count", 0),
        "raw_position_count": market_audit.get("market_raw_position_count", 0),
        "included_position_count": len(reconciled_positions) if request_status == "COMPLETE" else 0,
        "market_included_position_count": market_audit.get("market_included_position_count", 0),
        "closed_position_page_count": closed_audit.get("page_count", 0),
        "closed_raw_position_count": closed_audit.get("raw_position_count", 0),
        "closed_included_position_count": closed_audit.get("included_position_count", 0),
        "closed_position_realized_pnl_usd": closed_audit.get("closed_position_realized_pnl_usd", 0.0),
        "included_in_profitability": request_status == "COMPLETE",
    }


def _collection_status(audit: list[dict[str, Any]], global_issues: list[str]) -> tuple[str, list[str]]:
    eligible = [row for row in audit if row["settlement_status"] == "RESOLVED"]
    complete = [row for row in eligible if row["request_status"] == "COMPLETE"]
    affected = [row for row in eligible if row["request_status"] != "COMPLETE"]
    unresolved = [row for row in audit if row["settlement_status"] != "RESOLVED"]
    complete_dates = {str(row.get("weather_date") or "") for row in complete}
    historical_unresolved = [
        row for row in unresolved
        if any(day > str(row.get("weather_date") or "") for day in complete_dates)
    ]
    reasons = sorted(set(global_issues + [code for row in affected for code in row["issue_codes"]] + [
        code for row in historical_unresolved for code in row["issue_codes"]
    ]))
    unisolated = {
        "TARGET_KEY_MISSING", "TARGET_WEATHER_DATE_INVALID", "NO_TARGET_EVENTS_IN_SCOPE",
    }.intersection(global_issues)
    if unisolated:
        return "BLOCKED", reasons
    if not eligible:
        return "BLOCKED", sorted(set(reasons + ["NO_RESOLVED_TARGET_EVENTS"]))
    if affected and not complete:
        return "BLOCKED", reasons or ["NO_COMPLETE_CLOSED_TARGET_EVENTS"]
    if historical_unresolved:
        if not complete:
            return "BLOCKED", reasons or ["HISTORICAL_RESOLUTION_INCOMPLETE"]
        return "PARTIAL", reasons or ["HISTORICAL_RESOLUTION_INCOMPLETE"]
    if affected or global_issues:
        return "PARTIAL", reasons
    return "READY", []


def collect_profitability_evidence(
    client: Any,
    wallet: str,
    target_markets: Iterable[dict[str, Any]],
    date_from: date,
    date_to: date,
    cities: Iterable[str] | None,
    *,
    limit: int = CLOSED_POSITION_PAGE_LIMIT,
    offset_cap: int = CLOSED_POSITION_OFFSET_CAP,
    market_limit: int = MARKET_POSITION_PAGE_LIMIT,
    market_offset_cap: int = MARKET_POSITION_OFFSET_CAP,
    max_workers: int = CLOSED_POSITION_FETCH_WORKERS,
    observed_fills: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    target_rows, resolution_evidence, resolution_issues = _attach_gamma_resolution_evidence(
        client, list(target_markets),
    )
    scopes, global_issues = target_event_scopes(target_rows, date_from, date_to, cities)
    collected: list[tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]] = []
    if max_workers <= 1:
        collected = [
            _fetch_event_evidence(
                client, wallet, scope, closed_limit=limit, closed_offset_cap=offset_cap,
                market_limit=market_limit, market_offset_cap=market_offset_cap,
                observed_fills=observed_fills,
            )
            for scope in scopes
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _fetch_event_evidence, client, wallet, scope,
                    closed_limit=limit, closed_offset_cap=offset_cap,
                    market_limit=market_limit, market_offset_cap=market_offset_cap,
                    observed_fills=observed_fills,
                ): scope
                for scope in scopes
            }
            for future in as_completed(futures):
                scope = futures[future]
                try:
                    collected.append(future.result())
                except Exception as exc:  # fail closed at this isolated event
                    collected.append(([], [], {
                        "wallet": wallet,
                        "canonical_city": scope["canonical_city"],
                        "weather_date": scope["weather_date"],
                        "event_id": scope["event_id"],
                        "event_slug": scope["event_slug"],
                        "target_condition_count": len(scope["conditions"]),
                        "closed_target_condition_count": len(scope["closed_conditions"]),
                        "resolved_target_condition_count": len(scope["resolved_conditions"]),
                        "settlement_status": "RESOLVED" if scope["resolved_conditions"] == set(scope["conditions"]) else "NOT_RESOLVED",
                        "request_status": "REQUEST_FAILED",
                        "page_count": 0,
                        "raw_position_count": 0,
                        "included_position_count": 0,
                        "exact_duplicate_count": 0,
                        "excluded_position_count": 0,
                        "cash_pnl_usd": 0.0,
                        "realized_pnl_usd": 0.0,
                        "total_pnl_usd": 0.0,
                        "closed_position_realized_pnl_usd": 0.0,
                        "closed_position_crosscheck_status": "NOT_AVAILABLE",
                        "position_source_counts": {},
                        "observed_traded_position_count": None,
                        "pnl_covered_traded_position_count": None,
                        "traded_position_pnl_coverage": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
                        "traded_neither_source_count": None,
                        "traded_neither_source_details": [],
                        "observed_traded_position_coverage_status": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
                        "included_in_profitability": False,
                        "issue_codes": [f"UNEXPECTED_COLLECTION_ERROR:{type(exc).__name__}"],
                        "source": PNL_SOURCE,
                    }))
    positions = [row for rows, _, _ in collected for row in rows]
    closed_positions = [row for _, rows, _ in collected for row in rows]
    audit = sorted(
        [row for _, _, row in collected],
        key=lambda row: (row["weather_date"], row["canonical_city"], row["event_id"]),
    )
    status, reasons = _collection_status(audit, global_issues)
    eligible = [row for row in audit if row["settlement_status"] == "RESOLVED"]
    complete = [row for row in eligible if row["request_status"] == "COMPLETE"]
    unsettled = [row for row in audit if row["request_status"] == "EXCLUDED_NOT_RESOLVED"]
    meta = {
        "schema_version": SCHEMA_VERSION,
        "wallet": wallet,
        "profitability_status": status,
        "profitability_status_reasons": reasons,
        "pnl_source": PNL_SOURCE,
        "primary_pnl_source": PRIMARY_PNL_SOURCE,
        "closed_position_pnl_source": CLOSED_POSITION_PNL_SOURCE,
        "both_source_rule": "MARKET_TOTAL_PNL_ONLY_NO_DOUBLE_COUNT",
        "official_public_get_only": True,
        "target_event_count": len(scopes),
        "discovered_event_count": len(scopes),
        "discovered_market_weather_day_count": len({
            (scope["canonical_city"], scope["weather_date"]) for scope in scopes
        }),
        "closed_target_event_count": len(eligible),
        "closed_event_count": len(eligible),
        "resolved_target_event_count": len(eligible),
        "resolved_event_count": len(eligible),
        "unsettled_event_count": len(unsettled),
        "excluded_event_count": sum(row["request_status"] != "COMPLETE" for row in audit),
        "settled_scope_end": max(
            (row["weather_date"] for row in complete), default=None,
        ),
        "unsettled_boundary_count": len(unsettled),
        "unsettled_boundary_dates": sorted({row["weather_date"] for row in unsettled}),
        "unsettled_boundary_events": [
            {
                "canonical_city": row["canonical_city"],
                "weather_date": row["weather_date"],
                "event_id": row["event_id"],
                "event_slug": row["event_slug"],
                "settlement_status": "NOT_RESOLVED",
            }
            for row in unsettled
        ],
        "complete_closed_target_event_count": sum(row["request_status"] == "COMPLETE" for row in eligible),
        "affected_closed_target_event_count": sum(row["request_status"] != "COMPLETE" for row in eligible),
        "excluded_not_closed_event_count": sum(row["request_status"] == "EXCLUDED_NOT_RESOLVED" for row in audit),
        "included_position_count": len(positions),
        "position_source_counts": dict(Counter(row.get("pnl_source_class") for row in positions)),
        "market_position_weather_day_count": len({(row["_canonical_city"], row["_weather_date"]) for row in positions}),
        "hybrid_position_weather_day_count": len({(row["_canonical_city"], row["_weather_date"]) for row in positions}),
        "resolved_position_weather_day_count": len({(row["_canonical_city"], row["_weather_date"]) for row in positions}),
        "remaining_position_weather_day_count": len({(row["_canonical_city"], row["_weather_date"]) for row in positions if abs(_decimal(row.get("_size")) or Decimal(0)) > Decimal("0.01")}),
        "fully_closed_position_weather_day_count": len({(row["_canonical_city"], row["_weather_date"]) for row in positions if abs(_decimal(row.get("_size")) or Decimal(0)) <= Decimal("0.01")}),
        "resolution_evidence": resolution_evidence,
        "resolution_event_issues": {event_id: sorted(set(codes)) for event_id, codes in resolution_issues.items()},
        "market_position_request_failure_count": sum(
            "MARKET_POSITION_REQUEST_FAILED" in row.get("issue_codes", []) for row in audit
        ),
        "resolution_request_failure_count": sum(
            "RESOLUTION_REQUEST_FAILED" in codes for codes in resolution_issues.values()
        ),
        "resolution_evidence_conflict_count": sum(
            "RESOLUTION_EVIDENCE_CONFLICT" in row.get("issue_codes", []) for row in audit
        ),
        "_closed_positions": closed_positions,
    }
    if observed_fills is None:
        meta.update({
            "observed_traded_position_count": None,
            "pnl_covered_traded_position_count": None,
            "traded_position_pnl_coverage": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
            "traded_neither_source_count": None,
            "traded_neither_source_details": [],
            "observed_traded_position_coverage_status": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        })
    else:
        observed_count = sum(int(row.get("observed_traded_position_count") or 0) for row in audit)
        covered_count = sum(int(row.get("pnl_covered_traded_position_count") or 0) for row in audit)
        neither_count = sum(int(row.get("traded_neither_source_count") or 0) for row in audit)
        neither_details = [
            detail for row in audit for detail in row.get("traded_neither_source_details") or []
        ]
        meta.update({
            "observed_traded_position_count": observed_count,
            "pnl_covered_traded_position_count": covered_count,
            "traded_position_pnl_coverage": f"{covered_count}/{observed_count}",
            "traded_neither_source_count": neither_count,
            "traded_neither_source_details": neither_details,
            "observed_traded_position_coverage_status": "PASS" if neither_count == 0 else "FAIL",
        })
    return positions, audit, meta


def save_profitability_evidence(
    root: Path,
    wallet: str,
    date_from: date,
    date_to: date,
    cities: Iterable[str],
    positions: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    meta: dict[str, Any],
    requests: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    position_path = root / "market_positions.json"
    closed_position_path = root / "closed_positions.json"
    audit_path = root / "profitability_event_audit.json"
    resolution_path = root / "market_resolution.json"
    _write_json(position_path, positions)
    _write_json(closed_position_path, meta.get("_closed_positions") or [])
    _write_json(audit_path, audit)
    _write_json(resolution_path, meta.get("resolution_evidence") or [])
    manifest_meta = {key: value for key, value in meta.items() if key != "_closed_positions"}
    manifest = {
        **manifest_meta,
        "schema_version": SCHEMA_VERSION,
        "wallet": wallet,
        "weather_date_from": date_from.isoformat(),
        "weather_date_to": date_to.isoformat(),
        "cities": list(cities),
        "public_data_only": True,
        "public_get_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "requests": list(requests),
        "aggregates": {
            "market_positions": {
                "relative_path": position_path.name,
                "record_count": len(positions),
                "sha256": _sha256(position_path),
            },
            "closed_positions": {
                "relative_path": closed_position_path.name,
                "record_count": len(meta.get("_closed_positions") or []),
                "sha256": _sha256(closed_position_path),
            },
            "profitability_event_audit": {
                "relative_path": audit_path.name,
                "record_count": len(audit),
                "sha256": _sha256(audit_path),
            },
            "market_resolution": {
                "relative_path": resolution_path.name,
                "record_count": len(meta.get("resolution_evidence") or []),
                "sha256": _sha256(resolution_path),
            },
        },
    }
    _write_json(root / "manifest.json", manifest)
    return manifest


def save_multi_wallet_profitability_manifest(root: Path, wallet_manifests: Iterable[str]) -> Path:
    path = root / "manifest.json"
    _write_json(path, {
        "schema_version": f"{SCHEMA_VERSION}_multi_wallet",
        "public_data_only": True,
        "public_get_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "wallet_manifests": list(wallet_manifests),
    })
    return path


def _safe_relative(base: Path, value: Any) -> Path:
    relative = Path(str(value or ""))
    if not value or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("PROFITABILITY_EVIDENCE_PATH_NOT_PORTABLE")
    result = (base / relative).resolve()
    try:
        result.relative_to(base.resolve())
    except ValueError as exc:
        raise RuntimeError("PROFITABILITY_EVIDENCE_PATH_TRAVERSAL_REJECTED") from exc
    return result


def load_profitability_evidence(
    manifest_path: Path,
    wallets: Iterable[str],
    date_from: date,
    date_to: date,
) -> dict[str, dict[str, Any]]:
    path = manifest_path.resolve()
    root_manifest = json.loads(path.read_text(encoding="utf-8"))
    if root_manifest.get("schema_version") in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        children = [(path, root_manifest)]
    elif root_manifest.get("schema_version") in {f"{SCHEMA_VERSION}_multi_wallet", f"{LEGACY_SCHEMA_VERSION}_multi_wallet"}:
        children = []
        for relative in root_manifest.get("wallet_manifests") or []:
            child_path = _safe_relative(path.parent, relative)
            children.append((child_path, json.loads(child_path.read_text(encoding="utf-8"))))
    else:
        raise RuntimeError("UNSUPPORTED_PROFITABILITY_EVIDENCE_SCHEMA")
    requested = set(wallets)
    loaded: dict[str, dict[str, Any]] = {}
    for child_path, manifest in children:
        if manifest.get("public_data_only") is not True or manifest.get("public_get_only") is not True:
            raise RuntimeError("PROFITABILITY_EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
        if any(manifest.get(flag) is True for flag in ("account_connection", "signing", "real_order")):
            raise RuntimeError("PROFITABILITY_EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
        wallet = str(manifest.get("wallet") or "").lower()
        if wallet not in requested:
            raise RuntimeError("PROFITABILITY_EVIDENCE_WALLET_MISMATCH")
        if (
            manifest.get("weather_date_from") != date_from.isoformat()
            or manifest.get("weather_date_to") != date_to.isoformat()
        ):
            raise RuntimeError("PROFITABILITY_EVIDENCE_ANALYSIS_RANGE_MISMATCH")
        if manifest.get("schema_version") == SCHEMA_VERSION and manifest.get("pnl_source") != PNL_SOURCE:
            raise RuntimeError("PROFITABILITY_EVIDENCE_PNL_SOURCE_MISMATCH")
        payload: dict[str, Any] = {"profitability_collection_meta": {
            key: value for key, value in manifest.items()
            if key not in {"requests", "aggregates"}
        }}
        for aggregate_name, payload_name in (
            ("market_positions", "market_positions"),
            ("closed_positions", "closed_positions"),
            ("profitability_event_audit", "profitability_event_audit"),
            ("market_resolution", "market_resolution"),
        ):
            meta = (manifest.get("aggregates") or {}).get(aggregate_name)
            if not meta:
                if aggregate_name in {"market_positions", "market_resolution"} and manifest.get("schema_version") == LEGACY_SCHEMA_VERSION:
                    payload[payload_name] = []
                    continue
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_AGGREGATE_MISSING:{aggregate_name}")
            source = _safe_relative(child_path.parent, meta.get("relative_path"))
            if not source.is_file() or _sha256(source) != meta.get("sha256"):
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_SHA256_MISMATCH:{aggregate_name}")
            rows = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or len(rows) != meta.get("record_count"):
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_RECORD_COUNT_MISMATCH:{aggregate_name}")
            payload[payload_name] = rows
        if manifest.get("schema_version") == SCHEMA_VERSION:
            audit_rows = payload.get("profitability_event_audit") or []
            recalculated_status, recalculated_reasons = _collection_status(audit_rows, [])
            collection_meta = payload["profitability_collection_meta"]
            collection_meta["profitability_status"] = recalculated_status
            collection_meta["profitability_status_reasons"] = recalculated_reasons
        loaded[wallet] = payload
    if set(loaded) != requested:
        raise RuntimeError("PROFITABILITY_EVIDENCE_WALLET_SET_MISMATCH")
    return loaded


def _longest_streak(labels: list[str], target: str) -> int:
    longest = current = 0
    for label in labels:
        if label == target:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _profit_share(values: list[Decimal], count: int) -> float:
    positive = sorted((value for value in values if value > PNL_ZERO_TOLERANCE_USD), reverse=True)
    denominator = sum(positive, Decimal(0))
    if denominator <= 0:
        return 0.0
    return float(sum(positive[:count], Decimal(0)) / denominator)


def classify_stability(summary: dict[str, Any]) -> tuple[str, list[str]]:
    days = int(summary["SETTLED_MARKET_WEATHER_DAYS"])
    months = len(summary["MONTHLY_PNL"])
    if days < STABILITY_MIN_SETTLED_DAYS or months < STABILITY_MIN_SETTLED_MONTHS:
        return "INSUFFICIENT_DATA", [
            f"requires_at_least_{STABILITY_MIN_SETTLED_DAYS}_settled_days",
            f"requires_at_least_{STABILITY_MIN_SETTLED_MONTHS}_settled_months",
        ]
    total_positive = float(summary["TOTAL_POSITIVE_DAILY_PNL_USD"])
    max_loss = abs(min(0.0, float(summary["MAX_DAILY_LOSS"])))
    loss_ratio = max_loss / total_positive if total_positive > 0 else math.inf
    positive_month_rate = (
        int(summary["MONTHS_WITH_POSITIVE_PNL"]) / months if months else 0.0
    )
    high_checks = {
        "total_pnl_positive": float(summary["TOTAL_SETTLED_PNL_USD"]) > 0,
        "profitable_day_rate": float(summary["PROFITABLE_DAY_RATE"]) >= STABILITY_HIGH_PROFITABLE_DAY_RATE,
        "positive_month_rate": positive_month_rate >= STABILITY_HIGH_POSITIVE_MONTH_RATE,
        "top3_profit_share": float(summary["TOP3_PROFIT_DAYS_SHARE"]) <= STABILITY_HIGH_TOP3_PROFIT_SHARE,
        "max_loss_to_positive_profit": loss_ratio <= STABILITY_HIGH_MAX_LOSS_TO_POSITIVE_PROFIT,
        "longest_loss_streak": int(summary["LONGEST_LOSS_STREAK"]) <= STABILITY_HIGH_MAX_LOSS_STREAK,
    }
    if all(high_checks.values()):
        return "HIGH", [name for name, passed in high_checks.items() if passed]
    medium_checks = {
        "total_pnl_positive": float(summary["TOTAL_SETTLED_PNL_USD"]) > 0,
        "profitable_day_rate": float(summary["PROFITABLE_DAY_RATE"]) >= STABILITY_MEDIUM_PROFITABLE_DAY_RATE,
        "positive_month_rate": positive_month_rate >= STABILITY_MEDIUM_POSITIVE_MONTH_RATE,
        "top3_profit_share": float(summary["TOP3_PROFIT_DAYS_SHARE"]) <= STABILITY_MEDIUM_TOP3_PROFIT_SHARE,
        "max_loss_to_positive_profit": loss_ratio <= STABILITY_MEDIUM_MAX_LOSS_TO_POSITIVE_PROFIT,
        "longest_loss_streak": int(summary["LONGEST_LOSS_STREAK"]) <= STABILITY_MEDIUM_MAX_LOSS_STREAK,
    }
    if all(medium_checks.values()):
        return "MEDIUM", [name for name, passed in medium_checks.items() if passed]
    failed = [name for name, passed in medium_checks.items() if not passed]
    return "LOW", failed


def _blocked_summary(wallet: str, status: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_depth": "profitability",
        "wallet": wallet,
        "PROFITABILITY_STATUS": status,
        "PROFITABILITY_STATUS_REASONS": reasons,
        "PNL_SOURCE": PNL_SOURCE,
        "PRIMARY_PNL_SOURCE": PRIMARY_PNL_SOURCE,
        "CLOSED_POSITION_PNL_SOURCE": CLOSED_POSITION_PNL_SOURCE,
        "BOTH_SOURCE_RULE": "MARKET_TOTAL_PNL_ONLY_NO_DOUBLE_COUNT",
        "HYBRID_PNL_IS_CANONICAL": True,
        "TOTAL_SETTLED_PNL_USD": None,
        "CLOSED_POSITION_REALIZED_PNL_TOTAL": None,
        "CLOSED_POSITION_CROSSCHECK_STATUS": "NOT_AVAILABLE",
        "POSITION_SOURCE_COUNTS": {},
        "OBSERVED_TRADED_POSITION_COUNT": None,
        "PNL_COVERED_TRADED_POSITION_COUNT": None,
        "TRADED_POSITION_PNL_COVERAGE": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        "TRADED_NEITHER_SOURCE_COUNT": None,
        "OBSERVED_TRADED_POSITION_COVERAGE_STATUS": "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        "SETTLED_MARKET_WEATHER_DAYS": 0,
        "PNL_POSITION_WEATHER_DAYS": 0,
        "RESOLVED_POSITION_WEATHER_DAYS": 0,
        "REMAINING_POSITION_WEATHER_DAYS": 0,
        "FULLY_CLOSED_POSITION_WEATHER_DAYS": 0,
        "PROFITABLE_DAYS": 0,
        "LOSS_DAYS": 0,
        "ZERO_PNL_DAYS": 0,
        "PROFITABLE_DAY_RATE": None,
        "AVERAGE_DAILY_PNL": None,
        "MEDIAN_DAILY_PNL": None,
        "MAX_DAILY_PROFIT": None,
        "MAX_DAILY_LOSS": None,
        "LONGEST_PROFIT_STREAK": None,
        "LONGEST_LOSS_STREAK": None,
        "MONTHLY_PNL": {},
        "MONTHS_WITH_POSITIVE_PNL": 0,
        "MONTHS_WITH_NEGATIVE_PNL": 0,
        "TOP1_PROFIT_DAYS_SHARE": None,
        "TOP1_PROFIT_DAY_SHARE": None,
        "TOP3_PROFIT_DAYS_SHARE": None,
        "TOP10_PROFIT_DAYS_SHARE": None,
        "STABILITY_RATING": "INSUFFICIENT_DATA",
        "PROFITABILITY_STABILITY": "INSUFFICIENT_DATA",
        "INCLUDED_PNL_USD": None,
        "EXCLUDED_PNL_USD": None,
        "AFFECTED_MARKETS": [],
        "DISCOVERED_EVENT_COUNT": 0,
        "DISCOVERED_MARKET_WEATHER_DAY_COUNT": 0,
        "CLOSED_EVENT_COUNT": 0,
        "UNSETTLED_EVENT_COUNT": 0,
        "EXCLUDED_EVENT_COUNT": 0,
        "SETTLED_SCOPE_END": None,
        "UNSETTLED_BOUNDARY_COUNT": 0,
        "UNSETTLED_BOUNDARY_DATES": [],
        "UNSETTLED_BOUNDARY_EVENTS": [],
        "LEGACY_PROFITABILITY_V1_TOTALS_SUPERSEDED": True,
    }


def summarize_profitability(
    wallet: str,
    positions: Iterable[dict[str, Any]],
    event_audit: list[dict[str, Any]],
    collection_meta: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not collection_meta:
        summary = _blocked_summary(wallet, "BLOCKED", ["PROFITABILITY_EVIDENCE_MISSING"])
        return summary, [], [], event_audit
    status = str(collection_meta.get("profitability_status") or "BLOCKED")
    reasons = [str(value) for value in collection_meta.get("profitability_status_reasons") or []]
    complete_scope_events = [row for row in event_audit if row.get("request_status") == "COMPLETE" and row.get("settlement_status") == "RESOLVED"]
    unsettled_scope_events = [row for row in event_audit if row.get("request_status") == "EXCLUDED_NOT_RESOLVED" or row.get("settlement_status") == "NOT_RESOLVED"]
    closed_scope_events = [row for row in event_audit if row.get("settlement_status") == "RESOLVED"]
    scope_fields = {
        "DISCOVERED_EVENT_COUNT": int(collection_meta.get("discovered_event_count", len(event_audit))),
        "DISCOVERED_MARKET_WEATHER_DAY_COUNT": int(collection_meta.get(
            "discovered_market_weather_day_count",
            len({(row.get("canonical_city"), row.get("weather_date")) for row in event_audit}),
        )),
        "CLOSED_EVENT_COUNT": int(collection_meta.get("closed_event_count", len(closed_scope_events))),
        "RESOLVED_EVENT_COUNT": int(collection_meta.get("resolved_event_count", len(closed_scope_events))),
        "UNSETTLED_EVENT_COUNT": int(collection_meta.get("unsettled_event_count", len(unsettled_scope_events))),
        "EXCLUDED_EVENT_COUNT": int(collection_meta.get(
            "excluded_event_count", sum(row.get("request_status") != "COMPLETE" for row in event_audit),
        )),
        "SETTLED_SCOPE_END": collection_meta.get("settled_scope_end") or max(
            (str(row.get("weather_date") or "") for row in complete_scope_events), default=None,
        ),
        "UNSETTLED_BOUNDARY_COUNT": int(collection_meta.get("unsettled_boundary_count", len(unsettled_scope_events))),
        "UNSETTLED_BOUNDARY_DATES": list(collection_meta.get("unsettled_boundary_dates") or sorted({
            str(row.get("weather_date") or "") for row in unsettled_scope_events
        })),
        "UNSETTLED_BOUNDARY_EVENTS": list(collection_meta.get("unsettled_boundary_events") or [
            {
                "canonical_city": row.get("canonical_city"),
                "weather_date": row.get("weather_date"),
                "event_id": row.get("event_id"),
                "event_slug": row.get("event_slug"),
                "settlement_status": "NOT_RESOLVED",
            }
            for row in unsettled_scope_events
        ]),
    }
    if status == "BLOCKED":
        summary = _blocked_summary(wallet, status, reasons)
        summary.update(scope_fields)
        summary["AFFECTED_MARKETS"] = [
            f"{row.get('canonical_city', '')}|{row.get('weather_date', '')}|{row.get('event_id', '')}"
            for row in event_audit
            if row.get("settlement_status") == "RESOLVED" and row.get("request_status") != "COMPLETE"
        ]
        return summary, [], [], event_audit

    complete_event_ids = {
        str(row.get("event_id") or "")
        for row in event_audit
        if row.get("request_status") == "COMPLETE" and row.get("settlement_status") == "RESOLVED"
    }
    valid_positions: list[dict[str, Any]] = []
    invalid_events: set[str] = set()
    for row in positions:
        event_id = str(row.get("_queried_event_id") or "")
        city = str(row.get("_canonical_city") or "")
        raw_day = str(row.get("_weather_date") or "")
        cash_pnl = _decimal(row.get("_cash_pnl", row.get("cashPnl")))
        realized_pnl = _decimal(row.get("_realized_pnl", row.get("realizedPnl")))
        total_pnl = _decimal(row.get(
            "_official_position_pnl_decimal",
            row.get("official_position_pnl_usd", row.get("_total_pnl", row.get("totalPnl"))),
        ))
        if not event_id or event_id not in complete_event_ids or not city or realized_pnl is None or total_pnl is None:
            invalid_events.add(event_id)
            continue
        try:
            date.fromisoformat(raw_day)
        except ValueError:
            invalid_events.add(event_id)
            continue
        valid_positions.append({
            **row,
            "_cash_pnl_decimal": cash_pnl,
            "_realized_pnl_decimal": realized_pnl,
            "_total_pnl_decimal": total_pnl,
        })
    if invalid_events:
        valid_positions = [
            row for row in valid_positions if str(row.get("_queried_event_id") or "") not in invalid_events
        ]
        reasons = sorted(set(reasons + ["SAVED_POSITION_VALIDATION_FAILED"]))
        if valid_positions:
            status = "PARTIAL"
        else:
            summary = _blocked_summary(wallet, "BLOCKED", reasons)
            summary.update(scope_fields)
            summary["AFFECTED_MARKETS"] = [
                f"{row.get('canonical_city', '')}|{row.get('weather_date', '')}|{row.get('event_id', '')}"
                for row in event_audit
                if str(row.get("event_id") or "") in invalid_events
            ]
            return summary, [], [], event_audit

    by_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in valid_positions:
        by_day[(str(row["_canonical_city"]), str(row["_weather_date"]))].append(row)
    daily: list[dict[str, Any]] = []
    daily_values: list[Decimal] = []
    for (city, raw_day), rows in sorted(by_day.items(), key=lambda item: (item[0][1], item[0][0])):
        cash = _sum_optional(row["_cash_pnl_decimal"] for row in rows)
        realized = sum((row["_realized_pnl_decimal"] for row in rows), Decimal(0))
        total_pnl = sum((row["_total_pnl_decimal"] for row in rows), Decimal(0))
        daily_values.append(total_pnl)
        daily.append({
            "wallet": wallet,
            "canonical_city": city,
            "weather_date": raw_day,
            "event_count": len({str(row["_queried_event_id"]) for row in rows}),
            "position_count": len(rows),
            "settled_position_count": len(rows),
            "cash_pnl_usd": None if cash is None else _money(cash),
            "realized_pnl_usd": _money(realized),
            "total_official_pnl_usd": _money(total_pnl),
            "total_pnl_usd": _money(total_pnl),
            "profitable_or_loss": _classify_pnl(total_pnl),
            "source": PNL_SOURCE,
        })
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        by_month[row["weather_date"][:7]].append(row)
    monthly: list[dict[str, Any]] = []
    for month, rows in sorted(by_month.items()):
        cash = _sum_optional(
            None if row.get("cash_pnl_usd") is None else Decimal(str(row["cash_pnl_usd"]))
            for row in rows
        )
        realized = sum((Decimal(str(row["realized_pnl_usd"])) for row in rows), Decimal(0))
        total_pnl = sum((Decimal(str(row["total_official_pnl_usd"])) for row in rows), Decimal(0))
        monthly.append({
            "wallet": wallet,
            "weather_month": month,
            "market_weather_day_count": len(rows),
            "event_count": sum(int(row["event_count"]) for row in rows),
            "position_count": sum(int(row["position_count"]) for row in rows),
            "settled_position_count": sum(int(row["settled_position_count"]) for row in rows),
            "cash_pnl_usd": None if cash is None else _money(cash),
            "realized_pnl_usd": _money(realized),
            "total_official_pnl_usd": _money(total_pnl),
            "total_pnl_usd": _money(total_pnl),
            "profitable_or_loss": _classify_pnl(total_pnl),
            "source": PNL_SOURCE,
        })
    labels = [row["profitable_or_loss"] for row in daily]
    total = sum(daily_values, Decimal(0))
    profitable_days = labels.count("PROFIT")
    loss_days = labels.count("LOSS")
    zero_days = labels.count("FLAT")
    affected_markets = [
        f"{row.get('canonical_city', '')}|{row.get('weather_date', '')}|{row.get('event_id', '')}"
        for row in event_audit
            if row.get("settlement_status") == "RESOLVED" and row.get("request_status") != "COMPLETE"
    ]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "analysis_depth": "profitability",
        "wallet": wallet,
        "PROFITABILITY_STATUS": status,
        "PROFITABILITY_STATUS_REASONS": reasons,
        "PNL_SOURCE": PNL_SOURCE,
        "PRIMARY_PNL_SOURCE": PRIMARY_PNL_SOURCE,
        "CLOSED_POSITION_PNL_SOURCE": CLOSED_POSITION_PNL_SOURCE,
        "BOTH_SOURCE_RULE": "MARKET_TOTAL_PNL_ONLY_NO_DOUBLE_COUNT",
        "HYBRID_PNL_IS_CANONICAL": True,
        "TOTAL_SETTLED_PNL_USD": _money(total),
        "INCLUDED_PNL_USD": _money(total),
        "EXCLUDED_PNL_USD": None if status == "PARTIAL" else 0.0,
        "AFFECTED_MARKETS": affected_markets,
        "SETTLED_MARKET_WEATHER_DAYS": len(daily),
        "PNL_POSITION_WEATHER_DAYS": len(daily),
        "RESOLVED_POSITION_WEATHER_DAYS": len(daily),
        "REMAINING_POSITION_WEATHER_DAYS": len({
            (str(row["_canonical_city"]), str(row["_weather_date"])) for row in valid_positions
            if abs(_decimal(row.get("_size")) or Decimal(0)) > Decimal("0.01")
        }),
        "FULLY_CLOSED_POSITION_WEATHER_DAYS": len({
            key for key, rows in by_day.items()
            if rows and all(abs(_decimal(row.get("_size")) or Decimal(0)) <= Decimal("0.01") for row in rows)
        }),
        "SETTLED_EVENT_COUNT": len({str(row["_queried_event_id"]) for row in valid_positions}),
        "SETTLED_POSITION_COUNT": len(valid_positions),
        "PROFITABLE_DAYS": profitable_days,
        "LOSS_DAYS": loss_days,
        "ZERO_PNL_DAYS": zero_days,
        "PROFITABLE_DAY_RATE": profitable_days / len(daily) if daily else 0.0,
        "AVERAGE_DAILY_PNL": _money(total / len(daily)) if daily else 0.0,
        "MEDIAN_DAILY_PNL": _money(Decimal(str(statistics.median([float(value) for value in daily_values])))) if daily else 0.0,
        "MAX_DAILY_PROFIT": _money(max([Decimal(0), *daily_values])) if daily_values else 0.0,
        "MAX_DAILY_LOSS": _money(min([Decimal(0), *daily_values])) if daily_values else 0.0,
        "LONGEST_PROFIT_STREAK": _longest_streak(labels, "PROFIT"),
        "LONGEST_LOSS_STREAK": _longest_streak(labels, "LOSS"),
        "MONTHLY_PNL": {row["weather_month"]: row["total_official_pnl_usd"] for row in monthly},
        "MONTHLY_TOTAL_PNL": {row["weather_month"]: row["total_pnl_usd"] for row in monthly},
        "MONTHS_WITH_POSITIVE_PNL": sum(row["profitable_or_loss"] == "PROFIT" for row in monthly),
        "MONTHS_WITH_NEGATIVE_PNL": sum(row["profitable_or_loss"] == "LOSS" for row in monthly),
        "MONTHS_WITH_ZERO_PNL": sum(row["profitable_or_loss"] == "FLAT" for row in monthly),
        "TOTAL_POSITIVE_DAILY_PNL_USD": _money(sum((value for value in daily_values if value > PNL_ZERO_TOLERANCE_USD), Decimal(0))),
        "TOP1_PROFIT_DAYS_SHARE": _profit_share(daily_values, 1),
        "TOP1_PROFIT_DAY_SHARE": _profit_share(daily_values, 1),
        "TOP3_PROFIT_DAYS_SHARE": _profit_share(daily_values, 3),
        "TOP10_PROFIT_DAYS_SHARE": _profit_share(daily_values, 10),
        "PNL_ZERO_TOLERANCE_USD": float(PNL_ZERO_TOLERANCE_USD),
        "MONTH_MAPPING": "WEATHER_DATE_LOCAL_MONTH",
        "CLOSED_POSITION_REALIZED_PNL_TOTAL": _money(sum((Decimal(str(row.get("closed_position_realized_pnl_usd") or 0)) for row in event_audit), Decimal(0))),
        "CLOSED_POSITION_CROSSCHECK_STATUS": (
            "CONFLICT" if any(row.get("closed_position_crosscheck_status") == "CONFLICT" for row in event_audit)
            else "PASS" if any(row.get("closed_position_crosscheck_status") == "PASS" for row in event_audit)
            else "NOT_AVAILABLE"
        ),
        "POSITION_SOURCE_COUNTS": dict(collection_meta.get("position_source_counts") or Counter(
            row.get("pnl_source_class") for row in valid_positions
        )),
        "OBSERVED_TRADED_POSITION_COUNT": collection_meta.get("observed_traded_position_count"),
        "PNL_COVERED_TRADED_POSITION_COUNT": collection_meta.get("pnl_covered_traded_position_count"),
        "TRADED_POSITION_PNL_COVERAGE": collection_meta.get(
            "traded_position_pnl_coverage", "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        ),
        "TRADED_NEITHER_SOURCE_COUNT": collection_meta.get("traded_neither_source_count"),
        "OBSERVED_TRADED_POSITION_COVERAGE_STATUS": collection_meta.get(
            "observed_traded_position_coverage_status", "NOT_AUDITABLE_WITH_PATTERN_EVIDENCE",
        ),
        "LEGACY_PROFITABILITY_V1_TOTALS_SUPERSEDED": True,
        **scope_fields,
    }
    rating, rating_reasons = classify_stability(summary)
    summary["STABILITY_RATING"] = rating
    summary["PROFITABILITY_STABILITY"] = rating
    summary["STABILITY_RATING_FACTORS"] = rating_reasons
    event_pnl = Counter()
    event_position_count = Counter()
    for row in valid_positions:
        event_id = str(row["_queried_event_id"])
        event_pnl[event_id] += float(row["_total_pnl_decimal"])
        event_position_count[event_id] += 1
    audited = []
    for row in event_audit:
        event_id = str(row.get("event_id") or "")
        audited.append({
            **row,
            "included_position_count": event_position_count[event_id],
            "cash_pnl_usd": (
                None if _sum_optional(
                    row["_cash_pnl_decimal"] for row in valid_positions
                    if str(row.get("_queried_event_id") or "") == event_id
                ) is None else _money(_sum_optional(
                    row["_cash_pnl_decimal"] for row in valid_positions
                    if str(row.get("_queried_event_id") or "") == event_id
                ) or Decimal(0))
            ),
            "realized_pnl_usd": _money(sum((row["_realized_pnl_decimal"] for row in valid_positions if str(row.get("_queried_event_id") or "") == event_id), Decimal(0))),
            "total_pnl_usd": _money(event_pnl[event_id]),
            "total_official_pnl_usd": _money(event_pnl[event_id]),
            "closed_position_realized_pnl_usd": row.get("closed_position_realized_pnl_usd", 0.0),
            "included_in_profitability": event_id in complete_event_ids and event_id not in invalid_events,
        })
    return summary, daily, monthly, audited


def _fmt_money(value: Any) -> str:
    return "—" if value is None else f"${float(value):,.2f}"


def _fmt_pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def render_profitability_summary(summary: dict[str, Any], daily: list[dict[str, Any]], monthly: list[dict[str, Any]]) -> str:
    status = summary["PROFITABILITY_STATUS"]
    lines = [
        "# 最高温市场盈利能力摘要", "",
        f"- 钱包：`{summary['wallet']}`",
        f"- PROFITABILITY_STATUS：`{status}`",
        f"- PNL_SOURCE：`{PNL_SOURCE}`", "",
    ]
    if status == "BLOCKED":
        lines.extend([
            "当前官方 position 来源的混合证据不足以安全计算。本报告未从 advanced 公开成交反推 PnL。", "",
            f"阻断原因：{', '.join(summary['PROFITABILITY_STATUS_REASONS']) or 'UNKNOWN'}", "",
        ])
        return "\n".join(lines)
    total = float(summary["TOTAL_SETTLED_PNL_USD"])
    direction = "盈利" if total > float(PNL_ZERO_TOLERANCE_USD) else ("亏损" if total < -float(PNL_ZERO_TOLERANCE_USD) else "基本持平")
    monthly_text = "、".join(f"{row['weather_month']} {_fmt_money(row['total_pnl_usd'])}" for row in monthly) or "无"
    max_profit_day = max(daily, key=lambda row: row["total_pnl_usd"], default=None)
    max_loss_day = min(daily, key=lambda row: row["total_pnl_usd"], default=None)
    month_count = len(monthly)
    positive_months = int(summary["MONTHS_WITH_POSITIVE_PNL"])
    majority_months = (
        "大多数月份盈利" if month_count and positive_months > month_count / 2
        else "未达到大多数月份盈利"
    )
    concentration = float(summary["TOP3_PROFIT_DAYS_SHARE"])
    concentration_text = (
        f"Top3盈利日占总正利润{_fmt_pct(concentration)}，利润明显依赖少数天气日。"
        if concentration > STABILITY_MEDIUM_TOP3_PROFIT_SHARE
        else f"Top3盈利日占总正利润{_fmt_pct(concentration)}，未超过本Skill的高集中阈值。"
    )
    stability_text = {
        "HIGH": "在本Skill的简单阈值下，盈利日、正收益月、回撤和利润集中度同时通过HIGH条件。",
        "MEDIUM": "累计结果为正且通过MEDIUM条件，但至少一项没有达到HIGH阈值。",
        "LOW": "数据量足够，但累计PnL或胜负分布、月度结果、回撤、集中度中有项未通过MEDIUM条件。",
        "INSUFFICIENT_DATA": "已纳入天气日或月份数不足，不强行评判稳定性。",
    }[summary["STABILITY_RATING"]]
    lines.extend([
        "## 先说结论", "",
        f"在本次已纳入的官方已最终结算 position 证据中，混合官方 PnL 为{_fmt_money(total)}，结果为{direction}。"
        + ("本结果只覆盖已成功纳入的事件，因为当前状态是 PARTIAL。" if status == "PARTIAL" else ""), "",
        f"- 已结算/已关闭仓位涉及的市场天气日：{summary['SETTLED_MARKET_WEATHER_DAYS']}",
        f"- 发现event / 真实市场天气日 / RESOLVED event：{summary['DISCOVERED_EVENT_COUNT']} / {summary['DISCOVERED_MARKET_WEATHER_DAY_COUNT']} / {summary['CLOSED_EVENT_COUNT']}",
        f"- 未关闭边界event：{summary['UNSETTLED_BOUNDARY_COUNT']}；日期：{'、'.join(summary['UNSETTLED_BOUNDARY_DATES']) or '无'}；SETTLED_SCOPE_END={summary['SETTLED_SCOPE_END'] or '无'}",
        f"- 盈利日 / 亏损日 / 零PnL日：{summary['PROFITABLE_DAYS']} / {summary['LOSS_DAYS']} / {summary['ZERO_PNL_DAYS']}",
        f"- 盈利日比例：{_fmt_pct(summary['PROFITABLE_DAY_RATE'])}",
        f"- 日均 / 日中位PnL：{_fmt_money(summary['AVERAGE_DAILY_PNL'])} / {_fmt_money(summary['MEDIAN_DAILY_PNL'])}",
        f"- 月度PnL：{monthly_text}",
        f"- 正收益月 / 负收益月：{summary['MONTHS_WITH_POSITIVE_PNL']} / {summary['MONTHS_WITH_NEGATIVE_PNL']}",
        f"- 是否大多数月份赚钱：{majority_months}。",
        f"- 最大单日盈利：{_fmt_money(summary['MAX_DAILY_PROFIT'])}" + (f"（{max_profit_day['canonical_city']} {max_profit_day['weather_date']}）" if max_profit_day else ""),
        f"- 最大单日亏损：{_fmt_money(summary['MAX_DAILY_LOSS'])}" + (f"（{max_loss_day['canonical_city']} {max_loss_day['weather_date']}）" if max_loss_day else ""),
        f"- 最长盈利 / 亏损连续段：{summary['LONGEST_PROFIT_STREAK']} / {summary['LONGEST_LOSS_STREAK']}（按已纳入的市场天气日排序）",
        f"- Top1 / Top3 / Top10 盈利日集中度：{_fmt_pct(summary['TOP1_PROFIT_DAYS_SHARE'])} / {_fmt_pct(summary['TOP3_PROFIT_DAYS_SHARE'])} / {_fmt_pct(summary['TOP10_PROFIT_DAYS_SHARE'])}",
        f"- 稳定性等级：`{summary['STABILITY_RATING']}`", "",
        concentration_text, "",
        stability_text, "",
        "## 口径与限制", "",
        f"- 零PnL容差为 ±${float(PNL_ZERO_TOLERANCE_USD):.3f}。",
        "- 月份严格使用市场的 weather_date_local，不使用交易UTC或结算UTC。",
        "- 同一 position 同时存在两类官方来源时只使用 market-position.totalPnl；仅有 market-position 时使用其 totalPnl；仅有 closed-position 时回退到 closed-position.realizedPnl，禁止相加。",
        "- cashPnl 与 market-position.realizedPnl 保留为官方诊断字段；closed-position.realizedPnl 保留为回退来源及交叉核验字段。",
        "- 重复 arch/new event 保留 event 级审计，但同一 canonical_city + weather_date 在日级只合并一次。",
        "- 这是 Skill 内部用于研究对比的稳定性等级，不是行业绩效评级，也不是投资建议。",
        "- 本模块不计算ROI、年化、Sharpe、未实现PnL、Negative Risk、gas、rebate或策略归因。", "",
        "## 稳定性阈值", "",
        f"INSUFFICIENT_DATA：少于{STABILITY_MIN_SETTLED_DAYS}个已纳入天气日或{STABILITY_MIN_SETTLED_MONTHS}个月。HIGH 要求总PnL>0、盈利日率≥{STABILITY_HIGH_PROFITABLE_DAY_RATE:.0%}、正收益月率≥{STABILITY_HIGH_POSITIVE_MONTH_RATE:.0%}、Top3集中度≤{STABILITY_HIGH_TOP3_PROFIT_SHARE:.0%}、最大日亏损/正PnL总额≤{STABILITY_HIGH_MAX_LOSS_TO_POSITIVE_PROFIT:.0%}、最长亏损连续段≤{STABILITY_HIGH_MAX_LOSS_STREAK}。MEDIUM 对应阈值为{STABILITY_MEDIUM_PROFITABLE_DAY_RATE:.0%}、{STABILITY_MEDIUM_POSITIVE_MONTH_RATE:.0%}、{STABILITY_MEDIUM_TOP3_PROFIT_SHARE:.0%}、{STABILITY_MEDIUM_MAX_LOSS_TO_POSITIVE_PROFIT:.0%}和{STABILITY_MEDIUM_MAX_LOSS_STREAK}。其余为LOW。", "",
    ])
    return "\n".join(lines)


def write_wallet_profitability_outputs(
    output_dir: Path,
    summary: dict[str, Any],
    daily: list[dict[str, Any]],
    monthly: list[dict[str, Any]],
    event_audit: list[dict[str, Any]],
    collection_meta: dict[str, Any] | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "profitability_summary.json", summary)
    (output_dir / "profitability_summary.md").write_text(
        render_profitability_summary(summary, daily, monthly), encoding="utf-8",
    )
    _write_csv(output_dir / "daily_profitability.csv", daily, DAILY_FIELDS)
    _write_csv(output_dir / "monthly_profitability.csv", monthly, MONTHLY_FIELDS)
    _write_csv(output_dir / "event_profitability_audit.csv", event_audit, EVENT_AUDIT_FIELDS)
    meta = collection_meta or {}
    quality = [{
        "wallet": summary["wallet"],
        "profitability_status": summary["PROFITABILITY_STATUS"],
        "status_reasons": summary["PROFITABILITY_STATUS_REASONS"],
        "pnl_source": PNL_SOURCE,
        "closed_position_pnl_source": CLOSED_POSITION_PNL_SOURCE,
        "official_public_get_only": True,
        "target_event_count": meta.get("target_event_count", 0),
        "closed_target_event_count": meta.get("closed_target_event_count", 0),
        "complete_closed_target_event_count": meta.get("complete_closed_target_event_count", 0),
        "affected_closed_target_event_count": meta.get("affected_closed_target_event_count", 0),
        "excluded_not_closed_event_count": meta.get("excluded_not_closed_event_count", 0),
        "discovered_event_count": summary.get("DISCOVERED_EVENT_COUNT", 0),
        "discovered_market_weather_day_count": summary.get("DISCOVERED_MARKET_WEATHER_DAY_COUNT", 0),
        "closed_event_count": summary.get("CLOSED_EVENT_COUNT", 0),
        "unsettled_event_count": summary.get("UNSETTLED_EVENT_COUNT", 0),
        "excluded_event_count": summary.get("EXCLUDED_EVENT_COUNT", 0),
        "settled_scope_end": summary.get("SETTLED_SCOPE_END"),
        "unsettled_boundary_count": summary.get("UNSETTLED_BOUNDARY_COUNT", 0),
        "unsettled_boundary_dates": summary.get("UNSETTLED_BOUNDARY_DATES", []),
        "unsettled_boundary_events": summary.get("UNSETTLED_BOUNDARY_EVENTS", []),
        "included_position_count": meta.get("included_position_count", 0),
        "position_source_counts": meta.get("position_source_counts", {}),
        "primary_pnl_source": PRIMARY_PNL_SOURCE,
        "both_source_rule": "MARKET_TOTAL_PNL_ONLY_NO_DOUBLE_COUNT",
        "observed_traded_position_count": meta.get("observed_traded_position_count"),
        "pnl_covered_traded_position_count": meta.get("pnl_covered_traded_position_count"),
        "traded_position_pnl_coverage": meta.get("traded_position_pnl_coverage"),
        "traded_neither_source_count": meta.get("traded_neither_source_count"),
        "observed_traded_position_coverage_status": meta.get("observed_traded_position_coverage_status"),
        "included_pnl_usd": summary.get("INCLUDED_PNL_USD"),
        "excluded_pnl_usd": summary.get("EXCLUDED_PNL_USD"),
        "closed_position_realized_pnl_total": summary.get("CLOSED_POSITION_REALIZED_PNL_TOTAL"),
        "closed_position_crosscheck_status": summary.get("CLOSED_POSITION_CROSSCHECK_STATUS"),
        "pnl_position_weather_days": summary.get("PNL_POSITION_WEATHER_DAYS", 0),
        "resolved_position_weather_days": summary.get("RESOLVED_POSITION_WEATHER_DAYS", 0),
        "remaining_position_weather_days": summary.get("REMAINING_POSITION_WEATHER_DAYS", 0),
        "fully_closed_position_weather_days": summary.get("FULLY_CLOSED_POSITION_WEATHER_DAYS", 0),
        "affected_markets": summary.get("AFFECTED_MARKETS", []),
        "pnl_zero_tolerance_usd": float(PNL_ZERO_TOLERANCE_USD),
        "month_mapping": "WEATHER_DATE_LOCAL_MONTH",
    }]
    _write_csv(output_dir / "profitability_data_quality.csv", quality, list(quality[0]))


def run_profitability_analysis(
    wallets: Iterable[str],
    evidence_by_wallet: dict[str, dict[str, Any]],
    output_root: Path,
) -> dict[str, Any]:
    summaries: dict[str, dict[str, Any]] = {}
    for wallet in wallets:
        evidence = evidence_by_wallet.get(wallet) or {}
        summary, daily, monthly, audit = summarize_profitability(
            wallet,
            evidence.get("market_positions") or [],
            evidence.get("profitability_event_audit") or [],
            evidence.get("profitability_collection_meta"),
        )
        summaries[wallet] = summary
        write_wallet_profitability_outputs(
            output_root / wallet, summary, daily, monthly, audit,
            evidence.get("profitability_collection_meta"),
        )
    comparison = {
        "schema_version": SCHEMA_VERSION,
        "analysis_depth": "profitability",
        "pnl_source": PNL_SOURCE,
        "wallets": summaries,
    }
    _write_json(output_root / "profitability_trader_comparison.json", comparison)
    lines = [
        "# 最高温市场盈利能力对比", "",
        f"PNL_SOURCE=`{PNL_SOURCE}`。优先使用 market-position.totalPnl，缺失时回退到 closed-position.realizedPnl；BOTH 只计 market total，不含ROI、未实现PnL或策略归因。", "",
        "| wallet | status | total settled PnL | settled days | profitable day rate | longest loss streak | top3 share | stability |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for wallet, summary in summaries.items():
        lines.append(
            f"| {wallet} | {summary['PROFITABILITY_STATUS']} | {_fmt_money(summary['TOTAL_SETTLED_PNL_USD'])} | "
            f"{summary['SETTLED_MARKET_WEATHER_DAYS']} | {_fmt_pct(summary['PROFITABLE_DAY_RATE'])} | "
            f"{summary['LONGEST_LOSS_STREAK'] if summary['LONGEST_LOSS_STREAK'] is not None else '—'} | "
            f"{_fmt_pct(summary['TOP3_PROFIT_DAYS_SHARE'])} | {summary['STABILITY_RATING']} |"
        )
    (output_root / "profitability_trader_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return comparison
