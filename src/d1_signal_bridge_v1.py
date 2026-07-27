#!/usr/bin/env python3
"""D1 weather probability → value signal → Husky entry-signal bridge (v1).

This module is intentionally separate from the RC7 forward-simulation engine.
It only validates and converts standardized D1 signal bundles. It never:
starts formal mode, writes a formal ledger, connects a wallet, signs, or places orders.

Audit layout (no circular hash dependency):
  bridge_manifest_core.json  → immutable audit root (CSV notes reference its file SHA)
  husky_entry_signals.csv    → notes.bridge_manifest_sha256 = core file SHA256
  bridge_manifest.json       → wrapper listing all file hashes (no self-hash)
  bridge_manifest.sha256     → detached SHA256 of bridge_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:  # pragma: no cover - exercised in deployment configuration
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]

try:
    from src.polymarket_public_adapter_v5_1_8 import parse_temperature_bucket
except ModuleNotFoundError:
    from polymarket_public_adapter_v5_1_8 import parse_temperature_bucket


BRIDGE_VERSION = "d1_signal_bridge_v1"
RULES_VERSION_REQUIRED = "D1_manual_v1.0"
MODEL_FORMAL_NAME = "D1_1500"
SOURCE_TAG = "d1_signal_bridge_v1"
CANONICAL_WEATHER_METRIC = "highest_temperature"
ALLOWED_STATIONS = {"ZSPD", "ZBAA"}
STATION_CITY_ALIASES = {
    "ZSPD": {"shanghai", "上海"},
    "ZBAA": {"beijing", "北京"},
}
STATION_CITY_CANONICAL = {"ZSPD": "Shanghai", "ZBAA": "Beijing"}
METRIC_ALIASES = {
    "high": CANONICAL_WEATHER_METRIC,
    "highest": CANONICAL_WEATHER_METRIC,
    "highest_temperature": CANONICAL_WEATHER_METRIC,
    "最高温": CANONICAL_WEATHER_METRIC,
    "最高气温": CANONICAL_WEATHER_METRIC,
}
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
ZERO = Decimal("0")
ONE = Decimal("1")
PROB_EPS = Decimal("0.0000001")
EDGE_EPS = Decimal("0.0000001")
ALLOWED_DATA_STATUS = {"COMPLETE", "PARTIAL", "CONFLICTING", "STALE", "LEAKAGE_INVALID"}
NON_MARKETABLE_BUCKETS = {"其他", "其它", "other", "OTHER", "tail", "TAIL", "remainder", "REMAINDER"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
SOURCE_TIME_KEY_TOKENS = ("acquired_at", "released_at", "published_at", "captured_at", "source_time")
REQUIRED_OUTPUT_FILES = (
    "weather_probability_bundle.json",
    "value_signal_bundle.json",
    "husky_entry_signals.csv",
    "bridge_manifest_core.json",
    "bridge_manifest.json",
    "bridge_manifest.sha256",
    "validation_report.json",
)
ORDERBOOK_HASH_VALIDATION_LEVEL = "reference_format_only"

HUSKY_CSV_FIELDS = [
    "signal_id",
    "created_at_utc",
    "city",
    "weather_date_local",
    "weather_metric",
    "temperature_bucket",
    "market_slug",
    "condition_id",
    "token_id",
    "outcome",
    "side",
    "forecast_probability",
    "market_probability_at_signal",
    "intended_usd",
    "max_entry_price",
    "source",
    "notes",
    "entry_deadline_utc",
]


class BridgeError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


def dstr(value: Any) -> str:
    x = Decimal(str(value))
    if x == x.to_integral():
        return str(x.quantize(Decimal("1")))
    return format(x.normalize(), "f")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256_bytes(stable_json(value).encode("utf-8"))


def assert_sha256_hex(value: Any, *, field: str) -> str:
    # Never normalize an audit hash before validating it.  In particular, a
    # value that differs only by case or surrounding whitespace is invalid.
    text = value if isinstance(value, str) else ""
    if not SHA256_RE.fullmatch(text):
        raise BridgeError("INVALID_SHA256", f"{field} must be 64-char lowercase hex SHA256", {"field": field, "value": value})
    return text


SCHEMA_FILES = {
    "weather": "d1_weather_probability_v1.schema.json",
    "value": "d1_value_signal_v1.schema.json",
    "core_manifest": "d1_bridge_manifest_core_v1.schema.json",
    "final_manifest": "d1_bridge_manifest_v1.schema.json",
}
SCHEMA_ERROR_CODES = {
    "weather": "WEATHER_JSON_SCHEMA_INVALID",
    "value": "VALUE_JSON_SCHEMA_INVALID",
    "core_manifest": "CORE_MANIFEST_JSON_SCHEMA_INVALID",
    "final_manifest": "FINAL_MANIFEST_JSON_SCHEMA_INVALID",
}


def validate_against_schema(payload: Any, schema_name: str) -> dict[str, Any]:
    """Validate a bridge payload with Draft 2020-12 and RFC format checks."""
    if Draft202012Validator is None or FormatChecker is None:
        raise BridgeError(
            "JSON_SCHEMA_DEPENDENCY_MISSING",
            "jsonschema is required; install requirements-d1-signal-bridge-v1.txt",
        )
    if schema_name not in SCHEMA_FILES:
        raise BridgeError("UNKNOWN_SCHEMA", f"unsupported schema: {schema_name}")
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / SCHEMA_FILES[schema_name]
    try:
        schema = load_json(schema_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("SCHEMA_LOAD_FAILED", f"cannot load {schema_path.name}: {exc}") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    violations = sorted(validator.iter_errors(payload), key=lambda err: list(err.absolute_path))
    if violations:
        error = violations[0]
        json_path = "/" + "/".join(str(part) for part in error.absolute_path)
        schema_path_text = "/" + "/".join(str(part) for part in error.absolute_schema_path)
        raise BridgeError(
            SCHEMA_ERROR_CODES[schema_name],
            error.message,
            {"json_path": json_path, "schema_path": schema_path_text, "message": error.message},
        )
    return {"ok": True, "schema": SCHEMA_FILES[schema_name], "validator": "Draft202012Validator", "format_checker": True}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def relative_to_output(path: Path, output_dir: Path) -> str:
    return Path(path).resolve().relative_to(Path(output_dir).resolve()).as_posix()


def parse_iso_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeError("INVALID_TIMESTAMP", "empty timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BridgeError("INVALID_TIMESTAMP", f"invalid timestamp: {value}") from exc
    if dt.tzinfo is None:
        raise BridgeError("INVALID_TIMESTAMP", f"timezone required: {value}")
    return dt.astimezone(UTC)


def parse_iso_cst(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeError("INVALID_TIMESTAMP", "empty cst timestamp")
    if raw.endswith("Z"):
        raise BridgeError("INVALID_TIMESTAMP", f"CST timestamp must not use Z: {value}")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BridgeError("INVALID_TIMESTAMP", f"invalid cst timestamp: {value}") from exc
    if dt.tzinfo is None:
        raise BridgeError("INVALID_TIMESTAMP", f"CST timestamp must include explicit +08:00 offset: {value}")
    return dt.astimezone(CST)


def assert_explicit_cst_offset(raw: str) -> None:
    text = str(raw or "").strip()
    if not text.endswith("+08:00"):
        raise BridgeError(
            "CST_OFFSET_INVALID",
            "as_of_time_cst must explicitly use +08:00 offset",
            {"value": raw},
        )


def assert_explicit_utc_literal(raw: str) -> None:
    text = str(raw or "").strip()
    if not (text.endswith("Z") or text.endswith("+00:00")):
        raise BridgeError(
            "UTC_LITERAL_INVALID",
            "as_of_time_utc must explicitly use Z or +00:00",
            {"value": raw},
        )


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BridgeError("INVALID_DECIMAL", f"invalid decimal: {value}") from exc


def normalize_temp_bucket_label(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text in NON_MARKETABLE_BUCKETS:
        return text
    text = text.replace("℃", "C").replace("°C", "C").replace("°c", "C")
    canonical = parse_temperature_bucket(text)
    if canonical:
        return canonical
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"-?\d+C?", compact, flags=re.I):
        if not compact.upper().endswith("C"):
            compact = compact + "C"
        compact = f"exact:{compact}"
    canonical = parse_temperature_bucket(compact)
    return canonical or compact


def is_marketable_bucket(label: str) -> bool:
    if label in NON_MARKETABLE_BUCKETS:
        return False
    return bool(parse_temperature_bucket(label))


def expected_next_day(as_of_cst: datetime) -> date:
    return as_of_cst.astimezone(CST).date() + timedelta(days=1)


def normalize_weather_metric(raw: Any, *, formal_mode: bool) -> str:
    text = str(raw or "").strip()
    key = text.lower() if text.isascii() else text
    canonical = METRIC_ALIASES.get(key) or METRIC_ALIASES.get(text)
    if canonical is None:
        raise BridgeError("WEATHER_METRIC_INVALID", f"unsupported weather_metric: {raw}")
    if formal_mode and canonical != CANONICAL_WEATHER_METRIC:
        raise BridgeError("WEATHER_METRIC_INVALID", f"formal mode requires {CANONICAL_WEATHER_METRIC}")
    return canonical


def normalize_city_for_station(station: str, city: Any) -> str:
    text = str(city or "").strip()
    if not text:
        raise BridgeError("CITY_MISMATCH", "city is required")
    aliases = STATION_CITY_ALIASES.get(station, set())
    if text.casefold() not in {a.casefold() for a in aliases} and text not in aliases:
        raise BridgeError(
            "CITY_MISMATCH",
            f"city {city!r} is not valid for station {station}",
            {"station": station, "city": city, "allowed": sorted(aliases)},
        )
    return STATION_CITY_CANONICAL[station]


def validate_d1_1500_time_fields(
    as_of_time_utc: str,
    as_of_time_cst: str,
    weather_date_local: str,
    generated_at_utc: str | None = None,
    *,
    formal_mode: bool = True,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    generated_out_of_window = False

    if formal_mode:
        assert_explicit_cst_offset(as_of_time_cst)
        assert_explicit_utc_literal(as_of_time_utc)

    as_of_cst = parse_iso_cst(as_of_time_cst)
    as_of_utc = parse_iso_utc(as_of_time_utc)

    if as_of_cst.hour != 15 or as_of_cst.minute != 0 or as_of_cst.second != 0 or as_of_cst.microsecond != 0:
        issues.append("as_of_time_cst_not_1500")
    if as_of_utc.hour != 7 or as_of_utc.minute != 0 or as_of_utc.second != 0 or as_of_utc.microsecond != 0:
        issues.append("as_of_time_utc_not_0700")
    expected_utc = as_of_cst.astimezone(UTC)
    if as_of_utc != expected_utc:
        issues.append("as_of_utc_cst_mismatch")

    try:
        weather_day = date.fromisoformat(str(weather_date_local))
    except ValueError as exc:
        raise BridgeError("INVALID_WEATHER_DATE", f"invalid weather_date_local: {weather_date_local}") from exc
    if weather_day != expected_next_day(as_of_cst):
        issues.append("weather_date_local_not_next_day")

    if generated_at_utc:
        generated = parse_iso_utc(generated_at_utc)
        window_end = expected_utc + timedelta(minutes=5)
        if generated < expected_utc or generated > window_end:
            generated_out_of_window = True
            if generated < expected_utc:
                warnings.append("generated_at_before_as_of")
            if generated > window_end:
                warnings.append("generated_at_after_1505_window")

    if formal_mode and issues:
        raise BridgeError("D1_1500_TIME_INVALID", ";".join(issues), {"issues": issues, "warnings": warnings})

    return {
        "ok": not issues and not generated_out_of_window,
        "issues": issues,
        "warnings": warnings,
        "generated_out_of_window": generated_out_of_window,
        "as_of_utc": as_of_utc.isoformat(),
        "as_of_cst": as_of_cst.isoformat(),
        "weather_date_local": weather_day.isoformat(),
    }


def _iter_source_timestamps(obj: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            lk = str(key).lower()
            if any(tok in lk for tok in SOURCE_TIME_KEY_TOKENS):
                if not isinstance(value, str) or not value.strip():
                    raise BridgeError("SOURCE_TIMESTAMP_INVALID", f"empty source timestamp at {child}")
                found.append((child, value))
            found.extend(_iter_source_timestamps(value, child))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            found.extend(_iter_source_timestamps(item, f"{path}[{idx}]"))
    return found


def validate_source_snapshot(manifest: Any, declared_sha: Any, as_of_utc: datetime) -> str:
    if not isinstance(manifest, dict) or not manifest:
        raise BridgeError("SOURCE_MANIFEST_INVALID", "source_snapshot_manifest must be a non-empty object")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BridgeError("SOURCE_MANIFEST_INVALID", "source_snapshot_manifest.sources must be a non-empty list")

    computed = content_hash(manifest)
    declared = assert_sha256_hex(declared_sha, field="source_snapshot_sha256")
    if declared != computed:
        raise BridgeError(
            "SOURCE_MANIFEST_HASH_MISMATCH",
            "source_snapshot_sha256 must equal canonical SHA256(source_snapshot_manifest)",
            {"declared": declared, "computed": computed},
        )

    for loc, raw in _iter_source_timestamps(manifest):
        try:
            ts = parse_iso_utc(raw)
        except BridgeError as exc:
            raise BridgeError("SOURCE_TIMESTAMP_INVALID", f"invalid source timestamp at {loc}: {raw}", {"path": loc}) from exc
        if ts > as_of_utc:
            raise BridgeError(
                "LEAKAGE_INVALID",
                f"source timestamp after as_of_time_utc at {loc}",
                {"path": loc, "timestamp": raw, "as_of_time_utc": as_of_utc.isoformat()},
            )
    return computed


def validate_weather_probability_bundle(bundle: dict[str, Any], *, formal_mode: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = [
        "forecast_run_id",
        "model_version",
        "rules_version",
        "station",
        "city",
        "weather_date_local",
        "weather_metric",
        "as_of_time_utc",
        "as_of_time_cst",
        "generated_at_utc",
        "data_status",
        "confidence",
        "source_snapshot_sha256",
        "source_snapshot_manifest",
        "explanation",
        "integer_temperature_probabilities",
    ]
    for key in required:
        if key not in bundle:
            errors.append(f"missing_field:{key}")
    if errors:
        raise BridgeError("WEATHER_SCHEMA_INVALID", "missing required fields", {"errors": errors})

    station = str(bundle["station"]).upper()
    if station not in ALLOWED_STATIONS:
        raise BridgeError("UNKNOWN_STATION", f"station not allowed: {station}")
    city = normalize_city_for_station(station, bundle.get("city"))
    metric = normalize_weather_metric(bundle.get("weather_metric"), formal_mode=formal_mode)

    data_status = str(bundle["data_status"]).upper()
    if data_status not in ALLOWED_DATA_STATUS:
        raise BridgeError("INVALID_DATA_STATUS", f"unsupported data_status: {data_status}")

    if formal_mode:
        if str(bundle.get("model_version")) != MODEL_FORMAL_NAME:
            raise BridgeError("MODEL_VERSION_MISMATCH", f"formal model_version must be {MODEL_FORMAL_NAME}")
        if str(bundle.get("rules_version")) != RULES_VERSION_REQUIRED:
            raise BridgeError("RULES_VERSION_MISMATCH", f"formal rules_version must be {RULES_VERSION_REQUIRED}")

    time_info = validate_d1_1500_time_fields(
        str(bundle["as_of_time_utc"]),
        str(bundle["as_of_time_cst"]),
        str(bundle["weather_date_local"]),
        str(bundle.get("generated_at_utc") or ""),
        formal_mode=formal_mode,
    )
    warnings.extend(time_info["warnings"])
    formal_blocked = bool(formal_mode and time_info["generated_out_of_window"])

    as_of_utc = parse_iso_utc(str(bundle["as_of_time_utc"]))
    try:
        source_sha = validate_source_snapshot(bundle.get("source_snapshot_manifest"), bundle.get("source_snapshot_sha256"), as_of_utc)
    except BridgeError as exc:
        if exc.code == "LEAKAGE_INVALID":
            data_status = "LEAKAGE_INVALID"
            formal_blocked = True
            warnings.append(exc.message)
            source_sha = content_hash(bundle.get("source_snapshot_manifest"))
        else:
            raise

    probs = bundle.get("integer_temperature_probabilities")
    if not isinstance(probs, list) or not probs:
        raise BridgeError("PROBABILITY_LIST_INVALID", "integer_temperature_probabilities must be a non-empty list")

    seen: set[str] = set()
    total = ZERO
    normalized_rows: list[dict[str, Any]] = []
    for row in probs:
        if not isinstance(row, dict):
            raise BridgeError("PROBABILITY_ROW_INVALID", "probability row must be object")
        bucket = normalize_temp_bucket_label(row.get("temperature_bucket"))
        if not bucket:
            raise BridgeError("TEMPERATURE_BUCKET_INVALID", f"unparseable temperature_bucket: {row.get('temperature_bucket')}")
        if bucket in seen:
            raise BridgeError("DUPLICATE_TEMPERATURE_BUCKET", f"duplicate temperature_bucket: {bucket}")
        seen.add(bucket)
        p = dec(row.get("forecast_probability"))
        if p < ZERO or p > ONE:
            raise BridgeError("PROBABILITY_OUT_OF_RANGE", f"probability out of range for {bucket}: {p}")
        total += p
        normalized_rows.append(
            {
                "temperature_bucket": bucket,
                "forecast_probability": dstr(p),
                "marketable": is_marketable_bucket(bucket),
            }
        )

    if abs(total - ONE) > PROB_EPS:
        raise BridgeError("PROBABILITY_SUM_INVALID", f"probability sum must be 1, got {total}")

    if data_status == "LEAKAGE_INVALID":
        formal_blocked = True

    validate_against_schema(bundle, "weather")

    return {
        "ok": True,
        "data_status": data_status,
        "station": station,
        "city": city,
        "weather_metric": metric,
        "warnings": warnings,
        "normalized_probabilities": normalized_rows,
        "probability_sum": dstr(total),
        "formal_blocked": formal_blocked,
        "time_info": time_info,
        "bundle_sha256": content_hash(bundle),
        "source_snapshot_manifest_sha256": source_sha,
    }


def _assert_identity_match(weather: dict[str, Any], value: dict[str, Any], field: str, code: str) -> None:
    if str(value.get(field)) != str(weather.get(field)):
        raise BridgeError(code, f"value.{field} must match weather.{field}", {"weather": weather.get(field), "value": value.get(field)})


def _validate_status_no_upgrade(parent: str, child: str, *, field: str) -> str:
    parent = str(parent).upper()
    child = str(child).upper()
    if child not in ALLOWED_DATA_STATUS:
        raise BridgeError("VALUE_DATA_STATUS_INVALID", f"unsupported {field}: {child}")
    if child == "LEAKAGE_INVALID" or parent == "LEAKAGE_INVALID":
        raise BridgeError("LEAKAGE_INVALID", f"{field} is LEAKAGE_INVALID")
    if parent != "COMPLETE" and child == "COMPLETE":
        raise BridgeError("STATUS_UPGRADE_FORBIDDEN", f"cannot upgrade {parent} to COMPLETE via {field}")
    return child


def validate_value_signal_bundle(
    weather: dict[str, Any],
    value: dict[str, Any],
    *,
    formal_mode: bool = True,
    weather_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Preserve the bridge-specific identity error rather than reducing a
    # missing candidate identity to a generic JSON Schema violation.
    raw_candidates = value.get("candidates") if isinstance(value, dict) else None
    if isinstance(raw_candidates, list):
        for index, candidate in enumerate(raw_candidates):
            if isinstance(candidate, dict):
                missing_identity = [
                    field
                    for field in ("forecast_run_id", "station", "weather_date_local", "weather_metric", "data_status")
                    if field not in candidate
                ]
                if missing_identity:
                    raise BridgeError(
                        "CANDIDATE_IDENTITY_MISSING",
                        "candidate identity fields are required",
                        {"index": index, "missing": missing_identity},
                    )
    weather_validation = weather_validation or validate_weather_probability_bundle(weather, formal_mode=formal_mode)
    if weather_validation["data_status"] == "LEAKAGE_INVALID" or weather_validation.get("formal_blocked"):
        code = "LEAKAGE_INVALID" if weather_validation["data_status"] == "LEAKAGE_INVALID" else "FORMAL_TIME_WINDOW_BLOCKED"
        raise BridgeError(code, "weather bundle is not eligible for value/execution conversion", {"weather": weather_validation})

    required = [
        "forecast_run_id",
        "model_version",
        "rules_version",
        "station",
        "city",
        "weather_date_local",
        "weather_metric",
        "data_status",
        "weather_bundle_sha256",
        "candidates",
    ]
    for key in required:
        if key not in value:
            raise BridgeError("VALUE_SCHEMA_INVALID", f"missing field: {key}")

    _assert_identity_match(weather, value, "forecast_run_id", "FORECAST_RUN_ID_MISMATCH")
    _assert_identity_match(weather, value, "model_version", "MODEL_VERSION_MISMATCH")
    _assert_identity_match(weather, value, "rules_version", "RULES_VERSION_MISMATCH")
    if str(value["station"]).upper() != str(weather["station"]).upper():
        raise BridgeError("STATION_MISMATCH", "value.station must match weather.station")
    normalize_city_for_station(str(weather["station"]).upper(), value.get("city"))
    if normalize_city_for_station(str(weather["station"]).upper(), value.get("city")) != weather_validation["city"]:
        raise BridgeError("CITY_MISMATCH", "value.city must match weather.city for station")
    _assert_identity_match(weather, value, "weather_date_local", "WEATHER_DATE_MISMATCH")
    value_metric = normalize_weather_metric(value.get("weather_metric"), formal_mode=formal_mode)
    if value_metric != weather_validation["weather_metric"]:
        raise BridgeError("WEATHER_METRIC_MISMATCH", "value.weather_metric must match weather.weather_metric")

    weather_sha = assert_sha256_hex(value.get("weather_bundle_sha256"), field="weather_bundle_sha256")
    if weather_sha != weather_validation["bundle_sha256"]:
        raise BridgeError("WEATHER_HASH_MISMATCH", "value.weather_bundle_sha256 does not match weather bundle content hash")

    value_status = _validate_status_no_upgrade(
        weather_validation["data_status"],
        str(value.get("data_status") or ""),
        field="value.data_status",
    )

    validate_against_schema(value, "value")

    weather_prob_map = {
        row["temperature_bucket"]: Decimal(row["forecast_probability"])
        for row in weather_validation["normalized_probabilities"]
        if row["marketable"]
    }
    as_of_utc = parse_iso_utc(str(weather["as_of_time_utc"]))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise BridgeError("VALUE_CANDIDATES_INVALID", "candidates must be a list")

    for idx, raw in enumerate(candidates):
        try:
            if not isinstance(raw, dict):
                raise BridgeError("CANDIDATE_INVALID", "candidate must be object")
            identity_fields = ("forecast_run_id", "station", "weather_date_local", "weather_metric", "data_status")
            missing_identity = [field for field in identity_fields if field not in raw]
            if missing_identity:
                raise BridgeError(
                    "CANDIDATE_IDENTITY_MISSING",
                    "candidate identity fields are required",
                    {"missing": missing_identity},
                )
            if str(raw["forecast_run_id"]) != str(weather["forecast_run_id"]):
                raise BridgeError("FORECAST_RUN_ID_MISMATCH", "candidate.forecast_run_id mismatch")
            if str(raw["station"]).upper() != str(weather["station"]).upper():
                raise BridgeError("STATION_MISMATCH", "candidate.station mismatch")
            if str(raw["weather_date_local"]) != str(weather["weather_date_local"]):
                raise BridgeError("WEATHER_DATE_MISMATCH", "candidate.weather_date_local mismatch")
            if normalize_weather_metric(raw["weather_metric"], formal_mode=formal_mode) != weather_validation["weather_metric"]:
                raise BridgeError("WEATHER_METRIC_MISMATCH", "candidate.weather_metric mismatch")

            bucket = normalize_temp_bucket_label(raw.get("temperature_bucket"))
            if not is_marketable_bucket(bucket):
                raise BridgeError("NON_MARKETABLE_BUCKET", f"bucket cannot map to market token: {bucket}")
            if bucket not in weather_prob_map:
                raise BridgeError("BUCKET_NOT_IN_WEATHER", f"candidate bucket missing from weather probabilities: {bucket}")
            forecast_p = dec(raw.get("forecast_probability"))
            if abs(forecast_p - weather_prob_map[bucket]) > PROB_EPS:
                raise BridgeError("FORECAST_PROBABILITY_REWRITE", "value layer must not rewrite weather probability")
            ask = dec(raw.get("market_ask_price"))
            if ask < ZERO or ask > ONE:
                raise BridgeError("ASK_OUT_OF_RANGE", f"market_ask_price out of range: {ask}")
            edge_declared = dec(raw.get("edge"))
            edge_calc = forecast_p - ask
            if abs(edge_declared - edge_calc) > EDGE_EPS:
                raise BridgeError("EDGE_MISMATCH", f"edge must equal forecast_probability - market_ask_price; got {edge_declared}, expected {edge_calc}")
            max_price = dec(raw.get("recommended_max_price"))
            if max_price < ZERO or max_price > ONE:
                raise BridgeError("MAX_PRICE_OUT_OF_RANGE", f"recommended_max_price out of range: {max_price}")
            intended = dec(raw.get("intended_usd"))
            if intended <= ZERO:
                raise BridgeError("INTENDED_USD_INVALID", "intended_usd must be > 0")
            for key in ("market_slug", "condition_id", "token_id", "outcome"):
                if not str(raw.get(key) or "").strip():
                    raise BridgeError("MARKET_REF_MISSING", f"{key} is required")
            captured = parse_iso_utc(str(raw.get("orderbook_captured_at_utc") or ""))
            if captured > as_of_utc:
                raise BridgeError("ORDERBOOK_LEAKAGE", "orderbook_captured_at_utc after as_of_time_utc")
            if not str(raw.get("orderbook_snapshot_id") or "").strip():
                raise BridgeError("ORDERBOOK_SNAPSHOT_MISSING", "orderbook_snapshot_id required")
            ob_hash = assert_sha256_hex(raw.get("orderbook_snapshot_sha256"), field="orderbook_snapshot_sha256")
            evidence_path = str(raw.get("orderbook_snapshot_evidence_path") or "").strip()
            if evidence_path:
                ev = Path(evidence_path)
                if not ev.is_file():
                    raise BridgeError("ORDERBOOK_EVIDENCE_MISSING", f"orderbook evidence file not found: {evidence_path}")
                actual = sha256_file(ev)
                if actual != ob_hash:
                    raise BridgeError("ORDERBOOK_HASH_MISMATCH", "orderbook_snapshot_sha256 does not match evidence file")
            cand_status = _validate_status_no_upgrade(
                value_status,
                str(raw["data_status"]),
                field="candidate.data_status",
            )
            accepted.append(
                {
                    "forecast_run_id": str(weather["forecast_run_id"]),
                    "model_version": str(weather["model_version"]),
                    "rules_version": str(weather["rules_version"]),
                    "station": str(weather["station"]).upper(),
                    "city": weather_validation["city"],
                    "weather_date_local": str(weather["weather_date_local"]),
                    "weather_metric": weather_validation["weather_metric"],
                    "temperature_bucket": bucket,
                    "forecast_probability": dstr(forecast_p),
                    "market_slug": str(raw["market_slug"]),
                    "condition_id": str(raw["condition_id"]),
                    "token_id": str(raw["token_id"]),
                    "outcome": str(raw["outcome"]),
                    "market_ask_price": dstr(ask),
                    "edge": dstr(edge_calc),
                    "recommended_max_price": dstr(max_price),
                    "intended_usd": dstr(intended),
                    "reason": str(raw.get("reason") or ""),
                    "data_status": cand_status,
                    "orderbook_snapshot_id": str(raw["orderbook_snapshot_id"]),
                    "orderbook_snapshot_sha256": ob_hash,
                    "orderbook_captured_at_utc": captured.isoformat().replace("+00:00", "+00:00"),
                    "orderbook_hash_verification": (
                        "evidence_file_verified" if evidence_path else ORDERBOOK_HASH_VALIDATION_LEVEL
                    ),
                }
            )
        except BridgeError as exc:
            rejected.append({"index": idx, "code": exc.code, "message": exc.message, "details": exc.details})

    return {
        "ok": True,
        "accepted": accepted,
        "rejected": rejected,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "value_sha256": content_hash(value),
        "weather_sha256": weather_validation["bundle_sha256"],
        "data_status": value_status if value_status != "COMPLETE" else weather_validation["data_status"],
        "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL,
    }


def build_notes(audit: dict[str, str]) -> str:
    return ";".join(f"{k}={v}" for k, v in audit.items())


def parse_notes(notes: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(notes or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key] = value
    return out


def value_candidates_to_husky_csv_rows(
    weather: dict[str, Any],
    value_validation: dict[str, Any],
    *,
    entry_valid_minutes: int = 10,
    weather_sha256: str,
    value_sha256: str,
    bridge_manifest_sha256: str,
) -> list[dict[str, str]]:
    as_of_utc = parse_iso_utc(str(weather["as_of_time_utc"]))
    rows: list[dict[str, str]] = []
    for i, cand in enumerate(value_validation["accepted"], start=1):
        signal_id = f"{cand['forecast_run_id']}__{cand['temperature_bucket'].replace(':', '_')}__{i:02d}"
        deadline = (as_of_utc + timedelta(minutes=int(entry_valid_minutes))).isoformat()
        notes = build_notes(
            {
                "forecast_run_id": cand["forecast_run_id"],
                "model_version": cand["model_version"],
                "rules_version": cand["rules_version"],
                "data_status": cand["data_status"],
                "weather_bundle_sha256": weather_sha256,
                "value_bundle_sha256": value_sha256,
                "bridge_manifest_sha256": bridge_manifest_sha256,
                "orderbook_snapshot_id": cand["orderbook_snapshot_id"],
                "orderbook_snapshot_sha256": cand["orderbook_snapshot_sha256"],
                "edge": cand["edge"],
                "reason": cand["reason"].replace(";", ","),
            }
        )
        rows.append(
            {
                "signal_id": signal_id,
                "created_at_utc": as_of_utc.isoformat(),
                "city": cand["city"],
                "weather_date_local": cand["weather_date_local"],
                "weather_metric": cand["weather_metric"],
                "temperature_bucket": cand["temperature_bucket"],
                "market_slug": cand["market_slug"],
                "condition_id": cand["condition_id"],
                "token_id": cand["token_id"],
                "outcome": cand["outcome"],
                "side": "BUY",
                "forecast_probability": cand["forecast_probability"],
                "market_probability_at_signal": cand["market_ask_price"],
                "intended_usd": cand["intended_usd"],
                "max_entry_price": cand["recommended_max_price"],
                "source": SOURCE_TAG,
                "notes": notes,
                "entry_deadline_utc": deadline,
            }
        )
    return rows


def write_husky_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, HUSKY_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def output_dir_for_run(output_root: Path, forecast_run_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", forecast_run_id)
    return Path(output_root) / safe


def _required_files_present(output_dir: Path) -> list[str]:
    missing = []
    for name in REQUIRED_OUTPUT_FILES:
        if not (Path(output_dir) / name).is_file():
            missing.append(name)
    return missing


def _schema_result(payload: Any, schema_name: str, errors: list[str]) -> dict[str, Any]:
    try:
        return validate_against_schema(payload, schema_name)
    except BridgeError as exc:
        errors.append(exc.code)
        return {"ok": False, "code": exc.code, "details": exc.details}


def _manifest_artifact_path(output_dir: Path, name: str, meta: Any) -> Path:
    if not isinstance(meta, dict):
        raise BridgeError("MANIFEST_PATH_NOT_EXACT", f"manifest entry missing for {name}")
    raw_path = meta.get("path")
    if not isinstance(raw_path, str) or raw_path != name:
        if isinstance(raw_path, str) and (Path(raw_path).is_absolute() or WINDOWS_ABSOLUTE_PATH_RE.match(raw_path) or raw_path.startswith("\\\\")):
            raise BridgeError("MANIFEST_PATH_ABSOLUTE", f"manifest path must not be absolute: {raw_path!r}")
        if isinstance(raw_path, str) and (".." in Path(raw_path).parts or "/" in raw_path or "\\" in raw_path):
            raise BridgeError("MANIFEST_PATH_TRAVERSAL", f"manifest path must not traverse directories: {raw_path!r}")
        raise BridgeError("MANIFEST_PATH_NOT_EXACT", f"manifest path must be exactly {name!r}: {raw_path!r}")
    path = (output_dir / raw_path).resolve()
    if path.parent != output_dir.resolve():
        raise BridgeError("MANIFEST_PATH_TRAVERSAL", f"manifest path escapes output directory: {raw_path!r}")
    return path


def _build_validation_report(
    weather: dict[str, Any], weather_validation: dict[str, Any], value_validation: dict[str, Any], rows: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "bridge_version": BRIDGE_VERSION,
        "forecast_run_id": str(weather["forecast_run_id"]),
        "weather_validation": {
            "data_status": weather_validation["data_status"],
            "warnings": weather_validation["warnings"],
            "probability_sum": weather_validation["probability_sum"],
            "bundle_sha256": weather_validation["bundle_sha256"],
            "source_snapshot_manifest_sha256": weather_validation["source_snapshot_manifest_sha256"],
        },
        "value_validation": {
            "accepted_count": value_validation["accepted_count"],
            "rejected_count": value_validation["rejected_count"],
            "rejected": value_validation["rejected"],
            "value_sha256": value_validation["value_sha256"],
        },
        "converted_signal_count": len(rows),
        "rejected_signal_count": value_validation["rejected_count"],
        "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL,
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
        "files": {name: name for name in REQUIRED_OUTPUT_FILES},
    }


def verify_bridge_output(output_dir: Path) -> dict[str, Any]:
    """Verify wrapper hashes *and* replay every D1 conversion semantic."""
    output_dir = Path(output_dir)
    errors: list[str] = []
    result: dict[str, Any] = {
        "weather_revalidation_result": {"ok": False},
        "value_revalidation_result": {"ok": False},
        "core_rebuild_result": {"ok": False},
        "csv_rebuild_result": {"ok": False},
        "report_rebuild_result": {"ok": False},
        "manifest_identity_result": {"ok": False},
        "semantic_replay_result": {"ok": False},
    }
    missing = _required_files_present(output_dir)
    if missing:
        result.update({"ok": False, "errors": [f"missing:{name}" for name in missing], "manifest": None})
        return result
    try:
        manifest_path = output_dir / "bridge_manifest.json"
        core_path = output_dir / "bridge_manifest_core.json"
        detached_path = output_dir / "bridge_manifest.sha256"
        manifest = load_json(manifest_path)
        core = load_json(core_path)
        weather = load_json(output_dir / "weather_probability_bundle.json")
        value = load_json(output_dir / "value_signal_bundle.json")
        report = load_json(output_dir / "validation_report.json")
    except (OSError, json.JSONDecodeError) as exc:
        result.update({"ok": False, "errors": [f"output_parse_error:{exc}"], "manifest": None})
        return result

    result["weather_schema_runtime_result"] = _schema_result(weather, "weather", errors)
    result["value_schema_runtime_result"] = _schema_result(value, "value", errors)
    result["core_schema_runtime_result"] = _schema_result(core, "core_manifest", errors)
    result["final_schema_runtime_result"] = _schema_result(manifest, "final_manifest", errors)
    actual_manifest_sha = sha256_file(manifest_path)
    actual_core_sha = sha256_file(core_path)
    detached_raw = detached_path.read_text(encoding="utf-8")
    detached = detached_raw[:-1] if detached_raw.endswith("\n") else detached_raw
    try:
        assert_sha256_hex(detached, field="bridge_manifest.sha256")
        if detached != actual_manifest_sha:
            errors.append("detached_manifest_sha_mismatch")
    except BridgeError as exc:
        errors.append(exc.code)

    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        errors.append("manifest_files_invalid")
        files = {}
    resolved_paths: dict[str, Path] = {}
    for name in ("weather_probability_bundle.json", "value_signal_bundle.json", "bridge_manifest_core.json", "husky_entry_signals.csv", "validation_report.json"):
        try:
            artifact = _manifest_artifact_path(output_dir, name, files.get(name))
            resolved_paths[name] = artifact
            if not artifact.is_file():
                errors.append(f"missing:{name}")
                continue
            expected_sha = assert_sha256_hex(files[name].get("sha256"), field=f"manifest.files.{name}.sha256")
            if sha256_file(artifact) != expected_sha:
                errors.append(f"hash_mismatch:{name}")
        except BridgeError as exc:
            errors.append(exc.code)

    core_meta = files.get("bridge_manifest_core.json") if isinstance(files, dict) else None
    if isinstance(core_meta, dict):
        try:
            if assert_sha256_hex(core_meta.get("sha256"), field="manifest.core.sha256") != actual_core_sha:
                errors.append("core_manifest_hash_mismatch")
        except BridgeError as exc:
            errors.append(exc.code)

    try:
        formal_mode = bool(core["conversion_parameters"]["formal_mode"])
        entry_valid_minutes = int(core["conversion_parameters"]["entry_valid_minutes"])
        weather_validation = validate_weather_probability_bundle(weather, formal_mode=formal_mode)
        result["weather_revalidation_result"] = {"ok": True}
    except (BridgeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"weather_revalidation_failed:{getattr(exc, 'code', type(exc).__name__)}")
        weather_validation = None
    try:
        if weather_validation is None:
            raise BridgeError("WEATHER_REVALIDATION_UNAVAILABLE", "weather replay failed")
        value_validation = validate_value_signal_bundle(
            weather, value, formal_mode=formal_mode, weather_validation=weather_validation
        )
        result["value_revalidation_result"] = {"ok": True}
    except (BridgeError, UnboundLocalError) as exc:
        errors.append(f"value_revalidation_failed:{getattr(exc, 'code', type(exc).__name__)}")
        value_validation = None

    if weather_validation is not None and value_validation is not None:
        expected_core = _build_core_manifest(
            weather, weather_validation, value_validation, entry_valid_minutes=entry_valid_minutes, formal_mode=formal_mode
        )
        if core != expected_core:
            errors.append("core_rebuild_mismatch")
        else:
            result["core_rebuild_result"] = {"ok": True}
        expected_rows = value_candidates_to_husky_csv_rows(
            weather,
            value_validation,
            entry_valid_minutes=entry_valid_minutes,
            weather_sha256=weather_validation["bundle_sha256"],
            value_sha256=value_validation["value_sha256"],
            bridge_manifest_sha256=actual_core_sha,
        )
        try:
            with resolved_paths.get("husky_entry_signals.csv", output_dir / "husky_entry_signals.csv").open(encoding="utf-8", newline="") as handle:
                actual_rows = list(csv.DictReader(handle))
            if actual_rows != expected_rows:
                errors.append("csv_rebuild_mismatch")
            else:
                result["csv_rebuild_result"] = {"ok": True}
            for row in actual_rows:
                notes = parse_notes(row.get("notes") or "")
                if notes.get("bridge_manifest_sha256") != actual_core_sha:
                    errors.append("csv_core_manifest_reference_mismatch")
                    break
        except OSError as exc:
            errors.append(f"csv_read_failed:{exc}")
            actual_rows = []
        expected_report = _build_validation_report(weather, weather_validation, value_validation, expected_rows)
        if report != expected_report:
            errors.append("report_rebuild_mismatch")
        else:
            result["report_rebuild_result"] = {"ok": True}
        identity_keys = (
            "bridge_version", "forecast_run_id", "model_version", "rules_version", "station", "city", "weather_date_local",
            "weather_metric", "as_of_time_utc", "as_of_time_cst", "data_status", "formal_ledger_used", "wallet_or_real_order_used",
        )
        expected_identity = _build_core_manifest(
            weather, weather_validation, value_validation, entry_valid_minutes=entry_valid_minutes, formal_mode=formal_mode
        )
        manifest_identity_ok = all(manifest.get(key) == expected_identity.get(key) for key in identity_keys)
        manifest_identity_ok = manifest_identity_ok and manifest.get("converted_signal_count") == len(expected_rows)
        manifest_identity_ok = manifest_identity_ok and manifest.get("rejected_signal_count") == value_validation["rejected_count"]
        manifest_identity_ok = manifest_identity_ok and manifest.get("rejection_reasons") == [
            f"{item['code']}:{item['message']}" for item in value_validation["rejected"]
        ]
        expected_hashes = {
            "weather_probability_bundle": weather_validation["bundle_sha256"],
            "value_signal_bundle": value_validation["value_sha256"],
            "source_snapshot_manifest": weather_validation["source_snapshot_manifest_sha256"],
        }
        manifest_identity_ok = manifest_identity_ok and manifest.get("input_content_hashes") == expected_hashes
        if not manifest_identity_ok:
            errors.append("manifest_identity_mismatch")
        else:
            result["manifest_identity_result"] = {"ok": True}

    if manifest.get("formal_ledger_used") is not False or core.get("formal_ledger_used") is not False:
        errors.append("formal_ledger_used_not_false")
    if manifest.get("wallet_or_real_order_used") is not False or core.get("wallet_or_real_order_used") is not False:
        errors.append("wallet_or_real_order_used_not_false")
    result["semantic_replay_result"] = {"ok": not any("revalidation_failed" in error or "rebuild_mismatch" in error or error == "manifest_identity_mismatch" for error in errors)}
    result.update({
        "ok": not errors,
        "errors": errors,
        "manifest": manifest,
        "core_sha256": actual_core_sha,
        "manifest_sha256": actual_manifest_sha,
    })
    return result


def _build_core_manifest(
    weather: dict[str, Any],
    weather_validation: dict[str, Any],
    value_validation: dict[str, Any],
    *,
    entry_valid_minutes: int,
    formal_mode: bool,
) -> dict[str, Any]:
    return {
        "bridge_version": BRIDGE_VERSION,
        "forecast_run_id": str(weather["forecast_run_id"]),
        "model_version": str(weather["model_version"]),
        "rules_version": str(weather["rules_version"]),
        "station": weather_validation["station"],
        "city": weather_validation["city"],
        "weather_date_local": str(weather["weather_date_local"]),
        "weather_metric": weather_validation["weather_metric"],
        "as_of_time_utc": str(weather["as_of_time_utc"]),
        "as_of_time_cst": str(weather["as_of_time_cst"]),
        "data_status": value_validation["data_status"],
        "weather_bundle_content_sha256": weather_validation["bundle_sha256"],
        "value_bundle_content_sha256": value_validation["value_sha256"],
        "source_snapshot_manifest_sha256": weather_validation["source_snapshot_manifest_sha256"],
        "conversion_parameters": {
            "entry_valid_minutes": int(entry_valid_minutes),
            "formal_mode": bool(formal_mode),
            "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL,
        },
        "accepted_candidates": value_validation["accepted"],
        "rejected_candidates": value_validation["rejected"],
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
    }


def _write_run_artifacts(
    work_dir: Path,
    weather: dict[str, Any],
    value: dict[str, Any],
    weather_validation: dict[str, Any],
    value_validation: dict[str, Any],
    *,
    entry_valid_minutes: int,
    formal_mode: bool,
) -> dict[str, Any]:
    weather_path = work_dir / "weather_probability_bundle.json"
    value_path = work_dir / "value_signal_bundle.json"
    core_path = work_dir / "bridge_manifest_core.json"
    csv_path = work_dir / "husky_entry_signals.csv"
    report_path = work_dir / "validation_report.json"
    manifest_path = work_dir / "bridge_manifest.json"
    detached_path = work_dir / "bridge_manifest.sha256"

    write_json(weather_path, weather)
    write_json(value_path, value)

    core = _build_core_manifest(
        weather,
        weather_validation,
        value_validation,
        entry_valid_minutes=entry_valid_minutes,
        formal_mode=formal_mode,
    )
    validate_against_schema(core, "core_manifest")
    write_json(core_path, core)
    core_sha = sha256_file(core_path)

    rows = value_candidates_to_husky_csv_rows(
        weather,
        value_validation,
        entry_valid_minutes=entry_valid_minutes,
        weather_sha256=weather_validation["bundle_sha256"],
        value_sha256=value_validation["value_sha256"],
        bridge_manifest_sha256=core_sha,
    )
    write_husky_csv(csv_path, rows)

    report = _build_validation_report(weather, weather_validation, value_validation, rows)
    write_json(report_path, report)

    rejection_reasons = [f"{r['code']}:{r['message']}" for r in value_validation["rejected"]]
    manifest = {
        "bridge_version": BRIDGE_VERSION,
        "forecast_run_id": str(weather["forecast_run_id"]),
        "model_version": str(weather["model_version"]),
        "rules_version": str(weather["rules_version"]),
        "station": weather_validation["station"],
        "city": weather_validation["city"],
        "weather_date_local": str(weather["weather_date_local"]),
        "weather_metric": weather_validation["weather_metric"],
        "as_of_time_utc": str(weather["as_of_time_utc"]),
        "as_of_time_cst": str(weather["as_of_time_cst"]),
        "data_status": value_validation["data_status"],
        "converted_signal_count": len(rows),
        "rejected_signal_count": value_validation["rejected_count"],
        "rejection_reasons": rejection_reasons,
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
        "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL,
        "input_content_hashes": {
            "weather_probability_bundle": weather_validation["bundle_sha256"],
            "value_signal_bundle": value_validation["value_sha256"],
            "source_snapshot_manifest": weather_validation["source_snapshot_manifest_sha256"],
        },
        "files": {
            "weather_probability_bundle.json": {"path": "weather_probability_bundle.json", "sha256": sha256_file(weather_path)},
            "value_signal_bundle.json": {"path": "value_signal_bundle.json", "sha256": sha256_file(value_path)},
            "bridge_manifest_core.json": {"path": "bridge_manifest_core.json", "sha256": core_sha},
            "husky_entry_signals.csv": {"path": "husky_entry_signals.csv", "sha256": sha256_file(csv_path)},
            "validation_report.json": {"path": "validation_report.json", "sha256": sha256_file(report_path)},
        },
    }
    validate_against_schema(manifest, "final_manifest")
    write_json(manifest_path, manifest)
    detached_path.write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")

    verified = verify_bridge_output(work_dir)
    if not verified["ok"]:
        raise BridgeError("OUTPUT_SELF_CHECK_FAILED", "generated output failed verify-output", {"errors": verified["errors"]})

    return {
        "manifest": manifest,
        "core_sha256": core_sha,
        "manifest_sha256": verified["manifest_sha256"],
        "husky_csv": str(csv_path),
        "converted_signal_count": len(rows),
        "rejected_signal_count": value_validation["rejected_count"],
        "validation_report": report,
    }


def convert_bundles(
    weather: dict[str, Any],
    value: dict[str, Any],
    output_root: Path,
    *,
    formal_mode: bool = True,
    entry_valid_minutes: int = 10,
) -> dict[str, Any]:
    forecast_run_id = str(weather.get("forecast_run_id") or "")
    if not forecast_run_id:
        raise BridgeError("MISSING_FORECAST_RUN_ID", "weather.forecast_run_id required")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    out_dir = output_dir_for_run(output_root, forecast_run_id)

    weather_validation = validate_weather_probability_bundle(weather, formal_mode=formal_mode)
    if weather_validation["data_status"] == "LEAKAGE_INVALID":
        raise BridgeError("LEAKAGE_INVALID", "refusing to convert LEAKAGE_INVALID weather bundle")
    if weather_validation.get("formal_blocked"):
        raise BridgeError("FORMAL_TIME_WINDOW_BLOCKED", "generated_at_utc outside formal 15:00-15:05 window")
    value_validation = validate_value_signal_bundle(
        weather, value, formal_mode=formal_mode, weather_validation=weather_validation
    )

    weather_sha = weather_validation["bundle_sha256"]
    value_sha = value_validation["value_sha256"]

    if out_dir.exists():
        missing = _required_files_present(out_dir)
        if missing:
            raise BridgeError(
                "INCOMPLETE_EXISTING_OUTPUT",
                "existing forecast_run_id directory is incomplete; refusing overwrite",
                {"missing": missing, "output_dir": str(out_dir)},
            )
        verified = verify_bridge_output(out_dir)
        if not verified["ok"]:
            raise BridgeError(
                "CORRUPT_EXISTING_OUTPUT",
                "existing forecast_run_id output failed integrity verification",
                {"errors": verified["errors"], "output_dir": str(out_dir)},
            )
        existing = verified["manifest"] or load_json(out_dir / "bridge_manifest.json")
        existing_hashes = existing.get("input_content_hashes") or {}
        if (
            existing_hashes.get("weather_probability_bundle") == weather_sha
            and existing_hashes.get("value_signal_bundle") == value_sha
        ):
            return {
                "status": "reused",
                "output_dir": str(out_dir),
                "manifest": existing,
                "core_sha256": verified["core_sha256"],
                "manifest_sha256": verified["manifest_sha256"],
                "husky_csv": str(out_dir / "husky_entry_signals.csv"),
                "converted_signal_count": existing.get("converted_signal_count"),
                "rejected_signal_count": existing.get("rejected_signal_count"),
            }
        raise BridgeError(
            "FORECAST_RUN_ID_CONFLICT",
            f"forecast_run_id already exists with different content: {forecast_run_id}",
            {"output_dir": str(out_dir)},
        )

    tmp_dir = output_root / f".tmp_{out_dir.name}_{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        written = _write_run_artifacts(
            tmp_dir,
            weather,
            value,
            weather_validation,
            value_validation,
            entry_valid_minutes=entry_valid_minutes,
            formal_mode=formal_mode,
        )
        os.replace(tmp_dir, out_dir)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return {
        "status": "created",
        "output_dir": str(out_dir),
        "manifest": written["manifest"],
        "validation_report": written["validation_report"],
        "husky_csv": str(out_dir / "husky_entry_signals.csv"),
        "converted_signal_count": written["converted_signal_count"],
        "rejected_signal_count": written["rejected_signal_count"],
        "core_sha256": written["core_sha256"],
        "manifest_sha256": sha256_file(out_dir / "bridge_manifest.json"),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="d1_signal_bridge_v1")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("validate-weather")
    sp.add_argument("--input", required=True)
    sp.add_argument("--allow-informal", action="store_true")

    sp = sub.add_parser("validate-value")
    sp.add_argument("--weather", required=True)
    sp.add_argument("--value", required=True)
    sp.add_argument("--allow-informal", action="store_true")

    sp = sub.add_parser("convert")
    sp.add_argument("--weather", required=True)
    sp.add_argument("--value", required=True)
    sp.add_argument("--output-root", required=True)
    sp.add_argument("--entry-valid-minutes", type=int, default=10)
    sp.add_argument("--allow-informal", action="store_true")

    sp = sub.add_parser("verify-output")
    sp.add_argument("--output-dir", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-weather":
            weather = load_json(Path(args.input))
            result = validate_weather_probability_bundle(weather, formal_mode=not args.allow_informal)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "validate-value":
            weather = load_json(Path(args.weather))
            value = load_json(Path(args.value))
            result = validate_value_signal_bundle(weather, value, formal_mode=not args.allow_informal)
            printable = {k: result[k] for k in result if k != "accepted"}
            printable["accepted_count"] = result["accepted_count"]
            print(json.dumps(printable, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "convert":
            weather = load_json(Path(args.weather))
            value = load_json(Path(args.value))
            result = convert_bundles(
                weather,
                value,
                Path(args.output_root),
                formal_mode=not args.allow_informal,
                entry_valid_minutes=args.entry_valid_minutes,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
            return 0
        if args.command == "verify-output":
            result = verify_bridge_output(Path(args.output_dir))
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
            return 0 if result["ok"] else 2
    except BridgeError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": exc.message, "details": exc.details},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
