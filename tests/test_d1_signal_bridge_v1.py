"""D1 weather → value → Husky entry-signal bridge tests (tmp only)."""

from __future__ import annotations

import csv
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.d1_signal_bridge_v1 import (  # noqa: E402
    BRIDGE_VERSION,
    BridgeError,
    content_hash,
    convert_bundles,
    normalize_temp_bucket_label,
    sha256_file,
    validate_d1_1500_time_fields,
    validate_value_signal_bundle,
    validate_weather_probability_bundle,
    verify_bridge_output,
)
from src.forward_simulation_v5_1_8 import (  # noqa: E402
    DEMO,
    FORMAL,
    connect,
    db_path,
    init_ledger,
    load_config,
    register_signals,
)

CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_8.yaml"


def _weather(
    *,
    station: str = "ZSPD",
    city: str = "Shanghai",
    run_id: str = "d1_1500_zspd_20260724_t1",
    data_status: str = "COMPLETE",
    as_of_cst: str = "2026-07-23T15:00:00+08:00",
    as_of_utc: str = "2026-07-23T07:00:00+00:00",
    weather_date: str = "2026-07-24",
    generated_at: str = "2026-07-23T07:02:00+00:00",
    probs: list[dict] | None = None,
    source_acquired: str = "2026-07-23T06:50:00+00:00",
) -> dict:
    return {
        "forecast_run_id": run_id,
        "model_version": "D1_1500",
        "rules_version": "D1_manual_v1.0",
        "station": station,
        "city": city,
        "weather_date_local": weather_date,
        "weather_metric": "highest_temperature",
        "as_of_time_utc": as_of_utc,
        "as_of_time_cst": as_of_cst,
        "generated_at_utc": generated_at,
        "data_status": data_status,
        "confidence": 0.7,
        "source_snapshot_sha256": "abc123source",
        "source_snapshot_manifest": {
            "sources": [{"name": "GFS", "acquired_at_utc": source_acquired, "released_at_utc": "2026-07-23T06:40:00+00:00"}]
        },
        "explanation": "test bundle",
        "integer_temperature_probabilities": probs
        or [
            {"temperature_bucket": "31C", "forecast_probability": 0.20},
            {"temperature_bucket": "32C", "forecast_probability": 0.35},
            {"temperature_bucket": "33C", "forecast_probability": 0.25},
            {"temperature_bucket": "34C or higher", "forecast_probability": 0.15},
            {"temperature_bucket": "其他", "forecast_probability": 0.05},
        ],
    }


def _candidate(bucket: str, forecast_p: float, ask: float, **extra) -> dict:
    from decimal import Decimal

    fp = Decimal(str(forecast_p))
    ap = Decimal(str(ask))
    edge = fp - ap
    base = {
        "temperature_bucket": bucket,
        "forecast_probability": float(fp),
        "market_slug": f"slug-{bucket}",
        "condition_id": f"cond-{bucket}",
        "token_id": f"token-{bucket}",
        "outcome": "Yes",
        "market_ask_price": float(ap),
        "edge": float(edge),
        "recommended_max_price": float(min(ap + Decimal("0.03"), Decimal("0.99"))),
        "intended_usd": 10,
        "reason": "edge",
        "data_status": "COMPLETE",
        "orderbook_snapshot_id": f"ob-{bucket}",
        "orderbook_snapshot_sha256": "obhash",
        "orderbook_captured_at_utc": "2026-07-23T07:00:00+00:00",
    }
    base.update(extra)
    return base


def _value(weather: dict, candidates: list[dict] | None = None, **overrides) -> dict:
    payload = {
        "forecast_run_id": weather["forecast_run_id"],
        "model_version": weather["model_version"],
        "rules_version": weather["rules_version"],
        "station": weather["station"],
        "city": weather["city"],
        "weather_date_local": weather["weather_date_local"],
        "weather_metric": weather["weather_metric"],
        "data_status": weather["data_status"],
        "weather_bundle_sha256": content_hash(weather),
        "candidates": candidates
        or [
            _candidate("32C", 0.35, 0.25),
            _candidate("33C", 0.25, 0.20),
        ],
    }
    payload.update(overrides)
    return payload


def test_normalize_bucket_variants():
    assert normalize_temp_bucket_label("32C") == "exact:32C"
    assert normalize_temp_bucket_label("32℃") == "exact:32C"
    assert normalize_temp_bucket_label("32C or below") == "or_below:32C"
    assert normalize_temp_bucket_label("32C or higher") == "or_higher:32C"
    assert normalize_temp_bucket_label("其他") == "其他"


def test_valid_zspd_complete_passes():
    w = _weather(station="ZSPD", city="Shanghai")
    result = validate_weather_probability_bundle(w)
    assert result["ok"] is True
    assert result["data_status"] == "COMPLETE"
    assert result["formal_blocked"] is False


def test_valid_zbaa_complete_passes():
    w = _weather(station="ZBAA", city="Beijing", run_id="d1_1500_zbaa_20260724_t1")
    result = validate_weather_probability_bundle(w)
    assert result["ok"] is True
    assert result["station"] == "ZBAA"


def test_as_of_cst_not_1500_rejects_formal():
    with pytest.raises(BridgeError) as ei:
        validate_d1_1500_time_fields(
            "2026-07-23T06:00:00+00:00",
            "2026-07-23T14:00:00+08:00",
            "2026-07-24",
            formal_mode=True,
        )
    assert ei.value.code == "D1_1500_TIME_INVALID"
    assert "as_of_time_cst_not_1500" in ei.value.details["issues"]


def test_as_of_utc_cst_mismatch_rejects():
    with pytest.raises(BridgeError) as ei:
        validate_d1_1500_time_fields(
            "2026-07-23T08:00:00+00:00",
            "2026-07-23T15:00:00+08:00",
            "2026-07-24",
            formal_mode=True,
        )
    assert "as_of_utc_cst_mismatch" in ei.value.details["issues"]


def test_weather_date_not_next_day_rejects():
    with pytest.raises(BridgeError) as ei:
        validate_d1_1500_time_fields(
            "2026-07-23T07:00:00+00:00",
            "2026-07-23T15:00:00+08:00",
            "2026-07-23",
            formal_mode=True,
        )
    assert "weather_date_local_not_next_day" in ei.value.details["issues"]


def test_generated_at_after_1505_blocks_convert(tmp_path):
    w = _weather(generated_at="2026-07-23T07:10:00+00:00")
    v = _value(w)
    result = validate_weather_probability_bundle(w)
    assert result["formal_blocked"] is True
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, v, tmp_path)
    assert ei.value.code == "FORMAL_TIME_WINDOW_BLOCKED"


def test_source_after_cutoff_leakage_rejects(tmp_path):
    w = _weather(source_acquired="2026-07-23T07:01:00+00:00")
    result = validate_weather_probability_bundle(w)
    assert result["data_status"] == "LEAKAGE_INVALID"
    v = _value(w, data_status="LEAKAGE_INVALID")
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, v, tmp_path)
    assert ei.value.code == "LEAKAGE_INVALID"


def test_probability_sum_not_one_rejects():
    w = _weather(
        probs=[
            {"temperature_bucket": "32C", "forecast_probability": 0.5},
            {"temperature_bucket": "其他", "forecast_probability": 0.4},
        ]
    )
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "PROBABILITY_SUM_INVALID"


def test_probability_out_of_range_rejects():
    w = _weather(probs=[{"temperature_bucket": "32C", "forecast_probability": 1.2}, {"temperature_bucket": "其他", "forecast_probability": -0.2}])
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "PROBABILITY_OUT_OF_RANGE"


def test_duplicate_temperature_bucket_rejects():
    w = _weather(
        probs=[
            {"temperature_bucket": "32C", "forecast_probability": 0.5},
            {"temperature_bucket": "exact:32C", "forecast_probability": 0.5},
        ]
    )
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "DUPLICATE_TEMPERATURE_BUCKET"


def test_unknown_station_rejects():
    w = _weather(station="ZGGG")
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "UNKNOWN_STATION"


@pytest.mark.parametrize("status_name", ["PARTIAL", "CONFLICTING", "STALE"])
def test_non_complete_status_preserved(status_name):
    w = _weather(data_status=status_name)
    result = validate_weather_probability_bundle(w)
    assert result["data_status"] == status_name
    v = _value(w, candidates=[_candidate("32C", 0.35, 0.25, data_status=status_name)], data_status=status_name)
    vv = validate_value_signal_bundle(w, v)
    assert vv["accepted"][0]["data_status"] == status_name


def test_leakage_invalid_cannot_generate_csv(tmp_path):
    w = _weather(data_status="LEAKAGE_INVALID")
    v = _value(w)
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, v, tmp_path)
    assert ei.value.code == "LEAKAGE_INVALID"
    assert not list(tmp_path.rglob("husky_entry_signals.csv"))


def test_edge_mismatch_rejected():
    w = _weather()
    bad = _candidate("32C", 0.35, 0.25)
    bad["edge"] = 0.99
    v = _value(w, candidates=[bad])
    vv = validate_value_signal_bundle(w, v)
    assert vv["accepted_count"] == 0
    assert vv["rejected"][0]["code"] == "EDGE_MISMATCH"


def test_recommended_max_price_invalid_rejected():
    w = _weather()
    bad = _candidate("32C", 0.35, 0.25, recommended_max_price=1.5)
    v = _value(w, candidates=[bad])
    vv = validate_value_signal_bundle(w, v)
    assert vv["rejected"][0]["code"] == "MAX_PRICE_OUT_OF_RANGE"


def test_market_refs_missing_rejected():
    w = _weather()
    bad = _candidate("32C", 0.35, 0.25, token_id="")
    v = _value(w, candidates=[bad])
    vv = validate_value_signal_bundle(w, v)
    assert vv["rejected"][0]["code"] == "MARKET_REF_MISSING"


def test_other_bucket_not_converted_to_token():
    w = _weather()
    v = _value(w, candidates=[_candidate("其他", 0.05, 0.01)])
    vv = validate_value_signal_bundle(w, v)
    assert vv["accepted_count"] == 0
    assert vv["rejected"][0]["code"] == "NON_MARKETABLE_BUCKET"


def test_idempotent_same_content_reuse(tmp_path):
    w = _weather(run_id="reuse_run_1")
    v = _value(w)
    first = convert_bundles(w, v, tmp_path)
    second = convert_bundles(w, v, tmp_path)
    assert first["status"] == "created"
    assert second["status"] == "reused"
    assert first["output_dir"] == second["output_dir"]


def test_forecast_run_id_conflict_rejects_overwrite(tmp_path):
    w1 = _weather(run_id="conflict_run")
    v1 = _value(w1)
    convert_bundles(w1, v1, tmp_path)
    w2 = deepcopy(w1)
    w2["explanation"] = "different content"
    v2 = _value(w2)
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w2, v2, tmp_path)
    assert ei.value.code == "FORECAST_RUN_ID_CONFLICT"


def test_output_sha256_and_manifest_verify(tmp_path):
    w = _weather(run_id="hash_run")
    v = _value(w)
    result = convert_bundles(w, v, tmp_path)
    out = Path(result["output_dir"])
    verified = verify_bridge_output(out)
    assert verified["ok"] is True
    for name in [
        "weather_probability_bundle.json",
        "value_signal_bundle.json",
        "husky_entry_signals.csv",
        "bridge_manifest.json",
        "validation_report.json",
    ]:
        assert (out / name).exists()
    manifest = json.loads((out / "bridge_manifest.json").read_text(encoding="utf-8"))
    assert manifest["bridge_version"] == BRIDGE_VERSION
    assert manifest["formal_ledger_used"] is False
    assert manifest["wallet_or_real_order_used"] is False
    assert manifest["converted_signal_count"] == 2
    for name, meta in manifest["files"].items():
        if name == "bridge_manifest.json":
            continue
        assert sha256_file(Path(meta["path"])) == meta["sha256"]


def test_husky_csv_mapping_and_demo_register(tmp_path):
    w = _weather(run_id="demo_reg_run")
    v = _value(w)
    out_root = tmp_path / "bridge_out"
    demo_root = tmp_path / "demo_root"
    result = convert_bundles(w, v, out_root)
    csv_path = Path(result["husky_csv"])
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows
    for row in rows:
        assert row["created_at_utc"].startswith("2026-07-23T07:00:00")
        assert row["source"] == "d1_signal_bridge_v1"
        assert row["side"] == "BUY"
        assert "forecast_run_id=" in row["notes"]
        assert "weather_bundle_sha256=" in row["notes"]
        assert "value_bundle_sha256=" in row["notes"]
        assert "bridge_manifest_sha256=" in row["notes"]
        assert row["market_probability_at_signal"]
        assert row["max_entry_price"]

    init_ledger(demo_root, DEMO, CONFIG)
    now = datetime.fromisoformat("2026-07-23T07:00:30+00:00")
    accepted = register_signals(demo_root, DEMO, CONFIG, csv_path, now=now)
    assert len(accepted) == 2
    conn = connect(db_path(demo_root, DEMO, load_config(CONFIG)))
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM signals WHERE mode=?", (DEMO,)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 2

    # No formal pollution under demo_root or bridge output.
    assert not (demo_root / "data" / "forward_v5_1_8" / "formal").exists()
    assert not list(out_root.rglob("*formal*"))


def test_demo_register_does_not_create_formal_ledger(tmp_path):
    w = _weather(run_id="no_formal_run")
    v = _value(w)
    bridge_out = tmp_path / "b"
    root = tmp_path / "r"
    convert_bundles(w, v, bridge_out)
    csv_path = next(bridge_out.rglob("husky_entry_signals.csv"))
    init_ledger(root, DEMO, CONFIG)
    register_signals(root, DEMO, CONFIG, csv_path, now=datetime.fromisoformat("2026-07-23T07:00:10+00:00"))
    formal_db = db_path(root, FORMAL, load_config(CONFIG))
    assert not formal_db.exists()
    assert not (root / "data").joinpath("formal").exists()


def test_created_at_uses_as_of_not_wall_clock(tmp_path):
    w = _weather(run_id="asof_clock_run")
    v = _value(w)
    result = convert_bundles(w, v, tmp_path)
    with Path(result["husky_csv"]).open(encoding="utf-8", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["created_at_utc"] == datetime.fromisoformat(w["as_of_time_utc"]).astimezone(timezone.utc).isoformat()


def test_cli_validate_and_convert(tmp_path):
    from src.d1_signal_bridge_v1 import main

    w = _weather(run_id="cli_run")
    v = _value(w)
    wp = tmp_path / "w.json"
    vp = tmp_path / "v.json"
    wp.write_text(json.dumps(w), encoding="utf-8")
    vp.write_text(json.dumps(v), encoding="utf-8")
    assert main(["validate-weather", "--input", str(wp)]) == 0
    assert main(["validate-value", "--weather", str(wp), "--value", str(vp)]) == 0
    out = tmp_path / "out"
    assert main(["convert", "--weather", str(wp), "--value", str(vp), "--output-root", str(out)]) == 0
    run_dir = next(out.iterdir())
    assert main(["verify-output", "--output-dir", str(run_dir)]) == 0


def test_no_wallet_signing_order_imports_in_bridge_module():
    text = (PROJECT_ROOT / "src/d1_signal_bridge_v1.py").read_text(encoding="utf-8").lower()
    for forbidden in ["web3", "private_key", "eth_account", "place_order", "start_formal("]:
        assert forbidden not in text
    assert "formal_ledger_used" in text
    assert "wallet_or_real_order_used" in text
