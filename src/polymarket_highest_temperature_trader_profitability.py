#!/usr/bin/env python3
"""Official closed-position profitability for highest-temperature markets.

This module is intentionally independent from the public-fill pattern cores.
It uses only the official unauthenticated ``GET /closed-positions`` response
field ``realizedPnl`` and does not reconstruct a ledger, ROI, Negative Risk
economics, deposits, withdrawals, gas, rebates, or strategy-level PnL.
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


SCHEMA_VERSION = "polymarket_highest_temperature_trader_profitability_v1"
PNL_SOURCE = "OFFICIAL_DATA_API_CLOSED_POSITIONS_REALIZED_PNL"
CLOSED_POSITIONS_ENDPOINT = "https://data-api.polymarket.com/closed-positions"
CLOSED_POSITION_PAGE_LIMIT = 50
CLOSED_POSITION_OFFSET_CAP = 100_000
CLOSED_POSITION_FETCH_WORKERS = 4
PNL_ZERO_TOLERANCE_USD = Decimal("0.005")

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
    "settled_position_count", "realized_pnl_usd", "profitable_or_loss", "source",
]
MONTHLY_FIELDS = [
    "wallet", "weather_month", "market_weather_day_count", "event_count",
    "settled_position_count", "realized_pnl_usd", "profitable_or_loss", "source",
]
EVENT_AUDIT_FIELDS = [
    "wallet", "canonical_city", "weather_date", "event_id", "event_slug",
    "target_condition_count", "closed_target_condition_count", "settlement_status", "request_status",
    "page_count", "raw_position_count", "included_position_count",
    "exact_duplicate_count", "excluded_position_count", "realized_pnl_usd",
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
            meta = conditions.setdefault(condition_id, {"statuses": set(), "assets": {}, "slugs": set()})
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
            condition_owner_events[condition_id].add(event_id)
        closed_conditions = {
            condition_id for condition_id, meta in conditions.items()
            if "CLOSED" in meta["statuses"]
        }
        if any(len(meta["statuses"]) > 1 for meta in conditions.values()):
            event_issues.append("MARKET_STATUS_CONFLICT")
        scopes.append({
            "event_id": event_id,
            "canonical_city": city,
            "weather_date": raw_day,
            "event_slug": event_slug,
            "conditions": conditions,
            "closed_conditions": closed_conditions,
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


def _fetch_event_positions(
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
        "settlement_status": "CLOSED" if scope["closed_conditions"] else "NOT_CLOSED",
        "page_count": 0,
        "raw_position_count": 0,
        "included_position_count": 0,
        "exact_duplicate_count": 0,
        "excluded_position_count": 0,
        "realized_pnl_usd": 0.0,
        "included_in_profitability": False,
        "source": PNL_SOURCE,
    }
    if scope["issues"]:
        return [], {
            **base_audit,
            "request_status": "MAPPING_CONFLICT",
            "issue_codes": sorted(scope["issues"]),
        }
    if not scope["closed_conditions"]:
        return [], {
            **base_audit,
            "request_status": "EXCLUDED_NOT_CLOSED",
            "issue_codes": [],
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
        if condition_id not in scope["closed_conditions"]:
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
            "_source": PNL_SOURCE,
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
        "realized_pnl_usd": _money(event_pnl),
        "included_in_profitability": True,
        "issue_codes": [],
    }


def _collection_status(audit: list[dict[str, Any]], global_issues: list[str]) -> tuple[str, list[str]]:
    eligible = [row for row in audit if row["closed_target_condition_count"] > 0]
    complete = [row for row in eligible if row["request_status"] == "COMPLETE"]
    affected = [row for row in eligible if row["request_status"] != "COMPLETE"]
    reasons = sorted(set(global_issues + [code for row in affected for code in row["issue_codes"]]))
    unisolated = {
        "TARGET_KEY_MISSING", "TARGET_WEATHER_DATE_INVALID", "NO_TARGET_EVENTS_IN_SCOPE",
    }.intersection(global_issues)
    if unisolated:
        return "BLOCKED", reasons
    if not eligible:
        return "BLOCKED", sorted(set(reasons + ["NO_CLOSED_TARGET_EVENTS"]))
    if affected and not complete:
        return "BLOCKED", reasons or ["NO_COMPLETE_CLOSED_TARGET_EVENTS"]
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
    max_workers: int = CLOSED_POSITION_FETCH_WORKERS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    scopes, global_issues = target_event_scopes(target_markets, date_from, date_to, cities)
    collected: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    if max_workers <= 1:
        collected = [
            _fetch_event_positions(client, wallet, scope, limit=limit, offset_cap=offset_cap)
            for scope in scopes
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _fetch_event_positions, client, wallet, scope,
                    limit=limit, offset_cap=offset_cap,
                ): scope
                for scope in scopes
            }
            for future in as_completed(futures):
                scope = futures[future]
                try:
                    collected.append(future.result())
                except Exception as exc:  # fail closed at this isolated event
                    collected.append(([], {
                        "wallet": wallet,
                        "canonical_city": scope["canonical_city"],
                        "weather_date": scope["weather_date"],
                        "event_id": scope["event_id"],
                        "event_slug": scope["event_slug"],
                        "target_condition_count": len(scope["conditions"]),
                        "closed_target_condition_count": len(scope["closed_conditions"]),
                        "settlement_status": "CLOSED" if scope["closed_conditions"] else "NOT_CLOSED",
                        "request_status": "REQUEST_FAILED",
                        "page_count": 0,
                        "raw_position_count": 0,
                        "included_position_count": 0,
                        "exact_duplicate_count": 0,
                        "excluded_position_count": 0,
                        "realized_pnl_usd": 0.0,
                        "included_in_profitability": False,
                        "issue_codes": [f"UNEXPECTED_COLLECTION_ERROR:{type(exc).__name__}"],
                        "source": PNL_SOURCE,
                    }))
    positions = [row for rows, _ in collected for row in rows]
    audit = sorted(
        [row for _, row in collected],
        key=lambda row: (row["weather_date"], row["canonical_city"], row["event_id"]),
    )
    status, reasons = _collection_status(audit, global_issues)
    eligible = [row for row in audit if row["closed_target_condition_count"] > 0]
    complete = [row for row in eligible if row["request_status"] == "COMPLETE"]
    unsettled = [row for row in audit if row["request_status"] == "EXCLUDED_NOT_CLOSED"]
    meta = {
        "schema_version": SCHEMA_VERSION,
        "wallet": wallet,
        "profitability_status": status,
        "profitability_status_reasons": reasons,
        "pnl_source": PNL_SOURCE,
        "official_public_get_only": True,
        "target_event_count": len(scopes),
        "discovered_event_count": len(scopes),
        "discovered_market_weather_day_count": len({
            (scope["canonical_city"], scope["weather_date"]) for scope in scopes
        }),
        "closed_target_event_count": len(eligible),
        "closed_event_count": len(eligible),
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
                "settlement_status": "NOT_CLOSED",
            }
            for row in unsettled
        ],
        "complete_closed_target_event_count": sum(row["request_status"] == "COMPLETE" for row in eligible),
        "affected_closed_target_event_count": sum(row["request_status"] != "COMPLETE" for row in eligible),
        "excluded_not_closed_event_count": sum(row["request_status"] == "EXCLUDED_NOT_CLOSED" for row in audit),
        "included_position_count": len(positions),
    }
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
    position_path = root / "closed_positions.json"
    audit_path = root / "profitability_event_audit.json"
    _write_json(position_path, positions)
    _write_json(audit_path, audit)
    manifest = {
        **meta,
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
            "closed_positions": {
                "relative_path": position_path.name,
                "record_count": len(positions),
                "sha256": _sha256(position_path),
            },
            "profitability_event_audit": {
                "relative_path": audit_path.name,
                "record_count": len(audit),
                "sha256": _sha256(audit_path),
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
    if root_manifest.get("schema_version") == SCHEMA_VERSION:
        children = [(path, root_manifest)]
    elif root_manifest.get("schema_version") == f"{SCHEMA_VERSION}_multi_wallet":
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
        payload: dict[str, Any] = {"profitability_collection_meta": {
            key: value for key, value in manifest.items()
            if key not in {"requests", "aggregates"}
        }}
        for aggregate_name, payload_name in (
            ("closed_positions", "closed_positions"),
            ("profitability_event_audit", "profitability_event_audit"),
        ):
            meta = (manifest.get("aggregates") or {}).get(aggregate_name)
            if not meta:
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_AGGREGATE_MISSING:{aggregate_name}")
            source = _safe_relative(child_path.parent, meta.get("relative_path"))
            if not source.is_file() or _sha256(source) != meta.get("sha256"):
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_SHA256_MISMATCH:{aggregate_name}")
            rows = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or len(rows) != meta.get("record_count"):
                raise RuntimeError(f"PROFITABILITY_EVIDENCE_RECORD_COUNT_MISMATCH:{aggregate_name}")
            payload[payload_name] = rows
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
        "TOTAL_SETTLED_PNL_USD": None,
        "SETTLED_MARKET_WEATHER_DAYS": 0,
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
    complete_scope_events = [row for row in event_audit if row.get("request_status") == "COMPLETE"]
    unsettled_scope_events = [row for row in event_audit if row.get("request_status") == "EXCLUDED_NOT_CLOSED"]
    closed_scope_events = [row for row in event_audit if row.get("closed_target_condition_count", 0)]
    scope_fields = {
        "DISCOVERED_EVENT_COUNT": int(collection_meta.get("discovered_event_count", len(event_audit))),
        "DISCOVERED_MARKET_WEATHER_DAY_COUNT": int(collection_meta.get(
            "discovered_market_weather_day_count",
            len({(row.get("canonical_city"), row.get("weather_date")) for row in event_audit}),
        )),
        "CLOSED_EVENT_COUNT": int(collection_meta.get("closed_event_count", len(closed_scope_events))),
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
                "settlement_status": "NOT_CLOSED",
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
            if row.get("closed_target_condition_count", 0) and row.get("request_status") != "COMPLETE"
        ]
        return summary, [], [], event_audit

    complete_event_ids = {
        str(row.get("event_id") or "")
        for row in event_audit
        if row.get("request_status") == "COMPLETE"
    }
    valid_positions: list[dict[str, Any]] = []
    invalid_events: set[str] = set()
    for row in positions:
        event_id = str(row.get("_queried_event_id") or "")
        city = str(row.get("_canonical_city") or "")
        raw_day = str(row.get("_weather_date") or "")
        pnl = _decimal(row.get("_realized_pnl", row.get("realizedPnl")))
        if not event_id or event_id not in complete_event_ids or not city or pnl is None:
            invalid_events.add(event_id)
            continue
        try:
            date.fromisoformat(raw_day)
        except ValueError:
            invalid_events.add(event_id)
            continue
        valid_positions.append({**row, "_realized_pnl_decimal": pnl})
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
        pnl = sum((row["_realized_pnl_decimal"] for row in rows), Decimal(0))
        daily_values.append(pnl)
        daily.append({
            "wallet": wallet,
            "canonical_city": city,
            "weather_date": raw_day,
            "event_count": len({str(row["_queried_event_id"]) for row in rows}),
            "settled_position_count": len(rows),
            "realized_pnl_usd": _money(pnl),
            "profitable_or_loss": _classify_pnl(pnl),
            "source": PNL_SOURCE,
        })
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        by_month[row["weather_date"][:7]].append(row)
    monthly: list[dict[str, Any]] = []
    for month, rows in sorted(by_month.items()):
        pnl = sum((Decimal(str(row["realized_pnl_usd"])) for row in rows), Decimal(0))
        monthly.append({
            "wallet": wallet,
            "weather_month": month,
            "market_weather_day_count": len(rows),
            "event_count": sum(int(row["event_count"]) for row in rows),
            "settled_position_count": sum(int(row["settled_position_count"]) for row in rows),
            "realized_pnl_usd": _money(pnl),
            "profitable_or_loss": _classify_pnl(pnl),
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
        if row.get("closed_target_condition_count", 0) and row.get("request_status") != "COMPLETE"
    ]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "analysis_depth": "profitability",
        "wallet": wallet,
        "PROFITABILITY_STATUS": status,
        "PROFITABILITY_STATUS_REASONS": reasons,
        "PNL_SOURCE": PNL_SOURCE,
        "TOTAL_SETTLED_PNL_USD": _money(total),
        "INCLUDED_PNL_USD": _money(total),
        "EXCLUDED_PNL_USD": None if status == "PARTIAL" else 0.0,
        "AFFECTED_MARKETS": affected_markets,
        "SETTLED_MARKET_WEATHER_DAYS": len(daily),
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
        "MONTHLY_PNL": {row["weather_month"]: row["realized_pnl_usd"] for row in monthly},
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
        event_pnl[event_id] += float(row["_realized_pnl_decimal"])
        event_position_count[event_id] += 1
    audited = []
    for row in event_audit:
        event_id = str(row.get("event_id") or "")
        audited.append({
            **row,
            "included_position_count": event_position_count[event_id],
            "realized_pnl_usd": _money(event_pnl[event_id]),
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
            "当前官方 closed-position 证据不足以安全计算。本报告未从 advanced 公开成交反推 PnL。", "",
            f"阻断原因：{', '.join(summary['PROFITABILITY_STATUS_REASONS']) or 'UNKNOWN'}", "",
        ])
        return "\n".join(lines)
    total = float(summary["TOTAL_SETTLED_PNL_USD"])
    direction = "盈利" if total > float(PNL_ZERO_TOLERANCE_USD) else ("亏损" if total < -float(PNL_ZERO_TOLERANCE_USD) else "基本持平")
    monthly_text = "、".join(f"{row['weather_month']} {_fmt_money(row['realized_pnl_usd'])}" for row in monthly) or "无"
    max_profit_day = max(daily, key=lambda row: row["realized_pnl_usd"], default=None)
    max_loss_day = min(daily, key=lambda row: row["realized_pnl_usd"], default=None)
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
        f"在本次已纳入的官方已关闭仓位证据中，累计 realized PnL 为{_fmt_money(total)}，结果为{direction}。"
        + ("本结果只覆盖已成功纳入的事件，因为当前状态是 PARTIAL。" if status == "PARTIAL" else ""), "",
        f"- 已结算/已关闭仓位涉及的市场天气日：{summary['SETTLED_MARKET_WEATHER_DAYS']}",
        f"- 发现event / 真实市场天气日 / CLOSED event：{summary['DISCOVERED_EVENT_COUNT']} / {summary['DISCOVERED_MARKET_WEATHER_DAY_COUNT']} / {summary['CLOSED_EVENT_COUNT']}",
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
        "included_pnl_usd": summary.get("INCLUDED_PNL_USD"),
        "excluded_pnl_usd": summary.get("EXCLUDED_PNL_USD"),
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
            evidence.get("closed_positions") or [],
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
        f"PNL_SOURCE=`{PNL_SOURCE}`。仅比较官方已关闭仓位 realizedPnl，不含ROI、未实现PnL或策略归因。", "",
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
