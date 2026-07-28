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
    return {"schema_version":"2.0","forecast_run_id":w["forecast_run_id"],"model_version":"D1_1500","rules_version":"D1_manual_v1.0","station":"ZSPD","city":"Shanghai","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","as_of_time_utc":w["as_of_time_utc"],"generated_at_utc":"2026-07-23T07:03:00+00:00","data_status":"COMPLETE","weather_bundle_sha256":content_hash(w),"market_snapshot_manifest":market,"market_snapshot_sha256":adapter_hash(market),"orderbook_evidence":[evidence],"candidates":[{"forecast_run_id":w["forecast_run_id"],"station":"ZSPD","weather_date_local":"2026-07-24","weather_metric":"highest_temperature","temperature_bucket":"32C","forecast_probability":.35,"market_slug":"shanghai-high","condition_id":"cond-1","token_id":"token-1","outcome":"Yes","market_ask_price":.30,"edge":.05,"recommended_max_price":.33,"intended_usd":10,"reason":"fixture","data_status":"COMPLETE","orderbook_snapshot_id":"ob-1","orderbook_snapshot_sha256":evidence["normalized_book_sha256"],"orderbook_captured_at_utc":"2026-07-23T07:00:00+00:00","orderbook_evidence_ref":"ob-1"}]},w

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
    weather_bundle["generated_at_utc"] = "2026-07-23T07:00:00+00:00"
    bind_weather_hash(value, weather_bundle)
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


def test_value_generated_before_weather_is_rejected():
    value, weather_bundle = bundle()
    value["generated_at_utc"] = "2026-07-23T07:01:00+00:00"
    with pytest.raises(BridgeError, match="VALUE_V2_GENERATED_BEFORE_WEATHER"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "value_generated",
    [
        "2026-07-23T07:02:00+00:00",
        "2026-07-23T07:03:00+00:00",
    ],
)
def test_value_generated_equal_to_or_after_weather_is_accepted(value_generated):
    value, weather_bundle = bundle()
    value["generated_at_utc"] = value_generated
    assert validate_value_signal_bundle_v2(weather_bundle, value)["accepted_count"] == 1


def test_in_window_value_still_cannot_precede_weather():
    value, weather_bundle = bundle()
    weather_bundle["generated_at_utc"] = "2026-07-23T07:04:00+00:00"
    value["generated_at_utc"] = "2026-07-23T07:03:00+00:00"
    bind_weather_hash(value, weather_bundle)
    with pytest.raises(BridgeError, match="VALUE_V2_GENERATED_BEFORE_WEATHER"):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_nonformal_validation_enforces_weather_value_causality():
    value, weather_bundle = bundle()
    weather_bundle["generated_at_utc"] = "2026-07-23T08:02:00+00:00"
    value["generated_at_utc"] = "2026-07-23T08:01:00+00:00"
    bind_weather_hash(value, weather_bundle)
    with pytest.raises(BridgeError, match="VALUE_V2_GENERATED_BEFORE_WEATHER"):
        validate_value_signal_bundle_v2(weather_bundle, value, formal_mode=False)


@pytest.mark.parametrize(
    "source,outcomes,tokens",
    [
        ("gamma", ["Yes", "No"], ["token-1"]),
        ("gamma", ["Yes"], ["token-1", "token-2"]),
        ("clob", ["Yes", "No"], ["token-1"]),
        ("clob", ["Yes"], ["token-1", "token-2"]),
        ("gamma", ["Yes", ""], ["token-1", "token-2"]),
        ("gamma", ["Yes", "No"], ["token-1", ""]),
    ],
)
def test_outcome_token_arrays_must_be_complete_and_equal(source, outcomes, tokens):
    value, weather_bundle = bundle()
    payload = value["market_snapshot_manifest"][f"{source}_market_payload"]
    payload["outcomes"] = outcomes
    payload["clobTokenIds"] = tokens
    rehash_market(value)
    expected = (
        "EVIDENCE_STRING_INVALID"
        if any(item == "" for item in outcomes + tokens)
        else "MARKET_OUTCOME_TOKEN_CARDINALITY_MISMATCH"
    )
    with pytest.raises(BridgeError, match=expected):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "field,values,code",
    [
        ("outcomes", ["Yes", "Yes"], "MARKET_OUTCOME_DUPLICATE"),
        ("clobTokenIds", ["token-1", "token-1"], "MARKET_TOKEN_DUPLICATE"),
    ],
)
def test_coordinated_duplicate_mapping_is_rejected_after_all_hashes_recomputed(field, values, code):
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    outcomes = values if field == "outcomes" else ["Yes", "No"]
    tokens = values if field == "clobTokenIds" else ["token-1", "token-2"]
    market["outcomes"] = outcomes
    market["clob_token_ids"] = tokens
    for source in ("gamma_market_payload", "clob_market_payload"):
        market[source]["outcomes"] = outcomes
        market[source]["clobTokenIds"] = tokens
    rehash_market(value)
    with pytest.raises(BridgeError, match=code):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_legal_multi_outcome_mapping_preserves_full_order():
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    outcomes = ["Yes", "No"]
    tokens = ["token-1", "token-2"]
    market["outcomes"] = outcomes
    market["clob_token_ids"] = tokens
    for source in ("gamma_market_payload", "clob_market_payload"):
        market[source]["outcomes"] = outcomes
        market[source]["clobTokenIds"] = tokens
    rehash_market(value)
    assert validate_value_signal_bundle_v2(weather_bundle, value)["accepted_count"] == 1


@pytest.mark.parametrize("condition", [None, "", "cond-x"])
def test_clob_market_condition_is_required_and_exact(condition):
    value, weather_bundle = bundle()
    clob = value["market_snapshot_manifest"]["clob_market_payload"]
    if condition is None:
        clob.pop("condition_id")
        expected = "CLOB_MARKET_CONDITION_REQUIRED"
    else:
        clob["condition_id"] = condition
        expected = (
            "EVIDENCE_STRING_INVALID"
            if condition == ""
            else "ORDERBOOK_TOKEN_BINDING_MISMATCH"
        )
    rehash_market(value)
    with pytest.raises(BridgeError, match=expected):
        validate_value_signal_bundle_v2(weather_bundle, value)


def test_coordinated_clob_condition_rehash_cannot_override_gamma_identity():
    value, weather_bundle = bundle()
    market = value["market_snapshot_manifest"]
    market["condition_id"] = "cond-x"
    market["clob_market_payload"]["condition_id"] = "cond-x"
    rehash_market(value)
    with pytest.raises(BridgeError, match="MARKET_IDENTITY_REPLAY_MISMATCH"):
        validate_value_signal_bundle_v2(weather_bundle, value)


@pytest.mark.parametrize(
    "source,alias,value",
    [
        ("gamma", "condition_id", "cond-x"),
        ("clob", "market", "cond-x"),
        ("raw", "condition_id", "cond-x"),
        ("raw", "token_id", "token-x"),
    ],
)
def test_conflicting_scalar_aliases_are_rejected_after_rehash(source, alias, value):
    value_bundle, weather_bundle = bundle()
    if source == "raw":
        value_bundle["orderbook_evidence"][0]["raw_payload"][alias] = value
        rehash_raw_orderbook(value_bundle)
    else:
        value_bundle["market_snapshot_manifest"][f"{source}_market_payload"][alias] = value
        rehash_market(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_ALIAS_CONFLICT") as raised:
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)
    assert raised.value.details["source"].startswith(source)
    assert raised.value.details["alias_group"] in {"condition", "token"}
    assert alias in raised.value.details["present_aliases"]
    assert "raw_payload" not in raised.value.details


@pytest.mark.parametrize(
    "source,alias,value",
    [
        ("gamma", "outcome", ["No"]),
        ("gamma", "tokens", ["token-x"]),
        ("clob", "outcome", ["No"]),
        ("clob", "tokens", ["token-x"]),
    ],
)
def test_conflicting_array_aliases_are_rejected_after_rehash(source, alias, value):
    value_bundle, weather_bundle = bundle()
    value_bundle["market_snapshot_manifest"][f"{source}_market_payload"][alias] = value
    rehash_market(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_ALIAS_CONFLICT") as raised:
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)
    assert raised.value.details["source"] == source
    assert raised.value.details["alias_group"] in {"outcomes", "tokens"}
    assert alias in raised.value.details["present_aliases"]


def test_identical_scalar_aliases_are_accepted_without_mutating_payloads():
    value_bundle, weather_bundle = bundle()
    market = value_bundle["market_snapshot_manifest"]
    market["gamma_market_payload"]["condition_id"] = "cond-1"
    market["clob_market_payload"]["market"] = "cond-1"
    raw = value_bundle["orderbook_evidence"][0]["raw_payload"]
    raw["condition_id"] = "cond-1"
    raw["token_id"] = "token-1"
    before_gamma = deepcopy(market["gamma_market_payload"])
    before_clob = deepcopy(market["clob_market_payload"])
    before_raw = deepcopy(raw)
    rehash_market(value_bundle)
    rehash_raw_orderbook(value_bundle)
    assert validate_value_signal_bundle_v2(weather_bundle, value_bundle)["accepted_count"] == 1
    assert market["gamma_market_payload"] == before_gamma
    assert market["clob_market_payload"] == before_clob
    assert raw == before_raw


def test_identical_array_aliases_accept_list_and_strict_json_array_forms():
    value_bundle, weather_bundle = bundle()
    market = value_bundle["market_snapshot_manifest"]
    for source in ("gamma_market_payload", "clob_market_payload"):
        market[source]["outcome"] = '["Yes"]'
        market[source]["tokens"] = ["token-1"]
    rehash_market(value_bundle)
    assert validate_value_signal_bundle_v2(weather_bundle, value_bundle)["accepted_count"] == 1


def test_same_array_elements_in_different_order_are_an_alias_conflict():
    value_bundle, weather_bundle = bundle()
    market = value_bundle["market_snapshot_manifest"]
    outcomes = ["Yes", "No"]
    tokens = ["token-1", "token-2"]
    market["outcomes"] = outcomes
    market["clob_token_ids"] = tokens
    for source in ("gamma_market_payload", "clob_market_payload"):
        market[source]["outcomes"] = outcomes
        market[source]["clobTokenIds"] = tokens
    market["gamma_market_payload"]["outcome"] = ["No", "Yes"]
    rehash_market(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_ALIAS_CONFLICT"):
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("condition", "   "),
        ("outcome", "   "),
        ("token", "\t"),
    ],
)
def test_whitespace_only_evidence_identity_is_rejected(field, bad_value):
    value_bundle, weather_bundle = bundle()
    gamma = value_bundle["market_snapshot_manifest"]["gamma_market_payload"]
    if field == "condition":
        gamma["conditionId"] = bad_value
    elif field == "outcome":
        gamma["outcomes"] = [bad_value]
    else:
        gamma["clobTokenIds"] = [bad_value]
    rehash_market(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_STRING_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)


@pytest.mark.parametrize("bad_token", [" token-1", "token-1 ", "\ttoken-1", "token-1\n"])
def test_raw_token_leading_or_trailing_whitespace_is_rejected(bad_token):
    value_bundle, weather_bundle = bundle()
    value_bundle["orderbook_evidence"][0]["raw_payload"]["asset_id"] = bad_token
    rehash_raw_orderbook(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_STRING_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)


@pytest.mark.parametrize("bad_outcome", [" Yes", "Yes ", "\tYes", "Yes\n"])
def test_outcome_leading_or_trailing_whitespace_is_rejected(bad_outcome):
    value_bundle, weather_bundle = bundle()
    value_bundle["market_snapshot_manifest"]["gamma_market_payload"]["outcomes"] = [bad_outcome]
    rehash_market(value_bundle)
    with pytest.raises(BridgeError, match="EVIDENCE_STRING_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("condition_id", " cond-1"),
        ("token_id", "token-1 "),
        ("outcome", " Yes"),
        ("orderbook_snapshot_id", " ob-1"),
        ("orderbook_evidence_ref", "ob-1 "),
    ],
)
def test_candidate_security_identity_strings_reject_padding(field, bad_value):
    value_bundle, weather_bundle = bundle()
    value_bundle["candidates"][0][field] = bad_value
    with pytest.raises(BridgeError, match="EVIDENCE_STRING_INVALID"):
        validate_value_signal_bundle_v2(weather_bundle, value_bundle)


def test_legal_internal_whitespace_outcome_is_accepted():
    value_bundle, weather_bundle = bundle()
    market = value_bundle["market_snapshot_manifest"]
    outcome = "Less than 32C"
    market["outcomes"] = [outcome]
    market["gamma_market_payload"]["outcomes"] = [outcome]
    market["clob_market_payload"]["outcomes"] = [outcome]
    value_bundle["candidates"][0]["outcome"] = outcome
    rehash_market(value_bundle)
    accepted = validate_value_signal_bundle_v2(weather_bundle, value_bundle)["accepted"][0]
    assert accepted["outcome"] == outcome
