"""V2 bridge: self-contained evidence replay before a Husky CSV is emitted."""
from __future__ import annotations

import csv
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

try:
    from src.d1_signal_bridge_v1 import (BridgeError, HUSKY_CSV_FIELDS, content_hash, load_json, output_dir_for_run, parse_notes, sha256_file, value_candidates_to_husky_csv_rows, validate_weather_probability_bundle, write_husky_csv, write_json)
    from src.d1_value_signal_contract_v2 import ORDERBOOK_HASH_VALIDATION_LEVEL, SOURCE_TAG, validate_value_signal_bundle_v2
except ModuleNotFoundError:  # pragma: no cover
    from d1_signal_bridge_v1 import (BridgeError, HUSKY_CSV_FIELDS, content_hash, load_json, output_dir_for_run, parse_notes, sha256_file, value_candidates_to_husky_csv_rows, validate_weather_probability_bundle, write_husky_csv, write_json)
    from d1_value_signal_contract_v2 import ORDERBOOK_HASH_VALIDATION_LEVEL, SOURCE_TAG, validate_value_signal_bundle_v2

BRIDGE_VERSION = "d1_signal_bridge_v2"
REQUIRED_OUTPUT_FILES = ("weather_probability_bundle.json", "value_signal_bundle.json", "husky_entry_signals.csv", "bridge_manifest_core.json", "bridge_manifest.json", "bridge_manifest.sha256", "validation_report.json")
MANIFEST_HASHED_FILES = ("weather_probability_bundle.json", "value_signal_bundle.json", "bridge_manifest_core.json", "husky_entry_signals.csv", "validation_report.json")


def _core(weather: dict[str, Any], weather_v: dict[str, Any], value_v: dict[str, Any], entry_valid_minutes: int) -> dict[str, Any]:
    return {"bridge_version": BRIDGE_VERSION, "forecast_run_id": weather["forecast_run_id"], "model_version": weather["model_version"], "rules_version": weather["rules_version"], "station": weather_v["station"], "city": weather_v["city"], "weather_date_local": weather["weather_date_local"], "weather_metric": weather_v["weather_metric"], "as_of_time_utc": weather["as_of_time_utc"], "as_of_time_cst": weather["as_of_time_cst"], "data_status": value_v["data_status"], "weather_bundle_content_sha256": weather_v["bundle_sha256"], "value_bundle_content_sha256": value_v["value_sha256"], "conversion_parameters": {"entry_valid_minutes": int(entry_valid_minutes), "formal_mode": True, "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL}, "execution_eligible": True, "accepted_candidates": value_v["accepted"], "rejected_candidates": [], "formal_ledger_used": False, "wallet_or_real_order_used": False}


def _rows(weather: dict[str, Any], weather_v: dict[str, Any], value_v: dict[str, Any], core_sha: str, entry_valid_minutes: int) -> list[dict[str, str]]:
    rows = value_candidates_to_husky_csv_rows(weather, value_v, entry_valid_minutes=entry_valid_minutes, weather_sha256=weather_v["bundle_sha256"], value_sha256=value_v["value_sha256"], bridge_manifest_sha256=core_sha)
    for row in rows:
        row["source"] = SOURCE_TAG
    return rows


def _report(weather: dict[str, Any], weather_v: dict[str, Any], value_v: dict[str, Any], rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"bridge_version": BRIDGE_VERSION, "forecast_run_id": weather["forecast_run_id"], "weather_validation": {"data_status": weather_v["data_status"], "warnings": weather_v["warnings"], "probability_sum": weather_v["probability_sum"], "bundle_sha256": weather_v["bundle_sha256"]}, "value_validation": {"accepted_count": value_v["accepted_count"], "rejected_count": 0, "value_sha256": value_v["value_sha256"], "schema_runtime": value_v["schema_runtime"]}, "converted_signal_count": len(rows), "rejected_signal_count": 0, "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL, "execution_eligible": True, "formal_ledger_used": False, "wallet_or_real_order_used": False}


def _manifest(weather: dict[str, Any], weather_v: dict[str, Any], value_v: dict[str, Any], rows: list[dict[str, str]], work: Path, core_sha: str) -> dict[str, Any]:
    return {"bridge_version": BRIDGE_VERSION, "value_schema_version": "2.0", "forecast_run_id": weather["forecast_run_id"], "model_version": weather["model_version"], "rules_version": weather["rules_version"], "station": weather_v["station"], "city": weather_v["city"], "weather_date_local": weather["weather_date_local"], "weather_metric": weather_v["weather_metric"], "as_of_time_utc": weather["as_of_time_utc"], "as_of_time_cst": weather["as_of_time_cst"], "data_status": value_v["data_status"], "converted_signal_count": len(rows), "rejected_signal_count": 0, "formal_ledger_used": False, "wallet_or_real_order_used": False, "orderbook_hash_verification": ORDERBOOK_HASH_VALIDATION_LEVEL, "execution_eligible": True, "input_content_hashes": {"weather_probability_bundle": weather_v["bundle_sha256"], "value_signal_bundle": value_v["value_sha256"]}, "files": {name: {"path": name, "sha256": sha256_file(work / name)} for name in MANIFEST_HASHED_FILES}}


def verify_bridge_output(output_dir: Path) -> dict[str, Any]:
    out, errors = Path(output_dir), []
    result: dict[str, Any] = {key: {"ok": False} for key in ("weather_revalidation_result", "value_revalidation_result", "core_rebuild_result", "csv_rebuild_result", "report_rebuild_result", "manifest_identity_result", "semantic_replay_result")}
    if any(not (out / f).is_file() for f in REQUIRED_OUTPUT_FILES):
        return {**result, "ok": False, "errors": ["missing_required_output"], "manifest": None}
    try:
        weather, value, core, report, manifest = (load_json(out / n) for n in ("weather_probability_bundle.json", "value_signal_bundle.json", "bridge_manifest_core.json", "validation_report.json", "bridge_manifest.json"))
        if (out / "bridge_manifest.sha256").read_text(encoding="utf-8").strip() != sha256_file(out / "bridge_manifest.json"): errors.append("detached_manifest_sha_mismatch")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(MANIFEST_HASHED_FILES):
            errors.append("manifest_files_set_mismatch")
            files = files if isinstance(files, dict) else {}
        for name in MANIFEST_HASHED_FILES:
            meta = files.get(name)
            if (
                not isinstance(meta, dict)
                or set(meta) != {"path", "sha256"}
                or meta.get("path") != name
                or sha256_file(out / name) != meta.get("sha256")
            ):
                errors.append("artifact_hash_mismatch:" + name)
        if manifest.get("bridge_version") != BRIDGE_VERSION: errors.append("VALUE_BUNDLE_VERSION_UNKNOWN")
        if manifest.get("orderbook_hash_verification") != ORDERBOOK_HASH_VALIDATION_LEVEL: errors.append("VALUE_V2_DOWNGRADE_FORBIDDEN")
        weather_v = validate_weather_probability_bundle(weather, formal_mode=True); result["weather_revalidation_result"]={"ok":True}
        value_v = validate_value_signal_bundle_v2(weather, value, formal_mode=True); result["value_revalidation_result"]={"ok":True}
        if core == _core(weather, weather_v, value_v, int(core["conversion_parameters"]["entry_valid_minutes"])): result["core_rebuild_result"]={"ok":True}
        else: errors.append("core_rebuild_mismatch")
        rows = _rows(weather, weather_v, value_v, sha256_file(out / "bridge_manifest_core.json"), int(core["conversion_parameters"]["entry_valid_minutes"]))
        with (out / "husky_entry_signals.csv").open(encoding="utf-8", newline="") as f: actual_rows=list(csv.DictReader(f))
        if actual_rows == rows: result["csv_rebuild_result"]={"ok":True}
        else: errors.append("csv_rebuild_mismatch")
        if report == _report(weather, weather_v, value_v, rows): result["report_rebuild_result"]={"ok":True}
        else: errors.append("report_rebuild_mismatch")
        expected = _manifest(weather, weather_v, value_v, rows, out, sha256_file(out / "bridge_manifest_core.json"))
        if manifest == expected: result["manifest_identity_result"]={"ok":True}
        else: errors.append("manifest_identity_mismatch")
    except BridgeError as exc:
        errors.append(exc.code)
    except Exception as exc:
        errors.append("output_parse_error:" + type(exc).__name__)
    result["semantic_replay_result"]={"ok": not errors}
    result.update({"ok":not errors, "errors":errors, "manifest": manifest if "manifest" in locals() else None, "core_sha256": sha256_file(out / "bridge_manifest_core.json") if (out / "bridge_manifest_core.json").exists() else "", "manifest_sha256":sha256_file(out / "bridge_manifest.json") if (out / "bridge_manifest.json").exists() else ""})
    return result


def convert_bundles(weather: dict[str, Any], value: dict[str, Any], output_root: Path, *, formal_mode: bool = True, entry_valid_minutes: int = 10) -> dict[str, Any]:
    if formal_mode is not True: raise BridgeError("INFORMAL_EXECUTION_EXPORT_FORBIDDEN", "V2 only exports formal-compatible CSV")
    weather_v = validate_weather_probability_bundle(weather, formal_mode=True)
    value_v = validate_value_signal_bundle_v2(weather, value, formal_mode=True)
    out = output_dir_for_run(Path(output_root), str(weather["forecast_run_id"]))
    if out.exists():
        checked=verify_bridge_output(out)
        if checked["ok"] and checked["manifest"].get("input_content_hashes",{}).get("value_signal_bundle")==value_v["value_sha256"]: return {"status":"reused", "output_dir":str(out), **checked}
        raise BridgeError("FORECAST_RUN_ID_CONFLICT", "existing output cannot be overwritten")
    tmp=Path(output_root) / (".tmp_" + out.name + "_" + uuid.uuid4().hex); tmp.mkdir(parents=True)
    try:
        write_json(tmp / "weather_probability_bundle.json", weather); write_json(tmp / "value_signal_bundle.json", value)
        core=_core(weather,weather_v,value_v,entry_valid_minutes); write_json(tmp / "bridge_manifest_core.json",core); core_sha=sha256_file(tmp/"bridge_manifest_core.json")
        rows=_rows(weather,weather_v,value_v,core_sha,entry_valid_minutes); write_husky_csv(tmp/"husky_entry_signals.csv",rows)
        write_json(tmp/"validation_report.json",_report(weather,weather_v,value_v,rows)); manifest=_manifest(weather,weather_v,value_v,rows,tmp,core_sha); write_json(tmp/"bridge_manifest.json",manifest); (tmp/"bridge_manifest.sha256").write_text(sha256_file(tmp/"bridge_manifest.json")+"\n",encoding="utf-8")
        checked=verify_bridge_output(tmp)
        if not checked["ok"]: raise BridgeError("OUTPUT_SELF_CHECK_FAILED", "V2 output did not replay", errors=checked["errors"])
        os.replace(tmp,out)
    except Exception:
        if tmp.exists(): shutil.rmtree(tmp)
        raise
    return {"status":"created", "output_dir":str(out), "manifest":manifest, "husky_csv":str(out/"husky_entry_signals.csv"), "converted_signal_count":len(rows), "rejected_signal_count":0, "core_sha256":core_sha, "manifest_sha256":sha256_file(out/"bridge_manifest.json")}
