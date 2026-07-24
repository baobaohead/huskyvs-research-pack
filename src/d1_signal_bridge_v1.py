#!/usr/bin/env python3
"""D1 weather probability → value signal → Husky entry-signal bridge (v1).

This module is intentionally separate from the RC7 forward-simulation engine.
It only validates and converts standardized D1 signal bundles. It never:
starts formal mode, writes a formal ledger, connects a wallet, signs, or places orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from src.polymarket_public_adapter_v5_1_8 import parse_temperature_bucket
except ModuleNotFoundError:
    from polymarket_public_adapter_v5_1_8 import parse_temperature_bucket


BRIDGE_VERSION = "d1_signal_bridge_v1"
RULES_VERSION_DEFAULT = "D1_manual_v1.0"
MODEL_FORMAL_NAME = "D1_1500"
SOURCE_TAG = "d1_signal_bridge_v1"
ALLOWED_STATIONS = {"ZSPD", "ZBAA"}
STATION_CITY = {"ZSPD": "Shanghai", "ZBAA": "Beijing"}
CST = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
ZERO = Decimal("0")
ONE = Decimal("1")
PROB_EPS = Decimal("0.0000001")
EDGE_EPS = Decimal("0.0000001")
ALLOWED_DATA_STATUS = {"COMPLETE", "PARTIAL", "CONFLICTING", "STALE", "LEAKAGE_INVALID"}
NON_MARKETABLE_BUCKETS = {"其他", "其它", "other", "OTHER", "tail", "TAIL", "remainder", "REMAINDER"}

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
    return sha256_bytes(path.read_bytes())


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256_bytes(stable_json(value).encode("utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_iso_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeError("INVALID_TIMESTAMP", "empty timestamp")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise BridgeError("INVALID_TIMESTAMP", f"timezone required: {value}")
    return dt.astimezone(UTC)


def parse_iso_cst(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise BridgeError("INVALID_TIMESTAMP", "empty cst timestamp")
    if raw.endswith("Z"):
        raise BridgeError("INVALID_TIMESTAMP", f"CST timestamp must not use Z: {value}")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


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
    # Prefer spaced forms first so "32C or below" / "32C or higher" parse correctly.
    canonical = parse_temperature_bucket(text)
    if canonical:
        return canonical
    compact = re.sub(r"\s+", "", text)
    # bare integer temperature → exact:NC
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
    return (as_of_cst.astimezone(CST).date() + timedelta(days=1))


def validate_d1_1500_time_fields(
    as_of_time_utc: str,
    as_of_time_cst: str,
    weather_date_local: str,
    generated_at_utc: str | None = None,
    *,
    formal_mode: bool = True,
) -> dict[str, Any]:
    """Validate formal D1_1500 time binding.

    Returns status flags. Raises BridgeError on hard formal violations.
    """
    as_of_cst = parse_iso_cst(as_of_time_cst)
    as_of_utc = parse_iso_utc(as_of_time_utc)
    issues: list[str] = []
    warnings: list[str] = []

    if as_of_cst.hour != 15 or as_of_cst.minute != 0 or as_of_cst.second != 0:
        issues.append("as_of_time_cst_not_1500")
    expected_utc = as_of_cst.astimezone(UTC)
    if as_of_utc != expected_utc:
        issues.append("as_of_utc_cst_mismatch")
    try:
        weather_day = date.fromisoformat(str(weather_date_local))
    except ValueError as exc:
        raise BridgeError("INVALID_WEATHER_DATE", f"invalid weather_date_local: {weather_date_local}") from exc
    if weather_day != expected_next_day(as_of_cst):
        issues.append("weather_date_local_not_next_day")

    generated_late = False
    if generated_at_utc:
        generated = parse_iso_utc(generated_at_utc)
        window_end = expected_utc + timedelta(minutes=5)
        if generated < expected_utc:
            warnings.append("generated_at_before_as_of")
        if generated > window_end:
            generated_late = True
            warnings.append("generated_at_after_1505_window")

    if formal_mode and issues:
        raise BridgeError("D1_1500_TIME_INVALID", ";".join(issues), {"issues": issues, "warnings": warnings})
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "generated_late": generated_late,
        "as_of_utc": as_of_utc.isoformat(),
        "as_of_cst": as_of_cst.isoformat(),
        "weather_date_local": weather_day.isoformat(),
    }


def _collect_source_times(obj: Any, out: list[datetime]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lk = str(key).lower()
            if any(tok in lk for tok in ("acquired_at", "released_at", "published_at", "captured_at", "source_time")):
                if isinstance(value, str) and value.strip():
                    try:
                        out.append(parse_iso_utc(value))
                    except BridgeError:
                        pass
            _collect_source_times(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_source_times(item, out)


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

    data_status = str(bundle["data_status"]).upper()
    if data_status not in ALLOWED_DATA_STATUS:
        raise BridgeError("INVALID_DATA_STATUS", f"unsupported data_status: {data_status}")

    time_info = validate_d1_1500_time_fields(
        str(bundle["as_of_time_utc"]),
        str(bundle["as_of_time_cst"]),
        str(bundle["weather_date_local"]),
        str(bundle.get("generated_at_utc") or ""),
        formal_mode=formal_mode,
    )
    warnings.extend(time_info["warnings"])
    if time_info["generated_late"] and formal_mode:
        # Formal conversion blocked, but keep as explicit warning/status path.
        warnings.append("FORMAL_GENERATED_AT_OUT_OF_WINDOW")

    as_of_utc = parse_iso_utc(str(bundle["as_of_time_utc"]))
    source_times: list[datetime] = []
    _collect_source_times(bundle.get("source_snapshot_manifest"), source_times)
    leakage = False
    for ts in source_times:
        if ts > as_of_utc:
            leakage = True
            break
    if leakage:
        data_status = "LEAKAGE_INVALID"

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
        normalized_rows.append({"temperature_bucket": bucket, "forecast_probability": dstr(p), "marketable": is_marketable_bucket(bucket)})

    if abs(total - ONE) > PROB_EPS:
        raise BridgeError("PROBABILITY_SUM_INVALID", f"probability sum must be 1, got {total}")

    if formal_mode and str(bundle.get("model_version")) != MODEL_FORMAL_NAME and "D1_1500" not in str(bundle.get("model_version")):
        # Allow model_version containing D1_1500; otherwise warn for formal.
        warnings.append("model_version_not_D1_1500")

    result = {
        "ok": True,
        "data_status": data_status,
        "station": station,
        "city": str(bundle.get("city") or STATION_CITY.get(station, "")),
        "warnings": warnings,
        "normalized_probabilities": normalized_rows,
        "probability_sum": dstr(total),
        "formal_blocked": bool(time_info["generated_late"] and formal_mode) or data_status == "LEAKAGE_INVALID",
        "time_info": time_info,
        "bundle_sha256": content_hash(bundle),
    }
    return result


def validate_value_signal_bundle(
    weather: dict[str, Any],
    value: dict[str, Any],
    *,
    formal_mode: bool = True,
    weather_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    weather_validation = weather_validation or validate_weather_probability_bundle(weather, formal_mode=formal_mode)
    if weather_validation["data_status"] == "LEAKAGE_INVALID" or weather_validation.get("formal_blocked"):
        raise BridgeError("LEAKAGE_INVALID", "weather bundle is not eligible for value/execution conversion", {"weather": weather_validation})

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

    if str(value["forecast_run_id"]) != str(weather["forecast_run_id"]):
        raise BridgeError("FORECAST_RUN_ID_MISMATCH", "value forecast_run_id does not match weather bundle")
    if str(value["weather_bundle_sha256"]) != weather_validation["bundle_sha256"]:
        raise BridgeError("WEATHER_HASH_MISMATCH", "value.weather_bundle_sha256 does not match weather bundle content hash")

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
            if not str(raw.get("orderbook_snapshot_sha256") or "").strip():
                raise BridgeError("ORDERBOOK_HASH_MISSING", "orderbook_snapshot_sha256 required")
            status = str(raw.get("data_status") or value.get("data_status") or weather_validation["data_status"]).upper()
            if status == "LEAKAGE_INVALID":
                raise BridgeError("LEAKAGE_INVALID", "candidate marked LEAKAGE_INVALID")
            if status not in ALLOWED_DATA_STATUS:
                raise BridgeError("INVALID_DATA_STATUS", f"unsupported candidate data_status: {status}")
            # Preserve non-complete statuses; never upgrade to COMPLETE.
            parent_status = weather_validation["data_status"]
            if parent_status != "COMPLETE" and status == "COMPLETE":
                raise BridgeError("STATUS_UPGRADE_FORBIDDEN", f"cannot upgrade {parent_status} to COMPLETE")
            accepted.append(
                {
                    "forecast_run_id": str(value["forecast_run_id"]),
                    "model_version": str(value["model_version"]),
                    "rules_version": str(value["rules_version"]),
                    "station": str(value["station"]).upper(),
                    "city": str(value.get("city") or weather.get("city") or ""),
                    "weather_date_local": str(value["weather_date_local"]),
                    "weather_metric": str(value["weather_metric"]),
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
                    "data_status": status if status != "COMPLETE" else parent_status,
                    "orderbook_snapshot_id": str(raw["orderbook_snapshot_id"]),
                    "orderbook_snapshot_sha256": str(raw["orderbook_snapshot_sha256"]),
                    "orderbook_captured_at_utc": captured.isoformat(),
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
        "data_status": weather_validation["data_status"],
    }


def build_notes(audit: dict[str, str]) -> str:
    parts = [f"{k}={v}" for k, v in audit.items()]
    return ";".join(parts)


def value_candidates_to_husky_csv_rows(
    weather: dict[str, Any],
    value_validation: dict[str, Any],
    *,
    entry_valid_minutes: int = 10,
    weather_sha256: str,
    value_sha256: str,
    bridge_manifest_sha256: str = "pending",
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
    out_dir = output_dir_for_run(output_root, forecast_run_id)
    weather_path = out_dir / "weather_probability_bundle.json"
    value_path = out_dir / "value_signal_bundle.json"
    csv_path = out_dir / "husky_entry_signals.csv"
    manifest_path = out_dir / "bridge_manifest.json"
    report_path = out_dir / "validation_report.json"

    weather_sha = content_hash(weather)
    value_sha = content_hash(value)

    if out_dir.exists() and manifest_path.exists():
        existing = load_json(manifest_path)
        existing_hashes = existing.get("input_content_hashes") or {}
        existing_weather_sha = existing_hashes.get("weather_probability_bundle")
        existing_value_sha = existing_hashes.get("value_signal_bundle")
        # Fallback for older manifests: re-hash stored JSON bodies.
        if not existing_weather_sha and (out_dir / "weather_probability_bundle.json").exists():
            existing_weather_sha = content_hash(load_json(out_dir / "weather_probability_bundle.json"))
        if not existing_value_sha and (out_dir / "value_signal_bundle.json").exists():
            existing_value_sha = content_hash(load_json(out_dir / "value_signal_bundle.json"))
        if existing_weather_sha == weather_sha and existing_value_sha == value_sha:
            return {"status": "reused", "output_dir": str(out_dir), "manifest": existing}
        raise BridgeError(
            "FORECAST_RUN_ID_CONFLICT",
            f"forecast_run_id already exists with different content: {forecast_run_id}",
            {"output_dir": str(out_dir)},
        )

    weather_validation = validate_weather_probability_bundle(weather, formal_mode=formal_mode)
    if weather_validation["data_status"] == "LEAKAGE_INVALID":
        raise BridgeError("LEAKAGE_INVALID", "refusing to convert LEAKAGE_INVALID weather bundle")
    if weather_validation.get("formal_blocked"):
        raise BridgeError("FORMAL_TIME_WINDOW_BLOCKED", "generated_at_utc outside formal 15:00-15:05 window")

    value_validation = validate_value_signal_bundle(weather, value, formal_mode=formal_mode, weather_validation=weather_validation)

    # Write weather/value first, then temporary CSV with pending manifest hash, then finalize.
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(weather_path, weather)
    write_json(value_path, value)

    provisional_rows = value_candidates_to_husky_csv_rows(
        weather,
        value_validation,
        entry_valid_minutes=entry_valid_minutes,
        weather_sha256=weather_sha,
        value_sha256=value_sha,
        bridge_manifest_sha256="pending",
    )
    write_husky_csv(csv_path, provisional_rows)

    generated_at = datetime.now(tz=UTC).isoformat()
    rejection_reasons = [f"{r['code']}:{r['message']}" for r in value_validation["rejected"]]
    manifest = {
        "bridge_version": BRIDGE_VERSION,
        "generated_at_utc": generated_at,
        "forecast_run_id": forecast_run_id,
        "model_version": str(weather.get("model_version")),
        "rules_version": str(weather.get("rules_version")),
        "station": str(weather.get("station")).upper(),
        "weather_date_local": str(weather.get("weather_date_local")),
        "as_of_time_utc": str(weather.get("as_of_time_utc")),
        "data_status": weather_validation["data_status"],
        "converted_signal_count": len(provisional_rows),
        "rejected_signal_count": value_validation["rejected_count"],
        "rejection_reasons": rejection_reasons,
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
        "input_content_hashes": {
            "weather_probability_bundle": weather_sha,
            "value_signal_bundle": value_sha,
        },
        "input_files": {
            "weather_probability_bundle": "inline",
            "value_signal_bundle": "inline",
        },
        "output_files": {
            "weather_probability_bundle.json": str(weather_path),
            "value_signal_bundle.json": str(value_path),
            "husky_entry_signals.csv": str(csv_path),
            "bridge_manifest.json": str(manifest_path),
            "validation_report.json": str(report_path),
        },
        "files": {
            "weather_probability_bundle.json": {"sha256": sha256_file(weather_path), "path": str(weather_path)},
            "value_signal_bundle.json": {"sha256": sha256_file(value_path), "path": str(value_path)},
            "husky_entry_signals.csv": {"sha256": sha256_file(csv_path), "path": str(csv_path)},
        },
    }
    # Rewrite CSV notes with real manifest hash once known from content without circular dependency:
    # hash manifest without its own sha, then stamp CSV, then recompute csv hash into manifest.
    manifest_body_for_hash = deepcopy(manifest)
    manifest_sha = content_hash(manifest_body_for_hash)
    final_rows = value_candidates_to_husky_csv_rows(
        weather,
        value_validation,
        entry_valid_minutes=entry_valid_minutes,
        weather_sha256=weather_sha,
        value_sha256=value_sha,
        bridge_manifest_sha256=manifest_sha,
    )
    write_husky_csv(csv_path, final_rows)
    manifest["files"]["husky_entry_signals.csv"] = {"sha256": sha256_file(csv_path), "path": str(csv_path)}
    manifest["bridge_manifest_content_sha256"] = manifest_sha
    write_json(manifest_path, manifest)
    manifest["files"]["bridge_manifest.json"] = {"sha256": sha256_file(manifest_path), "path": str(manifest_path)}

    report = {
        "generated_at_utc": generated_at,
        "weather_validation": {
            "data_status": weather_validation["data_status"],
            "warnings": weather_validation["warnings"],
            "probability_sum": weather_validation["probability_sum"],
            "bundle_sha256": weather_validation["bundle_sha256"],
        },
        "value_validation": {
            "accepted_count": value_validation["accepted_count"],
            "rejected_count": value_validation["rejected_count"],
            "rejected": value_validation["rejected"],
            "value_sha256": value_validation["value_sha256"],
        },
        "converted_signal_count": len(final_rows),
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
    }
    write_json(report_path, report)
    manifest["files"]["validation_report.json"] = {"sha256": sha256_file(report_path), "path": str(report_path)}
    write_json(manifest_path, manifest)

    return {
        "status": "created",
        "output_dir": str(out_dir),
        "manifest": manifest,
        "validation_report": report,
        "husky_csv": str(csv_path),
        "converted_signal_count": len(final_rows),
        "rejected_signal_count": value_validation["rejected_count"],
    }


def verify_bridge_output(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    manifest = load_json(output_dir / "bridge_manifest.json")
    errors: list[str] = []
    for name, meta in (manifest.get("files") or {}).items():
        path = Path(meta["path"])
        if not path.exists():
            # allow relative to output_dir
            path = output_dir / name
        if not path.exists():
            errors.append(f"missing:{name}")
            continue
        actual = sha256_file(path)
        if name != "bridge_manifest.json" and actual != meta.get("sha256"):
            errors.append(f"hash_mismatch:{name}")
    return {"ok": not errors, "errors": errors, "manifest": manifest}


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
            print(json.dumps({k: result[k] for k in result if k != "accepted"} | {"accepted_count": result["accepted_count"]}, ensure_ascii=False, indent=2, sort_keys=True))
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
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
