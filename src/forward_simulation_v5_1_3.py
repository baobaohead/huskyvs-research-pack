#!/usr/bin/env python3
"""SQLite-backed weather forward simulation v5.1.3-RC2.

This module is standalone. It does not import v5, v5.1, v5.1.1, or v5.1.2.
Formal monitor commands use the v5.1.3 public adapter as the only live price
source and never use historical prices or page-displayed probabilities.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from pathlib import Path
from typing import Any

try:
    from src.polymarket_public_adapter_v5_1_3 import (
        ADAPTER_VERSION,
        CLOB_BASE,
        GAMMA_BASE,
        AdapterError,
        PublicAdapter,
        calculate_fee,
        clob_token_pairs,
        consume_buy_depth,
        consume_sell_depth,
        content_hash,
        dec,
        dstr,
        extract_fee_policy,
        gamma_token_pairs,
        is_weather_market,
        json_safe,
        market_is_live_tradable,
        normalize_orderbook,
        parse_settlement_evidence,
        parse_temperature_bucket,
        parse_weather_market,
        stable_json,
        validate_token_mapping,
        write_json,
    )
except ModuleNotFoundError:
    from polymarket_public_adapter_v5_1_3 import (
        ADAPTER_VERSION,
        CLOB_BASE,
        GAMMA_BASE,
        AdapterError,
        PublicAdapter,
        calculate_fee,
        clob_token_pairs,
        consume_buy_depth,
        consume_sell_depth,
        content_hash,
        dec,
        dstr,
        extract_fee_policy,
        gamma_token_pairs,
        is_weather_market,
        json_safe,
        market_is_live_tradable,
        normalize_orderbook,
        parse_settlement_evidence,
        parse_temperature_bucket,
        parse_weather_market,
        stable_json,
        validate_token_mapping,
        write_json,
    )


getcontext().prec = 28

VERSION = "forward_simulation_v5.1.3-rc2"
SCHEMA_VERSION = "forward_v5_1_3_schema_001"
FORMAL = "formal"
DEMO = "demo"
LIVE = "live_integration"
ZERO = Decimal("0")
ONE = Decimal("1")
EPS = Decimal("0.00000001")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

STRATEGIES: dict[str, dict[str, Decimal | None | str]] = {
    "hold_to_settlement": {"multiple": None, "fraction": Decimal("0"), "stage": "hold"},
    "tp_2x_sell_50pct": {"multiple": Decimal("2"), "fraction": Decimal("0.50"), "stage": "tp_2x_once"},
    "tp_2x_sell_75pct": {"multiple": Decimal("2"), "fraction": Decimal("0.75"), "stage": "tp_2x_once"},
    "tp_5x_sell_25pct": {"multiple": Decimal("5"), "fraction": Decimal("0.25"), "stage": "tp_5x_once"},
}
STRATEGY_IDS = list(STRATEGIES)

USER_SIGNAL_FIELDS = [
    "signal_id",
    "created_at_utc",
    "city",
    "weather_date_local",
    "weather_metric",
    "temperature_bucket",
    "market_slug",
    "condition_id",
    "token_id",
    "outcome",
    "side",
    "forecast_temperature",
    "forecast_probability",
    "market_probability_at_signal",
    "intended_usd",
    "max_entry_price",
    "source",
    "notes",
]

HASH_FILES = {
    "config_sha256": "config/forward_simulation_v5_1_3.yaml",
    "core_code_sha256": "src/forward_simulation_v5_1_3.py",
    "adapter_code_sha256": "src/polymarket_public_adapter_v5_1_3.py",
    "reporting_code_sha256": "src/forward_reporting_v5_1_3.py",
    "schema_sha256": "schemas/forward_simulation_v5_1_3.sql",
    "preregistration_sha256": "reports/FORWARD_SIMULATION_V5_1_3_PREREGISTRATION.md",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(timezone.utc).isoformat()


def parse_utc(value: str, require_utc: bool = False) -> datetime:
    if not value:
        raise ValueError("missing UTC timestamp")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    dt = dt.astimezone(timezone.utc)
    if require_utc and dt.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return dt


def parse_scalar(value: str) -> Any:
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    try:
        if "." in value:
            return Decimal(value)
        return int(value)
    except Exception:
        return value.strip("\"'")


def normalize_lists(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"__list__"}:
            return value["__list__"]
        return {k: normalize_lists(v) for k, v in value.items()}
    return value


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"configuration file not found: {config_path}")
    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, out)]
    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if text.startswith("- "):
            stack[-1][1].setdefault("__list__", []).append(parse_scalar(text[2:].strip()))
            continue
        key, _, value = text.partition(":")
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value.strip())
    return normalize_lists(out)


def normalize_city(city: str) -> str:
    return " ".join(str(city or "").strip().lower().split())


def normalize_metric(metric: str) -> str:
    raw = " ".join(str(metric or "").strip().lower().split())
    return {"highest": "high", "max": "high", "highest temperature": "high", "lowest": "low", "min": "low", "lowest temperature": "low"}.get(raw, raw)


def make_event_key(city: str, weather_date_local: str, weather_metric: str) -> str:
    return "|".join([normalize_city(city), str(weather_date_local).strip(), normalize_metric(weather_metric)])


def sha256_bytes(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def data_dir(root: Path, mode: str, config: dict[str, Any]) -> Path:
    if mode == LIVE:
        return root / str(config["paths"].get("live_integration_dir", "data/forward_v5_1_3/live_integration"))
    return root / str(config["paths"].get(f"{mode}_data_dir", f"data/forward_v5_1_3/{mode}"))


def db_path(root: Path, mode: str, config: dict[str, Any]) -> Path:
    return data_dir(root, mode, config) / "ledger.sqlite3"


def schema_path(root: Path, config: dict[str, Any]) -> Path:
    p = root / str(config["paths"].get("schema_file", "schemas/forward_simulation_v5_1_3.sql"))
    return p if p.exists() else PROJECT_ROOT / str(config["paths"].get("schema_file", "schemas/forward_simulation_v5_1_3.sql"))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value)))


def init_ledger(root: Path, mode: str, config_path: Path) -> Path:
    config = load_config(config_path)
    db = db_path(root, mode, config)
    conn = connect(db)
    try:
        conn.executescript(schema_path(root, config).read_text(encoding="utf-8"))
        with conn:
            defaults = {
                "schema_version": SCHEMA_VERSION,
                "mode": mode,
                "formal_started_at_utc": "",
                "paused": "false",
                "stopped": "false",
            }
            for key, value in defaults.items():
                if get_state(conn, key, None) is None:
                    set_state(conn, key, value)
    finally:
        conn.close()
    return db


def current_hashes(root: Path, config_path: Path) -> dict[str, str]:
    config = load_config(config_path)
    paths = {
        "config_sha256": config_path,
        "core_code_sha256": root / "src/forward_simulation_v5_1_3.py",
        "adapter_code_sha256": root / "src/polymarket_public_adapter_v5_1_3.py",
        "reporting_code_sha256": root / "src/forward_reporting_v5_1_3.py",
        "schema_sha256": schema_path(root, config),
        "preregistration_sha256": root / str(config["paths"].get("preregistration_file", "reports/FORWARD_SIMULATION_V5_1_3_PREREGISTRATION.md")),
    }
    return {k: sha256_bytes(v) for k, v in paths.items() if v.exists()}


def id_for(prefix: str, payload: dict[str, Any]) -> str:
    return prefix + "_" + content_hash(payload)[:24]


def append_audit(conn: sqlite3.Connection, mode: str, run_id: str, event_type: str, payload: dict[str, Any], severity: str = "info", now: datetime | None = None) -> str:
    audit_id = id_for("aud", {"run_id": run_id, "t": now_iso(now), "event": event_type, "payload": payload})
    conn.execute(
        "INSERT OR IGNORE INTO audit_log(audit_id,run_id,created_at_utc,mode,event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?)",
        (audit_id, run_id, now_iso(now), mode, event_type, severity, stable_json(payload)),
    )
    return audit_id


def make_run_id(command: str, now: datetime | None = None) -> str:
    t = now_iso(now).replace("+00:00", "Z")
    return command.replace("_", "-") + "_" + content_hash({"command": command, "started_at": t})[:16]


def create_run(conn: sqlite3.Connection, mode: str, command: str, config_hash: str, code_hashes: dict[str, str], run_id: str | None = None, now: datetime | None = None) -> str:
    rid = run_id or make_run_id(command, now)
    conn.execute(
        "INSERT OR IGNORE INTO runs(run_id,mode,command,started_at_utc,selected_tokens_json,snapshot_count,error_count,code_hash_json,config_hash,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rid, mode, command, now_iso(now), "[]", 0, 0, stable_json(code_hashes), config_hash, "{}"),
    )
    return rid


def finalize_run(conn: sqlite3.Connection, run_id: str, selected_tokens: list[str], manifest: dict[str, Any], now: datetime | None = None) -> None:
    snapshot_count = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE run_id=?", (run_id,)).fetchone()["c"]
    error_count = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE run_id=? AND severity IN ('error','warning')", (run_id,)).fetchone()["c"]
    conn.execute(
        "UPDATE runs SET ended_at_utc=?, selected_tokens_json=?, snapshot_count=?, error_count=?, manifest_json=? WHERE run_id=?",
        (now_iso(now), stable_json(selected_tokens), snapshot_count, error_count, stable_json(manifest), run_id),
    )


def assert_formal_hashes(root: Path, mode: str, config_path: Path, conn: sqlite3.Connection) -> None:
    if mode != FORMAL:
        return
    started = get_state(conn, "formal_started_at_utc", "")
    if not started:
        raise RuntimeError("formal sample is not started")
    current = current_hashes(root, config_path)
    drift = {k: {"expected": get_state(conn, k, ""), "current": v} for k, v in current.items() if get_state(conn, k, "") != v}
    if drift:
        append_audit(conn, mode, "", "hash_freeze_reject", {"drift": drift}, "error")
        raise RuntimeError("formal hash freeze mismatch; refusing ledger write")


def start_formal(root: Path, config_path: Path, confirm: bool, now: datetime | None = None) -> dict[str, Any]:
    if not confirm:
        raise RuntimeError("start-formal requires --confirm")
    config = load_config(config_path)
    db = init_ledger(root, FORMAL, config_path)
    conn = connect(db)
    try:
        if get_state(conn, "formal_started_at_utc", ""):
            return {"status": "already_started"}
        hashes = current_hashes(root, config_path)
        with conn:
            set_state(conn, "formal_started_at_utc", now_iso(now))
            for key, value in hashes.items():
                set_state(conn, key, value)
            append_audit(conn, FORMAL, "", "formal_started_v5_1_3", {"hashes": hashes}, "info", now)
        return {"status": "started", "formal_started_at_utc": now_iso(now)}
    finally:
        conn.close()


def signal_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {k: str(row.get(k, "")) for k in USER_SIGNAL_FIELDS}


def validate_signal(row: dict[str, Any], mode: str, conn: sqlite3.Connection, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    created = parse_utc(str(row.get("created_at_utc", "")), require_utc=True)
    if mode == FORMAL:
        started = get_state(conn, "formal_started_at_utc", "")
        if not started:
            raise ValueError("formal sample is not started")
        if created < parse_utc(started) - timedelta(microseconds=1):
            raise ValueError("signal before formal start")
        delay = (now - created).total_seconds()
        if delay > int(config["sample_rules"].get("max_signal_registration_delay_seconds", 300)):
            raise ValueError("signal registration delay exceeded")
    if str(row.get("side", "")).upper() != "BUY":
        raise ValueError("only BUY entry signals are supported")
    for key in ["signal_id", "city", "weather_date_local", "weather_metric", "temperature_bucket", "market_slug", "condition_id", "token_id", "outcome"]:
        if not row.get(key):
            raise ValueError(f"{key} is required")
    intended = dec(row.get("intended_usd"))
    max_price = dec(row.get("max_entry_price"))
    if intended <= ZERO or max_price <= ZERO or max_price > ONE:
        raise ValueError("intended_usd and max_entry_price must be valid positives")
    metric = normalize_metric(str(row.get("weather_metric", "")))
    event_key = make_event_key(str(row.get("city", "")), str(row.get("weather_date_local", "")), metric)
    valid_minutes = int(config["entry"].get("entry_valid_minutes", 10))
    return {
        **signal_payload(row),
        "created_at_utc": created.isoformat(),
        "registered_at_utc": now.isoformat(),
        "city_normalized": normalize_city(str(row["city"])),
        "weather_metric": metric,
        "temperature_bucket": parse_temperature_bucket(str(row["temperature_bucket"])),
        "event_key": event_key,
        "entry_deadline_utc": (created + timedelta(minutes=valid_minutes)).isoformat(),
        "intended_usd": dstr(intended),
        "max_entry_price": dstr(max_price),
        "mode": mode,
    }


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def register_signals(root: Path, mode: str, config_path: Path, signals_file: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    rows = read_csv_rows(signals_file)
    conn = connect(db)
    accepted: list[dict[str, Any]] = []
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            for row in rows:
                sid = row.get("signal_id", "")
                try:
                    payload = validate_signal(row, mode, conn, config, now)
                    sig_hash = content_hash(signal_payload(row))
                    existing = conn.execute("SELECT * FROM signals WHERE mode=? AND signal_id=? ORDER BY row_id", (mode, sid)).fetchall()
                    if existing:
                        if any(r["signal_hash"] != sig_hash for r in existing):
                            append_audit(conn, mode, "", "signal_duplicate_conflict_rejected", {"signal_id": sid}, "warning", now)
                            continue
                        accepted.append(dict(existing[-1]))
                        continue
                    audit_id = append_audit(conn, mode, "", "signal_registered", {"signal_id": sid, "signal_hash": sig_hash, "event_key": payload["event_key"]}, "info", now)
                    conn.execute(
                        """
                        INSERT INTO signals(signal_id,signal_hash,registration_audit_id,created_at_utc,registered_at_utc,city,city_normalized,weather_date_local,weather_metric,temperature_bucket,event_key,market_slug,condition_id,token_id,outcome,side,forecast_temperature,forecast_probability,market_probability_at_signal,intended_usd,max_entry_price,entry_deadline_utc,source,notes,mode)
                        VALUES(:signal_id,:signal_hash,:registration_audit_id,:created_at_utc,:registered_at_utc,:city,:city_normalized,:weather_date_local,:weather_metric,:temperature_bucket,:event_key,:market_slug,:condition_id,:token_id,:outcome,:side,:forecast_temperature,:forecast_probability,:market_probability_at_signal,:intended_usd,:max_entry_price,:entry_deadline_utc,:source,:notes,:mode)
                        """,
                        {**payload, "signal_hash": sig_hash, "registration_audit_id": audit_id},
                    )
                    conn.execute(
                        "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, payload["token_id"], now.isoformat(), payload["intended_usd"], "0", payload["intended_usd"], "0", "pending", payload["max_entry_price"], payload["entry_deadline_utc"], "", "registered", mode),
                    )
                    accepted.append(payload)
                except Exception as exc:
                    append_audit(conn, mode, "", "signal_rejected", {"signal_id": sid, "reason": str(exc)}, "warning", now)
        return accepted
    finally:
        conn.close()


def latest_entry_state(conn: sqlite3.Connection, signal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entry_order_state WHERE signal_id=? ORDER BY row_id DESC LIMIT 1", (signal_id,)).fetchone()


def active_signals(conn: sqlite3.Connection, mode: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.* FROM signals s
        JOIN (SELECT signal_id, MAX(row_id) max_row_id FROM entry_order_state GROUP BY signal_id) latest ON latest.signal_id=s.signal_id
        JOIN entry_order_state st ON st.row_id=latest.max_row_id
        WHERE s.mode=? AND st.entry_status IN ('pending','partial')
        ORDER BY s.created_at_utc, s.signal_id
        """,
        (mode,),
    ).fetchall()


def open_signal_ids(conn: sqlite3.Connection, mode: str) -> list[str]:
    rows = conn.execute("SELECT DISTINCT signal_id FROM strategy_lots WHERE mode=?", (mode,)).fetchall()
    return sorted({r["signal_id"] for r in rows})


def get_signal(conn: sqlite3.Connection, signal_id: str, mode: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM signals WHERE signal_id=? AND mode=? ORDER BY row_id DESC LIMIT 1", (signal_id, mode)).fetchone()
    if row is None:
        raise KeyError(f"unknown signal_id: {signal_id}")
    return row


def insert_fee_validation(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, fee_policy: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO fee_validations(run_id,market_slug,condition_id,fees_enabled,clob_fee_rate,clob_fee_exponent,clob_taker_only,clob_fee_effective_from,gamma_fee_schedule,gamma_fee_rate,fee_crosscheck_status,fee_conflict_details,raw_clob_market_hash,raw_gamma_market_hash,mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            sig["market_slug"],
            sig["condition_id"],
            None if fee_policy.get("fees_enabled") is None else int(bool(fee_policy.get("fees_enabled"))),
            "" if fee_policy.get("clob_fee_rate") is None else dstr(fee_policy["clob_fee_rate"]),
            "" if fee_policy.get("clob_fee_exponent") is None else dstr(fee_policy["clob_fee_exponent"]),
            "" if fee_policy.get("clob_taker_only") is None else str(fee_policy.get("clob_taker_only")),
            str(fee_policy.get("clob_fee_effective_from") or ""),
            stable_json(fee_policy.get("gamma_fee_schedule") or {}),
            "" if fee_policy.get("gamma_fee_rate") is None else dstr(fee_policy["gamma_fee_rate"]),
            str(fee_policy.get("fee_crosscheck_status") or ""),
            str(fee_policy.get("fee_conflict_details") or ""),
            str(fee_policy.get("raw_clob_market_hash") or ""),
            str(fee_policy.get("raw_gamma_market_hash") or ""),
            mode,
        ),
    )


def insert_token_validation(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, validation: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO token_validations(run_id,signal_id,market_slug,condition_id,token_id,outcome,event_key,mapping_valid,error_message,raw_gamma_market_hash,raw_clob_market_hash,raw_orderbook_hash,mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            sig["signal_id"],
            sig["market_slug"],
            sig["condition_id"],
            sig["token_id"],
            sig["outcome"],
            sig["event_key"],
            int(bool(validation["mapping_valid"])),
            ";".join(validation.get("errors") or []),
            validation.get("raw_gamma_market_hash", ""),
            validation.get("raw_clob_market_hash", ""),
            validation.get("raw_orderbook_hash", ""),
            mode,
        ),
    )


def record_snapshot(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, purpose: str, raw_book: dict[str, Any], source_endpoint: str, now: datetime | None = None) -> tuple[str, dict[str, Any], bool]:
    book = normalize_orderbook(raw_book, sig["token_id"], sig["condition_id"])
    snapshot_id = id_for("ob", {"run_id": run_id, "token_id": sig["token_id"], "purpose": purpose, "content_hash": book["content_hash"]})
    exists = conn.execute("SELECT 1 FROM orderbook_snapshots WHERE run_id=? AND snapshot_id=?", (run_id, snapshot_id)).fetchone()
    if exists:
        return snapshot_id, book, False
    conn.execute(
        """
        INSERT INTO orderbook_snapshots(run_id,snapshot_id,content_hash,captured_at_utc,token_id,condition_id,market_slug,purpose,best_bid,best_ask,spread,tick_size,min_order_size,neg_risk,raw_orderbook_json,source_endpoint,mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            snapshot_id,
            book["content_hash"],
            now_iso(now),
            sig["token_id"],
            sig["condition_id"],
            sig["market_slug"],
            purpose,
            None if book["best_bid"] is None else dstr(book["best_bid"]),
            None if book["best_ask"] is None else dstr(book["best_ask"]),
            None if book["spread"] is None else dstr(book["spread"]),
            dstr(book["tick_size"]),
            dstr(book["min_order_size"]),
            None if book["neg_risk"] is None else int(bool(book["neg_risk"])),
            stable_json(raw_book),
            source_endpoint,
            mode,
        ),
    )
    return snapshot_id, book, True


def lot_open_rows(conn: sqlite3.Connection, signal_id: str, strategy_id: str, mode: str) -> list[dict[str, Any]]:
    lots = conn.execute("SELECT * FROM strategy_lots WHERE signal_id=? AND strategy_id=? AND mode=? ORDER BY created_at_utc,row_id", (signal_id, strategy_id, mode)).fetchall()
    out: list[dict[str, Any]] = []
    for lot in lots:
        sold = dec(conn.execute("SELECT COALESCE(SUM(CAST(allocated_shares AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND strategy_id=? AND mode=?", (lot["lot_id"], strategy_id, mode)).fetchone()["v"])
        settled = dec(conn.execute("SELECT COALESCE(SUM(CAST(settled_shares AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND strategy_id=? AND mode=?", (lot["lot_id"], strategy_id, mode)).fetchone()["v"])
        open_shares = dec(lot["entry_shares"]) - sold - settled
        if open_shares > EPS:
            unit_cost = dec(lot["net_entry_cost"]) / dec(lot["entry_shares"])
            out.append({**dict(lot), "open_shares": open_shares, "unit_cost": unit_cost})
    return out


def signal_position_conn(conn: sqlite3.Connection, signal_id: str, strategy_id: str, mode: str) -> dict[str, Decimal | None]:
    lots = lot_open_rows(conn, signal_id, strategy_id, mode)
    shares = sum((x["open_shares"] for x in lots), ZERO)
    cost = sum((x["open_shares"] * x["unit_cost"] for x in lots), ZERO)
    return {"shares": shares, "cost": cost, "avg_cost": (cost / shares if shares > ZERO else None)}


def is_settled(conn: sqlite3.Connection, signal_id: str, strategy_id: str, mode: str) -> bool:
    return conn.execute("SELECT 1 FROM settlements WHERE signal_id=? AND strategy_id=? AND mode=?", (signal_id, strategy_id, mode)).fetchone() is not None


def allocate_fifo(conn: sqlite3.Connection, run_id: str, mode: str, signal_id: str, strategy_id: str, shares: Decimal, gross: Decimal, fee_value: Decimal, net: Decimal, exit_fill_id: str, trigger_id: str) -> None:
    remaining = shares
    for lot in lot_open_rows(conn, signal_id, strategy_id, mode):
        if remaining <= EPS:
            break
        qty = min(lot["open_shares"], remaining)
        ratio = qty / shares if shares > ZERO else ZERO
        conn.execute(
            "INSERT OR IGNORE INTO exit_fill_allocations(allocation_id,run_id,exit_fill_id,trigger_id,strategy_id,signal_id,event_key,token_id,lot_id,allocated_shares,gross_exit_proceeds,exit_fee,net_exit_proceeds,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (id_for("alloc", {"exit_fill_id": exit_fill_id, "lot_id": lot["lot_id"]}), run_id, exit_fill_id, trigger_id, strategy_id, signal_id, lot["event_key"], lot["token_id"], lot["lot_id"], dstr(qty), dstr(gross * ratio), dstr(fee_value * ratio), dstr(net * ratio), mode),
        )
        remaining -= qty
    if remaining > EPS:
        raise RuntimeError("FIFO allocation could not cover sold shares")


class MarketBundle:
    def __init__(self, gamma_result: Any, clob_result: Any, book_result: Any):
        self.gamma_result = gamma_result
        self.clob_result = clob_result
        self.book_result = book_result
        self.gamma_market = gamma_result.payload
        self.clob_info = clob_result.payload
        self.raw_book = book_result.payload


class PublicMarketProvider:
    def __init__(self, adapter: PublicAdapter):
        self.adapter = adapter

    def bundle(self, sig: sqlite3.Row) -> MarketBundle:
        gamma = self.adapter.market_by_slug(sig["market_slug"])
        clob = self.adapter.clob_market_info(sig["condition_id"])
        book = self.adapter.orderbook(sig["token_id"])
        return MarketBundle(gamma, clob, book)


def process_entry_with_bundle(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, bundle: MarketBundle, now: datetime | None = None) -> dict[str, Any]:
    state = latest_entry_state(conn, sig["signal_id"])
    if not state or state["entry_status"] not in {"pending", "partial"}:
        return {"signal_id": sig["signal_id"], "status": "entry_not_active"}
    if now and now > parse_utc(sig["entry_deadline_utc"]):
        conn.execute(
            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], "expired", sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), "entry_deadline_reached", mode),
        )
        return {"signal_id": sig["signal_id"], "status": "expired"}
    fee_policy = extract_fee_policy(bundle.gamma_market, bundle.clob_info)
    insert_fee_validation(conn, run_id, mode, sig, fee_policy)
    if fee_policy["fee_crosscheck_status"] in {"conflict", "unknown"}:
        append_audit(conn, mode, run_id, "entry_fee_policy_rejected", {"signal_id": sig["signal_id"], "status": fee_policy["fee_crosscheck_status"], "details": fee_policy["fee_conflict_details"]}, "warning", now)
        return {"signal_id": sig["signal_id"], "status": "fee_policy_rejected"}
    snapshot_id, book, inserted = record_snapshot(conn, run_id, mode, sig, "entry", bundle.raw_book, bundle.book_result.url, now)
    validation = validate_token_mapping(dict(sig), bundle.gamma_market, bundle.clob_info, book)
    insert_token_validation(conn, run_id, mode, sig, validation)
    if not validation["mapping_valid"]:
        append_audit(conn, mode, run_id, "token_mapping_rejected", {"signal_id": sig["signal_id"], "errors": validation["errors"]}, "warning", now)
        return {"signal_id": sig["signal_id"], "status": "mapping_rejected"}
    duplicate = conn.execute("SELECT 1 FROM entry_fills WHERE run_id=? AND signal_id=? AND snapshot_id=?", (run_id, sig["signal_id"], snapshot_id)).fetchone()
    if duplicate or not inserted:
        return {"signal_id": sig["signal_id"], "status": "skipped_duplicate_snapshot"}
    buy = consume_buy_depth(book, dec(state["remaining_entry_usd"]), dec(sig["max_entry_price"]))
    if buy["filled_shares"] <= ZERO:
        reason = buy["status"]
        conn.execute(
            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], state["entry_status"], sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), reason, mode),
        )
        return {"signal_id": sig["signal_id"], "status": reason, "snapshot_id": snapshot_id}
    fee_calc = calculate_fee("buy", buy["filled_shares"], buy["vwap"], fee_policy)
    if fee_calc["net_cost_or_proceeds"] is None:
        return {"signal_id": sig["signal_id"], "status": "fee_unknown"}
    fill_id = id_for("entry", {"run_id": run_id, "signal_id": sig["signal_id"], "snapshot_id": snapshot_id, "gross": buy["filled_usd"]})
    new_filled_usd = dec(state["filled_entry_usd"]) + buy["filled_usd"]
    new_filled_shares = dec(state["filled_entry_shares"]) + buy["filled_shares"]
    status = "filled" if new_filled_usd >= dec(sig["intended_usd"]) - EPS else "partial"
    conn.execute(
        "INSERT INTO entry_fills(entry_fill_id,run_id,signal_id,event_key,token_id,snapshot_id,filled_at_utc,gross_entry_cost,entry_fee,net_entry_cost,filled_shares,entry_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_usd_after_fill,depth_levels_json,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fill_id, run_id, sig["signal_id"], sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(buy["filled_shares"]), dstr(buy["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(status == "filled"), dstr(max(dec(sig["intended_usd"]) - new_filled_usd, ZERO)), stable_json(buy["levels"]), mode),
    )
    for strategy_id in STRATEGY_IDS:
        conn.execute(
            "INSERT INTO strategy_lots(lot_id,run_id,strategy_id,signal_id,event_key,token_id,entry_fill_id,created_at_utc,entry_shares,gross_entry_cost,entry_fee,net_entry_cost,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (id_for("lot", {"strategy_id": strategy_id, "entry_fill_id": fill_id}), run_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], fill_id, now_iso(now), dstr(buy["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), mode),
        )
    conn.execute(
        "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], dstr(new_filled_usd), dstr(max(dec(sig["intended_usd"]) - new_filled_usd, ZERO)), dstr(new_filled_shares), status, sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), status, mode),
    )
    return {"signal_id": sig["signal_id"], "status": status, "snapshot_id": snapshot_id, "filled_shares": dstr(buy["filled_shares"])}


def latest_trigger(conn: sqlite3.Connection, signal_id: str, strategy_id: str, stage: str, mode: str) -> sqlite3.Row | None:
    trigger_id = id_for("trig", {"signal_id": signal_id, "strategy_id": strategy_id, "stage": stage})
    return conn.execute("SELECT * FROM strategy_triggers WHERE trigger_id=? AND mode=? ORDER BY row_id DESC LIMIT 1", (trigger_id, mode)).fetchone()


def create_trigger(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, strategy_id: str, position: dict[str, Decimal | None], now: datetime | None = None) -> sqlite3.Row:
    strategy = STRATEGIES[strategy_id]
    assert strategy["multiple"] is not None
    target = position["shares"] * strategy["fraction"]  # type: ignore[operator]
    threshold = position["avg_cost"] * strategy["multiple"]  # type: ignore[operator]
    trigger_id = id_for("trig", {"signal_id": sig["signal_id"], "strategy_id": strategy_id, "stage": strategy["stage"]})
    conn.execute(
        "INSERT INTO strategy_triggers(trigger_id,run_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,trigger_created_at,trigger_target_shares,trigger_filled_shares,trigger_remaining_shares,trigger_status,trigger_completed_at,rolling_avg_cost_at_trigger,threshold_price,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trigger_id, run_id, sig["signal_id"], strategy_id, strategy["stage"], sig["event_key"], sig["token_id"], now_iso(now), dstr(target), "0", dstr(target), "open", "", dstr(position["avg_cost"]), dstr(threshold), mode),
    )
    return latest_trigger(conn, sig["signal_id"], strategy_id, str(strategy["stage"]), mode)  # type: ignore[return-value]


def update_trigger(conn: sqlite3.Connection, trigger: sqlite3.Row, filled_add: Decimal, now: datetime | None = None) -> None:
    filled = dec(trigger["trigger_filled_shares"]) + filled_add
    remaining = max(dec(trigger["trigger_target_shares"]) - filled, ZERO)
    status = "completed" if remaining <= EPS else "open"
    conn.execute(
        "INSERT INTO strategy_triggers(trigger_id,run_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,trigger_created_at,trigger_target_shares,trigger_filled_shares,trigger_remaining_shares,trigger_status,trigger_completed_at,rolling_avg_cost_at_trigger,threshold_price,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trigger["trigger_id"], trigger["run_id"], trigger["signal_id"], trigger["strategy_id"], trigger["trigger_stage_id"], trigger["event_key"], trigger["token_id"], trigger["trigger_created_at"], trigger["trigger_target_shares"], dstr(filled), dstr(remaining), status, now_iso(now) if status == "completed" else "", trigger["rolling_avg_cost_at_trigger"], trigger["threshold_price"], trigger["mode"]),
    )


def process_exit_with_bundle(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, bundle: MarketBundle, now: datetime | None = None) -> list[dict[str, Any]]:
    if all(is_settled(conn, sig["signal_id"], st, mode) for st in STRATEGY_IDS):
        return [{"signal_id": sig["signal_id"], "status": "already_settled"}]
    fee_policy = extract_fee_policy(bundle.gamma_market, bundle.clob_info)
    if fee_policy["fee_crosscheck_status"] in {"conflict", "unknown"}:
        append_audit(conn, mode, run_id, "exit_fee_policy_rejected", {"signal_id": sig["signal_id"], "status": fee_policy["fee_crosscheck_status"]}, "warning", now)
        return [{"signal_id": sig["signal_id"], "status": "fee_policy_rejected"}]
    snapshot_id, book, inserted = record_snapshot(conn, run_id, mode, sig, "exit", bundle.raw_book, bundle.book_result.url, now)
    validation = validate_token_mapping(dict(sig), bundle.gamma_market, bundle.clob_info, book)
    if not validation["mapping_valid"]:
        append_audit(conn, mode, run_id, "exit_mapping_rejected", {"signal_id": sig["signal_id"], "errors": validation["errors"]}, "warning", now)
        return [{"signal_id": sig["signal_id"], "status": "mapping_rejected"}]
    if not inserted:
        return [{"signal_id": sig["signal_id"], "status": "skipped_duplicate_snapshot"}]
    results = []
    for strategy_id, strategy in STRATEGIES.items():
        if strategy["multiple"] is None or is_settled(conn, sig["signal_id"], strategy_id, mode):
            continue
        position = signal_position_conn(conn, sig["signal_id"], strategy_id, mode)
        if position["shares"] <= EPS or position["avg_cost"] is None:  # type: ignore[operator]
            continue
        trigger = latest_trigger(conn, sig["signal_id"], strategy_id, str(strategy["stage"]), mode)
        if trigger and trigger["trigger_status"] == "completed":
            continue
        planned = dec(trigger["trigger_remaining_shares"]) if trigger else position["shares"] * strategy["fraction"]  # type: ignore[operator]
        threshold = dec(trigger["threshold_price"]) if trigger else position["avg_cost"] * strategy["multiple"]  # type: ignore[operator]
        sell = consume_sell_depth(book, planned)
        if sell["filled_shares"] <= ZERO or sell["vwap"] is None or sell["vwap"] < threshold:
            results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "not_triggered", "threshold": dstr(threshold), "vwap": None if sell["vwap"] is None else dstr(sell["vwap"])})
            continue
        trigger = trigger or create_trigger(conn, run_id, mode, sig, strategy_id, position, now)
        duplicate = conn.execute("SELECT 1 FROM exit_fills WHERE run_id=? AND trigger_id=? AND snapshot_id=?", (run_id, trigger["trigger_id"], snapshot_id)).fetchone()
        if duplicate:
            continue
        fee_calc = calculate_fee("sell", sell["filled_shares"], sell["vwap"], fee_policy)
        if fee_calc["net_cost_or_proceeds"] is None:
            continue
        fill_id = id_for("exit", {"run_id": run_id, "trigger_id": trigger["trigger_id"], "snapshot_id": snapshot_id, "shares": sell["filled_shares"]})
        conn.execute(
            "INSERT INTO exit_fills(exit_fill_id,run_id,trigger_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,snapshot_id,filled_at_utc,planned_sell_shares,filled_shares,gross_exit_proceeds,exit_fee,net_exit_proceeds,exit_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_trigger_shares_after_fill,depth_levels_json,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fill_id, run_id, trigger["trigger_id"], sig["signal_id"], strategy_id, trigger["trigger_stage_id"], sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(planned), dstr(sell["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(sell["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(sell["remaining_shares"] <= EPS), dstr(max(planned - sell["filled_shares"], ZERO)), stable_json(sell["levels"]), mode),
        )
        allocate_fifo(conn, run_id, mode, sig["signal_id"], strategy_id, sell["filled_shares"], fee_calc["gross_notional"], fee_calc["official_fee"], fee_calc["net_cost_or_proceeds"], fill_id, trigger["trigger_id"])
        update_trigger(conn, trigger, sell["filled_shares"], now)
        results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "exit_filled", "filled_shares": dstr(sell["filled_shares"])})
    return results


def settle_signal_with_market(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, gamma_market: dict[str, Any], clob_info: dict[str, Any], source_endpoint: str, now: datetime | None = None) -> list[dict[str, Any]]:
    pairs = clob_token_pairs(clob_info) or gamma_token_pairs(gamma_market)
    evidence = parse_settlement_evidence(gamma_market, pairs)
    if not evidence["evidence_valid"]:
        append_audit(conn, mode, run_id, "settlement_not_recorded", {"signal_id": sig["signal_id"], "status": evidence["settlement_status"], "error": evidence["error"]}, "info", now)
        return [{"signal_id": sig["signal_id"], "status": evidence["settlement_status"]}]
    value = dec(evidence["token_settlement_values"].get(sig["token_id"], ""))
    results = []
    for strategy_id in STRATEGY_IDS:
        if is_settled(conn, sig["signal_id"], strategy_id, mode):
            continue
        lots = lot_open_rows(conn, sig["signal_id"], strategy_id, mode)
        remaining = sum((x["open_shares"] for x in lots), ZERO)
        gross = remaining * value
        fee_calc = calculate_fee("settlement", remaining, value, {"fee_status": "settlement_fee_not_confirmed"})
        settlement_id = id_for("set", {"signal_id": sig["signal_id"], "strategy_id": strategy_id, "value": value})
        conn.execute(
            "INSERT OR IGNORE INTO settlements(settlement_id,run_id,signal_id,strategy_id,event_key,condition_id,token_id,source_endpoint,source_reference,observed_at_utc,recorded_at_utc,raw_response,raw_response_hash,market_status,resolution_status,winning_asset_id,winning_outcome,token_settlement_values,evidence_valid,settlement_value,remaining_shares_settled,gross_settlement_proceeds,settlement_fee,net_settlement_proceeds,fee_status,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (settlement_id, run_id, sig["signal_id"], strategy_id, sig["event_key"], sig["condition_id"], sig["token_id"], source_endpoint, sig["market_slug"], now_iso(now), now_iso(now), stable_json(gamma_market), evidence["raw_response_hash"], evidence["market_status"], str(evidence.get("resolution_status") or ""), evidence["winning_asset_id"], evidence["winning_outcome"], stable_json(evidence["token_settlement_values"]), int(True), dstr(value), dstr(remaining), dstr(gross), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), fee_calc["fee_status"], mode),
        )
        for lot in lots:
            ratio = lot["open_shares"] / remaining if remaining > ZERO else ZERO
            conn.execute(
                "INSERT OR IGNORE INTO settlement_allocations(settlement_allocation_id,run_id,settlement_id,strategy_id,signal_id,event_key,token_id,lot_id,settled_shares,gross_settlement_proceeds,settlement_fee,net_settlement_proceeds,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id_for("set_alloc", {"settlement_id": settlement_id, "lot_id": lot["lot_id"]}), run_id, settlement_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], lot["lot_id"], dstr(lot["open_shares"]), dstr(gross * ratio), "0", dstr(gross * ratio), mode),
            )
        results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "settled", "value": dstr(value)})
    return results


def monitor_once(root: Path, mode: str, config_path: Path, run_id: str | None = None, adapter: PublicAdapter | None = None, now: datetime | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    adapter = adapter or PublicAdapter(config["public_api"].get("gamma_base", GAMMA_BASE), config["public_api"].get("clob_base", CLOB_BASE), config["public_api"].get("timeout_seconds", 10), int(config["public_api"].get("max_retries", 2)), config["public_api"].get("backoff_seconds", Decimal("0.5")))
    provider = PublicMarketProvider(adapter)
    conn = connect(db)
    results: list[dict[str, Any]] = []
    selected_tokens: set[str] = set()
    now = (now or utcnow()).astimezone(timezone.utc)
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            rid = create_run(conn, mode, "monitor_once", current_hashes(root, config_path).get("config_sha256", ""), current_hashes(root, config_path), run_id, now)
            signals = {r["signal_id"]: r for r in active_signals(conn, mode)}
            for sid in open_signal_ids(conn, mode):
                signals.setdefault(sid, get_signal(conn, sid, mode))
            for sig in sorted(signals.values(), key=lambda r: (r["created_at_utc"], r["signal_id"])):
                selected_tokens.add(sig["token_id"])
                try:
                    bundle = provider.bundle(sig)
                    results.append(process_entry_with_bundle(conn, rid, mode, sig, bundle, now))
                    results.extend(process_exit_with_bundle(conn, rid, mode, sig, bundle, now))
                    results.extend(settle_signal_with_market(conn, rid, mode, sig, bundle.gamma_market, bundle.clob_info, bundle.gamma_result.url, now))
                except Exception as exc:
                    append_audit(conn, mode, rid, "monitor_market_error", {"signal_id": sig["signal_id"], "error": str(exc)}, "error", now)
                    results.append({"signal_id": sig["signal_id"], "status": "error", "error": str(exc)})
            aggregate_results_conn(conn, mode)
            finalize_run(conn, rid, sorted(selected_tokens), {"adapter_version": ADAPTER_VERSION, "results": results}, now)
        return {"run_id": rid, "results": json_safe(results)}
    finally:
        conn.close()


def run_loop(root: Path, mode: str, config_path: Path, iterations: int, interval_seconds: Decimal, run_id: str | None = None) -> dict[str, Any]:
    completed = 0
    rid = run_id or make_run_id("run_loop")
    while iterations <= 0 or completed < iterations:
        monitor_once(root, mode, config_path, run_id=rid)
        completed += 1
        if completed < iterations and interval_seconds > ZERO:
            time.sleep(float(interval_seconds))
    return {"run_id": rid, "iterations_completed": completed}


def aggregate_results_conn(conn: sqlite3.Connection, mode: str) -> list[dict[str, Any]]:
    conn.execute("DELETE FROM event_results WHERE mode=?", (mode,))
    rows: list[dict[str, Any]] = []
    event_keys = [r["event_key"] for r in conn.execute("SELECT DISTINCT event_key FROM signals WHERE mode=? ORDER BY event_key", (mode,))]
    for event_key in event_keys:
        signals = conn.execute("SELECT * FROM signals WHERE event_key=? AND mode=?", (event_key, mode)).fetchall()
        traded_signal_ids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM entry_fills WHERE event_key=? AND mode=?", (event_key, mode))}
        for strategy_id in STRATEGY_IDS:
            gross_entry = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_entry_cost AS REAL)),0) v FROM entry_fills WHERE event_key=? AND mode=?", (event_key, mode)).fetchone()["v"])
            entry_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_fee AS REAL)),0) v FROM entry_fills WHERE event_key=? AND mode=?", (event_key, mode)).fetchone()["v"])
            gross_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            exit_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(exit_fee AS REAL)),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            gross_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            settlement_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(settlement_fee AS REAL)),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            settled_ids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM settlements WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode))}
            settled_event = int(bool(traded_signal_ids) and all(sid in settled_ids for sid in traded_signal_ids))
            total_fees = entry_fee + exit_fee + settlement_fee
            gross_pnl = gross_exit + gross_settlement - gross_entry
            net_pnl = gross_pnl - total_fees
            row = {
                "event_key": event_key,
                "strategy_id": strategy_id,
                "mode": mode,
                "signal_count": len(signals),
                "position_count": len(traded_signal_ids),
                "traded_event_count": int(bool(traded_signal_ids)),
                "settled_event_count": settled_event,
                "gross_entry_cost": gross_entry,
                "entry_fee": entry_fee,
                "gross_exit_proceeds": gross_exit,
                "exit_fee": exit_fee,
                "gross_settlement_proceeds": gross_settlement,
                "settlement_fee": settlement_fee,
                "total_fees": total_fees,
                "gross_pnl": gross_pnl if settled_event else None,
                "net_pnl": net_pnl if settled_event else None,
                "triggered_take_profit": int(conn.execute("SELECT 1 FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? AND mode=? LIMIT 1", (event_key, strategy_id, mode)).fetchone() is not None),
                "incomplete_take_profit": int(conn.execute("SELECT 1 FROM strategy_triggers WHERE event_key=? AND strategy_id=? AND mode=? AND trigger_status='open' LIMIT 1", (event_key, strategy_id, mode)).fetchone() is not None),
            }
            conn.execute(
                "INSERT INTO event_results(event_key,strategy_id,mode,signal_count,position_count,traded_event_count,settled_event_count,gross_entry_cost,entry_fee,gross_exit_proceeds,exit_fee,gross_settlement_proceeds,settlement_fee,total_fees,gross_pnl,net_pnl,triggered_take_profit,incomplete_take_profit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_key, strategy_id, mode, row["signal_count"], row["position_count"], row["traded_event_count"], row["settled_event_count"], dstr(gross_entry), dstr(entry_fee), dstr(gross_exit), dstr(exit_fee), dstr(gross_settlement), dstr(settlement_fee), dstr(total_fees), None if row["gross_pnl"] is None else dstr(row["gross_pnl"]), None if row["net_pnl"] is None else dstr(row["net_pnl"]), row["triggered_take_profit"], row["incomplete_take_profit"]),
            )
            rows.append(row)
    return rows


def aggregate_results(root: Path, mode: str, config_path: Path) -> list[dict[str, Any]]:
    config = load_config(config_path)
    conn = connect(db_path(root, mode, config))
    try:
        with conn:
            return aggregate_results_conn(conn, mode)
    finally:
        conn.close()


def status(root: Path, mode: str, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    db = db_path(root, mode, config)
    if mode == FORMAL and not db.exists():
        return {
            "version": VERSION,
            "mode": mode,
            "config_path": str(config_path),
            "ledger_path": str(db),
            "formal_started_at_utc": None,
            "signals": 0,
            "snapshots": 0,
            "entry_fills": 0,
            "exit_fills": 0,
            "settlements": 0,
            "event_results": 0,
            "runs": 0,
        }
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        return {
            "version": VERSION,
            "mode": mode,
            "config_path": str(config_path),
            "ledger_path": str(db),
            "formal_started_at_utc": get_state(conn, "formal_started_at_utc", None),
            "signals": conn.execute("SELECT COUNT(*) c FROM signals WHERE mode=?", (mode,)).fetchone()["c"],
            "snapshots": conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE mode=?", (mode,)).fetchone()["c"],
            "entry_fills": conn.execute("SELECT COUNT(*) c FROM entry_fills WHERE mode=?", (mode,)).fetchone()["c"],
            "exit_fills": conn.execute("SELECT COUNT(*) c FROM exit_fills WHERE mode=?", (mode,)).fetchone()["c"],
            "settlements": conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=?", (mode,)).fetchone()["c"],
            "event_results": conn.execute("SELECT COUNT(*) c FROM event_results WHERE mode=?", (mode,)).fetchone()["c"],
            "runs": conn.execute("SELECT COUNT(*) c FROM runs WHERE mode=?", (mode,)).fetchone()["c"],
        }
    finally:
        conn.close()


def audit_integrity(root: Path, mode: str, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    db = db_path(root, mode, config)
    if mode == FORMAL and not db.exists():
        return {"ok": True, "checks": {"formal_ledger_absent": 0}}
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    checks: dict[str, Any] = {}
    try:
        checks["duplicate_snapshot_in_run"] = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT run_id,snapshot_id,COUNT(*) n FROM orderbook_snapshots WHERE mode=? GROUP BY run_id,snapshot_id HAVING n>1)",
            (mode,),
        ).fetchone()["c"]
        checks["fee_conflicts_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN fee_validations fv ON ef.run_id=fv.run_id AND ef.mode=fv.mode WHERE ef.mode=? AND fv.fee_crosscheck_status='conflict'", (mode,)).fetchone()["c"]
        checks["mapping_invalid_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN token_validations tv ON ef.run_id=tv.run_id AND ef.signal_id=tv.signal_id WHERE ef.mode=? AND tv.mapping_valid=0", (mode,)).fetchone()["c"]
        checks["settled_after_exit"] = conn.execute(
            "SELECT COUNT(*) c FROM exit_fills e JOIN settlements s ON e.signal_id=s.signal_id AND e.strategy_id=s.strategy_id AND e.mode=s.mode WHERE e.mode=? AND e.filled_at_utc > s.recorded_at_utc",
            (mode,),
        ).fetchone()["c"]
        checks["gross_minus_fees_net_mismatch"] = 0
        aggregate_results_conn(conn, mode)
        for row in conn.execute("SELECT * FROM event_results WHERE mode=? AND net_pnl IS NOT NULL", (mode,)):
            gross = dec(row["gross_pnl"])
            fees = dec(row["total_fees"])
            net = dec(row["net_pnl"])
            if abs((gross - fees) - net) > Decimal("0.00001"):
                checks["gross_minus_fees_net_mismatch"] += 1
        for sid in [r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM signals WHERE mode=?", (mode,))]:
            for strategy_id in STRATEGY_IDS:
                bought = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_shares AS REAL)),0) v FROM strategy_lots WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
                sold = dec(conn.execute("SELECT COALESCE(SUM(CAST(allocated_shares AS REAL)),0) v FROM exit_fill_allocations WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
                settled = dec(conn.execute("SELECT COALESCE(SUM(CAST(settled_shares AS REAL)),0) v FROM settlement_allocations WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
                if sold + settled - bought > EPS:
                    checks["inventory_oversold"] = checks.get("inventory_oversold", 0) + 1
        checks.setdefault("inventory_oversold", 0)
        ok = not any(v for v in checks.values())
        return {"ok": ok, "checks": checks}
    finally:
        conn.close()


def write_signal_template(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, USER_SIGNAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class FixtureAdapter(PublicAdapter):
    def __init__(self, market: dict[str, Any], clob: dict[str, Any], books: list[dict[str, Any]]):
        super().__init__(transport=self.transport)
        self.market = market
        self.clob = clob
        self.books = books
        self.calls = 0

    def transport(self, url: str, method: str, timeout: float) -> tuple[int, str]:
        self.visited_endpoints.append({"method": "GET", "url": url, "status_code": 200, "latency_ms": "0"})
        if "/markets/slug/" in url:
            return 200, json.dumps(self.market)
        if "/clob-markets/" in url:
            return 200, json.dumps(self.clob)
        if "/book" in url:
            book = self.books[min(self.calls, len(self.books) - 1)]
            self.calls += 1
            return 200, json.dumps(book)
        return 200, "{}"


def demo_fixture() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    market = {
        "question": "Highest temperature in Demo City on January 2?",
        "title": "Highest temperature in Demo City on January 2?",
        "slug": "highest-temperature-in-demo-city-on-january-2-2099-30c",
        "conditionId": "0xdemo",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        "outcomePrices": json.dumps(["0", "1"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.05", "exponent": "1"},
        "endDate": "2099-01-02T23:59:00Z",
        "groupItemTitle": "30C",
        "acceptingOrders": True,
    }
    clob = {"condition_id": "0xdemo", "t": [{"t": "yes-token", "o": "Yes"}, {"t": "no-token", "o": "No"}], "fd": {"r": "0.05", "e": "1", "to": True}}
    entry_book = {"market": "0xdemo", "asset_id": "yes-token", "timestamp": "1", "hash": "h1", "bids": [{"price": "0.09", "size": "1000"}], "asks": [{"price": "0.10", "size": "1000"}], "min_order_size": "5", "tick_size": "0.001", "neg_risk": False}
    exit_book = {"market": "0xdemo", "asset_id": "yes-token", "timestamp": "2", "hash": "h2", "bids": [{"price": "0.30", "size": "200"}], "asks": [{"price": "0.31", "size": "1000"}], "min_order_size": "5", "tick_size": "0.001", "neg_risk": False}
    signal = {
        "signal_id": "demo-signal-1",
        "created_at_utc": "2099-01-01T00:00:00+00:00",
        "city": "Demo City",
        "weather_date_local": "2099-01-02",
        "weather_metric": "high",
        "temperature_bucket": "30C",
        "market_slug": market["slug"],
        "condition_id": "0xdemo",
        "token_id": "yes-token",
        "outcome": "Yes",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.66",
        "market_probability_at_signal": "0.10",
        "intended_usd": "20",
        "max_entry_price": "0.11",
        "source": "v5.1.3_demo_fixture",
        "notes": "",
    }
    return market, clob, [entry_book, exit_book], signal


def demo_run(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    init_ledger(root, DEMO, config_path)
    market, clob, books, signal = demo_fixture()
    signal_file = data_dir(root, DEMO, config) / "demo_signal.csv"
    write_signal_template(signal_file, [signal])
    register_signals(root, DEMO, config_path, signal_file, now=parse_utc("2099-01-01T00:00:01+00:00"))
    adapter = FixtureAdapter(market, clob, books)
    result = monitor_once(root, DEMO, config_path, run_id="demo_run_fixture", adapter=adapter, now=parse_utc("2099-01-01T00:00:02+00:00"))
    # Flip market to resolved and settle in a second monitor pass.
    market["active"] = False
    market["closed"] = True
    market["resolved"] = True
    market["winningOutcome"] = "Yes"
    market["outcomePrices"] = json.dumps(["1", "0"])
    adapter = FixtureAdapter(market, clob, [books[-1]])
    settle_result = monitor_once(root, DEMO, config_path, run_id="demo_run_settlement", adapter=adapter, now=parse_utc("2099-01-03T00:00:00+00:00"))
    return {"demo_signal_file": str(signal_file), "monitor": result, "settlement": settle_result, "audit": audit_integrity(root, DEMO, config_path)}


def discover_weather_markets(adapter: PublicAdapter, config: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in config.get("live_integration", {}).get("search_terms", ["temperature"]):
        try:
            res = adapter.search(str(term), limit_per_type=10, events_status="active", keep_closed_markets=0)
        except AdapterError:
            continue
        for event in (res.payload.get("events") if isinstance(res.payload, dict) else []) or []:
            title = str(event.get("title") or "")
            for market in event.get("markets") or []:
                slug = str(market.get("slug") or "")
                if slug and slug not in seen and market_is_live_tradable(market) and is_weather_market(market, title):
                    market["_event_title"] = title
                    found.append(market)
                    seen.add(slug)
        if len(found) >= 2:
            break
    return found


def fetch_resolved_weather_evidence(adapter: PublicAdapter, config: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    for slug in config.get("live_integration", {}).get("resolved_weather_slugs", []):
        try:
            gamma = adapter.market_by_slug(str(slug))
            condition_id = str(gamma.payload.get("conditionId") or "")
            clob = adapter.clob_market_info(condition_id) if condition_id else None
            pairs = clob_token_pairs(clob.payload) if clob else gamma_token_pairs(gamma.payload)
            evidence = parse_settlement_evidence(gamma.payload, pairs)
            payload = {"slug": slug, "market_endpoint": gamma.url, "clob_endpoint": clob.url if clob else "", "evidence": evidence, "raw_market_hash": content_hash(gamma.payload)}
            write_json(out_dir / "resolved_weather_evidence.json", payload)
            if evidence.get("market_status") == "resolved":
                return payload
        except Exception as exc:
            write_json(out_dir / "resolved_weather_evidence_error.json", {"slug": slug, "error": str(exc)})
    return {"error": "no_resolved_weather_evidence_found"}


def live_integration(root: Path, config_path: Path, iterations: int, interval_seconds: Decimal, run_id: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    adapter = PublicAdapter(config["public_api"].get("gamma_base", GAMMA_BASE), config["public_api"].get("clob_base", CLOB_BASE), config["public_api"].get("timeout_seconds", 10), int(config["public_api"].get("max_retries", 2)), config["public_api"].get("backoff_seconds", Decimal("0.5")))
    rid = run_id or make_run_id("live_integration")
    out_dir = data_dir(root, LIVE, config) / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = discover_weather_markets(adapter, config)[:2]
    write_json(out_dir / "selected_markets.json", selected)
    snapshots: list[dict[str, Any]] = []
    started = utcnow()
    for i in range(iterations):
        for market in selected:
            try:
                slug = str(market.get("slug") or "")
                gamma = adapter.market_by_slug(slug)
                condition_id = str(gamma.payload.get("conditionId") or "")
                clob = adapter.clob_market_info(condition_id)
                pairs = clob_token_pairs(clob.payload) or gamma_token_pairs(gamma.payload)
                token = pairs[0]["token_id"]
                book = adapter.orderbook(token)
                normalized = normalize_orderbook(book.payload, token, condition_id)
                row = {
                    "run_id": rid,
                    "captured_at_utc": book.received_at_utc,
                    "market_slug": slug,
                    "condition_id": condition_id,
                    "token_id": token,
                    "snapshot_id": id_for("live", {"run_id": rid, "token": token, "content": normalized["content_hash"]}),
                    "content_hash": normalized["content_hash"],
                    "best_bid": normalized["best_bid"],
                    "best_ask": normalized["best_ask"],
                    "tick_size": normalized["tick_size"],
                    "min_order_size": normalized["min_order_size"],
                    "endpoint": book.url,
                }
                snapshots.append(row)
                write_json(out_dir / "raw_orderbooks" / f"{len(snapshots):05d}_{token}.json", {"http": json_safe(book.__dict__), "raw": book.payload})
            except Exception as exc:
                with (out_dir / "audit_log.jsonl").open("a", encoding="utf-8") as f:
                    f.write(stable_json({"run_id": rid, "created_at_utc": now_iso(), "event_type": "live_orderbook_error", "error": str(exc), "market": market.get("slug")}) + "\n")
        if i < iterations - 1 and interval_seconds > ZERO:
            time.sleep(float(interval_seconds))
    with (out_dir / "orderbook_snapshots.jsonl").open("w", encoding="utf-8") as f:
        for row in snapshots:
            f.write(stable_json(row) + "\n")
    evidence = fetch_resolved_weather_evidence(adapter, config, out_dir)
    ended = utcnow()
    manifest = {
        "run_id": rid,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "selected_tokens": sorted({str(s.get("token_id", "")) for s in snapshots}),
        "snapshot_count": len(snapshots),
        "error_count": len([1 for _ in (out_dir / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()]) if (out_dir / "audit_log.jsonl").exists() else 0,
        "adapter_version": ADAPTER_VERSION,
        "actual_endpoints": adapter.visited_endpoints,
        "resolved_evidence_status": evidence.get("evidence", {}).get("settlement_status", evidence.get("error", "")),
        "code_hash": current_hashes(root, config_path),
    }
    write_json(out_dir / "run_manifest.json", manifest)
    return manifest


def formal_empty_proof(root: Path, config_path: Path) -> dict[str, Any]:
    st = status(root, FORMAL, config_path)
    proof = {
        "generated_at_utc": now_iso(),
        "formal_started_at_utc": st["formal_started_at_utc"],
        "signals": st["signals"],
        "snapshots": st["snapshots"],
        "entry_fills": st["entry_fills"],
        "exit_fills": st["exit_fills"],
        "settlements": st["settlements"],
        "event_results": st["event_results"],
        "ok": st["formal_started_at_utc"] in (None, "", "null") and all(st[k] == 0 for k in ["signals", "snapshots", "entry_fills", "exit_fills", "settlements", "event_results"]),
    }
    write_json(root / "data/forward_v5_1_3/formal_empty_proof.json", proof)
    return proof


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("init"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=DEMO)
    sp = sub.add_parser("start-formal"); sp.add_argument("--confirm", action="store_true")
    sp = sub.add_parser("register-signal"); sp.add_argument("--signals-file", required=True); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("monitor-once"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--run-id")
    sp = sub.add_parser("run-loop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--iterations", type=int, default=1); sp.add_argument("--interval-seconds", default="0"); sp.add_argument("--run-id")
    sp = sub.add_parser("demo-run")
    sp = sub.add_parser("live-integration"); sp.add_argument("--iterations", type=int, default=1); sp.add_argument("--interval-seconds", default="0"); sp.add_argument("--run-id")
    sp = sub.add_parser("status"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("audit-integrity"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sub.add_parser("formal-empty-proof")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    if args.command == "init":
        print(json.dumps({"ledger": str(init_ledger(root, args.mode, config_path))}, indent=2, ensure_ascii=False))
    elif args.command == "start-formal":
        print(json.dumps(start_formal(root, config_path, args.confirm), indent=2, ensure_ascii=False))
    elif args.command == "register-signal":
        rows = register_signals(root, args.mode, config_path, Path(args.signals_file))
        print(json.dumps({"registered": len(rows)}, indent=2, ensure_ascii=False))
    elif args.command == "monitor-once":
        print(json.dumps(monitor_once(root, args.mode, config_path, args.run_id), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "run-loop":
        print(json.dumps(run_loop(root, args.mode, config_path, args.iterations, dec(args.interval_seconds), args.run_id), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "demo-run":
        print(json.dumps(json_safe(demo_run(root, config_path)), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "live-integration":
        print(json.dumps(json_safe(live_integration(root, config_path, args.iterations, dec(args.interval_seconds), args.run_id)), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "status":
        print(json.dumps(status(root, args.mode, config_path), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "audit-integrity":
        print(json.dumps(audit_integrity(root, args.mode, config_path), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "formal-empty-proof":
        print(json.dumps(formal_empty_proof(root, config_path), indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
