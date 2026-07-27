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


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _refresh_wrapper_hashes(out: Path) -> None:
    """Simulate an attacker who recomputes every wrapper hash they can reach."""
    manifest_path = out / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        meta["sha256"] = sha256_file(out / name)
    _write_json(manifest_path, manifest)
    (out / "bridge_manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")


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


@pytest.mark.parametrize("field", ["forecast_run_id", "station", "weather_date_local", "weather_metric", "data_status"])
def test_candidate_identity_is_required(field):
    weather = _weather()
    candidate = _candidate("32C", 0.35, 0.25, forecast_run_id=weather["forecast_run_id"])
    del candidate[field]
    with pytest.raises(BridgeError) as exc_info:
        validate_value_signal_bundle(weather, _value(weather, [candidate]))
    assert exc_info.value.code == "CANDIDATE_IDENTITY_MISSING"


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63 + "A", " " + "a" * 64, "a" * 64 + " ", "0x" + "a" * 62, "a" * 63, "g" * 64])
def test_sha_is_strict_lowercase(bad):
    weather = _weather(source_snapshot_sha256=bad)
    with pytest.raises(BridgeError) as exc_info:
        validate_weather_probability_bundle(weather)
    assert exc_info.value.code == "SOURCE_MANIFEST_HASH_MISMATCH" or exc_info.value.code == "INVALID_SHA256"


@pytest.mark.parametrize("bad_path,code", [
    ("/tmp/weather_probability_bundle.json", "MANIFEST_PATH_ABSOLUTE"),
    ("../weather_probability_bundle.json", "MANIFEST_PATH_TRAVERSAL"),
    ("nested/weather_probability_bundle.json", "MANIFEST_PATH_TRAVERSAL"),
    ("C:\\temp\\weather_probability_bundle.json", "MANIFEST_PATH_ABSOLUTE"),
    ("", "MANIFEST_PATH_NOT_EXACT"),
])
def test_manifest_paths_must_be_exact(tmp_path, bad_path, code):
    weather = _weather(forecast_run_id=f"strict_path_{code}_{len(bad_path)}")
    result = convert_bundles(weather, _value(weather), tmp_path)
    out = Path(result["output_dir"])
    manifest_path = out / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["weather_probability_bundle.json"]["path"] = bad_path
    _write_json(manifest_path, manifest)
    (out / "bridge_manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")
    verified = verify_bridge_output(out)
    assert verified["ok"] is False
    assert code in verified["errors"]


@pytest.mark.parametrize("field,bad_value", [("station", "ZBAA"), ("model_version", "D1_1400")])
def test_coordinated_wrapper_tampering_is_semantically_rejected(tmp_path, field, bad_value):
    weather = _weather(forecast_run_id="coordinated_manifest")
    result = convert_bundles(weather, _value(weather), tmp_path)
    out = Path(result["output_dir"])
    manifest_path = out / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = bad_value
    _write_json(manifest_path, manifest)
    (out / "bridge_manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")
    verified = verify_bridge_output(out)
    assert verified["ok"] is False
    assert "manifest_identity_mismatch" in verified["errors"]


def test_coordinated_core_value_csv_report_and_source_tampering_is_rejected(tmp_path):
    def fresh(name: str) -> Path:
        weather = _weather(forecast_run_id=name)
        return Path(convert_bundles(weather, _value(weather), tmp_path)["output_dir"])

    core_out = fresh("coordinated_core")
    core_path = core_out / "bridge_manifest_core.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["accepted_candidates"][0]["edge"] = "0.999"
    _write_json(core_path, core)
    new_core_sha = sha256_file(core_path)
    csv_path = core_out / "husky_entry_signals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    for row in rows:
        row["notes"] = row["notes"].replace(parse_notes(row["notes"])["bridge_manifest_sha256"], new_core_sha)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    _refresh_wrapper_hashes(core_out)
    assert "core_rebuild_mismatch" in verify_bridge_output(core_out)["errors"]

    value_out = fresh("coordinated_value")
    value_path = value_out / "value_signal_bundle.json"
    value = json.loads(value_path.read_text(encoding="utf-8"))
    value["candidates"][0]["edge"] = 0.999
    _write_json(value_path, value)
    _refresh_wrapper_hashes(value_out)
    # Invalid candidates are deterministically rejected by the value layer;
    # replay then detects the changed accepted/rejected core and CSV state.
    assert "core_rebuild_mismatch" in verify_bridge_output(value_out)["errors"]

    csv_out = fresh("coordinated_csv")
    csv_path = csv_out / "husky_entry_signals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    rows[0]["max_entry_price"] = "0.01"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    _refresh_wrapper_hashes(csv_out)
    assert "csv_rebuild_mismatch" in verify_bridge_output(csv_out)["errors"]

    report_out = fresh("coordinated_report")
    report_path = report_out / "validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["converted_signal_count"] = 999
    _write_json(report_path, report)
    _refresh_wrapper_hashes(report_out)
    assert "report_rebuild_mismatch" in verify_bridge_output(report_out)["errors"]

    source_out = fresh("coordinated_source")
    weather_path = source_out / "weather_probability_bundle.json"
    weather_payload = json.loads(weather_path.read_text(encoding="utf-8"))
    weather_payload["source_snapshot_manifest"]["sources"][0]["acquired_at_utc"] = "2026-07-23T07:00:01+00:00"
    weather_payload["source_snapshot_sha256"] = content_hash(weather_payload["source_snapshot_manifest"])
    _write_json(weather_path, weather_payload)
    _refresh_wrapper_hashes(source_out)
    verified = verify_bridge_output(source_out)
    assert verified["ok"] is False
    assert any("value_revalidation_failed:LEAKAGE_INVALID" == error for error in verified["errors"])

    weather_out = fresh("coordinated_weather")
    weather_path = weather_out / "weather_probability_bundle.json"
    weather_payload = json.loads(weather_path.read_text(encoding="utf-8"))
    weather_payload["integer_temperature_probabilities"][0]["forecast_probability"] = 0.21
    weather_payload["integer_temperature_probabilities"][1]["forecast_probability"] = 0.34
    _write_json(weather_path, weather_payload)
    value_path = weather_out / "value_signal_bundle.json"
    value_payload = json.loads(value_path.read_text(encoding="utf-8"))
    value_payload["weather_bundle_sha256"] = content_hash(weather_payload)
    _write_json(value_path, value_payload)
    _refresh_wrapper_hashes(weather_out)
    verified = verify_bridge_output(weather_out)
    assert verified["ok"] is False
    assert "core_rebuild_mismatch" in verified["errors"]


@pytest.mark.parametrize("amount", [0.01, 0.1, 0.5, 1, 10, 10.25])
def test_strictly_positive_intended_usd_including_sub_dollar_is_exportable(tmp_path, amount):
    weather = _weather(forecast_run_id=f"sub_dollar_{str(amount).replace('.', '_')}")
    candidate = _candidate("32C", 0.35, 0.25, forecast_run_id=weather["forecast_run_id"], intended_usd=amount)
    value = _value(weather, [candidate])
    assert validate_value_signal_bundle(weather, value)["accepted_count"] == 1
    result = convert_bundles(weather, value, tmp_path)
    out = Path(result["output_dir"])
    core = json.loads((out / "bridge_manifest_core.json").read_text(encoding="utf-8"))
    assert core["accepted_candidates"][0]["intended_usd"] == str(amount)
    assert verify_bridge_output(out)["ok"] is True


@pytest.mark.parametrize("amount", [0, 0.0, -1, "", "1e-1", " 0.5", "0.5 ", "not-a-number"])
def test_zero_negative_and_noncanonical_intended_usd_are_rejected(amount):
    weather = _weather()
    candidate = _candidate("32C", 0.35, 0.25, forecast_run_id=weather["forecast_run_id"], intended_usd=amount)
    with pytest.raises(BridgeError):
        validate_value_signal_bundle(weather, _value(weather, [candidate]))


def test_zero_core_intended_usd_tamper_fails_semantic_replay(tmp_path):
    weather = _weather(forecast_run_id="zero_core_amount")
    out = Path(convert_bundles(weather, _value(weather), tmp_path)["output_dir"])
    core_path = out / "bridge_manifest_core.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["accepted_candidates"][0]["intended_usd"] = "0"
    _write_json(core_path, core)
    core_sha = sha256_file(core_path)
    csv_path = out / "husky_entry_signals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    for row in rows:
        row["notes"] = row["notes"].replace(parse_notes(row["notes"])["bridge_manifest_sha256"], core_sha)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    _refresh_wrapper_hashes(out)
    verified = verify_bridge_output(out)
    assert verified["ok"] is False
    assert "CORE_MANIFEST_JSON_SCHEMA_INVALID" in verified["errors"]


def test_informal_validation_is_research_only_and_cannot_export(tmp_path):
    weather = _weather(forecast_run_id="informal_export_forbidden")
    value = _value(weather)
    assert validate_weather_probability_bundle(weather, formal_mode=False)["ok"] is True
    assert validate_value_signal_bundle(weather, value, formal_mode=False)["ok"] is True
    with pytest.raises(BridgeError) as exc_info:
        convert_bundles(weather, value, tmp_path, formal_mode=False)
    assert exc_info.value.code == "INFORMAL_EXECUTION_EXPORT_FORBIDDEN"
    assert not (tmp_path / "informal_export_forbidden").exists()
    assert not list(tmp_path.glob(".tmp_informal_export_forbidden_*"))
    from src.d1_signal_bridge_v1 import main

    weather_path = tmp_path / "weather.json"
    value_path = tmp_path / "value.json"
    weather_path.write_text(json.dumps(weather), encoding="utf-8")
    value_path.write_text(json.dumps(value), encoding="utf-8")
    cli_root = tmp_path / "cli"
    assert main([
        "convert", "--weather", str(weather_path), "--value", str(value_path),
        "--output-root", str(cli_root), "--allow-informal",
    ]) == 2
    assert not (cli_root / "informal_export_forbidden").exists()


def test_external_orderbook_evidence_path_is_schema_rejected_without_reading(tmp_path):
    weather = _weather()
    candidate = _candidate(
        "32C",
        0.35,
        0.25,
        forecast_run_id=weather["forecast_run_id"],
        orderbook_snapshot_evidence_path="/definitely/not/read/orderbook.json",
    )
    with pytest.raises(BridgeError) as exc_info:
        validate_value_signal_bundle(weather, _value(weather, [candidate]))
    assert exc_info.value.code == "VALUE_JSON_SCHEMA_INVALID"
    candidate["orderbook_snapshot_evidence_path"] = "../outside/orderbook.json"
    with pytest.raises(BridgeError) as exc_info:
        validate_value_signal_bundle(weather, _value(weather, [candidate]))
    assert exc_info.value.code == "VALUE_JSON_SCHEMA_INVALID"


@pytest.mark.parametrize("alias", ["high", "highest", "最高温", "最高气温"])
def test_formal_weather_metric_aliases_are_rejected(alias):
    with pytest.raises(BridgeError) as exc_info:
        validate_weather_probability_bundle(_weather(weather_metric=alias))
    assert exc_info.value.code == "WEATHER_METRIC_INVALID"


def test_execution_eligibility_and_orderbook_level_tampering_is_rejected(tmp_path):
    def fresh(name: str) -> Path:
        weather = _weather(forecast_run_id=name)
        return Path(convert_bundles(weather, _value(weather), tmp_path)["output_dir"])

    formal_out = fresh("formal_mode_tamper")
    core_path = formal_out / "bridge_manifest_core.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["conversion_parameters"]["formal_mode"] = False
    _write_json(core_path, core)
    _refresh_wrapper_hashes(formal_out)
    assert "INFORMAL_EXECUTION_BUNDLE_FORBIDDEN" in verify_bridge_output(formal_out)["errors"]

    core_eligibility_out = fresh("core_eligibility_tamper")
    core_path = core_eligibility_out / "bridge_manifest_core.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["execution_eligible"] = False
    _write_json(core_path, core)
    _refresh_wrapper_hashes(core_eligibility_out)
    assert "EXECUTION_ELIGIBILITY_MISMATCH" in verify_bridge_output(core_eligibility_out)["errors"]

    manifest_eligibility_out = fresh("manifest_eligibility_tamper")
    manifest_path = manifest_eligibility_out / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["execution_eligible"] = False
    _write_json(manifest_path, manifest)
    (manifest_eligibility_out / "bridge_manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")
    assert "EXECUTION_ELIGIBILITY_MISMATCH" in verify_bridge_output(manifest_eligibility_out)["errors"]

    report_eligibility_out = fresh("report_eligibility_tamper")
    report_path = report_eligibility_out / "validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["execution_eligible"] = False
    _write_json(report_path, report)
    _refresh_wrapper_hashes(report_eligibility_out)
    assert "EXECUTION_ELIGIBILITY_MISMATCH" in verify_bridge_output(report_eligibility_out)["errors"]

    csv_out = fresh("csv_eligibility_tamper")
    csv_path = csv_out / "husky_entry_signals.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    rows[0]["notes"] = rows[0]["notes"].replace(";execution_eligible=true", "")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    _refresh_wrapper_hashes(csv_out)
    assert "CSV_EXECUTION_ELIGIBILITY_MISMATCH" in verify_bridge_output(csv_out)["errors"]

    orderbook_out = fresh("orderbook_level_tamper")
    core_path = orderbook_out / "bridge_manifest_core.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["conversion_parameters"]["orderbook_hash_verification"] = "evidence_file_verified"
    _write_json(core_path, core)
    _refresh_wrapper_hashes(orderbook_out)
    assert "ORDERBOOK_HASH_VERIFICATION_MISMATCH" in verify_bridge_output(orderbook_out)["errors"]


def test_sub_dollar_intended_usd_registers_in_demo(tmp_path):
    weather = _weather(forecast_run_id="sub_dollar_demo")
    candidate = _candidate("32C", 0.35, 0.25, forecast_run_id=weather["forecast_run_id"], intended_usd=0.5)
    result = convert_bundles(weather, _value(weather, [candidate]), tmp_path / "bridge")
    demo_root = tmp_path / "demo"
    init_ledger(demo_root, DEMO, CONFIG)
    accepted = register_signals(
        demo_root,
        DEMO,
        CONFIG,
        Path(result["husky_csv"]),
        now=datetime.fromisoformat("2026-07-23T07:00:20+00:00"),
    )
    assert len(accepted) == 1
