"""Self-contained D1 value evidence contract v2.

V2 deliberately validates raw Gamma/CLOB and orderbook evidence again instead
of treating hashes as assertions.  It is read-only and has no network path.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ModuleNotFoundError:  # pragma: no cover
    Draft202012Validator = None  # type: ignore
    FormatChecker = None  # type: ignore

try:
    from src.d1_signal_bridge_v1 import (BridgeError, _validate_status_no_upgrade, assert_sha256_hex, content_hash as bridge_hash, dstr, normalize_city_for_station, normalize_temp_bucket_label, normalize_weather_metric, parse_iso_utc, validate_d1_1500_time_fields, validate_weather_probability_bundle)
    from src.polymarket_public_adapter_v5_1_8 import CLOB_BASE, NORMALIZED_BOOK_ALGORITHM_VERSION, content_hash, dstr as adapter_dstr, normalize_orderbook
except ModuleNotFoundError:  # pragma: no cover
    from d1_signal_bridge_v1 import (BridgeError, _validate_status_no_upgrade, assert_sha256_hex, content_hash as bridge_hash, dstr, normalize_city_for_station, normalize_temp_bucket_label, normalize_weather_metric, parse_iso_utc, validate_d1_1500_time_fields, validate_weather_probability_bundle)
    from polymarket_public_adapter_v5_1_8 import CLOB_BASE, NORMALIZED_BOOK_ALGORITHM_VERSION, content_hash, dstr as adapter_dstr, normalize_orderbook

SCHEMA_VERSION = "2.0"
ORDERBOOK_HASH_VALIDATION_LEVEL = "self_contained_semantic_replay"
SOURCE_TAG = "d1_signal_bridge_v2"
UTC = timezone.utc
CST = ZoneInfo("Asia/Shanghai")


def _error(code: str, msg: str, **details: Any) -> None:
    raise BridgeError(code, msg, details)


def validate_v2_schema(value: Any) -> dict[str, Any]:
    if Draft202012Validator is None or FormatChecker is None:
        _error("JSON_SCHEMA_DEPENDENCY_MISSING", "jsonschema is required")
    path = Path(__file__).resolve().parents[1] / "schemas" / "d1_value_signal_v2.schema.json"
    import json
    schema = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        _error("VALUE_V2_SCHEMA_INVALID", err.message, json_path="/" + "/".join(map(str, err.absolute_path)))
    return {"ok": True, "validator": "Draft202012Validator", "format_checker": True}


def _require_evidence_string(value: Any, *, source: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _error(
            "EVIDENCE_STRING_INVALID",
            f"{source} {field} must be a non-empty unpadded string",
            source=source,
            field=field,
        )
    return value


def _strict_scalar_alias(
    raw: dict[str, Any],
    names: tuple[str, ...],
    *,
    source: str,
    alias_group: str,
) -> str | None:
    present = [(name, raw[name]) for name in names if name in raw]
    if not present:
        return None
    values = [
        _require_evidence_string(value, source=source, field=name)
        for name, value in present
    ]
    if any(value != values[0] for value in values[1:]):
        _error(
            "EVIDENCE_ALIAS_CONFLICT",
            f"{source} {alias_group} aliases conflict",
            source=source,
            alias_group=alias_group,
            present_aliases=[name for name, _ in present],
        )
    return values[0]


def _require_evidence_bool(value: Any, *, source: str, field: str) -> bool:
    if type(value) is not bool:
        _error(
            "EVIDENCE_BOOLEAN_INVALID",
            f"{source} {field} must be a JSON boolean",
            source=source,
            field=field,
        )
    return value


def _strict_bool_alias(
    raw: dict[str, Any],
    names: tuple[str, ...],
    *,
    source: str,
    alias_group: str,
) -> bool | None:
    present = [(name, raw[name]) for name in names if name in raw]
    if not present:
        return None
    values = [
        _require_evidence_bool(value, source=source, field=name)
        for name, value in present
    ]
    if any(value is not values[0] for value in values[1:]):
        _error(
            "EVIDENCE_ALIAS_CONFLICT",
            f"{source} {alias_group} aliases conflict",
            source=source,
            alias_group=alias_group,
            present_aliases=[name for name, _ in present],
        )
    return values[0]


def _as_array(value: Any) -> list[Any] | None:
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except Exception:
            return None
    return value if isinstance(value, list) else None


def _strict_array_alias(
    raw: dict[str, Any],
    names: tuple[str, ...],
    *,
    source: str,
    alias_group: str,
) -> list[Any] | None:
    present = [(name, raw[name]) for name in names if name in raw]
    if not present:
        return None
    parsed = [(name, _as_array(value)) for name, value in present]
    if any(value is None for _, value in parsed):
        return None
    first = parsed[0][1]
    if any(value != first for _, value in parsed[1:]):
        _error(
            "EVIDENCE_ALIAS_CONFLICT",
            f"{source} {alias_group} aliases conflict",
            source=source,
            alias_group=alias_group,
            present_aliases=[name for name, _ in present],
        )
    return first


def _extract_exact_outcome_token_pairs(
    raw: dict[str, Any],
    *,
    source: str,
) -> list[tuple[str, str]]:
    outcomes = _strict_array_alias(
        raw,
        ("outcomes", "outcome"),
        source=source,
        alias_group="outcomes",
    )
    tokens = _strict_array_alias(
        raw,
        ("clobTokenIds", "clob_token_ids", "tokens", "tokenIds"),
        source=source,
        alias_group="tokens",
    )
    if (
        not outcomes
        or not tokens
        or len(outcomes) != len(tokens)
        or any(not isinstance(item, str) for item in outcomes)
        or any(not isinstance(item, str) for item in tokens)
    ):
        _error(
            "MARKET_OUTCOME_TOKEN_CARDINALITY_MISMATCH",
            f"{source} outcomes and tokens must be non-empty, exact, equal-length string arrays",
            source=source,
            outcome_count=len(outcomes or []),
            token_count=len(tokens or []),
        )
    outcomes = [
        _require_evidence_string(item, source=source, field=f"outcomes[{index}]")
        for index, item in enumerate(outcomes)
    ]
    tokens = [
        _require_evidence_string(item, source=source, field=f"tokens[{index}]")
        for index, item in enumerate(tokens)
    ]
    if len(set(outcomes)) != len(outcomes):
        _error(
            "MARKET_OUTCOME_DUPLICATE",
            f"{source} outcomes must be unique",
            source=source,
        )
    if len(set(tokens)) != len(tokens):
        _error(
            "MARKET_TOKEN_DUPLICATE",
            f"{source} tokens must be unique",
            source=source,
        )
    return [(outcomes[index], tokens[index]) for index in range(len(outcomes))]


def _assert_eq(actual: Any, expected: Any, code: str, field: str) -> None:
    if actual != expected:
        _error(code, f"{field} does not match replayed evidence", actual=actual, expected=expected)


def _validate_pre_schema_security_fields(value: Any) -> None:
    """Give security-critical type failures stable, contract-specific errors."""
    if not isinstance(value, dict):
        return
    manifest = value.get("market_snapshot_manifest")
    if isinstance(manifest, dict):
        for field in (
            "market_slug",
            "event_id",
            "question",
            "city",
            "weather_date_local",
            "weather_metric",
            "condition_id",
        ):
            if field in manifest:
                _require_evidence_string(
                    manifest[field],
                    source="market_manifest",
                    field=field,
                )
        for field in ("active", "closed", "accepting_orders"):
            if field in manifest:
                _require_evidence_bool(
                    manifest[field],
                    source="market_manifest",
                    field=field,
                )
        for field in ("outcomes", "clob_token_ids"):
            items = manifest.get(field)
            if isinstance(items, list):
                for index, item in enumerate(items):
                    _require_evidence_string(
                        item,
                        source="market_manifest",
                        field=f"{field}[{index}]",
                    )
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        for index, candidate in enumerate(candidates):
            if isinstance(candidate, dict) and "market_slug" in candidate:
                _require_evidence_string(
                    candidate["market_slug"],
                    source=f"candidate:{index}",
                    field="market_slug",
                )


def _validate_market(
    value: dict[str, Any],
    as_of: datetime,
    *,
    weather_city: str,
    weather_date_local: str,
    weather_metric: str,
) -> dict[str, Any]:
    m = value["market_snapshot_manifest"]
    if content_hash(m) != assert_sha256_hex(value["market_snapshot_sha256"], field="market_snapshot_sha256"):
        _error("MARKET_SNAPSHOT_HASH_MISMATCH", "market_snapshot_sha256 must replay from manifest")
    if m.get("method") != "GET": _error("MARKET_IDENTITY_REPLAY_MISMATCH", "market method must be GET")
    captured = parse_iso_utc(m["captured_at_utc"])
    if captured > as_of: _error("VALUE_V2_LEAKAGE_INVALID", "market captured after as_of_time_utc")
    gamma, clob = m["gamma_market_payload"], m["clob_market_payload"]
    if content_hash(gamma) != assert_sha256_hex(m["gamma_payload_sha256"], field="gamma_payload_sha256"):
        _error("MARKET_IDENTITY_REPLAY_MISMATCH", "gamma payload hash mismatch")
    if content_hash(clob) != assert_sha256_hex(m["clob_payload_sha256"], field="clob_payload_sha256"):
        _error("MARKET_IDENTITY_REPLAY_MISMATCH", "clob payload hash mismatch")
    manifest_strings = {
        field: _require_evidence_string(
            m[field],
            source="market_manifest",
            field=field,
        )
        for field in (
            "market_slug",
            "event_id",
            "question",
            "city",
            "weather_date_local",
            "weather_metric",
            "condition_id",
        )
    }
    manifest_bools = {
        field: _require_evidence_bool(
            m[field],
            source="market_manifest",
            field=field,
        )
        for field in ("active", "closed", "accepting_orders")
    }
    manifest_condition = manifest_strings["condition_id"]
    condition = _strict_scalar_alias(
        gamma,
        ("conditionId", "condition_id", "condition"),
        source="gamma",
        alias_group="condition",
    )
    if condition is None:
        _error("MARKET_IDENTITY_REPLAY_MISMATCH", "gamma condition id unavailable")
    _assert_eq(manifest_condition, condition, "MARKET_IDENTITY_REPLAY_MISMATCH", "condition_id")
    for key, names in (
        ("market_slug", ("slug", "market_slug")),
        ("event_id", ("eventId", "event_id", "id")),
        ("question", ("question", "title")),
        ("city", ("city",)),
        ("weather_date_local", ("weatherDateLocal", "weather_date_local")),
        ("weather_metric", ("weatherMetric", "weather_metric")),
    ):
        raw = _strict_scalar_alias(
            gamma,
            names,
            source="gamma",
            alias_group=key,
        )
        if raw is None:
            _error("MARKET_IDENTITY_REPLAY_MISMATCH", f"gamma {key} unavailable")
        _assert_eq(
            manifest_strings[key],
            raw,
            "MARKET_IDENTITY_REPLAY_MISMATCH",
            key,
        )
    for key, names in (
        ("active", ("active",)),
        ("closed", ("closed",)),
        ("accepting_orders", ("acceptingOrders", "accepting_orders")),
    ):
        raw_bool = _strict_bool_alias(
            gamma,
            names,
            source="gamma",
            alias_group=key,
        )
        if raw_bool is None:
            _error("MARKET_IDENTITY_REPLAY_MISMATCH", f"gamma {key} unavailable")
        _assert_eq(
            manifest_bools[key],
            raw_bool,
            "MARKET_IDENTITY_REPLAY_MISMATCH",
            key,
        )
    pairs = _extract_exact_outcome_token_pairs(gamma, source="gamma")
    manifest_outcomes = [
        _require_evidence_string(item, source="market_manifest", field=f"outcomes[{index}]")
        for index, item in enumerate(m["outcomes"])
    ]
    manifest_tokens = [
        _require_evidence_string(item, source="market_manifest", field=f"clob_token_ids[{index}]")
        for index, item in enumerate(m["clob_token_ids"])
    ]
    _assert_eq(manifest_outcomes, [p[0] for p in pairs], "MARKET_IDENTITY_REPLAY_MISMATCH", "outcomes")
    _assert_eq(manifest_tokens, [p[1] for p in pairs], "ORDERBOOK_TOKEN_BINDING_MISMATCH", "clob_token_ids")
    # The selected public CLOB payload must corroborate the chosen condition.
    clob_condition = _strict_scalar_alias(
        clob,
        ("condition_id", "conditionId", "market"),
        source="clob",
        alias_group="condition",
    )
    if clob_condition is None:
        _error(
            "CLOB_MARKET_CONDITION_REQUIRED",
            "CLOB market payload must contain a non-empty condition",
        )
    if clob_condition != condition or clob_condition != manifest_condition:
        _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "CLOB market condition conflicts with Gamma or manifest")
    cpairs = _extract_exact_outcome_token_pairs(clob, source="clob")
    if cpairs != pairs:
        _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "CLOB outcome/token mapping does not replay Gamma mapping")
    if (
        manifest_strings["city"] != weather_city
        or manifest_strings["weather_date_local"] != weather_date_local
        or manifest_strings["weather_metric"] != weather_metric
    ):
        _error(
            "MARKET_WEATHER_IDENTITY_MISMATCH",
            "market snapshot weather identity does not match the validated weather bundle",
        )
    if (
        manifest_bools["active"] is not True
        or manifest_bools["closed"] is not False
        or manifest_bools["accepting_orders"] is not True
    ):
        _error("MARKET_NOT_TRADABLE", "market snapshot is not active and accepting orders")
    return {"manifest": m, "captured": captured, "pairs": dict(pairs)}


def _validate_orderbook_endpoint(endpoint: str, token_id: str, *, snapshot_id: str) -> None:
    try:
        parsed = urlsplit(str(endpoint))
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        _error(
            "ORDERBOOK_TOKEN_BINDING_MISMATCH",
            "orderbook endpoint is malformed",
            snapshot_id=snapshot_id,
        )
    official = urlsplit(CLOB_BASE)
    if (
        parsed.scheme != "https"
        or parsed.hostname != official.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/book"
        or bool(parsed.fragment)
        or set(query) != {"token_id"}
        or len(query["token_id"]) != 1
        or query["token_id"][0] != token_id
    ):
        _error(
            "ORDERBOOK_TOKEN_BINDING_MISMATCH",
            "orderbook endpoint is not exactly bound to the evidence token",
            snapshot_id=snapshot_id,
        )


def _validate_orderbooks(value: dict[str, Any], market: dict[str, Any], as_of: datetime) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for item in value["orderbook_evidence"]:
        sid = _require_evidence_string(
            item["orderbook_snapshot_id"],
            source="orderbook_evidence",
            field="orderbook_snapshot_id",
        )
        evidence_condition = _require_evidence_string(
            item["condition_id"],
            source=f"orderbook_evidence:{sid}",
            field="condition_id",
        )
        evidence_token = _require_evidence_string(
            item["token_id"],
            source=f"orderbook_evidence:{sid}",
            field="token_id",
        )
        if sid in evidence: _error("ORDERBOOK_EVIDENCE_REF_MISSING", "duplicate orderbook snapshot id", snapshot_id=sid)
        if item["method"] != "GET" or item["status_code"] != 200:
            _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "orderbook endpoint/status/method invalid", snapshot_id=sid)
        _validate_orderbook_endpoint(item["endpoint"], evidence_token, snapshot_id=sid)
        captured = parse_iso_utc(item["captured_at_utc"])
        started = parse_iso_utc(item["request_started_at_utc"])
        if started > captured or captured > as_of or captured.astimezone(CST).time() > time(15, 0):
            _error("VALUE_V2_LEAKAGE_INVALID", "orderbook acquisition violates cutoff", snapshot_id=sid)
        if content_hash(item["raw_payload"]) != assert_sha256_hex(item["raw_payload_sha256"], field="raw_payload_sha256"):
            _error("ORDERBOOK_RAW_HASH_MISMATCH", "raw orderbook hash mismatch", snapshot_id=sid)
        if evidence_condition != market["manifest"]["condition_id"] or evidence_token not in market["pairs"].values():
            _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "orderbook token not in market snapshot", snapshot_id=sid)
        raw_token = _strict_scalar_alias(
            item["raw_payload"],
            ("asset_id", "token_id"),
            source=f"raw_orderbook:{sid}",
            alias_group="token",
        )
        if raw_token is None:
            _error("ORDERBOOK_RAW_TOKEN_REQUIRED", "raw orderbook token field is required", snapshot_id=sid)
        raw_condition = _strict_scalar_alias(
            item["raw_payload"],
            ("market", "condition_id", "conditionId"),
            source=f"raw_orderbook:{sid}",
            alias_group="condition",
        )
        if raw_condition is None:
            _error("ORDERBOOK_RAW_CONDITION_REQUIRED", "raw orderbook condition field is required", snapshot_id=sid)
        if raw_token != evidence_token or raw_condition != evidence_condition:
            _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "raw orderbook identity does not match evidence", snapshot_id=sid)
        try:
            normalized = normalize_orderbook(item["raw_payload"], item["token_id"], item["condition_id"], market["manifest"]["gamma_market_payload"])
        except Exception as exc:
            _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", f"orderbook normalization failed: {exc}", snapshot_id=sid)
        if item["normalization_algorithm_version"] != NORMALIZED_BOOK_ALGORITHM_VERSION:
            _error("ORDERBOOK_NORMALIZED_HASH_MISMATCH", "normalization algorithm version mismatch", snapshot_id=sid)
        if content_hash(normalized["normalized_book"]) != assert_sha256_hex(item["normalized_book_sha256"], field="normalized_book_sha256"):
            _error("ORDERBOOK_NORMALIZED_HASH_MISMATCH", "normalized orderbook hash mismatch", snapshot_id=sid)
        if content_hash(item["normalized_book"]) != content_hash(normalized["normalized_book"]):
            _error("ORDERBOOK_NORMALIZED_HASH_MISMATCH", "normalized orderbook payload mismatch", snapshot_id=sid)
        bid, ask = normalized["best_bid"], normalized["best_ask"]
        if (item["best_bid"] is None) != (bid is None) or (bid is not None and Decimal(str(item["best_bid"])) != bid):
            _error("ORDERBOOK_BEST_ASK_MISMATCH", "best_bid mismatch", snapshot_id=sid)
        if (item["best_ask"] is None) != (ask is None) or (ask is not None and Decimal(str(item["best_ask"])) != ask):
            _error("ORDERBOOK_BEST_ASK_MISMATCH", "best_ask mismatch", snapshot_id=sid)
        evidence[sid] = {"item": item, "normalized": normalized, "captured": captured}
    return evidence


def validate_value_signal_bundle_v2(weather: dict[str, Any], value: dict[str, Any], *, formal_mode: bool = True) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("orderbook_evidence") == [] and value.get("candidates"):
        _error("ORDERBOOK_EVIDENCE_REF_MISSING", "candidate references cannot be satisfied by an empty evidence set")
    _validate_pre_schema_security_fields(value)
    validate_v2_schema(value)
    weather_validation = validate_weather_probability_bundle(weather, formal_mode=formal_mode)
    if value.get("schema_version") != SCHEMA_VERSION: _error("VALUE_BUNDLE_VERSION_UNKNOWN", "V2 schema_version required")
    if formal_mode and weather_validation.get("formal_blocked"):
        _error("FORMAL_TIME_WINDOW_BLOCKED", "weather bundle is outside the formal generation window")
    if weather_validation["data_status"] == "LEAKAGE_INVALID" or value["data_status"] == "LEAKAGE_INVALID":
        _error("VALUE_V2_LEAKAGE_INVALID", "LEAKAGE_INVALID cannot produce an executable CSV")
    for key, code in (
        ("forecast_run_id", "FORECAST_RUN_ID_MISMATCH"),
        ("model_version", "MODEL_VERSION_MISMATCH"),
        ("rules_version", "RULES_VERSION_MISMATCH"),
        ("weather_date_local", "WEATHER_DATE_MISMATCH"),
    ):
        _assert_eq(value[key], weather[key], code, key)
    if str(value["station"]).upper() != weather_validation["station"]:
        _error("STATION_MISMATCH", "value station does not match weather bundle")
    if normalize_city_for_station(weather_validation["station"], value["city"]) != weather_validation["city"]:
        _error("CITY_MISMATCH", "value city does not match weather bundle")
    if normalize_weather_metric(value["weather_metric"], formal_mode=formal_mode) != weather_validation["weather_metric"]:
        _error("WEATHER_METRIC_MISMATCH", "value weather metric does not match weather bundle")
    as_of = parse_iso_utc(value["as_of_time_utc"])
    if as_of != parse_iso_utc(weather["as_of_time_utc"]): _error("VALUE_V2_LEAKAGE_INVALID", "as_of must match weather bundle")
    value_time = validate_d1_1500_time_fields(
        value["as_of_time_utc"],
        weather["as_of_time_cst"],
        value["weather_date_local"],
        value["generated_at_utc"],
        formal_mode=formal_mode,
    )
    if formal_mode and value_time["generated_out_of_window"]:
        _error("VALUE_V2_GENERATED_TIME_INVALID", "value bundle is outside the formal generation window")
    weather_generated = parse_iso_utc(weather["generated_at_utc"])
    generated = parse_iso_utc(value["generated_at_utc"])
    if generated < weather_generated:
        _error(
            "VALUE_V2_GENERATED_BEFORE_WEATHER",
            "value bundle cannot be generated before the weather bundle it hashes",
            weather_generated_at_utc=weather["generated_at_utc"],
            value_generated_at_utc=value["generated_at_utc"],
        )
    if value["weather_bundle_sha256"] != weather_validation["bundle_sha256"]:
        _error("WEATHER_HASH_MISMATCH", "weather hash mismatch")
    value_status = _validate_status_no_upgrade(
        weather_validation["data_status"],
        value["data_status"],
        field="value.data_status",
    )
    market = _validate_market(
        value,
        as_of,
        weather_city=weather_validation["city"],
        weather_date_local=str(weather["weather_date_local"]),
        weather_metric=weather_validation["weather_metric"],
    )
    books = _validate_orderbooks(value, market, as_of)
    if (
        generated < as_of
        or generated < market["captured"]
        or any(generated < b["captured"] for b in books.values())
    ):
        _error("VALUE_V2_GENERATED_TIME_INVALID", "value generated_at precedes market or orderbook evidence")
    probabilities = {r["temperature_bucket"]: Decimal(r["forecast_probability"]) for r in weather_validation["normalized_probabilities"] if r["marketable"]}
    accepted, refs = [], set()
    for raw in value["candidates"]:
        ref = _require_evidence_string(
            raw["orderbook_evidence_ref"],
            source="candidate",
            field="orderbook_evidence_ref",
        )
        candidate_snapshot_id = _require_evidence_string(
            raw["orderbook_snapshot_id"],
            source=f"candidate:{ref}",
            field="orderbook_snapshot_id",
        )
        candidate_condition = _require_evidence_string(
            raw["condition_id"],
            source=f"candidate:{ref}",
            field="condition_id",
        )
        candidate_token = _require_evidence_string(
            raw["token_id"],
            source=f"candidate:{ref}",
            field="token_id",
        )
        candidate_outcome = _require_evidence_string(
            raw["outcome"],
            source=f"candidate:{ref}",
            field="outcome",
        )
        candidate_market_slug = _require_evidence_string(
            raw["market_slug"],
            source=f"candidate:{ref}",
            field="market_slug",
        )
        if ref not in books: _error("ORDERBOOK_EVIDENCE_REF_MISSING", "candidate evidence reference missing", reference=ref)
        b = books[ref]["item"]
        if candidate_snapshot_id != ref or candidate_condition != b["condition_id"] or candidate_token != b["token_id"]:
            _error("ORDERBOOK_TOKEN_BINDING_MISMATCH", "candidate/evidence identity mismatch", reference=ref)
        if raw["forecast_run_id"] != value["forecast_run_id"]:
            _error("FORECAST_RUN_ID_MISMATCH", "candidate forecast_run_id does not match value bundle")
        if str(raw["station"]).upper() != weather_validation["station"]:
            _error("STATION_MISMATCH", "candidate station does not match weather bundle")
        if raw["weather_date_local"] != value["weather_date_local"]:
            _error("WEATHER_DATE_MISMATCH", "candidate weather date does not match value bundle")
        if normalize_weather_metric(raw["weather_metric"], formal_mode=formal_mode) != weather_validation["weather_metric"]:
            _error("WEATHER_METRIC_MISMATCH", "candidate weather metric does not match weather bundle")
        candidate_captured = parse_iso_utc(raw["orderbook_captured_at_utc"])
        if candidate_captured != books[ref]["captured"]:
            _error("ORDERBOOK_EVIDENCE_TIME_MISMATCH", "candidate orderbook time does not match referenced evidence")
        candidate_status = _validate_status_no_upgrade(
            value_status,
            raw["data_status"],
            field="candidate.data_status",
        )
        if candidate_market_slug != market["manifest"]["market_slug"] or candidate_outcome != {v:k for k,v in market["pairs"].items()}.get(candidate_token):
            _error("MARKET_IDENTITY_REPLAY_MISMATCH", "candidate market identity mismatch")
        bucket = normalize_temp_bucket_label(raw["temperature_bucket"])
        fp, ask = Decimal(str(raw["forecast_probability"])), Decimal(str(raw["market_ask_price"]))
        if bucket not in probabilities or fp != probabilities[bucket]: _error("MARKET_IDENTITY_REPLAY_MISMATCH", "forecast probability mismatch")
        best_ask = books[ref]["normalized"]["best_ask"]
        if best_ask is None or ask != best_ask: _error("ORDERBOOK_BEST_ASK_MISMATCH", "candidate ask must equal replayed best ask")
        if raw["orderbook_snapshot_sha256"] != b["normalized_book_sha256"] or Decimal(str(raw["edge"])) != fp - ask:
            _error("ORDERBOOK_BEST_ASK_MISMATCH", "candidate orderbook hash or edge mismatch")
        refs.add(ref)
        accepted.append({
            "forecast_run_id": value["forecast_run_id"],
            "model_version": value["model_version"],
            "rules_version": value["rules_version"],
            "station": weather_validation["station"],
            "city": weather_validation["city"],
            "weather_date_local": value["weather_date_local"],
            "weather_metric": weather_validation["weather_metric"],
            "temperature_bucket": bucket,
            "forecast_probability": dstr(fp),
            "market_slug": market["manifest"]["market_slug"],
            "condition_id": b["condition_id"],
            "token_id": b["token_id"],
            "outcome": {v: k for k, v in market["pairs"].items()}[b["token_id"]],
            "market_ask_price": dstr(ask),
            "edge": dstr(fp - ask),
            "recommended_max_price": dstr(Decimal(str(raw["recommended_max_price"]))),
            "intended_usd": dstr(Decimal(str(raw["intended_usd"]))),
            "reason": str(raw["reason"]),
            "data_status": candidate_status,
            "orderbook_snapshot_id": b["orderbook_snapshot_id"],
            "orderbook_snapshot_sha256": b["normalized_book_sha256"],
            "orderbook_captured_at_utc": books[ref]["captured"].isoformat(),
            "orderbook_evidence_ref": ref,
            "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL,
        })
    if refs != set(books): _error("ORDERBOOK_EVIDENCE_ORPHANED", "all orderbook evidence must be referenced", orphaned=sorted(set(books)-refs))
    return {"ok": True, "accepted": accepted, "rejected": [], "accepted_count": len(accepted), "rejected_count": 0, "value_sha256": bridge_hash(value), "weather_sha256": weather_validation["bundle_sha256"], "data_status": value_status, "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL, "schema_runtime": {"validator":"Draft202012Validator", "format_checker":True}}
