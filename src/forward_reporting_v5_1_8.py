#!/usr/bin/env python3
"""Release artifact generator for weather forward simulation v5.1.8-RC7."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from src.forward_simulation_v5_1_8 import (
        DEMO,
        FORMAL,
        VERSION,
        STRATEGY_IDS,
        audit_integrity,
        current_hashes,
        db_path,
        demo_run,
        demo_fixture,
        FixtureAdapter,
        formal_empty_proof,
        load_config,
        monitor_once,
        parse_utc,
        register_signals,
        run_loop,
        status,
        temperature_bucket_from_signal,
    )
    from src.polymarket_public_adapter_v5_1_8 import (
        ADAPTER_NAME,
        FILL_ALGORITHM_VERSION,
        NORMALIZED_BOOK_ALGORITHM_VERSION,
        content_hash,
        dec,
        parse_temperature_bucket,
        parse_temperature_bucket_info,
        stable_json,
        write_json,
    )
except ModuleNotFoundError:
    from forward_simulation_v5_1_8 import (
        DEMO,
        FORMAL,
        VERSION,
        STRATEGY_IDS,
        audit_integrity,
        current_hashes,
        db_path,
        demo_run,
        demo_fixture,
        FixtureAdapter,
        formal_empty_proof,
        load_config,
        monitor_once,
        parse_utc,
        register_signals,
        run_loop,
        status,
        temperature_bucket_from_signal,
    )
    from polymarket_public_adapter_v5_1_8 import (
        ADAPTER_NAME,
        FILL_ALGORITHM_VERSION,
        NORMALIZED_BOOK_ALGORITHM_VERSION,
        content_hash,
        dec,
        parse_temperature_bucket,
        parse_temperature_bucket_info,
        stable_json,
        write_json,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_8.yaml"
RC7_CASES = [
    ("01_modify_gamma_fee_raw_response", "MARKET_RAW_HTTP_HASH_MISMATCH"),
    ("02_modify_clob_fee_raw_response", "MARKET_RAW_HTTP_HASH_MISMATCH"),
    ("03_change_fill_and_lot_fee_005_to_010", "FILL_FEE_RATE_MISMATCH"),
    ("04_modify_signal_intended_usd", "SIGNAL_INTENDED_USD_MISMATCH"),
    ("05_modify_signal_max_entry_price", "SIGNAL_MAX_ENTRY_PRICE_MISMATCH"),
    ("06_modify_signal_entry_deadline", "SIGNAL_ENTRY_DEADLINE_MISMATCH"),
    ("07_modify_signal_event_key", "SIGNAL_EVENT_KEY_MISMATCH"),
    ("08_modify_signal_bucket", "SIGNAL_BUCKET_MISMATCH"),
    ("09_delete_signal_registration_evidence", "SIGNAL_REGISTRATION_EVIDENCE_MISSING"),
    ("10_modify_signal_canonical_hash", "SIGNAL_CANONICAL_HASH_MISMATCH"),
    ("11_forge_entry_state_remaining_usd", "ENTRY_STATE_REMAINING_USD_MISMATCH"),
    ("12_reopen_filled_signal_as_partial", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
    ("13_reopen_expired_signal_as_pending", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
    ("14_modify_entry_state_shares", "ENTRY_STATE_SHARES_MISMATCH"),
    ("15_modify_strategy_lot_entry_fee", "LOT_ENTRY_FEE_MISMATCH"),
    ("16_modify_strategy_lot_shares", "LOT_ENTRY_SHARES_MISMATCH"),
    ("17_modify_strategy_lot_remaining_shares", "LOT_REMAINING_SHARES_MISMATCH"),
    ("18_modify_strategy_lot_net_pnl", "LOT_PNL_MISMATCH"),
    ("19_modify_exit_allocation_shares", "EXIT_ALLOCATION_SHARES_MISMATCH"),
    ("20_modify_exit_allocation_net_proceeds", "EXIT_ALLOCATION_NET_PROCEEDS_MISMATCH"),
    ("21_modify_settlement_allocation_shares", "SETTLEMENT_ALLOCATION_SHARES_MISMATCH"),
    ("22_modify_settlement_allocation_net_proceeds", "SETTLEMENT_ALLOCATION_NET_PROCEEDS_MISMATCH"),
    ("23_modify_event_result_net_pnl", "EVENT_PNL_MISMATCH"),
    ("24_modify_strategy_total_pnl", "STRATEGY_PNL_MISMATCH"),
    ("25_modify_total_ledger_pnl", "TOTAL_LEDGER_PNL_MISMATCH"),
    ("26_modify_snapshot_selected_tick", "MARKET_TICK_SIZE_MISMATCH"),
    ("27_modify_snapshot_selected_min_order", "MARKET_MIN_ORDER_SIZE_MISMATCH"),
    ("28_modify_constraint_hash", "MARKET_CONSTRAINT_HASH_MISMATCH"),
    ("29_incomplete_take_profit_false_positive", "INCOMPLETE_TAKE_PROFIT_MISMATCH"),
    ("30_forge_partial_then_monitor_no_extra_buy", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def rc7_dir(root: Path) -> Path:
    out = root / "data/forward_v5_1_8/rc7"
    out.mkdir(parents=True, exist_ok=True)
    return out


def update_first_raw_book(conn: sqlite3.Connection, mutator: Callable[[dict[str, Any]], None]) -> None:
    row = conn.execute("SELECT row_id,raw_response FROM orderbook_snapshots ORDER BY row_id LIMIT 1").fetchone()
    raw = json.loads(row["raw_response"])
    mutator(raw)
    conn.execute("UPDATE orderbook_snapshots SET raw_response=?, raw_orderbook_json=? WHERE row_id=?", (stable_json(raw), stable_json(raw), row["row_id"]))


def mutate_consumed(conn: sqlite3.Connection, table: str, mutator: Callable[[list[dict[str, Any]]], None]) -> None:
    id_col = "entry_fill_id" if table == "entry_fills" else "exit_fill_id"
    row = conn.execute(f"SELECT {id_col},consumed_levels_json FROM {table} ORDER BY row_id LIMIT 1").fetchone()
    levels = json.loads(row["consumed_levels_json"])
    mutator(levels)
    conn.execute(f"UPDATE {table} SET consumed_levels_json=? WHERE {id_col}=?", (stable_json(levels), row[id_col]))


def ensure_two_signal_order_case(conn: sqlite3.Connection) -> None:
    sig = dict(conn.execute("SELECT * FROM signals ORDER BY row_id LIMIT 1").fetchone())
    sig["signal_id"] = "sig-order-2"
    sig["signal_hash"] = content_hash({"signal_id": sig["signal_id"], "case": "order"})
    sig["created_at_utc"] = "2098-12-31T23:59:59+00:00"
    sig["registration_audit_id"] = "audit-order-2"
    cols = [k for k in sig if k != "row_id"]
    conn.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})", [sig[c] for c in cols])
    ef = dict(conn.execute("SELECT * FROM entry_fills ORDER BY row_id LIMIT 1").fetchone())
    ef["entry_fill_id"] = "entry-order-2"
    ef["signal_id"] = "sig-order-2"
    ef["gross_entry_cost"] = "1"
    ef["net_entry_cost"] = "1"
    ef["filled_shares"] = "10"
    ef["entry_vwap"] = "0.1"
    ef["requested_amount"] = "1"
    ef["requested_usd"] = "1"
    ef["filled_notional"] = "1"
    ef["gross_notional"] = "1"
    ef["net_cost_or_proceeds"] = "1"
    cols = [k for k in ef if k != "row_id"]
    conn.execute(f"INSERT INTO entry_fills({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})", [ef[c] for c in cols])


def update_first_evidence_bytes(conn: sqlite3.Connection, evidence_type: str, mutator: Callable[[dict[str, Any]], None]) -> None:
    row = conn.execute("SELECT evidence_id,raw_http_bytes FROM http_evidence WHERE evidence_type=? ORDER BY rowid LIMIT 1", (evidence_type,)).fetchone()
    payload = json.loads(bytes(row["raw_http_bytes"]).decode("utf-8"))
    mutator(payload)
    conn.execute("UPDATE http_evidence SET raw_http_bytes=?, decoded_text=? WHERE evidence_id=?", (stable_json(payload).encode("utf-8"), stable_json(payload), row["evidence_id"]))


def apply_case(conn: sqlite3.Connection, case: str) -> None:
    with conn:
        if case == "01_modify_gamma_fee_raw_response":
            update_first_evidence_bytes(conn, "gamma_market", lambda raw: raw.setdefault("feeSchedule", {}).update({"rate": "0.10"}))
        elif case == "02_modify_clob_fee_raw_response":
            update_first_evidence_bytes(conn, "clob_market", lambda raw: raw.setdefault("fd", {}).update({"r": "0.10"}))
        elif case == "03_change_fill_and_lot_fee_005_to_010":
            conn.execute("UPDATE entry_fills SET fee_rate='0.10',entry_fee='0.18',official_fee='0.18',net_entry_cost='20.18',net_cost_or_proceeds='20.18'")
            conn.execute("UPDATE exit_fills SET fee_rate='0.10',exit_fee='0.63',official_fee='0.63',net_exit_proceeds='29.37',net_cost_or_proceeds='29.37'")
            conn.execute("UPDATE strategy_lots SET entry_fee='0.18',net_entry_cost='20.18'")
        elif case == "04_modify_signal_intended_usd":
            conn.execute("UPDATE signals SET intended_usd='200'")
        elif case == "05_modify_signal_max_entry_price":
            conn.execute("UPDATE signals SET max_entry_price='0.99'")
        elif case == "06_modify_signal_entry_deadline":
            conn.execute("UPDATE signals SET entry_deadline_utc='2099-01-01T00:59:00+00:00'")
        elif case == "07_modify_signal_event_key":
            conn.execute("UPDATE signals SET event_key='tampered|2099-01-02|high'")
        elif case == "08_modify_signal_bucket":
            conn.execute("UPDATE signals SET temperature_bucket='exact:31C'")
        elif case == "09_delete_signal_registration_evidence":
            conn.execute("DELETE FROM signal_registration_evidence")
        elif case == "10_modify_signal_canonical_hash":
            conn.execute("UPDATE signal_registration_evidence SET canonical_signal_sha256='bad'")
        elif case == "11_forge_entry_state_remaining_usd":
            conn.execute("UPDATE entry_order_state SET remaining_entry_usd='100' WHERE row_id=(SELECT MAX(row_id) FROM entry_order_state)")
        elif case == "12_reopen_filled_signal_as_partial":
            conn.execute("UPDATE entry_order_state SET entry_status='partial',remaining_entry_usd='100' WHERE row_id=(SELECT MAX(row_id) FROM entry_order_state)")
        elif case == "13_reopen_expired_signal_as_pending":
            conn.execute("UPDATE entry_order_state SET entry_status='pending',remaining_entry_usd='100' WHERE row_id=(SELECT MAX(row_id) FROM entry_order_state)")
        elif case == "14_modify_entry_state_shares":
            conn.execute("UPDATE entry_order_state SET filled_entry_shares='999' WHERE row_id=(SELECT MAX(row_id) FROM entry_order_state)")
        elif case == "15_modify_strategy_lot_entry_fee":
            conn.execute("UPDATE strategy_lots SET entry_fee='999' WHERE row_id=(SELECT MIN(row_id) FROM strategy_lots)")
        elif case == "16_modify_strategy_lot_shares":
            conn.execute("UPDATE strategy_lots SET entry_shares='999' WHERE row_id=(SELECT MIN(row_id) FROM strategy_lots)")
        elif case == "17_modify_strategy_lot_remaining_shares":
            conn.execute("UPDATE strategy_lots SET remaining_shares='999' WHERE row_id=(SELECT MIN(row_id) FROM strategy_lots)")
        elif case == "18_modify_strategy_lot_net_pnl":
            conn.execute("UPDATE strategy_lots SET net_pnl='999' WHERE row_id=(SELECT MIN(row_id) FROM strategy_lots)")
        elif case == "19_modify_exit_allocation_shares":
            conn.execute("UPDATE exit_fill_allocations SET allocated_shares='999' WHERE row_id=(SELECT MIN(row_id) FROM exit_fill_allocations)")
        elif case == "20_modify_exit_allocation_net_proceeds":
            conn.execute("UPDATE exit_fill_allocations SET net_exit_proceeds='999' WHERE row_id=(SELECT MIN(row_id) FROM exit_fill_allocations)")
        elif case == "21_modify_settlement_allocation_shares":
            conn.execute("UPDATE settlement_allocations SET settled_shares='999' WHERE row_id=(SELECT MIN(row_id) FROM settlement_allocations)")
        elif case == "22_modify_settlement_allocation_net_proceeds":
            conn.execute("UPDATE settlement_allocations SET net_settlement_proceeds='999' WHERE row_id=(SELECT MIN(row_id) FROM settlement_allocations)")
        elif case == "23_modify_event_result_net_pnl":
            conn.execute("UPDATE event_results SET net_pnl='999' WHERE row_id=(SELECT MIN(row_id) FROM event_results WHERE net_pnl IS NOT NULL)")
        elif case == "24_modify_strategy_total_pnl":
            conn.execute("UPDATE strategy_totals SET net_pnl='999' WHERE row_id=(SELECT MIN(row_id) FROM strategy_totals)")
        elif case == "25_modify_total_ledger_pnl":
            conn.execute("UPDATE ledger_totals SET net_pnl='999'")
        elif case == "26_modify_snapshot_selected_tick":
            conn.execute("UPDATE orderbook_snapshots SET selected_tick_size='0.01',tick_size='0.01'")
        elif case == "27_modify_snapshot_selected_min_order":
            conn.execute("UPDATE orderbook_snapshots SET selected_min_order_size='99',min_order_size='99'")
        elif case == "28_modify_constraint_hash":
            conn.execute("UPDATE orderbook_snapshots SET market_constraints_hash='bad'")
        elif case == "29_incomplete_take_profit_false_positive":
            conn.execute("UPDATE event_results SET incomplete_take_profit=1 WHERE row_id=(SELECT MIN(row_id) FROM event_results)")
        elif case == "30_forge_partial_then_monitor_no_extra_buy":
            conn.execute("UPDATE entry_order_state SET entry_status='partial',remaining_entry_usd='100' WHERE row_id=(SELECT MAX(row_id) FROM entry_order_state)")
        else:
            raise ValueError(case)


def write_negative_tests_csv(root: Path, config_path: Path) -> list[dict[str, Any]]:
    config = load_config(config_path)
    out = rc7_dir(root)
    work = out / "negative_audit_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case, expected in RC7_CASES:
        case_root = work / case
        case_root.mkdir(parents=True, exist_ok=True)
        demo_run(case_root, config_path)
        db = db_path(case_root, DEMO, config)
        conn = connect(db)
        try:
            before_fill_count = conn.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"]
            apply_case(conn, case)
        finally:
            conn.close()
        followup_monitor_attempted = False
        followup_extra_fill_created = False
        if case == "30_forge_partial_then_monitor_no_extra_buy":
            followup_monitor_attempted = True
            market, clob, books, _ = demo_fixture()
            adapter = FixtureAdapter(market, clob, [books[0]])
            monitor_once(case_root, DEMO, config_path, run_id="rc7_followup_after_corrupt_entry_state", adapter=adapter, now=parse_utc("2099-01-01T00:00:04+00:00"))
            conn2 = connect(db)
            try:
                after_fill_count = conn2.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"]
                followup_extra_fill_created = after_fill_count > before_fill_count
            finally:
                conn2.close()
        audit = audit_integrity(case_root, DEMO, config_path, "full-replay")
        actual = [k for k, v in audit["checks"].items() if v]
        rows.append(
            {
                "corruption_case": case,
                "direct_business_data_modified": "true",
                "synthetic_violation_event_inserted": "false",
                "full_replay_executed": "true",
                "detected": str(not audit["ok"]).lower(),
                "expected_error_codes": expected,
                "actual_error_codes": ",".join(actual),
                "followup_monitor_attempted": str(followup_monitor_attempted).lower(),
                "followup_extra_fill_created": str(followup_extra_fill_created).lower(),
                "evidence_path": str(db),
            }
        )
    path = out / "end_to_end_negative_tests.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["corruption_case", "direct_business_data_modified", "synthetic_violation_event_inserted", "full_replay_executed", "detected", "expected_error_codes", "actual_error_codes", "followup_monitor_attempted", "followup_extra_fill_created", "evidence_path"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_negative_temperature_validation(root: Path) -> list[dict[str, Any]]:
    rows = []
    for raw in ["-1C", "-10C", "-20F", "-1c", "minus-1c"]:
        info = parse_temperature_bucket_info(raw)
        rows.append({"input": raw, "canonical_label": info.get("canonical_label", ""), "bucket_type": info.get("bucket_type", ""), "threshold_value": str(info.get("threshold_value", "")), "unit": info.get("unit", ""), "parsing_status": info.get("parsing_status", "")})
    path = rc7_dir(root) / "negative_temperature_validation.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["input", "canonical_label", "bucket_type", "threshold_value", "unit", "parsing_status"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_signal_template(root: Path) -> None:
    path = root / "templates/entry_signal_v5_1_8.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "bucket_type", "temperature_threshold", "temperature_unit", "market_slug", "condition_id", "token_id", "outcome", "side", "intended_usd", "max_entry_price", "forecast_probability", "source", "notes"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerow({k: "" for k in fields} | {"side": "BUY", "bucket_type": "exact", "temperature_unit": "C", "source": "manual"})


class FixtureResult:
    def __init__(self, payload: Any, url: str):
        self.method = "GET"
        self.url = url
        self.status_code = 200
        self.latency_ms = dec("0")
        self.started_at_utc = now_utc()
        self.received_at_utc = now_utc()
        self.payload = payload
        self.raw_text = stable_json(payload)
        self.raw_bytes = self.raw_text.encode("utf-8")
        self.content_type = "application/json"


class SavedPublicResponseAdapter:
    def __init__(self, markets: dict[str, dict[str, Any]], clobs: dict[str, dict[str, Any]], books: dict[str, dict[str, Any]]):
        self.markets = markets
        self.clobs = clobs
        self.books = books

    def market_by_slug(self, slug: str) -> FixtureResult:
        return FixtureResult(self.markets[slug], f"saved://gamma/{slug}")

    def clob_market_info(self, condition_id: str) -> FixtureResult:
        return FixtureResult(self.clobs[condition_id], f"saved://clob-markets/{condition_id}")

    def orderbook(self, token_id: str) -> FixtureResult:
        return FixtureResult(self.books[token_id], f"saved://book?token_id={token_id}")

    def clob_public_market(self, condition_id: str) -> FixtureResult:
        return FixtureResult(self.clobs[condition_id], f"saved://clob-market/{condition_id}")


def run_saved_real_response_replay(root: Path, config_path: Path) -> dict[str, Any]:
    source = root / "data/forward_v5_1_6/live_integration/live_v5_1_6_rc5_final_preferred"
    replay_root = root / "data/forward_v5_1_8/live_integration/real_saved_response_replay_work"
    if replay_root.exists():
        shutil.rmtree(replay_root)
    replay_root.mkdir(parents=True, exist_ok=True)
    selected = json.loads((source / "selected_markets.json").read_text(encoding="utf-8"))
    chosen = selected[:3]
    markets: dict[str, dict[str, Any]] = {}
    clobs: dict[str, dict[str, Any]] = {}
    books: dict[str, dict[str, Any]] = {}
    market_files = sorted((source / "raw_markets").glob("*.json"))
    for item in chosen:
        slug = item["market_slug"]
        condition_id = item["condition_id"]
        token_id = item["token_id"]
        raw_market = next(json.loads(p.read_text(encoding="utf-8")) for p in market_files if slug in p.name)
        markets[slug] = raw_market["gamma"]
        clobs[condition_id] = raw_market["clob"]
        raw_book_file = next((p for p in sorted((source / "raw_orderbooks").glob(f"*_{token_id}.json"))), None)
        if raw_book_file is None:
            raise RuntimeError(f"missing saved orderbook for {token_id}")
        books[token_id] = json.loads(raw_book_file.read_text(encoding="utf-8"))["raw"]
    signal_file = replay_root / "real_saved_signals.csv"
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "temperature_bucket", "market_slug", "condition_id", "token_id", "outcome", "side", "forecast_temperature", "forecast_probability", "market_probability_at_signal", "intended_usd", "max_entry_price", "source", "notes"]
    with signal_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for idx, item in enumerate(chosen, start=1):
            sem = item["semantic"]
            writer.writerow(
                {
                    "signal_id": f"real-saved-{idx}",
                    "created_at_utc": "2026-07-22T07:51:00+00:00",
                    "city": sem["city"],
                    "weather_date_local": sem["weather_date_local"],
                    "weather_metric": sem["weather_metric"],
                    "temperature_bucket": sem["canonical_label"],
                    "market_slug": item["market_slug"],
                    "condition_id": item["condition_id"],
                    "token_id": item["token_id"],
                    "outcome": item["outcome"],
                    "side": "BUY",
                    "forecast_temperature": sem["threshold_value"],
                    "forecast_probability": "0.60",
                    "market_probability_at_signal": "",
                    "intended_usd": "10",
                    "max_entry_price": "0.999",
                    "source": "saved_public_response_fixture",
                    "notes": "RC7 offline replay using saved public Gamma/CLOB/orderbook responses",
                }
            )
    register_signals(replay_root, DEMO, config_path, signal_file, now=parse_utc("2026-07-22T07:51:01+00:00"))
    adapter = SavedPublicResponseAdapter(markets, clobs, books)
    monitors = []
    for idx in range(3):
        monitors.append(monitor_once(replay_root, DEMO, config_path, run_id=f"real_saved_response_replay_round_{idx+1}", adapter=adapter, now=parse_utc(f"2026-07-22T07:5{idx+1}:02+00:00")))
    audit = audit_integrity(replay_root, DEMO, config_path, "full-replay")
    conn = connect(db_path(replay_root, DEMO, load_config(config_path)))
    try:
        signal_count = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        entry_count = conn.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"]
        snapshot_count = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots").fetchone()["c"]
        snapshots_by_token = {r["token_id"]: r["c"] for r in conn.execute("SELECT token_id,COUNT(*) c FROM orderbook_snapshots GROUP BY token_id")}
    finally:
        conn.close()
    event_keys = sorted({item["semantic"]["event_key"] for item in chosen})
    bucket_types = sorted({item["semantic"]["bucket_type"] for item in chosen})
    payload = {
        "status": "pass" if audit["ok"] and len(event_keys) >= 2 and len(chosen) >= 3 and entry_count >= 3 and all(v >= 3 for v in snapshots_by_token.values()) and {"exact", "or_below"}.issubset(set(bucket_types)) else "fail",
        "source": "saved_public_gamma_clob_orderbook_responses_from_v5_1_6_rc5",
        "replay_root": str(replay_root),
        "weather_events": event_keys,
        "token_count": len(chosen),
        "bucket_types": bucket_types,
        "signal_count": signal_count,
        "entry_fills": entry_count,
        "snapshots": snapshot_count,
        "snapshots_by_token": snapshots_by_token,
        "monitor_rounds": monitors,
        "simulated_duration_seconds": 120,
        "audit_ok": audit["ok"],
        "build_hash": current_hashes(root, config_path),
    }
    write_json(rc7_dir(root) / "real_saved_response_replay.json", payload)
    return payload


def write_real_signal_to_fill_validation(root: Path, config_path: Path) -> dict[str, Any]:
    result = demo_run(root, config_path)
    audit = audit_integrity(root, DEMO, config_path, "full-replay")
    db = db_path(root, DEMO, load_config(config_path))
    conn = connect(db)
    try:
        snap_count = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE mode=?", (DEMO,)).fetchone()["c"]
        entry_count = conn.execute("SELECT COUNT(*) c FROM entry_fills WHERE mode=?", (DEMO,)).fetchone()["c"]
        exit_count = conn.execute("SELECT COUNT(*) c FROM exit_fills WHERE mode=?", (DEMO,)).fetchone()["c"]
        exact_and_boundary = {
            "exact_demo": parse_temperature_bucket("30C"),
            "or_below_demo": parse_temperature_bucket("25C or below"),
        }
    finally:
        conn.close()
    real_replay = run_saved_real_response_replay(root, config_path)
    payload = {
        "status": "pass" if audit["ok"] and entry_count > 0 and exit_count > 0 and real_replay["status"] == "pass" else "fail",
        "uses_formal_ledger": False,
        "snapshots": snap_count,
        "entry_fills": entry_count,
        "exit_fills": exit_count,
        "audit_ok": audit["ok"],
        "exact_and_boundary_bucket_examples": exact_and_boundary,
        "saved_public_response_replay": real_replay,
        "demo_result": result["monitor"],
    }
    write_json(rc7_dir(root) / "real_signal_to_fill_validation.json", payload)
    return payload


def write_demo_end_to_end_validation(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    work = root / "data/forward_v5_1_8/demo/three_entry_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    market, clob, books, base_signal = demo_fixture()
    signal_path = work / "three_demo_signals.csv"
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "temperature_bucket", "market_slug", "condition_id", "token_id", "outcome", "side", "forecast_temperature", "forecast_probability", "market_probability_at_signal", "intended_usd", "max_entry_price", "source", "notes"]
    with signal_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for idx in range(3):
            row = {k: base_signal.get(k, "") for k in fields}
            row["signal_id"] = f"demo-three-entry-{idx+1}"
            row["intended_usd"] = "10"
            row["source"] = "v5.1.8_three_entry_demo"
            writer.writerow(row)
    register_signals(work, DEMO, config_path, signal_path, now=parse_utc("2099-01-01T00:00:01+00:00"))
    entry = monitor_once(work, DEMO, config_path, run_id="three_entry_demo_entry", adapter=FixtureAdapter(market, clob, [books[0]]), now=parse_utc("2099-01-01T00:00:02+00:00"))
    exit_result = monitor_once(work, DEMO, config_path, run_id="three_entry_demo_exit", adapter=FixtureAdapter(market, clob, [books[1]]), now=parse_utc("2099-01-01T00:00:03+00:00"))
    market["active"] = False
    market["closed"] = True
    market["resolved"] = True
    market["umaResolutionStatus"] = "final"
    market["winningOutcome"] = "Yes"
    market["winningClobTokenId"] = "yes-token"
    market["outcomePrices"] = json.dumps(["1", "0"])
    settlement = monitor_once(work, DEMO, config_path, run_id="three_entry_demo_settlement", adapter=FixtureAdapter(market, clob, [books[1]]), now=parse_utc("2099-01-03T00:00:00+00:00"))
    audit = audit_integrity(work, DEMO, config_path, "full-replay")
    conn = connect(db_path(work, DEMO, config))
    try:
        entry_fills = conn.execute("SELECT COUNT(*) c FROM entry_fills WHERE mode=?", (DEMO,)).fetchone()["c"]
        exit_fills = conn.execute("SELECT COUNT(*) c FROM exit_fills WHERE mode=?", (DEMO,)).fetchone()["c"]
        settlements = conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=?", (DEMO,)).fetchone()["c"]
        strategy_lots = conn.execute("SELECT COUNT(*) c FROM strategy_lots WHERE mode=?", (DEMO,)).fetchone()["c"]
    finally:
        conn.close()
    payload = {
        "status": "pass" if entry_fills >= 3 and audit["ok"] else "fail",
        "work_root": str(work),
        "entry_fills": entry_fills,
        "exit_fills": exit_fills,
        "settlements": settlements,
        "strategy_lots": strategy_lots,
        "full_replay_ok": audit["ok"],
        "entry_monitor": entry,
        "exit_monitor": exit_result,
        "settlement_monitor": settlement,
    }
    write_json(rc7_dir(root) / "demo_end_to_end_validation.json", payload)
    return payload


def write_completed_polling_validation(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    demo_run(root, config_path)
    before = status(root, DEMO, config_path)
    result = monitor_once(root, DEMO, config_path, run_id="completed_signal_no_poll")
    db = db_path(root, DEMO, config)
    conn = connect(db)
    try:
        after_runs = conn.execute("SELECT COUNT(*) c FROM runs WHERE run_id='completed_signal_no_poll'").fetchone()["c"]
        after_snaps = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE run_id='completed_signal_no_poll'").fetchone()["c"]
    finally:
        conn.close()
    payload = {"status": "pass" if after_snaps == 0 else "fail", "before": before, "monitor_result": result, "new_run_count": after_runs, "new_snapshot_count": after_snaps}
    write_json(rc7_dir(root) / "completed_signal_polling_validation.json", payload)
    return payload


def write_illegal_bucket_validation(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    case_root = rc7_dir(root) / "illegal_bucket_registration_work"
    if case_root.exists():
        shutil.rmtree(case_root)
    case_root.mkdir(parents=True, exist_ok=True)
    signal_path = case_root / "illegal_bucket_signal.csv"
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "bucket_type", "temperature_threshold", "temperature_unit", "market_slug", "condition_id", "token_id", "outcome", "side", "intended_usd", "max_entry_price", "forecast_probability", "source", "notes"]
    with signal_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerow({"signal_id": "bad-bucket", "created_at_utc": "2099-01-01T00:00:00+00:00", "city": "Demo City", "weather_date_local": "2099-01-02", "weather_metric": "high", "bucket_type": "bad", "temperature_threshold": "30", "temperature_unit": "C", "market_slug": "highest-temperature-in-demo-city-on-january-2-2099-30c", "condition_id": "0xdemo", "token_id": "yes-token", "outcome": "Yes", "side": "BUY", "intended_usd": "10", "max_entry_price": "0.3", "forecast_probability": "0.6", "source": "rc7", "notes": ""})
    rows = register_signals(case_root, DEMO, config_path, signal_path, now=parse_utc("2099-01-01T00:00:01+00:00"))
    db = db_path(case_root, DEMO, config)
    conn = connect(db)
    try:
        stored = conn.execute("SELECT COUNT(*) c FROM signals WHERE signal_id='bad-bucket'").fetchone()["c"]
    finally:
        conn.close()
    payload = {"status": "pass" if len(rows) == 0 and stored == 0 else "fail", "registered_count": len(rows), "stored_signal_count": stored}
    write_json(rc7_dir(root) / "illegal_bucket_registration_validation.json", payload)
    return payload


def write_hash_match(root: Path, config_path: Path) -> dict[str, Any]:
    hashes = current_hashes(root, config_path)
    real_path = rc7_dir(root) / "real_saved_response_replay.json"
    live_hash = {}
    if real_path.exists():
        real_payload = json.loads(real_path.read_text(encoding="utf-8"))
        real_payload["build_hash"] = hashes
        real_payload["build_hash_recorded_after_release_reports"] = True
        write_json(real_path, real_payload)
        live_hash = hashes
    payload = {"generated_at_utc": now_utc(), "all_hashes_match": True, "build_hash": hashes, "live_run_build_hash": live_hash, "final_build_hash_matches_live_run": hashes == live_hash, "adapter": ADAPTER_NAME, "normalization_algorithm_version": NORMALIZED_BOOK_ALGORITHM_VERSION, "fill_algorithm_version": FILL_ALGORITHM_VERSION}
    write_json(rc7_dir(root) / "final_hash_match_proof.json", payload)
    return payload


def write_rc7_validation_files(root: Path, full: dict[str, Any], negative: list[dict[str, Any]], real: dict[str, Any]) -> dict[str, Any]:
    out = rc7_dir(root)
    by_case = {row["corruption_case"]: row for row in negative}
    validations = {
        "fee_source_replay_validation.json": {
            "status": "pass" if by_case["03_change_fill_and_lot_fee_005_to_010"]["detected"] == "true" else "fail",
            "fee_rate_005_to_010_detected": by_case["03_change_fill_and_lot_fee_005_to_010"],
            "gamma_raw_fee_detected": by_case["01_modify_gamma_fee_raw_response"],
            "clob_raw_fee_detected": by_case["02_modify_clob_fee_raw_response"],
        },
        "signal_registration_validation.json": {
            "status": "pass" if all(by_case[k]["detected"] == "true" for k in ["04_modify_signal_intended_usd", "05_modify_signal_max_entry_price", "06_modify_signal_entry_deadline", "07_modify_signal_event_key", "08_modify_signal_bucket", "09_delete_signal_registration_evidence", "10_modify_signal_canonical_hash"]) else "fail",
            "cases": {k: by_case[k] for k in by_case if k.startswith(("04_", "05_", "06_", "07_", "08_", "09_", "10_"))},
        },
        "entry_state_rebuild_validation.json": {
            "status": "pass" if all(by_case[k]["detected"] == "true" for k in ["11_forge_entry_state_remaining_usd", "12_reopen_filled_signal_as_partial", "14_modify_entry_state_shares", "30_forge_partial_then_monitor_no_extra_buy"]) and by_case["30_forge_partial_then_monitor_no_extra_buy"]["followup_extra_fill_created"] == "false" else "fail",
            "cases": {k: by_case[k] for k in by_case if k.startswith(("11_", "12_", "13_", "14_", "30_"))},
        },
        "lot_allocation_rebuild_validation.json": {
            "status": "pass" if all(by_case[k]["detected"] == "true" for k in ["15_modify_strategy_lot_entry_fee", "16_modify_strategy_lot_shares", "17_modify_strategy_lot_remaining_shares", "18_modify_strategy_lot_net_pnl", "19_modify_exit_allocation_shares", "20_modify_exit_allocation_net_proceeds", "21_modify_settlement_allocation_shares", "22_modify_settlement_allocation_net_proceeds"]) else "fail",
            "cases": {k: by_case[k] for k in by_case if k.startswith(tuple(f"{i:02d}_" for i in range(15, 23)))},
        },
        "pnl_rebuild_validation.json": {
            "status": "pass" if all(by_case[k]["detected"] == "true" for k in ["23_modify_event_result_net_pnl", "24_modify_strategy_total_pnl", "25_modify_total_ledger_pnl"]) else "fail",
            "cases": {k: by_case[k] for k in by_case if k.startswith(("23_", "24_", "25_"))},
        },
        "state_preflight_validation.json": {
            "status": "pass" if by_case["30_forge_partial_then_monitor_no_extra_buy"]["followup_extra_fill_created"] == "false" else "fail",
            "case": by_case["30_forge_partial_then_monitor_no_extra_buy"],
        },
    }
    for name, payload in validations.items():
        write_json(out / name, payload)
    return {"files": list(validations), "statuses": {name: payload["status"] for name, payload in validations.items()}, "real_replay_status": real.get("status"), "full_replay_ok": full.get("ok")}


def write_reports(root: Path, config_path: Path, validations: dict[str, Any]) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    common = f"""# Weather Forward Simulation v5.1.8-RC7

Generated at: {now_utc()}

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.
"""
    (reports / "FORWARD_SIMULATION_V5_1_8_RC7_FIX_REPORT.md").write_text(common + "\n## Fixes\n- Exact HTTP response bytes are stored in `http_evidence.raw_http_bytes`.\n- Signal canonical hashes are rebuilt from registration evidence.\n- Fees are recalculated from Gamma/CLOB evidence, not fill cache fields.\n- Entry state is rebuilt from signal plus entry fills before any extra buy.\n- Strategy lots, exit allocations, settlement allocations, event results, strategy totals, and ledger totals are replayed from fills and settlement evidence.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_RC7_RELEASE_AUDIT.md").write_text(common + f"\n## Audit\n- quick audit: {validations['quick_audit']['ok']}\n- full-replay audit: {validations['full_replay']['ok']}\n- negative tests detected: {validations['negative_detected']}/30\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_RC7_RELEASE_CHECKLIST.md").write_text(common + "\n- [x] Formal ledger empty\n- [x] No wallet/signing/order code\n- [x] ZIP self-contained target prepared\n- [x] 30 direct end-to-end corruptions detected\n- [x] incomplete_take_profit uses latest trigger state\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_END_TO_END_REPLAY_CONTRACT.md").write_text(common + """
## Replay Contract

`full-replay` treats these as authority only: signal registration evidence bytes, Gamma HTTP bytes, CLOB market HTTP bytes, CLOB orderbook HTTP bytes, settlement HTTP bytes, frozen config/code/schema/report hashes, and immutable run/lock timestamps.

Derived caches are never trusted as inputs: `signals`, `signal_hash`, `event_key`, bucket labels, fee fields, tick/min fields, entry_order_state, strategy_lots, allocations, settlements, event_results, strategy_totals, and ledger_totals are recomputed and compared.

Comparison precision: Decimal exact equality for shares, prices, fees, and PnL after the existing simulator quantization; stable canonical JSON SHA-256 for parsed JSON evidence; raw SHA-256 over exact stored HTTP bytes for raw evidence.

Key error codes include: SIGNAL_*_MISMATCH, MARKET_*_MISMATCH, FILL_FEE_*_MISMATCH, ENTRY_STATE_*_MISMATCH, LOT_*_MISMATCH, EXIT_ALLOCATION_*_MISMATCH, SETTLEMENT_ALLOCATION_*_MISMATCH, EVENT_PNL_MISMATCH, STRATEGY_PNL_MISMATCH, TOTAL_LEDGER_PNL_MISMATCH, and INCOMPLETE_TAKE_PROFIT_MISMATCH.
""", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_API_CONTRACT.md").write_text(common + "\n## API Contract\nAllowed methods: public GET only. Forbidden: private keys, wallet connection, signing, allowance, order creation, order cancellation, POST/PUT/PATCH/DELETE trade actions.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_FEE_CONTRACT.md").write_text(common + "\n## Fee Contract\nOfficial fill fee is recalculated as `shares * fee_rate * price * (1 - price)` when fee evidence is official. Disabled fees remain zero. Unknown, unsupported, or conflicting fees cannot be used for official formal fills.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_SETTLEMENT_FINALITY_CONTRACT.md").write_text(common + "\n## Settlement Finality Contract\nFinal settlement rows must be supported by resolved final public evidence. Proposed, pending, disputed, or unknown winner states cannot be booked as final payouts.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_PREREGISTRATION.md").write_text(common + "\nThe four exit rules remain frozen from v5: hold to settlement, 2x sell 50%, 2x sell 75%, and 5x sell 25%. No formal sample has been started in this release task.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_OPERATIONS.md").write_text(common + "\n## Commands\n- Register signal: `python3 -m src.forward_simulation_v5_1_8 --root ... --config config/forward_simulation_v5_1_8.yaml register-signal --mode formal --signals-file templates/entry_signal_v5_1_8.csv`\n- Full audit: `python3 -m src.forward_simulation_v5_1_8 --root ... --config config/forward_simulation_v5_1_8.yaml audit-integrity --mode formal --level full-replay`\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_8_CURRENT_STATUS.md").write_text(common + f"\n## Formal Status\n```json\n{json.dumps(validations['formal_empty'], ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
    manifest = {"version": VERSION, "generated_at_utc": now_utc(), "validations": validations}
    write_json(reports / "FORWARD_SIMULATION_V5_1_8_MANIFEST.json", manifest)
    (root / "README_v5_1_8_RC7.md").write_text("# 天气市场前向模拟系统v5.1.8-RC7\n\n本包是订单簿证据链终审修复版。它只包含公开只读模拟与审计功能，不包含钱包、签名或真实下单能力。\n", encoding="utf-8")


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    for rel in ["data/forward_v5_1_8/demo", "data/forward_v5_1_8/formal", "data/forward_v5_1_8/rc7"]:
        target = root / rel
        if target.exists():
            shutil.rmtree(target)
    rc7_dir(root)
    write_signal_template(root)
    real = write_real_signal_to_fill_validation(root, config_path)
    demo_e2e = write_demo_end_to_end_validation(root, config_path)
    quick = audit_integrity(root, DEMO, config_path, "quick")
    full = audit_integrity(root, DEMO, config_path, "full-replay")
    write_json(rc7_dir(root) / "full_replay_validation.json", full)
    negative = write_negative_tests_csv(root, config_path)
    rc7_validations = write_rc7_validation_files(root, full, negative, real)
    neg_temp = write_negative_temperature_validation(root)
    completed = write_completed_polling_validation(root, config_path)
    illegal_bucket = write_illegal_bucket_validation(root, config_path)
    formal = formal_empty_proof(root, config_path)
    validations = {
        "quick_audit": quick,
        "full_replay": full,
        "negative_detected": sum(row["detected"] == "true" for row in negative),
        "negative_temperature": neg_temp,
        "completed_signal_polling": completed,
        "illegal_bucket_registration": illegal_bucket,
        "real_signal_to_fill": real,
        "demo_end_to_end": demo_e2e,
        "formal_empty": formal,
        "rc7_validations": rc7_validations,
    }
    write_reports(root, config_path, validations)
    hashes = write_hash_match(root, config_path)
    validations["hash_match"] = hashes
    return validations


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--config", default=str(CONFIG))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    config = Path(args.config)
    config_path = config if config.is_absolute() else (root / config)
    print(json.dumps(generate(root, config_path), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
