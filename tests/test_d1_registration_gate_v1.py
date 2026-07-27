"""D1 bridge provenance must be enforced before signal registration (tmp only)."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.d1_registration_gate_v1 import D1RegistrationGateError, verify_d1_registration_bundle  # noqa: E402
from src.d1_signal_bridge_v1 import content_hash, convert_bundles, sha256_file  # noqa: E402
from src.forward_simulation_v5_1_8 import (  # noqa: E402
    DEMO,
    FORMAL,
    FixtureAdapter,
    db_path,
    demo_fixture,
    load_config,
    monitor_once,
    register_signals,
)

CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_8.yaml"
NOW = datetime(2026, 7, 23, 7, 2, 30, tzinfo=timezone.utc)
OB_HASH = "a7b60a2b2e37c2e0b9b0ab1eb215c10b14c16bcf720437cf576695d50251158d"


def _weather(run_id: str = "d1_gate_zspd_20260724") -> dict:
    source = {"sources": [{"name": "GFS", "acquired_at_utc": "2026-07-23T06:50:00+00:00", "released_at_utc": "2026-07-23T06:40:00+00:00"}]}
    return {
        "forecast_run_id": run_id, "model_version": "D1_1500", "rules_version": "D1_manual_v1.0", "station": "ZSPD", "city": "Shanghai",
        "weather_date_local": "2026-07-24", "weather_metric": "highest_temperature", "as_of_time_utc": "2026-07-23T07:00:00+00:00",
        "as_of_time_cst": "2026-07-23T15:00:00+08:00", "generated_at_utc": "2026-07-23T07:02:00+00:00", "data_status": "COMPLETE",
        "confidence": 0.7, "source_snapshot_manifest": source, "source_snapshot_sha256": content_hash(source), "explanation": "gate fixture",
        "integer_temperature_probabilities": [
            {"temperature_bucket": "31C", "forecast_probability": 0.20}, {"temperature_bucket": "32C", "forecast_probability": 0.35},
            {"temperature_bucket": "33C", "forecast_probability": 0.25}, {"temperature_bucket": "34C or higher", "forecast_probability": 0.15},
            {"temperature_bucket": "其他", "forecast_probability": 0.05},
        ],
    }


def _candidate(run_id: str, bucket: str, probability: float, ask: float) -> dict:
    return {
        "forecast_run_id": run_id, "station": "ZSPD", "weather_date_local": "2026-07-24", "weather_metric": "highest_temperature",
        "temperature_bucket": bucket, "forecast_probability": probability, "market_slug": f"slug-{bucket}", "condition_id": f"condition-{bucket}",
        "token_id": f"token-{bucket}", "outcome": "Yes", "market_ask_price": ask, "edge": probability - ask,
        "recommended_max_price": ask + 0.03, "intended_usd": 10, "reason": "fixture edge", "data_status": "COMPLETE",
        "orderbook_snapshot_id": f"orderbook-{bucket}", "orderbook_snapshot_sha256": OB_HASH, "orderbook_captured_at_utc": "2026-07-23T07:00:00+00:00",
    }


def _run(tmp_path: Path, name: str = "valid") -> Path:
    weather = _weather(f"d1_gate_{name}")
    value = {
        "forecast_run_id": weather["forecast_run_id"], "model_version": weather["model_version"], "rules_version": weather["rules_version"],
        "station": weather["station"], "city": weather["city"], "weather_date_local": weather["weather_date_local"],
        "weather_metric": weather["weather_metric"], "data_status": "COMPLETE", "weather_bundle_sha256": content_hash(weather),
        "candidates": [_candidate(weather["forecast_run_id"], "32C", 0.35, 0.25), _candidate(weather["forecast_run_id"], "33C", 0.25, 0.20)],
    }
    return Path(convert_bundles(weather, value, tmp_path)["output_dir"])


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _refresh_wrapper_hashes(out: Path) -> None:
    manifest_path = out / "bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        meta["sha256"] = sha256_file(out / name)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (out / "bridge_manifest.sha256").write_text(sha256_file(manifest_path) + "\n", encoding="utf-8")


def _signal_count(root: Path) -> int:
    db = db_path(root, DEMO, load_config(CONFIG))
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM signals WHERE mode=?", (DEMO,)).fetchone()[0]


def test_valid_d1_run_registers_all_rows_and_persists_evidence(tmp_path):
    out = _run(tmp_path)
    root = tmp_path / "ledger"
    accepted = register_signals(root, DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)
    assert len(accepted) == 2
    with sqlite3.connect(db_path(root, DEMO, load_config(CONFIG))) as conn:
        evidence = conn.execute("SELECT * FROM d1_registration_evidence").fetchone()
        assert evidence is not None
        assert evidence[8] == sha256_file(out / "bridge_manifest_core.json")
        assert evidence[10] == json.loads((out / "bridge_manifest.json").read_text())["input_content_hashes"]["value_signal_bundle"]
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


def test_detached_or_renamed_csv_is_rejected(tmp_path):
    out = _run(tmp_path)
    detached = tmp_path / "husky_entry_signals.csv"
    detached.write_bytes((out / "husky_entry_signals.csv").read_bytes())
    with pytest.raises(D1RegistrationGateError, match="D1_BRIDGE_OUTPUT_MISSING"):
        register_signals(tmp_path / "detached-ledger", DEMO, CONFIG, detached, now=NOW)
    renamed = out / "renamed.csv"
    (out / "husky_entry_signals.csv").rename(renamed)
    with pytest.raises(D1RegistrationGateError, match="D1_CSV_NOT_CANONICAL"):
        register_signals(tmp_path / "renamed-ledger", DEMO, CONFIG, renamed, now=NOW)


def test_mixed_source_and_missing_artifact_are_rejected_before_insert(tmp_path):
    out = _run(tmp_path, "mixed")
    rows = _rows(out / "husky_entry_signals.csv")
    rows[1]["source"] = "manual"
    _write_rows(out / "husky_entry_signals.csv", rows)
    with pytest.raises(D1RegistrationGateError, match="D1_MIXED_SOURCE_FILE"):
        register_signals(tmp_path / "mixed-ledger", DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)
    assert _signal_count(tmp_path / "mixed-ledger") == 0
    out = _run(tmp_path, "source-bypass")
    rows = _rows(out / "husky_entry_signals.csv")
    for row in rows:
        row["source"] = "manual"
    _write_rows(out / "husky_entry_signals.csv", rows)
    with pytest.raises(D1RegistrationGateError, match="D1_MIXED_SOURCE_FILE"):
        register_signals(tmp_path / "source-bypass-ledger", DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)
    out = _run(tmp_path, "missing")
    (out / "weather_probability_bundle.json").unlink()
    with pytest.raises(D1RegistrationGateError, match="D1_BRIDGE_OUTPUT_MISSING"):
        register_signals(tmp_path / "missing-ledger", DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)


@pytest.mark.parametrize("field,value", [("token_id", "attacker-token"), ("intended_usd", "999"), ("entry_deadline_utc", "2099-01-01T00:00:00+00:00")])
def test_rewrapped_csv_content_change_is_rejected_atomically(tmp_path, field, value):
    out = _run(tmp_path, field)
    rows = _rows(out / "husky_entry_signals.csv")
    rows[0][field] = value
    _write_rows(out / "husky_entry_signals.csv", rows)
    _refresh_wrapper_hashes(out)
    root = tmp_path / f"{field}-ledger"
    with pytest.raises(D1RegistrationGateError, match="D1_BRIDGE_VERIFY_FAILED"):
        register_signals(root, DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)
    assert _signal_count(root) == 0


def test_notes_safety_flag_and_manifest_sha_tamper_are_rejected(tmp_path):
    out = _run(tmp_path, "notes")
    rows = _rows(out / "husky_entry_signals.csv")
    rows[0]["notes"] = rows[0]["notes"].replace("formal_mode=true", "formal_mode=false")
    _write_rows(out / "husky_entry_signals.csv", rows)
    _refresh_wrapper_hashes(out)
    with pytest.raises(D1RegistrationGateError, match="D1_BRIDGE_VERIFY_FAILED"):
        register_signals(tmp_path / "notes-ledger", DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)
    out = _run(tmp_path, "detached")
    (out / "bridge_manifest.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(D1RegistrationGateError, match="D1_BRIDGE_VERIFY_FAILED"):
        register_signals(tmp_path / "sha-ledger", DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)


def test_duplicate_is_idempotent_and_non_d1_remains_compatible(tmp_path):
    out = _run(tmp_path, "repeat")
    root = tmp_path / "repeat-ledger"
    assert len(register_signals(root, DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)) == 2
    assert len(register_signals(root, DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)) == 2
    assert _signal_count(root) == 2
    row = _rows(out / "husky_entry_signals.csv")[0]
    row["source"] = "legacy_fixture"
    row["signal_id"] = "legacy-gate-compatible"
    plain = tmp_path / "legacy.csv"
    _write_rows(plain, [row])
    assert len(register_signals(tmp_path / "legacy-ledger", DEMO, CONFIG, plain, now=NOW)) == 1


def test_demo_and_formal_use_the_same_gate_function(tmp_path):
    out = _run(tmp_path, "modes")
    rows = _rows(out / "husky_entry_signals.csv")
    demo = verify_d1_registration_bundle(out / "husky_entry_signals.csv", rows, DEMO, tmp_path, {})
    formal = verify_d1_registration_bundle(out / "husky_entry_signals.csv", rows, FORMAL, tmp_path, {})
    assert demo and formal and demo["bridge_manifest_core_sha256"] == formal["bridge_manifest_core_sha256"]


def test_verified_d1_signal_reaches_demo_entry_processing(tmp_path):
    weather = _weather("d1_gate_demo_entry")
    market, clob, books, _ = demo_fixture()
    market.update({
        "question": "Highest temperature in Shanghai on July 24?", "title": "Highest temperature in Shanghai on July 24?",
        "slug": "d1-shanghai-2026-07-24-32c", "conditionId": "0xdemo", "groupItemTitle": "32C", "endDate": "2026-07-24T23:59:00Z",
    })
    candidate = _candidate(weather["forecast_run_id"], "32C", 0.35, 0.10)
    candidate.update({"market_slug": market["slug"], "condition_id": "0xdemo", "token_id": "yes-token", "recommended_max_price": 0.11})
    value = {
        "forecast_run_id": weather["forecast_run_id"], "model_version": weather["model_version"], "rules_version": weather["rules_version"],
        "station": weather["station"], "city": weather["city"], "weather_date_local": weather["weather_date_local"], "weather_metric": weather["weather_metric"],
        "data_status": "COMPLETE", "weather_bundle_sha256": content_hash(weather), "candidates": [candidate],
    }
    out = Path(convert_bundles(weather, value, tmp_path)["output_dir"])
    root = tmp_path / "entry-ledger"
    assert len(register_signals(root, DEMO, CONFIG, out / "husky_entry_signals.csv", now=NOW)) == 1
    result = monitor_once(root, DEMO, CONFIG, run_id="d1_gate_demo_entry", adapter=FixtureAdapter(market, clob, books), now=NOW)
    assert not result.get("fatal_error")
    with sqlite3.connect(db_path(root, DEMO, load_config(CONFIG))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entry_fills WHERE mode=?", (DEMO,)).fetchone()[0] == 1, result
