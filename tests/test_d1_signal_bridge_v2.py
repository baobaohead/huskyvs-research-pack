from __future__ import annotations
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys
import pytest
PROJECT_ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(PROJECT_ROOT))
from src.d1_signal_bridge_dispatch import convert_bundle_dispatch, verify_bridge_output_dispatch
from src.d1_signal_bridge_v1 import content_hash
from src.d1_value_signal_contract_v2 import ORDERBOOK_HASH_VALIDATION_LEVEL
from src.forward_simulation_v5_1_8 import DEMO, db_path, load_config, register_signals
from test_d1_value_signal_contract_v2 import bundle

def test_v2_convert_verify_and_dispatch(tmp_path):
    value,weather=bundle(); made=convert_bundle_dispatch(weather,value,tmp_path); out=Path(made["output_dir"]); checked=verify_bridge_output_dispatch(out)
    assert checked["ok"] and checked["manifest"]["orderbook_hash_verification"]==ORDERBOOK_HASH_VALIDATION_LEVEL
    with (out/"husky_entry_signals.csv").open() as f: assert next(csv.DictReader(f))["source"]=="d1_signal_bridge_v2"

def test_v2_demo_registration_persists_replay_level(tmp_path):
    value,weather=bundle(); out=Path(convert_bundle_dispatch(weather,value,tmp_path)["output_dir"]); config=PROJECT_ROOT/"config/forward_simulation_v5_1_8.yaml"
    assert len(register_signals(tmp_path/"ledger",DEMO,config,out/"husky_entry_signals.csv",now=datetime(2026,7,23,7,2,30,tzinfo=timezone.utc)))==1
    with sqlite3.connect(db_path(tmp_path/"ledger",DEMO,load_config(config))) as conn:
        assert conn.execute("SELECT orderbook_hash_verification FROM d1_registration_evidence").fetchone()[0]==ORDERBOOK_HASH_VALIDATION_LEVEL

def test_v2_reference_only_downgrade_is_rejected(tmp_path):
    value,weather=bundle(); out=Path(convert_bundle_dispatch(weather,value,tmp_path)["output_dir"]); path=out/"bridge_manifest.json"
    m=json.loads(path.read_text()); m["orderbook_hash_verification"]="reference_format_only"; path.write_text(json.dumps(m)); (out/"bridge_manifest.sha256").write_text(__import__("hashlib").sha256(path.read_bytes()).hexdigest()+"\n")
    checked=verify_bridge_output_dispatch(out); assert not checked["ok"] and "VALUE_V2_DOWNGRADE_FORBIDDEN" in checked["errors"]


def rewrite_manifest(out, mutation):
    path = out / "bridge_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutation(manifest)
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (out / "bridge_manifest.sha256").write_text(
        hashlib.sha256(path.read_bytes()).hexdigest() + "\n",
        encoding="utf-8",
    )


def converted_output(tmp_path):
    value, weather = bundle()
    return Path(convert_bundle_dispatch(weather, value, tmp_path)["output_dir"])


def test_v2_manifest_complete_exact_file_set_passes(tmp_path):
    out = converted_output(tmp_path)
    checked = verify_bridge_output_dispatch(out)
    assert checked["ok"]
    assert set(checked["manifest"]["files"]) == {
        "weather_probability_bundle.json",
        "value_signal_bundle.json",
        "bridge_manifest_core.json",
        "husky_entry_signals.csv",
        "validation_report.json",
    }


@pytest.mark.parametrize(
    "missing",
    [
        "weather_probability_bundle.json",
        "value_signal_bundle.json",
        "husky_entry_signals.csv",
    ],
)
def test_v2_manifest_missing_file_hash_entry_is_rejected(tmp_path, missing):
    out = converted_output(tmp_path)
    rewrite_manifest(out, lambda manifest: manifest["files"].pop(missing))
    checked = verify_bridge_output_dispatch(out)
    assert not checked["ok"]
    assert "manifest_files_set_mismatch" in checked["errors"]
    assert checked["manifest_identity_result"]["ok"] is False


def test_v2_manifest_unknown_file_hash_entry_is_rejected(tmp_path):
    out = converted_output(tmp_path)
    rewrite_manifest(
        out,
        lambda manifest: manifest["files"].update(
            {"unknown.json": {"path": "unknown.json", "sha256": "0" * 64}}
        ),
    )
    checked = verify_bridge_output_dispatch(out)
    assert not checked["ok"]
    assert "manifest_files_set_mismatch" in checked["errors"]


def test_v2_manifest_meta_path_must_equal_file_name(tmp_path):
    out = converted_output(tmp_path)
    rewrite_manifest(
        out,
        lambda manifest: manifest["files"]["value_signal_bundle.json"].update(
            {"path": "weather_probability_bundle.json"}
        ),
    )
    checked = verify_bridge_output_dispatch(out)
    assert not checked["ok"]
    assert "artifact_hash_mismatch:value_signal_bundle.json" in checked["errors"]


def test_v2_manifest_file_sha_must_replay(tmp_path):
    out = converted_output(tmp_path)
    rewrite_manifest(
        out,
        lambda manifest: manifest["files"]["validation_report.json"].update(
            {"sha256": "0" * 64}
        ),
    )
    checked = verify_bridge_output_dispatch(out)
    assert not checked["ok"]
    assert "artifact_hash_mismatch:validation_report.json" in checked["errors"]


def test_v2_coordinated_candidate_and_outer_hash_tampering_is_rejected(tmp_path):
    out = converted_output(tmp_path)
    value_path = out / "value_signal_bundle.json"
    value = json.loads(value_path.read_text(encoding="utf-8"))
    value["candidates"][0]["forecast_run_id"] = "coordinated-tamper"
    value_path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def update_outer_hashes(manifest):
        manifest["input_content_hashes"]["value_signal_bundle"] = content_hash(value)
        manifest["files"]["value_signal_bundle.json"]["sha256"] = hashlib.sha256(
            value_path.read_bytes()
        ).hexdigest()

    rewrite_manifest(out, update_outer_hashes)
    checked = verify_bridge_output_dispatch(out)
    assert not checked["ok"]
    assert "FORECAST_RUN_ID_MISMATCH" in checked["errors"]
