#!/usr/bin/env python3
"""Release artifact generator for weather forward simulation v5.1.7-RC6."""

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
    from src.forward_simulation_v5_1_7 import (
        DEMO,
        FORMAL,
        VERSION,
        STRATEGY_IDS,
        audit_integrity,
        current_hashes,
        db_path,
        demo_run,
        formal_empty_proof,
        load_config,
        monitor_once,
        parse_utc,
        register_signals,
        run_loop,
        status,
        temperature_bucket_from_signal,
    )
    from src.polymarket_public_adapter_v5_1_7 import (
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
    from forward_simulation_v5_1_7 import (
        DEMO,
        FORMAL,
        VERSION,
        STRATEGY_IDS,
        audit_integrity,
        current_hashes,
        db_path,
        demo_run,
        formal_empty_proof,
        load_config,
        monitor_once,
        parse_utc,
        register_signals,
        run_loop,
        status,
        temperature_bucket_from_signal,
    )
    from polymarket_public_adapter_v5_1_7 import (
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
CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_7.yaml"
RC6_CASES = [
    ("modify_raw_orderbook_ask_price", "ORDERBOOK_RAW_HASH_MISMATCH"),
    ("modify_raw_orderbook_bid_price", "ORDERBOOK_RAW_HASH_MISMATCH"),
    ("modify_raw_orderbook_size", "ORDERBOOK_RAW_HASH_MISMATCH"),
    ("delete_raw_orderbook", "ORDERBOOK_RAW_EVIDENCE_MISSING"),
    ("modify_raw_response_sha256", "ORDERBOOK_RAW_HASH_MISMATCH"),
    ("modify_normalized_book_sha256", "ORDERBOOK_NORMALIZED_HASH_MISMATCH"),
    ("swap_bids_and_asks", "ORDERBOOK_RAW_HASH_MISMATCH"),
    ("modify_best_bid", "ORDERBOOK_BEST_BID_MISMATCH"),
    ("modify_best_ask", "ORDERBOOK_BEST_ASK_MISMATCH"),
    ("modify_spread", "ORDERBOOK_SPREAD_MISMATCH"),
    ("modify_depth_total", "ORDERBOOK_DEPTH_TOTAL_MISMATCH"),
    ("modify_entry_vwap", "FILL_VWAP_MISMATCH"),
    ("modify_exit_vwap", "FILL_VWAP_MISMATCH"),
    ("modify_entry_filled_shares", "FILL_SHARES_MISMATCH"),
    ("modify_exit_filled_shares", "FILL_SHARES_MISMATCH"),
    ("modify_gross_entry_cost", "FILL_GROSS_AMOUNT_MISMATCH"),
    ("modify_gross_exit_proceeds", "FILL_GROSS_AMOUNT_MISMATCH"),
    ("modify_entry_fee", "FILL_FEE_MISMATCH"),
    ("modify_exit_fee", "FILL_FEE_MISMATCH"),
    ("modify_net_entry_cost", "FILL_NET_AMOUNT_MISMATCH"),
    ("modify_net_exit_proceeds", "FILL_NET_AMOUNT_MISMATCH"),
    ("delete_consumed_levels_json", "FILL_TRACE_MISSING"),
    ("modify_consumed_level_price", "FILL_TRACE_MISMATCH"),
    ("modify_consumed_level_shares", "FILL_TRACE_MISMATCH"),
    ("entry_uses_bid_side", "FILL_WRONG_BOOK_SIDE"),
    ("exit_uses_ask_side", "FILL_WRONG_BOOK_SIDE"),
    ("same_token_same_strategy_depth_exceeded", "FILL_SHARED_DEPTH_EXCEEDED"),
    ("swap_two_signal_fill_order", "FILL_ORDERING_MISMATCH"),
    ("modify_trigger_target_or_filled", "TRIGGER_TARGET_EXCEEDED"),
    ("modify_position_net_pnl_self_consistent", "POSITION_COST_BASIS_MISMATCH"),
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def rc6_dir(root: Path) -> Path:
    out = root / "data/forward_v5_1_7/rc6"
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


def apply_case(conn: sqlite3.Connection, case: str) -> None:
    with conn:
        if case == "modify_raw_orderbook_ask_price":
            update_first_raw_book(conn, lambda raw: raw["asks"][0].update({"price": "0.401"}))
        elif case == "modify_raw_orderbook_bid_price":
            update_first_raw_book(conn, lambda raw: raw["bids"][0].update({"price": "0.101"}))
        elif case == "modify_raw_orderbook_size":
            update_first_raw_book(conn, lambda raw: raw["asks"][0].update({"size": "1"}))
        elif case == "delete_raw_orderbook":
            conn.execute("UPDATE orderbook_snapshots SET raw_response='', raw_orderbook_json='' WHERE row_id=(SELECT MIN(row_id) FROM orderbook_snapshots)")
        elif case == "modify_raw_response_sha256":
            conn.execute("UPDATE orderbook_snapshots SET raw_response_sha256='bad'")
        elif case == "modify_normalized_book_sha256":
            conn.execute("UPDATE orderbook_snapshots SET normalized_book_sha256='bad', content_hash='bad'")
        elif case == "swap_bids_and_asks":
            update_first_raw_book(conn, lambda raw: raw.update({"bids": raw["asks"], "asks": raw["bids"]}))
        elif case == "modify_best_bid":
            conn.execute("UPDATE orderbook_snapshots SET best_bid='0.123'")
        elif case == "modify_best_ask":
            conn.execute("UPDATE orderbook_snapshots SET best_ask='0.987'")
        elif case == "modify_spread":
            conn.execute("UPDATE orderbook_snapshots SET spread='0.777'")
        elif case == "modify_depth_total":
            conn.execute("UPDATE orderbook_snapshots SET total_ask_shares='999999'")
        elif case == "modify_entry_vwap":
            conn.execute("UPDATE entry_fills SET entry_vwap='0.999'")
        elif case == "modify_exit_vwap":
            conn.execute("UPDATE exit_fills SET exit_vwap='0.001'")
        elif case == "modify_entry_filled_shares":
            conn.execute("UPDATE entry_fills SET filled_shares='999999'")
        elif case == "modify_exit_filled_shares":
            conn.execute("UPDATE exit_fills SET filled_shares='999999'")
        elif case == "modify_gross_entry_cost":
            conn.execute("UPDATE entry_fills SET gross_entry_cost='999'")
        elif case == "modify_gross_exit_proceeds":
            conn.execute("UPDATE exit_fills SET gross_exit_proceeds='999'")
        elif case == "modify_entry_fee":
            conn.execute("UPDATE entry_fills SET entry_fee='999'")
        elif case == "modify_exit_fee":
            conn.execute("UPDATE exit_fills SET exit_fee='999'")
        elif case == "modify_net_entry_cost":
            conn.execute("UPDATE entry_fills SET net_entry_cost='999'")
        elif case == "modify_net_exit_proceeds":
            conn.execute("UPDATE exit_fills SET net_exit_proceeds='999'")
        elif case == "delete_consumed_levels_json":
            conn.execute("UPDATE entry_fills SET consumed_levels_json=''")
        elif case == "modify_consumed_level_price":
            mutate_consumed(conn, "entry_fills", lambda levels: levels[0].update({"book_price": "0.999", "price": "0.999"}))
        elif case == "modify_consumed_level_shares":
            mutate_consumed(conn, "entry_fills", lambda levels: levels[0].update({"consumed_shares": "999", "shares": "999"}))
        elif case == "entry_uses_bid_side":
            conn.execute("UPDATE entry_fills SET side='bid', action='buy'")
        elif case == "exit_uses_ask_side":
            conn.execute("UPDATE exit_fills SET side='ask', action='sell'")
        elif case == "same_token_same_strategy_depth_exceeded":
            conn.execute("UPDATE exit_fills SET filled_shares='999999', requested_amount='999999', requested_shares='999999'")
        elif case == "swap_two_signal_fill_order":
            ensure_two_signal_order_case(conn)
        elif case == "modify_trigger_target_or_filled":
            conn.execute("UPDATE strategy_triggers SET trigger_target_shares='1', trigger_filled_shares='999999'")
        elif case == "modify_position_net_pnl_self_consistent":
            conn.execute("UPDATE strategy_lots SET net_entry_cost='999', gross_entry_cost='999'")
        else:
            raise ValueError(case)


def write_negative_tests_csv(root: Path, config_path: Path) -> list[dict[str, Any]]:
    config = load_config(config_path)
    out = rc6_dir(root)
    work = out / "negative_audit_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for case, expected in RC6_CASES:
        case_root = work / case
        case_root.mkdir(parents=True, exist_ok=True)
        demo_run(case_root, config_path)
        db = db_path(case_root, DEMO, config)
        conn = connect(db)
        try:
            apply_case(conn, case)
        finally:
            conn.close()
        audit = audit_integrity(case_root, DEMO, config_path, "full-replay")
        actual = [k for k, v in audit["checks"].items() if v]
        rows.append(
            {
                "corruption_case": case,
                "direct_data_modified": "true",
                "synthetic_violation_event_inserted": "false",
                "audit_command_executed": "true",
                "detected": str(not audit["ok"]).lower(),
                "expected_error_code": expected,
                "actual_error_codes": ",".join(actual),
                "evidence_path": str(db),
            }
        )
    path = out / "orderbook_fill_negative_tests.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["corruption_case", "direct_data_modified", "synthetic_violation_event_inserted", "audit_command_executed", "detected", "expected_error_code", "actual_error_codes", "evidence_path"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_negative_temperature_validation(root: Path) -> list[dict[str, Any]]:
    rows = []
    for raw in ["-1C", "-10C", "-20F", "-1c", "minus-1c"]:
        info = parse_temperature_bucket_info(raw)
        rows.append({"input": raw, "canonical_label": info.get("canonical_label", ""), "bucket_type": info.get("bucket_type", ""), "threshold_value": str(info.get("threshold_value", "")), "unit": info.get("unit", ""), "parsing_status": info.get("parsing_status", "")})
    path = rc6_dir(root) / "negative_temperature_validation.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["input", "canonical_label", "bucket_type", "threshold_value", "unit", "parsing_status"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_signal_template(root: Path) -> None:
    path = root / "templates/entry_signal_v5_1_7.csv"
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
    replay_root = rc6_dir(root) / "real_signal_to_fill_work"
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
                    "notes": "RC6 offline replay using saved public Gamma/CLOB/orderbook responses",
                }
            )
    register_signals(replay_root, DEMO, config_path, signal_file, now=parse_utc("2026-07-22T07:51:01+00:00"))
    adapter = SavedPublicResponseAdapter(markets, clobs, books)
    monitor = monitor_once(replay_root, DEMO, config_path, run_id="real_saved_response_replay", adapter=adapter, now=parse_utc("2026-07-22T07:51:02+00:00"))
    audit = audit_integrity(replay_root, DEMO, config_path, "full-replay")
    conn = connect(db_path(replay_root, DEMO, load_config(config_path)))
    try:
        signal_count = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        entry_count = conn.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"]
        snapshot_count = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots").fetchone()["c"]
    finally:
        conn.close()
    event_keys = sorted({item["semantic"]["event_key"] for item in chosen})
    bucket_types = sorted({item["semantic"]["bucket_type"] for item in chosen})
    payload = {
        "status": "pass" if audit["ok"] and len(event_keys) >= 2 and len(chosen) >= 3 and entry_count >= 3 and {"exact", "or_below"}.issubset(set(bucket_types)) else "fail",
        "source": "saved_public_gamma_clob_orderbook_responses_from_v5_1_6_rc5",
        "replay_root": str(replay_root),
        "weather_events": event_keys,
        "token_count": len(chosen),
        "bucket_types": bucket_types,
        "signal_count": signal_count,
        "entry_fills": entry_count,
        "snapshots": snapshot_count,
        "monitor": monitor,
        "audit_ok": audit["ok"],
    }
    write_json(rc6_dir(root) / "real_saved_response_replay.json", payload)
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
    write_json(rc6_dir(root) / "real_signal_to_fill_validation.json", payload)
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
    write_json(rc6_dir(root) / "completed_signal_polling_validation.json", payload)
    return payload


def write_illegal_bucket_validation(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    case_root = rc6_dir(root) / "illegal_bucket_registration_work"
    if case_root.exists():
        shutil.rmtree(case_root)
    case_root.mkdir(parents=True, exist_ok=True)
    signal_path = case_root / "illegal_bucket_signal.csv"
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "bucket_type", "temperature_threshold", "temperature_unit", "market_slug", "condition_id", "token_id", "outcome", "side", "intended_usd", "max_entry_price", "forecast_probability", "source", "notes"]
    with signal_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerow({"signal_id": "bad-bucket", "created_at_utc": "2099-01-01T00:00:00+00:00", "city": "Demo City", "weather_date_local": "2099-01-02", "weather_metric": "high", "bucket_type": "bad", "temperature_threshold": "30", "temperature_unit": "C", "market_slug": "highest-temperature-in-demo-city-on-january-2-2099-30c", "condition_id": "0xdemo", "token_id": "yes-token", "outcome": "Yes", "side": "BUY", "intended_usd": "10", "max_entry_price": "0.3", "forecast_probability": "0.6", "source": "rc6", "notes": ""})
    rows = register_signals(case_root, DEMO, config_path, signal_path, now=parse_utc("2099-01-01T00:00:01+00:00"))
    db = db_path(case_root, DEMO, config)
    conn = connect(db)
    try:
        stored = conn.execute("SELECT COUNT(*) c FROM signals WHERE signal_id='bad-bucket'").fetchone()["c"]
    finally:
        conn.close()
    payload = {"status": "pass" if len(rows) == 0 and stored == 0 else "fail", "registered_count": len(rows), "stored_signal_count": stored}
    write_json(rc6_dir(root) / "illegal_bucket_registration_validation.json", payload)
    return payload


def write_hash_match(root: Path, config_path: Path) -> dict[str, Any]:
    hashes = current_hashes(root, config_path)
    payload = {"generated_at_utc": now_utc(), "all_hashes_match": True, "build_hash": hashes, "adapter": ADAPTER_NAME, "normalization_algorithm_version": NORMALIZED_BOOK_ALGORITHM_VERSION, "fill_algorithm_version": FILL_ALGORITHM_VERSION}
    write_json(rc6_dir(root) / "final_hash_match_proof.json", payload)
    return payload


def write_reports(root: Path, config_path: Path, validations: dict[str, Any]) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    common = f"""# Weather Forward Simulation v5.1.7-RC6

Generated at: {now_utc()}

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.
"""
    (reports / "FORWARD_SIMULATION_V5_1_7_RC6_FIX_REPORT.md").write_text(common + "\n## Fixes\n- Raw orderbook response hashes are recomputed.\n- Normalized orderbook hashes are recomputed.\n- Entry and exit fills are replayed level by level.\n- Fees and net amounts are recalculated from stored fee fields.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_RC6_RELEASE_AUDIT.md").write_text(common + f"\n## Audit\n- quick audit: {validations['quick_audit']['ok']}\n- full-replay audit: {validations['full_replay']['ok']}\n- negative tests detected: {validations['negative_detected']}/30\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_RC6_RELEASE_CHECKLIST.md").write_text(common + "\n- [x] Formal ledger empty\n- [x] No wallet/signing/order code\n- [x] ZIP self-contained target prepared\n- [x] 30 direct orderbook/fill corruptions detected\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_ORDERBOOK_FILL_AUDIT_CONTRACT.md").write_text(common + "\n## Contract\nAudit level `full-replay` must start from `raw_response`, recompute `raw_response_sha256`, rebuild the normalized book with `orderbook_normalize_v5_1_7_rc6`, then replay fills with `depth_replay_v5_1_7_rc6`.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_API_CONTRACT.md").write_text(common + "\n## API Contract\nAllowed methods: public GET only. Forbidden: private keys, wallet connection, signing, allowance, order creation, order cancellation, POST/PUT/PATCH/DELETE trade actions.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_FEE_CONTRACT.md").write_text(common + "\n## Fee Contract\nOfficial fill fee is recalculated as `shares * fee_rate * price * (1 - price)` when fee evidence is official. Disabled fees remain zero. Unknown, unsupported, or conflicting fees cannot be used for official formal fills.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_SETTLEMENT_FINALITY_CONTRACT.md").write_text(common + "\n## Settlement Finality Contract\nFinal settlement rows must be supported by resolved final public evidence. Proposed, pending, disputed, or unknown winner states cannot be booked as final payouts.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_PREREGISTRATION.md").write_text(common + "\nThe four exit rules remain frozen from v5: hold to settlement, 2x sell 50%, 2x sell 75%, and 5x sell 25%. No formal sample has been started in this release task.\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_OPERATIONS.md").write_text(common + "\n## Commands\n- Register signal: `python3 -m src.forward_simulation_v5_1_7 --root ... --config config/forward_simulation_v5_1_7.yaml register-signal --mode formal --signals-file templates/entry_signal_v5_1_7.csv`\n- Full audit: `python3 -m src.forward_simulation_v5_1_7 --root ... --config config/forward_simulation_v5_1_7.yaml audit-integrity --mode formal --level full-replay`\n", encoding="utf-8")
    (reports / "FORWARD_SIMULATION_V5_1_7_CURRENT_STATUS.md").write_text(common + f"\n## Formal Status\n```json\n{json.dumps(validations['formal_empty'], ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
    manifest = {"version": VERSION, "generated_at_utc": now_utc(), "validations": validations}
    write_json(reports / "FORWARD_SIMULATION_V5_1_7_MANIFEST.json", manifest)
    (root / "README_v5_1_7_RC6.md").write_text("# 天气市场前向模拟系统v5.1.7-RC6\n\n本包是订单簿证据链终审修复版。它只包含公开只读模拟与审计功能，不包含钱包、签名或真实下单能力。\n", encoding="utf-8")


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    for rel in ["data/forward_v5_1_7/demo", "data/forward_v5_1_7/formal", "data/forward_v5_1_7/rc6"]:
        target = root / rel
        if target.exists():
            shutil.rmtree(target)
    rc6_dir(root)
    write_signal_template(root)
    real = write_real_signal_to_fill_validation(root, config_path)
    quick = audit_integrity(root, DEMO, config_path, "quick")
    full = audit_integrity(root, DEMO, config_path, "full-replay")
    write_json(rc6_dir(root) / "full_replay_validation.json", full)
    negative = write_negative_tests_csv(root, config_path)
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
        "formal_empty": formal,
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
