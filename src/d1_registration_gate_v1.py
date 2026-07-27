"""Registration-time provenance gate for D1 bridge signal files.

The bridge verifier remains the sole authority for bridge-output integrity and
semantic replay.  This module adds the registration-specific checks that bind
the exact CSV being submitted to that verified output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    if path.is_symlink():
        raise D1RegistrationGateError(code, "symbolic-link path is not permitted")


def _assert_no_explicit_traversal(path: Path) -> None:
    if ".." in path.parts:
        raise D1RegistrationGateError("D1_PATH_TRAVERSAL_FORBIDDEN", str(path))


def _strict_positive_int(value: Any, field: str, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise D1RegistrationGateError(code, {field: value})
    return value


def _strict_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise D1RegistrationGateError("D1_ENTRY_DEADLINE_MISMATCH", {field: value})
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise D1RegistrationGateError("D1_ENTRY_DEADLINE_MISMATCH", {field: value}) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise D1RegistrationGateError("D1_ENTRY_DEADLINE_MISMATCH", {field: value})
    return parsed.astimezone(timezone.utc)


def _portable_run_locator(forecast_run_id: Any) -> str:
    if not isinstance(forecast_run_id, str) or not forecast_run_id:
        raise D1RegistrationGateError("D1_RUN_DIRECTORY_LOCATOR_INVALID", forecast_run_id)
    encoded = quote(forecast_run_id, safe="-._")
    locator = f"d1_signal_bridge/{encoded}"
    if (
        locator.startswith("/")
        or ".." in Path(locator).parts
        or "\\" in locator
        or re.match(r"^[A-Za-z]:", locator)
        or "/Users/baobaotou" in locator
    ):
        raise D1RegistrationGateError("D1_RUN_DIRECTORY_LOCATOR_INVALID", locator)
    return locator


def _assert_csv_deadlines(rows: list[dict[str, str]], entry_valid_minutes: int) -> None:
    for row in rows:
        created = _strict_utc(row.get("created_at_utc"), "created_at_utc")
        deadline = _strict_utc(row.get("entry_deadline_utc"), "entry_deadline_utc")
        expected_deadline = created + timedelta(minutes=entry_valid_minutes)
        if deadline != expected_deadline:
            raise D1RegistrationGateError(
                "D1_ENTRY_DEADLINE_MISMATCH",
                {"signal_id": row.get("signal_id", ""), "expected": expected_deadline.isoformat(), "actual": deadline.isoformat()},
            )


def _gate_run_directory(csv_path: Path) -> Path:
    _assert_no_explicit_traversal(csv_path)
    if csv_path.name != CANONICAL_CSV_NAME:
        raise D1RegistrationGateError("D1_CSV_NOT_CANONICAL", csv_path.name)
    if not csv_path.is_file():
        raise D1RegistrationGateError("D1_BRIDGE_OUTPUT_MISSING", CANONICAL_CSV_NAME)
    _assert_no_symlink(csv_path, "D1_SYMLINK_PATH_FORBIDDEN")
    run_dir = csv_path.parent
    _assert_no_symlink(run_dir, "D1_SYMLINK_PATH_FORBIDDEN")
    for name in REQUIRED_OUTPUT_FILES:
        child = run_dir / name
        if not child.is_file():
            raise D1RegistrationGateError("D1_BRIDGE_OUTPUT_MISSING", name)
        _assert_no_symlink(child, "D1_SYMLINK_PATH_FORBIDDEN")
        if child.resolve().parent != run_dir.resolve():
            raise D1RegistrationGateError("D1_PATH_TRAVERSAL_FORBIDDEN", name)
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


def _assert_row_bindings(rows: list[dict[str, str]], core: dict[str, Any], manifest: dict[str, Any], core_sha: str, entry_valid_minutes: int) -> dict[str, str]:
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
    deadlines: dict[str, str] = {}
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
        created = _strict_utc(row.get("created_at_utc"), "created_at_utc")
        deadline = _strict_utc(row.get("entry_deadline_utc"), "entry_deadline_utc")
        _assert_csv_deadlines([row], entry_valid_minutes)
        deadlines[str(row.get("signal_id", ""))] = deadline.isoformat()
    if seen_runs != {run_id}:
        raise D1RegistrationGateError("D1_RUN_ID_MISMATCH")
    return deadlines


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
    del mode  # Both modes deliberately share one verification path.
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
        # The bridge verifier remains authoritative.  Classify a malformed
        # deadline precisely after it has run, so callers receive the stable
        # registration-contract error rather than a generic replay failure.
        try:
            failed_core = _load_json(run_dir / "bridge_manifest_core.json")
            failed_window = _strict_positive_int(
                (failed_core.get("conversion_parameters") or {}).get("entry_valid_minutes"),
                "bridge_entry_valid_minutes",
                code="D1_ENTRY_WINDOW_MISMATCH",
            )
            _assert_csv_deadlines(parsed_rows, failed_window)
        except D1RegistrationGateError as exc:
            if exc.code == "D1_ENTRY_DEADLINE_MISMATCH":
                raise
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
    bridge_window = _strict_positive_int(
        (core.get("conversion_parameters") or {}).get("entry_valid_minutes"),
        "bridge_entry_valid_minutes",
        code="D1_ENTRY_WINDOW_MISMATCH",
    )
    husky_window = _strict_positive_int(
        (config.get("entry") or {}).get("entry_valid_minutes"),
        "husky_entry_valid_minutes",
        code="D1_ENTRY_WINDOW_MISMATCH",
    )
    if bridge_window != husky_window:
        raise D1RegistrationGateError(
            "D1_ENTRY_WINDOW_MISMATCH",
            {"bridge_entry_valid_minutes": bridge_window, "husky_entry_valid_minutes": husky_window, "forecast_run_id": manifest.get("forecast_run_id")},
        )
    # Re-read through the canonical path so the submitted rows cannot be a
    # caller-supplied, detached in-memory subset.
    with (run_dir / CANONICAL_CSV_NAME).open(encoding="utf-8", newline="") as handle:
        canonical_rows = list(csv.DictReader(handle))
    if parsed_rows != canonical_rows:
        raise D1RegistrationGateError("D1_CSV_CONTENT_MISMATCH")
    deadlines = _assert_row_bindings(canonical_rows, core, manifest, core_sha, bridge_window)
    portable = _portable_run_locator(manifest.get("forecast_run_id"))
    return {
        "d1_bridge_verified": True,
        "verification_time_utc": "",  # Filled by the registration transaction.
        "forecast_run_id": manifest["forecast_run_id"],
        "bridge_version": manifest.get("bridge_version", ""),
        "model_version": core.get("model_version", ""),
        "rules_version": core.get("rules_version", ""),
        "run_directory_relative": portable,
        "entry_valid_minutes": bridge_window,
        "csv_entry_deadlines": deadlines,
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
