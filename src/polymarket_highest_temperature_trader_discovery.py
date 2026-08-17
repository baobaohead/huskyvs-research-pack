#!/usr/bin/env python3
"""Discover Beijing highest-temperature market participant wallets.

Phase 1 is a public-data-only funnel.  It discovers the already-reviewed
highest-temperature market universe, downloads complete public fill evidence
for each target condition, and ranks wallets by observable participation
persistence.  It deliberately does not calculate PnL or infer trading skill.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from .polymarket_highest_temperature_trader_pattern_v1 import (
    DATA_API,
    GAMMA_API,
    TARGET_TRADES_OFFSET_CAP,
    PublicGetClient,
    _source_wallet,
    _worst_status,
    deduplicate_records,
    discover_target_markets,
    normalize_cities,
    parse_date_range,
    sha256_file,
    stable_trade_key,
    write_csv,
    write_json,
)

DISCOVERY_SCHEMA_VERSION = "polymarket_highest_temperature_trader_discovery_v1"
CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION = (
    "polymarket_highest_temperature_trader_discovery_canonical_evidence_index_v1"
)
CITY = "beijing"
ICAO = "ZBAA"
KNOWN_BENCHMARK_WALLETS = {
    "0x8fbd7cf5f806f563080864694415829f7229a959": "A",
    "0x7c63520c2ca9b336af0c205b9ccf68217bb393d4": "B",
}
ACTIVITY_LIMIT = 500
ACTIVITY_OFFSET_CAP = 5_000
TRADES_LIMIT = 10_000
DEFAULT_WORKERS = 6
WALLET_HEX_LENGTH = 40
WALLET_PREFIX = "0x"
WALLET_PATTERN = re.compile(r"^0x[a-f0-9]{40}$")

ALL_WALLET_FIELDS = [
    "wallet", "display_name", "first_beijing_trade_date", "last_beijing_trade_date",
    "active_weather_days", "active_weeks", "active_months", "fill_count",
    "buy_fill_count", "sell_fill_count", "unique_events", "unique_conditions",
    "unique_assets", "observed_share_volume", "observed_notional_usd",
    "observed_notional_source", "top_holder_weather_days", "top_holder_appearances",
    "external_signal_count", "signal_sources", "known_benchmark_wallet",
    "eligibility_status",
]
CANDIDATE_FIELDS = [
    "discovery_priority_rank", "wallet", "display_name", "selection_channel",
    "priority_tier", "first_beijing_trade_date", "last_beijing_trade_date",
    "active_weather_days", "active_weeks", "active_months", "activity_density",
    "fill_count", "buy_fill_count", "sell_fill_count", "unique_events",
    "unique_conditions", "unique_assets", "observed_share_volume",
    "observed_notional_usd", "top_holder_weather_days", "external_signal_count",
    "known_benchmark_wallet", "profitability_run_status", "candidate_status",
]


@dataclass
class EndpointResult:
    rows: list[dict[str, Any]]
    status: str = "COMPLETE"
    page_count: int = 0
    record_count: int = 0


class DiscoveryEvidenceError(RuntimeError):
    """Raised when a saved Discovery evidence manifest cannot be trusted."""


def _stable_global_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Keep identical-looking fills from different wallets distinct."""
    return (_source_wallet(row),) + stable_trade_key(row)


def _deduplicate_global_records(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    duplicates = 0
    for row in rows:
        key = _stable_global_key(row)
        if key in by_key:
            duplicates += 1
            existing = by_key[key]
            existing_sources = {str(existing.get("_source_types") or "")}
            incoming_source = str(row.get("_source_types") or "")
            if incoming_source:
                existing_sources.add(incoming_source)
            existing["_source_types"] = "+".join(sorted(source for source in existing_sources if source))
            continue
        by_key[key] = dict(row)
    return list(by_key.values()), duplicates


def _reconciliation_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Identity shared by /trades and /activity, excluding rounded size."""
    return (
        _source_wallet(row),
        str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
        str(row.get("conditionId") or row.get("condition_id") or "").lower(),
        str(row.get("asset") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("outcome") or "").upper(),
        str(row.get("price") or ""),
        str(row.get("timestamp") or row.get("timestamp_epoch") or ""),
    )


def _numeric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _wallet(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if WALLET_PATTERN.fullmatch(candidate) else ""


def _display_name(row: dict[str, Any]) -> str:
    for field in ("name", "pseudonym", "displayUsername", "display_name", "username"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    return ""


def _condition_id(row: dict[str, Any]) -> str:
    return str(row.get("conditionId") or row.get("condition_id") or "").lower()


def _asset(row: dict[str, Any]) -> str:
    return str(row.get("asset") or row.get("token_id") or "")


def _size(row: dict[str, Any]) -> float | None:
    return _numeric(row.get("size") if "size" in row else row.get("shares"))


def _trade_usd(row: dict[str, Any]) -> tuple[float | None, str]:
    usdc_size = _numeric(row.get("usdcSize") if "usdcSize" in row else row.get("usdc_size"))
    if usdc_size is not None and usdc_size >= 0:
        return usdc_size, "usdcSize"
    price = _numeric(row.get("price"))
    size = _size(row)
    if price is not None and size is not None and price >= 0 and size >= 0:
        return price * size, "price_x_size"
    return None, "unavailable"


def _date_from_trade(row: dict[str, Any], target: dict[str, Any]) -> str:
    return str(target.get("weather_date_local") or row.get("weather_date_local") or "")


def _week_key(value: str) -> str:
    parsed = date.fromisoformat(value)
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _union_endpoint_rows(
    activity_rows: Iterable[dict[str, Any]],
    trade_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reconcile the two official public-fill views without cross-wallet loss."""
    activities = [dict(row) for row in activity_rows if str(row.get("type") or "TRADE").upper() == "TRADE"]
    trades = [dict(row) for row in trade_rows]
    activity_by_key: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(activities):
        activity_by_key[_reconciliation_key(row)].append((index, row))
    used_activity: set[int] = set()
    union: list[dict[str, Any]] = []
    both = 0
    trades_only = 0
    for trade in trades:
        candidates = [
            item for item in activity_by_key.get(_reconciliation_key(trade), [])
            if item[0] not in used_activity
        ]
        if candidates:
            trade_size = _size(trade) or 0.0
            index, activity = min(
                candidates,
                key=lambda item: abs((_size(item[1]) or 0.0) - trade_size),
            )
            used_activity.add(index)
            union.append({**activity, **trade, "_source_types": "source_both"})
            both += 1
        else:
            union.append({**trade, "_source_types": "source_trades"})
            trades_only += 1
    for index, activity in enumerate(activities):
        if index not in used_activity:
            union.append({**activity, "_source_types": "source_activity"})
    unique, _ = _deduplicate_global_records(union)
    return unique, {
        "activity_unique_fill_count": len(activities),
        "trades_unique_fill_count": len(trades),
        "union_fill_count": len(unique),
        "intersection_fill_count": both,
        "activity_only_count": len(activities) - both,
        "trades_only_count": trades_only,
    }


def _fetch_offset_pages(
    client: PublicGetClient,
    url: str,
    params: dict[str, Any],
    *,
    limit: int,
    offset_cap: int,
) -> EndpointResult:
    rows: list[dict[str, Any]] = []
    offset = 0
    page_count = 0
    while True:
        page_params = dict(params)
        page_params.update({"limit": limit, "offset": offset})
        try:
            page = client.get_json(url, page_params)
        except RuntimeError:
            return EndpointResult(rows, "REQUEST_FAILED", page_count, len(rows))
        if not isinstance(page, list):
            return EndpointResult(rows, "UNKNOWN", page_count, len(rows))
        page_count += 1
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < limit:
            unique, _ = _deduplicate_global_records(rows)
            return EndpointResult(unique, "COMPLETE", page_count, len(unique))
        offset += limit
        if offset > offset_cap:
            return EndpointResult(rows, "PAGINATION_INCOMPLETE", page_count, len(rows))


def _fetch_condition(
    client: PublicGetClient,
    condition_id: str,
) -> tuple[str, EndpointResult, EndpointResult]:
    activity = _fetch_offset_pages(
        client,
        f"{DATA_API}/activity",
        {
            "market": condition_id,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
        },
        limit=ACTIVITY_LIMIT,
        offset_cap=ACTIVITY_OFFSET_CAP,
    )
    trades = _fetch_offset_pages(
        client,
        f"{DATA_API}/trades",
        {"market": condition_id, "takerOnly": "false"},
        limit=TRADES_LIMIT,
        offset_cap=TARGET_TRADES_OFFSET_CAP,
    )
    return condition_id, activity, trades


def _fetch_event_trades(
    client: PublicGetClient,
    event_id: str,
) -> tuple[str, EndpointResult]:
    """Fetch all fills for one event in one paginated official query.

    The Data API supports ``eventId`` on /trades but not on /activity.  Phase
    1 only needs public fills to discover wallets, so event-level /trades is
    the authoritative low-cost source.  ``takerOnly=false`` retains maker and
    taker fills.
    """
    if event_id.startswith("condition:"):
        params = {"market": event_id.split(":", 1)[1], "takerOnly": "false"}
    else:
        params = {"eventId": event_id, "takerOnly": "false"}
    initial = _fetch_offset_pages(
        client,
        f"{DATA_API}/trades",
        params,
        limit=TRADES_LIMIT,
        offset_cap=TARGET_TRADES_OFFSET_CAP,
    )
    if initial.status != "PAGINATION_INCOMPLETE" or event_id.startswith("condition:"):
        return event_id, initial

    # A busy event can exceed the Data API offset cap.  Split only the
    # saturated event by its observable BUY/SELL side; this preserves the
    # complete-fill requirement without guessing a time window.
    side_results = []
    for side in ("BUY", "SELL"):
        side_results.append(_fetch_offset_pages(
            client,
            f"{DATA_API}/trades",
            {"eventId": event_id, "side": side, "takerOnly": "false"},
            limit=TRADES_LIMIT,
            offset_cap=TARGET_TRADES_OFFSET_CAP,
        ))
    combined, _ = _deduplicate_global_records(
        row for result in side_results for row in result.rows
    )
    return event_id, EndpointResult(
        combined,
        _worst_status(*(result.status for result in side_results)),
        initial.page_count + sum(result.page_count for result in side_results),
        len(combined),
    )


def _cache_path(evidence_root: Path, condition_id: str) -> Path:
    digest = hashlib.sha256(condition_id.encode("utf-8")).hexdigest()[:24]
    return evidence_root / "conditions" / f"{digest}.json"


def _load_condition_cache(path: Path, condition_id: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("condition_id") != condition_id:
        return None
    if payload.get("trades_status") != "COMPLETE" or payload.get("activity_status") not in {"COMPLETE", "NOT_USED_V1"}:
        return None
    return payload


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryEvidenceError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise DiscoveryEvidenceError(f"{label} must be a JSON object: {path}")
    return value


def _resolve_manifest_relative(base: Path, reference: Any, label: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise DiscoveryEvidenceError(f"missing {label} reference")
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DiscoveryEvidenceError(f"{label} must be a safe relative path: {reference!r}")
    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise DiscoveryEvidenceError(f"{label} escapes its manifest root: {reference!r}")
    return resolved


def _target_event_identity(target: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(target.get("event_id") or ""),
        str(target.get("event_slug") or ""),
        str(target.get("canonical_city") or "").lower(),
        str(target.get("weather_date_local") or ""),
    )


def build_canonical_evidence_index(
    evidence_root: Path,
    target_markets: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    *,
    date_from: str,
    date_to: str,
) -> Path:
    """Write the exact condition-cache index used for deterministic reruns."""
    conditions = _target_by_condition(target_markets)
    audits_by_condition = {
        str(row.get("condition_id") or "").lower(): row for row in audits
    }
    entries: list[dict[str, Any]] = []
    for condition_id in sorted(conditions):
        path = _cache_path(evidence_root, condition_id)
        cached = _load_condition_cache(path, condition_id)
        if cached is None or cached.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
            raise DiscoveryEvidenceError(
                f"missing or incompatible canonical evidence for condition {condition_id}"
            )
        target = conditions[condition_id]
        audit = cached.get("audit")
        if not isinstance(audit, dict) or audit.get("completeness_status") != "COMPLETE":
            raise DiscoveryEvidenceError(
                f"canonical evidence is incomplete for condition {condition_id}"
            )
        if audits_by_condition.get(condition_id) != audit:
            raise DiscoveryEvidenceError(
                f"audit/index mismatch for condition {condition_id}"
            )
        if _target_event_identity(target) != (
            str(audit.get("event_id") or ""),
            str(audit.get("event_slug") or ""),
            str(audit.get("canonical_city") or "").lower(),
            str(audit.get("weather_date_local") or ""),
        ):
            raise DiscoveryEvidenceError(
                f"target/audit identity mismatch for condition {condition_id}"
            )
        entries.append({
            "condition_id": condition_id,
            "event_id": target.get("event_id", ""),
            "event_slug": target.get("event_slug", ""),
            "canonical_city": target.get("canonical_city", ""),
            "weather_date_local": target.get("weather_date_local", ""),
            "schema_version": cached.get("schema_version"),
            "relative_path": path.relative_to(evidence_root).as_posix(),
            "sha256": sha256_file(path),
            "activity_status": cached.get("activity_status"),
            "trades_status": cached.get("trades_status"),
            "completeness_status": audit.get("completeness_status"),
            "union_row_count": len(cached.get("union_rows") or []),
            "target_market": target,
        })
    event_rows = {
        (str(row.get("event_id") or ""), str(row.get("event_slug") or ""),
         str(row.get("canonical_city") or ""), str(row.get("weather_date_local") or ""))
        for row in target_markets
    }
    index = {
        "schema_version": CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION,
        "discovery_schema_version": DISCOVERY_SCHEMA_VERSION,
        "city": CITY,
        "icao": ICAO,
        "date_from": date_from,
        "date_to": date_to,
        "target_event_count": len(event_rows),
        "target_condition_count": len(entries),
        "target_events": [
            {
                "event_id": event_id,
                "event_slug": event_slug,
                "canonical_city": canonical_city,
                "weather_date_local": weather_date,
            }
            for event_id, event_slug, canonical_city, weather_date in sorted(event_rows)
        ],
        "entries": entries,
    }
    index_path = evidence_root / "canonical_evidence_index.json"
    write_json(index_path, index)
    return index_path


def load_manifest_evidence(
    manifest_path: Path,
    *,
    expected_date_from: str | None = None,
    expected_date_to: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Load only manifest-referenced canonical evidence; never scan raw files."""
    manifest_path = manifest_path.resolve()
    manifest = _read_json_object(manifest_path, "discovery manifest")
    if manifest.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
        raise DiscoveryEvidenceError("unsupported discovery manifest schema")
    if manifest.get("city") != CITY or manifest.get("icao") != ICAO:
        raise DiscoveryEvidenceError("manifest city/ICAO does not match Beijing Discovery v1")
    if expected_date_from and manifest.get("date_from") != expected_date_from:
        raise DiscoveryEvidenceError("manifest date_from does not match requested range")
    if expected_date_to and manifest.get("date_to") != expected_date_to:
        raise DiscoveryEvidenceError("manifest date_to does not match requested range")
    index_path = _resolve_manifest_relative(
        manifest_path.parent,
        manifest.get("canonical_evidence_index"),
        "canonical evidence index",
    )
    if not index_path.is_file():
        raise DiscoveryEvidenceError(f"missing manifest-referenced evidence index: {index_path}")
    expected_index_sha = manifest.get("canonical_evidence_index_sha256")
    if expected_index_sha and sha256_file(index_path) != expected_index_sha:
        raise DiscoveryEvidenceError("canonical evidence index SHA-256 mismatch")
    index = _read_json_object(index_path, "canonical evidence index")
    if index.get("schema_version") != CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION:
        raise DiscoveryEvidenceError("unsupported canonical evidence index schema")
    if index.get("discovery_schema_version") != DISCOVERY_SCHEMA_VERSION:
        raise DiscoveryEvidenceError("canonical evidence index schema mismatch")
    for field in ("city", "icao", "date_from", "date_to"):
        if index.get(field) != manifest.get(field):
            raise DiscoveryEvidenceError(f"manifest/index mismatch for {field}")
    entries = index.get("entries")
    target_events = index.get("target_events")
    if not isinstance(entries, list) or not isinstance(target_events, list):
        raise DiscoveryEvidenceError("canonical evidence index is missing entries")
    if len(entries) != int(manifest.get("target_condition_count") or -1):
        raise DiscoveryEvidenceError("canonical evidence condition count mismatch")
    if len(target_events) != int(manifest.get("target_event_count") or -1):
        raise DiscoveryEvidenceError("canonical evidence event count mismatch")
    expected_conditions = {
        str(value).lower() for value in (manifest.get("target_condition_ids") or [])
    }
    if expected_conditions and expected_conditions != {
        str(entry.get("condition_id") or "").lower() for entry in entries
    }:
        raise DiscoveryEvidenceError("manifest/index condition identity mismatch")
    target_event_set = {
        (
            str(row.get("event_id") or ""), str(row.get("event_slug") or ""),
            str(row.get("canonical_city") or "").lower(), str(row.get("weather_date_local") or ""),
        )
        for row in target_events
    }
    target_markets: list[dict[str, Any]] = []
    all_union: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    cache_audits: list[dict[str, Any]] = []
    seen_conditions: set[str] = set()
    for entry in entries:
        condition_id = str(entry.get("condition_id") or "").lower()
        if not condition_id or condition_id in seen_conditions:
            raise DiscoveryEvidenceError("duplicate or empty condition identity in index")
        seen_conditions.add(condition_id)
        target = entry.get("target_market")
        if not isinstance(target, dict):
            raise DiscoveryEvidenceError(f"missing target market for condition {condition_id}")
        if _condition_id(target) != condition_id:
            raise DiscoveryEvidenceError(f"target/condition identity mismatch for {condition_id}")
        target_identity = _target_event_identity(target)
        entry_identity = (
            str(entry.get("event_id") or ""), str(entry.get("event_slug") or ""),
            str(entry.get("canonical_city") or "").lower(), str(entry.get("weather_date_local") or ""),
        )
        if target_identity != entry_identity or target_identity not in target_event_set:
            raise DiscoveryEvidenceError(f"target/event identity mismatch for {condition_id}")
        try:
            target_day = date.fromisoformat(target_identity[3])
            start = date.fromisoformat(str(manifest["date_from"]))
            end = date.fromisoformat(str(manifest["date_to"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DiscoveryEvidenceError("invalid manifest date range") from exc
        if target_day < start or target_day > end or target_identity[2] != CITY:
            raise DiscoveryEvidenceError(f"target date/city outside manifest scope for {condition_id}")
        cache_path = _resolve_manifest_relative(
            index_path.parent, entry.get("relative_path"),
            f"condition {condition_id} evidence",
        )
        if not cache_path.is_file():
            raise DiscoveryEvidenceError(f"missing manifest-referenced evidence: {cache_path}")
        expected_sha = entry.get("sha256")
        if expected_sha and sha256_file(cache_path) != expected_sha:
            raise DiscoveryEvidenceError(f"condition evidence SHA-256 mismatch: {condition_id}")
        cached = _read_json_object(cache_path, f"condition {condition_id} evidence")
        if cached.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
            raise DiscoveryEvidenceError(f"condition schema mismatch: {condition_id}")
        if cached.get("condition_id") != condition_id:
            raise DiscoveryEvidenceError(f"condition cache identity mismatch: {condition_id}")
        if cached.get("trades_status") != "COMPLETE" or cached.get("activity_status") not in {"COMPLETE", "NOT_USED_V1"}:
            raise DiscoveryEvidenceError(f"condition evidence is incomplete: {condition_id}")
        audit = cached.get("audit")
        if not isinstance(audit, dict) or audit.get("completeness_status") != "COMPLETE":
            raise DiscoveryEvidenceError(f"condition audit is incomplete: {condition_id}")
        if _target_event_identity(target) != (
            str(audit.get("event_id") or ""), str(audit.get("event_slug") or ""),
            str(audit.get("canonical_city") or "").lower(), str(audit.get("weather_date_local") or ""),
        ):
            raise DiscoveryEvidenceError(f"condition audit identity mismatch: {condition_id}")
        rows = cached.get("union_rows")
        if not isinstance(rows, list):
            raise DiscoveryEvidenceError(f"condition union_rows is not a list: {condition_id}")
        for row in rows:
            if not isinstance(row, dict) or _condition_id(row) != condition_id:
                raise DiscoveryEvidenceError(f"condition row identity mismatch: {condition_id}")
        if entry.get("union_row_count") != len(rows):
            raise DiscoveryEvidenceError(f"condition row count mismatch: {condition_id}")
        target_markets.append(target)
        all_union.extend(rows)
        audits.append(audit)
        cache_audits.append({
            "method": "MANIFEST_CACHE",
            "condition_id": condition_id,
            "success": True,
            "relative_path": entry.get("relative_path"),
        })
    if expected_conditions:
        condition_set_complete = seen_conditions == expected_conditions
    else:
        condition_set_complete = len(seen_conditions) == len(entries)
    if not condition_set_complete:
        raise DiscoveryEvidenceError("canonical evidence condition set is incomplete")
    all_union, _ = _deduplicate_global_records(all_union)
    expected_fills = manifest.get("total_public_fills")
    if expected_fills is not None and len(all_union) != int(expected_fills):
        raise DiscoveryEvidenceError("canonical evidence fill count mismatch")
    audits.sort(key=lambda row: (row.get("weather_date_local", ""), row.get("condition_id", "")))
    return target_markets, all_union, audits, cache_audits


def _target_by_condition(target_markets: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for target in target_markets:
        result.setdefault(str(target.get("condition_id") or "").lower(), target)
    return {key: value for key, value in result.items() if key}


def collect_market_evidence(
    client: PublicGetClient,
    target_markets: list[dict[str, Any]],
    evidence_root: Path,
    *,
    max_workers: int = DEFAULT_WORKERS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch or load all target-condition public fill evidence."""
    evidence_root.mkdir(parents=True, exist_ok=True)
    conditions = _target_by_condition(target_markets)
    all_union: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    cache_audits: list[dict[str, Any]] = []
    pending: list[str] = []
    for condition_id in sorted(conditions):
        cached = _load_condition_cache(_cache_path(evidence_root, condition_id), condition_id)
        if cached is None:
            pending.append(condition_id)
            continue
        all_union.extend(cached.get("union_rows") or [])
        audits.append(cached["audit"])
        cache_audits.append({
            "method": "CACHE",
            "condition_id": condition_id,
            "success": True,
            "relative_path": _cache_path(evidence_root, condition_id).relative_to(evidence_root).as_posix(),
        })

    pending_by_scope: dict[str, list[str]] = defaultdict(list)
    for condition_id in pending:
        event_id = str(conditions[condition_id].get("event_id") or "")
        pending_by_scope[event_id or f"condition:{condition_id}"].append(condition_id)

    def fetch_one(event_id: str) -> tuple[str, EndpointResult]:
        return _fetch_event_trades(client, event_id)

    fetched: list[tuple[str, EndpointResult]] = []
    if max_workers <= 1:
        fetched = [fetch_one(event_id) for event_id in sorted(pending_by_scope)]
    elif pending_by_scope:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_one, event_id): event_id for event_id in pending_by_scope}
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    fetched.append(future.result())
                except Exception:
                    fetched.append((event_id, EndpointResult([], "REQUEST_FAILED")))

    fetched_by_scope = {event_id: trades for event_id, trades in fetched}
    for event_id in sorted(pending_by_scope):
        trades = fetched_by_scope.get(event_id, EndpointResult([], "REQUEST_FAILED"))
        for condition_id in sorted(pending_by_scope[event_id]):
            condition_rows = [
                row for row in trades.rows
                if _condition_id(row) == condition_id
            ]
            union, metrics = _union_endpoint_rows([], condition_rows)
            target = conditions[condition_id]
            audit = {
                "canonical_city": target.get("canonical_city", ""),
                "weather_date_local": target.get("weather_date_local", ""),
                "event_id": target.get("event_id", ""),
                "event_slug": target.get("event_slug", ""),
                "condition_id": condition_id,
                "activity_status": "NOT_USED_V1",
                "trades_status": trades.status,
                "completeness_status": trades.status,
                "activity_page_count": 0,
                "trades_page_count": trades.page_count,
                "evidence_source": "official_trades_event_id_takerOnly_false",
                **metrics,
            }
            payload = {
                "schema_version": DISCOVERY_SCHEMA_VERSION,
                "condition_id": condition_id,
                "activity_status": "NOT_USED_V1",
                "trades_status": trades.status,
                "activity_rows": [],
                "trades_rows": condition_rows,
                "union_rows": union,
                "audit": audit,
            }
            path = _cache_path(evidence_root, condition_id)
            write_json(path, payload)
            all_union.extend(union)
            audits.append(audit)

    all_union, _ = _deduplicate_global_records(all_union)
    audits.sort(key=lambda row: (row.get("weather_date_local", ""), row.get("condition_id", "")))
    return all_union, audits, cache_audits


def _enrich_fill(row: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    for key in (
        "canonical_city", "weather_date_local", "event_id", "event_slug", "market_id",
        "temperature_bucket", "outcome", "asset", "condition_id", "title", "slug",
    ):
        if not enriched.get(key):
            source_key = {
                "condition_id": "condition_id",
                "asset": "asset",
                "outcome": "outcome",
            }.get(key, key)
            enriched[key] = target.get(source_key, "")
    enriched["wallet"] = _wallet(_source_wallet(enriched))
    enriched["condition_id"] = _condition_id(enriched)
    enriched["asset"] = _asset(enriched)
    enriched["side"] = str(enriched.get("side") or "").upper()
    return enriched


def _raw_wallet_identity(row: dict[str, Any]) -> Any:
    """Return the wallet field supplied by official evidence without repair."""
    if "proxyWallet" in row:
        return row.get("proxyWallet")
    if "proxy_wallet" in row:
        return row.get("proxy_wallet")
    return row.get("wallet")


def aggregate_wallets(
    fills: Iterable[dict[str, Any]],
    target_markets: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets = _target_by_condition(target_markets)
    grouped: dict[str, dict[str, Any]] = {}
    invalid_wallet_rows = 0
    invalid_wallet_values: Counter[str] = Counter()
    invalid_target_rows = 0
    for raw in fills:
        condition_id = _condition_id(raw)
        target = targets.get(condition_id)
        if not target:
            invalid_target_rows += 1
            continue
        raw_wallet = _raw_wallet_identity(raw)
        if not _wallet(raw_wallet):
            invalid_wallet_rows += 1
            rendered_wallet = "<missing>" if raw_wallet in (None, "") else str(raw_wallet).strip().lower()
            invalid_wallet_values[rendered_wallet] += 1
            continue
        row = _enrich_fill(raw, target)
        wallet = row["wallet"]
        if not wallet:
            invalid_wallet_rows += 1
            rendered_wallet = "<missing>" if raw_wallet in (None, "") else str(raw_wallet).strip().lower()
            invalid_wallet_values[rendered_wallet] += 1
            continue
        item = grouped.setdefault(wallet, {
            "wallet": wallet,
            "display_name": "",
            "weather_days": set(),
            "weeks": set(),
            "months": set(),
            "events": set(),
            "conditions": set(),
            "assets": set(),
            "fill_count": 0,
            "buy_fill_count": 0,
            "sell_fill_count": 0,
            "observed_share_volume": 0.0,
            "observed_notional_usd": 0.0,
            "notional_missing_count": 0,
            "notional_source_counts": Counter(),
            "dates": [],
            "top_holder_weather_days": 0,
            "top_holder_appearances": 0,
            "external_signal_count": 0,
            "signal_sources": [],
        })
        item["display_name"] = item["display_name"] or _display_name(row)
        weather_date = str(row.get("weather_date_local") or "")
        try:
            date.fromisoformat(weather_date)
        except ValueError:
            invalid_target_rows += 1
            continue
        item["weather_days"].add((str(row.get("canonical_city") or CITY), weather_date))
        item["weeks"].add(_week_key(weather_date))
        item["months"].add(weather_date[:7])
        item["events"].add((str(row.get("event_id") or ""), str(row.get("event_slug") or "")))
        item["conditions"].add(condition_id)
        item["assets"].add(_asset(row))
        item["fill_count"] += 1
        side = str(row.get("side") or "").upper()
        if side == "BUY":
            item["buy_fill_count"] += 1
        elif side == "SELL":
            item["sell_fill_count"] += 1
        size = _size(row)
        if size is not None and size >= 0:
            item["observed_share_volume"] += size
        usd, notional_source = _trade_usd(row)
        item["notional_source_counts"][notional_source] += 1
        if usd is None:
            item["notional_missing_count"] += 1
        else:
            item["observed_notional_usd"] += usd
        item["dates"].append(weather_date)

    rows: list[dict[str, Any]] = []
    for wallet, item in grouped.items():
        dates = sorted(item["dates"])
        first, last = dates[0], dates[-1]
        first_date = date.fromisoformat(first)
        last_date = date.fromisoformat(last)
        calendar_days = (last_date - first_date).days + 1
        active_days = len(item["weather_days"])
        active_months = len(item["months"])
        active_weeks = len(item["weeks"])
        benchmark = wallet in KNOWN_BENCHMARK_WALLETS
        eligibility = (
            "KNOWN_BENCHMARK" if benchmark else
            "ELIGIBLE" if active_days >= 10 and active_months >= 2 else
            "WATCHLIST" if 5 <= active_days <= 9 else
            "BELOW_MINIMUM"
        )
        source_counts = item["notional_source_counts"]
        if source_counts["unavailable"]:
            notional_source = "PARTIAL_MISSING"
        elif source_counts["usdcSize"] and source_counts["price_x_size"]:
            notional_source = "MIXED_USDC_AND_PRICE_X_SIZE"
        elif source_counts["usdcSize"]:
            notional_source = "OFFICIAL_USDC_SIZE"
        else:
            notional_source = "PRICE_X_SIZE_FALLBACK"
        rows.append({
            "wallet": wallet,
            "display_name": item["display_name"],
            "first_beijing_trade_date": first,
            "last_beijing_trade_date": last,
            "active_weather_days": active_days,
            "active_weeks": active_weeks,
            "active_months": active_months,
            "fill_count": item["fill_count"],
            "buy_fill_count": item["buy_fill_count"],
            "sell_fill_count": item["sell_fill_count"],
            "unique_events": len(item["events"]),
            "unique_conditions": len(item["conditions"]),
            "unique_assets": len(item["assets"]),
            "observed_share_volume": item["observed_share_volume"],
            "observed_notional_usd": item["observed_notional_usd"],
            "observed_notional_source": notional_source,
            "top_holder_weather_days": item["top_holder_weather_days"],
            "top_holder_appearances": item["top_holder_appearances"],
            "external_signal_count": item["external_signal_count"],
            "signal_sources": item["signal_sources"],
            "known_benchmark_wallet": "YES" if benchmark else "NO",
            "eligibility_status": eligibility,
            "activity_density": active_days / calendar_days if calendar_days else 1.0,
        })
    rows.sort(key=lambda row: row["wallet"])
    return rows, {
        "invalid_wallet_rows": invalid_wallet_rows,
        "invalid_target_rows": invalid_target_rows,
        "invalid_unique_wallet_count": len(invalid_wallet_values),
        "invalid_proxy_wallet_evidence": [
            {
                "reason": "INVALID_PROXY_WALLET_EVIDENCE",
                "raw_proxy_wallet": value,
                "row_count": count,
            }
            for value, count in sorted(invalid_wallet_values.items())
        ],
    }


def _descending_key(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(-float(row.get(field) or 0) for field in fields) + (str(row.get("wallet") or ""),)


def _long_term_sort(row: dict[str, Any]) -> tuple[Any, ...]:
    return _descending_key(row, ("active_weather_days", "active_weeks", "active_months", "top_holder_weather_days", "external_signal_count", "fill_count"))


def _external_sort(row: dict[str, Any]) -> tuple[Any, ...]:
    return _descending_key(row, ("external_signal_count", "top_holder_weather_days", "active_weather_days", "active_weeks", "active_months", "fill_count"))


def _emerging_sort(row: dict[str, Any]) -> tuple[Any, ...]:
    return _descending_key(row, ("active_weather_days", "active_weeks", "activity_density", "active_months", "fill_count"))


def _reserve_sort(row: dict[str, Any]) -> tuple[Any, ...]:
    return _descending_key(row, ("active_weather_days", "active_weeks", "active_months", "external_signal_count", "fill_count"))


def select_candidate_pool(
    wallet_rows: Iterable[dict[str, Any]],
    *,
    date_to: date,
    max_candidates: int = 30,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = [row for row in wallet_rows if row.get("known_benchmark_wallet") != "YES"]
    by_wallet = {row["wallet"]: row for row in rows}
    selected: dict[str, tuple[str, str]] = {}

    long_term = sorted(
        (row for row in rows if row["active_weather_days"] >= 10 and row["active_months"] >= 2 and row["active_weeks"] >= 6),
        key=_long_term_sort,
    )[:18]
    for row in long_term:
        selected.setdefault(row["wallet"], ("LONG_TERM_ACTIVE", "TIER_1"))

    external = sorted(
        (
            row for row in rows
            if row["wallet"] not in selected
            and row["active_weather_days"] >= 5
            and row["active_weeks"] >= 3
            and row["external_signal_count"] >= 1
        ),
        key=_external_sort,
    )[:6]
    for row in external:
        selected.setdefault(row["wallet"], ("STRONG_EXTERNAL_SIGNAL", "TIER_2"))

    cutoff = date_to - timedelta(days=60)
    emerging = sorted(
        (
            row for row in rows
            if row["wallet"] not in selected
            and date.fromisoformat(row["first_beijing_trade_date"]) >= cutoff
            and row["active_weather_days"] >= 8
            and row["active_weeks"] >= 4
        ),
        key=_emerging_sort,
    )[:6]
    for row in emerging:
        selected.setdefault(row["wallet"], ("EMERGING_HIGH_DENSITY", "TIER_3"))

    reserve = sorted(
        (row for row in rows if row["wallet"] not in selected and row["active_weather_days"] >= 10 and row["active_months"] >= 2),
        key=_reserve_sort,
    )
    for row in reserve:
        if len(selected) >= max_candidates:
            break
        selected.setdefault(row["wallet"], ("GENERAL_RESERVE", "TIER_4"))

    selected_rows: list[dict[str, Any]] = []
    for wallet, (channel, tier) in selected.items():
        if wallet not in by_wallet:
            continue
        row = dict(by_wallet[wallet])
        row["selection_channel"] = channel
        row["priority_tier"] = tier
        row["profitability_run_status"] = "NOT_RUN"
        row["candidate_status"] = "PROFITABILITY_PENDING"
        selected_rows.append(row)

    selected_rows.sort(
        key=lambda row: _descending_key(
            row,
            ("active_weather_days", "active_weeks", "active_months", "external_signal_count", "top_holder_weather_days", "activity_density", "fill_count"),
        )
    )
    for rank, row in enumerate(selected_rows[:max_candidates], start=1):
        row["discovery_priority_rank"] = rank
    selected_rows = selected_rows[:max_candidates]
    counts = Counter(row["selection_channel"] for row in selected_rows)
    return selected_rows, {
        "long_term_active_selected": counts["LONG_TERM_ACTIVE"],
        "strong_external_signal_selected": counts["STRONG_EXTERNAL_SIGNAL"],
        "emerging_high_density_selected": counts["EMERGING_HIGH_DENSITY"],
        "general_reserve_selected": counts["GENERAL_RESERVE"],
    }


def build_watchlist(wallet_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in wallet_rows:
        if row.get("known_benchmark_wallet") == "YES":
            continue
        if 5 <= int(row.get("active_weather_days") or 0) <= 9:
            item = dict(row)
            item["candidate_status"] = "WATCHLIST"
            result.append(item)
    return sorted(result, key=_reserve_sort)


def _sanity_check(
    fills: Iterable[dict[str, Any]],
    wallet_rows: list[dict[str, Any]],
    target_markets: Iterable[dict[str, Any]],
    sample_size: int = 5,
) -> tuple[str, list[dict[str, Any]]]:
    targets = _target_by_condition(target_markets)
    expected: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"fill_count": 0, "weather_days": set()}
    )
    for raw in fills:
        wallet = _wallet(_source_wallet(raw))
        condition = _condition_id(raw)
        target = targets.get(condition)
        if wallet and target:
            expected[wallet]["fill_count"] += 1
            expected[wallet]["weather_days"].add((target.get("canonical_city", CITY), target.get("weather_date_local", "")))
    selected_wallets = [row["wallet"] for row in wallet_rows[:sample_size]]
    selected_wallets.extend(wallet for wallet in KNOWN_BENCHMARK_WALLETS if wallet not in selected_wallets)
    checks = []
    status = "PASS"
    rows_by_wallet = {row["wallet"]: row for row in wallet_rows}
    for wallet in selected_wallets:
        row = rows_by_wallet.get(wallet)
        if not row:
            status = "NEEDS_REVIEW"
            checks.append({"wallet": wallet, "status": "MISSING_FROM_AGGREGATE"})
            continue
        observed_fill = expected[wallet]["fill_count"]
        observed_days = len(expected[wallet]["weather_days"])
        check_status = "PASS" if observed_fill == row["fill_count"] and observed_days == row["active_weather_days"] else "FAIL"
        if check_status != "PASS":
            status = "NEEDS_REVIEW"
        checks.append({
            "wallet": wallet,
            "status": check_status,
            "fill_count": row["fill_count"],
            "recomputed_fill_count": observed_fill,
            "active_weather_days": row["active_weather_days"],
            "recomputed_active_weather_days": observed_days,
        })
    return status, checks


def _source_statuses() -> dict[str, Any]:
    return {
        "official_public_fills": {
            "status": "INTEGRATED_EVENT_TRADES",
            "endpoint": "/trades",
            "query": "eventId + takerOnly=false",
            "request_count": 0,
            "failure_count": 0,
        },
        "official_activity": {
            "status": "NOT_USED_V1",
            "reason": "Data API returned 400 for eventId; wallet discovery does not require a second activity view",
            "request_count": 0,
            "failure_count": 0,
        },
        "top_holder": {"status": "NOT_INTEGRATED_V1", "request_count": 0, "failure_count": 0},
        "husky_smart_money": {"status": "NOT_INTEGRATED_V1", "request_count": 0, "failure_count": 0},
        "polymarket_analytics": {"status": "NOT_INTEGRATED_V1", "request_count": 0, "failure_count": 0},
        "official_weather_leaderboard": {"status": "NOT_INTEGRATED_V1", "request_count": 0, "failure_count": 0},
        "manual_candidate": {"status": "NOT_AVAILABLE", "request_count": 0, "failure_count": 0},
    }


def _discovery_status(
    target_markets: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    request_failures: int,
    invalid_wallet_rows: int,
) -> str:
    if not target_markets:
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    if request_failures or invalid_wallet_rows:
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    if len(audits) != len(_target_by_condition(target_markets)):
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    if any(row.get("completeness_status") != "COMPLETE" for row in audits):
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    return "READY"


def _render_summary(payload: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# Beijing highest-temperature trader discovery",
        "",
        "This is a Discovery Priority list based on observable public fills. It is not a profitability or expert ranking.",
        "",
        f"DISCOVERY_STATUS={payload['discovery_status']}",
        f"CITY={payload['city']} ICAO={payload['icao']}",
        f"DATE_RANGE={payload['date_from']}..{payload['date_to']}",
        "",
        "## Counts",
        "",
        f"- Target events: {payload['target_event_count']}",
        f"- Target conditions: {payload['target_condition_count']}",
        f"- Public fills: {payload['total_public_fills']}",
        f"- Unique wallets: {payload['total_unique_wallets']}",
        f"- New candidate pool: {payload['candidate_wallet_count']}",
        "",
        "## Discovery Priority",
        "",
        "| Rank | Wallet | Channel | Days | Weeks | Months | Fills |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for row in candidates:
        lines.append(
            f"| {row['discovery_priority_rank']} | {row['wallet']} | {row['selection_channel']} | "
            f"{row['active_weather_days']} | {row['active_weeks']} | {row['active_months']} | {row['fill_count']} |"
        )
    return "\n".join(lines) + "\n"


def discover(
    date_from: str,
    date_to: str,
    *,
    city: str = CITY,
    output_root: Path,
    max_workers: int = DEFAULT_WORKERS,
    force_refresh: bool = False,
    saved_discovery_manifest: Path | None = None,
) -> dict[str, Any]:
    start, end = parse_date_range(date_from, date_to)
    normalized_city = normalize_cities([city])
    if normalized_city != [CITY]:
        raise ValueError("Discovery v1 Phase 1 is fixed to city=beijing")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_root = output_root / "_public_evidence"
    if saved_discovery_manifest is not None and force_refresh:
        raise ValueError("--force-refresh cannot be combined with --saved-discovery-manifest")
    if saved_discovery_manifest is not None:
        target_markets, fills, audits, cache_audits = load_manifest_evidence(
            saved_discovery_manifest,
            expected_date_from=start.isoformat(),
            expected_date_to=end.isoformat(),
        )
        target_status = "COMPLETE"
        client: PublicGetClient | None = None
        canonical_index_path = _resolve_manifest_relative(
            Path(saved_discovery_manifest).resolve().parent,
            _read_json_object(Path(saved_discovery_manifest).resolve(), "discovery manifest").get("canonical_evidence_index"),
            "canonical evidence index",
        )
    else:
        client = PublicGetClient(evidence_root / "raw_event_batch")
        target_markets, target_status = discover_target_markets(
            client, start, end, normalized_city,
        )
        evidence_root.mkdir(parents=True, exist_ok=True)
        write_json(evidence_root / "target_markets.json", target_markets)
        target_conditions = sorted(_target_by_condition(target_markets))
        if force_refresh:
            for path in (evidence_root / "conditions").glob("*.json"):
                path.unlink()
        fills, audits, cache_audits = collect_market_evidence(
            client, target_markets, evidence_root, max_workers=max_workers,
        )
        canonical_index_path = build_canonical_evidence_index(
            evidence_root,
            target_markets,
            audits,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
    target_conditions = sorted(_target_by_condition(target_markets))
    wallet_rows, aggregate_quality = aggregate_wallets(fills, target_markets)
    candidates, channel_counts = select_candidate_pool(wallet_rows, date_to=end)
    watchlist = build_watchlist(wallet_rows)
    sanity_status, sanity_checks = _sanity_check(fills, wallet_rows, target_markets)
    request_audit = list(client.requests) if client is not None else []
    request_audit.extend(cache_audits)
    request_failures = sum(row.get("success") is not True for row in request_audit if row.get("method") == "GET")
    network_request_count = sum(row.get("method") == "GET" for row in request_audit)
    public_fill_request_count = sum(
        row.get("method") == "GET" and str(row.get("url") or "").startswith(DATA_API)
        for row in request_audit
    )
    market_discovery_request_count = sum(
        row.get("method") == "GET" and str(row.get("url") or "").startswith(GAMMA_API)
        for row in request_audit
    )
    cache_hit_count = sum(row.get("method") in {"CACHE", "MANIFEST_CACHE"} for row in request_audit)
    benchmark_a = KNOWN_BENCHMARK_WALLETS.keys().__iter__().__next__()
    benchmark_b = list(KNOWN_BENCHMARK_WALLETS.keys())[1]
    wallet_set = {row["wallet"] for row in wallet_rows}
    benchmark_a_discovered = "YES" if benchmark_a in wallet_set else "NO"
    benchmark_b_discovered = "YES" if benchmark_b in wallet_set else "NO"
    status = _discovery_status(
        target_markets,
        audits,
        request_failures + (1 if target_status != "COMPLETE" else 0),
        aggregate_quality["invalid_wallet_rows"],
    )
    if status != "BLOCKED_INCOMPLETE_EVIDENCE" and (
        benchmark_a_discovered != "YES" or benchmark_b_discovered != "YES" or sanity_status != "PASS"
    ):
        status = "NEEDS_REVIEW"
    target_events = {(row.get("event_id", ""), row.get("event_slug", "")) for row in target_markets}
    payload = {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "city": CITY,
        "icao": ICAO,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "market_discovery_source": "src.polymarket_highest_temperature_trader_pattern_v1.discover_target_markets",
        "public_fill_evidence_source": "official_data_api_trades_event_id_takerOnly_false",
        "market_discovery_status": target_status,
        "target_event_count": len(target_events),
        "target_condition_count": len(target_conditions),
        "public_fill_request_count": public_fill_request_count,
        "market_discovery_request_count": market_discovery_request_count,
        "network_request_count": network_request_count,
        "cache_hit_count": cache_hit_count,
        "request_audit_count": len(request_audit),
        "public_fill_request_failure_count": request_failures,
        "total_public_fills": len(fills),
        "total_unique_wallets": len(wallet_rows),
        "wallet_count_total": len(wallet_rows),
        "eligible_wallet_count": sum(row["eligibility_status"] == "ELIGIBLE" for row in wallet_rows),
        "watchlist_wallet_count": len(watchlist),
        "candidate_wallet_count": len(candidates),
        "known_benchmark_a_discovered": benchmark_a_discovered,
        "known_benchmark_b_discovered": benchmark_b_discovered,
        "known_benchmark_a_activity": next((row for row in wallet_rows if row["wallet"] == benchmark_a), None),
        "known_benchmark_b_activity": next((row for row in wallet_rows if row["wallet"] == benchmark_b), None),
        "selection_rules": {
            "base_eligibility": {"active_weather_days_min": 10, "active_months_min": 2},
            "watchlist": {"active_weather_days_min": 5, "active_weather_days_max": 9},
            "long_term_active": {"slots": 18, "active_weather_days_min": 10, "active_months_min": 2, "active_weeks_min": 6},
            "strong_external_signal": {"slots": 6, "active_weather_days_min": 5, "active_weeks_min": 3, "external_signal_min": 1},
            "emerging_high_density": {"slots": 6, "first_trade_cutoff_days": 60, "active_weather_days_min": 8, "active_weeks_min": 4},
            "general_reserve": {"max_total_candidates": 30},
            "active_weeks_definition": "ISO calendar year-week from canonical Beijing weather_date_local",
            "activity_density_definition": "active_weather_days / inclusive calendar days between first and last Beijing weather trade date",
            "fill_count_is_hard_gate": False,
            "top_holder_is_hard_gate": False,
            "profitability_run_status": "NOT_RUN",
        },
        "channel_counts": channel_counts,
        "external_sources": _source_statuses(),
        "discovery_status": status,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "request_failure_count": request_failures,
        "pagination_complete": "YES" if all(row.get("completeness_status") == "COMPLETE" for row in audits) and target_status == "COMPLETE" else "NO",
        "sanity_check_status": sanity_status,
        "sanity_checks": sanity_checks,
        "aggregate_quality": aggregate_quality,
        "invalid_proxy_wallet_row_count": aggregate_quality["invalid_wallet_rows"],
        "invalid_unique_wallet_count": aggregate_quality["invalid_unique_wallet_count"],
        "invalid_proxy_wallet_evidence": aggregate_quality["invalid_proxy_wallet_evidence"],
        "target_condition_ids": target_conditions,
        "canonical_evidence_index": canonical_index_path.relative_to(output_root).as_posix(),
        "canonical_evidence_index_sha256": sha256_file(canonical_index_path),
        "canonical_evidence_index_schema_version": CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION,
        "evidence_load_manifest_driven": "YES",
        "unreferenced_raw_can_affect_rerun": "NO",
        "evidence_rerun_mode": "MANIFEST_DRIVEN" if saved_discovery_manifest is not None else "NETWORK_OR_CACHE_BUILD",
        "EVIDENCE_LOAD_MANIFEST_DRIVEN": "YES",
        "UNREFERENCED_RAW_CAN_AFFECT_RERUN": "NO",
    }
    write_csv(output_root / "all_wallets.csv", wallet_rows, ALL_WALLET_FIELDS)
    write_csv(output_root / "eligible_wallets.csv", [row for row in wallet_rows if row["eligibility_status"] == "ELIGIBLE"], ALL_WALLET_FIELDS)
    write_csv(output_root / "watchlist.csv", watchlist, ALL_WALLET_FIELDS + ["candidate_status"])
    write_csv(output_root / "candidate_pool.csv", candidates, CANDIDATE_FIELDS)
    write_json(output_root / "request_audit.json", request_audit)
    write_json(output_root / "discovery_manifest.json", payload)
    write_json(output_root / "discovery_summary.json", payload)
    (output_root / "discovery_summary.md").write_text(_render_summary(payload, candidates), encoding="utf-8")
    return {"summary": payload, "wallets": wallet_rows, "watchlist": watchlist, "candidates": candidates, "audits": audits}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default=CITY)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--saved-discovery-manifest",
        type=Path,
        help="rerun from one manifest-referenced canonical evidence index without network calls",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    discover(
        args.date_from,
        args.date_to,
        city=args.city,
        output_root=Path(args.output_root),
        max_workers=max(1, args.max_workers),
        force_refresh=args.force_refresh,
        saved_discovery_manifest=args.saved_discovery_manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
