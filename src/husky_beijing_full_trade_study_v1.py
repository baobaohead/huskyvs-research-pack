#!/usr/bin/env python3
"""Analyze all observable Husky Beijing highest-temperature public fills.

This module is intentionally read-only with respect to Polymarket.  It only
uses unauthenticated public HTTP GET endpoints.  Public fills are not original
orders: one order may create several fills, while cancelled or unfilled orders
are normally absent from the public evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from zoneinfo import ZoneInfo


HUSKY_WALLET = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CST = ZoneInfo("Asia/Shanghai")
PUBLIC_DATA_ONLY = True
PUBLIC_GET_ONLY = True
ACCOUNT_CONNECTION = False
SIGNING = False
REAL_ORDER = False
FORMAL_STARTED = False
STATION_LABEL = "BEIJING_STATION_UNCONFIRMED"
API_HISTORY_START = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
RAW_EVIDENCE_ROOT = Path("/tmp/husky_beijing_full_trade_study_v1/raw_public_evidence")
ADD_THRESHOLD = Decimal("0.01")
AVERAGE_COST_NEAR = Decimal("0.01")
SIMULTANEOUS_SECONDS = 5 * 60
EPSILON = 1e-6
PNL_EXACT_TOLERANCE = 1e-9
PNL_CENT_TOLERANCE = 0.01
SELL_PNL_MATERIAL_DIFFERENCE_FRACTION = 0.10
POSITION_STATUSES = {
    "ACTIVE_OPEN_CONFIRMED",
    "RESOLVED_REDEEMABLE_UNREDEEMED",
    "PAST_ENDDATE_STATUS_UNKNOWN",
    "CLOSED_POSITION_CONFIRMED",
    "POSITION_STATUS_UNKNOWN",
}
CHECKPOINTS = {
    "D1_1200": (-1, 12, 0),
    "D1_1500": (-1, 15, 0),
    "D1_1800": (-1, 18, 0),
    "D0_0800": (0, 8, 0),
    "D0_1000": (0, 10, 0),
    "D0_1100": (0, 11, 0),
    "D0_1200": (0, 12, 0),
    "D0_1300": (0, 13, 0),
    "D0_1400": (0, 14, 0),
    "D0_1500": (0, 15, 0),
    "D0_1600": (0, 16, 0),
}
THRESHOLDS = (0.10, 0.25, 0.50, 0.75, 0.90)
FILL_FIELDS = [
    "event_key", "weather_date", "city", "weather_metric", "station_status",
    "condition_id", "event_slug", "slug", "asset", "outcome",
    "temperature_bucket", "bucket_kind", "bucket_low", "bucket_high", "unit",
    "timestamp_epoch", "public_trade_time_utc", "public_trade_time_cst",
    "relative_phase", "half_hour_bin", "side", "price", "shares", "trade_usd",
    "trade_usd_source", "transaction_hash", "source_repository_trade",
    "source_repository_activity", "source_current_public_api",
    "source_row_number", "activity_match_status", "previous_same_bucket_buy_price",
    "price_change_vs_previous_buy", "price_add_class", "pretrade_average_cost",
    "price_change_vs_average_cost", "average_cost_add_class",
]


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def canonical_decimal(value: Any) -> str:
    number = decimal(value)
    return format(number.normalize(), "f") if number is not None else ""


def epoch_seconds(value: Any) -> int:
    number = finite(value)
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


def iso_cst(value: Any) -> str:
    return datetime.fromtimestamp(epoch_seconds(value), timezone.utc).astimezone(CST).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_end_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_position_row(
    row: dict[str, Any],
    analysis_cutoff_utc: str,
    *,
    authoritative_closed: bool = False,
) -> str:
    """Classify one public position row without treating redeemable as open."""
    if authoritative_closed:
        return "CLOSED_POSITION_CONFIRMED"
    end_date = parse_end_date(row.get("endDate"))
    cutoff = parse_iso(analysis_cutoff_utc)
    explicitly_open = row.get("active") is True and row.get("closed") is not True
    if explicitly_open or (end_date is not None and end_date > cutoff):
        return "ACTIVE_OPEN_CONFIRMED"
    if end_date is None:
        return "POSITION_STATUS_UNKNOWN"
    if end_date <= cutoff and row.get("redeemable") is True:
        return "RESOLVED_REDEEMABLE_UNREDEEMED"
    if end_date <= cutoff and row.get("redeemable") is False:
        return "PAST_ENDDATE_STATUS_UNKNOWN"
    return "POSITION_STATUS_UNKNOWN"


def position_snapshot_pnl(row: dict[str, Any], formula: str) -> float | None:
    values = {
        "cashPnl": finite(row.get("cashPnl")),
        "realizedPnl": finite(row.get("realizedPnl")),
        "currentValue": finite(row.get("currentValue")),
        "initialValue": finite(row.get("initialValue")),
    }
    if formula == "A_cashPnl":
        return values["cashPnl"]
    if formula == "B_realizedPnl":
        return values["realizedPnl"]
    if formula == "C_cashPnl_plus_realizedPnl":
        if values["cashPnl"] is None or values["realizedPnl"] is None:
            return None
        return values["cashPnl"] + values["realizedPnl"]
    if formula == "D_currentValue_minus_initialValue_plus_realizedPnl":
        if any(values[key] is None for key in ("currentValue", "initialValue", "realizedPnl")):
            return None
        return values["currentValue"] - values["initialValue"] + values["realizedPnl"]
    raise ValueError(f"unknown PnL formula: {formula}")


def reconcile_resolved_pnl_formulas(
    resolved_rows: Iterable[dict[str, Any]],
    authoritative_by_asset: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    formulas = (
        "A_cashPnl",
        "B_realizedPnl",
        "C_cashPnl_plus_realizedPnl",
        "D_currentValue_minus_initialValue_plus_realizedPnl",
    )
    rows = list(resolved_rows)
    comparisons: dict[str, dict[str, Any]] = {}
    for formula in formulas:
        errors: list[float] = []
        for row in rows:
            authoritative = authoritative_by_asset.get(str(row.get("asset") or ""))
            expected = finite(authoritative.get("realizedPnl")) if authoritative else None
            observed = position_snapshot_pnl(row, formula)
            if expected is not None and observed is not None:
                errors.append(abs(observed - expected))
        comparisons[formula] = {
            "comparable_asset_count": len(errors),
            "exact_match_rate": (
                sum(error <= PNL_EXACT_TOLERANCE for error in errors) / len(errors)
                if errors else None
            ),
            "within_0_01_rate": (
                sum(error < PNL_CENT_TOLERANCE for error in errors) / len(errors)
                if errors else None
            ),
            "max_absolute_error": max(errors) if errors else None,
        }
    comparable = max(
        (metrics["comparable_asset_count"] for metrics in comparisons.values()),
        default=0,
    )
    if comparable:
        most_stable = min(
            formulas,
            key=lambda name: (
                comparisons[name]["max_absolute_error"],
                -comparisons[name]["exact_match_rate"],
                name,
            ),
        )
    else:
        most_stable = "UNDETERMINED_NO_AUTHORITATIVE_OVERLAP"
    selected = comparisons.get(most_stable)
    validated = bool(
        selected
        and selected["comparable_asset_count"] > 0
        and selected["exact_match_rate"] == 1.0
        and selected["within_0_01_rate"] == 1.0
        and selected["max_absolute_error"] < PNL_CENT_TOLERANCE
    )
    return {
        "formulas": comparisons,
        "comparable_asset_count": comparable,
        "most_stable_formula": most_stable,
        "validation_result": (
            "RESOLVED_UNREDEEMED_PNL_VALIDATED"
            if validated else "RESOLVED_UNREDEEMED_PNL_NOT_VALIDATED"
        ),
        "snapshot_formula": "C_cashPnl_plus_realizedPnl",
        "snapshot_formula_status": (
            "VALIDATED" if validated and most_stable == "C_cashPnl_plus_realizedPnl"
            else "UNVALIDATED_SNAPSHOT_ONLY"
        ),
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return value


def stable_json(value: Any) -> bytes:
    return json.dumps(
        json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str] | None = None) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted({key for row in materialized for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(json_safe(materialized))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def iter_csv(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            yield row_number, row


def stable_trade_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("timestamp") or row.get("timestamp_epoch") or ""),
        str(row.get("transactionHash") or row.get("transaction_hash") or "").lower(),
        str(row.get("conditionId") or row.get("condition_id") or "").lower(),
        str(row.get("asset") or ""),
        str(row.get("side") or "").upper(),
        canonical_decimal(row.get("price")),
        canonical_decimal(row.get("size") if "size" in row else row.get("shares")),
    )


def activity_join_key(row: dict[str, Any]) -> tuple[str, ...]:
    return stable_trade_key(row)[:-1]


def deduplicate_records(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    duplicates = 0
    for source in rows:
        row = dict(source)
        key = stable_trade_key(row)
        if key in by_key:
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
            target["source_row_number"] = "|".join(filter(None, dict.fromkeys([existing, incoming])))
        else:
            by_key[key] = row
    return list(by_key.values()), duplicates


BEIJING_EVENT_RE = re.compile(
    r"^highest-temperature-in-beijing-on-([a-z]+)-(\d{1,2})-(\d{4})$", re.I
)
BEIJING_MARKET_RE = re.compile(
    r"^highest-temperature-in-beijing-on-([a-z]+)-(\d{1,2})-(\d{4})-", re.I
)
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


def parse_weather_date(row: dict[str, Any]) -> str | None:
    explicit = str(row.get("weather_date") or row.get("weather_date_local") or "")
    if explicit:
        try:
            return date.fromisoformat(explicit[:10]).isoformat()
        except ValueError:
            pass
    candidates = [str(row.get("eventSlug") or row.get("event_slug") or ""), str(row.get("slug") or "")]
    for candidate in candidates:
        match = BEIJING_EVENT_RE.match(candidate) or BEIJING_MARKET_RE.match(candidate)
        if match:
            month = MONTHS.get(match.group(1).lower())
            if month:
                return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    end_date = str(row.get("endDate") or "")
    if end_date:
        try:
            end_day = parse_iso(end_date).date()
            return (end_day - timedelta(days=1)).isoformat()
        except ValueError:
            pass
    return None


def is_beijing_highest_market(row: dict[str, Any], require_wallet: bool = True) -> bool:
    wallet = str(row.get("proxyWallet") or row.get("proxy_wallet") or "").lower()
    if require_wallet and wallet != HUSKY_WALLET:
        return False
    city = str(row.get("city") or "").strip().lower()
    if city and city != "beijing":
        return False
    metric = str(row.get("weather_metric") or "").strip().lower()
    if metric and metric not in {"high", "highest", "tmax"}:
        return False
    title = str(row.get("title") or "").lower()
    event_slug = str(row.get("eventSlug") or row.get("event_slug") or "").lower()
    slug = str(row.get("slug") or "").lower()
    slug_ok = bool(BEIJING_EVENT_RE.match(event_slug)) and (
        slug == event_slug or bool(BEIJING_MARKET_RE.match(slug))
    )
    title_ok = "highest temperature in beijing" in title
    return slug_ok and title_ok and parse_weather_date(row) is not None


def parse_bucket(row: dict[str, Any]) -> dict[str, Any]:
    label = str(row.get("bucket_label") or row.get("outcome") or "").strip()
    title = str(row.get("title") or "")
    slug = str(row.get("slug") or "")
    if label.lower() in {"yes", "no", ""}:
        match = re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°\s*([CF])(?:\s+or\s+(below|higher))?", title, re.I)
        if match:
            number = float(match.group(1))
            unit = match.group(2).upper()
            tail = (match.group(3) or "").lower()
            kind = "below" if tail == "below" else ("above" if tail == "higher" else "exact")
            label = f"{number:g}°{unit}" + (f" or {tail}" if tail else "")
            return {
                "temperature_bucket": label,
                "bucket_kind": kind,
                "bucket_low": None if kind == "below" else number,
                "bucket_high": None if kind == "above" else number,
                "unit": unit,
            }
        slug_match = re.search(r"-(-?\d+(?:\.\d+)?)([cf])or(below|higher)$", slug, re.I)
        if slug_match:
            number = float(slug_match.group(1))
            unit = slug_match.group(2).upper()
            kind = "below" if slug_match.group(3).lower() == "below" else "above"
            return {
                "temperature_bucket": f"{number:g}°{unit} or {slug_match.group(3).lower()}",
                "bucket_kind": kind,
                "bucket_low": None if kind == "below" else number,
                "bucket_high": None if kind == "above" else number,
                "unit": unit,
            }
        return {
            "temperature_bucket": label or "UNKNOWN",
            "bucket_kind": "UNKNOWN",
            "bucket_low": None,
            "bucket_high": None,
            "unit": str(row.get("unit") or "C"),
        }
    kind = str(row.get("bucket_kind") or "exact")
    low = finite(row.get("bucket_low"))
    high = finite(row.get("bucket_high"))
    return {
        "temperature_bucket": label,
        "bucket_kind": kind,
        "bucket_low": low,
        "bucket_high": high,
        "unit": str(row.get("unit") or "C"),
    }


def event_key(row: dict[str, Any]) -> str:
    return f"{parse_weather_date(row)}__beijing__high"


def relative_phase(timestamp: Any, weather_date: str) -> str:
    local = datetime.fromtimestamp(epoch_seconds(timestamp), timezone.utc).astimezone(CST)
    target = date.fromisoformat(weather_date)
    delta = (local.date() - target).days
    minute = local.hour * 60 + local.minute
    if delta <= -2:
        return "D-2_OR_EARLIER"
    if delta == -1:
        if minute < 12 * 60:
            return "D-1_0000_1200"
        if minute < 15 * 60:
            return "D-1_1200_1500"
        if minute < 18 * 60:
            return "D-1_1500_1800"
        return "D-1_1800_2400"
    if delta == 0:
        boundaries = (
            (8, "D0_0000_0800"), (10, "D0_0800_1000"), (11, "D0_1000_1100"),
            (12, "D0_1100_1200"), (13, "D0_1200_1300"), (14, "D0_1300_1400"),
            (15, "D0_1400_1500"), (16, "D0_1500_1600"), (18, "D0_1600_1800"),
            (24, "D0_1800_2400"),
        )
        for hour, label in boundaries:
            if minute < hour * 60:
                return label
    return "D+1_OR_LATER"


def half_hour_bin(timestamp: Any) -> str:
    local = datetime.fromtimestamp(epoch_seconds(timestamp), timezone.utc).astimezone(CST)
    start = local.hour * 60 + (30 if local.minute >= 30 else 0)
    end = start + 30
    return f"{start // 60:02d}:{start % 60:02d}—{end // 60:02d}:{end % 60:02d}"


def classify_price_add(previous: Any, current: Any) -> str:
    prev, cur = decimal(previous), decimal(current)
    if prev is None or cur is None:
        return "UNKNOWN"
    change = cur - prev
    if change >= ADD_THRESHOLD:
        return "PRICE_UP_ADD"
    if change <= -ADD_THRESHOLD:
        return "PRICE_DOWN_ADD"
    return "PRICE_FLAT_ADD"


def classify_average_cost_add(average: Any, current: Any) -> str:
    avg, cur = decimal(average), decimal(current)
    if avg is None or cur is None:
        return "UNKNOWN"
    change = cur - avg
    if change >= AVERAGE_COST_NEAR:
        return "ABOVE_AVERAGE_COST_ADD"
    if change <= -AVERAGE_COST_NEAR:
        return "BELOW_AVERAGE_COST_ADD"
    return "NEAR_AVERAGE_COST_ADD"


def buckets_adjacent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("unit") != b.get("unit") or a.get("bucket_kind") != "exact" or b.get("bucket_kind") != "exact":
        return False
    left, right = finite(a.get("bucket_low")), finite(b.get("bucket_low"))
    return left is not None and right is not None and abs(left - right) == 1


def threshold_timestamp(
    rows: Iterable[dict[str, Any]], value_field: str, total: float, fraction: float
) -> int | None:
    if total <= 0:
        return None
    running = 0.0
    for row in sorted(rows, key=lambda item: int(item["timestamp_epoch"])):
        running += float(row.get(value_field) or 0)
        if running + EPSILON >= total * fraction:
            return int(row["timestamp_epoch"])
    return None


@dataclass(frozen=True)
class Window:
    start: int
    end: int


class PublicGetClient:
    """Small auditable GET-only client that records every request."""

    def __init__(self, evidence_root: Path = RAW_EVIDENCE_ROOT, attempts: int = 4) -> None:
        self.evidence_root = evidence_root
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.attempts = attempts
        self.requests: list[dict[str, Any]] = []
        self.counter = 0

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        if not url.startswith(("https://data-api.polymarket.com/", "https://gamma-api.polymarket.com/")):
            raise ValueError("only official Polymarket public GET endpoints are allowed")
        encoded = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{encoded}" if encoded else url
        last_error = ""
        for retry in range(self.attempts):
            received = datetime.now(timezone.utc).isoformat()
            try:
                request = urllib.request.Request(
                    full_url,
                    method="GET",
                    headers={"User-Agent": "husky-beijing-public-research/1.0"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                    payload = json.loads(raw)
                self.counter += 1
                path = self.evidence_root / f"{self.counter:05d}.json"
                path.write_bytes(raw)
                count = len(payload) if isinstance(payload, list) else 1
                self.requests.append({
                    "method": "GET",
                    "url": url,
                    "params": dict(params),
                    "start": params.get("start"),
                    "end": params.get("end"),
                    "offset": params.get("offset"),
                    "limit": params.get("limit"),
                    "received_at_utc": received,
                    "record_count": count,
                    "sha256": sha256_bytes(raw),
                    "path": str(path),
                    "success": True,
                    "retries": retry,
                })
                return payload
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
                last_error = repr(exc)
                if retry + 1 < self.attempts:
                    time.sleep(0.5 * (2 ** retry))
        self.requests.append({
            "method": "GET",
            "url": url,
            "params": dict(params),
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "record_count": 0,
            "success": False,
            "retries": self.attempts - 1,
            "error": last_error,
        })
        raise RuntimeError(f"public GET failed after retries: {full_url}: {last_error}")


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
        raise RuntimeError(f"excessive window splitting: {window}")
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.get_json(
            f"{DATA_API}/activity",
            {
                "user": wallet,
                "start": window.start,
                "end": window.end,
                "limit": limit,
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
            },
        )
        if not isinstance(page, list):
            raise RuntimeError("activity response was not a list")
        rows.extend(page)
        if len(page) < limit:
            return rows
        next_offset = offset + limit
        if next_offset >= offset_cap:
            left, right = split_window(window)
            return (
                fetch_activity_window(
                    client, wallet, left, limit=limit, offset_cap=offset_cap, depth=depth + 1
                )
                + fetch_activity_window(
                    client, wallet, right, limit=limit, offset_cap=offset_cap, depth=depth + 1
                )
            )
        offset = next_offset


def fetch_all_activity(
    client: PublicGetClient, wallet: str, start: int, end: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in thirty_day_windows(start, end):
        rows.extend(fetch_activity_window(client, wallet, window))
    deduped, _ = deduplicate_records(rows)
    return sorted(deduped, key=lambda row: (epoch_seconds(row.get("timestamp")), stable_trade_key(row)))


def fetch_offset_endpoint(
    client: PublicGetClient,
    path: str,
    wallet: str,
    *,
    limit: int,
    offset_cap: int,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while offset <= offset_cap:
        page = client.get_json(
            f"{DATA_API}{path}", {"user": wallet, "limit": limit, "offset": offset, **params}
        )
        if not isinstance(page, list):
            raise RuntimeError(f"{path} response was not a list")
        rows.extend(page)
        if len(page) < limit:
            return rows
        offset += limit
    raise RuntimeError(f"{path} saturated its offset cap; no window parameter is available")


def fetch_trades_by_side(client: PublicGetClient, wallet: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for side in ("BUY", "SELL"):
        out.extend(fetch_offset_endpoint(
            client,
            "/trades",
            wallet,
            limit=10_000,
            offset_cap=10_000,
            params={"takerOnly": "false", "side": side},
        ))
    deduped, _ = deduplicate_records(out)
    return deduped


def save_aggregate_evidence(
    root: Path,
    name: str,
    payload: Any,
) -> dict[str, Any]:
    path = root / f"{name}.json"
    write_json(path, payload)
    return {"path": str(path), "sha256": sha256_file(path), "record_count": len(payload) if isinstance(payload, list) else 1}


def refresh_public_evidence(
    analysis_cutoff_utc: str,
    evidence_root: Path = RAW_EVIDENCE_ROOT,
) -> dict[str, Any]:
    cutoff_epoch = int(parse_iso(analysis_cutoff_utc).timestamp())
    client = PublicGetClient(evidence_root)
    profile = client.get_json(f"{GAMMA_API}/public-profile", {"address": HUSKY_WALLET})
    activity = fetch_all_activity(client, HUSKY_WALLET, API_HISTORY_START, cutoff_epoch)
    trades = fetch_trades_by_side(client, HUSKY_WALLET)
    positions = fetch_offset_endpoint(
        client, "/positions", HUSKY_WALLET, limit=500, offset_cap=10_000,
        params={"sizeThreshold": 0, "sortBy": "TOKENS", "sortDirection": "DESC"},
    )
    closed = fetch_offset_endpoint(
        client, "/closed-positions", HUSKY_WALLET, limit=50, offset_cap=100_000,
        params={"sortBy": "TIMESTAMP", "sortDirection": "ASC"},
    )
    aggregates = {
        "profile": save_aggregate_evidence(evidence_root, "profile", profile),
        "activity": save_aggregate_evidence(evidence_root, "activity", activity),
        "trades": save_aggregate_evidence(evidence_root, "trades", trades),
        "positions": save_aggregate_evidence(evidence_root, "positions", positions),
        "closed_positions": save_aggregate_evidence(evidence_root, "closed_positions", closed),
    }
    manifest = {
        "schema_version": "husky_beijing_public_evidence_v1",
        "public_data_only": True,
        "public_get_only": True,
        "wallet": HUSKY_WALLET,
        "history_start_utc": iso_utc(API_HISTORY_START),
        "analysis_started_at_utc": analysis_cutoff_utc,
        "analysis_cutoff_utc": analysis_cutoff_utc,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "requests": client.requests,
        "aggregates": aggregates,
        "all_requests_successful": all(row["success"] for row in client.requests),
    }
    manifest_path = evidence_root / "saved_public_evidence_manifest.json"
    write_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def load_saved_public_evidence(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("wallet", "").lower() != HUSKY_WALLET:
        raise RuntimeError("saved evidence wallet conflicts with Husky wallet")
    if not manifest.get("all_requests_successful"):
        raise RuntimeError("saved evidence contains failed request windows")
    loaded: dict[str, Any] = {}
    for name, meta in manifest["aggregates"].items():
        path = Path(meta["path"])
        if sha256_file(path) != meta["sha256"]:
            raise RuntimeError(f"saved evidence hash mismatch: {name}")
        loaded[name] = json.loads(path.read_text(encoding="utf-8"))
    return manifest, loaded


def repository_wallet_verification(repo_root: Path) -> dict[str, Any]:
    files = (
        repo_root / "data/raw/trades.csv",
        repo_root / "data/raw/activity.csv",
        repo_root / "data/raw/closed_positions.csv",
        repo_root / "data/raw/current_positions.csv",
    )
    evidence: dict[str, list[str]] = {}
    all_wallets: set[str] = set()
    for path in files:
        wallets = {
            str(row.get("proxyWallet") or "").lower()
            for _, row in iter_csv(path)
            if row.get("proxyWallet")
        }
        evidence[str(path.relative_to(repo_root))] = sorted(wallets)
        all_wallets.update(wallets)
    if all_wallets != {HUSKY_WALLET}:
        raise RuntimeError(f"repository wallet conflict: {sorted(all_wallets)}")
    return {"status": "PASS", "wallets_by_file": evidence}


def profile_wallet_verification(profile: dict[str, Any]) -> str:
    observed = str(profile.get("proxyWallet") or "").lower()
    if observed and observed != HUSKY_WALLET:
        raise RuntimeError(f"public profile wallet conflict: {observed}")
    return "PASS" if observed == HUSKY_WALLET else "REPOSITORY_EVIDENCE_ONLY"


def load_repository_beijing(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    for row_number, source in iter_csv(repo_root / "data/processed/weather_trades_normalized.csv"):
        row = dict(source)
        if not is_beijing_highest_market(row):
            continue
        row.update({
            "source_repository_trade": True,
            "source_repository_activity": False,
            "source_current_public_api": False,
            "source_row_number": f"weather_trades_normalized.csv:{row_number}",
        })
        trades.append(row)
    activity: list[dict[str, Any]] = []
    for row_number, source in iter_csv(repo_root / "data/raw/activity.csv"):
        row = dict(source)
        if str(row.get("type") or "").upper() != "TRADE" or not is_beijing_highest_market(row):
            continue
        row.update({
            "source_repository_trade": False,
            "source_repository_activity": True,
            "source_current_public_api": False,
            "source_row_number": f"activity.csv:{row_number}",
        })
        activity.append(row)
    return trades, activity


def prepare_api_beijing(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    for index, source in enumerate(evidence["trades"], start=1):
        row = dict(source)
        if not is_beijing_highest_market(row):
            continue
        row.update({
            "source_repository_trade": False,
            "source_repository_activity": False,
            "source_current_public_api": True,
            "source_row_number": f"api_trades:{index}",
        })
        trades.append(row)
    activity: list[dict[str, Any]] = []
    for index, source in enumerate(evidence["activity"], start=1):
        row = dict(source)
        if str(row.get("type") or "").upper() != "TRADE" or not is_beijing_highest_market(row):
            continue
        row.update({
            "source_repository_trade": False,
            "source_repository_activity": False,
            "source_current_public_api": True,
            "source_row_number": f"api_activity:{index}",
        })
        activity.append(row)
    return trades, activity


def merge_public_fills(
    repository_trades: list[dict[str, Any]],
    repository_activity: list[dict[str, Any]],
    api_trades: list[dict[str, Any]],
    api_activity: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trade_rows, trade_duplicates = deduplicate_records([*repository_trades, *api_trades])
    activity_rows, activity_duplicates = deduplicate_records([*repository_activity, *api_activity])
    trade_join_keys = {activity_join_key(row) for row in trade_rows}
    for row in activity_rows:
        if activity_join_key(row) not in trade_join_keys:
            fallback = dict(row)
            fallback["source_repository_activity"] = bool(row.get("source_repository_activity"))
            trade_rows.append(fallback)
            trade_join_keys.add(activity_join_key(row))

    activity_groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in activity_rows:
        activity_groups[activity_join_key(row)].append(row)
    used: dict[tuple[str, ...], set[int]] = defaultdict(set)
    fills: list[dict[str, Any]] = []
    match_counts: Counter[str] = Counter()

    for source in sorted(trade_rows, key=lambda row: (epoch_seconds(row["timestamp"]), stable_trade_key(row))):
        join_key = activity_join_key(source)
        candidates = [
            (index, row) for index, row in enumerate(activity_groups.get(join_key, []))
            if index not in used[join_key]
        ]
        source_size = decimal(source.get("size"))
        exact = [(index, row) for index, row in candidates if decimal(row.get("size")) == source_size]
        if exact:
            activity_index, activity = exact[0]
            status = "EXACT_SIZE_MATCH"
        elif candidates:
            activity_index, activity = min(
                candidates,
                key=lambda pair: abs((decimal(pair[1].get("size")) or Decimal(0)) - (source_size or Decimal(0))),
            )
            status = "NEAREST_SIZE_MATCH"
        else:
            activity_index, activity = -1, {}
            status = "NO_ACTIVITY_MATCH"
        if activity_index >= 0:
            used[join_key].add(activity_index)
        match_counts[status] += 1
        amount = finite(activity.get("usdcSize"))
        price = finite(source.get("price")) or 0.0
        shares = finite(source.get("size")) or 0.0
        if amount is None:
            amount = price * shares
            amount_source = "price_x_size"
        else:
            amount_source = "activity_usdcSize"
        weather_date = parse_weather_date(source)
        if weather_date is None:
            continue
        bucket = parse_bucket(source)
        timestamp = epoch_seconds(source["timestamp"])
        fill = {
            "event_key": event_key(source),
            "weather_date": weather_date,
            "city": "Beijing",
            "weather_metric": "high",
            "station_status": STATION_LABEL,
            "condition_id": source.get("conditionId") or "",
            "event_slug": source.get("eventSlug") or "",
            "slug": source.get("slug") or "",
            "asset": source.get("asset") or "",
            "outcome": source.get("outcome") or "",
            **bucket,
            "timestamp_epoch": timestamp,
            "public_trade_time_utc": iso_utc(timestamp),
            "public_trade_time_cst": iso_cst(timestamp),
            "relative_phase": relative_phase(timestamp, weather_date),
            "half_hour_bin": half_hour_bin(timestamp),
            "side": str(source.get("side") or "").upper(),
            "price": price,
            "shares": shares,
            "trade_usd": amount,
            "trade_usd_source": amount_source,
            "transaction_hash": str(source.get("transactionHash") or "").lower(),
            "source_repository_trade": bool(source.get("source_repository_trade")),
            "source_repository_activity": bool(source.get("source_repository_activity")) or bool(activity.get("source_repository_activity")),
            "source_current_public_api": bool(source.get("source_current_public_api")) or bool(activity.get("source_current_public_api")),
            "source_row_number": "|".join(filter(None, dict.fromkeys([
                str(source.get("source_row_number") or ""),
                str(activity.get("source_row_number") or ""),
            ]))),
            "activity_match_status": status,
        }
        fills.append(fill)

    fills, final_duplicates = deduplicate_records(fills)
    fills.sort(key=lambda row: (row["timestamp_epoch"], row["transaction_hash"], row["asset"], row["side"]))
    return fills, {
        "repository_and_api_trade_duplicate_rows": trade_duplicates,
        "repository_and_api_activity_duplicate_rows": activity_duplicates,
        "final_duplicate_rows": final_duplicates,
        "activity_match_status": dict(match_counts),
    }


def annotate_adds(fills: list[dict[str, Any]]) -> None:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fills:
        by_event[row["event_key"]].append(row)
    for rows in by_event.values():
        state: dict[str, dict[str, float | None]] = defaultdict(
            lambda: {"buy_usd": 0.0, "buy_shares": 0.0, "previous_price": None}
        )
        for row in sorted(rows, key=lambda item: item["timestamp_epoch"]):
            bucket = row["temperature_bucket"]
            bucket_state = state[bucket]
            row.update({
                "previous_same_bucket_buy_price": None,
                "price_change_vs_previous_buy": None,
                "price_add_class": "",
                "pretrade_average_cost": None,
                "price_change_vs_average_cost": None,
                "average_cost_add_class": "",
            })
            if row["side"] != "BUY":
                continue
            price = float(row["price"])
            previous = bucket_state["previous_price"]
            if previous is not None:
                row["previous_same_bucket_buy_price"] = previous
                row["price_change_vs_previous_buy"] = price - float(previous)
                row["price_add_class"] = classify_price_add(previous, price)
            average = (
                float(bucket_state["buy_usd"]) / float(bucket_state["buy_shares"])
                if float(bucket_state["buy_shares"] or 0) > 0 else None
            )
            if average is not None:
                row["pretrade_average_cost"] = average
                row["price_change_vs_average_cost"] = price - average
                row["average_cost_add_class"] = classify_average_cost_add(average, price)
            bucket_state["buy_usd"] = float(bucket_state["buy_usd"] or 0) + float(row["trade_usd"])
            bucket_state["buy_shares"] = float(bucket_state["buy_shares"] or 0) + float(row["shares"])
            bucket_state["previous_price"] = price


def checkpoint_epoch(weather_date: str, checkpoint: str) -> int:
    day_offset, hour, minute = CHECKPOINTS[checkpoint]
    local_day = date.fromisoformat(weather_date) + timedelta(days=day_offset)
    local = datetime(local_day.year, local_day.month, local_day.day, hour, minute, tzinfo=CST)
    return int(local.astimezone(timezone.utc).timestamp())


def bucket_metrics(buys: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in buys:
        by_bucket[row["temperature_bucket"]].append(row)
    bucket_first = {
        bucket: min(row["timestamp_epoch"] for row in rows) for bucket, rows in by_bucket.items()
    }
    order = sorted(bucket_first, key=lambda bucket: (bucket_first[bucket], bucket))
    bucket_usd = {
        bucket: sum(float(row["trade_usd"]) for row in rows) for bucket, rows in by_bucket.items()
    }
    total = sum(bucket_usd.values())
    dominant = max(bucket_usd, key=lambda bucket: (bucket_usd[bucket], bucket)) if bucket_usd else ""
    representatives = {bucket: rows[0] for bucket, rows in by_bucket.items()}
    adjacent_pairs = [
        [left, right]
        for index, left in enumerate(order)
        for right in order[index + 1:]
        if buckets_adjacent(representatives[left], representatives[right])
    ]
    tail = any(row["bucket_kind"] in {"below", "above"} for row in buys)
    threshold_dominants: dict[str, str] = {}
    for fraction in (0.25, 0.50, 0.75, 1.0):
        target = total * fraction
        running = 0.0
        cumulative: Counter[str] = Counter()
        for row in sorted(buys, key=lambda item: item["timestamp_epoch"]):
            cumulative[row["temperature_bucket"]] += float(row["trade_usd"])
            running += float(row["trade_usd"])
            if running + EPSILON >= target:
                threshold_dominants[f"dominant_bucket_at_{int(fraction * 100)}pct" if fraction < 1 else "dominant_bucket_final"] = max(
                    cumulative, key=lambda bucket: (cumulative[bucket], bucket)
                )
                break
    rotation = len(set(threshold_dominants.values())) > 1
    if len(order) == 1:
        basket_type = "SINGLE_BUCKET_ONLY"
    elif tail and any(row["bucket_kind"] == "exact" for row in buys):
        basket_type = "TAIL_PLUS_EXACT_BASKET"
    elif adjacent_pairs:
        first_gap = bucket_first[order[1]] - bucket_first[order[0]]
        basket_type = "SIMULTANEOUS_MULTI_BUCKET" if first_gap <= SIMULTANEOUS_SECONDS else "SINGLE_THEN_ADJACENT_BASKET"
    elif rotation:
        basket_type = "BUCKET_ROTATION"
    else:
        basket_type = "NON_ADJACENT_BASKET"
    return {
        "first_bought_bucket": order[0] if order else "",
        "dominant_bought_bucket": dominant,
        "bucket_join_order": order,
        "bucket_first_buy_times": {bucket: iso_cst(ts) for bucket, ts in bucket_first.items()},
        "bucket_buy_usd": bucket_usd,
        "bucket_buy_usd_fraction": {
            bucket: value / total if total else None for bucket, value in bucket_usd.items()
        },
        "adjacent_bucket_pairs": adjacent_pairs,
        "tail_bucket_usage": tail,
        "basket_type": basket_type,
        "adjacent_join_delay_seconds": (
            bucket_first[order[1]] - bucket_first[order[0] if order else ""]
            if len(order) > 1 and adjacent_pairs else None
        ),
        "bucket_rotation": rotation,
        **threshold_dominants,
    }


def event_timeline_status(
    rows: list[dict[str, Any]], lifecycle_by_asset: dict[str, dict[str, Any]]
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    state: dict[str, float] = defaultdict(float)
    assets = {row["asset"] for row in rows}
    for row in sorted(rows, key=lambda item: item["timestamp_epoch"]):
        if row["side"] == "BUY":
            state[row["asset"]] += float(row["shares"])
        elif row["side"] == "SELL":
            state[row["asset"]] -= float(row["shares"])
            if state[row["asset"]] < -EPSILON:
                reasons.append(f"unmatched SELL on asset {row['asset']}")
    if reasons:
        return "ENTRY_TIMELINE_PARTIAL_UNMATCHED_SELL", reasons
    reconciliation = False
    for asset in assets:
        lifecycle = lifecycle_by_asset.get(asset)
        if not lifecycle:
            continue
        total_bought = finite(lifecycle.get("closed_total_bought_shares") or lifecycle.get("totalBought"))
        observed = sum(float(row["shares"]) for row in rows if row["asset"] == asset and row["side"] == "BUY")
        if total_bought is not None and abs(total_bought - observed) > max(0.02, 0.01 * max(total_bought, observed)):
            reconciliation = True
            reasons.append(f"BUY shares differ from lifecycle totalBought on asset {asset}")
    if reconciliation:
        return "ENTRY_TIMELINE_PARTIAL_RECONCILIATION", reasons
    if not any(row["side"] == "BUY" for row in rows):
        return "ENTRY_TIMELINE_UNKNOWN", ["no observed BUY"]
    return "ENTRY_TIMELINE_COMPLETE", reasons


def classify_entry(buys: list[dict[str, Any]], total_buy_usd: float) -> str:
    if len(buys) == 1:
        return "ONE_SHOT_ENTRY"
    initial_share = float(buys[0]["trade_usd"]) / total_buy_usd if total_buy_usd else 0
    duration = buys[-1]["timestamp_epoch"] - buys[0]["timestamp_epoch"]
    late_last = relative_phase(buys[-1]["timestamp_epoch"], buys[-1]["weather_date"]) in {
        "D0_1400_1500", "D0_1500_1600", "D0_1600_1800", "D0_1800_2400", "D+1_OR_LATER"
    }
    if initial_share <= 0.15:
        return "SMALL_TEST_THEN_SCALE"
    if late_last and float(buys[-1]["trade_usd"]) / total_buy_usd >= 0.30:
        return "LATE_LARGE_ENTRY"
    if duration >= 60 * 60:
        return "GRADUAL_ACCUMULATION"
    return "UNCLASSIFIED_ENTRY"


def classify_exit(
    buys: list[dict[str, Any]], sells: list[dict[str, Any]], total_buy_shares: float
) -> list[str]:
    if not sells:
        return ["NO_RECORDED_SELL"]
    labels: list[str] = []
    first_buy, first_sell, last_sell = buys[0]["timestamp_epoch"], sells[0]["timestamp_epoch"], sells[-1]["timestamp_epoch"]
    sold = sum(float(row["shares"]) for row in sells)
    if first_sell - first_buy <= 3600:
        labels.append("QUICK_EXIT_WITHIN_1H")
    if datetime.fromtimestamp(first_buy, timezone.utc).astimezone(CST).date() == datetime.fromtimestamp(last_sell, timezone.utc).astimezone(CST).date():
        labels.append("SAME_DAY_EXIT")
    if sold + EPSILON >= total_buy_shares:
        labels.append("FULL_RECORDED_EXIT")
    else:
        labels.append("PARTIAL_EXIT_OBSERVED")
    if any(
        buy["timestamp_epoch"] > first_sell for buy in buys
    ):
        labels.append("REENTRY_AFTER_SELL")
    return labels


def recorded_sell_realized_pnl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate recorded SELL PnL using independent FIFO and average-cost paths."""
    fifo_lots: dict[str, deque[list[float]]] = defaultdict(deque)
    average_state: dict[str, dict[str, float]] = defaultdict(
        lambda: {"shares": 0.0, "cost": 0.0}
    )
    fifo_pnl = average_pnl = 0.0
    unavailable_reasons: list[str] = []
    for row in sorted(rows, key=lambda item: item["timestamp_epoch"]):
        asset = str(row["asset"])
        shares = float(row["shares"])
        cash = float(row["trade_usd"])
        if shares <= EPSILON:
            unavailable_reasons.append(f"non-positive shares on asset {asset}")
            continue
        if row["side"] == "BUY":
            unit_cost = cash / shares
            fifo_lots[asset].append([shares, unit_cost])
            average_state[asset]["shares"] += shares
            average_state[asset]["cost"] += cash
            continue
        if row["side"] != "SELL":
            continue
        available_fifo = sum(lot[0] for lot in fifo_lots[asset])
        available_average = average_state[asset]["shares"]
        if shares > available_fifo + EPSILON or shares > available_average + EPSILON:
            unavailable_reasons.append(f"unmatched SELL on asset {asset}")
            continue
        proceeds_per_share = cash / shares
        remaining = shares
        while remaining > EPSILON:
            lot = fifo_lots[asset][0]
            used = min(remaining, lot[0])
            fifo_pnl += used * (proceeds_per_share - lot[1])
            lot[0] -= used
            remaining -= used
            if lot[0] <= EPSILON:
                fifo_lots[asset].popleft()
        state = average_state[asset]
        average_unit_cost = state["cost"] / state["shares"]
        average_pnl += cash - shares * average_unit_cost
        state["shares"] -= shares
        state["cost"] -= shares * average_unit_cost
        if state["shares"] <= EPSILON:
            state["shares"] = 0.0
            state["cost"] = 0.0
    if unavailable_reasons:
        return {
            "recorded_sell_realized_pnl_fifo": None,
            "recorded_sell_realized_pnl_average_cost": None,
            "sell_pnl_method_agreement": None,
            "sell_pnl_method_disagreement": False,
            "sell_pnl_status": "SELL_PNL_UNAVAILABLE",
            "sell_pnl_unavailable_reasons": unavailable_reasons,
        }
    scale = max(abs(fifo_pnl), abs(average_pnl), PNL_CENT_TOLERANCE)
    same_direction = (
        (fifo_pnl > PNL_CENT_TOLERANCE and average_pnl > PNL_CENT_TOLERANCE)
        or (fifo_pnl < -PNL_CENT_TOLERANCE and average_pnl < -PNL_CENT_TOLERANCE)
        or (
            abs(fifo_pnl) <= PNL_CENT_TOLERANCE
            and abs(average_pnl) <= PNL_CENT_TOLERANCE
        )
    )
    material_difference = abs(fifo_pnl - average_pnl) > max(
        PNL_CENT_TOLERANCE,
        SELL_PNL_MATERIAL_DIFFERENCE_FRACTION * scale,
    )
    disagreement = not same_direction or material_difference
    return {
        "recorded_sell_realized_pnl_fifo": fifo_pnl,
        "recorded_sell_realized_pnl_average_cost": average_pnl,
        "sell_pnl_method_agreement": not disagreement,
        "sell_pnl_method_disagreement": disagreement,
        "sell_pnl_status": (
            "SELL_PNL_METHOD_DISAGREEMENT" if disagreement else "SELL_PNL_AVAILABLE"
        ),
        "sell_pnl_unavailable_reasons": [],
    }


def final_path_classification(event: dict[str, Any]) -> str:
    if event.get("held_to_settlement_observed"):
        return "HOLD_TO_SETTLEMENT_OBSERVED"
    if event.get("sell_fill_count", 0) == 0:
        return "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN"
    if "FULL_RECORDED_EXIT" in event.get("exit_classifications", []):
        return "FULL_RECORDED_EXIT"
    return "PARTIAL_EXIT_FINAL_PATH_UNKNOWN"


def event_summary(
    key: str,
    rows: list[dict[str, Any]],
    lifecycle_by_asset: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda item: item["timestamp_epoch"])
    buys = [row for row in rows if row["side"] == "BUY"]
    sells = [row for row in rows if row["side"] == "SELL"]
    total_buy_usd = sum(float(row["trade_usd"]) for row in buys)
    total_sell_usd = sum(float(row["trade_usd"]) for row in sells)
    total_buy_shares = sum(float(row["shares"]) for row in buys)
    total_sell_shares = sum(float(row["shares"]) for row in sells)
    build_times = {
        f"build_{int(fraction * 100)}pct_time_utc": (
            iso_utc(ts) if (ts := threshold_timestamp(buys, "trade_usd", total_buy_usd, fraction)) is not None else None
        )
        for fraction in THRESHOLDS
    }
    build_times_cst = {
        key_name.replace("_utc", "_cst"): (parse_iso(value).astimezone(CST).isoformat() if value else None)
        for key_name, value in build_times.items()
    }
    sold_times: dict[str, Any] = {}
    for fraction in THRESHOLDS:
        ts = threshold_timestamp(sells, "shares", total_buy_shares, fraction)
        sold_times[f"sold_{int(fraction * 100)}pct_time_utc"] = iso_utc(ts) if ts is not None else None
        sold_times[f"sold_{int(fraction * 100)}pct_time_cst"] = iso_cst(ts) if ts is not None else None
    status, reasons = event_timeline_status(rows, lifecycle_by_asset)
    basket = bucket_metrics(buys)
    sell_pnl = recorded_sell_realized_pnl(rows) if sells else {
        "recorded_sell_realized_pnl_fifo": None,
        "recorded_sell_realized_pnl_average_cost": None,
        "sell_pnl_method_agreement": None,
        "sell_pnl_method_disagreement": False,
        "sell_pnl_status": "NO_RECORDED_SELL",
        "sell_pnl_unavailable_reasons": [],
    }
    if sells and status != "ENTRY_TIMELINE_COMPLETE":
        sell_pnl = {
            "recorded_sell_realized_pnl_fifo": None,
            "recorded_sell_realized_pnl_average_cost": None,
            "sell_pnl_method_agreement": None,
            "sell_pnl_method_disagreement": False,
            "sell_pnl_status": "SELL_PNL_UNAVAILABLE",
            "sell_pnl_unavailable_reasons": [
                "entry timeline is incomplete; recorded SELL cost basis is not reliable"
            ],
        }
    return {
        "event_key": key,
        "weather_date": buys[0]["weather_date"] if buys else rows[0]["weather_date"],
        "city": "Beijing",
        "weather_metric": "high",
        "station_status": STATION_LABEL,
        "event_slug": rows[0]["event_slug"],
        "market_count": len({row["condition_id"] for row in rows}),
        "outcome_token_count": len({row["asset"] for row in rows}),
        "entry_timeline_status": status,
        "entry_timeline_reasons": reasons,
        "first_observed_buy_time_utc": iso_utc(buys[0]["timestamp_epoch"]) if buys else None,
        "first_observed_buy_time_cst": iso_cst(buys[0]["timestamp_epoch"]) if buys else None,
        **build_times,
        **build_times_cst,
        "last_buy_time_utc": iso_utc(buys[-1]["timestamp_epoch"]) if buys else None,
        "last_buy_time_cst": iso_cst(buys[-1]["timestamp_epoch"]) if buys else None,
        "initial_buy_usd": float(buys[0]["trade_usd"]) if buys else 0,
        "initial_buy_share_of_final_buy_usd": (
            float(buys[0]["trade_usd"]) / total_buy_usd if buys and total_buy_usd else None
        ),
        "total_buy_usd": total_buy_usd,
        "total_sell_usd": total_sell_usd,
        "total_buy_shares": total_buy_shares,
        "total_sell_shares": total_sell_shares,
        "buy_fill_count": len(buys),
        "sell_fill_count": len(sells),
        "buy_duration_seconds": (
            buys[-1]["timestamp_epoch"] - buys[0]["timestamp_epoch"] if buys else None
        ),
        "phase_buy_usd": dict(Counter({
            phase: sum(float(row["trade_usd"]) for row in buys if row["relative_phase"] == phase)
            for phase in {row["relative_phase"] for row in buys}
        })),
        "d_minus_1_buy_usd_share": (
            sum(float(row["trade_usd"]) for row in buys if row["relative_phase"].startswith("D-1") or row["relative_phase"] == "D-2_OR_EARLIER")
            / total_buy_usd if total_buy_usd else None
        ),
        "d0_buy_usd_share": (
            sum(float(row["trade_usd"]) for row in buys if row["relative_phase"].startswith("D0_"))
            / total_buy_usd if total_buy_usd else None
        ),
        "d0_after_1200_buy_usd_share": (
            sum(float(row["trade_usd"]) for row in buys if row["relative_phase"] in {
                "D0_1200_1300", "D0_1300_1400", "D0_1400_1500",
                "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
            }) / total_buy_usd if total_buy_usd else None
        ),
        "d0_after_1400_buy_usd_share": (
            sum(float(row["trade_usd"]) for row in buys if row["relative_phase"] in {
                "D0_1400_1500", "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
            }) / total_buy_usd if total_buy_usd else None
        ),
        "d0_after_1500_buy_usd_share": (
            sum(float(row["trade_usd"]) for row in buys if row["relative_phase"] in {
                "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
            }) / total_buy_usd if total_buy_usd else None
        ),
        "time_first_to_build_50_seconds": (
            threshold_timestamp(buys, "trade_usd", total_buy_usd, 0.5) - buys[0]["timestamp_epoch"]
            if buys and threshold_timestamp(buys, "trade_usd", total_buy_usd, 0.5) is not None else None
        ),
        "time_build_50_to_last_buy_seconds": (
            buys[-1]["timestamp_epoch"] - threshold_timestamp(buys, "trade_usd", total_buy_usd, 0.5)
            if buys and threshold_timestamp(buys, "trade_usd", total_buy_usd, 0.5) is not None else None
        ),
        "entry_classification": classify_entry(buys, total_buy_usd),
        **basket,
        "first_sell_time_utc": iso_utc(sells[0]["timestamp_epoch"]) if sells else None,
        "first_sell_time_cst": iso_cst(sells[0]["timestamp_epoch"]) if sells else None,
        **sold_times,
        "last_sell_time_utc": iso_utc(sells[-1]["timestamp_epoch"]) if sells else None,
        "last_sell_time_cst": iso_cst(sells[-1]["timestamp_epoch"]) if sells else None,
        "holding_duration_to_first_sell_seconds": (
            sells[0]["timestamp_epoch"] - buys[0]["timestamp_epoch"] if sells and buys else None
        ),
        "holding_duration_to_last_sell_seconds": (
            sells[-1]["timestamp_epoch"] - buys[0]["timestamp_epoch"] if sells and buys else None
        ),
        "exit_classifications": classify_exit(buys, sells, total_buy_shares),
        **sell_pnl,
        "recorded_remaining_shares": total_buy_shares - total_sell_shares,
        "recorded_sell_share_fraction": min(total_sell_shares / total_buy_shares, 1.0) if total_buy_shares else None,
        "plain_language_summary": (
            f"{iso_cst(buys[0]['timestamp_epoch']) if buys else '未观察到BUY'} 开始，"
            f"共 {len(buys)} 笔公开BUY成交、{len(sells)} 笔公开SELL成交；"
            f"投入 ${total_buy_usd:.2f}，{basket['basket_type']}，"
            f"路径状态 {status}。"
        ),
    }


def load_position_evidence(
    repo_root: Path, evidence: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    lifecycle = {
        row["asset"]: row
        for row in read_csv(repo_root / "data/processed/weather_position_lifecycle.csv")
        if row.get("asset")
    }
    current: dict[str, dict[str, Any]] = {}
    closed: dict[str, dict[str, Any]] = {}
    for row in evidence["positions"]:
        if is_beijing_highest_market(row):
            current[str(row["asset"])] = row
    for row in evidence["closed_positions"]:
        if is_beijing_highest_market(row):
            closed[str(row["asset"])] = row
    return lifecycle, current, closed


def classify_event_position(
    assets: set[str],
    current_by_asset: dict[str, dict[str, Any]],
    analysis_cutoff_utc: str,
    *,
    strict_closed: bool,
) -> tuple[str, dict[str, str]]:
    if strict_closed:
        return "CLOSED_POSITION_CONFIRMED", {}
    asset_statuses = {
        asset: classify_position_row(current_by_asset[asset], analysis_cutoff_utc)
        for asset in assets
        if asset in current_by_asset
    }
    if not asset_statuses:
        return "POSITION_STATUS_UNKNOWN", asset_statuses
    statuses = set(asset_statuses.values())
    if "ACTIVE_OPEN_CONFIRMED" in statuses:
        return "ACTIVE_OPEN_CONFIRMED", asset_statuses
    if statuses == {"RESOLVED_REDEEMABLE_UNREDEEMED"}:
        return "RESOLVED_REDEEMABLE_UNREDEEMED", asset_statuses
    if statuses == {"PAST_ENDDATE_STATUS_UNKNOWN"}:
        return "PAST_ENDDATE_STATUS_UNKNOWN", asset_statuses
    return "POSITION_STATUS_UNKNOWN", asset_statuses


def attach_pnl(
    events: list[dict[str, Any]],
    fills_by_event: dict[str, list[dict[str, Any]]],
    repo_root: Path,
    current_by_asset: dict[str, dict[str, Any]],
    closed_by_asset: dict[str, dict[str, Any]],
    analysis_cutoff_utc: str,
) -> list[dict[str, Any]]:
    city_day = {
        f"{row['weather_date']}__beijing__high": row
        for row in read_csv(repo_root / "data/processed/city_day_pnl.csv")
        if str(row.get("city") or "").lower() == "beijing" and str(row.get("weather_metric") or "").lower() == "high"
    }
    out: list[dict[str, Any]] = []
    for event in events:
        key = event["event_key"]
        assets = {row["asset"] for row in fills_by_event[key] if row["side"] == "BUY"}
        current_assets = assets & set(current_by_asset)
        closed_assets = assets & set(closed_by_asset)
        repo = city_day.get(key)
        strict_pnl: float | None = None
        pnl_source = ""
        exit_modes = str(repo.get("exit_modes") or "") if repo else ""
        if repo and str(repo.get("has_open_or_unresolved")).lower() == "false":
            value = finite(repo.get("closed_authoritative_pnl"))
            if value is not None:
                strict_pnl = value
                pnl_source = "repository_city_day_pnl"
        if assets and closed_assets == assets and not current_assets:
            candidate = sum(finite(closed_by_asset[a].get("realizedPnl")) or 0 for a in assets)
            strict_pnl = candidate
            pnl_source = "current_public_closed_positions"
        snapshot_pnl = sum(
            position_snapshot_pnl(
                current_by_asset[a], "C_cashPnl_plus_realizedPnl"
            ) or 0
            for a in current_assets
        )
        incomplete_closed_pnl = sum(
            finite(closed_by_asset[a].get("realizedPnl")) or 0
            for a in closed_assets
        )
        position_status, asset_position_statuses = classify_event_position(
            assets,
            current_by_asset,
            analysis_cutoff_utc,
            strict_closed=strict_pnl is not None,
        )
        status = (
            "STRICT_CLOSED_SETTLED"
            if strict_pnl is not None else position_status
        )
        held_to_settlement = bool(
            strict_pnl is not None and "resolution" in exit_modes
        )
        event_with_path = {
            **event,
            "held_to_settlement_observed": held_to_settlement,
        }
        final_path = final_path_classification(event_with_path)
        out.append({
            **event,
            "pnl_status": status,
            "position_status": position_status,
            "asset_position_statuses": asset_position_statuses,
            "strict_pnl": strict_pnl,
            "strict_roi": strict_pnl / event["total_buy_usd"] if strict_pnl is not None and event["total_buy_usd"] else None,
            "pnl_source": pnl_source,
            "authoritative_exit_modes": exit_modes,
            "held_to_settlement_observed": held_to_settlement,
            "confirmed_settlement_path": bool(
                final_path in {"HOLD_TO_SETTLEMENT_OBSERVED", "FULL_RECORDED_EXIT"}
            ),
            "final_path_classification": final_path,
            "observable_but_incomplete_pnl": (
                incomplete_closed_pnl if strict_pnl is None and closed_assets else None
            ),
            "active_open_mark_to_market_pnl": (
                snapshot_pnl if position_status == "ACTIVE_OPEN_CONFIRMED" else None
            ),
            "resolved_redeemable_snapshot_pnl": (
                snapshot_pnl
                if position_status == "RESOLVED_REDEEMABLE_UNREDEEMED" else None
            ),
            "past_enddate_status_unknown_snapshot_pnl": (
                snapshot_pnl
                if position_status == "PAST_ENDDATE_STATUS_UNKNOWN" else None
            ),
            "position_snapshot_pnl_formula": "C_cashPnl_plus_realizedPnl",
            "current_position_asset_count": len(current_assets),
            "closed_position_asset_count": len(closed_assets),
            "unresolved_buy_usd": event["total_buy_usd"] if strict_pnl is None else 0,
        })
    return out


def archetype_labels(event: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    buys = [row for row in rows if row["side"] == "BUY"]
    sells = [row for row in rows if row["side"] == "SELL"]
    phases = {row["relative_phase"] for row in buys}
    if any(phase.startswith("D-1") or phase == "D-2_OR_EARLIER" for phase in phases):
        labels.append("D1_EARLY_POSITION")
    if any(phase.startswith("D-1") for phase in phases) and any(phase.startswith("D0_") for phase in phases):
        labels.append("D1_TEST_D0_SCALE")
    if phases & {"D0_0800_1000", "D0_1000_1100"}:
        labels.append("D0_MORNING_ENTRY")
    if phases & {"D0_1100_1200", "D0_1200_1300", "D0_1300_1400"}:
        labels.append("D0_WARMING_ENTRY")
    if phases & {"D0_1400_1500", "D0_1500_1600", "D0_1600_1800", "D0_1800_2400"}:
        labels.append("D0_LATE_ENTRY")
    if event["entry_classification"] == "ONE_SHOT_ENTRY":
        labels.append("ONE_SHOT_ENTRY")
    if event["entry_classification"] in {"GRADUAL_ACCUMULATION", "SMALL_TEST_THEN_SCALE"}:
        labels.append("GRADUAL_ACCUMULATION")
    if any(row["price_add_class"] == "PRICE_DOWN_ADD" for row in buys):
        labels.append("AVERAGING_DOWN")
    if any(row["price_add_class"] == "PRICE_UP_ADD" for row in buys):
        labels.append("CHASE_UP")
    if event["adjacent_bucket_pairs"]:
        labels.append("ADJACENT_BASKET")
    if event["bucket_rotation"]:
        labels.append("BUCKET_ROTATION")
    if "QUICK_EXIT_WITHIN_1H" in event["exit_classifications"]:
        labels.append("QUICK_FLIP")
    labels.extend(
        label for label in event.get("exit_classifications", [])
        if label in {"PARTIAL_EXIT_OBSERVED", "FULL_RECORDED_EXIT"}
    )
    if sells and event.get("sell_pnl_status") == "SELL_PNL_UNAVAILABLE":
        labels.append("SELL_PNL_UNAVAILABLE")
    elif sells and event.get("sell_pnl_method_disagreement"):
        labels.append("SELL_PNL_METHOD_DISAGREEMENT")
    elif "PARTIAL_EXIT_OBSERVED" in event.get("exit_classifications", []):
        fifo = event.get("recorded_sell_realized_pnl_fifo")
        average = event.get("recorded_sell_realized_pnl_average_cost")
        if fifo is not None and average is not None:
            if fifo > PNL_CENT_TOLERANCE and average > PNL_CENT_TOLERANCE:
                labels.append("PROFITABLE_PARTIAL_SELL_OBSERVED")
            elif fifo < -PNL_CENT_TOLERANCE and average < -PNL_CENT_TOLERANCE:
                labels.append("LOSS_REALIZING_PARTIAL_SELL_OBSERVED")
    final_path = event.get("final_path_classification") or final_path_classification(event)
    labels.append(final_path)
    return sorted(set(labels))


def distribution_rows(
    fills: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    phase_keys = [
        "D-2_OR_EARLIER", "D-1_0000_1200", "D-1_1200_1500",
        "D-1_1500_1800", "D-1_1800_2400", "D0_0000_0800",
        "D0_0800_1000", "D0_1000_1100", "D0_1100_1200",
        "D0_1200_1300", "D0_1300_1400", "D0_1400_1500",
        "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
        "D+1_OR_LATER",
    ]
    half_hour_keys = [
        f"{minute // 60:02d}:{minute % 60:02d}—{(minute + 30) // 60:02d}:{(minute + 30) % 60:02d}"
        for minute in range(0, 24 * 60, 30)
    ]
    for grain, keys in (
        ("RELATIVE_PHASE", phase_keys),
        ("HALF_HOUR_CST", half_hour_keys),
    ):
        for key in keys:
            subset = [
                row for row in fills
                if (row["relative_phase"] if grain == "RELATIVE_PHASE" else row["half_hour_bin"]) == key
            ]
            buy = [row for row in subset if row["side"] == "BUY"]
            sell = [row for row in subset if row["side"] == "SELL"]
            first_buy_events = sum(
                1 for event in events
                if event.get("first_observed_buy_time_cst")
                and half_or_phase_of_iso(event["first_observed_buy_time_cst"], event["weather_date"], grain) == key
            )
            build_50_events = sum(
                1 for event in events
                if event.get("build_50pct_time_cst")
                and half_or_phase_of_iso(event["build_50pct_time_cst"], event["weather_date"], grain) == key
            )
            last_buy_events = sum(
                1 for event in events
                if event.get("last_buy_time_cst")
                and half_or_phase_of_iso(event["last_buy_time_cst"], event["weather_date"], grain) == key
            )
            first_sell_events = sum(
                1 for event in events
                if event.get("first_sell_time_cst")
                and half_or_phase_of_iso(event["first_sell_time_cst"], event["weather_date"], grain) == key
            )
            result.append({
                "grain": grain,
                "time_bin": key,
                "buy_fill_count": len(buy),
                "buy_usd": sum(row["trade_usd"] for row in buy),
                "buy_shares": sum(row["shares"] for row in buy),
                "first_entry_event_count": first_buy_events,
                "build_50_event_count": build_50_events,
                "last_buy_event_count": last_buy_events,
                "sell_fill_count": len(sell),
                "sell_usd": sum(row["trade_usd"] for row in sell),
                "first_sell_event_count": first_sell_events,
            })
    return result


def half_or_phase_of_iso(timestamp: str, weather_date: str, grain: str) -> str:
    ts = int(parse_iso(timestamp).timestamp())
    return relative_phase(ts, weather_date) if grain == "RELATIVE_PHASE" else half_hour_bin(ts)


def candidate_rows(
    events: list[dict[str, Any]],
    fills_by_event: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    scopes = {
        "ALL_OBSERVABLE_EVENTS": events,
        "ENTRY_TIMELINE_COMPLETE_ONLY": [
            event for event in events if event["entry_timeline_status"] == "ENTRY_TIMELINE_COMPLETE"
        ],
    }
    for scope_name, scoped_events in scopes.items():
        for checkpoint in CHECKPOINTS:
            before_usd = after_usd = 0.0
            first_before = first_after = continued = 0
            thresholds_before = Counter()
            for event in scoped_events:
                cutoff = checkpoint_epoch(event["weather_date"], checkpoint)
                buys = [row for row in fills_by_event[event["event_key"]] if row["side"] == "BUY"]
                before_usd += sum(row["trade_usd"] for row in buys if row["timestamp_epoch"] <= cutoff)
                after_usd += sum(row["trade_usd"] for row in buys if row["timestamp_epoch"] > cutoff)
                has_buy_before = any(row["timestamp_epoch"] <= cutoff for row in buys)
                has_buy_after = any(row["timestamp_epoch"] > cutoff for row in buys)
                if has_buy_before:
                    first_before += 1
                elif has_buy_after:
                    first_after += 1
                if has_buy_before and has_buy_after:
                    continued += 1
                for fraction in THRESHOLDS:
                    threshold = threshold_timestamp(buys, "trade_usd", event["total_buy_usd"], fraction)
                    if threshold is not None and threshold <= cutoff:
                        thresholds_before[int(fraction * 100)] += 1
            total = before_usd + after_usd
            out.append({
                "scope": scope_name,
                "checkpoint": checkpoint,
                "event_count": len(scoped_events),
                "buy_usd_before": before_usd,
                "buy_usd_after": after_usd,
                "buy_usd_share_before": before_usd / total if total else None,
                "buy_usd_share_after": after_usd / total if total else None,
                "first_entry_before_event_count": first_before,
                "first_entry_after_event_count": first_after,
                "build_10_before_event_count": thresholds_before[10],
                "build_25_before_event_count": thresholds_before[25],
                "build_50_before_event_count": thresholds_before[50],
                "build_75_before_event_count": thresholds_before[75],
                "build_90_before_event_count": thresholds_before[90],
                "continues_buying_after_event_count": continued,
            })
    return out


def event_order_drawdown(events: list[dict[str, Any]]) -> tuple[float, int]:
    strict = sorted(
        [event for event in events if event.get("strict_pnl") is not None],
        key=lambda event: event["weather_date"],
    )
    cumulative = peak = 0.0
    max_drawdown = 0.0
    consecutive = maximum_consecutive = 0
    for event in strict:
        pnl = float(event["strict_pnl"])
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        if pnl < 0:
            consecutive += 1
            maximum_consecutive = max(maximum_consecutive, consecutive)
        else:
            consecutive = 0
    return max_drawdown, maximum_consecutive


def median_relative_time(events: list[dict[str, Any]], field: str) -> str | None:
    relative_minutes: list[float] = []
    for event in events:
        value = event.get(field)
        if not value:
            continue
        local = parse_iso(value).astimezone(CST)
        weather_day = date.fromisoformat(event["weather_date"])
        weather_midnight = datetime(
            weather_day.year, weather_day.month, weather_day.day, tzinfo=CST
        )
        relative_minutes.append((local - weather_midnight).total_seconds() / 60)
    if not relative_minutes:
        return None
    med = int(statistics.median(relative_minutes))
    day_offset, minute = divmod(med, 24 * 60)
    day_label = "D0" if day_offset == 0 else (f"D+{day_offset}" if day_offset > 0 else f"D{day_offset}")
    return f"{day_label} {minute // 60:02d}:{minute % 60:02d} CST"


def median_present(events: list[dict[str, Any]], field: str, divisor: float = 1.0) -> float | None:
    values = [
        float(event[field]) / divisor
        for event in events
        if event.get(field) is not None
    ]
    return statistics.median(values) if values else None


def timing_subset_metrics(subset: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event_count": len(subset),
        "total_buy_usd": sum(event["total_buy_usd"] for event in subset),
        "median_total_buy_usd": median_present(subset, "total_buy_usd"),
        "median_first_buy_time": median_relative_time(subset, "first_observed_buy_time_cst"),
        "median_build_25_time": median_relative_time(subset, "build_25pct_time_cst"),
        "median_build_50_time": median_relative_time(subset, "build_50pct_time_cst"),
        "median_build_75_time": median_relative_time(subset, "build_75pct_time_cst"),
        "median_last_buy_time": median_relative_time(subset, "last_buy_time_cst"),
        "median_initial_buy_share": median_present(
            subset, "initial_buy_share_of_final_buy_usd"
        ),
        "median_d_minus_1_buy_usd_share": median_present(
            subset, "d_minus_1_buy_usd_share"
        ),
        "median_d0_buy_usd_share": median_present(subset, "d0_buy_usd_share"),
        "median_d0_after_1200_buy_usd_share": median_present(
            subset, "d0_after_1200_buy_usd_share"
        ),
        "median_d0_after_1400_buy_usd_share": median_present(
            subset, "d0_after_1400_buy_usd_share"
        ),
        "median_d0_after_1500_buy_usd_share": median_present(
            subset, "d0_after_1500_buy_usd_share"
        ),
        "median_buy_duration_hours": median_present(
            subset, "buy_duration_seconds", 3600
        ),
        "median_bucket_count": median_present(subset, "outcome_token_count"),
        "adjacent_basket_event_share": (
            sum(bool(event["adjacent_bucket_pairs"]) for event in subset) / len(subset)
            if subset else None
        ),
        "price_up_add_count": sum(event.get("price_up_add_count", 0) for event in subset),
        "price_down_add_count": sum(event.get("price_down_add_count", 0) for event in subset),
        "price_flat_add_count": sum(event.get("price_flat_add_count", 0) for event in subset),
        "median_first_sell_time": median_relative_time(subset, "first_sell_time_cst"),
        "median_last_sell_time": median_relative_time(subset, "last_sell_time_cst"),
        "median_recorded_sell_share_fraction": median_present(
            subset, "recorded_sell_share_fraction"
        ),
        "median_holding_to_first_sell_hours": median_present(
            subset, "holding_duration_to_first_sell_seconds", 3600
        ),
    }


def timing_comparison(events: list[dict[str, Any]]) -> dict[str, Any]:
    scopes = {
        "STRICT_PNL_ENTRY_COMPLETE_ONLY": [
            event
            for event in events
            if event.get("strict_pnl") is not None
            and event.get("entry_timeline_status") == "ENTRY_TIMELINE_COMPLETE"
        ],
        "STRICT_PNL_ALL": [
            event for event in events if event.get("strict_pnl") is not None
        ],
    }
    result: dict[str, Any] = {}
    for scope, scoped_events in scopes.items():
        result[scope] = {
            "profit_events": timing_subset_metrics(
                [event for event in scoped_events if float(event["strict_pnl"]) > 0]
            ),
            "loss_events": timing_subset_metrics(
                [event for event in scoped_events if float(event["strict_pnl"]) < 0]
            ),
        }
    return result


def build_archetype_rows(
    events: list[dict[str, Any]], fills_by_event: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        labels = archetype_labels(event, fills_by_event[event["event_key"]])
        event["strategy_archetypes"] = labels
        for label in labels:
            groups[label].append(event)
    out: list[dict[str, Any]] = []
    for label, subset in sorted(groups.items()):
        complete = [event for event in subset if event.get("strict_pnl") is not None]
        pnls = [float(event["strict_pnl"]) for event in complete]
        winner = max(pnls) if pnls else 0
        out.append({
            "archetype": label,
            "event_count": len(subset),
            "buy_usd": sum(event["total_buy_usd"] for event in subset),
            "pnl_complete_event_count": len(complete),
            "strict_pnl": sum(pnls),
            "roi": sum(pnls) / sum(event["total_buy_usd"] for event in complete) if complete and sum(event["total_buy_usd"] for event in complete) else None,
            "profit_event_count": sum(pnl > 0 for pnl in pnls),
            "loss_event_count": sum(pnl < 0 for pnl in pnls),
            "median_pnl": statistics.median(pnls) if pnls else None,
            "pnl_without_largest_winner": sum(pnls) - winner if pnls else None,
            "data_limit": "研究分类；公开成交不含未成交或撤销订单，分类关联不证明因果。",
        })
    return out


def summarize(
    fills: list[dict[str, Any]],
    events: list[dict[str, Any]],
    repository_trades: list[dict[str, Any]],
    api_trades: list[dict[str, Any]],
    analysis_started_at_utc: str,
    analysis_cutoff_utc: str,
    manifest: dict[str, Any],
    merge_meta: dict[str, Any],
    wallet_verification: str,
    resolved_pnl_reconciliation: dict[str, Any],
) -> dict[str, Any]:
    buys = [row for row in fills if row["side"] == "BUY"]
    sells = [row for row in fills if row["side"] == "SELL"]
    strict = [event for event in events if event.get("strict_pnl") is not None]
    strict_pnls = sorted(
        [(float(event["strict_pnl"]), event["event_key"]) for event in strict], reverse=True
    )
    positive = [item for item in strict_pnls if item[0] > 0]
    total_positive = sum(value for value, _ in positive)
    total_strict_pnl = sum(value for value, _ in strict_pnls)
    total_strict_buy = sum(event["total_buy_usd"] for event in strict)
    max_drawdown, max_consecutive = event_order_drawdown(events)
    repository_filtered = sorted(repository_trades, key=lambda row: epoch_seconds(row["timestamp"]))
    api_filtered = sorted(api_trades, key=lambda row: epoch_seconds(row["timestamp"]))
    repo_keys = {stable_trade_key(row) for row in repository_filtered}
    new_api = sum(stable_trade_key(row) not in repo_keys for row in api_filtered)
    cutoff_epoch = int(parse_iso(analysis_cutoff_utc).timestamp())
    first_ts = min(row["timestamp_epoch"] for row in fills)
    last_ts = max(row["timestamp_epoch"] for row in fills)
    event_buy_values = [event["total_buy_usd"] for event in events]
    adds = Counter(row["price_add_class"] for row in buys if row["price_add_class"])
    average_adds = Counter(row["average_cost_add_class"] for row in buys if row["average_cost_add_class"])
    complete_events = [event for event in events if event["entry_timeline_status"] == "ENTRY_TIMELINE_COMPLETE"]
    phase_usd = Counter()
    for row in buys:
        phase_usd[row["relative_phase"]] += row["trade_usd"]
    total_buy_usd = sum(phase_usd.values())
    d_minus_1 = sum(value for phase, value in phase_usd.items() if phase.startswith("D-1") or phase == "D-2_OR_EARLIER")
    d0 = sum(value for phase, value in phase_usd.items() if phase.startswith("D0_"))
    after_12 = sum(value for phase, value in phase_usd.items() if phase in {
        "D0_1200_1300", "D0_1300_1400", "D0_1400_1500", "D0_1500_1600",
        "D0_1600_1800", "D0_1800_2400",
    })
    after_14 = sum(value for phase, value in phase_usd.items() if phase in {
        "D0_1400_1500", "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
    })
    after_15 = sum(value for phase, value in phase_usd.items() if phase in {
        "D0_1500_1600", "D0_1600_1800", "D0_1800_2400",
    })
    weather_dates = sorted({event["weather_date"] for event in events})
    gaps: list[str] = []
    for previous, current in zip(weather_dates, weather_dates[1:]):
        days = (date.fromisoformat(current) - date.fromisoformat(previous)).days
        if days > 14:
            gaps.append(f"{previous} to {current}: {days} days")
    months = sorted({weather_date[:7] for weather_date in weather_dates})
    expected_months: list[str] = []
    month_cursor = date.fromisoformat(f"{months[0]}-01")
    final_month = date.fromisoformat(f"{months[-1]}-01")
    while month_cursor <= final_month:
        expected_months.append(month_cursor.strftime("%Y-%m"))
        month_cursor = (
            date(month_cursor.year + 1, 1, 1)
            if month_cursor.month == 12
            else date(month_cursor.year, month_cursor.month + 1, 1)
        )
    checkpoint = candidate_rows(events, {event["event_key"]: [row for row in fills if row["event_key"] == event["event_key"]] for event in events})
    return {
        "schema_version": "husky_beijing_full_trade_study_v1",
        "public_record_semantics": "public trade fill count; not original order count",
        "analysis_started_at_utc": analysis_started_at_utc,
        "analysis_cutoff_utc": analysis_cutoff_utc,
        "api_received_at_utc": manifest.get("generated_at_utc"),
        "husky_wallet": HUSKY_WALLET,
        "husky_wallet_verification": wallet_verification,
        "station_status": STATION_LABEL,
        "repository_data_range": {
            "first": iso_utc(repository_filtered[0]["timestamp"]) if repository_filtered else None,
            "last": iso_utc(repository_filtered[-1]["timestamp"]) if repository_filtered else None,
        },
        "public_api_data_range": {
            "first": iso_utc(api_filtered[0]["timestamp"]) if api_filtered else None,
            "last": iso_utc(api_filtered[-1]["timestamp"]) if api_filtered else None,
        },
        "beijing_first_observed_public_trade_utc": iso_utc(first_ts),
        "beijing_first_observed_public_trade_cst": iso_cst(first_ts),
        "beijing_last_observed_public_trade_utc": iso_utc(last_ts),
        "beijing_last_observed_public_trade_cst": iso_cst(last_ts),
        "beijing_first_weather_date": min(weather_dates),
        "beijing_last_weather_date": max(weather_dates),
        "earliest_observed_current_api_history_confidence": "HIGH",
        "absolute_lifetime_first_beijing_trade": "NOT_PROVEN",
        "latest_trade_lag_seconds": cutoff_epoch - last_ts,
        "new_beijing_fills_after_repository_snapshot": new_api,
        "public_request_coverage": (
            "PASS" if manifest.get("all_requests_successful") else "FAIL"
        ),
        "observed_month_coverage": (
            "PASS" if months == expected_months else "FAIL"
        ),
        "absence_of_unobserved_history_gaps": "NOT_PROVEN",
        "observed_history_detail": {
            "gaps_over_14_days": gaps,
            "observed_months": months,
            "expected_months_between_first_and_last": expected_months,
            "note": (
                "成功请求只证明本次公开接口请求完整返回；"
                "不证明 API 从未遗漏或删除历史数据。"
            ),
        },
        "beijing_event_count": len(events),
        "beijing_market_count": len({row["condition_id"] for row in fills}),
        "beijing_outcome_token_count": len({row["asset"] for row in fills}),
        "public_buy_fill_count": len(buys),
        "public_sell_fill_count": len(sells),
        "total_public_fill_count": len(fills),
        "unique_transaction_hash_count": len({row["transaction_hash"] for row in fills}),
        "buy_transaction_hash_count": len({row["transaction_hash"] for row in buys}),
        "sell_transaction_hash_count": len({row["transaction_hash"] for row in sells}),
        "total_buy_usd": sum(row["trade_usd"] for row in buys),
        "total_sell_usd": sum(row["trade_usd"] for row in sells),
        "total_buy_shares": sum(row["shares"] for row in buys),
        "total_sell_shares": sum(row["shares"] for row in sells),
        "median_buy_usd_per_event": statistics.median(event_buy_values),
        "mean_buy_usd_per_event": statistics.mean(event_buy_values),
        "max_buy_usd_event": max(events, key=lambda event: event["total_buy_usd"])["event_key"],
        "min_buy_usd_event": min(events, key=lambda event: event["total_buy_usd"])["event_key"],
        "single_buy_event_count": sum(event["buy_fill_count"] == 1 for event in events),
        "multi_buy_event_count": sum(event["buy_fill_count"] > 1 for event in events),
        "single_bucket_event_count": sum(event["outcome_token_count"] == 1 for event in events),
        "multi_bucket_event_count": sum(event["outcome_token_count"] > 1 for event in events),
        "events_with_recorded_sell": sum(event["sell_fill_count"] > 0 for event in events),
        "events_with_no_recorded_sell": sum(event["sell_fill_count"] == 0 for event in events),
        "events_with_confirmed_settlement_path": sum(event["confirmed_settlement_path"] for event in events),
        "events_with_unresolved_path": sum(not event["confirmed_settlement_path"] for event in events),
        "events_held_to_settlement_observed": sum(
            event["final_path_classification"] == "HOLD_TO_SETTLEMENT_OBSERVED"
            for event in events
        ),
        "no_recorded_sell_final_path_unknown_count": sum(
            event["final_path_classification"]
            == "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN"
            for event in events
        ),
        "partial_exit_final_path_unknown_count": sum(
            event["final_path_classification"] == "PARTIAL_EXIT_FINAL_PATH_UNKNOWN"
            for event in events
        ),
        "full_recorded_exit_count": sum(
            event["final_path_classification"] == "FULL_RECORDED_EXIT"
            for event in events
        ),
        "entry_timeline_complete_event_count": len(complete_events),
        "entry_timeline_partial_event_count": len(events) - len(complete_events),
        "d_minus_1_buy_usd_share": d_minus_1 / total_buy_usd if total_buy_usd else None,
        "d0_buy_usd_share": d0 / total_buy_usd if total_buy_usd else None,
        "d0_after_1200_buy_usd_share": after_12 / total_buy_usd if total_buy_usd else None,
        "d0_after_1400_buy_usd_share": after_14 / total_buy_usd if total_buy_usd else None,
        "d0_after_1500_buy_usd_share": after_15 / total_buy_usd if total_buy_usd else None,
        "median_first_buy_time": median_relative_time(events, "first_observed_buy_time_cst"),
        "median_build_25_time": median_relative_time(events, "build_25pct_time_cst"),
        "median_build_50_time": median_relative_time(events, "build_50pct_time_cst"),
        "median_build_75_time": median_relative_time(events, "build_75pct_time_cst"),
        "median_last_buy_time": median_relative_time(events, "last_buy_time_cst"),
        "median_first_sell_time": median_relative_time(events, "first_sell_time_cst"),
        "median_last_sell_time": median_relative_time(events, "last_sell_time_cst"),
        "entry_timeline_complete_only_medians": {
            "median_first_buy_time": median_relative_time(complete_events, "first_observed_buy_time_cst"),
            "median_build_25_time": median_relative_time(complete_events, "build_25pct_time_cst"),
            "median_build_50_time": median_relative_time(complete_events, "build_50pct_time_cst"),
            "median_build_75_time": median_relative_time(complete_events, "build_75pct_time_cst"),
            "median_last_buy_time": median_relative_time(complete_events, "last_buy_time_cst"),
        },
        "price_add_counts": dict(adds),
        "average_cost_add_counts": dict(average_adds),
        "adjacent_basket_event_count": sum(bool(event["adjacent_bucket_pairs"]) for event in events),
        "bucket_rotation_event_count": sum(event["bucket_rotation"] for event in events),
        "strict_closed_settled_event_count": len(strict),
        "beijing_total_pnl_strict": total_strict_pnl,
        "strict_total_buy_usd": total_strict_buy,
        "strict_roi": total_strict_pnl / total_strict_buy if total_strict_buy else None,
        "observable_but_incomplete_event_count": sum(
            event.get("strict_pnl") is None for event in events
        ),
        "observable_but_incomplete_pnl": sum(
            event.get("observable_but_incomplete_pnl") or 0 for event in events
        ),
        "active_open_event_count": sum(
            event["position_status"] == "ACTIVE_OPEN_CONFIRMED" for event in events
        ),
        "active_open_mark_to_market_pnl": sum(
            event.get("active_open_mark_to_market_pnl") or 0 for event in events
        ),
        "resolved_redeemable_event_count": sum(
            event["position_status"] == "RESOLVED_REDEEMABLE_UNREDEEMED"
            for event in events
        ),
        "resolved_redeemable_snapshot_pnl": sum(
            event.get("resolved_redeemable_snapshot_pnl") or 0 for event in events
        ),
        "past_enddate_status_unknown_event_count": sum(
            event["position_status"] == "PAST_ENDDATE_STATUS_UNKNOWN"
            for event in events
        ),
        "past_enddate_status_unknown_snapshot_pnl": sum(
            event.get("past_enddate_status_unknown_snapshot_pnl") or 0
            for event in events
        ),
        "position_status_unknown_event_count": sum(
            event["position_status"] == "POSITION_STATUS_UNKNOWN"
            for event in events
        ),
        "position_snapshot_at_utc": manifest.get("generated_at_utc"),
        "resolved_pnl_reconciliation": resolved_pnl_reconciliation,
        "unresolved_event_count": sum(event.get("strict_pnl") is None for event in events),
        "unresolved_buy_usd": sum(event["total_buy_usd"] for event in events if event.get("strict_pnl") is None),
        "beijing_total_pnl_with_active_open_mark_to_market": (
            total_strict_pnl
            + sum(event.get("active_open_mark_to_market_pnl") or 0 for event in events)
        ),
        "profit_event_count": sum(value > 0 for value, _ in strict_pnls),
        "loss_event_count": sum(value < 0 for value, _ in strict_pnls),
        "break_even_event_count": sum(value == 0 for value, _ in strict_pnls),
        "win_rate": sum(value > 0 for value, _ in strict_pnls) / len(strict_pnls) if strict_pnls else None,
        "median_event_pnl": statistics.median([value for value, _ in strict_pnls]) if strict_pnls else None,
        "max_profit_event": strict_pnls[0] if strict_pnls else None,
        "max_loss_event": strict_pnls[-1] if strict_pnls else None,
        "pnl_without_top_1": total_strict_pnl - sum(value for value, _ in positive[:1]),
        "pnl_without_top_5": total_strict_pnl - sum(value for value, _ in positive[:5]),
        "top_winner_share": positive[0][0] / total_positive if positive and total_positive else None,
        "max_consecutive_losses": max_consecutive,
        "max_weather_date_drawdown": max_drawdown,
        "checkpoint_results": checkpoint,
        "profit_loss_timing_comparison": timing_comparison(events),
        "partial_exit_observed_count": sum(
            "PARTIAL_EXIT_OBSERVED" in event["exit_classifications"]
            for event in events
        ),
        "profitable_partial_sell_observed_count": sum(
            "PROFITABLE_PARTIAL_SELL_OBSERVED"
            in archetype_labels(
                event,
                [row for row in fills if row["event_key"] == event["event_key"]],
            )
            for event in events
        ),
        "loss_realizing_partial_sell_observed_count": sum(
            "LOSS_REALIZING_PARTIAL_SELL_OBSERVED"
            in archetype_labels(
                event,
                [row for row in fills if row["event_key"] == event["event_key"]],
            )
            for event in events
        ),
        "sell_pnl_method_disagreement_count": sum(
            event.get("sell_pnl_method_disagreement") is True for event in events
        ),
        "sell_pnl_unavailable_count": sum(
            event.get("sell_pnl_status") == "SELL_PNL_UNAVAILABLE"
            for event in events
        ),
        "path_label_mutual_exclusion": (
            "PASS"
            if all(
                event.get("final_path_classification") in {
                    "HOLD_TO_SETTLEMENT_OBSERVED",
                    "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN",
                    "PARTIAL_EXIT_FINAL_PATH_UNKNOWN",
                    "FULL_RECORDED_EXIT",
                }
                for event in events
            )
            else "FAIL"
        ),
        "merge_data_quality": merge_meta,
        "public_data_only": True,
        "public_get_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "formal_started": False,
    }


def _render_report_legacy(summary: dict[str, Any], events: list[dict[str, Any]]) -> str:
    top_wins = sorted(
        [event for event in events if event.get("strict_pnl") is not None],
        key=lambda event: event["strict_pnl"],
        reverse=True,
    )[:5]
    top_losses = sorted(
        [event for event in events if event.get("strict_pnl") is not None],
        key=lambda event: event["strict_pnl"],
    )[:5]
    earliest = sorted(events, key=lambda event: event["weather_date"])[:5]
    latest = sorted(events, key=lambda event: event["weather_date"], reverse=True)[:5]
    d1_examples = [
        event for event in events
        if (event.get("d_minus_1_buy_usd_share") or 0) > 0
    ][:3]
    midday_examples = [
        event for event in events
        if any(key in event.get("phase_buy_usd", {}) for key in ("D0_1100_1200", "D0_1200_1300", "D0_1300_1400"))
    ][:3]
    afternoon_examples = [
        event for event in events
        if any(key in event.get("phase_buy_usd", {}) for key in ("D0_1400_1500", "D0_1500_1600", "D0_1600_1800"))
    ][:3]
    adjacent_examples = [event for event in events if event["adjacent_bucket_pairs"]][:3]
    quick_examples = [
        event for event in events if "QUICK_EXIT_WITHIN_1H" in event["exit_classifications"]
    ][:3]
    checkpoints = {
        row["checkpoint"]: row
        for row in summary["checkpoint_results"]
        if row["scope"] == "ALL_OBSERVABLE_EVENTS"
    }
    profit_timing = summary["profit_loss_timing_comparison"]["profit_events"]
    loss_timing = summary["profit_loss_timing_comparison"]["loss_events"]
    archetype_counts = {
        row["archetype"]: row["event_count"] for row in summary.get("strategy_archetypes", [])
    }
    dominant_entry = sorted(
        (
            (label, archetype_counts.get(label, 0))
            for label in (
                "D1_EARLY_POSITION", "D1_TEST_D0_SCALE", "D0_MORNING_ENTRY",
                "D0_WARMING_ENTRY", "D0_LATE_ENTRY", "ONE_SHOT_ENTRY",
                "GRADUAL_ACCUMULATION",
            )
        ),
        key=lambda item: (-item[1], item[0]),
    )[:3]
    dominant_exit = sorted(
        (
            (label, archetype_counts.get(label, 0))
            for label in (
                "QUICK_FLIP", "PROFITABLE_PARTIAL_SELL_OBSERVED",
                "LOSS_REALIZING_PARTIAL_SELL_OBSERVED",
                "HOLD_TO_SETTLEMENT_OBSERVED",
                "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN",
                "PARTIAL_EXIT_FINAL_PATH_UNKNOWN",
            )
        ),
        key=lambda item: (-item[1], item[0]),
    )[:3]

    def event_table(rows: list[dict[str, Any]]) -> list[str]:
        output = ["| 天气日 | 投入USD | 严格PnL | BUY/SELL公开成交笔数 | 路径状态 |", "|---|---:|---:|---:|---|"]
        for row in rows:
            pnl = "—" if row.get("strict_pnl") is None else f"{row['strict_pnl']:.2f}"
            output.append(
                f"| {row['weather_date']} | {row['total_buy_usd']:.2f} | {pnl} | "
                f"{row['buy_fill_count']}/{row['sell_fill_count']} | {row['entry_timeline_status']} |"
            )
        return output

    return "\n".join([
        "# Husky 北京最高温市场全量公开交易深度分析 v1",
        "",
        "## 技术摘要",
        "",
        f"截至 `{summary['analysis_cutoff_utc']}`，合并仓库快照与本次公开 GET 后，"
        f"观察到北京最高温市场 {summary['beijing_event_count']} 个天气事件、"
        f"{summary['public_buy_fill_count']} 笔 BUY 与 {summary['public_sell_fill_count']} 笔 SELL 公开成交。"
        f"总 BUY 金额 ${summary['total_buy_usd']:.2f}。严格关闭/结算口径覆盖 "
        f"{summary['strict_closed_settled_event_count']} 个事件，PnL ${summary['beijing_total_pnl_strict']:.2f}，"
        f"ROI {summary['strict_roi']:.2%}。",
        "",
        "**重要定义：上述均为 public trade fill count（公开成交笔数），不是 original order count（原始订单数）。**",
        "",
        "## 22 个核心问题的大白话答案",
        "",
        f"1. 最早观察到北京公开成交是 {summary['beijing_first_observed_public_trade_cst']}；这不是绝对第一张原始订单。",
        f"2. 最新一笔是 {summary['beijing_last_observed_public_trade_cst']}。",
        f"3. activity 请求成功覆盖到冻结截止时间 {summary['analysis_cutoff_utc']}；最后成交早于截止时间不等于抓取缺口。",
        f"4. 北京共有 {summary['beijing_event_count']} 个最高温天气事件。",
        f"5. BUY / SELL 分别有 {summary['public_buy_fill_count']} / {summary['public_sell_fill_count']} 笔公开成交。",
        f"6. 可观察总投入为 ${summary['total_buy_usd']:.2f}。",
        f"7. 严格已关闭/结算总 PnL 为 ${summary['beijing_total_pnl_strict']:.2f}，严格 ROI {summary['strict_roi']:.2%}。",
        f"8. 未进入严格口径的事件 {summary['unresolved_event_count']} 个、BUY 金额 ${summary['unresolved_buy_usd']:.2f}。",
        f"9. 资金上以 D0 为主：D-1及更早 {summary['d_minus_1_buy_usd_share']:.1%}，D0 {summary['d0_buy_usd_share']:.1%}。",
        f"10. 全部事件的 50% 建仓中位时点是 {summary['median_build_50_time']}；完整路径事件为 {summary['entry_timeline_complete_only_medians']['median_build_50_time']}。",
        f"11. 多次 BUY 事件 {summary['multi_buy_event_count']} 个，单次 BUY {summary['single_buy_event_count']} 个，整体更常见分批建仓。",
        f"12. 同档后续 BUY 中，上涨/下跌/持平为 {summary['price_add_counts'].get('PRICE_UP_ADD', 0)} / {summary['price_add_counts'].get('PRICE_DOWN_ADD', 0)} / {summary['price_add_counts'].get('PRICE_FLAT_ADD', 0)} 笔。",
        f"13. 单档/多档事件 {summary['single_bucket_event_count']} / {summary['multi_bucket_event_count']}；多档更常见。",
        f"14. 相邻档篮子 {summary['adjacent_basket_event_count']} 个；逐事件加入时间见 event summary。",
        f"15. 首次 SELL 中位相对时点为 {summary['median_first_sell_time']}。",
        f"16. PROFITABLE_PARTIAL_SELL_OBSERVED / LOSS_REALIZING_PARTIAL_SELL_OBSERVED "
        f"研究标签事件为 {archetype_counts.get('PROFITABLE_PARTIAL_SELL_OBSERVED', 0)} / "
        f"{archetype_counts.get('LOSS_REALIZING_PARTIAL_SELL_OBSERVED', 0)}。",
        f"17. 明确观察到持有到结算的事件 {summary['events_held_to_settlement_observed']} 个。",
        f"18. 严格盈利事件中相邻篮子占 {profit_timing['adjacent_basket_event_share']:.1%}，初始投入中位占比 {profit_timing['median_initial_buy_share']:.1%}。",
        f"19. 严格亏损事件中相邻篮子占 {loss_timing['adjacent_basket_event_share']:.1%}，初始投入中位占比 {loss_timing['median_initial_buy_share']:.1%}；只报告关联。",
        f"20. 最值得继续验证 D0 10:00—16:00，尤其 14:00（此前投入 {checkpoints['D0_1400']['buy_usd_share_before']:.1%}）和 15:00（{checkpoints['D0_1500']['buy_usd_share_before']:.1%}）。",
        "21. 与中国正常工作时间最容易配合的是 D0 10:00、12:00、13:00、14:00、15:00、16:00；D-1 15:00/18:00 也可操作。",
        "22. 公开数据仍不能证明原始挂单/撤单、主观预测档、交易因果、无 SELL 必然持有结算，或北京结算站就是 ZBAA。",
        "",
        "## 可观察历史从何时开始、覆盖到何时",
        "",
        f"- 第一笔可观察北京公开成交：{summary['beijing_first_observed_public_trade_utc']} / "
        f"{summary['beijing_first_observed_public_trade_cst']}。",
        f"- 最新一笔可观察北京公开成交：{summary['beijing_last_observed_public_trade_utc']} / "
        f"{summary['beijing_last_observed_public_trade_cst']}。",
        f"- 北京天气日范围：{summary['beijing_first_weather_date']} 至 {summary['beijing_last_weather_date']}。",
        f"- 最早/最新历史信心：{summary['earliest_history_confidence']} / {summary['latest_history_confidence']}。",
        "- OBSERVED：公开 profile 与仓库四类文件中的 proxyWallet 均一致。",
        "- NOT_SUPPORTED：公开成交无法证明 Husky 的绝对第一笔原始订单、未成交挂单或撤单。",
        "",
        "## 建仓通常发生在何时",
        "",
        f"D-1 及更早占 BUY 金额 {summary['d_minus_1_buy_usd_share']:.1%}；"
        f"D0 占 {summary['d0_buy_usd_share']:.1%}。D0 12:00 / 14:00 / 15:00 后分别占 "
        f"{summary['d0_after_1200_buy_usd_share']:.1%} / "
        f"{summary['d0_after_1400_buy_usd_share']:.1%} / "
        f"{summary['d0_after_1500_buy_usd_share']:.1%}。"
        f"首次、25%、50%、75%和最后 BUY 的北京时间中位时钟分别为 "
        f"{summary['median_first_buy_time']}、{summary['median_build_25_time']}、"
        f"{summary['median_build_50_time']}、{summary['median_build_75_time']}、"
        f"{summary['median_last_buy_time']}。",
        f"仅保留完整路径事件后，对应中位时钟为 "
        f"{summary['entry_timeline_complete_only_medians']['median_first_buy_time']}、"
        f"{summary['entry_timeline_complete_only_medians']['median_build_25_time']}、"
        f"{summary['entry_timeline_complete_only_medians']['median_build_50_time']}、"
        f"{summary['entry_timeline_complete_only_medians']['median_build_75_time']}、"
        f"{summary['entry_timeline_complete_only_medians']['median_last_buy_time']}。",
        "",
        "这些是描述性关联，不证明某个时点导致盈利。",
        "",
        "## 盈利与亏损事件的可观察关联",
        "",
        f"严格盈利事件 {profit_timing['event_count']} 个：首次/50% 建仓中位相对时点 "
        f"{profit_timing['median_first_buy_time']} / {profit_timing['median_build_50_time']}，"
        f"初始投入占比中位数 {profit_timing['median_initial_buy_share']:.1%}，"
        f"投入中位数 ${profit_timing['median_total_buy_usd']:.2f}，"
        f"D-1 投入占比中位数 {profit_timing['median_d_minus_1_buy_usd_share']:.1%}，"
        f"相邻篮子事件占 {profit_timing['adjacent_basket_event_share']:.1%}。",
        "",
        f"严格亏损事件 {loss_timing['event_count']} 个：首次/50% 建仓中位相对时点 "
        f"{loss_timing['median_first_buy_time']} / {loss_timing['median_build_50_time']}，"
        f"初始投入占比中位数 {loss_timing['median_initial_buy_share']:.1%}，"
        f"投入中位数 ${loss_timing['median_total_buy_usd']:.2f}，"
        f"D-1 投入占比中位数 {loss_timing['median_d_minus_1_buy_usd_share']:.1%}，"
        f"相邻篮子事件占 {loss_timing['adjacent_basket_event_share']:.1%}。",
        "",
        "这些差异来自非随机的观察性数据，只能作为后续模型假设，不能写成“某时段买入所以赚钱”。",
        "",
        "## 分批、补仓与温度篮子",
        "",
        f"单次/多次 BUY 事件为 {summary['single_buy_event_count']} / "
        f"{summary['multi_buy_event_count']}；单档/多档为 "
        f"{summary['single_bucket_event_count']} / {summary['multi_bucket_event_count']}。"
        f"价格上涨/下跌/持平后的同档补仓为 "
        f"{summary['price_add_counts'].get('PRICE_UP_ADD', 0)} / "
        f"{summary['price_add_counts'].get('PRICE_DOWN_ADD', 0)} / "
        f"{summary['price_add_counts'].get('PRICE_FLAT_ADD', 0)} 笔。"
        f"相邻档篮子 {summary['adjacent_basket_event_count']} 个，投入主导档发生切换 "
        f"{summary['bucket_rotation_event_count']} 个。",
        "",
        "dominant_bought_bucket 只表示投入金额最高档，不表示 Husky 主观预测主档。",
        "",
        "## 卖出、关闭与未解决路径",
        "",
        f"有记录 SELL 的事件 {summary['events_with_recorded_sell']} 个，无记录 SELL 的事件 "
        f"{summary['events_with_no_recorded_sell']} 个。首次/最后 SELL 的北京时间中位时钟为 "
        f"{summary['median_first_sell_time']} / {summary['median_last_sell_time']}。"
        f"尚未进入严格口径的事件 {summary['unresolved_event_count']} 个，对应可观察 BUY 金额 "
        f"${summary['unresolved_buy_usd']:.2f}。无 SELL 不自动等于持有到结算。",
        f"具有明确结算路径且可标记 `HELD_TO_SETTLEMENT_OBSERVED` 的事件为 "
        f"{summary['events_held_to_settlement_observed']} 个。",
        "",
        "## 严格盈亏与集中度",
        "",
        f"严格总 PnL ${summary['beijing_total_pnl_strict']:.2f}；严格投入 "
        f"${summary['strict_total_buy_usd']:.2f}；ROI {summary['strict_roi']:.2%}。"
        f"盈利/亏损/打平事件 {summary['profit_event_count']} / {summary['loss_event_count']} / "
        f"{summary['break_even_event_count']}，胜率 {summary['win_rate']:.1%}。"
        f"去掉最大 1/5 个赢家后 PnL 为 ${summary['pnl_without_top_1']:.2f} / "
        f"${summary['pnl_without_top_5']:.2f}；最大连续亏损 {summary['max_consecutive_losses']} 个事件，"
        f"天气日序列最大回撤 ${summary['max_weather_date_drawdown']:.2f}。",
        "",
        "### 最大盈利 5 个事件",
        "",
        *event_table(top_wins),
        "",
        "### 最大亏损 5 个事件",
        "",
        *event_table(top_losses),
        "",
        "### 最早 5 个北京事件",
        "",
        *event_table(earliest),
        "",
        "### 最新 5 个北京事件",
        "",
        *event_table(latest),
        "",
        "### 典型 D-1 建仓事件",
        "",
        *event_table(d1_examples),
        "",
        "### 典型 D0 中午建仓事件",
        "",
        *event_table(midday_examples),
        "",
        "### 典型 D0 下午建仓事件",
        "",
        *event_table(afternoon_examples),
        "",
        "### 典型相邻档篮子事件",
        "",
        *event_table(adjacent_examples),
        "",
        "### 典型快速买卖事件",
        "",
        *event_table(quick_examples),
        "",
        "## 候选预测与检查时点",
        "",
        "候选时点必须同时看截止前资金、首次建仓覆盖、各累计阈值和之后继续加仓事件数；"
        "完整数值见 `beijing_candidate_checkpoints.csv`。当前优先继续验证中国正常工作时间内的 "
        "D0 10:00、12:00、13:00、14:00、15:00、16:00，以及可自动化采集的 D-1 15:00/18:00。",
        f"可观察主导建仓标签为 {dominant_entry}；主导退出/最终路径标签为 {dominant_exit}。"
        "这些都是本研究标签，不是 Husky 公布的规则。",
        "",
        "## 数据、口径与方法",
        "",
        "- 纳入条件同时检查 city、title、eventSlug、slug 和 weather_metric；只保留 Beijing + highest temperature。",
        "- 事件单位为北京天气日；温度档不是独立事件样本。",
        "- 去重键为 timestamp + transactionHash + conditionId + asset + side + price + size。",
        "- trades 记录提供成交份额；activity 的 usdcSize 优先作为成交金额，并保留 EXACT/NEAREST/NO_ACTIVITY_MATCH。",
        "- 公开 activity 从 2020-01-01 起按 30 天窗口、ASC 顺序完整分页；饱和窗口递归拆分。",
        "- 盈亏优先使用验证的事件级 city_day_pnl 或公开 closed positions；逐笔 SELL 成本法不重复加入最终 PnL。",
        "- 所有显示时间使用 Asia/Shanghai，并保留 UTC。",
        "",
        "## 局限、不确定性与稳健性检查",
        "",
        "- OBSERVED：公开成交、公开仓位、公开 profile 与官方市场元数据字段。",
        "- INFERRED：建仓类型、补仓类型、篮子类型和策略 archetype 均为本研究确定性分类。",
        "- NOT_SUPPORTED：原始挂单/撤单、Husky 主观预测、因果盈利解释、北京结算站为 ZBAA。",
        "- UNKNOWN：部分无 SELL 或仍在 positions 中的仓位最终路径。",
        f"- 完整建仓路径 {summary['entry_timeline_complete_event_count']} 个；部分/未知 "
        f"{summary['entry_timeline_partial_event_count']} 个。完整路径口径与全部可观察口径在候选时点表中分开。",
        "",
        "## 下一步",
        "",
        "优先建立 D0 10:00—16:00 的逐小时预测快照，并保留 D-1 15:00/18:00 作为早期基线；"
        "后续验证应以事件级严格 PnL 为结果变量，同时控制路径完整性与最大赢家集中度。",
        "",
    ])


def render_report(summary: dict[str, Any], events: list[dict[str, Any]]) -> str:
    complete_timing = summary["profit_loss_timing_comparison"][
        "STRICT_PNL_ENTRY_COMPLETE_ONLY"
    ]
    all_timing = summary["profit_loss_timing_comparison"]["STRICT_PNL_ALL"]
    complete_profit = complete_timing["profit_events"]
    complete_loss = complete_timing["loss_events"]
    all_profit = all_timing["profit_events"]
    all_loss = all_timing["loss_events"]
    reconciliation = summary["resolved_pnl_reconciliation"]
    checkpoint_names = (
        "D1_1500", "D0_1000", "D0_1200", "D0_1300",
        "D0_1400", "D0_1500", "D0_1600",
    )
    checkpoints = {
        row["checkpoint"]: row
        for row in summary["checkpoint_results"]
        if row["scope"] == "ENTRY_TIMELINE_COMPLETE_ONLY"
    }

    def percent(value: Any) -> str:
        return "—" if value is None else f"{float(value):.1%}"

    def money(value: Any) -> str:
        return "—" if value is None else f"${float(value):.2f}"

    def timing_row(label: str, metrics: dict[str, Any]) -> str:
        return (
            f"| {label} | {metrics['event_count']} | {money(metrics['total_buy_usd'])} | "
            f"{metrics['median_first_buy_time'] or '—'} | "
            f"{metrics['median_build_25_time'] or '—'} | "
            f"{metrics['median_build_50_time'] or '—'} | "
            f"{metrics['median_build_75_time'] or '—'} | "
            f"{metrics['median_last_buy_time'] or '—'} | "
            f"{percent(metrics['median_initial_buy_share'])} | "
            f"{percent(metrics['median_d_minus_1_buy_usd_share'])} | "
            f"{percent(metrics['median_d0_buy_usd_share'])} | "
            f"{percent(metrics['median_d0_after_1200_buy_usd_share'])} | "
            f"{percent(metrics['median_d0_after_1400_buy_usd_share'])} | "
            f"{percent(metrics['median_d0_after_1500_buy_usd_share'])} |"
        )

    profitable_partial = [
        event["weather_date"]
        for event in events
        if "PROFITABLE_PARTIAL_SELL_OBSERVED"
        in event.get("strategy_archetypes", [])
    ]
    loss_partial = [
        event["weather_date"]
        for event in events
        if "LOSS_REALIZING_PARTIAL_SELL_OBSERVED"
        in event.get("strategy_archetypes", [])
    ]
    top_strict = sorted(
        [event for event in events if event.get("strict_pnl") is not None],
        key=lambda event: event["strict_pnl"],
        reverse=True,
    )

    lines = [
        "# Husky 北京最高温市场全量公开交易深度分析 v1",
        "",
        "## 技术摘要",
        "",
        f"截至 `{summary['analysis_cutoff_utc']}`，北京最高温公开历史仍为 "
        f"{summary['beijing_event_count']} 个天气事件、"
        f"{summary['total_public_fill_count']} 笔公开成交（"
        f"{summary['public_buy_fill_count']} BUY / {summary['public_sell_fill_count']} SELL）。"
        f"严格已关闭/结算口径覆盖 {summary['strict_closed_settled_event_count']} 个事件，"
        f"总 PnL {money(summary['beijing_total_pnl_strict'])}。",
        "",
        f"本轮最重要修正是：`ACTIVE_OPEN_CONFIRMED` 只有 "
        f"{summary['active_open_event_count']} 个；原先混在“当前开放”里的 "
        f"{summary['resolved_redeemable_event_count']} 个事件实际是已过 endDate、"
        f"`redeemable=true` 的待赎回仓位。其隔离快照为 "
        f"{money(summary['resolved_redeemable_snapshot_pnl'])}，但由于权威资产重叠数为 "
        f"{reconciliation['comparable_asset_count']}，验证结果是 "
        f"`{reconciliation['validation_result']}`，不得并入严格 PnL。",
        "",
        "**公开成交笔数不是原始订单数；公开接口不展示未成交挂单或撤单。**",
        "",
        "## 16 个审核问题的大白话答案",
        "",
        f"1. 当前可观察北京历史从 {summary['beijing_first_observed_public_trade_cst']} 开始；"
        f"`ABSOLUTE_LIFETIME_FIRST_BEIJING_TRADE={summary['absolute_lifetime_first_beijing_trade']}`。",
        f"2. 最新可观察成交为 {summary['beijing_last_observed_public_trade_cst']}，"
        f"冻结点为 {summary['analysis_cutoff_utc']}。",
        f"3. 核心计数维持：{summary['beijing_event_count']} 个事件、"
        f"{summary['total_public_fill_count']} 笔公开成交。",
        f"4. D0 BUY 金额占比为 {summary['d0_buy_usd_share']:.1%}；"
        "“约 95% 在 D0 买入”的描述性结论维持。",
        f"5. 完整路径事件的首次/25%/50%/75%/最后建仓中位时点为 "
        f"{summary['entry_timeline_complete_only_medians']['median_first_buy_time']} / "
        f"{summary['entry_timeline_complete_only_medians']['median_build_25_time']} / "
        f"{summary['entry_timeline_complete_only_medians']['median_build_50_time']} / "
        f"{summary['entry_timeline_complete_only_medians']['median_build_75_time']} / "
        f"{summary['entry_timeline_complete_only_medians']['median_last_buy_time']}。",
        f"6. 真正 ACTIVE_OPEN 为 {summary['active_open_event_count']} 个，"
        f"活动仓位 MTM {money(summary['active_open_mark_to_market_pnl'])}。",
        f"7. 已结算但未赎回为 {summary['resolved_redeemable_event_count']} 个，"
        f"隔离快照 {money(summary['resolved_redeemable_snapshot_pnl'])}。",
        f"8. 已过 endDate 但状态不足为 "
        f"{summary['past_enddate_status_unknown_event_count']} 个；"
        f"其他仓位状态不明为 {summary['position_status_unknown_event_count']} 个。",
        f"9. 严格已结算总 PnL 为 {money(summary['beijing_total_pnl_strict'])}，"
        f"覆盖 {summary['strict_closed_settled_event_count']} 个事件。",
        f"10. resolved-unredeemed PnL 不能验证："
        f"`{reconciliation['validation_result']}`，且不进入严格 PnL。",
        f"11. 两种成本法都确认盈利的部分 SELL 有 "
        f"{summary['profitable_partial_sell_observed_count']} 个事件："
        f"{', '.join(profitable_partial) if profitable_partial else '无'}。",
        f"12. 两种成本法都确认亏损的部分 SELL 有 "
        f"{summary['loss_realizing_partial_sell_observed_count']} 个事件："
        f"{', '.join(loss_partial) if loss_partial else '无'}。",
        f"13. 权威结算路径明确、归为持有到结算的事件有 "
        f"{summary['events_held_to_settlement_observed']} 个。",
        f"14. 最终路径未知分为：无 SELL "
        f"{summary['no_recorded_sell_final_path_unknown_count']} 个、"
        f"部分 SELL 后余量未知 {summary['partial_exit_final_path_unknown_count']} 个。",
        f"15. 盈亏完整路径事件的 50% 建仓中位时点分别为 "
        f"{complete_profit['median_build_50_time']} / "
        f"{complete_loss['median_build_50_time']}；只表示观察性差异。",
        "16. 最值得后续验证的是完整路径口径下的 D-1 15:00 与 "
        "D0 10:00、12:00、13:00、14:00、15:00、16:00；"
        "当前仍为 `INSUFFICIENT_FOR_FINAL_MODEL_SELECTION`。",
        "",
        "## 可观察历史有高请求覆盖，但绝对生命周期起点未被证明",
        "",
        f"- `BEIJING_FIRST_OBSERVED_PUBLIC_TRADE={summary['beijing_first_observed_public_trade_cst']}`",
        f"- `EARLIEST_OBSERVED_CURRENT_API_HISTORY_CONFIDENCE="
        f"{summary['earliest_observed_current_api_history_confidence']}`",
        f"- `ABSOLUTE_LIFETIME_FIRST_BEIJING_TRADE="
        f"{summary['absolute_lifetime_first_beijing_trade']}`",
        f"- `PUBLIC_REQUEST_COVERAGE={summary['public_request_coverage']}`",
        f"- `OBSERVED_MONTH_COVERAGE={summary['observed_month_coverage']}`",
        f"- `ABSENCE_OF_UNOBSERVED_HISTORY_GAPS="
        f"{summary['absence_of_unobserved_history_gaps']}`",
        "",
        "请求全部成功只证明本次公开接口请求完整返回，不能证明 API 从未遗漏、"
        "删除或截断过更早历史。",
        "",
        "## 当前仓位被拆成互斥状态",
        "",
        "| 事件状态 | 事件数 | 隔离 PnL | 是否进入严格 PnL |",
        "|---|---:|---:|---|",
        f"| ACTIVE_OPEN_CONFIRMED | {summary['active_open_event_count']} | "
        f"{money(summary['active_open_mark_to_market_pnl'])} | 否 |",
        f"| RESOLVED_REDEEMABLE_UNREDEEMED | "
        f"{summary['resolved_redeemable_event_count']} | "
        f"{money(summary['resolved_redeemable_snapshot_pnl'])} | 否 |",
        f"| PAST_ENDDATE_STATUS_UNKNOWN | "
        f"{summary['past_enddate_status_unknown_event_count']} | "
        f"{money(summary['past_enddate_status_unknown_snapshot_pnl'])} | 否 |",
        f"| POSITION_STATUS_UNKNOWN | {summary['position_status_unknown_event_count']} | — | 否 |",
        f"| CLOSED_POSITION_CONFIRMED | {summary['strict_closed_settled_event_count']} | "
        f"{money(summary['beijing_total_pnl_strict'])} | 是 |",
        "",
        "resolved 快照采用 `cashPnl + realizedPnl`，仅用于隔离观察。四个候选公式的"
        "权威重叠校验如下；没有重叠时“最稳定公式”必须保持未确定。",
        "",
        "| 公式 | 可比资产数 | 精确匹配率 | 误差<0.01比例 | 最大绝对误差 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in reconciliation["formulas"].items():
        lines.append(
            f"| {name} | {metrics['comparable_asset_count']} | "
            f"{percent(metrics['exact_match_rate'])} | "
            f"{percent(metrics['within_0_01_rate'])} | "
            f"{money(metrics['max_absolute_error'])} |"
        )
    lines.extend([
        "",
        f"`MOST_STABLE_FORMULA={reconciliation['most_stable_formula']}`；"
        f"`SNAPSHOT_FORMULA_STATUS={reconciliation['snapshot_formula_status']}`。",
        "",
        "## SELL 标签只依据已记录 SELL 的实现盈亏",
        "",
        f"观察到部分退出 {summary['partial_exit_observed_count']} 个事件；"
        f"盈利部分 SELL {summary['profitable_partial_sell_observed_count']} 个，"
        f"亏损部分 SELL {summary['loss_realizing_partial_sell_observed_count']} 个，"
        f"成本法明显不一致 {summary['sell_pnl_method_disagreement_count']} 个，"
        f"成本路径不足 {summary['sell_pnl_unavailable_count']} 个。",
        "",
        "FIFO 与平均成本法都为正，才标记 `PROFITABLE_PARTIAL_SELL_OBSERVED`；"
        "都为负，才标记 `LOSS_REALIZING_PARTIAL_SELL_OBSERVED`。"
        "最终事件盈利或亏损本身不会生成部分止盈/止损标签。",
        "",
        "最终路径只有一个标签：",
        "",
        f"- `HOLD_TO_SETTLEMENT_OBSERVED`: "
        f"{summary['events_held_to_settlement_observed']}",
        f"- `NO_RECORDED_SELL_FINAL_PATH_UNKNOWN`: "
        f"{summary['no_recorded_sell_final_path_unknown_count']}",
        f"- `PARTIAL_EXIT_FINAL_PATH_UNKNOWN`: "
        f"{summary['partial_exit_final_path_unknown_count']}",
        f"- `FULL_RECORDED_EXIT`: {summary['full_recorded_exit_count']}",
        f"- `PATH_LABEL_MUTUAL_EXCLUSION={summary['path_label_mutual_exclusion']}`",
        "",
        "## 盈利/亏损时间比较以完整建仓路径为主",
        "",
        "主口径 `STRICT_PNL_ENTRY_COMPLETE_ONLY`：",
        "",
        "| 结果 | 事件数 | 总BUY | 首次 | 25% | 50% | 75% | 最后 | 初始占比 | D-1占比 | D0占比 | D0 12后 | D0 14后 | D0 15后 |",
        "|---|---:|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
        timing_row("盈利", complete_profit),
        timing_row("亏损", complete_loss),
        "",
        f"完整盈利/亏损的建仓时长中位数为 "
        f"{complete_profit['median_buy_duration_hours']} / "
        f"{complete_loss['median_buy_duration_hours']} 小时；相邻篮子占比为 "
        f"{percent(complete_profit['adjacent_basket_event_share'])} / "
        f"{percent(complete_loss['adjacent_basket_event_share'])}；"
        f"涨/跌/平价加仓计数分别为 "
        f"{complete_profit['price_up_add_count']}/"
        f"{complete_profit['price_down_add_count']}/"
        f"{complete_profit['price_flat_add_count']} 与 "
        f"{complete_loss['price_up_add_count']}/"
        f"{complete_loss['price_down_add_count']}/"
        f"{complete_loss['price_flat_add_count']}；首次/最后 SELL 中位时点为 "
        f"{complete_profit['median_first_sell_time'] or '—'} / "
        f"{complete_profit['median_last_sell_time'] or '—'}（盈利）和 "
        f"{complete_loss['median_first_sell_time'] or '—'} / "
        f"{complete_loss['median_last_sell_time'] or '—'}（亏损）。",
        "",
        "次要敏感性口径 `STRICT_PNL_ALL`：",
        "",
        f"- 盈利事件 {all_profit['event_count']} 个，50% 建仓中位 "
        f"{all_profit['median_build_50_time']}。",
        f"- 亏损事件 {all_loss['event_count']} 个，50% 建仓中位 "
        f"{all_loss['median_build_50_time']}。",
        "",
        "## 候选预测时点优先使用完整路径事件",
        "",
        "| 时点 | 完整事件数 | 截止前资金 | 截止后资金 | 50%建仓已完成 | 此后仍买入 | 此后才首次买入 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for name in checkpoint_names:
        row = checkpoints[name]
        lines.append(
            f"| {name} | {row['event_count']} | "
            f"{percent(row['buy_usd_share_before'])} | "
            f"{percent(row['buy_usd_share_after'])} | "
            f"{row['build_50_before_event_count']} | "
            f"{row['continues_buying_after_event_count']} | "
            f"{row['first_entry_after_event_count']} |"
        )
    lines.extend([
        "",
        "`INSUFFICIENT_FOR_FINAL_MODEL_SELECTION`：这些时点只进入后续验证，"
        "本报告不冻结正式预测时点。",
        "",
        "## 严格 PnL 保持独立",
        "",
        f"严格口径事件数 {summary['strict_closed_settled_event_count']}，"
        f"总 PnL {money(summary['beijing_total_pnl_strict'])}，"
        f"严格投入 {money(summary['strict_total_buy_usd'])}，"
        f"ROI {summary['strict_roi']:.2%}。resolved、active-open 和状态不明"
        "快照均没有并入该总数。",
        "",
        "| 天气日 | 严格PnL | BUY金额 | 建仓路径 | 最终路径 |",
        "|---|---:|---:|---|---|",
    ])
    for event in top_strict:
        lines.append(
            f"| {event['weather_date']} | {event['strict_pnl']:.2f} | "
            f"{event['total_buy_usd']:.2f} | {event['entry_timeline_status']} | "
            f"{event['final_path_classification']} |"
        )
    lines.extend([
        "",
        "## 口径、限制与下一步",
        "",
        "- 事件单位是北京天气日，温度档不是独立事件样本。",
        "- 当前仓位状态按冻结点与 `endDate`、`redeemable` 和权威关闭证据分类；"
        "只有 `ACTIVE_OPEN_CONFIRMED` 算开放。",
        "- recorded SELL PnL 只为行为标签服务，不重复加入事件最终 PnL。",
        "- SELL 两成本法方向不同，或绝对差异超过 "
        "`max($0.01, 两法较大绝对值的10%)`，即标记方法不一致。",
        "- 完整路径口径是盈利/亏损建仓时间比较与候选预测时点的主口径；"
        "全部严格 PnL 仅作为敏感性结果。",
        "- 公开请求成功不证明绝对历史完整；公开成交也不证明原始挂单、撤单、"
        "主观预测或因果策略。",
        "- 北京结算站仍为 `BEIJING_STATION_UNCONFIRMED`，本轮没有改动 ZBAA。",
        "",
        "下一步只应验证候选快照时点与完整路径事件上的稳健性；在 resolved PnL "
        "获得权威重叠前，不得将其用于总收益、胜率或模型标签。",
        "",
    ])
    return "\n".join(lines)


def analyze(
    repo_root: Path,
    output_root: Path,
    summary_md: Path,
    summary_json: Path,
    *,
    analysis_started_at_utc: str,
    analysis_cutoff_utc: str,
    evidence_manifest: Path,
) -> dict[str, Any]:
    repository_verification = repository_wallet_verification(repo_root)
    manifest, evidence = load_saved_public_evidence(evidence_manifest)
    wallet_verification = profile_wallet_verification(evidence["profile"])
    if wallet_verification != "PASS":
        wallet_verification = repository_verification["status"] if evidence["profile"] else "REPOSITORY_EVIDENCE_ONLY"

    repository_trades, repository_activity = load_repository_beijing(repo_root)
    api_trades, api_activity = prepare_api_beijing(evidence)
    fills, merge_meta = merge_public_fills(
        repository_trades, repository_activity, api_trades, api_activity
    )
    if not fills:
        raise RuntimeError("no Beijing highest-temperature public fills were found")
    annotate_adds(fills)
    lifecycle, current, closed = load_position_evidence(repo_root, evidence)
    fills_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fills:
        fills_by_event[row["event_key"]].append(row)
    events = [
        event_summary(key, rows, lifecycle)
        for key, rows in sorted(fills_by_event.items())
        if any(row["side"] == "BUY" for row in rows)
    ]
    events = attach_pnl(
        events,
        fills_by_event,
        repo_root,
        current,
        closed,
        analysis_cutoff_utc,
    )
    for event in events:
        rows = fills_by_event[event["event_key"]]
        event["price_up_add_count"] = sum(row["price_add_class"] == "PRICE_UP_ADD" for row in rows)
        event["price_down_add_count"] = sum(row["price_add_class"] == "PRICE_DOWN_ADD" for row in rows)
        event["price_flat_add_count"] = sum(row["price_add_class"] == "PRICE_FLAT_ADD" for row in rows)

    observed_assets = {
        row["asset"] for row in fills if row["side"] == "BUY"
    }
    resolved_rows = [
        row
        for asset, row in current.items()
        if asset in observed_assets
        and classify_position_row(row, analysis_cutoff_utc)
        == "RESOLVED_REDEEMABLE_UNREDEEMED"
    ]
    resolved_pnl_reconciliation = reconcile_resolved_pnl_formulas(
        resolved_rows, closed
    )
    summary = summarize(
        fills, events, repository_trades, api_trades,
        analysis_started_at_utc, analysis_cutoff_utc, manifest, merge_meta,
        wallet_verification, resolved_pnl_reconciliation,
    )
    archetypes = build_archetype_rows(events, fills_by_event)
    distributions = distribution_rows(fills, events)
    candidates = candidate_rows(events, fills_by_event)
    completeness = [{
        "event_key": event["event_key"],
        "weather_date": event["weather_date"],
        "entry_timeline_status": event["entry_timeline_status"],
        "entry_timeline_reasons": "|".join(event["entry_timeline_reasons"]),
        "pnl_status": event["pnl_status"],
        "position_status": event["position_status"],
        "pnl_source": event["pnl_source"],
        "final_path_classification": event["final_path_classification"],
        "sell_pnl_status": event["sell_pnl_status"],
        "sell_pnl_method_agreement": event["sell_pnl_method_agreement"],
        "buy_fill_count": event["buy_fill_count"],
        "sell_fill_count": event["sell_fill_count"],
        "activity_exact_match_count": sum(
            row["activity_match_status"] == "EXACT_SIZE_MATCH"
            for row in fills_by_event[event["event_key"]]
        ),
        "activity_nearest_match_count": sum(
            row["activity_match_status"] == "NEAREST_SIZE_MATCH"
            for row in fills_by_event[event["event_key"]]
        ),
        "activity_no_match_count": sum(
            row["activity_match_status"] == "NO_ACTIVITY_MATCH"
            for row in fills_by_event[event["event_key"]]
        ),
    } for event in events]

    output_root.mkdir(parents=True, exist_ok=True)
    write_csv(output_root / "beijing_all_public_fills.csv", fills, FILL_FIELDS)
    write_csv(output_root / "beijing_event_summary.csv", events)
    write_csv(output_root / "beijing_time_distribution.csv", distributions)
    write_csv(output_root / "beijing_candidate_checkpoints.csv", candidates)
    write_csv(output_root / "beijing_pnl_by_event.csv", events)
    write_csv(output_root / "beijing_strategy_archetypes.csv", archetypes)
    write_csv(output_root / "beijing_data_completeness.csv", completeness)
    committed_manifest = dict(manifest)
    committed_manifest["analysis_started_at_utc"] = analysis_started_at_utc
    committed_manifest["analysis_cutoff_utc"] = analysis_cutoff_utc
    committed_manifest["repository_wallet_verification"] = repository_verification
    committed_manifest["analysis_outputs"] = {
        "fill_count": len(fills),
        "event_count": len(events),
        "core_statistics_sha256": sha256_bytes(stable_json({
            "event_count": summary["beijing_event_count"],
            "buy_fill_count": summary["public_buy_fill_count"],
            "sell_fill_count": summary["public_sell_fill_count"],
            "total_buy_usd": summary["total_buy_usd"],
            "strict_pnl": summary["beijing_total_pnl_strict"],
        })),
    }
    write_json(output_root / "source_manifest.json", committed_manifest)
    summary["events"] = events
    summary["strategy_archetypes"] = archetypes
    summary["implementation_status"] = "READY_FOR_REVIEW"
    write_json(summary_json, summary)
    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(render_report(summary, events), encoding="utf-8")
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("analyze")
    command.add_argument("--repo-root", required=True)
    command.add_argument("--output-root", required=True)
    command.add_argument("--summary-md", required=True)
    command.add_argument("--summary-json", required=True)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--refresh-public-data", action="store_true")
    source.add_argument("--saved-public-evidence-manifest")
    cutoff = command.add_mutually_exclusive_group(required=False)
    cutoff.add_argument("--analysis-cutoff-now", action="store_true")
    cutoff.add_argument("--analysis-cutoff-utc")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    analysis_started = datetime.now(timezone.utc).isoformat()
    analysis_cutoff = (
        analysis_started
        if args.analysis_cutoff_now or not args.analysis_cutoff_utc
        else parse_iso(args.analysis_cutoff_utc).isoformat()
    )
    if args.refresh_public_data:
        manifest = refresh_public_evidence(analysis_cutoff)
        manifest_path = Path(manifest["manifest_path"])
    else:
        manifest_path = Path(args.saved_public_evidence_manifest)
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        saved_cutoff = parse_iso(saved["analysis_cutoff_utc"]).isoformat()
        if args.analysis_cutoff_utc and parse_iso(args.analysis_cutoff_utc).isoformat() != saved_cutoff:
            raise SystemExit("offline cutoff must match the saved evidence cutoff")
        analysis_cutoff = saved_cutoff
        analysis_started = saved.get("analysis_started_at_utc", saved_cutoff)
    analyze(
        Path(args.repo_root).resolve(),
        Path(args.output_root),
        Path(args.summary_md),
        Path(args.summary_json),
        analysis_started_at_utc=analysis_started,
        analysis_cutoff_utc=analysis_cutoff,
        evidence_manifest=manifest_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
