"""Registration-time provenance gate for D1 bridge signal files.

The bridge verifier remains the sole authority for bridge-output integrity and
semantic replay.  This module adds the registration-specific checks that bind
the exact CSV being submitted to that verified output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from src.d1_signal_bridge_v1 import (
        MODEL_FORMAL_NAME,
        ORDERBOOK_HASH_VALIDATION_LEVEL,
        RULES_VERSION_REQUIRED,
        SOURCE_TAG,
        verify_bridge_output,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from d1_signal_bridge_v1 import (
        MODEL_FORMAL_NAME,
        ORDERBOOK_HASH_VALIDATION_LEVEL,
        RULES_VERSION_REQUIRED,
        SOURCE_TAG,
        verify_bridge_output,
    )


CANONICAL_CSV_NAME = "husky_entry_signals.csv"
REQUIRED_OUTPUT_FILES = (
    "weather_probability_bundle.json",
    "value_signal_bundle.json",
    "bridge_manifest_core.json",
    CANONICAL_CSV_NAME,
    "validation_report.json",
    "bridge_manifest.json",
    "bridge_manifest.sha256",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class D1RegistrationGateError(ValueError):
    """Stable registration-gate failure with auditable detail."""

    def __init__(self, code: str, details: Any = None):
        self.code = code
        self.details = details
        suffix = "" if details in (None, "", [], {}) else f": {details}"
        super().__init__(f"{code}{suffix}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D1RegistrationGateError("D1_BRIDGE_VERIFY_FAILED", str(exc)) from exc
    if not isinstance(value, dict):
        raise D1RegistrationGateError("D1_BRIDGE_VERIFY_FAILED", f"{path.name} is not an object")
    return value


def _strict_notes(raw: Any) -> dict[str, str]:
    if not isinstance(raw, str):
        raise D1RegistrationGateError("D1_NOTES_MISMATCH", "notes must be a string")
    values: dict[str, str] = {}
    for part in raw.split(";"):
        if not part or "=" not in part:
            raise D1RegistrationGateError("D1_NOTES_MISMATCH", "malformed notes field")
        key, value = part.split("=", 1)
        if not key or key in values:
            raise D1RegistrationGateError("D1_NOTES_MISMATCH", "missing or duplicate notes key")
        values[key] = value
    return values


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise D1RegistrationGateError("D1_HASH_FORMAT_INVALID", field)
    return value


def _assert_no_symlink(path: Path, code: str) -> None:
    if path.is_symlink() or path.absolute() != path.resolve():
        raise D1RegistrationGateError(code, "symbolic-link path is not permitted")


def _gate_run_directory(csv_path: Path) -> Path:
    if csv_path.name != CANONICAL_CSV_NAME:
        raise D1RegistrationGateError("D1_CSV_NOT_CANONICAL", csv_path.name)
    if not csv_path.is_file():
        raise D1RegistrationGateError("D1_BRIDGE_OUTPUT_MISSING", CANONICAL_CSV_NAME)
    _assert_no_symlink(csv_path, "D1_CSV_NOT_CANONICAL")
    run_dir = csv_path.parent
    _assert_no_symlink(run_dir, "D1_CSV_NOT_CANONICAL")
    for name in REQUIRED_OUTPUT_FILES:
        child = run_dir / name
        if not child.is_file():
            raise D1RegistrationGateError("D1_BRIDGE_OUTPUT_MISSING", name)
        _assert_no_symlink(child, "D1_BRIDGE_OUTPUT_MISSING")
        if child.resolve().parent != run_dir.resolve():
            raise D1RegistrationGateError("D1_BRIDGE_OUTPUT_MISSING", name)
    return run_dir


def _all_bridge_results_ok(verified: dict[str, Any]) -> bool:
    required = (
        "semantic_replay_result",
        "weather_revalidation_result",
        "value_revalidation_result",
        "core_rebuild_result",
        "csv_rebuild_result",
        "report_rebuild_result",
        "manifest_identity_result",
    )
    return bool(verified.get("ok")) and all(bool((verified.get(key) or {}).get("ok")) for key in required)


def _assert_safety_flags(core: dict[str, Any], manifest: dict[str, Any], report: dict[str, Any]) -> None:
    if (core.get("conversion_parameters") or {}).get("formal_mode") is not True:
        raise D1RegistrationGateError("D1_FORMAL_MODE_REQUIRED")
    if any(value.get("execution_eligible") is not True for value in (core, manifest, report)):
        raise D1RegistrationGateError("D1_EXECUTION_NOT_ELIGIBLE")
    if any(value.get("formal_ledger_used") is not False or value.get("wallet_or_real_order_used") is not False for value in (core, manifest, report)):
        raise D1RegistrationGateError("D1_SAFETY_FLAG_INVALID")
    if (
        core.get("conversion_parameters", {}).get("orderbook_hash_verification") != ORDERBOOK_HASH_VALIDATION_LEVEL
        or manifest.get("orderbook_hash_verification") != ORDERBOOK_HASH_VALIDATION_LEVEL
        or report.get("orderbook_hash_verification") != ORDERBOOK_HASH_VALIDATION_LEVEL
    ):
        raise D1RegistrationGateError("D1_ORDERBOOK_VERIFICATION_LEVEL_INVALID")


def _assert_row_bindings(rows: list[dict[str, str]], core: dict[str, Any], manifest: dict[str, Any], core_sha: str) -> None:
    candidates = core.get("accepted_candidates")
    if not isinstance(candidates, list) or len(rows) != len(candidates):
        raise D1RegistrationGateError("D1_CSV_CONTENT_MISMATCH")
    run_id = manifest.get("forecast_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise D1RegistrationGateError("D1_RUN_ID_MISMATCH")
    weather_sha = _require_hash((manifest.get("input_content_hashes") or {}).get("weather_probability_bundle"), "manifest.weather")
    value_sha = _require_hash((manifest.get("input_content_hashes") or {}).get("value_signal_bundle"), "manifest.value")
    _require_hash(core_sha, "bridge_manifest_core")
    seen_runs: set[str] = set()
    for index, (row, candidate) in enumerate(zip(rows, candidates), start=1):
        if not isinstance(candidate, dict):
            raise D1RegistrationGateError("D1_CANDIDATE_BINDING_MISMATCH", index)
        notes = _strict_notes(row.get("notes"))
        seen_runs.add(notes.get("forecast_run_id", ""))
        if notes.get("forecast_run_id") != run_id:
            raise D1RegistrationGateError("D1_RUN_ID_MISMATCH", index)
        if notes.get("model_version") != MODEL_FORMAL_NAME or notes.get("rules_version") != RULES_VERSION_REQUIRED:
            raise D1RegistrationGateError("D1_NOTES_MISMATCH", index)
        if notes.get("formal_mode") != "true" or notes.get("execution_eligible") != "true":
            raise D1RegistrationGateError("D1_SAFETY_FLAG_INVALID", index)
        hashes = {
            "weather_bundle_sha256": weather_sha,
            "value_bundle_sha256": value_sha,
            "bridge_manifest_sha256": core_sha,
            "orderbook_snapshot_sha256": candidate.get("orderbook_snapshot_sha256"),
        }
        for field, expected in hashes.items():
            actual = _require_hash(notes.get(field), field)
            if actual != expected:
                raise D1RegistrationGateError("D1_HASH_MISMATCH", field)
        required_pairs = {
            "data_status": candidate.get("data_status"),
            "orderbook_snapshot_id": candidate.get("orderbook_snapshot_id"),
        }
        if any(notes.get(key) != value for key, value in required_pairs.items()):
            raise D1RegistrationGateError("D1_CANDIDATE_BINDING_MISMATCH", index)
    if seen_runs != {run_id}:
        raise D1RegistrationGateError("D1_RUN_ID_MISMATCH")


def verify_d1_registration_bundle(
    csv_path: Path,
    parsed_rows: list[dict[str, str]],
    mode: str,
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Verify the immutable D1 run that supplied ``parsed_rows``.

    Returns ``None`` for a wholly non-D1 file.  Any D1 row makes the entire
    input subject to this gate in both DEMO and FORMAL modes.
    """
    del mode, config  # Both modes deliberately share one verification path.
    candidate_path = Path(csv_path)
    sources = {str(row.get("source", "")) for row in parsed_rows}
    d1_present = SOURCE_TAG in sources
    if not d1_present:
        # A changed source column must not turn an otherwise complete D1 run
        # into an unverified legacy submission.
        if candidate_path.name == CANONICAL_CSV_NAME and all((candidate_path.parent / name).is_file() for name in REQUIRED_OUTPUT_FILES):
            raise D1RegistrationGateError("D1_MIXED_SOURCE_FILE", "bridge directory CSV must retain the D1 source tag")
        return None
    if sources != {SOURCE_TAG}:
        raise D1RegistrationGateError("D1_MIXED_SOURCE_FILE")
    run_dir = _gate_run_directory(candidate_path)
    verified = verify_bridge_output(run_dir)
    if not _all_bridge_results_ok(verified):
        raise D1RegistrationGateError("D1_BRIDGE_VERIFY_FAILED", verified.get("errors", []))
    manifest = verified.get("manifest")
    if not isinstance(manifest, dict):
        raise D1RegistrationGateError("D1_BRIDGE_VERIFY_FAILED", "manifest unavailable")
    core = _load_json(run_dir / "bridge_manifest_core.json")
    report = _load_json(run_dir / "validation_report.json")
    _assert_safety_flags(core, manifest, report)
    core_sha = _sha256_file(run_dir / "bridge_manifest_core.json")
    if core_sha != verified.get("core_sha256"):
        raise D1RegistrationGateError("D1_HASH_MISMATCH", "bridge_manifest_core")
    # Re-read through the canonical path so the submitted rows cannot be a
    # caller-supplied, detached in-memory subset.
    with (run_dir / CANONICAL_CSV_NAME).open(encoding="utf-8", newline="") as handle:
        canonical_rows = list(csv.DictReader(handle))
    if parsed_rows != canonical_rows:
        raise D1RegistrationGateError("D1_CSV_CONTENT_MISMATCH")
    _assert_row_bindings(canonical_rows, core, manifest, core_sha)
    try:
        portable = os.path.relpath(run_dir, Path(root).resolve())
    except ValueError:
        portable = run_dir.name
    return {
        "d1_bridge_verified": True,
        "verification_time_utc": "",  # Filled by the registration transaction.
        "forecast_run_id": manifest["forecast_run_id"],
        "bridge_version": manifest.get("bridge_version", ""),
        "model_version": core.get("model_version", ""),
        "rules_version": core.get("rules_version", ""),
        "run_directory_relative": portable,
        "bridge_manifest_sha256": verified.get("manifest_sha256", ""),
        "bridge_manifest_core_sha256": core_sha,
        "weather_bundle_sha256": (manifest.get("input_content_hashes") or {}).get("weather_probability_bundle", ""),
        "value_bundle_sha256": (manifest.get("input_content_hashes") or {}).get("value_signal_bundle", ""),
        "semantic_replay_result": "pass",
        "execution_eligible": True,
        "formal_mode": True,
        "source": SOURCE_TAG,
        "verified_signal_count": len(canonical_rows),
        "verified_signal_ids_json": json.dumps([row.get("signal_id", "") for row in canonical_rows], separators=(",", ":")),
        "formal_ledger_used": False,
        "wallet_or_real_order_used": False,
    }
