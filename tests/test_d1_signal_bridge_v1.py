"""D1 bridge integrity: audit root, time bounds, identity binding (tmp only)."""

from __future__ import annotations

import csv
import json
import sys
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
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
    parse_notes,
    sha256_file,
    validate_d1_1500_time_fields,
    validate_value_signal_bundle,
    validate_weather_probability_bundle,
    verify_bridge_output,
)
from src.forward_simulation_v5_1_8 import (  # noqa: E402
    DEMO,
    FORMAL,
    db_path,
    init_ledger,
    load_config,
    register_signals,
)

CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_8.yaml"
OB_HASH = "a7b60a2b2e37c2e0b9b0ab1eb215c10b14c16bcf720437cf576695d50251158d"


def _source_manifest(acquired: str = "2026-07-23T06:50:00+00:00") -> dict:
    return {
        "sources": [
            {
                "name": "GFS",
                "acquired_at_utc": acquired,
                "released_at_utc": "2026-07-23T06:40:00+00:00",
            }
        ]
    }


def _weather(**overrides) -> dict:
    manifest = overrides.pop("source_snapshot_manifest", None) or _source_manifest()
    base = {
        "forecast_run_id": "d1_1500_zspd_20260724_t1",
        "model_version": "D1_1500",
        "rules_version": "D1_manual_v1.0",
        "station": "ZSPD",
        "city": "Shanghai",
        "weather_date_local": "2026-07-24",
        "weather_metric": "highest_temperature",
        "as_of_time_utc": "2026-07-23T07:00:00+00:00",
        "as_of_time_cst": "2026-07-23T15:00:00+08:00",
        "generated_at_utc": "2026-07-23T07:02:00+00:00",
        "data_status": "COMPLETE",
        "confidence": 0.7,
        "source_snapshot_manifest": manifest,
        "source_snapshot_sha256": content_hash(manifest),
        "explanation": "test bundle",
        "integer_temperature_probabilities": [
            {"temperature_bucket": "31C", "forecast_probability": 0.20},
            {"temperature_bucket": "32C", "forecast_probability": 0.35},
            {"temperature_bucket": "33C", "forecast_probability": 0.25},
            {"temperature_bucket": "34C or higher", "forecast_probability": 0.15},
            {"temperature_bucket": "其他", "forecast_probability": 0.05},
        ],
    }
    base.update(overrides)
    if "source_snapshot_sha256" not in overrides:
        base["source_snapshot_sha256"] = content_hash(base["source_snapshot_manifest"])
    return base


def _candidate(bucket: str, forecast_p: float, ask: float, **extra) -> dict:
    fp = Decimal(str(forecast_p))
    ap = Decimal(str(ask))
    base = {
        "forecast_run_id": "d1_1500_zspd_20260724_t1",
        "station": "ZSPD",
        "weather_date_local": "2026-07-24",
        "weather_metric": "highest_temperature",
        "temperature_bucket": bucket,
        "forecast_probability": float(fp),
        "market_slug": f"slug-{bucket}",
        "condition_id": f"cond-{bucket}",
        "token_id": f"token-{bucket}",
        "outcome": "Yes",
        "market_ask_price": float(ap),
        "edge": float(fp - ap),
        "recommended_max_price": float(min(ap + Decimal("0.03"), Decimal("0.99"))),
        "intended_usd": 10,
        "reason": "edge",
        "data_status": "COMPLETE",
        "orderbook_snapshot_id": f"ob-{bucket}",
        "orderbook_snapshot_sha256": OB_HASH,
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
            _candidate("32C", 0.35, 0.25, forecast_run_id=weather["forecast_run_id"], station=weather["station"]),
            _candidate("33C", 0.25, 0.20, forecast_run_id=weather["forecast_run_id"], station=weather["station"]),
        ],
    }
    payload.update(overrides)
    if "weather_bundle_sha256" not in overrides:
        payload["weather_bundle_sha256"] = content_hash(weather)
    return payload


def test_normalize_bucket_variants():
    assert normalize_temp_bucket_label("32C") == "exact:32C"
    assert normalize_temp_bucket_label("32C or below") == "or_below:32C"
    assert normalize_temp_bucket_label("32C or higher") == "or_higher:32C"


def test_valid_zspd_and_zbaa_pass():
    assert validate_weather_probability_bundle(_weather())["ok"] is True
    zbaa = _weather(station="ZBAA", city="Beijing", forecast_run_id="d1_1500_zbaa_20260724_t1")
    assert validate_weather_probability_bundle(zbaa)["station"] == "ZBAA"


def test_core_manifest_sha_in_csv_notes(tmp_path):
    w = _weather(forecast_run_id="core_sha_run")
    v = _value(w)
    result = convert_bundles(w, v, tmp_path)
    out = Path(result["output_dir"])
    core_sha = sha256_file(out / "bridge_manifest_core.json")
    assert result["core_sha256"] == core_sha
    with (out / "husky_entry_signals.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            notes = parse_notes(row["notes"])
            assert notes["bridge_manifest_sha256"] == core_sha


def test_detached_manifest_sha(tmp_path):
    w = _weather(forecast_run_id="detached_sha_run")
    result = convert_bundles(w, _value(w), tmp_path)
    out = Path(result["output_dir"])
    detached = (out / "bridge_manifest.sha256").read_text(encoding="utf-8").strip().split()[0]
    assert detached == sha256_file(out / "bridge_manifest.json")
    assert detached == result["manifest_sha256"]


def test_verify_output_passes_complete(tmp_path):
    w = _weather(forecast_run_id="verify_ok_run")
    result = convert_bundles(w, _value(w), tmp_path)
    verified = verify_bridge_output(Path(result["output_dir"]))
    assert verified["ok"] is True
    assert verified["errors"] == []


@pytest.mark.parametrize(
    "target,marker",
    [
        ("bridge_manifest.json", "manifest_tamper"),
        ("bridge_manifest_core.json", "core_tamper"),
        ("husky_entry_signals.csv", "csv_tamper"),
        ("validation_report.json", "report_tamper"),
    ],
)
def test_tamper_fails_verify(tmp_path, target, marker):
    w = _weather(forecast_run_id=f"tamper_{marker}")
    result = convert_bundles(w, _value(w), tmp_path)
    out = Path(result["output_dir"])
    path = out / target
    raw = path.read_text(encoding="utf-8")
    if target.endswith(".csv"):
        path.write_text(raw + f"\n# {marker}\n", encoding="utf-8")
    else:
        path.write_text(raw.replace("{", "{ ", 1) if "{" in raw else raw + "\n", encoding="utf-8")
        # ensure bytes change for JSON: append space before final newline already may not change sorted json;
        # force change:
        path.write_bytes(path.read_bytes() + b"\n")
    assert verify_bridge_output(out)["ok"] is False


def test_csv_notes_hash_tamper_fails(tmp_path):
    w = _weather(forecast_run_id="notes_tamper_run")
    result = convert_bundles(w, _value(w), tmp_path)
    out = Path(result["output_dir"])
    csv_path = out / "husky_entry_signals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    rows[0]["notes"] = rows[0]["notes"].replace(
        parse_notes(rows[0]["notes"])["bridge_manifest_sha256"],
        "0" * 64,
    )
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    verified = verify_bridge_output(out)
    assert verified["ok"] is False
    assert any("csv_core_manifest_reference_mismatch" in e for e in verified["errors"])


def test_generated_before_1500_rejects(tmp_path):
    w = _weather(generated_at_utc="2026-07-23T06:59:59+00:00", forecast_run_id="early_gen")
    result = validate_weather_probability_bundle(w)
    assert result["formal_blocked"] is True
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, _value(w), tmp_path)
    assert ei.value.code == "FORMAL_TIME_WINDOW_BLOCKED"


def test_generated_after_1505_rejects(tmp_path):
    w = _weather(generated_at_utc="2026-07-23T07:05:01+00:00", forecast_run_id="late_gen")
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, _value(w), tmp_path)
    assert ei.value.code == "FORMAL_TIME_WINDOW_BLOCKED"


def test_cst_must_be_explicit_plus0800():
    with pytest.raises(BridgeError) as ei:
        validate_d1_1500_time_fields(
            "2026-07-23T07:00:00+00:00",
            "2026-07-23T07:00:00+00:00",  # same instant but not explicit +08:00 CST field
            "2026-07-24",
            "2026-07-23T07:02:00+00:00",
            formal_mode=True,
        )
    assert ei.value.code == "CST_OFFSET_INVALID"


def test_utc_must_not_use_plus0800():
    with pytest.raises(BridgeError) as ei:
        validate_d1_1500_time_fields(
            "2026-07-23T15:00:00+08:00",
            "2026-07-23T15:00:00+08:00",
            "2026-07-24",
            "2026-07-23T07:02:00+00:00",
            formal_mode=True,
        )
    assert ei.value.code == "UTC_LITERAL_INVALID"


def test_model_and_rules_profile_required():
    w = _weather(model_version="D1_1400")
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "MODEL_VERSION_MISMATCH"
    w2 = _weather(rules_version="D1_manual_v0.9")
    with pytest.raises(BridgeError) as ei2:
        validate_weather_probability_bundle(w2)
    assert ei2.value.code == "RULES_VERSION_MISMATCH"


def test_identity_mismatches():
    w = _weather()
    cases = [
        ("station", "ZBAA", "STATION_MISMATCH"),
        ("city", "Beijing", "CITY_MISMATCH"),
        ("weather_date_local", "2026-07-25", "WEATHER_DATE_MISMATCH"),
        ("weather_metric", "lowest_temperature", "WEATHER_METRIC_INVALID"),
        ("model_version", "OTHER", "MODEL_VERSION_MISMATCH"),
        ("rules_version", "other", "RULES_VERSION_MISMATCH"),
    ]
    for field, bad, code in cases:
        v = _value(w)
        v[field] = bad
        with pytest.raises(BridgeError) as ei:
            validate_value_signal_bundle(w, v)
        assert ei.value.code == code


def test_value_status_upgrade_forbidden():
    w = _weather(data_status="PARTIAL")
    v = _value(w, data_status="COMPLETE")
    with pytest.raises(BridgeError) as ei:
        validate_value_signal_bundle(w, v)
    assert ei.value.code == "STATUS_UPGRADE_FORBIDDEN"


def test_source_hash_binding():
    w = _weather(source_snapshot_sha256="0" * 64)
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "SOURCE_MANIFEST_HASH_MISMATCH"


def test_source_invalid_timestamp_not_silent():
    manifest = _source_manifest()
    manifest["sources"][0]["acquired_at_utc"] = "not-a-time"
    w = _weather(source_snapshot_manifest=manifest)
    with pytest.raises(BridgeError) as ei:
        validate_weather_probability_bundle(w)
    assert ei.value.code == "SOURCE_TIMESTAMP_INVALID"


def test_source_late_leakage_rejects(tmp_path):
    manifest = _source_manifest(acquired="2026-07-23T07:00:01+00:00")
    w = _weather(source_snapshot_manifest=manifest, forecast_run_id="leak_run")
    result = validate_weather_probability_bundle(w)
    assert result["data_status"] == "LEAKAGE_INVALID"
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, _value(w), tmp_path)
    assert ei.value.code == "LEAKAGE_INVALID"


def test_reuse_deterministic(tmp_path):
    w = _weather(forecast_run_id="reuse_ok")
    v = _value(w)
    first = convert_bundles(w, v, tmp_path)
    second = convert_bundles(w, v, tmp_path)
    assert first["status"] == "created"
    assert second["status"] == "reused"
    assert first["core_sha256"] == second["core_sha256"]


def test_corrupt_existing_blocks_reuse(tmp_path):
    w = _weather(forecast_run_id="corrupt_reuse")
    v = _value(w)
    convert_bundles(w, v, tmp_path)
    out = tmp_path / "corrupt_reuse"
    (out / "husky_entry_signals.csv").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, v, tmp_path)
    assert ei.value.code == "CORRUPT_EXISTING_OUTPUT"


def test_incomplete_directory_blocks_overwrite(tmp_path):
    w = _weather(forecast_run_id="incomplete_dir")
    out = tmp_path / "incomplete_dir"
    out.mkdir()
    (out / "weather_probability_bundle.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w, _value(w), tmp_path)
    assert ei.value.code == "INCOMPLETE_EXISTING_OUTPUT"


def test_atomic_write_no_partial_on_failure(tmp_path, monkeypatch):
    w = _weather(forecast_run_id="atomic_fail")
    v = _value(w)
    from src import d1_signal_bridge_v1 as bridge

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure after tmp create")

    monkeypatch.setattr(bridge, "_write_run_artifacts", boom)
    with pytest.raises(RuntimeError):
        convert_bundles(w, v, tmp_path)
    assert not (tmp_path / "atomic_fail").exists()
    assert list(tmp_path.glob(".tmp_atomic_fail_*")) == []


def test_forecast_run_conflict(tmp_path):
    w1 = _weather(forecast_run_id="conflict_run")
    convert_bundles(w1, _value(w1), tmp_path)
    w2 = deepcopy(w1)
    w2["explanation"] = "changed"
    with pytest.raises(BridgeError) as ei:
        convert_bundles(w2, _value(w2), tmp_path)
    assert ei.value.code == "FORECAST_RUN_ID_CONFLICT"


def test_demo_register_and_no_formal(tmp_path):
    w = _weather(forecast_run_id="demo_reg_integrity")
    v = _value(w)
    out_root = tmp_path / "bridge"
    demo_root = tmp_path / "demo"
    result = convert_bundles(w, v, out_root)
    assert verify_bridge_output(Path(result["output_dir"]))["ok"] is True
    init_ledger(demo_root, DEMO, CONFIG)
    accepted = register_signals(
        demo_root,
        DEMO,
        CONFIG,
        Path(result["husky_csv"]),
        now=datetime.fromisoformat("2026-07-23T07:00:20+00:00"),
    )
    assert len(accepted) == 2
    formal_db = db_path(demo_root, FORMAL, load_config(CONFIG))
    assert not formal_db.exists()
    assert not (demo_root / "data" / "forward_v5_1_8" / "formal").exists()


def test_cli_chain(tmp_path):
    from src.d1_signal_bridge_v1 import main

    w = _weather(forecast_run_id="cli_integrity")
    v = _value(w)
    wp = tmp_path / "w.json"
    vp = tmp_path / "v.json"
    wp.write_text(json.dumps(w), encoding="utf-8")
    vp.write_text(json.dumps(v), encoding="utf-8")
    assert main(["validate-weather", "--input", str(wp)]) == 0
    assert main(["validate-value", "--weather", str(wp), "--value", str(vp)]) == 0
    out = tmp_path / "out"
    assert main(["convert", "--weather", str(wp), "--value", str(vp), "--output-root", str(out)]) == 0
    run_dir = next(p for p in out.iterdir() if p.is_dir() and not p.name.startswith("."))
    assert main(["verify-output", "--output-dir", str(run_dir)]) == 0


def test_manifest_paths_are_relative(tmp_path):
    w = _weather(forecast_run_id="rel_paths")
    result = convert_bundles(w, _value(w), tmp_path)
    manifest = json.loads((Path(result["output_dir"]) / "bridge_manifest.json").read_text(encoding="utf-8"))
    for meta in manifest["files"].values():
        assert not str(meta["path"]).startswith("/")
        assert "Users" not in str(meta["path"])


def test_no_trading_primitives_in_module():
    text = (PROJECT_ROOT / "src/d1_signal_bridge_v1.py").read_text(encoding="utf-8").lower()
    for forbidden in ["web3", "private_key", "eth_account", "place_order", "start_formal("]:
        assert forbidden not in text
