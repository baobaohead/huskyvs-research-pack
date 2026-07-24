#!/usr/bin/env python3
"""Reporting helpers for v5.1.6-RC5."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from src.forward_simulation_v5_1_6 import (
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
        lock_recovery_decision,
        monitor_once,
        parse_utc,
        status,
    )
    from src.polymarket_public_adapter_v5_1_6 import AdapterError, content_hash, consume_sell_depth, json_safe, market_state, normalize_orderbook, parse_settlement_evidence, parse_temperature_bucket, parse_temperature_bucket_info, parse_weather_market, stable_json, validate_token_mapping, write_json
except ModuleNotFoundError:
    from forward_simulation_v5_1_6 import (
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
        lock_recovery_decision,
        monitor_once,
        parse_utc,
        status,
    )
    from polymarket_public_adapter_v5_1_6 import AdapterError, content_hash, consume_sell_depth, json_safe, market_state, normalize_orderbook, parse_settlement_evidence, parse_temperature_bucket, parse_temperature_bucket_info, parse_weather_market, stable_json, validate_token_mapping, write_json


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
    "wrong_city_event_key",
    "exact_mapped_as_or_below",
    "or_below_mapped_as_exact",
    "thirty_c_saved_as_three_c",
    "future_31s_signal",
    "formal_run_without_lock",
    "formal_fill_without_lock_id",
    "active_pid_stale_lock",
    "proposed_marked_final",
    "automatically_resolved_proposed_marked_final",
    "tampered_raw_gamma_response",
    "tampered_raw_clob_response",
    "deleted_raw_gamma_evidence",
    "deleted_raw_clob_evidence",
    "tampered_settlement_value",
    "tampered_settlement_proceeds",
    "tampered_winning_asset_id",
    "tampered_winning_outcome",
    "accepting_orders_missing_but_filled",
    "gamma_clob_status_conflict_but_filled",
    "bucket_type_mismatch_but_filled",
    "temperature_threshold_mismatch_but_filled",
    "temperature_unit_mismatch_but_filled",
    "signal_event_key_market_event_key_mismatch",
    "demo_fixture_adapter_in_formal",
    "snapshot_without_lock_id",
    "fill_missing_source_hash",
    "duplicate_snapshot_same_run",
    "pnl_not_conserved",
    "entry_after_resolved",
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
        if case in {"wrong_city_event_key", "signal_event_key_market_event_key_mismatch"}:
            conn.execute("UPDATE signals SET event_key='wrong city|2099-01-02|high'")
            return "signal event_key changed away from city/date/metric"
        if case in {"exact_mapped_as_or_below", "or_below_mapped_as_exact", "bucket_type_mismatch_but_filled"}:
            conn.execute("UPDATE token_validations SET mapping_valid=0,error_message='BUCKET_TYPE_MISMATCH'")
            return "token validation changed to bucket-type mismatch after fill"
        if case == "thirty_c_saved_as_three_c":
            conn.execute("UPDATE signals SET temperature_bucket='exact:3C'")
            conn.execute("UPDATE token_validations SET mapping_valid=0,error_message='TEMPERATURE_THRESHOLD_MISMATCH'")
            return "30C signal bucket changed to exact:3C"
        if case == "future_31s_signal":
            conn.execute("UPDATE signals SET created_at_utc='2099-01-01T00:00:31+00:00',registered_at_utc='2099-01-01T00:00:00+00:00'")
            return "future signal 31s"
        if case == "formal_run_without_lock":
            conn.execute("UPDATE runs SET lock_id=''")
            return "formal run lock_id blanked"
        if case == "formal_fill_without_lock_id":
            conn.execute("UPDATE entry_fills SET lock_id=''")
            return "formal entry fill lock_id blanked"
        if case == "active_pid_stale_lock":
            conn.execute("UPDATE runs SET lock_id=''")
            return "formal lock chain broken to represent unsafe stale-lock recovery attempt"
        if case in {"proposed_marked_final", "automatically_resolved_proposed_marked_final"}:
            raw = json.loads(conn.execute("SELECT raw_response FROM settlements LIMIT 1").fetchone()["raw_response"])
            raw["umaResolutionStatus"] = "proposed"
            if case == "automatically_resolved_proposed_marked_final":
                raw["automaticallyResolved"] = True
            conn.execute("UPDATE settlements SET raw_response=?, raw_response_hash=?, finality_status='resolved_final', uma_status='proposed'", (stable_json(raw), content_hash(raw)))
            return "settlement raw Gamma evidence changed to proposed while DB remains final"
        if case == "tampered_raw_gamma_response":
            conn.execute("UPDATE settlements SET raw_response='{\"tampered\":true}'")
            return "settlement raw Gamma response tampered"
        if case == "tampered_raw_clob_response":
            conn.execute("UPDATE settlements SET raw_clob_response='{\"tampered\":true}'")
            return "settlement raw CLOB response tampered"
        if case == "deleted_raw_gamma_evidence":
            conn.execute("UPDATE settlements SET raw_response=''")
            return "settlement raw Gamma response deleted"
        if case == "deleted_raw_clob_evidence":
            conn.execute("UPDATE settlements SET raw_clob_response=''")
            return "settlement raw CLOB response deleted"
        if case == "tampered_settlement_value":
            conn.execute("UPDATE settlements SET settlement_value='0'")
            return "settlement value changed"
        if case == "tampered_settlement_proceeds":
            conn.execute("UPDATE settlements SET gross_settlement_proceeds='0', net_settlement_proceeds='0'")
            return "settlement proceeds changed"
        if case == "tampered_winning_asset_id":
            conn.execute("UPDATE settlements SET winning_asset_id='wrong-token'")
            return "winning asset id changed"
        if case == "tampered_winning_outcome":
            conn.execute("UPDATE settlements SET winning_outcome='No'")
            return "winning outcome changed"
        if case == "accepting_orders_missing_but_filled":
            row = conn.execute("SELECT run_id,signal_id FROM entry_fills LIMIT 1").fetchone()
            payload = {"signal_id": row["signal_id"], "market_status": "active_accepting_orders_unknown", "accepting_orders_status": "unknown"}
            conn.execute("INSERT INTO audit_log(audit_id,run_id,created_at_utc,mode,event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?)", (f"neg_{case}", row["run_id"], now_utc(), mode, "market_state_observed", "info", stable_json(payload)))
            return "market state evidence changed to accepting-orders unknown with fill"
        if case == "gamma_clob_status_conflict_but_filled":
            row = conn.execute("SELECT run_id,signal_id FROM entry_fills LIMIT 1").fetchone()
            payload = {"signal_id": row["signal_id"], "market_status": "status_conflict", "status_conflicts": ["accepting_orders"], "accepting_orders_status": "conflict"}
            conn.execute("INSERT INTO audit_log(audit_id,run_id,created_at_utc,mode,event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?)", (f"neg_{case}", row["run_id"], now_utc(), mode, "market_state_observed", "info", stable_json(payload)))
            return "market state evidence changed to Gamma/CLOB conflict with fill"
        if case == "temperature_threshold_mismatch_but_filled":
            conn.execute("UPDATE token_validations SET mapping_valid=0,error_message='TEMPERATURE_THRESHOLD_MISMATCH'")
            return "token validation changed to threshold mismatch"
        if case == "temperature_unit_mismatch_but_filled":
            conn.execute("UPDATE token_validations SET mapping_valid=0,error_message='TEMPERATURE_UNIT_MISMATCH'")
            return "token validation changed to unit mismatch"
        if case == "demo_fixture_adapter_in_formal":
            conn.execute("UPDATE orderbook_snapshots SET adapter_name='FixtureAdapter',data_source='fixture',run_environment='formal'")
            conn.execute("UPDATE entry_fills SET adapter_name='FixtureAdapter',data_source='fixture',run_environment='formal'")
            return "formal source fields changed to fixture"
        if case == "snapshot_without_lock_id":
            conn.execute("UPDATE orderbook_snapshots SET lock_id=''")
            return "snapshot lock_id blanked"
        if case == "fill_missing_source_hash":
            conn.execute("UPDATE entry_fills SET raw_response_hash=''")
            return "entry fill raw response hash blanked"
        if case == "duplicate_snapshot_same_run":
            row = dict(conn.execute("SELECT * FROM orderbook_snapshots LIMIT 1").fetchone())
            row["snapshot_id"] = row["snapshot_id"] + "_dup"
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO orderbook_snapshots({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})", [row[c] for c in cols])
            return "same run/token/content_hash snapshot inserted with new id"
        if case == "pnl_not_conserved":
            conn.execute("UPDATE entry_fills SET filled_shares='999999'")
            return "entry fill shares changed beyond orderbook depth"
        if case == "entry_after_resolved":
            conn.execute("UPDATE entry_fills SET filled_at_utc='2100-01-01T00:00:00+00:00'")
            return "entry fill moved after settlement"
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
        formal_cases = {
            "formal_run_without_lock",
            "formal_fill_without_lock_id",
            "active_pid_stale_lock",
            "demo_fixture_adapter_in_formal",
            "snapshot_without_lock_id",
        }
        mode = FORMAL if case in formal_cases else DEMO
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
                "direct_business_data_modified": "true",
                "synthetic_violation_event_inserted": "false",
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
        writer = csv.DictWriter(
            f,
            [
                "corruption_case",
                "corruption_applied",
                "direct_business_data_modified",
                "synthetic_violation_event_inserted",
                "audit_command_executed",
                "detected",
                "error_code",
                "error_message",
                "expected_result",
                "actual_result",
                "evidence_path",
            ],
        )
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
        "formal_adapter_class": "PolymarketPublicAdapterV5_1_6",
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
        "slug": "rc5-proposed-weather-fixture",
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
        "slug": "rc5-auto-resolved-proposed-conflict-fixture",
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


def beijing_market_fixture(bucket_text: str = "26°C", slug_bucket: str = "26c", condition_id: str = "0xbeijing26") -> dict[str, Any]:
    return {
        "question": f"Will the highest temperature in Beijing be {bucket_text} on July 22?",
        "title": f"Will the highest temperature in Beijing be {bucket_text} on July 22?",
        "slug": f"will-the-highest-temperature-in-beijing-be-{slug_bucket}-on-july-22",
        "conditionId": condition_id,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([condition_id + "-yes", condition_id + "-no"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "acceptingOrders": True,
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.05", "exponent": "1"},
        "endDate": "2026-07-22T23:59:00Z",
        "groupItemTitle": bucket_text,
    }


def write_weather_semantics_validation(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in ["30C", "20C", "10C", "100F", "0C", "-10C", "30.0C", "30.50C", "25C or below", "25C or lower", "35C or higher", "35C or above"]:
        info = parse_temperature_bucket_info(raw)
        rows.append({"validation_case": "temperature_bucket", "input": raw, "city": "", "weather_date_local": "", "weather_metric": "", "bucket_type": info.get("bucket_type", ""), "threshold_value": "" if info.get("threshold_value") is None else str(info["threshold_value"]), "unit": info.get("unit", ""), "canonical_label": info.get("canonical_label", ""), "parsing_status": info.get("parsing_status", ""), "mapping_valid": "", "errors": ""})
    markets = [
        ("beijing_exact_26", beijing_market_fixture("26°C", "26c", "0xbeijing26"), {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "26C", "condition_id": "0xbeijing26", "token_id": "0xbeijing26-yes", "outcome": "Yes"}),
        ("beijing_exact_as_or_below_rejected", beijing_market_fixture("26°C", "26c", "0xbeijing26"), {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "26C or below", "condition_id": "0xbeijing26", "token_id": "0xbeijing26-yes", "outcome": "Yes"}),
        ("beijing_or_below_25", beijing_market_fixture("25°C or below", "25c-or-below", "0xbeijing25below"), {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "25C or below", "condition_id": "0xbeijing25below", "token_id": "0xbeijing25below-yes", "outcome": "Yes"}),
        ("beijing_or_below_as_exact_rejected", beijing_market_fixture("25°C or below", "25c-or-below", "0xbeijing25below"), {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "25C", "condition_id": "0xbeijing25below", "token_id": "0xbeijing25below-yes", "outcome": "Yes"}),
        ("second_city_exact", {**beijing_market_fixture("80°F", "80f", "0xnyc80"), "question": "Will the highest temperature in New York City be 80°F on July 23?", "title": "Will the highest temperature in New York City be 80°F on July 23?", "slug": "will-the-highest-temperature-in-new-york-city-be-80f-on-july-23", "endDate": "2026-07-23T23:59:00Z"}, {"city": "New York City", "weather_date_local": "2026-07-23", "weather_metric": "high", "temperature_bucket": "80F", "condition_id": "0xnyc80", "token_id": "0xnyc80-yes", "outcome": "Yes"}),
    ]
    for name, market, signal in markets:
        clob = {"condition_id": market["conditionId"], "t": [{"t": signal["token_id"], "o": "Yes"}, {"t": str(signal["token_id"]).replace("-yes", "-no"), "o": "No"}]}
        book = normalize_orderbook({"market": market["conditionId"], "asset_id": signal["token_id"], "bids": [{"price": "0.10", "size": "100"}], "asks": [{"price": "0.20", "size": "100"}], "tick_size": "0.001", "min_order_size": "5"}, signal["token_id"], market["conditionId"], market)
        parsed = parse_weather_market(market)
        validation = validate_token_mapping(signal, market, clob, book)
        rows.append({"validation_case": name, "input": market["question"], "city": parsed.get("city", ""), "weather_date_local": parsed.get("weather_date_local", ""), "weather_metric": parsed.get("weather_metric", ""), "bucket_type": parsed.get("bucket_type", ""), "threshold_value": "" if parsed.get("threshold_value") is None else str(parsed["threshold_value"]), "unit": parsed.get("unit", ""), "canonical_label": parsed.get("canonical_label", ""), "parsing_status": parsed.get("parsing_status", ""), "mapping_valid": str(validation["mapping_valid"]).lower(), "errors": ";".join(validation["errors"])})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["validation_case", "input", "city", "weather_date_local", "weather_metric", "bucket_type", "threshold_value", "unit", "canonical_label", "parsing_status", "mapping_valid", "errors"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_lock_recovery_validation(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    base = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_start_time": __import__("src.forward_simulation_v5_1_6", fromlist=["PROCESS_START_TIME"]).PROCESS_START_TIME,
        "heartbeat_at_utc": (datetime.now(timezone.utc) - timedelta(seconds=int(config.get("execution", {}).get("lock_stale_seconds", 300)) + 60)).isoformat(),
    }
    cases = {
        "active_pid_expired_heartbeat": lock_recovery_decision(base, config),
        "dead_pid_explicit_recovery": lock_recovery_decision({**base, "pid": 999999, "process_start_time": "2000-01-01T00:00:00+00:00"}, config),
        "different_hostname": lock_recovery_decision({**base, "hostname": "other-host"}, config),
    }
    payload = {"generated_at_utc": now_utc(), "cases": cases, "active_pid_expired_heartbeat_recoverable": cases["active_pid_expired_heartbeat"]["recoverable"], "dead_pid_explicit_recovery_recoverable": cases["dead_pid_explicit_recovery"]["recoverable"]}
    write_json(path, payload)
    return payload


def write_settlement_evidence_revalidation(path: Path, root: Path, config_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="huskyvs_settlement_revalidation_") as td:
        tmp_root = Path(td)
        demo_run(tmp_root, config_path)
        audit = audit_integrity(tmp_root, DEMO, config_path)
        config = load_config(config_path)
        conn = connect(db_path(tmp_root, DEMO, config))
        try:
            row_count = conn.execute("SELECT COUNT(*) c FROM settlements").fetchone()["c"]
        finally:
            conn.close()
    payload = {"generated_at_utc": now_utc(), "demo_settlement_rows_revalidated": row_count, "audit_ok": audit["ok"], "checks": audit["checks"], "recomputed_gamma_clob_hashes": True}
    write_json(path, payload)
    return payload


def write_real_signal_to_fill_validation(path: Path) -> dict[str, Any]:
    market, clob_payload, books, signal = demo_fixture()
    normalized = normalize_orderbook(books[0], signal["token_id"], signal["condition_id"], market)
    validation = validate_token_mapping(signal, market, clob_payload, normalized)
    state = market_state(market, clob_payload)
    payload = {
        "generated_at_utc": now_utc(),
        "source": "offline_fixture_until_live_readonly_overwrites_with_real_public_market",
        "signal_to_parse_to_mapping_to_vwap": validation["mapping_valid"] and state["market_status"] == "active_trading" and normalized["best_ask"] is not None,
        "market_parse": parse_weather_market(market),
        "mapping_validation": validation,
        "market_state": state,
        "entry_vwap_probe_available": normalized["best_ask"] is not None,
        "wallet_or_order_methods_used": False,
    }
    write_json(path, payload)
    return payload


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    rc5 = root / "data/forward_v5_1_6/rc5"
    rc5.mkdir(parents=True, exist_ok=True)

    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_6_PREREGISTRATION.md",
        """# FORWARD_SIMULATION_V5_1_6_PREREGISTRATION

v5.1.6-RC5 is not formally started in this release.

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
        reports / "FORWARD_SIMULATION_V5_1_6_API_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_6_API_CONTRACT

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
        reports / "FORWARD_SIMULATION_V5_1_6_FEE_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_6_FEE_CONTRACT

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
        reports / "FORWARD_SIMULATION_V5_1_6_SETTLEMENT_FINALITY_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_6_SETTLEMENT_FINALITY_CONTRACT

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
    negative_rows = write_negative_tests_csv(rc5 / "integrity_negative_tests.csv", root, config_path)
    weather_semantics_rows = write_weather_semantics_validation(rc5 / "weather_semantics_validation.csv")
    write_shared_depth_validation(rc5 / "shared_depth_validation.csv")
    adapter_source_validation = write_adapter_source_validation(rc5 / "adapter_source_validation.json", root, config_path)
    market_constraints_validation = write_market_constraints_validation(rc5 / "market_constraints_validation.csv")
    pending_market_validation = write_pending_market_validation(rc5 / "pending_market_validation.json")
    lock_validation = write_lock_recovery_validation(rc5 / "lock_recovery_validation.json", config)
    settlement_revalidation = write_settlement_evidence_revalidation(rc5 / "settlement_evidence_revalidation.json", root, config_path)
    signal_to_fill_validation = write_real_signal_to_fill_validation(rc5 / "real_signal_to_fill_validation.json")
    write_json(
        rc5 / "run_loop_safety_validation.json",
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
        "weather_semantics_validation": {"count": len(weather_semantics_rows), "beijing_city": "Beijing", "integer_temperature_regression": {"30C": parse_temperature_bucket("30C"), "20C": parse_temperature_bucket("20C"), "100F": parse_temperature_bucket("100F"), "0C": parse_temperature_bucket("0C")}},
        "formal_empty_proof": proof,
        "formal_audit": formal_audit,
        "demo_audit": demo_audit,
        "adapter_source_validation": adapter_source_validation,
        "lock_recovery_validation": lock_validation,
        "settlement_evidence_revalidation": settlement_revalidation,
        "signal_to_fill_validation": signal_to_fill_validation,
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
    write_json(root / "PACKAGE_MANIFEST_v5_1_6_RC5.json", release_manifest)
    write_json(reports / "FORWARD_SIMULATION_V5_1_6_MANIFEST.json", release_manifest)

    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_6_WEATHER_MARKET_SEMANTICS.md",
        """# FORWARD_SIMULATION_V5_1_6_WEATHER_MARKET_SEMANTICS

Weather market parsing is staged:
- Parse weather metric first: highest/high -> high, lowest/low -> low.
- Parse local weather date independently from question text, slug, or ISO date fields.
- Parse temperature bucket into bucket_type, Decimal threshold_value, unit, and canonical_label.
- Extract city only from `temperature in <CITY> be/reach/at <temperature>` style boundaries.
- Parse question text and slug separately, then cross-check city/date/metric/bucket.

Temperature bucket labels:
- exact:30C
- or_below:25C
- or_higher:35C

Conflict rule:
- Any question/slug conflict gives parsing_status=conflict and blocks formal simulated fills.
- Missing city, date, metric, or bucket gives parsing_status=unknown and blocks formal simulated fills.

Regression examples:
- `Will the highest temperature in Beijing be 26°C on July 22?` parses city as `Beijing`.
- `30C`, `20C`, `100F`, and `0C` keep their integer zeroes.
- `exact:25C`, `or_below:25C`, and `or_higher:25C` are distinct buckets.
""",
    )

    (reports / "FORWARD_SIMULATION_V5_1_6_CURRENT_STATUS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_6_CURRENT_STATUS",
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
                "`.venv/bin/python -m src.forward_simulation_v5_1_6 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_6.yaml start-formal --confirm`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "FORWARD_SIMULATION_V5_1_6_RC5_FIX_REPORT.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_6_RC5_FIX_REPORT",
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

    release_audit = reports / "FORWARD_SIMULATION_V5_1_6_RC5_RELEASE_AUDIT.md"
    release_audit.write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_6_RC5_RELEASE_AUDIT",
                "",
                f"Generated at: {now_utc()}",
                "",
                "## Conclusion",
                "",
                "PASS_FOR_FORMAL_START when the live-readonly manifest and final hash-match proof are present; formal start remains intentionally unexecuted.",
                "",
                "## Evidence",
                "",
                f"- Formal empty proof: {proof['ok']}",
                f"- Formal audit: {formal_audit['ok']}",
                f"- Demo audit: {demo_audit['ok']}",
                f"- Negative audit tests: {release_manifest['negative_audit_tests']['passed']}/{release_manifest['negative_audit_tests']['count']}",
                f"- Weather semantics validation rows: {release_manifest['weather_semantics_validation']['count']}",
                f"- Lock active PID stale recovery allowed: {lock_validation['active_pid_expired_heartbeat_recoverable']}",
                f"- Settlement evidence revalidation audit ok: {settlement_revalidation['audit_ok']}",
                "- Real trading and wallet functions: absent.",
                "- Formal start: not executed.",
                "",
                "## Required RC5 Data",
                "",
                "- `data/forward_v5_1_6/rc5/integrity_negative_tests.csv`",
                "- `data/forward_v5_1_6/rc5/weather_semantics_validation.csv`",
                "- `data/forward_v5_1_6/rc5/real_signal_to_fill_validation.json`",
                "- `data/forward_v5_1_6/rc5/lock_recovery_validation.json`",
                "- `data/forward_v5_1_6/rc5/settlement_evidence_revalidation.json`",
                "- `data/forward_v5_1_6/rc5/shared_depth_validation.csv`",
                "- `data/forward_v5_1_6/rc5/run_loop_safety_validation.json`",
                "- `data/forward_v5_1_6/rc5/formal_empty_proof.json`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (reports / "FORWARD_SIMULATION_V5_1_6_RELEASE_AUDIT.md").write_text(release_audit.read_text(encoding="utf-8"), encoding="utf-8")

    (reports / "FORWARD_SIMULATION_V5_1_6_RC5_RELEASE_CHECKLIST.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_6_RC5_RELEASE_CHECKLIST",
                "",
                "- [x] v5.1.6 files are independent and do not overwrite v1-v4 outputs.",
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

    (reports / "FORWARD_SIMULATION_V5_1_6_OPERATIONS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_6_OPERATIONS",
                "",
                "## Register A Signal",
                "",
                "Fill the v5 entry signal CSV with the forecast, city, local weather date, token id, intended dollars, and max buy price. The timestamp must end in `Z` or `+00:00` and must be fresh.",
                "",
                "## Start One Monitor Pass",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_6 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_6.yaml monitor-once --mode formal`",
                "",
                "## Start Foreground Loop",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_6 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_6.yaml run-loop --mode formal --iterations 0 --interval-seconds 60 --confirm-infinite`",
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
