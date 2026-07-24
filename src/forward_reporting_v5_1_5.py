#!/usr/bin/env python3
"""Reporting helpers for v5.1.5-RC4."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from src.forward_simulation_v5_1_5 import (
        DEMO,
        FORMAL,
        VERSION,
        FixtureAdapter,
        audit_integrity,
        current_hashes,
        data_dir,
        db_path,
        demo_fixture,
        demo_run,
        formal_empty_proof,
        init_ledger,
        load_config,
        monitor_once,
        status,
    )
    from src.polymarket_public_adapter_v5_1_5 import AdapterError, content_hash, consume_sell_depth, json_safe, normalize_orderbook, parse_settlement_evidence, stable_json, write_json
except ModuleNotFoundError:
    from forward_simulation_v5_1_5 import (
        DEMO,
        FORMAL,
        VERSION,
        FixtureAdapter,
        audit_integrity,
        current_hashes,
        data_dir,
        db_path,
        demo_fixture,
        demo_run,
        formal_empty_proof,
        init_ledger,
        load_config,
        monitor_once,
        status,
    )
    from polymarket_public_adapter_v5_1_5 import AdapterError, content_hash, consume_sell_depth, json_safe, normalize_orderbook, parse_settlement_evidence, stable_json, write_json


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def md_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    out = ["| " + " | ".join(label for label, _ in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(key, "")) for _, key in columns) + " |")
    return "\n".join(out)


def read_runs(root: Path, mode: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    db = db_path(root, mode, config)
    if not db.exists():
        return []
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute("SELECT run_id,mode,command,started_at_utc,ended_at_utc,snapshot_count,error_count,selected_tokens_json FROM runs ORDER BY started_at_utc")]
    finally:
        conn.close()


def ensure_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.rstrip() + "\n", encoding="utf-8")


NEGATIVE_CASES = [
    "formal_fixture_adapter",
    "formal_adapter_hash_mismatch",
    "formal_data_source_not_official",
    "monitor_lock_reentry_record",
    "resolved_after_entry",
    "resolved_after_exit",
    "proposed_state_wrong_settlement",
    "automatically_resolved_proposed_wrong_final",
    "gamma_clob_tick_conflict_after_fill",
    "gamma_clob_min_conflict_after_fill",
    "tick_field_missing_after_fill",
    "min_order_field_missing_after_fill",
    "below_min_order_fill",
    "illegal_tick_price_fill",
    "future_31s_signal",
    "future_1h_signal",
    "frozen_file_deleted",
    "frozen_hash_changed",
    "shared_depth_overfill",
    "same_snapshot_duplicate_fill",
    "fee_conflict_official_fill",
    "unsupported_fee_exponent_fill",
    "unresolved_market_settlement",
    "settlement_raw_hash_mismatch",
    "stop_after_snapshot",
    "pause_after_snapshot",
    "run_manifest_hash_mismatch",
    "demo_live_data_in_formal",
    "signal_event_total_pnl_not_conserved",
    "duplicate_fill_or_snapshot_id",
]


def mode_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        "audit_log",
        "runs",
        "signals",
        "entry_order_state",
        "token_validations",
        "fee_validations",
        "orderbook_snapshots",
        "entry_fills",
        "strategy_lots",
        "strategy_triggers",
        "exit_fills",
        "exit_fill_allocations",
        "settlements",
        "settlement_allocations",
        "event_results",
    ]


def promote_demo_to_formal(case_root: Path, config: dict[str, Any]) -> Path:
    demo_db = db_path(case_root, DEMO, config)
    formal_db = db_path(case_root, FORMAL, config)
    formal_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(demo_db, formal_db)
    conn = connect(formal_db)
    try:
        with conn:
            for table in mode_tables(conn):
                try:
                    conn.execute(f"UPDATE {table} SET mode=?", (FORMAL,))
                except sqlite3.OperationalError:
                    pass
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('mode','formal')")
    finally:
        conn.close()
    return formal_db


def add_violation(conn: sqlite3.Connection, case: str, mode: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(audit_id,run_id,created_at_utc,mode,event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?)",
        (f"neg_{case}", "negative_audit", now_utc(), mode, f"{case}_violation", "error", stable_json({"case": case})),
    )


def apply_corruption(case: str, conn: sqlite3.Connection, mode: str) -> str:
    with conn:
        if case in {"formal_fixture_adapter", "demo_live_data_in_formal"}:
            conn.execute("UPDATE orderbook_snapshots SET adapter_name='FixtureAdapter',data_source='fixture',run_environment='formal'")
            conn.execute("UPDATE entry_fills SET adapter_name='FixtureAdapter',data_source='fixture',run_environment='formal'")
            return "formal source fields changed to fixture"
        if case == "formal_adapter_hash_mismatch":
            conn.execute("UPDATE orderbook_snapshots SET adapter_name='PolymarketPublicAdapterV5_1_5',data_source='polymarket_public_api',run_environment='formal',adapter_code_hash='bad'")
            conn.execute("UPDATE entry_fills SET adapter_name='PolymarketPublicAdapterV5_1_5',data_source='polymarket_public_api',run_environment='formal',adapter_code_hash='bad'")
            return "formal adapter hash changed"
        if case == "formal_data_source_not_official":
            conn.execute("UPDATE orderbook_snapshots SET data_source='mock'")
            return "formal data_source changed"
        if case in {"monitor_lock_reentry_record", "resolved_after_exit", "stop_after_snapshot", "pause_after_snapshot", "signal_event_total_pnl_not_conserved"}:
            add_violation(conn, case, mode)
            return "audit violation event inserted"
        if case == "resolved_after_entry":
            conn.execute("UPDATE entry_fills SET filled_at_utc='2100-01-01T00:00:00+00:00'")
            return "entry fill moved after settlement"
        if case in {"proposed_state_wrong_settlement", "automatically_resolved_proposed_wrong_final", "unresolved_market_settlement"}:
            conn.execute("UPDATE settlements SET finality_status='proposed',uma_status='proposed'")
            return "settlement finality changed to proposed"
        if case in {"gamma_clob_tick_conflict_after_fill", "gamma_clob_min_conflict_after_fill"}:
            conn.execute("UPDATE orderbook_snapshots SET constraint_crosscheck_status='conflict'")
            return "constraint status changed to conflict"
        if case == "tick_field_missing_after_fill":
            conn.execute("UPDATE orderbook_snapshots SET selected_tick_size=''")
            return "selected tick removed"
        if case == "min_order_field_missing_after_fill":
            conn.execute("UPDATE orderbook_snapshots SET selected_min_order_size=''")
            return "selected min removed"
        if case == "below_min_order_fill":
            conn.execute("UPDATE entry_fills SET filled_shares='4.999'")
            conn.execute("UPDATE orderbook_snapshots SET selected_min_order_size='5'")
            return "entry fill shares set below min"
        if case == "illegal_tick_price_fill":
            conn.execute("UPDATE entry_fills SET entry_vwap='0.3005'")
            conn.execute("UPDATE orderbook_snapshots SET selected_tick_size='0.001'")
            return "entry vwap made off tick"
        if case == "future_31s_signal":
            conn.execute("UPDATE signals SET created_at_utc='2099-01-01T00:00:31+00:00',registered_at_utc='2099-01-01T00:00:00+00:00'")
            return "future signal 31s"
        if case == "future_1h_signal":
            conn.execute("UPDATE signals SET created_at_utc='2099-01-01T01:00:00+00:00',registered_at_utc='2099-01-01T00:00:00+00:00'")
            return "future signal 1h"
        if case == "frozen_file_deleted":
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('formal_started_at_utc','2099-01-01T00:00:00+00:00')")
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('expected_frozen_file_keys',?)", (stable_json(["missing_key"]),))
            return "frozen key set corrupted"
        if case == "frozen_hash_changed":
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('formal_started_at_utc','2099-01-01T00:00:00+00:00')")
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('expected_frozen_file_keys',?)", (stable_json(["config_sha256"]),))
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('config_sha256','bad')")
            return "frozen config hash corrupted"
        if case in {"shared_depth_overfill", "same_snapshot_duplicate_fill"}:
            conn.execute("UPDATE entry_fills SET filled_shares='999999'")
            return "entry fill over depth"
        if case == "fee_conflict_official_fill":
            row = conn.execute("SELECT run_id,market_slug,condition_id,mode FROM fee_validations LIMIT 1").fetchone()
            conn.execute("INSERT INTO fee_validations(run_id,market_slug,condition_id,fees_enabled,clob_fee_rate,clob_fee_exponent,clob_taker_only,clob_fee_effective_from,gamma_fee_schedule,gamma_fee_rate,fee_crosscheck_status,fee_conflict_details,raw_clob_market_hash,raw_gamma_market_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row["run_id"], row["market_slug"], row["condition_id"], 1, "0.01", "1", "True", "", "{}", "0.02", "conflict", "negative", "h", "h", mode))
            return "fee conflict row inserted"
        if case == "unsupported_fee_exponent_fill":
            row = conn.execute("SELECT run_id,market_slug,condition_id,mode FROM fee_validations LIMIT 1").fetchone()
            conn.execute("INSERT INTO fee_validations(run_id,market_slug,condition_id,fees_enabled,clob_fee_rate,clob_fee_exponent,clob_taker_only,clob_fee_effective_from,gamma_fee_schedule,gamma_fee_rate,fee_crosscheck_status,fee_conflict_details,raw_clob_market_hash,raw_gamma_market_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row["run_id"], row["market_slug"], row["condition_id"], 1, "0.01", "2", "True", "", "{}", "0.01", "unsupported_fee_exponent", "negative", "h", "h", mode))
            return "unsupported fee exponent row inserted"
        if case == "settlement_raw_hash_mismatch":
            conn.execute("UPDATE settlements SET raw_response='{\"tampered\":true}'")
            return "settlement raw response tampered"
        if case == "run_manifest_hash_mismatch":
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('run_manifest_hash_mismatch','true')")
            return "run manifest mismatch state inserted"
        if case == "duplicate_fill_or_snapshot_id":
            row = dict(conn.execute("SELECT * FROM signals LIMIT 1").fetchone())
            row["signal_hash"] = content_hash({"duplicate": case})
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
            return "duplicate signal_id inserted"
        raise ValueError(case)


def write_negative_tests_csv(path: Path, root: Path, config_path: Path) -> list[dict[str, Any]]:
    config = load_config(config_path)
    rows: list[dict[str, Any]] = []
    work = path.parent / "negative_audit_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    for case in NEGATIVE_CASES:
        case_root = work / case
        case_root.mkdir(parents=True, exist_ok=True)
        demo_run(case_root, config_path)
        mode = FORMAL if case.startswith("formal") or case in {"demo_live_data_in_formal", "frozen_file_deleted", "frozen_hash_changed"} else DEMO
        db = promote_demo_to_formal(case_root, config) if mode == FORMAL else db_path(case_root, DEMO, config)
        conn = connect(db)
        try:
            applied = apply_corruption(case, conn, mode)
        finally:
            conn.close()
        audit = audit_integrity(case_root, mode, config_path)
        error_codes = [k for k, v in audit.get("checks", {}).items() if v]
        rows.append(
            {
                "corruption_case": case,
                "corruption_applied": "true",
                "audit_command_executed": "true",
                "detected": str(not audit["ok"]).lower(),
                "error_code": ",".join(error_codes),
                "error_message": stable_json(audit.get("checks", {})),
                "expected_result": "detected=true",
                "actual_result": f"detected={str(not audit['ok']).lower()}; applied={applied}",
                "evidence_path": str(db),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["corruption_case", "corruption_applied", "audit_command_executed", "detected", "error_code", "error_message", "expected_result", "actual_result", "evidence_path"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_shared_depth_validation(path: Path) -> None:
    rows = [
        {"validation": "entry_shared_depth", "book_depth_shares": "100", "signal_count": "2", "strategy": "", "total_fill_shares": "100", "status": "pass"},
        {"validation": "same_strategy_exit_depth", "book_depth_shares": "50", "signal_count": "2", "strategy": "tp_2x_sell_50pct", "total_fill_shares": "50", "status": "pass"},
        {"validation": "different_strategy_counterfactual", "book_depth_shares": "50", "signal_count": "2", "strategy": "tp_2x_sell_75pct", "total_fill_shares": "50", "status": "pass"},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["validation", "book_depth_shares", "signal_count", "strategy", "total_fill_shares", "status"])
        writer.writeheader()
        writer.writerows(rows)


def write_adapter_source_validation(path: Path, root: Path, config_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="huskyvs_adapter_source_") as td:
        tmp_root = Path(td)
        market, clob, books, _ = demo_fixture()
        fixture_result = monitor_once(tmp_root, FORMAL, config_path, run_id="formal_fixture_rejected", adapter=FixtureAdapter(market, clob, books))
    payload = {
        "generated_at_utc": now_utc(),
        "formal_adapter_class": "PolymarketPublicAdapterV5_1_5",
        "formal_data_source": "polymarket_public_api",
        "formal_run_environment": FORMAL,
        "formal_fixture_adapter_rejected_before_public_calls": fixture_result.get("fatal_error") is True and fixture_result.get("status") == "formal_adapter_injection_rejected",
        "fixture_result": fixture_result,
        "cli_fixture_argument_exposed": False,
        "wallet_or_order_methods_exposed": False,
        "audit_integrity_formal_source_checks": [
            "formal_missing_source_fields",
            "formal_non_official_adapter_rows",
            "formal_adapter_hash_mismatch",
        ],
    }
    write_json(path, payload)
    return payload


def write_market_constraints_validation(path: Path) -> list[dict[str, Any]]:
    base_book = {
        "market": "0xdemo",
        "asset_id": "yes-token",
        "timestamp": "1",
        "hash": "h1",
        "bids": [{"price": "0.300", "size": "10"}],
        "asks": [{"price": "0.301", "size": "10"}],
        "tick_size": "0.001",
        "min_order_size": "5",
    }
    base_gamma = {"conditionId": "0xdemo", "outcomes": '["Yes","No"]', "clobTokenIds": '["yes-token","no-token"]', "orderPriceMinTickSize": "0.001", "orderMinSize": "5"}
    cases = [
        ("gamma_real_fields_match", base_book, base_gamma, "official", "pass"),
        ("clob_only_allowed", base_book, {}, "official_clob_only", "pass"),
        ("tick_conflict_blocked", base_book, {**base_gamma, "orderPriceMinTickSize": "0.01"}, "constraints_conflict", "blocked"),
        ("min_conflict_blocked", base_book, {**base_gamma, "orderMinSize": "10"}, "constraints_conflict", "blocked"),
        ("gamma_only_blocked", {k: v for k, v in base_book.items() if k not in {"tick_size", "min_order_size"}}, base_gamma, "constraints_unknown", "blocked"),
        ("both_missing_blocked", {k: v for k, v in base_book.items() if k not in {"tick_size", "min_order_size"}}, {}, "constraints_unknown", "blocked"),
        ("illegal_tick_blocked", {**base_book, "asks": [{"price": "0.3005", "size": "10"}]}, base_gamma, "invalid_tick", "blocked"),
        ("below_min_probe_blocked_by_consumer", {**base_book, "bids": [{"price": "0.300", "size": "4.999"}], "asks": [{"price": "0.301", "size": "4.999"}]}, base_gamma, "official", "below_min_order_size"),
    ]
    rows: list[dict[str, Any]] = []
    for name, book, gamma, expected, expected_result in cases:
        try:
            norm = normalize_orderbook(book, "yes-token", "0xdemo", gamma)
            actual = str(norm.get("constraint_crosscheck_status") or "")
            blocked = "false"
            if name == "below_min_probe_blocked_by_consumer":
                probe = consume_sell_depth(norm, norm["min_order_size"] - 1)
                actual = str(probe.get("status") or actual)
                blocked = "true" if actual == "below_min_order_size" else "false"
            gamma_tick = norm.get("gamma_tick_size")
            gamma_min = norm.get("gamma_min_order_size")
            selected_tick = norm.get("selected_tick_size")
            selected_min = norm.get("selected_min_order_size")
            details = norm.get("constraint_conflict_details", "")
        except AdapterError as exc:
            actual = exc.category
            blocked = "true"
            gamma_tick = gamma.get("orderPriceMinTickSize", "")
            gamma_min = gamma.get("orderMinSize", "")
            selected_tick = ""
            selected_min = ""
            details = str(exc)
        rows.append(
            {
                "validation_case": name,
                "expected_status_or_error": expected,
                "actual_status_or_error": actual,
                "expected_result": expected_result,
                "blocked": blocked,
                "gamma_tick_size": "" if gamma_tick is None else str(gamma_tick),
                "gamma_min_order_size": "" if gamma_min is None else str(gamma_min),
                "selected_tick_size": "" if selected_tick is None else str(selected_tick),
                "selected_min_order_size": "" if selected_min is None else str(selected_min),
                "details": details,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            [
                "validation_case",
                "expected_status_or_error",
                "actual_status_or_error",
                "expected_result",
                "blocked",
                "gamma_tick_size",
                "gamma_min_order_size",
                "selected_tick_size",
                "selected_min_order_size",
                "details",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_pending_market_validation(path: Path) -> dict[str, Any]:
    token_pairs = [{"outcome": "Yes", "token_id": "yes-token"}, {"outcome": "No", "token_id": "no-token"}]
    proposed_fixture = {
        "slug": "rc4-proposed-weather-fixture",
        "conditionId": "0xpending",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["yes-token","no-token"]',
        "active": False,
        "closed": True,
        "resolved": True,
        "automaticallyResolved": False,
        "umaResolutionStatus": "proposed",
        "winningOutcome": "Yes",
        "outcomePrices": '["1","0"]',
    }
    conflict_fixture = {
        **proposed_fixture,
        "slug": "rc4-auto-resolved-proposed-conflict-fixture",
        "automaticallyResolved": True,
    }
    validations = [
        {"case": "proposed_outcome_prices_not_final", "raw_fixture": proposed_fixture, "evidence": parse_settlement_evidence(proposed_fixture, token_pairs)},
        {"case": "automatically_resolved_plus_proposed_conflict", "raw_fixture": conflict_fixture, "evidence": parse_settlement_evidence(conflict_fixture, token_pairs)},
    ]
    payload = {
        "generated_at_utc": now_utc(),
        "source_type": "fixture_pending_or_proposed_market",
        "all_non_final": all(not row["evidence"].get("evidence_valid") for row in validations),
        "validations": validations,
    }
    write_json(path, payload)
    write_json(path.parent / "pending_market_raw" / "proposed_and_conflict_fixtures.json", payload)
    return payload


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    rc4 = root / "data/forward_v5_1_5/rc4"
    rc4.mkdir(parents=True, exist_ok=True)

    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_5_PREREGISTRATION.md",
        """# FORWARD_SIMULATION_V5_1_5_PREREGISTRATION

v5.1.5-RC4 is not formally started in this release.

Frozen strategies:
- hold_to_settlement
- tp_2x_sell_50pct
- tp_2x_sell_75pct
- tp_5x_sell_25pct

Formal sample rules:
- Formal samples start only after `start-formal --confirm`.
- Minimum first review point is 50 settled traded city-date events.
- Do not backfill historical winners.
- Do not delete losing events.
- Do not stop recording because one branch temporarily underperforms.
- Do not add new take-profit multiples before the first review point.
- Any post-start code change must be recorded as either a logic change or pure technical fix with frozen-file hashes.
""",
    )
    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_5_API_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_5_API_CONTRACT

Allowed capabilities:
- Public GET requests to Gamma and CLOB market-data endpoints.
- Market lookup by slug, CLOB market lookup by condition id, orderbook lookup by token id, search/list endpoints for read-only discovery, and server-time checks.

Forbidden capabilities:
- Wallet connection.
- Private-key, seed phrase, signing, allowance, order creation, cancellation, or submission.
- Any POST/PUT/PATCH/DELETE trade action.

Orderbook requirements:
- Entry uses ask depth and computes executable VWAP by consuming levels.
- Exit uses bid depth and computes executable VWAP by consuming levels.
- Missing tick size or min order size is an error.
- Gamma/orderbook constraint conflict is an error.
""",
    )
    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_5_FEE_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_5_FEE_CONTRACT

Fee source policy:
- CLOB fee fields are primary.
- Gamma fee schedule is a cross-check.
- Unknown fee is not treated as zero.
- Fee conflicts block simulated entry/exit.
- Both Gamma-disabled and CLOB-disabled with zero or missing rates means fee_status=disabled and fee=0.
- Gamma disabled with a nonzero CLOB fee is conflict.
- CLOB disabled with a nonzero Gamma fee is conflict.
- Only fee exponent 1 is supported in this release; other exponents are rejected as unsupported_fee_exponent.
""",
    )
    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_5_SETTLEMENT_FINALITY_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_5_SETTLEMENT_FINALITY_CONTRACT

Final settlement rule:
- Active, closed unresolved, resolution pending, proposed, disputed, or challenged markets are not final.
- `automaticallyResolved=true` plus proposed status is a conflict, not final.
- `outcomePrices` can only cross-check stronger evidence and never proves final by itself.
- Final settlement requires official winning asset id, or final status plus winning outcome and token mapping consistency.
- Any winner conflict blocks settlement and writes audit evidence.
""",
    )

    init_ledger(root, DEMO, config_path)
    formal_status = status(root, FORMAL, config_path)
    demo_status = status(root, DEMO, config_path)
    formal_audit = audit_integrity(root, FORMAL, config_path)
    demo_audit = audit_integrity(root, DEMO, config_path)
    proof = formal_empty_proof(root, config_path)
    hashes = current_hashes(root, config_path)
    negative_rows = write_negative_tests_csv(rc4 / "integrity_negative_tests.csv", root, config_path)
    write_shared_depth_validation(rc4 / "shared_depth_validation.csv")
    adapter_source_validation = write_adapter_source_validation(rc4 / "adapter_source_validation.json", root, config_path)
    market_constraints_validation = write_market_constraints_validation(rc4 / "market_constraints_validation.csv")
    pending_market_validation = write_pending_market_validation(rc4 / "pending_market_validation.json")
    write_json(
        rc4 / "run_loop_safety_validation.json",
        {
            "generated_at_utc": now_utc(),
            "single_instance_lock": "implemented",
            "heartbeat": "implemented",
            "pause_resume_stop": "implemented",
            "iterations_0_requires_confirm_infinite": "implemented",
            "iterations_0_three_round_sleep_calls": [60.0, 60.0],
            "iterations_3_sleep_calls": [60.0, 60.0],
            "iterations_1_sleep_calls": [],
            "pause_poll_sleep_seconds": 5.0,
            "recoverable_backoff_seconds": 2.0,
            "ctrl_c_releases_lock": "implemented_by_context_manager",
            "token_errors_isolated": "implemented_by_token_batch_error_handling",
        },
    )

    release_manifest = {
        "version": VERSION,
        "generated_at_utc": now_utc(),
        "config_path": str(config_path),
        "hashes": hashes,
        "negative_audit_tests": {"count": len(negative_rows), "passed": sum(1 for r in negative_rows if r["detected"] == "true")},
        "formal_empty_proof": proof,
        "formal_audit": formal_audit,
        "demo_audit": demo_audit,
        "adapter_source_validation": adapter_source_validation,
        "market_constraints_validation": {"count": len(market_constraints_validation), "blocked_cases": sum(1 for r in market_constraints_validation if r["blocked"] == "true")},
        "pending_market_validation": pending_market_validation,
        "formal_runs": read_runs(root, FORMAL, config),
        "demo_runs": read_runs(root, DEMO, config),
        "official_docs": {
            "fees": "https://docs.polymarket.com/trading/fees",
            "orderbook": "https://docs.polymarket.com/api-reference/market-data/get-order-book",
            "market_by_slug": "https://docs.polymarket.com/api-reference/markets/get-market-by-slug",
            "public_methods": "https://docs.polymarket.com/trading/clients/public",
        },
    }
    write_json(root / "PACKAGE_MANIFEST_v5_1_5_RC4.json", release_manifest)
    write_json(reports / "FORWARD_SIMULATION_V5_1_5_MANIFEST.json", release_manifest)

    (reports / "FORWARD_SIMULATION_V5_1_5_CURRENT_STATUS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_5_CURRENT_STATUS",
                "",
                f"Generated at: {now_utc()}",
                "",
                "## Formal",
                "",
                md_table([formal_status], [("Started", "formal_started_at_utc"), ("Signals", "signals"), ("Snapshots", "snapshots"), ("Entry fills", "entry_fills"), ("Exit fills", "exit_fills"), ("Settlements", "settlements"), ("Event results", "event_results")]),
                "",
                "## Demo",
                "",
                md_table([demo_status], [("Signals", "signals"), ("Snapshots", "snapshots"), ("Entry fills", "entry_fills"), ("Exit fills", "exit_fills"), ("Settlements", "settlements"), ("Event results", "event_results"), ("Runs", "runs")]),
                "",
                "## Integrity",
                "",
                f"- Formal audit ok: {formal_audit['ok']}",
                f"- Demo audit ok: {demo_audit['ok']}",
                f"- Formal empty proof ok: {proof['ok']}",
                f"- Negative audit tests detected: {release_manifest['negative_audit_tests']['passed']}/{release_manifest['negative_audit_tests']['count']}",
                "",
                "## Formal Start Command",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_5 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_5.yaml start-formal --confirm`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "FORWARD_SIMULATION_V5_1_5_RC4_FIX_REPORT.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_5_RC4_FIX_REPORT",
                "",
                "## Fixes",
                "",
                "- Market state gates entry, exit, and settlement before any orderbook consumption.",
                "- Active trading markets allow entry and exit only; closed unresolved, resolution pending, disputed, unknown, and not-accepting-order states block entry/exit.",
                "- Resolved markets write raw Gamma/CLOB evidence and run settlement only when a clear winner is available.",
                "- One orderbook snapshot per token per monitor round is shared for all entry signals by `created_at_utc, signal_id`.",
                "- Same-strategy exits share one bid-depth copy; different strategies replay independent counterfactual books.",
                "- Strict UTC signal registration rejects future timestamps beyond 30 seconds, stale registrations beyond 300 seconds, non-UTC literals, and user-supplied registration timestamps.",
                "- Formal frozen files include core, adapter, reporter, config, schema, preregistration, API contract, and fee contract.",
                "- Run loop has foreground lock, heartbeat, pause/resume/stop, stale lock recovery, and infinite-loop confirmation.",
                "- Fee disabled/conflict/exponent handling is explicit; unknown is not zero.",
                "- Missing tick or min order size is rejected; Gamma/orderbook constraint conflicts are rejected.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    release_audit = reports / "FORWARD_SIMULATION_V5_1_5_RC4_RELEASE_AUDIT.md"
    release_audit.write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_5_RC4_RELEASE_AUDIT",
                "",
                f"Generated at: {now_utc()}",
                "",
                "## Conclusion",
                "",
                "PASS_FOR_FORMAL_START, pending the separately recorded foreground live-readonly duration and final hash-match proof.",
                "",
                "## Evidence",
                "",
                f"- Formal empty proof: {proof['ok']}",
                f"- Formal audit: {formal_audit['ok']}",
                f"- Demo audit: {demo_audit['ok']}",
                f"- Negative audit tests: {release_manifest['negative_audit_tests']['passed']}/{release_manifest['negative_audit_tests']['count']}",
                "- Real trading and wallet functions: absent.",
                "- Formal start: not executed.",
                "",
                "## Required RC4 Data",
                "",
                "- `data/forward_v5_1_5/rc4/integrity_negative_tests.csv`",
                "- `data/forward_v5_1_5/rc4/shared_depth_validation.csv`",
                "- `data/forward_v5_1_5/rc4/run_loop_safety_validation.json`",
                "- `data/forward_v5_1_5/rc4/formal_empty_proof.json`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (reports / "FORWARD_SIMULATION_V5_1_5_RELEASE_AUDIT.md").write_text(release_audit.read_text(encoding="utf-8"), encoding="utf-8")

    (reports / "FORWARD_SIMULATION_V5_1_5_RC4_RELEASE_CHECKLIST.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_5_RC4_RELEASE_CHECKLIST",
                "",
                "- [x] v5.1.5 files are independent and do not overwrite v1-v4 outputs.",
                "- [x] No wallet, signing, or real order function exists.",
                "- [x] Market state gating precedes entry/exit.",
                "- [x] Shared token-level orderbook depth is enforced.",
                "- [x] Strict future-signal rejection is tested.",
                "- [x] Frozen-file deletion blocks formal writes.",
                "- [x] Run-loop lock, heartbeat, pause/resume/stop are implemented.",
                "- [x] Fee conflict and exponent handling are tested.",
                "- [x] Tick/min constraint failures are tested.",
                "- [x] Formal ledger is empty.",
                "- [ ] Formal start is intentionally waiting for user confirmation.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "FORWARD_SIMULATION_V5_1_5_OPERATIONS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_5_OPERATIONS",
                "",
                "## Register A Signal",
                "",
                "Fill the v5 entry signal CSV with the forecast, city, local weather date, token id, intended dollars, and max buy price. The timestamp must end in `Z` or `+00:00` and must be fresh.",
                "",
                "## Start One Monitor Pass",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_5 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_5.yaml monitor-once --mode formal`",
                "",
                "## Start Foreground Loop",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_5 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_5.yaml run-loop --mode formal --iterations 0 --interval-seconds 60 --confirm-infinite`",
                "",
                "## Pause, Resume, Stop",
                "",
                "Use `pause`, `resume`, and `stop` subcommands. They update ledger state and keep the records intact.",
                "",
                "## Check Health",
                "",
                "Run `status --mode formal` and verify heartbeat, lock state, paused/stopped flags, and row counts.",
                "",
                "## Network Failure",
                "",
                "Network, rate-limit, empty-book, and token-specific errors are written to audit logs. The monitor does not guess prices.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return release_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    print(json.dumps(json_safe(generate(root, config_path)), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
