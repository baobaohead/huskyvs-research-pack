from __future__ import annotations
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys
PROJECT_ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(PROJECT_ROOT))
from src.d1_signal_bridge_dispatch import convert_bundle_dispatch, verify_bridge_output_dispatch
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
    value,weather=bundle(); out=Path(convert_bundle_dispatch(weather,value,tmp_path)["output_dir"]); path=out/"bridge_manifest.json"; import json
    m=json.loads(path.read_text()); m["orderbook_hash_verification"]="reference_format_only"; path.write_text(json.dumps(m)); (out/"bridge_manifest.sha256").write_text(__import__("hashlib").sha256(path.read_bytes()).hexdigest()+"\n")
    checked=verify_bridge_output_dispatch(out); assert not checked["ok"] and "VALUE_V2_DOWNGRADE_FORBIDDEN" in checked["errors"]
