#!/usr/bin/env python3
"""Analyze observable public fills in Polymarket daily highest-temperature markets.

This module is deliberately public-data-only and GET-only. Public trades are
fills, not original orders: one order may create several fills, while unfilled
and cancelled orders are normally not observable. The module does not compute
PnL, connect an account, sign, submit, cancel, or interpret Negative Risk.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = "polymarket_highest_temperature_trader_pattern_v1"
EVIDENCE_SCHEMA = "polymarket_highest_temperature_public_evidence_v1"
LEGACY_HUSKY_EVIDENCE_SCHEMA = "husky_beijing_portable_evidence_v1"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
NO_NETWORK_ENV = "POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK"
PUBLIC_DATA_ONLY = True
PUBLIC_GET_ONLY = True
ACCOUNT_CONNECTION = False
SIGNING = False
REAL_ORDER = False
FORMAL_STARTED = False
NETWORK_CALL_COUNT = 0
HUSKY_WALLET = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"


def _default_timezone_registry_path() -> Path:
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parents[1] / "config/highest_temperature_city_timezones_v1.json",
        module_path.parents[1] / "references/highest_temperature_city_timezones_v1.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


DEFAULT_TIMEZONE_REGISTRY = _default_timezone_registry_path()

PRICE_BANDS = (
    ("PRICE_0_10C", Decimal("0.00"), Decimal("0.10"), False),
    ("PRICE_10_30C", Decimal("0.10"), Decimal("0.30"), False),
    ("PRICE_30_70C", Decimal("0.30"), Decimal("0.70"), False),
    ("PRICE_70_90C", Decimal("0.70"), Decimal("0.90"), False),
    ("PRICE_90_100C", Decimal("0.90"), Decimal("1.00"), True),
)
SHARES_BANDS = (
    ("SHARES_0_100", Decimal("0"), Decimal("100")),
    ("SHARES_100_500", Decimal("100"), Decimal("500")),
    ("SHARES_500_PLUS", Decimal("500"), None),
)
CORE_RELATIVE_DAYS = {"D-2", "D-1", "D0"}
CORE_REPORT_BUCKETS = {
    "D-2", "D-1", "D0_00_08", "D0_08_12", "D0_12_16", "D0_16_24"
}

MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        (
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )
    )
    if name
}
EVENT_SLUG_RE = re.compile(
    r"^highest-temperature-in-(?P<city>[a-z0-9]+(?:-[a-z0-9]+)*)-on-"
    r"(?P<month>[a-z]+)-(?P<day>\d{1,2})(?:-(?P<year>\d{4}))?(?:$|-)",
    re.IGNORECASE,
)
TITLE_CITY_RE = re.compile(
    r"highest\s+temperature\s+in\s+(?P<city>.+?)\s+(?:be|on)\b",
    re.IGNORECASE,
)
TITLE_DATE_RE = re.compile(
    r"\bon\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
TITLE_BUCKET_RE = re.compile(
    r"\bbe\s+(?P<value>-?\d+(?:\.\d+)?)\s*\u00b0?\s*(?P<unit>[CF])"
    r"(?:\s+or\s+(?P<tail>below|higher))?\b",
    re.IGNORECASE,
)
TITLE_RANGE_BUCKET_RE = re.compile(
    r"\bbetween\s+(?P<low>-?\d+(?:\.\d+)?)\s*(?:-|to|and)\s*"
    r"(?P<high>-?\d+(?:\.\d+)?)\s*\u00b0?\s*(?P<unit>[CF])\b",
    re.IGNORECASE,
)
SLUG_BUCKET_RE = re.compile(
    r"-(?P<value>-?\d+(?:\.\d+)?)(?P<unit>[cf])(?P<tail>orbelow|orhigher)?$",
    re.IGNORECASE,
)
SLUG_RANGE_BUCKET_RE = re.compile(
    r"(?:^|-)between-(?P<low>-?\d+(?:\.\d+)?)-"
    r"(?P<high>-?\d+(?:\.\d+)?)(?P<unit>[cf])(?:-|$)",
    re.IGNORECASE,
)
WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")

FILL_FIELDS = [
    "wallet", "event_key", "weather_date_local", "canonical_city",
    "original_city_slug", "weather_metric", "condition_id", "asset",
    "temperature_bucket", "bucket_kind", "bucket_low", "bucket_high", "unit",
    "outcome", "side", "price", "implied_yes_equivalent_price", "shares",
    "trade_usd", "trade_usd_source", "timestamp_epoch", "trade_time_utc",
    "trade_time_beijing", "trade_time_market_local", "market_timezone",
    "relative_weather_day", "report_time_bucket", "transaction_hash",
    "event_slug", "slug", "title", "market_identity_status", "source_types",
]


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


# These small public-fill helpers preserve the reviewed semantics from
# husky_beijing_full_trade_study_v1, but live here so the generated personal
# plugin is self-contained and does not copy or import the protected study.
def epoch_seconds(value: Any) -> int:
    """Normalize second, millisecond, microsecond, or nanosecond epochs."""
    number = _finite(value)
    if number is None:
        raise ValueError(f"invalid timestamp: {value!r}")
    magnitude = abs(number)
    if magnitude >= 1e17:
        number /= 1e9
    elif magnitude >= 1e14:
        number /= 1e6
    elif magnitude >= 1e11:
        number /= 1e3
    return int(number)


def iso_utc(value: Any) -> str:
    return datetime.fromtimestamp(epoch_seconds(value), timezone.utc).isoformat()


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def canonical_exact_decimal(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        raise ValueError(f"invalid decimal: {value!r}")
    rendered = format(number.normalize(), "f")
    return "0" if rendered == "-0" else rendered


def _key_decimal(value: Any) -> str:
    number = _decimal(value)
    return format(number.normalize(), "f") if number is not None else ""


def stable_trade_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Return the portable public-fill identity used for deduplication."""
    return (
        str(row.get("timestamp") or row.get("timestamp_epoch") or ""),
        str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
        str(row.get("conditionId") or row.get("condition_id") or "").lower(),
        str(row.get("asset") or ""),
        str(row.get("side") or "").upper(),
        _key_decimal(row.get("price")),
        _key_decimal(row.get("size") if "size" in row else row.get("shares")),
    )


def deduplicate_records(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate public records while preserving source provenance flags."""
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    duplicates = 0
    for source in rows:
        row = dict(source)
        key = stable_trade_key(row)
        if key not in by_key:
            by_key[key] = row
            continue
        duplicates += 1
        target = by_key[key]
        for flag in (
            "source_repository_trade",
            "source_repository_activity",
            "source_current_public_api",
        ):
            target[flag] = bool(target.get(flag)) or bool(row.get(flag))
        existing = str(target.get("source_row_number") or "")
        incoming = str(row.get("source_row_number") or "")
        target["source_row_number"] = "|".join(
            filter(None, dict.fromkeys([existing, incoming]))
        )
    return list(by_key.values()), duplicates


@dataclass(frozen=True)
class Window:
    start: int
    end: int


def thirty_day_windows(start: int, end: int) -> Iterator[Window]:
    cursor = start
    step = 30 * 24 * 60 * 60
    while cursor < end:
        nxt = min(cursor + step, end)
        yield Window(cursor, nxt)
        cursor = nxt


def split_window(window: Window) -> tuple[Window, Window]:
    middle = (window.start + window.end) // 2
    if middle <= window.start or middle >= window.end:
        raise RuntimeError(f"cannot split saturated one-second window: {window}")
    return Window(window.start, middle), Window(middle, window.end)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(value), encoding="utf-8")


def csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        seen: set[str] = set()
        for row in materialized:
            for field in row:
                if field not in seen:
                    seen.add(field)
                    fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in materialized:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def normalize_wallets(wallets: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in wallets:
        wallet = str(raw).strip().lower()
        if not WALLET_RE.fullmatch(wallet):
            raise ValueError(f"invalid wallet address: {raw!r}")
        if wallet not in seen:
            seen.add(wallet)
            result.append(wallet)
    if not result:
        raise ValueError("at least one wallet is required")
    return result


def canonical_city(value: Any) -> str:
    normalized = re.sub(r"[\s_-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-")


def normalize_cities(cities: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in cities or []:
        city = canonical_city(raw)
        if not city:
            continue
        if city not in seen:
            seen.add(city)
            result.append(city)
    return result


def parse_date_range(date_from: str, date_to: str) -> tuple[date, date]:
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if start > end:
        raise ValueError("date_from must be on or before date_to")
    return start, end


def _nearest_calendar_date(month: int, day: int, reference: date) -> date | None:
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    return min(candidates, key=lambda candidate: abs((candidate - reference).days)) if candidates else None


def _parsed_slug(value: Any, fallback_date: date | None = None) -> dict[str, Any] | None:
    match = EVENT_SLUG_RE.match(str(value or "").strip())
    if not match:
        return None
    month = MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    if match.group("year"):
        try:
            weather_date = date(int(match.group("year")), month, int(match.group("day")))
        except ValueError:
            return None
    elif fallback_date:
        weather_date = _nearest_calendar_date(month, int(match.group("day")), fallback_date)
        if weather_date is None:
            return None
    else:
        return None
    return {
        "canonical_city": canonical_city(match.group("city")),
        "original_city_slug": match.group("city").lower(),
        "weather_date_local": weather_date.isoformat(),
    }


def _parsed_title(value: Any, fallback_year: int | None = None) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not re.search(r"\bhighest\s+temperature\b", text, re.IGNORECASE):
        return None
    city_match = TITLE_CITY_RE.search(text)
    date_match = TITLE_DATE_RE.search(text)
    if not city_match:
        return None
    title_city = re.sub(r"\s*\([^)]*\)\s*$", "", city_match.group("city")).strip()
    result: dict[str, Any] = {"canonical_city": canonical_city(title_city)}
    if date_match:
        month = MONTHS.get(date_match.group("month").lower())
        year = int(date_match.group("year")) if date_match.group("year") else fallback_year
        if month and year:
            try:
                result["weather_date_local"] = date(
                    year, month, int(date_match.group("day"))
                ).isoformat()
            except ValueError:
                pass
    return result


def parse_temperature_bucket(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("title") or "")
    slug = str(row.get("slug") or "")
    range_match = TITLE_RANGE_BUCKET_RE.search(text) or SLUG_RANGE_BUCKET_RE.search(slug)
    if range_match:
        low = float(range_match.group("low"))
        high = float(range_match.group("high"))
        if low > high:
            low, high = high, low
        unit = range_match.group("unit").upper()
        return {
            "temperature_bucket": f"{low:g}-{high:g}\u00b0{unit}",
            "bucket_kind": "range",
            "bucket_low": low,
            "bucket_high": high,
            "unit": unit,
        }
    match = TITLE_BUCKET_RE.search(text) or SLUG_BUCKET_RE.search(slug)
    if not match:
        label = str(row.get("temperature_bucket") or row.get("bucket_label") or "UNKNOWN")
        kind = str(row.get("bucket_kind") or "UNKNOWN").lower()
        unit = str(row.get("unit") or "").upper()
        low = _finite(row.get("bucket_low"))
        high = _finite(row.get("bucket_high"))
        return {
            "temperature_bucket": label,
            "bucket_kind": kind if kind in {"exact", "range", "below", "above"} else "UNKNOWN",
            "bucket_low": low,
            "bucket_high": high,
            "unit": unit if unit in {"C", "F"} else "UNKNOWN",
        }
    number = float(match.group("value"))
    unit = match.group("unit").upper()
    raw_tail = (match.groupdict().get("tail") or "").lower()
    kind = "below" if raw_tail in {"below", "orbelow"} else (
        "above" if raw_tail in {"higher", "orhigher"} else "exact"
    )
    suffix = " or below" if kind == "below" else (" or higher" if kind == "above" else "")
    return {
        "temperature_bucket": f"{number:g}\u00b0{unit}{suffix}",
        "bucket_kind": kind,
        "bucket_low": None if kind == "below" else number,
        "bucket_high": None if kind == "above" else number,
        "unit": unit,
    }


def parse_highest_temperature_market(row: dict[str, Any]) -> dict[str, Any] | None:
    """Parse identity without accepting other weather market types."""
    explicit_metric = str(row.get("weather_metric") or "").strip().lower()
    if explicit_metric and explicit_metric not in {"highest_temperature", "highest", "high", "tmax"}:
        return None
    reference_date: date | None = None
    explicit_reference = str(
        row.get("weather_date_local") or row.get("weather_date") or row.get("endDate") or row.get("end_date") or ""
    )[:10]
    try:
        reference_date = date.fromisoformat(explicit_reference)
    except ValueError:
        try:
            reference_date = datetime.fromtimestamp(
                epoch_seconds(row.get("timestamp") or row.get("timestamp_epoch")), timezone.utc
            ).date()
        except ValueError:
            pass
    values = [row.get("eventSlug"), row.get("event_slug"), row.get("slug")]
    slug_identities = [
        parsed for value in values if (parsed := _parsed_slug(value, reference_date))
    ]
    title_text = str(row.get("title") or "")
    if re.search(r"\b(lowest|minimum)\s+temperature\b|\brain(?:fall)?\b|\bforecast\b", title_text, re.I):
        return None
    if not slug_identities and not re.search(r"\bhighest\s+temperature\b", title_text, re.I):
        return None
    primary = slug_identities[0] if slug_identities else None
    fallback_year = (
        int(primary["weather_date_local"][:4]) if primary else (
            reference_date.year if reference_date else None
        )
    )
    title_identity = _parsed_title(title_text, fallback_year)
    if primary is None and not title_identity:
        return None
    identity = dict(primary or title_identity or {})
    if "weather_date_local" not in identity:
        explicit_date = str(row.get("weather_date_local") or row.get("weather_date") or "")[:10]
        try:
            identity["weather_date_local"] = date.fromisoformat(explicit_date).isoformat()
        except ValueError:
            return None
    if "canonical_city" not in identity or not identity["canonical_city"]:
        return None
    conflicts: list[str] = []
    for candidate in slug_identities[1:]:
        if any(candidate.get(key) != identity.get(key) for key in ("canonical_city", "weather_date_local")):
            conflicts.append("slug")
    if title_identity:
        if title_identity.get("canonical_city") != identity.get("canonical_city"):
            conflicts.append("title_city")
        title_date = title_identity.get("weather_date_local")
        if title_date and title_date != identity.get("weather_date_local"):
            conflicts.append("title_date")
    identity.update({
        "original_city_slug": identity.get("original_city_slug") or identity["canonical_city"],
        "weather_metric": "highest_temperature",
        "market_identity_status": "MARKET_IDENTITY_CONFLICT" if conflicts else "OBSERVED",
        "market_identity_conflicts": sorted(set(conflicts)),
        **parse_temperature_bucket(row),
    })
    return identity


def load_timezone_registry(
    path: Path = DEFAULT_TIMEZONE_REGISTRY,
    overrides: Iterable[str] | None = None,
) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    registry = {canonical_city(key): str(value) for key, value in payload["cities"].items()}
    for city, zone in list(registry.items()):
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid timezone registry entry: {city}={zone}") from exc
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(f"timezone override must be city=IANA/Zone: {raw!r}")
        city, zone = raw.split("=", 1)
        city = canonical_city(city)
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid IANA timezone: {zone!r}") from exc
        registry[city] = zone
    return registry


def classify_relative_weather_time(
    timestamp: Any,
    weather_date_local: str,
    market_timezone: str | None,
) -> dict[str, str | None]:
    epoch = epoch_seconds(timestamp)
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    result: dict[str, str | None] = {
        "trade_time_utc": utc.isoformat(),
        "trade_time_beijing": utc.astimezone(BEIJING_TIMEZONE).isoformat(),
        "trade_time_market_local": None,
        "relative_weather_day": "UNKNOWN",
        "report_time_bucket": "UNKNOWN",
    }
    if not market_timezone:
        return result
    local = utc.astimezone(ZoneInfo(market_timezone))
    target = date.fromisoformat(weather_date_local)
    delta = (local.date() - target).days
    result["trade_time_market_local"] = local.isoformat()
    if delta < -2:
        result["relative_weather_day"] = "EARLIER_THAN_D2"
        result["report_time_bucket"] = "EARLIER_THAN_D2"
    elif delta == -2:
        result["relative_weather_day"] = "D-2"
        result["report_time_bucket"] = "D-2"
    elif delta == -1:
        result["relative_weather_day"] = "D-1"
        result["report_time_bucket"] = "D-1"
    elif delta == 0:
        result["relative_weather_day"] = "D0"
        if local.hour < 8:
            result["report_time_bucket"] = "D0_00_08"
        elif local.hour < 12:
            result["report_time_bucket"] = "D0_08_12"
        elif local.hour < 16:
            result["report_time_bucket"] = "D0_12_16"
        else:
            result["report_time_bucket"] = "D0_16_24"
    else:
        result["relative_weather_day"] = "POST_EVENT"
        result["report_time_bucket"] = "POST_EVENT"
    return result


def price_band(value: Any) -> str:
    price = _decimal(value)
    if price is None:
        raise ValueError(f"invalid price: {value!r}")
    for label, low, high, inclusive_high in PRICE_BANDS:
        if low <= price and (price <= high if inclusive_high else price < high):
            return label
    raise ValueError(f"price outside [0,1]: {value!r}")


def cumulative_shares_band(value: Any) -> str:
    shares = _decimal(value)
    if shares is None or shares < 0:
        raise ValueError(f"invalid cumulative shares: {value!r}")
    for label, low, high in SHARES_BANDS:
        if shares >= low and (high is None or shares < high):
            return label
    raise AssertionError("unreachable shares band")


def _source_wallet(row: dict[str, Any]) -> str:
    return str(row.get("proxyWallet") or row.get("proxy_wallet") or row.get("wallet") or "").lower()


def _event_key(identity: dict[str, Any]) -> str:
    return (
        f"{identity['weather_date_local']}__{identity['canonical_city']}__"
        "highest_temperature"
    )


def _activity_usd_map(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, ...], list[tuple[Decimal | None, float]]]:
    result: dict[tuple[str, ...], list[tuple[Decimal | None, float]]] = defaultdict(list)
    for row in rows:
        if str(row.get("type") or "TRADE").upper() != "TRADE":
            continue
        amount = _finite(row.get("usdcSize"))
        if amount is not None:
            result[stable_trade_key(row)[:-1]].append(
                (_decimal(row.get("size") if "size" in row else row.get("shares")), amount)
            )
    return result


def normalize_fill_rows(
    wallet: str,
    raw_rows: Iterable[dict[str, Any]],
    *,
    activity_rows: Iterable[dict[str, Any]] = (),
    date_from: date,
    date_to: date,
    cities: Iterable[str] = (),
    timezone_registry: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    requested_cities = set(cities)
    activity_usd = _activity_usd_map(activity_rows)
    activity_used: dict[tuple[str, ...], set[int]] = defaultdict(set)
    discovery: list[dict[str, Any]] = []
    quality: Counter[str] = Counter()
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(raw_rows, start=1):
        row = dict(source)
        if _source_wallet(row) != wallet:
            quality["wallet_mismatch_count"] += 1
            continue
        identity = parse_highest_temperature_market(row)
        if identity is None:
            quality["unparseable_market_count"] += 1
            discovery.append({"source_row": index, "status": "UNPARSEABLE_OR_NOT_HIGHEST", "title": row.get("title", ""), "event_slug": row.get("eventSlug", ""), "slug": row.get("slug", "")})
            continue
        weather_day = date.fromisoformat(identity["weather_date_local"])
        if weather_day < date_from or weather_day > date_to:
            continue
        if requested_cities and identity["canonical_city"] not in requested_cities:
            continue
        discovery.append({
            "source_row": index,
            "status": identity["market_identity_status"],
            "canonical_city": identity["canonical_city"],
            "original_city_slug": identity["original_city_slug"],
            "weather_date_local": identity["weather_date_local"],
            "temperature_bucket": identity["temperature_bucket"],
            "condition_id": str(row.get("conditionId") or row.get("condition_id") or "").lower(),
            "asset": str(row.get("asset") or ""),
            "event_slug": row.get("eventSlug") or row.get("event_slug") or "",
            "slug": row.get("slug") or "",
            "title": row.get("title") or "",
        })
        if identity["market_identity_status"] == "MARKET_IDENTITY_CONFLICT":
            quality["market_identity_conflict_count"] += 1
            continue
        side = str(row.get("side") or "").strip().upper()
        outcome = str(row.get("outcome") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            quality["unknown_side_count"] += 1
            continue
        if outcome not in {"YES", "NO"}:
            quality["unknown_outcome_count"] += 1
            continue
        price = _finite(row.get("price"))
        shares = _finite(row.get("size") if "size" in row else row.get("shares"))
        if price is None or price < 0 or price > 1:
            quality["price_out_of_range_count"] += 1
            continue
        if shares is None or shares < 0:
            quality["shares_invalid_count"] += 1
            continue
        try:
            timestamp = epoch_seconds(row.get("timestamp") or row.get("timestamp_epoch"))
        except ValueError:
            quality["timestamp_invalid_count"] += 1
            continue
        timezone_name = timezone_registry.get(identity["canonical_city"])
        if not timezone_name:
            quality["unknown_timezone_fill_count"] += 1
        timing = classify_relative_weather_time(timestamp, identity["weather_date_local"], timezone_name)
        if timing["relative_weather_day"] == "UNKNOWN":
            quality["unknown_relative_day_count"] += 1
        if timing["relative_weather_day"] == "EARLIER_THAN_D2":
            quality["earlier_than_d2_count"] += 1
        if timing["relative_weather_day"] == "POST_EVENT":
            quality["post_event_fill_count"] += 1
        key = stable_trade_key(row)[:-1]
        candidates = [
            (candidate_index, candidate_size, candidate_amount)
            for candidate_index, (candidate_size, candidate_amount)
            in enumerate(activity_usd.get(key, []))
            if candidate_index not in activity_used[key]
        ]
        source_size = _decimal(shares)
        exact = [candidate for candidate in candidates if candidate[1] == source_size]
        selected = exact[0] if exact else (
            min(
                candidates,
                key=lambda candidate: abs(
                    (candidate[1] or Decimal(0)) - (source_size or Decimal(0))
                ),
            )
            if candidates else None
        )
        amount = selected[2] if selected else None
        if selected is not None:
            activity_used[key].add(selected[0])
            amount_source = "activity_usdcSize"
        else:
            amount = price * shares
            amount_source = "price_x_shares"
            quality["trade_usd_missing_count"] += 1
        normalized.append({
            "wallet": wallet,
            "event_key": _event_key(identity),
            "weather_date_local": identity["weather_date_local"],
            "canonical_city": identity["canonical_city"],
            "original_city_slug": identity["original_city_slug"],
            "weather_metric": "highest_temperature",
            "condition_id": str(row.get("conditionId") or row.get("condition_id") or "").lower(),
            "asset": str(row.get("asset") or ""),
            "temperature_bucket": identity["temperature_bucket"],
            "bucket_kind": identity["bucket_kind"],
            "bucket_low": identity["bucket_low"],
            "bucket_high": identity["bucket_high"],
            "unit": identity["unit"],
            "outcome": outcome,
            "side": side,
            "price": price,
            "implied_yes_equivalent_price": 1 - price if outcome == "NO" else None,
            "shares": shares,
            "trade_usd": amount,
            "trade_usd_source": amount_source,
            "timestamp_epoch": timestamp,
            **timing,
            "market_timezone": timezone_name,
            "transaction_hash": str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
            "event_slug": str(row.get("eventSlug") or row.get("event_slug") or ""),
            "slug": str(row.get("slug") or ""),
            "title": str(row.get("title") or ""),
            "market_identity_status": identity["market_identity_status"],
            "source_types": str(row.get("_source_types") or "trades"),
        })
    discovery_by_market: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in discovery:
        discovery_key = (
            str(item.get("status") or ""),
            str(item.get("condition_id") or ""),
            str(item.get("event_slug") or ""),
            str(item.get("slug") or ""),
        )
        discovery_by_market.setdefault(discovery_key, item)
    discovery = list(discovery_by_market.values())
    deduped, duplicate_count = deduplicate_records(normalized)
    quality["duplicate_fill_count"] += duplicate_count
    deduped.sort(key=lambda item: (item["timestamp_epoch"], item["transaction_hash"], item["asset"], item["side"], item["outcome"]))
    return deduped, discovery, quality


SAME_PRICE_KEY_FIELDS = (
    "wallet", "canonical_city", "weather_date_local", "event_key", "asset",
    "temperature_bucket", "bucket_kind", "outcome", "side",
    "canonical_exact_price", "relative_weather_day", "report_time_bucket",
)


def build_same_price_cumulative_groups(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in fills:
        enriched = dict(row)
        enriched["canonical_exact_price"] = canonical_exact_decimal(row["price"])
        key = tuple(enriched[field] for field in SAME_PRICE_KEY_FIELDS)
        groups[key].append(enriched)
    result: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items(), key=lambda item: tuple(str(value) for value in item[0])):
        ordered = sorted(rows, key=lambda row: row["timestamp_epoch"])
        shares = sum(float(row["shares"]) for row in rows)
        usd = sum(float(row["trade_usd"]) for row in rows)
        base = {field: value for field, value in zip(SAME_PRICE_KEY_FIELDS, key)}
        result.append({
            **base,
            "fill_count": len(rows),
            "cumulative_shares": shares,
            "cumulative_trade_usd": usd,
            "first_trade_time": ordered[0]["trade_time_utc"],
            "last_trade_time": ordered[-1]["trade_time_utc"],
            "price": float(ordered[0]["price"]),
            "price_band": price_band(ordered[0]["price"]),
            "shares_band": cumulative_shares_band(shares),
        })
    return result


def _aggregate_distribution(
    fills: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    dimensions: tuple[str, ...],
) -> list[dict[str, Any]]:
    fill_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    cumulative_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    shares_band_by_same_price_key = {
        tuple(row[field] for field in SAME_PRICE_KEY_FIELDS): row["shares_band"]
        for row in groups
    }
    for row in fills:
        if row["relative_weather_day"] == "EARLIER_THAN_D2":
            continue
        canonical_price = canonical_exact_decimal(row["price"])
        same_price_key = tuple(
            canonical_price if field == "canonical_exact_price" else row[field]
            for field in SAME_PRICE_KEY_FIELDS
        )
        enriched = {
            **row,
            "price_band": price_band(row["price"]),
            "shares_band": shares_band_by_same_price_key.get(same_price_key),
        }
        fill_groups[tuple(enriched.get(field) for field in dimensions)].append(enriched)
    for row in groups:
        if row["relative_weather_day"] == "EARLIER_THAN_D2":
            continue
        cumulative_groups[tuple(row.get(field) for field in dimensions)].append(row)
    keys = sorted(set(fill_groups) | set(cumulative_groups), key=lambda key: tuple(str(v) for v in key))
    result = []
    for key in keys:
        rows = fill_groups.get(key, [])
        same = cumulative_groups.get(key, [])
        shares = sum(float(row["shares"]) for row in rows)
        usd = sum(float(row["trade_usd"]) for row in rows)
        prices = [float(row["price"]) for row in rows]
        result.append({
            **{field: value for field, value in zip(dimensions, key)},
            "fill_count": len(rows),
            "same_price_cumulative_group_count": len(same),
            "cumulative_shares": shares,
            "trade_usd": usd,
            "event_count": len({row["event_key"] for row in rows}),
            "temperature_bucket_count": len({(row["temperature_bucket"], row["outcome"]) for row in rows}),
            "city_count": len({row["canonical_city"] for row in rows}),
            "minimum_price": min(prices) if prices else None,
            "maximum_price": max(prices) if prices else None,
            "weighted_average_price": (
                sum(float(row["price"]) * float(row["shares"]) for row in rows) / shares
                if shares else None
            ),
        })
    total_shares = sum(row["cumulative_shares"] for row in result)
    total_usd = sum(row["trade_usd"] for row in result)
    for row in result:
        row["shares_share"] = row["cumulative_shares"] / total_shares if total_shares else 0
        row["usd_share"] = row["trade_usd"] / total_usd if total_usd else 0
    return result


def _bucket_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["temperature_bucket"]), str(row["bucket_kind"]), str(row["outcome"]))


def _adjacent_yes_pairs(rows: list[dict[str, Any]]) -> list[list[str]]:
    representatives: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["outcome"] == "YES":
            representatives.setdefault(row["temperature_bucket"], row)
    exact = list(representatives.values())
    pairs = []
    for index, left in enumerate(exact):
        for right in exact[index + 1:]:
            if (
                left["unit"] == right["unit"]
                and left["bucket_kind"] == right["bucket_kind"] == "exact"
                and left["bucket_low"] is not None
                and right["bucket_low"] is not None
                and abs(float(left["bucket_low"]) - float(right["bucket_low"])) == 1
            ):
                pairs.append(sorted([left["temperature_bucket"], right["temperature_bucket"]]))
    return sorted(pairs)


def classify_event_temperature_structure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buys = [row for row in rows if row["side"] == "BUY"]
    yes = [row for row in buys if row["outcome"] == "YES"]
    no = [row for row in buys if row["outcome"] == "NO"]
    yes_buckets = {_bucket_identity(row) for row in yes}
    no_buckets = {_bucket_identity(row) for row in no}
    yes_names = {row["temperature_bucket"] for row in yes}
    no_names = {row["temperature_bucket"] for row in no}
    if not buys:
        structure = "NO_BUY"
    elif yes and no:
        structure = "MIXED_YES_NO"
    elif yes:
        structure = "SINGLE_YES_TEMPERATURE" if len(yes_buckets) == 1 else "MULTI_YES_ONLY"
    else:
        structure = "SINGLE_NO_TEMPERATURE" if len(no_buckets) == 1 else "MULTI_NO_ONLY"
    same = bool(yes_names & no_names)
    cross = any(y != n for y in yes_names for n in no_names)
    subtype = ""
    if structure == "MIXED_YES_NO":
        subtype = "BOTH" if same and cross else (
            "SAME_BUCKET_BOTH_SIDES" if same else "CROSS_BUCKET_YES_NO"
        )
    pairs = _adjacent_yes_pairs(yes)
    return {
        "event_buy_structure": structure,
        "mixed_yes_no_subtype": subtype,
        "yes_temperature_bucket_count": len(yes_buckets),
        "no_temperature_bucket_count": len(no_buckets),
        "yes_temperature_buckets": sorted(yes_names),
        "no_temperature_buckets": sorted(no_names),
        "adjacent_yes_pairs": pairs,
        "has_yes_tail_bucket": any(row["bucket_kind"] in {"below", "above"} for row in yes),
        "has_no_tail_bucket": any(row["bucket_kind"] in {"below", "above"} for row in no),
        "total_buy_yes_shares": sum(float(row["shares"]) for row in yes),
        "total_buy_yes_usd": sum(float(row["trade_usd"]) for row in yes),
        "total_buy_no_shares": sum(float(row["shares"]) for row in no),
        "total_buy_no_usd": sum(float(row["trade_usd"]) for row in no),
    }


def build_event_temperature_structures(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in fills:
        grouped[(row["wallet"], row["event_key"])].append(row)
    result = []
    for (wallet, event_key), rows in sorted(grouped.items()):
        first = rows[0]
        result.append({
            "wallet": wallet,
            "canonical_city": first["canonical_city"],
            "weather_date_local": first["weather_date_local"],
            "event_key": event_key,
            **classify_event_temperature_structure(rows),
        })
    return result


def _allocation_rows(fills: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cities = sorted({row["canonical_city"] for row in fills})
    for city in [*cities, "ALL_CITIES"]:
        scoped = fills if city == "ALL_CITIES" else [row for row in fills if row["canonical_city"] == city]
        scoped_groups = groups if city == "ALL_CITIES" else [row for row in groups if row["canonical_city"] == city]
        buy = [row for row in scoped if row["side"] == "BUY"]
        sell = [row for row in scoped if row["side"] == "SELL"]
        buy_yes = [row for row in buy if row["outcome"] == "YES"]
        buy_no = [row for row in buy if row["outcome"] == "NO"]
        sell_yes = [row for row in sell if row["outcome"] == "YES"]
        sell_no = [row for row in sell if row["outcome"] == "NO"]
        ratio_metrics = {}
        for prefix, yes_rows, no_rows in (
            ("buy", buy_yes, buy_no), ("sell", sell_yes, sell_no)
        ):
            yes_shares = sum(float(row["shares"]) for row in yes_rows)
            no_shares = sum(float(row["shares"]) for row in no_rows)
            yes_usd = sum(float(row["trade_usd"]) for row in yes_rows)
            no_usd = sum(float(row["trade_usd"]) for row in no_rows)
            ratio_metrics.update({
                f"{prefix}_yes_usd_share": yes_usd / (yes_usd + no_usd) if yes_usd + no_usd else 0,
                f"{prefix}_no_usd_share": no_usd / (yes_usd + no_usd) if yes_usd + no_usd else 0,
                f"{prefix}_yes_shares_share": yes_shares / (yes_shares + no_shares) if yes_shares + no_shares else 0,
                f"{prefix}_no_shares_share": no_shares / (yes_shares + no_shares) if yes_shares + no_shares else 0,
                f"{prefix}_yes_to_no_usd_ratio": yes_usd / no_usd if no_usd else None,
                f"{prefix}_yes_to_no_shares_ratio": yes_shares / no_shares if no_shares else None,
            })
        for side, outcome in (("BUY", "YES"), ("BUY", "NO"), ("SELL", "YES"), ("SELL", "NO")):
            rows = [row for row in scoped if row["side"] == side and row["outcome"] == outcome]
            same = [row for row in scoped_groups if row["side"] == side and row["outcome"] == outcome]
            side_rows = buy if side == "BUY" else sell
            shares = sum(float(row["shares"]) for row in rows)
            usd = sum(float(row["trade_usd"]) for row in rows)
            total_shares = sum(float(row["shares"]) for row in side_rows)
            total_usd = sum(float(row["trade_usd"]) for row in side_rows)
            result.append({
                "wallet": scoped[0]["wallet"] if scoped else "",
                "city": city,
                "side": side,
                "outcome": outcome,
                "fill_count": len(rows),
                "same_price_group_count": len(same),
                "shares": shares,
                "trade_usd": usd,
                "event_count": len({row["event_key"] for row in rows}),
                "side_shares_share": shares / total_shares if total_shares else 0,
                "side_usd_share": usd / total_usd if total_usd else 0,
                **ratio_metrics,
            })
    return result


def _leading(rows: list[dict[str, Any]], field: str, value_field: str = "trade_usd") -> str:
    amounts: Counter[str] = Counter()
    for row in rows:
        amounts[str(row[field])] += float(row.get(value_field) or 0)
    return max(amounts, key=lambda key: (amounts[key], key)) if amounts else "UNKNOWN"


def _summary(
    wallet: str,
    fills: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    structures: list[dict[str, Any]],
    quality: dict[str, Any],
    date_from: date,
    date_to: date,
    requested_cities: list[str],
    collection_start_utc: str,
    collection_end_utc: str,
) -> dict[str, Any]:
    combo = Counter(row["event_buy_structure"] for row in structures)
    subtype = Counter(row["mixed_yes_no_subtype"] for row in structures if row["mixed_yes_no_subtype"])
    counts = Counter((row["side"], row["outcome"]) for row in fills)
    buy_yes = [row for row in fills if row["side"] == "BUY" and row["outcome"] == "YES"]
    buy_no = [row for row in fills if row["side"] == "BUY" and row["outcome"] == "NO"]
    sell_yes = [row for row in fills if row["side"] == "SELL" and row["outcome"] == "YES"]
    sell_no = [row for row in fills if row["side"] == "SELL" and row["outcome"] == "NO"]
    core = [row for row in fills if row["relative_weather_day"] in CORE_RELATIVE_DAYS]
    d0 = [row for row in core if row["relative_weather_day"] == "D0"]
    return {
        "schema_version": SCHEMA_VERSION,
        "wallet": wallet,
        "weather_date_from": date_from.isoformat(),
        "weather_date_to": date_to.isoformat(),
        "requested_cities": requested_cities,
        "all_cities_default": not requested_cities,
        "discovered_cities": sorted({row["canonical_city"] for row in fills}),
        "collection_start_utc": collection_start_utc,
        "collection_end_utc": collection_end_utc,
        "weather_event_count": len({row["event_key"] for row in fills}),
        "market_count": len({row["condition_id"] for row in fills}),
        "total_public_fill_count": len(fills),
        "buy_fill_count": sum(row["side"] == "BUY" for row in fills),
        "sell_fill_count": sum(row["side"] == "SELL" for row in fills),
        "buy_yes_fill_count": counts[("BUY", "YES")],
        "buy_no_fill_count": counts[("BUY", "NO")],
        "sell_yes_fill_count": counts[("SELL", "YES")],
        "sell_no_fill_count": counts[("SELL", "NO")],
        "buy_yes_shares": sum(float(row["shares"]) for row in buy_yes),
        "buy_yes_trade_usd": sum(float(row["trade_usd"]) for row in buy_yes),
        "buy_no_shares": sum(float(row["shares"]) for row in buy_no),
        "buy_no_trade_usd": sum(float(row["trade_usd"]) for row in buy_no),
        "sell_yes_shares": sum(float(row["shares"]) for row in sell_yes),
        "sell_yes_trade_usd": sum(float(row["trade_usd"]) for row in sell_yes),
        "sell_no_shares": sum(float(row["shares"]) for row in sell_no),
        "sell_no_trade_usd": sum(float(row["trade_usd"]) for row in sell_no),
        "main_relative_weather_day_by_usd": _leading(core, "relative_weather_day"),
        "main_d0_bucket_by_usd": _leading(d0, "report_time_bucket"),
        "buy_yes_main_price_band_by_usd": _leading([{**row, "price_band": price_band(row["price"])} for row in buy_yes], "price_band"),
        "buy_no_main_price_band_by_usd": _leading([{**row, "price_band": price_band(row["price"])} for row in buy_no], "price_band"),
        "sell_yes_main_price_band_by_usd": _leading([{**row, "price_band": price_band(row["price"])} for row in sell_yes], "price_band"),
        "sell_no_main_price_band_by_usd": _leading([{**row, "price_band": price_band(row["price"])} for row in sell_no], "price_band"),
        "main_cumulative_shares_band_by_usd": _leading(groups, "shares_band", "cumulative_trade_usd"),
        "single_yes_temperature_event_count": combo["SINGLE_YES_TEMPERATURE"],
        "single_no_temperature_event_count": combo["SINGLE_NO_TEMPERATURE"],
        "multi_yes_event_count": sum(row["yes_temperature_bucket_count"] > 1 for row in structures),
        "multi_yes_only_event_count": combo["MULTI_YES_ONLY"],
        "multi_no_only_event_count": combo["MULTI_NO_ONLY"],
        "mixed_yes_no_event_count": combo["MIXED_YES_NO"],
        "same_bucket_both_sides_event_count": subtype["SAME_BUCKET_BOTH_SIDES"] + subtype["BOTH"],
        "cross_bucket_yes_no_event_count": subtype["CROSS_BUCKET_YES_NO"] + subtype["BOTH"],
        "adjacent_yes_event_count": sum(bool(row["adjacent_yes_pairs"]) for row in structures),
        "data_quality": quality,
        "public_record_semantics": "public fills, not original orders",
        "conclusion_labels": ["OBSERVED", "INFERRED", "NOT_SUPPORTED", "UNKNOWN"],
        "not_supported": [
            "complete PnL", "ROI", "win rate", "unfilled orders", "cancelled orders",
            "subjective forecast intent", "Negative Risk conversion economics",
        ],
        "public_data_only": True,
        "public_get_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "formal_started": False,
    }


_CITY_NAMES_ZH = {
    "beijing": "北京",
    "shanghai": "上海",
    "san-francisco": "旧金山",
    "new-york": "纽约",
    "wellington": "惠灵顿",
}

_DAY_NAMES_ZH = {
    "D-2": "天气日前两天",
    "D-1": "天气日前一天",
    "D0": "天气当天",
    "POST_EVENT": "天气日以后",
    "EARLIER_THAN_D2": "天气日前两天以前",
    "UNKNOWN": "无法判断",
}

_TIME_NAMES_ZH = {
    "D0_00_08": "当天00:00—08:00",
    "D0_08_12": "当天08:00—12:00",
    "D0_12_16": "当天12:00—16:00",
    "D0_16_24": "当天16:00—24:00",
    "D-1": "天气日前一天",
    "POST_EVENT": "天气日以后",
    "UNKNOWN": "无法判断",
}

_PRICE_NAMES_ZH = {
    "PRICE_0_10C": "0—10美分",
    "PRICE_10_30C": "10—30美分",
    "PRICE_30_70C": "30—70美分",
    "PRICE_70_90C": "70—90美分",
    "PRICE_90_100C": "90—100美分",
    "UNKNOWN": "当前没有足够成交可判断",
}

_SHARES_NAMES_ZH = {
    "SHARES_0_100": "同一价格累计不足100份",
    "SHARES_100_500": "同一价格累计100—500份",
    "SHARES_500_PLUS": "同一价格累计500份以上",
    "UNKNOWN": "当前没有足够成交可判断",
}


def _zh_city(city: str) -> str:
    return _CITY_NAMES_ZH.get(city, f"{city}（{city}）")


def _zh_day(value: str) -> str:
    return _DAY_NAMES_ZH.get(value, "无法判断")


def _zh_time(value: str) -> str:
    return _TIME_NAMES_ZH.get(value, "无法判断")


def _zh_price(value: str) -> str:
    return _PRICE_NAMES_ZH.get(value, "当前没有足够成交可判断")


def _zh_shares(value: str) -> str:
    return _SHARES_NAMES_ZH.get(value, "当前没有足够成交可判断")


def _fmt_amount(value: Any, unit: str) -> str:
    number = float(value or 0)
    return "未观察到" if number == 0 else f"{number:.2f}{unit}"


def _fmt_shares(value: Any) -> str:
    number = float(value or 0)
    return "未观察到" if number == 0 else f"{number:.2f}份"


def _data_quality_lines(summary: dict[str, Any]) -> list[str]:
    q = summary["data_quality"]
    lines: list[str] = []
    if q.get("pagination_saturation_status") == "PAGINATION_INCOMPLETE":
        lines.extend([
            "> **重要提醒：**本次数据抓取达到公开接口的分页上限，报告可能只包含部分历史成交。",
            "> 因此，当前没有观察到的交易不能解释为没有发生；买入为0不能解释为交易员没有买入，温度组合为0也可能只是缺少买入证据。本报告只描述当前抓到的公开成交记录。",
        ])
    else:
        lines.append("> **数据状态：**当前公开接口抓取未发现分页截断。")
    if q.get("api_request_failure_count", 0):
        lines.append(f"- 有{q['api_request_failure_count']}个公开接口请求失败，相关历史可能缺失。")
    if q.get("unknown_timezone_fill_count", 0) or q.get("unknown_relative_day_count", 0):
        lines.append("- 有部分成交无法映射到城市本地时间或天气相对日期。")
    if q.get("market_identity_conflict_count", 0):
        lines.append(f"- 有{q['market_identity_conflict_count']}条市场身份信息冲突，已按质量规则保留并标记。")
    if q.get("unknown_side_count", 0) or q.get("unknown_outcome_count", 0):
        lines.append("- 有部分成交缺少买卖方向或YES/NO结果，未用于对应方向的统计。")
    if q.get("trade_usd_missing_count", 0):
        lines.append(f"- 有{q['trade_usd_missing_count']}条公开记录没有原生美元金额字段，金额沿用现有分析器的备用金额口径。")
    if q.get("unparseable_market_count", 0):
        lines.append(f"- 有{q['unparseable_market_count']}条记录无法识别为目标最高温市场，未纳入成交模式统计。")
    lines.append("- 本报告只包含公开成交，不包含未成交挂单和撤单；卖出成交额不是利润。")
    return lines


def _direction_sentence(summary: dict[str, Any]) -> str:
    buys = summary["buy_fill_count"]
    sells = summary["sell_fill_count"]
    total = summary["total_public_fill_count"]
    if buys == 0 and sells:
        return f"本次在最高温市场中观察到{total}笔公开成交，当前抓到的记录全部是卖出，没有观察到买入。"
    if sells == 0 and buys:
        return f"本次在最高温市场中观察到{total}笔公开成交，当前抓到的记录全部是买入，没有观察到卖出。"
    if buys and sells:
        return f"本次在最高温市场中观察到{total}笔公开成交，买入和卖出两种方向都有记录。"
    return "本次没有解析到可用于模式判断的公开成交。"


def render_summary(summary: dict[str, Any]) -> str:
    q = summary["data_quality"]
    discovered = summary.get("discovered_cities") or summary.get("requested_cities") or []
    cities = "、".join(_zh_city(city) for city in discovered) or "未识别城市"
    buy_count = summary["buy_fill_count"]
    sell_count = summary["sell_fill_count"]
    day = _zh_day(summary["main_relative_weather_day_by_usd"])
    d0_time = _zh_time(summary["main_d0_bucket_by_usd"])
    if buy_count == 0:
        conclusion = (
            f"{_direction_sentence(summary)}由于数据完整性为{('可能不完整' if q.get('pagination_saturation_status') == 'PAGINATION_INCOMPLETE' else '公开成交证据')}，"
            "不能据此认定交易员没有买入，也不能还原完整建仓方式。"
        )
    else:
        conclusion = f"{_direction_sentence(summary)}已观察到的成交金额主要集中在{day}，其中{d0_time}是天气当天的主要时段。"
    if sell_count:
        sell_prices = []
        if summary["sell_yes_fill_count"]:
            sell_prices.append(f"卖出YES主要在{_zh_price(summary['sell_yes_main_price_band_by_usd'])}")
        if summary["sell_no_fill_count"]:
            sell_prices.append(f"卖出NO主要在{_zh_price(summary['sell_no_main_price_band_by_usd'])}")
        price_sentence = "；".join(sell_prices) + "。" if sell_prices else ""
    else:
        price_sentence = ""
    if buy_count == 0:
        temp_sentence = "当前没有买入证据，因此无法判断温度组合方式。"
    else:
        temp_sentence = "温度组合可结合后文的买入记录判断。"

    rows = [
        "# Polymarket最高温市场交易模式报告",
        "",
        "## 一、先说结论",
        "",
        *( ["> **重要提醒：本次数据抓取没有覆盖完整历史。**", ""] if q.get("pagination_saturation_status") == "PAGINATION_INCOMPLETE" else [] ),
        conclusion,
        price_sentence + temp_sentence,
        "",
        "## 二、本次研究范围",
        "",
        "| 项目 | 本次结果 |",
        "|---|---|",
        f"| 交易员钱包 | `{summary['wallet']}` |",
        f"| 研究日期 | {summary['weather_date_from']} 至 {summary['weather_date_to']} |",
        f"| 研究城市 | {cities} |",
        f"| 最高温事件 | {summary['weather_event_count']}个天气日 |",
        f"| 公开成交 | {summary['total_public_fill_count']}笔 |",
        f"| 数据完整性 | {'可能不完整' if q.get('pagination_saturation_status') == 'PAGINATION_INCOMPLETE' else '当前未发现分页截断'} |",
        "",
        "## 三、先看数据完整性",
        "",
        *_data_quality_lines(summary),
        "",
        "## 四、成交概览",
        "",
        "| 成交类型 | 笔数 | 总份数 | 成交金额 |",
        "|---|---:|---:|---:|",
        f"| 买入YES | {summary['buy_yes_fill_count'] or '未观察到'} | {_fmt_shares(summary['buy_yes_shares'])} | {_fmt_amount(summary['buy_yes_trade_usd'], '美元')} |",
        f"| 买入NO | {summary['buy_no_fill_count'] or '未观察到'} | {_fmt_shares(summary['buy_no_shares'])} | {_fmt_amount(summary['buy_no_trade_usd'], '美元')} |",
        f"| 卖出YES | {summary['sell_yes_fill_count'] or '未观察到'} | {_fmt_shares(summary['sell_yes_shares'])} | {_fmt_amount(summary['sell_yes_trade_usd'], '美元')} |",
        f"| 卖出NO | {summary['sell_no_fill_count'] or '未观察到'} | {_fmt_shares(summary['sell_no_shares'])} | {_fmt_amount(summary['sell_no_trade_usd'], '美元')} |",
        "",
        "YES表示押某个温度结果会发生；NO表示押某个温度结果不会发生。卖出金额只是公开观察到的卖出成交额，不是利润。",
        "",
        "## 五、买入方式",
        "",
    ]
    if buy_count == 0:
        rows.extend([
            "【当前无法判断】当前证据中没有观察到买入成交，因此无法判断这个交易员如何建仓、主要买哪个价格、是否分批买入，以及是否同时购买多个温度。",
            "这不等于交易员没有买入；尤其在数据分页未完整时，部分买入记录可能没有进入当前证据。",
        ])
    else:
        rows.extend([
            f"当前观察到{summary['buy_fill_count']}笔买入成交：买入YES {summary['buy_yes_fill_count']}笔，主要价格为{_zh_price(summary['buy_yes_main_price_band_by_usd'])}；买入NO {summary['buy_no_fill_count']}笔，主要价格为{_zh_price(summary['buy_no_main_price_band_by_usd'])}。",
            f"买入主要发生在{day}，温度组合统计显示：单一YES事件{summary['single_yes_temperature_event_count']}个，多YES事件{summary['multi_yes_event_count']}个，多NO排除事件{summary['multi_no_only_event_count']}个，YES/NO混合事件{summary['mixed_yes_no_event_count']}个。",
        ])
    rows.extend([
        "",
        "## 六、卖出方式",
        "",
    ])
    if sell_count:
        rows.append(
            f"当前观察到卖出YES {summary['sell_yes_fill_count']}笔、{_fmt_shares(summary['sell_yes_shares'])}，卖出成交额{_fmt_amount(summary['sell_yes_trade_usd'], '美元')}；卖出NO {summary['sell_no_fill_count']}笔、{_fmt_shares(summary['sell_no_shares'])}，卖出成交额{_fmt_amount(summary['sell_no_trade_usd'], '美元')}。"
        )
        rows.append(f"卖出金额主要集中在{day}，其中{d0_time}是主要时段。")
    else:
        rows.append("当前证据中没有观察到卖出成交，因此无法判断退出或止盈方式。")
    rows.extend([
        "",
        "## 七、交易时间集中在哪里",
        "",
        f"按公开成交金额计算，交易最集中在{day}。天气当天的主要时段是{d0_time}。",
        "",
        "## 八、主要在什么价格成交",
        "",
    ])
    if buy_count:
        rows.append(f"买入YES主要在{_zh_price(summary['buy_yes_main_price_band_by_usd'])}，买入NO主要在{_zh_price(summary['buy_no_main_price_band_by_usd'])}。")
    else:
        rows.append("由于没有观察到买入记录，无法判断买入价格偏好。")
    if sell_count:
        rows.append(price_sentence.rstrip("。") + "。")
    else:
        rows.append("当前没有观察到卖出记录，无法判断卖出价格偏好。")
    rows.extend([
        "",
        "## 九、同一价格累计成交规模",
        "",
        f"按现有统计，最常见的同价累计规模是{_zh_shares(summary['main_cumulative_shares_band_by_usd'])}。这里的累计范围是同一交易员、同一天、同一温度档、同一YES/NO方向、同一买卖方向、同一价格和同一时间桶的成交合计；它不是单笔订单大小，也不是实际投入美元。",
        "",
        "## 十、他通常买几个温度",
        "",
    ])
    if buy_count:
        rows.append(f"根据当前买入记录，可以看到单一YES事件{summary['single_yes_temperature_event_count']}个、多YES事件{summary['multi_yes_event_count']}个、多NO排除事件{summary['multi_no_only_event_count']}个，以及YES/NO混合事件{summary['mixed_yes_no_event_count']}个。")
    else:
        rows.append("当前没有观察到买入成交，所以无法判断他是只押一个温度，还是会组合多个温度。现有卖出记录不能代替建仓记录来判断温度组合。")
    rows.extend([
        "",
        "## 十一、目前可以确认的交易特点",
        "",
    ])
    if sell_count:
        rows.append(f"- 【已观察】当前公开记录中有{summary['sell_fill_count']}笔卖出成交，卖出YES和卖出NO均有记录。")
    elif buy_count:
        rows.append(f"- 【已观察】当前公开记录中有{summary['buy_fill_count']}笔买入成交。")
    if summary["main_relative_weather_day_by_usd"] != "UNKNOWN":
        rows.append(f"- 【行为概括】成交金额主要集中在{day}，其中{d0_time}是主要时间段。")
    if sell_count:
        rows.append(f"- 【行为概括】{price_sentence.rstrip('。')}。")
    rows.append(f"- 【行为概括】同一价格累计成交的主区间为{_zh_shares(summary['main_cumulative_shares_band_by_usd'])}。")
    if buy_count == 0:
        rows.append("- 【当前无法判断】由于没有观察到买入记录，无法还原完整建仓方法或温度组合方式。")
    rows.extend([
        "",
        "## 十二、目前不能下的结论",
        "",
        "- 不能据此计算完整PnL、ROI或胜率，也不能判断是否赚钱。",
        "- 不能看到未成交挂单和撤单，不能知道每笔公开成交背后的完整订单。",
        "- 不能从公开成交直接知道交易员的主观预测意图。",
        "- 没有观察到买入时，不能判断建仓策略；数据分页不完整时，也不能把本报告当成完整历史。",
        "- 卖出成交额不等于利润。",
        "",
        "这份报告适合用来观察已经抓到的公开成交习惯，不适合单独用来判断交易员是否盈利或复制其完整策略。",
        "",
    ])
    return "\n".join(rows)


class PublicGetClient:
    """Official Polymarket unauthenticated GET-only client with request evidence."""

    def __init__(self, raw_root: Path, attempts: int = 4) -> None:
        self.raw_root = raw_root
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self.attempts = attempts
        self.requests: list[dict[str, Any]] = []
        self.counter = 0

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        global NETWORK_CALL_COUNT
        if os.environ.get(NO_NETWORK_ENV) == "1":
            raise RuntimeError("NETWORK_DISABLED_BY_POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK")
        if not url.startswith((f"{DATA_API}/", f"{GAMMA_API}/")):
            raise ValueError("only official Polymarket public GET endpoints are allowed")
        encoded = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{encoded}" if encoded else url
        last_error = ""
        for retry in range(self.attempts):
            received = datetime.now(timezone.utc).isoformat()
            try:
                request = urllib.request.Request(full_url, method="GET", headers={"User-Agent": "polymarket-highest-temperature-public-research/1.0"})
                NETWORK_CALL_COUNT += 1
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                payload = json.loads(raw)
                self.counter += 1
                relative = Path("requests") / f"{self.counter:05d}.json"
                path = self.raw_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                count = len(payload) if isinstance(payload, list) else 1
                self.requests.append({
                    "method": "GET", "url": url, "params": dict(params),
                    "requested_at_utc": received, "record_count": count,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "relative_path": relative.as_posix(), "success": True,
                    "retries": retry,
                })
                return payload
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
                last_error = repr(exc)
                if retry + 1 < self.attempts:
                    time.sleep(0.5 * (2 ** retry))
        self.requests.append({
            "method": "GET", "url": url, "params": dict(params),
            "requested_at_utc": datetime.now(timezone.utc).isoformat(),
            "record_count": 0, "success": False, "retries": self.attempts - 1,
            "error": last_error,
        })
        raise RuntimeError(f"public GET failed after retries: {full_url}: {last_error}")


def fetch_activity_window(
    client: PublicGetClient,
    wallet: str,
    window: Window,
    *,
    limit: int = 500,
    offset_cap: int = 10_000,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > 40:
        raise RuntimeError("PAGINATION_INCOMPLETE:excessive_window_splitting")
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.get_json(f"{DATA_API}/activity", {
            "user": wallet, "start": window.start, "end": window.end,
            "limit": limit, "offset": offset, "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
        })
        if not isinstance(page, list):
            raise RuntimeError("activity response was not a list")
        rows.extend(page)
        if len(page) < limit:
            return rows
        next_offset = offset + limit
        if next_offset >= offset_cap:
            left, right = split_window(window)
            return (
                fetch_activity_window(client, wallet, left, limit=limit, offset_cap=offset_cap, depth=depth + 1)
                + fetch_activity_window(client, wallet, right, limit=limit, offset_cap=offset_cap, depth=depth + 1)
            )
        offset = next_offset


def fetch_all_activity(client: PublicGetClient, wallet: str, start: int, end: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in thirty_day_windows(start, end):
        rows.extend(fetch_activity_window(client, wallet, window))
    deduped, _ = deduplicate_records(rows)
    return sorted(deduped, key=lambda row: (epoch_seconds(row.get("timestamp")), stable_trade_key(row)))


def fetch_trades_by_side(
    client: PublicGetClient, wallet: str, *, limit: int = 10_000, offset_cap: int = 10_000
) -> tuple[list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    saturated = False
    for side in ("BUY", "SELL"):
        offset = 0
        while offset <= offset_cap:
            page = client.get_json(f"{DATA_API}/trades", {
                "user": wallet, "limit": limit, "offset": offset,
                "takerOnly": "false", "side": side,
            })
            if not isinstance(page, list):
                raise RuntimeError("trades response was not a list")
            result.extend(page)
            if len(page) < limit:
                break
            offset += limit
            if offset > offset_cap:
                saturated = True
                break
    deduped, _ = deduplicate_records(result)
    return deduped, saturated


def _collection_bounds(date_from: date, date_to: date) -> tuple[int, int]:
    start = datetime.combine(date_from - timedelta(days=3), datetime_time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_to + timedelta(days=3), datetime_time.max, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def refresh_wallet_evidence(
    wallet: str,
    date_from: date,
    date_to: date,
    root: Path,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    start, end = _collection_bounds(date_from, date_to)
    client = PublicGetClient(root)
    activity = fetch_all_activity(client, wallet, start, end)
    trades, saturated = fetch_trades_by_side(client, wallet)
    trade_rows = [row for row in trades if start <= epoch_seconds(row.get("timestamp")) <= end]
    payloads = {"activity": activity, "trades": trade_rows}
    aggregates: dict[str, Any] = {}
    for name, payload in payloads.items():
        relative = Path(f"{name}.json")
        write_json(root / relative, payload)
        aggregates[name] = {
            "relative_path": relative.as_posix(), "record_count": len(payload),
            "sha256": sha256_file(root / relative),
        }
    manifest = {
        "schema_version": EVIDENCE_SCHEMA,
        "wallet": wallet,
        "weather_date_from": date_from.isoformat(),
        "weather_date_to": date_to.isoformat(),
        "collection_start_utc": iso_utc(start),
        "collection_end_utc": iso_utc(end),
        "public_data_only": True, "public_get_only": True,
        "account_connection": False, "signing": False, "real_order": False,
        "all_requests_successful": all(row["success"] for row in client.requests),
        "pagination_saturation_status": "PAGINATION_INCOMPLETE" if saturated else "COMPLETE",
        "requests": client.requests, "aggregates": aggregates,
    }
    write_json(root / "manifest.json", manifest)
    return manifest, payloads


def _safe_relative_file(base: Path, value: Any) -> Path:
    relative = Path(str(value or ""))
    if not value or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("EVIDENCE_PATH_NOT_PORTABLE")
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise RuntimeError("EVIDENCE_PATH_TRAVERSAL_REJECTED") from exc
    return resolved


def load_saved_evidence(
    manifest_path: Path,
    *,
    wallets: list[str],
    date_from: date,
    date_to: date,
) -> dict[str, tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]]:
    path = manifest_path.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("public_data_only") is not True or manifest.get("public_get_only") is not True:
        raise RuntimeError("EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
    if any(manifest.get(flag) is True for flag in ("account_connection", "signing", "real_order")):
        raise RuntimeError("EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
    if manifest.get("schema_version") == LEGACY_HUSKY_EVIDENCE_SCHEMA:
        if wallets != [HUSKY_WALLET]:
            raise RuntimeError(
                "EVIDENCE_WALLET_MISMATCH: the bundled legacy manifest belongs only to "
                f"{HUSKY_WALLET}; use --refresh-public-data for a new wallet or provide "
                "a saved manifest that belongs to the requested wallet"
            )
        result: dict[str, list[dict[str, Any]]] = {}
        for name in ("activity", "trades"):
            meta = manifest["aggregates"][name]
            source = _safe_relative_file(path.parent, meta.get("relative_path"))
            if not source.is_file() or sha256_file(source) != meta.get("sha256"):
                raise RuntimeError(f"EVIDENCE_SHA256_MISMATCH:{name}")
            payload = json.loads(source.read_text(encoding="utf-8"))
            if len(payload) != meta.get("record_count"):
                raise RuntimeError(f"EVIDENCE_RECORD_COUNT_MISMATCH:{name}")
            if any(_source_wallet(row) != HUSKY_WALLET for row in payload):
                raise RuntimeError(f"EVIDENCE_WALLET_MISMATCH:{name}")
            result[name] = payload
        request_times = sorted(
            str(row.get("received_at_utc"))
            for row in manifest.get("source_requests", [])
            if row.get("received_at_utc")
        )
        legacy = {
            **manifest,
            "weather_date_from": date_from.isoformat(),
            "weather_date_to": date_to.isoformat(),
            "collection_start_utc": request_times[0] if request_times else manifest.get("analysis_started_at_utc"),
            "collection_end_utc": request_times[-1] if request_times else manifest.get("analysis_cutoff_utc"),
            "pagination_saturation_status": "COMPLETE",
        }
        return {HUSKY_WALLET: (legacy, result)}
    manifests: list[tuple[Path, dict[str, Any]]]
    if manifest.get("schema_version") == EVIDENCE_SCHEMA:
        manifests = [(path, manifest)]
    elif manifest.get("schema_version") == f"{EVIDENCE_SCHEMA}_multi_wallet":
        manifests = []
        for relative in manifest.get("wallet_manifests", []):
            child = _safe_relative_file(path.parent, relative)
            manifests.append((child, json.loads(child.read_text(encoding="utf-8"))))
    else:
        raise RuntimeError("UNSUPPORTED_EVIDENCE_MANIFEST_SCHEMA")
    loaded: dict[str, tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]] = {}
    for child_path, child in manifests:
        if child.get("public_data_only") is not True or child.get("public_get_only") is not True:
            raise RuntimeError("EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
        if any(child.get(flag) is True for flag in ("account_connection", "signing", "real_order")):
            raise RuntimeError("EVIDENCE_PUBLIC_SAFETY_FLAGS_MISMATCH")
        wallet = str(child.get("wallet") or "").lower()
        if wallet not in wallets:
            raise RuntimeError(
                "EVIDENCE_WALLET_MISMATCH: the saved manifest wallet set does not match "
                "the requested wallet set; use --refresh-public-data for new wallets or "
                "provide the matching saved manifest"
            )
        if child.get("weather_date_from") != date_from.isoformat() or child.get("weather_date_to") != date_to.isoformat():
            raise RuntimeError("EVIDENCE_ANALYSIS_RANGE_MISMATCH")
        payloads = {}
        for name in ("activity", "trades"):
            meta = child.get("aggregates", {}).get(name)
            if not meta:
                raise RuntimeError(f"EVIDENCE_AGGREGATE_MISSING:{name}")
            source = _safe_relative_file(child_path.parent, meta.get("relative_path"))
            if sha256_file(source) != meta.get("sha256"):
                raise RuntimeError(f"EVIDENCE_SHA256_MISMATCH:{name}")
            payload = json.loads(source.read_text(encoding="utf-8"))
            if len(payload) != meta.get("record_count"):
                raise RuntimeError(f"EVIDENCE_RECORD_COUNT_MISMATCH:{name}")
            if any(_source_wallet(row) != wallet for row in payload):
                raise RuntimeError(f"EVIDENCE_WALLET_MISMATCH:{name}")
            payloads[name] = payload
        loaded[wallet] = (child, payloads)
    if set(loaded) != set(wallets):
        raise RuntimeError("EVIDENCE_WALLET_SET_MISMATCH")
    return loaded


def _combined_source_rows(trades: list[dict[str, Any]], activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in trades:
        rows.append({**row, "_source_types": "trades"})
    # Generalized from activity_join_key in the protected Beijing study: size
    # may differ between the two public endpoints, so activity is a fallback
    # only when the otherwise-identical public trade identity is absent.
    trade_keys = {stable_trade_key(row)[:-1] for row in trades}
    for row in activity:
        if str(row.get("type") or "TRADE").upper() != "TRADE":
            continue
        if stable_trade_key(row)[:-1] not in trade_keys:
            rows.append({**row, "_source_types": "activity"})
    return rows


def _quality_payload(
    requested_wallet_count: int,
    requested_city_count: int,
    fills: list[dict[str, Any]],
    raw_activity_count: int,
    raw_trade_count: int,
    discovery: list[dict[str, Any]],
    counters: Counter[str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    requests = manifest.get("requests") or manifest.get("source_requests") or []
    return {
        "requested_wallet_count": requested_wallet_count,
        "valid_wallet_count": requested_wallet_count,
        "requested_city_count": requested_city_count,
        "discovered_city_count": len({row["canonical_city"] for row in fills}),
        "weather_event_count": len({row["event_key"] for row in fills}),
        "market_count": len({row["condition_id"] for row in fills}),
        "raw_activity_count": raw_activity_count,
        "raw_trade_count": raw_trade_count,
        "normalized_fill_count": len(fills) + counters["duplicate_fill_count"],
        "deduplicated_fill_count": len(fills),
        "duplicate_fill_count": counters["duplicate_fill_count"],
        "unparseable_market_count": counters["unparseable_market_count"],
        "unparseable_weather_date_count": counters["unparseable_weather_date_count"],
        "unknown_timezone_city_count": len({row["canonical_city"] for row in fills if not row["market_timezone"]}),
        "unknown_relative_day_count": counters["unknown_relative_day_count"],
        "earlier_than_d2_count": counters["earlier_than_d2_count"],
        "post_event_fill_count": counters["post_event_fill_count"],
        "unknown_outcome_count": counters["unknown_outcome_count"],
        "unknown_side_count": counters["unknown_side_count"],
        "price_out_of_range_count": counters["price_out_of_range_count"],
        "shares_invalid_count": counters["shares_invalid_count"],
        "trade_usd_missing_count": counters["trade_usd_missing_count"],
        "market_identity_conflict_count": counters["market_identity_conflict_count"],
        "timestamp_invalid_count": counters["timestamp_invalid_count"],
        "wallet_mismatch_count": counters["wallet_mismatch_count"],
        "unknown_timezone_fill_count": counters["unknown_timezone_fill_count"],
        "api_request_count": len(requests),
        "api_request_failure_count": sum(row.get("success") is not True for row in requests),
        "pagination_saturation_status": manifest.get("pagination_saturation_status", "COMPLETE"),
        "data_completeness_status": (
            "PAGINATION_INCOMPLETE"
            if manifest.get("pagination_saturation_status") == "PAGINATION_INCOMPLETE"
            else "PUBLIC_FILL_EVIDENCE_ONLY"
        ),
        "market_discovery_row_count": len(discovery),
    }


def _write_wallet_outputs(
    root: Path,
    summary: dict[str, Any],
    fills: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    structures: list[dict[str, Any]],
    discovery: list[dict[str, Any]],
    quality: dict[str, Any],
    source_manifest: dict[str, Any],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "summary.json", summary)
    (root / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    write_csv(root / "all_fills.csv", fills, FILL_FIELDS)
    write_csv(root / "same_price_cumulative_groups.csv", groups)
    dimensions = (
        "wallet", "canonical_city", "side", "outcome", "price_band",
        "relative_weather_day", "report_time_bucket", "shares_band",
    )
    cross = _aggregate_distribution(fills, groups, dimensions)
    write_csv(root / "price_time_cumulative_shares_distribution.csv", cross)
    for side, outcome, filename in (
        ("BUY", "YES", "buy_yes_distribution.csv"),
        ("BUY", "NO", "buy_no_distribution.csv"),
        ("SELL", "YES", "sell_yes_distribution.csv"),
        ("SELL", "NO", "sell_no_distribution.csv"),
    ):
        subset = [row for row in fills if row["side"] == side and row["outcome"] == outcome]
        subset_groups = [row for row in groups if row["side"] == side and row["outcome"] == outcome]
        write_csv(root / filename, _aggregate_distribution(subset, subset_groups, ("wallet", "canonical_city", "price_band")))
    write_csv(root / "event_temperature_structure.csv", structures)
    write_csv(root / "city_summary.csv", _allocation_rows(fills, groups))
    write_csv(root / "market_discovery.csv", discovery)
    write_csv(root / "data_quality.csv", [{"metric": key, "value": value} for key, value in quality.items()])
    write_json(root / "source_manifest.json", source_manifest)


def analyze(
    wallets: Iterable[str],
    date_from: str,
    date_to: str,
    cities: Iterable[str] | None,
    output_root: Path,
    *,
    refresh_public_data: bool = False,
    saved_public_evidence_manifest: Path | None = None,
    city_timezone_overrides: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized_wallets = normalize_wallets(wallets)
    requested_cities = normalize_cities(cities)
    start_date, end_date = parse_date_range(date_from, date_to)
    if refresh_public_data == bool(saved_public_evidence_manifest):
        raise ValueError("exactly one evidence source is required")
    timezone_registry = load_timezone_registry(overrides=city_timezone_overrides)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    loaded: dict[str, tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]]
    if refresh_public_data:
        loaded = {}
        wallet_manifest_paths = []
        for wallet in normalized_wallets:
            evidence_root = output_root / "_public_evidence" / wallet
            loaded[wallet] = refresh_wallet_evidence(wallet, start_date, end_date, evidence_root)
            wallet_manifest_paths.append((Path(wallet) / "manifest.json").as_posix())
        if len(normalized_wallets) > 1:
            write_json(output_root / "_public_evidence/manifest.json", {
                "schema_version": f"{EVIDENCE_SCHEMA}_multi_wallet",
                "public_data_only": True, "public_get_only": True,
                "wallet_manifests": wallet_manifest_paths,
            })
    else:
        loaded = load_saved_evidence(
            Path(saved_public_evidence_manifest), wallets=normalized_wallets,
            date_from=start_date, date_to=end_date,
        )
    summaries: list[dict[str, Any]] = []
    for wallet in normalized_wallets:
        manifest, payloads = loaded[wallet]
        combined = _combined_source_rows(payloads["trades"], payloads["activity"])
        fills, discovery, counters = normalize_fill_rows(
            wallet, combined, activity_rows=payloads["activity"],
            date_from=start_date, date_to=end_date, cities=requested_cities,
            timezone_registry=timezone_registry,
        )
        groups = build_same_price_cumulative_groups(fills)
        structures = build_event_temperature_structures(fills)
        quality = _quality_payload(
            len(normalized_wallets), len(requested_cities), fills,
            len(payloads["activity"]), len(payloads["trades"]), discovery,
            counters, manifest,
        )
        collection_start = str(manifest.get("collection_start_utc") or manifest.get("analysis_started_at_utc") or "UNKNOWN")
        collection_end = str(manifest.get("collection_end_utc") or manifest.get("analysis_cutoff_utc") or "UNKNOWN")
        summary = _summary(
            wallet, fills, groups, structures, quality, start_date, end_date,
            requested_cities, collection_start, collection_end,
        )
        summaries.append(summary)
        source_manifest = {
            "schema_version": EVIDENCE_SCHEMA,
            "wallet": wallet,
            "weather_date_from": start_date.isoformat(),
            "weather_date_to": end_date.isoformat(),
            "public_data_only": True, "public_get_only": True,
            "account_connection": False, "signing": False, "real_order": False,
            "evidence_source": "saved_manifest" if saved_public_evidence_manifest else "refreshed_public_get",
            "source_manifest_sha256": (
                sha256_file(Path(saved_public_evidence_manifest).resolve())
                if saved_public_evidence_manifest else sha256_file(output_root / "_public_evidence" / wallet / "manifest.json")
            ),
            "raw_evidence_copied_to_wallet_output": False,
            "api_request_count": quality["api_request_count"],
            "api_request_failure_count": quality["api_request_failure_count"],
            "pagination_saturation_status": quality["pagination_saturation_status"],
        }
        _write_wallet_outputs(
            output_root / wallet, summary, fills, groups, structures, discovery,
            quality, source_manifest,
        )
    comparison = [{
        "wallet": row["wallet"],
        "weather_event_count": row["weather_event_count"],
        "total_public_fill_count": row["total_public_fill_count"],
        "buy_yes_fill_count": row["buy_yes_fill_count"],
        "buy_no_fill_count": row["buy_no_fill_count"],
        "sell_yes_fill_count": row["sell_yes_fill_count"],
        "sell_no_fill_count": row["sell_no_fill_count"],
        "buy_yes_trade_usd": row["buy_yes_trade_usd"],
        "buy_no_trade_usd": row["buy_no_trade_usd"],
        "main_relative_weather_day_by_usd": row["main_relative_weather_day_by_usd"],
        "main_d0_bucket_by_usd": row["main_d0_bucket_by_usd"],
        "multi_yes_event_count": row["multi_yes_event_count"],
        "mixed_yes_no_event_count": row["mixed_yes_no_event_count"],
    } for row in summaries]
    write_csv(output_root / "trader_comparison.csv", comparison)
    comparison_lines = [
        "# Trader comparison", "",
        "Public fills are not original orders; this comparison contains no PnL, ROI, or intent claims.", "",
        "| Wallet | Events | Fills | BUY YES | BUY NO | SELL YES | SELL NO | Main day |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    comparison_lines.extend(
        f"| {row['wallet']} | {row['weather_event_count']} | {row['total_public_fill_count']} | {row['buy_yes_fill_count']} | {row['buy_no_fill_count']} | {row['sell_yes_fill_count']} | {row['sell_no_fill_count']} | {row['main_relative_weather_day_by_usd']} |"
        for row in comparison
    )
    (output_root / "trader_comparison.md").write_text("\n".join(comparison_lines) + "\n", encoding="utf-8")
    run_manifest = {
        "schema_version": SCHEMA_VERSION,
        "wallets": normalized_wallets,
        "weather_date_from": start_date.isoformat(),
        "weather_date_to": end_date.isoformat(),
        "cities": requested_cities,
        "all_cities_default": not requested_cities,
        "public_data_only": True, "public_get_only": True,
        "account_connection": False, "signing": False, "real_order": False,
        "formal_started": False, "network_call_count": NETWORK_CALL_COUNT,
    }
    write_json(output_root / "run_manifest.json", run_manifest)
    return {"run_manifest": run_manifest, "summaries": summaries, "comparison": comparison}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("analyze")
    command.add_argument("--wallet", action="append", required=True)
    command.add_argument("--date-from", required=True)
    command.add_argument("--date-to", required=True)
    command.add_argument("--city", action="append", default=[])
    command.add_argument("--city-timezone", action="append", default=[])
    command.add_argument("--output-root", required=True)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--refresh-public-data", action="store_true")
    source.add_argument("--saved-public-evidence-manifest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    analyze(
        args.wallet, args.date_from, args.date_to, args.city,
        Path(args.output_root), refresh_public_data=args.refresh_public_data,
        saved_public_evidence_manifest=(
            Path(args.saved_public_evidence_manifest)
            if args.saved_public_evidence_manifest else None
        ),
        city_timezone_overrides=args.city_timezone,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
