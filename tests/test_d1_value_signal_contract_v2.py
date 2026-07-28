"""V2 replay tests use only synthetic, self-contained public payloads."""
from __future__ import annotations
import json
from copy import deepcopy
from pathlib import Path
import sys
import pytest
PROJECT_ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(PROJECT_ROOT))
from src.d1_signal_bridge_v1 import BridgeError, content_hash
from src.d1_value_signal_contract_v2 import validate_value_signal_bundle_v2
from src.polymarket_public_adapter_v5_1_8 import content_hash as adapter_hash, normalize_orderbook, stable_json

def weather():
    source={"sources":[{"name":"GFS","acquired_at_utc":"2026-07-23T06:50:00+00:00"}]}
    return {"forecast_run_id":"d1_v2_zspd","model_version":"D1_1500","rules_version":"D1_manual_v1.0","station":"ZSPD","city":"Shanghai","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","as_of_time_utc":"2026-07-23T07:00:00+00:00","as_of_time_cst":"2026-07-23T15:00:00+08:00","generated_at_utc":"2026-07-23T07:02:00+00:00","data_status":"COMPLETE","confidence":.7,"source_snapshot_manifest":source,"source_snapshot_sha256":content_hash(source),"explanation":"fixture","integer_temperature_probabilities":[{"temperature_bucket":"32C","forecast_probability":.35},{"temperature_bucket":"33C","forecast_probability":.6},{"temperature_bucket":"other","forecast_probability":.05}]}

def bundle():
    w=weather(); gamma={"slug":"shanghai-high","eventId":"event-1","conditionId":"cond-1","question":"Shanghai high temperature","city":"Shanghai","weatherDateLocal":"2026-07-24","weatherMetric":"highest_temperature","active":True,"closed":False,"acceptingOrders":True,"outcomes":["Yes"],"clobTokenIds":["token-1"],"orderPriceMinTickSize":"0.01","orderMinSize":"1"}; clob={"condition_id":"cond-1","outcomes":["Yes"],"clobTokenIds":["token-1"]}; raw={"market":"cond-1","asset_id":"token-1","bids":[{"price":"0.29","size":"10"}],"asks":[{"price":"0.30","size":"10"}],"tick_size":"0.01","min_order_size":"1"}; normalized=normalize_orderbook(raw,"token-1","cond-1",gamma); nbook=json.loads(stable_json(normalized["normalized_book"])); evidence={"orderbook_snapshot_id":"ob-1","condition_id":"cond-1","token_id":"token-1","endpoint":"https://clob.polymarket.com/book?token_id=token-1","method":"GET","status_code":200,"request_started_at_utc":"2026-07-23T06:59:00+00:00","captured_at_utc":"2026-07-23T07:00:00+00:00","raw_payload":raw,"raw_payload_sha256":adapter_hash(raw),"normalized_book":nbook,"normalized_book_sha256":adapter_hash(normalized["normalized_book"]),"normalization_algorithm_version":normalized["normalization_algorithm_version"],"best_bid":.29,"best_ask":.30}; market={"market_slug":"shanghai-high","event_id":"event-1","condition_id":"cond-1","question":"Shanghai high temperature","city":"Shanghai","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","active":True,"closed":False,"accepting_orders":True,"outcomes":["Yes"],"clob_token_ids":["token-1"],"captured_at_utc":"2026-07-23T06:59:30+00:00","method":"GET","gamma_market_payload":gamma,"clob_market_payload":clob,"gamma_payload_sha256":adapter_hash(gamma),"clob_payload_sha256":adapter_hash(clob)}
    return {"schema_version":"2.0","forecast_run_id":w["forecast_run_id"],"model_version":"D1_1500","rules_version":"D1_manual_v1.0","station":"ZSPD","city":"Shanghai","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","as_of_time_utc":w["as_of_time_utc"],"generated_at_utc":"2026-07-23T07:01:00+00:00","data_status":"COMPLETE","weather_bundle_sha256":content_hash(w),"market_snapshot_manifest":market,"market_snapshot_sha256":adapter_hash(market),"orderbook_evidence":[evidence],"candidates":[{"forecast_run_id":w["forecast_run_id"],"station":"ZSPD","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","temperature_bucket":"32C","forecast_probability":.35,"market_slug":"shanghai-high","condition_id":"cond-1","token_id":"token-1","outcome":"Yes","market_ask_price":.30,"edge":.05,"recommended_max_price":.33,"intended_usd":10,"reason":"fixture","data_status":"COMPLETE","orderbook_snapshot_id":"ob-1","orderbook_snapshot_sha256":evidence["normalized_book_sha256"],"orderbook_captured_at_utc":"2026-07-23T07:00:00+00:00","orderbook_evidence_ref":"ob-1"}]},w

def test_valid_v2_contract_replays():
    v,w=bundle(); assert validate_value_signal_bundle_v2(w,v)["accepted_count"]==1

@pytest.mark.parametrize("mutation,code",[("market","MARKET_IDENTITY_REPLAY_MISMATCH"),("clobtoken","ORDERBOOK_TOKEN_BINDING_MISMATCH"),("raw","ORDERBOOK_NORMALIZED_HASH_MISMATCH"),("ask","ORDERBOOK_BEST_ASK_MISMATCH"),("token","ORDERBOOK_TOKEN_BINDING_MISMATCH"),("time","VALUE_V2_LEAKAGE_INVALID"),("missing","ORDERBOOK_EVIDENCE_REF_MISSING"),("orphan","ORDERBOOK_EVIDENCE_ORPHANED"),("downgrade","VALUE_V2_SCHEMA_INVALID")])
def test_coordinated_tampering_is_rejected(mutation,code):
    v,w=bundle()
    if mutation=="market": v["market_snapshot_manifest"]["gamma_market_payload"]["question"]="tampered"; v["market_snapshot_manifest"]["gamma_payload_sha256"]=adapter_hash(v["market_snapshot_manifest"]["gamma_market_payload"]); v["market_snapshot_sha256"]=adapter_hash(v["market_snapshot_manifest"])
    elif mutation=="clobtoken": v["market_snapshot_manifest"]["clob_market_payload"]["clobTokenIds"]=["token-x"]; v["market_snapshot_manifest"]["clob_payload_sha256"]=adapter_hash(v["market_snapshot_manifest"]["clob_market_payload"]); v["market_snapshot_sha256"]=adapter_hash(v["market_snapshot_manifest"])
    elif mutation=="raw": v["orderbook_evidence"][0]["raw_payload"]["asks"][0]["price"]="0.31"; v["orderbook_evidence"][0]["raw_payload_sha256"]=adapter_hash(v["orderbook_evidence"][0]["raw_payload"])
    elif mutation=="ask": v["candidates"][0]["market_ask_price"]=.31; v["candidates"][0]["edge"]=.04
    elif mutation=="token": v["candidates"][0]["token_id"]="token-x"
    elif mutation=="time": v["orderbook_evidence"][0]["captured_at_utc"]="2026-07-23T07:01:00+00:00"; v["candidates"][0]["orderbook_captured_at_utc"]="2026-07-23T07:01:00+00:00"
    elif mutation=="missing": v["orderbook_evidence"]=[]
    elif mutation=="orphan": v["orderbook_evidence"].append(deepcopy(v["orderbook_evidence"][0])); v["orderbook_evidence"][1]["orderbook_snapshot_id"]="ob-2"
    else: v["schema_version"]="1.0"
    with pytest.raises(BridgeError, match=code): validate_value_signal_bundle_v2(w,v)


def rehash_market(value):
    market = value["market_snapshot_manifest"]
    market["gamma_payload_sha256"] = adapter_hash(market["gamma_market_payload"])
    market["clob_payload_sha256"] = adapter_hash(market["clob_market_payload"])
    value["market_snapshot_sha256"] = adapter_hash(market)


def rehash_raw_orderbook(value):
    evidence = value["orderbook_evidence"][0]
    evidence["raw_payload_sha256"] = adapter_hash(evidence["raw_payload"])


def bind_weather_hash(value, weather_bundle):
    value["weather_bundle_sha256"] = content_hash(weather_bundle)


@pytest.mark.parametrize(
    "field,gamma_field,replacement",
    [
        ("city", "city", "Beijing"),
        ("weather_date_local", "weatherDateLocal", "2026-07-25"),
    ],
)
def test_market_weather_identity_survives_coordinated_market_rehash(field, gamma_field, replacement):
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    market[field] = replacement
    market["gamma_market_payload"][gamma_field] = replacement
    rehash_market(value)
    with pytest.raises(BridgeError, match="MARKET_WEATHER_IDENTITY_MISMATCH"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_market_metric_coordinated_rehash_is_rejected_by_contract():
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    market["weather_metric"] = "daily_temperature"
    market["gamma_market_payload"]["weatherMetric"] = "daily_temperature"
    rehash_market(value)
    with pytest.raises(BridgeError, match="VALUE_V2_SCHEMA_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "field,gamma_field,replacement",
    [
        ("active", "active", False),
        ("closed", "closed", True),
        ("accepting_orders", "acceptingOrders", False),
    ],
)
def test_market_must_be_tradable(field, gamma_field, replacement):
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    market[field] = replacement
    market["gamma_market_payload"][gamma_field] = replacement
    rehash_market(value)
    with pytest.raises(BridgeError, match="MARKET_NOT_TRADABLE"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize("weather_status", ["PARTIAL", "CONFLICTING"])
def test_value_cannot_upgrade_incomplete_weather_to_complete(weather_status):
    value, weather_bundle = bundle()
    weather_bundle["data_status"] = weather_status
    bind_weather_hash(value, weather_bundle)
    with pytest.raises(BridgeError, match="STATUS_UPGRADE_FORBIDDEN"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_candidate_cannot_upgrade_partial_value_to_complete():
    value, weather_bundle = bundle()
    value["data_status"] = "PARTIAL"
    with pytest.raises(BridgeError, match="STATUS_UPGRADE_FORBIDDEN"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "field,replacement,code",
    [
        ("forecast_run_id", "other-run", "FORECAST_RUN_ID_MISMATCH"),
        ("station", "ZBAA", "STATION_MISMATCH"),
        ("weather_date_local", "2026-07-25", "WEATHER_DATE_MISMATCH"),
        ("weather_metric", "daily_temperature", "VALUE_V2_SCHEMA_INVALID"),
    ],
)
def test_candidate_identity_must_match_validated_weather_and_value(field, replacement, code):
    value, weather_bundle = bundle()
    value["candidates"][0][field] = replacement
    with pytest.raises(BridgeError, match=code):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "field,replacement,code",
    [
        ("forecast_run_id", "other-run", "FORECAST_RUN_ID_MISMATCH"),
        ("model_version", "OTHER_MODEL", "VALUE_V2_SCHEMA_INVALID"),
        ("rules_version", "other-rules", "VALUE_V2_SCHEMA_INVALID"),
        ("station", "ZBAA", "STATION_MISMATCH"),
        ("city", "Beijing", "CITY_MISMATCH"),
        ("weather_date_local", "2026-07-25", "WEATHER_DATE_MISMATCH"),
        ("weather_metric", "daily_temperature", "VALUE_V2_SCHEMA_INVALID"),
        ("weather_bundle_sha256", "0" * 64, "WEATHER_HASH_MISMATCH"),
    ],
)
def test_top_level_value_identity_must_match_weather(field, replacement, code):
    value, weather_bundle = bundle()
    value[field] = replacement
    with pytest.raises(BridgeError, match=code):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-07-23T07:00:00+00:00",
        "2026-07-23T07:05:00+00:00",
    ],
)
def test_formal_value_generation_window_includes_exact_boundaries(generated_at):
    value, weather_bundle = bundle()
    value["generated_at_utc"] = generated_at
    assert validate_value_signal_bundle_v2(weather_bundle, value)["accepted_count"] == 1


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-07-23T06:59:59.999999+00:00",
        "2026-07-23T07:05:00.000001+00:00",
    ],
)
def test_formal_value_generation_window_rejects_outside_microsecond(generated_at):
    value, weather_bundle = bundle()
    value["generated_at_utc"] = generated_at
    with pytest.raises(BridgeError, match="VALUE_V2_GENERATED_TIME_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_weather_outside_formal_window_blocks_in_window_value():
    value, weather_bundle = bundle()
    weather_bundle["generated_at_utc"] = "2026-07-23T07:05:00.000001+00:00"
    bind_weather_hash(value, weather_bundle)
    with pytest.raises(BridgeError, match="FORMAL_TIME_WINDOW_BLOCKED"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_value_outside_formal_window_blocks_in_window_weather():
    value, weather_bundle = bundle()
    value["generated_at_utc"] = "2026-07-23T06:59:59.999999+00:00"
    with pytest.raises(BridgeError, match="VALUE_V2_GENERATED_TIME_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "candidate_time",
    [
        "2026-07-23T06:59:00+00:00",
        "2026-07-23T07:01:00+00:00",
    ],
)
def test_candidate_orderbook_time_must_equal_evidence_instant(candidate_time):
    value, weather_bundle = bundle()
    value["candidates"][0]["orderbook_captured_at_utc"] = candidate_time
    with pytest.raises(BridgeError, match="ORDERBOOK_EVIDENCE_TIME_MISMATCH"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_candidate_equivalent_utc_offset_time_is_accepted_and_canonicalized_from_evidence():
    value, weather_bundle = bundle()
    value["candidates"][0]["orderbook_captured_at_utc"] = "2026-07-23T15:00:00+08:00"
    accepted = validate_value_signal_bundle_v2(weather_bundle, value)["accepted"][0]
    assert accepted["orderbook_captured_at_utc"] == value["orderbook_evidence"][0]["captured_at_utc"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing_token", "ORDERBOOK_RAW_TOKEN_REQUIRED"),
        ("missing_condition", "ORDERBOOK_RAW_CONDITION_REQUIRED"),
        ("wrong_token", "ORDERBOOK_TOKEN_BINDING_MISMATCH"),
        ("wrong_condition", "ORDERBOOK_TOKEN_BINDING_MISMATCH"),
    ],
)
def test_raw_orderbook_identity_is_required_and_exact(mutation, code):
    value, weather_bundle = bundle()
    raw = value["orderbook_evidence"][0]["raw_payload"]
    if mutation == "missing_token":
        raw.pop("asset_id")
    elif mutation == "missing_condition":
        raw.pop("market")
    elif mutation == "wrong_token":
        raw["asset_id"] = "token-x"
    else:
        raw["market"] = "cond-x"
    rehash_raw_orderbook(value)
    with pytest.raises(BridgeError, match=code):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://clob.polymarket.com/book?token_id=token-x",
        "https://clob.polymarket.com/book-extra?token_id=token-1",
        "https://clob.polymarket.com.evil.example/book?token_id=token-1",
        "https://user@clob.polymarket.com/book?token_id=token-1",
        "https://clob.polymarket.com/book?token_id=token-1&token_id=token-1",
        "https://clob.polymarket.com/book?token_id=token-1#fragment",
    ],
)
def test_orderbook_endpoint_is_exactly_bound(endpoint):
    value, weather_bundle = bundle()
    value["orderbook_evidence"][0]["endpoint"] = endpoint
    with pytest.raises(BridgeError, match="ORDERBOOK_TOKEN_BINDING_MISMATCH"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_accepted_candidate_identity_and_time_are_derived_from_validated_sources():
    value, weather_bundle = bundle()
    accepted = validate_value_signal_bundle_v2(weather_bundle, value)["accepted"][0]
    assert accepted["forecast_run_id"] == weather_bundle["forecast_run_id"]
    assert accepted["station"] == weather_bundle["station"]
    assert accepted["weather_date_local"] == weather_bundle["weather_date_local"]
    assert accepted["weather_metric"] == weather_bundle["weather_metric"]
    assert accepted["condition_id"] == value["orderbook_evidence"][0]["condition_id"]
    assert accepted["token_id"] == value["orderbook_evidence"][0]["token_id"]
    assert accepted["orderbook_captured_at_utc"] == value["orderbook_evidence"][0]["captured_at_utc"]
